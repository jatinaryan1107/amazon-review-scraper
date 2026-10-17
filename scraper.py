"""Live browser entry point. This file is not executed by offline validation.

Run offline.py to explore the project without installing a browser or accessing
Amazon. Browser imports are deliberately confined to the live commands below.
"""

import argparse
from pathlib import Path
import sys
from threading import Event

from collection_runtime import run_auth, run_collection
from collector import load_input_urls
from review_store import ReviewStore


def authenticate(auth_file: Path):
    finished = Event()

    def confirm(event):
        if event.get("status") == "awaiting_login":
            input("Sign in manually in the browser. Once signed in, press Enter here to save the session: ")
            finished.set()

    report = run_auth(auth_file, finished, None, on_progress=confirm)
    if report["status"] != "completed":
        raise ValueError(report["detail"])
    print("Browser session saved locally. This file contains private session data.")


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
    parser.add_argument("--transport", choices=("browser", "hybrid"), default="browser",
                        help="Browser rendering or experimental same-session HTML first")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum seconds between request starts (0 to 60)")
    parser.add_argument("--full-assets", action="store_true", help="Load images, fonts and media during collection")
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
        if args.output is not None and args.output.resolve() in {args.auth_file.resolve(), args.input.resolve()}:
            raise ValueError("CSV output cannot replace the session or input file.")
        if args.db.resolve() in {args.auth_file.resolve(), args.input.resolve()}:
            raise ValueError("Database cannot replace the session or input file.")

        def report_product(event):
            if event.get("event") == "product_finished":
                print(f"{event['asin']}: {event['status']}; new reviews {event['inserted']}; processed snapshots {event['pages_processed']}")
                print("  " + event["detail"])

        report = run_collection(
            urls, args.db, args.auth_file, max_pages=args.pages,
            resume=args.resume, restart=args.restart, replay_limit=args.replay_limit,
            transport=args.transport, delay=args.delay, headful=args.headful,
            lightweight=not args.full_assets, on_progress=report_product,
        )
        if report.get("detail"):
            print(report["detail"])
        if args.output is not None and args.db.is_file():
            with ReviewStore(args.db, read_only=True) as store:
                print(f"Exported {store.export_csv(args.output)} reviews to {args.output}")
        return 1 if report["status"] in {"failed", "blocked", "partial"} else 0
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
