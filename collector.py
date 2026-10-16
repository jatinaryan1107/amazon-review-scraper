"""Bounded collection controller. Tests use a fake browser, never Amazon.

The browser is a replaceable tool; this module owns durable progress and stops.
"""

from dataclasses import dataclass
import csv
from pathlib import Path
from typing import Protocol

from review_parser import detect_page_state, extract_asin, parse_reviews
from review_store import ReviewStore


class SafeNavigationError(ValueError):
    """An adapter validation failure with deliberately nonsecret diagnostics."""

    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.diagnostic = dict(diagnostic)


class PageHTTPError(RuntimeError):
    """A known page response failure containing only a validated HTTP status."""

    def __init__(self, status):
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("HTTP status must be a valid three-digit integer.")
        self.http_status = status
        super().__init__(f"Amazon returned HTTP {status}; collection stopped.")


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    html: str
    title: str = ""
    has_next: bool = False
    next_url: str | None = None


class Browser(Protocol):
    def load(self, url: str) -> PageSnapshot: ...

    def advance(self) -> PageSnapshot | None: ...


def load_input_urls(path: str | Path) -> list[str]:
    """Validate all input before navigation; ignore repeated product identities."""
    urls = []
    seen = set()
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for line, row in enumerate(csv.reader(handle), start=1):
            if not row or not any(value.strip() for value in row):
                continue
            if len(row) != 1:
                raise ValueError(f"Input row {line} must contain exactly one URL.")
            url = row[0].strip()
            if not urls and url.casefold() == "url":
                continue
            asin = extract_asin(url)
            if asin is None:
                raise ValueError(f"Input row {line} is not an HTTPS Amazon.in product/review URL.")
            if asin not in seen:
                seen.add(asin)
                urls.append(url)
    if not urls:
        raise ValueError("Input contains no product URLs.")
    return urls


