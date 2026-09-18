import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from inbox_pipeline import (activate_from_id, activation_cutoff, ensure_pipeline_state,
                            log_routing_plan, notify_auth_failure, pending_ready,
                            publish_completed, publish_pending, transcribe_one,
                            validate_segment_metadata)
from inbox_watcher import classify, open_state


def audio(drive_id, path, digest):
    return {"ID": drive_id, "Path": path, "Size": 100, "IsDir": False,
            "ModTime": "2026-08-23T10:00:00Z", "Hashes": {"sha256": digest}}


class InboxPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = open_state(Path(self.temp.name) / "state.sqlite3")
        ensure_pipeline_state(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_activation_excludes_old_ready_and_duplicate(self):
        old = audio("old", "old.wav", "aaa")
        new = audio("a-new", "new.wav", "bbb")
        duplicate = audio("z-copy", "copy.wav", "bbb")
        classify(self.db, [old], 1000, 10)
        classify(self.db, [old], 1011, 10)
        classify(self.db, [old, new, duplicate], 2000, 10)
        classify(self.db, [old, new, duplicate], 2011, 10)

        cutoff = activate_from_id(self.db, "a-new")
        self.assertEqual(activation_cutoff(self.db), cutoff)
        self.assertEqual([row["drive_id"] for row in pending_ready(self.db, cutoff)], ["a-new"])

    def test_done_job_is_not_pending_again(self):
        item = audio("new", "new.wav", "bbb")
        classify(self.db, [item], 2000, 10)
        classify(self.db, [item], 2011, 10)
        cutoff = activate_from_id(self.db, "new")
        self.db.execute(
            "INSERT INTO transcription_jobs (drive_id,status,attempts,updated_at) VALUES (?, 'DONE', 1, ?)",
            ("new", 2020),
        )
        self.db.commit()
        self.assertEqual(pending_ready(self.db, cutoff), [])

    def test_transcribe_one_requires_and_requests_segment_metadata(self):
        audio_path = Path(self.temp.name) / "Aufnahme #1__drive-id.wav"
        audio_path.write_bytes(b"audio")
        output_dir = Path(self.temp.name) / "transcripts"

        def create_outputs(command, check):
            self.assertTrue(check)
            self.assertIn("--write-segments", command)
            source_index = command.index("--source-id")
            self.assertEqual(command[source_index + 1], "drive-id")
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"{audio_path.stem}.md").write_text(
                "Transkript", encoding="utf-8"
            )
            (output_dir / f"{audio_path.stem}.segments.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "source_id": "drive-id",
                    "segments": [],
                }),
                encoding="utf-8",
            )

        with (patch("inbox_pipeline.OUTPUT_DIR", output_dir),
              patch("inbox_pipeline.subprocess.run", side_effect=create_outputs)):
            transcript = transcribe_one(
                self.db, "drive-id", audio_path, now=1000
            )

        self.assertEqual(transcript, output_dir / f"{audio_path.stem}.md")
        row = self.db.execute(
            "SELECT status FROM transcription_jobs WHERE drive_id='drive-id'"
        ).fetchone()
        self.assertEqual(row["status"], "DONE")

    def test_transcribe_one_fails_when_segment_metadata_is_missing(self):
        audio_path = Path(self.temp.name) / "Aufnahme #2__drive-id.wav"
        audio_path.write_bytes(b"audio")
        output_dir = Path(self.temp.name) / "transcripts"

        def create_transcript_only(_command, check):
            self.assertTrue(check)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"{audio_path.stem}.md").write_text(
                "Transkript", encoding="utf-8"
            )

        with (patch("inbox_pipeline.OUTPUT_DIR", output_dir),
              patch("inbox_pipeline.subprocess.run",
                    side_effect=create_transcript_only)):
            with self.assertRaisesRegex(RuntimeError, "Segmentdaten"):
                transcribe_one(self.db, "drive-id", audio_path, now=1000)

        row = self.db.execute(
            "SELECT status FROM transcription_jobs WHERE drive_id='drive-id'"
        ).fetchone()
        self.assertEqual(row["status"], "FAILED")

    def test_segment_metadata_must_match_drive_id(self):
        path = Path(self.temp.name) / "segments.json"
        path.write_text(
            json.dumps({
                "schema_version": 1,
                "source_id": "other-id",
                "segments": [],
            }),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "Drive-ID drive-id"):
            validate_segment_metadata(path, "drive-id")

    def test_publish_pending_processes_done_job(self):
        item = audio("new", "new.wav", "bbb")
        classify(self.db, [item], 2000, 10)
        classify(self.db, [item], 2011, 10)
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES ('new','DONE',1,'audio.wav','transcript.md',NULL,2020)"""
        )
        self.db.commit()
        logger = Mock()
        with patch("inbox_pipeline.publish_one", return_value=("new.md", True)) as publish:
            failures = publish_pending(self.db, "gdrive,target:", logger)
        self.assertEqual(failures, 0)
        publish.assert_called_once_with(self.db, "gdrive,target:", "new", unittest.mock.ANY)

    def test_publish_completed_uploads_exact_job(self):
        logger = Mock()
        with (patch("inbox_pipeline.publish_one", return_value=("new.md", True)) as publish,
              patch("inbox_pipeline.log_routing_plan") as route):
            failures = publish_completed(
                self.db, "gdrive,target:", "drive-id", logger
            )
        self.assertEqual(failures, 0)
        publish.assert_called_once_with(
            self.db, "gdrive,target:", "drive-id", unittest.mock.ANY
        )
        route.assert_called_once_with(self.db, "drive-id", logger)

    def test_publish_completed_failure_is_deferred_without_raising(self):
        logger = Mock()
        with patch("inbox_pipeline.publish_one", side_effect=RuntimeError("Drive offline")):
            failures = publish_completed(
                self.db, "gdrive,target:", "drive-id", logger
            )
        self.assertEqual(failures, 1)
        logger.exception.assert_called_once()

    def test_log_routing_plan_reports_projects_and_topics(self):
        logger = Mock()
        plan = {
            "audio_path": "Aufnahme #626.wav",
            "topics": ["workflow-test"],
            "projects": [
                {"name": "Z04", "reasons": ["default"]},
                {"name": "IT-Dienstleistungen Probephase",
                 "reasons": ["content:KI-Lotse"]},
            ],
        }
        with (patch("inbox_pipeline.load_config", return_value={}),
              patch("inbox_pipeline.plan_published", return_value=plan)):
            result = log_routing_plan(self.db, "drive-id", logger)
        self.assertTrue(result)
        messages = [call.args[0] for call in logger.info.call_args_list]
        self.assertEqual(messages, ["Job ROUTED id=%s path=%r topics=%s", "  -> %s [%s]",
                                    "  -> %s [%s]"])

    def test_log_routing_failure_does_not_raise(self):
        logger = Mock()
        with patch("inbox_pipeline.load_config", side_effect=ValueError("bad config")):
            result = log_routing_plan(self.db, "drive-id", logger)
        self.assertFalse(result)
        logger.exception.assert_called_once()

    def test_auth_notification_is_persistent_and_rate_limited(self):
        logger = Mock()
        marker = Path(self.temp.name) / "auth-required"
        with (patch("inbox_pipeline.STATE_DIR", Path(self.temp.name)),
              patch("inbox_pipeline.AUTH_ALERT", marker),
              patch("inbox_pipeline.subprocess.run") as run):
            self.assertTrue(notify_auth_failure(RuntimeError("invalid_grant"), logger, 1000))
            self.assertFalse(notify_auth_failure(RuntimeError("invalid_grant"), logger, 1100))
        run.assert_called_once()
        self.assertTrue(marker.exists())

    def test_unrelated_failure_does_not_notify(self):
        logger = Mock()
        with patch("inbox_pipeline.subprocess.run") as run:
            self.assertFalse(notify_auth_failure(RuntimeError("network timeout"), logger, 1000))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
