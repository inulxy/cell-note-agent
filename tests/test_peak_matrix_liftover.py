"""hg19 -> hg38 liftover at peak-matrix standardize (stage 2.4).

- interval conversion via pyliftover point lookups on start and end-1, with
  same-chrom/same-strand checks, minus-strand reconstruction, and dropping of
  duplicate targets;
- success-rate gate: below --min_liftover_rate the stage must abort;
- summary records liftover provenance and genome_build flips to GRCh38.
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

peak_matrix = import_script("scatac_peak_matrix")


class FakeLiftOver:
    """convert_coordinate returns [(chrom, pos, strand, score)] or []."""

    def __init__(self, chain_path, mapping=None):
        self.chain_path = chain_path
        self.mapping = mapping or {}

    def convert_coordinate(self, chrom, pos):
        return self.mapping.get((chrom, pos), [])


def fake_pyliftover(mapping):
    module = types.ModuleType("pyliftover")
    module.LiftOver = lambda chain: FakeLiftOver(chain, mapping)
    return module


def shift_mapping(peaks, offset=1000, chrom_map=None):
    """Build a plus-strand point mapping shifting every coordinate by offset."""
    mapping = {}
    for chrom, start, end in peaks:
        new_chrom = (chrom_map or {}).get(chrom, chrom)
        mapping[(chrom, start)] = [(new_chrom, start + offset, "+", 1)]
        mapping[(chrom, end - 1)] = [(new_chrom, end - 1 + offset, "+", 1)]
    return mapping


class LiftoverPeaksHelperTests(unittest.TestCase):
    def lift(self, parsed, mapping):
        with mock.patch.dict(sys.modules, {"pyliftover": fake_pyliftover(mapping)}):
            return peak_matrix._liftover_peaks("chain.gz", parsed)

    def test_plus_strand_intervals_shift_and_keep_length(self):
        parsed = [("chr1", 100, 200), ("chr2", 500, 700)]
        lifted, kept_idx, stats = self.lift(parsed, shift_mapping(parsed))
        self.assertEqual(kept_idx, [0, 1])
        self.assertEqual(lifted, [("chr1", 1100, 1200), ("chr2", 1500, 1700)])
        self.assertEqual(stats["n_failed"], 0)

    def test_unmapped_and_cross_chrom_peaks_are_dropped(self):
        parsed = [("chr1", 100, 200), ("chr2", 500, 700), ("chr3", 10, 20)]
        mapping = shift_mapping([parsed[0]])
        # chr2 maps its two ends to different chromosomes -> inconsistent
        mapping[("chr2", 500)] = [("chr2", 1500, "+", 1)]
        mapping[("chr2", 699)] = [("chr5", 1699, "+", 1)]
        # chr3 has no mapping at all
        lifted, kept_idx, stats = self.lift(parsed, mapping)
        self.assertEqual(kept_idx, [0])
        self.assertEqual(stats["n_failed"], 2)

    def test_minus_strand_interval_is_reconstructed(self):
        parsed = [("chr1", 100, 200)]
        mapping = {
            ("chr1", 100): [("chr1", 5199, "-", 1)],
            ("chr1", 199): [("chr1", 5100, "-", 1)],
        }
        lifted, kept_idx, stats = self.lift(parsed, mapping)
        self.assertEqual(kept_idx, [0])
        self.assertEqual(lifted, [("chr1", 5100, 5200)])

    def test_duplicate_targets_are_dropped_as_ambiguous(self):
        parsed = [("chr1", 100, 200), ("chr1", 300, 400)]
        mapping = {
            ("chr1", 100): [("chr1", 1000, "+", 1)],
            ("chr1", 199): [("chr1", 1099, "+", 1)],
            ("chr1", 300): [("chr1", 1000, "+", 1)],
            ("chr1", 399): [("chr1", 1099, "+", 1)],
        }
        lifted, kept_idx, stats = self.lift(parsed, mapping)
        self.assertEqual(kept_idx, [])
        self.assertEqual(stats["n_duplicate_targets"], 2)


class FakeVarNamesX:
    def __init__(self, n_obs, n_vars):
        self.shape = (n_obs, n_vars)
        self.nnz = n_obs * n_vars

    def getnnz(self, axis=None):
        return [self.shape[0]] * self.shape[1]


class FakeStandardizeAd:
    def __init__(self, var_names, n_obs=3):
        self.var_names = list(var_names)
        self.X = FakeVarNamesX(n_obs, len(self.var_names))
        self.n_obs = n_obs
        self.n_vars = len(self.var_names)
        self.written_to = None

    def __getitem__(self, key):
        rows, cols = key
        assert rows == slice(None)
        return FakeStandardizeAd([self.var_names[i] for i in cols], self.n_obs)

    def copy(self):
        return self

    def write(self, path):
        self.written_to = str(path)
        Path(path).write_text("H5AD", encoding="utf-8")


def fake_scanpy(adata):
    sc = types.ModuleType("scanpy")
    sc.read = lambda path: adata
    return sc


def standardize_args(tmp, **overrides):
    defaults = dict(
        matrix="matrix.h5",
        peaks=None,
        genome_build="hg19",
        results_root=str(tmp),
        dataset_id="ds",
        liftover_chain=str(Path(tmp) / "chain.gz"),
        min_liftover_rate=0.95,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


def setup_standardize_workdir(tmp):
    processed = Path(tmp) / "processed" / "ds"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "peak_matrix.h5ad").write_text("stub", encoding="utf-8")
    (processed / "qc_summary.json").write_text(
        json.dumps(
            {
                "qc_mode": "full",
                "filter_thresholds": {"min_peaks": 500},
                "genome_build": "hg19",
            }
        ),
        encoding="utf-8",
    )
    (Path(tmp) / "chain.gz").write_bytes(b"chain")
    return processed


class StandardizeLiftoverTests(unittest.TestCase):
    def run_standardize(self, tmp, *, var_names, mapping, **arg_overrides):
        setup_standardize_workdir(tmp)
        adata = FakeStandardizeAd(var_names)
        modules = {
            "scanpy": fake_scanpy(adata),
            "pyliftover": fake_pyliftover(mapping),
        }
        args = standardize_args(tmp, **arg_overrides)
        with mock.patch.dict(sys.modules, modules):
            peak_matrix.standardize(args)
        return adata

    def test_hg19_matrix_is_lifted_and_delivered_as_grch38(self):
        peaks = [("chr1", 100, 200), ("chr2", 500, 700)]
        var_names = [f"{c}:{s}-{e}" for c, s, e in peaks]
        with tempfile.TemporaryDirectory() as tmp:
            adata = self.run_standardize(
                tmp, var_names=var_names, mapping=shift_mapping(peaks)
            )
            summary = json.loads(
                (Path(tmp) / "processed" / "ds" / "qc_summary.json").read_text()
            )
            bed = (
                Path(tmp) / "processed" / "ds" / "peaks.hg38.bed"
            ).read_text(encoding="utf-8")
            matrix_content = (
                Path(tmp) / "processed" / "ds" / "peak_matrix.h5ad"
            ).read_text(encoding="utf-8")

        # the lifted subset (FakeStandardizeAd.write) replaced the stub file
        self.assertEqual(matrix_content, "H5AD")
        self.assertEqual(summary["genome_build"], "GRCh38")
        self.assertEqual(summary["liftover"]["n_input"], 2)
        self.assertEqual(summary["liftover"]["n_lifted"], 2)
        self.assertEqual(summary["liftover"]["rate"], 1.0)
        self.assertEqual(bed, "chr1\t1100\t1200\nchr2\t1500\t1700\n")

    def test_low_liftover_rate_aborts(self):
        peaks = [("chr1", 100, 200), ("chr2", 500, 700)]
        var_names = [f"{c}:{s}-{e}" for c, s, e in peaks]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                # only chr1 maps -> rate 0.5 < 0.95
                self.run_standardize(
                    tmp, var_names=var_names, mapping=shift_mapping(peaks[:1])
                )

    def test_hg19_without_chain_aborts_with_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_standardize_workdir(tmp)
            args = standardize_args(tmp, liftover_chain=None)
            with self.assertRaises(SystemExit) as ctx:
                peak_matrix.standardize(args)
        self.assertIn("resource-setup", str(ctx.exception))

    def test_unsupported_build_still_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_standardize_workdir(tmp)
            args = standardize_args(tmp, genome_build="mm10")
            with self.assertRaises(SystemExit):
                peak_matrix.standardize(args)


if __name__ == "__main__":
    unittest.main()
