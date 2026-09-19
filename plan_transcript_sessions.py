#!/usr/bin/env python3
"""Group published transcripts into deterministic recording sessions (dry-run)."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from inbox_watcher import open_state, setup_logging
from publish_transcripts import prepare_publish_state
from route_transcripts import recording_number
from segment_metadata import load_segment_metadata


STATE_DIR = Path(".inbox-watcher")
DEFAULT_TIMEZONE = os.environ.get("AUDIOREC_TIMEZONE", "Europe/Berlin")
DEFAULT_MAX_GAP_MINUTES = 90


def published_recordings(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return only successfully published recordings with their source time."""
    return db.execute(
        """SELECT p.drive_id, p.remote_path, p.published_at,
                  j.transcript_path, f.path AS audio_path, f.mod_time
           FROM published_transcripts p
           JOIN transcription_jobs j USING (drive_id)
           JOIN files f USING (drive_id)
           ORDER BY f.mod_time, p.drive_id"""
    ).fetchall()


def parse_mod_time(value: object, drive_id: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Drive-Zeit fehlt für {drive_id}")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Ungültige Drive-Zeit für {drive_id}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Drive-Zeit hat keine Zeitzone für {drive_id}: {value!r}")
    return parsed


def speech_duration(payload: dict[str, Any]) -> float:
    """Use the furthest segment end as the recording-duration approximation."""
    return max((float(segment["end"]) for segment in payload["segments"]), default=0.0)


def build_recording(row: sqlite3.Row, timezone: ZoneInfo) -> dict[str, Any]:
    drive_id = str(row["drive_id"])
    transcript_path = Path(row["transcript_path"])
    if not transcript_path.is_file():
        raise ValueError(f"Lokales Transkript fehlt: {transcript_path}")
    segment_path = transcript_path.with_suffix(".segments.json")
    payload = load_segment_metadata(segment_path, expected_source_id=drive_id)
    duration = speech_duration(payload)
    recording_end = parse_mod_time(row["mod_time"], drive_id).astimezone(timezone)
    estimated_start = recording_end - timedelta(seconds=duration)
    return {
        "drive_id": drive_id,
        "audio_path": row["audio_path"],
        "transcript_path": str(transcript_path),
        "segment_path": str(segment_path),
        "remote_path": row["remote_path"],
        "recording_number": recording_number(str(row["audio_path"])),
        "recording_start": estimated_start,
        "recording_end": recording_end,
        "speech_duration_seconds": round(duration, 3),
    }


def session_id(first: dict[str, Any]) -> str:
    digest = hashlib.sha256(first["drive_id"].encode("utf-8")).hexdigest()[:8]
    stamp = first["recording_end"].strftime("%Y%m%d-%H%M")
    return f"session-{stamp}-{digest}"


