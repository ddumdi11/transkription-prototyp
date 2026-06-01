import os
import argparse
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# .env laden (für OPENAI_API_KEY)
load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY ist nicht gesetzt. "
        "Bitte in der .env-Datei oder als Umgebungsvariable hinterlegen."
    )

client = OpenAI(api_key=API_KEY)

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm"}

# Whisper API Limit: 25 MB
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


def is_audio_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTS


def get_file_size_mb(path: Path) -> float:
    """Gibt die Dateigröße in MB zurück."""
    return path.stat().st_size / (1024 * 1024)


def transcript_exists(audio_path: Path, output_dir: Path, suffix: str) -> bool:
    """Prüft, ob bereits ein Transkript für diese Audio-Datei existiert."""
    transcript_path = output_dir / (audio_path.stem + suffix)
    return transcript_path.exists()


def split_audio_file(audio_path: Path, max_size_mb: float = MAX_FILE_SIZE_MB) -> list[Path]:
    """
    Teilt eine zu große Audio-Datei in kleinere Teile.
    Gibt eine Liste der Teil-Dateien zurück.
    """
    file_size_mb = get_file_size_mb(audio_path)

    if file_size_mb <= max_size_mb:
        return [audio_path]

    # Berechne wie viele Teile wir brauchen
    num_parts = int(file_size_mb / max_size_mb) + 1

    # Hole die Dauer der Audio-Datei
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True
    )
    total_duration = float(result.stdout.strip())
    segment_duration = total_duration / num_parts

    print(f"   [SPLIT] Datei zu gross ({file_size_mb:.1f} MB), teile in {num_parts} Teile...")

    # Erstelle temporäres Verzeichnis für die Teile
    temp_dir = Path(tempfile.mkdtemp(prefix="transcribe_split_"))
    parts = []

    for i in range(num_parts):
        start_time = i * segment_duration
        part_path = temp_dir / f"{audio_path.stem}_{i+1:02d}{audio_path.suffix}"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(audio_path),
            "-ss", str(start_time),
            "-t", str(segment_duration),
            "-c", "copy",
            str(part_path)
        ]

        subprocess.run(cmd, capture_output=True, text=True)
        parts.append(part_path)
        print(f"      Teil {i+1}/{num_parts}: {part_path.name}")

    return parts


def transcribe_file(audio_path: Path, model: str, language: str, prompt: str = None) -> str:
    """
    Vollständige Transkription (ohne Zeitmarken) als reinen Text zurückgeben.
    """
    print(f"-> Transkribiere: {audio_path.name} ...")

    with audio_path.open("rb") as f:
        kwargs = {
            "model": model,
            "file": f,
            "language": language,
        }
        if prompt:
            kwargs["prompt"] = prompt

        result = client.audio.transcriptions.create(**kwargs)

    return result.text


def write_transcript(
    audio_path: Path,
    text: str,
    output_dir: Path,
    suffix: str = ".md",
    markdown_title: bool = True,
) -> Path:
    """
    Speichert die Transkription in output_dir als Datei mit gleichem Basenamen.
    Bei Markdown wird ein einfacher Titel hinzugefügt.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / (audio_path.stem + suffix)

    if markdown_title and suffix.lower() == ".md":
        content = f"# Transkript: {audio_path.name}\n\n{text.strip()}\n"
    else:
        content = text

    out_path.write_text(content, encoding="utf-8")
    print(f"   [OK] gespeichert als: {out_path}")
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
        "--model",
        default="gpt-4o-mini-transcribe",
        help=(
            "Transkriptionsmodell. Standard: gpt-4o-mini-transcribe "
            "($0.003/Min, ~halber Preis von whisper-1 bei besserer Qualitaet). "
            "Alternativen: gpt-4o-transcribe ($0.006/Min), whisper-1 ($0.006/Min)."
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
        "--prompt",
        default=None,
        help="Kontext-Prompt für bessere Erkennung (z.B. Fachbegriffe, Namen).",
    )
    parser.add_argument(
        "--no-replacements",
        action="store_true",
        help="Standard-Ersetzungen deaktivieren.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bereits transkribierte Dateien erneut verarbeiten.",
    )

    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    audio_files = collect_audio_files(input_path)
    print(f"Gefundene Audio-Dateien: {len(audio_files)}")

    # Ersetzungen vorbereiten
    replacements = {} if args.no_replacements else DEFAULT_REPLACEMENTS

    # Statistik
    skipped = 0
    processed = 0
    errors = 0

    for audio in audio_files:
        try:
            # Prüfen ob bereits transkribiert
            if not args.force and transcript_exists(audio, output_dir, args.suffix):
                print(f"[SKIP] Ueberspringe (bereits vorhanden): {audio.name}")
                skipped += 1
                continue

            # Prüfen ob Datei zu groß ist und ggf. splitten
            file_size_mb = get_file_size_mb(audio)
            if file_size_mb > MAX_FILE_SIZE_MB:
                # Datei aufteilen und alle Teile transkribieren
                parts = split_audio_file(audio)
                all_texts = []

                for part in parts:
                    text = transcribe_file(
                        part,
                        model=args.model,
                        language=args.language,
                        prompt=args.prompt,
                    )
                    all_texts.append(text)

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
                text = transcribe_file(
                    audio,
                    model=args.model,
                    language=args.language,
                    prompt=args.prompt,
                )

            # Post-Processing: Ersetzungen anwenden
            text = apply_replacements(text, replacements)

            # Speichern
            write_transcript(
                audio_path=audio,
                text=text,
                output_dir=output_dir,
                suffix=args.suffix,
                markdown_title=not args.no_markdown_title,
            )
            processed += 1

        except Exception as e:
            print(f"   [X] Fehler bei {audio.name}: {e}")
            errors += 1

    # Zusammenfassung
    print(f"\n{'=' * 40}")
    print(f"Fertig: {processed} transkribiert, {skipped} übersprungen, {errors} Fehler")


if __name__ == "__main__":
    main()
