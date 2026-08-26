#!/usr/bin/env python3
"""Google Drive inbox scanner with optional explicit local staging.

Normal scans only list and classify files; they never download, move, delete, or
transcribe anything. ``--stage-id`` explicitly downloads verified audio to local
staging. The local SQLite state is intentionally persistent so a file can become
READY after two unchanged observations with enough time apart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any, Iterable
from urllib.parse import quote


AUDIO_SUFFIXES = {".wav", ".m4a", ".mp3", ".flac", ".ogg"}
DEFAULT_SOURCE = os.environ.get("AUDIOREC_SOURCE", "gdrive:AudioRec Recordings")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive-Inbox read-only prüfen; keine Transkription oder Drive-Änderung."
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--state-dir", type=Path, default=Path(".inbox-watcher"))
    parser.add_argument(
        "--stable-seconds",
        type=int,
        default=120,
        help="Mindestabstand zwischen zwei identischen Prüfungen (Standard: 120)",
    )
    parser.add_argument(
        "--listing-file",
        type=Path,
        help="Testmodus: rclone-lsjson aus Datei lesen statt Drive abzufragen",
    )
    parser.add_argument("--json", action="store_true", help="Ergebnis als JSON ausgeben")
    parser.add_argument(
        "--verbose", action="store_true", help="Zusätzlich eine Logzeile pro Datei ausgeben"
    )
    parser.add_argument(
        "--recent-minutes", type=int, metavar="MINUTEN",
        help="Nur seit diesem Zeitraum erstmals gesehene Dateien zusätzlich anzeigen",
    )
    parser.add_argument(
        "--stage-id", action="append", default=[], metavar="DRIVE_ID",
        help="READY-Datei mit dieser exakten Drive-ID lokal bereitstellen (wiederholbar)",
    )
    parser.add_argument(
        "--staging-dir", type=Path, default=Path("staging/inbox"),
        help="Lokaler Zielordner für --stage-id (Standard: staging/inbox)",
    )
    return parser.parse_args(argv)


def setup_logging(state_dir: Path) -> logging.Logger:
    state_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("inbox_watcher")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logfile = RotatingFileHandler(
        state_dir / "inbox-watcher.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    logfile.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(logfile)
    return logger


def open_state(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            drive_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            hash_type TEXT NOT NULL,
            mod_time TEXT,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            stable_observations INTEGER NOT NULL,
            status TEXT NOT NULL,
            duplicate_of TEXT
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS files_hash_idx ON files(content_hash)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS staged_files (
            drive_id TEXT PRIMARY KEY,
            local_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            staged_at REAL NOT NULL
        )"""
    )
    return db


def ensure_pipeline_state(db: sqlite3.Connection) -> None:
    """Create state shared by transcription and publication workflows."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS pipeline_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS transcription_jobs (
            drive_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, local_audio TEXT,
            transcript_path TEXT, last_error TEXT, updated_at REAL NOT NULL
        )"""
    )
    db.commit()


def load_listing(source: str, listing_file: Path | None) -> list[dict[str, Any]]:
    if listing_file:
        raw = listing_file.read_text(encoding="utf-8")
    else:
        command = ["rclone", "lsjson", source, "--files-only", "--hash"]
        result = subprocess.run(command, check=True, text=True, capture_output=True)
        raw = result.stdout
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("rclone-Ausgabe ist keine JSON-Liste")
    return value


def get_hash(item: dict[str, Any]) -> tuple[str, str] | None:
    hashes = {str(k).lower(): str(v).lower() for k, v in (item.get("Hashes") or {}).items() if v}
    for kind in ("sha256", "sha1", "md5"):
        if kind in hashes:
            return kind, hashes[kind]
    return None


def eligible(item: dict[str, Any]) -> tuple[bool, str]:
    if item.get("IsDir"):
        return False, "directory"
    if Path(str(item.get("Path", ""))).suffix.lower() not in AUDIO_SUFFIXES:
        return False, "non_audio"
    if int(item.get("Size", 0)) <= 0:
        return False, "empty"
    if not item.get("ID"):
        return False, "missing_id"
    if get_hash(item) is None:
        return False, "missing_hash"
    return True, "eligible"


