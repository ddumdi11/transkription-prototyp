import tempfile
import unittest
from pathlib import Path

from inbox_pipeline import activate_from_id, activation_cutoff, ensure_pipeline_state, pending_ready
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


if __name__ == "__main__":
    unittest.main()
