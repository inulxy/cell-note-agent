#!/usr/bin/env python
"""Normalize heterogeneous inputs to the canonical cell x peak matrix.

This is the bridge between agent routing and the modeling contract:

    every ATAC-bearing input -> results_root/peak_matrices/<dataset_id>/
        cell_x_peak.npz
        peaks.bed
        peak_matrix_metadata.json

Heavy bioinformatics is delegated to modality-specific tools. This script records
the deterministic plan, registers already-provided peak matrices, and validates
the canonical handoff location.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone

from _common import require_files, run_stages


def _out_dir(args) -> str:
    path = os.path.join(args.results_root, "peak_matrices", args.dataset_id)
    os.makedirs(path, exist_ok=True)
    return path


def _metadata(args, route: str) -> dict:
    return {
        "dataset_id": args.dataset_id,
        "input_kind": args.input_kind,
        "input": args.input,
        "peaks": args.peaks,
        "rna": args.rna,
        "genome_build": args.genome_build,
        "route": route,
        "canonical_outputs": {
            "matrix": f"peak_matrices/{args.dataset_id}/cell_x_peak.npz",
            "peaks": f"peak_matrices/{args.dataset_id}/peaks.bed",
            "metadata": f"peak_matrices/{args.dataset_id}/peak_matrix_metadata.json",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _route(args) -> str:
    routes = {
        "fragments": "fragments -> SnapATAC2 QC -> MACS3/consensus peaks -> cell_x_peak.npz",
        "peak_matrix": "register provided cell x peak matrix + peaks.bed",
        "multiome": "multiome pair-check -> ATAC fragments/peaks -> cell_x_peak.npz; RNA retained as metadata/reference",
        "rna_reference": "RNA reference only; no ATAC peak matrix generated for FM pretraining",
    }
    return routes[args.input_kind]


def plan(args) -> None:
    out_dir = _out_dir(args)
    payload = _metadata(args, _route(args))
    _write_json(os.path.join(out_dir, "peak_matrix_plan.json"), payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def materialize(args) -> None:
    out_dir = _out_dir(args)
    if args.input_kind == "peak_matrix":
        require_files(args.input, args.peaks)
        matrix_out = os.path.join(out_dir, "cell_x_peak.npz")
        peaks_out = os.path.join(out_dir, "peaks.bed")
        if os.path.abspath(args.input) != os.path.abspath(matrix_out):
            shutil.copyfile(args.input, matrix_out)
        if os.path.abspath(args.peaks) != os.path.abspath(peaks_out):
            shutil.copyfile(args.peaks, peaks_out)
        payload = _metadata(args, _route(args))
        payload["materialized"] = True
        payload["representation_quality"] = "provided_peak_matrix"
        _write_json(os.path.join(out_dir, "peak_matrix_metadata.json"), payload)
        print(f"[ok] registered peak matrix under {out_dir}")
        return

    payload = _metadata(args, _route(args))
    payload["materialized"] = False
    payload["reason"] = (
        "This input kind requires modality-specific bioinformatics first. "
        "Run the generated Pi plan; its downstream output must be copied or written "
        "to this canonical peak_matrices directory."
    )
    _write_json(os.path.join(out_dir, "peak_matrix_metadata.json"), payload)
    raise SystemExit(
        "[needs-tool] canonical peak matrix not materialized yet; run the modality-specific skill first."
    )


def validate(args) -> None:
    out_dir = _out_dir(args)
    matrix_path = os.path.join(out_dir, "cell_x_peak.npz")
    peaks_path = os.path.join(out_dir, "peaks.bed")
    metadata_path = os.path.join(out_dir, "peak_matrix_metadata.json")
    require_files(matrix_path, peaks_path, metadata_path)
    print(f"[ok] canonical peak matrix is ready: {matrix_path}")


def _parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--input_kind", required=True, choices=["fragments", "peak_matrix", "multiome", "rna_reference"])
    parser.add_argument("--input", help="fragments.tsv.gz, matrix.npz, matrix.h5ad, or modality-specific ATAC input")
    parser.add_argument("--peaks", help="peaks.bed when --input_kind=peak_matrix")
    parser.add_argument("--rna", help="RNA matrix for multiome or RNA reference side input")
    parser.add_argument("--results_root", required=True)
    parser.add_argument("--dataset_id", required=True)
    parser.add_argument("--genome_build", default="GRCh38")
    return parser


if __name__ == "__main__":
    run_stages("normalize_to_peak_matrix", {"plan": plan, "materialize": materialize, "validate": validate}, _parser)
