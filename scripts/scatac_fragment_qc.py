#!/usr/bin/env python
"""scatac-fragment-qc: staged SnapATAC2 QC for fragment-based scATAC.

Integrates teammate SnapATAC2 implementation while preserving the current deliverable:
GRCh38 per-dataset cell-by-peak matrix plus QC provenance.
"""
from __future__ import annotations

import gzip
import csv
import json
import os
import re
import shutil
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from _common import (
    file_sha256,
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


def _snap():
    return _optional_import("snapatac2")


def _out_dir(args) -> str:
    return stage_subdir(args.results_root, "processed", args.dataset_id)


def _safe_sample_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-") or "sample"


def _sample_id_from_fragment(path: Path) -> str:
    name = path.name
    for suffix in (".tsv.gz", ".bed.gz", ".tsv", ".bed"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return _safe_sample_id(name.removesuffix("_fragments").removesuffix("-fragments"))


def _metadata_for_fragment(path: Path) -> Path | None:
    name = path.name
    for suffix in (".tsv.gz", ".bed.gz", ".tsv", ".bed"):
        if name.lower().endswith(suffix):
            candidate = path.with_name(name[: -len(suffix)] + "-metadata.csv")
            return candidate if candidate.exists() else None
    return None


def _fragment_entries(args) -> list[dict[str, str]]:
    raw = Path(str(args.fragments or "")).expanduser()
    if not raw.exists():
        raise SystemExit(f"[error] missing fragments input: {raw}")
    entries: list[dict[str, str]] = []
    if raw.is_dir():
        files = sorted(
            path for path in raw.rglob("*")
            if path.is_file()
            and path.name.lower().endswith((".tsv.gz", ".bed.gz", ".tsv", ".bed"))
            and "metadata" not in path.name.lower()
            and "peak" not in path.name.lower()
        )
        for path in files:
            metadata = _metadata_for_fragment(path)
            entries.append({"sample_id": _sample_id_from_fragment(path), "fragments_path": str(path.resolve()), "metadata_path": str(metadata.resolve()) if metadata else ""})
    elif raw.suffix.lower() == ".csv":
        with raw.open(newline="", encoding="utf-8") as handle:
            for index, row in enumerate(csv.DictReader(handle), 1):
                value = row.get("fragments_path") or row.get("local_path") or ""
                path = Path(value).expanduser()
                role = str(row.get("role") or "").lower()
                if not value or (role and "fragment" not in role):
                    continue
                if not path.is_absolute():
                    path = (raw.parent / path).resolve()
                metadata_value = row.get("metadata_path") or ""
                metadata = Path(metadata_value).expanduser() if metadata_value else _metadata_for_fragment(path)
                if metadata and not metadata.is_absolute():
                    metadata = (raw.parent / metadata).resolve()
                entries.append({
                    "sample_id": _safe_sample_id(str(row.get("sample_id") or _sample_id_from_fragment(path) or index)),
                    "fragments_path": str(path),
                    "metadata_path": str(metadata) if metadata and metadata.exists() else "",
                })
    else:
        metadata = _metadata_for_fragment(raw)
        entries.append({"sample_id": _sample_id_from_fragment(raw), "fragments_path": str(raw.resolve()), "metadata_path": str(metadata.resolve()) if metadata else ""})
    if not entries:
        raise SystemExit(f"[error] no fragment files discovered under {raw}")
    seen: set[str] = set()
    for entry in entries:
        path = entry["fragments_path"]
        require_files(path)
        sample_id = entry["sample_id"]
        if sample_id in seen:
            raise SystemExit(f"[error] duplicate sample_id in fragment collection: {sample_id}")
        seen.add(sample_id)
    return entries


def _is_collection(args) -> bool:
    fragments = getattr(args, "fragments", None)
    if not fragments:
        return os.path.exists(os.path.join(_out_dir(args), "atac_qc.h5ads"))
    return len(_fragment_entries(args)) > 1


def _h5ad_path(args) -> str:
    return os.path.join(_out_dir(args), "atac_qc.h5ad")


def _sample_h5ad_dir(args) -> str:
    return os.path.join(_out_dir(args), "sample_h5ad")


def _merged_fragments_path(args) -> str:
    return os.path.join(_out_dir(args), "collection_input", "merged_fragments.tsv.gz")


def _merged_fragments_state_path(args) -> str:
    return os.path.join(_out_dir(args), "collection_input", "merge_state.json")


def _sample_manifest_path(args) -> str:
    return os.path.join(_out_dir(args), "fragment_sample_manifest.csv")


def _peak_matrix_path(args) -> str:
    return os.path.join(_out_dir(args), "peak_matrix.h5ad")


def _peaks_path(args) -> str:
    return os.path.join(_out_dir(args), "peaks.hg38.bed")


def _barcodes_path(args) -> str:
    return os.path.join(_out_dir(args), "barcodes.tsv.gz")


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


def _load_data(args):
    path = _h5ad_path(args)
    require_files(path)
    snap = _snap()
    if path.endswith(".h5ads"):
        data = snap.read_dataset(path)
        _sync_collection_obs(data)
        return data
    return snap.read(path)


def _pick_genome(genome_build: str):
    snap = _snap()
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
    genome = mapping.get(genome_build)
    if genome is None:
        raise SystemExit(f"[error] unsupported genome_build '{genome_build}'")
    return genome


def _obs_has(data, key: str) -> bool:
    obs = getattr(data, "obs", None)
    if obs is None:
        return False
    try:
        obs[key]
        return True
    except Exception:
        return False


def _sync_collection_obs(data) -> list[str]:
    """Expose per-sample import QC columns on the AnnDataSet outer obs.

    SnapATAC2 stores ``n_fragment``/duplication/mitochondrial metrics on the
    stacked child AnnData objects, while dataset-level TSSe is written to the
    AnnDataSet itself. Several preprocessing functions read only the outer
    ``obs``, so collection mode must synchronize these factual columns before
    filtering. Assignment persists in the backed ``.h5ads`` and is idempotent.
    """
    stacked = getattr(data, "adatas", None)
    if stacked is None or getattr(stacked, "obs", None) is None:
        return []
    synced: list[str] = []
    for key in ("n_fragment", "frac_dup", "frac_mito"):
        if _obs_has(data, key):
            continue
        try:
            values = stacked.obs[key]
            if len(values) != int(data.n_obs):
                continue
            data.obs[key] = values
            synced.append(key)
        except Exception:
            continue
    if synced:
        print(f"[collection] synchronized outer obs columns: {', '.join(synced)}")
    return synced


def _obs_values(data, key: str):
    values = data.obs[key]
    if hasattr(values, "to_numpy"):
        return values.to_numpy()
    return values


def _median(values) -> float:
    import numpy as np
    return float(np.median(values))


def _save_matplotlib_plot(fig, out_dir: str, name: str) -> str:
    import matplotlib.pyplot as plt
    path = os.path.join(out_dir, f"{name}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [plot] {path}")
    return path


def _write_barcodes(args, data) -> str:
    path = _barcodes_path(args)
    names = [str(x) for x in data.obs_names]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(names).rstrip() + "\n")
    return path


def _parse_peak_name(name: str) -> tuple[str, int, int] | None:
    if ":" not in name or "-" not in name:
        return None
    try:
        chrom, rest = name.split(":", 1)
        start, end = rest.replace(",", "").split("-", 1)
        return chrom, int(start), int(end)
    except ValueError:
        return None


def _macs3_uns_key(grouped: bool) -> str:
    return "macs3" if grouped else "macs3_pseudobulk"


def _macs3_peak_tables(data, *, grouped: bool) -> dict | None:
    """Collect the MACS3 peak tables written by the run that just finished.

    SnapATAC2 stores grouped results in ``uns['macs3']`` and bulk (no groupby)
    results in ``uns['macs3_pseudobulk']``. The working h5ad keeps ``.uns``
    across runs, so only the key matching the mode just executed may be read:
    falling back to the other key would silently ship stale peaks from a
    previous run with a different ``--peak_calling`` mode. Peaks are never
    read from ``var_names``: those are the tile bins created by
    ``add_tile_matrix`` and must not be exported as peaks.
    """
    uns = getattr(data, "uns", None)
    if uns is None:
        return None
    try:
        value = uns[_macs3_uns_key(grouped)]
    except (KeyError, TypeError):
        return None
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value) or None
    return {"all": value}


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
    """Copy a BED-like peaks file while dropping metadata/header lines."""
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


