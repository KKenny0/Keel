from __future__ import annotations

import json

from keel_runtime import ToolError, ToolResult


def test_tool_error_dict_round_trip_and_string_message() -> None:
    error = ToolError(
        code="timeout",
        message="tool timed out after 30 seconds",
        retryable=True,
        safe_to_retry=False,
    )

    data = error.to_dict()
    restored = ToolError.from_dict(json.loads(json.dumps(data)))

    assert restored.to_dict() == data
    assert str(restored) == "tool timed out after 30 seconds"


def test_tool_result_supports_typed_error_round_trip() -> None:
    result = ToolResult.failure(
        "send_email",
        ToolError(
            code="timeout",
            message="tool timed out",
            retryable=True,
            safe_to_retry=False,
        ),
        call_id="call-1",
    )

    data = result.to_dict()
    restored = ToolResult.from_dict(json.loads(json.dumps(data)))

    assert data == {
        "name": "send_email",
        "ok": False,
        "output": None,
        "error": {
            "code": "timeout",
            "message": "tool timed out",
            "retryable": True,
            "safe_to_retry": False,
        },
        "call_id": "call-1",
    }
    assert restored.error == "tool timed out"
    assert isinstance(restored.error_details, ToolError)
    assert restored.to_dict() == data


def test_tool_result_keeps_legacy_string_error_round_trip() -> None:
    result = ToolResult.failure("lookup", "boom", call_id="call-1")

    assert result.error == "boom"
    assert result.error_details is None
    assert ToolResult.from_dict(result.to_dict()).error == "boom"


def test_tool_error_factories_use_stable_codes() -> None:
    assert ToolError.unknown_tool("missing").to_dict() == {
        "code": "unknown_tool",
        "message": "Unknown tool: missing",
        "retryable": False,
        "safe_to_retry": False,
    }
    assert ToolError.validation("bad args").code == "validation_error"
    assert ToolError.execution("boom").code == "execution_error"
