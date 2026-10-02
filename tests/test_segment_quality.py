import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from analyze_segment_quality import (
    analyze_audio_context,
    build_quality_report,
    find_suspicious_segments,
    main,
)


class SegmentQualityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio = self.root / "Aufnahme #1__drive-id.wav"
        self.audio.write_bytes(b"source audio")
        self.segments = self.root / "Aufnahme #1__drive-id.segments.json"
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
                    "start": 0.0,
                    "end": 10.0,
                    "raw_text": " Normaler Abschnitt mit ausreichender Dauer. ",
                    "text": "Normaler Abschnitt mit ausreichender Dauer.",
                },
                {
                    "id": "segment-000002",
                    "start": 10.0,
                    "end": 10.1,
                    "raw_text": " Viel zu viel korrekt erkannter Text für eine Zehntelsekunde. ",
                    "text": "Viel zu viel korrekt erkannter Text für eine Zehntelsekunde.",
                },
            ],
        }
        self.segments.write_text(
            json.dumps(self.payload), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _fake_audio_context(
        audio_path,
        start,
        end,
        context_seconds,
        vad_threshold,
        timeout_seconds,
        **kwargs,
    ):
        return {
            "window_start": start - context_seconds,
            "window_end": end + context_seconds,
            "zones": {
                "before": {"classification": "speech", "speech_seconds": 2.0},
                "nominal": {"classification": "noise_or_uncertain", "speech_seconds": 0.0},
                "after": {"classification": "speech", "speech_seconds": 2.0},
            },
            "interpretation": "speech_detected_outside_nominal_window",
        }

    def test_finds_density_and_short_duration_without_flagging_normal_segment(self):
        candidates = find_suspicious_segments(self.payload)

        self.assertEqual([item["id"] for item in candidates], ["segment-000002"])
        self.assertEqual(
            candidates[0]["flags"],
            ["implausible_text_density", "very_short_text_segment"],
        )
        self.assertGreater(candidates[0]["chars_per_second"], 500)
        self.assertEqual(candidates[0]["previous_gap"], 0.0)

    def test_explicit_segment_is_reported_even_without_flags(self):
        candidates = find_suspicious_segments(
            self.payload,
            selected_ids={"segment-000001"},
        )

        self.assertEqual(len(candidates), 2)
        normal = candidates[0]
        self.assertEqual(normal["id"], "segment-000001")
        self.assertEqual(normal["flags"], [])
        self.assertTrue(normal["explicitly_selected"])

    def test_rejects_unknown_explicit_segment(self):
        with self.assertRaisesRegex(ValueError, "nicht gefunden"):
            find_suspicious_segments(
                self.payload,
                selected_ids={"segment-missing"},
            )

    def test_build_report_is_read_only_and_uses_audio_context(self):
        before = {path.name for path in self.root.iterdir()}
        report = build_quality_report(
            self.audio,
            self.segments,
            audio_analyzer=self._fake_audio_context,
        )
        after = {path.name for path in self.root.iterdir()}

        self.assertEqual(before, after)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["summary"]["total_segments"], 2)
        self.assertEqual(report["summary"]["reported_segments"], 1)
        self.assertEqual(
            report["segments"][0]["audio_context"]["interpretation"],
            "speech_detected_outside_nominal_window",
        )

    def test_text_only_does_not_call_audio_analyzer(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("Audioanalyse darf nicht laufen")

        report = build_quality_report(
            self.audio,
            self.segments,
            text_only=True,
            audio_analyzer=forbidden,
        )

        self.assertIsNone(report["segments"][0]["audio_context"])

    def test_selected_normal_segment_is_not_called_too_short(self):
        def nominal_speech(*args, **kwargs):
            return {
                "zones": {
                    "before": {"classification": "silence"},
                    "nominal": {"classification": "speech"},
                    "after": {"classification": "silence"},
                },
                "interpretation": "speech_detected_in_nominal_window",
            }

        report = build_quality_report(
            self.audio,
            self.segments,
            selected_ids={"segment-000001"},
            audio_analyzer=nominal_speech,
        )

        normal = next(
            item for item in report["segments"]
            if item["id"] == "segment-000001"
        )
        self.assertEqual(
            normal["audio_context"]["interpretation"],
            "speech_detected_in_nominal_window",
        )

    def test_audio_window_is_streamed_and_detects_nearby_speech(self):
        sample_rate = 16_000
        samples = np.full(int(4.1 * sample_rate), 0.01, dtype="<f4")

        def fake_run(command, check, capture_output, timeout):
            self.assertTrue(check)
            self.assertTrue(capture_output)
            self.assertGreater(timeout, 0)
            self.assertEqual(command[-1], "pipe:1")
            return SimpleNamespace(stdout=samples.tobytes(), stderr=b"")

        speech = [
            {"start": int(0.5 * sample_rate), "end": int(1.5 * sample_rate)},
            {"start": int(2.5 * sample_rate), "end": int(3.5 * sample_rate)},
        ]
        with (
            patch(
                "analyze_segment_quality.subprocess.run",
                side_effect=fake_run,
            ),
            patch(
                "faster_whisper.vad.get_speech_timestamps",
                return_value=speech,
            ),
        ):
            result = analyze_audio_context(
                self.audio,
                segment_start=2.0,
                segment_end=2.1,
                context_seconds=2.0,
            )

        self.assertEqual(result["zones"]["before"]["classification"], "speech")
        self.assertEqual(
            result["zones"]["nominal"]["classification"],
            "noise_or_uncertain",
        )
        self.assertEqual(result["zones"]["after"]["classification"], "speech")
        self.assertEqual(
            result["interpretation"],
            "speech_detected_outside_nominal_window",
        )

    def test_main_prints_report_without_writing_files(self):
        before = {path.name for path in self.root.iterdir()}
        stdout = io.StringIO()
        with (
            patch(
                "analyze_segment_quality.analyze_audio_context",
                side_effect=self._fake_audio_context,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            result = main([str(self.audio), str(self.segments)])

        self.assertEqual(result, 0)
        self.assertIn("rein lesend", stdout.getvalue())
        self.assertIn("segment-000002", stdout.getvalue())
        self.assertEqual(before, {path.name for path in self.root.iterdir()})

    def test_main_reports_ffmpeg_failure(self):
        stderr = io.StringIO()
        error = subprocess.CalledProcessError(
            1, ["ffmpeg"], stderr=b"decode failed"
        )
        with (
            patch(
                "analyze_segment_quality.analyze_audio_context",
                side_effect=error,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = main([str(self.audio), str(self.segments)])

        self.assertEqual(result, 1)
        self.assertIn("decode failed", stderr.getvalue())

    def test_main_reports_missing_audio_dependency_without_traceback(self):
        stderr = io.StringIO()
        with (
            patch(
                "analyze_segment_quality.analyze_audio_context",
                side_effect=ImportError("No module named 'faster_whisper'"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = main([str(self.audio), str(self.segments)])

        message = stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("NumPy und Faster-Whisper", message)
        self.assertIn("requirements-local.txt", message)
        self.assertNotIn("Traceback", message)


if __name__ == "__main__":
    unittest.main()
