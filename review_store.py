"""Persistent, transactional review storage using only the Python standard library.

This module has no browser or network dependencies. Reviews are deduplicated by
product ASIN and review ID, including across separate program runs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit


REVIEW_FIELDS = (
    "asin",
    "product_url",
    "review_id",
    "reviewer_name",
    "verified_purchase",
    "review_title",
    "rating",
    "review_date",
    "review_body",
)

CHECKPOINT_STATUSES = frozenset(
    {"in_progress", "complete", "paused", "blocked", "failed", "zero_reviews", "partial"}
)
SCHEMA_VERSION = 2


def product_name(value: Any) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 and not char.isspace() for char in value):
        raise ValueError("Product name must be plain text.")
    name = " ".join(value.split())
    if not 1 <= len(name) <= 500:
        raise ValueError("Use a product name between 1 and 500 characters.")
    return name

_ASIN = re.compile(r"[A-Z0-9]{10}\Z", re.ASCII)
_PRODUCT_PATH = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|product-reviews)/([A-Z0-9]{10})(?:/|$)",
    re.ASCII,
)
PORTAL_REVIEW_PATH = re.compile(
    r"/portal/customer-reviews/([A-Z0-9]{10})(?:/ref=[A-Z0-9_-]{1,200})?/?\Z",
    re.IGNORECASE | re.ASCII,
)


def _text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def _asin(value: Any) -> str:
    result = _text(value, "asin").strip()
    if not _ASIN.fullmatch(result):
        raise ValueError("asin must contain exactly 10 uppercase letters or digits")
    return result


def _amazon_url(value: Any, asin: str, field: str) -> str:
    result = _text(value, field).strip()
    try:
        parsed = urlsplit(result)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid Amazon.in product URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"amazon.in", "www.amazon.in"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise ValueError(f"{field} must be an HTTPS Amazon.in product or review URL")
    decoded_path = unquote(parsed.path)
    if decoded_path.casefold().startswith("/portal/customer-reviews"):
        # Portal pagination has a narrow route and an optional reference suffix.
        # Match the original path so encoded separators cannot widen the route.
        match = PORTAL_REVIEW_PATH.fullmatch(parsed.path)
    else:
        match = _PRODUCT_PATH.search(decoded_path)
    if not match or match.group(1).upper() != asin:
        raise ValueError(f"{field} must refer to ASIN {asin}")
    return result


def _rating(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if isinstance(value, bool):
        raise ValueError("rating must be empty or a number between 1 and 5")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("rating must be empty or a number between 1 and 5") from exc
    if not number.is_finite() or not Decimal(1) <= number <= Decimal(5):
        raise ValueError("rating must be empty or a number between 1 and 5")
    return format(number.normalize(), "f")


def _verified(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and value in (0, 1):
        return "true" if value else "false"
    normalized = _text(value, "verified_purchase").strip().casefold()
    if normalized in {"true", "yes", "1", "verified purchase"}:
        return "true"
    if normalized in {"false", "no", "0"}:
        return "false"
    if not normalized:
        return ""
    raise ValueError("verified_purchase must be true, false, or empty")


def make_review_id(
    reviewer_name: str, review_date: str, review_title: str, review_body: str
) -> str:
    """Return the shared, stable fallback ID used by parsers and imports."""
    fingerprint = json.dumps(
        [
            _text(reviewer_name, "reviewer_name"),
            _text(review_date, "review_date"),
            _text(review_title, "review_title"),
            _text(review_body, "review_body"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _review(review: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(review, Mapping):
        raise ValueError("each review must be a mapping of field names to values")
    normalized = {name: _text(review.get(name), name) for name in REVIEW_FIELDS if name not in {"rating", "verified_purchase"}}
    normalized["asin"] = _asin(review.get("asin"))
    normalized["product_url"] = _amazon_url(
        review.get("product_url"), normalized["asin"], "product_url"
    )
    normalized["rating"] = _rating(review.get("rating"))
    normalized["verified_purchase"] = _verified(review.get("verified_purchase"))
    normalized["review_id"] = normalized["review_id"].strip()
    if not normalized["review_id"]:
        normalized["review_id"] = make_review_id(
            normalized["reviewer_name"], normalized["review_date"],
            normalized["review_title"], normalized["review_body"],
        )
    return normalized


def _source(value: Any) -> str:
    result = _text(value, "source").strip()
    if not result:
        raise ValueError("source must be nonempty text")
    return result


class ReviewStore:
    """Store reviews and resumable collection checkpoints in one SQLite file."""

    def __init__(self, path: str | os.PathLike[str], *, read_only: bool = False):
        self.path = str(path)
        self.read_only = read_only
        connection = None
        try:
            if read_only:
                database = Path(self.path)
                if self.path == ":memory:" or not database.is_file():
                    raise ValueError(f"Review database does not exist: {self.path}")
                connection = sqlite3.connect(
                    database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
                )
            else:
                if self.path != ":memory:":
                    Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, timeout=30)
            self._connection = connection
            connection.row_factory = sqlite3.Row
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < 0 or version > SCHEMA_VERSION:
                raise ValueError(
                    f"Review database schema version {version} is unsupported; "
                    f"this program supports versions 0 through {SCHEMA_VERSION}."
                )
            if read_only:
                self._validate_schema(allow_legacy=version == 0)
            else:
                self._initialize_schema()
        except BaseException as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, sqlite3.DatabaseError):
                raise ValueError(f"Cannot open review database {self.path}: {exc}") from exc
            raise

    def _validate_table(self, table: str, *, allow_legacy: bool) -> None:
        columns = {
            row["name"]: row
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if table == "reviews":
            required = set(REVIEW_FIELDS) | {"source", "created_at"}
            expected_primary_key = ["asin", "review_id"]
        elif table == "products":
            required = {"asin", "name", "name_source", "updated_at"}
            expected_primary_key = ["asin"]
        else:
            required = {"asin", "next_url", "pages_completed", "status", "detail", "updated_at"}
            if not allow_legacy:
                required.add("replay_steps")
            expected_primary_key = ["asin"]
        missing = required - columns.keys()
        if missing:
            raise ValueError(
                f"Not a review database: table '{table}' is missing columns: "
                + ", ".join(sorted(missing))
            )
        primary_key = [
            row["name"] for row in sorted(columns.values(), key=lambda row: row["pk"])
            if row["pk"]
        ]
        if primary_key != expected_primary_key:
            raise ValueError(f"Not a review database: table '{table}' has an incompatible primary key.")

    def _validate_schema(self, *, allow_legacy: bool) -> None:
        tables = self._table_names()
        missing = {"reviews", "checkpoints"} - tables
        if missing:
            raise ValueError("Not a review database: missing tables: " + ", ".join(sorted(missing)))
        for table in ("reviews", "checkpoints"):
            self._validate_table(table, allow_legacy=allow_legacy)
        if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 2:
            self._validate_table("products", allow_legacy=False)

    def _table_names(self) -> set[str]:
        return {
            row["name"] for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _initialize_schema(self) -> None:
        # Validate before changing an existing file, including partial old schemas.
        tables = self._table_names()
        if tables and not tables.intersection({"reviews", "checkpoints"}):
            raise ValueError("Not a review database: existing file contains unrelated tables.")
        for table in ("reviews", "checkpoints"):
            if table in tables:
                self._validate_table(table, allow_legacy=True)
        if "products" in tables:
            self._validate_table("products", allow_legacy=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        with self._connection:
            self._connection.execute("BEGIN")
            self._connection.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                asin TEXT NOT NULL,
                product_url TEXT NOT NULL,
                review_id TEXT NOT NULL,
                reviewer_name TEXT NOT NULL,
                verified_purchase TEXT NOT NULL,
                review_title TEXT NOT NULL,
                rating TEXT NOT NULL,
                review_date TEXT NOT NULL,
                review_body TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (asin, review_id)
            )
            """)
            self._connection.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                asin TEXT PRIMARY KEY,
                next_url TEXT,
                pages_completed INTEGER NOT NULL CHECK (pages_completed >= 0),
                replay_steps INTEGER NOT NULL DEFAULT 0 CHECK (replay_steps >= 0),
                status TEXT NOT NULL CHECK (status IN (
                    'in_progress', 'complete', 'paused', 'blocked', 'failed',
                    'zero_reviews', 'partial'
                )),
                detail TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """)
            checkpoint_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(checkpoints)")
            }
            if "replay_steps" not in checkpoint_columns:
                self._connection.execute(
                    "ALTER TABLE checkpoints ADD COLUMN replay_steps INTEGER NOT NULL "
                    "DEFAULT 0 CHECK (replay_steps >= 0)"
                )
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    asin TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_source TEXT NOT NULL CHECK (name_source IN ('manual', 'amazon', 'import', 'research')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
            """)
            self._validate_schema(allow_legacy=False)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def __enter__(self) -> ReviewStore:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _insert(self, reviews: Iterable[dict[str, str]], source: str) -> dict[str, int]:
        inserted = 0
        duplicates = 0
        columns = ", ".join(REVIEW_FIELDS) + ", source"
        placeholders = ", ".join("?" for _ in range(len(REVIEW_FIELDS) + 1))
        for review in reviews:
            cursor = self._connection.execute(
                f"INSERT INTO reviews ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT (asin, review_id) DO NOTHING",
                [review[name] for name in REVIEW_FIELDS] + [source],
            )
            if cursor.rowcount:
                inserted += 1
            else:
                duplicates += 1
        return {"inserted": inserted, "duplicates": duplicates}

    def upsert_reviews(
        self, reviews: Iterable[Mapping[str, Any]], source: str = "import", *, product_names=None
    ) -> dict[str, int]:
        """Insert a batch, preserving existing rows and rolling back on errors."""
        self._require_writable()
        normalized_source = _source(source)
        with self._connection:
            counts = self._insert((_review(row) for row in reviews), normalized_source)
            for asin, name in (product_names or {}).items():
                self._set_product_name(asin, name, "import")
            return counts

    def product_names(self) -> dict[str, dict[str, str]]:
        if "products" not in self._table_names():
            return {}
        return {row["asin"]: dict(row) for row in self._connection.execute("SELECT * FROM products")}

    def _set_product_name(self, asin, name, source):
        if source not in {"manual", "amazon", "import", "research"}:
            raise ValueError("Unsupported product name source.")
        self._connection.execute("""
            INSERT INTO products (asin, name, name_source) VALUES (?, ?, ?)
            ON CONFLICT (asin) DO UPDATE SET name=excluded.name, name_source=excluded.name_source,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE (CASE products.name_source WHEN 'manual' THEN 3 WHEN 'amazon' THEN 2 ELSE 1 END)
               <= (CASE excluded.name_source WHEN 'manual' THEN 3 WHEN 'amazon' THEN 2 ELSE 1 END)
        """, (_asin(asin), product_name(name), source))

    def set_product_name(self, asin, name, *, source="manual"):
        self._require_writable()
        with self._connection:
            self._set_product_name(asin, name, source)

    def save_page(
        self,
        asin: str,
        reviews: Iterable[Mapping[str, Any]],
        *,
        next_url: str | None,
        pages_completed: int,
        status: str,
        detail: str = "",
        source: str = "live",
        replay_steps: int = 0,
        product_name: str = "",
    ) -> dict[str, int]:
        """Commit a page's reviews and its resume checkpoint in one transaction."""
        self._require_writable()
        normalized_asin = _asin(asin)
        normalized_source = _source(source)
        if isinstance(pages_completed, bool) or not isinstance(pages_completed, int) or pages_completed < 0:
            raise ValueError("pages_completed must be a nonnegative integer")
        if isinstance(replay_steps, bool) or not isinstance(replay_steps, int) or replay_steps < 0:
            raise ValueError("replay_steps must be a nonnegative integer")
        if not isinstance(status, str) or status not in CHECKPOINT_STATUSES:
            raise ValueError(f"unsupported checkpoint status: {status!r}")
        normalized_detail = _text(detail, "detail")
        normalized_url = None if next_url in (None, "") else _amazon_url(next_url, normalized_asin, "next_url")

        def checked_reviews() -> Iterable[dict[str, str]]:
            for row in reviews:
                normalized = _review(row)
                if normalized["asin"] != normalized_asin:
                    raise ValueError("all page reviews must match the checkpoint ASIN")
                yield normalized

        with self._connection:
            counts = self._insert(checked_reviews(), normalized_source)
            if product_name:
                self._set_product_name(normalized_asin, product_name, "amazon")
            self._connection.execute(
                """
                INSERT INTO checkpoints (asin, next_url, pages_completed, status, detail, replay_steps)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (asin) DO UPDATE SET
                    next_url = excluded.next_url,
                    pages_completed = excluded.pages_completed,
                    status = excluded.status,
                    detail = excluded.detail,
                    replay_steps = excluded.replay_steps,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (normalized_asin, normalized_url, pages_completed, status, normalized_detail, replay_steps),
            )
            return counts

    def get_checkpoint(self, asin: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM checkpoints WHERE asin = ?", (_asin(asin),)
        ).fetchone()
        return self._checkpoint(row) if row is not None else None

    def list_checkpoints(self) -> list[dict[str, Any]]:
        return [self._checkpoint(row) for row in self._connection.execute("SELECT * FROM checkpoints ORDER BY asin")]

    @staticmethod
    def _checkpoint(row: sqlite3.Row) -> dict[str, Any]:
        checkpoint = dict(row)
        checkpoint.setdefault("replay_steps", 0)
        return checkpoint

    def _require_writable(self) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("Review database is open read-only; writes are disabled.")

    def export_csv(self, path: str | os.PathLike[str]) -> int:
        """Replace a CSV atomically, retaining the existing CSV if export fails."""
        output = Path(path)
        if self.path != ":memory:" and output.resolve() == Path(self.path).resolve():
            raise ValueError("CSV output cannot replace the SQLite database")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", newline="", dir=output.parent,
                prefix=f".{output.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                writer.writeheader()
                count = 0
                query = f"SELECT {', '.join(REVIEW_FIELDS)} FROM reviews ORDER BY asin, review_id"
                for row in self._connection.execute(query):
                    writer.writerow(dict(row))
                    count += 1
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output)
            temporary = None
            return count
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def summary(self) -> dict[str, Any]:
        rows = self._connection.execute(
            """
            SELECT asin, COUNT(*) AS review_count,
                   AVG(CASE WHEN rating != '' THEN CAST(rating AS REAL) END) AS average_rating
            FROM reviews GROUP BY asin ORDER BY asin
            """
        )
        products = [dict(row) for row in rows]
        return {"total_reviews": sum(product["review_count"] for product in products), "products": products}
