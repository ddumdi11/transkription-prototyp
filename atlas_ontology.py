#!/usr/bin/env python3
"""Validate an Atlas ontology export without modifying local state."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, NoReturn

from project_catalog import ProjectCatalogError, load_project_catalog


SCHEMA_VERSION = 1
EXPORT_TYPE = "z_system_ontology"
PRODUCTION_ONTOLOGY_ID = "z-ontologie"
PRODUCTION_APPLICATION = "Atlas"
PRODUCTION_INSTANCE_ID = "atlas-primary"
MAX_SAFE_INTEGER = (1 << 53) - 1
CONCEPT_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
CONCEPT_TYPES = frozenset({"dachbereich", "dach", "thema", "quellenteil"})
CONCEPT_STATUSES = frozenset({"active", "deprecated"})
HEAD_KEYS = {
    "schema_version", "export_type", "ontology_id", "ontology_revision",
    "exported_at", "source", "stamm_version", "ontology_hash", "concepts",
}
CONCEPT_KEYS = {
    "concept_id", "type", "pref_label", "alt_labels", "hidden_labels",
    "definition", "status", "deprecated_at", "broader", "narrower",
    "related",
}


class OntologyError(ValueError):
    """The ontology violates the Atlas ontology export v1 contract."""


def _fail_json(message: str) -> NoReturn:
    raise OntologyError(message)


def _parse_constant(value: str) -> NoReturn:
    _fail_json(f"Nicht endliche JSON-Zahl ist unzulässig: {value}")


def _parse_float(value: str) -> NoReturn:
    _fail_json(f"Gleitkommazahl ist unzulässig: {value}")


def _parse_int(value: str) -> int:
    number = int(value)
    if abs(number) > MAX_SAFE_INTEGER:
        _fail_json(f"Ganzzahl liegt außerhalb ±(2^53-1): {value}")
    return number


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail_json(f"Doppelter JSON-Schlüssel: {key!r}")
        if not key.isascii():
            _fail_json(f"JSON-Feldname ist nicht ASCII: {key!r}")
        result[key] = value
    return result


def parse_ontology_json(text: str) -> Any:
    """Parse the contract's strict JSON subset and reject unsafe values."""
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_pairs_object,
            parse_constant=_parse_constant,
            parse_float=_parse_float,
            parse_int=_parse_int,
        )
    except OntologyError:
        raise
    except json.JSONDecodeError as exc:
        raise OntologyError(f"Ontologie ist kein gültiges JSON: {exc}") from exc
    _reject_surrogates(payload, "ontology")
    return payload


def _reject_surrogates(value: Any, path: str) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise OntologyError(f"{path} enthält ein einzelnes Surrogat")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_surrogates(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_surrogates(item, f"{path}.{key}")


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OntologyError(f"{path} muss ein Objekt sein")
    return value


def _exact_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - required)
    if missing:
        raise OntologyError(f"{path}: fehlende Felder: {', '.join(missing)}")
    if unexpected:
        raise OntologyError(f"{path}: unbekannte Felder: {', '.join(unexpected)}")


