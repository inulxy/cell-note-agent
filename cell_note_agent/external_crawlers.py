"""Optional external crawler/search tool adapters for CellNoteAgent.

These adapters intentionally write side-car artifacts next to a normal CellNote
crawl run instead of mutating the audited crawler outputs. The agent can then
merge those side-car files into its candidate catalog while the core crawl event
chain remains intact.
"""
from __future__ import annotations

import argparse
import csv
import concurrent.futures
import json
import os
import re
import shutil
import site
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cell_note_agent.search_expansion import run_official_sources, write_official_outputs

USER_AGENT = "CellNoteAgent external-crawlers/0.1"
ACCESSION_RE = re.compile(r"\b(?:GSE|SRP|ERP|DRP|PRJNA|PRJEB|SRR|ERR|DRR|SAMN|SRS)\d+\b", re.IGNORECASE)
GEO_SERIES_RE = re.compile(r"\bGSE\d+\b", re.IGNORECASE)
RUN_RE = re.compile(r"\b(?:SRR|ERR|DRR)\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class ToolStatus:
    name: str
    available: bool
    command: str
    version: str = ""
    error: str = ""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_tsv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        names: list[str] = []
        for row in rows:
            for key in row:
                if key not in names:
                    names.append(key)
        fieldnames = names
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def user_script_dirs() -> list[Path]:
    dirs = [Path.home() / ".local" / "bin"]
    try:
        dirs.append(Path(site.USER_BASE) / "bin")
    except Exception:
        pass
    return list(dict.fromkeys(dirs))


def tool_path(name: str) -> str | None:
    explicit = os.environ.get(f"CELLNOTE_{name.upper()}_BIN")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which(name)
    if found:
        return found
    for directory in user_script_dirs():
        local = directory / name
        if local.exists():
            return str(local)
    return None


def import_status(module: str) -> tuple[bool, str]:
    try:
        imported = __import__(module)
        version = getattr(imported, "__version__", "ok")
        return True, str(version)
    except Exception as error:
        return False, str(error)


def run_capture(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    script_bins = os.pathsep.join(str(path) for path in user_script_dirs())
    env["PATH"] = script_bins + os.pathsep + env.get("PATH", "")
    return subprocess.run(argv, check=False, text=True, capture_output=True, timeout=timeout, env=env)


def check_tools() -> dict[str, dict[str, Any]]:
    statuses: list[ToolStatus] = []
    for command in ["pysradb", "ffq"]:
        path = tool_path(command)
        if not path:
            statuses.append(ToolStatus(command, False, command, error="not found on PATH or ~/.local/bin"))
            continue
        version_args = [path, "--version"] if command == "pysradb" else [path, "--version"]
        try:
            completed = run_capture(version_args, timeout=20)
            output = (completed.stdout or completed.stderr).strip().splitlines()
            version = output[0] if output else "available"
            statuses.append(ToolStatus(command, completed.returncode == 0, path, version=version, error=(completed.stderr or "").strip() if completed.returncode else ""))
        except Exception as error:
            statuses.append(ToolStatus(command, False, path, error=str(error)))
    for module in ["GEOparse", "requests"]:
        ok, detail = import_status(module)
        statuses.append(ToolStatus(module, ok, f"python import {module}", version=detail if ok else "", error="" if ok else detail))
    return {item.name: item.__dict__ for item in statuses}


def parse_tsv(text: str) -> list[dict[str, str]]:
    clean_lines = [line for line in text.splitlines() if line.strip() and not line.startswith("[") and "\t" in line]
    if not clean_lines:
        return []
    return list(csv.DictReader(clean_lines, delimiter="\t"))


def pysradb_search(query: str, out_dir: Path, *, limit: int = 50, dbs: list[str] | None = None) -> list[dict[str, Any]]:
    path = tool_path("pysradb")
    if not path:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    selected_dbs = dbs or ["sra", "geo"]
    for db in selected_dbs:
        if db == "geo":
            argv = [path, "search", "--db", "geo", "-G", query, "-m", str(limit), "-v", "3"]
        else:
            argv = [path, "search", "--db", db, "-q", query, "-m", str(limit), "-v", "3"]
        try:
            completed = run_capture(argv, timeout=90)
        except Exception as error:
            write_json(out_dir / f"pysradb_{db}_error.json", {"command": argv, "error": str(error)})
            continue
        (out_dir / f"pysradb_{db}.stdout.tsv").write_text(completed.stdout or "", encoding="utf-8")
        (out_dir / f"pysradb_{db}.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
        parsed = parse_tsv(completed.stdout or "")
        for row in parsed:
            row["external_source"] = f"pysradb_{db}"
            rows.append(row)
    write_tsv(out_dir / "pysradb_combined.tsv", rows)
    return rows


def run_rows_from_pysradb(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        for key, value in row.items():
            if not key.startswith("run_") or not key.endswith("_accession"):
                continue
            run = str(value or "").strip().upper()
            if not RUN_RE.fullmatch(run):
                continue
            study = str(row.get("study_accession") or "unknown").strip() or "unknown"
            study_alias = str(row.get("study_alias") or "").strip()
            study_external = str(row.get("study_external_id_1") or "").strip()
            geo_accession = next((value.upper() for value in (study_alias, study_external) if GEO_SERIES_RE.fullmatch(value.upper())), "")
            bioproject_accession = next(
                (value.upper() for value in (study_external, study_alias, str(row.get("study_attributes_1_value") or "")) if re.fullmatch(r"PRJ(?:NA|EB|DB)\d+", value.upper())),
                "",
            )
            sample_context = "; ".join(
                f"{row.get(f'sample_attributes_{index}_tag')}: {row.get(f'sample_attributes_{index}_value')}"
                for index in range(1, 9)
                if row.get(f"sample_attributes_{index}_tag") and row.get(f"sample_attributes_{index}_value")
            )
            result[run] = {
                "run_accession": run,
                "study_accession": study,
                "secondary_study_accession": study,
                "experiment_accession": row.get("experiment_accession", ""),
                "sample_accession": row.get("sample_accession", ""),
                "secondary_sample_accession": row.get("pool_external_id", ""),
                "scientific_name": row.get("sample_scientific_name") or row.get("pool_member_organism", ""),
                "library_strategy": row.get("experiment_library_strategy", ""),
                "library_source": row.get("experiment_library_source", ""),
                "library_selection": row.get("experiment_library_selection", ""),
                "library_layout": row.get("library_layout", ""),
                "instrument_platform": row.get("experiment_platform", ""),
                "instrument_model": row.get("experiment_instrument_model", ""),
                "secondary_project": bioproject_accession or study_external or study_alias,
                "geo_accession": geo_accession,
                "bioproject_accession": bioproject_accession,
                "experiment_title": row.get("experiment_title", ""),
                "sample_title": row.get("sample_title", ""),
                "study_title": row.get("study_study_title", ""),
                "study_abstract": row.get("study_study_abstract", ""),
                "sample_context": sample_context,
                "source_ref": "pysradb search",
                "source_sha256": "",
            }
    return list(result.values())


def flatten_ffq_files(value: Any) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("url") and (value.get("filename") or value.get("filetype")):
            files.append(value)
        for child in value.values():
            files.extend(flatten_ffq_files(child))
    elif isinstance(value, list):
        for child in value:
            files.extend(flatten_ffq_files(child))
    return files


def _bounded_env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    try:
        return max(minimum, min(int(os.environ.get(name, str(default)).strip()), maximum))
    except ValueError:
        return default


def ffq_resolve_runs(
    runs: list[dict[str, Any]],
    out_dir: Path,
    *,
    max_runs: int = 8,
    timeout_seconds: int = 30,
) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = tool_path("ffq")
    if not path or not runs:
        return []
    run_by_id = {str(row.get("run_accession", "")).upper(): row for row in runs if row.get("run_accession")}
    run_ids = list(run_by_id)[:max_runs]
    file_rows: list[dict[str, Any]] = []
    for run_id in run_ids:
        argv = [path, "--ftp", run_id]
        try:
            completed = run_capture(argv, timeout=timeout_seconds)
        except Exception as error:
            write_json(out_dir / f"ffq_{run_id}_error.json", {"command": argv, "error": str(error)})
            continue
        (out_dir / f"ffq_{run_id}.stdout.json").write_text(completed.stdout or "", encoding="utf-8")
        (out_dir / f"ffq_{run_id}.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
        if completed.returncode != 0 or not (completed.stdout or "").strip():
            continue
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            continue
        for index, file_obj in enumerate(flatten_ffq_files(payload), start=1):
            url = str(file_obj.get("url") or "")
            if not url:
                continue
            run = run_by_id[run_id]
            filetype = str(file_obj.get("filetype") or "").lower()
            filename = str(file_obj.get("filename") or Path(urllib.parse.urlsplit(url).path).name or run_id)
            role = "fastq" if "fastq" in filetype or filename.endswith((".fastq.gz", ".fq.gz")) else filetype or "remote_file"
            checksum = str(file_obj.get("md5") or "")
            file_rows.append(
                {
                    "file_id": f"{run_id}_{index}",
                    "source": "ffq_ftp",
                    "study_accession": run.get("study_accession", "unknown"),
                    "experiment_accession": run.get("experiment_accession", ""),
                    "run_accession": run_id,
                    "sample_accession": run.get("sample_accession", ""),
                    "uri": url,
                    "file_format": filetype or filename.rsplit(".", 1)[-1],
                    "file_role": role,
                    "size_bytes": int(file_obj.get("filesize") or 0),
                    "checksum_algorithm": "md5" if checksum else "",
                    "checksum": checksum,
                    "filename": filename,
                    "source_ref": "ffq --ftp",
                    "source_sha256": "",
                }
            )
    return file_rows


def omicsdi_search(query: str, out_dir: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    encoded = urllib.parse.urlencode({"query": query, "size": str(limit)})
    url = f"https://www.omicsdi.org/ws/dataset/search?{encoded}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception as error:
        write_json(out_dir / "omicsdi_error.json", {"url": url, "error": str(error)})
        return []
    (out_dir / "omicsdi_search.json").write_text(body, encoding="utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    rows: list[dict[str, Any]] = []
    candidates = payload.get("datasets") or payload.get("data") or payload.get("hits") or []
    if isinstance(candidates, dict):
        candidates = candidates.get("datasets") or candidates.get("hits") or []
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        text = json.dumps(item, ensure_ascii=False)
        accessions = sorted(set(match.upper() for match in ACCESSION_RE.findall(text)))
        rows.append(
            {
                "source": "omicsdi",
                "source_id": item.get("id") or item.get("accession") or item.get("database") or "",
                "title": item.get("title") or item.get("name") or "",
                "repository": item.get("database") or item.get("source") or "",
                "description": item.get("description") or item.get("omics_type") or "",
                "accessions": ";".join(accessions),
                "raw": item,
            }
        )
    write_jsonl(out_dir / "omicsdi_records.jsonl", rows)
    return rows


def geoparse_supplementary(accessions: list[str], out_dir: Path, *, max_accessions: int = 8) -> list[dict[str, Any]]:
    try:
        import GEOparse  # type: ignore
    except Exception as error:
        write_json(out_dir / "geoparse_error.json", {"error": str(error)})
        return []
    rows: list[dict[str, Any]] = []
    geo_dir = out_dir / "geoparse_cache"
    geo_dir.mkdir(parents=True, exist_ok=True)
    for accession in [item.upper() for item in accessions if GEO_SERIES_RE.fullmatch(item.upper())][:max_accessions]:
        try:
            gse = GEOparse.get_GEO(geo=accession, destdir=str(geo_dir), include_data=False, silent=True)
        except Exception as error:
            write_json(out_dir / f"geoparse_{accession}_error.json", {"accession": accession, "error": str(error)})
            continue
        supps = [
            {"url": str(url), "scope": "series", "sample_accession": ""}
            for url in gse.metadata.get("supplementary_file", [])
        ]
        for sample_accession, sample in gse.gsms.items():
            for url in sample.metadata.get("supplementary_file", []):
                supps.append({
                    "url": str(url),
                    "scope": "sample",
                    "sample_accession": str(sample_accession),
                })
        rows.append(
            {
                "source": "geoparse",
                "source_id": accession,
                "title": "; ".join(gse.metadata.get("title", [])),
                "sample_count": len(gse.gsms),
                "platform_count": len(gse.gpls),
                "supplementary_files": supps,
                "summary": "; ".join(gse.metadata.get("summary", []))[:2000],
            }
        )
    write_jsonl(out_dir / "geoparse_supplementary.jsonl", rows)
    return rows


def supplementary_file_rows(geo_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    preferred_terms = [
        "fragment", "fragments.tsv", "filtered_peak", "peak_bc", "matrix", "mtx",
        "peaks.bed", "counts", "filtered_feature_bc_matrix", "raw_feature_bc_matrix",
        "barcodes", "features.tsv", ".h5ad", ".h5mu", ".h5",
    ]
    for row in geo_rows:
        accession = str(row.get("source_id") or "")
        for index, value in enumerate(row.get("supplementary_files") or [], start=1):
            item = value if isinstance(value, dict) else {"url": value, "scope": "series", "sample_accession": ""}
            uri = str(item.get("url") or "")
            lowered = uri.lower()
            is_archive = lowered.endswith((".tar", ".tar.gz", ".tgz", ".zip"))
            if not any(term in lowered for term in preferred_terms) and not is_archive:
                continue
            filename = Path(urllib.parse.urlsplit(uri).path).name
            if "fragment" in lowered:
                role = "fragments"
            elif "peak" in lowered and lowered.endswith((".bed", ".bed.gz")):
                role = "peaks"
            elif "matrix" in lowered or "mtx" in lowered or "counts" in lowered or "filtered_peak" in lowered:
                role = "peak_matrix"
            elif is_archive:
                # GEO *_RAW.tar is a supplementary archive, not proof of FASTQ
                # and not yet proof of an analysis-ready matrix. Keep it visible
                # for evidence-backed manual inspection without overclaiming.
                role = "supplementary_archive"
            else:
                role = "processed_atac_file"
            result.append(
                {
                    "file_id": f"{accession}_supp_{index}",
                    "source": "geoparse_geo_supplementary",
                    "study_accession": accession,
                    "experiment_accession": "",
                    "run_accession": "",
                    "sample_accession": str(item.get("sample_accession") or ""),
                    "uri": uri.replace("ftp://ftp.ncbi.nlm.nih.gov", "https://ftp.ncbi.nlm.nih.gov"),
                    "file_format": filename.rsplit(".", 1)[-1] if "." in filename else "unknown",
                    "file_role": role,
                    "size_bytes": 0,
                    "checksum_algorithm": "",
                    "checksum": "",
                    "filename": filename,
                    "source_ref": f"GEOparse {item.get('scope') or 'series'} supplementary_file metadata",
                    "source_sha256": "",
                }
            )
    return result


def _remote_content_length(uri: str, *, timeout_seconds: int = 8) -> int:
    try:
        request = urllib.request.Request(uri, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return max(0, int(response.headers.get("Content-Length") or 0))
    except Exception:
        return 0


def probe_supplementary_sizes(rows: list[dict[str, Any]], *, max_files: int = 40) -> list[dict[str, Any]]:
    """Best-effort bounded HEAD probing; unknown size remains zero, never a mismatch."""
    targets = [row for row in rows if row.get("uri") and not int(row.get("size_bytes") or 0)][:max_files]
    if not targets:
        return rows
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
        futures = {executor.submit(_remote_content_length, str(row["uri"])): row for row in targets}
        for future in concurrent.futures.as_completed(futures):
            futures[future]["size_bytes"] = future.result()
    return rows


def accessions_from_sources(run_dir: Path, pysradb_rows: list[dict[str, Any]], omicsdi_rows: list[dict[str, Any]], *, max_accessions: int = 40) -> list[str]:
    values: list[str] = []
    for row in pysradb_rows:
        for key in [
            "study_alias", "study_external_id_1", "study_attributes_1_value",
            "study_accession", "experiment_accession", "sample_accession", "pool_external_id",
        ]:
            value = str(row.get(key) or "")
            values.extend(ACCESSION_RE.findall(value))
        values.extend(ACCESSION_RE.findall(str(row.get("experiment_title") or "")))
    for row in omicsdi_rows:
        accessions = row.get("accessions") or []
        values.extend(accessions if isinstance(accessions, list) else str(accessions).split(";"))
    for path in [run_dir / "unresolved_accessions.json", run_dir / "discovery_records.jsonl"]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        values.extend(ACCESSION_RE.findall(text))
    seen: list[str] = []
    for value in values:
        item = str(value).strip().upper()
        if item and item not in seen:
            seen.append(item)
        if len(seen) >= max_accessions:
            break
    return seen


def _deduplicate_rows(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    output: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(str(row.get(name) or "").strip().lower() for name in keys)
        if any(key):
            output.setdefault(key, row)
    return list(output.values())


def bounded_search_queries(primary: str, queries: list[str] | None = None) -> tuple[list[str], int]:
    """Keep synonym expansion useful without multiplying every network adapter."""
    all_queries = list(dict.fromkeys([primary, *(queries or [])]))
    configured = os.environ.get("CELLNOTE_EXTERNAL_QUERY_BUDGET", "8").strip()
    try:
        budget = max(1, min(int(configured), 12))
    except ValueError:
        budget = 8
    return all_queries[:budget], max(0, len(all_queries) - budget)


def run_external_discovery(
    query: str,
    run_dir: Path,
    *,
    limit: int = 50,
    enable_network: bool = True,
    queries: list[str] | None = None,
    progress: Any = None,
    enable_ffq: bool = True,
) -> dict[str, Any]:
    out_dir = run_dir / "external_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    health = check_tools()
    write_json(out_dir / "external_tool_status.json", health)
    if not enable_network:
        return {"enabled": False, "tool_status": health}

    search_queries, omitted_query_count = bounded_search_queries(query, queries)
    pys_rows: list[dict[str, Any]] = []
    for index, search_query in enumerate(search_queries, 1):
        if progress:
            progress("pysradb", "running", int((index - 1) / max(1, len(search_queries)) * 15))
        pys_rows.extend(pysradb_search(search_query, out_dir / f"query_{index:02d}", limit=min(limit, 200)))
    pys_rows = _deduplicate_rows(pys_rows, ["study_accession", "experiment_accession", "run_1_accession", "experiment_title"])
    run_rows = run_rows_from_pysradb(pys_rows)
    ffq_budget = min(_bounded_env_int("CELLNOTE_FFQ_RUN_BUDGET", 8, maximum=25), max(1, limit))
    ffq_timeout = _bounded_env_int("CELLNOTE_FFQ_TIMEOUT_SECONDS", 30, maximum=60)
    if progress:
        progress("ffq", "running" if enable_ffq else "skipped (raw files not requested)", 16)
    ffq_files = (
        ffq_resolve_runs(run_rows, out_dir, max_runs=ffq_budget, timeout_seconds=ffq_timeout)
        if enable_ffq
        else []
    )
    def official_progress(name: str, status: str, value: int) -> None:
        if progress:
            progress(name, status, 20 + int(value * 0.65))
    official_records, official_files, official_summary = run_official_sources(
        search_queries,
        limit_per_source=limit,
        progress=official_progress,
    )
    write_official_outputs(run_dir, official_records, official_files, official_summary)
    omics_rows = [row for row in official_records if str(row.get("source") or "").startswith("OmicsDI/")]
    accessions = accessions_from_sources(run_dir, pys_rows, official_records)
    if progress:
        progress("geoparse", "running", 88)
    geo_rows = geoparse_supplementary(
        [item for item in accessions if item.startswith("GSE")],
        out_dir,
        max_accessions=min(50, max(8, limit)),
    )
    geo_files = probe_supplementary_sizes(
        supplementary_file_rows(geo_rows),
        max_files=_bounded_env_int("CELLNOTE_GEO_SIZE_PROBE_BUDGET", 40, maximum=200),
    )
    if progress:
        progress("external_discovery", "completed", 100)

    external_runs = run_rows
    external_files = _deduplicate_rows(ffq_files + geo_files + official_files, ["uri", "study_accession", "filename"])
    write_jsonl(run_dir / "external_run_manifest.jsonl", external_runs)
    write_jsonl(run_dir / "external_remote_file_candidates.jsonl", external_files)
    summary = {
        "enabled": True,
        "query": query,
        "queries": search_queries,
        "query_budget": len(search_queries),
        "omitted_query_count": omitted_query_count,
        "tool_status": health,
        "pysradb_rows": len(pys_rows),
        "pysradb_runs": len(run_rows),
        "ffq_files": len(ffq_files),
        "ffq_enabled": enable_ffq,
        "ffq_run_budget": ffq_budget if enable_ffq else 0,
        "omicsdi_records": len(omics_rows),
        "geoparse_records": len(geo_rows),
        "geo_supplementary_files": len(geo_files),
        "official_sources": official_summary,
        "official_dataset_records": len(official_records),
        "official_downloadable_files": len(official_files),
        "outputs": {
            "external_dir": str(out_dir),
            "external_run_manifest": str(run_dir / "external_run_manifest.jsonl"),
            "external_remote_files": str(run_dir / "external_remote_file_candidates.jsonl"),
        },
    }
    write_json(out_dir / "external_discovery_summary.json", summary)
    return summary


def print_health(status: dict[str, dict[str, Any]]) -> None:
    print("外部 crawler 工具状态：")
    for name, item in status.items():
        state = "OK" if item.get("available") else "MISSING"
        detail = item.get("version") or item.get("error") or ""
        print(f"- {name}: {state} ({item.get('command', '')}) {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CellNote external crawler adapters")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check optional external crawler tools")
    check.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="run optional external discovery adapters")
    run.add_argument("--query", required=True)
    run.add_argument("--run-dir", required=True)
    run.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    if args.command == "check":
        status = check_tools()
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_health(status)
        return 0 if all(item.get("available") for item in status.values()) else 2
    if args.command == "run":
        summary = run_external_discovery(args.query, Path(args.run_dir), limit=args.limit)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
