"""Shared normalization helpers for public-data source adapters."""

from __future__ import annotations

import html
import re
from typing import Iterable

from ..crawl_models import CrawlEvidence, FetchReceipt, stable_id
from ..models import EvidenceStrength


IDENTIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "geo_series": re.compile(r"\bGSE\d+\b", re.IGNORECASE),
    "geo_sample": re.compile(r"\bGSM\d+\b", re.IGNORECASE),
    "geo_platform": re.compile(r"\bGPL\d+\b", re.IGNORECASE),
    "sra_study": re.compile(r"\b(?:SRP|ERP|DRP)\d+\b", re.IGNORECASE),
    "sra_experiment": re.compile(r"\b(?:SRX|ERX|DRX)\d+\b", re.IGNORECASE),
    "sra_run": re.compile(r"\b(?:SRR|ERR|DRR)\d+\b", re.IGNORECASE),
    "sra_sample": re.compile(r"\b(?:SRS|ERS|DRS)\d+\b", re.IGNORECASE),
    "bioproject": re.compile(r"\bPRJ(?:NA|EB|DB)\d+\b", re.IGNORECASE),
    "biosample": re.compile(r"\bSAM(?:N|E|D)\d+\b", re.IGNORECASE),
    "pmcid": re.compile(r"\bPMC\d+\b", re.IGNORECASE),
}
DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")
SPACE_PATTERN = re.compile(r"\s+")


def collapse_space(value: str) -> str:
    return SPACE_PATTERN.sub(" ", value).strip()


def strip_markup(value: str) -> str:
    return collapse_space(html.unescape(TAG_PATTERN.sub(" ", value)))


def extract_identifiers(*values: str) -> dict[str, list[str]]:
    text = "\n".join(value for value in values if value)
    result: dict[str, list[str]] = {}
    for key, pattern in IDENTIFIER_PATTERNS.items():
        matches = sorted({match.upper() for match in pattern.findall(text)})
        if matches:
            result[key] = matches
    dois = sorted(
        {
            match.rstrip(".,;:)]}").lower()
            for match in DOI_PATTERN.findall(text)
        }
    )
    if dois:
        result["doi"] = dois
    return result


def merge_identifiers(*groups: dict[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, set[str]] = {}
    for group in groups:
        for key, values in group.items():
            merged.setdefault(key, set()).update(values)
    return {key: sorted(values) for key, values in sorted(merged.items())}


def infer_artifact_role(uri: str) -> str:
    lowered = uri.lower()
    if "fragment" in lowered and lowered.endswith((".gz", ".tsv", ".txt")):
        return "atac_fragments"
    if "peak" in lowered and any(
        marker in lowered for marker in ("matrix", ".h5", ".mtx", ".bed")
    ):
        return "atac_peak_matrix"
    if any(marker in lowered for marker in ("filtered_feature_bc_matrix", "raw_feature_bc_matrix")):
        return "rna_count_matrix"
    if lowered.endswith((".fastq", ".fastq.gz", ".fq", ".fq.gz")):
        return "fastq"
    if any(marker in lowered for marker in ("metadata", "annotation", "celltype")):
        return "cell_metadata"
    if lowered.endswith((".h5ad", ".h5mu", ".rds", ".loom")):
        return "processed_object"
    if lowered.endswith((".mtx", ".mtx.gz", ".h5")):
        return "count_matrix_unknown_modality"
    return "unknown"


def evidence_for_values(
    *,
    record_id: str,
    claim_key: str,
    values: Iterable[str],
    strength: EvidenceStrength,
    source_type: str,
    source_ref: str,
    source_locator: str,
    receipt: FetchReceipt,
    method: str,
) -> list[CrawlEvidence]:
    evidence: list[CrawlEvidence] = []
    for value in sorted(set(values)):
        material = {
            "record_id": record_id,
            "claim_key": claim_key,
            "observed_value": value,
            "source_ref": source_ref,
            "source_locator": source_locator,
            "source_sha256": receipt.body_sha256,
            "method": method,
        }
        evidence.append(
            CrawlEvidence(
                evidence_id=stable_id("crawl-ev", material),
                record_id=record_id,
                claim_key=claim_key,
                observed_value=value,
                strength=strength,
                source_type=source_type,
                source_ref=source_ref,
                source_locator=source_locator,
                source_sha256=receipt.body_sha256,
                method=method,
            )
        )
    return evidence
