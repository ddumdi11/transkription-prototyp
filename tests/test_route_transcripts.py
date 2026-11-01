import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from inbox_watcher import open_state
from publish_transcripts import prepare_publish_state
from route_transcripts import (accepted_occurrences, load_config,
                               log_content_evidence, plan_one,
                               published_transcripts, plan_published,
                               prompt_leak_shadow_diagnostics,
                               recording_number, matching_terms)


class RouteTranscriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = open_state(self.root / "state.sqlite3")
        prepare_publish_state(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_published(self, text):
        transcript = self.root / "Aufnahme #565__drive-id.md"
        transcript.write_text(text, encoding="utf-8")
        self.db.execute(
            """INSERT INTO files
               (drive_id,path,size,content_hash,hash_type,mod_time,first_seen,last_seen,
                stable_observations,status,duplicate_of)
               VALUES ('drive-id', 'Aufnahme #565.wav', 100, 'hash', 'sha256', NULL,
                       1000, 1010, 2, 'READY', NULL)"""
        )
        self.db.execute(
            """INSERT INTO transcription_jobs
               (drive_id,status,attempts,local_audio,transcript_path,last_error,updated_at)
               VALUES ('drive-id', 'DONE', 1, 'audio.wav', ?, NULL, 1020)""",
            (str(transcript),),
        )
        self.db.execute(
            """INSERT INTO published_transcripts
               (drive_id,local_path,local_hash,size,remote_path,remote_id,published_at)
               VALUES ('drive-id', ?, 'transcript-hash', 100,
                       'Aufnahme #565__drive-id.md', 'remote-id', 1030)""",
            (str(transcript),),
        )
        self.db.commit()

    def test_plan_combines_default_active_content_and_topics_without_duplicates(self):
        self.add_published("Der Upload wurde ins Drive geladen. Termin bei der IHK.")
        config = {
            "default_projects": ["Z04", "Selbstregulation"],
            "active_projects": ["Watcher", "Selbständigkeit"],
            "project_rules": [
                {"project": "Watcher", "match_any": ["Upload"]},
                {"project": "Selbständigkeit", "match_any": ["IHK"]},
            ],
            "topic_rules": {
                "workflow-test": ["Drive"],
                "behoerden-ihk": ["IHK"],
                "nicht-passend": ["Physiotherapie"],
            },
        }
        row = published_transcripts(self.db)[0]
        plan = plan_one(row, config)

        self.assertEqual([project["name"] for project in plan["projects"]],
                         ["Z04", "Selbstregulation", "Watcher", "Selbständigkeit"])
        self.assertEqual(plan["projects"][2]["reasons"],
                         ["active_context", "content:Upload"])
        self.assertEqual(plan["topics"], ["workflow-test", "behoerden-ihk"])
        self.assertEqual(plan["recording_number"], 565)

    def test_only_published_transcripts_are_candidates(self):
        self.assertEqual(published_transcripts(self.db), [])
        self.add_published("Test")
        self.assertEqual([row["drive_id"] for row in published_transcripts(self.db)],
                         ["drive-id"])

    def test_plan_published_selects_exact_drive_id(self):
        self.add_published("KI-Lotse beim Kunden")
        config = {
            "default_projects": [], "active_projects": [],
            "project_rules": [
                {"project": "IT-Dienstleistungen Probephase", "match_any": ["KI-Lotse"]}
            ],
            "topic_rules": {},
        }
        plan = plan_published(self.db, "drive-id", config)
        self.assertEqual(plan["recording_number"], 565)
        self.assertEqual(plan["projects"][0]["name"], "IT-Dienstleistungen Probephase")

    def test_recording_number_handles_unknown_names(self):
        self.assertEqual(recording_number("Aufnahme #570.wav"), 570)
        self.assertIsNone(recording_number("Meeting.wav"))

    def test_matching_terms_requires_a_safe_word_start(self):
        self.assertEqual(matching_terms("eine Begründung", ["Gründung"]), [])
        self.assertEqual(matching_terms("Kundenberater", ["Kunde"]), ["Kunde"])
        self.assertEqual(matching_terms("Kino und KI", ["KI"]), ["KI"])
        self.assertEqual(matching_terms("Driver", ["Drive"]), [])

    def test_matching_terms_can_require_an_exact_word_end(self):
        self.assertEqual(
            matching_terms(
                "Test Driven Development", ["Drive"], exact_terms=["Drive"]
            ),
            [],
        )
        self.assertEqual(
            matching_terms("Google Drive", ["Drive"], exact_terms=["Drive"]),
            ["Drive"],
        )
        self.assertEqual(
            matching_terms("Drive-Ordner", ["Drive"], exact_terms=["Drive"]),
            ["Drive"],
        )

    def test_glossary_routes_it_lotse_variants_to_probephase_project(self):
        self.add_published("Platzhalter")
        row = published_transcripts(self.db)[0]
        transcript = Path(row["transcript_path"])
        config_path = self.root / "routing.json"
        config_path.write_text(json.dumps({
            "default_projects": [],
            "active_projects": [],
            "project_rules": [],
            "topic_rules": {},
        }), encoding="utf-8")

        for variant in (
            "IT-Dienstleistungen Probephase", "KI-/IT-Lotse", "KI-Lotse",
            "IT-Lotse", "Digitaler Check",
        ):
            with self.subTest(variant=variant):
                transcript.write_text(
                    f"Heute arbeite ich als {variant} beim Kunden.", encoding="utf-8"
                )
                projects = [
                    project["name"]
                    for project in plan_one(row, load_config(config_path))["projects"]
                ]
                self.assertEqual(projects, ["it-ki-beratung"])

    def test_example_config_routes_transkriptionspipeline_compound(self):
        # Routing-Audit #909–#914, P3: "Pipeline" matches only at a word
        # start, so the compound "Transkriptionspipeline" needs its own term.
        example = Path(__file__).resolve().parents[1] / "routing.example.json"
        config = load_config(example)
        self.add_published("Platzhalter")
        row = published_transcripts(self.db)[0]
        transcript = Path(row["transcript_path"])

        def content_routing(text):
            transcript.write_text(text, encoding="utf-8")
            plan = plan_one(row, config)
            content = {
                project["name"]: reason
                for project in plan["projects"]
                for reason in project["reasons"]
                if reason.startswith("content:")
            }
            return content, plan["topics"]

        cases = [
            ("ob die Transkriptionspipeline aktiv ist",
             "content:Transkriptionspipeline", True),
            ("beide Transkriptionspipelines laufen",
             "content:Transkriptionspipeline", True),
            ("die Transkription Pipeline läuft", "content:Pipeline", True),
            ("die Ölpipeline in Russland", None, False),
            ("die Transkriptionspipe ist verstopft", None, False),
        ]
        for text, reason, routed in cases:
            with self.subTest(text=text):
                content, topics = content_routing(text)
                self.assertEqual(
                    content.get("projektverwaltung-steuerung"), reason
                )
                self.assertEqual(
                    "transkriptionsworkflow-test" in topics, routed
                )

    def test_prompt_leak_shadow_reports_without_changing_routing(self):
        self.add_published(
            "Fachbegriffe: AudioRec, Qwen, IHK, Traktat."
        )
        config = {
            "default_projects": ["always"],
            "active_projects": [],
            "exact_terms": [],
            "project_rules": [
                {"project": "consulting", "match_any": ["IHK"]},
            ],
            "topic_rules": {"behoerden-ihk": ["IHK"]},
            "_shadow_prompt_leak_hotwords": [
                "AudioRec", "Qwen", "IHK", "Traktat"
            ],
        }

        plan = plan_one(published_transcripts(self.db)[0], config)

        self.assertEqual(
            [project["name"] for project in plan["projects"]],
            ["always", "consulting"],
        )
        self.assertEqual(plan["topics"], ["behoerden-ihk"])
        findings = plan["diagnostics"]["prompt_leak_shadow"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0]["glossary_terms"],
            ["AudioRec", "Qwen", "IHK", "Traktat"],
        )
        self.assertEqual(
            findings[0]["project_hits"],
            [{"project": "consulting", "terms": ["IHK"]}],
        )
        self.assertEqual(
            findings[0]["topic_hits"],
            [{"topic": "behoerden-ihk", "terms": ["IHK"]}],
        )

    def test_prompt_leak_shadow_ignores_known_counterexamples(self):
        config = {
            "project_rules": [],
            "topic_rules": {},
            "exact_terms": [],
            "_shadow_prompt_leak_hotwords": [
                "IHK", "Job Queue", "Checkpointing", "Idempotenz",
                "IT-Lotse", "KI-Lotse", "Vier-Ohren-Modell", "Traktat",
            ],
        }
        texts = (
            "im IHK zeigen, Kunden zeigen, auf LinkedIn",
            "Job Queue, Checkpointing und Idempotenz drei wichtige Dinge",
            "IT-Lotse bzw. KI-Lotse",
            "Traktat, Traktat, Traktat, Traktat",
            "Vier-Ohren-Modell, Kauderwelsch, IHK, weitere Füllwörter",
        )

        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(
                    prompt_leak_shadow_diagnostics(text, config), []
                )

    def test_load_config_rejects_malformed_nested_rules(self):
        base = {
            "default_projects": ["Z04"],
            "active_projects": [],
            "exact_terms": ["Drive"],
            "project_rules": [{"project": "Watcher", "match_any": ["Upload"]}],
            "topic_rules": {"workflow": ["Drive"]},
        }
        malformed = [
            {**base, "project_rules": ["not-an-object"]},
            {**base, "project_rules": [{"match_any": ["Upload"]}]},
            {**base, "project_rules": [{"project": "Watcher", "match_any": "Upload"}]},
            {**base, "project_rules": [{"project": "Watcher", "match_any": []}]},
            {**base, "project_rules": [{"project": "Watcher", "match_any": ["  "]}]},
            {**base, "topic_rules": {"workflow": "Drive"}},
            {**base, "topic_rules": {"workflow": []}},
            {**base, "topic_rules": {"workflow": ["\t"]}},
            {**base, "exact_terms": "Drive"},
            {**base, "exact_terms": [" "]},
        ]
        for index, config in enumerate(malformed):
            with self.subTest(index=index):
                path = self.root / f"malformed-{index}.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)



