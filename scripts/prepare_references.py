#!/usr/bin/env python
"""prepare-references: GRCh38 reference assets with pinned URLs + sha256.

Contract: skills/resource-setup/SKILL.md
Env: curator (stdlib only, works with any Python >= 3.10)

Stages:
- plan:   write reference_plan.json (asset list, no network access)
- fetch:  download each asset, verify sha256 before moving into place,
          decompress the blacklist; already-verified files are skipped
- verify: re-check checksums + format sanity, write reference_manifest.json

TSS/gene annotation is intentionally not fetched here: the fragment pipeline
computes TSSe via snapatac2's built-in GENCODE annotation. Recorded in the
plan/manifest notes so the gap is explicit rather than silent.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from _common import run_stages

# Checksums pinned 2026-08-08 by downloading each asset and hashing it
# The blacklist checksum covers the .gz file
# as served; the decompressed BED is validated by format checks in verify.
ASSETS = {
    "chrom_sizes": {
        "filename": "hg38.chrom.sizes",
        "url": "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.chrom.sizes",
        "sha256": "e1d9152418038457a959e949d99c9caf7ae3e4f87cfbb4ffe7d8b9a54ba1202b",
        "liftover": False,
    },
    "blacklist": {
        "filename": "hg38-blacklist.v2.bed.gz",
        "url": "https://raw.githubusercontent.com/Boyle-Lab/Blacklist/master/lists/hg38-blacklist.v2.bed.gz",
        "sha256": "c92e763af17271446194991e71917ac220593a5a3d40a06667be24178ef08cf2",
        "gunzip_to": "hg38-blacklist.v2.bed",
        "liftover": False,
    },
    "liftover_chain": {
        "filename": "hg19ToHg38.over.chain.gz",
        "url": "https://hgdownload.soe.ucsc.edu/goldenPath/hg19/liftOver/hg19ToHg38.over.chain.gz",
        "sha256": "5c0598e500ceb5a78c73086929e8ef993aec309bcafb595139b53d440b125a1d",
        "liftover": True,
    },
}

NOTES = {
    "tss_annotation": (
        "not fetched: TSSe in scatac_fragment_qc.py uses snapatac2's built-in "
        "GENCODE annotation"
    ),
}


def _require_grch38(args) -> None:
    if args.genome_build != "GRCh38":
        sys.exit(
            f"[error] only GRCh38 is supported (got {args.genome_build!r}); "
            "hg19 inputs need the liftover chain plus a not-yet-implemented "
            "liftover step"
        )


def _selected_assets(args) -> dict:
    return {
        name: asset
        for name, asset in ASSETS.items()
        if args.include_liftover or not asset["liftover"]
    }


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: str) -> None:
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)


def _asset_verified(path: str, asset: dict) -> bool:
    return os.path.exists(path) and _sha256_file(path) == asset["sha256"]


def _check_chrom_sizes(path: str) -> int:
    """Every line must be '<chrom>\t<int>'; chr1 must be present."""
    n = 0
    seen_chr1 = False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2 or not parts[1].isdigit():
                sys.exit(f"[error] malformed chrom sizes line in {path}: {line!r}")
            seen_chr1 = seen_chr1 or parts[0] == "chr1"
            n += 1
    if n == 0 or not seen_chr1:
        sys.exit(f"[error] chrom sizes file {path} is empty or lacks chr1")
    return n


def _check_blacklist_bed(path: str) -> int:
    """BED with >=3 columns, integer start < end."""
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3 or not (parts[1].isdigit() and parts[2].isdigit()):
                sys.exit(f"[error] malformed blacklist BED line in {path}: {line!r}")
            if int(parts[1]) >= int(parts[2]):
                sys.exit(f"[error] blacklist interval start>=end in {path}: {line!r}")
            n += 1
    if n == 0:
        sys.exit(f"[error] blacklist BED {path} is empty")
    return n


def plan(args) -> None:
    _require_grch38(args)
    os.makedirs(args.out, exist_ok=True)
    assets = [
        {"name": name, **{k: v for k, v in asset.items() if k != "liftover"}}
        for name, asset in _selected_assets(args).items()
    ]
    plan_doc = {
        "genome_build": args.genome_build,
        "assets": assets,
        "notes": NOTES,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    plan_path = os.path.join(args.out, "reference_plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan_doc, fh, ensure_ascii=False, indent=2)
    print(f"[plan] {len(assets)} asset(s) -> {plan_path}")
    for asset in assets:
        print(f"[plan]   {asset['name']}: {asset['url']}")


def fetch(args) -> None:
    _require_grch38(args)
    os.makedirs(args.out, exist_ok=True)
    for name, asset in _selected_assets(args).items():
        dest = os.path.join(args.out, asset["filename"])
        if _asset_verified(dest, asset):
            print(f"[fetch] {name}: already present and verified, skipping")
        else:
            tmp = dest + ".tmp"
            print(f"[fetch] {name}: downloading {asset['url']}")
            try:
                _download(asset["url"], tmp)
                got = _sha256_file(tmp)
                if got != asset["sha256"]:
                    sys.exit(
                        f"[error] checksum mismatch for {name}: "
                        f"expected {asset['sha256']}, got {got}"
                    )
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            print(f"[fetch] {name}: OK ({os.path.getsize(dest)} bytes)")
        if asset.get("gunzip_to"):
            bed_path = os.path.join(args.out, asset["gunzip_to"])
            with gzip.open(dest, "rb") as src, open(bed_path, "wb") as out:
                out.write(src.read())
            print(f"[fetch] {name}: decompressed -> {bed_path}")


def verify(args) -> None:
    _require_grch38(args)
    manifest_assets = {}
    for name, asset in _selected_assets(args).items():
        dest = os.path.join(args.out, asset["filename"])
        if not os.path.exists(dest):
            sys.exit(f"[error] missing asset {name}: {dest} (run --stage fetch)")
        got = _sha256_file(dest)
        if got != asset["sha256"]:
            sys.exit(
                f"[error] checksum mismatch for {name}: "
                f"expected {asset['sha256']}, got {got}"
            )
        entry = {
            "path": asset["filename"],
            "url": asset["url"],
            "sha256": asset["sha256"],
            "bytes": os.path.getsize(dest),
        }
        if name == "chrom_sizes":
            entry["n_chromosomes"] = _check_chrom_sizes(dest)
        if asset.get("gunzip_to"):
            bed_path = os.path.join(args.out, asset["gunzip_to"])
            if not os.path.exists(bed_path):
                sys.exit(f"[error] missing decompressed file {bed_path} (run --stage fetch)")
            entry["decompressed_path"] = asset["gunzip_to"]
            entry["n_intervals"] = _check_blacklist_bed(bed_path)
        manifest_assets[name] = entry
        print(f"[verify] {name}: OK")
    manifest = {
        "genome_build": args.genome_build,
        "assets": manifest_assets,
        "notes": NOTES,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = os.path.join(args.out, "reference_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(f"[verify] manifest -> {manifest_path}")


def _parser(p):
    p.add_argument("--out", default="reference", help="reference output directory")
    p.add_argument("--genome_build", default="GRCh38")
    p.add_argument("--include_liftover", action="store_true")
    return p


if __name__ == "__main__":
    run_stages("prepare_references", {"plan": plan, "fetch": fetch, "verify": verify}, _parser)
