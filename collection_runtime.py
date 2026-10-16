"""Shared live collection and manual session setup for the CLI and dashboard.

Nothing starts on import. A calling worker owns every Playwright object for its
whole lifetime. Tests inject a fake factory and never launch a real browser.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import time

from browser_tool import HTTPFirstBrowser, PlaywrightBrowser, _amazon_location
from collector import collect_product
from review_parser import detect_page_state, extract_asin
from review_store import ReviewStore


class CollectionCancelled(Exception):
    """Cooperative cancellation between bounded browser operations."""


def _is_set(signal):
    if signal is None:
        return False
    return bool(signal() if callable(signal) else signal.is_set())


def _notify(callback, event):
    if callback is not None:
        try:
            callback(dict(event))
        except Exception:
            # A UI observer cannot roll back a committed collection page.
            pass


def session_status(path):
    """Describe local storage-state readiness without exposing session contents."""
    auth_file = Path(path)
    if not auth_file.is_file():
        return {"exists": False, "valid": False, "verified": False,
                "detail": "Connect an Amazon.in session before live collection."}
    try:
        if auth_file.stat().st_size > 5 * 1024 * 1024:
            raise ValueError
        state = json.loads(auth_file.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or not isinstance(state.get("cookies"), list)
                or not isinstance(state.get("origins"), list)):
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        return {"exists": True, "valid": False, "verified": False,
                "detail": "The saved session could not be read. Connect again."}
    return {"exists": True, "valid": True, "verified": False,
            "detail": "Saved session available. Amazon checks it when collection starts."}


def _playwright_factory():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def _save_session(context, auth_file):
    auth_file = Path(auth_file)
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=auth_file.parent, prefix=".auth-",
                                         suffix=".json", delete=False) as handle:
            temporary = Path(handle.name)
        context.storage_state(path=str(temporary))
        # Check the browser produced a complete state before replacing credentials.
        if not session_status(temporary)["valid"]:
            raise ValueError("The browser did not produce a valid saved session.")
        temporary.replace(auth_file)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class MinimumInterval:
    """Pace request starts without adding a fixed sleep after slow responses."""

    def __init__(self, delay, *, cancelled=None, metrics=None, clock=None, sleep=None):
        if (isinstance(delay, bool) or not isinstance(delay, (int, float))
                or not math.isfinite(delay) or not 0 <= delay <= 60):
            raise ValueError("Request interval must be between 0 and 60 seconds.")
        self.delay = float(delay)
        self.cancelled = cancelled
        self.metrics = metrics if metrics is not None else {}
        self.clock = clock if clock is not None else time.monotonic
        self.sleep = sleep if sleep is not None else time.sleep
        self.last_started = None

    def __call__(self):
        if _is_set(self.cancelled):
            raise CollectionCancelled
        started = self.clock()
        if self.last_started is not None:
            remaining = self.delay - (started - self.last_started)
            while remaining > 0:
                if _is_set(self.cancelled):
                    raise CollectionCancelled
                self.sleep(min(remaining, 0.1))
                remaining = self.delay - (self.clock() - self.last_started)
        waited = max(0.0, self.clock() - started)
        self.metrics["wait_seconds"] = self.metrics.get("wait_seconds", 0.0) + waited
        self.last_started = self.clock()


def _limit_assets(context, metrics):
    def route_request(route):
        if route.request.resource_type in {"image", "font", "media"}:
            metrics["assets_skipped"] += 1
            route.abort()
        else:
            route.continue_()
    context.route("**/*", route_request)


def run_collection(urls, db_path, auth_file, *, max_pages=3, resume=True,
                   restart=False, replay_limit=20, transport="browser",
                   on_progress=None, cancelled=None, delay=1.0,
                   headful=False, lightweight=True):
    """Run a bounded batch on the caller's worker thread, never an HTTP thread.

    The optional hybrid transport uses ordinary authenticated HTML requests and
    falls back only for unsupported HTML or JavaScript pagination. It never
    switches transports in response to an access block.
    """
    if isinstance(urls, (str, bytes)):
        raise ValueError("Provide a list of Amazon.in product URLs.")
    unique = []
    seen = set()
    for url in urls:
        asin = extract_asin(url)
        if asin is None:
            raise ValueError("Every URL must be an HTTPS Amazon.in product or review URL.")
        if asin not in seen:
            seen.add(asin)
            unique.append(url)
        if len(unique) > 20:
            raise ValueError("Collect at most 20 products in one batch.")
    if not unique:
        raise ValueError("Add at least one Amazon.in product URL.")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 50:
        raise ValueError("Choose between 1 and 50 new pages per product.")
    if isinstance(replay_limit, bool) or not isinstance(replay_limit, int) or not 0 <= replay_limit <= 100:
        raise ValueError("Replay limit must be between 0 and 100.")
    if transport not in {"browser", "hybrid"}:
        raise ValueError("Choose browser or hybrid collection.")
    if Path(db_path).resolve() == Path(auth_file).resolve():
        raise ValueError("The review database cannot replace the saved session.")
    metrics = {"transport": transport, "browser_pages": 0, "http_pages": 0,
               "browser_fallbacks": 0, "assets_skipped": 0, "wait_seconds": 0.0}
    pacer = MinimumInterval(delay, cancelled=cancelled, metrics=metrics)
    reports = []
    started = time.monotonic()

    def finish(status, detail="", was_cancelled=False):
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
        metrics["wait_seconds"] = round(metrics["wait_seconds"], 3)
        result = {"products": reports, "status": status,
                  "inserted": sum(item["inserted"] for item in reports),
                  "pages_processed": sum(item["pages_processed"] for item in reports),
                  "metrics": dict(metrics), "detail": detail}
        if was_cancelled:
            result["cancelled"] = True
        return result

    if _is_set(cancelled):
        return finish("paused", "Cancelled before collection started.", True)
    if not session_status(auth_file)["valid"]:
        return finish("blocked", "Connect an Amazon.in session before live collection.")
    status = "completed"
    try:
        with ReviewStore(db_path) as store, _playwright_factory() as playwright:
            browser = playwright.chromium.launch(headless=not headful)
            try:
                context = browser.new_context(
                    storage_state=str(auth_file), locale="en-IN",
                    viewport={"width": 1280, "height": 800},
                )
                if lightweight:
                    _limit_assets(context, metrics)
                page = context.new_page()
                if transport == "hybrid":
                    tool = HTTPFirstBrowser(context, page, pacer=pacer, metrics=metrics)
                else:
                    tool = PlaywrightBrowser(page, pacer=pacer, metrics=metrics)
                for url in unique:
                    if _is_set(cancelled):
                        _save_session(context, auth_file)
                        return finish("paused", "Cancelled. Saved progress is retained.", True)
                    report = collect_product(
                        tool, store, url, max_pages=max_pages, resume=resume,
                        restart=restart, replay_limit=replay_limit,
                        on_progress=on_progress, cancelled=lambda: _is_set(cancelled),
                    )
                    reports.append(report)
                    _notify(on_progress, {**report, "event": "product_finished"})
                    if report.get("cancelled"):
                        _save_session(context, auth_file)
                        return finish("paused", report["detail"], True)
                    if report["status"] == "blocked":
                        return finish("blocked", report["detail"])
                    if report["status"] == "failed":
                        status = "failed"
                    elif report["status"] == "partial" and status != "failed":
                        status = "partial"
                    elif report["status"] == "paused" and status == "completed":
                        status = "paused"
                _save_session(context, auth_file)
            finally:
                browser.close()
        return finish(status)
    except CollectionCancelled:
        return finish("paused", "Cancelled. Saved progress is retained.", True)
    except ImportError:
        return finish("failed", "Live collection needs Playwright and Chromium. Follow the setup instructions in README.md.")
    except Exception:
        # Browser diagnostics can contain URLs or local credential paths. Keep
        # the user-facing job record free of private browser internals.
        return finish("failed", "The browser runtime could not finish. Check the local browser setup and saved session.")


def run_auth(auth_file, finished, cancelled, on_progress=None):
    """Let the user sign in manually and explicitly finish or cancel setup.

    Signals may be zero-argument callables or threading.Event objects. Credentials
    never pass through the dashboard API. Saved state is checked again on use.
    """
    if _is_set(cancelled):
        return {"status": "paused", "cancelled": True, "detail": "Session setup cancelled."}
    try:
        with _playwright_factory() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context = browser.new_context(locale="en-IN")
                page = context.new_page()
                page.goto("https://www.amazon.in/", wait_until="domcontentloaded", timeout=30000)
                _notify(on_progress, {"status": "awaiting_login", "detail": "Sign in in the browser, then choose Save session in Reviewdesk."})
                deadline = time.monotonic() + 600
                while not _is_set(finished):
                    if _is_set(cancelled):
                        return {"status": "paused", "cancelled": True, "detail": "Session setup cancelled. The previous session was retained."}
                    if page.is_closed():
                        return {"status": "failed", "detail": "The sign-in browser was closed before the session was saved."}
                    if time.monotonic() >= deadline:
                        return {"status": "paused", "detail": "Session setup timed out. Start again when ready."}
                    # A Playwright wait also services browser events while the
                    # user signs in, including navigation and window closure.
                    page.wait_for_timeout(100)
                if _is_set(cancelled):
                    return {"status": "paused", "cancelled": True, "detail": "Session setup cancelled. The previous session was retained."}
                html, title = page.content(), page.title()
                if not _amazon_location(page.url):
                    return {"status": "failed", "detail": "Return to Amazon.in before saving the session."}
                state, _ = detect_page_state(html, title)
                if state in {"login", "captcha"}:
                    return {"status": "blocked", "detail": "Sign-in or a browser challenge is still visible. No session was replaced."}
                _save_session(context, auth_file)
                return {"status": "completed", "detail": "Browser session saved locally. Amazon checks it when collection starts."}
            finally:
                browser.close()
    except ImportError:
        return {"status": "failed", "detail": "Session setup needs Playwright and Chromium. Follow README.md."}
    except Exception:
        return {"status": "failed", "detail": "The sign-in browser could not finish. Check the local browser setup."}