def legacy_matching_terms(text, terms, exact_terms=None):
    """Verbatim copy of matching_terms before the shared occurrence generator.

    Serves as an oracle: the refactoring must not change routing matches.
    """
    folded = text.casefold()
    folded_exact_terms = {
        term.casefold() for term in (exact_terms or [])
    }
    matched = []
    for term in terms:
        folded_term = term.casefold()
        pattern = rf"(?<!\w){re.escape(folded_term)}"
        for occurrence in re.finditer(pattern, folded):
            tail = folded[occurrence.end():]
            if not tail or not (tail[0].isalnum() or tail[0] == "_"):
                matched.append(term)
                break
            if folded_term in folded_exact_terms:
                continue
            is_short_acronym = (
                term.isupper() and len(re.sub(r"\W", "", term)) <= 3
            )
            if not is_short_acronym and tail.startswith(("e", "en", "er", "n", "s")):
                matched.append(term)
                break
    return matched


SUPPORT_TERMS = [
    "KI", "IT", "Beratung", "Dienstleistung", "Automatisierung", "Akquise",
    "Auftrag", "Rechnung", "Stundensatz", "Lotse",
]
EVIDENCE_BLOCK = {
    "support_window_chars": 250,
    "rules": [
        {"target_type": "project", "target": "consulting",
         "weak_terms": ["Kunde", "Angebot"], "support_any": SUPPORT_TERMS},
        {"target_type": "topic", "target": "angebote",
         "weak_terms": ["Kunde", "Angebot", "Preis"],
         "support_any": SUPPORT_TERMS},
    ],
}
BASE_CONFIG = {
    "default_projects": ["workshop", "basis"],
    "active_projects": ["consulting"],
    "exact_terms": ["Drive"],
    "project_rules": [
        {"project": "consulting",
         "match_any": ["Selbstständigkeit", "Kunde", "Angebot"]},
        {"project": "workshop", "match_any": ["Pipeline", "Drive"]},
    ],
    "topic_rules": {
        "angebote": ["Geschäftsmodell", "Kunde", "Angebot", "Preis"],
        "workflow": ["Pipeline"],
    },
}


