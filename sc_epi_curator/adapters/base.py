"""Adapter protocol.

Network-backed GEO/SRA/ENCODE adapters will implement the same interface in the
next phase.  The first phase uses a deterministic fixture adapter so harness
behavior can be tested without external services.
"""

from __future__ import annotations

from typing import Any, Protocol


class SourceAdapter(Protocol):
    def retrieval_spec(self) -> dict[str, Any]: ...

    def candidates(self) -> list[dict[str, Any]]: ...

    def recover(self, dataset_id: str, issue: str) -> dict[str, Any]: ...

