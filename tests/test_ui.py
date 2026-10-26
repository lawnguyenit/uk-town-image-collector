"""Unit tests for the UK Town Image Collector Desktop Web UI server and APIs."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from uk_town_image_collector.exporter import write_outputs
from uk_town_image_collector.models import RESULT_FIELDS
from uk_town_image_collector.ui import AppServer, CollectorState, find_free_port


def _dummy_record(input_id: str, place: str, status: str = "CANDIDATE") -> dict:
    return {
        "input_id": input_id,
        "place_name": place,
        "wikipedia_url": f"https://en.wikipedia.org/wiki/{place}",
        "canonical_title": place,
        "canonical_page_url": f"https://en.wikipedia.org/wiki/{place}",
        "file_title": f"{place}_Castle.jpg",
        "commons_file_page_url": f"https://commons.wikimedia.org/wiki/File:{place}_Castle.jpg",
        "original_file_url": f"https://upload.wikimedia.org/wikipedia/commons/a/a0/{place}_Castle.jpg",
        "thumbnail_url": f"https://thumb.wikimedia.org/{place}_Castle.jpg",
        "mime_type": "image/jpeg",
        "width_px": "1920",
        "height_px": "1080",
        "creator_text": "Photographer",
        "license_short_name": "CC BY-SA 3.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/3.0",
        "license_evidence": '[{"name":"CC BY-SA 3.0","url":"https://creativecommons.org/licenses/by-sa/3.0","selected":true}]',
        "license_status": "SUPPORTED_METADATA",
        "attribution_text": f"{place}_Castle.jpg — Photographer — CC BY-SA 3.0",
        "status": status,
        "reason_code": "FIRST_INFOBOX_PHOTO",
        "reviewed": "PENDING",
        "reviewer_note": "",
        "retrieved_at_utc": "2026-10-26T00:00:00Z",
        "evidence_path": f"evidence/{input_id}.json",
    }


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.output_root = self.root / "outputs"
        self.run_dir = self.output_root / "20261026T000000000000Z"
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.records = [
            _dummy_record("UK001", "Alnwick", "CANDIDATE"),
            _dummy_record("UK002", "Bakewell", "CANDIDATE"),
        ]
        self.report = {
            "run_id": "20261026T000000000000Z",
            "run_status": "COMPLETED",
            "input_count": 2,
            "processed_count": 2,
            "review_approved_count": 0,
            "review_rejected_count": 0,
            "review_pending_count": 2,
            "ready_for_import_count": 0,
        }
        write_outputs(self.run_dir, self.records, self.report)

        self.state = CollectorState(self.root)
        self.port = find_free_port(preferred=8990)
        self.server = AppServer(("127.0.0.1", self.port), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def test_static_assets_serve_correctly(self):
        # GET /
        with urllib.request.urlopen(f"{self.base_url}/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            body = resp.read().decode("utf-8")
            self.assertIn("UK Town Image Collector", body)

        # GET /style.css
        with urllib.request.urlopen(f"{self.base_url}/style.css") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/css", resp.headers.get("Content-Type", ""))

        # GET /app.js
        with urllib.request.urlopen(f"{self.base_url}/app.js") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("javascript", resp.headers.get("Content-Type", ""))

    def test_api_state_and_review_and_bulk_approve(self):
        # 1. Check initial state
        with urllib.request.urlopen(f"{self.base_url}/api/state") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(data["records"]), 2)
            self.assertEqual(data["summary"]["total"], 2)
            self.assertEqual(data["summary"]["ready"], 0)

        # 2. Review one card as APPROVED
        req = urllib.request.Request(
            f"{self.base_url}/api/review",
            data=json.dumps({
                "input_id": "UK001",
                "file_title": "Alnwick_Castle.jpg",
                "reviewed": "APPROVED",
                "reviewer_note": "Verified by reviewer",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res_data["status"], "updated")
            self.assertEqual(res_data["record"]["reviewed"], "APPROVED")
            self.assertEqual(res_data["summary"]["ready"], 1)

        # 3. Bulk approve remaining cards
        req = urllib.request.Request(
            f"{self.base_url}/api/bulk_approve",
            data=json.dumps({"reviewer": "Lead Reviewer"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            bulk_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(bulk_data["status"], "success")
            self.assertEqual(bulk_data["approved_count"], 2)
            self.assertEqual(bulk_data["summary"]["ready"], 2)

        # Verify ready_for_import.csv on disk now has 2 rows
        ready_file = self.run_dir / "ready_for_import.csv"
        self.assertTrue(ready_file.exists())
        lines = ready_file.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(len(lines), 3)  # header + 2 data rows

    def test_export_endpoints(self):
        # Test downloading ready_csv
        with urllib.request.urlopen(f"{self.base_url}/api/export/ready_csv") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
            self.assertIn("ready_for_import.csv", resp.headers.get("Content-Disposition", ""))

        # Test downloading results_xlsx
        with urllib.request.urlopen(f"{self.base_url}/api/export/results_xlsx") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("results.xlsx", resp.headers.get("Content-Disposition", ""))
            content = resp.read()
            self.assertGreater(len(content), 100)


if __name__ == "__main__":
    unittest.main()
