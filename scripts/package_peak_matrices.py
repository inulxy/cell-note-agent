#!/usr/bin/env python
"""package-peak-matrices: package per-dataset GRCh38 peak matrices for FM handoff."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from _common import run_stages


def _processed_root(args) -> str:
    return os.path.join(args.results_root, "processed")


def _dataset_dirs(args) -> list[str]:
    root = _processed_root(args)
    if not os.path.isdir(root):
        return []
    quarantine_markers = (".failed-", ".partial-", ".backup-", ".quarantine-")
    return [
        os.path.join(root, name)
        for name in sorted(os.listdir(root))
        if os.path.isdir(os.path.join(root, name))
        and not name.startswith(".")
        and not any(marker in name for marker in quarantine_markers)
    ]


def _load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def _rel(args, path: str | None) -> str | None:
    if not path:
        return None
    try:
        return os.path.relpath(path, args.results_root)
    except ValueError:
        return path


def _find_file(dataset_dir: str, names: tuple[str, ...]) -> str | None:
    for name in names:
        path = os.path.join(dataset_dir, name)
        if os.path.exists(path):
            return path
    return None


def cards(args):
    count = 0
    for dataset_dir in _dataset_dirs(args):
        dataset_id = os.path.basename(dataset_dir)
        summary_path = os.path.join(dataset_dir, "qc_summary.json")
        summary = _load_json(summary_path)
        peak_matrix = summary.get("peak_matrix") or _find_file(dataset_dir, ("peak_matrix.h5ad", "matrix.mtx.gz"))
        peaks = summary.get("peaks_file") or _find_file(dataset_dir, ("peaks.hg38.bed", "peaks.bed"))
        barcodes = summary.get("barcodes_file") or _find_file(dataset_dir, ("barcodes.tsv.gz", "barcodes.tsv"))
        card = {
            "dataset_id": summary.get("dataset_id", dataset_id),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "deliverable": "grch38_per_dataset_peak_matrix",
            # No silent default: a missing build must fail validate, not
            # masquerade as GRCh38.
            "genome_build": summary.get("genome_build"),
            "representation_quality": summary.get("representation_quality"),
            "files": {
                "peak_matrix": _rel(args, peak_matrix),
                "peaks": _rel(args, peaks),
                "barcodes": _rel(args, barcodes),
                "qc_summary": _rel(args, summary_path),
            },
        }
        with open(os.path.join(dataset_dir, "data_card.json"), "w", encoding="utf-8") as handle:
            json.dump(card, handle, indent=2, ensure_ascii=False)
        print(f"[cards] {dataset_id}")
        count += 1
    print(f"[cards] wrote {count} data cards")


def validate(args):
    errors: list[str] = []
    for dataset_dir in _dataset_dirs(args):
        dataset_id = os.path.basename(dataset_dir)
        card = _load_json(os.path.join(dataset_dir, "data_card.json"))
        files = card.get("files", {})
        for key in ("peak_matrix", "peaks", "barcodes", "qc_summary"):
            rel_path = files.get(key)
            if not rel_path:
                errors.append(f"{dataset_id}: missing {key} reference")
                continue
            path = rel_path if os.path.isabs(rel_path) else os.path.join(args.results_root, rel_path)
            if not os.path.exists(path):
                errors.append(f"{dataset_id}: {key} not found: {path}")
        build = card.get("genome_build")
        if build is None and isinstance(card.get("qc_summary"), dict):
            build = card["qc_summary"].get("genome_build")
        if build not in {"GRCh38", "hg38"}:
            errors.append(
                f"{dataset_id}: genome_build missing or not GRCh38/hg38 (got {build!r})"
            )
    if errors:
        print("[validate] failed:")
        for error in errors:
            print(f"  - {error}")
        raise SystemExit(2)
    print("[validate] all dataset cards look complete")


def package(args):
    datasets = []
    for dataset_dir in _dataset_dirs(args):
        card_path = os.path.join(dataset_dir, "data_card.json")
        card = _load_json(card_path)
        if not card:
            continue
        files = card.get("files", {})
        datasets.append({
            "dataset_id": card.get("dataset_id", os.path.basename(dataset_dir)),
            "data_card": _rel(args, card_path),
            "peak_matrix": files.get("peak_matrix"),
            "peaks": files.get("peaks"),
            "barcodes": files.get("barcodes"),
            "qc_summary": files.get("qc_summary"),
            "representation_quality": card.get("representation_quality"),
        })
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deliverable": "grch38_per_dataset_peak_matrices",
        "consensus_peak_matrix": False,
        "dataset_count": len(datasets),
        "datasets": datasets,
    }
    out_path = args.out if os.path.isabs(args.out) else os.path.join(args.results_root, args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    print(f"[package] wrote {out_path} ({len(datasets)} datasets)")


def _parser(parser):
    parser.add_argument("--results_root", required=True)
    parser.add_argument("--out", default="corpus/MANIFEST.json")
    return parser


if __name__ == "__main__":
    run_stages("package_peak_matrices", {"cards": cards, "validate": validate, "package": package}, _parser)
