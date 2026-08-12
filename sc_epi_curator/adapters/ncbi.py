"""NCBI Entrez adapters for GEO DataSets and SRA metadata discovery."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..crawl_models import CrawlError, DiscoveryRecord, stable_id
from ..http import FetchError, HttpClient, build_url
from ..models import EvidenceStrength
from .common import (
    collapse_space,
    evidence_for_values,
    extract_identifiers,
    infer_artifact_role,
    merge_identifiers,
    strip_markup,
)


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TITLE_PATTERN = re.compile(r"<Title>(.*?)</Title>", re.IGNORECASE | re.DOTALL)
ORGANISM_PATTERN = re.compile(
    r"<Organism[^>]*>(.*?)</Organism>", re.IGNORECASE | re.DOTALL
)
SOFT_FIELD_PATTERN = re.compile(r"^!(?P<key>[^=]+?)\s*=\s*(?P<value>.*)$")


class NcbiEntrezAdapter:
    def __init__(
        self,
        client: HttpClient,
        *,
        database: str,
        email: str,
        api_key: str | None = None,
        enrich_geo_soft: bool = False,
        max_geo_soft_records: int = 10,
    ) -> None:
        if database not in {"gds", "sra"}:
            raise ValueError("database must be 'gds' or 'sra'")
        if not email:
            raise ValueError("NCBI E-utilities requires a contact email")
        self.client = client
        self.database = database
        self.email = email
        self.api_key = api_key if api_key is not None else os.getenv("NCBI_API_KEY")
        self.name = f"ncbi_{database}"
        self.enrich_geo_soft = enrich_geo_soft
        self.max_geo_soft_records = max(0, max_geo_soft_records)
        self.errors: list[CrawlError] = []

    def _params(self) -> dict[str, Any]:
        return {
            "tool": "cellnote_agent",
            "email": self.email,
            "api_key": self.api_key,
        }

    def discover(self, query: str, limit: int) -> list[DiscoveryRecord]:
        if not query.strip():
            raise ValueError("NCBI query must not be empty")
        if limit < 1:
            return []
        search_url = build_url(
            f"{EUTILS_BASE}/esearch.fcgi",
            {
                "db": self.database,
                "term": query,
                "retmode": "json",
                "retmax": min(limit, 500),
                **self._params(),
            },
        )
        search_payload, search_receipt = self.client.get_json(search_url)
        identifiers = [
            str(item)
            for item in search_payload.get("esearchresult", {}).get("idlist", [])
        ][:limit]
        if not identifiers:
            return []

        summary_url = build_url(
            f"{EUTILS_BASE}/esummary.fcgi",
            {
                "db": self.database,
                "id": ",".join(identifiers),
                "retmode": "json",
                "version": "2.0",
                **self._params(),
            },
        )
        summary_payload, summary_receipt = self.client.get_json(summary_url)
        result = summary_payload.get("result", {})
        records: list[DiscoveryRecord] = []
        for uid in identifiers:
            document = result.get(uid)
            if not isinstance(document, dict):
                continue
            record = self._normalize_document(
                uid,
                document,
                search_receipt=search_receipt,
                summary_receipt=summary_receipt,
            )
            if (
                self.database == "gds"
                and self.enrich_geo_soft
                and len(records) < self.max_geo_soft_records
                and record.source_id.upper().startswith("GSE")
            ):
                self._enrich_geo_soft(record)
            records.append(record)
        return records

    def _normalize_document(
        self,
        uid: str,
        document: dict[str, Any],
        *,
        search_receipt: Any,
        summary_receipt: Any,
    ) -> DiscoveryRecord:
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True)
        expxml = str(document.get("expxml", ""))
        title_match = TITLE_PATTERN.search(expxml)
        title = collapse_space(
            str(document.get("title") or "")
            or (strip_markup(title_match.group(1)) if title_match else "")
            or f"{self.database.upper()} record {uid}"
        )
        summary = collapse_space(
            str(
                document.get("summary")
                or document.get("gdstype")
                or strip_markup(expxml)
                or ""
            )
        )[:4000]
        accession = str(
            document.get("accession")
            or document.get("gse")
            or document.get("caption")
            or ""
        ).strip()
        extracted = extract_identifiers(raw, title, summary, accession)
        if self.database == "gds" and accession.upper().startswith("GSE"):
            extracted = merge_identifiers(
                extracted, {"geo_series": [accession.upper()]}
            )

        source_id = accession or self._preferred_accession(extracted) or uid
        if self.database == "gds" and source_id.upper().startswith(
            ("GSE", "GSM", "GPL", "GDS")
        ):
            canonical_url = (
                "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=" + source_id
            )
        else:
            canonical_url = f"https://www.ncbi.nlm.nih.gov/sra/{source_id}"
        record_id = stable_id(
            "record", {"source": self.name, "source_id": source_id}
        )

        organism_match = ORGANISM_PATTERN.search(expxml)
        organism = (
            strip_markup(organism_match.group(1)) if organism_match else ""
        )
        metadata = {
            "uid": uid,
            "database": self.database,
            "organism": organism,
            "published": document.get("pdat") or document.get("createdate"),
            "updated": document.get("updatedate"),
            "sample_count": document.get("n_samples"),
        }
        evidence = evidence_for_values(
            record_id=record_id,
            claim_key="repository_identifier",
            values=[
                value
                for values in extracted.values()
                for value in values
            ],
            strength=EvidenceStrength.STRUCTURAL,
            source_type="official_repository",
            source_ref=canonical_url,
            source_locator=f"/result/{uid}",
            receipt=summary_receipt,
            method=f"ncbi_esummary_{self.database}",
        )
        return DiscoveryRecord(
            record_id=record_id,
            source=self.name,
            source_id=source_id,
            canonical_url=canonical_url,
            title=title,
            summary=summary,
            identifiers=extracted,
            metadata=metadata,
            evidence=evidence,
            receipts=[search_receipt, summary_receipt],
        )

    def _enrich_geo_soft(self, record: DiscoveryRecord) -> None:
        url = build_url(
            "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi",
            {
                "acc": record.source_id,
                "targ": "self",
                "view": "full",
                "form": "text",
            },
        )
        try:
            text, receipt = self.client.get_text(url, accept="text/plain")
        except FetchError as error:
            self.errors.append(
                CrawlError(
                    source=self.name,
                    stage="geo_soft",
                    target=record.source_id,
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )
            return
        fields: dict[str, list[str]] = {}
        for line in text.splitlines():
            match = SOFT_FIELD_PATTERN.match(line.strip())
            if not match:
                continue
            key = match.group("key").strip()
            value = match.group("value").strip()
            fields.setdefault(key, []).append(value)
        supplementary = sorted(
            set(fields.get("Series_supplementary_file", []))
        )
        relations = sorted(set(fields.get("Series_relation", [])))
        samples = sorted(set(fields.get("Series_sample_id", [])))
        identifiers = extract_identifiers(
            *supplementary,
            *relations,
            *samples,
        )
        record.identifiers = merge_identifiers(record.identifiers, identifiers)
        record.metadata["geo_soft"] = {
            "supplementary_files": [
                {
                    "uri": item,
                    "role_hint": infer_artifact_role(item),
                }
                for item in supplementary
            ],
            "relations": relations,
            "sample_ids": samples,
        }
        record.receipts.append(receipt)
        record.evidence.extend(
            evidence_for_values(
                record_id=record.record_id,
                claim_key="supplementary_file_uri",
                values=supplementary,
                strength=EvidenceStrength.STRUCTURAL,
                source_type="official_repository",
                source_ref=url,
                source_locator="!Series_supplementary_file",
                receipt=receipt,
                method="geo_soft_field_parser",
            )
        )
        record.evidence.extend(
            evidence_for_values(
                record_id=record.record_id,
                claim_key="repository_relation",
                values=relations + samples,
                strength=EvidenceStrength.STRUCTURAL,
                source_type="official_repository",
                source_ref=url,
                source_locator="!Series_relation|!Series_sample_id",
                receipt=receipt,
                method="geo_soft_field_parser",
            )
        )

    @staticmethod
    def _preferred_accession(identifiers: dict[str, list[str]]) -> str:
        for key in (
            "geo_series",
            "sra_study",
            "sra_experiment",
            "sra_run",
            "bioproject",
        ):
            values = identifiers.get(key, [])
            if values:
                return values[0]
        return ""
