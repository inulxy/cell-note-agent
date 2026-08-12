#!/usr/bin/env python
"""detect-input: classify existing CellNote analysis inputs.

The detector is intentionally factual and conservative. It inspects files,
directories, or manifests and emits JSON for a planner/executor to choose a
whitelisted CellNote skill. It does not run analysis and does not invent missing
metadata.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
from pathlib import Path
from typing import Any

PEAK_NAME_RE = re.compile(r"^chr[^:]+:\d+-\d+$")
FRAGMENT_RE = re.compile(r"fragments(?:\.tsv)?(?:\.gz)?$", re.IGNORECASE)
MATRIX_EXTENSIONS = (".h5ad", ".h5", ".mtx", ".mtx.gz", ".npz")
TEXT_MATRIX_NAMES = {"matrix.mtx", "matrix.mtx.gz"}
BARCODE_NAMES = {"barcodes.tsv", "barcodes.tsv.gz"}
FEATURE_NAMES = {"features.tsv", "features.tsv.gz", "peaks.bed", "peaks.bed.gz"}


def jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def base_result(path: Path) -> dict[str, Any]:
    return {
        "schema_version": "detect_input.v1",
        "query_path": str(path),
        "exists": path.exists(),
        "input_kind": "unknown",
        "confidence": 0.0,
        "files": {"matrix": "", "fragments": "", "fragment_files": [], "metadata_files": [], "peaks": "", "barcodes": "", "rna": "", "manifest": ""},
        "input_mode": "unknown",
        "file_count": 0,
        "genome_build": "unknown",
        "safe_mode": "review_required",
        "recommended_qc_mode": "review_required",
        "size_risk": "unknown",
        "requires_user_confirmation": True,
        "reason": "not inspected",
        "warnings": [],
        "metadata": {},
    }


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def is_peak_name(name: str) -> bool:
    return bool(PEAK_NAME_RE.match(str(name).replace(",", "")))


def is_fragment_file(path: Path) -> bool:
    """Recognize a fragment file by its five-column schema, not its filename."""
    lower = path.name.lower()
    if not lower.endswith((".tsv", ".tsv.gz", ".bed", ".bed.gz")):
        return False
    if lower.endswith((".bed", ".bed.gz")) and "fragment" not in lower:
        return False
    opener = gzip.open if lower.endswith(".gz") else open
    try:
        checked = 0
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip() or line.startswith("#"):
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 5:
                    return False
                start, end, count = int(fields[1]), int(fields[2]), int(fields[4])
                if not fields[0] or not fields[3] or start < 0 or end <= start or count < 0:
                    return False
                checked += 1
                if checked >= 3:
                    break
        return checked > 0
    except (OSError, UnicodeError, ValueError):
        return False


def paired_metadata_file(fragment: Path) -> Path | None:
    name = fragment.name
    for suffix in (".tsv.gz", ".bed.gz", ".tsv", ".bed"):
        if name.lower().endswith(suffix):
            candidate = fragment.with_name(name[: -len(suffix)] + "-metadata.csv")
            return candidate if candidate.exists() else None
    return None


def fragment_result(query_path: Path, fragment_files: list[Path]) -> dict[str, Any]:
    result = base_result(query_path)
    ordered = sorted(dict.fromkeys(path.resolve() for path in fragment_files))
    metadata_files = [item for item in (paired_metadata_file(path) for path in ordered) if item]
    total_size = sum(file_size(path) for path in ordered) + sum(file_size(path) for path in metadata_files)
    input_mode = "collection" if len(ordered) > 1 else "single"
    result.update({
        "input_kind": "fragments",
        "input_mode": input_mode,
        "confidence": 0.98 if ordered else 0.0,
        "safe_mode": "fragment_qc",
        "recommended_qc_mode": "large_full_qc" if total_size > 5_000_000_000 else "standard_full_qc",
        "size_risk": "large" if total_size > 5_000_000_000 else "standard",
        "reason": f"detected {len(ordered)} five-column scATAC fragment file(s) by schema",
        "file_count": len(ordered) + len(metadata_files),
        "metadata": {
            "sample_count": len(ordered),
            "fragment_file_count": len(ordered),
            "metadata_file_count": len(metadata_files),
            "metadata_pairs_complete": len(metadata_files) == len(ordered),
            "total_size_bytes": total_size,
        },
    })
    result["files"]["fragments"] = str(query_path if input_mode == "collection" else ordered[0])
    result["files"]["fragment_files"] = [str(path) for path in ordered]
    result["files"]["metadata_files"] = [str(path) for path in metadata_files]
    if len(metadata_files) != len(ordered):
        result["warnings"].append("not every fragment file has a paired *-metadata.csv; QC can continue without complete annotations")
    return result


def score_h5ad(path: Path) -> dict[str, Any]:
    result = {
        "input_kind": "peak_matrix",
        "confidence": 0.65,
        "safe_mode": "large_full_qc" if file_size(path) > 5_000_000_000 else "standard_peak_matrix_qc",
        "recommended_qc_mode": "large_full_qc" if file_size(path) > 5_000_000_000 else "standard_full_qc",
        "size_risk": "large" if file_size(path) > 5_000_000_000 else "standard",
        "reason": "h5ad extension detected; metadata inspection unavailable",
        "metadata": {"file_size_bytes": file_size(path)},
        "warnings": [],
    }
    try:
        import anndata as ad  # type: ignore

        adata = ad.read_h5ad(path, backed="r")
        try:
            first_vars = [str(item) for item in adata.var_names[:100]]
            peak_like = sum(1 for item in first_vars if is_peak_name(item))
            peak_fraction = peak_like / max(1, len(first_vars))
            obs_columns = list(map(str, adata.obs.columns))
            var_columns = list(map(str, adata.var.columns))
            metadata = {
                "n_obs": int(adata.n_obs),
                "n_vars": int(adata.n_vars),
                "obs_columns": obs_columns,
                "var_columns": var_columns,
                "peak_like_var_names_checked": len(first_vars),
                "peak_like_var_names": peak_like,
                "file_size_bytes": file_size(path),
            }
            if peak_fraction >= 0.8:
                kind = "peak_matrix"
                confidence = 0.96
                reason = f"h5ad with peak-like var_names ({peak_like}/{len(first_vars)})"
            elif any("gene" in col.lower() or "rna" in col.lower() for col in var_columns + obs_columns):
                kind = "multiome_or_expression_h5ad"
                confidence = 0.55
                reason = "h5ad detected but peak coordinates are not dominant; review for RNA/multiome content"
            else:
                kind = "unknown_h5ad"
                confidence = 0.4
                reason = f"h5ad detected but peak-like var_names are weak ({peak_like}/{len(first_vars)})"
            is_large = kind == "peak_matrix" and (
                file_size(path) > 5_000_000_000 or adata.n_obs > 200_000 or adata.n_vars > 200_000
            )
            recommended_qc_mode = "large_full_qc" if is_large else "standard_full_qc"
            # Keep safe_mode for backward compatibility; packaging is optional, not mandatory.
            safe_mode = "large_full_qc" if is_large else "standard_peak_matrix_qc"
            result.update({
                "input_kind": kind,
                "confidence": confidence,
                "safe_mode": safe_mode,
                "recommended_qc_mode": recommended_qc_mode,
                "size_risk": "large" if is_large else "standard",
                "reason": reason,
                "metadata": metadata,
            })
        finally:
            adata.file.close()
    except Exception as error:
        result["warnings"].append(f"backed h5ad inspection failed: {error}")
    return result


def score_h5(path: Path) -> dict[str, Any]:
    result = {
        "input_kind": "peak_matrix",
        "confidence": 0.65,
        "safe_mode": "standard_peak_matrix_qc",
        "reason": "h5 matrix extension detected",
        "metadata": {"file_size_bytes": file_size(path)},
        "warnings": [],
    }
    try:
        import h5py  # type: ignore

        feature_types: set[str] = set()
        with h5py.File(path, "r") as handle:
            for candidate in ["matrix/features/feature_type", "matrix/features/name", "matrix/features/id"]:
                if candidate in handle:
                    values = handle[candidate][:1000]
                    for value in values:
                        text = value.decode() if isinstance(value, bytes) else str(value)
                        if "Gene Expression" in text:
                            feature_types.add("Gene Expression")
                        if "Peaks" in text or is_peak_name(text):
                            feature_types.add("Peaks")
        if {"Gene Expression", "Peaks"}.issubset(feature_types):
            result.update({"input_kind": "multiome", "confidence": 0.9, "reason": "10x h5 contains both Gene Expression and Peaks feature types", "metadata": {"feature_types": sorted(feature_types), "file_size_bytes": file_size(path)}})
        elif "Peaks" in feature_types:
            result.update({"input_kind": "peak_matrix", "confidence": 0.9, "reason": "10x h5 contains Peaks feature type", "metadata": {"feature_types": sorted(feature_types), "file_size_bytes": file_size(path)}})
    except Exception as error:
        result["warnings"].append(f"h5 feature inspection failed: {error}")
    return result


def classify_file(path: Path) -> dict[str, Any]:
    result = base_result(path)
    name = path.name.lower()
    result["file_count"] = 1
    result["metadata"]["file_size_bytes"] = file_size(path)
    if FRAGMENT_RE.search(name) or "fragments.tsv" in name or is_fragment_file(path):
        result = fragment_result(path, [path])
    elif name.endswith(".h5ad"):
        scored = score_h5ad(path)
        result.update({
            key: scored[key]
            for key in [
                "input_kind", "confidence", "safe_mode", "recommended_qc_mode",
                "size_risk", "reason", "metadata", "warnings",
            ]
            if key in scored
        })
        result["files"]["matrix"] = str(path)
    elif name.endswith(".h5"):
        scored = score_h5(path)
        result.update({
            key: scored[key]
            for key in [
                "input_kind", "confidence", "safe_mode", "recommended_qc_mode",
                "size_risk", "reason", "metadata", "warnings",
            ]
            if key in scored
        })
        result["files"]["matrix"] = str(path)
    elif name.endswith((".mtx", ".mtx.gz", ".npz")):
        result.update({"input_kind": "peak_matrix", "confidence": 0.75, "safe_mode": "standard_peak_matrix_qc", "reason": "matrix file extension detected"})
        result["files"]["matrix"] = str(path)
    elif name.endswith((".bed", ".bed.gz")) or "peaks" in name:
        result.update({"input_kind": "peaks_only", "confidence": 0.45, "safe_mode": "needs_matrix", "reason": "peaks/BED file detected without matrix"})
        result["files"]["peaks"] = str(path)
    elif name.endswith((".fastq", ".fastq.gz", ".fq", ".fq.gz", ".sra")):
        result.update({"input_kind": "raw_reads", "confidence": 0.85, "safe_mode": "raw_preprocessing_required", "reason": "raw sequencing reads detected"})
    elif name.endswith(".csv"):
        result.update(classify_manifest(path))
    return result


def scan_directory(path: Path, max_files: int) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(path):
        dirs[:] = [item for item in dirs if item not in {".git", "__pycache__", ".cache"}]
        for name in names:
            files.append(Path(root) / name)
            if len(files) >= max_files:
                return files
    return files


def classify_directory(path: Path, max_files: int) -> dict[str, Any]:
    result = base_result(path)
    files = scan_directory(path, max_files=max_files)
    result["file_count"] = len(files)
    by_name = {item.name.lower(): item for item in files}
    fragment_files = [item for item in files if is_fragment_file(item)]
    fragment = fragment_files[0] if fragment_files else None
    h5ad = next((item for item in files if item.name.lower().endswith(".h5ad")), None)
    h5 = next((item for item in files if item.name.lower().endswith(".h5")), None)
    matrix = next((item for item in files if item.name.lower() in TEXT_MATRIX_NAMES or item.name.lower().endswith((".mtx", ".mtx.gz", ".npz"))), None)
    peaks = next((item for item in files if item.name.lower() in FEATURE_NAMES or item.name.lower().endswith((".bed", ".bed.gz")) or "peaks" in item.name.lower()), None)
    barcodes = next((item for item in files if item.name.lower() in BARCODE_NAMES), None)
    rna = next((item for item in files if "rna" in item.name.lower() or "gene" in item.name.lower() or "gex" in item.name.lower()), None)
    if h5ad:
        child = classify_file(h5ad)
        child["query_path"] = str(path)
        child["file_count"] = len(files)
        if peaks and not child["files"].get("peaks"):
            child["files"]["peaks"] = str(peaks)
        if barcodes:
            child["files"]["barcodes"] = str(barcodes)
        return child
    if h5:
        child = classify_file(h5)
        child["query_path"] = str(path)
        child["file_count"] = len(files)
        return child
    if fragment and (rna or "multiome" in path.name.lower()):
        result.update({"input_kind": "multiome", "confidence": 0.78, "safe_mode": "multiome_qc", "reason": "directory contains ATAC fragments plus RNA/GEX-like files"})
        result["files"].update({"fragments": str(fragment), "rna": str(rna or ""), "peaks": str(peaks or "")})
    elif fragment:
        result = fragment_result(path, fragment_files)
        result["file_count"] = len(files)
        result["files"]["peaks"] = str(peaks or "")
    elif matrix:
        result.update({"input_kind": "peak_matrix", "confidence": 0.82, "safe_mode": "standard_peak_matrix_qc", "reason": "directory contains matrix file"})
        result["files"].update({"matrix": str(matrix), "peaks": str(peaks or ""), "barcodes": str(barcodes or "")})
    elif peaks:
        result.update({"input_kind": "peaks_only", "confidence": 0.45, "safe_mode": "needs_matrix", "reason": "directory contains peaks but no matrix/fragments"})
        result["files"]["peaks"] = str(peaks)
    else:
        result["reason"] = "directory scanned but no supported CellNote input pattern was found"
    return result


def classify_manifest(path: Path) -> dict[str, Any]:
    result = base_result(path)
    result["files"]["manifest"] = str(path)
    result["file_count"] = 1
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as error:
        result.update({"reason": f"CSV/manifest read failed: {error}", "warnings": [str(error)]})
        return result
    result["metadata"] = {"manifest_rows": len(rows), "columns": list(rows[0].keys()) if rows else []}
    names = " ".join(str(row.get(key, "")) for row in rows for key in ["role", "file_format", "artifact_id", "local_path", "source_uri"] ).lower()
    local_paths = [
        Path(str(row.get("fragments_path") or row.get("local_path") or ""))
        for row in rows
        if str(row.get("fragments_path") or row.get("local_path") or "").strip()
    ]
    fragment_paths = [item for item in local_paths if item.is_file() and is_fragment_file(item)]
    if fragment_paths:
        child = fragment_result(path, fragment_paths)
        child["files"]["manifest"] = str(path)
        child["metadata"]["manifest_rows"] = len(rows)
        return child
    for item in local_paths:
        if item.exists():
            child = classify_file(item) if item.is_file() else classify_directory(item, max_files=200)
            if child["input_kind"] != "unknown":
                child["files"]["manifest"] = str(path)
                child["metadata"]["manifest_rows"] = len(rows)
                return child
    if "fragment" in names:
        result.update({"input_kind": "fragments", "confidence": 0.75, "safe_mode": "fragment_qc", "reason": "manifest references fragments"})
    elif "rna" in names and ("atac" in names or "peak" in names):
        result.update({"input_kind": "multiome", "confidence": 0.7, "safe_mode": "multiome_qc", "reason": "manifest references RNA and ATAC/peak files"})
    elif "matrix" in names or "peak" in names or ".h5" in names or ".mtx" in names:
        result.update({"input_kind": "peak_matrix", "confidence": 0.65, "safe_mode": "standard_peak_matrix_qc", "reason": "manifest references matrix/peak files"})
    elif "fastq" in names or ".sra" in names:
        result.update({"input_kind": "raw_reads", "confidence": 0.75, "safe_mode": "raw_preprocessing_required", "reason": "manifest references raw reads"})
    return result


def detect(path: Path, max_files: int, genome_build: str | None = None) -> dict[str, Any]:
    result = base_result(path)
    if not path.exists():
        result["reason"] = "path does not exist"
        return result
    if path.is_dir():
        result = classify_directory(path, max_files=max_files)
    else:
        result = classify_file(path)
    if genome_build:
        result["genome_build"] = genome_build
    elif result.get("input_kind") in {"peak_matrix", "fragments", "multiome"}:
        result["genome_build"] = "unknown_requires_user_or_prompt"
    result["requires_user_confirmation"] = result.get("confidence", 0) < 0.9 or result.get("genome_build", "").startswith("unknown")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect CellNote input type from a file, directory, or manifest.")
    parser.add_argument("--path", required=True, help="input file/directory/manifest/run folder")
    parser.add_argument("--max_files", type=int, default=500, help="max files to scan inside a directory")
    parser.add_argument("--genome_build", help="user-specified genome build, e.g. GRCh38/hg38/hg19")
    args = parser.parse_args(argv)
    result = detect(Path(args.path).expanduser().resolve(), max_files=args.max_files, genome_build=args.genome_build)
    print(json.dumps(jsonable(result), indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("exists") else 2


if __name__ == "__main__":
    raise SystemExit(main())
