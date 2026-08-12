#!/usr/bin/env python
"""scatac-peak-matrix: process existing scATAC cell-by-peak matrices.

Supports standard in-memory QC for moderate matrices and backed/chunked QC for
large h5ad inputs. Final output is an independent GRCh38 peak matrix package.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from _common import require_files, run_stages, software_versions, stage_subdir

LARGE_FILE_BYTES = 5_000_000_000
LARGE_N_OBS = 200_000
LARGE_N_VARS = 200_000
DEFAULT_CHUNK = 4096


def _optional_import(name: str):
    try:
        import importlib
        return importlib.import_module(name)
    except ImportError as error:
        raise SystemExit(f"[error] optional dependency '{name}' is required for this stage: {error}") from error


def _out_dir(args) -> str:
    return stage_subdir(args.results_root, "processed", args.dataset_id)


def _h5ad_path(args) -> str:
    return os.path.join(_out_dir(args), "peak_matrix.h5ad")


def _prepare_output_h5ad(path: str) -> str:
    """Make output path writable; never write through a symlink into source data.

    Packaging runs may leave peak_matrix.h5ad as a symlink to the original
    matrix. AnnData then opens that path for write and hits PermissionError on
    the read-only source. Always unlink first, then write a real new file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        link_note = f" -> {os.path.realpath(target)}" if target.is_symlink() else ""
        print(f"[write] removing existing {'symlink' if target.is_symlink() else 'file'} before overwrite: {target}{link_note}")
        try:
            target.unlink()
        except OSError as error:
            raise SystemExit(
                f"[write] cannot remove existing output path {target}: {error}. "
                "Remove the symlink/file manually, then re-run this stage."
            ) from error
    if target.exists() or target.is_symlink():
        raise SystemExit(f"[write] output path still exists after unlink: {target}")
    return str(target)


def _is_real_file(path: str) -> bool:
    """True only for a regular file (not a symlink / missing path)."""
    target = Path(path)
    return target.is_file() and not target.is_symlink()


def _require_filtered_peak_matrix(args) -> str:
    """Fail fast if peak_matrix.h5ad is missing, a symlink, or filter never finished."""
    path = _h5ad_path(args)
    if Path(path).is_symlink():
        raise SystemExit(
            f"[error] {path} is a symlink (likely packaging leftover). "
            "Re-run filter so a real filtered matrix is written."
        )
    require_files(path)
    summary = _load_summary(args)
    if summary.get("qc_mode") in {"full", "full_backed"} and not summary.get("filter_thresholds"):
        raise SystemExit(
            "[error] peak_matrix exists but filter did not complete "
            "(qc_summary missing filter_thresholds). Re-run filter before later stages."
        )
    return path


def _summary_path(args) -> str:
    return os.path.join(_out_dir(args), "qc_summary.json")


def _metrics_path(args) -> str:
    return os.path.join(_out_dir(args), "cell_peak_metrics.npz")


def _load_summary(args) -> dict:
    path = _summary_path(args)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def _save_summary(args, summary: dict) -> None:
    path = _summary_path(args)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)


