"""Execution records retain transitions without retaining request content."""

import unittest
from pathlib import Path
import tempfile

from execution_trace import advance, public_trace
from dashboard_data import DashboardData
from job_manager import AutomationService
from llm_provider import ProviderRegistry


class TraceTests(unittest.TestCase):
    def job(self):
        return {"phase": "queued", "status": "queued", "created_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z", "progress": {}}

    def test_transitions_have_real_durations_and_only_allowlisted_counters(self):
        job = self.job()
        trace = advance(job, {"phase": "analysing", "status": "running", "progress": {"calls": 1, "api_key": "PRIVATE", "question": "PRIVATE"}}, "2026-01-01T00:00:02.000Z")
        job.update(phase="analysing", status="running", progress={"_execution": trace})
        trace = advance(job, {"phase": "finished", "status": "succeeded"}, "2026-01-01T00:00:05.000Z")
        public = public_trace(trace, {**job, "updated_at": "2026-01-01T00:00:05.000Z"})
        self.assertEqual([item["elapsed_seconds"] for item in public["events"]], [2, 3, 0])
        self.assertEqual(public["events"][1]["counters"], {"calls": 1})
        self.assertNotIn("PRIVATE", str(public))
        self.assertEqual(public["elapsed_seconds"], 5)

    def test_repeated_progress_does_not_create_unbounded_events(self):
        job = self.job()
        for count in range(100):
            job["progress"] = {"_execution": advance(job, {"progress": {"pages_processed": count}}, "2026-01-01T00:00:02.000Z")}
        trace = job["progress"]["_execution"]
        self.assertEqual(len(trace["events"]), 1)
        self.assertEqual(trace["events"][0]["counters"]["pages_processed"], 99)

    def test_older_jobs_have_no_invented_trace(self):
        self.assertIsNone(public_trace(None, self.job()))

    def test_restart_does_not_count_offline_time_as_processing(self):
        job = self.job()
        trace = advance(job, {"phase": "analysing", "status": "running"}, "2026-01-01T00:00:02.000Z")
        job.update(phase="analysing", status="running", progress={"_execution": trace})
        trace = advance(job, {"phase": "finished", "status": "interrupted"}, "2026-01-02T00:00:00.000Z")
        job.update(phase="finished", status="interrupted", updated_at="2026-01-02T00:00:00.000Z")
        checked = public_trace(trace, job)
        self.assertIsNone(checked["elapsed_seconds"])
        self.assertIsNone(checked["events"][1]["elapsed_seconds"])

    def test_transition_limit_keeps_first_and_latest_with_disclosure(self):
        job = self.job()
        for count in range(60):
            phase = "analysing" if count % 2 else "researching"
            trace = advance(job, {"phase": phase}, "2026-01-01T00:00:02.000Z")
            job.update(phase=phase, progress={"_execution": trace})
        self.assertEqual(len(trace["events"]), 32)
        self.assertEqual(trace["events"][0]["phase"], "queued")
        self.assertEqual(trace["events"][-1]["phase"], "analysing")
        self.assertEqual(trace["omitted_events"], 29)

    def test_job_trace_survives_restart_and_internal_storage_stays_out_of_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            data = DashboardData(Path(folder))
            service = AutomationService(data, Path(folder) / "unused.json", autostart=False, provider_registry=ProviderRegistry(environ={}))
            try:
                job = service.submit({"kind": "analysis", "dataset": "demo"})
                service._update(job["id"], status="running", phase="analysing", progress={"calls": 0})
                before = service.get_job(job["id"])
                self.assertNotIn("_execution", before["progress"])
                self.assertEqual(before["execution"]["events"][-1]["phase"], "analysing")
            finally:
                service.close()
            restored = AutomationService(data, Path(folder) / "unused.json", autostart=False, provider_registry=ProviderRegistry(environ={}))
            try:
                after = restored.get_job(job["id"])
                self.assertEqual(after["status"], "interrupted")
                self.assertIsNone(after["execution"]["elapsed_seconds"])
                self.assertEqual(after["execution"]["events"][-1]["status"], "interrupted")
            finally:
                restored.close()
