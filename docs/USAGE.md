# Using Reviewdesk

This project collects product reviews from Amazon India and stores them for research in Reviewdesk. You can start a bounded background collection, resume saved progress, and generate reports linked to source reviews. Local analysis uses transparent keyword rules and rating statistics. Optional OpenRouter analysis uses free models, cached batches, and explicit sharing consent. **Ask reviews** adds a bounded agent that can choose local evidence tools when a question needs investigation. The existing optional OpenAI adapter is also available.

Reviewdesk is a local, single-user application. You can share a demo or the source, but the current server is not a public hosted service. See the [release notes and deployment boundary](RELEASE.md) before presenting it as production ready.

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

## Build a buying brief

Open the dashboard at `http://127.0.0.1:8765/#buying`. This is the default entry screen when no view is specified. The existing dashboard, analysis and collection tools remain available under **Research workspace**.

1. Paste an HTTPS Amazon India product link or a ten-character ASIN, or choose a saved product. **Compare another product** opens the separate category-neutral comparison module. Short links must be expanded to their full product URL by the user; the app does not follow arbitrary redirects.
2. Optionally choose up to three priorities: ease of use, durability, cleaning or value for money. The visible priorities cover ease of use, durability, cleaning and perceived value. Older saved audio-specific briefs remain readable. Leaving them blank requests a general brief.
3. Build the brief from saved reviews. Without AI sharing, the result contains local owner evidence and exact stored rating statistics. Optional AI interpretation produces source-linked benefits, concerns and uncertainty notes.

Comparisons assess both products on the same priorities. The interface displays accepted findings beside each other with expandable source evidence. It does not assign a made-up overall score or choose a universal winner. Product details and full findings remain available below the comparison. No purchase is made.

A brief selects at most 12 relevant reviews per product, balancing available rating groups. This is a retrieved sample, not a representative survey or complete Amazon coverage. A positive overall rating does not make every passage praise. Stored counts and average ratings are calculated in Python. Review evidence does not establish a current price, verified specifications, failure probability or guaranteed suitability; shared review IDs and product variants are disclosed.

One optional free-model request covers either one or two products. Matching cached interpretations are revalidated against current source passages and reused without another request. Source data, product order, priorities and privacy preferences participate in cache identity. Unknown product or passage labels, altered quotes, unsupported comparison sources and invalid output fail local validation. Equivalent single-field text notes can be normalized without another model request. A disclosed English keyword check omits off-topic findings; it does not prove semantic relevance or sentiment. Provider and validation failures leave labelled local evidence available, with no automatic retry or paid fallback.

The saved OpenRouter privacy settings apply. Model input uses opaque product, source and passage labels with selected review text and ratings. Reviewer fields, original review IDs and Amazon session data are omitted; review text itself may contain personal information. Sharing requires the explicit checkbox. A missing product prompts collection or CSV import rather than fabricating a brief. Collection remains a separate action that may require manual Amazon sign-in. This local release still requires a configured provider key for AI and is not a hosted consumer service.

## Compare any two products

Open **Compare products** at `http://127.0.0.1:8765/#compare`. This workflow is separate from the priority-based buying brief, collection controls, full analysis and question research.

1. Paste one full Amazon India link in each field, or choose two saved ASINs. Only HTTPS `amazon.in` and `www.amazon.in` product routes are accepted. Links are canonicalized without tracking parameters; duplicate ASINs, other hosts, credentials, ports, pasted prose and short links are rejected before collection.
2. Review the local preview of product names, saved review counts and planned pages. It makes no Amazon or AI request and creates no job. Choose a target of 10, 30 or 100 reviews per product. Saved reviews count toward the target; collection gathers only the shortfall within your per-run page limit and resumes durable checkpoints. The page estimate assumes up to 10 reviews per page, so a target is not a guarantee. Sign-in or access blocks require attention; they are not bypassed. Demo comparisons never collect live reviews.
3. Optionally describe what matters to your decision, such as durability or easy cleaning. Local retrieval selects related owner excerpts where lexical matches exist. General context remains available with an explicit limitation when no matches are found. Priorities participate in cache identity and are treated as preferences, not instructions to override the analysis rules.
4. Optionally allow one free OpenRouter request covering both products. Local preparation selects up to 12 excerpts per product across available rating groups. Names, selected text, ratings and opaque source labels are sent; reviewer names, original IDs and session data stay out of the request. Saved privacy settings still apply, and review text itself can contain personal information.
5. Inspect the side-by-side reason to consider each product and its main concern, then expand the sources and the fuller excerpt shown to AI. Exact saved rating statistics and missing information remain available. The AI chooses aspects from the evidence without fixed audio categories. Unrelated products receive separate findings rather than a forced winner. Product relationship and interpretation remain AI judgments, not verified specifications. Save a plain-text comparison with source quotes for later use; the export omits account keys, reviewer fields and Amazon session data.

