"""Explicit, bounded collection campaigns through the local Reviewdesk API."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from review_parser import extract_asin


ROOT = Path(__file__).resolve().parent
ACTIVE = {"queued", "running", "cancelling"}
TERMINAL = {"succeeded", "paused", "blocked", "failed", "cancelled", "interrupted"}
DONE = {"complete", "zero_reviews", "replay_limit"}


class CampaignError(ValueError):
    """The campaign cannot safely continue without review."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_manifest(value) -> list[dict]:
    entries = value.get("products") if isinstance(value, dict) else value
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise CampaignError("Supply a manifest containing one to 100 product entries.")
    products, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CampaignError("Each manifest product must be an object.")
        asin, url = entry.get("asin"), entry.get("url", entry.get("product_url"))
        title, source = entry.get("title", ""), entry.get("source_url", "")
        if (not isinstance(asin, str) or not isinstance(url, str) or len(url) > 2048
                or extract_asin(url) != asin or not re.fullmatch(r"[A-Z0-9]{10}", asin)):
            raise CampaignError("Each ASIN must match its HTTPS Amazon.in product URL.")
        if not isinstance(title, str) or len(title) > 1000 or not isinstance(source, str) or len(source) > 2048:
            raise CampaignError("Product titles and source URLs must be bounded text.")
        if source:
            try:
                parsed = urlsplit(source)
            except ValueError as exc:
                raise CampaignError("Product provenance must use an HTTPS Amazon.in source URL.") from exc
            if (parsed.scheme.lower() != "https" or parsed.netloc.lower() not in {"amazon.in", "www.amazon.in"}
                    or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in source)):
                raise CampaignError("Product provenance must use an HTTPS Amazon.in source URL.")
        if asin not in seen:
            products.append({"asin": asin, "url": f"https://www.amazon.in/dp/{asin}",
                             "title": title, "source_url": source})
            seen.add(asin)
    return products


