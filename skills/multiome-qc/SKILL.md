---
name: multiome-qc
description: Stage-aware multiome preprocessing. Checks RNA/ATAC barcode pairing, runs RNA-side supporting QC, routes ATAC to fragment or peak-matrix processing, and outputs ATAC GRCh38 peak matrix.
---

# Multiome QC

Multiome is retained as an ATAC deliverable path. RNA is used for barcode pairing, QC sanity checks, and optional annotation support; standalone scRNA output is not part of the current handoff.

## Use This Skill When

- a dataset contains paired RNA + ATAC modalities
- barcode overlap and paired-pass logic are required
- the final requested artifact is an ATAC peak matrix

## Project Source

- script: `scripts/multiome_qc.py`
- conda env: `muon` for the peak-matrix branch; `snapatac2` for the fragments
  branch (pair-check/qc-atac/intersect need snapatac2 + MACS3, and that env
  also ships scanpy + hdf5plugin for the RNA side and the paired subset)
- downstream: `../handoff-pipeline/SKILL.md`

## Stages

```text
pair-check -> qc-rna -> qc-atac -> intersect -> finalize
```

## Script Interface

```bash
conda run -n muon python scripts/multiome_qc.py --stage pair-check \
  --dataset_id <dataset_id> --rna <rna.h5ad_or_10x_h5> \
  --atac_matrix <atac_peak_matrix.h5ad_or_10x_h5> --peaks <peaks.bed> \
  --genome_build GRCh38 --results_root <results_root>

# Fragment-backed ATAC uses --atac_fragments instead of --atac_matrix.
```

## QC Gates

- Fragments branch (`qc-atac`): `min_fragments` / `max_fragments` /
  `min_tsse` on backed AnnData, plus scrublet doublet removal. TSSe is
  computed if missing; if that computation fails the stage aborts
  (min_tsse is a hard gate, never silently skipped).
- Peak-matrix branch (`qc-atac`): `--atac_min_counts` (default 1000) and
  `--atac_min_peaks` (default 500) per cell, the same semantics and
  defaults as `scatac_peak_matrix.py --min_counts/--min_peaks`. Failing
  cells are marked `atac_pass=False` (not dropped); `intersect` combines
  them with `rna_pass` into paired-pass. Set both to 0 to disable. The
  stage aborts if no cell passes.

## Mandatory Rules

1. Confirm RNA and ATAC barcode relationship before filtering. Suffix
   normalization (-1/-2/...) that makes two originals collide is reported
   in `qc_summary.barcode_collisions` and those barcodes are excluded from
   pairing (never silently mispair).
2. Track `rna_pass`, `atac_pass`, and `paired_pass` separately.
3. Route ATAC through fragment or peak-matrix logic based on available files.
4. Do not package RNA-only matrices as final deliverables.
5. Record threshold choices and overlap metrics in `qc_summary.json`.
6. Do not run `finalize` until `peak_matrix`, `peaks`, and `barcodes` exist.

Both branches are executable end to end. For fragments input, `intersect`
subsets an in-memory copy of the QC'd ATAC to paired-pass cells, runs
dataset-level MACS3 + merge_peaks (or uses `--peaks` when provided), and
materializes `peak_matrix.h5ad` + `peaks.hg38.bed` + `barcodes.tsv.gz`;
any failure in that chain aborts the stage instead of half-delivering.
The backed working h5ad is never subset in place (anndata-rs backed subset
corrupts the file on real 10x ARC data).

## Expected Deliverables

- `processed/<dataset_id>/peak_matrix.h5ad` or MEX equivalent for ATAC
- `processed/<dataset_id>/peaks.hg38.bed`
- `processed/<dataset_id>/qc_summary.json`
- paired-pass metadata / data card entries
