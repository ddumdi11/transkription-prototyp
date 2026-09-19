"""Load and validate machine-readable transcript segment sidecars."""

from __future__ import annotations

import json
from math import isfinite
from pathlib import Path
from typing import Any


class SegmentMetadataError(ValueError):
    """The segment sidecar is missing, unreadable or structurally invalid."""


def validate_segment_payload(
    payload: object,
    path: Path,
    expected_source_id: str | None = None,
) -> dict[str, Any]:
    """Validate a decoded version-1 segment payload and return it typed."""
    if not isinstance(payload, dict):
        raise SegmentMetadataError(f"Segmentdaten sind kein JSON-Objekt: {path}")
    if payload.get("schema_version") != 1:
        raise SegmentMetadataError(f"Unbekannte Segmentdaten-Version: {path}")
    if (
        expected_source_id is not None
        and payload.get("source_id") != expected_source_id
    ):
        raise SegmentMetadataError(
            f"Segmentdaten gehören nicht zu Drive-ID {expected_source_id}: {path}"
        )

    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise SegmentMetadataError(
            f"Segmentliste fehlt oder ist ungültig: {path}"
        )

    seen_ids: set[str] = set()
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise SegmentMetadataError(f"Segment {index} ist kein Objekt: {path}")
        if any(
            not isinstance(segment.get(field), str)
            for field in ("id", "raw_text", "text")
        ):
            raise SegmentMetadataError(
                f"Segment {index} enthält ungültigen Text: {path}"
            )
        segment_id = segment["id"]
        if not segment_id or segment_id in seen_ids:
            raise SegmentMetadataError(
                f"Segment {index} enthält keine eindeutige ID: {path}"
            )
        seen_ids.add(segment_id)

        start = segment.get("start")
        end = segment.get("end")
        timestamps = (start, end)
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                for value in timestamps
            )
            or any(
                isinstance(value, float) and not isfinite(value)
                for value in timestamps
            )
            or start < 0
            or end < start
        ):
            raise SegmentMetadataError(
                f"Segment {index} enthält ungültige Zeiten: {path}"
            )

    return payload


def load_segment_metadata(
    path: Path,
    expected_source_id: str | None = None,
) -> dict[str, Any]:
    """Read and validate a segment sidecar."""
    if not path.exists():
        raise SegmentMetadataError(f"Segmentdaten wurden nicht erzeugt: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SegmentMetadataError(
            f"Segmentdaten sind nicht lesbar: {path}"
        ) from exc
    return validate_segment_payload(payload, path, expected_source_id)
