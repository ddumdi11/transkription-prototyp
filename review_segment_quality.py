#!/usr/bin/env python3
"""Prepare explicitly confirmed context clips for suspicious ASR segments."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

from analyze_segment_quality import (
    DEFAULT_MAX_CHARS_PER_SECOND,
    DEFAULT_MIN_TEXT_CHARACTERS,
    DEFAULT_SHORT_SEGMENT_SECONDS,
    find_suspicious_segments,
)
from project_glossary import glossary_hotwords, glossary_prompt
from providers.base import TranscriptionProvider, TranscriptionResult
from segment_metadata import SegmentMetadataError, load_segment_metadata


DEFAULT_CONTEXT_SECONDS = 12.0
DEFAULT_MAX_EXTENDED_SECONDS = 90.0
DEFAULT_OUTPUT_DIR = Path("staging/quality-review")
DEFAULT_MODEL = "medium"
SUPPORTED_AUDIO_SUFFIXES = {".wav", ".m4a", ".mp3", ".flac", ".ogg", ".webm"}
EXTRACTION_TIMEOUT_STARTUP_SECONDS = 15.0
EXTRACTION_TIMEOUT_REALTIME_FACTOR = 4.0


@dataclass(frozen=True)
class SegmentReviewPlan:
    """One dry-run-safe mapping from an ASR record to a padded review clip."""

    audio_path: Path
    segments_path: Path
    source_id: str
    transcript_file: str
    candidate: dict[str, Any]
    previous_segment: dict[str, Any] | None
    next_segment: dict[str, Any] | None
    context_seconds: float
    extended_to_next_segment: bool
    clip_start: float
    clip_end: float
    review_key: str
    output_audio: Path
    output_metadata: Path

    def display(self) -> dict[str, Any]:
        return {
            "review_key": self.review_key,
            "source_id": self.source_id,
            "audio": str(self.audio_path),
            "segments": str(self.segments_path),
            "segment_id": self.candidate["id"],
            "flags": self.candidate["flags"],
            "nominal_interval": {
                "start": self.candidate["start"],
                "end": self.candidate["end"],
                "duration": self.candidate["duration"],
            },
            "review_window": {
                "start": self.clip_start,
                "end": self.clip_end,
                "context_seconds": self.context_seconds,
                "extended_to_next_segment": self.extended_to_next_segment,
            },
            "segment_text": self.candidate["raw_text"],
            "output_audio": str(self.output_audio),
            "output_metadata": str(self.output_metadata),
        }


def _safe_component(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return result or "segment"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _segment_summary(segment: dict[str, Any] | None) -> dict[str, Any] | None:
    if segment is None:
        return None
    return {
        "id": segment["id"],
        "start": segment["start"],
        "end": segment["end"],
        "raw_text": segment["raw_text"],
        "text": segment["text"],
    }


def build_review_plans(
    audio_path: Path,
    segments_path: Path,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    context_seconds: float = DEFAULT_CONTEXT_SECONDS,
    extend_to_next_segment: bool = False,
    max_extended_seconds: float = DEFAULT_MAX_EXTENDED_SECONDS,
    max_chars_per_second: float = DEFAULT_MAX_CHARS_PER_SECOND,
    min_text_characters: int = DEFAULT_MIN_TEXT_CHARACTERS,
    short_segment_seconds: float = DEFAULT_SHORT_SEGMENT_SECONDS,
    selected_ids: set[str] | None = None,
) -> list[SegmentReviewPlan]:
    """Return review plans without creating directories or files."""
    audio_path = audio_path.expanduser().resolve()
    segments_path = segments_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise ValueError(f"Audiodatei fehlt oder ist leer: {audio_path}")
    if audio_path.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError(f"Nicht unterstütztes Audioformat: {audio_path.suffix}")
    if not isfinite(context_seconds) or context_seconds < 0:
        raise ValueError("Der Kontext muss eine endliche nichtnegative Zahl sein")
    if not isfinite(max_extended_seconds) or max_extended_seconds <= 0:
        raise ValueError("Die maximale Erweiterung muss eine positive Zahl sein")

    payload = load_segment_metadata(segments_path)
    if payload.get("audio_file") != audio_path.name:
        raise ValueError(
            "Audiodatei passt nicht zur Segmentdatei: "
            f"{audio_path.name!r} != {payload.get('audio_file')!r}"
        )
    source_id = payload.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("Segmentdaten enthalten keine stabile source_id")
    transcript_file = payload.get("transcript_file")
    if not isinstance(transcript_file, str) or not transcript_file.strip():
        raise ValueError("Segmentdaten enthalten keine transcript_file-Angabe")

    candidates = find_suspicious_segments(
        payload,
        max_chars_per_second=max_chars_per_second,
        min_text_characters=min_text_characters,
        short_segment_seconds=short_segment_seconds,
        selected_ids=selected_ids,
    )
    if selected_ids:
        candidates = [item for item in candidates if item["id"] in selected_ids]

    segments = payload["segments"]
    indexes = {segment["id"]: index for index, segment in enumerate(segments)}
    plans = []
    for candidate in candidates:
        index = indexes[candidate["id"]]
        previous_segment = segments[index - 1] if index else None
        next_segment = segments[index + 1] if index + 1 < len(segments) else None
        clip_start = max(0.0, float(candidate["start"]) - context_seconds)
        clip_end = float(candidate["end"]) + context_seconds
        extended = False
        if extend_to_next_segment and next_segment is not None:
            extended_end = min(
                float(next_segment["start"]) + context_seconds,
                float(candidate["start"]) + max_extended_seconds,
            )
            if extended_end > clip_end:
                clip_end = extended_end
                extended = True
        material = "\0".join(
            (
                source_id,
                candidate["id"],
                f"{clip_start:.3f}",
                f"{clip_end:.3f}",
            )
        ).encode("utf-8")
        review_key = hashlib.sha256(material).hexdigest()[:16]
        source_key = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
        stem = (
            f"review__{source_key}__"
            f"{_safe_component(candidate['id'])[:64]}__{review_key}"
        )
        plans.append(
            SegmentReviewPlan(
                audio_path=audio_path,
                segments_path=segments_path,
                source_id=source_id,
                transcript_file=transcript_file,
                candidate=candidate,
                previous_segment=previous_segment,
                next_segment=next_segment,
                context_seconds=context_seconds,
                extended_to_next_segment=extended,
                clip_start=round(clip_start, 3),
                clip_end=round(clip_end, 3),
                review_key=review_key,
                output_audio=output_dir / f"{stem}.flac",
                output_metadata=output_dir / f"{stem}.json",
            )
        )
    return plans


def extraction_timeout(
    duration: float,
    configured_timeout: float | None = None,
) -> float:
    if configured_timeout is not None:
        if not isfinite(configured_timeout) or configured_timeout <= 0:
            raise ValueError("Der FFmpeg-Timeout muss eine positive Zahl sein")
        return configured_timeout
    return EXTRACTION_TIMEOUT_STARTUP_SECONDS + duration * EXTRACTION_TIMEOUT_REALTIME_FACTOR


def _existing_clip_is_valid(plan: SegmentReviewPlan) -> bool:
    if not plan.output_audio.is_file() or not plan.output_metadata.is_file():
        return False
    if plan.output_audio.stat().st_size == 0:
        return False
    try:
        payload = json.loads(plan.output_metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    clip = payload.get("clip") if isinstance(payload, dict) else None
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and payload.get("review_type") == "asr_segment_context"
        and payload.get("review_key") == plan.review_key
        and isinstance(clip, dict)
        and clip.get("sha256") == _sha256(plan.output_audio)
        and clip.get("size") == plan.output_audio.stat().st_size
    )


def extract_review_clip(
    plan: SegmentReviewPlan,
    timeout_seconds: float | None = None,
) -> tuple[Path, Path, bool]:
    """Create one losslessly encoded context clip and atomic metadata."""
    if plan.output_audio.exists() or plan.output_metadata.exists():
        if _existing_clip_is_valid(plan):
            return plan.output_audio, plan.output_metadata, False
        raise FileExistsError(
            "QA-Ausgabe existiert bereits, ist aber nicht vollständig oder "
            f"nicht identisch: {plan.output_audio.parent}"
        )

    output_dir = plan.output_audio.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_fd, audio_name = tempfile.mkstemp(
        prefix=f".{plan.output_audio.stem}.", suffix=".tmp.flac", dir=output_dir
    )
    os.close(audio_fd)
    temp_audio = Path(audio_name)
    temp_audio.unlink()
    metadata_fd, metadata_name = tempfile.mkstemp(
        prefix=f".{plan.output_metadata.stem}.", suffix=".tmp.json", dir=output_dir
    )
    os.close(metadata_fd)
    temp_metadata = Path(metadata_name)
    duration = plan.clip_end - plan.clip_start
    timeout = extraction_timeout(duration, timeout_seconds)
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{plan.clip_start:.3f}",
        "-i", str(plan.audio_path),
        "-t", f"{duration:.3f}",
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-c:a", "flac",
        str(temp_audio),
    ]

    audio_installed = False
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if not temp_audio.is_file() or temp_audio.stat().st_size == 0:
            raise RuntimeError("FFmpeg hat keinen verwendbaren QA-Ausschnitt erzeugt")
        metadata = {
            "schema_version": 1,
            "review_type": "asr_segment_context",
            "review_key": plan.review_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "confirmation": "explicit_cli",
            "source": {
                "source_id": plan.source_id,
                "audio_file": plan.audio_path.name,
                "segments_file": plan.segments_path.name,
                "transcript_file": plan.transcript_file,
            },
            "candidate": {
                "id": plan.candidate["id"],
                "start": plan.candidate["start"],
                "end": plan.candidate["end"],
                "duration": plan.candidate["duration"],
                "flags": plan.candidate["flags"],
                "raw_text": plan.candidate["raw_text"],
                "text": plan.candidate["text"],
            },
            "adjacent_segments": {
                "previous": _segment_summary(plan.previous_segment),
                "next": _segment_summary(plan.next_segment),
            },
            "review_window": {
                "start": plan.clip_start,
                "end": plan.clip_end,
                "requested_duration": round(duration, 3),
                "context_seconds": plan.context_seconds,
                "extended_to_next_segment": plan.extended_to_next_segment,
            },
            "clip": {
                "file": plan.output_audio.name,
                "sha256": _sha256(temp_audio),
                "size": temp_audio.stat().st_size,
                "codec": "flac",
                "encoding": "lossless_without_explicit_resampling",
            },
        }
        temp_metadata.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_audio.replace(plan.output_audio)
        audio_installed = True
        temp_metadata.replace(plan.output_metadata)
    except Exception as exc:
        temp_audio.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)
        if audio_installed and not plan.output_metadata.exists():
            plan.output_audio.unlink(missing_ok=True)
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or "").strip()
            if detail:
                raise RuntimeError(f"FFmpeg fehlgeschlagen: {detail}") from exc
        raise
    return plan.output_audio, plan.output_metadata, True


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def compare_segment_text(original: str, retranscription: str) -> dict[str, Any]:
    """Measure ordered token coverage; this is diagnostic, not ground truth."""
    original_tokens = _tokens(original)
    retranscribed_tokens = _tokens(retranscription)
    if not original_tokens:
        coverage = 0.0
        ratio = 0.0
    else:
        matcher = SequenceMatcher(
            None, original_tokens, retranscribed_tokens, autojunk=False
        )
        matching = sum(block.size for block in matcher.get_matching_blocks())
        coverage = matching / len(original_tokens)
        ratio = matcher.ratio()
    if coverage >= 0.75:
        assessment = "original_text_supported"
    elif coverage >= 0.4:
        assessment = "partial_match_needs_review"
    else:
        assessment = "low_match_needs_listening"
    return {
        "original_token_count": len(original_tokens),
        "retranscribed_token_count": len(retranscribed_tokens),
        "ordered_original_token_coverage": round(coverage, 3),
        "sequence_ratio": round(ratio, 3),
        "assessment": assessment,
        "note": "diagnostic_only_not_ground_truth",
    }


def _recognition_config(
    model: str,
    language: str,
    prompt: str | None,
    hotwords: str | None,
) -> dict[str, Any]:
    settings = {
        "model": model,
        "language": language,
        "prompt": prompt,
        "hotwords": hotwords,
    }
    encoded = json.dumps(
        settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        **settings,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def write_local_retranscription(
    plan: SegmentReviewPlan,
    provider: TranscriptionProvider,
    *,
    model: str,
    language: str,
    prompt: str | None,
    hotwords: str | None,
) -> tuple[Path, bool]:
    """Transcribe an existing review clip and atomically store comparison data."""
    if not _existing_clip_is_valid(plan):
        raise RuntimeError(f"QA-Ausschnitt fehlt oder ist ungültig: {plan.output_audio}")
    recognition = _recognition_config(model, language, prompt, hotwords)
    target = plan.output_metadata.with_name(
        f"{plan.output_metadata.stem}.retranscription__"
        f"{recognition['sha256'][:12]}.json"
    )
    if target.exists():
        try:
            current = json.loads(
                target.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FileExistsError(
                f"Ungültige Neu-Transkription existiert: {target}"
            ) from exc
        saved_recognition = (
            current.get("recognition") if isinstance(current, dict) else None
        )
        if (
            isinstance(current, dict)
            and current.get("schema_version") == 1
            and current.get("review_key") == plan.review_key
            and isinstance(saved_recognition, dict)
            and saved_recognition.get("sha256") == recognition["sha256"]
            and current.get("clip_sha256") == _sha256(plan.output_audio)
        ):
            return target, False
        raise FileExistsError(
            f"Abweichende Neu-Transkription existiert: {target}"
        )

    result: TranscriptionResult = provider.transcribe_with_segments(
        plan.output_audio,
        language=language,
        prompt=prompt,
        hotwords=hotwords,
    )
    requested_duration = plan.clip_end - plan.clip_start
    segments = [
        {
            "start": round(float(segment.start), 3),
            "end": round(float(segment.end), 3),
            "source_start": round(plan.clip_start + float(segment.start), 3),
            "source_end": round(plan.clip_start + float(segment.end), 3),
            "timestamp_within_requested_window": (
                0 <= float(segment.start) <= float(segment.end)
                <= requested_duration + 0.01
            ),
            "text": segment.text,
        }
        for segment in result.segments
    ]
    payload = {
        "schema_version": 1,
        "review_type": "asr_segment_local_retranscription",
        "review_key": plan.review_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "clip_file": plan.output_audio.name,
        "clip_sha256": _sha256(plan.output_audio),
        "provider": "local",
        "recognition": recognition,
        "text": result.text,
        "segments": segments,
        "timestamp_warning_count": sum(
            not segment["timestamp_within_requested_window"]
            for segment in segments
        ),
        "comparison": compare_segment_text(plan.candidate["raw_text"], result.text),
    }
    output_dir = target.parent
    fd, name = tempfile.mkstemp(
        prefix=f".{target.stem}.",
        suffix=".tmp.json",
        dir=output_dir,
    )
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target, True


def _recognition_hints(
    explicit_prompt: str | None,
    explicit_hotwords: str | None,
    use_pipeline_hints: bool,
) -> tuple[str | None, str | None]:
    prompt = explicit_prompt
    if prompt is None and use_pipeline_hints:
        prompt = os.environ.get("AUDIOREC_PROMPT")
        if prompt is None:
            prompt = glossary_prompt()
    hotwords = explicit_hotwords
    if hotwords is None and use_pipeline_hints:
        hotwords = os.environ.get("AUDIOREC_HOTWORDS")
        if hotwords is None:
            hotwords = ", ".join(glossary_hotwords())
    return prompt, hotwords


def _input_pairs(args: argparse.Namespace) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    if args.audio is not None or args.segments is not None:
        if args.audio is None or args.segments is None:
            raise ValueError("Audio und Segmentdatei müssen gemeinsam angegeben werden")
        pairs.append((args.audio, args.segments))
    pairs.extend(tuple(pair) for pair in args.input)
    if not pairs:
        raise ValueError(
            "Audio und Segmentdatei positional oder mit --input angeben"
        )
    return pairs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Kontextausschnitte für verdächtige ASR-Segmente vorbereiten. "
            "Ohne --confirm ausschließlich Dry-Run."
        )
    )
    parser.add_argument("audio", type=Path, nargs="?", help="Lokale Quell-Audiodatei")
    parser.add_argument("segments", type=Path, nargs="?", help="Zugehörige .segments.json")
    parser.add_argument(
        "--input", action="append", nargs=2, type=Path, default=[],
        metavar=("AUDIO", "SEGMENTE"),
        help="Weiteres Eingabepaar; für einen gemeinsamen Modell-Batch wiederholen",
    )
    parser.add_argument(
        "--segment-id", action="append", default=[],
        help="Nur diese Segment-ID prüfen; mehrfach verwendbar",
    )
    parser.add_argument(
        "--context-seconds", type=float, default=DEFAULT_CONTEXT_SECONDS,
        help="Audio vor und nach dem nominellen Intervall (Standard: 12)",
    )
    parser.add_argument(
        "--extend-to-next-segment", action="store_true",
        help=(
            "Prüffenster bei kollabierten Endzeiten kontrolliert bis zur "
            "nächsten Segmentgrenze erweitern"
        ),
    )
    parser.add_argument(
        "--max-extended-seconds", type=float,
        default=DEFAULT_MAX_EXTENDED_SECONDS,
        help="Obergrenze ab nominellem Start für die Erweiterung (Standard: 90)",
    )
    parser.add_argument(
        "--max-chars-per-second", type=float,
        default=DEFAULT_MAX_CHARS_PER_SECOND,
    )
    parser.add_argument(
        "--min-text-characters", type=int,
        default=DEFAULT_MIN_TEXT_CHARACTERS,
    )
    parser.add_argument(
        "--short-segment-seconds", type=float,
        default=DEFAULT_SHORT_SEGMENT_SECONDS,
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Lokales Ausgabeziel (Standard: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Kontextausschnitte und Metadaten wirklich schreiben",
    )
    parser.add_argument(
        "--transcribe-local", action="store_true",
        help="Bestätigte Ausschnitte zusätzlich lokal neu transkribieren",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--language", default="de")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--hotwords", default=None)
    parser.add_argument(
        "--use-pipeline-hints", action="store_true",
        help="Pipeline-Prompt und Hotwords ausdrücklich für einen Vergleich verwenden",
    )
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.transcribe_local and not args.confirm:
            raise ValueError("--transcribe-local erfordert --confirm")
        if args.json and args.confirm:
            raise ValueError("--json ist nur für den Dry-Run verfügbar")
        input_pairs = _input_pairs(args)
        if args.segment_id and len(input_pairs) > 1:
            raise ValueError(
                "--segment-id ist nur mit genau einem Eingabepaar zulässig"
            )
        plans = []
        for audio_path, segments_path in input_pairs:
            plans.extend(build_review_plans(
                audio_path,
                segments_path,
                args.output_dir,
                context_seconds=args.context_seconds,
                extend_to_next_segment=args.extend_to_next_segment,
                max_extended_seconds=args.max_extended_seconds,
                max_chars_per_second=args.max_chars_per_second,
                min_text_characters=args.min_text_characters,
                short_segment_seconds=args.short_segment_seconds,
                selected_ids=set(args.segment_id) or None,
            ))
        if args.json:
            print(json.dumps(
                {
                    "dry_run": True,
                    "plans": [plan.display() for plan in plans],
                },
                ensure_ascii=False,
                indent=2,
            ))
        else:
            if not plans:
                print("Keine verdächtigen oder ausgewählten ASR-Segmente.")
            for plan in plans:
                print(
                    f"{plan.candidate['id']}: nominell "
                    f"{plan.candidate['start']:.3f}–{plan.candidate['end']:.3f}s; "
                    f"Prüfausschnitt {plan.clip_start:.3f}–{plan.clip_end:.3f}s"
                )
                print(f"  -> {plan.output_audio}")
        if not args.confirm:
            if not args.json:
                print("Dry-Run: keine Datei geschrieben. Mit --confirm bestätigen.")
            return 0
        if not plans:
            return 0

        provider = None
        prompt = hotwords = None
        if args.transcribe_local:
            from providers.local_provider import LocalWhisperProvider

            prompt, hotwords = _recognition_hints(
                args.prompt, args.hotwords, args.use_pipeline_hints
            )
            provider = LocalWhisperProvider(model_size=args.model)

        for plan in plans:
            audio, metadata, created = extract_review_clip(
                plan, timeout_seconds=args.timeout_seconds
            )
            state = "erstellt" if created else "bereits identisch vorhanden"
            print(f"QA-Ausschnitt {state}: {audio}")
            print(f"Metadaten: {metadata}")
            if provider is not None:
                result_path, retranscribed = write_local_retranscription(
                    plan,
                    provider,
                    model=args.model,
                    language=args.language,
                    prompt=prompt,
                    hotwords=hotwords,
                )
                result_state = (
                    "erstellt" if retranscribed else "bereits identisch vorhanden"
                )
                print(f"Lokale Neu-Transkription {result_state}: {result_path}")
        return 0
    except (
        ImportError,
        OSError,
        RuntimeError,
        SegmentMetadataError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"[X] QA-Prüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
