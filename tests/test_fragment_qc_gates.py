"""blacklist-fraction and FRiP gates must be truly enforced, never silently skipped.

- filter stage: --blacklist_bed enables the max_blacklist_frac gate via
  snap.metrics.frip(regions={"blacklist_frac": bed}); without the bed the gate
  must stay in thresholds_declared_not_applied with an explicit reason.
- make-peak-matrix stage: min_frip is applied once peaks exist, using the same
  metrics.frip mechanism; the deliverable matrix must only contain passing cells.
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

fragment_qc = import_script("scatac_fragment_qc")


class FakeGateData:
    def __init__(self, n_obs: int):
        self.obs = {}
        self.obs_names = [f"BC{i}" for i in range(n_obs)]
        self.n_obs = n_obs
        self.subset_calls: list[list] = []
        self.closed = False

    def subset(self, obs_indices):
        idx = list(obs_indices)
        self.subset_calls.append(idx)
        if idx and isinstance(idx[0], bool):
            idx = [i for i, keep in enumerate(idx) if keep]
        self.obs = {k: [v[i] for i in idx] for k, v in self.obs.items()}
        self.obs_names = [self.obs_names[i] for i in idx]
        self.n_obs = len(idx)

    def close(self):
        self.closed = True


class FakePeakMatrix:
    def __init__(self, n_obs: int):
        self.n_obs = n_obs

    def write(self, path):
        Path(path).write_text("PEAK_MATRIX", encoding="utf-8")

    def close(self):
        pass


def fake_snap(record, *, frip_values=None):
    snap = types.ModuleType("snapatac2")

    def read(path):
        return record["data"]

    def filter_cells(data, **kwargs):
        record["filter_cells_kwargs"] = kwargs

    def frip(data, regions, inplace=True, **kwargs):
        record.setdefault("frip_calls", []).append(dict(regions))
        for key in regions:
            data.obs[key] = list(frip_values)[: data.n_obs]

    def make_peak_matrix(data, peak_file=None, **kwargs):
        record["make_peak_matrix_n_obs"] = data.n_obs
        record["peak_file"] = peak_file
        return FakePeakMatrix(data.n_obs)

    snap.read = read
    snap.pp = types.SimpleNamespace(
        filter_cells=filter_cells, make_peak_matrix=make_peak_matrix
    )
    snap.metrics = types.SimpleNamespace(frip=frip)
    snap.genome = types.SimpleNamespace(hg38="G", hg19="G", mm10="G", mm39="G")
    return snap


def base_args(tmp: str, **overrides) -> Namespace:
    defaults = dict(
        fragments=None,
        peaks=None,
        genome_build="GRCh38",
        results_root=str(tmp),
        dataset_id="ds",
        min_fragments=1000,
        max_fragments=100000,
        min_tsse=4.0,
        max_blacklist_frac=0.05,
        min_frip=0.10,
        blacklist_bed=None,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


def setup_workdir(tmp: str, summary: dict | None = None) -> Path:
    processed = Path(tmp) / "processed" / "ds"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "atac_qc.h5ad").write_text("stub", encoding="utf-8")
    if summary is not None:
        (processed / "qc_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
    return processed


def read_summary(tmp: str) -> dict:
    return json.loads(
        (Path(tmp) / "processed" / "ds" / "qc_summary.json").read_text()
    )


class FilterBlacklistGateTests(unittest.TestCase):
    def run_filter(self, tmp, *, fracs, blacklist_bed, n_obs=5, **arg_overrides):
        setup_workdir(tmp)
        record = {"data": FakeGateData(n_obs)}
        snap = fake_snap(record, frip_values=fracs)
        args = base_args(tmp, blacklist_bed=blacklist_bed, **arg_overrides)
        with mock.patch.dict(sys.modules, {"snapatac2": snap}):
            fragment_qc.filter_cells(args)
        return record

    def test_blacklist_gate_filters_cells_and_records_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            bed = Path(tmp) / "blacklist.bed"
            bed.write_text("chr1\t0\t100\n", encoding="utf-8")
            record = self.run_filter(
                tmp,
                fracs=[0.01, 0.02, 0.10, 0.03, 0.20],
                blacklist_bed=str(bed),
            )
            summary = read_summary(tmp)

        self.assertEqual(record["frip_calls"], [{"blacklist_frac": str(bed)}])
        self.assertEqual(record["data"].n_obs, 3)
        self.assertEqual(summary["n_cells_removed_blacklist"], 2)
        self.assertEqual(
            summary["filter_thresholds"]["max_blacklist_frac"], 0.05
        )
        self.assertNotIn(
            "max_blacklist_frac", summary["thresholds_declared_not_applied"]
        )
        # reference-asset provenance for the enforced gate
        self.assertEqual(summary["blacklist_bed"], str(bed))
        self.assertRegex(summary["blacklist_bed_sha256"], r"^[0-9a-f]{64}$")

    def test_no_blacklist_bed_keeps_gate_declared_not_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_filter(tmp, fracs=[], blacklist_bed=None)
            summary = read_summary(tmp)

        self.assertNotIn("frip_calls", record)
        self.assertEqual(record["data"].n_obs, 5)
        self.assertEqual(summary["n_cells_removed_blacklist"], 0)
        declared = summary["thresholds_declared_not_applied"]
        self.assertIn("max_blacklist_frac", declared)
        self.assertIn("reason", declared["max_blacklist_frac"])
        self.assertNotIn("max_blacklist_frac", summary["filter_thresholds"])

    def test_missing_blacklist_bed_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_filter(
                    tmp,
                    fracs=[],
                    blacklist_bed=str(Path(tmp) / "nonexistent.bed"),
                )

    def test_min_frip_recorded_as_deferred_at_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_filter(tmp, fracs=[], blacklist_bed=None)
            summary = read_summary(tmp)

        declared = summary["thresholds_declared_not_applied"]
        self.assertIn("min_frip", declared)
        self.assertIn("make-peak-matrix", declared["min_frip"]["reason"])


class MakePeakMatrixFripGateTests(unittest.TestCase):
    PRIOR_SUMMARY = {
        "filter_thresholds": {"min_fragments": 1000},
        "thresholds_declared_not_applied": {
            "min_frip": {"value": 0.10, "reason": "deferred"},
        },
    }

    def run_mpm(self, tmp, *, fracs, n_obs=4, **arg_overrides):
        processed = setup_workdir(tmp, summary=self.PRIOR_SUMMARY)
        (processed / "peaks.hg38.bed").write_text(
            "chr1\t100\t600\n", encoding="utf-8"
        )
        record = {"data": FakeGateData(n_obs)}
        snap = fake_snap(record, frip_values=fracs)
        args = base_args(tmp, **arg_overrides)
        with mock.patch.dict(sys.modules, {"snapatac2": snap}):
            fragment_qc.make_peak_matrix(args)
        return record

    def test_frip_gate_filters_low_frip_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_mpm(tmp, fracs=[0.5, 0.05, 0.3, 0.02])
            summary = read_summary(tmp)
            barcodes_path = Path(tmp) / "processed" / "ds" / "barcodes.tsv.gz"
            self.assertTrue(barcodes_path.exists())

        self.assertEqual(record["make_peak_matrix_n_obs"], 2)
        self.assertEqual(summary["n_cells_removed_frip"], 2)
        self.assertAlmostEqual(summary["frip_median"], 0.175)
        self.assertEqual(summary["filter_thresholds"]["min_frip"], 0.10)
        self.assertNotIn(
            "min_frip", summary["thresholds_declared_not_applied"]
        )

    def test_frip_gate_disabled_when_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.run_mpm(tmp, fracs=[0.5, 0.05], n_obs=2, min_frip=0.0)
            summary = read_summary(tmp)

        self.assertNotIn("frip_calls", record)
        self.assertEqual(record["make_peak_matrix_n_obs"], 2)
        self.assertEqual(summary.get("n_cells_removed_frip", 0), 0)

    def test_frip_gate_removing_all_cells_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_mpm(tmp, fracs=[0.01, 0.02, 0.03, 0.04])
            matrix = Path(tmp) / "processed" / "ds" / "peak_matrix.h5ad"
            self.assertFalse(matrix.exists())


if __name__ == "__main__":
    unittest.main()
