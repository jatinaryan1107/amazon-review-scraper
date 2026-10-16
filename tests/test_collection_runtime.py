"""Exercise the runtime using fake browser objects and synthetic review HTML."""

import builtins
import importlib
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

import collection_runtime as runtime
from browser_tool import AccessBlockedError
from collector import PageSnapshot
from review_parser import extract_asin
from review_store import ReviewStore


ASIN = "B0DEMO0001"
URL = f"https://www.amazon.in/dp/{ASIN}"
OTHER = "https://www.amazon.in/dp/B0DEMO0002"
STATE = {"cookies": [{"name": "synthetic", "value": "private-test-value"}], "origins": []}


class FakePage:
    def __init__(self):
        self.url = "https://www.amazon.in/"
        self.html = "<h1>Synthetic signed-in homepage</h1>"
        self.closed = False
        self.waits = []
        self.wait_callback = None

    def goto(self, url, **kwargs):
        self.url = url

    def content(self):
        return self.html

    def title(self):
        return "Synthetic homepage"

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        if self.wait_callback is not None:
            self.wait_callback()


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.saved = []
        self.routes = []

    def new_page(self):
        return self.page

    def storage_state(self, *, path):
        self.saved.append(path)
        Path(path).write_text(json.dumps(STATE), encoding="utf-8")

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))


class FakeBrowser:
    def __init__(self):
        self.context = FakeContext()
        self.context_options = []
        self.closed = False

    def new_context(self, **kwargs):
        self.context_options.append(kwargs)
        return self.context

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.chromium = self
        self.browser = FakeBrowser()
        self.launches = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def launch(self, **kwargs):
        self.launches.append(kwargs)
        return self.browser


class FakeTool:
    loads = []
    blocked = False

    def __init__(self, page, **kwargs):
        self.page = page

    def load(self, url):
        self.loads.append(url)
        if self.blocked:
            raise AccessBlockedError(429)
        asin = extract_asin(url)
        html = f'''<div data-hook="review" id="TEST-{asin}">
          <span class="a-profile-name">Synthetic reviewer</span>
          <span data-hook="review-title">Fictional evidence</span>
          <span data-hook="review-star-rating">4 out of 5 stars</span>
          <span data-hook="review-body">Only a fake runtime test.</span></div>'''
        return PageSnapshot(url, html)

    def advance(self):
        raise AssertionError("No next page exists in this fake")


class CollectionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth.json"
        self.auth.write_text(json.dumps(STATE), encoding="utf-8")
        self.db = self.root / "reviews.sqlite3"
        self.playwright = FakePlaywright()
        factory_patch = patch.object(runtime, "_playwright_factory", return_value=self.playwright)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        tool_patch = patch.object(runtime, "PlaywrightBrowser", FakeTool)
        tool_patch.start()
        self.addCleanup(tool_patch.stop)
        FakeTool.loads = []
        FakeTool.blocked = False

    def test_import_has_no_live_browser_or_scraper_dependency(self):
        original_import = builtins.__import__

        def restricted(name, *args, **kwargs):
            if name.startswith("playwright") or name == "scraper":
                raise AssertionError("Offline imports cannot load live dependencies")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=restricted):
            importlib.reload(runtime)
        # Reload restores the module factory, so restore the injected test seam.
        runtime._playwright_factory = self.factory
        runtime.PlaywrightBrowser = FakeTool
        self.factory.assert_not_called()

    def test_batch_reuses_one_context_and_persists_session_atomically(self):
        report = runtime.run_collection([URL, OTHER, URL], self.db, self.auth)
        self.assertEqual((report["status"], report["inserted"], len(report["products"])), ("completed", 2, 2))
        self.assertEqual(len(self.playwright.launches), 1)
        self.assertEqual(len(self.playwright.browser.context_options), 1)
        self.assertEqual(len(FakeTool.loads), 2)
        self.assertEqual(json.loads(self.auth.read_text(encoding="utf-8")), STATE)
        self.assertEqual(list(self.root.glob(".auth-*.json")), [])
        self.assertTrue(self.playwright.browser.closed)
        with ReviewStore(self.db, read_only=True) as store:
            self.assertEqual(store.summary()["total_reviews"], 2)

    def test_invalid_input_and_missing_session_never_launch_browser(self):
        for urls in [["https://example.com/"], [], [URL] * 2 + ["https://amazon.in.evil.test/dp/B0DEMO0001"]]:
            with self.subTest(urls=urls), self.assertRaises(ValueError):
                runtime.run_collection(urls, self.db, self.auth)
        self.auth.unlink()
        report = runtime.run_collection([URL], self.db, self.auth)
        self.assertEqual(report["status"], "blocked")
        self.factory.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_access_block_stops_remaining_products_and_preserves_old_session(self):
        FakeTool.blocked = True
        before = self.auth.read_bytes()
        report = runtime.run_collection([URL, OTHER], self.db, self.auth)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(len(FakeTool.loads), 1)
        self.assertEqual(self.auth.read_bytes(), before)
        self.assertTrue(self.playwright.browser.closed)

    def test_cancel_before_start_has_no_browser_or_database_side_effects(self):
        stop = Event()
        stop.set()
        report = runtime.run_collection([URL], self.db, self.auth, cancelled=stop)
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["status"], "paused")
        self.factory.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_cancel_between_products_retains_committed_review(self):
        stop = Event()

        def progress(event):
            if event.get("event") == "product_finished":
                stop.set()

        report = runtime.run_collection([URL, OTHER], self.db, self.auth,
                                        cancelled=stop, on_progress=progress)
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["inserted"], 1)
        self.assertEqual(len(FakeTool.loads), 1)

    def test_session_status_never_returns_cookie_contents(self):
        status = runtime.session_status(self.auth)
        self.assertTrue(status["valid"])
        self.assertFalse(status["verified"])
        self.assertNotIn("private-test-value", json.dumps(status))
        self.auth.write_text("not json", encoding="utf-8")
        self.assertFalse(runtime.session_status(self.auth)["valid"])

    def test_manual_auth_requires_finish_and_never_replaces_session_on_cancel(self):
        finished, cancelled = Event(), Event()
        before = self.auth.read_bytes()

        def progress(event):
            self.assertEqual(event["status"], "awaiting_login")
            cancelled.set()

        report = runtime.run_auth(self.auth, finished, cancelled, progress)
        self.assertTrue(report["cancelled"])
        self.assertEqual(self.auth.read_bytes(), before)
        self.assertTrue(self.playwright.browser.closed)

    def test_manual_auth_saves_only_after_user_finish_and_checks_access_block(self):
        finished = Event()
        report = runtime.run_auth(self.auth, finished, None,
                                  lambda event: finished.set())
        self.assertEqual(report["status"], "completed")
        self.assertEqual(len(self.playwright.browser.context.saved), 1)
        self.playwright.browser.context.page.html = '<input id="ap_email">'
        before = self.auth.read_bytes()
        report = runtime.run_auth(self.auth, finished, None)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(self.auth.read_bytes(), before)

    def test_manual_auth_wait_pumps_driver_and_checks_updated_location_before_save(self):
        finished = Event()
        page = self.playwright.browser.context.page

        def finish_after_driver_wait():
            page.url = "https://example.com/"
            finished.set()

        page.wait_callback = finish_after_driver_wait
        before = self.auth.read_bytes()
        report = runtime.run_auth(self.auth, finished, None)
        self.assertEqual(page.waits, [100])
        self.assertEqual(report["status"], "failed")
        self.assertIn("Amazon.in", report["detail"])
        self.assertEqual(self.auth.read_bytes(), before)

    def test_missing_browser_dependency_has_actionable_nonsecret_error(self):
        with patch.object(runtime, "_playwright_factory", side_effect=ImportError("secret path")):
            report = runtime.run_collection([URL], self.db, self.auth)
        self.assertEqual(report["status"], "failed")
        self.assertIn("Playwright", report["detail"])
        self.assertNotIn("secret path", json.dumps(report))


class PacingTests(unittest.TestCase):
    def test_slow_responses_do_not_add_another_fixed_delay(self):
        now = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        metrics = {}
        pacer = runtime.MinimumInterval(1, clock=lambda: now[0], sleep=sleep, metrics=metrics)
        pacer()
        self.assertEqual(sleeps, [])
        now[0] = 0.4
        pacer()
        self.assertAlmostEqual(sum(sleeps), 0.6)
        now[0] = 4.0
        pacer()
        self.assertAlmostEqual(sum(sleeps), 0.6)
        self.assertAlmostEqual(metrics["wait_seconds"], 0.6)

    def test_pacing_can_be_cancelled_before_the_next_request(self):
        stop = Event()
        pacer = runtime.MinimumInterval(1, cancelled=stop)
        stop.set()
        with self.assertRaises(runtime.CollectionCancelled):
            pacer()


if __name__ == "__main__":
    unittest.main()
