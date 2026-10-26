"""Command-line entrypoint for the UK Town Image & Attribution Collector."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .runner import RunInterrupted, run_job
from .validation import InputValidationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect one traceable Wikimedia Commons image candidate per UK town input.")
    parser.add_argument("--input", type=Path, default=Path("input.csv"), help="Input CSV (input_id, place_name, wikipedia_url)")
    parser.add_argument("--output", type=Path, default=Path("outputs"), help="Output root; each run creates a timestamped subdirectory")
    parser.add_argument("--cache", type=Path, default=Path("cache"), help="Local compressed HTTP/API cache directory")
    parser.add_argument("--review-from", type=Path, help="Reviewed review.csv; decisions bind to (input_id, file_title) + fingerprint")
    parser.add_argument("--resume-from", type=Path, help="Interrupted .tmp directory or checkpoint.json to resume")
    parser.add_argument("--contact-url", default="", help="Public HTTPS project/profile URL for Wikimedia User-Agent (overrides WIKI_CONTACT_URL)")
    parser.add_argument("--expected-count", type=int, default=20, help="Expected number of input rows (0 disables exact-count check)")
    parser.add_argument("--allow-invalid-rows", action="store_true", help="Emit status=INVALID_INPUT per bad row instead of failing preflight")
    parser.add_argument("--no-api", action="store_true", help="Disable MediaWiki Action API fallback/enrichment and use HTML only")
    parser.add_argument("--refresh", action="store_true", help="Bypass cached article/file responses (robots.txt is always fetched fresh)")
    parser.add_argument("--probe-only", action="store_true", help="Process only the first three rows and write a field-probe artifact")
    parser.add_argument("--ui", action="store_true", help="Launch the interactive desktop Web UI dashboard")
    parser.add_argument("--port", type=int, default=8765, help="Port for the UI dashboard (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically launch a browser window when starting the UI")
    args = parser.parse_args(argv)

    if args.ui:
        from .ui import launch_ui_app
        launch_ui_app(preferred_port=args.port, open_browser=not args.no_browser)
        return 0

    contact_url = (args.contact_url or os.environ.get("WIKI_CONTACT_URL", "")).strip()
    if not contact_url:
        parser.error("Provide --contact-url or set WIKI_CONTACT_URL to a public HTTPS project/profile URL.")

    expected = args.expected_count if args.expected_count > 0 else None
    try:
        output_dir = run_job(
            args.input,
            args.output,
            args.cache,
            contact_url,
            refresh=args.refresh,
            review_from=args.review_from,
            probe_only=args.probe_only,
            resume_from=args.resume_from,
            expected_count=expected,
            allow_row_invalid=args.allow_invalid_rows,
            enable_api=not args.no_api,
        )
    except InputValidationError as exc:
        args.output.mkdir(parents=True, exist_ok=True)
        report_path = args.output / "input-validation-report.json"
        report_path.write_text(json.dumps({"status": "INVALID_INPUT", "issues": exc.issues}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(exc), file=sys.stderr)
        print(f"Validation report: {report_path}", file=sys.stderr)
        return 2
    except RunInterrupted as exc:
        print(str(exc), file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Run could not start or complete: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"Run artifact: {output_dir.resolve()}")
    report_file = output_dir / ("field-probe.json" if args.probe_only else "run-report.json")
    if report_file.exists():
        report = json.loads(report_file.read_text(encoding="utf-8"))
        print(f"Status: {report.get('run_status', 'PROBE')}; requests={report.get('request_count', 0)}; cache_hits={report.get('cache_hits', 0)}")
        if not args.probe_only:
            print(
                f"Rows: {report.get('input_count')}; candidates={report.get('status_counts', {}).get('CANDIDATE', 0)}; "
                f"approved={report.get('review_approved_count')}; ready={report.get('ready_for_import_count')}"
            )
    return 0
