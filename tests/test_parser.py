"""Offline parser checks using fictional HTML only; no browser or network."""

import hashlib
import json
from pathlib import Path
import unittest

from review_parser import detect_page_state, extract_asin, parse_reviews


ASIN = "B0EXAMPLE1"
PRODUCT_URL = f"https://www.amazon.in/dp/{ASIN}"
FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "review_page.html"


class ReviewParserTests(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text(encoding="utf-8")

    def test_normal_review_preserves_nine_fields_and_cleans_nested_text(self):
        reviews = parse_reviews(self.html, ASIN, PRODUCT_URL)
        self.assertEqual(len(reviews), 3)
        self.assertEqual(reviews[0], {
            "asin": ASIN,
            "product_url": PRODUCT_URL,
            "review_id": "customer_review-FICTIONAL001",
            "reviewer_name": "Example Reviewer A",
            "verified_purchase": "True",
            "review_title": "Bright & easy to carry",
            "rating": "5.0",
            "review_date": "10 October 2026",
            "review_body": "The small lamp fits my bag. Battery lasts all evening.",
        })

    def test_alternative_hooks_and_class_rating(self):
        reviews = parse_reviews(self.html, ASIN, PRODUCT_URL)
        self.assertEqual(reviews[1]["review_id"], "FICTIONAL002")
        self.assertEqual(reviews[1]["review_title"], "Good light, loose stand")
        self.assertEqual(reviews[1]["review_body"], "The light is clear. The stand feels loose.")
        self.assertEqual(reviews[1]["rating"], "3.0")
        self.assertEqual(reviews[1]["verified_purchase"], "False")
        self.assertEqual(reviews[2]["rating"], "4.0")

    def test_missing_id_uses_stable_cleaned_fingerprint(self):
        record = parse_reviews(self.html, ASIN, PRODUCT_URL)[2]
        fingerprint = json.dumps(
            ["Example Reviewer C", "12 October 2026", "Useful for a desk",
             "Easy controls & a comfortable warm light."],
            ensure_ascii=False, separators=(",", ":"),
        )
        expected = "sha256:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        self.assertEqual(record["review_id"], expected)
        # Whitespace changes and a different input URL do not change identity.
        altered = self.html.replace("Easy controls", "Easy\n  controls")
        again = parse_reviews(altered, ASIN, PRODUCT_URL + "?ref=test")[2]
        self.assertEqual(again["review_id"], expected)

    def test_missing_fields_and_non_english_date_are_preserved(self):
        html = '<div data-hook="review"><span data-hook="review-date">12 Oktober 2026</span></div>'
        record = parse_reviews(html, ASIN, PRODUCT_URL)[0]
        self.assertEqual(record["reviewer_name"], "Anonymous")
        self.assertEqual(record["review_date"], "12 Oktober 2026")
        self.assertEqual(record["review_body"], "")
        self.assertEqual(record["rating"], "")
        self.assertTrue(record["review_id"].startswith("sha256:"))
        self.assertEqual(len(record["review_id"]), 71)
        self.assertEqual(parse_reviews("<p>No review containers</p>", ASIN, PRODUCT_URL), [])

    def test_detect_success_and_access_blocks(self):
        self.assertEqual(detect_page_state(self.html)[0], "success")
        self.assertEqual(detect_page_state(self.html)[1], "Loaded page containing 3 reviews")
        for html, title, state in [
            ('<input id="ap_email">', "", "login"),
            ("", "Amazon Sign-In", "login"),
            ("<title>Sign In</title>", "", "login"),
            ('<input id="captchacharacters">', "", "captcha"),
            ("", "Robot Check", "captcha"),
            ("<title>CAPTCHA</title>", "", "captcha"),
        ]:
            with self.subTest(html=html, title=title):
                self.assertEqual(detect_page_state(html, title)[0], state)
        # A login page that also contains review markup is still a login block.
        self.assertEqual(detect_page_state('<input id="ap_email">' + self.html)[0], "login")

    def test_explicit_zero_is_distinct_from_unknown(self):
        self.assertEqual(detect_page_state("<p>No customer reviews</p>")[0], "zero_reviews")
        self.assertEqual(detect_page_state('<div data-hook="no-reviews-section"></div>')[0], "zero_reviews")
        self.assertEqual(detect_page_state("<h1>Something went wrong</h1>")[0], "unknown")
        self.assertEqual(detect_page_state("")[0], "unknown")
        self.assertEqual(detect_page_state('<script>"no customer reviews"</script>')[0], "unknown")


class AsinTests(unittest.TestCase):
    def test_portal_customer_review_pagination_route(self):
        actual_route = "https://www.amazon.in/portal/customer-reviews/B0CGDZC2FK/ref=cm_cr_arp_d_paging_btm_2"
        self.assertEqual(extract_asin(actual_route), "B0CGDZC2FK")
        for suffix in ["", "/", "/ref=cm_cr_arp_d_paging_btm_2", "/ref=cm_cr_arp_d_paging_btm_2/?pageNumber=2"]:
            with self.subTest(suffix=suffix):
                self.assertEqual(extract_asin(f"https://amazon.in/portal/customer-reviews/{ASIN}{suffix}"), ASIN)

    def test_portal_route_keeps_exact_origin_and_path_boundaries(self):
        for url in [
            f"https://amazon.in.evil.example/portal/customer-reviews/{ASIN}",
            f"http://www.amazon.in/portal/customer-reviews/{ASIN}",
            f"https://user:password@www.amazon.in/portal/customer-reviews/{ASIN}",
            f"https://www.amazon.in:443/portal/customer-reviews/{ASIN}",
            f"https://www.amazon.in/prefix/portal/customer-reviews/{ASIN}",
            f"https://www.amazon.in/portal/customer-reviews/{ASIN}EXTRA",
            f"https://www.amazon.in/portal/customer-reviews/{ASIN}/extra",
            f"https://www.amazon.in/portal/customer-reviews/{ASIN}/ref=",
            f"https://www.amazon.in/portal/customer-reviews/{ASIN}/ref=valid/extra",
            f"https://www.amazon.in/portal/customer-reviews/{ASIN}/ref=bad%2Fextra",
            f"https://www.amazon.in/portal%2Fcustomer-reviews/{ASIN}",
            f"https://www.amazon.in/portal/customer-reviews/{ASIN}/dp/{ASIN}",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(extract_asin(url))

    def test_supported_amazon_in_paths(self):
        for url in [
            PRODUCT_URL,
            f"https://amazon.in/product-reviews/{ASIN}/ref=test?pageNumber=2",
            f"https://www.amazon.in/Fictional-Pocket-Lamp/dp/{ASIN.lower()}/",
            f"HTTPS://WWW.AMAZON.IN/dp/{ASIN}?ref=test#reviews",
        ]:
            with self.subTest(url=url):
                self.assertEqual(extract_asin(url), ASIN)

    def test_rejects_untrusted_hosts_schemes_credentials_and_malformed_paths(self):
        for url in [
            f"http://www.amazon.in/dp/{ASIN}",
            f"https://amazon.com/dp/{ASIN}",
            f"https://amazon.in.evil.example/dp/{ASIN}",
            f"https://evilamazon.in/dp/{ASIN}",
            f"https://www.amazon.in@evil.example/dp/{ASIN}",
            f"https://user:password@www.amazon.in/dp/{ASIN}",
            f"https://www.amazon.in:443/dp/{ASIN}",
            f"https://www.amazon.in/search?q={ASIN}",
            f"https://www.amazon.in/{ASIN}",
            f"https://www.amazon.in/dp/{ASIN}EXTRA",
            "https://www.amazon.in/dp/B0123",
            "https://www.amazon.in/dp/B0EXAMPLE!",
            "https://www.amazon.in/dp/B0EXAMPLE\u0131",
            f"https://www.amazon.in/dp/{ASIN} ",
            f"\x00https://www.amazon.in/dp/{ASIN}",
            f"//www.amazon.in/dp/{ASIN}",
            "https://[invalid/dp/B0EXAMPLE1",
            "",
            None,
        ]:
            with self.subTest(url=url):
                self.assertIsNone(extract_asin(url))


if __name__ == "__main__":
    unittest.main()
