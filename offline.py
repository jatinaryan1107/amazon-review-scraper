"""Network-free commands for importing, inspecting, and exporting review data."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import csv
from pathlib import Path
import sqlite3
import sys

from review_store import REVIEW_FIELDS, ReviewStore


DEMO_CSV = Path(__file__).resolve().parent / "examples" / "demo_reviews.csv"
SYNTHETIC_NOTICE = (
    "Synthetic demo: all products, reviewers, and reviews are fictional. "
    "These are not real Amazon observations."
)


@contextmanager
def open_csv_rows(path: Path):
    """Check the file and header before opening a writable database."""
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV is empty: {path}")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("CSV contains duplicate column names")
        missing = set(REVIEW_FIELDS) - set(reader.fieldnames)
        if missing:
            raise ValueError("CSV is missing columns: " + ", ".join(sorted(missing)))

        def rows():
            for row in reader:
                if None in row:
                    raise ValueError(f"CSV row {reader.line_num} has too many columns")
                yield {field: row[field] for field in REVIEW_FIELDS}

        yield rows()


def print_summary(store: ReviewStore) -> None:
    summary = store.summary()
    print(f"Stored reviews: {summary['total_reviews']}")
    for product in summary["products"]:
        average = product["average_rating"]
        rating = "unavailable" if average is None else f"{average:.2f}/5"
        print(f"  {product['asin']}: {product['review_count']} reviews; average rating {rating}")
    checkpoints = store.list_checkpoints()
    print(f"Saved collection checkpoints: {len(checkpoints)}")
    for checkpoint in checkpoints:
        print(
            f"  {checkpoint['asin']}: {checkpoint['status']}; "
            f"processed snapshots {checkpoint['pages_completed']}"
        )
        if checkpoint.get("detail"):
            print(f"    {checkpoint['detail']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Work with a local review database. These commands make no network requests."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    demo = subcommands.add_parser("demo", help="Load a clearly labelled synthetic demonstration dataset")
    demo.add_argument("--db", type=Path, default=Path("data/demo.sqlite3"))
    demo.add_argument("--output", type=Path, help="Optionally export the database to CSV")

    importer = subcommands.add_parser("import", help="Import a CSV with the documented review columns")
    importer.add_argument("input", type=Path)
    importer.add_argument("--db", type=Path, default=Path("data/reviews.sqlite3"))

    exporter = subcommands.add_parser("export", help="Export an existing database to CSV")
    exporter.add_argument("output", type=Path)
    exporter.add_argument("--db", type=Path, default=Path("data/reviews.sqlite3"))

    status = subcommands.add_parser("status", help="Show counts from an existing database")
    status.add_argument("--db", type=Path, default=Path("data/reviews.sqlite3"))
    return parser


def run(args: argparse.Namespace) -> None:
    read_only = args.command in {"status", "export"}
    if read_only and not args.db.is_file():
        raise ValueError(f"Database does not exist: {args.db}. Run demo or import first.")

    with ExitStack() as stack:
        rows = None
        if args.command in {"demo", "import"}:
            csv_path = DEMO_CSV if args.command == "demo" else args.input
            rows = stack.enter_context(open_csv_rows(csv_path))
        store = stack.enter_context(ReviewStore(args.db, read_only=read_only))
        if args.command == "demo":
            print(SYNTHETIC_NOTICE)
            counts = store.upsert_reviews(rows, source="synthetic-demo")
            print(f"Imported {counts['inserted']} reviews; skipped {counts['duplicates']} duplicates.")
            if args.output is not None:
                count = store.export_csv(args.output)
                print(f"Exported {count} reviews to {args.output}")
        elif args.command == "import":
            counts = store.upsert_reviews(rows, source="import")
            print(f"Imported {counts['inserted']} reviews; skipped {counts['duplicates']} duplicates.")
        elif args.command == "export":
            count = store.export_csv(args.output)
            print(f"Exported {count} reviews to {args.output}")
        print_summary(store)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except (OSError, ValueError, csv.Error, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
