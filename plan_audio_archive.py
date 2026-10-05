#!/usr/bin/env python3
"""Build a read-only plan for lossless archival of processed audio."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from analyze_segment_quality import find_suspicious_segments
from inbox_watcher import setup_logging, staging_name
from quality_reviews import candidate_hash
from segment_metadata import SegmentMetadataError, load_segment_metadata


DEFAULT_STATE = Path(".inbox-watcher/state.sqlite3")
DEFAULT_ARCHIVE_TARGET = os.environ.get("AUDIOREC_ARCHIVE_TARGET")


def open_readonly_state(path: Path) -> sqlite3.Connection:
    """Open an existing pipeline database without creating files or schemas."""
    if not path.is_file():
        raise ValueError(f"Pipeline-Datenbank fehlt: {path}")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def pipeline_sources(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return jobs and their publication/staging evidence without changing state."""
    required = {"files", "transcription_jobs", "published_transcripts"}
    missing = sorted(name for name in required if not table_exists(db, name))
    if missing:
        raise ValueError(
            "Pipeline-Datenbank ist unvollständig; Tabelle(n) fehlen: "
            + ", ".join(missing)
        )
    staged_join = (
        "LEFT JOIN staged_files s USING (drive_id)"
        if table_exists(db, "staged_files")
        else "LEFT JOIN (SELECT NULL AS drive_id, NULL AS staged_local_path, "
             "NULL AS staged_content_hash) s ON 0"
    )
    staged_columns = (
        "s.local_path AS staged_local_path, s.content_hash AS staged_content_hash"
        if table_exists(db, "staged_files")
        else "s.staged_local_path, s.staged_content_hash"
    )
    return db.execute(
        f"""SELECT j.drive_id, j.status AS job_status, j.local_audio,
                   j.transcript_path, j.last_error, f.path AS source_path,
                   f.size AS source_size, f.content_hash AS source_hash,
                   f.hash_type AS source_hash_type, f.mod_time,
                   p.remote_path AS published_path, p.remote_id,
                   p.published_at, {staged_columns}
            FROM transcription_jobs j
            JOIN files f USING (drive_id)
            LEFT JOIN published_transcripts p USING (drive_id)
            {staged_join}
            ORDER BY f.mod_time, j.drive_id"""
    ).fetchall()


def archive_name(source_path: str, drive_id: str) -> str:
    """Derive a collision-resistant FLAC name from the canonical staging name."""
    return str(Path(staging_name(source_path, drive_id)).with_suffix(".flac"))


def archive_destination(target: str | None, name: str) -> str | None:
    if not target or not target.strip():
        return None
    normalized = target.strip().rstrip("/")
    separator = "" if normalized.endswith(":") else "/"
    return f"{normalized}{separator}{name}"


def _current_quality_rows(
    db: sqlite3.Connection, drive_id: str
) -> list[sqlite3.Row]:
    if not table_exists(db, "quality_reviews"):
        return []
    return list(db.execute(
        """SELECT segment_id, candidate_hash, status
           FROM quality_reviews
           WHERE drive_id=? AND is_current=1
           ORDER BY segment_start, segment_id""",
        (drive_id,),
    ))


