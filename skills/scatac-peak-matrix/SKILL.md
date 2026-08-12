---
name: scatac-peak-matrix
description: QC and standardization path for datasets that already provide a cell × peak matrix but no fragments.
---

# scATAC Peak Matrix

This is the fallback path when fragments are unavailable. It performs matrix-level QC and coordinate standardization; fragment-level metrics cannot be recomputed and must be recorded as unavailable.

## Use This Skill When

- a dataset provides an existing peak/tile matrix
- fragments are unavailable or not feasible to download
- the user wants simple reproducible filtering and GRCh38 standardization

## Project Source

- script: `scripts/scatac_peak_matrix.py`
- conda env: `snapatac2` or `curator`

## Stages

```text
load -> profile -> filter -> standardize -> embed-cluster -> finalize
```

## Matrix-Level QC

- counts per cell
- detected peaks per cell
- cells per peak
- sparsity
- peak coordinate validity
- genome build / liftover status

## Mandatory Rules

1. Do not claim fragment-level QC metrics that cannot be computed.
2. If genome build is not GRCh38, liftover peak coordinates or route to manual
   review. hg19/GRCh37 input is lifted at `standardize` via
   `--liftover_chain reference/hg19ToHg38.over.chain.gz` (from resource-setup;
   pyliftover, curator env). Peaks whose ends map to different chroms/strands
   or whose lifted target duplicates another peak are dropped; the stage
   aborts when the success rate falls below `--min_liftover_rate` (default
   0.95). `qc_summary.liftover` records n_input/n_lifted/rate/chain. Other
   builds still fail to manual review.
3. Mark `representation_quality = matrix_only` in the data card.
4. Keep all filtering thresholds in config/provenance.

## Expected Deliverables

- `processed/<dataset_id>/peak_matrix.h5ad` or MEX equivalent
- `processed/<dataset_id>/peaks.hg38.bed`
- `processed/<dataset_id>/barcodes.tsv.gz`
- `processed/<dataset_id>/qc_summary.json`
- `processed/<dataset_id>/data_card.json`
