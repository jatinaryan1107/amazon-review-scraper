"""Verify the local automation HTTP contract without browsers or providers."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from dashboard import DashboardHandler, DashboardServer
from dashboard_data import DashboardData
from job_manager import AutomationService


class DashboardJobTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.data = DashboardData(self.folder / "data")
        self.data.seed_demo()
        self.auth_saved = False
        self.auth_entered = threading.Event()
        def fake_auth(path, finished, cancelled, on_progress):
            on_progress({"status": "awaiting_login", "detail": "Fictional sign-in window for a test."})
            self.auth_entered.set()
            while not finished() and not cancelled():
                time.sleep(0.005)
            self.auth_saved = finished() and not cancelled()
            return {"status": "completed" if self.auth_saved else "paused", "detail": "Test session finished."}
        def fake_collection(*args, **kwargs):
            return {"status": "blocked", "detail": "Fictional access block for this test.",
                    "products": [], "inserted": 0, "pages_processed": 0, "metrics": {}}
        self.service = AutomationService(
            self.data, self.folder / "auth.json", auth_runner=fake_auth, collection_runner=fake_collection,
            settings=lambda: {"configured": False, "model": ""},
            session_reader=lambda path: {"exists": self.auth_saved, "valid": self.auth_saved, "detail": "Test session status."},
        )
        self.server = DashboardServer(("127.0.0.1", 0), self.data, automation=self.service)
        quiet = patch.object(DashboardHandler, "log_message")
        quiet.start()
        self.addCleanup(quiet.stop)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.stop, thread)
        self.port = self.server.server_address[1]

    def stop(self, thread):
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=2)

    def request(self, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST" if body is not None else "GET", path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def post(self, payload, path="/api/jobs", **headers):
        return self.request(path, body=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "X-Reviewdesk-Request": "1", **headers,
        })

    def wait_job(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.service.get_job(job_id)
            if job["status"] not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(0.005)
        self.fail("The fake local job did not finish.")

    def test_status_is_local_and_has_no_cookie_or_provider_values(self):
        status, result = self.request("/api/automation")
        self.assertEqual(status, 200)
        self.assertEqual(result["auth"]["status"], "missing")
        self.assertFalse(result["auth"]["verified"])
        self.assertFalse(result["capabilities"]["openai_configured"])
        self.assertIsNone(result["active_job"])
        self.assertNotIn(str(self.folder), json.dumps(result))
        self.assertNotIn("cookies", json.dumps(result))
        self.assertEqual(self.request("/api/automation?dataset=demo")[0], 400)

    def test_local_analysis_job_persists_report_and_reuses_unchanged_content(self):
        self.assertEqual(self.request("/api/analysis?dataset=demo")[1], {"report": None, "stale": False})
        status, response = self.post({"kind": "analysis", "dataset": "demo"})
        self.assertEqual(status, 202)
        job = self.wait_job(response["job"]["id"])
        self.assertEqual(job["status"], "succeeded", job)
        status, report = self.request("/api/analysis?dataset=demo")
        self.assertEqual(status, 200)
        self.assertEqual(report["report"]["scope"]["total_reviews"], 15)
        self.assertEqual(report["report"]["method"], "local")
        self.assertFalse(report["stale"])
        self.assertTrue(report["report"]["themes"])
        status, again = self.post({"kind": "analysis", "dataset": "demo"})
        self.assertEqual(status, 202)
        reused = self.wait_job(again["job"]["id"])
        self.assertTrue(reused["result"]["cached"])
        self.assertEqual(job["result"]["analysis_id"], reused["result"]["analysis_id"])

    def test_evidence_review_endpoint_requires_exact_product_and_review_identity(self):
        status, response = self.post({"kind": "analysis", "dataset": "demo"})
        self.wait_job(response["job"]["id"])
        report = self.request("/api/analysis?dataset=demo")[1]["report"]
        evidence = report["themes"][0]["evidence"][0]
        params = urlencode({"dataset": "demo", "asin": evidence["asin"], "review_id": evidence["review_id"]})
        status, result = self.request("/api/review?" + params)
        self.assertEqual(status, 200)
        original = result["review"]
        self.assertEqual(original["asin"], evidence["asin"])
        self.assertEqual(original["review_id"], evidence["review_id"])
        self.assertTrue(evidence["quote"] in original["review_title"] or evidence["quote"] in original["review_body"])
        unknown = urlencode({"dataset": "demo", "asin": evidence["asin"], "review_id": "' OR 1=1 --"})
        self.assertEqual(self.request("/api/review?" + unknown)[0], 404)
        self.assertEqual(self.request("/api/review?dataset=demo")[0], 400)
        self.assertEqual(self.request("/api/review?dataset=demo&asin=BAD&review_id=x")[0], 400)

    def test_auth_job_requires_manual_finish_and_excludes_competing_jobs(self):
        status, response = self.post({"kind": "auth"})
        self.assertEqual(status, 202)
        job_id = response["job"]["id"]
        self.assertTrue(self.auth_entered.wait(2))
        self.assertEqual(self.request("/api/automation")[1]["active_job"]["phase"], "awaiting_login")
        self.assertFalse(self.auth_saved)
        self.assertEqual(self.post({"kind": "analysis", "dataset": "demo"})[0], 409)
        self.assertEqual(self.post({}, f"/api/jobs/{job_id}/finish")[0], 200)
        self.assertEqual(self.wait_job(job_id)["status"], "succeeded")
        self.assertTrue(self.auth_saved)
        self.assertEqual(self.request("/api/automation")[1]["auth"]["status"], "saved")

    def test_cancellation_and_missing_job_actions_are_explicit(self):
        status, response = self.post({"kind": "auth"})
        self.assertEqual(status, 202)
        job_id = response["job"]["id"]
        self.assertTrue(self.auth_entered.wait(2))
        status, cancelling = self.post({}, f"/api/jobs/{job_id}/cancel")
        self.assertEqual(status, 200)
        self.assertIn(cancelling["job"]["status"], {"cancelling", "cancelled"})
        self.assertEqual(self.wait_job(job_id)["status"], "cancelled")
        self.assertFalse(self.auth_saved)
        self.assertEqual(self.post({}, "/api/jobs/" + "0" * 32 + "/cancel")[0], 404)
        self.assertEqual(self.post({}, "/api/jobs/not-an-id/cancel")[0], 404)

    def test_blocked_collection_returns_honest_status_without_an_analysis(self):
        status, response = self.post({"kind": "collection", "urls": ["https://www.amazon.in/dp/B0TEST0001"]})
        self.assertEqual(status, 202)
        job = self.wait_job(response["job"]["id"])
        self.assertEqual(job["status"], "blocked")
        self.assertIn("Fictional access block", job["detail"])
        self.assertEqual(self.request("/api/analysis?dataset=workspace")[1]["report"], None)

    def test_job_mutations_require_local_origin_header_and_json(self):
        payload = json.dumps({"kind": "analysis", "dataset": "demo"}).encode()
        self.assertEqual(self.request("/api/jobs", body=payload, headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.post({"kind": "auth"}, Origin="https://example.com")[0], 403)
        self.assertEqual(self.post({"kind": "auth"}, **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.post({"kind": "auth"}, **{"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post({"kind": "auth"}, path="/api/jobs?extra=yes")[0], 400)

    def test_malformed_and_excessive_job_payloads_never_reach_the_worker(self):
        headers = {"Content-Type": "application/json", "X-Reviewdesk-Request": "1"}
        for payload in (b"{", b'\xff', b'{"kind":"auth","kind":"auth"}', b'[]', b'{"kind":[]}', b'[' * 1200 + b']' * 1200):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("/api/jobs", body=payload, headers=headers)[0], 400)
        self.assertEqual(self.request("/api/jobs", body=b" " * 16_385, headers=headers)[0], 413)
        self.assertEqual(self.post({"kind": "analysis", "dataset": "demo", "analysis_method": "openai"})[0], 400)
        self.assertEqual(self.post({"kind": "analysis", "dataset": "demo", "analysis_method": "openai", "allow_external": True})[0], 400)
        self.assertEqual(self.request("/api/automation")[1]["jobs"], [])


if __name__ == "__main__":
    unittest.main()
