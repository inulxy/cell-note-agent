# Golden Fixtures

Small real datasets used for pipeline acceptance and integration tests.
Binaries are not committed; run `./fetch_fixtures.sh` (curl + sha256 verify)
to download them into this directory.

| File | Size | Source | sha256 |
|---|---|---|---|
| `scatac_pbmc500_downsample.fragments.tsv.gz` | 20 MB | 10x `atac_pbmc_500_nextgem` official downsample (OSF mirror used by `snapatac2.datasets.pbmc500(downsample=True)`) | `6053cf4578a140bfd8ce34964602769dc5f5ec6b25ba4f2db23cdbd4681b0e2f` |
| `multiome_pbmc3k_arc.filtered_feature_bc_matrix.h5` | 38 MB | [10x ARC 2.0.0 `pbmc_granulocyte_sorted_3k` filtered feature matrix](https://cf.10xgenomics.com/samples/cell-arc/2.0.0/pbmc_granulocyte_sorted_3k/pbmc_granulocyte_sorted_3k_filtered_feature_bc_matrix.h5) | `5fbff5a4d85e0df345f6502e966ec787a8a4c429fd6b88a8772c43fd915cf3ff` |

## What each fixture exercises

- **fragments fixture** -> `scatac-fragment-qc` (env `snapatac2`), all 9 stages
  with **default thresholds** (no special fixture config needed).
- **ARC h5 fixture** -> both `scatac-peak-matrix` (env `cellnote-curator`)
  and `multiome-qc` (env `muon`, pass the same file as `--rna` and
  `--atac_matrix`). The mixed GEX+Peaks feature table also covers the
  "keep only Peaks features" guards in both scripts.

## Verified golden numbers (2026-08-08)

| Pipeline | Command sketch | Expected result |
|---|---|---|
| fragment (9 stages) | `--fragments fixtures/scatac_pbmc500_downsample.fragments.tsv.gz` + defaults, plus `--blacklist_bed reference/hg38-blacklist.v2.bed` | import 584 cells -> filter 469 (blacklist gate removes 0, median frac 0.0065) -> doublet 464 -> FRiP gate removes 1 (median FRiP 0.6083); 44,007 merged peaks; matrix 463 x 44,007; finalize + cards + validate pass |
| peak-matrix (6 stages) | `--matrix fixtures/multiome_pbmc3k_arc.filtered_feature_bc_matrix.h5` + defaults | load keeps 98,319 Peaks (drops 36,601 GEX); filter 2711->2637 cells, peaks ->98,077; finalize + package chain pass |
| multiome, peak-matrix branch (5 stages) | `--rna <arc.h5> --atac_matrix <arc.h5>` + defaults | pair-check drops 36,601 GEX / keeps 98,319 Peaks, overlap 100%; qc-rna 2711->2646; qc-atac 2711->2637 pass (atac_min_counts=1000 / atac_min_peaks=500, same 2637 as the standalone peak-matrix filter); paired-pass 2579; ATAC deliverable 2579 x 98,319 |
| multiome, fragments branch (5 stages) | `--rna <arc.h5> --atac_fragments pbmc_granulocyte_sorted_3k_atac_fragments.tsv.gz` (446 MB, not vendored as a fixture) | env `snapatac2`; import 4463 -> qc-atac 2636 (TSSe + doublets); paired-pass 2505; dataset-level MACS3 -> 102,713 merged peaks; deliverable 2505 x 102,713 uint32 |

Numbers may drift slightly with tool versions; treat large deviations
(>5% cells, >10% peaks) as regressions to investigate.

Full pbmc500 fragments (106 MB, non-downsampled) are intentionally not part
of the fixture set; fetch via `snapatac2.datasets.pbmc500()` when needed
(sha256 `196c5d7ee0169957417e9f4d5502abf1667ef99453328f8d290d4a7f3b205c6c`).
