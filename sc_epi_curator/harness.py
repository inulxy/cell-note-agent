"""CellNote domain control plane executed beneath the Pi agent harness."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.base import SourceAdapter
from .evidence import EvidenceLedger
from .events import EventStore, canonical_json
from .models import (
    ActionBudget,
    ArtifactRole,
    ClaimRecord,
    ClaimRule,
    ClaimStatus,
    DatasetState,
    EvidenceRecord,
    EvidenceStrength,
    FileArtifact,
    ProposedAction,
    ReadinessTier,
    RetrievalSpec,
    to_primitive,
)
from .policy import BudgetManager
from .validators import audit_bundle, validate_artifact


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: Path
    dataset_count: int
    ready_count: int
    review_count: int
    event_chain_valid: bool


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(to_primitive(value), handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def _parse_artifact(dataset_id: str, value: dict[str, Any]) -> FileArtifact:
    return FileArtifact(
        artifact_id=value["artifact_id"],
        dataset_id=dataset_id,
        role=ArtifactRole(value["role"]),
        source_uri=value["source_uri"],
        size_bytes=int(value["size_bytes"]),
        source=value["source"],
        discovered_via=value["discovered_via"],
        checksum=value.get("checksum"),
        local_path=value.get("local_path"),
    )


def _parse_evidence(dataset_id: str, value: dict[str, Any]) -> EvidenceRecord:
    source_payload = value.get(
        "source_payload",
        {
            "source_ref": value["source_ref"],
            "source_locator": value["source_locator"],
            "observed_value": value["observed_value"],
        },
    )
    return EvidenceRecord(
        evidence_id=value["evidence_id"],
        dataset_id=dataset_id,
        claim_key=value["claim_key"],
        observed_value=value["observed_value"],
        supports=bool(value["supports"]),
        strength=EvidenceStrength(value["strength"]),
        source_type=value["source_type"],
        source_ref=value["source_ref"],
        source_locator=value["source_locator"],
        source_sha256=_sha256_json(source_payload),
        method=value["method"],
        tool_version=value["tool_version"],
    )


class CellNoteDomainHarness:
    """A deterministic state/evidence controller, not an LLM runtime."""

    def __init__(
        self,
        adapter: SourceAdapter,
        run_dir: str | Path,
        run_id: str,
        budget: ActionBudget,
    ) -> None:
        self.adapter = adapter
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventStore(self.run_dir / "state.sqlite")
        self.budget = BudgetManager(budget)

    def run(self) -> RunResult:
        spec = RetrievalSpec.from_dict(self.adapter.retrieval_spec())
        _write_json(self.run_dir / "retrieval_spec.json", spec)
        self._event(
            "RetrievalSpecCompiled",
            {"spec": to_primitive(spec)},
            "run:retrieval-spec",
        )

        cards: list[dict[str, Any]] = []
        all_artifacts: list[FileArtifact] = []
        all_evidence: list[EvidenceRecord] = []
        graph_nodes: list[dict[str, Any]] = []
        graph_edges: list[dict[str, Any]] = []
        acquisition_plans: list[dict[str, Any]] = []
        validation_reports: list[dict[str, Any]] = []
        ready_count = 0
        review_count = 0

        for candidate in self.adapter.candidates():
            result = self._process_candidate(candidate, spec)
            cards.append(result["card"])
            all_artifacts.extend(result["artifacts"])
            all_evidence.extend(result["evidence"])
            graph_nodes.extend(result["graph_nodes"])
            graph_edges.extend(result["graph_edges"])
            acquisition_plans.append(result["acquisition_plan"])
            validation_reports.append(result["validation"])
            if result["card"]["state"] == DatasetState.READY_FOR_ACQUISITION.value:
                ready_count += 1
            else:
                review_count += 1

        self._write_evidence(all_evidence)
        self._write_manifest(all_artifacts)
        _write_json(
            self.run_dir / "accession_graph.json",
            {"nodes": graph_nodes, "edges": graph_edges},
        )
        _write_json(
            self.run_dir / "acquisition_plan.json",
            {
                "run_id": self.run_id,
                "plans": acquisition_plans,
                "budget_usage": to_primitive(self.budget.usage),
            },
        )
        _write_json(
            self.run_dir / "validation_report.json",
            {"run_id": self.run_id, "datasets": validation_reports},
        )
        for card in cards:
            _write_json(
                self.run_dir / "datasets" / card["dataset_id"] / "dataset_card.json",
                card,
            )

        self.events.export_jsonl(self.run_id, self.run_dir / "events.jsonl")
        chain_valid = self.events.verify_chain(self.run_id)
        summary = {
            "run_id": self.run_id,
            "dataset_count": len(cards),
            "ready_for_acquisition": ready_count,
            "manual_review": review_count,
            "event_chain_valid": chain_valid,
            "scope": getattr(
                self.adapter,
                "run_scope",
                "offline_fixture_harness_validation",
            ),
            "not_claimed": getattr(
                self.adapter,
                "run_not_claimed",
                [
                    "no public API was queried",
                    "no biological data file was downloaded",
                    "no training-ready matrix was produced",
                ],
            ),
        }
        _write_json(self.run_dir / "run_summary.json", summary)
        self.events.close()
        return RunResult(
            run_id=self.run_id,
            run_dir=self.run_dir,
            dataset_count=len(cards),
            ready_count=ready_count,
            review_count=review_count,
            event_chain_valid=chain_valid,
        )

    def _process_candidate(
        self,
        candidate: dict[str, Any],
        spec: RetrievalSpec,
    ) -> dict[str, Any]:
        dataset_id = candidate["dataset_id"]
        state = DatasetState.DISCOVERED
        self._event(
            "CandidateObserved",
            {"dataset_id": dataset_id, "state": state.value},
            f"{dataset_id}:observed",
        )

        ledger = EvidenceLedger()
        for value in candidate["claims"]:
            ledger.add_claim(
                ClaimRecord(
                    dataset_id=dataset_id,
                    claim_key=value["claim_key"],
                    expected_value=value["expected_value"],
                    rule=ClaimRule(value["rule"]),
                )
            )
        evidence = [
            _parse_evidence(dataset_id, value)
            for value in candidate.get("initial_evidence", [])
        ]
        for item in evidence:
            ledger.add_evidence(item)
        initial_claims = ledger.resolve_all()
        state = DatasetState.EVIDENCE_PENDING
        self._event(
            "ClaimsResolved",
            {
                "dataset_id": dataset_id,
                "phase": "initial",
                "claims": to_primitive(initial_claims),
                "state": state.value,
            },
            f"{dataset_id}:claims:initial",
        )

        artifacts = [
            _parse_artifact(dataset_id, value)
            for value in candidate.get("initial_artifacts", [])
        ]
        required_roles = tuple(
            ArtifactRole(value) for value in candidate["required_roles"]
        )
        initial_audit = audit_bundle(dataset_id, artifacts, required_roles)
        self._event(
            "FileBundleAudited",
            {
                "dataset_id": dataset_id,
                "phase": "initial",
                "audit": to_primitive(initial_audit),
            },
            f"{dataset_id}:audit:initial",
        )

        graph_nodes = list(candidate.get("graph_nodes", []))
        graph_edges = list(candidate.get("graph_edges", []))
        recovery_decision: dict[str, Any] | None = None
        if initial_audit.missing_roles:
            state = DatasetState.RECOVERY_REQUIRED
            issue = "MISSING_" + "_AND_".join(
                role.value.upper() for role in initial_audit.missing_roles
            )
            self._event(
                "RecoveryRequired",
                {
                    "dataset_id": dataset_id,
                    "issue": issue,
                    "state": state.value,
                },
                f"{dataset_id}:recovery-required:{issue}",
            )
            recovery = self.adapter.recover(dataset_id, issue)
            action = ProposedAction(
                action_id=f"{dataset_id}:recovery:1",
                action_type="expand_accession_neighborhood",
                dataset_id=dataset_id,
                request_cost=int(recovery.get("request_cost", 0)),
                byte_cost=int(recovery.get("byte_cost", 0)),
                recovery_rounds=1,
                reason=f"recover roles: {[role.value for role in initial_audit.missing_roles]}",
            )
            policy = self.budget.evaluate(action)
            recovery_decision = {
                "action": to_primitive(action),
                "policy": to_primitive(policy),
            }
            self._event(
                "RecoveryPlanned",
                {"dataset_id": dataset_id, **recovery_decision},
                f"{dataset_id}:recovery-plan:1",
            )
            if policy.allowed and not policy.requires_approval:
                self.budget.commit(action)
                recovered_artifacts = [
                    _parse_artifact(dataset_id, value)
                    for value in recovery.get("artifacts", [])
                ]
                recovered_evidence = [
                    _parse_evidence(dataset_id, value)
                    for value in recovery.get("evidence", [])
                ]
                artifacts.extend(recovered_artifacts)
                evidence.extend(recovered_evidence)
                for item in recovered_evidence:
                    ledger.add_evidence(item)
                graph_nodes.extend(recovery.get("graph_nodes", []))
                graph_edges.extend(recovery.get("graph_edges", []))
                self._event(
                    "RecoveryExecuted",
                    {
                        "dataset_id": dataset_id,
                        "artifact_ids": [
                            item.artifact_id for item in recovered_artifacts
                        ],
                        "evidence_ids": [
                            item.evidence_id for item in recovered_evidence
                        ],
                    },
                    f"{dataset_id}:recovery-executed:1",
                )

        claims = ledger.resolve_all()
        final_audit = audit_bundle(dataset_id, artifacts, required_roles)
        artifact_errors = {
            item.artifact_id: validate_artifact(item)
            for item in artifacts
            if validate_artifact(item)
        }
        required_claims = set(candidate.get("required_verified_claims", []))
        verified_claims = {
            claim.claim_key
            for claim in claims
            if claim.status == ClaimStatus.VERIFIED
        }
        unresolved_claims = sorted(required_claims - verified_claims)

        license_value = candidate.get("metadata", {}).get("license", "unknown")
        license_ok = license_value not in {"", "unknown", "unclear"}
        valid = (
            final_audit.usable
            and not artifact_errors
            and not unresolved_claims
            and license_ok
        )
        if valid:
            state = DatasetState.READY_FOR_ACQUISITION
            tier = ReadinessTier.GOLD_CANDIDATE
        else:
            state = DatasetState.MANUAL_REVIEW
            tier = ReadinessTier.REVIEW

        total_bytes = sum(item.size_bytes for item in artifacts)
        acquisition_action = ProposedAction(
            action_id=f"{dataset_id}:acquire",
            action_type="download_verified_bundle",
            dataset_id=dataset_id,
            request_cost=len(artifacts),
            byte_cost=total_bytes,
            recovery_rounds=0,
            reason="acquire the minimal verified preferred-file bundle",
        )
        acquisition_policy = self.budget.evaluate(acquisition_action)
        acquisition_plan = {
            "dataset_id": dataset_id,
            "artifact_ids": [item.artifact_id for item in artifacts],
            "estimated_bytes": total_bytes,
            "decision": to_primitive(acquisition_policy),
            "execution_status": (
                "PLANNED_NOT_EXECUTED"
                if state == DatasetState.READY_FOR_ACQUISITION
                else "BLOCKED_MANUAL_REVIEW"
            ),
        }
        self._event(
            "AcquisitionPlanned",
            {
                "dataset_id": dataset_id,
                "plan": acquisition_plan,
                "state": state.value,
            },
            f"{dataset_id}:acquisition-plan",
        )

        validation = {
            "dataset_id": dataset_id,
            "bundle": to_primitive(final_audit),
            "artifact_errors": artifact_errors,
            "required_claims": sorted(required_claims),
            "verified_claims": sorted(verified_claims),
            "unresolved_claims": unresolved_claims,
            "license_ok": license_ok,
            "valid_for_acquisition_planning": valid,
        }
        card = {
            "dataset_id": dataset_id,
            "state": state.value,
            "readiness_tier": tier.value,
            "metadata": candidate.get("metadata", {}),
            "claims": to_primitive(claims),
            "artifact_ids": [item.artifact_id for item in artifacts],
            "recovery": recovery_decision,
            "acquisition_plan_ref": "../../acquisition_plan.json",
            "validation_report_ref": "../../validation_report.json",
            "limitations": [
                "readiness grades describe remote-bundle review, not a training-data release",
                "files have not been downloaded or biologically processed in this run",
                *candidate.get("limitations", []),
            ],
        }
        return {
            "card": card,
            "artifacts": artifacts,
            "evidence": ledger.evidence,
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "acquisition_plan": acquisition_plan,
            "validation": validation,
        }

    def _event(
        self,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> None:
        self.events.append(self.run_id, event_type, payload, idempotency_key)

    def _write_evidence(self, evidence: list[EvidenceRecord]) -> None:
        path = self.run_dir / "evidence_ledger.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for item in sorted(evidence, key=lambda value: value.evidence_id):
                handle.write(canonical_json(to_primitive(item)) + "\n")

    def _write_manifest(self, artifacts: list[FileArtifact]) -> None:
        path = self.run_dir / "file_manifest.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "artifact_id",
                    "dataset_id",
                    "role",
                    "source_uri",
                    "size_bytes",
                    "source",
                    "discovered_via",
                    "checksum",
                    "local_path",
                ],
            )
            writer.writeheader()
            for artifact in sorted(artifacts, key=lambda item: item.artifact_id):
                writer.writerow(to_primitive(artifact))
