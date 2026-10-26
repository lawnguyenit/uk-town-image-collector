# Module Catalog — `uk_town_image_collector` (v1.0.0)

This catalog documents every reusable module in `src/uk_town_image_collector`, its contract, inputs/outputs, test coverage, and operational boundaries (Brief §11).

| Module | File | Role | Reusable across jobs? |
| --- | --- | --- | --- |
| `validation` | `src/uk_town_image_collector/validation.py` | Pre-request `input.csv` schema, uniqueness, and Wikipedia URL namespace validation | Reuse / adapt |
| `robots` | `src/uk_town_image_collector/robots.py` | Fresh `robots.txt` preflight and canonical route guard | Reuse |
| `transport` | `src/uk_town_image_collector/transport.py` | Bounded sequential HTTP/API fetcher, gzip cache, retry & stop policy | Reuse |
| `parsers` | `src/uk_town_image_collector/parsers.py` | Wikipedia infobox image discovery, Commons HTML metadata & license normaliser | Reuse |
| `mediawiki_api` | `src/uk_town_image_collector/mediawiki_api.py` | MediaWiki Action API `pageimages` fallback and `imageinfo`/`extmetadata` sanitiser | Reuse |
| `review` | `src/uk_town_image_collector/review.py` | Review-state binding `(input_id, file_title)` + SHA-256 metadata fingerprint & `ready_for_import` filter | Reuse |
| `exporter` | `src/uk_town_image_collector/exporter.py` | Formula-safe UTF-8 BOM CSV, 5-sheet XLSX workbook, `evidence-index.csv`, and `qa-report.md` | Reuse |
| `runner` | `src/uk_town_image_collector/runner.py` | Batch orchestrator, canonical page/file deduplication, atomic checkpoint/resume, and `run-report.json` | Adapt per job |
| `cli` | `src/uk_town_image_collector/cli.py` | Windows/POSIX CLI entrypoint | Adapt per job |

---

## 1. `validation` — Input & URL Validation (`v1.0.0`)

- **Input:** Path to `input.csv` (`input_id,place_name,wikipedia_url`), `expected_count` (default `20`), `allow_row_invalid` flag.
- **Output:** `(list[InputRecord], input_sha256)`.
- **Guarantees:**
  - Rejects wrong headers, duplicate or whitespace-padded `input_id`, blank `place_name`, non-HTTPS schemes, credentials (`user:pass@`), ports, query/fragment strings, hosts other than `en.wikipedia.org`, and non-article namespaces (`File:`, `Special:`, `Category:`, `Talk:`, `User:`, `Wikipedia:`, `Template:`, `Help:`, `Portal:`).
  - Never guesses or searches for a replacement article when an input URL is invalid.
- **Tests:** `test_input_validation_enforces_20_rows_unique_id_and_enwiki_article_url`, `test_invalid_url_host_credentials_and_namespace_emit_invalid_input_row`.

## 2. `transport` + `robots` — Bounded HTTP & Action API Fetcher (`v1.0.0`)

- **Input:** `contact_url` (public HTTPS URL for `User-Agent`), `cache_dir`, `min_delay_seconds=1.0`, `timeout_seconds=20`, `max_response_bytes=2_097_152`, `max_requests=62`.
- **Output:** `FetchResponse(requested_url, final_url, status_code, body, sha256, retrieved_at_utc, headers, redirect_chain, from_cache)`.
- **Guarantees:**
  - Single request in flight; minimum 1.0 s pacing between uncached network calls.
  - At most 2 retries (`1s`, `2s` backoff) for network timeouts/errors and HTTP `5xx`.
  - Immediate batch stop (`StopRun`) on HTTP `401`, `403`, `429`, `Retry-After`, CAPTCHA/challenge markup, disallowed redirect host/path, or request budget exhaustion; remaining rows are marked `NOT_PROCESSED` (never `NO_IMAGE`).
  - Compressed `.html.gz` + `.json` cache with SHA-256 verification and 7-day TTL (`robots.txt` is always fetched live).
- **Tests:** `test_404_and_timeout_and_5xx_retry_and_429_stop_leave_remaining_not_processed`, `test_403_access_denial_marks_blocked_and_stops_batch`, `test_route_guard_is_checked_before_http_or_cache`.

## 3. `parsers` — HTML Image Discovery & Commons Metadata Normalisation (`v1.0.0`)

