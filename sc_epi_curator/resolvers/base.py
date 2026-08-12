"""Protocol for enriching discovery records with repository relationships."""

from __future__ import annotations

from typing import Protocol

from ..crawl_models import DiscoveryRecord, ResolutionBatch


class AccessionResolver(Protocol):
    name: str

    def resolve(self, records: list[DiscoveryRecord]) -> ResolutionBatch: ...