def _validate_fragment_schema(path: str) -> None:
    opener = gzip.open if path.lower().endswith(".gz") else open
    checked = 0
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip() or line.startswith("#"):
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 5:
                    raise ValueError("expected at least five tab-separated columns")
                start, end, count = int(fields[1]), int(fields[2]), int(fields[4])
                if not fields[0] or not fields[3] or start < 0 or end <= start or count < 0:
                    raise ValueError("invalid chromosome/start/end/barcode/count values")
                checked += 1
                if checked >= 5:
                    break
    except (OSError, UnicodeError, ValueError) as error:
        raise SystemExit(f"[import] invalid fragment schema in {path}: {error}") from error
    if checked == 0:
        raise SystemExit(f"[import] empty fragment file: {path}")


def _sample_counts(data) -> dict[str, int]:
    if not _obs_has(data, "sample"):
        return {}
    counts: dict[str, int] = {}
    for value in _obs_values(data, "sample"):
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _write_sample_manifest(args, entries: list[dict[str, str]], counts: dict[str, int]) -> str:
    path = _sample_manifest_path(args)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sample_id", "fragments_path", "metadata_path", "file_size_bytes", "n_cells_imported"])
        writer.writeheader()
        for entry in entries:
            writer.writerow({
                **entry,
                "file_size_bytes": os.path.getsize(entry["fragments_path"]),
                "n_cells_imported": counts.get(entry["sample_id"], ""),
            })
    return path


