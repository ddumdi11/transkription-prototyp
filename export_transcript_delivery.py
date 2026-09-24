#!/usr/bin/env python3
"""Build a verified local Atlas delivery from confirmed session routing."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

from inbox_watcher import setup_logging
from plan_session_routing import (
    SESSION_ID_PATTERN,
    fsync_directory,
    manifest_lock,
    routing_plan_hash,
)
from plan_transcript_sessions import STATE_DIR, open_readonly_state
from project_catalog import load_project_catalog
from publish_transcripts import sha256_file


DEFAULT_DELIVERY_DIR = Path("staging/project-deliveries")


def delivery_directory_name(delivery_id: str) -> str:
    """Return the cross-platform directory name for a canonical delivery ID."""
    return delivery_id.replace(":", "__")


def load_routing_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Routingmanifest kann nicht gelesen werden: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Routingmanifest muss ein Objekt sein")
    if payload.get("schema_version") != 1:
        raise ValueError("Routingmanifest hat keine unterstützte schema_version")
    if payload.get("manifest_type") != "confirmed_session_routing":
        raise ValueError("Routingmanifest ist nicht ausdrücklich bestätigt")
    if payload.get("confirmation") != "explicit_cli":
        raise ValueError("Routingmanifest besitzt keine ausdrückliche CLI-Bestätigung")
    session = payload.get("session")
    if not isinstance(session, dict):
        raise ValueError("Routingmanifest enthält keine Sitzung")
    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("Routingmanifest enthält keine gültige Sitzungs-ID")
    expected_hash = routing_plan_hash(session)
    if payload.get("routing_plan_sha256") != expected_hash:
        raise ValueError("Routingmanifest enthält einen abweichenden Plan-Hash")
    expected_key = f"{session.get('session_id')}:{expected_hash[:16]}"
    if payload.get("manifest_key") != expected_key:
        raise ValueError("Routingmanifest enthält einen abweichenden Manifest-Schlüssel")
    confirmed_at = payload.get("confirmed_at")
    if not isinstance(confirmed_at, str):
        raise ValueError("Routingmanifest enthält keinen Bestätigungszeitpunkt")
    try:
        parsed = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Bestätigungszeitpunkt ist ungültig") from exc
    if parsed.tzinfo is None:
        raise ValueError("Bestätigungszeitpunkt benötigt eine Zeitzone")
    return payload


def published_asset(db: sqlite3.Connection, drive_id: str) -> sqlite3.Row:
    row = db.execute(
        """SELECT p.remote_id, p.remote_path, p.local_hash, p.size, p.local_path,
                  j.transcript_path
           FROM published_transcripts p
           JOIN transcription_jobs j USING (drive_id)
           WHERE p.drive_id = ?""",
        (drive_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Veröffentlichtes Transkript fehlt für Audio-ID {drive_id}")
    return row


def verified_asset(db: sqlite3.Connection, recording: dict[str, Any]) -> dict[str, Any]:
    drive_id = recording.get("drive_id")
    if not isinstance(drive_id, str) or not drive_id:
        raise ValueError("Aufnahme enthält keine gültige Drive-ID")
    row = published_asset(db, drive_id)
    local_path = Path(str(row["local_path"]))
    if not local_path.is_file():
        raise ValueError(f"Lokales kanonisches Transkript fehlt: {local_path}")
    size = local_path.stat().st_size
    digest = sha256_file(local_path)
    if size != int(row["size"]) or digest != str(row["local_hash"]):
        raise ValueError(f"Lokales Transkript weicht vom Veröffentlichungsstand ab: {local_path}")
    if str(row["transcript_path"]) != str(local_path):
        raise ValueError(f"Pipeline- und Veröffentlichungsdatei weichen ab: {drive_id}")
    remote_id = str(row["remote_id"])
    remote_path = str(row["remote_path"])
    if not remote_id or not remote_path:
        raise ValueError(f"Kanonische Remote-Identität fehlt: {drive_id}")
    if (Path(remote_path).name != remote_path or remote_path in {".", ".."}
            or "/" in remote_path or "\\" in remote_path):
        raise ValueError(f"Unsicherer kanonischer Remotepfad: {remote_path!r}")
    return {
        "asset_id": f"transcript:{remote_id}",
        "kind": "canonical_transcript_copy",
        "relative_path": f"transcripts/{remote_path}",
        "canonical_drive_id": remote_id,
        "source_audio_drive_id": drive_id,
        "sha256": digest,
        "size": size,
        "_local_path": str(local_path),
    }


def assignment(project: dict[str, Any]) -> dict[str, Any]:
    project_id = project.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("Routingprojekt enthält keine stabile project_id")
    whole_session = project.get("whole_session") is True
    scopes = project.get("scopes")
    segments = project.get("segments")
    if (not isinstance(scopes, list) or not scopes
            or not all(isinstance(item, str) and item for item in scopes)):
        raise ValueError(f"Routingprojekt {project_id!r} enthält ungültige scopes")
    if len(scopes) != len(set(scopes)):
        raise ValueError(f"Routingprojekt {project_id!r} enthält doppelte scopes")
    if not isinstance(segments, list):
        raise ValueError(f"Routingprojekt {project_id!r} enthält ungültige Segmente")
    references = []
    seen = set()
    for item in segments:
        if not isinstance(item, dict):
            raise ValueError(f"Routingprojekt {project_id!r} enthält ein ungültiges Segment")
        source_id = item.get("drive_id")
        segment_id = item.get("segment_id")
        if not isinstance(source_id, str) or not isinstance(segment_id, str):
            raise ValueError(f"Routingprojekt {project_id!r} enthält unvollständige Segmentbelege")
        key = (source_id, segment_id)
        if key not in seen:
            seen.add(key)
            references.append({
                "source_audio_drive_id": source_id,
                "segment_id": segment_id,
            })
    if not whole_session and not references:
        raise ValueError(f"Routingprojekt {project_id!r} besitzt keinen Geltungsbereich")
    return {
        "project_id": project_id,
        "scope": "whole_session" if whole_session else "segments",
        "reasons": list(scopes),
        "segments": references,
    }


def build_delivery(
    db: sqlite3.Connection,
    routing_manifest: dict[str, Any],
    catalog: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Path]]:
    session = routing_manifest["session"]
    recordings = session.get("recordings")
    projects = session.get("projects")
    if not isinstance(recordings, list) or not recordings:
        raise ValueError("Bestätigte Sitzung enthält keine Aufnahmen")
    if not isinstance(projects, list) or not projects:
        raise ValueError("Bestätigte Sitzung enthält keine Projektzuordnungen")

    assets_with_paths = [verified_asset(db, item) for item in recordings]
    local_paths = {
        item["relative_path"]: Path(item.pop("_local_path"))
        for item in assets_with_paths
    }
    assignments = [assignment(project) for project in projects]
    known_projects = {project["project_id"] for project in catalog["projects"]}
    unknown = sorted({item["project_id"] for item in assignments} - known_projects)
    if unknown:
        raise ValueError("Projekt-IDs fehlen im Atlas-Katalog: " + ", ".join(unknown))
    project_ids = [item["project_id"] for item in assignments]
    if len(project_ids) != len(set(project_ids)):
        raise ValueError("Lieferung enthält eine project_id mehrfach")
    source_ids = {item["source_audio_drive_id"] for item in assets_with_paths}
    invalid_segment_sources = sorted({
        segment["source_audio_drive_id"]
        for item in assignments
        for segment in item["segments"]
        if segment["source_audio_drive_id"] not in source_ids
    })
    if invalid_segment_sources:
        raise ValueError(
            "Segmentbelege verweisen nicht auf Liefer-Assets: "
            + ", ".join(invalid_segment_sources)
        )

    delivery_id = routing_manifest["manifest_key"]
    delivery = {
        "schema_version": 1,
        "manifest_type": "z_system_transcript_delivery",
        "delivery_id": delivery_id,
        "created_at": routing_manifest["confirmed_at"],
        "project_catalog": {
            "catalog_id": catalog["catalog_id"],
            "catalog_revision": catalog["catalog_revision"],
            "catalog_hash": catalog["catalog_hash"],
        },
        "source_routing_manifest": {
            "manifest_key": routing_manifest["manifest_key"],
            "sha256": routing_manifest["routing_plan_sha256"],
        },
        "session": {
            "session_id": session["session_id"],
            "started_at": session["start"],
            "ended_at": session["end"],
        },
        "assets": assets_with_paths,
        "assignments": assignments,
    }
    return delivery, local_paths


def serialized_delivery(delivery: dict[str, Any]) -> str:
    return json.dumps(delivery, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def existing_delivery_matches(
    target: Path, delivery: dict[str, Any], local_paths: dict[str, Path]
) -> bool:
    manifest = target / "delivery.json"
    try:
        if manifest.read_text(encoding="utf-8") != serialized_delivery(delivery):
            return False
        for relative_path, source in local_paths.items():
            installed = target / relative_path
            if (not installed.is_file()
                    or installed.stat().st_size != source.stat().st_size
                    or sha256_file(installed) != sha256_file(source)):
                return False
    except (OSError, UnicodeError):
        return False
    return True


def install_delivery(
    delivery: dict[str, Any], local_paths: dict[str, Path], output_dir: Path
) -> tuple[Path, bool]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / delivery_directory_name(delivery["delivery_id"])
    with manifest_lock(target):
        if target.exists():
            if existing_delivery_matches(target, delivery, local_paths):
                return target, False
            raise FileExistsError(f"Abweichende Lieferung existiert bereits: {target}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=output_dir))
        try:
            for relative_path, source in local_paths.items():
                destination = temporary / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                with destination.open("rb") as handle:
                    os.fsync(handle.fileno())
                if sha256_file(destination) != sha256_file(source):
                    raise ValueError(f"Kopierprüfung fehlgeschlagen: {relative_path}")
            manifest = temporary / "delivery.json"
            manifest.write_text(serialized_delivery(delivery), encoding="utf-8")
            with manifest.open("rb") as handle:
                os.fsync(handle.fileno())
            fsync_directory(temporary / "transcripts")
            fsync_directory(temporary)
            temporary.replace(target)
            fsync_directory(output_dir)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return target, True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bestätigtes Sitzungsrouting als lokale Atlas-Lieferung vorbereiten"
    )
    parser.add_argument("--routing-manifest", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DELIVERY_DIR)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_logging(args.state_dir)
    try:
        routing_manifest = load_routing_manifest(args.routing_manifest)
        catalog = load_project_catalog(args.catalog)
        with open_readonly_state(args.state_dir / "state.sqlite3") as db:
            delivery, local_paths = build_delivery(db, routing_manifest, catalog)
        installed = None
        created = False
        if args.confirm:
            installed, created = install_delivery(delivery, local_paths, args.output_dir)
        result = {
            "dry_run": not args.confirm,
            "delivery": delivery,
            "installed_path": str(installed) if installed else None,
            "created": created,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            logger.info(
                "Lieferung: mode=%s id=%s assets=%d assignments=%d",
                "confirm" if args.confirm else "dry-run",
                delivery["delivery_id"], len(delivery["assets"]),
                len(delivery["assignments"]),
            )
            if installed:
                logger.info(
                    "Lokales Lieferpaket %s: %s",
                    "erstellt" if created else "bereits identisch", installed,
                )
            else:
                logger.info("Dry-Run beendet (keine Datei- oder Drive-Änderung)")
        return 0
    except Exception as exc:
        logger.exception("Lieferung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
