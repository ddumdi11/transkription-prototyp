#!/usr/bin/env python3
"""Create a deterministic, read-only project routing plan for transcripts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from inbox_watcher import open_state, setup_logging
from publish_transcripts import prepare_publish_state

STATE_DIR = Path(".inbox-watcher")
CONFIG_PATH = Path(os.environ.get(
    "AUDIOREC_ROUTING_CONFIG", STATE_DIR / "routing.json"
))
NUMBER_PATTERN = re.compile(r"Aufnahme\s*#(\d+)", re.IGNORECASE)


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("default_projects", "active_projects", "project_rules", "topic_rules"):
        if key not in config:
            raise ValueError(f"Routing-Konfiguration enthält {key!r} nicht")
    if not all(isinstance(config[key], list) for key in
               ("default_projects", "active_projects", "project_rules")):
        raise ValueError("Projektfelder der Routing-Konfiguration müssen Listen sein")
    if not isinstance(config["topic_rules"], dict):
        raise ValueError("topic_rules muss ein Objekt sein")
    for index, rule in enumerate(config["project_rules"]):
        if not isinstance(rule, dict):
            raise ValueError(f"project_rules[{index}] muss ein Objekt sein")
        project = rule.get("project")
        if not isinstance(project, str) or not project.strip():
            raise ValueError(
                f"project_rules[{index}].project muss ein nichtleerer Text sein"
            )
        terms = rule.get("match_any")
        if (not isinstance(terms, list)
                or not all(isinstance(term, str) and term for term in terms)):
            raise ValueError(
                f"project_rules[{index}].match_any muss eine Textliste sein"
            )
    for topic, terms in config["topic_rules"].items():
        if (not isinstance(terms, list)
                or not all(isinstance(term, str) and term for term in terms)):
            raise ValueError(f"topic_rules[{topic!r}] muss eine Textliste sein")
    return config


def published_transcripts(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT p.drive_id, p.remote_path, p.published_at,
                  j.transcript_path, f.path AS audio_path
           FROM published_transcripts p
           JOIN transcription_jobs j USING (drive_id)
           JOIN files f USING (drive_id)
           ORDER BY f.first_seen, p.drive_id"""
    ).fetchall()


def recording_number(path: str) -> int | None:
    match = NUMBER_PATTERN.search(path)
    return int(match.group(1)) if match else None


def matching_terms(text: str, terms: list[str]) -> list[str]:
    folded = text.casefold()
    return [term for term in terms if term.casefold() in folded]


def plan_one(row: sqlite3.Row, config: dict[str, Any]) -> dict[str, Any]:
    transcript_path = Path(row["transcript_path"])
    if not transcript_path.is_file():
        raise ValueError(f"Lokales Transkript fehlt: {transcript_path}")
    text = transcript_path.read_text(encoding="utf-8")
    reasons: dict[str, list[str]] = {}

    def add_project(project: str, reason: str) -> None:
        reasons.setdefault(project, [])
        if reason not in reasons[project]:
            reasons[project].append(reason)

    for project in config["default_projects"]:
        add_project(str(project), "default")
    for project in config["active_projects"]:
        add_project(str(project), "active_context")
    for rule in config["project_rules"]:
        matched = matching_terms(text, list(rule.get("match_any", [])))
        if matched:
            add_project(str(rule["project"]), "content:" + ",".join(matched))

    topics = []
    for topic, terms in config["topic_rules"].items():
        if matching_terms(text, list(terms)):
            topics.append(str(topic))

    return {
        "drive_id": row["drive_id"],
        "audio_path": row["audio_path"],
        "transcript_path": row["transcript_path"],
        "remote_path": row["remote_path"],
        "recording_number": recording_number(row["audio_path"]),
        "projects": [
            {"name": project, "reasons": project_reasons}
            for project, project_reasons in reasons.items()
        ],
        "topics": topics,
    }


def selected(row: sqlite3.Row, args: argparse.Namespace) -> bool:
    if args.drive_id and row["drive_id"] not in args.drive_id:
        return False
    number = recording_number(row["audio_path"])
    if args.from_number is not None and (number is None or number < args.from_number):
        return False
    if args.to_number is not None and (number is None or number > args.to_number):
        return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Projektverteilung veröffentlichter Transkripte als Dry-Run planen"
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--drive-id", action="append", default=[])
    parser.add_argument("--from-number", type=int)
    parser.add_argument("--to-number", type=int)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logging(STATE_DIR)
    try:
        config = load_config(args.config)
        with open_state(STATE_DIR / "state.sqlite3") as db:
            prepare_publish_state(db)
            rows = [row for row in published_transcripts(db) if selected(row, args)]
            plans = [plan_one(row, config) for row in rows]
        if args.json:
            print(json.dumps({"dry_run": True, "plans": plans}, ensure_ascii=False,
                             indent=2))
        else:
            logger.info("Routing-Dry-Run: %d veröffentlichte(s) Transkript(e)", len(plans))
            for plan in plans:
                logger.info("route path=%r id=%s topics=%s", plan["audio_path"],
                            plan["drive_id"], ",".join(plan["topics"]) or "-")
                for project in plan["projects"]:
                    logger.info("  -> %s [%s]", project["name"],
                                "; ".join(project["reasons"]))
            logger.info("Dry-Run beendet (keine Kopie, keine Drive-Änderung)")
        return 0
    except Exception as exc:
        logger.exception("Routing-Plan fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
