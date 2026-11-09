import hashlib
import io
import json
from pathlib import Path
from unittest import mock
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from bind_legacy_segment_metadata import (
    build_binding_plan,
    main,
    plan_sha256,
    write_payload_atomic,
)
from inbox_watcher import open_state
from publish_transcripts import prepare_publish_state


class LegacySegmentMetadataBindingTest(unittest.TestCase):
    """Exercise conservative planning and atomic legacy-sidecar binding."""

    def setUp(self):
        """Create an isolated pipeline database for every test."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.sqlite3"
        self.db = open_state(self.state)
        prepare_publish_state(self.db)

    def tearDown(self):
        """Close fixture resources and remove the temporary directory."""
        if self.db is not None:
            self.db.close()
        self.temp.cleanup()

    def add_recording(
        self,
        drive_id="legacy",
        *,
        body="Erster Satz. Zweiter Satz.",
        segment_texts=("Erster Satz.", "Zweiter Satz."),
        embedded_hash=None,
        corrected=False,
    ):
        """Create one published transcript and its optional legacy sidecar."""
        transcript = self.root / f"{drive_id}.md"
        status = "Rohtranskript (LLM-korrigiert)" if corrected else "Rohtranskript"
        transcript.write_text(
            "\n".join([
                "# Aufnahme #1",
                f"- Datei: {drive_id}.wav",
                "- Datum: 2026-01-01",
                "- Modell: medium (local)",
                "- Quelle: Einzelaufnahme",
                "- Kontext:",
                f"- Status: {status}",
                "",
                body,
                "",
            ]),
            encoding="utf-8",
        )
        transcript_hash = hashlib.sha256(transcript.read_bytes()).hexdigest()
        payload = {
            "schema_version": 1,
            "audio_file": f"{drive_id}.wav",
            "transcript_file": transcript.name,
            "provider": "local",
            "model": "medium",
            "language": "de",
            "source_id": drive_id,
            "segments": [
                {
                    "id": f"segment-{index:06d}",
                    "start": float(index),
                    "end": float(index + 1),
                    "raw_text": f" {text} ",
                    "text": text,
                }
                for index, text in enumerate(segment_texts, 1)
            ],
        }
        if embedded_hash is not None:
            payload["transcript_sha256"] = (
                transcript_hash if embedded_hash == "current" else embedded_hash
            )
        sidecar = transcript.with_suffix(".segments.json")
        sidecar.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES (?, 'DONE', 1, ?, ?, NULL, 1)""",
            (drive_id, str(self.root / f"{drive_id}.wav"), str(transcript)),
        )
        self.db.execute(
            """INSERT INTO published_transcripts
               (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (
                drive_id,
                str(transcript),
                transcript_hash,
                transcript.stat().st_size,
                transcript.name,
                f"remote-{drive_id}",
            ),
        )
        self.db.commit()
        return transcript, sidecar, transcript_hash

    def close_db(self):
        """Close the fixture connection before exercising CLI read-only opens."""
        self.db.close()
        self.db = None

    def test_plan_marks_reconstructable_legacy_sidecar_eligible(self):
        """Matching publication and segment evidence makes a legacy file eligible."""
        _, _, transcript_hash = self.add_recording()

        plan = build_binding_plan(self.db)

        self.assertEqual(
            plan["summary"]["status_counts"],
            {"ELIGIBLE": 1, "ALREADY_BOUND": 0, "HOLD": 0},
        )
        recording = plan["recordings"][0]
        self.assertEqual(recording["transcript_sha256"], transcript_hash)
        self.assertEqual(recording["reasons"], [])
        self.assertEqual(len(plan["plan_sha256"]), 64)

    def test_dry_run_preserves_database_and_sidecar_bytes(self):
        """Planning must leave both SQLite and segment bytes unchanged."""
        _, sidecar, _ = self.add_recording()
        self.close_db()
        database_before = self.state.read_bytes()
        sidecar_before = sidecar.read_bytes()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--state", str(self.state), "--json"])

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(
            json.loads(stdout.getvalue())["summary"]["recording_count"], 1
        )
        self.assertEqual(self.state.read_bytes(), database_before)
        self.assertEqual(sidecar.read_bytes(), sidecar_before)

    def test_confirmed_plan_adds_hash_and_preserves_mtime(self):
        """Confirmation adds only the binding while retaining historical mtime."""
        _, sidecar, transcript_hash = self.add_recording()
        original_payload = json.loads(sidecar.read_text(encoding="utf-8"))
        original_mtime = sidecar.stat().st_mtime_ns
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
            json.loads(stdout.getvalue())["binding_result"]["changed_sidecars"],
            1,
        )
        updated = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(updated["transcript_sha256"], transcript_hash)
        without_hash = dict(updated)
        without_hash.pop("transcript_sha256")
        self.assertEqual(without_hash, original_payload)
        self.assertEqual(sidecar.stat().st_mtime_ns, original_mtime)

        self.db = open_state(self.state)
        repeated = build_binding_plan(self.db)
        self.assertEqual(
            repeated["summary"]["status_counts"]["ALREADY_BOUND"], 1
        )

    def test_whitespace_only_differences_still_reconstruct(self):
        """Whitespace layout alone does not invalidate equivalent transcript text."""
        self.add_recording(body="Erster  Satz.\n\nZweiter\tSatz.")

        recording = build_binding_plan(self.db)["recordings"][0]

        self.assertEqual(recording["status"], "ELIGIBLE")

    def test_changed_text_and_llm_corrected_transcripts_are_held(self):
        """Divergent or freely corrected transcript bodies fail closed."""
        self.add_recording("changed", body="Anderer Text.")
        self.add_recording("corrected", corrected=True)

        plan = build_binding_plan(self.db)
        by_id = {row["drive_id"]: row for row in plan["recordings"]}

        self.assertEqual(by_id["changed"]["status"], "HOLD")
        self.assertIn(
            "segment_text_reconstruction_mismatch", by_id["changed"]["reasons"]
        )
        self.assertEqual(by_id["corrected"]["status"], "HOLD")
        self.assertIn("llm_corrected_transcript", by_id["corrected"]["reasons"])

    def test_published_or_embedded_hash_mismatch_is_held(self):
        """Conflicting publication or sidecar version evidence remains on HOLD."""
        _, _, _ = self.add_recording("published")
        self.db.execute(
            "UPDATE published_transcripts SET local_hash=? WHERE drive_id=?",
            ("a" * 64, "published"),
        )
        self.add_recording("embedded", embedded_hash="b" * 64)
        self.db.commit()

        by_id = {
            row["drive_id"]: row
            for row in build_binding_plan(self.db)["recordings"]
        }

        self.assertIn("published_hash_mismatch", by_id["published"]["reasons"])
        self.assertIn(
            "segment_transcript_hash_mismatch", by_id["embedded"]["reasons"]
        )

    def test_confirmation_rejects_changed_plan_and_hold_plan(self):
        """A stale plan or an unchanged plan containing HOLD writes nothing."""
        transcript, sidecar, _ = self.add_recording()
        plan_hash = build_binding_plan(self.db)["plan_sha256"]
        before = sidecar.read_bytes()
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
        self.assertEqual(sidecar.read_bytes(), before)

        self.db = open_state(self.state)
        current_plan = build_binding_plan(self.db)
        self.close_db()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main([
                "--state", str(self.state),
                "--confirm-plan-sha256", current_plan["plan_sha256"],
            ])
        self.assertEqual(code, 1)
        self.assertIn("HOLD-Fälle", stderr.getvalue())

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self):
        """Installation failure retains old bytes and removes the temporary file."""
        _, sidecar, transcript_hash = self.add_recording()
        before = sidecar.read_bytes()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["transcript_sha256"] = transcript_hash

        with mock.patch(
            "bind_legacy_segment_metadata.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(OSError):
                write_payload_atomic(
                    sidecar,
                    payload,
                    hashlib.sha256(before).hexdigest(),
                    sidecar.stat().st_mtime_ns,
                )

        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(list(self.root.glob(f".{sidecar.name}.*.tmp")), [])

    def test_plan_hash_excludes_only_its_own_field(self):
        """The plan digest ignores itself but covers all binding evidence."""
        self.add_recording()
        plan = build_binding_plan(self.db)
        stored = plan["plan_sha256"]

        plan["plan_sha256"] = "0" * 64
        self.assertEqual(plan_sha256(plan), stored)
        plan["recordings"][0]["transcript_size"] += 1
        self.assertNotEqual(plan_sha256(plan), stored)

    def test_selected_unknown_drive_id_fails(self):
        """An explicit selection may not silently omit an unknown Drive ID."""
        self.add_recording()

        with self.assertRaisesRegex(ValueError, "Unbekannte Drive-ID"):
            build_binding_plan(self.db, {"missing"})

    def test_eligible_only_plan_excludes_holds_but_binds_observed_counts(self):
        """Eligible-only plans retain aggregate evidence about excluded states."""
        self.add_recording("eligible")
        self.add_recording("hold", body="Abweichender Text.")

        plan = build_binding_plan(self.db, eligible_only=True)

        self.assertEqual(
            [row["drive_id"] for row in plan["recordings"]], ["eligible"]
        )
        self.assertEqual(plan["summary"]["recording_count"], 1)
        self.assertEqual(plan["summary"]["observed_recording_count"], 2)
        self.assertEqual(
            plan["summary"]["observed_status_counts"],
            {"ELIGIBLE": 1, "ALREADY_BOUND": 0, "HOLD": 1},
        )

    def test_eligible_only_confirmation_rejects_newly_eligible_file(self):
        """A changed observed classification invalidates the frozen subset plan."""
        self.add_recording("eligible")
        transcript, _, _ = self.add_recording(
            "hold", body="Abweichender Text."
        )
        plan = build_binding_plan(self.db, eligible_only=True)
        transcript.write_text(
            transcript.read_text(encoding="utf-8").replace(
                "Abweichender Text.", "Erster Satz. Zweiter Satz."
            ),
            encoding="utf-8",
        )
        transcript_hash = hashlib.sha256(transcript.read_bytes()).hexdigest()
        self.db.execute(
            """UPDATE published_transcripts
               SET local_hash=?, size=? WHERE drive_id='hold'""",
            (transcript_hash, transcript.stat().st_size),
        )
        self.db.commit()
        self.close_db()
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            code = main([
                "--state", str(self.state),
                "--eligible-only",
                "--confirm-plan-sha256", plan["plan_sha256"],
            ])

        self.assertEqual(code, 1)
        self.assertIn("seit dem Dry-Run geändert", stderr.getvalue())

    def test_eligible_only_confirmation_binds_eligible_and_leaves_hold(self):
        """Eligible-only confirmation never modifies excluded HOLD sidecars."""
        _, eligible_sidecar, eligible_hash = self.add_recording("eligible")
        _, hold_sidecar, _ = self.add_recording(
            "hold", body="Abweichender Text."
        )
        hold_before = hold_sidecar.read_bytes()
        plan = build_binding_plan(self.db, eligible_only=True)
        self.close_db()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "--state", str(self.state),
                "--eligible-only",
                "--confirm-plan-sha256", plan["plan_sha256"],
            ])

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(
            json.loads(eligible_sidecar.read_text(encoding="utf-8"))[
                "transcript_sha256"
            ],
            eligible_hash,
        )
        self.assertEqual(hold_sidecar.read_bytes(), hold_before)


if __name__ == "__main__":
    unittest.main()
