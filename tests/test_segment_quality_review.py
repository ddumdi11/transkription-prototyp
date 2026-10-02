import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from providers.base import TranscriptSegment, TranscriptionResult
from review_segment_quality import (
    build_review_plans,
    compare_segment_text,
    extract_review_clip,
    main,
    write_local_retranscription,
)


class SegmentQualityReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio = self.root / "Aufnahme #1__drive-id.wav"
        self.audio.write_bytes(b"source audio")
        self.segments = self.root / "Aufnahme #1__drive-id.segments.json"
        self.output = self.root / "quality-review"
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
                    "start": 1.0,
                    "end": 9.0,
                    "raw_text": " Vorheriger normaler Abschnitt. ",
                    "text": "Vorheriger normaler Abschnitt.",
                },
                {
                    "id": "segment-000002",
                    "start": 10.0,
                    "end": 10.1,
                    "raw_text": " Sehr viel gesprochener Text für nur eine Zehntelsekunde. ",
                    "text": "Sehr viel gesprochener Text für nur eine Zehntelsekunde.",
                },
                {
                    "id": "segment-000003",
                    "start": 11.0,
                    "end": 18.0,
                    "raw_text": " Nachfolgender normaler Abschnitt. ",
                    "text": "Nachfolgender normaler Abschnitt.",
                },
            ],
        }
        self.segments.write_text(
            json.dumps(self.payload, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def _plan(self):
        plans = build_review_plans(
            self.audio, self.segments, self.output, context_seconds=4.0
        )
        self.assertEqual(len(plans), 1)
        return plans[0]

    @staticmethod
    def _fake_ffmpeg(command, check, capture_output, text, timeout):
        assert check and capture_output and text and timeout > 0
        Path(command[-1]).write_bytes(b"lossless flac clip")
        return SimpleNamespace(stdout="", stderr="")

    def test_builds_padded_plan_with_adjacent_segments(self):
        plan = self._plan()

        self.assertEqual(plan.candidate["id"], "segment-000002")
        self.assertEqual(plan.clip_start, 6.0)
        self.assertEqual(plan.clip_end, 14.1)
        self.assertEqual(plan.previous_segment["id"], "segment-000001")
        self.assertEqual(plan.next_segment["id"], "segment-000003")
        self.assertEqual(plan.output_audio.suffix, ".flac")
        self.assertFalse(self.output.exists())

    def test_explicit_selection_returns_only_selected_normal_segment(self):
        plans = build_review_plans(
            self.audio,
            self.segments,
            self.output,
            selected_ids={"segment-000001"},
        )

        self.assertEqual([plan.candidate["id"] for plan in plans], ["segment-000001"])

    def test_can_extend_window_to_next_segment_with_hard_limit(self):
        plan = build_review_plans(
            self.audio,
            self.segments,
            self.output,
            context_seconds=4.0,
            extend_to_next_segment=True,
            max_extended_seconds=5.0,
        )[0]

        self.assertTrue(plan.extended_to_next_segment)
        self.assertEqual(plan.clip_start, 6.0)
        self.assertEqual(plan.clip_end, 15.0)

        limited = build_review_plans(
            self.audio,
            self.segments,
            self.output,
            context_seconds=4.0,
            extend_to_next_segment=True,
            max_extended_seconds=4.5,
        )[0]
        self.assertEqual(limited.clip_end, 14.5)

    def test_main_is_dry_run_by_default_and_json_is_valid(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main([
                str(self.audio),
                str(self.segments),
                "--output-dir", str(self.output),
                "--json",
            ])

        report = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(report["dry_run"])
        self.assertEqual(len(report["plans"]), 1)
        self.assertFalse(self.output.exists())

    def test_confirm_extracts_lossless_context_and_is_idempotent(self):
        plan = self._plan()
        with patch(
            "review_segment_quality.subprocess.run", side_effect=self._fake_ffmpeg
        ) as ffmpeg:
            audio, metadata, created = extract_review_clip(plan)

        self.assertTrue(created)
        command = ffmpeg.call_args.args[0]
        self.assertEqual(command[command.index("-ss") + 1], "6.000")
        self.assertEqual(command[command.index("-t") + 1], "8.100")
        self.assertEqual(command[command.index("-c:a") + 1], "flac")
        saved = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual(saved["candidate"]["id"], "segment-000002")
        self.assertEqual(
            saved["adjacent_segments"]["previous"]["id"], "segment-000001"
        )
        self.assertEqual(saved["clip"]["size"], audio.stat().st_size)

        with patch("review_segment_quality.subprocess.run") as rerun:
            same_audio, same_metadata, created_again = extract_review_clip(plan)
        self.assertFalse(created_again)
        self.assertEqual((same_audio, same_metadata), (audio, metadata))
        rerun.assert_not_called()

    def test_failed_extraction_leaves_no_partial_files(self):
        plan = self._plan()
        error = subprocess.CalledProcessError(
            1, ["ffmpeg"], stderr="decode failed"
        )
        with (
            patch("review_segment_quality.subprocess.run", side_effect=error),
            self.assertRaisesRegex(RuntimeError, "decode failed"),
        ):
            extract_review_clip(plan)

        self.assertEqual(list(self.output.iterdir()), [])

    def test_local_retranscription_records_source_times_and_comparison(self):
        plan = self._plan()
        with patch(
            "review_segment_quality.subprocess.run", side_effect=self._fake_ffmpeg
        ):
            extract_review_clip(plan)
        provider = SimpleNamespace(
            transcribe_with_segments=lambda *args, **kwargs: TranscriptionResult(
                text="Sehr viel gesprochener Text für nur eine Zehntelsekunde.",
                segments=(
                    TranscriptSegment(
                        start=3.8,
                        end=7.2,
                        text=" Sehr viel gesprochener Text für nur eine Zehntelsekunde. ",
                    ),
                    TranscriptSegment(
                        start=8.0,
                        end=9.0,
                        text=" Nachlauf mit erneut unplausibler Zeitgrenze. ",
                    ),
                ),
            )
        )

        result_path, created = write_local_retranscription(
            plan,
            provider,
            model="medium",
            language="de",
            prompt="Fachbegriffe",
            hotwords="Traktat",
        )

        self.assertTrue(created)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["recognition"]["model"], "medium")
        self.assertEqual(result["recognition"]["prompt"], "Fachbegriffe")
        self.assertEqual(len(result["recognition"]["sha256"]), 64)
        self.assertEqual(result["segments"][0]["source_start"], 9.8)
        self.assertEqual(result["segments"][0]["source_end"], 13.2)
        self.assertTrue(
            result["segments"][0]["timestamp_within_requested_window"]
        )
        self.assertFalse(
            result["segments"][1]["timestamp_within_requested_window"]
        )
        self.assertEqual(result["timestamp_warning_count"], 1)
        self.assertEqual(
            result["comparison"]["assessment"], "original_text_supported"
        )
        self.assertEqual(
            result["comparison"]["ordered_original_token_coverage"], 1.0
        )

        same_path, created_again = write_local_retranscription(
            plan,
            provider,
            model="medium",
            language="de",
            prompt="Fachbegriffe",
            hotwords="Traktat",
        )
        self.assertEqual(same_path, result_path)
        self.assertFalse(created_again)

        result["recognition"] = "beschädigt"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "Abweichende"):
            write_local_retranscription(
                plan,
                provider,
                model="medium",
                language="de",
                prompt="Fachbegriffe",
                hotwords="Traktat",
            )

        variant_path, variant_created = write_local_retranscription(
            plan,
            provider,
            model="small",
            language="de",
            prompt="Fachbegriffe",
            hotwords="Traktat",
        )
        self.assertTrue(variant_created)
        self.assertNotEqual(variant_path, result_path)

    def test_comparison_marks_low_match_for_human_listening(self):
        comparison = compare_segment_text(
            "Das ist der ursprüngliche längere Text.",
            "Vollständig andere Wörter.",
        )
        self.assertEqual(comparison["assessment"], "low_match_needs_listening")

    def test_local_transcription_requires_confirmation(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main([
                str(self.audio),
                str(self.segments),
                "--transcribe-local",
            ])
        self.assertEqual(result, 1)
        self.assertIn("erfordert --confirm", stderr.getvalue())

    def test_batch_inputs_share_one_dry_run(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main([
                "--input", str(self.audio), str(self.segments),
                "--input", str(self.audio), str(self.segments),
                "--output-dir", str(self.output),
                "--json",
            ])

        report = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(len(report["plans"]), 2)
        self.assertFalse(self.output.exists())

    def test_segment_selection_rejects_ambiguous_batch_scope(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main([
                "--input", str(self.audio), str(self.segments),
                "--input", str(self.audio), str(self.segments),
                "--segment-id", "segment-000002",
            ])

        self.assertEqual(result, 1)
        self.assertIn("genau einem Eingabepaar", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
