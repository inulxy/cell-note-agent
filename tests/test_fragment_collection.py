from __future__ import annotations

import csv
import gzip
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import import_script

detect_input = import_script("detect_input")
fragment_qc = import_script("scatac_fragment_qc")


def write_fragment(path: Path, barcode: str = "AAAC-1") -> None:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as handle:
        handle.write(f"chr1\t100\t150\t{barcode}\t1\n")


class FragmentCollectionDetectionTests(unittest.TestCase):
    def test_directory_is_one_collection_with_paired_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for sample in ("sample-a", "sample-b"):
                write_fragment(root / f"{sample}.tsv.gz", barcode=f"{sample}-AAAC")
                (root / f"{sample}-metadata.csv").write_text("cell_barcode\nAAAC\n", encoding="utf-8")

            detected = detect_input.detect(root, max_files=50, genome_build="GRCh38")

        self.assertEqual(detected["input_kind"], "fragments")
        self.assertEqual(detected["input_mode"], "collection")
        self.assertEqual(detected["metadata"]["sample_count"], 2)
        self.assertEqual(len(detected["files"]["fragment_files"]), 2)
        self.assertEqual(len(detected["files"]["metadata_files"]), 2)

    def test_single_generic_tsv_gz_remains_single_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample-a.tsv.gz"
            write_fragment(path)
            detected = detect_input.detect(path, max_files=50, genome_build="GRCh38")

        self.assertEqual(detected["input_kind"], "fragments")
        self.assertEqual(detected["input_mode"], "single")
        self.assertEqual(detected["files"]["fragments"], str(path.resolve()))


class FragmentCollectionManifestTests(unittest.TestCase):
    def test_directory_and_manifest_resolve_to_same_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "sample-a.tsv.gz"
            second = root / "sample-b.tsv.gz"
            write_fragment(first)
            write_fragment(second)
            manifest = root / "fragments.csv"
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["sample_id", "fragments_path"])
                writer.writeheader()
                writer.writerow({"sample_id": "A", "fragments_path": first.name})
                writer.writerow({"sample_id": "B", "fragments_path": second.name})

            directory_args = Namespace(fragments=str(root), results_root=tmp, dataset_id="ds")
            manifest_args = Namespace(fragments=str(manifest), results_root=tmp, dataset_id="ds")
            directory_entries = fragment_qc._fragment_entries(directory_args)
            manifest_entries = fragment_qc._fragment_entries(manifest_args)
            self.assertEqual(len(directory_entries), 2)
            self.assertEqual([item["sample_id"] for item in manifest_entries], ["A", "B"])
            self.assertTrue(fragment_qc._is_collection(manifest_args))
            self.assertTrue(fragment_qc._h5ad_path(manifest_args).endswith("atac_qc.h5ad"))

    def test_collection_qc_columns_are_synchronized_to_outer_obs(self) -> None:
        class FakeStacked:
            obs = {
                "n_fragment": [10, 20],
                "frac_dup": [0.1, 0.2],
                "frac_mito": [0.0, 0.01],
            }

        class FakeDataset:
            n_obs = 2
            obs = {"sample": ["A", "B"], "tsse": [5.0, 6.0]}
            adatas = FakeStacked()

        data = FakeDataset()
        synced = fragment_qc._sync_collection_obs(data)

        self.assertEqual(synced, ["n_fragment", "frac_dup", "frac_mito"])
        self.assertEqual(data.obs["n_fragment"], [10, 20])
        self.assertEqual(fragment_qc._sync_collection_obs(data), [])

    def test_collection_merge_prefixes_unscoped_barcodes_and_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            root.mkdir()
            first = root / "sample-a.tsv.gz"
            second = root / "sample-b.tsv.gz"
            write_fragment(first, barcode="AAAC-1")
            write_fragment(second, barcode="AAAC-1")
            args = Namespace(fragments=str(root), results_root=tmp, dataset_id="ds")
            entries = fragment_qc._fragment_entries(args)

            merged, mode = fragment_qc._prepare_collection_fragments(args, entries)
            merged_again, mode_again = fragment_qc._prepare_collection_fragments(args, entries)
            with gzip.open(merged, "rt", encoding="utf-8") as handle:
                barcodes = [line.split("\t")[3] for line in handle if line.strip()]

        self.assertEqual(mode, "sample_prefix_rewrite")
        self.assertEqual((merged_again, mode_again), (merged, mode))
        self.assertEqual(barcodes, ["sample-a::AAAC-1", "sample-b::AAAC-1"])


if __name__ == "__main__":
    unittest.main()
