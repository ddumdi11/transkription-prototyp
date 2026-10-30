#!/usr/bin/env python3
"""Plan transcript repairs from confirmed QA decisions without changing state."""

from __future__ import annotations

import argparse
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable

from quality_reviews import ISSUE_TYPES, open_readonly_state


DEFAULT_STATE = Path(".inbox-watcher/state.sqlite3")
TEXT_ISSUES = {"WORD_ERROR", "OMISSION", "HALLUCINATION", "OTHER"}
REPAIR_ACTIONS = {
    "NO_TEXT_CHANGE",
    "CURATED_REPLACEMENT_AVAILABLE",
    "CURATED_DELETION_AVAILABLE",
    "CURATION_REQUIRED",
}
RECORDING_STATUSES = {
    "NO_TEXT_CHANGE",
    "CURATED_CHANGES_AVAILABLE",
    "CURATION_REQUIRED",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_tables(db: sqlite3.Connection) -> tuple[bool, bool]:
    required = {"quality_reviews", "transcription_jobs", "published_transcripts"}
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?, ?)",
        tuple(sorted(required)),
    ).fetchall()
    present = {str(row[0]) for row in rows}
    missing = sorted(required - present)
    if missing:
        raise ValueError(
            "Pipeline-Datenbank enthält nicht alle benötigten Tabellen: "
            + ", ".join(missing)
        )
    quality_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(quality_reviews)")
    }
    return (
        "transcript_sha256" in quality_columns,
        "curation_action" in quality_columns,
    )


def _string_list(raw: object, label: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(raw, str):
        raise ValueError(f"{label} ist kein JSON-Text")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} ist ungültiges JSON") from exc
    if (
        not isinstance(values, list)
        or (not allow_empty and not values)
        or not all(isinstance(value, str) and value.strip() for value in values)
        or values != sorted(set(values))
    ):
        raise ValueError(f"{label} ist keine kanonische Begriffsliste")
    return values


def _repair_action(
    issue_types: set[str],
    confirmed_text: object,
    curation_action: object,
) -> str:
    if not issue_types & TEXT_ISSUES:
        if curation_action is not None:
            raise ValueError("Reiner Grenzfehler trägt unerwartete Textkuratierung")
        # Older hearing decisions may carry a confirmed excerpt as evidence even
        # though only the segment boundary was wrong.  Without an explicit
        # curation action that text must never become a replacement implicitly.
        return "NO_TEXT_CHANGE"
    if curation_action == "DELETE":
        if confirmed_text is not None:
            raise ValueError("DELETE-Kuratierung trägt unerwarteten Ersatztext")
        if "HALLUCINATION" not in issue_types:
            raise ValueError("DELETE-Kuratierung erfordert HALLUCINATION")
        return "CURATED_DELETION_AVAILABLE"
    if curation_action == "REPLACE":
        if not isinstance(confirmed_text, str) or not confirmed_text.strip():
            raise ValueError("REPLACE-Kuratierung benötigt einen Ersatztext")
        return "CURATED_REPLACEMENT_AVAILABLE"
    if curation_action is not None:
        raise ValueError(f"Unbekannte Kuratierungsaktion: {curation_action}")
    if isinstance(confirmed_text, str) and confirmed_text.strip():
        return "CURATED_REPLACEMENT_AVAILABLE"
    if confirmed_text is not None:
        raise ValueError("Kuratierter Ersatztext darf nicht leer sein")
    return "CURATION_REQUIRED"


def _recording_status(actions: Iterable[str]) -> str:
    present = set(actions)
    if "CURATION_REQUIRED" in present:
        return "CURATION_REQUIRED"
    if present & {
        "CURATED_REPLACEMENT_AVAILABLE",
        "CURATED_DELETION_AVAILABLE",
    }:
        return "CURATED_CHANGES_AVAILABLE"
    return "NO_TEXT_CHANGE"


