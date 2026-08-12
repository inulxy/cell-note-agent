# CellNote Skills vs. Frozen Public Skills: Final Benchmark

## Study design

- **CellNote condition:** the same StepFun model plus frozen CellNote skills.
- **External condition:** the same StepFun model plus frozen public skills.
- **Datasets:** Li2023a existing peak matrix and Li2023b fragments collection.
- **Runs:** three validated formal runs per dataset and method, for 12 runs.
- **Reporting:** delivery validation, network audit, repeat consistency, QC
  semantics, output differences, runtime, memory, and model calls are reported
  separately. No subjective weighted score is used.
- **Efficiency subset:** external repeat 1 used repaired warm/resume workspaces and
  is excluded from efficiency means. External efficiency uses repeats 2–3;
  CellNote efficiency uses repeats 1–3.
- **Network boundary:** the harness audit is not equivalent to strict offline
  execution. Both Li2023b producer routes triggered an implicit SnapATAC2
  GENCODE annotation download.

## Overview

| Dataset / route | Method | Delivery | Audit | Modal outcome | Runtime, min | Peak RSS, GB | Model calls |
|---|---|---:|---:|---:|---:|---:|---:|
| Li2023a peak-matrix QC | CellNote | 3/3 | 3/3 | 3/3 | 8.9 ± 0.3 | 43.2 ± 0.0 | 18.7 ± 1.5 |
| Li2023a peak-matrix QC | External | 3/3 | 3/3 | 2/3 | 25.4 ± 0.6 | 107.4 ± 2.4 | 20.0 ± 5.7 |
| Li2023b fragments → peaks | CellNote | 3/3 | 3/3 | 3/3 | 75.4 ± 12.1 | 30.5 ± 0.0 | 38.7 ± 4.7 |
| Li2023b fragments → peaks | External | 3/3 | 3/3 | 3/3 | 65.3 ± 6.5 | 25.0 ± 0.0 | 15.5 ± 2.1 |

## Li2023a: peak-matrix QC

- CellNote produced `731,023 × 544,729` in all three repeats, with identical
  complete-scan nonzero counts and total matrix counts.
- External repeats 1 and 3 matched CellNote. External repeat 2 produced
  `851,143 × 544,729`, retaining 120,120 additional cells.
- External repeat 2 interpreted input `obs['n_genes']` as detected peaks per
  cell, while the other runs recomputed the metric from the matrix. This shows
  that file-level validity does not guarantee stable threshold semantics.
- Across comparable runs, CellNote averaged 8.9 minutes and 43.2 GB peak RSS;
  external averaged 25.4 minutes and 107.4 GB.

## Li2023b: fragments to peak matrix

### Repeatability and delivery

- Both methods passed complete delivery validation in 3/3 runs with zero
  registered threshold violations.
- CellNote was identical across repeats: `64,225 × 368,353`, 214,117,317
  nonzero values, and a total matrix count of 225,406,574.
- External was identical across repeats: `66,354 × 380,576`, 231,163,851
  nonzero values, and a total matrix count of 243,862,304.
- Both methods were deterministic within method, but their biological outputs
  differed because their workflow scopes differed.

### Output differences

- All 64,225 CellNote cells were present in the external result. External kept
  2,129 additional cells; the cell-set Jaccard index was 0.9679.
- Both routes retained 66,354 cells after fragment and TSSE thresholds.
  CellNote then removed 2,129 predicted doublets; external did not perform
  doublet removal.
- CellNote produced 368,353 peaks and external produced 380,576 peaks. The
  exact-interval Jaccard index was 0.2515, while the covered-base Jaccard index
  was 0.8374.
- CellNote includes embedding, clustering, doublet handling, and sample-level
  peak calling. The external route is a narrower delivery baseline.

### Efficiency

- CellNote comparable runs averaged 75.4 minutes and 30.5 GB peak RSS.
- External cold/full repeats 2–3 averaged 65.3 minutes and 25.0 GB peak RSS.
- The external route's lower resource use must be interpreted with its narrower
  scope, which omits CellNote embedding, Leiden clustering, and doublet
  detection.

## Conclusions

1. Both conditions can produce validator-compliant GRCh38 cell-by-peak
   packages on the two tested routes.
2. CellNote achieved a 3/3 modal outcome on both datasets. External achieved
   3/3 on Li2023b and 2/3 on Li2023a, exposing semantic drift in a temporary
   threshold interpretation.
3. CellNote provides a broader and explicitly staged Li2023b QC workflow;
   external provides a narrower but efficient baseline.
4. CellNote is clearly faster and more memory-efficient on Li2023a. Li2023b
   efficiency is not a like-for-like comparison because the scopes differ.
5. Within this benchmark, CellNote's main advantage is stable execution
   semantics, repeatability, and explicit workflow coverage.
6. Future runs should pre-stage reference annotations and add process-level
   egress auditing because both Li2023b methods triggered an implicit reference
   download that the final audit did not capture.

## Limitations

- The benchmark covers two datasets, two routes, and three repeats per method;
  no significance inference is performed.
- The external condition evaluates a frozen, contract-adapted executor rather
  than the average behavior of arbitrary public skills.
- Li2023b methods do not execute identical downstream scopes.
- Complete matrix scans, overlap analyses, and hashes establish delivery and
  repeatability but do not replace biological validation of cell-type
  preservation, clustering stability, or differential accessibility.
