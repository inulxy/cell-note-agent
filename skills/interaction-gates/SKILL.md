---
name: interaction-gates
description: Cross-cutting human-in-the-loop policy for CellNoteAgent. Defines when the agent must pause and ask the user to choose, what evidence to show, what defaults apply if the user skips, and which low-level decisions must never be dumped on the user.
---

# Interaction Gates

This skill is a **policy contract**, not a compute stage. It tells the agent (Pi / LLM / CLI shell) when interaction is mandatory, optional, or forbidden before routing to curation, processing, or handoff skills.

Core principle:

```text
Detect first → ask with evidence → confirm irreversible/expensive steps → then run scripts.
Do not ask about every stage. Do not invent bioinformatics parameters for the user to guess.
```

CellNoteAgent keeps the current deliverable invariant:

```text
ATAC-bearing input → GRCh38 per-dataset cell × peak matrix + QC record + data card / MANIFEST
```

## Use This Skill When

- the agent is about to choose a route, spend network/disk/compute, overwrite outputs, or apply QC thresholds
- input type, genome build, or user goal is ambiguous
- a previous step failed in a way that needs a user policy choice (retry / relax / stop)
- the user asks for “smarter interaction” or fewer blind auto-runs

## Project Source

- policy owner: this skill
- orchestration consumers: `../sc-epi-agent/SKILL.md`, entry/leaf skills under `skills/`
- execution remains in `scripts/` and the interactive agent shell; this file does **not** execute code

## Gate Classes

Use exactly these classes when deciding whether to interrupt:

| Class | Meaning | Typical latency cost if wrong |
|---|---|---|
| `MUST_ASK` | Block until the user chooses | High (wrong route, big download, destructive overwrite) |
| `ASK_WITH_EVIDENCE` | Ask only after a cheap probe/summary is available | Medium |
| `SOFT_ASK` | Offer a recommendation; proceed on timeout/default if allowed | Low–medium |
| `NEVER_ASK` | Agent decides from policy/tools; do not surface to user | N/A |

## MUST_ASK Triggers

Pause and present numbered choices before continuing when any of the following is true:

1. **Irreversible or expensive acquisition**
   - starting real remote download / fetch of biological files
   - total planned download exceeds a configured size budget (if known)

2. **Ambiguous modality route**
   - cannot decide among `fragments` / `peak_matrix` / `multiome` / `out_of_scope`
   - user goal conflicts with detected files (e.g. asks for fragments QC but only peak matrix exists)

3. **Deliverable mismatch**
   - user asks for cCRE matrix, tokens, or RNA-only handoff while current contract is peak-matrix packaging
   - user requests a genome build that is unsupported without liftover/review

4. **Destructive resume**
   - non-empty outputs exist and the next action would overwrite them
   - user asks to “rerun everything” without stating keep/resume/overwrite policy

5. **Quality stop-conditions**
   - planned QC would likely remove nearly all cells, or a completed filter retained ~0 cells
   - all ranked download candidates are raw-only while the user asked for analysis-ready matrices

## ASK_WITH_EVIDENCE Triggers

First run a cheap probe or produce a short summary, then ask:

1. **After input detection**
   - show detected kind, rough size (`n_obs` × `n_vars` or file bytes), genome hint
   - then ask analysis mode (e.g. full QC vs packaging-oriented path)

2. **After search / candidate ranking** (see also Smart Search Interaction)
   - show top candidates with file-role hints (matrix / fragments / raw / unknown)
   - then ask which candidates enter the manifest

3. **After QC profile / distribution summary**
   - show median peaks/counts or a distribution snapshot
   - then ask threshold policy tier or custom bounds

4. **After a failed stage with a clear fork**
   - show failure class (missing dependency, empty filter, unreachable URL, genome unknown)
   - then ask retry / relax / switch route / stop

Evidence shown to the user should be short: 3–8 lines or one compact table. Do not paste full logs.

## SOFT_ASK Triggers

Recommend a default and continue if the user accepts or if auto-default mode is enabled:

1. Whether to skip expensive embedding/clustering on very large matrices
2. Whether to download only a smallest representative file first as a smoke test
3. Whether to package immediately after QC when handoff was implied
4. Whether to reuse an existing crawl/manifest instead of searching again for the same query

## NEVER_ASK Items

Do not ask the user to choose these unless they explicitly enter advanced mode:

1. conda environment / package manager details for a fixed tool path
2. chunk sizes, backed-reader internals, sparse format details
3. every individual `--stage` confirmation after the pipeline plan is approved
4. low-level crawler API knobs (timeouts, retry counts) when defaults are healthy
5. which Python import to use for a decided route

## Question Design Rules

1. **Ask progressively**: route before thresholds; thresholds before long compute; download plan before fetch.
2. **Options over essays**: prefer 2–5 discrete choices; allow one “other / custom” only when necessary.
3. **Options must carry consequences**: e.g. `Standard QC (keeps more cells)` vs `Strict QC (higher purity, fewer cells)`.
4. **One decision per prompt**: do not combine download approval and QC thresholds in one question.
5. **Bind answers to state**: remember choices for the current run; do not re-ask unless the user requests reset or evidence changed materially.
6. **Language**: match the user’s language; keep technical terms stable (`peak matrix`, `fragments`, `GRCh38`).

## Recommended Choice Templates

These are policy templates, not required UI widgets.

### A. Route selection

```text
Detected/requested input is ambiguous. Choose one:
1. Fragment scATAC path
2. Existing peak-matrix path
3. Multiome path (RNA supports ATAC deliverable)
4. Not supported / send to review
```

### B. Compute ambition

