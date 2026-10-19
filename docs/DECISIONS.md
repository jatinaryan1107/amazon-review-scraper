# Design decisions

## Keep the application local

The current purpose is a demonstrable research tool and engineering portfolio project. Loopback serving and local storage fit that scope. Public hosting would introduce identities, authorization, isolated sessions and operational work that are currently deferred. The present HTTP server is not presented as a public deployment stack.

## Use SQLite for durable work

A file database makes setup small while supporting transactions, deduplication, checkpoints and saved jobs. Separate review, job and analysis stores keep their responsibilities clear. The tradeoff is limited concurrency and responsibility for local backups. The app permits one worker per data folder; it does not claim distributed processing.

## Give browser work one owner

The worker thread owns the synchronous Playwright objects and writable collection store for a batch. HTTP handlers enqueue requests and return promptly. Reusing a browser session reduces repeated setup, while saved checkpoints make partial work useful. One slow collection can hold the worker, so job progress and cancellation stay visible.

## Retrieve locally before asking a model

Counts and averages have exact local answers. Lexical and concept matching can select context cheaply and repeatably. This keeps most research to one optional model call and makes cache behavior understandable. It also misses word forms and can mix variant descriptions. The evaluation publishes those shortcomings. Embeddings or another agent should earn their place through measured improvement.

## Treat structured output as a boundary

A provider can return HTTP 200 while the answer is incomplete, malformed or unsupported. The transport checks completion and usage, and the analysis layer checks structure and sources. Preferred free routing and JSON repair reduce some failures, but do not make free capacity dependable. The app does not make a paid fallback or silently relax privacy preferences.

## Keep AI optional in the experience

Local statistics, original reviews and retrieved passages remain useful when a model cannot answer. The sharing checkbox starts off. AI interpretations, local evidence and the written showcase example have different labels. This keeps the user in charge of what leaves the device and how much confidence to place in a result.

## Refactor around workflows

Inspection and comparison now have separate frontend modules with explicit dependencies. Shared DOM construction uses text content for user and review strings. Execution records and provider-error messages are separate backend modules. Existing public imports and routes remain compatible. Browser journeys protect behavior across these boundaries.

## Report what has actually been measured

A successful example is not an uptime benchmark. Passing source validation is not an accuracy score. Synthetic cases are useful for regressions, but their reference notes still need independent human review. The project records these limits instead of presenting a perfect score.
