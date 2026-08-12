"""pair-check on the peak-matrix branch must not require snapatac2.

The multiome skill contract pins ``conda env: muon`` (scanpy, no snapatac2).
Only the fragments branch needs snapatac2; importing it unconditionally makes
the executable peak-matrix branch fail in its documented environment.
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

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

multiome_qc = import_script("multiome_qc")


class FakeAnnData:
    def __init__(self, var: pd.DataFrame, obs_names: list[str]):
        self.var = var
        self.obs_names = list(obs_names)
        self.n_obs = len(self.obs_names)
        self.n_vars = len(var)
        self.var_names = list(var.index)

    def var_names_make_unique(self) -> None:
        pass

    def __getitem__(self, key):
        rows, cols = key
        assert rows == slice(None)
        mask = np.asarray(cols, dtype=bool)
        return FakeAnnData(self.var[mask], self.obs_names)

    def copy(self) -> "FakeAnnData":
        return FakeAnnData(self.var.copy(), list(self.obs_names))

    def write(self, path) -> None:
        Path(path).write_text("h5ad-stub\n", encoding="utf-8")


def make_mixed_atac() -> FakeAnnData:
    var = pd.DataFrame(
        {"feature_types": ["Gene Expression", "Peaks", "Peaks"]},
        index=["GENE1", "chr1:100-600", "chr2:50-550"],
    )
    return FakeAnnData(var, ["AAA-1", "BBB-1"])


def make_rna() -> FakeAnnData:
    var = pd.DataFrame(index=["GENE1", "GENE2"])
    return FakeAnnData(var, ["AAA-1", "CCC-1"])


def fake_scanpy() -> types.ModuleType:
    sc = types.ModuleType("scanpy")

    def read_10x_h5(path, gex_only=True):
        return make_rna() if gex_only else make_mixed_atac()

    sc.read_10x_h5 = read_10x_h5
    sc.read = lambda path: make_rna()
    return sc


class PairCheckMatrixBranchTests(unittest.TestCase):
    def test_matrix_branch_runs_without_snapatac2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            matrix = Path(tmp) / "arc.h5"
            matrix.write_text("h5-stub\n", encoding="utf-8")
            args = Namespace(
                rna=str(matrix),
                atac_fragments=None,
                atac_matrix=str(matrix),
                peaks=None,
                genome_build="GRCh38",
                results_root=tmp,
                dataset_id="ds",
                min_pair_overlap=0.5,
                import_min_fragments=200,
            )
            # snapatac2 absent: importing it raises ImportError.
            with mock.patch.dict(
                sys.modules, {"scanpy": fake_scanpy(), "snapatac2": None}
            ):
                multiome_qc.pair_check(args)

            summary = json.loads(
                (Path(tmp) / "processed" / "ds" / "qc_summary.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(summary["atac_input_type"], "peak_matrix")
        self.assertEqual(summary["n_atac_features"], 2)
        self.assertEqual(summary["n_atac_nonpeak_features_dropped"], 1)
        self.assertEqual(summary["n_shared_barcodes"], 1)


if __name__ == "__main__":
    unittest.main()
