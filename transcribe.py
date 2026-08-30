import argparse
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from math import ceil
from pathlib import Path

from dotenv import load_dotenv

from correction import (
    BACKEND_DEFAULTS,
    DEFAULT_CHUNK_THRESHOLD,
    Corrector,
    build_corrector,
    split_into_chunks,
)
from providers import TranscriptionProvider, get_provider

# Untergrenze für die LLM-Antwortlänge relativ zum Original (Plan §6):
# kürzere Antworten gelten als "verschluckter" Inhalt -> Original behalten.
CORRECTION_MIN_LENGTH_RATIO = 0.5

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm"}

# Whisper API Limit: 25 MB (Fallback für split_audio_file, wenn kein
# Provider-Limit übergeben wird).
MAX_FILE_SIZE_MB = 25

# Standard-Ersetzungen für häufige Erkennungsfehler
DEFAULT_REPLACEMENTS = {
    "Deklärgerät": "Diktiergerät",
    "Cloud": "Claude",
    "Cloud Code": "Claude Code",
    "Cloud AI": "Claude AI",
}


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    """
    Wendet Wort-Ersetzungen auf den Text an.
    Ersetzt ganze Wörter und berücksichtigt auch Wortgrenzen.
    """
    if not replacements:
        return text

    result = text
    for wrong, correct in replacements.items():
        # Ersetze exakte Übereinstimmungen (case-sensitive)
        result = result.replace(wrong, correct)

    return result


def _correct_chunk(corrector: Corrector, chunk: str, label: str) -> str:
    """
    Korrigiert einen einzelnen Abschnitt defensiv (Plan §6).

    Bei JEDEM Fehler (Server aus, Timeout, Modell fehlt, leere/zu kurze
    Antwort) wird der unkorrigierte Abschnitt zurückgegeben — Inhalt darf
    nie verloren gehen.
    """
    try:
        corrected = corrector.correct(chunk)
    except Exception as e:
        print(f"   [!] LLM-Korrektur fehlgeschlagen{label} ({e}); "
              f"behalte unkorrigierten Abschnitt.")
        return chunk

    # Schutz gegen "verschluckten" Inhalt (z. B. faelschliche Zusammenfassung).
    if len(corrected.strip()) < CORRECTION_MIN_LENGTH_RATIO * len(chunk.strip()):
        print(f"   [!] LLM-Antwort verdaechtig kurz{label} "
              f"({len(corrected.strip())} statt {len(chunk.strip())} Zeichen); "
              f"behalte unkorrigierten Abschnitt.")
        return chunk

    return corrected.strip()


def correct_text(corrector: Corrector, text: str) -> str:
    """
    Wendet die optionale LLM-Korrektur an (Plan §6 + §7).

    Lange Transkripte werden an Absatz-/Satzgrenzen in Abschnitte zerlegt,
    einzeln korrigiert und wieder zusammengefügt. Jeder Abschnitt ist defensiv
    gekapselt: ein Fehler verliert nur die Korrektur dieses Abschnitts, nie
    den Inhalt — und nie das ganze Transkript.
    """
    chunks = split_into_chunks(text)
    multi = len(chunks) > 1
    if multi:
        print(f"   [i] Langer Text ({len(text)} Zeichen) -> "
              f"Korrektur in {len(chunks)} Abschnitten.")

    parts = []
    for idx, chunk in enumerate(chunks, 1):
        label = f" (Abschnitt {idx}/{len(chunks)})" if multi else ""
        parts.append(_correct_chunk(corrector, chunk, label))

    result = "\n\n".join(p.strip() for p in parts).strip()
    print(f"   [OK] LLM-Korrektur angewendet"
          f"{f' ({len(chunks)} Abschnitte)' if multi else ''}.")
    return result


def is_audio_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTS


def get_file_size_mb(path: Path) -> float:
    """Gibt die Dateigröße in MB zurück."""
    return path.stat().st_size / (1024 * 1024)


