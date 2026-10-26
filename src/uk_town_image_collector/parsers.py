"""HTML parsers for Wikipedia article pages and Wikimedia Commons file-description pages.

Only canonical ``/wiki/`` HTML is parsed. Every extracted value is paired with an
evidence record (locator, value, source URL, response hash, timestamp) so a reviewer
can trace it back to the exact response that produced it.
"""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from lxml import html

from .models import CommonsMetadata, FetchResponse, ImageCandidate, ParsedArticle


EXCLUDED_IMAGE_HINT = re.compile(
    r"(?:^|[\s_.(-])(?:map|location[ _-]?map|locator[ _-]?map|flag|coat[ _-]?of[ _-]?arms|logo|icon|badge|crest|seal|signature)(?:$|[\s_.),-])",
    re.I,
)
PHOTO_MIME_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff")
SKIP_TEXT_TAGS = {"style", "script", "noscript"}
LICENSE_HOSTS = {"creativecommons.org", "www.gnu.org", "commons.wikimedia.org"}

# Public-domain statements are only accepted when the file page states the basis.
PD_BASIS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PD-self", re.compile(r"copyright holder of this work,?\s+(?:hereby\s+)?releases?\s+(?:this work|it)\s+into the public domain", re.I)),
    ("PD-old", re.compile(r"(?:author|creator)[^.]{0,80}?died\s+(?:more than|over|at least)\s+\d+\s+years\s+ago", re.I)),
    ("PD-old", re.compile(r"copyright term is the author'?s life plus\s+\d+\s+years or fewer", re.I)),
    ("PD-USGov", re.compile(r"work of (?:a|an)?\s*(?:employee of )?the (?:U\.?S\.?|United States) federal government", re.I)),
)
CREATOR_NOTICE = re.compile(
    r"^(?:(?:photo(?:graph)?|image|picture)\s+)?(?:©|\(c\)|copyright)\s*(?:\d{4}\s+)?(?P<name>[^,;.\n]{2,80}?)\s*(?:[,;.]|$)",
    re.I,
)


# --------------------------------------------------------------------------- article


def parse_article(response: FetchResponse) -> ParsedArticle:
    doc = _document(response.body)
    result = ParsedArticle()
    canonical_nodes = doc.xpath("//link[translate(@rel,'CANONICAL','canonical')='canonical']/@href")
    canonical_url = urljoin(response.final_url, canonical_nodes[0]) if canonical_nodes else response.final_url
    heading = _text(doc.xpath("//*[@id='firstHeading']"))
    title_from_url = unquote(urlsplit(canonical_url).path.rsplit("/", 1)[-1]).replace("_", " ")
    result.canonical_title = heading or title_from_url
    result.canonical_page_url = canonical_url
    result.page_id, result.revision_id = _article_ids(doc)
    result.evidence.extend([
        _evidence("canonical_title", "//*[@id='firstHeading']", result.canonical_title, response),
        _evidence("canonical_page_url", "//link[@rel='canonical']", canonical_url, response),
    ])

    infoboxes = doc.xpath(
        "//table[contains(concat(' ', normalize-space(@class), ' '), ' infobox ')]"
        " | //div[contains(concat(' ', normalize-space(@class), ' '), ' infobox ')]"
    )
    for box in infoboxes:
        for anchor in box.xpath(".//a[@href and .//img]"):
            target = _file_target(anchor.get("href", ""), response.final_url)
            if not target:
                continue
            source_host, file_title = target
            img_nodes = anchor.xpath(".//img[1]")
            img_node = img_nodes[0] if img_nodes else None
            caption = _caption_for(img_node if img_node is not None else anchor)
            alt = img_node.get("alt", "") if img_node is not None else ""
            hint = " ".join([file_title, alt, caption])
            if EXCLUDED_IMAGE_HINT.search(hint) or not file_title.casefold().endswith(PHOTO_MIME_EXTENSIONS):
                result.evidence.append(_evidence("excluded_infobox_image", _path(anchor), hint, response))
                continue
            thumbnail = ""
            if img_node is not None:
                thumbnail = clean_media_url(urljoin(response.final_url, img_node.get("src") or img_node.get("data-src") or ""))
            media_data = _commons_filename_from_media_url(thumbnail) if thumbnail else None
            media_is_commons = bool(media_data and _normalize_title(media_data[0]) == _normalize_title(file_title))
            is_commons = source_host == "commons.wikimedia.org" or media_is_commons
            media_mismatch = source_host == "en.wikipedia.org" and media_data is not None and not media_is_commons
            review_reason = "FILE_TITLE_MEDIA_MISMATCH" if media_mismatch else ("NON_COMMONS_IMAGE" if not is_commons else "")
            candidate = ImageCandidate(
                file_title=file_title if is_commons else "",
                commons_file_page_url=commons_file_url(file_title) if is_commons else "",
                selection_method="infobox_first_photo",
                caption=caption,
                thumbnail_url=thumbnail,
                evidence=[_evidence("file_link", _path(anchor), anchor.get("href", ""), response)],
                needs_review_reason=review_reason,
            )
            if thumbnail:
                candidate.evidence.append(_evidence("thumbnail_media_url", _path(img_node), thumbnail, response))
            candidate.evidence.append(_evidence("caption", _path(anchor), caption, response))
            result.candidate = candidate
            result.status = "NEEDS_REVIEW" if review_reason else "CANDIDATE"
            result.reason_code = review_reason or "FIRST_INFOBOX_PHOTO"
            result.evidence.extend(candidate.evidence)
            break
        if result.candidate:
            break

    if result.candidate is None:
        _apply_og_image_fallback(doc, result, response)

    if result.page_id:
        result.evidence.append(_evidence("page_id", "mw.config wgArticleId", result.page_id, response))
    if result.revision_id:
        result.evidence.append(_evidence("revision_id", "mw.config wgRevisionId", result.revision_id, response))
    return result


