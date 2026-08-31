"""Validated single source for project vocabulary and recognition hints."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_GLOSSARY_PATH = Path(__file__).with_name("project_glossary.json")


def glossary_path() -> Path:
    return Path(os.environ.get("AUDIOREC_GLOSSARY", DEFAULT_GLOSSARY_PATH))


def _text_list(entry: dict[str, Any], key: str, index: int) -> list[str]:
    value = entry.get(key, [])
    if (not isinstance(value, list)
            or not all(isinstance(item, str) and item.strip() for item in value)):
        raise ValueError(f"terms[{index}].{key} muss eine Textliste sein")
    return value


def load_glossary(path: Path | None = None) -> dict[str, Any]:
    source = path or glossary_path()
    glossary = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(glossary, dict) or not isinstance(glossary.get("terms"), list):
        raise ValueError("Glossar muss ein Objekt mit einer terms-Liste sein")

    canonicals: set[str] = set()
    replacements: dict[str, str] = {}
    for index, entry in enumerate(glossary["terms"]):
        if not isinstance(entry, dict):
            raise ValueError(f"terms[{index}] muss ein Objekt sein")
        canonical = entry.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError(f"terms[{index}].canonical muss ein nichtleerer Text sein")
        folded = canonical.casefold()
        if folded in canonicals:
            raise ValueError(f"Doppelter kanonischer Begriff: {canonical}")
        canonicals.add(folded)
        for key in ("hotwords", "replacements", "routing_aliases", "projects"):
            _text_list(entry, key, index)
        for wrong in entry.get("replacements", []):
            previous = replacements.get(wrong)
            if previous is not None and previous != canonical:
                raise ValueError(f"Mehrdeutige Ersetzung für {wrong!r}")
            replacements[wrong] = canonical
    return glossary


def _unique(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        folded = value.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(value)
    return result


def glossary_hotwords(glossary: dict[str, Any] | None = None) -> list[str]:
    data = glossary or load_glossary()
    return _unique(
        hotword for entry in data["terms"] for hotword in entry.get("hotwords", [])
    )


def glossary_replacements(glossary: dict[str, Any] | None = None) -> dict[str, str]:
    data = glossary or load_glossary()
    return {
        wrong: entry["canonical"]
        for entry in data["terms"]
        for wrong in entry.get("replacements", [])
    }


def glossary_project_rules(glossary: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = glossary or load_glossary()
    rules = []
    for entry in data["terms"]:
        aliases = _unique([entry["canonical"], *entry.get("routing_aliases", [])])
        for project in entry.get("projects", []):
            rules.append({"project": project, "match_any": aliases})
    return rules


def glossary_prompt(glossary: dict[str, Any] | None = None) -> str:
    return "Fachbegriffe: " + ", ".join(glossary_hotwords(glossary))
