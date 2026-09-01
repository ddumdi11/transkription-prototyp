import json
import tempfile
import unittest
from pathlib import Path

from inbox_watcher import open_state
from publish_transcripts import prepare_publish_state
from route_transcripts import (load_config, plan_one, published_transcripts,
                               recording_number)


class RouteTranscriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = open_state(self.root / "state.sqlite3")
        prepare_publish_state(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_published(self, text):
        transcript = self.root / "Aufnahme #565__drive-id.md"
        transcript.write_text(text, encoding="utf-8")
        self.db.execute(
            """INSERT INTO files
               (drive_id,path,size,content_hash,hash_type,mod_time,first_seen,last_seen,
                stable_observations,status,duplicate_of)
               VALUES ('drive-id', 'Aufnahme #565.wav', 100, 'hash', 'sha256', NULL,
                       1000, 1010, 2, 'READY', NULL)"""
        )
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES ('drive-id', 'DONE', 1, 'audio.wav', ?, NULL, 1020)""",
            (str(transcript),),
        )
        self.db.execute(
            """INSERT INTO published_transcripts
               (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
               VALUES ('drive-id', ?, 'transcript-hash', 100,
                       'Aufnahme #565__drive-id.md', 'remote-id', 1030)""",
            (str(transcript),),
        )
        self.db.commit()

    def test_plan_combines_default_active_content_and_topics_without_duplicates(self):
        self.add_published("Der Upload wurde ins Drive geladen. Termin bei der IHK.")
        config = {
            "default_projects": ["Z04", "Selbstregulation"],
            "active_projects": ["Watcher", "Selbständigkeit"],
            "project_rules": [
                {"project": "Watcher", "match_any": ["Upload"]},
                {"project": "Selbständigkeit", "match_any": ["IHK"]},
            ],
            "topic_rules": {
                "workflow-test": ["Drive"],
                "behoerden-ihk": ["IHK"],
                "nicht-passend": ["Physiotherapie"],
            },
        }
        row = published_transcripts(self.db)[0]
        plan = plan_one(row, config)

        self.assertEqual([project["name"] for project in plan["projects"]],
                         ["Z04", "Selbstregulation", "Watcher", "Selbständigkeit"])
        self.assertEqual(plan["projects"][2]["reasons"],
                         ["active_context", "content:Upload"])
        self.assertEqual(plan["topics"], ["workflow-test", "behoerden-ihk"])
        self.assertEqual(plan["recording_number"], 565)

    def test_only_published_transcripts_are_candidates(self):
        self.assertEqual(published_transcripts(self.db), [])
        self.add_published("Test")
        self.assertEqual([row["drive_id"] for row in published_transcripts(self.db)],
                         ["drive-id"])

    def test_recording_number_handles_unknown_names(self):
        self.assertEqual(recording_number("Aufnahme #570.wav"), 570)
        self.assertIsNone(recording_number("Meeting.wav"))

    def test_glossary_routes_it_lotse_variants_to_probephase_project(self):
        self.add_published("Platzhalter")
        row = published_transcripts(self.db)[0]
        transcript = Path(row["transcript_path"])
        config_path = self.root / "routing.json"
        config_path.write_text(json.dumps({
            "default_projects": [],
            "active_projects": [],
            "project_rules": [],
            "topic_rules": {},
        }), encoding="utf-8")

        for variant in (
            "IT-Dienstleistungen Probephase", "KI-/IT-Lotse", "KI-Lotse", "IT-Lotse"
        ):
            with self.subTest(variant=variant):
                transcript.write_text(
                    f"Heute arbeite ich als {variant} beim Kunden.", encoding="utf-8"
                )
                projects = [
                    project["name"]
                    for project in plan_one(row, load_config(config_path))["projects"]
                ]
                self.assertEqual(projects, ["IT-Dienstleistungen Probephase"])

    def test_load_config_rejects_malformed_nested_rules(self):
        base = {
            "default_projects": ["Z04"],
            "active_projects": [],
            "project_rules": [{"project": "Watcher", "match_any": ["Upload"]}],
            "topic_rules": {"workflow": ["Drive"]},
        }
        malformed = [
            {**base, "project_rules": ["not-an-object"]},
            {**base, "project_rules": [{"match_any": ["Upload"]}]},
            {**base, "project_rules": [{"project": "Watcher", "match_any": "Upload"}]},
            {**base, "project_rules": [{"project": "Watcher", "match_any": []}]},
            {**base, "project_rules": [{"project": "Watcher", "match_any": ["  "]}]},
            {**base, "topic_rules": {"workflow": "Drive"}},
            {**base, "topic_rules": {"workflow": []}},
            {**base, "topic_rules": {"workflow": ["\t"]}},
        ]
        for index, config in enumerate(malformed):
            with self.subTest(index=index):
                path = self.root / f"malformed-{index}.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
