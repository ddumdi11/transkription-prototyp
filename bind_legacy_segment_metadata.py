#!/usr/bin/env python3
"""Bind legacy segment sidecars to one verified transcript version."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from typing import Any

from quality_reviews import open_readonly_state
from segment_metadata import SegmentMetadataError, load_segment_metadata


DEFAULT_STATE = Path(".inbox-watcher/state.sqlite3")
PLAN_TYPE = "legacy_segment_metadata_binding"
PLAN_SCHEMA_VERSION = 1
STATUSES = ("ELIGIBLE", "ALREADY_BOUND", "HOLD")


def file_sha256(path: Path) -> str:
    """Return the lowercase SHA-256 digest of one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_sha256(value: object) -> bool:
    """Return whether value is one lowercase hexadecimal SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def normalized_text(value: str) -> str:
    """Normalize only whitespace for a conservative reconstruction check."""
    return " ".join(value.split())


def transcript_body(transcript_text: str) -> tuple[str, bool]:
    """Return the canonical body and whether the header declares LLM correction."""
    header, separator, body = transcript_text.partition("\n\n")
    lines = header.splitlines()
    expected_prefixes = (
        "# Aufnahme",
        "- Datei:",
        "- Datum:",
        "- Modell:",
        "- Quelle:",
        "- Kontext:",
        "- Status:",
    )
    if not separator or len(lines) != len(expected_prefixes) or any(
        not line.startswith(prefix)
        for line, prefix in zip(lines, expected_prefixes)
    ):
        raise ValueError("Transkript besitzt keinen erwarteten Metadatenkopf")
    return body, "LLM-korrigiert" in lines[-1]


def reconstructed_body(payload: dict[str, Any]) -> str:
    """Reconstruct the transcript body from deterministic segment text."""
    segments = payload["segments"]
    if not segments:
        raise ValueError("Segmentdatei enthält keine Segmente")
    return " ".join(
        segment["text"].strip()
        for segment in segments
        if segment["text"].strip()
    )


def require_schema(db: sqlite3.Connection) -> None:
    """Require the pipeline tables that provide publication evidence."""
    required = {"transcription_jobs", "published_transcripts"}
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


def transcript_rows(
    db: sqlite3.Connection, selected_ids: set[str] | None
) -> list[sqlite3.Row]:
    """Load deterministic transcript and publication evidence rows."""
    clauses: list[str] = []
    values: list[str] = []
    if selected_ids:
        placeholders = ",".join("?" for _ in selected_ids)
        clauses.append(f"j.drive_id IN ({placeholders})")
        values.extend(sorted(selected_ids))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return list(db.execute(
        f"""SELECT j.drive_id, j.status AS job_status, j.transcript_path,
                   p.local_path AS published_local_path,
                   p.local_hash AS published_local_hash,
                   p.size AS published_size, p.remote_path, p.remote_id,
                   p.published_at
            FROM transcription_jobs j
            LEFT JOIN published_transcripts p ON p.drive_id=j.drive_id
            {where}
            ORDER BY j.drive_id""",
        values,
    ))


def binding_record(row: sqlite3.Row) -> dict[str, Any]:
    """Classify one sidecar using transcript, publication and text evidence."""
    drive_id = str(row["drive_id"])
    reasons: list[str] = []
    transcript_value = row["transcript_path"]
    transcript = (
        Path(transcript_value)
        if isinstance(transcript_value, str) and transcript_value.strip()
        else None
    )
    if row["job_status"] != "DONE":
        reasons.append("transcription_not_done")
    if transcript is None:
        reasons.append("transcript_path_missing")
    elif not transcript.is_file():
        reasons.append("transcript_missing")

    transcript_hash: str | None = None
    transcript_size: int | None = None
    transcript_text: str | None = None
    if transcript is not None and transcript.is_file():
        transcript_hash = file_sha256(transcript)
        transcript_size = transcript.stat().st_size
        try:
            transcript_text = transcript.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            reasons.append(f"transcript_unreadable:{exc}")

    if transcript is not None and row["published_local_path"] != str(transcript):
        reasons.append("published_path_mismatch")
    if transcript_size is not None and row["published_size"] != transcript_size:
        reasons.append("published_size_mismatch")
    published_hash = row["published_local_hash"]
    if not valid_sha256(published_hash):
        reasons.append("published_hash_missing_or_invalid")
    elif published_hash != transcript_hash:
        reasons.append("published_hash_mismatch")
    if not isinstance(row["remote_id"], str) or not row["remote_id"]:
        reasons.append("published_remote_id_missing")
    if not isinstance(row["remote_path"], str) or not row["remote_path"]:
        reasons.append("published_remote_path_missing")
    published_at = row["published_at"]
    if (
        isinstance(published_at, bool)
        or not isinstance(published_at, (int, float))
        or not isfinite(float(published_at))
    ):
        reasons.append("published_at_missing_or_invalid")

    sidecar = transcript.with_suffix(".segments.json") if transcript else None
    sidecar_hash: str | None = None
    sidecar_size: int | None = None
    sidecar_mtime_ns: int | None = None
    payload: dict[str, Any] | None = None
    if sidecar is None or not sidecar.is_file():
        reasons.append("segment_file_missing")
    else:
        sidecar_stat = sidecar.stat()
        sidecar_hash = file_sha256(sidecar)
        sidecar_size = sidecar_stat.st_size
        sidecar_mtime_ns = sidecar_stat.st_mtime_ns
        try:
            payload = load_segment_metadata(sidecar, expected_source_id=drive_id)
        except (OSError, SegmentMetadataError, ValueError) as exc:
            reasons.append(f"segment_file_invalid:{exc}")

    embedded_hash: object = None
    if payload is not None:
        if (
            transcript is not None
            and payload.get("transcript_file") != transcript.name
        ):
            reasons.append("segment_transcript_name_mismatch")
        embedded_hash = payload.get("transcript_sha256")
        if embedded_hash is not None and embedded_hash != transcript_hash:
            reasons.append("segment_transcript_hash_mismatch")
        if transcript_text is not None:
            try:
                body, llm_corrected = transcript_body(transcript_text)
                if llm_corrected:
                    reasons.append("llm_corrected_transcript")
                if normalized_text(body) != normalized_text(
                    reconstructed_body(payload)
                ):
                    reasons.append("segment_text_reconstruction_mismatch")
            except ValueError as exc:
                reasons.append(f"transcript_reconstruction_invalid:{exc}")

    if reasons:
        status = "HOLD"
    elif embedded_hash is None:
        status = "ELIGIBLE"
    else:
        status = "ALREADY_BOUND"
    return {
        "drive_id": drive_id,
        "status": status,
        "reasons": reasons,
        "transcript_path": str(transcript) if transcript else None,
        "segment_path": str(sidecar) if sidecar else None,
        "transcript_sha256": transcript_hash,
        "transcript_size": transcript_size,
        "segment_sha256": sidecar_hash,
        "segment_size": sidecar_size,
        "segment_mtime_ns": sidecar_mtime_ns,
        "published_local_hash": published_hash,
        "published_size": row["published_size"],
        "remote_path": row["remote_path"],
        "remote_id": row["remote_id"],
        "published_at": published_at,
        "embedded_transcript_sha256": embedded_hash,
    }


def canonical_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the complete plan material except its own digest field."""
    material = dict(plan)
    material.pop("plan_sha256", None)
    return material


