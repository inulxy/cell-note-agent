#!/usr/bin/env python
"""Minimal local demo of the crawler -> QC -> peak-matrix handoff shape.

Generates toy artifacts only; no real SnapATAC2/muon/network work.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)


def write_stub(path: str, text: str = "stub") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def stage_qc(args) -> None:
    out_dir = os.path.join(args.results_root, "processed", args.dataset_id)
    os.makedirs(out_dir, exist_ok=True)
    write_stub(os.path.join(out_dir, "atac_qc.h5ad.stub"))
    write_stub(os.path.join(out_dir, "qc_report.html.stub"))
    write_json(os.path.join(out_dir, "qc_summary.json"), {
        "dataset_id": args.dataset_id,
        "genome_build": args.genome_build,
        "n_cells_import": 120,
        "n_cells_pass": 100,
        "fraction_pass": 0.833,
        "tsse_median": 12.4,
        "fragments_median": 25000,
        "blacklist_frac_median": 0.01,
        "leiden_res": args.leiden_res,
        "n_clusters": 5,
        "doublet_rate": 0.08,
        "representation_quality": "fragment_recomputed",
    })
    print(f"[demo] wrote QC artifacts -> {out_dir}")


def stage_peak_matrix(args) -> None:
    out_dir = os.path.join(args.results_root, "processed", args.dataset_id)
    write_stub(os.path.join(out_dir, "peak_matrix.h5ad.stub"))
    write_stub(os.path.join(out_dir, "peaks.hg38.bed"), "chr1\t1000\t1500\nchr1\t3000\t3500\n")
    write_stub(os.path.join(out_dir, "barcodes.tsv.gz.stub"))
    qc_path = os.path.join(out_dir, "qc_summary.json")
    summary = {}
    if os.path.exists(qc_path):
        with open(qc_path, "r", encoding="utf-8") as fh:
            summary = json.load(fh)
    summary["peak_matrix"] = {
        "n_cells": 100,
        "n_peaks": 2,
        "genome_build": "GRCh38",
        "matrix_file": "peak_matrix.h5ad.stub",
        "peaks_file": "peaks.hg38.bed",
    }
    write_json(qc_path, summary)
    print(f"[demo] wrote peak matrix stubs -> {out_dir}")


def stage_handoff(args) -> None:
    out_dir = os.path.join(args.results_root, "processed", args.dataset_id)
    card_path = os.path.join(out_dir, "data_card.json")
    write_json(card_path, {
        "dataset_id": args.dataset_id,
        "genome_build": args.genome_build,
        "modality": "scATAC",
        "deliverable": "per_dataset_peak_matrix",
        "files": {
            "peak_matrix": f"processed/{args.dataset_id}/peak_matrix.h5ad.stub",
            "peaks": f"processed/{args.dataset_id}/peaks.hg38.bed",
            "barcodes": f"processed/{args.dataset_id}/barcodes.tsv.gz.stub",
            "qc_summary": f"processed/{args.dataset_id}/qc_summary.json",
        },
        "provenance": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workflow": "demo_workflow.py",
        },
    })
    manifest_path = os.path.join(args.results_root, "corpus", "MANIFEST.json")
    write_json(manifest_path, {
        "project": args.project,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deliverable": "grch38_per_dataset_peak_matrices",
        "datasets": [{
            "dataset_id": args.dataset_id,
            "data_card": f"processed/{args.dataset_id}/data_card.json",
            "peak_matrix": f"processed/{args.dataset_id}/peak_matrix.h5ad.stub",
            "peaks": f"processed/{args.dataset_id}/peaks.hg38.bed",
            "qc_summary": f"processed/{args.dataset_id}/qc_summary.json",
        }],
    })
    print(f"[demo] wrote handoff artifacts -> {card_path}, {manifest_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal workflow demo (no real computation).")
    parser.add_argument("--results_root", default="demo_results")
    parser.add_argument("--dataset_id", default="toy_pbmc")
    parser.add_argument("--genome_build", default="GRCh38")
    parser.add_argument("--leiden_res", type=float, default=1.0)
    parser.add_argument("--project", default="demo-sc-epi")
    parser.add_argument("--stage", required=True, choices=["qc", "peak-matrix", "handoff", "all"])
    args = parser.parse_args()

    stages = {
        "qc": stage_qc,
        "peak-matrix": stage_peak_matrix,
        "handoff": stage_handoff,
    }
    if args.stage == "all":
        for fn in stages.values():
            fn(args)
    else:
        stages[args.stage](args)


if __name__ == "__main__":
    main()
