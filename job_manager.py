"""Durable, bounded local jobs. Browser work belongs to one worker thread."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from dashboard_data import DashboardData, REVIEW_COLUMNS
from review_parser import extract_asin
from llm_provider import ProviderError, public_diagnostics


ACTIVE = ("queued", "running", "cancelling")
TERMINAL = ("succeeded", "paused", "blocked", "failed", "cancelled", "interrupted")
LOG = logging.getLogger(__name__)
PROVIDER_MESSAGES = {
    "configuration": "Check the OpenRouter settings and try again.",
    "environment_key": "OpenRouter is configured by the server environment. Change its key there.",
    "storage": "Windows could not update the saved key. Existing settings were kept. Try again or use a session-only key.",
    "not_configured": "Add an OpenRouter key in Settings before using AI.",
    "invalid_request": "The AI request could not be accepted. Check the selected scope and settings.",
    "authorization": "OpenRouter could not authorize this request. Check the configured key.",
    "rate_limit": "The free AI service is limiting requests. Your daily allowance may still be available; local evidence remains usable.",
    "cooldown": "Free AI is cooling down after a provider limit. Local evidence remains available. Try AI again after the displayed wait.",
    "research_storage": "The local research cache or request ledger is unavailable. Related source reviews are shown; check the local data storage before another AI attempt.",
    "check_busy": "An account quota check is already running. Wait for its result.",
    "unavailable": "A compatible free model route is unavailable. The saved response does not identify why.",
    "budget": "The analysis call budget was reached. Completed chunks are saved.",
    "connection": "The provider request could not be completed. Inspect the job before trying again.",
    "timeout": "The free model took too long. No answer was accepted. This attempt still counts; check OpenRouter Logs before retrying.",
    "invalid_response": "The provider returned a response that could not be validated.",
    "invalid_json": "The model did not return valid JSON. No answer was saved; this attempt still counts.",
    "report_format": "The model response did not match the required report format. No answer was saved.",
    "evidence_reference": "The model cited missing or duplicate review evidence. No answer was saved.",
    "evidence_quote": "A model quote did not match its source review. No answer was saved.",
    "incomplete": "The provider did not finish its response. Completed chunks are saved.",
    "refusal": "The model declined this request. Local analysis remains available.",
    "daily_limit": "The daily free request limit was reached. Try again after the limit resets.",
    "cost_guard": "The provider reported a charge. Further AI requests are disabled for this server session.",
    "call_budget": "The per-job AI call budget was reached. Completed chunks are saved.",
}


def provider_failure(error):
    """Expose bounded provider diagnostics without exception or request content."""
    code = getattr(error, "code", "provider_error")
    if not isinstance(code, str) or code not in PROVIDER_MESSAGES:
        code = "provider_error"
    result = {"error_code": code}
    retry = getattr(error, "retry_after", None)
    if (not isinstance(retry, bool) and isinstance(retry, (int, float))
            and math.isfinite(retry) and 0 <= retry <= 86400):
        result["retry_after"] = math.ceil(retry)
    details = public_diagnostics(getattr(error, "diagnostics", None))
    if details:
        result["diagnostics"] = details
    message = PROVIDER_MESSAGES.get(code, "AI analysis could not finish. Completed chunks are retained.")
    if code == "evidence_reference":
        message = {
            "unknown_passage": "The model cited a passage label that was not supplied. No answer was saved.",
            "repeated_passage": "The model repeated one passage within a topic. No answer was saved.",
            "same_review": "The model used multiple passages from one review as separate sources within a topic. No answer was saved.",
        }.get(details.get("evidence_reason"), message)
    if code == "rate_limit" and details.get("rate_limit_source") == "provider":
        message = "The model provider is limiting requests. Your daily OpenRouter allowance may still be available."
    elif code == "rate_limit" and details.get("rate_limit_source") == "openrouter":
        message = "OpenRouter blocked this request under a platform rate limit. Check the account counter and reset details."
    if code in {"unavailable", "invalid_request"}:
        message = {
            "privacy_policy": "OpenRouter reported a data-policy routing restriction. Check account and key policies before another run.",
            "required_parameters": "OpenRouter reported no route supporting the required parameters. Review the response format and requested features.",
            "region_policy": "OpenRouter reported a region routing restriction. Review the account and provider region requirements.",
            "model_unavailable": "OpenRouter reported that the requested model is unavailable.",
            "no_matching_endpoint": "OpenRouter reported no matching endpoint, without identifying which requirement excluded it.",
        }.get(details.get("routing_reason"), message)
    return message, result


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JobConflict(ValueError):
    """A new action conflicts with a running job."""


class WorkerLock:
    """Keep two dashboard processes from collecting into one data folder."""

    def __init__(self, path: Path):
        self.file = path.open("a+b")
        try:
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise JobConflict("Another Reviewdesk server is already using this data folder.") from exc

    def close(self):
        if not self.file.closed:
            self.file.close()


class AutomationService:
    """Queue one job at a time and retain reports across server restarts.

    Runtime functions are injected in tests. Constructing the service never
    opens a browser, reads cookie values, or calls an analysis provider.
    """

    def __init__(self, data: DashboardData, auth_file: Path, *, collection_runner=None,
                 auth_runner=None, local_analyser=None, ai_analyser=None,
                 settings=None, session_reader=None, autostart=True,
                 provider_registry=None, llm_engine=None):
        self.data = data
        self.auth_file = Path(auth_file)
        self.path = data.data_dir / "jobs.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._worker_lock = WorkerLock(data.data_dir / "worker.lock")
        self._mutex = threading.RLock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._cancel = threading.Event()
        self._finish = threading.Event()
        self._active_id = None
        self._collection_runner = collection_runner
        self._auth_runner = auth_runner
        self._local_analyser = local_analyser
        self._ai_analyser = ai_analyser
        self._settings = settings
        self._session_reader = session_reader
        self._thread = None
        self._provider_registry = provider_registry
        self._llm_engine = llm_engine
        self._analysis_store = None
        self._provider_mutex = threading.RLock()
        try:
            self._initialize()
            if llm_engine is None:
                from analysis_store import AnalysisStore
                self._analysis_store = AnalysisStore(data.data_dir / "analysis.sqlite3")
            if autostart:
                self._thread = threading.Thread(target=self._worker, name="reviewdesk-worker", daemon=True)
                self._thread.start()
        except BaseException:
            self._worker_lock.close()
            raise

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version not in (0, 1) or tables - {"jobs", "reports"}:
                raise ValueError("The local jobs database has an unsupported format.")
            required = {
                "jobs": {"id", "kind", "status", "phase", "options", "progress", "result", "error", "detail", "created_at", "updated_at"},
                "reports": {"id", "dataset", "asin", "method", "fingerprint", "model", "report", "created_at"},
            }
            # Validate before any DDL so an unrelated or damaged database is
            # rejected without adding tables, indexes, or a schema version.
            for table in tables:
                columns = list(connection.execute(f"PRAGMA table_info({table})"))
                if ({column["name"] for column in columns} != required[table]
                        or any(column["type"].upper() != "TEXT" for column in columns)
                        or [(column["name"], column["pk"]) for column in columns if column["pk"]] != [("id", 1)]):
                    raise ValueError("The local jobs database has an unsupported format.")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    phase TEXT NOT NULL, options TEXT NOT NULL, progress TEXT NOT NULL,
                    result TEXT, error TEXT, detail TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs((1))
                    WHERE status IN ('queued', 'running', 'cancelling');
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, dataset TEXT NOT NULL, asin TEXT NOT NULL,
                    method TEXT NOT NULL, fingerprint TEXT NOT NULL, model TEXT NOT NULL,
                    report TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reports_scope ON reports(dataset, asin, created_at DESC);
                PRAGMA user_version = 1;
            """)
            connection.execute(
                "UPDATE jobs SET status='interrupted', phase='finished', detail=?, updated_at=? "
                "WHERE status IN ('queued', 'running', 'cancelling')",
                ("The server stopped before this job finished. Start a new job to resume saved progress.", now()),
            )

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("options", "progress", "result"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def get_job(self, job_id):
        with self._connection() as connection:
            return self._decode(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def _update(self, job_id, **changes):
        allowed = {"status", "phase", "progress", "result", "error", "detail"}
        if set(changes) - allowed:
            raise ValueError("Unsupported job update.")
        values = {key: json.dumps(value, ensure_ascii=False) if key in {"progress", "result"} else value
                  for key, value in changes.items()}
        values["updated_at"] = now()
        with self._mutex, self._connection() as connection:
            current = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if current and current["status"] == "cancelling" and values.get("status") in TERMINAL:
                values.update(status="cancelled", detail="Job stopped. Saved reviews and reports are retained.")
            connection.execute("UPDATE jobs SET " + ", ".join(f"{key}=?" for key in values) + " WHERE id=?",
                               [*values.values(), job_id])

    def provider_settings(self):
        if self._settings:
            return self._settings()
        from review_analysis import provider_settings
        return provider_settings()

    def _registry(self):
        with self._provider_mutex:
            if self._provider_registry is None:
                from llm_provider import ProviderRegistry
                from credential_store import system_credential_store
                self._provider_registry = ProviderRegistry(credential_store=system_credential_store(self.data.data_dir))
            return self._provider_registry

    def provider_metadata(self):
        public = self._registry().public()
        source = public.get("key_source", "none")
        remaining, retry_at = None, None
        if self._analysis_store:
            try:
                remaining = self._analysis_store.daily_remaining()
                retry_at = self._analysis_store.retry_at()
            except (OSError, sqlite3.Error, ValueError):
                pass
        return {"provider": "openrouter", "configured": public.get("configured") is True,
                "key_source": source if isinstance(source, str) and source in {"environment", "session", "saved", "none"} else "none",
                "remember_key": public.get("remember_key") is True,
                "remember_available": public.get("remember_available") is True,
                "storage_error": public.get("storage_error") is True,
                "quota_snapshot": public.get("quota_snapshot"),
                "app_daily_remaining": remaining, "retry_at": retry_at,
                "model": "openrouter/free", "free_only": True, "privacy": "deny_training",
                "daily_limit": 50, "require_zdr": public.get("require_zdr") is True,
                "response_mode": "prompt" if public.get("response_mode") == "prompt" else "schema",
                "disabled": public.get("disabled") is True}

    def configure_provider(self, payload):
        if not isinstance(payload, dict) or not payload or set(payload) - {"api_key", "clear", "require_zdr", "response_mode", "remember_key"}:
            raise ValueError("Supply an OpenRouter key, clear the session key, or change the privacy setting.")
        if "require_zdr" in payload and not isinstance(payload["require_zdr"], bool):
            raise ValueError("The zero data retention setting must be true or false.")
        if "response_mode" in payload and (not isinstance(payload["response_mode"], str) or payload["response_mode"] not in {"prompt", "schema"}):
            raise ValueError("Choose compatible JSON or provider-enforced JSON.")
        if "clear" in payload and payload["clear"] is not True:
            raise ValueError("Clear must be true to forget the session key.")
        if "api_key" in payload:
            key = payload["api_key"]
            if (not isinstance(key, str) or not key.strip() or len(key) > 4096
                    or any(ord(character) < 32 or ord(character) == 127 for character in key)):
                raise ValueError("Supply a nonempty API key without control characters.")
            if "clear" in payload:
                raise ValueError("Choose either saving a key or clearing it.")
        with self._mutex:
            with self._connection() as connection:
                active = self._decode(connection.execute(
                    "SELECT * FROM jobs WHERE status IN ('queued','running','cancelling') LIMIT 1"
                ).fetchone())
            if active and (active["kind"] in {"research", "brief", "comparison"} or active["options"].get("analysis_method") == "openrouter"):
                raise JobConflict("Stop the active AI job before changing its provider settings.")
            try:
                self._registry().configure(payload)
                return self.provider_metadata()
            except ProviderError:
                raise
            except Exception:
                raise ValueError("Provider settings could not be updated. Check the settings and try again.") from None

    def check_provider_quota(self):
        result = self._registry().check_quota()
        return {**result, "app_daily_limit": 50,
                "app_daily_remaining": self._analysis_store.daily_remaining() if self._analysis_store else None}

    def _engine(self):
        with self._provider_mutex:
            if self._llm_engine is None:
                from llm_analysis import LLMEngine
                self._llm_engine = LLMEngine(self._registry(), self._analysis_store)
            return self._llm_engine

    @staticmethod
    def _scope(dataset, asin):
        if not isinstance(dataset, str) or dataset not in {"demo", "workspace"} or not isinstance(asin, str):
            raise ValueError("Choose a valid dataset and product scope.")
        if asin and extract_asin(f"https://www.amazon.in/dp/{asin}") != asin:
            raise ValueError("Choose a valid product ASIN.")

    def analysis_plan(self, dataset="workspace", asin="", depth="quick"):
        self._scope(dataset, asin)
        if not isinstance(depth, str) or depth not in {"quick", "deep"}:
            raise ValueError("Choose quick or deep analysis.")
        return self._engine().plan(self._rows(dataset, asin), dataset=dataset, asin=asin, depth=depth)

    def status(self):
        from collection_runtime import session_status
        reader = self._session_reader or session_status
        session = reader(self.auth_file)
        with self._connection() as connection:
            jobs = [self._decode(row) for row in connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT 12")]
            active = self._decode(connection.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running','cancelling') LIMIT 1").fetchone())
            last_collection = connection.execute(
                "SELECT rowid AS sequence, status, updated_at FROM jobs WHERE kind='collection' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
            last_auth = connection.execute(
                "SELECT rowid AS sequence, updated_at FROM jobs WHERE kind='auth' AND status='succeeded' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        auth_status = "saved" if session.get("valid") else ("invalid" if session.get("exists") else "missing")
        if (last_collection and last_collection["status"] == "blocked"
                and (not last_auth or (last_auth["updated_at"], last_auth["sequence"])
                     < (last_collection["updated_at"], last_collection["sequence"]))):
            auth_status = "blocked"
        settings = self.provider_settings()
        router = self.provider_metadata()
        try:
            installed = importlib.util.find_spec("playwright") is not None
        except (ValueError, ImportError):
            installed = False
        return {
            "auth": {"status": auth_status, "detail": session.get("detail", "Session validity is checked during collection."),
                     "busy": bool(active and active["kind"] == "auth"), "verified": False},
            "capabilities": {"playwright_installed": installed, "openai_configured": settings.get("configured", False),
                             "model": settings.get("model", ""), "openrouter_configured": router["configured"],
                             "openrouter_model": router["model"], "openrouter_disabled": router["disabled"], "free_only": True},
            "jobs": jobs, "active_job": active,
        }

    def _validate(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Supply a job as a JSON object.")
        kind = payload.get("kind")
        allowed = {
            "collection": {"kind", "urls", "max_pages", "resume", "transport", "analyse", "analysis_method", "allow_external"},
            "analysis": {"kind", "dataset", "asin", "analysis_method", "allow_external", "depth"},
            "research": {"kind", "dataset", "asin", "question", "allow_external", "research_mode"},
            "brief": {"kind", "dataset", "products", "priorities", "allow_external"},
            "comparison": {"kind", "dataset", "products", "allow_external", "collect_missing", "max_pages", "target_reviews", "focus"},
            "auth": {"kind"},
        }
        if not isinstance(kind, str) or kind not in allowed or set(payload) - allowed[kind]:
            raise ValueError("Choose a supported job and options.")
        if kind == "auth":
            return kind, {}
        method = payload.get("analysis_method", "local")
        if not isinstance(method, str) or method not in {"local", "openai", "openrouter"}:
            raise ValueError("Choose local analysis or a supported optional AI provider.")
        external = payload.get("allow_external", False)
        if not isinstance(external, bool):
            raise ValueError("External analysis consent must be true or false.")
        if kind == "brief":
            from buying_brief import validate_options
            options = validate_options(payload.get("dataset", "workspace"), payload.get("products"), payload.get("priorities", []))
            return kind, {**options, "allow_external": external}
        if kind == "comparison":
            from product_comparison import validate_options
            options = validate_options(payload.get("dataset", "workspace"), payload.get("products"),
                payload.get("collect_missing", False), payload.get("max_pages", 3), payload.get("target_reviews", 0), payload.get("focus", ""))
            return kind, {**options, "allow_external": external}
        if kind == "research":
            from llm_analysis import can_answer_locally
            dataset, asin, question = payload.get("dataset", "workspace"), payload.get("asin", ""), payload.get("question")
            self._scope(dataset, asin)
            if not isinstance(question, str) or not question.strip() or len(question) > 1500:
                raise ValueError("Ask a nonempty question with at most 1,500 characters.")
            mode = payload.get("research_mode", "focused")
            if not isinstance(mode, str) or mode not in {"focused", "agent"}:
                raise ValueError("Choose focused research or agent investigation.")
            return kind, {"dataset": dataset, "asin": asin, "question": question.strip(), "allow_external": external, "research_mode": mode}
        if method == "openrouter":
            if kind != "analysis":
                raise ValueError("Automatic collection reports use local analysis. Choose AI separately in Analysis.")
            if not self.provider_metadata()["configured"]:
                raise ValueError("Configure OpenRouter in Settings before using AI analysis.")
            if not external:
                raise ValueError("Confirm that the selected review text may be sent to OpenRouter.")
        if "depth" in payload and method != "openrouter":
            raise ValueError("Analysis depth applies only to OpenRouter analysis.")
        if method == "openai":
            if kind == "collection":
                raise ValueError("Automatic collection reports use local analysis. Choose OpenAI separately in Analysis.")
            if not external:
                raise ValueError("Confirm that the selected review text may be sent to OpenAI.")
            if not self.provider_settings().get("configured"):
                raise ValueError("Optional OpenAI analysis needs OPENAI_API_KEY and OPENAI_MODEL in the server environment.")
        if kind == "analysis":
            dataset, asin = payload.get("dataset", "workspace"), payload.get("asin", "")
            self._scope(dataset, asin)
            options = {"dataset": dataset, "asin": asin, "analysis_method": method, "allow_external": external}
            if method == "openrouter":
                depth = payload.get("depth", "quick")
                if not isinstance(depth, str) or depth not in {"quick", "deep"}:
                    raise ValueError("Choose quick or deep analysis.")
                options["depth"] = depth
            return kind, options
        urls = payload.get("urls")
        if not isinstance(urls, list) or not 1 <= len(urls) <= 10:
            raise ValueError("Add between one and ten Amazon India product URLs.")
        normalized, seen = [], set()
        for url in urls:
            if not isinstance(url, str) or len(url) > 2048:
                raise ValueError("Each product URL must be text with at most 2,048 characters.")
            asin = extract_asin(url.strip())
            if asin is None:
                raise ValueError("Use HTTPS Amazon.in product URLs containing a valid ASIN.")
            if asin not in seen:
                seen.add(asin)
                normalized.append(f"https://www.amazon.in/dp/{asin}")
        pages = payload.get("max_pages", 3)
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 20 or pages * len(normalized) > 50:
            raise ValueError("Use 1 to 20 pages per product, with at most 50 pages across the job.")
        resume, analyse = payload.get("resume", True), payload.get("analyse", True)
        if not isinstance(resume, bool) or not isinstance(analyse, bool):
            raise ValueError("Resume and automatic analysis must be true or false.")
        transport = payload.get("transport", "browser")
        if not isinstance(transport, str) or transport not in {"browser", "hybrid"}:
            raise ValueError("Choose browser collection or the experimental hybrid transport.")
        return kind, {"urls": normalized, "max_pages": pages, "resume": resume, "transport": transport,
                      "analyse": analyse, "analysis_method": "local", "allow_external": False}

    def submit(self, payload):
        kind, options = self._validate(payload)
        with self._mutex:
            if self._shutdown.is_set():
                raise JobConflict("The server is stopping. Start it again before creating a job.")
            timestamp, job_id = now(), uuid.uuid4().hex
            progress = {"current_asin": "", "products_done": 0, "products_total": len(options.get("urls", [])),
                        "pages_processed": 0, "reviews_inserted": 0}
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','cancelling')").fetchone():
                    raise JobConflict("A job is already active. Wait for it to finish or stop it first.")
                connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    job_id, kind, "queued", "queued", json.dumps(options), json.dumps(progress), None, None,
                    "Waiting for the local worker.", timestamp, timestamp,
                ))
            self._wake.set()
            return self.get_job(job_id)

    def cancel(self, job_id):
        with self._mutex:
            job = self.get_job(job_id)
            if job is None:
                raise LookupError("This job does not exist.")
            if job["status"] in TERMINAL:
                return job
            if job["status"] == "queued":
                self._update(job_id, status="cancelled", phase="finished", detail="Stopped before the job started.")
            else:
                self._update(job_id, status="cancelling", detail="Stopping after the current operation. Saved pages are retained.")
                if self._active_id == job_id:
                    self._cancel.set()
            return self.get_job(job_id)

    def finish_auth(self, job_id):
        with self._mutex:
            job = self.get_job(job_id)
            if job is None:
                raise LookupError("This sign-in job does not exist.")
            if job["kind"] != "auth" or job["status"] != "running" or job["phase"] != "awaiting_login":
                raise JobConflict("There is no sign-in window ready to save for this job.")
            if self._active_id != job_id:
                raise JobConflict("The sign-in worker is no longer active.")
            self._finish.set()
            return job

    def _rows(self, dataset, asin=""):
        with self.data.connection(dataset) as connection:
            if connection is None:
                return []
            clause = " WHERE asin=?" if asin else ""
            return [dict(row) for row in connection.execute(
                f"SELECT {REVIEW_COLUMNS} FROM reviews{clause} ORDER BY asin, review_id", [asin] if asin else [])]

    def latest_report(self, dataset, asin=""):
        from review_analysis import report_fingerprint
        self.data.path(dataset)
        if asin and extract_asin(f"https://www.amazon.in/dp/{asin}") != asin:
            raise ValueError("Choose a valid product ASIN.")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM reports WHERE dataset=? AND asin=? ORDER BY created_at DESC, id DESC LIMIT 1",
                                     (dataset, asin)).fetchone()
        if row is None:
            return {"report": None, "stale": False}
        report = json.loads(row["report"])
        current = report_fingerprint(self._rows(dataset, asin), dataset=dataset, asin=asin)
        return {"report": report, "stale": current != row["fingerprint"]}

    def _llm_progress(self, job_id, phase):
        progress = dict(self.get_job(job_id)["progress"])
        def update(event):
            if not isinstance(event, dict):
                return
            for key, value in event.items():
                if (isinstance(key, str) and len(key) <= 48 and key.replace("_", "").isalnum()
                        and not isinstance(value, bool) and isinstance(value, (int, float))
                        and math.isfinite(value) and 0 <= value <= 10**12):
                    progress[key] = value
            self._update(job_id, phase=phase, progress=progress,
                         detail="Inspecting review evidence." if phase == "researching" else "Analyzing saved review chunks.")
        return update

    @staticmethod
    def _router_report_complete(report, depth):
        scope = report.get("scope", {})
        return scope.get("analysis_complete") is True and scope.get("partial") is not True

    def _analyse(self, job_id, dataset, asin, method, depth="quick"):
        from review_analysis import analyse_reviews, analyse_with_openai, report_fingerprint
        rows = self._rows(dataset, asin)
        if not rows:
            raise ValueError("There are no reviews in this scope yet. Collect or import reviews first.")
        fingerprint = report_fingerprint(rows, dataset=dataset, asin=asin)
        settings = self.provider_settings()
        model = settings.get("model", "") if method == "openai" else "openrouter/free" if method == "openrouter" else ""
        cache_method = self._engine().cache_discriminator(depth) if method == "openrouter" else method
        with self._connection() as connection:
            matches = connection.execute(
                "SELECT id,report FROM reports WHERE dataset=? AND asin=? AND method=? AND fingerprint=? AND model=? "
                "ORDER BY created_at DESC, id DESC", (dataset, asin, cache_method, fingerprint, model)).fetchall()
        cached = next((row for row in matches if method != "openrouter" or
                       self._router_report_complete(json.loads(row["report"]), depth)), None)
        if cached:
            with self._connection() as connection:
                connection.execute("UPDATE reports SET created_at=? WHERE id=?", (now(), cached["id"]))
            return cached["id"], True
        self._update(job_id, phase="analysing", detail="Analyzing stored review evidence." if method == "local" else "Analyzing selected review evidence with AI.")
        if method == "local":
            report = (self._local_analyser or analyse_reviews)(rows, dataset=dataset, asin=asin)
        elif method == "openrouter":
            report = self._engine().analyse(rows, dataset=dataset, asin=asin, depth=depth,
                                            cancelled=self._cancel.is_set,
                                            on_progress=self._llm_progress(job_id, "analysing"))
        else:
            report = (self._ai_analyser or analyse_with_openai)(rows, dataset=dataset, asin=asin, model=model, cancelled=self._cancel.is_set)
        if self._cancel.is_set() or self._shutdown.is_set():
            return None, False
        if method == "openrouter" and isinstance(report.get("pause_reason"), dict):
            pause = report["pause_reason"]
            message, diagnostics = provider_failure(ProviderError("Analysis paused.", code=pause.get("code"), retry_after=pause.get("retry_after"), diagnostics=pause.get("diagnostics")))
            report["pause_reason"] = {"code": diagnostics["error_code"], "message": message,
                                      "retry_after": diagnostics.get("retry_after"),
                                      **({"diagnostics": diagnostics["diagnostics"]} if "diagnostics" in diagnostics else {})}
        report_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("INSERT INTO reports VALUES (?,?,?,?,?,?,?,?)", (
                report_id, dataset, asin, cache_method, fingerprint, model, json.dumps(report, ensure_ascii=False, allow_nan=False), now()))
        return report_id, False

    def _brief(self, job_id, options):
        from buying_brief import build_brief
        self._update(job_id, phase="researching", detail="Finding owner evidence for your priorities.")
        rows = [row for asin in options["asins"] for row in self._rows(options["dataset"], asin)]
        result = build_brief(self._engine(), rows, dataset=options["dataset"], products=options["asins"],
            priorities=options["priorities"], allow_external=options["allow_external"], cancelled=self._cancel.is_set,
            on_progress=lambda event: self._update(job_id, phase="analysing", detail="Writing your buying brief from selected owner evidence.",
                progress={"selected_reviews": event["selected_reviews"]}))
        if self._cancel.is_set() or self._shutdown.is_set():
            self._update(job_id, status="cancelled", phase="finished", detail="Buying brief stopped. Saved reviews remain available.")
            return
        names = {product["asin"]: product for product in self.data.overview(options["dataset"])["products"]}
        for product in result["products"]:
            info = names.get(product["asin"], {})
            product.update(name=info.get("name", ""), last_review_added_at=info.get("last_review_added_at"))
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 131072:
            raise ValueError("The buying brief exceeded its saved result limit.")
        self._update(job_id, status="succeeded", phase="finished", result=result,
            detail="Your buying brief is ready. Check the owner evidence behind each finding." if result["method"] == "openrouter" else "Owner evidence is ready. AI interpretation is not included.")

    def comparison_preview(self, payload):
        from collection_runtime import session_status
        from product_comparison import collection_plan
        kind, options = self._validate({**payload, "kind": "comparison"})
        overview = self.data.overview(options["dataset"])
        saved = {item["asin"]: item for item in overview["products"]}
        ended = {item["asin"] for item in overview["checkpoints"] if item["status"] in {"complete", "zero_reviews"}}
        for asin in ended:
            saved.setdefault(asin, {})["collection_complete"] = True
        planned = collection_plan(options, saved)
        session = (self._session_reader or session_status)(self.auth_file)
        products = []
        for asin, url in zip(options["asins"], options["urls"]):
            info = saved.get(asin, {})
            step = next((step for step in planned if step["asin"] == asin), None)
            products.append({"asin": asin, "url": url, "name": info.get("name", ""), "stored_reviews": info.get("review_count", 0),
                "last_added_at": info.get("last_review_added_at"), "planned_pages": step["pages"] if step else 0, "available_pages_ended": asin in ended})
        return {"products": products, "target_reviews": options["target_reviews"], "new_pages_maximum": sum(step["pages"] for step in planned),
            "needs_session": bool(planned) and not session.get("valid", False), "collection_planned": bool(planned),
            "session_saved": bool(session.get("valid")), "ai_configured": self.provider_metadata()["configured"],
            "model_calls": 0, "detail": "This checks local saved data only. No Amazon or AI request was made."}

    def _comparison(self, job_id, options):
        from collection_runtime import run_collection
        from product_comparison import compare_products, collection_plan
        started = time.monotonic()
        dataset = options["dataset"]
        collection, collection_seconds = None, 0.0
        overview = self.data.overview(dataset)
        saved = {item["asin"]: item for item in overview["products"]}
        for item in overview["checkpoints"]:
            if item["status"] in {"complete", "zero_reviews"}:
                saved.setdefault(item["asin"], {})["collection_complete"] = True
        planned = collection_plan(options, saved)
        if planned:
            self._update(job_id, phase="collecting", detail="Gathering reviews toward your sample target.")
            collection_started = time.monotonic()
            runner = self._collection_runner or run_collection
            collection = {"status": "completed", "products": [], "inserted": 0, "pages_processed": 0, "detail": ""}
            # Group equal page budgets to retain one browser session when possible.
            budgets = list(dict.fromkeys(step["pages"] for step in planned))
            for pages in budgets:
                if self._cancel.is_set() or self._shutdown.is_set():
                    break
                batch = runner([step["url"] for step in planned if step["pages"] == pages], self.data.path("workspace"), self.auth_file,
                    max_pages=pages, resume=True, transport="browser", cancelled=self._cancel.is_set,
                    on_progress=lambda event: self._update(job_id, phase="collecting", detail=event.get("detail") or "Saving reviews for comparison.",
                        progress={key: event[key] for key in ("asin", "pages_processed", "inserted", "status") if key in event}))
                collection["products"].extend(batch.get("products", []))
                for field in ("inserted", "pages_processed"):
                    collection[field] += batch.get(field, 0)
                if batch.get("status") != "completed":
                    collection["status"] = batch.get("status", "failed")
                    collection["detail"] = batch.get("detail", "")
                if batch.get("status") in {"blocked", "failed"} or batch.get("cancelled"):
                    break
            collection_seconds = time.monotonic() - collection_started
        if self._cancel.is_set() or self._shutdown.is_set():
            self._update(job_id, status="cancelled", phase="finished", result={"collection": collection,
                "timings": {"collection_seconds": round(collection_seconds, 3), "total_seconds": round(time.monotonic() - started, 3)}},
                detail="Comparison stopped. Collected pages remain saved.")
            return
        names = {item["asin"]: item.get("name", "") for item in self.data.overview(dataset)["products"]}
        rows = [row for asin in options["asins"] for row in self._rows(dataset, asin)]
        self._update(job_id, phase="researching", detail="Preparing separate owner evidence for each product.")
        result = compare_products(self._engine(), rows, dataset=dataset, products=options["asins"], names=names,
            allow_external=options["allow_external"], focus=options.get("focus", ""), cancelled=self._cancel.is_set,
            on_progress=lambda event: self._update(job_id, phase="analysing", detail="Comparing owner experiences in one free AI request.", progress=event))
        result["collection"] = collection
        result["timings"].update(collection_seconds=round(collection_seconds, 3), total_seconds=round(time.monotonic() - started, 3))
        result["collection_mode"] = "collected_missing" if collection else "saved_reviews"
        target = options.get("target_reviews", 0)
        result["coverage"] = {"target_reviews": target, "target_met": all(item["stored_reviews"] >= target for item in result["products"]),
            "needs_more": [item["asin"] for item in result["products"] if target and item["stored_reviews"] < target]}
        ended = {item["asin"] for item in self.data.overview(dataset)["checkpoints"] if item["status"] in {"complete", "zero_reviews"}}
        if ended.intersection(options["asins"]):
            result["coverage"]["available_pages_ended"] = sorted(ended.intersection(options["asins"]))
        incomplete = any(not item["selected_reviews"] for item in result["products"])
        status = "blocked" if incomplete and collection and collection.get("status") == "blocked" else "succeeded"
        if self._cancel.is_set() or self._shutdown.is_set():
            status = "cancelled"
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 131072:
            raise ValueError("The comparison exceeded its saved result limit.")
        self._update(job_id, status=status, phase="finished", result=result,
            detail="Comparison stopped." if status == "cancelled" else
                "Collection needs attention. No AI comparison was made for missing evidence." if incomplete else
                "Local owner evidence is ready. The AI response was not accepted; inspect its notice before retrying." if result.get("notice") else
                "Your comparison is ready. Inspect the quotes and measured timings.")

    def _research(self, job_id, options):
        self._update(job_id, phase="researching", detail="Inspecting saved review evidence.")
        answer = self._engine().ask(self._rows(options["dataset"], options["asin"]), options["question"],
                                    dataset=options["dataset"], asin=options["asin"],
                                    allow_external=options["allow_external"], strategy=options.get("research_mode", "focused"),
                                    cancelled=self._cancel.is_set,
                                    on_progress=self._llm_progress(job_id, "researching"))
        if self._cancel.is_set() or self._shutdown.is_set():
            self._update(job_id, status="cancelled", phase="finished", detail="Research stopped. Saved analysis chunks are retained.")
            return
        fields = {"question", "answer", "evidence", "limitations", "scope", "usage", "steps", "method", "model", "fingerprint", "notice", "research_mode", "cache_hit"}
        if not isinstance(answer, dict):
            raise ValueError("Research did not return a valid answer.")
        result = {key: value for key, value in answer.items() if key in fields}
        if (not isinstance(result.get("answer"), str) or len(result["answer"]) > 16000
                or not isinstance(result.get("evidence"), list) or len(result["evidence"]) > 32
                or not isinstance(result.get("steps"), list) or len(result["steps"]) > 16
                or len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 131072):
            raise ValueError("Research exceeded its answer limits. No answer was saved.")
        result["question"] = options["question"]
        self._update(job_id, status="succeeded", phase="finished", result=result,
                     detail="Local evidence is ready. No AI conclusion was saved." if result.get("method") == "local_evidence" else "The answer is ready. Open its source reviews to check the evidence.")

    def _run(self, job):
        job_id, options = job["id"], job["options"]
        kind = job["kind"]
        if kind == "auth":
            from collection_runtime import run_auth
            runner = self._auth_runner or run_auth
            def auth_progress(event):
                self._update(job_id, phase="awaiting_login" if event.get("status") == "awaiting_login" else "signing_in",
                             detail=event.get("detail", "Complete sign-in in the browser, then save the session here."))
            result = runner(self.auth_file, self._finish.is_set, self._cancel.is_set, on_progress=auth_progress)
            status = "cancelled" if self._cancel.is_set() else {
                "saved": "succeeded", "completed": "succeeded", "succeeded": "succeeded",
                "failed": "failed", "paused": "paused", "blocked": "blocked",
            }.get(result.get("status"), "failed")
            self._update(job_id, status=status, phase="finished", result=result,
                         detail=result.get("detail", "Session saved. Its validity is checked during collection."))
            return
        if kind == "analysis":
            depth = options.get("depth", "quick")
            report_id, cached = self._analyse(job_id, options["dataset"], options["asin"], options["analysis_method"], depth)
            cancelled = self._cancel.is_set()
            partial = False
            if report_id and options["analysis_method"] == "openrouter":
                with self._connection() as connection:
                    report = json.loads(connection.execute("SELECT report FROM reports WHERE id=?", (report_id,)).fetchone()[0])
                partial = not self._router_report_complete(report, depth)
            detail = "Analysis stopped." if cancelled else (
                "Analysis paused with completed chunks saved. Start another job to continue." if partial else
                "The saved report is still current. Reused it without a provider call." if cached else
                "Analysis is ready. Open Analysis to inspect its evidence.")
            result = {"analysis_id": report_id, "cached": cached}
            if options["analysis_method"] == "openrouter":
                result["partial"] = partial
                if partial and isinstance(report.get("pause_reason"), dict):
                    pause = report["pause_reason"]
                    detail, diagnostics = provider_failure(ProviderError("Analysis paused.", code=pause.get("code"), retry_after=pause.get("retry_after")))
                    result.update(diagnostics)
            self._update(job_id, status="cancelled" if cancelled else "paused" if partial else "succeeded", phase="finished",
                         result=result, detail=detail)
            return
        if kind == "brief":
            self._brief(job_id, options)
            return
        if kind == "comparison":
            self._comparison(job_id, options)
            return
        if kind == "research":
            self._research(job_id, options)
            return
        from collection_runtime import run_collection
        runner = self._collection_runner or run_collection
        current_progress = dict(job["progress"])
        per_product = {}
        finished_products = set()
        def progress(event):
            asin = event.get("asin", "")
            if asin:
                per_product[asin] = (int(event.get("pages_processed", 0)), int(event.get("inserted", 0)))
                current_progress["current_asin"] = asin
                if event.get("status") in {"complete", "zero_reviews", "paused", "blocked", "failed", "partial"}:
                    finished_products.add(asin)
            current_progress.update(products_done=len(finished_products),
                                    pages_processed=sum(value[0] for value in per_product.values()),
                                    reviews_inserted=sum(value[1] for value in per_product.values()))
            self._update(job_id, phase="collecting", progress=current_progress,
                         detail=event.get("detail", "Collecting reviews and saving progress."))
        self._update(job_id, phase="collecting", detail="Opening the collection session.")
        result = runner(options["urls"], self.data.path("workspace"), self.auth_file,
                        max_pages=options["max_pages"], resume=options["resume"],
                        transport=options["transport"], on_progress=progress, cancelled=self._cancel.is_set)
        reports = result.get("products", [])
        current_progress.update(products_done=len(reports), pages_processed=result.get("pages_processed", 0),
                                reviews_inserted=result.get("inserted", 0))
        status = {"completed": "succeeded", "complete": "succeeded", "paused": "paused",
                  "blocked": "blocked", "partial": "paused", "failed": "failed"}.get(result.get("status"), "failed")
        detail = result.get("detail") or {"succeeded": "Collection finished.", "paused": "Collection paused with saved progress. Start another job to resume.",
                  "blocked": "Collection stopped for access or session review. Check the saved product details before retrying.",
                  "failed": "Collection could not finish. Saved reviews and checkpoints are retained."}[status]
        if self._cancel.is_set() or result.get("cancelled"):
            status, detail = "cancelled", "Collection stopped. Saved pages are retained."
        if options["analyse"] and status in {"succeeded", "paused"} and not self._cancel.is_set():
            if self._rows("workspace"):
                try:
                    report_id, cached = self._analyse(job_id, "workspace", "", "local")
                    result.update(analysis_id=report_id, cached=cached)
                    detail += " Local workspace analysis is ready."
                except Exception:
                    LOG.exception("Automatic local analysis failed for job %s", job_id)
                    detail += " Local analysis could not finish. Run it separately in Analysis."
                    result["analysis_error"] = "Local analysis could not finish."
        if self._cancel.is_set():
            status, detail = "cancelled", "Job stopped. Saved reviews and reports are retained."
        self._update(job_id, status=status, phase="finished", progress=current_progress, result=result, detail=detail)

    def _worker(self):
        try:
            while not self._shutdown.is_set():
                self._wake.wait(0.5)
                self._wake.clear()
                if self._shutdown.is_set():
                    break
                with self._mutex:
                    with self._connection() as connection:
                        job = self._decode(connection.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone())
                    if job is None:
                        continue
                    self._cancel.clear()
                    self._finish.clear()
                    self._active_id = job["id"]
                    self._update(job["id"], status="running", phase="starting", detail="Starting the local job.")
                try:
                    self._run(job)
                except Exception as exc:
                    provider_job = job["kind"] in {"research", "brief", "comparison"} or (job["kind"] == "analysis" and job["options"].get("analysis_method") in {"openai", "openrouter"})
                    if provider_job:
                        detail, result = provider_failure(exc)
                        if job["options"].get("analysis_method") == "openai" and not isinstance(exc, ProviderError):
                            detail = str(exc) if isinstance(exc, (ValueError, ImportError)) else "The local job failed. Check the server terminal for details."
                        LOG.error("Provider job %s failed (%s)", job["id"], result["error_code"])
                        cancelled = self._cancel.is_set() or self._shutdown.is_set()
                        self._update(job["id"], status="cancelled" if cancelled else "failed", phase="finished",
                                     detail="Analysis stopped. Saved chunks are retained." if cancelled else detail,
                                     error=None if cancelled else detail, result=result)
                    else:
                        LOG.exception("Local job %s failed", job["id"])
                        detail = str(exc) if isinstance(exc, (ValueError, ImportError)) else "The local job failed. Check the server terminal for details."
                        self._update(job["id"], status="cancelled" if self._cancel.is_set() else "failed", phase="finished",
                                     detail=detail, error=detail)
                finally:
                    with self._mutex:
                        self._active_id = None
        finally:
            self._worker_lock.close()

    def close(self):
        self._shutdown.set()
        self._cancel.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        else:
            self._worker_lock.close()
        if self._analysis_store is not None:
            self._analysis_store.close()