def format_run_timing(transcribe_seconds: float, audio_duration: float) -> str:
    """Benchmark-Notiz: Transkriptionsdauer + Echtzeitfaktor.

    Echtzeitfaktor = Transkriptionsdauer / Audiodauer (<1 = schneller als
    Echtzeit). Ohne verlässliche Audiodauer (ffprobe fehlgeschlagen) wird nur
    die Dauer ausgegeben. Zahlformat wie im Rest des Tools mit Punkt.
    """
    if audio_duration and audio_duration > 0:
        rtf = transcribe_seconds / audio_duration
        return f"{transcribe_seconds:.0f} s, {rtf:.2f}x Echtzeit"
    return f"{transcribe_seconds:.0f} s"


def extract_recording_number(filename: str) -> int | None:
    """
    Extrahiert die Aufnahme-Nummer aus dem Dateinamen (erste Zahlengruppe,
    konsistent zur Sortierlogik in join_audio.py).
    """
    match = re.search(r"(\d+)", filename)
    return int(match.group(1)) if match else None


def build_metadata_header(audio_path: Path, model_label: str,
                          corrected: bool) -> str:
    """
    Baut den Metadaten-Kopf für Einzeltranskripte (BUG-TRANSCRIBE-001).

    'Kontext' bleibt bewusst leer: die inhaltliche Einordnung kann nur der
    Nutzer (oder eine spätere LLM-Analyse) liefern.
    'Datum' ist das Dateidatum (mtime) — beim Diktiergerät i. d. R. das
    Aufnahmedatum.
    """
    number = extract_recording_number(audio_path.stem)
    title = (f"# Aufnahme #{number}" if number is not None
             else f"# Aufnahme: {audio_path.stem}")
    datum = datetime.fromtimestamp(
        audio_path.stat().st_mtime).strftime("%Y-%m-%d")
    status = "Rohtranskript (LLM-korrigiert)" if corrected else "Rohtranskript"
    return (
        f"{title}\n"
        f"- Datei: {audio_path.name}\n"
        f"- Datum: {datum}\n"
        f"- Modell: {model_label}\n"
        f"- Quelle: Einzelaufnahme\n"
        f"- Kontext:\n"
        f"- Status: {status}\n"
    )


def write_merged_transcript(folder_name: str, transcript_paths: list[Path],
                            output_dir: Path, suffix: str = ".md") -> Path | None:
    """
    Stellt die Einzeltranskripte eines Quellordners auf TEXTebene zu einer
    Sammeldatei <Ordnername><suffix> zusammen (BUG-TRANSCRIBE-001: Zusammen-
    führung erst NACH der Transkription; Einzeltranskripte bleiben erhalten,
    Metadaten-Köpfe werden mit übernommen).
    """
    paths = [p for p in transcript_paths if p.exists()]
    if not paths:
        return None

    # Reihenfolge: Aufnahme-Nummer, sonst Dateiname.
    paths.sort(key=lambda p: (extract_recording_number(p.stem) is None,
                              extract_recording_number(p.stem) or 0, p.name))

    header = (
        f"# Sammeltranskript: {folder_name}\n"
        f"- Erstellt: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"- Aufnahmen: {len(paths)}\n"
        f"- Quelle: Zusammenstellung auf Textebene "
        f"(Einzeltranskripte bleiben erhalten)\n"
    )
    parts = [p.read_text(encoding="utf-8").strip() for p in paths]
    content = header + "\n---\n\n" + "\n\n---\n\n".join(parts) + "\n"

    out_path = output_dir / f"{folder_name}{suffix}"
    out_path.write_text(content, encoding="utf-8")
    print(f"   [OK] Sammeltranskript: {out_path} ({len(paths)} Aufnahmen)")
    return out_path


def top_level_subfolder(audio: Path, root: Path) -> Path | None:
    """
    Oberster Unterordner von root, in dem die Datei liegt
    (None für Dateien direkt in root oder außerhalb von root).
    """
    try:
        rel = audio.parent.relative_to(root)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return root / rel.parts[0]


