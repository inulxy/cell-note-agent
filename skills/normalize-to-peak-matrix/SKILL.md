---
name: normalize-to-peak-matrix
description: Canonical input-normalization skill. Routes fragments, provided peak matrices, and multiome ATAC into one downstream representation: a GRCh38 cell × peak matrix plus peaks.bed.
---

# Normalize To Peak Matrix

This skill is the routing boundary between heterogeneous public inputs and the
project handoff contract. It does not rename arbitrary matrix formats. Every
accepted ATAC-bearing dataset must eventually produce:

```text
<results_root>/processed/<dataset_id>/
├── peak_matrix.h5ad or matrix.mtx.gz
├── peaks.hg38.bed
├── barcodes.tsv.gz
├── qc_summary.json
└── data_card.json
```

Modality-specific QC scripts consume the original supported input format and
write this package. `handoff-pipeline` validates it and builds the corpus
manifest.

## Use This Skill When

- the user provides fragments, a precomputed peak matrix, or a multiome dataset
- the workflow needs a single representation before final packaging
- the agent needs to explain how Pi skills will be sequenced

## Project Source

- execution source of truth: `scripts/normalize_to_peak_matrix.py`
- planner helper: `python -m cell_note_agent.pi_bridge plan-peak-matrix ...`
- conda env: `curator` for planning/registration; modality-specific stages use their own envs

## Input Kinds

| `--input_kind` | Route |
|---|---|
| `fragments` | fragments → `scatac-fragment-qc` → peak calling → canonical peak matrix |
| `peak_matrix` | run matrix QC on the provided `.h5ad`, 10x `.h5`, or MEX directory |
| `multiome` | pair-check RNA/ATAC → ATAC branch to canonical peak matrix; RNA retained as metadata/reference |
| `rna_reference` | reference only; does not enter current ATAC deliverable |

## Workflow Stages

1. `normalize-to-peak-matrix --stage=plan` — write the route declaration.
2. Run the selected modality script on the original input format.
3. Run `handoff-pipeline` stages `cards -> validate -> package`.

`normalize_to_peak_matrix.py` still exposes legacy `.npz` registration stages
for existing callers, but `pi_bridge plan-peak-matrix` intentionally does not
emit them: the QC scripts do not accept that untyped `.npz` contract.

## Script Interface

```bash
python scripts/normalize_to_peak_matrix.py --stage=plan \
  --input_kind peak_matrix \
  --input data/pbmc/peak_matrix.h5ad \
  --peaks data/pbmc/peaks.bed \
  --dataset_id pbmc \
  --results_root results/mvp

python -m cell_note_agent.pi_bridge plan-peak-matrix \
  --input_kind fragments \
  --input data/pbmc/fragments.tsv.gz \
  --dataset_id pbmc \
  --results_root results/mvp
```

## Human Review Triggers

- fragments or multiome route has no successful canonical peak matrix after modality-specific processing
- peak coordinates are missing, mixed genome builds are suspected, or barcode counts do not align
- RNA-only data is mistakenly requested as a final ATAC deliverable

## Expected Deliverables

`peak_matrix_plan.json`, the processed per-dataset peak-matrix package, and a
validated `corpus/MANIFEST.json`.
