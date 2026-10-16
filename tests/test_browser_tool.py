"""Test browser decisions with in-memory fakes; no Playwright or network needed."""

import builtins
import importlib
import unittest
from unittest.mock import patch

import browser_tool
from browser_tool import NEXT_SELECTORS, PlaywrightBrowser


ASIN = "B0DEMO0001"
FIRST = f"https://www.amazon.in/product-reviews/{ASIN}?pageNumber=1"
SECOND = f"https://www.amazon.in/product-reviews/{ASIN}?pageNumber=2"
OTHER = "https://www.amazon.in/product-reviews/B0DEMO0002"


def review_html(body="First fictional review"):
    return f'<div data-hook="review"><span data-hook="review-body">{body}</span></div>'


class FakeTimeout(Exception):
    pass


class FakeControl:
    def __init__(self, href=None, *, visible=True, enabled=True, disabled=False, on_click=None):
        self.href = href
        self.visible = visible
        self.enabled = enabled
        self.disabled = disabled
        self.on_click = on_click
        self.clicks = []

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def evaluate(self, expression):
        # The real read-only check also accounts for disabled ancestor elements.
        return self.disabled

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def click(self, **kwargs):
        self.clicks.append(kwargs)
        if self.on_click:
            self.on_click()


class FakeLocator:
    def __init__(self, controls=(), texts=()):
        self.controls = controls
        self.texts = texts

    def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]

    def all_text_contents(self):
        return list(self.texts)


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakePage:
    """The complete fake contains no networking or real browser implementation."""

    def __init__(self):
        self.url = FIRST
        self.html = review_html()
        self.heading = "Synthetic reviews"
        self.texts = ["First fictional review"]
        self.controls = {}
        self.loads = []
        self.goto_actions = []
        self.waits = []
        self.ready_timeout = False
        self.change_callback = None

    def content(self):
        return self.html

    def title(self):
        return self.heading

    def goto(self, url, **kwargs):
        self.loads.append((url, kwargs))
        self.url = url
        action = self.goto_actions.pop(0) if self.goto_actions else {}
        for name, value in action.items():
            if name not in {"error", "status"}:
                setattr(self, name, value)
        if "error" in action:
            raise action["error"]
        return FakeResponse(action.get("status", 200))

    def locator(self, selector):
        if selector == '[data-hook="review"]':
            return FakeLocator(texts=self.texts)
        return FakeLocator(self.controls.get(selector, ()))

    def wait_for_function(self, expression, *, timeout, arg=None):
        self.waits.append((expression, timeout, arg))
        if arg is None:
            if self.ready_timeout:
                raise FakeTimeout("content readiness timed out")
            return
        if self.change_callback:
            self.change_callback()
        blocked = 'id="ap_email"' in self.html or 'id="captchacharacters"' in self.html
        if not blocked and (not self.texts or self.texts == arg):
            raise FakeTimeout("review content did not change")


