import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from plan_session_routing import build_session_routing
from segment_metadata import SegmentMetadataError


class SessionRoutingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.timezone = ZoneInfo("Europe/Berlin")

    def tearDown(self):
        self.temp.cleanup()

    def session(self, segments, source_id="drive-one"):
        segment_path = self.root / "Aufnahme #715__drive-one.segments.json"
        segment_path.write_text(json.dumps({
            "schema_version": 1,
            "source_id": source_id,
            "segments": segments,
        }), encoding="utf-8")
        start = datetime(2026, 9, 19, 11, 0, tzinfo=self.timezone)
        end = datetime(2026, 9, 19, 11, 30, tzinfo=self.timezone)
        return {
            "session_id": "session-test",
            "start": start,
            "end": end,
            "span_seconds": 1800,
            "recording_count": 1,
            "recordings": [{
                "drive_id": "drive-one",
                "audio_path": "Aufnahme #715.wav",
                "transcript_path": str(self.root / "Aufnahme #715__drive-one.md"),
                "segment_path": str(segment_path),
                "remote_path": "Aufnahme #715__drive-one.md",
                "recording_number": 715,
                "recording_start": start,
                "recording_end": end,
                "speech_duration_seconds": 1800,
            }],
        }

    @staticmethod
    def segment(number, start, end, text):
        return {
            "id": f"segment-{number:06d}",
            "start": start,
            "end": end,
            "raw_text": f" {text} ",
            "text": text,
        }

    @staticmethod
    def config():
        return {
            "default_projects": ["Z04", "Gemeinsam"],
            "active_projects": ["Watcher", "Gemeinsam"],
            "exact_terms": ["Drive"],
            "project_rules": [
                {"project": "Watcher", "match_any": ["Pipeline"]},
                {"project": "Watcher", "match_any": ["Drive"]},
                {"project": "IT-Dienstleistungen", "match_any": ["KI-Lotse"]},
            ],
            "topic_rules": {
                "workflow": ["Pipeline", "Drive"],
                "recherche": ["Recherche"],
            },
        }

    def test_combines_whole_session_projects_with_segment_evidence(self):
        session = self.session([
            self.segment(1, 10, 20, "Die Pipeline lädt das Ergebnis ins Drive."),
            self.segment(2, 30, 40, "Ich arbeite als KI-Lotse."),
            self.segment(3, 50, 60, "Diese Recherche braucht noch Fakten."),
            self.segment(4, 70, 80, "Ein allgemeiner Gedanke."),
        ])

        result = build_session_routing(session, self.config())

        self.assertEqual(result["segment_count"], 4)
        self.assertEqual(result["content_assigned_segment_count"], 2)
        self.assertEqual(result["unassigned_segment_count"], 2)
        projects = {project["name"]: project for project in result["projects"]}
        self.assertEqual(projects["Z04"]["scopes"], ["default"])
        self.assertTrue(projects["Z04"]["whole_session"])
        self.assertEqual(
            projects["Gemeinsam"]["scopes"], ["default", "active_context"]
        )
        self.assertEqual(
            projects["Watcher"]["scopes"], ["active_context", "content"]
        )
        self.assertEqual(len(projects["Watcher"]["segments"]), 1)
        self.assertEqual(
            projects["Watcher"]["segments"][0]["matched_terms"],
            ["Pipeline", "Drive"],
        )
        self.assertFalse(projects["IT-Dienstleistungen"]["whole_session"])
        self.assertEqual(
            projects["IT-Dienstleistungen"]["segments"][0]["segment_id"],
            "segment-000002",
        )

        topics = {topic["name"]: topic for topic in result["topics"]}
        self.assertEqual(len(topics["workflow"]["segments"]), 1)
        self.assertEqual(len(topics["recherche"]["segments"]), 1)
        self.assertEqual(
            [item["segment_id"] for item in result["unassigned_segments"]],
            ["segment-000003", "segment-000004"],
        )
        evidence = projects["Watcher"]["segments"][0]
        self.assertEqual(evidence["absolute_start"], "2026-09-19T11:00:10+02:00")
        self.assertEqual(evidence["absolute_end"], "2026-09-19T11:00:20+02:00")

    def test_empty_recording_keeps_whole_session_destinations(self):
        result = build_session_routing(self.session([]), self.config())
        self.assertEqual(result["segment_count"], 0)
        self.assertEqual(result["unassigned_segments"], [])
        self.assertEqual(
            [project["name"] for project in result["projects"]],
            ["Z04", "Gemeinsam", "Watcher"],
        )

    def test_exact_term_does_not_match_inside_driven(self):
        result = build_session_routing(self.session([
            self.segment(1, 10, 20, "Ich lerne Test Driven Development."),
        ]), self.config())

        projects = {project["name"]: project for project in result["projects"]}
        self.assertNotIn("content", projects["Watcher"]["scopes"])
        self.assertEqual(result["topics"], [])

    def test_rejects_sidecar_from_another_recording(self):
        session = self.session([], source_id="other-drive")
        with self.assertRaises(SegmentMetadataError):
            build_session_routing(session, self.config())


if __name__ == "__main__":
    unittest.main()
