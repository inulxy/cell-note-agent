#!/usr/bin/env python
"""multiome-qc: paired RNA+ATAC QC with ATAC peak-matrix deliverable.

Integrates teammate barcode/QC orchestration while keeping RNA as supporting metadata only.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone

from _common import (
    log_provenance,
    require_files,
    run_stages,
    software_versions,
    stage_subdir,
)


def _optional_import(name: str):
    try:
        return __import__(name)
    except ImportError as error:
        raise SystemExit(f"[error] optional dependency '{name}' is required for this stage: {error}") from error


def _out_dir(args) -> str:
    return stage_subdir(args.results_root, "processed", args.dataset_id)


def _rna_path(args) -> str:
    return os.path.join(_out_dir(args), "rna_support.h5ad")


def _atac_path(args) -> str:
    return os.path.join(_out_dir(args), "atac_qc.h5ad")


def _peak_matrix_path(args) -> str:
    return os.path.join(_out_dir(args), "peak_matrix.h5ad")


def _peaks_path(args) -> str:
    return os.path.join(_out_dir(args), "peaks.hg38.bed")


def _barcodes_path(args) -> str:
    return os.path.join(_out_dir(args), "barcodes.tsv.gz")


def _mudata_path(args) -> str:
    return os.path.join(_out_dir(args), "multiome.h5mu")


def _summary_path(args) -> str:
    return os.path.join(_out_dir(args), "qc_summary.json")


def _load_summary(args) -> dict:
    path = _summary_path(args)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def _save_summary(args, summary: dict) -> None:
    os.makedirs(os.path.dirname(_summary_path(args)), exist_ok=True)
    with open(_summary_path(args), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)


def _backed_obs_has(data, key: str) -> bool:
    """Column-existence probe that works for backed snapatac2 AnnData.

    Backed ``.obs`` is a PyDataFrameElem without ``.columns`` or
    ``__contains__``; the only reliable probe is attempting the read.
    """
    try:
        return key in data.obs.columns
    except AttributeError:
        pass
    try:
        data.obs[key]
        return True
    except Exception:
        return False


def _normalize_barcode(barcode: str) -> str:
    for suffix in ("-1", "-2", "-3", "-4", "-5"):
        if barcode.endswith(suffix) and len(barcode) > len(suffix):
            return barcode[: -len(suffix)]
    return barcode


def _normalize_barcode_map(names) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Map normalized -> original barcode, separating collision groups.

    Originals that collapse to the same normalized key (e.g. AAA-1 and AAA-2)
    cannot be paired unambiguously; silently overwriting dict entries would
    mispair cells, so collisions are excluded from pairing and reported.
    """
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(_normalize_barcode(str(name)), []).append(str(name))
    mapping = {key: members[0] for key, members in groups.items() if len(members) == 1}
    collisions = {key: members for key, members in groups.items() if len(members) > 1}
    return mapping, collisions


def _collision_stats(collisions: dict[str, list[str]]) -> dict:
    return {
        "groups": len(collisions),
        "barcodes": sum(len(members) for members in collisions.values()),
    }


def _parse_peak_name(name: str) -> tuple[str, int, int] | None:
    if ":" not in name or "-" not in name:
        return None
    try:
        chrom, rest = name.split(":", 1)
        start, end = rest.replace(",", "").split("-", 1)
        return chrom, int(start), int(end)
    except ValueError:
        return None


def _write_peaks_from_var_names(args, data) -> str | None:
    rows = []
    for name in [str(x) for x in data.var_names]:
        parsed = _parse_peak_name(name)
        if parsed is None:
            return None
        chrom, start, end = parsed
        rows.append(f"{chrom}\t{start}\t{end}")
    if not rows:
        return None
    with open(_peaks_path(args), "w", encoding="utf-8") as handle:
        handle.write("\n".join(rows) + "\n")
    return _peaks_path(args)


