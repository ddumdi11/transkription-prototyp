#!/usr/bin/env python3
"""Bind legacy confirmed QA decisions to verified transcript versions."""

from __future__ import annotations

import argparse
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
import sys
from typing import Any

from analyze_segment_quality import find_suspicious_segments
from quality_reviews import candidate_hash, open_readonly_state
from segment_metadata import SegmentMetadataError, load_segment_metadata


DEFAULT_STATE = Path(".inbox-watcher/state.sqlite3")
BINDING_STATUSES = ("ELIGIBLE", "ALREADY_BOUND", "HOLD")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def legacy_candidate_hash(candidate: dict[str, Any]) -> str:
    """Reproduce the fingerprint used before transcript-version binding."""
    density = candidate.get("chars_per_second")
    material = {
        "id": candidate["id"],
        "start": float(candidate["start"]),
        "end": float(candidate["end"]),
        "raw_text": candidate["raw_text"],
        "text": candidate["text"],
        "flags": sorted(set(candidate["flags"])),
        "chars_per_second": float(density) if density is not None else None,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_schema(db: sqlite3.Connection) -> None:
    required = {"quality_reviews", "transcription_jobs", "published_transcripts"}
    present = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(required - present)
    if missing:
        raise ValueError(
            "Pipeline-Datenbank enthält nicht alle benötigten Tabellen: "
            + ", ".join(missing)
        )
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(quality_reviews)")
    }
    required_columns = {"candidate_transcript_sha256", "transcript_sha256"}
    missing_columns = sorted(required_columns - columns)
    if missing_columns:
        raise ValueError(
            "QA-Tabelle enthält nicht alle Versionsspalten: "
            + ", ".join(missing_columns)
        )


def confirmed_rows(
    db: sqlite3.Connection, selected_ids: set[str] | None
) -> list[sqlite3.Row]:
    clauses = ["q.is_current=1", "q.status='CONFIRMED'"]
    values: list[str] = []
    if selected_ids:
        placeholders = ",".join("?" for _ in selected_ids)
        clauses.append(f"q.drive_id IN ({placeholders})")
        values.extend(sorted(selected_ids))
    return list(db.execute(
        f"""SELECT q.*, j.transcript_path,
                   p.local_path AS published_local_path,
                   p.local_hash AS published_local_hash,
                   p.size AS published_size, p.published_at,
                   p.remote_path, p.remote_id
            FROM quality_reviews q
            LEFT JOIN transcription_jobs j ON j.drive_id=q.drive_id
            LEFT JOIN published_transcripts p ON p.drive_id=q.drive_id
            WHERE {' AND '.join(clauses)}
            ORDER BY q.drive_id, q.segment_start, q.segment_id""",
        values,
    ))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _recording_evidence(rows: list[sqlite3.Row]) -> dict[str, Any]:
    first = rows[0]
    drive_id = str(first["drive_id"])
    reasons: list[str] = []
    transcript_value = first["transcript_path"]
    transcript = (
        Path(transcript_value)
        if isinstance(transcript_value, str) and transcript_value.strip()
        else None
    )
    if transcript is None:
        reasons.append("transcript_path_missing")
    elif not transcript.is_file():
        reasons.append("transcript_missing")

    transcript_exists = transcript is not None and transcript.is_file()
    transcript_sha256 = file_sha256(transcript) if transcript_exists else None
    transcript_size = transcript.stat().st_size if transcript_exists else None
    published_path = first["published_local_path"]
    if transcript is not None and published_path != str(transcript):
        reasons.append("published_path_mismatch")
    published_size = first["published_size"]
    if transcript_size is not None and published_size != transcript_size:
        reasons.append("published_size_mismatch")
    published_hash = first["published_local_hash"]
    if not valid_sha256(published_hash):
        reasons.append("published_hash_missing_or_invalid")
    elif transcript_sha256 != published_hash:
        reasons.append("published_hash_mismatch")

    published_at = _number(first["published_at"])
    if published_at is None:
        reasons.append("published_at_missing_or_invalid")

    sidecar = transcript.with_suffix(".segments.json") if transcript else None
    payload: dict[str, Any] | None = None
    candidates: dict[str, dict[str, Any]] = {}
    if sidecar is None or not sidecar.is_file():
        reasons.append("segment_file_missing")
    else:
        try:
            payload = load_segment_metadata(sidecar, expected_source_id=drive_id)
            detected = find_suspicious_segments(payload)
            candidates = {str(item["id"]): item for item in detected}
            if len(candidates) != len(detected):
                reasons.append("duplicate_detected_segment_id")
        except (OSError, SegmentMetadataError, ValueError) as exc:
            reasons.append(f"segment_file_invalid:{exc}")

    if payload is not None:
        transcript_file = payload.get("transcript_file")
        if transcript is not None and transcript_file != transcript.name:
            reasons.append("segment_transcript_name_mismatch")
        embedded_hash = payload.get("transcript_sha256")
        if embedded_hash is not None and embedded_hash != transcript_sha256:
            reasons.append("segment_transcript_hash_mismatch")

    return {
        "drive_id": drive_id,
        "transcript_path": str(transcript) if transcript else None,
        "segment_path": str(sidecar) if sidecar else None,
        "transcript_sha256": transcript_sha256,
        "transcript_size": transcript_size,
        "published_local_path": published_path,
        "published_local_hash": published_hash,
        "published_size": published_size,
        "published_at": published_at,
        "remote_path": first["remote_path"],
        "remote_id": first["remote_id"],
        "transcript_mtime": (
            transcript.stat().st_mtime if transcript_exists else None
        ),
        "segment_mtime": (
            sidecar.stat().st_mtime
            if sidecar is not None and sidecar.is_file()
            else None
        ),
        "common_reasons": reasons,
        "candidates": candidates,
    }


