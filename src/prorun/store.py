from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    priority INTEGER NOT NULL,
    dedup_key TEXT UNIQUE,
    status TEXT NOT NULL,
    available_at REAL NOT NULL,
    lease_owner TEXT,
    lease_until REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_claim_idx
ON events(status, available_at, priority DESC, created_at ASC);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    salience REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_rank_idx
ON memories(salience DESC, created_at DESC);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    final_text TEXT,
    last_error TEXT,
    blocked_request_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS run_messages_idx ON run_messages(run_id, id);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    every_seconds REAL,
    next_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_subject_idx ON journal(subject_id, seq);
"""


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    payload: dict[str, Any]
    priority: int
    status: str
    attempts: int
    lease_owner: str | None
    lease_until: float | None


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(_SCHEMA)
        run_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runs)")}
        if "blocked_request_id" not in run_columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN blocked_request_id TEXT")

    def close(self) -> None:
        self.connection.close()

    def append_journal(
        self,
        *,
        event_type: str,
        subject_id: str | None,
        payload: dict[str, Any],
        now: float,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO journal (event_type, subject_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (
                event_type,
                subject_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
        return int(cursor.lastrowid)

    def list_journal(
        self, *, subject_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if subject_id is None:
            rows = self.connection.execute(
                "SELECT * FROM journal ORDER BY seq ASC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM journal WHERE subject_id=? ORDER BY seq ASC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "event_type": str(row["event_type"]),
                "subject_id": row["subject_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def enqueue_event(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        priority: int = 0,
        dedup_key: str | None = None,
        now: float,
        available_at: float | None = None,
    ) -> str:
        if dedup_key:
            row = self.connection.execute(
                "SELECT id FROM events WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            if row:
                return str(row["id"])
        event_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO events (
                id, kind, payload_json, priority, dedup_key, status,
                available_at, lease_owner, lease_until, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, NULL, NULL, 0, ?, ?)
            """,
            (
                event_id,
                kind,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                int(priority),
                dedup_key,
                now if available_at is None else available_at,
                now,
                now,
            ),
        )
        self.append_journal(
            event_type="EVENT_ENQUEUED",
            subject_id=event_id,
            payload={"kind": kind, "priority": int(priority), "dedup_key": dedup_key},
            now=now,
        )
        return event_id


    def create_run(self, *, task: str, capabilities: set[str], now: float) -> str:
        run_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO runs (id, task, status, capabilities_json, step_count, created_at, updated_at)
            VALUES (?, ?, 'RUNNING', ?, 0, ?, ?)
            """,
            (run_id, task, json.dumps(sorted(capabilities)), now, now),
        )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return {
            "id": str(row["id"]),
            "task": str(row["task"]),
            "status": str(row["status"]),
            "capabilities": set(json.loads(row["capabilities_json"])),
            "step_count": int(row["step_count"]),
            "final_text": row["final_text"],
            "last_error": row["last_error"],
            "blocked_request_id": row["blocked_request_id"],
        }

    def update_run(
        self, run_id: str, *, now: float, status: str | None = None,
        final_text: str | None = None, last_error: str | None = None,
        increment_step: bool = False,
    ) -> None:
        sets = ["updated_at=?"]
        values: list[Any] = [now]
        if status is not None:
            sets.append("status=?")
            values.append(status)
        if final_text is not None:
            sets.append("final_text=?")
            values.append(final_text)
        if last_error is not None:
            sets.append("last_error=?")
            values.append(last_error)
        if increment_step:
            sets.append("step_count=step_count+1")
        values.append(run_id)
        self.connection.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", values)

    def block_run_on_effect(
        self, *, run_id: str, request_id: str, error: str, now: float
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE runs
            SET status='BLOCKED_EFFECT', blocked_request_id=?, last_error=?, updated_at=?
            WHERE id=? AND status='RUNNING'
            """,
            (request_id, error, now, run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("run could not enter BLOCKED_EFFECT")
        self.append_journal(
            event_type="RUN_BLOCKED_EFFECT",
            subject_id=run_id,
            payload={"request_id": request_id, "error": error},
            now=now,
        )

    def resume_run_after_effect(self, *, run_id: str, now: float) -> int:
        row = self.connection.execute(
            "SELECT step_count, status FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        if row["status"] != "BLOCKED_EFFECT":
            raise RuntimeError("run is not blocked on an effect")
        next_step = int(row["step_count"]) + 1
        self.connection.execute(
            """
            UPDATE runs
            SET status='RUNNING', blocked_request_id=NULL, last_error=NULL,
                step_count=step_count+1, updated_at=?
            WHERE id=?
            """,
            (now, run_id),
        )
        self.append_journal(
            event_type="RUN_EFFECT_RECOVERED",
            subject_id=run_id,
            payload={"next_step": next_step},
            now=now,
        )
        return next_step

    def record_run_message(self, *, run_id: str, role: str, content: str, now: float) -> None:
        self.connection.execute(
            "INSERT INTO run_messages (run_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (run_id, role, content, now),
        )

    def list_run_messages(self, run_id: str, *, limit: int = 32) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT role, content FROM run_messages WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [{"role": str(r["role"]), "content": str(r["content"])} for r in reversed(rows)]

    def add_memory(
        self, *, kind: str, content: str, salience: float = 0.5, now: float
    ) -> str:
        memory_id = str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO memories (id, kind, content, salience, created_at) VALUES (?, ?, ?, ?, ?)",
            (memory_id, kind, content, float(salience), now),
        )
        return memory_id

    def search_memories(self, *, query: str = "", limit: int = 8) -> list[dict[str, Any]]:
        terms = [term.lower() for term in query.split() if term.strip()]
        rows = self.connection.execute(
            "SELECT id, kind, content, salience, created_at FROM memories ORDER BY salience DESC, created_at DESC LIMIT ?",
            (max(limit * 4, limit),),
        ).fetchall()
        scored: list[tuple[int, float, float, sqlite3.Row]] = []
        for row in rows:
            text = str(row["content"]).lower()
            matches = sum(1 for term in terms if term in text)
            scored.append((matches, float(row["salience"]), float(row["created_at"]), row))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return [
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "content": str(row["content"]),
                "salience": float(row["salience"]),
                "created_at": float(row["created_at"]),
            }
            for _, _, _, row in scored[:limit]
        ]

    def ack_event(self, event_id: str, *, worker_id: str, now: float) -> None:
        cursor = self.connection.execute(
            """
            UPDATE events SET status='DONE', lease_owner=NULL, lease_until=NULL, updated_at=?
            WHERE id=? AND status='CLAIMED' AND lease_owner=?
            """,
            (now, event_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("event acknowledgement lost lease ownership")
        self.append_journal(
            event_type="EVENT_ACKED",
            subject_id=event_id,
            payload={"worker_id": worker_id},
            now=now,
        )

    def fail_event(self, event_id: str, *, worker_id: str, now: float, retry_at: float | None = None) -> None:
        cursor = self.connection.execute(
            """
            UPDATE events SET status='PENDING', lease_owner=NULL, lease_until=NULL, available_at=?, updated_at=?
            WHERE id=? AND status='CLAIMED' AND lease_owner=?
            """,
            (now if retry_at is None else retry_at, now, event_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("event failure update lost lease ownership")
        self.append_journal(
            event_type="EVENT_RETRY_SCHEDULED",
            subject_id=event_id,
            payload={"worker_id": worker_id, "retry_at": now if retry_at is None else retry_at},
            now=now,
        )

    def list_events(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self.connection.execute(
                "SELECT * FROM events ORDER BY created_at ASC, id ASC"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE kind=? ORDER BY created_at ASC, id ASC", (kind,)
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "payload": json.loads(row["payload_json"]),
                "dedup_key": row["dedup_key"],
                "status": str(row["status"]),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    def pending_event_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE status IN ('PENDING','CLAIMED')"
        ).fetchone()
        return int(row["n"])

    def claim_event(
        self, *, worker_id: str, now: float, lease_seconds: float
    ) -> Event | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT * FROM events
                WHERE available_at <= ?
                  AND (
                    status = 'PENDING'
                    OR (status = 'CLAIMED' AND lease_until IS NOT NULL AND lease_until <= ?)
                  )
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            attempts = int(row["attempts"]) + 1
            lease_until = now + lease_seconds
            self.connection.execute(
                """
                UPDATE events
                SET status='CLAIMED', lease_owner=?, lease_until=?, attempts=?, updated_at=?
                WHERE id=?
                """,
                (worker_id, lease_until, attempts, now, row["id"]),
            )
            self.append_journal(
                event_type="EVENT_CLAIMED",
                subject_id=str(row["id"]),
                payload={"worker_id": worker_id, "attempt": attempts, "lease_until": lease_until},
                now=now,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return Event(
            id=str(row["id"]),
            kind=str(row["kind"]),
            payload=json.loads(row["payload_json"]),
            priority=int(row["priority"]),
            status="CLAIMED",
            attempts=attempts,
            lease_owner=worker_id,
            lease_until=lease_until,
        )
