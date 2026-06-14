from __future__ import annotations

import asyncio

from keel_runtime import ToolCall, ToolRegistry, ToolResult, ToolSpec, ensure_tool_spec, tool


def run(coro):
    return asyncio.run(coro)


def test_tool_decorator_generates_schema_and_registry_executes_async_tool() -> None:
    @tool(name="add_numbers", description="Add two integers")
    async def add(left: int, right: int = 1) -> int:
        return left + right

    spec = ensure_tool_spec(add)
    registry = ToolRegistry([add])

    result = run(registry.execute(ToolCall(name="add_numbers", arguments={"left": 2})))

    assert result == ToolResult.success("add_numbers", 3)
    assert spec.to_dict() == {
        "name": "add_numbers",
        "description": "Add two integers",
        "parameters": {
            "type": "object",
            "properties": {
                "left": {"type": "integer"},
                "right": {"type": "integer", "default": 1},
            },
            "required": ["left"],
            "additionalProperties": False,
        },
    }


def test_registry_executes_sync_callable_without_decorator() -> None:
    def shout(text: str) -> str:
        return text.upper()

    registry = ToolRegistry()
    spec = registry.register(shout)

    result = run(registry.execute("shout", {"text": "keel"}))

    assert spec.name == "shout"
    assert result.ok is True
    assert result.output == "KEEL"


def test_registry_reports_unknown_tool_as_error_result() -> None:
    registry = ToolRegistry()

    result = run(registry.execute(ToolCall(name="missing", call_id="call-1")))

    assert result.ok is False
    assert result.name == "missing"
    assert result.call_id == "call-1"
    assert result.error == "Unknown tool: missing"


def test_registry_reports_missing_unknown_and_wrong_type_arguments() -> None:
    @tool()
    def repeat(text: str, count: int) -> str:
        return text * count

    registry = ToolRegistry([repeat])

    missing = run(registry.execute("repeat", {"text": "x"}))
    unknown = run(registry.execute("repeat", {"text": "x", "count": 2, "extra": True}))
    wrong_type = run(registry.execute("repeat", {"text": "x", "count": "2"}))

    assert missing.ok is False
    assert "missing a required argument" in (missing.error or "")
    assert unknown.ok is False
    assert "unexpected keyword argument" in (unknown.error or "")
    assert wrong_type.ok is False
    assert "expected int" in (wrong_type.error or "")


def test_registry_wraps_tool_exception_as_error_result() -> None:
    @tool(name="explode")
    def explode() -> str:
        raise RuntimeError("boom")

    registry = ToolRegistry([explode])

    result = run(registry.execute("explode"))

    assert result.ok is False
    assert result.error == "boom"


def test_tool_call_and_result_dict_round_trip() -> None:
    call = ToolCall.from_dict(
        {"name": "lookup", "arguments": {"query": "keel"}, "call_id": "abc"}
    )
    result = ToolResult.success(call.name, {"ok": True}, call_id=call.call_id)

    assert call.to_dict() == {
        "name": "lookup",
        "arguments": {"query": "keel"},
        "call_id": "abc",
    }
    assert result.to_dict() == {
        "name": "lookup",
        "ok": True,
        "output": {"ok": True},
        "error": None,
        "call_id": "abc",
    }


def test_tool_spec_rejects_varargs() -> None:
    def invalid(*values: str) -> str:
        return ",".join(values)

    try:
        ToolSpec.from_callable(invalid)
    except ValueError as exc:
        assert "*args or **kwargs" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_tool_timeout_returns_structured_error() -> None:
    @tool(name="slow", timeout_seconds=0.01)
    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "done"

    registry = ToolRegistry([slow])

    result = run(registry.execute("slow"))

    assert result.ok is False
    assert result.error == "Tool timed out after 0.01 seconds"
    assert result.error_details is not None
    assert result.error_details.code == "timeout"
    assert result.error_details.retryable is True


def test_tool_internal_timeout_error_is_not_wait_for_timeout() -> None:
    @tool(name="upstream", timeout_seconds=10)
    async def upstream() -> str:
        raise TimeoutError("upstream timeout")

    registry = ToolRegistry([upstream])

    result = run(registry.execute("upstream"))

    assert result.ok is False
    assert result.error == "upstream timeout"
    assert result.error_details is not None
    assert result.error_details.code == "execution_error"


def test_persisted_side_effect_tool_requires_idempotency_key() -> None:
    calls = {"send": 0}

    @tool(name="send_email", side_effect=True)
    def send_email() -> str:
        calls["send"] += 1
        return "sent"

    registry = ToolRegistry([send_email])

    result = run(registry.execute("send_email", persisted=True))

    assert result.ok is False
    assert result.error_details is not None
    assert result.error_details.code == "validation_error"
    assert "idempotency_key" in (result.error or "")
    assert calls["send"] == 0


def test_persisted_side_effect_tool_with_idempotency_key_executes() -> None:
    @tool(name="send_email", side_effect=True, idempotency_key="email-1")
    def send_email() -> str:
        return "sent"

    registry = ToolRegistry([send_email])

    result = run(registry.execute("send_email", persisted=True))

    assert result == ToolResult.success("send_email", "sent")
