"""Local dashboard queries and CSV transfers, with no browser dependencies."""

from __future__ import annotations

from contextlib import contextmanager
import csv
from dataclasses import dataclass
import io
import math
from pathlib import Path
import sqlite3
from typing import Any

from offline import DEMO_CSV, SYNTHETIC_NOTICE, open_csv_rows
from review_store import REVIEW_FIELDS, ReviewStore, product_name


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 10_000
DEMO_NAMES = {
    "B0DEMO0001": "North Desk Fan",
    "B0DEMO0002": "Field Desk Fan",
    "B0DEMO0003": "Harbor Desk Fan",
}
DATASETS = {
    "demo": {"id": "demo", "name": "Demo workspace", "is_demo": True,
             "description": SYNTHETIC_NOTICE},
    "workspace": {"id": "workspace", "name": "My workspace", "is_demo": False,
                  "description": "Your locally imported reviews. Collection coverage depends on your source data."},
}
REVIEW_COLUMNS = ", ".join(REVIEW_FIELDS) + ", source, created_at"
RATING_BUCKET = "CAST(CAST(rating AS REAL) + 0.5 AS INTEGER)"
SORTS = {
    "newest": "created_at DESC, asin, review_id DESC",
    "oldest": "created_at ASC, asin, review_id ASC",
    "rating_desc": "(rating = '') ASC, CAST(rating AS REAL) DESC, created_at DESC, asin, review_id",
    "rating_asc": "(rating = '') ASC, CAST(rating AS REAL) ASC, created_at DESC, asin, review_id",
}


@dataclass(frozen=True)
class ReviewQuery:
    dataset: str = "demo"
    q: str = ""
    asin: str = ""
    rating: str = "all"
    verified: str = "all"
    sort: str = "newest"
    page: int = 1
    page_size: int = 10

    @classmethod
    def parse(cls, params: dict[str, list[str]]) -> "ReviewQuery":
        allowed = {"dataset", "q", "asin", "rating", "verified", "sort", "page", "page_size"}
        if set(params) - allowed:
            raise ValueError("This request contains an unsupported filter.")
        if any(len(values) != 1 for values in params.values()):
            raise ValueError("Each filter can be supplied only once.")
        values = {name: entries[0] for name, entries in params.items()}
        dataset = values.get("dataset", "demo")
        if dataset not in DATASETS:
            raise ValueError("Choose the demo or workspace dataset.")
        q = values.get("q", "")
        if len(q) > 200:
            raise ValueError("Search text must be 200 characters or fewer.")
        asin = values.get("asin", "")
        if asin and (len(asin) != 10 or not asin.isascii() or not asin.isalnum() or asin != asin.upper()):
            raise ValueError("The product filter must be a valid ten-character ASIN.")
        rating = values.get("rating", "all")
        verified = values.get("verified", "all")
        sort = values.get("sort", "newest")
        if rating not in {"all", "critical", "1", "2", "3", "4", "5"}:
            raise ValueError("Choose all ratings, critical reviews, or a star rating from 1 to 5.")
        if verified not in {"all", "true", "false"}:
            raise ValueError("Choose all, verified, or unverified purchases.")
        if sort not in SORTS:
            raise ValueError("Choose a supported review sort order.")
        try:
            page = max(1, int(values.get("page", "1")))
            page_size = min(100, max(1, int(values.get("page_size", "10"))))
        except ValueError as exc:
            raise ValueError("Page and page size must be whole numbers.") from exc
        return cls(dataset, q, asin, rating, verified, sort, page, page_size)

    def where(self, *, product_names=False) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.q:
            literal = self.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(" + " OR ".join(
                f"{column} LIKE ? ESCAPE '\\'" for column in
                ("review_title", "review_body", "reviewer_name", "review_id", "asin")
            ) + ")")
            params.extend([f"%{literal}%"] * 5)
            if product_names:
                clauses[-1] = clauses[-1][:-1] + " OR asin IN (SELECT asin FROM products WHERE name LIKE ? ESCAPE '\\'))"
                params.append(f"%{literal}%")
        if self.asin:
            clauses.append("asin = ?")
            params.append(self.asin)
        if self.rating == "critical":
            clauses.append("rating != '' AND CAST(rating AS REAL) <= 2")
        elif self.rating != "all":
            clauses.append(f"rating != '' AND {RATING_BUCKET} = ?")
            params.append(int(self.rating))
        if self.verified != "all":
            clauses.append("verified_purchase = ?")
            params.append(self.verified)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


