#!/usr/bin/env python3
"""Create a read-only, segment-level routing plan for recording sessions."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from inbox_watcher import setup_logging
from plan_transcript_sessions import (
    DEFAULT_MAX_GAP_MINUTES,
    DEFAULT_TIMEZONE,
    STATE_DIR,
    open_readonly_state,
    plan_sessions,
    serializable_session,
)
from route_transcripts import CONFIG_PATH, load_config, matching_terms
from segment_metadata import load_segment_metadata


def add_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def segment_reference(
    recording: dict[str, Any], segment: dict[str, Any]
) -> dict[str, Any]:
    absolute_start = recording["recording_start"] + timedelta(
        seconds=float(segment["start"])
    )
    absolute_end = recording["recording_start"] + timedelta(
        seconds=float(segment["end"])
    )
    return {
        "drive_id": recording["drive_id"],
        "audio_path": recording["audio_path"],
        "recording_number": recording["recording_number"],
        "segment_id": segment["id"],
        "start": segment["start"],
        "end": segment["end"],
        "absolute_start": absolute_start.isoformat(timespec="seconds"),
        "absolute_end": absolute_end.isoformat(timespec="seconds"),
        "text": segment["text"],
    }


def build_session_routing(
    session: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Add deterministic project and topic evidence to one session."""
    projects: dict[str, dict[str, Any]] = {}
    topics: dict[str, dict[str, Any]] = {}
    unassigned_segments: list[dict[str, Any]] = []
    segment_count = 0
    content_assigned_count = 0

    def project_entry(name: str) -> dict[str, Any]:
        return projects.setdefault(name, {
            "name": name,
            "whole_session": False,
            "scopes": [],
            "segments": [],
            "_segment_index": {},
        })

    for project in config["default_projects"]:
        entry = project_entry(str(project))
        entry["whole_session"] = True
        add_unique(entry["scopes"], "default")
    for project in config["active_projects"]:
        entry = project_entry(str(project))
        entry["whole_session"] = True
        add_unique(entry["scopes"], "active_context")

    for recording in session["recordings"]:
        payload = load_segment_metadata(
            Path(recording["segment_path"]),
            expected_source_id=recording["drive_id"],
        )
        for segment in payload["segments"]:
            segment_count += 1
            reference = segment_reference(recording, segment)
            segment_key = (recording["drive_id"], segment["id"])
            assigned_to_project = False

            for rule in config["project_rules"]:
                matched = matching_terms(segment["text"], rule["match_any"])
                if not matched:
                    continue
                assigned_to_project = True
                entry = project_entry(str(rule["project"]))
                add_unique(entry["scopes"], "content")
                existing = entry["_segment_index"].get(segment_key)
                if existing is None:
                    existing = {**reference, "matched_terms": []}
                    entry["_segment_index"][segment_key] = existing
                    entry["segments"].append(existing)
                for term in matched:
                    add_unique(existing["matched_terms"], term)

            if assigned_to_project:
                content_assigned_count += 1
            else:
                unassigned_segments.append(reference)

            for topic, terms in config["topic_rules"].items():
                matched = matching_terms(segment["text"], terms)
                if not matched:
                    continue
                entry = topics.setdefault(str(topic), {
                    "name": str(topic),
                    "segments": [],
                })
                entry["segments"].append({
                    **reference,
                    "matched_terms": matched,
                })

    project_results = []
    for entry in projects.values():
        entry.pop("_segment_index")
        project_results.append(entry)

    result = serializable_session(session)
    result.update({
        "segment_count": segment_count,
        "content_assigned_segment_count": content_assigned_count,
        "unassigned_segment_count": len(unassigned_segments),
        "projects": project_results,
        "topics": list(topics.values()),
        "unassigned_segments": unassigned_segments,
    })
    return result


def plan_session_routes(
    db: sqlite3.Connection,
    selected_date: date,
    timezone: ZoneInfo,
    max_gap_minutes: float,
    config: dict[str, Any],
    break_before: set[str] | None = None,
    join_with_previous: set[str] | None = None,
) -> list[dict[str, Any]]:
    sessions = plan_sessions(
        db,
        selected_date=selected_date,
        timezone=timezone,
        max_gap_minutes=max_gap_minutes,
        break_before=break_before,
        join_with_previous=join_with_previous,
    )
    return [build_session_routing(session, config) for session in sessions]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sitzungen und ihre Segmente schreibgeschützt Projekten zuordnen"
    )
    parser.add_argument("--date", type=date.fromisoformat, metavar="JJJJ-MM-TT")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--max-gap-minutes", type=float, default=DEFAULT_MAX_GAP_MINUTES,
    )
    parser.add_argument("--break-before", action="append", default=[])
    parser.add_argument("--join-with-previous", action="append", default=[])
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
        config = load_config(args.config)
        with open_readonly_state(args.state_dir / "state.sqlite3") as db:
            sessions = plan_session_routes(
                db,
                selected_date=selected_date,
                timezone=timezone,
                max_gap_minutes=args.max_gap_minutes,
                config=config,
                break_before=set(args.break_before),
                join_with_previous=set(args.join_with_previous),
            )

        if args.json:
            print(json.dumps({
                "dry_run": True,
                "date": selected_date.isoformat(),
                "timezone": args.timezone,
                "max_gap_minutes": args.max_gap_minutes,
                "sessions": sessions,
            }, ensure_ascii=False, indent=2))
        else:
            logger.info(
                "Sitzungs-Routing-Dry-Run: date=%s sessions=%d",
                selected_date, len(sessions),
            )
            for session in sessions:
                logger.info(
                    "session=%s recordings=%d segments=%d content_assigned=%d "
                    "unassigned=%d",
                    session["session_id"], session["recording_count"],
                    session["segment_count"],
                    session["content_assigned_segment_count"],
                    session["unassigned_segment_count"],
                )
                for project in session["projects"]:
                    logger.info(
                        "  project=%r scopes=%s evidence_segments=%d",
                        project["name"], ",".join(project["scopes"]),
                        len(project["segments"]),
                    )
                for topic in session["topics"]:
                    logger.info(
                        "  topic=%s evidence_segments=%d",
                        topic["name"], len(topic["segments"]),
                    )
            logger.info(
                "Dry-Run beendet (keine Transkript-, Status- oder Drive-Änderung)"
            )
        return 0
    except Exception as exc:
        logger.exception("Sitzungs-Routing fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
