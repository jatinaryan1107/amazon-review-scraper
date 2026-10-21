"""Small execution records stored with jobs, without prompts or credentials."""

from datetime import datetime
import re


VERSION = 1
MAX_EVENTS = 32
COUNTERS = {"calls", "cached_chunks", "chunks_done", "chunks_total", "pages_processed", "reviews_inserted", "selected_reviews"}


def seconds(start, end):
    try:
        value = (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
        return round(max(0, value), 3)
    except (ValueError, TypeError, AttributeError):
        return None


def stage(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,32}", value) else "unknown"


def advance(current, changes, timestamp):
    progress = current.get("progress") or {}
    saved = progress.get("_execution")
    events = list(saved.get("events", [])) if isinstance(saved, dict) and saved.get("version") == VERSION else []
    if not events:
        events = [{"phase": stage(current.get("phase")), "status": stage(current.get("status")),
                   "started_at": current["created_at"], "ended_at": None, "counters": {}}]
    phase = stage(changes.get("phase", current.get("phase")))
    status = stage(changes.get("status", current.get("status")))
    if (events[-1]["phase"], events[-1]["status"]) != (phase, status):
        events[-1] = {**events[-1], "ended_at": None if status == "interrupted" else timestamp}
        events.append({"phase": phase, "status": status, "started_at": timestamp, "ended_at": None, "counters": {}})
    counters = dict(events[-1].get("counters", {}))
    for key, value in changes.get("progress", {}).items():
        if key in COUNTERS and type(value) is int and 0 <= value <= 10_000_000:
            counters[key] = value
    result = changes.get("result")
    usage = result.get("usage") if isinstance(result, dict) else None
    if isinstance(usage, dict):
        for key in ("calls", "cached_chunks"):
            value = usage.get(key)
            if type(value) is int and 0 <= value <= 10_000_000:
                counters[key] = value
    events[-1] = {**events[-1], "counters": counters}
    if status not in {"queued", "running", "cancelling"}:
        events[-1]["ended_at"] = timestamp
    omitted = saved.get("omitted_events", 0) if isinstance(saved, dict) else 0
    if len(events) > MAX_EVENTS:
        omitted += len(events) - MAX_EVENTS
        events = [events[0], *events[-(MAX_EVENTS - 1):]]
    return {"version": VERSION, "events": events, "omitted_events": omitted}


def public_trace(value, job):
    if not isinstance(value, dict) or value.get("version") != VERSION or not isinstance(value.get("events"), list):
        return None
    events = []
    for item in value["events"][:MAX_EVENTS]:
        if not isinstance(item, dict) or seconds(item.get("started_at"), item.get("started_at")) is None:
            return None
        end = item.get("ended_at")
        counters = item.get("counters", {})
        if not isinstance(counters, dict) or (end is not None and seconds(item["started_at"], end) is None):
            return None
        events.append({"phase": stage(item.get("phase")), "status": stage(item.get("status")),
                       "started_at": item["started_at"], "ended_at": end,
                       "elapsed_seconds": seconds(item["started_at"], end),
                       "counters": {key: number for key, number in counters.items()
                                    if key in COUNTERS and type(number) is int and 0 <= number <= 10_000_000}})
    omitted = value.get("omitted_events", 0)
    return {"version": VERSION, "events": events,
            "elapsed_seconds": None if job.get("status") == "interrupted" else seconds(job.get("created_at"), job.get("updated_at")),
            "omitted_events": omitted if type(omitted) is int and 0 <= omitted <= 10_000_000 else 0}
