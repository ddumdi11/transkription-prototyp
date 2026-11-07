#!/usr/bin/env python3
"""Plan conservative AudioRec phone backups without changing any state."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 1
PLAN_TYPE = "audiorec_phone_backup_plan"
DEFAULT_INVENTORY_STATE = Path(".inbox-watcher/audiorec-inventory.sqlite3")
DEFAULT_PIPELINE_STATE = Path(".inbox-watcher/state.sqlite3")
DEFAULT_DEVICE_ID = "audiorec-android-primary"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def setup_cli_logging() -> logging.Logger:
    """Return a stderr-only logger without creating runtime files."""
    logger = logging.getLogger("audiorec_backup_plan")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


@contextmanager
def open_readonly_state(
    path: Path, label: str
) -> Iterator[sqlite3.Connection]:
    """Yield an existing SQLite state in query-only mode and always close it."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} fehlt: {path}")
    db: sqlite3.Connection | None = None
    try:
        db = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        if db is not None:
            db.close()


def table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    """Return the declared columns of one table, or an empty set."""
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return set()
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}


def require_columns(
    db: sqlite3.Connection, table: str, required: Iterable[str], label: str
) -> None:
    """Fail closed when a required table or column is absent."""
    missing = sorted(set(required) - table_columns(db, table))
    if missing:
        raise ValueError(
            f"{label} ist unvollständig; {table} fehlt oder benötigt: "
            + ", ".join(missing)
        )


def validate_states(
    inventory_db: sqlite3.Connection, pipeline_db: sqlite3.Connection
) -> None:
    """Validate the minimal schemas needed by the read-only planner."""
    require_columns(
        inventory_db,
        "inventory_epochs",
        {"device_id", "epoch_id", "coverage_from", "is_current"},
        "AudioRec-Inventardatenbank",
    )
    require_columns(
        inventory_db,
        "inventory_recordings",
        {
            "inventory_id", "device_id", "epoch_id", "relative_path",
            "original_name", "normalized_name", "recording_number",
            "size_bytes", "modified_ns", "modified_at", "sha256",
            "hash_status", "identity_status", "presence_status", "is_current",
            "last_scan_id",
        },
        "AudioRec-Inventardatenbank",
    )
    require_columns(
        inventory_db,
        "inventory_scans",
        {"scan_id", "device_id", "epoch_id", "scanned_at"},
        "AudioRec-Inventardatenbank",
    )
    require_columns(
        pipeline_db,
        "files",
        {"drive_id", "path", "size", "content_hash", "hash_type", "mod_time"},
        "Pipeline-Datenbank",
    )
    require_columns(
        pipeline_db,
        "transcription_jobs",
        {"drive_id", "status", "transcript_path"},
        "Pipeline-Datenbank",
    )
    require_columns(
        pipeline_db,
        "published_transcripts",
        {"drive_id", "remote_id", "remote_path"},
        "Pipeline-Datenbank",
    )


def current_epoch(
    db: sqlite3.Connection, device_id: str, selected_epoch: str | None
) -> sqlite3.Row:
    """Return the one configured current epoch for a phone."""
    rows = db.execute(
        """SELECT device_id,epoch_id,coverage_from
           FROM inventory_epochs WHERE device_id=? AND is_current=1""",
        (device_id,),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            f"Für device-id {device_id!r} wird genau eine aktuelle Epoche benötigt"
        )
    row = rows[0]
    if selected_epoch is not None and row["epoch_id"] != selected_epoch:
        raise ValueError(
            f"Aktuelle Epoche ist {row['epoch_id']!r}, nicht {selected_epoch!r}"
        )
    return row


def latest_scan(
    db: sqlite3.Connection, device_id: str, epoch_id: str
) -> dict[str, str] | None:
    """Return the newest recorded inventory scan metadata."""
    row = db.execute(
        """SELECT scan_id,scanned_at FROM inventory_scans
           WHERE device_id=? AND epoch_id=?
           ORDER BY scanned_at DESC,scan_id DESC LIMIT 1""",
        (device_id, epoch_id),
    ).fetchone()
    if row is None:
        return None
    return {"scan_id": str(row["scan_id"]), "scanned_at": str(row["scanned_at"])}


