"""Browser navigation used by the live command, with no work performed on import.

Tests supply a fake page and timeout exception. The Playwright dependency is only
loaded when a live adapter is constructed without an injected exception class.
"""

import math
import time
from html.parser import HTMLParser
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


class AccessBlockedError(RuntimeError):
    """An HTTP access block stops the batch without another transport attempt."""

    def __init__(self, status):
        self.http_status = status
        super().__init__(f"Amazon returned HTTP {status}; collection stopped.")


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
                 timeout_error=None, sleep=None, pacer=None, metrics=None):
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
        self._pacer = pacer
        self.metrics = metrics if metrics is not None else {}
        self._next = None
        self._next_url = None
        self._asin = None
        self._blocked_status = None
        if hasattr(page, "on"):
            page.on("response", self._observe_response)

    def _observe_response(self, response):
        """Notice access blocks on review requests without inspecting headers."""
        try:
            if (response.status in {403, 429} and _amazon_location(response.url)
                    and response.request.resource_type in {"document", "xhr", "fetch"}):
                self._blocked_status = response.status
        except Exception:
            # Closed pages and incomplete response objects cannot produce usable
            # status evidence. The bounded navigation checks remain in place.
            pass

    def _raise_access_block(self):
        if self._blocked_status is not None:
            self._next, self._next_url = None, None
            raise AccessBlockedError(self._blocked_status)

    def _wait_for_review_change(self, before):
        remaining = self.timeout_ms
        while remaining > 0:
            self._raise_access_block()
            interval = min(remaining, 250)
            try:
                self.page.wait_for_function(_REVIEW_CHANGE, arg=before, timeout=interval)
                self._raise_access_block()
                return
            except self._timeout_error:
                self._raise_access_block()
                remaining -= interval
                if remaining <= 0:
                    raise

    def _pause(self):
        if self._pacer is not None:
            self._pacer()
        else:
            self._sleep(self.delay)

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
        remaining = min(self.timeout_ms, 10000)
        while remaining > 0:
            self._raise_access_block()
            interval = min(remaining, 250)
            try:
                self.page.wait_for_function(_CONTENT_READY, timeout=interval)
                self._raise_access_block()
                return
            except self._timeout_error:
                self._raise_access_block()
                remaining -= interval
        # An unknown layout remains unknown; the controller records a failure.

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
        self._blocked_status = None
        for attempt in range(self.retries + 1):
            self._pause()
            try:
                response = self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                self._raise_access_block()
                if response is not None and response.status in {403, 429}:
                    self._next, self._next_url = None, None
                    raise AccessBlockedError(response.status)
                self._wait_for_content()
                self._raise_access_block()
                snapshot = self._snapshot()
                self.metrics["browser_pages"] = self.metrics.get("browser_pages", 0) + 1
                if detect_page_state(snapshot.html, snapshot.title)[0] in {"login", "captcha"}:
                    return snapshot
                if response is not None and response.status >= 400:
                    self._next, self._next_url = None, None
                    if response.status in {403, 429}:
                        raise AccessBlockedError(response.status)
                    raise RuntimeError(f"Amazon returned HTTP {response.status}; collection stopped.")
                return snapshot
            except self._timeout_error:
                self._raise_access_block()
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
        self._raise_access_block()
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
        self._pause()
        try:
            control.click(timeout=self.timeout_ms)
            self._raise_access_block()
            self._wait_for_review_change(before)
        except self._timeout_error:
            self._raise_access_block()
            block = self._access_block()
            if block is not None:
                return block
            raise
        self._wait_for_content()
        self._raise_access_block()
        self.metrics["browser_pages"] = self.metrics.get("browser_pages", 0) + 1
        return self._snapshot()


