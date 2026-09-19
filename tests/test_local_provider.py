import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from providers.local_provider import LocalWhisperProvider


class LocalWhisperProviderTest(unittest.TestCase):
    @patch("providers.local_provider.WhisperModel")
    def test_passes_hotwords_to_faster_whisper(self, model_class):
        segment = Mock()
        segment.text = "Das Traktat"
        segment.start = 1.25
        segment.end = 2.75
        model_class.return_value.transcribe.return_value = ([segment], Mock())
        provider = LocalWhisperProvider(model_size="medium")

        result = provider.transcribe(
            Path("aufnahme.wav"),
            language="de",
            prompt="Fachbegriffe",
            hotwords="Traktat, MyOwnCents",
        )

        self.assertEqual(result, "Das Traktat")
        model_class.return_value.transcribe.assert_called_once_with(
            "aufnahme.wav",
            language="de",
            initial_prompt="Fachbegriffe",
            hotwords="Traktat, MyOwnCents",
            vad_filter=True,
            condition_on_previous_text=True,
            repetition_penalty=1.0,
            compression_ratio_threshold=2.4,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        )

    @patch("providers.local_provider.WhisperModel")
    def test_preserves_segment_times_and_raw_text(self, model_class):
        first = Mock(text=" Erster Abschnitt ", start=0.4, end=2.1)
        second = Mock(text="Zweiter Abschnitt", start=2.3, end=4.8)
        model_class.return_value.transcribe.return_value = (
            iter([first, second]),
            Mock(),
        )
        provider = LocalWhisperProvider(model_size="medium")

        result = provider.transcribe_with_segments(
            Path("aufnahme.wav"), language="de"
        )

        self.assertEqual(result.text, "Erster Abschnitt Zweiter Abschnitt")
        self.assertEqual(
            [(item.start, item.end, item.text) for item in result.segments],
            [
                (0.4, 2.1, " Erster Abschnitt "),
                (2.3, 4.8, "Zweiter Abschnitt"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
