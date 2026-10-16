"""Durable, bounded local jobs. Browser work belongs to one worker thread."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import logging
from pathlib import Path
import sqlite3
import threading
import uuid

from dashboard_data import DashboardData, REVIEW_COLUMNS
from review_parser import extract_asin


ACTIVE = ("queued", "running", "cancelling")
TERMINAL = ("succeeded", "paused", "blocked", "failed", "cancelled", "interrupted")
LOG = logging.getLogger(__name__)


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
                 settings=None, session_reader=None, autostart=True):
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
        try:
            self._initialize()
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
        try:
            installed = importlib.util.find_spec("playwright") is not None
        except (ValueError, ImportError):
            installed = False
        return {
            "auth": {"status": auth_status, "detail": session.get("detail", "Session validity is checked during collection."),
                     "busy": bool(active and active["kind"] == "auth"), "verified": False},
            "capabilities": {"playwright_installed": installed, "openai_configured": settings.get("configured", False),
                             "model": settings.get("model", "")},
            "jobs": jobs, "active_job": active,
        }

    def _validate(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Supply a job as a JSON object.")
        kind = payload.get("kind")
        allowed = {
            "collection": {"kind", "urls", "max_pages", "resume", "transport", "analyse", "analysis_method", "allow_external"},
            "analysis": {"kind", "dataset", "asin", "analysis_method", "allow_external"},
            "auth": {"kind"},
        }
        if not isinstance(kind, str) or kind not in allowed or set(payload) - allowed[kind]:
            raise ValueError("Choose a supported job and options.")
        if kind == "auth":
            return kind, {}
        method = payload.get("analysis_method", "local")
        if not isinstance(method, str) or method not in {"local", "openai"}:
            raise ValueError("Choose local analysis or optional OpenAI analysis.")
        external = payload.get("allow_external", False)
        if not isinstance(external, bool):
            raise ValueError("External analysis consent must be true or false.")
        if method == "openai":
            if kind == "collection":
                raise ValueError("Automatic collection reports use local analysis. Choose OpenAI separately in Analysis.")
            if not external:
                raise ValueError("Confirm that the selected review text may be sent to OpenAI.")
            if not self.provider_settings().get("configured"):
                raise ValueError("Optional OpenAI analysis needs OPENAI_API_KEY and OPENAI_MODEL in the server environment.")
        if kind == "analysis":
            dataset, asin = payload.get("dataset", "workspace"), payload.get("asin", "")
            if not isinstance(dataset, str) or dataset not in {"demo", "workspace"} or not isinstance(asin, str):
                raise ValueError("Choose a valid dataset and product scope.")
            if asin and extract_asin(f"https://www.amazon.in/dp/{asin}") != asin:
                raise ValueError("Choose a valid product ASIN.")
            return kind, {"dataset": dataset, "asin": asin, "analysis_method": method, "allow_external": external}
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

    def _analyse(self, job_id, dataset, asin, method):
        from review_analysis import analyse_reviews, analyse_with_openai, report_fingerprint
        rows = self._rows(dataset, asin)
        if not rows:
            raise ValueError("There are no reviews in this scope yet. Collect or import reviews first.")
        fingerprint = report_fingerprint(rows, dataset=dataset, asin=asin)
        settings = self.provider_settings()
        model = settings.get("model", "") if method == "openai" else ""
        with self._connection() as connection:
            cached = connection.execute(
                "SELECT id FROM reports WHERE dataset=? AND asin=? AND method=? AND fingerprint=? AND model=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (dataset, asin, method, fingerprint, model)).fetchone()
        if cached:
            with self._connection() as connection:
                connection.execute("UPDATE reports SET created_at=? WHERE id=?", (now(), cached["id"]))
            return cached["id"], True
        self._update(job_id, phase="analysing", detail="Analyzing stored review evidence." if method == "local" else "Analyzing the bounded review sample with OpenAI.")
        if method == "local":
            report = (self._local_analyser or analyse_reviews)(rows, dataset=dataset, asin=asin)
        else:
            report = (self._ai_analyser or analyse_with_openai)(rows, dataset=dataset, asin=asin, model=model, cancelled=self._cancel.is_set)
        if self._cancel.is_set() or self._shutdown.is_set():
            return None, False
        report_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("INSERT INTO reports VALUES (?,?,?,?,?,?,?,?)", (
                report_id, dataset, asin, method, fingerprint, model, json.dumps(report, ensure_ascii=False), now()))
        return report_id, False

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
            report_id, cached = self._analyse(job_id, options["dataset"], options["asin"], options["analysis_method"])
            cancelled = self._cancel.is_set()
            self._update(job_id, status="cancelled" if cancelled else "succeeded", phase="finished",
                         result={"analysis_id": report_id, "cached": cached},
                         detail="Analysis stopped." if cancelled else ("The saved report is still current. Reused it without a provider call." if cached else "Analysis is ready. Open Analysis to inspect its evidence."))
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
