import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from project_catalog import (
    ProjectCatalogError,
    load_project_catalog,
    project_catalog_hash,
    validate_project_catalog,
)


class ProjectCatalogTest(unittest.TestCase):
    @staticmethod
    def catalog():
        projects = [
            {
                "project_id": "projektverwaltung-steuerung",
                "name": "Projektverwaltung und -Steuerung",
                "status": "active",
                "category": "Z-System – Eigenprojekte",
                "aliases": ["Z04 - MyOwn2Cents"],
                "relations": [],
                "routing": {
                    "enabled": True,
                    "terms": ["z04", "Projektverwaltung"],
                    "exact_terms": ["z04"],
                },
            },
            {
                "project_id": "selbstregulation",
                "name": "selbstregulation",
                "status": "candidate",
                "category": None,
                "aliases": [
                    "Selbstregulation statt Selbstverbesserung",
                    "Selbstregulation statt Selbstoptimierung",
                ],
                "relations": [],
                "routing": {
                    "enabled": True,
                    "terms": ["selbstregulation"],
                    "exact_terms": [],
                },
            },
        ]
        return {
            "schema_version": 1,
            "export_type": "z_system_project_catalog",
            "catalog_id": "z-system-main",
            "catalog_revision": "2026-09-23T14:23:59Z",
            "exported_at": "2026-09-23T14:23:59Z",
            "source": {"application": "Atlas", "instance_id": "atlas-primary"},
            "catalog_hash": project_catalog_hash(projects),
            "projects": projects,
        }

    @staticmethod
    def v2_vector_catalog():
        """Return the projects from Atlas' shared catalog-hash v2 vector."""
        projects = [
            {
                "project_id": "v2-mehrere-daecher",
                "name": "Mehrere Dächer",
                "status": "active",
                "category": "Persönliche Testbereiche",
                "aliases": [],
                "relations": [],
                "concept_relations": [
                    {"relation": "hatDach", "concept_id": "test-bereich-b"},
                    {"relation": "hatDach", "concept_id": "test-dach-a"},
                    {"relation": "hatDach", "concept_id": "test-dach-z"},
                ],
                "routing": {
                    "enabled": True,
                    "terms": ["Mehrere Dächer", "MD"],
                    "exact_terms": ["MD"],
                },
            },
            {
                "project_id": "v2-nur-dachbereich",
                "name": "Nur Dachbereich",
                "status": "active",
                "category": "Testbereich A",
                "aliases": ["Übungsprojekt 🌱"],
                "relations": [],
                "concept_relations": [
                    {"relation": "hatDach", "concept_id": "test-bereich-a"}
                ],
                "routing": {
                    "enabled": True,
                    "terms": ["Nur Dachbereich", "NDB"],
                    "exact_terms": ["NDB"],
                },
            },
            {
                "project_id": "v2-leer",
                "name": "Leere Liste",
                "status": "candidate",
                "category": None,
                "aliases": [],
                "relations": [],
                "concept_relations": [],
                "routing": {
                    "enabled": True,
                    "terms": ["Leere Liste"],
                    "exact_terms": [],
                },
            },
            {
                "project_id": "v2-dachbereich-und-dach",
                "name": "Dachbereich und Dach",
                "status": "active",
                "category": "Testbereich A",
                "aliases": [],
                "relations": [],
                "concept_relations": [
                    {"relation": "hatDach", "concept_id": "test-bereich-a"},
                    {"relation": "hatDach", "concept_id": "test-dach-a"},
                ],
                "routing": {
                    "enabled": True,
                    "terms": ["Dachbereich und Dach", "DUD"],
                    "exact_terms": ["DUD"],
                },
            },
        ]
        return {
            "schema_version": 2,
            "export_type": "z_system_project_catalog",
            "catalog_id": "z-system-v2-test",
            "catalog_revision": "2026-11-14T00:00:00Z",
            "exported_at": "2026-11-14T00:00:00Z",
            "source": {"application": "Atlas", "instance_id": "atlas-test"},
            "catalog_hash": project_catalog_hash(projects),
            "projects": projects,
        }

    def test_valid_catalog_and_file_load_are_read_only(self):
        catalog = self.catalog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            serialized = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
            path.write_text(serialized, encoding="utf-8")

            loaded = load_project_catalog(path)

            self.assertEqual(loaded, catalog)
            self.assertEqual(path.read_text(encoding="utf-8"), serialized)

    def test_rejects_hash_mismatch(self):
        catalog = self.catalog()
        catalog["projects"][0]["name"] = "Manipuliert"
        with self.assertRaisesRegex(ProjectCatalogError, "catalog_hash stimmt nicht"):
            validate_project_catalog(catalog)

    def test_accepts_atlas_v2_hash_vector(self):
        catalog = self.v2_vector_catalog()

        self.assertEqual(
            catalog["catalog_hash"],
            "sha256:4e93f634daab61aa7ac31581413e66add24938d3b7d2b98c10c858464bfa94dc",
        )
        self.assertEqual(validate_project_catalog(catalog), catalog)

        reversed_relations = copy.deepcopy(catalog["projects"])
        for project in reversed_relations:
            project["concept_relations"].reverse()
        self.assertEqual(
            project_catalog_hash(reversed_relations),
            "sha256:95f41c6752da3648cf904a83ac6e2c432254bce704a3bb10b841fa9c5eba65b6",
        )

    def test_v1_rejects_and_v2_requires_concept_relations(self):
        v1 = self.catalog()
        v1["projects"][0]["concept_relations"] = []
        v1["catalog_hash"] = project_catalog_hash(v1["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "unbekannte Felder"):
            validate_project_catalog(v1)

        v2 = self.v2_vector_catalog()
        del v2["projects"][0]["concept_relations"]
        v2["catalog_hash"] = project_catalog_hash(v2["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "fehlende Felder"):
            validate_project_catalog(v2)

    def test_v2_rejects_unsorted_and_duplicate_concept_relations(self):
        unsorted = self.v2_vector_catalog()
        unsorted["projects"][3]["concept_relations"].reverse()
        unsorted["catalog_hash"] = project_catalog_hash(unsorted["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "nicht nach"):
            validate_project_catalog(unsorted)

        duplicate = self.v2_vector_catalog()
        relation = duplicate["projects"][1]["concept_relations"][0]
        duplicate["projects"][1]["concept_relations"].append(copy.deepcopy(relation))
        duplicate["catalog_hash"] = project_catalog_hash(duplicate["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "doppelte Beziehungen"):
            validate_project_catalog(duplicate)

    def test_v2_rejects_embedded_label_and_invalid_concept_id(self):
        label = self.v2_vector_catalog()
        label["projects"][1]["concept_relations"][0]["pref_label"] = "Testbereich A"
        label["catalog_hash"] = project_catalog_hash(label["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "unbekannte Felder"):
            validate_project_catalog(label)

        invalid_id = self.v2_vector_catalog()
        invalid_id["projects"][1]["concept_relations"][0]["concept_id"] = "Kein Slug"
        invalid_id["catalog_hash"] = project_catalog_hash(invalid_id["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "kein stabiler Begriff-Slug"):
            validate_project_catalog(invalid_id)

    def test_v2_allows_null_category_for_non_candidate(self):
        catalog = self.v2_vector_catalog()
        catalog["projects"][0]["category"] = None
        catalog["catalog_hash"] = project_catalog_hash(catalog["projects"])

        self.assertEqual(validate_project_catalog(catalog), catalog)

    def test_rejects_non_slug_and_duplicate_project_ids(self):
        non_slug = self.catalog()
        non_slug["projects"][0]["project_id"] = "Kein Slug"
        non_slug["catalog_hash"] = project_catalog_hash(non_slug["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "kein stabiler Vorhaben-Slug"):
            validate_project_catalog(non_slug)

        duplicate = self.catalog()
        duplicate["projects"][1]["project_id"] = duplicate["projects"][0]["project_id"]
        duplicate["catalog_hash"] = project_catalog_hash(duplicate["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "Doppelte project_id"):
            validate_project_catalog(duplicate)

    def test_rejects_exact_term_outside_terms(self):
        catalog = self.catalog()
        catalog["projects"][0]["routing"]["exact_terms"] = ["z01"]
        catalog["catalog_hash"] = project_catalog_hash(catalog["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "keine Teilmenge"):
            validate_project_catalog(catalog)

    def test_rejects_multiword_exact_term(self):
        catalog = self.catalog()
        catalog["projects"][0]["routing"]["terms"].append("zentrale Verwaltung")
        catalog["projects"][0]["routing"]["exact_terms"] = [
            "zentrale Verwaltung"
        ]
        catalog["catalog_hash"] = project_catalog_hash(catalog["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "Mehrwortbegriffe"):
            validate_project_catalog(catalog)

    def test_rejects_exact_term_without_unicode_word_character(self):
        unicode_catalog = self.catalog()
        unicode_catalog["projects"][0]["routing"]["terms"].append("ä")
        unicode_catalog["projects"][0]["routing"]["exact_terms"] = ["ä"]
        unicode_catalog["catalog_hash"] = project_catalog_hash(
            unicode_catalog["projects"]
        )
        validate_project_catalog(unicode_catalog)

        catalog = self.catalog()
        catalog["projects"][0]["routing"]["terms"].append("!!!")
        catalog["projects"][0]["routing"]["exact_terms"] = ["!!!"]
        catalog["catalog_hash"] = project_catalog_hash(catalog["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "ohne Wortzeichen"):
            validate_project_catalog(catalog)

    def test_rejects_unknown_relation_target(self):
        catalog = self.catalog()
        catalog["projects"][0]["relations"] = [
            {"type": "part_of", "project_id": "unbekannt"}
        ]
        catalog["catalog_hash"] = project_catalog_hash(catalog["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "unbekanntes Projekt"):
            validate_project_catalog(catalog)

    def test_rejects_null_category_for_non_candidate(self):
        catalog = self.catalog()
        catalog["projects"][0]["category"] = None
        catalog["catalog_hash"] = project_catalog_hash(catalog["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "nur bei candidate null"):
            validate_project_catalog(catalog)

    def test_rejects_unknown_fields_and_duplicate_terms(self):
        unknown = self.catalog()
        unknown["projects"][0]["extra"] = True
        unknown["catalog_hash"] = project_catalog_hash(unknown["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "unbekannte Felder"):
            validate_project_catalog(unknown)

        duplicate = copy.deepcopy(self.catalog())
        duplicate["projects"][0]["routing"]["terms"].append("Z04")
        duplicate["catalog_hash"] = project_catalog_hash(duplicate["projects"])
        with self.assertRaisesRegex(ProjectCatalogError, "doppelte Begriffe"):
            validate_project_catalog(duplicate)

    def test_rejects_non_utc_timestamp(self):
        for timestamp in (
            "2026-09-23T16:23:59+02:00",
            "2026-09-23T14:23:59z",
        ):
            with self.subTest(timestamp=timestamp):
                catalog = self.catalog()
                catalog["exported_at"] = timestamp
                with self.assertRaisesRegex(ProjectCatalogError, "muss in UTC"):
                    validate_project_catalog(catalog)

    def test_schema_timestamp_pattern_accepts_only_explicit_utc_suffixes(self):
        schema_path = (
            Path(__file__).parents[1]
            / "schemas"
            / "atlas-project-catalog.schema.v1.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        for property_name in ("catalog_revision", "exported_at"):
            pattern = schema["properties"][property_name]["pattern"]
            self.assertIsNotNone(re.search(pattern, "2026-09-23T14:23:59Z"))
            self.assertIsNotNone(re.search(pattern, "2026-09-23T14:23:59+00:00"))
            self.assertIsNone(re.search(pattern, "2026-09-23T16:23:59+02:00"))
            self.assertIsNone(re.search(pattern, "2026-09-23T14:23:59z"))


if __name__ == "__main__":
    unittest.main()
