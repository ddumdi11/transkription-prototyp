import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inbox_pipeline import ensure_pipeline_state
from inbox_watcher import open_state
from publish_transcripts import (ensure_publish_state, pending_publications,
                                 prepare_publish_state, publish_one)


class PublishTranscriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = open_state(self.root / "state.sqlite3")
        ensure_pipeline_state(self.db)
        ensure_publish_state(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_done_job(self, drive_id="audio-id", audio_name="Aufnahme #1.wav"):
        transcript = self.root / "transcript.md"
        transcript.write_text("# Test\n\nHallo\n", encoding="utf-8")
        self.db.execute(
            """INSERT INTO files
               (drive_id,path,size,content_hash,hash_type,mod_time,first_seen,last_seen,
                stable_observations,status,duplicate_of)
               VALUES (?, ?, 100, 'audiohash', 'sha256', NULL, 1000, 1010, 2, 'READY', NULL)""",
            (drive_id, audio_name),
        )
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES (?, 'DONE', 1, 'audio.wav', ?, NULL, 1020)""",
            (drive_id, str(transcript)),
        )
        self.db.commit()
        return transcript

    def test_pending_contains_done_unpublished_job(self):
        self.add_done_job()
        self.assertEqual([row["drive_id"] for row in pending_publications(self.db)],
                         ["audio-id"])

    def test_publish_planning_initializes_fresh_database(self):
        fresh = open_state(self.root / "fresh-state.sqlite3")
        try:
            prepare_publish_state(fresh)
            self.assertEqual(pending_publications(fresh), [])
        finally:
            fresh.close()

    def test_publish_verifies_remote_and_is_idempotent(self):
        transcript = self.add_done_job()
        digest = hashlib.sha256(transcript.read_bytes()).hexdigest()
        remote_row = {
            "Path": "Aufnahme #1__audio-id.md", "Size": transcript.stat().st_size,
            "ID": "remote-id", "Hashes": {"sha256": digest},
        }
        listings = iter([[], [remote_row]])

        def fake_run(command, **kwargs):
            if command[1] == "lsjson":
                return subprocess.CompletedProcess(command, 0,
                                                   stdout=json.dumps(next(listings)), stderr="")
            self.assertEqual(command[1], "copyto")
            self.assertIn("--immutable", command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch("publish_transcripts.subprocess.run", side_effect=fake_run) as run:
            name, uploaded = publish_one(self.db, "gdrive,target:", "audio-id", 1030)
        self.assertTrue(uploaded)
        self.assertEqual(name, remote_row["Path"])
        self.assertEqual(run.call_count, 3)
        self.assertEqual(pending_publications(self.db), [])

        name_again, uploaded_again = publish_one(
            self.db, "gdrive,target:", "audio-id", 1040
        )
        self.assertEqual(name_again, name)
        self.assertFalse(uploaded_again)


if __name__ == "__main__":
    unittest.main()
