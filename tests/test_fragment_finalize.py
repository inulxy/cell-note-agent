"""fragment finalize must reject incomplete deliverables like multiome does."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import REPO_ROOT, import_script

fragment_qc = import_script("scatac_fragment_qc")
SCRIPT = REPO_ROOT / "scripts" / "scatac_fragment_qc.py"


class FragmentDeliverableChecksTests(unittest.TestCase):
    def make_args(self, tmp: str) -> Namespace:
        return Namespace(results_root=str(tmp), dataset_id="ds", genome_build="GRCh38")

    def write_deliverables(self, tmp: str) -> Path:
        out = Path(tmp) / "processed" / "ds"
        out.mkdir(parents=True, exist_ok=True)
        for name in ("peak_matrix.h5ad", "peaks.hg38.bed", "barcodes.tsv.gz"):
            (out / name).write_text("fixture\n", encoding="utf-8")
        return out

    def test_missing_outputs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "missing required deliverable"):
                fragment_qc._require_fragment_deliverables(self.make_args(tmp), {})

    def test_complete_package_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self.write_deliverables(tmp)
            deliverables = fragment_qc._require_fragment_deliverables(
                self.make_args(tmp), {"genome_build": "GRCh38"}
            )

            self.assertEqual(
                deliverables,
                {
                    "peak_matrix": str(out / "peak_matrix.h5ad"),
                    "peaks": str(out / "peaks.hg38.bed"),
                    "barcodes": str(out / "barcodes.tsv.gz"),
                },
            )

    def test_non_grch38_summary_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_deliverables(tmp)
            with self.assertRaisesRegex(SystemExit, "GRCh38"):
                fragment_qc._require_fragment_deliverables(
                    self.make_args(tmp), {"genome_build": "hg19"}
                )


class FragmentFinalizeStageTests(unittest.TestCase):
    def test_finalize_exits_nonzero_when_deliverables_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--stage",
                    "finalize",
                    "--results_root",
                    tmp,
                    "--dataset_id",
                    "ds",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "missing required deliverable", completed.stdout + completed.stderr
        )


if __name__ == "__main__":
    unittest.main()
