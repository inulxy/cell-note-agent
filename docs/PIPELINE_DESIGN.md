# cell-note-agent — Pipeline

> 目标：把异构的公开 scATAC-seq / multiome 数据，通过 **Pi coding agent + skills + scripts** 驱动的流程，自动整理成 **可复现、可审计、GRCh38 per-dataset peak matrix**。本文以组员更新的 pipeline 分析为主线，同时删去当前阶段不需要的 cCRE mapping、tokenization 和单 scRNA 交付路径。

---

## 1. Orchestrator（总控）

**形态**：MVP 采用 **Pi coding agent** 做对话式编排，`skills/` 负责流程契约，`scripts/` 负责确定性执行。StepFun adapter 可作为 OpenAI-compatible planning layer；后续如需要更强状态图能力，可再加 LangGraph 状态图，当前不自建运行时。

- **输入**：`configs/*.json`（物种、基因组版本、模态、数据源、QC 阈值、输出路径）。
- **输出**：每个 stage 的产物 + `provenance.jsonl`（查询/路由/决策/反馈事件）+ `run_summary.json` / `qc_summary.json`。
- **成功判据**：纳入数据集完成 QC，生成 `processed/<dataset_id>/peak_matrix.*`、`peaks.hg38.bed`、data card；失败样本进入 review queue，而非中断全局。
- **人工审核触发条件**：模态置信度低、配对不明确、缺文件、许可不清、QC 大面积不达标、基因组版本不清。
- **关键能力**：断点续跑（checkpoint per dataset）、幂等（相同输入命中缓存）、每步可回放。

---

## 2. CurationAgent（发现 → 标准化 → 清单 → 纳排）

### 2.1 Dataset Discovery

- **数据源**：GEO/SRA、Europe PMC / literature、accession seeds、allowlisted web pages；后续可扩展 10x Genomics catalog、ENCODE、CELLxGENE/HCA census，以及已发表 FM 的数据清单（EpiAgent / ChromFound / EpiFoundation）。
- **实现**：已合并的 `sc_epi_curator` crawler，可通过 `./cell-note crawl ...` 或 `python -m sc_epi_curator.cli ...` 调用。
- **当前进展**：crawler 初步测试功能正常，可集成到整体 agent 架构做自动搜索；下载数据模块先保留 plan / verify / fetch 的接口，实际大规模下载暂时留空或显式人工确认。
- **输出**：候选数据集列表（原始 API / 页面响应缓存到 `outputs/raw_api_responses/` 或 `runs/<run_id>/raw/`）和 crawler event chain。

### 2.2 Metadata Standardization

把各库元数据映射到统一 schema：

```text
dataset_id, source, species, genome_build, modality, assay,
tissue, n_cells_est, donor, license, files[], pub_ref
```

### 2.3 File Manifest & Availability

确认每个数据集实际提供哪些文件，决定处理分支。**文件优先级**：

```text
fragments.tsv.gz > paired multiome fragments/matrix > cell×peak matrix > only count matrix
```

### 2.4 Eligibility & Routing（决策）

明确、可复现的纳排标准 → 动态路由：

```text
paired multiome       -> ProcessingAgent / multiome-qc
scATAC + fragments    -> ProcessingAgent / scatac-fragment-qc
scATAC + peak matrix  -> ProcessingAgent / scatac-peak-matrix
scRNA-only reference  -> out of scope / manual_review
scATAC but no files   -> file follow-up
ambiguous / low-conf  -> manual_review
excluded              -> stop
```

- **保留三条主线**：`scATAC fragments`、`scATAC peak matrix`、`multiome`。
- **删除旧表示**：不再使用 7A–7D 编号，不再保留单 scRNA 独立分析路径。
- **产出**：`dataset_catalog.csv`、`file_manifest.csv`、`review_queue.csv`、routing decisions。

---

## 3. ResourceAgent（参考资源 + 下载校验）

### 3.1 Reference Assets

当前不构建 cCRE vocabulary，只准备 QC / peak matrix 生成所需资源：

- `hg38.chrom.sizes`
- ENCODE blacklist
- TSS / gene annotation for TSS enrichment
- liftover chains（如 hg19 → hg38）
- `reference_manifest.json`

**统一基因组版本**：最终交付统一到 GRCh38。原始 peak / fragment 如果不是 GRCh38，先 liftover 或进入 manual review；所有转换记录到 provenance。

### 3.2 Download & Integrity

