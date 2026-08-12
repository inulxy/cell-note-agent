#!/usr/bin/env python
"""Registry and adapter planner for trusted external skills.

External skills are treated as *consulted SOPs* unless they are explicitly vendored
and pinned. CellNoteAgent owns the canonical contracts:

    ATAC-bearing input -> peak_matrices/<dataset_id>/cell_x_peak.npz + peaks.bed
    peak matrix -> data cards -> manifest handoff
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import Any


DEFAULT_REGISTRY = os.path.join("configs", "external_skills.json")


@dataclass(frozen=True)
class ExternalSkillStep:
    order: int
    action: str
    skill_id: str
    reason: str
    source_url: str
    human_confirmation: bool = False


def load_registry(path: str = DEFAULT_REGISTRY) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def registry_skills(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return list(registry.get("skills", []))


def validate_registry(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    required = ["id", "kind", "display_name", "source", "trust", "modalities", "roles", "cellnote_contract"]
    for index, item in enumerate(registry_skills(registry)):
        prefix = f"skills[{index}]"
        for key in required:
            if key not in item:
                errors.append(f"{prefix}: missing {key}")
        skill_id = item.get("id")
        if skill_id in seen:
            errors.append(f"{prefix}: duplicate id {skill_id}")
        seen.add(skill_id)
        source = item.get("source", {})
        for key in ["repo_or_url", "path", "ref", "url"]:
            if key not in source:
                errors.append(f"{prefix}: missing source.{key}")
        contract = item.get("cellnote_contract", {})
        if "next_cellnote_skill" not in contract:
            errors.append(f"{prefix}: missing cellnote_contract.next_cellnote_skill")
    return errors


def filter_skills(
    registry: dict[str, Any],
    *,
    modality: str | None = None,
    role: str | None = None,
    kind: str | None = None,
    core_only: bool = False,
) -> list[dict[str, Any]]:
    items = registry_skills(registry)
    if modality:
        items = [item for item in items if modality in item.get("modalities", []) or modality in item.get("core_for", [])]
    if role:
        items = [item for item in items if role in item.get("roles", [])]
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    if core_only:
        items = [item for item in items if item.get("core_for")]
    return items


def _by_id(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in registry_skills(registry)}


def plan_for_modality(modality: str, registry: dict[str, Any]) -> list[ExternalSkillStep]:
    """Return an ordered, conservative external-skill consultation plan."""
    skill_ids_by_modality = {
        "scatac_fragments": [
            "official-encode-atac-standards",
            "official-snapatac2",
            "gptomics-atac-qc",
            "gptomics-single-cell-atac",
            "gptomics-atac-peak-calling",
            "gptomics-consensus-peakset",
            "kdense-anndata"
        ],
        "scatac_peak_matrix": [
            "official-encode-atac-standards",
            "gptomics-atac-qc",
            "gptomics-single-cell-atac",
            "kdense-anndata"
        ],
        "multiome": [
            "official-muon",
            "official-snapatac2",
            "official-scanpy",
            "gptomics-single-cell-atac",
            "gptomics-atac-qc",
            "gptomics-atac-peak-calling",
            "kdense-scanpy",
            "kdense-anndata"
        ],
        "rna_support": [
            "official-scanpy",
            "kdense-scanpy",
            "kdense-anndata"
        ],
        "downstream_eval": [
            "gptomics-motif-deviation",
            "gptomics-differential-accessibility",
            "gptomics-co-accessibility",
            "gptomics-enhancer-gene-linking",
            "gptomics-footprinting",
            "gptomics-deep-learning-atac",
            "gptomics-allele-specific-accessibility"
        ]
    }
    if modality not in skill_ids_by_modality:
        raise ValueError(f"unknown modality plan: {modality}")

    lookup = _by_id(registry)
    steps: list[ExternalSkillStep] = []
    for order, skill_id in enumerate(skill_ids_by_modality[modality], start=1):
        item = lookup[skill_id]
        trust_level = item["trust"]["level"]
        action = "consult official reference" if item["kind"] == "reference_source" else "consult pinned external skill"
        steps.append(
            ExternalSkillStep(
                order=order,
                action=action,
                skill_id=skill_id,
                reason=f"{item['display_name']} ({trust_level}) -> {', '.join(item.get('roles', []))}",
                source_url=item["source"]["url"],
                human_confirmation=item["kind"] == "agent_skill",
            )
        )

    return steps


def render_text_plan(
    steps: list[ExternalSkillStep],
    *,
    modality: str,
    dataset_id: str | None,
    results_root: str | None,
) -> str:
    lines = [
        f"# External skill plan: {modality}",
        "# External skills are consulted/wrapped; CellNoteAgent keeps canonical outputs.",
    ]
    if dataset_id and results_root:
        lines.append(f"# Dataset: {dataset_id}; results_root: {results_root}")
    lines.append("")
    for step in steps:
        confirm = "review first" if step.human_confirmation else "reference"
        lines.append(f"{step.order}. {step.action}: {step.skill_id} [{confirm}]")
        lines.append(f"   - why: {step.reason}")
        lines.append(f"   - source: {step.source_url}")
    lines.extend([
        "",
        "# CellNoteAgent invariant after external consultation:",
        "#   ATAC-bearing inputs -> normalize-to-peak-matrix -> peak_matrices/<dataset_id>/cell_x_peak.npz + peaks.bed",
        "#   peak matrix -> package_peak_matrices -> corpus/MANIFEST.json",
    ])
    return "\n".join(lines)


def _cmd_validate(args: argparse.Namespace) -> None:
    registry = load_registry(args.registry)
    errors = validate_registry(registry)
    if errors:
        for error in errors:
            print(f"[error] {error}", file=sys.stderr)
        sys.exit(1)
    print(f"[ok] registry valid: {args.registry} ({len(registry_skills(registry))} entries)")


def _cmd_list(args: argparse.Namespace) -> None:
    registry = load_registry(args.registry)
    items = filter_skills(
        registry,
        modality=args.modality,
        role=args.role,
        kind=args.kind,
        core_only=args.core_only,
    )
    if args.format == "json":
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return
    for item in items:
        print(f"{item['id']}\t{item['kind']}\t{item['trust']['level']}\t{', '.join(item.get('roles', []))}")


def _cmd_plan(args: argparse.Namespace) -> None:
    registry = load_registry(args.registry)
    steps = plan_for_modality(args.modality, registry)
    if args.format == "json":
        print(json.dumps([asdict(step) for step in steps], indent=2, ensure_ascii=False))
        return
    print(render_text_plan(steps, modality=args.modality, dataset_id=args.dataset_id, results_root=args.results_root))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CellNoteAgent external skills registry.")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate registry structure.")
    validate.set_defaults(func=_cmd_validate)

    list_cmd = sub.add_parser("list", help="List trusted external skills/references.")
    list_cmd.add_argument("--modality")
    list_cmd.add_argument("--role")
    list_cmd.add_argument("--kind", choices=["agent_skill", "reference_source"])
    list_cmd.add_argument("--core-only", action="store_true")
    list_cmd.add_argument("--format", choices=["text", "json"], default="text")
    list_cmd.set_defaults(func=_cmd_list)

    plan = sub.add_parser("plan", help="Plan which external skills to consult for a modality.")
    plan.add_argument("--modality", required=True, choices=["scatac_fragments", "scatac_peak_matrix", "multiome", "rna_support", "downstream_eval"])
    plan.add_argument("--dataset_id")
    plan.add_argument("--results_root")
    plan.add_argument("--format", choices=["text", "json"], default="text")
    plan.set_defaults(func=_cmd_plan)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
