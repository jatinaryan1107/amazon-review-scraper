"""Live browser entry point. This file is not executed by offline validation.

Run offline.py to explore the project without installing a browser or accessing
Amazon. Browser imports are deliberately confined to the live commands below.
"""

import argparse
import json
from pathlib import Path
import sys
import tempfile

from browser_tool import PlaywrightBrowser
from collector import collect_product, load_input_urls
from review_parser import detect_page_state
from review_store import ReviewStore


def authenticate(auth_file: Path):
    from playwright.sync_api import sync_playwright

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context = browser.new_context(locale="en-IN")
                page = context.new_page()
                page.goto("https://www.amazon.in/", wait_until="domcontentloaded", timeout=30000)
                input("Sign in manually in the browser. Once signed in, press Enter here to save the session: ")
                state, _ = detect_page_state(page.content(), page.title())
                if state in {"login", "captcha"}:
                    raise ValueError("Login or CAPTCHA is still visible; no session was saved.")
                # Store state atomically. Session validity is checked again by live pages.
                with tempfile.NamedTemporaryFile(dir=auth_file.parent, prefix=".auth-", suffix=".json", delete=False) as handle:
                    temporary = Path(handle.name)
                context.storage_state(path=str(temporary))
                temporary.replace(auth_file)
                temporary = None
            finally:
                browser.close()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Browser session saved to {auth_file}. This file contains private session data.")


def positive(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser():
    parser = argparse.ArgumentParser(description="Bounded Amazon.in review collection with durable progress")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--auth", action="store_true", help="Manually sign in and save browser session")
    mode.add_argument("--scrape", action="store_true", help="Collect reviews (default mode)")
    parser.add_argument("--input", type=Path, default=Path("input_urls.csv"))
    parser.add_argument("--db", type=Path, default=Path("data/reviews.sqlite3"))
    parser.add_argument("--output", type=Path, help="Optional CSV snapshot of the full database")
    parser.add_argument("--auth-file", type=Path, default=Path("auth.json"))
    parser.add_argument("--pages", type=positive, default=3, help="New snapshots per product in this invocation")
    parser.add_argument("--replay-limit", type=positive, default=20, help="Maximum earlier show-more clicks when restoring a saved position")
    parser.add_argument("--headful", action="store_true", help="Show the collection browser")
    progress = parser.add_mutually_exclusive_group()
    progress.add_argument("--resume", action="store_true", help="Continue saved positions; skip completed products")
    progress.add_argument("--restart", action="store_true", help="Restart positions; retain stored reviews")
    parser.add_argument("--until-end", action="store_true", help="Compatibility option; --pages remains a hard budget")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.auth:
            authenticate(args.auth_file)
            return 0
        urls = load_input_urls(args.input)
        if not args.auth_file.is_file():
            raise ValueError("No saved session. Use --auth to sign in manually first.")
        # Fail early for malformed session JSON without printing private contents.
        try:
            session = json.loads(args.auth_file.read_text(encoding="utf-8"))
            if not isinstance(session, dict) or not isinstance(session.get("cookies"), list) or not isinstance(session.get("origins"), list):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ValueError("Saved session file is invalid; refresh it with --auth.") from None
        if args.output is not None and args.output.resolve() in {args.auth_file.resolve(), args.input.resolve()}:
            raise ValueError("CSV output cannot replace the session or input file.")
        if args.db.resolve() in {args.auth_file.resolve(), args.input.resolve()}:
            raise ValueError("Database cannot replace the session or input file.")

        from playwright.sync_api import sync_playwright

        failed = False
        with ReviewStore(args.db) as store, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headful)
            try:
                context = browser.new_context(storage_state=str(args.auth_file), locale="en-IN", viewport={"width": 1280, "height": 800})
                tool = PlaywrightBrowser(context.new_page())
                for url in urls:
                    report = collect_product(tool, store, url, max_pages=args.pages, resume=args.resume, restart=args.restart, replay_limit=args.replay_limit)
                    print(f"{report['asin']}: {report['status']}; new reviews {report['inserted']}; processed snapshots {report['pages_processed']}")
                    print("  " + report["detail"])
                    failed = failed or report["status"] in {"failed", "blocked", "partial"}
                    if report["status"] == "blocked":
                        print("Collection stopped for manual session review. No further products will be requested.")
                        break
            finally:
                try:
                    browser.close()
                finally:
                    if args.output is not None:
                        print(f"Exported {store.export_csv(args.output)} reviews to {args.output}")
        return 1 if failed else 0
    except KeyboardInterrupt:
        print("Interrupted. Saved database progress is retained.", file=sys.stderr)
        return 130
    except ImportError:
        print("Live collection needs Playwright. Install requirements and Chromium as documented in README.md.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
