import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from build_transcript_previews import PlanFormatError, main, validate_plan_gate
from inbox_watcher import open_state
from plan_transcript_repairs import build_repair_plan
from publish_transcripts import prepare_publish_state
from quality_reviews import (
    candidate_hash,
    decide_quality_review,
    ensure_quality_state,
    sync_quality_candidates,
    transcript_sha256_for_drive,
)


def candidate() -> dict:
    text = "Ein auffälliger Text."
    return {
        "id": "segment-000001",
        "start": 10.0,
        "end": 10.2,
        "raw_text": f" {text}",
        "text": text,
        "flags": ["implausible_text_density"],
        "chars_per_second": len(text) / 0.2,
    }


class TranscriptPreviewCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.sqlite3"
        self.db = open_state(self.state)
        prepare_publish_state(self.db)
        ensure_quality_state(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_review(self, issues: list[str]) -> Path:
        transcript = self.root / "one.md"
        transcript.write_text("# one\n\nEin auffälliger Text.\n", encoding="utf-8")
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES ('one', 'DONE', 1, 'audio.wav', ?, NULL, 1000)""",
            (str(transcript),),
        )
        self.db.execute(
            """INSERT INTO published_transcripts
               (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
               VALUES ('one', ?, 'hash', 10, 'remote/one.md', 'remote-one', 1010)""",
            (str(transcript),),
        )
        item = candidate()
        transcript_hash = transcript_sha256_for_drive(self.db, "one")
        fingerprint = candidate_hash(item, transcript_hash)
        sync_quality_candidates(
            self.db,
            "one",
            [item],
            transcript_sha256=transcript_hash,
            segment_transcript_sha256=transcript_hash,
            now=1020,
        )
        decide_quality_review(
            self.db,
            "one",
            "segment-000001",
            fingerprint,
            "CONFIRMED",
            transcript_sha256=transcript_hash,
            issue_types=issues,
            now=1030,
        )
        self.db.commit()
        return transcript

    def freeze_plan(self) -> tuple[Path, dict]:
        plan = build_repair_plan(self.db)
        path = self.root / "plan.json"
        path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path, plan

    def invoke(self, plan_path: Path, plan_hash: str, *extra: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = [
            "--plan",
            str(plan_path),
            "--expect-plan-sha256",
            plan_hash,
            "--state",
            str(self.state),
            *extra,
        ]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_check_is_readonly_and_reports_verified_plan(self):
        self.add_review(["BOUNDARY_ERROR"])
        plan_path, plan = self.freeze_plan()
        before_state = self.state.read_bytes()
        before_paths = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))

        code, stdout, stderr = self.invoke(
            plan_path,
            plan["plan_sha256"],
            "--check",
            "--json",
        )

        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["check"], "PASSED")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["preview_written"])
        self.assertEqual(self.state.read_bytes(), before_state)
        self.assertEqual(
            sorted(path.relative_to(self.root) for path in self.root.rglob("*")),
            before_paths,
        )

    def test_tampered_plan_hash_is_rejected(self):
        self.add_review(["BOUNDARY_ERROR"])
        plan_path, plan = self.freeze_plan()
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        payload["summary"]["review_count"] = 99
        plan_path.write_text(json.dumps(payload), encoding="utf-8")

        code, _, stderr = self.invoke(
            plan_path, plan["plan_sha256"], "--check"
        )

        self.assertEqual(code, 2)
        self.assertIn("Hash stimmt nicht", stderr)

    def test_hold_rejects_entire_plan_before_current_state_check(self):
        self.add_review(["OMISSION"])
        plan_path, plan = self.freeze_plan()

        code, _, stderr = self.invoke(
            plan_path, plan["plan_sha256"], "--check"
        )

        self.assertEqual(code, 3)
        self.assertIn("gesamte Lauf bleibt gesperrt", stderr)

    def test_changed_transcript_rejects_stale_frozen_plan(self):
        transcript = self.add_review(["BOUNDARY_ERROR"])
        plan_path, plan = self.freeze_plan()
        transcript.write_text("Nach dem Plan verändert.\n", encoding="utf-8")

        code, _, stderr = self.invoke(
            plan_path, plan["plan_sha256"], "--check"
        )

        self.assertEqual(code, 4)
        self.assertIn("veraltet", stderr)

    def test_without_check_does_not_create_preview(self):
        self.add_review(["BOUNDARY_ERROR"])
        plan_path, plan = self.freeze_plan()

        code, _, stderr = self.invoke(plan_path, plan["plan_sha256"])

        self.assertEqual(code, 2)
        self.assertIn("--check verwenden", stderr)

    def test_non_string_gate_values_raise_plan_format_error(self):
        self.add_review(["BOUNDARY_ERROR"])
        base = build_repair_plan(self.db)
        mutations = (
            (base["recordings"][0], "status"),
            (base["recordings"][0], "transcript_version_status"),
            (base["recordings"][0]["reviews"][0], "action"),
            (
                base["recordings"][0]["reviews"][0],
                "transcript_version_status",
            ),
        )

        for target, field in mutations:
            original = target[field]
            target[field] = []
            with self.subTest(field=field):
                with self.assertRaises(PlanFormatError):
                    validate_plan_gate(base)
            target[field] = original


if __name__ == "__main__":
    unittest.main()
