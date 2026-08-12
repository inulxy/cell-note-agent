"""Barcode normalization must report collisions and exclude them from pairing.

``_normalize_barcode`` strips -1/-2/... suffixes. Two originals collapsing to
the same normalized key (e.g. AAA-1 and AAA-2) used to silently overwrite each
other in the norm->original dict, which can mispair cells. Collisions must be
detected, excluded from pairing, and reported in qc_summary.
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


class NormalizeBarcodeMapTests(unittest.TestCase):
    def test_collision_groups_are_separated_from_unique_mapping(self):
        mapping, collisions = multiome_qc._normalize_barcode_map(
            ["AAA-1", "AAA-2", "BBB-1"]
        )
        self.assertEqual(mapping, {"BBB": "BBB-1"})
        self.assertEqual(collisions, {"AAA": ["AAA-1", "AAA-2"]})

    def test_no_collisions(self):
        mapping, collisions = multiome_qc._normalize_barcode_map(["AAA-1", "BBB-1"])
        self.assertEqual(mapping, {"AAA": "AAA-1", "BBB": "BBB-1"})
        self.assertEqual(collisions, {})


class FakeRna:
    def __init__(self, obs_names):
        self.obs_names = list(obs_names)
        self.obs = {}
        self.n_obs = len(self.obs_names)
        self.n_vars = 3

    def var_names_make_unique(self):
        pass

    def write(self, path):
        Path(path).write_text("RNA", encoding="utf-8")


class FakeAtacMatrix:
    def __init__(self, obs_names):
        self.obs_names = list(obs_names)
        self.obs = {}
        self.n_obs = len(self.obs_names)
        self.n_vars = 2
        self.var = None
        self.var_names = ["chr1:100-200", "chr2:300-400"]

    def write(self, path):
        Path(path).write_text("ATAC", encoding="utf-8")


def fake_scanpy(rna, atac):
    sc = types.ModuleType("scanpy")

    def read(path):
        return rna if "rna" in str(path) else atac

    sc.read = read
    return sc


def pair_check_args(tmp: str) -> Namespace:
    rna_path = Path(tmp) / "rna.h5ad"
    atac_path = Path(tmp) / "atac.h5ad"
    rna_path.write_text("stub", encoding="utf-8")
    atac_path.write_text("stub", encoding="utf-8")
    return Namespace(
        rna=str(rna_path),
        atac_fragments=None,
        atac_matrix=str(atac_path),
        peaks=None,
        genome_build="GRCh38",
        results_root=str(tmp),
        dataset_id="ds",
        min_pair_overlap=0.0,
        import_min_fragments=200,
    )


class PairCheckCollisionTests(unittest.TestCase):
    def test_pair_check_reports_and_excludes_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            rna = FakeRna(["AAA-1", "AAA-2", "BBB-1"])
            atac = FakeAtacMatrix(["AAA-1", "BBB-1"])
            with mock.patch.dict(sys.modules, {"scanpy": fake_scanpy(rna, atac)}):
                multiome_qc.pair_check(pair_check_args(tmp))
            summary = json.loads(
                (Path(tmp) / "processed" / "ds" / "qc_summary.json").read_text()
            )

        # AAA collides on the RNA side -> only BBB can be paired
        self.assertEqual(summary["n_shared_barcodes"], 1)
        self.assertEqual(
            summary["barcode_collisions"],
            {
                "rna": {"groups": 1, "barcodes": 2},
                "atac": {"groups": 0, "barcodes": 0},
            },
        )
        # stage-2.1 provenance: software versions captured at pair-check
        self.assertIn("python", summary["software_versions"])
        self.assertIn("scanpy", summary["software_versions"])


def setup_fragments_workdir(tmp: str) -> Path:
    processed = Path(tmp) / "processed" / "ds"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "rna_support.h5ad").write_text("stub", encoding="utf-8")
    (processed / "atac_qc.h5ad").write_text("stub", encoding="utf-8")
    (processed / "qc_summary.json").write_text(
        json.dumps({"atac_input_type": "fragments", "genome_build": "GRCh38"}),
        encoding="utf-8",
    )
    return processed


class IntersectCollisionTests(unittest.TestCase):
    def test_intersect_excludes_atac_side_collisions(self):
        import test_multiome_fragments_branch as frag

        with tempfile.TemporaryDirectory() as tmp:
            setup_fragments_workdir(tmp)
            rna_names = ["AAA-1", "BBB-1"]
            atac_names = ["AAA-1", "AAA-2", "BBB-1"]
            record = {
                "atac": frag.FakeBackedAtac(atac_names),
                "mem": frag.FakeMemAtac(atac_names),
            }
            modules = {
                "snapatac2": frag.fake_snap(record),
                "scanpy": frag.fake_scanpy(frag.FakeRna(rna_names)),
                "hdf5plugin": types.ModuleType("hdf5plugin"),
            }
            args = Namespace(
                rna="rna.h5",
                atac_fragments="frags.tsv.gz",
                atac_matrix=None,
                peaks=None,
                genome_build="GRCh38",
                results_root=str(tmp),
                dataset_id="ds",
                min_pair_overlap=0.5,
            )
            with mock.patch.dict(sys.modules, modules):
                multiome_qc.intersect(args)
            with gzip.open(
                Path(tmp) / "processed" / "ds" / "barcodes.tsv.gz", "rt"
            ) as fh:
                barcodes = fh.read().split()

        # AAA collides on the ATAC side -> only BBB-1 is delivered
        self.assertEqual(barcodes, ["BBB-1"])
        self.assertEqual(record["make_peak_matrix_n_obs"], 1)


if __name__ == "__main__":
    unittest.main()
