# Amazon India Review Scraper

A browser-based Amazon India review scraper that collects structured written reviews from selected product URLs and exports them to CSV for consumer pain-point analysis, category insights, and pricing simulation.

## What the Scraper Does

This scraper:

- reads product URLs from `input_urls.csv`
- opens Amazon product review pages in a browser session
- scrapes written review text and metadata
- extracts ASIN, product URL, review ID, reviewer name, review title, review body, rating, review date and verified purchase status
- deduplicates reviews by review ID
- exports clean review rows to CSV
- supports multi-product scraping
- uses browser session persistence via `auth.json` when required

## Repository Structure

- `scraper.py` — main scraper script
- `input_urls.csv` — product URLs to process
- `requirements.txt` — Python dependencies
- `README.md` — project documentation

Generated files such as review CSVs, `auth.json`, debug HTML, and Python cache files should not be committed.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Install Playwright browsers if needed:

```bash
playwright install
```

Verify the scraper compiles:

```bash
python -m py_compile scraper.py
```

## Input Format

Add one Amazon product URL per line in `input_urls.csv`. A header row is optional, but if used it should be `url`.

Example:

```csv
url
https://www.amazon.in/dp/B0DFHKJRVH
https://www.amazon.in/dp/B0G5PYK44L
https://www.amazon.in/dp/B0CC99CQ4F
```

## Authentication / Session Flow

Amazon may require a logged-in session to access review pages reliably. The scraper supports a saved browser session in `auth.json`.

### First-time session setup

Run the interactive authentication command:

```bash
python scraper.py --auth
```

This opens a visible browser. Complete Amazon login and any verification steps manually. Once authenticated, the session is saved locally to `auth.json`.

### Normal scraping after auth is saved

After `auth.json` exists, run the scraper normally:

```bash
python scraper.py --scrape --pages 10 --output reviews_output.csv
```

## Example Commands

```bash
python scraper.py --scrape --pages 10 --output category_a_reviews.csv
python scraper.py --scrape --pages 10 --output category_a_validation.csv
python scraper.py --scrape --pages 10 --output category_b_reviews.csv
python scraper.py --scrape --pages 10 --output category_b_validation.csv
```

## Checking Output

Count total rows:

```powershell
Import-Csv reviews_output.csv | Measure-Object
```

Count reviews by ASIN:

```powershell
Import-Csv reviews_output.csv | Group-Object asin | Sort-Object Count -Descending
```

Preview titles and review body text:

```powershell
Import-Csv reviews_output.csv | Select-Object asin, review_title, review_body | Format-Table -Wrap
```

## Output Columns

The scraper writes the following CSV columns:

- `asin`
- `product_url`
- `review_id`
- `reviewer_name`
- `review_title`
- `review_body`
- `rating`
- `review_date`
- `verified_purchase`

## GitHub Safety

Recommended `.gitignore` entries:

```gitignore
auth.json
*.csv
debug_review_*.html
__pycache__/
*.pyc
.env
```

Do not push `auth.json` because it contains local browser session state and may expose session credentials or tokens.

## Review Analysis Workflow

A typical review analysis process:

1. Filter products by category from the source data.
2. Sort products by monthly revenue.
3. Avoid duplicate variants and clusters.
4. Select representative high-revenue products.
5. Add validation products.
6. Scrape written reviews.
7. Group reviews by ASIN and by category.
8. Split reviews by rating sentiment:
   - 4-5 star positive
   - 3 star neutral
   - 1-2 star negative
9. Identify recurring themes:
   - purchase drivers
   - positive features
   - pain points
   - value-for-money comments
   - expectation mismatches
10. Use AI-assisted theme clustering.
11. Validate themes against actual review samples.
12. Convert patterns into category-level insights.

## Data Validation Note

A source sheet's `Reviews` column often reflects total rating count rather than actual written review count. Written-review analysis should therefore be based on scraped review text rather than relying directly on that field.

## Market Price Simulator

A separate category-level market price simulator can be built from:

- product price
- monthly sales
- monthly revenue
- category
- brand
- price bands

It should be interpreted as a category-level market signal simulator, not a SKU-level demand forecasting model.

## Limitations

- Amazon page structure can change.
- Amazon may show different reviews based on session, region, address or login state.
- This is a browser-based scraper, not an official API.
- Written review count may differ from rating count.
- The scraper covers selected representative products, not every SKU.
- The pricing simulator is directional, not causal.

## Future Improvements

- star-rating filters
- keyword-based review filters
- sort controls for recent, critical, or helpful reviews
- automatic theme frequency counts
- a review pain-point dashboard
- more categories
- SKU-level demand modelling with historical data
- an interactive simulator dashboard

## Summary

This project combines a repeatable Amazon review scraper, a review analysis workflow, and a category-level price simulation approach to identify customer pain points, product strengths, and pricing opportunities.
