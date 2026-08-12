"""Seed discovery records from accessions supplied explicitly by the user."""

from __future__ import annotations

import hashlib

from ..crawl_models import CrawlEvidence, DiscoveryRecord, stable_id
from ..events import canonical_json
from ..models import EvidenceStrength
from .common import extract_identifiers


class AccessionSeedAdapter:
    name = "accession_seed"
    errors: list[object] = []

    def __init__(self, accessions: list[str]) -> None:
        self.accessions = sorted(
            {item.strip().upper() for item in accessions if item.strip()}
        )
        if not self.accessions:
            raise ValueError("at least one accession is required")

    def discover(self, query: str, limit: int) -> list[DiscoveryRecord]:
        del query
        records: list[DiscoveryRecord] = []
        for accession in self.accessions[:limit]:
            identifiers = extract_identifiers(accession)
            if not identifiers:
                raise ValueError(f"unsupported accession format: {accession}")
            record_id = stable_id(
                "record", {"source": self.name, "source_id": accession}
            )
            source_payload = {"accession": accession, "origin": "user_input"}
            source_sha256 = hashlib.sha256(
                canonical_json(source_payload).encode("utf-8")
            ).hexdigest()
            records.append(
                DiscoveryRecord(
                    record_id=record_id,
                    source=self.name,
                    source_id=accession,
                    canonical_url=self._canonical_url(accession),
                    title=f"User-supplied accession {accession}",
                    summary="Explicit accession seed awaiting official repository resolution.",
                    identifiers=identifiers,
                    metadata={"origin": "user_input"},
                    evidence=[
                        CrawlEvidence(
                            evidence_id=stable_id(
                                "crawl-ev",
                                {
                                    "record_id": record_id,
                                    "accession": accession,
                                    "source_sha256": source_sha256,
                                },
                            ),
                            record_id=record_id,
                            claim_key="candidate_accession",
                            observed_value=accession,
                            strength=EvidenceStrength.DECLARATIVE,
                            source_type="user_input",
                            source_ref="user://crawl-input",
                            source_locator="/accession",
                            source_sha256=source_sha256,
                            method="explicit_accession_seed",
                        )
                    ],
                )
            )
        return records

    @staticmethod
    def _canonical_url(accession: str) -> str:
        if accession.startswith(("GSE", "GSM", "GPL", "GDS")):
            return (
                "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc="
                + accession
            )
        if accession.startswith(
            ("SRP", "SRX", "SRR", "SRS", "ERP", "ERX", "ERR", "ERS", "DRP", "DRX", "DRR", "DRS", "PRJ")
        ):
            return f"https://www.ebi.ac.uk/ena/browser/view/{accession}"
        return f"https://www.ncbi.nlm.nih.gov/search/all/?term={accession}"
