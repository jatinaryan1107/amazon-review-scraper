# Amazon review research project

This project collects product reviews from Amazon India and stores them for later research. The current version includes Reviewdesk, a local interface for exploring reviews, alongside SQLite storage, duplicate prevention, resumable collection, and an offline demonstration. **AI analysis and model-directed decisions are not implemented yet.**

## Start with the offline demonstration

Use Python 3.10 or newer. The offline commands use the Python standard library and make no network requests. You do not need Playwright, an Amazon account, or an AI API key.

Open a terminal in this folder and run the commands below. Paths such as `data/demo.sqlite3` are relative to this folder.

```powershell
python offline.py demo --db data/demo.sqlite3 --output data/demo_reviews.csv
python offline.py status --db data/demo.sqlite3
python offline.py demo --db data/demo.sqlite3
```

The first command loads 15 **wholly fictional reviews** across three fictional desk-fan products and exports a CSV. These are synthetic examples, not observations about real Amazon products. The second command shows stored counts; the third inserts zero new reviews because their stable IDs are already stored. `examples/demo_reviews.csv` labels every review as fictional. Its Amazon-shaped URLs use fictional ASINs as placeholders for the required CSV format; the offline commands never visit them.

Keep demo data in `data/demo.sqlite3` and live data in a separate database so later reports cannot mix them accidentally. SQLite is a database saved in one local file: it lets the project remember reviews and progress after Python exits.

## Import, export, and inspect

```powershell
python offline.py import examples/demo_reviews.csv --db data/imported.sqlite3
python offline.py export data/exported_reviews.csv --db data/imported.sqlite3
python offline.py status --db data/imported.sqlite3
```

CSV input needs these column names:

```text
asin,product_url,review_id,reviewer_name,verified_purchase,review_title,rating,review_date,review_body
```

An ASIN is Amazon's ten-character product identifier. `review_id` identifies one review. The database prevents duplicates using the product and review identity; repeated imports are safe. An invalid import fails as one transaction, so earlier rows from that import are rolled back. A missing input file or invalid CSV header is rejected before creating the database. `status` and `export` open the database in read-only mode and report an error if it does not exist.

The CSV export contains review fields; database-only metadata and collection checkpoints remain in SQLite. Checkpoint counts, statuses, and details appear in the status output.

## Reviewdesk interface

Run the local workspace with Python 3.10 or newer:

```powershell
python dashboard.py
```

Open `http://127.0.0.1:8765` in your browser. The interface uses local assets and the Python standard library. It does not need Node, an AI API key, or an Amazon login.

- **Overview** shows review counts, rating distribution, and product comparisons from the selected dataset.
- **Reviews** supports text search, product and rating filters, verified-purchase filters, sorting, and full review details.
- **Products** compares the products in the dataset and opens their reviews.
- **Collection** shows saved progress and explains incomplete or blocked collection states.

The demo workspace contains fictional reviews. Your reviews use `data/reviews.sqlite3`, the same database as the collection command. Importing a CSV keeps duplicate protection and rejects invalid batches without saving partial rows. Demo and personal data stay in separate databases.

Downloads from the interface are spreadsheet-safe CSVs. Cells that could be interpreted as formulas receive a leading apostrophe. The stored text is unchanged; the command-line export retains the original field values for exact data processing.

Use `--port` to change the port or `--data-dir` to select a different local data folder. The server listens only on this computer. Live collection remains a separate command, and AI analysis is not implemented yet.

## Live collection

Live collection uses Playwright to control Chromium. Only use it when you intend to access Amazon; the commands below are not part of the offline demo.

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python scraper.py --auth
python scraper.py --scrape --db data/reviews.sqlite3 --input input_urls.csv --pages 3 --resume --output data/reviews.csv
```

Authentication opens a browser for you to sign in manually and saves the session in `auth.json`. Treat that file as a private login session. The sample `input_urls.csv` contains product links, one per line. Scraping stores review rows and collection progress in SQLite; `--output` also exports the database as CSV.

- `--pages 3` gives each product a maximum of three newly processed page snapshots **during this invocation**. A later run can continue from its saved checkpoint. Restoring a saved position may also revisit earlier pages, with a separate default limit of 20 replay clicks. `--replay-limit N` changes that limit; a paused checkpoint explains if it needs a larger value.
- `--resume` continues saved progress and skips products already marked completed.
- Without `--resume`, collection starts at the beginning; persistent duplicate prevention still applies.
- `--restart` resets the pagination checkpoint and starts again. It retains stored reviews. Use either `--restart` or `--resume`; these options are mutually exclusive.
- `--until-end` is accepted for compatibility; the `--pages` budget still applies.

Amazon can change page layouts, restrict access, ask for login, or show a CAPTCHA. Collection records failures and blocked states. A login screen or CAPTCHA stops the entire invocation; the collector does not solve CAPTCHAs. A completed traversal means the collector found no next page under the current layout. **It does not guarantee that every review was collected.** Always interpret findings using the actual sample size and collection status.

## Moving from the original CSV scraper

If you already have a CSV from the original scraper, import it before collecting more reviews:

```powershell
python offline.py import reviews.csv --db data/reviews.sqlite3
python offline.py status --db data/reviews.sqlite3
```

Use the path to your existing CSV in place of `reviews.csv`. The nine review columns are unchanged. Reviews without an ID receive a stable fingerprint from their contents. Stored rows are deduplicated by ASIN and review ID, so importing the same file again does not add another copy.

The original collector appended directly to CSV. Collection now writes to `data/reviews.sqlite3` by default; `--db` selects a different file. `--output` is optional and writes a fresh CSV export of the stored reviews instead of appending rows. Use a separate output filename to keep an existing CSV unchanged. The `--auth` command still opens the browser for manual sign-in.

## How this becomes an agentic AI project

The scraper is a tool with fixed rules. SQLite provides persistent memory. A later AI agent can choose tools to search stored reviews, examine themes, request more evidence within limits, and create a report. Supporting review IDs will let a person check each claim. Future evaluation should check evidence quality as well as whether the code ran successfully.

The planned progression is:

1. **Current:** dependable storage, bounded collection, resumability, an offline demo, and the Reviewdesk interface.
2. **Next:** structured AI analysis with supporting review IDs and counts calculated by code.
3. **Later:** one agent that selects tools, checks evidence, and stops within explicit step and spending limits.

Review text remains input data. Future AI tools must not treat instructions embedded in a review as authority to act.

## Validation

```powershell
python -m unittest discover -s tests -v
```

Validation uses local data, a fake browser for the collection controller, local HTML fixtures for the parser, and a loopback server for the interface API. It never executes the live `scraper.py` entry point or contacts Amazon. Passing these tests verifies local behavior; live pagination, saved-session validity, and compatibility with Amazon's current layout still require a separate, intentional live check.

GitHub Actions runs the offline suite and a synthetic demo on Linux and Windows with Python 3.10 and 3.13. It does not install Playwright, open a browser, or need account secrets.
