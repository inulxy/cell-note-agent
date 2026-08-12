"""Broad, evidence-preserving metadata discovery for CellNoteAgent."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


USER_AGENT = "CellNoteAgent broad-search/0.1"
ACCESSION_RE = re.compile(
    r"\b(?:GSE|GSM|SRP|ERP|DRP|PRJNA|PRJEB|EGAS|EGAD|ENCSR|S-BSST)\d+\b",
    re.IGNORECASE,
)

BIOLOGY_HINT_TRANSLATIONS = {
    "溃疡性结肠炎": "ulcerative colitis",
    "炎症性肠病": "inflammatory bowel disease",
    "克罗恩病": "Crohn disease",
    "克罗恩": "Crohn disease",
    "结肠炎": "colitis",
    "白血病": "leukemia",
    "淋巴瘤": "lymphoma",
    "阿尔茨海默": "Alzheimer disease",
    "帕金森": "Parkinson disease",
    "糖尿病": "diabetes",
    "哮喘": "asthma",
    "肺炎": "pneumonia",
    "肝硬化": "liver cirrhosis",
    "外周血": "peripheral blood",
    "脑": "brain",
    "心脏": "heart",
    "肝": "liver",
    "肺": "lung",
    "肾": "kidney",
    "系统性硬化症": "systemic sclerosis",
    "白塞病": "Behcet disease",
    "系统性红斑狼疮": "systemic lupus erythematosus",
    "类风湿关节炎": "rheumatoid arthritis",
}

BIOLOGY_SYNONYM_GROUPS = {
    "ulcerative colitis": ["ulcerative colitis", "UC", "ulcerative proctocolitis"],
    "inflammatory bowel disease": ["inflammatory bowel disease", "IBD", "Crohn disease", "ulcerative colitis"],
    "crohn disease": ["Crohn disease", "Crohn's disease", "regional enteritis"],
    "systemic sclerosis": ["systemic sclerosis", "scleroderma", "systemic scleroderma", "SSc"],
    "behcet disease": ["Behcet disease", "Behcet's disease", "Behçet disease"],
    "systemic lupus erythematosus": ["systemic lupus erythematosus", "SLE", "lupus"],
    "rheumatoid arthritis": ["rheumatoid arthritis", "RA"],
    "alzheimer disease": ["Alzheimer disease", "Alzheimer's disease", "AD"],
    "parkinson disease": ["Parkinson disease", "Parkinson's disease", "PD"],
    "peripheral blood": ["peripheral blood", "PBMC", "peripheral blood mononuclear cell"],
}

# Broader concepts are retrieval-only. They increase recall but are deliberately
# excluded from semantic entity evidence, so an IBD hit is not treated as proof
# that the study specifically contains ulcerative-colitis samples.
BIOLOGY_RETRIEVAL_BROADENING = {
    "ulcerative colitis": ["inflammatory bowel disease", "IBD"],
    "crohn disease": ["inflammatory bowel disease", "IBD"],
}


def expand_biology_hint(value: str) -> str:
    """Translate common Chinese tissue/disease hints for public metadata APIs."""
    text = " ".join(str(value or "").split()).strip()
    lowered = text.lower()
    for source, target in BIOLOGY_HINT_TRANSLATIONS.items():
        if source.lower() in lowered:
            return target
    return text


def biology_query_terms(value: str, extra_aliases: list[str] | None = None, *, maximum: int = 8) -> list[str]:
    """Return transparent query variants without treating aliases as verified facts."""
    canonical = expand_biology_hint(value)
    values = [canonical]
    values.extend(BIOLOGY_SYNONYM_GROUPS.get(canonical.casefold(), []))
    values.extend(str(item or "").strip() for item in (extra_aliases or []))
    safe: list[str] = []
    for item in values:
        cleaned = " ".join(item.split()).strip()[:120]
        if not cleaned or any(blocked in cleaned.lower() for blocked in ("http://", "https://", "/ssd/", "api_key", "token=")):
            continue
        if cleaned.casefold() not in {existing.casefold() for existing in safe}:
            safe.append(cleaned)
        if len(safe) >= maximum:
            break
    return safe


@dataclass(frozen=True)
class SearchPlan:
    user_query: str
    core_queries: list[str]
    external_queries: list[str]
    retrieval_limit_per_source: int
    exhaustive_requested: bool
    biological_terms: list[str]
    relaxed_queries: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_query": self.user_query,
            "core_queries": self.core_queries,
            "external_queries": self.external_queries,
            "retrieval_limit_per_source": self.retrieval_limit_per_source,
            "exhaustive_requested": self.exhaustive_requested,
            "biological_terms": self.biological_terms,
            "relaxed_queries": self.relaxed_queries,
        }


def _unique(values: list[str], *, maximum: int) -> list[str]:
    output: list[str] = []
    for value in values:
        normalized = " ".join(value.split()).strip()
        if normalized and normalized.lower() not in {item.lower() for item in output}:
            output.append(normalized)
        if len(output) >= maximum:
            break
    return output


def build_search_plan(user_query: str, preferences: dict[str, Any] | None = None) -> SearchPlan:
    preferences = preferences or {}
    joined = " ".join([user_query, *(str(value) for value in preferences.values())]).lower()
    # A confirmed structured value is authoritative. Species names must never be
    # inferred from an arbitrary substring of disease names (e.g. ulceRATive).
    explicit_species = str(preferences.get("species") or "").strip()
    normalized_species = {
        "human": "Homo sapiens",
        "homo sapiens": "Homo sapiens",
        "人类": "Homo sapiens",
        "mouse": "Mus musculus",
        "mus musculus": "Mus musculus",
        "小鼠": "Mus musculus",
        "rat": "Rattus norvegicus",
        "rattus norvegicus": "Rattus norvegicus",
        "大鼠": "Rattus norvegicus",
    }
    if explicit_species:
        species = normalized_species.get(explicit_species.casefold(), explicit_species)
    else:
        query_text = user_query.casefold()
        if "小鼠" in query_text or re.search(r"(?<![a-z])(mouse|mice|murine|mus musculus)(?![a-z])", query_text):
            species = "Mus musculus"
        elif "大鼠" in query_text or re.search(r"(?<![a-z])(rat|rats|rattus norvegicus)(?![a-z])", query_text):
            species = "Rattus norvegicus"
        elif "人类" in query_text or re.search(r"(?<![a-z])(human|humans|homo sapiens)(?![a-z])", query_text):
            species = "Homo sapiens"
        else:
            species = ""
    multiome_requested = any(term in joined for term in ("multiome", "multi-ome", "多组"))
    ten_x_requested = "10x" in joined or "10 x" in joined or "chromium" in joined
    if multiome_requested:
        modalities = [
            "10x Genomics Single Cell Multiome ATAC Gene Expression",
            "Chromium Single Cell Multiome ATAC Gene Expression",
            "single cell multiome ATAC RNA",
            "paired RNA chromatin accessibility single cell",
            "joint profiling RNA chromatin accessibility",
            "GEX ATAC",
        ]
        if not ten_x_requested:
            modalities.extend(["paired scATAC scRNA", "SHARE-seq SNARE-seq"])
    elif any(term in joined for term in ("scatac", "single cell atac", "snatac", "染色质")):
        modalities = ["single cell ATAC-seq", "scATAC-seq", "snATAC-seq"]
    else:
        modalities = ["single cell ATAC-seq", "10x Multiome", "single cell chromatin accessibility"]

    disease_terms: list[str] = []
    evidence_biology_terms: list[str] = []
    if any(term in joined for term in ("cancer", "tumor", "tumour", "泛癌", "肿瘤", "癌")):
        disease_terms = ["cancer", "tumor", "leukemia glioma carcinoma"]
    alias_value = preferences.get("biology_aliases") or []
    if isinstance(alias_value, str):
        alias_value = re.split(r"[,;，；\n]+", alias_value)
    aliases = [str(item) for item in alias_value if str(item).strip()] if isinstance(alias_value, list) else []
    tissue_hint = expand_biology_hint(str(preferences.get("tissue_hint") or preferences.get("user_note") or ""))
    if tissue_hint:
        evidence_biology_terms = biology_query_terms(tissue_hint, aliases)
        broad_terms = BIOLOGY_RETRIEVAL_BROADENING.get(tissue_hint.casefold(), [])
        # Keep canonical and a short exact alias first, then add the broader
        # disease family before less common exact synonyms.
        disease_terms = [
            *evidence_biology_terms[:2],
            *broad_terms,
            *evidence_biology_terms[2:],
            *disease_terms,
        ]
    elif user_query and not re.search(r"[\u4e00-\u9fff]", user_query):
        disease_terms.insert(0, user_query)

    core: list[str] = []
    for modality in modalities:
        # Specific biological intent must run before broad modality searches.
        # Several public APIs stop once a per-source limit is reached, so putting
        # the generic query first can crowd disease-relevant records out entirely.
        for disease in disease_terms[:3]:
            core.append(" ".join(term for term in (species, modality, disease) if term))
        core.append(" ".join(term for term in (species, modality) if term))

    external = [*core]
    acquisition = str(preferences.get("acquisition") or "").lower()
    if any(term in acquisition for term in ("处理后", "matrix", "fragment")) or not acquisition:
        for query in core[:4]:
            external.extend([
                f"{query} fragments peak matrix",
                f"{query} processed data supplementary",
                f"{query} filtered_feature_bc_matrix atac_fragments",
            ])

    relaxed: list[str] = []
    if multiome_requested:
        relaxed = [
            " ".join(term for term in (species, "10x Multiome") if term),
            " ".join(term for term in (species, "Chromium Multiome Gene Expression ATAC") if term),
            " ".join(term for term in (species, "paired RNA ATAC single cell") if term),
            " ".join(term for term in (species, "single cell chromatin accessibility transcriptome") if term),
        ]
    else:
        relaxed = [
            " ".join(term for term in (species, "single cell chromatin accessibility") if term),
            " ".join(term for term in (species, "scATAC-seq") if term),
        ]

    request = " ".join(
        str(preferences.get(key) or "")
        for key in ("candidate_limit_request", "candidate_limit", "user_note")
    ).lower()
    exhaustive = any(term in request for term in ("所有", "全部", "不限", "all", "everything"))
    default_limit = 1000 if exhaustive else 200
    configured = os.environ.get("CELLNOTE_SEARCH_RETRIEVAL_LIMIT", "").strip()
    try:
        retrieval_limit = max(20, int(configured)) if configured else default_limit
    except ValueError:
        retrieval_limit = default_limit
    return SearchPlan(
        user_query=user_query,
        core_queries=_unique(core, maximum=10),
        external_queries=_unique(external, maximum=18),
        retrieval_limit_per_source=retrieval_limit,
        exhaustive_requested=exhaustive,
        biological_terms=_unique(evidence_biology_terms or disease_terms, maximum=8),
        relaxed_queries=_unique(relaxed, maximum=6),
    )


def fetch_json(
    url: str,
    *,
    timeout: int = 45,
    payload: dict[str, Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with opener(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def infer_modality(text: str) -> str:
    lowered = text.lower()
    if "multiome" in lowered or ("rna" in lowered and "atac" in lowered):
        return "multiome_or_rna_atac"
    if any(term in lowered for term in ("scatac", "snatac", "single cell atac", "chromatin accessibility")):
        return "scatac_or_atac"
    if "atac" in lowered:
        return "atac_relevance_uncertain"
    return "unknown_atac_relevance"


def dataset_record(
    source: str,
    source_id: str,
    title: str,
    *,
    description: str = "",
    species: str = "",
    landing_url: str = "",
    access: str = "public_metadata",
    genome_build: str = "",
    publication_date: str = "",
    file_count: int = 0,
    total_size_bytes: int = 0,
) -> dict[str, Any]:
    evidence = " ".join((source_id, title, description))
    species_evidence = "source_metadata" if species else ""
    genome_build_evidence = "source_metadata" if genome_build else ""
    if not species:
        if re.search(r"\bHomo sapiens\b|\bhuman\b", evidence, re.IGNORECASE):
            species = "Homo sapiens"
            species_evidence = "metadata_text"
        elif re.search(r"\bMus musculus\b|\bmouse\b", evidence, re.IGNORECASE):
            species = "Mus musculus"
            species_evidence = "metadata_text"
    if not genome_build:
        if re.search(r"\b(?:GRCh38|hg38)\b", evidence, re.IGNORECASE):
            genome_build = "GRCh38"
            genome_build_evidence = "metadata_text"
        elif re.search(r"\b(?:GRCh37|hg19)\b", evidence, re.IGNORECASE):
            genome_build = "GRCh37"
            genome_build_evidence = "metadata_text"
    return {
        "source": source,
        "source_id": source_id or landing_url or title,
        "title": title,
        "description": description[:4000],
        "scientific_name": species,
        "species_evidence": species_evidence,
        "inferred_modality": infer_modality(evidence),
        "genome_build": genome_build,
        "genome_build_evidence": genome_build_evidence,
        "access": access,
        "landing_url": landing_url,
        "publication_date": publication_date,
        "file_count": file_count,
        "total_size_bytes": total_size_bytes,
        "accessions": sorted(set(match.upper() for match in ACCESSION_RE.findall(evidence))),
    }


def _matches_atac(record: dict[str, Any]) -> bool:
    text = " ".join(str(record.get(key) or "") for key in ("title", "description", "inferred_modality")).lower()
    return any(term in text for term in ("atac", "chromatin accessibility", "multiome"))


def omicsdi_search(queries: list[str], *, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_size = min(100, limit)
    for query in queries:
        for start in range(0, limit, page_size):
            url = "https://www.omicsdi.org/ws/dataset/search?" + urllib.parse.urlencode(
                {"query": query, "start": start, "size": min(page_size, limit - start)}
            )
            payload = fetch_json(url)
            items = (payload.get("datasets") or []) if isinstance(payload, dict) else []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                source_id = str(item.get("id") or item.get("accession") or "")
                repository = str(item.get("database") or item.get("repository") or "OmicsDI")
                record = dataset_record(
                    f"OmicsDI/{repository}",
                    source_id,
                    str(item.get("title") or item.get("name") or source_id),
                    description=str(item.get("description") or item.get("omics_type") or ""),
                    landing_url=f"https://www.omicsdi.org/dataset/{urllib.parse.quote(repository)}/{urllib.parse.quote(source_id)}",
                )
                if _matches_atac(record):
                    records.append(record)
                    if len(records) >= limit:
                        return records
            if len(items) < page_size:
                break
    return records


def biostudies_search(queries: list[str], *, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_size = min(100, limit)
    for query in queries:
        for page in range(1, (limit + page_size - 1) // page_size + 1):
            url = "https://www.ebi.ac.uk/biostudies/api/v1/search?" + urllib.parse.urlencode(
                {"query": query, "page": page, "pageSize": page_size}
            )
            payload = fetch_json(url)
            items = (payload.get("hits") or payload.get("studies") or []) if isinstance(payload, dict) else []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                accession = str(item.get("accession") or item.get("accno") or item.get("id") or "")
                record = dataset_record(
                    "BioStudies",
                    accession,
                    str(item.get("title") or item.get("name") or accession),
                    description=str(item.get("description") or item.get("content") or ""),
                    landing_url=f"https://www.ebi.ac.uk/biostudies/studies/{accession}" if accession else "",
                    publication_date=str(item.get("release_date") or item.get("releaseDate") or ""),
                )
                if _matches_atac(record):
                    records.append(record)
                    if len(records) >= limit:
                        return records
            if len(items) < page_size:
                break
    return records


def encode_search(queries: list[str], *, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for query in queries[:3]:
        parameters = {"type": "Experiment", "status": "released", "assay_title": "ATAC-seq", "format": "json", "frame": "object", "limit": min(limit, 200)}
        if any(term in query.lower() for term in ("human", "homo sapiens")):
            parameters["replicates.library.biosample.donor.organism.scientific_name"] = "Homo sapiens"
        url = "https://www.encodeproject.org/search/?" + urllib.parse.urlencode(parameters)
        payload = fetch_json(url)
        for item in payload.get("@graph", []) if isinstance(payload, dict) else []:
            accession = str(item.get("accession") or "")
            title = str(item.get("description") or item.get("assay_title") or accession)
            record = dataset_record(
                "ENCODE",
                accession,
                title,
                description=json.dumps({"assay_title": item.get("assay_title"), "biosample": item.get("biosample_summary")}, ensure_ascii=False),
                species="",
                landing_url=f"https://www.encodeproject.org/experiments/{accession}/" if accession else "",
                genome_build="GRCh38" if "GRCh38" in json.dumps(item) else "",
            )
            if _matches_atac(record):
                records.append(record)
                if len(records) >= limit:
                    return records, files
    return records, files


def single_cell_portal_search(queries: list[str], *, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for query in queries:
        url = "https://singlecell.broadinstitute.org/single_cell/api/v1/site/studies?" + urllib.parse.urlencode(
            {"search": query, "limit": min(limit, 200)}
        )
        payload = fetch_json(url)
        items = (payload.get("studies") or payload.get("data") or []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
        if isinstance(items, dict):
            items = items.get("studies") or items.get("results") or []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            study_id = str(item.get("accession") or item.get("id") or item.get("url_safe_name") or "")
            record = dataset_record(
                "Broad Single Cell Portal",
                study_id,
                str(item.get("name") or item.get("title") or study_id),
                description=str(item.get("description") or item.get("summary") or ""),
                species=str(item.get("species") or ""),
                landing_url=f"https://singlecell.broadinstitute.org/single_cell/study/{study_id}" if study_id else "",
            )
            if _matches_atac(record):
                records.append(record)
                if len(records) >= limit:
                    return records
    return records


def hubmap_search(queries: list[str], *, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for query in queries:
        payload = fetch_json(
            "https://search.api.hubmapconsortium.org/v3/portal/search",
            payload={"query": {"multi_match": {"query": query, "fields": ["title^3", "description", "data_types", "organ"]}}, "size": min(limit, 200)},
        )
        hits = ((payload.get("hits") or {}).get("hits") or []) if isinstance(payload, dict) else []
        for hit in hits:
            item = hit.get("_source") or {}
            dataset_id = str(item.get("hubmap_id") or item.get("uuid") or hit.get("_id") or "")
            record = dataset_record(
                "HuBMAP",
                dataset_id,
                str(item.get("title") or item.get("description") or dataset_id),
                description=json.dumps({"data_types": item.get("data_types"), "organ": item.get("organ")}, ensure_ascii=False),
                species=str(item.get("species") or "Homo sapiens"),
                landing_url=f"https://portal.hubmapconsortium.org/browse/dataset/{item.get('uuid')}" if item.get("uuid") else "",
            )
            if _matches_atac(record):
                records.append(record)
                if len(records) >= limit:
                    return records
    return records


def gdc_search(*, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    filters = {"op": "in", "content": {"field": "files.experimental_strategy", "value": ["ATAC-Seq"]}}
    fields = ["file_id", "file_name", "file_size", "md5sum", "access", "data_format", "experimental_strategy", "cases.project.project_id"]
    payload = fetch_json(
        "https://api.gdc.cancer.gov/files",
        payload={"filters": filters, "format": "JSON", "fields": ",".join(fields), "size": limit},
    )
    records: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    for item in ((payload.get("data") or {}).get("hits") or []) if isinstance(payload, dict) else []:
        projects = sorted({str(case.get("project", {}).get("project_id") or "") for case in item.get("cases") or [] if case.get("project")})
        study = projects[0] if projects else str(item.get("file_id") or "GDC-ATAC")
        records.setdefault(study, dataset_record("GDC", study, f"GDC ATAC dataset {study}", description=";".join(projects), landing_url="https://portal.gdc.cancer.gov/", access=str(item.get("access") or "unknown")))
        if str(item.get("access") or "").lower() == "open":
            file_id = str(item.get("file_id") or "")
            files.append({
                "file_id": file_id,
                "source": "gdc",
                "study_accession": study,
                "uri": f"https://api.gdc.cancer.gov/data/{file_id}",
                "file_format": str(item.get("data_format") or "unknown").lower(),
                "file_role": "processed_atac_file",
                "size_bytes": int(item.get("file_size") or 0),
                "checksum_algorithm": "md5" if item.get("md5sum") else "",
                "checksum": str(item.get("md5sum") or ""),
                "filename": str(item.get("file_name") or file_id),
                "source_ref": "GDC files API",
                "source_sha256": "",
            })
    return list(records.values()), files


def ega_search(queries: list[str], *, limit: int) -> list[dict[str, Any]]:
    records = omicsdi_search([f"{query} AND repository:\"EGA\"" for query in queries], limit=limit)
    for record in records:
        record["source"] = "EGA metadata"
        record["access"] = "controlled_or_metadata_only"
    return records


def cellxgene_search(queries: list[str], *, limit: int) -> list[dict[str, Any]]:
    payload = fetch_json("https://api.cellxgene.cziscience.com/curation/v1/collections?visibility=PUBLIC")
    collections = (payload.get("collections") or []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
    query_terms = {term.lower() for query in queries for term in query.split() if len(term) > 3}
    records: list[dict[str, Any]] = []
    for item in collections if isinstance(collections, list) else []:
        if not isinstance(item, dict):
            continue
        text = json.dumps(item, ensure_ascii=False).lower()
        if not any(term in text for term in query_terms) or not any(term in text for term in ("atac", "multiome", "chromatin")):
            continue
        collection_id = str(item.get("collection_id") or item.get("id") or "")
        records.append(dataset_record(
            "CELLxGENE Discover",
            collection_id,
            str(item.get("name") or item.get("title") or collection_id),
            description=str(item.get("description") or ""),
            landing_url=f"https://cellxgene.cziscience.com/collections/{collection_id}" if collection_id else "",
        ))
        if len(records) >= limit:
            break
    return records


def deduplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    accession_owner: dict[str, str] = {}
    for record in records:
        source_id = str(record.get("source_id") or "").strip().upper()
        accessions = [str(item).upper() for item in record.get("accessions") or []]
        key = next((accession_owner[item] for item in accessions if item in accession_owner), "")
        if not key:
            key = source_id or f"{record.get('source')}::{str(record.get('title') or '').lower()}"
        if key not in output:
            output[key] = record
        else:
            current = output[key]
            current["source"] = ";".join(dict.fromkeys([str(current.get("source") or ""), str(record.get("source") or "")]))
            current["accessions"] = sorted(set([*(current.get("accessions") or []), *accessions]))
            if len(str(record.get("description") or "")) > len(str(current.get("description") or "")):
                current["description"] = record.get("description")
        for accession in accessions:
            accession_owner[accession] = key
    return list(output.values())


def run_official_sources(
    queries: list[str],
    *,
    limit_per_source: int,
    progress: Callable[[str, str, int], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    adapters: list[tuple[str, Callable[[], Any]]] = [
        ("omicsdi", lambda: (omicsdi_search(queries, limit=limit_per_source), [])),
        ("biostudies", lambda: (biostudies_search(queries, limit=limit_per_source), [])),
        ("encode", lambda: encode_search(queries, limit=limit_per_source)),
        ("single_cell_portal", lambda: (single_cell_portal_search(queries, limit=limit_per_source), [])),
        ("hubmap", lambda: (hubmap_search(queries, limit=limit_per_source), [])),
        ("ega", lambda: (ega_search(queries, limit=limit_per_source), [])),
        ("cellxgene", lambda: (cellxgene_search(queries, limit=limit_per_source), [])),
    ]
    query_text = " ".join(queries).lower()
    if any(term in query_text for term in ("cancer", "tumor", "tumour", "leukemia", "lymphoma", "glioma", "carcinoma")):
        adapters.insert(-2, ("gdc", lambda: gdc_search(limit=limit_per_source)))
    source_counts: dict[str, int] = {}
    for index, (name, adapter) in enumerate(adapters, 1):
        if progress:
            progress(name, "running", int((index - 1) / len(adapters) * 100))
        try:
            source_records, source_files = adapter()
            records.extend(source_records)
            files.extend(source_files)
            source_counts[name] = len(source_records)
            if progress:
                progress(name, "completed", int(index / len(adapters) * 100))
        except Exception as error:
            errors[name] = str(error)
            source_counts[name] = 0
            if progress:
                progress(name, "failed", int(index / len(adapters) * 100))
    deduplicated = deduplicate_records(records)
    return deduplicated, files, {"source_counts": source_counts, "errors": errors, "record_count": len(deduplicated), "file_count": len(files)}


def write_official_outputs(run_dir: Path, records: list[dict[str, Any]], files: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    out_dir = run_dir / "external_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "external_dataset_records.jsonl").open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with (out_dir / "official_source_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
