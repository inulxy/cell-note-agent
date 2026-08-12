---
name: download-validate
description: Controlled, resumable download of eligible datasets with integrity checks (size + checksum), producing a local file store and a missing-file report. Only runs after an eligibility decision has passed.
---

# Download & Validate

Grouped, auditable download planning and validation for eligible datasets only. Fetch is implemented but intentionally guarded behind `--enable_fetch`.

## Use This Skill When

- eligible datasets from `curation-pipeline` need their files fetched locally
- the user wants integrity validation / a missing-file report

## Project Source

- execution source of truth: `scripts/download_validate.py`
- conda env: `curator`

## Inputs

- `--manifest` : `outputs/file_manifest.csv` (eligible rows only)
- `--store` : local download dir (e.g. `data/raw/`)

## Mandatory Rules

1. Never download before eligibility has passed (`policy.require_human` respected).
2. Fetch requires explicit `--enable_fetch`; use resume + retry with backoff, then verify size + checksum when provided.
3. Do not re-download files already present and validated.
4. Record source URL, bytes, checksum, timestamp in `provenance.jsonl`.

## Workflow Stages

1. `--stage=plan`: resolve download URLs per dataset; show plan for confirmation.
2. `--stage=fetch`: download with resume/retry.
3. `--stage=verify`: size + checksum; write `missing_files_report.csv`.

## Failure Handling

- checksum mismatch → re-fetch once; if still bad, mark corrupt + review.
- URL gone / access denied → log, add to missing report, continue others.

## Human-Review Triggers

- persistent checksum failure; access-restricted files; unexpectedly large total size.

## Script Interface

```bash
conda run -n curator python scripts/download_validate.py --stage=plan \
  --manifest outputs/file_manifest.csv --store data/raw/
conda run -n curator python scripts/download_validate.py --stage=fetch \
  --manifest outputs/file_manifest.csv --store data/raw/ --enable_fetch
conda run -n curator python scripts/download_validate.py --stage=verify \
  --manifest outputs/file_manifest.csv --store data/raw/
```

## Expected Deliverables

Validated local files under the store, `missing_files_report.csv`, provenance entries.
