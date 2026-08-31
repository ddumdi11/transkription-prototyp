import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from inbox_pipeline import (activate_from_id, activation_cutoff, ensure_pipeline_state,
                            notify_auth_failure, pending_ready, publish_completed,
                            publish_pending)
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
        with patch("inbox_pipeline.publish_one", return_value=("new.md", True)) as publish:
            failures = publish_completed(
                self.db, "gdrive,target:", "drive-id", logger
            )
        self.assertEqual(failures, 0)
        publish.assert_called_once_with(
            self.db, "gdrive,target:", "drive-id", unittest.mock.ANY
        )

    def test_publish_completed_failure_is_deferred_without_raising(self):
        logger = Mock()
        with patch("inbox_pipeline.publish_one", side_effect=RuntimeError("Drive offline")):
            failures = publish_completed(
                self.db, "gdrive,target:", "drive-id", logger
            )
        self.assertEqual(failures, 1)
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
