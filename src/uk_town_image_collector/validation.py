"""Pre-request input.csv validation.

Structural problems (wrong headers, row count, blank or duplicate ``input_id``) stop the
run before any HTTP request is sent. Row-level URL or place-name violations are recorded
on the ``InputRecord`` so the batch can emit ``status=INVALID_INPUT`` for that row while
still failing fast in strict preflight mode when requested.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .models import InputRecord


EXPECTED_HEADERS = ["input_id", "place_name", "wikipedia_url"]
DISALLOWED_NAMESPACES = {"file", "category", "special", "talk", "user", "wikipedia", "template", "help", "portal", "module", "draft", "mediawiki", "timedtext"}


class InputValidationError(ValueError):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("Input validation failed: " + "; ".join(issues))


def read_and_validate_input(
    path: Path,
    *,
    expected_count: int | None = 20,
    allow_row_invalid: bool = False,
) -> tuple[list[InputRecord], str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPECTED_HEADERS:
            raise InputValidationError([f"Header must be exactly {EXPECTED_HEADERS}; got {reader.fieldnames!r}"])
        rows = list(reader)

    structural_issues: list[str] = []
    row_issues: list[str] = []
    if expected_count is not None and len(rows) != expected_count:
        structural_issues.append(f"Expected exactly {expected_count} data rows; got {len(rows)}")
    if not rows:
        structural_issues.append("Input CSV has no data rows")

    records: list[InputRecord] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        if None in row:
            structural_issues.append(f"Line {line}: unexpected extra CSV column(s)")
        input_id = row.get("input_id") or ""
        place_name = row.get("place_name") or ""
        wikipedia_url = row.get("wikipedia_url") or ""
        if not input_id or input_id != input_id.strip():
            structural_issues.append(f"Line {line}: input_id must be non-empty without surrounding whitespace")
        elif input_id in seen:
            structural_issues.append(f"Line {line}: duplicate input_id {input_id!r}")
        seen.add(input_id)

        reason = validate_row(place_name, wikipedia_url)
        if reason:
            row_issues.append(f"Line {line} ({input_id}): {reason}")
            records.append(InputRecord(input_id, place_name, wikipedia_url, valid=False, error=reason))
        else:
            records.append(InputRecord(input_id, place_name, wikipedia_url, valid=True))

    if structural_issues or (row_issues and not allow_row_invalid):
        raise InputValidationError(structural_issues + row_issues)

    canonical_input = [{"input_id": r.input_id, "place_name": r.place_name, "wikipedia_url": r.wikipedia_url} for r in records]
    digest = hashlib.sha256(json.dumps(canonical_input, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return records, digest


def validate_row(place_name: str, wikipedia_url: str) -> str:
    if not (place_name or "").strip():
        return "BLANK_PLACE_NAME"
    return validate_article_url(wikipedia_url)


def validate_article_url(value: str) -> str:
    if not value or value != value.strip() or any(ch.isspace() for ch in value):
        return "MALFORMED_URL"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "MALFORMED_URL"
    if parsed.username or parsed.password:
        return "URL_HAS_CREDENTIALS"
    if parsed.scheme != "https":
        return "NON_HTTPS_URL"
    if parsed.hostname != "en.wikipedia.org" or parsed.port is not None:
        return "DISALLOWED_HOST"
    if parsed.query or parsed.fragment:
        return "URL_HAS_QUERY_OR_FRAGMENT"
    if not parsed.path.startswith("/wiki/"):
        return "NON_WIKI_PATH"
    title = unquote(parsed.path[len("/wiki/"):]).replace("_", " ").strip()
    if not title or "/" in title:
        return "INVALID_ARTICLE_TITLE"
    if ":" in title and title.split(":", 1)[0].strip().casefold() in DISALLOWED_NAMESPACES:
        return "NON_ARTICLE_NAMESPACE"
    return ""


def canonical_request_key(url: str) -> str:
    parsed = urlsplit(url)
    title = unquote(parsed.path).replace("_", " ").strip()
    return f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}{title.casefold()}"
