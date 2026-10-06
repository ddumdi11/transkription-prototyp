#!/usr/bin/env python3
"""Explicitly upload and verify one local FLAC archive package on Drive."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from math import isfinite
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any

from convert_audio_archive import (
    DEFAULT_OUTPUT,
    archive_lock,
    existing_package_matches,
    file_digest,
    fsync_directory,
    select_conversion_plan,
)
from plan_audio_archive import DEFAULT_ARCHIVE_TARGET, DEFAULT_STATE, setup_cli_logging


RECEIPT_NAME = "upload-receipt.json"
LIST_TIMEOUT_SECONDS = 120.0
UPLOAD_STARTUP_SECONDS = 120.0
MIN_UPLOAD_BYTES_PER_SECOND = 64 * 1024
REMOTE_NAME_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
REMOTE_NAME_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def validate_remote_root(target: str | None) -> tuple[str, str]:
    """Require one ID-pinned rclone remote root and return it with its Drive ID."""
    if not isinstance(target, str) or not target.strip():
        raise ValueError(
            "AUDIOREC_ARCHIVE_TARGET fehlt; erwartet wird "
            "'gdrive,root_folder_id=DRIVE_ORDNER_ID:'"
        )
    normalized = target.strip().rstrip("/")
    if any(character in normalized for character in ("\n", "\r", "\0")):
        raise ValueError("Archivziel enthält ungültige Steuerzeichen")
    if not normalized.endswith(":") or "/" in normalized:
        raise ValueError(
            "Archivziel muss ein per Drive-ID fixierter rclone-Root ohne Unterpfad sein"
        )
    parts = normalized[:-1].split(",")
    if not parts[0] or ":" in parts[0]:
        raise ValueError("Archivziel enthält keinen gültigen rclone-Remote-Namen")
    root_ids = [
        part.split("=", 1)[1]
        for part in parts[1:]
        if part.startswith("root_folder_id=") and "=" in part
    ]
    if len(root_ids) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", root_ids[0]):
        raise ValueError("Archivziel benötigt genau eine gültige root_folder_id")
    return normalized, root_ids[0]


def portable_remote_name(name: str) -> str:
    """Return one component that remains usable after a Windows download."""
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise ValueError("Leerer oder ungültiger Archivname")
    result = REMOTE_NAME_UNSAFE.sub("_", name).rstrip(" .")
    if not result or result in {".", ".."}:
        raise ValueError(f"Archivname ist nicht plattformneutral: {name!r}")
    stem = result.split(".", 1)[0].upper()
    if stem in REMOTE_NAME_RESERVED:
        result = f"_{result}"
    return result


def remote_join(root: str, *components: str) -> str:
    suffix = "/".join(components)
    return f"{root}{suffix}"


def transfer_timeout(size: int, configured: float | None = None) -> float:
    if configured is not None:
        if not isfinite(configured) or configured <= 0:
            raise ValueError("Der rclone-Timeout muss eine positive Zahl sein")
        return configured
    if not isinstance(size, int) or size < 0:
        raise ValueError("Upload-Größe ist ungültig")
    return max(
        LIST_TIMEOUT_SECONDS,
        UPLOAD_STARTUP_SECONDS + size / MIN_UPLOAD_BYTES_PER_SECOND,
    )


def run_rclone(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f": {detail}" if detail else ""
        raise RuntimeError(f"rclone fehlgeschlagen{message}") from exc


def parse_listing(result: subprocess.CompletedProcess[str], context: str) -> list[dict]:
    try:
        rows = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"rclone-Ausgabe ist für {context} kein JSON") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"rclone-Ausgabe ist für {context} keine Objektliste")
    return rows


def inspect_remote_package(
    target: str, package_name: str
) -> dict[str, dict[str, Any]] | None:
    directories = parse_listing(
        run_rclone(
            ["rclone", "lsjson", target, "--dirs-only"], LIST_TIMEOUT_SECONDS
        ),
        "den Archivordner",
    )
    matches = [row for row in directories if row.get("Path") == package_name]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            f"Mehrdeutiger Remote-Paketordner {package_name!r}: {len(matches)} Treffer"
        )
    if not matches[0].get("ID"):
        raise ValueError(f"Remote-ID des Paketordners fehlt: {package_name}")

    package_target = remote_join(target, package_name)
    rows = parse_listing(
        run_rclone(
            ["rclone", "lsjson", package_target, "--files-only", "--hash"],
            LIST_TIMEOUT_SECONDS,
        ),
        f"das Paket {package_name!r}",
    )
    files: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("Path")
        if not isinstance(name, str) or not name or "/" in name:
            raise ValueError(f"Ungültiger Remote-Dateiname im Paket: {name!r}")
        if name in files:
            raise ValueError(f"Doppelte Remote-Datei im Paket: {name!r}")
        files[name] = row
    return files


def expected_remote_files(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    archive_file = Path(plan["archive_file"])
    metadata_file = Path(plan["metadata_file"])
    names = [
        portable_remote_name(archive_file.name),
        portable_remote_name(metadata_file.name),
    ]
    if len(set(names)) != len(names):
        raise ValueError("Remote-Dateinamen des Archivpakets kollidieren")
    return {
        names[0]: {
            "local_path": archive_file,
            "size": archive_file.stat().st_size,
            "sha256": file_digest(archive_file, "sha256"),
        },
        names[1]: {
            "local_path": metadata_file,
            "size": metadata_file.stat().st_size,
            "sha256": file_digest(metadata_file, "sha256"),
        },
    }


def build_upload_plan(
    conversion_plan: dict[str, Any], target: str | None
) -> dict[str, Any]:
    normalized_target, target_id = validate_remote_root(target)
    if not existing_package_matches(conversion_plan):
        raise ValueError(
            "Lokales FLAC-Paket fehlt, ist unvollständig oder stimmt nicht mit dem Plan überein"
        )
    package_dir = Path(conversion_plan["package_dir"])
    package_name = portable_remote_name(package_dir.name)
    files = expected_remote_files(conversion_plan)
    return {
        "archive_key": conversion_plan["archive_key"],
        "drive_id": conversion_plan["drive_id"],
        "target": normalized_target,
        "target_root_id": target_id,
        "package_name": package_name,
        "package_dir": str(package_dir),
        "remote_package": remote_join(normalized_target, package_name),
        "receipt_file": str(package_dir / RECEIPT_NAME),
        "files": {
            name: {
                "local_path": str(details["local_path"]),
                "remote_path": remote_join(normalized_target, package_name, name),
                "size": details["size"],
                "sha256": details["sha256"],
            }
            for name, details in files.items()
        },
        "source_preserved": True,
        "cleanup_ready": False,
    }


def select_upload_plan(
    state: Path, drive_id: str, output_root: Path, target: str | None
) -> dict[str, Any]:
    conversion_plan = select_conversion_plan(state, drive_id, output_root)
    return build_upload_plan(conversion_plan, target)


def normalized_remote_hashes(row: dict[str, Any]) -> dict[str, str]:
    hashes = row.get("Hashes") or {}
    if not isinstance(hashes, dict):
        return {}
    return {
        str(name).lower(): str(value).lower()
        for name, value in hashes.items()
        if value
    }


def verify_remote_file(
    name: str, expected: dict[str, Any], remote: dict[str, Any]
) -> dict[str, Any]:
    try:
        remote_size = int(remote.get("Size", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Remote-Größe ist ungültig: {name}") from exc
    if remote_size != expected["size"]:
        raise ValueError(f"Remote-Größe weicht ab: {name}")
    if normalized_remote_hashes(remote).get("sha256") != expected["sha256"]:
        raise ValueError(f"Remote-SHA256 weicht ab: {name}")
    remote_id = remote.get("ID")
    if not isinstance(remote_id, str) or not remote_id:
        raise ValueError(f"Remote-ID fehlt: {name}")
    return {
        "remote_name": name,
        "remote_id": remote_id,
        "size": expected["size"],
        "sha256": expected["sha256"],
    }


def verify_remote_package(
    expected: dict[str, dict[str, Any]], remote: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    unknown = sorted(set(remote) - set(expected))
    missing = sorted(set(expected) - set(remote))
    if unknown:
        raise ValueError("Unerwartete Remote-Datei(en): " + ", ".join(unknown))
    if missing:
        raise ValueError("Remote-Datei(en) fehlen: " + ", ".join(missing))
    return [
        verify_remote_file(name, expected[name], remote[name])
        for name in sorted(expected)
    ]


def receipt_payload(
    plan: dict[str, Any], verified_files: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "receipt_type": "verified_drive_flac_archive",
        "archive_key": plan["archive_key"],
        "source_drive_id": plan["drive_id"],
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "target_root_id": plan["target_root_id"],
        "remote_package": plan["package_name"],
        "files": verified_files,
        "source_preserved": True,
        "cleanup_ready": False,
    }


def receipt_matches(
    existing: object, plan: dict[str, Any], verified_files: list[dict[str, Any]]
) -> bool:
    if not isinstance(existing, dict):
        return False
    return (
        existing.get("schema_version") == 1
        and existing.get("receipt_type") == "verified_drive_flac_archive"
        and existing.get("archive_key") == plan["archive_key"]
        and existing.get("source_drive_id") == plan["drive_id"]
        and existing.get("target_root_id") == plan["target_root_id"]
        and existing.get("remote_package") == plan["package_name"]
        and existing.get("files") == verified_files
        and existing.get("source_preserved") is True
        and existing.get("cleanup_ready") is False
        and isinstance(existing.get("verified_at"), str)
    )


def validate_existing_receipt_binding(
    existing: object | None, plan: dict[str, Any], path: Path
) -> None:
    if existing is None:
        return
    if not isinstance(existing, dict):
        raise FileExistsError(f"Upload-Quittung ist kein JSON-Objekt: {path}")
    expected = {
        "schema_version": 1,
        "receipt_type": "verified_drive_flac_archive",
        "archive_key": plan["archive_key"],
        "source_drive_id": plan["drive_id"],
        "target_root_id": plan["target_root_id"],
        "remote_package": plan["package_name"],
        "source_preserved": True,
        "cleanup_ready": False,
    }
    mismatched = [
        field for field, value in expected.items() if existing.get(field) != value
    ]
    if mismatched:
        raise FileExistsError(
            f"Upload-Quittung gehört nicht zu diesem Plan ({', '.join(mismatched)}): "
            f"{path}"
        )
    if not isinstance(existing.get("verified_at"), str) or not isinstance(
        existing.get("files"), list
    ):
        raise FileExistsError(f"Upload-Quittung ist unvollständig: {path}")


def read_existing_receipt(path: Path) -> object | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise FileExistsError(f"Upload-Quittung ist keine Datei: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Upload-Quittung ist ungültig: {path}") from exc


def conversion_plan_for_verification(plan: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the minimal v0.2 plan from the immutable local manifest."""
    metadata_entries = [
        details for details in plan["files"].values()
        if Path(details["local_path"]).name == "archive.json"
    ]
    archive_entries = [
        details for details in plan["files"].values()
        if Path(details["local_path"]).suffix.lower() == ".flac"
    ]
    if len(metadata_entries) != 1 or len(archive_entries) != 1:
        raise ValueError("Lokaler Uploadplan enthält kein eindeutiges FLAC-Paket")
    metadata_path = Path(metadata_entries[0]["local_path"])
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        source = payload["source"]
        if not isinstance(source, dict):
            raise TypeError("source ist kein Objekt")
        source_size = int(source["size"])
        hash_type = str(source["hash_type"])
        content_hash = str(source["content_hash"])
    except (
        KeyError, OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError
    ) as exc:
        raise ValueError(f"Lokales Archivmanifest ist ungültig: {metadata_path}") from exc
    return {
        "archive_key": plan["archive_key"],
        "drive_id": plan["drive_id"],
        "package_dir": plan["package_dir"],
        "archive_file": archive_entries[0]["local_path"],
        "metadata_file": str(metadata_path),
        "source_size": source_size,
        "source_hash_type": hash_type,
        "source_hash": content_hash,
    }


