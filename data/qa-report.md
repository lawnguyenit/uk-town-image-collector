# QA Report — UK Town Image & Attribution Collector

- **Run ID:** `20261026T032327433492Z`
- **Run status:** `COMPLETED`
- **Started / finished (UTC):** `2026-10-26T03:23:27Z` → `2026-10-26T03:23:29Z`
- **Input SHA-256:** `df68e1ae0d38efb2bbdedc80a78c298e53aaf2da7a6cc18f073db96f33eda268`

## Separate operational metrics (Brief §10)

These metrics have different denominators and meanings and are intentionally never merged into a single 'accuracy' number:

| Metric | Count | Notes |
| --- | --- | --- |
| Supplied inputs | 20 | Exact row order and unique `input_id` preserved |
| Unique canonical Wikipedia pages | 20 | Deduplicated in fetch cache |
| Unique Wikimedia Commons files | 20 | At most one candidate per input |
| Extraction coverage (`CANDIDATE`) | 20/20 | Candidate found with complete required metadata |
| Metadata completeness (`SUPPORTED_METADATA`) | 20/20 | Creator + supported license + dimensions + URLs + evidence |
| Rows flagged `NEEDS_REVIEW` | 0/20 | Ambiguous, missing, or unsupported source metadata |
| Error / unprocessed rows | 0/20 | `FETCH_ERROR` / `INVALID_INPUT` / `RATE_LIMITED` / `BLOCKED` / `NOT_PROCESSED` |
| Human review `APPROVED` | 20/20 | Bound to `(input_id, file_title)` and metadata fingerprint |
| Human review `REJECTED` | 0/20 | Explicitly excluded from import |
| Human review `PENDING` | 0/20 | Awaiting human verification |
| `ready_for_import.csv` rows | 20/20 | Requires `CANDIDATE` + `APPROVED` + `SUPPORTED_METADATA` + all required fields |
| HTTP requests / cache hits / retries | 2 / 40 / 0 | Budget cap: 62 |

## Status breakdown

- `CANDIDATE`: 20

## Provenance and review notes

- `SUPPORTED_METADATA` indicates that the source page states a license in the v1 supported set (`CC BY 2.0/3.0/4.0`, `CC BY-SA 2.0/3.0/4.0`, `CC0 1.0`, or `Public domain` with an explicit basis such as `PD-self`). It is not legal advice or clearance of personality, trademark, or local property rights.
- When a file is multi-licensed (for example `CC BY-SA 3.0` + `GFDL`), `license_evidence` preserves every option and the collector selects the deterministic supported license while flagging `MULTI_LICENSE_SUPPORTED_SELECTED` in `reason_code`; a reviewer can still override via `reviewer_license_choice`.
- When creator attribution comes from an explicit copyright notice on the file page rather than a standard `{{Information}}` table, `reason_code` includes `CREATOR_FROM_COPYRIGHT_NOTICE` so the reviewer can double-check it.
- All CSV/XLSX text cells starting with `=`, `+`, `-`, or `@` are apostrophe-prefixed and typed as strings (`data_type='s'`).
