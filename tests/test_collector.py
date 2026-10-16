"""Offline controller tests. FakeBrowser cannot perform network requests."""

import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector import PageHTTPError, PageSnapshot, SafeNavigationError, collect_product, load_input_urls
from review_store import ReviewStore


ASIN = "B0DEMO0001"
PRODUCT = f"https://www.amazon.in/dp/{ASIN}"
FIRST = f"https://www.amazon.in/product-reviews/{ASIN}?reviewerType=all_reviews"
SECOND = FIRST + "&pageNumber=2"


def html(review_id="DEMO-R1", rating="4.0"):
    return f'''<html><title>Synthetic reviews</title><div data-hook="review" id="{review_id}">
      <span class="a-profile-name">Demo reviewer</span>
      <span data-hook="review-title">Fictional review</span>
      <span data-hook="review-star-rating">{rating} out of 5 stars</span>
      <span data-hook="review-body">Synthetic evidence for an offline test.</span>
    </div></html>'''


class FakeBrowser:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.loads = []
        self.advances = 0
        self.index = 0

    def load(self, url):
        self.loads.append(url)
        self.index = 0
        if isinstance(self.snapshots[0], BaseException):
            raise self.snapshots[0]
        return self.snapshots[0]

    def advance(self):
        self.advances += 1
        self.index += 1
        if self.index >= len(self.snapshots):
            return None
        value = self.snapshots[self.index]
        if isinstance(value, BaseException):
            raise value
        return value


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ReviewStore(self.root / "reviews.sqlite3")
        self.addCleanup(self.store.close)

    def test_page_budget_resume_and_completed_skip(self):
        browser = FakeBrowser([PageSnapshot(FIRST, html(), has_next=True, next_url=SECOND)])
        first = collect_product(browser, self.store, PRODUCT, max_pages=1)
        self.assertEqual((first["status"], first["inserted"]), ("paused", 1))
        self.assertEqual(browser.advances, 0)  # budget never requests the next page
        self.assertEqual(self.store.get_checkpoint(ASIN)["next_url"], SECOND)
        browser2 = FakeBrowser([PageSnapshot(SECOND, html("DEMO-R2"))])
        second = collect_product(browser2, self.store, PRODUCT, resume=True)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(browser2.loads, [SECOND])
        self.assertEqual(self.store.get_checkpoint(ASIN)["pages_completed"], 2)
        self.assertEqual(self.store.summary()["total_reviews"], 2)
        browser3 = FakeBrowser([])
        skipped = collect_product(browser3, self.store, PRODUCT, resume=True)
        self.assertTrue(skipped["skipped"])
        self.assertEqual(browser3.loads, [])

    def test_restart_retains_rows_and_restarts_cursor(self):
        collect_product(FakeBrowser([PageSnapshot(FIRST, html(), has_next=True, next_url=SECOND)]), self.store, PRODUCT, max_pages=1)
        browser = FakeBrowser([PageSnapshot(FIRST, html())])
        report = collect_product(browser, self.store, PRODUCT, resume=True, restart=True)
        self.assertEqual(browser.loads, [FIRST])
        self.assertEqual(report["inserted"], 0)
        self.assertEqual(self.store.summary()["total_reviews"], 1)
        self.assertEqual(self.store.get_checkpoint(ASIN)["pages_completed"], 1)

    def test_portal_pagination_commits_first_page_and_resumes_saved_cursor(self):
        portal = f"https://www.amazon.in/portal/customer-reviews/{ASIN}/ref=cm_cr_arp_d_paging_btm_2?pageNumber=2"
        first = collect_product(FakeBrowser([PageSnapshot(FIRST, html(), has_next=True, next_url=portal)]),
                                self.store, PRODUCT, max_pages=1)
        self.assertEqual((first["status"], first["inserted"]), ("paused", 1))
        self.assertEqual(self.store.get_checkpoint(ASIN)["next_url"], portal)
        browser = FakeBrowser([PageSnapshot(portal, html("DEMO-R2"))])
        resumed = collect_product(browser, self.store, PRODUCT, resume=True)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(browser.loads, [portal])
        self.assertEqual(self.store.summary()["total_reviews"], 2)

    def test_rendered_portal_pagination_saves_base_and_replays_to_resume(self):
        portal2 = f"https://www.amazon.in/portal/customer-reviews/{ASIN}/ref=cm_cr_arp_d_paging_btm_2"
        portal3 = f"https://www.amazon.in/portal/customer-reviews/{ASIN}/ref=cm_cr_arp_d_paging_btm_3"
        first = PageSnapshot(FIRST, html(), has_next=True)
        second = PageSnapshot(portal2, html("DEMO-R2"), has_next=True)
        report = collect_product(FakeBrowser([first, second]), self.store, PRODUCT, max_pages=2)
        self.assertEqual((report["status"], report["inserted"], report["pages_processed"]), ("paused", 2, 2))
        checkpoint = self.store.get_checkpoint(ASIN)
        self.assertEqual((checkpoint["next_url"], checkpoint["replay_steps"]), (FIRST, 2))
        browser = FakeBrowser([first, second, PageSnapshot(portal3, html("DEMO-R3"))])
        resumed = collect_product(browser, self.store, PRODUCT, resume=True, max_pages=1)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(browser.loads, [FIRST])
        self.assertEqual(browser.advances, 2)
        self.assertEqual(self.store.summary()["total_reviews"], 3)

    def test_ajax_resume_replays_then_processes_saved_position(self):
        initial = PageSnapshot(FIRST, html(), has_next=True)
        collect_product(FakeBrowser([initial]), self.store, PRODUCT, max_pages=1)
        self.assertEqual(self.store.get_checkpoint(ASIN)["replay_steps"], 1)
        browser = FakeBrowser([initial, PageSnapshot(FIRST, html("DEMO-R2"))])
        report = collect_product(browser, self.store, PRODUCT, resume=True, max_pages=1)
        self.assertEqual((report["status"], report["pages_processed"], browser.advances), ("complete", 1, 1))
        self.assertEqual(self.store.summary()["total_reviews"], 2)

    def test_repeated_identity_during_replay_does_not_advance_checkpoint(self):
        self.store.save_page(ASIN, [], next_url=FIRST, pages_completed=2,
                             replay_steps=2, status="paused")
        repeated = PageSnapshot(FIRST, html(), has_next=True)
        browser = FakeBrowser([repeated, repeated])
        report = collect_product(browser, self.store, PRODUCT, resume=True)
        self.assertEqual((report["status"], report["pages_processed"], report["inserted"]), ("partial", 0, 0))
        self.assertEqual(browser.advances, 1)
        checkpoint = self.store.get_checkpoint(ASIN)
        self.assertEqual((checkpoint["pages_completed"], checkpoint["replay_steps"]), (2, 2))
        self.assertIn("not advanced", report["detail"])

    def test_resumed_target_cannot_repeat_a_page_seen_during_replay(self):
        self.store.save_page(ASIN, [], next_url=FIRST, pages_completed=1,
                             replay_steps=1, status="paused")
        initial = PageSnapshot(FIRST, html(), has_next=True)
        browser = FakeBrowser([initial, initial])
        report = collect_product(browser, self.store, PRODUCT, resume=True, max_pages=1)
        self.assertEqual((report["status"], report["pages_processed"]), ("partial", 0))
        checkpoint = self.store.get_checkpoint(ASIN)
        self.assertEqual((checkpoint["pages_completed"], checkpoint["replay_steps"]), (1, 1))

    def test_all_stored_reviews_do_not_mean_a_distinct_resumed_page_is_complete(self):
        initial = PageSnapshot(FIRST, html(), has_next=True)
        second = PageSnapshot(FIRST, html("DEMO-R2"), has_next=True)
        rows = [{"asin": ASIN, "product_url": PRODUCT, "review_id": identity}
                for identity in ["DEMO-R1", "DEMO-R2"]]
        self.store.save_page(ASIN, rows, next_url=FIRST, pages_completed=1,
                             replay_steps=1, status="paused")
        browser = FakeBrowser([initial, second])
        report = collect_product(browser, self.store, PRODUCT, resume=True, max_pages=1)
        self.assertEqual((report["status"], report["pages_processed"], report["inserted"]), ("paused", 1, 0))
        checkpoint = self.store.get_checkpoint(ASIN)
        self.assertEqual((checkpoint["pages_completed"], checkpoint["replay_steps"]), (2, 2))

    def test_duplicate_container_counts_do_not_hide_repeated_identity_sets(self):
        browser = FakeBrowser([PageSnapshot(FIRST, html(), has_next=True, next_url=SECOND),
                               PageSnapshot(SECOND, html() + html(), has_next=True, next_url=FIRST)])
        report = collect_product(browser, self.store, PRODUCT)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["pages_processed"], 1)

    def test_replay_limit_stops_before_browser(self):
        self.store.save_page(ASIN, [], next_url=FIRST, pages_completed=21, replay_steps=21, status="paused")
        browser = FakeBrowser([])
        report = collect_product(browser, self.store, PRODUCT, resume=True)
        self.assertEqual(report["status"], "paused")
        self.assertEqual(browser.loads, [])

    def test_ajax_limit_pause_has_a_resumable_explicit_override(self):
        initial = PageSnapshot(FIRST, html(), has_next=True)
        second = PageSnapshot(FIRST, html("DEMO-R2"), has_next=True)
        third = PageSnapshot(FIRST, html("DEMO-R3"))
        report = collect_product(FakeBrowser([initial, second, third]), self.store, PRODUCT, max_pages=3, replay_limit=1)
        self.assertEqual(report["status"], "paused")
        self.assertIn("--replay-limit 2", report["detail"])
        resumed = collect_product(FakeBrowser([initial, second, third]), self.store, PRODUCT, resume=True, replay_limit=2)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(self.store.summary()["total_reviews"], 3)

    def test_wrong_product_is_rejected_before_replay_click(self):
        self.store.save_page(ASIN, [], next_url=FIRST, pages_completed=1, replay_steps=1, status="paused")
        browser = FakeBrowser([PageSnapshot("https://www.amazon.in/product-reviews/B0DEMO0002", html())])
        report = collect_product(browser, self.store, PRODUCT, resume=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(browser.advances, 0)

    def test_blocked_zero_unknown_and_wrong_product_are_distinct(self):
        cases = [
            (PageSnapshot("https://www.amazon.in/ap/signin", '<input id="ap_email">'), "blocked"),
            (PageSnapshot(FIRST, '<input id="captchacharacters">'), "blocked"),
            (PageSnapshot(FIRST, '<div data-hook="no-reviews-section">No customer reviews</div>'), "zero_reviews"),
            (PageSnapshot(FIRST, "<html>Changed layout</html>"), "failed"),
            (PageSnapshot("https://www.amazon.in/product-reviews/B0DEMO0002", "<p>No customer reviews</p>"), "failed"),
        ]
        for snapshot, expected in cases:
            with self.subTest(expected=expected, url=snapshot.url):
                report = collect_product(FakeBrowser([snapshot]), self.store, PRODUCT)
                self.assertEqual(report["status"], expected)
                self.assertEqual(self.store.summary()["total_reviews"], 0)

    def test_repeated_review_page_stops_partial(self):
        browser = FakeBrowser([
            PageSnapshot(FIRST, html(), has_next=True, next_url=SECOND),
            PageSnapshot(SECOND, html(), has_next=True, next_url=FIRST),
        ])
        report = collect_product(browser, self.store, PRODUCT)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(self.store.summary()["total_reviews"], 1)

    def test_navigation_error_retains_retry_cursor(self):
        browser = FakeBrowser([
            PageSnapshot(FIRST, html(), has_next=True, next_url=SECOND),
            RuntimeError("Synthetic navigation failure"),
        ])
        report = collect_product(browser, self.store, PRODUCT)
        checkpoint = self.store.get_checkpoint(ASIN)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(checkpoint["next_url"], SECOND)
        self.assertEqual(checkpoint["pages_completed"], 1)
        self.assertEqual(self.store.summary()["total_reviews"], 1)

    def test_failed_row_validation_does_not_skip_unsaved_page(self):
        report = collect_product(FakeBrowser([PageSnapshot(FIRST, html(rating="9"), has_next=True, next_url=SECOND)]), self.store, PRODUCT)
        checkpoint = self.store.get_checkpoint(ASIN)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(checkpoint["next_url"], FIRST)
        self.assertEqual(checkpoint["pages_completed"], 0)
        self.assertEqual(self.store.summary()["total_reviews"], 0)

    def test_interrupt_retains_saved_position(self):
        browser = FakeBrowser([PageSnapshot(FIRST, html(), has_next=True, next_url=SECOND), KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            collect_product(browser, self.store, PRODUCT)
        checkpoint = self.store.get_checkpoint(ASIN)
        self.assertEqual(checkpoint["status"], "paused")
        self.assertEqual(checkpoint["next_url"], SECOND)
        self.assertEqual(self.store.summary()["total_reviews"], 1)

    def test_interrupt_during_page_save_keeps_unsaved_page_cursor(self):
        original_save = self.store.save_page

        def interrupt_rows(asin, rows, **kwargs):
            if rows:
                raise KeyboardInterrupt
            return original_save(asin, rows, **kwargs)

        with patch.object(self.store, "save_page", side_effect=interrupt_rows):
            with self.assertRaises(KeyboardInterrupt):
                collect_product(FakeBrowser([PageSnapshot(FIRST, html(), has_next=True, next_url=SECOND)]), self.store, PRODUCT)
        checkpoint = self.store.get_checkpoint(ASIN)
        self.assertEqual(checkpoint["status"], "paused")
        self.assertEqual(checkpoint["next_url"], FIRST)
        self.assertEqual(checkpoint["pages_completed"], 0)
        self.assertEqual(self.store.summary()["total_reviews"], 0)

    def test_input_validation_and_product_deduplication(self):
        source = self.root / "input.csv"
        source.write_text(f"url\n{PRODUCT}\n{FIRST}\n\n", encoding="utf-8")
        self.assertEqual(load_input_urls(source), [PRODUCT])
        source.write_text(PRODUCT + "\nhttps://amazon.in.evil.test/dp/" + ASIN, encoding="utf-8")
        with self.assertRaises(ValueError):
            load_input_urls(source)

    def test_cancel_before_load_creates_resumable_checkpoint_without_navigation(self):
        browser = FakeBrowser([])
        report = collect_product(browser, self.store, PRODUCT, cancelled=lambda: True)
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["status"], "paused")
        self.assertEqual(browser.loads, [])
        self.assertEqual(self.store.get_checkpoint(ASIN)["next_url"], FIRST)

    def test_cancel_after_commit_keeps_next_cursor_and_does_not_advance(self):
        stopped = False
        events = []

        def progress(event):
            nonlocal stopped
            events.append(event)
            if event["pages_processed"]:
                self.assertEqual(self.store.summary()["total_reviews"], 1)
                self.assertEqual(self.store.get_checkpoint(ASIN)["next_url"], SECOND)
                stopped = True

        browser = FakeBrowser([PageSnapshot(FIRST, html(), has_next=True, next_url=SECOND)])
        report = collect_product(browser, self.store, PRODUCT, max_pages=3,
                                 on_progress=progress, cancelled=lambda: stopped)
        self.assertEqual((report["status"], report["inserted"], report["pages_processed"]), ("paused", 1, 1))
        self.assertTrue(report["cancelled"])
        self.assertEqual(browser.advances, 0)
        self.assertEqual(events[-1]["status"], "paused")

    def test_progress_observer_failure_does_not_undo_saved_page(self):
        def failed_observer(event):
            raise RuntimeError("Synthetic observer failure")

        report = collect_product(FakeBrowser([PageSnapshot(FIRST, html())]), self.store,
                                 PRODUCT, on_progress=failed_observer)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(self.store.summary()["total_reviews"], 1)

    def test_cancel_during_replay_retains_original_replay_position(self):
        self.store.save_page(ASIN, [], next_url=FIRST, pages_completed=1,
                             replay_steps=1, status="paused")
        stopped = False

        class CancelOnLoad(FakeBrowser):
            def load(inner, url):
                nonlocal stopped
                snapshot = super().load(url)
                stopped = True
                return snapshot

        browser = CancelOnLoad([PageSnapshot(FIRST, html(), has_next=True)])
        report = collect_product(browser, self.store, PRODUCT, resume=True,
                                 cancelled=lambda: stopped)
        self.assertTrue(report["cancelled"])
        self.assertEqual(browser.advances, 0)
        self.assertEqual(self.store.get_checkpoint(ASIN)["replay_steps"], 1)

    def test_http_access_block_records_blocked_checkpoint(self):
        error = RuntimeError("Synthetic HTTP 429")
        error.http_status = 429
        report = collect_product(FakeBrowser([error]), self.store, PRODUCT)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(self.store.get_checkpoint(ASIN)["status"], "blocked")

    def test_known_http_failures_expose_only_validated_status(self):
        for status in [404, 500, 503]:
            with self.subTest(status=status):
                error = PageHTTPError(status)
                error.args = ("Cookie: PRIVATE-SYNTHETIC-SECRET",)
                events = []
                report = collect_product(FakeBrowser([error]), self.store, PRODUCT, on_progress=events.append)
                self.assertEqual((report["status"], report["http_status"]), ("failed", status))
                self.assertEqual(events[-1]["http_status"], status)
                self.assertIn(f"HTTP {status}", report["detail"])
                self.assertNotIn("PRIVATE-SYNTHETIC-SECRET", str(report))
                self.assertNotIn("PRIVATE-SYNTHETIC-SECRET", self.store.get_checkpoint(ASIN)["detail"])

    def test_http_failure_type_rejects_unvalidated_status_values(self):
        for status in [True, "404", None, 99, 600, {"status": 404}]:
            with self.subTest(status=status), self.assertRaises(ValueError):
                PageHTTPError(status)

    def test_browser_diagnostics_do_not_leak_into_checkpoints_or_progress(self):
        events = []
        private = "Cookie: session-id=PRIVATE-SYNTHETIC-SECRET"
        report = collect_product(FakeBrowser([RuntimeError(private)]), self.store,
                                 PRODUCT, on_progress=events.append)
        self.assertEqual(report["status"], "failed")
        self.assertNotIn(private, report["detail"])
        self.assertNotIn(private, self.store.get_checkpoint(ASIN)["detail"])
        self.assertTrue(all(private not in event["detail"] for event in events))

    def test_safe_navigation_diagnostic_is_structured_and_checkpoint_stays_readable(self):
        diagnostic = {"origin": "https://www.amazon.in", "path": "/unexpected-route",
                      "detected_state": "unknown", "expected_asin": ASIN, "actual_asin": None}
        error = SafeNavigationError("Browser did not reach the requested Amazon.in product.", diagnostic)
        events = []
        report = collect_product(FakeBrowser([error]), self.store, PRODUCT, on_progress=events.append)
        self.assertEqual(report["diagnostic"], diagnostic)
        self.assertEqual(events[-1]["diagnostic"], diagnostic)
        self.assertEqual(self.store.get_checkpoint(ASIN)["detail"], str(error))
        self.assertNotIn("detected_state", self.store.get_checkpoint(ASIN)["detail"])

    def test_live_entry_point_syntax_without_executing_it(self):
        source = Path(__file__).resolve().parents[1] / "scraper.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        self.assertIsInstance(tree, ast.Module)


if __name__ == "__main__":
    unittest.main()
