"""scatac-peak-matrix QC invariants.

1. embed-cluster must not overwrite the delivery matrix: raw counts stay in
   ``X`` while cluster labels/embeddings are attached as annotations.
2. ``min_cells_per_peak`` counts peak prevalence among kept cells only, so the
   in-memory path matches the backed path.
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
from scipy import sparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

peak_matrix = import_script("scatac_peak_matrix")


class FakeAnnData:
    def __init__(self, X, obs=None):
        self.X = np.array(X, dtype=float)
        self.n_obs, self.n_vars = self.X.shape
        self.obs = (
            obs
            if obs is not None
            else pd.DataFrame(index=[f"cell{i}" for i in range(self.n_obs)])
        )
        self.obsm: dict = {}
        self.written_to: list[str] = []

    def copy(self) -> "FakeAnnData":
        clone = FakeAnnData(self.X.copy(), self.obs.copy())
        clone.obsm = dict(self.obsm)
        return clone

    def write(self, path) -> None:
        self.written_to.append(str(path))
        Path(path).write_text("h5ad-stub\n", encoding="utf-8")


def fake_scanpy(adata: FakeAnnData) -> types.ModuleType:
    sc = types.ModuleType("scanpy")

    def read(path):
        return adata

    def normalize_total(data, target_sum=None):
        totals = data.X.sum(axis=1, keepdims=True)
        data.X = data.X / np.maximum(totals, 1e-9) * (target_sum or 1.0)

    def log1p(data):
        data.X = np.log1p(data.X)

    def pca(data, n_comps=None):
        n = max(1, min(int(n_comps or 2), data.X.shape[1]))
        data.obsm["X_pca"] = data.X[:, :n].copy()

    def neighbors(data):
        return None

    def umap(data):
        data.obsm["X_umap"] = data.obsm["X_pca"][:, :2].copy()

    def leiden(data, resolution=1.0):
        labels = [str(i % 2) for i in range(data.X.shape[0])]
        data.obs["leiden"] = pd.Series(labels, index=data.obs.index)

    sc.read = read
    sc.pp = types.SimpleNamespace(
        normalize_total=normalize_total, log1p=log1p, neighbors=neighbors
    )
    sc.tl = types.SimpleNamespace(pca=pca, umap=umap, leiden=leiden)
    return sc


class EmbedClusterPreservesCountsTests(unittest.TestCase):
    COUNTS = [
        [10.0, 0.0, 5.0],
        [0.0, 3.0, 3.0],
        [2.0, 2.0, 2.0],
        [8.0, 1.0, 0.0],
    ]

    def prepare(self, tmp: str) -> tuple[Namespace, FakeAnnData]:
        out = Path(tmp) / "processed" / "ds"
        out.mkdir(parents=True)
        (out / "peak_matrix.h5ad").write_text("h5ad-stub\n", encoding="utf-8")
        (out / "qc_summary.json").write_text(
            json.dumps(
                {
                    "dataset_id": "ds",
                    "genome_build": "GRCh38",
                    "qc_mode": "full",
                    "filter_thresholds": {"min_peaks": 1, "min_counts": 1, "min_cells_per_peak": 1},
                }
            ),
            encoding="utf-8",
        )
        args = Namespace(
            results_root=str(tmp),
            dataset_id="ds",
            leiden_res=1.0,
            skip_embed_cluster=False,
            force_embed_cluster=False,
        )
        return args, FakeAnnData(self.COUNTS)

    def test_annotation_helper_never_mutates_input(self) -> None:
        adata = FakeAnnData(self.COUNTS)
        original = adata.X.copy()
        work = peak_matrix._embed_cluster_annotations(
            fake_scanpy(adata), adata, n_comps=2, leiden_res=1.0
        )

        np.testing.assert_array_equal(adata.X, original)
        self.assertFalse(np.array_equal(work.X, original))
        self.assertIn("leiden", work.obs)
        self.assertIn("X_umap", work.obsm)

    def test_embed_cluster_stage_writes_raw_counts_with_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, adata = self.prepare(tmp)
            original = adata.X.copy()
            with mock.patch.dict(sys.modules, {"scanpy": fake_scanpy(adata)}):
                peak_matrix.embed_cluster(args)

            summary = json.loads(
                (Path(tmp) / "processed" / "ds" / "qc_summary.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(adata.written_to)
        np.testing.assert_array_equal(adata.X, original)
        self.assertIn("leiden", adata.obs)
        self.assertIn("X_umap", adata.obsm)
        self.assertEqual(summary["n_clusters"], 2)
        self.assertFalse(summary["embed_cluster_skipped"])


class KeptPeakMaskTests(unittest.TestCase):
    def build_matrix(self):
        # cells 0 and 3 are kept; peak0 only lives in removed cells,
        # peak1 lives in both kept cells, peak2 only in one kept cell.
        dense = np.array(
            [
                [0.0, 4.0, 1.0],
                [5.0, 0.0, 0.0],
                [7.0, 0.0, 0.0],
                [0.0, 2.0, 0.0],
            ]
        )
        keep_cells = np.array([True, False, False, True])
        return dense, keep_cells

    def test_prevalence_counts_kept_cells_only_dense(self) -> None:
        dense, keep_cells = self.build_matrix()
        keep_peaks, prevalence = peak_matrix._kept_peak_mask(dense, keep_cells, 2)

        np.testing.assert_array_equal(prevalence, [0, 2, 1])
        np.testing.assert_array_equal(keep_peaks, [False, True, False])

    def test_prevalence_counts_kept_cells_only_sparse(self) -> None:
        dense, keep_cells = self.build_matrix()
        keep_peaks, prevalence = peak_matrix._kept_peak_mask(
            sparse.csr_matrix(dense), keep_cells, 2
        )

        np.testing.assert_array_equal(prevalence, [0, 2, 1])
        np.testing.assert_array_equal(keep_peaks, [False, True, False])


if __name__ == "__main__":
    unittest.main()