def assess_quality(
    db: sqlite3.Connection, drive_id: str, transcript_path: object
) -> dict[str, Any]:
    """Re-run the detector and compare it with persistent human QA decisions."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return {
            "status": "METADATA_MISSING",
            "candidate_count": None,
            "counts": {},
            "detail": "Transkriptpfad fehlt",
        }
    sidecar = Path(transcript_path).with_suffix(".segments.json")
    try:
        payload = load_segment_metadata(sidecar, expected_source_id=drive_id)
        candidates = find_suspicious_segments(payload)
    except (OSError, SegmentMetadataError, ValueError) as exc:
        return {
            "status": "METADATA_INVALID",
            "candidate_count": None,
            "counts": {},
            "detail": str(exc),
            "segment_path": str(sidecar),
        }

    current = _current_quality_rows(db, drive_id)
    states = {
        (str(row["segment_id"]), str(row["candidate_hash"])): str(row["status"])
        for row in current
    }
    detected_keys = {
        (str(candidate["id"]), candidate_hash(candidate)) for candidate in candidates
    }
    counts = {"PENDING": 0, "CONFIRMED": 0, "DISMISSED": 0, "UNREGISTERED": 0}
    for key in detected_keys:
        status = states.get(key, "UNREGISTERED")
        counts[status] = counts.get(status, 0) + 1
    stale = len(set(states) - detected_keys)
    unknown_states = sum(
        count for state, count in counts.items()
        if state not in {"PENDING", "CONFIRMED", "DISMISSED", "UNREGISTERED"}
    )

    if stale or unknown_states:
        status = "STATE_MISMATCH"
    elif counts["CONFIRMED"]:
        status = "CONFIRMED_ISSUES"
    elif counts["PENDING"]:
        status = "PENDING"
    elif counts["UNREGISTERED"]:
        status = "UNREGISTERED"
    else:
        status = "CLEAR"
    return {
        "status": status,
        "candidate_count": len(candidates),
        "counts": counts,
        "stale_current_records": stale,
        "unknown_status_records": unknown_states,
        "segment_path": str(sidecar),
    }


def plan_one(
    db: sqlite3.Connection, row: sqlite3.Row, archive_target: str | None
) -> dict[str, Any]:
    drive_id = str(row["drive_id"])
    source_path = str(row["source_path"])
    expected_name = archive_name(source_path, drive_id)
    archive_blockers: list[str] = []

    if row["job_status"] != "DONE":
        archive_blockers.append("transcription_not_done")
    if not row["published_path"] or not row["remote_id"]:
        archive_blockers.append("transcript_not_published")
    if Path(source_path).suffix.lower() != ".wav":
        archive_blockers.append("source_not_wav")

    local_audio = Path(row["local_audio"]) if row["local_audio"] else None
    if local_audio is None:
        archive_blockers.append("local_audio_path_missing")
        local_size = None
    elif not local_audio.is_file():
        archive_blockers.append("local_audio_missing")
        local_size = None
    else:
        local_size = local_audio.stat().st_size
        if local_size <= 0:
            archive_blockers.append("local_audio_empty")
        if local_size != int(row["source_size"]):
            archive_blockers.append("local_size_mismatch")
        if local_audio.suffix.lower() != ".wav":
            archive_blockers.append("local_audio_not_wav")

    if not row["staged_local_path"]:
        archive_blockers.append("staging_record_missing")
    elif local_audio is not None:
        if Path(str(row["staged_local_path"])) != local_audio:
            archive_blockers.append("staging_path_mismatch")
    if not row["staged_content_hash"]:
        archive_blockers.append("staging_hash_missing")
    else:
        if str(row["staged_content_hash"]).lower() != str(row["source_hash"]).lower():
            archive_blockers.append("staging_hash_mismatch")

    quality = assess_quality(db, drive_id, row["transcript_path"])
    cleanup_blockers = ["archive_not_created", "retention_policy_unconfigured"]
    if quality["status"] != "CLEAR":
        cleanup_blockers.append(f"quality_{quality['status'].lower()}")

    return {
        "drive_id": drive_id,
        "source_path": source_path,
        "local_audio": str(local_audio) if local_audio is not None else None,
        "source_size": int(row["source_size"]),
        "local_size": local_size,
        "source_hash_type": row["source_hash_type"],
        "source_hash": row["source_hash"],
        "job_status": row["job_status"],
        "published_path": row["published_path"],
        "archive_name": expected_name,
        "archive_destination": archive_destination(archive_target, expected_name),
        "archive_status": "CANDIDATE" if not archive_blockers else "HOLD",
        "archive_blockers": archive_blockers,
        "quality": quality,
        "cleanup_ready": False,
        "cleanup_blockers": cleanup_blockers,
    }


def build_plan(
    db: sqlite3.Connection,
    archive_target: str | None = None,
    selected_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows = pipeline_sources(db)
    known_ids = {str(row["drive_id"]) for row in rows}
    if selected_ids:
        missing = sorted(selected_ids - known_ids)
        if missing:
            raise ValueError("Unbekannte Drive-ID(s): " + ", ".join(missing))
        rows = [row for row in rows if row["drive_id"] in selected_ids]
    return [plan_one(db, row, archive_target) for row in rows]


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [item for item in items if item["archive_status"] == "CANDIDATE"]
    holds = [item for item in items if item["archive_status"] == "HOLD"]
    qa_counts: dict[str, int] = {}
    hold_reasons: dict[str, int] = {}
    for item in items:
        status = str(item["quality"]["status"])
        qa_counts[status] = qa_counts.get(status, 0) + 1
        for reason in item["archive_blockers"]:
            hold_reasons[reason] = hold_reasons.get(reason, 0) + 1
    return {
        "total_jobs": len(items),
        "archive_candidates": len(candidates),
        "held": len(holds),
        "candidate_source_bytes": sum(item["source_size"] for item in candidates),
        "cleanup_ready": 0,
        "quality_statuses": qa_counts,
        "hold_reasons": hold_reasons,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verarbeitete WAV-Dateien schreibgeschützt für ein FLAC-Archiv planen"
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--archive-target", default=DEFAULT_ARCHIVE_TARGET,
        help="Logisches rclone-Ziel; alternativ AUDIOREC_ARCHIVE_TARGET",
    )
    parser.add_argument(
        "--drive-id", action="append", default=[], metavar="DRIVE_ID",
        help="Plan auf diese Drive-ID begrenzen (wiederholbar)",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_logging(args.state.parent)
    try:
        with open_readonly_state(args.state) as db:
            items = build_plan(
                db,
                archive_target=args.archive_target,
                selected_ids=set(args.drive_id) or None,
            )
        summary = summarize(items)
        if args.json:
            print(json.dumps({
                "dry_run": True,
                "state": str(args.state),
                "archive_target": args.archive_target,
                "summary": summary,
                "items": items,
            }, ensure_ascii=False, indent=2))
        else:
            logger.info(
                "Archiv-Dry-Run: jobs=%d candidates=%d held=%d bytes=%d cleanup_ready=0",
                summary["total_jobs"], summary["archive_candidates"],
                summary["held"], summary["candidate_source_bytes"],
            )
            if summary["hold_reasons"]:
                logger.info("Zurückstellungen nach Grund: %s", summary["hold_reasons"])
            if not args.archive_target:
                logger.info(
                    "Archivziel noch nicht konfiguriert; erwartete FLAC-Namen werden "
                    "trotzdem geplant"
                )
            if args.verbose:
                for item in items:
                    logger.info(
                        "archive=%s path=%r id=%s qa=%s blockers=%s target=%r",
                        item["archive_status"], item["source_path"], item["drive_id"],
                        item["quality"]["status"],
                        ",".join(item["archive_blockers"]) or "-",
                        item["archive_destination"],
                    )
            logger.info(
                "Dry-Run beendet (keine Datenbank-, Audio- oder Drive-Änderung; "
                "keine Datei zur Löschung freigegeben)"
            )
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.error("Archivplanung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
