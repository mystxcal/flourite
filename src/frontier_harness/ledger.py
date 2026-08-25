"""Append-only, hash-chained SQLite event ledger."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .errors import LedgerIntegrityError
from .ids import new_id
from .util import canonical_json, sha256_text, utc_now

GENESIS_HASH = "0" * 64


class LedgerEvent(BaseModel):
    seq: int
    event_id: str
    run_id: str
    timestamp: str
    event_type: str
    actor: str
    action_id: str | None
    payload: dict[str, Any]
    payload_hash: str
    previous_hash: str
    event_hash: str


class EventLedger:
    """Authoritative event store.

    Only INSERT is exposed. SQLite triggers reject UPDATE and DELETE against the
    events table, and each event commits to the previous hash and canonical
    payload. Derived state may be rebuilt from this ledger at any time.
    """

    def __init__(self, path: Path, run_id: str, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._initialize()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                action_id TEXT,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            );

            CREATE INDEX IF NOT EXISTS idx_events_run_seq
                ON events(run_id, seq);
            CREATE INDEX IF NOT EXISTS idx_events_type
                ON events(run_id, event_type);
            CREATE INDEX IF NOT EXISTS idx_events_action
                ON events(run_id, action_id);

            CREATE TRIGGER IF NOT EXISTS events_no_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS events_no_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are immutable');
            END;
            """
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> EventLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _normalize_payload(payload: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
        if isinstance(payload, BaseModel):
            return payload.model_dump(mode="json")
        return dict(payload)

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any] | BaseModel,
        *,
        actor: str = "runtime",
        action_id: str | None = None,
    ) -> LedgerEvent:
        normalized = self._normalize_payload(payload)
        payload_json = canonical_json(normalized)
        payload_hash = sha256_text(payload_json)
        event_id = new_id("evt")
        timestamp = utc_now()

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT event_hash FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                (self.run_id,),
            ).fetchone()
            previous_hash = row["event_hash"] if row else GENESIS_HASH
            event_material = canonical_json(
                {
                    "event_id": event_id,
                    "run_id": self.run_id,
                    "timestamp": timestamp,
                    "event_type": event_type,
                    "actor": actor,
                    "action_id": action_id,
                    "payload_hash": payload_hash,
                    "previous_hash": previous_hash,
                }
            )
            event_hash = sha256_text(event_material)
            cursor = self._connection.execute(
                """
                INSERT INTO events(
                    event_id, run_id, timestamp, event_type, actor, action_id,
                    payload_json, payload_hash, previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    self.run_id,
                    timestamp,
                    event_type,
                    actor,
                    action_id,
                    payload_json,
                    payload_hash,
                    previous_hash,
                    event_hash,
                ),
            )
            if cursor.lastrowid is None:
                raise LedgerIntegrityError("SQLite did not return an event sequence")
            seq = int(cursor.lastrowid)
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

        return LedgerEvent(
            seq=seq,
            event_id=event_id,
            run_id=self.run_id,
            timestamp=timestamp,
            event_type=event_type,
            actor=actor,
            action_id=action_id,
            payload=normalized,
            payload_hash=payload_hash,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )

    def events(self, *, after_seq: int = 0) -> Iterator[LedgerEvent]:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq ASC",
            (self.run_id, after_seq),
        )
        for row in rows:
            yield LedgerEvent(
                seq=row["seq"],
                event_id=row["event_id"],
                run_id=row["run_id"],
                timestamp=row["timestamp"],
                event_type=row["event_type"],
                actor=row["actor"],
                action_id=row["action_id"],
                payload=json.loads(row["payload_json"]),
                payload_hash=row["payload_hash"],
                previous_hash=row["previous_hash"],
                event_hash=row["event_hash"],
            )

    def last_event(self) -> LedgerEvent | None:
        row = self._connection.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            (self.run_id,),
        ).fetchone()
        if row is None:
            return None
        return next(self.events(after_seq=int(row["seq"]) - 1))

    def count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        return int(row["n"])

    def backup(self, destination: Path) -> Path:
        """Create a transactionally consistent SQLite backup."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self._connection.backup(target)
        finally:
            target.close()
        return destination

    @staticmethod
    def _verify_event_sequence(events: list[LedgerEvent]) -> tuple[int, str]:
        previous = GENESIS_HASH
        count = 0
        for event in events:
            payload_json = canonical_json(event.payload)
            expected_payload_hash = sha256_text(payload_json)
            if expected_payload_hash != event.payload_hash:
                raise LedgerIntegrityError(
                    f"Payload hash mismatch at event {event.seq} ({event.event_id})"
                )
            if event.previous_hash != previous:
                raise LedgerIntegrityError(
                    f"Hash-chain break at event {event.seq} ({event.event_id})"
                )
            material = canonical_json(
                {
                    "event_id": event.event_id,
                    "run_id": event.run_id,
                    "timestamp": event.timestamp,
                    "event_type": event.event_type,
                    "actor": event.actor,
                    "action_id": event.action_id,
                    "payload_hash": event.payload_hash,
                    "previous_hash": event.previous_hash,
                }
            )
            expected_event_hash = sha256_text(material)
            if expected_event_hash != event.event_hash:
                raise LedgerIntegrityError(
                    f"Event hash mismatch at event {event.seq} ({event.event_id})"
                )
            previous = event.event_hash
            count += 1
        return count, previous

    def verified_events(self) -> list[LedgerEvent]:
        """Return one transactionally consistent, hash-verified event snapshot."""

        self._connection.execute("BEGIN")
        try:
            events = list(self.events())
            self._verify_event_sequence(events)
            self._connection.execute("COMMIT")
            return events
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def verify(self) -> tuple[int, str]:
        events = self.verified_events()
        return len(events), events[-1].event_hash if events else GENESIS_HASH
