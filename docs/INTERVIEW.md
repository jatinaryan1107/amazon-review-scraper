# Interview notes

These notes are prompts for understanding the implementation. Rewrite the explanations in your own words and run the examples before discussing the project in an interview.

## A concise description

Reviewdesk collects product reviews into a local workspace and helps inspect the evidence behind product claims. Collection can resume after an interruption. Local code computes exact statistics and selects context; optional AI produces source-linked interpretations. Invalid model output leaves the original evidence available.

## Decisions worth being able to explain

- Why one worker owns the browser, and what happens while a request is running.
- How review identity, shared variants and conflicting ratings affect counts.
- Which changes invalidate an analysis cache entry.
- Why an HTTP success does not guarantee an accepted AI answer.
- How cancellation, a process restart and a provider rate limit differ.
- What the sharing checkbox authorizes and where credentials are stored.
- Why a matching quote does not prove the associated claim.
- How a real response exposed redundant citations, and why exact duplicate removal is safe while unknown source labels still fail.
- Which tests use real browser interactions, fake providers and authored examples.
- Why local storage fits this version and what would change for multiple users.

The architecture and decisions documents point to the relevant modules. For a concrete example, follow a comparison from `/api/jobs`, through the worker and local evidence selection, to validation and the saved result. Then repeat it and inspect the zero-call cache path.

## Resume wording supported by the implementation

- Built a local product-review research application with resumable Playwright collection, SQLite persistence, background jobs and source-linked comparisons.
- Added bounded optional LLM analysis with validated citations, privacy-aware cache identity, cancellation and recovery to local evidence when a provider fails.
- Created an isolated offline showcase, synthetic retrieval evaluations and browser journey tests alongside backend tests on Windows and Linux.

Use numbers only when you can explain how they were measured. The recorded 100-review run and cache examples are individual observations, not production latency or reliability benchmarks. Commit count is not an impact metric.

## Gaps to discuss openly

Public hosting is deferred. The deeper live agent remains provider-limited. Focused retrieval supports a limited English variant grammar and selected word families. Unrecognized language can be missed; review descriptions are not verified variant identity. The evaluation has not measured semantic accuracy on independent reviews. Automated checks and AI assistance do not replace reading the code, reviewing changes and understanding the tradeoffs.
