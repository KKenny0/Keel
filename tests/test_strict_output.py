from __future__ import annotations

import asyncio
import json

from keel_runtime import (
    AgentLoop,
    AgentLoopConfig,
    OutputValidationError,
    PrefixStableContext,
    ToolRegistry,
    parse_output,
)


def test_output_validation_error_dict_round_trip() -> None:
    error = OutputValidationError(
        "model validation failed",
        raw_output='{"answer": 42}',
        retryable=False,
    )

    data = error.to_dict()
    restored = OutputValidationError.from_dict(json.loads(json.dumps(data)))

    assert restored.to_dict() == data
    assert str(restored) == "model validation failed"


def test_output_validation_error_uses_stable_default_code() -> None:
    error = OutputValidationError("bad output", raw_output="plain")

    assert error.to_dict() == {
        "code": "output_validation_failed",
        "message": "bad output",
        "raw_output": "plain",
        "retryable": False,
    }


def test_parse_output_fallback_behavior_is_unchanged() -> None:
    class RejectingModel:
        @classmethod
        def model_validate(cls, data: object) -> object:
            raise ValueError("invalid")

    text = '{"answer": "ok"}'

    assert parse_output(text, model=RejectingModel) == text
    assert parse_output("plain text") == "plain text"


def test_parse_output_strict_raises_typed_error_on_invalid_model_output() -> None:
    class RejectingModel:
        @classmethod
        def model_validate(cls, data: object) -> object:
            raise ValueError("invalid")

    try:
        parse_output('{"answer": "ok"}', model=RejectingModel, strict=True)
    except OutputValidationError as exc:
        assert exc.code == "output_validation_failed"
        assert exc.raw_output == '{"answer": "ok"}'
        assert exc.retryable is True
    else:
        raise AssertionError("expected OutputValidationError")


def test_agent_loop_strict_output_returns_failed_result() -> None:
    class FakeChatClient:
        async def chat(self, messages, tools):
            return {"content": '{"answer": "ok"}'}

    class RejectingModel:
        @classmethod
        def model_validate(cls, data: object) -> object:
            raise ValueError("invalid")

    async def run_loop():
        loop = AgentLoop(
            FakeChatClient(),
            PrefixStableContext(max_tokens=1_000),
            ToolRegistry(),
            AgentLoopConfig(output_model=RejectingModel, output_mode="strict"),
        )
        return await loop.run("question")

    result = asyncio.run(run_loop())

    assert result.status == "failed"
    assert result.error == "invalid"
    assert result.raw_output == '{"answer": "ok"}'
