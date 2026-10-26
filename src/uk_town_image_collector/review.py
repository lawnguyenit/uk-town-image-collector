"""Human review binding and the ready-for-import filter.

A decision in review.csv applies only to the exact (input_id, file_title) it was made for
and, when present, only while the review fingerprint (file, creator, license, original URL,
selection method) is unchanged. Anything else falls back to PENDING.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .models import READY_REQUIRED_FIELDS, REVIEW_FIELDS
from .parsers import is_supported_choice

REVIEW_STATES = {"PENDING", "APPROVED", "REJECTED"}


def review_fingerprint(record: dict) -> str:
    parts = [record.get(k, "") or "" for k in ("input_id", "file_title", "selection_method", "original_file_url", "creator_text", "license_short_name")]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def load_review_decisions(path: Path | None) -> dict[tuple[str, str], dict[str, str]]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Review file not found: {path}")
    decisions: dict[tuple[str, str], dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"input_id", "file_title", "reviewed", "reviewer_note"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"Review file must include columns: {sorted(required)}")
        for row in reader:
            input_id = _unescape(row.get("input_id"))
            file_title = _unescape(row.get("file_title"))
            if not input_id or not file_title:
                continue
            reviewed = (row.get("reviewed") or "PENDING").strip().upper()
            decisions[(input_id, file_title)] = {
                "reviewed": reviewed if reviewed in REVIEW_STATES else "PENDING",
                "reviewer_note": _unescape(row.get("reviewer_note")),
                "reviewer_license_choice": _unescape(row.get("reviewer_license_choice")),
                "reviewer": _unescape(row.get("reviewer")),
                "reviewed_at_utc": _unescape(row.get("reviewed_at_utc")),
                "review_fingerprint": _unescape(row.get("review_fingerprint")),
            }
    return decisions


def apply_review_decisions(records: list[dict], decisions: dict[tuple[str, str], dict[str, str]]) -> dict[str, int]:
    stats = {"decisions_loaded": len(decisions), "applied": 0, "invalidated_changed_metadata": 0, "unmatched": 0}
    matched_keys = set()
    for record in records:
        record.update({"reviewed": "PENDING", "reviewer_note": "", "reviewer_license_choice": "", "reviewer": "", "reviewed_at_utc": ""})
        record["review_fingerprint"] = review_fingerprint(record) if record.get("file_title") else ""
        key = (record.get("input_id", ""), record.get("file_title", ""))
        decision = decisions.get(key) if all(key) else None
        if not decision:
            continue
        matched_keys.add(key)
        saved_fp = decision.get("review_fingerprint", "")
        if saved_fp and saved_fp != record["review_fingerprint"]:
            stats["invalidated_changed_metadata"] += 1
            record["reviewer_note"] = "REVIEW_INVALIDATED: file metadata changed since the earlier decision; review again."
            continue
        record.update({k: v for k, v in decision.items() if k != "review_fingerprint"})
        stats["applied"] += 1
    stats["unmatched"] = len(set(decisions) - matched_keys)
    return stats


def parse_license_choices(value: str) -> list[dict[str, str]]:
    if not value:
        return []
    try:
        choices = json.loads(value)
        return [c for c in choices if isinstance(c, dict)] if isinstance(choices, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def ready_record(record: dict) -> dict | None:
    if record.get("status") != "CANDIDATE" or record.get("reviewed") != "APPROVED" or record.get("license_status") != "SUPPORTED_METADATA":
        return None
    choices = parse_license_choices(record.get("license_evidence", ""))
    wanted = (record.get("reviewer_license_choice") or "").strip() or (record.get("license_short_name") or "").strip()
    match = next((c for c in choices if c.get("name", "") == wanted), None)
    if match is None or not is_supported_choice(match):
        return None
    ready = {k: v for k, v in record.items() if not k.startswith("_")}
    ready["license_short_name"] = match.get("name", "")
    ready["license_url"] = match.get("url", "")
    if not all(ready.get(field, "") for field in READY_REQUIRED_FIELDS):
        return None
    ready["attribution_text"] = " — ".join([ready["file_title"], ready["creator_text"], ready["license_short_name"], ready["commons_file_page_url"]])
    return ready


def write_review_template(records: list[dict], path: Path) -> None:
    write_csv(records, path, REVIEW_FIELDS)


def write_csv(rows: list[dict], path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: safe_text(row.get(field, "")) for field in fields})


def safe_text(value) -> str:
    """Neutralise spreadsheet formulas: prefix an apostrophe when text starts with = + - @."""
    text = "" if value is None else str(value)
    # Excel treats values beginning with these characters as formulas. Include leading
    # whitespace in the check because spreadsheet programs may ignore it.
    probe = text.lstrip(" \t\r\n\x00")
    if probe and probe[0] in "=+-@":
        return "'" + text
    return text


def _unescape(value) -> str:
    """Reverse :func:`safe_text` so a re-imported review file keeps the original values."""
    text = (value or "").strip()
    if text.startswith("'") and text[1:].lstrip(" \t\r\n\x00")[:1] in tuple("=+-@"):
        return text[1:]
    return text
