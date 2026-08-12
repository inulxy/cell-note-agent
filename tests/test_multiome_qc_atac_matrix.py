"""qc-atac on peak-matrix input must apply real per-cell thresholds.

The historical implementation set ``atac_pass = True`` for every cell
(pure passthrough). It must now mark cells failing --atac_min_counts /
--atac_min_peaks as atac_pass=False (matching scatac_peak_matrix.py's
min_counts/min_peaks semantics), and fail loudly when nothing passes.
"""
from __future__ import annotations

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


class FakeX:
    def __init__(self, totals, nnzs):
        self._totals = list(totals)
        self._nnzs = list(nnzs)

    def sum(self, axis=None):
        assert axis == 1
        return list(self._totals)

    def getnnz(self, axis=None):
        assert axis == 1
        return list(self._nnzs)


class FakeMatrixAnnData:
    def __init__(self, totals, nnzs):
        self.X = FakeX(totals, nnzs)
        self.n_obs = len(totals)
        self.obs_names = [f"BC{i}" for i in range(self.n_obs)]
        self.obs = {}
        self.written_to = None

    def write(self, path):
        self.written_to = str(path)


def fake_scanpy(adata):
    sc = types.ModuleType("scanpy")
    sc.read = lambda path: adata
    return sc


def make_args(tmp: str, **overrides) -> Namespace:
    defaults = dict(
        rna="rna.h5",
        atac_fragments=None,
        atac_matrix="atac.h5",
        peaks=None,
        genome_build="GRCh38",
        results_root=str(tmp),
        dataset_id="ds",
        atac_min_counts=1000,
        atac_min_peaks=500,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


def setup_workdir(tmp: str) -> Path:
    processed = Path(tmp) / "processed" / "ds"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "atac_qc.h5ad").write_text("stub", encoding="utf-8")
    (processed / "qc_summary.json").write_text(
        json.dumps({"atac_input_type": "peak_matrix", "genome_build": "GRCh38"}),
        encoding="utf-8",
    )
    return processed


def read_summary(tmp: str) -> dict:
    return json.loads(
        (Path(tmp) / "processed" / "ds" / "qc_summary.json").read_text()
    )


class QcAtacMatrixThresholdTests(unittest.TestCase):
    def run_qc_atac(self, tmp, *, totals, nnzs, **arg_overrides):
        setup_workdir(tmp)
        adata = FakeMatrixAnnData(totals, nnzs)
        with mock.patch.dict(sys.modules, {"scanpy": fake_scanpy(adata)}):
            multiome_qc.qc_atac(make_args(tmp, **arg_overrides))
        return adata

    def test_thresholds_mark_failing_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            adata = self.run_qc_atac(
                tmp,
                totals=[2000.0, 500.0, 1500.0],
                nnzs=[600, 700, 100],
            )
            summary = read_summary(tmp)

        # cell0 passes; cell1 fails min_counts; cell2 fails min_peaks
        self.assertEqual(list(adata.obs["atac_pass"]), [True, False, False])
        self.assertIsNotNone(adata.written_to)
        self.assertEqual(summary["n_atac_before_qc"], 3)
        self.assertEqual(summary["n_atac_pass"], 1)
        self.assertEqual(
            summary["atac_matrix_thresholds"],
            {"atac_min_counts": 1000, "atac_min_peaks": 500},
        )

    def test_all_cells_failing_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_qc_atac(tmp, totals=[10.0, 20.0], nnzs=[5, 5])

    def test_zero_thresholds_disable_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            adata = self.run_qc_atac(
                tmp,
                totals=[10.0, 20.0],
                nnzs=[5, 5],
                atac_min_counts=0,
                atac_min_peaks=0,
            )
            summary = read_summary(tmp)

        self.assertEqual(list(adata.obs["atac_pass"]), [True, True])
        self.assertEqual(summary["n_atac_pass"], 2)


if __name__ == "__main__":
    unittest.main()