def classify(
    db: sqlite3.Connection, items: Iterable[dict[str, Any]], now: float, stable_seconds: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # Stable ordering makes the canonical upload deterministic for same-scan duplicates.
    ordered = sorted(items, key=lambda x: (str(x.get("ModTime", "")), str(x.get("ID", ""))))
    for item in ordered:
        ok, reason = eligible(item)
        path = str(item.get("Path", ""))
        if not ok:
            results.append({"path": path, "drive_id": item.get("ID"), "status": "IGNORED", "reason": reason})
            continue

        drive_id = str(item["ID"])
        size = int(item["Size"])
        hash_type, content_hash = get_hash(item)  # type: ignore[misc]
        previous = db.execute("SELECT * FROM files WHERE drive_id = ?", (drive_id,)).fetchone()

        duplicate = db.execute(
            """SELECT drive_id FROM files
               WHERE content_hash = ? AND drive_id <> ? AND status = 'READY'
               ORDER BY first_seen, drive_id LIMIT 1""",
            (content_hash, drive_id),
        ).fetchone()

        unchanged = bool(
            previous
            and previous["size"] == size
            and previous["content_hash"] == content_hash
            and previous["hash_type"] == hash_type
        )
        if duplicate:
            status, duplicate_of = "DUPLICATE", duplicate["drive_id"]
            observations = (previous["stable_observations"] + 1) if unchanged else 1
            first_seen = previous["first_seen"] if unchanged else now
        elif unchanged and now - previous["first_seen"] >= stable_seconds:
            status, duplicate_of = "READY", None
            observations = previous["stable_observations"] + 1
            first_seen = previous["first_seen"]
        else:
            status, duplicate_of = "OBSERVED", None
            observations = previous["stable_observations"] if unchanged else 1
            first_seen = previous["first_seen"] if unchanged else now

        db.execute(
            """INSERT INTO files
               (drive_id, path, size, content_hash, hash_type, mod_time, first_seen,
                last_seen, stable_observations, status, duplicate_of)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(drive_id) DO UPDATE SET
                 path=excluded.path, size=excluded.size, content_hash=excluded.content_hash,
                 hash_type=excluded.hash_type, mod_time=excluded.mod_time,
                 first_seen=excluded.first_seen, last_seen=excluded.last_seen,
                 stable_observations=excluded.stable_observations, status=excluded.status,
                 duplicate_of=excluded.duplicate_of""",
            (drive_id, path, size, content_hash, hash_type, item.get("ModTime"), first_seen,
             now, observations, status, duplicate_of),
        )
        results.append(
            {"path": path, "drive_id": drive_id, "size": size, "hash_type": hash_type,
             "hash": content_hash, "status": status, "stable_observations": observations,
             "duplicate_of": duplicate_of}
        )
    db.commit()
    return results


def file_digest(path: Path, hash_type: str) -> str:
    digest = hashlib.new(hash_type)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def staging_name(path: str, drive_id: str) -> str:
    source_name = Path(path).name
    suffix = Path(source_name).suffix
    stem = source_name[:-len(suffix)] if suffix else source_name
    safe_id = quote(drive_id, safe="")
    return f"{stem}__{safe_id}{suffix}"


def remote_root(source: str) -> str:
    if ":" not in source:
        raise ValueError("--source muss ein rclone-Remote wie 'gdrive:Ordner' sein")
    return source.split(":", 1)[0] + ":"


def stage_ready_file(
    db: sqlite3.Connection, source: str, staging_dir: Path, drive_id: str, now: float
) -> tuple[Path, bool]:
    row = db.execute("SELECT * FROM files WHERE drive_id = ?", (drive_id,)).fetchone()
    if row is None:
        raise ValueError(f"unbekannte Drive-ID: {drive_id}")
    if row["status"] != "READY":
        raise ValueError(f"Drive-ID {drive_id} hat Status {row['status']}, nicht READY")

    staging_dir.mkdir(parents=True, exist_ok=True)
    destination = staging_dir / staging_name(row["path"], drive_id)
    if destination.exists():
        if destination.stat().st_size == row["size"] and file_digest(destination, row["hash_type"]) == row["content_hash"]:
            return destination, False
        raise ValueError(f"Zieldatei existiert mit abweichendem Inhalt: {destination}")

    partial = destination.with_name(destination.name + ".part")
    if partial.exists():
        partial.unlink()
    try:
        subprocess.run(
            ["rclone", "backend", "copyid", remote_root(source), drive_id, str(partial)],
            check=True, text=True, capture_output=True,
        )
        if partial.stat().st_size != row["size"]:
            raise ValueError(
                f"Größe nach Download abweichend: erwartet {row['size']}, erhalten {partial.stat().st_size}"
            )
        actual_hash = file_digest(partial, row["hash_type"])
        if actual_hash != row["content_hash"]:
            raise ValueError(
                f"Hash nach Download abweichend: erwartet {row['content_hash']}, erhalten {actual_hash}"
            )
        partial.replace(destination)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise

    db.execute(
        """INSERT INTO staged_files (drive_id, local_path, content_hash, staged_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(drive_id) DO UPDATE SET local_path=excluded.local_path,
             content_hash=excluded.content_hash, staged_at=excluded.staged_at""",
        (drive_id, str(destination), row["content_hash"], now),
    )
    db.commit()
    return destination, True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.stable_seconds < 0:
        print("Fehler: --stable-seconds darf nicht negativ sein", file=sys.stderr)
        return 2
    if args.recent_minutes is not None and args.recent_minutes < 0:
        print("Fehler: --recent-minutes darf nicht negativ sein", file=sys.stderr)
        return 2
    logger = setup_logging(args.state_dir)
    try:
        listing = load_listing(args.source, args.listing_file)
        now = time.time()
        with open_state(args.state_dir / "state.sqlite3") as db:
            results = classify(db, listing, now, args.stable_seconds)
            first_seen = {
                row["drive_id"]: row["first_seen"]
                for row in db.execute("SELECT drive_id, first_seen FROM files")
            }
            staged = [stage_ready_file(db, args.source, args.staging_dir, drive_id, now)
                      for drive_id in args.stage_id]
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        logger.error("Scan fehlgeschlagen: %s", exc)
        return 1

    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        if args.verbose:
            logger.info("status=%s path=%r id=%s reason=%s duplicate_of=%s",
                        result["status"], result["path"], result.get("drive_id"),
                        result.get("reason", "-"), result.get("duplicate_of") or "-")
    logger.info("Dry-Run beendet: source=%r total=%d counts=%s (keine Drive-Änderung)",
                args.source, len(results), counts)
    if args.recent_minutes is not None:
        cutoff = now - args.recent_minutes * 60
        recent = [item for item in results
                  if first_seen.get(item.get("drive_id"), 0) >= cutoff]
        logger.info("Neu in den letzten %d Minuten: %d", args.recent_minutes, len(recent))
        for item in recent:
            logger.info("recent status=%s path=%r id=%s duplicate_of=%s",
                        item["status"], item["path"], item.get("drive_id"),
                        item.get("duplicate_of") or "-")
    for destination, downloaded in staged:
        logger.info("Staging %s: %s (Drive unverändert)",
                    "heruntergeladen" if downloaded else "bereits geprüft vorhanden", destination)
    if args.json:
        print(json.dumps({"source": args.source, "counts": counts, "files": results},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