def plan_sha256(plan: dict[str, Any]) -> str:
    """Hash one plan deterministically without hashing the digest itself."""
    encoded = json.dumps(
        canonical_plan(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_binding_plan(
    db: sqlite3.Connection,
    selected_ids: set[str] | None = None,
    eligible_only: bool = False,
) -> dict[str, Any]:
    """Build a deterministic and non-mutating legacy-sidecar binding plan."""
    require_schema(db)
    requested = set(selected_ids or ())
    if any(not isinstance(value, str) or not value.strip() for value in requested):
        raise ValueError("Drive-ID darf nicht leer sein")
    rows = transcript_rows(db, requested or None)
    found = {str(row["drive_id"]) for row in rows}
    missing = sorted(requested - found)
    if missing:
        raise ValueError("Unbekannte Drive-ID(s): " + ", ".join(missing))

    observed = [binding_record(row) for row in rows]
    observed_counts = {status: 0 for status in STATUSES}
    for recording in observed:
        observed_counts[recording["status"]] += 1
    recordings = (
        [row for row in observed if row["status"] == "ELIGIBLE"]
        if eligible_only
        else observed
    )
    counts = {status: 0 for status in STATUSES}
    for recording in recordings:
        counts[recording["status"]] += 1
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_type": PLAN_TYPE,
        "selection": {
            "eligible_only": eligible_only,
            "requested_drive_ids": sorted(requested),
        },
        "summary": {
            "recording_count": len(recordings),
            "status_counts": counts,
            "observed_recording_count": len(observed),
            "observed_status_counts": observed_counts,
        },
        "recordings": recordings,
    }
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def upgraded_payload(payload: dict[str, Any], transcript_hash: str) -> dict[str, Any]:
    """Insert the binding next to transcript_file while preserving all fields."""
    result: dict[str, Any] = {}
    inserted = False
    for key, value in payload.items():
        result[key] = value
        if key == "transcript_file":
            result["transcript_sha256"] = transcript_hash
            inserted = True
    if not inserted:
        raise ValueError("Segmentdatei enthält keinen Transkriptdateinamen")
    return result


def write_payload_atomic(
    path: Path,
    payload: dict[str, Any],
    expected_sha256: str,
    expected_mtime_ns: int,
) -> None:
    """Durably replace one unchanged sidecar and preserve its timestamps."""
    if file_sha256(path) != expected_sha256:
        raise RuntimeError("Segmentdatei wurde während der Bindung verändert")
    original_stat = path.stat()
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, stat.S_IMODE(original_stat.st_mode))
        os.utime(
            temporary_name,
            ns=(original_stat.st_atime_ns, expected_mtime_ns),
        )
        with open(temporary_name, "rb") as temporary:
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def apply_binding_plan(plan: dict[str, Any]) -> int:
    """Apply an already revalidated plan, atomically per eligible sidecar."""
    holds = [row for row in plan["recordings"] if row["status"] == "HOLD"]
    if holds:
        raise ValueError("Bindungsplan enthält HOLD-Fälle")
    prepared: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
    for recording in plan["recordings"]:
        if recording["status"] == "ALREADY_BOUND":
            continue
        transcript = Path(recording["transcript_path"])
        if file_sha256(transcript) != recording["transcript_sha256"]:
            raise RuntimeError("Transkript wurde während der Bindung verändert")
        path = Path(recording["segment_path"])
        if file_sha256(path) != recording["segment_sha256"]:
            raise RuntimeError("Segmentdatei wurde während der Bindung verändert")
        payload = load_segment_metadata(
            path, expected_source_id=recording["drive_id"]
        )
        if payload.get("transcript_sha256") is not None:
            raise RuntimeError("Segmentdatei wurde bereits anderweitig gebunden")
        updated = upgraded_payload(payload, recording["transcript_sha256"])
        prepared.append((recording, path, updated))

    changed = 0
    for recording, path, updated in prepared:
        write_payload_atomic(
            path,
            updated,
            recording["segment_sha256"],
            recording["segment_mtime_ns"],
        )
        changed += 1
    return changed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse dry-run or explicitly hash-confirmed binding arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Alte Segmentdateien zunächst rein lesend einer belegten "
            "Transkriptversion zuordnen"
        )
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--drive-id", action="append", default=[])
    parser.add_argument(
        "--eligible-only",
        action="store_true",
        help=(
            "Nur nach vollständiger Prüfung ELIGIBLE-Fälle in den Plan "
            "aufnehmen; beobachtete HOLD-Zahlen bleiben hash-gebunden"
        ),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--confirm-plan-sha256",
        metavar="SHA256",
        help="Exakt diesen zuvor angezeigten Plan bestätigen",
    )
    return parser.parse_args(argv)