def _text(value: Any, path: str, *, normalized: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise OntologyError(f"{path} muss ein nichtleerer Text sein")
    if value != value.strip():
        raise OntologyError(f"{path} enthält Rand-Leerzeichen")
    if normalized and unicodedata.normalize("NFC", value) != value:
        raise OntologyError(f"{path} ist nicht NFC-normalisiert")
    return value


def _timestamp(value: Any, path: str) -> str:
    text = _text(value, path)
    if not TIMESTAMP_PATTERN.fullmatch(text):
        raise OntologyError(f"{path} muss YYYY-MM-DDThh:mm:ssZ entsprechen")
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise OntologyError(f"{path} ist kein gültiger UTC-Zeitpunkt") from exc
    return text


def _date(value: Any, path: str) -> str:
    text = _text(value, path)
    if not DATE_PATTERN.fullmatch(text):
        raise OntologyError(f"{path} muss YYYY-MM-DD entsprechen")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise OntologyError(f"{path} ist kein gültiges Datum") from exc
    return text


def _sorted_text_list(
    value: Any,
    path: str,
    *,
    slugs: bool = False,
    normalized: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise OntologyError(f"{path} muss eine Liste sein")
    result = [
        _text(item, f"{path}[{index}]", normalized=normalized)
        for index, item in enumerate(value)
    ]
    if len(result) != len(set(result)):
        raise OntologyError(f"{path} enthält Dubletten")
    if result != sorted(result):
        raise OntologyError(f"{path} ist nicht nach Unicode-Codepoints sortiert")
    if slugs:
        for index, item in enumerate(result):
            if not CONCEPT_ID_PATTERN.fullmatch(item):
                raise OntologyError(f"{path}[{index}] ist kein stabiler Begriff-Slug")
    return result


def canonical_concepts(concepts: list[dict[str, Any]]) -> bytes:
    """Return the canonical UTF-8 bytes covered by ontology_hash."""
    return json.dumps(
        sorted(concepts, key=lambda concept: concept["concept_id"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def ontology_hash(concepts: list[dict[str, Any]]) -> str:
    """Return the v1 SHA-256 identifier for a concept list."""
    return "sha256:" + hashlib.sha256(canonical_concepts(concepts)).hexdigest()


def _check_cycle(concepts: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(concept_id: str) -> None:
        if concept_id in visiting:
            raise OntologyError(f"broader enthält einen Zyklus bei {concept_id!r}")
        if concept_id in visited:
            return
        visiting.add(concept_id)
        for broader_id in concepts[concept_id]["broader"]:
            visit(broader_id)
        visiting.remove(concept_id)
        visited.add(concept_id)

    for concept_id in concepts:
        visit(concept_id)


def validate_ontology(payload: Any) -> dict[str, Any]:
    """Validate an already parsed Atlas ontology export v1 document."""
    ontology = _object(payload, "ontology")
    _exact_keys(ontology, HEAD_KEYS, "ontology")
    if type(ontology["schema_version"]) is not int:
        raise OntologyError("schema_version muss eine Ganzzahl sein")
    if ontology["schema_version"] != SCHEMA_VERSION:
        raise OntologyError(
            f"Nicht unterstützte schema_version: {ontology['schema_version']!r}"
        )
    if ontology["export_type"] != EXPORT_TYPE:
        raise OntologyError(
            f"Nicht unterstützter export_type: {ontology['export_type']!r}"
        )
    _text(ontology["ontology_id"], "ontology_id")
    revision = _timestamp(ontology["ontology_revision"], "ontology_revision")
    exported_at = _timestamp(ontology["exported_at"], "exported_at")
    if revision != exported_at:
        raise OntologyError("ontology_revision muss exported_at entsprechen")
    source = _object(ontology["source"], "source")
    _exact_keys(source, {"application", "instance_id"}, "source")
    _text(source["application"], "source.application")
    _text(source["instance_id"], "source.instance_id")
    _text(ontology["stamm_version"], "stamm_version")
    stored_hash = _text(ontology["ontology_hash"], "ontology_hash")
    if not HASH_PATTERN.fullmatch(stored_hash):
        raise OntologyError("ontology_hash muss sha256:<64 Hexzeichen> sein")

    raw_concepts = ontology["concepts"]
    if not isinstance(raw_concepts, list):
        raise OntologyError("concepts muss eine Liste sein")
    concepts: dict[str, dict[str, Any]] = {}
    for index, raw_concept in enumerate(raw_concepts):
        path = f"concepts[{index}]"
        concept = _object(raw_concept, path)
        _exact_keys(concept, CONCEPT_KEYS, path)
        concept_id = _text(concept["concept_id"], f"{path}.concept_id")
        if not CONCEPT_ID_PATTERN.fullmatch(concept_id):
            raise OntologyError(f"{path}.concept_id ist kein stabiler Begriff-Slug")
        if concept_id in concepts:
            raise OntologyError(f"Doppelte concept_id: {concept_id!r}")
        concepts[concept_id] = concept

        concept_type = concept["type"]
        if not isinstance(concept_type, str) or concept_type not in CONCEPT_TYPES:
            raise OntologyError(f"{path}.type ist unbekannt: {concept_type!r}")
        pref_label = _text(
            concept["pref_label"], f"{path}.pref_label", normalized=True
        )
        alt_labels = _sorted_text_list(
            concept["alt_labels"], f"{path}.alt_labels", normalized=True
        )
        hidden_labels = _sorted_text_list(
            concept["hidden_labels"], f"{path}.hidden_labels", normalized=True
        )
        all_labels = [pref_label, *alt_labels, *hidden_labels]
        if len(all_labels) != len(set(all_labels)):
            raise OntologyError(f"{path}: ein Label kommt mehrfach vor")
        if concept["definition"] is not None:
            _text(concept["definition"], f"{path}.definition", normalized=True)

        status = concept["status"]
        if not isinstance(status, str) or status not in CONCEPT_STATUSES:
            raise OntologyError(f"{path}.status ist unbekannt: {status!r}")
        deprecated_at = concept["deprecated_at"]
        if status == "deprecated":
            _date(deprecated_at, f"{path}.deprecated_at")
        elif deprecated_at is not None:
            raise OntologyError(f"{path}.deprecated_at muss bei active null sein")

        for field in ("broader", "narrower", "related"):
            _sorted_text_list(concept[field], f"{path}.{field}", slugs=True)

    for concept_id, concept in concepts.items():
        for field in ("broader", "narrower", "related"):
            for target_id in concept[field]:
                if target_id not in concepts:
                    raise OntologyError(
                        f"{concept_id}.{field} verweist auf unbekannten Begriff "
                        f"{target_id!r}"
                    )
                if target_id == concept_id:
                    raise OntologyError(f"{concept_id}.{field} verweist auf sich selbst")

    for concept_id, concept in concepts.items():
        concept_type = concept["type"]
        if concept_type == "dachbereich" and concept["broader"]:
            raise OntologyError(f"Dachbereich {concept_id!r} darf kein broader haben")
        if concept_type == "dach" and not concept["broader"]:
            raise OntologyError(f"Dach {concept_id!r} benötigt mindestens ein broader")
        for broader_id in concept["broader"]:
            broader_type = concepts[broader_id]["type"]
            same_family = (
                concept_type in {"dach", "dachbereich"}
                and broader_type in {"dach", "dachbereich"}
            ) or concept_type == broader_type in {"thema", "quellenteil"}
            if not same_family:
                raise OntologyError(
                    f"{concept_id}.broader verweist in eine andere Begriffsfamilie"
                )
            if concept_id not in concepts[broader_id]["narrower"]:
                raise OntologyError("narrower ist nicht die Umkehrung von broader")
        for narrower_id in concept["narrower"]:
            if concept_id not in concepts[narrower_id]["broader"]:
                raise OntologyError("narrower ist nicht die Umkehrung von broader")
        for related_id in concept["related"]:
            if concept_id not in concepts[related_id]["related"]:
                raise OntologyError("related ist nicht symmetrisch")
            if related_id in concept["broader"] or related_id in concept["narrower"]:
                raise OntologyError("related überschneidet sich mit broader/narrower")

    _check_cycle(concepts)
    computed_hash = ontology_hash(raw_concepts)
    if stored_hash != computed_hash:
        raise OntologyError(
            f"ontology_hash stimmt nicht: gespeichert={stored_hash}, "
            f"berechnet={computed_hash}"
        )
    return ontology


def load_ontology(path: Path) -> dict[str, Any]:
    """Load and strictly validate an ontology JSON file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OntologyError(f"Ontologie kann nicht gelesen werden: {exc}") from exc
    return validate_ontology(parse_ontology_json(text))


def validate_catalog_concepts(
    catalog: dict[str, Any], ontology: dict[str, Any]
) -> None:
    """Require every catalog-v2 concept reference to exist in the ontology."""
    if catalog["schema_version"] != 2:
        raise OntologyError("Die Teilmengenprüfung benötigt einen Katalog v2")
    known = {concept["concept_id"] for concept in ontology["concepts"]}
    missing = sorted({
        relation["concept_id"]
        for project in catalog["projects"]
        for relation in project["concept_relations"]
        if relation["concept_id"] not in known
    })
    if missing:
        raise OntologyError(
            "Katalog verweist auf unbekannte Ontologiebegriffe: " + ", ".join(missing)
        )


def validate_production_identity(ontology: dict[str, Any]) -> None:
    """Require the fixed identity of a production delivery from Atlas."""
    expected = (
        ontology["ontology_id"] == PRODUCTION_ONTOLOGY_ID
        and ontology["source"]["application"] == PRODUCTION_APPLICATION
        and ontology["source"]["instance_id"] == PRODUCTION_INSTANCE_ID
    )
    if not expected:
        raise OntologyError(
            "Produktivlieferung benötigt ontology_id='z-ontologie' und "
            "source Atlas/atlas-primary"
        )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the read-only validator."""
    parser = argparse.ArgumentParser(
        description="Atlas-Ontologie v1 ausschließlich lesend validieren"
    )
    parser.add_argument("ontology", type=Path, help="Lokale Ontologie-JSON-Datei")
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Optionaler Atlas-Katalog v2 für die Teilmengenprüfung",
    )
    parser.add_argument(
        "--allow-test-identifiers",
        action="store_true",
        help="Künstliche Testkennungen statt der festen Produktionskennung erlauben",
    )
    return parser.parse_args()


def main() -> int:
    """Run validation and return a process exit status."""
    args = parse_args()
    try:
        ontology = load_ontology(args.ontology)
        if not args.allow_test_identifiers:
            validate_production_identity(ontology)
        if args.catalog is not None:
            try:
                catalog = load_project_catalog(args.catalog)
            except ProjectCatalogError as exc:
                raise OntologyError(f"Katalog ist ungültig: {exc}") from exc
            validate_catalog_concepts(catalog, ontology)
    except OntologyError as exc:
        print(f"UNGÜLTIG: {exc}")
        return 1
    print(
        "GÜLTIG: "
        f"ontology_id={ontology['ontology_id']!r} "
        f"concepts={len(ontology['concepts'])} "
        f"hash={ontology['ontology_hash']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
