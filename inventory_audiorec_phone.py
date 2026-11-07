#!/usr/bin/env python3
"""Inventory AudioRec WAV files without modifying the connected phone."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unicodedata
import uuid
import wave
from typing import Any, Iterable


SCHEMA_VERSION = 1
REPORT_TYPE = "audiorec_phone_inventory"
DEFAULT_STATE = Path(".inbox-watcher/audiorec-inventory.sqlite3")
DEFAULT_REPORT = Path("staging/audiorec-inventory/latest-report.json")
DEFAULT_DEVICE_ID = "audiorec-android-primary"
DEFAULT_EPOCH_ID = "epoch-1"
DEFAULT_COVERAGE_FROM = "2025-12-01T00:00:00+01:00"
STANDARD_NAME = re.compile(r"^Aufnahme #(\d+)\.wav$")
TRAILING_NUMBER = re.compile(r"(\d+)(?=\.wav$)", re.IGNORECASE)


def setup_cli_logging() -> logging.Logger:
    """Return a stderr-only logger without creating local artifacts."""
    logger = logging.getLogger("audiorec_phone_inventory")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    """Serialize one aware datetime as an ISO-8601 UTC value."""
    if value.tzinfo is None:
        raise ValueError("Zeitwert benötigt eine Zeitzone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_iso_datetime(value: str, field: str) -> str:
    """Validate an ISO-8601 datetime and return its original representation."""
    if not value or not value.strip():
        raise ValueError(f"{field} darf nicht leer sein")
    candidate = value.strip()
    parsed_value = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
    try:
        parsed = datetime.fromisoformat(parsed_value)
    except ValueError as exc:
        raise ValueError(f"{field} ist kein gültiger ISO-Zeitwert: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} benötigt eine Zeitzone: {value}")
    return candidate


def _is_below(path: Path, root: Path) -> bool:
    """Return whether path resolves to root or one of its descendants."""
    resolved_path = path.expanduser().resolve(strict=False)
    resolved_root = root.expanduser().resolve(strict=False)
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def validate_paths(source: Path, state: Path, report: Path | None) -> None:
    """Require a readable directory and keep every output outside the phone."""
    if not source.is_dir():
        raise ValueError(f"AudioRec-Quellordner fehlt oder ist kein Ordner: {source}")
    if _is_below(state, source):
        raise ValueError("Inventardatenbank darf nicht im Smartphone-Quellordner liegen")
    if report is not None and _is_below(report, source):
        raise ValueError("Inventarbericht darf nicht im Smartphone-Quellordner liegen")


def inspect_wave_header(path: Path) -> dict[str, Any]:
    """Read only the WAV header and return technical audio properties."""
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
            sample_width_bits = audio.getsampwidth() * 8
            compression = audio.getcomptype()
        if channels <= 0 or sample_rate <= 0 or frame_count < 0:
            raise ValueError("ungültige WAV-Parameter")
        if compression != "NONE":
            raise ValueError(f"komprimierter WAV-Typ wird nicht erwartet: {compression}")
        return {
            "audio_status": "VALID",
            "audio_error": None,
            "duration_seconds": frame_count / sample_rate,
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width_bits": sample_width_bits,
        }
    except (EOFError, OSError, ValueError, wave.Error) as exc:
        return {
            "audio_status": "INVALID",
            "audio_error": str(exc),
            "duration_seconds": None,
            "sample_rate": None,
            "channels": None,
            "sample_width_bits": None,
        }


def classify_name(name: str) -> dict[str, Any]:
    """Classify a WAV name while treating parsed numbers as non-unique labels."""
    standard = STANDARD_NAME.fullmatch(name)
    trailing = TRAILING_NUMBER.search(name)
    return {
        "normalized_name": unicodedata.normalize("NFC", name),
        "name_status": "STANDARD" if standard else "ANOMALOUS_PATTERN",
        "recording_number": int(standard.group(1)) if standard else None,
        "trailing_number": int(trailing.group(1)) if trailing else None,
    }


def scan_source(source: Path, inspect_audio: bool = False) -> tuple[list[dict[str, Any]], int]:
    """Return a flat, read-only inventory of WAVs and a count of ignored files."""
    items: list[dict[str, Any]] = []
    ignored = 0
    with os.scandir(source) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                ignored += 1
                continue
            if Path(entry.name).suffix.lower() != ".wav":
                ignored += 1
                continue
            stat = entry.stat(follow_symlinks=False)
            item: dict[str, Any] = {
                "relative_path": entry.name,
                "original_name": entry.name,
                "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "modified_at": iso_utc(
                    datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                ),
                **classify_name(entry.name),
            }
            if inspect_audio:
                item.update(inspect_wave_header(Path(entry.path)))
            else:
                item.update({
                    "audio_status": "NOT_CHECKED",
                    "audio_error": None,
                    "duration_seconds": None,
                    "sample_rate": None,
                    "channels": None,
                    "sample_width_bits": None,
                })
            items.append(item)
    items.sort(key=lambda item: (
        int(item["modified_ns"]), str(item["normalized_name"])
    ))
    return items, ignored


def provisional_inventory_id(
    device_id: str, epoch_id: str, item: dict[str, Any]
) -> str:
    """Derive a stable provisional ID from source metadata, not audio content."""
    material = "\0".join((
        device_id,
        epoch_id,
        str(item["relative_path"]),
        str(item["size_bytes"]),
        str(item["modified_ns"]),
    )).encode("utf-8")
    return "inv-" + hashlib.sha256(material).hexdigest()


def open_inventory_state(path: Path) -> sqlite3.Connection:
    """Open the separate local inventory database with durable SQLite settings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=FULL")
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create version-one inventory tables inside the caller's transaction."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS inventory_schema (
               singleton INTEGER PRIMARY KEY CHECK (singleton=1),
               schema_version INTEGER NOT NULL
           )"""
    )
    row = db.execute(
        "SELECT schema_version FROM inventory_schema WHERE singleton=1"
    ).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO inventory_schema (singleton,schema_version) VALUES (1,?)",
            (SCHEMA_VERSION,),
        )
    elif int(row["schema_version"]) != SCHEMA_VERSION:
        raise ValueError(
            "Nicht unterstützte Inventardatenbank-Version: "
            f"{row['schema_version']}"
        )
    db.execute(
        """CREATE TABLE IF NOT EXISTS inventory_epochs (
               device_id TEXT NOT NULL,
               epoch_id TEXT NOT NULL,
               coverage_from TEXT NOT NULL,
               started_at TEXT NOT NULL,
               reason TEXT NOT NULL,
               is_current INTEGER NOT NULL CHECK (is_current IN (0,1)),
               PRIMARY KEY (device_id,epoch_id)
           )"""
    )
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS inventory_current_epoch_idx
           ON inventory_epochs(device_id) WHERE is_current=1"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS inventory_scans (
               scan_id TEXT PRIMARY KEY,
               device_id TEXT NOT NULL,
               epoch_id TEXT NOT NULL,
               source_root TEXT NOT NULL,
               scanned_at TEXT NOT NULL,
               file_count INTEGER NOT NULL,
               total_bytes INTEGER NOT NULL,
               ignored_entries INTEGER NOT NULL,
               anomaly_count INTEGER NOT NULL,
               audio_invalid_count INTEGER NOT NULL,
               FOREIGN KEY (device_id,epoch_id)
                   REFERENCES inventory_epochs(device_id,epoch_id)
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS inventory_recordings (
               inventory_id TEXT PRIMARY KEY,
               device_id TEXT NOT NULL,
               epoch_id TEXT NOT NULL,
               relative_path TEXT NOT NULL,
               original_name TEXT NOT NULL,
               normalized_name TEXT NOT NULL,
               name_status TEXT NOT NULL,
               recording_number INTEGER,
               trailing_number INTEGER,
               size_bytes INTEGER NOT NULL,
               modified_ns INTEGER NOT NULL,
               modified_at TEXT NOT NULL,
               duration_seconds REAL,
               sample_rate INTEGER,
               channels INTEGER,
               sample_width_bits INTEGER,
               audio_status TEXT NOT NULL,
               audio_error TEXT,
               sha256 TEXT,
               hash_status TEXT NOT NULL,
               identity_status TEXT NOT NULL,
               presence_status TEXT NOT NULL,
               first_seen_at TEXT NOT NULL,
               last_seen_at TEXT NOT NULL,
               last_scan_id TEXT NOT NULL,
               is_current INTEGER NOT NULL CHECK (is_current IN (0,1)),
               replaces_inventory_id TEXT,
               FOREIGN KEY (device_id,epoch_id)
                   REFERENCES inventory_epochs(device_id,epoch_id),
               FOREIGN KEY (last_scan_id) REFERENCES inventory_scans(scan_id),
               FOREIGN KEY (replaces_inventory_id)
                   REFERENCES inventory_recordings(inventory_id)
           )"""
    )
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS inventory_current_path_idx
           ON inventory_recordings(device_id,epoch_id,relative_path)
           WHERE is_current=1"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS inventory_recording_number_idx
           ON inventory_recordings(device_id,epoch_id,recording_number)"""
    )


def ensure_epoch(
    db: sqlite3.Connection,
    device_id: str,
    epoch_id: str,
    coverage_from: str,
    reason: str,
    scanned_at: str,
) -> None:
    """Create the first epoch or require the configured current epoch to match."""
    current = db.execute(
        """SELECT epoch_id,coverage_from FROM inventory_epochs
           WHERE device_id=? AND is_current=1""",
        (device_id,),
    ).fetchone()
    if current is None:
        db.execute(
            """INSERT INTO inventory_epochs
               (device_id,epoch_id,coverage_from,started_at,reason,is_current)
               VALUES (?,?,?,?,?,1)""",
            (device_id, epoch_id, coverage_from, scanned_at, reason),
        )
        return
    if current["epoch_id"] != epoch_id:
        raise ValueError(
            "Konfigurierte Epoche stimmt nicht mit der aktuellen Datenbankepoche "
            f"überein ({epoch_id!r} != {current['epoch_id']!r}); "
            "ein Epochenwechsel benötigt einen ausdrücklichen Reset"
        )
    if current["coverage_from"] != coverage_from:
        raise ValueError(
            "coverage_from darf innerhalb einer Epoche nicht geändert werden "
            f"({coverage_from!r} != {current['coverage_from']!r})"
        )


def _same_source_version(row: sqlite3.Row, item: dict[str, Any]) -> bool:
    """Compare the cheap source signature used before a strong hash exists."""
    return (
        int(row["size_bytes"]) == int(item["size_bytes"])
        and int(row["modified_ns"]) == int(item["modified_ns"])
    )


def _audio_values(
    existing: sqlite3.Row | None, item: dict[str, Any]
) -> tuple[Any, Any, Any, Any, Any, Any]:
    """Preserve a prior header inspection when a quick scan skips audio parsing."""
    if item["audio_status"] != "NOT_CHECKED" or existing is None:
        return (
            item["duration_seconds"], item["sample_rate"], item["channels"],
            item["sample_width_bits"], item["audio_status"], item["audio_error"],
        )
    return (
        existing["duration_seconds"], existing["sample_rate"], existing["channels"],
        existing["sample_width_bits"], existing["audio_status"],
        existing["audio_error"],
    )


def synchronize_inventory(
    db: sqlite3.Connection,
    *,
    source: Path,
    device_id: str,
    epoch_id: str,
    coverage_from: str,
    reason: str,
    items: list[dict[str, Any]],
    ignored_entries: int,
    scanned_at: str,
    scan_id: str,
) -> list[dict[str, Any]]:
    """Persist one complete scan without ever writing to the source directory."""
    db.execute("BEGIN IMMEDIATE")
    try:
        ensure_schema(db)
        ensure_epoch(
            db, device_id, epoch_id, coverage_from, reason, scanned_at
        )
        db.execute(
            """INSERT INTO inventory_scans
               (scan_id,device_id,epoch_id,source_root,scanned_at,file_count,
                total_bytes,ignored_entries,anomaly_count,audio_invalid_count)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                scan_id, device_id, epoch_id, str(source), scanned_at, len(items),
                sum(int(item["size_bytes"]) for item in items), ignored_entries,
                sum(item["name_status"] != "STANDARD" for item in items),
                sum(item["audio_status"] == "INVALID" for item in items),
            ),
        )
        seen_paths: set[str] = set()
        result: list[dict[str, Any]] = []
        for source_item in items:
            item = dict(source_item)
            relative_path = str(item["relative_path"])
            seen_paths.add(relative_path)
            existing = db.execute(
                """SELECT * FROM inventory_recordings
                   WHERE device_id=? AND epoch_id=? AND relative_path=?
                         AND is_current=1""",
                (device_id, epoch_id, relative_path),
            ).fetchone()
            if existing is not None and _same_source_version(existing, item):
                audio = _audio_values(existing, item)
                db.execute(
                    """UPDATE inventory_recordings
                       SET original_name=?,normalized_name=?,name_status=?,
                           recording_number=?,trailing_number=?,modified_at=?,
                           duration_seconds=?,sample_rate=?,channels=?,
                           sample_width_bits=?,audio_status=?,audio_error=?,
                           presence_status='PRESENT',last_seen_at=?,last_scan_id=?
                       WHERE inventory_id=?""",
                    (
                        item["original_name"], item["normalized_name"],
                        item["name_status"], item["recording_number"],
                        item["trailing_number"], item["modified_at"], *audio,
                        scanned_at, scan_id, existing["inventory_id"],
                    ),
                )
                item["inventory_id"] = str(existing["inventory_id"])
                item["inventory_status"] = "UNCHANGED"
                item["audio_status"] = audio[4]
                item["audio_error"] = audio[5]
                item["duration_seconds"] = audio[0]
                item["sample_rate"] = audio[1]
                item["channels"] = audio[2]
                item["sample_width_bits"] = audio[3]
                item["hash_status"] = str(existing["hash_status"])
                item["sha256"] = existing["sha256"]
                item["identity_status"] = str(existing["identity_status"])
            else:
                replaces = str(existing["inventory_id"]) if existing is not None else None
                if existing is not None:
                    db.execute(
                        """UPDATE inventory_recordings
                           SET is_current=0,presence_status='SUPERSEDED'
                           WHERE inventory_id=?""",
                        (replaces,),
                    )
                inventory_id = provisional_inventory_id(device_id, epoch_id, item)
                collision = db.execute(
                    "SELECT 1 FROM inventory_recordings WHERE inventory_id=?",
                    (inventory_id,),
                ).fetchone()
                if collision is not None:
                    inventory_id = "inv-" + uuid.uuid4().hex
                db.execute(
                    """INSERT INTO inventory_recordings
                       (inventory_id,device_id,epoch_id,relative_path,original_name,
                        normalized_name,name_status,recording_number,trailing_number,
                        size_bytes,modified_ns,modified_at,duration_seconds,sample_rate,
                        channels,sample_width_bits,audio_status,audio_error,sha256,
                        hash_status,identity_status,presence_status,first_seen_at,
                        last_seen_at,last_scan_id,is_current,replaces_inventory_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,
                               'NOT_COMPUTED','PROVISIONAL','PRESENT',?,?,?,1,?)""",
                    (
                        inventory_id, device_id, epoch_id, relative_path,
                        item["original_name"], item["normalized_name"],
                        item["name_status"], item["recording_number"],
                        item["trailing_number"], item["size_bytes"],
                        item["modified_ns"], item["modified_at"],
                        item["duration_seconds"], item["sample_rate"],
                        item["channels"], item["sample_width_bits"],
                        item["audio_status"], item["audio_error"], scanned_at,
                        scanned_at, scan_id, replaces,
                    ),
                )
                item["inventory_id"] = inventory_id
                item["inventory_status"] = "CHANGED" if existing is not None else "NEW"
                item["hash_status"] = "NOT_COMPUTED"
                item["sha256"] = None
                item["identity_status"] = "PROVISIONAL"
            item["presence_status"] = "PRESENT"
            result.append(item)

        current_rows = db.execute(
            """SELECT inventory_id,relative_path FROM inventory_recordings
               WHERE device_id=? AND epoch_id=? AND is_current=1""",
            (device_id, epoch_id),
        ).fetchall()
        for row in current_rows:
            if row["relative_path"] not in seen_paths:
                db.execute(
                    """UPDATE inventory_recordings
                       SET presence_status='MISSING',last_scan_id=?
                       WHERE inventory_id=?""",
                    (scan_id, row["inventory_id"]),
                )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def duplicate_numbers(items: Iterable[dict[str, Any]]) -> list[int]:
    """Return repeated standard AudioRec numbers without treating them as IDs."""
    counts = Counter(
        int(item["recording_number"])
        for item in items
        if item.get("recording_number") is not None
    )
    return sorted(number for number, count in counts.items() if count > 1)


