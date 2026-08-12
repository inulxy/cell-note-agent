from __future__ import annotations

import unittest
from pathlib import Path

from cell_note_agent.agent_cli import (
    AgentConfig,
    canonical_analysis_commands,
    deterministic_analysis_plan,
)
from cell_note_agent.pi_bridge import peak_matrix_plan


class PeakMatrixPlanTests(unittest.TestCase):
    def test_peak_matrix_uses_original_supported_input_for_every_qc_stage(self) -> None:
        plan = peak_matrix_plan(
            input_kind="peak_matrix",
            dataset_id="pbmc",
            results_root="runs/pbmc",
            input_path="/data/pbmc.h5ad",
            peaks_path="/data/pbmc.bed",
        )

        qc = [item for item in plan if item.skill == "scatac-peak-matrix"]

        self.assertEqual(
            [item.stage for item in qc],
            ["load", "profile", "filter", "standardize", "embed-cluster", "finalize"],
        )
        self.assertTrue(all("--matrix /data/pbmc.h5ad" in item.command for item in qc))
        self.assertTrue(all("cell_x_peak.npz" not in item.command for item in plan))

    def test_fragments_route_to_fragment_qc_stages(self) -> None:
        plan = peak_matrix_plan(
            input_kind="fragments",
            dataset_id="pbmc",
            results_root="runs/pbmc",
            input_path="/data/fragments.tsv.gz",
        )

        qc = [item for item in plan if item.skill == "scatac-fragment-qc"]

        self.assertEqual(
            [item.stage for item in qc],
            [
                "import",
                "pre-filter",
                "filter",
                "embed",
                "cluster",
                "doublet",
                "call-peaks",
                "make-peak-matrix",
                "finalize",
            ],
        )
        self.assertTrue(all("--fragments /data/fragments.tsv.gz" in item.command for item in qc))
        self.assertTrue(all("--blacklist_bed runs/pbmc/reference/hg38-blacklist.v2.bed" in item.command for item in qc))
        self.assertEqual(
            [item.stage for item in plan if item.skill == "resource-setup"],
            ["plan", "fetch", "verify"],
        )

    def test_agent_fragment_collection_uses_same_skill_with_sample_peak_calling(self) -> None:
        context = {
            "input_kind": "fragments",
            "input_mode": "collection",
            "sample_count": 28,
            "fragments": "/data/Li2023b/fragments_standardized",
            "results_root": "runs/li2023b",
            "genome_build": "GRCh38",
            "dataset_id": "Li2023b-brain_tissue",
        }
        plan = deterministic_analysis_plan(context)
        commands = canonical_analysis_commands(
            AgentConfig(repo_root=Path("."), run_root=Path("runs/test"), processing_python="python"),
            context,
            plan,
        )
        fragment_commands = [command for command in commands if "scripts/scatac_fragment_qc.py" in command]

        self.assertEqual(len(fragment_commands), 9)
        self.assertTrue(all("/data/Li2023b/fragments_standardized" in command for command in fragment_commands))
        self.assertTrue(all(command[command.index("--peak_calling") + 1] == "sample" for command in fragment_commands))

    def test_multiome_route_to_multiome_qc_stages(self) -> None:
        plan = peak_matrix_plan(
            input_kind="multiome",
            dataset_id="pbmc_multiome",
            results_root="runs/pbmc-multiome",
            input_path="/data/atac_peak_matrix.h5ad",
            peaks_path="/data/peaks.bed",
            rna_path="/data/rna.h5ad",
        )

        qc = [item for item in plan if item.skill == "multiome-qc"]

        self.assertEqual(
            [item.stage for item in qc],
            ["pair-check", "qc-rna", "qc-atac", "intersect", "finalize"],
        )
        self.assertTrue(all("--rna /data/rna.h5ad" in item.command for item in qc))
        self.assertTrue(
            all("--atac_matrix /data/atac_peak_matrix.h5ad" in item.command for item in qc)
        )

    def test_rna_reference_never_schedules_atac_processing_or_handoff(self) -> None:
        plan = peak_matrix_plan(
            input_kind="rna_reference",
            dataset_id="pbmc_rna",
            results_root="runs/pbmc-rna",
            rna_path="/data/rna.h5ad",
        )

        self.assertEqual([item.skill for item in plan], ["normalize-to-peak-matrix"])
        self.assertEqual([item.stage for item in plan], ["plan"])

    def test_multiome_fragments_route_is_executable(self) -> None:
        plan = peak_matrix_plan(
            input_kind="multiome",
            dataset_id="pbmc_multiome",
            results_root="runs/pbmc-multiome",
            input_path="/data/fragments.tsv.gz",
            rna_path="/data/rna.h5ad",
        )

        qc = [item for item in plan if item.skill == "multiome-qc"]
        self.assertEqual([item.stage for item in qc], ["pair-check", "qc-rna", "qc-atac", "intersect", "finalize"])
        self.assertTrue(all("--atac_fragments /data/fragments.tsv.gz" in item.command for item in qc))
        self.assertTrue(all("scripts/multiome_qc.py" in item.command for item in qc))

    def test_agent_builds_multiome_fragments_commands(self) -> None:
        context = {
            "input_kind": "multiome",
            "fragments": "/data/fragments.tsv.gz",
            "rna": "/data/rna.h5ad",
            "matrix": "",
        }
        plan = deterministic_analysis_plan(context)

        self.assertEqual(plan["action"], "run_analysis")
        commands = canonical_analysis_commands(
            AgentConfig(
                repo_root=Path("."),
                run_root=Path("runs/test"),
                processing_python="python",
            ),
            context,
            plan,
        )
        self.assertTrue(any("scripts/multiome_qc.py" in command and "--atac_fragments" in command for command in commands))
        self.assertTrue(any("scripts/package_peak_matrices.py" in command for command in commands))

    def test_hg19_peak_matrix_plan_prepares_liftover(self) -> None:
        plan = peak_matrix_plan(
            input_kind="peak_matrix",
            dataset_id="hg19_ds",
            results_root="runs/hg19",
            input_path="/data/hg19.h5ad",
            genome_build="hg19",
        )

        resources = [item for item in plan if item.skill == "resource-setup"]
        qc = [item for item in plan if item.skill == "scatac-peak-matrix"]
        self.assertEqual([item.stage for item in resources], ["plan", "fetch", "verify"])
        self.assertTrue(all("--include_liftover" in item.command for item in resources))
        self.assertTrue(all("--liftover_chain runs/hg19/reference/hg19ToHg38.over.chain.gz" in item.command for item in qc))


if __name__ == "__main__":
    unittest.main()