def load_manifest(path: Path) -> list[dict]:
    with Path(path).open("rb") as handle:
        payload = handle.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise CampaignError("The product manifest must be no larger than 1 MiB.")
    try:
        return validate_manifest(json.loads(payload.decode("utf-8-sig")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CampaignError("The product manifest must be a UTF-8 JSON document.") from exc


def database_progress(path: Path, asins: list[str]) -> dict:
    """Read distinct live identities and resume state without creating any file."""
    path = Path(path)
    if not path.exists():
        return {"count": 0, "checkpoints": {}}
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        placeholders = ",".join("?" for _ in asins)
        count = connection.execute(
            f"SELECT COUNT(DISTINCT review_id) FROM reviews WHERE source='live' AND asin IN ({placeholders})", asins
        ).fetchone()[0]
        checkpoints = {row["asin"]: dict(row) for row in connection.execute(
            f"SELECT asin,status,pages_completed,replay_steps FROM checkpoints WHERE asin IN ({placeholders})", asins
        )}
        return {"count": count, "checkpoints": checkpoints}
    finally:
        connection.close()


class LocalAPI:
    """No redirects, proxies, external hosts, credentials, or POST retries."""

    def __init__(self, port: int = 8765, *, timeout: float = 10):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise CampaignError("Choose a local port from 1 to 65535.")
        self.port, self.timeout = port, timeout

    def request(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json", "X-Reviewdesk-Request": "1"}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise CampaignError("The local API response exceeded its size limit.")
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise CampaignError("The local API returned an unexpected response.")
            if response.status not in {200, 202}:
                raise CampaignError(f"Local API request failed ({response.status}): {str(result.get('error', 'Check Reviewdesk.'))[:500]}")
            return result
        except (UnicodeError, json.JSONDecodeError, http.client.HTTPException) as exc:
            raise CampaignError("The local API did not return valid JSON.") from exc
        finally:
            connection.close()

    def status(self):
        return self.request("GET", "/api/automation")

    def submit(self, payload):
        return self.request("POST", "/api/jobs", payload)

    def cancel(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise CampaignError("The local API supplied an invalid job identity.")
        return self.request("POST", f"/api/jobs/{job_id}/cancel", {})


@dataclass(frozen=True)
class Limits:
    target: int = 5000
    budget_seconds: float = 45 * 60
    max_pages: int = 1000

    def __post_init__(self):
        if isinstance(self.target, bool) or not isinstance(self.target, int) or not 1 <= self.target <= 5000:
            raise CampaignError("The distinct review target must be between 1 and 5,000.")
        if (isinstance(self.budget_seconds, bool) or not isinstance(self.budget_seconds, (int, float))
                or not math.isfinite(self.budget_seconds) or not 0 < self.budget_seconds <= 3600):
            raise CampaignError("The campaign time budget must be greater than zero and at most 60 minutes.")
        if isinstance(self.max_pages, bool) or not isinstance(self.max_pages, int) or not 1 <= self.max_pages <= 1000:
            raise CampaignError("The campaign page budget must be between 1 and 1,000.")


class Campaign:
    def __init__(self, products, db_path: Path, journal_dir: Path, *, api=None, limits=None,
                 progress_reader=None, clock=None, sleep=None, output=print):
        self.products = validate_manifest(products)
        self.asins = [product["asin"] for product in self.products]
        self.api, self.limits = api or LocalAPI(), limits or Limits()
        self.read_progress = progress_reader or (lambda: database_progress(db_path, self.asins))
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.output = output
        self.started = self.clock()
        self.deadline = self.started + self.limits.budget_seconds
        self.active_id = None
        self.cancelled_ids = set()
        self.queue = list(self.asins)
        self.path = Path(journal_dir) / f"campaign-{uuid.uuid4().hex}.json"
        self.state = {
            "version": 1, "run_id": self.path.stem, "created_at": now(), "updated_at": now(),
            "target": self.limits.target, "budget_seconds": self.limits.budget_seconds,
            "max_pages": self.limits.max_pages, "pages_processed": 0, "initial_count": None,
            "count": 0, "zero_new_batches": 0, "phase": "starting", "stop_reason": None,
            "candidates": [{**product, "status": "pending"} for product in self.products],
            "jobs": [], "cancel_requested_job_ids": [],
        }

    def save(self):
        self.state.update(updated_at=now(), elapsed_seconds=round(max(0, self.clock() - self.started), 3))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".campaign-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Windows readers can briefly hold the destination without sharing deletion.
            for attempt in range(5):
                try:
                    os.replace(temporary, self.path)
                    break
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                        raise
                    self.sleep(0.05 * 2 ** attempt)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def snapshot(self):
        progress = self.read_progress()
        count = progress.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise CampaignError("The review counter returned an invalid result.")
        self.state["count"] = count
        if self.state["initial_count"] is None:
            self.state["initial_count"] = count
        for candidate in self.state["candidates"]:
            checkpoint = progress.get("checkpoints", {}).get(candidate["asin"])
            if checkpoint:
                candidate["checkpoint"] = checkpoint
                candidate["status"] = checkpoint["status"]
                if checkpoint.get("replay_steps", 0) > 20 and checkpoint["status"] not in {"complete", "zero_reviews", "blocked", "failed", "partial"}:
                    candidate["status"] = "replay_limit"

    def stop(self, reason, detail=""):
        self.state.update(phase="stopped", stop_reason=reason, detail=detail)
        self.save()
        return self.state

    def cancel_own(self):
        if not self.active_id or self.active_id in self.cancelled_ids:
            return
        self.cancelled_ids.add(self.active_id)
        self.state["cancel_requested_job_ids"].append(self.active_id)
        try:
            self.save()
        except OSError as exc:
            self.state["journal_error"] = str(exc)[:1000]
        try:
            self.api.cancel(self.active_id)
        except Exception as exc:
            self.state["cancel_error"] = str(exc)[:1000]

    def checkpoint_stop(self):
        statuses = {candidate["status"] for candidate in self.state["candidates"]}
        if "blocked" in statuses:
            return "blocked"
        if statuses.intersection({"partial", "failed"}):
            return "review_required"
        return None

    def status(self):
        result = self.api.status()
        if (not isinstance(result, dict) or not isinstance(result.get("jobs", []), list)
                or (result.get("active_job") is not None and not isinstance(result["active_job"], dict))):
            raise CampaignError("The local API returned invalid automation status.")
        return result

    @staticmethod
    def job_from_status(status, job_id):
        jobs = [status.get("active_job"), *status.get("jobs", [])]
        for job in jobs:
            if isinstance(job, dict) and job.get("id") == job_id:
                return job
        raise CampaignError("The submitted job is missing from local automation status. Inspect Reviewdesk before continuing.")

    def run(self):
        self.save()
        try:
            status = self.status()
            if status.get("active_job"):
                return self.stop("active_job_present", "Another job is active. Wait for it to finish, then explicitly start a new campaign.")
            self.snapshot()
            self.save()
            while True:
                reason = self.checkpoint_stop()
                if reason:
                    return self.stop(reason, "Inspect saved product checkpoints before starting more collection.")
                if self.state["count"] >= self.limits.target:
                    return self.stop("target_reached")
                if self.clock() >= self.deadline:
                    return self.stop("time_budget")
                remaining_pages = self.limits.max_pages - self.state["pages_processed"]
                if remaining_pages <= 0:
                    return self.stop("page_budget")
                candidates = {item["asin"]: item for item in self.state["candidates"]}
                self.queue = [asin for asin in self.queue if candidates[asin]["status"] not in DONE]
                if not self.queue:
                    return self.stop("products_exhausted", "All selected products are complete, empty, or beyond the existing replay limit.")
                batch = self.queue[:min(10, remaining_pages)]
                pages = min(5, remaining_pages // len(batch))
                if self.status().get("active_job"):
                    return self.stop("active_job_present", "Another job started. This campaign will not compete with it.")
                if self.clock() >= self.deadline:
                    return self.stop("time_budget")
                payload = {"kind": "collection", "urls": [candidates[asin]["url"] for asin in batch],
                           "max_pages": pages, "resume": True, "transport": "browser", "analyse": True,
                           "analysis_method": "local", "allow_external": False}
                before = self.state["count"]
                self.state.update(phase="submitting", pending_batch=batch)
                self.save()
                response = self.api.submit(payload)
                job = response.get("job") if isinstance(response, dict) else None
                if not isinstance(job, dict):
                    raise CampaignError("Submission did not return a job. Inspect Reviewdesk before continuing.")
                job_id = job.get("id")
                if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
                    raise CampaignError("Submission did not return a valid job ID. Inspect Reviewdesk before continuing.")
                self.active_id = job_id
                record = {"id": job_id, "asins": batch, "max_pages": pages, "before_count": before, "status": job.get("status")}
                self.state["jobs"].append(record)
                self.state.update(phase="waiting", pending_batch=None)
                self.save()
                while job.get("status") in ACTIVE:
                    if self.clock() >= self.deadline:
                        self.state["stop_reason"] = "time_budget"
                        self.cancel_own()
                        self.snapshot()
                        return self.stop("time_budget", "Cancellation requested for this campaign's active job.")
                    self.sleep(max(0, min(2, self.deadline - self.clock())))
                    if self.clock() >= self.deadline:
                        continue
                    job = self.job_from_status(self.status(), job_id)
                    record.update(status=job.get("status"), progress=job.get("progress", {}))
                    self.save()
                self.active_id = None
                if job.get("status") not in TERMINAL:
                    raise CampaignError("The local job returned an unknown terminal status.")
                result = job.get("result") or {}
                if not isinstance(result, dict):
                    raise CampaignError("The local job returned invalid collection results.")
                reports = result.get("products", [])
                if not isinstance(reports, list):
                    raise CampaignError("The local job returned invalid product results.")
                processed = result.get("pages_processed", job.get("progress", {}).get("pages_processed", 0))
                if isinstance(processed, bool) or not isinstance(processed, int) or not 0 <= processed <= pages * len(batch):
                    raise CampaignError("The local job returned an invalid page count.")
                self.state["pages_processed"] += processed
                record.update(status=job["status"], products=reports, pages_processed=processed, detail=job.get("detail", ""))
                for report in reports:
                    if not isinstance(report, dict) or report.get("asin") not in batch:
                        raise CampaignError("The local job returned an unexpected product result.")
                    if report.get("status") not in {"complete", "zero_reviews", "paused", "partial", "failed", "blocked"}:
                        raise CampaignError("The local job returned an unknown product status.")
                    candidates[report["asin"]]["status"] = report.get("status", "failed")
                if (job["status"] in {"succeeded", "paused"}
                        and result.get("status") not in {"partial", "failed", "blocked"}
                        and not any(report["status"] in {"partial", "failed", "blocked"} for report in reports)
                        and {report["asin"] for report in reports} != set(batch)):
                    raise CampaignError("The finished job omitted product results. Inspect Reviewdesk before continuing.")
                self.snapshot()
                record["after_count"] = self.state["count"]
                delta = self.state["count"] - before
                self.state["zero_new_batches"] = 0 if delta > 0 else self.state["zero_new_batches"] + 1
                self.output(f"{self.state['count']} / {self.limits.target} distinct live reviews; {self.state['pages_processed']} pages; job {job_id} {job['status']}")
                self.save()
                if job["status"] == "blocked" or result.get("status") == "blocked" or any(item.get("status") == "blocked" for item in reports):
                    return self.stop("blocked", "Access was blocked. No challenge bypass or transport change will be attempted.")
                if job["status"] == "failed" or result.get("status") in {"partial", "failed"} or any(item.get("status") in {"partial", "failed"} for item in reports):
                    return self.stop("review_required", "A product finished partially or failed. Inspect it before continuing.")
                if job["status"] in {"cancelled", "interrupted"}:
                    return self.stop(job["status"])
                if self.state["count"] >= self.limits.target:
                    return self.stop("target_reached")
                if self.state["zero_new_batches"] >= 2:
                    return self.stop("no_new_reviews", "Two consecutive batches added no distinct live review IDs.")
                self.queue = self.queue[len(batch):] + batch
        except KeyboardInterrupt:
            if self.state["phase"] == "submitting" and not self.active_id:
                return self.stop("submission_uncertain", "Interrupted while submitting. The local job may still be running. Inspect Reviewdesk before explicitly starting another campaign.")
            self.state["stop_reason"] = "interrupted"
            self.cancel_own()
            try:
                self.snapshot()
            except (CampaignError, OSError, sqlite3.Error) as exc:
                self.state["counter_error"] = str(exc)[:1000]
            return self.stop("interrupted", "Stopped by the user. A restart requires another explicit CLI invocation.")
        except (CampaignError, OSError, sqlite3.Error) as exc:
            reason = "submission_uncertain" if self.state["phase"] == "submitting" and not self.active_id else "error"
            self.state["stop_reason"] = reason
            self.cancel_own()
            return self.stop(reason, str(exc)[:2000])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicitly collect a bounded review campaign through local Reviewdesk.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "reviews.sqlite3")
    parser.add_argument("--target", type=int, default=5000)
    parser.add_argument("--budget-minutes", type=float, default=45)
    parser.add_argument("--max-pages", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        campaign = Campaign(load_manifest(args.manifest), args.db, args.db.parent / "campaigns",
                            api=LocalAPI(args.port), limits=Limits(args.target, args.budget_minutes * 60, args.max_pages))
        print(f"Campaign journal: {campaign.path}", flush=True)
        result = campaign.run()
        print(f"Stopped: {result['stop_reason']}. {result['count']} distinct live reviews.", flush=True)
        return 0 if result["stop_reason"] == "target_reached" else (130 if result["stop_reason"] == "interrupted" else 1)
    except (CampaignError, OSError, sqlite3.Error) as exc:
        print(f"Campaign could not start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
