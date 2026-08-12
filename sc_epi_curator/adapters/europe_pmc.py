"""Europe PMC metadata and open-access full-text discovery adapter."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from ..crawl_models import CrawlError, DiscoveryRecord, stable_id
from ..http import FetchError, HttpClient, build_url
from ..models import EvidenceStrength
from .common import (
    collapse_space,
    evidence_for_values,
    extract_identifiers,
    merge_identifiers,
)


EUROPE_PMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def _is_yes(value: Any) -> bool:
    return str(value).strip().lower() in {"y", "yes", "true", "1"}


class EuropePmcAdapter:
    name = "europe_pmc"

    def __init__(
        self,
        client: HttpClient,
        *,
        email: str = "",
        include_open_access_full_text: bool = False,
        max_full_text_articles: int = 3,
    ) -> None:
        self.client = client
        self.email = email
        self.include_open_access_full_text = include_open_access_full_text
        self.max_full_text_articles = max(0, max_full_text_articles)
        self.errors: list[CrawlError] = []

    def discover(self, query: str, limit: int) -> list[DiscoveryRecord]:
        if not query.strip():
            raise ValueError("Europe PMC query must not be empty")
        if limit < 1:
            return []
        url = build_url(
            f"{EUROPE_PMC_BASE}/search",
            {
                "query": query,
                "format": "json",
                "resultType": "core",
                "pageSize": min(limit, 1000),
                "email": self.email or None,
            },
        )
        payload, search_receipt = self.client.get_json(url)
        results = payload.get("resultList", {}).get("result", [])
        if not isinstance(results, list):
            return []
        records: list[DiscoveryRecord] = []
        full_text_count = 0
        for document in results[:limit]:
            if not isinstance(document, dict):
                continue
            allow_full_text = (
                self.include_open_access_full_text
                and full_text_count < self.max_full_text_articles
                and _is_yes(document.get("isOpenAccess"))
                and bool(document.get("pmcid"))
            )
            record = self._normalize_document(
                document,
                search_receipt,
                include_full_text=allow_full_text,
            )
            if len(record.receipts) > 1:
                full_text_count += 1
            records.append(record)
        return records

    def _normalize_document(
        self,
        document: dict[str, Any],
        search_receipt: Any,
        *,
        include_full_text: bool,
    ) -> DiscoveryRecord:
        pmcid = str(document.get("pmcid") or "").upper()
        pmid = str(document.get("pmid") or document.get("id") or "")
        doi = str(document.get("doi") or "").lower()
        source_id = pmcid or pmid or doi or str(document.get("id") or "")
        record_id = stable_id(
            "record", {"source": self.name, "source_id": source_id}
        )
        title = collapse_space(str(document.get("title") or "Untitled publication"))
        abstract = collapse_space(str(document.get("abstractText") or ""))[:12_000]
        identifiers = extract_identifiers(title, abstract, doi, pmcid)
        explicit: dict[str, list[str]] = {}
        if doi:
            explicit["doi"] = [doi]
        if pmid:
            explicit["pmid"] = [pmid]
        if pmcid:
            explicit["pmcid"] = [pmcid]
        identifiers = merge_identifiers(identifiers, explicit)
        canonical_url = (
            f"https://europepmc.org/article/MED/{pmid}"
            if pmid
            else f"https://europepmc.org/article/PMC/{pmcid.removeprefix('PMC')}"
        )
        receipts = [search_receipt]
        evidence = evidence_for_values(
            record_id=record_id,
            claim_key="literature_identifier",
            values=[value for values in explicit.values() for value in values],
            strength=EvidenceStrength.STRUCTURAL,
            source_type="literature_index",
            source_ref=canonical_url,
            source_locator="/resultList/result",
            receipt=search_receipt,
            method="europe_pmc_search_core",
        )
        mentioned = [
            value
            for key, values in identifiers.items()
            if key not in {"doi", "pmid", "pmcid"}
            for value in values
        ]
        evidence.extend(
            evidence_for_values(
                record_id=record_id,
                claim_key="mentioned_accession",
                values=mentioned,
                strength=EvidenceStrength.DECLARATIVE,
                source_type="literature_abstract",
                source_ref=canonical_url,
                source_locator="/resultList/result/abstractText",
                receipt=search_receipt,
                method="accession_regex_from_abstract",
            )
        )

        full_text_sections: list[str] = []
        if include_full_text and pmcid:
            full_text_url = f"{EUROPE_PMC_BASE}/{pmcid}/fullTextXML"
            try:
                full_text, receipt = self.client.get_text(
                    full_text_url, accept="application/xml, text/xml;q=0.9"
                )
            except FetchError as error:
                self.errors.append(
                    CrawlError(
                        source=self.name,
                        stage="open_access_full_text",
                        target=pmcid,
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )
            else:
                receipts.append(receipt)
                full_text_sections = self._data_sections(full_text)
                full_text_identifiers = extract_identifiers(*full_text_sections)
                identifiers = merge_identifiers(identifiers, full_text_identifiers)
                mentioned_full_text = [
                    value
                    for values in full_text_identifiers.values()
                    for value in values
                ]
                evidence.extend(
                    evidence_for_values(
                        record_id=record_id,
                        claim_key="mentioned_accession",
                        values=mentioned_full_text,
                        strength=EvidenceStrength.DECLARATIVE,
                        source_type="open_access_full_text",
                        source_ref=full_text_url,
                        source_locator="/article/body/sec[data-availability]",
                        receipt=receipt,
                        method="accession_regex_from_oa_data_sections",
                    )
                )

        metadata = {
            "authors": document.get("authorString"),
            "journal": document.get("journalTitle"),
            "publication_date": document.get("firstPublicationDate"),
            "publication_types": document.get("pubTypeList", {}).get("pubType", []),
            "open_access": _is_yes(document.get("isOpenAccess")),
            "has_full_text": _is_yes(document.get("inEPMC")),
            "data_sections_found": len(full_text_sections),
        }
        return DiscoveryRecord(
            record_id=record_id,
            source=self.name,
            source_id=source_id,
            canonical_url=canonical_url,
            title=title,
            summary=abstract,
            identifiers=identifiers,
            metadata=metadata,
            evidence=evidence,
            receipts=receipts,
        )

    @staticmethod
    def _data_sections(full_text_xml: str) -> list[str]:
        try:
            root = ET.fromstring(full_text_xml)
        except ET.ParseError:
            return []
        sections: list[str] = []
        for section in root.findall(".//sec"):
            title = collapse_space(" ".join(section.findtext("title", "").split()))
            normalized = title.lower()
            if not any(
                marker in normalized
                for marker in (
                    "data availability",
                    "availability of data",
                    "data and materials availability",
                    "accession",
                    "data deposition",
                )
            ):
                continue
            text = collapse_space(" ".join(section.itertext()))
            if text:
                sections.append(text[:30_000])
        return sections
