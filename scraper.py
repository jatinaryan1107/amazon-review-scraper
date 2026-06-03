import os
import re
import csv
import sys
import time
import random
import argparse
import hashlib
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright



def extract_asin(url):
    asin_match = re.search(r'/(?:dp|product-reviews)/([A-Z0-9]{10})', url, re.IGNORECASE)
    if asin_match:
        return asin_match.group(1).upper()
    fallback_match = re.search(r'\b([A-Z0-9]{10})\b', url, re.IGNORECASE)
    if fallback_match:
        return fallback_match.group(1).upper()
    return None


def clean_date(date_text):
    if not date_text:
        return ""
    if "on " in date_text:
        return date_text.split("on ", 1)[1].strip()
    return date_text


def clean_rating(rating_text):
    if not rating_text:
        return ""
    match = re.search(r'([0-9.]+)', rating_text)
    if match:
        return match.group(1)
    return rating_text


def detect_page_state(page):
    try:
        title = page.title().lower()
    except Exception:
        title = ""
    if page.query_selector("input#ap_email") or "sign-in" in title or "sign in" in title:
        return "login", "Amazon Sign-In Page"
    if page.query_selector("input#captchacharacters") or "robot check" in title or "captcha" in title:
        return "captcha", "Amazon CAPTCHA / Robot Check Page"
    reviews = page.query_selector_all('[data-hook="review"]')
    if len(reviews) > 0:
        return "success", f"Loaded page containing {len(reviews)} reviews"
    try:
        content = page.content().lower()
    except Exception:
        content = ""
    if "no customer reviews" in content or page.query_selector('[data-hook="no-reviews-section"]'):
        return "zero_reviews", "Product has zero customer reviews"
    return "unknown", "Empty or unknown page layout"


def find_next_page_button(page):
    try:
        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        page.wait_for_timeout(1500)
    except Exception:
        pass
    try:
        locator = page.locator('a[data-hook="show-more-button"]')
        if locator.count() > 0:
            return locator.first
    except Exception:
        pass
    return None


def extract_reviews_from_elements(review_elements, asin, product_url):
    extracted = []
    global _SAVED_REVIEW_HTML
    for el in review_elements:
        try:
            try:
                raw_id = el.get_attribute('id') or el.get_attribute('data-review-id')
            except Exception:
                raw_id = None
            reviewer_node = el.query_selector('span.a-profile-name')
            reviewer_name = reviewer_node.inner_text().strip() if reviewer_node else "Anonymous"
            vp_node = el.query_selector('[data-hook="avp-badge"]')
            verified_purchase = "True" if vp_node else "False"
            # Robust title extraction: prefer the data-hook target and use inner_text
            title_node = el.query_selector('[data-hook="review-title"], [data-hook="reviewTitle"]')
            review_title = ""
            if title_node:
                try:
                    raw_title = title_node.inner_text().strip()
                except Exception:
                    raw_title = title_node.evaluate('node => node.innerText').strip()
                # Remove rating substrings (e.g. '5.0 out of 5 stars'), then split and join remaining non-empty lines
                cleaned = re.sub(r"\d+(?:\.\d+)?\s*out of\s*\d+\s*stars", "", raw_title, flags=re.IGNORECASE)
                parts = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
                if parts:
                    review_title = " ".join(parts).strip()
            rating_node = el.query_selector('[data-hook="review-star-rating"]')
            if not rating_node:
                rating_node = el.query_selector('[data-hook="cmps-review-star-rating"]')
            if not rating_node:
                rating_node = el.query_selector('.review-rating')
            rating_val = ""
            if rating_node:
                rating_text = rating_node.inner_text().strip()
                rating_val = clean_rating(rating_text)
            date_node = el.query_selector('[data-hook="review-date"]')
            review_date = ""
            if date_node:
                review_date = clean_date(date_node.inner_text().strip())
            body_node = el.query_selector('[data-hook="review-body"], [data-hook="reviewText"]')
            review_body = ""
            if body_node:
                body_span = body_node.query_selector('span')
                if body_span:
                    review_body = body_span.inner_text().strip()
                else:
                    review_body = body_node.inner_text().strip()
            review_body = review_body.replace('\n', ' ').strip()
            if raw_id:
                review_id = raw_id
            else:
                fp = f"{reviewer_name}|{review_date}|{review_title}|{review_body}"
                review_id = hashlib.sha256(fp.encode('utf-8')).hexdigest()
            extracted.append({
                "asin": asin,
                "product_url": product_url,
                "review_id": review_id,
                "reviewer_name": reviewer_name,
                "verified_purchase": verified_purchase,
                "review_title": review_title,
                "rating": rating_val,
                "review_date": review_date,
                "review_body": review_body
            })
        except Exception as ex:
            print(f"  [Warning] Failed parsing individual review element: {ex}")
    return extracted


def write_to_csv(output_csv_path, reviews, headers):
    file_exists = os.path.isfile(output_csv_path)
    write_header = not file_exists or os.path.getsize(output_csv_path) == 0
    with open(output_csv_path, mode='a', newline='', encoding='utf-8') as out_f:
        writer = csv.DictWriter(out_f, fieldnames=headers)
        if write_header:
            writer.writeheader()
        for row in reviews:
            writer.writerow(row)


