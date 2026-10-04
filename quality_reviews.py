#!/usr/bin/env python3
"""Persist and explicitly decide transcript quality-review candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable

from analyze_segment_quality import find_suspicious_segments
from inbox_watcher import open_state
from segment_metadata import load_segment_metadata


DEFAULT_STATE = Path(".inbox-watcher/state.sqlite3")
REVIEW_STATUSES = {"PENDING", "CONFIRMED", "DISMISSED"}
ISSUE_TYPES = {
    "BOUNDARY_ERROR",
    "WORD_ERROR",
    "OMISSION",
    "HALLUCINATION",
    "OTHER",
}


def ensure_quality_state(db: sqlite3.Connection) -> None:
    """Create version-1 persistent QA state without changing transcript data."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS quality_reviews (
            drive_id TEXT NOT NULL,
            segment_id TEXT NOT NULL,
            candidate_hash TEXT NOT NULL,
            is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
            status TEXT NOT NULL
                CHECK (status IN ('PENDING', 'CONFIRMED', 'DISMISSED')),
            flags_json TEXT NOT NULL,
            issue_types_json TEXT NOT NULL DEFAULT '[]',
            segment_start REAL NOT NULL,
            segment_end REAL NOT NULL,
            segment_text TEXT NOT NULL,
            chars_per_second REAL,
            note TEXT,
            confirmed_text TEXT,
            speaker_intent TEXT,
            first_detected_at REAL NOT NULL,
            last_detected_at REAL NOT NULL,
            decided_at REAL,
            PRIMARY KEY (drive_id, segment_id, candidate_hash)
        )"""
    )
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS quality_reviews_current_idx
           ON quality_reviews(drive_id, segment_id) WHERE is_current = 1"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS quality_reviews_status_idx
           ON quality_reviews(status, is_current, first_detected_at)"""
    )
    db.commit()


def _candidate_material(candidate: dict[str, Any]) -> dict[str, Any]:
    required_strings = ("id", "raw_text", "text")
    if any(not isinstance(candidate.get(field), str) for field in required_strings):
        raise ValueError("QA-Kandidat enthält ungültige Segmenttexte")
    flags = candidate.get("flags")
    if (
        not isinstance(flags, list)
        or not flags
        or not all(isinstance(flag, str) and flag.strip() for flag in flags)
    ):
        raise ValueError("QA-Kandidat benötigt mindestens ein gültiges Prüfsignal")
    start = candidate.get("start")
    end = candidate.get("end")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
        or not isfinite(float(start))
        or not isfinite(float(end))
        or float(start) < 0
        or float(end) < float(start)
    ):
        raise ValueError("QA-Kandidat enthält ungültige Segmentzeiten")
    density = candidate.get("chars_per_second")
    if density is not None and (
        isinstance(density, bool)
        or not isinstance(density, (int, float))
        or not isfinite(float(density))
        or float(density) < 0
    ):
        raise ValueError("QA-Kandidat enthält ungültige Textdichte")
    return {
        "id": candidate["id"],
        "start": float(start),
        "end": float(end),
        "raw_text": candidate["raw_text"],
        "text": candidate["text"],
        "flags": sorted(set(flags)),
        "chars_per_second": float(density) if density is not None else None,
    }


