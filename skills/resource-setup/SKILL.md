---
name: resource-setup
description: Prepares GRCh38 reference assets for scATAC/multiome QC and peak-matrix output: chromosome sizes, ENCODE blacklist, and optional hg19->hg38 liftover chain, all with pinned URLs and sha256 verification.
status: executable
---

# Resource Setup

Prepares reference resources needed for QC and GRCh38 peak matrix output. This no longer builds a cCRE vocabulary.

Implemented minimal scope (2026-08-08): chromosome sizes + ENCODE/Boyle-Lab
blacklist v2 (+ optional liftover chain). TSS/gene annotation is intentionally
not fetched: the fragment pipeline computes TSSe via snapatac2's built-in
GENCODE annotation; this is recorded in the plan/manifest notes. The hg19->hg38
The liftover *execution* lives in `scatac-peak-matrix` standardize
(`--liftover_chain` + `--min_liftover_rate`, pyliftover in the curator env);
this skill only pins and fetches the chain file.

## Use This Skill When

- processing requires chromosome sizes or the ENCODE blacklist (e.g. blacklist
  fraction / FRiP gates in scatac-fragment-qc)
- a dataset reports hg19 or unknown genome build and needs a GRCh38 decision

## Project Source

- execution source of truth: `scripts/prepare_references.py`
- conda env: `curator` (stdlib only; any Python >= 3.10 works)

## Core Outputs

1. `reference/hg38.chrom.sizes` (UCSC, 455 sequences, sha256-pinned)
2. `reference/hg38-blacklist.v2.bed` (Boyle-Lab v2, 636 intervals, gz sha256-pinned)
3. optional `reference/hg19ToHg38.over.chain.gz` (with `--include_liftover`)
4. `reference/reference_plan.json` (plan stage) and `reference/reference_manifest.json` (verify stage)

## Mandatory Rules

1. Version every downloaded reference asset (URL + sha256 pinned in script).
2. Do not emit cCRE vocab files.
3. Stop if a required reference is missing or fails checksum/format checks.
4. Record source URL, checksum, byte size, and timestamp in the manifest.
5. Only GRCh38 is supported; other builds fail fast.

## Script Interface

```bash
conda run -n curator python scripts/prepare_references.py --stage=plan --out reference/
conda run -n curator python scripts/prepare_references.py --stage=fetch --out reference/
conda run -n curator python scripts/prepare_references.py --stage=verify --out reference/
# add --include_liftover to any stage to include the hg19->hg38 chain file
```

fetch is idempotent: assets already present with matching sha256 are skipped.

## Expected Deliverables

Reference assets and `reference/reference_manifest.json`.
