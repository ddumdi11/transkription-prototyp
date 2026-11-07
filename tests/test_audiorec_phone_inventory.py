import json
import sqlite3
import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from inventory_audiorec_phone import (
    build_report,
    classify_name,
    ensure_schema,
    main,
    open_inventory_state,
    scan_source,
    synchronize_inventory,
    validate_iso_datetime,
    validate_paths,
)


class AudioRecPhoneInventoryTest(unittest.TestCase):
    """Exercise source-read-only inventory behavior and persistent versioning."""

    def setUp(self):
        """Create isolated phone, runtime, and report directories."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "phone" / "recordings"
        self.source.mkdir(parents=True)
        self.state = self.root / "runtime" / "inventory.sqlite3"
        self.report = self.root / "reports" / "latest.json"

    def tearDown(self):
        """Remove the isolated test tree."""
        self.temp.cleanup()

    def write_wav(self, name: str, frames: int = 1600) -> Path:
        """Write one minimal PCM WAV fixture."""
        path = self.source / name
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(b"\0\0" * frames)
        return path

    def synchronize(
        self,
        db: sqlite3.Connection,
        items: list[dict],
        scan_id: str,
        scanned_at: str,
        ignored: int = 0,
    ) -> list[dict]:
        """Persist one fixture scan with stable epoch metadata."""
        return synchronize_inventory(
            db,
            source=self.source,
            device_id="test-phone",
            epoch_id="epoch-1",
            coverage_from="2025-12-01T00:00:00+01:00",
            reason="test",
            items=items,
            ignored_entries=ignored,
            scanned_at=scanned_at,
            scan_id=scan_id,
        )

    def test_classifies_standard_and_arbitrary_unicode_names(self):
        """Keep Unicode anomalies processable and number labels non-unique."""
        standard = classify_name("Aufnahme #1000.wav")
        klingon = classify_name("#e+ü+rt4465.wav")

        self.assertEqual(standard["name_status"], "STANDARD")
        self.assertEqual(standard["recording_number"], 1000)
        self.assertEqual(standard["trailing_number"], 1000)
        self.assertEqual(klingon["name_status"], "ANOMALOUS_PATTERN")
        self.assertIsNone(klingon["recording_number"])
        self.assertEqual(klingon["trailing_number"], 4465)

    def test_quick_scan_is_read_only_and_skips_non_wav(self):
        """A quick scan must leave every source entry byte-for-byte untouched."""
        regular = self.write_wav("Aufnahme #954.wav")
        anomalous = self.write_wav("#e+ü+rt4465.wav")
        ignored = self.source / ".nomedia"
        ignored.write_bytes(b"")
        before = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.source.iterdir()
        }

        items, ignored_count = scan_source(self.source)

        after = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.source.iterdir()
        }
        self.assertEqual(before, after)
        self.assertEqual(ignored_count, 1)
        self.assertEqual({item["original_name"] for item in items}, {
            regular.name, anomalous.name,
        })
        self.assertTrue(all(item["audio_status"] == "NOT_CHECKED" for item in items))

    def test_optional_audio_inspection_reads_header(self):
        """Optional header parsing reports the expected PCM properties."""
        self.write_wav("Aufnahme #1.wav", frames=8000)

        items, _ = scan_source(self.source, inspect_audio=True)

        self.assertEqual(items[0]["audio_status"], "VALID")
        self.assertEqual(items[0]["sample_rate"], 16_000)
        self.assertEqual(items[0]["channels"], 1)
        self.assertEqual(items[0]["sample_width_bits"], 16)
        self.assertEqual(items[0]["duration_seconds"], 0.5)

    def test_repeated_scan_reuses_version_and_marks_missing(self):
        """Unchanged files retain identity while absent files become missing."""
        first_path = self.write_wav("Aufnahme #1.wav")
        self.write_wav("Aufnahme #2.wav")
        first_items, ignored = scan_source(self.source)
        with open_inventory_state(self.state) as db:
            first = self.synchronize(
                db, first_items, "scan-1", "2026-11-07T08:00:00Z", ignored
            )
            first_id = next(
                item["inventory_id"] for item in first
                if item["original_name"] == first_path.name
            )
            first_path.unlink()
            second_items, ignored = scan_source(self.source)
            second = self.synchronize(
                db, second_items, "scan-2", "2026-11-07T09:00:00Z", ignored
            )
            rows = db.execute(
                """SELECT inventory_id,presence_status,is_current
                   FROM inventory_recordings ORDER BY relative_path"""
            ).fetchall()

        self.assertTrue(all(item["inventory_status"] == "UNCHANGED" for item in second))
        self.assertEqual(rows[0]["inventory_id"], first_id)
        self.assertEqual(rows[0]["presence_status"], "MISSING")
        self.assertEqual(rows[0]["is_current"], 1)
        self.assertEqual(rows[1]["presence_status"], "PRESENT")

    def test_changed_path_creates_new_version_and_preserves_history(self):
        """Path reuse with changed metadata creates a linked new version."""
        path = self.write_wav("Aufnahme #1.wav", frames=100)
        first_items, _ = scan_source(self.source)
        with open_inventory_state(self.state) as db:
            first = self.synchronize(
                db, first_items, "scan-1", "2026-11-07T08:00:00Z"
            )
            path.unlink()
            self.write_wav("Aufnahme #1.wav", frames=200)
            second_items, _ = scan_source(self.source)
            second = self.synchronize(
                db, second_items, "scan-2", "2026-11-07T09:00:00Z"
            )
            rows = db.execute(
                """SELECT inventory_id,presence_status,is_current,
                          replaces_inventory_id
                   FROM inventory_recordings ORDER BY first_seen_at"""
            ).fetchall()

        self.assertEqual(first[0]["inventory_status"], "NEW")
        self.assertEqual(second[0]["inventory_status"], "CHANGED")
        self.assertNotEqual(first[0]["inventory_id"], second[0]["inventory_id"])
        self.assertEqual(rows[0]["presence_status"], "SUPERSEDED")
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[1]["replaces_inventory_id"], rows[0]["inventory_id"])
        self.assertEqual(rows[1]["is_current"], 1)

    def test_epoch_and_coverage_changes_require_explicit_reset(self):
        """An implicit numbering-epoch change must fail closed."""
        self.write_wav("Aufnahme #1.wav")
        items, _ = scan_source(self.source)
        with open_inventory_state(self.state) as db:
            self.synchronize(
                db, items, "scan-1", "2026-11-07T08:00:00Z"
            )
            with self.assertRaisesRegex(ValueError, "Epochenwechsel"):
                synchronize_inventory(
                    db,
                    source=self.source,
                    device_id="test-phone",
                    epoch_id="epoch-2",
                    coverage_from="2025-12-01T00:00:00+01:00",
                    reason="reset",
                    items=items,
                    ignored_entries=0,
                    scanned_at="2026-11-07T09:00:00Z",
                    scan_id="scan-2",
                )

    def test_output_paths_inside_phone_are_rejected(self):
        """No database or report may be placed below the phone source."""
        self.write_wav("Aufnahme #1.wav")
        with self.assertRaisesRegex(ValueError, "Smartphone-Quellordner"):
            validate_paths(
                self.source,
                self.source / "inventory.sqlite3",
                self.report,
            )
        with self.assertRaisesRegex(ValueError, "Smartphone-Quellordner"):
            validate_paths(
                self.source,
                self.state,
                self.source / "report.json",
            )

    def test_check_mode_creates_no_state_or_report(self):
        """Check mode scans without creating either local artifact."""
        self.write_wav("Aufnahme #1.wav")

        result = main([
            "--source", str(self.source),
            "--state", str(self.state),
            "--report", str(self.report),
            "--device-id", "test-phone",
            "--check",
        ])

        self.assertEqual(result, 0)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.report.exists())

    def test_main_commits_database_and_atomic_report(self):
        """The normal command records state and installs a JSON report."""
        self.write_wav("Aufnahme #1.wav")
        self.write_wav("#e+ü+rt4465.wav")
        with patch(
            "inventory_audiorec_phone.utc_now",
            return_value=datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc),
        ):
            result = main([
                "--source", str(self.source),
                "--state", str(self.state),
                "--report", str(self.report),
                "--device-id", "test-phone",
            ])

        self.assertEqual(result, 0)
        self.assertTrue(self.state.is_file())
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertTrue(report["source_read_only"])
        self.assertTrue(report["committed"])
        self.assertEqual(report["summary"]["wav_files"], 2)
        self.assertEqual(report["summary"]["anomalous_names"], 1)
        with sqlite3.connect(self.state) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM inventory_recordings").fetchone()[0],
                2,
            )

    def test_validation_rejects_naive_coverage_time(self):
        """Coverage boundaries require an explicit timezone."""
        with self.assertRaisesRegex(ValueError, "Zeitzone"):
            validate_iso_datetime("2025-12-01T00:00:00", "coverage_from")

    def test_report_flags_duplicate_numbers_without_using_them_as_identity(self):
        """Repeated labels are reported but do not define inventory identity."""
        one = self.write_wav("Aufnahme #7.wav")
        items, ignored = scan_source(self.source)
        duplicate = dict(items[0])
        duplicate["relative_path"] = "copy.wav"
        duplicate["original_name"] = "copy.wav"
        duplicate["recording_number"] = 7
        duplicate["inventory_status"] = "OBSERVED_UNCOMMITTED"
        items.append(duplicate)
        items[0]["inventory_status"] = "OBSERVED_UNCOMMITTED"
        for item in (items[0], duplicate):
            item.update({
                "inventory_id": one.name,
                "hash_status": "NOT_COMPUTED",
                "sha256": None,
                "identity_status": "PROVISIONAL",
                "presence_status": "PRESENT",
            })
        report = build_report(
            source=self.source,
            state=self.state,
            device_id="test-phone",
            epoch_id="epoch-1",
            coverage_from="2025-12-01T00:00:00+01:00",
            scanned_at="2026-11-07T08:00:00Z",
            scan_id="scan-1",
            items=items,
            ignored_entries=ignored,
            committed=False,
        )
        self.assertEqual(report["summary"]["duplicate_standard_numbers"], [7])

    def test_schema_creation_is_idempotent(self):
        """Repeated schema setup within one write transaction is safe."""
        with open_inventory_state(self.state) as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            ensure_schema(db)
            db.commit()


if __name__ == "__main__":
    unittest.main()
