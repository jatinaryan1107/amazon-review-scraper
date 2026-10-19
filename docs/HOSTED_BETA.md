# A public beta built around review research

The goal is a hosted workspace people can use without installing Python. Review research is its first complete feature. Keep collection and analysis as separate services so other data sources can be added later.

## First public workflow

1. Sign in and create a private workspace.
2. Upload a review CSV or try a clearly labelled fictional dataset.
3. Inspect import errors, duplicate counts, ratings and source coverage.
4. Run a local topic scan on the server without an LLM charge.
5. Optionally connect a personal OpenRouter key, inspect a request plan and consent to sharing selected text.
6. Read the report, open its evidence and delete the workspace when finished.

Ship this path before adding more agents or unrelated features. Authenticated Amazon collection should stay in the local companion initially. A later hosted collector needs a separate browser profile and session lifecycle for every user. Never use the project owner's Amazon session as a shared collection account.

## Boundaries to implement before inviting users

| Area | Required behavior |
| --- | --- |
| Identity | Managed sign-in, verified sessions and secure session cookies over HTTPS. |
| Data | Every dataset, review, upload, job, report and cache entry belongs to a workspace. Every read and write checks membership. |
| Storage | Managed relational storage with reviewed migrations and recovery-tested backups. Private uploads have expiry and size limits. |
| Workers | A durable queue carries workspace IDs, cancellation and an explicit budget. Worker restarts cannot duplicate a provider request with an uncertain result. |
| Credentials | Per-user provider credentials have a defined expiry and deletion path. No global fallback key and no credentials in job records or browser storage. |
| Cost | Per-user, workspace and deployment limits. Actual provider quota is distinct from local attempts. Free-route failures leave local analysis usable. |
| Cache | Scope reuse to the authorized workspace and privacy policy. A shared content hash must not expose another user's reviews or results. |
| Collection | Isolate browser profiles and login sessions. Access challenges stop work and require the owning user's action. |
| Operations | A supported application server, health checks, request limits and redacted logs. Track job failures, queue age and provider availability without logging review bodies or credentials. |
| Deletion | Remove a workspace's uploads, review text, reports, caches and credentials according to an explicit retention policy. |

## Changes to the current code

Keep the parser, deterministic collector and local analysis functions as independent domain modules. Put the current HTTP handlers behind a service layer that accepts an authenticated workspace identity. Replace process-wide provider configuration and the single data folder with workspace-scoped repositories. Move durable job execution into a separate worker process. The existing interface can continue to use explicit analysis plans, source drawers and consent controls.

Do not reuse the current unauthenticated local endpoints through a tunnel. Host checks and loopback binding protect the local workflow; they do not implement user authorization.

## Acceptance checks for that beta

- Two users cannot read, change, export, cancel or infer each other's data, jobs, credentials or cached answers, even with a guessed ID.
- A user with no provider key can import data, browse reviews and run local analysis.
- Sharing consent is bound to a specific dataset snapshot and plan. A changed scope requires consent again.
- An interrupted or rate-limited job retains valid work without submitting the same uncertain model request automatically.
- Unavailable free models produce a clear failure without paid fallback or privacy changes.
- A workspace can be deleted, a backup can be restored and a secret can be revoked without affecting another workspace.
- Model evaluation checks source support as well as JSON shape. Exact quote matching alone is not a semantic correctness test.

These are implementation requirements for the next release, not claims about the present local app. Choose the hosting, identity and storage services before that migration so their costs and constraints can be reviewed together.
