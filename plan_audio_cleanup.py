#!/usr/bin/env python3
"""Build a read-only evidence and retention plan for archived WAV cleanup."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from math import isfinite
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

from convert_audio_archive import (
    DEFAULT_OUTPUT,
    archive_key,
    existing_package_matches,
    file_digest,
)
from plan_audio_archive import (
    DEFAULT_ARCHIVE_TARGET,
    DEFAULT_STATE,
    build_plan,
    open_readonly_state,
    setup_cli_logging,
)
from upload_audio_archive import (
    LIST_TIMEOUT_SECONDS,
    RECEIPT_NAME,
    build_upload_plan,
    inspect_remote_package,
    normalized_remote_hashes,
    parse_listing,
    read_existing_receipt,
    run_rclone,
    validate_existing_receipt_binding,
    validate_remote_root,
    verify_remote_package,
)


DEFAULT_SOURCE = os.environ.get("AUDIOREC_SOURCE")
LOCAL_RETENTION_ENV = "AUDIOREC_LOCAL_RETENTION_DAYS"
REMOTE_RETENTION_ENV = "AUDIOREC_REMOTE_RETENTION_DAYS"


def parse_utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} fehlt")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{label} ist kein ISO-8601-Zeitpunkt") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} muss ausdrücklich in UTC angegeben sein")
    return parsed.astimezone(timezone.utc)


def validate_retention_days(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    if not isfinite(value) or value < 0:
        raise ValueError(f"{label} muss eine nichtnegative endliche Zahl sein")
    return value


def resolve_retention_days(
    cli_value: float | None, env_name: str, label: str
) -> float | None:
    """Resolve a CLI override or an optional environment policy value."""
    if cli_value is not None:
        return validate_retention_days(cli_value, label)
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{label} in {env_name} ist keine Zahl") from exc
    return validate_retention_days(value, f"{label} in {env_name}")


def source_remote_index(source: str) -> dict[str, dict[str, Any]]:
    normalized, _root_id = validate_remote_root(source)
    rows = parse_listing(
        run_rclone(
            ["rclone", "lsjson", normalized, "--files-only", "--hash"],
            LIST_TIMEOUT_SECONDS,
        ),
        "die Live-Inbox",
    )
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        drive_id = row.get("ID")
        if not isinstance(drive_id, str) or not drive_id:
            raise ValueError("Datei ohne Drive-ID in der Live-Inbox")
        if drive_id in indexed:
            raise ValueError(f"Doppelte Drive-ID in der Live-Inbox: {drive_id}")
        indexed[drive_id] = row
    return indexed


def verify_source_remote(item: dict[str, Any], row: object) -> None:
    if not isinstance(row, dict):
        raise ValueError("Quell-WAV ist nicht mehr in der Live-Inbox sichtbar")
    try:
        size = int(row.get("Size", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError("Remote-Größe der Quell-WAV ist ungültig") from exc
    if size != item["source_size"]:
        raise ValueError("Remote-Größe der Quell-WAV weicht vom Pipeline-State ab")
    expected_hash = str(item["source_hash"]).lower()
    hash_type = str(item["source_hash_type"]).lower()
    if normalized_remote_hashes(row).get(hash_type) != expected_hash:
        raise ValueError("Remote-Hash der Quell-WAV weicht vom Pipeline-State ab")


def verify_local_source(item: dict[str, Any]) -> None:
    if not item.get("local_audio"):
        raise ValueError("Lokaler Quellpfad fehlt")
    path = Path(item["local_audio"])
    if not path.is_file():
        raise ValueError("Lokale Quell-WAV fehlt")
    if path.stat().st_size != item["source_size"]:
        raise ValueError("Lokale Quell-WAV hat eine abweichende Größe")
    if file_digest(path, str(item["source_hash_type"])) != str(
        item["source_hash"]
    ).lower():
        raise ValueError("Lokale Quell-WAV hat einen abweichenden Hash")


def verify_receipt_files(
    receipt: object, upload_plan: dict[str, Any]
) -> tuple[datetime, list[dict[str, Any]]]:
    path = Path(upload_plan["receipt_file"])
    validate_existing_receipt_binding(receipt, upload_plan, path)
    if not isinstance(receipt, dict):
        raise ValueError("Upload-Quittung ist kein JSON-Objekt")
    rows = receipt["files"]
    if not isinstance(rows, list):
        raise ValueError("Upload-Quittung enthält keine Dateiliste")
    expected = upload_plan["files"]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Upload-Quittung enthält keinen gültigen Dateieintrag")
        name = row.get("remote_name")
        if not isinstance(name, str) or name not in expected or name in seen:
            raise ValueError(
                f"Upload-Quittung enthält einen ungültigen Dateinamen: {name!r}"
            )
        remote_id = row.get("remote_id")
        if not isinstance(remote_id, str) or not remote_id:
            raise ValueError(f"Remote-ID fehlt in der Upload-Quittung: {name}")
        details = expected[name]
        if row.get("size") != details["size"]:
            raise ValueError(f"Größe weicht in der Upload-Quittung ab: {name}")
        if row.get("sha256") != details["sha256"]:
            raise ValueError(f"SHA256 weicht in der Upload-Quittung ab: {name}")
        seen.add(name)
        normalized.append({
            "remote_name": name,
            "remote_id": remote_id,
            "size": details["size"],
            "sha256": details["sha256"],
        })
    normalized.sort(key=lambda row: row["remote_name"])
    if seen != set(expected):
        raise ValueError("Upload-Quittung enthält nicht alle Archivdateien")
    if rows != normalized:
        raise ValueError("Upload-Quittung ist nicht kanonisch oder enthält Zusatzfelder")
    verified_at = parse_utc_timestamp(receipt.get("verified_at"), "verified_at")
    return verified_at, normalized


def retention_blockers(
    configured_days: float | None,
    age_days: float | None,
    prefix: str,
) -> list[str]:
    if configured_days is None:
        return [f"{prefix}_retention_unconfigured"]
    if age_days is None:
        return ["archive_verification_time_invalid"]
    if age_days < configured_days:
        return [f"{prefix}_retention_not_elapsed"]
    return []


def conversion_evidence_plan(
    item: dict[str, Any], output_root: Path
) -> dict[str, Any]:
    """Describe a previously created package without requiring the WAV locally."""
    package_dir = output_root / Path(item["archive_name"]).stem
    return {
        "archive_key": archive_key(item),
        "drive_id": item["drive_id"],
        "package_dir": str(package_dir),
        "archive_file": str(package_dir / item["archive_name"]),
        "metadata_file": str(package_dir / "archive.json"),
        "source_size": item["source_size"],
        "source_hash_type": item["source_hash_type"],
        "source_hash": item["source_hash"],
    }


def plan_cleanup_one(
    item: dict[str, Any],
    output_root: Path,
    archive_target: str | None,
    source_rows: dict[str, dict[str, Any]] | None,
    verify_remote: bool,
    local_retention_days: float | None,
    remote_retention_days: float | None,
    as_of: datetime,
) -> dict[str, Any]:
    common_blockers: list[str] = []
    local_only_blockers: list[str] = []
    evidence: dict[str, Any] = {
        "local_source_verified": False,
        "local_archive_verified": False,
        "upload_receipt_verified": False,
        "archive_remote_reverified": False,
        "source_remote_reverified": False,
    }
    verified_at: datetime | None = None
    age_days: float | None = None
    package_dir = output_root / Path(item["archive_name"]).stem
    receipt_path = package_dir / RECEIPT_NAME

    local_archive_reasons = {
        "local_audio_path_missing",
        "local_audio_missing",
        "local_audio_empty",
        "local_size_mismatch",
        "local_audio_not_wav",
    }
    for blocker in item["archive_blockers"]:
        target = (
            local_only_blockers if blocker in local_archive_reasons
            else common_blockers
        )
        target.append(f"archive_{blocker}")

    conversion_plan = conversion_evidence_plan(item, output_root)
    if not existing_package_matches(conversion_plan):
        common_blockers.append("local_archive_missing_or_invalid")
    else:
        evidence["local_archive_verified"] = True
        if archive_target is None:
            common_blockers.append("archive_target_unconfigured")
        else:
            try:
                upload_plan = build_upload_plan(conversion_plan, archive_target)
                receipt = read_existing_receipt(receipt_path)
                if receipt is None:
                    raise ValueError("Upload-Quittung fehlt")
                verified_at, receipt_files = verify_receipt_files(
                    receipt, upload_plan
                )
                evidence["upload_receipt_verified"] = True
                if verified_at > as_of:
                    raise ValueError("verified_at liegt nach dem Planungszeitpunkt")
                age_days = (as_of - verified_at).total_seconds() / 86400

                if verify_remote:
                    remote = inspect_remote_package(
                        upload_plan["target"], upload_plan["package_name"]
                    )
                    if remote is None:
                        raise ValueError("Remote-Archivpaket fehlt")
                    remote_files = verify_remote_package(
                        upload_plan["files"], remote
                    )
                    if remote_files != receipt_files:
                        raise ValueError(
                            "Remote-Datei-IDs weichen von der Upload-Quittung ab"
                        )
                    evidence["archive_remote_reverified"] = True
            except (
                FileNotFoundError,
                FileExistsError,
                OSError,
                RuntimeError,
                subprocess.TimeoutExpired,
                ValueError,
            ) as exc:
                common_blockers.append("archive_upload_evidence_invalid")
                evidence["archive_upload_detail"] = str(exc)

    try:
        verify_local_source(item)
        evidence["local_source_verified"] = True
    except (OSError, ValueError) as exc:
        local_only_blockers.append("local_source_mismatch")
        evidence["local_source_detail"] = str(exc)

    if item["quality"]["status"] != "CLEAR":
        common_blockers.append(f"quality_{item['quality']['status'].lower()}")
    if not verify_remote:
        common_blockers.append("archive_remote_not_reverified")

    local_blockers = list(dict.fromkeys(common_blockers + local_only_blockers))
    local_blockers.extend(
        retention_blockers(local_retention_days, age_days, "local")
    )

    remote_blockers = list(dict.fromkeys(common_blockers))
    remote_blockers.extend(
        retention_blockers(remote_retention_days, age_days, "remote")
    )
    if verify_remote:
        try:
            verify_source_remote(item, (source_rows or {}).get(item["drive_id"]))
            evidence["source_remote_reverified"] = True
        except ValueError as exc:
            remote_blockers.append("source_remote_evidence_invalid")
            evidence["source_remote_detail"] = str(exc)
    else:
        remote_blockers.append("source_remote_not_reverified")

    local_blockers = list(dict.fromkeys(local_blockers))
    remote_blockers = list(dict.fromkeys(remote_blockers))
    return {
        "archive_key": conversion_plan["archive_key"],
        "drive_id": item["drive_id"],
        "source_path": item["source_path"],
        "local_audio": item["local_audio"],
        "source_size": item["source_size"],
        "source_hash_type": item["source_hash_type"],
        "source_hash": item["source_hash"],
        "archive_package": str(package_dir),
        "upload_receipt": str(receipt_path),
        "quality_status": item["quality"]["status"],
        "archive_verified_at": verified_at.isoformat() if verified_at else None,
        "archive_age_days": round(age_days, 6) if age_days is not None else None,
        "evidence": evidence,
        "local_policy_status": "ELIGIBLE" if not local_blockers else "HOLD",
        "local_blockers": local_blockers,
        "remote_policy_status": "ELIGIBLE" if not remote_blockers else "HOLD",
        "remote_blockers": remote_blockers,
        "cleanup_ready": False,
        "action": None,
    }


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "planned_items": len(items),
        "local_policy_eligible": sum(
            item["local_policy_status"] == "ELIGIBLE" for item in items
        ),
        "remote_policy_eligible": sum(
            item["remote_policy_status"] == "ELIGIBLE" for item in items
        ),
        "cleanup_ready": 0,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archivnachweise und Aufbewahrungsfristen rein lesend planen"
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--archive-target", default=DEFAULT_ARCHIVE_TARGET)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--drive-id", action="append", default=[])
    parser.add_argument("--local-retention-days", type=float)
    parser.add_argument("--remote-retention-days", type=float)
    parser.add_argument("--verify-remote", action="store_true")
    parser.add_argument("--as-of", help="Reproduzierbarer UTC-Zeitpunkt")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_cli_logging()
    try:
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
        as_of = (
            parse_utc_timestamp(args.as_of, "--as-of")
            if args.as_of
            else datetime.now(timezone.utc)
        )
        archive_target = None
        if args.archive_target:
            archive_target, _archive_id = validate_remote_root(args.archive_target)
        source_rows = None
        if args.verify_remote:
            if archive_target is None:
                raise ValueError("--verify-remote benötigt --archive-target")
            if not args.source:
                raise ValueError("--verify-remote benötigt --source")
            source_rows = source_remote_index(args.source)

        selected = set(args.drive_id) or None
        with open_readonly_state(args.state) as db:
            archive_items = build_plan(
                db, archive_target=archive_target, selected_ids=selected
            )
        items = [
            plan_cleanup_one(
                item,
                args.archive_dir,
                archive_target,
                source_rows,
                args.verify_remote,
                local_days,
                remote_days,
                as_of,
            )
            for item in archive_items
            if item["archive_status"] == "CANDIDATE"
            or (args.archive_dir / Path(item["archive_name"]).stem).exists()
        ]
        result = {
            "dry_run": True,
            "as_of": as_of.isoformat(),
            "verify_remote": args.verify_remote,
            "local_retention_days": local_days,
            "remote_retention_days": remote_days,
            "summary": summarize(items),
            "items": items,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            summary = result["summary"]
            logger.info(
                "Bereinigungsplan: items=%d local_eligible=%d remote_eligible=%d "
                "cleanup_ready=0 (keine Änderungen)",
                summary["planned_items"],
                summary["local_policy_eligible"],
                summary["remote_policy_eligible"],
            )
            if args.verbose:
                for item in items:
                    logger.info(
                        "local=%s remote=%s path=%r id=%s local_blockers=%s "
                        "remote_blockers=%s",
                        item["local_policy_status"], item["remote_policy_status"],
                        item["source_path"], item["drive_id"],
                        ",".join(item["local_blockers"]) or "-",
                        ",".join(item["remote_blockers"]) or "-",
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
        logger.error("Bereinigungsplanung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