def _collection_signature(entries: list[dict[str, str]]) -> list[dict[str, int | str]]:
    return [
        {
            "sample_id": entry["sample_id"],
            "fragments_path": entry["fragments_path"],
            "size_bytes": os.path.getsize(entry["fragments_path"]),
            "mtime_ns": os.stat(entry["fragments_path"]).st_mtime_ns,
        }
        for entry in entries
    ]


def _barcodes_have_sample_prefix(entry: dict[str, str], max_records: int = 100) -> bool:
    path = entry["fragments_path"]
    sample_id = entry["sample_id"]
    opener = gzip.open if path.lower().endswith(".gz") else open
    checked = 0
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 5:
                return False
            barcode = fields[3]
            if not (barcode.startswith(sample_id + "-") or barcode.startswith(sample_id + "::")):
                return False
            checked += 1
            if checked >= max_records:
                break
    return checked > 0


def _prepare_collection_fragments(args, entries: list[dict[str, str]]) -> tuple[str, str]:
    """Create one resumable fragments stream for the mutable AnnData QC path.

    SnapATAC2 2.9 imports a list into ``AnnDataSet`` objects that cannot be
    subsetted. A standard concatenated gzip is lossless and lets all downstream
    stages use the mature single-AnnData implementation. When input barcodes do
    not already carry sample identity, the slower rewrite path prefixes them.
    """
    output = _merged_fragments_path(args)
    state_path = _merged_fragments_state_path(args)
    signature = _collection_signature(entries)
    if os.path.isfile(output) and os.path.getsize(output) > 0 and os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            if state.get("sources") == signature and state.get("complete") is True:
                print(f"[import] reusing merged collection input: {output}")
                return output, str(state.get("merge_mode") or "unknown")
        except (OSError, json.JSONDecodeError):
            pass

    os.makedirs(os.path.dirname(output), exist_ok=True)
    tmp = output + ".tmp"
    all_prefixed = all(_barcodes_have_sample_prefix(entry) for entry in entries)
    merge_mode = "concatenated_gzip" if all_prefixed and all(entry["fragments_path"].lower().endswith(".gz") for entry in entries) else "sample_prefix_rewrite"
    print(f"[import] preparing collection input ({merge_mode}) -> {output}")
    try:
        if merge_mode == "concatenated_gzip":
            with open(tmp, "wb") as target:
                for entry in entries:
                    with open(entry["fragments_path"], "rb") as source:
                        shutil.copyfileobj(source, target, length=8 << 20)
        else:
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as target:
                for entry in entries:
                    path = entry["fragments_path"]
                    opener = gzip.open if path.lower().endswith(".gz") else open
                    with opener(path, "rt", encoding="utf-8", errors="replace") as source:
                        for line in source:
                            if not line.strip() or line.startswith("#"):
                                continue
                            fields = line.rstrip("\n").split("\t")
                            barcode = fields[3]
                            if not (barcode.startswith(entry["sample_id"] + "-") or barcode.startswith(entry["sample_id"] + "::")):
                                fields[3] = entry["sample_id"] + "::" + barcode
                            target.write("\t".join(fields) + "\n")
        os.replace(tmp, output)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump({"complete": True, "merge_mode": merge_mode, "sources": signature}, handle, indent=2, ensure_ascii=False)
    return output, merge_mode


def _assign_collection_samples(data, entries: list[dict[str, str]]) -> dict[str, int]:
    sample_ids = sorted((entry["sample_id"] for entry in entries), key=len, reverse=True)
    labels: list[str] = []
    unmatched: list[str] = []
    for raw_name in data.obs_names:
        name = str(raw_name)
        sample_id = next(
            (item for item in sample_ids if name.startswith(item + "-") or name.startswith(item + "::")),
            "",
        )
        if not sample_id:
            unmatched.append(name)
            sample_id = "unknown"
        labels.append(sample_id)
    if unmatched:
        raise SystemExit(
            f"[import] could not recover sample identity for {len(unmatched)} barcode(s); "
            f"first unmatched barcode: {unmatched[0]}"
        )
    data.obs["sample"] = labels
    return _sample_counts(data)


