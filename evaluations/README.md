# Evaluating Reviewdesk

This small corpus checks local statistics, lexical retrieval and rejection of invalid source references. All reviews are fictional. Its reference notes and interpretation rubrics were authored for this project and still need independent human review.

```console
python -m evaluations.run --output evaluation-report.json
```

The command uses no model, network or database. Output includes the corpus version and content hash, per-case outcomes and known limits. Generating the report succeeds even when retrieval cases fail; those failures are measurements, not silently skipped tests.

## Current baseline

| Group | Cases passing | What is checked |
| --- | --- | --- |
| Local statistics | 4 of 4 | Mixed ratings, duplicate IDs, shared membership and conflicting ratings |
| Source validation | 8 of 8 | Valid evidence and rejection of unknown, altered, repeated, missing, oversized or malformed output |
| Retrieval | 10 of 12 | Relevant passages in small authored scenarios |
| Model interpretation | Not measured | Requires actual model answers and human review |

The committed [result file](../docs/evaluation-results.json) contains every outcome.

Two retrieval gaps remain visible. A question about a blue variant also selects the red variant because they share topic words. A question using `overheat` misses a passage using `overheating`. Neither result is fixed by matching a quote after generation. The tiny corpus should not become a list of special cases built just to make a score green.

## Checking model answers

Supply a JSON object keyed by a case ID:

```json
{
  "battery_synonym": {
    "answer": "One account describes short battery backup; typical runtime is unknown.",
    "evidence": [
      {"review_id": "a", "asin": "B0EVAL0001", "quote": "Charging takes two hours and backup is short."}
    ]
  }
}
```

```console
python -m evaluations.run --answers my-answers.json --output checked-answers.json
```

This checks the submitted citations against the fictional source text and flags missing, altered, repeated or wrong-product sources. It never sends the file anywhere. A passing source check is not a semantic pass. Even a false claim can cite an exact quote.

For each answer, use the case's `human_review_rubric` and record whether it preserves negation, includes material counterevidence, handles missing information and avoids unsupported specifications, prices or prevalence. Suggested ratings are **supported**, **partly supported**, **unsupported** and **insufficient evidence**. Record the reviewer and disagreements separately. Do not label these ratings as independent until someone other than the author has actually reviewed them.

Live provider comparison is intentionally separate from this offline command. Preserve privacy settings, account for every request attempt and report model availability and failures alongside accepted answers. No semantic accuracy percentage has been established yet.

## Optional provider run

The separate runner can request real answers for one to four fictional retrieval cases. It uses Reviewdesk's fixed free route and the chosen workspace's request ledger. It stops remaining cases after a capacity, authorization or budget limit.

```console
python -m evaluations.live --live-openrouter --case battery_synonym --use-saved-key
```

The explicit flag permits sending the selected fictional text to OpenRouter. Each uncached case can consume a free request attempt. `--use-saved-key` loads the normal workspace's remembered Windows key and preferences; without it, use `OPENROUTER_API_KEY` from your terminal environment. `--workspace` selects a different workspace and its shared ledger. Run it while other AI work is idle if you want interpretable timings.

Results default to `data/evaluations/live.json`, outside published source. They retain accepted answers, original evidence, actual usage and safe failure notices. Human review stays pending. Cached answers are labelled and use zero new calls. This runner was verified with fake provider responses during the portfolio upgrade; no live semantic benchmark was run.
