"""Protocol implemented by network-backed discovery adapters."""

from __future__ import annotations

from typing import Protocol

from ..crawl_models import DiscoveryRecord


class DiscoveryAdapter(Protocol):
    name: str

    def discover(self, query: str, limit: int) -> list[DiscoveryRecord]: ...

