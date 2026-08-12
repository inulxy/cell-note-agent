"""Resolve SRA/ENA accessions into run-level FASTQ manifests."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..crawl_models import (
    AccessionEdge,
    CrawlError,
    CrawlEvidence,
    DiscoveryRecord,
    EnaRunRecord,
    RemoteFileCandidate,
    ResolutionBatch,
    stable_id,
)
from ..http import FetchError, HttpClient, build_url
from ..models import EvidenceStrength


ENA_SEARCH_URL = "https://www.ebi.ac.uk/ena/portal/api/search"
RETURN_FIELDS = (
    "study_accession",
    "secondary_study_accession",
    "secondary_project",
    "experiment_accession",
    "run_accession",
    "sample_accession",
    "secondary_sample_accession",
    "scientific_name",
    "library_strategy",
    "library_source",
    "library_selection",
    "library_layout",
    "instrument_platform",
    "instrument_model",
    "first_public",
    "fastq_ftp",
    "fastq_md5",
    "fastq_bytes",
    "fastq_file_role",
)


def _split(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _at(values: list[str], index: int) -> str:
    return values[index] if index < len(values) else ""


def _as_int(value: str) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _https_file_uri(value: str) -> str:
    if value.startswith("https://"):
        return value
    if value.startswith("ftp://"):
        return "https://" + value.removeprefix("ftp://")
    return "https://" + value.lstrip("/")


class EnaRunResolver:
    name = "ena_read_run"

    def __init__(
        self,
        client: HttpClient,
        *,
        max_accessions: int = 25,
        max_runs: int = 200,
        accessions_per_request: int = 10,
    ) -> None:
        self.client = client
        self.max_accessions = max(0, max_accessions)
        self.max_runs = max(0, max_runs)
        self.accessions_per_request = max(1, min(accessions_per_request, 25))

    def resolve(self, records: list[DiscoveryRecord]) -> ResolutionBatch:
        batch = ResolutionBatch(resolver=self.name)
        accession_records = self._collect_accessions(records)
        selected = sorted(accession_records)[: self.max_accessions]
        skipped = sorted(accession_records)[self.max_accessions :]
        batch.unresolved_accessions.extend(skipped)
        if not selected or self.max_runs == 0:
            return batch

        runs: dict[str, EnaRunRecord] = {}
        files: dict[str, RemoteFileCandidate] = {}
        edges: dict[str, AccessionEdge] = {}
        evidence: dict[str, CrawlEvidence] = {}
        resolved: set[str] = set()
        for start in range(0, len(selected), self.accessions_per_request):
            accessions = selected[start : start + self.accessions_per_request]
            remaining = self.max_runs - len(runs)
            if remaining <= 0:
                batch.unresolved_accessions.extend(accessions)
                continue
            query = " OR ".join(self._predicate(item) for item in accessions)
            url = build_url(
                ENA_SEARCH_URL,
                {
                    "result": "read_run",
                    "query": query,
                    "fields": ",".join(RETURN_FIELDS),
                    "format": "json",
                    "limit": remaining,
                },
            )
            try:
                payload, receipt = self.client.get_json_value(url)
            except FetchError as error:
                batch.errors.append(
                    CrawlError(
                        source=self.name,
                        stage="resolve",
                        target=",".join(accessions),
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )
                batch.unresolved_accessions.extend(accessions)
                continue
            batch.receipts.append(receipt)
            if not isinstance(payload, list):
                batch.errors.append(
                    CrawlError(
                        source=self.name,
                        stage="parse",
                        target=",".join(accessions),
                        error_type="FetchContentError",
                        message="ENA read_run response was not a JSON array",
                    )
                )
                batch.unresolved_accessions.extend(accessions)
                continue
            for row in payload:
                if not isinstance(row, dict):
                    continue
                run_accession = str(row.get("run_accession") or "")
                if not run_accession:
                    continue
                matched = self._matched_inputs(row, accessions)
                resolved.update(matched)
                run = self._run_record(row, receipt)
                runs.setdefault(run.run_accession, run)
                for edge in self._edges(row, receipt):
                    edges.setdefault(edge.edge_id, edge)
                for file in self._files(row, receipt):
                    files.setdefault(file.file_id, file)
                linked_record_ids = {
                    record_id
                    for accession in matched
                    for record_id in accession_records[accession]
                }
                for record_id in linked_record_ids:
                    item = self._resolution_evidence(
                        record_id=record_id,
                        query_accessions=matched,
                        run_accession=run_accession,
                        receipt=receipt,
                    )
                    evidence.setdefault(item.evidence_id, item)

        batch.runs = [runs[key] for key in sorted(runs)]
        batch.files = [files[key] for key in sorted(files)]
        batch.edges = [edges[key] for key in sorted(edges)]
        batch.evidence = [evidence[key] for key in sorted(evidence)]
        batch.unresolved_accessions.extend(
            item for item in selected if item not in resolved
        )
        batch.unresolved_accessions = sorted(set(batch.unresolved_accessions))
        return batch

    @staticmethod
    def _collect_accessions(
        records: list[DiscoveryRecord],
    ) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for record in records:
            for key in ("sra_study", "sra_run", "bioproject"):
                for accession in record.identifiers.get(key, []):
                    result[accession.upper()].add(record.record_id)
        return result

    @staticmethod
    def _predicate(accession: str) -> str:
        if accession.startswith(("SRR", "ERR", "DRR")):
            return f'run_accession="{accession}"'
        if accession.startswith(("SRP", "ERP", "DRP")):
            return (
                f'(study_accession="{accession}" OR '
                f'secondary_study_accession="{accession}")'
            )
        if accession.startswith("PRJ"):
            return f'secondary_project="{accession}"'
        raise ValueError(f"unsupported ENA resolver accession: {accession}")

    @staticmethod
    def _matched_inputs(row: dict[str, Any], accessions: list[str]) -> set[str]:
        row_values = {
            str(row.get("run_accession") or "").upper(),
            str(row.get("study_accession") or "").upper(),
            str(row.get("secondary_study_accession") or "").upper(),
            str(row.get("secondary_project") or "").upper(),
        }
        return set(accessions) & row_values

    @staticmethod
    def _run_record(row: dict[str, Any], receipt: Any) -> EnaRunRecord:
        return EnaRunRecord(
            run_accession=str(row.get("run_accession") or ""),
            study_accession=str(row.get("study_accession") or ""),
            secondary_study_accession=str(
                row.get("secondary_study_accession") or ""
            ),
            experiment_accession=str(row.get("experiment_accession") or ""),
            sample_accession=str(row.get("sample_accession") or ""),
            secondary_sample_accession=str(
                row.get("secondary_sample_accession") or ""
            ),
            scientific_name=str(row.get("scientific_name") or ""),
            library_strategy=str(row.get("library_strategy") or ""),
            library_source=str(row.get("library_source") or ""),
            library_selection=str(row.get("library_selection") or ""),
            library_layout=str(row.get("library_layout") or ""),
            instrument_platform=str(row.get("instrument_platform") or ""),
            instrument_model=str(row.get("instrument_model") or ""),
            first_public=str(row.get("first_public") or ""),
            source_ref=receipt.final_url,
            source_sha256=receipt.body_sha256,
        )

    @staticmethod
    def _files(
        row: dict[str, Any], receipt: Any
    ) -> list[RemoteFileCandidate]:
        uris = _split(row.get("fastq_ftp"))
        checksums = _split(row.get("fastq_md5"))
        sizes = _split(row.get("fastq_bytes"))
        roles = _split(row.get("fastq_file_role"))
        layout = str(row.get("library_layout") or "").upper()
        result: list[RemoteFileCandidate] = []
        for index, raw_uri in enumerate(uris):
            uri = _https_file_uri(raw_uri)
            ena_role = _at(roles, index).lower()
            role = (
                ena_role
                if ena_role in {"read1", "read2", "single", "index"}
                else EnaRunResolver._fallback_role(layout, index, len(uris))
            )
            material = {
                "source": "ena",
                "run_accession": row.get("run_accession"),
                "uri": uri,
            }
            result.append(
                RemoteFileCandidate(
                    file_id=stable_id("remote-file", material),
                    source="ena",
                    study_accession=str(row.get("study_accession") or ""),
                    experiment_accession=str(
                        row.get("experiment_accession") or ""
                    ),
                    run_accession=str(row.get("run_accession") or ""),
                    sample_accession=str(row.get("sample_accession") or ""),
                    uri=uri,
                    file_format="fastq.gz",
                    file_role=role,
                    size_bytes=_as_int(_at(sizes, index)),
                    checksum_algorithm="md5" if _at(checksums, index) else "",
                    checksum=_at(checksums, index),
                    source_ref=receipt.final_url,
                    source_sha256=receipt.body_sha256,
                )
            )
        return result

    @staticmethod
    def _fallback_role(layout: str, index: int, count: int) -> str:
        if layout == "PAIRED" and count == 2:
            return "read1" if index == 0 else "read2"
        if layout == "PAIRED":
            return f"paired_file_{index + 1}"
        return "single"

    @staticmethod
    def _edges(row: dict[str, Any], receipt: Any) -> list[AccessionEdge]:
        values = {
            "study": str(
                row.get("secondary_study_accession")
                or row.get("study_accession")
                or ""
            ),
            "experiment": str(row.get("experiment_accession") or ""),
            "run": str(row.get("run_accession") or ""),
            "sample": str(
                row.get("secondary_sample_accession")
                or row.get("sample_accession")
                or ""
            ),
        }
        relationships = (
            ("study", "experiment", "contains_experiment"),
            ("experiment", "run", "contains_run"),
            ("run", "sample", "derived_from_sample"),
        )
        result: list[AccessionEdge] = []
        for source_key, target_key, relation in relationships:
            source = values[source_key]
            target = values[target_key]
            if not source or not target:
                continue
            material = {
                "source": source,
                "target": target,
                "relation": relation,
                "sha256": receipt.body_sha256,
            }
            result.append(
                AccessionEdge(
                    edge_id=stable_id("edge", material),
                    source_node=source,
                    target_node=target,
                    relation=relation,
                    source_ref=receipt.final_url,
                    source_sha256=receipt.body_sha256,
                    method="ena_read_run_relation",
                )
            )
        return result

    @staticmethod
    def _resolution_evidence(
        *,
        record_id: str,
        query_accessions: set[str],
        run_accession: str,
        receipt: Any,
    ) -> CrawlEvidence:
        material = {
            "record_id": record_id,
            "query_accessions": sorted(query_accessions),
            "run_accession": run_accession,
            "source_sha256": receipt.body_sha256,
        }
        return CrawlEvidence(
            evidence_id=stable_id("crawl-ev", material),
            record_id=record_id,
            claim_key="resolved_run_accession",
            observed_value=run_accession,
            strength=EvidenceStrength.STRUCTURAL,
            source_type="official_repository",
            source_ref=receipt.final_url,
            source_locator="/read_run",
            source_sha256=receipt.body_sha256,
            method="ena_portal_read_run_resolver",
        )
