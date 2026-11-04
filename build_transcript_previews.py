#!/usr/bin/env python3
"""Verify one frozen transcript-repair plan before preview generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

from plan_transcript_repairs import (
    DEFAULT_STATE,
    PLAN_SCHEMA_VERSION,
    PLAN_TYPE,
    REPAIR_ACTIONS,
    RECORDING_STATUSES,
    build_repair_plan,
    repair_plan_sha256,
)
from quality_reviews import open_readonly_state


class PlanFormatError(ValueError):
    """The frozen plan or its explicit hash confirmation is invalid."""


class PlanHoldError(ValueError):
    """The plan contains at least one entry that is not safe to process."""


class CurrentPlanMismatch(ValueError):
    """The current read-only state no longer reproduces the frozen plan."""


def valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def load_repair_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    """Load and hash-verify one frozen schema-3 repair plan."""
    if not valid_sha256(expected_sha256):
        raise PlanFormatError("--expect-plan-sha256 ist keine gültige SHA-256")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanFormatError(f"Reparaturplan ist nicht lesbar: {path}") from exc
    if not isinstance(payload, dict):
        raise PlanFormatError("Reparaturplan muss ein JSON-Objekt sein")
    if payload.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise PlanFormatError(
            f"Reparaturplan benötigt Schema {PLAN_SCHEMA_VERSION}"
        )
    if payload.get("plan_type") != PLAN_TYPE:
        raise PlanFormatError("Unbekannter Reparaturplan-Typ")
    stored_hash = payload.get("plan_sha256")
    if not valid_sha256(stored_hash):
        raise PlanFormatError("Reparaturplan enthält keine gültige plan_sha256")
    calculated_hash = repair_plan_sha256(payload)
    if stored_hash != calculated_hash:
        raise PlanFormatError("Reparaturplan-Hash stimmt nicht mit dem Inhalt überein")
    if stored_hash != expected_sha256:
        raise PlanFormatError("Reparaturplan-Hash stimmt nicht mit --expect überein")
    return payload


def validate_plan_gate(plan: dict[str, Any]) -> set[str]:
    """Reject malformed or mixed HOLD plans before any output is considered."""
    recordings = plan.get("recordings")
    if not isinstance(recordings, list) or not recordings:
        raise PlanFormatError("Reparaturplan enthält keine Aufnahmen")
    drive_ids: set[str] = set()
    holds: list[str] = []
    for index, recording in enumerate(recordings):
        label = f"recordings[{index}]"
        if not isinstance(recording, dict):
            raise PlanFormatError(f"{label} ist kein Objekt")
        drive_id = recording.get("drive_id")
        if not isinstance(drive_id, str) or not drive_id.strip():
            raise PlanFormatError(f"{label}.drive_id ist ungültig")
        if drive_id in drive_ids:
            raise PlanFormatError(f"Doppelte Drive-ID im Plan: {drive_id}")
        drive_ids.add(drive_id)
        status = recording.get("status")
        if status not in RECORDING_STATUSES:
            raise PlanFormatError(f"Ungültiger Aufnahmestatus für {drive_id}")
        version_status = recording.get("transcript_version_status")
        if version_status not in {"MATCHED", "MISMATCH", "UNVERIFIED"}:
            raise PlanFormatError(f"Ungültiger Versionsstatus für {drive_id}")
        if status == "CURATION_REQUIRED" or version_status != "MATCHED":
            holds.append(
                f"{drive_id}: status={status}, version={version_status}"
            )
        reviews = recording.get("reviews")
        if not isinstance(reviews, list) or not reviews:
            raise PlanFormatError(f"Aufnahme {drive_id} enthält keine Reviews")
        for review_index, review in enumerate(reviews):
            review_label = f"{drive_id}/reviews[{review_index}]"
            if not isinstance(review, dict):
                raise PlanFormatError(f"{review_label} ist kein Objekt")
            action = review.get("action")
            if action not in REPAIR_ACTIONS:
                raise PlanFormatError(f"Ungültige Aktion für {review_label}")
            review_version = review.get("transcript_version_status")
            if review_version not in {"MATCHED", "MISMATCH", "UNVERIFIED"}:
                raise PlanFormatError(
                    f"Ungültiger Review-Versionsstatus für {review_label}"
                )
            if action == "CURATION_REQUIRED" or review_version != "MATCHED":
                holds.append(
                    f"{review_label}: action={action}, version={review_version}"
                )
    if holds:
        raise PlanHoldError(
            "Reparaturplan enthält HOLD-Fälle; der gesamte Lauf bleibt gesperrt: "
            + "; ".join(holds)
        )
    return drive_ids


def verify_current_state(
    plan: dict[str, Any], state_path: Path, drive_ids: set[str]
) -> None:
    """Rebuild the selected plan read-only and require byte-semantic identity."""
    with open_readonly_state(state_path) as db:
        current = build_repair_plan(db, drive_ids)
    if current["plan_sha256"] != plan["plan_sha256"]:
        raise CurrentPlanMismatch(
            "Aktueller QA-/Transkriptzustand reproduziert den eingefrorenen Plan nicht"
        )


def check_plan(
    plan_path: Path, expected_sha256: str, state_path: Path
) -> dict[str, Any]:
    """Perform every currently implemented read-only preview prerequisite."""
    plan = load_repair_plan(plan_path, expected_sha256)
    drive_ids = validate_plan_gate(plan)
    verify_current_state(plan, state_path, drive_ids)
    return {
        "check": "PASSED",
        "read_only": True,
        "plan_sha256": plan["plan_sha256"],
        "recording_count": len(drive_ids),
        "preview_written": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Eingefrorenen Reparaturplan gegen Hash, HOLD-Regeln und den "
            "aktuellen Nur-Lese-Zustand prüfen."
        )
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expect-plan-sha256", required=True)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Alle Prüfungen ausführen; erzeugt keine Verzeichnisse oder Dateien",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.check:
        print(
            "[X] Vorschauerzeugung ist in diesem Sicherheitsbaustein noch nicht "
            "freigegeben; --check verwenden",
            file=sys.stderr,
        )
        return 2
    try:
        result = check_plan(
            args.plan,
            args.expect_plan_sha256,
            args.state,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                "Reparaturvorschau-Prüfung bestanden: "
                f"plan_sha256={result['plan_sha256']} "
                f"Aufnahmen={result['recording_count']} (keine Ausgabe geschrieben)"
            )
        return 0
    except PlanFormatError as exc:
        print(f"[X] Reparaturplan ungültig: {exc}", file=sys.stderr)
        return 2
    except PlanHoldError as exc:
        print(f"[X] Reparaturplan gesperrt: {exc}", file=sys.stderr)
        return 3
    except CurrentPlanMismatch as exc:
        print(f"[X] Reparaturplan veraltet: {exc}", file=sys.stderr)
        return 4
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"[X] Reparaturvorschau-Prüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
