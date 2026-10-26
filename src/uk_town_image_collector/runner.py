"""Batch orchestration: validation → robots preflight → article → Commons → review → exports.

The runner keeps one result row per input_id, writes a checkpoint after every input and
publishes outputs only by renaming a completed temporary directory, so an interrupted or
stopped run is never mistaken for a finished one.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from . import __version__
from .exporter import write_outputs
from .mediawiki_api import merge_commons_metadata, parse_commons_imageinfo, parse_pageimages_response
from .models import InputRecord, RESULT_FIELDS
from .parsers import license_status, parse_article, parse_commons, select_license
from .review import apply_review_decisions, load_review_decisions, ready_record
from .robots import RobotsGuard
from .transport import FetchError, HtmlClient, StopRun, utc_now
from .validation import canonical_request_key, read_and_validate_input

JOB_ID = "uk-town-image-collector-v1"
# Bump when selection/normalisation rules change so old checkpoints cannot be resumed silently.
CONFIG_VERSION = "2026-10-26.2"
RESUMABLE_DONE = {"CANDIDATE", "NEEDS_REVIEW", "NO_IMAGE", "INVALID_INPUT"}
VALID_METHODS = {"infobox_first_photo", "pageimages_fallback", "html_pageimage_fallback"}


class RunInterrupted(RuntimeError):
    def __init__(self, working_dir: Path, completed: int, total: int):
        self.working_dir = working_dir
        super().__init__(f"Run interrupted after {completed}/{total} inputs; checkpoint saved in {working_dir}")


def run_job(
    input_path: Path,
    output_root: Path,
    cache_dir: Path,
    contact_url: str,
    *,
    refresh: bool = False,
    review_from: Path | None = None,
    probe_only: bool = False,
    resume_from: Path | None = None,
    expected_count: int | None = 20,
    allow_row_invalid: bool = False,
    enable_api: bool = True,
    client: HtmlClient | None = None,
) -> Path:
    records, input_digest = read_and_validate_input(input_path, expected_count=expected_count, allow_row_invalid=allow_row_invalid)
    run_started = utc_now()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ("-probe" if probe_only else "")
    output_root.mkdir(parents=True, exist_ok=True)
    final_dir = output_root / run_id
    working_dir = output_root / (run_id + ".tmp")
    if final_dir.exists() or working_dir.exists():
        raise FileExistsError(f"Run directory already exists: {final_dir}")

    restored = _load_checkpoint(resume_from, input_digest) if resume_from else {}
    working_dir.mkdir(parents=True)
    evidence_dir = working_dir / "evidence"
    evidence_dir.mkdir()

    client = client or HtmlClient(contact_url, cache_dir, max_requests=2 + 3 * len(records))
    guard = RobotsGuard(client)
    review_decisions = load_review_decisions(review_from)
    all_results = [_empty_result(record) for record in records]
    restored_ids: list[str] = []
    for row in all_results:
        saved = restored.get(row["input_id"])
        if saved and saved.get("status") in RESUMABLE_DONE:
            row.update(saved)
            restored_ids.append(row["input_id"])

    page_cache: dict[str, tuple[object, object]] = {}
    file_cache: dict[str, tuple[object, object]] = {}
    processed_count = len(restored_ids)
    stop_reason = ""
    run_status = "COMPLETED"
    process_records = records[:3] if probe_only else records
    pending_indexes = [i for i, r in enumerate(process_records) if r.input_id not in restored_ids]
    checkpoint_meta = {
        "job_id": JOB_ID, "config_version": CONFIG_VERSION, "code_version": __version__,
        "input_file_sha256": input_digest, "run_id": run_id, "probe_only": probe_only,
        "resumed_from": str(resume_from) if resume_from else "",
    }

    if pending_indexes:
        try:
            guard.preflight()
        except StopRun as exc:
            run_status = "STOPPED"
            stop_reason = exc.reason_code
            for index in pending_indexes:
                all_results[index]["status"] = "BLOCKED"
                all_results[index]["reason_code"] = exc.reason_code
                all_results[index]["retrieved_at_utc"] = exc.response.retrieved_at_utc if exc.response else utc_now()
            pending_indexes = []

    for position, index in enumerate(pending_indexes):
        record = process_records[index]
        result = all_results[index]
        try:
            outcome = _process_one(record, result, client, guard, refresh, enable_api, page_cache, file_cache)
        except KeyboardInterrupt:
            all_results[index] = _empty_result(record)  # never checkpoint a half-processed row
            _write_checkpoint(working_dir, checkpoint_meta, all_results)
            raise RunInterrupted(working_dir, processed_count, len(process_records)) from None
        processed_count += 1
        if outcome is not None:
            run_status = "STOPPED"
            stop_reason = outcome
            for later in pending_indexes[position + 1:]:
                all_results[later]["status"] = "NOT_PROCESSED"
                all_results[later]["reason_code"] = "BATCH_STOPPED:" + outcome
                all_results[later]["retrieved_at_utc"] = utc_now()
            _write_checkpoint(working_dir, checkpoint_meta, all_results)
            break
        _write_checkpoint(working_dir, checkpoint_meta, all_results)

    if probe_only:
        return _finish_probe(working_dir, final_dir, evidence_dir, all_results, run_id, input_digest,
                             processed_count, run_status, stop_reason, guard, client)

    for row in all_results:
        _write_row_evidence(row, evidence_dir)
    review_stats = apply_review_decisions(all_results, review_decisions)
    status_counts = dict(Counter(row["status"] for row in all_results))
    if run_status != "STOPPED" and any(s in status_counts for s in ("FETCH_ERROR", "NEEDS_REVIEW", "BLOCKED", "RATE_LIMITED", "NOT_PROCESSED", "INVALID_INPUT")):
        run_status = "COMPLETED_WITH_ERRORS"
    ready_count = sum(1 for row in all_results if ready_record(row) is not None)
    run_report = {
        "job_id": JOB_ID, "run_id": run_id, "run_mode": "full",
        "started_at_utc": run_started, "finished_at_utc": utc_now(),
        "code_version": __version__, "config_version": CONFIG_VERSION,
        "input_file_sha256": input_digest, "input_count": len(records), "processed_count": processed_count,
        "resumed_from": str(resume_from) if resume_from else "",
        "rows_restored_from_checkpoint": restored_ids,
        "unique_input_article_urls": len({canonical_request_key(r.wikipedia_url) for r in records if r.valid}),
        "unique_canonical_pages": len({canonical_request_key(r["canonical_page_url"]) for r in all_results if r.get("canonical_page_url")}),
        "unique_image_files": len({r["file_title"].casefold() for r in all_results if r.get("file_title")}),
        "request_count": client.request_count, "cache_hits": client.cache_hits, "retry_count": client.retry_count,
        "max_request_budget": client.max_requests,
        "status_counts": status_counts,
        "complete_candidate_count": sum(1 for row in all_results if _is_complete_candidate(row)),
        "metadata_complete_count": sum(1 for row in all_results if _is_complete_candidate(row) and row.get("license_status") == "SUPPORTED_METADATA"),
        "review": review_stats,
        "review_approved_count": sum(1 for row in all_results if row.get("reviewed") == "APPROVED"),
        "review_rejected_count": sum(1 for row in all_results if row.get("reviewed") == "REJECTED"),
        "review_pending_count": sum(1 for row in all_results if row.get("reviewed") == "PENDING"),
        "ready_for_import_count": ready_count,
        "run_status": run_status, "stop_reason": stop_reason,
        "robot_policy_evidence": guard.evidence, "request_log": client.request_log,
        "acceptance_note": "Extraction, metadata completeness, human review and import-readiness are reported separately; none of them is an accuracy score or legal clearance.",
    }
    write_outputs(working_dir, all_results, run_report)
    working_dir.replace(final_dir)
    return final_dir


# --------------------------------------------------------------------------- per input


def _process_one(record: InputRecord, result: dict, client: HtmlClient, guard: RobotsGuard, refresh: bool,
                 enable_api: bool, page_cache: dict, file_cache: dict) -> str | None:
    """Fill ``result`` for one input. Returns a stop reason when the whole batch must stop."""
    if not record.valid:
        result["status"] = "INVALID_INPUT"
        result["reason_code"] = record.error or "INVALID_INPUT"
        result["retrieved_at_utc"] = utc_now()
        result["_evidence"] = {"validation_error": record.error}
        return None
    article_evidence: list[dict] = []
    commons_evidence: list[dict] = []
    retrieved_at = ""
    try:
        page_key = canonical_request_key(record.wikipedia_url)
        if page_key in page_cache:
            article_response, article = page_cache[page_key]
        else:
            article_response = client.fetch(record.wikipedia_url, route_guard=guard.allows, use_cache=not refresh, purpose="wikipedia_article")
            if article_response.status_code != 200:
                raise FetchError(f"Article page returned HTTP {article_response.status_code}", article_response.status_code)
            article = parse_article(article_response)
            if enable_api and (article.candidate is None or not article.page_id or not article.revision_id):
                _supplement_article_via_api(article, client, refresh)
            page_cache[page_key] = (article_response, article)
            page_cache[canonical_request_key(article.canonical_page_url)] = (article_response, article)
            page_cache[canonical_request_key(article_response.final_url)] = (article_response, article)
        retrieved_at = article_response.retrieved_at_utc
        article_evidence = list(article.evidence)
        if article_response.redirect_chain:
            article_evidence.append({"field": "redirect_chain", "value": article_response.redirect_chain + [article_response.final_url]})
        _apply_article(result, article)
        candidate = article.candidate
        if article.status == "CANDIDATE" and candidate:
            if not candidate.commons_file_page_url or not candidate.file_title:
                result["status"] = "NEEDS_REVIEW"
                result["reason_code"] = candidate.needs_review_reason or "COMMONS_FILE_PAGE_NOT_IDENTIFIED"
            else:
                file_key = canonical_request_key(candidate.commons_file_page_url)
                if file_key in file_cache:
                    commons_response, commons = file_cache[file_key]
                else:
                    commons_response = client.fetch(candidate.commons_file_page_url, route_guard=guard.allows, use_cache=not refresh, purpose="commons_file_page")
                    if commons_response.status_code != 200:
                        raise FetchError(f"Commons file page returned HTTP {commons_response.status_code}", commons_response.status_code)
                    commons = parse_commons(commons_response)
                    if enable_api and (not commons.creator_text or not commons.credit_text or not commons.licenses or not commons.original_file_url):
                        _supplement_commons_via_api(commons, candidate.file_title, client, refresh)
                    file_cache[file_key] = (commons_response, commons)
                    if commons.canonical_file_title:
                        file_cache[canonical_request_key(_commons_page_for_title(commons.canonical_file_title))] = (commons_response, commons)
                retrieved_at = commons_response.retrieved_at_utc
                commons_evidence = list(commons.evidence)
                _apply_commons(result, candidate, commons, commons_response)
        result["retrieved_at_utc"] = retrieved_at or utc_now()
        result["_evidence"] = {"article": article_evidence, "commons": commons_evidence}
        return None
    except StopRun as exc:
        result["status"] = "RATE_LIMITED" if exc.reason_code in {"RATE_LIMITED", "RETRY_AFTER_STOP"} else "BLOCKED"
        result["reason_code"] = exc.reason_code
        result["retrieved_at_utc"] = exc.response.retrieved_at_utc if exc.response else utc_now()
        result["_evidence"] = {"article": article_evidence, "commons": commons_evidence, "stop": {"message": str(exc)}}
        return exc.reason_code
    except FetchError as exc:
        result["status"] = "FETCH_ERROR"
        result["reason_code"] = f"HTTP_{exc.status_code}" if exc.status_code else "NETWORK_ERROR"
        result["retrieved_at_utc"] = retrieved_at or utc_now()
        result["_evidence"] = {"article": article_evidence, "commons": commons_evidence, "fetch_error": str(exc)}
        return None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        result["status"] = "FETCH_ERROR"
        result["reason_code"] = "PARSER_ERROR"
        result["retrieved_at_utc"] = retrieved_at or utc_now()
        result["_evidence"] = {"article": article_evidence, "commons": commons_evidence, "error": f"{type(exc).__name__}: {exc}"}
        return None


def _supplement_article_via_api(article, client: HtmlClient, refresh: bool) -> None:
    try:
        payload, api_resp = client.fetch_api_json("en.wikipedia.org", {
            "action": "query", "titles": article.canonical_title,
            "prop": "info|revisions|pageimages", "piprop": "original|thumbnail|name",
            "pithumbsize": 400, "pilicense": "free", "format": "json", "formatversion": 2,
        }, use_cache=not refresh)
    except FetchError:
        return
    page_id, rev_id, candidate = parse_pageimages_response(payload, api_resp)
    article.page_id = article.page_id or page_id
    article.revision_id = article.revision_id or rev_id
    if article.candidate is None and candidate is not None:
        article.candidate = candidate
        article.status = "NEEDS_REVIEW" if candidate.needs_review_reason else "CANDIDATE"
        article.reason_code = candidate.needs_review_reason or "PAGEIMAGES_FALLBACK"
        article.evidence.extend(candidate.evidence)


def _supplement_commons_via_api(commons, file_title: str, client: HtmlClient, refresh: bool) -> None:
    try:
        payload, api_resp = client.fetch_api_json("commons.wikimedia.org", {
            "action": "query", "titles": "File:" + file_title, "prop": "imageinfo",
            "iiprop": "url|size|mime|timestamp|extmetadata", "iiurlwidth": 400,
            "format": "json", "formatversion": 2,
        }, use_cache=not refresh)
    except FetchError:
        return
    merge_commons_metadata(commons, parse_commons_imageinfo(payload, api_resp))


def _empty_result(record: InputRecord) -> dict:
    row: dict = {field: "" for field in RESULT_FIELDS}
    row.update({"input_id": record.input_id, "place_name": record.place_name, "wikipedia_url": record.wikipedia_url,
                "status": "NOT_PROCESSED", "reason_code": "NOT_STARTED", "reviewed": "PENDING", "license_status": "REVIEW_REQUIRED"})
    row["_evidence"] = {"article": [], "commons": []}
    return row


def _apply_article(row: dict, article) -> None:
    row["canonical_title"] = article.canonical_title
    row["canonical_page_url"] = article.canonical_page_url
    row["page_id"] = article.page_id
    row["revision_id"] = article.revision_id
    row["status"] = article.status
    row["reason_code"] = article.reason_code
    if article.candidate:
        candidate = article.candidate
        row["selection_method"] = candidate.selection_method
        row["file_title"] = candidate.file_title
        row["caption"] = candidate.caption
        row["commons_file_page_url"] = candidate.commons_file_page_url
        row["thumbnail_url"] = candidate.thumbnail_url


def _apply_commons(row: dict, candidate, commons, response) -> None:
    if commons.canonical_file_title:
        row["file_title"] = commons.canonical_file_title
    row["commons_file_page_url"] = response.final_url
    row["original_file_url"] = commons.original_file_url
    if not row.get("thumbnail_url"):
        row["thumbnail_url"] = commons.thumbnail_url
    row["mime_type"] = commons.mime_type
    row["width_px"] = commons.width_px
    row["height_px"] = commons.height_px
    row["creator_text"] = commons.creator_text
    row["creator_url"] = commons.creator_url
    row["credit_text"] = commons.credit_text
    row["source_revision_or_timestamp"] = commons.source_revision_or_timestamp

    licenses = commons.licenses
    selected = select_license(licenses)
    row["license_evidence"] = json.dumps(
        [dict(item, selected=True) if item is selected else item for item in licenses],
        ensure_ascii=False, separators=(",", ":"),
    )
    row["license_status"] = license_status(licenses)
    if selected:
        row["license_short_name"] = selected.get("name", "")
        row["license_url"] = selected.get("url", "")

    flags: list[str] = []
    if len(licenses) > 1 and selected:
        flags.append("MULTI_LICENSE_SUPPORTED_SELECTED")
    if selected and selected.get("basis"):
        flags.append("PD_BASIS:" + selected["basis"])
    if commons.creator_source == "copyright_notice":
        flags.append("CREATOR_FROM_COPYRIGHT_NOTICE")

    title_mismatch = commons.canonical_file_title and _normalize_file_title(commons.canonical_file_title) != _normalize_file_title(candidate.file_title)
    missing = _missing_candidate_fields(row)
    if title_mismatch:
        row["status"], row["reason_code"] = "NEEDS_REVIEW", "COMMONS_TITLE_MISMATCH"
    elif missing:
        row["status"], row["reason_code"] = "NEEDS_REVIEW", "MISSING_OR_AMBIGUOUS_METADATA:" + ",".join(missing)
    elif selected is None:
        row["status"], row["reason_code"] = "NEEDS_REVIEW", "UNSUPPORTED_OR_UNCLEAR_LICENSE_METADATA"
    else:
        default_base = (
            "PAGEIMAGES_FALLBACK" if candidate.selection_method == "pageimages_fallback"
            else ("HTML_OG_IMAGE_FALLBACK" if candidate.selection_method == "html_pageimage_fallback" else "FIRST_INFOBOX_PHOTO")
        )
        base = candidate.needs_review_reason or default_base
        row["status"], row["reason_code"] = "CANDIDATE", ";".join([base] + flags)
        row["attribution_text"] = " — ".join([row["file_title"], row["creator_text"], row["license_short_name"], row["commons_file_page_url"]])


# --------------------------------------------------------------------------- checkpoint


def _write_checkpoint(working_dir: Path, meta: dict, rows: list[dict]) -> None:
    payload = dict(meta)
    payload["saved_at_utc"] = utc_now()
    payload["rows"] = {
        row["input_id"]: {**{k: v for k, v in row.items()}, "_source_hashes": _source_hashes(row)}
        for row in rows if row.get("status") != "NOT_PROCESSED" or row.get("reason_code") != "NOT_STARTED"
    }
    path = working_dir / "checkpoint.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _load_checkpoint(location: Path, input_digest: str) -> dict[str, dict]:
    path = location / "checkpoint.json" if location.is_dir() else location
    if not path.exists():
        raise FileNotFoundError(f"No checkpoint.json found at {location}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("input_file_sha256") != input_digest:
        raise ValueError("Checkpoint belongs to a different input.csv; refusing to resume.")
    if data.get("config_version") != CONFIG_VERSION:
        raise ValueError(f"Checkpoint config_version {data.get('config_version')!r} differs from {CONFIG_VERSION!r}; run fresh instead.")
    rows = data.get("rows", {})
    for row in rows.values():
        row.pop("_source_hashes", None)
        row["reviewed"], row["reviewer_note"] = "PENDING", ""
    return rows


def _source_hashes(row: dict) -> list[str]:
    hashes = set()
    for group in (row.get("_evidence") or {}).values():
        if isinstance(group, list):
            hashes.update(item.get("response_sha256", "") for item in group if isinstance(item, dict))
    hashes.discard("")
    return sorted(hashes)


# --------------------------------------------------------------------------- outputs


def _finish_probe(working_dir, final_dir, evidence_dir, all_results, run_id, input_digest, processed_count,
                  run_status, stop_reason, guard, client) -> Path:
    probe_rows = all_results[:3]
    for row in probe_rows:
        _write_row_evidence(row, evidence_dir)
    probe = {
        "job_id": JOB_ID, "run_id": run_id, "run_mode": "field_probe", "config_version": CONFIG_VERSION,
        "input_file_sha256": input_digest, "probe_input_count": len(probe_rows),
        "processed_count": processed_count, "run_status": run_status, "stop_reason": stop_reason,
        "robot_policy_evidence": guard.evidence, "request_count": client.request_count,
        "cache_hits": client.cache_hits, "retry_count": client.retry_count,
        "rows": [{field: row.get(field, "") for field in RESULT_FIELDS} for row in probe_rows],
    }
    (working_dir / "field-probe.json").write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Three-row field probe", "",
        f"Run status: **{run_status}**; rows processed: {processed_count}/3; requests: {client.request_count}; cache hits: {client.cache_hits}.",
        "", "| input_id | title | method | file | creator | license | status |", "|---|---|---|---|---|---|---|",
    ]
    for row in probe_rows:
        values = [row.get(k, "") for k in ("input_id", "canonical_title", "selection_method", "file_title", "creator_text", "license_short_name", "status")]
        lines.append("| " + " | ".join(v.replace("|", "\\|") for v in values) + " |")
    lines += ["", "Compare these fields with the linked article and Commons pages. A probe is not the 20-row run or human approval."]
    (working_dir / "FIELD_PROBE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    working_dir.replace(final_dir)
    return final_dir


def _write_row_evidence(row: dict, evidence_dir: Path) -> None:
    input_id = row.get("input_id", "row")
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", input_id).strip("._-")[:32] or "row"
    suffix = hashlib.sha256(input_id.encode("utf-8")).hexdigest()[:8]
    filename = f"{token}-{suffix}.json"
    payload = {
        "input_id": input_id, "status": row.get("status"), "reason_code": row.get("reason_code"),
        "article_url": row.get("canonical_page_url") or row.get("wikipedia_url"),
        "commons_file_page_url": row.get("commons_file_page_url"),
        "retrieved_at_utc": row.get("retrieved_at_utc"), "fields": row.get("_evidence", {}),
    }
    (evidence_dir / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    row["evidence_path"] = f"evidence/{filename}"


def _missing_candidate_fields(row: dict) -> list[str]:
    fields = ["file_title", "commons_file_page_url", "original_file_url", "thumbnail_url", "mime_type", "width_px", "height_px", "creator_text", "license_evidence"]
    return [field for field in fields if not row.get(field)]


def _is_complete_candidate(row: dict) -> bool:
    required = ["canonical_page_url", "file_title", "commons_file_page_url", "original_file_url", "thumbnail_url",
                "mime_type", "width_px", "height_px", "creator_text", "license_evidence", "evidence_path"]
    return row.get("selection_method") in VALID_METHODS and all(row.get(k) for k in required)


def _normalize_file_title(value: str) -> str:
    return unquote(value.replace("_", " ")).strip().casefold()


def _commons_page_for_title(title: str) -> str:
    return "https://commons.wikimedia.org/wiki/File:" + quote(title.replace(" ", "_"), safe="()!,:'")