def run_auth_mode():
    auth_file = "auth.json"
    print("\n==================================================")
    print("      AMAZON.IN SCRAPER - AUTHENTICATION MODE     ")
    print("==================================================")
    print(f"This mode will open a visible browser window.")
    print(f"Please sign in manually. Once logged in, your session")
    print(f"will be saved to '{auth_file}' for headless scraping.")
    print("==================================================")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-IN")
        page = context.new_page()
        print("Navigating to Amazon.in...")
        page.goto("https://www.amazon.in/", wait_until="domcontentloaded")
        print("\n[USER ACTION REQUIRED]")
        print("1. Go to the browser window that just opened.")
        print("2. Click 'Sign in' and log into your account manually.")
        print("3. Solve any CAPTCHAs that appear.")
        print("4. Once you are successfully signed in and see your Amazon home page,")
        input("   return to this terminal and press [Enter] to save the session...")
        context.storage_state(path=auth_file)
        browser.close()
    print(f"\n[Success] Session successfully saved to '{auth_file}'!")
    print("You can now run the scraper in scrape mode: python scraper.py --scrape")


def run_scrape_mode(input_csv_path, output_csv_path, max_pages, headful_mode, until_end=False):
    headers = [
        "asin",
        "product_url",
        "review_id",
        "reviewer_name",
        "verified_purchase",
        "review_title",
        "rating",
        "review_date",
        "review_body",
    ]
    auth_file = "auth.json"
    if not os.path.isfile(input_csv_path):
        print(f"[Error] Input file '{input_csv_path}' not found. Please create it first.")
        sys.exit(1)
    urls = []
    with open(input_csv_path, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if 'url' not in reader.fieldnames:
            f.seek(0)
            urls = [line.strip() for line in f if line.strip() and not line.startswith('url')]
        else:
            urls = [row['url'].strip() for row in reader if row.get('url')]
    if not urls:
        print(f"[Info] No URLs found in '{input_csv_path}'. Please add some URLs to scrape.")
        return
    print(f"[Info] Loaded {len(urls)} URL(s) to process.")
    has_auth = os.path.isfile(auth_file)
    if has_auth:
        print(f"[Info] Found existing session file '{auth_file}'. Loading cookies...")
    else:
        print(f"[Warning] No session file '{auth_file}' found. Scraping anonymously...")
    with sync_playwright() as p:
        print(f"[Info] Launching browser (Headless: {not headful_mode})...")
        browser = p.chromium.launch(headless=not headful_mode, args=["--disable-blink-features=AutomationControlled"])
        context_args = {
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-IN",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        }
        if has_auth:
            context_args["storage_state"] = auth_file
        context = browser.new_context(**context_args)
        page = context.new_page()
        for idx, url in enumerate(urls, start=1):
            asin = extract_asin(url)
            if not asin:
                print(f"\n[{idx}/{len(urls)}] [Warning] Skipping invalid URL (could not parse ASIN): {url}")
                continue
            product_url = f"https://www.amazon.in/dp/{asin}"
            print(f"\n[{idx}/{len(urls)}] Processing ASIN: {asin}")
            print(f"Product URL: {product_url}")
            fallback_triggered = False
            seen_ids = set()
            cumulative_unique = 0
            page_url = f"https://www.amazon.in/product-reviews/{asin}/ref=cm_cr_dp_d_show_all_top?_encoding=UTF8&ie=UTF8&reviewerType=all_reviews"
            pagination_mode = "until-end" if until_end else f"max-pages({max_pages})"
            print(f"  Pagination mode: {pagination_mode}")
            page_num = 0
            consecutive_no_new = 0
            while True:
                page_num += 1
                if not until_end and page_num > max_pages:
                    print(f"  [Info] Reached max pages limit ({max_pages}). Ending pagination.")
                    break
                if until_end and page_num > max_pages:
                    print(f"  [Info] Reached safety cap (max {max_pages} pages in until-end mode). Ending pagination.")
                    break
                print(f"  [Load {page_num}] Scraping reviews from: {page_url}")
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(2.0, 4.5))
                    state, desc = detect_page_state(page)
                    print(f"  Page State Detected: {state.upper()} ({desc})")
                    if state in ("login", "captcha"):
                        print(f"  [Block Detected] Blocked by Amazon ({state}) on reviews page {page_num}.")
                        if page_num == 1:
                            print(f"  [Fallback] Triggering product page fallback to get visible reviews...")
                            fallback_triggered = True
                        break
                    elif state == "zero_reviews":
                        print(f"  [Info] Product has zero reviews. Ending pagination.")
                        break
                    elif state == "success":
                        review_elements = page.query_selector_all('[data-hook="review"]')
                        reviews = extract_reviews_from_elements(review_elements, asin, product_url)
                        before_count = len(reviews)
                        new_reviews = []
                        skipped = 0
                        for r in reviews:
                            rid = r.get('review_id')
                            if rid in seen_ids:
                                skipped += 1
                                continue
                            seen_ids.add(rid)
                            new_reviews.append(r)
                        write_to_csv(output_csv_path, new_reviews, headers)
                        cumulative_unique = len(seen_ids)
                        print(f"  [Load {page_num} Report] Found: {before_count} | New: {len(new_reviews)} | Skipped duplicates: {skipped} | Cumulative unique: {cumulative_unique}")
                        if len(new_reviews) == 0:
                            consecutive_no_new += 1
                            print(f"  [Info] No new unique reviews in this load (consecutive no-new count: {consecutive_no_new}). Checking for next page...")
                        else:
                            consecutive_no_new = 0
                        if until_end and consecutive_no_new >= 2:
                            print(f"  [Info] Two consecutive loads with no new unique reviews. Stopping pagination.")
                            break
                        next_button = find_next_page_button(page)
                        if next_button:
                            try:
                                next_button.scroll_into_view_if_needed()
                            except Exception:
                                pass
                            try:
                                with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                                    next_button.click()
                            except Exception:
                                try:
                                    next_button.click()
                                    page.wait_for_load_state("networkidle", timeout=30000)
                                except Exception as click_exc2:
                                    print(f"  [Warning] Clicked show-more button, but waiting for load failed: {click_exc2}")
                            page_url = page.url
                        else:
                            print(f"  [Info] No next-page button found. Pagination complete.")
                            break
                    else:
                        print(f"  [Warning] Zero review elements found on page {page_num} without explicit login/captcha block.")
                        if page_num == 1:
                            print(f"  [Fallback] Triggering product page fallback as precaution...")
                            fallback_triggered = True
                        break
                except Exception as e:
                    print(f"  [Error] An exception occurred while scraping load {page_num}: {e}")
                    if page_num == 1:
                        print(f"  [Fallback] Triggering product page fallback due to exception...")
                        fallback_triggered = True
                    break
            if fallback_triggered:
                print(f"  Running Fallback: Navigating to Product Detail Page {product_url}...")
                try:
                    page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(2.0, 4.0))
                    prod_state, prod_desc = detect_page_state(page)
                    print(f"  Product Page State: {prod_state.upper()} ({prod_desc})")
                    if prod_state in ("login", "captcha"):
                        print(f"  [Fallback Blocked] Product detail page also blocked by Amazon ({prod_state}).")
                    elif prod_state == "zero_reviews":
                        print(f"  [Info] Product page confirms zero reviews for ASIN {asin}.")
                    else:
                        review_elements = page.query_selector_all('[data-hook="review"]')
                        if len(review_elements) > 0:
                            reviews = extract_reviews_from_elements(review_elements, asin, product_url)
                            before_count = len(reviews)
                            new_reviews = []
                            skipped = 0
                            for r in reviews:
                                rid = r.get('review_id')
                                if rid in seen_ids:
                                    skipped += 1
                                    continue
                                seen_ids.add(rid)
                                new_reviews.append(r)
                            write_to_csv(output_csv_path, new_reviews, headers)
                            cumulative_unique = len(seen_ids)
                            print(f"  [Fallback Success] Scraped {before_count} reviews from product page; new unique: {len(new_reviews)}; duplicates skipped: {skipped}; cumulative unique: {cumulative_unique}")
                        else:
                            print(f"  [Info] Loaded product page successfully, but no reviews were found in the template.")
                except Exception as fallback_err:
                    print(f"  [Fallback Error] Failed to execute product page fallback: {fallback_err}")
        browser.close()
        print("\n==================================================")
        print("Scraping session finished!")
        print(f"Output saved in '{output_csv_path}'")
        print("==================================================")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Beginner-friendly Amazon.in Review Scraper using Playwright")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--auth", action="store_true", help="Run in interactive mode to log in and save session to auth.json")
    mode_group.add_argument("--scrape", action="store_true", help="Run in scraper mode (default)")
    parser.add_argument("--input", default="input_urls.csv", help="CSV file containing Amazon.in review URLs (default: input_urls.csv)")
    parser.add_argument("--output", default="reviews.csv", help="Output CSV file path (default: reviews.csv)")
    parser.add_argument("--pages", type=int, default=3, help="Max pages of reviews to scrape per product URL (default: 3)")
    parser.add_argument("--headful", action="store_true", help="Run scraper browser in headful mode (visible window)")
    parser.add_argument("--until-end", action="store_true", help="Keep scraping until no new reviews found or max pages reached (safety cap)")
    args = parser.parse_args()
    if not args.auth and not args.scrape:
        args.scrape = True
    if args.auth:
        run_auth_mode()
    else:
        # Require an existing authenticated session before scraping
        if not os.path.isfile('auth.json'):
            print("[Error] No authenticated session found.")
            print("Please run:")
            print("python scraper.py --auth")
            sys.exit(1)
        run_scrape_mode(
            input_csv_path=args.input,
            output_csv_path=args.output,
            max_pages=args.pages,
            headful_mode=args.headful,
            until_end=args.until_end
        )

