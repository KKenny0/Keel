"""Structured model API configuration and usage records."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

MODEL_USAGE_PREFIX = "KEEL_MODEL_USAGE_JSON:"
MODEL_USAGE_ARTIFACT = "model-usage.json"


class ModelProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    AZURE_OPENAI = "azure_openai"
    CUSTOM = "custom"


@dataclass(slots=True)
class ModelUsage:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelUsage:
        return cls(
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            total_tokens=int(data.get("total_tokens") or 0),
            cost_usd=(
                float(data["cost_usd"])
                if data.get("cost_usd") is not None
                else None
            ),
        )


@dataclass(slots=True)
class ModelConfig:
    provider: ModelProvider | str
    model: str
    api_key_ref: str | None = None
    endpoint: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    fallback: ModelConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        return self._to_dict(set())

    def _to_dict(self, seen: set[int]) -> dict[str, Any]:
        identity = id(self)
        if identity in seen:
            raise ValueError("ModelConfig fallback chain contains a cycle")
        seen.add(identity)
        return {
            "provider": self.provider_value,
            "model": self.model,
            "api_key_ref": self.api_key_ref,
            "endpoint": self.endpoint,
            "params": dict(self.params),
            "fallback": self.fallback._to_dict(seen) if self.fallback else None,
        }

    @property
    def provider_value(self) -> str:
        if isinstance(self.provider, ModelProvider):
            return self.provider.value
        return str(self.provider)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelConfig:
        fallback = data.get("fallback")
        return cls(
            provider=data.get("provider") or "",
            model=str(data.get("model") or ""),
            api_key_ref=(
                str(data["api_key_ref"])
                if data.get("api_key_ref") is not None
                else None
            ),
            endpoint=str(data["endpoint"]) if data.get("endpoint") is not None else None,
            params=dict(data.get("params") or {}),
            fallback=cls.from_dict(fallback) if isinstance(fallback, dict) else None,
        )

    def api_key_refs(self) -> list[str]:
        refs: list[str] = []
        current: ModelConfig | None = self
        seen: set[int] = set()
        while current is not None:
            identity = id(current)
            if identity in seen:
                break
            seen.add(identity)
            if current.api_key_ref and current.api_key_ref not in refs:
                refs.append(current.api_key_ref)
            current = current.fallback
        return refs


class ProviderRegistry:
    """Warning-level validation for model provider configuration."""

    def __init__(self) -> None:
        self._providers: dict[str, set[str]] = {
            ModelProvider.OPENAI.value: {"gpt-4.1", "gpt-4o", "gpt-4o-mini"},
            ModelProvider.ANTHROPIC.value: {
                "claude-sonnet-4-20250514",
                "claude-3-5-sonnet-latest",
            },
            ModelProvider.GOOGLE.value: {"gemini-1.5-pro", "gemini-1.5-flash"},
            ModelProvider.AZURE_OPENAI.value: set(),
            ModelProvider.CUSTOM.value: set(),
        }

    def register_model(self, provider: str, model: str) -> None:
        provider_key = str(provider)
        self._providers.setdefault(provider_key, set()).add(str(model))

    def known_providers(self) -> list[str]:
        return sorted(self._providers)

    def known_models(self, provider: str) -> list[str]:
        return sorted(self._providers.get(str(provider), set()))

    def validate(
        self,
        config: ModelConfig,
        *,
        secret_env: dict[str, str] | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        self._validate_config(config, secret_env or {}, warnings, set())
        return warnings

    def _validate_config(
        self,
        config: ModelConfig,
        secret_env: dict[str, str],
        warnings: list[str],
        seen: set[int],
    ) -> None:
        identity = id(config)
        if identity in seen:
            warnings.append("model fallback chain contains a cycle")
            return
        seen.add(identity)

        provider = config.provider_value.strip()
        model = config.model.strip()
        if not provider:
            warnings.append("model provider is empty")
        elif provider not in self._providers:
            warnings.append(f"unknown model provider: {provider}")
        if not model:
            warnings.append("model name is empty")
        else:
            known_models = self._providers.get(provider, set())
            if known_models and model not in known_models:
                warnings.append(f"unknown model for provider {provider}: {model}")

        if config.api_key_ref and (
            config.api_key_ref not in secret_env and config.api_key_ref not in os.environ
        ):
            warnings.append(f"api_key_ref is not available: {config.api_key_ref}")

        if config.fallback is not None:
            self._validate_config(config.fallback, secret_env, warnings, seen)


def parse_model_usage(
    output: list[str] | str,
    *,
    artifact_dir: str | Path | None = None,
    scan_output: bool = True,
) -> tuple[ModelUsage | None, list[str]]:
    warnings: list[str] = []
    if artifact_dir is not None:
        path = Path(artifact_dir) / MODEL_USAGE_ARTIFACT
        if path.exists():
            try:
                return ModelUsage.from_dict(json.loads(path.read_text(encoding="utf-8"))), warnings
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                warnings.append(f"model usage artifact could not be parsed: {exc}")
                return None, warnings

    if not scan_output:
        return None, warnings

    lines = output.splitlines() if isinstance(output, str) else output
    for line in lines:
        if not line.startswith(MODEL_USAGE_PREFIX):
            continue
        payload = line.removeprefix(MODEL_USAGE_PREFIX)
        try:
            return ModelUsage.from_dict(json.loads(payload)), warnings
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            warnings.append(f"model usage output could not be parsed: {exc}")
            return None, warnings
    return None, warnings
