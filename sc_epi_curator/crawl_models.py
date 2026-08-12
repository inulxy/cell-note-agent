"""Typed records shared by CellNote web and literature crawlers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .events import canonical_json
from .models import EvidenceStrength


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:20]}"


@dataclass(frozen=True)
class FetchReceipt:
    request_key: str
    requested_url: str
    final_url: str
    status: int
    content_type: str
    fetched_at: str
    body_sha256: str
    body_bytes: int
    blob_path: str
    from_cache: bool
    method: str = "GET"
    response_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CrawlEvidence:
    evidence_id: str
    record_id: str
    claim_key: str
    observed_value: str
    strength: EvidenceStrength
    source_type: str
    source_ref: str
    source_locator: str
    source_sha256: str
    method: str
    tool_version: str = "cellnote-crawler/0.1"


@dataclass
class DiscoveryRecord:
    record_id: str
    source: str
    source_id: str
    canonical_url: str
    title: str
    summary: str
    identifiers: dict[str, list[str]]
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: list[CrawlEvidence] = field(default_factory=list)
    receipts: list[FetchReceipt] = field(default_factory=list)

    def normalized_identifiers(self) -> dict[str, list[str]]:
        return {
            key: sorted({item.strip() for item in values if item.strip()})
            for key, values in sorted(self.identifiers.items())
            if any(item.strip() for item in values)
        }


@dataclass(frozen=True)
class NetworkBudget:
    max_requests: int
    max_bytes: int


@dataclass
class NetworkUsage:
    requests: int = 0
    bytes: int = 0
    cache_hits: int = 0


@dataclass(frozen=True)
class CrawlError:
    source: str
    stage: str
    target: str
    error_type: str
    message: str


@dataclass(frozen=True)
class AccessionEdge:
    edge_id: str
    source_node: str
    target_node: str
    relation: str
    source_ref: str
    source_sha256: str
    method: str


@dataclass(frozen=True)
class EnaRunRecord:
    run_accession: str
    study_accession: str
    secondary_study_accession: str
    experiment_accession: str
    sample_accession: str
    secondary_sample_accession: str
    scientific_name: str
    library_strategy: str
    library_source: str
    library_selection: str
    library_layout: str
    instrument_platform: str
    instrument_model: str
    first_public: str
    source_ref: str
    source_sha256: str


@dataclass(frozen=True)
class RemoteFileCandidate:
    file_id: str
    source: str
    study_accession: str
    experiment_accession: str
    run_accession: str
    sample_accession: str
    uri: str
    file_format: str
    file_role: str
    size_bytes: int | None
    checksum_algorithm: str
    checksum: str
    source_ref: str
    source_sha256: str


@dataclass(frozen=True)
class RemoteFileIssue:
    issue_id: str
    issue_code: str
    severity: str
    run_accession: str
    file_id: str
    message: str


@dataclass(frozen=True)
class RemoteFileProbe:
    probe_id: str
    file_id: str
    run_accession: str
    uri: str
    method: str
    status: int
    reachable: bool
    expected_size_bytes: int | None
    reported_size_bytes: int | None
    size_matches: bool | None
    accept_ranges: str
    etag: str
    last_modified: str
    receipt: FetchReceipt | None = None


@dataclass
class ResolutionBatch:
    resolver: str
    runs: list[EnaRunRecord] = field(default_factory=list)
    files: list[RemoteFileCandidate] = field(default_factory=list)
    edges: list[AccessionEdge] = field(default_factory=list)
    evidence: list[CrawlEvidence] = field(default_factory=list)
    receipts: list[FetchReceipt] = field(default_factory=list)
    unresolved_accessions: list[str] = field(default_factory=list)
    errors: list[CrawlError] = field(default_factory=list)
