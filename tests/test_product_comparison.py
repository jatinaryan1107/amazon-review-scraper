"""Fictional cross-category evidence, no external model or browser requests."""

import json
from pathlib import Path
import tempfile
import unittest

from analysis_store import AnalysisStore
from llm_analysis import LLMEngine
from llm_provider import ProviderError, ProviderRegistry
from product_comparison import collection_plan, compare_products, comparison_json, validate_options
from review_analysis import AnalysisCancelled


A, B = "B0TEST0001", "B0TEST0002"


def review(asin, index=1, **changes):
    return {"asin": asin, "product_url": f"https://www.amazon.in/dp/{asin}", "review_id": f"PRIVATE-{asin}-{index}",
            "reviewer_name": "PRIVATE AUTHOR", "verified_purchase": "true", "rating": "4", "review_date": "",
            "review_title": "Fictional owner experience", "review_body": "Bottle stays cold." if asin == A else "Chopper cuts onions but its cord sticks.", **changes}


def completion(payload):
    packet = json.loads(payload["messages"][-1]["content"])
    products = []
    for item in packet["products"]:
        row = next(row for row in packet["review_evidence"]["reviews"] if row["product"] == item["product"])
        label = next(iter(row["body"] or row["title"]))
        products.append({"product": item["product"], "benefits": [{"aspect": "Everyday use", "text": "A selected owner describes everyday use.", "evidence": [label]}],
                         "concerns": [], "uncertainties": ["Current price was not checked."]})
    return {"model": "fictional/comparison:free", "usage": {"prompt_tokens": 20, "completion_tokens": 30, "cost": 0},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"relationship": "different_uses", "products": products})}}]}


class ProductComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.store = AnalysisStore(Path(self.temp.name) / "analysis.sqlite3")
        self.registry = ProviderRegistry(environ={})
        self.registry.configure({"api_key": "synthetic-comparison-key"})

    def engine(self, transport=completion):
        return LLMEngine(self.registry, self.store, transport=transport)

    def compare(self, engine, rows=None, **options):
        return compare_products(engine, rows if rows is not None else [review(A), review(B)],
                                dataset="workspace", products=[A, B], **options)

    def test_links_normalize_tracking_reject_duplicates_prose_and_unsafe_destinations(self):
        valid = validate_options("workspace", [f"https://www.amazon.in/Bottle/dp/{A}?tag=tracking&ref=x#details", B.lower()], True, 3)
        self.assertEqual(valid["urls"], [f"https://www.amazon.in/dp/{A}", f"https://www.amazon.in/dp/{B}"])
        for products in [[A, A.lower()], [A], [A, B, "B0TEST0003"], [A, f"https://www.amazon.in.evil.test/dp/{B}"],
                         [A, f"http://amazon.in/dp/{B}"], [A, f"https://user:pass@amazon.in/dp/{B}"],
                         [A, f"https://amazon.in:8443/dp/{B}"], [A, f"https://localhost/dp/{B}"],
                         [A, "https://amzn.in/short"], [A, f"buy this https://amazon.in/dp/{B}"], [A, None]]:
            with self.subTest(products=products), self.assertRaises(ValueError):
                validate_options("workspace", products)
        for dataset, collect, pages in [("demo", True, 3), ("unknown", False, 3), ("workspace", "yes", 3), ("workspace", False, True), ("workspace", False, 11)]:
            with self.assertRaises(ValueError):
                validate_options(dataset, [A, B], collect, pages)

    def test_local_missing_or_empty_product_text_never_uses_model_allowance(self):
        def forbidden(payload):
            self.fail("No model call should occur")
        engine = self.engine(forbidden)
        for rows, external in [([review(A), review(B)], False), ([review(A)], True), ([], True),
                               ([review(A), review(B, review_title="", review_body="")], True)]:
            result = self.compare(engine, rows, allow_external=external)
            self.assertEqual(result["usage"]["calls"], 0)
            self.assertEqual(result["method"], "local_evidence")

    def test_cross_category_one_call_private_fields_stay_local_and_cache_is_revalidated(self):
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or completion(payload))
        names = {A: "Fictional bottle", B: "Fictional chopper"}
        for expected in (1, 0):
            result = self.compare(engine, allow_external=True, names=names)
            self.assertEqual(result["method"], "openrouter")
            self.assertEqual(result["usage"]["calls"], expected)
            self.assertEqual(result["relationship"], "different_uses")
            self.assertGreaterEqual(result["timings"]["ai_seconds"], 0)
            for item in result["products"]:
                quote = item["benefits"][0]["evidence"][0]["quote"]
                self.assertIn(quote, review(item["asin"])["review_body"])
        self.assertEqual(len(sent), 1)
        packet = json.dumps(sent[0])
        for value in [A, B, "PRIVATE", "www.amazon.in", "synthetic-comparison-key"]:
            self.assertNotIn(value, packet)
        self.assertIn("Fictional bottle", packet)
        self.assertNotIn("tools", sent[0])
        self.assertEqual(sent[0]["provider"]["max_price"], {"prompt": 0, "completion": 0, "request": 0})

    def test_changed_source_or_product_name_invalidates_cached_interpretation(self):
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or completion(payload))
        self.compare(engine, allow_external=True)
        self.compare(engine, [review(A, review_body="Bottle leaked."), review(B)], allow_external=True)
        self.compare(engine, allow_external=True, names={A: "Renamed bottle"})
        self.assertEqual(len(sent), 3)

    def test_cross_product_or_unknown_citations_fail_closed_without_retry(self):
        for mutation in ("wrong_product", "unknown_label", "duplicate_product", "unknown_relationship"):
            def bad(payload):
                response = completion(payload)
                raw = json.loads(response["choices"][0]["message"]["content"])
                if mutation == "wrong_product":
                    raw["products"][0]["benefits"][0]["evidence"] = raw["products"][1]["benefits"][0]["evidence"]
                elif mutation == "unknown_label":
                    raw["products"][0]["benefits"][0]["evidence"] = ["unknown"]
                elif mutation == "duplicate_product":
                    raw["products"][1]["product"] = "p1"
                else:
                    raw["relationship"] = "winner"
                response["choices"][0]["message"]["content"] = json.dumps(raw)
                return response
            result = self.compare(self.engine(bad), allow_external=True, names={A: mutation})
            self.assertEqual(result["method"], "local_evidence")
            self.assertEqual(result["usage"]["calls"], 1)
            self.assertIsNotNone(result["notice"])

    def test_cancelled_before_provider_attempt(self):
        with self.assertRaises(AnalysisCancelled):
            self.compare(self.engine(), allow_external=True, cancelled=lambda: True)

    def test_category_neutral_selection_balances_ratings_and_prefers_substantive_text(self):
        rows = []
        for asin in (A, B):
            rows.extend(review(asin, index, review_body="Good", rating="5") for index in range(1, 16))
            rows.extend(review(asin, index, review_body="Detailed owner observation about the product's daily use and build. " * 3,
                               rating="1" if index % 2 else "3") for index in range(16, 31))
        result = self.compare(self.engine(), rows)
        for item in result["products"]:
            self.assertEqual(item["selected_reviews"], 12)
            self.assertEqual(item["stored_reviews"], 30)
            self.assertEqual({example["rating"] for example in item["source_examples"]}, {"1", "3", "5"})

    def test_target_plan_reuses_full_samples_and_bounds_only_the_shortfall(self):
        options = validate_options("workspace", [A, B], True, 3, 30)
        plan = collection_plan(options, {A: {"review_count": 30}, B: {"review_count": 19}})
        self.assertEqual(plan, [{"asin": B, "url": f"https://www.amazon.in/dp/{B}", "pages": 2, "before": 19}])
        self.assertEqual(collection_plan({**options, "collect_missing": False}, {}), [])
        self.assertEqual(collection_plan(options, {A: {"review_count": 10, "collection_complete": True}, B: {"review_count": 30}}), [])
        self.assertEqual(collection_plan({**options, "target_reviews": 100, "max_pages": 1}, {})[0]["pages"], 1)
        for target, focus in [(True, ""), (31, ""), (30, "x" * 301), (30, "bad\x00focus"), (30, None)]:
            with self.assertRaises(ValueError):
                validate_options("workspace", [A, B], target_reviews=target, focus=focus)

    def test_priorities_change_selection_and_cache_identity_without_cross_product_sources(self):
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or completion(payload))
        rows = [review(A, review_body="The bottle is easy to clean."), review(B, review_body="Chopper is easy to clean."),
                review(A, 2, review_body="The finish is shiny."), review(B, 2, review_body="The box arrived yesterday.")]
        result = self.compare(engine, rows, allow_external=True, focus="clean")
        self.assertEqual(result["method"], "openrouter")
        self.assertTrue(all(item["focus_matched_reviews"] == 1 for item in result["products"]))
        self.assertEqual(len(result["source_context"]), 2)
        self.assertTrue(all("clean" in item["body"] for item in result["source_context"]))
        incoming = json.loads(sent[0]["messages"][-1]["content"])
        self.assertEqual(incoming["decision_focus"], "clean")
        repeated = self.compare(engine, rows, allow_external=True, focus="clean")
        self.assertEqual(repeated["usage"]["calls"], 0)
        other = self.compare(engine, rows, allow_external=True, focus="shiny")
        self.assertEqual(other["usage"]["calls"], 1)
        self.assertEqual(other["products"][1]["focus_matched_reviews"], 0)
        self.assertTrue(any("no lexical matches" in note for note in other["limitations"]))

    def test_unmatched_focus_returns_general_context_and_explicit_limits(self):
        result = self.compare(self.engine(), focus="dishwasher")
        self.assertTrue(all(item["selected_reviews"] for item in result["products"]))
        self.assertTrue(all(item["focus_matched_reviews"] == 0 for item in result["products"]))
        for context in result["source_context"]:
            self.assertIn(context["body"], review(context["asin"])["review_body"])
            self.assertNotIn("reviewer_name", context)

    def test_bounded_delimiter_normalization_does_not_change_fields_or_quoted_brackets(self):
        raw = {"relationship": "different_uses", "products": [{"text": "A quoted ] bracket and an escaped \" quote"}, {"text": "Two"}]}
        valid = json.dumps(raw)
        for content in (valid[:-2] + "}", valid + "]}"):
            value, normalized = comparison_json(content)
            self.assertEqual(value, raw)
            self.assertTrue(normalized)
        self.assertEqual(comparison_json(valid), (raw, False))

    def test_normalization_rejects_truncation_duplicate_keys_prose_and_large_repairs(self):
        for content in ['{"products":[', '{"text":"unfinished}', '{"x":1,"x":2}]}',
                        '{"x":1} {"y":2}', 'Here is {"x":1}', ']{"x":1}', '{"x":1}]]]', '{"x":1']:
            with self.subTest(content=content), self.assertRaises(ProviderError):
                comparison_json(content)

    def test_missing_array_delimiter_is_normalized_once_and_cached_after_source_checks(self):
        sent = []
        def missing_delimiter(payload):
            sent.append(payload)
            response = completion(payload)
            raw = response["choices"][0]["message"]["content"]
            self.assertTrue(raw.endswith("]}"))
            response["choices"][0]["message"]["content"] = raw[:-2] + "}"
            return response
        engine = self.engine(missing_delimiter)
        for expected in (1, 0):
            result = self.compare(engine, allow_external=True, focus="clean")
            self.assertEqual(result["method"], "openrouter")
            self.assertTrue(result["format_normalized"])
            self.assertEqual(result["usage"]["calls"], expected)
        self.assertEqual(len(sent), 1)

    def test_flat_output_contract_keeps_correct_product_sources_and_revalidates_cache(self):
        sent = []
        def flat(payload):
            sent.append(payload)
            response = completion(payload)
            nested = json.loads(response["choices"][0]["message"]["content"])
            raw = {"relationship": nested["relationship"]}
            for item in nested["products"]:
                key = item["product"]
                for field, plural in (("benefit", "benefits"), ("concern", "concerns")):
                    finding = (item[plural] or [{}])[0]
                    raw[f"{key}_{field}"] = finding.get("text", "")
                    raw[f"{key}_{field}_aspect"] = finding.get("aspect", "")
                    raw[f"{key}_{field}_sources"] = finding.get("evidence", [])
                raw[f"{key}_unknowns"] = "Current price is unknown."
            response["choices"][0]["message"]["content"] = json.dumps(raw)
            return response
        engine = self.engine(flat)
        for expected in (1, 0):
            result = self.compare(engine, allow_external=True)
            self.assertEqual(result["method"], "openrouter")
            self.assertEqual(result["usage"]["calls"], expected)
            self.assertEqual(result["products"][0]["benefits"][0]["evidence"][0]["asin"], A)
        self.assertEqual(len(sent), 1)

    def test_flat_findings_cannot_leave_sources_on_empty_claims(self):
        from product_comparison import _validate
        raw = {"relationship": "different_uses"}
        for product in ("p1", "p2"):
            for field in ("benefit", "concern"):
                raw[f"{product}_{field}"] = ""
                raw[f"{product}_{field}_aspect"] = ""
                raw[f"{product}_{field}_sources"] = []
            raw[f"{product}_unknowns"] = ""
        raw["p1_benefit_sources"] = ["e1"]
        with self.assertRaises(ProviderError):
            _validate(self.engine(), raw, {"p1": A, "p2": B}, {}, {})