def import_data(args):
    entries = _fragment_entries(args)
    for entry in entries:
        _validate_fragment_schema(entry["fragments_path"])
    snap = _snap()
    h5ad_path = _h5ad_path(args)
    if os.path.exists(h5ad_path) and os.path.getsize(h5ad_path) > 0 and not args.overwrite:
        print(f"[import] existing file found, skipping: {h5ad_path}")
        return
    genome = _pick_genome(args.genome_build)
    input_mode = "collection" if len(entries) > 1 else "single"
    print(f"[import] fragments input mode: {input_mode}; samples={len(entries)}")
    tempdir = os.environ.get("TMPDIR") or None
    merge_mode = None
    if input_mode == "single":
        data = snap.pp.import_fragments(
            entries[0]["fragments_path"],
            chrom_sizes=genome,
            file=h5ad_path,
            sorted_by_barcode=False,
            min_num_fragments=args.import_min_fragments,
            tempdir=tempdir,
            n_jobs=args.import_jobs,
        )
    else:
        merged_fragments, merge_mode = _prepare_collection_fragments(args, entries)
        data = snap.pp.import_fragments(
            merged_fragments,
            chrom_sizes=genome,
            file=h5ad_path,
            sorted_by_barcode=False,
            min_num_fragments=args.import_min_fragments,
            tempdir=tempdir,
            n_jobs=args.import_jobs,
        )
        _assign_collection_samples(data, entries)
    n_cells = int(data.n_obs)
    try:
        snap.metrics.tsse(data, genome)
    except Exception as error:
        print(f"[import] warning: TSS enrichment failed: {error}")
    sample_counts = _sample_counts(data)
    data.close()
    sample_manifest = _write_sample_manifest(args, entries, sample_counts)
    summary = {
        "dataset_id": args.dataset_id,
        "genome_build": args.genome_build,
        "target_genome_build": "GRCh38",
        "fragments_file": args.fragments if input_mode == "single" else None,
        "fragments_input": args.fragments,
        "input_mode": input_mode,
        "sample_count": len(entries),
        "sample_cell_counts_imported": sample_counts,
        "sample_manifest": sample_manifest,
        "collection_merge_mode": merge_mode,
        "merged_fragments_file": _merged_fragments_path(args) if input_mode == "collection" else None,
        "n_cells_imported": n_cells,
        "working_h5ad": h5ad_path,
        "software_versions": software_versions("snapatac2", "macs3"),
        "stages_completed": ["import"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_summary(args, summary)
    log_provenance(args.results_root, {"event": "scatac_import", "dataset_id": args.dataset_id, "n_cells": n_cells})
    print(f"[import] done: {n_cells} cells -> {h5ad_path}")


def pre_filter(args):
    snap = _snap()
    data = _load_data(args)
    out_dir = _out_dir(args)
    if not _obs_has(data, "tsse"):
        try:
            snap.metrics.tsse(data, _pick_genome(args.genome_build))
        except Exception as error:
            print(f"[pre-filter] warning: TSSe unavailable: {error}")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if _obs_has(data, "tsse"):
        tsse = _obs_values(data, "tsse")
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.hist(tsse, bins=100, edgecolor="none", alpha=0.8)
        ax.axvline(args.min_tsse, color="red", linestyle="--", label=f"min_tsse={args.min_tsse}")
        ax.set_xlabel("TSS enrichment")
        ax.legend()
        _save_matplotlib_plot(fig, out_dir, "tsse_hist")
    frag_key = "n_fragment" if _obs_has(data, "n_fragment") else "n_fragments" if _obs_has(data, "n_fragments") else None
    if frag_key:
        n_frag = _obs_values(data, frag_key)
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.hist(n_frag, bins=100, edgecolor="none", alpha=0.8)
        ax.axvline(args.min_fragments, color="red", linestyle="--", label=f"min_fragments={args.min_fragments}")
        ax.axvline(args.max_fragments, color="orange", linestyle="--", label=f"max_fragments={args.max_fragments}")
        ax.set_xlabel("Unique fragments")
        ax.legend()
        _save_matplotlib_plot(fig, out_dir, "fragment_hist")
    summary = _load_summary(args)
    summary["n_cells_pre_filter"] = int(data.n_obs)
    sample_counts = _sample_counts(data)
    if sample_counts:
        summary["sample_cell_counts_pre_filter"] = sample_counts
    summary.setdefault("stages_completed", []).append("pre-filter")
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args, summary)
    data.close()
    print(f"[pre-filter] plots and summary saved under {out_dir}")


def _region_fractions(snap, data, bed_path: str, obs_key: str, stage: str) -> list[float]:
    """Per-cell fraction of reads inside ``bed_path`` via snap.metrics.frip."""
    try:
        snap.metrics.frip(data, regions={obs_key: bed_path}, inplace=True)
    except Exception as error:
        data.close()
        raise SystemExit(
            f"[{stage}] fraction-of-reads computation failed for {bed_path}: {error}"
        ) from error
    return [float(v) for v in _obs_values(data, obs_key)]


def filter_cells(args):
    snap = _snap()
    data = _load_data(args)
    n_before = int(data.n_obs)
    kwargs = {"min_counts": args.min_fragments, "min_tsse": args.min_tsse, "inplace": True}
    if args.max_fragments:
        kwargs["max_counts"] = args.max_fragments
    snap.pp.filter_cells(data, **kwargs)

    applied = {
        "min_fragments": args.min_fragments,
        "max_fragments": args.max_fragments,
        "min_tsse": args.min_tsse,
    }
    declared_not_applied = {}
    n_removed_blacklist = 0
    blacklist_frac_median = None
    if args.blacklist_bed:
        require_files(args.blacklist_bed)
        fracs = _region_fractions(
            snap, data, args.blacklist_bed, "blacklist_frac", "filter"
        )
        keep = [i for i, frac in enumerate(fracs) if frac <= args.max_blacklist_frac]
        n_removed_blacklist = len(fracs) - len(keep)
        blacklist_frac_median = round(statistics.median(fracs), 4) if fracs else None
        if n_removed_blacklist:
            data.subset(keep)
        applied["max_blacklist_frac"] = args.max_blacklist_frac
    else:
        declared_not_applied["max_blacklist_frac"] = {
            "value": args.max_blacklist_frac,
            "reason": (
                "no --blacklist_bed provided; run resource-setup and pass "
                "reference/hg38-blacklist.v2.bed to enforce this gate"
            ),
        }
    # FRiP needs the peak universe, which does not exist until call-peaks.
    declared_not_applied["min_frip"] = {
        "value": args.min_frip,
        "reason": "deferred: enforced at make-peak-matrix once peaks exist",
    }

    n_after = int(data.n_obs)
    sample_counts = _sample_counts(data)
    data.close()
    summary = _load_summary(args)
    summary.update({
        "n_cells_pass_filter": n_after,
        "n_cells_removed_filter": n_before - n_after,
        "n_cells_removed_blacklist": n_removed_blacklist,
        "fraction_pass_filter": round(n_after / n_before if n_before else 0, 4),
        "filter_thresholds": applied,
        # Declared but not enforced in this run; kept out of filter_thresholds
        # so provenance never claims gates that did not run.
        "thresholds_declared_not_applied": declared_not_applied,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    if sample_counts:
        summary["sample_cell_counts_pass_filter"] = sample_counts
    if blacklist_frac_median is not None:
        summary["blacklist_frac_median"] = blacklist_frac_median
    if args.blacklist_bed:
        # Reference-asset provenance: which exact blacklist enforced the gate.
        summary["blacklist_bed"] = args.blacklist_bed
        summary["blacklist_bed_sha256"] = file_sha256(args.blacklist_bed)
    summary.setdefault("stages_completed", []).append("filter")
    _save_summary(args, summary)
    log_provenance(args.results_root, {"event": "scatac_filter", "dataset_id": args.dataset_id, "n_before": n_before, "n_after": n_after, "n_removed_blacklist": n_removed_blacklist})
    print(f"[filter] cells {n_before}->{n_after}"
          + (f" (blacklist gate removed {n_removed_blacklist})" if n_removed_blacklist else ""))


def embed(args):
    snap = _snap()
    data = _load_data(args)
    try:
        snap.pp.add_tile_matrix(data, bin_size=args.tile_size)
    except TypeError:
        snap.pp.add_tile_matrix(data)
    snap.pp.select_features(data, n_features=args.n_features)
    snap.tl.spectral(data, n_comps=args.n_comps)
    snap.tl.umap(data)
    n_selected = 0
    try:
        selected = data.var["selected"]
        n_selected = int(selected.sum()) if hasattr(selected, "sum") else int(sum(selected))
    except Exception:
        pass
    data.close()
    summary = _load_summary(args)
    summary.update({"n_features_selected": n_selected, "n_comps": args.n_comps, "tile_size": args.tile_size, "updated_at": datetime.now(timezone.utc).isoformat()})
    summary.setdefault("stages_completed", []).append("embed")
    _save_summary(args, summary)
    print(f"[embed] selected {n_selected} features")


def cluster(args):
    snap = _snap()
    data = _load_data(args)
    snap.pp.knn(data)
    snap.tl.leiden(data, resolution=args.leiden_res)
    n_clusters = 0
    try:
        leiden = data.obs["leiden"]
        n_clusters = int(leiden.n_unique()) if hasattr(leiden, "n_unique") else len(set(leiden))
    except Exception:
        pass
    try:
        fig = snap.pl.umap(data, color="leiden", show=False, height=500)
        if hasattr(fig, "write_image"):
            fig.write_image(os.path.join(_out_dir(args), "umap_clusters.png"))
    except Exception as error:
        print(f"[cluster] warning: UMAP plot failed: {error}")
    data.close()
    summary = _load_summary(args)
    summary.update({"n_clusters": n_clusters, "leiden_res": args.leiden_res, "updated_at": datetime.now(timezone.utc).isoformat()})
    summary.setdefault("stages_completed", []).append("cluster")
    _save_summary(args, summary)
    print(f"[cluster] {n_clusters} clusters")


def doublet(args):
    snap = _snap()
    data = _load_data(args)
    n_before = int(data.n_obs)
    snap.pp.scrublet(data, expected_doublet_rate=args.expected_doublet_rate)
    snap.pp.filter_doublets(data, inplace=True)
    n_after = int(data.n_obs)
    sample_counts = _sample_counts(data)
    data.close()
    summary = _load_summary(args)
    summary.update({"n_cells_after_doublet": n_after, "n_doublets_removed": n_before - n_after, "updated_at": datetime.now(timezone.utc).isoformat()})
    if sample_counts:
        summary["sample_cell_counts_after_doublet"] = sample_counts
    summary.setdefault("stages_completed", []).append("doublet")
    _save_summary(args, summary)
    print(f"[doublet] cells {n_before}->{n_after}")


def call_peaks(args):
    snap = _snap()
    data = _load_data(args)
    if args.peaks:
        require_files(args.peaks)
        n_peaks = _copy_clean_bed(args.peaks, _peaks_path(args))
        data.close()
        print(f"[call-peaks] using provided peaks: {_peaks_path(args)} ({n_peaks} intervals)")
    else:
        requested_mode = getattr(args, "peak_calling", "auto")
        effective_mode = (
            "sample" if requested_mode == "auto" and _is_collection(args)
            else "dataset" if requested_mode == "auto"
            else requested_mode
        )
        group_key = {"dataset": None, "sample": "sample", "cluster": "leiden"}[effective_mode]
        if effective_mode == "sample" and not _obs_has(data, group_key):
            data.close()
            raise SystemExit(
                f"[call-peaks] --peak_calling {effective_mode} requires obs['{group_key}']; "
                "run the corresponding import/cluster stage first"
            )
        try:
            if group_key:
                snap.tl.macs3(data, groupby=group_key)
            else:
                snap.tl.macs3(data)
        except Exception as error:
            data.close()
            raise SystemExit(f"[call-peaks] MACS3 failed; provide --peaks or inspect env: {error}") from error
        tables = _macs3_peak_tables(data, grouped=group_key is not None)
        if not tables:
            data.close()
            raise SystemExit(
                "[call-peaks] MACS3 finished but "
                f"uns['{_macs3_uns_key(group_key is not None)}'] is missing or empty. "
                "That is the only key written by this --peak_calling mode; stale tables "
                "left in .uns by other modes are ignored, and var_names (tile bins) are "
                "never exported as peaks. Provide --peaks or inspect the env."
            )
        try:
            merged = snap.tl.merge_peaks(tables, _pick_genome(args.genome_build))
        except Exception as error:
            data.close()
            raise SystemExit(f"[call-peaks] merge_peaks failed: {error}") from error
        data.close()
        n_peaks = _write_bed_rows(_peaks_path(args), _peaks_column(merged))
        if not n_peaks:
            raise SystemExit(
                "[call-peaks] merged MACS3 peaks could not be exported to BED; provide --peaks"
            )
        print(f"[call-peaks] wrote {_peaks_path(args)} ({n_peaks} merged peaks)")
    summary = _load_summary(args)
    summary["peaks_file"] = _peaks_path(args) if os.path.exists(_peaks_path(args)) else args.peaks
    summary["n_peaks_called"] = n_peaks
    summary["peak_calling_requested"] = getattr(args, "peak_calling", "auto")
    summary["peak_calling"] = locals().get("effective_mode", "provided")
    summary.setdefault("stages_completed", []).append("call-peaks")
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args, summary)


def make_peak_matrix(args):
    snap = _snap()
    data = _load_data(args)
    peaks = _peaks_path(args) if os.path.exists(_peaks_path(args)) else args.peaks
    if not peaks:
        data.close()
        raise SystemExit(
            "[make-peak-matrix] no peaks available; run call-peaks first or provide --peaks"
        )
    require_files(peaks)
    out_path = _peak_matrix_path(args)

    n_before_frip = int(data.n_obs)
    n_removed_frip = 0
    frip_median = None
    if args.min_frip and args.min_frip > 0:
        fracs = _region_fractions(snap, data, peaks, "frip", "make-peak-matrix")
        keep = [i for i, frac in enumerate(fracs) if frac >= args.min_frip]
        n_removed_frip = n_before_frip - len(keep)
        frip_median = round(statistics.median(fracs), 4) if fracs else None
        if not keep:
            data.close()
            raise SystemExit(
                f"[make-peak-matrix] min_frip={args.min_frip} removed all "
                f"{n_before_frip} cells (median FRiP {frip_median}); "
                "check peak quality or lower --min_frip"
            )
        if n_removed_frip:
            data.subset(keep)
            print(f"[make-peak-matrix] FRiP gate removed {n_removed_frip} cells "
                  f"(median FRiP {frip_median})")

    try:
        # make_peak_matrix parameters are keyword-only; a positional second
        # argument would be misread as use_rep on older releases.
        if _is_collection(args):
            peak_data = snap.pp.make_peak_matrix(data, peak_file=peaks, file=out_path)
        else:
            peak_data = snap.pp.make_peak_matrix(data, peak_file=peaks)
    except TypeError as error:
        data.close()
        raise SystemExit(
            f"[make-peak-matrix] snapatac2.pp.make_peak_matrix rejected peak_file=: {error}"
        ) from error
    if hasattr(peak_data, "write"):
        if not os.path.exists(out_path):
            peak_data.write(out_path)
        try:
            peak_data.close()
        except Exception:
            pass
    else:
        data.write(out_path)
    barcodes = _write_barcodes(args, data)
    data.close()
    summary = _load_summary(args)
    summary.update({"peak_matrix": out_path, "peaks_file": peaks, "barcodes_file": barcodes, "representation_quality": "fragment_recomputed", "updated_at": datetime.now(timezone.utc).isoformat()})
    if args.min_frip and args.min_frip > 0:
        summary["n_cells_removed_frip"] = n_removed_frip
        if frip_median is not None:
            summary["frip_median"] = frip_median
        summary.setdefault("filter_thresholds", {})["min_frip"] = args.min_frip
        declared = summary.get("thresholds_declared_not_applied") or {}
        declared.pop("min_frip", None)
        summary["thresholds_declared_not_applied"] = declared
    summary.setdefault("stages_completed", []).append("make-peak-matrix")
    _save_summary(args, summary)
    print(f"[make-peak-matrix] wrote {out_path}")


def _require_fragment_deliverables(args, summary: dict) -> dict:
    """Fail fast when the peak-matrix package is incomplete (mirrors multiome)."""
    matrix = summary.get("peak_matrix") or (
        _peak_matrix_path(args) if os.path.exists(_peak_matrix_path(args)) else None
    )
    peaks = summary.get("peaks_file") or (
        _peaks_path(args) if os.path.exists(_peaks_path(args)) else None
    )
    barcodes = summary.get("barcodes_file") or (
        _barcodes_path(args) if os.path.exists(_barcodes_path(args)) else None
    )
    required = {"peak_matrix": matrix, "peaks": peaks, "barcodes": barcodes}
    missing = [
        name
        for name, path in required.items()
        if not path or not os.path.isfile(str(path))
    ]
    if missing:
        raise SystemExit(
            "[finalize] missing required deliverable(s): "
            + ", ".join(missing)
            + ". Run call-peaks and make-peak-matrix before finalize."
        )
    if summary.get("genome_build", args.genome_build) not in {"GRCh38", "hg38"}:
        raise SystemExit("[finalize] final peak matrix must be GRCh38/hg38")
    return {name: str(path) for name, path in required.items()}


def finalize(args):
    out_dir = _out_dir(args)
    summary = _load_summary(args)
    deliverables = _require_fragment_deliverables(args, summary)
    data = _load_data(args)
    summary["n_cells_final"] = int(data.n_obs)
    final_sample_counts = _sample_counts(data)
    if final_sample_counts:
        summary["sample_cell_counts_final"] = final_sample_counts
    if _obs_has(data, "tsse"):
        summary["tsse_median"] = round(_median(_obs_values(data, "tsse")), 2)
    frag_key = "n_fragment" if _obs_has(data, "n_fragment") else "n_fragments" if _obs_has(data, "n_fragments") else None
    if frag_key:
        summary["fragments_median"] = round(_median(_obs_values(data, frag_key)), 1)
    data.close()
    summary["peak_matrix"] = deliverables["peak_matrix"]
    summary["peaks_file"] = deliverables["peaks"]
    summary["barcodes_file"] = deliverables["barcodes"]
    summary.setdefault("stages_completed", []).append("finalize")
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_summary(args, summary)
    sample_qc_path = ""
    imported_counts = summary.get("sample_cell_counts_imported") or {}
    if imported_counts or final_sample_counts:
        sample_qc_path = os.path.join(out_dir, "sample_qc_summary.csv")
        sample_ids = sorted(set(imported_counts) | set(final_sample_counts))
        with open(sample_qc_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["sample_id", "n_cells_imported", "n_cells_final", "retention_fraction"])
            writer.writeheader()
            for sample_id in sample_ids:
                imported = int(imported_counts.get(sample_id, 0))
                final = int(final_sample_counts.get(sample_id, 0))
                writer.writerow({
                    "sample_id": sample_id,
                    "n_cells_imported": imported,
                    "n_cells_final": final,
                    "retention_fraction": round(final / imported, 4) if imported else "",
                })
    card = {
        "dataset_id": args.dataset_id,
        "deliverable": "grch38_per_dataset_peak_matrix",
        "genome_build": summary.get("genome_build", args.genome_build),
        "files": {
            "peak_matrix": summary.get("peak_matrix"),
            "peaks": summary.get("peaks_file"),
            "barcodes": summary.get("barcodes_file"),
            "qc_summary": _summary_path(args),
            "fragment_sample_manifest": summary.get("sample_manifest") or "",
            "sample_qc_summary": sample_qc_path,
        },
        "qc_summary": summary,
    }
    with open(os.path.join(out_dir, "data_card.json"), "w", encoding="utf-8") as handle:
        json.dump(card, handle, indent=2, ensure_ascii=False)
    log_provenance(args.results_root, {"event": "scatac_finalize", "dataset_id": args.dataset_id, "n_cells_final": summary["n_cells_final"]})
    print(f"[finalize] package ready: {out_dir}")


def _parser(parser):
    parser.add_argument(
        "--fragments",
        help="single fragment file, directory of fragment files, or CSV fragment manifest (required for --stage=import)",
    )
    parser.add_argument("--peaks", help="optional precomputed peaks.bed for call-peaks/make-peak-matrix")
    parser.add_argument("--genome_build", default="GRCh38")
    parser.add_argument("--results_root", required=True)
    parser.add_argument("--dataset_id", required=True)
    parser.add_argument("--import_min_fragments", type=int, default=200)
    parser.add_argument("--import_jobs", type=int, default=4)
    parser.add_argument("--min_fragments", type=int, default=1000)
    parser.add_argument("--max_fragments", type=int, default=100000)
    parser.add_argument("--min_tsse", type=float, default=4.0)
    parser.add_argument("--max_blacklist_frac", type=float, default=0.05)
    parser.add_argument("--min_frip", type=float, default=0.10)
    parser.add_argument(
        "--blacklist_bed",
        default=None,
        help="ENCODE blacklist BED (e.g. reference/hg38-blacklist.v2.bed from "
        "resource-setup); enables the max_blacklist_frac gate at the filter stage",
    )
    parser.add_argument("--tile_size", type=int, default=500)
    parser.add_argument("--n_features", type=int, default=250000)
    parser.add_argument("--n_comps", type=int, default=30)
    parser.add_argument("--leiden_res", type=float, default=1.0)
    parser.add_argument("--expected_doublet_rate", type=float, default=0.08)
    parser.add_argument(
        "--peak_calling",
        choices=["auto", "dataset", "sample", "cluster"],
        default="auto",
        help="auto uses dataset pseudobulk for one fragment file and per-sample pseudobulk for a collection",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser


if __name__ == "__main__":
    run_stages("scatac_fragment_qc", {
        "import": import_data,
        "pre-filter": pre_filter,
        "filter": filter_cells,
        "embed": embed,
        "cluster": cluster,
        "doublet": doublet,
        "call-peaks": call_peaks,
        "make-peak-matrix": make_peak_matrix,
        "finalize": finalize,
    }, _parser)
