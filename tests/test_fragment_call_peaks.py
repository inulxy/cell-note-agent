"""call-peaks must export merged MACS3 peaks, never tile-bin var_names.

SnapATAC2 stores MACS3 results in ``.uns['macs3']`` (grouped) or
``.uns['macs3_pseudobulk']`` (bulk) and requires ``tl.merge_peaks`` to build the
final peak universe. ``var_names`` at this stage are 500bp tile bins from
``add_tile_matrix``; exporting them silently produces a cell-by-bin matrix.
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

fragment_qc = import_script("scatac_fragment_qc")

TILE_BINS = ["chr1:0-500", "chr1:500-1000", "chr2:0-500"]
MERGED_PEAKS = ["chr1:100-600", "chr2:50-550"]


class FakeBackedData:
    def __init__(self, var_names, uns=None):
        self.var_names = list(var_names)
        self.uns = dict(uns or {})
        self.n_obs = 4
        self.closed = False

    def close(self):
        self.closed = True


def fake_snap_module(record, *, macs3_writes_uns=True, merged_peaks=MERGED_PEAKS):
    snap = types.ModuleType("snapatac2")
    snap.genome = types.SimpleNamespace(
        hg38="GENOME_HG38", hg19="GENOME_HG19", mm10="GENOME_MM10", mm39="GENOME_MM39"
    )

    def read(path):
        record["read_path"] = path
        return record["data"]

    def macs3(data, groupby=None, **kwargs):
        record["macs3_groupby"] = groupby
        if not macs3_writes_uns:
            return
        if groupby is None:
            data.uns["macs3_pseudobulk"] = "BULK_TABLE"
        else:
            data.uns["macs3"] = {"0": "TABLE0", "1": "TABLE1"}

    def merge_peaks(tables, chrom_sizes, half_width=250):
        record["merge_tables"] = tables
        record["merge_chrom_sizes"] = chrom_sizes
        return {"Peaks": list(merged_peaks)}

    snap.read = read
    snap.tl = types.SimpleNamespace(macs3=macs3, merge_peaks=merge_peaks)
    snap.pp = types.SimpleNamespace()
    snap.metrics = types.SimpleNamespace()
    return snap


class CallPeaksTests(unittest.TestCase):
    def make_args(self, tmp: str, peak_calling: str) -> Namespace:
        processed = Path(tmp) / "processed" / "ds"
        processed.mkdir(parents=True, exist_ok=True)
        (processed / "atac_qc.h5ad").write_text("stub", encoding="utf-8")
        return Namespace(
            fragments=None,
            peaks=None,
            genome_build="GRCh38",
            results_root=str(tmp),
            dataset_id="ds",
            peak_calling=peak_calling,
        )

    def run_call_peaks(
        self,
        tmp: str,
        *,
        peak_calling: str = "dataset",
        initial_uns: dict | None = None,
        **snap_kwargs,
    ):
        record = {"data": FakeBackedData(TILE_BINS, uns=initial_uns)}
        snap = fake_snap_module(record, **snap_kwargs)
        args = self.make_args(tmp, peak_calling)
        with mock.patch.dict(sys.modules, {"snapatac2": snap}):
            fragment_qc.call_peaks(args)
        return record

    def bed_path(self, tmp: str) -> Path:
        return Path(tmp) / "processed" / "ds" / "peaks.hg38.bed"

    def test_dataset_mode_exports_merged_macs3_peaks_not_tile_bins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_call_peaks(tmp)
            content = self.bed_path(tmp).read_text(encoding="utf-8")

        self.assertEqual(content, "chr1\t100\t600\nchr2\t50\t550\n")
        self.assertNotIn("chr1\t0\t500", content)
        self.assertIsNone(record["macs3_groupby"])
        self.assertEqual(record["merge_tables"], {"all": "BULK_TABLE"})
        self.assertEqual(record["merge_chrom_sizes"], "GENOME_HG38")

    def test_cluster_mode_groups_by_leiden_and_merges_group_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_call_peaks(tmp, peak_calling="cluster")
            content = self.bed_path(tmp).read_text(encoding="utf-8")

        self.assertEqual(record["macs3_groupby"], "leiden")
        self.assertEqual(record["merge_tables"], {"0": "TABLE0", "1": "TABLE1"})
        self.assertEqual(content, "chr1\t100\t600\nchr2\t50\t550\n")

    def test_missing_uns_peak_tables_fail_instead_of_exporting_var_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_call_peaks(tmp, macs3_writes_uns=False)
            self.assertFalse(self.bed_path(tmp).exists())

    def test_dataset_mode_ignores_stale_cluster_tables_in_uns(self) -> None:
        """The working h5ad keeps .uns across runs: a previous --peak_calling
        cluster run leaves uns['macs3'] behind. A dataset-mode re-run writes
        uns['macs3_pseudobulk'] and MUST merge that, not the stale dict."""
        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_call_peaks(
                tmp,
                peak_calling="dataset",
                initial_uns={"macs3": {"0": "STALE0", "1": "STALE1"}},
            )

        self.assertEqual(record["merge_tables"], {"all": "BULK_TABLE"})

    def test_cluster_mode_ignores_stale_pseudobulk_tables_in_uns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_call_peaks(
                tmp,
                peak_calling="cluster",
                initial_uns={"macs3_pseudobulk": "STALE_BULK"},
            )

        self.assertEqual(record["merge_tables"], {"0": "TABLE0", "1": "TABLE1"})

    def test_dataset_mode_fails_when_only_stale_cluster_tables_exist(self) -> None:
        """If this run produced nothing, stale tables from the other mode must
        not be shipped as a silent fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_call_peaks(
                    tmp,
                    macs3_writes_uns=False,
                    initial_uns={"macs3": {"0": "STALE0"}},
                )
            self.assertFalse(self.bed_path(tmp).exists())

    def test_cluster_mode_fails_when_only_stale_pseudobulk_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_call_peaks(
                    tmp,
                    peak_calling="cluster",
                    macs3_writes_uns=False,
                    initial_uns={"macs3_pseudobulk": "STALE_BULK"},
                )
            self.assertFalse(self.bed_path(tmp).exists())


if __name__ == "__main__":
    unittest.main()
