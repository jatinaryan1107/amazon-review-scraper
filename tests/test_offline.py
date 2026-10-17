"""Exercise only the network-free CLI; never import the live scraper."""

import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from review_store import REVIEW_FIELDS, ReviewStore


ROOT = Path(__file__).resolve().parents[1]


class OfflineCLITests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tempdir.cleanup)
        self.folder = Path(self.tempdir.name)
        self.db = self.folder / "reviews.sqlite3"

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "offline.py"), *map(str, args)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_demo_is_idempotent_and_exports_all_reviews(self):
        output = self.folder / "reviews.csv"
        first = self.cli("demo", "--db", self.db, "--output", output)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("not real Amazon observations", first.stdout)
        self.assertIn("Imported 15 reviews; skipped 0 duplicates", first.stdout)
        second = self.cli("demo", "--db", self.db)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Imported 0 reviews; skipped 15 duplicates", second.stdout)
        with output.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, list(REVIEW_FIELDS))
            rows = list(reader)
        self.assertEqual(len(rows), 15)
        self.assertEqual(len({(row["asin"], row["review_id"]) for row in rows}), 15)
        self.assertEqual({row["asin"] for row in rows}, {"B0DEMO0001", "B0DEMO0002", "B0DEMO0003"})
        database_before_reads = self.db.read_bytes()
        status = self.cli("status", "--db", self.db)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("Stored reviews: 15", status.stdout)
        second_export = self.folder / "exported.csv"
        result = self.cli("export", second_export, "--db", self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_bytes(), second_export.read_bytes())
        self.assertEqual(self.db.read_bytes(), database_before_reads)

    def test_missing_database_does_not_get_created(self):
        missing = self.folder / "does-not-exist" / "missing.sqlite3"
        output = self.folder / "missing.csv"
        for args in [("status", "--db", missing), ("export", output, "--db", missing)]:
            with self.subTest(command=args[0]):
                result = self.cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Database does not exist", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(missing.exists())
                self.assertFalse(missing.parent.exists())
                self.assertFalse(output.exists())

    def test_invalid_import_rolls_back_earlier_valid_rows(self):
        fixture = self.folder / "invalid.csv"
        valid = dict(zip(REVIEW_FIELDS, [
            "B0DEMO0001", "https://www.amazon.in/dp/B0DEMO0001", "NEW-REVIEW-01", "Demo reviewer",
            "True", "Synthetic test", "4", "2026-01-01", "Fictional test review.",
        ]))
        invalid = {**valid, "review_id": "NEW-REVIEW-02", "rating": "9"}
        with fixture.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
            writer.writeheader()
            writer.writerows([valid, invalid])
        result = self.cli("import", fixture, "--db", self.db)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        with ReviewStore(self.db) as store:
            self.assertEqual(store.summary()["total_reviews"], 0)

    def test_import_rejects_missing_columns_with_helpful_error(self):
        fixture = self.folder / "wrong.csv"
        fixture.write_text("asin,review_id\nB0DEMO0001,DEMO-1\n", encoding="utf-8")
        result = self.cli("import", fixture, "--db", self.db)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CSV is missing columns", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.db.exists())

    def test_missing_import_file_does_not_create_database_or_parent(self):
        missing_db = self.folder / "new-folder" / "reviews.sqlite3"
        result = self.cli("import", self.folder / "missing.csv", "--db", missing_db)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(missing_db.parent.exists())

    def test_import_rejects_duplicate_column_names_before_creating_database(self):
        fixture = self.folder / "duplicate-columns.csv"
        fixture.write_text(",".join([*REVIEW_FIELDS, "review_id"]) + "\n", encoding="utf-8")
        result = self.cli("import", fixture, "--db", self.db)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CSV contains duplicate column names", result.stderr)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
