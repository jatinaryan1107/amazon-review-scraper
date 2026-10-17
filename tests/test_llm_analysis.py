"""Evidence, coverage and agent bounds tested with deterministic fake models."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis_store import AnalysisStore
from llm_analysis import LLMEngine, can_answer_locally
from llm_provider import ProviderError, ProviderRegistry
from review_analysis import AnalysisError, report_fingerprint


def review(index=1, **changes):
    return {"asin": "B0TEST0001", "review_id": f"PRIVATE-ORIGINAL-ID-{index}",
            "review_title": "Fictional battery review", "review_body": "The battery lasts all evening.",
            "rating": "4", "reviewer_name": "PRIVATE-AUTHOR", "product_url": "https://www.amazon.in/dp/B0TEST0001", **changes}


def response(content=None, tool_calls=None):
    return {"model": "fictional/model:free", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0},
            "choices": [{"finish_reason": "tool_calls" if tool_calls else "stop",
                         "message": {"content": content, **({"tool_calls": tool_calls} if tool_calls else {})}}]}


def report_response(payload):
    entries = json.loads(payload["messages"][-1]["content"])["reviews"]
    first = entries[0]
    passage = next(iter(first["body"] or first["title"]))
    return response(json.dumps({"summary": "A fictional review discusses battery use.",
        "themes": [{"label": "Battery use", "explanation": "A cited example of battery use.",
                    "evidence": [passage]}], "limitations": ["A test interpretation."]}))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.now = [1_800_000_000.0]
        self.store = AnalysisStore(Path(self.temp.name) / "analysis.sqlite3", clock=lambda: self.now[0],
                                   sleep=lambda seconds: self.now.__setitem__(0, self.now[0] + max(seconds, 0.001)))
        self.registry = ProviderRegistry(environ={})
        self.registry.configure({"api_key": "synthetic-test-key"})

    def engine(self, transport=report_response):
        return LLMEngine(self.registry, self.store, transport=transport)

    def test_provider_ignoring_compact_schema_cannot_save_or_cache_report(self):
        mutations = {
            "long_summary": lambda value, ids: value.update(summary="x" * 361),
            "long_label": lambda value, ids: value["themes"][0].update(label="x" * 61),
            "long_explanation": lambda value, ids: value["themes"][0].update(explanation="x" * 161),
            "many_themes": lambda value, ids: value.update(themes=[{**value["themes"][0], "label": str(i)} for i in range(4)]),
            "many_sources": lambda value, ids: value["themes"][0].update(evidence=ids[:3]),
            "many_limits": lambda value, ids: value.update(limitations=["A limit"] * 3),
            "long_limit": lambda value, ids: value.update(limitations=["x" * 161]),
        }
        rows = [review(i) for i in range(3)]
        for name, mutate in mutations.items():
            def invalid(payload):
                entries = json.loads(payload["messages"][-1]["content"])["reviews"]
                ids = [next(iter(entry["body"])) for entry in entries]
                value = json.loads(report_response(payload)["choices"][0]["message"]["content"])
                mutate(value, ids)
                return response(json.dumps(value))
            with self.subTest(case=name), self.assertRaises(ProviderError) as caught:
                self.engine(invalid).analyse(rows, dataset="workspace", asin="")
            self.assertEqual(caught.exception.code, "report_format")
            self.assertEqual(self.engine().plan(rows)["chunks_cached"], 0)

    def test_cached_report_must_still_meet_compact_limits(self):
        engine = self.engine()
        engine.analyse([review()], dataset="workspace", asin="")
        canonical, _, _, _ = engine._prepare([review()], "workspace", "")
        chunks, _, _, _ = engine._manifest(canonical, "quick")
        key = engine._chunk_key(chunks[0], "quick")
        cached = self.store.get_chunk(key)
        cached["result"]["summary"] = "x" * 361
        self.store.put_chunk(key, cached)
        self.assertEqual(engine.plan([review()])["chunks_cached"], 0)

    def test_two_segments_of_one_review_cannot_count_as_two_sources(self):
        def duplicate_source(payload):
            entries = json.loads(payload["messages"][-1]["content"])["reviews"]
            self.assertEqual(len(entries), 2)
            value = json.loads(report_response(payload)["choices"][0]["message"]["content"])
            value["themes"][0]["evidence"] = [next(iter(entry["body"])) for entry in entries]
            return response(json.dumps(value))
        rows = [review(review_body="Battery observation " * 100)]
        with self.assertRaises(ProviderError) as caught:
            self.engine(duplicate_source).analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertEqual(caught.exception.code, "evidence_reference")
        self.assertEqual(self.engine().plan(rows, depth="deep")["chunks_cached"], 0)

    def test_full_hundred_review_workflow_records_policy_and_reuses_validated_text(self):
        rows = [review(i, rating=str(i % 5 + 1), review_body="Fictional battery text " * 80) for i in range(100)]
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or report_response(payload))
        result = engine.analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertEqual(result["scope"]["analysed_reviews"], 100)
        self.assertTrue(result["scope"]["full_coverage"])
        self.assertEqual(result["rating_summary"]["average"], 3.0)
        self.assertTrue(result["provenance"]["require_zdr"])
        self.assertEqual(result["provenance"]["quote_validation"], "exact_source_match")
        self.assertGreaterEqual(result["provenance"]["elapsed_seconds"], 0)
        for payload in sent:
            self.assertTrue(payload["provider"]["zdr"])
            self.assertNotIn("PRIVATE-AUTHOR", json.dumps(payload))
        before = len(sent)
        again = engine.analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertEqual(len(sent), before)
        self.assertEqual(again["usage"]["calls"], 0)
        self.assertEqual(again["scope"]["analysed_reviews"], 100)

    def test_agent_ignoring_compact_answer_schema_is_rejected(self):
        row = {**review(), "sent_title": "", "sent_body": "The battery lasts all evening."}
        citation = {"key": "r1", "quote": row["sent_body"]}
        valid = {"answer": "A cited battery observation.", "evidence": [citation], "limitations": []}
        for changes in [{"answer": "x" * 1201}, {"limitations": ["x" * 161]},
                        {"limitations": ["A limit"] * 3}, {"evidence": [citation] * 5}]:
            with self.subTest(changes=changes), self.assertRaises(ProviderError) as caught:
                LLMEngine._validate_answer({**valid, **changes}, {"r1": row})
            self.assertEqual(caught.exception.code, "report_format")

    def test_quick_report_preserves_fingerprint_and_cache_avoids_second_request(self):
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or report_response(payload))
        rows = [review()]
        report = engine.analyse(rows, dataset="workspace", asin="")
        self.assertEqual(report["fingerprint"], report_fingerprint(rows))
        self.assertEqual(report["method"], "openrouter")
        self.assertTrue(report["scope"]["analysis_complete"])
        self.assertEqual(report["usage"]["calls"], 1)
        again = engine.analyse(rows, dataset="workspace", asin="")
        self.assertEqual((again["usage"]["calls"], again["usage"]["cached_chunks"]), (0, 1))
        self.assertEqual(len(sent), 1)
        public_payload = json.dumps(sent)
        for private in ["PRIVATE-ORIGINAL-ID", "PRIVATE-AUTHOR", "https://www.amazon.in", "B0TEST0001"]:
            self.assertNotIn(private, public_payload)
        self.assertIn("p000001", public_payload)

    def test_passage_selection_preserves_original_unicode_quotes(self):
        text = "Battery's great 👍.\nHindi: बैटरी अच्छी है.  Keep both spaces."
        result = self.engine().analyse([review(review_body=text)], dataset="workspace", asin="")
        quote = result["themes"][0]["evidence"][0]["quote"]
        self.assertEqual(quote, text)

    def test_unknown_or_duplicate_passages_are_rejected_without_caching(self):
        for mode in ["unknown", "duplicate", "same_review"]:
            def invalid(payload):
                entry = json.loads(payload["messages"][-1]["content"])["reviews"][0]
                ids = [*entry["title"], *entry["body"]]
                evidence = ["unavailable"] if mode == "unknown" else [ids[0], ids[0] if mode == "duplicate" else ids[1]]
                value = json.loads(report_response(payload)["choices"][0]["message"]["content"])
                value["themes"][0]["evidence"] = evidence
                return response(json.dumps(value))
            with self.subTest(mode=mode), self.assertRaises(ProviderError) as caught:
                self.engine(invalid).analyse([review()], dataset="workspace", asin="")
            self.assertEqual(caught.exception.code, "evidence_reference")
            self.assertEqual(self.engine().plan([review()])["chunks_cached"], 0)

    def test_tampered_cached_quote_is_rejected_even_after_passage_resolution(self):
        engine = self.engine()
        engine.analyse([review()], dataset="workspace", asin="")
        canonical, _, _, _ = engine._prepare([review()], "workspace", "")
        chunks, _, _, _ = engine._manifest(canonical, "quick")
        key = engine._chunk_key(chunks[0], "quick")
        cached = self.store.get_chunk(key)
        cached["result"]["themes"][0]["evidence"][0]["quote"] = "A fabricated cached quote."
        self.store.put_chunk(key, cached)
        self.assertEqual(engine.plan([review()])["chunks_cached"], 0)

    def test_global_duplicate_memberships_and_conflicting_variants_are_disclosed(self):
        rows = [review(), review(asin="B0TEST0002"), review(asin="B0TEST0003", review_body="A conflicting fictional account with more text.")]
        report = self.engine().analyse(rows, dataset="workspace", asin="", depth="deep")
        scope = report["scope"]
        self.assertEqual((scope["row_count"], scope["memberships"], scope["distinct_reviews"]), (3, 3, 1))
        self.assertEqual(scope["conflicting_variants"], 1)
        self.assertGreater(scope["excluded_variant_rows"], 0)
        self.assertFalse(scope["full_coverage"])
        self.assertEqual(report["fingerprint"], report_fingerprint(rows))

    def test_deep_segments_preserve_every_character_and_plan_is_read_only(self):
        rows = [review(review_title="A" * 2500, review_body="B" * 7000)]
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or report_response(payload))
        before = self.store.path.read_bytes()
        plan = engine.plan(rows, depth="deep")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(plan["segmented_reviews"], 1)
        report = engine.analyse(rows, dataset="workspace", asin="", depth="deep")
        entries = [entry for payload in sent for entry in json.loads(payload["messages"][-1]["content"])["reviews"]]
        self.assertEqual("".join(text for entry in entries for text in entry["title"].values()), "A" * 2500)
        self.assertEqual("".join(text for entry in entries for text in entry["body"].values()), "B" * 7000)
        self.assertTrue(report["scope"]["full_coverage"])
        self.assertFalse(report["scope"]["body_truncated"])

    def test_larger_batches_reduce_calls_and_bound_encoded_unicode_requests(self):
        rows = [review(index, review_body="बैटरी 👍\\\n" * 600) for index in range(40)]
        sent = []
        self.registry.configure({"require_zdr": True})
        engine = self.engine(lambda payload: sent.append(payload) or report_response(payload))
        efficient = engine.plan(rows, depth="deep")
        with patch("llm_analysis.MAX_BATCH_BYTES", 24_000):
            small = engine.plan(rows, depth="deep")
        self.assertLess(efficient["calls_needed"], small["calls_needed"])
        report = engine.analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertTrue(report["scope"]["full_coverage"])
        self.assertEqual(report["scope"]["analysed_reviews"], 40)
        self.assertEqual(len(sent), efficient["calls_needed"])
        for payload in sent:
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                                 efficient["batch_request_byte_limit"])

    @patch("llm_analysis.MAX_BATCH_BYTES", 24_000)
    def test_rate_limit_retains_completed_chunks_and_resume_reuses_them(self):
        rows = [review(index, review_body="Battery " + "x" * 1000) for index in range(80)]
        calls = []
        def limited(payload):
            calls.append(payload)
            if len(calls) == 2:
                raise ProviderError("Synthetic free quota pause.", code="rate_limit", retry_after=30)
            return report_response(payload)
        engine = self.engine(limited)
        first = engine.analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertTrue(first["scope"]["partial"])
        self.assertFalse(first["scope"]["analysis_complete"])
        self.assertEqual(first["pause_reason"]["code"], "rate_limit")
        second = self.engine().analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertTrue(second["scope"]["analysis_complete"])
        self.assertGreater(second["usage"]["cached_chunks"], 0)
        self.assertEqual(second["scope"]["analysed_reviews"], 80)

    def test_fabricated_citation_never_enters_the_chunk_cache(self):
        def fabricated(payload):
            value = json.loads(report_response(payload)["choices"][0]["message"]["content"])
            value["themes"][0]["evidence"][0] = {"key": "e1", "quote": "This quote does not occur in the supplied source."}
            return response(json.dumps(value))
        engine = self.engine(fabricated)
        with self.assertRaises(AnalysisError):
            engine.analyse([review()], dataset="workspace", asin="")
        self.assertEqual(engine.plan([review()])["chunks_cached"], 0)
        self.assertEqual(self.store.daily_remaining(), 49)

    def test_single_json_fence_is_accepted_but_prose_and_duplicate_fields_are_rejected(self):
        def fenced(payload):
            value = report_response(payload)
            content = value["choices"][0]["message"]["content"]
            value["choices"][0]["message"]["content"] = "```json\n" + content + "\n```"
            return value
        result = self.engine(fenced).analyse([review()], dataset="workspace", asin="")
        self.assertTrue(result["scope"]["analysis_complete"])
        for content in ['Here is your report: {"summary":"x"}',
                        '```json\n{"summary":"x"}\n```\nExtra prose',
                        '{"summary":"x","summary":"y","themes":[],"limitations":[]}']:
            with self.subTest(content=content), self.assertRaises(ProviderError) as caught:
                self.engine(lambda payload: response(content)).analyse(
                    [review(2, review_body="A different source excerpt.")], dataset="workspace", asin="")
            self.assertEqual(caught.exception.code, "invalid_json")

    def test_report_validation_exposes_safe_specific_reasons_without_raw_output(self):
        for expected in ["report_format", "evidence_reference"]:
            def invalid(payload):
                value = json.loads(report_response(payload)["choices"][0]["message"]["content"])
                if expected == "report_format":
                    value["unexpected"] = "PRIVATE-MODEL-OUTPUT"
                elif expected == "evidence_reference":
                    value["themes"][0]["evidence"][0] = "PRIVATE-MODEL-OUTPUT"
                return response(json.dumps(value))
            with self.subTest(expected=expected), self.assertRaises(ProviderError) as caught:
                self.engine(invalid).analyse([review()], dataset="workspace", asin="")
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("PRIVATE-MODEL-OUTPUT", str(caught.exception))
        self.assertEqual(self.engine().plan([review()])["chunks_cached"], 0)

    def test_agent_rejects_parallel_calls_before_running_any_tool(self):
        calls = [{"id": identity, "type": "function", "function": {"name": "get_stats", "arguments": "{}"}}
                 for identity in ["first", "second"]]
        sent = []
        engine = self.engine(lambda payload: sent.append(payload) or response(tool_calls=calls))
        with self.assertRaises(AnalysisError):
            engine.ask([review()], "Explain battery comments", dataset="workspace", asin="")
        self.assertEqual(len(sent), 1)
        self.assertNotIn("parallel_tool_calls", sent[0])

    def test_local_exact_questions_need_no_credentials_or_provider_requests(self):
        self.registry.configure({"clear": True})
        def forbidden(payload):
            raise AssertionError("A local statistics question must not call a model")
        engine = self.engine(forbidden)
        result = engine.ask([review(), review(2, rating="")], "How many reviews are stored?", dataset="workspace", asin="")
        self.assertEqual(result["method"], "local")
        self.assertIn("2 distinct", result["answer"])
        self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(self.store.daily_remaining(), 50)
        self.assertTrue(can_answer_locally("What is the average rating?"))
        self.assertTrue(can_answer_locally("  HOW many reviews are in this selection?!  "))
        self.assertFalse(can_answer_locally("How many reviews complain about battery quality?"))
        for filtered in ["How many one star reviews?", "How many reviews with a 1 star rating?",
                         "What is the average rating for product B0TEST0001?",
                         "Count reviews without a rating", "Average rating distribution",
                         "How many reviews are not in this selection?"]:
            with self.subTest(question=filtered):
                self.assertFalse(can_answer_locally(filtered))

    def test_truncated_compact_report_is_counted_but_not_cached_or_retried(self):
        sent = []
        def truncated(payload):
            sent.append(payload)
            value = response('{"summary":"unfinished')
            value["choices"][0]["finish_reason"] = "length"
            return value
        engine = self.engine(truncated)
        with self.assertRaises(ProviderError) as caught:
            engine.analyse([review()], dataset="workspace", asin="")
        self.assertEqual(caught.exception.code, "incomplete")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["max_tokens"], 1000)
        self.assertEqual(sent[0]["reasoning"], {"enabled": False})
        self.assertEqual(engine.plan([review()])["chunks_cached"], 0)
        self.assertEqual(self.store.daily_remaining(), 49)

    @patch("llm_analysis.MAX_BATCH_BYTES", 24_000)
    def test_deep_call_budget_pauses_and_resumes_without_repeating_cached_requests(self):
        rows = [review(index, review_body="Battery " + "x" * 24000) for index in range(32)]
        engine = self.engine()
        plan = engine.plan(rows, depth="deep")
        self.assertGreater(plan["chunks_total"], 30)
        self.assertLessEqual(plan["chunks_total"], 50)
        first = engine.analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertEqual(first["usage"]["calls"], 30)
        self.assertEqual(first["pause_reason"]["code"], "call_budget")
        self.assertTrue(first["scope"]["partial"])
        self.assertFalse(first["scope"]["analysis_complete"])
        second = engine.analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertEqual(second["usage"]["cached_chunks"], 30)
        self.assertEqual(second["usage"]["calls"], plan["chunks_total"] - 30)
        self.assertEqual(second["scope"]["analysed_reviews"], 32)
        self.assertTrue(second["scope"]["full_coverage"])

    def test_cache_provenance_and_privacy_policy_are_revalidated(self):
        engine = self.engine()
        engine.analyse([review()], dataset="workspace", asin="")
        canonical, _, _, _ = engine._prepare([review()], "workspace", "")
        chunks, _, _, _ = engine._manifest(canonical, "quick")
        key = engine._chunk_key(chunks[0], "quick")
        value = self.store.get_chunk(key)
        value["model"] = "invalid model with private text"
        self.store.put_chunk(key, value)
        self.assertEqual(engine.plan([review()])["chunks_cached"], 0)
        engine.analyse([review()], dataset="workspace", asin="")
        previous = engine.cache_discriminator()
        self.registry.configure({"require_zdr": False})
        self.assertNotEqual(previous, engine.cache_discriminator())
        self.assertEqual(engine.plan([review()])["chunks_cached"], 0)

    def test_agent_uses_read_only_tool_then_validates_only_exposed_evidence(self):
        calls = []
        def agent(payload):
            calls.append(payload)
            if len(calls) == 1:
                return response(tool_calls=[{"id": "test-call", "type": "function", "function": {
                    "name": "search_reviews", "arguments": json.dumps({"query": "battery", "rating": "all", "limit": 2})}}])
            tool = json.loads(payload["messages"][-1]["content"])["reviews"][0]
            return response(json.dumps({"answer": "The cited review describes battery life.",
                "evidence": [next(iter(tool["body"]))], "limitations": ["Only one retrieved example."]}))
        result = self.engine(agent).ask([review()], "What do reviews say about battery life?", dataset="workspace", asin="")
        self.assertEqual(result["evidence"][0]["review_id"], review()["review_id"])
        self.assertEqual(result["usage"]["calls"], 2)
        self.assertEqual(calls[0]["tool_choice"], "required")
        self.assertNotIn("response_format", calls[0])
        self.assertEqual(result["evidence"][0]["quote"], "The battery lasts all evening.")
        self.assertEqual([step["name"] for step in result["steps"] if step["kind"] == "tool"], ["search_reviews"])
        self.assertNotIn("PRIVATE-ORIGINAL-ID", json.dumps(calls))
        self.assertNotIn("B0TEST0001", json.dumps(calls))

    def test_agent_rejects_unknown_tools_repeated_calls_and_unseen_citations(self):
        for name, args in [("run_sql", {"sql": "DELETE FROM reviews"}),
                           ("search_reviews", {"query": "battery", "rating": "all", "limit": 500})]:
            with self.subTest(name=name):
                transport = lambda payload: response(tool_calls=[{"id": "test", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}])
                with self.assertRaises(AnalysisError):
                    self.engine(transport).ask([review()], "Explain battery comments", dataset="workspace", asin="")
        def unseen(payload):
            return response(json.dumps({"answer": "A claim without retrieval.", "evidence": [{"key": "r000001", "quote": "The battery lasts all evening."}], "limitations": []}))
        with self.assertRaises(AnalysisError):
            self.engine(unseen).ask([review()], "Explain battery comments", dataset="workspace", asin="")
        sent = []
        def repeated(payload):
            sent.append(payload)
            return response(tool_calls=[{"id": str(len(sent)), "type": "function", "function": {
                "name": "get_stats", "arguments": "{}"}}])
        with self.assertRaisesRegex(AnalysisError, "repeated"):
            self.engine(repeated).ask([review()], "Explain battery comments", dataset="workspace", asin="")
        self.assertEqual(len(sent), 2)

    def test_agent_json_errors_are_safe_and_retrieval_truncation_is_explicit(self):
        for arguments in [None, "PRIVATE-MALFORMED-MODEL-TEXT"]:
            def malformed(payload):
                if arguments is None:
                    return response("PRIVATE-MALFORMED-MODEL-TEXT")
                return response(tool_calls=[{"id": "bad-json", "type": "function", "function": {
                    "name": "get_stats", "arguments": arguments}}])
            with self.subTest(arguments=arguments), self.assertRaises(AnalysisError) as caught:
                self.engine(malformed).ask([review()], "Explain battery comments", dataset="workspace", asin="")
            self.assertNotIn("PRIVATE-MALFORMED", str(caught.exception))
        sent = []
        def detail(payload):
            sent.append(payload)
            if len(sent) == 1:
                return response(tool_calls=[{"id": "detail", "type": "function", "function": {
                    "name": "get_review", "arguments": '{"key":"r000001"}'}}])
            tool = json.loads(payload["messages"][-1]["content"])
            self.assertEqual(sum(map(len, tool["body"].values())), 4000)
            self.assertTrue(tool["body_truncated"])
            return response(json.dumps({"answer": "A retrieved excerpt contains battery text.",
                "evidence": [next(iter(tool["body"]))], "limitations": []}))
        result = self.engine(detail).ask([review(review_body="Battery " + "x" * 6000)], "Explain battery comments", dataset="workspace", asin="")
        self.assertEqual(result["scope"]["retrieved_truncated_reviews"], 1)
        self.assertEqual(result["scope"]["detail_body_limit"], 4000)

    def test_fractional_ratings_and_conflicting_shared_ratings_are_not_silently_counted(self):
        rows = [review(rating="1"), review(asin="B0TEST0002", rating="5"),
                review(2, rating="3.5"), review(3, rating="")]
        engine = self.engine(lambda payload: self.fail("Local statistics must not call a provider"))
        result = engine.ask(rows, "What is the average rating?", dataset="workspace", asin="")
        self.assertIn("3.50 across 1 rated", result["answer"])
        self.assertEqual(result["scope"]["rating_conflicts"], 1)
        self.assertTrue(any("conflicting" in text for text in result["limitations"]))
        counts = engine.ask(rows, "Rating distribution", dataset="workspace", asin="")
        self.assertIn("3.5 stars: 1", counts["answer"])
        self.assertIn("Unrated or conflicting: 2", counts["answer"])

    def test_agent_product_mapping_and_centered_search_remain_within_fixed_scope(self):
        rows = [review(review_body="x" * 2500 + " battery concern " + "y" * 2500)]
        sent = []
        def agent(payload):
            sent.append(payload)
            if len(sent) == 1:
                name, arguments = "get_stats", {}
            elif len(sent) == 2:
                products = json.loads(payload["messages"][-1]["content"])["products"]
                self.assertEqual(products, [{"product": "p000001", "asin": "B0TEST0001"}])
                name, arguments = "search_reviews", {"query": "battery concern", "rating": "all", "limit": 1}
            else:
                item = json.loads(payload["messages"][-1]["content"])["reviews"][0]
                self.assertLessEqual(sum(map(len, item["body"].values())), 600)
                self.assertIn("battery concern", "".join(item["body"].values()))
                return response(json.dumps({"answer": "The retrieved excerpt describes a battery concern.",
                    "evidence": [next(key for key, text in item["body"].items() if "battery concern" in text)], "limitations": []}))
            return response(tool_calls=[{"id": str(len(sent)), "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}])
        result = self.engine(agent).ask(rows, "Investigate this product's battery concern.", dataset="workspace", asin="B0TEST0001")
        self.assertEqual(result["usage"]["calls"], 3)
        self.assertIn("battery concern", result["evidence"][0]["quote"])
        for private in ("PRIVATE-AUTHOR", "PRIVATE-ORIGINAL-ID", "https://www.amazon.in"):
            self.assertNotIn(private, json.dumps(sent))

    @patch("llm_analysis.MAX_BATCH_BYTES", 24_000)
    def test_validated_chunk_explanations_and_limits_reach_merged_report(self):
        rows = [review(index, review_body="Battery " + "x" * 1000) for index in range(40)]
        report = self.engine().analyse(rows, dataset="workspace", asin="", depth="deep")
        self.assertIn("A cited example of battery use.", report["themes"][0]["explanation"])
        self.assertTrue(any("A test interpretation." in item for item in report["limitations"]))


if __name__ == "__main__":
    unittest.main()
