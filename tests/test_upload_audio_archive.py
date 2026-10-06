import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from upload_audio_archive import (
    build_upload_plan,
    inspect_remote_package,
    portable_remote_name,
    transfer_timeout,
    upload_one,
    validate_remote_root,
    verify_remote_package,
)


class UploadAudioArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "Aufnahme #780__drive-id"
        self.package.mkdir()
        self.archive = self.package / "Aufnahme #780__drive-id.flac"
        self.archive.write_bytes(b"verified flac bytes")
        self.metadata = self.package / "archive.json"
        self.metadata.write_text(json.dumps({
            "schema_version": 1,
            "archive_type": "local_lossless_flac",
            "archive_key": "archive-key",
            "source": {
                "drive_id": "drive-id",
                "size": 123,
                "hash_type": "sha256",
                "content_hash": "a" * 64,
            },
        }), encoding="utf-8")
        conversion_plan = {
            "archive_key": "archive-key",
            "drive_id": "drive-id",
            "package_dir": str(self.package),
            "archive_file": str(self.archive),
            "metadata_file": str(self.metadata),
            "source_size": 123,
            "source_hash_type": "sha256",
            "source_hash": "a" * 64,
        }
        self.target = "gdrive,root_folder_id=archive-folder-id:"
        with patch(
            "upload_audio_archive.existing_package_matches", return_value=True
        ):
            self.plan = build_upload_plan(conversion_plan, self.target)

    def tearDown(self):
        self.temp.cleanup()

    def remote_rows(self):
        return {
            name: {
                "Path": name,
                "Size": details["size"],
                "Hashes": {"sha256": details["sha256"]},
                "ID": f"remote-{index}",
            }
            for index, (name, details) in enumerate(self.plan["files"].items())
        }

    def test_requires_id_pinned_root_and_portable_names(self):
        self.assertEqual(
            validate_remote_root(self.target),
            (self.target, "archive-folder-id"),
        )
        for invalid in (None, "", "gdrive:Audio Archive", "gdrive:"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_remote_root(invalid)
        self.assertEqual(portable_remote_name("a:b?.flac"), "a_b_.flac")
        self.assertEqual(portable_remote_name("CON"), "_CON")
        self.assertGreater(transfer_timeout(1024 * 1024 * 1024), 3600)

    def test_upload_is_verified_idempotent_and_writes_receipt(self):
        remote = self.remote_rows()
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch(
                "upload_audio_archive.existing_package_matches", return_value=True
            ),
            patch(
                "upload_audio_archive.inspect_remote_package",
                side_effect=[None, {}, remote],
            ),
            patch(
                "upload_audio_archive.run_rclone", return_value=completed
            ) as run,
        ):
            receipt, uploaded, created = upload_one(self.plan)

        self.assertTrue(created)
        self.assertEqual(set(uploaded), set(self.plan["files"]))
        self.assertTrue(receipt.is_file())
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["target_root_id"], "archive-folder-id")
        self.assertEqual(len(payload["files"]), 2)
        self.assertFalse(payload["cleanup_ready"])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:2], ["rclone", "mkdir"])
        self.assertEqual(
            sum(command[1] == "copyto" for command in commands), 2
        )
        upload_sources = [Path(command[2]) for command in commands if command[1] == "copyto"]
        self.assertTrue(all(".archive-upload." in source.parent.name for source in upload_sources))
        self.assertTrue(all(not source.exists() for source in upload_sources))

        with (
            patch(
                "upload_audio_archive.existing_package_matches", return_value=True
            ),
            patch(
                "upload_audio_archive.inspect_remote_package", return_value=remote
            ),
            patch("upload_audio_archive.run_rclone") as repeated_run,
        ):
            repeated_receipt, repeated_uploads, repeated_created = upload_one(self.plan)
        self.assertEqual(repeated_receipt, receipt)
        self.assertEqual(repeated_uploads, [])
        self.assertFalse(repeated_created)
        repeated_run.assert_not_called()

    def test_partial_upload_resumes_but_mismatch_never_uploads(self):
        remote = self.remote_rows()
        first_name = sorted(remote)[0]
        partial = {first_name: remote[first_name]}
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch(
                "upload_audio_archive.existing_package_matches", return_value=True
            ),
            patch(
                "upload_audio_archive.inspect_remote_package",
                side_effect=[partial, remote],
            ),
            patch(
                "upload_audio_archive.run_rclone", return_value=completed
            ) as run,
        ):
            _receipt, uploaded, _created = upload_one(self.plan)
        self.assertEqual(uploaded, sorted(set(remote) - {first_name}))
        self.assertEqual(run.call_count, 1)

        (self.package / "upload-receipt.json").unlink()
        mismatched = dict(remote)
        bad_name = sorted(mismatched)[0]
        mismatched[bad_name] = {**mismatched[bad_name], "Size": 999}
        with (
            patch(
                "upload_audio_archive.existing_package_matches", return_value=True
            ),
            patch(
                "upload_audio_archive.inspect_remote_package",
                return_value=mismatched,
            ),
            patch("upload_audio_archive.run_rclone") as run,
            self.assertRaisesRegex(ValueError, "Remote-Größe"),
        ):
            upload_one(self.plan)
        run.assert_not_called()

    def test_changed_local_bytes_are_rejected_before_remote_access(self):
        self.archive.write_bytes(b"changed after dry run")
        with (
            patch(
                "upload_audio_archive.existing_package_matches", return_value=True
            ),
            patch("upload_audio_archive.inspect_remote_package") as inspect,
            self.assertRaisesRegex(ValueError, "Upload-(Größe|Hash)"),
        ):
            upload_one(self.plan)
        inspect.assert_not_called()
        self.assertEqual(list(self.root.glob(".archive-upload.*.tmp")), [])

    def test_remote_verification_rejects_missing_and_extra_files(self):
        remote = self.remote_rows()
        expected = self.plan["files"]
        missing = dict(remote)
        missing.pop(next(iter(missing)))
        with self.assertRaisesRegex(ValueError, "fehlen"):
            verify_remote_package(expected, missing)
        with self.assertRaisesRegex(ValueError, "Unerwartete"):
            verify_remote_package(expected, {**remote, "extra.txt": {}})

    def test_remote_inspection_rejects_duplicate_package_directories(self):
        directories = json.dumps([
            {"Path": self.plan["package_name"], "ID": "one"},
            {"Path": self.plan["package_name"], "ID": "two"},
        ])
        result = subprocess.CompletedProcess([], 0, directories, "")
        with (
            patch("upload_audio_archive.run_rclone", return_value=result),
            self.assertRaisesRegex(ValueError, "Mehrdeutiger"),
        ):
            inspect_remote_package(self.target, self.plan["package_name"])


if __name__ == "__main__":
    unittest.main()
