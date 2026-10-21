"""Evaluation reports must be reproducible and avoid implying measured AI accuracy."""

import copy
import json
import unittest

from evaluations.run import CASES, evaluate, grade_sources


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.corpus = json.loads(CASES.read_text(encoding="utf-8"))

    def test_offline_report_is_reproducible_and_discloses_unmeasured_semantics(self):
        first = evaluate(self.corpus)
        self.assertEqual(first, evaluate(self.corpus))
        self.assertEqual(first["cases"], 32)
        self.assertEqual(first["model_calls"], 0)
        self.assertEqual(first["model_answer_evaluation"]["status"], "not_run")
        self.assertEqual(first["model_answer_evaluation"]["semantic_accuracy"], "not_measured")
        self.assertTrue(all(item["passed"] for item in first["results"] if item["kind"] != "retrieval"))

    def test_matching_quote_does_not_prove_the_opposite_claim(self):
        case = next(item for item in self.corpus["cases"] if item["id"] == "negated_defect")
        answer = {"answer": "Every hinge breaks.", "evidence": [{"review_id": "a", "quote": "The hinge is not broken."}]}
        result = grade_sources(case, answer)
        self.assertTrue(result["passed"])
        self.assertEqual(result["semantic_review"], "required")

    def test_fabricated_quotes_wrong_products_and_repeated_reviews_fail(self):
        case = next(item for item in self.corpus["cases"] if item["id"] == "battery_synonym")
        source = {"review_id": "a", "asin": "B0EVAL0001", "quote": case["reviews"][0]["body"]}
        for mutation in [{**source, "quote": "Guaranteed 100 hours."}, {**source, "asin": "B0EVAL0002"}, {**source, "review_id": "unknown"}]:
            self.assertFalse(grade_sources(case, {"answer": "A claim.", "evidence": [mutation]})["passed"])
        self.assertFalse(grade_sources(case, {"answer": "A claim.", "evidence": [source, source]})["passed"])
        self.assertFalse(grade_sources(case, {"answer": "A claim.", "evidence": [{**source, "quotes": ["Invented second passage"]}]})["passed"])

    def test_committed_evaluation_report_matches_the_current_corpus(self):
        baseline = json.loads((CASES.parent.parent / "docs" / "evaluation-results.json").read_text(encoding="utf-8"))
        self.assertEqual(evaluate(self.corpus), baseline)

    def test_answers_and_corpus_identity_are_checked(self):
        with self.assertRaises(ValueError):
            evaluate(self.corpus, {"unknown-case": {}})
        changed = copy.deepcopy(self.corpus)
        changed["cases"].append(changed["cases"][0])
        with self.assertRaises(ValueError):
            evaluate(changed)
