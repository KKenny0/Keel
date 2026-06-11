"""Structured output parsing helpers."""

from __future__ import annotations

import json
import re
from typing import Any

_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_NOT_FOUND = object()


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

    balanced = _extract_balanced_json(text)
    if balanced is None:
        return _NOT_FOUND
    return _loads_or_not_found(balanced)


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


def _extract_balanced_json(text: str) -> str | None:
    starts = [index for index, char in enumerate(text) if char in "[{"]
    for start in starts:
        candidate = _balanced_from(text, start)
        if candidate is not None:
            return candidate
    return None


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