def phone_recordings(
    db: sqlite3.Connection,
    device_id: str,
    epoch_id: str,
    selected_numbers: set[int] | None = None,
) -> list[sqlite3.Row]:
    """Return current, present phone recordings in deterministic order."""
    rows = db.execute(
        """SELECT inventory_id,relative_path,original_name,normalized_name,
                  recording_number,size_bytes,modified_ns,modified_at,sha256,
                  hash_status,identity_status,last_scan_id
           FROM inventory_recordings
           WHERE device_id=? AND epoch_id=? AND is_current=1
                 AND presence_status='PRESENT'
           ORDER BY modified_ns,normalized_name,inventory_id""",
        (device_id, epoch_id),
    ).fetchall()
    if selected_numbers is None:
        return list(rows)
    known = {
        int(row["recording_number"])
        for row in rows
        if row["recording_number"] is not None
    }
    unknown = sorted(selected_numbers - known)
    if unknown:
        raise ValueError(
            "Unbekannte aktuelle Aufnahme-Nummer(n): "
            + ", ".join(str(number) for number in unknown)
        )
    return [
        row for row in rows
        if row["recording_number"] is not None
        and int(row["recording_number"]) in selected_numbers
    ]


def pipeline_candidates(db: sqlite3.Connection) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Index pipeline evidence by exact normalized path and byte size."""
    rows = db.execute(
        """SELECT f.drive_id,f.path,f.size,f.content_hash,f.hash_type,f.mod_time,
                  j.status AS job_status,j.transcript_path,
                  p.remote_id AS transcript_remote_id,
                  p.remote_path AS transcript_remote_path
           FROM files f
           LEFT JOIN transcription_jobs j USING (drive_id)
           LEFT JOIN published_transcripts p USING (drive_id)
           ORDER BY f.path,f.size,f.drive_id"""
    ).fetchall()
    indexed: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        path = unicodedata.normalize("NFC", str(row["path"]))
        hash_type = str(row["hash_type"]).lower()
        content_hash = row["content_hash"]
        if hash_type == "sha256" and (
            not isinstance(content_hash, str)
            or not SHA256_PATTERN.fullmatch(content_hash.lower())
        ):
            raise ValueError(
                f"Ungültiger Pipeline-SHA-256 für Drive-ID {row['drive_id']}"
            )
        indexed[(path, int(row["size"]))].append({
            "drive_id": str(row["drive_id"]),
            "path": str(row["path"]),
            "size_bytes": int(row["size"]),
            "hash_type": hash_type,
            "content_hash": str(content_hash).lower(),
            "drive_mod_time": row["mod_time"],
            "job_status": row["job_status"],
            "transcript_path": row["transcript_path"],
            "transcript_published": bool(row["transcript_remote_id"]),
            "transcript_remote_id": row["transcript_remote_id"],
            "transcript_remote_path": row["transcript_remote_path"],
        })
    return indexed


def verified_phone_sha256(row: sqlite3.Row) -> str | None:
    """Return a usable phone hash only when its status explicitly verifies it."""
    value = row["sha256"]
    if row["hash_status"] != "VERIFIED":
        return None
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value.lower()):
        raise ValueError(
            f"Ungültiger verifizierter SHA-256 für {row['inventory_id']}"
        )
    return value.lower()


def plan_item(
    phone: sqlite3.Row, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Classify one phone recording without claiming unproven remote safety."""
    distinct_hashes = sorted({
        (candidate["hash_type"], candidate["content_hash"])
        for candidate in candidates
    })
    phone_sha256 = verified_phone_sha256(phone)
    if not candidates:
        match_status = "NO_PIPELINE_MATCH"
        next_action = "LOCATE_REMOTE_OR_COPY"
    elif len(distinct_hashes) > 1:
        match_status = "MULTIPLE_PIPELINE_HASHES"
        next_action = "REVIEW_PIPELINE_CONFLICT"
    elif distinct_hashes[0][0] != "sha256":
        match_status = "UNSUPPORTED_PIPELINE_HASH"
        next_action = "COMPUTE_COMPARABLE_HASH"
    elif phone_sha256 is None:
        match_status = "PHONE_HASH_REQUIRED"
        next_action = "COMPUTE_PHONE_SHA256"
    elif phone_sha256 == distinct_hashes[0][1]:
        match_status = "CONTENT_HASH_MATCH"
        next_action = "VERIFY_REMOTE_PRESENCE"
    else:
        match_status = "CONTENT_HASH_MISMATCH"
        next_action = "REVIEW_CONTENT_MISMATCH"
    return {
        "inventory_id": str(phone["inventory_id"]),
        "relative_path": str(phone["relative_path"]),
        "original_name": str(phone["original_name"]),
        "recording_number": phone["recording_number"],
        "size_bytes": int(phone["size_bytes"]),
        "modified_at": str(phone["modified_at"]),
        "last_scan_id": str(phone["last_scan_id"]),
        "phone_hash_status": str(phone["hash_status"]),
        "phone_sha256": phone_sha256,
        "identity_status": str(phone["identity_status"]),
        "match_status": match_status,
        "backup_status": "NOT_PROVEN",
        "next_action": next_action,
        "pipeline_candidate_count": len(candidates),
        "pipeline_content_hashes": [
            {"hash_type": hash_type, "content_hash": content_hash}
            for hash_type, content_hash in distinct_hashes
        ],
        "pipeline_candidates": candidates,
    }


