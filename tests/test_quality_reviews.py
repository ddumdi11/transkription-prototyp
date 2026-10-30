import json
import tempfile
import unittest
from pathlib import Path

from inbox_watcher import open_state
from quality_reviews import (
    candidate_hash,
    current_quality_reviews,
    curate_quality_review,
    decide_quality_review,
    ensure_quality_state,
    sync_quality_candidates,
)


TRANSCRIPT_SHA256 = "a" * 64
CHANGED_TRANSCRIPT_SHA256 = "b" * 64


def candidate(text="Sehr viel Text in einem viel zu kurzen Segment."):
    return {
        "id": "segment-000001",
        "start": 10.0,
        "end": 10.5,
        "duration": 0.5,
        "text_characters": len(text),
        "chars_per_second": len(text) / 0.5,
        "raw_text": f" {text} ",
        "text": text,
        "flags": ["implausible_text_density"],
        "explicitly_selected": False,
        "previous_gap": 0.0,
        "next_gap": 30.0,
    }


class QualityReviewsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = open_state(Path(self.temp.name) / "state.sqlite3")
        ensure_quality_state(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_sync_registers_pending_candidate_idempotently(self):
        item = candidate()
        first = sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )
        second = sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1100,
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        row = second[0]
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(
            row["candidate_hash"], candidate_hash(item, TRANSCRIPT_SHA256)
        )
        self.assertEqual(
            row["candidate_transcript_sha256"], TRANSCRIPT_SHA256
        )
        self.assertEqual(row["first_detected_at"], 1000)
        self.assertEqual(row["last_detected_at"], 1100)
        count = self.db.execute("SELECT COUNT(*) FROM quality_reviews").fetchone()[0]
        self.assertEqual(count, 1)

    def test_changed_candidate_supersedes_without_destroying_decision(self):
        original = candidate()
        fingerprint = candidate_hash(original, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [original],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )
        decide_quality_review(
            self.db,
            "drive-id",
            "segment-000001",
            fingerprint,
            "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["BOUNDARY_ERROR"],
            now=1010,
        )

        changed = candidate("Ein anderer Segmenttext mit ebenfalls hoher Textdichte.")
        rows = sync_quality_candidates(
            self.db, "drive-id", [changed],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1100,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "PENDING")
        self.assertNotEqual(rows[0]["candidate_hash"], fingerprint)
        history = list(self.db.execute(
            "SELECT status, is_current FROM quality_reviews ORDER BY first_detected_at"
        ))
        self.assertEqual(
            [(row["status"], row["is_current"]) for row in history],
            [("CONFIRMED", 0), ("PENDING", 1)],
        )

    def test_confirmation_is_exact_and_idempotent(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )

        changed = decide_quality_review(
            self.db,
            "drive-id",
            "segment-000001",
            fingerprint,
            "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["WORD_ERROR", "BOUNDARY_ERROR"],
            note="Text vorhanden, Zeitgrenze falsch.",
            confirmed_text="Bestätigter Text",
            speaker_intent="Gemeinte Präzisierung",
            now=1100,
        )
        repeated = decide_quality_review(
            self.db,
            "drive-id",
            "segment-000001",
            fingerprint,
            "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["BOUNDARY_ERROR", "WORD_ERROR"],
            note="Text vorhanden, Zeitgrenze falsch.",
            confirmed_text="Bestätigter Text",
            speaker_intent="Gemeinte Präzisierung",
            now=1200,
        )

        self.assertTrue(changed)
        self.assertFalse(repeated)
        row = current_quality_reviews(self.db)[0]
        self.assertEqual(row["status"], "CONFIRMED")
        self.assertEqual(
            json.loads(row["issue_types_json"]),
            ["BOUNDARY_ERROR", "WORD_ERROR"],
        )
        self.assertEqual(row["decided_at"], 1100)
        self.assertEqual(row["transcript_sha256"], TRANSCRIPT_SHA256)

    def test_divergent_second_decision_is_rejected(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )
        decide_quality_review(
            self.db,
            "drive-id",
            "segment-000001",
            fingerprint,
            "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["BOUNDARY_ERROR"],
            now=1100,
        )

        with self.assertRaisesRegex(FileExistsError, "abweichende"):
            decide_quality_review(
                self.db,
                "drive-id",
                "segment-000001",
                fingerprint,
                "CONFIRMED",
                transcript_sha256=TRANSCRIPT_SHA256,
                issue_types=["OMISSION"],
                now=1200,
            )

    def test_confirmed_review_can_be_curated_exactly_once(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )
        decide_quality_review(
            self.db, "drive-id", "segment-000001", fingerprint, "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["OMISSION"], now=1100,
        )

        changed = curate_quality_review(
            self.db, "drive-id", "segment-000001", fingerprint, "REPLACE",
            transcript_sha256=TRANSCRIPT_SHA256,
            confirmed_text="Vollständiger bestätigter Text.", now=1200,
        )
        repeated = curate_quality_review(
            self.db, "drive-id", "segment-000001", fingerprint, "REPLACE",
            transcript_sha256=TRANSCRIPT_SHA256,
            confirmed_text="Vollständiger bestätigter Text.", now=1300,
        )

        self.assertTrue(changed)
        self.assertFalse(repeated)
        row = current_quality_reviews(self.db)[0]
        self.assertEqual(row["curation_action"], "REPLACE")
        self.assertEqual(row["confirmed_text"], "Vollständiger bestätigter Text.")
        self.assertEqual(row["curated_at"], 1200)

        with self.assertRaisesRegex(FileExistsError, "abweichende Kuratierung"):
            curate_quality_review(
                self.db, "drive-id", "segment-000001", fingerprint, "REPLACE",
                transcript_sha256=TRANSCRIPT_SHA256,
                confirmed_text="Anderer Text.", now=1400,
            )

    def test_delete_curation_requires_confirmed_hallucination(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )
        decide_quality_review(
            self.db, "drive-id", "segment-000001", fingerprint, "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["WORD_ERROR"], now=1100,
        )

        with self.assertRaisesRegex(ValueError, "HALLUCINATION"):
            curate_quality_review(
                self.db, "drive-id", "segment-000001", fingerprint, "DELETE",
                transcript_sha256=TRANSCRIPT_SHA256,
            )

        self.db.execute(
            "UPDATE quality_reviews SET issue_types_json='[\"HALLUCINATION\"]'"
        )
        self.db.commit()
        self.assertTrue(curate_quality_review(
            self.db, "drive-id", "segment-000001", fingerprint, "DELETE",
            transcript_sha256=TRANSCRIPT_SHA256, now=1200,
        ))

    def test_curation_rejects_changed_transcript_version(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )
        decide_quality_review(
            self.db, "drive-id", "segment-000001", fingerprint, "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["OMISSION"], now=1100,
        )

        with self.assertRaisesRegex(ValueError, "Transkriptversion"):
            curate_quality_review(
                self.db, "drive-id", "segment-000001", fingerprint, "REPLACE",
                transcript_sha256=CHANGED_TRANSCRIPT_SHA256,
                confirmed_text="Korrektur.",
            )

    def test_curation_rejects_pending_and_boundary_only_review(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )

        with self.assertRaisesRegex(ValueError, "Nur bestätigte"):
            curate_quality_review(
                self.db, "drive-id", "segment-000001", fingerprint, "REPLACE",
                transcript_sha256=TRANSCRIPT_SHA256,
                confirmed_text="Korrektur.",
            )

        decide_quality_review(
            self.db, "drive-id", "segment-000001", fingerprint, "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["BOUNDARY_ERROR"], now=1100,
        )
        with self.assertRaisesRegex(ValueError, "Grenzfehler"):
            curate_quality_review(
                self.db, "drive-id", "segment-000001", fingerprint, "REPLACE",
                transcript_sha256=TRANSCRIPT_SHA256,
                confirmed_text="Korrektur.",
            )

    def test_curation_rejects_malformed_issue_types(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )
        decide_quality_review(
            self.db, "drive-id", "segment-000001", fingerprint, "CONFIRMED",
            transcript_sha256=TRANSCRIPT_SHA256,
            issue_types=["OMISSION"], now=1100,
        )
        self.db.execute(
            "UPDATE quality_reviews SET issue_types_json='{\"OMISSION\": true}'"
        )
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "ungültige Fehlerarten"):
            curate_quality_review(
                self.db, "drive-id", "segment-000001", fingerprint, "REPLACE",
                transcript_sha256=TRANSCRIPT_SHA256,
                confirmed_text="Korrektur.",
            )

    def test_decision_rejects_transcript_changed_since_registration(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )

        with self.assertRaisesRegex(ValueError, "seit der Kandidatenregistrierung"):
            decide_quality_review(
                self.db,
                "drive-id",
                "segment-000001",
                fingerprint,
                "CONFIRMED",
                transcript_sha256=CHANGED_TRANSCRIPT_SHA256,
                issue_types=["BOUNDARY_ERROR"],
                now=1100,
            )

        row = current_quality_reviews(self.db)[0]
        self.assertEqual(row["status"], "PENDING")
        self.assertIsNone(row["transcript_sha256"])

        replacement = sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=CHANGED_TRANSCRIPT_SHA256,
            segment_transcript_sha256=CHANGED_TRANSCRIPT_SHA256, now=1200,
        )[0]
        self.assertNotEqual(replacement["candidate_hash"], fingerprint)
        self.assertEqual(
            replacement["candidate_transcript_sha256"],
            CHANGED_TRANSCRIPT_SHA256,
        )

    def test_registration_rejects_stale_segment_metadata(self):
        with self.assertRaisesRegex(
            ValueError, "nicht zur aktuellen Transkriptversion"
        ):
            sync_quality_candidates(
                self.db,
                "drive-id",
                [candidate()],
                transcript_sha256=CHANGED_TRANSCRIPT_SHA256,
                segment_transcript_sha256=TRANSCRIPT_SHA256,
                now=1000,
            )

        count = self.db.execute(
            "SELECT COUNT(*) FROM quality_reviews"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_dismissal_rejects_issue_types(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )

        with self.assertRaisesRegex(ValueError, "keine Fehlerart"):
            decide_quality_review(
                self.db,
                "drive-id",
                "segment-000001",
                fingerprint,
                "DISMISSED",
                transcript_sha256=TRANSCRIPT_SHA256,
                issue_types=["BOUNDARY_ERROR"],
            )

        with self.assertRaisesRegex(ValueError, "keinen bestätigten Text"):
            decide_quality_review(
                self.db,
                "drive-id",
                "segment-000001",
                fingerprint,
                "DISMISSED",
                transcript_sha256=TRANSCRIPT_SHA256,
                confirmed_text="Kein Fehler",
            )

    def test_decision_rejects_invalid_transcript_hash(self):
        item = candidate()
        fingerprint = candidate_hash(item, TRANSCRIPT_SHA256)
        sync_quality_candidates(
            self.db, "drive-id", [item],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            decide_quality_review(
                self.db,
                "drive-id",
                "segment-000001",
                fingerprint,
                "CONFIRMED",
                transcript_sha256="not-a-hash",
                issue_types=["BOUNDARY_ERROR"],
            )

    def test_empty_rescan_removes_candidate_from_current_view(self):
        sync_quality_candidates(
            self.db, "drive-id", [candidate()],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1000,
        )
        sync_quality_candidates(
            self.db, "drive-id", [],
            transcript_sha256=TRANSCRIPT_SHA256,
            segment_transcript_sha256=TRANSCRIPT_SHA256, now=1100,
        )

        self.assertEqual(current_quality_reviews(self.db), [])
        row = self.db.execute("SELECT is_current FROM quality_reviews").fetchone()
        self.assertEqual(row["is_current"], 0)


if __name__ == "__main__":
    unittest.main()
