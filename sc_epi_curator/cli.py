"""Command-line bridge used directly and by the Pi CellNote extension."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .adapters import (
    AccessionSeedAdapter,
    CrawlRunAdapter,
    EuropePmcAdapter,
    FixtureAdapter,
    NcbiEntrezAdapter,
    WebPageAdapter,
)
from .crawl_models import NetworkBudget, NetworkUsage
from .crawler import DiscoveryCrawler
from .events import EventStore
from .harness import CellNoteDomainHarness
from .http import FetchPolicy, HttpClient, RawResponseCache
from .models import ActionBudget, to_primitive
from .resolvers import EnaRunResolver
from .remote_probe import RemoteFileProber


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURE = PACKAGE_ROOT / "fixtures" / "pbmc_multiome_rescue.json"


def _load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _budget_from_config(config: dict[str, Any]) -> ActionBudget:
    value = config.get("acquisition", {}).get("budget", {})
    return ActionBudget(
        max_requests=int(value.get("max_requests", 100)),
        max_bytes=int(value.get("max_bytes", 20_000_000_000)),
        max_recovery_rounds=int(value.get("max_recovery_rounds", 2)),
        approval_download_bytes=int(
            value.get("approval_download_bytes", 2_000_000_000)
        ),
    )


def _run_fixture(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    adapter = FixtureAdapter(args.fixture)
    harness = CellNoteDomainHarness(
        adapter=adapter,
        run_dir=args.out,
        run_id=args.run_id,
        budget=_budget_from_config(config),
    )
    result = harness.run()
    print(json.dumps(to_primitive(result), indent=2, ensure_ascii=False, default=str))
    return 0 if result.event_chain_valid else 2


def _crawl_settings(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("crawler", {}))


def _client(
    *,
    cache: RawResponseCache,
    usage: NetworkUsage,
    budget: NetworkBudget,
    allowed_hosts: tuple[str, ...],
    settings: dict[str, Any],
    requests_per_second: float,
    cache_only: bool,
) -> HttpClient:
    return HttpClient(
        cache=cache,
        usage=usage,
        budget=budget,
        policy=FetchPolicy(
            allowed_hosts=allowed_hosts,
            user_agent=str(settings.get("user_agent", "CellNoteAgent/0.2")),
            requests_per_second=requests_per_second,
            timeout_seconds=float(settings.get("timeout_seconds", 30)),
            max_response_bytes=int(
                settings.get("max_response_bytes", 25_000_000)
            ),
            retries=int(settings.get("retries", 3)),
            backoff_seconds=float(settings.get("backoff_seconds", 0.5)),
            cache_only=cache_only,
        ),
    )


def _run_crawl(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    settings = _crawl_settings(config)
    sources = args.source or list(settings.get("sources", ["geo", "literature"]))
    run_dir = Path(args.out)
    cache = RawResponseCache(run_dir / "raw")
    usage = NetworkUsage()
    budget = NetworkBudget(
        max_requests=int(settings.get("max_requests", 100)),
        max_bytes=int(settings.get("max_bytes", 100_000_000)),
    )
    email = (
        args.email
        or str(settings.get("contact_email", ""))
        or os.getenv("CELLNOTE_CONTACT_EMAIL", "")
    )
    adapters = []
    resolvers = []
    remote_file_prober: RemoteFileProber | None = None
    ncbi_client: HttpClient | None = None
    if "accession" in sources:
        if not args.accession:
            print("[error] accession source requires --accession", file=sys.stderr)
            return 2
        adapters.append(AccessionSeedAdapter(list(args.accession)))
    if any(source in {"geo", "sra"} for source in sources):
        if not email:
            print(
                "[error] NCBI sources require --email, crawler.contact_email, "
                "or CELLNOTE_CONTACT_EMAIL",
                file=sys.stderr,
            )
            return 2
        ncbi_client = _client(
            cache=cache,
            usage=usage,
            budget=budget,
            allowed_hosts=(
                "eutils.ncbi.nlm.nih.gov",
                "www.ncbi.nlm.nih.gov",
            ),
            settings=settings,
            requests_per_second=float(
                settings.get(
                    "ncbi_requests_per_second",
                    8.0 if os.getenv("NCBI_API_KEY") else 2.5,
                )
            ),
            cache_only=args.cache_only,
        )
    if "geo" in sources and ncbi_client is not None:
        adapters.append(
            NcbiEntrezAdapter(
                ncbi_client,
                database="gds",
                email=email,
                enrich_geo_soft=bool(
                    settings.get("enrich_geo_soft", True)
                ),
                max_geo_soft_records=int(
                    settings.get("max_geo_soft_records", 10)
                ),
            )
        )
    if "sra" in sources and ncbi_client is not None:
        adapters.append(
            NcbiEntrezAdapter(
                ncbi_client, database="sra", email=email
            )
        )
    if "literature" in sources:
        literature_client = _client(
            cache=cache,
            usage=usage,
            budget=budget,
            allowed_hosts=("www.ebi.ac.uk",),
            settings=settings,
            requests_per_second=float(
                settings.get("europe_pmc_requests_per_second", 2.0)
            ),
            cache_only=args.cache_only,
        )
        adapters.append(
            EuropePmcAdapter(
                literature_client,
                email=email,
                include_open_access_full_text=args.include_open_access_full_text,
                max_full_text_articles=int(
                    settings.get("max_full_text_articles", 3)
                ),
            )
        )
    if "web" in sources:
        seed_urls = list(args.seed_url or settings.get("seed_urls", []))
        if not seed_urls:
            print("[error] web source requires --seed-url", file=sys.stderr)
            return 2
        from urllib.parse import urlsplit

        seed_hosts = [
            urlsplit(url).hostname
            for url in seed_urls
            if urlsplit(url).hostname
        ]
        allowed_hosts = tuple(
            sorted(
                set(seed_hosts)
                | set(args.allow_host or settings.get("web_allowed_hosts", []))
            )
        )
        web_client = _client(
            cache=cache,
            usage=usage,
            budget=budget,
            allowed_hosts=allowed_hosts,
            settings=settings,
            requests_per_second=float(
                settings.get("web_requests_per_second", 1.0)
            ),
            cache_only=args.cache_only,
        )
        adapters.append(
            WebPageAdapter(
                web_client,
                seed_urls=seed_urls,
                max_depth=args.web_depth,
                robots_fail_open=args.robots_fail_open,
            )
        )
    resolve_ena_runs = (
        bool(settings.get("resolve_ena_runs", True))
        if args.resolve_ena_runs is None
        else args.resolve_ena_runs
    )
    if resolve_ena_runs:
        ena_client = _client(
            cache=cache,
            usage=usage,
            budget=budget,
            allowed_hosts=("www.ebi.ac.uk",),
            settings=settings,
            requests_per_second=float(
                settings.get("ena_requests_per_second", 2.0)
            ),
            cache_only=args.cache_only,
        )
        resolvers.append(
            EnaRunResolver(
                ena_client,
                max_accessions=int(
                    settings.get("max_ena_accessions", 25)
                ),
                max_runs=int(settings.get("max_ena_runs", 200)),
                accessions_per_request=int(
                    settings.get("ena_accessions_per_request", 10)
                ),
            )
        )
    probe_remote_files = (
        bool(settings.get("probe_remote_files", False))
        if args.probe_remote_files is None
        else args.probe_remote_files
    )
    if probe_remote_files:
        probe_client = _client(
            cache=cache,
            usage=usage,
            budget=budget,
            allowed_hosts=("ftp.sra.ebi.ac.uk",),
            settings=settings,
            requests_per_second=float(
                settings.get("remote_probe_requests_per_second", 1.0)
            ),
            cache_only=args.cache_only,
        )
        remote_file_prober = RemoteFileProber(
            probe_client,
            max_files=int(settings.get("max_remote_file_probes", 20)),
        )
    if not adapters:
        print("[error] no supported crawl source selected", file=sys.stderr)
        return 2

    crawler = DiscoveryCrawler(
        adapters=adapters,
        run_dir=run_dir,
        run_id=args.run_id,
        usage=usage,
        resolvers=resolvers,
        remote_file_prober=remote_file_prober,
    )
    result = crawler.run(query=args.query, limit_per_source=args.limit)
    print(json.dumps(to_primitive(result), indent=2, ensure_ascii=False, default=str))
    return 0 if result.event_chain_valid and result.error_count == 0 else 2


def _run_promote(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    try:
        adapter = CrawlRunAdapter(args.crawl_run)
        harness = CellNoteDomainHarness(
            adapter=adapter,
            run_dir=args.out,
            run_id=args.run_id,
            budget=_budget_from_config(config),
        )
        result = harness.run()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"[error] crawl promotion failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(to_primitive(result), indent=2, ensure_ascii=False, default=str))
    return 0 if result.event_chain_valid else 2


def _run_download(args: argparse.Namespace) -> int:
    script = PACKAGE_ROOT.parent / "scripts" / "download_validate.py"
    if not script.exists():
        print(f"[error] download script not found: {script}", file=sys.stderr)
        return 2
    command = [
        sys.executable,
        str(script),
        "--stage",
        args.stage,
        "--manifest",
        args.manifest,
        "--store",
        args.store,
        "--max_retries",
        str(args.max_retries),
        "--user_agent",
        args.user_agent,
        "--downloader",
        args.downloader,
    ]
    if args.enable_fetch:
        command.append("--enable_fetch")
    return subprocess.call(command)


def _run_agent(args: argparse.Namespace) -> int:
    from cell_note_agent.agent_cli import run_agent

    return run_agent(args)


def _run_external_tools(args: argparse.Namespace) -> int:
    from cell_note_agent.external_crawlers import main as external_main

    if args.external_command == "run":
        forwarded = ["run", "--query", args.query, "--run-dir", args.run_dir, "--limit", str(args.limit)]
    else:
        forwarded = ["check"]
        if args.json:
            forwarded.append("--json")
    return external_main(forwarded)


def _crawl_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    manifest_path = run_dir / "crawl_manifest.json"
    db_path = run_dir / "crawl_state.sqlite"
    if not manifest_path.exists() or not db_path.exists():
        print(f"[error] crawl run not found: {run_dir}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    store = EventStore(db_path)
    try:
        run_ids = _run_ids(db_path)
        chains = {
            run_id: store.verify_chain(run_id)
            for run_id in run_ids
        }
    finally:
        store.close()
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "manifest": manifest,
                "event_chains": chains,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(chains.values()) else 2


def _run_ids(db_path: Path) -> list[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT DISTINCT run_id FROM events ORDER BY run_id"
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def _status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    db_path = run_dir / "state.sqlite"
    if not db_path.exists():
        print(f"[error] state database not found: {db_path}", file=sys.stderr)
        return 2
    run_ids = _run_ids(db_path)
    result: dict[str, Any] = {"run_dir": str(run_dir), "runs": []}
    store = EventStore(db_path)
    try:
        for run_id in run_ids:
            events = store.list_events(run_id)
            result["runs"].append(
                {
                    "run_id": run_id,
                    "event_count": len(events),
                    "last_event": events[-1].event_type if events else None,
                    "event_chain_valid": store.verify_chain(run_id),
                }
            )
    finally:
        store.close()
    summary_path = run_dir / "run_summary.json"
    if summary_path.exists():
        result["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _export(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    db_path = run_dir / "state.sqlite"
    if not db_path.exists():
        print(f"[error] state database not found: {db_path}", file=sys.stderr)
        return 2
    run_ids = _run_ids(db_path)
    if not run_ids:
        print("[error] no runs found", file=sys.stderr)
        return 2
    run_id = args.run_id or run_ids[-1]
    destination = Path(args.out) if args.out else run_dir / "events.jsonl"
    store = EventStore(db_path)
    try:
        store.export_jsonl(run_id, destination)
        valid = store.verify_chain(run_id)
    finally:
        store.close()
    print(
        json.dumps(
            {
                "run_id": run_id,
                "events": str(destination),
                "event_chain_valid": valid,
            },
            indent=2,
        )
    )
    return 0 if valid else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cell-note",
        description="CellNote evidence-grounded acquisition domain core",
    )
    parser.add_argument("--config", help="versioned CellNote JSON config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("discover", "demo"):
        command = subparsers.add_parser(
            name,
            help="run the deterministic offline discovery/recovery vertical slice",
        )
        command.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
        command.add_argument("--out", default="runs/demo-pbmc-multiome")
        command.add_argument("--run-id", default="demo-pbmc-multiome-v1")
        command.set_defaults(handler=_run_fixture)

    status = subparsers.add_parser("status", help="inspect a run directory")
    status.add_argument("--run", default="runs/demo-pbmc-multiome")
    status.set_defaults(handler=_status)

    export = subparsers.add_parser("export", help="export and verify the event chain")
    export.add_argument("--run", default="runs/demo-pbmc-multiome")
    export.add_argument("--run-id")
    export.add_argument("--out")
    export.set_defaults(handler=_export)

    crawl = subparsers.add_parser(
        "crawl",
        help="crawl public metadata, literature, and allowlisted web pages",
    )
    crawl.add_argument("--query", required=True)
    crawl.add_argument(
        "--source",
        action="append",
        choices=("geo", "sra", "literature", "web", "accession"),
        help="repeat to combine sources",
    )
    crawl.add_argument("--out", default="runs/crawl")
    crawl.add_argument("--run-id", default="crawl-v1")
    crawl.add_argument("--limit", type=int, default=20)
    crawl.add_argument("--email", help="contact email for public APIs")
    crawl.add_argument("--seed-url", action="append")
    crawl.add_argument(
        "--accession",
        action="append",
        help="explicit GEO/SRA/ENA/BioProject accession seed",
    )
    crawl.add_argument("--allow-host", action="append")
    crawl.add_argument("--web-depth", type=int, default=0)
    crawl.add_argument("--include-open-access-full-text", action="store_true")
    crawl.add_argument("--robots-fail-open", action="store_true")
    crawl.add_argument("--cache-only", action="store_true")
    crawl.add_argument(
        "--resolve-ena-runs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="resolve discovered SRA/BioProject accessions to ENA FASTQ manifests",
    )
    crawl.add_argument(
        "--probe-remote-files",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="HEAD-probe remote FASTQ files with a one-byte Range fallback",
    )
    crawl.set_defaults(handler=_run_crawl)

    promote = subparsers.add_parser(
        "promote",
        help="integrity-check a crawl run and promote it into curation review",
    )
    promote.add_argument("--crawl-run", required=True)
    promote.add_argument("--out", default="runs/promoted")
    promote.add_argument("--run-id", default="promoted-v1")
    promote.set_defaults(handler=_run_promote)

    download = subparsers.add_parser(
        "download",
        help="plan, fetch, or verify local acquisition from a file manifest",
    )
    download.add_argument("--stage", required=True, choices=("plan", "fetch", "verify"))
    download.add_argument("--manifest", required=True)
    download.add_argument("--store", default="data/raw")
    download.add_argument("--enable_fetch", action="store_true")
    download.add_argument("--max_retries", type=int, default=3)
    download.add_argument("--user_agent", default="cellnote-agent/0.1")
    download.add_argument("--downloader", choices=("auto", "curl", "wget", "urllib"), default="auto")
    download.set_defaults(handler=_run_download)

    agent = subparsers.add_parser(
        "agent",
        help="start an interactive natural-language CellNoteAgent shell",
    )
    agent.add_argument("--once", help="run one natural-language instruction and exit")
    agent.add_argument("--yes", action="store_true", help="assume yes for execution confirmations")
    agent.add_argument("--repo_root", help="repository root; defaults to current directory")
    agent.add_argument("--run_root", default="runs/agent-demo", help="run/output directory")
    agent.add_argument("--processing_python", help="Python executable with scanpy/anndata installed")
    agent.set_defaults(handler=_run_agent)

    external_tools = subparsers.add_parser("external-tools", help="check or run optional external crawler adapters")
    external_sub = external_tools.add_subparsers(dest="external_command", required=True)
    external_check = external_sub.add_parser("check", help="check pysradb / ffq / GEOparse / OmicsDI support")
    external_check.add_argument("--json", action="store_true")
    external_check.set_defaults(handler=_run_external_tools)
    external_run = external_sub.add_parser("run", help="run external crawler adapters into a crawl side-car directory")
    external_run.add_argument("--query", required=True)
    external_run.add_argument("--run-dir", required=True)
    external_run.add_argument("--limit", type=int, default=50)
    external_run.add_argument("--json", action="store_true")
    external_run.set_defaults(handler=_run_external_tools)

    crawl_status = subparsers.add_parser(
        "crawl-status", help="inspect a crawl manifest and event chain"
    )
    crawl_status.add_argument("--run", default="runs/crawl")
    crawl_status.set_defaults(handler=_crawl_status)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
