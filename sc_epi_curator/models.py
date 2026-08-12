"""Typed, dependency-free domain models for CellNote."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class DatasetState(StringEnum):
    DISCOVERED = "DISCOVERED"
    EVIDENCE_PENDING = "EVIDENCE_PENDING"
    EVIDENCE_VERIFIED = "EVIDENCE_VERIFIED"
    FILES_PENDING = "FILES_PENDING"
    FILES_AUDITED = "FILES_AUDITED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    ACQUISITION_PLANNED = "ACQUISITION_PLANNED"
    READY_FOR_ACQUISITION = "READY_FOR_ACQUISITION"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    QUARANTINED = "QUARANTINED"


class EvidenceStrength(StringEnum):
    DECLARATIVE = "DECLARATIVE"
    STRUCTURAL = "STRUCTURAL"
    EXECUTABLE = "EXECUTABLE"


class ClaimRule(StringEnum):
    AUTHORITATIVE_SOURCE = "AUTHORITATIVE_SOURCE"
    TWO_INDEPENDENT_SOURCES = "TWO_INDEPENDENT_SOURCES"
    REQUIRES_EXECUTABLE = "REQUIRES_EXECUTABLE"


class ClaimStatus(StringEnum):
    UNVERIFIED = "UNVERIFIED"
    SUPPORTED = "SUPPORTED"
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"
    REFUTED = "REFUTED"
    NOT_TESTABLE = "NOT_TESTABLE"


class ArtifactRole(StringEnum):
    RNA_COUNT_MATRIX = "rna_count_matrix"
    ATAC_FRAGMENTS = "atac_fragments"
    ATAC_PEAK_MATRIX = "atac_peak_matrix"
    CELL_METADATA = "cell_metadata"
    BARCODE_LIST = "barcode_list"
    FASTQ = "fastq"


class ReadinessTier(StringEnum):
    GOLD_CANDIDATE = "GOLD_CANDIDATE"
    SILVER_CANDIDATE = "SILVER_CANDIDATE"
    REVIEW = "REVIEW"
    QUARANTINE = "QUARANTINE"


@dataclass(frozen=True)
class RetrievalSpec:
    species: tuple[str, ...]
    tissues: tuple[str, ...]
    conditions: tuple[str, ...]
    modalities: tuple[str, ...]
    pairing_requirement: str
    preferred_files: tuple[str, ...]
    fallback_files: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RetrievalSpec":
        return cls(
            species=tuple(value.get("species", [])),
            tissues=tuple(value.get("tissues", [])),
            conditions=tuple(value.get("conditions", [])),
            modalities=tuple(value.get("modalities", [])),
            pairing_requirement=value.get("pairing_requirement", "unknown"),
            preferred_files=tuple(value.get("preferred_files", [])),
            fallback_files=tuple(value.get("fallback_files", [])),
            exclude=tuple(value.get("exclude", [])),
        )


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    dataset_id: str
    claim_key: str
    observed_value: str
    supports: bool
    strength: EvidenceStrength
    source_type: str
    source_ref: str
    source_locator: str
    source_sha256: str
    method: str
    tool_version: str


@dataclass
class ClaimRecord:
    dataset_id: str
    claim_key: str
    expected_value: str
    rule: ClaimRule
    status: ClaimStatus = ClaimStatus.UNVERIFIED
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    resolution_reason: str = ""


@dataclass(frozen=True)
class FileArtifact:
    artifact_id: str
    dataset_id: str
    role: ArtifactRole
    source_uri: str
    size_bytes: int
    source: str
    discovered_via: str
    checksum: str | None = None
    local_path: str | None = None


@dataclass(frozen=True)
class BundleAudit:
    dataset_id: str
    required_roles: tuple[ArtifactRole, ...]
    present_roles: tuple[ArtifactRole, ...]
    missing_roles: tuple[ArtifactRole, ...]
    usable: bool


@dataclass(frozen=True)
class ActionBudget:
    max_requests: int
    max_bytes: int
    max_recovery_rounds: int
    approval_download_bytes: int


@dataclass
class BudgetUsage:
    requests: int = 0
    bytes: int = 0
    recovery_rounds: int = 0


@dataclass(frozen=True)
class ProposedAction:
    action_id: str
    action_type: str
    dataset_id: str
    request_cost: int
    byte_cost: int
    recovery_rounds: int
    reason: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reasons: tuple[str, ...]


def to_primitive(value: Any) -> Any:
    """Convert dataclasses and enums into JSON-compatible primitives."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_primitive(item) for item in value]
    return value