- `download_validate.py --stage=plan`：读取 `file_manifest.csv`，估算大小、输出下载目的地。
- `download_validate.py --stage=verify`：校验已存在文件大小和 checksum。
- `download_validate.py --stage=fetch`：预留 resumable fetch 接口；默认禁用，必须显式 `--enable_fetch`，避免误下大文件。
- **当前边界**：MVP 阶段先完成自动搜索、文件线索整理、下载计划和校验；真正下载可以放在通过审核之后。

---

## 4. ProcessingAgent（按模态分支）

统一原则：所有进入 handoff 的 ATAC 数据最终都是 **GRCh38 cell × peak matrix**。Agent 只负责选分支和参数；实际计算由 Tool/script stage 完成。QC 采用标准流程 + 分支节点：固定默认阈值可复现，关键阈值可通过对话让用户确认。

### 4.1 Normalize To Peak Matrix

`normalize-to-peak-matrix` 是异构输入与后续处理之间的边界：

```text
<results_root>/peak_matrices/<dataset_id>/
├── cell_x_peak.npz
├── peaks.bed
└── peak_matrix_metadata.json
```

它负责生成 route plan、注册已有 peak matrix、验证标准目录；fragment 和 multiome 的重计算仍交给对应 QC skill。

### 4.2 Fragment-based scATAC

调用 SnapATAC2 / MACS3，保留组员 pipeline 中的分析步骤，但输出从统一 cCRE 改为 dataset-level peak matrix：

1. `pp.import_data` / `pp.import_fragments`（fragments → backed AnnData）
2. `metrics.tsse`（TSS enrichment）
3. cell QC 过滤：fragment depth、TSS enrichment、blacklist / mitochondrial fraction（可用时）
4. `pp.add_tile_matrix` / `pp.select_features`
5. `tl.spectral` → `tl.umap` → `pp.knn` → `tl.leiden`
6. doublet scoring / filtering（默认阈值 + 用户确认分支）
7. `tl.macs3` 调 peak（dataset-level 或 cluster-level；暂默认 dataset-level）
8. 生成 **cell × peak matrix**，输出 `peak_matrix.h5ad` + `peaks.hg38.bed`

### 4.3 Peak-matrix scATAC

只有聚合好的 peak matrix、无 fragment 时使用。由于无法重算 fragment-level QC，采用保守 matrix-level QC：

- 可观测指标：counts/cell、detected peaks/cell、cells/peak、sparsity、peak coordinate validity。
- genome build 不是 GRCh38：liftover peak coordinates；资源缺失则进入 manual review。
- 低质量 peak / cell 做简单过滤，保留阈值、过滤前后统计和随机种子。
- 标注 `representation_quality = matrix_only`，写入 data card。

### 4.4 Multiome（RNA + ATAC 配对）

用 muon/mudata 或轻量 AnnData orchestration：

1. 校验 RNA–ATAC barcode 对应关系
2. RNA 做 supporting QC（不作为单独交付路径）
3. ATAC 走 fragment 或 peak-matrix 路径
4. 取 paired-pass 细胞，记录 `atac_pass` / `rna_pass` / `paired_pass`
5. 输出 ATAC 侧 **GRCh38 peak matrix**；RNA 只保留 QC / annotation 支持信息

---

## 5. HandoffAgent（peak matrix → data card → 打包）

### 5.1 Peak Matrix Representation

每个 dataset 单独输出，暂不做跨 dataset consensus peak matrix：

```text
processed/<dataset_id>/
  peak_matrix.h5ad 或 matrix.mtx.gz
  peaks.hg38.bed
  barcodes.tsv.gz
  qc_summary.json
  data_card.json
```

### 5.2 Data Card + MANIFEST

每个数据集输出一张 data card，包括：基本信息、文件、策展决策、QC 摘要、表示方式、provenance。

`package_peak_matrices.py` 汇总：

```text
corpus/MANIFEST.json
```

Manifest 只列出 per-dataset peak matrix，不含 tokens、不含 cCRE vocabulary、不含 train/val/test split。

---

## 6. Tool 层（确定性函数封装）

Agent 调用这些函数 / 脚本：

```text
sc_epi_curator/
  cli.py                     # crawler / discovery / evidence / event chain
  providers/                 # GEO/SRA, Europe PMC, accession, allowlist providers

cell_note_agent/
  step_api.py                # StepFun OpenAI-compatible API adapter
  pi_bridge.py               # deterministic Pi skill plan helper
  external_skills.py         # trusted external SOP registry planner

scripts/
  normalize_to_peak_matrix.py
  download_validate.py
  prepare_references.py
  scatac_fragment_qc.py
  scatac_peak_matrix.py
  multiome_qc.py
  package_peak_matrices.py
  demo_pbmc.py
```

