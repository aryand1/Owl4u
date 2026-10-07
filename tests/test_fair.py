from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "batch"))

import owl4_fair as fair  # noqa: E402


def all_checks(result: dict) -> list[dict]:
    return [
        check
        for principle in result["principles"].values()
        for criterion in principle["subprinciples"].values()
        for check in criterion["checks"]
    ]


class FairAssessmentTests(unittest.TestCase):
    def test_profile_has_61_checks_15_subprinciples_and_478_credits(self):
        result = fair.assess()
        self.assertEqual(61, len(all_checks(result)))
        self.assertEqual(15, sum(len(p["subprinciples"]) for p in result["principles"].values()))
        self.assertEqual(478.0, result["max_credits"])
        self.assertEqual(478.0, sum(p["max_credits"] for p in result["principles"].values()))

    def test_richer_metadata_scores_higher_and_preserves_evidence(self):
        sparse = fair.assess(
            {"acronym": "EX"},
            {"submission_id": 1},
            {},
            {"catalog_status": "ok", "download_status": "ok"},
        )
        rich = fair.assess(
            {"acronym": "EX", "name": "Example", "viewingRestriction": "public",
             "group": ["OBO-FOUNDRY"]},
            {"submission_id": 3, "URI": "https://example.org/ontology",
             "versionIRI": "https://example.org/ontology/3", "identifier": ["https://doi.org/10.1234/example"],
             "hasOntologyLanguage": "OWL", "description": "Example ontology", "homepage": "https://example.org",
             "hasLicense": "https://creativecommons.org/licenses/by/4.0/", "contact": ["A. Curator"],
             "version": "3.0", "naturalLanguage": ["en"], "definitionProperty": "https://example.org/definition",
             "prefLabelProperty": "http://www.w3.org/2004/02/skos/core#prefLabel"},
            {"classes": 100, "properties": 20, "individuals": 5},
            {"catalog_status": "ok", "download_status": "ok", "entities": 100,
             "labelled_entities": 95, "define_share": 0.8, "owl_imports_count": 2,
             "ontology_metadata_standard_predicates": 4},
        )
        self.assertGreater(rich["score"], sparse["score"])
        f1q1 = next(check for check in all_checks(rich) if check["id"] == "F1Q1")
        self.assertEqual("pass", f1q1["status"])
        self.assertIn("https://example.org/ontology", f1q1["evidence"])
        self.assertTrue(rich["recommendations"])

    def test_live_checks_are_explicitly_not_tested(self):
        result = fair.assess()
        checks = {check["id"]: check for check in all_checks(result)}
        self.assertEqual("not_tested", checks["F4Q3"]["status"])
        self.assertEqual(0.0, checks["F4Q3"]["score"])
        self.assertIn("not probed", checks["F4Q3"]["evidence"])

    def test_assessment_is_json_serializable(self):
        encoded = json.dumps(fair.assess())
        self.assertIn("owl4-fair-1.0", encoded)


class D1SchemaTests(unittest.TestCase):
    def test_new_schema_contains_fair_columns(self):
        db = sqlite3.connect(":memory:")
        schema = (ROOT / "data" / "run_v1_1_1" / "exports" / "d1_schema.sql").read_text(encoding="utf-8")
        db.executescript(schema)
        columns = {row[1] for row in db.execute("PRAGMA table_info(ontology_scores)")}
        self.assertTrue({"fair_score", "fair_findable", "fair_accessible", "fair_interoperable",
                         "fair_reusable", "fair_assessment_json"}.issubset(columns))


if __name__ == "__main__":
    unittest.main()
