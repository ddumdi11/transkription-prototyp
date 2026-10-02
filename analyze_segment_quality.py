#!/usr/bin/env python3
"""Read-only plausibility checks for ASR segment timestamps and nearby audio."""

from __future__ import annotations

import argparse
import json
from math import isfinite, log10
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from segment_metadata import SegmentMetadataError, load_segment_metadata


DEFAULT_CONTEXT_SECONDS = 8.0
DEFAULT_MAX_CHARS_PER_SECOND = 25.0
DEFAULT_MIN_TEXT_CHARACTERS = 20
DEFAULT_SHORT_SEGMENT_SECONDS = 0.5
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_SILENCE_DBFS = -45.0
DEFAULT_VAD_THRESHOLD = 0.5
AUDIO_TIMEOUT_STARTUP_SECONDS = 15.0
AUDIO_TIMEOUT_REALTIME_FACTOR = 4.0

AudioAnalyzer = Callable[[Path, float, float, float, float, float | None], dict[str, Any]]


def _finite_positive(value: float, label: str, *, allow_zero: bool = False) -> float:
    minimum_ok = value >= 0 if allow_zero else value > 0
    if not isfinite(value) or not minimum_ok:
        qualifier = "nichtnegative" if allow_zero else "positive"
        raise ValueError(f"{label} muss eine {qualifier} Zahl sein")
    return value


def _text_length(segment: dict[str, Any]) -> int:
    return len(segment["raw_text"].strip())


