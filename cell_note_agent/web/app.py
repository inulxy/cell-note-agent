"""Single-user CellNote web workspace.

The UI only creates structured gates and launches existing whitelisted scripts.
It never exposes shell execution or provider credentials to the browser.
"""
from __future__ import annotations

import json
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from cell_note_agent.agent_cli import (
    AgentConfig,
    AgentState,
    analysis_ready_candidate_ids,
    apply_manifest_edit_plan,
    build_candidate_catalog,
    canonical_analysis_commands,
    classify_remote_file_role,
    create_manifest_from_selection,
    deterministic_action,
    deterministic_manifest_edit_plan,
    default_processing_python,
    deterministic_analysis_plan,
    direct_analysis_context,
    infer_search_slots,
    merge_crawl_runs,
    query_from_preferences,
    run_external_crawlers,
    run_long_commands,
    smallest_candidate_ids,
    step_action,
    step_manifest_edit_plan,
    write_manifest_revision,
)
from cell_note_agent.search_expansion import build_search_plan


REPO_ROOT = Path(os.environ.get("CELLNOTE_REPO_ROOT", Path(__file__).resolve().parents[2])).resolve()
WORKSPACE_ROOT = Path(os.environ.get("CELLNOTE_WEB_WORKSPACES", REPO_ROOT / "runs" / "web-workspaces")).resolve()
STATE_ROOT = Path(os.environ.get("CELLNOTE_WEB_STATE", REPO_ROOT / "state")).resolve()
STATIC_ROOT = REPO_ROOT / "web_static"
DIALOGUE_SKILL_PATH = REPO_ROOT / "skills" / "agent-dialogue-governance" / "SKILL.md"
ALLOWED_ROOTS = [
    WORKSPACE_ROOT,
    Path("/ssd/deecamp/cellnotes/EpiAgent_data").resolve(),
    (REPO_ROOT / "runs").resolve(),
]
DB_PATH = STATE_ROOT / "cellnote-web.db"
LOCK = threading.Lock()
_DIALOGUE_SKILL_CACHE = ""


