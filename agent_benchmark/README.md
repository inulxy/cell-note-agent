# CellNote Agent Benchmarks

This directory packages the two benchmark suites used to evaluate CellNote
Agent under one versioned repository path.

## Package layout

```text
agent_benchmark/
├── benchmark1_step_only/
│   ├── agent.py
│   ├── benchmark1_li2023a_runs.csv
│   ├── benchmark1_summary.csv
│   ├── run_li2023a_repeat3.py
│   └── tools.py
├── benchmark2_skills_comparison/
│   ├── FINAL_COMBINED_BENCHMARK.md
│   ├── benchmark2_runs.csv
│   ├── benchmark2_summary.csv
│   ├── benchmark_design.md
│   ├── build_manifest.json
│   ├── li2023b_overlap.json
│   └── figures/
│       ├── reproducibility_outcomes.png
│       └── runtime_memory.png
├── PACKAGE_MANIFEST.json
└── SHA256SUMS
```

## Benchmark 1: StepFun only vs. CellNote skills

`benchmark1_step_only/` contains the constrained StepFun-only baseline. The
model can call generic shell and Python tools with SnapATAC2, MACS3, Scanpy,
and AnnData, but it cannot access CellNote scripts, skills, prior outputs, or
prior QC summaries.

The baseline defines two representative tasks:

- Li2023a: QC and delivery from an existing cell-by-peak matrix.
- Li2023b: QC and peak-matrix generation from a fragments collection.

`run_li2023a_repeat3.py` is the three-repeat runner used to collect runtime,
peak RSS, model-call count, cell retention, logs, and output paths for Li2023a.
`benchmark1_li2023a_runs.csv` and `benchmark1_summary.csv` contain the compact
run-level and combined Benchmark 1 metrics used in the repository summary.
The scripts retain the original server paths used in the experiment so the
published baseline remains auditable. Adapt those paths before running in a
different environment.

Set the API credential at runtime; no credential is included in this package:

```bash
export STEPFUN_API_KEY=your-key
cd agent_benchmark/benchmark1_step_only
python run_li2023a_repeat3.py
```

## Benchmark 2: CellNote skills vs. external skills

`benchmark2_skills_comparison/` contains the compact run-level results,
aggregate summary, resource and reproducibility figures, overlap analysis,
design document, and final interpretation for StepFun plus CellNote skills
versus StepFun plus frozen public external skills.

The two evaluated routes are:

- Li2023a: existing peak-matrix QC.
- Li2023b: fragments-to-peak-matrix processing.

Large source datasets, model caches, intermediate matrices, Conda
environments, and API credentials are intentionally excluded.

## Integrity

`PACKAGE_MANIFEST.json` records the source package mapping and exclusions.
`SHA256SUMS` contains a SHA-256 digest for every packaged benchmark artifact
except the checksum file itself.
