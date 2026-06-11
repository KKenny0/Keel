from __future__ import annotations

from keel_runtime import extract_json, parse_output


def test_parse_output_reads_direct_json_object() -> None:
    assert parse_output('{"answer": 42}') == {"answer": 42}


def test_parse_output_reads_json_code_block() -> None:
    text = """Result:
```json
{"answer": "ok"}
```"""

    assert parse_output(text) == {"answer": "ok"}


def test_parse_output_reads_embedded_balanced_json() -> None:
    text = 'prefix {"items": [1, 2], "done": true} suffix'

    assert parse_output(text) == {"items": [1, 2], "done": True}


def test_parse_output_skips_invalid_balanced_json_before_valid_json() -> None:
    text = 'ignore {not json} then {"ok": true}'

    assert parse_output(text) == {"ok": True}


def test_parse_output_returns_raw_text_when_json_is_not_found() -> None:
    assert parse_output("plain text") == "plain text"


def test_parse_output_validates_with_model_validate_when_available() -> None:
    class FakeModel:
        def __init__(self, answer: str) -> None:
            self.answer = answer

        @classmethod
        def model_validate(cls, data: dict[str, str]) -> FakeModel:
            return cls(data["answer"])

    parsed = parse_output('{"answer": "ok"}', model=FakeModel)

    assert isinstance(parsed, FakeModel)
    assert parsed.answer == "ok"


def test_parse_output_returns_raw_text_when_model_validation_fails() -> None:
    class RejectingModel:
        @classmethod
        def model_validate(cls, data: object) -> object:
            raise ValueError("invalid")

    text = '{"answer": "ok"}'

    assert parse_output(text, model=RejectingModel) == text


def test_extract_json_reads_array() -> None:
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_returns_none_when_json_is_not_found() -> None:
    assert extract_json("plain text") is None
