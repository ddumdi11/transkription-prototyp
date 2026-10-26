import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from inbox_watcher import open_state
from plan_transcript_repairs import build_repair_plan, main
from publish_transcripts import prepare_publish_state
from quality_reviews import (
    candidate_hash,
    decide_quality_review,
    ensure_quality_state,
    sync_quality_candidates,
    transcript_sha256_for_drive,
)


def candidate(segment_id: str, text: str = "Ein auffälliger Text.") -> dict:
    return {
        "id": segment_id,
        "start": 10.0,
        "end": 10.2,
        "raw_text": f" {text}",
        "text": text,
        "flags": ["implausible_text_density"],
        "chars_per_second": len(text) / 0.2,
    }


class TranscriptRepairPlanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.sqlite3"
        self.db = open_state(self.state)
        prepare_publish_state(self.db)
        ensure_quality_state(self.db)
        self.candidates: dict[str, list[dict]] = {}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_review(
        self,
        drive_id: str,
        segment_id: str,
        issues: list[str],
        *,
        confirmed_text: str | None = None,
        status: str = "CONFIRMED",
    ) -> None:
        transcript = self.root / f"{drive_id}.md"
        if not transcript.exists():
            transcript.write_text(f"# {drive_id}\n\nKanonischer Text.\n", encoding="utf-8")
            self.db.execute(
                """INSERT INTO transcription_jobs
                   (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
                   VALUES (?, 'DONE', 1, 'audio.wav', ?, NULL, 1000)""",
                (drive_id, str(transcript)),
            )
            self.db.execute(
                """INSERT INTO published_transcripts
                   (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
                   VALUES (?, ?, 'hash', 10, ?, ?, 1010)""",
                (
                    drive_id,
                    str(transcript),
                    f"remote/{transcript.name}",
                    f"remote-{drive_id}",
                ),
            )
        item = candidate(segment_id)
        transcript_sha256 = transcript_sha256_for_drive(self.db, drive_id)
        fingerprint = candidate_hash(item, transcript_sha256)
        current = self.candidates.setdefault(drive_id, [])
        current.append(item)
        sync_quality_candidates(
            self.db,
            drive_id,
            current,
            transcript_sha256=transcript_sha256,
            segment_transcript_sha256=transcript_sha256,
            now=1020,
        )
        if status == "CONFIRMED":
            decide_quality_review(
                self.db,
                drive_id,
                segment_id,
                fingerprint,
                "CONFIRMED",
                transcript_sha256=transcript_sha256,
                issue_types=issues,
                note="Hörprüfung bestätigt.",
                confirmed_text=confirmed_text,
                now=1030,
            )
        self.db.commit()

    def test_classifies_boundary_curation_and_available_text(self):
        self.add_review("one", "segment-000001", ["BOUNDARY_ERROR"])
        self.add_review("two", "segment-000001", ["OMISSION"])
        self.add_review(
            "three",
            "segment-000001",
            ["WORD_ERROR"],
            confirmed_text="Bestätigter Ersatztext",
        )

        plan = build_repair_plan(self.db)

        self.assertEqual(plan["summary"]["recording_count"], 3)
        self.assertEqual(
            plan["summary"]["review_action_counts"],
            {
                "CURATED_REPLACEMENT_AVAILABLE": 1,
                "CURATION_REQUIRED": 1,
                "NO_TEXT_CHANGE": 1,
            },
        )
        statuses = {row["drive_id"]: row["status"] for row in plan["recordings"]}
        self.assertEqual(statuses["one"], "NO_TEXT_CHANGE")
        self.assertEqual(statuses["two"], "CURATION_REQUIRED")
        self.assertEqual(statuses["three"], "CURATED_REPLACEMENT_AVAILABLE")
        self.assertEqual(len(plan["recordings"][0]["transcript_sha256"]), 64)
        self.assertEqual(
            plan["summary"]["review_version_counts"],
            {"MATCHED": 3, "MISMATCH": 0, "UNVERIFIED": 0},
        )

    def test_groups_multiple_reviews_and_ignores_pending(self):
        self.add_review("one", "segment-000001", ["BOUNDARY_ERROR"])
        self.add_review("one", "segment-000002", ["OMISSION"])
        self.add_review("pending", "segment-000001", [], status="PENDING")

        plan = build_repair_plan(self.db)

        self.assertEqual(plan["summary"]["recording_count"], 1)
        self.assertEqual(plan["summary"]["review_count"], 2)
        self.assertEqual(plan["recordings"][0]["drive_id"], "one")
        self.assertEqual(plan["recordings"][0]["status"], "CURATION_REQUIRED")
        self.assertEqual(len(plan["recordings"][0]["reviews"]), 2)

    def test_filter_rejects_drive_id_without_confirmed_review(self):
        self.add_review("one", "segment-000001", ["BOUNDARY_ERROR"])
        self.add_review("pending", "segment-000001", [], status="PENDING")

        with self.assertRaisesRegex(ValueError, "Keine aktuelle bestätigte"):
            build_repair_plan(self.db, {"pending"})
        filtered = build_repair_plan(self.db, {"one"})
        self.assertEqual([row["drive_id"] for row in filtered["recordings"]], ["one"])

    def test_cli_reads_readonly_database_and_emits_json(self):
        self.add_review("one", "segment-000001", ["BOUNDARY_ERROR"])
        self.db.close()
        before = self.state.read_bytes()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--state", str(self.state), "--json"])

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["summary"]["review_count"], 1)
        self.assertEqual(self.state.read_bytes(), before)
        self.db = open_state(self.state)

    def test_missing_transcript_fails_closed(self):
        self.add_review("one", "segment-000001", ["OMISSION"])
        Path(self.db.execute(
            "SELECT transcript_path FROM transcription_jobs WHERE drive_id='one'"
        ).fetchone()[0]).unlink()

        with self.assertRaisesRegex(ValueError, "Lokales Transkript fehlt"):
            build_repair_plan(self.db)

    def test_missing_job_is_not_silently_omitted(self):
        self.add_review("one", "segment-000001", ["OMISSION"])
        self.db.execute("DELETE FROM transcription_jobs WHERE drive_id='one'")
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "Transkriptpfad fehlt"):
            build_repair_plan(self.db)

    def test_changed_transcript_marks_decision_as_mismatch(self):
        self.add_review("one", "segment-000001", ["OMISSION"])
        transcript = Path(self.db.execute(
            "SELECT transcript_path FROM transcription_jobs WHERE drive_id='one'"
        ).fetchone()[0])
        transcript.write_text("Nach der Hörentscheidung geändert.\n", encoding="utf-8")

        plan = build_repair_plan(self.db)

        self.assertEqual(
            plan["recordings"][0]["transcript_version_status"], "MISMATCH"
        )
        self.assertEqual(
            plan["recordings"][0]["reviews"][0]["transcript_version_status"],
            "MISMATCH",
        )

    def test_legacy_decision_without_hash_is_unverified(self):
        self.add_review("one", "segment-000001", ["BOUNDARY_ERROR"])
        self.db.execute(
            "UPDATE quality_reviews SET transcript_sha256=NULL WHERE drive_id='one'"
        )
        self.db.commit()

        plan = build_repair_plan(self.db)

        self.assertEqual(
            plan["recordings"][0]["transcript_version_status"], "UNVERIFIED"
        )
        self.assertIsNone(
            plan["recordings"][0]["reviews"][0]["decision_transcript_sha256"]
        )


if __name__ == "__main__":
    unittest.main()
