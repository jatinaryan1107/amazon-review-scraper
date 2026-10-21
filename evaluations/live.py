"""Optional free-provider evaluation on a small selection of fictional cases."""

import argparse
import json
from pathlib import Path
import sys
import time
import sqlite3

from analysis_store import AnalysisStore
from credential_store import system_credential_store
from evaluations.run import CASES, grade_sources, rows
from llm_analysis import LLMEngine
from llm_provider import ProviderError, ProviderRegistry


ROOT = CASES.parent.parent
STOP_CODES = {"rate_limit", "cooldown", "daily_limit", "cost_guard", "authorization", "not_configured", "budget"}


def select_cases(corpus, identities):
    if not 1 <= len(identities) <= 4 or len(set(identities)) != len(identities):
        raise ValueError("Select one to four different retrieval cases.")
    lookup = {case["id"]: case for case in corpus["cases"] if case["kind"] == "retrieval"}
    if any(identity not in lookup for identity in identities):
        raise ValueError("Live evaluation accepts only known retrieval case IDs.")
    return [lookup[identity] for identity in identities]


def run_cases(cases, engine):
    results = []
    stopped = None
    for case in cases:
        if stopped:
            results.append({"id": case["id"], "status": "not_run", "reason": stopped})
            continue
        started = time.monotonic()
        answer = engine.ask(rows(case), case["question"], dataset="demo", asin="", allow_external=True, strategy="focused")
        notice = answer.get("notice") or {}
        status = "accepted_ai" if answer["method"] == "openrouter" else "local_evidence"
        results.append({"id": case["id"], "status": status, "model": answer.get("model", ""),
            "elapsed_seconds": round(time.monotonic() - started, 3), "usage": answer["usage"],
            "cache_hit": answer.get("cache_hit", False), "notice": notice,
            "answer": answer["answer"], "evidence": answer["evidence"],
            "source_check": grade_sources(case, answer) if status == "accepted_ai" else None,
            "human_review": "pending", "rubric": case.get("human_review_rubric", [])})
        if notice.get("code") in STOP_CODES:
            stopped = notice["code"]
    return {"data": "fictional", "mode": "optional_live_provider", "semantic_accuracy": "not_measured",
            "results": results, "stopped_reason": stopped,
            "model_calls": sum(item.get("usage", {}).get("calls", 0) for item in results),
            "notice": "Accepted AI answers passed application structure and source checks. Semantic judgments still require human review. Failed attempts can consume provider allowance."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-openrouter", action="store_true", help="Explicitly allow these fictional cases to be sent to the fixed free OpenRouter route")
    parser.add_argument("--case", action="append", required=True, dest="cases")
    parser.add_argument("--workspace", type=Path, default=ROOT / "data", help="Use this workspace's shared attempt ledger")
    parser.add_argument("--use-saved-key", action="store_true", help="Load this workspace's remembered Windows key and privacy preferences")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "evaluations" / "live.json")
    args = parser.parse_args(argv)
    if not args.live_openrouter:
        parser.error("Live requests require --live-openrouter. The offline runner is python -m evaluations.run.")
    try:
        corpus = json.loads(CASES.read_text(encoding="utf-8"))
        cases = select_cases(corpus, args.cases)
        registry = ProviderRegistry(credential_store=system_credential_store(args.workspace) if args.use_saved_key else None)
        if not registry.public()["configured"]:
            parser.error("Connect a remembered workspace key and use --use-saved-key, or set OPENROUTER_API_KEY in the environment.")
        store = AnalysisStore(args.workspace / "analysis.sqlite3")
        report = run_cases(cases, LLMEngine(registry, store))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"cases": len(cases), "model_calls": report["model_calls"], "stopped_reason": report["stopped_reason"], "output": str(args.output)}))
        return 0
    except (ProviderError, ValueError, OSError, sqlite3.Error):
        print("The live evaluation could not finish. Check the workspace, provider settings and any saved result before another attempt.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
