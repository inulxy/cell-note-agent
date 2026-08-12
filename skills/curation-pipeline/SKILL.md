---
name: curation-pipeline
description: Entry skill for dataset discovery, metadata standardization, file manifest, eligibility/routing, reference planning, and controlled download planning for scATAC/multiome peak-matrix production.
---

# Curation Pipeline

Discovers candidate datasets, standardizes metadata, builds a file manifest, and routes each dataset into one of the supported processing paths.

## Use This Skill When

- the user wants to find public scATAC-seq / snATAC-seq / multiome datasets
- a list of accessions or publications needs to become a reproducible file manifest
- the pipeline needs crawler outputs before QC / peak matrix generation

## Project Source

- crawler CLI: `./cell-note` or `python -m sc_epi_curator.cli`
- config: `configs/mvp.json`
- related skills: `../resource-setup/SKILL.md`, `../download-validate/SKILL.md`, `../processing-pipeline/SKILL.md`

## Workflow

1. Discover candidates via `sc_epi_curator.cli discover` / `crawl`.
2. Cache raw API/page responses under the run directory.
3. Standardize metadata to `dataset_catalog.csv`.
4. Build `file_manifest.csv` with file type, URL/accession, size/checksum when available.
5. Route datasets:

```text
paired multiome       -> multiome-qc
scATAC + fragments    -> scatac-fragment-qc
scATAC + peak matrix  -> scatac-peak-matrix
scRNA-only            -> out of scope / manual_review
missing files         -> file follow-up
ambiguous             -> manual_review
```

6. Run `download-validate --stage=plan`; only run fetch after explicit user approval.

## Mandatory Rules

1. Do not download large biological files unless the user explicitly asks and `--enable_fetch` is set.
2. Preserve crawler event chains and raw response cache for auditability.
3. Keep eligibility independent of cCRE/tokenization; current deliverable is per-dataset peak matrix.
4. Route ambiguous modality or unclear genome build to `review_queue.csv`.

## Expected Deliverables

- `dataset_catalog.csv`
- `file_manifest.csv`
- `review_queue.csv`
- raw response cache / crawler events
- routing decisions for `processing-pipeline`
