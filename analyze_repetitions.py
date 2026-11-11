#!/usr/bin/env python3
"""Read-only check for longer repeated word sequences in transcripts.

Complements ``eval/loop_check.py``: that tool finds *immediate* loops
("a b c a b c a b c"). Decoder errors seen in long recordings (#977) look
different: a sentence or half-sentence reappears a few dozen words later,
sometimes slightly re-phrased around it. Those are found here via word
n-grams that occur again at a distance.

The result is only a suspicion signal. Speakers repeat themselves on
purpose, so nothing is ever removed or changed; length, distance and number
of occurrences help a human decide.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from eval.loop_check import strip_header
from segment_metadata import SegmentMetadataError, load_segment_metadata


DEFAULT_MIN_WORDS = 8
PREVIEW_WORDS = 16
_WORD = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    """Split text into words; punctuation is ignored for matching."""
    return _WORD.findall(text)


def find_repetitions(
    words: list[str],
    *,
    min_words: int = DEFAULT_MIN_WORDS,
    max_distance: int | None = None,
) -> list[dict[str, Any]]:
    """Return repeated word sequences of at least ``min_words`` words.

    A finding is a contiguous region whose every ``min_words``-gram already
    appeared earlier without overlapping it. Matching is case-insensitive.
    ``distance_words`` is the gap between the start of the nearest earlier
    occurrence and the start of the repetition; ``occurrences`` counts how
    often the leading n-gram appears (non-overlapping) in the whole text.
    """
    if min_words < 2:
        raise ValueError("Die Mindestlänge muss mindestens 2 Wörter betragen")
    if max_distance is not None and max_distance < min_words:
        raise ValueError("Der maximale Abstand muss mindestens der Mindestlänge entsprechen")

    folded = [word.casefold() for word in words]
    positions: dict[tuple[str, ...], list[int]] = {}
    hits: list[tuple[int, int]] = []
    for index in range(len(folded) - min_words + 1):
        gram = tuple(folded[index:index + min_words])
        seen = positions.setdefault(gram, [])
        # Nearest earlier occurrence that does not overlap; overlapping
        # repeats are immediate loops and belong to eval/loop_check.py.
        earlier = next((p for p in reversed(seen) if index - p >= min_words), None)
        seen.append(index)
        if earlier is None:
            continue
        if max_distance is not None and index - earlier > max_distance:
            continue
        hits.append((earlier, index))

    regions: list[list[int]] = []  # [source_start, repeat_start, last_gram]
    for source, repeat in hits:
        if regions and repeat == regions[-1][2] + 1:
            regions[-1][2] = repeat
        else:
            regions.append([source, repeat, repeat])

    findings = []
    for source, repeat, last_gram in regions:
        length = last_gram - repeat + min_words
        leading = tuple(folded[repeat:repeat + min_words])
        findings.append({
            "first_word_index": source,
            "repeat_word_index": repeat,
            "length_words": length,
            "distance_words": repeat - source,
            "occurrences": _count_non_overlapping(positions[leading], min_words),
            "text": " ".join(words[repeat:repeat + min(length, PREVIEW_WORDS)]),
        })
    return findings


def _count_non_overlapping(starts: list[int], width: int) -> int:
    count, next_free = 0, -1
    for start in starts:
        if start >= next_free:
            count += 1
            next_free = start + width
    return count


def _words_from_segments(payload: dict[str, Any]) -> tuple[list[str], list[float]]:
    words: list[str] = []
    times: list[float] = []
    for segment in payload["segments"]:
        segment_words = tokenize(segment["raw_text"])
        words.extend(segment_words)
        times.extend([float(segment["start"])] * len(segment_words))
    return words, times


def build_repetition_report(
    path: Path,
    *,
    min_words: int = DEFAULT_MIN_WORDS,
    max_distance: int | None = None,
) -> dict[str, Any]:
    """Analyse a Markdown transcript or ``.segments.json`` without changing it."""
    times: list[float] | None = None
    if path.name.endswith(".segments.json"):
        payload = load_segment_metadata(path)
        words, times = _words_from_segments(payload)
        source = "segments_raw_text"
    else:
        words = tokenize(strip_header(path.read_text(encoding="utf-8")))
        source = "markdown"

    findings = find_repetitions(words, min_words=min_words, max_distance=max_distance)
    for item in findings:
        item["first_start_seconds"] = times[item["first_word_index"]] if times else None
        item["repeat_start_seconds"] = times[item["repeat_word_index"]] if times else None
    # Neighbouring findings may overlap by up to min_words - 1 words; count
    # each repeated word once so repeated_ratio cannot exceed the real share.
    repeated = len({
        index
        for item in findings
        for index in range(item["repeat_word_index"],
                           item["repeat_word_index"] + item["length_words"])
    })
    return {
        "file": str(path),
        "source": source,
        "settings": {"min_words": min_words, "max_distance": max_distance},
        "summary": {
            "total_words": len(words),
            "findings": len(findings),
            "repeated_words": repeated,
            "repeated_ratio": round(repeated / len(words), 4) if words else 0.0,
        },
        "findings": findings,
    }


def _position(item: dict[str, Any], key: str) -> str:
    seconds = item[f"{key}_start_seconds"]
    word = item[f"{key}_word_index"]
    if seconds is None:
        return f"Wort {word}"
    minutes, rest = divmod(seconds, 60)
    return f"Wort {word} ({int(minutes)}:{rest:04.1f})"


def _human_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    settings = report["settings"]
    limit = settings["max_distance"]
    lines = [
        "Wiederholungsprüfung (rein lesend, nur Verdachtsstellen)",
        f"Datei: {report['file']}",
        (
            f"Wörter: {summary['total_words']}; Mindestlänge: "
            f"{settings['min_words']}; max. Abstand: "
            f"{'unbegrenzt' if limit is None else limit}"
        ),
        (
            f"Verdachtsstellen: {summary['findings']}; wiederholte Wörter: "
            f"{summary['repeated_words']} ({summary['repeated_ratio']:.1%})"
        ),
    ]
    if not report["findings"]:
        lines.append("Keine Wiederholung über den gewählten Schwellen.")
        return "\n".join(lines)
    for item in report["findings"]:
        lines.extend([
            "",
            (
                f"{item['length_words']} Wörter, Abstand {item['distance_words']} "
                f"Wörter, {item['occurrences']}x vorhanden"
            ),
            f"  zuerst: {_position(item, 'first')}; erneut: {_position(item, 'repeat')}",
            f"  Text: {item['text']}",
        ])
    lines.extend([
        "",
        "Hinweis: Absichtliche Wiederholungen sind möglich; vor einer Korrektur "
        "bitte Audio prüfen.",
    ])
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Längere Wortwiederholungen in einem Transkript rein lesend melden. "
            "Es werden keine Dateien verändert und kein Text entfernt."
        )
    )
    parser.add_argument(
        "transcript",
        type=Path,
        help="Markdown-Transkript oder zugehörige .segments.json (mit Zeitangaben)",
    )
    parser.add_argument(
        "--min-words",
        type=int,
        default=DEFAULT_MIN_WORDS,
        help=f"Mindestlänge einer Wiederholung in Wörtern (Standard: {DEFAULT_MIN_WORDS})",
    )
    parser.add_argument(
        "--max-distance",
        type=int,
        default=None,
        help="Nur Wiederholungen bis zu diesem Abstand in Wörtern melden (Standard: unbegrenzt)",
    )
    parser.add_argument("--json", action="store_true", help="JSON ausgeben")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; exit code 0 also when suspicious spots are found."""
    args = parse_args(argv)
    try:
        report = build_repetition_report(
            args.transcript,
            min_words=args.min_words,
            max_distance=args.max_distance,
        )
    except (OSError, UnicodeError, SegmentMetadataError, ValueError) as exc:
        print(f"[X] Wiederholungsprüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_human_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
