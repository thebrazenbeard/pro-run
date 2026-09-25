from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol

from .context import ContextAssembler
from .store import Store
from .tools import AmbiguousEffect, ToolError, ToolRegistry


@dataclass(frozen=True)
class ToolCall:
    request_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    final_text: str | None = None
    tool_call: ToolCall | None = None

    def __post_init__(self) -> None:
        if (self.final_text is None) == (self.tool_call is None):
            raise ValueError("model response must contain exactly one of final_text or tool_call")


class ModelAdapter(Protocol):
    def respond(
        self, *, messages: list[dict[str, str]], tools: list[dict[str, Any]]
    ) -> ModelResponse: ...


class Engine:
    def __init__(
        self,
        *,
        store: Store,
        model: ModelAdapter,
        tools: ToolRegistry,
        context: ContextAssembler,
        system_prompt: str,
        worker_id: str,
        lease_seconds: float = 30.0,
        max_steps: int = 24,
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools
        self.context = context
        self.system_prompt = system_prompt
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_steps = max_steps

    def submit_task(self, task: str, capabilities: set[str], *, now: float) -> str:
        run_id = self.store.create_run(task=task, capabilities=capabilities, now=now)
        self.store.enqueue_event(
            kind="run.step",
            payload={"run_id": run_id, "step": 0},
            priority=0,
            dedup_key=f"run-step:{run_id}:0",
            now=now,
        )
        return run_id

    def _messages_for_run(self, run: dict[str, Any]) -> list[dict[str, str]]:
        transcript = self.store.list_run_messages(run["id"])
        task = run["task"]
        if transcript:
            rendered = "\n".join(f"{m['role']}: {m['content']}" for m in transcript)
            task = f"{task}\n\nDurable run transcript:\n{rendered}"
        return self.context.build(
            system_prompt=self.system_prompt,
            task=task,
            query=run["task"],
        )

    def resume_blocked_effect(self, run_id: str, *, now: float) -> None:
        run = self.store.get_run(run_id)
        if run["status"] != "BLOCKED_EFFECT" or not run["blocked_request_id"]:
            raise RuntimeError("run is not blocked on an unresolved effect")
        request_id = str(run["blocked_request_id"] )
        result = self.tools.recover_reconciled(
            request_id=request_id,
            allowed_capabilities=run["capabilities"],
            now=now,
        )
        self.store.record_run_message(
            run_id=run_id,
            role="tool",
            content=f"reconciled {result.tool_name} => {json.dumps(result.output, sort_keys=True)}",
            now=now,
        )
        next_step = self.store.resume_run_after_effect(run_id=run_id, now=now)
        self.store.enqueue_event(
            kind="run.step",
            payload={"run_id": run_id, "step": next_step},
            priority=0,
            dedup_key=f"run-step:{run_id}:{next_step}",
            now=now,
        )

    def run_once(self, *, now: float) -> str | None:
        event = self.store.claim_event(
            worker_id=self.worker_id,
            now=now,
            lease_seconds=self.lease_seconds,
        )
        if event is None:
            return None
        if event.kind == "task.requested":
            task = event.payload.get("task")
            capabilities = event.payload.get("capabilities", [])
            if not isinstance(task, str) or not task.strip():
                self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
                return None
            if not isinstance(capabilities, list) or not all(
                isinstance(item, str) for item in capabilities
            ):
                self.store.fail_event(
                    event.id,
                    worker_id=self.worker_id,
                    now=now,
                    retry_at=now + 60.0,
                )
                raise ValueError("task.requested capabilities must be a list of strings")
            run_id = self.submit_task(task.strip(), set(capabilities), now=now)
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id
        if event.kind != "run.step":
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return None

        run_id = str(event.payload["run_id"])
        run = self.store.get_run(run_id)
        event_step = event.payload.get("step")
        if not isinstance(event_step, int) or event_step != run["step_count"]:
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id
        if run["status"] != "RUNNING":
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id
        if run["step_count"] >= self.max_steps:
            self.store.update_run(
                run_id,
                now=now,
                status="FAILED",
                last_error="max_steps exceeded",
            )
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id

        try:
            response = self.model.respond(
                messages=self._messages_for_run(run),
                tools=self.tools.specs(run["capabilities"]),
            )
            if response.tool_call is not None:
                call = response.tool_call
                self.store.record_run_message(
                    run_id=run_id,
                    role="assistant",
                    content=(
                        f"tool_call id={call.request_id} name={call.name} "
                        f"arguments={json.dumps(call.arguments, sort_keys=True)}"
                    ),
                    now=now,
                )
                try:
                    result = self.tools.execute(
                        name=call.name,
                        arguments=call.arguments,
                        request_id=call.request_id,
                        allowed_capabilities=run["capabilities"],
                        now=now,
                    )
                except BaseException as exc:
                    effect_state = self.tools.effect_state(call.request_id)
                    if effect_state in {"EXECUTING", "ATTEMPTED_UNKNOWN"}:
                        self.store.block_run_on_effect(
                            run_id=run_id,
                            request_id=call.request_id,
                            error=f"{type(exc).__name__}: {exc}",
                            now=now,
                        )
                        self.store.ack_event(
                            event.id, worker_id=self.worker_id, now=now
                        )
                        return run_id
                    if isinstance(exc, ToolError):
                        self.store.update_run(
                            run_id,
                            now=now,
                            status="FAILED",
                            last_error=f"{type(exc).__name__}: {exc}",
                        )
                        self.store.ack_event(
                            event.id, worker_id=self.worker_id, now=now
                        )
                        return run_id
                    raise
                self.store.record_run_message(
                    run_id=run_id,
                    role="tool",
                    content=f"{call.name} => {json.dumps(result.output, sort_keys=True)}",
                    now=now,
                )
                self.store.update_run(run_id, now=now, increment_step=True)
                next_step = run["step_count"] + 1
                self.store.enqueue_event(
                    kind="run.step",
                    payload={"run_id": run_id, "step": next_step},
                    priority=event.priority,
                    dedup_key=f"run-step:{run_id}:{next_step}",
                    now=now,
                )
            else:
                assert response.final_text is not None
                self.store.record_run_message(
                    run_id=run_id,
                    role="assistant",
                    content=response.final_text,
                    now=now,
                )
                self.store.update_run(
                    run_id,
                    now=now,
                    status="COMPLETED",
                    final_text=response.final_text,
                    increment_step=True,
                )
            self.store.ack_event(event.id, worker_id=self.worker_id, now=now)
            return run_id
        except BaseException as exc:
            self.store.update_run(run_id, now=now, last_error=f"{type(exc).__name__}: {exc}")
            self.store.fail_event(
                event.id,
                worker_id=self.worker_id,
                now=now,
                retry_at=now + min(60.0, 2.0 ** min(event.attempts, 6)),
            )
            raise