class BrowserToolTests(unittest.TestCase):
    def setUp(self):
        self.page = FakePage()
        self.sleeps = []

    def tool(self, **kwargs):
        return PlaywrightBrowser(self.page, timeout_error=FakeTimeout,
                                 sleep=self.sleeps.append, **kwargs)

    def test_import_does_not_load_playwright_or_scraper_entrypoint(self):
        original_import = builtins.__import__

        def restricted_import(name, *args, **kwargs):
            if name.startswith("playwright") or name == "scraper":
                raise AssertionError("Offline imports must not load live code")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=restricted_import):
            importlib.reload(browser_tool)
            self.tool().load(FIRST)

    def test_rejects_invalid_target_before_any_navigation(self):
        for url in ["https://example.com/dp/B0DEMO0001", "http://amazon.in/dp/B0DEMO0001",
                    "https://amazon.in.evil.test/dp/B0DEMO0001", "https://amazon.in/"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.tool().load(url)
        self.assertEqual(self.page.loads, [])

    def test_bounded_navigation_timeouts_and_backoff(self):
        self.page.goto_actions = [{"error": FakeTimeout("slow")}] * 3
        with self.assertRaises(FakeTimeout):
            self.tool(retries=2, timeout_ms=1500, delay=0.25).load(FIRST)
        self.assertEqual(len(self.page.loads), 3)
        self.assertEqual(self.sleeps, [0.25, 1, 0.25, 2, 0.25])
        self.assertTrue(all(call[1]["timeout"] == 1500 for call in self.page.loads))

    def test_timeout_can_recover_without_retrying_a_click(self):
        self.page.goto_actions = [{"error": FakeTimeout("slow")}, {}]
        snapshot = self.tool(retries=1).load(FIRST)
        self.assertEqual(snapshot.url, FIRST)
        self.assertEqual(len(self.page.loads), 2)

    def test_unreadable_timeout_page_preserves_original_navigation_error(self):
        self.page.goto_actions = [{"error": FakeTimeout("original navigation timeout")}]
        with patch.object(self.page, "content", side_effect=RuntimeError("document is changing")):
            with self.assertRaisesRegex(FakeTimeout, "original navigation timeout"):
                self.tool(retries=0).load(FIRST)
        self.assertEqual(len(self.page.loads), 1)

    def test_captcha_on_timeout_stops_navigation_retries(self):
        self.page.goto_actions = [{"error": FakeTimeout("slow"),
                                   "html": '<input id="captchacharacters">',
                                   "url": "https://www.amazon.in/errors/validateCaptcha"}]
        snapshot = self.tool(retries=3).load(FIRST)
        self.assertIn("captchacharacters", snapshot.html)
        self.assertFalse(snapshot.has_next)
        self.assertEqual(len(self.page.loads), 1)

    def test_http_errors_stop_without_retry_but_access_blocks_stay_visible(self):
        for status in [403, 429, 500, 503]:
            with self.subTest(status=status):
                self.page = FakePage()
                self.page.goto_actions = [{"status": status}]
                tool = self.tool(retries=3)
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                    tool.load(FIRST)
                self.assertEqual(len(self.page.loads), 1)
                self.assertIsNone(tool.advance())
        self.page = FakePage()
        self.page.goto_actions = [{"status": 503, "html": '<input id="captchacharacters">'}]
        self.assertIn("captchacharacters", self.tool().load(FIRST).html)

    def test_direct_link_pagination_loads_without_clicking(self):
        control = FakeControl(f"/product-reviews/{ASIN}?pageNumber=2")
        self.page.controls[NEXT_SELECTORS[0]] = [control]
        tool = self.tool()
        snapshot = tool.load(FIRST)
        self.assertEqual(snapshot.next_url, SECOND)
        self.page.goto_actions = [{"html": review_html("Second fictional review"),
                                   "texts": ["Second fictional review"], "controls": {}}]
        advanced = tool.advance()
        self.assertEqual(advanced.url, SECOND)
        self.assertFalse(advanced.has_next)
        self.assertEqual([call[0] for call in self.page.loads], [FIRST, SECOND])
        self.assertEqual(control.clicks, [])

    def test_ajax_click_waits_for_actual_changed_reviews_once(self):
        control = FakeControl("#more")
        self.page.controls[NEXT_SELECTORS[-1]] = [control]
        tool = self.tool(timeout_ms=1200)
        self.assertIsNone(tool.load(FIRST).next_url)

        def finish_update():
            self.page.html = review_html("Second fictional review")
            self.page.texts = ["Second fictional review"]
            self.page.controls = {}

        self.page.change_callback = finish_update
        snapshot = tool.advance()
        self.assertIn("Second fictional review", snapshot.html)
        self.assertEqual(len(control.clicks), 1)
        self.assertEqual(len(self.page.loads), 1)
        self.assertEqual(self.page.waits[-2][2], ["First fictional review"])
        self.assertIsNone(tool.advance())

    def test_ajax_url_change_without_review_change_times_out_without_second_click(self):
        control = FakeControl(on_click=lambda: setattr(self.page, "url", SECOND))
        self.page.controls[NEXT_SELECTORS[-1]] = [control]
        tool = self.tool()
        tool.load(FIRST)
        with self.assertRaises(FakeTimeout):
            tool.advance()
        self.assertEqual(len(control.clicks), 1)
        self.assertIsNone(tool.advance())
        change_script = next(script for script, _, arg in self.page.waits if arg is not None)
        self.assertNotIn("location.href", change_script)

    def test_same_document_absolute_fragment_uses_ajax_click(self):
        control = FakeControl(FIRST + "#more")
        self.page.controls[NEXT_SELECTORS[2]] = [control]
        tool = self.tool()
        snapshot = tool.load(FIRST)
        self.assertTrue(snapshot.has_next)
        self.assertIsNone(snapshot.next_url)

    def test_click_timeout_does_not_retry_a_consumed_control(self):
        def fail_click():
            raise FakeTimeout("click timed out")

        control = FakeControl(on_click=fail_click)
        self.page.controls[NEXT_SELECTORS[-1]] = [control]
        tool = self.tool()
        tool.load(FIRST)
        with self.assertRaisesRegex(FakeTimeout, "click timed out"):
            tool.advance()
        self.assertEqual(len(control.clicks), 1)
        self.assertIsNone(tool.advance())

    def test_ajax_stops_on_captcha_after_one_click(self):
        def block():
            self.page.html = '<input id="captchacharacters">'
        control = FakeControl("javascript:void(0)", on_click=block)
        self.page.controls[NEXT_SELECTORS[2]] = [control]
        tool = self.tool()
        tool.load(FIRST)
        snapshot = tool.advance()
        self.assertIn("captchacharacters", snapshot.html)
        self.assertEqual(len(control.clicks), 1)
        self.assertIsNone(tool.advance())

    def test_next_control_must_refer_to_same_product(self):
        for href in [OTHER, "https://example.com/", "javascript:alert(1)"]:
            with self.subTest(href=href):
                self.page = FakePage()
                control = FakeControl(href)
                self.page.controls[NEXT_SELECTORS[0]] = [control]
                with self.assertRaisesRegex(ValueError, "outside the current"):
                    self.tool().load(FIRST)
                self.assertEqual(control.clicks, [])
                self.assertEqual(len(self.page.loads), 1)

    def test_disabled_and_hidden_controls_are_skipped(self):
        controls = [FakeControl(SECOND, visible=False), FakeControl(SECOND, enabled=False),
                    FakeControl(SECOND, disabled=True)]
        self.page.controls[NEXT_SELECTORS[0]] = controls
        snapshot = self.tool().load(FIRST)
        self.assertFalse(snapshot.has_next)
        self.assertTrue(all(control.clicks == [] for control in controls))

    def test_disabled_control_does_not_hide_valid_later_selector(self):
        self.page.controls[NEXT_SELECTORS[0]] = [FakeControl(SECOND, disabled=True)]
        self.page.controls[NEXT_SELECTORS[2]] = [FakeControl(SECOND)]
        self.assertEqual(self.tool().load(FIRST).next_url, SECOND)

    def test_redirect_and_changed_location_cannot_advance_other_products(self):
        for url in [OTHER, "https://example.com/", "https://www.amazon.in/unrecognized"]:
            with self.subTest(url=url):
                self.page = FakePage()
                self.page.goto_actions = [{"url": url}]
                with self.assertRaises(ValueError):
                    self.tool().load(FIRST)
        self.page = FakePage()
        control = FakeControl()
        self.page.controls[NEXT_SELECTORS[-1]] = [control]
        tool = self.tool()
        tool.load(FIRST)
        self.page.url = OTHER
        with self.assertRaises(ValueError):
            tool.advance()
        self.assertEqual(control.clicks, [])

    def test_content_wait_timeout_preserves_unknown_instead_of_zero_reviews(self):
        self.page.html = "<h1>Unrecognized content</h1>"
        self.page.ready_timeout = True
        snapshot = self.tool().load(FIRST)
        self.assertEqual(snapshot.html, self.page.html)
        self.assertFalse(snapshot.has_next)
        self.assertEqual(len(self.page.loads), 1)


if __name__ == "__main__":
    unittest.main()
