# CellNoteAgent — 可执行框架

> 方向：用开源 **[Pi coding agent](https://github.com/badlogic/pi-mono)** 做编排；把 **scATAC / Multiome 数据发现、QC、peak matrix 生成** 做成 **Skills**，由 agent 按需加载并调用 `scripts/` 里的脚本。
>
> 项目重点：**爬虫 + 数据预处理 + per-dataset peak matrix 交付**。最终输出为每个 dataset 独立的 **GRCh38 cell × peak matrix**。

---

## 1. 如何运行

```bash
# 1) 安装 Pi
npm install -g @earendil-works/pi-coding-agent

# 2) 进入本仓库，把 skills/scripts 挂到 Pi 发现路径
cd ~/Desktop/cell-note-agent
./setup_pi.sh

# 3) 启动 Pi（在仓库根目录）
pi

# 4) 在对话里启动总控/强制加载某个 skill
#    /skill:sc-epi-agent
#    /skill:curation-pipeline
#    /skill:normalize-to-peak-matrix
#    /skill:scatac-fragment-qc
#    /skill:scatac-peak-matrix
#    /skill:multiome-qc
```

Pi 启动时只把各 skill 的 `name` + `description` 注入系统提示；匹配任务后再 `read` 完整 `SKILL.md`，按其中的 `Script Interface` 调 `scripts/*.py --stage=...`。

### 1.1 Pi coding agent 接阶跃星辰 API

阶跃星辰 Chat Completion 是 OpenAI-compatible 接口。真实 key 只放在本机环境变量或 `.env`，不要提交到 GitHub：

```bash
cp .env.example .env
export STEP_API_KEY="<STEPFUN_API_KEY>"
export STEP_API_BASE_URL="https://api.stepfun.com/v1"
export STEP_API_MODEL="step-3.5-flash"

python -m cell_note_agent.step_api chat "用一句话介绍 CellNoteAgent"
python -m cell_note_agent.step_api plan "处理 PBMC fragments 到 GRCh38 peak matrix"
```

Pi 仍负责本地 skill 加载、命令展示和人工确认；StepFun adapter 只用于生成路线、参数解释和 Pi skill 调用计划。详细说明见 `docs/STEP_PI_INTEGRATION.md`。

---

## 2. 框架分层（可执行视图）

```text
用户自然语言
    │
    ▼
┌──────────────────────────────────────────┐
│  Pi coding agent（开源 harness）          │
│  · 发现 .pi/skills/**/SKILL.md           │
│  · /skill:name 强制加载                   │
│  · shell / read / write 工具              │
└──────────────────┬───────────────────────┘
                   │ 加载契约
                   ▼
┌──────────────────────────────────────────┐
│  Skills（Markdown 契约，按需加载）         │
│  router → entry pipeline → leaf tool     │
└──────────────────┬───────────────────────┘
                   │ conda run + --stage
                   ▼
┌──────────────────────────────────────────┐
│  scripts/（确定性 Python，执行事实源）     │
│  crawler / SnapATAC2 / matrix QC / muon  │
└──────────────────────────────────────────┘
```

| 层 | 技术 | 职责 |
|---|---|---|
| Harness | Pi coding agent | 对话、工具调用、skill 发现与加载 |
| Planning | StepFun adapter / Pi bridge | 生成 skill 调用计划，不保存密钥 |
| Skills | `skills/*/SKILL.md` | 何时用、阶段顺序、参数/QC、失败/人工审核、精确命令 |
| Scripts | `scripts/*.py --stage=` | 真正跑 crawler / SnapATAC2 / matrix QC / muon；可单元测试 |
| Env | conda：`curator` / `snapatac2` / `muon` | 按工具绑定环境（写在 skill 里） |

---

## 3. Skills

### 3.1 Overview

| # | Skill | 类型 | 模态/阶段 | 调用的脚本 | conda |
|---|---|---|---|---|---|
| 1 | `sc-epi-agent` | router | 全流程入口 | —（只路由） | — |
| 2 | `external-skill-router` | router | 可信外部 SOP 选择 | `python -m cell_note_agent.external_skills` | `curator` |
| 3 | `curation-pipeline` | entry | 发现→纳排→资源→下载计划/校验 | `sc_epi_curator.cli` + leaf | `curator` |
| 4 | `processing-pipeline` | entry | 三条模态分支路由 | leaf QC skills | — |
| 5 | `handoff-pipeline` | entry | peak matrix 打包 | `package_peak_matrices.py` | `curator` |
| 6 | `normalize-to-peak-matrix` | leaf | 输入标准化到 canonical peak matrix | `normalize_to_peak_matrix.py` | `curator` |
| 7 | `resource-setup` | leaf | GRCh38 参考资源 | `prepare_references.py` | `curator` |
| 8 | `download-validate` | leaf | 受控下载计划 + 校验 | `download_validate.py` | `curator` |
| 9 | `scatac-fragment-qc` | leaf | fragment scATAC / snATAC | `scatac_fragment_qc.py` | `snapatac2` |
| 10 | `scatac-peak-matrix` | leaf | 已有 peak matrix | `scatac_peak_matrix.py` | `snapatac2` / `curator` |
| 11 | `multiome-qc` | leaf | paired RNA+ATAC，ATAC 为交付主线 | `multiome_qc.py` | `muon` |

### 3.2 分析类 Skills

```text
/skill:normalize-to-peak-matrix # fragments / peak matrix / multiome → canonical peak matrix plan
/skill:scatac-fragment-qc      # SnapATAC2：fragments → TSSe → QC → spectral/Leiden → peaks → peak matrix
/skill:scatac-peak-matrix      # 已有 cell×peak matrix → matrix QC → GRCh38 标准化 → peak matrix package
/skill:multiome-qc             # muon：barcode 对齐 → RNA/ATAC QC → paired-pass → ATAC peak matrix
```

| Skill | Stages（顺序） |
|---|---|
| `normalize-to-peak-matrix` | `plan` → `materialize` → `validate` |
| `scatac-fragment-qc` | `import` → `pre-filter` → `filter` → `embed` → `cluster` → `doublet` → `call-peaks` → `make-peak-matrix` → `finalize` |
| `scatac-peak-matrix` | `load` → `profile` → `filter` → `standardize` → [`embed-cluster`] → `finalize` |
| `multiome-qc` | `pair-check` → `qc-rna` → `qc-atac` → `intersect` → `finalize` |

### 3.3 策展类 Skills

```text
/skill:curation-pipeline    # crawler 自动搜索 → 元数据 → 文件清单 → 纳排路由
/skill:resource-setup       # GRCh38 chrom sizes / blacklist / TSS / liftover chain
/skill:download-validate    # plan/verify；fetch 需显式 --enable_fetch
/skill:handoff-pipeline     # data card + per-dataset peak matrix MANIFEST
```

### 3.4 路由关系（Pi 如何选择 skill）

```text
用户："分析 10x PBMC scATAC / multiome"
        │
        ▼
  /skill:sc-epi-agent          ← router：判断是 curation / processing / handoff
        │
        ├─► curation-pipeline
        │       └─► crawler + file manifest + routing decision
        │
        ├─► processing-pipeline
        │       ├─► normalize-to-peak-matrix
        │       ├─► scatac-fragment-qc
        │       ├─► scatac-peak-matrix
        │       └─► multiome-qc
        │
        └─► handoff-pipeline
                └─► package_peak_matrices.py
```

用户也可跳过 router，直接 `/skill:scatac-fragment-qc` 做单步分析。

---

## 4. 仓库结构

```text
cell-note-agent/
├── README.md
├── prompt_example.txt
├── setup_pi.sh
├── cell-note
├── configs/
│   ├── mvp.json
│   └── external_skills.json
├── cell_note_agent/
│   ├── step_api.py
│   ├── pi_bridge.py
│   └── external_skills.py
├── sc_epi_curator/
├── skills/
│   ├── sc-epi-agent/SKILL.md
│   ├── external-skill-router/SKILL.md
│   ├── curation-pipeline/SKILL.md
│   ├── processing-pipeline/SKILL.md
│   ├── handoff-pipeline/SKILL.md
│   ├── normalize-to-peak-matrix/SKILL.md
│   ├── resource-setup/SKILL.md
│   ├── download-validate/SKILL.md
│   ├── scatac-fragment-qc/SKILL.md
│   ├── scatac-peak-matrix/SKILL.md
│   └── multiome-qc/SKILL.md
├── scripts/
│   ├── normalize_to_peak_matrix.py
│   ├── download_validate.py
│   ├── prepare_references.py
│   ├── scatac_fragment_qc.py
│   ├── scatac_peak_matrix.py
│   ├── multiome_qc.py
│   ├── package_peak_matrices.py
│   └── demo_pbmc.py
└── docs/
    ├── EXECUTABLE_FRAMEWORK.md
    ├── PIPELINE_DESIGN.md
    ├── STEP_PI_INTEGRATION.md
    └── EXTERNAL_SKILLS.md
```

---

## 5. 环境建议

```bash
conda create -n curator   python=3.11
conda create -n snapatac2 python=3.11
conda create -n muon      python=3.11
```

| env | 用途 |
|---|---|
| `curator` | crawler、manifest、reference、download plan/verify、StepFun adapter、Pi bridge、package |
| `snapatac2` | fragment scATAC、MACS3、peak matrix 生成 |
| `muon` | multiome barcode/QC orchestration |

---

## 6. Demo / smoke test

```bash
# crawler offline demo
./cell-note demo --out runs/demo-pbmc-multiome
./cell-note status --run runs/demo-pbmc-multiome

# crawler accession smoke test，不下载大文件
./cell-note --config configs/mvp.json crawl \
  --query "PBMC 10x multiome" \
  --source accession --accession GSE194122 \
  --limit 1 --out runs/crawl-smoke \
  --run-id crawl-smoke --no-resolve-ena-runs

# peak matrix 形状 demo
python scripts/demo_pbmc.py --stage all --results_root demo_pbmc --dataset_id toy_pbmc
python scripts/package_peak_matrices.py --stage cards --results_root demo_pbmc
python scripts/package_peak_matrices.py --stage validate --results_root demo_pbmc
python scripts/package_peak_matrices.py --stage package --results_root demo_pbmc
```
