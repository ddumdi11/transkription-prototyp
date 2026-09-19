import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from create_correction_sample import build_plan, create_sample, main


class CorrectionSampleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio = self.root / "Aufnahme #1__drive-id.wav"
        self.audio.write_bytes(b"source audio")
        self.segments = self.root / "Aufnahme #1__drive-id.segments.json"
        self.output = self.root / "samples"
        self.payload = {
            "schema_version": 1,
            "audio_file": self.audio.name,
            "transcript_file": "Aufnahme #1__drive-id.md",
            "provider": "local",
            "model": "medium",
            "language": "de",
            "source_id": "drive-id",
            "segments": [
                {
                    "id": "segment-000001",
                    "start": 0.5,
                    "end": 2.75,
                    "raw_text": " Das Taktat ",
                    "text": "Das Traktat",
                },
                {
                    "id": "segment-000002",
                    "start": 3.0,
                    "end": 4.5,
                    "raw_text": " Anderer Text ",
                    "text": "Anderer Text",
                },
            ],
        }
        self._write_payload()

    def tearDown(self):
        self.temp.cleanup()

    def _write_payload(self):
        self.segments.write_text(
            json.dumps(self.payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_build_plan_selects_unique_term_and_normalized_correction(self):
        plan = build_plan(
            self.audio, self.segments, self.output, term="Traktat"
        )

        self.assertEqual(plan.segment["id"], "segment-000001")
        self.assertEqual(plan.segment["raw_text"], " Das Taktat ")
        self.assertEqual(plan.corrected_text, "Das Traktat")
        self.assertEqual(plan.source_id, "drive-id")
        self.assertEqual(plan.output_audio.suffix, ".wav")
        self.assertEqual(plan.output_audio.parent, self.output)

    def test_ambiguous_term_requires_segment_id(self):
        self.payload["segments"][1]["raw_text"] = " Noch ein Taktat "
        self.payload["segments"][1]["text"] = "Noch ein Traktat"
        self._write_payload()

        with self.assertRaisesRegex(ValueError, "--segment-id"):
            build_plan(
                self.audio, self.segments, self.output, term="Traktat"
            )

        plan = build_plan(
            self.audio,
            self.segments,
            self.output,
            term="Traktat",
            segment_id="segment-000002",
        )
        self.assertEqual(plan.segment["id"], "segment-000002")

    def test_rejects_unchanged_text(self):
        with self.assertRaisesRegex(ValueError, "unterscheidet sich nicht"):
            build_plan(
                self.audio,
                self.segments,
                self.output,
                term="Anderer",
                segment_id="segment-000002",
            )

    def test_default_is_dry_run_without_file_writes(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main([
                str(self.audio),
                str(self.segments),
                "--term", "Traktat",
                "--output-dir", str(self.output),
            ])

        self.assertEqual(result, 0)
        self.assertIn("Dry-Run", stdout.getvalue())
        self.assertFalse(self.output.exists())

    def test_confirm_extracts_losslessly_and_is_idempotent(self):
        plan = build_plan(
            self.audio, self.segments, self.output, term="Traktat"
        )

        def fake_ffmpeg(command, check, capture_output, text):
            self.assertTrue(check)
            self.assertTrue(capture_output)
            self.assertTrue(text)
            copy_index = command.index("-c:a")
            self.assertEqual(command[copy_index + 1], "copy")
            Path(command[-1]).write_bytes(b"lossless clip")

        with patch(
            "create_correction_sample.subprocess.run",
            side_effect=fake_ffmpeg,
        ) as run:
            audio_path, metadata_path, created = create_sample(plan)

        self.assertTrue(created)
        run.assert_called_once()
        self.assertEqual(audio_path.read_bytes(), b"lossless clip")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["source"]["source_id"], "drive-id")
        self.assertEqual(metadata["segment"]["raw_text"], " Das Taktat ")
        self.assertEqual(
            metadata["correction"]["confirmed_text"], "Das Traktat"
        )
        self.assertEqual(metadata["clip"]["codec_mode"], "copy")

        with patch("create_correction_sample.subprocess.run") as rerun:
            second_audio, second_metadata, created = create_sample(plan)
        self.assertFalse(created)
        rerun.assert_not_called()
        self.assertEqual(second_audio, audio_path)
        self.assertEqual(second_metadata, metadata_path)

    def test_rejects_audio_from_another_sidecar(self):
        self.payload["audio_file"] = "andere.wav"
        self._write_payload()

        with self.assertRaisesRegex(ValueError, "passt nicht"):
            build_plan(
                self.audio, self.segments, self.output, term="Traktat"
            )


if __name__ == "__main__":
    unittest.main()
