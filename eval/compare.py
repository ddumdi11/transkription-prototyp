"""
Vergleichs-Helfer für die ASR-Qualitätsmessung (docs/ASR_QUALITY_SPIKE.md).

Schickt EINEN Clip durch mehrere Transkriptions-Kandidaten und legt die
Ergebnisse (+ optional eine Referenzspalte) nebeneinander in eine Markdown-
Tabelle. Zählt NICHTS automatisch — die bedeutungskritischen Fehler beurteilt
der Mensch. Reine Mess-Vorbereitung.

Beispiel:
    python eval/compare.py "test/input/clip.wav"
    python eval/compare.py "eval/clips/clip1.mp3" --reference eval/clips/clip1.txt \
        --prompt "Fachbegriffe: Diktiergerät, Claude" --models gpt-4o,local-large-v3
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

# Projektwurzel (eval/ liegt direkt darunter) und transcribe.py.
ROOT = Path(__file__).resolve().parent.parent
TRANSCRIBE = ROOT / "transcribe.py"

# Kandidaten gemäß docs/ASR_QUALITY_SPIKE.md §3. Liste nach Bedarf kürzen
# (große lokale Modelle laden beim ersten Lauf mehrere GB).
CANDIDATES = [
    {"label": "gpt-4o-mini", "provider": "openai", "model": "gpt-4o-mini-transcribe"},
    {"label": "gpt-4o", "provider": "openai", "model": "gpt-4o-transcribe"},
    {"label": "local-small", "provider": "local", "model": "small"},
    {"label": "local-medium", "provider": "local", "model": "medium"},
    {"label": "local-large-v3", "provider": "local", "model": "large-v3"},
]


def run_candidate(clip: Path, cand: dict, out_dir: Path,
                  prompt: str | None, language: str) -> dict:
    """Transkribiert den Clip mit einem Kandidaten über transcribe.py.

    Gibt {label, model, seconds, text, error} zurück. Reine Text-Ausgabe via
    --no-markdown-title + --suffix .txt, damit das Ergebnis direkt lesbar ist.
    """
    cand_out = out_dir / cand["label"]
    cand_out.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(TRANSCRIBE), str(clip),
        "--provider", cand["provider"],
        "--model", cand["model"],
        "--output-dir", str(cand_out),
        "--language", language,
        "--suffix", ".txt",
        "--no-markdown-title",
        "--force",  # immer neu, damit der Vergleich frisch ist
    ]
    if prompt:
        cmd += ["--prompt", prompt]

    start = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    seconds = time.perf_counter() - start

    out_file = cand_out / (clip.stem + ".txt")
    text, error = "", None
    if out_file.exists():
        text = out_file.read_text(encoding="utf-8").strip()
    if not text:
        # Kein Transkript -> Fehlerursache aus der Ausgabe ziehen.
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        error = tail[-1] if tail else f"kein Output (Exit {proc.returncode})"

    return {
        "label": cand["label"],
        "model": cand["model"],
        "seconds": seconds,
        "text": text,
        "error": error,
    }


def cell(text: str) -> str:
    """Macht Text tabellentauglich (keine rohen Zeilenumbrüche/Pipes)."""
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("|", r"\|")


def build_table(clip: Path, results: list[dict], reference: str | None) -> str:
    lines = [
        f"# ASR-Vergleich: {clip.name}",
        "",
        "Bedeutungskritische Fehler (Sinnverkehrung, falsche Eigennamen) bitte "
        "von Hand zählen — siehe docs/ASR_QUALITY_SPIKE.md §4.",
        "",
        "| Kandidat | Modell | Zeit (s) | Transkript |",
        "|---|---|---|---|",
    ]
    if reference:
        lines.append(f"| **Referenz** | (Ground Truth) | — | {cell(reference)} |")
    for r in results:
        body = cell(r["text"]) if r["text"] else f"_FEHLER: {cell(r['error'] or '')}_"
        lines.append(f"| {r['label']} | {r['model']} | {r['seconds']:.0f} | {body} |")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Stellt die Transkripte mehrerer ASR-Kandidaten für einen Clip gegenüber."
    )
    parser.add_argument("clip", help="Audio-Clip (eine Datei).")
    parser.add_argument("--reference", default=None,
                        help="Textdatei mit dem handgeschriebenen Referenz-Transkript (optional).")
    parser.add_argument("--prompt", default=None, help="Kontext-Prompt (an alle Kandidaten gleich).")
    parser.add_argument("--language", default="de", help="Sprachcode (Standard: de).")
    parser.add_argument("--output-dir", default=str(ROOT / "eval" / "out"),
                        help="Basis-Ausgabeordner (je Kandidat ein Unterordner).")
    parser.add_argument("--models", default=None,
                        help="Komma-Liste von Kandidaten-Labels (Default: alle). "
                             f"Verfügbar: {', '.join(c['label'] for c in CANDIDATES)}.")
    args = parser.parse_args()

    clip = Path(args.clip).expanduser().resolve()
    if not clip.is_file():
        print(f"[X] Clip nicht gefunden: {clip}")
        raise SystemExit(1)

    candidates = CANDIDATES
    if args.models:
        wanted = {m.strip() for m in args.models.split(",") if m.strip()}
        candidates = [c for c in CANDIDATES if c["label"] in wanted]
        unknown = wanted - {c["label"] for c in CANDIDATES}
        if unknown:
            print(f"[!] Unbekannte Labels ignoriert: {', '.join(sorted(unknown))}")
        if not candidates:
            print("[X] Keine gültigen Kandidaten gewählt.")
            raise SystemExit(1)

    reference = None
    if args.reference:
        ref_path = Path(args.reference).expanduser().resolve()
        if ref_path.is_file():
            reference = ref_path.read_text(encoding="utf-8").strip()
        else:
            print(f"[!] Referenzdatei nicht gefunden, fahre ohne fort: {ref_path}")

    out_dir = Path(args.output_dir).expanduser().resolve()
    results = []
    for cand in candidates:
        print(f"-> {cand['label']} ({cand['provider']}/{cand['model']}) ...")
        r = run_candidate(clip, cand, out_dir, args.prompt, args.language)
        status = "OK" if r["text"] else f"FEHLER ({r['error']})"
        print(f"   {status}, {r['seconds']:.0f}s")
        results.append(r)

    table = build_table(clip, results, reference)
    table_path = out_dir / f"vergleich_{clip.stem}.md"
    table_path.write_text(table, encoding="utf-8")
    print(f"\n[OK] Gegenüberstellung gespeichert: {table_path}")


if __name__ == "__main__":
    main()
