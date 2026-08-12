"""Interactive CellNoteAgent shell.

This is intentionally small: natural language is used for routing and planning,
while execution stays in deterministic local scripts.
"""
from __future__ import annotations

import argparse
import shlex
import shutil
import csv
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cell_note_agent.search_expansion import build_search_plan


PBMC500_MATRIX_URL = (
    "https://cf.10xgenomics.com/samples/cell-atac/2.0.0/"
    "atac_pbmc_500_nextgem/atac_pbmc_500_nextgem_filtered_peak_bc_matrix.h5"
)
PBMC500_PEAKS_URL = (
    "https://cf.10xgenomics.com/samples/cell-atac/2.0.0/"
    "atac_pbmc_500_nextgem/atac_pbmc_500_nextgem_peaks.bed"
)


@dataclass(frozen=True)
class AgentConfig:
    repo_root: Path
    run_root: Path
    processing_python: str
    assume_yes: bool = False
    auto_all: bool = False
    use_tmux: bool = True


@dataclass
class AgentState:
    last_crawl_run: Path | None = None
    last_promote_run: Path | None = None
    last_manifest: Path | None = None
    last_candidate_catalog: Path | None = None
    last_search_profile: dict | None = None
    last_downloaded_manifest: Path | None = None


def default_processing_python() -> str:
    candidates = [
        os.environ.get("CELLNOTE_PROCESSING_PYTHON", ""),
        "/home/lixinyu/miniforge3/envs/cellnote-agent/bin/python",
        "/opt/anaconda3/envs/snapatac2/bin/python",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return sys.executable


def pipeline_python(default_python: str, environment: str) -> str:
    env_key = f"CELLNOTE_{environment.upper().replace('-', '_')}_PYTHON"
    candidates = [
        os.environ.get(env_key, ""),
        f"/home/lixinyu/miniforge3/envs/{environment}/bin/python",
        f"/ssd/deecamp/cellnotes/conda-envs/{environment}/bin/python",
        f"/opt/anaconda3/envs/{environment}/bin/python",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return default_python


def content_length(url: str) -> int:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "cellnote-agent/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        value = response.headers.get("Content-Length", "0")
    return int(value) if value.isdigit() else 0


def run_command(argv: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    print("\n$ " + " ".join(argv))
    return subprocess.run(argv, cwd=str(cwd), check=check)


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _sanitize_tmux_session(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")
    return (cleaned or "cellnote-job")[:48]


def _unique_tmux_session(base: str) -> str:
    session = _sanitize_tmux_session(base)
    if not tmux_available():
        return session
    probe = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        return session
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    return _sanitize_tmux_session(f"{session}-{stamp}")


def should_use_tmux(config: "AgentConfig") -> bool:
    if not getattr(config, "use_tmux", True):
        return False
    if os.environ.get("CELLNOTE_NO_TMUX", "").strip() in {"1", "true", "TRUE", "yes", "YES"}:
        return False
    return tmux_available()


def write_long_job_script(
    commands: list[list[str]],
    *,
    cwd: Path,
    script_path: Path,
    log_path: Path,
    job_name: str,
) -> Path:
    """Write a fail-fast bash runner that tees all stage output to log_path."""
    script_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {shlex.quote(str(cwd))}",
        f"LOG={shlex.quote(str(log_path))}",
        'mkdir -p "$(dirname "$LOG")"',
        'TMPDIR="$(dirname "$LOG")/tmp"',
        'mkdir -p "$TMPDIR"',
        'export TMPDIR TMP="$TMPDIR" TEMP="$TMPDIR"',
        'exec > >(tee -a "$LOG") 2>&1',
        f'echo "[start] job={shlex.quote(job_name)} $(date -Is) cwd=$(pwd)"',
        f'echo "[start] log=$LOG"',
    ]
    for index, command in enumerate(commands, 1):
        rendered = " ".join(shlex.quote(part) for part in command)
        lines.append(f'echo "===== [{index}/{len(commands)}] {rendered} ====="')
        lines.append(f'echo "[stage-start] $(date -Is)"')
        lines.append(rendered)
        lines.append(f'echo "[stage-done] $(date -Is)"')
    lines.extend(
        [
            'echo "[done] $(date -Is)"',
            'echo "[hint] detach: Ctrl-b d | reattach: tmux attach -t <session>"',
        ]
    )
    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script_path.chmod(0o750)
    return script_path


def run_long_commands(
    commands: list[list[str]],
    *,
    config: "AgentConfig",
    job_name: str,
    check: bool = True,
) -> dict:
    """Run a long command sequence in a detached tmux session when enabled.

    Interactive confirms must happen before calling this. Short/interactive
    commands should keep using run_command() in the agent TTY.
    """
    if not commands:
        return {"mode": "empty"}
    if not should_use_tmux(config):
        for command in commands:
            run_command(command, cwd=config.repo_root, check=check)
        return {"mode": "foreground", "commands": len(commands)}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    session = _unique_tmux_session(f"cellnote-{job_name}-{stamp}")
    job_dir = config.run_root / "tmux_jobs" / session
    script_path = job_dir / "run.sh"
    log_path = job_dir / "job.log"
    write_long_job_script(
        commands,
        cwd=config.repo_root,
        script_path=script_path,
        log_path=log_path,
        job_name=job_name,
    )
    # Detached session; do not attach (keeps agent interactive shell usable).
    create = [
        "tmux",
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        str(config.repo_root),
        "bash",
        str(script_path),
    ]
    print("\n$ " + " ".join(create))
    subprocess.run(create, check=True)
    print("\n长任务已在 tmux 中启动（SSH 断开也不会中断该任务）：")
    print(f"- session: {session}")
    print(f"- attach : tmux attach -t {session}")
    print(f"- log    : {log_path}")
    print(f"- script : {script_path}")
    print("- list   : tmux ls")
    print("提示：在 tmux 内用 Ctrl-b d 退出会话但不停止任务。")
    return {
        "mode": "tmux",
        "session": session,
        "log": str(log_path),
        "script": str(script_path),
        "commands": len(commands),
    }


def choose_option(title: str, options: list[str], *, default_index: int = 0, assume_choice: int | None = None) -> int:
    """Numbered prompt with arrow-key support in a TTY and numeric fallback otherwise."""
    if assume_choice is not None:
        choice = max(0, min(assume_choice, len(options) - 1))
        print(title)
        for index, option in enumerate(options, 1):
            marker = "*" if index - 1 == choice else " "
            print(f"{marker} {index}. {option}")
        print(f"已选择：{options[choice]}")
        return choice

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(title)
        for index, option in enumerate(options, 1):
            print(f"  {index}. {option}")
        answer = input("请选择编号，直接回车使用默认选项：").strip()
        if not answer:
            return default_index
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        lowered = answer.lower()
        for index, option in enumerate(options):
            if lowered == option.lower():
                return index
        return default_index

    import termios
    import tty

    selected = max(0, min(default_index, len(options) - 1))
    print(title)
    print("使用 ↑/↓ 移动，Enter 选择；也可按数字。")
    rendered = False

    def render() -> None:
        nonlocal rendered
        if rendered:
            sys.stdout.write(f"\033[{len(options)}F")
        for index, option in enumerate(options):
            prefix = "❯" if index == selected else " "
            style_start = "\033[7m" if index == selected else ""
            style_end = "\033[0m" if index == selected else ""
            sys.stdout.write("\033[2K")
            sys.stdout.write(f"{style_start}{prefix} {index + 1}. {option}{style_end}\n")
        sys.stdout.flush()
        rendered = True

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        render()
        while True:
            char = sys.stdin.read(1)
            if char in {"\r", "\n"}:
                print(f"已选择：{options[selected]}")
                return selected
            if char.isdigit() and 1 <= int(char) <= len(options):
                selected = int(char) - 1
                render()
                print(f"已选择：{options[selected]}")
                return selected
            if char == "\x1b":
                sequence = sys.stdin.read(2)
                if sequence == "[A":
                    selected = (selected - 1) % len(options)
                    render()
                elif sequence == "[B":
                    selected = (selected + 1) % len(options)
                    render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return choose_option(prompt, ["确认", "取消"], default_index=0, assume_choice=0) == 0
    return choose_option(prompt, ["确认", "取消"], default_index=1) == 0


def manifest_review_answer() -> str:
    choice = choose_option("是否修改清单或确认下载？", ["下载", "取消", "其他"], default_index=1)
    if choice == 0:
        return "下载"
    if choice == 1:
        return "取消"
    return input("请输入你的要求：").strip()


def csv_data_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def latest_crawl_run(run_root: Path) -> Path | None:
    roots = [run_root / "crawl", *(run_root / "crawls").glob("agent-crawl-*")]
    valid = [path for path in roots if (path / "crawl_manifest.json").exists()]
    if not valid:
        return None
    return max(valid, key=lambda path: (path / "crawl_manifest.json").stat().st_mtime)


def latest_downloaded_manifest(run_root: Path) -> Path | None:
    candidates = sorted(run_root.glob("raw/**/downloaded_file_manifest.csv"), key=lambda path: path.stat().st_mtime)
    direct = run_root / "raw" / "downloaded_file_manifest.csv"
    if direct.exists():
        candidates.append(direct)
    valid = [path for path in candidates if csv_data_row_count(path) > 0]
    return valid[-1] if valid else None

def optimize_search_query(query: str) -> str:
    text = query.strip()
    lower = text.lower()
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
    organism = "Homo sapiens"
    modality_terms = []
    if "multiome" in lower or "multi-ome" in lower or "多组" in text:
        modality_terms.extend(["10x multiome", "RNA ATAC", "scATAC-seq"])
    elif "scatac" in lower or "atac" in lower or "染色质" in text:
        modality_terms.extend(["10x Genomics", "single cell ATAC-seq", "scATAC-seq"])
    else:
        modality_terms.extend(["10x Genomics", "single cell ATAC-seq", "multiome"])
    if "pbmc" in lower:
        modality_terms.append("PBMC")
    if any(term in lower for term in ["sra", "geo", "prjna", "srp", "gse"]):
        modality_terms.append(text)
    elif not has_cjk and len(text.split()) >= 3:
        modality_terms.append(text)
    return " ".join(dict.fromkeys([organism, *modality_terms]))



def choose_option_with_other(title: str, options: list[str], *, default_index: int = 0, assume_choice: int | None = None) -> str:
    choice = choose_option(title, [*options, "其他"], default_index=default_index, assume_choice=assume_choice)
    if choice == len(options):
        return input("请输入你的补充要求：").strip()
    return options[choice]


def parse_candidate_limit_preference(value: str) -> int | None:
    lowered = value.lower()
    if any(term in lowered for term in ["所有", "全部", "all", "不设", "不限"]):
        return None
    match = re.search(r"(\d+)", value)
    return int(match.group(1)) if match else None


def parse_size_limit_gb_preference(value: str) -> float | None:
    lowered = value.lower()
    if any(term in lowered for term in ["不限制", "不限", "all", "none"]):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb|g|mb|m)?", lowered)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "gb"
    if unit == "tb":
        return number * 1000
    if unit in {"mb", "m"}:
        return number / 1000
    return number


def normalize_genome_preference(value: str) -> str:
    lowered = value.lower()
    if "hg19" in lowered or "grch37" in lowered:
        return "hg19"
    if "不确定" in value or "unknown" in lowered or "unsure" in lowered:
        return "unknown"
    return "GRCh38"


def broad_dataset_request(query: str) -> bool:
    lower = query.lower()
    search_terms = ["搜集", "搜索", "寻找", "找", "discover", "search", "collect", "dataset", "数据集"]
    if not any(term in lower for term in search_terms):
        return False
    # These are truly concrete inputs. Biological qualifiers such as cancer, PBMC,
    # FASTQ, fragments, or processed matrix should still get the Biomni-style
    # clarification form because they are search preferences, not dataset choices.
    concrete_patterns = [
        r"https?://",
        r"\b(?:GSE|GSM|SRP|SRR|PRJNA|PRJEB|ERP|ERR|DRP|DRR)\d+\b",
        r"\bfile_manifest(?:_v\d+)?\.csv\b",
        r"\.(?:csv|tsv|json|jsonl|h5|h5ad|mtx|bed|gz)\b",
    ]
    if any(re.search(pattern, query, flags=re.IGNORECASE) for pattern in concrete_patterns):
        return False
    if any(term in lower for term in ["下载刚才", "下载清单", "下载 manifest", "开始分析", "运行分析", "继续分析"]):
        return False
    return True



def infer_search_slots(query: str) -> dict:
    """Infer search preference slots from natural language (interaction-gates style)."""
    text = query.strip()
    lower = text.lower()
    inferred: dict[str, object] = {"source_query": text}

    # modality / data type
    if any(term in lower or term in text for term in ["multiome", "多组学", "rna+atac", "rna atac"]):
        inferred["data_type"] = "10x Multiome"
    elif any(term in lower or term in text for term in ["scatac", "snatac", "atac", "染色质"]):
        if "multiome" not in lower:
            inferred["data_type"] = "纯 scATAC-seq"

    # tissue / disease
    if any(term in lower for term in ["pbmc", "peripheral blood"]) or "外周血" in text:
        inferred["tissue_or_disease"] = "正常组织图谱"
        inferred["tissue_hint"] = "PBMC"
    elif any(term in text for term in ["泛癌", "肿瘤", "癌症"]) or any(term in lower for term in ["cancer", "tumor", "oncolog"]):
        inferred["tissue_or_disease"] = "泛癌/肿瘤"
    elif any(term in text for term in ["正常组织", "图谱", "atlas"]) or "healthy" in lower:
        inferred["tissue_or_disease"] = "正常组织图谱"
    elif any(term in text for term in ["白血病", "淋巴瘤", "阿尔茨海默", "帕金森", "糖尿病", "哮喘", "肺炎", "肝硬化"]) or any(
        term in lower for term in ["leukemia", "lymphoma", "alzheimer", "parkinson", "diabetes", "asthma", "fibrosis", "aml", "cll", "all"]
    ):
        inferred["tissue_or_disease"] = "特定疾病"
        inferred["tissue_hint"] = "specific_disease_mentioned"
    elif any(term in text for term in ["脑", "心脏", "肝", "肺", "肾"]) or any(
        term in lower for term in ["brain", "heart", "liver", "lung", "kidney"]
    ):
        inferred["tissue_or_disease"] = "特定疾病"
        inferred["tissue_hint"] = "specific_tissue_mentioned"

    # acquisition / file readiness
    wants_processed = any(
        term in text or term in lower
        for term in [
            "处理后", "矩阵", "fragments", "peak matrix", "peak-matrix", "h5ad",
            "直接做分析", "可分析", "analysis-ready", "processed matrix",
        ]
    )
    rejects_raw = any(term in text or term in lower for term in ["不要fastq", "不要 fastq", "不要raw", "不要 raw", "别给我fastq", "no fastq", "not raw"])
    wants_raw = any(term in lower or term in text for term in ["fastq", "原始测序", "原始 sra", "只要sra"])
    if wants_processed or rejects_raw:
        inferred["acquisition"] = "处理后的矩阵或 fragments"
        inferred["prefer_analysis_ready"] = True
    elif wants_raw and not wants_processed:
        inferred["acquisition"] = "原始 FASTQ/SRA"
        inferred["prefer_analysis_ready"] = False

    # genome
    if any(term in lower for term in ["hg19", "grch37"]):
        inferred["target_genome_build"] = "hg19"
        inferred["genome_label"] = "hg19/GRCh37"
    elif any(term in lower for term in ["grch38", "hg38"]) or "基因组" in text:
        inferred["target_genome_build"] = "GRCh38"
        inferred["genome_label"] = "GRCh38/hg38"

    # candidate budget (avoid matching 10x / years / accession digits)
    m = re.search(
        r"(?:top\s*|最多|先看|只要|需要)?\s*(\d{1,2})\s*(?:个|条)\s*(?:候选|数据集|结果)?",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        m = re.search(r"top\s*(\d{1,2})\b", text, flags=re.IGNORECASE)
    if m and int(m.group(1)) <= 100:
        n = int(m.group(1))
        inferred["candidate_limit"] = str(n)
        inferred["candidate_limit_value"] = n
    elif any(term in text for term in ["几个", "少量", "先看看"]):
        inferred["candidate_limit"] = "5"
        inferred["candidate_limit_value"] = 5

    # size
    gb = re.search(r"(\d+(?:\.\d+)?)\s*(?:gb|g)", lower)
    if gb:
        inferred["size_limit"] = f"{gb.group(1)}GB以内"
        inferred["size_limit_gb"] = float(gb.group(1))
    elif any(term in text for term in ["小文件", "不要太大", "控体积"]):
        inferred["size_limit"] = "5GB以内"
        inferred["size_limit_gb"] = 5.0

    return inferred


def classify_remote_file_role(item: dict) -> str:
    blob = " ".join(
        str(item.get(key) or "")
        for key in ["file_role", "file_format", "uri", "file_id", "source"]
    ).lower()
    if any(token in blob for token in ["fragments.tsv", "fragment", "frag.tsv"]):
        return "fragments"
    if any(
        token in blob
        for token in [
            "cell_by_peak",
            "peak_matrix",
            "peak-bc",
            "peak_bc",
            "filtered_peak",
            "filtered_feature_bc_matrix",
            "raw_feature_bc_matrix",
            ".h5ad",
            ".h5mu",
            "matrix.h5",
        ]
    ):
        return "peak_matrix"
    if blob.endswith(".bed") or "peaks.bed" in blob or "/peaks" in blob:
        return "peaks"
    if any(token in blob for token in ["fastq", ".fq", "sra_run", ".sra", "sra"]):
        return "raw"
    return "unknown"


def summarize_study_files(study_files: list[dict]) -> dict:
    roles = [classify_remote_file_role(item) for item in study_files]
    rank = {"peak_matrix": 0, "fragments": 1, "peaks": 2, "unknown": 3, "raw": 4}
    best = min(roles, key=lambda role: rank.get(role, 9)) if roles else "unknown"
    if best in {"peak_matrix", "fragments"}:
        fit = "high"
    elif best in {"peaks", "unknown"}:
        fit = "medium"
    else:
        fit = "low"
    why_map = {
        "peak_matrix": "has peak/matrix-like files",
        "fragments": "has fragments-like files",
        "peaks": "has peaks bed",
        "raw": "raw FASTQ/SRA dominant",
        "unknown": "file role unclear",
    }
    ready_files = [item for item in study_files if classify_remote_file_role(item) in {"peak_matrix", "fragments", "peaks"}]
    preferred_files = ready_files or study_files
    known_sizes = [int(item.get("size_bytes") or 0) for item in preferred_files if int(item.get("size_bytes") or 0) > 0]
    return {
        "best_file_role": best,
        "pipeline_fit": fit,
        "fit_reason": why_map.get(best, "needs_review"),
        "role_counts": {role: roles.count(role) for role in sorted(set(roles))},
        "preferred_file_count": len(preferred_files),
        "preferred_size_bytes": sum(int(item.get("size_bytes") or 0) for item in preferred_files),
        "smallest_file_size_bytes": min(known_sizes) if known_sizes else 0,
        "unknown_size_file_count": sum(1 for item in preferred_files if int(item.get("size_bytes") or 0) <= 0),
    }


def collect_search_preferences(config: AgentConfig, query: str) -> dict:
    """Smart search prefs: infer slots from language; only ask missing critical gaps."""
    if config.assume_yes or not broad_dataset_request(query):
        inferred = infer_search_slots(query)
        if not inferred:
            return {}
        prefs = {
            "data_type": inferred.get("data_type", "纯 scATAC-seq"),
            "tissue_or_disease": inferred.get("tissue_or_disease", "不限/广泛搜集"),
            "acquisition": inferred.get("acquisition", "处理后的矩阵或 fragments"),
            "candidate_limit": str(inferred.get("candidate_limit") or "10"),
            "candidate_limit_value": inferred.get("candidate_limit_value", 10),
            "size_limit": inferred.get("size_limit", "20GB以内"),
            "size_limit_gb": inferred.get("size_limit_gb", 20.0),
            "target_genome_build": inferred.get("target_genome_build", "GRCh38"),
            "prefer_analysis_ready": bool(inferred.get("prefer_analysis_ready", True)),
            "inferred_slots": inferred,
        }
        return prefs

    inferred = infer_search_slots(query)
    print("\n[interaction-gates] 智能搜索：先从你的描述推断条件，只补问缺失项。")
    if inferred:
        print("已推断：")
        for key in ["data_type", "tissue_or_disease", "tissue_hint", "acquisition", "target_genome_build", "candidate_limit", "size_limit"]:
            if key in inferred:
                print(f"- {key}: {inferred[key]}")

    # Fill with inference defaults, then ask only gaps.
    data_type = inferred.get("data_type")
    if not data_type:
        data_type = choose_option_with_other(
            "数据类型：你需要哪种 scATAC 数据？",
            ["10x Multiome", "纯 scATAC-seq", "两者都要"],
            default_index=1,
        )

    tissue = inferred.get("tissue_or_disease")
    if not tissue:
        tissue = choose_option_with_other(
            "组织/疾病方向？",
            ["泛癌/肿瘤", "正常组织图谱", "特定疾病", "不限/广泛搜集"],
            default_index=1 if any(x in query.lower() for x in ["pbmc", "blood"]) else 3,
        )

    acquisition = inferred.get("acquisition")
    if not acquisition:
        acquisition = choose_option_with_other(
            "更希望拿到什么文件？",
            ["处理后的矩阵或 fragments", "原始 FASTQ/SRA", "公开 accession 即可", "都行/越方便越好"],
            default_index=0,
        )

    candidate_limit = inferred.get("candidate_limit")
    candidate_limit_value = inferred.get("candidate_limit_value")
    if candidate_limit is None:
        choice = choose_option(
            "候选数量（SOFT：用于控制浏览量）",
            ["5", "10（推荐）", "30", "不设上限"],
            default_index=1,
        )
        candidate_limit = ["5", "10", "30", "所有/不设上限"][choice]
        candidate_limit_value = parse_candidate_limit_preference(candidate_limit)

    size_limit = inferred.get("size_limit", "20GB以内")
    size_limit_gb = inferred.get("size_limit_gb", 20.0)
    # Only ask size if user didn't constrain and acquisition prefers processed.
    if "size_limit" not in inferred and "处理后" not in str(acquisition):
        size_limit = choose_option_with_other(
            "单个候选下载量希望控制在？",
            ["5GB以内", "20GB以内", "100GB以内", "不限制"],
            default_index=1,
        )
        size_limit_gb = parse_size_limit_gb_preference(size_limit)

    genome_label = inferred.get("genome_label")
    target_genome = inferred.get("target_genome_build")
    if not target_genome:
        genome_label = choose_option_with_other(
            "目标基因组版本？",
            ["GRCh38/hg38", "hg19/GRCh37", "不确定，先检测/询问"],
            default_index=0,
        )
        target_genome = normalize_genome_preference(str(genome_label))

    prefer_ready = bool(inferred.get("prefer_analysis_ready"))
    if "prefer_analysis_ready" not in inferred:
        prefer_ready = "处理后" in str(acquisition) or "fragments" in str(acquisition).lower()

    prefs = {
        "data_type": data_type,
        "tissue_or_disease": tissue,
        "acquisition": acquisition,
        "candidate_limit": str(candidate_limit),
        "candidate_limit_value": candidate_limit_value if candidate_limit_value is not None else parse_candidate_limit_preference(str(candidate_limit)),
        "size_limit": size_limit,
        "size_limit_gb": size_limit_gb,
        "target_genome_build": target_genome,
        "prefer_analysis_ready": prefer_ready,
        "inferred_slots": inferred,
    }
    print("\n将按以下条件检索（已合并推断与补充确认）：")
    for key, value in prefs.items():
        if key == "inferred_slots":
            continue
        print(f"- {key}: {value}")
    return prefs


def query_from_preferences(query: str, prefs: dict) -> str:
    # Keep public database search broad. File-format preferences such as
    # fragments.tsv.gz or processed matrix are used later for ranking/manifest review,
    # not as NCBI/GEO query terms, because they often cause zero hits.
    joined = " ".join(str(value) for value in prefs.values()).lower()
    terms = ["Homo sapiens"]
    if "multiome" in joined and "纯 scatac" not in joined:
        terms.extend(["multiome", "ATAC-seq"])
    elif "两者" in joined:
        terms.extend(["scATAC-seq", "multiome", "single cell ATAC-seq"])
    else:
        terms.extend(["scATAC-seq", "single cell ATAC-seq"])
    if "泛癌" in joined or "肿瘤" in joined or "cancer" in joined or "tumor" in joined:
        terms.extend(["cancer", "tumor"])
    if "正常组织" in joined:
        terms.extend(["normal tissue atlas"])
    if "特定疾病" in joined:
        # Preserve user-provided disease words when they are already in the original query.
        terms.append(query)
    user_note = str(prefs.get("user_note") or prefs.get("tissue_hint") or "").strip()
    if user_note:
        terms.append(user_note)
    if "fastq" in joined or "sra" in joined:
        terms.append("SRA")
    return " ".join(dict.fromkeys(term for term in terms if term))



def compact_candidate_rows(catalog: Path, *, max_rows: int = 30) -> list[dict]:
    rows = []
    with catalog.open(newline="", encoding="utf-8") as handle:
        for row in list(csv.DictReader(handle))[:max_rows]:
            rows.append(
                {
                    "candidate_id": row.get("candidate_id", ""),
                    "study_accession": row.get("study_accession", ""),
                    "source": row.get("repository_source", ""),
                    "species": row.get("scientific_name", ""),
                    "library_strategy": row.get("library_strategy", ""),
                    "modality": row.get("inferred_modality", ""),
                    "genome_build": row.get("genome_build", ""),
                    "score": row.get("priority_score", ""),
                    "recommended": row.get("recommended", ""),
                    "reason": row.get("priority_reason", ""),
                    "run_count": row.get("run_count", ""),
                    "file_count": row.get("file_count", ""),
                    "total_size_gb": row.get("total_size_gb", ""),
                }
            )
    return rows


def compact_literature_records(crawl_run: Path | None, *, max_rows: int = 10) -> list[dict]:
    if crawl_run is None:
        return []
    records_path = crawl_run / "discovery_records.jsonl"
    rows = []
    for record in read_jsonl(records_path)[:max_rows]:
        rows.append(
            {
                "source": record.get("source", ""),
                "source_id": record.get("source_id", ""),
                "title": record.get("title", ""),
                "summary": str(record.get("summary", ""))[:600],
                "identifiers": record.get("identifiers", {}),
                "publication_date": (record.get("metadata") or {}).get("publication_date", ""),
            }
        )
    return rows


def catalog_stats(catalog: Path) -> dict:
    rows = []
    with catalog.open(newline="", encoding="utf-8") as handle:
        rows.extend(csv.DictReader(handle))
    total_files = sum(int(row.get("file_count") or 0) for row in rows)
    total_size = sum(int(row.get("total_size_bytes") or 0) for row in rows) / 1e9
    modality_counts: dict[str, int] = {}
    species_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    recommended = 0
    for row in rows:
        modality = row.get("inferred_modality") or "unknown"
        modality_counts[modality] = modality_counts.get(modality, 0) + 1
        species = row.get("scientific_name") or "unknown"
        species_counts[species] = species_counts.get(species, 0) + 1
        source = row.get("repository_source") or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
        if str(row.get("recommended", "")).lower() == "yes":
            recommended += 1
    return {
        "candidate_count": len(rows),
        "total_files": total_files,
        "total_size_gb": round(total_size, 3),
        "recommended_count": recommended,
        "modality_counts": modality_counts,
        "species_counts": species_counts,
        "source_counts": source_counts,
        "smallest_candidates": sorted(compact_candidate_rows(catalog, max_rows=len(rows)), key=lambda item: float(item.get("total_size_gb") or 0))[:5],
    }


def step_candidate_landscape_report(catalog: Path, prefs: dict | None, crawl_run: Path | None = None) -> str | None:
    if not os.environ.get("STEP_API_KEY"):
        return None
    try:
        from cell_note_agent.step_api import chat_completion, first_message_content

        stats = catalog_stats(catalog)
        source_scope: dict = {}
        profile_path = crawl_run / "search_profile.json" if crawl_run else None
        if profile_path and profile_path.exists():
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            source_scope = profile.get("external_discovery", {}).get("official_sources", {})
        payload = {
            "user_preferences": prefs or {},
            "core_sources": ["GEO/NCBI GDS", "NCBI SRA", "Europe PMC"],
            "extended_source_execution": source_scope,
            "stats": stats,
            "candidate_rows": compact_candidate_rows(catalog, max_rows=12),
            "literature_titles": [
                {"title": item.get("title", ""), "source_id": item.get("source_id", ""), "date": item.get("publication_date", "")}
                for item in compact_literature_records(crawl_run, max_rows=5)
            ],
            "output_files": {
                "candidate_catalog_csv": str(catalog),
                "crawl_run_dir": str(crawl_run) if crawl_run else "",
                "summary_md": str(catalog.parent / "candidate_landscape_summary.md"),
            },
        }
        system = (
            "你是 CellNoteAgent 的检索结果报告层。请用中文 Markdown 输出 Biomni 风格报告。"
            "必须只基于用户给定 JSON，不得编造数量、来源、癌种、年份或数据集。"
            "如果 metadata 是 unknown 或推荐候选为 0，要明确说明这是本次 crawler 的局限。"
            "标题必须根据用户实际检索主题生成，不要固定写泛癌。必须包含：### 核心发现、"
            "### 候选数据集、### 处理后矩阵可用性、### 输出文件、### 局限性、### 建议下一步。"
            "候选表只使用 candidate_rows。只有 extended_source_execution 中实际执行且无错误的来源才可称为已检索；失败来源必须注明。必须输出非空 Markdown。"
        )
        user = "请根据以下 JSON 写报告：\n" + json.dumps(payload, ensure_ascii=False)
        response = chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=1800,
        )
        report = first_message_content(response).strip()
        if not report:
            retry_system = "用中文 Markdown 总结这个 crawler 结果。只用 JSON 事实，不编造。必须非空。"
            retry_user = json.dumps({"stats": stats, "candidates": payload["candidate_rows"], "prefs": prefs or {}}, ensure_ascii=False)
            response = chat_completion(
                [{"role": "system", "content": retry_system}, {"role": "user", "content": retry_user}],
                temperature=0.0,
                max_tokens=1200,
            )
            report = first_message_content(response).strip()
        if not report:
            return None
        summary_path = catalog.parent / "candidate_landscape_summary.md"
        summary_path.write_text(report + "\n", encoding="utf-8")
        return report
    except Exception as error:
        print(f"[step-candidate-summary] warning: {error}")
        return None

def deterministic_candidate_landscape_report(catalog: Path, prefs: dict | None = None, crawl_run: Path | None = None) -> str:
    stats = catalog_stats(catalog)
    candidates = compact_candidate_rows(catalog, max_rows=12)
    smallest = stats["smallest_candidates"]
    prefs_text = "; ".join(f"{key}={value}" for key, value in (prefs or {}).items()) or "未指定"
    candidate_lines = [
        "| 数据集 | 数据类型/相关性 | 文件数 | 估计大小 | 说明 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for row in candidates[:8]:
        note_parts = []
        if row.get("recommended") != "yes":
            note_parts.append("需人工复核")
        if row.get("species") in {"", "unknown"}:
            note_parts.append("物种 metadata 缺失")
        if row.get("modality") == "unknown_atac_relevance":
            note_parts.append("ATAC 相关性未能自动确认")
        note = "; ".join(note_parts) or row.get("reason") or "候选"
        candidate_lines.append(
            f"| {row.get('study_accession','-')} | {row.get('modality','-')} | {row.get('file_count','-')} | {row.get('total_size_gb','-')} GB | {note} |"
        )
    smallest_lines = [
        f"- [{row.get('candidate_id')}] {row.get('study_accession')}: files={row.get('file_count')}, size={row.get('total_size_gb')} GB, score={row.get('score')}"
        for row in smallest
    ]
    attempted_sources = ["GEO/NCBI GDS", "NCBI SRA", "Europe PMC"]
    failed_sources: list[str] = []
    profile_path = crawl_run / "search_profile.json" if crawl_run else None
    if profile_path and profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        official = profile.get("external_discovery", {}).get("official_sources", {})
        attempted_sources.extend(str(name) for name in (official.get("source_counts") or {}))
        failed_sources.extend(str(name) for name in (official.get("errors") or {}))
    source_text = "、".join(dict.fromkeys(attempted_sources))
    failure_text = "、".join(failed_sources) or "无"
    report = f"""## 检索结果总结

本次检索偏好：{prefs_text}。
CellNote crawler 本轮尝试检索：{source_text}。适配器失败来源：{failure_text}；失败来源不计入覆盖声明。

### 核心发现

- 共找到 **{stats['candidate_count']} 个候选数据集**，涉及 **{stats['total_files']} 个可下载文件**，估计总大小 **{stats['total_size_gb']} GB**。
- 自动推荐候选为 **{stats['recommended_count']} 个**；当前结果中多个候选的物种或 modality metadata 不完整，因此需要人工复核。
- 数据类型分布：{', '.join(f'{k}={v}' for k, v in sorted(stats['modality_counts'].items()))}。
- 物种分布：{', '.join(f'{k}={v}' for k, v in sorted(stats['species_counts'].items()))}。

### 候选数据集

{chr(10).join(candidate_lines)}

### 最小候选

{chr(10).join(smallest_lines) if smallest_lines else '暂无可排序候选。'}

### 处理后矩阵可用性

本次候选主要来自 SRA/ENA 可下载文件清单。若文件角色显示为 FASTQ/SRA/raw reads，则不能直接进入 peak matrix QC，需要先做 raw preprocessing。用户偏好为“处理后的矩阵或 fragments”，但当前 crawler 只能从已解析到的 remote file metadata 判断，尚不能保证这些候选都包含 `fragments.tsv.gz` 或 peak-barcode matrix。

### 输出文件

| 文件 | 内容 |
| --- | --- |
| `{catalog}` | 候选数据集目录 |
| `{crawl_run or ''}` | 本次 crawl 运行目录 |
| `{catalog.parent / 'candidate_landscape_summary.md'}` | 本摘要报告 |

### 局限性

1. **来源覆盖边界**：覆盖范围以本轮成功返回的适配器为准，不等价于全网系统综述。
2. **metadata 缺失**：部分候选的 species、library strategy 或 modality 为 unknown，导致推荐分偏低。
3. **文件形式不确定**：SRA/ENA remote files 多为原始 reads；是否存在处理后矩阵需要进一步检查 GEO supplementary 或论文数据链接。
4. **关键词检索可能遗漏**：scATAC/Multiome 的命名差异较大，建议用 accession、组织或疾病关键词补充检索。

### 建议下一步

- 如果目标是快速演示下载，请在清单审阅时选择“其他”，输入：`只保留最小1个非零文件`。
- 如果目标是直接分析，请优先寻找明确包含 `fragments.tsv.gz`、`filtered_peak_bc_matrix.h5`、`matrix.mtx.gz`、`peaks.bed` 的 GEO supplementary 数据。
- 如果目标是系统综述，可继续用 accession 种子和特定组织/疾病词迭代检索，并审阅 master candidate CSV。"""
    summary_path = catalog.parent / "candidate_landscape_summary.md"
    summary_path.write_text(report + "\n", encoding="utf-8")
    return report

def print_candidate_landscape(catalog: Path, prefs: dict | None = None, crawl_run: Path | None = None) -> None:
    report = step_candidate_landscape_report(catalog, prefs, crawl_run)
    if not report:
        report = deterministic_candidate_landscape_report(catalog, prefs, crawl_run)
    if report:
        print("\n" + report)
        print(f"\n摘要报告已保存：{catalog.parent / 'candidate_landscape_summary.md'}")

def step_search_profile(user_query: str) -> dict | None:
    """Use StepFun to turn broad user intent into crawler-friendly search metadata."""
    if not os.environ.get("STEP_API_KEY"):
        return None
    try:
        from cell_note_agent.step_api import chat_completion, first_message_content

        system = (
            "You convert broad biomedical dataset requests into a strict JSON search profile. "
            "Return ONLY JSON. No markdown. Fields: optimized_query, species, target_genome_build, "
            "modalities, preferred_technology, preferred_repositories, quality_preferences, notes. "
            "Prefer public human 10x Genomics scATAC-seq / 10x Multiome datasets when compatible. "
            "Use English NCBI/SRA-friendly search terms in optimized_query. Do not invent accessions."
        )
        response = chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": user_query}],
            temperature=0.0,
            max_tokens=512,
        )
        parsed = _json_from_text(first_message_content(response))
        if isinstance(parsed, dict):
            return parsed
    except Exception as error:
        print(f"[step-search-profile] warning: {error}")
    return None


def search_profile_query(user_query: str) -> tuple[str, dict]:
    profile = step_search_profile(user_query) or {}
    optimized = str(profile.get("optimized_query") or "").strip()
    if not optimized:
        optimized = optimize_search_query(user_query)
        profile.setdefault("optimized_query", optimized)
    profile.setdefault("species", "Homo sapiens")
    profile.setdefault("target_genome_build", "GRCh38")
    profile.setdefault("preferred_technology", "10x Genomics")
    profile.setdefault("preferred_repositories", ["SRA", "GEO", "ENA"])
    profile.setdefault("quality_preferences", ["human", "10x", "public", "downloadable", "ATAC-bearing"])
    return optimized, profile


def infer_modality(strategies: list[str], study: str, projects: list[str]) -> str:
    joined = " ".join([study, *projects, *strategies]).lower()
    if "multiome" in joined or ("rna" in joined and "atac" in joined):
        return "multiome_or_rna_atac"
    if "atac" in joined:
        return "scatac_or_atac"
    return "unknown_atac_relevance"


def heuristic_candidate_classification(row: dict, profile: dict) -> dict:
    preferences = profile.get("user_preferences") if isinstance(profile.get("user_preferences"), dict) else profile
    species = str(row.get("scientific_name") or "")
    strategy = str(row.get("library_strategy") or "")
    text = " ".join(str(row.get(key, "")) for key in ["study_accession", "secondary_project", "scientific_name", "library_strategy", "title", "inferred_modality"]).lower()
    best_role = str(row.get("best_file_role") or "unknown")
    fit = str(row.get("pipeline_fit") or "medium")
    requested_type = str(preferences.get("data_type") or "").lower()
    target_species = str(preferences.get("species") or "").lower()
    target_build = str(preferences.get("target_genome_build") or "")
    size_limit = preferences.get("size_limit_gb")
    preferred_size = int(row.get("preferred_size_bytes") or row.get("total_size_bytes") or 0)
    smallest_size = int(row.get("smallest_file_size_bytes") or 0)
    facts: list[str] = []
    unknowns: list[str] = []
    mismatches: list[str] = []
    if species:
        if target_species and target_species not in species.lower():
            mismatches.append(f"物种元数据为 {species}")
        else:
            facts.append(f"物种元数据为 {species}")
    else:
        unknowns.append("物种未标注")
    existing_modality = str(row.get("inferred_modality") or "").strip()
    inferred_modality = infer_modality(strategy.split(";"), text, str(row.get("secondary_project") or "").split(";"))
    modality = existing_modality if existing_modality not in {"", "unknown", "unknown_atac_relevance"} else inferred_modality
    if "multiome" in requested_type:
        if modality == "multiome_or_rna_atac":
            facts.append("元数据支持 RNA+ATAC/Multiome")
        elif modality == "scatac_or_atac":
            mismatches.append("仅确认 ATAC，未确认配对 RNA")
        else:
            unknowns.append("Multiome 模态未确认")
    if best_role in {"peak_matrix", "fragments"}:
        facts.append(f"发现 {best_role} 文件")
    elif best_role == "raw":
        facts.append("当前仅解析到原始 reads")
    else:
        unknowns.append("文件角色尚未确认")
    genome = str(row.get("genome_build") or "")
    if target_build:
        if genome and target_build.lower() in genome.lower():
            facts.append(f"基因组版本已标注为 {genome}")
        elif genome:
            mismatches.append(f"基因组元数据为 {genome}")
        else:
            unknowns.append(f"未确认是否为 {target_build}")
    if size_limit not in {None, ""}:
        try:
            limit_bytes = float(size_limit) * 1e9
            if preferred_size and preferred_size <= limit_bytes:
                facts.append("优先文件组合在体积偏好内")
            elif smallest_size and smallest_size <= limit_bytes:
                facts.append("至少一个单文件在体积偏好内")
            elif preferred_size:
                facts.append("优先文件组合超过体积偏好，可继续做文件级选择")
            else:
                unknowns.append("文件大小未知")
        except (TypeError, ValueError):
            pass
    if mismatches:
        evidence_status = "mismatch"
        recommended = "no"
    elif best_role in {"peak_matrix", "fragments"} and not unknowns:
        evidence_status = "confirmed"
        recommended = "yes"
    elif facts:
        evidence_status = "partial"
        recommended = "review"
    else:
        evidence_status = "unknown"
        recommended = "review"

    return {
        "candidate_id": row.get("candidate_id", ""),
        "inferred_modality": modality,
        "genome_build": genome,
        "priority_score": 0,
        "priority_reason": "; ".join([*facts, *unknowns, *mismatches]) or "公开元数据不足，需要复核",
        "recommended": recommended,
        "best_file_role": best_role,
        "pipeline_fit": fit,
        "fit_reason": row.get("fit_reason", ""),
        "evidence_status": evidence_status,
        "confirmed_facts": " | ".join(facts),
        "unknown_fields": " | ".join(unknowns),
        "mismatch_fields": " | ".join(mismatches),
    }


def step_classify_candidates(rows: list[dict], profile: dict) -> dict[str, dict]:
    if not os.environ.get("STEP_API_KEY") or not rows:
        return {}
    try:
        from cell_note_agent.step_api import chat_completion, first_message_content

        compact = [
            {key: row.get(key, "") for key in ["candidate_id", "study_accession", "secondary_project", "scientific_name", "library_strategy", "best_file_role", "pipeline_fit", "run_count", "file_count", "total_size_gb"]}
            for row in rows[:25]
        ]
        system = (
            "Classify crawler dataset candidates for scATAC/multiome acquisition. Return ONLY JSON object "
            "with key candidates as a list. Each item: candidate_id, inferred_modality, genome_build, "
            "priority_score 0-100, priority_reason, recommended yes/review/no. "
            "Be honest: raw FASTQ usually has unknown genome build until processing. Prefer human, 10x, "
            "ATAC-bearing, downloadable, and analysis-ready (peak_matrix/fragments) when prefer_analysis_ready. "
            "Use best_file_role/pipeline_fit if present. Do not invent metadata."
        )
        user = json.dumps({"search_profile": profile, "candidates": compact}, ensure_ascii=False)
        response = chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=2048,
        )
        parsed = _json_from_text(first_message_content(response)) or {}
        output = {}
        for item in parsed.get("candidates", []):
            if isinstance(item, dict):
                output[str(item.get("candidate_id"))] = item
        return output
    except Exception as error:
        print(f"[step-candidate-classify] warning: {error}")
        return {}

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def crawl_file_rows_for_catalog(crawl_run: Path) -> list[dict]:
    """Merge audited crawler files with optional external adapter side-cars."""
    rows = read_jsonl(crawl_run / "remote_file_candidates.jsonl")
    rows.extend(read_jsonl(crawl_run / "external_remote_file_candidates.jsonl"))
    return rows


def crawl_run_rows_for_catalog(crawl_run: Path) -> list[dict]:
    rows = read_jsonl(crawl_run / "ena_run_manifest.jsonl")
    rows.extend(read_jsonl(crawl_run / "external_run_manifest.jsonl"))
    return rows


def merge_crawl_runs(primary: Path, secondary_runs: list[Path]) -> None:
    """Merge discovery sidecars from query shards without altering their audit logs."""
    jsonl_names = [
        "discovery_records.jsonl", "crawl_evidence.jsonl", "identifier_mentions.jsonl",
        "ena_run_manifest.jsonl", "remote_file_candidates.jsonl", "external_run_manifest.jsonl",
        "external_remote_file_candidates.jsonl", "external_dataset_records.jsonl",
    ]
    for name in jsonl_names:
        rows: list[dict] = []
        seen: set[str] = set()
        for directory in [primary, *secondary_runs]:
            for row in read_jsonl(directory / name):
                key = json.dumps(row, ensure_ascii=False, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
        if rows:
            with (primary / name).open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    unresolved: set[str] = set()
    for directory in [primary, *secondary_runs]:
        path = directory / "unresolved_accessions.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            unresolved.update(str(item) for item in payload.get("accessions", []))
    if unresolved:
        (primary / "unresolved_accessions.json").write_text(
            json.dumps({"accessions": sorted(unresolved)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def external_crawl_limit(default: int = 50) -> int:
    value = os.environ.get("CELLNOTE_EXTERNAL_CRAWL_LIMIT", "").strip()
    if not value:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def run_external_crawlers(
    crawl_dir: Path,
    query: str,
    requested_limit: int | None = None,
    *,
    queries: list[str] | None = None,
    progress=None,
    enable_ffq: bool = True,
) -> dict:
    try:
        from cell_note_agent.external_crawlers import run_external_discovery

        print("\n正在调用扩展检索：pysradb / ffq / GEOparse / OmicsDI / BioStudies / ENCODE / HuBMAP / SCP / GDC / EGA / CELLxGENE")
        summary = run_external_discovery(
            query,
            crawl_dir,
            limit=requested_limit or external_crawl_limit(),
            queries=queries,
            progress=progress,
            enable_ffq=enable_ffq,
        )
        print(
            "[external-crawlers] "
            f"pysradb_rows={summary.get('pysradb_rows', 0)}, "
            f"ffq_files={summary.get('ffq_files', 0)}, "
            f"omicsdi_records={summary.get('omicsdi_records', 0)}, "
            f"geo_supplementary_files={summary.get('geo_supplementary_files', 0)}"
        )
        return summary
    except Exception as error:
        if "cancelled by user" in str(error).lower():
            raise
        print(f"[external-crawlers] warning: {error}")
        return {"enabled": False, "error": str(error)}


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_pbmc500_manifest(run_root: Path) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    manifest = run_root / "file_manifest.csv"
    matrix_size = content_length(PBMC500_MATRIX_URL)
    peaks_size = content_length(PBMC500_PEAKS_URL)
    manifest.write_text(
        "\n".join(
            [
                "artifact_id,dataset_id,role,file_format,source_uri,size_bytes,checksum,local_path",
                f"pbmc500_peak_matrix_h5,pbmc500_agent_demo,peak_matrix,h5,{PBMC500_MATRIX_URL},{matrix_size},,",
                f"pbmc500_peaks_bed,pbmc500_agent_demo,peaks,bed,{PBMC500_PEAKS_URL},{peaks_size},,",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


def initialize_state(config: AgentConfig) -> AgentState:
    state = AgentState()
    crawl = latest_crawl_run(config.run_root)
    if crawl is not None:
        state.last_crawl_run = crawl
        profile_path = crawl / "search_profile.json"
        if profile_path.exists():
            state.last_search_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    catalog = config.run_root / "candidate_datasets.csv"
    if catalog.exists():
        state.last_candidate_catalog = catalog
    selected_candidates = sorted(config.run_root.glob("selected_file_manifest*.csv"), key=lambda path: path.stat().st_mtime)
    promoted_manifest = config.run_root / "promoted" / "file_manifest.csv"
    if selected_candidates:
        latest_manifest = selected_candidates[-1]
        if csv_data_row_count(latest_manifest) > 0:
            state.last_manifest = latest_manifest
    elif promoted_manifest.exists() and csv_data_row_count(promoted_manifest) > 0:
        state.last_manifest = promoted_manifest
    state.last_downloaded_manifest = latest_downloaded_manifest(config.run_root)
    return state

def sra_runinfo_fallback(crawl_run: Path, *, max_accessions: int = 25) -> int:
    """Fallback when ENA is unavailable: use NCBI SRA RunInfo to create downloadable SRA candidates."""
    import csv as csv_module
    import io
    import urllib.parse

    remote_path = crawl_run / "remote_file_candidates.jsonl"
    if remote_path.exists() and remote_path.stat().st_size > 0:
        return 0
    unresolved_path = crawl_run / "unresolved_accessions.json"
    if not unresolved_path.exists():
        return 0
    accessions = json.loads(unresolved_path.read_text(encoding="utf-8")).get("accessions", [])[:max_accessions]
    if not accessions:
        return 0

    run_rows = {}
    file_rows = {}
    for accession in accessions:
        url = "https://trace.ncbi.nlm.nih.gov/Traces/sra-db-be/runinfo?" + urllib.parse.urlencode({"acc": accession})
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "cellnote-agent/0.1"})
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8", "replace")
        except Exception as error:
            print(f"[sra-runinfo-fallback] warning: {accession}: {error}")
            continue
        reader = csv_module.DictReader(io.StringIO(body))
        for row in reader:
            run = (row.get("Run") or "").strip()
            uri = (row.get("download_path") or "").strip()
            if not run or not uri:
                continue
            study = (row.get("SRAStudy") or row.get("BioProject") or accession).strip()
            try:
                size_bytes = int(float(row.get("size_MB") or 0) * 1024 * 1024)
            except ValueError:
                size_bytes = 0
            run_rows[run] = {
                "run_accession": run,
                "study_accession": study,
                "secondary_study_accession": study,
                "experiment_accession": row.get("Experiment", ""),
                "sample_accession": row.get("Sample", ""),
                "secondary_sample_accession": row.get("BioSample", ""),
                "scientific_name": row.get("ScientificName", ""),
                "library_strategy": row.get("LibraryStrategy", ""),
                "library_source": row.get("LibrarySource", ""),
                "library_selection": row.get("LibrarySelection", ""),
                "library_layout": row.get("LibraryLayout", ""),
                "instrument_platform": row.get("Platform", ""),
                "instrument_model": row.get("Model", ""),
                "first_public": row.get("ReleaseDate", ""),
                "secondary_project": row.get("BioProject", ""),
                "source_ref": url,
                "source_sha256": "",
            }
            file_rows[run] = {
                "file_id": run,
                "source": "ncbi_sra_runinfo",
                "study_accession": study,
                "experiment_accession": row.get("Experiment", ""),
                "run_accession": run,
                "sample_accession": row.get("Sample", ""),
                "uri": uri,
                "file_format": "sra",
                "file_role": "sra_run",
                "size_bytes": size_bytes,
                "checksum_algorithm": "",
                "checksum": "",
                "source_ref": url,
                "source_sha256": "",
            }

    if not file_rows:
        return 0
    with (crawl_run / "ena_run_manifest.jsonl").open("a", encoding="utf-8") as handle:
        for row in run_rows.values():
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with remote_path.open("a", encoding="utf-8") as handle:
        for row in file_rows.values():
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"[sra-runinfo-fallback] added {len(file_rows)} downloadable SRA candidates from NCBI RunInfo")
    return len(file_rows)

def build_candidate_catalog(config: AgentConfig, state: AgentState, crawl_run: Path | None = None) -> Path | None:
    source = crawl_run or state.last_crawl_run
    if source is None:
        print("还没有 crawl run。请先说：帮我搜集人类 scATAC 数据集。")
        return None
    if not source.is_absolute():
        source = (config.repo_root / source).resolve()
    files = crawl_file_rows_for_catalog(source)
    if not files:
        sra_runinfo_fallback(source)
        files = crawl_file_rows_for_catalog(source)
    runs = crawl_run_rows_for_catalog(source)
    dataset_records = read_jsonl(source / "external_dataset_records.jsonl")
    if not files and not dataset_records:
        print(f"没有发现可下载远程文件：{source / 'remote_file_candidates.jsonl'}")
        print("建议：换更具体的 query，或直接输入 GSE/SRP/PRJNA/SRR accession。")
        return None

    profile = state.last_search_profile or {}
    profile_path = source / "search_profile.json"
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))

    runs_by_study: dict[str, list[dict]] = {}
    for run in runs:
        study = str(
            run.get("secondary_study_accession")
            or run.get("study_accession")
            or run.get("secondary_project")
            or "unknown"
        )
        runs_by_study.setdefault(study, []).append(run)

    files_by_study: dict[str, list[dict]] = {}
    for item in files:
        study = str(item.get("study_accession") or "unknown")
        files_by_study.setdefault(study, []).append(item)

    rows: list[dict] = []
    for index, study in enumerate(sorted(files_by_study), start=1):
        study_files = files_by_study[study]
        study_runs = runs_by_study.get(study, [])
        study_total_size = sum(int(item.get("size_bytes") or 0) for item in study_files)
        strategies = sorted({str(item.get("library_strategy") or "") for item in study_runs if item.get("library_strategy")})
        species = sorted({str(item.get("scientific_name") or "") for item in study_runs if item.get("scientific_name")})
        projects = sorted({str(item.get("secondary_project") or "") for item in study_runs if item.get("secondary_project")})
        file_summary = summarize_study_files(study_files)
        row = {
            "candidate_id": index,
            "study_accession": study,
            "repository_source": "ENA/SRA",
            "secondary_project": ";".join(projects),
            "scientific_name": ";".join(species),
            "library_strategy": ";".join(strategies),
            "best_file_role": file_summary["best_file_role"],
            "pipeline_fit": file_summary["pipeline_fit"],
            "fit_reason": file_summary["fit_reason"],
            "run_count": len(study_runs),
            "file_count": len(study_files),
            "preferred_file_count": file_summary["preferred_file_count"],
            "total_size_bytes": file_summary["preferred_size_bytes"],
            "total_size_gb": f"{file_summary['preferred_size_bytes'] / 1e9:.3f}",
            "study_total_size_bytes": study_total_size,
            "study_total_size_gb": f"{study_total_size / 1e9:.3f}",
            "smallest_file_size_bytes": file_summary["smallest_file_size_bytes"],
            "unknown_size_file_count": file_summary["unknown_size_file_count"],
            "title": "",
            "access": "public_or_unknown",
            "landing_url": "",
            "publication_date": "",
            "metadata_only": "no",
        }
        rows.append(row)

    rows_by_study = {str(row.get("study_accession") or "").upper(): row for row in rows}
    for record in dataset_records:
        accessions = [str(item).upper() for item in record.get("accessions") or []]
        study = next((item for item in accessions if item), "") or str(record.get("source_id") or "").strip()
        if not study:
            continue
        row = next((rows_by_study[item] for item in accessions if item in rows_by_study), None)
        if row is None:
            row = {
                "candidate_id": len(rows) + 1,
                "study_accession": study,
                "repository_source": str(record.get("source") or "external metadata"),
                "secondary_project": "",
                "scientific_name": str(record.get("scientific_name") or ""),
                "library_strategy": "",
                "best_file_role": "metadata_only",
                "pipeline_fit": "review",
                "fit_reason": "已发现公开元数据；下载前需要进一步解析文件清单或访问权限",
                "inferred_modality": str(record.get("inferred_modality") or "unknown_atac_relevance"),
                "genome_build": str(record.get("genome_build") or ""),
                "priority_score": 0,
                "recommended": "no",
                "priority_reason": "metadata discovery",
                "run_count": 0,
                "file_count": int(record.get("file_count") or 0),
                "preferred_file_count": int(record.get("file_count") or 0),
                "total_size_bytes": int(record.get("total_size_bytes") or 0),
                "total_size_gb": f"{int(record.get('total_size_bytes') or 0) / 1e9:.3f}",
                "study_total_size_bytes": int(record.get("total_size_bytes") or 0),
                "study_total_size_gb": f"{int(record.get('total_size_bytes') or 0) / 1e9:.3f}",
                "smallest_file_size_bytes": 0,
                "unknown_size_file_count": int(record.get("file_count") or 0),
                "title": str(record.get("title") or ""),
                "access": str(record.get("access") or "public_metadata"),
                "landing_url": str(record.get("landing_url") or ""),
                "publication_date": str(record.get("publication_date") or ""),
                "metadata_only": "yes",
            }
            rows.append(row)
            rows_by_study[study.upper()] = row
        else:
            sources = [item for item in str(row.get("repository_source") or "").split(";") if item]
            source_name = str(record.get("source") or "")
            if source_name and source_name not in sources:
                sources.append(source_name)
            row["repository_source"] = ";".join(sources)
            for target, source_key in (("title", "title"), ("access", "access"), ("landing_url", "landing_url"), ("publication_date", "publication_date")):
                if not row.get(target) and record.get(source_key):
                    row[target] = record[source_key]
            for target, source_key in (("scientific_name", "scientific_name"), ("genome_build", "genome_build"), ("inferred_modality", "inferred_modality")):
                if (not row.get(target) or str(row.get(target)).startswith("unknown")) and record.get(source_key):
                    row[target] = record[source_key]

    for row in rows:
        row.update(heuristic_candidate_classification(row, profile))
    role_rank = {"peak_matrix": 0, "fragments": 1, "peaks": 2, "unknown": 3, "metadata_only": 4, "raw": 5}
    evidence_rank = {"confirmed": 0, "partial": 1, "unknown": 2, "mismatch": 3}
    preferences = profile.get("user_preferences") if isinstance(profile.get("user_preferences"), dict) else profile
    try:
        size_limit_bytes = float(preferences.get("size_limit_gb")) * 1e9
    except (TypeError, ValueError):
        size_limit_bytes = 0
    rows.sort(
        key=lambda row: (
            role_rank.get(str(row.get("best_file_role") or "unknown"), 9),
            evidence_rank.get(str(row.get("evidence_status") or "unknown"), 9),
            bool(size_limit_bytes and int(row.get("total_size_bytes") or 0) > size_limit_bytes),
            int(row.get("total_size_bytes") or 0) == 0,
            int(row.get("total_size_bytes") or 0),
        )
    )
    for index, row in enumerate(rows, start=1):
        row["candidate_id"] = index

    catalog = config.run_root / "candidate_datasets.csv"
    write_csv(
        catalog,
        rows,
        [
            "candidate_id",
            "study_accession",
            "repository_source",
            "secondary_project",
            "scientific_name",
            "library_strategy",
            "best_file_role",
            "pipeline_fit",
            "fit_reason",
            "inferred_modality",
            "genome_build",
            "priority_score",
            "recommended",
            "priority_reason",
            "run_count",
            "file_count",
            "preferred_file_count",
            "total_size_bytes",
            "total_size_gb",
            "study_total_size_bytes",
            "study_total_size_gb",
            "smallest_file_size_bytes",
            "unknown_size_file_count",
            "evidence_status",
            "confirmed_facts",
            "unknown_fields",
            "mismatch_fields",
            "title",
            "access",
            "landing_url",
            "publication_date",
            "metadata_only",
        ],
    )
    state.last_candidate_catalog = catalog
    print_candidate_catalog(catalog)
    return catalog

def print_candidate_catalog(catalog: Path) -> None:
    rows: list[dict] = []
    with catalog.open(newline="", encoding="utf-8") as handle:
        rows.extend(csv.DictReader(handle))
    if not rows:
        print("候选表为空。")
        return
    print(f"\n候选数据集：{catalog}")
    for row in rows[:20]:
        print(
            f"[{row['candidate_id']}] {row['study_accession']} "
            f"role={row.get('best_file_role', '-') or '-'} "
            f"source={row.get('repository_source', '-') or '-'} "
            f"species={row['scientific_name'] or '-'} "
            f"genome={row.get('genome_build', '-') or '-'} "
            f"modality={row.get('inferred_modality', '-') or '-'} "
            f"evidence={row.get('evidence_status', 'unknown')} "
            f"runs={row['run_count']} files={row['file_count']} "
            f"preferred_files={row.get('preferred_file_count', row['file_count'])} "
            f"preferred_size={row['total_size_gb']}GB study_size={row.get('study_total_size_gb', row['total_size_gb'])}GB"
            f" | {row.get('fit_reason') or row.get('priority_reason') or ''}"
        )
    if len(rows) > 20:
        print(f"... plus {len(rows) - 20} more")

def parse_selection(text: str) -> list[int]:
    match = re.search(r"(?:选择|select)\s*([0-9,\s，]+)", text, flags=re.IGNORECASE)
    if not match:
        return []
    values = []
    for item in re.split(r"[,，\s]+", match.group(1).strip()):
        if item.isdigit():
            values.append(int(item))
    return sorted(set(values))


MANIFEST_FIELDNAMES = ["artifact_id", "dataset_id", "role", "source_uri", "size_bytes", "source", "discovered_via", "checksum", "local_path"]


def candidate_ids_from_catalog(catalog: Path, *, recommended_only: bool = True) -> list[int]:
    rows: list[dict] = []
    with catalog.open(newline="", encoding="utf-8") as handle:
        rows.extend(csv.DictReader(handle))
    ids: list[int] = []
    for row in rows:
        if recommended_only and str(row.get("recommended", "")).lower() not in {"yes", "true", "1"}:
            continue
        try:
            ids.append(int(row["candidate_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not ids and recommended_only:
        return candidate_ids_from_catalog(catalog, recommended_only=False)
    return ids


def read_manifest_rows(manifest: Path) -> list[dict]:
    with manifest.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def print_manifest_summary(manifest: Path, *, max_rows: int = 50) -> None:
    rows = read_manifest_rows(manifest)
    total_size = sum(int(row.get("size_bytes") or 0) for row in rows)
    print(f"\n下载清单：{manifest}")
    print(f"entries：{len(rows)}，estimated size：{total_size / 1e9:.3f} GB")
    if not rows:
        return
    print("清单内容：")
    for index, row in enumerate(rows[:max_rows], 1):
        size_gb = int(row.get("size_bytes") or 0) / 1e9
        artifact = row.get("artifact_id", "")
        dataset = row.get("dataset_id", "")
        role = row.get("role", "")
        source = row.get("source", "")
        print(f"[{index}] dataset={dataset} artifact={artifact} role={role} source={source} size={size_gb:.3f}GB")
    if len(rows) > max_rows:
        print(f"... plus {len(rows) - max_rows} more manifest rows")


def write_manifest_revision(config: AgentConfig, state: AgentState, rows: list[dict]) -> Path:
    version = 2
    while True:
        manifest = config.run_root / f"selected_file_manifest_v{version}.csv"
        if not manifest.exists():
            break
        version += 1
    write_csv(manifest, rows, MANIFEST_FIELDNAMES)
    state.last_manifest = manifest
    print_manifest_summary(manifest)
    return manifest


def parse_manifest_deletions(text: str) -> tuple[set[int], set[str]]:
    lower = text.lower()
    if not any(keyword in lower for keyword in ["删除", "删掉", "remove", "drop", "exclude"]):
        return set(), set()
    row_ids: set[int] = set()
    dataset_ids: set[str] = set(re.findall(r"(?:SRP|SRR|PRJNA|ERP|GSE)\d+", text, flags=re.IGNORECASE))
    for match in re.findall(r"(?:删除|删掉|remove|drop|exclude)\s*([0-9,，\s]+)", text, flags=re.IGNORECASE):
        for item in re.split(r"[,，\s]+", match.strip()):
            if item.isdigit():
                row_ids.add(int(item))
    return row_ids, {item.upper() for item in dataset_ids}



def manifest_rows_for_prompt(rows: list[dict], *, max_rows: int = 80) -> list[dict]:
    compact = []
    for index, row in enumerate(rows[:max_rows], 1):
        compact.append(
            {
                "row": index,
                "dataset_id": row.get("dataset_id", ""),
                "artifact_id": row.get("artifact_id", ""),
                "role": row.get("role", ""),
                "source": row.get("source", ""),
                "size_gb": round(int(row.get("size_bytes") or 0) / 1e9, 3),
            }
        )
    return compact


def step_manifest_edit_plan(user_text: str, rows: list[dict]) -> dict | None:
    if not os.environ.get("STEP_API_KEY"):
        return None
    try:
        from cell_note_agent.step_api import chat_completion, first_message_content

        total_gb = sum(int(row.get("size_bytes") or 0) for row in rows) / 1e9
        system = (
            "You edit a download manifest according to the user's natural-language instruction. "
            "Return ONLY strict JSON. Never return shell commands. Allowed actions: keep_smallest, keep_rows, "
            "remove_rows, remove_datasets, filter_by_size, cancel, download, clarify. Fields: action, keep_count, "
            "rows, dataset_ids, max_total_gb, max_file_gb, message. Interpret Chinese and English. "
            "Examples: '太大了只下载一个小文件' => {action:'keep_smallest', keep_count:1}. "
            "'控制在5GB以内' => {action:'filter_by_size', max_total_gb:5}. "
            "'删除1,2' => {action:'remove_rows', rows:[1,2]}."
        )
        user = json.dumps(
            {
                "instruction": user_text,
                "manifest_summary": {"row_count": len(rows), "total_gb": round(total_gb, 3)},
                "rows": manifest_rows_for_prompt(rows),
            },
            ensure_ascii=False,
        )
        response = chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=512,
        )
        parsed = _json_from_text(first_message_content(response))
        return parsed if isinstance(parsed, dict) else None
    except Exception as error:
        print(f"[step-manifest-edit] warning: {error}")
        return None


def deterministic_manifest_edit_plan(user_text: str, rows: list[dict]) -> dict:
    lower = user_text.lower()
    if not user_text or lower in {"n", "no", "取消", "退出", "不下载"}:
        return {"action": "cancel"}
    if lower in {"y", "yes", "下载", "确认", "确认下载", "download", "fetch"}:
        return {"action": "download"}
    row_ids, dataset_ids = parse_manifest_deletions(user_text)
    if row_ids or dataset_ids:
        return {"action": "remove_rows", "rows": sorted(row_ids), "dataset_ids": sorted(dataset_ids)}
    count_match = re.search(r"(?:只|保留|下载|keep|smallest|最小|小文件)[^0-9]*(\d+)\s*(?:个|条|files?|rows?)?", user_text, flags=re.IGNORECASE)
    if any(keyword in lower for keyword in ["小文件", "最小", "太大", "太多", "smaller", "smallest", "too large", "only one"]):
        keep_count = int(count_match.group(1)) if count_match else 1
        return {"action": "keep_smallest", "keep_count": max(1, keep_count)}
    gb_match = re.search(r"(\d+(?:\.\d+)?)\s*(gb|g)", lower)
    mb_match = re.search(r"(\d+(?:\.\d+)?)\s*(mb|m)", lower)
    if gb_match and any(keyword in lower for keyword in ["以内", "小于", "不超过", "控制", "under", "less", "max"]):
        return {"action": "filter_by_size", "max_total_gb": float(gb_match.group(1))}
    if mb_match and any(keyword in lower for keyword in ["以内", "小于", "不超过", "控制", "under", "less", "max"]):
        return {"action": "filter_by_size", "max_total_gb": float(mb_match.group(1)) / 1000}
    return {"action": "clarify", "message": "我没理解要如何修改清单。可以说：只保留最小1个文件、控制在5GB内、删除1,2、删除SRPxxxx、下载或取消。"}


def apply_manifest_edit_plan(rows: list[dict], plan: dict) -> tuple[list[dict] | None, str]:
    action = str(plan.get("action") or "").strip().lower()
    if action == "keep_smallest":
        keep_count = int(plan.get("keep_count") or 1)
        nonzero_rows = [row for row in rows if int(row.get("size_bytes") or 0) > 0]
        source_rows = nonzero_rows or rows
        kept = sorted(source_rows, key=lambda row: int(row.get("size_bytes") or 0))[: max(1, keep_count)]
        zero_note = "（已跳过 size=0 的异常条目）" if nonzero_rows and len(nonzero_rows) < len(rows) else ""
        return kept, f"已按大小升序只保留 {len(kept)} 个最小文件{zero_note}。"
    if action == "keep_rows":
        wanted = {int(item) for item in plan.get("rows", []) if str(item).isdigit()}
        kept = [row for index, row in enumerate(rows, 1) if index in wanted]
        return kept, f"已只保留指定的 {len(kept)} 条。"
    if action in {"remove_rows", "remove_datasets"}:
        remove_rows = {int(item) for item in plan.get("rows", []) if str(item).isdigit()}
        remove_datasets = {str(item).upper() for item in plan.get("dataset_ids", [])}
        kept = []
        for index, row in enumerate(rows, 1):
            dataset = str(row.get("dataset_id", "")).upper()
            artifact = str(row.get("artifact_id", "")).upper()
            if index in remove_rows or dataset in remove_datasets or artifact in remove_datasets:
                continue
            kept.append(row)
        return kept, f"已删除 {len(rows) - len(kept)} 条。"
    if action == "filter_by_size":
        max_file_gb = plan.get("max_file_gb")
        max_total_gb = plan.get("max_total_gb")
        candidates = list(rows)
        if max_file_gb not in {None, ""}:
            max_file_bytes = float(max_file_gb) * 1e9
            candidates = [row for row in candidates if int(row.get("size_bytes") or 0) <= max_file_bytes]
        if max_total_gb not in {None, ""}:
            max_total_bytes = float(max_total_gb) * 1e9
            kept = []
            total = 0
            for row in sorted(candidates, key=lambda item: int(item.get("size_bytes") or 0)):
                size = int(row.get("size_bytes") or 0)
                if total + size > max_total_bytes and kept:
                    continue
                if size <= max_total_bytes or not kept:
                    kept.append(row)
                    total += size
            candidates = kept
        return candidates, f"已按大小约束筛选，保留 {len(candidates)} 条。"
    return None, str(plan.get("message") or "没有可应用的清单修改。")

def review_manifest_interactively(config: AgentConfig, state: AgentState, manifest: Path) -> None:
    print_manifest_summary(manifest)
    if config.assume_yes:
        print("自动模式下只生成并展示清单，不自动下载真实数据。")
        return
    current = manifest
    while True:
        answer = manifest_review_answer()
        rows = read_manifest_rows(current)
        plan = step_manifest_edit_plan(answer, rows) or deterministic_manifest_edit_plan(answer, rows)
        action = str(plan.get("action") or "").strip().lower()
        if action == "cancel":
            print("已暂停在清单确认节点；之后可输入：下载刚才的 manifest。")
            return
        if action == "download":
            run_download_manifest(config, str(current), state)
            return
        updated_rows, message = apply_manifest_edit_plan(rows, plan)
        if updated_rows is None:
            print(message)
            continue
        if not updated_rows:
            print("这个修改会让清单变空，我没有保存。请换一个条件，例如：只保留最小1个文件。")
            continue
        if len(updated_rows) == len(rows):
            print(message)
            print("清单条目数没有变化；如果要缩小清单，可以说：只保留最小1个文件，或控制在5GB内。")
            continue
        print(message)
        current = write_manifest_revision(config, state, updated_rows)

def generate_manifest_from_catalog(config: AgentConfig, state: AgentState, *, candidate_ids: list[int] | None = None) -> Path | None:
    catalog = state.last_candidate_catalog or build_candidate_catalog(config, state)
    if catalog is None:
        return None
    ids = candidate_ids or candidate_ids_from_catalog(catalog, recommended_only=True)
    if not ids:
        print("没有可用于生成下载清单的候选。")
        return None
    return create_manifest_from_selection(config, state, ids)


def _catalog_rows(catalog: Path) -> list[dict]:
    with catalog.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def analysis_ready_candidate_ids(catalog: Path) -> list[int]:
    ids: list[int] = []
    for row in _catalog_rows(catalog):
        role = str(row.get("best_file_role") or "").lower()
        fit = str(row.get("pipeline_fit") or "").lower()
        recommended = str(row.get("recommended") or "").lower()
        if role in {"peak_matrix", "fragments"} or fit == "high" or recommended in {"yes", "true", "1"}:
            try:
                ids.append(int(row["candidate_id"]))
            except (KeyError, TypeError, ValueError):
                continue
    return ids


def smallest_candidate_ids(catalog: Path, *, n: int = 1) -> list[int]:
    rows = _catalog_rows(catalog)
    ranked = sorted(rows, key=lambda row: float(row.get("total_size_gb") or 1e18))
    ids: list[int] = []
    for row in ranked[:n]:
        try:
            ids.append(int(row["candidate_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def offer_manifest_generation(config: AgentConfig, state: AgentState) -> None:
    """Smart post-crawl triage: auto analysis-ready / smoke / manual / stop."""
    if state.last_candidate_catalog is None:
        return
    catalog = state.last_candidate_catalog
    rows = _catalog_rows(catalog)
    if not rows:
        return

    ready_ids = analysis_ready_candidate_ids(catalog)
    high = sum(1 for row in rows if str(row.get("pipeline_fit")) == "high")
    medium = sum(1 for row in rows if str(row.get("pipeline_fit")) == "medium")
    low = sum(1 for row in rows if str(row.get("pipeline_fit")) == "low")
    print("\n[interaction-gates] 候选分诊")
    print(f"- 总计 {len(rows)} 个候选：pipeline_fit high={high}, medium={medium}, low={low}")
    print(f"- 建议优先（analysis-ready / recommended）：{ready_ids[:12] or '暂无'}")
    print("说明：优先 peak_matrix/fragments；raw FASTQ/SRA 适合对齐重跑，不适合立刻做 QC Demo。")

    if config.assume_yes or getattr(config, "auto_all", False):
        ids = ready_ids or candidate_ids_from_catalog(catalog, recommended_only=False)[:1]
        if not ids:
            print("没有可自动选择的候选。")
            return
        manifest = create_manifest_from_selection(config, state, ids)
        if manifest:
            review_manifest_interactively(config, state, manifest)
        return

    choice = choose_option(
        "下一步怎么处理这些候选？",
        [
            "自动选 analysis-ready / 推荐候选并生成清单（推荐）",
            "烟雾测试：只选体量最小的 1 个候选",
            "手动选择候选编号",
            "仅收紧展示：只看 analysis-ready，稍后再决定",
            "停在这里，稍后再说",
        ],
        default_index=0 if ready_ids else 1,
    )

    if choice == 4:
        print("已停在候选确认节点；之后可输入：生成下载清单，或：选择 1,3 生成下载清单。")
        return

    if choice == 3:
        if ready_ids:
            print("analysis-ready / 推荐候选：")
            for row in rows:
                try:
                    cid = int(row["candidate_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if cid in set(ready_ids):
                    print(
                        f"[{cid}] {row.get('study_accession')} role={row.get('best_file_role')} "
                        f"fit={row.get('pipeline_fit')} size={row.get('total_size_gb')}GB "
                        f"score={row.get('priority_score')}"
                    )
            follow = choose_option(
                "是否现在用这些候选生成清单？",
                ["是，生成清单", "否，先停在这里"],
                default_index=0,
            )
            if follow == 0:
                manifest = create_manifest_from_selection(config, state, ready_ids)
                if manifest:
                    review_manifest_interactively(config, state, manifest)
            else:
                print("已收紧候选视图；需要时输入：选择 <编号> 生成下载清单。")
        else:
            print("当前没有明显 analysis-ready 候选。建议：换更具体的 query，或选烟雾测试/手动。")
        return

    if choice == 2:
        raw = input("请输入候选编号（例如 1,3）：").strip()
        ids = []
        for item in re.split(r"[,，\s]+", raw):
            if item.isdigit():
                ids.append(int(item))
        if not ids:
            print("未解析到编号。")
            return
        manifest = create_manifest_from_selection(config, state, sorted(set(ids)))
        if manifest:
            review_manifest_interactively(config, state, manifest)
        return

    if choice == 1:
        ids = smallest_candidate_ids(catalog, n=1)
        print(f"烟雾测试候选：{ids}")
    else:
        ids = ready_ids or smallest_candidate_ids(catalog, n=1)
        print(f"自动选择候选：{ids}")

    if not ids:
        print("没有可选候选。")
        return
    manifest = create_manifest_from_selection(config, state, ids)
    if manifest:
        # For smoke test, keep smallest files inside the selected study if huge.
        if choice == 1:
            rows_m = read_manifest_rows(manifest)
            if len(rows_m) > 3:
                keep = sorted(rows_m, key=lambda row: int(row.get("size_bytes") or 0))[:1]
                manifest = write_manifest_revision(config, state, keep)
                print("烟雾测试：清单已收缩为最小的 1 个文件。")
        review_manifest_interactively(config, state, manifest)

def create_manifest_from_selection(config: AgentConfig, state: AgentState, candidate_ids: list[int]) -> Path | None:
    if state.last_crawl_run is None:
        print("还没有 crawl run。")
        return None
    catalog = state.last_candidate_catalog or build_candidate_catalog(config, state)
    if catalog is None:
        return None
    rows = []
    with catalog.open(newline="", encoding="utf-8") as handle:
        rows.extend(csv.DictReader(handle))
    selected_studies = {
        str(row["study_accession"])
        for row in rows
        if int(row["candidate_id"]) in set(candidate_ids)
    }
    if not selected_studies:
        print("没有匹配到你选择的候选编号。")
        return None

    files = crawl_file_rows_for_catalog(state.last_crawl_run)
    profile = state.last_search_profile or {}
    prefer_ready = bool(profile.get("prefer_analysis_ready", True))
    selected_files = [item for item in files if str(item.get("study_accession") or "") in selected_studies]
    if prefer_ready and selected_files:
        ready = [
            item
            for item in selected_files
            if classify_remote_file_role(item) in {"peak_matrix", "fragments", "peaks"}
        ]
        if ready:
            print(f"[smart-manifest] 优先纳入 analysis-ready 文件：{len(ready)}/{len(selected_files)}")
            selected_files = ready
        else:
            print("[smart-manifest] 未发现明显 matrix/fragments；将保留所选候选的全部可下载文件，请人工审查。")

    manifest_rows = []
    for item in selected_files:
        checksum = ""
        if item.get("checksum"):
            algorithm = str(item.get("checksum_algorithm") or "md5").lower()
            checksum = f"{algorithm}:{item.get('checksum')}"
        role = item.get("file_role") or classify_remote_file_role(item)
        manifest_rows.append(
            {
                "artifact_id": item.get("file_id", ""),
                "dataset_id": item.get("study_accession", ""),
                "role": role,
                "source_uri": item.get("uri", ""),
                "size_bytes": item.get("size_bytes", ""),
                "source": item.get("source", "ena"),
                "discovered_via": "agent_candidate_selection",
                "checksum": checksum,
                "local_path": "",
            }
        )
    if not manifest_rows:
        print("选择的候选没有可下载文件。")
        return None
    manifest = config.run_root / "selected_file_manifest.csv"
    write_csv(manifest, manifest_rows, MANIFEST_FIELDNAMES)
    state.last_manifest = manifest
    return manifest


SKILL_REGISTRY = {
    "download-validate": {
        "script": "scripts/download_validate.py",
        "description": "Plan, fetch, and verify manifest downloads.",
        "allowed_stages": ["plan", "fetch", "verify"],
    },
    "resource-setup": {
        "script": "scripts/prepare_references.py",
        "description": "Prepare and verify pinned GRCh38 chromosome sizes, blacklist, and optional hg19 liftover chain.",
        "allowed_stages": ["plan", "fetch", "verify"],
    },
    "scatac-fragment-qc": {
        "script": "scripts/scatac_fragment_qc.py",
        "description": "QC one scATAC fragment file or a multi-sample fragment collection and materialize one per-dataset peak matrix.",
        "allowed_stages": ["import", "pre-filter", "filter", "embed", "cluster", "doublet", "call-peaks", "make-peak-matrix", "finalize"],
    },
    "scatac-peak-matrix": {
        "script": "scripts/scatac_peak_matrix.py",
        "description": "QC and standardize an existing cell x peak matrix.",
        "allowed_stages": ["load", "profile", "filter", "standardize", "embed-cluster", "finalize"],
    },
    "existing-peak-matrix-package": {
        "script": "scripts/package_existing_peak_matrix.py",
        "description": "Backed metadata QC and packaging for very large existing h5ad cell x peak matrices.",
        "allowed_stages": ["inspect", "materialize", "finalize"],
    },
    "multiome-qc": {
        "script": "scripts/multiome_qc.py",
        "description": "QC paired RNA and ATAC multiome inputs.",
        "allowed_stages": ["pair-check", "qc-rna", "qc-atac", "intersect", "finalize"],
    },
    "normalize-to-peak-matrix": {
        "script": "scripts/normalize_to_peak_matrix.py",
        "description": "Normalize supported inputs into canonical GRCh38 peak matrix outputs.",
        "allowed_stages": ["plan", "materialize", "validate"],
    },
    "handoff-pipeline": {
        "script": "scripts/package_peak_matrices.py",
        "description": "Package peak matrices, QC reports, and data cards for handoff.",
        "allowed_stages": ["cards", "validate", "package"],
    },
}


def safe_dataset_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "dataset"


def sanitize_extracted_path_token(token: str) -> str:
    """Clean path tokens glued to Chinese/punctuation, e.g. '.../Li2023a数据'."""
    cleaned = token.strip().strip("。.!?）)】]\"'`")
    # Drop trailing CJK words commonly glued after a path.
    cleaned = re.sub(r"[\u4e00-\u9fff]+$", "", cleaned)
    cleaned = cleaned.rstrip("。.!?）)】]/\"'`")
    # If still nonexistent, progressively trim trailing junk characters.
    return cleaned


def prefer_analysis_file(path: Path) -> Path:
    """If a directory is given, prefer a cell_by_peak / peak h5ad inside it."""
    if path.is_file() or not path.is_dir():
        return path
    matches = list(path.glob("*.h5ad")) + list(path.glob("*/*.h5ad"))
    if not matches:
        return path

    def rank(item: Path) -> tuple[int, int, str]:
        name = item.name.lower()
        if "cell_by_peak" in name:
            return (0, len(str(item)), str(item))
        if "peak" in name and "ccre" not in name:
            return (1, len(str(item)), str(item))
        if name.endswith(".h5ad"):
            return (2, len(str(item)), str(item))
        return (3, len(str(item)), str(item))

    return sorted(matches, key=rank)[0]


def extract_existing_analysis_paths(text: str) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()
    for match in re.findall(r"/[^\s，,；;]+", text):
        cleaned = sanitize_extracted_path_token(match)
        if not cleaned.startswith("/"):
            continue
        # Try exact cleaned path, then progressive parent trim for glued suffixes.
        trial = cleaned
        found: Path | None = None
        while trial.startswith("/"):
            path = Path(trial).expanduser()
            if path.exists():
                found = prefer_analysis_file(path.resolve())
                break
            # Trim one trailing path segment if it looks like glued junk.
            parent = str(Path(trial).parent)
            if parent == trial:
                break
            trial = parent
        if found is not None and str(found) not in seen:
            seen.add(str(found))
            candidates.append(found)
    return candidates


def known_datasets_files(config: AgentConfig) -> list[Path]:
    return [
        config.repo_root / "configs" / "known_datasets.json",
        config.run_root / "known_datasets.json",
    ]


def load_known_datasets(config: AgentConfig) -> dict[str, dict]:
    """Load dataset aliases from configs/known_datasets.json (and optional run_root override)."""
    merged: dict[str, dict] = {}
    for path in known_datasets_files(config):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"[known-datasets] warning: failed to read {path}: {error}")
            continue
        if not isinstance(payload, dict):
            continue
        items = payload.get("datasets", payload)
        if not isinstance(items, dict):
            continue
        for alias, value in items.items():
            name = safe_dataset_id(str(alias))
            if isinstance(value, str):
                entry = {"path": value, "dataset_id": name}
            elif isinstance(value, dict):
                entry = {
                    "path": str(value.get("path") or value.get("input_path") or ""),
                    "dataset_id": safe_dataset_id(str(value.get("dataset_id") or name)),
                    "notes": str(value.get("notes") or ""),
                }
            else:
                continue
            if entry["path"]:
                merged[name] = entry
                merged[str(alias)] = entry
    return merged


def resolve_dataset_alias(config: AgentConfig, token: str) -> Path | None:
    alias = token.strip().strip("\"'`")
    if not alias:
        return None
    datasets = load_known_datasets(config)
    entry = datasets.get(alias) or datasets.get(safe_dataset_id(alias))
    if entry is None:
        alias_fold = alias.casefold()
        for key, value in datasets.items():
            if str(key).casefold() == alias_fold or safe_dataset_id(str(key)).casefold() == alias_fold:
                entry = value
                break
    if not entry:
        return None
    path = Path(str(entry["path"])).expanduser()
    if not path.is_absolute():
        path = (config.repo_root / path).resolve()
    if not path.exists():
        return None
    return prefer_analysis_file(path)


def extract_alias_from_text(config: AgentConfig, text: str) -> Path | None:
    datasets = load_known_datasets(config)
    if not datasets:
        return None
    # Prefer longer aliases to avoid partial overlaps.
    # Use ASCII identifier boundaries so Chinese suffixes like "Li2023的数据" still match.
    for alias in sorted({str(key) for key in datasets.keys()}, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(alias)}(?![A-Za-z0-9_.-])", text, flags=re.IGNORECASE):
            resolved = resolve_dataset_alias(config, alias)
            if resolved is not None:
                return resolved
    return None


def intended_local_input_request(text: str) -> bool:
    """True when the user likely pointed at a local path/alias rather than downloaded manifest."""
    if "/" in text:
        return True
    lower = text.lower()
    return any(term in lower for term in ["已有", "本地", "别名", "li2023", "h5ad", "fragments.tsv"])


def resolve_analysis_input_path(config: AgentConfig, text: str, explicit_path: str = "") -> Path | None:
    if explicit_path.strip():
        cleaned = sanitize_extracted_path_token(explicit_path.strip())
        path = Path(cleaned).expanduser()
        if not path.is_absolute():
            path = (config.repo_root / path).resolve()
        if path.exists():
            return prefer_analysis_file(path)
        # Also try path extraction cleanup from the raw explicit token.
        recovered = extract_existing_analysis_paths(cleaned)
        if recovered:
            return recovered[0]
        return None
    paths = extract_existing_analysis_paths(text)
    if paths:
        return paths[0]
    return extract_alias_from_text(config, text)


def analysis_intent(text: str) -> bool:
    lower = text.lower()
    return any(
        term in lower
        for term in [
            "分析", "qc", "process", "analyze", "peak", "matrix", "fragments",
            "multiome", "输出", "package", "已有", "本地数据", "帮我做",
        ]
    )


def print_known_dataset_aliases(config: AgentConfig) -> None:
    datasets = load_known_datasets(config)
    unique: dict[str, dict] = {}
    for alias, entry in datasets.items():
        key = str(entry.get("dataset_id") or alias)
        unique.setdefault(key, {**entry, "alias": key})
    if not unique:
        print("（当前没有已注册别名。可在 configs/known_datasets.json 添加。）")
        return
    print("已注册数据集别名：")
    for alias, entry in sorted(unique.items()):
        notes = f"  # {entry['notes']}" if entry.get("notes") else ""
        print(f"- {alias}: {entry.get('path')}{notes}")


def prompt_for_existing_input(config: AgentConfig) -> Path | None:
    """Ask for a local path or registered dataset alias when none was provided."""
    print("\n未检测到可分析输入路径。")
    print("请提供本地绝对路径，或已注册的数据集别名。")
    print_known_dataset_aliases(config)
    if config.assume_yes:
        return None
    answer = input("路径或别名（留空取消）：").strip()
    if not answer:
        return None
    path = Path(answer).expanduser()
    if path.exists():
        return path.resolve()
    resolved = resolve_dataset_alias(config, answer)
    if resolved is not None:
        print(f"已解析别名 → {resolved}")
        return resolved
    # Allow "alias=/path" one-liner for convenience.
    if "=" in answer:
        alias, raw_path = answer.split("=", 1)
        candidate = Path(raw_path.strip()).expanduser()
        if candidate.exists():
            print(f"提示：可用 configs/known_datasets.json 持久注册别名 {alias.strip()}")
            return candidate.resolve()
    print(f"无法解析路径或别名：{answer}")
    return None


def dataset_id_from_prompt(text: str, fallback: str) -> str:
    patterns = [
        r"(?:数据集名|数据集id|dataset(?:_id)?|dataset name)\s*[:：]?\s*([A-Za-z0-9_.-]+)",
        r"(?:命名为|叫做)\s*([A-Za-z0-9_.-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return safe_dataset_id(match.group(1))
    return safe_dataset_id(fallback)


def genome_build_from_prompt(text: str) -> str:
    lower = text.lower()
    if "hg19" in lower or "grch37" in lower:
        return "hg19"
    if "hg38" in lower or "grch38" in lower:
        return "GRCh38"
    return "GRCh38"


def detect_existing_input(config: AgentConfig, path: Path, prompt: str) -> dict:
    script = config.repo_root / "scripts" / "detect_input.py"
    if not script.exists():
        return classify_existing_input(path, prompt)
    command = [config.processing_python, str(script), "--path", str(path)]
    genome = genome_build_from_prompt(prompt)
    if genome:
        command.extend(["--genome_build", genome])
    completed = subprocess.run(command, cwd=str(config.repo_root), check=False, text=True, capture_output=True)
    if completed.returncode not in {0, 2}:
        print(f"[detect-input] warning: {completed.stderr.strip() or completed.stdout.strip()}")
        return classify_existing_input(path, prompt)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        print("[detect-input] warning: detector did not return JSON; falling back to local heuristics")
        return classify_existing_input(path, prompt)


def detection_files(detected: dict) -> dict:
    files = detected.get("files")
    return files if isinstance(files, dict) else {}


def classify_existing_input(path: Path, prompt: str = "") -> dict:
    if path.is_dir():
        preferred = prefer_analysis_file(path)
        if preferred != path and preferred.exists():
            result = classify_existing_input(preferred, prompt)
            result["reason"] = f"directory input; selected {preferred.name}; " + str(result.get("reason") or "")
            return result
    name = path.name.lower()
    lower = prompt.lower()
    if path.is_dir():
        return {"input_kind": "peak_matrix", "matrix": str(path), "reason": "directory input; treating as matrix directory"}
    if "fragments.tsv" in name or name.endswith((".fragments.tsv.gz", ".fragments.tsv")):
        return {"input_kind": "fragments", "fragments": str(path), "reason": "ATAC fragments file detected from filename"}
    if name.endswith(".h5ad"):
        try:
            import anndata as ad
            adata = ad.read_h5ad(path, backed="r")
            try:
                first_vars = [str(item) for item in adata.var_names[:20]]
                peak_like = sum(1 for item in first_vars if re.match(r"^chr[^:]+:\d+-\d+$", item.replace(",", "")))
                obs_cols = {str(col).lower() for col in adata.obs.columns}
                input_kind = "peak_matrix" if peak_like >= max(1, len(first_vars) // 2) else "unknown"
                reason = f"h5ad detected; shape={adata.n_obs} cells x {adata.n_vars} vars; peak-like var names={peak_like}/{len(first_vars)}"
                is_large = input_kind == "peak_matrix" and (
                    path.stat().st_size > 5_000_000_000 or adata.n_obs > 200_000 or adata.n_vars > 200_000
                )
                return {
                    "input_kind": input_kind,
                    "matrix": str(path),
                    "reason": reason,
                    "n_obs": int(adata.n_obs),
                    "n_vars": int(adata.n_vars),
                    "safe_packaging_only": False,
                    "recommended_qc_mode": "large_full_qc" if is_large else "standard_full_qc",
                    "size_risk": "large" if is_large else "standard",
                    "safe_mode": "large_full_qc" if is_large else "standard_peak_matrix_qc",
                    "obs_columns": sorted(obs_cols),
                }
            finally:
                adata.file.close()
        except Exception as error:
            return {
                "input_kind": "peak_matrix",
                "matrix": str(path),
                "reason": f"h5ad extension detected; backed inspection failed: {error}",
                "safe_packaging_only": False,
                "recommended_qc_mode": "large_full_qc",
                "size_risk": "large",
                "safe_mode": "large_full_qc",
            }
    if name.endswith(".h5"):
        kind = "multiome" if "multiome" in lower or "multiome" in name else "peak_matrix"
        return {"input_kind": kind, "matrix": str(path), "reason": "h5 matrix file detected from extension/name"}
    if name.endswith((".mtx", ".mtx.gz", ".npz")):
        return {"input_kind": "peak_matrix", "matrix": str(path), "reason": "matrix file detected from extension"}
    return {"input_kind": "unknown", "reason": f"unsupported input path: {path}"}


def direct_analysis_context(config: AgentConfig, prompt: str, path: Path) -> dict:
    detected = detect_existing_input(config, path, prompt)
    files = detection_files(detected)
    fallback_path = path
    if path.is_dir() and path.name.lower() in {
        "fragments", "fragment", "fragments_standardized", "standardized_fragments"
    }:
        fallback_path = path.parent
    fallback_id = fallback_path.stem.replace("-cell_by_peak", "").replace("_cell_by_peak", "")
    dataset_id = dataset_id_from_prompt(prompt, fallback_id)
    metadata = detected.get("metadata") if isinstance(detected.get("metadata"), dict) else {}
    safe_mode = str(detected.get("safe_mode") or "")
    genome = detected.get("genome_build")
    if not genome or genome in {"unknown", "unknown_requires_user_or_prompt"}:
        genome = genome_build_from_prompt(prompt)
    size_risk = str(detected.get("size_risk") or "")
    if size_risk in {"", "unknown"}:
        shape = [metadata.get("n_obs"), metadata.get("n_vars")]
        try:
            n_obs = int(shape[0] or 0)
            n_vars = int(shape[1] or 0)
        except (TypeError, ValueError):
            n_obs = n_vars = 0
        large = (
            safe_mode in {"large_full_qc", "large_h5ad_backed_package"}
            or (path.is_file() and path.stat().st_size > 5_000_000_000)
            or n_obs > 200_000
            or n_vars > 200_000
        )
        size_risk = "large" if large else "standard"
    recommended = str(detected.get("recommended_qc_mode") or "")
    if recommended in {"", "review_required", "unknown"}:
        recommended = "large_full_qc" if size_risk == "large" else "standard_full_qc"
    context = {
        "manifest": files.get("manifest") or detected.get("manifest") or "",
        "rows": [],
        "dataset_ids": [dataset_id],
        "dataset_id": dataset_id,
        "input_kind": detected.get("input_kind", "unknown"),
        "confidence": detected.get("confidence", 0),
        "reason": detected.get("reason", "existing local/server path supplied by user"),
        "fragments": files.get("fragments") or detected.get("fragments") or "",
        "fragment_files": files.get("fragment_files") or [],
        "metadata_files": files.get("metadata_files") or [],
        "input_mode": detected.get("input_mode") or "unknown",
        "sample_count": metadata.get("sample_count") or 0,
        "peaks": files.get("peaks") or detected.get("peaks") or "",
        "matrix": files.get("matrix") or detected.get("matrix") or "",
        "rna": files.get("rna") or detected.get("rna") or "",
        "raw_sra_count": 1 if detected.get("input_kind") in {"raw_reads", "raw_sra"} else 0,
        "results_root": str(config.run_root / "results"),
        "genome_build": genome,
        "source_path": str(path),
        # Packaging is an explicit user choice now; never auto-force it for large inputs.
        "safe_packaging_only": False,
        "analysis_mode": "full_qc",
        "size_risk": size_risk,
        "recommended_qc_mode": recommended,
        "safe_mode": safe_mode or recommended,
        "detector": detected,
        "detected_shape": [metadata.get("n_obs"), metadata.get("n_vars")],
    }
    return context


def direct_analysis_request(text: str, config: AgentConfig | None = None) -> bool:
    if not analysis_intent(text):
        return False
    if extract_existing_analysis_paths(text):
        return True
    if config is not None and extract_alias_from_text(config, text) is not None:
        return True
    return False


def manifest_context(config: AgentConfig, state: AgentState) -> dict:
    manifest = state.last_downloaded_manifest or latest_downloaded_manifest(config.run_root)
    if manifest is None:
        return {"manifest": "", "rows": [], "input_kind": "missing", "reason": "no downloaded manifest"}
    rows = read_manifest_rows(manifest)
    files = []
    for row in rows:
        path = row.get("local_path", "")
        name = Path(path).name.lower()
        role = str(row.get("role", "")).lower()
        artifact = str(row.get("artifact_id", "")).lower()
        file_format = str(row.get("file_format", "")).lower()
        files.append({
            "artifact_id": row.get("artifact_id", ""),
            "dataset_id": row.get("dataset_id", ""),
            "role": row.get("role", ""),
            "file_format": row.get("file_format", ""),
            "local_path": path,
            "size_bytes": row.get("size_bytes", ""),
            "name": name,
            "role_lower": role,
            "artifact_lower": artifact,
            "format_lower": file_format,
        })
    dataset_ids = sorted({str(item["dataset_id"]) for item in files if item.get("dataset_id")})

    def find_file(predicate):
        for item in files:
            if predicate(item):
                return item["local_path"]
        return ""

    fragments = find_file(lambda item: "fragment" in item["role_lower"] or "fragments.tsv" in item["name"])
    peaks = find_file(lambda item: item["role_lower"] in {"peaks", "peak", "peaks_bed"} or item["name"].endswith(('.bed', '.bed.gz')) or "peaks" in item["name"])
    matrix = find_file(lambda item: any(term in item["role_lower"] for term in ["peak_matrix", "matrix", "atac_matrix"]) or item["name"].endswith(('.h5', '.h5ad', '.mtx', '.mtx.gz', '.npz')))
    rna = find_file(lambda item: any(term in item["role_lower"] for term in ["rna", "gene", "expression"]) or "rna" in item["name"] or "gene" in item["name"])
    raw_sra = [item for item in files if item["role_lower"] == "sra_run" or item["name"].endswith(('.sra', '.lite.1')) or item["format_lower"] == "sra"]

    if raw_sra:
        input_kind = "raw_sra"
        reason = "downloaded files are SRA/raw read objects; preprocessing to FASTQ/fragments or peak matrix is required first"
    elif rna and (fragments or matrix):
        input_kind = "multiome"
        reason = "paired RNA and ATAC inputs detected"
    elif fragments:
        input_kind = "fragments"
        reason = "ATAC fragments detected"
    elif matrix:
        input_kind = "peak_matrix"
        reason = "cell x peak matrix detected"
    else:
        input_kind = "unknown"
        reason = "could not identify fragments, peak matrix, multiome, or SRA raw reads"

    return {
        "manifest": str(manifest),
        "rows": files,
        "dataset_ids": dataset_ids,
        "dataset_id": safe_dataset_id(dataset_ids[0] if dataset_ids else "dataset"),
        "input_kind": input_kind,
        "reason": reason,
        "fragments": fragments,
        "peaks": peaks,
        "matrix": matrix,
        "rna": rna,
        "raw_sra_count": len(raw_sra),
        "results_root": str(config.run_root / "results"),
        "genome_build": "GRCh38",
    }


def print_skill_registry() -> None:
    print("可执行 skills/scripts 白名单：")
    for name, spec in SKILL_REGISTRY.items():
        stages = ",".join(spec["allowed_stages"])
        print(f"- {name}: {spec['script']} stages={stages}")


def step_analysis_plan(user_prompt: str, context: dict) -> dict | None:
    if not os.environ.get("STEP_API_KEY"):
        return None
    try:
        from cell_note_agent.step_api import chat_completion, first_message_content

        skills = {
            name: {"description": spec["description"], "allowed_stages": spec["allowed_stages"]}
            for name, spec in SKILL_REGISTRY.items()
        }
        system = (
            "You are CellNoteAgent's safe analysis planner. Return ONLY strict JSON. "
            "You may only choose skills listed in skill_registry. Do not invent scripts or shell commands. "
            "If input_kind is raw_sra, do NOT run QC directly; return action explain with next_step raw_preprocessing_required. "
            "Otherwise return action run_analysis with input_kind, dataset_id, genome_build, optional qc_params, and concise rationale. "
            "Valid input_kind values: fragments, peak_matrix, multiome, raw_sra, unknown."
        )
        user = json.dumps({"user_request": user_prompt, "download_context": context, "skill_registry": skills}, ensure_ascii=False)
        response = chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=1024,
        )
        parsed = _json_from_text(first_message_content(response))
        return parsed if isinstance(parsed, dict) else None
    except Exception as error:
        print(f"[step-analysis-plan] warning: {error}")
        return None


def deterministic_analysis_plan(context: dict) -> dict:
    input_kind = context.get("input_kind", "unknown")
    genome_build = str(
        (context.get("qc_params") or {}).get("genome_build")
        or context.get("genome_build")
        or "GRCh38"
    )
    if input_kind == "raw_sra":
        return {
            "action": "explain",
            "next_step": "raw_preprocessing_required",
            "message": "下载产物是 SRA/raw reads，需要先用 SRA Toolkit + Cell Ranger ATAC/ARC 等流程生成 fragments.tsv.gz 或 peak matrix，再进入 QC。",
        }
    if input_kind == "unknown" or input_kind == "missing":
        return {
            "action": "explain",
            "next_step": "provide_supported_inputs",
            "message": "没有识别到可分析输入。请提供 fragments.tsv.gz、peak matrix(+peaks.bed) 或 multiome RNA+ATAC 文件。",
        }
    fragment_backed = input_kind == "fragments" or (
        input_kind == "multiome" and context.get("fragments") and not context.get("matrix")
    )
    if fragment_backed and genome_build.lower() not in {"grch38", "hg38"}:
        return {
            "action": "explain",
            "next_step": "fragment_liftover_required",
            "message": (
                f"检测到 {genome_build} fragments 输入。当前受控 fragments 路径只对 GRCh38/hg38 "
                "启用 blacklist 与 peak-matrix 生成；请先提供已转换到 GRCh38 的 fragments，"
                "或转人工复核，避免错误套用 hg38 参考资源。"
            ),
        }
    return {
        "action": "run_analysis",
        "input_kind": input_kind,
        "dataset_id": context.get("dataset_id", "dataset"),
        "genome_build": genome_build,
        "qc_params": context.get("qc_params") or {},
        "rationale": context.get("reason", "deterministic route selected from downloaded manifest"),
    }


def canonical_analysis_commands(config: AgentConfig, context: dict, plan: dict) -> list[list[str]]:
    if plan.get("action") != "run_analysis":
        return []
    input_kind = str(plan.get("input_kind") or context.get("input_kind") or "")
    dataset_id = safe_dataset_id(str(plan.get("dataset_id") or context.get("dataset_id") or "dataset"))
    qc_params = plan.get("qc_params") or context.get("qc_params") or {}
    if not isinstance(qc_params, dict):
        qc_params = {}
    genome_build = str(qc_params.get("genome_build") or plan.get("genome_build") or context.get("genome_build") or "GRCh38")
    results_root = Path(context.get("results_root") or config.run_root / "results")
    analysis_mode = str(context.get("analysis_mode") or ("packaging_only" if context.get("safe_packaging_only") else "full_qc"))
    commands: list[list[str]] = []
    reference_dir = results_root / "reference"
    curator_python = pipeline_python(config.processing_python, "cellnote-curator")
    snapatac_python = pipeline_python(config.processing_python, "snapatac2")
    muon_python = pipeline_python(config.processing_python, "muon")

    def _append_reference_setup(*, include_liftover: bool = False) -> None:
        common = [
            curator_python,
            "scripts/prepare_references.py",
            "--out", str(reference_dir),
        ]
        if include_liftover:
            common.append("--include_liftover")
        for stage in SKILL_REGISTRY["resource-setup"]["allowed_stages"]:
            commands.append([*common, "--stage", stage])

    def _append_qc_flags(cmd: list[str], *, peak_matrix: bool = False, multiome: bool = False) -> None:
        if qc_params.get("min_fragments") is not None:
            cmd.extend(["--min_fragments", str(qc_params["min_fragments"])])
        if qc_params.get("min_tsse") is not None:
            cmd.extend(["--min_tsse", str(qc_params["min_tsse"])])
        if qc_params.get("min_peaks") is not None and not multiome:
            cmd.extend(["--min_peaks", str(qc_params["min_peaks"])])
        if peak_matrix and qc_params.get("min_counts") is not None:
            cmd.extend(["--min_counts", str(qc_params["min_counts"])])
        if peak_matrix and qc_params.get("min_cells_per_peak") is not None:
            cmd.extend(["--min_cells_per_peak", str(qc_params["min_cells_per_peak"])])
        if peak_matrix and qc_params.get("skip_embed_cluster"):
            cmd.append("--skip_embed_cluster")
        if peak_matrix and (
            context.get("size_risk") == "large" or context.get("recommended_qc_mode") == "large_full_qc"
        ):
            cmd.append("--backed")
        if multiome and qc_params.get("min_counts") is not None:
            cmd.extend(["--atac_min_counts", str(qc_params["min_counts"])])
        if multiome and qc_params.get("min_peaks") is not None:
            cmd.extend(["--atac_min_peaks", str(qc_params["min_peaks"])])

    if input_kind == "fragments":
        _append_reference_setup()
        common = [
            snapatac_python,
            "scripts/scatac_fragment_qc.py",
            "--fragments", str(context.get("fragments") or ""),
            "--results_root", str(results_root),
            "--dataset_id", dataset_id,
            "--genome_build", genome_build,
            "--blacklist_bed", str(reference_dir / "hg38-blacklist.v2.bed"),
        ]
        if context.get("peaks"):
            common.extend(["--peaks", str(context["peaks"])])
        peak_calling = qc_params.get("peak_calling")
        if peak_calling is not None:
            common.extend(["--peak_calling", str(peak_calling)])
        elif context.get("input_mode") == "collection":
            common.extend(["--peak_calling", "sample"])
        if qc_params.get("import_jobs") is not None:
            common.extend(["--import_jobs", str(qc_params["import_jobs"])])
        _append_qc_flags(common)
        for stage in SKILL_REGISTRY["scatac-fragment-qc"]["allowed_stages"]:
            commands.append([*common, "--stage", stage])
    elif input_kind == "peak_matrix":
        if analysis_mode == "packaging_only":
            common = [
                curator_python,
                "scripts/package_existing_peak_matrix.py",
                "--matrix", str(context.get("matrix") or ""),
                "--results_root", str(results_root),
                "--dataset_id", dataset_id,
                "--genome_build", genome_build,
            ]
            if context.get("peaks"):
                common.extend(["--peaks", str(context["peaks"])])
            for stage in SKILL_REGISTRY["existing-peak-matrix-package"]["allowed_stages"]:
                commands.append([*common, "--stage", stage])
        else:
            if genome_build.lower() in {"hg19", "grch37"}:
                _append_reference_setup(include_liftover=True)
            common = [
                curator_python,
                "scripts/scatac_peak_matrix.py",
                "--matrix", str(context.get("matrix") or ""),
                "--results_root", str(results_root),
                "--dataset_id", dataset_id,
                "--genome_build", genome_build,
            ]
            if context.get("peaks"):
                common.extend(["--peaks", str(context["peaks"])])
            if genome_build.lower() in {"hg19", "grch37"}:
                common.extend(["--liftover_chain", str(reference_dir / "hg19ToHg38.over.chain.gz")])
                if qc_params.get("min_liftover_rate") is not None:
                    common.extend(["--min_liftover_rate", str(qc_params["min_liftover_rate"])])
            _append_qc_flags(common, peak_matrix=True)
            for stage in SKILL_REGISTRY["scatac-peak-matrix"]["allowed_stages"]:
                commands.append([*common, "--stage", stage])
    elif input_kind == "multiome":
        common = [
            snapatac_python if context.get("fragments") else muon_python,
            "scripts/multiome_qc.py",
            "--rna", str(context.get("rna") or ""),
            "--results_root", str(results_root),
            "--dataset_id", dataset_id,
            "--genome_build", genome_build,
        ]
        if context.get("fragments"):
            common.extend(["--atac_fragments", str(context["fragments"])])
        if context.get("matrix"):
            common.extend(["--atac_matrix", str(context["matrix"])])
        if context.get("peaks"):
            common.extend(["--peaks", str(context["peaks"])])
        _append_qc_flags(common, multiome=True)
        for stage in SKILL_REGISTRY["multiome-qc"]["allowed_stages"]:
            commands.append([*common, "--stage", stage])
    else:
        return []

    for stage in SKILL_REGISTRY["handoff-pipeline"]["allowed_stages"]:
        commands.append([config.processing_python, "scripts/package_peak_matrices.py", "--results_root", str(results_root), "--stage", stage])
    return commands


def show_pi_plan(config: AgentConfig, context: dict, plan: dict) -> None:
    input_kind = str(plan.get("input_kind") or context.get("input_kind") or "")
    if input_kind not in {"fragments", "peak_matrix", "multiome"}:
        return
    dataset_id = safe_dataset_id(str(plan.get("dataset_id") or context.get("dataset_id") or "dataset"))
    command = [
        sys.executable,
        "-m",
        "cell_note_agent.pi_bridge",
        "plan-peak-matrix",
        "--input_kind", input_kind,
        "--dataset_id", dataset_id,
        "--results_root", str(context.get("results_root") or config.run_root / "results"),
        "--genome_build", str(plan.get("genome_build") or context.get("genome_build") or "GRCh38"),
    ]
    if input_kind in {"fragments", "peak_matrix", "multiome"}:
        if input_kind == "fragments":
            input_path = context.get("fragments")
        elif input_kind == "peak_matrix":
            input_path = context.get("matrix")
        else:
            input_path = context.get("fragments") or context.get("matrix")
        if input_path:
            command.extend(["--input", str(input_path)])
    if context.get("peaks"):
        command.extend(["--peaks", str(context["peaks"])])
    if context.get("rna"):
        command.extend(["--rna", str(context["rna"])])
    print("\nPi skill plan：")
    run_command(command, cwd=config.repo_root, check=False)


def execute_analysis_plan(config: AgentConfig, context: dict, plan: dict) -> None:
    action = str(plan.get("action") or "").strip()
    if action != "run_analysis":
        print(plan.get("message") or "当前输入暂不能直接进入分析。")
        if plan.get("next_step") == "raw_preprocessing_required":
            print("建议下一步：先把 SRA/raw reads 转成 FASTQ，再用 Cell Ranger ATAC/ARC 或等价流程生成 fragments.tsv.gz / peak matrix。")
        return
    commands = canonical_analysis_commands(config, context, plan)
    if not commands:
        print("没有可执行的白名单分析命令；请检查输入类型。")
        return
    print("\n将执行以下白名单 scripts：")
    for index, command in enumerate(commands, 1):
        print(f"[{index}] " + " ".join(command))
    if not confirm("确认执行分析流程吗？", config.assume_yes):
        print("已取消分析。")
        return
    dataset = str(context.get("dataset_id") or context.get("input_kind") or "analysis")
    job = _sanitize_tmux_session(f"qc-{dataset}")
    result = run_long_commands(commands, config=config, job_name=job)
    if result.get("mode") == "tmux":
        print("分析流程已移交 tmux；完成后可查看 qc_summary / peak_matrix，或继续输入下一条指令。")


def choose_analysis_mode(config: AgentConfig, context: dict) -> str | None:
    """Return full_qc / packaging_only, or None if cancelled."""
    input_kind = str(context.get("input_kind") or "")
    if input_kind != "peak_matrix":
        return "full_qc"
    size_risk = str(context.get("size_risk") or "standard")
    if config.assume_yes or getattr(config, "auto_all", False):
        return "full_qc"

    if size_risk == "large":
        print("\n检测到较大 peak matrix：默认完整 QC（backed/分块），过滤后默认跳过 embed-cluster。")
        print("也可选择仅 packaging（不做阈值过滤）。")
        choice = choose_option(
            "请选择分析模式",
            ["完整 QC（推荐）", "仅 packaging（不做阈值过滤）", "取消"],
            default_index=0,
        )
        if choice == 2:
            return None
        return "full_qc" if choice == 0 else "packaging_only"

    print("\n将默认执行完整 QC（scatac-peak-matrix）。")
    choice = choose_option(
        "请选择分析模式",
        ["完整 QC（推荐）", "仅 packaging（不做阈值过滤）", "取消"],
        default_index=0,
    )
    if choice == 2:
        return None
    return "full_qc" if choice == 0 else "packaging_only"


def collect_qc_preferences(
    config: AgentConfig,
    input_kind: str,
    *,
    current_genome: str | None = None,
    size_risk: str = "standard",
    analysis_mode: str = "full_qc",
) -> dict:
    """Ask the user for QC parameter preferences before running analysis."""
    if analysis_mode == "packaging_only":
        print("\n已选择仅 packaging：不会应用 QC 阈值过滤。")
        return {"genome_build": current_genome or "GRCh38"}
    if config.assume_yes or getattr(config, "auto_all", False):
        params: dict[str, object] = {"genome_build": current_genome or "GRCh38"}
        if input_kind == "peak_matrix":
            params.update({"min_peaks": 1000, "min_counts": 1000, "min_cells_per_peak": 10})
            if size_risk == "large":
                params["skip_embed_cluster"] = True
        elif input_kind in {"fragments", "multiome"}:
            params.update({"min_fragments": 3000, "min_tsse": 6.0})
        return params
    if input_kind not in {"fragments", "peak_matrix", "multiome"}:
        return {}

    print(f"\n分析前 QC 参数设置（输入类型: {input_kind}）:")
    print("将先选择阈值预设（宽松 / 标准 / 严格），再确认执行对应 skill。")

    qc_params: dict[str, object] = {}
    if current_genome in {"GRCh38", "hg38", None, ""}:
        # SOFT_ASK: if already GRCh38-like / unknown, keep default without forcing a menu.
        if current_genome in {"GRCh38", "hg38"}:
            print(f"1. 参考基因组：沿用已检测/默认 {current_genome or 'GRCh38'}（可在自定义流程中再改）")
            qc_params["genome_build"] = "GRCh38"
        else:
            genome_build = choose_option_with_other(
                "1. 参考基因组版本",
                ["GRCh38 (推荐)", "GRCh37/hg19", "mm10"],
                default_index=0,
            )
            genome_map = {"GRCh38 (推荐)": "GRCh38", "GRCh37/hg19": "GRCh37", "mm10": "mm10"}
            qc_params["genome_build"] = genome_map.get(genome_build, "GRCh38")
    else:
        genome_options = ["GRCh38 (推荐)", "GRCh37/hg19", "mm10"]
        genome_default = 1 if current_genome in {"GRCh37", "hg19"} else 2 if current_genome == "mm10" else 0
        genome_build = choose_option_with_other(
            "1. 参考基因组版本",
            genome_options,
            default_index=genome_default,
        )
        genome_map = {"GRCh38 (推荐)": "GRCh38", "GRCh37/hg19": "GRCh37", "mm10": "mm10"}
        qc_params["genome_build"] = genome_map.get(genome_build, current_genome or "GRCh38")

    if input_kind in {"fragments", "multiome"}:
        min_frag = choose_option_with_other(
            "2. 最小 fragments 数阈值（低于此值的细胞被过滤）",
            ["1000 (宽松)", "3000 (标准)", "5000 (严格)"],
            default_index=1,
        )
        frag_map = {"1000 (宽松)": 1000, "3000 (标准)": 3000, "5000 (严格)": 5000}
        qc_params["min_fragments"] = frag_map.get(min_frag, 3000)

        min_tsse = choose_option_with_other(
            "3. 最小 TSS 富集分数阈值",
            ["4.0 (宽松)", "6.0 (标准)", "8.0 (严格)"],
            default_index=1,
        )
        tsse_map = {"4.0 (宽松)": 4.0, "6.0 (标准)": 6.0, "8.0 (严格)": 8.0}
        qc_params["min_tsse"] = tsse_map.get(min_tsse, 6.0)

    if input_kind == "peak_matrix":
        print("阈值档位会同步设置 min_peaks / min_counts / min_cells_per_peak。")
        if size_risk == "large":
            print("证据：当前矩阵规模较大；标准档通常可保留大部分细胞，严格档更激进。")
        tier = choose_option(
            "2. QC 阈值档位（ASK_WITH_EVIDENCE）",
            [
                "宽松（min_peaks/counts=500, cells_per_peak=5）",
                "标准（1000/1000/10，推荐）",
                "严格（2000/2000/20）",
                "自定义各项阈值",
            ],
            default_index=1,
        )
        if tier == 0:
            qc_params.update({"min_peaks": 500, "min_counts": 500, "min_cells_per_peak": 5})
        elif tier == 2:
            qc_params.update({"min_peaks": 2000, "min_counts": 2000, "min_cells_per_peak": 20})
        elif tier == 3:
            min_peaks = choose_option_with_other(
                "最小检测 peaks 数阈值",
                ["500 (宽松)", "1000 (标准)", "2000 (严格)"],
                default_index=1,
            )
            peaks_map = {"500 (宽松)": 500, "1000 (标准)": 1000, "2000 (严格)": 2000}
            qc_params["min_peaks"] = peaks_map.get(min_peaks, 1000)
            min_counts = choose_option_with_other(
                "最小 total counts 阈值",
                ["500 (宽松)", "1000 (标准)", "2000 (严格)"],
                default_index=1,
            )
            counts_map = {"500 (宽松)": 500, "1000 (标准)": 1000, "2000 (严格)": 2000}
            qc_params["min_counts"] = counts_map.get(min_counts, 1000)
            min_cells_per_peak = choose_option_with_other(
                "最小 cells-per-peak 阈值（过滤稀有 peak）",
                ["5 (宽松)", "10 (标准)", "20 (严格)"],
                default_index=1,
            )
            cpp_map = {"5 (宽松)": 5, "10 (标准)": 10, "20 (严格)": 20}
            qc_params["min_cells_per_peak"] = cpp_map.get(min_cells_per_peak, 10)
        else:
            qc_params.update({"min_peaks": 1000, "min_counts": 1000, "min_cells_per_peak": 10})

        # Large/ultra matrices: skip embed-cluster by default after filter.
        if size_risk == "large":
            embed_choice = choose_option(
                "3. 过滤后是否运行 embed-cluster（大矩阵默认跳过）",
                ["跳过 embed-cluster（推荐）", "强制运行 embed-cluster"],
                default_index=0,
            )
            qc_params["skip_embed_cluster"] = embed_choice == 0
        else:
            qc_params["skip_embed_cluster"] = False

    print("\nQC 参数汇总：")
    for key, value in qc_params.items():
        print(f"- {key}: {value}")
    return qc_params


def planned_skill_names(context: dict) -> list[str]:
    input_kind = str(context.get("input_kind") or "unknown")
    analysis_mode = str(context.get("analysis_mode") or ("packaging_only" if context.get("safe_packaging_only") else "full_qc"))
    if input_kind == "fragments":
        return ["scatac-fragment-qc", "handoff-pipeline"]
    if input_kind == "peak_matrix":
        if analysis_mode == "packaging_only":
            return ["existing-peak-matrix-package", "handoff-pipeline"]
        return ["scatac-peak-matrix", "handoff-pipeline"]
    if input_kind == "multiome":
        return ["multiome-qc", "handoff-pipeline"]
    return []


def planned_skill_summary(context: dict) -> str:
    skills = planned_skill_names(context)
    if skills:
        return " -> ".join(skills)
    input_kind = str(context.get("input_kind") or "unknown")
    if input_kind in {"raw_sra", "raw_reads"}:
        return "raw preprocessing required (no direct QC)"
    return "unsupported / needs user clarification"


def planned_stages_by_skill(context: dict) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    qc_params = context.get("qc_params") if isinstance(context.get("qc_params"), dict) else {}
    for skill_name in planned_skill_names(context):
        spec = SKILL_REGISTRY.get(skill_name)
        if not spec:
            continue
        stages = list(spec.get("allowed_stages") or [])
        if skill_name == "scatac-peak-matrix" and qc_params.get("skip_embed_cluster"):
            stages = [
                (f"{stage} (skip work)" if stage == "embed-cluster" else stage)
                for stage in stages
            ]
        rows.append((skill_name, stages))
    return rows


def print_planned_pipeline(context: dict) -> None:
    print("\n拟执行分析流程：")
    print(f"- input_kind: {context.get('input_kind')}")
    print(f"- dataset_id: {context.get('dataset_id', '-')}")
    print(f"- genome_build: {context.get('genome_build', '-')}")
    print(f"- size_risk: {context.get('size_risk', '-')}")
    print(f"- analysis_mode: {context.get('analysis_mode', 'full_qc')}")
    print(f"- recommended_qc_mode: {context.get('recommended_qc_mode', '-')}")
    print(f"- skills: {planned_skill_summary(context)}")
    stage_rows = planned_stages_by_skill(context)
    if not stage_rows:
        print("- stages: (无可用白名单 stages)")
        return
    print("- stages:")
    for skill_name, stages in stage_rows:
        print(f"  • {skill_name}: {', '.join(stages)}")
    if context.get("analysis_mode") == "packaging_only":
        print("- 注意: packaging-only 不会应用 min_peaks/min_counts 等阈值过滤")
    elif context.get("size_risk") == "large":
        print("- 注意: 大矩阵将使用 backed/分块完整 QC；过滤后默认跳过 embed-cluster")


def enrich_action_with_local_path(action: dict | None, text: str, config: AgentConfig | None = None) -> dict | None:
    """Ensure API-routed actions still carry a local input_path when the user provided one."""
    if not action:
        return None
    resolved = None
    if config is not None:
        resolved = resolve_analysis_input_path(config, text, str(action.get("input_path") or ""))
    if resolved is None:
        paths = extract_existing_analysis_paths(text)
        resolved = paths[0] if paths else None
    if resolved is None:
        return action
    input_path = str(resolved)
    current_path = str(action.get("input_path") or "").strip()
    name = str(action.get("action") or "").strip()
    if direct_analysis_request(text, config) or analysis_intent(text):
        if name in {"", "explain", "run_analysis"} or direct_analysis_request(text, config):
            merged = dict(action)
            merged["action"] = "run_analysis"
            merged["query"] = merged.get("query") or text
            if not current_path:
                merged["input_path"] = input_path
            return merged
    if name == "run_analysis" and not current_path:
        merged = dict(action)
        merged["input_path"] = input_path
        return merged
    return action


def run_analysis_interactively(config: AgentConfig, state: AgentState, user_prompt: str, explicit_context: dict | None = None) -> None:
    context = explicit_context or manifest_context(config, state)
    input_kind = str(context.get("input_kind") or "unknown")

    # If analysis was requested without path/alias and no usable downloaded input, ask interactively.
    if explicit_context is None and input_kind in {"missing", "unknown"}:
        prompted = prompt_for_existing_input(config)
        if prompted is None:
            print("已取消：需要本地路径或数据集别名。")
            return
        context = direct_analysis_context(config, user_prompt, prompted)
        input_kind = str(context.get("input_kind") or "unknown")

    print("\n输入识别：")
    print(f"- source_path: {context.get('source_path') or '-'}")
    print(f"- manifest: {context.get('manifest') or '-'}")
    print(f"- input_kind: {context.get('input_kind')}")
    print(f"- confidence: {context.get('confidence', '-')}")
    print(f"- size_risk: {context.get('size_risk', '-')}")
    print(f"- recommended_qc_mode: {context.get('recommended_qc_mode', '-')}")
    print(f"- safe_mode: {context.get('safe_mode', '-')}")
    print(f"- reason: {context.get('reason')}")
    print(f"- dataset_id: {context.get('dataset_id', '-')}")
    if context.get("detected_shape"):
        print(f"- detected_shape: {context.get('detected_shape')}")

    if input_kind not in {"fragments", "peak_matrix", "multiome"}:
        print_planned_pipeline(context)
        plan = deterministic_analysis_plan(context)
        print("\n当前输入暂不能直接进入 QC：")
        print(plan.get("message") or plan)
        return

    analysis_mode = choose_analysis_mode(config, context)
    if analysis_mode is None:
        print("已取消分析。")
        return
    context["analysis_mode"] = analysis_mode
    context["safe_packaging_only"] = analysis_mode == "packaging_only"

    print_planned_pipeline(context)
    if not confirm("确认按上述 skill/stages 继续（下一步选择 QC 阈值）吗？", config.assume_yes):
        print("已取消分析。")
        return

    qc_params = collect_qc_preferences(
        config,
        input_kind,
        current_genome=str(context.get("genome_build") or ""),
        size_risk=str(context.get("size_risk") or "standard"),
        analysis_mode=analysis_mode,
    )
    if qc_params:
        context["qc_params"] = qc_params
        if qc_params.get("genome_build"):
            context["genome_build"] = qc_params["genome_build"]

    # Refresh stage preview after QC prefs (e.g. skip embed-cluster).
    print_planned_pipeline(context)

    plan = step_analysis_plan(user_prompt, context) or deterministic_analysis_plan(context)
    if isinstance(plan, dict) and context.get("qc_params"):
        merged_qc = dict(plan.get("qc_params") or {})
        merged_qc.update(context["qc_params"])
        plan["qc_params"] = merged_qc
        plan["genome_build"] = merged_qc.get("genome_build") or plan.get("genome_build") or context.get("genome_build")
    print("\n阶跃/本地 planner 输出：")
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    if analysis_mode != "packaging_only":
        show_pi_plan(config, context, plan)
    execute_analysis_plan(config, context, plan)

    # Post-run honesty check for peak-matrix QC.
    if input_kind == "peak_matrix" and analysis_mode == "full_qc":
        summary_path = Path(context.get("results_root") or config.run_root / "results") / "processed" / safe_dataset_id(str(context.get("dataset_id") or "dataset")) / "qc_summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                summary = {}
            print("\nQC 结果核对：")
            print(f"- qc_mode: {summary.get('qc_mode', '-')}")
            print(f"- cells: {summary.get('n_cells_loaded', '-')} -> {summary.get('n_cells_pass_filter', '-')}")
            print(f"- peaks: {summary.get('n_peaks_loaded', '-')} -> {summary.get('n_peaks_pass_filter', '-')}")
            print(f"- filter_thresholds: {summary.get('filter_thresholds', {})}")
            if summary.get("embed_cluster_skipped"):
                print(f"- embed-cluster: skipped ({summary.get('embed_cluster_skip_reason', '')})")
            if not summary.get("filter_thresholds"):
                print("- 警告: 未找到 filter_thresholds，可能未真正执行阈值过滤")
        else:
            print(f"\n警告: 未找到 qc_summary: {summary_path}")

def run_pbmc500_demo(config: AgentConfig) -> None:
    print("我将运行真实 10x PBMC500 scATAC peak matrix demo：下载、校验、QC、标准化、打包。")
    print(f"输出目录：{config.run_root}")
    if not confirm("确认开始真实下载和处理吗？", config.assume_yes):
        print("已取消。")
        return

    manifest = write_pbmc500_manifest(config.run_root)
    raw_store = config.run_root / "raw"
    results = config.run_root / "results"
    matrix = raw_store / "pbmc500_agent_demo" / "atac_pbmc_500_nextgem_filtered_peak_bc_matrix.h5"
    peaks = raw_store / "pbmc500_agent_demo" / "atac_pbmc_500_nextgem_peaks.bed"

    commands: list[list[str]] = []
    for stage in ["plan", "fetch", "verify"]:
        command = [
            "./cell-note",
            "download",
            "--stage",
            stage,
            "--manifest",
            str(manifest),
            "--store",
            str(raw_store),
        ]
        if stage == "fetch":
            command.append("--enable_fetch")
        commands.append(command)

    common = [
        "--matrix",
        str(matrix),
        "--peaks",
        str(peaks),
        "--results_root",
        str(results),
        "--dataset_id",
        "pbmc500_agent_demo",
        "--genome_build",
        "GRCh38",
    ]
    for stage in ["load", "profile", "filter", "standardize", "embed-cluster", "finalize"]:
        commands.append(
            [config.processing_python, "scripts/scatac_peak_matrix.py", "--stage", stage, *common]
        )
    for stage in ["cards", "validate", "package"]:
        commands.append(
            [
                config.processing_python,
                "scripts/package_peak_matrices.py",
                "--stage",
                stage,
                "--results_root",
                str(results),
            ]
        )
    result = run_long_commands(commands, config=config, job_name="pbmc500-demo")
    if result.get("mode") == "tmux":
        print("PBMC500 demo 已在 tmux 中运行；完成后检查：")
    else:
        print("\n完成：")
    print(f"- manifest: {results / 'corpus' / 'MANIFEST.json'}")
    print(f"- peak matrix: {results / 'processed' / 'pbmc500_agent_demo' / 'peak_matrix.h5ad'}")
    print(f"- data card: {results / 'processed' / 'pbmc500_agent_demo' / 'data_card.json'}")


def run_download_manifest(config: AgentConfig, manifest: str, state: AgentState | None = None) -> None:
    manifest_path = Path(manifest).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = (config.repo_root / manifest_path).resolve()
    raw_store = config.run_root / "raw"
    row_count = csv_data_row_count(manifest_path)
    print(f"检测到 manifest：{manifest_path}")
    print(f"manifest entries：{row_count}")
    print(f"下载目录：{raw_store}")
    if row_count == 0:
        print("这个 manifest 没有可下载条目，因此不会执行 fetch。")
        print("建议：换一个更具体的数据集 accession / URL，或重新搜索并包含 SRA source。")
        return

    plan_command = [
        "./cell-note",
        "download",
        "--stage",
        "plan",
        "--manifest",
        str(manifest_path),
        "--store",
        str(raw_store),
    ]
    print("先展示下载计划和预计大小，不会立即下载。")
    run_command(plan_command, cwd=config.repo_root)

    if not confirm("确认开始 fetch 下载真实数据吗？", config.assume_yes):
        print("已取消 fetch；你仍可稍后输入：下载刚才的 manifest。")
        return

    fetch_command = [
        "./cell-note",
        "download",
        "--stage",
        "fetch",
        "--manifest",
        str(manifest_path),
        "--store",
        str(raw_store),
        "--enable_fetch",
    ]
    verify_command = [
        "./cell-note",
        "download",
        "--stage",
        "verify",
        "--manifest",
        str(manifest_path),
        "--store",
        str(raw_store),
    ]
    result = run_long_commands(
        [fetch_command, verify_command],
        config=config,
        job_name="download-fetch",
    )
    downloaded = raw_store / "downloaded_file_manifest.csv"
    if result.get("mode") == "tmux":
        if state is not None:
            # Manifest path is known even before fetch finishes; analysis waits for user.
            state.last_downloaded_manifest = downloaded
        print("下载/校验已在 tmux 运行。完成后可输入：开始分析。")
        print(f"完成后可检查：{downloaded}")
        return
    if state is not None and downloaded.exists() and csv_data_row_count(downloaded) > 0:
        state.last_downloaded_manifest = downloaded
        if confirm("下载和校验完成。是否进入分析流程？", config.assume_yes):
            run_analysis_interactively(config, state, "下载完成后继续分析")
        else:
            print("已停在下载完成节点；之后可输入：开始分析。")

def run_crawl(config: AgentConfig, state: AgentState, *, query: str, sources: list[str] | None = None, limit: int | None = None, original_query: str | None = None) -> None:
    preference_query = original_query or query
    prefs = collect_search_preferences(config, preference_query)
    effective_query = query_from_preferences(query, prefs) if prefs else query
    search_plan = build_search_plan(preference_query, prefs)
    run_id = "agent-crawl-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    crawl_dir = config.run_root / "crawls" / run_id
    selected_sources = sources or ["geo", "sra", "literature"]
    if prefs:
        optimized_query = effective_query
        _, search_profile = search_profile_query(effective_query)
        search_profile["optimized_query"] = optimized_query
        search_profile["user_preferences"] = prefs
    else:
        optimized_query, search_profile = search_profile_query(effective_query)
    core_queries = list(dict.fromkeys([optimized_query, *search_plan.core_queries]))
    print("我将开始 crawler 搜集数据集候选。")
    print(f"- user query: {query}")
    if prefs:
        print(f"- refined query: {effective_query}")
    print(f"- optimized query: {optimized_query}")
    print(f"- target species: {search_profile.get('species', 'unknown')}")
    print(f"- target genome build: {search_profile.get('target_genome_build', 'unknown')}")
    print(f"- sources: {', '.join(selected_sources)}")
    print(f"- query variants: {len(core_queries)} core / {len(search_plan.external_queries)} extended")
    print(f"- retrieval budget per source: {search_plan.retrieval_limit_per_source}")
    print(f"- output: {crawl_dir}")
    if not confirm("确认开始搜索吗？", config.assume_yes):
        print("已取消。")
        return
    query_runs: list[Path] = []
    for index, search_query in enumerate(core_queries, 1):
        target_dir = crawl_dir if index == 1 else crawl_dir / "query_runs" / f"q{index:02d}"
        target_run_id = run_id if index == 1 else f"{run_id}-q{index:02d}"
        command = ["./cell-note", "--config", "configs/mvp.json", "crawl", "--query", search_query,
                   "--out", str(target_dir), "--run-id", target_run_id, "--resolve-ena-runs",
                   "--limit", str(search_plan.retrieval_limit_per_source)]
        for source in selected_sources:
            command.extend(["--source", source])
        print(f"\n[search query {index}/{len(core_queries)}] {search_query}")
        completed = run_command(command, cwd=config.repo_root, check=False)
        if completed.returncode != 0 and not (target_dir / "crawl_manifest.json").exists():
            print(f"[crawl warning] query shard failed with code {completed.returncode}; continuing other queries.")
        if index > 1 and target_dir.exists():
            query_runs.append(target_dir)
    crawl_dir.mkdir(parents=True, exist_ok=True)
    merge_crawl_runs(crawl_dir, query_runs)
    external_summary = run_external_crawlers(
        crawl_dir,
        optimized_query,
        search_plan.retrieval_limit_per_source,
        queries=search_plan.external_queries,
    )
    search_profile["search_plan"] = search_plan.as_dict()
    search_profile["external_discovery"] = external_summary
    (crawl_dir / "search_profile.json").write_text(json.dumps(search_profile, indent=2, ensure_ascii=False), encoding="utf-8")
    state.last_crawl_run = crawl_dir
    state.last_search_profile = search_profile
    catalog = build_candidate_catalog(config, state, crawl_dir)
    print("\n搜索完成。")
    if catalog:
        print_candidate_landscape(catalog, prefs, crawl_dir)
        print("检索结果已保留。你可以继续提出筛选、下载或分析要求。")
    else:
        print("未生成候选清单。你可以输入：查看 crawl 状态。")

def run_promote(config: AgentConfig, state: AgentState, crawl_run: str | None = None) -> None:
    source = Path(crawl_run).expanduser() if crawl_run else state.last_crawl_run
    if source is None:
        print("还没有 crawl run。请先说：帮我搜集人类 scATAC 数据集。")
        return
    if not source.is_absolute():
        source = (config.repo_root / source).resolve()
    promote_dir = config.run_root / "promoted"
    run_id = "agent-promote-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    command = [
        "./cell-note",
        "--config",
        "configs/mvp.json",
        "promote",
        "--crawl-run",
        str(source),
        "--out",
        str(promote_dir),
        "--run-id",
        run_id,
    ]
    print("我将把 crawler 结果提升为 curation/download manifest。")
    print(f"- crawl_run: {source}")
    print(f"- output: {promote_dir}")
    if not confirm("确认生成下载清单吗？", config.assume_yes):
        print("已取消。")
        return
    run_command(command, cwd=config.repo_root)
    manifest = promote_dir / "file_manifest.csv"
    state.last_promote_run = promote_dir
    if manifest.exists():
        row_count = csv_data_row_count(manifest)
        print(f"\n已生成 manifest：{manifest}")
        print(f"manifest entries：{row_count}")
        if row_count > 0:
            state.last_manifest = manifest
            print("如果要下载，输入：下载刚才的 manifest")
        else:
            state.last_manifest = None
            print("但这个 manifest 为空：当前 crawl 没找到可下载远程文件。")
            print("建议输入更具体的 accession，例如 GSE/SRP/PRJNA，或提供 direct file_manifest.csv。")
    else:
        print(f"\n未找到 file_manifest.csv，请检查 promote 输出：{promote_dir}")


def run_last_download(config: AgentConfig, state: AgentState) -> None:
    if state.last_manifest is None:
        print("还没有可下载的 manifest。请先搜索并生成下载清单，或提供 file_manifest.csv 路径。")
        return
    run_download_manifest(config, str(state.last_manifest), state)


def print_crawl_status(config: AgentConfig, state: AgentState) -> None:
    if state.last_crawl_run is None:
        print("还没有 crawl run。")
        return
    run_command(["./cell-note", "crawl-status", "--run", str(state.last_crawl_run)], cwd=config.repo_root)


def step_plan(prompt: str, repo_root: Path) -> str | None:
    if not os.environ.get("STEP_API_KEY"):
        return None
    from cell_note_agent.pi_bridge import discover_skills
    from cell_note_agent.step_api import plan_with_skills

    return plan_with_skills(prompt, discover_skills(str(repo_root / "skills")))


def _json_from_text(text: str) -> dict | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


def step_action(prompt: str, config: AgentConfig, state: AgentState) -> dict | None:
    if not os.environ.get("STEP_API_KEY"):
        return None
    from cell_note_agent.step_api import chat_completion, first_message_content

    state_text = {
        "last_crawl_run": str(state.last_crawl_run) if state.last_crawl_run else "",
        "last_promote_run": str(state.last_promote_run) if state.last_promote_run else "",
        "last_manifest": str(state.last_manifest) if state.last_manifest else "",
        "run_root": str(config.run_root),
    }
    system = (
        "You are CellNoteAgent's intent router. Return ONLY strict JSON, no markdown. "
        "Never return shell commands. Choose one action from: "
        "crawl, promote, generate_manifest, list_candidates, select_candidates, download_manifest, run_analysis, list_skills, pbmc500_demo, crawl_status, external_tools, explain. "
        "Use crawl for requests like searching/collecting datasets. "
        "Use generate_manifest for making a reviewed download manifest from current candidates. "
        "Use promote only for explicit curation/promote pipeline requests. "
        "Use list_candidates when the user asks to show/list candidates. "
        "Use select_candidates when the user chooses candidate numbers. "
        "Use download_manifest only when the user provided a manifest path or asks to download the last manifest. "
        "Use pbmc500_demo only for explicit 10x/PBMC500 demo requests. "
        "Use run_analysis with input_path when the user provides an existing local/server file or directory "
        "and asks for QC/analysis/processing (for example fragments.tsv.gz, peak matrix h5/h5ad, or multiome inputs). "
        "Prefer run_analysis over crawl when an absolute existing path is present in the user message. "
        "Schema: {\"action\":\"crawl|promote|generate_manifest|download_manifest|run_analysis|list_skills|pbmc500_demo|crawl_status|external_tools|explain\","
        "\"query\":\"\", \"sources\":[\"geo\",\"sra\",\"literature\"], \"limit\":null, "
        "\"manifest\":\"\", \"crawl_run\":\"\", \"candidate_ids\":[1], \"input_path\":\"\", \"message\":\"\"}. "
        "Use limit only when the user explicitly asks for a number. If the user asks for all/everything, set limit to 500. "
        "Keep downloads gated by confirmation; the local agent handles execution."
    )
    user = f"State:\n{json.dumps(state_text, ensure_ascii=False)}\n\nUser:\n{prompt}"
    response = chat_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=512,
    )
    return _json_from_text(first_message_content(response))

def parse_requested_limit(text: str) -> int | None:
    lower = text.lower()
    if any(word in lower for word in ["所有", "全部", "all", "everything"]):
        return 500
    match = re.search(r"(?:搜集|搜索|寻找|找|collect|search|find)?\s*(\d{1,3})\s*(?:个|条|份|个数据集|datasets?|dataset)", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return max(1, min(int(match.group(1)), 500))
    except ValueError:
        return None


def deterministic_action(text: str, state: AgentState, config: AgentConfig | None = None) -> dict | None:
    lower = text.lower()
    resolved = resolve_analysis_input_path(config, text) if config is not None else None
    if resolved is None:
        paths = extract_existing_analysis_paths(text)
        resolved = paths[0] if paths else None
    if resolved is not None and analysis_intent(text):
        return {"action": "run_analysis", "query": text, "input_path": str(resolved)}
    # "已有数据/本地数据 + QC/分析" without an explicit path: enter run_analysis and prompt later.
    if any(term in lower for term in ["已有", "本地数据", "已有input", "已有输入"]) and analysis_intent(text):
        return {"action": "run_analysis", "query": text}
    manifest_match = re.search(r"(\S*file_manifest(?:_v\d+)?\.csv)", text)
    if manifest_match:
        return {"action": "download_manifest", "manifest": manifest_match.group(1)}
    wants_demo = any(keyword in lower for keyword in ["demo", "pbmc500", "pbmc 500", "演示"])
    wants_peak_matrix = any(keyword in lower for keyword in ["scatac", "peak matrix", "peak_matrix", "atac", "下载", "处理"])
    if wants_demo and wants_peak_matrix:
        return {"action": "pbmc500_demo"}
    if ("外部" in lower and ("crawler" in lower or "工具" in lower)) or "external tools" in lower:
        return {"action": "external_tools"}
    if any(keyword in lower for keyword in ["crawl status", "查看 crawl", "查看搜索", "搜索状态"]):
        return {"action": "crawl_status"}
    if any(keyword in lower for keyword in ["skill", "skills", "白名单", "可用脚本", "可调用"]):
        return {"action": "list_skills"}
    if any(keyword in lower for keyword in ["开始分析", "运行分析", "继续分析", "执行分析", "分析", "run analysis", "analyze", "qc"]):
        return {"action": "run_analysis", "query": text}
    if any(keyword in lower for keyword in ["列出候选", "显示候选", "候选数据", "candidate"]):
        return {"action": "list_candidates"}
    candidate_ids = parse_selection(text)
    if candidate_ids:
        return {"action": "select_candidates", "candidate_ids": candidate_ids, "download": False}
    if any(keyword in lower for keyword in ["生成下载清单", "生成 manifest"]) and state.last_candidate_catalog:
        return {"action": "generate_manifest"}
    if any(keyword in lower for keyword in ["生成下载清单", "生成 manifest", "promote", "curation"]):
        return {"action": "promote"}
    if any(keyword in lower for keyword in ["下载刚才", "下载上一步", "下载 manifest", "下载清单"]) and state.last_manifest:
        return {"action": "download_manifest", "manifest": str(state.last_manifest)}
    if any(keyword in lower for keyword in ["搜集", "搜索", "寻找", "找", "discover", "search"]) and any(
        keyword in lower for keyword in ["dataset", "数据集", "scatac", "multiome", "atac"]
    ):
        limit = parse_requested_limit(text)
        return {"action": "crawl", "query": text, "sources": ["geo", "sra", "literature"], "limit": limit}
    return None

def execute_action(action: dict, config: AgentConfig, state: AgentState, original_prompt: str) -> None:
    name = str(action.get("action", "")).strip()
    if name == "external_tools":
        run_command(["./cell-note", "external-tools", "check"], cwd=config.repo_root, check=False)
        return
    if name == "crawl":
        query = str(action.get("query") or original_prompt).strip()
        sources = action.get("sources")
        if not isinstance(sources, list):
            sources = ["geo", "literature"]
        safe_sources = [str(item) for item in sources if str(item) in {"geo", "sra", "literature", "web", "accession"}]
        raw_limit = action.get("limit")
        if raw_limit in {None, "", "null", "None"}:
            limit_int = parse_requested_limit(original_prompt)
        else:
            try:
                limit_int = max(1, min(int(raw_limit), 500))
            except (TypeError, ValueError):
                limit_int = None
        run_crawl(config, state, query=query, sources=safe_sources or ["geo", "literature"], limit=limit_int, original_query=original_prompt)
        return
    if name == "generate_manifest":
        manifest = generate_manifest_from_catalog(config, state)
        if manifest:
            review_manifest_interactively(config, state, manifest)
        return
    if name == "promote":
        if state.last_candidate_catalog and not str(action.get("crawl_run") or "").strip():
            manifest = generate_manifest_from_catalog(config, state)
            if manifest:
                review_manifest_interactively(config, state, manifest)
            return
        crawl_run = str(action.get("crawl_run") or "").strip() or None
        run_promote(config, state, crawl_run)
        return
    if name == "list_candidates":
        catalog = state.last_candidate_catalog or build_candidate_catalog(config, state)
        if catalog:
            print_candidate_catalog(catalog)
        return
    if name == "select_candidates":
        candidate_ids = action.get("candidate_ids") or parse_selection(original_prompt)
        if not isinstance(candidate_ids, list):
            candidate_ids = []
        parsed_ids = []
        for item in candidate_ids:
            try:
                parsed_ids.append(int(item))
            except (TypeError, ValueError):
                pass
        manifest = create_manifest_from_selection(config, state, parsed_ids)
        if manifest:
            review_manifest_interactively(config, state, manifest)
        return
    if name == "download_manifest":
        manifest = str(action.get("manifest") or "").strip()
        if manifest:
            run_download_manifest(config, manifest, state)
        else:
            run_last_download(config, state)
        return
    if name == "run_analysis":
        input_path = str(action.get("input_path") or "").strip()
        explicit_context = None
        resolved = resolve_analysis_input_path(config, original_prompt, input_path)
        if resolved is not None:
            print(f"已解析分析输入：{resolved}")
            explicit_context = direct_analysis_context(config, original_prompt, resolved)
        else:
            if input_path:
                print(f"输入路径不存在或无法解析：{input_path}")
            preview = manifest_context(config, state)
            # Never silently reuse an old downloaded manifest when the user pointed at a local path/alias.
            if intended_local_input_request(original_prompt) or preview.get("input_kind") in {"missing", "unknown"}:
                if intended_local_input_request(original_prompt) and preview.get("input_kind") not in {"missing", "unknown", ""}:
                    print("未能从你的输入解析到有效本地路径/别名；不会使用上次下载的 manifest。")
                prompted = prompt_for_existing_input(config)
                if prompted is None:
                    print("已取消：需要本地路径或数据集别名。")
                    return
                explicit_context = direct_analysis_context(config, original_prompt, prompted)
        run_analysis_interactively(config, state, str(action.get("query") or original_prompt), explicit_context)
        return
    if name == "list_skills":
        print_skill_registry()
        return
    if name == "pbmc500_demo":
        run_pbmc500_demo(config)
        return
    if name == "crawl_status":
        print_crawl_status(config, state)
        return
    print(action.get("message") or "我理解了，但当前还不能自动执行这个请求。")
    if os.environ.get("STEP_API_KEY"):
        plan = step_plan(original_prompt, config.repo_root)
        if plan:
            print("\n参考计划：")
            print(plan)

def handle_prompt(prompt: str, config: AgentConfig, state: AgentState) -> bool:
    text = prompt.strip()
    lower = text.lower()
    if not text:
        return True
    if lower in {"exit", "quit", "q", "退出"}:
        return False
    if lower in {"help", "帮助", "?"}:
        print_help()
        return True

    if direct_analysis_request(text, config) or (
        analysis_intent(text) and any(term in text.lower() for term in ["已有", "本地数据", "已有input", "已有输入"])
    ):
        action = deterministic_action(text, state, config) or step_action(text, config, state)
    else:
        action = step_action(text, config, state) or deterministic_action(text, state, config)
    action = enrich_action_with_local_path(action, text, config)
    if action:
        execute_action(action, config, state, text)
        return True

    plan = step_plan(text, config.repo_root)
    if plan:
        print(plan)
        print("\n我已经给出计划。若要我直接执行，可以说：搜集数据集、生成下载清单、下载刚才的 manifest。")
    else:
        print("我现在能直接执行这些任务：")
        print("1. `运行 10x PBMC500 scATAC demo`")
        print("2. `下载 /path/to/file_manifest.csv`")
        print("3. `帮我搜集人类 scATAC 数据集`")
        print("4. `列出候选` / `生成下载清单` / `选择 1,3 生成下载清单`")
        print("5. `生成下载清单` / `下载刚才的 manifest`")
        print("若要更自由的自然语言理解，请先 export STEP_API_KEY。")
    return True


def print_help() -> None:
    print(
        """
常用自然语言例子：
- 帮我搜集人类外周血 PBMC 的公开 scATAC-seq（优先处理后矩阵/fragments，不要 FASTQ）
- 列出候选 / 生成下载清单 / 选择 1,3 生成下载清单
- 下载刚才的 manifest
- 用本地 peak matrix 做 QC（可说路径或别名如 Li2023a）
- 跑 PBMC500 demo

智能交互（interaction-gates）：
- 搜索前：从描述推断 modality/组织/获取形式，只补问缺失项
- 搜索后：按 best_file_role / pipeline_fit 分诊（auto / 烟雾测试 / 手动）
- QC 前：阈值档位化（宽松/标准/严格），大矩阵默认跳过 embed-cluster
- 大下载与覆盖写：仍需明确确认

其他：
- help
- exit
"""
    )



def run_agent(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().resolve()
    config = AgentConfig(
        repo_root=repo_root,
        run_root=Path(args.run_root).expanduser().resolve(),
        processing_python=args.processing_python or default_processing_python(),
        assume_yes=args.yes,
        use_tmux=not getattr(args, "no_tmux", False),
    )
    state = initialize_state(config)
    if args.once:
        return 0 if handle_prompt(args.once, config, state) else 0

    print("CellNoteAgent 已启动。输入 help 查看例子，输入 exit 退出。")
    if config.use_tmux and tmux_available():
        print("长任务模式：tmux（可用 --no-tmux 关闭）")
    elif config.use_tmux:
        print("长任务模式：前台（未检测到 tmux）")
    else:
        print("长任务模式：前台（--no-tmux）")
    if os.environ.get("STEP_API_KEY"):
        print("阶跃 API：已启用，用于自然语言意图解析。")
    else:
        print("阶跃 API：未启用；将使用本地规则解析。")
    while True:
        try:
            prompt = input("cell-note> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not handle_prompt(prompt, config, state):
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive CellNoteAgent shell.")
    parser.add_argument("--once", help="Run one natural-language instruction and exit.")
    parser.add_argument("--yes", action="store_true", help="Assume yes for execution confirmations.")
    parser.add_argument(
        "--no-tmux",
        action="store_true",
        help="Run long jobs (QC/download/demo) in the foreground instead of detached tmux.",
    )
    parser.add_argument("--repo_root", help="Repository root. Defaults to current directory.")
    parser.add_argument("--run_root", default="runs/agent-demo", help="Run/output directory.")
    parser.add_argument("--processing_python", help="Python executable with scanpy/anndata installed.")
    return parser


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run_agent(build_parser().parse_args(argv)))


if __name__ == "__main__":
    main()
