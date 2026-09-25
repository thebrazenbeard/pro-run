from pathlib import Path

from prorun.context import ContextAssembler
from prorun.engine import Engine, ModelResponse, ToolCall
from prorun.store import Store
from prorun.tools import ToolRegistry, ToolSpec


class ScriptedModel:
    def __init__(self) -> None:
        self.calls = 0

    def respond(self, *, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="tool-1",
                    name="math.double",
                    arguments={"value": 6},
                )
            )
        assert any("12" in item["content"] for item in messages)
        return ModelResponse(final_text="done: 12")


def test_react_cycle_persists_tool_result_and_continues(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)
    tools.register(
        ToolSpec(
            name="math.double",
            description="double an integer",
            input_schema={"type": "object", "required": ["value"]},
            capability="math.read",
            mutation=False,
        ),
        lambda args: {"value": int(args["value"]) * 2},
    )
    engine = Engine(
        store=store,
        model=ScriptedModel(),
        tools=tools,
        context=ContextAssembler(store, max_chars=4000),
        system_prompt="Use tools when needed.",
        worker_id="worker-1",
    )

    run_id = engine.submit_task("Double six and report it.", {"math.read"}, now=1.0)
    first = engine.run_once(now=2.0)
    assert first == run_id
    assert store.get_run(run_id)["status"] == "RUNNING"

    second = engine.run_once(now=3.0)
    assert second == run_id
    run = store.get_run(run_id)
    assert run["status"] == "COMPLETED"
    assert run["final_text"] == "done: 12"
    assert store.pending_event_count() == 0


def test_task_requested_event_spawns_a_durable_run(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class FinalModel:
        def respond(self, *, messages, tools):
            return ModelResponse(final_text="finished")

    engine = Engine(
        store=store,
        model=FinalModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    store.enqueue_event(
        kind="task.requested",
        payload={"task": "scheduled task", "capabilities": []},
        dedup_key="external-1",
        now=1.0,
    )

    run_id = engine.run_once(now=2.0)
    assert run_id is not None
    assert store.get_run(run_id)["task"] == "scheduled task"
    assert store.pending_event_count() == 1

    engine.run_once(now=3.0)
    assert store.get_run(run_id)["status"] == "COMPLETED"


def test_ambiguous_tool_effect_blocks_run_until_exact_reconciliation(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)
    attempts = 0

    def flaky_write(args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("lost response after dispatch")
        return {"written": args["value"]}

    tools.register(
        ToolSpec(
            name="record.write",
            description="write once",
            input_schema={"type": "object", "required": ["value"]},
            capability="record.write",
            mutation=True,
        ),
        flaky_write,
    )

    class RecoveryModel:
        def __init__(self):
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_call=ToolCall(
                        request_id="effect-1",
                        name="record.write",
                        arguments={"value": 9},
                    )
                )
            assert any("written" in item["content"] for item in messages)
            return ModelResponse(final_text="recovered")

    engine = Engine(
        store=store,
        model=RecoveryModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("write 9", {"record.write"}, now=1.0)

    first = engine.run_once(now=2.0)
    assert first == run_id
    blocked = store.get_run(run_id)
    assert blocked["status"] == "BLOCKED_EFFECT"
    assert blocked["blocked_request_id"] == "effect-1"
    assert store.pending_event_count() == 0

    tools.reconcile(
        request_id="effect-1",
        effect_occurred=False,
        evidence_digest="b" * 64,
        now=3.0,
    )
    engine.resume_blocked_effect(run_id, now=4.0)
    assert attempts == 2
    assert store.get_run(run_id)["status"] == "RUNNING"

    engine.run_once(now=5.0)
    assert store.get_run(run_id)["status"] == "COMPLETED"
    assert store.get_run(run_id)["final_text"] == "recovered"


def test_deterministic_tool_admission_error_fails_run_without_retry_loop(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class BadToolModel:
        def respond(self, *, messages, tools):
            return ModelResponse(
                tool_call=ToolCall(
                    request_id="bad-1",
                    name="missing.tool",
                    arguments={},
                )
            )

    engine = Engine(
        store=store,
        model=BadToolModel(),
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("call unavailable tool", set(), now=1.0)

    assert engine.run_once(now=2.0) == run_id
    run = store.get_run(run_id)
    assert run["status"] == "FAILED"
    assert "unknown tool" in run["last_error"]
    assert store.pending_event_count() == 0


def test_stale_run_step_redelivery_is_acked_without_advancing_model(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    tools = ToolRegistry(store)

    class TwoTurnModel:
        def __init__(self):
            self.calls = 0

        def respond(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_call=ToolCall(
                        request_id="read-1",
                        name="read.once",
                        arguments={},
                    )
                )
            return ModelResponse(final_text="done")

    model = TwoTurnModel()
    tools.register(
        ToolSpec(
            name="read.once",
            description="read",
            input_schema={"type": "object"},
            capability="read",
            mutation=False,
        ),
        lambda args: {"value": 1},
    )
    engine = Engine(
        store=store,
        model=model,
        tools=tools,
        context=ContextAssembler(store),
        system_prompt="Run.",
        worker_id="worker-1",
    )
    run_id = engine.submit_task("read then finish", {"read"}, now=1.0)
    engine.run_once(now=2.0)
    assert model.calls == 1

    old = store.list_events(kind="run.step")[0]
    store.connection.execute(
        "UPDATE events SET status='PENDING', available_at=2.5 WHERE id=?", (old["id"],)
    )

    engine.run_once(now=3.0)
    assert model.calls == 1
    assert store.get_run(run_id)["status"] == "RUNNING"

    engine.run_once(now=4.0)
    assert model.calls == 2
    assert store.get_run(run_id)["status"] == "COMPLETED"
