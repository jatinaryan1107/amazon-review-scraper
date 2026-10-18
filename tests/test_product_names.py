"""Product identity and migration checks using fictional data only."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from collector import PageSnapshot, collect_product
from review_parser import parse_product_name
from review_store import ReviewStore


ASIN = "B0TEST0001"
URL = f"https://www.amazon.in/dp/{ASIN}"


def review():
    return {"asin": ASIN, "product_url": URL, "review_id": "TEST-1", "reviewer_name": "Fictional",
            "verified_purchase": "true", "review_title": "A review", "rating": "4", "review_date": "",
            "review_body": "Works well."}


class ProductNamesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "reviews.sqlite3"

    def test_version_one_reads_without_writing_and_migrates_without_losing_reviews(self):
        with ReviewStore(self.path) as store:
            store.save_page(ASIN, [review()], next_url=None, pages_completed=1, status="complete")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE products")
            connection.execute("PRAGMA user_version = 1")
        before = self.path.read_bytes()
        with ReviewStore(self.path, read_only=True) as store:
            self.assertEqual(store.product_names(), {})
            self.assertEqual(store.summary()["total_reviews"], 1)
        self.assertEqual(self.path.read_bytes(), before)
        with ReviewStore(self.path) as store:
            store.set_product_name(ASIN, "Fictional headphones")
            self.assertEqual(store.summary()["total_reviews"], 1)
            self.assertEqual(store.get_checkpoint(ASIN)["status"], "complete")
        with ReviewStore(self.path, read_only=True) as store:
            self.assertEqual(store.product_names()[ASIN]["name"], "Fictional headphones")
            with self.assertRaises(sqlite3.OperationalError):
                store.set_product_name(ASIN, "No write")

    def test_manual_name_survives_lower_priority_refreshes(self):
        with ReviewStore(self.path) as store:
            store.set_product_name(ASIN, "Earlier research", source="research")
            store.set_product_name(ASIN, "Amazon title", source="amazon")
            store.set_product_name(ASIN, "CSV label", source="import")
            self.assertEqual(store.product_names()[ASIN]["name"], "Amazon title")
            store.set_product_name(ASIN, "  My   headphones  ")
            store.save_page(ASIN, [review()], next_url=None, pages_completed=1, status="complete", product_name="New Amazon title")
            store.upsert_reviews([review()], product_names={ASIN: "CSV label"})
            self.assertEqual(store.product_names()[ASIN]["name"], "My headphones")
            self.assertEqual(store.product_names()[ASIN]["name_source"], "manual")

    def test_metadata_validation_rolls_back_review_import_and_page_checkpoint(self):
        with ReviewStore(self.path) as store:
            for name in ["", "x" * 501, "bad\x00name", 12]:
                with self.assertRaises(ValueError):
                    store.upsert_reviews([review()], product_names={ASIN: name})
                self.assertEqual(store.summary()["total_reviews"], 0)
            with self.assertRaises(ValueError):
                store.save_page(ASIN, [review()], next_url=None, pages_completed=1, status="complete", product_name="x" * 501)
            self.assertEqual(store.summary()["total_reviews"], 0)
            self.assertIsNone(store.get_checkpoint(ASIN))
            self.assertEqual(store.product_names(), {})

    def test_parser_uses_explicit_title_and_rejects_other_product_link(self):
        self.assertEqual(parse_product_name('<span id="productTitle">Fictional &amp; compact speaker</span>', ASIN), "Fictional & compact speaker")
        self.assertEqual(parse_product_name(f'<a data-hook="product-link" href="/dp/{ASIN}">Fictional earbuds</a>', ASIN), "Fictional earbuds")
        self.assertEqual(parse_product_name('<title>Amazon.in: Customer reviews: Fictional speaker</title>', ASIN), "Fictional speaker")
        for html in ['<title>Amazon Sign In</title>', '<div data-hook="review-title">Not a product name</div>', '<a data-hook="product-link" href="/dp/B0OTHER001">Other product</a>']:
            self.assertEqual(parse_product_name(html, ASIN), "")

    def test_collection_captures_title_in_the_saved_page_transaction(self):
        class Browser:
            def load(self, url):
                return PageSnapshot(url, '<span id="productTitle">Fictional speaker</span><div data-hook="review" id="ONE"><span data-hook="review-body">Good.</span></div>')
        with ReviewStore(self.path) as store:
            result = collect_product(Browser(), store, URL, max_pages=1)
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(store.product_names()[ASIN]["name"], "Fictional speaker")
            self.assertEqual(store.product_names()[ASIN]["name_source"], "amazon")


if __name__ == "__main__":
    unittest.main()
