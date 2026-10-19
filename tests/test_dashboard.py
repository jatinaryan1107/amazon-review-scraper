"""Exercise local dashboard HTTP behavior using temporary data and loopback only."""

import csv
from contextlib import closing
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from dashboard import DashboardHandler, DashboardServer
from dashboard_data import DashboardData, MAX_IMPORT_BYTES, ReviewQuery, spreadsheet_cell
from offline import DEMO_CSV
from review_store import REVIEW_FIELDS, ReviewStore
from llm_provider import ProviderError, ProviderRegistry


class DashboardTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(tempdir.cleanup)
        self.folder = Path(tempdir.name)
        self.data = DashboardData(self.folder / "data")
        self.data.seed_demo()
        self.server = DashboardServer(("127.0.0.1", 0), self.data)
        self.server.automation._provider_registry = ProviderRegistry(environ={})
        self.server.automation._settings = lambda: {"configured": False, "model": ""}
        self.server.automation._session_reader = lambda path: {"exists": False, "valid": False, "detail": "Synthetic session status."}
        provider_guard = patch.object(self.server.automation._provider_registry, "chat", side_effect=AssertionError("No provider call is allowed in dashboard tests."))
        provider_guard.start()
        self.addCleanup(provider_guard.stop)
        quiet_log = patch.object(DashboardHandler, "log_message")
        quiet_log.start()
        self.addCleanup(quiet_log.stop)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server, thread)
        self.port = self.server.server_address[1]

    def stop_server(self, thread):
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=2)

    def request(self, path, *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            content = response.read()
            return response.status, dict(response.getheaders()), content
        finally:
            connection.close()

    def get_json(self, path):
        status, headers, content = self.request(path)
        self.assertEqual(status, 200, content)
        self.assertEqual(headers["Cache-Control"], "no-store")
        return json.loads(content)

    def upload(self, payload, dataset="workspace", **headers):
        return self.request(
            f"/api/import?dataset={dataset}", method="POST", body=payload,
            headers={"Content-Type": "text/csv; charset=utf-8", "X-Reviewdesk-Request": "1", **headers},
        )

    @staticmethod
    def review(**changes):
        return {
            "asin": "B0TEST0001", "product_url": "https://www.amazon.in/dp/B0TEST0001",
            "review_id": "TEST-1", "reviewer_name": "Test reviewer", "verified_purchase": "true",
            "review_title": "A test review", "rating": "4", "review_date": "Unparsed review date",
            "review_body": "A fictional review for a local test.", **changes,
        }

    @staticmethod
    def csv_bytes(rows, headers=REVIEW_FIELDS):
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode("utf-8-sig")

    def seed_workspace(self, rows):
        with ReviewStore(self.data.path("workspace")) as store:
            store.upsert_reviews(rows, source="test")

    def test_demo_seed_is_idempotent_and_overview_aggregates_are_exact(self):
        self.data.seed_demo()
        actual = self.get_json("/api/overview?dataset=demo")
        self.assertTrue(actual["dataset"]["is_demo"])
        self.assertIn("fictional", actual["dataset"]["description"])
        self.assertEqual(actual["summary"]["total_reviews"], 15)
        self.assertEqual(actual["summary"]["product_count"], 3)
        with DEMO_CSV.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertAlmostEqual(actual["summary"]["average_rating"], sum(float(row["rating"]) for row in rows) / 15)
        verified = sum(row["verified_purchase"].lower() == "true" for row in rows)
        self.assertEqual(actual["summary"]["verified_count"], verified)
        self.assertAlmostEqual(actual["summary"]["verified_share"], verified * 100 / 15)
        self.assertEqual(actual["summary"]["low_rating_count"], sum(float(row["rating"]) <= 2 for row in rows))
        self.assertEqual(sum(bucket["count"] for bucket in actual["ratings"]), 15)
        self.assertEqual({product["name"] for product in actual["products"]}, {"Desk fan 01", "Desk fan 02", "Desk fan 03"})
        self.assertEqual(len(actual["recent_reviews"]), 5)
        self.assertNotIn(str(self.folder), json.dumps(actual))
        self.assertTrue(actual["last_updated"].endswith("Z"))

    def test_empty_workspace_reads_and_exports_do_not_create_a_database(self):
        before = sorted(self.data.data_dir.iterdir())
        overview = self.get_json("/api/overview?dataset=workspace")
        self.assertEqual(overview["summary"]["total_reviews"], 0)
        self.assertIsNone(overview["summary"]["average_rating"])
        self.assertIsNone(overview["last_updated"])
        self.assertFalse(overview["dataset"]["is_demo"])
        result = self.get_json("/api/reviews?dataset=workspace&page=999&page_size=100")
        self.assertEqual(result, {"items": [], "total": 0, "page": 1, "pages": 1, "page_size": 100})
        status, _, content = self.request("/api/export?dataset=workspace")
        self.assertEqual(status, 200)
        self.assertEqual(list(csv.DictReader(io.StringIO(content.decode("utf-8-sig")))), [])
        self.assertEqual(sorted(self.data.data_dir.iterdir()), before)
        self.assertFalse(self.data.path("workspace").exists())
        self.assertEqual(self.data.path("workspace").name, "reviews.sqlite3")

    def test_product_names_persist_and_search_matches_name_with_existing_filters(self):
        self.seed_workspace([self.review(), self.review(review_id="SECOND", rating="1")])
        status, _, content = self.post_json("/api/products/name", {"dataset": "workspace", "asin": "B0TEST0001", "name": "Fictional 50%_off speaker"})
        self.assertEqual(status, 200, content)
        product = self.get_json("/api/overview?dataset=workspace")["products"][0]
        self.assertEqual(product["name"], "Fictional 50%_off speaker")
        self.assertEqual(product["name_source"], "manual")
        self.assertEqual(product["critical_count"], 1)
        self.assertEqual(product["product_url"], "https://www.amazon.in/dp/B0TEST0001")
        rows = self.get_json("/api/reviews?dataset=workspace&q=50%25_off&rating=critical")["items"]
        self.assertEqual([row["review_id"] for row in rows], ["SECOND"])
        with ReviewStore(self.data.path("workspace"), read_only=True) as store:
            self.assertEqual(store.product_names()["B0TEST0001"]["name"], product["name"])

    def test_naming_rejects_unknown_product_invalid_input_and_cross_origin(self):
        valid = {"dataset": "workspace", "asin": "B0TEST0001", "name": "Fictional speaker"}
        self.assertEqual(self.post_json("/api/products/name", valid)[0], 400)
        self.assertFalse(self.data.path("workspace").exists())
        self.seed_workspace([self.review()])
        for payload in [None, [], {**valid, "dataset": "demo"}, {**valid, "name": ""}, {**valid, "name": "x" * 501}, {**valid, "asin": 123}, {**valid, "extra": True}]:
            self.assertEqual(self.post_json("/api/products/name", payload)[0], 400)
        self.assertEqual(self.post_json("/api/products/name", valid, Origin="https://example.invalid")[0], 403)
        self.assertEqual(self.request("/api/products/name", method="POST", body=json.dumps(valid), headers={"Content-Type": "application/json"})[0], 403)

    def test_csv_names_round_trip_without_overwriting_manual_names(self):
        row = {**self.review(), "product_name": "Fictional speaker"}
        self.assertEqual(self.upload(self.csv_bytes([row], (*REVIEW_FIELDS, "product_name")))[0], 200)
        _, _, content = self.request("/api/export?dataset=workspace")
        exported = next(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(exported["product_name"], "Fictional speaker")
        self.post_json("/api/products/name", {"dataset": "workspace", "asin": row["asin"], "name": "My speaker"})
        self.upload(self.csv_bytes([row], (*REVIEW_FIELDS, "product_name")))
        self.assertEqual(self.get_json("/api/overview?dataset=workspace")["products"][0]["name"], "My speaker")

    def test_conflicting_csv_names_and_invalid_reviews_save_no_metadata(self):
        rows = [{**self.review(), "product_name": "First name"}, {**self.review(review_id="SECOND"), "product_name": "Different name"}]
        self.assertEqual(self.upload(self.csv_bytes(rows, (*REVIEW_FIELDS, "product_name")))[0], 400)
        self.assertFalse(self.data.path("workspace").exists())
        rows[1]["product_name"] = "First name"
        rows[1]["rating"] = "9"
        self.assertEqual(self.upload(self.csv_bytes(rows, (*REVIEW_FIELDS, "product_name")))[0], 400)
        with ReviewStore(self.data.path("workspace"), read_only=True) as store:
            self.assertEqual(store.product_names(), {})
            self.assertEqual(store.summary()["total_reviews"], 0)

    def test_search_is_literal_and_composes_with_filters(self):
        self.seed_workspace([
            self.review(review_id="A", review_body="Battery: 50%_off\\today", rating="4.4"),
            self.review(review_id="B", review_body="Battery: 50XXofftoday", rating="4"),
            self.review(review_id="C", review_body="Battery: 50%_off\\today", verified_purchase="false", rating="4.5"),
        ])
        result = self.get_json("/api/reviews?dataset=workspace&q=50%25_off%5Ctoday&asin=B0TEST0001&rating=4&verified=true")
        self.assertEqual([row["review_id"] for row in result["items"]], ["A"])
        empty = self.get_json("/api/reviews?dataset=workspace&q=%27%20OR%201%3D1--")
        self.assertEqual(empty["total"], 0)

    def test_fractional_ratings_use_shared_nearest_star_buckets_and_ignore_blanks(self):
        self.seed_workspace([
            self.review(review_id="A", rating="4.4"),
            self.review(review_id="B", rating="4.5"),
            self.review(review_id="C", rating=""),
        ])
        overview = self.get_json("/api/overview?dataset=workspace")
        self.assertAlmostEqual(overview["summary"]["average_rating"], 4.45)
        self.assertEqual(overview["ratings"][:2], [{"rating": 5, "count": 1}, {"rating": 4, "count": 1}])
        result = self.get_json("/api/reviews?dataset=workspace&rating=5")
        self.assertEqual([row["review_id"] for row in result["items"]], ["B"])

    def test_sorting_uses_import_timestamp_and_keeps_blank_ratings_last(self):
        self.seed_workspace([
            self.review(review_id="A", review_date="2099-01-01", rating=""),
            self.review(review_id="B", review_date="1900-01-01", rating="5"),
            self.review(review_id="C", rating="1"),
        ])
        with closing(sqlite3.connect(self.data.path("workspace"))) as connection:
            with connection:
                for review_id, created_at in [("A", "2026-01-03T00:00:00Z"), ("B", "2026-01-02T00:00:00Z"), ("C", "2026-01-01T00:00:00Z")]:
                    connection.execute("UPDATE reviews SET created_at = ? WHERE review_id = ?", (created_at, review_id))
        for sort, expected in [("newest", ["A", "B", "C"]), ("oldest", ["C", "B", "A"]), ("rating_desc", ["B", "C", "A"]), ("rating_asc", ["C", "B", "A"])]:
            with self.subTest(sort=sort):
                result = self.get_json(f"/api/reviews?dataset=workspace&sort={sort}")
                self.assertEqual([row["review_id"] for row in result["items"]], expected)

    def test_pagination_limits_and_clamps(self):
        first = self.get_json("/api/reviews?dataset=demo&page_size=4&page=2")
        self.assertEqual((first["total"], first["page"], first["pages"], len(first["items"])), (15, 2, 4, 4))
        last = self.get_json("/api/reviews?dataset=demo&page_size=4&page=999")
        self.assertEqual((last["page"], len(last["items"])), (4, 3))
        clamped = self.get_json("/api/reviews?dataset=demo&page_size=99999&page=-3")
        self.assertEqual((clamped["page"], clamped["page_size"]), (1, 100))

    def test_export_ignores_pagination_and_matches_filtered_reviews(self):
        query = "dataset=demo&rating=5&verified=true&sort=oldest&page_size=1"
        reviews = self.get_json("/api/reviews?" + query)
        status, headers, content = self.request("/api/export?" + query)
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="reviewdesk-demo.csv"')
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
        rows = list(reader)
        self.assertEqual(reader.fieldnames, [*REVIEW_FIELDS, "product_name"])
        self.assertEqual(len(rows), reviews["total"])
        self.assertGreater(len(rows), 1)
        self.assertEqual(rows[0]["review_id"], reviews["items"][0]["review_id"])
        uploaded, _, result = self.upload(content)
        self.assertEqual(uploaded, 200, result)
        self.assertEqual(json.loads(result)["inserted"], len(rows))

    def test_export_escapes_spreadsheet_formulas_without_changing_stored_text(self):
        dangerous = " \x01\t=HYPERLINK(\"https://example.invalid\")"
        self.seed_workspace([self.review(review_id="=ID", reviewer_name="@name", review_title="+sum(1)", review_body=dangerous)])
        _, _, content = self.request("/api/export?dataset=workspace")
        row = next(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(row["review_body"], "'" + dangerous)
        self.assertEqual(row["review_id"], "'=ID")
        self.assertEqual(row["reviewer_name"], "'@name")
        self.assertEqual(row["review_title"], "'+sum(1)")
        stored = self.get_json("/api/reviews?dataset=workspace")["items"][0]
        self.assertEqual(stored["review_body"], dangerous)
        for value in ["\tplain", "\rplain", "\nplain", "\u2003-5", "\x00@name"]:
            self.assertEqual(spreadsheet_cell(value), "'" + value)
        self.assertEqual(spreadsheet_cell("ordinary text"), "ordinary text")

    def test_import_is_idempotent_and_preserves_utf8(self):
        payload = self.csv_bytes([self.review(review_title="Quiet fan, café", review_body="Multiline\nfictional review.")])
        for expected in [(1, 0), (0, 1)]:
            status, _, content = self.upload(payload, Origin=f"http://127.0.0.1:{self.port}")
            self.assertEqual(status, 200, content)
            counts = json.loads(content)
            self.assertEqual((counts["inserted"], counts["duplicates"]), expected)
            self.assertEqual(counts["total_reviews"], 1)
        result = self.get_json("/api/reviews?dataset=workspace")["items"][0]
        self.assertEqual(result["source"], "dashboard-import")
        self.assertEqual(result["review_title"], "Quiet fan, café")
        self.assertEqual(self.get_json("/api/overview?dataset=demo")["summary"]["total_reviews"], 15)

    def test_invalid_field_rolls_back_entire_import(self):
        self.seed_workspace([self.review(review_id="EXISTING")])
        payload = self.csv_bytes([self.review(review_id="VALID"), self.review(review_id="INVALID", rating="9")])
        status, _, content = self.upload(payload)
        self.assertEqual(status, 400, content)
        self.assertIn("rating", json.loads(content)["error"])
        result = self.get_json("/api/reviews?dataset=workspace")
        self.assertEqual([row["review_id"] for row in result["items"]], ["EXISTING"])

    def test_malformed_csv_headers_and_rows_are_rejected(self):
        valid = self.csv_bytes([self.review()]).decode("utf-8-sig")
        payloads = [b"", b"asin,review_id\nB0TEST0001,A\n", b"asin,asin\nA,A\n", ",".join(REVIEW_FIELDS).encode(), (valid + "short,row\n").encode(), (valid + ",".join(["x"] * 10)).encode(), valid.replace("A test review", '"unterminated').encode(), b"\xff\xfe\x00\x00"]
        for payload in payloads:
            with self.subTest(payload=payload[:80]):
                status, _, content = self.upload(payload)
                self.assertEqual(status, 400, content)
                self.assertIn("error", json.loads(content))
        self.assertFalse(self.data.path("workspace").exists())

    def test_import_row_and_byte_limits(self):
        with patch("dashboard_data.MAX_IMPORT_ROWS", 1):
            status, _, _ = self.upload(self.csv_bytes([self.review(), self.review(review_id="SECOND")]))
            self.assertEqual(status, 400)
        status, _, content = self.request("/api/import?dataset=workspace", method="POST", body=b"x", headers={"Content-Length": str(MAX_IMPORT_BYTES + 1), "Content-Type": "text/csv", "X-Reviewdesk-Request": "1"})
        self.assertEqual(status, 413, content)
        self.assertFalse(self.data.path("workspace").exists())

    def test_import_requires_expected_route_content_type_and_request_header(self):
        payload = self.csv_bytes([self.review()])
        attempts = [
            ("/api/import?dataset=demo", {"Content-Type": "text/csv", "X-Reviewdesk-Request": "1"}, 400),
            ("/api/import?dataset=workspace", {"Content-Type": "text/csv"}, 403),
            ("/api/import?dataset=workspace", {"Content-Type": "application/json", "X-Reviewdesk-Request": "1"}, 415),
            ("/api/import?dataset=workspace", {"Content-Type": "text/csv", "X-Reviewdesk-Request": "1", "Origin": "https://example.invalid"}, 403),
        ]
        for path, headers, expected in attempts:
            status, _, content = self.request(path, method="POST", body=payload, headers=headers)
            self.assertEqual(status, expected, content)
        self.assertFalse(self.data.path("workspace").exists())

    def test_import_rejects_missing_or_ambiguous_upload_lengths(self):
        for extra_headers in [[], [("Content-Length", "-1")], [("Content-Length", "abc")],
                              [("Content-Length", "1"), ("Content-Length", "1")],
                              [("Content-Length", "1"), ("Transfer-Encoding", "")]]:
            with self.subTest(headers=extra_headers):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                try:
                    connection.putrequest("POST", "/api/import?dataset=workspace")
                    connection.putheader("Content-Type", "text/csv")
                    connection.putheader("X-Reviewdesk-Request", "1")
                    for name, value in extra_headers:
                        connection.putheader(name, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    content = response.read()
                    self.assertEqual(response.status, 400, content)
                finally:
                    connection.close()
        self.assertFalse(self.data.path("workspace").exists())

    def test_host_origin_and_cross_site_guards(self):
        for headers in [{"Host": "evil.invalid"}, {"Host": f"127.0.0.1:{self.port + 1}"}, {"Origin": "null"}, {"Origin": "https://example.invalid"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                status, _, content = self.request("/api/overview", headers=headers)
                self.assertEqual(status, 403, content)
        status, _, _ = self.request("/api/overview", headers={"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 200)
        with self.assertRaises(ValueError):
            DashboardServer(("0.0.0.0", 0), self.data)

    def test_traversal_and_unknown_routes_are_never_served(self):
        for path in ["/../review_store.py", "/%2e%2e/review_store.py", "/web/../review_store.py", "/%5c..%5creview_store.py", "/data/demo.sqlite3", "/review_store.py", "/api/nope"]:
            status, _, content = self.request(path)
            self.assertEqual(status, 404, (path, content))
            self.assertIn("error", json.loads(content))

    def test_invalid_query_values_return_human_json_errors(self):
        for query in ["dataset=unknown", "dataset=demo&dataset=workspace", "rating=6", "verified=yes", "sort=rating;DROP", "page=hello", "page_size=hello", "asin=../../", "q=" + "a" * 201, "unknown=value"]:
            status, _, content = self.request("/api/reviews?" + query)
            self.assertEqual(status, 400, (query, content))
            self.assertNotIn(str(self.folder), json.loads(content)["error"])

    def test_database_errors_do_not_leak_filesystem_paths(self):
        self.data.path("workspace").write_text("not a database", encoding="utf-8")
        with patch.object(DashboardHandler, "log_error") as log:
            for path in ["/api/overview?dataset=workspace", "/api/reviews?dataset=workspace", "/api/export?dataset=workspace"]:
                status, _, content = self.request(path)
                self.assertEqual(status, 500, content)
                self.assertNotIn(str(self.folder), content.decode())
            self.assertEqual(log.call_count, 3)
        self.assertEqual(self.data.path("workspace").read_text(), "not a database")

    def test_cancelled_client_response_does_not_send_a_second_response(self):
        for error_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            for stage in ("headers", "body"):
                with self.subTest(error=error_type.__name__, stage=stage):
                    handler = object.__new__(DashboardHandler)
                    handler.server = self.server
                    handler.path = "/api/overview?dataset=demo"
                    handler.command = "GET"
                    handler.request_version = "HTTP/1.1"
                    handler.requestline = "GET /api/overview?dataset=demo HTTP/1.1"
                    handler.client_address = ("127.0.0.1", 12345)
                    handler.headers = http.client.HTTPMessage()
                    handler.headers["Host"] = f"127.0.0.1:{self.port}"
                    handler.close_connection = False
                    handler.wfile = Mock()
                    error = error_type(10053, "The browser cancelled its request")
                    handler.wfile.write.side_effect = [error] if stage == "headers" else [None, error]
                    with patch.object(handler, "send_response", wraps=handler.send_response) as send_response, patch.object(handler, "log_error") as log_error:
                        handler.do_GET()
                    send_response.assert_called_once_with(200)
                    log_error.assert_not_called()
                    self.assertTrue(handler.close_connection)
                    self.assertEqual(handler.wfile.write.call_count, 1 if stage == "headers" else 2)

    def test_read_endpoints_preserve_database_bytes_and_include_checkpoints(self):
        self.seed_workspace([self.review()])
        with ReviewStore(self.data.path("workspace")) as store:
            store.save_page("B0TEST0001", [], next_url=None, pages_completed=2, status="partial", detail="Saved collection stopped before all reviews were available.")
        before = self.data.path("workspace").read_bytes()
        overview = self.get_json("/api/overview?dataset=workspace")
        self.assertEqual(overview["checkpoints"][0]["status"], "partial")
        self.assertEqual(overview["checkpoints"][0]["pages_completed"], 2)
        self.get_json("/api/reviews?dataset=workspace")
        self.request("/api/export?dataset=workspace")
        self.assertEqual(self.data.path("workspace").read_bytes(), before)

    def test_static_assets_have_csp_and_no_directory_listing(self):
        for path, expected_type in [("/", "text/html"), ("/app.css", "text/css"), ("/app.js", "text/javascript")]:
            status, headers, content = self.request(path)
            self.assertEqual(status, 200, content)
            self.assertIn(expected_type, headers["Content-Type"])
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def post_json(self, path, payload, **headers):
        return self.request(path, method="POST", body=json.dumps(payload).encode("utf-8"), headers={
            "Content-Type": "application/json", "X-Reviewdesk-Request": "1", **headers,
        })

    def test_provider_settings_are_sanitized_and_session_keys_never_reach_databases(self):
        public = self.get_json("/api/provider")
        self.assertEqual(public["key_source"], "none")
        self.assertFalse(public["configured"])
        self.assertTrue(public["free_only"])
        self.assertEqual(public["response_mode"], "prompt")
        status, _, content = self.post_json("/api/provider", {"api_key": "SYNTHETIC-PRIVATE-KEY", "require_zdr": True})
        self.assertEqual(status, 200, content)
        public = json.loads(content)
        self.assertEqual(public["key_source"], "session")
        self.assertTrue(public["require_zdr"])
        self.assertNotIn("PRIVATE-KEY", content.decode())
        status, _, content = self.post_json("/api/provider", {"require_zdr": False})
        self.assertEqual(status, 200, content)
        self.assertFalse(json.loads(content)["require_zdr"])
        status, _, content = self.post_json("/api/provider", {"response_mode": "schema"})
        self.assertEqual(status, 200, content)
        self.assertEqual(json.loads(content)["response_mode"], "schema")
        self.assertFalse(json.loads(content)["require_zdr"])
        self.assertEqual(self.get_json("/api/provider")["response_mode"], "schema")
        capabilities = self.get_json("/api/automation")["capabilities"]
        self.assertTrue(capabilities["openrouter_configured"])
        self.assertEqual(capabilities["openrouter_model"], "openrouter/free")
        self.assertTrue(capabilities["free_only"])
        for filename in ("jobs.sqlite3", "analysis.sqlite3"):
            self.assertNotIn(b"SYNTHETIC-PRIVATE-KEY", (self.data.data_dir / filename).read_bytes())
        status, _, content = self.post_json("/api/provider", {"clear": True})
        self.assertEqual(status, 200, content)
        self.assertFalse(self.get_json("/api/provider")["configured"])

    def test_provider_settings_require_local_origin_json_and_strict_options(self):
        payload = b'{"api_key":"SYNTHETIC-KEY"}'
        attempts = [
            ({"Content-Type": "application/json"}, 403),
            ({"Content-Type": "text/plain", "X-Reviewdesk-Request": "1"}, 415),
            ({"Content-Type": "application/json", "X-Reviewdesk-Request": "1", "Origin": "https://example.invalid"}, 403),
        ]
        for headers, expected in attempts:
            status, _, content = self.request("/api/provider", method="POST", body=payload, headers=headers)
            self.assertEqual(status, expected, content)
        for options in [{}, {"api_key": ""}, {"api_key": "x", "clear": True}, {"require_zdr": 1}, {"model": "paid-model"}, {"clear": False}, {"response_mode": []}, {"response_mode": "auto"}, {"remember_key": "yes"}]:
            status, _, content = self.post_json("/api/provider", options)
            self.assertEqual(status, 400, content)
        for body in [b'{"api_key":"one","api_key":"two"}', b'{"require_zdr":NaN}', b" " * 16385]:
            status, _, _ = self.request("/api/provider", method="POST", body=body, headers={"Content-Type": "application/json", "X-Reviewdesk-Request": "1"})
            self.assertEqual(status, 413 if len(body) > 16384 else 400)
        self.assertEqual(self.request("/api/provider?extra=yes")[0], 400)
        self.assertEqual(self.post_json("/api/provider?extra=yes", {"clear": True})[0], 400)
        self.assertFalse(self.get_json("/api/provider")["configured"])

    def test_http_remember_restores_configuration_and_forget_removes_the_saved_copy(self):
        class Vault:
            value = None
            def load(self):
                return self.value.copy() if self.value else None
            def save(self, value):
                self.value = value.copy()
            def delete(self):
                self.value = None
        vault = Vault()
        self.server.automation._provider_registry = ProviderRegistry(environ={}, credential_store=vault)
        status, _, content = self.post_json("/api/provider", {"api_key": "SYNTHETIC-SAVED-SECRET",
            "remember_key": True, "require_zdr": False})
        self.assertEqual(status, 200, content)
        self.assertNotIn(b"SYNTHETIC-SAVED-SECRET", content)
        self.server.automation._provider_registry = ProviderRegistry(environ={}, credential_store=vault)
        public = self.get_json("/api/provider")
        self.assertEqual(public["key_source"], "saved")
        self.assertTrue(public["remember_key"])
        self.assertFalse(public["require_zdr"])
        for filename in ("jobs.sqlite3", "analysis.sqlite3"):
            self.assertNotIn(b"SYNTHETIC-SAVED-SECRET", (self.data.data_dir / filename).read_bytes())
        self.assertEqual(self.post_json("/api/provider", {"clear": True})[0], 200)
        self.assertIsNone(vault.value)
        self.assertFalse(self.get_json("/api/provider")["configured"])

    def test_analysis_plan_is_read_only_unconfigured_and_rejects_ambiguous_scope(self):
        before = {path.name: path.read_bytes() for path in self.data.data_dir.glob("*.sqlite3")}
        plan = self.get_json("/api/analysis/plan?dataset=demo&depth=deep")
        self.assertEqual(plan["depth"], "deep")
        self.assertEqual(plan["total_reviews"], 15)
        self.assertEqual(plan["provider"], "openrouter")
        self.assertEqual({path.name: path.read_bytes() for path in self.data.data_dir.glob("*.sqlite3")}, before)
        self.assertFalse(self.get_json("/api/provider")["configured"])
        for query in ["dataset=other", "depth=all", "dataset=demo&dataset=workspace", "asin=BAD", "question=hello"]:
            status, _, content = self.request("/api/analysis/plan?" + query)
            self.assertEqual(status, 400, (query, content))

    def test_account_quota_check_is_explicit_read_only_and_makes_no_model_call(self):
        registry = self.server.automation._provider_registry
        checked = []
        registry._quota_transport = lambda: checked.append(True) or {"data": {"label": "PRIVATE-LABEL",
            "free_model_daily_requests": {"used": 5, "limit": 50, "remaining": 45}}}
        self.assertEqual(self.post_json("/api/provider", {"api_key": "SYNTHETIC-SECRET"})[0], 200)
        before = {p.name: p.read_bytes() for p in self.data.data_dir.glob("*.sqlite3")}
        status, _, content = self.post_json("/api/provider/check", {})
        self.assertEqual(status, 200, content)
        data = json.loads(content)
        self.assertEqual(data["quota"]["remaining"], 45)
        self.assertEqual(data["app_daily_remaining"], 50)
        self.assertEqual(data["model_calls"], 0)
        self.assertEqual(checked, [True])
        public = self.get_json("/api/provider")
        self.assertEqual(public["quota_snapshot"]["quota"]["remaining"], 45)
        self.assertEqual(public["app_daily_remaining"], 50)
        self.assertEqual(checked, [True])
        self.assertNotIn("PRIVATE-LABEL", content.decode())
        self.assertNotIn("SYNTHETIC-SECRET", content.decode())
        self.assertEqual({p.name: p.read_bytes() for p in self.data.data_dir.glob("*.sqlite3")}, before)

    def test_account_quota_check_rejects_cross_origin_and_unknown_options_before_network(self):
        registry = self.server.automation._provider_registry
        registry._quota_transport = Mock(side_effect=AssertionError("Invalid requests must not reach OpenRouter"))
        self.post_json("/api/provider", {"api_key": "SYNTHETIC-SECRET"})
        headers = {"Content-Type": "application/json", "X-Reviewdesk-Request": "1", "Origin": "https://example.invalid"}
        self.assertEqual(self.request("/api/provider/check", method="POST", body=b"{}", headers=headers)[0], 403)
        self.assertEqual(self.request("/api/provider/check", method="POST", body=b"{}", headers={"Content-Type":"application/json"})[0], 403)
        for payload in [{"url": "https://example.invalid"}, {"api_key": "SYNTHETIC-SECRET"}, []]:
            self.assertEqual(self.post_json("/api/provider/check", payload)[0], 400)
        self.assertEqual(self.post_json("/api/provider/check?extra=yes", {})[0], 400)
        registry._quota_transport.assert_not_called()

    def test_rate_diagnostics_do_not_expose_raw_provider_fields(self):
        failure = ProviderError("RAW-SECRET", code="rate_limit")
        failure.diagnostics = {"rate_limit_source": "provider", "provider_code": 429, "raw_error": "RAW-SECRET",
                               "remaining": True, "reset_at": float("inf"), "message": "RAW-SECRET"}
        with patch.object(self.server.automation, "check_provider_quota", side_effect=failure):
            status, _, content = self.post_json("/api/provider/check", {})
        self.assertEqual(status, 429)
        data = json.loads(content)
        self.assertEqual(data["diagnostics"], {"rate_limit_source": "provider", "provider_code": 429})
        self.assertIn("model provider", data["error"])
        self.assertNotIn("RAW-SECRET", content.decode())

    def test_exact_research_http_job_runs_without_a_key_and_makes_zero_provider_calls(self):
        status, _, content = self.post_json("/api/jobs", {"kind": "research", "dataset": "demo", "question": "How many reviews are stored?"})
        self.assertEqual(status, 202, content)
        job_id = json.loads(content)["job"]["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.server.automation.get_job(job_id)
            if job["status"] not in {"queued", "running", "cancelling"}:
                break
            threading.Event().wait(0.01)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["result"]["usage"]["calls"], 0)
        self.assertEqual(job["result"]["method"], "local")
        self.assertIn("15", job["result"]["answer"])
        self.assertIsNone(self.get_json("/api/analysis?dataset=demo")["report"])
        status, _, content = self.post_json("/api/jobs", {"kind": "research", "dataset": "demo", "question": "Which battery complaints recur?"})
        self.assertEqual(status, 202, content)
        job_id = json.loads(content)["job"]["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.server.automation.get_job(job_id)
            if job["status"] not in {"queued", "running", "cancelling"}:
                break
            threading.Event().wait(0.01)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["result"]["method"], "local_evidence")
        self.assertEqual(job["result"]["usage"]["calls"], 0)
        self.assertEqual(self.post_json("/api/jobs", {"kind": "analysis", "analysis_method": "openrouter", "allow_external": True})[0], 400)

    def test_buying_brief_http_validates_inputs_and_saves_local_product_scopes(self):
        payload = {"kind": "brief", "dataset": "workspace", "products": ["B0TEST0001", "B0TEST0002"], "priorities": ["comfort"], "allow_external": False}
        for changes in [{"products": []}, {"products": ["B0TEST0001", "B0TEST0001"]}, {"products": ["https://evil.invalid/dp/B0TEST0001"]}, {"priorities": ["price"]}, {"allow_external": "yes"}, {"dataset": "other"}]:
            self.assertEqual(self.post_json("/api/jobs", {**payload, **changes})[0], 400)
        self.seed_workspace([self.review(review_body="Comfortable fit."), self.review(asin="B0TEST0002", product_url="https://www.amazon.in/dp/B0TEST0002", review_body="The fit feels tight.")])
        self.data.name_product({"dataset": "workspace", "asin": "B0TEST0001", "name": "Fictional headphones"})
        status, _, content = self.post_json("/api/jobs", payload)
        self.assertEqual(status, 202, content)
        job_id = json.loads(content)["job"]["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.server.automation.get_job(job_id)
            if job["status"] not in {"queued", "running", "cancelling"}: break
            threading.Event().wait(0.01)
        self.assertEqual(job["status"], "succeeded", job)
        result = job["result"]
        self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(result["products"][0]["name"], "Fictional headphones")
        self.assertEqual([item["asin"] for item in result["products"]], payload["products"])
        self.assertEqual(result["method"], "local_evidence")
        self.assertEqual(result["tradeoffs"], [])
        self.assertTrue(any(item["id"] == job_id for item in self.get_json("/api/automation")["jobs"]))

    def test_buying_brief_http_uses_one_fake_request_then_a_revalidated_cache(self):
        from test_buying_brief import completion
        sent = []
        registry = ProviderRegistry(environ={}, transport=lambda payload: sent.append(payload) or completion(payload))
        registry.configure({"api_key": "SYNTHETIC-BUYING-KEY"})
        self.server.automation._provider_registry = registry
        self.seed_workspace([self.review(review_body="Comfortable fit and good battery life.")])
        payload = {"kind": "brief", "dataset": "workspace", "products": ["B0TEST0001"], "priorities": ["comfort"], "allow_external": True}
        for calls in [1, 0]:
            status, _, content = self.post_json("/api/jobs", payload)
            self.assertEqual(status, 202, content)
            job_id = json.loads(content)["job"]["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = self.server.automation.get_job(job_id)
                if job["status"] not in {"queued", "running", "cancelling"}: break
                threading.Event().wait(0.01)
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(job["result"]["method"], "openrouter")
            self.assertEqual(job["result"]["usage"]["calls"], calls)
        self.assertEqual(len(sent), 1)

    def wait_comparison(self, payload):
        status, _, content = self.post_json("/api/jobs", {"kind": "comparison", "dataset": "workspace", **payload})
        self.assertEqual(status, 202, content)
        job_id = json.loads(content)["job"]["id"]
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            job = self.server.automation.get_job(job_id)
            if job["status"] not in {"queued", "running", "cancelling"}:
                return job
            threading.Event().wait(0.01)
        self.fail("Comparison did not finish in time")

    def test_comparison_collects_only_missing_product_and_preserves_partial_outcome(self):
        a, b = "B0TEST0001", "B0TEST0002"
        self.seed_workspace([self.review()])
        calls = []
        def collect(urls, db, auth, **options):
            calls.append((urls, options))
            self.seed_workspace([self.review(asin=b, review_id="SECOND", product_url=f"https://www.amazon.in/dp/{b}")])
            return {"status": "partial", "products": [{"asin": b, "status": "paused", "inserted": 1, "pages_processed": 1}], "inserted": 1, "pages_processed": 1}
        self.server.automation._collection_runner = collect
        job = self.wait_comparison({"products": [a, b], "collect_missing": True, "max_pages": 3})
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(calls[0][0], [f"https://www.amazon.in/dp/{b}"])
        self.assertEqual(calls[0][1]["max_pages"], 3)
        self.assertEqual(job["result"]["collection"]["status"], "partial")
        self.assertEqual(job["result"]["usage"]["calls"], 0)
        self.assertEqual([item["stored_reviews"] for item in job["result"]["products"]], [1, 1])
        self.assertGreaterEqual(job["result"]["timings"]["total_seconds"], job["result"]["timings"]["collection_seconds"])
        repeat = self.wait_comparison({"products": [a, b], "collect_missing": True})
        self.assertEqual(repeat["result"]["collection_mode"], "saved_reviews")
        self.assertEqual(repeat["result"]["timings"]["collection_seconds"], 0)
        self.assertEqual(len(calls), 1)

    def test_comparison_blocked_collection_preserves_evidence_and_makes_no_ai_request(self):
        self.server.automation._collection_runner = lambda *args, **kwargs: {"status": "blocked", "products": [], "detail": "Synthetic sign-in required."}
        job = self.wait_comparison({"products": ["B0TEST0001", "B0TEST0002"], "collect_missing": True, "allow_external": True})
        self.assertEqual(job["status"], "blocked", job)
        self.assertEqual(job["result"]["usage"]["calls"], 0)
        self.assertEqual(job["result"]["method"], "local_evidence")

    def test_comparison_http_uses_one_fake_request_then_cache_and_rejects_unsafe_links(self):
        from test_product_comparison import completion
        sent = []
        registry = ProviderRegistry(environ={}, transport=lambda payload: sent.append(payload) or completion(payload))
        registry.configure({"api_key": "SYNTHETIC-COMPARISON-KEY"})
        self.server.automation._provider_registry = registry
        a, b = "B0TEST0001", "B0TEST0002"
        self.seed_workspace([self.review(), self.review(asin=b, review_id="SECOND", product_url=f"https://www.amazon.in/dp/{b}")])
        for expected in (1, 0):
            job = self.wait_comparison({"products": [a, b], "allow_external": True})
            self.assertEqual(job["result"]["method"], "openrouter", job)
            self.assertEqual(job["result"]["usage"]["calls"], expected)
        self.assertEqual(len(sent), 1)
        for products in [[a, a], [a, "https://amazon.in.evil.test/dp/" + b], [a, "https://amzn.in/short"]]:
            status, _, content = self.post_json("/api/jobs", {"kind": "comparison", "products": products})
            self.assertEqual(status, 400, content)

    def test_http_provider_errors_do_not_echo_messages_or_unsafe_retry_values(self):
        failure = ProviderError("RAW-REQUEST-SECRET", code="rate_limit", retry_after=12.2)
        with patch.object(self.server.automation, "analysis_plan", side_effect=failure):
            status, _, content = self.request("/api/analysis/plan?dataset=demo")
        self.assertEqual(status, 429, content)
        self.assertEqual(json.loads(content)["code"], "rate_limit")
        self.assertEqual(json.loads(content)["retry_after"], 13)
        self.assertNotIn("RAW-REQUEST-SECRET", content.decode())
        failure = ProviderError("RAW-REQUEST-SECRET", code="RAW-REQUEST-SECRET", retry_after=float("inf"))
        with patch.object(self.server.automation, "analysis_plan", side_effect=failure):
            status, _, content = self.request("/api/analysis/plan?dataset=demo")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(content)["code"], "provider_error")
        self.assertNotIn("retry_after", json.loads(content))
        self.assertNotIn("RAW-REQUEST-SECRET", content.decode())

    def test_research_http_rate_limit_returns_sources_and_next_request_respects_cooldown(self):
        sent = []
        def limited(payload):
            sent.append(payload)
            raise ProviderError("RAW-PROVIDER-SECRET", code="rate_limit", retry_after=30)
        registry = ProviderRegistry(environ={}, transport=limited)
        registry.configure({"api_key": "SYNTHETIC-RESEARCH-KEY"})
        self.server.automation._provider_registry = registry
        for expected_calls, expected_reason in [(1, "rate_limit"), (0, "cooldown")]:
            status, _, content = self.post_json("/api/jobs", {"kind": "research", "dataset": "demo",
                "question": "What do reviewers say about battery?", "allow_external": True})
            self.assertEqual(status, 202, content)
            job_id = json.loads(content)["job"]["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = self.server.automation.get_job(job_id)
                if job["status"] not in {"queued", "running", "cancelling"}:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(job["result"]["method"], "local_evidence")
            self.assertEqual(job["result"]["notice"]["code"], expected_reason)
            self.assertEqual(job["result"]["usage"]["calls"], expected_calls)
            self.assertTrue(job["result"]["evidence"])
            self.assertNotIn("RAW-PROVIDER-SECRET", json.dumps(job))
        self.assertEqual(len(sent), 1)
        self.assertIsInstance(self.get_json("/api/provider")["retry_at"], int)

    def test_openrouter_http_job_uses_a_fake_transport_and_reuses_its_validated_report(self):
        sent = []
        def transport(payload):
            sent.append(payload)
            entry = json.loads(payload["messages"][-1]["content"])["reviews"][0]
            content = json.dumps({"summary": "A fictional review describes product use.",
                "themes": [{"label": "Use", "explanation": "One checked example.",
                            "evidence": [next(iter(entry["body"] or entry["title"]))]}],
                "limitations": ["A test interpretation."]})
            return {"model": "fictional/model:free", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0},
                    "choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        self.server.automation._provider_registry = ProviderRegistry(environ={}, transport=transport)
        self.assertEqual(self.post_json("/api/provider", {"api_key": "SYNTHETIC-INTEGRATION-SECRET"})[0], 200)
        payload = {"kind": "analysis", "dataset": "demo", "analysis_method": "openrouter", "depth": "quick", "allow_external": True}
        for index in range(2):
            status, _, content = self.post_json("/api/jobs", payload)
            self.assertEqual(status, 202, content)
            job_id = json.loads(content)["job"]["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = self.server.automation.get_job(job_id)
                if job["status"] not in {"queued", "running", "cancelling"}:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(job["result"]["cached"], index == 1)
        report = self.get_json("/api/analysis?dataset=demo")["report"]
        self.assertEqual(report["method"], "openrouter")
        self.assertTrue(report["scope"]["analysis_complete"])
        self.assertEqual(report["usage"]["calls"], 1)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("SYNTHETIC-INTEGRATION-SECRET", json.dumps(sent))
        self.assertNotIn("B0DEMO", json.dumps(sent))
        for filename in ("jobs.sqlite3", "analysis.sqlite3"):
            self.assertNotIn(b"SYNTHETIC-INTEGRATION-SECRET", (self.data.data_dir / filename).read_bytes())


if __name__ == "__main__":
    unittest.main()
