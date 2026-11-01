#!/usr/bin/env python3
"""Create a deterministic, read-only project routing plan for transcripts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from inbox_watcher import open_state, setup_logging
from publish_transcripts import prepare_publish_state
from project_glossary import (
    glossary_hotwords,
    glossary_project_rules,
    load_glossary,
)

STATE_DIR = Path(".inbox-watcher")
CONFIG_PATH = Path(os.environ.get(
    "AUDIOREC_ROUTING_CONFIG", STATE_DIR / "routing.json"
))
NUMBER_PATTERN = re.compile(r"Aufnahme\s*#(\d+)", re.IGNORECASE)
LIST_ITEM_PATTERN = re.compile(r"[^,.;:!?\n]+[,.;:!?\n]?")
CONTENT_EVIDENCE_SCHEMA_VERSION = 1
CONTENT_EVIDENCE_TARGET_TYPES = ("project", "topic")


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
    exact_terms = config.get("exact_terms", [])
    if (not isinstance(exact_terms, list)
            or not all(isinstance(term, str) and term.strip()
                       for term in exact_terms)):
        raise ValueError("exact_terms muss eine Textliste sein")
    config["exact_terms"] = exact_terms
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
                or not terms
                or not all(isinstance(term, str) and term.strip() for term in terms)):
            raise ValueError(
                f"project_rules[{index}].match_any muss eine Textliste sein"
            )
    for topic, terms in config["topic_rules"].items():
        if (not isinstance(terms, list)
                or not terms
                or not all(isinstance(term, str) and term.strip() for term in terms)):
            raise ValueError(f"topic_rules[{topic!r}] muss eine Textliste sein")
    glossary = load_glossary()
    config["project_rules"].extend(glossary_project_rules(glossary))
    # Internal diagnostic input. It is deliberately not a routing rule: the
    # shadow detector may report likely ASR prompt leakage, but never suppresses
    # a project or topic.
    config["_shadow_prompt_leak_hotwords"] = glossary_hotwords(glossary)
    # Validated after the glossary merge, so glossary aliases count as rule
    # terms. Without the optional block, plans keep their previous shape.
    if "content_evidence" in config:
        config["content_evidence"] = validate_content_evidence_config(config)
    return config


def _text_terms(value: Any, field: str, *, allow_empty: bool) -> list[str]:
    if (not isinstance(value, list)
            or (not value and not allow_empty)
            or not all(isinstance(term, str) and term.strip() for term in value)):
        raise ValueError(f"{field} muss eine Textliste sein")
    folded = [term.casefold() for term in value]
    if len(set(folded)) != len(folded):
        raise ValueError(f"{field} enthält doppelte Begriffe")
    return list(value)


def validate_content_evidence_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the optional, advisory weak-evidence block and normalize it.

    The block only classifies existing content matches. It never adds rules,
    so it cannot create, remove or block a project or topic.
    """
    block = config["content_evidence"]
    if not isinstance(block, dict):
        raise ValueError("content_evidence muss ein Objekt sein")
    unknown = set(block) - {"support_window_chars", "rules"}
    if unknown:
        raise ValueError(
            f"content_evidence enthält unbekannte Felder: {sorted(unknown)}"
        )
    window = block.get("support_window_chars")
    if type(window) is not int or window < 1:
        raise ValueError(
            "content_evidence.support_window_chars muss eine positive ganze Zahl sein"
        )
    rules = block.get("rules")
    if not isinstance(rules, list):
        raise ValueError("content_evidence.rules muss eine Liste sein")

    rule_terms: dict[tuple[str, str], list[str]] = {}
    for rule in config["project_rules"]:
        rule_terms.setdefault(("project", rule["project"]), []).extend(
            rule["match_any"]
        )
    for topic, terms in config["topic_rules"].items():
        rule_terms[("topic", topic)] = list(terms)

    normalized = []
    seen: set[tuple[str, str]] = set()
    for index, rule in enumerate(rules):
        field = f"content_evidence.rules[{index}]"
        if not isinstance(rule, dict):
            raise ValueError(f"{field} muss ein Objekt sein")
        expected = {"target_type", "target", "weak_terms", "support_any"}
        if set(rule) != expected:
            raise ValueError(
                f"{field} benötigt genau die Felder {sorted(expected)}"
            )
        target_type = rule["target_type"]
        if target_type not in CONTENT_EVIDENCE_TARGET_TYPES:
            raise ValueError(
                f"{field}.target_type muss 'project' oder 'topic' sein"
            )
        target = rule["target"]
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"{field}.target muss ein nichtleerer Text sein")
        key = (target_type, target)
        if key not in rule_terms:
            raise ValueError(
                f"{field}.target hat keine Inhaltsregel: {target_type} {target!r}"
            )
        if key in seen:
            raise ValueError(f"{field}.target ist doppelt: {target_type} {target!r}")
        seen.add(key)
        weak_terms = _text_terms(
            rule["weak_terms"], f"{field}.weak_terms", allow_empty=False
        )
        known = {term.casefold() for term in rule_terms[key]}
        unknown_terms = [term for term in weak_terms if term.casefold() not in known]
        if unknown_terms:
            raise ValueError(
                f"{field}.weak_terms sind keine Regelbegriffe des Ziels: "
                f"{unknown_terms}"
            )
        support_any = _text_terms(
            rule["support_any"], f"{field}.support_any", allow_empty=True
        )
        weak_folded = {term.casefold() for term in weak_terms}
        overlap = [term for term in support_any if term.casefold() in weak_folded]
        if overlap:
            raise ValueError(
                f"{field}.support_any darf keine schwachen Begriffe enthalten: "
                f"{overlap}"
            )
        normalized.append({
            "target_type": target_type,
            "target": target,
            "weak_terms": weak_terms,
            "support_any": support_any,
        })
    return {"support_window_chars": window, "rules": normalized}