def _review_plan(row: sqlite3.Row, evidence: dict[str, Any]) -> dict[str, Any]:
    reasons = list(evidence["common_reasons"])
    decided_at = _number(row["decided_at"])
    if decided_at is None:
        reasons.append("decided_at_missing_or_invalid")
    else:
        published_at = evidence["published_at"]
        if published_at is not None and published_at > decided_at:
            reasons.append("publication_after_decision")
        transcript_mtime = evidence["transcript_mtime"]
        if transcript_mtime is not None and transcript_mtime > decided_at:
            reasons.append("transcript_modified_after_decision")
        segment_mtime = evidence["segment_mtime"]
        if segment_mtime is not None and segment_mtime > decided_at:
            reasons.append("segment_file_modified_after_decision")

    segment_id = str(row["segment_id"])
    candidate = evidence["candidates"].get(segment_id)
    if candidate is None:
        reasons.append("detector_candidate_missing")

    source_hash = row["candidate_transcript_sha256"]
    decision_hash = row["transcript_sha256"]
    current_hash = evidence["transcript_sha256"]
    legacy = source_hash is None and decision_hash is None
    already_bound = source_hash == current_hash and decision_hash == current_hash
    if not legacy and not already_bound:
        reasons.append("partial_or_conflicting_binding")

    target_candidate_hash = None
    if candidate is not None and valid_sha256(current_hash):
        target_candidate_hash = candidate_hash(candidate, current_hash)
        expected_existing_hash = (
            legacy_candidate_hash(candidate) if legacy else target_candidate_hash
        )
        if row["candidate_hash"] != expected_existing_hash:
            reasons.append("candidate_fingerprint_mismatch")

    if reasons:
        status = "HOLD"
    elif legacy:
        status = "ELIGIBLE"
    else:
        status = "ALREADY_BOUND"
    return {
        "segment_id": segment_id,
        "candidate_hash": row["candidate_hash"],
        "target_candidate_hash": target_candidate_hash,
        "decided_at": decided_at,
        "status": status,
        "reasons": reasons,
    }


