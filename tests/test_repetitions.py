"""Rein lesende Prüfung auf längere Wortwiederholungen (analyze_repetitions)."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from analyze_repetitions import (
    build_repetition_report,
    find_repetitions,
    main,
    tokenize,
)


def filler(prefix: str, count: int = 20) -> str:
    """Eindeutige Füllwörter, die sich weder untereinander noch mit PHRASE wiederholen."""
    return " ".join(f"{prefix}{i}" for i in range(count))


FILLER, FILLER_B, FILLER_C = filler("a"), filler("b"), filler("c")
PHRASE = "diesen Schritt haben wir versäumt oder ich habe auch versäumt"  # 10 Wörter


def words(text: str) -> list[str]:
    """Text wie das Werkzeug in Wörter zerlegen."""
    return tokenize(text)


class FindRepetitionsTest(unittest.TestCase):
    """Kernlogik auf Wortlisten."""

    def test_clean_text_has_no_findings(self):
        """Text ohne wiederholte Wortfolge ergibt keine Verdachtsstelle."""
        self.assertEqual(find_repetitions(words(FILLER)), [])

    def test_distant_repetition_is_reported_with_length_distance_and_count(self):
        """Eine entfernte Wiederholung wird mit Länge, Abstand und Anzahl gemeldet."""
        text = f"{PHRASE} {FILLER} {PHRASE}"
        [finding] = find_repetitions(words(text))
        self.assertEqual(finding["length_words"], 10)
        self.assertEqual(finding["first_word_index"], 0)
        self.assertEqual(finding["repeat_word_index"], 30)
        self.assertEqual(finding["distance_words"], 30)
        self.assertEqual(finding["occurrences"], 2)
        self.assertEqual(finding["text"], PHRASE)

    def test_shorter_than_min_words_is_ignored(self):
        """Wiederholungen unterhalb der Mindestlänge werden nicht gemeldet."""
        short = "haben wir versäumt oder ich habe auch"  # 7 Wörter
        self.assertEqual(find_repetitions(words(f"{short} {FILLER} {short}")), [])
        self.assertEqual(len(find_repetitions(words(f"{short} {FILLER} {short}"),
                                              min_words=7)), 1)

    def test_case_and_punctuation_are_ignored_but_original_text_is_shown(self):
        """Groß-/Kleinschreibung und Satzzeichen verhindern keinen Treffer."""
        variant = "Diesen Schritt, haben wir versäumt. Oder ich habe auch versäumt!"
        [finding] = find_repetitions(words(f"{PHRASE} {FILLER} {variant}"))
        self.assertEqual(finding["length_words"], 10)
        self.assertTrue(finding["text"].startswith("Diesen Schritt haben"))

    def test_three_occurrences_use_nearest_earlier_occurrence(self):
        """Bei mehrfachen Vorkommen zählt der Abstand zum nächstliegenden."""
        text = f"{PHRASE} {FILLER} {FILLER_B} {PHRASE} {FILLER_C} {PHRASE}"
        findings = find_repetitions(words(text))
        self.assertEqual([f["distance_words"] for f in findings], [50, 30])
        self.assertEqual([f["length_words"] for f in findings], [10, 10])
        self.assertEqual([f["occurrences"] for f in findings], [3, 3])

    def test_max_distance_limits_reported_findings(self):
        """--max-distance blendet weiter entfernte Wiederholungen aus."""
        text = f"{PHRASE} {FILLER} {FILLER_B} {PHRASE} {FILLER_C} {PHRASE}"
        findings = find_repetitions(words(text), max_distance=40)
        self.assertEqual([f["repeat_word_index"] for f in findings], [80])

    def test_invalid_settings_are_rejected(self):
        """Unsinnige Schwellen führen zu ValueError statt stiller Fehlmessung."""
        with self.assertRaises(ValueError):
            find_repetitions(["a"], min_words=1)
        with self.assertRaises(ValueError):
            find_repetitions(["a"], min_words=8, max_distance=5)


class ReportFileTest(unittest.TestCase):
    """Dateieingabe als Markdown oder Segmentdatei, ohne Änderung der Datei."""

    def setUp(self):
        """Temporäres Verzeichnis für Testdateien anlegen."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _markdown(self) -> Path:
        """Transkript mit Metadatenkopf und einer Wiederholung schreiben."""
        path = self.root / "Aufnahme #1.md"
        path.write_text(
            "# Aufnahme #1\n- Datei: Aufnahme #1.wav\n- Modell: medium (local)\n\n"
            f"{PHRASE} {FILLER} {PHRASE}\n",
            encoding="utf-8",
        )
        return path

    def _segments(self) -> Path:
        """Gültige Segmentdatei mit einer Wiederholung im zweiten Segment schreiben."""
        path = self.root / "Aufnahme #1.segments.json"
        payload = {
            "schema_version": 1,
            "audio_file": "Aufnahme #1.wav",
            "transcript_file": "Aufnahme #1.md",
            "provider": "local",
            "model": "medium",
            "language": "de",
            "source_id": None,
            "segments": [
                {"id": "segment-000001", "start": 0.5, "end": 12.0,
                 "raw_text": f" {PHRASE} {FILLER}", "text": "ersetzt"},
                {"id": "segment-000002", "start": 75.0, "end": 80.0,
                 "raw_text": f" {PHRASE}.", "text": "ersetzt"},
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_markdown_header_is_not_counted(self):
        """Metadatenkopf zählt nicht als Transkripttext."""
        report = build_repetition_report(self._markdown())
        self.assertEqual(report["source"], "markdown")
        self.assertEqual(report["summary"]["total_words"], 40)
        self.assertEqual(report["summary"]["findings"], 1)
        self.assertIsNone(report["findings"][0]["repeat_start_seconds"])

    def test_overlapping_findings_count_repeated_words_once(self):
        """Überlappende Fundstellen bleiben getrennt, zählen in der Summe aber einfach."""
        x = filler("x", 12).split()
        text = " ".join([*x[0:8], FILLER, *x[4:12], FILLER_B, *x])
        path = self.root / "Ueberlappung.md"
        path.write_text(text, encoding="utf-8")
        report = build_repetition_report(path)
        self.assertEqual([f["length_words"] for f in report["findings"]], [8, 8])
        self.assertEqual([f["repeat_word_index"] for f in report["findings"]], [56, 60])
        self.assertEqual(report["summary"]["repeated_words"], 12)
        self.assertEqual(report["summary"]["repeated_ratio"], round(12 / 68, 4))

    def test_segments_use_raw_text_and_report_times(self):
        """Segmentdatei: ASR-Rohtext wird geprüft, Startzeiten werden gemeldet."""
        report = build_repetition_report(self._segments())
        self.assertEqual(report["source"], "segments_raw_text")
        [finding] = report["findings"]
        self.assertEqual(finding["first_start_seconds"], 0.5)
        self.assertEqual(finding["repeat_start_seconds"], 75.0)

    def test_cli_reports_findings_with_exit_code_zero_and_leaves_file_unchanged(self):
        """CLI meldet nur; Exit-Code 0 trotz Treffer, Datei bleibt byte-gleich."""
        path = self._segments()
        before = path.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([str(path)]), 0)
        self.assertIn("Verdachtsstellen: 1", out.getvalue())
        self.assertIn("1:15.0", out.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_cli_json_output(self):
        """--json liefert den Bericht maschinenlesbar."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([str(self._markdown()), "--json", "--min-words", "9"]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["settings"], {"min_words": 9, "max_distance": None})
        self.assertEqual(report["findings"][0]["length_words"], 10)

    def test_cli_errors_return_exit_code_one(self):
        """Fehlende Datei oder ungültige Schwelle ergeben Exit-Code 1."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.root / "fehlt.md")]), 1)
            self.assertEqual(main([str(self._markdown()), "--min-words", "1"]), 1)
        self.assertIn("Wiederholungsprüfung fehlgeschlagen", err.getvalue())


if __name__ == "__main__":
    unittest.main()
