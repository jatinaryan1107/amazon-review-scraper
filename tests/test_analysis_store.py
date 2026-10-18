"""Exercise the local ledger and cache without real time waits or network."""

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from analysis_store import AnalysisStore
from llm_provider import ProviderError
from review_analysis import AnalysisCancelled


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "analysis.sqlite3"
        self.now = [1_800_000_000.0]
        self.sleeps = []
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now[0] += max(seconds, 0.001)
        self.store = AnalysisStore(self.path, clock=lambda: self.now[0], sleep=sleep)

    def test_cache_round_trip_and_failed_uncertain_attempts_count_toward_quota(self):
        key = "a" * 64
        self.store.put_chunk(key, {"validated": True})
        self.assertEqual(self.store.get_chunk(key), {"validated": True})
        first = self.store.reserve_attempt(key)
        self.store.finish_attempt(first, success=False)
        self.store.reserve_attempt(key)
        self.assertEqual(self.store.daily_remaining(), 48)
        self.assertGreaterEqual(sum(self.sleeps), 3)
        reopened = AnalysisStore(self.path, clock=lambda: self.now[0])
        self.assertEqual(reopened.daily_remaining(), 48)

    def test_daily_limit_and_cancel_do_not_create_extra_attempts(self):
        for _ in range(50):
            self.store.reserve_attempt("b" * 64)
        with self.assertRaises(ProviderError) as caught:
            self.store.reserve_attempt("b" * 64)
        self.assertEqual(caught.exception.code, "daily_limit")
        with self.assertRaises(AnalysisCancelled):
            self.store.reserve_attempt("b" * 64, cancelled=lambda: True)
        self.assertEqual(self.store.daily_remaining(), 0)

    def test_rate_limit_backoff_survives_restart_and_reserves_no_attempt_during_wait(self):
        identity = self.store.reserve_attempt("d" * 64)
        self.store.finish_attempt(identity, error=ProviderError("private", code="rate_limit", retry_after=20))
        reopened = AnalysisStore(self.path, clock=lambda: self.now[0])
        self.assertEqual(reopened.retry_at(), self.now[0] + 20)
        with self.assertRaises(ProviderError) as caught:
            reopened.reserve_attempt("e" * 64)
        self.assertEqual(caught.exception.code, "cooldown")
        self.assertEqual(reopened.daily_remaining(), 49)
        self.now[0] += 21
        self.assertIsNone(reopened.retry_at())
        reopened.reserve_attempt("e" * 64)
        self.assertEqual(reopened.daily_remaining(), 48)

    def test_reported_zero_remaining_honors_reset_but_malformed_backoff_is_not_trusted(self):
        identity = self.store.reserve_attempt("f" * 64)
        self.store.finish_attempt(identity, error=ProviderError("private", code="rate_limit", diagnostics={
            "remaining": 0, "reset_at": int(self.now[0] + 120)}))
        self.assertEqual(self.store.retry_at(), self.now[0] + 120)

    def test_incompatible_schema_is_rejected_before_any_mutation(self):
        wrong = self.path.parent / "wrong.sqlite3"
        with closing(sqlite3.connect(wrong)) as connection, connection:
            connection.execute("CREATE TABLE chunks (key TEXT PRIMARY KEY, payload TEXT)")
            connection.execute("PRAGMA user_version=1")
        before = wrong.read_bytes()
        with self.assertRaises(ValueError):
            AnalysisStore(wrong)
        self.assertEqual(wrong.read_bytes(), before)

    def test_corrupt_json_and_nonfinite_payload_are_rejected(self):
        key = "c" * 64
        with self.assertRaises(ValueError):
            self.store.put_chunk(key, {"value": float("nan")})
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO chunks VALUES (?,?,?)", (key, '{"x":1,"x":2}', self.now[0]))
        with self.assertRaises(ValueError):
            self.store.get_chunk(key)


if __name__ == "__main__":
    unittest.main()
