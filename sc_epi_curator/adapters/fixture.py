"""Deterministic offline adapter for the first evidence-recovery vertical slice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FixtureAdapter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        with self.path.open("r", encoding="utf-8") as handle:
            self.payload: dict[str, Any] = json.load(handle)

    def retrieval_spec(self) -> dict[str, Any]:
        return dict(self.payload["retrieval_spec"])

    def candidates(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.payload["datasets"]]

    def recover(self, dataset_id: str, issue: str) -> dict[str, Any]:
        for dataset in self.payload["datasets"]:
            if dataset["dataset_id"] != dataset_id:
                continue
            recovery = dataset.get("recovery", {})
            if recovery.get("issue") != issue:
                return {
                    "issue": issue,
                    "artifacts": [],
                    "evidence": [],
                    "graph_nodes": [],
                    "graph_edges": [],
                }
            return dict(recovery)
        raise KeyError(f"unknown fixture dataset: {dataset_id}")

