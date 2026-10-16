"""Browser navigation used by the live command, with no work performed on import.

Tests supply a fake page and timeout exception. The Playwright dependency is only
loaded when a live adapter is constructed without an injected exception class.
"""

import math
import time
from urllib.parse import urljoin, urlsplit

from collector import PageSnapshot
from review_parser import detect_page_state, extract_asin


NEXT_SELECTORS = (
    'li.a-last:not(.a-disabled) a',
    'a[data-hook="pagination-bar-next"]',
    'a[data-hook="show-more-button"]',
    'button[data-hook="show-more-button"]',
)

_CONTENT_READY = """() => document.querySelector(
    '[data-hook="review"], input#ap_email, input#captchacharacters, [data-hook="no-reviews-section"]')
    || /no customer reviews|robot check|captcha|sign.in/i.test(
        document.title + ' ' + (document.body?.innerText || ''))"""

_REVIEW_CHANGE = """before => {
    const reviews = Array.from(document.querySelectorAll('[data-hook="review"]'), node => node.textContent);
    const blocked = document.querySelector('input#ap_email, input#captchacharacters')
        || /robot check|captcha|sign.in/i.test(document.title);
    const empty = document.querySelector('[data-hook="no-reviews-section"]')
        || /no customer reviews/i.test(document.body?.innerText || '');
    return Boolean(blocked || (reviews.length === 0 && empty) || (reviews.length > 0
        && JSON.stringify(reviews) !== JSON.stringify(before)));
}"""

_DISABLED_CONTROL = """node => Boolean(
    node.closest('[aria-disabled="true"], .a-disabled, [disabled]'))"""


def _amazon_location(url):
    """Allow Amazon sign-in paths as well as products, but reject other origins."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc.lower() in {"amazon.in", "www.amazon.in"}


def _document_url(url):
    return urlsplit(url)._replace(fragment="").geturl()


class PlaywrightBrowser:
    """Navigate with bounded timeout retries and consume each pagination click once.

    ``timeout_error`` and ``sleep`` are optional test seams. Normal callers only
    need to provide a Playwright page. No method opens a browser by itself.
    """

    def __init__(self, page, *, retries=2, timeout_ms=30000, delay=2.0,
                 timeout_error=None, sleep=None):
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a nonnegative integer")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms must be a positive integer")
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
            raise ValueError("delay must be a finite nonnegative number")
        if timeout_error is None:
            from playwright.sync_api import TimeoutError as timeout_error
        if not isinstance(timeout_error, type) or not issubclass(timeout_error, Exception):
            raise TypeError("timeout_error must be an exception class")
        self.page = page
        self.retries = retries
        self.timeout_ms = timeout_ms
        self.delay = delay
        self._timeout_error = timeout_error
        self._sleep = sleep if sleep is not None else time.sleep
        self._next = None
        self._next_url = None
        self._asin = None

    def _validate_location(self, state):
        current_asin = extract_asin(self.page.url)
        if not _amazon_location(self.page.url):
            raise ValueError("Browser left the allowed HTTPS Amazon.in origin.")
        if current_asin is not None and current_asin != self._asin:
            raise ValueError("Browser navigated to a different Amazon.in product.")
        if current_asin is None and state not in {"login", "captcha"}:
            raise ValueError("Browser did not reach the requested Amazon.in product.")

    def _snapshot(self):
        self._next, self._next_url = None, None
        html = self.page.content()
        title = self.page.title()
        state, _ = detect_page_state(html, title)
        self._validate_location(state)
        if state == "success":
            for selector in NEXT_SELECTORS:
                candidates = self.page.locator(selector)
                for index in range(candidates.count()):
                    candidate = candidates.nth(index)
                    if not candidate.is_visible() or not candidate.is_enabled():
                        continue
                    if candidate.evaluate(_DISABLED_CONTROL):
                        continue
                    href = candidate.get_attribute("href")
                    next_url = self._pagination_url(href)
                    self._next, self._next_url = candidate, next_url
                    break
                if self._next is not None:
                    break
        return PageSnapshot(self.page.url, html, title, self._next is not None, self._next_url)

    def _pagination_url(self, href):
        if not href:
            return None
        href = href.strip()
        if href.startswith("#") or href.lower() in {
            "javascript:", "javascript:;", "javascript:void(0)", "javascript:void(0);",
        }:
            return None
        next_url = urljoin(self.page.url, href)
        if extract_asin(next_url) != self._asin:
            raise ValueError("Next control points outside the current Amazon.in product.")
        return None if _document_url(next_url) == _document_url(self.page.url) else next_url

    def _wait_for_content(self):
        try:
            self.page.wait_for_function(_CONTENT_READY, timeout=min(self.timeout_ms, 10000))
        except self._timeout_error:
            # An unknown layout remains unknown; the controller records a failure.
            pass

    def _access_block(self):
        try:
            html, title = self.page.content(), self.page.title()
        except Exception:
            # Navigation can temporarily make the document unreadable. Preserve
            # the original timeout so retries and the final error stay accurate.
            return None
        state, _ = detect_page_state(html, title)
        if state not in {"login", "captcha"}:
            return None
        self._validate_location(state)
        self._next, self._next_url = None, None
        return PageSnapshot(self.page.url, html, title)

    def load(self, url):
        """Load one product page; retry timeouts but stop at login or CAPTCHA."""
        asin = extract_asin(url)
        if asin is None:
            raise ValueError("Refusing to navigate to an invalid product/review URL.")
        self._asin = asin
        self._next, self._next_url = None, None
        for attempt in range(self.retries + 1):
            self._sleep(self.delay)
            try:
                response = self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                self._wait_for_content()
                snapshot = self._snapshot()
                if detect_page_state(snapshot.html, snapshot.title)[0] in {"login", "captcha"}:
                    return snapshot
                if response is not None and response.status >= 400:
                    self._next, self._next_url = None, None
                    raise RuntimeError(f"Amazon returned HTTP {response.status}; collection stopped.")
                return snapshot
            except self._timeout_error:
                block = self._access_block()
                if block is not None:
                    return block
                if attempt == self.retries:
                    raise
                self._sleep(2 ** attempt)
        raise RuntimeError("Navigation attempts exhausted")

    def advance(self):
        """Follow a validated direct link or click one in-place control once."""
        if self._next is None:
            return None
        state, _ = detect_page_state(self.page.content(), self.page.title())
        self._validate_location(state)
        if state in {"login", "captcha"}:
            return self._access_block()
        control, next_url = self._next, self._next_url
        # A failed click cannot be repeated accidentally using this stale control.
        self._next, self._next_url = None, None
        if next_url is not None:
            if extract_asin(next_url) != self._asin:
                raise ValueError("Next link refers to a different product.")
            return self.load(next_url)
        before = self.page.locator('[data-hook="review"]').all_text_contents()
        self._sleep(self.delay)
        try:
            control.click(timeout=self.timeout_ms)
            self.page.wait_for_function(_REVIEW_CHANGE, arg=before, timeout=self.timeout_ms)
        except self._timeout_error:
            block = self._access_block()
            if block is not None:
                return block
            raise
        self._wait_for_content()
        return self._snapshot()
