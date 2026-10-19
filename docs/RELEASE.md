# Release status

The current release is a local, single-user portfolio application. Public hosting is deferred. The goal is a reproducible demonstration, clear source evidence and engineering decisions that can be inspected.

## Included

- Resumable Amazon India collection with durable reviews and checkpoints, bounded pages and manual sign-in recovery.
- Product inspection, category-neutral comparisons, local review search and optional source-linked AI interpretation.
- An isolated offline showcase with named fictional products, a guided path and a separately labelled written example.
- Persisted execution records with bounded counters and honest handling of unknown interruption timings.
- Separate frontend feature modules, backend error mapping and execution-record code.
- Backend tests, real Chromium journeys using fake providers, and a published synthetic evaluation report.

## Verified examples

Recorded checks on October 19, 2026 include an accepted 100-review report that processed all 103 stored text segments in one model request, with a provider-reported cost of $0.00. That run took 8.811 seconds. A two-product focused comparison took 28.268 seconds; a later cached repeat took 0.243 seconds with no new AI request. These are individual observations, not latency or uptime guarantees.

Core source checks and structure validation passed for those examples. They do not establish that every interpretation was supported. The optional deeper live agent received provider-level rate limits on two bounded checks and returned local evidence.

The offline evaluation passes 4 statistics cases, 8 source-validation cases and 10 of 12 retrieval cases. Its reference notes are synthetic and have not been independently reviewed. Semantic accuracy has not been measured.

## Verification commands

```console
python -m unittest discover -s tests -v
python tools/check_web.py
python -m evaluations.run --output evaluation-report.json
python -m unittest discover -s browser_tests -v
```

Browser tests require the pinned Playwright dependency and Chromium. Tests use fictional reviews and injected provider responses. The optional real Windows credential round trip is separate and skipped by default. See the usage guide for its prerequisites.

## Recovery and boundaries

Stop active work through the app before stopping the server. Reviews, checkpoints, reports and validated cache entries survive restart. Unfinished jobs become interrupted and require an explicit new run. Remembered Windows keys stay in Credential Manager rather than project files or databases.

Keep real datasets, authentication state, keys, logs and backups private. Public recordings should use the fictional showcase. Source matching does not detect fake reviews, establish current prices, prove representativeness or measure failure rates.

The HTTP server remains loopback-only. Authentication, isolated users, supported public serving and distributed workers have not been implemented. The deferred [hosted plan](HOSTED_BETA.md) is background context, not a release commitment.
