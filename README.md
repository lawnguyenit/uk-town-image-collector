# UK Town Image & Attribution Collector

> **Independent portfolio project inspired by a public freelance brief** ([Freelancer Project #38882143 — Wiki Page Image Scraper](https://www.freelancer.ca/projects/web-scraping/wiki-page-image-scraper)).
> Built as a production-grade, auditable Python data collection & human-review pipeline. No contract or endorsement by the original client is claimed.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](#quick-start-windows-powershell)
[![Tests: 19/19 passing](https://img.shields.io/badge/tests-19%2F19%20passing-brightgreen.svg)](#verification--test-suite)
[![Live Pilot: 20/20 Ready](https://img.shields.io/badge/live%20pilot-20%2F20%20reviewed-success.svg)](#measured-pilot-results-20-uk-towns)
[![Desktop UI: Zero Config](https://img.shields.io/badge/Desktop%20UI-Included-indigo.svg)](#interactive-desktop-application-non-technical-friendly)

![Output Preview](docs/assets/output-preview.png)

## Overview

Content teams curating location pages often spend hours opening Wikipedia articles, skipping locator maps and coats of arms in the infobox, following `File:` links to Wikimedia Commons, separating the actual photographer from the uploader, verifying license terms, and formatting attribution lines.

**`uk-town-image-collector`** automates the discovery, provenance capture, and spreadsheet packaging for a bounded batch of 20 UK towns while keeping a human editor in control of final approval:

- **DOM-order infobox photo discovery** (`infobox_first_photo`) with automatic exclusion of locator maps, flags, coats of arms, crests, seals, icons, and logos — plus clean per-tile caption extraction for multi-image infoboxes (`{{multiple image}}`).
- **Hybrid HTML + MediaWiki Action API architecture:** parses canonical Wikipedia & Commons HTML pages for full DOM evidence and uses the MediaWiki Action API (`pageimages` with `pilicense=free` and `imageinfo`/`extmetadata`) as a structured fallback/enrichment layer.
- **Strict attribution & license hygiene:** distinguishes creator from uploader, preserves all license templates on multi-licensed files (e.g. `CC BY-SA 3.0` + `GFDL`), verifies explicit public-domain bases (`PD-self`, `PD-old`, `PD-USGov`), and formats plain-text attribution lines.
- **Cryptographically bound human review:** `review.csv` decisions bind to `(input_id, file_title)` and a 16-hex SHA-256 metadata fingerprint. If an image or license changes on a later run, stale approvals automatically reset to `PENDING`.
- **Excel-safe deliverables:** writes UTF-8 BOM CSVs and a formatted 5-sheet `.xlsx` workbook (`Results`, `Review`, `Ready for Import`, `Run Summary`, `Data Dictionary`) with formula-injection neutralisation (`=`, `+`, `-`, `@`).

See [`CASE_STUDY.md`](CASE_STUDY.md) for the full portfolio write-up and [`MODULE_CATALOG.md`](MODULE_CATALOG.md) for reusable module contracts.

---

## Measured Pilot Results (20 UK Towns)

Sample artifacts from the live 20-town run (`20261026T032327433492Z`) are included in [`samples/20261026-cotswolds-20/`](samples/20261026-cotswolds-20/):

| Metric | Count | Details |
| --- | --- | --- |
| Supplied inputs (`input.csv`) | `20` | Bibury, Burford, Bourton-on-the-Water, Stow-on-the-Wold, Chipping Campden, Moreton-in-Marsh, Painswick, Tetbury, Winchcombe, Castle Combe, Chipping Norton, Charlbury, Northleach, Malmesbury, Fairford, Lechlade, Nailsworth, Dursley, Minchinhampton, Wotton-under-Edge |
| Unique canonical Wikipedia pages | `20` | Deduplicated in cache; `page_id` & `revision_id` captured for all 20 |
| Unique Wikimedia Commons files | `20` | Separate `original_file_url` and `thumbnail_url` (with `utm_*` stripped) |
| Extraction coverage (`CANDIDATE`) | `20 / 20` | 100% of supplied towns resolved to a Commons photograph |
| Metadata completeness (`SUPPORTED_METADATA`) | `20 / 20` | Author, license, dimensions, MIME type, and SHA-256 field evidence |
| Human review (`APPROVED`) | `20 / 20` | Verified against source pages and recorded in `review.csv` |
| Import-ready (`ready_for_import.csv`) | `20 / 20` | `CANDIDATE` + `APPROVED` + `SUPPORTED_METADATA` |

---

## Interactive Desktop Application (Non-Technical Friendly)

For business clients, content editors, and non-technical stakeholders who want a standard desktop app experience without typing terminal commands or handling command-line arguments:

### 1-Click Launch on Windows

- **With Python installed:** Double-click **`start_app.bat`** (or execute `python app.py`).
- **Standalone Windows Executable (`.exe`):** Run **`build_exe.bat`** to generate a single-file `dist\UKTownImageCollector.exe` (~16 MB). Clients without Python can simply double-click this `.exe` to run the application immediately with zero setup.

The application automatically opens as a native desktop window (via Microsoft Edge App Mode, or default system browser):
- **Visual Candidate Grid:** Every town is displayed as an interactive card featuring photo preview thumbnail, Commons links, photographer, dimensions, and license status.
- **1-Click Copy Attribution:** Formatted plain-text attributions ready to paste with an instant confirmation toast.
- **Interactive Review:** Approve or Reject candidates with a single click; add notes or override licenses dynamically. All edits automatically re-export `ready_for_import.csv` and `results.xlsx` on disk in real time.
- **Bulk Approve:** Sign off on all valid candidates at once with a single click.
- **Instant Deliverable Downloads:** One-click download buttons for `ready_for_import.csv`, `results.xlsx`, `review.csv`, and `QA_REPORT.md`.
- **Output Folder Shortcut:** Direct button to open the run folder in Windows File Explorer.

---

## Quick Start (Windows PowerShell)

Requires **Python 3.11+**.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .

# Configure your public project/profile URL for the Wikimedia User-Agent header:
$env:WIKI_CONTACT_URL = "https://github.com/lawnguyenit/uk-town-image-collector"
```

### 1. Run a 3-row pilot probe

```powershell
.\.venv\Scripts\python.exe -m uk_town_image_collector --input input.csv --output outputs --cache cache --probe-only
```

### 2. Run the full 20-row batch

```powershell
.\.venv\Scripts\python.exe -m uk_town_image_collector --input input.csv --output outputs --cache cache
```

### 3. Record human review decisions and generate `ready_for_import.csv`

Open `outputs\<run_id>\review.csv` (or the `Review` sheet in `results.xlsx`), set `reviewed` to `APPROVED` or `REJECTED`, add `reviewer_note` (and `reviewer_license_choice` if overriding a multi-licensed file), then re-run with `--review-from`:

```powershell
.\.venv\Scripts\python.exe -m uk_town_image_collector --input input.csv --output outputs --cache cache --review-from outputs\<run_id>\review.csv
```

### 4. Resume an interrupted batch

Every completed input is checkpointed to `outputs\<run_id>.tmp\checkpoint.json` (keyed by input SHA-256 and `config_version`). Resume with:

```powershell
.\.venv\Scripts\python.exe -m uk_town_image_collector --input input.csv --output outputs --cache cache --resume-from outputs\<run_id>.tmp
```

---

## Output Schema & Status Codes

Each run writes an atomic timestamped folder under `outputs\<run_id>\`:

- `results.csv` & `results.xlsx` (`Results`, `Review`, `Ready for Import`, `Run Summary`, `Data Dictionary` sheets)
- `review.csv` (human review worksheet with `review_fingerprint`)
- `ready_for_import.csv` (approved, metadata-complete rows ready for CMS import)
- `run-report.json` & `qa-report.md` (request/cache/retry counts, separate coverage/completeness/review metrics, artifact SHA-256 hashes)
- `evidence-index.csv` & `evidence\*.json` (per-field XPath/API locators, observed values, timestamps, and response SHA-256 hashes)

### Row `status` values

| Status | Meaning |
| --- | --- |
| `CANDIDATE` | Commons photograph found with creator, dimensions, URLs, and a v1-supported license |
| `NEEDS_REVIEW` | Image or metadata requires human resolution (non-Commons file, missing creator, or unsupported/unclear license) |
| `NO_IMAGE` | No suitable infobox or fallback image found on the Wikipedia article |
| `INVALID_INPUT` | Input URL or row failed validation (when `--allow-invalid-rows` is used) |
| `FETCH_ERROR` | Per-item HTTP `4xx`/`5xx` or network error after bounded retries |
| `RATE_LIMITED` | HTTP `429` or `Retry-After` encountered; batch stops immediately |
| `BLOCKED` | HTTP `401`/`403`, CAPTCHA challenge, or `robots.txt` denial; batch stops immediately |
| `NOT_PROCESSED` | Input was not reached because the batch stopped earlier |

---

## Verification & Test Suite

Run the offline contract and integration test suite (16 tests covering input validation, map exclusion, multi-image montage captions, `pageimages` API & `og:image` fallbacks, multi-license selection, `PD-self` verification, copyright-notice creator extraction, `extmetadata` HTML sanitisation, redirects + canonical deduplication, `404`/timeout/`5xx`/`403`/`429` handling, checkpoint/resume, review fingerprint invalidation, and CSV/XLSX formula neutralisation):

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
