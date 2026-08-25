"""Durable operator commands and bounded live activity for one Flourite run.

The hash-chained event ledger remains the sole semantic authority.  This sidecar
stores an append-only operator inbox and transient presentation state so another
process can observe or steer a live run without becoming a second controller.
"""

from __future__ import annotations

import json
import os
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from .ids import new_id
from .models import StrictModel
from .util import canonical_json, sha256_text, utc_now


class CommandKind(StrEnum):
    STEER = "steer"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"


class CommandStatus(StrEnum):
    QUEUED = "queued"
    APPLIED = "applied"
    REJECTED = "rejected"


class RuntimeStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    SEALING = "sealing"
    COMPLETE = "complete"
    FAILED = "failed"


class ControlCommand(StrictModel):
    seq: int
    command_id: str
    run_id: str
    created_at: str
    kind: CommandKind
    text: str = ""
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CommandStatus = CommandStatus.QUEUED
    status_detail: str = ""
    processed_at: str | None = None


class ActivityRecord(StrictModel):
    seq: int
    timestamp: str
    kind: str
    label: str
    message: str
    state: str = "active"
    call_id: str | None = None
    action_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RuntimeSnapshot(StrictModel):
    run_id: str
    pid: int | None = None
    status: RuntimeStatus = RuntimeStatus.IDLE
    phase: str = "created"
    started_at: str | None = None
    updated_at: str
    current_call_id: str | None = None
    current_action_id: str | None = None
    detail: str = ""

    @property
    def process_alive(self) -> bool:
        if self.pid is None or self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        return True