class ProjectCreate(BaseModel):
    name: str | None = Field(default=None, max_length=80)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class GateResponse(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class GovernedText(str):
    """Marks text that has already passed through the constrained LLM output layer."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def suggested_project_name(prompt: str = "") -> str:
    """Generate a concise project title, then refine it after the first request."""
    lower = prompt.lower()
    if "li2023" in lower:
        return "Li2023a 脑组织 peak matrix 分析"
    species = "人类" if any(term in lower for term in ("人类", "human", "homo sapiens")) else "单细胞"
    modality = "Multiome" if "multiome" in lower else "scATAC"
    tissue = "PBMC " if "pbmc" in lower else ""
    if any(term in lower for term in ("搜索", "搜集", "数据集", "search", "discover")):
        return f"{species} {tissue}{modality} 数据检索"
    if any(term in lower for term in ("分析", "qc", "h5ad", "fragments", "matrix")):
        return f"{species} {tissue}{modality} 数据分析"
    return "新建单细胞表观组任务"


def refine_project_name(project_id: str, prompt: str) -> None:
    project = require_project(project_id)
    if not project["name"].startswith("新建单细胞表观组任务"):
        return
    name = suggested_project_name(prompt)
    with connect() as conn:
        conn.execute("update projects set name = ?, updated_at = ? where id = ?", (name, now(), project_id))


def connect() -> sqlite3.Connection:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists projects (
              id text primary key, name text not null, created_at text not null, updated_at text not null
            );
            create table if not exists gates (
              id text primary key, project_id text not null, kind text not null, status text not null,
              payload text not null, created_at text not null, resolved_at text
            );
            create table if not exists jobs (
              id text primary key, project_id text not null, kind text not null, status text not null,
              detail text not null, log_path text, tmux_session text, created_at text not null, updated_at text not null
            );
            create table if not exists messages (
              id text primary key, project_id text not null, role text not null, content text not null,
              created_at text not null
            );
            """
        )
        duplicate_rows = conn.execute(
            "select id, project_id, kind from gates where status = 'pending' order by created_at desc"
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        for row in duplicate_rows:
            key = (row["project_id"], row["kind"])
            if key in seen:
                conn.execute("update gates set status = ?, resolved_at = ? where id = ?", ("superseded", now(), row["id"]))
            else:
                seen.add(key)
        conn.execute(
            """
            update gates set status = ?, resolved_at = ?
            where kind = 'search' and status = 'pending' and project_id in (
              select project_id from gates where kind = 'candidate_review' and status = 'pending'
            )
            """,
            ("superseded", now()),
        )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key in ("payload", "detail"):
        if key in value:
            value[key] = json.loads(value[key])
    return value


def add_message(
    project_id: str,
    role: str,
    content: str,
    *,
    govern: bool = True,
    event: str = "auto",
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in {"user", "assistant"}:
        raise ValueError("invalid message role")
    if role == "assistant" and govern and not isinstance(content, GovernedText):
        content = governed_agent_output(project_id, content, event=event, facts=facts)
    message = {"id": str(uuid.uuid4()), "project_id": project_id, "role": role, "content": content.strip(), "created_at": now()}
    if not message["content"]:
        return message
    with connect() as conn:
        conn.execute(
            "insert into messages values (?, ?, ?, ?, ?)",
            (message["id"], message["project_id"], message["role"], message["content"], message["created_at"]),
        )
    return message


def messages_for_project(project_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "select * from messages where project_id = ? order by created_at asc limit 200", (project_id,)
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def project_dir(project_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{8,64}", project_id):
        raise HTTPException(404, "unknown project")
    return WORKSPACE_ROOT / project_id


def require_project(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, "project not found")
    return row_to_dict(row)


def read_state(project_id: str) -> dict[str, Any]:
    path = project_dir(project_id) / "project_state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(project_id: str, values: dict[str, Any]) -> None:
    path = project_dir(project_id) / "project_state.json"
    state = read_state(project_id)
    state.update(values)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def agent_config(project_id: str) -> AgentConfig:
    return AgentConfig(
        repo_root=REPO_ROOT,
        run_root=project_dir(project_id),
        processing_python=default_processing_python(),
        assume_yes=True,
        use_tmux=True,
    )


def agent_state(project_id: str) -> AgentState:
    state = read_state(project_id)
    return AgentState(
        last_crawl_run=Path(state["crawl_dir"]) if state.get("crawl_dir") else None,
        last_candidate_catalog=Path(state["candidate_catalog"]) if state.get("candidate_catalog") else None,
        last_manifest=Path(state["manifest"]) if state.get("manifest") else None,
    )


def is_allowed_path(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise HTTPException(400, "输入路径不存在")
    if not any(path.is_relative_to(root) for root in ALLOWED_ROOTS):
        raise HTTPException(403, "该路径不在允许的数据目录中")
    return path


def add_gate(project_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    gate_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            "update gates set status = ?, resolved_at = ? where project_id = ? and kind = ? and status = 'pending'",
            ("superseded", now(), project_id, kind),
        )
        conn.execute(
            "insert into gates values (?, ?, ?, ?, ?, ?, null)",
            (gate_id, project_id, kind, "pending", json.dumps(payload, ensure_ascii=False), now()),
        )
    return get_gate(project_id, gate_id)


def get_gate(project_id: str, gate_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("select * from gates where id = ? and project_id = ?", (gate_id, project_id)).fetchone()
    if not row:
        raise HTTPException(404, "gate not found")
    return row_to_dict(row)


def resolve_gate(project_id: str, gate_id: str) -> dict[str, Any]:
    gate = get_gate(project_id, gate_id)
    if gate["status"] != "pending":
        raise HTTPException(409, "gate already resolved")
    with connect() as conn:
        conn.execute("update gates set status = ?, resolved_at = ? where id = ?", ("resolved", now(), gate_id))
    return gate


def create_job(project_id: str, kind: str, detail: dict[str, Any]) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    log_path = project_dir(project_id) / "jobs" / job_id / "job.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            "insert into jobs values (?, ?, ?, ?, ?, ?, null, ?, ?)",
            (job_id, project_id, kind, "queued", json.dumps(detail, ensure_ascii=False), str(log_path), now(), now()),
        )
    return get_job(project_id, job_id)


def update_job(project_id: str, job_id: str, status: str, detail: dict[str, Any] | None = None, tmux_session: str | None = None) -> None:
    current = get_job(project_id, job_id)
    merged = current["detail"]
    if detail:
        merged.update(detail)
    with connect() as conn:
        conn.execute(
            "update jobs set status = ?, detail = ?, tmux_session = coalesce(?, tmux_session), updated_at = ? where id = ?",
            (status, json.dumps(merged, ensure_ascii=False), tmux_session, now(), job_id),
        )


def get_job(project_id: str, job_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("select * from jobs where id = ? and project_id = ?", (job_id, project_id)).fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    return row_to_dict(row)


def ensure_gate(project_id: str, kind: str, payload: dict[str, Any]) -> None:
    with connect() as conn:
        row = conn.execute("select id from gates where project_id = ? and kind = ? and status = 'pending'", (project_id, kind)).fetchone()
    if not row:
        add_gate(project_id, kind, payload)


def hydrate_job(project_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Infer progress from durable tmux logs after the browser reconnects."""
    detail = job["detail"]
    job["progress"] = int(detail.get("progress", 0))
    job["stage"] = str(detail.get("stage", "等待执行"))
    result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
    tmux_log = Path(result.get("log", "")) if result.get("log") else None
    if job["status"] != "submitted" or not tmux_log or not tmux_log.exists():
        return job
    text = tmux_log.read_text(encoding="utf-8", errors="replace")
    if "[done]" in text:
        update_job(project_id, job["id"], "completed", {"progress": 100, "stage": "阶段任务完成"})
        job = get_job(project_id, job["id"])
        job["progress"], job["stage"] = 100, "阶段任务完成"
        if job["kind"] == "download":
            ensure_gate(project_id, "download_complete", {"message": "下载与校验已完成。请在结果文件中确认文件，随后提供路径进入分析。"})
            add_message(project_id, "assistant", download_completion_summary(project_id))
        if job["kind"] == "qc":
            ensure_gate(project_id, "qc_complete", {"message": "QC 与标准化交付已完成。请查看结果文件，或将 peak matrix 交给 Foundation Model。"})
            fallback = "## QC 与标准化交付完成\n\n受控 QC stages 已完成，系统已生成独立数据集的 GRCh38 cell × peak matrix、QC summary、data card 与 MANIFEST。右侧结果文件仅用于下载和查看产物。\n\n**下一步：**你可以说“解释 QC 结果”或“继续 Foundation Model 分析”；也可以直接新建项目处理下一份数据。"
            add_message(project_id, "assistant", llm_report(
                "QC 与标准化交付结果",
                {
                    "job_kind": "qc",
                    "job_status": "completed",
                    "progress": 100,
                    "expected_deliverables": ["GRCh38 cell × peak matrix", "QC summary", "data card", "MANIFEST"],
                    "next_actions": ["解释 QC 结果", "继续 Foundation Model 分析", "处理新的数据集"],
                },
                fallback,
                guidance="清楚总结完成边界、交付物和可选下一步，不要推断未提供的 QC 数值。",
            ))
        return job
    if job["kind"] == "download":
        if "stage=verify" in text:
            job["progress"], job["stage"] = 85, "完整性校验中"
        elif "stage=fetch" in text or "downloading:" in text:
            job["progress"], job["stage"] = 50, "文件下载中"
    elif job["kind"] == "qc":
        match = re.findall(r"===== \[(\d+)/(\d+)\]", text)
        if match:
            current, total = map(int, match[-1])
            job["progress"] = max(10, int((current - 1) * 100 / total))
            job["stage"] = f"QC stage {current}/{total}"
    return job


def pause_managed_job(project_id: str, job_id: str) -> dict[str, Any]:
    """Interrupt any active task; tmux jobs pause, foreground jobs cancel."""
    job = hydrate_job(project_id, get_job(project_id, job_id))
    progress = int(job["detail"].get("progress", job.get("progress", 0)))
    label = {"download": "下载", "qc": "QC", "crawl": "搜索", "manifest": "下载清单生成"}.get(job["kind"], job["kind"])
    if job["status"] == "submitted":
        session = job.get("tmux_session")
        running = bool(session) and subprocess.run(
            ["tmux", "has-session", "-t", f"={session}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        ).returncode == 0
        if not running:
            hydrated = hydrate_job(project_id, get_job(project_id, job_id))
            if hydrated["status"] == "completed":
                raise HTTPException(409, "该任务已完成，不能暂停")
            raise HTTPException(409, "任务会话已结束，无法暂停；请刷新查看最新状态")
        subprocess.run(["tmux", "kill-session", "-t", f"={session}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        update_job(project_id, job_id, "paused", {"progress": progress, "stage": f"{label}已暂停；当前结果与日志已保留"})
        append_log(Path(job["log_path"]), "[paused] user requested pause; tmux session stopped")
        add_gate(project_id, "paused_task", {"job_id": job_id, "kind": job["kind"], "progress": progress})
        return get_job(project_id, job_id)
    if job["status"] not in {"queued", "running"}:
        raise HTTPException(409, "该任务当前不在运行中；如果已完成，不能再暂停")
    pid = job["detail"].get("pid")
    if pid:
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (TypeError, ValueError):
            pass
    update_job(project_id, job_id, "cancelled", {"progress": progress, "stage": f"{label}已中断；已保留日志和已发现结果"})
    append_log(Path(job["log_path"]), "[cancelled] user requested interruption")
    add_gate(project_id, "cancelled_task", {"job_id": job_id, "kind": job["kind"], "progress": progress})
    return get_job(project_id, job_id)


def latest_pauseable_job(project_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        rows = conn.execute(
            "select * from jobs where project_id = ? and status in ('queued', 'submitted', 'running') order by created_at desc", (project_id,)
        ).fetchall()
    for row in rows:
        job = hydrate_job(project_id, row_to_dict(row))
        if job["status"] in {"queued", "submitted", "running"}:
            return job
    return None


def append_log(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def set_job_progress(project_id: str, job_id: str, progress: int, stage: str) -> None:
    update_job(project_id, job_id, "running", {"progress": max(0, min(progress, 100)), "stage": stage})


def run_background(project_id: str, kind: str, detail: dict[str, Any], work) -> dict[str, Any]:
    job = create_job(project_id, kind, detail)
    def runner() -> None:
        log_path = Path(job["log_path"])
        if get_job(project_id, job["id"])["status"] == "cancelled":
            append_log(log_path, "[cancelled] task was cancelled before start")
            return
        update_job(project_id, job["id"], "running")
        append_log(log_path, f"[start] {now()} kind={kind}")
        try:
            result = work(log_path, job["id"])
            if get_job(project_id, job["id"])["status"] == "cancelled":
                append_log(log_path, f"[cancelled] {now()}")
                return
            if isinstance(result, dict) and result.get("mode") == "tmux":
                update_job(project_id, job["id"], "submitted", {"result": result}, result.get("session"))
            else:
                update_job(project_id, job["id"], "completed", {"result": result or {}})
            append_log(log_path, f"[done] {now()}")
        except Exception:
            append_log(log_path, traceback.format_exc())
            update_job(project_id, job["id"], "failed")
    threading.Thread(target=runner, daemon=True).start()
    return job


def csv_rows(path: Path, limit: int = 100) -> list[dict[str, str]]:
    import csv
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))[:limit]


def dialogue_skill_text() -> str:
    global _DIALOGUE_SKILL_CACHE
    if not _DIALOGUE_SKILL_CACHE and DIALOGUE_SKILL_PATH.is_file():
        _DIALOGUE_SKILL_CACHE = DIALOGUE_SKILL_PATH.read_text(encoding="utf-8")
    return _DIALOGUE_SKILL_CACHE


def redact_dialogue_text(text: str) -> str:
    value = re.sub(r"https?://\S+|ftp://\S+", "<REMOTE_URL>", str(text))
    value = re.sub(r"(?<!\w)/(?:[^\s`'\"]+/)*[^\s`'\"]+", "<LOCAL_PATH>", value)
    value = re.sub(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+", r"\1=<REDACTED>", value)
    return value[:4000]


def compact_interaction_state(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        gate_row = conn.execute(
            "select kind from gates where project_id = ? and status = 'pending' order by created_at desc limit 1",
            (project_id,),
        ).fetchone()
        job_row = conn.execute(
            "select kind, status, detail from jobs where project_id = ? order by created_at desc limit 1",
            (project_id,),
        ).fetchone()
    gate_kind = str(gate_row["kind"]) if gate_row else ""
    allowed_actions = {
        "search": ["确认或补充可见搜索条件", "取消"],
        "candidate_triage": ["自动选择", "最小文件测试", "手动选择", "收紧筛选", "停止"],
        "manifest_review": ["确认下载", "取消", "自然语言修改清单"],
        "analysis": ["完整 QC", "仅 packaging", "取消", "补充阈值偏好"],
        "analysis_execute": ["确认提交 QC", "取消"],
        "download": ["确认开始下载", "取消"],
        "paused_task": ["继续", "终止", "调整后重跑"],
    }
    job_context: dict[str, Any] = {}
    if job_row:
        detail = json.loads(job_row["detail"]) if job_row["detail"] else {}
        job_context = {
            "kind": job_row["kind"],
            "status": job_row["status"],
            "progress": detail.get("progress"),
            "stage": detail.get("stage"),
        }
    return {
        "pending_interaction": gate_kind,
        "allowed_actions": allowed_actions.get(gate_kind, []),
        "latest_job": job_context,
    }


def governed_agent_output(
    project_id: str,
    fallback: str,
    *,
    event: str = "auto",
    facts: dict[str, Any] | None = None,
) -> str:
    if not os.environ.get("STEP_API_KEY"):
        return fallback
    skill = dialogue_skill_text()
    if not skill:
        return fallback
    try:
        from cell_note_agent.step_api import chat_completion, first_message_content

        context = compact_interaction_state(project_id)
        if facts:
            context["event_facts"] = sanitize_report_evidence(facts)
        system = (
            f"{skill}\n\n"
            "Apply this policy to one Web assistant message. The local interaction card is authoritative. "
            "Do not enumerate numeric menu choices or invent controls. When a gate exists, explain the decision briefly and ask the user to use the visible card. "
            "Return only the user-facing Chinese Markdown message."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({
                "event": event,
                "interaction_state": context,
                "reliable_message": redact_dialogue_text(fallback),
            }, ensure_ascii=False)},
        ]
        for attempt in range(2):
            try:
                response = chat_completion(messages, temperature=0.35, max_tokens=700)
                generated = first_message_content(response).strip()
                if generated:
                    return generated
            except Exception:
                if attempt == 0:
                    time.sleep(0.6)
        return fallback
    except Exception:
        return fallback


def llm_report(
    report_kind: str,
    evidence: dict[str, Any],
    fallback: str,
    *,
    guidance: str = "",
    max_tokens: int = 1200,
) -> str:
    """Explain sanitized local evidence without allowing the model to control execution."""
    if not os.environ.get("STEP_API_KEY"):
        return GovernedText(fallback)
    try:
        from cell_note_agent.step_api import chat_completion, first_message_content

        system = (
            f"{dialogue_skill_text()}\n\n"
            "你是 CellNoteAgent 的专业生物信息学报告层。你的职责仅是解释本地程序提供的脱敏 evidence，"
            "不能决定或执行搜索、下载、QC，也不能修改任何参数、文件或状态。"
            "只使用 evidence 中明确出现的事实；绝不补写数据集标题、论文、样本数、细胞组成、基因组版本、"
            "文件可用性、校验结论或已经完成的步骤。必须区分已验证事实、元数据推断、计划和未知信息。"
            "请用自然、专业、连贯的中文 Markdown 组织内容，像资深生信分析师向用户汇报；"
            "根据本次证据选择重点，不要机械复述固定模板，不要输出命令、密钥、本地路径或下载 URL。"
            "必须保留关键数字、安全确认边界和 evidence 中指定的下一步。"
            f"报告类型：{report_kind}。{guidance}"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(sanitize_report_evidence(evidence), ensure_ascii=False)},
        ]
        for attempt in range(2):
            try:
                response = chat_completion(messages, temperature=0.3, max_tokens=max_tokens)
                generated = first_message_content(response).strip()
                if generated:
                    return GovernedText(generated)
            except Exception:
                if attempt == 0:
                    time.sleep(0.6)
        return GovernedText(fallback)
    except Exception:
        return GovernedText(fallback)


def sanitize_report_evidence(value: Any) -> Any:
    """Remove local paths, URLs, credentials and checksum values before API calls."""
    blocked = ("path", "uri", "url", "secret", "token", "api_key", "checksum", "command", "argv")
    if isinstance(value, dict):
        return {
            str(key): sanitize_report_evidence(item)
            for key, item in value.items()
            if not any(term in str(key).lower() for term in blocked)
        }
    if isinstance(value, list):
        return [sanitize_report_evidence(item) for item in value]
    return value


def smallest_download_request(text: str) -> bool:
    lower = text.lower()
    wants_download = any(word in lower for word in ("下载", "download", "获取"))
    wants_smallest = any(word in lower for word in ("最小", "最少", "最轻量", "小数据集", "smallest", "small dataset"))
    return wants_download and wants_smallest


def smallest_downloadable_candidate_ids(catalog: Path, *, n: int = 1) -> list[int]:
    rows = csv_rows(catalog, limit=10_000)
    ranked: list[tuple[int, int]] = []
    for row in rows:
        try:
            candidate_id = int(row.get("candidate_id") or "")
            size_bytes = int(row.get("total_size_bytes") or 0)
            if size_bytes <= 0:
                size_bytes = int(float(row.get("total_size_gb") or 0) * 1e9)
        except (TypeError, ValueError):
            continue
        if size_bytes > 0 and int(row.get("file_count") or 0) > 0:
            ranked.append((size_bytes, candidate_id))
    ranked.sort()
    return [candidate_id for _, candidate_id in ranked[:n]]


def candidate_selection_summary(catalog: Path, candidate_ids: list[int]) -> str:
    rows = csv_rows(catalog, limit=500)
    by_id = {int(row.get("candidate_id") or 0): row for row in rows if str(row.get("candidate_id") or "").isdigit()}
    selected = [by_id[item] for item in candidate_ids if item in by_id]
    total_candidates = len(rows)
    lines = []
    for row in selected:
        lines.append(
            f"- **{row.get('study_accession') or 'unknown'}**：来源 {row.get('repository_source') or 'unknown'}；"
            f"推断类型 {row.get('inferred_modality') or 'unknown'}；主要文件角色 {row.get('best_file_role') or 'unknown'}；"
            f"{row.get('file_count') or 'unknown'} 个远程文件；估计总量 {row.get('total_size_gb') or 'unknown'} GB。"
        )
    fallback = (
        "## 已选择待下载数据集\n\n"
        f"我已在当前 **{total_candidates}** 个候选中，按候选表记录的可下载文件总大小从小到大比较，选择体量最小的候选。\n\n"
        "### 选择结果\n" + ("\n".join(lines) or "- 未找到有效候选。") + "\n\n"
        "### 为什么选择它\n"
        "该候选是当前目录中具有有效文件数量和已知远程体积的最小项。选择依据是 crawler 收集到的可下载文件总大小，"
        "而不是论文样本数，也不会用大小未知的记录冒充最小数据集。下一步会解析该候选的具体远程文件并生成下载清单；"
        "只有在清单中确认文件名、角色和体积后，才会进入真实下载。"
    )
    evidence_rows = [
        {
            key: row.get(key, "")
            for key in (
                "candidate_id", "study_accession", "repository_source", "scientific_name",
                "inferred_modality", "best_file_role", "genome_build", "file_count",
                "total_size_bytes", "total_size_gb", "priority_reason",
            )
        }
        for row in selected
    ]
    return llm_report(
        "最小候选数据集选择说明",
        {
            "candidate_count": total_candidates,
            "selected_candidates": evidence_rows,
            "selection_rule": "只比较文件数量大于 0 且远程总大小为正数的候选，选择可下载文件总大小最小项",
            "current_stage": "已选择候选，尚未生成最终下载清单，也尚未下载",
            "next_action": "生成并展示下载清单，由用户确认后才允许真实下载",
        },
        fallback,
        guidance="重点解释为什么选择该候选、依据的局限性以及下载尚未开始。",
    )


def download_completion_summary(project_id: str) -> str:
    root = project_dir(project_id)
    downloaded = root / "raw" / "downloaded_file_manifest.csv"
    if not downloaded.is_file():
        fallback = "## 下载任务结束\n\n未找到已验证文件清单，请查看右侧日志与缺失报告确认下载状态。"
        return llm_report(
            "下载结果异常说明",
            {"verified_manifest_found": False, "next_action": "查看任务日志与缺失报告，不能声称下载成功"},
            fallback,
        )
    rows = csv_rows(downloaded, limit=10_000)
    total_size = sum(int(row.get("size_bytes") or 0) for row in rows)
    datasets = sorted({row.get("dataset_id") or "unknown" for row in rows})
    details = "\n".join(
        f"- **{Path(row.get('local_path') or row.get('artifact_id') or 'unknown').name}**："
        f"数据集 {row.get('dataset_id') or 'unknown'}；角色 {row.get('role') or 'unknown'}；"
        f"格式 {row.get('file_format') or 'unknown'}；大小 {int(row.get('size_bytes') or 0) / 1e6:.2f} MB；"
        f"校验 {row.get('checksum_algorithm') or 'checksum'} 已记录。"
        for row in rows[:20]
    ) or "- 没有通过验证的文件。"
    fallback = (
        "## 下载结果\n\n"
        f"下载与完整性校验流程已完成。当前共有 **{len(rows)}** 个文件进入已验证清单，"
        f"涉及 **{len(datasets)}** 个数据集，总大小 **{total_size / 1e9:.3f} GB**。\n\n"
        f"**数据集：**{', '.join(datasets) if datasets else '无'}\n\n"
        "### 文件内容\n" + details + "\n\n"
        "### 完整性说明\n"
        "上述文件已进入本地 verify 阶段的已验证清单：存在远程预期大小或校验值时会进行一致性检查，同时记录实际大小、"
        "校验算法、校验值和本地路径。这里不额外声称矩阵维度、细胞组成或基因组版本；"
        "这些信息需要在下一步输入检测/QC 时读取文件内容后才能确认。\n\n"
        "### 下一步\n"
        "文件已显示在右侧结果区域。你可以直接说“分析刚下载的数据”，Agent 会检测它是 fragments、peak matrix 还是 multiome，再生成对应 QC 计划。"
    )
    evidence_rows = [
        {
            "dataset_id": row.get("dataset_id") or "unknown",
            "artifact_id": row.get("artifact_id") or "unknown",
            "role": row.get("role") or "unknown",
            "file_format": row.get("file_format") or "unknown",
            "size_bytes": int(row.get("size_bytes") or 0),
            "checksum_algorithm": row.get("checksum_algorithm") or "unknown",
        }
        for row in rows[:50]
    ]
    return llm_report(
        "下载与完整性校验结果",
        {
            "verified_manifest_found": True,
            "verified_file_count": len(rows),
            "dataset_count": len(datasets),
            "dataset_ids": datasets,
            "total_size_bytes": total_size,
            "verified_files": evidence_rows,
            "verification_scope": "文件进入已验证清单；存在远程预期大小或校验值时执行一致性检查，并在本地记录校验信息",
            "unknown_until_inspection": ["矩阵维度", "细胞组成", "基因组版本", "文件内容语义"],
            "next_action": "用户可要求分析刚下载的数据；Agent 将先检测输入类型和规模，再生成 QC 计划",
        },
        fallback,
        guidance="先明确下载结果，再解释数据集、文件内容、验证范围、未知信息和后续分析入口。",
        max_tokens=1600,
    )


def manifest_summary(rows: list[dict[str, str]]) -> str:
    total_size = sum(int(row.get("size_bytes") or 0) for row in rows)
    role_counts: dict[str, int] = {}
    for row in rows:
        role = str(row.get("role") or "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1
    preview = "\n".join(
        f"- [{index}] {row.get('dataset_id') or '-'} · {row.get('artifact_id') or '-'} · "
        f"{row.get('role') or '-'} · {int(row.get('size_bytes') or 0) / 1e9:.3f} GB"
        for index, row in enumerate(rows[:8], 1)
    ) or "- 没有可下载条目"
    fallback = (
        "## 下载清单已生成\n\n"
        f"已从选定候选中整理出 **{len(rows)}** 个文件，预计总下载量 **{total_size / 1e9:.3f} GB**。"
        f"文件角色分布：{'，'.join(f'{name} {count}' for name, count in role_counts.items()) or '未知'}。\n\n"
        "### 清单预览\n" + preview + "\n\n"
        "### 边界与下一步\n"
        "清单仅表示待下载文件，尚未执行 fetch。你可以继续缩小范围，系统会生成新的版本；确认后才会启动 plan → fetch → verify。"
    )
    evidence_rows = [
        {
            key: row.get(key, "")
            for key in ("dataset_id", "artifact_id", "role", "file_format", "size_bytes", "checksum_algorithm")
        }
        for row in rows[:50]
    ]
    return llm_report(
        "下载清单审核报告",
        {
            "file_count": len(rows),
            "total_size_bytes": total_size,
            "role_counts": role_counts,
            "manifest_files": evidence_rows,
            "current_stage": "下载清单已生成，尚未执行 fetch",
            "safety_boundary": "必须由用户确认后才执行 plan、fetch、verify",
            "next_action": "用户审核文件、角色和体积，可修改清单、取消或确认下载",
        },
        fallback,
        guidance="突出清单规模、主要文件、潜在体积风险和人工确认边界。",
        max_tokens=1500,
    )


def run_search(project_id: str, payload: dict[str, Any], log_path: Path, job_id: str) -> dict[str, Any]:
    query = str(payload["query"])
    original_query = str(payload.get("original_query") or query)
    preferences = payload["preferences"]
    request = str(preferences.get("candidate_limit_request") or preferences.get("candidate_limit") or "10")
    lowered_request = request.lower()
    if any(word in lowered_request for word in ("所有", "全部", "不限", "不设", "尽量多", "all", "everything")):
        display_limit = 500
    else:
        match = re.search(r"\d+", request)
        display_limit = max(1, min(int(match.group(0)), 500)) if match else 10
    sources = preferences.get("sources") or ["geo", "sra", "literature"]
    config = agent_config(project_id)
    run_id = "web-crawl-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    crawl_dir = config.run_root / "crawls" / run_id
    optimized_query = query_from_preferences(query, preferences)
    search_plan = build_search_plan(original_query, preferences)
    core_queries = list(dict.fromkeys([optimized_query, *search_plan.core_queries]))
    acquisition = str(preferences.get("acquisition") or "").lower()
    enable_ffq = not any(term in acquisition for term in ("处理后", "matrix", "fragment"))
    set_job_progress(project_id, job_id, 5, "准备 crawler")
    append_log(log_path, f"[search-preference] candidate display limit request={request!r}; display_limit={display_limit}")
    append_log(log_path, f"[search-plan] core_queries={len(core_queries)} external_queries={len(search_plan.external_queries)} retrieval_limit={search_plan.retrieval_limit_per_source}")
    append_log(log_path, f"[search-plan] raw FASTQ resolution={'enabled' if enable_ffq else 'skipped; user prefers processed files'}")
    append_log(log_path, "[stage] core crawler running")
    query_runs: list[Path] = []
    for index, search_query in enumerate(core_queries, 1):
        if get_job(project_id, job_id)["status"] == "cancelled":
            return {"cancelled": True, "crawl_dir": str(crawl_dir)}
        target_dir = crawl_dir if index == 1 else crawl_dir / "query_runs" / f"q{index:02d}"
        target_run_id = run_id if index == 1 else f"{run_id}-q{index:02d}"
        command = ["./cell-note", "--config", "configs/mvp.json", "crawl", "--query", search_query,
                   "--out", str(target_dir), "--run-id", target_run_id, "--resolve-ena-runs",
                   "--limit", str(search_plan.retrieval_limit_per_source)]
        for source in sources:
            command.extend(["--source", str(source)])
        append_log(log_path, "$ " + " ".join(command))
        progress_value = 10 + int(35 * (index - 1) / max(1, len(core_queries)))
        set_job_progress(project_id, job_id, progress_value, f"公开来源检索 {index}/{len(core_queries)}")
        process = subprocess.Popen(command, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, start_new_session=True)
        update_job(project_id, job_id, "running", {"pid": process.pid, "stage": f"公开来源检索 {index}/{len(core_queries)}"})
        assert process.stdout is not None
        for line in process.stdout:
            append_log(log_path, line)
        return_code = process.wait()
        if return_code and not (target_dir / "crawl_manifest.json").exists():
            append_log(log_path, f"[warning] query shard {index} failed with exit code {return_code}")
        if index > 1 and target_dir.exists():
            query_runs.append(target_dir)
    merge_crawl_runs(crawl_dir, query_runs)
    update_job(project_id, job_id, "running", {"pid": None, "stage": "公开来源检索完成，正在补充外部来源"})
    append_log(log_path, "[stage] external discovery running")
    set_job_progress(project_id, job_id, 48, "外部来源补充中")
    def external_progress(source_name: str, status: str, source_progress: int) -> None:
        if get_job(project_id, job_id)["status"] == "cancelled":
            raise RuntimeError("search cancelled by user")
        overall = 48 + int(32 * max(0, min(source_progress, 100)) / 100)
        set_job_progress(project_id, job_id, overall, f"{source_name}: {status}")
        append_log(log_path, f"[external-source] {source_name} {status} {source_progress}%")
    external = run_external_crawlers(
        crawl_dir,
        optimized_query,
        search_plan.retrieval_limit_per_source,
        queries=search_plan.external_queries,
        progress=external_progress,
        enable_ffq=enable_ffq,
    )
    current_files = []
    for sidecar in (crawl_dir / "remote_file_candidates.jsonl", crawl_dir / "external_remote_file_candidates.jsonl"):
        if sidecar.exists():
            for line in sidecar.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    current_files.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    ready_roles = {"peak_matrix", "fragments"}
    relaxed = None
    if not any(classify_remote_file_role(item) in ready_roles for item in current_files) and search_plan.relaxed_queries:
        append_log(log_path, "[stage] no analysis-ready files found; starting one relaxed retrieval pass")
        set_job_progress(project_id, job_id, 80, "未发现分析就绪文件，正在自动放宽一次检索")
        relaxed_dir = crawl_dir / "relaxed_pass"
        relaxed = run_external_crawlers(
            relaxed_dir,
            search_plan.relaxed_queries[0],
            min(search_plan.retrieval_limit_per_source, 200),
            queries=search_plan.relaxed_queries[1:],
            progress=external_progress,
            enable_ffq=enable_ffq,
        )
        merge_crawl_runs(crawl_dir, [relaxed_dir])
        append_log(log_path, f"[relaxed-search] {json.dumps(relaxed, ensure_ascii=False)}")
    external["relaxed_pass"] = relaxed
    if get_job(project_id, job_id)["status"] == "cancelled":
        return {"cancelled": True, "crawl_dir": str(crawl_dir)}
    append_log(log_path, "[stage] candidate catalog building")
    set_job_progress(project_id, job_id, 85, "候选数据集整理中")
    state = AgentState(last_crawl_run=crawl_dir)
    search_profile = {
        "user_query": original_query,
        "crawler_query": query,
        "optimized_query": optimized_query,
        "user_preferences": preferences,
        "search_plan": search_plan.as_dict(),
        "external_discovery": external,
    }
    (crawl_dir / "search_profile.json").write_text(json.dumps(search_profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state.last_search_profile = search_profile
    captured = StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        catalog = build_candidate_catalog(config, state, crawl_dir)
    append_log(log_path, captured.getvalue())
    append_log(log_path, "[stage] search complete")
    records = csv_rows(catalog, limit=10_000) if catalog and catalog.exists() else []
    actionable_records = [
        item for item in records
        if str(item.get("metadata_only") or "no").lower() != "yes" and int(item.get("file_count") or 0) > 0
    ]
    write_state(project_id, {"crawl_dir": str(crawl_dir), "candidate_catalog": str(catalog) if catalog else "", "candidate_filter": "", "search_preferences": preferences, "candidate_display_limit": display_limit, "search_plan": search_plan.as_dict()})
    with connect() as conn:
        conn.execute(
            "update gates set status = ?, resolved_at = ? where project_id = ? and kind = ? and status = 'pending'",
            ("resolved", now(), project_id, "search"),
        )
    set_job_progress(project_id, job_id, 100, "搜索完成，结果已保留")
    summary_records = [
        {
            key: item.get(key, "")
            for key in (
                "study_accession", "repository_source", "scientific_name", "inferred_modality",
                "best_file_role", "genome_build", "total_size_gb", "study_total_size_gb",
                "smallest_file_size_bytes", "preferred_file_count", "file_count", "evidence_status",
                "confirmed_facts", "unknown_fields", "mismatch_fields", "priority_reason", "metadata_only",
            )
        }
        for item in (actionable_records[:12] or records[:12])
    ]
    add_message(project_id, "assistant", stage_summary("search", {
        "candidate_count": len(records),
        "actionable_count": len(actionable_records),
        "metadata_only_count": len(records) - len(actionable_records),
        "display_limit": display_limit,
        "preferences": preferences,
        "search_plan": search_plan.as_dict(),
        "source_summary": external.get("official_sources", {}),
        "retrieval_diagnostics": {
            "external_enabled": bool(external.get("enabled")),
            "external_error": external.get("error", ""),
            "official_dataset_records": external.get("official_dataset_records", 0),
            "official_downloadable_files": external.get("official_downloadable_files", 0),
            "geo_supplementary_files": external.get("geo_supplementary_files", 0),
            "relaxed_pass_used": relaxed is not None,
            "relaxed_pass_error": (relaxed or {}).get("error", "") if isinstance(relaxed, dict) else "",
        },
        "candidate_records": summary_records,
        "next_action": "停在检索结果查看阶段，等待用户主动提出筛选、生成下载清单或其他下一步要求。",
    }))
    return {"crawl_dir": str(crawl_dir), "catalog": str(catalog) if catalog else "", "external": external, "records": records}


def run_manifest(project_id: str, candidate_ids: list[int], log_path: Path, job_id: str) -> dict[str, Any]:
    set_job_progress(project_id, job_id, 10, "读取候选数据集")
    append_log(log_path, f"[stage] selected candidate ids: {candidate_ids}")
    state = agent_state(project_id)
    manifest = create_manifest_from_selection(agent_config(project_id), state, candidate_ids)
    if not manifest:
        raise RuntimeError("所选候选没有可下载文件")
    set_job_progress(project_id, job_id, 70, "整理可下载文件与大小")
    rows = csv_rows(manifest)
    with connect() as conn:
        conn.execute(
            "update gates set status = ?, resolved_at = ? where project_id = ? and kind = ? and status = 'pending'",
            ("resolved", now(), project_id, "candidate_review"),
        )
    write_state(project_id, {"manifest": str(manifest), "downloads_visible": False})
    manifest_gate = add_manifest_review_gate(project_id, manifest)
    set_job_progress(project_id, job_id, 100, "下载清单已生成，等待确认下载")
    add_message(project_id, "assistant", manifest_summary(rows))
    add_message(project_id, "assistant", conversation_gate_prompt("manifest_review", manifest_gate["payload"]))
    append_log(log_path, f"[done] manifest={manifest}; files={len(rows)}")
    return {"manifest": str(manifest), "file_count": len(rows)}


def intelligent_reply(project_id: str, prompt: str) -> str:
    """Use StepFun for conversational guidance only; execution stays in local gates."""
    if not os.environ.get("STEP_API_KEY"):
        return "我可以帮你搜索公开数据、查看候选、生成下载清单、下载校验，或分析服务器上的 fragments、peak matrix、multiome 路径。"
    try:
        from cell_note_agent.step_api import chat_completion, first_message_content

        history = messages_for_project(project_id)[-8:]
        messages = [{
            "role": "system",
            "content": (
                f"{dialogue_skill_text()}\n\n"
                "你是 CellNoteAgent 的中文对话层。简洁地澄清用户需求或解释当前流程。"
                "不要声称已执行任何下载或生信分析，不要给 shell 命令，不要编造数据集。"
                "可执行动作由本地受控 gate 完成：搜索、候选选择、下载清单、确认下载、输入检测、QC。"
                "若用户要执行操作，说明下一步应该在界面中确认什么。"
            ),
        }]
        messages.extend({"role": item["role"], "content": redact_dialogue_text(item["content"])} for item in history)
        response = chat_completion(messages, temperature=0.2, max_tokens=500)
        return GovernedText(first_message_content(response).strip())
    except Exception:
        return "我已理解你的问题。请告诉我是否要搜索公开数据，或粘贴已有数据的完整路径；我会先展示受控的下一步。"


STAGE_DESCRIPTIONS = {
    "plan": "生成参考资源计划，列出固定版本、下载地址、校验和与目标路径。",
    "fetch": "下载缺失的参考资源，并对已存在且校验通过的文件安全跳过。",
    "verify": "核对参考资源校验和、文件结构与可用性，阻止损坏资源进入 QC。",
    "import": "导入 fragments 并建立可处理的数据对象。",
    "pre-filter": "执行基础预过滤，去除明显无效记录。",
    "filter": "按选定阈值过滤低质量细胞与低支持 peaks。",
    "embed": "计算低维表示，供质量诊断与可视化使用。",
    "cluster": "进行聚类辅助检查细胞群结构。",
    "doublet": "检测并标记潜在 doublets。",
    "call-peaks": "在通过 QC 的数据上调用 peaks。",
    "make-peak-matrix": "生成每个数据集独立的 cell × peak matrix。",
    "load": "读取已有 cell × peak matrix 并校验矩阵结构。",
    "profile": "统计细胞、peak 和计数分布，形成 QC 基线。",
    "standardize": "标准化 peak 坐标和矩阵元数据，保持目标基因组版本。",
    "embed-cluster": "执行可选的降维与聚类；大矩阵可跳过以优先稳妥交付。",
    "pair-check": "检查 RNA 与 ATAC 输入是否能正确配对。",
    "qc-rna": "对 RNA 部分进行质量控制。",
    "qc-atac": "对 ATAC 部分进行质量控制。",
    "intersect": "保留通过 RNA 和 ATAC 联合 QC 的细胞。",
    "finalize": "固化本路径 QC 后的标准化数据与统计结果。",
    "cards": "生成数据卡和 QC 摘要。",
    "validate": "验证交付物完整性、结构与关键元数据。",
    "package": "打包 peak matrix、QC 记录和 MANIFEST 供下游使用。",
    "inspect": "以安全方式检查超大矩阵的元数据与结构。",
    "materialize": "物化过滤后的可交付矩阵或所需子集。",
}


def qc_stage_details(commands: list[list[str]]) -> list[dict[str, str]]:
    stages: list[dict[str, str]] = []
    for command in commands:
        try:
            stage = command[command.index("--stage") + 1]
        except (ValueError, IndexError):
            continue
        script = next((part for part in command if part.startswith("scripts/")), "受控脚本")
        stages.append({"stage": stage, "script": script, "description": STAGE_DESCRIPTIONS.get(stage, "执行该受控流水线阶段。")})
    return stages


def stage_summary(stage: str, evidence: dict[str, Any]) -> str:
    """Ask StepFun to explain a completed decision using only supplied evidence."""
    if stage == "search":
        records = evidence.get("candidate_records") or []
        source_counts: dict[str, int] = {}
        modality_counts: dict[str, int] = {}
        role_counts: dict[str, int] = {}
        evidence_counts: dict[str, int] = {}
        for record in records:
            source = str(record.get("repository_source") or "unknown")
            modality = str(record.get("inferred_modality") or "unknown")
            role = str(record.get("best_file_role") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
            modality_counts[modality] = modality_counts.get(modality, 0) + 1
            role_counts[role] = role_counts.get(role, 0) + 1
            status = str(record.get("evidence_status") or "unknown")
            evidence_counts[status] = evidence_counts.get(status, 0) + 1
        source_text = "，".join(f"{key} {value}" for key, value in source_counts.items()) or "暂无可可靠归类来源"
        modality_text = "，".join(f"{key} {value}" for key, value in modality_counts.items()) or "暂无可靠模态标签"
        role_text = "，".join(f"{key} {value}" for key, value in role_counts.items()) or "暂无可下载文件角色"
        evidence_text = "，".join(f"{key} {value}" for key, value in evidence_counts.items()) or "暂无证据状态"
        candidate_lines = "\n".join(
            f"- **[{index}] {record.get('study_accession') or 'unknown'}**：来源 {record.get('repository_source') or 'unknown'}；"
            f"类型 {record.get('inferred_modality') or 'unknown'}；可用文件 {record.get('best_file_role') or 'unknown'}；"
            f"优先文件组合约 {record.get('total_size_gb') or 'unknown'} GB（整项研究约 {record.get('study_total_size_gb') or 'unknown'} GB）；"
            f"证据状态 {record.get('evidence_status') or 'unknown'}。"
            for index, record in enumerate(records[:6], 1)
        ) or "- 本次没有形成候选记录。"
        diagnostics = evidence.get("retrieval_diagnostics") or {}
        source_summary = evidence.get("source_summary") or {}
        source_errors = source_summary.get("errors") or {}
        source_error_text = "；".join(f"{name}: {message}" for name, message in source_errors.items()) or "未记录来源请求错误"
        fallback = (
            "## 检索结果总结\n\n"
            f"本次已按确认条件完成公开元数据检索和候选整理，共形成 **{evidence['candidate_count']}** 条候选记录；"
            f"其中 **{evidence.get('actionable_count', 0)}** 条已解析出下载文件，"
            f"**{evidence.get('metadata_only_count', 0)}** 条目前只有公开元数据。"
            "这不是全网数据集总量，文件体积限制只用于排序和下载规划，不会在检索阶段删除相关研究。\n\n"
            "### 核心发现\n"
            f"- **来源分布（候选表样本）**：{source_text}。\n"
            f"- **模态分布（候选表样本）**：{modality_text}。\n"
            f"- **文件角色**：{role_text}。\n"
            f"- **证据状态**：{evidence_text}。`unknown` 表示信息不足，不等同于不匹配。\n"
            f"- **补充文件解析**：本轮解析到 {diagnostics.get('geo_supplementary_files', 0)} 个 GEO supplementary 文件；"
            f"自动放宽检索{'已执行' if diagnostics.get('relaxed_pass_used') else '未触发'}。\n\n"
            "### 候选示例\n"
            f"{candidate_lines}\n\n"
            "### 来源问题与局限性\n"
            f"- 外部适配器状态：{'已运行' if diagnostics.get('external_enabled') else '未完整运行'}；{source_error_text}。\n"
            "- 检索范围以本次实际成功返回的公开来源和适配器为准，不能据此声称覆盖全部公开库。\n"
            "- 物种、模态或基因组版本未标注时只记为未知；只有元数据明确冲突时才记为不匹配。\n"
            "- 候选体积优先报告分析就绪文件组合；整项研究的全部原始文件体积单独保留，下载前仍需逐文件审核 manifest。\n"
            "- 关键词检索可能遗漏命名不规范或受控访问的数据集。\n\n"
            "### 下一步\n"
            "检索结果已保留，本阶段不会自动进入下载。你可以继续要求查看某个候选、按文件大小或格式筛选、生成下载清单，或调整条件重新检索。"
        )
        structure = (
            "使用标题：检索结果总结、核心发现、候选示例、来源问题与局限性、下一步。"
            "禁止输出任何评分、等级或综合分。必须区分 confirmed/partial/unknown/mismatch；unknown 绝不能表述为不符合。"
            "体积限制是软偏好，不能声称超限研究已被排除。所有数字只能来自 evidence，结尾必须给出完整、未截断的下一步。"
        )
    else:
        details = evidence.get("execution_stages") or []
        rendered = "\n".join(
            f"{index}. **{item['stage']}**（`{item['script']}`）：{item['description']}"
            for index, item in enumerate(details, 1)
        )
        fallback = (
            f"## QC 计划已生成\n\n已识别输入类型为 {evidence.get('input_kind', 'unknown')}，"
            f"将执行 {evidence.get('stage_count', 0)} 个受控 stages。\n\n"
            f"### 本地受控执行清单\n{rendered or '当前没有可执行 stage。'}\n\n"
            "以上只是计划，只有确认后才会提交到 tmux；执行后将产出标准化 cell × peak matrix、QC summary、data card 与 MANIFEST。"
        )
        structure = "使用标题：输入识别、QC 策略、执行计划、预期交付与下一步。"
    guidance = structure
    if stage == "qc":
        guidance += " 必须逐条解释 execution_stages 中的每一个 stage，并明确当前仍是计划。"
    else:
        guidance += " 所有数字只能来自 evidence，不能声称已覆盖未检索的数据源。"
    return llm_report(
        "公开数据检索结果" if stage == "search" else "QC 执行计划",
        evidence,
        fallback,
        guidance=guidance,
        max_tokens=1800 if stage == "qc" else 2200,
    )


app = FastAPI(title="CellNote Agent Web", version="0.1.0")


@app.on_event("startup")
def startup() -> None:
    initialize()
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "repo_root": str(REPO_ROOT), "step_api_enabled": bool(os.environ.get("STEP_API_KEY"))}


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("select * from projects order by updated_at desc").fetchall()
    return [row_to_dict(row) for row in rows]


@app.post("/api/projects")
def create_project(body: ProjectCreate) -> dict[str, Any]:
    project_id = uuid.uuid4().hex[:12]
    created = now()
    name = body.name.strip() if body.name and body.name.strip() else suggested_project_name()
    with connect() as conn:
        conn.execute("insert into projects values (?, ?, ?, ?)", (project_id, name, created, created))
    project_dir(project_id).mkdir(parents=True, exist_ok=True)
    add_message(project_id, "assistant", f"已创建项目“{name}”。请描述想搜索的数据，或粘贴已有 fragments、peak matrix、multiome 文件路径。")
    return require_project(project_id)


@app.get("/api/projects/{project_id}")
def project_overview(project_id: str) -> dict[str, Any]:
    project = require_project(project_id)
    with connect() as conn:
        gates = conn.execute("select * from gates where project_id = ? and status = 'pending' order by created_at desc", (project_id,)).fetchall()
        jobs = conn.execute("select * from jobs where project_id = ? order by created_at desc limit 20", (project_id,)).fetchall()
    job_rows = [hydrate_job(project_id, row_to_dict(row)) for row in jobs]
    for job in job_rows:
        log_path = Path(job["log_path"])
        job["log_tail"] = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.exists() else "任务已创建，等待执行…"
    return {"project": project, "state": read_state(project_id), "gates": [row_to_dict(row) for row in gates], "jobs": job_rows, "artifacts": artifacts(project_id), "messages": messages_for_project(project_id)}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict[str, Any]:
    """Delete one explicitly selected project and its isolated workspace."""
    require_project(project_id)
    with connect() as conn:
        job_rows = conn.execute("select tmux_session from jobs where project_id = ?", (project_id,)).fetchall()
    for row in job_rows:
        session = row["tmux_session"]
        if session:
            subprocess.run(["tmux", "kill-session", "-t", f"={session}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    target = project_dir(project_id)
    if target.exists():
        shutil.rmtree(target)
    with connect() as conn:
        conn.execute("delete from gates where project_id = ?", (project_id,))
        conn.execute("delete from jobs where project_id = ?", (project_id,))
        conn.execute("delete from messages where project_id = ?", (project_id,))
        conn.execute("delete from projects where id = ?", (project_id,))
    return {"deleted": project_id}


@app.post("/api/projects/{project_id}/chat")
def chat(project_id: str, body: ChatRequest) -> dict[str, Any]:
    require_project(project_id)
    prompt = body.prompt.strip()
    refine_project_name(project_id, prompt)
    add_message(project_id, "user", prompt)
    if any(token in prompt.lower() for token in ("暂停", "pause", "stop qc", "停止下载")):
        job = latest_pauseable_job(project_id)
        if not job:
            message = "当前没有可暂停的下载或 QC 任务。已完成任务不能暂停；请在右侧查看下一步操作。"
        else:
            try:
                paused = pause_managed_job(project_id, job["id"])
                label = {"download": "下载", "qc": "QC", "crawl": "搜索", "manifest": "下载清单生成"}.get(paused["kind"], paused["kind"])
                verb = "已暂停" if paused["status"] == "paused" else "已中断"
                message = f"{verb}{label}任务，当前日志和已产生结果已保留。请在右侧选择下一步操作。"
            except HTTPException as exc:
                message = f"未能暂停：{exc.detail}"
        add_message(project_id, "assistant", message)
        return {"message": message, "action": "pause"}
    if handle_pending_gate_input(project_id, prompt):
        return {"message": "已处理当前交互节点。", "action": "gate_response"}
    config, state = agent_config(project_id), agent_state(project_id)
    if smallest_download_request(prompt):
        catalog = state.last_candidate_catalog
        if not catalog or not catalog.exists():
            message = "当前项目还没有可供比较的候选数据集。请先完成一次公开数据搜索，我会保留候选表，之后再按实际可下载体积选择最小的数据集。"
            add_message(project_id, "assistant", message)
            return {"message": message, "action": "select_smallest_download"}
        selected = smallest_downloadable_candidate_ids(catalog, n=1)
        if not selected:
            message = "当前候选表中没有带可下载文件和有效体积信息的数据集，暂时无法可靠选择最小项。你可以让我扩大搜索范围或指定 accession。"
            add_message(project_id, "assistant", message)
            return {"message": message, "action": "select_smallest_download"}
        selection_message = candidate_selection_summary(catalog, selected)
        add_message(project_id, "assistant", selection_message)
        run_background(
            project_id,
            "manifest",
            {"candidate_ids": selected, "selection_strategy": "smallest_downloadable_dataset"},
            lambda log, job_id: run_manifest(project_id, selected, log, job_id),
        )
        message = "我正在核对该数据集的具体远程文件并生成下载清单。清单完成后会展示文件名、角色和预计体积；只有你确认后才会开始真实下载。"
        add_message(project_id, "assistant", message)
        return {"message": message, "action": "select_smallest_download"}
    paths = re.findall(r"/(?:[^\s'\"]+)", prompt)
    try:
        local_path = is_allowed_path(paths[0]) if paths else None
    except HTTPException:
        local_path = None
    action = {"action": "run_analysis", "input_path": str(local_path)} if local_path else (step_action(prompt, config, state) or deterministic_action(prompt, state, config))
    action_name = str((action or {}).get("action") or "explain")
    message_added = False

    if action_name == "crawl":
        defaults = default_web_search_preferences(prompt, action)
        search_gate = add_gate(project_id, "search", {
            "query": str((action or {}).get("query") or prompt),
            "original_query": prompt,
            "preferences": defaults,
        })
        stored = add_message(project_id, "assistant", conversation_gate_prompt("search", search_gate["payload"]), event="search_clarification")
        message = stored["content"]
        message_added = True
    elif action_name in {"select_candidates", "generate_manifest"}:
        selected = [int(value) for value in (action or {}).get("candidate_ids", []) if str(value).isdigit()]
        if not selected:
            message = "请先在右侧候选表勾选数据集；也可以直接说“选择 1,3 生成下载清单”。"
        elif not state.last_candidate_catalog:
            message = "当前项目还没有候选数据集。请先完成一次搜索。"
        else:
            run_background(project_id, "manifest", {"candidate_ids": selected}, lambda log, job_id: run_manifest(project_id, selected, log, job_id))
            message = f"已开始为候选 {', '.join(map(str, selected))} 生成下载清单；右侧会显示整理进度。"
    elif action_name == "list_candidates":
        rows = csv_rows(state.last_candidate_catalog) if state.last_candidate_catalog and state.last_candidate_catalog.exists() else []
        message = f"当前有 {len(rows)} 个候选数据集。请在右侧查看、勾选并生成下载清单。" if rows else "当前没有候选数据集，请先搜索公开数据。"
    elif action_name == "download_manifest":
        manifest = state.last_manifest
        if manifest and manifest.exists():
            add_manifest_review_gate(project_id, manifest)
            message = "已找到当前项目的下载清单。请在右侧查看体积与文件角色；可下载、取消，或继续用自然语言修改清单。"
        else:
            message = "当前没有可下载的清单。请先选择候选并生成下载清单。"
    elif action_name == "run_analysis":
        input_path = str((action or {}).get("input_path") or "")
        try:
            path = is_allowed_path(input_path) if input_path else None
        except HTTPException as exc:
            path = None
            path_error = str(exc.detail)
        else:
            path_error = ""
        if path:
            context = direct_analysis_context(config, prompt, path)
            write_state(project_id, {"analysis_context": context})
            analysis_gate = add_gate(project_id, "analysis", {"context": context})
            stored = add_message(project_id, "assistant", conversation_gate_prompt("analysis", analysis_gate["payload"]), event="analysis_clarification")
            message = stored["content"]
            message_added = True
        else:
            message = f"{path_error}。" if path_error else "请粘贴服务器上的完整文件或目录路径，或说明要分析的已下载文件；我会先检测 fragments、peak matrix 或 multiome 类型。"
    elif action_name == "crawl_status":
        message = "右侧“任务进度与日志”会显示当前搜索状态；已完成任务收纳在历史栏中。"
    elif action_name == "list_skills":
        message = "可执行的受控路径包括：scATAC fragments（SnapATAC2 QC）、scATAC peak matrix（AnnData/Scanpy QC）、multiome（RNA+ATAC 联合 QC），以及搜索、清单、下载与完整性校验。"
    elif action_name == "external_tools":
        message = "搜索会使用公开来源与已配置的外部适配层；检索结果会先经过文件角色识别和 pipeline-fit 分诊，再由你确认下载。"
    elif action_name == "pbmc500_demo":
        message = "已识别为 PBMC500 demo 请求。请使用“搜索 PBMC scATAC”快捷任务，或直接说明希望运行哪一条 QC 路径；Web 端会先展示受控执行计划。"
    else:
        message = intelligent_reply(project_id, prompt)
    if not message_added:
        stored = add_message(project_id, "assistant", message, event=action_name)
        message = stored["content"]
    return {"message": message, "action": action_name}


@app.post("/api/projects/{project_id}/gates/{gate_id}")
def respond_gate(project_id: str, gate_id: str, body: GateResponse) -> dict[str, Any]:
    gate = resolve_gate(project_id, gate_id)
    payload = {**gate["payload"], **body.payload}
    if gate["kind"] == "search":
        if body.payload.get("cancelled"):
            add_message(project_id, "assistant", "已取消搜索条件确认，尚未启动 crawler。")
            return {"cancelled": True}
        payload["preferences"] = normalize_web_search_preferences(
            str(payload.get("original_query") or payload.get("query") or ""),
            dict(payload.get("preferences") or {}),
        )
        job = run_background(project_id, "crawl", payload, lambda log, job_id: run_search(project_id, payload, log, job_id))
        add_message(project_id, "assistant", "搜索任务已启动。右侧会实时显示检索、外部来源补充和候选整理进度。")
        return {"job": job}
    if gate["kind"] == "candidate_triage":
        catalog = agent_state(project_id).last_candidate_catalog
        choice = str(body.payload.get("choice") or "manual")
        if not catalog or not catalog.exists():
            raise HTTPException(409, "候选目录不存在；请重新搜索")
        if choice == "auto_ready":
            selected = payload.get("ready_ids") or smallest_candidate_ids(catalog, n=1)
        elif choice == "smoke":
            selected = payload.get("smallest_ids") or smallest_candidate_ids(catalog, n=1)
        elif choice == "manual":
            add_message(project_id, "assistant", "已进入手动选择。请在右侧候选表勾选一个或多个数据集，再点击“生成下载清单”。")
            return {"selected": []}
        elif choice == "filter_ready":
            write_state(project_id, {"candidate_filter": "analysis_ready"})
            add_message(project_id, "assistant", "已收紧为 analysis-ready / 推荐候选。你可以继续手动勾选，或稍后再生成下载清单。")
            return {"selected": []}
        else:
            add_message(project_id, "assistant", "已停在候选确认节点。候选与搜索记录已保留；需要时可继续勾选候选或重新搜索。")
            return {"selected": []}
        selected = [int(value) for value in selected]
        job = run_background(project_id, "manifest", {"candidate_ids": selected}, lambda log, job_id: run_manifest(project_id, selected, log, job_id))
        add_message(project_id, "assistant", f"已按分诊策略选择候选 {', '.join(map(str, selected))} 并开始生成下载清单；完成后可继续修改清单或确认下载。")
        return {"job": job, "selected": selected}
    if gate["kind"] == "manifest_review":
        manifest = Path(str(payload.get("manifest") or "")).resolve()
        if not manifest.is_relative_to(project_dir(project_id)) or not manifest.exists():
            raise HTTPException(403, "invalid manifest")
        choice = str(body.payload.get("choice") or "cancel")
        if choice == "download":
            download_gate = add_gate(project_id, "download", {"manifest": str(manifest), "rows": csv_rows(manifest, limit=500)})
            add_message(project_id, "assistant", conversation_gate_prompt("download", download_gate["payload"]))
            return {"gate": download_gate}
        if choice == "cancel":
            add_message(project_id, "assistant", "已停在下载确认节点，当前清单已保留。你可以稍后输入“下载刚才的 manifest”，或继续说明如何修改清单。")
            return {"cancelled": True}
        instruction = str(body.payload.get("instruction") or "").strip()
        rows = csv_rows(manifest, limit=500)
        plan = step_manifest_edit_plan(instruction, rows) or deterministic_manifest_edit_plan(instruction, rows)
        action = str(plan.get("action") or "").lower()
        if action == "download":
            download_gate = add_gate(project_id, "download", {"manifest": str(manifest), "rows": rows})
            add_message(project_id, "assistant", conversation_gate_prompt("download", download_gate["payload"]))
            return {"gate": download_gate}
        if action == "cancel":
            add_message(project_id, "assistant", "已保留当前下载清单，暂不开始下载。")
            return {"cancelled": True}
        updated_rows, message = apply_manifest_edit_plan(rows, plan)
        if updated_rows is None:
            add_message(project_id, "assistant", message)
            review_gate = add_manifest_review_gate(project_id, manifest)
            add_message(project_id, "assistant", conversation_gate_prompt("manifest_review", review_gate["payload"]))
            return {"gate": review_gate, "unchanged": True}
        if not updated_rows:
            add_message(project_id, "assistant", "这个修改会让清单变空，因此没有保存。请换一个条件，例如“只保留最小 1 个文件”。")
            review_gate = add_manifest_review_gate(project_id, manifest)
            add_message(project_id, "assistant", conversation_gate_prompt("manifest_review", review_gate["payload"]))
            return {"gate": review_gate, "unchanged": True}
        if len(updated_rows) == len(rows):
            add_message(project_id, "assistant", f"{message} 当前清单未改变；可以改说“只保留最小 1 个文件”“控制在 5GB 内”或“删除 1,2”。")
            return {"unchanged": True}
        revised = write_manifest_revision(agent_config(project_id), agent_state(project_id), updated_rows)
        write_state(project_id, {"manifest": str(revised), "downloads_visible": False})
        add_message(project_id, "assistant", f"{message} 已生成新的下载清单，请继续查看后选择下载、取消或其他修改。")
        review_gate = add_manifest_review_gate(project_id, revised)
        add_message(project_id, "assistant", conversation_gate_prompt("manifest_review", review_gate["payload"]))
        return {"gate": review_gate, "manifest": str(revised)}
    if gate["kind"] == "analysis":
        if body.payload.get("cancelled"):
            add_message(project_id, "assistant", "已取消 QC 计划生成，尚未执行任何分析脚本。")
            return {"cancelled": True}
        context = payload["context"]
        mode = body.payload.get("analysis_mode", "full_qc")
        context["analysis_mode"] = mode
        context["safe_packaging_only"] = mode == "packaging_only"
        context["qc_params"] = body.payload.get("qc_params", {})
        plan = deterministic_analysis_plan(context)
        commands = canonical_analysis_commands(agent_config(project_id), context, plan)
        write_state(project_id, {"analysis_context": context})
        add_message(project_id, "assistant", stage_summary("qc", {
            "dataset_id": context.get("dataset_id"),
            "input_path": context.get("input_path"),
            "input_kind": context.get("input_kind"),
            "size_risk": context.get("size_risk"),
            "genome_hint": context.get("genome_hint"),
            "analysis_mode": context.get("analysis_mode"),
            "qc_parameters": context.get("qc_params"),
            "stage_count": len(commands),
            "execution_stages": qc_stage_details(commands),
            "next_action": "在右侧选择确认执行，才会把白名单 QC scripts 提交到 tmux。",
        }))
        execute_gate = add_gate(project_id, "analysis_execute", {"context": context, "plan": plan})
        add_message(project_id, "assistant", conversation_gate_prompt("analysis_execute", execute_gate["payload"]))
        return {"plan": plan, "commands": commands, "gate": execute_gate}
    if gate["kind"] == "analysis_execute":
        if not body.payload.get("confirm"):
            add_message(project_id, "assistant", "已取消 QC 执行；计划和输入检测结果已保留。")
            return {"cancelled": True}
        context, plan = payload["context"], payload["plan"]
        def start(log: Path, job_id: str):
            set_job_progress(project_id, job_id, 5, "正在提交 QC 到 tmux")
            commands = canonical_analysis_commands(agent_config(project_id), context, plan)
            append_log(log, "\n".join("$ " + " ".join(command) for command in commands))
            return run_long_commands(commands, config=agent_config(project_id), job_name=f"web-qc-{context.get('dataset_id', 'dataset')}")
        job = run_background(project_id, "qc", {"dataset_id": context.get("dataset_id")}, start)
        fallback = "## QC 已提交\n\n已将确认过的白名单 QC stages 提交到 tmux；当前开始执行，不会由大模型直接生成生信结果。右侧仅显示实时进度、日志和最终文件。\n\n**下一步：**可随时在聊天框输入“暂停”；完成后我会总结交付物和可继续的分析操作。"
        add_message(project_id, "assistant", llm_report(
            "QC 任务提交说明",
            {
                "status": "submitted",
                "execution_backend": "本地白名单 scripts，通过 tmux 执行",
                "llm_role": "仅解释计划与结果，不生成生信计算结果",
                "progress_location": "Web 任务进度与日志区域",
                "interruptible": True,
                "next_action": "等待执行；用户可随时输入暂停",
            },
            fallback,
            guidance="说明任务刚提交而非已完成，并强调可暂停和本地受控执行。",
        ))
        return {"job": job}
    if gate["kind"] == "download":
        if not body.payload.get("confirm"):
            add_message(project_id, "assistant", "已取消下载；当前清单已保留，未启动 fetch。")
            return {"cancelled": True}
        manifest = Path(payload["manifest"]).resolve()
        if not manifest.is_relative_to(project_dir(project_id)):
            raise HTTPException(403, "invalid manifest")
        raw_store = project_dir(project_id) / "raw"
        config = agent_config(project_id)
        def start(log: Path, job_id: str):
            write_state(project_id, {"downloads_visible": True})
            set_job_progress(project_id, job_id, 5, "正在检查下载清单")
            plan_command = ["./cell-note", "download", "--stage", "plan", "--manifest", str(manifest), "--store", str(raw_store)]
            append_log(log, "$ " + " ".join(plan_command))
            planned = subprocess.run(plan_command, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            append_log(log, planned.stdout)
            if planned.returncode:
                raise RuntimeError(f"download plan failed with exit code {planned.returncode}")
            set_job_progress(project_id, job_id, 15, "下载计划已确认，正在提交 fetch")
            commands = [
                ["./cell-note", "download", "--stage", "fetch", "--manifest", str(manifest), "--store", str(raw_store), "--enable_fetch"],
                ["./cell-note", "download", "--stage", "verify", "--manifest", str(manifest), "--store", str(raw_store)],
            ]
            append_log(log, "\n".join("$ " + " ".join(command) for command in commands))
            return run_long_commands(commands, config=config, job_name="web-download")
        job = run_background(project_id, "download", {"manifest": str(manifest), "store": str(raw_store)}, start)
        fallback = "## 下载已提交\n\n将依次执行 **plan → fetch → verify**：先核对清单，再下载，最后验证文件完整性。文件将保存到当前项目独立工作目录的 `raw/` 下。\n\n**下一步：**右侧会展示进度和文件；如需中断，直接在聊天框输入“暂停”。"
        add_message(project_id, "assistant", llm_report(
            "下载任务提交说明",
            {
                "status": "submitted",
                "controlled_stages": ["plan", "fetch", "verify"],
                "storage_scope": "当前项目隔离的 raw 目录",
                "progress_location": "Web 任务进度与文件区域",
                "interruptible": True,
                "next_action": "等待下载和校验；用户可随时输入暂停",
            },
            fallback,
            guidance="解释三个下载阶段和当前未完成状态，不要声称文件已经验证。",
        ))
        return {"job": job}
    raise HTTPException(400, "unsupported gate")


@app.get("/api/projects/{project_id}/candidates")
def candidates(project_id: str) -> dict[str, Any]:
    state = read_state(project_id)
    raw_path = str(state.get("candidate_catalog") or "")
    path = Path(raw_path) if raw_path else None
    rows = csv_rows(path) if path and path.is_file() else []
    if state.get("candidate_filter") == "analysis_ready":
        rows = [
            row for row in rows
            if row.get("best_file_role") in {"peak_matrix", "fragments"}
            or row.get("pipeline_fit") == "high"
            or str(row.get("recommended", "")).lower() in {"yes", "true", "1"}
        ]
    try:
        display_limit = max(1, int(state.get("candidate_display_limit") or len(rows) or 1))
    except (TypeError, ValueError):
        display_limit = len(rows)
    return {"rows": rows[:display_limit], "total": len(rows), "display_limit": display_limit, "catalog": str(path) if path and path.is_file() else ""}


@app.post("/api/projects/{project_id}/manifest")
def create_manifest(project_id: str, body: GateResponse) -> dict[str, Any]:
    selected = [int(value) for value in body.payload.get("candidate_ids", [])]
    state = agent_state(project_id)
    if not selected or not state.last_candidate_catalog:
        raise HTTPException(400, "请选择至少一个候选数据集")
    return {"job": run_background(project_id, "manifest", {"candidate_ids": selected}, lambda log, job_id: run_manifest(project_id, selected, log, job_id))}


@app.post("/api/projects/{project_id}/jobs/{job_id}/pause")
def pause_task(project_id: str, job_id: str) -> dict[str, Any]:
    job = pause_managed_job(project_id, job_id)
    label = {"download": "下载", "qc": "QC", "crawl": "搜索", "manifest": "下载清单生成"}.get(job["kind"], job["kind"])
    verb = "已暂停" if job["status"] == "paused" else "已中断"
    add_message(project_id, "assistant", f"{label}{verb}。请在右侧选择继续、重新运行或保留当前结果。")
    return job


@app.post("/api/projects/{project_id}/jobs/{job_id}/resume")
def resume_task(project_id: str, job_id: str) -> dict[str, Any]:
    """Resume downloads in place, or submit a fresh controlled QC run."""
    job = get_job(project_id, job_id)
    if job["kind"] not in {"download", "qc"} or job["status"] != "paused":
        raise HTTPException(409, "only a paused download or QC task can be resumed")
    if job["kind"] == "qc":
        context = read_state(project_id).get("analysis_context")
        if not isinstance(context, dict):
            raise HTTPException(409, "缺少 QC 上下文；请重新检测输入后再执行")
        plan = deterministic_analysis_plan(context)
        commands = canonical_analysis_commands(agent_config(project_id), context, plan)
        def start(log: Path, new_job_id: str):
            set_job_progress(project_id, new_job_id, 5, "正在重新提交 QC 到 tmux")
            append_log(log, "\n".join("$ " + " ".join(command) for command in commands))
            return run_long_commands(commands, config=agent_config(project_id), job_name=f"web-qc-{context.get('dataset_id', 'dataset')}")
        new_job = run_background(project_id, "qc", {"dataset_id": context.get("dataset_id"), "resumed_from": job_id}, start)
        with connect() as conn:
            conn.execute("update gates set status = ?, resolved_at = ? where project_id = ? and kind = ? and status = 'pending'", ("resolved", now(), project_id, "paused_task"))
        add_message(project_id, "assistant", "已重新提交 QC。为保证结果一致性，将从受控 QC 流程重新运行，而不是跳过未知的中间阶段。")
        return new_job
    manifest = Path(str(job["detail"].get("manifest") or "")).resolve()
    store = Path(str(job["detail"].get("store") or "")).resolve()
    root = project_dir(project_id)
    if not manifest.is_relative_to(root) or not store.is_relative_to(root):
        raise HTTPException(403, "invalid paused download paths")
    commands = [
        ["./cell-note", "download", "--stage", "fetch", "--manifest", str(manifest), "--store", str(store), "--enable_fetch"],
        ["./cell-note", "download", "--stage", "verify", "--manifest", str(manifest), "--store", str(store)],
    ]
    append_log(Path(job["log_path"]), "[resume] continuing fetch/verify from retained partial files")
    result = run_long_commands(commands, config=agent_config(project_id), job_name="web-download")
    update_job(project_id, job_id, "submitted", {"progress": max(15, int(job["detail"].get("progress", 0))), "stage": "已继续下载", "result": result}, tmux_session=result.get("session"))
    with connect() as conn:
        conn.execute("update gates set status = ?, resolved_at = ? where project_id = ? and kind = ? and status = 'pending'", ("resolved", now(), project_id, "paused_task"))
    add_message(project_id, "assistant", "下载已继续。系统将复用保留文件，并在完成后执行完整性校验。")
    return get_job(project_id, job_id)


@app.get("/api/projects/{project_id}/jobs/{job_id}")
def job(project_id: str, job_id: str) -> dict[str, Any]:
    value = get_job(project_id, job_id)
    log = Path(value["log_path"])
    value["log_tail"] = log.read_text(encoding="utf-8", errors="replace")[-12000:] if log.exists() else ""
    if value["status"] == "submitted" and value.get("tmux_session"):
        running = subprocess.run(["tmux", "has-session", "-t", f"={value['tmux_session']}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
        value["tmux_running"] = running
    return value


def manifest_download_rows(project_id: str) -> list[dict[str, Any]]:
    state = read_state(project_id)
    manifest = Path(str(state.get("manifest") or ""))
    if not state.get("downloads_visible") or not manifest.is_file():
        return []
    root = project_dir(project_id)
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(csv_rows(manifest, limit=10_000), 1):
        url = row.get("source_uri") or row.get("url") or row.get("download_url") or row.get("remote_url") or ""
        dataset_id = row.get("dataset_id") or row.get("accession") or f"dataset_{index}"
        filename = Path(urlsplit(url).path).name or row.get("artifact_id") or row.get("file_id") or f"artifact_{index}"
        explicit = row.get("local_path") or row.get("path")
        path = Path(explicit) if explicit else root / "raw" / dataset_id / filename
        if not path.is_absolute():
            path = root / "raw" / path
        if not path.is_relative_to(root):
            continue
        expected_text = row.get("size_bytes") or row.get("file_size") or row.get("bytes") or ""
        expected = int(expected_text) if str(expected_text).isdigit() else 0
        actual = path.stat().st_size if path.exists() else 0
        progress = min(100, int(actual * 100 / expected)) if expected else (100 if actual else 0)
        rows.append({"path": str(path.relative_to(root)), "size": actual, "expected_size": expected, "progress": progress, "downloading": progress < 100})
    return rows


def candidate_triage_payload(catalog: Path) -> dict[str, Any]:
    """Build optional next-step choices when the user explicitly requests them."""
    rows = csv_rows(catalog, limit=500)
    ready_ids = analysis_ready_candidate_ids(catalog)
    return {
        "candidate_count": len(rows),
        "ready_ids": ready_ids,
        "smallest_ids": smallest_candidate_ids(catalog, n=1),
    }


def add_manifest_review_gate(project_id: str, manifest: Path) -> dict[str, Any]:
    rows = csv_rows(manifest, limit=500)
    total_bytes = sum(int(row.get("size_bytes") or 0) for row in rows)
    return add_gate(project_id, "manifest_review", {
        "manifest": str(manifest),
        "rows": rows,
        "total_size_gb": round(total_bytes / 1e9, 3),
    })


def pending_gate(project_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "select * from gates where project_id = ? and status = 'pending' order by created_at desc limit 1",
            (project_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def infer_web_search_slots(text: str) -> dict[str, Any]:
    """Extend shared CLI inference with concrete disease/tissue labels needed by Web cards."""
    inferred = infer_search_slots(text)
    if inferred.get("tissue_or_disease") == "特定疾病" and inferred.get("tissue_hint") in {
        "specific_disease_mentioned", "specific_tissue_mentioned",
    }:
        inferred["tissue_hint"] = ""
    if "tissue_or_disease" not in inferred:
        disease_match = re.search(
            r"([\u4e00-\u9fff]{1,10}(?:癌|肉瘤|白血病|淋巴瘤|综合征|疾病))|"
            r"((?:lung|breast|prostate|colorectal|pancreatic|liver|brain|kidney)\s+(?:cancer|tumou?r|carcinoma))",
            text,
            flags=re.IGNORECASE,
        )
        if disease_match:
            inferred["tissue_or_disease"] = "特定疾病"
            inferred["tissue_hint"] = disease_match.group(0).strip()
    if inferred.get("tissue_or_disease") == "特定疾病" and not inferred.get("tissue_hint"):
        tissue_match = re.search(
            r"(PBMC|外周血|脑(?:组织)?|心脏|肝(?:脏)?|肺(?:组织)?|肾(?:脏)?|"
            r"brain|heart|liver|lung|kidney)",
            text,
            flags=re.IGNORECASE,
        )
        if tissue_match:
            inferred["tissue_hint"] = tissue_match.group(0)
    return inferred


def default_web_search_preferences(prompt: str, action: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the Web search state from the same deterministic inference used by the CLI."""
    action = action or {}
    inferred = infer_web_search_slots(prompt)
    raw_limit = action.get("limit") or inferred.get("candidate_limit_value")
    try:
        candidate_limit = max(1, min(int(raw_limit), 500)) if raw_limit is not None else 10
    except (TypeError, ValueError):
        candidate_limit = 10
    sources = [
        str(source)
        for source in (action.get("sources") or ["geo", "sra", "literature"])
        if str(source) in {"geo", "sra", "literature"}
    ]
    acquisition = str(inferred.get("acquisition") or "处理后的矩阵或 fragments")
    return {
        "data_type": inferred.get("data_type") or "纯 scATAC-seq",
        "tissue_or_disease": inferred.get("tissue_or_disease") or "不限/广泛搜集",
        "tissue_hint": inferred.get("tissue_hint") or "",
        "acquisition": acquisition,
        "candidate_limit": candidate_limit,
        "candidate_limit_request": str(inferred.get("candidate_limit") or candidate_limit),
        "size_limit": inferred.get("size_limit") or "20GB以内",
        "size_limit_gb": inferred.get("size_limit_gb", 20.0),
        "target_genome_build": inferred.get("target_genome_build") or "GRCh38",
        "sources": sources or ["geo", "sra", "literature"],
        "prefer_analysis_ready": bool(inferred.get("prefer_analysis_ready", "处理后" in acquisition or "fragments" in acquisition.lower())),
        "inferred_slots": inferred,
        "user_note": "",
    }


def normalize_web_search_preferences(query: str, preferences: dict[str, Any]) -> dict[str, Any]:
    """Validate browser/chat search preferences before they reach crawler commands."""
    normalized = dict(preferences)
    inferred = dict(normalized.get("inferred_slots") or infer_web_search_slots(query))
    normalized["inferred_slots"] = inferred
    normalized["data_type"] = str(normalized.get("data_type") or inferred.get("data_type") or "纯 scATAC-seq")
    normalized["tissue_or_disease"] = str(normalized.get("tissue_or_disease") or inferred.get("tissue_or_disease") or "不限/广泛搜集")
    normalized["tissue_hint"] = str(normalized.get("tissue_hint") or inferred.get("tissue_hint") or "").strip()
    normalized["acquisition"] = str(normalized.get("acquisition") or inferred.get("acquisition") or "处理后的矩阵或 fragments")
    normalized["target_genome_build"] = str(normalized.get("target_genome_build") or inferred.get("target_genome_build") or "GRCh38")
    normalized["size_limit"] = str(normalized.get("size_limit") or inferred.get("size_limit") or "20GB以内")
    try:
        normalized["size_limit_gb"] = float(normalized.get("size_limit_gb", inferred.get("size_limit_gb", 20.0)))
    except (TypeError, ValueError):
        normalized["size_limit_gb"] = 20.0
    request = str(normalized.get("candidate_limit_request") or normalized.get("candidate_limit") or "10").strip()
    normalized["candidate_limit_request"] = request
    match = re.search(r"\d+", request)
    normalized["candidate_limit"] = max(1, min(int(match.group(0)), 500)) if match else None
    normalized["sources"] = [
        str(source)
        for source in (normalized.get("sources") or ["geo", "sra", "literature"])
        if str(source) in {"geo", "sra", "literature"}
    ] or ["geo", "sra", "literature"]
    normalized["user_note"] = str(normalized.get("user_note") or "").strip()[:500]
    normalized["prefer_analysis_ready"] = bool(
        normalized.get("prefer_analysis_ready")
        or "处理后" in normalized["acquisition"]
        or "fragments" in normalized["acquisition"].lower()
    )
    return normalized


def revise_web_search_preferences(query: str, current: dict[str, Any], instruction: str) -> dict[str, Any]:
    """Apply a free-form chat correction without resolving the search confirmation gate."""
    revised = dict(current)
    inferred = infer_web_search_slots(instruction)
    for key in ("data_type", "tissue_or_disease", "tissue_hint", "acquisition", "target_genome_build", "size_limit", "size_limit_gb", "prefer_analysis_ready"):
        if key in inferred:
            revised[key] = inferred[key]
    if "candidate_limit" in inferred:
        revised["candidate_limit_request"] = str(inferred["candidate_limit"])
        revised["candidate_limit"] = inferred.get("candidate_limit_value")
    elif any(word in instruction.lower() for word in ("所有", "全部", "不限数量", "不设上限", "尽量多", "all")):
        revised["candidate_limit_request"] = instruction.strip()
        revised["candidate_limit"] = None
    source_map = {"geo": "geo", "sra": "sra", "ena": "sra", "文献": "literature", "literature": "literature"}
    mentioned_sources = [value for token, value in source_map.items() if token in instruction.lower()]
    if mentioned_sources:
        revised["sources"] = list(dict.fromkeys(mentioned_sources))
    previous_note = str(revised.get("user_note") or "").strip()
    revised["user_note"] = " ".join(part for part in (previous_note, instruction.strip()) if part)[-500:]
    inferred_slots = dict(revised.get("inferred_slots") or {})
    inferred_slots.update({key: value for key, value in inferred.items() if key != "source_query"})
    revised["inferred_slots"] = inferred_slots
    return normalize_web_search_preferences(query, revised)


def update_pending_gate_payload(project_id: str, gate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        conn.execute(
            "update gates set payload = ? where id = ? and project_id = ? and status = 'pending'",
            (json.dumps(payload, ensure_ascii=False), gate_id, project_id),
        )
    return get_gate(project_id, gate_id)


def conversation_gate_prompt(kind: str, payload: dict[str, Any]) -> str:
    """The browser and terminal expose the same choices, through chat text."""
    titles = {
        "search": "搜索条件确认",
        "candidate_triage": "候选数据集下一步",
        "manifest_review": "下载清单确认",
        "analysis": "输入识别与分析模式",
        "analysis_execute": "QC 执行确认",
        "download": "最终下载确认",
        "paused_task": "任务已暂停",
    }
    if kind == "search":
        return (
            "### 搜索条件确认\n"
            "我已先从你的描述中提取明确条件。下方只显示仍需确认的项目；你可以直接勾选，也可以在聊天框补充或修改条件。"
            "确认前不会启动 crawler。"
        )
    return f"### {titles.get(kind, '下一步确认')}\n请直接在下方交互卡中勾选或点击你的选择；无需在输入框填写编号。"
    if kind == "candidate_triage":
        counts = payload.get("fit_counts", {})
        return ("### 候选数据集分诊\n"
                f"共 {payload.get('candidate_count', 0)} 个候选（pipeline fit：high={counts.get('high', 0)}，medium={counts.get('medium', 0)}，low={counts.get('low', 0)}）。\n\n"
                "1. 自动选 analysis-ready / 推荐候选并生成清单（推荐）\n"
                "2. 烟雾测试：只选体量最小的 1 个候选\n"
                "3. 手动选择候选编号\n"
                "4. 仅显示 analysis-ready，稍后决定\n"
                "5. 停在这里\n\n直接回复编号。")
    if kind == "manifest_review":
        return ("### 下载清单确认\n"
                f"当前清单含 {len(payload.get('rows', []))} 个文件，估计总量 {payload.get('total_size_gb', 0)} GB。\n\n"
                "1. 下载\n2. 取消\n3. 其他：直接输入要求，例如“只保留最小 1 个文件”“控制在 5GB 内”“删除 1,2”。\n\n"
                "请直接回复 `1`、`2` 或你的修改要求。")
    if kind == "analysis":
        context = payload.get("context", {})
        return ("### 输入识别与分析模式\n"
                f"检测到 `{context.get('input_kind', 'unknown')}`，规模风险：`{context.get('size_risk', 'standard')}`。\n"
                f"{context.get('reason', '')}\n\n"
                "1. 完整 QC（标准阈值，推荐）\n2. 仅 packaging（不做阈值过滤）\n3. 取消\n\n"
                "回复编号；如要定制阈值，可回复例如“1，宽松阈值”或“1，严格阈值”。")
    if kind == "analysis_execute":
        return "### QC 执行确认\n受控 QC 计划已展示，尚未运行。\n\n1. 确认提交到 tmux 执行\n2. 取消\n\n直接回复编号。"
    if kind == "download":
        return "### 最终下载确认\n将依次执行 plan → fetch → verify，并由 tmux 托管。\n\n1. 确认下载\n2. 取消\n\n直接回复编号。"
    if kind == "paused_task":
        label = "下载" if payload.get("kind") == "download" else "QC"
        return f"### {label}已暂停\n当前日志和已生成结果均已保留。\n\n1. {('继续下载' if label == '下载' else '重新运行 QC')}\n2. 保持暂停\n\n直接回复编号。"
    return "请直接在聊天框回复下一步要求。"


def tiered_qc_params(context: dict[str, Any], text: str) -> dict[str, Any]:
    lower = text.lower()
    tier = "strict" if any(word in lower for word in ("严格", "strict")) else "loose" if any(word in lower for word in ("宽松", "loose")) else "standard"
    kind = str(context.get("input_kind") or "")
    params: dict[str, Any] = {"genome_build": str(context.get("genome_build") or "GRCh38")}
    if kind == "peak_matrix":
        presets = {"loose": (500, 500, 5), "standard": (1000, 1000, 10), "strict": (2000, 2000, 20)}
        min_peaks, min_counts, min_cells = presets[tier]
        params.update({"min_peaks": min_peaks, "min_counts": min_counts, "min_cells_per_peak": min_cells})
        if context.get("size_risk") == "large":
            params["skip_embed_cluster"] = True
    elif kind == "fragments" or (kind == "multiome" and context.get("fragments") and not context.get("matrix")):
        presets = {"loose": (1000, 4.0), "standard": (3000, 6.0), "strict": (5000, 8.0)}
        min_fragments, min_tsse = presets[tier]
        params.update({"min_fragments": min_fragments, "min_tsse": min_tsse})
    elif kind == "multiome":
        presets = {"loose": (500, 500), "standard": (1000, 1000), "strict": (2000, 2000)}
        min_peaks, min_counts = presets[tier]
        params.update({"min_peaks": min_peaks, "min_counts": min_counts})
    return params


def handle_pending_gate_input(project_id: str, prompt: str) -> bool:
    """Route natural replies to the current conversation gate."""
    gate = pending_gate(project_id)
    if not gate:
        return False
    text = prompt.strip()
    compact = text.lower().replace("。", "").replace(".", "")
    kind = gate["kind"]
    if kind == "search":
        if compact in {"确认", "开始", "开始搜索", "确认搜索", "使用当前条件", "yes", "y"}:
            preferences = normalize_web_search_preferences(
                str(gate["payload"].get("original_query") or gate["payload"].get("query") or ""),
                dict(gate["payload"].get("preferences") or {}),
            )
            respond_gate(project_id, gate["id"], GateResponse(payload={"preferences": preferences}))
            return True
        if compact in {"取消", "重新描述", "cancel", "n", "no"}:
            respond_gate(project_id, gate["id"], GateResponse(payload={"cancelled": True}))
            return True
        query = str(gate["payload"].get("original_query") or gate["payload"].get("query") or "")
        preferences = revise_web_search_preferences(
            query,
            dict(gate["payload"].get("preferences") or {}),
            text,
        )
        update_pending_gate_payload(project_id, gate["id"], {**gate["payload"], "preferences": preferences})
        add_message(
            project_id,
            "assistant",
            "我已把这条补充要求合并到当前搜索条件。请检查下方更新后的选项；可以继续补充，或点击“确认并开始检索”。",
            event="search_clarification",
            facts={
                "updated_preferences": preferences,
                "current_stage": "搜索条件确认，crawler 尚未启动",
                "next_action": "继续修改条件，或使用交互卡确认开始检索",
            },
        )
        return True
    if kind == "candidate_triage":
        choices = {"1": "auto_ready", "2": "smoke", "3": "manual", "4": "filter_ready", "5": "stop"}
        if compact in choices:
            respond_gate(project_id, gate["id"], GateResponse(payload={"choice": choices[compact]}))
        else:
            add_message(project_id, "assistant", "请回复候选分诊编号 `1` 到 `5`。选择手动后，你可以在聊天框说“选择 1,3 生成下载清单”。")
        return True
    if kind == "manifest_review":
        payload = {"choice": "download"} if compact in {"1", "下载", "确认下载", "download"} else {"choice": "cancel"} if compact in {"2", "取消", "cancel"} else {"choice": "other", "instruction": text}
        respond_gate(project_id, gate["id"], GateResponse(payload=payload))
        return True
    if kind == "analysis":
        if compact.startswith("2") or compact in {"仅打包", "packaging"}:
            response = {"analysis_mode": "packaging_only", "qc_params": tiered_qc_params(gate["payload"]["context"], text)}
        elif compact.startswith("3") or compact in {"取消", "cancel"}:
            response = {"cancelled": True}
        elif compact.startswith("1") or any(word in compact for word in ("完整", "qc", "宽松", "标准", "严格", "loose", "strict")):
            response = {"analysis_mode": "full_qc", "qc_params": tiered_qc_params(gate["payload"]["context"], text)}
        else:
            add_message(project_id, "assistant", "请回复 `1`（完整 QC）、`2`（仅 packaging）或 `3`（取消）；可附加“宽松”或“严格”。")
            return True
        respond_gate(project_id, gate["id"], GateResponse(payload=response))
        return True
    if kind in {"analysis_execute", "download"}:
        confirm = compact in {"1", "确认", "确认执行", "确认下载", "yes", "y"}
        if not confirm and compact not in {"2", "取消", "cancel", "n", "no"}:
            add_message(project_id, "assistant", "请回复 `1` 确认，或回复 `2` 取消。")
            return True
        respond_gate(project_id, gate["id"], GateResponse(payload={"confirm": confirm}))
        return True
    if kind == "paused_task":
        if compact in {"1", "继续", "恢复", "resume"}:
            resume_task(project_id, str(gate["payload"]["job_id"]))
        else:
            add_message(project_id, "assistant", "任务保持暂停。需要继续时回复 `1`。")
        return True
    return False


def artifacts(project_id: str) -> list[dict[str, Any]]:
    root = project_dir(project_id)
    rows = {item["path"]: item for item in manifest_download_rows(project_id)}
    results_root = root / "results"
    if results_root.exists():
        for path in results_root.rglob("*"):
            if path.is_file():
                rows[str(path.relative_to(root))] = {"path": str(path.relative_to(root)), "size": path.stat().st_size, "expected_size": path.stat().st_size, "progress": 100, "downloading": False}
    return sorted(rows.values(), key=lambda item: item["path"])[-150:]


@app.get("/api/projects/{project_id}/artifacts")
def project_artifacts(project_id: str) -> list[dict[str, Any]]:
    require_project(project_id)
    return artifacts(project_id)


@app.get("/api/projects/{project_id}/artifacts/{relative_path:path}")
def download_artifact(project_id: str, relative_path: str):
    path = (project_dir(project_id) / relative_path).resolve()
    if not path.is_relative_to(project_dir(project_id)) or not path.is_file():
        raise HTTPException(404, "artifact not found")
    return FileResponse(path)


app.mount("/", StaticFiles(directory=STATIC_ROOT, html=True), name="static")
