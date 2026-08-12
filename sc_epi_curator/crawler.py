"""Crawl orchestration and auditable discovery-run artifact generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .adapters.discovery import DiscoveryAdapter
from .crawl_models import (
    AccessionEdge,
    CrawlError,
    DiscoveryRecord,
    EnaRunRecord,
    FetchReceipt,
    NetworkUsage,
    RemoteFileCandidate,
    RemoteFileProbe,
    ResolutionBatch,
    stable_id,
)
from .events import EventStore, canonical_json
from .file_audit import audit_remote_files
from .models import to_primitive
from .resolvers.base import AccessionResolver
from .remote_probe import RemoteFileProber


@dataclass(frozen=True)
class CrawlResult:
    run_id: str
    run_dir: Path
    record_count: int
    evidence_count: int
    error_count: int
    event_chain_valid: bool


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            to_primitive(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(canonical_json(to_primitive(value)) + "\n")
            count += 1
    return count


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DiscoveryCrawler:
    def __init__(
        self,
        *,
        adapters: list[DiscoveryAdapter],
        run_dir: str | Path,
        run_id: str,
        usage: NetworkUsage,
        resolvers: list[AccessionResolver] | None = None,
        remote_file_prober: RemoteFileProber | None = None,
    ) -> None:
        self.adapters = adapters
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.usage = usage
        self.resolvers = list(resolvers or [])
        self.remote_file_prober = remote_file_prober
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventStore(self.run_dir / "crawl_state.sqlite")

    def run(self, *, query: str, limit_per_source: int) -> CrawlResult:
        source_names = [adapter.name for adapter in self.adapters]
        resolver_names = [resolver.name for resolver in self.resolvers]
        prober_name = (
            self.remote_file_prober.name if self.remote_file_prober is not None else None
        )
        existing_manifest_path = self.run_dir / "crawl_manifest.json"
        if existing_manifest_path.exists():
            manifest = json.loads(
                existing_manifest_path.read_text(encoding="utf-8")
            )
            expected = {
                "run_id": self.run_id,
                "query": query,
                "sources": source_names,
                "resolvers": resolver_names,
                "remote_file_prober": prober_name,
                "limit_per_source": limit_per_source,
            }
            actual = {key: manifest.get(key) for key in expected}
            if actual != expected:
                self.events.close()
                raise ValueError(
                    "run_id/run_dir already contains a different crawl: "
                    f"expected={expected}, actual={actual}"
                )
            valid = self.events.verify_chain(self.run_id)
            self.events.close()
            counts = manifest.get("counts", {})
            return CrawlResult(
                run_id=self.run_id,
                run_dir=self.run_dir,
                record_count=int(counts.get("records", 0)),
                evidence_count=int(counts.get("evidence", 0)),
                error_count=int(counts.get("errors", 0)),
                event_chain_valid=valid,
            )

        started_at = datetime.now(timezone.utc).isoformat()
        self.events.append(
            self.run_id,
            "CrawlStarted",
            {
                "query": query,
                "limit_per_source": limit_per_source,
                "sources": source_names,
                "resolvers": resolver_names,
                "remote_file_prober": prober_name,
            },
            "crawl:started",
        )
        records: list[DiscoveryRecord] = []
        errors: list[CrawlError] = []
        seen: set[tuple[str, str]] = set()

        for adapter in self.adapters:
            try:
                discovered = adapter.discover(query, limit_per_source)
            except Exception as error:
                crawl_error = CrawlError(
                    source=adapter.name,
                    stage="discover",
                    target=query,
                    error_type=type(error).__name__,
                    message=str(error),
                )
                errors.append(crawl_error)
                self.events.append(
                    self.run_id,
                    "SourceFailed",
                    to_primitive(crawl_error),
                    f"source:{adapter.name}:failed",
                )
                continue
            accepted = 0
            for record in discovered:
                key = (record.source, record.source_id)
                if key in seen:
                    continue
                seen.add(key)
                record.identifiers = record.normalized_identifiers()
                records.append(record)
                accepted += 1
            adapter_errors = getattr(adapter, "errors", [])
            errors.extend(adapter_errors)
            self.events.append(
                self.run_id,
                "SourceCompleted",
                {
                    "source": adapter.name,
                    "records": accepted,
                    "errors": len(adapter_errors),
                },
                f"source:{adapter.name}:completed",
            )

        records.sort(key=lambda item: (item.source, item.source_id))
        resolution_batches = self._resolve_accessions(records, errors)
        runs = self._unique_runs(resolution_batches)
        remote_files = self._unique_remote_files(resolution_batches)
        remote_file_issues = audit_remote_files(runs, remote_files)
        remote_file_probes: list[RemoteFileProbe] = []
        probe_receipts: list[FetchReceipt] = []
        if self.remote_file_prober is not None:
            probe_batch = self.remote_file_prober.probe(remote_files)
            remote_file_probes = sorted(
                probe_batch.probes, key=lambda item: item.probe_id
            )
            remote_file_issues.extend(probe_batch.issues)
            probe_receipts = probe_batch.receipts
            self.events.append(
                self.run_id,
                "RemoteFilesProbed",
                {
                    "prober": prober_name,
                    "candidates": len(remote_files),
                    "probes": len(remote_file_probes),
                    "issues": len(probe_batch.issues),
                },
                "remote-files:probed",
            )
        remote_file_issues.sort(key=lambda item: item.issue_id)
        resolver_edges = self._unique_edges(resolution_batches)
        unresolved = sorted(
            {
                accession
                for batch in resolution_batches
                for accession in batch.unresolved_accessions
            }
        )
        evidence = sorted(
            [item for record in records for item in record.evidence]
            + [
                item
                for batch in resolution_batches
                for item in batch.evidence
            ],
            key=lambda item: item.evidence_id,
        )
        receipts = self._unique_receipts(
            records, resolution_batches, probe_receipts
        )
        mentions = self._identifier_mentions(records)
        accession_graph = self._accession_graph(
            records,
            mentions,
            resolver_edges,
            remote_files,
        )

        output_paths = {
            "records": self.run_dir / "discovery_records.jsonl",
            "evidence": self.run_dir / "crawl_evidence.jsonl",
            "receipts": self.run_dir / "fetch_receipts.jsonl",
            "mentions": self.run_dir / "identifier_mentions.jsonl",
            "errors": self.run_dir / "crawl_errors.jsonl",
            "runs": self.run_dir / "ena_run_manifest.jsonl",
            "remote_files": self.run_dir / "remote_file_candidates.jsonl",
            "remote_file_issues": self.run_dir / "remote_file_issues.jsonl",
            "remote_file_probes": self.run_dir / "remote_file_probes.jsonl",
            "accession_graph": self.run_dir / "accession_graph.json",
            "unresolved": self.run_dir / "unresolved_accessions.json",
        }
        _write_jsonl(output_paths["records"], records)
        _write_jsonl(output_paths["evidence"], evidence)
        _write_jsonl(output_paths["receipts"], receipts)
        _write_jsonl(output_paths["mentions"], mentions)
        _write_jsonl(output_paths["errors"], errors)
        _write_jsonl(output_paths["runs"], runs)
        _write_jsonl(output_paths["remote_files"], remote_files)
        _write_jsonl(output_paths["remote_file_issues"], remote_file_issues)
        _write_jsonl(output_paths["remote_file_probes"], remote_file_probes)
        _write_json(output_paths["accession_graph"], accession_graph)
        _write_json(
            output_paths["unresolved"],
            {"accessions": unresolved},
        )

        manifest = {
            "run_id": self.run_id,
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "sources": source_names,
            "resolvers": resolver_names,
            "remote_file_prober": prober_name,
            "limit_per_source": limit_per_source,
            "counts": {
                "records": len(records),
                "evidence": len(evidence),
                "receipts": len(receipts),
                "identifier_mentions": len(mentions),
                "errors": len(errors),
                "ena_runs": len(runs),
                "remote_files": len(remote_files),
                "remote_file_issues": len(remote_file_issues),
                "remote_file_probes": len(remote_file_probes),
                "unresolved_accessions": len(unresolved),
            },
            "network_usage": to_primitive(self.usage),
            "outputs": {
                key: {
                    "path": path.name,
                    "sha256": _file_sha256(path),
                }
                for key, path in output_paths.items()
            },
            "scope": "metadata_and_open_access_evidence_discovery",
            "not_claimed": [
                "discovered identifiers are not automatically verified datasets",
                "no large biological data files were downloaded",
                "literature statements remain declarative evidence",
                "remote file candidates have not been downloaded or checksummed locally",
                "remote probes establish reachability and reported size only, not content integrity",
            ],
        }
        _write_json(self.run_dir / "crawl_manifest.json", manifest)
        self.events.append(
            self.run_id,
            "CrawlCompleted",
            {
                "records": len(records),
                "evidence": len(evidence),
                "errors": len(errors),
                "manifest_sha256": _file_sha256(
                    self.run_dir / "crawl_manifest.json"
                ),
            },
            "crawl:completed",
        )
        self.events.export_jsonl(
            self.run_id, self.run_dir / "crawl_events.jsonl"
        )
        valid = self.events.verify_chain(self.run_id)
        self.events.close()
        return CrawlResult(
            run_id=self.run_id,
            run_dir=self.run_dir,
            record_count=len(records),
            evidence_count=len(evidence),
            error_count=len(errors),
            event_chain_valid=valid,
        )

    @staticmethod
    def _unique_receipts(
        records: list[DiscoveryRecord],
        batches: list[ResolutionBatch],
        extra_receipts: list[FetchReceipt] | None = None,
    ) -> list[FetchReceipt]:
        receipts: dict[str, FetchReceipt] = {}
        for record in records:
            for receipt in record.receipts:
                receipts.setdefault(receipt.request_key, receipt)
        for batch in batches:
            for receipt in batch.receipts:
                receipts.setdefault(receipt.request_key, receipt)
        for receipt in extra_receipts or []:
            receipts.setdefault(receipt.request_key, receipt)
        return [receipts[key] for key in sorted(receipts)]

    def _resolve_accessions(
        self,
        records: list[DiscoveryRecord],
        errors: list[CrawlError],
    ) -> list[ResolutionBatch]:
        batches: list[ResolutionBatch] = []
        for resolver in self.resolvers:
            try:
                batch = resolver.resolve(records)
            except Exception as error:
                crawl_error = CrawlError(
                    source=resolver.name,
                    stage="resolve",
                    target=self.run_id,
                    error_type=type(error).__name__,
                    message=str(error),
                )
                errors.append(crawl_error)
                self.events.append(
                    self.run_id,
                    "ResolverFailed",
                    to_primitive(crawl_error),
                    f"resolver:{resolver.name}:failed",
                )
                continue
            errors.extend(batch.errors)
            batches.append(batch)
            self.events.append(
                self.run_id,
                "ResolverCompleted",
                {
                    "resolver": resolver.name,
                    "runs": len(batch.runs),
                    "files": len(batch.files),
                    "unresolved_accessions": len(batch.unresolved_accessions),
                    "errors": len(batch.errors),
                },
                f"resolver:{resolver.name}:completed",
            )
        return batches

    @staticmethod
    def _unique_runs(batches: list[ResolutionBatch]) -> list[EnaRunRecord]:
        values: dict[str, EnaRunRecord] = {}
        for batch in batches:
            for run in batch.runs:
                values.setdefault(run.run_accession, run)
        return [values[key] for key in sorted(values)]

    @staticmethod
    def _unique_remote_files(
        batches: list[ResolutionBatch],
    ) -> list[RemoteFileCandidate]:
        values: dict[str, RemoteFileCandidate] = {}
        for batch in batches:
            for item in batch.files:
                values.setdefault(item.file_id, item)
        return [values[key] for key in sorted(values)]

    @staticmethod
    def _unique_edges(
        batches: list[ResolutionBatch],
    ) -> list[AccessionEdge]:
        values: dict[str, AccessionEdge] = {}
        for batch in batches:
            for item in batch.edges:
                values.setdefault(item.edge_id, item)
        return [values[key] for key in sorted(values)]

    @staticmethod
    def _accession_graph(
        records: list[DiscoveryRecord],
        mentions: list[dict[str, str]],
        resolver_edges: list[AccessionEdge],
        remote_files: list[RemoteFileCandidate],
    ) -> dict[str, list[Any]]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}
        for record in records:
            nodes[record.record_id] = {
                "node_id": record.record_id,
                "node_type": "discovery_record",
                "label": record.title,
                "source": record.source,
            }
        for mention in mentions:
            identifier_node = f"accession:{mention['identifier']}"
            nodes.setdefault(
                identifier_node,
                {
                    "node_id": identifier_node,
                    "node_type": mention["identifier_type"],
                    "label": mention["identifier"],
                },
            )
            material = {
                "source": mention["record_id"],
                "target": identifier_node,
                "relation": "mentions",
            }
            edge_id = stable_id("edge", material)
            edges[edge_id] = {
                "edge_id": edge_id,
                "source_node": mention["record_id"],
                "target_node": identifier_node,
                "relation": "mentions",
            }
        for edge in resolver_edges:
            source_node = f"accession:{edge.source_node}"
            target_node = f"accession:{edge.target_node}"
            nodes.setdefault(
                source_node,
                {
                    "node_id": source_node,
                    "node_type": "accession",
                    "label": edge.source_node,
                },
            )
            nodes.setdefault(
                target_node,
                {
                    "node_id": target_node,
                    "node_type": "accession",
                    "label": edge.target_node,
                },
            )
            value = to_primitive(edge)
            value["source_node"] = source_node
            value["target_node"] = target_node
            edges[edge.edge_id] = value
        for item in remote_files:
            nodes[item.file_id] = {
                "node_id": item.file_id,
                "node_type": "remote_file",
                "label": item.uri,
            }
            run_node = f"accession:{item.run_accession}"
            nodes.setdefault(
                run_node,
                {
                    "node_id": run_node,
                    "node_type": "sra_run",
                    "label": item.run_accession,
                },
            )
            material = {
                "source": run_node,
                "target": item.file_id,
                "relation": "has_remote_file",
            }
            edge_id = stable_id("edge", material)
            edges[edge_id] = {
                "edge_id": edge_id,
                "source_node": run_node,
                "target_node": item.file_id,
                "relation": "has_remote_file",
                "source_ref": item.source_ref,
                "source_sha256": item.source_sha256,
            }
        return {
            "nodes": [nodes[key] for key in sorted(nodes)],
            "edges": [edges[key] for key in sorted(edges)],
        }

    @staticmethod
    def _identifier_mentions(
        records: list[DiscoveryRecord],
    ) -> list[dict[str, str]]:
        mentions: list[dict[str, str]] = []
        for record in records:
            for identifier_type, values in record.identifiers.items():
                for value in values:
                    mentions.append(
                        {
                            "identifier_type": identifier_type,
                            "identifier": value,
                            "record_id": record.record_id,
                            "source": record.source,
                            "source_id": record.source_id,
                        }
                    )
        return sorted(
            mentions,
            key=lambda item: (
                item["identifier_type"],
                item["identifier"],
                item["record_id"],
            ),
        )
