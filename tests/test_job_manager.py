"""Exercise durable local jobs using fake collectors and no external services."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from dashboard_data import DashboardData
from job_manager import AutomationService, JobConflict, TERMINAL
from review_analysis import AnalysisError, analyse_reviews
from review_store import ReviewStore
from llm_provider import ProviderError, ProviderRegistry


def review(index=1, **changes):
    return {"asin": "B0TEST0001", "product_url": "https://www.amazon.in/dp/B0TEST0001",
            "review_id": f"JOB-{index}", "reviewer_name": "Fictional test reviewer",
            "verified_purchase": "true", "review_title": "Quiet test fan", "rating": "4",
            "review_date": "Test date", "review_body": "The battery works and the controls are easy.", **changes}


def collection_result(status="completed", **changes):
    return {"status": status, "inserted": 1, "pages_processed": 1,
            "products": [{"asin": "B0TEST0001", "status": "complete", "inserted": 1, "pages_processed": 1}],
            "metrics": {"browser_pages": 1, "http_pages": 0}, **changes}


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.partial_once = False
        self.failure = None
        self.entered = self.release = None

    def cache_discriminator(self, depth="quick"):
        return f"openrouter:{depth}:v1:test-policy"

    def plan(self, rows, *, dataset, asin, depth="quick"):
        return {"depth": depth, "total_reviews": len(rows), "calls_needed": 1 if rows else 0,
                "provider": "openrouter", "model": "openrouter/free", "free_only": True}

    def analyse(self, rows, *, dataset, asin, depth, cancelled, on_progress):
        self.calls.append(("analysis", depth))
        if self.failure:
            raise self.failure
        if self.entered:
            self.entered.set()
            self.release.wait(3)
        on_progress({"chunks_done": 1, "chunks_total": 2, "calls": 1, "private_key": "must-not-be-saved"})
        report = analyse_reviews(rows, dataset=dataset, asin=asin)
        complete = not self.partial_once
        self.partial_once = False
        report.update(method="openrouter", method_label="Free AI analysis", model="openrouter/free", usage={"calls": 1})
        report["scope"].update(analysis_complete=complete, full_coverage=complete and depth == "deep")
        return report

    def ask(self, rows, question, *, dataset, asin, cancelled, on_progress, allow_external=False, strategy="focused"):
        self.calls.append(("research", question))
        if self.failure:
            raise self.failure
        if self.entered:
            self.entered.set()
            self.release.wait(3)
        return {"question": question, "answer": "A checked test answer.", "evidence": [],
                "limitations": [], "scope": {"total_reviews": len(rows)}, "usage": {"calls": 0},
                "steps": [{"kind": "local", "name": "get_stats"}], "method": "local", "model": "",
                "fingerprint": "test", "api_key": "must-not-be-saved"}


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.services = []
        self.browser_guard = patch("collection_runtime._playwright_factory", side_effect=AssertionError("A live browser must never start in these tests."))
        self.browser_guard.start()
        self.addCleanup(self.browser_guard.stop)
        self.provider_guard = patch("review_analysis._openai_request", side_effect=AssertionError("A real provider must never be called in these tests."))
        self.provider_guard.start()
        self.addCleanup(self.provider_guard.stop)
        self.addCleanup(self.close_services)

    def close_services(self):
        for service in reversed(self.services):
            service.close()

    def service(self, folder=None, **options):
        folder = folder or self.folder / "data"
        data = DashboardData(folder)
        defaults = {"settings": lambda: {"configured": False, "model": ""},
                    "session_reader": lambda path: {"exists": path.exists(), "valid": path.exists(), "detail": "Synthetic test session."},
                    "provider_registry": ProviderRegistry(environ={})}
        defaults.update(options)
        service = AutomationService(data, self.folder / "synthetic-auth.json", **defaults)
        self.services.append(service)
        return service

    def wait_job(self, service, job_id, condition=None, timeout=4):
        condition = condition or (lambda job: job["status"] in TERMINAL)
        deadline = time.monotonic() + timeout
        pause = threading.Event()
        while time.monotonic() < deadline:
            job = service.get_job(job_id)
            if condition(job):
                return job
            pause.wait(0.01)
        self.fail(f"Job did not reach its expected state: {service.get_job(job_id)}")

    @staticmethod
    def collection_payload(**changes):
        return {"kind": "collection", "urls": ["https://www.amazon.in/dp/B0TEST0001"], **changes}

    @staticmethod
    def seed(service, rows=None):
        with ReviewStore(service.data.path("workspace")) as store:
            store.upsert_reviews(rows or [review()], source="synthetic-test")

    def test_submission_canonicalizes_and_deduplicates_before_page_budget(self):
        service = self.service(autostart=False)
        job = service.submit(self.collection_payload(urls=[
            " https://amazon.in/a-product/dp/B0TEST0001?ref=test ",
            "https://www.amazon.in/product-reviews/B0TEST0001?pageNumber=2",
        ], max_pages=20, transport="hybrid", resume=False))
        self.assertEqual(job["options"]["urls"], ["https://www.amazon.in/dp/B0TEST0001"])
        self.assertEqual(job["progress"]["products_total"], 1)
        self.assertEqual(job["options"]["transport"], "hybrid")
        self.assertFalse(job["options"]["resume"])
        self.assertFalse(job["options"]["allow_external"])
        self.assertEqual(job["options"]["analysis_method"], "local")

    def test_submission_rejects_invalid_urls_options_and_page_budgets(self):
        service = self.service(autostart=False)
        invalid = [None, [], {}, {"kind": "unknown"}, {"kind": "auth", "extra": True},
                   self.collection_payload(urls=[]), self.collection_payload(urls="https://www.amazon.in/dp/B0TEST0001"),
                   self.collection_payload(urls=["http://www.amazon.in/dp/B0TEST0001"]),
                   self.collection_payload(urls=["https://www.amazon.in.evil.test/dp/B0TEST0001"]),
                   self.collection_payload(urls=["https://user:password@www.amazon.in/dp/B0TEST0001"]),
                   self.collection_payload(urls=[42]), self.collection_payload(urls=["x" * 2049]),
                   self.collection_payload(urls=[f"https://www.amazon.in/dp/B0TEST00{i:02}" for i in range(11)]),
                   self.collection_payload(max_pages=0), self.collection_payload(max_pages=21),
                   self.collection_payload(max_pages=True), self.collection_payload(max_pages="3"),
                   self.collection_payload(urls=[f"https://www.amazon.in/dp/B0TEST000{i}" for i in (1, 2, 3)], max_pages=20),
                   self.collection_payload(resume="true"), self.collection_payload(analyse=1),
                   self.collection_payload(transport="fast"), {"kind": "analysis", "asin": "invalid"},
                   {"kind": "analysis", "dataset": "other"}, {"kind": "analysis", "allow_external": "yes"}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                service.submit(payload)
        self.assertIsNone(service.status()["active_job"])

    def test_unhashable_option_values_are_validation_errors(self):
        service = self.service(autostart=False)
        for payload in ({"kind": []}, {"kind": {}}, {"kind": "analysis", "analysis_method": []},
                        {"kind": "analysis", "dataset": []}, self.collection_payload(transport={})):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                service.submit(payload)

    def test_openai_requires_both_explicit_consent_and_configuration(self):
        ai = Mock()
        service = self.service(autostart=False, ai_analyser=ai,
                               settings=lambda: {"configured": True, "model": "test-model"})
        with self.assertRaisesRegex(ValueError, "Confirm"):
            service.submit({"kind": "analysis", "analysis_method": "openai"})
        with self.assertRaisesRegex(ValueError, "separately"):
            service.submit(self.collection_payload(analysis_method="openai", allow_external=True))
        other = self.service(folder=self.folder / "other", autostart=False, ai_analyser=ai)
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            other.submit({"kind": "analysis", "analysis_method": "openai", "allow_external": True})
        ai.assert_not_called()

    def test_only_one_job_and_one_server_can_use_the_same_data_folder(self):
        service = self.service(autostart=False)
        first = service.submit({"kind": "auth"})
        with self.assertRaises(JobConflict):
            service.submit(self.collection_payload())
        with self.assertRaises(JobConflict):
            self.service(folder=service.data.data_dir, autostart=False)
        service.cancel(first["id"])
        second = service.submit({"kind": "auth"})
        self.assertNotEqual(first["id"], second["id"])

    def test_fake_collection_saves_progress_then_generates_a_durable_local_report(self):
        entered, release = threading.Event(), threading.Event()
        def collect(urls, db_path, auth_file, **options):
            self.assertEqual(urls, ["https://www.amazon.in/dp/B0TEST0001"])
            with ReviewStore(db_path) as store:
                store.save_page("B0TEST0001", [review()], next_url=None, pages_completed=1, status="complete", source="synthetic-test")
            options["on_progress"]({"asin": "B0TEST0001", "status": "complete", "pages_processed": 1, "inserted": 1})
            entered.set()
            self.assertTrue(release.wait(3), "The test did not release its fake collector.")
            return collection_result()
        self.addCleanup(release.set)
        local = Mock(wraps=analyse_reviews)
        service = self.service(collection_runner=collect, local_analyser=local)
        job = service.submit(self.collection_payload())
        self.assertTrue(entered.wait(3))
        progress = service.get_job(job["id"])
        self.assertEqual(progress["phase"], "collecting")
        self.assertEqual(progress["progress"]["reviews_inserted"], 1)
        self.assertEqual(progress["progress"]["pages_processed"], 1)
        self.assertEqual(progress["progress"]["products_done"], 1)
        release.set()
        finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "succeeded", finished)
        self.assertTrue(finished["result"]["analysis_id"])
        self.assertFalse(finished["result"]["cached"])
        local.assert_called_once()
        latest = service.latest_report("workspace")
        self.assertFalse(latest["stale"])
        self.assertEqual(latest["report"]["scope"]["total_reviews"], 1)
        self.assertEqual(latest["report"]["method"], "local")
        service.close()
        restarted = self.service(folder=service.data.data_dir, autostart=False)
        self.assertEqual(restarted.get_job(job["id"])["status"], "succeeded")
        self.assertEqual(restarted.latest_report("workspace")["report"], latest["report"])

    def test_analysis_cache_is_reused_and_new_reviews_mark_a_report_stale(self):
        analyser = Mock(wraps=analyse_reviews)
        service = self.service(local_analyser=analyser)
        self.seed(service)
        first = service.submit({"kind": "analysis"})
        first = self.wait_job(service, first["id"])
        self.assertEqual(first["status"], "succeeded", first)
        second = service.submit({"kind": "analysis"})
        second = self.wait_job(service, second["id"])
        self.assertTrue(second["result"]["cached"])
        self.assertEqual(first["result"]["analysis_id"], second["result"]["analysis_id"])
        self.assertEqual(analyser.call_count, 1)
        self.assertFalse(service.latest_report("workspace")["stale"])
        self.seed(service, [review(2, rating="1")])
        self.assertTrue(service.latest_report("workspace")["stale"])
        third = service.submit({"kind": "analysis"})
        third = self.wait_job(service, third["id"])
        self.assertFalse(third["result"]["cached"])
        self.assertEqual(analyser.call_count, 2)
        self.assertFalse(service.latest_report("workspace")["stale"])
        self.assertEqual(service.latest_report("workspace")["report"]["scope"]["total_reviews"], 2)
        self.assertEqual(service.latest_report("workspace", "B0TEST0001"), {"report": None, "stale": False})

    def test_cancelled_queued_jobs_never_run_and_survive_restart(self):
        collect = Mock()
        service = self.service(autostart=False, collection_runner=collect)
        job = service.submit(self.collection_payload())
        cancelled = service.cancel(job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(service.cancel(job["id"])["status"], "cancelled")
        service.close()
        restarted = self.service(folder=service.data.data_dir, collection_runner=collect)
        self.assertEqual(restarted.get_job(job["id"])["status"], "cancelled")
        collect.assert_not_called()

    def test_running_cancellation_retains_saved_rows_and_a_resumable_checkpoint(self):
        entered = threading.Event()
        pause = threading.Event()
        def collect(urls, db_path, auth_file, **options):
            with ReviewStore(db_path) as store:
                store.save_page("B0TEST0001", [review()], next_url="https://www.amazon.in/product-reviews/B0TEST0001?pageNumber=2",
                                pages_completed=1, status="in_progress", source="synthetic-test")
            options["on_progress"]({"asin": "B0TEST0001", "status": "in_progress", "pages_processed": 1, "inserted": 1})
            entered.set()
            deadline = time.monotonic() + 3
            while not options["cancelled"]() and time.monotonic() < deadline:
                pause.wait(0.01)
            self.assertTrue(options["cancelled"](), "Cancellation was not delivered to the fake collector.")
            with ReviewStore(db_path) as store:
                checkpoint = store.get_checkpoint("B0TEST0001")
                store.save_page("B0TEST0001", [], next_url=checkpoint["next_url"], pages_completed=1,
                                status="paused", detail="Stopped by the test.", source="synthetic-test")
            return collection_result("paused", cancelled=True)
        local = Mock(wraps=analyse_reviews)
        service = self.service(collection_runner=collect, local_analyser=local)
        job = service.submit(self.collection_payload())
        self.assertTrue(entered.wait(3))
        self.assertEqual(service.cancel(job["id"])["status"], "cancelling")
        finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "cancelled")
        with ReviewStore(service.data.path("workspace"), read_only=True) as store:
            self.assertEqual(store.summary()["total_reviews"], 1)
            self.assertEqual(store.get_checkpoint("B0TEST0001")["status"], "paused")
        local.assert_not_called()
        self.assertIsNone(service.latest_report("workspace")["report"])

    def test_access_block_stops_without_automatic_analysis(self):
        def blocked(urls, db_path, auth_file, **options):
            with ReviewStore(db_path) as store:
                store.save_page("B0TEST0001", [review()], next_url="https://www.amazon.in/product-reviews/B0TEST0001",
                                pages_completed=1, status="blocked", detail="Sign-in is required.", source="synthetic-test")
            options["on_progress"]({"asin": "B0TEST0001", "status": "blocked", "pages_processed": 1, "inserted": 1})
            return collection_result("blocked", detail="Sign-in is required.")
        local = Mock(wraps=analyse_reviews)
        service = self.service(collection_runner=blocked, local_analyser=local)
        job = service.submit(self.collection_payload())
        finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "blocked", finished)
        self.assertEqual(service.status()["auth"]["status"], "blocked")
        self.assertFalse(service.status()["auth"]["verified"])
        local.assert_not_called()
        self.assertIsNone(service.latest_report("workspace")["report"])

    def test_collection_success_is_preserved_when_auto_analysis_fails(self):
        def collect(urls, db_path, auth_file, **options):
            with ReviewStore(db_path) as store:
                store.upsert_reviews([review()], source="synthetic-test")
            return collection_result()
        service = self.service(collection_runner=collect, local_analyser=Mock(side_effect=ValueError("Synthetic analysis failure.")))
        with self.assertLogs("job_manager", level="ERROR"):
            job = service.submit(self.collection_payload())
            finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertIn("analysis_error", finished["result"])
        self.assertIn("separately", finished["detail"])
        self.assertIsNone(service.latest_report("workspace")["report"])

    def test_manual_sign_in_only_saves_after_finish_and_can_be_cancelled(self):
        ready = threading.Event()
        waiting = threading.Event()
        def authenticate(path, finished, cancelled, *, on_progress):
            on_progress({"status": "awaiting_login", "detail": "Use the synthetic sign-in window."})
            ready.set()
            deadline = time.monotonic() + 3
            while not finished() and not cancelled() and time.monotonic() < deadline:
                waiting.wait(0.01)
            if cancelled():
                return {"status": "cancelled", "detail": "Synthetic sign-in stopped."}
            self.assertTrue(finished())
            path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
            return {"status": "saved", "detail": "Synthetic session saved."}
        service = self.service(auth_runner=authenticate)
        first = service.submit({"kind": "auth"})
        self.assertTrue(ready.wait(3))
        self.assertFalse(service.auth_file.exists())
        self.assertEqual(service.get_job(first["id"])["phase"], "awaiting_login")
        self.assertTrue(service.status()["auth"]["busy"])
        service.finish_auth(first["id"])
        self.assertEqual(self.wait_job(service, first["id"])["status"], "succeeded")
        self.assertTrue(service.auth_file.exists())
        self.assertEqual(service.status()["auth"]["status"], "saved")
        with self.assertRaises(JobConflict):
            service.finish_auth(first["id"])
        ready.clear()
        second = service.submit({"kind": "auth"})
        self.assertTrue(ready.wait(3))
        service.cancel(second["id"])
        self.assertEqual(self.wait_job(service, second["id"])["status"], "cancelled")

    def test_restart_marks_unfinished_jobs_interrupted_and_never_autoruns_them(self):
        for status in ("queued", "running", "cancelling"):
            with self.subTest(status=status):
                folder = self.folder / status
                old = self.service(folder=folder, autostart=False)
                job = old.submit({"kind": "auth"})
                with closing(sqlite3.connect(old.path)) as connection, connection:
                    connection.execute("UPDATE jobs SET status=?, phase='awaiting_login' WHERE id=?", (status, job["id"]))
                old.close()
                authenticate = Mock()
                new = self.service(folder=folder, auth_runner=authenticate)
                interrupted = new.get_job(job["id"])
                self.assertEqual(interrupted["status"], "interrupted")
                self.assertIn("resume", interrupted["detail"])
                self.assertIsNone(new.status()["active_job"])
                authenticate.assert_not_called()

    def test_explicit_provider_analysis_is_cached_and_never_calls_the_real_adapter(self):
        def fake_ai(rows, *, dataset, asin, model, cancelled):
            self.assertEqual(model, "test-model")
            self.assertFalse(cancelled())
            report = analyse_reviews(rows, dataset=dataset, asin=asin)
            report.update(method="openai", method_label="Synthetic AI test", model=model)
            return report
        provider = Mock(side_effect=fake_ai)
        service = self.service(ai_analyser=provider, settings=lambda: {"configured": True, "model": "test-model"})
        self.seed(service)
        payload = {"kind": "analysis", "analysis_method": "openai", "allow_external": True}
        first = service.submit(payload)
        first = self.wait_job(service, first["id"])
        self.assertEqual(first["status"], "succeeded", first)
        second = service.submit(payload)
        second = self.wait_job(service, second["id"])
        self.assertTrue(second["result"]["cached"])
        self.assertEqual(first["result"]["analysis_id"], second["result"]["analysis_id"])
        provider.assert_called_once()
        self.assertEqual(service.latest_report("workspace")["report"]["method"], "openai")

    def test_provider_failure_and_empty_scope_save_no_report(self):
        provider = Mock(side_effect=AnalysisError("The synthetic provider declined this report."))
        service = self.service(ai_analyser=provider, settings=lambda: {"configured": True, "model": "test-model"})
        with self.assertLogs("job_manager", level="ERROR"):
            empty = service.submit({"kind": "analysis"})
            empty = self.wait_job(service, empty["id"])
        self.assertEqual(empty["status"], "failed")
        self.assertIn("no reviews", empty["detail"])
        self.seed(service)
        with self.assertLogs("job_manager", level="ERROR"):
            job = service.submit({"kind": "analysis", "analysis_method": "openai", "allow_external": True})
            failed = self.wait_job(service, job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("declined", failed["error"])
        self.assertIsNone(service.latest_report("workspace")["report"])

    def test_cancellation_wins_when_final_success_update_is_already_in_flight(self):
        service = self.service()
        self.seed(service)
        terminal_ready, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_update = service._update
        def pause_final_update(job_id, **changes):
            if changes.get("status") == "succeeded":
                terminal_ready.set()
                self.assertTrue(release.wait(3), "The final status update was not released.")
            return original_update(job_id, **changes)
        with patch.object(service, "_update", side_effect=pause_final_update):
            job = service.submit({"kind": "analysis"})
            self.assertTrue(terminal_ready.wait(3))
            self.assertEqual(service.cancel(job["id"])["status"], "cancelling")
            release.set()
            terminal = self.wait_job(service, job["id"])
        self.assertEqual(terminal["status"], "cancelled")
        # The report was committed before cancellation reached finalization.
        self.assertIsNotNone(service.latest_report("workspace")["report"])

    def test_cancelling_analysis_before_it_returns_saves_no_partial_report(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow_local(rows, *, dataset, asin):
            entered.set()
            self.assertTrue(release.wait(3), "The fake analysis was not released.")
            return analyse_reviews(rows, dataset=dataset, asin=asin)
        service = self.service(local_analyser=slow_local)
        self.seed(service)
        job = service.submit({"kind": "analysis"})
        self.assertTrue(entered.wait(3))
        service.cancel(job["id"])
        release.set()
        finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertIsNone(finished["result"]["analysis_id"])
        self.assertIsNone(service.latest_report("workspace")["report"])

    def test_unexpected_provider_exception_does_not_expose_internal_details(self):
        service = self.service(ai_analyser=Mock(side_effect=RuntimeError("synthetic private provider detail")),
                               settings=lambda: {"configured": True, "model": "test-model"})
        self.seed(service)
        with self.assertLogs("job_manager", level="ERROR"):
            job = service.submit({"kind": "analysis", "analysis_method": "openai", "allow_external": True})
            failed = self.wait_job(service, job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("private provider", failed["detail"])
        self.assertNotIn("private provider", failed["error"])
        self.assertIsNone(service.latest_report("workspace")["report"])

    def test_reconnecting_a_session_clears_the_previous_access_block(self):
        def blocked(urls, db_path, auth_file, **options):
            return collection_result("blocked", detail="Synthetic session expired.")
        def reconnect(path, finished, cancelled, *, on_progress):
            path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
            return {"status": "saved", "detail": "A synthetic replacement session was saved."}
        service = self.service(collection_runner=blocked, auth_runner=reconnect)
        collection = service.submit(self.collection_payload())
        self.assertEqual(self.wait_job(service, collection["id"])["status"], "blocked")
        self.assertEqual(service.status()["auth"]["status"], "blocked")
        auth = service.submit({"kind": "auth"})
        self.assertEqual(self.wait_job(service, auth["id"])["status"], "succeeded")
        self.assertEqual(service.status()["auth"]["status"], "saved")
        self.assertFalse(service.status()["auth"]["verified"])
        again = service.submit(self.collection_payload())
        self.assertEqual(self.wait_job(service, again["id"])["status"], "blocked")
        self.assertEqual(service.status()["auth"]["status"], "blocked")

    def test_cached_local_report_becomes_current_after_a_newer_ai_report(self):
        def fake_ai(rows, *, dataset, asin, model, cancelled):
            report = analyse_reviews(rows, dataset=dataset, asin=asin)
            report.update(method="openai", method_label="Synthetic AI test", model=model)
            return report
        local = Mock(wraps=analyse_reviews)
        service = self.service(local_analyser=local, ai_analyser=fake_ai,
                               settings=lambda: {"configured": True, "model": "test-model"})
        self.seed(service)
        local_job = service.submit({"kind": "analysis"})
        local_job = self.wait_job(service, local_job["id"])
        ai_job = service.submit({"kind": "analysis", "analysis_method": "openai", "allow_external": True})
        self.assertEqual(self.wait_job(service, ai_job["id"])["status"], "succeeded")
        self.assertEqual(service.latest_report("workspace")["report"]["method"], "openai")
        reused = service.submit({"kind": "analysis"})
        reused = self.wait_job(service, reused["id"])
        self.assertTrue(reused["result"]["cached"])
        self.assertEqual(reused["result"]["analysis_id"], local_job["result"]["analysis_id"])
        self.assertEqual(service.latest_report("workspace")["report"]["method"], "local")
        local.assert_called_once()

    def test_auth_failure_states_are_not_reported_as_access_blocks(self):
        for result_status in ("failed", "paused", "blocked"):
            with self.subTest(result_status=result_status):
                service = self.service(folder=self.folder / result_status,
                                       auth_runner=lambda *args, value=result_status, **kwargs: {"status": value, "detail": "Synthetic outcome."})
                job = service.submit({"kind": "auth"})
                self.assertEqual(self.wait_job(service, job["id"])["status"], result_status)

    def test_future_and_unrelated_jobs_databases_are_rejected_without_changes(self):
        for kind in ("future", "unrelated"):
            with self.subTest(kind=kind):
                folder = self.folder / kind
                folder.mkdir()
                path = folder / "jobs.sqlite3"
                with closing(sqlite3.connect(path)) as connection, connection:
                    if kind == "future":
                        connection.execute("PRAGMA user_version = 99")
                    else:
                        connection.execute("CREATE TABLE unrelated (content TEXT)")
                        connection.execute("INSERT INTO unrelated VALUES ('preserve this data')")
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    self.service(folder=folder, autostart=False)
                self.assertEqual(path.read_bytes(), before)
                # Failed startup must release its worker lock for a repaired folder.
                path.unlink()
                repaired = self.service(folder=folder, autostart=False)
                self.assertEqual(repaired.status()["jobs"], [])

    def test_malformed_known_jobs_schema_is_rejected_without_changes(self):
        for table in ("jobs", "reports"):
            with self.subTest(table=table):
                folder = self.folder / f"malformed-{table}"
                folder.mkdir()
                path = folder / "jobs.sqlite3"
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, unrelated TEXT)")
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    self.service(folder=folder, autostart=False)
                self.assertEqual(path.read_bytes(), before)

    def test_known_jobs_tables_require_their_durable_primary_key(self):
        for table in ("jobs", "reports"):
            with self.subTest(table=table):
                folder = self.folder / f"missing-key-{table}"
                service = self.service(folder=folder, autostart=False)
                service.close()
                with closing(sqlite3.connect(service.path)) as connection, connection:
                    # CREATE TABLE AS keeps the columns while removing key constraints.
                    connection.execute(f"ALTER TABLE {table} RENAME TO old_table")
                    connection.execute(f"CREATE TABLE {table} AS SELECT * FROM old_table")
                    connection.execute("DROP TABLE old_table")
                before = service.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.service(folder=folder, autostart=False)
                self.assertEqual(service.path.read_bytes(), before)

    def test_unknown_job_controls_and_shutdown_are_explicit(self):
        service = self.service(autostart=False)
        with self.assertRaises(LookupError):
            service.cancel("missing")
        with self.assertRaises(LookupError):
            service.finish_auth("missing")
        job = service.submit({"kind": "analysis"})
        with self.assertRaises(JobConflict):
            service.finish_auth(job["id"])
        service.close()
        with self.assertRaises(JobConflict):
            service.submit({"kind": "auth"})

    def router_service(self, *, configured=True, engine=None, **options):
        registry = ProviderRegistry(environ={})
        if configured:
            registry.configure({"api_key": "synthetic-session-key"})
        return self.service(provider_registry=registry, llm_engine=engine or FakeLLM(), **options)

    def test_provider_metadata_configuration_and_privacy_never_persist_a_key(self):
        service = self.router_service(configured=False, autostart=False)
        self.assertFalse(service.provider_metadata()["configured"])
        metadata = service.configure_provider({"api_key": "synthetic-private-key", "require_zdr": True})
        self.assertTrue(metadata["configured"])
        self.assertTrue(metadata["require_zdr"])
        self.assertEqual(metadata["key_source"], "session")
        self.assertNotIn("private-key", json.dumps(metadata))
        self.assertFalse(service.configure_provider({"require_zdr": False})["require_zdr"])
        self.assertFalse(service.configure_provider({"clear": True})["configured"])
        self.assertNotIn(b"synthetic-private-key", service.path.read_bytes())
        for payload in [{}, {"api_key": ""}, {"api_key": "x", "clear": True}, {"clear": 1},
                        {"require_zdr": "yes"}, {"api_key": "line\nbreak"}, {"model": "paid-model"}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                service.configure_provider(payload)

    def test_router_requires_sharing_configuration_and_valid_depth(self):
        service = self.router_service(autostart=False)
        for payload in [{"kind": "analysis", "analysis_method": "openrouter"},
                        {"kind": "analysis", "analysis_method": "openrouter", "allow_external": True, "depth": "all"},
                        {"kind": "analysis", "depth": "quick"},
                        self.collection_payload(analysis_method="openrouter", allow_external=True)]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                service.submit(payload)
        other = self.router_service(folder=self.folder / "unconfigured", configured=False, autostart=False)
        with self.assertRaisesRegex(ValueError, "Configure"):
            other.submit({"kind": "analysis", "analysis_method": "openrouter", "allow_external": True})

    def test_router_cache_separates_depth_and_reuses_only_completed_reports(self):
        engine = FakeLLM()
        service = self.router_service(engine=engine)
        self.seed(service)
        base = {"kind": "analysis", "analysis_method": "openrouter", "allow_external": True}
        for depth in ("quick", "deep", "quick", "deep"):
            job = service.submit({**base, "depth": depth})
            finished = self.wait_job(service, job["id"])
            self.assertEqual(finished["status"], "succeeded", finished)
        self.assertEqual(engine.calls, [("analysis", "quick"), ("analysis", "deep")])
        latest = service.latest_report("workspace")["report"]
        self.assertEqual(latest["method"], "openrouter")
        self.assertNotIn("must-not-be-saved", json.dumps(service.get_job(job["id"])))
        self.assertEqual(service.get_job(job["id"])["result"]["cached"], True)

    def test_partial_deep_report_is_saved_honestly_and_never_reused_as_complete(self):
        engine = FakeLLM()
        engine.partial_once = True
        service = self.router_service(engine=engine)
        self.seed(service)
        payload = {"kind": "analysis", "analysis_method": "openrouter", "allow_external": True, "depth": "deep"}
        first = service.submit(payload)
        first = self.wait_job(service, first["id"])
        self.assertEqual(first["status"], "paused")
        self.assertTrue(first["result"]["partial"])
        self.assertFalse(service.latest_report("workspace")["report"]["scope"]["analysis_complete"])
        second = service.submit(payload)
        second = self.wait_job(service, second["id"])
        self.assertEqual(second["status"], "succeeded")
        self.assertFalse(second["result"]["cached"])
        self.assertEqual(len(engine.calls), 2)

    def test_completed_deep_report_can_disclose_variant_gaps_without_pausing_forever(self):
        engine = FakeLLM()
        analyse = engine.analyse
        def conflicting_report(*args, **kwargs):
            report = analyse(*args, **kwargs)
            report["scope"].update(analysis_complete=True, partial=False, full_coverage=False, conflicting_variants=1)
            return report
        engine.analyse = conflicting_report
        service = self.router_service(engine=engine)
        self.seed(service)
        payload = {"kind": "analysis", "analysis_method": "openrouter", "allow_external": True, "depth": "deep"}
        for _ in range(2):
            job = service.submit(payload)
            finished = self.wait_job(service, job["id"])
            self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(len(engine.calls), 1)
        self.assertFalse(service.latest_report("workspace")["report"]["scope"]["full_coverage"])

    def test_partial_provider_pause_exposes_the_safe_reason_and_retry_hint(self):
        engine = FakeLLM()
        analyse = engine.analyse
        def paused_report(*args, **kwargs):
            report = analyse(*args, **kwargs)
            report["scope"].update(analysis_complete=False, partial=True)
            report["pause_reason"] = {"code": "rate_limit", "retry_after": 11.2, "message": "RAW-REQUEST-SECRET"}
            return report
        engine.analyse = paused_report
        service = self.router_service(engine=engine)
        self.seed(service)
        job = service.submit({"kind": "analysis", "analysis_method": "openrouter", "allow_external": True})
        finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "paused")
        self.assertEqual(finished["result"]["error_code"], "rate_limit")
        self.assertEqual(finished["result"]["retry_after"], 12)
        self.assertNotIn("RAW-REQUEST-SECRET", finished["detail"])
        self.assertNotIn("RAW-REQUEST-SECRET", json.dumps(service.latest_report("workspace")))

    def test_local_research_needs_no_provider_or_consent_and_stays_out_of_reports(self):
        engine = FakeLLM()
        service = self.router_service(configured=False, engine=engine)
        self.seed(service)
        job = service.submit({"kind": "research", "question": "How many reviews are stored?"})
        finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "succeeded", finished)
        self.assertEqual(finished["result"]["method"], "local")
        self.assertNotIn("api_key", finished["result"])
        self.assertNotIn("must-not-be-saved", json.dumps(finished))
        self.assertIsNone(service.latest_report("workspace")["report"])
        for external in (False, True):
            job = service.submit({"kind": "research", "question": "What battery complaints recur?", "allow_external": external})
            self.assertEqual(self.wait_job(service, job["id"])["status"], "succeeded")

    def test_research_options_and_answer_size_are_bounded(self):
        engine = FakeLLM()
        service = self.router_service(engine=engine, autostart=False)
        for payload in [{"question": ""}, {"question": "q" * 1501}, {"question": []},
                        {"question": "How many reviews?", "dataset": []},
                        {"question": "How many reviews?", "allow_external": 1},
                        {"question": "A question", "research_mode": []}, {"question": "A question", "research_mode": "unbounded"}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                service.submit({"kind": "research", **payload})
        engine.ask = Mock(return_value={"answer": "a" * 16001, "evidence": [], "steps": []})
        job = service.submit({"kind": "research", "question": "How many reviews?"})
        with self.assertRaises(ValueError):
            service._run(job)
        self.assertIsNone(service.get_job(job["id"])["result"])

    def test_provider_errors_expose_only_safe_codes_and_bounded_retry_hints(self):
        engine = FakeLLM()
        engine.failure = ProviderError("RAW-REQUEST-SECRET", code="rate_limit", retry_after=3.2)
        service = self.router_service(engine=engine)
        self.seed(service)
        with self.assertLogs("job_manager", level="ERROR") as logs:
            job = service.submit({"kind": "analysis", "analysis_method": "openrouter", "allow_external": True})
            finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["result"], {"error_code": "rate_limit", "retry_after": 4})
        self.assertNotIn("RAW-REQUEST-SECRET", json.dumps(finished) + " ".join(logs.output))
        engine.failure = ProviderError("RAW-REQUEST-SECRET", code="timeout")
        with self.assertLogs("job_manager", level="ERROR") as logs:
            job = service.submit({"kind": "analysis", "analysis_method": "openrouter", "allow_external": True})
            finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["result"], {"error_code": "timeout"})
        self.assertIn("OpenRouter Logs", finished["detail"])
        self.assertIsNone(service.latest_report("workspace")["report"])
        self.assertNotIn("RAW-REQUEST-SECRET", json.dumps(finished) + " ".join(logs.output))
        engine.failure = RuntimeError("Authorization: Bearer RAW-REQUEST-SECRET")
        with self.assertLogs("job_manager", level="ERROR") as logs:
            job = service.submit({"kind": "research", "question": "What battery complaints recur?", "allow_external": True})
            finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["result"]["error_code"], "provider_error")
        self.assertNotIn("RAW-REQUEST-SECRET", json.dumps(finished) + " ".join(logs.output))

    def test_running_router_cancellation_preserves_chunks_but_saves_no_report(self):
        engine = FakeLLM()
        engine.entered, engine.release = threading.Event(), threading.Event()
        self.addCleanup(engine.release.set)
        service = self.router_service(engine=engine)
        self.seed(service)
        job = service.submit({"kind": "analysis", "analysis_method": "openrouter", "allow_external": True})
        self.assertTrue(engine.entered.wait(3))
        with self.assertRaises(JobConflict):
            service.configure_provider({"clear": True})
        service.cancel(job["id"])
        engine.release.set()
        finished = self.wait_job(service, job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertIsNone(service.latest_report("workspace")["report"])

    def test_plan_is_local_and_does_not_mutate_databases_or_require_a_key(self):
        engine = FakeLLM()
        service = self.router_service(configured=False, engine=engine, autostart=False)
        self.seed(service)
        before = service.path.read_bytes(), service.data.path("workspace").read_bytes()
        planned = service.analysis_plan("workspace", "", "deep")
        self.assertEqual((planned["depth"], planned["total_reviews"]), ("deep", 1))
        self.assertEqual(engine.calls, [])
        self.assertEqual((service.path.read_bytes(), service.data.path("workspace").read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
