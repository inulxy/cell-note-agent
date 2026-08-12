"""Matrix quality metrics for the peak-matrix deliverable (stage 2.3).

standardize must record coordinate validity, sparsity and cells-per-peak in
qc_summary.matrix_quality alongside the post-filter shape.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

peak_matrix = import_script("scatac_peak_matrix")


class FakeSparseX:
    def __init__(self, shape, nnz, cells_per_peak):
        self.shape = shape
        self.nnz = nnz
        self._cpp = list(cells_per_peak)

    def getnnz(self, axis=None):
        assert axis == 0
        return list(self._cpp)


class MatrixQualityMetricsTests(unittest.TestCase):
    def test_metrics_for_sparse_matrix_with_valid_coordinates(self):
        X = FakeSparseX(shape=(4, 3), nnz=6, cells_per_peak=[3, 2, 1])
        metrics = peak_matrix._matrix_quality_metrics(
            X, ["chr1:100-200", "chr2:300-400", "chrX:1-50"]
        )
        self.assertEqual(metrics["shape"], [4, 3])
        self.assertEqual(metrics["nnz"], 6)
        self.assertAlmostEqual(metrics["density"], 6 / 12)
        self.assertEqual(metrics["cells_per_peak_median"], 2)
        self.assertEqual(
            metrics["peak_coordinate_validity"],
            {"n_valid": 3, "n_total": 3, "fraction_valid": 1.0},
        )

    def test_invalid_coordinates_lower_the_validity_fraction(self):
        X = FakeSparseX(shape=(2, 4), nnz=4, cells_per_peak=[1, 1, 1, 1])
        metrics = peak_matrix._matrix_quality_metrics(
            X,
            [
                "chr1:100-200",     # valid
                "GENE_SYMBOL",      # unparsable
                "chr2:500-400",     # end <= start
                "chr3:-5-10",       # negative start -> unparsable/invalid
            ],
        )
        validity = metrics["peak_coordinate_validity"]
        self.assertEqual(validity["n_total"], 4)
        self.assertEqual(validity["n_valid"], 1)
        self.assertAlmostEqual(validity["fraction_valid"], 0.25)


if __name__ == "__main__":
    unittest.main()
