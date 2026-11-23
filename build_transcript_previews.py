#!/usr/bin/env python3
"""Build deterministic, non-canonical previews from a frozen repair plan."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
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
from quality_reviews import ISSUE_TYPES, candidate_hash, open_readonly_state
from segment_metadata import load_segment_metadata


DEFAULT_OUTPUT_DIR = Path("staging/repair-previews")
TOOL_VERSION = "1"
SAFE_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


class PlanFormatError(ValueError):
    """The frozen plan or its explicit hash confirmation is invalid."""


class PlanHoldError(ValueError):
    """The plan contains at least one entry that is not safe to process."""


class CurrentPlanMismatch(ValueError):
    """The current read-only state no longer reproduces the frozen plan."""


class InputVersionMismatch(ValueError):
    """Transcript, sidecar or review evidence no longer matches the plan."""


class ReconstructionError(ValueError):
    """The canonical transcript cannot be reconstructed or changed safely."""


class OutputSafetyError(ValueError):
    """The requested preview destination is unsafe or conflicts with output."""


def valid_sha256(value: object) -> bool:
    """Return whether value is one lowercase hexadecimal SHA-256 digest."""
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
        if not isinstance(status, str) or status not in RECORDING_STATUSES:
            raise PlanFormatError(f"Ungültiger Aufnahmestatus für {drive_id}")
        version_status = recording.get("transcript_version_status")
        if (
            not isinstance(version_status, str)
            or version_status not in {"MATCHED", "MISMATCH", "UNVERIFIED"}
        ):
            raise PlanFormatError(f"Ungültiger Versionsstatus für {drive_id}")
        if status == "CURATION_REQUIRED" or version_status != "MATCHED":
            holds.append(
                f"{drive_id}: status={status}, version={version_status}"
            )
        reviews = recording.get("reviews")
        transcript_path = recording.get("transcript_path")
        transcript_size = recording.get("transcript_size")
        transcript_hash = recording.get("transcript_sha256")
        if not isinstance(transcript_path, str) or not transcript_path.strip():
            raise PlanFormatError(f"Transkriptpfad fehlt für {drive_id}")
        if (
            isinstance(transcript_size, bool)
            or not isinstance(transcript_size, int)
            or transcript_size < 0
        ):
            raise PlanFormatError(f"Transkriptgröße ist ungültig für {drive_id}")
        if not valid_sha256(transcript_hash):
            raise PlanFormatError(f"Transkripthash ist ungültig für {drive_id}")
        if not isinstance(reviews, list) or not reviews:
            raise PlanFormatError(f"Aufnahme {drive_id} enthält keine Reviews")
        for review_index, review in enumerate(reviews):
            review_label = f"{drive_id}/reviews[{review_index}]"
            if not isinstance(review, dict):
                raise PlanFormatError(f"{review_label} ist kein Objekt")
            segment_id = review.get("segment_id")
            candidate = review.get("candidate_hash")
            segment_text = review.get("segment_text")
            start = review.get("segment_start")
            end = review.get("segment_end")
            density = review.get("chars_per_second")
            flags = review.get("flags")
            issues = review.get("issue_types")
            if not isinstance(segment_id, str) or not segment_id.strip():
                raise PlanFormatError(f"Segment-ID fehlt für {review_label}")
            if not valid_sha256(candidate):
                raise PlanFormatError(f"Kandidatenhash ist ungültig für {review_label}")
            if not isinstance(segment_text, str) or not segment_text:
                raise PlanFormatError(f"Segmenttext ist ungültig für {review_label}")
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
                raise PlanFormatError(f"Segmentzeit ist ungültig für {review_label}")
            if density is not None and (
                isinstance(density, bool)
                or not isinstance(density, (int, float))
                or not isfinite(float(density))
                or float(density) < 0
            ):
                raise PlanFormatError(f"Textdichte ist ungültig für {review_label}")
            if (
                not isinstance(flags, list)
                or not flags
                or not all(isinstance(flag, str) and flag.strip() for flag in flags)
            ):
                raise PlanFormatError(f"Prüfsignale sind ungültig für {review_label}")
            if (
                not isinstance(issues, list)
                or not issues
                or not all(isinstance(issue, str) for issue in issues)
                or set(issues) - ISSUE_TYPES
            ):
                raise PlanFormatError(f"Fehlerarten sind ungültig für {review_label}")
            action = review.get("action")
            if not isinstance(action, str) or action not in REPAIR_ACTIONS:
                raise PlanFormatError(f"Ungültige Aktion für {review_label}")
            review_version = review.get("transcript_version_status")
            if (
                not isinstance(review_version, str)
                or review_version not in {"MATCHED", "MISMATCH", "UNVERIFIED"}
            ):
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


def _sha256(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of one immutable byte string."""
    return hashlib.sha256(data).hexdigest()


