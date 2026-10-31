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

The offline evaluation passes 4 statistics cases, 8 source-validation cases and 20 of 20 retrieval cases, 32 authored scenarios in total. Its reference notes are synthetic and have not been independently reviewed. Semantic accuracy has not been measured.

The October 21, 2026 refinement passes 422 backend tests with one optional vault test skipped, seven Chromium journeys and syntax checks for all six frontend scripts. Product previews identify saved names and counts; research results disclose selection counts and exact variant descriptions. These checks use fictional data and fake providers; no new Amazon collection was run. Two separate fictional OpenRouter checks were also attempted. The first response repeated a citation and was rejected. After local duplicate-citation recovery was added and tested, the second attempt received a provider-level rate limit. Both retained local evidence, and no further live retry was made. A later focused-research check on October 22, 2026 returned an accepted answer; see the follow-up below.

The October 22 provider compatibility fix uses the free router with a verified free backup, excludes returned reasoning instead of disabling required reasoning, and shares a 2,500-token reasoning-plus-answer limit across the request planner and execution. A real website check on fictional reviews completed in 13.477 seconds with one model request and a provider-reported cost of $0.00. Its cached repeat completed in 0.067 seconds with zero model requests. These observations and exact fictional sources are in [the live check record](live-provider-checks.json). Provider rate limits remain possible; wait headers now handle HTTP dates and case-insensitive names. No application retry loop or paid fallback was added.

## Verification commands

```console
python -m unittest discover -s tests -v
python tools/check_web.py
python -m evaluations.run --output evaluation-report.json
python -m unittest discover -s browser_tests -v
```

Browser tests require the pinned Playwright dependency and Chromium. The browser CI job uses Ubuntu 24.04 because the pinned Playwright release does not support the newer Ubuntu image behind `ubuntu-latest`. The standard-library test matrix still exercises the current Windows and Ubuntu runners. Tests use fictional reviews and injected provider responses. The optional real Windows credential round trip is separate and skipped by default. See the usage guide for its prerequisites.

## Recovery and boundaries

Stop active work through the app before stopping the server. Reviews, checkpoints, reports and validated cache entries survive restart. Unfinished jobs become interrupted and require an explicit new run. Remembered Windows keys stay in Credential Manager rather than project files or databases.

Keep real datasets, authentication state, keys, logs and backups private. Public recordings should use the fictional showcase. Source matching does not detect fake reviews, establish current prices, prove representativeness or measure failure rates.

The HTTP server remains loopback-only. Authentication, isolated users, supported public serving and distributed workers have not been implemented. The deferred [hosted plan](HOSTED_BETA.md) is background context, not a release commitment.
