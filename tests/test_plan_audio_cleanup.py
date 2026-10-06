import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from plan_audio_cleanup import (
    parse_utc_timestamp,
    plan_cleanup_one,
    source_remote_index,
    summarize,
    resolve_retention_days,
    validate_retention_days,
    verify_receipt_files,
)


class AudioCleanupPlanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio = self.root / "Aufnahme #1__drive-id.wav"
        self.audio.write_bytes(b"original wav")
        digest = hashlib.sha256(self.audio.read_bytes()).hexdigest()
        self.item = {
            "archive_status": "CANDIDATE",
            "archive_blockers": [],
            "drive_id": "drive-id",
            "source_path": "Aufnahme #1.wav",
            "local_audio": str(self.audio),
            "source_size": self.audio.stat().st_size,
            "source_hash_type": "sha256",
            "source_hash": digest,
            "archive_name": "Aufnahme #1__drive-id.flac",
            "quality": {"status": "CLEAR"},
        }
        self.package = self.root / "Aufnahme #1__drive-id"
        self.package.mkdir()
        self.upload_plan = {
            "archive_key": "archive-key",
            "drive_id": "drive-id",
            "target": "gdrive,root_folder_id=archive-target-id:",
            "target_root_id": "archive-target-id",
            "package_name": self.package.name,
            "package_dir": str(self.package),
            "receipt_file": str(self.package / "upload-receipt.json"),
            "files": {
                "Aufnahme #1__drive-id.flac": {
                    "size": 10,
                    "sha256": "a" * 64,
                },
                "archive.json": {
                    "size": 20,
                    "sha256": "b" * 64,
                },
            },
        }
        self.receipt_files = [
            {
                "remote_name": name,
                "remote_id": f"remote-{index}",
                "size": details["size"],
                "sha256": details["sha256"],
            }
            for index, (name, details) in enumerate(
                sorted(self.upload_plan["files"].items())
            )
        ]
        self.receipt = {
            "schema_version": 1,
            "receipt_type": "verified_drive_flac_archive",
            "archive_key": "archive-key",
            "source_drive_id": "drive-id",
            "verified_at": "2026-10-01T10:00:00+00:00",
            "target_root_id": "archive-target-id",
            "remote_package": self.package.name,
            "files": self.receipt_files,
            "source_preserved": True,
            "cleanup_ready": False,
        }
        (self.package / "upload-receipt.json").write_text(
            json.dumps(self.receipt), encoding="utf-8"
        )
        self.as_of = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
        self.source_row = {
            "ID": "drive-id",
            "Size": self.item["source_size"],
            "Hashes": {"sha256": digest},
        }

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, *, verify_remote=False, local_days=None, remote_days=None):
        with (
            patch("plan_audio_cleanup.existing_package_matches", return_value=True),
            patch("plan_audio_cleanup.build_upload_plan", return_value=self.upload_plan),
            patch(
                "plan_audio_cleanup.inspect_remote_package",
                return_value={"placeholder": {}},
            ),
            patch(
                "plan_audio_cleanup.verify_remote_package",
                return_value=self.receipt_files,
            ),
        ):
            return plan_cleanup_one(
                self.item,
                self.root,
                "gdrive,root_folder_id=archive-target-id:",
                {"drive-id": self.source_row} if verify_remote else None,
                verify_remote,
                local_days,
                remote_days,
                self.as_of,
            )

    def test_policy_is_held_without_retention_and_live_verification(self):
        item = self.plan()
        self.assertEqual(item["local_policy_status"], "HOLD")
        self.assertEqual(item["remote_policy_status"], "HOLD")
        self.assertIn("archive_remote_not_reverified", item["local_blockers"])
        self.assertIn("local_retention_unconfigured", item["local_blockers"])
        self.assertIn("source_remote_not_reverified", item["remote_blockers"])
        self.assertFalse(item["cleanup_ready"])
        self.assertIsNone(item["action"])

    def test_elapsed_policies_and_live_evidence_can_be_eligible_only(self):
        item = self.plan(verify_remote=True, local_days=7, remote_days=10)
        self.assertEqual(item["local_policy_status"], "ELIGIBLE")
        self.assertEqual(item["remote_policy_status"], "ELIGIBLE")
        self.assertTrue(all(item["evidence"].values()))
        self.assertEqual(item["archive_age_days"], 10.0)
        self.assertFalse(item["cleanup_ready"])
        self.assertEqual(
            summarize([item]),
            {
                "planned_items": 1,
                "local_policy_eligible": 1,
                "remote_policy_eligible": 1,
                "cleanup_ready": 0,
            },
        )

    def test_quality_issue_and_unelapsed_retention_remain_blocked(self):
        self.item["quality"] = {"status": "PENDING"}
        item = self.plan(verify_remote=True, local_days=11, remote_days=30)
        self.assertIn("quality_pending", item["local_blockers"])
        self.assertIn("local_retention_not_elapsed", item["local_blockers"])
        self.assertIn("remote_retention_not_elapsed", item["remote_blockers"])

    def test_missing_local_wav_does_not_block_later_remote_policy(self):
        self.audio.unlink()
        self.item["archive_status"] = "HOLD"
        self.item["archive_blockers"] = ["local_audio_missing"]
        item = self.plan(verify_remote=True, local_days=0, remote_days=0)
        self.assertEqual(item["local_policy_status"], "HOLD")
        self.assertIn("archive_local_audio_missing", item["local_blockers"])
        self.assertIn("local_source_mismatch", item["local_blockers"])
        self.assertEqual(item["remote_policy_status"], "ELIGIBLE")
        self.assertEqual(item["remote_blockers"], [])

    def test_receipt_requires_canonical_complete_file_evidence(self):
        verified_at, files = verify_receipt_files(self.receipt, self.upload_plan)
        self.assertEqual(verified_at, datetime(2026, 10, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(files, self.receipt_files)

        malformed = dict(self.receipt)
        malformed["files"] = [{**self.receipt_files[0], "extra": True}]
        with self.assertRaises(ValueError):
            verify_receipt_files(malformed, self.upload_plan)

    def test_time_and_retention_validation(self):
        self.assertEqual(
            parse_utc_timestamp("2026-10-01T10:00:00Z", "test"),
            datetime(2026, 10, 1, 10, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ValueError, "UTC"):
            parse_utc_timestamp("2026-10-01T10:00:00+02:00", "test")
        self.assertEqual(validate_retention_days(0, "test"), 0)
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_retention_days(value, "test")

    def test_retention_uses_cli_override_then_optional_environment(self):
        with patch.dict(os.environ, {"TEST_RETENTION": "7"}):
            self.assertEqual(
                resolve_retention_days(None, "TEST_RETENTION", "Testfrist"), 7
            )
            self.assertEqual(
                resolve_retention_days(3, "TEST_RETENTION", "Testfrist"), 3
            )
        with patch.dict(os.environ, {"TEST_RETENTION": "kein-tag"}):
            with self.assertRaisesRegex(ValueError, "keine Zahl"):
                resolve_retention_days(None, "TEST_RETENTION", "Testfrist")

    def test_source_listing_rejects_duplicate_drive_ids(self):
        result = subprocess.CompletedProcess(
            [], 0,
            json.dumps([
                {"ID": "same", "Path": "one.wav"},
                {"ID": "same", "Path": "two.wav"},
            ]),
            "",
        )
        with (
            patch("plan_audio_cleanup.run_rclone", return_value=result),
            self.assertRaisesRegex(ValueError, "Doppelte Drive-ID"),
        ):
            source_remote_index("gdrive,root_folder_id=source-id:")


if __name__ == "__main__":
    unittest.main()