def published_transcripts(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT p.drive_id, p.remote_path, p.published_at,
                  j.transcript_path, f.path AS audio_path
           FROM published_transcripts p
           JOIN transcription_jobs j USING (drive_id)
           JOIN files f USING (drive_id)
           ORDER BY f.first_seen, p.drive_id"""
    ).fetchall()


def published_transcript(db: sqlite3.Connection, drive_id: str) -> sqlite3.Row:
    row = db.execute(
        """SELECT p.drive_id, p.remote_path, p.published_at,
                  j.transcript_path, f.path AS audio_path
           FROM published_transcripts p
           JOIN transcription_jobs j USING (drive_id)
           JOIN files f USING (drive_id)
           WHERE p.drive_id = ?""",
        (drive_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Veröffentlichtes Transkript ist unbekannt: {drive_id}")
    return row


def recording_number(path: str) -> int | None:
    match = NUMBER_PATTERN.search(path)
    return int(match.group(1)) if match else None


def accepted_occurrences(
    folded: str,
    term: str,
    folded_exact_terms: set[str],
) -> Iterator[tuple[int, int]]:
    """Yield every accepted occurrence of ``term`` in casefolded text.

    This is the single source of the routing match semantics; matching_terms
    only asks whether a first occurrence exists.
    """
    folded_term = term.casefold()
    is_short_acronym = term.isupper() and len(re.sub(r"\W", "", term)) <= 3
    allow_inflection = (
        folded_term not in folded_exact_terms and not is_short_acronym
    )
    # Terms must start at a word boundary so "Gründung" does not match
    # "Begründung". Common inflections and linking letters remain valid,
    # while unrelated continuations such as "Drive" in "Driver" do not.
    pattern = rf"(?<!\w){re.escape(folded_term)}"
    for occurrence in re.finditer(pattern, folded):
        # Two characters suffice for every check below.
        tail = folded[occurrence.end():occurrence.end() + 2]
        if not tail or not (tail[0].isalnum() or tail[0] == "_"):
            yield occurrence.start(), occurrence.end()
        elif allow_inflection and tail.startswith(("e", "en", "er", "n", "s")):
            yield occurrence.start(), occurrence.end()


def matching_terms(
    text: str,
    terms: list[str],
    exact_terms: list[str] | None = None,
) -> list[str]:
    folded = text.casefold()
    folded_exact_terms = {
        term.casefold() for term in (exact_terms or [])
    }
    return [
        term
        for term in terms
        if next(accepted_occurrences(folded, term, folded_exact_terms), None)
        is not None
    ]


def _short_comma_runs(
    text: str,
    *,
    max_words: int = 4,
    min_items: int = 3,
) -> list[tuple[int, int]]:
    """Return comma-separated runs of short items for shadow diagnostics."""
    runs: list[tuple[int, int]] = []
    current: list[tuple[int, int]] = []
    for match in LIST_ITEM_PATTERN.finditer(text):
        raw = match.group(0)
        item = raw.rstrip(",.;:!?\n").strip()
        separator = raw[-1] if raw else ""
        words = item.split()
        if 1 <= len(words) <= max_words:
            current.append((match.start(), match.start() + len(
                raw.rstrip(",.;:!?\n")
            )))
            if separator != ",":
                if len(current) >= min_items:
                    runs.append((current[0][0], current[-1][1]))
                current = []
        else:
            if len(current) >= min_items:
                runs.append((current[0][0], current[-1][1]))
            current = []
    if len(current) >= min_items:
        runs.append((current[0][0], current[-1][1]))
    return runs


def _exact_terms_in_text(text: str, terms: list[str]) -> list[str]:
    folded = text.casefold()
    return [
        term
        for term in terms
        if re.search(
            rf"(?<!\w){re.escape(term.casefold())}(?!\w)",
            folded,
        )
    ]


def prompt_leak_shadow_diagnostics(
    text: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Report possible prompt leakage without changing routing decisions."""
    hotwords = list(config.get("_shadow_prompt_leak_hotwords", []))
    if not hotwords:
        return []
    diagnostics = []
    for start, end in _short_comma_runs(text):
        excerpt = text[start:end]
        glossary_terms = _exact_terms_in_text(excerpt, hotwords)
        if len({term.casefold() for term in glossary_terms}) < 3:
            continue

        project_hits: dict[str, list[str]] = {}
        for rule in config["project_rules"]:
            matched = matching_terms(
                excerpt,
                list(rule.get("match_any", [])),
                config.get("exact_terms", []),
            )
            if matched:
                project = str(rule["project"])
                project_hits.setdefault(project, [])
                for term in matched:
                    if term not in project_hits[project]:
                        project_hits[project].append(term)

        topic_hits = []
        for topic, terms in config["topic_rules"].items():
            matched = matching_terms(
                excerpt,
                list(terms),
                config.get("exact_terms", []),
            )
            if matched:
                topic_hits.append({"topic": str(topic), "terms": matched})

        diagnostics.append({
            "start": start,
            "end": end,
            "glossary_terms": glossary_terms,
            "project_hits": [
                {"project": project, "terms": terms}
                for project, terms in project_hits.items()
            ],
            "topic_hits": topic_hits,
        })
    return diagnostics


def log_shadow_diagnostics(plan: dict[str, Any], logger) -> None:
    """Log shadow findings without logging transcript excerpts."""
    for finding in plan.get("diagnostics", {}).get("prompt_leak_shadow", []):
        projects = ",".join(
            f"{hit['project']}:{'|'.join(hit['terms'])}"
            for hit in finding["project_hits"]
        ) or "-"
        topics = ",".join(
            f"{hit['topic']}:{'|'.join(hit['terms'])}"
            for hit in finding["topic_hits"]
        ) or "-"
        logger.warning(
            "Routing SHADOW prompt-leak id=%s span=%d-%d glossary=%s "
            "would_suppress_projects=%s would_suppress_topics=%s",
            plan["drive_id"],
            finding["start"],
            finding["end"],
            "|".join(finding["glossary_terms"]),
            projects,
            topics,
        )


def _unique_casefold(terms: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        folded = term.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(term)
    return result


def build_content_evidence(
    text: str,
    config: dict[str, Any],
    content_matches: list[tuple[str, str, list[str]]],
) -> dict[str, Any] | None:
    """Classify existing content matches as strong, supported or weak.

    ``content_matches`` holds ``(target_type, target, matched_terms)`` for
    actual content hits only, in plan order. Delivery rules (default,
    active_context) are never passed in and therefore never count as evidence
    or support. The result only uses configured term names and counts; it
    contains no transcript text or matched surface forms.
    """
    settings = config.get("content_evidence")
    if settings is None:
        return None
    window = settings["support_window_chars"]
    rules = {
        (rule["target_type"], rule["target"]): rule for rule in settings["rules"]
    }
    folded = text.casefold()
    folded_exact_terms = {
        term.casefold() for term in config.get("exact_terms", [])
    }

    def spans(term: str) -> list[tuple[int, int]]:
        return list(accepted_occurrences(folded, term, folded_exact_terms))

    items = []
    for target_type, target, terms in content_matches:
        matched = _unique_casefold(terms)
        rule = rules.get((target_type, target))
        weak_folded = (
            {term.casefold() for term in rule["weak_terms"]} if rule else set()
        )
        weak_terms = [term for term in matched if term.casefold() in weak_folded]
        term_spans = {term: spans(term) for term in matched}
        supporting_terms: list[str] = []
        if len(weak_terms) < len(matched):
            strength, basis = "strong", "specific_term"
        else:
            weak_spans = [span for term in weak_terms for span in term_spans[term]]
            for support in rule["support_any"]:
                if any(
                    weak_start - window <= start <= weak_end + window
                    for start, _ in spans(support)
                    for weak_start, weak_end in weak_spans
                ):
                    supporting_terms.append(support)
            if supporting_terms:
                strength, basis = "supported", "weak_terms_with_local_support"
            else:
                strength, basis = "weak", "weak_terms_only"
        items.append({
            "target_type": target_type,
            "target": target,
            "strength": strength,
            "basis": basis,
            "matched_terms": matched,
            "weak_terms": weak_terms,
            "supporting_terms": supporting_terms,
            "occurrences": {term: len(term_spans[term]) for term in matched},
        })
    return {
        "schema_version": CONTENT_EVIDENCE_SCHEMA_VERSION,
        "support_window_chars": window,
        "items": items,
    }


def log_content_evidence(plan: dict[str, Any], logger) -> None:
    """Log weak targets with configured term names only."""
    evidence = plan.get("content_evidence") or {}
    for item in evidence.get("items", []):
        if item["strength"] != "weak":
            continue
        logger.info(
            "Routing EVIDENCE target_type=%s target=%s strength=%s terms=%s",
            item["target_type"],
            item["target"],
            item["strength"],
            "|".join(item["matched_terms"]),
        )


def plan_one(row: sqlite3.Row, config: dict[str, Any]) -> dict[str, Any]:
    transcript_path = Path(row["transcript_path"])
    if not transcript_path.is_file():
        raise ValueError(f"Lokales Transkript fehlt: {transcript_path}")
    text = transcript_path.read_text(encoding="utf-8")
    shadow_findings = prompt_leak_shadow_diagnostics(text, config)
    reasons: dict[str, list[str]] = {}
    project_content_terms: dict[str, list[str]] = {}

    def add_project(project: str, reason: str) -> None:
        reasons.setdefault(project, [])
        if reason not in reasons[project]:
            reasons[project].append(reason)

    for project in config["default_projects"]:
        add_project(str(project), "default")
    for project in config["active_projects"]:
        add_project(str(project), "active_context")
    for rule in config["project_rules"]:
        matched = matching_terms(
            text,
            list(rule.get("match_any", [])),
            config.get("exact_terms", []),
        )
        if matched:
            add_project(str(rule["project"]), "content:" + ",".join(matched))
            project_content_terms.setdefault(str(rule["project"]), []).extend(
                matched
            )

    topics = []
    topic_content_terms: dict[str, list[str]] = {}
    for topic, terms in config["topic_rules"].items():
        matched = matching_terms(text, list(terms), config.get("exact_terms", []))
        if matched:
            topics.append(str(topic))
            topic_content_terms[str(topic)] = matched

    plan = {
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
        "diagnostics": {"prompt_leak_shadow": shadow_findings},
    }
    content_matches = [
        ("project", project, project_content_terms[project])
        for project in reasons
        if project in project_content_terms
    ] + [
        ("topic", topic, topic_content_terms[topic]) for topic in topics
    ]
    evidence = build_content_evidence(text, config, content_matches)
    if evidence is not None:
        plan["content_evidence"] = evidence
    return plan


def plan_published(
    db: sqlite3.Connection, drive_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Create the deterministic routing plan for one published transcript."""
    return plan_one(published_transcript(db, drive_id), config)


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
                log_shadow_diagnostics(plan, logger)
                log_content_evidence(plan, logger)
            logger.info("Dry-Run beendet (keine Kopie, keine Drive-Änderung)")
        return 0
    except Exception as exc:
        logger.exception("Routing-Plan fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
