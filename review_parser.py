"""Turn saved Amazon review HTML into plain records, without opening a browser.

Keeping extraction separate from browser navigation lets us test it with fictional
HTML. These functions do not make network requests or import Playwright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from review_store import make_review_id


_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_BLOCK_TAGS = {
    "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "li", "p", "section", "table", "td", "th", "tr",
}
_HIDDEN_TAGS = {"script", "style", "template", "noscript"}


def _collapse(text: str) -> str:
    """Replace runs of whitespace (including HTML nonbreaking spaces) with spaces."""
    return " ".join(text.split())


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self) -> str:
        if self.tag in _HIDDEN_TAGS:
            return ""
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                value = child.text()
                # A line break or block boundary must not join neighboring words.
                parts.append(f" {value} " if child.tag in _BLOCK_TAGS else value)
        return "".join(parts)


class _TreeParser(HTMLParser):
    """A small tolerant HTML tree; enough for the review attributes we inspect."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(
            _Node(tag, {name: value or "" for name, value in attrs})
        )

    def handle_endtag(self, tag):
        # Tolerate unmatched closing tags without dropping the whole page.
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _tree(html: str) -> _Node:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _hook(node: _Node, *names: str) -> _Node | None:
    return next(
        (child for child in node.walk() if child.attrs.get("data-hook") in names),
        None,
    )


def _class(node: _Node, name: str) -> _Node | None:
    return next(
        (child for child in node.walk()
         if name in child.attrs.get("class", "").split()),
        None,
    )


def _value(node: _Node | None) -> str:
    return _collapse(node.text()) if node else ""


def extract_asin(url: str) -> str | None:
    """Read a ten-character ASIN from an HTTPS Amazon.in product/review URL.

    Only the exact amazon.in and www.amazon.in hosts are accepted. Credentials,
    explicit ports, lookalike hosts and loose ten-character text are rejected.
    A product's optional readable slug before ``/dp/`` is supported.
    """
    if (not isinstance(url, str) or not url
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https":
        return None
    if parsed.netloc.lower() not in {"amazon.in", "www.amazon.in"}:
        return None
    match = re.match(
        r"^/(?:[^/]+/)?(?:dp|product-reviews)/([A-Z0-9]{10})(?:/|$)",
        parsed.path,
        flags=re.IGNORECASE | re.ASCII,
    )
    return match.group(1).upper() if match else None


def parse_reviews(html: str, asin: str, product_url: str) -> list[dict[str, str]]:
    """Extract the nine CSV fields from review containers in supplied HTML.

    Missing review IDs become a stable SHA-256 fingerprint of the cleaned
    reviewer, date, title and body. Review text stays plain data throughout.
    """
    records = []
    for review in _tree(html).walk():
        if review.attrs.get("data-hook") != "review":
            continue
        reviewer = _value(_class(review, "a-profile-name")) or "Anonymous"
        title = _value(_hook(review, "review-title", "reviewTitle"))
        title = _collapse(re.sub(
            r"\d+(?:\.\d+)?\s*out of\s*\d+\s*stars", "", title,
            flags=re.IGNORECASE,
        ))
        rating_node = (
            _hook(review, "review-star-rating")
            or _hook(review, "cmps-review-star-rating")
            or _class(review, "review-rating")
        )
        rating_text = _value(rating_node)
        rating_match = re.search(r"\d+(?:\.\d+)?", rating_text)
        rating = rating_match.group(0) if rating_match else rating_text
        date = _value(_hook(review, "review-date"))
        date = re.sub(r"^Reviewed\b.*?\bon\s+", "", date, flags=re.IGNORECASE)
        body = _value(_hook(review, "review-body", "reviewText"))
        raw_id = (
            review.attrs.get("id", "").strip()
            or review.attrs.get("data-review-id", "").strip()
        )
        review_id = raw_id or make_review_id(reviewer, date, title, body)
        records.append({
            "asin": asin,
            "product_url": product_url,
            "review_id": review_id,
            "reviewer_name": reviewer,
            "verified_purchase": "True" if _hook(review, "avp-badge") else "False",
            "review_title": title,
            "rating": rating,
            "review_date": date,
            "review_body": body,
        })
    return records


def detect_page_state(html: str, title: str = "") -> tuple[str, str]:
    """Distinguish usable reviews, access blocks, explicit emptiness and unknown HTML.

    An unrecognized page is never treated as proof that a product has no reviews.
    A caller may provide the browser title; otherwise the HTML title is used.
    """
    root = _tree(html)
    nodes = list(root.walk())
    html_title = next((node for node in nodes if node.tag == "title"), None)
    page_title = (title or _value(html_title)).lower()
    input_ids = {node.attrs.get("id") for node in nodes if node.tag == "input"}
    if "ap_email" in input_ids or "sign-in" in page_title or "sign in" in page_title:
        return "login", "Amazon Sign-In Page"
    if ("captchacharacters" in input_ids or "robot check" in page_title
            or "captcha" in page_title):
        return "captcha", "Amazon CAPTCHA / Robot Check Page"
    count = sum(node.attrs.get("data-hook") == "review" for node in nodes)
    if count:
        return "success", f"Loaded page containing {count} reviews"
    if (_hook(root, "no-reviews-section")
            or "no customer reviews" in _collapse(root.text()).lower()):
        return "zero_reviews", "Product has zero customer reviews"
    return "unknown", "Empty or unknown page layout"
