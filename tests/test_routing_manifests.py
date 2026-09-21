import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from plan_session_routing import (
    confirm_selected_sessions,
    existing_manifest_matches,
    routing_plan_hash,
    write_confirmed_manifest,
)


class RoutingManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "manifests"
        self.confirmed_at = datetime(2026, 9, 20, 18, 30, tzinfo=timezone.utc)
        self.session = {
            "session_id": "session-20260919-1124-1234abcd",
            "start": "2026-09-19T11:22:14+02:00",
            "end": "2026-09-19T14:56:42+02:00",
            "recording_count": 1,
            "segment_count": 1,
            "projects": [{
                "name": "Projekt",
                "whole_session": False,
                "scopes": ["content"],
                "segments": [{"segment_id": "segment-000001", "text": "Test"}],
            }],
            "topics": [],
            "unassigned_segments": [],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_writes_atomic_manifest_and_repeats_idempotently(self):
        path, created = write_confirmed_manifest(
            self.session, self.output, self.confirmed_at
        )
        self.assertTrue(created)
        self.assertTrue(path.is_file())
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["confirmation"], "explicit_cli")
        self.assertEqual(payload["session"], self.session)
        self.assertEqual(
            payload["routing_plan_sha256"], routing_plan_hash(self.session)
        )
        original = path.read_bytes()

        repeated_path, created = write_confirmed_manifest(
            self.session,
            self.output,
            datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(created)
        self.assertEqual(repeated_path, path)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(existing_manifest_matches(path, self.session))

    def test_rejects_changed_plan_under_same_session_id(self):
        path, _created = write_confirmed_manifest(
            self.session, self.output, self.confirmed_at
        )
        original = path.read_bytes()
        changed = deepcopy(self.session)
        changed["projects"][0]["name"] = "Anderes Projekt"

        with self.assertRaisesRegex(FileExistsError, "stimmt aber nicht"):
            write_confirmed_manifest(changed, self.output, self.confirmed_at)

        self.assertEqual(path.read_bytes(), original)

    def test_tampered_confirmation_metadata_is_not_identical(self):
        path, _created = write_confirmed_manifest(
            self.session, self.output, self.confirmed_at
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["confirmed_at"] = "not-a-time"
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertFalse(existing_manifest_matches(path, self.session))
        with self.assertRaises(FileExistsError):
            write_confirmed_manifest(self.session, self.output, self.confirmed_at)

    def test_unknown_or_duplicate_selection_writes_nothing(self):
        with self.assertRaisesRegex(ValueError, "Unbekannte Sitzungs-ID"):
            confirm_selected_sessions(
                [self.session],
                [self.session["session_id"], "session-20260920-1200-deadbeef"],
                self.output,
                self.confirmed_at,
            )
        self.assertFalse(self.output.exists())

        with self.assertRaisesRegex(ValueError, "mehrfach"):
            confirm_selected_sessions(
                [self.session],
                [self.session["session_id"], self.session["session_id"]],
                self.output,
                self.confirmed_at,
            )
        self.assertFalse(self.output.exists())

    def test_multi_manifest_failure_rolls_back_new_files(self):
        other = deepcopy(self.session)
        other["session_id"] = "session-20260919-1800-deadbeef"
        self.output.mkdir()
        conflicting_path = self.output / f"routing__{other['session_id']}.json"
        conflicting_path.write_text("{}", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            confirm_selected_sessions(
                [self.session, other],
                [self.session["session_id"], other["session_id"]],
                self.output,
                self.confirmed_at,
            )

        first_path = self.output / f"routing__{self.session['session_id']}.json"
        self.assertFalse(first_path.exists())
        self.assertTrue(conflicting_path.exists())
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

    def test_install_failure_removes_temporary_file(self):
        with patch.object(Path, "replace", side_effect=OSError("install failed")):
            with self.assertRaisesRegex(OSError, "install failed"):
                write_confirmed_manifest(
                    self.session, self.output, self.confirmed_at
                )

        target = self.output / f"routing__{self.session['session_id']}.json"
        self.assertFalse(target.exists())
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

    def test_concurrent_divergent_confirmation_cannot_replace_winner(self):
        changed = deepcopy(self.session)
        changed["projects"][0]["name"] = "Anderes Projekt"
        barrier = threading.Barrier(2)

        def attempt(session):
            barrier.wait()
            try:
                return ("written", write_confirmed_manifest(
                    session, self.output, self.confirmed_at
                ))
            except Exception as exc:
                return ("error", exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [self.session, changed]))

        written = [result for status, result in results if status == "written"]
        errors = [result for status, result in results if status == "error"]
        self.assertEqual(len(written), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], FileExistsError)
        winner_path, created = written[0]
        self.assertTrue(created)
        winner = json.loads(winner_path.read_text(encoding="utf-8"))["session"]
        self.assertIn(winner, [self.session, changed])
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

    def test_fsync_order_is_file_replace_directory(self):
        events = []
        original_replace = Path.replace

        def track_fsync(_descriptor):
            events.append("fsync")

        def track_replace(source, target):
            events.append("replace")
            return original_replace(source, target)

        with (
            patch("plan_session_routing.os.fsync", side_effect=track_fsync),
            patch.object(Path, "replace", autospec=True, side_effect=track_replace),
        ):
            path, created = write_confirmed_manifest(
                self.session, self.output, self.confirmed_at
            )

        self.assertTrue(created)
        self.assertTrue(path.exists())
        self.assertEqual(events, ["fsync", "replace", "fsync"])

    def test_directory_fsync_failure_is_reported_and_rolls_back_target(self):
        calls = 0

        def fail_directory_fsync(_descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("directory fsync failed")

        with patch(
            "plan_session_routing.os.fsync", side_effect=fail_directory_fsync
        ):
            with self.assertRaisesRegex(OSError, "directory fsync failed"):
                write_confirmed_manifest(
                    self.session, self.output, self.confirmed_at
                )

        target = self.output / f"routing__{self.session['session_id']}.json"
        self.assertFalse(target.exists())
        self.assertEqual(list(self.output.glob(".*.tmp")), [])
        self.assertEqual(calls, 3)

    def test_dry_selection_does_not_create_output_directory(self):
        result = confirm_selected_sessions(
            [self.session], [], self.output, self.confirmed_at
        )
        self.assertEqual(result, [])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
