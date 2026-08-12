"""Bounded reachability and size probes for remote biological file candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .crawl_models import (
    FetchReceipt,
    RemoteFileCandidate,
    RemoteFileIssue,
    RemoteFileProbe,
    stable_id,
)
from .http import FetchError, HttpClient


_CONTENT_RANGE_TOTAL = re.compile(r"^bytes\s+\d+-\d+/(\d+|\*)$", re.IGNORECASE)


@dataclass
class RemoteProbeBatch:
    probes: list[RemoteFileProbe] = field(default_factory=list)
    issues: list[RemoteFileIssue] = field(default_factory=list)
    receipts: list[FetchReceipt] = field(default_factory=list)


def _reported_size(receipt: FetchReceipt) -> int | None:
    headers = {key.lower(): value for key, value in receipt.response_headers.items()}
    content_range = headers.get("content-range", "").strip()
    match = _CONTENT_RANGE_TOTAL.match(content_range)
    if match and match.group(1) != "*":
        return int(match.group(1))
    content_length = headers.get("content-length", "").strip()
    if content_length.isdigit():
        return int(content_length)
    return None


class RemoteFileProber:
    """Probe at most ``max_files`` candidates without retrieving their payloads."""

    name = "remote-file-head-range"

    def __init__(self, client: HttpClient, *, max_files: int = 20) -> None:
        if max_files < 0:
            raise ValueError("max_files must be non-negative")
        self.client = client
        self.max_files = max_files

    def probe(self, files: list[RemoteFileCandidate]) -> RemoteProbeBatch:
        batch = RemoteProbeBatch()
        ordered = sorted(files, key=lambda item: item.file_id)
        for item in ordered[: self.max_files]:
            self._probe_one(item, batch)
        for item in ordered[self.max_files :]:
            batch.issues.append(
                self._issue(
                    item,
                    "REMOTE_NOT_PROBED",
                    "REVIEW",
                    f"remote probe limit {self.max_files} reached",
                )
            )
        return batch

    def _probe_one(
        self,
        item: RemoteFileCandidate,
        batch: RemoteProbeBatch,
    ) -> None:
        try:
            receipt = self.client.probe(item.uri)
        except FetchError as error:
            status = int(getattr(error, "status", 0) or 0)
            batch.probes.append(
                RemoteFileProbe(
                    probe_id=stable_id(
                        "probe", {"file_id": item.file_id, "status": status}
                    ),
                    file_id=item.file_id,
                    run_accession=item.run_accession,
                    uri=item.uri,
                    method="HEAD_OR_RANGE",
                    status=status,
                    reachable=False,
                    expected_size_bytes=item.size_bytes,
                    reported_size_bytes=None,
                    size_matches=None,
                    accept_ranges="",
                    etag="",
                    last_modified="",
                )
            )
            batch.issues.append(
                self._issue(
                    item,
                    "REMOTE_UNREACHABLE",
                    "BLOCK",
                    f"remote object probe failed: {error}",
                )
            )
            return

        reported = _reported_size(receipt)
        expected = item.size_bytes
        size_matches = (
            reported == expected
            if reported is not None and expected is not None
            else None
        )
        headers = receipt.response_headers
        batch.receipts.append(receipt)
        batch.probes.append(
            RemoteFileProbe(
                probe_id=stable_id(
                    "probe",
                    {
                        "file_id": item.file_id,
                        "method": receipt.method,
                        "status": receipt.status,
                        "reported_size_bytes": reported,
                    },
                ),
                file_id=item.file_id,
                run_accession=item.run_accession,
                uri=item.uri,
                method=receipt.method,
                status=receipt.status,
                reachable=True,
                expected_size_bytes=expected,
                reported_size_bytes=reported,
                size_matches=size_matches,
                accept_ranges=headers.get("accept-ranges", ""),
                etag=headers.get("etag", ""),
                last_modified=headers.get("last-modified", ""),
                receipt=receipt,
            )
        )
        if size_matches is False:
            batch.issues.append(
                self._issue(
                    item,
                    "REMOTE_SIZE_MISMATCH",
                    "BLOCK",
                    f"ENA reports {expected} bytes but remote server reports "
                    f"{reported} bytes",
                )
            )
        elif reported is None:
            batch.issues.append(
                self._issue(
                    item,
                    "REMOTE_SIZE_UNKNOWN",
                    "REVIEW",
                    "remote object is reachable but did not report a total size",
                )
            )

    @staticmethod
    def _issue(
        item: RemoteFileCandidate,
        code: str,
        severity: str,
        message: str,
    ) -> RemoteFileIssue:
        return RemoteFileIssue(
            issue_id=stable_id(
                "remote-issue",
                {"file_id": item.file_id, "code": code, "message": message},
            ),
            issue_code=code,
            severity=severity,
            run_accession=item.run_accession,
            file_id=item.file_id,
            message=message,
        )