def _apply_og_image_fallback(doc, result: ParsedArticle, response: FetchResponse) -> None:
    og_nodes = doc.xpath("//meta[translate(@property,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='og:image']/@content")
    og_image = clean_media_url(urljoin(response.final_url, og_nodes[0])) if og_nodes else ""
    if not og_image:
        result.status = "NO_IMAGE"
        result.reason_code = "NO_SUITABLE_COMMONS_IMAGE"
        return
    og_evidence = _evidence("og_image", "//meta[@property='og:image']", og_image, response)
    result.evidence.append(og_evidence)
    file_data = _commons_filename_from_media_url(og_image)
    if file_data and not EXCLUDED_IMAGE_HINT.search(file_data[0]) and file_data[0].casefold().endswith(PHOTO_MIME_EXTENSIONS):
        result.status = "CANDIDATE"
        result.reason_code = "HTML_OG_IMAGE_FALLBACK"
        result.candidate = ImageCandidate(file_data[0], commons_file_url(file_data[0]), "html_pageimage_fallback",
                                          thumbnail_url=og_image, evidence=[og_evidence])
    else:
        reason = "OG_IMAGE_LOOKS_LIKE_EXCLUDED_ARTIFACT" if file_data else "NON_COMMONS_OG_IMAGE"
        result.status = "NEEDS_REVIEW"
        result.reason_code = reason
        result.candidate = ImageCandidate("", "", "html_pageimage_fallback", thumbnail_url=og_image,
                                          needs_review_reason=reason, evidence=[og_evidence])


# --------------------------------------------------------------------------- commons