def create_verified_upload_snapshot(
    expected: dict[str, dict[str, Any]], parent: Path
) -> tuple[tempfile.TemporaryDirectory[str], dict[str, Path]]:
    """Copy exactly the planned bytes to a private temporary upload input."""
    temporary = tempfile.TemporaryDirectory(
        prefix=".archive-upload.", suffix=".tmp", dir=parent
    )
    root = Path(temporary.name)
    snapshots: dict[str, Path] = {}
    try:
        for name, details in expected.items():
            source = Path(details["local_path"])
            snapshot = root / name
            shutil.copyfile(source, snapshot)
            if snapshot.stat().st_size != details["size"]:
                raise ValueError(f"Lokale Upload-Größe hat sich geändert: {name}")
            if file_digest(snapshot, "sha256") != details["sha256"]:
                raise ValueError(f"Lokaler Upload-Hash hat sich geändert: {name}")
            snapshots[name] = snapshot
    except Exception:
        temporary.cleanup()
        raise
    return temporary, snapshots


def install_receipt(path: Path, payload: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    installed = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        installed = True
        fsync_directory(path.parent)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if installed:
            path.unlink(missing_ok=True)
            try:
                fsync_directory(path.parent)
            except OSError:
                pass
        raise


def upload_one(
    plan: dict[str, Any], timeout_seconds: float | None = None
) -> tuple[Path, list[str], bool]:
    """Upload missing package files, verify them, and persist a local receipt."""
    package_dir = Path(plan["package_dir"])
    receipt_path = Path(plan["receipt_file"])
    expected = plan["files"]
    uploaded: list[str] = []

    with archive_lock(package_dir):
        conversion_plan = conversion_plan_for_verification(plan)
        if not existing_package_matches(conversion_plan):
            raise ValueError("Lokales FLAC-Paket wurde seit dem Dry-Run verändert")

        snapshot_dir, snapshots = create_verified_upload_snapshot(
            expected, package_dir.parent
        )
        try:
            return _upload_verified_snapshot(
                plan, expected, receipt_path, snapshots, uploaded, timeout_seconds
            )
        finally:
            snapshot_dir.cleanup()


def _upload_verified_snapshot(
    plan: dict[str, Any],
    expected: dict[str, dict[str, Any]],
    receipt_path: Path,
    snapshots: dict[str, Path],
    uploaded: list[str],
    timeout_seconds: float | None,
) -> tuple[Path, list[str], bool]:
    """Perform remote changes only from already verified immutable snapshots."""
    existing_receipt = read_existing_receipt(receipt_path)
    validate_existing_receipt_binding(existing_receipt, plan, receipt_path)

    remote = inspect_remote_package(plan["target"], plan["package_name"])
    if remote is None:
        run_rclone(
            ["rclone", "mkdir", plan["remote_package"]], LIST_TIMEOUT_SECONDS
        )
        remote = inspect_remote_package(plan["target"], plan["package_name"])
        if remote is None:
            raise RuntimeError("Remote-Paketordner fehlt nach rclone mkdir")

    unknown = sorted(set(remote) - set(expected))
    if unknown:
        raise ValueError("Unerwartete Remote-Datei(en): " + ", ".join(unknown))
    for name, row in remote.items():
        verify_remote_file(name, expected[name], row)

    for name in sorted(set(expected) - set(remote)):
        details = expected[name]
        run_rclone(
            [
                "rclone", "copyto", str(snapshots[name]),
                details["remote_path"], "--immutable",
            ],
            transfer_timeout(details["size"], timeout_seconds),
        )
        uploaded.append(name)

    final_remote = inspect_remote_package(plan["target"], plan["package_name"])
    if final_remote is None:
        raise RuntimeError("Remote-Paketordner ist nach dem Upload nicht sichtbar")
    verified_files = verify_remote_package(expected, final_remote)
    if existing_receipt is not None:
        if not receipt_matches(existing_receipt, plan, verified_files):
            raise FileExistsError(
                f"Abweichende Upload-Quittung existiert bereits: {receipt_path}"
            )
        return receipt_path, uploaded, False

    install_receipt(receipt_path, receipt_payload(plan, verified_files))
    return receipt_path, uploaded, True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ein lokales FLAC-Paket ausdrücklich auf Drive hochladen und prüfen"
    )
    parser.add_argument("--drive-id", required=True)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--target", default=DEFAULT_ARCHIVE_TARGET)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_cli_logging()
    try:
        plan = select_upload_plan(
            args.state, args.drive_id, args.output_dir, args.target
        )
        if not args.confirm:
            if args.json:
                print(json.dumps({"dry_run": True, "plan": plan}, ensure_ascii=False, indent=2))
            else:
                logger.info(
                    "Drive-Archiv-Dry-Run id=%s local=%r remote=%r",
                    plan["drive_id"], plan["package_dir"], plan["remote_package"],
                )
                logger.info("Kein Upload. Mit --confirm ausdrücklich bestätigen.")
            return 0

        receipt, uploaded, receipt_created = upload_one(plan, args.timeout_seconds)
        result = {
            "dry_run": False,
            "uploaded": uploaded,
            "receipt_created": receipt_created,
            "receipt_file": str(receipt),
            "source_preserved": True,
            "cleanup_ready": False,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            logger.info(
                "Drive-Archiv verifiziert id=%s upload=%s receipt=%r WAV=unverändert",
                plan["drive_id"], ",".join(uploaded) or "bereits vorhanden",
                str(receipt),
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
        logger.error("Drive-FLAC-Archivierung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
