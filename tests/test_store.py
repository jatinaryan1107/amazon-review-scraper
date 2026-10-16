"""Offline tests: no scraper, browser, or Amazon requests are used here."""

import csv
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from review_store import REVIEW_FIELDS, SCHEMA_VERSION, ReviewStore, make_review_id


ASIN = "B012345678"
SECOND_ASIN = "B987654321"
URL = f"https://www.amazon.in/dp/{ASIN}"


def review(review_id="R1", **changes):
    result = {
        "asin": ASIN,
        "product_url": URL,
        "review_id": review_id,
        "reviewer_name": "Anita",
        "verified_purchase": "Verified Purchase",
        "review_title": "Useful product",
        "rating": "4.0",
        "review_date": "Reviewed in India on 1 January 2026",
        "review_body": "Works well.",
    }
    result.update(changes)
    return result


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="store-test-", dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "reviews.sqlite3"

    def test_deduplicates_across_reopens_and_keeps_product_identity(self):
        with ReviewStore(self.database) as store:
            self.assertEqual(store.upsert_reviews([review(), review()]), {"inserted": 1, "duplicates": 1})
        with ReviewStore(self.database) as store:
            self.assertEqual(store.upsert_reviews([review()]), {"inserted": 0, "duplicates": 1})
            other_product = review(asin=SECOND_ASIN, product_url=f"https://amazon.in/dp/{SECOND_ASIN}")
            self.assertEqual(store.upsert_reviews([other_product]), {"inserted": 1, "duplicates": 0})
            self.assertEqual(store.summary()["total_reviews"], 2)

    def test_invalid_later_row_rolls_back_entire_batch(self):
        with ReviewStore(self.database) as store:
            store.upsert_reviews([review("existing")])
            with self.assertRaises(ValueError):
                store.upsert_reviews([review("new"), review("invalid", rating="6")])
            self.assertEqual(store.summary()["total_reviews"], 1)

    def test_page_and_checkpoint_roll_back_when_checkpoint_database_write_fails(self):
        with ReviewStore(self.database) as store:
            store.save_page(ASIN, [review("first")], next_url=URL, pages_completed=1, status="in_progress")
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute(
                    "CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON checkpoints "
                    "BEGIN SELECT RAISE(ABORT, 'simulated checkpoint failure'); END"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                store.save_page(ASIN, [review("second")], next_url=None, pages_completed=2, status="complete")
            self.assertEqual(store.summary()["total_reviews"], 1)
            self.assertEqual(store.get_checkpoint(ASIN)["pages_completed"], 1)
            self.assertEqual(store.get_checkpoint(ASIN)["status"], "in_progress")

    def test_invalid_page_review_preserves_existing_checkpoint(self):
        with ReviewStore(self.database) as store:
            store.save_page(ASIN, [], next_url=URL, pages_completed=0, status="paused", detail="Start later")
            with self.assertRaises(ValueError):
                store.save_page(
                    ASIN, [review("valid"), review("bad", asin=SECOND_ASIN, product_url=f"https://amazon.in/dp/{SECOND_ASIN}")],
                    next_url=None, pages_completed=1, status="complete",
                )
            self.assertEqual(store.summary()["total_reviews"], 0)
            self.assertEqual(store.get_checkpoint(ASIN)["status"], "paused")

    def test_checkpoint_persists_and_updates_without_duplicate_reviews(self):
        next_url = f"https://www.amazon.in/product-reviews/{ASIN}/?pageNumber=2"
        with ReviewStore(self.database) as store:
            store.save_page(ASIN, [review()], next_url=next_url, pages_completed=1, status="in_progress", replay_steps=2)
        with ReviewStore(self.database) as store:
            checkpoint = store.get_checkpoint(ASIN)
            self.assertEqual(checkpoint["next_url"], next_url)
            self.assertEqual(checkpoint["pages_completed"], 1)
            self.assertEqual(checkpoint["replay_steps"], 2)
            counts = store.save_page(ASIN, [review()], next_url=None, pages_completed=2, status="complete", detail="Finished")
            self.assertEqual(counts, {"inserted": 0, "duplicates": 1})
            self.assertEqual(store.list_checkpoints()[0]["detail"], "Finished")
            self.assertEqual(store.list_checkpoints()[0]["replay_steps"], 0)
            self.assertIsNone(store.get_checkpoint(SECOND_ASIN))

    def test_export_round_trips_unicode_quoted_text_and_newlines(self):
        body = 'बहुत अच्छा, "excellent"!\nSecond line with café.'
        with ReviewStore(self.database) as store:
            store.upsert_reviews([review(review_body=body, reviewer_name="मीरा", verified_purchase=True)])
            output = self.root / "exports" / "reviews.csv"
            output.parent.mkdir()
            output.write_text("old file", encoding="utf-8")
            self.assertEqual(store.export_csv(output), 1)
            with output.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames), REVIEW_FIELDS)
                rows = list(reader)
            self.assertEqual(rows[0]["review_body"], body)
            self.assertEqual(rows[0]["reviewer_name"], "मीरा")
            self.assertEqual(rows[0]["verified_purchase"], "true")
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_failed_export_preserves_previous_csv_and_cleans_temp_file(self):
        output = self.root / "reviews.csv"
        output.write_text("previous", encoding="utf-8")
        with ReviewStore(self.database) as store:
            store.upsert_reviews([review()])
            with patch("review_store.os.replace", side_effect=PermissionError("output is open")):
                with self.assertRaises(PermissionError):
                    store.export_csv(output)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_fallback_id_is_stable_across_reopens_and_sensitive_to_content(self):
        with ReviewStore(self.database) as store:
            store.upsert_reviews([review("")])
        with ReviewStore(self.database) as store:
            self.assertEqual(store.upsert_reviews([review(None)]), {"inserted": 0, "duplicates": 1})
            self.assertEqual(store.upsert_reviews([review("", review_body="Different contents")]), {"inserted": 1, "duplicates": 0})
        with closing(sqlite3.connect(self.database)) as connection, connection:
            identifiers = [row[0] for row in connection.execute("SELECT review_id FROM reviews")]
        self.assertTrue(all(value.startswith("sha256:") and len(value) == 71 for value in identifiers))
        original = review("")
        self.assertIn(make_review_id(*(original[field] for field in ("reviewer_name", "review_date", "review_title", "review_body"))), identifiers)

    def test_validation_rejects_invalid_identity_url_rating_and_boolean(self):
        invalid_cases = [
            {"asin": "b012345678"},
            {"asin": "SHORT"},
            {"asin": "B01234567é"},
            {"product_url": f"https://amazon.in.evil.example/dp/{ASIN}"},
            {"product_url": f"https://amazon.in@evil.example/dp/{ASIN}"},
            {"product_url": f"https://www.amazon.in/dp/{SECOND_ASIN}"},
            {"product_url": "https://amazon.in/"},
            {"product_url": f"file:///dp/{ASIN}"},
            {"product_url": f"http://www.amazon.in/dp/{ASIN}"},
            {"product_url": f"https://amazon.in:443/dp/{ASIN}"},
            {"rating": "NaN"},
            {"rating": "Infinity"},
            {"rating": "0"},
            {"rating": True},
            {"verified_purchase": "maybe"},
        ]
        with ReviewStore(self.database) as store:
            for changes in invalid_cases:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    store.upsert_reviews([review(**changes)])
            self.assertEqual(store.summary()["total_reviews"], 0)

    def test_checkpoint_validation_is_atomic(self):
        invalid_options = [
            {"status": "unknown"},
            {"pages_completed": -1},
            {"pages_completed": True},
            {"pages_completed": 1.5},
            {"replay_steps": -1},
            {"replay_steps": True},
            {"replay_steps": 1.5},
            {"next_url": f"https://example.com/product-reviews/{ASIN}"},
            {"next_url": f"http://www.amazon.in/product-reviews/{ASIN}"},
        ]
        with ReviewStore(self.database) as store:
            for changes in invalid_options:
                options = {"next_url": None, "pages_completed": 1, "status": "complete"}
                options.update(changes)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    store.save_page(ASIN, [review()], **options)
            self.assertEqual(store.summary()["total_reviews"], 0)
            self.assertEqual(store.list_checkpoints(), [])

    def test_summary_averages_only_available_ratings_and_records_source(self):
        with ReviewStore(self.database) as store:
            store.upsert_reviews([review("one", rating="3"), review("two", rating=5), review("three", rating="")], source="demo")
            store.upsert_reviews([review("other", asin=SECOND_ASIN, product_url=f"https://amazon.in/gp/product/{SECOND_ASIN}", rating=None)])
            self.assertEqual(store.summary(), {
                "total_reviews": 4,
                "products": [
                    {"asin": ASIN, "review_count": 3, "average_rating": 4.0},
                    {"asin": SECOND_ASIN, "review_count": 1, "average_rating": None},
                ],
            })
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self.assertEqual(connection.execute("SELECT source FROM reviews WHERE review_id = 'one'").fetchone()[0], "demo")

    def test_csv_cannot_replace_database(self):
        with ReviewStore(self.database) as store:
            store.upsert_reviews([review()])
            with self.assertRaises(ValueError):
                store.export_csv(self.database)
            self.assertEqual(store.summary()["total_reviews"], 1)

    def test_legacy_checkpoint_table_adds_replay_steps(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "CREATE TABLE checkpoints (asin TEXT PRIMARY KEY, next_url TEXT, "
                "pages_completed INTEGER NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT '')"
            )
            connection.execute(
                "INSERT INTO checkpoints (asin, next_url, pages_completed, status, detail) "
                "VALUES (?, ?, ?, ?, ?)", (ASIN, URL, 1, "paused", "Previous checkpoint")
            )
        with ReviewStore(self.database) as store:
            self.assertEqual(store.get_checkpoint(ASIN)["replay_steps"], 0)

    def test_read_only_reads_and_export_leave_database_unchanged(self):
        self.database = self.root / "reviews #1.sqlite3"
        with ReviewStore(self.database) as store:
            store.save_page(ASIN, [review()], next_url=URL, pages_completed=1, status="paused", replay_steps=2)
        original_bytes = self.database.read_bytes()
        original_modified = self.database.stat().st_mtime_ns
        output = self.root / "read-only-export.csv"
        with ReviewStore(self.database, read_only=True) as store:
            self.assertEqual(store.summary()["total_reviews"], 1)
            self.assertEqual(store.get_checkpoint(ASIN)["replay_steps"], 2)
            self.assertEqual(store.list_checkpoints()[0]["status"], "paused")
            self.assertEqual(store.export_csv(output), 1)
            with self.assertRaisesRegex(sqlite3.OperationalError, "read-only"):
                store.upsert_reviews([review("new")])
            with self.assertRaisesRegex(sqlite3.OperationalError, "read-only"):
                store.save_page(ASIN, [], next_url=None, pages_completed=2, status="complete")
        self.assertEqual(self.database.read_bytes(), original_bytes)
        self.assertEqual(self.database.stat().st_mtime_ns, original_modified)
        self.assertEqual({path.name for path in self.root.iterdir()}, {self.database.name, output.name})

    def test_unrelated_database_is_rejected_without_adding_tables(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE personal_notes (note TEXT)")
            connection.execute("INSERT INTO personal_notes VALUES ('Leave this file alone')")
        original_bytes = self.database.read_bytes()
        for read_only in (True, False):
            with self.subTest(read_only=read_only), self.assertRaisesRegex(ValueError, "Not a review database"):
                ReviewStore(self.database, read_only=read_only)
            self.assertEqual(self.database.read_bytes(), original_bytes)
        with closing(sqlite3.connect(self.database)) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        self.assertEqual(tables, ["personal_notes"])

    def test_missing_read_only_database_does_not_create_file_or_parent(self):
        missing = self.root / "missing-folder" / "missing.sqlite3"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ReviewStore(missing, read_only=True)
        self.assertFalse(missing.parent.exists())

    def test_read_only_corrupt_database_is_rejected_without_changes(self):
        self.database.write_text("This is not a SQLite database", encoding="utf-8")
        original_bytes = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "Cannot open review database"):
            ReviewStore(self.database, read_only=True)
        self.assertEqual(self.database.read_bytes(), original_bytes)

    def test_future_schema_version_is_rejected_without_changes(self):
        with ReviewStore(self.database):
            pass
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        original_bytes = self.database.read_bytes()
        for read_only in (True, False):
            with self.subTest(read_only=read_only), self.assertRaisesRegex(ValueError, "schema version.*unsupported"):
                ReviewStore(self.database, read_only=read_only)
            self.assertEqual(self.database.read_bytes(), original_bytes)

    def test_new_database_sets_schema_version(self):
        with ReviewStore(self.database):
            pass
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_legacy_version_zero_is_readable_without_migration(self):
        review_columns = ", ".join(f"{field} TEXT NOT NULL" for field in (*REVIEW_FIELDS, "source", "created_at"))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(f"CREATE TABLE reviews ({review_columns}, PRIMARY KEY (asin, review_id))")
            connection.execute(
                "CREATE TABLE checkpoints (asin TEXT PRIMARY KEY, next_url TEXT, "
                "pages_completed INTEGER NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT '')"
            )
            connection.execute(
                "INSERT INTO checkpoints (asin, next_url, pages_completed, status, detail) VALUES (?, ?, ?, ?, ?)",
                (ASIN, URL, 1, "paused", "Older schema"),
            )
        original_bytes = self.database.read_bytes()
        with ReviewStore(self.database, read_only=True) as store:
            self.assertEqual(store.summary()["total_reviews"], 0)
            self.assertEqual(store.get_checkpoint(ASIN)["replay_steps"], 0)
            self.assertEqual(store.list_checkpoints()[0]["replay_steps"], 0)
        self.assertEqual(self.database.read_bytes(), original_bytes)
        with ReviewStore(self.database) as store:
            self.assertEqual(store.get_checkpoint(ASIN)["replay_steps"], 0)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertIn("replay_steps", {row[1] for row in connection.execute("PRAGMA table_info(checkpoints)")})

    def test_initialization_error_closes_the_connection(self):
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE reviews (asin TEXT)")
        connection.commit()
        with patch("review_store.sqlite3.connect", return_value=connection):
            with self.assertRaisesRegex(ValueError, "Not a review database"):
                ReviewStore(self.database, read_only=True)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_review_table_without_deduplication_key_is_rejected(self):
        with ReviewStore(self.database):
            pass
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("ALTER TABLE reviews RENAME TO previous_reviews")
            connection.execute("CREATE TABLE reviews AS SELECT * FROM previous_reviews")
            connection.execute("DROP TABLE previous_reviews")
        original_bytes = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "incompatible primary key"):
            ReviewStore(self.database, read_only=True)
        self.assertEqual(self.database.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
