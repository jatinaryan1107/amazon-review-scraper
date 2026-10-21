"""Evaluate synthetic scenarios locally, or source-check supplied model answers."""

import argparse
import hashlib
import json
from pathlib import Path

from llm_analysis import LLMEngine, _model_json
from llm_provider import ProviderError, ProviderRegistry
from research import retrieve


CASES = Path(__file__).with_name("cases.json")


def rows(case):
    return [{"asin": item.get("asin", "B0EVAL0001"), "review_id": item["id"], "review_title": item.get("title", ""),
             "review_body": item["body"], "rating": str(item.get("rating", "")), "reviewer_name": "Fictional reviewer",
             "product_url": "", "verified_purchase": "false", "review_date": ""} for item in case["reviews"]]


def grade_sources(case, answer):
    """Check traceability only. Whether the claim follows needs human review."""
    issues = []
    if not isinstance(answer, dict) or not isinstance(answer.get("answer"), str) or not answer["answer"].strip():
        return {"passed": False, "issues": ["missing_answer"], "semantic_review": "required"}
    evidence = answer.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 8:
        return {"passed": False, "issues": ["invalid_evidence_list"], "semantic_review": "required"}
    lookup = {}
    for item in rows(case):
        lookup.setdefault(item["review_id"], []).append(item)
    seen = set()
    for citation in evidence:
        if not isinstance(citation, dict):
            issues.append("invalid_citation")
            continue
        identity = citation.get("review_id")
        if not isinstance(identity, str) or identity not in lookup:
            issues.append("unknown_review")
            continue
        if identity in seen:
            issues.append("repeated_review")
        seen.add(identity)
        originals = lookup[identity]
        asin = citation.get("asin")
        if asin is not None:
            originals = [item for item in originals if item["asin"] == asin]
        elif len({item["asin"] for item in originals}) > 1:
            issues.append("ambiguous_product")
        if not originals:
            issues.append("wrong_product")
        quote = citation.get("quote")
        quotes = citation.get("quotes", [])
        if not isinstance(quotes, list) or len(quotes) > 8:
            issues.append("invalid_quotes")
            quotes = []
        for passage in [quote, *quotes]:
            if not isinstance(passage, str) or not passage or not any(passage in original[field] for original in originals for field in ("review_title", "review_body")):
                issues.append("altered_quote")
    if not evidence and case.get("relevant_ids"):
        issues.append("no_cited_evidence")
    return {"passed": not issues, "issues": sorted(set(issues)), "semantic_review": "required"}


def evaluate(corpus, answers=None):
    if not isinstance(corpus, dict) or corpus.get("version") != 1:
        raise ValueError("Unsupported evaluation corpus")
    cases = corpus["cases"]
    identities = [case["id"] for case in cases]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate case identity")
    if answers is not None and set(answers) - set(identities):
        raise ValueError("Answers contain an unknown case identity")
    results, model_results = [], []
    engine = LLMEngine(ProviderRegistry(environ={}), None)
    for case in cases:
        canonical, _, _, metadata = engine._prepare(rows(case), "demo", "")
        if case["kind"] == "statistics":
            stats = {"total_reviews": len(canonical), **engine._stats(canonical, metadata)}
            actual = {key: stats[key] for key in case["expected"]}
            results.append({"id": case["id"], "kind": case["kind"], "passed": actual == case["expected"], "expected": case["expected"], "actual": actual})
        elif case["kind"] == "retrieval":
            _, exposed, _ = retrieve(engine, canonical, case["question"])
            actual = sorted({item["review_id"] for item in exposed.values()})
            expected = set(case["relevant_ids"])
            matched = len(expected.intersection(actual))
            precision = matched / len(actual) if actual else 1.0 if not expected else 0.0
            recall = matched / len(expected) if expected else 1.0
            results.append({"id": case["id"], "kind": case["kind"], "passed": set(actual) == expected,
                            "precision": round(precision, 3), "recall": round(recall, 3), "expected": sorted(expected), "actual": actual})
        elif case["kind"] == "source_validation":
            row = canonical[0]
            exposed = {"r1": {**row, "sent_title": row["review_title"], "sent_body": row["review_body"],
                               "shown_titles": [row["review_title"]], "shown_bodies": [row["review_body"]]}}
            answer = {"answer": "A selected account describes its experience.", "evidence": [{"key": "r1", "quote": row["review_body"]}], "limitations": []}
            mutation = case["mutation"]
            if mutation == "unknown_source": answer["evidence"][0]["key"] = "unknown"
            if mutation == "altered_quote": answer["evidence"][0]["quote"] = "Invented warranty promise."
            if mutation == "repeated_source": answer["evidence"] *= 2
            if mutation == "empty_evidence": answer["evidence"] = []
            if mutation == "long_answer": answer["answer"] = "x" * 2401
            if mutation == "extra_field": answer["secret"] = "Should be rejected"
            accepted = True
            try:
                if mutation == "duplicate_json":
                    _model_json('{"answer":"first","answer":"second"}')
                engine._validate_answer(answer, exposed)
            except (ProviderError, ValueError):
                accepted = False
            results.append({"id": case["id"], "kind": case["kind"], "passed": accepted == case["accept"], "expected_accept": case["accept"], "actual_accept": accepted})
        else:
            raise ValueError("Unknown evaluation kind")
        if answers is not None and case["id"] in answers:
            model_results.append({"id": case["id"], **grade_sources(case, answers[case["id"]])})
    return {"corpus_version": corpus["version"], "corpus_sha256": hashlib.sha256(json.dumps(corpus, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "data": "fictional", "model_calls": 0, "mode": "offline_contracts_and_retrieval", "cases": len(results),
            "passed": sum(item["passed"] for item in results), "results": results,
            "model_answer_evaluation": {"status": "source_checked_only" if model_results else "not_run", "results": model_results,
                "semantic_accuracy": "not_measured", "human_review_required": True},
            "limitations": ["Retrieval is lexical and can select text with the opposite meaning. Source matching cannot establish semantic support.",
                            "These are authored synthetic scenarios, not independent customer data or a general accuracy benchmark.",
                            "Reference notes and review rubrics need independent human review. No LLM judge or live model is used by this command."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--answers", type=Path, help="Optional JSON object keyed by case ID; never sends its contents anywhere")
    args = parser.parse_args(argv)
    corpus = json.loads(CASES.read_text(encoding="utf-8"))
    answers = None
    if args.answers:
        if args.answers.stat().st_size > 1_048_576:
            parser.error("Answer file must be at most 1 MiB")
        answers = json.loads(args.answers.read_text(encoding="utf-8"))
        if not isinstance(answers, dict):
            parser.error("Answers must be a JSON object keyed by case ID")
    report = evaluate(corpus, answers)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