def _plan_hash(plan: dict[str, Any]) -> str:
    encoded = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_binding_plan(
    db: sqlite3.Connection, selected_ids: set[str] | None = None
) -> dict[str, Any]:
    """Build a deterministic, non-mutating migration plan."""
    require_schema(db)
    requested = set(selected_ids or ())
    if any(not isinstance(value, str) or not value.strip() for value in requested):
        raise ValueError("Drive-ID darf nicht leer sein")
    rows = confirmed_rows(db, requested or None)
    found_ids = {str(row["drive_id"]) for row in rows}
    missing = sorted(requested - found_ids)
    if missing:
        raise ValueError(
            "Keine aktuelle bestätigte QA-Entscheidung für Drive-ID: "
            + ", ".join(missing)
        )

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["drive_id"]), []).append(row)

    recordings: list[dict[str, Any]] = []
    review_counts = {status: 0 for status in BINDING_STATUSES}
    recording_counts = dict(review_counts)
    for drive_id in sorted(grouped):
        evidence = _recording_evidence(grouped[drive_id])
        reviews = [_review_plan(row, evidence) for row in grouped[drive_id]]
        for review in reviews:
            review_counts[review["status"]] += 1
        statuses = {review["status"] for review in reviews}
        if "HOLD" in statuses:
            status = "HOLD"
        elif "ELIGIBLE" in statuses:
            status = "ELIGIBLE"
        else:
            status = "ALREADY_BOUND"
        recording_counts[status] += 1
        evidence.pop("candidates")
        evidence.pop("common_reasons")
        recordings.append({**evidence, "status": status, "reviews": reviews})

    plan = {
        "schema_version": 1,
        "plan_type": "legacy_quality_review_version_binding",
        "summary": {
            "recording_count": len(recordings),
            "review_count": len(rows),
            "recording_status_counts": recording_counts,
            "review_status_counts": review_counts,
        },
        "recordings": recordings,
    }
    plan["plan_sha256"] = _plan_hash(plan)
    return plan


def apply_binding_plan(db: sqlite3.Connection, plan: dict[str, Any]) -> int:
    """Apply one already verified plan inside the caller's transaction."""
    holds = [row for row in plan["recordings"] if row["status"] == "HOLD"]
    if holds:
        raise ValueError("Bindungsplan enthält HOLD-Fälle")
    changed = 0
    for recording in plan["recordings"]:
        if recording["status"] == "ALREADY_BOUND":
            continue
        current_hash = recording["transcript_sha256"]
        for review in recording["reviews"]:
            if review["status"] != "ELIGIBLE":
                continue
            cursor = db.execute(
                """UPDATE quality_reviews
                   SET candidate_hash=?, candidate_transcript_sha256=?,
                       transcript_sha256=?
                   WHERE drive_id=? AND segment_id=? AND candidate_hash=?
                     AND is_current=1 AND status='CONFIRMED'
                     AND candidate_transcript_sha256 IS NULL
                     AND transcript_sha256 IS NULL""",
                (
                    review["target_candidate_hash"],
                    current_hash,
                    current_hash,
                    recording["drive_id"],
                    review["segment_id"],
                    review["candidate_hash"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "QA-Entscheidung wurde während der Bindung verändert"
                )
            changed += 1
    return changed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Alte bestätigte QA-Entscheidungen zunächst rein lesend auf eine "
            "sicher belegbare Transkriptversion prüfen."
        )
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--drive-id", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--confirm-plan-sha256",
        metavar="SHA256",
        help="Exakt diesen zuvor angezeigten Plan atomar bestätigen",
    )
    return parser.parse_args(argv)


def print_plan(plan: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False))
        return
    summary = plan["summary"]
    print(
        "Legacy-Bindungsplan: "
        f"Aufnahmen={summary['recording_count']} "
        f"Entscheidungen={summary['review_count']} "
        f"Status={summary['review_status_counts']}"
    )
    print(f"Plan-SHA256: {plan['plan_sha256']}")
    for recording in plan["recordings"]:
        print(
            f"{recording['status']} id={recording['drive_id']} "
            f"transcript={recording['transcript_path']}"
        )
        for review in recording["reviews"]:
            detail = ",".join(review["reasons"]) or "-"
            print(
                f"  {review['status']} segment={review['segment_id']} "
                f"reason={detail}"
            )


def _open_writable_state(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"Pipeline-Datenbank fehlt: {path}")
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = set(args.drive_id) or None
    try:
        if args.confirm_plan_sha256 is None:
            with open_readonly_state(args.state) as db:
                plan = build_binding_plan(db, selected)
            print_plan(plan, args.json)
            return 0

        if not valid_sha256(args.confirm_plan_sha256):
            raise ValueError("Bestätigter Plan-SHA-256 ist ungültig")
        with _open_writable_state(args.state) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                plan = build_binding_plan(db, selected)
                if plan["plan_sha256"] != args.confirm_plan_sha256:
                    raise ValueError(
                        "Bindungsplan hat sich seit dem Dry-Run geändert"
                    )
                changed = apply_binding_plan(db, plan)
                db.commit()
            except Exception:
                db.rollback()
                raise
        if args.json:
            result = dict(plan)
            result["binding_result"] = {"changed_reviews": changed}
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            print_plan(plan, False)
            print(f"Legacy-Bindung gespeichert: {changed} Entscheidung(en)")
        return 0
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"[X] Legacy-Bindung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
