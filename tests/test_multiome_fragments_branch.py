"""multiome fragments branch must materialize the ATAC deliverable at intersect.

Historically the fragments branch stopped after pair counting: no peak calling,
no peak matrix, and finalize (correctly) failed. intersect must now, for
atac_input_type == "fragments": subset the backed ATAC to paired-pass cells,
call MACS3 (dataset-level) + merge_peaks, export peaks.hg38.bed, materialize
peak_matrix.h5ad and barcodes.tsv.gz — or fail loudly, never half-deliver.
"""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

multiome_qc = import_script("multiome_qc")

MERGED_PEAKS = ["chr1:100-600", "chr2:50-550"]


class FakeRna:
    def __init__(self, obs_names):
        self.obs_names = list(obs_names)
        self.obs = {}


class FakeBackedAtac:
    """Backed handle: intersect must never subset/mutate it (that corrupts
    the working file on real data); it may only read obs_names and close."""

    def __init__(self, obs_names):
        self.obs_names = list(obs_names)
        self.obs = {}
        self.n_obs = len(self.obs_names)
        self.subset_calls = []
        self.closed = False

    def subset(self, *args, **kwargs):
        raise AssertionError("backed subset must not be called on the working file")

    def close(self):
        self.closed = True


class FakeMemAtac:
    """In-memory AnnData stand-in supporting obs_names fancy indexing."""

    def __init__(self, obs_names):
        self.obs_names = list(obs_names)
        self.uns = {}
        self.n_obs = len(self.obs_names)

    def __getitem__(self, names):
        return FakeMemAtac([str(n) for n in names])

    def copy(self):
        clone = FakeMemAtac(self.obs_names)
        clone.uns = dict(self.uns)
        return clone


class FakePeakMatrix:
    def __init__(self, n_obs):
        self.n_obs = n_obs

    def write(self, path):
        Path(path).write_text("PEAK_MATRIX", encoding="utf-8")

    def close(self):
        pass


def fake_scanpy(rna):
    sc = types.ModuleType("scanpy")
    sc.read = lambda path: rna
    return sc


def fake_snap(record, *, macs3_writes_uns=True):
    snap = types.ModuleType("snapatac2")

    def read(path, backed="r+"):
        if backed is None:
            record["read_in_memory"] = True
            return record["mem"]
        return record["atac"]

    def macs3(data, groupby=None, **kwargs):
        record["macs3_called_n_obs"] = data.n_obs
        record["macs3_groupby"] = groupby
        if macs3_writes_uns:
            data.uns["macs3_pseudobulk"] = "BULK_TABLE"

    def merge_peaks(tables, chrom_sizes, **kwargs):
        record["merge_tables"] = tables
        return {"Peaks": list(MERGED_PEAKS)}

    def make_peak_matrix(data, peak_file=None, **kwargs):
        record["make_peak_matrix_n_obs"] = data.n_obs
        record["peak_file"] = peak_file
        return FakePeakMatrix(data.n_obs)

    snap.read = read
    snap.tl = types.SimpleNamespace(macs3=macs3, merge_peaks=merge_peaks)
    snap.pp = types.SimpleNamespace(make_peak_matrix=make_peak_matrix)
    snap.genome = types.SimpleNamespace(hg38="G", hg19="G", mm10="G", mm39="G")
    return snap


def base_args(tmp: str, **overrides) -> Namespace:
    defaults = dict(
        rna="rna.h5",
        atac_fragments="frags.tsv.gz",
        atac_matrix=None,
        peaks=None,
        genome_build="GRCh38",
        results_root=str(tmp),
        dataset_id="ds",
        min_pair_overlap=0.5,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


def setup_workdir(tmp: str) -> Path:
    processed = Path(tmp) / "processed" / "ds"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "rna_support.h5ad").write_text("stub", encoding="utf-8")
    (processed / "atac_qc.h5ad").write_text("stub", encoding="utf-8")
    (processed / "qc_summary.json").write_text(
        json.dumps({"atac_input_type": "fragments", "genome_build": "GRCh38"}),
        encoding="utf-8",
    )
    return processed