```text
Choose processing ambition:
1. Full QC + package
2. Lightweight inspect/package only
3. Cancel
```

### C. Threshold policy

```text
Choose QC strictness:
1. Lenient
2. Standard (recommended)
3. Strict
4. Custom values
```

When evidence exists, append one line such as: `Estimated cells retained: ~XX%`.

### D. Acquisition approval

```text
Download plan ready. Choose one:
1. Fetch selected files now
2. Edit selection / regenerate manifest
3. Stop before download
```

### E. Overwrite policy

```text
Outputs already exist. Choose one:
1. Resume / reuse existing artifacts
2. Overwrite
3. Write to a new run directory
```

### F. Search intent compaction (prefer this over long preference questionnaires)

When the user already stated tissue/modality/file preference in natural language, do **not** re-ask the same fields. Only ask the missing critical slots.

```text
I inferred:
- species: human
- tissue/context: PBMC / normal blood
- modality: scATAC-seq
- prefer: analysis-ready matrix or fragments (deprioritize raw FASTQ)
Missing only:
1. Candidate budget: top 5 / top 10 / top 30
2. Genome preference: GRCh38 (default) / no preference
```

### G. Search result triage

```text
Ranked candidates ready. Choose one:
1. Auto-pick analysis-ready set (matrix/fragments first; recommended)
2. Smallest smoke-test file only
3. Manual pick by numbers (e.g. 1,3,5)
4. Tighten filters and re-rank (matrix-only / fragments-only / size cap)
5. Stop
```

## Smart Search Interaction

Search should optimize for **pipeline-fit files**, not only keyword hits.

### Search flow (interaction view)

```text
User query
  → infer slots from language (SOFT: confirm only gaps)
  → multi-query recall + rank by pipeline_fit
  → ASK_WITH_EVIDENCE: show ranked table (role / size / fit reason)
  → user picks strategy (auto / smoke / manual / re-rank)
  → MUST_ASK before fetch
  → SOFT_ASK: continue to QC or stop at download
```

### Must infer before asking (from user text when possible)

| Slot | Examples in user language | If missing |
|---|---|---|
| species | 人类 / human / mouse | ask or default human for PBMC-style asks |
| tissue/disease | PBMC / brain / tumor | ask only if query is broad (“找 scATAC”) |
| modality | scATAC / multiome | ask if both possible |
| file readiness | matrix / fragments / raw OK | default: prefer matrix & fragments |
| genome | GRCh38 / hg38 | default GRCh38 |
| budget | 只要几个 / 全面搜 | default top 10 |

### Ranking signals to show the user

For each top candidate, show at most:

1. `file_role`: `peak_matrix` / `fragments` / `raw` / `unknown`
2. `fit`: high / medium / low for current CellNoteAgent peak-matrix pipeline
3. `why`: one short reason (e.g. “has filtered_peak_bc_matrix.h5”)
4. `size`: if probed; else `size unknown`

Deprioritize or warn on `raw`-only rows when the user asked for analysis-ready inputs.

### Smart defaults for search

| Situation | Default |
|---|---|
| User names PBMC + scATAC | normal-blood / atlas context; prefer matrix/fragments |
| User says “公开数据集/搜集” only | discover+rank; no fetch |
| Many raw-only hits | auto-propose re-rank: hide raw / keep matrix+fragments |
| One clear small matrix hit | propose smoke-test download |
| Same query rerun in-session | reuse last crawl unless user asks refresh |

### Search NEVER_ASK

Do not ask users to choose crawler sources (GEO/SRA/literature), API keys, or retry counts during normal search. Expose those only in advanced/debug mode.

## Defaults (when SOFT_ASK is allowed)

| Situation | Default |
|---|---|
| Clear `*.h5ad` cell-by-peak input | peak-matrix route |
| Clear `fragments.tsv.gz` | fragment route |
| Paired RNA+ATAC explicitly stated | multiome route |
| Ultra-large matrix | full QC allowed, skip heavy embed/cluster unless requested |
| Search request without fetch language | discover + rank only; do not fetch |
| Genome mentioned as hg38/GRCh38 | accept; otherwise ask or review |
| User says “just prepare package” | packaging-oriented path |

## Failure Handling

- If the user refuses a `MUST_ASK` gate: stop cleanly; record the refusal in run notes; do not partial-fetch or partial-overwrite.
- If evidence is insufficient for `ASK_WITH_EVIDENCE`: downgrade to a narrower probe, or escalate to `MUST_ASK` with an explicit “insufficient evidence” note.
- If the user gives an unsupported goal: offer the nearest supported deliverable or review queue; do not silently reinterpret.

## Human-Review Triggers

Route to review (and explain why) when:

1. modality remains unresolved after one clarification round
2. genome build is unknown and liftover is not approved
3. access-restricted or persistently corrupt downloads block all selected candidates
4. user demands an out-of-contract artifact (e.g. RNA-only FM corpus) as the primary deliverable

## Handoff To Other Skills

After gates are satisfied:

- discovery / manifest / download decisions → `../curation-pipeline/SKILL.md`, `../download-validate/SKILL.md`
- modality QC → `../processing-pipeline/SKILL.md` and the selected leaf skill
- packaging / data card / MANIFEST → `../handoff-pipeline/SKILL.md`
- external SOP consultation → `../external-skill-router/SKILL.md`

## Response Style

When applying this skill, the agent should state:

1. which gate class fired (`MUST_ASK` / `ASK_WITH_EVIDENCE` / `SOFT_ASK`)
2. what evidence was used
3. the exact choices
4. the default if the user accepts the recommendation

Do not claim that this skill ran a script stage. It only governs interaction policy before scripts run.
