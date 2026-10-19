"""Buying briefs use fictional evidence and fake provider transports only."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from analysis_store import AnalysisStore
from buying_brief import build_brief, validate_options
from llm_analysis import LLMEngine
from llm_provider import ProviderError, ProviderRegistry
from review_analysis import AnalysisCancelled


A, B = "B0TEST0001", "B0TEST0002"


def review(asin=A, index=1, **changes):
    return {"asin": asin, "product_url": f"https://www.amazon.in/dp/{asin}", "review_id": f"PRIVATE-{asin}-{index}",
        "reviewer_name": "PRIVATE AUTHOR", "verified_purchase": "true", "rating": "4", "review_date": "",
        "review_title": "Fictional headphones", "review_body": "Comfortable fit. Battery lasts well. The microphone is clear.", **changes}


def completion(payload):
    incoming = json.loads(payload["messages"][-1]["content"])
    items, passages = [], []
    for product in incoming["products"]:
        row = next(row for row in incoming["review_evidence"]["reviews"] if row["product"] == product)
        passage = next(iter(row["body"] or row["title"]))
        passages.append(passage)
        items.append({"product": product, "benefits": [{"priority": incoming["priorities"][0],
            "text": f"A selected owner describes {incoming['priorities'][0]}.", "evidence": [passage]}], "concerns": [],
            "uncertainties": ["This is a selected sample."]})
    value = {"products": items, "tradeoffs": []}
    if len(items) == 2:
        value["tradeoffs"] = [{"priority": incoming["priorities"][0], "text": f"Read both products' {incoming['priorities'][0]} experiences before deciding.", "evidence": passages}]
    return {"model": "fictional/buying:free", "usage": {"prompt_tokens": 20, "completion_tokens": 30, "cost": 0},
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}


class BuyingBriefTests(unittest.TestCase):
    def test_category_neutral_priorities_are_supported_without_audio_assumptions(self):
        options = validate_options("demo", [A], ["usability", "cleaning", "value"])
        self.assertEqual(options["priorities"], ["cleaning", "usability", "value"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.now = [1800000000.0]
        self.store = AnalysisStore(Path(self.temp.name) / "analysis.sqlite3", clock=lambda: self.now[0],
            sleep=lambda seconds: self.now.__setitem__(0, self.now[0] + max(seconds, 0.001)))
        self.registry = ProviderRegistry(environ={})
        self.registry.configure({"api_key": "synthetic-buying-key"})

    def engine(self, transport=completion):
        return LLMEngine(self.registry, self.store, transport=transport)

    def build(self, engine, rows=None, **options):
        return build_brief(engine, rows if rows is not None else [review()], dataset="workspace",
            products=options.pop("products", [A]), priorities=options.pop("priorities", ["comfort"]), **options)

    def test_validates_full_urls_asins_priorities_and_rejects_unsafe_input(self):
        self.assertEqual(validate_options("workspace", [f"https://www.amazon.in/Fictional/dp/{A}?tag=x"], ["sound"])["asins"], [A])
        self.assertEqual(validate_options("demo", [A.lower()], [])["asins"], [A])
        for products in [[], [A, A], [A, B, "B0TEST0003"], ["https://amazon.in.evil.invalid/dp/" + A], ["http://amazon.in/dp/" + A], ["https://user:secret@amazon.in/dp/" + A], ["https://amzn.in/x"], [12]]:
            with self.assertRaises(ValueError):
                validate_options("workspace", products, [])
        for priorities in [["comfort", "comfort"], ["price"], ["comfort", "calls", "sound", "battery"], "sound", [None]]:
            with self.assertRaises(ValueError):
                validate_options("workspace", [A], priorities)

    def test_local_only_and_missing_products_do_not_call_a_provider(self):
        def forbidden(payload):
            self.fail("Local and missing-data paths must not call a provider")
        for rows, products in [([review()], [A]), ([], [A]), ([review()], [A, B]), ([review(review_title="", review_body="")], [A])]:
            result = self.build(self.engine(forbidden), rows, products=products, allow_external=products != [A] or not rows or not rows[0]["review_body"])
            self.assertEqual(result["method"], "local_evidence")
            self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(self.store.daily_remaining(), 50)

    def test_two_products_share_one_bounded_tool_free_request_and_private_fields_stay_local(self):
        sent = []
        result = self.build(self.engine(lambda payload: sent.append(payload) or completion(payload)), [review(), review(B)], products=[A, B], allow_external=True)
        self.assertEqual(result["method"], "openrouter")
        self.assertEqual(result["usage"]["calls"], 1)
        self.assertEqual(len(result["tradeoffs"]), 1)
        payload = sent[0]
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["max_tokens"], 1000)
        self.assertEqual(payload["provider"]["data_collection"], "deny")
        self.assertEqual(payload["provider"]["max_price"], {"prompt": 0, "completion": 0, "request": 0})
        for private in [A, B, "PRIVATE", "www.amazon.in", "synthetic-buying-key"]:
            self.assertNotIn(private, json.dumps(payload))
        for item in result["products"]:
            self.assertIn(item["benefits"][0]["evidence"][0]["quote"], review()["review_body"])

    def test_cache_revalidates_and_changes_to_sources_priorities_or_privacy_need_new_requests(self):
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or completion(payload))
        self.build(engine, allow_external=True)
        cached = self.build(engine, allow_external=True)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["usage"]["calls"], 0)
        self.build(engine, [review(review_body="Comfortable fit with different battery feedback.")], allow_external=True)
        self.build(engine, priorities=["battery"], allow_external=True)
        self.registry.configure({"require_zdr": False})
        self.build(engine, allow_external=True)
        self.assertEqual(len(sent), 4)

    def test_selection_contains_critical_and_positive_ratings_with_at_most_twelve_per_product(self):
        rows = [review(index=index, rating="1" if index < 5 else "5", review_body="Comfortable fit " + "x" * 4000) for index in range(30)]
        sent = []
        result = self.build(self.engine(lambda payload: sent.append(payload) or completion(payload)), rows, allow_external=True)
        selection = json.loads(sent[0]["messages"][-1]["content"])["review_evidence"]["reviews"]
        self.assertLessEqual(len(selection), 12)
        self.assertEqual({entry["rating"] for entry in selection}, {"1", "5"})
        self.assertLess(len(json.dumps(sent[0]).encode()), 200000)
        self.assertEqual(result["products"][0]["stored_reviews"], 30)
        self.assertEqual(result["products"][0]["critical_rated_reviews"], 5)

    def test_invalid_citations_cross_product_sources_and_unrequested_priorities_fall_back(self):
        for mutation in ["unknown", "other_product", "priority", "oversized", "duplicate"]:
            with self.subTest(mutation=mutation):
                def invalid(payload):
                    response = completion(payload)
                    value = json.loads(response["choices"][0]["message"]["content"])
                    finding = value["products"][0]["benefits"][0]
                    if mutation == "unknown": finding["evidence"] = ["missing"]
                    if mutation == "other_product": finding["evidence"] = value["products"][1]["benefits"][0]["evidence"]
                    if mutation == "priority": finding["priority"] = "price"
                    if mutation == "oversized": finding["text"] = "x" * 301
                    if mutation == "duplicate": finding["evidence"] *= 2
                    response["choices"][0]["message"]["content"] = json.dumps(value)
                    return response
                result = self.build(self.engine(invalid), [review(), review(B)], products=[A, B], allow_external=True)
                self.assertEqual(result["method"], "local_evidence")
                self.assertIsNotNone(result["notice"])
                self.assertNotIn("benefits", result["products"][0])

    def test_shared_review_ids_are_disclosed_and_cannot_alone_support_a_tradeoff(self):
        result = self.build(self.engine(), [review(review_id="SHARED"), review(B, review_id="SHARED")], products=[A, B], allow_external=True)
        self.assertEqual(result["shared_review_ids"], 1)
        self.assertEqual(result["method"], "local_evidence")
        self.assertEqual(result["notice"]["code"], "evidence_reference")

    def test_five_valid_sources_are_accepted_and_wrong_topic_is_omitted(self):
        def response(payload):
            incoming = json.loads(payload["messages"][-1]["content"])
            labels = [next(iter(row["body"])) for row in incoming["review_evidence"]["reviews"]]
            value = {"products": [{"product": "p1", "benefits": [
                {"priority": "comfort", "text": "Owners describe a comfortable fit.", "evidence": labels[:5]},
                {"priority": "durability", "text": "The battery lasts several days.", "evidence": labels[:1]}],
                "concerns": [], "uncertainties": []}], "tradeoffs": []}
            result = completion(payload)
            result["choices"][0]["message"]["content"] = json.dumps(value)
            return result
        result = self.build(self.engine(response), [review(index=index) for index in range(8)], priorities=["comfort", "durability"], allow_external=True)
        self.assertEqual(result["method"], "openrouter")
        self.assertEqual(len(result["products"][0]["benefits"]), 1)
        self.assertEqual(len(result["products"][0]["benefits"][0]["evidence"]), 5)
        self.assertTrue(any("omitted" in note for note in result["products"][0]["uncertainties"]))

    def test_provider_failure_and_restart_backoff_return_evidence_without_retry(self):
        sent = []
        def fail(payload):
            sent.append(payload)
            raise ProviderError("PRIVATE PROVIDER ERROR", code="rate_limit", retry_after=30)
        first = self.build(self.engine(fail), allow_external=True)
        second = self.build(self.engine(fail), allow_external=True)
        self.assertEqual(first["notice"]["code"], "rate_limit")
        self.assertEqual(second["notice"]["code"], "cooldown")
        self.assertEqual(second["usage"]["calls"], 0)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("PRIVATE PROVIDER ERROR", json.dumps(first))

    def test_missing_key_and_cancellation_do_not_consume_attempts(self):
        self.registry.configure({"clear": True})
        result = self.build(self.engine(), allow_external=True)
        self.assertEqual(result["notice"]["code"], "not_configured")
        self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(self.store.daily_remaining(), 50)
        with self.assertRaises(AnalysisCancelled):
            self.build(self.engine(), cancelled=lambda: True)

    def test_tampered_cache_citations_are_rejected_and_rebuilt(self):
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or completion(payload))
        self.build(engine, allow_external=True)
        with closing(sqlite3.connect(self.store.path)) as connection:
            key = connection.execute("SELECT key FROM chunks").fetchone()[0]
        saved = self.store.get_chunk(key)
        saved["raw"]["products"][0]["benefits"][0]["evidence"] = ["invented"]
        self.store.put_chunk(key, saved)
        result = self.build(engine, allow_external=True)
        self.assertEqual(result["method"], "openrouter")
        self.assertFalse(result["cache_hit"])
        self.assertEqual(len(sent), 2)

    def test_cache_write_failure_preserves_validated_brief_with_an_explicit_limit(self):
        with patch.object(self.store, "put_chunk", side_effect=OSError("PRIVATE-STORAGE-ERROR")):
            result = self.build(self.engine(), allow_external=True)
        self.assertEqual(result["method"], "openrouter")
        self.assertTrue(any("could not be cached" in note for note in result["limitations"]))
        self.assertNotIn("PRIVATE-STORAGE-ERROR", json.dumps(result))

    def test_equivalent_text_notes_are_normalised_and_unselected_topics_are_omitted(self):
        def response(payload):
            result = completion(payload)
            value = json.loads(result["choices"][0]["message"]["content"])
            value["products"][0]["uncertainties"] = [{"text": "Comfort over longer sessions is not established."}, {"text": "No battery duration is established."}]
            result["choices"][0]["message"]["content"] = json.dumps(value)
            return result
        result = self.build(self.engine(response), allow_external=True)
        self.assertEqual(result["method"], "openrouter")
        notes = result["products"][0]["uncertainties"]
        self.assertIn("Comfort over longer sessions is not established.", notes)
        self.assertFalse(any("battery duration" in note for note in notes))
        self.assertTrue(all(isinstance(note, str) for note in notes))


if __name__ == "__main__":
    unittest.main()