def candidate_hash(candidate: dict[str, Any]) -> str:
    """Bind a human decision to one exact detector result."""
    encoded = json.dumps(
        _candidate_material(candidate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sync_quality_candidates(
    db: sqlite3.Connection,
    drive_id: str,
    candidates: Iterable[dict[str, Any]],
    now: float | None = None,
) -> list[sqlite3.Row]:
    """Install the current detector results while retaining decision history."""
    if not isinstance(drive_id, str) or not drive_id.strip():
        raise ValueError("Drive-ID darf nicht leer sein")
    ensure_quality_state(db)
    moment = time.time() if now is None else now
    prepared: list[tuple[dict[str, Any], str]] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        material = _candidate_material(candidate)
        segment_id = material["id"]
        if segment_id in seen_ids:
            raise ValueError(f"Doppelte QA-Segment-ID: {segment_id}")
        seen_ids.add(segment_id)
        prepared.append((material, candidate_hash(candidate)))

    with db:
        db.execute(
            "UPDATE quality_reviews SET is_current=0 WHERE drive_id=? AND is_current=1",
            (drive_id,),
        )
        for material, fingerprint in prepared:
            existing = db.execute(
                """SELECT 1 FROM quality_reviews
                   WHERE drive_id=? AND segment_id=? AND candidate_hash=?""",
                (drive_id, material["id"], fingerprint),
            ).fetchone()
            if existing:
                db.execute(
                    """UPDATE quality_reviews
                       SET is_current=1, last_detected_at=?
                       WHERE drive_id=? AND segment_id=? AND candidate_hash=?""",
                    (moment, drive_id, material["id"], fingerprint),
                )
                continue
            db.execute(
                """INSERT INTO quality_reviews
                   (drive_id, segment_id, candidate_hash, is_current, status,
                    flags_json, issue_types_json, segment_start, segment_end,
                    segment_text, chars_per_second, first_detected_at,
                    last_detected_at)
                   VALUES (?, ?, ?, 1, 'PENDING', ?, '[]', ?, ?, ?, ?, ?, ?)""",
                (
                    drive_id,
                    material["id"],
                    fingerprint,
                    json.dumps(material["flags"], ensure_ascii=False),
                    material["start"],
                    material["end"],
                    material["text"],
                    material["chars_per_second"],
                    moment,
                    moment,
                ),
            )
    return list(db.execute(
        """SELECT * FROM quality_reviews
           WHERE drive_id=? AND is_current=1 ORDER BY segment_start, segment_id""",
        (drive_id,),
    ))


def current_quality_reviews(
    db: sqlite3.Connection,
    *,
    status: str | None = None,
    drive_id: str | None = None,
) -> list[sqlite3.Row]:
    clauses = ["is_current=1"]
    values: list[str] = []
    if status is not None:
        if status not in REVIEW_STATUSES:
            raise ValueError(f"Unbekannter QA-Status: {status}")
        clauses.append("status=?")
        values.append(status)
    if drive_id is not None:
        clauses.append("drive_id=?")
        values.append(drive_id)
    return list(db.execute(
        f"""SELECT * FROM quality_reviews WHERE {' AND '.join(clauses)}
            ORDER BY first_detected_at, drive_id, segment_start, segment_id""",
        values,
    ))


def decide_quality_review(
    db: sqlite3.Connection,
    drive_id: str,
    segment_id: str,
    fingerprint: str,
    status: str,
    *,
    issue_types: Iterable[str] = (),
    note: str | None = None,
    confirmed_text: str | None = None,
    speaker_intent: str | None = None,
    now: float | None = None,
) -> bool:
    """Persist one exact, conflict-detecting human decision."""
    if status not in {"CONFIRMED", "DISMISSED"}:
        raise ValueError("Entscheidung muss CONFIRMED oder DISMISSED sein")
    issues = sorted(set(issue_types))
    unknown = set(issues) - ISSUE_TYPES
    if unknown:
        raise ValueError(f"Unbekannte Fehlerart: {', '.join(sorted(unknown))}")
    if status == "CONFIRMED" and not issues:
        raise ValueError("CONFIRMED benötigt mindestens eine Fehlerart")
    if status == "DISMISSED" and issues:
        raise ValueError("DISMISSED darf keine Fehlerart tragen")
    if status == "DISMISSED" and (confirmed_text is not None or speaker_intent is not None):
        raise ValueError(
            "DISMISSED darf keinen bestätigten Text oder Sprecherintention tragen"
        )
    decision = {
        "status": status,
        "issue_types_json": json.dumps(issues, ensure_ascii=False),
        "note": note,
        "confirmed_text": confirmed_text,
        "speaker_intent": speaker_intent,
    }
    row = db.execute(
        """SELECT * FROM quality_reviews
           WHERE drive_id=? AND segment_id=? AND candidate_hash=? AND is_current=1""",
        (drive_id, segment_id, fingerprint),
    ).fetchone()
    if row is None:
        raise ValueError("Aktueller QA-Kandidat mit diesem Fingerabdruck fehlt")
    if row["status"] != "PENDING":
        if all(row[key] == value for key, value in decision.items()):
            return False
        raise FileExistsError(
            "QA-Kandidat besitzt bereits eine abweichende menschliche Entscheidung"
        )
    moment = time.time() if now is None else now
    with db:
        cursor = db.execute(
            """UPDATE quality_reviews
               SET status=?, issue_types_json=?, note=?, confirmed_text=?,
                   speaker_intent=?, decided_at=?
               WHERE drive_id=? AND segment_id=? AND candidate_hash=?
                 AND is_current=1 AND status='PENDING'""",
            (
                status,
                decision["issue_types_json"],
                note,
                confirmed_text,
                speaker_intent,
                moment,
                drive_id,
                segment_id,
                fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("QA-Entscheidung wurde gleichzeitig verändert")
    return True


def open_readonly_state(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"Pipeline-Datenbank fehlt: {path}")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _serializable(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["flags"] = json.loads(result.pop("flags_json"))
    result["issue_types"] = json.loads(result.pop("issue_types_json"))
    result["is_current"] = bool(result["is_current"])
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persistente Qualitätsprüfungen anzeigen oder ausdrücklich entscheiden."
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--register-segments", type=Path, action="append", default=[],
        metavar="SEGMENTE_JSON",
        help="Auffälligkeiten aus einer validierten Segmentdatei registrieren",
    )
    parser.add_argument("--drive-id")
    parser.add_argument("--segment-id")
    parser.add_argument("--candidate-hash")
    parser.add_argument("--status", choices=sorted(REVIEW_STATUSES))
    decision = parser.add_mutually_exclusive_group()
    decision.add_argument("--confirm", action="store_true")
    decision.add_argument("--dismiss", action="store_true")
    parser.add_argument("--issue", action="append", default=[], choices=sorted(ISSUE_TYPES))
    parser.add_argument("--note")
    parser.add_argument("--confirmed-text")
    parser.add_argument("--speaker-intent")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _validate_operation(args: argparse.Namespace) -> None:
    deciding = args.confirm or args.dismiss
    if deciding and not all((args.drive_id, args.segment_id, args.candidate_hash)):
        raise ValueError(
            "Bestätigen erfordert --drive-id, --segment-id und --candidate-hash"
        )
    if not deciding and any((args.issue, args.note, args.confirmed_text, args.speaker_intent)):
        raise ValueError("Entscheidungsdetails erfordern --confirm oder --dismiss")
    if args.register_segments and deciding:
        raise ValueError("Registrieren und Entscheiden sind getrennte Schritte")
    if args.register_segments and any(
        (args.drive_id, args.segment_id, args.candidate_hash, args.status)
    ):
        raise ValueError("Registrieren darf nicht mit Anzeigefiltern kombiniert werden")
    if not deciding and args.candidate_hash is not None:
        raise ValueError("--candidate-hash wird nur für eine Entscheidung verwendet")
    if deciding and args.status is not None:
        raise ValueError("--status ist ein Anzeigefilter, keine Entscheidung")


def _print_rows(rows: list[sqlite3.Row], as_json: bool) -> None:
    items = [_serializable(row) for row in rows]
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        print("Keine aktuellen QA-Kandidaten.")
        return
    for item in items:
        issues = ",".join(item["issue_types"]) or "-"
        print(
            f"{item['status']} id={item['drive_id']} segment={item['segment_id']} "
            f"hash={item['candidate_hash']} issues={issues}"
        )
        print(f"  {item['segment_start']:.3f}–{item['segment_end']:.3f}s {item['segment_text']}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        _validate_operation(args)
        deciding = args.confirm or args.dismiss
        if args.register_segments or deciding:
            with open_state(args.state) as db:
                ensure_quality_state(db)
                if args.register_segments:
                    for path in args.register_segments:
                        payload = load_segment_metadata(path)
                        source_id = payload.get("source_id")
                        if not isinstance(source_id, str) or not source_id.strip():
                            raise ValueError(f"Segmentdatei enthält keine Drive-ID: {path}")
                        rows = sync_quality_candidates(
                            db, source_id, find_suspicious_segments(payload)
                        )
                        print(f"Registriert id={source_id}: {len(rows)} aktuelle Kandidaten")
                else:
                    changed = decide_quality_review(
                        db,
                        args.drive_id,
                        args.segment_id,
                        args.candidate_hash,
                        "CONFIRMED" if args.confirm else "DISMISSED",
                        issue_types=args.issue,
                        note=args.note,
                        confirmed_text=args.confirmed_text,
                        speaker_intent=args.speaker_intent,
                    )
                    print("QA-Entscheidung gespeichert." if changed else "QA-Entscheidung bereits identisch vorhanden.")
            return 0

        with open_readonly_state(args.state) as db:
            rows = current_quality_reviews(
                db, status=args.status, drive_id=args.drive_id
            )
            if args.segment_id is not None:
                rows = [row for row in rows if row["segment_id"] == args.segment_id]
            _print_rows(rows, args.json)
        return 0
    except (FileExistsError, OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"[X] QA-Status fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
