import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from plan_audiorec_backup import (
    build_backup_plan,
    main,
    open_readonly_state,
)


class AudioRecBackupPlanTest(unittest.TestCase):
    """Exercise conservative matching without writing either source state."""

    def setUp(self):
        """Create isolated inventory and pipeline databases."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inventory = self.root / "inventory.sqlite3"
        self.pipeline = self.root / "pipeline.sqlite3"
        self._create_inventory()
        self._create_pipeline()

    def tearDown(self):
        """Remove the isolated fixture tree."""
        self.temp.cleanup()

    def _create_inventory(self):
        """Create the minimal supported phone inventory schema."""
        with sqlite3.connect(self.inventory) as db:
            db.executescript(
                """CREATE TABLE inventory_epochs (
                       device_id TEXT, epoch_id TEXT, coverage_from TEXT,
                       is_current INTEGER
                   );
                   CREATE TABLE inventory_scans (
                       scan_id TEXT, device_id TEXT, epoch_id TEXT, scanned_at TEXT
                   );
                   CREATE TABLE inventory_recordings (
                       inventory_id TEXT, device_id TEXT, epoch_id TEXT,
                       relative_path TEXT, original_name TEXT,
                       normalized_name TEXT, recording_number INTEGER,
                       size_bytes INTEGER, modified_ns INTEGER, modified_at TEXT,
                       sha256 TEXT, hash_status TEXT, identity_status TEXT,
                       presence_status TEXT, is_current INTEGER,
                       last_scan_id TEXT
                   );"""
            )
            db.execute(
                "INSERT INTO inventory_epochs VALUES (?,?,?,1)",
                ("phone", "epoch-1", "2025-12-01T00:00:00+01:00"),
            )
            db.execute(
                "INSERT INTO inventory_scans VALUES (?,?,?,?)",
                ("scan-1", "phone", "epoch-1", "2026-11-07T10:00:00Z"),
            )

    def _create_pipeline(self):
        """Create the minimal supported transcription pipeline schema."""
        with sqlite3.connect(self.pipeline) as db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS files (
                       drive_id TEXT PRIMARY KEY, path TEXT, size INTEGER,
                       content_hash TEXT, hash_type TEXT, mod_time TEXT
                   );
                   CREATE TABLE IF NOT EXISTS transcription_jobs (
                       drive_id TEXT PRIMARY KEY, status TEXT, transcript_path TEXT
                   );
                   CREATE TABLE IF NOT EXISTS published_transcripts (
                       drive_id TEXT PRIMARY KEY, remote_id TEXT, remote_path TEXT
                   );"""
            )

    def add_phone(
        self,
        number: int,
        *,
        size: int = 100,
        sha256: str | None = None,
        hash_status: str = "NOT_COMPUTED",
        present: bool = True,
        current: bool = True,
    ) -> None:
        """Add one current-style phone recording fixture."""
        name = f"Aufnahme #{number}.wav"
        with sqlite3.connect(self.inventory) as db:
            db.execute(
                """INSERT INTO inventory_recordings VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"inv-{number}", "phone", "epoch-1", name, name, name,
                    number, size, number, "2026-11-07T09:00:00Z", sha256,
                    hash_status, "PROVISIONAL",
                    "PRESENT" if present else "MISSING", int(current), "scan-1",
                ),
            )

    def add_pipeline(
        self,
        number: int,
        drive_id: str,
        digest: str,
        *,
        size: int = 100,
        hash_type: str = "sha256",
        published: bool = True,
        path: str | None = None,
    ) -> None:
        """Add one pipeline candidate and optional publication evidence."""
        name = path or f"Aufnahme #{number}.wav"
        with sqlite3.connect(self.pipeline) as db:
            db.execute(
                "INSERT INTO files VALUES (?,?,?,?,?,?)",
                (drive_id, name, size, digest, hash_type, "2026-11-07T10:00:00Z"),
            )
            db.execute(
                "INSERT INTO transcription_jobs VALUES (?,?,?)",
                (drive_id, "DONE", f"staging/{drive_id}.md"),
            )
            if published:
                db.execute(
                    "INSERT INTO published_transcripts VALUES (?,?,?)",
                    (drive_id, f"remote-{drive_id}", f"{drive_id}.md"),
                )

    def plan(self, selected_numbers=None):
        """Build a fixture plan through read-only connections."""
        with (
            open_readonly_state(self.inventory, "inventory") as inventory_db,
            open_readonly_state(self.pipeline, "pipeline") as pipeline_db,
        ):
            return build_backup_plan(
                inventory_db,
                pipeline_db,
                inventory_state=self.inventory,
                pipeline_state=self.pipeline,
                device_id="phone",
                selected_numbers=selected_numbers,
            )

    def test_classifies_every_evidence_state_without_proving_backup(self):
        """Hashes refine candidates but never imply live remote presence."""
        matching = "a" * 64
        self.add_phone(1)
        self.add_pipeline(1, "id-1a", matching)
        self.add_pipeline(1, "id-1b", matching)
        self.add_phone(2)
        self.add_phone(3, sha256=matching, hash_status="VERIFIED")
        self.add_pipeline(3, "id-3", matching)
        self.add_phone(4, sha256="b" * 64, hash_status="VERIFIED")
        self.add_pipeline(4, "id-4", matching)
        self.add_phone(5)
        self.add_pipeline(5, "id-5a", "c" * 64)
        self.add_pipeline(5, "id-5b", "d" * 64)
        self.add_phone(6)
        self.add_pipeline(6, "id-6", "e" * 32, hash_type="md5")

        plan = self.plan()
        items = {item["recording_number"]: item for item in plan["items"]}

        self.assertEqual(items[1]["match_status"], "PHONE_HASH_REQUIRED")
        self.assertEqual(items[1]["pipeline_candidate_count"], 2)
        self.assertEqual(len(items[1]["pipeline_content_hashes"]), 1)
        self.assertEqual(items[2]["match_status"], "NO_PIPELINE_MATCH")
        self.assertEqual(items[3]["match_status"], "CONTENT_HASH_MATCH")
        self.assertEqual(items[4]["match_status"], "CONTENT_HASH_MISMATCH")
        self.assertEqual(items[5]["match_status"], "MULTIPLE_PIPELINE_HASHES")
        self.assertEqual(items[6]["match_status"], "UNSUPPORTED_PIPELINE_HASH")
        self.assertTrue(all(item["backup_status"] == "NOT_PROVEN" for item in items.values()))
        self.assertEqual(plan["summary"]["backup_proven"], 0)
        self.assertFalse(plan["remote_verified"])

    def test_exact_normalized_path_and_size_are_required(self):
        """Loose basename or size matches cannot become pipeline candidates."""
        self.add_phone(1, size=100)
        self.add_phone(2, size=200)
        self.add_pipeline(1, "wrong-size", "a" * 64, size=101)
        self.add_pipeline(
            2, "wrong-path", "b" * 64, size=200,
            path="Unterordner/Aufnahme #2.wav",
        )

        plan = self.plan()

        self.assertEqual(
            [item["match_status"] for item in plan["items"]],
            ["NO_PIPELINE_MATCH", "NO_PIPELINE_MATCH"],
        )

    def test_selection_rejects_unknown_numbers_and_filters_known_rows(self):
        """Explicit recording selection fails closed for unknown labels."""
        self.add_phone(1)
        self.add_phone(2)

        selected = self.plan({2})

        self.assertEqual(
            [item["recording_number"] for item in selected["items"]], [2]
        )
        with self.assertRaisesRegex(ValueError, "Unbekannte"):
            self.plan({3})

    def test_missing_and_historical_phone_rows_are_excluded(self):
        """Only current rows still present on the phone enter the plan."""
        self.add_phone(1)
        self.add_phone(2, present=False)
        self.add_phone(3, current=False)

        plan = self.plan()

        self.assertEqual(
            [item["recording_number"] for item in plan["items"]], [1]
        )

    def test_invalid_verified_phone_hash_fails_closed(self):
        """A claimed verified hash must contain exactly one SHA-256 value."""
        self.add_phone(1, sha256="not-a-hash", hash_status="VERIFIED")
        self.add_pipeline(1, "id-1", "a" * 64)

        with self.assertRaisesRegex(ValueError, "Ungültiger"):
            self.plan()

    def test_plan_is_deterministic_and_reports_latest_snapshot(self):
        """Identical database snapshots produce byte-equivalent plan data."""
        self.add_phone(1)

        first = self.plan()
        second = self.plan()

        self.assertEqual(first, second)
        self.assertEqual(
            first["sources"]["latest_inventory_scan"],
            {"scan_id": "scan-1", "scanned_at": "2026-11-07T10:00:00Z"},
        )

    def test_cli_keeps_both_database_files_unchanged(self):
        """The command reads existing states without changing their bytes."""
        self.add_phone(1)
        self.add_pipeline(1, "id-1", "a" * 64)
        before = {
            self.inventory: hashlib.sha256(self.inventory.read_bytes()).hexdigest(),
            self.pipeline: hashlib.sha256(self.pipeline.read_bytes()).hexdigest(),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main([
                "--inventory-state", str(self.inventory),
                "--pipeline-state", str(self.pipeline),
                "--device-id", "phone",
                "--json",
            ])

        self.assertEqual(result, 0)
        self.assertTrue(json.loads(stdout.getvalue())["read_only"])
        self.assertIn("rein lesend", stderr.getvalue())
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_incomplete_schema_and_wrong_epoch_are_rejected(self):
        """Missing evidence tables and stale epoch selections fail closed."""
        with sqlite3.connect(self.pipeline) as db:
            db.execute("DROP TABLE published_transcripts")
        with self.assertRaisesRegex(ValueError, "unvollständig"):
            self.plan()

        self._create_pipeline()
        self.add_phone(1)
        with (
            open_readonly_state(self.inventory, "inventory") as inventory_db,
            open_readonly_state(self.pipeline, "pipeline") as pipeline_db,
        ):
            with self.assertRaisesRegex(ValueError, "Aktuelle Epoche"):
                build_backup_plan(
                    inventory_db,
                    pipeline_db,
                    inventory_state=self.inventory,
                    pipeline_state=self.pipeline,
                    device_id="phone",
                    epoch_id="epoch-2",
                )


if __name__ == "__main__":
    unittest.main()
