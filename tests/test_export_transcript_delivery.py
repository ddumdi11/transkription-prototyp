import copy
from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path

from export_transcript_delivery import (
    build_delivery,
    delivery_directory_name,
    install_delivery,
    load_routing_manifest,
)
from inbox_watcher import open_state
from plan_session_routing import build_confirmed_manifest
from project_catalog import project_catalog_hash, validate_project_catalog
from publish_transcripts import prepare_publish_state, sha256_file


class ExportTranscriptDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = open_state(self.root / "state.sqlite3")
        prepare_publish_state(self.db)
        self.transcript = self.root / "Aufnahme #725__audio-one.md"
        self.transcript.write_text("# Aufnahme #725\n\nEin Test.\n", encoding="utf-8")
        digest = sha256_file(self.transcript)
        size = self.transcript.stat().st_size
        self.db.execute(
            """INSERT INTO files
               (drive_id,path,size,content_hash,hash_type,mod_time,first_seen,last_seen,
                stable_observations,status,duplicate_of)
               VALUES ('audio-one', 'Aufnahme #725.wav', 100, 'audio-hash', 'sha256',
                       '2026-09-22T13:01:00Z', 1, 2, 2, 'READY', NULL)"""
        )
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES ('audio-one', 'DONE', 1, 'audio.wav', ?, NULL, 3)""",
            (str(self.transcript),),
        )
        self.db.execute(
            """INSERT INTO published_transcripts
               (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
               VALUES ('audio-one', ?, ?, ?,
                       'Aufnahme #725__audio-one.md', 'transcript-one', 4)""",
            (str(self.transcript), digest, size),
        )
        self.db.commit()

        self.session = {
            "session_id": "session-20260922-1501-cb301a63",
            "start": "2026-09-22T14:55:00+02:00",
            "end": "2026-09-22T15:01:00+02:00",
            "span_seconds": 360,
            "recording_count": 1,
            "recordings": [{
                "drive_id": "audio-one",
                "audio_path": "Aufnahme #725.wav",
                "transcript_path": str(self.transcript),
                "segment_path": str(self.transcript.with_suffix(".segments.json")),
                "remote_path": "Aufnahme #725__audio-one.md",
                "recording_number": 725,
                "recording_start": "2026-09-22T14:55:00+02:00",
                "recording_end": "2026-09-22T15:01:00+02:00",
                "speech_duration_seconds": 360,
            }],
            "segment_count": 1,
            "content_assigned_segment_count": 1,
            "unassigned_segment_count": 0,
            "projects": [{
                "project_id": "z-system",
                "name": "z-system",
                "whole_session": False,
                "scopes": ["content"],
                "segments": [{
                    "drive_id": "audio-one",
                    "segment_id": "segment-000001",
                }],
            }],
            "topics": [],
            "unassigned_segments": [],
        }
        self.routing_manifest = build_confirmed_manifest(
            self.session, datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
        )
        projects = [{
            "project_id": "z-system",
            "name": "Z-System",
            "status": "active",
            "category": "Z-System",
            "aliases": [],
            "relations": [],
            "routing": {
                "enabled": True,
                "terms": ["Z-System"],
                "exact_terms": [],
            },
        }]
        self.catalog = validate_project_catalog({
            "schema_version": 1,
            "export_type": "z_system_project_catalog",
            "catalog_id": "z-system-main",
            "catalog_revision": "2026-09-23T14:23:59Z",
            "exported_at": "2026-09-23T14:23:59Z",
            "source": {"application": "Atlas", "instance_id": "atlas-primary"},
            "catalog_hash": project_catalog_hash(projects),
            "projects": projects,
        })

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_builds_verified_delivery_with_canonical_remote_identity(self):
        delivery, paths = build_delivery(
            self.db, self.routing_manifest, self.catalog
        )

        self.assertEqual(delivery["delivery_id"], self.routing_manifest["manifest_key"])
        self.assertEqual(delivery["assets"][0]["canonical_drive_id"], "transcript-one")
        self.assertEqual(delivery["assets"][0]["source_audio_drive_id"], "audio-one")
        self.assertEqual(delivery["assignments"][0]["project_id"], "z-system")
        self.assertEqual(delivery["assignments"][0]["scope"], "segments")
        self.assertEqual(
            delivery["assignments"][0]["segments"],
            [{"source_audio_drive_id": "audio-one", "segment_id": "segment-000001"}],
        )
        self.assertEqual(list(paths.values()), [self.transcript])

    def test_installs_delivery_atomically_and_idempotently(self):
        delivery, paths = build_delivery(
            self.db, self.routing_manifest, self.catalog
        )
        output = self.root / "deliveries"

        target, created = install_delivery(delivery, paths, output)
        repeated, repeated_created = install_delivery(delivery, paths, output)

        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated, target)
        self.assertEqual(
            target.name,
            "session-20260922-1501-cb301a63__"
            + delivery["delivery_id"].rsplit(":", 1)[1],
        )
        self.assertNotIn(":", target.name)
        installed_manifest = json.loads(
            (target / "delivery.json").read_text(encoding="utf-8")
        )
        self.assertEqual(installed_manifest["delivery_id"], delivery["delivery_id"])
        self.assertEqual(
            sha256_file(target / delivery["assets"][0]["relative_path"]),
            delivery["assets"][0]["sha256"],
        )
        self.assertTrue((target / "delivery.json").is_file())

    def test_delivery_directory_name_replaces_only_colons(self):
        self.assertEqual(
            delivery_directory_name("session-20260922-1501-cb301a63:abc123"),
            "session-20260922-1501-cb301a63__abc123",
        )

    def test_rejects_tampered_manifest_unknown_project_and_changed_transcript(self):
        manifest_path = self.root / "routing.json"
        tampered = copy.deepcopy(self.routing_manifest)
        tampered["session"]["end"] = "2026-09-22T15:02:00+02:00"
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "abweichenden Plan-Hash"):
            load_routing_manifest(manifest_path)

        unknown = copy.deepcopy(self.routing_manifest)
        unknown["session"]["projects"][0]["project_id"] = "unknown-project"
        unknown = build_confirmed_manifest(
            unknown["session"], datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
        )
        with self.assertRaisesRegex(ValueError, "fehlen im Atlas-Katalog"):
            build_delivery(self.db, unknown, self.catalog)

        invalid_source = copy.deepcopy(self.routing_manifest)
        invalid_source["session"]["projects"][0]["segments"][0]["drive_id"] = (
            "other-audio"
        )
        invalid_source = build_confirmed_manifest(
            invalid_source["session"],
            datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ValueError, "nicht auf Liefer-Assets"):
            build_delivery(self.db, invalid_source, self.catalog)

        self.transcript.write_text("verändert", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "weicht vom Veröffentlichungsstand ab"):
            build_delivery(self.db, self.routing_manifest, self.catalog)


if __name__ == "__main__":
    unittest.main()
