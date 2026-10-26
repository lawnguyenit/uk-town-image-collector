"""MediaWiki Action API adapters for PageImages fallback and Commons Imageinfo metadata.

Brief section 8:
- Use MediaWiki Action API for page identity / PageImages (``pilicense=free``) fallback
  and ``imageinfo`` + ``extmetadata`` when appropriate.
- Normalise ``extmetadata`` HTML into plain text and keep only valid HTTPS creator links.
- Never infer creator identity from the uploader (``imageinfo.user``).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .models import CommonsMetadata, FetchResponse, ImageCandidate
from .parsers import (
    EXCLUDED_IMAGE_HINT,
    PD_BASIS_PATTERNS,
    PHOTO_MIME_EXTENSIONS,
    _document,
    _evidence,
    _first_external_or_user_link,
    _https_license_url,
    _is_public_domain_label,
    _text,
    clean_media_url,
    commons_file_url,
)


def parse_pageimages_response(payload: dict, response: FetchResponse) -> tuple[str, str, ImageCandidate | None]:
    """Return ``(page_id, revision_id, candidate)`` from an Action API PageImages response."""
    pages = (payload.get("query") or {}).get("pages") or []
    if not pages or not isinstance(pages[0], dict):
        return "", "", None
    page = pages[0]
    if page.get("missing"):
        return "", "", None
    page_id = str(page.get("pageid") or "")
    rev_id = str(page.get("lastrevid") or "")
    if not rev_id and page.get("revisions"):
        rev_id = str((page["revisions"][0] or {}).get("revid") or "")

    raw_name = (page.get("pageimage") or "").replace("_", " ").strip()
    if not raw_name:
        return page_id, rev_id, None
    thumb = clean_media_url((page.get("thumbnail") or {}).get("source", ""))
    original = clean_media_url((page.get("original") or {}).get("source", ""))
    sample_url = thumb or original
    is_commons = "/wikipedia/commons/" in urlsplit(sample_url).path if sample_url else True
    ev = [_evidence("pageimages_api", "query.pages[0].pageimage", {"pageimage": raw_name, "thumbnail": thumb}, response)]
    if not is_commons:
        return page_id, rev_id, ImageCandidate("", "", "pageimages_fallback", thumbnail_url=sample_url,
                                               evidence=ev, needs_review_reason="NON_COMMONS_IMAGE")
    if EXCLUDED_IMAGE_HINT.search(raw_name) or not raw_name.casefold().endswith(PHOTO_MIME_EXTENSIONS):
        return page_id, rev_id, ImageCandidate("", "", "pageimages_fallback", thumbnail_url=sample_url,
                                               evidence=ev, needs_review_reason="PAGEIMAGE_LOOKS_LIKE_EXCLUDED_ARTIFACT")
    return page_id, rev_id, ImageCandidate(
        file_title=raw_name,
        commons_file_page_url=commons_file_url(raw_name),
        selection_method="pageimages_fallback",
        thumbnail_url=sample_url,
        evidence=ev,
    )


def parse_commons_imageinfo(payload: dict, response: FetchResponse) -> CommonsMetadata:
    """Parse a Commons ``action=query&prop=imageinfo&iiprop=url|size|mime|timestamp|extmetadata`` payload."""
    meta = CommonsMetadata()
    pages = (payload.get("query") or {}).get("pages") or []
    if not pages or not isinstance(pages[0], dict) or pages[0].get("missing"):
        meta.warnings.append("COMMONS_API_FILE_MISSING")
        return meta
    page = pages[0]
    title = str(page.get("title") or "")
    meta.canonical_file_title = re.sub(r"^\s*File\s*:\s*", "", title, flags=re.I).replace("_", " ").strip()
    infos = page.get("imageinfo") or []
    if not infos or not isinstance(infos[0], dict):
        meta.warnings.append("COMMONS_API_NO_IMAGEINFO")
        return meta
    info = infos[0]
    meta.original_file_url = clean_media_url(str(info.get("url") or ""))
    meta.thumbnail_url = clean_media_url(str(info.get("thumburl") or "")) or meta.original_file_url
    meta.mime_type = str(info.get("mime") or "").lower()
    if info.get("width"):
        meta.width_px = str(info["width"])
    if info.get("height"):
        meta.height_px = str(info["height"])
    if info.get("timestamp"):
        meta.source_revision_or_timestamp = str(info["timestamp"])
    meta.evidence.append(_evidence("imageinfo_core", "query.pages[0].imageinfo[0]", {
        "url": meta.original_file_url, "width": meta.width_px, "height": meta.height_px, "mime": meta.mime_type,
    }, response))

    ext = info.get("extmetadata") or {}
    artist_html = _ext_value(ext, "Artist")
    if artist_html:
        text, link = clean_extmetadata_html(artist_html)
        if text:
            meta.creator_text = text
            meta.creator_url = link
            meta.creator_source = "extmetadata_artist"
            meta.evidence.append(_evidence("creator_text", "extmetadata.Artist", {"text": text, "url": link}, response))

    credit_html = _ext_value(ext, "Credit") or _ext_value(ext, "Attribution")
    if credit_html:
        credit_text, _ = clean_extmetadata_html(credit_html)
        if credit_text:
            meta.credit_text = credit_text
            meta.evidence.append(_evidence("credit_text", "extmetadata.Credit", credit_text, response))

    short, _ = clean_extmetadata_html(_ext_value(ext, "LicenseShortName"))
    raw_url, _ = clean_extmetadata_html(_ext_value(ext, "LicenseUrl"))
    url = _https_license_url(raw_url)
    usage, _ = clean_extmetadata_html(_ext_value(ext, "UsageTerms"))
    permission, _ = clean_extmetadata_html(_ext_value(ext, "Permission"))
    if short:
        choice: dict[str, str] = {"name": short, "url": url}
        if re.search(r"creativecommons\.org/publicdomain/zero/1\.0(?:/|$)", url, re.I):
            choice = {"name": "CC0 1.0", "url": url, "source_label": short}
        elif _is_public_domain_label(short):
            basis_blob = " ".join([usage, permission])
            for basis, pattern in PD_BASIS_PATTERNS:
                if pattern.search(basis_blob):
                    choice = {"name": "Public domain", "url": url, "source_label": short, "basis": basis}
                    break
        meta.licenses.append(choice)
        meta.evidence.append(_evidence("license", "extmetadata.LicenseShortName", choice, response))
    return meta


def merge_commons_metadata(primary: CommonsMetadata, secondary: CommonsMetadata) -> CommonsMetadata:
    """Fill blanks in ``primary`` (HTML) from ``secondary`` (Action API) without overwriting HTML evidence."""
    for attr in ("canonical_file_title", "original_file_url", "thumbnail_url", "mime_type",
                 "width_px", "height_px", "credit_text", "source_revision_or_timestamp"):
        if not getattr(primary, attr) and getattr(secondary, attr):
            setattr(primary, attr, getattr(secondary, attr))
    if not primary.creator_text and secondary.creator_text:
        primary.creator_text = secondary.creator_text
        primary.creator_url = secondary.creator_url
        primary.creator_source = secondary.creator_source
    for choice in secondary.licenses:
        if not any(existing.get("name") == choice.get("name") for existing in primary.licenses):
            primary.licenses.append(choice)
    primary.evidence.extend(secondary.evidence)
    return primary


def clean_extmetadata_html(raw: str) -> tuple[str, str]:
    """Strip HTML/scripts/styles from an ``extmetadata`` snippet and return ``(plain_text, first_https_url)``."""
    if not raw or not raw.strip():
        return "", ""
    wrapped = f"<div>{raw}</div>".encode("utf-8")
    doc = _document(wrapped)
    return _text(doc), _first_external_or_user_link(doc)


def _ext_value(ext: dict, key: str) -> str:
    entry = ext.get(key)
    return str(entry.get("value") or "") if isinstance(entry, dict) else ""