def parse_commons(response: FetchResponse) -> CommonsMetadata:
    doc = _document(response.body)
    metadata = CommonsMetadata()
    title_node = doc.xpath("//*[@id='firstHeading']//*[contains(concat(' ',normalize-space(@class),' '),' mw-page-title-main ')]")
    heading = _text(title_node) or _text(doc.xpath("//*[@id='firstHeading']"))
    metadata.canonical_file_title = re.sub(r"^\s*File\s*:\s*", "", heading, flags=re.I).replace("_", " ").strip() if heading else ""

    original_nodes = doc.xpath(
        "//*[@id='file']//a[contains(concat(' ',normalize-space(@class),' '),' internal ')]/@href"
        " | //div[contains(@class,'fullImageLink')]//a[@href]/@href"
    )
    if original_nodes:
        metadata.original_file_url = clean_media_url(urljoin(response.final_url, original_nodes[0]))
    thumb_nodes = doc.xpath("//*[@id='file']//img[contains(concat(' ',normalize-space(@class),' '),' thumbimage ')]/@src | //*[@id='file']//img/@src")
    metadata.thumbnail_url = clean_media_url(urljoin(response.final_url, thumb_nodes[0])) if thumb_nodes else metadata.original_file_url

    info_text = " ".join(_text(n) for n in doc.xpath("//*[@id='file']//*[contains(@class,'fileinfo')] | //div[contains(@class,'fullMedia')]"))
    dimensions = re.search(r"([\d,]+)\s*[×xX]\s*([\d,]+)\s*pixels?", info_text, re.I)
    if dimensions:
        metadata.width_px = dimensions.group(1).replace(",", "")
        metadata.height_px = dimensions.group(2).replace(",", "")
    mime = re.search(r"MIME\s+type:\s*([\w.+-]+/[\w.+-]+)", info_text, re.I)
    if mime:
        metadata.mime_type = mime.group(1).lower()
    if not metadata.mime_type and metadata.original_file_url:
        metadata.mime_type = mimetypes.guess_type(urlsplit(metadata.original_file_url).path)[0] or ""

    creator_cell = _find_info_row(doc, {"author", "artist", "creator", "photographer"})
    if creator_cell is not None:
        metadata.creator_text = _text(creator_cell)
        metadata.creator_url = _first_external_or_user_link(creator_cell)
        metadata.creator_source = "information_template_author"
        metadata.evidence.append(_evidence("creator_text", _path(creator_cell), metadata.creator_text, response))
    else:
        notice = _creator_from_copyright_notice(doc)
        if notice is not None:
            node, name = notice
            metadata.creator_text = name
            metadata.creator_url = _first_external_or_user_link(node)
            metadata.creator_source = "copyright_notice"
            metadata.warnings.append("CREATOR_FROM_COPYRIGHT_NOTICE")
            metadata.evidence.append(_evidence("creator_text", _path(node), {"value": name, "notice": _text(node)}, response))
    if metadata.creator_url:
        metadata.evidence.append(_evidence("creator_url", "creator cell first HTTPS link", metadata.creator_url, response))

    credit_cell = _find_info_row(doc, {"credit", "credit line", "attribution", "source"})
    if credit_cell is not None:
        metadata.credit_text = _text(credit_cell)
        metadata.evidence.append(_evidence("credit_text", _path(credit_cell), metadata.credit_text, response))
    elif notice := _creator_from_copyright_notice(doc):
        metadata.credit_text = _text(notice[0])
        metadata.evidence.append(_evidence("credit_text", _path(notice[0]), metadata.credit_text, response))

    content_text = _text(doc.xpath("//*[@id='mw-imagepage-content']")) or _text(doc.xpath("//body"))
    for box in doc.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' licensetpl ')]"):
        choice = _license_choice(box, response, content_text)
        if choice is None:
            continue
        if choice not in metadata.licenses:
            metadata.licenses.append(choice)
        metadata.evidence.append(_evidence("license", _path(box), choice, response))

    page_modified = _text(doc.xpath("//*[@id='footer-info-lastmod']"))
    if page_modified:
        metadata.source_revision_or_timestamp = page_modified
        metadata.evidence.append(_evidence("source_revision_or_timestamp", "//*[@id='footer-info-lastmod']", page_modified, response))
    metadata.evidence.extend([
        _evidence("original_file_url", "//*[@id='file']//a[contains(@class,'internal')]", metadata.original_file_url, response),
        _evidence("media_properties", "//*[@id='file']//*[contains(@class,'fileinfo')]", info_text, response),
    ])
    if not metadata.creator_text:
        metadata.warnings.append("CREATOR_NOT_FOUND_IN_FILE_DESCRIPTION")
    if not metadata.licenses:
        metadata.warnings.append("LICENSE_NOT_FOUND_IN_FILE_DESCRIPTION")
    if not metadata.original_file_url:
        metadata.warnings.append("ORIGINAL_FILE_URL_NOT_FOUND")
    return metadata


def _license_choice(box, response: FetchResponse, content_text: str) -> dict[str, str] | None:
    short = _text(box.xpath(".//*[contains(concat(' ',normalize-space(@class),' '),' licensetpl_short ')]"))
    if not short:
        return None
    link = _https_license_url(_text(box.xpath(".//*[contains(concat(' ',normalize-space(@class),' '),' licensetpl_link ')]")))
    if not link:
        for node in box.xpath(".//a[@href]"):
            link = _https_license_url(urljoin(response.final_url, node.get("href", "")))
            if link:
                break
    choice: dict[str, str] = {"name": short, "url": link}
    if re.search(r"creativecommons\.org/publicdomain/zero/1\.0(?:/|$)", link, re.I):
        choice = {"name": "CC0 1.0", "url": link, "source_label": short}
    elif _is_public_domain_label(short):
        for basis, pattern in PD_BASIS_PATTERNS:
            match = pattern.search(content_text)
            if match:
                choice = {
                    "name": "Public domain",
                    "url": link or (response.final_url.split("#", 1)[0] + "#Licensing"),
                    "source_label": short,
                    "basis": basis,
                    "basis_text": _sentence_around(content_text, match.start(), match.end()),
                }
                break
    return choice