def collect_product(
    browser: Browser,
    store: ReviewStore,
    url: str,
    *,
    max_pages: int = 3,
    resume: bool = False,
    restart: bool = False,
    replay_limit: int = 20,
    on_progress=None,
    cancelled=None,
) -> dict:
    """Collect at most max_pages new snapshots; commit rows with their cursor.

    A direct pagination link is a durable cursor. In-place show-more pages need
    their earlier clicks replayed after a restart; these are bounded separately.
    Duplicate review rows are harmless because the database enforces uniqueness.
    """
    if max_pages < 1 or replay_limit < 0:
        raise ValueError("Page budget must be positive; replay limit cannot be negative.")
    asin = extract_asin(url)
    if asin is None:
        raise ValueError("Expected an HTTPS Amazon.in product or review URL with an ASIN.")
    product_url = f"https://www.amazon.in/dp/{asin}"
    first_url = f"https://www.amazon.in/product-reviews/{asin}?reviewerType=all_reviews"
    checkpoint = store.get_checkpoint(asin) if resume and not restart else None
    if checkpoint and checkpoint["status"] in {"complete", "zero_reviews"}:
        return {
            "asin": asin, "status": checkpoint["status"], "skipped": True,
            "inserted": 0, "pages_processed": 0, "detail": checkpoint["detail"],
        }

    cursor_url = (checkpoint or {}).get("next_url") or first_url
    cursor_steps = (checkpoint or {}).get("replay_steps", 0)
    pages_completed = (checkpoint or {}).get("pages_completed", 0)
    inserted = 0
    processed = 0
    fingerprints = set()
    diagnostic = None
    http_status = None
    durable_position = (cursor_url, cursor_steps, pages_completed)

    def restore_committed_position():
        nonlocal cursor_url, cursor_steps, pages_completed, durable_position
        persisted = store.get_checkpoint(asin)
        if persisted is not None:
            durable_position = (persisted["next_url"], persisted["replay_steps"], persisted["pages_completed"])
        cursor_url, cursor_steps, pages_completed = durable_position

    def save(status: str, detail: str, rows=(), *, page=False) -> dict:
        nonlocal durable_position, cursor_url, cursor_steps, pages_completed, inserted, processed
        try:
            counts = store.save_page(
                asin, rows, next_url=cursor_url, pages_completed=pages_completed,
                status=status, detail=detail, replay_steps=cursor_steps,
            )
        except BaseException:
            # Never skip an unsaved page when parsing or a DB transaction fails.
            restore_committed_position()
            raise
        durable_position = (cursor_url, cursor_steps, pages_completed)
        if page:
            inserted += counts["inserted"]
            processed += 1
        if on_progress is not None:
            # Reporting observes committed state and cannot invalidate a saved page.
            try:
                event = {"asin": asin, "status": status, "detail": detail,
                         "inserted": inserted, "pages_processed": processed,
                         "pages_completed": pages_completed}
                if diagnostic is not None:
                    event["diagnostic"] = dict(diagnostic)
                if http_status is not None:
                    event["http_status"] = http_status
                on_progress(event)
            except Exception:
                pass
        return counts

    def result(status: str, detail: str) -> dict:
        report = {
            "asin": asin, "status": status, "skipped": False,
            "inserted": inserted, "pages_processed": processed, "detail": detail,
        }
        if diagnostic is not None:
            report["diagnostic"] = dict(diagnostic)
        if http_status is not None:
            report["http_status"] = http_status
        return report

    def cancellation():
        if cancelled is None or not cancelled():
            return None
        detail = "Cancelled. Committed reviews and the saved cursor are retained."
        save("paused", detail)
        return {**result("paused", detail), "cancelled": True}

    # Validate persisted cursors before any browser action.
    if extract_asin(cursor_url) != asin:
        detail = "Saved cursor is invalid for this product. Restart collection explicitly."
        cursor_url, cursor_steps = first_url, 0
        save("failed", detail)
        return result("failed", detail)
    if cursor_steps > replay_limit:
        detail = f"Saved position needs {cursor_steps} replay steps; limit is {replay_limit}."
        save("paused", detail)
        return result("paused", detail)

    try:
        stopped = cancellation()
        if stopped:
            return stopped
        save("in_progress", "Opening saved collection position.")
        stopped = cancellation()
        if stopped:
            return stopped
        snapshot = browser.load(cursor_url)
        for _ in range(cursor_steps):
            stopped = cancellation()
            if stopped:
                return stopped
            state, detail = detect_page_state(snapshot.html, snapshot.title)
            if state in {"login", "captcha"}:
                save("blocked", detail)
                return result("blocked", detail)
            if extract_asin(snapshot.url) != asin:
                detail = "Browser reached an unexpected product while restoring saved position."
                save("failed", detail)
                return result("failed", detail)
            if state != "success":
                detail = "Cannot restore saved show-more position: " + detail
                save("failed", detail)
                return result("failed", detail)
            restored_rows = parse_reviews(snapshot.html, asin, product_url)
            if not restored_rows:
                detail = "Cannot restore saved position because no review identities were extracted."
                save("failed", detail)
                return result("failed", detail)
            restored_fingerprint = tuple(sorted({str(row["review_id"]) for row in restored_rows}))
            if restored_fingerprint in fingerprints:
                detail = "Restoring saved position repeated the same review set; saved progress was not advanced."
                save("partial", detail)
                return result("partial", detail)
            fingerprints.add(restored_fingerprint)
            snapshot = browser.advance()
            if snapshot is None:
                detail = "Saved show-more position no longer exists. Restart to inspect changed content."
                save("partial", detail)
                return result("partial", detail)

        while processed < max_pages:
            stopped = cancellation()
            if stopped:
                return stopped
            state, detail = detect_page_state(snapshot.html, snapshot.title)
            if state in {"login", "captcha"}:
                save("blocked", detail)
                return result("blocked", detail)
            if extract_asin(snapshot.url) != asin:
                detail = "Browser reached an unexpected product or URL; stopped."
                save("failed", detail)
                return result("failed", detail)
            if state == "zero_reviews":
                # An empty later page does not erase previously collected evidence.
                status = "zero_reviews" if pages_completed == 0 else "complete"
                cursor_url, cursor_steps = None, 0
                save(status, detail)
                return result(status, detail)
            if state != "success":
                detail = "No recognized review layout; collection failed, not zero reviews."
                save("failed", detail)
                return result("failed", detail)
            rows = parse_reviews(snapshot.html, asin, product_url)
            if not rows:
                detail = "Review elements were detected but no rows could be extracted."
                save("failed", detail)
                return result("failed", detail)
            fingerprint = tuple(sorted({str(row["review_id"]) for row in rows}))
            if fingerprint in fingerprints:
                detail = "Pagination repeated the same review set; stopped with partial coverage."
                save("partial", detail)
                return result("partial", detail)
            fingerprints.add(fingerprint)

            # Decide the next durable position before committing this page.
            if snapshot.has_next:
                if snapshot.next_url is not None:
                    if extract_asin(snapshot.next_url) != asin:
                        detail = "Pagination points outside the current Amazon.in product."
                        save("failed", detail, rows, page=True)
                        return result("failed", detail)
                    cursor_url, cursor_steps = snapshot.next_url, 0
                else:
                    # Keep the navigable base and replay N in-place advances.
                    cursor_steps += 1
                status = "paused" if processed + 1 == max_pages else "in_progress"
                detail = "Page budget reached; resume continues at saved position." if status == "paused" else "Page saved."
                if cursor_steps > replay_limit:
                    status = "paused"
                    detail = f"Next position needs {cursor_steps} replay steps. Resume with --replay-limit {cursor_steps} or restart."
            else:
                cursor_url, cursor_steps = None, 0
                status = "complete"
                detail = "All visible reviews saved; no supported next control found. Coverage is not guaranteed."
            pages_completed += 1
            save(status, detail, rows, page=True)
            # A terminal page remains complete even if cancellation arrived as it
            # committed. Otherwise the next durable cursor is safe to resume.
            if status != "complete":
                stopped = cancellation()
                if stopped:
                    return stopped
            if status in {"paused", "complete"}:
                return result(status, detail)
            snapshot = browser.advance()
            if snapshot is None:
                detail = "Next control disappeared after this page was saved."
                save("partial", detail)
                return result("partial", detail)
    except KeyboardInterrupt:
        restore_committed_position()
        save("paused", "Interrupted. Committed reviews and the saved cursor are retained.")
        raise
    except Exception as exc:
        restore_committed_position()
        stopped = cancellation()
        if stopped:
            return stopped
        candidate_status = getattr(exc, "http_status", None)
        valid_status = type(candidate_status) is int and 100 <= candidate_status <= 599
        http_status = (candidate_status if valid_status and (isinstance(exc, PageHTTPError)
                       or candidate_status in {403, 429}) else None)
        status = "blocked" if http_status in {403, 429} else "failed"
        # Browser request diagnostics can include authenticated request headers.
        # Only public failure information belongs in checkpoints or UI events.
        if isinstance(exc, SafeNavigationError):
            detail = str(exc)
            diagnostic = dict(exc.diagnostic)
        elif isinstance(exc, PageHTTPError) and http_status is not None:
            detail = f"Amazon returned HTTP {http_status}; collection stopped. The saved cursor is retained."
        else:
            detail = (f"Amazon returned HTTP {http_status}; collection stopped for access review."
                      if status == "blocked" else
                      f"{type(exc).__name__}: Collection could not save the next page. "
                      "The saved cursor is retained. Check the browser setup or retry later.")
        save(status, detail)
        return result(status, detail)

    raise RuntimeError("Unexpected collection exit")