def _peaks_column(merged) -> list[str]:
    """Extract the ``Peaks`` column ("chr:start-end") from ``merge_peaks`` output."""
    try:
        column = merged["Peaks"]
    except (KeyError, TypeError, IndexError):
        return []
    if hasattr(column, "to_list"):
        column = column.to_list()
    return [str(item) for item in column]


def _write_bed_rows(path: str, names: list[str]) -> int:
    rows = []
    for name in names:
        parsed = _parse_peak_name(name)
        if parsed is None:
            return 0
        chrom, start, end = parsed
        rows.append(f"{chrom}\t{start}\t{end}")
    if not rows:
        return 0
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(rows) + "\n")
    return len(rows)


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


def _write_barcodes(args, barcodes) -> str:
    with gzip.open(_barcodes_path(args), "wt", encoding="utf-8") as handle:
        handle.write("\n".join(str(x) for x in barcodes).rstrip() + "\n")
    return _barcodes_path(args)


def _pick_genome(genome_build: str):
    snap = _optional_import("snapatac2")
    mapping = {
        "GRCh38": snap.genome.hg38,
        "hg38": snap.genome.hg38,
        "GRCh37": snap.genome.hg19,
        "hg19": snap.genome.hg19,
        "GRCm38": snap.genome.mm10,
        "mm10": snap.genome.mm10,
        "GRCm39": snap.genome.mm39,
        "mm39": snap.genome.mm39,
    }
    if genome_build not in mapping:
        raise SystemExit(f"[error] unsupported genome_build '{genome_build}'")
    return mapping[genome_build]


def _read_rna(path: str):
    sc = _optional_import("scanpy")
    if os.path.isdir(path):
        return sc.read_10x_mtx(path, var_names="gene_symbols", cache=True)
    if path.endswith(".h5"):
        return sc.read_10x_h5(path)
    if path.endswith(".h5ad"):
        return sc.read(path)
    raise ValueError(f"Cannot determine RNA input format: {path}")


def _subset_atac_to_peak_features(atac_adata):
    """Keep only Peaks features when a combined 10x ARC matrix is provided.

    ``read_10x_h5(..., gex_only=False)`` on a multiome h5 returns Gene
    Expression and Peaks features together; delivering that mix as an "ATAC
    peak matrix" would leak genes into the FM handoff.
    """
    var = getattr(atac_adata, "var", None)
    if var is None or "feature_types" not in getattr(var, "columns", []):
        return atac_adata, 0
    types_series = var["feature_types"].astype(str)
    unique = set(types_series)
    if unique == {"Peaks"}:
        return atac_adata, 0
    if "Peaks" not in unique:
        raise SystemExit(
            "[pair-check] --atac_matrix contains no 'Peaks' features (found: "
            + ", ".join(sorted(unique))
            + "); pass the ATAC peak matrix, not a GEX matrix"
        )
    mask = (types_series == "Peaks").to_numpy()
    n_removed = int((~mask).sum())
    subset = atac_adata[:, mask].copy()
    print(
        f"[pair-check] dropped {n_removed} non-Peaks feature(s) from --atac_matrix; "
        f"kept {int(mask.sum())} Peaks"
    )
    return subset, n_removed


