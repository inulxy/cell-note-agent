#!/usr/bin/env python
"""Toy PBMC peak-matrix demo.

Creates tiny, dependency-free placeholder artifacts that match the current project contract:
QC summary -> GRCh38 per-dataset peak matrix package -> manifest. No cCRE mapping or
cell tokenization is performed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from datetime import datetime, timezone


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, ensure_ascii=False)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def write_gzip(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(text)


def processed_dir(args) -> str:
    return os.path.join(args.results_root, "processed", args.dataset_id)


def stage_qc(args) -> None:
    out = processed_dir(args)
    write_json(os.path.join(out, "qc_summary.json"), {
        "dataset_id": args.dataset_id,
        "species": "Homo sapiens",
        "genome_build": "GRCh38",
        "input_type": args.input_type,
        "thresholds": {
            "min_fragments": args.min_fragments,
            "min_tsse": args.min_tsse,
            "min_peaks": args.min_peaks,
            "min_counts": args.min_counts,
        },
        "n_cells_before_filter": 4,
        "n_cells_after_filter": 3,
        "n_peaks_after_filter": 3,
        "representation_quality": "toy_peak_matrix",
        "notes": "Dependency-free demo artifact; not real biological processing.",
    })
    write_text(os.path.join(out, "qc_report.html.stub"), "<html><body>toy QC report</body></html>\n")
    print(f"qc: wrote {out}/qc_summary.json")


def stage_peak_matrix(args) -> None:
    out = processed_dir(args)
    write_text(os.path.join(out, "peaks.hg38.bed"), "chr1\t1000\t1500\nchr1\t3000\t3600\nchr2\t500\t900\n")
    write_gzip(os.path.join(out, "barcodes.tsv.gz"), "cellA\ncellB\ncellC\n")
    write_gzip(os.path.join(out, "features.tsv.gz"), "chr1:1000-1500\tchr1:1000-1500\tPeaks\nchr1:3000-3600\tchr1:3000-3600\tPeaks\nchr2:500-900\tchr2:500-900\tPeaks\n")
    write_gzip(os.path.join(out, "matrix.mtx.gz"), "%%MatrixMarket matrix coordinate integer general\n3 3 5\n1 1 2\n2 1 1\n2 2 3\n3 2 1\n3 3 4\n")
    write_text(os.path.join(out, "peak_matrix.h5ad.stub"), "toy AnnData placeholder; MEX files are authoritative for this demo\n")
    print(f"peak-matrix: wrote toy GRCh38 peak matrix under {out}")


def stage_handoff(args) -> None:
    out = processed_dir(args)
    generated_at = datetime.now(timezone.utc).isoformat()
    write_json(os.path.join(out, "data_card.json"), {
        "dataset_id": args.dataset_id,
        "generated_at": generated_at,
        "deliverable": "grch38_per_dataset_peak_matrix",
        "files": {
            "matrix": f"processed/{args.dataset_id}/matrix.mtx.gz",
            "features": f"processed/{args.dataset_id}/features.tsv.gz",
            "barcodes": f"processed/{args.dataset_id}/barcodes.tsv.gz",
            "peaks": f"processed/{args.dataset_id}/peaks.hg38.bed",
            "qc_summary": f"processed/{args.dataset_id}/qc_summary.json",
        },
    })
    manifest_path = os.path.join(args.results_root, "corpus", "MANIFEST.json")
    write_json(manifest_path, {
        "project": args.project,
        "generated_at": generated_at,
        "deliverable": "grch38_per_dataset_peak_matrices",
        "datasets": [{
            "dataset_id": args.dataset_id,
            "data_card": f"processed/{args.dataset_id}/data_card.json",
            "matrix": f"processed/{args.dataset_id}/matrix.mtx.gz",
            "features": f"processed/{args.dataset_id}/features.tsv.gz",
            "barcodes": f"processed/{args.dataset_id}/barcodes.tsv.gz",
            "peaks": f"processed/{args.dataset_id}/peaks.hg38.bed",
            "qc_summary": f"processed/{args.dataset_id}/qc_summary.json",
        }],
    })
    print(f"handoff: wrote {manifest_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Toy PBMC peak-matrix demo.")
    parser.add_argument("--results_root", default="demo_pbmc")
    parser.add_argument("--dataset_id", default="toy_pbmc")
    parser.add_argument("--project", default="demo-sc-epi")
    parser.add_argument("--input_type", choices=["fragments", "peak_matrix", "multiome"], default="fragments")
    parser.add_argument("--min_fragments", type=int, default=1000)
    parser.add_argument("--min_tsse", type=float, default=4.0)
    parser.add_argument("--min_peaks", type=int, default=500)
    parser.add_argument("--min_counts", type=int, default=1000)
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
