"""Shared helpers for sc-epi-curator stage scripts.

These scripts are the *execution source of truth* referenced by the SKILL.md contracts
under ``skills/``. Skills describe what/when/thresholds/failure/human-review; scripts do
the deterministic computation.

Current status: shared stage-dispatch helpers. Some stages are implemented by
project-specific scripts, while heavy bioinformatics stages still fail fast when their
optional runtime dependencies or inputs are missing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def stage_subdir(results_root: str, name: str, dataset_id: str | None = None) -> str:
    """Derive a per-stage output subdirectory under the confirmed results root.

    Dataset-scoped outputs use the documented layout, e.g.
    ``<results_root>/processed/<dataset_id>/``.
    """
    path = os.path.join(results_root, name, dataset_id) if dataset_id else os.path.join(results_root, name)
    os.makedirs(path, exist_ok=True)
    return path


def log_provenance(results_root: str, event: dict) -> None:
    """Append a provenance event (query/routing/decision/feedback)."""
    os.makedirs(results_root, exist_ok=True)
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with open(os.path.join(results_root, "provenance.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def require_files(*paths: str) -> None:
    """Validate declared inputs exist before doing work (fail fast)."""
    missing = [p for p in paths if p and not os.path.exists(p)]
    if missing:
        sys.exit(f"[error] missing required input(s): {missing}")


def software_versions(*packages: str) -> dict:
    """Python + installed package versions for qc_summary provenance.

    Missing packages are recorded as "not-installed" rather than failing:
    version capture must never break a pipeline stage.
    """
    import platform
    from importlib import metadata

    versions = {"python": platform.python_version()}
    for name in packages:
        try:
            versions[name] = metadata.version(name)
        except Exception:
            versions[name] = "not-installed"
    return versions


def file_sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def not_implemented(stage: str) -> None:
    raise NotImplementedError(
        f"stage '{stage}': deterministic computation not implemented yet. "
        "This is a design skeleton; see the matching SKILL.md for the intended behavior."
    )


def run_stages(prog: str, stages: dict, build_parser) -> None:
    """Standard entry: dispatch on --stage. `stages` maps name -> callable(args)."""
    parser = build_parser(argparse.ArgumentParser(prog=prog))
    parser.add_argument("--stage", required=True, choices=list(stages),
                        help="which stage of this skill to run")
    args = parser.parse_args()
    print(f"[{prog}] stage={args.stage}")
    stages[args.stage](args)
