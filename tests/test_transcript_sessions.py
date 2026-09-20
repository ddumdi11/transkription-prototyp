import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from inbox_watcher import open_state
from plan_transcript_sessions import (
    group_recordings,
    open_readonly_state,
    parse_mod_time,
    plan_sessions,
    serializable_session,
)
from publish_transcripts import prepare_publish_state
from segment_metadata import SegmentMetadataError


class TranscriptSessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = open_state(self.root / "state.sqlite3")
        prepare_publish_state(self.db)
        self.timezone = ZoneInfo("Europe/Berlin")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_recording(
        self,
        drive_id: str,
        number: int,
        mod_time: str,
        segment_end: float,
        *,
        published: bool = True,
        valid_sidecar: bool = True,
    ) -> None:
        transcript = self.root / f"Aufnahme #{number}__{drive_id}.md"
        transcript.write_text("# Test\n\nText\n", encoding="utf-8")
        segment_path = transcript.with_suffix(".segments.json")
        payload = {
            "schema_version": 1,
            "source_id": drive_id,
            "segments": [{
                "id": "segment-000001",
                "start": 0.0,
                "end": segment_end,
                "raw_text": " Text ",
                "text": "Text",
            }],
        }
        if valid_sidecar:
            segment_path.write_text(json.dumps(payload), encoding="utf-8")

        self.db.execute(
            """INSERT INTO files
               (drive_id,path,size,content_hash,hash_type,mod_time,first_seen,last_seen,
                stable_observations,status,duplicate_of)
               VALUES (?, ?, 100, ?, 'sha256', ?, 1000, 1010, 2, 'READY', NULL)""",
            (drive_id, f"Aufnahme #{number}.wav", f"hash-{drive_id}", mod_time),
        )
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES (?, 'DONE', 1, 'audio.wav', ?, NULL, 1020)""",
            (drive_id, str(transcript)),
        )
        if published:
            self.db.execute(
                """INSERT INTO published_transcripts
                   (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
                   VALUES (?, ?, 'transcript-hash', 10, ?, ?, 1030)""",
                (drive_id, str(transcript), transcript.name, f"remote-{drive_id}"),
            )
        self.db.commit()

    @staticmethod
    def recording(drive_id: str, start: datetime, duration: int = 60):
        return {
            "drive_id": drive_id,
            "audio_path": f"{drive_id}.wav",
            "recording_start": start,
            "recording_end": start + timedelta(seconds=duration),
        }

    def test_parse_mod_time_requires_timezone(self):
        parsed = parse_mod_time("2026-09-19T09:24:26.811Z", "id-1")
        self.assertEqual(parsed.tzinfo, timezone.utc)
        with self.assertRaisesRegex(ValueError, "keine Zeitzone"):
            parse_mod_time("2026-09-19T09:24:26", "id-1")

    def test_readonly_state_rejects_persistent_writes(self):
        self.db.commit()
        with open_readonly_state(self.root / "state.sqlite3") as readonly:
            self.assertIs(readonly.row_factory, sqlite3.Row)
            self.assertEqual(readonly.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                readonly.execute(
                    "INSERT INTO pipeline_settings (key, value) VALUES ('test', 'test')"
                )
            readonly.commit()
        self.assertIsNone(
            self.db.execute(
                "SELECT value FROM pipeline_settings WHERE key = 'test'"
            ).fetchone()
        )

    def test_groups_by_actual_quiet_gap(self):
        base = datetime(2026, 9, 19, 10, tzinfo=self.timezone)
        recordings = [
            self.recording("one", base, 600),
            self.recording("two", base + timedelta(minutes=20), 300),
            self.recording("three", base + timedelta(hours=2), 60),
        ]
        sessions = group_recordings(recordings, max_gap_seconds=60 * 60)
        self.assertEqual([item["recording_count"] for item in sessions], [2, 1])
        self.assertEqual(
            sessions[0]["recordings"][1]["gap_from_previous_seconds"], 600
        )

    def test_manual_break_and_join_override_time_rule(self):
        base = datetime(2026, 9, 19, 10, tzinfo=self.timezone)
        recordings = [
            self.recording("one", base),
            self.recording("two", base + timedelta(minutes=2)),
            self.recording("three", base + timedelta(hours=3)),
        ]
        sessions = group_recordings(
            deepcopy(recordings), 3600,
            break_before={"two"}, join_with_previous={"three"},
        )
        self.assertEqual([item["recording_count"] for item in sessions], [1, 2])

    def test_rejects_conflicting_unknown_and_first_join_rules(self):
        base = datetime(2026, 9, 19, 10, tzinfo=self.timezone)
        recordings = [self.recording("one", base)]
        with self.assertRaisesRegex(ValueError, "zugleich"):
            group_recordings(recordings, 3600, {"one"}, {"one"})
        with self.assertRaisesRegex(ValueError, "nicht auf die Auswahl"):
            group_recordings(recordings, 3600, {"unknown"}, set())
        with self.assertRaisesRegex(ValueError, "erste Aufnahme"):
            group_recordings(recordings, 3600, set(), {"one"})

    def test_session_id_stays_stable_when_later_recording_is_added(self):
        base = datetime(2026, 9, 19, 10, tzinfo=self.timezone)
        first = self.recording("one", base)
        initial = group_recordings([deepcopy(first)], 3600)[0]["session_id"]
        extended = group_recordings([
            deepcopy(first), self.recording("two", base + timedelta(minutes=2))
        ], 3600)[0]["session_id"]
        self.assertEqual(initial, extended)

    def test_plan_uses_segment_duration_and_only_published_local_date(self):
        self.add_recording("one", 715, "2026-09-19T23:30:00Z", 600)
        self.add_recording(
            "unpublished", 716, "2026-09-19T23:35:00Z", 60, published=False
        )
        sessions = plan_sessions(
            self.db, date(2026, 9, 20), self.timezone, max_gap_minutes=90
        )
        self.assertEqual(len(sessions), 1)
        recording = sessions[0]["recordings"][0]
        self.assertEqual(recording["drive_id"], "one")
        self.assertEqual(recording["speech_duration_seconds"], 600)
        self.assertEqual(recording["recording_start"].hour, 1)
        self.assertEqual(recording["recording_end"].hour, 1)
        serialized = serializable_session(sessions[0])
        self.assertEqual(serialized["recordings"][0]["recording_number"], 715)

    def test_plan_rejects_missing_segment_sidecar(self):
        self.add_recording(
            "missing", 715, "2026-09-19T09:30:00Z", 60, valid_sidecar=False
        )
        with self.assertRaises(SegmentMetadataError):
            plan_sessions(
                self.db, date(2026, 9, 19), self.timezone, max_gap_minutes=90
            )

    def test_invalid_gap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "nichtnegative"):
            group_recordings([], -1)
        with self.assertRaisesRegex(ValueError, "endliche"):
            group_recordings([], float("nan"))


if __name__ == "__main__":
    unittest.main()
