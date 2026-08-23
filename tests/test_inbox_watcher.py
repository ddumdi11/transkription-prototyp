import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inbox_watcher import classify, open_state, stage_ready_file, staging_name


def audio(drive_id, path, digest, size=100):
    return {"ID": drive_id, "Path": path, "Size": size, "IsDir": False,
            "ModTime": "2026-08-23T10:00:00Z", "Hashes": {"sha256": digest}}


class InboxWatcherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = open_state(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_requires_two_spaced_unchanged_observations(self):
        item = audio("id-1", "eins.wav", "aaa")
        self.assertEqual(classify(self.db, [item], 1000, 120)[0]["status"], "OBSERVED")
        self.assertEqual(classify(self.db, [item], 1100, 120)[0]["status"], "OBSERVED")
        result = classify(self.db, [item], 1220, 120)[0]
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["stable_observations"], 2)

    def test_frequent_checks_do_not_postpone_ready(self):
        item = audio("id-1", "eins.wav", "aaa")
        self.assertEqual(classify(self.db, [item], 1000, 120)[0]["status"], "OBSERVED")
        self.assertEqual(classify(self.db, [item], 1060, 120)[0]["status"], "OBSERVED")
        self.assertEqual(classify(self.db, [item], 1121, 120)[0]["status"], "READY")

    def test_changed_content_resets_observation(self):
        classify(self.db, [audio("id-1", "gleich.wav", "aaa")], 1000, 10)
        result = classify(self.db, [audio("id-1", "gleich.wav", "bbb", 101)], 1020, 10)[0]
        self.assertEqual(result["status"], "OBSERVED")
        self.assertEqual(result["stable_observations"], 1)

    def test_same_hash_is_duplicate_but_same_name_different_hash_is_not(self):
        first = audio("id-1", "gleich.wav", "aaa")
        classify(self.db, [first], 1000, 10)
        classify(self.db, [first], 1011, 10)
        results = classify(self.db, [audio("id-2", "kopie.wav", "aaa"),
                                     audio("id-3", "gleich.wav", "bbb")], 1020, 10)
        by_id = {item["drive_id"]: item for item in results}
        self.assertEqual(by_id["id-2"]["status"], "DUPLICATE")
        self.assertEqual(by_id["id-2"]["duplicate_of"], "id-1")
        self.assertEqual(by_id["id-3"]["status"], "OBSERVED")

    def test_duplicate_classification_is_stable_across_repeated_scans(self):
        original = audio("id-1", "original.wav", "aaa")
        copy = audio("id-2", "kopie.wav", "aaa")
        classify(self.db, [original, copy], 1000, 10)
        first = classify(self.db, [original, copy], 1011, 10)
        second = classify(self.db, [original, copy], 1022, 10)
        self.assertEqual([item["status"] for item in first], ["READY", "DUPLICATE"])
        self.assertEqual([item["status"] for item in second], ["READY", "DUPLICATE"])

    def test_ignores_empty_and_non_audio(self):
        results = classify(self.db, [audio("zero", "leer.wav", "aaa", 0),
                                     audio("text", "notiz.md", "bbb")], 1000, 10)
        self.assertEqual([item["status"] for item in results], ["IGNORED", "IGNORED"])

    def test_staging_uses_exact_id_and_verifies_download(self):
        content = b"hello"
        digest = hashlib.sha256(content).hexdigest()
        item = audio("id-1_with-symbols", "Aufnahme #1.wav", digest, len(content))
        classify(self.db, [item], 1000, 10)
        classify(self.db, [item], 1011, 10)
        staging_dir = Path(self.temp.name) / "staging"

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(content)

        with patch("inbox_watcher.subprocess.run", side_effect=fake_run) as run:
            destination, downloaded = stage_ready_file(
                self.db, "gdrive:AudioRec Recordings", staging_dir,
                "id-1_with-symbols", 1020,
            )
        self.assertTrue(downloaded)
        self.assertEqual(destination.name, staging_name("Aufnahme #1.wav", "id-1_with-symbols"))
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(run.call_args.args[0][0:4],
                         ["rclone", "backend", "copyid", "gdrive:"])
        self.assertEqual(run.call_args.args[0][4], "id-1_with-symbols")

        destination_again, downloaded_again = stage_ready_file(
            self.db, "gdrive:AudioRec Recordings", staging_dir,
            "id-1_with-symbols", 1030,
        )
        self.assertEqual(destination_again, destination)
        self.assertFalse(downloaded_again)

    def test_staging_destinations_differ_after_matching_id_prefixes(self):
        first_content = b"first"
        second_content = b"second"
        first_id = "same-prefix-123_A"
        second_id = "same-prefix-123_B"
        items = [
            audio(first_id, "gleich.wav", hashlib.sha256(first_content).hexdigest(),
                  len(first_content)),
            audio(second_id, "gleich.wav", hashlib.sha256(second_content).hexdigest(),
                  len(second_content)),
        ]
        classify(self.db, items, 1000, 10)
        classify(self.db, items, 1011, 10)
        staging_dir = Path(self.temp.name) / "staging-distinct"
        content_by_id = {first_id: first_content, second_id: second_content}

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(content_by_id[command[4]])

        with patch("inbox_watcher.subprocess.run", side_effect=fake_run):
            first_path, _ = stage_ready_file(
                self.db, "gdrive:AudioRec Recordings", staging_dir, first_id, 1020
            )
            second_path, _ = stage_ready_file(
                self.db, "gdrive:AudioRec Recordings", staging_dir, second_id, 1020
            )

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.name, "gleich__same-prefix-123_A.wav")
        self.assertEqual(second_path.name, "gleich__same-prefix-123_B.wav")


if __name__ == "__main__":
    unittest.main()