def _segments_path(transcript_path: Path) -> Path:
    """Derive the machine-readable segment sidecar beside one transcript."""
    return transcript_path.with_suffix(".segments.json")


def _align_segments(
    transcript: str, segments: list[dict[str, Any]]
) -> dict[str, tuple[int, int]]:
    """Align every segment in order and reject non-whitespace body gaps."""
    if not segments:
        raise ReconstructionError("Segmentdatei enthält keine Segmente")
    cursor = 0
    if transcript.startswith("# "):
        separator = re.search(r"(?:\r\n|\n){2}", transcript)
        if separator is None:
            raise ReconstructionError("Transkriptkopf besitzt keinen Textkörper")
        cursor = separator.end()
    positions: dict[str, tuple[int, int]] = {}
    for segment in segments:
        segment_text = segment["text"]
        if not segment_text:
            raise ReconstructionError(
                f"Leerer Segmenttext: {segment['id']}"
            )
        start = transcript.find(segment_text, cursor)
        if start < 0:
            raise ReconstructionError(
                f"Segmenttext nicht im Transkript gefunden: {segment['id']}"
            )
        if transcript[cursor:start].strip():
            raise ReconstructionError(
                f"Nicht rekonstruierbarer Text vor {segment['id']}"
            )
        end = start + len(segment_text)
        positions[segment["id"]] = (start, end)
        cursor = end
    if transcript[cursor:].strip():
        raise ReconstructionError(
            "Nicht rekonstruierbarer Text nach dem letzten Segment"
        )
    return positions


def _deletion_bounds(
    transcript: str,
    start: int,
    end: int,
    segment_spans: list[tuple[int, int]],
    claimed_gaps: set[int],
) -> tuple[int, int]:
    """Include at most one unowned inter-segment space in a deletion."""
    candidates = ((end, start, end + 1), (start - 1, start - 1, end))
    for position, expanded_start, expanded_end in candidates:
        if (
            0 <= position < len(transcript)
            and transcript[position] == " "
            and position not in claimed_gaps
            and not any(
                span_start <= position < span_end
                for span_start, span_end in segment_spans
            )
        ):
            claimed_gaps.add(position)
            return expanded_start, expanded_end
    return start, end


def _candidate_from_segment(
    segment: dict[str, Any], review: dict[str, Any]
) -> dict[str, Any]:
    """Recreate exactly the candidate material covered by candidate_hash."""
    return {
        "id": segment["id"],
        "start": segment["start"],
        "end": segment["end"],
        "raw_text": segment["raw_text"],
        "text": segment["text"],
        "flags": review["flags"],
        "chars_per_second": review["chars_per_second"],
    }


def _context(text: str, start: int, end: int, words: int = 12) -> str:
    """Return deterministic word context around one character span."""
    tokens = list(re.finditer(r"\S+", text))
    before = [match for match in tokens if match.end() <= start]
    after = [match for match in tokens if match.start() >= end]
    left = before[-words:]
    right = after[:words]
    context_start = left[0].start() if left else start
    context_end = right[-1].end() if right else end
    return text[context_start:context_end]


def _word_diff(old: str, new: str) -> str:
    """Render one compact word-level replacement diff."""
    old_words = old.split()
    new_words = new.split()
    output: list[str] = []
    for opcode, first_start, first_end, second_start, second_end in (
        difflib.SequenceMatcher(None, old_words, new_words).get_opcodes()
    ):
        first = " ".join(old_words[first_start:first_end])
        second = " ".join(new_words[second_start:second_end])
        if opcode == "equal":
            output.append(first)
        elif opcode == "delete":
            output.append(f"[-{first}-]")
        elif opcode == "insert":
            output.append(f"{{+{second}+}}")
        else:
            output.append(f"[-{first}-]{{+{second}+}}")
    return " ".join(part for part in output if part)