def find_suspicious_segments(
    payload: dict[str, Any],
    *,
    max_chars_per_second: float = DEFAULT_MAX_CHARS_PER_SECOND,
    min_text_characters: int = DEFAULT_MIN_TEXT_CHARACTERS,
    short_segment_seconds: float = DEFAULT_SHORT_SEGMENT_SECONDS,
    selected_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return timestamp candidates without reading or changing the audio file."""
    _finite_positive(max_chars_per_second, "Maximale Zeichendichte")
    _finite_positive(short_segment_seconds, "Kurze Segmentdauer")
    if min_text_characters < 1:
        raise ValueError("Die Mindesttextlänge muss mindestens 1 sein")

    segments = payload["segments"]
    known_ids = {segment["id"] for segment in segments}
    if selected_ids:
        missing = sorted(selected_ids - known_ids)
        if missing:
            raise ValueError(f"Segment-ID nicht gefunden: {', '.join(missing)}")

    candidates = []
    for index, segment in enumerate(segments):
        start = float(segment["start"])
        end = float(segment["end"])
        duration = end - start
        text_characters = _text_length(segment)
        chars_per_second = (
            text_characters / duration if duration > 0 else None
        )
        flags = []
        if (
            text_characters >= min_text_characters
            and (
                chars_per_second is None
                or chars_per_second > max_chars_per_second
            )
        ):
            flags.append("implausible_text_density")
        if (
            text_characters >= min_text_characters
            and duration < short_segment_seconds
        ):
            flags.append("very_short_text_segment")

        previous_gap = None
        next_gap = None
        if index:
            previous_gap = start - float(segments[index - 1]["end"])
            if previous_gap < 0:
                flags.append("overlaps_previous_segment")
        if index + 1 < len(segments):
            next_gap = float(segments[index + 1]["start"]) - end
            if next_gap < 0:
                flags.append("overlaps_next_segment")

        explicitly_selected = bool(selected_ids and segment["id"] in selected_ids)
        if flags or explicitly_selected:
            candidates.append({
                "id": segment["id"],
                "start": start,
                "end": end,
                "duration": round(duration, 3),
                "text_characters": text_characters,
                "chars_per_second": (
                    round(chars_per_second, 3)
                    if chars_per_second is not None
                    else None
                ),
                "raw_text": segment["raw_text"],
                "text": segment["text"],
                "flags": flags,
                "explicitly_selected": explicitly_selected,
                "previous_gap": (
                    round(previous_gap, 3)
                    if previous_gap is not None
                    else None
                ),
                "next_gap": (
                    round(next_gap, 3)
                    if next_gap is not None
                    else None
                ),
            })
    return candidates


def _dbfs(value: float) -> float | None:
    if value <= 1e-12:
        return None
    return round(20.0 * log10(value), 2)


def _overlap(start: float, end: float, other_start: float, other_end: float) -> float:
    return max(0.0, min(end, other_end) - max(start, other_start))


def _zone_metrics(
    samples: Any,
    speech_spans: list[tuple[float, float]],
    *,
    window_start: float,
    zone_start: float,
    zone_end: float,
    sample_rate: int,
    silence_dbfs: float,
) -> dict[str, Any]:
    import numpy as np

    actual_window_end = window_start + len(samples) / sample_rate
    start = max(window_start, zone_start)
    end = min(actual_window_end, zone_end)
    duration = max(0.0, end - start)
    if duration == 0:
        return {
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": 0.0,
            "classification": "unavailable",
            "speech_seconds": 0.0,
            "speech_ratio": 0.0,
            "rms_dbfs": None,
            "peak_dbfs": None,
        }

    relative_start = start - window_start
    relative_end = end - window_start
    first = max(0, int(round(relative_start * sample_rate)))
    last = min(len(samples), int(round(relative_end * sample_rate)))
    zone_samples = samples[first:last]
    if len(zone_samples):
        rms = float(np.sqrt(np.mean(np.square(zone_samples, dtype=np.float64))))
        peak = float(np.max(np.abs(zone_samples)))
    else:
        rms = peak = 0.0

    speech_seconds = sum(
        _overlap(relative_start, relative_end, speech_start, speech_end)
        for speech_start, speech_end in speech_spans
    )
    speech_seconds = min(duration, speech_seconds)
    speech_ratio = speech_seconds / duration
    rms_dbfs = _dbfs(rms)
    peak_dbfs = _dbfs(peak)
    minimum_speech = min(0.25, max(0.064, duration * 0.15))
    if speech_seconds >= minimum_speech and speech_ratio >= 0.1:
        classification = "speech"
    elif rms_dbfs is None or rms_dbfs <= silence_dbfs:
        classification = "silence"
    else:
        classification = "noise_or_uncertain"

    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(duration, 3),
        "classification": classification,
        "speech_seconds": round(speech_seconds, 3),
        "speech_ratio": round(speech_ratio, 3),
        "rms_dbfs": rms_dbfs,
        "peak_dbfs": peak_dbfs,
    }


def _interpret_audio_context(zones: dict[str, dict[str, Any]]) -> str:
    nominal = zones["nominal"]
    nearby = (zones["before"], zones["after"])
    if nominal["classification"] == "speech":
        return "speech_detected_in_nominal_window"
    if any(zone["classification"] == "speech" for zone in nearby):
        return "speech_detected_outside_nominal_window"
    available = [
        zone for zone in zones.values()
        if zone["classification"] != "unavailable"
    ]
    if available and all(zone["classification"] == "silence" for zone in available):
        return "no_speech_detected_in_context"
    return "inconclusive_audio_context"


def analyze_audio_context(
    audio_path: Path,
    segment_start: float,
    segment_end: float,
    context_seconds: float = DEFAULT_CONTEXT_SECONDS,
    vad_threshold: float = DEFAULT_VAD_THRESHOLD,
    timeout_seconds: float | None = None,
    *,
    silence_dbfs: float = DEFAULT_SILENCE_DBFS,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> dict[str, Any]:
    """Analyze one padded window in memory using FFmpeg and Silero VAD."""
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    _finite_positive(context_seconds, "Kontextfenster", allow_zero=True)
    _finite_positive(vad_threshold, "VAD-Schwelle")
    if vad_threshold > 1:
        raise ValueError("Die VAD-Schwelle darf höchstens 1 sein")
    if not isfinite(silence_dbfs):
        raise ValueError("Die Stillegrenze muss eine endliche Zahl sein")
    window_start = max(0.0, segment_start - context_seconds)
    requested_end = segment_end + context_seconds
    requested_duration = requested_end - window_start
    timeout = (
        _finite_positive(timeout_seconds, "FFmpeg-Timeout")
        if timeout_seconds is not None
        else AUDIO_TIMEOUT_STARTUP_SECONDS
        + requested_duration * AUDIO_TIMEOUT_REALTIME_FACTOR
    )
    command = [
        "ffmpeg", "-v", "error",
        "-ss", f"{window_start:.3f}",
        "-i", str(audio_path),
        "-t", f"{requested_duration:.3f}",
        "-map", "0:a:0",
        "-vn", "-sn", "-dn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "f32le",
        "pipe:1",
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=timeout,
    )
    if not completed.stdout:
        raise RuntimeError("FFmpeg hat für das Analysefenster kein Audio geliefert")
    if len(completed.stdout) % 4:
        raise RuntimeError("FFmpeg lieferte unvollständige Float32-Audiodaten")
    samples = np.frombuffer(completed.stdout, dtype="<f4")
    vad_options = VadOptions(
        threshold=vad_threshold,
        min_speech_duration_ms=100,
        min_silence_duration_ms=150,
        speech_pad_ms=0,
    )
    timestamps = get_speech_timestamps(
        samples,
        vad_options=vad_options,
        sampling_rate=sample_rate,
    )
    speech_spans = [
        (item["start"] / sample_rate, item["end"] / sample_rate)
        for item in timestamps
    ]
    actual_end = window_start + len(samples) / sample_rate
    zones = {
        "before": _zone_metrics(
            samples,
            speech_spans,
            window_start=window_start,
            zone_start=window_start,
            zone_end=segment_start,
            sample_rate=sample_rate,
            silence_dbfs=silence_dbfs,
        ),
        "nominal": _zone_metrics(
            samples,
            speech_spans,
            window_start=window_start,
            zone_start=segment_start,
            zone_end=segment_end,
            sample_rate=sample_rate,
            silence_dbfs=silence_dbfs,
        ),
        "after": _zone_metrics(
            samples,
            speech_spans,
            window_start=window_start,
            zone_start=segment_end,
            zone_end=requested_end,
            sample_rate=sample_rate,
            silence_dbfs=silence_dbfs,
        ),
    }
    return {
        "window_start": round(window_start, 3),
        "window_end": round(actual_end, 3),
        "requested_context_seconds": context_seconds,
        "vad_threshold": vad_threshold,
        "speech_spans": [
            {
                "start": round(window_start + start, 3),
                "end": round(window_start + end, 3),
            }
            for start, end in speech_spans
        ],
        "zones": zones,
        "interpretation": _interpret_audio_context(zones),
    }


def build_quality_report(
    audio_path: Path,
    segments_path: Path,
    *,
    context_seconds: float = DEFAULT_CONTEXT_SECONDS,
    max_chars_per_second: float = DEFAULT_MAX_CHARS_PER_SECOND,
    min_text_characters: int = DEFAULT_MIN_TEXT_CHARACTERS,
    short_segment_seconds: float = DEFAULT_SHORT_SEGMENT_SECONDS,
    vad_threshold: float = DEFAULT_VAD_THRESHOLD,
    silence_dbfs: float = DEFAULT_SILENCE_DBFS,
    timeout_seconds: float | None = None,
    selected_ids: set[str] | None = None,
    text_only: bool = False,
    audio_analyzer: AudioAnalyzer | None = None,
) -> dict[str, Any]:
    """Build a report without changing audio, sidecars, state or transcripts."""
    audio_path = audio_path.expanduser().resolve()
    segments_path = segments_path.expanduser().resolve()
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise ValueError(f"Audiodatei fehlt oder ist leer: {audio_path}")
    payload = load_segment_metadata(segments_path)
    if payload.get("audio_file") != audio_path.name:
        raise ValueError(
            "Audiodatei passt nicht zur Segmentdatei: "
            f"{audio_path.name!r} != {payload.get('audio_file')!r}"
        )
    candidates = find_suspicious_segments(
        payload,
        max_chars_per_second=max_chars_per_second,
        min_text_characters=min_text_characters,
        short_segment_seconds=short_segment_seconds,
        selected_ids=selected_ids,
    )
    analyzer = audio_analyzer or analyze_audio_context
    for candidate in candidates:
        if text_only:
            candidate["audio_context"] = None
            continue
        audio_context = analyzer(
            audio_path,
            candidate["start"],
            candidate["end"],
            context_seconds,
            vad_threshold,
            timeout_seconds,
            silence_dbfs=silence_dbfs,
        )
        if (
            "implausible_text_density" in candidate["flags"]
            and audio_context.get("interpretation")
            == "speech_detected_in_nominal_window"
        ):
            audio_context["interpretation"] = (
                "speech_present_but_timestamp_too_short"
            )
        candidate["audio_context"] = audio_context

    flag_counts: dict[str, int] = {}
    for candidate in candidates:
        for flag in candidate["flags"]:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    return {
        "schema_version": 1,
        "report_type": "asr_segment_quality",
        "read_only": True,
        "source_id": payload.get("source_id"),
        "audio_file": str(audio_path),
        "segments_file": str(segments_path),
        "settings": {
            "context_seconds": context_seconds,
            "max_chars_per_second": max_chars_per_second,
            "min_text_characters": min_text_characters,
            "short_segment_seconds": short_segment_seconds,
            "vad_threshold": vad_threshold,
            "silence_dbfs": silence_dbfs,
            "audio_analysis": not text_only,
        },
        "summary": {
            "total_segments": len(payload["segments"]),
            "reported_segments": len(candidates),
            "flag_counts": flag_counts,
        },
        "segments": candidates,
    }


def _human_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "ASR-Segmentprüfung (rein lesend)",
        f"Audio: {report['audio_file']}",
        (
            f"Segmente: {summary['total_segments']}; "
            f"gemeldet: {summary['reported_segments']}"
        ),
    ]
    if not report["segments"]:
        lines.append("Keine Auffälligkeit nach den gewählten Schwellen.")
        return "\n".join(lines)

    for item in report["segments"]:
        density = item["chars_per_second"]
        density_text = "unendlich" if density is None else f"{density:.1f}"
        flags = ", ".join(item["flags"]) or "explizit ausgewählt"
        lines.extend([
            "",
            (
                f"{item['id']} {item['start']:.3f}–{item['end']:.3f}s; "
                f"{item['duration']:.3f}s; {density_text} Zeichen/s"
            ),
            f"  Gründe: {flags}",
            f"  Text: {item['raw_text'].strip()}",
        ])
        audio_context = item["audio_context"]
        if audio_context is None:
            lines.append("  Audio: nicht untersucht (--text-only)")
            continue
        zones = audio_context["zones"]
        lines.append(
            "  Audio: "
            f"vorher={zones['before']['classification']} "
            f"({zones['before']['speech_seconds']:.2f}s Sprache), "
            f"nominell={zones['nominal']['classification']} "
            f"({zones['nominal']['speech_seconds']:.2f}s), "
            f"nachher={zones['after']['classification']} "
            f"({zones['after']['speech_seconds']:.2f}s)"
        )
        lines.append(f"  Einordnung: {audio_context['interpretation']}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ASR-Segmentzeiten und gepolsterte Audiofenster rein lesend auf "
            "Plausibilität prüfen. Es werden keine Dateien verändert."
        )
    )
    parser.add_argument("audio", type=Path, help="Lokale Quell-Audiodatei")
    parser.add_argument("segments", type=Path, help="Zugehörige .segments.json")
    parser.add_argument(
        "--segment-id",
        action="append",
        default=[],
        help="Zusätzlich ein bestimmtes Segment untersuchen (mehrfach möglich)",
    )
    parser.add_argument(
        "--context-seconds",
        type=float,
        default=DEFAULT_CONTEXT_SECONDS,
        help="Audiofenster vor und nach dem Segment (Standard: 8)",
    )
    parser.add_argument(
        "--max-chars-per-second",
        type=float,
        default=DEFAULT_MAX_CHARS_PER_SECOND,
        help="Warnschwelle für Textdichte (Standard: 25)",
    )
    parser.add_argument(
        "--min-text-characters",
        type=int,
        default=DEFAULT_MIN_TEXT_CHARACTERS,
        help="Mindesttextlänge für Dichte-/Kurzsegmentwarnungen (Standard: 20)",
    )
    parser.add_argument(
        "--short-segment-seconds",
        type=float,
        default=DEFAULT_SHORT_SEGMENT_SECONDS,
        help="Grenze für auffällig kurze Textsegmente (Standard: 0.5)",
    )
    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=DEFAULT_VAD_THRESHOLD,
        help="Silero-VAD-Sprachschwelle zwischen 0 und 1 (Standard: 0.5)",
    )
    parser.add_argument(
        "--silence-dbfs",
        type=float,
        default=DEFAULT_SILENCE_DBFS,
        help="RMS-Grenze für Stille, in dBFS (Standard: -45)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="Optionaler FFmpeg-Timeout je Analysefenster",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="Nur Segmentdaten prüfen; Audio und VAD nicht aufrufen",
    )
    parser.add_argument("--json", action="store_true", help="JSON ausgeben")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_quality_report(
            args.audio,
            args.segments,
            context_seconds=args.context_seconds,
            max_chars_per_second=args.max_chars_per_second,
            min_text_characters=args.min_text_characters,
            short_segment_seconds=args.short_segment_seconds,
            vad_threshold=args.vad_threshold,
            silence_dbfs=args.silence_dbfs,
            timeout_seconds=args.timeout_seconds,
            selected_ids=set(args.segment_id) or None,
            text_only=args.text_only,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(_human_report(report))
        return 0
    except (
        OSError,
        RuntimeError,
        SegmentMetadataError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            stderr = exc.stderr or b""
            detail = (
                stderr.decode(errors="replace")
                if isinstance(stderr, bytes)
                else str(stderr)
            ).strip()
            if detail:
                exc = RuntimeError(f"FFmpeg fehlgeschlagen: {detail}")
        print(f"[X] Segmentprüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
