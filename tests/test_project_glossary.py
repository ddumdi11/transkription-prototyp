import json
import tempfile
import unittest
from pathlib import Path

from project_glossary import (glossary_hotwords, glossary_project_rules,
                              glossary_replacements, load_glossary)


class ProjectGlossaryTest(unittest.TestCase):
    def test_defaults_feed_recognition_correction_and_routing(self):
        glossary = load_glossary(Path("project_glossary.json"))

        self.assertIn("Traktat", glossary_hotwords(glossary))
        self.assertEqual(glossary_replacements(glossary)["Taktat"], "Traktat")
        service_rule = next(
            rule for rule in glossary_project_rules(glossary)
            if rule["project"] == "IT-Dienstleistungen Probephase"
        )
        self.assertEqual(
            service_rule["match_any"],
            [
                "IT-Dienstleistungen Probephase", "KI-/IT-Lotse", "KI-Lotse",
                "IT-Lotse", "Digitaler Check",
            ],
        )

    def test_observed_personal_and_technical_terms_are_available(self):
        glossary = load_glossary(Path("project_glossary.json"))
        hotwords = glossary_hotwords(glossary)
        replacements = glossary_replacements(glossary)

        for term in (
            "Z-System", "Abbildwerkstatt", "Lullipulli", "Qwen", "Job Queue",
            "Checkpointing", "idempotent", "GGUF", "Bitwarden", "IHK",
        ):
            self.assertIn(term, hotwords)
        self.assertEqual(replacements["Set-System"], "Z-System")
        self.assertEqual(replacements["Quen"], "Qwen")
        self.assertEqual(replacements["Job-Kühe"], "Job Queue")
        self.assertEqual(replacements["Bitwaden"], "Bitwarden")
        self.assertEqual(replacements["IRK"], "IHK")

    def test_rejects_ambiguous_replacement(self):
        data = {
            "terms": [
                {"canonical": "Eins", "replacements": ["Fehler"]},
                {"canonical": "Zwei", "replacements": ["Fehler"]},
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "glossary.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Mehrdeutige Ersetzung"):
                load_glossary(path)


if __name__ == "__main__":
    unittest.main()
