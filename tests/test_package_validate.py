"""package validate must reject cards whose genome build is missing or unknown."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_test_utils import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "package_peak_matrices.py"


def write_dataset(results_root: Path, card_overrides: dict | None = None) -> None:
    out = results_root / "processed" / "ds"
    out.mkdir(parents=True, exist_ok=True)
    for name in ("peak_matrix.h5ad", "peaks.hg38.bed", "barcodes.tsv.gz", "qc_summary.json"):
        (out / name).write_text("fixture\n", encoding="utf-8")
    card = {
        "dataset_id": "ds",
        "deliverable": "grch38_per_dataset_peak_matrix",
        "genome_build": "GRCh38",
        "files": {
            "peak_matrix": "processed/ds/peak_matrix.h5ad",
            "peaks": "processed/ds/peaks.hg38.bed",
            "barcodes": "processed/ds/barcodes.tsv.gz",
            "qc_summary": "processed/ds/qc_summary.json",
        },
    }
    card.update(card_overrides or {})
    (out / "data_card.json").write_text(
        json.dumps(card, ensure_ascii=False), encoding="utf-8"
    )


def run_validate(results_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--stage",
            "validate",
            "--results_root",
            str(results_root),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class ValidateGenomeBuildTests(unittest.TestCase):
    def test_grch38_card_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_dataset(Path(tmp))
            completed = run_validate(Path(tmp))

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_missing_genome_build_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_dataset(Path(tmp), {"genome_build": None})
            completed = run_validate(Path(tmp))

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("genome_build", completed.stdout + completed.stderr)

    def test_embedded_qc_summary_genome_build_is_accepted_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_dataset(
                Path(tmp),
                {"genome_build": None, "qc_summary": {"genome_build": "hg38"}},
            )
            completed = run_validate(Path(tmp))

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_non_grch38_build_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_dataset(Path(tmp), {"genome_build": "hg19"})
            completed = run_validate(Path(tmp))

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("genome_build", completed.stdout + completed.stderr)

    def test_quarantined_failed_directory_is_not_treated_as_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_root = Path(tmp)
            write_dataset(results_root)
            failed = results_root / "processed" / "ds.failed-20260809-103159"
            failed.mkdir(parents=True)
            (failed / "data_card.json").write_text(
                json.dumps({"dataset_id": failed.name, "files": {}}),
                encoding="utf-8",
            )
            completed = run_validate(results_root)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
