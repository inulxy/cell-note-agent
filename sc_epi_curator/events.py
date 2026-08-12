"""SQLite-backed, hash-chained event store for reproducible runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StoredEvent:
    sequence: int
    run_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str
    idempotency_key: str
    previous_hash: str
    event_hash: str


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                UNIQUE(run_id, idempotency_key)
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _last_hash(self, run_id: str) -> str:
        row = self.connection.execute(
            "SELECT event_hash FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return str(row["event_hash"]) if row else GENESIS_HASH

    def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> StoredEvent:
        existing = self.connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND idempotency_key = ?",
            (run_id, idempotency_key),
        ).fetchone()
        if existing:
            stored = self._row_to_event(existing)
            if stored.event_type != event_type or stored.payload != payload:
                raise ValueError(
                    "idempotency key was reused with different event content: "
                    f"{run_id}/{idempotency_key}"
                )
            return stored

        created_at = datetime.now(timezone.utc).isoformat()
        previous_hash = self._last_hash(run_id)
        material = canonical_json(
            {
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload,
                "created_at": created_at,
                "idempotency_key": idempotency_key,
                "previous_hash": previous_hash,
            }
        )
        event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        self.connection.execute(
            """
            INSERT INTO events (
                run_id, event_type, payload_json, created_at,
                idempotency_key, previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                canonical_json(payload),
                created_at,
                idempotency_key,
                previous_hash,
                event_hash,
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND idempotency_key = ?",
            (run_id, idempotency_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("event insert did not persist")
        return self._row_to_event(row)

    def list_events(self, run_id: str) -> list[StoredEvent]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def verify_chain(self, run_id: str) -> bool:
        previous_hash = GENESIS_HASH
        for event in self.list_events(run_id):
            if event.previous_hash != previous_hash:
                return False
            material = canonical_json(
                {
                    "run_id": event.run_id,
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "created_at": event.created_at,
                    "idempotency_key": event.idempotency_key,
                    "previous_hash": event.previous_hash,
                }
            )
            expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if expected != event.event_hash:
                return False
            previous_hash = event.event_hash
        return True

    def export_jsonl(self, run_id: str, destination: str | Path) -> None:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for event in self.list_events(run_id):
                handle.write(
                    canonical_json(
                        {
                            "sequence": event.sequence,
                            "run_id": event.run_id,
                            "event_type": event.event_type,
                            "payload": event.payload,
                            "created_at": event.created_at,
                            "idempotency_key": event.idempotency_key,
                            "previous_hash": event.previous_hash,
                            "event_hash": event.event_hash,
                        }
                    )
                    + "\n"
                )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        return StoredEvent(
            sequence=int(row["sequence"]),
            run_id=str(row["run_id"]),
            event_type=str(row["event_type"]),
            payload=json.loads(row["payload_json"]),
            created_at=str(row["created_at"]),
            idempotency_key=str(row["idempotency_key"]),
            previous_hash=str(row["previous_hash"]),
            event_hash=str(row["event_hash"]),
        )
