"""
QA-Werkzeug: erkennt Whisper-Decoder-Loops in Transkripten (WORT-basiert).

Hintergrund: Whisper neigt zu Wiederholungsschleifen — dieselbe Wortfolge
wird zig- bis hundertfach hintereinander ausgegeben (z. B. 95x
"Feuerwerkzeug, am 5. November 2013 a. D."). Eine SATZ-basierte Pruefung
(identische aufeinanderfolgende Saetze) hat dabei einen blinden Fleck:
In-Satz-Wort-Loops wie "heute heute heute" oder eine Phrase, die die
Satzsegmentierung sprengt, werden uebersehen. Deshalb arbeitet dieses
Werkzeug bewusst auf WORTEBENE.

Befund der Korpus-Untersuchung (siehe docs/LOOP_INVESTIGATION_2026-07.md):
Loops sind additiver Muell, kein Deckel ueber verlorenem Inhalt — der Text
rundherum bleibt vollstaendig. Ein erneuter Transkriptionslauf loest sie
meist auf (stochastisch). Dieses Werkzeug MISST nur; es repariert nichts.

Aufruf:
    python eval/loop_check.py "output/Aufnahme #402.md"     # eine Datei
    python eval/loop_check.py --scan output                 # ganzer Ordner
    python eval/loop_check.py --scan output --min-count 3 --min-words 3
"""

import argparse
import re
from pathlib import Path

# Standard-Schwellen fuer einen "echten" Loop (statt normaler Floskeln wie
# "ja ja ja" oder einer einmaligen Selbstkorrektur):
DEFAULT_MIN_WORDS = 3    # Phrasenlaenge in Woertern (schliesst 1-2-Wort-Floskeln aus)
DEFAULT_MIN_COUNT = 3    # Anzahl aufeinanderfolgender Vorkommen
DEFAULT_MAX_WORDS = 12   # obere Grenze der betrachteten Phrasenlaenge

# Metadaten-Kopf / Markdown-Titel / Sammel-Trenner aus dem Body entfernen.
_HEADER_LINE = re.compile(r"^\s*(#.*|-\s*\w+:.*|---+)\s*$")


def strip_header(md_text: str) -> str:
    """Entfernt Metadaten-Kopf und Markdown-Titel, gibt den reinen Body."""
    lines = md_text.splitlines()
    return " ".join(ln for ln in lines if not _HEADER_LINE.match(ln))