def _confirmed_rows(
    db: sqlite3.Connection,
    selected_ids: set[str] | None,
    has_transcript_binding: bool,
    has_curation_action: bool,
) -> list[sqlite3.Row]:
    clauses = ["q.is_current=1", "q.status='CONFIRMED'"]
    values: list[str] = []
    if selected_ids:
        placeholders = ",".join("?" for _ in selected_ids)
        clauses.append(f"q.drive_id IN ({placeholders})")
        values.extend(sorted(selected_ids))
    binding = (
        "q.transcript_sha256" if has_transcript_binding else "NULL"
    )
    curation = "q.curation_action" if has_curation_action else "NULL"
    return list(db.execute(
        f"""SELECT q.*, {binding} AS decision_transcript_sha256,
                   {curation} AS repair_curation_action,
                   j.transcript_path,
                   p.remote_path, p.remote_id, p.published_at
            FROM quality_reviews q
            LEFT JOIN transcription_jobs j ON j.drive_id=q.drive_id
            LEFT JOIN published_transcripts p ON p.drive_id=q.drive_id
            WHERE {' AND '.join(clauses)}
            ORDER BY q.drive_id, q.segment_start, q.segment_id""",
        values,
    ))


def build_repair_plan(
    db: sqlite3.Connection, selected_ids: set[str] | None = None
) -> dict[str, Any]:
    """Build a deterministic repair plan from current confirmed QA rows."""
    has_transcript_binding, has_curation_action = _require_tables(db)
    requested = set(selected_ids or ())
    if any(not isinstance(value, str) or not value.strip() for value in requested):
        raise ValueError("Drive-ID darf nicht leer sein")
    rows = _confirmed_rows(
        db,
        requested or None,
        has_transcript_binding,
        has_curation_action,
    )
    found_ids = {str(row["drive_id"]) for row in rows}
    missing_ids = sorted(requested - found_ids)
    if missing_ids:
        raise ValueError(
            "Keine aktuelle bestätigte QA-Entscheidung für Drive-ID: "
            + ", ".join(missing_ids)
        )

    grouped: dict[str, dict[str, Any]] = {}
    action_counts = {action: 0 for action in sorted(REPAIR_ACTIONS)}
    for row in rows:
        drive_id = str(row["drive_id"])
        issue_list = _string_list(
            row["issue_types_json"],
            f"Fehlerarten für {drive_id}/{row['segment_id']}",
            allow_empty=False,
        )
        unknown = set(issue_list) - ISSUE_TYPES
        if unknown:
            raise ValueError(
                f"Unbekannte Fehlerart für {drive_id}/{row['segment_id']}: "
                + ", ".join(sorted(unknown))
            )
        flags = _string_list(
            row["flags_json"],
            f"Prüfsignale für {drive_id}/{row['segment_id']}",
            allow_empty=False,
        )
        start = row["segment_start"]
        end = row["segment_end"]
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not isfinite(float(start))
            or not isfinite(float(end))
            or float(start) < 0
            or float(end) < float(start)
        ):
            raise ValueError(f"Ungültige Segmentzeit für {drive_id}/{row['segment_id']}")
        action = _repair_action(
            set(issue_list),
            row["confirmed_text"],
            row["repair_curation_action"],
        )
        action_counts[action] += 1

        transcript_value = row["transcript_path"]
        if not isinstance(transcript_value, str) or not transcript_value.strip():
            raise ValueError(f"Transkriptpfad fehlt für {drive_id}")
        transcript_path = Path(transcript_value)
        if not transcript_path.is_file():
            raise ValueError(f"Lokales Transkript fehlt für {drive_id}: {transcript_path}")
        recording = grouped.get(drive_id)
        if recording is None:
            recording = {
                "drive_id": drive_id,
                "transcript_path": str(transcript_path),
                "transcript_size": transcript_path.stat().st_size,
                "transcript_sha256": file_sha256(transcript_path),
                "published": (
                    {
                        "remote_path": row["remote_path"],
                        "remote_id": row["remote_id"],
                        "published_at": row["published_at"],
                    }
                    if row["remote_path"] is not None
                    else None
                ),
                "reviews": [],
            }
            grouped[drive_id] = recording
        elif recording["transcript_path"] != str(transcript_path):
            raise ValueError(f"Widersprüchliche Transkriptpfade für {drive_id}")

        decision_hash = row["decision_transcript_sha256"]
        if decision_hash is None:
            version_status = "UNVERIFIED"
        elif (
            not isinstance(decision_hash, str)
            or len(decision_hash) != 64
            or any(character not in "0123456789abcdef" for character in decision_hash)
        ):
            raise ValueError(
                f"Ungültiger Entscheidungs-Hash für {drive_id}/{row['segment_id']}"
            )
        elif decision_hash == recording["transcript_sha256"]:
            version_status = "MATCHED"
        else:
            version_status = "MISMATCH"

        recording["reviews"].append({
            "segment_id": row["segment_id"],
            "candidate_hash": row["candidate_hash"],
            "segment_start": float(start),
            "segment_end": float(end),
            "segment_text": row["segment_text"],
            "flags": flags,
            "issue_types": issue_list,
            "note": row["note"],
            "confirmed_text": row["confirmed_text"],
            "curation_action": row["repair_curation_action"],
            "speaker_intent": row["speaker_intent"],
            "decision_transcript_sha256": decision_hash,
            "transcript_version_status": version_status,
            "action": action,
        })

    recordings = list(grouped.values())
    recording_counts = {status: 0 for status in sorted(RECORDING_STATUSES)}
    review_version_counts = {
        status: 0 for status in ("MATCHED", "MISMATCH", "UNVERIFIED")
    }
    recording_version_counts = dict(review_version_counts)
    for recording in recordings:
        status = _recording_status(review["action"] for review in recording["reviews"])
        recording["status"] = status
        recording_counts[status] += 1
        review_versions = {
            review["transcript_version_status"] for review in recording["reviews"]
        }
        for review in recording["reviews"]:
            review_version_counts[review["transcript_version_status"]] += 1
        if "MISMATCH" in review_versions:
            version_status = "MISMATCH"
        elif "UNVERIFIED" in review_versions:
            version_status = "UNVERIFIED"
        else:
            version_status = "MATCHED"
        recording["transcript_version_status"] = version_status
        recording_version_counts[version_status] += 1
    return {
        "schema_version": 2,
        "plan_type": "confirmed_transcript_repair_dry_run",
        "summary": {
            "recording_count": len(recordings),
            "review_count": len(rows),
            "review_action_counts": action_counts,
            "recording_status_counts": recording_counts,
            "review_version_counts": review_version_counts,
            "recording_version_counts": recording_version_counts,
        },
        "recordings": recordings,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bestätigte QA-Entscheidungen rein lesend zu einem "
            "Transkript-Reparaturplan zusammenführen."
        )
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--drive-id", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def print_plan(plan: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False))
        return
    summary = plan["summary"]
    print(
        "Reparatur-Dry-Run: "
        f"Aufnahmen={summary['recording_count']} "
        f"Entscheidungen={summary['review_count']} "
        f"Aktionen={summary['review_action_counts']}"
    )
    for recording in plan["recordings"]:
        print(
            f"{recording['status']} id={recording['drive_id']} "
            f"version={recording['transcript_version_status']} "
            f"transcript={recording['transcript_path']}"
        )
        for review in recording["reviews"]:
            print(
                f"  {review['action']} segment={review['segment_id']} "
                f"version={review['transcript_version_status']} "
                f"issues={','.join(review['issue_types'])}"
            )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with open_readonly_state(args.state) as db:
            plan = build_repair_plan(db, set(args.drive_id) or None)
        print_plan(plan, args.json)
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"[X] Reparaturplan fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
