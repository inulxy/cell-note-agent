# CellNoteAgent Skills

本目录是 Pi coding agent 发现和加载的 skill 契约层。每个 `SKILL.md` 只描述何时使用、阶段顺序、参数、人工审核点和精确命令；真正执行逻辑在 `scripts/` 或 `sc_epi_curator/`。

## Active Contract

```text
sc-epi-agent
├── curation-pipeline
│   ├── resource-setup
│   └── download-validate
├── processing-pipeline
│   ├── normalize-to-peak-matrix
│   ├── scatac-fragment-qc
│   ├── scatac-peak-matrix
│   └── multiome-qc
├── handoff-pipeline
└── external-skill-router
```

当前主线只保留三类输入：

```text
scATAC fragments
scATAC peak matrix
multiome RNA+ATAC
```

已移除主线：`map-to-ccre`、`tokenize-cell-sentence`、`fm-handoff`、单独 `scrna-qc`。

## Output Contract

每个进入 handoff 的 dataset 最终输出独立的 GRCh38 peak matrix：

```text
processed/<dataset_id>/peak_matrix.h5ad 或 matrix.mtx.gz
processed/<dataset_id>/peaks.hg38.bed
processed/<dataset_id>/barcodes.tsv.gz
processed/<dataset_id>/qc_summary.json
processed/<dataset_id>/data_card.json
corpus/MANIFEST.json
```

不生成统一 cCRE vocabulary，不生成 tokenized cell sentences，不做 train/val/test split。

## Runtime Envs

| env | 用途 |
|---|---|
| `curator` | 数据发现 / IO / StepFun API adapter / Pi bridge / peak-matrix registration / package |
| `snapatac2` | fragment scATAC QC、MACS3、peak matrix 生成 |
| `muon` | multiome barcode 对齐和 paired-pass orchestration |