def group_recordings(
    recordings: Iterable[dict[str, Any]],
    max_gap_seconds: float,
    break_before: set[str] | None = None,
    join_with_previous: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isfinite(max_gap_seconds) or max_gap_seconds < 0:
        raise ValueError("Die maximale Pause muss eine endliche, nichtnegative Zahl sein")
    forced_breaks = set(break_before or ())
    forced_joins = set(join_with_previous or ())
    conflicts = forced_breaks & forced_joins
    if conflicts:
        raise ValueError(
            "Drive-ID zugleich getrennt und verbunden: " + ", ".join(sorted(conflicts))
        )

    ordered = [
        dict(item)
        for item in sorted(
            recordings,
            key=lambda item: (item["recording_end"], item["drive_id"]),
        )
    ]
    known_ids = {item["drive_id"] for item in ordered}
    unknown = (forced_breaks | forced_joins) - known_ids
    if unknown:
        raise ValueError(
            "Manuelle Regel verweist nicht auf die Auswahl: " + ", ".join(sorted(unknown))
        )
    if ordered and ordered[0]["drive_id"] in forced_joins:
        raise ValueError("Die erste Aufnahme kann nicht mit einer vorherigen verbunden werden")

    grouped: list[list[dict[str, Any]]] = []
    for recording in ordered:
        if not grouped:
            grouped.append([recording])
            continue
        previous = grouped[-1][-1]
        gap = (recording["recording_start"] - previous["recording_end"]).total_seconds()
        recording["gap_from_previous_seconds"] = round(gap, 3)
        should_split = gap > max_gap_seconds
        if recording["drive_id"] in forced_breaks:
            should_split = True
        elif recording["drive_id"] in forced_joins:
            should_split = False
        if should_split:
            grouped.append([recording])
        else:
            grouped[-1].append(recording)

    sessions = []
    for items in grouped:
        start = min(item["recording_start"] for item in items)
        end = max(item["recording_end"] for item in items)
        sessions.append(
            {
                "session_id": session_id(items[0]),
                "start": start,
                "end": end,
                "span_seconds": round((end - start).total_seconds(), 3),
                "recording_count": len(items),
                "recordings": items,
            }
        )
    return sessions


def isoformat(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def serializable_session(session: dict[str, Any]) -> dict[str, Any]:
    result = dict(session)
    result["start"] = isoformat(result["start"])
    result["end"] = isoformat(result["end"])
    result["recordings"] = []
    for recording in session["recordings"]:
        item = dict(recording)
        item["recording_start"] = isoformat(item["recording_start"])
        item["recording_end"] = isoformat(item["recording_end"])
        result["recordings"].append(item)
    return result


def plan_sessions(
    db: sqlite3.Connection,
    selected_date: date,
    timezone: ZoneInfo,
    max_gap_minutes: float,
    break_before: set[str] | None = None,
    join_with_previous: set[str] | None = None,
) -> list[dict[str, Any]]:
    recordings = []
    for row in published_recordings(db):
        end = parse_mod_time(row["mod_time"], str(row["drive_id"])).astimezone(timezone)
        if end.date() == selected_date:
            recordings.append(build_recording(row, timezone))
    return group_recordings(
        recordings,
        max_gap_seconds=max_gap_minutes * 60,
        break_before=break_before,
        join_with_previous=join_with_previous,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Veröffentlichte Transkripte schreibgeschützt zu Sitzungen gruppieren"
    )
    parser.add_argument(
        "--date", type=date.fromisoformat, metavar="JJJJ-MM-TT",
        help="Lokales Datum des Aufnahmeendes (Standard: heute)",
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument(
        "--max-gap-minutes", type=float, default=DEFAULT_MAX_GAP_MINUTES,
        help="Neue Sitzung nach längerer Ruhezeit (Standard: 90)",
    )
    parser.add_argument(
        "--break-before", action="append", default=[], metavar="DRIVE_ID",
        help="Vor dieser Aufnahme manuell eine neue Sitzung beginnen (wiederholbar)",
    )
    parser.add_argument(
        "--join-with-previous", action="append", default=[], metavar="DRIVE_ID",
        help="Diese Aufnahme trotz Zeitlücke mit der vorherigen verbinden (wiederholbar)",
    )
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_logging(args.state_dir)
    try:
        try:
            timezone = ZoneInfo(args.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unbekannte Zeitzone: {args.timezone}") from exc
        selected_date = args.date or datetime.now(timezone).date()
        with open_state(args.state_dir / "state.sqlite3") as db:
            prepare_publish_state(db)
            sessions = plan_sessions(
                db,
                selected_date=selected_date,
                timezone=timezone,
                max_gap_minutes=args.max_gap_minutes,
                break_before=set(args.break_before),
                join_with_previous=set(args.join_with_previous),
            )
        serializable = [serializable_session(session) for session in sessions]
        if args.json:
            print(json.dumps({
                "dry_run": True,
                "date": selected_date.isoformat(),
                "timezone": args.timezone,
                "max_gap_minutes": args.max_gap_minutes,
                "sessions": serializable,
            }, ensure_ascii=False, indent=2))
        else:
            total = sum(session["recording_count"] for session in sessions)
            logger.info(
                "Sitzungs-Dry-Run: date=%s sessions=%d recordings=%d",
                selected_date, len(sessions), total,
            )
            for session in serializable:
                numbers = [
                    f"#{item['recording_number']}" if item["recording_number"] is not None
                    else item["audio_path"]
                    for item in session["recordings"]
                ]
                logger.info(
                    "session=%s start=%s end=%s recordings=%s",
                    session["session_id"], session["start"], session["end"],
                    ",".join(numbers),
                )
            logger.info(
                "Dry-Run beendet (keine Transkript-, Status- oder Drive-Änderung)"
            )
        return 0
    except Exception as exc:
        logger.exception("Sitzungsplanung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
