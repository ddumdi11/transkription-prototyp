import json
import tempfile
import unittest
from pathlib import Path

from providers import TranscriptSegment
from transcribe import transcript_exists, write_segment_metadata


class SegmentMetadataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp.name)
        self.audio = self.output_dir / "Aufnahme #1.wav"
        self.transcript = self.output_dir / "Aufnahme #1.md"
        self.transcript.write_text("Transkript", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_writes_stable_raw_and_normalized_segments(self):
        path = write_segment_metadata(
            audio_path=self.audio,
            transcript_path=self.transcript,
            segments=(
                TranscriptSegment(0.12349, 2.34567, " Das Taktat "),
                TranscriptSegment(2.5, 4.0, "Zweiter Abschnitt"),
            ),
            output_dir=self.output_dir,
            provider_name="local",
            model_name="medium",
            language="de",
            replacements={"Taktat": "Traktat"},
            source_id="drive-id-1",
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["audio_file"], "Aufnahme #1.wav")
        self.assertEqual(payload["transcript_file"], "Aufnahme #1.md")
        self.assertEqual(payload["provider"], "local")
        self.assertEqual(payload["model"], "medium")
        self.assertEqual(payload["language"], "de")
        self.assertEqual(payload["source_id"], "drive-id-1")
        self.assertEqual(
            payload["segments"],
            [
                {
                    "id": "segment-000001",
                    "start": 0.123,
                    "end": 2.346,
                    "raw_text": " Das Taktat ",
                    "text": "Das Traktat",
                },
                {
                    "id": "segment-000002",
                    "start": 2.5,
                    "end": 4.0,
                    "raw_text": "Zweiter Abschnitt",
                    "text": "Zweiter Abschnitt",
                },
            ],
        )
        self.assertFalse(path.with_name(f"{path.name}.tmp").exists())

    def test_existing_transcript_without_required_segments_is_incomplete(self):
        self.assertTrue(
            transcript_exists(self.audio, self.output_dir, ".md")
        )
        self.assertFalse(
            transcript_exists(
                self.audio,
                self.output_dir,
                ".md",
                require_segments=True,
            )
        )

    def test_writes_empty_segment_list_for_recording_without_speech(self):
        path = write_segment_metadata(
            audio_path=self.audio,
            transcript_path=self.transcript,
            segments=(),
            output_dir=self.output_dir,
            provider_name="local",
            model_name="medium",
            language="de",
            replacements={},
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["segments"], [])

    def test_rejects_invalid_segment_bounds(self):
        with self.assertRaisesRegex(ValueError, "Ungültige Zeitgrenzen"):
            write_segment_metadata(
                audio_path=self.audio,
                transcript_path=self.transcript,
                segments=(TranscriptSegment(3.0, 2.0, "Fehler"),),
                output_dir=self.output_dir,
                provider_name="local",
                model_name="medium",
                language="de",
                replacements={},
            )


if __name__ == "__main__":
    unittest.main()
