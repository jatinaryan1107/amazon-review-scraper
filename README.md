# Amazon review research project

This project collects product reviews from Amazon India and stores them for research in Reviewdesk. You can start a bounded background collection, resume saved progress, and generate reports linked to source reviews. Local analysis uses transparent keyword rules and rating statistics. Optional OpenRouter analysis uses free models, cached batches, and explicit sharing consent. **Ask reviews** adds a bounded agent that can choose local evidence tools when a question needs investigation. The existing optional OpenAI adapter is also available.

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

Open [Reviewdesk](http://127.0.0.1:8765) in your browser. Exploring stored reviews and running local analysis uses local assets and the Python standard library. It does not need Node, an API key, or an Amazon login. Live collection additionally needs Playwright, Chromium, and a manually saved Amazon session.

- **Overview** shows review counts, rating distribution, and product comparisons from the selected dataset.
- **Reviews** supports text search, product and rating filters, verified-purchase filters, sorting, and full review details.
- **Products** compares the products in the dataset and opens their reviews.
- **Collection** starts background jobs, connects a session, reports progress, and resumes saved positions.
- **Analysis** creates scoped reports with exact rating statistics, recurring topics, and links to source reviews.
- **Ask reviews** answers questions about the stored dataset and shows the evidence and tools used.

Use **Settings** to connect an optional OpenRouter key. Saving settings does not make a model request.

The demo workspace contains fictional reviews. Your reviews use `data/reviews.sqlite3`, the same database as the collection command. Importing a CSV keeps duplicate protection and rejects invalid batches without saving partial rows. Demo and personal data stay in separate databases.

Downloads from the interface are spreadsheet-safe CSVs. Cells that could be interpreted as formulas receive a leading apostrophe. The stored text is unchanged; the command-line export retains the original field values for exact data processing.

Use `--port` to change the port, `--data-dir` to select a different local data folder, or `--auth-file` to choose the saved session file. The server listens only on `127.0.0.1` and has no account or deployment system. Run one dashboard per data folder. A process lock prevents two workers from using the same job database.

## Collect and analyse from Reviewdesk

Install live collection support once:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python dashboard.py
```

1. Open **Collection** and choose **Open sign-in**. A separate Amazon browser opens. Sign in there yourself, then choose **Save signed-in session** in Reviewdesk. Credentials never pass through the dashboard form. A saved session is not a guarantee that Amazon will accept it later.
2. Paste one HTTPS Amazon India product URL per line. Start with one product and two or three pages. The interface allows up to ten products, twenty new pages per product, and fifty new pages across a job. Restoring an in-place position has a separate limit of twenty earlier clicks per product.
3. Keep **Continue saved progress** selected to resume unfinished products and skip completed ones. Clearing it starts at the beginning while keeping stored reviews and duplicate prevention.
4. Keep **Analyse after collection** selected for a free local report covering My workspace. Collection always saves to My workspace, even when the demo dataset is selected.
5. Follow **Activity** for real page and review counts. **Stop job** requests a cooperative stop, which can take until the current browser operation finishes. Committed rows and checkpoints remain available.

Login screens, CAPTCHA, and HTTP 403 or 429 stop the batch. Resolve access manually before starting another job. The application does not solve challenges or rotate identities. The sign-in window closes after ten minutes if it is not completed.

Jobs and reports persist in `data/jobs.sqlite3`. A server restart marks unfinished jobs interrupted. It does not silently restart collection or paid analysis. Start a new job with Continue saved progress to resume. Reports remain available after a restart and are marked stale when their source review content changes.

## Larger collection attempts

For an explicitly chosen larger target, keep Reviewdesk running and start the campaign driver in another terminal:

```powershell
python bulk_collect.py reviewed-products.json --target 5000 --budget-minutes 45 --max-pages 1000
```

The UTF-8 JSON manifest contains a `products` array. Each entry needs `asin` and `product_url` (or `url`), with optional `title` and an HTTPS Amazon.in `source_url` for provenance. Supply real product links you have reviewed; the driver does not discover products. It accepts at most 100 distinct products and a target from one to 5,000 distinct live review IDs.

Collection uses the existing local API, browser session, and page limits: at most ten products and five new pages per product in each job. Completed products are skipped, paused products rotate through subsequent jobs, and existing replay limits remain enforced. The target counts review IDs once across the selected products, so reviews shared by colour variants do not inflate it. Reviewdesk's review count can be higher because the database preserves each product's source rows. The target is checked between jobs, so a final job can take the count above the requested number.

The default time budget is 45 minutes, with a maximum of 60. The total new-page budget is at most 1,000; restoring earlier positions can require additional requests. A failed or partial product, an access block, two consecutive batches without new review IDs, or an exhausted budget stops the campaign. Existing failed, partial, or blocked checkpoints require review before another campaign starts. The driver never retries an uncertain job submission. Interrupting it requests cancellation only for its own active job. A restart requires an explicit new invocation.

Progress journals are saved atomically under `data/campaigns/`, which is ignored by Git. Brief Windows file-sharing locks receive at most five file-replacement attempts over 0.75 seconds. Other file errors fail immediately; job submissions are never retried by this mechanism. Journals record the selected products, submitted job IDs, distinct counts, and the reason collection stopped. A 5,000-review target does not establish that Amazon will expose 5,000 written reviews. A large rating count is not a written-review count, and a missing Next control does not establish complete coverage.

Automatic analysis remains the free local method. OpenRouter's broader analysis reuses validated chunks across runs and reports its actual text coverage. The optional OpenAI adapter analyses a disclosed bounded sample. A report should state exactly which texts were analysed and preserve source review IDs.

## What local analysis can tell you

Local analysis calculates averages and rating groups from all stored reviews in the selected scope. Missing ratings remain separate. It scans English keywords for topics such as battery, noise, price, delivery, and support, counts each matching review once per topic, and keeps exact excerpts linked to the original product and review ID. A review may match several topics.

These rules do not understand sarcasm, multilingual text, or context like a language model. A mention of battery life does not establish a battery defect. Star-rating groups describe the review's overall rating, not sentiment about each topic. The interface states the method and sample limits so the results can be checked.

## Optional free OpenRouter analysis

1. Create an API key in [OpenRouter settings](https://openrouter.ai/settings/keys).
2. In Reviewdesk, open **Settings**, enter the key, and save. Enter it in the local application, not a chat or source file. The key remains in server memory for this process and is cleared when the server exits. Alternatively, set `OPENROUTER_API_KEY` in the server's environment before starting it.
3. Open **Analysis**, choose OpenRouter and a scope, and inspect the coverage and call estimate. **Quick** uses a bounded sample. **Broader coverage** processes deterministic chunks and reuses completed chunks on subsequent runs.
4. Allow the selected review text to be sent to OpenRouter, then start the job. Collection's automatic report still runs locally.

The fixed model is [`openrouter/free`](https://openrouter.ai/docs/guides/routing/routers/free-router). It selects an available free model supporting the requested features. The actual model can vary between calls and is recorded with results. The request sets zero price limits and has no paid fallback. If a compatible free model is unavailable, the request stops instead of changing that policy.

OpenRouter currently documents 20 requests per minute and 50 per day for free models on accounts without purchased credits. Purchasing at least $10 in credits raises the documented daily allowance, but Reviewdesk keeps its own conservative 50-attempt daily cap. Failed or uncertain requests can consume quota. The app paces requests, limits each job, and never retries a provider request automatically. Other applications using the same account can reduce the provider allowance independently. Check [current OpenRouter limits](https://openrouter.ai/pricing) before relying on a daily capacity.

Validated analysis chunks and request accounting are stored in `data/analysis.sqlite3`. Keys are never stored there, in job options, in browser storage, or in reports. A cancelled or rate-limited run retains completed chunks; starting the same analysis again can reuse them. A large dataset may need several runs or days. Coverage counts, skipped text, cached chunks, and call usage determine what the report actually examined.

Provider responses are read incrementally. A two-minute response budget prevents a stream of keepalive bytes from keeping a job waiting indefinitely. Cancellation and the deadline are checked between reads; a blocked network operation can take up to its forty-second socket timeout before control returns. Provider requests are not retried automatically.

Requests disable optional model reasoning and ask for compact, evidence-linked answers with a 1,000-token output ceiling. Each report chunk has at most three themes and two short source quotes per theme. Broader analysis still processes the selected text in chunks and combines their validated findings locally. These settings reduce waiting on slow free endpoints; they do not guarantee response speed. A timeout accepts no partial answer and directs you to [OpenRouter Logs](https://openrouter.ai/logs) before retrying. Provider execution can continue after the local connection closes, so a timed-out request may still appear there with output tokens.

Responses must pass local JSON, report structure, source key and exact-quote checks. A single complete JSON markdown fence is accepted as a formatting wrapper; surrounding prose, duplicate JSON fields, invented quotes and unknown sources are rejected. Jobs distinguish these validation failures without exposing raw provider responses or credentials. A successful provider generation is not the same as an accepted report.

Reviewdesk requests providers that do not collect prompts for training and offers an additional zero data retention requirement. These controls can leave no compatible free route. The app does not silently relax them. They are routing requirements, not a promise about every aspect of provider data handling. Remote response caching and automatic context compression are disabled. See [OpenRouter provider controls](https://openrouter.ai/docs/guides/routing/provider-selection).

## Ask questions about your reviews

Choose a dataset and optional product under **Ask reviews**. Recognized count and rating questions use local calculations with zero model calls. Other questions require a configured OpenRouter key and permission to send the selected evidence.

The agent can request local statistics, search stored reviews, look up evidence, and compare products inside the chosen scope. It has at most four model turns and three tool executions. It cannot control the browser, collect more reviews, access session files, run shell commands, or query arbitrary SQL. The interface shows actual research steps and source quotes, rather than simulated agent activity.

Review text and model output are untrusted. Returned evidence must identify supplied reviews and quote their text exactly. A matching quote supports traceability, but it does not prove that the model's interpretation is correct. Reports distinguish stored product-review rows from distinct review IDs because Amazon can share reviews across variants.

When a shared review ID has conflicting ratings, global AI-report statistics exclude its rating rather than choose one arbitrarily. The ID still counts once as a stored review. Fractional ratings from imports remain visible in the local rating distribution. Conflicting text variants, excerpt limits, and incomplete chunk coverage are disclosed separately.

## Optional OpenAI analysis

OpenAI is disabled by default. To enable it, set `OPENAI_API_KEY` and an explicit `OPENAI_MODEL` in the environment of the server process, then restart the dashboard. Keep keys out of source code and CSV files. The application does not load a `.env` file or expose the key through its API.

In **Analysis**, choose a scope, select OpenAI, and explicitly allow the selected review text to be sent. This is a separate manual action; automatic analysis after collection always uses the local method. There is one bounded Responses API request, no automatic retries, no model tools, and a maximum of 3,500 output tokens. The input includes at most 120 reviews and 180,000 characters, with titles and bodies individually capped. Reports disclose skipped reviews and shortened text.

Provider input uses opaque evidence and product keys with text and ratings. The app omits author fields, product URLs, dates, and original review IDs. Review text itself may still contain personal information, so inspect your data before opting in. `store:false` disables Responses application-state storage; provider data-handling terms still apply. Every returned evidence key and quote must match the supplied text, or the report is rejected. Exact statistics always come from local code. A valid quote does not by itself establish that the model's interpretation is correct.

Unchanged content reuses a matching saved report, including the model choice, without another provider request. Changed text, ratings, product scope, or analysis version invalidates that match. Input/output caps bound the request size, not a guaranteed currency amount; actual cost depends on the configured model's [current pricing](https://developers.openai.com/api/docs/pricing). The optional adapter is tested with fake responses; live provider access is a separate check.

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
- `--transport hybrid` enables the experimental HTTP-first path described below. Browser collection is the default.
- `--delay 1` sets the minimum interval between request starts. Time spent waiting for a slow response counts toward the interval. `--full-assets` keeps images, fonts, and media enabled; collection normally skips them while retaining scripts and styles.

Amazon can change page layouts, restrict access, ask for login, or show a CAPTCHA. Collection records failures and blocked states. A login screen or CAPTCHA stops the entire invocation; the collector does not solve CAPTCHAs. A completed traversal means the collector found no next page under the current layout. **It does not guarantee that every review was collected.** Always interpret findings using the actual sample size and collection status.

## Moving from the original CSV scraper

If you already have a CSV from the original scraper, import it before collecting more reviews:

```powershell
python offline.py import reviews.csv --db data/reviews.sqlite3
python offline.py status --db data/reviews.sqlite3
```

Use the path to your existing CSV in place of `reviews.csv`. The nine review columns are unchanged. Reviews without an ID receive a stable fingerprint from their contents. Stored rows are deduplicated by ASIN and review ID, so importing the same file again does not add another copy.

The original collector appended directly to CSV. Collection now writes to `data/reviews.sqlite3` by default; `--db` selects a different file. `--output` is optional and writes a fresh CSV export of the stored reviews instead of appending rows. Use a separate output filename to keep an existing CSV unchanged. The `--auth` command still opens the browser for manual sign-in.

## Collection architecture and alternatives

The CLI and interface share `collection_runtime.py`. One worker thread owns the synchronous Playwright objects, browser context, and writable review store for the batch. HTTP handlers enqueue work and return immediately. Progress is reported after durable saves. The browser session is reused across products, and refreshed state is saved atomically after a successful batch. Blocked runs retain the previous session file.

The original fixed two-second sleep has been replaced in the shared runtime by a minimum start interval, one second by default. Images, fonts, and media can be skipped. The **experimental hybrid** transport uses `context.request` with the same cookies to fetch ordinary review-page HTML without rendering it. It validates same-product HTTPS URLs and redirects, disposes responses, and returns to the browser for JavaScript pagination, unknown layouts, or a page without a confirmed next link. The browser confirms pagination boundaries. Login, CAPTCHA, and HTTP access blocks stop collection instead of triggering another transport.

Portal review Next controls are clicked in the rendered browser, and collection waits for review text to change. Their reference links are not treated as durable page URLs. Saved positions retain the base page and a bounded number of replay clicks. Hybrid mode returns to the browser when it encounters these controls. A repeated review set still stops collection with partial coverage.

If the exact legacy review entry returns HTTP 404, browser mode opens that product's own page once and inspects its **See more reviews** link. It follows only a validated review-portal link for the same ASIN on HTTPS Amazon.in. A confirmed portal entry becomes the durable replay base. This recovery does not apply to later pagination URLs, login challenges, HTTP 403 or 429, unknown product links, or other server errors. Missing review links remain failures.

Hybrid requests may reduce rendering where server HTML is sufficient, but they are not an official review API or a guaranteed speed improvement. AJAX and portal positions can require replay after a restart. Job metrics include elapsed time, pacing time, HTTP pages, browser pages, fallbacks, and skipped assets. Compare the same product, page limit, and starting position; different review order or access conditions can make comparisons misleading.

A live check on 16 October 2026 used product `B0CGDZC2FK` and a manually saved session. Browser mode processed two distinct pages in 5.58 seconds. Hybrid mode processed the same two pages in 8.64 seconds, needed one browser fallback, and added no duplicate reviews. Browser mode then resumed the saved position and added ten reviews from the third page. The workspace held thirty unique reviews, with local analysis generated automatically. These are individual observations on one product, not a performance benchmark or a guarantee of complete coverage. Browser mode remains the default.

Other sources were considered:

| Source | Useful for | Limitation for this project |
| --- | --- | --- |
| Amazon Creators API | Official product catalog, offers, variations, and discovery | The documented operations and resources do not provide a general full-review-text feed. It requires Associates access. |
| Amazon Selling Partner Customer Feedback API | Seller and vendor review and return insights | It exposes a business insights workflow with its own access requirements, rather than a drop-in general review scraper. |
| Amazon Reviews 2023 research dataset | Offline analysis and evaluation without live scraping | Historical data through September 2023 does not provide current arbitrary Amazon India reviews. |
| CSV from an existing authorised source | Immediate local analysis with no browser waiting | Freshness and coverage depend on that source. |

Primary references: [Creators API resources](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/api-reference), [Customer Feedback API announcement](https://developer-docs.amazon.com/sp-api/docs/sp-api-release-notes), [Amazon Reviews 2023](https://amazon-reviews-2023.github.io/main.html), [Playwright session reuse](https://playwright.dev/python/docs/auth), [shared-cookie requests](https://playwright.dev/python/docs/api/class-apirequestcontext), and [network resource controls](https://playwright.dev/python/docs/network).

## Where agents help

Collection, storage, exact statistics, and ordinary report batching follow fixed steps. These tasks do not benefit from an agent deciding what to do on every review. SQLite preserves collection progress, jobs, reports, validated analysis chunks, and request accounting.

Ask reviews uses model-directed tool selection when a question needs evidence retrieval. The model chooses from a small read-only toolset; the application validates its arguments, executes the tool, checks the returned evidence, and enforces stopping limits. Recognized statistics questions bypass the model. This keeps agents focused on investigation and avoids a separate model call for every review.

Future evaluation should compare theme quality, unsupported claims, multilingual understanding, and useful evidence against human-reviewed examples. Passing software tests alone does not establish analysis quality.

## Validation

```powershell
python -m unittest discover -s tests -v
```

Validation uses local data, fake browser/request adapters, local HTML fixtures, fake provider responses, and a loopback server for the interface API. It checks collection cancellation, access blocks, session persistence, job recovery, schema preservation, report caching, quote validation, and the UI API. It never executes the live `scraper.py` entry point, contacts Amazon, or calls OpenAI or OpenRouter. Passing these tests verifies local behavior; live pagination, saved-session validity, provider access, and compatibility with Amazon's current layout require separate intentional checks.

GitHub Actions runs the offline suite and a synthetic demo on Linux and Windows with Python 3.10 and 3.13. It does not install Playwright, open a browser, or need account secrets.
