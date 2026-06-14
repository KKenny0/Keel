from __future__ import annotations

import json

from keel_runtime import OutputValidationError, parse_output


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