class RunControlPlane:
    """SQLite sidecar shared by the runner, CLI commands, and live dashboard."""

    MAX_ACTIVITY_ROWS = 500

    def __init__(self, path: Path, run_id: str, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = path
        self.run_id = run_id
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            path,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._initialize()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS commands (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                urgency TEXT NOT NULL DEFAULT 'boundary',
                text TEXT NOT NULL,
                digest TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS commands_no_update
            BEFORE UPDATE ON commands
            BEGIN
                SELECT RAISE(ABORT, 'commands are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS commands_no_delete
            BEFORE DELETE ON commands
            BEGIN
                SELECT RAISE(ABORT, 'commands are immutable');
            END;

            CREATE TABLE IF NOT EXISTS command_receipts (
                command_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                processed_at TEXT,
                FOREIGN KEY(command_id) REFERENCES commands(command_id)
            );

            CREATE TABLE IF NOT EXISTS activity (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                message TEXT NOT NULL,
                state TEXT NOT NULL,
                call_id TEXT,
                action_id TEXT,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime (
                run_id TEXT PRIMARY KEY,
                pid INTEGER,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                current_call_id TEXT,
                current_action_id TEXT,
                detail TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_commands_run_seq
                ON commands(run_id, seq);
            CREATE INDEX IF NOT EXISTS idx_activity_seq
                ON activity(seq);
            """
        )

    def close(self) -> None:
        self._connection.close()

    def enqueue(
        self,
        kind: CommandKind,
        *,
        text: str = "",
    ) -> ControlCommand:
        normalized = text.strip()
        if kind == CommandKind.STEER and not normalized:
            raise ValueError("A steering command requires non-empty guidance")
        command_id = new_id("cmd")
        created_at = utc_now()
        digest = sha256_text(
            canonical_json(
                {
                    "command_id": command_id,
                    "run_id": self.run_id,
                    "created_at": created_at,
                    "kind": kind.value,
                    "text": normalized,
                }
            )
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            runtime = self.runtime()
            if runtime.status in {
                RuntimeStatus.SEALING,
                RuntimeStatus.COMPLETE,
                RuntimeStatus.FAILED,
            }:
                raise ValueError(
                    f"The run is {runtime.status.value}; it cannot accept operator commands"
                )
            cursor = self._connection.execute(
                """
                INSERT INTO commands(command_id, run_id, created_at, kind, urgency, text, digest)
                VALUES (?, ?, ?, ?, 'boundary', ?, ?)
                """,
                (
                    command_id,
                    self.run_id,
                    created_at,
                    kind.value,
                    normalized,
                    digest,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO command_receipts(command_id, status, detail, processed_at)
                VALUES (?, ?, ?, NULL)
                """,
                (
                    command_id,
                    CommandStatus.QUEUED.value,
                    "waiting for the active controller",
                ),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return ControlCommand(
            seq=int(cursor.lastrowid or 0),
            command_id=command_id,
            run_id=self.run_id,
            created_at=created_at,
            kind=kind,
            text=normalized,
            digest=digest,
        )

    def commands(self, *, pending_only: bool = False) -> list[ControlCommand]:
        predicate = (
            "AND COALESCE(r.status, 'queued') = 'queued'" if pending_only else ""
        )
        rows = self._connection.execute(
            f"""
            SELECT c.*, COALESCE(r.status, 'queued') AS receipt_status,
                   COALESCE(r.detail, '') AS status_detail, r.processed_at
            FROM commands c
            LEFT JOIN command_receipts r ON r.command_id = c.command_id
            WHERE c.run_id = ? {predicate}
            ORDER BY c.seq ASC
            """,
            (self.run_id,),
        ).fetchall()
        return [
            ControlCommand(
                seq=int(row["seq"]),
                command_id=str(row["command_id"]),
                run_id=str(row["run_id"]),
                created_at=str(row["created_at"]),
                kind=CommandKind(str(row["kind"])),
                text=str(row["text"]),
                digest=str(row["digest"]),
                status=CommandStatus(str(row["receipt_status"])),
                status_detail=str(row["status_detail"]),
                processed_at=(str(row["processed_at"]) if row["processed_at"] else None),
            )
            for row in rows
        ]

    def mark_command(self, command_id: str, status: CommandStatus, detail: str) -> None:
        processed_at = utc_now() if status != CommandStatus.QUEUED else None
        self._connection.execute(
            """
            INSERT INTO command_receipts(command_id, status, detail, processed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(command_id) DO UPDATE SET
                status = excluded.status,
                detail = excluded.detail,
                processed_at = excluded.processed_at
            """,
            (command_id, status.value, detail, processed_at),
        )

    def record_activity(
        self,
        *,
        kind: str,
        label: str,
        message: str,
        state: str = "active",
        call_id: str | None = None,
        action_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ActivityRecord:
        timestamp = utc_now()
        cursor = self._connection.execute(
            """
            INSERT INTO activity(
                timestamp, kind, label, message, state, call_id, action_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                kind,
                label,
                message,
                state,
                call_id,
                action_id,
                canonical_json(payload or {}),
            ),
        )
        seq = int(cursor.lastrowid or 0)
        cutoff = max(0, seq - self.MAX_ACTIVITY_ROWS)
        if cutoff:
            self._connection.execute("DELETE FROM activity WHERE seq <= ?", (cutoff,))
        return ActivityRecord(
            seq=seq,
            timestamp=timestamp,
            kind=kind,
            label=label,
            message=message,
            state=state,
            call_id=call_id,
            action_id=action_id,
            payload=payload or {},
        )

    def recent_activity(self, *, limit: int = 40, after_seq: int = 0) -> list[ActivityRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM activity
            WHERE seq > ?
            ORDER BY seq DESC
            LIMIT ?
            """,
            (after_seq, max(1, min(limit, self.MAX_ACTIVITY_ROWS))),
        ).fetchall()
        return [
            ActivityRecord(
                seq=int(row["seq"]),
                timestamp=str(row["timestamp"]),
                kind=str(row["kind"]),
                label=str(row["label"]),
                message=str(row["message"]),
                state=str(row["state"]),
                call_id=str(row["call_id"]) if row["call_id"] else None,
                action_id=str(row["action_id"]) if row["action_id"] else None,
                payload=json.loads(str(row["payload_json"])),
            )
            for row in reversed(rows)
        ]

    def set_runtime(
        self,
        *,
        status: RuntimeStatus,
        phase: str,
        pid: int | None = None,
        started_at: str | None = None,
        current_call_id: str | None = None,
        current_action_id: str | None = None,
        detail: str = "",
    ) -> RuntimeSnapshot:
        updated_at = utc_now()
        self._connection.execute(
            """
            INSERT INTO runtime(
                run_id, pid, status, phase, started_at, updated_at,
                current_call_id, current_action_id, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                pid = excluded.pid,
                status = excluded.status,
                phase = excluded.phase,
                started_at = COALESCE(runtime.started_at, excluded.started_at),
                updated_at = excluded.updated_at,
                current_call_id = excluded.current_call_id,
                current_action_id = excluded.current_action_id,
                detail = excluded.detail
            """,
            (
                self.run_id,
                pid,
                status.value,
                phase,
                started_at,
                updated_at,
                current_call_id,
                current_action_id,
                detail,
            ),
        )
        return RuntimeSnapshot(
            run_id=self.run_id,
            pid=pid,
            status=status,
            phase=phase,
            started_at=started_at,
            updated_at=updated_at,
            current_call_id=current_call_id,
            current_action_id=current_action_id,
            detail=detail,
        )

    def runtime(self) -> RuntimeSnapshot:
        row = self._connection.execute(
            "SELECT * FROM runtime WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        if row is None:
            return RuntimeSnapshot(run_id=self.run_id, updated_at=utc_now())
        return RuntimeSnapshot(
            run_id=str(row["run_id"]),
            pid=int(row["pid"]) if row["pid"] is not None else None,
            status=RuntimeStatus(str(row["status"])),
            phase=str(row["phase"]),
            started_at=str(row["started_at"]) if row["started_at"] else None,
            updated_at=str(row["updated_at"]),
            current_call_id=str(row["current_call_id"]) if row["current_call_id"] else None,
            current_action_id=(
                str(row["current_action_id"]) if row["current_action_id"] else None
            ),
            detail=str(row["detail"]),
        )
