#!/usr/bin/env python
"""Pi skill bridge and deterministic peak-matrix routing helpers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


@dataclass(frozen=True)
class SkillInvocation:
    skill: str
    stage: str
    command: str
    human_confirmation: bool
    reason: str


def environment_python(environment: str) -> str:
    env_key = f"CELLNOTE_{environment.upper().replace('-', '_')}_PYTHON"
    candidates = [
        os.environ.get(env_key, ""),
        f"/home/lixinyu/miniforge3/envs/{environment}/bin/python",
        f"/ssd/deecamp/cellnotes/conda-envs/{environment}/bin/python",
        f"/opt/anaconda3/envs/{environment}/bin/python",
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).exists()), "python")


def discover_skills(skills_root: str = "skills") -> list[dict[str, str]]:
    """Return skill names and descriptions from local ``SKILL.md`` files."""
    root = Path(skills_root)
    skills: list[dict[str, str]] = []
    for path in sorted(root.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(text)
        metadata: dict[str, str] = {"name": path.parent.name, "description": ""}
        if match:
            for line in match.group(1).splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip('"')
        skills.append(metadata)
    return skills


def peak_matrix_plan(
    *,
    input_kind: str,
    dataset_id: str,
    results_root: str,
    input_path: str | None = None,
    peaks_path: str | None = None,
    rna_path: str | None = None,
    genome_build: str = "GRCh38",
) -> list[SkillInvocation]:
    """Build a Pi plan whose commands match each modality script's real CLI.

    ``normalize-to-peak-matrix`` is retained as a route declaration only.  Its
    historical ``materialize`` output is an untyped ``.npz`` that the QC
    scripts cannot read, so processing must consume the original supported
    input and write the documented ``processed/<dataset_id>`` package.
    """

    def render(parts: list[str]) -> str:
        return shlex.join(parts)

    normalize = [
        "python",
        "scripts/normalize_to_peak_matrix.py",
        "--input_kind",
        input_kind,
        "--dataset_id",
        dataset_id,
        "--results_root",
        results_root,
        "--genome_build",
        genome_build,
    ]
    if input_path:
        normalize.extend(["--input", input_path])
    if peaks_path:
        normalize.extend(["--peaks", peaks_path])
    if rna_path:
        normalize.extend(["--rna", rna_path])

    plan = [
        SkillInvocation(
            skill="normalize-to-peak-matrix",
            stage="plan",
            command=render([*normalize, "--stage", "plan"]),
            human_confirmation=False,
            reason="Select the modality-specific route without rewriting the source matrix format.",
        )
    ]
    if input_kind == "rna_reference":
        return plan

    reference_dir = str(Path(results_root) / "reference")
    curator_python = environment_python("cellnote-curator")
    snapatac_python = environment_python("snapatac2")
    muon_python = environment_python("muon")

    def append_reference_setup(*, include_liftover: bool = False) -> None:
        command_base = [
            curator_python,
            "scripts/prepare_references.py", "--out", reference_dir,
        ]
        if include_liftover:
            command_base.append("--include_liftover")
        for stage in ("plan", "fetch", "verify"):
            plan.append(
                SkillInvocation(
                    skill="resource-setup",
                    stage=stage,
                    command=render([*command_base, "--stage", stage]),
                    human_confirmation=stage == "fetch",
                    reason="Prepare pinned and checksum-verified reference assets required by the selected QC route.",
                )
            )

    if input_kind == "fragments":
        if not input_path:
            raise ValueError("fragments planning requires input_path")
        if genome_build.lower() not in {"grch38", "hg38"}:
            raise ValueError("fragments planning currently requires GRCh38/hg38 input")
        skill = "scatac-fragment-qc"
        append_reference_setup()
        stages = [
            "import",
            "pre-filter",
            "filter",
            "embed",
            "cluster",
            "doublet",
            "call-peaks",
            "make-peak-matrix",
            "finalize",
        ]
        command_base = [
            snapatac_python,
            "scripts/scatac_fragment_qc.py",
            "--fragments",
            input_path,
            "--results_root",
            results_root,
            "--dataset_id",
            dataset_id,
            "--genome_build",
            genome_build,
            "--blacklist_bed",
            str(Path(reference_dir) / "hg38-blacklist.v2.bed"),
        ]
        if peaks_path:
            command_base.extend(["--peaks", peaks_path])
    elif input_kind == "peak_matrix":
        if not input_path:
            raise ValueError("peak_matrix planning requires input_path")
        skill = "scatac-peak-matrix"
        needs_liftover = genome_build.lower() in {"hg19", "grch37"}
        if needs_liftover:
            append_reference_setup(include_liftover=True)
        stages = ["load", "profile", "filter", "standardize", "embed-cluster", "finalize"]
        command_base = [
            curator_python,
            "scripts/scatac_peak_matrix.py",
            "--matrix",
            input_path,
            "--results_root",
            results_root,
            "--dataset_id",
            dataset_id,
            "--genome_build",
            genome_build,
        ]
        if peaks_path:
            command_base.extend(["--peaks", peaks_path])
        if needs_liftover:
            command_base.extend(["--liftover_chain", str(Path(reference_dir) / "hg19ToHg38.over.chain.gz")])
    elif input_kind == "multiome":
        if not input_path or not rna_path:
            raise ValueError("multiome planning requires input_path and rna_path")
        fragments_input = "fragment" in Path(input_path).name.lower()
        if fragments_input and genome_build.lower() not in {"grch38", "hg38"}:
            raise ValueError("multiome fragments planning currently requires GRCh38/hg38 input")
        skill = "multiome-qc"
        stages = ["pair-check", "qc-rna", "qc-atac", "intersect", "finalize"]
        command_base = [
            snapatac_python if fragments_input else muon_python,
            "scripts/multiome_qc.py",
            "--rna",
            rna_path,
            "--atac_fragments" if fragments_input else "--atac_matrix",
            input_path,
            "--results_root",
            results_root,
            "--dataset_id",
            dataset_id,
            "--genome_build",
            genome_build,
        ]
        if peaks_path:
            command_base.extend(["--peaks", peaks_path])
    else:
        raise ValueError(f"unsupported input_kind: {input_kind}")

    for index, stage in enumerate(stages):
        plan.append(
            SkillInvocation(
                skill=skill,
                stage=stage,
                command=render([*command_base, "--stage", stage]),
                human_confirmation=index == 0,
                reason=f"Run the deterministic {skill} {stage} stage on the original supported input.",
            )
        )

    for stage in ("cards", "validate", "package"):
        plan.append(
            SkillInvocation(
                skill="handoff-pipeline",
                stage=stage,
                command=render(
                    [
                        "python",
                        "scripts/package_peak_matrices.py",
                        "--results_root",
                        results_root,
                        "--stage",
                        stage,
                    ]
                ),
                human_confirmation=stage == "package",
                reason="Validate and package the processed GRCh38 per-dataset peak matrix.",
            )
        )
    return plan


def _skills(args: argparse.Namespace) -> None:
    print(json.dumps(discover_skills(args.skills_root), indent=2, ensure_ascii=False))


def _plan(args: argparse.Namespace) -> None:
    invocations = peak_matrix_plan(
        input_kind=args.input_kind,
        dataset_id=args.dataset_id,
        results_root=args.results_root,
        input_path=args.input,
        peaks_path=args.peaks,
        rna_path=args.rna,
        genome_build=args.genome_build,
    )
    payload = [asdict(item) for item in invocations]
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for item in invocations:
            confirm = "confirm" if item.human_confirmation else "auto"
            print(f"/skill:{item.skill}  # stage={item.stage}, {confirm}")
            print(item.command)
            print(f"# {item.reason}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CellNoteAgent Pi bridge.")
    sub = parser.add_subparsers(dest="command", required=True)

    skills = sub.add_parser("skills", help="List local Pi skills.")
    skills.add_argument("--skills_root", default="skills")
    skills.set_defaults(func=_skills)

    plan = sub.add_parser("plan-peak-matrix", help="Build the canonical peak-matrix skill plan.")
    plan.add_argument("--input_kind", required=True, choices=["fragments", "peak_matrix", "multiome", "rna_reference"])
    plan.add_argument("--input")
    plan.add_argument("--peaks")
    plan.add_argument("--rna")
    plan.add_argument("--dataset_id", required=True)
    plan.add_argument("--results_root", required=True)
    plan.add_argument("--genome_build", default="GRCh38")
    plan.add_argument("--format", choices=["text", "json"], default="text")
    plan.set_defaults(func=_plan)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
