#!/usr/bin/env bash
# Download the CellNote golden fixtures with sha256 verification.
# Binaries are intentionally NOT committed to git; sources are stable public
# URLs (10x Genomics CDN / OSF mirror used by snapatac2's own dataset registry).
#
# Usage: ./fetch_fixtures.sh [target_dir]   (default: this script's directory)
set -euo pipefail

TARGET="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
mkdir -p "$TARGET"

fetch() {
  local name="$1" url="$2" sha="$3"
  local path="$TARGET/$name"
  if [ -f "$path" ] && echo "$sha  $path" | sha256sum -c --status 2>/dev/null; then
    echo "[fixtures] $name already present and verified"
    return
  fi
  echo "[fixtures] downloading $name"
  curl -fL --retry 3 -o "$path.tmp" "$url"
  echo "$sha  $path.tmp" | sha256sum -c --status
  mv "$path.tmp" "$path"
  echo "[fixtures] $name OK"
}

# scATAC fragments (10x atac_pbmc_500_nextgem, official downsampled variant;
# same file and checksum as snapatac2.datasets.pbmc500(downsample=True)).
fetch scatac_pbmc500_downsample.fragments.tsv.gz \
  "https://osf.io/download/wjv4b" \
  "6053cf4578a140bfd8ce34964602769dc5f5ec6b25ba4f2db23cdbd4681b0e2f"

# Multiome 10x ARC PBMC 3k combined feature matrix (GEX + Peaks).
# Serves BOTH the multiome branch and the scatac-peak-matrix branch.
fetch multiome_pbmc3k_arc.filtered_feature_bc_matrix.h5 \
  "https://cf.10xgenomics.com/samples/cell-arc/2.0.0/pbmc_granulocyte_sorted_3k/pbmc_granulocyte_sorted_3k_filtered_feature_bc_matrix.h5" \
  "5fbff5a4d85e0df345f6502e966ec787a8a4c429fd6b88a8772c43fd915cf3ff"

echo "[fixtures] all fixtures ready under $TARGET"
