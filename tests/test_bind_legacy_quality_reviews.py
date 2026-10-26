import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from analyze_segment_quality import find_suspicious_segments
from bind_legacy_quality_reviews import (
    build_binding_plan,
    legacy_candidate_hash,
    main,
)
from inbox_watcher import open_state
from publish_transcripts import prepare_publish_state
from quality_reviews import (
    candidate_hash,
    decide_quality_review,
    ensure_quality_state,
    open_readonly_state,
    sync_quality_candidates,
)


class LegacyQualityBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.sqlite3"
        self.db = open_state(self.state)
        prepare_publish_state(self.db)
        ensure_quality_state(self.db)
        self.decision_time = time.time() + 120

    def tearDown(self):
        if self.db is not None:
            self.db.close()
        self.temp.cleanup()

    def add_review(self, drive_id="legacy", *, legacy=True):
        transcript = self.root / f"{drive_id}.md"
        transcript.write_text("# Test\n\nKanonischer Text.\n", encoding="utf-8")
        transcript_hash = hashlib.sha256(transcript.read_bytes()).hexdigest()
        payload = {
            "schema_version": 1,
            "audio_file": f"{drive_id}.wav",
            "transcript_file": transcript.name,
            "provider": "local",
            "model": "medium",
            "language": "de",
            "source_id": drive_id,
            "segments": [{
                "id": "segment-000001",
                "start": 10.0,
                "end": 10.1,
                "raw_text": " Sehr viel Text für eine Zehntelsekunde. ",
                "text": "Sehr viel Text für eine Zehntelsekunde.",
            }],
        }
        sidecar = transcript.with_suffix(".segments.json")
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        candidate = find_suspicious_segments(payload)[0]
        fingerprint = candidate_hash(candidate, transcript_hash)
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES (?, 'DONE', 1, 'audio.wav', ?, NULL, ?)""",
            (drive_id, str(transcript), self.decision_time - 200),
        )
        self.db.execute(
            """INSERT INTO published_transcripts
               (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                drive_id,
                str(transcript),
                transcript_hash,
                transcript.stat().st_size,
                transcript.name,
                f"remote-{drive_id}",
                self.decision_time - 60,
            ),
        )
        sync_quality_candidates(
            self.db,
            drive_id,
            [candidate],
            transcript_sha256=transcript_hash,
            segment_transcript_sha256=transcript_hash,
            now=self.decision_time - 30,
        )
        decide_quality_review(
            self.db,
            drive_id,
            "segment-000001",
            fingerprint,
            "CONFIRMED",
            transcript_sha256=transcript_hash,
            issue_types=["OMISSION"],
            confirmed_text="Kuratierter Ersatztext.",
            now=self.decision_time,
        )
        if legacy:
            self.db.execute(
                """UPDATE quality_reviews
                   SET candidate_hash=?, candidate_transcript_sha256=NULL,
                       transcript_sha256=NULL
                   WHERE drive_id=?""",
                (legacy_candidate_hash(candidate), drive_id),
            )
        self.db.commit()
        return transcript, sidecar, transcript_hash

    def close_db(self):
        self.db.close()
        self.db = None

    def test_plan_marks_fully_evidenced_legacy_review_eligible(self):
        _, _, transcript_hash = self.add_review()

        plan = build_binding_plan(self.db)

        self.assertEqual(
            plan["summary"]["review_status_counts"],
            {"ELIGIBLE": 1, "ALREADY_BOUND": 0, "HOLD": 0},
        )
        recording = plan["recordings"][0]
        self.assertEqual(recording["transcript_sha256"], transcript_hash)
        self.assertEqual(recording["status"], "ELIGIBLE")
        self.assertEqual(recording["reviews"][0]["reasons"], [])
        self.assertEqual(len(plan["plan_sha256"]), 64)

    def test_dry_run_keeps_database_byte_identical(self):
        self.add_review()
        self.close_db()
        before = self.state.read_bytes()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--state", str(self.state), "--json"])

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(
            json.loads(stdout.getvalue())["summary"]["review_count"], 1
        )
        self.assertEqual(self.state.read_bytes(), before)

    def test_confirmed_plan_binds_hashes_and_candidate_fingerprint(self):
        _, _, transcript_hash = self.add_review()
        old_hash = self.db.execute(
            "SELECT candidate_hash FROM quality_reviews"
        ).fetchone()[0]
        plan_hash = build_binding_plan(self.db)["plan_sha256"]
        self.close_db()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "--state", str(self.state),
                "--confirm-plan-sha256", plan_hash,
                "--json",
            ])

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(
            json.loads(stdout.getvalue())["binding_result"]["changed_reviews"],
            1,
        )
        self.db = open_state(self.state)
        row = self.db.execute("SELECT * FROM quality_reviews").fetchone()
        self.assertEqual(row["candidate_transcript_sha256"], transcript_hash)
        self.assertEqual(row["transcript_sha256"], transcript_hash)
        self.assertNotEqual(row["candidate_hash"], old_hash)
        rebound = build_binding_plan(self.db)
        self.assertEqual(
            rebound["summary"]["review_status_counts"]["ALREADY_BOUND"], 1
        )

    def test_changed_transcript_is_held(self):
        transcript, _, _ = self.add_review()
        transcript.write_text("Nachträglich verändert.\n", encoding="utf-8")

        review = build_binding_plan(self.db)["recordings"][0]["reviews"][0]

        self.assertEqual(review["status"], "HOLD")
        self.assertIn("published_hash_mismatch", review["reasons"])

    def test_changed_segment_candidate_is_held(self):
        _, sidecar, _ = self.add_review()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["segments"][0]["text"] = "Anderer erkannter Text."
        payload["segments"][0]["raw_text"] = " Anderer erkannter Text. "
        sidecar.write_text(json.dumps(payload), encoding="utf-8")

        review = build_binding_plan(self.db)["recordings"][0]["reviews"][0]

        self.assertEqual(review["status"], "HOLD")
        self.assertTrue(
            {"detector_candidate_missing", "candidate_fingerprint_mismatch"}
            & set(review["reasons"])
        )

    def test_confirmation_rejects_changed_or_held_plan(self):
        transcript, _, _ = self.add_review()
        plan_hash = build_binding_plan(self.db)["plan_sha256"]
        transcript.write_text("Nach dem Dry-Run verändert.\n", encoding="utf-8")
        self.close_db()
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            code = main([
                "--state", str(self.state),
                "--confirm-plan-sha256", plan_hash,
            ])

        self.assertEqual(code, 1)
        self.assertIn("seit dem Dry-Run geändert", stderr.getvalue())
        self.db = open_state(self.state)
        row = self.db.execute(
            "SELECT transcript_sha256 FROM quality_reviews"
        ).fetchone()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
