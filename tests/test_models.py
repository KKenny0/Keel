from __future__ import annotations

import json
from pathlib import Path

import pytest

from keel_runtime import AgentSpec, ModelConfig, ModelProvider, ModelUsage, ProviderRegistry
from keel_runtime.models import MODEL_USAGE_PREFIX, parse_model_usage


def test_model_config_serializes_nested_fallback() -> None:
    config = ModelConfig(
        provider=ModelProvider.OPENAI,
        model="gpt-4.1",
        api_key_ref="OPENAI_API_KEY",
        params={"temperature": 0.2},
        fallback=ModelConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_ref="ANTHROPIC_API_KEY",
        ),
    )

    data = config.to_dict()
    restored = ModelConfig.from_dict(data)

    assert data["provider"] == "openai"
    assert data["fallback"]["provider"] == "anthropic"
    assert restored.to_dict() == data


def test_provider_registry_warns_without_blocking(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    registry = ProviderRegistry()
    config = ModelConfig(
        provider="openai",
        model="not-in-default-list",
        api_key_ref="OPENAI_API_KEY",
    )

    warnings = registry.validate(config, secret_env={})

    assert "unknown model for provider openai: not-in-default-list" in warnings
    assert "api_key_ref is not available: OPENAI_API_KEY" in warnings


def test_provider_registry_detects_fallback_cycle() -> None:
    config = ModelConfig(provider="openai", model="gpt-4.1")
    config.fallback = config

    warnings = ProviderRegistry().validate(config)

    assert "model fallback chain contains a cycle" in warnings
    with pytest.raises(ValueError, match="fallback chain"):
        config.to_dict()


def test_agent_spec_keeps_legacy_model_dict_unchanged() -> None:
    legacy_model = {"model": "legacy-model-name", "temperature": 0.7, "nested": {"x": 1}}
    spec = AgentSpec(name="legacy", model=legacy_model)
    serialized = spec.to_dict()
    restored = AgentSpec.from_dict(serialized)

    assert serialized["model"] == legacy_model
    assert restored.model == legacy_model


def test_agent_spec_serializes_model_config_only_when_explicit() -> None:
    spec = AgentSpec(
        name="structured",
        model=ModelConfig(
            provider="openai",
            model="gpt-4.1",
            api_key_ref="OPENAI_API_KEY",
        ),
    )

    serialized = spec.to_dict()

    assert serialized["model"]["provider"] == "openai"
    assert serialized["model"]["api_key_ref"] == "OPENAI_API_KEY"
    assert AgentSpec.from_dict(serialized).model == serialized["model"]


def test_model_usage_round_trips_and_parses_prefix() -> None:
    payload = ModelUsage(
        provider="openai",
        model="gpt-4.1",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cost_usd=0.01,
    ).to_dict()

    usage, warnings = parse_model_usage([MODEL_USAGE_PREFIX + json.dumps(payload)])

    assert warnings == []
    assert usage is not None
    assert usage.to_dict() == payload


def test_model_usage_parses_artifact_before_output(tmp_path: Path) -> None:
    artifact_usage = ModelUsage(provider="openai", model="gpt-4.1", total_tokens=10).to_dict()
    output_usage = ModelUsage(provider="anthropic", model="claude", total_tokens=99).to_dict()
    (tmp_path / "model-usage.json").write_text(
        json.dumps(artifact_usage),
        encoding="utf-8",
    )

    usage, warnings = parse_model_usage(
        [MODEL_USAGE_PREFIX + json.dumps(output_usage)],
        artifact_dir=tmp_path,
    )

    assert warnings == []
    assert usage is not None
    assert usage.to_dict() == artifact_usage


def test_model_usage_bad_json_returns_warning(tmp_path: Path) -> None:
    (tmp_path / "model-usage.json").write_text("{bad", encoding="utf-8")

    usage, warnings = parse_model_usage([], artifact_dir=tmp_path)

    assert usage is None
    assert warnings
    assert "model usage artifact could not be parsed" in warnings[0]
