"""Helpers for keeping sensitive runtime values out of persisted records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REDACTION = "[redacted]"
SENSITIVE_ENV_MARKERS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASS",
    "API_KEY",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "CREDENTIAL",
    "AUTH",
)


def is_sensitive_key(key: str) -> bool:
    upper_key = key.upper()
    return any(marker in upper_key for marker in SENSITIVE_ENV_MARKERS)


def sanitize_env(env: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): REDACTION if is_sensitive_key(str(key)) else str(value)
        for key, value in env.items()
    }


def sanitize_secret_env(secret_env: Mapping[str, str]) -> dict[str, str]:
    return {str(key): REDACTION for key in secret_env}


def collect_secret_values(
    env: Mapping[str, str],
    secret_env: Mapping[str, str] | None = None,
) -> list[str]:
    values: list[str] = []
    for key, value in env.items():
        text = str(value)
        if is_sensitive_key(str(key)) and text and text != REDACTION:
            values.append(text)
    for value in (secret_env or {}).values():
        text = str(value)
        if text and text != REDACTION:
            values.append(text)
    return sorted(set(values), key=len, reverse=True)


def redact_text(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTION)
    return redacted


def redact_data(data: Any, secrets: list[str] | tuple[str, ...]) -> Any:
    if isinstance(data, str):
        return redact_text(data, secrets)
    if isinstance(data, dict):
        return {
            key: REDACTION if is_sensitive_key(str(key)) else redact_data(value, secrets)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_data(value, secrets) for value in data]
    if isinstance(data, tuple):
        return tuple(redact_data(value, secrets) for value in data)
    return data
