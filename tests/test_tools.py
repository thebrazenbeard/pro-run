from pathlib import Path

from prorun.store import Store
from prorun.tools import ToolRegistry, ToolSpec


def test_mutation_tool_is_idempotent_across_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)
    calls: list[int] = []

    def handler(args: dict[str, object]) -> dict[str, object]:
        calls.append(int(args["value"]))
        return {"doubled": int(args["value"]) * 2}

    registry.register(
        ToolSpec(
            name="counter.write",
            description="test mutation",
            input_schema={"type": "object", "required": ["value"]},
            capability="counter.write",
            mutation=True,
        ),
        handler,
    )

    first = registry.execute(
        name="counter.write",
        arguments={"value": 4},
        request_id="req-1",
        allowed_capabilities={"counter.write"},
        now=10.0,
    )
    second = registry.execute(
        name="counter.write",
        arguments={"value": 4},
        request_id="req-1",
        allowed_capabilities={"counter.write"},
        now=11.0,
    )

    assert first.output == {"doubled": 8}
    assert second.output == first.output
    assert second.replayed is True
    assert calls == [4]


def test_ambiguous_mutation_must_be_reconciled_before_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    registry = ToolRegistry(store)
    attempts = 0

    def handler(args: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transport died after dispatch")
        return {"written": args["value"]}

    registry.register(
        ToolSpec(
            name="record.write",
            description="test ambiguous mutation",
            input_schema={"type": "object", "required": ["value"]},
            capability="record.write",
            mutation=True,
        ),
        handler,
    )

    import pytest
    from prorun.tools import AmbiguousEffect

    with pytest.raises(RuntimeError):
        registry.execute(
            name="record.write",
            arguments={"value": 7},
            request_id="req-ambiguous",
            allowed_capabilities={"record.write"},
            now=1.0,
        )

    with pytest.raises(AmbiguousEffect):
        registry.execute(
            name="record.write",
            arguments={"value": 7},
            request_id="req-ambiguous",
            allowed_capabilities={"record.write"},
            now=2.0,
        )

    registry.reconcile(
        request_id="req-ambiguous",
        effect_occurred=False,
        evidence_digest="a" * 64,
        now=3.0,
    )
    retried = registry.execute(
        name="record.write",
        arguments={"value": 7},
        request_id="req-ambiguous",
        allowed_capabilities={"record.write"},
        now=4.0,
    )

    assert retried.output == {"written": 7}
    assert attempts == 2