class DashboardData:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def seed_demo(self) -> None:
        with open_csv_rows(DEMO_CSV) as rows:
            with ReviewStore(self.path("demo")) as store:
                store.upsert_reviews(rows, source="synthetic-demo")

    @staticmethod
    def _has_names(connection):
        return bool(connection and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'").fetchone())

    def name_product(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"dataset", "asin", "name"} or payload["dataset"] != "workspace":
            raise ValueError("Name a product in My workspace using dataset, asin and name.")
        if not isinstance(payload["asin"], str):
            raise ValueError("Choose a valid product ASIN.")
        query = ReviewQuery.parse({"dataset": ["workspace"], "asin": [payload["asin"]]})
        if not query.asin:
            raise ValueError("Choose a product to name.")
        name = product_name(payload["name"])
        with self.connection("workspace") as connection:
            exists = connection and connection.execute("SELECT 1 FROM reviews WHERE asin=? LIMIT 1", (query.asin,)).fetchone()
        if not exists:
            raise ValueError("This product is not in your saved reviews.")
        with ReviewStore(self.path("workspace")) as store:
            store.set_product_name(query.asin, name)
        return {"asin": query.asin, "name": name, "name_source": "manual"}

    def path(self, dataset: str) -> Path:
        if dataset not in DATASETS:
            raise ValueError("Choose the demo or workspace dataset.")
        return self.data_dir / ("demo.sqlite3" if dataset == "demo" else "reviews.sqlite3")

    @contextmanager
    def connection(self, dataset: str):
        path = self.path(dataset)
        if dataset == "workspace" and not path.exists():
            yield None
            return
        # ReviewStore owns schema validation. A separate read-only connection
        # avoids depending on its private connection or changing the schema.
        with ReviewStore(path, read_only=True):
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                yield connection
            finally:
                connection.close()

    def overview(self, dataset: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "dataset": dict(DATASETS[dataset]),
            "summary": {"total_reviews": 0, "product_count": 0, "average_rating": None,
                        "verified_count": 0, "verified_share": 0, "low_rating_count": 0},
            "ratings": [{"rating": rating, "count": 0} for rating in range(5, 0, -1)],
            "ratings_grouped": True,
            "products": [], "checkpoints": [], "recent_reviews": [], "last_updated": None,
        }
        with self.connection(dataset) as connection:
            if connection is None:
                return result
            aggregate = dict(connection.execute("""
                SELECT COUNT(*) AS total_reviews, COUNT(DISTINCT asin) AS product_count,
                       AVG(CASE WHEN rating != '' THEN CAST(rating AS REAL) END) AS average_rating,
                       COALESCE(SUM(verified_purchase = 'true'), 0) AS verified_count,
                       COALESCE(SUM(rating != '' AND CAST(rating AS REAL) <= 2), 0) AS low_rating_count,
                       MAX(created_at) AS last_updated FROM reviews
            """).fetchone())
            result["last_updated"] = aggregate.pop("last_updated")
            aggregate["verified_share"] = (
                100 * aggregate["verified_count"] / aggregate["total_reviews"]
                if aggregate["total_reviews"] else 0
            )
            result["summary"] = aggregate
            buckets = {row["bucket"]: row["count"] for row in connection.execute(
                f"SELECT {RATING_BUCKET} AS bucket, COUNT(*) AS count FROM reviews "
                "WHERE rating != '' GROUP BY bucket"
            )}
            result["ratings"] = [{"rating": rating, "count": buckets.get(rating, 0)} for rating in range(5, 0, -1)]
            products = connection.execute("""
                SELECT asin, COUNT(*) AS review_count,
                       AVG(CASE WHEN rating != '' THEN CAST(rating AS REAL) END) AS average_rating,
                       SUM(verified_purchase = 'true') AS verified_count,
                       SUM(rating != '' AND CAST(rating AS REAL) <= 2) AS critical_count,
                       MAX(created_at) AS last_review_added_at
                FROM reviews GROUP BY asin ORDER BY review_count DESC, asin
            """)
            names = {row["asin"]: dict(row) for row in connection.execute("SELECT * FROM products")} if self._has_names(connection) else {}
            result["products"] = [
                {**dict(row), "name": DEMO_NAMES.get(row["asin"], "") if dataset == "demo" else names.get(row["asin"], {}).get("name", ""),
                 "name_source": "demo" if dataset == "demo" else names.get(row["asin"], {}).get("name_source", ""),
                 "product_url": "" if dataset == "demo" else f"https://www.amazon.in/dp/{row['asin']}"}
                for row in products
            ]
            result["checkpoints"] = [
                {**dict(row), "replay_steps": dict(row).get("replay_steps", 0)}
                for row in connection.execute("SELECT * FROM checkpoints ORDER BY asin")
            ]
            result["recent_reviews"] = [dict(row) for row in connection.execute(
                f"SELECT {REVIEW_COLUMNS} FROM reviews ORDER BY {SORTS['newest']} LIMIT 5"
            )]
            checkpoint_update = connection.execute("SELECT MAX(updated_at) FROM checkpoints").fetchone()[0]
            if checkpoint_update and (not result["last_updated"] or checkpoint_update > result["last_updated"]):
                result["last_updated"] = checkpoint_update
        return result

    def reviews(self, query: ReviewQuery) -> dict[str, Any]:
        with self.connection(query.dataset) as connection:
            where, params = query.where(product_names=self._has_names(connection))
            total = connection.execute("SELECT COUNT(*) FROM reviews" + where, params).fetchone()[0] if connection else 0
            pages = max(1, math.ceil(total / query.page_size))
            page = min(query.page, pages)
            items = [dict(row) for row in connection.execute(
                f"SELECT {REVIEW_COLUMNS} FROM reviews{where} ORDER BY {SORTS[query.sort]} LIMIT ? OFFSET ?",
                [*params, query.page_size, (page - 1) * query.page_size],
            )] if connection else []
            return {"items": items, "total": total, "page": page, "pages": pages, "page_size": query.page_size}

    def review(self, dataset: str, asin: str, review_id: str):
        with self.connection(dataset) as connection:
            row = connection.execute(f"SELECT {REVIEW_COLUMNS} FROM reviews WHERE asin=? AND review_id=?",
                                     (asin, review_id)).fetchone() if connection else None
            return dict(row) if row else None

    def export(self, query: ReviewQuery) -> bytes:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=(*REVIEW_FIELDS, "product_name"))
        writer.writeheader()
        with self.connection(query.dataset) as connection:
            if connection:
                where, params = query.where(product_names=self._has_names(connection))
                names = {row["asin"]: row["name"] for row in connection.execute("SELECT asin, name FROM products")} if self._has_names(connection) else {}
                for row in connection.execute(
                    f"SELECT {', '.join(REVIEW_FIELDS)} FROM reviews{where} ORDER BY {SORTS[query.sort]}", params
                ):
                    writer.writerow({**{key: spreadsheet_cell(row[key]) for key in REVIEW_FIELDS},
                                     "product_name": spreadsheet_cell(DEMO_NAMES.get(row["asin"], "") if query.dataset == "demo" else names.get(row["asin"], ""))})
        return buffer.getvalue().encode("utf-8-sig")

    def import_csv(self, payload: bytes) -> dict[str, Any]:
        if len(payload) > MAX_IMPORT_BYTES:
            raise ValueError("Choose a CSV file no larger than 5 MiB.")
        try:
            content = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Save your CSV with UTF-8 encoding and try again.") from exc
        if "\x00" in content:
            raise ValueError("The CSV contains an invalid null character.")
        reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
        try:
            headers = reader.fieldnames
            if not headers:
                raise ValueError("This CSV is empty. Include the nine review column names.")
            if len(headers) != len(set(headers)):
                raise ValueError("The CSV contains duplicate column names.")
            missing = set(REVIEW_FIELDS) - set(headers)
            if missing:
                raise ValueError("The CSV is missing columns: " + ", ".join(sorted(missing)) + ".")
            rows, names = [], {}
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"CSV row {reader.line_num} does not match the column headers.")
                if len(rows) == MAX_IMPORT_ROWS:
                    raise ValueError("Import at most 10,000 reviews at a time.")
                rows.append({field: row[field] for field in REVIEW_FIELDS})
                if row.get("product_name", "").strip():
                    name = product_name(row["product_name"])
                    asin = row["asin"].strip()
                    if asin in names and names[asin] != name:
                        raise ValueError("Use one consistent product name per ASIN in a CSV.")
                    names[asin] = name
        except csv.Error as exc:
            raise ValueError("The CSV could not be read. Check its quoting and column lengths.") from exc
        if not rows:
            raise ValueError("The CSV has column headers but no reviews to import.")
        with ReviewStore(self.path("workspace")) as store:
            counts = store.upsert_reviews(rows, source="dashboard-import", product_names=names)
            total = store.summary()["total_reviews"]
        return {**counts, "total_reviews": total,
                "message": f"Imported {counts['inserted']} reviews. Skipped {counts['duplicates']} duplicates."}


def spreadsheet_cell(value: str) -> str:
    """Keep external review text from becoming an executable spreadsheet cell."""
    start = 0
    while start < len(value) and (value[start].isspace() or ord(value[start]) < 32 or ord(value[start]) == 127):
        start += 1
    stripped = value[start:]
    if value.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
