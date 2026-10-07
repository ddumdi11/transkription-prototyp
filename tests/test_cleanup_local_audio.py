import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cleanup_local_audio import (
    RECEIPT_NAME,
    pending_path_for,
    read_cleanup_receipt,
    remove_local_wav,
    main,
)


class LocalAudioCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "Aufnahme #1__drive-id.wav"
        self.source.write_bytes(b"original wav bytes")
        self.package = self.root / "audio-archive" / "Aufnahme #1__drive-id"
        self.package.mkdir(parents=True)
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.plan = {
            "archive_key": "drive-id:sha256:" + digest,
            "drive_id": "drive-id",
            "source_path": "Aufnahme #1.wav",
            "local_audio": str(self.source),
            "source_size": self.source.stat().st_size,
            "source_hash_type": "sha256",
            "source_hash": digest,
            "archive_package": str(self.package),
            "archive_verified_at": "2026-10-01T10:00:00+00:00",
            "quality_status": "CLEAR",
            "local_policy_status": "ELIGIBLE",
            "local_blockers": [],
            "evidence": {
                "local_source_verified": True,
                "local_archive_verified": True,
                "upload_receipt_verified": True,
                "archive_remote_reverified": True,
                "source_remote_reverified": True,
            },
        }
        self.now = datetime(2026, 10, 11, 10, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_removal_is_durable_audited_and_idempotent(self):
        receipt_path, removed = remove_local_wav(
            self.plan, 7, 30, now=self.now
        )
        self.assertTrue(removed)
        self.assertFalse(self.source.exists())
        self.assertFalse(pending_path_for(self.source).exists())
        payload = read_cleanup_receipt(receipt_path)
        self.assertEqual(payload["status"], "REMOVED")
        self.assertEqual(payload["source_drive_id"], "drive-id")
        self.assertEqual(payload["policy"]["local_retention_days"], 7)

        repeated_plan = dict(self.plan)
        repeated_plan["local_policy_status"] = "HOLD"
        repeated_plan["local_blockers"] = [
            "archive_local_audio_missing", "local_source_mismatch"
        ]
        repeated_plan["evidence"] = {
            **self.plan["evidence"], "local_source_verified": False
        }
        repeated_receipt, repeated_removed = remove_local_wav(
            repeated_plan, 7, 30, now=self.now
        )
        self.assertEqual(repeated_receipt, receipt_path)
        self.assertFalse(repeated_removed)

    def test_ineligible_new_cleanup_does_not_write_or_move(self):
        self.plan["local_policy_status"] = "HOLD"
        self.plan["local_blockers"] = ["local_retention_not_elapsed"]
        with self.assertRaisesRegex(ValueError, "nicht löschbar"):
            remove_local_wav(self.plan, 7, 30, now=self.now)
        self.assertTrue(self.source.exists())
        self.assertFalse((self.package / RECEIPT_NAME).exists())

    def test_confirmed_cleanup_rejects_retention_below_seven_before_planning(self):
        with patch("cleanup_local_audio.build_local_cleanup_plan") as build:
            result = main([
                "--drive-id", "drive-id",
                "--local-retention-days", "6.999",
                "--confirm-local-cleanup",
            ])
        self.assertEqual(result, 1)
        build.assert_not_called()

    def test_prepared_operation_resumes_from_verified_pending_file(self):
        receipt_path, _removed = remove_local_wav(
            self.plan, 7, 30, now=self.now
        )
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        payload["status"] = "PREPARED"
        payload["removed_at"] = None
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        pending = pending_path_for(self.source)
        pending.write_bytes(b"original wav bytes")

        resumed_plan = dict(self.plan)
        resumed_plan["local_policy_status"] = "HOLD"
        resumed_plan["local_blockers"] = [
            "archive_local_audio_missing", "local_source_mismatch"
        ]
        resumed_plan["evidence"] = {
            **self.plan["evidence"], "local_source_verified": False
        }
        resumed_receipt, resumed = remove_local_wav(
            resumed_plan, 7, 30, now=self.now
        )
        self.assertTrue(resumed)
        self.assertFalse(pending.exists())
        self.assertEqual(
            read_cleanup_receipt(resumed_receipt)["status"], "REMOVED"
        )

    def test_prepared_operation_requires_fresh_remote_evidence(self):
        binding = {
            "schema_version": 1,
            "receipt_type": "verified_local_wav_cleanup",
            "status": "PREPARED",
            "confirmation": "explicit_cli",
            "archive_key": self.plan["archive_key"],
            "source_drive_id": self.plan["drive_id"],
            "source": {
                "local_file": self.plan["local_audio"],
                "size": self.plan["source_size"],
                "hash_type": self.plan["source_hash_type"],
                "content_hash": self.plan["source_hash"],
            },
            "archive_package": self.plan["archive_package"],
        }
        (self.package / RECEIPT_NAME).write_text(
            json.dumps(binding), encoding="utf-8"
        )
        self.plan["evidence"]["archive_remote_reverified"] = False
        with self.assertRaisesRegex(ValueError, "Live-Nachweise"):
            remove_local_wav(self.plan, 7, 30, now=self.now)
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