The result records collection, preparation, AI plus validation and total job seconds, excluding queue time. Saved-data runs explicitly show zero collection time. Collection outcomes and any partial sample remain visible. A cache hit revalidates the saved interpretation with zero new model calls. Invalid output falls back to labelled local evidence without automatic retries or paid routing. This focused workflow does not need a planner or tool-using agent; deeper investigation remains a separate research mode.

If the sample target is unmet, **Prepare to resume collection** restores the product pair and saved cursors with AI sharing off. The local preview recalculates only the remaining work. The app does not silently restart a completed listing or repeatedly call a provider. Samples without low-rated reviews receive an explicit warning about possible bias.

Comparison requests use one flat JSON object for the two products, avoiding nested finding lists. Accepted output is converted to the same source-checked product structure. Comparison JSON can normalize at most two misplaced closing array or object delimiters outside quoted strings. It never invents fields, strings or missing content at the end of input. The strict parser still rejects duplicate fields, and the full schema, correct-product references and exact source checks run afterwards. This avoids an extra request for a narrow formatting defect; it does not repair or verify the model's meaning.

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

### Inspect a product

Find products by name or ASIN in **Products**, then choose **Inspect product**. The inspection summary shows stored review counts, average rating, critical reviews at 1 or 2 stars, verification markers and when reviews were added. Open findings or ask a question directly for that product. These figures describe saved evidence, not every Amazon rating or a live market price. Review sorting uses when reviews were added to the workspace.

Product names appear in cards, filters, analysis headings and cited sources. The ASIN stays visible to distinguish similar models and variants. Collection saves explicit product titles when the supported page supplies them. Missing titles remain clearly labelled and can be named locally. **Edit name** stores a local label that later collection and imports do not overwrite. Names are plain display text and do not change review identities or analysis evidence.

Dashboard CSV imports accept an optional `product_name` column; use one consistent name per ASIN. Dashboard exports include this column and escape spreadsheet formulas. Existing nine-column imports still work. Standalone CLI exports retain the original nine review fields. Database schema 2 adds product metadata transactionally; read-only access to older supported databases remains available without migrating them. Back up a database before using it with an older application version.

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
2. In Reviewdesk, open **Settings**, enter the key, and save. Enter it in the local application, not a chat or source file. On Windows, choose **Remember on this Windows account** to save it in Windows Credential Manager and reconnect after restart. The saved entry also retains your response-format and zero-retention preferences. Without that option, the key stays in memory until the server exits. Alternatively, set `OPENROUTER_API_KEY` in the server's environment before starting it.
3. In AI settings, use **Provider-enforced JSON**, the default for new configurations. It requests the complete response schema and automatic syntax repair. Existing saved format preferences remain unchanged until you save new settings. Open **Analysis**, choose OpenRouter and a scope, and inspect the coverage and call estimate. **Quick** uses a bounded sample. **Broader coverage** processes deterministic chunks and reuses completed chunks on subsequent runs.
4. Allow the selected review text to be sent to OpenRouter, then start the job. Collection's automatic report still runs locally.

