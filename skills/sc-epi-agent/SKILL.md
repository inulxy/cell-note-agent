---
name: sc-epi-agent
description: Top-level router for the public scATAC-seq / multiome data curation and FM-corpus workflow. Use as the single entry point; it decides whether the request is curation, modality processing, or handoff, then hands off to the correct entry skill.
---

# sc-epi Curation Agent (Router)

Top-level entry point for turning heterogeneous public scATAC-seq / multiome data into a
reproducible, auditable, FM-ready corpus. This is a **routing and orchestration** skill.
It does not run computation itself; it decides the stage and hands off.

Core principle: **the agent orchestrates trusted tools and records decisions; it does not
invent bioinformatics algorithms.** All computation lives in `scripts/` and is described by
the leaf skills.

## Use This Skill When

- the user wants an end-to-end run of the curation → processing → handoff workflow
- the user has not clearly separated the stage they are in
- the user wants to continue from a previous stage's validated outputs
- the user asks workflow-level questions about the whole project

## Workflow Layers

1. **Curation** (stage 1–6): discovery → metadata standardization → file manifest →
   eligibility/routing → reference feature space → download & validation.
2. **Processing** (stage 7): modality-specific QC (7A fragment scATAC / 7B peak-matrix /
   7C multiome / 7D scRNA-reference).
3. **Handoff** (stage 8–10): unified cCRE mapping → tokenization → FM corpus packaging.

## Primary Entry Skills

- curation entry: `../curation-pipeline/SKILL.md`
- processing entry: `../processing-pipeline/SKILL.md`
- handoff entry: `../handoff-pipeline/SKILL.md`
- interaction policy: `../interaction-gates/SKILL.md`

## Mandatory Operating Rules

1. First determine whether the request is curation, processing, handoff, or a cross-stage continuation.
2. Confirm one top-level results root once per workflow, then derive stage subdirectories automatically.
3. Ask questions progressively; do not front-load later-stage parameters. Follow `interaction-gates`:
   infer search slots from language, ask only gaps, and triage candidates by file role / pipeline fit.
4. Before running any substantial command, show the exact final command and wait for confirmation
   (`MUST_ASK` for downloads, overwrites, and expensive QC).
5. Reuse validated artifacts from earlier stages instead of forcing the user to restate them.
6. Do not silently rerun long jobs if outputs may already exist and are non-empty.
7. If required inputs or dependencies are missing, stop and report clearly; route unresolved
   ambiguity into `outputs/review_queue.csv` rather than guessing.
8. Never auto-download large raw files (fragments/FASTQ/matrices) before an eligibility decision has passed.
9. Prefer analysis-ready inputs (peak matrix / fragments) when the user asked for immediate analysis;
   do not present raw FASTQ/SRA as the default path without saying why.

## Routing Rules

- discovery / metadata / eligibility / reference prep / download  → `../curation-pipeline/SKILL.md`
- per-dataset modality QC (has fragments / peak matrix / multiome / scRNA ref) → `../processing-pipeline/SKILL.md`
- cCRE mapping / tokenization / corpus packaging / data cards → `../handoff-pipeline/SKILL.md`

## Environment Routing (conda)

- discovery / IO / vocab: `curator`
- SnapATAC2 + MACS3: `snapatac2`
- scanpy: `scanpy`
- muon / mudata: `muon`

Do not ask the user to choose the env for a step whose tool is fixed above.

## Response Style

- say whether the request is curation, processing, or handoff
- say which entry skill is being used next
- say which validated inputs are being reused
- do not duplicate leaf-skill detail that already lives in the specialized skills
