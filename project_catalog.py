#!/usr/bin/env python3
"""Validate a versioned Atlas project catalog without modifying local state."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA_VERSION = 1
EXPORT_TYPE = "z_system_project_catalog"
PROJECT_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CATALOG_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
PROJECT_STATUSES = {"active", "paused", "archived", "candidate"}


class ProjectCatalogError(ValueError):
    """The Atlas catalog violates the agreed v1 contract."""


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectCatalogError(f"{path} muss ein Objekt sein")
    return value


def _exact_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - required)
    if missing:
        raise ProjectCatalogError(f"{path}: fehlende Felder: {', '.join(missing)}")
    if unexpected:
        raise ProjectCatalogError(
            f"{path}: unbekannte Felder: {', '.join(unexpected)}"
        )


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectCatalogError(f"{path} muss ein nichtleerer Text sein")
    return value


def _text_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ProjectCatalogError(f"{path} muss eine Textliste sein")
    result = [_text(item, f"{path}[{index}]") for index, item in enumerate(value)]
    folded = [item.casefold() for item in result]
    if len(folded) != len(set(folded)):
        raise ProjectCatalogError(f"{path} enthält doppelte Begriffe")
    return result


def _timestamp(value: Any, path: str) -> str:
    text = _text(value, path)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectCatalogError(f"{path} ist kein ISO-8601-Zeitpunkt") from exc
    if parsed.tzinfo is None:
        raise ProjectCatalogError(f"{path} benötigt eine Zeitzone")
    if parsed.utcoffset() != timedelta(0):
        raise ProjectCatalogError(f"{path} muss in UTC angegeben sein")
    return text


def canonical_projects(projects: list[dict[str, Any]]) -> bytes:
    """Return the canonical bytes covered by catalog_hash."""
    return json.dumps(
        sorted(projects, key=lambda item: item["project_id"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def project_catalog_hash(projects: list[dict[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(canonical_projects(projects)).hexdigest()


def validate_project_catalog(payload: Any) -> dict[str, Any]:
    """Validate structure and cross-field invariants, then return the catalog."""
    catalog = _object(payload, "catalog")
    _exact_keys(catalog, {
        "schema_version", "export_type", "catalog_id", "catalog_revision",
        "exported_at", "source", "catalog_hash", "projects",
    }, "catalog")

    if type(catalog["schema_version"]) is not int:
        raise ProjectCatalogError("schema_version muss eine Ganzzahl sein")
    if catalog["schema_version"] != SCHEMA_VERSION:
        raise ProjectCatalogError(
            f"Nicht unterstützte schema_version: {catalog['schema_version']!r}"
        )
    if catalog["export_type"] != EXPORT_TYPE:
        raise ProjectCatalogError(
            f"Nicht unterstützter export_type: {catalog['export_type']!r}"
        )
    _text(catalog["catalog_id"], "catalog_id")
    _timestamp(catalog["catalog_revision"], "catalog_revision")
    _timestamp(catalog["exported_at"], "exported_at")

    source = _object(catalog["source"], "source")
    _exact_keys(source, {"application", "instance_id"}, "source")
    _text(source["application"], "source.application")
    _text(source["instance_id"], "source.instance_id")

    stored_hash = _text(catalog["catalog_hash"], "catalog_hash")
    if not CATALOG_HASH_PATTERN.fullmatch(stored_hash):
        raise ProjectCatalogError("catalog_hash muss sha256:<64 Hexzeichen> sein")

    projects = catalog["projects"]
    if not isinstance(projects, list) or not projects:
        raise ProjectCatalogError("projects muss eine nichtleere Liste sein")

    project_ids: set[str] = set()
    relation_targets: list[tuple[str, str]] = []
    for index, raw_project in enumerate(projects):
        path = f"projects[{index}]"
        project = _object(raw_project, path)
        _exact_keys(project, {
            "project_id", "name", "status", "category", "aliases",
            "relations", "routing",
        }, path)

        project_id = _text(project["project_id"], f"{path}.project_id")
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ProjectCatalogError(
                f"{path}.project_id ist kein stabiler Vorhaben-Slug: {project_id!r}"
            )
        if project_id in project_ids:
            raise ProjectCatalogError(f"Doppelte project_id: {project_id!r}")
        project_ids.add(project_id)

        _text(project["name"], f"{path}.name")
        status = project["status"]
        if not isinstance(status, str) or status not in PROJECT_STATUSES:
            raise ProjectCatalogError(f"{path}.status ist ungültig: {status!r}")
        category = project["category"]
        if category is not None:
            _text(category, f"{path}.category")
        if status != "candidate" and category is None:
            raise ProjectCatalogError(
                f"{path}.category darf nur bei candidate null sein"
            )
        _text_list(project["aliases"], f"{path}.aliases")

        relations = project["relations"]
        if not isinstance(relations, list):
            raise ProjectCatalogError(f"{path}.relations muss eine Liste sein")
        seen_relations: set[tuple[str, str]] = set()
        for relation_index, raw_relation in enumerate(relations):
            relation_path = f"{path}.relations[{relation_index}]"
            relation = _object(raw_relation, relation_path)
            _exact_keys(relation, {"type", "project_id"}, relation_path)
            relation_type = _text(relation["type"], f"{relation_path}.type")
            target_id = _text(
                relation["project_id"], f"{relation_path}.project_id"
            )
            relation_key = (relation_type, target_id)
            if relation_key in seen_relations:
                raise ProjectCatalogError(
                    f"{relation_path} ist eine doppelte Beziehung"
                )
            seen_relations.add(relation_key)
            relation_targets.append((relation_path, target_id))

        routing = _object(project["routing"], f"{path}.routing")
        _exact_keys(routing, {"enabled", "terms", "exact_terms"}, f"{path}.routing")
        if type(routing["enabled"]) is not bool:
            raise ProjectCatalogError(f"{path}.routing.enabled muss boolesch sein")
        terms = _text_list(routing["terms"], f"{path}.routing.terms")
        exact_terms = _text_list(
            routing["exact_terms"], f"{path}.routing.exact_terms"
        )
        invalid_exact = [term for term in exact_terms if re.search(r"\s", term)]
        if invalid_exact:
            raise ProjectCatalogError(
                f"{path}.routing.exact_terms enthält Mehrwortbegriffe: "
                + ", ".join(invalid_exact)
            )
        folded_terms = {term.casefold() for term in terms}
        unknown_exact = [
            term for term in exact_terms if term.casefold() not in folded_terms
        ]
        if unknown_exact:
            raise ProjectCatalogError(
                f"{path}.routing.exact_terms ist keine Teilmenge von terms: "
                + ", ".join(unknown_exact)
            )

    for relation_path, target_id in relation_targets:
        if target_id not in project_ids:
            raise ProjectCatalogError(
                f"{relation_path} verweist auf unbekanntes Projekt {target_id!r}"
            )

    computed_hash = project_catalog_hash(projects)
    if stored_hash != computed_hash:
        raise ProjectCatalogError(
            f"catalog_hash stimmt nicht: gespeichert={stored_hash}, "
            f"berechnet={computed_hash}"
        )
    return catalog


def load_project_catalog(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectCatalogError(f"Katalog kann nicht gelesen werden: {exc}") from exc
    return validate_project_catalog(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atlas-Projektkatalog v1 ausschließlich lesend validieren"
    )
    parser.add_argument("catalog", type=Path, help="Lokale JSON-Katalogdatei")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        catalog = load_project_catalog(args.catalog)
    except ProjectCatalogError as exc:
        print(f"UNGÜLTIG: {exc}")
        return 1
    print(
        "GÜLTIG: "
        f"catalog_id={catalog['catalog_id']!r} "
        f"projects={len(catalog['projects'])} "
        f"hash={catalog['catalog_hash']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
