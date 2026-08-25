#!/usr/bin/env python3
"""Plan and explicitly publish verified transcripts to Google Drive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from inbox_watcher import ensure_pipeline_state, open_state, setup_logging, staging_name

STATE_DIR = Path(".inbox-watcher")
TARGET = os.environ.get("AUDIOREC_TRANSCRIPTS_TARGET")


def ensure_publish_state(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS published_transcripts (
            drive_id TEXT PRIMARY KEY,
            local_path TEXT NOT NULL,
            local_hash TEXT NOT NULL,
            size INTEGER NOT NULL,
            remote_path TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            published_at REAL NOT NULL
        )"""
    )
    db.commit()


def prepare_publish_state(db: sqlite3.Connection) -> None:
    ensure_pipeline_state(db)
    ensure_publish_state(db)


def pending_publications(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT j.drive_id, j.transcript_path, f.path, f.first_seen
           FROM transcription_jobs j
           JOIN files f USING (drive_id)
           LEFT JOIN published_transcripts p USING (drive_id)
           WHERE j.status = 'DONE' AND p.drive_id IS NULL
           ORDER BY f.first_seen, j.drive_id"""
    ).fetchall()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remote_name(audio_path: str, drive_id: str) -> str:
    return str(Path(staging_name(audio_path, drive_id)).with_suffix(".md"))


def inspect_remote(target: str, name: str) -> dict | None:
    result = subprocess.run(
        ["rclone", "lsjson", target, "--files-only", "--hash"],
        check=True, text=True, capture_output=True,
    )
    rows = json.loads(result.stdout)
    matches = [row for row in rows if row.get("Path") == name]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"Mehrdeutiges Upload-Ziel für {name!r}: {len(matches)} Dateien")
    return matches[0]


def publish_one(
    db: sqlite3.Connection, target: str, drive_id: str, now: float
) -> tuple[str, bool]:
    row = db.execute(
        """SELECT j.transcript_path, j.status, f.path
           FROM transcription_jobs j JOIN files f USING (drive_id)
           WHERE j.drive_id = ?""", (drive_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unbekannter Transkriptionsjob: {drive_id}")
    if row["status"] != "DONE":
        raise ValueError(f"Job {drive_id} hat Status {row['status']}, nicht DONE")
    existing_state = db.execute(
        "SELECT remote_path FROM published_transcripts WHERE drive_id = ?", (drive_id,)
    ).fetchone()
    if existing_state:
        return existing_state["remote_path"], False

    local_path = Path(row["transcript_path"])
    if not local_path.is_file():
        raise ValueError(f"Lokales Transkript fehlt: {local_path}")
    size = local_path.stat().st_size
    local_hash = sha256_file(local_path)
    name = remote_name(row["path"], drive_id)

    remote = inspect_remote(target, name)
    uploaded = False
    if remote is None:
        subprocess.run(
            ["rclone", "copyto", str(local_path), f"{target}{name}", "--immutable"],
            check=True,
        )
        uploaded = True
        remote = inspect_remote(target, name)
    if remote is None:
        raise RuntimeError(f"Upload ist nach copyto nicht sichtbar: {name}")
    hashes = {str(k).lower(): str(v).lower()
              for k, v in (remote.get("Hashes") or {}).items() if v}
    if int(remote.get("Size", -1)) != size:
        raise ValueError(f"Remote-Größe abweichend für {name}")
    if hashes.get("sha256") != local_hash:
        raise ValueError(f"Remote-SHA256 abweichend für {name}")
    remote_id = str(remote.get("ID") or "")
    if not remote_id:
        raise ValueError(f"Remote-ID fehlt für {name}")

    db.execute(
        """INSERT INTO published_transcripts
           (drive_id, local_path, local_hash, size, remote_path, remote_id, published_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (drive_id, str(local_path), local_hash, size, name, remote_id, now),
    )
    db.commit()
    return name, uploaded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DONE-Transkripte planen oder explizit verifiziert veröffentlichen"
    )
    parser.add_argument(
        "--publish-id", action="append", default=[], metavar="DRIVE_ID",
        help="Diesen DONE-Job veröffentlichen (wiederholbar)",
    )
    parser.add_argument(
        "--publish-all", action="store_true",
        help="Alle geplanten DONE-Jobs verifizieren beziehungsweise veröffentlichen",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Alle geplanten Jobs einzeln anzeigen",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logging(STATE_DIR)
    try:
        if args.publish_all and args.publish_id:
            raise ValueError("--publish-all und --publish-id dürfen nicht kombiniert werden")
        with open_state(STATE_DIR / "state.sqlite3") as db:
            prepare_publish_state(db)
            pending = pending_publications(db)
            logger.info("Veröffentlichungsplan: %d DONE, noch nicht veröffentlicht", len(pending))
            if args.verbose:
                for row in pending:
                    logger.info("pending path=%r id=%s transcript=%s",
                                row["path"], row["drive_id"], row["transcript_path"])
            selected_ids = ([row["drive_id"] for row in pending]
                            if args.publish_all else args.publish_id)
            if selected_ids and not TARGET:
                raise ValueError("AUDIOREC_TRANSCRIPTS_TARGET ist nicht konfiguriert")
            published = 0
            for drive_id in selected_ids:
                name, uploaded = publish_one(db, TARGET, drive_id, time.time())
                published += 1
                logger.info("PUBLISHED id=%s remote=%r upload=%s",
                            drive_id, name, "neu" if uploaded else "bereits vorhanden")
            if selected_ids:
                logger.info("Veröffentlichung beendet: %d erfolgreich", published)
        return 0
    except Exception as exc:
        logger.exception("Veröffentlichung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
