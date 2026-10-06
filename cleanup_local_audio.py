#!/usr/bin/env python3
"""Explicitly remove one retained local WAV after fresh archive verification."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from typing import Any

from dotenv import load_dotenv

from convert_audio_archive import (
    DEFAULT_OUTPUT,
    archive_lock,
    file_digest,
    fsync_directory,
)
from plan_audio_archive import (
    DEFAULT_STATE,
    build_plan,
    open_readonly_state,
    setup_cli_logging,
)
from plan_audio_cleanup import (
    LOCAL_RETENTION_ENV,
    REMOTE_RETENTION_ENV,
    parse_utc_timestamp,
    plan_cleanup_one,
    resolve_retention_days,
    source_remote_index,
)
from upload_audio_archive import validate_remote_root


RECEIPT_NAME = "local-cleanup.json"
PENDING_SUFFIX = ".local-wav-removal.pending"
DEFAULT_ENV_FILE = Path(".inbox-watcher/pipeline.env")
RESUMABLE_LOCAL_BLOCKERS = {
    "archive_local_audio_path_missing",
    "archive_local_audio_missing",
    "archive_local_audio_empty",
    "archive_local_size_mismatch",
    "archive_local_audio_not_wav",
    "local_source_mismatch",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pending_path_for(source_path: Path) -> Path:
    """Keep the atomic pending move on the source filesystem."""
    return source_path.with_name(f".{source_path.name}{PENDING_SUFFIX}")


def build_local_cleanup_plan(
    state: Path,
    archive_dir: Path,
    archive_target: str | None,
    source: str | None,
    drive_id: str,
    local_retention_days: float,
    remote_retention_days: float | None,
    as_of: datetime,
) -> dict[str, Any]:
    """Build one current plan with mandatory live verification of both remotes."""
    if not archive_target:
        raise ValueError("Archivziel fehlt")
    if not source:
        raise ValueError("Live-Inbox-Quelle fehlt")
    normalized_target, _target_id = validate_remote_root(archive_target)
    source_rows = source_remote_index(source)
    with open_readonly_state(state) as db:
        items = build_plan(
            db, archive_target=normalized_target, selected_ids={drive_id}
        )
    if len(items) != 1:
        raise ValueError(
            f"Drive-ID muss genau einen Pipeline-Eintrag treffen: {drive_id}"
        )
    return plan_cleanup_one(
        items[0],
        archive_dir,
        normalized_target,
        source_rows,
        True,
        local_retention_days,
        remote_retention_days,
        as_of,
    )


def required_live_evidence(plan: dict[str, Any]) -> bool:
    evidence = plan.get("evidence")
    return isinstance(evidence, dict) and all(
        evidence.get(name) is True
        for name in (
            "local_archive_verified",
            "upload_receipt_verified",
            "archive_remote_reverified",
            "source_remote_reverified",
        )
    )


def ready_for_new_cleanup(plan: dict[str, Any]) -> bool:
    evidence = plan.get("evidence")
    return (
        plan.get("local_policy_status") == "ELIGIBLE"
        and isinstance(evidence, dict)
        and evidence.get("local_source_verified") is True
        and required_live_evidence(plan)
    )


def ready_to_resume(plan: dict[str, Any]) -> bool:
    blockers = set(plan.get("local_blockers") or [])
    return (
        plan.get("quality_status") == "CLEAR"
        and required_live_evidence(plan)
        and blockers.issubset(RESUMABLE_LOCAL_BLOCKERS)
    )


def cleanup_binding(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "archive_key": plan["archive_key"],
        "source_drive_id": plan["drive_id"],
        "source": {
            "local_file": plan["local_audio"],
            "size": plan["source_size"],
            "hash_type": plan["source_hash_type"],
            "content_hash": plan["source_hash"],
        },
        "archive_package": plan["archive_package"],
    }


def read_cleanup_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Lokale Bereinigungsquittung ist unlesbar: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Lokale Bereinigungsquittung ist kein JSON-Objekt")
    return payload


def validate_cleanup_receipt(
    payload: dict[str, Any], plan: dict[str, Any], path: Path
) -> None:
    expected = cleanup_binding(plan)
    if (
        payload.get("schema_version") != 1
        or payload.get("receipt_type") != "verified_local_wav_cleanup"
        or payload.get("status") not in {"PREPARED", "REMOVED"}
        or payload.get("confirmation") != "explicit_cli"
        or any(payload.get(name) != value for name, value in expected.items())
    ):
        raise FileExistsError(
            f"Bereinigungsquittung passt nicht zum aktuellen Plan: {path}"
        )


def write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise ValueError(f"Zielverzeichnis der Quittung fehlt: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def verify_cleanup_source(path: Path, plan: dict[str, Any]) -> None:
    if not path.is_file():
        raise ValueError(f"Lokale Quell-WAV fehlt: {path}")
    if path.stat().st_size != plan["source_size"]:
        raise ValueError("Lokale Quell-WAV hat eine abweichende Größe")
    if file_digest(path, str(plan["source_hash_type"])) != str(
        plan["source_hash"]
    ).lower():
        raise ValueError("Lokale Quell-WAV hat einen abweichenden Hash")


def prepared_payload(
    plan: dict[str, Any],
    local_retention_days: float,
    remote_retention_days: float | None,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "receipt_type": "verified_local_wav_cleanup",
        "status": "PREPARED",
        "confirmation": "explicit_cli",
        **cleanup_binding(plan),
        "prepared_at": now.isoformat(),
        "removed_at": None,
        "policy": {
            "local_retention_days": local_retention_days,
            "remote_retention_days": remote_retention_days,
            "archive_verified_at": plan["archive_verified_at"],
            "evaluated_at": now.isoformat(),
        },
        "live_evidence": {
            name: plan["evidence"][name]
            for name in (
                "local_archive_verified",
                "upload_receipt_verified",
                "archive_remote_reverified",
                "source_remote_reverified",
            )
        },
    }


def remove_local_wav(
    plan: dict[str, Any],
    local_retention_days: float,
    remote_retention_days: float | None,
    now: datetime | None = None,
) -> tuple[Path, bool]:
    """Remove one verified WAV, resuming safely after an interrupted attempt."""
    moment = now or utc_now()
    package_dir = Path(plan["archive_package"])
    receipt_path = package_dir / RECEIPT_NAME
    source_path = Path(plan["local_audio"])
    pending_path = pending_path_for(source_path)
    receipt = read_cleanup_receipt(receipt_path)

    if receipt is not None:
        validate_cleanup_receipt(receipt, plan, receipt_path)
        if receipt["status"] == "REMOVED":
            if source_path.exists() or pending_path.exists():
                raise FileExistsError(
                    "Abgeschlossene Bereinigungsquittung widerspricht lokalen Dateien"
                )
            return receipt_path, False
        if not ready_to_resume(plan):
            raise ValueError(
                "Unterbrochene Bereinigung kann ohne gültige Live-Nachweise "
                "nicht fortgesetzt werden"
            )
    else:
        if not ready_for_new_cleanup(plan):
            raise ValueError("Lokale WAV ist nach aktueller Richtlinie nicht löschbar")
        receipt = prepared_payload(
            plan, local_retention_days, remote_retention_days, moment
        )
        write_json_durable(receipt_path, receipt)

    source_exists = source_path.exists()
    pending_exists = pending_path.exists()
    if source_exists and pending_exists:
        raise FileExistsError("Quell-WAV und Bereinigungs-Zwischendatei existieren beide")
    if source_exists:
        verify_cleanup_source(source_path, plan)
        os.replace(source_path, pending_path)
        fsync_directory(source_path.parent)
        pending_exists = True
    if pending_exists:
        verify_cleanup_source(pending_path, plan)
        pending_path.unlink()
        fsync_directory(pending_path.parent)

    receipt["status"] = "REMOVED"
    receipt["removed_at"] = moment.isoformat()
    write_json_durable(receipt_path, receipt)
    return receipt_path, True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Eine lokale WAV nach frischer Archivprüfung ausdrücklich entfernen"
    )
    parser.add_argument("--drive-id", required=True)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--archive-target")
    parser.add_argument("--source")
    parser.add_argument("--local-retention-days", type=float)
    parser.add_argument("--remote-retention-days", type=float)
    parser.add_argument("--as-of", help="Reproduzierbarer UTC-Zeitpunkt nur im Dry-Run")
    parser.add_argument("--confirm-local-cleanup", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_cli_logging()
    try:
        load_dotenv(args.env_file, override=False)
        archive_target = args.archive_target or os.environ.get(
            "AUDIOREC_ARCHIVE_TARGET"
        )
        source = args.source or os.environ.get("AUDIOREC_SOURCE")
        local_days = resolve_retention_days(
            args.local_retention_days,
            LOCAL_RETENTION_ENV,
            "Lokale Aufbewahrungsfrist",
        )
        remote_days = resolve_retention_days(
            args.remote_retention_days,
            REMOTE_RETENTION_ENV,
            "Remote-Aufbewahrungsfrist",
        )
        if local_days is None:
            raise ValueError("Lokale Aufbewahrungsfrist ist nicht konfiguriert")
        if args.confirm_local_cleanup and args.as_of:
            raise ValueError("--as-of ist bei einer echten Bereinigung nicht erlaubt")
        as_of = parse_utc_timestamp(args.as_of, "--as-of") if args.as_of else utc_now()
        plan = build_local_cleanup_plan(
            args.state,
            args.archive_dir,
            archive_target,
            source,
            args.drive_id,
            local_days,
            remote_days,
            as_of,
        )
        execution_ready = ready_for_new_cleanup(plan)
        result: dict[str, Any] = {
            "dry_run": not args.confirm_local_cleanup,
            "execution_ready": execution_ready,
            "action": "remove_local_wav" if execution_ready else None,
            "plan": plan,
        }
        if args.confirm_local_cleanup:
            package_dir = Path(plan["archive_package"])
            with archive_lock(package_dir):
                fresh_plan = build_local_cleanup_plan(
                    args.state,
                    args.archive_dir,
                    archive_target,
                    source,
                    args.drive_id,
                    local_days,
                    remote_days,
                    utc_now(),
                )
                receipt, removed = remove_local_wav(
                    fresh_plan, local_days, remote_days
                )
            result.update({
                "dry_run": False,
                "removed": removed,
                "receipt": str(receipt),
                "source_present": Path(fresh_plan["local_audio"]).exists(),
                "plan": fresh_plan,
            })
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result["dry_run"]:
            logger.info(
                "Lokaler Bereinigungs-Dry-Run id=%s status=%s action=%s "
                "(keine Änderungen)",
                args.drive_id,
                plan["local_policy_status"],
                result["action"] or "-",
            )
        else:
            logger.info(
                "Lokale WAV %s id=%s receipt=%r",
                "entfernt" if result["removed"] else "bereits entfernt",
                args.drive_id,
                result["receipt"],
            )
        return 0
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        logger.error("Lokale Bereinigung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
