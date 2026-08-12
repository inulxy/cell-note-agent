from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "multiome_qc.py"


class MultiomeFinalizeTests(unittest.TestCase):
    def run_finalize(self, results_root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--stage",
                "finalize",
                "--rna",
                "unused-at-finalize",
                "--results_root",
                str(results_root),
                "--dataset_id",
                "multiome",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_finalize_rejects_missing_atac_peak_matrix_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = self.run_finalize(Path(tmp))

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("missing required ATAC deliverable", completed.stdout + completed.stderr)

    def test_finalize_accepts_complete_peak_matrix_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_root = Path(tmp)
            output = results_root / "processed" / "multiome"
            output.mkdir(parents=True)
            matrix = output / "peak_matrix.h5ad"
            peaks = output / "peaks.hg38.bed"
            barcodes = output / "barcodes.tsv.gz"
            for path in (matrix, peaks, barcodes):
                path.write_text("fixture\n", encoding="utf-8")
            (output / "qc_summary.json").write_text(
                json.dumps(
                    {
                        "dataset_id": "multiome",
                        "genome_build": "GRCh38",
                        "peak_matrix": str(matrix),
                        "peaks_file": str(peaks),
                        "barcodes_file": str(barcodes),
                        "n_paired_pass": 2,
                    }
                ),
                encoding="utf-8",
            )

            completed = self.run_finalize(results_root)

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue((output / "data_card.json").is_file())


if __name__ == "__main__":
    unittest.main()