def _https_license_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    if parsed.scheme == "http" and parsed.hostname in {"creativecommons.org", "www.gnu.org"}:
        value = "https://" + value[len("http://"):]
        parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname in LICENSE_HOSTS:
        return value
    return ""


def _creator_from_copyright_notice(doc):
    """Return (node, name) for an explicit authorship notice such as ``Photograph © Jane Doe``.

    License templates are excluded so boilerplate text is never treated as a creator.
    """
    scope = doc.xpath("//*[@id='mw-imagepage-content']")
    if not scope:
        return None
    for node in scope[0].xpath(".//td | .//p | .//li | .//dd | .//div[not(*)]"):
        if node.xpath("ancestor-or-self::*[contains(@class,'licensetpl') or contains(@class,'layouttemplate')]"):
            continue
        text = _text(node)
        match = CREATOR_NOTICE.match(text)
        if match:
            name = match.group("name").strip()
            if name and not re.search(r"\b(?:license|licence|public domain|creative commons|all rights)\b", name, re.I):
                return node, name
    return None


# --------------------------------------------------------------------------- licenses


def license_status(licenses: list[dict[str, str]]) -> str:
    return "SUPPORTED_METADATA" if select_license(licenses) is not None else "REVIEW_REQUIRED"


def is_supported_license(name: str, url: str, basis: str = "") -> bool:
    return is_supported_choice({"name": name, "url": url, "basis": basis})


def is_supported_choice(choice: dict[str, str]) -> bool:
    return _license_rank(choice) is not None


def select_license(licenses: list[dict[str, str]]) -> dict[str, str] | None:
    """Pick one supported license by a documented, deterministic preference.

    Preference: CC0 1.0 > Public domain with stated basis > CC BY (newest first) > CC BY-SA (newest first).
    Unsupported choices (for example GFDL) are preserved in evidence but never selected.
    """
    ranked = [(rank, index, item) for index, item in enumerate(licenses) if (rank := _license_rank(item)) is not None]
    if not ranked:
        return None
    return min(ranked, key=lambda entry: (entry[0], entry[1]))[2]


def _license_rank(choice: dict[str, str]) -> tuple[int, float] | None:
    name = re.sub(r"\s+", " ", choice.get("name", "")).strip()
    url = choice.get("url", "")
    if re.search(r"\bcc0\s*1\.0\b", name, re.I) or re.search(r"creativecommons\.org/publicdomain/zero/1\.0(?:/|$)", url, re.I):
        return (0, 0.0)
    if _is_public_domain_label(name):
        return (1, 0.0) if choice.get("basis") else None
    match = re.search(r"\bcc[\s-]*by(?P<sa>\s*[- ]?\s*sa)?\s*(?P<version>2\.0|3\.0|4\.0)\b", name, re.I)
    if match:
        family = 3 if match.group("sa") else 2
        return (family, -float(match.group("version")))
    return None


def _is_public_domain_label(value: str) -> bool:
    return bool(re.search(r"public\s+domain|^\s*PD\b", value, re.I))


# --------------------------------------------------------------------------- helpers


def clean_media_url(value: str) -> str:
    """Normalise a Wikimedia media URL: force HTTPS and drop ``utm_*`` tracking parameters."""
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    if parsed.hostname not in {"upload.wikimedia.org", "thumb.wikimedia.org"}:
        return value
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit(("https", parsed.netloc, parsed.path, urlencode(query), ""))


def commons_file_url(file_title: str) -> str:
    normalized = file_title.replace("_", " ").strip()
    return "https://commons.wikimedia.org/wiki/File:" + quote(normalized.replace(" ", "_"), safe="()!,:'")


def _document(body: bytes):
    try:
        return html.fromstring(body, parser=html.HTMLParser(encoding="utf-8"))
    except (ValueError, TypeError):
        return html.fromstring(b"<html><body></body></html>")


def _article_ids(doc) -> tuple[str, str]:
    text = "\n".join(doc.xpath("//script/text()"))
    page = re.search(r"wgArticleId\s*[\"']?\s*[:=,]\s*[\"']?(\d+)", text)
    revision = re.search(r"wgRevisionId\s*[\"']?\s*[:=,]\s*[\"']?(\d+)", text)
    return (page.group(1) if page else "", revision.group(1) if revision else "")