def _save_plot(fig, out_dir: str, name: str) -> str:
    path = os.path.join(out_dir, f"{name}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    print(f"  [plot] {path}")
    return path


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _is_large_matrix(path: str, n_obs: int | None = None, n_vars: int | None = None) -> bool:
    if _file_size(path) > LARGE_FILE_BYTES:
        return True
    if n_obs is not None and n_obs > LARGE_N_OBS:
        return True
    if n_vars is not None and n_vars > LARGE_N_VARS:
        return True
    return False


def _use_backed(args, n_obs: int | None = None, n_vars: int | None = None) -> bool:
    if getattr(args, "force_in_memory", False):
        return False
    if getattr(args, "backed", False):
        return True
    return args.matrix.endswith(".h5ad") and _is_large_matrix(args.matrix, n_obs=n_obs, n_vars=n_vars)


def _as_csr(matrix):
    scipy_sparse = _optional_import("scipy.sparse")
    if scipy_sparse.issparse(matrix):
        return matrix.tocsr()
    return scipy_sparse.csr_matrix(matrix)


def _chunk_metrics_from_matrix(X, n_obs: int, n_vars: int, chunk_size: int):
    np = _optional_import("numpy")
    n_peaks = np.zeros(n_obs, dtype=np.int64)
    total_counts = np.zeros(n_obs, dtype=np.float64)
    n_cells = np.zeros(n_vars, dtype=np.int64)
    for start in range(0, n_obs, chunk_size):
        end = min(start + chunk_size, n_obs)
        block = _as_csr(X[start:end])
        n_peaks[start:end] = np.asarray((block > 0).sum(axis=1)).ravel()
        total_counts[start:end] = np.asarray(block.sum(axis=1)).ravel()
        n_cells += np.asarray((block > 0).sum(axis=0)).ravel()
        print(f"  [metrics] rows {start}:{end}/{n_obs}")
    return n_peaks, total_counts, n_cells


def _write_gzip(path: str, lines: list[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")


def _copy_clean_bed(input_path: str, output_path: str) -> int:
    count = 0
    with open(input_path, "r", encoding="utf-8") as source, open(output_path, "w", encoding="utf-8") as target:
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
    if count == 0:
        raise SystemExit(f"[error] no valid BED intervals found in {input_path}")
    return count


def _parse_peak_coord(name: str) -> tuple[str, int, int] | None:
    """Parse "chr:start-end" -> tuple, or None when unparsable/invalid."""
    if ":" not in name or "-" not in name:
        return None
    chrom, rest = name.split(":", 1)
    start_str, _, end_str = rest.replace(",", "").partition("-")
    try:
        start, end = int(start_str), int(end_str)
    except ValueError:
        return None
    if start < 0 or end <= start:
        return None
    return chrom, start, end


def _matrix_quality_metrics(X, var_names: list[str]) -> dict:
    """Deliverable-quality stats: shape, sparsity, cells/peak, coord validity."""
    import statistics

    n_obs, n_vars = int(X.shape[0]), int(X.shape[1])
    if hasattr(X, "nnz"):
        nnz = int(X.nnz)
    else:
        nnz = int((X > 0).sum())
    if hasattr(X, "getnnz"):
        cells_per_peak = [int(v) for v in X.getnnz(axis=0)]
    else:
        counts = (X > 0).sum(axis=0)
        if hasattr(counts, "A1"):
            counts = counts.A1
        elif hasattr(counts, "ravel"):
            counts = counts.ravel()
        cells_per_peak = [int(v) for v in counts]
    n_valid = sum(1 for name in var_names if _parse_peak_coord(str(name)) is not None)
    n_total = len(var_names)
    return {
        "shape": [n_obs, n_vars],
        "nnz": nnz,
        "density": round(nnz / (n_obs * n_vars), 6) if n_obs and n_vars else 0.0,
        "cells_per_peak_median": (
            statistics.median(cells_per_peak) if cells_per_peak else None
        ),
        "peak_coordinate_validity": {
            "n_valid": n_valid,
            "n_total": n_total,
            "fraction_valid": round(n_valid / n_total, 4) if n_total else 0.0,
        },
    }


def _liftover_peaks(
    chain_path: str, parsed: list[tuple[str, int, int]]
) -> tuple[list[tuple[str, int, int]], list[int], dict]:
    """Lift half-open intervals via pyliftover point conversion.

    Start and end-1 are converted independently; a peak survives only if both
    ends map to the same chromosome and strand and the reconstructed interval
    is non-empty. Peaks whose lifted target duplicates another peak's target
    are dropped as ambiguous (many-to-one mappings corrupt a peak universe).
    """
    from collections import Counter

    pyliftover = _optional_import("pyliftover")
    lifter = pyliftover.LiftOver(chain_path)
    candidates: list[tuple[int, tuple[str, int, int]]] = []
    n_failed = 0
    for idx, (chrom, start, end) in enumerate(parsed):
        start_hits = lifter.convert_coordinate(chrom, start)
        end_hits = lifter.convert_coordinate(chrom, end - 1)
        if not start_hits or not end_hits:
            n_failed += 1
            continue
        s_chrom, s_pos, s_strand = start_hits[0][0], int(start_hits[0][1]), start_hits[0][2]
        e_chrom, e_pos, e_strand = end_hits[0][0], int(end_hits[0][1]), end_hits[0][2]
        if s_chrom != e_chrom or s_strand != e_strand:
            n_failed += 1
            continue
        if s_strand == "-":
            new_start, new_end = e_pos, s_pos + 1
        else:
            new_start, new_end = s_pos, e_pos + 1
        if new_end <= new_start:
            n_failed += 1
            continue
        candidates.append((idx, (s_chrom, new_start, new_end)))

    target_counts = Counter(target for _, target in candidates)
    lifted: list[tuple[str, int, int]] = []
    kept_idx: list[int] = []
    n_duplicates = 0
    for idx, target in candidates:
        if target_counts[target] > 1:
            n_duplicates += 1
            continue
        kept_idx.append(idx)
        lifted.append(target)
    return lifted, kept_idx, {"n_failed": n_failed, "n_duplicate_targets": n_duplicates}


def _liftover_matrix_to_hg38(args, adata, summary: dict):
    """hg19/GRCh37 deliverable -> GRCh38 coordinates, or fail loudly."""
    if not args.liftover_chain:
        raise SystemExit(
            "[standardize] genome_build=hg19/GRCh37 requires --liftover_chain; "
            "run resource-setup fetch --include_liftover and pass "
            "reference/hg19ToHg38.over.chain.gz"
        )
    require_files(args.liftover_chain)
    names = [str(x) for x in adata.var_names]
    parsed = []
    for name in names:
        coord = _parse_peak_coord(name)
        if coord is None:
            raise SystemExit(
                f"[standardize] cannot liftover: peak name '{name}' lacks valid "
                "chr:start-end coordinates"
            )
        parsed.append(coord)
    lifted, kept_idx, stats = _liftover_peaks(args.liftover_chain, parsed)
    n_input = len(parsed)
    rate = len(kept_idx) / n_input if n_input else 0.0
    if rate < args.min_liftover_rate:
        raise SystemExit(
            f"[standardize] liftover success rate {rate:.4f} is below "
            f"--min_liftover_rate {args.min_liftover_rate} "
            f"(lifted {len(kept_idx)}/{n_input}, failed {stats['n_failed']}, "
            f"duplicate targets {stats['n_duplicate_targets']}); route to review"
        )
    sub = adata[:, kept_idx].copy()
    sub.var_names = [f"{chrom}:{start}-{end}" for chrom, start, end in lifted]
    out_h5ad = _prepare_output_h5ad(_h5ad_path(args))
    sub.write(out_h5ad)
    summary["liftover"] = {
        "from_build": args.genome_build,
        "to_build": "GRCh38",
        "chain": args.liftover_chain,
        "n_input": n_input,
        "n_lifted": len(kept_idx),
        "n_failed": stats["n_failed"],
        "n_duplicate_targets": stats["n_duplicate_targets"],
        "rate": round(rate, 4),
        "min_liftover_rate": args.min_liftover_rate,
    }
    print(
        f"[standardize] liftover {args.genome_build}->GRCh38: "
        f"{len(kept_idx)}/{n_input} peaks kept (rate {rate:.4f})"
    )
    return sub


def _copy_or_write_peaks(args, var_names) -> str:
    out_dir = _out_dir(args)
    out_path = os.path.join(out_dir, "peaks.hg38.bed")
    peaks = [str(name) for name in var_names]
    rows = []
    for peak in peaks:
        if ":" in peak and "-" in peak:
            chrom, rest = peak.split(":", 1)
            start, end = rest.replace(",", "").split("-", 1)
            rows.append(f"{chrom}\t{int(start)}\t{int(end)}")
    if len(rows) == len(peaks):
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
        return out_path
    if args.peaks:
        require_files(args.peaks)
        _copy_clean_bed(args.peaks, out_path)
        return out_path
    raise SystemExit("[error] peak coordinates missing or unparsable; provide --peaks")


def _plot_qc_distributions(args, n_peaks, total_counts, out_dir: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    np = _optional_import("numpy")
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    axes[0].hist(np.asarray(n_peaks), bins=50, edgecolor="none")
    axes[0].axvline(args.min_peaks, color="red", linestyle="--", label=f"min_peaks={args.min_peaks}")
    axes[0].set_xlabel("Detected peaks per cell")
    axes[0].legend()
    axes[1].hist(np.asarray(total_counts), bins=50, edgecolor="none")
    axes[1].axvline(args.min_counts, color="red", linestyle="--", label=f"min_counts={args.min_counts}")
    axes[1].set_xlabel("Total counts per cell")
    axes[1].legend()
    _save_plot(fig, out_dir, "matrix_qc_distributions")


def _subset_to_peak_features(adata):
    """Keep only ``Peaks`` features when the matrix carries 10x feature_types.

    10x ARC combined matrices (``read_10x_h5(gex_only=False)``) mix Gene
    Expression and Peaks features; gene rows must never enter the peak matrix
    deliverable. Matrices without feature_types metadata pass through
    unchanged (coordinate validation still guards downstream).
    """
    var = getattr(adata, "var", None)
    if var is None or "feature_types" not in var:
        return adata, 0
    types_series = var["feature_types"].astype(str)
    unique = set(types_series)
    if unique == {"Peaks"}:
        return adata, 0
    if "Peaks" not in unique:
        raise SystemExit(
            "[load] --matrix contains no 'Peaks' features (found: "
            + ", ".join(sorted(unique))
            + "); pass the ATAC peak matrix, not a GEX matrix"
        )
    mask = (types_series == "Peaks").to_numpy()
    n_removed = int((~mask).sum())
    subset = adata[:, mask].copy()
    print(f"[load] dropped {n_removed} non-Peaks feature(s); kept {int(mask.sum())} Peaks")
    return subset, n_removed


def _require_peak_only_var(var) -> None:
    """Backed mode cannot subset features; refuse mixed matrices up front."""
    if var is None or "feature_types" not in var:
        return
    unique = set(var["feature_types"].astype(str))
    if unique - {"Peaks"}:
        raise SystemExit(
            "[load] backed matrix mixes feature types (found: "
            + ", ".join(sorted(unique))
            + "); subset it to Peaks first or rerun without --backed"
        )


def _read_matrix_in_memory(args):
    sc = _optional_import("scanpy")
    if args.matrix.endswith(".h5ad"):
        return sc.read(args.matrix)
    if args.matrix.endswith(".h5"):
        return sc.read_10x_h5(args.matrix, gex_only=False)
    if os.path.isdir(args.matrix):
        return sc.read_10x_mtx(args.matrix, var_names="gene_symbols")
    raise ValueError(f"Cannot determine matrix format: {args.matrix}")


def load(args):
    np = _optional_import("numpy")
    require_files(args.matrix)
    out_dir = _out_dir(args)
    chunk_size = max(256, int(getattr(args, "chunk_size", DEFAULT_CHUNK) or DEFAULT_CHUNK))

    if args.matrix.endswith(".h5ad"):
        ad = _optional_import("anndata")
        probe = ad.read_h5ad(args.matrix, backed="r")
        try:
            n_obs, n_vars = int(probe.n_obs), int(probe.n_vars)
        finally:
            probe.file.close()
    else:
        n_obs = n_vars = None

    if _use_backed(args, n_obs=n_obs, n_vars=n_vars):
        ad = _optional_import("anndata")
        adata = ad.read_h5ad(args.matrix, backed="r")
        try:
            _require_peak_only_var(adata.var)
            print(f"[load] backed mode: {adata.n_obs} cells x {adata.n_vars} peaks")
            n_peaks, total_counts, n_cells = _chunk_metrics_from_matrix(
                adata.X, adata.n_obs, adata.n_vars, chunk_size
            )
            obs_names = [str(x) for x in adata.obs_names]
            var_names = [str(x) for x in adata.var_names]
            peaks_path = _copy_or_write_peaks(args, var_names) if args.genome_build in {"GRCh38", "hg38"} else None
            np.savez_compressed(
                _metrics_path(args),
                n_peaks=n_peaks,
                total_counts=total_counts,
                n_cells=n_cells,
                obs_names=np.asarray(obs_names, dtype=object),
                var_names=np.asarray(var_names, dtype=object),
            )
            _write_gzip(os.path.join(out_dir, "barcodes.tsv.gz"), obs_names)
            _plot_qc_distributions(args, n_peaks, total_counts, out_dir)
            summary = {
                "dataset_id": args.dataset_id,
                "genome_build": args.genome_build,
                "target_genome_build": "GRCh38",
                "input_matrix": args.matrix,
                "input_peaks": args.peaks,
                "n_cells_loaded": int(adata.n_obs),
                "n_peaks_loaded": int(adata.n_vars),
                "counts_median": float(np.median(total_counts)),
                "detected_peaks_median": float(np.median(n_peaks)),
                "representation_quality": "matrix_only",
                "qc_mode": "full_backed",
                "working_matrix_source": os.path.abspath(args.matrix),
                "metrics_file": _metrics_path(args),
                "peaks_file": peaks_path,
                "software_versions": software_versions("scanpy", "anndata", "numpy", "scipy"),
                "stages_completed": ["load"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_summary(args, summary)
            print(f"[load] backed metrics saved: {_metrics_path(args)}")
            print("[load] deferred writing filtered peak_matrix.h5ad until filter stage")
        finally:
            adata.file.close()
        return

    adata = _read_matrix_in_memory(args)
    adata, n_nonpeak_dropped = _subset_to_peak_features(adata)
    adata.var_names_make_unique()
    adata.obs_names_make_unique()
    print(f"[load] in-memory mode: {adata.n_obs} cells x {adata.n_vars} peaks")
    x = adata.X
    adata.obs["n_peaks"] = np.asarray((x > 0).sum(axis=1)).ravel()
    adata.obs["total_counts"] = np.asarray(x.sum(axis=1)).ravel()
    adata.var["n_cells"] = np.asarray((x > 0).sum(axis=0)).ravel()
    _plot_qc_distributions(args, adata.obs["n_peaks"], adata.obs["total_counts"], out_dir)
    peaks_path = _copy_or_write_peaks(args, adata.var_names) if args.genome_build in {"GRCh38", "hg38"} else None
    out_h5ad = _prepare_output_h5ad(_h5ad_path(args))
    adata.write(out_h5ad)
    _write_gzip(os.path.join(out_dir, "barcodes.tsv.gz"), [str(x) for x in adata.obs_names])
    summary = {
        "dataset_id": args.dataset_id,
        "genome_build": args.genome_build,
        "target_genome_build": "GRCh38",
        "input_matrix": args.matrix,
        "input_peaks": args.peaks,
        "n_cells_loaded": int(adata.n_obs),
        "n_peaks_loaded": int(adata.n_vars),
        "n_nonpeak_features_dropped": n_nonpeak_dropped,
        "counts_median": float(np.median(adata.obs["total_counts"])),
        "detected_peaks_median": float(np.median(adata.obs["n_peaks"])),
        "representation_quality": "matrix_only",
        "qc_mode": "full",
        "peaks_file": peaks_path,
        "software_versions": software_versions("scanpy", "anndata", "numpy", "scipy"),
        "stages_completed": ["load"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_summary(args, summary)
    print(f"[load] saved working matrix: {out_h5ad}")


def profile(args):
    summary = _load_summary(args)
    if not summary:
        raise SystemExit("[profile] missing qc_summary.json; run load first")
    print(json.dumps({k: summary.get(k) for k in [
        "n_cells_loaded", "n_peaks_loaded", "counts_median", "detected_peaks_median", "qc_mode"
    ]}, indent=2))
    summary.setdefault("stages_completed", []).append("profile")
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args, summary)


def _kept_peak_mask(X, keep_cells, min_cells_per_peak: int):
    """Peak prevalence counted among kept cells only.

    This is the semantic the backed path implements; the in-memory path must
    match it so identical thresholds produce identical matrices.
    """
    np = _optional_import("numpy")
    keep = np.asarray(keep_cells, dtype=bool)
    sub = X[keep]
    prevalence = np.asarray((sub > 0).sum(axis=0)).ravel()
    return prevalence >= int(min_cells_per_peak), prevalence


def _filter_in_memory(args):
    np = _optional_import("numpy")
    sc = _optional_import("scanpy")
    require_files(_h5ad_path(args))
    adata = sc.read(_h5ad_path(args))
    n_cells_before, n_peaks_before = adata.n_obs, adata.n_vars
    keep_cells = np.asarray(
        (adata.obs["n_peaks"] >= args.min_peaks) & (adata.obs["total_counts"] >= args.min_counts)
    ).astype(bool, copy=False)
    keep_peaks, prevalence = _kept_peak_mask(adata.X, keep_cells, args.min_cells_per_peak)
    adata = adata[keep_cells, keep_peaks].copy()
    adata.var["n_cells"] = prevalence[keep_peaks]
    out_h5ad = _prepare_output_h5ad(_h5ad_path(args))
    adata.write(out_h5ad)
    _write_gzip(os.path.join(_out_dir(args), "barcodes.tsv.gz"), [str(x) for x in adata.obs_names])
    return n_cells_before, n_peaks_before, int(adata.n_obs), int(adata.n_vars)


def _filter_backed(args):
    """Backed filter via contiguous CSR row scans (not fancy HDF5 indexing).

    Fancy ``adata[kept_idx, peak_idx]`` on large CSRDataset is extremely slow.
    Contiguous ``X[start:end]`` + in-memory boolean masks preserve the same
    QC thresholds while finishing orders of magnitude faster.
    """
    np = _optional_import("numpy")
    ad = _optional_import("anndata")
    scipy_sparse = _optional_import("scipy.sparse")
    import pandas as pd

    summary = _load_summary(args)
    source = summary.get("working_matrix_source") or args.matrix
    require_files(source, _metrics_path(args))
    metrics = np.load(_metrics_path(args), allow_pickle=True)
    n_peaks = metrics["n_peaks"]
    total_counts = metrics["total_counts"]
    keep_cells = np.asarray(
        (n_peaks >= args.min_peaks) & (total_counts >= args.min_counts)
    ).astype(bool, copy=False)
    n_keep_cells = int(keep_cells.sum())
    print(f"[filter] backed mode: keeping {n_keep_cells}/{keep_cells.size} cells by thresholds")
    if n_keep_cells == 0:
        raise SystemExit("[filter] no cells passed min_peaks/min_counts; relax thresholds")
    if n_keep_cells > LARGE_N_OBS:
        print(
            f"[filter] warning: keeping {n_keep_cells} cells; "
            "writing filtered matrix may require substantial memory/time"
        )

    adata = ad.read_h5ad(source, backed="r")
    try:
        n_cells_before, n_peaks_before = int(adata.n_obs), int(adata.n_vars)
        if keep_cells.size != n_cells_before:
            raise SystemExit(
                f"[filter] metrics length {keep_cells.size} != matrix n_obs {n_cells_before}"
            )
        chunk_size = max(256, int(getattr(args, "chunk_size", DEFAULT_CHUNK) or DEFAULT_CHUNK))
        # Larger contiguous reads improve HDF5 CSR throughput on big matrices.
        if n_cells_before > LARGE_N_OBS:
            chunk_size = max(chunk_size, 8192)

        # Pass 1: peak prevalence among kept cells only (contiguous scan).
        n_cells = np.zeros(n_peaks_before, dtype=np.int64)
        seen_kept = 0
        for start in range(0, n_cells_before, chunk_size):
            end = min(start + chunk_size, n_cells_before)
            row_mask = keep_cells[start:end]
            if not np.any(row_mask):
                continue
            block = _as_csr(adata.X[start:end])[row_mask]
            n_cells += np.asarray((block > 0).sum(axis=0)).ravel()
            seen_kept += int(row_mask.sum())
            print(
                f"  [filter] peak stats rows {start}:{end}/{n_cells_before} "
                f"(kept {seen_kept}/{n_keep_cells})",
                flush=True,
            )
        if seen_kept != n_keep_cells:
            raise SystemExit(
                f"[filter] internal error: scanned kept cells {seen_kept} != {n_keep_cells}"
            )
        keep_peaks = n_cells >= args.min_cells_per_peak
        n_keep_peaks = int(np.count_nonzero(keep_peaks))
        if n_keep_peaks == 0:
            raise SystemExit("[filter] no peaks passed min_cells_per_peak; relax thresholds")
        print(f"[filter] keeping {n_keep_peaks}/{n_peaks_before} peaks by min_cells_per_peak")

        # Pass 2: materialize filtered matrix with contiguous reads + boolean column mask.
        blocks = []
        kept_row_ids = []
        seen_kept = 0
        merge_every = 16  # periodically collapse blocks to limit Python list overhead
        for start in range(0, n_cells_before, chunk_size):
            end = min(start + chunk_size, n_cells_before)
            row_mask = keep_cells[start:end]
            if not np.any(row_mask):
                continue
            block = _as_csr(adata.X[start:end])[row_mask][:, keep_peaks]
            blocks.append(block)
            kept_row_ids.append(np.arange(start, end, dtype=np.int64)[row_mask])
            seen_kept += int(row_mask.sum())
            if len(blocks) >= merge_every:
                blocks = [scipy_sparse.vstack(blocks, format="csr")]
            print(
                f"  [filter] materialize rows {start}:{end}/{n_cells_before} "
                f"(kept {seen_kept}/{n_keep_cells}, nnz={block.nnz})",
                flush=True,
            )
        if seen_kept != n_keep_cells:
            raise SystemExit(
                f"[filter] internal error: materialized kept cells {seen_kept} != {n_keep_cells}"
            )
        print("[filter] stacking filtered blocks...", flush=True)
        X = blocks[0] if len(blocks) == 1 else scipy_sparse.vstack(blocks, format="csr")
        row_ids = np.concatenate(kept_row_ids)
        if row_ids.size != X.shape[0]:
            raise SystemExit("[filter] internal error: obs rows != matrix rows after materialize")
        obs = adata.obs.iloc[row_ids].copy()
        obs["n_peaks"] = n_peaks[row_ids]
        obs["total_counts"] = total_counts[row_ids]
        var = adata.var.iloc[np.flatnonzero(keep_peaks)].copy()
        filtered = ad.AnnData(X=X, obs=obs, var=var)
        filtered.obs_names_make_unique()
        filtered.var_names_make_unique()
        filtered.var["n_cells"] = n_cells[keep_peaks]
        out_h5ad = _prepare_output_h5ad(_h5ad_path(args))
        print(f"[filter] writing {filtered.n_obs} x {filtered.n_vars} -> {out_h5ad}", flush=True)
        filtered.write(out_h5ad)
        print(f"[filter] wrote filtered matrix: {out_h5ad}", flush=True)
        _write_gzip(os.path.join(_out_dir(args), "barcodes.tsv.gz"), [str(x) for x in filtered.obs_names])
        return n_cells_before, n_peaks_before, int(filtered.n_obs), int(filtered.n_vars)
    finally:
        adata.file.close()


def filter_cells(args):
    summary = _load_summary(args)
    h5ad = _h5ad_path(args)
    # Symlink leftovers (packaging) must not force in-memory filter on the source matrix.
    use_backed = (
        summary.get("qc_mode") == "full_backed"
        or (not _is_real_file(h5ad) and os.path.exists(_metrics_path(args)))
        or Path(h5ad).is_symlink()
    )
    if use_backed:
        n_cells_before, n_peaks_before, n_cells_after, n_peaks_after = _filter_backed(args)
        qc_mode = "full_backed"
    else:
        n_cells_before, n_peaks_before, n_cells_after, n_peaks_after = _filter_in_memory(args)
        qc_mode = summary.get("qc_mode") or "full"

    summary = _load_summary(args)
    summary.update({
        "qc_mode": qc_mode,
        "n_cells_pass_filter": n_cells_after,
        "n_cells_removed_filter": int(n_cells_before - n_cells_after),
        "n_peaks_pass_filter": n_peaks_after,
        "n_peaks_removed_filter": int(n_peaks_before - n_peaks_after),
        "filter_thresholds": {
            "min_peaks": args.min_peaks,
            "min_counts": args.min_counts,
            "min_cells_per_peak": args.min_cells_per_peak,
        },
        "peak_matrix": _h5ad_path(args),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    summary.setdefault("stages_completed", []).append("filter")
    _save_summary(args, summary)
    print(f"[filter] cells {n_cells_before}->{n_cells_after}; peaks {n_peaks_before}->{n_peaks_after}")


def standardize(args):
    h5ad = _require_filtered_peak_matrix(args)
    summary = _load_summary(args)
    if args.genome_build not in {"GRCh38", "hg38", "GRCh37", "hg19"}:
        raise SystemExit(
            f"[standardize] unsupported genome_build '{args.genome_build}': only "
            "GRCh38/hg38 (native) and GRCh37/hg19 (liftover) are handled; route to review"
        )
    if args.genome_build in {"GRCh37", "hg19"} and not args.liftover_chain:
        # Argument validation must precede dependency imports.
        raise SystemExit(
            "[standardize] genome_build=hg19/GRCh37 requires --liftover_chain; "
            "run resource-setup fetch --include_liftover and pass "
            "reference/hg19ToHg38.over.chain.gz"
        )
    sc = _optional_import("scanpy")
    adata = sc.read(h5ad)
    if args.genome_build in {"GRCh37", "hg19"}:
        adata = _liftover_matrix_to_hg38(args, adata, summary)
    peaks_path = _copy_or_write_peaks(args, adata.var_names)
    summary["genome_build"] = "GRCh38"
    summary["peaks_file"] = peaks_path
    quality = _matrix_quality_metrics(adata.X, [str(x) for x in adata.var_names])
    summary["matrix_quality"] = quality
    validity = quality["peak_coordinate_validity"]
    if validity["fraction_valid"] < 1.0:
        print(
            f"[standardize] WARNING: only {validity['n_valid']}/{validity['n_total']} "
            "peak names carry valid chr:start-end coordinates"
        )
    summary.setdefault("stages_completed", []).append("standardize")
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args, summary)
    print(
        f"[standardize] GRCh38 peaks: {peaks_path}; density "
        f"{quality['density']}, cells/peak median {quality['cells_per_peak_median']}"
    )


def _embed_cluster_annotations(sc, adata, *, n_comps: int, leiden_res: float):
    """Run normalize/log1p/PCA/UMAP/Leiden on a copy.

    The delivery matrix keeps raw counts in ``X``; only labels and embeddings
    are copied back by the caller.
    """
    work = adata.copy()
    sc.pp.normalize_total(work, target_sum=1e4)
    sc.pp.log1p(work)
    sc.tl.pca(work, n_comps=n_comps)
    sc.pp.neighbors(work)
    sc.tl.umap(work)
    sc.tl.leiden(work, resolution=leiden_res)
    return work


def embed_cluster(args):
    summary = _load_summary(args)
    h5ad = _require_filtered_peak_matrix(args)
    skip = bool(getattr(args, "skip_embed_cluster", False) or summary.get("skip_embed_cluster"))
    if skip:
        summary["embed_cluster_skipped"] = True
        summary["embed_cluster_skip_reason"] = "skip_embed_cluster requested for large/ultra matrix"
        summary.setdefault("stages_completed", []).append("embed-cluster")
        summary["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_summary(args, summary)
        print("[embed-cluster] skipped by request (large/ultra matrix default)")
        return

    sc = _optional_import("scanpy")
    adata = sc.read(h5ad)
    if adata.n_obs < 3:
        print("[embed-cluster] too few cells; skipping")
        summary["embed_cluster_skipped"] = True
        summary["embed_cluster_skip_reason"] = "too_few_cells"
        summary.setdefault("stages_completed", []).append("embed-cluster")
        summary["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_summary(args, summary)
        return
    # Ultra-large after filter: auto-skip unless explicitly forced.
    if not getattr(args, "force_embed_cluster", False) and (
        adata.n_obs > LARGE_N_OBS or adata.n_vars > LARGE_N_VARS
    ):
        summary["embed_cluster_skipped"] = True
        summary["embed_cluster_skip_reason"] = f"auto-skip after filter for large matrix ({adata.n_obs} x {adata.n_vars})"
        summary.setdefault("stages_completed", []).append("embed-cluster")
        summary["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_summary(args, summary)
        print(f"[embed-cluster] auto-skipped for large filtered matrix: {adata.n_obs} x {adata.n_vars}")
        return

    work = _embed_cluster_annotations(
        sc,
        adata,
        n_comps=max(1, min(30, adata.n_obs - 1, adata.n_vars - 1)),
        leiden_res=args.leiden_res,
    )
    adata.obs["leiden"] = work.obs["leiden"]
    for key in ("X_pca", "X_umap"):
        if key in getattr(work, "obsm", {}):
            adata.obsm[key] = work.obsm[key]
    out_h5ad = _prepare_output_h5ad(h5ad)
    adata.write(out_h5ad)
    summary = _load_summary(args)
    summary["n_clusters"] = int(adata.obs["leiden"].nunique())
    summary["embed_cluster_skipped"] = False
    summary["embed_cluster_preserves_counts"] = True
    summary.setdefault("stages_completed", []).append("embed-cluster")
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args, summary)
    print(f"[embed-cluster] {summary['n_clusters']} clusters")


def finalize(args):
    h5ad = _require_filtered_peak_matrix(args)
    out_dir = _out_dir(args)
    summary = _load_summary(args)
    if summary.get("genome_build") not in {"GRCh38", "hg38"}:
        raise SystemExit("[finalize] final peak matrix must be GRCh38/hg38")
    summary["peak_matrix"] = h5ad
    summary["barcodes_file"] = os.path.join(out_dir, "barcodes.tsv.gz")
    summary["representation_quality"] = "matrix_only"
    summary.setdefault("qc_mode", "full")
    summary.setdefault("stages_completed", []).append("finalize")
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args, summary)
    card = {
        "dataset_id": args.dataset_id,
        "deliverable": "grch38_per_dataset_peak_matrix",
        "genome_build": summary.get("genome_build"),
        "files": {
            "peak_matrix": summary["peak_matrix"],
            "peaks": summary.get("peaks_file"),
            "barcodes": summary["barcodes_file"],
            "qc_summary": _summary_path(args),
        },
        "qc_summary": summary,
    }
    with open(os.path.join(out_dir, "data_card.json"), "w", encoding="utf-8") as handle:
        json.dump(card, handle, indent=2, ensure_ascii=False)
    print(f"[finalize] peak matrix package ready: {out_dir}")
    if summary.get("filter_thresholds"):
        print(
            "[finalize] filter applied: "
            f"cells {summary.get('n_cells_loaded')}->{summary.get('n_cells_pass_filter')}, "
            f"peaks {summary.get('n_peaks_loaded')}->{summary.get('n_peaks_pass_filter')}"
        )
    if summary.get("embed_cluster_skipped"):
        print(f"[finalize] embed-cluster skipped: {summary.get('embed_cluster_skip_reason')}")


def _parser(parser):
    parser.add_argument("--matrix", required=True, help="cell x peak matrix (mtx dir/h5/h5ad)")
    parser.add_argument("--peaks", help="peaks.bed/features.tsv with peak coordinates")
    parser.add_argument("--genome_build", default="GRCh38")
    parser.add_argument("--results_root", required=True)
    parser.add_argument("--dataset_id", required=True)
    parser.add_argument("--min_peaks", type=int, default=500)
    parser.add_argument("--min_counts", type=int, default=1000)
    parser.add_argument("--min_cells_per_peak", type=int, default=10)
    parser.add_argument("--liftover_chain", help="UCSC chain file for hg19/GRCh37 input (reference/hg19ToHg38.over.chain.gz)")
    parser.add_argument("--min_liftover_rate", type=float, default=0.95, help="abort standardize when the lifted-peak fraction falls below this")
    parser.add_argument("--leiden_res", type=float, default=1.0)
    parser.add_argument("--chunk_size", type=int, default=DEFAULT_CHUNK, help="row chunk size for backed metrics/filter")
    parser.add_argument("--backed", action="store_true", help="force backed/chunked mode for h5ad")
    parser.add_argument("--force_in_memory", action="store_true", help="force full in-memory load")
    parser.add_argument("--skip_embed_cluster", action="store_true", help="skip embed-cluster stage work")
    parser.add_argument("--force_embed_cluster", action="store_true", help="run embed-cluster even for large matrices")
    return parser


if __name__ == "__main__":
    run_stages("scatac_peak_matrix", {
        "load": load,
        "profile": profile,
        "filter": filter_cells,
        "standardize": standardize,
        "embed-cluster": embed_cluster,
        "finalize": finalize,
    }, _parser)
