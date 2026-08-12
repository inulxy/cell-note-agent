# StepFun + Pi 接入说明

> 安全约定：API key 只放在本机环境变量或本机 `.env`，不要写入 Git。

## 1. 配置阶跃星辰 API

阶跃星辰 Chat Completion 是 OpenAI-compatible 接口。官方文档给出的普通 Chat Completion 地址是 `https://api.stepfun.com/v1/chat/completions`；Step Plan 编码场景可用 `https://api.stepfun.com/step_plan/v1/chat/completions`。

```bash
cp .env.example .env
# 编辑 .env，填入 STEP_API_KEY
export STEP_API_KEY="你的本机 key"
export STEP_API_BASE_URL="https://api.stepfun.com/v1"
export STEP_API_MODEL="step-3.5-flash"
```

快速验证：

```bash
python -m cell_note_agent.step_api chat "用一句话介绍 CellNoteAgent"
```

让 StepFun 生成 Pi skill 计划：

```bash
python -m cell_note_agent.step_api plan   "处理一个 PBMC fragments.tsv.gz，做成 GRCh38 peak matrix"
```

## 2. Pi 如何调用本地 skills

Pi 仍是本项目的主执行 harness：它负责加载 `skills/*/SKILL.md`，展示命令，在需要人工确认的阶段等待用户确认，再调用 `scripts/*.py --stage=...`。

```bash
./setup_pi.sh
pi
# Pi 对话中：
/skill:sc-epi-agent
```

也可以不用大模型，直接用本地桥接层生成命令计划：

```bash
python -m cell_note_agent.pi_bridge skills

python -m cell_note_agent.pi_bridge plan-peak-matrix   --input_kind fragments   --input data/pbmc/fragments.tsv.gz   --dataset_id pbmc   --results_root results/mvp
```

## 3. 统一表示：所有 ATAC 输入先转 peak matrix

本项目现在固定一个中间表示：

```text
<results_root>/peak_matrices/<dataset_id>/
├── cell_x_peak.npz
├── peaks.bed
└── peak_matrix_metadata.json
```

路由规则：

| 输入 | 处理路线 |
|---|---|
| fragments / snATAC fragments | `normalize-to-peak-matrix` → `scatac-fragment-qc` → peak calling → canonical peak matrix |
| 已有 peak matrix | `normalize-to-peak-matrix` 直接注册 `cell_x_peak.npz` + `peaks.bed` |
| multiome | `multiome-qc` 做配对检查；ATAC 侧进入 canonical peak matrix；RNA 侧保留为 metadata/reference |
| RNA-only | 只能作为 reference/eval，不进入当前 ATAC peak matrix 交付 |

peak matrix 之后只做 data card 和 manifest 打包：

```text
cell_x_peak.npz + peaks.bed
  → package_peak_matrices.py
  → corpus/MANIFEST.json
```

## 4. 推荐调用顺序

```bash
# 1) 生成 peak matrix 路由计划
python scripts/normalize_to_peak_matrix.py --stage=plan   --input_kind peak_matrix   --input data/pbmc/cell_x_peak.npz   --peaks data/pbmc/peaks.bed   --dataset_id pbmc   --results_root results/mvp

# 2) 如果已经有 peak matrix，注册到标准目录
python scripts/normalize_to_peak_matrix.py --stage=materialize   --input_kind peak_matrix   --input data/pbmc/cell_x_peak.npz   --peaks data/pbmc/peaks.bed   --dataset_id pbmc   --results_root results/mvp

# 3) 校验 canonical handoff
python scripts/normalize_to_peak_matrix.py --stage=validate   --input_kind peak_matrix   --dataset_id pbmc   --results_root results/mvp
```

## 5. Agent 责任边界

- StepFun：生成路线、参数解释、Pi skill 调用计划。
- Pi：加载本地 skills、执行命令、处理人工确认。
- `scripts/`：确定性事实源；不要把不可复现的生信逻辑藏在大模型回复里。

## References

- [StepFun Chat Completion API](https://platform.stepfun.com/docs/zh/api-reference/chat/chat-completion-create)
- [Step Plan quick start](https://platform.stepfun.com/docs/zh/step-plan/quick-start)
- [Step Plan reasoning/API integration](https://platform.stepfun.com/docs/zh/step-plan/integrations/reasoning-api)
