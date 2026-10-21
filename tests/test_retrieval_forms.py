"""Word-family retrieval retains original evidence, including negated reports."""

import unittest

from llm_analysis import LLMEngine
from llm_provider import ProviderRegistry
from llm_provider import ProviderError
from research import _focused_citations, retrieve


class RetrievalFormsTests(unittest.TestCase):
    def test_only_redundant_exact_citations_are_collapsed(self):
        citations = {"e1": {"key": "r1", "quote": "It leaks."},
                     "e2": {"key": "r1", "quote": "It leaks."},
                     "e3": {"key": "r2", "quote": "It leaks."}}
        evidence, changed = _focused_citations(["e1", "e1", "e2", "e3"], citations)
        self.assertTrue(changed)
        self.assertEqual(evidence, [citations["e1"], citations["e3"]])
        for invalid in (["e1", "unknown"], ["e1", {}], [], ["e1"] * 9, "e1"):
            with self.subTest(invalid=invalid), self.assertRaises(ProviderError):
                _focused_citations(invalid, citations)

    def test_leak_forms_keep_negative_accounts_without_matching_substrings(self):
        engine = LLMEngine(ProviderRegistry(environ={}), None)
        rows = [{"asin": "B0TEST0001", "review_id": str(i), "review_title": "", "review_body": body, "rating": "4"}
                for i, body in enumerate(["It leaks near the lid.", "It has never leaked.", "No leaking in my use.", "The design feels sleek."])]
        canonical, _, _, _ = engine._prepare(rows, "demo", "")
        for form in ("leak", "leaks", "leaked", "leaking"):
            with self.subTest(form=form):
                _, exposed, _ = retrieve(engine, canonical, f"Does it {form}?")
                self.assertEqual({row["review_id"] for row in exposed.values()}, {"0", "1", "2"})

    def test_word_forms_are_symmetric_and_keep_exact_source_excerpts(self):
        engine = LLMEngine(ProviderRegistry(environ={}), None)
        forms = ["overheat", "overheats", "overheated", "overheating"]
        bodies = [f"{'Neutral background. ' * 80}No {word} in my use." for word in forms]
        bodies += ["The logo resembles a heater.", "The case is heat resistant."]
        rows = [{"asin": "B0TEST0001", "review_id": str(i), "review_title": "",
                 "review_body": body, "rating": "4"} for i, body in enumerate(bodies)]
        canonical, _, _, _ = engine._prepare(rows, "demo", "")
        for form in forms:
            with self.subTest(form=form):
                selected, exposed, scope = retrieve(engine, canonical, f"Does it {form}?")
                self.assertEqual({row["review_id"] for row in exposed.values()}, {"0", "1", "2", "3"})
                self.assertEqual(scope["matched_reviews"], 4)
                for item in selected:
                    original = exposed[item["key"]]["review_body"]
                    self.assertIn(item["body"], original)
                    self.assertIn("No overheat", item["body"])


if __name__ == "__main__":
    unittest.main()