def transcript_exists(audio_path: Path, output_dir: Path, suffix: str) -> bool:
    """Prüft, ob bereits ein Transkript für diese Audio-Datei existiert."""
    transcript_path = output_dir / (audio_path.stem + suffix)
    return transcript_path.exists()


def get_audio_duration(path: Path) -> float:
    """Gibt die Audiodauer in Sekunden zurück (via ffprobe).

    Gibt 0.0 zurück, wenn die Dauer nicht ermittelbar ist (ffprobe fehlt oder
    scheitert, unlesbare/defekte Datei). Wirft NIE — Aufrufer behandeln 0.0 als
    "unbekannt": split_audio_file verarbeitet die Datei dann am Stück, und der
    Echtzeitfaktor entfällt. So kippt eine fehlgeschlagene Dauerermittlung
    (auch die Benchmark-Annehmlichkeit) niemals einen laufenden Lauf.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True
        )
        return float(result.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


def split_audio_file(audio_path: Path, max_size_mb: float | None = MAX_FILE_SIZE_MB,
                     max_duration_seconds: float | None = None) -> list[Path]:
    """
    Teilt eine zu große/zu lange Audio-Datei in kleinere Teile.

    Die Teilezahl ergibt sich aus der *bindenden* Grenze (Größe ODER Dauer):
    die gpt-4o-Transcribe-Modelle haben eine Dauergrenze (~1400 s), die bei den
    üblichen Bitraten lange vor dem 25-MB-Größenlimit greift. Gibt eine Liste
    der Teil-Dateien zurück (bzw. [audio_path], wenn kein Split nötig ist).
    """
    file_size_mb = get_file_size_mb(audio_path)
    total_duration = get_audio_duration(audio_path)

    # Ohne verlässliche Dauer (ffprobe fehlgeschlagen) kann zeitbasiert nicht
    # geteilt werden -> Datei am Stück lassen statt 0-s-Teile zu erzeugen.
    if total_duration <= 0:
        print("   [!] Audiodauer unbekannt (ffprobe) — kann nicht splitten, "
              "verarbeite Datei am Stueck.")
        return [audio_path]

    parts_by_size = ceil(file_size_mb / max_size_mb) if max_size_mb else 1
    parts_by_dur = ceil(total_duration / max_duration_seconds) if max_duration_seconds else 1
    num_parts = max(parts_by_size, parts_by_dur, 1)

    if num_parts <= 1:
        return [audio_path]

    segment_duration = total_duration / num_parts
    grund = "Dauer" if parts_by_dur >= parts_by_size else "Groesse"
    print(f"   [SPLIT] Teile Datei in {num_parts} Teile "
          f"(bindend: {grund}; {file_size_mb:.1f} MB, {total_duration:.0f} s)...")

    # Erstelle temporäres Verzeichnis für die Teile
    temp_dir = Path(tempfile.mkdtemp(prefix="transcribe_split_"))
    parts = []

    for i in range(num_parts):
        start_time = i * segment_duration
        part_path = temp_dir / f"{audio_path.stem}_{i+1:02d}{audio_path.suffix}"

        # Re-Encode (kein -c copy): vermeidet falsche Dauer-Metadata durch
        # mitkopierte VBR-Header (Xing/Info); jede Teildatei meldet ihre echte
        # Dauer. ffmpeg waehlt den Encoder anhand der Dateiendung.
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-t", str(segment_duration),
            "-i", str(audio_path),
            "-map", "0:a",
            "-reset_timestamps", "1",
            str(part_path)
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        part_dur = get_audio_duration(part_path) if part_path.exists() else 0.0
        # Fehlgeschlagenen/leeren Teil nicht weiterreichen — sonst landet
        # Muell im Transkript. Aufraeumen und klar melden.
        if proc.returncode != 0 or part_dur <= 0:
            for p in parts:
                p.unlink(missing_ok=True)
            part_path.unlink(missing_ok=True)
            temp_dir.rmdir()
            raise RuntimeError(
                f"ffmpeg-Split fehlgeschlagen fuer Teil {i+1}/{num_parts} von "
                f"{audio_path.name}: {(proc.stderr or '').strip()[-200:]}"
            )
        parts.append(part_path)
        print(f"      Teil {i+1}/{num_parts}: {part_path.name} ({part_dur:.0f} s)")

    return parts


def transcribe_file(audio_path: Path, provider: TranscriptionProvider,
                    language: str, prompt: str = None,
                    hotwords: str = None) -> str:
    """
    Vollständige Transkription (ohne Zeitmarken) als reinen Text zurückgeben.
    """
    print(f"-> Transkribiere: {audio_path.name} ...")
    return provider.transcribe(
        audio_path, language=language, prompt=prompt, hotwords=hotwords
    )


def write_transcript(
    audio_path: Path,
    text: str,
    output_dir: Path,
    suffix: str = ".md",
    markdown_title: bool = True,
    header: str | None = None,
    timing: str | None = None,
) -> Path:
    """
    Speichert die Transkription in output_dir als Datei mit gleichem Basenamen.
    Mit header wird der Metadaten-Kopf vorangestellt; sonst bei Markdown
    ein einfacher Titel. timing (optional) wird nur an die Erfolgsmeldung
    angehängt (Benchmark-Info, keine Auswirkung auf die Datei).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / (audio_path.stem + suffix)

    if header:
        content = f"{header}\n{text.strip()}\n"
    elif markdown_title and suffix.lower() == ".md":
        content = f"# Transkript: {audio_path.name}\n\n{text.strip()}\n"
    else:
        content = text

    out_path.write_text(content, encoding="utf-8")
    note = f" ({timing})" if timing else ""
    print(f"   [OK] gespeichert als: {out_path}{note}")
    return out_path


