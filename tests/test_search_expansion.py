import csv
import json
from pathlib import Path
from types import SimpleNamespace

from cell_note_agent import external_crawlers
from cell_note_agent.agent_cli import AgentConfig, AgentState, build_candidate_catalog, heuristic_candidate_classification
from cell_note_agent.search_expansion import build_search_plan, dataset_record, deduplicate_records


def test_search_plan_separates_display_request_from_retrieval_budget(monkeypatch):
    monkeypatch.delenv("CELLNOTE_SEARCH_RETRIEVAL_LIMIT", raising=False)
    plan = build_search_plan(
        "搜索人类 scATAC 泛癌处理后矩阵",
        {"acquisition": "处理后的矩阵", "candidate_limit_request": "展示 10 个"},
    )
    assert len(plan.core_queries) > 1
    assert len(plan.external_queries) > len(plan.core_queries)
    assert plan.retrieval_limit_per_source == 200


def test_search_plan_all_request_expands_budget(monkeypatch):
    monkeypatch.delenv("CELLNOTE_SEARCH_RETRIEVAL_LIMIT", raising=False)
    plan = build_search_plan("找到所有人类 multiome 数据", {"candidate_limit_request": "所有"})
    assert plan.exhaustive_requested is True
    assert plan.retrieval_limit_per_source == 1000


def test_multiome_plan_has_platform_aliases_and_relaxed_pass():
    plan = build_search_plan(
        "搜索人类 10x Multiome 公开数据集，优先 GRCh38",
        {"species": "Homo sapiens", "data_type": "10x Multiome", "acquisition": "处理后的矩阵或 fragments"},
    )
    joined = "\n".join(plan.external_queries)
    assert "Chromium Single Cell Multiome" in joined
    assert "GEX ATAC" in joined
    assert any("paired RNA ATAC" in query for query in plan.relaxed_queries)


def test_pysradb_search_creates_per_query_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(external_crawlers, "tool_path", lambda _name: "/fake/pysradb")
    monkeypatch.setattr(
        external_crawlers,
        "run_capture",
        lambda _argv, timeout=90: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    target = tmp_path / "nested" / "query"
    assert external_crawlers.pysradb_search("multiome", target, limit=5) == []
    assert (target / "pysradb_sra.stdout.tsv").exists()
    assert (target / "pysradb_geo.stdout.tsv").exists()


def test_record_deduplication_merges_sources_by_accession():
    records = [
        {"source": "A", "source_id": "one", "title": "first", "description": "x", "accessions": ["GSE1"]},
        {"source": "B", "source_id": "two", "title": "second", "description": "longer", "accessions": ["GSE1"]},
    ]
    merged = deduplicate_records(records)
    assert len(merged) == 1
    assert merged[0]["source"] == "A;B"
    assert merged[0]["description"] == "longer"


def test_metadata_only_source_enters_candidate_catalog(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("STEP_API_KEY", raising=False)
    crawl = tmp_path / "crawl"
    crawl.mkdir()
    record = {
        "source": "BioStudies",
        "source_id": "S-BSST1",
        "title": "human single cell ATAC study",
        "description": "scATAC processed matrix",
        "scientific_name": "Homo sapiens",
        "inferred_modality": "scatac_or_atac",
        "access": "public_metadata",
        "landing_url": "https://example.test/S-BSST1",
        "publication_date": "2025",
        "file_count": 0,
        "total_size_bytes": 0,
        "accessions": ["S-BSST1"],
    }
    (crawl / "external_dataset_records.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    config = AgentConfig(repo_root=tmp_path, run_root=tmp_path / "run", processing_python="python3")
    state = AgentState(last_crawl_run=crawl)
    catalog = build_candidate_catalog(config, state, crawl)
    assert catalog is not None
    with catalog.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["study_accession"] == "S-BSST1"
    assert rows[0]["metadata_only"] == "yes"
    assert rows[0]["repository_source"] == "BioStudies"


def test_candidate_size_uses_analysis_ready_files_as_soft_download_estimate(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("STEP_API_KEY", raising=False)
    crawl = tmp_path / "crawl"
    crawl.mkdir()
    files = [
        {"study_accession": "GSE1", "file_id": "matrix", "file_role": "peak_matrix", "uri": "https://example.test/matrix.h5", "size_bytes": 100},
        {"study_accession": "GSE1", "file_id": "raw", "file_role": "fastq", "uri": "https://example.test/raw.fastq.gz", "size_bytes": 10_000},
    ]
    (crawl / "remote_file_candidates.jsonl").write_text("".join(json.dumps(item) + "\n" for item in files), encoding="utf-8")
    config = AgentConfig(repo_root=tmp_path, run_root=tmp_path / "run", processing_python="python3")
    catalog = build_candidate_catalog(config, AgentState(last_crawl_run=crawl), crawl)
    with catalog.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["preferred_file_count"] == "1"
    assert row["total_size_bytes"] == "100"
    assert row["study_total_size_bytes"] == "10100"


def test_unknown_metadata_is_not_reported_as_mismatch():
    result = heuristic_candidate_classification(
        {"best_file_role": "raw", "pipeline_fit": "low", "file_count": 1, "total_size_bytes": 100},
        {"user_preferences": {"species": "Homo sapiens", "data_type": "10x Multiome", "target_genome_build": "GRCh38"}},
    )
    assert result["evidence_status"] in {"partial", "unknown"}
    assert result["mismatch_fields"] == ""
    assert "未确认" in result["unknown_fields"] or "未标注" in result["unknown_fields"]


def test_dataset_record_infers_explicit_species_and_build():
    record = dataset_record(
        "test",
        "E-MTAB-1",
        "Human 10x Multiome atlas",
        description="Processed with GRCh38 and includes paired RNA and ATAC.",
    )
    assert record["scientific_name"] == "Homo sapiens"
    assert record["species_evidence"] == "metadata_text"
    assert record["genome_build"] == "GRCh38"
    assert record["genome_build_evidence"] == "metadata_text"
    assert record["inferred_modality"] == "multiome_or_rna_atac"


def test_existing_modality_is_preserved():
    row = {
        "candidate_id": "1",
        "study_accession": "E-MTAB-1",
        "inferred_modality": "multiome_or_rna_atac",
        "best_file_role": "unknown",
    }
    result = heuristic_candidate_classification(
        row,
        {"user_preferences": {"data_type": "10x Multiome", "species": "Homo sapiens"}},
    )
    assert result["inferred_modality"] == "multiome_or_rna_atac"