def run_intersect(tmp, *, rna_names, atac_names, args=None, **snap_kwargs):
    setup_workdir(tmp)
    record = {
        "atac": FakeBackedAtac(atac_names),
        "mem": FakeMemAtac(atac_names),
    }
    snap = fake_snap(record, **snap_kwargs)
    sc = fake_scanpy(FakeRna(rna_names))
    args = args or base_args(tmp)
    modules = {
        "snapatac2": snap,
        "scanpy": sc,
        "hdf5plugin": types.ModuleType("hdf5plugin"),
    }
    with mock.patch.dict(sys.modules, modules):
        multiome_qc.intersect(args)
    return record


def read_summary(tmp: str) -> dict:
    return json.loads(
        (Path(tmp) / "processed" / "ds" / "qc_summary.json").read_text()
    )


class BackedObsLikePyDataFrameElem:
    """Mimics snapatac2's backed ``PyDataFrameElem``: no ``.columns``, no
    ``__contains__``; missing keys raise RuntimeError on access."""

    def __init__(self):
        self._store = {"n_fragment": [5000, 6000, 7000]}

    def __getitem__(self, key):
        if key not in self._store:
            raise RuntimeError(f'not found: "{key}" not found')
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value


class FakeBackedQcAtac:
    def __init__(self):
        self.obs = BackedObsLikePyDataFrameElem()
        self.obs_names = ["AAA-1", "BBB-1", "CCC-1"]
        self.n_obs = 3
        self.closed = False

    def close(self):
        self.closed = True


def fake_snap_qc_atac(record, *, tsse_fails=False):
    snap = types.ModuleType("snapatac2")

    def read(path):
        return record["atac"]

    def tsse(data, genome, **kwargs):
        if tsse_fails:
            raise RuntimeError("annotation download failed")
        record["tsse_computed"] = True
        data.obs["tsse"] = [10.0, 12.0, 14.0]

    def filter_cells(data, min_counts=None, min_tsse=None, max_counts=None, inplace=True):
        # Mirrors snapatac2: touching obs["tsse"] before it exists raises.
        if min_tsse:
            data.obs["tsse"]
        record["filter_cells_ran"] = True

    noop = lambda *a, **k: None
    snap.read = read
    snap.metrics = types.SimpleNamespace(tsse=tsse)
    snap.pp = types.SimpleNamespace(
        filter_cells=filter_cells,
        add_tile_matrix=noop,
        select_features=noop,
        knn=noop,
        scrublet=noop,
        filter_doublets=noop,
    )
    snap.tl = types.SimpleNamespace(spectral=noop, umap=noop, leiden=noop)
    snap.genome = types.SimpleNamespace(hg38="G", hg19="G", mm10="G", mm39="G")
    return snap


def qc_atac_args(tmp: str) -> Namespace:
    return Namespace(
        rna="rna.h5",
        atac_fragments="frags.tsv.gz",
        atac_matrix=None,
        peaks=None,
        genome_build="GRCh38",
        results_root=str(tmp),
        dataset_id="ds",
        min_fragments=1000,
        max_fragments=100000,
        min_tsse=4.0,
        tile_size=500,
        n_features=250000,
        n_comps=30,
        leiden_res=1.0,
        expected_doublet_rate=0.08,
    )


