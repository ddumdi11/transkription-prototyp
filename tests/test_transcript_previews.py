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
    curate_quality_review,
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
        transcript.with_suffix(".segments.json").write_text(
            json.dumps({
                "schema_version": 1,
                "source_id": "one",
                "transcript_sha256": transcript_hash,
                "segments": [{
                    "id": item["id"],
                    "start": item["start"],
                    "end": item["end"],
                    "raw_text": item["raw_text"],
                    "text": item["text"],
                }],
            }),
            encoding="utf-8",
        )
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

    def test_generation_writes_preview_and_second_run_is_noop(self):
        transcript = self.add_review(["BOUNDARY_ERROR"])
        plan_path, plan = self.freeze_plan()
        output = self.root / "previews"

        code, stdout, stderr = self.invoke(
            plan_path,
            plan["plan_sha256"],
            "--output-dir",
            str(output),
            "--json",
        )

        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        self.assertTrue(result["preview_written"])
        target = Path(result["output_path"])
        preview = target / "recordings" / "one" / "preview.md"
        self.assertEqual(preview.read_bytes(), transcript.read_bytes())

        code, stdout, stderr = self.invoke(
            plan_path,
            plan["plan_sha256"],
            "--output-dir",
            str(output),
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        self.assertFalse(json.loads(stdout)["preview_written"])

    def test_curated_replacement_changes_preview_not_canonical_transcript(self):
        transcript = self.add_review(["WORD_ERROR"])
        row = self.db.execute(
            "SELECT * FROM quality_reviews WHERE drive_id='one' AND is_current=1"
        ).fetchone()
        transcript_hash = transcript_sha256_for_drive(self.db, "one")
        curate_quality_review(
            self.db,
            "one",
            "segment-000001",
            row["candidate_hash"],
            "REPLACE",
            transcript_sha256=transcript_hash,
            confirmed_text="Ein korrigierter Text.",
            now=1040,
        )
        plan_path, plan = self.freeze_plan()
        original = transcript.read_bytes()

        code, stdout, stderr = self.invoke(
            plan_path,
            plan["plan_sha256"],
            "--output-dir",
            str(self.root / "previews"),
            "--json",
        )

        self.assertEqual(code, 0, stderr)
        target = Path(json.loads(stdout)["output_path"])
        preview = target / "recordings" / "one" / "preview.md"
        self.assertIn("Ein korrigierter Text.", preview.read_text(encoding="utf-8"))
        self.assertEqual(transcript.read_bytes(), original)

    def test_curated_hallucination_deletes_only_preview_text(self):
        transcript = self.add_review(["HALLUCINATION"])
        row = self.db.execute(
            "SELECT * FROM quality_reviews WHERE drive_id='one' AND is_current=1"
        ).fetchone()
        transcript_hash = transcript_sha256_for_drive(self.db, "one")
        curate_quality_review(
            self.db,
            "one",
            "segment-000001",
            row["candidate_hash"],
            "DELETE",
            transcript_sha256=transcript_hash,
            now=1040,
        )
        plan_path, plan = self.freeze_plan()
        original = transcript.read_bytes()

        code, stdout, stderr = self.invoke(
            plan_path,
            plan["plan_sha256"],
            "--output-dir",
            str(self.root / "previews"),
            "--json",
        )

        self.assertEqual(code, 0, stderr)
        target = Path(json.loads(stdout)["output_path"])
        preview = target / "recordings" / "one" / "preview.md"
        self.assertNotIn("Ein auffälliger Text.", preview.read_text(encoding="utf-8"))
        self.assertEqual(transcript.read_bytes(), original)

    def test_check_rejects_output_inside_canonical_transcripts(self):
        self.add_review(["BOUNDARY_ERROR"])
        plan_path, plan = self.freeze_plan()

        code, _, stderr = self.invoke(
            plan_path,
            plan["plan_sha256"],
            "--output-dir",
            "staging/transcripts",
            "--check",
        )

        self.assertEqual(code, 6)
        self.assertIn("Ausgabeziel", stderr)

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