def pair_check(args):
    require_files(args.rna)
    if args.atac_fragments:
        require_files(args.atac_fragments)
    elif args.atac_matrix:
        require_files(args.atac_matrix)
    else:
        raise SystemExit("[error] multiome requires --atac_fragments or --atac_matrix")

    rna = _read_rna(args.rna)
    rna.var_names_make_unique()
    print(f"[pair-check] RNA: {rna.n_obs} cells x {rna.n_vars} genes")

    if args.atac_fragments:
        # Only the fragments branch needs snapatac2; the peak-matrix branch
        # must stay runnable in the documented muon env (scanpy only).
        snap = _optional_import("snapatac2")
        atac = snap.pp.import_fragments(
            args.atac_fragments,
            chrom_sizes=_pick_genome(args.genome_build),
            file=_atac_path(args),
            sorted_by_barcode=False,
            min_num_fragments=args.import_min_fragments,
        )
        atac_obs_names = list(atac.obs_names)
        n_atac_vars = int(getattr(atac, "n_vars", 0))
        atac.close()
        atac_input_type = "fragments"
        n_nonpeak_dropped = 0
    else:
        sc = _optional_import("scanpy")
        atac_adata = sc.read(args.atac_matrix) if args.atac_matrix.endswith(".h5ad") else sc.read_10x_h5(args.atac_matrix, gex_only=False)
        atac_adata, n_nonpeak_dropped = _subset_atac_to_peak_features(atac_adata)
        atac_obs_names = list(atac_adata.obs_names)
        n_atac_vars = int(atac_adata.n_vars)
        atac_adata.write(_atac_path(args))
        if args.peaks:
            require_files(args.peaks)
            _copy_clean_bed(args.peaks, _peaks_path(args))
        elif _write_peaks_from_var_names(args, atac_adata) is None:
            print("[pair-check] warning: peak coordinates could not be inferred; provide --peaks before handoff")
        atac_input_type = "peak_matrix"

    rna_norm, rna_collisions = _normalize_barcode_map(rna.obs_names)
    atac_norm, atac_collisions = _normalize_barcode_map(atac_obs_names)
    if rna_collisions or atac_collisions:
        print(
            f"[pair-check] WARNING: barcode collisions after suffix "
            f"normalization (RNA {len(rna_collisions)} group(s), ATAC "
            f"{len(atac_collisions)} group(s)); collided barcodes are "
            f"excluded from pairing"
        )
    shared = set(rna_norm) & set(atac_norm)
    frac_rna = len(shared) / max(len(rna_norm), 1)
    frac_atac = len(shared) / max(len(atac_norm), 1)
    print(f"[pair-check] shared={len(shared)} ({frac_rna:.1%} RNA, {frac_atac:.1%} ATAC)")
    if min(frac_rna, frac_atac) < args.min_pair_overlap:
        print(f"[pair-check] WARNING: overlap below {args.min_pair_overlap}")

    rna.write(_rna_path(args))
    summary = {
        "dataset_id": args.dataset_id,
        "genome_build": args.genome_build,
        "target_genome_build": "GRCh38",
        "rna_file": args.rna,
        "atac_file": args.atac_fragments or args.atac_matrix,
        "atac_input_type": atac_input_type,
        "n_rna_cells": int(rna.n_obs),
        "n_rna_genes": int(rna.n_vars),
        "n_atac_cells": len(atac_obs_names),
        "n_atac_features": n_atac_vars,
        "n_atac_nonpeak_features_dropped": n_nonpeak_dropped,
        "n_shared_barcodes": len(shared),
        "frac_rna_shared": round(frac_rna, 4),
        "frac_atac_shared": round(frac_atac, 4),
        "barcode_collisions": {
            "rna": _collision_stats(rna_collisions),
            "atac": _collision_stats(atac_collisions),
        },
        "software_versions": software_versions(
            "scanpy", *(("snapatac2", "macs3") if args.atac_fragments else ())
        ),
        "stages_completed": ["pair-check"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_summary(args, summary)
    log_provenance(args.results_root, {"event": "multiome_pair_check", "dataset_id": args.dataset_id, "n_shared": len(shared)})


def qc_rna(args):
    sc = _optional_import("scanpy")
    require_files(_rna_path(args))
    adata = sc.read(_rna_path(args))
    adata.var["mt"] = adata.var_names.str.startswith(("MT-", "mt-"))
    adata.var["ribo"] = adata.var_names.str.startswith(("RPS", "RPL", "rps", "rpl"))
    sc.pp.calculate_qc_metrics(adata, qc_vars=["mt", "ribo"], percent_top=None, log1p=False, inplace=True)
    n_before = int(adata.n_obs)
    sc.pp.filter_cells(adata, min_genes=args.rna_min_genes)
    if "pct_counts_mt" in adata.obs:
        adata = adata[adata.obs["pct_counts_mt"] < args.rna_max_mito_pct, :].copy()
    adata.obs["rna_pass"] = True
    adata.write(_rna_path(args))
    summary = _load_summary(args)
    summary.update({"n_rna_before_qc": n_before, "n_rna_pass": int(adata.n_obs), "rna_is_supporting_only": True, "updated_at": datetime.now(timezone.utc).isoformat()})
    summary.setdefault("stages_completed", []).append("qc-rna")
    _save_summary(args, summary)
    print(f"[qc-rna] cells {n_before}->{adata.n_obs}")


def _per_cell_matrix_metrics(X) -> tuple[list[float], list[int]]:
    """Per-cell total counts and detected-feature counts for a cell x peak X.

    Works for scipy sparse (sum/getnnz), dense arrays, and simple test fakes.
    """
    totals = X.sum(axis=1)
    if hasattr(totals, "A1"):
        totals = totals.A1
    elif hasattr(totals, "ravel"):
        totals = totals.ravel()
    totals = [float(v) for v in totals]
    if hasattr(X, "getnnz"):
        nnz = X.getnnz(axis=1)
    else:
        nz = (X > 0).sum(axis=1)
        if hasattr(nz, "A1"):
            nz = nz.A1
        elif hasattr(nz, "ravel"):
            nz = nz.ravel()
        nnz = nz
    return totals, [int(v) for v in nnz]


def qc_atac(args):
    require_files(_atac_path(args))
    summary = _load_summary(args)
    if summary.get("atac_input_type") == "peak_matrix":
        sc = _optional_import("scanpy")
        data = sc.read(_atac_path(args))
        n_before = int(data.n_obs)
        # Real matrix-level gates (same semantics as scatac_peak_matrix.py's
        # min_counts/min_peaks). Cells are marked, not dropped: intersect
        # combines atac_pass with rna_pass into the paired-pass set.
        totals, nnzs = _per_cell_matrix_metrics(data.X)
        passing = [
            total >= args.atac_min_counts and nnz >= args.atac_min_peaks
            for total, nnz in zip(totals, nnzs)
        ]
        n_pass = sum(passing)
        if n_pass == 0:
            raise SystemExit(
                f"[qc-atac] no cells pass atac_min_counts={args.atac_min_counts} / "
                f"atac_min_peaks={args.atac_min_peaks}; relax the thresholds"
            )
        data.obs["atac_pass"] = passing
        data.write(_atac_path(args))
        summary["atac_matrix_thresholds"] = {
            "atac_min_counts": args.atac_min_counts,
            "atac_min_peaks": args.atac_min_peaks,
        }
        n_after_filter = n_pass
        n_after = n_pass
    else:
        snap = _optional_import("snapatac2")
        data = snap.read(_atac_path(args))
        if not _backed_obs_has(data, "tsse"):
            # min_tsse is a hard gate below: if TSSe cannot be computed the
            # stage must fail here, not crash later inside filter_cells.
            try:
                snap.metrics.tsse(data, _pick_genome(args.genome_build))
            except Exception as error:
                data.close()
                raise SystemExit(
                    f"[qc-atac] TSS enrichment computation failed (required for "
                    f"the min_tsse gate): {error}"
                ) from error
        n_before = int(data.n_obs)
        snap.pp.filter_cells(data, min_counts=args.min_fragments, min_tsse=args.min_tsse, max_counts=args.max_fragments, inplace=True)
        n_after_filter = int(data.n_obs)
        try:
            snap.pp.add_tile_matrix(data, bin_size=args.tile_size)
        except TypeError:
            snap.pp.add_tile_matrix(data)
        snap.pp.select_features(data, n_features=args.n_features)
        snap.tl.spectral(data, n_comps=args.n_comps)
        snap.tl.umap(data)
        snap.pp.knn(data)
        snap.tl.leiden(data, resolution=args.leiden_res)
        try:
            snap.pp.scrublet(data, expected_doublet_rate=args.expected_doublet_rate)
            snap.pp.filter_doublets(data, inplace=True)
        except Exception as error:
            print(f"[qc-atac] warning: doublet stage skipped: {error}")
        n_after = int(data.n_obs)
        try:
            data.obs["atac_pass"] = True
        except Exception:
            pass
        data.close()
    summary.update({"n_atac_before_qc": n_before, "n_atac_after_filter": n_after_filter, "n_atac_pass": n_after, "updated_at": datetime.now(timezone.utc).isoformat()})
    summary.setdefault("stages_completed", []).append("qc-atac")
    _save_summary(args, summary)
    print(f"[qc-atac] cells {n_before}->{n_after}")

def _materialize_fragments_deliverable(args, atac, paired_atac_barcodes, summary) -> None:
    """Fragments branch: paired-pass subset -> peaks -> matrix -> barcodes.

    This closes the historical gap where the fragments branch stopped after
    pair counting and finalize had to fail. Every step either completes or
    exits loudly; no partial deliverables are written into the summary.

    The subset is done on an in-memory copy: anndata-rs' backed ``subset``
    corrupts this working file on real 10x ARC data (DataFrame height panics,
    even with ``out=``), and a failed in-place subset leaves the h5ad
    half-written. The backed handle is closed untouched instead.
    """
    snap = _optional_import("snapatac2")
    if not paired_atac_barcodes:
        atac.close()
        raise SystemExit(
            "[intersect] no paired-pass cells; cannot deliver an ATAC peak matrix"
        )
    atac.close()
    # snapatac2 writes zstd-compressed h5ad; reading it back through
    # h5py/anndata needs the HDF5 plugin registered by hdf5plugin.
    _optional_import("hdf5plugin")
    mem = snap.read(_atac_path(args), backed=None)
    keep = set(paired_atac_barcodes)
    ordered = [name for name in (str(x) for x in mem.obs_names) if name in keep]
    sub = mem[ordered].copy()
    del mem
    print(f"[intersect] paired-pass subset: {sub.n_obs} cells (in-memory)")

    if args.peaks:
        require_files(args.peaks)
        n_peaks = _copy_clean_bed(args.peaks, _peaks_path(args))
    else:
        # Dataset-level MACS3 on the paired-pass pseudobulk, mirroring the
        # fragment pipeline's dataset mode (uns['macs3_pseudobulk'] only;
        # never trust stale tables from other runs).
        try:
            snap.tl.macs3(sub)
        except Exception as error:
            raise SystemExit(
                f"[intersect] MACS3 failed on paired-pass cells: {error}"
            ) from error
        raw = sub.uns.get("macs3_pseudobulk")
        if raw is None or (isinstance(raw, dict) and not raw):
            raise SystemExit(
                "[intersect] MACS3 finished but uns['macs3_pseudobulk'] is "
                "missing or empty; cannot export peaks"
            )
        tables = dict(raw) if isinstance(raw, dict) else {"all": raw}
        try:
            merged = snap.tl.merge_peaks(tables, _pick_genome(args.genome_build))
        except Exception as error:
            raise SystemExit(f"[intersect] merge_peaks failed: {error}") from error
        n_peaks = _write_bed_rows(_peaks_path(args), _peaks_column(merged))
        if not n_peaks:
            raise SystemExit(
                "[intersect] merged MACS3 peaks could not be exported to BED"
            )
    print(f"[intersect] peaks ready: {_peaks_path(args)} ({n_peaks} intervals)")

    try:
        peak_data = snap.pp.make_peak_matrix(sub, peak_file=_peaks_path(args))
    except Exception as error:
        raise SystemExit(f"[intersect] make_peak_matrix failed: {error}") from error
    peak_data.write(_peak_matrix_path(args))
    try:
        peak_data.close()
    except Exception:
        pass
    barcodes = _write_barcodes(args, [str(x) for x in sub.obs_names])
    summary.update({
        "peak_matrix": _peak_matrix_path(args),
        "peaks_file": _peaks_path(args),
        "barcodes_file": barcodes,
        "n_peaks_called": n_peaks,
        "peak_calling": "provided" if args.peaks else "dataset",
        "representation_quality": "multiome_fragments_recomputed",
    })
    print(f"[intersect] materialized ATAC deliverable for {sub.n_obs} paired-pass cells")


def intersect(args):
    sc = _optional_import("scanpy")
    require_files(_rna_path(args), _atac_path(args))
    summary = _load_summary(args)
    rna = sc.read(_rna_path(args))
    if summary.get("atac_input_type") == "peak_matrix":
        atac = sc.read(_atac_path(args))
        close_atac = False
    else:
        snap = _optional_import("snapatac2")
        atac = snap.read(_atac_path(args))
        close_atac = True
    rna_norm, _ = _normalize_barcode_map(rna.obs_names)
    atac_norm, _ = _normalize_barcode_map(atac.obs_names)
    shared = sorted(set(rna_norm) & set(atac_norm))
    rna_shared = [rna_norm[bc] for bc in shared]
    atac_shared = [atac_norm[bc] for bc in shared]
    rna_pass = set(rna_shared)
    if "rna_pass" in rna.obs:
        rna_pass = set(rna[rna.obs["rna_pass"]].obs_names) & set(rna_shared)
    atac_pass = set(atac_shared)
    if "atac_pass" in atac.obs:
        atac_pass = set(atac[atac.obs["atac_pass"]].obs_names) & set(atac_shared)
    rna_pass_norm = {_normalize_barcode(str(bc)) for bc in rna_pass}
    atac_pass_norm = {_normalize_barcode(str(bc)) for bc in atac_pass}
    paired_norm = [bc for bc in shared if bc in rna_pass_norm and bc in atac_pass_norm]
    paired_atac_barcodes = [atac_norm[bc] for bc in paired_norm]
    if summary.get("atac_input_type") == "peak_matrix":
        atac_subset = atac[paired_atac_barcodes, :].copy()
        atac_subset.write(_peak_matrix_path(args))
        barcodes = _write_barcodes(args, paired_atac_barcodes)
        summary.update({
            "peak_matrix": _peak_matrix_path(args),
            "peaks_file": _peaks_path(args) if os.path.exists(_peaks_path(args)) else None,
            "barcodes_file": barcodes,
            "representation_quality": "multiome_peak_matrix",
        })
    else:
        _materialize_fragments_deliverable(args, atac, paired_atac_barcodes, summary)
    summary.update({"n_shared": len(shared), "n_rna_pass_shared": len(rna_pass), "n_atac_pass_shared": len(atac_pass), "n_paired_pass": len(paired_norm), "updated_at": datetime.now(timezone.utc).isoformat()})
    summary.setdefault("stages_completed", []).append("intersect")
    _save_summary(args, summary)
    if close_atac:
        try:
            atac.close()
        except Exception:
            pass
    print(f"[intersect] paired-pass={len(paired_norm)}")

def finalize(args):
    out_dir = _out_dir(args)
    summary = _load_summary(args)
    matrix = summary.get("peak_matrix") or (
        _peak_matrix_path(args) if os.path.exists(_peak_matrix_path(args)) else None
    )
    peaks = summary.get("peaks_file") or (
        _peaks_path(args) if os.path.exists(_peaks_path(args)) else None
    )
    barcodes = summary.get("barcodes_file") or (
        _barcodes_path(args) if os.path.exists(_barcodes_path(args)) else None
    )
    required = {
        "peak_matrix": matrix,
        "peaks": peaks,
        "barcodes": barcodes,
    }
    missing = [
        name
        for name, path in required.items()
        if not path or not os.path.isfile(str(path))
    ]
    if missing:
        raise SystemExit(
            "[finalize] missing required ATAC deliverable(s): "
            + ", ".join(missing)
            + ". For fragment input, complete peak calling and peak-matrix materialization first."
        )
    if summary.get("genome_build", args.genome_build) not in {"GRCh38", "hg38"}:
        raise SystemExit("[finalize] final multiome ATAC peak matrix must be GRCh38/hg38")

    summary["deliverable"] = "atac_grch38_peak_matrix_for_paired_cells"
    summary["rna_is_supporting_only"] = True
    summary["peak_matrix"] = str(matrix)
    summary["peaks_file"] = str(peaks)
    summary["barcodes_file"] = str(barcodes)
    summary.setdefault("stages_completed", []).append("finalize")
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args, summary)
    with open(os.path.join(out_dir, "multiome_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    card = {
        "dataset_id": args.dataset_id,
        "deliverable": summary["deliverable"],
        "genome_build": summary.get("genome_build", args.genome_build),
        "files": {
            "peak_matrix": summary.get("peak_matrix"),
            "peaks": summary.get("peaks_file"),
            "barcodes": summary.get("barcodes_file"),
            "qc_summary": _summary_path(args),
        },
        "qc_summary": summary,
    }
    with open(os.path.join(out_dir, "data_card.json"), "w", encoding="utf-8") as handle:
        json.dump(card, handle, indent=2, ensure_ascii=False)
    log_provenance(args.results_root, {"event": "multiome_finalize", "dataset_id": args.dataset_id, "n_paired_pass": summary.get("n_paired_pass", 0)})
    print(f"[finalize] multiome package summary ready: {out_dir}")


def _parser(parser):
    parser.add_argument("--rna", required=True, help="cell x gene matrix for paired QC support")
    parser.add_argument("--atac_fragments", help="ATAC fragments.tsv.gz")
    parser.add_argument("--atac_matrix", help="fallback ATAC cell x peak matrix")
    parser.add_argument("--peaks", help="peaks.bed/features.tsv for --atac_matrix")
    parser.add_argument("--genome_build", default="GRCh38")
    parser.add_argument("--results_root", required=True)
    parser.add_argument("--dataset_id", required=True)
    parser.add_argument("--min_pair_overlap", type=float, default=0.5)
    parser.add_argument("--import_min_fragments", type=int, default=200)
    parser.add_argument("--min_fragments", type=int, default=1000)
    parser.add_argument("--max_fragments", type=int, default=100000)
    parser.add_argument("--min_tsse", type=float, default=4.0)
    parser.add_argument("--tile_size", type=int, default=500)
    parser.add_argument("--n_features", type=int, default=250000)
    parser.add_argument("--n_comps", type=int, default=30)
    parser.add_argument("--leiden_res", type=float, default=1.0)
    parser.add_argument("--expected_doublet_rate", type=float, default=0.08)
    parser.add_argument("--rna_min_genes", type=int, default=200)
    parser.add_argument("--rna_max_mito_pct", type=float, default=20.0)
    # peak-matrix input gates (qc-atac); same semantics/defaults as
    # scatac_peak_matrix.py --min_counts / --min_peaks. Set to 0 to disable.
    parser.add_argument("--atac_min_counts", type=int, default=1000)
    parser.add_argument("--atac_min_peaks", type=int, default=500)
    return parser


if __name__ == "__main__":
    run_stages("multiome_qc", {"pair-check": pair_check, "qc-rna": qc_rna, "qc-atac": qc_atac, "intersect": intersect, "finalize": finalize}, _parser)
