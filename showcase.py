"""Launch a separate, fictional workspace with external integrations disabled."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from dashboard import DashboardServer
from dashboard_data import DashboardData
from job_manager import AutomationService
from llm_provider import ProviderError, ProviderRegistry
from review_store import ReviewStore


ROOT = Path(__file__).resolve().parent
MARKER = "reviewdesk-showcase-v1"
NAMES = {"B0DEMO0001": "North Desk Fan", "B0DEMO0002": "Field Desk Fan", "B0DEMO0003": "Harbor Desk Fan"}


class ShowcaseService(AutomationService):
    def __init__(self, data, **options):
        super().__init__(data, data.data_dir / "unused-session.json", provider_registry=ProviderRegistry(environ={}),
            settings=lambda: {"configured": False, "model": ""},
            session_reader=lambda path: {"valid": False, "exists": False, "detail": "External connections are disabled in this showcase."}, **options)

    def _validate(self, payload):
        if (not isinstance(payload, dict) or payload.get("kind") not in {"brief", "comparison", "analysis", "research"}
                or payload.get("dataset", "workspace") != "demo" or payload.get("allow_external", False) is not False
                or payload.get("analysis_method", "local") != "local" or payload.get("collect_missing", False) is not False):
            raise ValueError("The showcase uses fictional saved data only. Collection and external AI are disabled.")
        return super()._validate(payload)

    def configure_provider(self, payload):
        raise ProviderError("Provider settings are disabled in the offline showcase.", code="configuration")

    def check_provider_quota(self):
        raise ProviderError("The offline showcase does not contact OpenRouter.", code="configuration")


def prepare(folder):
    folder = Path(folder).resolve()
    if folder.exists() and any(folder.iterdir()):
        marker = folder / ".reviewdesk-showcase"
        if not marker.is_file() or marker.read_text(encoding="utf-8") != MARKER:
            raise ValueError("Choose an empty folder or an existing Reviewdesk showcase folder. Personal workspaces are not reused.")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".reviewdesk-showcase").write_text(MARKER, encoding="utf-8")
    data = DashboardData(folder)
    data.seed_demo()
    with ReviewStore(data.path("demo")) as store:
        for asin, name in NAMES.items():
            store.set_product_name(asin, name)
    return data


def manifest(data):
    with data.connection("demo") as connection:
        rows = {row["review_id"]: dict(row) for row in connection.execute("SELECT asin,review_id,review_title,review_body FROM reviews")}
    examples = []
    for review_id, text in [
        ("DEMO-001-02", "For someone who needs cordless use through a work session, this account is a reason to check battery life carefully. It does not establish a typical runtime."),
        ("DEMO-002-03", "Quiet operation can come with a tradeoff: this account describes weak airflow unless the fan is nearby. One account cannot tell us how common that experience is."),
    ]:
        row = rows[review_id]
        examples.append({"interpretation": text, "source": {"asin": row["asin"], "review_id": review_id, "quote": row["review_body"]}})
    return {"enabled": True, "offline": True, "products": [{"asin": asin, "name": name} for asin, name in NAMES.items()],
            "notice": "All products and reviews here are fictional. This workspace makes no Amazon or AI requests.",
            "example_label": "Written walkthrough example, not a live AI answer", "examples": examples}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "showcase")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        data = prepare(args.data_dir)
        service = ShowcaseService(data)
        try:
            with DashboardServer(("127.0.0.1", args.port), data, automation=service, showcase=manifest(data)) as server:
                print(f"Offline showcase: http://127.0.0.1:{server.server_address[1]}/#buying", flush=True)
                print("Fictional data only. No API key or Amazon session is used. Press Ctrl+C to stop.", flush=True)
                server.serve_forever()
        finally:
            service.close()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Showcase could not start: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
