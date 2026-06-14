"""Structured output parsing helpers."""

from __future__ import annotations

import json
import re
from typing import Any

_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_NOT_FOUND = object()


class OutputValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        raw_output: str,
        code: str = "output_validation_failed",
        retryable: bool = False,
    ) -> None:
        if not code.strip():
            raise ValueError("OutputValidationError.code cannot be empty")
        if not message.strip():
            raise ValueError("OutputValidationError.message cannot be empty")
        super().__init__(message)
        self.code = code
        self.message = message
        self.raw_output = raw_output
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "raw_output": self.raw_output,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutputValidationError:
        return cls(
            message=str(data["message"]),
            raw_output=str(data.get("raw_output") or ""),
            code=str(data.get("code") or "output_validation_failed"),
            retryable=bool(data.get("retryable", False)),
        )


def parse_output(text: str, model: Any | None = None) -> Any:
    parsed = _extract_json_or_not_found(text)
    if parsed is _NOT_FOUND:
        return text
    if model is None:
        return parsed
    try:
        return _validate_model(parsed, model)
    except Exception:
        return text


def extract_json(text: str) -> Any:
    parsed = _extract_json_or_not_found(text)
    if parsed is _NOT_FOUND:
        return None
    return parsed


def _extract_json_or_not_found(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return _NOT_FOUND
    direct = _loads_or_not_found(stripped)
    if direct is not _NOT_FOUND:
        return direct

    for match in _CODE_BLOCK_RE.findall(text):
        parsed = _loads_or_not_found(match.strip())
        if parsed is not _NOT_FOUND:
            return parsed

    for balanced in _balanced_json_candidates(text):
        parsed = _loads_or_not_found(balanced)
        if parsed is not _NOT_FOUND:
            return parsed
    return _NOT_FOUND


def _validate_model(parsed: Any, model: Any) -> Any:
    if hasattr(model, "model_validate"):
        return model.model_validate(parsed)
    if hasattr(model, "parse_obj"):
        return model.parse_obj(parsed)
    if isinstance(parsed, dict):
        return model(**parsed)
    return model(parsed)


def _loads_or_not_found(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _NOT_FOUND


def _balanced_json_candidates(text: str) -> list[str]:
    starts = [index for index, char in enumerate(text) if char in "[{"]
    candidates: list[str] = []
    for start in starts:
        candidate = _balanced_from(text, start)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _balanced_from(text: str, start: int) -> str | None:
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"{": "}", "[": "]"}
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "]}":
            if not stack or char != stack.pop():
                return None
            if not stack:
                return text[start : index + 1]
    return None
