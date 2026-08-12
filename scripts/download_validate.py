#!/usr/bin/env python
"""download-validate: auditable download planning and guarded validation.

The crawler can discover candidate files today. Real fetching is intentionally gated behind
``--enable_fetch`` so accidental large downloads do not happen while the download policy is
still being finalized.
"""
from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from _common import require_files, run_stages


SUPPORTED_CHECKSUMS = {"md5", "sha256"}


def _read_manifest(manifest_path: str) -> list[dict]:
    rows: list[dict] = []
    with open(manifest_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows.extend(reader)
    return rows


def _row_url(row: dict) -> str:
    return row.get("source_uri") or row.get("url") or row.get("download_url") or row.get("remote_url") or ""


def _row_artifact_id(row: dict, index: int) -> str:
    return row.get("artifact_id") or row.get("file_id") or row.get("accession") or f"artifact_{index}"


def _row_dataset_id(row: dict, index: int) -> str:
    return row.get("dataset_id") or row.get("accession") or f"dataset_{index}"


def _row_dest(args, row: dict, index: int) -> str:
    explicit = row.get("local_path") or row.get("path")
    if explicit:
        return explicit if os.path.isabs(explicit) else os.path.join(args.store, explicit)
    url = _row_url(row)
    filename = os.path.basename(urlsplit(url).path) if url else ""
    if not filename:
        filename = _row_artifact_id(row, index)
    return os.path.join(args.store, _row_dataset_id(row, index), filename or "unknown")


def _expected_size(row: dict) -> int | None:
    size = row.get("size_bytes") or row.get("file_size") or row.get("bytes") or ""
    return int(size) if str(size).isdigit() else None


def _checksum_spec(row: dict) -> tuple[str, str] | None:
    checksum = (row.get("checksum") or row.get("md5") or row.get("sha256") or "").strip()
    algorithm = (row.get("checksum_algorithm") or row.get("hash_algorithm") or "").strip().lower()
    if ":" in checksum:
        prefix, value = checksum.split(":", 1)
        algorithm = algorithm or prefix.strip().lower()
        checksum = value.strip()
    if not algorithm:
        if row.get("md5") or len(checksum) == 32:
            algorithm = "md5"
        elif row.get("sha256") or len(checksum) == 64:
            algorithm = "sha256"
    if algorithm not in SUPPORTED_CHECKSUMS or not checksum:
        return None
    return algorithm, checksum.lower()


def _checksum_file(path: str, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provenance_path(args) -> str:
    return os.path.join(args.store, "provenance.jsonl")


def _downloaded_manifest_path(args) -> str:
    return os.path.join(args.store, "downloaded_file_manifest.csv")


def _append_provenance(args, row: dict, index: int, status: str, dest: str, reason: str = "") -> None:
    import json

    os.makedirs(args.store, exist_ok=True)
    checksum = _checksum_spec(row)
    actual_size = os.path.getsize(dest) if os.path.exists(dest) else 0
    actual_checksum = ""
    if os.path.exists(dest):
        actual_checksum = _checksum_file(dest, checksum[0] if checksum else "sha256")
    record = {
        "timestamp": _now(),
        "status": status,
        "reason": reason,
        "artifact_id": _row_artifact_id(row, index),
        "dataset_id": _row_dataset_id(row, index),
        "role": row.get("role", ""),
        "source_uri": _row_url(row),
        "local_path": dest,
        "expected_size_bytes": _expected_size(row),
        "actual_size_bytes": actual_size,
        "expected_checksum_algorithm": checksum[0] if checksum else "",
        "expected_checksum": checksum[1] if checksum else "",
        "actual_checksum": actual_checksum,
    }
    with open(_provenance_path(args), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def _file_matches_expected(path: str, row: dict) -> tuple[bool, str]:
    if not os.path.exists(path):
        return False, "file not found"
    expected_size = _expected_size(row)
    actual_size = os.path.getsize(path)
    if expected_size is not None and actual_size != expected_size:
        return False, f"size mismatch: {actual_size} != {expected_size}"
    checksum = _checksum_spec(row)
    if checksum:
        algorithm, expected = checksum
        actual = _checksum_file(path, algorithm)
        if actual != expected:
            return False, f"{algorithm.upper()} mismatch: {actual} != {expected}"
    return True, "ok"


def _run_external_downloader(command: list[str]) -> bool:
    completed = subprocess.run(command, check=False)
    return completed.returncode == 0


def _download_with_curl(url: str, dest: str, user_agent: str, max_retries: int) -> bool:
    curl = shutil.which("curl")
    if not curl:
        return False
    command = [
        curl,
        "--location",
        "--fail",
        "--continue-at",
        "-",
        "--retry",
        str(max_retries),
        "--retry-delay",
        "5",
        "--retry-all-errors",
        "--connect-timeout",
        "60",
        "--speed-time",
        "300",
        "--speed-limit",
        "1024",
        "--user-agent",
        user_agent,
        "--output",
        dest,
        url,
    ]
    return _run_external_downloader(command)


def _download_with_wget(url: str, dest: str, user_agent: str, max_retries: int) -> bool:
    wget = shutil.which("wget")
    if not wget:
        return False
    command = [
        wget,
        "--continue",
        "--tries",
        str(max_retries),
        "--timeout",
        "300",
        "--waitretry",
        "5",
        "--user-agent",
        user_agent,
        "--output-document",
        dest,
        url,
    ]
    return _run_external_downloader(command)


def _download_with_urllib(url: str, dest: str, user_agent: str) -> None:
    existing_size = os.path.getsize(dest) if os.path.exists(dest) else 0
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    if existing_size > 0:
        request.add_header("Range", f"bytes={existing_size}-")
    with urllib.request.urlopen(request, timeout=300) as response:
        status = getattr(response, "status", response.getcode())
        mode = "ab" if existing_size > 0 and status == 206 else "wb"
        with open(dest, mode) as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)


def _download_file(url: str, dest: str, row: dict, user_agent: str, max_retries: int = 3, downloader: str = "auto") -> bool:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    expected = _expected_size(row)
    if expected is not None and os.path.exists(dest) and os.path.getsize(dest) > expected:
        print("  existing file is larger than expected; removing before retry")
        os.remove(dest)

    choices = [downloader] if downloader != "auto" else ["curl", "wget", "urllib"]
    for attempt in range(max_retries):
        for choice in choices:
            try:
                before = os.path.getsize(dest) if os.path.exists(dest) else 0
                print(f"  attempt {attempt + 1}/{max_retries} using {choice}; existing={before} bytes")
                if choice == "curl":
                    ok = _download_with_curl(url, dest, user_agent, max_retries)
                elif choice == "wget":
                    ok = _download_with_wget(url, dest, user_agent, max_retries)
                elif choice == "urllib":
                    _download_with_urllib(url, dest, user_agent)
                    ok = True
                else:
                    raise ValueError(f"unsupported downloader: {choice}")
                if not ok:
                    print(f"  {choice} returned non-zero status")
                matches, reason = _file_matches_expected(dest, row)
                if matches:
                    return True
                after = os.path.getsize(dest) if os.path.exists(dest) else 0
                print(f"  validation pending: {reason}; current={after} bytes")
            except Exception as error:  # network-facing diagnostic
                print(f"  attempt {attempt + 1}/{max_retries} with {choice} failed: {error}")
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    return False


def _downloaded_manifest_row(args, row: dict, index: int, dest: str) -> dict:
    checksum = _checksum_spec(row)
    if checksum:
        algorithm, value = checksum
    else:
        algorithm, value = "sha256", _checksum_file(dest, "sha256")
    return {
        "artifact_id": _row_artifact_id(row, index),
        "dataset_id": _row_dataset_id(row, index),
        "role": row.get("role", ""),
        "file_format": row.get("file_format", ""),
        "source_uri": _row_url(row),
        "local_path": dest,
        "size_bytes": os.path.getsize(dest),
        "checksum_algorithm": algorithm,
        "checksum": value,
        "verified_at": _now(),
    }


def _write_downloaded_manifest(args, rows: list[dict]) -> None:
    os.makedirs(args.store, exist_ok=True)
    path = _downloaded_manifest_path(args)
    fieldnames = [
        "artifact_id",
        "dataset_id",
        "role",
        "file_format",
        "source_uri",
        "local_path",
        "size_bytes",
        "checksum_algorithm",
        "checksum",
        "verified_at",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[verify] downloaded manifest: {path}")


def plan(args):
    require_files(args.manifest)
    rows = _read_manifest(args.manifest)
    print(f"[plan] {len(rows)} entries in manifest")
    total_bytes = 0
    missing_url = 0
    for index, row in enumerate(rows):
        url = _row_url(row)
        dest = _row_dest(args, row, index)
        size = _expected_size(row)
        status = "exists" if os.path.exists(dest) else "new"
        if size is not None:
            total_bytes += size
        if not url:
            missing_url += 1
        print(f"  [{status}] {_row_dataset_id(row, index)}: {url or '<no url>'} -> {dest}")
    print(f"\n[plan] total estimated size: {total_bytes / 1e9:.2f} GB")
    print(f"[plan] rows without URL: {missing_url}")
    print(f"[plan] store directory: {args.store}")
    print(f"[plan] provenance log: {_provenance_path(args)}")
    print(f"[plan] verified local manifest: {_downloaded_manifest_path(args)}")
    print("[plan] fetch is guarded; run --stage=fetch --enable_fetch only after review.")


def fetch(args):
    if not args.enable_fetch:
        raise SystemExit(
            "[fetch] disabled by default. Re-run with --enable_fetch after eligibility, "
            "storage, and download policy are approved."
        )
    require_files(args.manifest)
    rows = _read_manifest(args.manifest)
    success = failed = skipped = 0
    for index, row in enumerate(rows):
        url = _row_url(row)
        if not url:
            print(f"  [skip] {_row_dataset_id(row, index)}: no URL")
            skipped += 1
            continue
        dest = _row_dest(args, row, index)
        complete, reason = _file_matches_expected(dest, row)
        if complete:
            print(f"  [skip] {_row_dataset_id(row, index)}: already complete")
            _append_provenance(args, row, index, "skipped_existing", dest)
            skipped += 1
            continue
        print(f"  [{index + 1}/{len(rows)}] downloading: {url}")
        if _download_file(url, dest, row, args.user_agent, args.max_retries, args.downloader):
            print(f"    done: {dest} ({os.path.getsize(dest)} bytes)")
            _append_provenance(args, row, index, "downloaded", dest)
            success += 1
        else:
            print(f"    FAILED: {url}")
            _append_provenance(args, row, index, "failed", dest, reason)
            failed += 1
    print(f"\n[fetch] {success} succeeded, {failed} failed, {skipped} skipped")


def verify(args):
    require_files(args.manifest)
    rows = _read_manifest(args.manifest)
    verified = missing = corrupt = 0
    report_rows: list[dict] = []
    manifest_rows: list[dict] = []
    for index, row in enumerate(rows):
        url = _row_url(row)
        if not url and not (row.get("local_path") or row.get("path")):
            continue
        dest = _row_dest(args, row, index)
        dataset_id = _row_dataset_id(row, index)
        if not os.path.exists(dest):
            missing += 1
            report_rows.append({"dataset_id": dataset_id, "file": dest, "status": "missing", "reason": "file not found"})
            _append_provenance(args, row, index, "missing", dest, "file not found")
            print(f"  [missing] {dest}")
            continue
        matches, reason = _file_matches_expected(dest, row)
        if not matches:
            corrupt += 1
            report_rows.append({"dataset_id": dataset_id, "file": dest, "status": "corrupt", "reason": reason})
            _append_provenance(args, row, index, "corrupt", dest, reason)
            print(f"  [corrupt] {dest}: {reason}")
            continue
        verified += 1
        manifest_rows.append(_downloaded_manifest_row(args, row, index, dest))
        _append_provenance(args, row, index, "verified", dest)
        actual_size = os.path.getsize(dest)
        print(f"  [verified] {dest} ({actual_size} bytes)")
    print(f"\n[verify] {verified} verified, {missing} missing, {corrupt} corrupt")
    _write_downloaded_manifest(args, manifest_rows)
    report_path = os.path.join(args.store, "missing_files_report.csv")
    if report_rows:
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        with open(report_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["dataset_id", "file", "status", "reason"])
            writer.writeheader()
            writer.writerows(report_rows)
        print(f"[verify] report: {report_path}")


def _parser(parser):
    parser.add_argument("--manifest", required=True, help="file_manifest.csv with URL/path rows")
    parser.add_argument("--store", default="data/raw", help="local download directory")
    parser.add_argument("--enable_fetch", action="store_true", help="explicitly allow network downloads")
    parser.add_argument("--max_retries", type=int, default=3)
    parser.add_argument("--user_agent", default="cellnote-agent/0.1", help="HTTP User-Agent for remote file downloads")
    parser.add_argument("--downloader", choices=("auto", "curl", "wget", "urllib"), default="auto", help="download backend; auto prefers curl/wget resumable downloads")
    return parser


if __name__ == "__main__":
    run_stages("download_validate", {"plan": plan, "fetch": fetch, "verify": verify}, _parser)
