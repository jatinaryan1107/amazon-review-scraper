"""Fictional cross-category evidence, no external model or browser requests."""

import json
from pathlib import Path
import tempfile
import unittest

from analysis_store import AnalysisStore
from llm_analysis import LLMEngine
from llm_provider import ProviderRegistry
from product_comparison import compare_products, validate_options
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