def collect_audio_files(input_path: Path) -> list[Path]:
    """
    Sammelt alle Audio-Dateien in einem Ordner (rekursiv) oder gibt
    bei einzelner Datei einfach diese zurück.
    Der 'processed/' Unterordner wird ignoriert.
    """
    if input_path.is_file():
        if not is_audio_file(input_path):
            raise ValueError(f"Datei ist kein unterstütztes Audioformat: {input_path}")
        return [input_path]

    if input_path.is_dir():
        files: list[Path] = []
        for p in sorted(input_path.rglob("*")):
            # Ignoriere den processed/ Ordner
            if "processed" in p.parts:
                continue
            if is_audio_file(p):
                files.append(p)
        if not files:
            raise ValueError(f"Im Ordner {input_path} wurden keine Audio-Dateien gefunden.")
        return files

    raise FileNotFoundError(f"Pfad nicht gefunden: {input_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Einfache Voll-Transkription von Diktiergerät-Audiodateien (ohne Zeitmarken)."
    )
    parser.add_argument(
        "input",
        help="Audiodatei oder Ordner mit Audiodateien (z. B. 'input')",
    )
    parser.add_argument(
        "--provider",
        default="openai",
        choices=["openai", "local"],
        help=(
            "Transkriptions-Engine. Standard: openai (Cloud, kostenpflichtig). "
            "local = faster-whisper (lokal, kostenlos, ohne API-Key/Netz)."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Modell/Modellgröße. Bedeutung haengt vom Provider ab. "
            "Ohne Angabe waehlt jeder Provider seinen Default: "
            "openai -> gpt-4o-mini-transcribe ($0.003/Min; Alternativen: "
            "gpt-4o-transcribe, whisper-1); "
            "local -> small (Alternativen: tiny, base, medium, large-v3)."
        ),
    )
    parser.add_argument(
        "--language",
        default="de",
        help="Sprachcode der Aufnahme (Standard: de).",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Ausgabeordner für Transkripte (Standard: output).",
    )
    parser.add_argument(
        "--suffix",
        default=".md",
        help="Dateiendung für Transkriptdateien (Standard: .md).",
    )
    parser.add_argument(
        "--no-markdown-title",
        action="store_true",
        help="Keinen Markdown-Titel in die Ausgabedatei schreiben.",
    )
    parser.add_argument(
        "--metadata-header",
        action="store_true",
        help=(
            "Metadaten-Kopf je Transkript schreiben (Aufnahme-Nr., Datum, "
            "Modell, Quelle, Status). Für den Einzeldatei-Workflow gedacht."
        ),
    )
    parser.add_argument(
        "--merge-transcripts",
        action="store_true",
        help=(
            "Je Quell-Unterordner zusätzlich ein Sammeltranskript "
            "(<Ordnername>.md) auf Textebene erstellen — nach der "
            "Einzeltranskription, Einzeltranskripte bleiben erhalten."
        ),
    )
    parser.add_argument(
        "--move-processed",
        action="store_true",
        help=(
            "Quell-Unterordner nach fehlerfreiem Lauf nach 'processed/' "
            "verschieben (nur bei Ordner-Input; Dateien direkt im "
            "Input-Ordner bleiben liegen)."
        ),
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Kontext-Prompt für bessere Erkennung (z.B. Fachbegriffe, Namen).",
    )
    parser.add_argument(
        "--hotwords",
        default=None,
        help=(
            "Kommagetrennte Hotwords/Hinweisbegriffe für faster-whisper "
            "(nur Provider local)."
        ),
    )
    parser.add_argument(
        "--no-replacements",
        action="store_true",
        help="Standard-Ersetzungen deaktivieren.",
    )
    parser.add_argument(
        "--correct",
        action="store_true",
        help=(
            "Optionale LLM-Nachkorrektur ueber ein lokales, OpenAI-kompatibles "
            "Backend (Ollama/LM Studio) aktivieren. Standard: aus."
        ),
    )
    parser.add_argument(
        "--correct-backend",
        choices=["ollama", "lmstudio"],
        default=None,
        help=(
            "Backend fuer die LLM-Korrektur (setzt die Default-URL). "
            "Standard: ollama bzw. CORRECTION_BACKEND aus .env."
        ),
    )
    parser.add_argument(
        "--correct-model",
        default=None,
        help=(
            "Modellname fuer die LLM-Korrektur (Pflicht bei --correct; "
            "kein universeller Default). Alternativ CORRECTION_MODEL in .env."
        ),
    )
    parser.add_argument(
        "--correct-base-url",
        default=None,
        help=(
            "Ueberschreibt die Backend-Default-URL (abweichende Ports/Hosts). "
            "Alternativ CORRECTION_BASE_URL in .env."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bereits transkribierte Dateien erneut verarbeiten.",
    )

    args = parser.parse_args()

    # .env laden (für CORRECTION_*; OpenAIProvider lädt zusätzlich selbst).
    load_dotenv()

    # Benchmark-Instrumentierung: Gesamtlaufzeit ab hier messen.
    run_start = time.perf_counter()

    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    # Transkriptions-Engine wählen. Fehler hier (fehlender API-Key bei openai,
    # nicht installiertes faster-whisper bei local) klar melden statt Traceback.
    # Modell-Ladezeit separat messen, damit sie den Benchmark-Vergleich nicht
    # verfälscht (einmaliger Fixaufwand, unabhängig von der Audiomenge).
    try:
        t_load = time.perf_counter()
        provider = get_provider(args.provider, model=args.model)
        load_seconds = time.perf_counter() - t_load
    except (RuntimeError, ValueError) as e:
        print(f"[X] {e}")
        raise SystemExit(1)

    # Einmalig protokollieren, welches Device/compute_type tatsächlich genutzt
    # wird (steht auf "auto" -> hier steht das real Gewählte). Nur wenn der
    # Provider es zuverlässig auslesen kann (sonst nichts).
    describe = getattr(provider, "runtime_description", None)
    runtime_note = describe() if callable(describe) else None
    if runtime_note:
        print(f"[i] Engine-Laufzeit: {runtime_note}")

    # Optionale LLM-Korrektur vorbereiten (Default: aus). CLI hat Vorrang vor .env.
    corrector = None
    if args.correct:
        backend = args.correct_backend or os.getenv("CORRECTION_BACKEND") or "ollama"
        model = args.correct_model or os.getenv("CORRECTION_MODEL")
        base_url = args.correct_base_url or os.getenv("CORRECTION_BASE_URL")
        try:
            corrector = build_corrector(backend, model, base_url)
        except (ValueError, RuntimeError) as e:
            print(f"[X] {e}")
            raise SystemExit(1)
        print(
            f"LLM-Korrektur aktiv: backend={backend}, model={model}, "
            f"url={base_url or BACKEND_DEFAULTS.get(backend)}"
        )

    audio_files = collect_audio_files(input_path)
    print(f"Gefundene Audio-Dateien: {len(audio_files)}")

    # Ersetzungen vorbereiten
    replacements = {} if args.no_replacements else DEFAULT_REPLACEMENTS

    # Statistik
    skipped = 0
    processed = 0
    errors = 0

    # Für --move-processed: je oberstem Quell-Unterordner merken, ob alle
    # seine Dateien fehlerfrei durchliefen (Skip zählt als fehlerfrei).
    folder_ok: dict[Path, bool] = {}
    # Für --merge-transcripts: Transkriptpfade je Quell-Unterordner sammeln
    # (auch übersprungene — deren Transkripte existieren bereits).
    transcripts_by_folder: dict[Path, list[Path]] = {}
    input_is_dir = input_path.is_dir()

    for audio in audio_files:
        src_folder = top_level_subfolder(audio, input_path) if input_is_dir else None
        try:
            # Leere (0-Byte-)Datei ZUERST prüfen — noch vor dem Skip bei
            # vorhandenem Transkript. Sonst würde eine 0-Byte-Datei (z. B.
            # abgebrochene Kopie) hinter einem veralteten Transkript als
            # "übersprungen" gewertet und könnte per --move-processed unbemerkt
            # nach processed/ wandern. Klarere Meldung statt kryptischem
            # ffmpeg-Fehler ("Invalid data found when processing input");
            # bleibt ein Fehler (kein Skip, kein Sammeltranskript, kein
            # Verschieben).
            if audio.stat().st_size == 0:
                raise ValueError(
                    "Datei ist leer (0 Bytes) — vermutlich unvollständige "
                    "Kopie, bitte erneut kopieren")

            # Prüfen ob bereits transkribiert
            if not args.force and transcript_exists(audio, output_dir, args.suffix):
                print(f"[SKIP] Ueberspringe (bereits vorhanden): {audio.name}")
                skipped += 1
                if src_folder:
                    folder_ok.setdefault(src_folder, True)
                    transcripts_by_folder.setdefault(src_folder, []).append(
                        output_dir / (audio.stem + args.suffix))
                continue

            # Prüfen ob Datei zu groß ODER zu lang ist und ggf. splitten.
            # Splitting nur, wenn der Provider überhaupt eine Grenze hat
            # (lokale Engines haben keine -> kein Splitting nötig).
            size_limit = provider.max_file_size_mb
            dur_limit = provider.max_duration_seconds
            file_size_mb = get_file_size_mb(audio)
            # Audiodauer einmal ermitteln: fuer den Dauer-Split UND fuer den
            # Echtzeitfaktor (Benchmark).
            audio_duration = get_audio_duration(audio)
            over_size = bool(size_limit) and file_size_mb > size_limit
            over_dur = bool(dur_limit) and audio_duration > dur_limit
            # Reine Transkriptionsdauer messen (Benchmark): der Timer laeuft in
            # BEIDEN Zweigen nur um die transcribe_file-Aufrufe — ffmpeg-Split
            # und Temp-Aufraeumen gehoeren nicht in die Transkriptionszeit,
            # sonst waeren gesplittete Dateien nicht mit normalen vergleichbar.
            if over_size or over_dur:
                # Datei aufteilen und alle Teile transkribieren
                parts = split_audio_file(
                    audio, max_size_mb=size_limit, max_duration_seconds=dur_limit)
                all_texts = []

                t_transcribe = time.perf_counter()
                for part in parts:
                    text = transcribe_file(
                        part,
                        provider=provider,
                        language=args.language,
                        prompt=args.prompt,
                        hotwords=args.hotwords,
                    )
                    all_texts.append(text)
                transcribe_seconds = time.perf_counter() - t_transcribe

                # Alle Teile zusammenfügen
                text = "\n\n".join(all_texts)

                # Temporäre Dateien aufräumen
                for part in parts:
                    if part != audio:  # Originaldatei nicht löschen
                        part.unlink(missing_ok=True)
                # Temporäres Verzeichnis aufräumen
                if parts and parts[0].parent.name.startswith("transcribe_split_"):
                    parts[0].parent.rmdir()
            else:
                # Normale Transkription
                t_transcribe = time.perf_counter()
                text = transcribe_file(
                    audio,
                    provider=provider,
                    language=args.language,
                    prompt=args.prompt,
                    hotwords=args.hotwords,
                )
                transcribe_seconds = time.perf_counter() - t_transcribe

            # Post-Processing: Ersetzungen anwenden
            text = apply_replacements(text, replacements)

            # Optionale LLM-Nachkorrektur (defensiv: Fehler -> Original behalten).
            if corrector is not None:
                text = correct_text(corrector, text)

            # Optionaler Metadaten-Kopf (Einzeldatei-Workflow).
            header = None
            if args.metadata_header:
                model_label = f"{getattr(provider, 'model', None) or getattr(provider, 'model_size', '?')} ({provider.name})"
                header = build_metadata_header(
                    audio, model_label, corrected=corrector is not None)

            # Speichern (mit Benchmark-Notiz: Dauer + Echtzeitfaktor)
            out_path = write_transcript(
                audio_path=audio,
                text=text,
                output_dir=output_dir,
                suffix=args.suffix,
                markdown_title=not args.no_markdown_title,
                header=header,
                timing=format_run_timing(transcribe_seconds, audio_duration),
            )
            processed += 1
            if src_folder:
                folder_ok.setdefault(src_folder, True)
                transcripts_by_folder.setdefault(src_folder, []).append(out_path)

        except Exception as e:
            print(f"   [X] Fehler bei {audio.name}: {e}")
            errors += 1
            if src_folder:
                folder_ok[src_folder] = False

    # Sammeltranskript je Quellordner (Textebene) — nur bei fehlerfreiem
    # Ordner, sonst entstünde eine unvollständige Zusammenstellung.
    if args.merge_transcripts and input_is_dir:
        for folder in sorted(transcripts_by_folder):
            if not folder_ok.get(folder, False):
                print(f"   [!] Kein Sammeltranskript fuer {folder.name} "
                      f"(Fehler im Lauf).")
                continue
            try:
                write_merged_transcript(
                    folder.name, transcripts_by_folder[folder],
                    output_dir, args.suffix)
            except OSError as e:
                print(f"   [!] Sammeltranskript fuer {folder.name} "
                      f"fehlgeschlagen: {e}")

    # Quell-Unterordner nach fehlerfreiem Lauf aufräumen (Einzeldatei-Workflow,
    # BUG-TRANSCRIBE-001). Bewusst defensiv: bei Fehlern bleibt der Ordner liegen.
    if args.move_processed and input_is_dir:
        from join_audio import move_to_processed
        for folder in sorted(folder_ok):
            if folder_ok[folder] and folder.exists():
                try:
                    move_to_processed(folder)
                except OSError as e:
                    print(f"   [!] Konnte {folder.name} nicht verschieben: {e}")

    # Zusammenfassung
    total_seconds = time.perf_counter() - run_start
    print(f"\n{'=' * 40}")
    print(f"Fertig: {processed} transkribiert, {skipped} übersprungen, {errors} Fehler")
    # Modell-Ladezeit separat ausweisen, damit sie den Maschinenvergleich nicht
    # verfälscht ("ohne Laden" ist die vergleichsrelevante Zahl).
    print(f"Laufzeit: {total_seconds:.0f} s gesamt "
          f"(Modell laden: {load_seconds:.0f} s, "
          f"ohne Laden: {total_seconds - load_seconds:.0f} s).")


if __name__ == "__main__":
    main()
