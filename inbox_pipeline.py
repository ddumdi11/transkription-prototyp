#!/usr/bin/env python3
"""Conservative automatic pipeline for newly activated Drive inbox files."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from inbox_watcher import (classify, ensure_pipeline_state, load_listing, open_state,
                           setup_logging, stage_ready_file)
from publish_transcripts import ensure_publish_state, pending_publications, publish_one
from project_glossary import glossary_hotwords, glossary_prompt
from route_transcripts import CONFIG_PATH as ROUTING_CONFIG, load_config, plan_published

PROMPT = os.environ.get("AUDIOREC_PROMPT")
if PROMPT is None:
    PROMPT = glossary_prompt()
HOTWORDS = os.environ.get("AUDIOREC_HOTWORDS")
if HOTWORDS is None:
    HOTWORDS = ", ".join(glossary_hotwords())
SOURCE = os.environ.get("AUDIOREC_SOURCE", "gdrive:AudioRec Recordings")
STATE_DIR = Path(".inbox-watcher")
STAGING_DIR = Path("staging/inbox")
OUTPUT_DIR = Path("staging/transcripts")
TRANSCRIPTS_TARGET = os.environ.get("AUDIOREC_TRANSCRIPTS_TARGET")
AUTO_PUBLISH = os.environ.get("AUDIOREC_AUTO_PUBLISH") == "1"
AUTH_ALERT = STATE_DIR / "rclone-auth-required"
AUTH_ALERT_COOLDOWN = 6 * 60 * 60


def clear_auth_alert() -> None:
    """Allow a future auth incident to notify again after a successful scan."""
    AUTH_ALERT.unlink(missing_ok=True)


def notify_auth_failure(exc: Exception, logger, now: float | None = None) -> bool:
    """Persist and rate-limit a desktop warning for an expired Drive login."""
    if "invalid_grant" not in str(exc).lower():
        return False

    now = time.time() if now is None else now
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        last_notified = float(AUTH_ALERT.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        last_notified = None

    if last_notified is not None and now - last_notified < AUTH_ALERT_COOLDOWN:
        return False

    AUTH_ALERT.write_text(f"{now}\n", encoding="utf-8")

    message = "Google Drive neu anmelden: rclone config reconnect gdrive:"
    try:
        subprocess.run(
            ["notify-send", "--urgency=critical", "AudioRec benötigt Anmeldung", message],
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as notify_exc:
        logger.warning("Desktop-Benachrichtigung fehlgeschlagen: %s", notify_exc)
    logger.error("Rclone-Anmeldung erforderlich. %s", message)
    return True


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
    if HOTWORDS.strip():
        command.extend(["--hotwords", HOTWORDS])
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


def publish_completed(db: sqlite3.Connection, target: str, drive_id: str, logger) -> int:
    """Publish one completed job without stopping later transcription jobs."""
    try:
        name, uploaded = publish_one(db, target, drive_id, time.time())
        logger.info("Job PUBLISHED id=%s remote=%r upload=%s",
                    drive_id, name, "neu" if uploaded else "bereits vorhanden")
        log_routing_plan(db, drive_id, logger)
        return 0
    except Exception as exc:
        logger.exception("Publish FAILED id=%s: %s", drive_id, exc)
        return 1


def log_routing_plan(db: sqlite3.Connection, drive_id: str, logger) -> bool:
    """Log advisory project routing without changing Drive or job status."""
    try:
        plan = plan_published(db, drive_id, load_config(ROUTING_CONFIG))
        topics = ",".join(plan["topics"]) or "-"
        logger.info(
            "Job ROUTED id=%s path=%r topics=%s",
            drive_id, plan["audio_path"], topics,
        )
        for project in plan["projects"]:
            logger.info(
                "  -> %s [%s]",
                project["name"], "; ".join(project["reasons"]),
            )
        return True
    except Exception as exc:
        logger.exception("Routing FAILED id=%s: %s", drive_id, exc)
        return False


def publish_pending(db: sqlite3.Connection, target: str, logger) -> int:
    ensure_publish_state(db)
    jobs = pending_publications(db)
    logger.info("Auto-Veröffentlichung: %d neuer DONE-Job(s)", len(jobs))
    return sum(
        publish_completed(db, target, job["drive_id"], logger) for job in jobs
    )


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
        clear_auth_alert()
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
            if AUTO_PUBLISH:
                if not TRANSCRIPTS_TARGET:
                    raise ValueError(
                        "AUDIOREC_AUTO_PUBLISH=1, aber AUDIOREC_TRANSCRIPTS_TARGET fehlt"
                    )
                failures += publish_pending(db, TRANSCRIPTS_TARGET, logger)
            for job in jobs:
                try:
                    audio_path, downloaded = stage_ready_file(
                        db, SOURCE, STAGING_DIR, job["drive_id"], time.time()
                    )
                    logger.info("Job id=%s path=%r staging=%s", job["drive_id"], job["path"],
                                "geladen" if downloaded else "vorhanden")
                    transcript = transcribe_one(db, job["drive_id"], audio_path, time.time())
                    logger.info("Job DONE id=%s transcript=%s", job["drive_id"], transcript)
                    if AUTO_PUBLISH:
                        failures += publish_completed(
                            db, TRANSCRIPTS_TARGET, job["drive_id"], logger
                        )
                except Exception as exc:
                    failures += 1
                    logger.exception("Job FAILED id=%s: %s", job["drive_id"], exc)
            return 1 if failures else 0
    except Exception as exc:
        notify_auth_failure(exc, logger)
        logger.exception("Pipeline fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