def find_repeats(text: str, max_len: int = DEFAULT_MAX_WORDS) -> list[dict]:
    """Alle unmittelbaren Wiederholungsbloecke im Text.

    Greedy: an jeder Position die laengste Phrase L (<=max_len Woerter)
    suchen, die sich direkt danach wiederholt; hinter den Block springen.
    Anders als phrase_repetition() (nur der auffaelligste Block) liefert dies
    JEDEN Block, damit ein langer Loop nicht von einer kurzen Floskel
    ueberdeckt wird.

    Rueckgabe: Liste von dict(phrase, unit_words, count), count = Anzahl
    aufeinanderfolgender Vorkommen (>=2).
    """
    toks = text.split()
    n = len(toks)
    out: list[dict] = []
    i = 0
    while i < n:
        found = False
        for L in range(min(max_len, (n - i) // 2), 0, -1):
            unit = toks[i:i + L]
            reps = 0
            j = i + L
            while j + L <= n and toks[j:j + L] == unit:
                reps += 1
                j += L
            if reps >= 1:
                out.append(dict(phrase=" ".join(unit), unit_words=L,
                                count=reps + 1))
                i = j
                found = True
                break
        if not found:
            i += 1
    return out


def phrase_repetition(text: str, max_len: int = DEFAULT_MAX_WORDS) -> dict:
    """Zusammenfassung der Wort-Loop-Redundanz eines Textes.

    Rueckgabe: dict(redundant_words, total_words, ratio, top_phrase,
    top_count). redundant_words = Summe aller Wiederholungskopien (ohne das
    jeweils erste Vorkommen). top_* = der Block mit den meisten Vorkommen.
    """
    toks = text.split()
    total = len(toks)
    reps = find_repeats(text, max_len)
    redundant = sum((r["count"] - 1) * r["unit_words"] for r in reps)
    top = max(reps, key=lambda r: r["count"], default=None)
    return dict(
        redundant_words=redundant,
        total_words=total,
        ratio=(redundant / total) if total else 0.0,
        top_phrase=top["phrase"] if top else "",
        top_count=top["count"] if top else 0,
    )


def loops_in_file(md: Path, min_words: int, min_count: int,
                  max_words: int = DEFAULT_MAX_WORDS) -> list[dict]:
    """Echte Loop-Bloecke einer Transkriptdatei (ueber den Schwellen)."""
    body = strip_header(md.read_text(encoding="utf-8", errors="replace"))
    reps = [r for r in find_repeats(body, max_len=max_words)
            if r["unit_words"] >= min_words and r["count"] >= min_count]
    reps.sort(key=lambda r: (-r["count"], -r["unit_words"]))
    return reps


def _report_file(md: Path, min_words: int, min_count: int,
                 max_words: int = DEFAULT_MAX_WORDS) -> int:
    reps = loops_in_file(md, min_words, min_count, max_words)
    if reps:
        print(f"* {md.name}")
        for r in reps:
            print(f"    {r['count']}x ({r['unit_words']} W.): "
                  f"{r['phrase'][:70]!r}")
    return len(reps)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Erkennt Whisper-Wort-Loops in Transkripten (misst, "
                    "repariert nicht).")
    ap.add_argument("target", help="Transkript (.md/.txt) ODER mit --scan ein Ordner.")
    ap.add_argument("--scan", action="store_true",
                    help="target als Ordner behandeln und alle *.md scannen.")
    ap.add_argument("--min-words", type=int, default=DEFAULT_MIN_WORDS,
                    help=f"Mindest-Phrasenlaenge in Woertern (Default {DEFAULT_MIN_WORDS}).")
    ap.add_argument("--min-count", type=int, default=DEFAULT_MIN_COUNT,
                    help=f"Mindest-Wiederholungen (Default {DEFAULT_MIN_COUNT}).")
    ap.add_argument("--max-words", type=int, default=DEFAULT_MAX_WORDS,
                    help=f"Obere Grenze der Phrasenlaenge (Default {DEFAULT_MAX_WORDS}). "
                         "Phrasen laenger als dieser Wert werden nicht erkannt.")
    args = ap.parse_args()

    # Ohne diese Pruefung liefert z. B. --min-words 13 (bei --max-words 12)
    # stillschweigend NIE einen Treffer.
    if not (1 <= args.min_words <= args.max_words):
        ap.error(f"--min-words ({args.min_words}) muss zwischen 1 und "
                 f"--max-words ({args.max_words}) liegen.")
    if args.min_count < 2:
        ap.error(f"--min-count ({args.min_count}) muss >= 2 sein.")

    target = Path(args.target).expanduser()
    if args.scan:
        if not target.is_dir():
            ap.error(f"--scan erwartet einen existierenden Ordner: {target}")
        files = sorted(target.glob("*.md"))
        flagged = [(md, loops_in_file(md, args.min_words, args.min_count,
                                      args.max_words))
                   for md in files]
        flagged = [(md, r) for md, r in flagged if r]
        flagged.sort(key=lambda t: -max(r["count"] for r in t[1]))
        print(f"Gescannt: {len(files)} Dateien in {target} "
              f"(Schwelle: {args.min_words}-{args.max_words} Woerter, "
              f">={args.min_count}x)")
        print(f"Mit Loop-Befund: {len(flagged)}\n")
        for md, reps in flagged:
            print(f"* {md.name}")
            for r in reps[:4]:
                print(f"    {r['count']}x ({r['unit_words']} W.): "
                      f"{r['phrase'][:70]!r}")
    else:
        if not target.is_file():
            print(f"[X] Datei nicht gefunden: {target}")
            raise SystemExit(1)
        n = _report_file(target, args.min_words, args.min_count, args.max_words)
        if not n:
            print(f"{target.name}: kein Loop ueber den Schwellen "
                  f"({args.min_words}-{args.max_words} Woerter, "
                  f">={args.min_count}x).")


if __name__ == "__main__":
    main()