每个函数定义：inputs / outputs / success criteria / failure states / logs。

---

## 7. QC 指标与交互阈值

| 模态 | 指标 | 默认处理 | 可交互阈值 |
|---|---|---|---|
| fragment scATAC | fragment depth、TSS enrichment、FRiP / reads-in-peaks、blacklist fraction、nucleosome signal、doublet score | SnapATAC2 / MACS3 标准流程 | `min_fragments`、`min_tsse`、`max_blacklist_frac`、`min_frip`、`max_doublet_score` |
| peak-matrix scATAC | counts/cell、detected peaks/cell、cells/peak、sparsity、peak coordinate validity | matrix-level 简单过滤 | `min_counts`、`min_peaks`、`min_cells_per_peak` |
| multiome | barcode overlap、RNA supporting QC、ATAC QC、paired-pass cell count | RNA/ATAC 独立 QC 后取交集 | `min_pair_overlap`、RNA supporting QC 阈值、ATAC 阈值 |

原则：默认阈值写入 config，用户修改阈值也写入 `provenance.jsonl` 和 `qc_summary.json`，保证可复现。

---

## 8. External Skills / RAG 知识库（可选）

- **来源**：SnapATAC2 tutorials、Signac/ArchR 方法段、ENCODE ATAC-seq standards、单细胞最佳实践、K-Dense、GPTomics/bioSkills。
- **形态**：外部 skills / 官方 SOP 只作为 consult / pin / vendor / adapter 参考；最终输出契约仍由 CellNoteAgent 控制。
- **约束**：检索或外部建议必须映射到本地 script stage + 默认参数。
- **用途**：优先优化 QC、peak calling、peak matrix 生成流程；不用于重新引入 cCRE mapping/tokenization 主线。
- 引用可溯源（记录命中片段来源、ref/commit、review decision）。

---

## 9. 输出目录结构

```text
outputs/ 或 runs/
├── dataset_catalog.csv          # 标准化候选
├── file_manifest.csv            # 文件与下载线索
├── review_queue.csv             # 人工审核队列
├── provenance.jsonl             # 事件日志
├── run_summary.json             # 运行摘要
├── state.sqlite                 # 可恢复状态
├── raw_api_responses/ 或 raw/    # crawler raw response cache
├── reference/
│   ├── hg38.chrom.sizes
│   ├── hg38-blacklist.v2.bed
│   ├── hg38.tss.bed
│   └── reference_manifest.json
├── peak_matrices/<dataset_id>/
│   ├── cell_x_peak.npz
│   ├── peaks.bed
│   └── peak_matrix_metadata.json
├── processed/<dataset_id>/
│   ├── peak_matrix.h5ad
│   ├── peaks.hg38.bed
│   ├── barcodes.tsv.gz
│   ├── qc_summary.json
│   └── data_card.json
└── corpus/
    └── MANIFEST.json            # per-dataset peak matrix index
```

---

## 10. 配置示例

```jsonc
{
  "genome_build": "GRCh38",
  "deliverable": "per_dataset_peak_matrix",
  "liftover": { "enable": true, "chains": { "hg19": "hg19ToHg38" } },
  "download": {
    "enable_fetch": false,
    "mode": "plan_then_review"
  },
  "qc": {
    "mode": "fixed",                 // fixed | interactive
    "scatac_fragment": {
      "min_fragments": 1000,
      "max_fragments": 100000,
      "min_tsse": 4,
      "max_blacklist_frac": 0.05,
      "min_frip": 0.10,
      "max_doublet_score": 0.5
    },
    "scatac_peak_matrix": {
      "min_peaks": 500,
      "min_counts": 1000,
      "min_cells_per_peak": 10
    },
    "multiome": { "min_pair_overlap": 0.5 }
  },
  "handoff": {
    "consensus_peak_matrix": false,
    "include_tokens": false,
    "include_ccre_mapping": false
  }
}
```

---

## References

- Stuart et al. *Single-cell chromatin state analysis with Signac*. Nature Methods, 2021.
- Granja et al. *ArchR*. Nature Genetics, 2021.
- ENCODE Project Consortium. *Expanded encyclopaedias of DNA elements*. Nature, 2020.
- Heumos et al. *Best practices for single-cell analysis across modalities*. Nat Rev Genetics, 2023.
- SnapATAC2 tutorials — https://scverse.org/SnapATAC2/tutorials/index.html
- omics-os / lobster-ai — https://docs.omics-os.com/docs/
- scIsoAgent — https://github.com/zczali4403/scIsoAgent