class _HTMLPagination(HTMLParser):
    """Read only supported pagination anchors from server-rendered HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.controls = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        ancestors = self.stack + [(tag, attrs)]
        disabled = any(
            "disabled" in values or "hidden" in values
            or values.get("aria-disabled") == "true"
            or values.get("aria-hidden") == "true"
            or "a-disabled" in values.get("class", "").split()
            or "display:none" in values.get("style", "").replace(" ", "").lower()
            for _, values in ancestors
        )
        hook = attrs.get("data-hook")
        last_link = tag == "a" and any(
            name == "li" and "a-last" in values.get("class", "").split()
            for name, values in self.stack
        )
        supported = last_link or (
            tag in {"a", "button"}
            and hook in {"pagination-bar-next", "show-more-button"}
        )
        if supported and not disabled:
            self.controls.append(attrs.get("href") if tag == "a" else None)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class HTTPFirstBrowser:
    """Experimental same-session HTML transport with an explicit browser fallback.

    Only ordinary Amazon.in review URLs are requested. Access blocks never cause
    a transport switch. JavaScript pagination still belongs to the browser.
    """

    MAX_HTML_BYTES = 5 * 1024 * 1024

    def __init__(self, context, page, *, pacer=None, metrics=None,
                 timeout_ms=30000, timeout_error=None, sleep=None):
        self.request = context.request
        self.metrics = metrics if metrics is not None else {}
        self.pacer = pacer if pacer is not None else lambda: None
        self.timeout_ms = timeout_ms
        self.browser = PlaywrightBrowser(
            page, pacer=self.pacer, metrics=self.metrics,
            timeout_ms=timeout_ms, timeout_error=timeout_error, sleep=sleep,
        )
        self._browser_mode = False
        self._next_url = None

    def _fallback(self, url):
        self._browser_mode = True
        self.metrics["browser_fallbacks"] = self.metrics.get("browser_fallbacks", 0) + 1
        return self.browser.load(url)

    def load(self, url):
        asin = extract_asin(url)
        if asin is None:
            raise ValueError("Refusing to request an invalid Amazon.in review URL.")
        self._browser_mode = False
        self._next_url = None
        current = url
        visited = set()
        for redirect in range(4):
            if current in visited:
                raise ValueError("Review request repeated a redirect; stopped.")
            visited.add(current)
            self.pacer()
            response = self.request.get(
                current, timeout=self.timeout_ms, max_redirects=0, max_retries=0,
            )
            try:
                if not _amazon_location(response.url):
                    raise ValueError("Review response left the allowed Amazon.in origin.")
                status = response.status
                headers = {key.lower(): value for key, value in response.headers.items()}
                if status in {403, 429}:
                    raise AccessBlockedError(status)
                if status in {301, 302, 303, 307, 308}:
                    location = headers.get("location")
                    target = urljoin(current, location or "")
                    if not location or not _amazon_location(target):
                        raise ValueError("Review redirect left the allowed Amazon.in origin.")
                    if urlsplit(target).path.startswith("/ap/signin"):
                        return PageSnapshot(target, '<input id="ap_email">', "Amazon Sign-In")
                    if extract_asin(target) != asin:
                        raise ValueError("Review redirect points outside the requested product.")
                    if redirect == 3:
                        raise ValueError("Review request exceeded the redirect limit.")
                    current = target
                    continue
                if status >= 400 or status < 200 or status >= 300:
                    raise RuntimeError(f"Amazon returned HTTP {status}; collection stopped.")
                if extract_asin(response.url) != asin:
                    raise ValueError("Review response refers to a different product.")
                content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise ValueError("Review response was not an HTML document.")
                declared_size = headers.get("content-length", "")
                if declared_size.isdecimal() and int(declared_size) > self.MAX_HTML_BYTES:
                    raise ValueError("Review response was larger than the supported size.")
                html = response.text()
                if len(html.encode("utf-8")) > self.MAX_HTML_BYTES:
                    raise ValueError("Review response was larger than the supported size.")
                self.metrics["http_pages"] = self.metrics.get("http_pages", 0) + 1
                response_url = response.url
            finally:
                response.dispose()

            state, _ = detect_page_state(html)
            if state in {"login", "captcha", "zero_reviews"}:
                return PageSnapshot(response_url, html)
            if state != "success":
                return self._fallback(response_url)
            pagination = _HTMLPagination()
            pagination.feed(html)
            pagination.close()
            if pagination.controls:
                href = pagination.controls[0]
                if not href or href.strip().startswith("#") or href.strip().lower().startswith("javascript:"):
                    return self._fallback(response_url)
                target = urljoin(response_url, href.strip())
                if extract_asin(target) != asin:
                    raise ValueError("Next control points outside the current Amazon.in product.")
                if _document_url(target) == _document_url(response_url):
                    return self._fallback(response_url)
                self._next_url = target
            else:
                # A server-rendered review block does not prove JavaScript will
                # not add another page. Let the browser confirm the boundary.
                return self._fallback(response_url)
            return PageSnapshot(response_url, html, has_next=self._next_url is not None,
                                next_url=self._next_url)
        raise RuntimeError("Review redirect handling ended unexpectedly.")

    def advance(self):
        if self._browser_mode:
            return self.browser.advance()
        target, self._next_url = self._next_url, None
        return self.load(target) if target is not None else None
