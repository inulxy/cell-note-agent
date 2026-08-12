"""Deterministic pre-download audit for remote run-level file candidates."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from .crawl_models import (
    EnaRunRecord,
    RemoteFileCandidate,
    RemoteFileIssue,
    stable_id,
)


MD5_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


def audit_remote_files(
    runs: list[EnaRunRecord],
    files: list[RemoteFileCandidate],
) -> list[RemoteFileIssue]:
    issues: dict[str, RemoteFileIssue] = {}
    files_by_run: dict[str, list[RemoteFileCandidate]] = defaultdict(list)
    uri_counts = Counter(item.uri for item in files)

    def add(
        *,
        code: str,
        run_accession: str,
        file_id: str,
        message: str,
        severity: str = "REVIEW",
    ) -> None:
        material = {
            "code": code,
            "run_accession": run_accession,
            "file_id": file_id,
            "message": message,
        }
        issue = RemoteFileIssue(
            issue_id=stable_id("file-issue", material),
            issue_code=code,
            severity=severity,
            run_accession=run_accession,
            file_id=file_id,
            message=message,
        )
        issues.setdefault(issue.issue_id, issue)

    for item in files:
        files_by_run[item.run_accession].append(item)
        parsed = urlsplit(item.uri)
        if parsed.scheme != "https" or not parsed.hostname:
            add(
                code="UNSAFE_REMOTE_URI",
                run_accession=item.run_accession,
                file_id=item.file_id,
                message="remote file URI must be an absolute HTTPS URL",
                severity="BLOCK",
            )
        if item.size_bytes is None or item.size_bytes <= 0:
            add(
                code="MISSING_FILE_SIZE",
                run_accession=item.run_accession,
                file_id=item.file_id,
                message="remote file size is missing or non-positive",
            )
        if not item.checksum:
            add(
                code="MISSING_CHECKSUM",
                run_accession=item.run_accession,
                file_id=item.file_id,
                message="remote file checksum is missing",
            )
        elif (
            item.checksum_algorithm.lower() == "md5"
            and not MD5_PATTERN.fullmatch(item.checksum)
        ):
            add(
                code="INVALID_MD5",
                run_accession=item.run_accession,
                file_id=item.file_id,
                message="remote file MD5 is not a 32-character hexadecimal digest",
                severity="BLOCK",
            )
        if uri_counts[item.uri] > 1:
            add(
                code="DUPLICATE_REMOTE_URI",
                run_accession=item.run_accession,
                file_id=item.file_id,
                message="the same remote URI is associated with multiple file records",
            )

    for run in runs:
        run_files = files_by_run.get(run.run_accession, [])
        if not run_files:
            add(
                code="RUN_WITHOUT_FASTQ",
                run_accession=run.run_accession,
                file_id="",
                message="ENA run has no FASTQ file candidates",
            )
            continue
        if run.library_layout.upper() == "PAIRED":
            roles = {item.file_role for item in run_files}
            if not {"read1", "read2"}.issubset(roles):
                add(
                    code="INCOMPLETE_PAIRED_FASTQ",
                    run_accession=run.run_accession,
                    file_id="",
                    message="paired run does not expose both read1 and read2 files",
                    severity="BLOCK",
                )
    return [issues[key] for key in sorted(issues)]