class MatcherEquivalenceTest(unittest.TestCase):
    TEXTS = [
        "", "eine Begründung", "Gründung der Firma", "Kundenberater",
        "Kino und KI", "KI-Angebote", "Kiel, Kind, Kiste", "Driver",
        "Google Drive", "Drive-Ordner", "Test Driven Development",
        "IT-Lotse und KI-/IT-Lotse", "Item, ITler, it is", "IT.",
        "Angebote, angeboten, Angebots, Angebotes", "Straße und STRASSE",
        "_Kunde Kunde_ Kunde2 Kunden-Termin", "Ölpipeline, Pipeline-Status",
        "Preisschild, Preise, Preis.", "kunde KUNDE Kunde", "Lotsen Lotsin",
    ]
    TERMS = [
        "Gründung", "Kunde", "KI", "Drive", "IT", "Lotse", "Angebot",
        "Pipeline", "Preis", "Straße", "KI-/IT-Lotse", "IT-Lotse",
    ]

    def test_matching_terms_equals_legacy_implementation(self):
        for exact_terms in ([], ["Drive"], ["Kunde", "Preis"]):
            for text in self.TEXTS:
                with self.subTest(text=text, exact_terms=exact_terms):
                    self.assertEqual(
                        matching_terms(text, self.TERMS, exact_terms),
                        legacy_matching_terms(text, self.TERMS, exact_terms),
                    )
                    for term in self.TERMS:
                        found = list(accepted_occurrences(
                            text.casefold(), term,
                            {item.casefold() for item in exact_terms},
                        ))
                        self.assertEqual(
                            bool(found),
                            legacy_matching_terms(text, [term], exact_terms)
                            == [term],
                        )

    def test_duplicate_terms_keep_legacy_behaviour(self):
        self.assertEqual(matching_terms("Kunde", ["Kunde", "Kunde"]),
                         legacy_matching_terms("Kunde", ["Kunde", "Kunde"]))

    def test_accepted_occurrences_counts_every_accepted_position(self):
        def count(text, term, exact=()):
            return len(list(accepted_occurrences(
                text.casefold(), term, {item.casefold() for item in exact}
            )))

        self.assertEqual(count("Ein Kunde, zwei Kunden, die Kundennummer.",
                               "Kunde"), 3)
        self.assertEqual(count("Kino, Kind, KI und KI-Lotse", "KI"), 2)
        self.assertEqual(count("Drive, Driver, Drive-Ordner, Driven", "Drive",
                               ["Drive"]), 2)
        self.assertEqual(count("Begründung und Gründung", "Gründung"), 1)


class ContentEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, block=EVIDENCE_BLOCK, base=BASE_CONFIG, name="routing.json"):
        data = copy.deepcopy(base)
        if block is not None:
            data["content_evidence"] = copy.deepcopy(block)
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_config(path)

    def plan(self, text, config):
        transcript = self.root / "transcript.md"
        transcript.write_text(text, encoding="utf-8")
        row = {
            "drive_id": "drive-id",
            "audio_path": "Aufnahme.wav",
            "transcript_path": str(transcript),
            "remote_path": "transcript.md",
        }
        plan = plan_one(row, config)
        if "content_evidence" in plan:
            self.assert_invariants(plan)
        return plan

    @staticmethod
    def items(plan):
        return {
            (item["target_type"], item["target"]): item
            for item in plan["content_evidence"]["items"]
        }

    def assert_invariants(self, plan):
        evidence = plan["content_evidence"]
        self.assertEqual(
            set(evidence), {"schema_version", "support_window_chars", "items"}
        )
        self.assertEqual(evidence["schema_version"], 1)
        content_projects = [
            project["name"] for project in plan["projects"]
            if any(reason.startswith("content:") for reason in project["reasons"])
        ]
        self.assertEqual(
            [(item["target_type"], item["target"]) for item in evidence["items"]],
            [("project", name) for name in content_projects]
            + [("topic", topic) for topic in plan["topics"]],
        )
        expected_basis = {
            "strong": "specific_term",
            "supported": "weak_terms_with_local_support",
            "weak": "weak_terms_only",
        }
        for item in evidence["items"]:
            self.assertEqual(set(item), {
                "target_type", "target", "strength", "basis", "matched_terms",
                "weak_terms", "supporting_terms", "occurrences",
            })
            self.assertEqual(item["basis"], expected_basis[item["strength"]])
            self.assertTrue(item["matched_terms"])
            self.assertEqual(
                item["weak_terms"],
                [term for term in item["matched_terms"]
                 if term in item["weak_terms"]],
            )
            self.assertEqual(list(item["occurrences"]), item["matched_terms"])
            self.assertTrue(all(
                isinstance(count, int) and count >= 1
                for count in item["occurrences"].values()
            ))
            all_weak = item["weak_terms"] == item["matched_terms"]
            self.assertEqual(item["strength"] == "strong", not all_weak)
            self.assertEqual(
                bool(item["supporting_terms"]), item["strength"] == "supported"
            )

    def test_missing_block_keeps_previous_plan_shape(self):
        plan = self.plan("Ein Angebot für den Kunden.", self.config(block=None))
        self.assertNotIn("content_evidence", plan)
        self.assertEqual(set(plan), {
            "drive_id", "audio_path", "transcript_path", "remote_path",
            "recording_number", "projects", "topics", "diagnostics",
        })

    def test_block_never_changes_routing_projects_topics_or_diagnostics(self):
        with_block = self.config()
        without_block = self.config(block=None, name="plain.json")
        texts = [
            "Ein Angebot.",
            "KI-Angebote zum Preis.",
            "Selbstständigkeit, Kunde und Pipeline.",
            "KI, IT, Beratung, Rechnung, Auftrag, Lotse und Akquise.",
            "Fachbegriffe: KI, IT-Lotse, KI-Lotse, Claude, AudioRec.",
            "",
        ]
        for text in texts:
            with self.subTest(text=text):
                plan = self.plan(text, with_block)
                plain = self.plan(text, without_block)
                self.assertIn("content_evidence", plan)
                plan.pop("content_evidence")
                self.assertEqual(plan, plain)

    def test_support_terms_alone_create_no_target_and_no_evidence(self):
        plan = self.plan(
            "KI, IT, Beratung, Dienstleistung, Automatisierung, Akquise, "
            "Auftrag, Rechnung, Stundensatz und Lotse.",
            self.config(),
        )
        self.assertEqual(
            [(project["name"], project["reasons"]) for project in plan["projects"]],
            [("workshop", ["default"]), ("basis", ["default"]),
             ("consulting", ["active_context"])],
        )
        self.assertEqual(plan["topics"], [])
        self.assertEqual(plan["content_evidence"]["items"], [])

    def test_delivery_rules_are_never_evidence(self):
        plan = self.plan("Ein Kunde.", self.config())
        projects = {project["name"]: project["reasons"]
                    for project in plan["projects"]}
        self.assertEqual(projects["consulting"],
                         ["active_context", "content:Kunde"])
        self.assertEqual(projects["workshop"], ["default"])
        items = self.items(plan)
        self.assertNotIn(("project", "workshop"), items)
        self.assertNotIn(("project", "basis"), items)
        self.assertEqual(items[("project", "consulting")]["strength"], "weak")

    def test_weak_terms_only_without_support_are_weak(self):
        items = self.items(self.plan("Das Angebot steht.", self.config()))
        self.assertEqual(items[("project", "consulting")], {
            "target_type": "project", "target": "consulting",
            "strength": "weak", "basis": "weak_terms_only",
            "matched_terms": ["Angebot"], "weak_terms": ["Angebot"],
            "supporting_terms": [], "occurrences": {"Angebot": 1},
        })
        self.assertEqual(items[("topic", "angebote")]["strength"], "weak")

    def test_local_support_upgrades_to_supported_only(self):
        items = self.items(self.plan("KI-Angebote zum Preis.", self.config()))
        self.assertEqual(items[("project", "consulting")], {
            "target_type": "project", "target": "consulting",
            "strength": "supported", "basis": "weak_terms_with_local_support",
            "matched_terms": ["Angebot"], "weak_terms": ["Angebot"],
            "supporting_terms": ["KI"], "occurrences": {"Angebot": 1},
        })
        topic = items[("topic", "angebote")]
        self.assertEqual(topic["strength"], "supported")
        self.assertEqual(topic["matched_terms"], ["Angebot", "Preis"])
        self.assertEqual(topic["supporting_terms"], ["KI"])

    def test_supporting_terms_follow_configuration_order(self):
        items = self.items(self.plan(
            "Rechnung, Lotse und Beratung zum Angebot.", self.config()
        ))
        self.assertEqual(items[("project", "consulting")]["supporting_terms"],
                         ["Beratung", "Rechnung", "Lotse"])

    def test_support_window_boundaries(self):
        config = self.config()
        cases = [
            ("Angebot" + " " * 250 + "KI", "supported"),
            ("Angebot" + " " * 251 + "KI", "weak"),
            ("KI" + " " * 248 + "Angebot", "supported"),
            ("KI" + " " * 249 + "Angebot", "weak"),
        ]
        for text, strength in cases:
            with self.subTest(length=len(text)):
                items = self.items(self.plan(text, config))
                self.assertEqual(items[("project", "consulting")]["strength"],
                                 strength)

    def test_support_window_uses_original_offsets_across_casefold_expansion(self):
        config = self.config()
        cases = [
            ("Angebot.ß" + " " * 248 + "KI", "supported"),
            ("Angebot.ß" + " " * 249 + "KI", "weak"),
        ]
        for text, strength in cases:
            with self.subTest(strength=strength):
                items = self.items(self.plan(text, config))
                self.assertEqual(
                    items[("project", "consulting")]["strength"], strength
                )

    def test_inflection_suffix_counts_toward_support_window(self):
        config = self.config()
        for weak_term in ("Kunden", "Angebot"):
            with self.subTest(weak_term=weak_term):
                items = self.items(self.plan(
                    weak_term + " " * 250 + "KI", config
                ))
                self.assertEqual(
                    items[("project", "consulting")]["strength"],
                    "supported",
                )

    def test_support_lookup_keeps_overlapping_covering_span(self):
        block = copy.deepcopy(EVIDENCE_BLOCK)
        block["support_window_chars"] = 1
        block["rules"][0]["weak_terms"] = [
            "Kunde", "Kunde Preis Paket", "Preis"
        ]
        base = copy.deepcopy(BASE_CONFIG)
        base["project_rules"][0]["match_any"].extend(
            ["Kunde Preis Paket", "Preis"]
        )
        config = self.config(block=block, base=base, name="overlap.json")
        items = self.items(self.plan("Kunde Preis Paket KI", config))
        self.assertEqual(
            items[("project", "consulting")]["strength"], "supported"
        )

    def test_wider_window_changes_only_the_classification(self):
        text = "Angebot" + " " * 300 + "KI"
        narrow = self.plan(text, self.config())
        wide_block = {**copy.deepcopy(EVIDENCE_BLOCK), "support_window_chars": 1000}
        wide = self.plan(text, self.config(block=wide_block, name="wide.json"))
        self.assertEqual(self.items(narrow)[("project", "consulting")]["strength"],
                         "weak")
        self.assertEqual(self.items(wide)[("project", "consulting")]["strength"],
                         "supported")
        self.assertEqual(wide["content_evidence"]["support_window_chars"], 1000)
        for plan in (narrow, wide):
            plan.pop("content_evidence")
        self.assertEqual(narrow, wide)

    def test_specific_term_is_strong_and_ignores_support(self):
        items = self.items(self.plan(
            "KI für die Selbstständigkeit und zwei Kunden.", self.config()
        ))
        self.assertEqual(items[("project", "consulting")], {
            "target_type": "project", "target": "consulting",
            "strength": "strong", "basis": "specific_term",
            "matched_terms": ["Selbstständigkeit", "Kunde"],
            "weak_terms": ["Kunde"], "supporting_terms": [],
            "occurrences": {"Selbstständigkeit": 1, "Kunde": 1},
        })
        self.assertEqual(items[("topic", "angebote")]["strength"], "supported")

    def test_targets_without_weak_terms_are_strong(self):
        items = self.items(self.plan("Die Pipeline läuft.", self.config()))
        self.assertEqual(items[("project", "workshop")]["strength"], "strong")
        self.assertEqual(items[("topic", "workflow")]["weak_terms"], [])
        empty = self.config(
            block={"support_window_chars": 250, "rules": []}, name="empty.json"
        )
        items = self.items(self.plan("Ein Angebot.", empty))
        self.assertEqual(
            {key: item["strength"] for key, item in items.items()},
            {("project", "consulting"): "strong", ("topic", "angebote"): "strong"},
        )

    def test_empty_support_list_keeps_weak(self):
        block = copy.deepcopy(EVIDENCE_BLOCK)
        block["rules"][0]["support_any"] = []
        items = self.items(self.plan(
            "KI-Angebote.", self.config(block=block, name="nosupport.json")
        ))
        self.assertEqual(items[("project", "consulting")]["strength"], "weak")
        self.assertEqual(items[("topic", "angebote")]["strength"], "supported")

    def test_config_and_glossary_rules_form_one_item(self):
        base = copy.deepcopy(BASE_CONFIG)
        base["project_rules"].append(
            {"project": "it-ki-beratung", "match_any": ["Kunde", "Angebot"]}
        )
        block = copy.deepcopy(EVIDENCE_BLOCK)
        block["rules"].append({
            "target_type": "project", "target": "it-ki-beratung",
            "weak_terms": ["Kunde", "Angebot"], "support_any": SUPPORT_TERMS,
        })
        config = self.config(block=block, base=base, name="glossary.json")

        plan = self.plan("Ein Kunde fragt nach dem KI-Lotse.", config)
        reasons = {project["name"]: project["reasons"]
                   for project in plan["projects"]}
        self.assertEqual(reasons["it-ki-beratung"],
                         ["content:Kunde", "content:KI-Lotse"])
        item = self.items(plan)[("project", "it-ki-beratung")]
        self.assertEqual(item["matched_terms"], ["Kunde", "KI-Lotse"])
        self.assertEqual(item["strength"], "strong")

    def test_inflected_and_compound_forms_count_like_routing(self):
        items = self.items(self.plan(
            "Das wurde angeboten. Ein Kunde, zwei Kunden, die Kundennummer.",
            self.config(),
        ))
        item = items[("project", "consulting")]
        self.assertEqual(item["strength"], "weak")
        self.assertEqual(item["occurrences"], {"Kunde": 3, "Angebot": 1})

    def test_casefolded_weak_terms_use_rule_spelling(self):
        block = copy.deepcopy(EVIDENCE_BLOCK)
        block["rules"][0]["weak_terms"] = ["kunde", "ANGEBOT"]
        items = self.items(self.plan(
            "Kunde und Angebot.", self.config(block=block, name="fold.json")
        ))
        item = items[("project", "consulting")]
        self.assertEqual(item["weak_terms"], ["Kunde", "Angebot"])
        self.assertEqual(item["strength"], "weak")

    def test_item_order_follows_plan_order(self):
        plan = self.plan(
            "Geschäftsmodell, Pipeline und Kunde.", self.config()
        )
        self.assertEqual(
            [project["name"] for project in plan["projects"]],
            ["workshop", "basis", "consulting"],
        )
        self.assertEqual(
            [(item["target_type"], item["target"])
             for item in plan["content_evidence"]["items"]],
            [("project", "workshop"), ("project", "consulting"),
             ("topic", "angebote"), ("topic", "workflow")],
        )

    def test_output_is_deterministic_json(self):
        config = self.config()
        text = "KI-Angebote, Kunden, Preis und Pipeline."
        first = json.dumps(self.plan(text, config), ensure_ascii=False,
                           sort_keys=True, allow_nan=False)
        second = json.dumps(self.plan(text, self.config(name="again.json")),
                            ensure_ascii=False, sort_keys=True, allow_nan=False)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["content_evidence"]["items"],
                         self.plan(text, config)["content_evidence"]["items"])

    def test_evidence_and_log_contain_no_transcript_text(self):
        marker = "Zebrafinkenschwarm"
        plan = self.plan(
            f"Der {marker} wollte als Kundennummer-Fall ein Angebot.",
            self.config(),
        )
        serialized = json.dumps(plan["content_evidence"], ensure_ascii=False)
        self.assertNotIn(marker.casefold(), serialized.casefold())
        self.assertNotIn("kundennummer", serialized.casefold())

        logger = Mock()
        log_content_evidence(plan, logger)
        logged = " ".join(
            str(arg) for call in logger.info.call_args_list for arg in call.args
        ).casefold()
        self.assertNotIn(marker.casefold(), logged)
        self.assertNotIn("kundennummer", logged)
        self.assertNotIn("drive-id", logged)

    def test_log_reports_only_weak_targets_with_term_names(self):
        plan = self.plan("KI-Angebote. " + " " * 300 + "Kunde.", self.config())
        plan["content_evidence"]["items"].append({
            "target_type": "project", "target": "workshop",
            "strength": "strong", "basis": "specific_term",
            "matched_terms": ["Pipeline"], "weak_terms": [],
            "supporting_terms": [], "occurrences": {"Pipeline": 1},
        })
        plan["content_evidence"]["items"].append({
            "target_type": "topic", "target": "angebote", "strength": "weak",
            "basis": "weak_terms_only", "matched_terms": ["Kunde", "Preis"],
            "weak_terms": ["Kunde", "Preis"], "supporting_terms": [],
            "occurrences": {"Kunde": 1, "Preis": 1},
        })
        logger = Mock()
        log_content_evidence(plan, logger)
        self.assertEqual(
            [call.args for call in logger.info.call_args_list],
            [("Routing EVIDENCE target_type=%s target=%s strength=%s terms=%s",
              "topic", "angebote", "weak", "Kunde|Preis")],
        )
        logger.warning.assert_not_called()

    def test_log_without_evidence_is_silent(self):
        logger = Mock()
        log_content_evidence({"projects": [], "topics": []}, logger)
        logger.info.assert_not_called()

    def test_example_config_block_is_valid(self):
        example = Path(__file__).resolve().parents[1] / "routing.example.json"
        config = load_config(example)
        rules = config["content_evidence"]["rules"]
        self.assertEqual(config["content_evidence"]["support_window_chars"], 250)
        self.assertEqual(
            [(rule["target_type"], rule["target"], rule["weak_terms"])
             for rule in rules],
            [("project", "it-ki-beratung", ["Kunde", "Angebot"]),
             ("topic", "geschaeftsmodell-angebote", ["Kunde", "Angebot", "Preis"])],
        )
        for rule in rules:
            self.assertEqual(rule["support_any"], SUPPORT_TERMS)

    def test_glossary_alias_is_a_valid_weak_term(self):
        block = {"support_window_chars": 250, "rules": [{
            "target_type": "project", "target": "it-ki-beratung",
            "weak_terms": ["KI-Lotse"], "support_any": [],
        }]}
        config = self.config(block=block, name="alias.json")
        item = self.items(self.plan("Als KI-Lotse unterwegs.", config))[
            ("project", "it-ki-beratung")
        ]
        self.assertEqual(item["strength"], "weak")

    def test_malformed_evidence_config_fails_closed(self):
        def rule(**changes):
            return {**copy.deepcopy(EVIDENCE_BLOCK["rules"][0]), **changes}

        def block(**changes):
            return {**copy.deepcopy(EVIDENCE_BLOCK), **changes}

        malformed = {
            "not-object": "Kunde",
            "list-block": [],
            "unknown-block-key": block(window=250),
            "missing-window": {"rules": []},
            "missing-rules": {"support_window_chars": 250},
            "zero-window": block(support_window_chars=0),
            "negative-window": block(support_window_chars=-5),
            "bool-window": block(support_window_chars=True),
            "float-window": block(support_window_chars=250.0),
            "text-window": block(support_window_chars="250"),
            "rules-object": block(rules={}),
            "rule-text": block(rules=["consulting"]),
            "rule-missing-key": block(rules=[{
                key: value for key, value in rule().items() if key != "support_any"
            }]),
            "rule-extra-key": block(rules=[rule(strength="weak")]),
            "bad-target-type": block(rules=[rule(target_type="projekt")]),
            "target-not-text": block(rules=[rule(target=["consulting"])]),
            "empty-target": block(rules=[rule(target=" ")]),
            "unknown-project": block(rules=[rule(target="unbekannt")]),
            "project-without-rule": block(rules=[rule(target="basis")]),
            "topic-as-project": block(rules=[rule(target="angebote")]),
            "unknown-topic": block(rules=[rule(
                target_type="topic", target="consulting"
            )]),
            "duplicate-target": block(rules=[rule(), rule()]),
            "weak-not-list": block(rules=[rule(weak_terms="Kunde")]),
            "weak-empty": block(rules=[rule(weak_terms=[])]),
            "weak-blank": block(rules=[rule(weak_terms=["Kunde", " "])]),
            "weak-not-rule-term": block(rules=[rule(weak_terms=["Rechnung"])]),
            "weak-other-target": block(rules=[rule(weak_terms=["Pipeline"])]),
            "weak-duplicate": block(rules=[rule(weak_terms=["Kunde", "kunde"])]),
            "support-not-list": block(rules=[rule(support_any="KI")]),
            "support-blank": block(rules=[rule(support_any=["KI", ""])]),
            "support-not-text": block(rules=[rule(support_any=["KI", 1])]),
            "support-duplicate": block(rules=[rule(support_any=["KI", "ki"])]),
            "support-overlaps-weak": block(rules=[rule(support_any=["KI", "kunde"])]),
        }
        for name, malformed_block in malformed.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.config(block=malformed_block, name=f"{name}.json")


if __name__ == "__main__":
    unittest.main()
