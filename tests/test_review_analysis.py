"""Check local statistics and provider contracts without contacting any provider."""

import copy
import json
import unittest
from unittest.mock import Mock, patch

import review_analysis as analysis


def review(index=1, **changes):
    return {"asin": "B0TEST0001", "review_id": f"R-{index}",
            "review_title": "A quiet fan", "review_body": "The battery lasts well. It is quiet on my desk.",
            "rating": "4", "reviewer_name": "PRIVATE AUTHOR", "review_date": "PRIVATE DATE",
            "product_url": "https://www.amazon.in/dp/B0TEST0001", **changes}


def api_response(report):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(report)}]}]}


def grounded_transport(payload):
    sent = json.loads(payload["input"][1]["content"])["reviews"]
    return api_response({"summary": "The cited reviews discuss battery life.", "themes": [{
        "label": "Battery life", "explanation": "These excerpts describe battery use.",
        "evidence": [{"key": row["key"], "quote": row["body"][:100]} for row in sent[:2]],
    }], "limitations": ["The sample is limited."]})


class ReviewAnalysisTests(unittest.TestCase):
    def test_empty_report_has_no_invented_topics_or_rating(self):
        actual = analysis.analyse_reviews([])
        self.assertEqual(actual["themes"], [])
        self.assertIsNone(actual["rating_summary"]["average"])
        self.assertEqual(actual["scope"]["total_reviews"], 0)
        self.assertEqual(actual["rating_summary"]["rated"], 0)
        self.assertEqual(actual["method"], "local")

    def test_statistics_keep_fractional_and_missing_ratings_exact(self):
        rows = [review(1, rating="4.25"), review(2, rating="2.5"), review(3, rating="2"), review(4, rating="")]
        actual = analysis.analyse_reviews(rows)["rating_summary"]
        self.assertEqual(actual, {"rated": 3, "unrated": 1, "average": 8.75 / 3,
                                  "positive": 1, "neutral": 1, "critical": 1})

    def test_topic_counts_include_every_matching_review_but_bound_excerpts(self):
        rows = [review(index, review_body="Battery battery battery.") for index in range(12)]
        actual = analysis.analyse_reviews(rows)
        battery = next(topic for topic in actual["themes"] if topic["label"] == "Battery and charging")
        self.assertEqual(battery["review_count"], 12)
        self.assertEqual(len(battery["evidence"]), analysis.MAX_THEME_EVIDENCE)
        self.assertEqual(battery["count_basis"], "all_keyword_matches")
        self.assertEqual(battery["rating_mix"]["positive"], 12)
        for citation in battery["evidence"]:
            original = next(row for row in rows if row["review_id"] == citation["review_id"])
            self.assertIn(citation["quote"], original["review_body"])

    def test_local_keywords_use_word_boundaries_and_are_honestly_labeled(self):
        actual = analysis.analyse_reviews([review(review_title="An evaluation", review_body="No subject terms here.")])
        self.assertFalse(any(topic["label"] == "Price and value" for topic in actual["themes"]))
        self.assertIn("keyword", " ".join(actual["limitations"]))

    def test_fingerprint_is_order_independent_and_detects_content_or_scope_changes(self):
        rows = [review(1), review(2)]
        before = analysis.report_fingerprint(rows)
        self.assertEqual(before, analysis.report_fingerprint(list(reversed(rows))))
        self.assertEqual(before, analysis.analyse_reviews(rows)["fingerprint"])
        self.assertNotEqual(before, analysis.report_fingerprint([review(1), review(2, review_body="Changed.")]))
        self.assertNotEqual(before, analysis.report_fingerprint([review(1), review(2, rating="3")]))
        self.assertNotEqual(before, analysis.report_fingerprint(rows, "demo"))

    def test_product_scope_and_composite_ids_are_preserved(self):
        rows = [review(1), review(1, asin="B0TEST0002", rating="1")]
        self.assertEqual(analysis.analyse_reviews(rows)["scope"]["total_reviews"], 2)
        actual = analysis.analyse_reviews(rows, asin="B0TEST0002")
        self.assertEqual(actual["scope"]["total_reviews"], 1)
        self.assertEqual(actual["rating_summary"]["average"], 1)
        self.assertEqual(analysis.analyse_reviews([review(1), review(1)])["scope"]["total_reviews"], 1)
        with self.assertRaises(analysis.AnalysisError):
            analysis.analyse_reviews([review(1), review(1, rating="1")])

    def test_invalid_ratings_and_scope_do_not_produce_misleading_stats(self):
        for invalid in (True, "nan", "inf", "0", "6", "no stars"):
            with self.subTest(rating=invalid), self.assertRaises(analysis.AnalysisError):
                analysis.analyse_reviews([review(rating=invalid)])
        with self.assertRaises(analysis.AnalysisError):
            analysis.analyse_reviews([], dataset="unknown")
        with self.assertRaises(analysis.AnalysisError):
            analysis.analyse_reviews([], asin="bad")

    def test_demo_is_explicitly_fictional_and_local_mode_never_reads_provider_settings(self):
        with patch.object(analysis.os, "environ", {}), patch.object(analysis, "_openai_request") as network:
            result = analysis.analyse_reviews([review()], dataset="demo")
        network.assert_not_called()
        self.assertIn("fictional", result["limitations"][0])

    def test_provider_settings_require_a_key_and_explicit_valid_model(self):
        for env in ({}, {"OPENAI_API_KEY": "test-secret"}, {"OPENAI_MODEL": "test-model"},
                    {"OPENAI_API_KEY": "test-secret", "OPENAI_MODEL": "bad\nmodel"}):
            with self.subTest(env=env), patch.object(analysis.os, "environ", env):
                self.assertFalse(analysis.provider_settings()["configured"])
        with patch.object(analysis.os, "environ", {"OPENAI_API_KEY": "test-secret", "OPENAI_MODEL": "test-model"}):
            self.assertEqual(analysis.provider_settings(), {"configured": True, "model": "test-model"})

    def test_ai_request_is_anonymous_bounded_and_statistics_are_local(self):
        rows = [review(1), review(2, rating="2"), review(3, rating="")]
        capture = Mock(side_effect=grounded_transport)
        with patch.object(analysis.os, "environ", {}):
            actual = analysis.analyse_with_openai(rows, model="test-model", transport=capture)
        payload = capture.call_args.args[0]
        serialized = json.dumps(payload)
        self.assertNotIn("PRIVATE AUTHOR", serialized)
        self.assertNotIn("PRIVATE DATE", serialized)
        self.assertNotIn("amazon.in", serialized)
        self.assertNotIn("B0TEST0001", serialized)
        self.assertNotIn('"review_id"', serialized)
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertLessEqual(payload["max_output_tokens"], 4000)
        self.assertEqual(actual["rating_summary"], analysis.analyse_reviews(rows)["rating_summary"])
        self.assertEqual(actual["themes"][0]["review_count"], 2)
        self.assertEqual(actual["themes"][0]["count_basis"], "cited_reviews")
        self.assertEqual(sum(actual["themes"][0]["rating_mix"].values()), 2)
        self.assertEqual(actual["method"], "openai")

    def test_ai_sampling_respects_count_char_caps_and_discloses_truncation(self):
        rows = [review(index, review_body="battery " * 1000) for index in range(160)]
        capture = Mock(side_effect=lambda payload: api_response({"summary": "Some evidence was provided.", "themes": [], "limitations": []}))
        actual = analysis.analyse_with_openai(rows, model="test-model", transport=capture)
        payload = capture.call_args.args[0]
        input_text = payload["input"][1]["content"]
        sent = json.loads(input_text)["reviews"]
        self.assertLessEqual(len(sent), analysis.MAX_AI_REVIEWS)
        self.assertLessEqual(len(input_text), analysis.MAX_AI_INPUT_CHARS)
        self.assertEqual(actual["scope"]["analysed_reviews"], len(sent))
        self.assertEqual(actual["scope"]["excluded_reviews"], len(rows) - len(sent))
        self.assertTrue(actual["scope"]["body_truncated"])
        self.assertTrue(any("shortened" in line for line in actual["limitations"]))
        self.assertEqual(actual["rating_summary"]["rated"], 160)

    def test_ai_sampling_gives_products_and_rating_groups_a_turn(self):
        rows = [review(index, asin=f"B0TEST000{product}", rating=str(rating))
                for index in range(130) for product in (1, 2) for rating in (1, 3, 5)]
        # IDs must remain unique within a product across rating groups.
        for index, row in enumerate(rows):
            row["review_id"] = f"ID-{index}"
        capture = Mock(side_effect=lambda payload: api_response({"summary": "A bounded sample.", "themes": [], "limitations": []}))
        analysis.analyse_with_openai(rows, model="test-model", transport=capture)
        sent = json.loads(capture.call_args.args[0]["input"][1]["content"])["reviews"]
        self.assertEqual(len(sent), analysis.MAX_AI_REVIEWS)
        self.assertEqual({(row["product"], row["rating"]) for row in sent[:6]},
                         {(product, rating) for product in ("p0001", "p0002") for rating in ("1", "3", "5")})

    def test_unknown_citations_changed_quotes_and_duplicate_evidence_are_rejected(self):
        for mutation in ("unknown", "invented", "duplicate", "no-evidence", "extra-field"):
            def invalid(payload, mutation=mutation):
                response = grounded_transport(payload)
                report = json.loads(response["output"][0]["content"][0]["text"])
                evidence = report["themes"][0]["evidence"]
                if mutation == "unknown":
                    evidence[0]["key"] = "not-a-review"
                elif mutation == "invented":
                    evidence[0]["quote"] = "This quote does not exist."
                elif mutation == "duplicate":
                    evidence.append(copy.deepcopy(evidence[0]))
                elif mutation == "no-evidence":
                    report["themes"][0]["evidence"] = []
                else:
                    report["untrusted_field"] = "ignored?"
                return api_response(report)
            with self.subTest(mutation=mutation), self.assertRaises(analysis.AnalysisError):
                analysis.analyse_with_openai([review(1), review(2)], model="test-model", transport=invalid)

    def test_quote_must_be_in_the_text_sent_not_an_omitted_body_tail(self):
        body = "battery " * 1000 + "PRIVATE OMITTED END"
        def tail_citation(payload):
            key = json.loads(payload["input"][1]["content"])["reviews"][0]["key"]
            return api_response({"summary": "A topic.", "themes": [{"label": "Tail", "explanation": "A claim.",
                "evidence": [{"key": key, "quote": "PRIVATE OMITTED END"}]}], "limitations": []})
        with self.assertRaises(analysis.AnalysisError):
            analysis.analyse_with_openai([review(review_body=body)], model="test-model", transport=tail_citation)

    def test_refusal_incomplete_invalid_json_and_transport_failure_are_honest_errors(self):
        responses = [
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No."}]}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "not JSON"}]}]},
        ]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(analysis.AnalysisError):
                analysis.analyse_with_openai([review()], model="test-model", transport=lambda payload: response)
        with self.assertRaises(analysis.AnalysisError) as raised:
            analysis.analyse_with_openai([review()], model="test-model", transport=Mock(side_effect=TimeoutError("secret")))
        self.assertNotIn("secret", str(raised.exception))

    def test_cancelled_or_empty_ai_requests_never_contact_provider(self):
        transport = Mock()
        with self.assertRaises(analysis.AnalysisCancelled):
            analysis.analyse_with_openai([review()], model="test-model", transport=transport, cancelled=lambda: True)
        with self.assertRaises(analysis.AnalysisError):
            analysis.analyse_with_openai([review(review_title="", review_body="")], model="test-model", transport=transport)
        transport.assert_not_called()
        state = iter([False, False, True])
        with self.assertRaises(analysis.AnalysisCancelled):
            analysis.analyse_with_openai([review()], model="test-model", transport=grounded_transport, cancelled=lambda: next(state))

    def test_default_adapter_uses_fixed_endpoint_timeout_and_response_size_limit(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"status": "completed", "output": []}).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(analysis.os, "environ", {"OPENAI_API_KEY": "test-secret"}), \
             patch.object(analysis.request, "build_opener", return_value=opener):
            analysis._openai_request({"model": "test-model"})
        outgoing = opener.open.call_args.args[0]
        self.assertEqual(outgoing.full_url, analysis.OPENAI_ENDPOINT)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], 45)
        response.read.assert_called_once_with(analysis.MAX_AI_RESPONSE_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
