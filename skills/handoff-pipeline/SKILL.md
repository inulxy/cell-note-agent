---
name: handoff-pipeline
description: Packages GRCh38 per-dataset peak matrices, QC summaries, data cards, and a manifest for downstream FM consumption. No cCRE mapping or tokenization.
---

# Handoff Pipeline

Packages processed datasets for downstream FM work. The handoff unit is an independent dataset peak matrix, not a unified cCRE vocabulary and not tokenized cell sentences.

## Use This Skill When

- QC/processing has produced one or more per-dataset peak matrices
- the user wants a final manifest/data-card package
- the run needs validation before handoff

## Project Source

- `scripts/package_peak_matrices.py`
- conda env: `curator`

## Stages

```text
cards    -> write/refresh per-dataset data_card.json
validate -> check matrix/peaks/barcodes/qc files exist
package  -> write corpus/MANIFEST.json
```

## Script Interface

```bash
python scripts/package_peak_matrices.py --stage cards \
  --results_root results/mvp

python scripts/package_peak_matrices.py --stage validate \
  --results_root results/mvp

python scripts/package_peak_matrices.py --stage package \
  --results_root results/mvp
```

## Mandatory Rules

1. Package each dataset independently.
2. Keep peak coordinates in GRCh38.
3. Include QC summaries and provenance references.
4. Do not tokenize cells or create cCRE vocabulary outputs.
5. Do not create train/val/test splits in the current scope.

## Expected Deliverables

- `processed/<dataset_id>/data_card.json`
- `processed/<dataset_id>/qc_summary.json`
- `corpus/MANIFEST.json`
