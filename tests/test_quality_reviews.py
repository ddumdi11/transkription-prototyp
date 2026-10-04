import json
import tempfile
import unittest
from pathlib import Path

from inbox_watcher import open_state
from quality_reviews import (
    candidate_hash,
    current_quality_reviews,
    decide_quality_review,
    ensure_quality_state,
    sync_quality_candidates,
)


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
        first = sync_quality_candidates(self.db, "drive-id", [item], now=1000)
        second = sync_quality_candidates(self.db, "drive-id", [item], now=1100)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        row = second[0]
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["candidate_hash"], candidate_hash(item))
        self.assertEqual(row["first_detected_at"], 1000)
        self.assertEqual(row["last_detected_at"], 1100)
        count = self.db.execute("SELECT COUNT(*) FROM quality_reviews").fetchone()[0]
        self.assertEqual(count, 1)

    def test_changed_candidate_supersedes_without_destroying_decision(self):
        original = candidate()
        fingerprint = candidate_hash(original)
        sync_quality_candidates(self.db, "drive-id", [original], now=1000)
        decide_quality_review(
            self.db,
            "drive-id",
            "segment-000001",
            fingerprint,
            "CONFIRMED",
            issue_types=["BOUNDARY_ERROR"],
            now=1010,
        )

        changed = candidate("Ein anderer Segmenttext mit ebenfalls hoher Textdichte.")
        rows = sync_quality_candidates(self.db, "drive-id", [changed], now=1100)

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
        fingerprint = candidate_hash(item)
        sync_quality_candidates(self.db, "drive-id", [item], now=1000)

        changed = decide_quality_review(
            self.db,
            "drive-id",
            "segment-000001",
            fingerprint,
            "CONFIRMED",
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

    def test_divergent_second_decision_is_rejected(self):
        item = candidate()
        fingerprint = candidate_hash(item)
        sync_quality_candidates(self.db, "drive-id", [item], now=1000)
        decide_quality_review(
            self.db,
            "drive-id",
            "segment-000001",
            fingerprint,
            "CONFIRMED",
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
                issue_types=["OMISSION"],
                now=1200,
            )

    def test_dismissal_rejects_issue_types(self):
        item = candidate()
        fingerprint = candidate_hash(item)
        sync_quality_candidates(self.db, "drive-id", [item], now=1000)

        with self.assertRaisesRegex(ValueError, "keine Fehlerart"):
            decide_quality_review(
                self.db,
                "drive-id",
                "segment-000001",
                fingerprint,
                "DISMISSED",
                issue_types=["BOUNDARY_ERROR"],
            )

        with self.assertRaisesRegex(ValueError, "keinen bestätigten Text"):
            decide_quality_review(
                self.db,
                "drive-id",
                "segment-000001",
                fingerprint,
                "DISMISSED",
                confirmed_text="Kein Fehler",
            )

    def test_empty_rescan_removes_candidate_from_current_view(self):
        sync_quality_candidates(self.db, "drive-id", [candidate()], now=1000)
        sync_quality_candidates(self.db, "drive-id", [], now=1100)

        self.assertEqual(current_quality_reviews(self.db), [])
        row = self.db.execute("SELECT is_current FROM quality_reviews").fetchone()
        self.assertEqual(row["is_current"], 0)


if __name__ == "__main__":
    unittest.main()
