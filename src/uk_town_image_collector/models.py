"""Data structures and schema constants for the collector."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


RESULT_FIELDS = [
    "input_id", "place_name", "wikipedia_url", "canonical_title", "canonical_page_url",
    "page_id", "revision_id", "selection_method", "file_title", "caption",
    "commons_file_page_url", "original_file_url", "thumbnail_url", "mime_type",
    "width_px", "height_px", "creator_text", "creator_url", "credit_text",
    "license_short_name", "license_url", "license_evidence", "license_status",
    "attribution_text", "status", "reason_code", "reviewed", "reviewer_note",
    "retrieved_at_utc", "evidence_path", "source_revision_or_timestamp",
]
REVIEW_FIELDS = RESULT_FIELDS + ["reviewer_license_choice", "reviewer", "reviewed_at_utc", "review_fingerprint"]
READY_REQUIRED_FIELDS = [
    "input_id", "place_name", "wikipedia_url", "canonical_title", "canonical_page_url", "file_title",
    "commons_file_page_url", "original_file_url", "thumbnail_url", "mime_type", "width_px", "height_px",
    "creator_text", "license_short_name", "license_url", "license_evidence", "evidence_path",
]
STATUSES = ["CANDIDATE", "NO_IMAGE", "NEEDS_REVIEW", "INVALID_INPUT", "FETCH_ERROR", "RATE_LIMITED", "BLOCKED", "NOT_PROCESSED"]


@dataclass(frozen=True)
class InputRecord:
    input_id: str
    place_name: str
    wikipedia_url: str
    valid: bool = True
    error: str = ""


@dataclass
class FetchResponse:
    requested_url: str
    final_url: str
    status_code: int
    body: bytes
    sha256: str
    retrieved_at_utc: str
    headers: dict[str, str] = field(default_factory=dict)
    redirect_chain: list[str] = field(default_factory=list)
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class ImageCandidate:
    file_title: str
    commons_file_page_url: str
    selection_method: str
    caption: str = ""
    thumbnail_url: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    needs_review_reason: str = ""


@dataclass
class ParsedArticle:
    canonical_title: str = ""
    canonical_page_url: str = ""
    page_id: str = ""
    revision_id: str = ""
    candidate: ImageCandidate | None = None
    status: str = "NO_IMAGE"
    reason_code: str = "NO_SUITABLE_COMMONS_IMAGE"
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CommonsMetadata:
    canonical_file_title: str = ""
    original_file_url: str = ""
    thumbnail_url: str = ""
    mime_type: str = ""
    width_px: str = ""
    height_px: str = ""
    creator_text: str = ""
    creator_url: str = ""
    creator_source: str = ""
    credit_text: str = ""
    licenses: list[dict[str, str]] = field(default_factory=list)
    source_revision_or_timestamp: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