def _sentence_lines(text: str) -> list[str]:
    """Split prose at sentence boundaries for a readable unified diff."""
    lines: list[str] = []
    for line in text.splitlines(keepends=False):
        if not line:
            lines.append("")
            continue
        lines.extend(re.split(r"(?<=[.!?])\s+", line))
    return [line + "\n" for line in lines]


def _apply_recording(
    recording: dict[str, Any]
) -> tuple[bytes, bytes, dict[str, Any], bytes]:
    """Verify one recording and return preview, diff, manifest and input bytes."""
    drive_id = recording["drive_id"]
    if SAFE_DRIVE_ID.fullmatch(drive_id) is None:
        raise OutputSafetyError(f"Unsichere Drive-ID: {drive_id}")
    transcript_path = Path(recording["transcript_path"])
    try:
        transcript_bytes = transcript_path.read_bytes()
        transcript = transcript_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise InputVersionMismatch(
            f"Transkript ist nicht unverändert lesbar: {transcript_path}"
        ) from exc
    transcript_hash = _sha256(transcript_bytes)
    if (
        transcript_hash != recording.get("transcript_sha256")
        or len(transcript_bytes) != recording.get("transcript_size")
    ):
        raise InputVersionMismatch(
            f"Transkript stimmt nicht mit dem Plan überein: {drive_id}"
        )

    sidecar_path = _segments_path(transcript_path)
    try:
        payload = load_segment_metadata(sidecar_path, drive_id)
    except (OSError, ValueError) as exc:
        raise InputVersionMismatch(
            f"Segmentdatei ist nicht verwendbar: {sidecar_path}"
        ) from exc
    if payload.get("transcript_sha256") != transcript_hash:
        raise InputVersionMismatch(
            f"Segmentdatei gehört nicht zur Transkriptversion: {drive_id}"
        )
    segments = payload["segments"]
    positions = _align_segments(transcript, segments)
    by_id = {segment["id"]: segment for segment in segments}

    operations: list[dict[str, Any]] = []
    seen_segments: set[str] = set()
    for review in recording["reviews"]:
        segment_id = review["segment_id"]
        if segment_id in seen_segments or segment_id not in by_id:
            raise InputVersionMismatch(
                f"Review-Segment fehlt oder ist doppelt: {segment_id}"
            )
        seen_segments.add(segment_id)
        segment = by_id[segment_id]
        if (
            review["segment_text"] != segment["text"]
            or float(review["segment_start"]) != float(segment["start"])
            or float(review["segment_end"]) != float(segment["end"])
        ):
            raise InputVersionMismatch(
                f"Segmentmaterial weicht vom Plan ab: {segment_id}"
            )
        calculated = candidate_hash(
            _candidate_from_segment(segment, review), transcript_hash
        )
        if calculated != review["candidate_hash"]:
            raise InputVersionMismatch(
                f"Kandidatenhash stimmt nicht: {segment_id}"
            )
        start, end = positions[segment_id]
        if transcript[start:end] != review["segment_text"]:
            raise ReconstructionError(
                f"Fundstelle stimmt nicht mit Segmenttext überein: {segment_id}"
            )
        action = review["action"]
        replacement = review.get("replacement_text")
        if action == "CURATED_REPLACEMENT_AVAILABLE":
            if not isinstance(replacement, str) or not replacement.strip():
                raise ReconstructionError(
                    f"REPLACE ohne Ersatztext: {segment_id}"
                )
            new_text = replacement
        elif action == "CURATED_DELETION_AVAILABLE":
            if "HALLUCINATION" not in review["issue_types"]:
                raise ReconstructionError(
                    f"DELETE ohne HALLUCINATION: {segment_id}"
                )
            new_text = ""
        elif action == "NO_TEXT_CHANGE":
            new_text = review["segment_text"]
        else:
            raise ReconstructionError(
                f"Nicht freigegebene Reparaturaktion: {segment_id}"
            )
        operations.append({
            "segment_id": segment_id,
            "segment_start": review["segment_start"],
            "segment_end": review["segment_end"],
            "issue_types": review["issue_types"],
            "action": action,
            "old_text": review["segment_text"],
            "new_text": new_text,
            "start": start,
            "end": end,
            "context": _context(transcript, start, end),
        })

    ordered = sorted(operations, key=lambda item: item["start"])
    for previous, current in zip(ordered, ordered[1:]):
        if previous["end"] > current["start"]:
            raise ReconstructionError(
                f"Überlappende Reparaturen: {previous['segment_id']} und "
                f"{current['segment_id']}"
            )

    segment_spans = list(positions.values())
    claimed_gaps: set[int] = set()
    for operation in ordered:
        edit_start = operation["start"]
        edit_end = operation["end"]
        if operation["action"] == "CURATED_DELETION_AVAILABLE":
            edit_start, edit_end = _deletion_bounds(
                transcript,
                edit_start,
                edit_end,
                segment_spans,
                claimed_gaps,
            )
        operation["edit_start"] = edit_start
        operation["edit_end"] = edit_end

    preview = transcript
    for operation in reversed(ordered):
        start = operation["edit_start"]
        end = operation["edit_end"]
        preview = preview[:start] + operation["new_text"] + preview[end:]

    public_changes = [
        {
            key: value
            for key, value in operation.items()
            if key not in {"start", "end", "edit_start", "edit_end"}
        }
        for operation in ordered
    ]
    manifest = {
        "schema_version": 1,
        "manifest_type": "non_canonical_transcript_repair_preview",
        "non_canonical": True,
        "drive_id": drive_id,
        "input_sha256": transcript_hash,
        "output_sha256": _sha256(preview.encode("utf-8")),
        "changes": public_changes,
    }
    diff_lines = [
        "# Nicht-kanonische Reparaturvorschau\n",
        f"- Drive-ID: `{drive_id}`\n",
        f"- Eingabe: `{transcript_hash}`\n",
        f"- Ausgabe: `{manifest['output_sha256']}`\n",
        "\n## Änderungen\n",
    ]
    changed = [
        operation for operation in ordered
        if operation["action"] != "NO_TEXT_CHANGE"
    ]
    if not changed:
        diff_lines.append("\nKeine Textänderung; ausschließlich bestätigte Grenzfälle.\n")
    for operation in changed:
        diff_lines.extend([
            f"\n### {operation['segment_id']}\n",
            f"- Zeit: {operation['segment_start']:.3f}–{operation['segment_end']:.3f} Sekunden\n",
            f"- Fehlerarten: {', '.join(operation['issue_types'])}\n",
            f"- Aktion: `{operation['action']}`\n",
            f"- Kontext: {operation['context']}\n",
            f"- Wortdiff: {_word_diff(operation['old_text'], operation['new_text'])}\n",
        ])
    diff_lines.append("\n## Gesamtdiff\n\n```diff\n")
    diff_lines.extend(difflib.unified_diff(
        _sentence_lines(transcript),
        _sentence_lines(preview),
        fromfile="kanonisch.md",
        tofile="vorschau.md",
    ))
    diff_lines.append("```\n")
    return (
        preview.encode("utf-8"),
        "".join(diff_lines).encode("utf-8"),
        manifest,
        transcript_bytes,
    )


