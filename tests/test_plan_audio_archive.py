import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analyze_segment_quality import find_suspicious_segments
from inbox_watcher import open_state
from plan_audio_archive import (
    build_plan,
    main,
    open_readonly_state,
    summarize,
)
from publish_transcripts import prepare_publish_state
from quality_reviews import ensure_quality_state, sync_quality_candidates


class AudioArchivePlanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.sqlite3"
        self.db = open_state(self.state)
        prepare_publish_state(self.db)
        ensure_quality_state(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_job(
        self,
        drive_id: str = "drive-id",
        source_name: str = "Aufnahme #1.wav",
        *,
        job_status: str = "DONE",
        published: bool = True,
        local_exists: bool = True,
        suspicious: bool = False,
        staged: bool = True,
    ) -> tuple[Path, Path]:
        audio = self.root / f"Aufnahme #1__{drive_id}{Path(source_name).suffix}"
        if local_exists:
            audio.write_bytes(b"audio bytes")
        transcript = self.root / f"Aufnahme #1__{drive_id}.md"
        transcript.write_text("# Test\n", encoding="utf-8")
        text = "Sehr viel Text in einem viel zu kurzen Segment." if suspicious else "Hallo"
        end = 0.1 if suspicious else 1.0
        payload = {
            "schema_version": 1,
            "source_id": drive_id,
            "segments": [{
                "id": "segment-000001", "start": 0.0, "end": end,
                "raw_text": f" {text} ", "text": text,
            }],
        }
        transcript.with_suffix(".segments.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        size = audio.stat().st_size if local_exists else len(b"audio bytes")
        self.db.execute(
            """INSERT INTO files
               (drive_id,path,size,content_hash,hash_type,mod_time,first_seen,last_seen,
                stable_observations,status,duplicate_of)
               VALUES (?, ?, ?, ?, 'sha256', '2026-10-01T10:00:00Z',
                       1000, 1010, 2, 'READY', NULL)""",
            (drive_id, source_name, size, f"hash-{drive_id}"),
        )
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES (?, ?, 1, ?, ?, NULL, 1020)""",
            (drive_id, job_status, str(audio), str(transcript)),
        )
        if staged:
            self.db.execute(
                """INSERT INTO staged_files (drive_id,local_path,content_hash,staged_at)
                   VALUES (?, ?, ?, 1015)""",
                (drive_id, str(audio), f"hash-{drive_id}"),
            )
        if published:
            self.db.execute(
                """INSERT INTO published_transcripts
                   (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
                   VALUES (?, ?, 'transcript-hash', 10, ?, ?, 1030)""",
                (drive_id, str(transcript), transcript.name, f"remote-{drive_id}"),
            )
        self.db.commit()
        return audio, transcript

    def test_plans_published_wav_with_unique_flac_name(self):
        self.add_job()
        item = build_plan(self.db, "gdrive:Audio Archive")[0]

        self.assertEqual(item["archive_status"], "CANDIDATE")
        self.assertEqual(item["archive_name"], "Aufnahme #1__drive-id.flac")
        self.assertEqual(
            item["archive_destination"],
            "gdrive:Audio Archive/Aufnahme #1__drive-id.flac",
        )
        self.assertEqual(item["quality"]["status"], "CLEAR")
        self.assertFalse(item["cleanup_ready"])
        self.assertEqual(
            item["cleanup_blockers"],
            ["archive_not_created", "retention_policy_unconfigured"],
        )

        root_item = build_plan(self.db, "gdrive:")[0]
        self.assertEqual(
            root_item["archive_destination"], "gdrive:Aufnahme #1__drive-id.flac"
        )

    def test_holds_unpublished_missing_and_non_wav_sources(self):
        self.add_job("unpublished", published=False)
        self.add_job("missing", local_exists=False)
        self.add_job("compressed", source_name="Aufnahme #2.m4a")
        self.add_job("unstaged", staged=False)
        items = {item["drive_id"]: item for item in build_plan(self.db)}

        self.assertIn("transcript_not_published", items["unpublished"]["archive_blockers"])
        self.assertIn("local_audio_missing", items["missing"]["archive_blockers"])
        self.assertIn("source_not_wav", items["compressed"]["archive_blockers"])
        self.assertIn("local_audio_not_wav", items["compressed"]["archive_blockers"])
        self.assertIn("staging_record_missing", items["unstaged"]["archive_blockers"])
        self.assertIn("staging_hash_missing", items["unstaged"]["archive_blockers"])
        summary = summarize(list(items.values()))
        self.assertEqual(summary["archive_candidates"], 0)
        self.assertEqual(summary["hold_reasons"]["local_audio_missing"], 1)

    def test_open_quality_candidate_blocks_only_future_cleanup(self):
        _, transcript = self.add_job(suspicious=True)
        payload = json.loads(
            transcript.with_suffix(".segments.json").read_text(encoding="utf-8")
        )
        sync_quality_candidates(
            self.db, "drive-id", find_suspicious_segments(payload), now=1040
        )
        item = build_plan(self.db)[0]

        self.assertEqual(item["archive_status"], "CANDIDATE")
        self.assertEqual(item["quality"]["status"], "PENDING")
        self.assertIn("quality_pending", item["cleanup_blockers"])

    def test_unregistered_and_stale_quality_state_are_conservative(self):
        _, transcript = self.add_job("unregistered", suspicious=True)
        first = build_plan(self.db, selected_ids={"unregistered"})[0]
        self.assertEqual(first["quality"]["status"], "UNREGISTERED")

        payload = json.loads(
            transcript.with_suffix(".segments.json").read_text(encoding="utf-8")
        )
        sync_quality_candidates(
            self.db, "unregistered", find_suspicious_segments(payload), now=1040
        )
        payload["segments"][0]["end"] = 10.0
        transcript.with_suffix(".segments.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        second = build_plan(self.db, selected_ids={"unregistered"})[0]
        self.assertEqual(second["quality"]["status"], "STATE_MISMATCH")

    def test_readonly_connection_rejects_writes_and_commit_is_harmless(self):
        self.add_job()
        self.db.commit()
        with open_readonly_state(self.state) as readonly:
            self.assertEqual(readonly.execute("PRAGMA query_only").fetchone()[0], 1)
            build_plan(readonly)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                readonly.execute(
                    "INSERT INTO pipeline_settings (key,value) VALUES ('x','y')"
                )
            readonly.commit()
        self.assertIsNone(
            self.db.execute(
                "SELECT value FROM pipeline_settings WHERE key='x'"
            ).fetchone()
        )

    def test_unknown_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unbekannte Drive-ID"):
            build_plan(self.db, selected_ids={"missing"})

    def test_missing_state_does_not_create_logging_directory(self):
        missing = self.root / "not-created" / "state.sqlite3"
        self.assertEqual(main(["--state", str(missing)]), 1)
        self.assertFalse(missing.parent.exists())


if __name__ == "__main__":
    unittest.main()
