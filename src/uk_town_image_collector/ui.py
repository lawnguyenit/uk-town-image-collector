"""Standalone local Web and Desktop UI for UK Town Image Collector.

Provides a user-friendly graphical interface for non-technical users to inspect
town image candidates, review and approve attributions, run scrapers, and download
ready-for-import CSV and Excel deliverables with one click.
"""
from __future__ import annotations

import csv
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .exporter import write_outputs
from .models import RESULT_FIELDS
from .review import apply_review_decisions, load_review_decisions, ready_record, review_fingerprint
from .runner import run_job
from .transport import utc_now

WEB_DIR = Path(__file__).parent / "web"


class CollectorState:
    """Thread-safe state container for the collector UI."""

    def __init__(self, root_dir: Path | None = None):
        self.lock = threading.RLock()
        self.root_dir = (root_dir or Path.cwd()).resolve()
        self.output_root = self.root_dir / "outputs"
        self.cache_dir = self.root_dir / "cache"
        self.input_path = self.root_dir / "input.csv"
        self.contact_url = os.environ.get("WIKI_CONTACT_URL", "https://github.com/lawnguyenit")

        self.status: str = "idle"  # idle, running, completed, error
        self.progress: dict = {"completed": 0, "total": 0, "place_name": "", "input_id": ""}
        self.logs: list[dict] = []
        self.records: list[dict] = []
        self.run_report: dict = {}
        self.active_output_dir: Path | None = None
        self.current_run_id: str = ""
        self.available_runs: list[str] = []
        self.worker_thread: threading.Thread | None = None
        self.error_message: str = ""

        self.refresh_available_runs()
        if self.available_runs:
            self.load_run(self.available_runs[0])

    def log(self, msg: str, level: str = "info") -> None:
        with self.lock:
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "msg": msg,
                "level": level,
            }
            self.logs.append(entry)
            if len(self.logs) > 120:
                self.logs.pop(0)

    def refresh_available_runs(self) -> list[str]:
        with self.lock:
            runs: list[str] = []
            if self.output_root.exists():
                for p in sorted(self.output_root.iterdir(), reverse=True):
                    if p.is_dir() and not p.name.endswith(".tmp") and (p / "results.csv").exists():
                        runs.append(p.name)
            self.available_runs = runs
            return runs

    def load_run(self, run_id: str) -> bool:
        target_dir = self.output_root / run_id
        if not target_dir.exists() or not target_dir.is_dir():
            return False

        with self.lock:
            records: list[dict] = []
            checkpoint_file = target_dir / "checkpoint.json"
            results_file = target_dir / "results.csv"
            review_file = target_dir / "review.csv"
            report_file = target_dir / "run-report.json"

            if checkpoint_file.exists():
                try:
                    data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                    records = list(data.get("rows", {}).values())
                except Exception:
                    records = []

            if results_file.exists():
                try:
                    with results_file.open("r", encoding="utf-8-sig", newline="") as handle:
                        reader = csv.DictReader(handle)
                        results_map = {row.get("input_id"): row for row in reader if row.get("input_id")}
                        if not records:
                            records = list(results_map.values())
                        else:
                            for r in records:
                                res_row = results_map.get(r.get("input_id"))
                                if res_row:
                                    for k in ("evidence_path", "source_revision_or_timestamp", "retrieved_at_utc", "status", "reason_code"):
                                        if not r.get(k) and res_row.get(k):
                                            r[k] = res_row[k]
                except Exception:
                    pass

            if review_file.exists() and records:
                try:
                    decisions = load_review_decisions(review_file)
                    apply_review_decisions(records, decisions)
                except Exception:
                    pass

            report = {}
            if report_file.exists():
                try:
                    report = json.loads(report_file.read_text(encoding="utf-8"))
                except Exception:
                    report = {}

            self.records = records
            self.run_report = report
            self.active_output_dir = target_dir
            self.current_run_id = run_id
            self.status = "completed" if report else "idle"
            return True

    def get_summary(self) -> dict:
        with self.lock:
            total = len(self.records)
            candidates = sum(1 for r in self.records if r.get("status") == "CANDIDATE")
            ready = sum(1 for r in self.records if ready_record(r) is not None)
            approved = sum(1 for r in self.records if r.get("reviewed") == "APPROVED")
            pending = sum(1 for r in self.records if r.get("status") == "CANDIDATE" and r.get("reviewed") in ("PENDING", "", None))
            needs_review = sum(1 for r in self.records if r.get("status") == "NEEDS_REVIEW" or r.get("reviewed") == "REJECTED")
            return {
                "total": total,
                "candidates": candidates,
                "ready": ready,
                "approved": approved,
                "pending": pending,
                "needs_review": needs_review,
            }

    def save_review_decision(
        self,
        input_id: str,
        file_title: str,
        reviewed: str,
        note: str | None = None,
        license_choice: str | None = None,
        reviewer: str = "Reviewer (UI)",
    ) -> dict | None:
        with self.lock:
            if not self.active_output_dir or not self.active_output_dir.exists():
                return None

            target = next((r for r in self.records if r.get("input_id") == input_id), None)
            if not target:
                return None

            target["reviewed"] = reviewed
            target["reviewer"] = reviewer
            target["reviewed_at_utc"] = utc_now()
            if note is not None:
                target["reviewer_note"] = note
            if license_choice is not None:
                target["reviewer_license_choice"] = license_choice

            target["review_fingerprint"] = review_fingerprint(target)

            # Re-export all artifacts atomically
            self._reexport_locked()
            return {k: v for k, v in target.items() if not k.startswith("_")}

    def bulk_approve_valid(self, reviewer: str = "Reviewer (UI)") -> int:
        with self.lock:
            if not self.active_output_dir or not self.active_output_dir.exists():
                return 0

            count = 0
            for r in self.records:
                if r.get("status") == "CANDIDATE" and r.get("license_status") == "SUPPORTED_METADATA":
                    r["reviewed"] = "APPROVED"
                    r["reviewer"] = reviewer
                    r["reviewed_at_utc"] = utc_now()
                    r["review_fingerprint"] = review_fingerprint(r)
                    count += 1

            self._reexport_locked()
            return count

    def _reexport_locked(self) -> None:
        if not self.active_output_dir:
            return

        # Update run_report numbers
        summary = self._calculate_summary_locked()
        self.run_report["review_approved_count"] = summary["approved"]
        self.run_report["review_rejected_count"] = sum(1 for r in self.records if r.get("reviewed") == "REJECTED")
        self.run_report["review_pending_count"] = summary["pending"]
        self.run_report["ready_for_import_count"] = summary["ready"]

        # Update checkpoint.json if present
        checkpoint_path = self.active_output_dir / "checkpoint.json"
        if checkpoint_path.exists():
            try:
                cp_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                for r in self.records:
                    iid = r.get("input_id")
                    if iid in cp_data.get("rows", {}):
                        for k in ("reviewed", "reviewer_note", "reviewer_license_choice", "reviewer", "reviewed_at_utc", "review_fingerprint"):
                            cp_data["rows"][iid][k] = r.get(k, "")
                checkpoint_path.write_text(json.dumps(cp_data, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:
                pass

        write_outputs(self.active_output_dir, self.records, self.run_report)

    def _calculate_summary_locked(self) -> dict:
        total = len(self.records)
        candidates = sum(1 for r in self.records if r.get("status") == "CANDIDATE")
        ready = sum(1 for r in self.records if ready_record(r) is not None)
        approved = sum(1 for r in self.records if r.get("reviewed") == "APPROVED")
        pending = sum(1 for r in self.records if r.get("status") == "CANDIDATE" and r.get("reviewed") in ("PENDING", "", None))
        return {"total": total, "candidates": candidates, "ready": ready, "approved": approved, "pending": pending}

    def start_run_thread(self, config: dict) -> tuple[bool, str]:
        with self.lock:
            if self.status == "running":
                return False, "A collection job is already running."

            self.status = "running"
            self.logs = []
            self.progress = {"completed": 0, "total": 20, "place_name": "Starting...", "input_id": ""}
            self.error_message = ""

        input_file = Path(config.get("input_file", "input.csv"))
        if not input_file.is_absolute():
            input_file = self.root_dir / input_file
        contact_url = (config.get("contact_url") or self.contact_url).strip()
        enable_api = bool(config.get("enable_api", True))
        refresh = bool(config.get("refresh", False))
        allow_invalid = bool(config.get("allow_invalid_rows", True))

        def _worker():
            self.log(f"Starting collector run on {input_file.name}...", "info")
            try:
                def _on_progress(event: dict):
                    ev_type = event.get("type")
                    if ev_type == "progress":
                        rec = event.get("record", {})
                        res = event.get("result", {})
                        completed = event.get("processed", 0)
                        total = event.get("total", 0)
                        status = res.get("status", "")
                        place = rec.get("place_name", "")
                        with self.lock:
                            self.progress = {
                                "completed": completed,
                                "total": total,
                                "place_name": place,
                                "input_id": rec.get("input_id", ""),
                            }
                        self.log(f"[{completed}/{total}] {rec.get('input_id')} {place} → {status} ({res.get('selection_method', 'none')})", "info")
                    elif ev_type == "completed":
                        self.log(f"Batch completed: {event.get('run_status')}! Output saved.", "success")

                output_dir = run_job(
                    input_path=input_file,
                    output_root=self.output_root,
                    cache_dir=self.cache_dir,
                    contact_url=contact_url,
                    refresh=refresh,
                    expected_count=None,
                    allow_row_invalid=allow_invalid,
                    enable_api=enable_api,
                    progress_callback=_on_progress,
                )
                self.refresh_available_runs()
                self.load_run(output_dir.name)
                with self.lock:
                    self.status = "completed"
                self.log(f"Outputs written to: {output_dir.name}", "success")
            except Exception as exc:
                with self.lock:
                    self.status = "error"
                    self.error_message = str(exc)
                self.log(f"Run failed: {type(exc).__name__}: {exc}", "error")

        self.worker_thread = threading.Thread(target=_worker, daemon=True)
        self.worker_thread.start()
        return True, "Job started"


class CollectorRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler serving dashboard UI and REST JSON APIs."""

    server: AppServer  # type hint for parent server

    def log_message(self, format: str, *args) -> None:
        # Suppress noisy HTTP stdout logging during normal UI usage
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self._serve_file(WEB_DIR / "style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._serve_file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
        elif path == "/api/state":
            self._handle_api_state()
        elif path.startswith("/api/export/"):
            export_type = path.split("/api/export/")[-1]
            self._handle_export(export_type)
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        data = {}
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len > 0:
            raw = self.rfile.read(content_len)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = {}

        if path == "/api/start":
            self._handle_api_start(data)
        elif path == "/api/load_run":
            self._handle_api_load_run(data)
        elif path == "/api/review":
            self._handle_api_review(data)
        elif path == "/api/bulk_approve":
            self._handle_api_bulk_approve(data)
        elif path == "/api/open_folder":
            self._handle_api_open_folder()
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "API route not found")

    # ---------------- File and API Helpers ----------------

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists():
            self._send_error(HTTPStatus.NOT_FOUND, f"File {file_path.name} not found")
            return
        content = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, payload: dict | list, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message, "status": status.value}, status)

    def _handle_api_state(self) -> None:
        state = self.server.state
        with state.lock:
            # Clean records for JSON display (omit internal _keys like _evidence)
            clean_records = [
                {k: v for k, v in r.items() if not k.startswith("_")}
                for r in state.records
            ]
            payload = {
                "status": state.status,
                "progress": dict(state.progress),
                "logs": list(state.logs),
                "records": clean_records,
                "summary": state.get_summary(),
                "available_runs": list(state.available_runs),
                "current_run_id": state.current_run_id,
                "error_message": state.error_message,
            }
        self._send_json(payload)

    def _handle_api_start(self, config: dict) -> None:
        ok, msg = self.server.state.start_run_thread(config)
        if ok:
            self._send_json({"status": "started", "message": msg})
        else:
            self._send_json({"error": msg}, HTTPStatus.CONFLICT)

    def _handle_api_load_run(self, data: dict) -> None:
        run_id = data.get("run_id", "")
        if not run_id:
            self._send_error(HTTPStatus.BAD_REQUEST, "Missing run_id")
            return
        ok = self.server.state.load_run(run_id)
        if ok:
            self._send_json({"status": "loaded", "run_id": run_id})
        else:
            self._send_error(HTTPStatus.NOT_FOUND, f"Run {run_id} could not be loaded")

    def _handle_api_review(self, data: dict) -> None:
        input_id = data.get("input_id")
        file_title = data.get("file_title", "")
        reviewed = data.get("reviewed")
        note = data.get("reviewer_note")
        choice = data.get("reviewer_license_choice")

        if not input_id or not reviewed:
            self._send_error(HTTPStatus.BAD_REQUEST, "input_id and reviewed are required")
            return

        updated = self.server.state.save_review_decision(
            input_id=input_id,
            file_title=file_title,
            reviewed=reviewed,
            note=note,
            license_choice=choice,
        )
        if updated is not None:
            self._send_json({"status": "updated", "record": updated, "summary": self.server.state.get_summary()})
        else:
            self._send_error(HTTPStatus.BAD_REQUEST, "Failed to update review decision")

    def _handle_api_bulk_approve(self, data: dict) -> None:
        reviewer = data.get("reviewer", "Reviewer (UI)")
        count = self.server.state.bulk_approve_valid(reviewer=reviewer)
        self._send_json({"status": "success", "approved_count": count, "summary": self.server.state.get_summary()})

    def _handle_api_open_folder(self) -> None:
        state = self.server.state
        out_dir = state.active_output_dir or state.output_root
        if not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=True)

        try:
            if sys.platform == "win32":
                os.startfile(str(out_dir))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(out_dir)])
            else:
                subprocess.Popen(["xdg-open", str(out_dir)])
            self._send_json({"status": "opened", "path": str(out_dir)})
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not open directory: {exc}")

    def _handle_export(self, kind: str) -> None:
        state = self.server.state
        out_dir = state.active_output_dir
        if not out_dir or not out_dir.exists():
            self._send_error(HTTPStatus.NOT_FOUND, "No active run output directory")
            return

        mapping = {
            "ready_csv": ("ready_for_import.csv", "text/csv; charset=utf-8"),
            "results_xlsx": ("results.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "review_csv": ("review.csv", "text/csv; charset=utf-8"),
            "results_csv": ("results.csv", "text/csv; charset=utf-8"),
            "qa_report": ("qa-report.md", "text/markdown; charset=utf-8"),
        }

        if kind not in mapping:
            self._send_error(HTTPStatus.BAD_REQUEST, f"Unknown export type: {kind}")
            return

        filename, mime = mapping[kind]
        file_path = out_dir / filename
        if not file_path.exists():
            self._send_error(HTTPStatus.NOT_FOUND, f"Export file {filename} not found in {out_dir.name}")
            return

        content = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content)


class AppServer(ThreadingHTTPServer):
    """Threading HTTP server with embedded application state."""

    def __init__(self, server_address, state: CollectorState):
        super().__init__(server_address, CollectorRequestHandler)
        self.state = state


def find_free_port(preferred: int = 8765, host: str = "127.0.0.1") -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return preferred


def find_edge_path() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / r"Microsoft\Edge\Application\msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / r"Microsoft\Edge\Application\msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / r"Microsoft\Edge\Application\msedge.exe",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def launch_ui_app(
    host: str = "127.0.0.1",
    preferred_port: int = 8765,
    root_dir: Path | None = None,
    open_browser: bool = True,
) -> None:
    """Launch the UK Town Image Collector dashboard application."""
    port = find_free_port(preferred_port, host)
    state = CollectorState(root_dir)
    server = AppServer((host, port), state)
    url = f"http://{host}:{port}"

    print("=" * 65)
    print("🇬🇧  UK Town Image & Attribution Collector — Desktop Web UI")
    print("=" * 65)
    print(f"Server running at: {url}")
    print(f"Outputs location:  {state.output_root}")
    print("Press Ctrl+C in this terminal to stop the server.")
    print("=" * 65)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    if open_browser:
        edge_path = find_edge_path() if sys.platform == "win32" else None
        if edge_path and edge_path.exists():
            print(f"Launching native desktop app window via Microsoft Edge...")
            try:
                subprocess.Popen([
                    str(edge_path),
                    f"--app={url}",
                    "--window-size=1360,900",
                ])
            except Exception:
                webbrowser.open(url)
        else:
            print("Opening in default browser...")
            webbrowser.open(url)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down UK Town Image Collector server...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    launch_ui_app()
