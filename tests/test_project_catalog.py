import copy
import json
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
        catalog = self.catalog()
        catalog["exported_at"] = "2026-09-23T16:23:59+02:00"
        with self.assertRaisesRegex(ProjectCatalogError, "muss in UTC"):
            validate_project_catalog(catalog)


if __name__ == "__main__":
    unittest.main()
