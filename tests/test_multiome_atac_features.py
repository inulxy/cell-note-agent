"""--atac_matrix inputs with mixed 10x ARC feature types must keep only Peaks."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

multiome_qc = import_script("multiome_qc")


class FakeAtacAnnData:
    def __init__(self, var: pd.DataFrame):
        self.var = var
        self.n_obs = 3
        self.n_vars = len(var)
        self.obs_names = [f"cell{i}" for i in range(self.n_obs)]
        self.var_names = list(var.index)

    def __getitem__(self, key):
        rows, cols = key
        assert rows == slice(None)
        mask = np.asarray(cols, dtype=bool)
        return FakeAtacAnnData(self.var[mask])

    def copy(self) -> "FakeAtacAnnData":
        return FakeAtacAnnData(self.var.copy())


def make_var(feature_types: list[str] | None) -> pd.DataFrame:
    index = [f"f{i}" for i in range(len(feature_types or ["x"] * 3))]
    if feature_types is None:
        return pd.DataFrame(index=index)
    return pd.DataFrame({"feature_types": feature_types}, index=index)


class SubsetAtacToPeakFeaturesTests(unittest.TestCase):
    def test_mixed_arc_matrix_keeps_only_peaks(self) -> None:
        adata = FakeAtacAnnData(
            make_var(["Gene Expression", "Peaks", "Gene Expression", "Peaks"])
        )
        subset, n_removed = multiome_qc._subset_atac_to_peak_features(adata)

        self.assertEqual(n_removed, 2)
        self.assertEqual(set(subset.var["feature_types"]), {"Peaks"})
        self.assertEqual(subset.n_vars, 2)

    def test_pure_peaks_matrix_is_unchanged(self) -> None:
        adata = FakeAtacAnnData(make_var(["Peaks", "Peaks"]))
        subset, n_removed = multiome_qc._subset_atac_to_peak_features(adata)

        self.assertIs(subset, adata)
        self.assertEqual(n_removed, 0)

    def test_matrix_without_feature_types_is_unchanged(self) -> None:
        adata = FakeAtacAnnData(make_var(None))
        subset, n_removed = multiome_qc._subset_atac_to_peak_features(adata)

        self.assertIs(subset, adata)
        self.assertEqual(n_removed, 0)

    def test_gex_only_matrix_is_rejected(self) -> None:
        adata = FakeAtacAnnData(make_var(["Gene Expression", "Gene Expression"]))
        with self.assertRaisesRegex(SystemExit, "no 'Peaks' features"):
            multiome_qc._subset_atac_to_peak_features(adata)


if __name__ == "__main__":
    unittest.main()
