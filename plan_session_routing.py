#!/usr/bin/env python3
"""Create a read-only, segment-level routing plan for recording sessions."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone as dt_timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, BinaryIO, Iterator
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


DEFAULT_MANIFEST_DIR = Path("staging/routing-manifests")
SESSION_ID_PATTERN = re.compile(r"session-\d{8}-\d{4}-[0-9a-f]{8}")


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
                matched = matching_terms(
                    segment["text"],
                    rule["match_any"],
                    config.get("exact_terms", []),
                )
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
                matched = matching_terms(
                    segment["text"], terms, config.get("exact_terms", [])
                )
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


def routing_plan_hash(session: dict[str, Any]) -> str:
    encoded = json.dumps(
        session,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_target(session: dict[str, Any], output_dir: Path) -> Path:
    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(f"Ungültige Sitzungs-ID für Manifest: {session_id!r}")
    return output_dir / f"routing__{session_id}.json"


def build_confirmed_manifest(
    session: dict[str, Any], confirmed_at: datetime | None = None
) -> dict[str, Any]:
    moment = confirmed_at or datetime.now(dt_timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("Bestätigungszeit benötigt eine Zeitzone")
    plan_hash = routing_plan_hash(session)
    return {
        "schema_version": 1,
        "manifest_type": "confirmed_session_routing",
        "manifest_key": f"{session['session_id']}:{plan_hash[:16]}",
        "routing_plan_sha256": plan_hash,
        "confirmed_at": moment.astimezone(dt_timezone.utc).isoformat(),
        "confirmation": "explicit_cli",
        "session": session,
    }


def existing_manifest_matches(path: Path, session: dict[str, Any]) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return False
    stored_session = payload.get("session")
    if not isinstance(stored_session, dict):
        return False
    try:
        stored_hash = routing_plan_hash(stored_session)
        expected_hash = routing_plan_hash(session)
        confirmed_at = datetime.fromisoformat(str(payload.get("confirmed_at", "")))
    except (TypeError, ValueError):
        return False
    expected_key = f"{session['session_id']}:{expected_hash[:16]}"
    return (
        payload.get("manifest_type") == "confirmed_session_routing"
        and payload.get("confirmation") == "explicit_cli"
        and payload.get("manifest_key") == expected_key
        and payload.get("routing_plan_sha256") == stored_hash == expected_hash
        and confirmed_at.tzinfo is not None
        and stored_session == session
    )


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def manifest_lock(target: Path) -> Iterator[None]:
    """Serialize the existence check and installation for one target."""
    lock_path = target.parent / f".{target.name}.lock"
    with lock_path.open("a+b") as handle:
        _lock_file(handle)
        try:
            yield
        finally:
            _unlock_file(handle)


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes or propagate the durability failure."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_confirmed_manifest(
    session: dict[str, Any],
    output_dir: Path,
    confirmed_at: datetime | None = None,
) -> tuple[Path, bool]:
    """Atomically install one explicitly confirmed, idempotent manifest."""
    output_dir = output_dir.expanduser().resolve()
    target = manifest_target(session, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_confirmed_manifest(session, confirmed_at)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    with manifest_lock(target):
        if target.exists():
            if existing_manifest_matches(target, session):
                return target, False
            raise FileExistsError(
                "Manifest existiert bereits, stimmt aber nicht mit dem aktuellen "
                f"Routingplan überein: {target}"
            )

        temporary: Path | None = None
        installed = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_dir,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
            installed = True
            fsync_directory(output_dir)
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if installed:
                target.unlink(missing_ok=True)
                try:
                    fsync_directory(output_dir)
                except OSError:
                    pass
            raise
        return target, True


def confirm_selected_sessions(
    sessions: list[dict[str, Any]],
    session_ids: list[str],
    output_dir: Path,
    confirmed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Validate all selections first, then write their local manifests."""
    if len(session_ids) != len(set(session_ids)):
        raise ValueError("Eine Sitzungs-ID wurde mehrfach zur Bestätigung angegeben")
    by_id = {session["session_id"]: session for session in sessions}
    unknown = [session_id for session_id in session_ids if session_id not in by_id]
    if unknown:
        raise ValueError(
            "Unbekannte Sitzungs-ID für dieses Datum: " + ", ".join(unknown)
        )

    results = []
    newly_created: list[Path] = []
    output_existed = output_dir.expanduser().resolve().exists()
    try:
        for session_id in session_ids:
            session = by_id[session_id]
            path, created = write_confirmed_manifest(
                session, output_dir, confirmed_at=confirmed_at
            )
            if created:
                newly_created.append(path)
            results.append({
                "session_id": session_id,
                "path": str(path),
                "created": created,
                "routing_plan_sha256": routing_plan_hash(session),
            })
    except Exception:
        for path in newly_created:
            path.unlink(missing_ok=True)
        resolved_output = output_dir.expanduser().resolve()
        if not output_existed and resolved_output.is_dir():
            try:
                resolved_output.rmdir()
            except OSError:
                pass
        raise
    return results


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
    parser.add_argument(
        "--confirm-session", action="append", default=[], metavar="SESSION_ID",
        help="Diese Sitzung ausdrücklich als lokales Manifest bestätigen (wiederholbar)",
    )
    parser.add_argument(
        "--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR,
        help=f"Lokales Manifestziel (Standard: {DEFAULT_MANIFEST_DIR})",
    )
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
        manifests = confirm_selected_sessions(
            sessions,
            session_ids=args.confirm_session,
            output_dir=args.manifest_dir,
        )

        if args.json:
            print(json.dumps({
                "dry_run": not bool(args.confirm_session),
                "date": selected_date.isoformat(),
                "timezone": args.timezone,
                "max_gap_minutes": args.max_gap_minutes,
                "sessions": sessions,
                "confirmed_manifests": manifests,
            }, ensure_ascii=False, indent=2))
        else:
            logger.info(
                "Sitzungs-Routing: mode=%s date=%s sessions=%d",
                "confirm" if args.confirm_session else "dry-run",
                selected_date,
                len(sessions),
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
            for manifest in manifests:
                logger.info(
                    "manifest session=%s path=%r status=%s",
                    manifest["session_id"], manifest["path"],
                    "neu" if manifest["created"] else "bereits identisch",
                )
            if manifests:
                logger.info(
                    "Bestätigung beendet (lokale Manifeste; keine Transkript-, "
                    "Status- oder Drive-Änderung)"
                )
            else:
                logger.info(
                    "Dry-Run beendet (keine Transkript-, Status- oder Drive-Änderung)"
                )
        return 0
    except Exception as exc:
        logger.exception("Sitzungs-Routing fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
