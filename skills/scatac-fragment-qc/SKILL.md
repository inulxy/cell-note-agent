---
name: scatac-fragment-qc
description: Stage-aware SnapATAC2 QC for fragment-based scATAC/snATAC. Imports fragments, computes TSS enrichment, gates QC thresholds, calls peaks, and produces per-dataset GRCh38 peak matrix.
---

# scATAC Fragment QC

This is one unified route for fragment-based scATAC inputs. It accepts either:

- one five-column `fragments.tsv.gz`/BED-like fragment file (`input_mode=single`),
- a directory containing multiple per-sample fragment files (`input_mode=collection`), or
- a CSV manifest with `sample_id`, `fragments_path`, and optional `metadata_path`.

Both modes execute the same controlled stages and produce one dataset-level cell × peak matrix. Collection mode creates a resumable merged fragment stream, preserves or adds sample-prefixed barcodes, records sample identity in `obs['sample']`, writes `fragment_sample_manifest.csv` and `sample_qc_summary.csv`, and calls MACS3 per sample by default before merging peaks into a common peak set. This deliberately uses a mutable backed AnnData because SnapATAC2 2.9 `AnnDataSet` objects cannot be subsetted by QC filters. Single-file behavior remains dataset-pseudobulk peak calling by default.

Fragment-based scATAC is the preferred path because fragment-level QC and peak calling can be reproduced.

## Use This Skill When

- the dataset has `fragments.tsv.gz` / fragment-like input
- the user wants SnapATAC2 / MACS3 based preprocessing
- the final deliverable should be a dataset-level peak matrix

## Project Source

- script: `scripts/scatac_fragment_qc.py`
- conda env: `snapatac2`
- reference resources: `resource-setup`

## Stages

```text
import -> pre-filter -> filter -> embed -> cluster -> doublet -> call-peaks -> make-peak-matrix -> finalize
```

## Script Interface

```bash
conda run -n snapatac2 python scripts/scatac_fragment_qc.py --stage import \
  --dataset_id <dataset_id> --fragments <fragments.tsv.gz> \
  --genome_build GRCh38 --results_root <results_root>
```

## QC Gates

Enforced by the script:

- `min_fragments` / `max_fragments` / `min_tsse` (stage `filter`)
- `max_blacklist_frac` (stage `filter`, only when `--blacklist_bed` is passed;
  use `reference/hg38-blacklist.v2.bed` from resource-setup). Without the bed
  the gate is recorded in `qc_summary.thresholds_declared_not_applied` with an
  explicit reason instead of silently passing.
- doublet removal via scrublet with `expected_doublet_rate`, then
  `filter_doublets` at the SnapATAC2 default threshold (stage `doublet`;
  there is no score CLI flag)
- `min_frip` (stage `make-peak-matrix`, once the peak universe exists; both
  this and the blacklist fraction use `snapatac2.metrics.frip` so the
  fraction definitions are consistent). Set `--min_frip 0` to disable.
  If the gate removes every cell the stage fails instead of shipping an
  empty matrix.

Applied gates are recorded in `qc_summary.filter_thresholds`; anything not
actually executed stays in `qc_summary.thresholds_declared_not_applied`.

Show pre-filter summaries before applying non-default thresholds.

## Expected Deliverables

- `processed/<dataset_id>/peak_matrix.h5ad`
- `processed/<dataset_id>/peaks.hg38.bed`
- `processed/<dataset_id>/barcodes.tsv.gz`
- `processed/<dataset_id>/qc_summary.json`
- `processed/<dataset_id>/data_card.json`
