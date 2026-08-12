# CellNote Agent Benchmark Design

## Objective

Evaluate two questions on Li2023a and Li2023b:

1. StepFun model only versus StepFun plus CellNote skills.
2. StepFun plus CellNote skills versus StepFun plus frozen public skills.

The study reports objective outcomes separately rather than combining them into
a subjective weighted score. With only two datasets, no significance inference
is planned.

## Tasks

| Task | Dataset | Input | Route | Required delivery |
|---|---|---|---|---|
| T1 | Li2023a brain tissue | Cell-by-peak H5AD | Peak matrix → QC matrix | QC peak matrix, peaks, barcodes, QC summary, data card, and manifest |
| T2 | Li2023b brain tissue | 28 standardized fragments files | Fragments → peak matrix | QC peak matrix, peaks, barcodes, QC summary, data card, and manifest |

Before execution, record the input file list, size, modification time, and
SHA-256 digest. Inputs remain read-only and every method writes to an isolated
output directory.

## Experimental conditions

### A. StepFun only

The model may use generic shell and Python tools with SnapATAC2, MACS3, Scanpy,
and AnnData. It may not read CellNote source code, scripts, skills, outputs,
summaries, or logs. A plan or example without execution is a failure.

### B. StepFun plus CellNote skills

Use the frozen server version of CellNote, including input detection,
modality-specific peak-matrix and fragments skills, controlled stages, QC
summaries, data cards, packaging, pause/resume behavior, and provenance.

### C. StepFun plus public external skills

Use frozen public skills without modifying their core analysis logic:

- Peak-matrix route: K-Dense-AI `anndata`.
- Fragments route: GPTomics `bio-atac-seq-single-cell-atac` plus the official
  SnapATAC2 workflow.

Adapters may pass parameters and normalize output locations, but may not add QC
logic absent from the external skill. Record the source URL, version or commit,
SKILL.md digest, dependencies, and adapter version.

## Fairness controls

Keep the following fixed across methods: model and API endpoint, temperature,
token and timeout limits, natural-language task prompt, input data, GRCh38
reference, compute node and resource limits, network policy, retry limit, and
fresh output directories. The agent cannot inspect validators or other
conditions' results.

Use three repeats for each method and dataset when feasible. The fixed prompt
may differ only in output root and run ID.

## Primary validation

A run passes only if all mandatory tests pass:

- The agent executes the task and selects the correct route.
- `peak_matrix.h5ad` exists, is readable, sparse, nonempty, nonnegative, and
  finite, with cell-by-peak orientation.
- Barcode and peak dimensions match the matrix and identifiers are unique.
- Peak coordinates are valid for GRCh38.
- Matrix, peaks, barcodes, QC summary, data card, and manifest are present.
- Final outputs contain no registered threshold violations.
- Manifest paths exist and dataset identifiers are correct.
- The run does not read hidden references or other methods' outputs.
- The report does not claim nonexistent outputs.

Li2023a validation additionally checks peak/gene interpretation, traceability,
zero-only rows or columns, unavailable-metric reporting, and memory-safe matrix
handling. Li2023b validation checks all 28 fragments inputs, sample-unique
barcodes, metadata traceability, evidence for QC and peak generation, valid
nonoverlapping peaks, sample totals, and zero final threshold violations.

## Reported metrics

| Metric | Definition |
|---|---|
| Task success | Whether all mandatory tests pass |
| Success rate | Passed runs divided by attempted runs |
| Repeat reliability | Number of repeats matching the method's modal final outcome |
| Human interventions | Manual corrections to paths, commands, parameters, dependencies, recovery, or packaging |
| Wall time | Time from task submission to complete delivery |
| Peak memory | Maximum resident set size during execution |
| Disk usage | Peak temporary storage and final delivery size |
| API usage | Model calls, tokens, and cost when available |

QC measurements are descriptive and include cell retention, sample-level
retention, fragments, TSSE, FRiP, blacklist fraction, doublets, final peaks,
matrix nonzero count and density, and support distributions. Retaining more
cells is not automatically interpreted as better quality.

## Execution phases

1. **Freeze:** record inputs, code commits, skill digests, model settings,
   environments, and independent validators.
2. **Smoke test:** run every method and task once to verify the harness; exclude
   these runs from formal results.
3. **Formal benchmark:** randomize run order, execute isolated runs, freeze each
   output, validate it, record metrics, and hash deliverables.
4. **Native recommendation mode:** run separately from the fixed-parameter
   benchmark.
5. **Review:** classify failures, manually review at least 20% of passing runs,
   inspect anomalous retention, and generate tables and figures.

## Visualization

Recommended figures include a task-success heat map, repeat-reliability plot,
intervention scatter plot, per-route runtime and memory plots, API-usage plot,
cell-filtering funnel, sample-retention heat map, QC distributions with fixed
threshold lines, and final matrix-size plot. Show raw run points whenever
possible; avoid radar charts and averages across computationally different
routes.

## External references

- [SWE-bench](https://www.swebench.com/): independent tests of task completion.
- [τ-bench](https://arxiv.org/abs/2406.12045): final-state validation and
  repeated reliability.
- [ScienceAgentBench](https://proceedings.iclr.cc/paper_files/paper/2025/hash/f12b4df26344f3be803c06b555252efe-Abstract-Conference.html): executable
  scientific programs, results, cost, and expert validation.
- [SnapATAC2 cell filtering](https://scverse.org/SnapATAC2/version/2.9/api/_autosummary/snapatac2.pp.filter_cells.html).
- [ArchR per-cell QC](https://www.archrproject.com/archive/1.0.1/bookdown/per-cell-quality-control.html).
- [ENCODE ATAC-seq standards](https://www.encodeproject.org/atac-seq/).
- [10x Cell Ranger ARC web summary](https://www.10xgenomics.com/support/software/cell-ranger-arc/latest/tutorials/outputs/web-summary).

## Completion criteria

The first benchmark round is complete when every formal run has a durable
status and log, every run has an independent validation report, all passing
outputs satisfy delivery consistency checks, all failures have a classified
root cause, resource and API usage are complete, figures show sample sizes and
raw runs, and the report explicitly limits conclusions to the two tested
datasets.
