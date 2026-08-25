#!/usr/bin/env python3
"""Conservative automatic pipeline for newly activated Drive inbox files."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from inbox_watcher import classify, load_listing, open_state, setup_logging, stage_ready_file
from publish_transcripts import ensure_publish_state, pending_publications, publish_one

PROMPT = "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI"
SOURCE = os.environ.get("AUDIOREC_SOURCE", "gdrive:AudioRec Recordings")
STATE_DIR = Path(".inbox-watcher")
STAGING_DIR = Path("staging/inbox")
OUTPUT_DIR = Path("staging/transcripts")
TRANSCRIPTS_TARGET = os.environ.get("AUDIOREC_TRANSCRIPTS_TARGET")
AUTO_PUBLISH = os.environ.get("AUDIOREC_AUTO_PUBLISH") == "1"


def ensure_pipeline_state(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS pipeline_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS transcription_jobs (
            drive_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, local_audio TEXT,
            transcript_path TEXT, last_error TEXT, updated_at REAL NOT NULL
        )"""
    )
    db.commit()


def activate_from_id(db: sqlite3.Connection, drive_id: str) -> float:
    row = db.execute("SELECT first_seen, status FROM files WHERE drive_id = ?", (drive_id,)).fetchone()
    if row is None:
        raise ValueError(f"Aktivierungs-ID ist unbekannt: {drive_id}")
    if row["status"] != "READY":
        raise ValueError(f"Aktivierungs-ID hat Status {row['status']}, nicht READY")
    cutoff = float(row["first_seen"])
    db.execute(
        """INSERT INTO pipeline_settings (key, value) VALUES ('activated_from', ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(cutoff),)
    )
    db.commit()
    return cutoff


def activation_cutoff(db: sqlite3.Connection) -> float | None:
    row = db.execute("SELECT value FROM pipeline_settings WHERE key='activated_from'").fetchone()
    return float(row["value"]) if row else None


def pending_ready(db: sqlite3.Connection, cutoff: float) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT f.* FROM files f
           LEFT JOIN transcription_jobs j ON j.drive_id = f.drive_id
           WHERE f.status = 'READY' AND f.first_seen >= ?
             AND (j.status IS NULL OR j.status = 'FAILED')
           ORDER BY f.first_seen, f.drive_id""", (cutoff,)
    ).fetchall()


def transcribe_one(db: sqlite3.Connection, drive_id: str, audio_path: Path, now: float) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    transcript = OUTPUT_DIR / f"{audio_path.stem}.md"
    previous = db.execute(
        "SELECT attempts FROM transcription_jobs WHERE drive_id = ?", (drive_id,)
    ).fetchone()
    attempts = (previous["attempts"] if previous else 0) + 1
    db.execute(
        """INSERT INTO transcription_jobs
           (drive_id, status, attempts, local_audio, transcript_path, last_error, updated_at)
           VALUES (?, 'RUNNING', ?, ?, ?, NULL, ?)
           ON CONFLICT(drive_id) DO UPDATE SET status='RUNNING', attempts=excluded.attempts,
             local_audio=excluded.local_audio, transcript_path=excluded.transcript_path,
             last_error=NULL, updated_at=excluded.updated_at""",
        (drive_id, attempts, str(audio_path), str(transcript), now),
    )
    db.commit()
    command = [
        ".venv/bin/python", "transcribe.py", str(audio_path),
        "--output-dir", str(OUTPUT_DIR), "--provider", "local", "--model", "medium",
        "--prompt", PROMPT, "--metadata-header",
    ]
    try:
        subprocess.run(command, check=True)
        if not transcript.exists():
            raise RuntimeError(f"Transkript wurde nicht erzeugt: {transcript}")
    except Exception as exc:
        db.execute(
            "UPDATE transcription_jobs SET status='FAILED', last_error=?, updated_at=? WHERE drive_id=?",
            (str(exc), time.time(), drive_id),
        )
        db.commit()
        raise
    db.execute(
        "UPDATE transcription_jobs SET status='DONE', last_error=NULL, updated_at=? WHERE drive_id=?",
        (time.time(), drive_id),
    )
    db.commit()
    return transcript


def publish_pending(db: sqlite3.Connection, target: str, logger) -> int:
    ensure_publish_state(db)
    jobs = pending_publications(db)
    logger.info("Auto-Veröffentlichung: %d neuer DONE-Job(s)", len(jobs))
    failures = 0
    for job in jobs:
        try:
            name, uploaded = publish_one(db, target, job["drive_id"], time.time())
            logger.info("Job PUBLISHED id=%s remote=%r upload=%s",
                        job["drive_id"], name, "neu" if uploaded else "bereits vorhanden")
        except Exception as exc:
            failures += 1
            logger.exception("Publish FAILED id=%s: %s", job["drive_id"], exc)
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Neue READY-Aufnahmen automatisch lokal transkribieren")
    parser.add_argument("--activate-from-id", metavar="DRIVE_ID",
                        help="Automatik ab dieser bekannten READY-Datei aktivieren")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logging(STATE_DIR)
    try:
        listing = load_listing(SOURCE, None)
        now = time.time()
        with open_state(STATE_DIR / "state.sqlite3") as db:
            ensure_pipeline_state(db)
            classify(db, listing, now, 120)
            if args.activate_from_id:
                cutoff = activate_from_id(db, args.activate_from_id)
                logger.info("Auto-Transkription aktiviert ab Drive-ID=%s", args.activate_from_id)
            else:
                cutoff = activation_cutoff(db)
                if cutoff is None:
                    logger.error("Auto-Transkription ist nicht aktiviert")
                    return 2
            jobs = pending_ready(db, cutoff)
            logger.info("Auto-Transkription: %d neuer READY-Job(s)", len(jobs))
            failures = 0
            for job in jobs:
                try:
                    audio_path, downloaded = stage_ready_file(
                        db, SOURCE, STAGING_DIR, job["drive_id"], time.time()
                    )
                    logger.info("Job id=%s path=%r staging=%s", job["drive_id"], job["path"],
                                "geladen" if downloaded else "vorhanden")
                    transcript = transcribe_one(db, job["drive_id"], audio_path, time.time())
                    logger.info("Job DONE id=%s transcript=%s", job["drive_id"], transcript)
                except Exception as exc:
                    failures += 1
                    logger.exception("Job FAILED id=%s: %s", job["drive_id"], exc)
            if AUTO_PUBLISH:
                if not TRANSCRIPTS_TARGET:
                    raise ValueError(
                        "AUDIOREC_AUTO_PUBLISH=1, aber AUDIOREC_TRANSCRIPTS_TARGET fehlt"
                    )
                failures += publish_pending(db, TRANSCRIPTS_TARGET, logger)
            return 1 if failures else 0
    except Exception as exc:
        logger.exception("Pipeline fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
