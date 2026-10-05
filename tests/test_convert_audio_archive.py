import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from convert_audio_archive import (
    build_conversion_plan,
    convert_one,
    decoded_pcm_sha256,
    operation_timeout,
    probe_audio,
    validate_media_match,
)


class ConvertAudioArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "Aufnahme #780__drive-id.wav"
        self.source.write_bytes(b"unchanged wav bytes")
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        item = {
            "archive_status": "CANDIDATE",
            "archive_blockers": [],
            "drive_id": "drive-id",
            "source_path": "Aufnahme #780.wav",
            "local_audio": str(self.source),
            "source_size": self.source.stat().st_size,
            "source_hash_type": "sha256",
            "source_hash": source_hash,
            "archive_name": "Aufnahme #780__drive-id.flac",
            "quality": {"status": "CLEAR"},
        }
        self.output = self.root / "archive"
        self.plan = build_conversion_plan(item, self.output)
        self.source_probe = {
            "audio_stream_count": 1, "codec": "pcm_s16le",
            "sample_rate": 48000, "channels": 1,
            "channel_layout": "mono", "duration": 60.0,
        }
        self.archive_probe = {**self.source_probe, "codec": "flac"}

    def tearDown(self):
        self.temp.cleanup()

    def test_build_plan_rejects_held_source(self):
        with self.assertRaisesRegex(ValueError, "local_audio_missing"):
            build_conversion_plan({
                "archive_status": "HOLD",
                "archive_blockers": ["local_audio_missing"],
            }, self.output)

    def test_timeout_validation(self):
        self.assertEqual(operation_timeout(10.0), 50.0)
        self.assertEqual(operation_timeout(10.0, 123.0), 123.0)
        for value in (0.0, -1.0, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                operation_timeout(10.0, value)

    def test_probe_and_pcm_hash_parsing(self):
        completed = subprocess.CompletedProcess(
            [], 0,
            stdout=json.dumps({
                "streams": [{
                    "codec_name": "flac", "sample_rate": "48000",
                    "channels": 1, "channel_layout": "mono",
                }],
                "format": {"duration": "12.5"},
            }),
            stderr="",
        )
        with patch("convert_audio_archive._run", return_value=completed):
            probe = probe_audio(self.source, 30.0)
        self.assertEqual(probe["duration"], 12.5)
        self.assertEqual(probe["sample_rate"], 48000)
        self.assertEqual(probe["audio_stream_count"], 1)

        digest = "a" * 64
        completed.stdout = f"SHA256={digest}\n"
        with patch("convert_audio_archive._run", return_value=completed):
            self.assertEqual(decoded_pcm_sha256(self.source, 30.0), digest)

        completed.stdout = json.dumps({
            "streams": [{
                "codec_name": "flac", "sample_rate": "48000",
                "channels": 1, "duration": "N/A",
            }],
            "format": {"duration": "13.25"},
        })
        with patch("convert_audio_archive._run", return_value=completed):
            self.assertEqual(probe_audio(self.source, 30.0)["duration"], 13.25)

        completed.stdout = json.dumps({
            "streams": [
                {
                    "codec_name": "pcm_s16le", "sample_rate": "48000",
                    "channels": 1, "duration": "13.25",
                },
                {
                    "codec_name": "pcm_s16le", "sample_rate": "48000",
                    "channels": 1, "duration": "13.25",
                },
            ],
            "format": {"duration": "13.25"},
        })
        with patch("convert_audio_archive._run", return_value=completed):
            multi = probe_audio(self.source, 30.0)
        self.assertEqual(multi["audio_stream_count"], 2)

    def test_media_verification_rejects_lossy_or_changed_output(self):
        verified = validate_media_match(
            self.source_probe, self.archive_probe, "a" * 64, "a" * 64
        )
        self.assertTrue(verified["full_decode"])

        changed = {**self.archive_probe, "sample_rate": 44100}
        with self.assertRaisesRegex(ValueError, "Abtastrate"):
            validate_media_match(
                self.source_probe, changed, "a" * 64, "a" * 64
            )
        with self.assertRaisesRegex(ValueError, "PCM-Inhalt"):
            validate_media_match(
                self.source_probe, self.archive_probe, "a" * 64, "b" * 64
            )

    def test_conversion_is_atomic_idempotent_and_preserves_source(self):
        original = self.source.read_bytes()

        def fake_conversion(command, _timeout):
            self.assertEqual(command[0], "ffmpeg")
            self.assertIn("flac", command)
            Path(command[-1]).write_bytes(b"verified flac")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch("convert_audio_archive._run", side_effect=fake_conversion),
            patch(
                "convert_audio_archive.probe_audio",
                side_effect=[self.source_probe, self.archive_probe],
            ),
            patch(
                "convert_audio_archive.decoded_pcm_sha256",
                side_effect=["a" * 64, "a" * 64],
            ),
        ):
            audio, metadata, created = convert_one(self.plan)

        self.assertTrue(created)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(audio.is_file())
        self.assertTrue(metadata.is_file())
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertTrue(payload["verification"]["full_decode"])
        self.assertFalse(payload["cleanup_ready"])
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

        with patch("convert_audio_archive._run") as run:
            repeated_audio, repeated_metadata, repeated = convert_one(self.plan)
        self.assertFalse(repeated)
        self.assertEqual(repeated_audio, audio)
        self.assertEqual(repeated_metadata, metadata)
        run.assert_not_called()

    def test_hash_mismatch_and_conversion_failure_leave_no_package(self):
        changed_plan = dict(self.plan)
        changed_plan["source_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "WAV-Hash"):
            convert_one(changed_plan)
        self.assertFalse(self.output.exists())

        def failed_conversion(_command, _timeout):
            raise RuntimeError("FFmpeg test failure")

        with (
            patch("convert_audio_archive._run", side_effect=failed_conversion),
            patch("convert_audio_archive.probe_audio", return_value=self.source_probe),
        ):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                convert_one(self.plan)
        self.assertFalse(Path(self.plan["package_dir"]).exists())
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

    def test_multiple_source_streams_are_rejected_before_encoding(self):
        multi_stream = {**self.source_probe, "audio_stream_count": 2}
        with (
            patch("convert_audio_archive.probe_audio", return_value=multi_stream),
            patch("convert_audio_archive._run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "2 Audiospuren"):
                convert_one(self.plan)
        run.assert_not_called()
        self.assertFalse(Path(self.plan["package_dir"]).exists())

    def test_directory_fsync_failure_rolls_back_installed_package(self):
        def fake_conversion(command, _timeout):
            Path(command[-1]).write_bytes(b"verified flac")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch("convert_audio_archive._run", side_effect=fake_conversion),
            patch(
                "convert_audio_archive.probe_audio",
                side_effect=[self.source_probe, self.archive_probe],
            ),
            patch(
                "convert_audio_archive.decoded_pcm_sha256",
                side_effect=["a" * 64, "a" * 64],
            ),
            patch(
                "convert_audio_archive.fsync_directory",
                side_effect=[OSError("directory fsync failed"), None],
            ),
        ):
            with self.assertRaisesRegex(OSError, "directory fsync failed"):
                convert_one(self.plan)
        self.assertFalse(Path(self.plan["package_dir"]).exists())
        self.assertTrue(self.source.is_file())


if __name__ == "__main__":
    unittest.main()
