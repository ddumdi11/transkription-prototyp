#!/usr/bin/env python3
"""Create an explicitly confirmed ASR correction sample from one segment."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from segment_metadata import load_segment_metadata


DEFAULT_OUTPUT_DIR = Path("staging/training-samples")
SUPPORTED_AUDIO_SUFFIXES = {".wav", ".m4a", ".mp3", ".flac", ".ogg", ".webm"}


@dataclass(frozen=True)
class CorrectionSamplePlan:
    audio_path: Path
    segments_path: Path
    source_id: str
    transcript_file: str
    segment: dict[str, Any]
    term: str
    corrected_text: str
    sample_key: str
    output_audio: Path
    output_metadata: Path

    def display(self) -> dict[str, Any]:
        """Return a JSON-serializable dry-run view."""
        return {
            "source_id": self.source_id,
            "audio": str(self.audio_path),
            "segments": str(self.segments_path),
            "segment_id": self.segment["id"],
            "start": self.segment["start"],
            "end": self.segment["end"],
            "raw_text": self.segment["raw_text"],
            "normalized_text": self.segment["text"],
            "corrected_text": self.corrected_text,
            "term": self.term,
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


def select_segment(
    payload: dict[str, Any],
    term: str,
    segment_id: str | None = None,
) -> dict[str, Any]:
    """Select exactly one segment, automatically by term when unambiguous."""
    if segment_id:
        matches = [
            segment
            for segment in payload["segments"]
            if segment["id"] == segment_id
        ]
        if not matches:
            raise ValueError(f"Segment-ID nicht gefunden: {segment_id}")
        return matches[0]

    needle = term.casefold()
    matches = [
        segment
        for segment in payload["segments"]
        if needle in segment["text"].casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"Kein Segment enthält den bestätigten Begriff {term!r}"
        )
    ids = ", ".join(segment["id"] for segment in matches)
    raise ValueError(
        f"Begriff {term!r} kommt in mehreren Segmenten vor ({ids}); "
        "--segment-id ist erforderlich"
    )


def build_plan(
    audio_path: Path,
    segments_path: Path,
    output_dir: Path,
    term: str,
    segment_id: str | None = None,
    corrected_text: str | None = None,
) -> CorrectionSamplePlan:
    """Validate inputs and build a side-effect-free correction sample plan."""
    audio_path = audio_path.expanduser().resolve()
    segments_path = segments_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()

    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise ValueError(f"Audiodatei fehlt oder ist leer: {audio_path}")
    if audio_path.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError(f"Nicht unterstütztes Audioformat: {audio_path.suffix}")

    payload = load_segment_metadata(segments_path)
    source_id = payload.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("Segmentdaten enthalten keine stabile source_id")
    if payload.get("audio_file") != audio_path.name:
        raise ValueError(
            "Audiodatei passt nicht zur Segmentdatei: "
            f"{audio_path.name!r} != {payload.get('audio_file')!r}"
        )
    transcript_file = payload.get("transcript_file")
    if not isinstance(transcript_file, str) or not transcript_file:
        raise ValueError("Segmentdaten enthalten keine transcript_file-Angabe")

    term = term.strip()
    if not term:
        raise ValueError("Der bestätigte Begriff darf nicht leer sein")
    segment = select_segment(payload, term, segment_id)
    if segment["end"] <= segment["start"]:
        raise ValueError("Das ausgewählte Segment hat keine positive Dauer")

    confirmed = (
        corrected_text.strip()
        if corrected_text is not None
        else segment["text"].strip()
    )
    if not confirmed:
        raise ValueError("Der bestätigte Korrekturtext darf nicht leer sein")
    if confirmed == segment["raw_text"].strip():
        raise ValueError("Der bestätigte Text unterscheidet sich nicht vom ASR-Rohtext")
    if term.casefold() not in confirmed.casefold():
        raise ValueError(
            f"Der bestätigte Begriff {term!r} fehlt im Korrekturtext"
        )

    key_material = "\0".join(
        (source_id, segment["id"], term, confirmed)
    ).encode("utf-8")
    sample_key = hashlib.sha256(key_material).hexdigest()[:16]
    source_key = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
    sample_stem = (
        f"correction__{source_key}__"
        f"{_safe_component(segment['id'])[:64]}__{sample_key}"
    )
    return CorrectionSamplePlan(
        audio_path=audio_path,
        segments_path=segments_path,
        source_id=source_id,
        transcript_file=transcript_file,
        segment=segment,
        term=term,
        corrected_text=confirmed,
        sample_key=sample_key,
        output_audio=output_dir / f"{sample_stem}{audio_path.suffix.lower()}",
        output_metadata=output_dir / f"{sample_stem}.json",
    )


def _existing_sample_is_valid(plan: CorrectionSamplePlan) -> bool:
    if not plan.output_audio.is_file() or not plan.output_metadata.is_file():
        return False
    if plan.output_audio.stat().st_size == 0:
        return False
    try:
        payload = json.loads(plan.output_metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    clip = payload.get("clip") if isinstance(payload, dict) else None
    source = payload.get("source") if isinstance(payload, dict) else None
    segment = payload.get("segment") if isinstance(payload, dict) else None
    correction = payload.get("correction") if isinstance(payload, dict) else None
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and payload.get("sample_key") == plan.sample_key
        and isinstance(source, dict)
        and source.get("source_id") == plan.source_id
        and source.get("audio_file") == plan.audio_path.name
        and isinstance(segment, dict)
        and segment.get("id") == plan.segment["id"]
        and isinstance(correction, dict)
        and correction.get("term") == plan.term
        and correction.get("confirmed_text") == plan.corrected_text
        and isinstance(clip, dict)
        and clip.get("sha256") == _sha256(plan.output_audio)
        and clip.get("size") == plan.output_audio.stat().st_size
    )


def create_sample(plan: CorrectionSamplePlan) -> tuple[Path, Path, bool]:
    """Losslessly extract the planned clip and atomically write its metadata."""
    if plan.output_audio.exists() or plan.output_metadata.exists():
        if _existing_sample_is_valid(plan):
            return plan.output_audio, plan.output_metadata, False
        raise FileExistsError(
            "Ausgabe existiert bereits, ist aber kein vollständiges identisches "
            f"Korrekturbeispiel: {plan.output_audio.parent}"
        )

    output_dir = plan.output_audio.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_audio = output_dir / (
        f".{plan.output_audio.stem}.tmp{plan.output_audio.suffix}"
    )
    temp_metadata = output_dir / f".{plan.output_metadata.name}.tmp"
    duration = float(plan.segment["end"]) - float(plan.segment["start"])
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{float(plan.segment['start']):.3f}",
        "-i", str(plan.audio_path),
        "-t", f"{duration:.3f}",
        "-map", "0:a:0",
        "-c:a", "copy",
        str(temp_audio),
    ]

    audio_installed = False
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not temp_audio.is_file() or temp_audio.stat().st_size == 0:
            raise RuntimeError("ffmpeg hat keinen verwendbaren Audioausschnitt erzeugt")
        clip_hash = _sha256(temp_audio)
        metadata = {
            "schema_version": 1,
            "sample_key": plan.sample_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "confirmation": "explicit_cli",
            "source": {
                "source_id": plan.source_id,
                "audio_file": plan.audio_path.name,
                "segments_file": plan.segments_path.name,
                "transcript_file": plan.transcript_file,
            },
            "segment": {
                "id": plan.segment["id"],
                "start": plan.segment["start"],
                "end": plan.segment["end"],
                "raw_text": plan.segment["raw_text"],
                "normalized_text": plan.segment["text"],
            },
            "correction": {
                "term": plan.term,
                "confirmed_text": plan.corrected_text,
            },
            "clip": {
                "file": plan.output_audio.name,
                "sha256": clip_hash,
                "size": temp_audio.stat().st_size,
                "codec_mode": "copy",
            },
        }
        temp_metadata.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_audio.replace(plan.output_audio)
        audio_installed = True
        temp_metadata.replace(plan.output_metadata)
    except Exception:
        temp_audio.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)
        if audio_installed and not plan.output_metadata.exists():
            plan.output_audio.unlink(missing_ok=True)
        raise

    return plan.output_audio, plan.output_metadata, True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ein ausdrücklich bestätigtes ASR-Korrekturbeispiel aus einem "
            "Zeitsegment erzeugen. Ohne --confirm nur Dry-Run."
        )
    )
    parser.add_argument("audio", type=Path, help="Lokale Quell-Audiodatei")
    parser.add_argument("segments", type=Path, help="Zugehörige .segments.json")
    parser.add_argument("--term", required=True, help="Bestätigter Fachbegriff")
    parser.add_argument(
        "--segment-id",
        help="Explizite Segment-ID; nötig, wenn der Begriff mehrfach vorkommt",
    )
    parser.add_argument(
        "--corrected-text",
        help="Bestätigter Segmenttext; Standard ist das normalisierte Textfeld",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Lokales Ausgabeziel (Standard: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Audioausschnitt und Metadaten wirklich schreiben",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_plan(
            audio_path=args.audio,
            segments_path=args.segments,
            output_dir=args.output_dir,
            term=args.term,
            segment_id=args.segment_id,
            corrected_text=args.corrected_text,
        )
        print(json.dumps(plan.display(), ensure_ascii=False, indent=2))
        if not args.confirm:
            print("Dry-Run: keine Datei geschrieben. Mit --confirm bestätigen.")
            return 0
        audio_path, metadata_path, created = create_sample(plan)
        state = "erstellt" if created else "bereits identisch vorhanden"
        print(f"Korrekturbeispiel {state}: {audio_path}")
        print(f"Metadaten: {metadata_path}")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[X] Korrekturbeispiel fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
