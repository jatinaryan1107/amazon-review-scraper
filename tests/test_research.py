"""Research recovery, privacy and request counts with fictional reviews only."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from analysis_store import AnalysisStore
from llm_analysis import LLMEngine
from llm_provider import ProviderError, ProviderRegistry
from review_analysis import AnalysisCancelled


def review(index=1, **changes):
    return {"asin": "B0TEST0001", "review_id": f"PRIVATE-ID-{index}", "rating": "4",
            "review_title": "Fictional value feedback", "review_body": "The price is affordable and the build feels solid.",
            "reviewer_name": "PRIVATE-AUTHOR", "product_url": "https://www.amazon.in/dp/B0TEST0001", **changes}


def completion(payload):
    entries = json.loads(payload["messages"][-1]["content"])["review_evidence"]["reviews"]
    passage = next(iter(entries[0]["body"]))
    value = {"answer": "A cited reviewer describes the price as affordable. Current market price is unknown.",
             "evidence": [passage], "limitations": ["A retrieved example, not a prevalence estimate."]}
    return {"model": "fictional/research:free", "usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": 0},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.now = [1800000000.0]
        self.store = AnalysisStore(Path(self.temp.name) / "analysis.sqlite3", clock=lambda: self.now[0],
            sleep=lambda seconds: self.now.__setitem__(0, self.now[0] + max(0.001, seconds)))
        self.registry = ProviderRegistry(environ={})
        self.registry.configure({"api_key": "synthetic-research-key"})

    def engine(self, transport=completion):
        return LLMEngine(self.registry, self.store, transport=transport)

    def ask(self, engine, rows=None, **options):
        return engine.ask(rows if rows is not None else [review()], "Is this product considered cheap?",
                          dataset="workspace", asin="B0TEST0001", **options)

    def test_focused_research_retrieves_locally_and_uses_one_tool_free_request(self):
        sent = []
        result = self.ask(self.engine(lambda payload: sent.append(payload) or completion(payload)))
        self.assertEqual(result["method"], "openrouter")
        self.assertEqual(result["usage"]["calls"], 1)
        self.assertEqual(result["scope"]["retrieved_reviews"], 1)
        self.assertNotIn("tools", sent[0])
        self.assertNotIn("tool_choice", sent[0])
        self.assertEqual(sent[0]["provider"]["data_collection"], "deny")
        self.assertEqual(sent[0]["provider"]["max_price"], {"prompt": 0, "completion": 0, "request": 0})
        for private in ["PRIVATE-ID", "PRIVATE-AUTHOR", "B0TEST0001", "www.amazon.in", "synthetic-research-key"]:
            self.assertNotIn(private, json.dumps(sent))
        self.assertIn(result["evidence"][0]["quote"], review()["review_body"])
        self.assertTrue(any("today's price" in note for note in result["limitations"]))

    def test_same_question_uses_revalidated_cache_but_text_and_privacy_changes_do_not(self):
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or completion(payload))
        self.ask(engine)
        again = self.ask(engine)
        self.assertTrue(again["cache_hit"])
        self.assertEqual(again["usage"]["calls"], 0)
        self.assertEqual(len(sent), 1)
        self.assertTrue(any("today's price" in note for note in again["limitations"]))
        self.ask(engine, [review(review_body="A different affordable price observation.")])
        self.assertEqual(len(sent), 2)
        self.registry.configure({"require_zdr": False})
        self.ask(engine)
        self.assertEqual(len(sent), 3)

    def test_altered_cached_quote_cannot_be_displayed_as_valid_ai_evidence(self):
        engine = self.engine()
        self.ask(engine)
        with closing(sqlite3.connect(self.store.path)) as connection:
            key = connection.execute("SELECT key FROM chunks").fetchone()[0]
        value = self.store.get_chunk(key)
        value["result"]["evidence"][0]["quote"] = "An invented quote"
        self.store.put_chunk(key, value)
        result = self.ask(engine)
        self.assertFalse(result["cache_hit"])
        self.assertNotIn("invented", json.dumps(result["evidence"]))

    def test_no_sharing_no_key_empty_scope_and_no_matches_need_no_model_call(self):
        def forbidden(payload):
            self.fail("This path must not contact a provider")
        engine = self.engine(forbidden)
        self.assertEqual(self.ask(engine, allow_external=False)["method"], "local_evidence")
        self.registry.configure({"clear": True})
        result = self.ask(engine)
        self.assertEqual(result["notice"]["code"], "not_configured")
        self.assertEqual(self.store.daily_remaining(), 50)
        for rows in [[], [review(review_title="Fictional", review_body="Bluetooth pairing works.")]]:
            result = self.ask(engine, rows)
            self.assertEqual(result["method"], "local_evidence")
            self.assertEqual(result["usage"]["calls"], 0)
            self.assertEqual(result["evidence"], [])

    def test_transport_and_invalid_output_failures_return_honest_local_evidence_without_retry(self):
        for code in ["rate_limit", "timeout", "connection", "unavailable", "report_format", "evidence_reference"]:
            sent = []
            def failed(payload):
                sent.append(payload)
                raise ProviderError("PRIVATE-PROVIDER-RESPONSE", code=code, retry_after=20)
            result = self.ask(self.engine(failed))
            self.assertEqual(result["method"], "local_evidence")
            self.assertTrue(result["evidence"])
            self.assertEqual(result["notice"]["code"], code)
            self.assertEqual(len(sent), 1)
            self.assertIsNone(result["usage"]["cost"])
            self.assertNotIn("PRIVATE-PROVIDER-RESPONSE", json.dumps(result))
            self.now[0] += 61

    def test_invalid_json_unknown_passage_and_oversized_answer_are_not_cached_or_claimed_as_ai(self):
        for mode in ["json", "unknown", "size"]:
            sent = []
            def bad(payload):
                sent.append(payload)
                response = completion(payload)
                value = json.loads(response["choices"][0]["message"]["content"])
                if mode == "unknown":
                    value["evidence"] = ["PRIVATE-INVENTED-LABEL"]
                elif mode == "size":
                    value["answer"] = "x" * 2401
                response["choices"][0]["message"]["content"] = "PRIVATE-BAD-JSON" if mode == "json" else json.dumps(value)
                return response
            result = self.ask(self.engine(bad))
            self.assertEqual(result["method"], "local_evidence")
            self.assertEqual(len(sent), 1)
            self.assertNotIn("PRIVATE-", json.dumps(result["notice"]))
            with closing(sqlite3.connect(self.store.path)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)

    def test_cooldown_stops_repeated_requests_without_consuming_another_attempt(self):
        sent = []
        def limited(payload):
            sent.append(payload)
            raise ProviderError("private", code="rate_limit", retry_after=20)
        self.ask(self.engine(limited))
        reopened = AnalysisStore(self.store.path, clock=lambda: self.now[0])
        result = self.ask(LLMEngine(self.registry, reopened, transport=limited))
        self.assertEqual(result["notice"]["code"], "cooldown")
        self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(reopened.daily_remaining(), 49)

    def test_multiple_passages_from_one_review_are_grouped(self):
        def two(payload):
            response = completion(payload)
            value = json.loads(response["choices"][0]["message"]["content"])
            row = json.loads(payload["messages"][-1]["content"])["review_evidence"]["reviews"][0]
            value["evidence"] = [*row["title"], *row["body"]]
            response["choices"][0]["message"]["content"] = json.dumps(value)
            return response
        result = self.ask(self.engine(two))
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(len(result["evidence"][0]["quotes"]), 2)

    def test_cancel_is_not_converted_to_an_answer(self):
        with self.assertRaises(AnalysisCancelled):
            self.ask(self.engine(), cancelled=lambda: True)
        self.assertEqual(self.store.daily_remaining(), 50)

    def test_failed_cache_write_keeps_a_validated_answer_and_discloses_missing_cache(self):
        with patch.object(self.store, "put_chunk", side_effect=sqlite3.OperationalError("PRIVATE-STORAGE-DETAIL")):
            result = self.ask(self.engine())
        self.assertEqual(result["method"], "openrouter")
        self.assertTrue(any("could not be cached" in text for text in result["limitations"]))
        self.assertNotIn("PRIVATE-STORAGE", json.dumps(result))

    def test_unavailable_attempt_ledger_returns_sources_without_contacting_model(self):
        sent = []
        with patch.object(self.store, "reserve_attempt", side_effect=sqlite3.OperationalError("PRIVATE-STORAGE-DETAIL")):
            result = self.ask(self.engine(lambda payload: sent.append(payload) or completion(payload)))
        self.assertEqual(sent, [])
        self.assertEqual(result["notice"]["code"], "research_storage")
        self.assertTrue(result["evidence"])

    def test_general_questions_and_long_reviews_stay_bounded(self):
        sent = []
        rows = [review(i, review_body="x " * 5000 + "affordable price" + " y" * 5000) for i in range(50)]
        result = self.ask(self.engine(lambda payload: sent.append(payload) or completion(payload)), rows)
        self.assertEqual(result["scope"]["retrieved_reviews"], 12)
        wire = json.loads(sent[0]["messages"][-1]["content"])["review_evidence"]["reviews"]
        self.assertTrue(all(sum(map(len, row["body"].values())) <= 600 for row in wire))
        self.assertTrue(all("price" in "".join(row["body"].values()) for row in wire))
        self.assertLess(len(json.dumps(sent[0], ensure_ascii=False).encode()), 200000)

    def test_agent_protocol_failure_preserves_attempt_count_and_returns_local_sources(self):
        def malformed(payload):
            response = completion({"messages": [{"content": json.dumps({"review_evidence": {"reviews": [{"body": {"e1": "Fictional"}}]}})}]})
            response["choices"][0]["message"]["tool_calls"] = [{"id": "bad", "type": "function", "function": {"name": "run_shell", "arguments": "{}"}}]
            response["choices"][0]["finish_reason"] = "tool_calls"
            return response
        result = self.ask(self.engine(malformed), strategy="agent")
        self.assertEqual(result["method"], "local_evidence")
        self.assertEqual(result["usage"]["calls"], 1)
        self.assertTrue(result["evidence"])

    def test_exact_local_statistics_remain_provider_free(self):
        self.registry.configure({"clear": True})
        result = self.engine(lambda payload: self.fail("No model for exact statistics")).ask(
            [review()], "How many reviews?", dataset="workspace", asin="")
        self.assertEqual(result["method"], "local")
        self.assertEqual(result["usage"]["calls"], 0)