def print_plan(plan: dict[str, Any], as_json: bool) -> None:
    """Print a machine-readable or compact human-readable plan."""
    if as_json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False))
        return
    summary = plan["summary"]
    print(
        "Legacy-Segmentbindungsplan: "
        f"Aufnahmen={summary['recording_count']} "
        f"Status={summary['status_counts']}"
    )
    print(f"Plan-SHA256: {plan['plan_sha256']}")
    for recording in plan["recordings"]:
        detail = ",".join(recording["reasons"]) or "-"
        print(
            f"{recording['status']} id={recording['drive_id']} "
            f"reason={detail}"
        )


def confirmation_lock(state: Path):
    """Open the persistent cross-process lock used only by confirmed runs."""
    if not state.is_file():
        raise ValueError(f"Pipeline-Datenbank fehlt: {state}")
    handle = (state.parent / "legacy-segment-metadata.lock").open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def main(argv: list[str] | None = None) -> int:
    """Run a read-only plan or bind the exact revalidated confirmed plan."""
    args = parse_args(argv)
    selected = set(args.drive_id) or None
    try:
        if args.confirm_plan_sha256 is None:
            with open_readonly_state(args.state) as db:
                plan = build_binding_plan(
                    db, selected, eligible_only=args.eligible_only
                )
            print_plan(plan, args.json)
            return 0

        if not valid_sha256(args.confirm_plan_sha256):
            raise ValueError("Bestätigter Plan-SHA-256 ist ungültig")
        with confirmation_lock(args.state):
            with open_readonly_state(args.state) as db:
                plan = build_binding_plan(
                    db, selected, eligible_only=args.eligible_only
                )
            if plan["plan_sha256"] != args.confirm_plan_sha256:
                raise ValueError("Bindungsplan hat sich seit dem Dry-Run geändert")
            changed = apply_binding_plan(plan)
        if args.json:
            result = dict(plan)
            result["binding_result"] = {"changed_sidecars": changed}
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            print_plan(plan, False)
            print(f"Legacy-Segmentbindung gespeichert: {changed} Datei(en)")
        return 0
    except (
        OSError,
        RuntimeError,
        SegmentMetadataError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"[X] Legacy-Segmentbindung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
