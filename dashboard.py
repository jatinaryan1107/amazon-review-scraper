"""Serve the local Reviewdesk interface without running collection or AI tools."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from dashboard_data import MAX_IMPORT_BYTES, DashboardData, ReviewQuery


ROOT = Path(__file__).resolve().parent
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], data: DashboardData):
        if address[0] != "127.0.0.1":
            raise ValueError("The dashboard can bind only to 127.0.0.1.")
        self.data = data
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "Reviewdesk"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def local_request(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update({"127.0.0.1", "localhost"})
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            self.json_response(403, {"error": "Open this dashboard using its local address."})
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0].lower() != f"http://{hosts[0].lower()}"):
            self.json_response(403, {"error": "This dashboard accepts requests only from its own local page."})
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self.json_response(403, {"error": "Cross-site dashboard requests are not allowed."})
            return False
        return True

    def respond(self, status: int, content: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        for key, value in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(key, value)
        try:
            self.end_headers()
            self.wfile.write(content)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # A cancelled browser request has no client left to receive an error.
            self.close_connection = True

    def json_response(self, status: int, data: dict) -> None:
        self.respond(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.json_response(code, {"error": "This request could not be accepted."})

    def do_GET(self) -> None:
        if not self.local_request():
            return
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if parsed.scheme or parsed.netloc or "\\" in path or ".." in path.split("/"):
                self.json_response(404, {"error": "This page does not exist."})
                return
            if path in STATIC_FILES:
                filename, content_type = STATIC_FILES[path]
                self.respond(200, (ROOT / "web" / filename).read_bytes(), content_type)
                return
            if path not in {"/api/overview", "/api/reviews", "/api/export"}:
                self.json_response(404, {"error": "This page does not exist."})
                return
            params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
            query = ReviewQuery.parse(params)
            if path == "/api/overview":
                self.json_response(200, self.server.data.overview(query.dataset))
            elif path == "/api/reviews":
                self.json_response(200, self.server.data.reviews(query))
            else:
                self.respond(200, self.server.data.export(query), "text/csv; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="reviewdesk-{query.dataset}.csv"'})
        except ValueError as exc:
            database_error = self.is_database_error(exc)
            if database_error:
                self.log_error("Local review database could not be read: %s", exc)
            self.json_response(500 if database_error else 400, {"error": "The local review database is unavailable. Check the server terminal." if database_error else str(exc)})
        except (OSError, sqlite3.Error) as exc:
            self.log_error("Local dashboard data could not be read: %s", exc)
            self.json_response(500, {"error": "The local review data could not be read. Check the server terminal."})

    @staticmethod
    def is_database_error(error: ValueError) -> bool:
        return str(error).startswith(("Review database", "Cannot open review database", "Not a review database"))

    def do_POST(self) -> None:
        if not self.local_request():
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.path != "/api/import" or parsed.scheme or parsed.netloc:
                self.json_response(404, {"error": "This page does not exist."})
                return
            params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
            if params != {"dataset": ["workspace"]}:
                self.json_response(400, {"error": "Import reviews into My workspace. The demo dataset cannot be changed."})
                return
            if self.headers.get_all("X-Reviewdesk-Request", []) != ["1"]:
                self.json_response(403, {"error": "Import CSV files from the local dashboard page."})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "text/csv":
                self.json_response(415, {"error": "Upload a CSV file with the text/csv content type."})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
                self.json_response(400, {"error": "The CSV upload must include its file size."})
                return
            try:
                length = int(lengths[0])
            except ValueError:
                length = -1
            if length <= 0:
                self.json_response(400, {"error": "Choose a nonempty CSV file."})
                return
            if length > MAX_IMPORT_BYTES:
                self.json_response(413, {"error": "Choose a CSV file no larger than 5 MiB."})
                return
            payload = self.rfile.read(length)
            if len(payload) != length:
                self.json_response(400, {"error": "The CSV upload was incomplete. Try again."})
                return
            self.json_response(200, self.server.data.import_csv(payload))
        except ValueError as exc:
            status = 500 if self.is_database_error(exc) else 400
            if status == 500:
                self.log_error("Local review database could not be opened for import: %s", exc)
            self.json_response(status, {"error": "The local review database is unavailable. Check the server terminal." if status == 500 else str(exc)})
        except (OSError, sqlite3.Error) as exc:
            self.log_error("Local CSV import could not be saved: %s", exc)
            self.json_response(500, {"error": "The CSV could not be saved. Check the server terminal."})

    def do_OPTIONS(self) -> None:
        self.json_response(405, {"error": "Cross-origin requests are not supported."})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open Reviewdesk locally without running the scraper.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        data = DashboardData(args.data_dir)
        data.seed_demo()
        with DashboardServer(("127.0.0.1", args.port), data) as server:
            print(f"Local: http://127.0.0.1:{server.server_address[1]}", flush=True)
            print("Demo reviews are fictional. Press Ctrl+C to stop.", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Dashboard could not start: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
