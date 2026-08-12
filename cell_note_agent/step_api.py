#!/usr/bin/env python
"""StepFun API adapter for CellNoteAgent.

The adapter intentionally reads credentials from environment variables only:

    STEP_API_KEY       required
    STEP_API_BASE_URL  optional, default: https://api.stepfun.com/v1
    STEP_API_MODEL     optional, default: step-3.5-flash

StepFun exposes an OpenAI-compatible chat completions API, so this module keeps
the integration dependency-free by calling ``/chat/completions`` with ``urllib``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = "https://api.stepfun.com/v1"
DEFAULT_MODEL = "step-3.5-flash"


@dataclass(frozen=True)
class StepAPIConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "StepAPIConfig":
        api_key = os.environ.get("STEP_API_KEY")
        if not api_key:
            raise RuntimeError("STEP_API_KEY is not set. Export it in your shell or .env loader.")
        return cls(
            api_key=api_key,
            base_url=os.environ.get("STEP_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            model=os.environ.get("STEP_API_MODEL", DEFAULT_MODEL),
            timeout_seconds=int(os.environ.get("STEP_API_TIMEOUT_SECONDS", "60")),
        )


def chat_completion(
    messages: list[dict[str, str]],
    *,
    config: StepAPIConfig | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Call StepFun chat completions and return the decoded JSON response."""
    cfg = config or StepAPIConfig.from_env()
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        f"{cfg.base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"StepFun API HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"StepFun API network error: {exc}") from exc


def first_message_content(response: dict[str, Any]) -> str:
    """Extract assistant text from an OpenAI-compatible response."""
    try:
        return str(response["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected StepFun response shape: {response}") from exc


def executable_skills(skills: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return only skills whose deterministic runtime is available.

    Existing skills without an explicit status remain executable for backward
    compatibility.  Placeholders must opt out with ``status: planned`` or
    ``status: unavailable`` in their frontmatter.
    """
    return [
        item
        for item in skills
        if item.get("status", "executable").strip().lower()
        not in {"planned", "unavailable", "disabled"}
    ]


def plan_with_skills(
    user_request: str,
    skills: list[dict[str, str]],
    *,
    config: StepAPIConfig | None = None,
) -> str:
    """Ask StepFun to produce a Pi-executable skill plan.

    The plan is constrained to the local skill contracts and to the project rule
    that every accepted ATAC-bearing modality must end as a GRCh38 per-dataset peak matrix before handoff.
    """
    available = executable_skills(skills)
    if not available:
        raise RuntimeError("No executable local skills are available for StepFun planning.")
    skill_lines = "\n".join(
        f"- {item['name']}: {item.get('description', '')}" for item in available
    )
    system = (
        "You are the planning layer for CellNoteAgent. Produce concise Pi-coding-agent "
        "instructions only. Use the listed local skills; do not invent unavailable tools. "
        "When external SOPs are useful, route through external-skill-router and the "
        "configs/external_skills.json registry; never execute remote GitHub skill code directly. "
        "Project invariant: all scATAC, snATAC, and multiome ATAC inputs must end as a GRCh38 "
        "per-dataset cell x peak matrix. Do not plan cCRE mapping, tokenization, or standalone "
        "scRNA delivery. For each step, name the skill, stage, expected inputs, outputs, and "
        "whether human confirmation is required."
    )
    user = f"Available skills:\n{skill_lines}\n\nUser request:\n{user_request}"
    response = chat_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        config=config,
    )
    return first_message_content(response)


def _chat(args: argparse.Namespace) -> None:
    response = chat_completion(
        [{"role": "user", "content": args.prompt}],
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    print(first_message_content(response))


def _plan(args: argparse.Namespace) -> None:
    from cell_note_agent.pi_bridge import discover_skills

    print(plan_with_skills(args.prompt, discover_skills(args.skills_root)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CellNoteAgent StepFun API adapter.")
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="Send a raw chat prompt to StepFun.")
    chat.add_argument("prompt")
    chat.add_argument("--temperature", type=float, default=0.2)
    chat.add_argument("--max_tokens", type=int, default=2048)
    chat.set_defaults(func=_chat)

    plan = sub.add_parser("plan", help="Ask StepFun to plan a Pi skill workflow.")
    plan.add_argument("prompt")
    plan.add_argument("--skills_root", default="skills")
    plan.set_defaults(func=_plan)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except RuntimeError as exc:
        print(f"[step-api:error] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