def build_backup_plan(
    inventory_db: sqlite3.Connection,
    pipeline_db: sqlite3.Connection,
    *,
    inventory_state: Path,
    pipeline_state: Path,
    device_id: str,
    epoch_id: str | None = None,
    selected_numbers: set[int] | None = None,
) -> dict[str, Any]:
    """Build one deterministic, fully read-only backup evidence plan."""
    validate_states(inventory_db, pipeline_db)
    inventory_db.execute("BEGIN")
    try:
        epoch = current_epoch(inventory_db, device_id, epoch_id)
        phones = phone_recordings(
            inventory_db, device_id, str(epoch["epoch_id"]), selected_numbers
        )
        inventory_scan = latest_scan(
            inventory_db, device_id, str(epoch["epoch_id"])
        )
    finally:
        inventory_db.rollback()
    indexed = pipeline_candidates(pipeline_db)
    items = [
        plan_item(
            phone,
            indexed.get(
                (unicodedata.normalize("NFC", str(phone["normalized_name"])),
                 int(phone["size_bytes"])),
                [],
            ),
        )
        for phone in phones
    ]
    statuses = Counter(str(item["match_status"]) for item in items)
    actions = Counter(str(item["next_action"]) for item in items)
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_type": PLAN_TYPE,
        "read_only": True,
        "remote_verified": False,
        "backup_guarantee": "NONE",
        "sources": {
            "inventory_state": str(inventory_state),
            "pipeline_state": str(pipeline_state),
            "device_id": device_id,
            "epoch_id": str(epoch["epoch_id"]),
            "coverage_from": str(epoch["coverage_from"]),
            "latest_inventory_scan": inventory_scan,
        },
        "summary": {
            "recordings": len(items),
            "total_bytes": sum(int(item["size_bytes"]) for item in items),
            "match_statuses": dict(sorted(statuses.items())),
            "next_actions": dict(sorted(actions.items())),
            "backup_proven": 0,
        },
        "items": items,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the read-only backup planner command line."""
    parser = argparse.ArgumentParser(
        description=(
            "AudioRec-Smartphone-Inventar konservativ mit Pipeline-Belegen "
            "abgleichen; keine Dateien kopieren oder löschen"
        )
    )
    parser.add_argument("--inventory-state", type=Path, default=DEFAULT_INVENTORY_STATE)
    parser.add_argument("--pipeline-state", type=Path, default=DEFAULT_PIPELINE_STATE)
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    parser.add_argument("--epoch-id")
    parser.add_argument(
        "--recording-number", type=int, action="append", default=[],
        help="Nur diese AudioRec-Nummer planen (wiederholbar)",
    )
    parser.add_argument("--json", action="store_true", help="Vollständigen Plan ausgeben")
    return parser.parse_args(argv)


def render_summary(plan: dict[str, Any]) -> str:
    """Render a compact human-readable summary without transcript or audio data."""
    summary = plan["summary"]
    lines = [
        f"AudioRec-Sicherungsplan: {summary['recordings']} Aufnahme(n)",
        f"Bytes: {summary['total_bytes']}",
    ]
    lines.extend(
        f"- {status}: {count}"
        for status, count in summary["match_statuses"].items()
    )
    lines.append(
        "Keine Sicherung bestätigt: Smartphone-Hash und Live-Remote-Prüfung "
        "sind noch nicht gemeinsam belegt."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the planner while preserving both SQLite states."""
    args = parse_args(argv)
    logger = setup_cli_logging()
    try:
        if not args.device_id.strip():
            raise ValueError("device-id darf nicht leer sein")
        selected = set(args.recording_number) or None
        with (
            open_readonly_state(
                args.inventory_state, "AudioRec-Inventardatenbank"
            ) as inventory_db,
            open_readonly_state(args.pipeline_state, "Pipeline-Datenbank") as pipeline_db,
        ):
            plan = build_backup_plan(
                inventory_db,
                pipeline_db,
                inventory_state=args.inventory_state,
                pipeline_state=args.pipeline_state,
                device_id=args.device_id,
                epoch_id=args.epoch_id,
                selected_numbers=selected,
            )
        print(
            json.dumps(plan, ensure_ascii=False, indent=2)
            if args.json else render_summary(plan)
        )
        logger.info(
            "Plan blieb rein lesend; Drive und Smartphone wurden nicht abgefragt"
        )
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.error("AudioRec-Sicherungsplanung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