Reviewdesk first requests [`xingchen-agi/xing4.0-29b-a4b:free`](https://openrouter.ai/xingchen-agi/xing4.0-29b-a4b:free), which supports JSON objects, structured outputs and tools in the public model catalog checked on October 19, 2026. Using a preferred model reduces changes in output behavior between calls. The same request supplies `openrouter/free` as its only backup candidate. [OpenRouter model fallbacks](https://openrouter.ai/docs/guides/routing/model-fallbacks) can use that free route when the primary route errors. The request enforces zero prompt, completion and request prices, denies training use, and preserves the chosen zero-retention policy on both routes. There is no paid fallback. Reports record the actual model that answered. A preferred model does not guarantee availability or interpretation quality.

OpenRouter currently documents 20 requests per minute and 50 per day for free models on accounts without purchased credits. Purchasing at least $10 in credits raises the documented daily allowance, but Reviewdesk keeps its own conservative 50-attempt daily cap. Failed or uncertain requests can consume quota. The app paces requests, limits each job, and does not make another application request automatically. OpenRouter may try the configured free backup within the original request; its account counter remains authoritative. Other applications using the same account can reduce the provider allowance independently. Check [current OpenRouter limits](https://openrouter.ai/pricing) before relying on a daily capacity.

The sidebar shows the last checked OpenRouter allowance and its check time. Click its refresh button for the current account counter; the app also refreshes it after an AI job finishes. This reads the account endpoint without sending reviews or generating a model response. The detailed quota check in AI settings also shows the reset time. OpenRouter's account allowance and Reviewdesk's conservative attempt budget are separate. A daily count does not measure per-minute limits or provider capacity.

Remembered keys are scoped to this data folder and the current Windows account on this computer. They are stored using [Windows Credential Manager](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw), outside the project and its databases. Other programs running as the same Windows user can access that user's generic credentials. **Forget key** removes both the active key and its saved copy. Unchecking **Remember** and saving removes the saved copy while keeping the current session connected. Moving the data folder requires reconnecting the key. Other operating systems retain session-only and environment-key support.

Validated analysis chunks and request accounting are stored in `data/analysis.sqlite3`. Keys are never stored there, in job options, in browser storage, or in reports. A cancelled or rate-limited run retains completed chunks; starting the same analysis again can reuse them. A large dataset may need several runs or days. Coverage counts, skipped text, cached chunks, and call usage determine what the report actually examined.

Provider responses are read incrementally. A two-minute response budget prevents a stream of keepalive bytes from keeping a job waiting indefinitely. Cancellation and the deadline are checked between reads; a blocked network operation can take up to its forty-second socket timeout before control returns. The app does not retry provider requests automatically; the configured upstream free backup is part of the same request.

Requests disable optional model reasoning and ask for compact, evidence-linked answers with a 1,000-token output ceiling. The preferred format is three themes and two passage labels per theme. Broader analysis still processes the selected text in chunks and combines their validated findings locally. These settings reduce waiting on slow free endpoints; they do not guarantee response speed. A timeout accepts no partial answer and directs you to [OpenRouter Logs](https://openrouter.ai/logs) before retrying. Provider execution can continue after the local connection closes, so a timed-out request may still appear there with output tokens.

Review text is supplied as labelled source passages. Models select passage IDs instead of transcribing quotes; the app resolves each selection to the original text and source review. Text splitting preserves every submitted character. Unknown passages and duplicate reviews within a theme are rejected. Responses still pass local JSON, report structure and exact-source checks, including when a cached report is reused. A single complete JSON markdown fence is accepted as a formatting wrapper; surrounding prose and duplicate JSON fields are rejected. Jobs distinguish validation failures without exposing raw provider responses or credentials. A successful provider generation is not the same as an accepted report.

**Provider-enforced JSON** is the default for new configurations and supplies a strict `json_schema` request. **Compatible JSON** remains available: it puts the schema in the system prompt and requests `response_format: {"type": "json_object"}`. Both use non-streaming responses, temperature zero and OpenRouter's [Response Healing plugin](https://openrouter.ai/docs/guides/features/plugins/response-healing), which repairs common JSON syntax problems without another app request. Tool-selection turns have no JSON format or repair plugin so agent tools remain usable. Healing does not prove schema compliance or source accuracy, and cannot recover a response cut off by the token budget. Both modes run the same local format, length, source and duplicate-review checks; invalid output is never cached as a successful answer. Privacy, cost, tool and availability requirements still apply to the preferred and backup routes.

The complete encoded request plan includes the chosen response mode and retention policy. Mode changes have separate cache identities. Engine versions, provider routes, response formats and privacy settings separate cache identities; older saved reports remain readable. New reports record which response mode produced them. A failed AI attempt appears above an older saved report so that local findings are not mistaken for a new LLM result.

Local validation enforces hard report limits independently of whether a provider follows its JSON schema: summaries up to 1,500 characters, eight themes, eight source passages per theme, 80-character labels, 600-character explanations and six 300-character limitations. Shorter responses are requested as a preference. A longer valid report is not discarded just for exceeding that preference. Unknown or repeated passage labels and quotes that do not match their submitted source still fail. Multiple passages from one review appear together and count as one cited review and one rating. Research answers allow up to 2,400 characters, eight passages and six 300-character limitations, while requesting shorter output. Cached chunks and research answers must pass the same source checks. New OpenRouter reports record their engine version, request privacy policy, free-only route and elapsed processing time. Older reports without that record are labelled accordingly in the explanation panel.

Open **How this report was produced** below a saved report to inspect the steps. Rating statistics are computed in Python. The model proposes themes; source matching proves where an excerpt came from, not that its interpretation is correct. Report generation usage describes the saved run, while the plan describes calls still needed for a future run. A provider-reported price is shown when available; missing cost data remains unknown.

Broader coverage packs batches by the complete encoded request size, including messages, citation schema and provider controls, with a 200,000-byte cap and at most 1,000 review segments per call. Binary search finds each batch boundary without repeatedly encoding every growing prefix. Quick sampling and research retain their smaller input limits. Larger batches reduce request count without dropping selected text; they do not guarantee faster responses or that a model will notice every issue. Model context limits and free-provider availability can still stop a run. The local plan shows the actual call estimate before any text is shared.

Reviewdesk requests providers that do not collect prompts for training and defaults new server sessions to requiring zero data retention. Changing that setting requires an explicit action in AI settings. These controls can leave no compatible free route. The app does not silently relax them. They are routing requirements, not a promise about every aspect of provider data handling. Remote response caching and automatic context compression are disabled. See [OpenRouter provider controls](https://openrouter.ai/docs/guides/routing/provider-selection).

In AI settings, **Check OpenRouter quota** reads the account's reported free-request usage, daily limit and remaining allowance. It sends no reviews and makes no model request. Reviewdesk's local attempt counter is shown separately because failed and uncertain attempts count locally. Missing account counters are displayed as unknown, not zero. Account quota checks are explicit actions and are not polled automatically.

A 429 does not establish that the daily allowance is exhausted. Provider failures retain only typed status codes, any supplied rate-limit headers, a reported reset time and an identified platform or provider source. Unknown sources remain unknown. Errors inside HTTP 200 responses are kept distinct from the HTTP status. Raw error bodies, account labels, unrelated budget fields and credentials are not exposed. See [OpenRouter limits](https://openrouter.ai/docs/api_reference/limits).

For recognized routing rejections, diagnostics retain a fixed category such as required parameters, data policy, region policy or no matching endpoint. Only that category leaves the transport; the raw provider message is discarded. A generic 404 is not labelled as a privacy failure. These categories describe the provider's reported reason, not an independently established diagnosis of account settings. Earlier failures without a category cannot be classified retrospectively.

## Ask questions about your reviews

Choose a dataset and optional product under **Ask reviews**. Recognized count and rating questions use local calculations with zero model calls. Other questions first search the stored review text locally. Leave sharing unchecked for local source excerpts only. A connected key and explicit sharing opt-in enable optional AI interpretation.

**Focused answer** is the default. Local keyword and concept expansion retrieves up to 12 reviews, with titles capped at 300 characters and bodies at 600. For example, a question about cheapness can retrieve affordable, price, value and expensive wording. The model receives labelled passages in one bounded request without tool calling. This is lexical retrieval, not an embedding model, and can miss relevant evidence or other languages. It cannot establish current prices or cheapest alternatives.

Validated focused answers are cached by question, current source fingerprint, retrieval version and privacy/format settings. A cache hit rechecks the quotes against the current retrieved excerpts and makes no model call. Changed review text or policy invalidates reuse. Ordinary analysis keeps its separate existing cache.

Choose **Agent investigation** only when you want deeper tool-based exploration. The agent can request local statistics, search stored reviews, look up evidence, and compare products inside the chosen scope. It has at most four model turns and three tool executions. It must retrieve review text before a final answer can be accepted. It cannot control the browser, collect more reviews, access session files, run shell commands, or query arbitrary SQL. The interface shows actual research steps and source quotes, rather than simulated agent activity.

Provider limits, timeouts, unavailable routes, malformed answers and invalid citations do not become fabricated AI answers. Research returns clearly labelled local source evidence and a fixed notice describing why AI was not accepted. No matching sources produces a clear no-evidence result without a model call. Cancellation remains cancellation. The latest result for each question appears in research history; earlier attempts remain in job history.

Rate-limit backoff is saved in the request ledger and survives server restarts. Repeating a request during the wait makes no model call and reserves no additional attempt. The app uses a provider retry/reset hint when available, or a conservative 30-second pause when the response supplies none. It does not automatically repeat the model request or pay for another route. Daily allowance, per-minute limits and upstream provider capacity remain separate constraints; see [OpenRouter error handling](https://openrouter.ai/docs/api_reference/errors-and-debugging).

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
