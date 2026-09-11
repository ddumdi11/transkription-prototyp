import unittest

from transcribe import apply_replacements


class ApplyReplacementsTest(unittest.TestCase):
    def test_replaces_observed_complete_term(self):
        self.assertEqual(
            apply_replacements(
                "Das Quen-Modell nutzt eine Job-Kühe.",
                {"Quen": "Qwen", "Job-Kühe": "Job Queue"},
            ),
            "Das Qwen-Modell nutzt eine Job Queue.",
        )

    def test_does_not_replace_inside_longer_word(self):
        self.assertEqual(
            apply_replacements("Quentin bleibt unverändert.", {"Quen": "Qwen"}),
            "Quentin bleibt unverändert.",
        )


if __name__ == "__main__":
    unittest.main()
