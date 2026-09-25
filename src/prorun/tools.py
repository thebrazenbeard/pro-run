from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable

from .store import Store


_TOOL_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_effects (
    request_id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL,
    result_json TEXT,
    result_sha256 TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    effect_occurred INTEGER NOT NULL,
    evidence_digest TEXT NOT NULL,
    result_json TEXT,
    reconciled_at REAL NOT NULL,
    FOREIGN KEY (request_id) REFERENCES tool_effects(request_id)
);
"""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    capability: str
    mutation: bool = False


@dataclass(frozen=True)
class ToolExecution:
    request_id: str
    tool_name: str
    output: dict[str, Any]
    replayed: bool
    result_sha256: str


class ToolError(RuntimeError):
    pass


class CapabilityDenied(ToolError):
    pass


class IdempotencyConflict(ToolError):
    pass


class AmbiguousEffect(ToolError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ToolRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.store.connection.executescript(_TOOL_LEDGER_SCHEMA)
        effect_columns = {
            row["name"] for row in self.store.connection.execute("PRAGMA table_info(tool_effects)")
        }
        if "request_json" not in effect_columns:
            self.store.connection.execute("ALTER TABLE tool_effects ADD COLUMN request_json TEXT")
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        if not spec.name or spec.name in self._specs:
            raise ValueError(f"duplicate or empty tool name: {spec.name!r}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self, allowed_capabilities: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in sorted(self._specs):
            spec = self._specs[name]
            if spec.capability not in allowed_capabilities:
                continue
            out.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                    "capability": spec.capability,
                    "mutation": spec.mutation,
                }
            )
        return out

    def _validate_arguments(self, spec: ToolSpec, arguments: dict[str, Any]) -> None:
        schema = spec.input_schema
        if schema.get("type") == "object" and not isinstance(arguments, dict):
            raise ToolError("tool arguments must be an object")
        for key in schema.get("required", []):
            if key not in arguments:
                raise ToolError(f"missing required argument: {key}")

    def execute(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        request_id: str,
        allowed_capabilities: set[str],
        now: float,
    ) -> ToolExecution:
        if name not in self._specs:
            raise ToolError(f"unknown tool: {name}")
        spec = self._specs[name]
        if spec.capability not in allowed_capabilities:
            raise CapabilityDenied(f"capability not granted: {spec.capability}")
        self._validate_arguments(spec, arguments)
        request_payload = _canonical_json({"tool": name, "arguments": arguments})
        request_digest = _sha256(request_payload)

        if not spec.mutation:
            output = self._handlers[name](arguments)
            result_json = _canonical_json(output)
            return ToolExecution(
                request_id=request_id,
                tool_name=name,
                output=output,
                replayed=False,
                result_sha256=_sha256(result_json),
            )

        row = self.store.connection.execute(
            "SELECT * FROM tool_effects WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is not None:
            if row["request_sha256"] != request_digest or row["tool_name"] != name:
                raise IdempotencyConflict("request_id was already used for different input")
            if row["state"] == "COMMITTED":
                output = json.loads(row["result_json"])
                return ToolExecution(
                    request_id=request_id,
                    tool_name=name,
                    output=output,
                    replayed=True,
                    result_sha256=str(row["result_sha256"]),
                )
            if row["state"] == "RECONCILED_NO_EFFECT":
                self.store.connection.execute(
                    """
                    UPDATE tool_effects
                    SET state='EXECUTING', result_json=NULL, result_sha256=NULL, updated_at=?
                    WHERE request_id=?
                    """,
                    (now, request_id),
                )
            else:
                raise AmbiguousEffect(
                    f"mutation request {request_id} is in unresolved state {row['state']}"
                )
        else:
            self.store.connection.execute(
                """
                INSERT INTO tool_effects (
                    request_id, tool_name, request_sha256, request_json, state,
                    result_json, result_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'EXECUTING', NULL, NULL, ?, ?)
                """,
                (request_id, name, request_digest, request_payload, now, now),
            )
        try:
            output = self._handlers[name](arguments)
        except BaseException:
            self.store.connection.execute(
                "UPDATE tool_effects SET state='ATTEMPTED_UNKNOWN', updated_at=? WHERE request_id=?",
                (now, request_id),
            )
            raise

        result_json = _canonical_json(output)
        result_digest = _sha256(result_json)
        self.store.connection.execute(
            """
            UPDATE tool_effects
            SET state='COMMITTED', result_json=?, result_sha256=?, updated_at=?
            WHERE request_id=?
            """,
            (result_json, result_digest, now, request_id),
        )
        return ToolExecution(
            request_id=request_id,
            tool_name=name,
            output=output,
            replayed=False,
            result_sha256=result_digest,
        )
    def effect_state(self, request_id: str) -> str | None:
        row = self.store.connection.execute(
            "SELECT state FROM tool_effects WHERE request_id=?", (request_id,)
        ).fetchone()
        return None if row is None else str(row["state"])

    def recover_reconciled(
        self,
        *,
        request_id: str,
        allowed_capabilities: set[str],
        now: float,
    ) -> ToolExecution:
        row = self.store.connection.execute(
            "SELECT * FROM tool_effects WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ToolError(f"unknown mutation request: {request_id}")
        if row["state"] == "COMMITTED":
            output = json.loads(row["result_json"])
            return ToolExecution(
                request_id=request_id,
                tool_name=str(row["tool_name"]),
                output=output,
                replayed=True,
                result_sha256=str(row["result_sha256"]),
            )
        if row["state"] != "RECONCILED_NO_EFFECT":
            raise AmbiguousEffect(
                f"mutation request {request_id} remains unresolved in state {row['state']}"
            )
        if not row["request_json"]:
            raise ToolError("mutation request predates recoverable request payload storage")
        payload = json.loads(row["request_json"] )
        return self.execute(
            name=str(payload["tool"]),
            arguments=dict(payload["arguments"]),
            request_id=request_id,
            allowed_capabilities=allowed_capabilities,
            now=now,
        )

    def reconcile(
        self,
        *,
        request_id: str,
        effect_occurred: bool,
        evidence_digest: str,
        now: float,
        result: dict[str, Any] | None = None,
    ) -> None:
        if len(evidence_digest) != 64 or any(
            ch not in "0123456789abcdefABCDEF" for ch in evidence_digest
        ):
            raise ValueError("evidence_digest must be a 64-character SHA-256 hex digest")
        row = self.store.connection.execute(
            "SELECT * FROM tool_effects WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise ToolError(f"unknown mutation request: {request_id}")
        if row["state"] not in {"EXECUTING", "ATTEMPTED_UNKNOWN"}:
            raise ToolError(
                f"mutation request {request_id} cannot be reconciled from state {row['state']}"
            )
        if effect_occurred and result is None:
            raise ValueError("result is required when reconciliation confirms the effect")
        if not effect_occurred and result is not None:
            raise ValueError("result must be omitted when reconciliation confirms no effect")

        result_json = _canonical_json(result) if result is not None else None
        result_digest = _sha256(result_json) if result_json is not None else None
        self.store.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.store.connection.execute(
                "SELECT state FROM tool_effects WHERE request_id = ?", (request_id,)
            ).fetchone()
            if current is None or current["state"] not in {"EXECUTING", "ATTEMPTED_UNKNOWN"}:
                raise ToolError("mutation state changed during reconciliation")
            self.store.connection.execute(
                """
                INSERT INTO tool_reconciliations (
                    request_id, effect_occurred, evidence_digest, result_json, reconciled_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, int(effect_occurred), evidence_digest.lower(), result_json, now),
            )
            if effect_occurred:
                self.store.connection.execute(
                    """
                    UPDATE tool_effects
                    SET state='COMMITTED', result_json=?, result_sha256=?, updated_at=?
                    WHERE request_id=?
                    """,
                    (result_json, result_digest, now, request_id),
                )
            else:
                self.store.connection.execute(
                    """
                    UPDATE tool_effects
                    SET state='RECONCILED_NO_EFFECT', result_json=NULL, result_sha256=NULL, updated_at=?
                    WHERE request_id=?
                    """,
                    (now, request_id),
                )
            self.store.connection.execute("COMMIT")
        except BaseException:
            self.store.connection.execute("ROLLBACK")
            raise

