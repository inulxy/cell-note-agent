---
name: processing-pipeline
description: Entry skill that routes curated datasets to one of three supported preprocessing paths: fragment scATAC, peak-matrix scATAC, or multiome.
---

# Processing Pipeline

Per-dataset preprocessing router. The shared invariant is: **every accepted dataset ends as a GRCh38 cell × peak matrix with a reproducible QC record**.

## Use This Skill When

- a curated dataset is ready for preprocessing and QC
- the user wants to rerun or tune thresholds for a dataset
- the user needs to choose between fragment, peak-matrix, or multiome processing

## Project Source

Leaf skills:

- `../normalize-to-peak-matrix/SKILL.md`
- `../scatac-fragment-qc/SKILL.md`
- `../scatac-peak-matrix/SKILL.md`
- `../multiome-qc/SKILL.md`

## Routing

```text
has scATAC fragments       -> normalize-to-peak-matrix -> scatac-fragment-qc
has scATAC peak matrix     -> normalize-to-peak-matrix -> scatac-peak-matrix
has paired RNA+ATAC        -> multiome-qc -> ATAC peak matrix
scRNA-only                 -> out of scope / review
```

## Mandatory Rules

1. Read the route from curation output; do not silently switch path.
2. Use staged QC with explicit threshold gates.
3. Preserve original matrix counts unless a deterministic filtering stage is requested.
4. If input genome is not GRCh38, liftover peak coordinates before handoff or route to review.
5. Do not produce cCRE matrices or token files.
6. Do not rerun non-empty outputs unless the user confirms overwrite/resume behavior.

## Human-Review Triggers

- unknown genome build
- missing peak coordinates for a matrix-only dataset
- >50% cells removed by QC
- barcode overlap below threshold for multiome
- matrix-only dataset with insufficient provenance

## Expected Deliverables

Per dataset:

- `processed/<dataset_id>/peak_matrix.h5ad` or MEX equivalent
- `processed/<dataset_id>/peaks.hg38.bed`
- `processed/<dataset_id>/barcodes.tsv.gz`
- `processed/<dataset_id>/qc_summary.json`
- `processed/<dataset_id>/data_card.json`
