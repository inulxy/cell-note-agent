#!/usr/bin/env python
"""package-existing-peak-matrix: lightweight packaging for existing h5ad peak matrices.

This is intended for very large already-materialized cell x peak matrices. It avoids
full in-memory reads, exports peak coordinates/barcodes, records metadata QC, and
creates a GRCh38 per-dataset peak matrix package without cCRE mapping or tokenization.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from _common import require_files, run_stages, stage_subdir


def _optional_import(name: str):
    try:
        return __import__(name)
    except ImportError as error:
        raise SystemExit(f"[error] optional dependency '{name}' is required: {error}") from error


def _out_dir(args) -> Path:
    return Path(stage_subdir(args.results_root, "processed", args.dataset_id))


def _summary_path(args) -> Path:
    return _out_dir(args) / "qc_summary.json"


def _matrix_out(args) -> Path:
    return _out_dir(args) / "peak_matrix.h5ad"


def _peaks_out(args) -> Path:
    return _out_dir(args) / "peaks.hg38.bed"


def _barcodes_out(args) -> Path:
    return _out_dir(args) / "barcodes.tsv.gz"


def _data_card_path(args) -> Path:
    return _out_dir(args) / "data_card.json"


def _load_summary(args) -> dict:
    path = _summary_path(args)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_summary(args, summary: dict) -> None:
    path = _summary_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_backed_h5ad(path: str):
    ad = _optional_import("anndata")
    return ad.read_h5ad(path, backed="r")


def _safe_link_or_copy(source: Path, target: Path, mode: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.resolve() == source.resolve():
            return "symlink"
        try:
            if target.stat().st_ino == source.stat().st_ino:
                return "hardlink"
        except OSError:
            pass
        target.unlink()
    if mode == "symlink":
        target.symlink_to(source)
        return "symlink"
    if mode == "copy":
        shutil.copy2(source, target)
        return "copy"
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        target.symlink_to(source)
        return "symlink"


def _peak_rows_from_var_names(var_names) -> list[str]:
    rows: list[str] = []
    pattern = re.compile(r"^(chr[^:]+):(\d+)-(\d+)$")
    for name in map(str, var_names):
        match = pattern.match(name.replace(",", ""))
        if not match:
            return []
        chrom, start, end = match.groups()
        rows.append(f"{chrom}\t{int(start)}\t{int(end)}")
    return rows


def _copy_clean_bed(input_path: str, output_path: Path) -> int:
    count = 0
    with open(input_path, "r", encoding="utf-8") as source, output_path.open("w", encoding="utf-8") as target:
        for line in source:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            try:
                start = int(parts[1])
                end = int(parts[2])
            except ValueError:
                continue
            target.write(f"{parts[0]}\t{start}\t{end}\n")
            count += 1
    return count


def _write_peaks(args, adata) -> int:
    out = _peaks_out(args)
    rows = _peak_rows_from_var_names(adata.var_names)
    if rows:
        out.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return len(rows)
    if args.peaks:
        require_files(args.peaks)
        count = _copy_clean_bed(args.peaks, out)
        if count:
            return count
    raise SystemExit("[error] peak coordinates missing or unparsable; provide --peaks")


def _write_barcodes(args, obs_names) -> int:
    out = _barcodes_out(args)
    count = 0
    with gzip.open(out, "wt", encoding="utf-8") as handle:
        for name in map(str, obs_names):
            handle.write(name + "\n")
            count += 1
    return count


def _obs_summaries(adata) -> tuple[dict, dict]:
    np = _optional_import("numpy")
    pd = _optional_import("pandas")
    numeric: dict = {}
    categorical: dict = {}
    for column in adata.obs.columns:
        series = adata.obs[column]
        if pd.api.types.is_numeric_dtype(series):
            values = np.asarray(series.dropna())
            if values.size:
                numeric[column] = {
                    "median": float(np.median(values)),
                    "mean": float(np.mean(values)),
                    "min": float(np.min(values)),
                    "max": float(np.max(values)),
                }
        elif isinstance(series.dtype, pd.CategoricalDtype) or pd.api.types.is_object_dtype(series):
            counts = series.astype(str).value_counts(dropna=False).head(20)
            categorical[column] = {str(key): int(value) for key, value in counts.items()}
    return numeric, categorical


def inspect(args):
    require_files(args.matrix)
    if not str(args.matrix).endswith(".h5ad"):
        raise SystemExit("[inspect] package-existing-peak-matrix currently expects an .h5ad input")
    adata = _load_backed_h5ad(args.matrix)
    try:
        numeric, categorical = _obs_summaries(adata)
        summary = _load_summary(args)
        summary.update({
            "dataset_id": args.dataset_id,
            "genome_build": args.genome_build,
            "target_genome_build": "GRCh38",
            "source_peak_matrix": str(Path(args.matrix).resolve()),
            "n_cells_loaded": int(adata.n_obs),
            "n_peaks_loaded": int(adata.n_vars),
            "obs_columns": list(map(str, adata.obs.columns)),
            "var_columns": list(map(str, adata.var.columns)),
            "obs_numeric_summary": numeric,
            "obs_categorical_top20": categorical,
            "representation_quality": "existing_cell_by_peak_h5ad_packaged_without_ccre_or_tokenization",
            "consensus_peak_matrix": False,
            "qc_note": "Backed metadata inspection only; full matrix was not loaded into memory.",
            "stages_completed": sorted(set(summary.get("stages_completed", []) + ["inspect"])),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        _save_summary(args, summary)
        print(json.dumps({
            "dataset_id": args.dataset_id,
            "input_kind": "peak_matrix",
            "matrix": args.matrix,
            "shape": [int(adata.n_obs), int(adata.n_vars)],
            "genome_build": args.genome_build,
            "summary": str(_summary_path(args)),
        }, indent=2, ensure_ascii=False))
    finally:
        adata.file.close()


def materialize(args):
    require_files(args.matrix)
    adata = _load_backed_h5ad(args.matrix)
    try:
        matrix_link_type = _safe_link_or_copy(Path(args.matrix).resolve(), _matrix_out(args), args.link_mode)
        n_peaks = _write_peaks(args, adata)
        n_barcodes = _write_barcodes(args, adata.obs_names)
        summary = _load_summary(args)
        summary.update({
            "dataset_id": args.dataset_id,
            "genome_build": args.genome_build,
            "target_genome_build": "GRCh38",
            "source_peak_matrix": str(Path(args.matrix).resolve()),
            "peak_matrix": str(_matrix_out(args)),
            "matrix_link_type": matrix_link_type,
            "peaks_file": str(_peaks_out(args)),
            "barcodes_file": str(_barcodes_out(args)),
            "n_cells_loaded": int(adata.n_obs),
            "n_peaks_loaded": int(adata.n_vars),
            "n_peaks_with_parseable_coordinates": int(n_peaks),
            "n_barcodes_written": int(n_barcodes),
            "representation_quality": "existing_cell_by_peak_h5ad_packaged_without_ccre_or_tokenization",
            "consensus_peak_matrix": False,
            "stages_completed": sorted(set(summary.get("stages_completed", []) + ["materialize"])),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        _save_summary(args, summary)
        print(f"[materialize] matrix={_matrix_out(args)} ({matrix_link_type}); peaks={n_peaks}; barcodes={n_barcodes}")
    finally:
        adata.file.close()


def finalize(args):
    summary = _load_summary(args)
    required = {
        "peak_matrix": _matrix_out(args),
        "peaks": _peaks_out(args),
        "barcodes": _barcodes_out(args),
        "qc_summary": _summary_path(args),
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        raise SystemExit(f"[finalize] missing outputs: {', '.join(missing)}")
    if args.genome_build not in {"GRCh38", "hg38"}:
        raise SystemExit("[finalize] final peak matrix must be GRCh38/hg38")
    summary.update({
        "peak_matrix": str(_matrix_out(args)),
        "peaks_file": str(_peaks_out(args)),
        "barcodes_file": str(_barcodes_out(args)),
        "stages_completed": sorted(set(summary.get("stages_completed", []) + ["finalize"])),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_summary(args, summary)
    card = {
        "dataset_id": args.dataset_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deliverable": "grch38_per_dataset_peak_matrix",
        "genome_build": args.genome_build,
        "representation_quality": summary.get("representation_quality"),
        "files": {key: str(path) for key, path in required.items()},
        "qc_summary": summary,
    }
    _data_card_path(args).write_text(json.dumps(card, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[finalize] peak matrix package ready: {_out_dir(args)}")


def _parser(parser):
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--peaks")
    parser.add_argument("--genome_build", default="GRCh38")
    parser.add_argument("--results_root", required=True)
    parser.add_argument("--dataset_id", required=True)
    parser.add_argument("--link_mode", choices=("auto", "symlink", "copy"), default="auto")
    return parser


if __name__ == "__main__":
    run_stages("package_existing_peak_matrix", {"inspect": inspect, "materialize": materialize, "finalize": finalize}, _parser)
