"""--matrix inputs with mixed 10x ARC feature types must keep only Peaks.

10x ARC combined h5 files mix "Gene Expression" and "Peaks" features;
``load`` must subset to Peaks in-memory, and refuse mixed matrices in backed
mode (where subsetting is not implemented) instead of failing later with a
confusing coordinate-parse error.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

peak_matrix = import_script("scatac_peak_matrix")


class FakeAnnData:
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
        return FakeAnnData(self.var[mask])

    def copy(self) -> "FakeAnnData":
        return FakeAnnData(self.var.copy())


def make_var(feature_types: list[str] | None) -> pd.DataFrame:
    index = [f"chr1:{i * 100}-{i * 100 + 50}" for i in range(len(feature_types or ["x"] * 3))]
    if feature_types is None:
        return pd.DataFrame(index=index)
    return pd.DataFrame({"feature_types": feature_types}, index=index)


class SubsetToPeakFeaturesTests(unittest.TestCase):
    def test_mixed_arc_matrix_keeps_only_peaks(self) -> None:
        adata = FakeAnnData(
            make_var(["Gene Expression", "Peaks", "Gene Expression", "Peaks"])
        )
        subset, n_removed = peak_matrix._subset_to_peak_features(adata)

        self.assertEqual(n_removed, 2)
        self.assertEqual(set(subset.var["feature_types"]), {"Peaks"})
        self.assertEqual(subset.n_vars, 2)

    def test_pure_peaks_matrix_is_unchanged(self) -> None:
        adata = FakeAnnData(make_var(["Peaks", "Peaks"]))
        subset, n_removed = peak_matrix._subset_to_peak_features(adata)

        self.assertIs(subset, adata)
        self.assertEqual(n_removed, 0)

    def test_matrix_without_feature_types_is_unchanged(self) -> None:
        adata = FakeAnnData(make_var(None))
        subset, n_removed = peak_matrix._subset_to_peak_features(adata)

        self.assertIs(subset, adata)
        self.assertEqual(n_removed, 0)

    def test_gex_only_matrix_is_rejected(self) -> None:
        adata = FakeAnnData(make_var(["Gene Expression", "Gene Expression"]))
        with self.assertRaisesRegex(SystemExit, "no 'Peaks' features"):
            peak_matrix._subset_to_peak_features(adata)


class BackedPeakOnlyGuardTests(unittest.TestCase):
    def test_mixed_backed_matrix_is_rejected(self) -> None:
        var = make_var(["Gene Expression", "Peaks"])
        with self.assertRaisesRegex(SystemExit, "mixes feature types"):
            peak_matrix._require_peak_only_var(var)

    def test_pure_or_untyped_backed_matrix_passes(self) -> None:
        peak_matrix._require_peak_only_var(make_var(["Peaks", "Peaks"]))
        peak_matrix._require_peak_only_var(make_var(None))


if __name__ == "__main__":
    unittest.main()
