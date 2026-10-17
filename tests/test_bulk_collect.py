"""Control-flow tests with fake jobs and temporary data, without live requests."""

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import DEFAULT, Mock, patch

from bulk_collect import Campaign, CampaignError, Limits, LocalAPI, database_progress, load_manifest, validate_manifest
from review_store import ReviewStore


def product(number=1):
    asin = f"B0TEST{number:04d}"
    return {"asin": asin, "url": f"https://www.amazon.in/dp/{asin}",
            "title": f"Fictional test product {number}", "source_url": "https://www.amazon.in/s?k=test"}


class Clock:
    def __init__(self):
        self.value, self.sleeps = 0, []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class FakeAPI:
    def __init__(self, plans=(), *, active=None):
        self.plans = list(plans)
        self.posts, self.cancels, self.jobs = [], [], []
        self.active, self.plan, self.polls = active, None, 0
        self.count, self.checkpoints = 0, {}
        self.post_error = self.poll_error = None

    def progress(self):
        return {"count": self.count, "checkpoints": self.checkpoints}

    def status(self):
        if self.plan is not None:
            if self.poll_error:
                raise self.poll_error
            self.polls += 1
            if self.polls >= self.plan.get("polls", 1):
                self.active.update(status=self.plan.get("status", "paused"), result={
                    "status": self.plan.get("result_status", self.plan.get("status", "paused")),
                    "products": self.plan.get("products", []),
                    "pages_processed": self.plan.get("pages", 5),
                })
                self.count += self.plan.get("new", 0)
                self.checkpoints.update(self.plan.get("checkpoints", {}))
                self.active, self.plan = None, None
        return {"active_job": self.active, "jobs": self.jobs[-12:]}

    def submit(self, payload):
        self.posts.append(payload)
        if self.post_error:
            raise self.post_error
        self.plan = self.plans.pop(0)
        self.polls = 0
        self.active = {"id": f"{len(self.posts):032x}", "status": "queued", "progress": {}}
        self.jobs.append(self.active)
        return {"job": self.active.copy()}

    def cancel(self, job_id):
        self.cancels.append(job_id)
        return {"job": {"id": job_id, "status": "cancelling"}}


def plan(*numbers, status="paused", new=1, pages=5, **changes):
    return {"status": status, "new": new, "pages": pages,
            "products": [{"asin": product(number)["asin"], "status": "complete" if status == "succeeded" else status} for number in numbers],
            **changes}


class BulkCampaignTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.clock = Clock()

    def campaign(self, api, products=None, **limits):
        return Campaign(products or [product()], self.folder / "missing.sqlite3", self.folder / "campaigns",
                        api=api, limits=Limits(**limits), progress_reader=api.progress,
                        clock=self.clock, sleep=self.clock.sleep, output=lambda message: None)

    def test_manifest_aliases_validation_and_deduplication(self):
        original = product()
        alias = {**original, "product_url": original["url"]}
        del alias["url"]
        manifest = {"researched_on": "2026-10-17", "products": [original, alias]}
        self.assertEqual(validate_manifest(manifest), [original])
        source = self.folder / "products.json"
        source.write_text(json.dumps(manifest), encoding="utf-8-sig")
        self.assertEqual(load_manifest(source), [original])
        for invalid in [[], [product()] * 101, ["text"], [{**original, "asin": "B0TEST0002"}],
                        [{**original, "url": "http://www.amazon.in/dp/B0TEST0001"}],
                        [{**original, "url": "https://amazon.in.evil.invalid/dp/B0TEST0001"}],
                        [{**original, "source_url": "https://user@www.amazon.in/dp/B0TEST0001"}]]:
            with self.subTest(manifest=invalid):
                with self.assertRaises(CampaignError):
                    validate_manifest(invalid)

    def test_limits_and_local_port_are_bounded(self):
        for settings in [{"target": 0}, {"target": 5001}, {"target": True}, {"budget_seconds": 3601},
                         {"budget_seconds": float("nan")}, {"budget_seconds": 0}, {"max_pages": 1001}]:
            with self.assertRaises(CampaignError):
                Limits(**settings)
        for port in [0, 65536, True, "8765"]:
            with self.assertRaises(CampaignError):
                LocalAPI(port)

    def test_progress_resumes_paused_jobs_and_saves_a_durable_journal(self):
        api = FakeAPI([plan(1, new=7), plan(1, status="succeeded", new=8)])
        campaign = self.campaign(api, target=12)
        result = campaign.run()
        self.assertEqual((result["stop_reason"], result["count"], len(api.posts)), ("target_reached", 15, 2))
        self.assertEqual(result["pages_processed"], 10)
        for payload in api.posts:
            self.assertTrue(payload["resume"])
            self.assertTrue(payload["analyse"])
            self.assertEqual(payload["transport"], "browser")
            self.assertEqual(payload["analysis_method"], "local")
            self.assertFalse(payload["allow_external"])
        saved = json.loads(campaign.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["jobs"][0]["before_count"], 0)
        self.assertEqual(saved["jobs"][0]["after_count"], 7)
        self.assertEqual(len(saved["jobs"]), 2)
        self.assertTrue(saved["run_id"])
        self.assertEqual(self.clock.sleeps, [2, 2])
        self.assertEqual(list(campaign.path.parent.glob("*.tmp")), [])

    def test_active_job_is_refused_without_submitting_or_cancelling_it(self):
        api = FakeAPI(active={"id": "a" * 32, "status": "running"})
        result = self.campaign(api).run()
        self.assertEqual(result["stop_reason"], "active_job_present")
        self.assertEqual((api.posts, api.cancels), ([], []))

    def test_complete_empty_and_replay_limited_products_are_skipped(self):
        api = FakeAPI([plan(4, status="succeeded")])
        api.checkpoints = {
            product(1)["asin"]: {"status": "complete", "replay_steps": 0},
            product(2)["asin"]: {"status": "zero_reviews", "replay_steps": 0},
            product(3)["asin"]: {"status": "paused", "replay_steps": 21},
        }
        result = self.campaign(api, [product(number) for number in range(1, 5)]).run()
        self.assertEqual(result["stop_reason"], "products_exhausted")
        self.assertEqual(api.posts[0]["urls"], [product(4)["url"]])
        self.assertEqual(result["candidates"][2]["status"], "replay_limit")

    def test_partial_or_failed_checkpoints_need_review_before_new_work(self):
        for status in ["partial", "failed", "blocked"]:
            api = FakeAPI()
            api.checkpoints = {product()["asin"]: {"status": status}}
            result = self.campaign(api).run()
            self.assertEqual(result["stop_reason"], "blocked" if status == "blocked" else "review_required")
            self.assertEqual(api.posts, [])

    def test_partial_failed_and_blocked_results_stop_the_whole_campaign(self):
        for status, expected in [("partial", "review_required"), ("failed", "review_required"), ("blocked", "blocked")]:
            api = FakeAPI([plan(1, status="paused" if status == "partial" else status,
                                result_status=status, products=[{"asin": product()["asin"], "status": status}], new=100)])
            result = self.campaign(api, [product(), product(2)], target=50).run()
            self.assertEqual(result["stop_reason"], expected)
            self.assertEqual(len(api.posts), 1)

    def test_two_batches_without_distinct_progress_stop_even_when_pages_are_saved(self):
        api = FakeAPI([plan(1, new=0), plan(1, new=0), plan(1, new=50)])
        result = self.campaign(api).run()
        self.assertEqual(result["stop_reason"], "no_new_reviews")
        self.assertEqual((len(api.posts), result["pages_processed"], result["zero_new_batches"]), (2, 10, 2))

    def test_batch_and_productive_page_limits_cannot_overshoot(self):
        products = [product(number) for number in range(1, 12)]
        api = FakeAPI([plan(*range(1, 11), pages=50), plan(11, 1, 2, pages=3)])
        result = self.campaign(api, products, max_pages=53).run()
        self.assertEqual(result["stop_reason"], "page_budget")
        self.assertEqual(result["pages_processed"], 53)
        self.assertEqual((len(api.posts[0]["urls"]), api.posts[0]["max_pages"]), (10, 5))
        self.assertEqual((len(api.posts[1]["urls"]), api.posts[1]["max_pages"]), (3, 1))
        self.assertEqual(api.posts[1]["urls"][0], product(11)["url"])

    def test_time_budget_cancels_only_the_owned_job_once(self):
        api = FakeAPI([plan(1, polls=100)])
        result = self.campaign(api, budget_seconds=3).run()
        self.assertEqual(result["stop_reason"], "time_budget")
        self.assertEqual(api.cancels, ["0" * 31 + "1"])
        self.assertEqual(result["cancel_requested_job_ids"], api.cancels)
        self.assertEqual(self.clock.value, 3)

    def test_interrupt_cancels_once_and_an_explicit_restart_has_a_new_run_identity(self):
        api = FakeAPI([plan(1)])
        api.poll_error = KeyboardInterrupt()
        campaign = self.campaign(api)
        result = campaign.run()
        self.assertEqual(result["stop_reason"], "interrupted")
        self.assertEqual(len(api.cancels), 1)
        campaign.cancel_own()
        self.assertEqual(len(api.cancels), 1)
        fresh = self.campaign(FakeAPI())
        self.assertNotEqual(campaign.path, fresh.path)
        self.assertEqual(fresh.state["jobs"], [])

    def test_uncertain_submission_is_not_retried_and_does_not_cancel_an_unknown_job(self):
        api = FakeAPI([plan(1)])
        api.post_error = OSError("The local connection closed after sending the request.")
        result = self.campaign(api).run()
        self.assertEqual(result["stop_reason"], "submission_uncertain")
        self.assertEqual((len(api.posts), api.cancels), (1, []))
        self.assertEqual(result["pending_batch"], [product()["asin"]])

    def test_interrupt_after_accepting_submission_preserves_uncertainty(self):
        api = FakeAPI([plan(1)])
        submit = api.submit
        def accept_then_interrupt(payload):
            submit(payload)
            raise KeyboardInterrupt
        api.submit = accept_then_interrupt
        campaign = self.campaign(api)
        result = campaign.run()
        self.assertEqual(result["stop_reason"], "submission_uncertain")
        self.assertEqual(result["pending_batch"], [product()["asin"]])
        self.assertEqual((len(api.posts), api.cancels, result["jobs"]), (1, [], []))
        self.assertEqual(api.active["status"], "queued")
        self.assertIn("may still be running", result["detail"])
        saved = json.loads(campaign.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stop_reason"], "submission_uncertain")

    def test_poll_failure_cancels_the_known_job_without_retrying_the_post(self):
        api = FakeAPI([plan(1)])
        api.poll_error = OSError("The local API is unavailable.")
        result = self.campaign(api).run()
        self.assertEqual(result["stop_reason"], "error")
        self.assertEqual((len(api.posts), len(api.cancels)), (1, 1))

    def test_existing_target_does_not_submit_any_jobs(self):
        api = FakeAPI()
        api.count = 5000
        result = self.campaign(api).run()
        self.assertEqual(result["stop_reason"], "target_reached")
        self.assertEqual(api.posts, [])

    def test_preflight_elapsed_budget_does_not_submit_new_work(self):
        api = FakeAPI([plan(1)])
        status = api.status
        calls = []
        def slow_status():
            calls.append(1)
            if len(calls) == 2:
                self.clock.value = 10
            return status()
        api.status = slow_status
        result = self.campaign(api, budget_seconds=10).run()
        self.assertEqual(result["stop_reason"], "time_budget")
        self.assertEqual(api.posts, [])

    def test_invalid_submission_and_missing_product_results_are_not_retried(self):
        api = FakeAPI()
        api.submit = Mock(return_value={"job": None})
        result = self.campaign(api).run()
        self.assertEqual(result["stop_reason"], "submission_uncertain")
        api.submit.assert_called_once()
        api = FakeAPI([plan(1, products=[])])
        result = self.campaign(api).run()
        self.assertEqual(result["stop_reason"], "error")
        self.assertEqual(len(api.posts), 1)

    def test_import_has_no_network_database_auth_or_browser_side_effects(self):
        script = """from unittest.mock import patch
import sys
with patch('http.client.HTTPConnection', side_effect=AssertionError('network')), patch('sqlite3.connect', side_effect=AssertionError('database')), patch('pathlib.Path.open', side_effect=AssertionError('file read')):
    import bulk_collect
assert not {'scraper', 'collection_runtime', 'job_manager', 'playwright'}.intersection(sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_journal_retries_only_transient_windows_locks(self):
        campaign = self.campaign(FakeAPI())
        for code in (5, 32, 33):
            locked = PermissionError("File is temporarily locked")
            locked.winerror = code
            with patch("bulk_collect.os.replace", wraps=os.replace, side_effect=[locked, locked, DEFAULT]) as replace:
                campaign.save()
            self.assertEqual(replace.call_count, 3)
            self.assertEqual(json.loads(campaign.path.read_text(encoding="utf-8"))["phase"], "starting")
        self.assertEqual(self.clock.sleeps, [0.05, 0.1] * 3)

    def test_journal_permanent_lock_is_bounded_and_cleans_temporary_file(self):
        campaign = self.campaign(FakeAPI())
        campaign.save()
        before = campaign.path.read_bytes()
        locked = PermissionError("File is temporarily locked")
        locked.winerror = 5
        campaign.state["phase"] = "changed"
        with patch("bulk_collect.os.replace", side_effect=locked) as replace:
            with self.assertRaises(PermissionError):
                campaign.save()
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(self.clock.sleeps, [0.05, 0.1, 0.2, 0.4])
        self.assertEqual(campaign.path.read_bytes(), before)
        self.assertEqual(list(campaign.path.parent.glob(".campaign-*.tmp")), [])

    def test_journal_does_not_retry_other_file_errors(self):
        campaign = self.campaign(FakeAPI())
        for failure in (PermissionError("Denied"), OSError("Disk full")):
            with patch("bulk_collect.os.replace", side_effect=failure) as replace:
                with self.assertRaises(OSError):
                    campaign.save()
            self.assertEqual(replace.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(list(campaign.path.parent.glob(".campaign-*.tmp")), [])

    def test_journal_lock_after_submission_cancels_only_its_known_job(self):
        api = FakeAPI([plan(1, polls=10)])
        campaign = self.campaign(api)
        locked = PermissionError("File is temporarily locked")
        locked.winerror = 32
        original_save = campaign.save

        def save():
            if campaign.active_id:
                with patch("bulk_collect.os.replace", side_effect=locked):
                    return original_save()
            return original_save()

        with patch.object(campaign, "save", side_effect=save):
            with self.assertRaises(PermissionError):
                campaign.run()
        self.assertEqual(api.cancels, [api.jobs[0]["id"]])
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(list(campaign.path.parent.glob(".campaign-*.tmp")), [])

    def test_local_api_has_fixed_loopback_host_headers_and_no_redirect_retry(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status, response.read.return_value = 202, b'{"job":{"id":"abc"}}'
        with patch("bulk_collect.http.client.HTTPConnection", return_value=connection) as constructor:
            api = LocalAPI(8766)
            api.submit({"kind": "collection"})
            constructor.assert_called_once_with("127.0.0.1", 8766, timeout=10)
            args, options = connection.request.call_args
            self.assertEqual(args, ("POST", "/api/jobs"))
            self.assertEqual(options["headers"], {"Content-Type": "application/json", "X-Reviewdesk-Request": "1"})
            connection.close.assert_called_once()
        connection.reset_mock()
        response.status, response.read.return_value = 302, b'{"error":"Moved"}'
        with patch("bulk_collect.http.client.HTTPConnection", return_value=connection):
            with self.assertRaises(CampaignError):
                LocalAPI().status()
        self.assertEqual(connection.request.call_count, 1)

    def test_database_counter_excludes_demo_imports_and_duplicate_variant_identities(self):
        path = self.folder / "reviews.sqlite3"
        missing = self.folder / "missing" / "reviews.sqlite3"
        self.assertEqual(database_progress(missing, [product()["asin"]]), {"count": 0, "checkpoints": {}})
        self.assertFalse(missing.parent.exists())
        def row(number, review_id):
            item = product(number)
            return {"asin": item["asin"], "product_url": item["url"], "review_id": review_id,
                    "reviewer_name": "Fictional reviewer", "verified_purchase": "true",
                    "review_title": "Test", "rating": "4", "review_date": "2026-01-01", "review_body": "Fictional review."}
        with ReviewStore(path) as store:
            store.upsert_reviews([row(1, "SHARED"), row(2, "SHARED"), row(2, "UNIQUE"), row(3, "OUTSIDE")], source="live")
            store.upsert_reviews([row(1, "DEMO")], source="synthetic-demo")
            store.upsert_reviews([row(1, "IMPORT")], source="dashboard-import")
            store.save_page(product()["asin"], [], next_url=None, pages_completed=2, status="complete")
        before = path.read_bytes()
        result = database_progress(path, [product()["asin"], product(2)["asin"]])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["checkpoints"][product()["asin"]]["status"], "complete")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
