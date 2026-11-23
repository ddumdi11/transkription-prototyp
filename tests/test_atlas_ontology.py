import copy
import json
import tempfile
import unittest
from pathlib import Path

from atlas_ontology import (
    OntologyError,
    canonical_concepts,
    load_ontology,
    ontology_hash,
    parse_ontology_json,
    validate_catalog_concepts,
    validate_ontology,
    validate_production_identity,
)
from project_catalog import project_catalog_hash


class AtlasOntologyTest(unittest.TestCase):
    @staticmethod
    def concepts():
        return [
            {
                "concept_id": "test-dach-z", "type": "dach",
                "pref_label": "Dach Z", "alt_labels": [], "hidden_labels": [],
                "definition": None, "status": "active", "deprecated_at": None,
                "broader": ["test-dach-a"], "narrower": [], "related": [],
            },
            {
                "concept_id": "test-bereich-a", "type": "dachbereich",
                "pref_label": "Testbereich A",
                "alt_labels": ["Bereich A", "Ärger-Bereich"],
                "hidden_labels": [],
                "definition": "Künstlicher Dachbereich für den Testvektor.",
                "status": "active", "deprecated_at": None, "broader": [],
                "narrower": ["test-dach-a", "test-dach-alt"], "related": [],
            },
            {
                "concept_id": "test-thema-y", "type": "thema",
                "pref_label": "Thema Y", "alt_labels": [], "hidden_labels": [],
                "definition": None, "status": "active", "deprecated_at": None,
                "broader": [], "narrower": [], "related": ["test-thema-x"],
            },
            {
                "concept_id": "test-dach-a", "type": "dach",
                "pref_label": "Dach A", "alt_labels": ["Dach-A 🌱"],
                "hidden_labels": ["dach ah", "dachah"],
                "definition": "Dach mit zwei Oberbegriffen (Polyhierarchie).",
                "status": "active", "deprecated_at": None,
                "broader": ["test-bereich-a", "test-bereich-b"],
                "narrower": ["test-dach-z"], "related": [],
            },
            {
                "concept_id": "test-teil-transkript", "type": "quellenteil",
                "pref_label": "Transkript", "alt_labels": [], "hidden_labels": [],
                "definition": None, "status": "active", "deprecated_at": None,
                "broader": [], "narrower": [], "related": [],
            },
            {
                "concept_id": "test-bereich-b", "type": "dachbereich",
                "pref_label": "Testbereich B", "alt_labels": [],
                "hidden_labels": [], "definition": None, "status": "active",
                "deprecated_at": None, "broader": [],
                "narrower": ["test-dach-a"], "related": [],
            },
            {
                "concept_id": "test-dach-alt", "type": "dach",
                "pref_label": "Altes Dach", "alt_labels": [], "hidden_labels": [],
                "definition": None, "status": "deprecated",
                "deprecated_at": "2026-11-22", "broader": ["test-bereich-a"],
                "narrower": [], "related": [],
            },
            {
                "concept_id": "test-thema-x", "type": "thema",
                "pref_label": "Thema X", "alt_labels": [], "hidden_labels": [],
                "definition": "Thema mit symmetrischem related.",
                "status": "active", "deprecated_at": None, "broader": [],
                "narrower": [], "related": ["test-thema-y"],
            },
        ]

    @classmethod
    def ontology(cls):
        concepts = cls.concepts()
        return {
            "schema_version": 1,
            "export_type": "z_system_ontology",
            "ontology_id": "test-ontologie",
            "ontology_revision": "2026-11-22T12:00:00Z",
            "exported_at": "2026-11-22T12:00:00Z",
            "source": {"application": "Testvektor", "instance_id": "test"},
            "stamm_version": "0.3",
            "ontology_hash": ontology_hash(concepts),
            "concepts": concepts,
        }

    @staticmethod
    def sort_concept():
        return {
            "concept_id": "test-sortierung", "type": "thema",
            "pref_label": "Sortierung nach Codepoints",
            "alt_labels": ["Probe ﬁ", "Probe 🌱"],
            "hidden_labels": ["Ａ", "😀"],
            "definition": "Je Liste ein Emoji und ein Zeichen aus U+E000 bis U+FFFF.",
            "status": "active", "deprecated_at": None, "broader": [],
            "narrower": [], "related": [],
        }

    @staticmethod
    def refresh_hash(ontology):
        ontology["ontology_hash"] = ontology_hash(ontology["concepts"])
        return ontology

    def concept(self, ontology, concept_id):
        return next(
            concept for concept in ontology["concepts"]
            if concept["concept_id"] == concept_id
        )

    def test_shared_main_and_control_hashes(self):
        """Reproduce the shared main vector and reversed-broader hashes."""
        concepts = self.concepts()
        self.assertEqual(len(canonical_concepts(concepts)), 1975)
        self.assertEqual(
            ontology_hash(concepts),
            "sha256:af452e87dfbe55f13032334b4fdf3005684d81a14b667e1ed1baa4ab40d559e3",
        )
        reversed_broader = copy.deepcopy(concepts)
        self.concept({"concepts": reversed_broader}, "test-dach-a")[
            "broader"
        ].reverse()
        self.assertEqual(
            ontology_hash(reversed_broader),
            "sha256:13ee8a4a1b406570db268d570828aaf4cb8d4d1dd10a109df87ccf6461c6ccfe",
        )

    def test_shared_codepoint_sort_vector_and_utf16_negative(self):
        """Distinguish Python codepoint ordering from JavaScript UTF-16 order."""
        concept = self.sort_concept()
        self.assertEqual(len(canonical_concepts([concept])), 314)
        self.assertEqual(
            ontology_hash([concept]),
            "sha256:bb4fcec3f109ed175c4348d6f8ac8ececb6d56f7500db21db7b0fc8da49cd23f",
        )
        ontology = self.ontology()
        ontology["concepts"] = [concept]
        self.refresh_hash(ontology)
        self.assertEqual(validate_ontology(ontology), ontology)

        concept["alt_labels"] = ["Probe 🌱", "Probe ﬁ"]
        self.refresh_hash(ontology)
        with self.assertRaisesRegex(OntologyError, "Unicode-Codepoints"):
            validate_ontology(ontology)

    def test_valid_file_load_is_read_only(self):
        """Accept the shared structure without changing the source file."""
        ontology = self.ontology()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "atlas-ontology.v1.json"
            original = json.dumps(ontology, ensure_ascii=False, indent=2) + "\n"
            path.write_text(original, encoding="utf-8")
            self.assertEqual(load_ontology(path), ontology)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_rejects_head_and_concept_shape_errors(self):
        """Reject missing and additional fields at both schema levels."""
        for description, mutate, message in (
            ("head missing", lambda o: o.pop("stamm_version"), "fehlende Felder"),
            ("head extra", lambda o: o.update(extra=True), "unbekannte Felder"),
            ("concept missing", lambda o: o["concepts"][0].pop("related"), "fehlende Felder"),
            ("concept extra", lambda o: o["concepts"][0].update(herkunft="x"), "unbekannte Felder"),
        ):
            with self.subTest(description=description):
                ontology = self.ontology()
                mutate(ontology)
                self.refresh_hash(ontology)
                with self.assertRaisesRegex(OntologyError, message):
                    validate_ontology(ontology)

    def test_rejects_invalid_identifiers_type_status_and_dates(self):
        """Enforce stable identifiers and the closed value sets."""
        cases = (
            ("concept_id", "Kein Slug", "Begriff-Slug"),
            ("type", "neuer-typ", "type ist unbekannt"),
            ("status", "intern", "status ist unbekannt"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                ontology = self.ontology()
                ontology["concepts"][0][field] = value
                self.refresh_hash(ontology)
                with self.assertRaisesRegex(OntologyError, message):
                    validate_ontology(ontology)

        ontology = self.ontology()
        concept = self.concept(ontology, "test-dach-alt")
        concept["deprecated_at"] = None
        self.refresh_hash(ontology)
        with self.assertRaisesRegex(OntologyError, "nichtleerer Text"):
            validate_ontology(ontology)

        ontology = self.ontology()
        ontology["concepts"][0]["deprecated_at"] = "2026-11-22"
        self.refresh_hash(ontology)
        with self.assertRaisesRegex(OntologyError, "bei active null"):
            validate_ontology(ontology)

    def test_rejects_label_definition_and_list_errors(self):
        """Enforce normalized labels and deterministic lists."""
        mutations = (
            (lambda c: c.update(pref_label=" Dach Z"), "Rand-Leerzeichen"),
            (lambda c: c.update(pref_label="Da\u0308ch Z"), "NFC"),
            (lambda c: c.update(definition=""), "nichtleerer Text"),
            (lambda c: c.update(alt_labels=["Dach Z"]), "Label kommt mehrfach"),
            (lambda c: c.update(hidden_labels=["z", "a"]), "Unicode-Codepoints"),
            (lambda c: c.update(alt_labels=["a", "a"]), "Dubletten"),
        )
        for mutate, message in mutations:
            ontology = self.ontology()
            mutate(ontology["concepts"][0])
            self.refresh_hash(ontology)
            with self.subTest(message=message):
                with self.assertRaisesRegex(OntologyError, message):
                    validate_ontology(ontology)

    def test_rejects_reference_and_hierarchy_errors(self):
        """Reject invalid graph targets, inverses, families, and cycles."""
        cases = []

        def unknown(o):
            self.concept(o, "test-dach-z")["broader"] = ["nicht-da"]

        def self_reference(o):
            self.concept(o, "test-thema-x")["related"] = ["test-thema-x"]

        def dachbereich_parent(o):
            self.concept(o, "test-bereich-a")["broader"] = ["test-bereich-b"]

        def dach_without_parent(o):
            self.concept(o, "test-dach-z")["broader"] = []

        def foreign_family(o):
            self.concept(o, "test-dach-z")["broader"] = ["test-thema-x"]

        def missing_inverse(o):
            self.concept(o, "test-bereich-b")["narrower"] = []

        def asymmetric(o):
            self.concept(o, "test-thema-y")["related"] = []

        def cycle(o):
            self.concept(o, "test-dach-a")["broader"] = ["test-dach-z"]
            self.concept(o, "test-dach-a")["narrower"] = ["test-dach-z"]
            self.concept(o, "test-bereich-a")["narrower"] = ["test-dach-alt"]
            self.concept(o, "test-bereich-b")["narrower"] = []
            self.concept(o, "test-dach-z")["narrower"] = ["test-dach-a"]

        cases.extend((
            (unknown, "unbekannten Begriff"),
            (self_reference, "sich selbst"),
            (dachbereich_parent, "Dachbereich"),
            (dach_without_parent, "mindestens ein broader"),
            (foreign_family, "andere Begriffsfamilie"),
            (missing_inverse, "Umkehrung"),
            (asymmetric, "nicht symmetrisch"),
            (cycle, "Zyklus"),
        ))
        for mutate, message in cases:
            ontology = self.ontology()
            mutate(ontology)
            self.refresh_hash(ontology)
            with self.subTest(message=message):
                with self.assertRaisesRegex(OntologyError, message):
                    validate_ontology(ontology)

    def test_deep_acyclic_hierarchy_does_not_use_python_recursion(self):
        """Validate a hierarchy deeper than Python's normal recursion limit."""
        concepts = []
        count = 1200
        for index in range(count):
            concept_id = f"deep-{index:04d}"
            broader = [] if index == 0 else [f"deep-{index - 1:04d}"]
            narrower = [] if index == count - 1 else [f"deep-{index + 1:04d}"]
            concepts.append({
                "concept_id": concept_id,
                "type": "thema",
                "pref_label": f"Tiefe Ebene {index}",
                "alt_labels": [],
                "hidden_labels": [],
                "definition": None,
                "status": "active",
                "deprecated_at": None,
                "broader": broader,
                "narrower": narrower,
                "related": [],
            })
        ontology = self.ontology()
        ontology["concepts"] = concepts
        self.refresh_hash(ontology)

        self.assertEqual(validate_ontology(ontology), ontology)

    def test_rejects_duplicate_ids_unsorted_references_and_hash_mismatch(self):
        """Reject duplicate identities, unsorted edges, and stale hashes."""
        ontology = self.ontology()
        ontology["concepts"].append(copy.deepcopy(ontology["concepts"][0]))
        self.refresh_hash(ontology)
        with self.assertRaisesRegex(OntologyError, "Doppelte concept_id"):
            validate_ontology(ontology)

        ontology = self.ontology()
        self.concept(ontology, "test-dach-a")["broader"].reverse()
        self.refresh_hash(ontology)
        with self.assertRaisesRegex(OntologyError, "Unicode-Codepoints"):
            validate_ontology(ontology)

        ontology = self.ontology()
        ontology["concepts"][0]["pref_label"] = "Manipuliert"
        with self.assertRaisesRegex(OntologyError, "ontology_hash stimmt nicht"):
            validate_ontology(ontology)

    def test_strict_json_reader_rejects_all_lexical_boundaries(self):
        """Exercise every lexical rejection case in the shared vector."""
        invalid = (
            ('{"x": NaN}', "Nicht endliche"),
            ('{"x": 1.0}', "Gleitkommazahl"),
            ('{"x": 9007199254740992}', "außerhalb"),
            ('{"x": "\\ud800"}', "Surrogat"),
            ('{"stämm_version": 1}', "nicht ASCII"),
            ('{"x": 1, "x": 2}', "Doppelter JSON-Schlüssel"),
        )
        for text, message in invalid:
            with self.subTest(text=text):
                with self.assertRaisesRegex(OntologyError, message):
                    parse_ontology_json(text)

    def test_rejects_invalid_timestamps_and_hash_shape(self):
        """Require exact UTC timestamps, matching revisions, and SHA-256."""
        for timestamp in (
            "2026-11-22T12:00:00+00:00",
            "2026-11-22T12:00:00z",
            "2026-02-30T12:00:00Z",
        ):
            ontology = self.ontology()
            ontology["exported_at"] = timestamp
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(OntologyError, "UTC-Zeitpunkt|entsprechen"):
                    validate_ontology(ontology)

        ontology = self.ontology()
        ontology["ontology_hash"] = "sha256:abc"
        with self.assertRaisesRegex(OntologyError, "64 Hexzeichen"):
            validate_ontology(ontology)

        ontology = self.ontology()
        ontology["ontology_revision"] = "2026-11-22T12:00:01Z"
        with self.assertRaisesRegex(OntologyError, "exported_at entsprechen"):
            validate_ontology(ontology)

    def test_production_identity_is_checked_separately(self):
        """Keep synthetic vector identifiers separate from production identity."""
        test_vector = self.ontology()
        validate_ontology(test_vector)
        with self.assertRaisesRegex(OntologyError, "Produktivlieferung"):
            validate_production_identity(test_vector)

        production = self.ontology()
        production["ontology_id"] = "z-ontologie"
        production["source"] = {
            "application": "Atlas", "instance_id": "atlas-primary"
        }
        validate_production_identity(production)

    def test_catalog_v2_subset_check(self):
        """Require all catalog-v2 concept references in the ontology."""
        ontology = self.ontology()
        projects = [{
            "project_id": "test-projekt", "name": "Testprojekt",
            "status": "active", "category": "Testbereich A", "aliases": [],
            "relations": [],
            "concept_relations": [
                {"relation": "hatDach", "concept_id": "test-bereich-a"}
            ],
            "routing": {"enabled": True, "terms": [], "exact_terms": []},
        }]
        catalog = {
            "schema_version": 2, "export_type": "z_system_project_catalog",
            "catalog_id": "test", "catalog_revision": "2026-11-22T12:00:00Z",
            "exported_at": "2026-11-22T12:00:00Z",
            "source": {"application": "Test", "instance_id": "test"},
            "catalog_hash": project_catalog_hash(projects), "projects": projects,
        }
        validate_catalog_concepts(catalog, ontology)
        catalog["projects"][0]["concept_relations"][0]["concept_id"] = "fehlt"
        with self.assertRaisesRegex(OntologyError, "unbekannte Ontologiebegriffe"):
            validate_catalog_concepts(catalog, ontology)


if __name__ == "__main__":
    unittest.main()