class QcAtacFragmentsTests(unittest.TestCase):
    """The fragments qc-atac path must compute TSSe on backed AnnData whose
    obs has no ``.columns`` attribute, and must fail loudly when TSSe cannot
    be computed (min_tsse is a hard gate)."""

    def run_qc_atac(self, tmp, **snap_kwargs):
        setup_workdir(tmp)
        record = {"atac": FakeBackedQcAtac()}
        snap = fake_snap_qc_atac(record, **snap_kwargs)
        with mock.patch.dict(sys.modules, {"snapatac2": snap}):
            multiome_qc.qc_atac(qc_atac_args(tmp))
        return record

    def test_tsse_computed_despite_pydataframeelem_obs(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_qc_atac(tmp)
            summary = read_summary(tmp)

        self.assertTrue(record.get("tsse_computed"))
        self.assertTrue(record.get("filter_cells_ran"))
        self.assertIn("qc-atac", summary["stages_completed"])

    def test_tsse_failure_is_fatal_not_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_qc_atac(tmp, tsse_fails=True)


class FragmentsIntersectTests(unittest.TestCase):
    def test_intersect_materializes_atac_deliverables(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = run_intersect(
                tmp,
                rna_names=["AAA-1", "BBB-1", "CCC-1"],
                atac_names=["AAA-1", "BBB-1", "DDD-1"],
            )
            processed = Path(tmp) / "processed" / "ds"
            bed = (processed / "peaks.hg38.bed").read_text(encoding="utf-8")
            matrix_exists = (processed / "peak_matrix.h5ad").exists()
            with gzip.open(processed / "barcodes.tsv.gz", "rt") as fh:
                barcodes = fh.read().split()
            summary = read_summary(tmp)

        # subset to the 2 paired barcodes before peak calling, done on the
        # in-memory copy (backed working file must never be subset/mutated)
        self.assertTrue(record.get("read_in_memory"))
        self.assertEqual(record["atac"].subset_calls, [])
        self.assertEqual(record["macs3_called_n_obs"], 2)
        self.assertIsNone(record["macs3_groupby"])
        self.assertEqual(record["merge_tables"], {"all": "BULK_TABLE"})
        self.assertEqual(record["make_peak_matrix_n_obs"], 2)
        self.assertEqual(bed, "chr1\t100\t600\nchr2\t50\t550\n")
        self.assertTrue(matrix_exists)
        self.assertEqual(barcodes, ["AAA-1", "BBB-1"])
        self.assertEqual(summary["n_paired_pass"], 2)
        self.assertEqual(summary["n_peaks_called"], 2)
        self.assertEqual(summary["peak_calling"], "dataset")
        self.assertTrue(summary["peak_matrix"].endswith("peak_matrix.h5ad"))
        self.assertTrue(summary["peaks_file"].endswith("peaks.hg38.bed"))
        self.assertTrue(summary["barcodes_file"].endswith("barcodes.tsv.gz"))

    def test_intersect_with_user_peaks_skips_macs3(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_peaks = Path(tmp) / "user_peaks.bed"
            user_peaks.write_text("chr3\t10\t20\n", encoding="utf-8")
            record = run_intersect(
                tmp,
                rna_names=["AAA-1"],
                atac_names=["AAA-1"],
                args=base_args(tmp, peaks=str(user_peaks)),
            )
            bed = (
                Path(tmp) / "processed" / "ds" / "peaks.hg38.bed"
            ).read_text(encoding="utf-8")

        self.assertNotIn("macs3_called_n_obs", record)
        self.assertEqual(bed, "chr3\t10\t20\n")
        self.assertEqual(record["make_peak_matrix_n_obs"], 1)

    def test_intersect_zero_paired_fails_without_deliverables(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                run_intersect(
                    tmp,
                    rna_names=["AAA-1"],
                    atac_names=["ZZZ-1"],
                )
            processed = Path(tmp) / "processed" / "ds"
            self.assertFalse((processed / "peak_matrix.h5ad").exists())

    def test_intersect_fails_when_macs3_produces_no_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                run_intersect(
                    tmp,
                    rna_names=["AAA-1"],
                    atac_names=["AAA-1"],
                    macs3_writes_uns=False,
                )
            processed = Path(tmp) / "processed" / "ds"
            self.assertFalse((processed / "peak_matrix.h5ad").exists())

    def test_finalize_passes_after_fragments_intersect(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_intersect(
                tmp,
                rna_names=["AAA-1", "BBB-1"],
                atac_names=["AAA-1", "BBB-1"],
            )
            multiome_qc.finalize(base_args(tmp))
            card = json.loads(
                (Path(tmp) / "processed" / "ds" / "data_card.json").read_text()
            )

        self.assertEqual(
            card["deliverable"], "atac_grch38_peak_matrix_for_paired_cells"
        )


if __name__ == "__main__":
    unittest.main()