def _protected_output_roots() -> list[Path]:
    """Return repository locations that must never receive preview output."""
    return [
        Path("staging/transcripts"),
        Path("staging/inbox"),
        Path("input"),
        Path("output"),
        Path(".inbox-watcher"),
    ]


def _validate_output_dir(output_dir: Path) -> Path:
    """Resolve and reject output inside canonical or operational locations."""
    resolved = output_dir.resolve()
    for protected in _protected_output_roots():
        protected_resolved = protected.resolve()
        if resolved == protected_resolved or protected_resolved in resolved.parents:
            raise OutputSafetyError(
                f"Unzulässiges Ausgabeziel: {output_dir}"
            )
    return resolved


def _json_bytes(payload: object) -> bytes:
    """Serialize deterministic human-readable JSON."""
    return (json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def prepare_preview_files(
    plan: dict[str, Any], output_dir: Path
) -> tuple[Path, dict[Path, bytes]]:
    """Verify all recordings and prepare the complete deterministic tree."""
    root = _validate_output_dir(output_dir)
    target = root / plan["plan_sha256"][:16]
    files: dict[Path, bytes] = {}
    run_recordings: list[dict[str, Any]] = []
    for recording in plan["recordings"]:
        preview, diff, manifest, _ = _apply_recording(recording)
        base = Path("recordings") / recording["drive_id"]
        files[base / "preview.md"] = preview
        files[base / "diff.md"] = diff
        files[base / "changes.json"] = _json_bytes(manifest)
        run_recordings.append({
            "drive_id": recording["drive_id"],
            "input_sha256": manifest["input_sha256"],
            "output_sha256": manifest["output_sha256"],
        })
    files[Path("run.json")] = _json_bytes({
        "schema_version": 1,
        "run_type": "non_canonical_transcript_repair_preview",
        "non_canonical": True,
        "tool_version": TOOL_VERSION,
        "plan_sha256": plan["plan_sha256"],
        "recordings": run_recordings,
    })
    return target, files


def _existing_tree_matches(target: Path, files: dict[Path, bytes]) -> bool:
    """Return whether an existing tree is exactly the requested result."""
    if not target.is_dir() or any(path.is_symlink() for path in target.rglob("*")):
        return False
    existing = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    return existing == files


def install_preview_files(target: Path, files: dict[Path, bytes]) -> bool:
    """Atomically install one complete preview tree or perform an exact no-op."""
    if target.exists():
        if _existing_tree_matches(target, files):
            return False
        raise OutputSafetyError(
            f"Abweichendes Vorschauergebnis existiert bereits: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.", dir=target.parent
    ))
    try:
        for relative, content in sorted(files.items(), key=lambda item: str(item[0])):
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        if target.exists():
            if _existing_tree_matches(target, files):
                return False
            raise OutputSafetyError(
                f"Vorschauergebnis entstand gleichzeitig: {target}"
            )
        try:
            temporary.replace(target)
        except OSError as exc:
            if target.exists():
                if _existing_tree_matches(target, files):
                    return False
                raise OutputSafetyError(
                    f"Vorschauergebnis entstand gleichzeitig: {target}"
                ) from exc
            raise
        return True
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def check_plan(
    plan_path: Path, expected_sha256: str, state_path: Path
) -> dict[str, Any]:
    """Perform every currently implemented read-only preview prerequisite."""
    plan = load_repair_plan(plan_path, expected_sha256)
    drive_ids = validate_plan_gate(plan)
    verify_current_state(plan, state_path, drive_ids)
    # Input bytes, sidecars, candidate hashes and reconstruction are part of
    # the safety gate even though --check does not install any output.
    prepare_preview_files(plan, DEFAULT_OUTPUT_DIR)
    return {
        "check": "PASSED",
        "read_only": True,
        "plan_sha256": plan["plan_sha256"],
        "recording_count": len(drive_ids),
        "preview_written": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse preview verification and generation arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Eingefrorenen Reparaturplan gegen Hash, HOLD-Regeln und den "
            "aktuellen Nur-Lese-Zustand prüfen."
        )
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expect-plan-sha256", required=True)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Alle Prüfungen ausführen; schreibt keine Vorschauen, SQLite kann "
            "jedoch -wal/-shm-Sidecar-Dateien anlegen"
        ),
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Check or generate one deterministic non-canonical preview tree."""
    args = parse_args(argv)
    try:
        plan = load_repair_plan(args.plan, args.expect_plan_sha256)
        drive_ids = validate_plan_gate(plan)
        verify_current_state(plan, args.state, drive_ids)
        target, files = prepare_preview_files(plan, args.output_dir)
        written = False if args.check else install_preview_files(target, files)
        result = {
            "check": "PASSED",
            "read_only": bool(args.check),
            "plan_sha256": plan["plan_sha256"],
            "recording_count": len(drive_ids),
            "preview_written": written,
            "output_path": None if args.check else str(target),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                "Reparaturvorschau-Prüfung bestanden: "
                f"plan_sha256={result['plan_sha256']} "
                f"Aufnahmen={result['recording_count']} "
                + (
                    "(keine Ausgabe geschrieben)"
                    if args.check
                    else f"Ausgabe={result['output_path']}"
                )
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
    except InputVersionMismatch as exc:
        print(f"[X] Eingabeversion stimmt nicht: {exc}", file=sys.stderr)
        return 4
    except ReconstructionError as exc:
        print(f"[X] Reparatur nicht sicher rekonstruierbar: {exc}", file=sys.stderr)
        return 5
    except OutputSafetyError as exc:
        print(f"[X] Ausgabeziel oder Ergebnis unzulässig: {exc}", file=sys.stderr)
        return 6
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"[X] Reparaturvorschau-Prüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