- **Input:** `FetchResponse` for `https://en.wikipedia.org/wiki/<title>` or `https://commons.wikimedia.org/wiki/File:<name>`.
- **Output:** `ParsedArticle` / `CommonsMetadata` with per-field XPath/DOM evidence and response SHA-256.
- **Guarantees:**
  - Selects the first infobox photograph in DOM order that links to a `File:` page and has a photographic extension (`.jpg`, `.jpeg`, `.png`, `.webp`, `.tif`, `.tiff`), skipping maps, locator maps, flags, coats of arms, logos, icons, crests, seals, and badges.
  - Strips `<style>`, `<script>`, and `<noscript>` nodes before text extraction so TemplateStyles CSS inside multi-image infoboxes (`{{multiple image}}`) never leaks into `caption`.
  - Extracts per-tile captions inside multi-image infobox montages (`div.tsingle`).
  - Strips `utm_*` tracking parameters from `original_file_url` and `thumbnail_url` while keeping both URLs distinct.
  - Extracts creator from `{{Information}}` author rows or explicit non-template copyright notices (`Photograph © <Creator>`) and never from upload history.
  - Preserves every license choice in `license_evidence` and deterministically selects a v1-supported license (`CC0 1.0` > `Public domain` with stated basis such as `PD-self` > `CC BY` > `CC BY-SA`).
- **Tests:** `test_article_selects_first_photo_after_skipping_map_and_strips_style_tags_from_montage`, `test_commons_multilicense_selects_supported_license_and_keeps_all_evidence`, `test_public_domain_requires_explicit_basis_and_copyright_notice_extracts_author`, `test_missing_license_stays_review_required`, `test_cc0_one_point_zero_url_is_supported_and_source_label_is_preserved`.

## 4. `mediawiki_api` — PageImages & Imageinfo Adapter (`v1.0.0`)

- **Input:** JSON payload from `en.wikipedia.org/w/api.php` (`prop=info|revisions|pageimages&pilicense=free`) or `commons.wikimedia.org/w/api.php` (`prop=imageinfo&iiprop=url|size|mime|timestamp|extmetadata`).
- **Output:** `(page_id, revision_id, ImageCandidate)` with `selection_method=pageimages_fallback`, or `CommonsMetadata` merged non-destructively with HTML metadata.
- **Guarantees:**
  - Sanitises `extmetadata` HTML into plain text, strips `<script>`/`<style>` tags, and accepts only `https://` creator links.
  - Never copies `imageinfo.user` (the uploader) into `creator_text`.
- **Tests:** `test_no_image_and_pageimages_api_fallback_and_og_image_fallback`, `test_extmetadata_html_strips_scripts_and_non_https_creator_links`.

## 5. `review` — Human Review Binding & Ready Filter (`v1.0.0`)

- **Input:** `review.csv` (`input_id`, `file_title`, `reviewed`, `reviewer_note`, `reviewer_license_choice`, `reviewer`, `reviewed_at_utc`, `review_fingerprint`).
- **Output:** Updated records, review application stats, and filtered `ready_for_import` rows.
- **Guarantees:**
  - Binds decisions to `(input_id, file_title)` AND a 16-hex SHA-256 `review_fingerprint` over `(input_id, file_title, selection_method, original_file_url, creator_text, license_short_name)`. If the selected image or its core metadata changes on a later run, the prior `APPROVED` status is automatically invalidated back to `PENDING`.
  - `ready_for_import.csv` admits only `status=CANDIDATE` + `reviewed=APPROVED` + `license_status=SUPPORTED_METADATA` with every required field populated.
- **Tests:** `test_review_decision_invalidates_when_file_or_metadata_fingerprint_changes`.

## 6. `exporter` + `runner` — Checkpointed Orchestration & Formula-Safe Exports (`v1.0.0`)

- **Input:** Validated inputs, cache directory, review decisions, optional checkpoint path.
- **Output:** Timestamped run directory written atomically via `<run_id>.tmp` rename, containing `results.csv`, `results.xlsx`, `review.csv`, `ready_for_import.csv`, `run-report.json`, `qa-report.md`, `evidence-index.csv`, `checkpoint.json`, and `evidence/*.json`.
- **Guarantees:**
  - Deduplicates canonical Wikipedia pages and Commons file pages in memory and on disk while preserving one output row per `input_id`.
  - Checkpoints after each input; `--resume-from` verifies `input_file_sha256` and `config_version` before restoring completed rows.
  - Prefixes any cell starting with `=`, `+`, `-`, or `@` (after leading whitespace) with `'` in CSV and XLSX and enforces `data_type='s'` across all 5 worksheet tabs (`Results`, `Review`, `Ready for Import`, `Run Summary`, `Data Dictionary`).
- **Tests:** `test_redirect_and_two_inputs_sharing_canonical_page_fetch_once_and_keep_two_rows`, `test_interrupted_batch_writes_checkpoint_and_resumes_without_duplicate_rows`, `test_csv_and_xlsx_neutralise_formulas_and_preserve_vietnamese_unicode`.