def _file_target(href: str, page_url: str) -> tuple[str, str] | None:
    absolute = urljoin(page_url, href)
    parsed = urlsplit(absolute)
    if parsed.hostname not in {"en.wikipedia.org", "commons.wikimedia.org"} or not parsed.path.startswith("/wiki/File:"):
        return None
    title = unquote(parsed.path[len("/wiki/File:"):]).replace("_", " ").strip()
    if not title:
        return None
    return parsed.hostname or "", title


def _commons_filename_from_media_url(value: str) -> tuple[str, str] | None:
    parsed = urlsplit(value)
    if parsed.hostname not in {"upload.wikimedia.org", "thumb.wikimedia.org"}:
        return None
    segments = [unquote(s) for s in parsed.path.split("/") if s]
    if len(segments) < 5 or segments[:2] != ["wikipedia", "commons"]:
        return None
    try:
        index = segments.index("thumb")
        filename = segments[index + 3]
    except ValueError:
        filename = segments[4]
    except IndexError:
        return None
    if not filename or "." not in filename:
        return None
    return filename.replace("_", " "), value


def _caption_for(node) -> str:
    # Multi-image infobox montage: each tile has its own caption block.
    tile = node.xpath("ancestor::div[contains(concat(' ',normalize-space(@class),' '),' tsingle ')][1]")
    if tile:
        caption = _text(tile[0].xpath("./*[contains(@class,'thumbcaption')] | ./div[not(.//img)]"))
        if caption:
            return caption
    figure = node.xpath("ancestor::figure[1]")
    if figure:
        caption = _text(figure[0].xpath("./figcaption"))
        if caption:
            return caption
    cell = node.xpath("ancestor::td[1]")
    if cell:
        classes = cell[0].get("class", "")
        own_caption = cell[0].xpath(".//*[contains(@class,'infobox-caption')]")
        if own_caption:
            return _text(own_caption)
        next_caption = cell[0].xpath("following-sibling::*[contains(@class,'infobox-caption')][1]")
        if next_caption:
            return _text(next_caption)
        if "infobox-image" in classes or "infobox-full-data" in classes:
            text = _text(cell[0].xpath("./*[not(.//img) and not(self::style) and not(self::script)]"))
            if text:
                return text
    return ""


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", unquote(value.replace("_", " "))).strip().casefold()


def _find_info_row(doc, labels: set[str]):
    for row in doc.xpath("//table[contains(@class,'fileinfotpl')]//tr | //tr"):
        th = row.xpath("./th[1]")
        tds = row.xpath("./td")
        heading = (_text(th) or (_text(tds[0]) if tds else "")).rstrip(":").strip().casefold()
        if heading in labels:
            if th and tds:
                return tds[0]
            if len(tds) >= 2:
                return tds[1]
    return None


def _first_external_or_user_link(node) -> str:
    for anchor in node.xpath(".//a[@href]"):
        href = urljoin("https://commons.wikimedia.org/", anchor.get("href", ""))
        parsed = urlsplit(href)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        if parsed.hostname != "commons.wikimedia.org":
            return href
        if "/wiki/User:" in parsed.path:
            return href
    return ""


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start) + 1, 0)
    right = text.find(".", end)
    return text[left:(right + 1 if right != -1 else len(text))].strip()[:400]


def _node_text(node) -> str:
    parts: list[str] = []

    def walk(element) -> None:
        tag = element.tag if isinstance(element.tag, str) else ""
        if not tag or tag.lower() in SKIP_TEXT_TAGS:
            return
        if element.text:
            parts.append(element.text)
        for child in element:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return " ".join(parts)


def _text(nodes) -> str:
    if nodes is None or (isinstance(nodes, list) and not nodes):
        return ""
    if not isinstance(nodes, list):
        nodes = [nodes]
    value = " ".join(_node_text(n) if hasattr(n, "tag") else str(n) for n in nodes)
    return re.sub(r"\s+", " ", value).strip()


def _path(node) -> str:
    try:
        return node.getroottree().getpath(node)
    except Exception:
        return "HTML selector"


def _evidence(field: str, locator: str, value, response: FetchResponse) -> dict[str, object]:
    return {
        "field": field, "locator": locator, "value": value,
        "source_url": response.final_url, "response_sha256": response.sha256,
        "retrieved_at_utc": response.retrieved_at_utc,
    }
