"""Promote an integrity-checked crawl run into curation-state candidates."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..events import EventStore


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path.name}:{line_number} is not a JSON object")
            result.append(value)
    return result


class CrawlRunAdapter:
    """Read-only bridge from crawler artifacts to the deterministic harness."""

    run_scope = "integrity_checked_crawl_promotion_for_acquisition_review"
    run_not_claimed = [
        "no biological data file was downloaded",
        "remote checksums have not been recomputed locally",
        "license and genome build remain unresolved unless separately verified",
        "no training-ready matrix was produced",
    ]

    def __init__(self, crawl_run: str | Path) -> None:
        self.root = Path(crawl_run).resolve()
        self.manifest_path = self.root / "crawl_manifest.json"
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"crawl manifest not found: {self.manifest_path}")
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._verify_integrity()
        self.runs = _jsonl(self.root / "ena_run_manifest.jsonl")
        self.files = _jsonl(self.root / "remote_file_candidates.jsonl")
        self.issues = _jsonl(self.root / "remote_file_issues.jsonl")
        graph_path = self.root / "accession_graph.json"
        self.graph = (
            json.loads(graph_path.read_text(encoding="utf-8"))
            if graph_path.exists()
            else {"nodes": [], "edges": []}
        )
        self.probes = _jsonl(self.root / "remote_file_probes.jsonl")

    def _verify_integrity(self) -> None:
        outputs = self.manifest.get("outputs")
        if not isinstance(outputs, dict):
            raise ValueError("crawl manifest has no outputs map")
        for name, descriptor in outputs.items():
            if not isinstance(descriptor, dict):
                raise ValueError(f"invalid output descriptor: {name}")
            relative = Path(str(descriptor.get("path", "")))
            if (
                not relative.name
                or relative.is_absolute()
                or relative.parent != Path(".")
            ):
                raise ValueError(f"unsafe crawl output path: {relative}")
            path = self.root / relative
            if not path.exists():
                raise FileNotFoundError(f"crawl output is missing: {path}")
            expected = str(descriptor.get("sha256", ""))
            observed = _sha256(path)
            if not expected or observed != expected:
                raise ValueError(f"crawl output hash mismatch: {path.name}")

        database = self.root / "crawl_state.sqlite"
        if not database.exists():
            raise FileNotFoundError(f"crawl event database is missing: {database}")
        store = EventStore(database)
        try:
            run_id = str(self.manifest.get("run_id", ""))
            if not run_id or not store.verify_chain(run_id):
                raise ValueError("crawl event chain verification failed")
            completed = [
                event
                for event in store.list_events(run_id)
                if event.event_type == "CrawlCompleted"
            ]
            if len(completed) != 1:
                raise ValueError("crawl completion event is missing or ambiguous")
            event_manifest_hash = str(
                completed[0].payload.get("manifest_sha256", "")
            )
            if event_manifest_hash != _sha256(self.manifest_path):
                raise ValueError("crawl manifest is not bound to its completion event")
        finally:
            store.close()

    def retrieval_spec(self) -> dict[str, Any]:
        species = sorted(
            {
                str(item.get("scientific_name", "")).strip()
                for item in self.runs
                if str(item.get("scientific_name", "")).strip()
            }
        )
        modalities = sorted(
            {
                str(item.get("library_strategy", "")).strip()
                for item in self.runs
                if str(item.get("library_strategy", "")).strip()
            }
        )
        return {
            "species": species,
            "tissues": [],
            "conditions": [],
            "modalities": modalities,
            "pairing_requirement": "unresolved_requires_review",
            "preferred_files": ["fastq"],
            "fallback_files": [],
            "exclude": [],
        }

    def candidates(self) -> list[dict[str, Any]]:
        runs_by_study: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run in self.runs:
            study = str(
                run.get("secondary_study_accession")
                or run.get("study_accession")
                or ""
            )
            if study:
                runs_by_study[study].append(run)

        result = []
        for study in sorted(runs_by_study):
            study_runs = sorted(
                runs_by_study[study],
                key=lambda item: str(item.get("run_accession", "")),
            )
            run_ids = {
                str(item.get("run_accession", "")) for item in study_runs
            }
            files = [
                item for item in self.files
                if str(item.get("run_accession", "")) in run_ids
            ]
            blocking_file_ids = {
                str(item.get("file_id", ""))
                for item in self.issues
                if item.get("severity") == "BLOCK" and item.get("file_id")
            }
            blocking_runs = {
                str(item.get("run_accession", ""))
                for item in self.issues
                if item.get("severity") == "BLOCK" and not item.get("file_id")
            }
            usable_files = [
                item for item in files
                if str(item.get("file_id", "")) not in blocking_file_ids
                and str(item.get("run_accession", "")) not in blocking_runs
            ]
            result.append(
                self._candidate(
                    study, study_runs, usable_files, files, blocking_file_ids, blocking_runs
                )
            )
        return result

    def _candidate(
        self,
        study: str,
        runs: list[dict[str, Any]],
        usable_files: list[dict[str, Any]],
        all_files: list[dict[str, Any]],
        blocking_file_ids: set[str],
        blocking_runs: set[str],
    ) -> dict[str, Any]:
        species = sorted(
            {
                str(item.get("scientific_name", "")).strip()
                for item in runs
                if str(item.get("scientific_name", "")).strip()
            }
        )
        strategies = sorted(
            {
                str(item.get("library_strategy", "")).strip()
                for item in runs
                if str(item.get("library_strategy", "")).strip()
            }
        )
        layouts = sorted(
            {
                str(item.get("library_layout", "")).strip()
                for item in runs
                if str(item.get("library_layout", "")).strip()
            }
        )
        claims = [
            {
                "claim_key": "repository_manifest",
                "expected_value": study,
                "rule": "AUTHORITATIVE_SOURCE",
            }
        ]
        evidence = [
            self._evidence(
                study,
                "repository_manifest",
                study,
                runs[0],
                "ena_read_run_manifest_promotion",
            )
        ]
        required_claims = ["repository_manifest"]
        if len(species) == 1:
            claims.append(
                {
                    "claim_key": "species",
                    "expected_value": species[0],
                    "rule": "AUTHORITATIVE_SOURCE",
                }
            )
            evidence.append(
                self._evidence(
                    study,
                    "species",
                    species[0],
                    runs[0],
                    "ena_scientific_name_promotion",
                )
            )
            required_claims.append("species")

        artifacts = [
            {
                "artifact_id": str(item["file_id"]),
                "role": "fastq",
                "source_uri": str(item["uri"]),
                "size_bytes": int(item.get("size_bytes") or 0),
                "source": "ena",
                "discovered_via": "integrity_checked_crawl_run",
                "checksum": (
                    f"{item.get('checksum_algorithm')}:{item.get('checksum')}"
                    if item.get("checksum")
                    else None
                ),
            }
            for item in sorted(usable_files, key=lambda value: str(value["file_id"]))
        ]
        run_ids = {str(item.get("run_accession", "")) for item in runs}
        file_ids = {str(item.get("file_id", "")) for item in all_files}
        graph_nodes, graph_edges = self._graph_subset(
            {study, *run_ids, *file_ids}
        )
        probed_ids = {
            str(item.get("file_id", ""))
            for item in self.probes
            if item.get("reachable") is True
        }
        return {
            "dataset_id": study,
            "claims": claims,
            "initial_evidence": evidence,
            "initial_artifacts": artifacts,
            "required_roles": ["fastq"],
            "required_verified_claims": required_claims,
            "metadata": {
                "source_crawl_run": str(self.root),
                "source_crawl_run_id": self.manifest.get("run_id"),
                "source_crawl_manifest_sha256": _sha256(self.manifest_path),
                "study_accession": study,
                "species": species,
                "library_strategies": strategies,
                "library_layouts": layouts,
                "run_count": len(runs),
                "remote_file_count": len(all_files),
                "usable_remote_file_count": len(usable_files),
                "reachable_probe_count": len(file_ids & probed_ids),
                "blocked_file_ids": sorted(blocking_file_ids & file_ids),
                "blocked_run_accessions": sorted(blocking_runs & run_ids),
                "license": "unknown",
                "genome_build": "unknown",
                "pairing_evidence": "repository_layout_only",
            },
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "limitations": [
                "license is unresolved and blocks automatic acquisition readiness",
                "genome build is unresolved",
                "ENA layout metadata does not prove same-cell multiome pairing",
                "remote MD5 values are repository declarations until locally recomputed",
            ],
        }

    @staticmethod
    def _evidence(
        dataset_id: str,
        claim_key: str,
        observed_value: str,
        run: dict[str, Any],
        method: str,
    ) -> dict[str, Any]:
        payload = {
            "source_ref": run.get("source_ref"),
            "source_locator": f"/read_run/{run.get('run_accession')}/{claim_key}",
            "observed_value": observed_value,
            "crawl_source_sha256": run.get("source_sha256"),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "evidence_id": f"promotion-ev-{digest[:20]}",
            "claim_key": claim_key,
            "observed_value": observed_value,
            "supports": True,
            "strength": "STRUCTURAL",
            "source_type": "official_repository",
            "source_ref": str(run.get("source_ref", "")),
            "source_locator": payload["source_locator"],
            "method": method,
            "tool_version": "cellnote-crawl-promotion/0.1",
            "source_payload": payload,
        }

    def _graph_subset(
        self, labels: set[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nodes = [
            item for item in self.graph.get("nodes", [])
            if str(item.get("node_id", "")).removeprefix("accession:") in labels
            or str(item.get("label", "")) in labels
        ]
        node_ids = {str(item.get("node_id", "")) for item in nodes}
        edges = [
            item for item in self.graph.get("edges", [])
            if item.get("source_node") in node_ids
            and item.get("target_node") in node_ids
        ]
        return nodes, edges

    def recover(self, dataset_id: str, issue: str) -> dict[str, Any]:
        return {
            "issue": issue,
            "request_cost": 0,
            "byte_cost": 0,
            "artifacts": [],
            "evidence": [],
            "graph_nodes": [],
            "graph_edges": [],
            "reason": f"crawl promotion cannot synthesize missing files for {dataset_id}",
        }
