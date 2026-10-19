"""The optional runner is checked with fictional responses, without network access."""

import json
from pathlib import Path
import tempfile
import unittest

from analysis_store import AnalysisStore
from evaluations.live import run_cases, select_cases
from evaluations.run import CASES
from llm_analysis import LLMEngine
from llm_provider import ProviderError, ProviderRegistry


class LiveEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.corpus = json.loads(CASES.read_text(encoding="utf-8"))
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def engine(self, transport):
        registry = ProviderRegistry(environ={}, transport=transport)
        registry.configure({"api_key": "synthetic-evaluation-key"})
        return LLMEngine(registry, AnalysisStore(Path(self.folder.name) / "analysis.sqlite3"))

    def test_case_selection_is_bounded_and_excludes_statistics_and_unknown_cases(self):
        for identities in [[], ["unknown"], ["stats_mixed_ratings"], ["battery_synonym"] * 2, ["battery_synonym"] * 5]:
            with self.subTest(identities=identities), self.assertRaises(ValueError):
                select_cases(self.corpus, identities)

    def test_accepted_answers_are_source_checked_but_not_marked_semantically_verified(self):
        calls = []
        def transport(payload):
            calls.append(payload)
            packet = json.loads(payload["messages"][-1]["content"])
            row = packet["review_evidence"]["reviews"][0]
            label = next(iter(row["body"] or row["title"]))
            answer = {"answer": "One account describes battery backup.", "evidence": [label], "limitations": ["A selected account."]}
            return {"model": "fictional/evaluation:free", "usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0},
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}
        engine = self.engine(transport)
        cases = select_cases(self.corpus, ["battery_synonym"])
        report = run_cases(cases, engine)
        self.assertEqual(report["model_calls"], 1)
        self.assertEqual(report["results"][0]["status"], "accepted_ai")
        self.assertTrue(report["results"][0]["source_check"]["passed"])
        self.assertEqual(report["results"][0]["human_review"], "pending")
        self.assertEqual(report["semantic_accuracy"], "not_measured")
        cached = run_cases(cases, engine)
        self.assertEqual(cached["model_calls"], 0)
        self.assertEqual(len(calls), 1)

    def test_provider_limit_stops_remaining_cases_without_another_attempt(self):
        calls = []
        def unavailable(payload):
            calls.append(payload)
            raise ProviderError("Synthetic limit", code="rate_limit")
        report = run_cases(select_cases(self.corpus, ["battery_synonym", "negated_defect"]), self.engine(unavailable))
        self.assertEqual(report["stopped_reason"], "rate_limit")
        self.assertEqual(report["results"][1]["status"], "not_run")
        self.assertEqual(len(calls), 1)