def build_report(
    *,
    source: Path,
    state: Path,
    device_id: str,
    epoch_id: str,
    coverage_from: str,
    scanned_at: str,
    scan_id: str,
    items: list[dict[str, Any]],
    ignored_entries: int,
    committed: bool,
) -> dict[str, Any]:
    """Build the human- and machine-readable inventory report."""
    anomalies = [item for item in items if item["name_status"] != "STANDARD"]
    invalid_audio = [item for item in items if item["audio_status"] == "INVALID"]
    repeated = duplicate_numbers(items)
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "source_read_only": True,
        "committed": committed,
        "scan_id": scan_id,
        "scanned_at": scanned_at,
        "source": {
            "path": str(source),
            "device_id": device_id,
            "epoch_id": epoch_id,
            "coverage_from": coverage_from,
        },
        "state": str(state) if committed else None,
        "summary": {
            "wav_files": len(items),
            "total_bytes": sum(int(item["size_bytes"]) for item in items),
            "ignored_entries": ignored_entries,
            "standard_names": len(items) - len(anomalies),
            "anomalous_names": len(anomalies),
            "duplicate_standard_numbers": repeated,
            "audio_invalid": len(invalid_audio),
            "audio_not_checked": sum(
                item["audio_status"] == "NOT_CHECKED" for item in items
            ),
            "new": sum(item.get("inventory_status") == "NEW" for item in items),
            "changed": sum(
                item.get("inventory_status") == "CHANGED" for item in items
            ),
            "unchanged": sum(
                item.get("inventory_status") == "UNCHANGED" for item in items
            ),
        },
        "items": items,
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Install a UTF-8 JSON report atomically and durably."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for a source-read-only inventory run."""
    parser = argparse.ArgumentParser(
        description=(
            "AudioRec-WAVs auf einem eingebundenen Smartphone rein lesend "
            "inventarisieren"
        )
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    parser.add_argument("--epoch-id", default=DEFAULT_EPOCH_ID)
    parser.add_argument("--coverage-from", default=DEFAULT_COVERAGE_FROM)
    parser.add_argument(
        "--reason", default="Initialer sicherer AudioRec-Inventurbestand"
    )
    parser.add_argument(
        "--inspect-audio",
        action="store_true",
        help="WAV-Köpfe zusätzlich öffnen; kann über MTP deutlich länger dauern",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Nur scannen und JSON ausgeben; keine Datenbank und keinen Bericht schreiben",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Den vollständigen Bericht zusätzlich auf stdout ausgeben",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run one inventory scan while keeping the phone source read-only."""
    args = parse_args(argv)
    logger = setup_cli_logging()
    try:
        if not args.device_id.strip() or not args.epoch_id.strip():
            raise ValueError("device-id und epoch-id dürfen nicht leer sein")
        coverage_from = validate_iso_datetime(
            args.coverage_from, "coverage_from"
        )
        validate_paths(
            args.source,
            args.state,
            None if args.check else args.report,
        )
        scanned_at = iso_utc(utc_now())
        scan_id = "scan-" + uuid.uuid4().hex
        items, ignored = scan_source(args.source, args.inspect_audio)
        if args.check:
            for item in items:
                item["inventory_id"] = provisional_inventory_id(
                    args.device_id, args.epoch_id, item
                )
                item["inventory_status"] = "OBSERVED_UNCOMMITTED"
                item["hash_status"] = "NOT_COMPUTED"
                item["sha256"] = None
                item["identity_status"] = "PROVISIONAL"
                item["presence_status"] = "PRESENT"
            committed = False
        else:
            with open_inventory_state(args.state) as db:
                items = synchronize_inventory(
                    db,
                    source=args.source,
                    device_id=args.device_id,
                    epoch_id=args.epoch_id,
                    coverage_from=coverage_from,
                    reason=args.reason,
                    items=items,
                    ignored_entries=ignored,
                    scanned_at=scanned_at,
                    scan_id=scan_id,
                )
            committed = True
        report = build_report(
            source=args.source,
            state=args.state,
            device_id=args.device_id,
            epoch_id=args.epoch_id,
            coverage_from=coverage_from,
            scanned_at=scanned_at,
            scan_id=scan_id,
            items=items,
            ignored_entries=ignored,
            committed=committed,
        )
        if not args.check:
            write_json_atomic(args.report, report)
        if args.json or args.check:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        logger.info(
            "AudioRec-Inventur: wav=%d bytes=%d anomalien=%d audio_ungueltig=%d "
            "committed=%s",
            report["summary"]["wav_files"],
            report["summary"]["total_bytes"],
            report["summary"]["anomalous_names"],
            report["summary"]["audio_invalid"],
            committed,
        )
        logger.info(
            "Smartphone-Quelle blieb schreibgeschützt; Hashes und Löschfreigaben "
            "sind nicht Bestandteil von v0.1"
        )
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.error("AudioRec-Inventur fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
