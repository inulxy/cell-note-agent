"""Claim–evidence ledger and deterministic resolution rules."""

from __future__ import annotations

from collections import defaultdict

from .models import (
    ClaimRecord,
    ClaimRule,
    ClaimStatus,
    EvidenceRecord,
    EvidenceStrength,
)


class EvidenceLedger:
    def __init__(self) -> None:
        self._claims: dict[tuple[str, str], ClaimRecord] = {}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._claim_evidence: dict[tuple[str, str], list[str]] = defaultdict(list)

    def add_claim(self, claim: ClaimRecord) -> None:
        key = (claim.dataset_id, claim.claim_key)
        if key in self._claims:
            raise ValueError(f"duplicate claim: {key}")
        self._claims[key] = claim

    def add_evidence(self, evidence: EvidenceRecord) -> None:
        if evidence.evidence_id in self._evidence:
            raise ValueError(f"duplicate evidence: {evidence.evidence_id}")
        key = (evidence.dataset_id, evidence.claim_key)
        if key not in self._claims:
            raise KeyError(f"evidence references unknown claim: {key}")
        self._evidence[evidence.evidence_id] = evidence
        self._claim_evidence[key].append(evidence.evidence_id)

    def resolve(self, dataset_id: str, claim_key: str) -> ClaimRecord:
        key = (dataset_id, claim_key)
        claim = self._claims[key]
        records = [self._evidence[item] for item in self._claim_evidence[key]]
        supporting = [
            item for item in records
            if item.supports and item.observed_value == claim.expected_value
        ]
        contradicting = [
            item for item in records
            if (not item.supports) or item.observed_value != claim.expected_value
        ]

        claim.supporting_evidence = [item.evidence_id for item in supporting]
        claim.contradicting_evidence = [item.evidence_id for item in contradicting]

        if supporting and contradicting:
            claim.status = ClaimStatus.CONFLICTED
            claim.resolution_reason = "supporting and contradicting evidence coexist"
            return claim
        if not supporting and contradicting:
            claim.status = ClaimStatus.REFUTED
            claim.resolution_reason = "only contradicting evidence is available"
            return claim
        if not supporting:
            claim.status = ClaimStatus.UNVERIFIED
            claim.resolution_reason = "no supporting evidence"
            return claim

        source_count = len({item.source_ref for item in supporting})
        strengths = {item.strength for item in supporting}
        if claim.rule == ClaimRule.REQUIRES_EXECUTABLE:
            verified = EvidenceStrength.EXECUTABLE in strengths
            reason = "requires executable evidence"
        elif claim.rule == ClaimRule.TWO_INDEPENDENT_SOURCES:
            verified = source_count >= 2 and (
                EvidenceStrength.STRUCTURAL in strengths
                or EvidenceStrength.EXECUTABLE in strengths
            )
            reason = "requires two sources including structural/executable evidence"
        else:
            verified = any(
                item.source_type in {"official_repository", "license_record"}
                for item in supporting
            )
            reason = "requires an authoritative source"

        claim.status = ClaimStatus.VERIFIED if verified else ClaimStatus.SUPPORTED
        claim.resolution_reason = (
            f"verification rule satisfied: {reason}"
            if verified
            else f"support exists but verification rule is unmet: {reason}"
        )
        return claim

    def resolve_all(self) -> list[ClaimRecord]:
        return [
            self.resolve(dataset_id, claim_key)
            for dataset_id, claim_key in sorted(self._claims)
        ]

    @property
    def evidence(self) -> list[EvidenceRecord]:
        return [self._evidence[key] for key in sorted(self._evidence)]

    @property
    def claims(self) -> list[ClaimRecord]:
        return [self._claims[key] for key in sorted(self._claims)]

