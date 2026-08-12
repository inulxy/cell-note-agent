---
name: external-skill-router
description: Routes CellNoteAgent tasks to trusted external Agent Skills and official references for scATAC, multiome, and RNA-side supporting analysis while preserving CellNoteAgent's peak-matrix contract.
---

# External Skill Router

This skill chooses which trusted external skills/references should be consulted for a dataset, but it does **not** let external content own the final data contract.

CellNoteAgent keeps the invariant:

```text
ATAC-bearing input
  → peak_matrices/<dataset_id>/cell_x_peak.npz + peaks.bed
  → processed/<dataset_id>/peak_matrix.* + peaks.hg38.bed
  → corpus/MANIFEST.json
```

## Use This Skill When

- the user asks whether to use existing external skills instead of writing our own
- a scATAC, multiome, or RNA-side supporting workflow needs a vetted external SOP
- the agent needs to pick skills from `configs/external_skills.json`

## Project Source

- registry: `configs/external_skills.json`
- execution source of truth: `python -m cell_note_agent.external_skills`
- local contract owner: `normalize-to-peak-matrix`, modality QC skills, `handoff-pipeline`

## Workflow

1. Run registry validation:

```bash
python -m cell_note_agent.external_skills validate
```

2. Pick a modality plan:

```bash
python -m cell_note_agent.external_skills plan \
  --modality multiome \
  --dataset_id pbmc_multiome \
  --results_root results/mvp
```

3. Read/review the listed external skills or official docs before executing any derived command.
4. Translate external recommendations into local `scripts/*.py --stage=...` outputs.
5. Stop if a recommended external step cannot produce or support the canonical CellNoteAgent output.

## Trusted Source Tiers

- `official_reference`: official project documentation or data standards.
- `approved_reference`: external Agent Skill from a curated repo; consult/pin/vendor before use.
- `optional_*_reference`: useful for downstream validation, not required for preprocessing.

## Mandatory Rules

1. Never execute remote skill code directly from GitHub at runtime.
2. Pin external skills to a commit before vendoring into a reproducible run.
3. Do not let external skills bypass `normalize-to-peak-matrix` for ATAC-bearing data.
4. Record source URL, ref/commit, role, and review decision in provenance.
5. Treat RNA-only skills as reference/evaluation support, not ATAC pretraining input.
6. Do not reintroduce cCRE mapping or tokenization as active pipeline steps.

## Expected Deliverables

- External skill plan for the dataset/modality.
- Provenance entry listing consulted external skills and official references.
- Canonical peak matrix and final manifest owned by CellNoteAgent.
