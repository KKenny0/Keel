from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from keel_runtime import (
    AgentContext,
    AgentLoop,
    AgentLoopConfig,
    ComposedPrompt,
    FileSkillComposer,
    Message,
    PrefixStableContext,
)


def run(coro):
    return asyncio.run(coro)


class FakeChatClient:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        if not self.responses:
            raise AssertionError("unexpected chat call")
        return self.responses.pop(0)


def test_file_skill_composer_loads_json_and_yaml_in_priority_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "research.json").write_text(
        json.dumps(
            {
                "name": "research",
                "description": "Collect evidence",
                "priority": 5,
                "constraints": ["cite sources"],
                "examples": [{"input": "question", "output": "cited answer"}],
                "schema_overrides": {"answer": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "draft.yaml").write_text(
        """
name: draft
description: Write compactly
priority: 10
constraints:
  - keep it concise
examples:
  - one paragraph
schema_overrides: {"tone": {"type": "string"}}
""".strip(),
        encoding="utf-8",
    )

    composed = FileSkillComposer(tmp_path).compose(
        "Base prompt",
        AgentContext(task="write"),
    )

    assert composed.skill_names == ["draft", "research"]
    assert composed.constraints == ["keep it concise", "cite sources"]
    assert composed.examples == ["one paragraph", {"input": "question", "output": "cited answer"}]
    assert composed.schema_overrides == {
        "answer": {"type": "string"},
        "tone": {"type": "string"},
    }
    assert composed.content.index("draft") < composed.content.index("research")
    assert "Base prompt" in composed.content
    assert "Schema Overrides" in composed.content


def test_file_skill_composer_ignores_disabled_skills(tmp_path: Path) -> None:
    (tmp_path / "enabled.json").write_text(
        json.dumps({"name": "enabled", "constraints": ["use me"]}),
        encoding="utf-8",
    )
    (tmp_path / "disabled.json").write_text(
        json.dumps({"name": "disabled", "enabled": False, "constraints": ["skip me"]}),
        encoding="utf-8",
    )

    composed = FileSkillComposer(tmp_path).compose("", AgentContext(task="task"))

    assert composed.skill_names == ["enabled"]
    assert composed.constraints == ["use me"]
    assert "skip me" not in composed.content


def test_agent_loop_uses_prompt_composer_for_system_prompt(tmp_path: Path) -> None:
    (tmp_path / "skill.json").write_text(
        json.dumps(
            {
                "name": "formatter",
                "description": "Format the answer",
                "constraints": ["return JSON"],
            }
        ),
        encoding="utf-8",
    )
    client = FakeChatClient({"content": '{"ok": true}'})
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(
            system_prompt="Base prompt",
            prompt_composer=FileSkillComposer(tmp_path),
        ),
    )

    result = run(loop.run("question"))

    system_message = client.calls[0]["messages"][0]
    assert system_message.role == "system"
    assert "Base prompt" in system_message.content
    assert "formatter" in system_message.content
    assert "return JSON" in system_message.content
    assert result.composed_prompts[0].skill_names == ["formatter"]
    assert result.output == {"ok": True}


def test_agent_loop_without_prompt_composer_keeps_existing_system_prompt() -> None:
    client = FakeChatClient({"content": "ok"})
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(system_prompt="Plain system prompt"),
    )

    result = run(loop.run("question"))

    assert result.composed_prompts == []
    assert client.calls[0]["messages"][0].content == "Plain system prompt"
    assert result.output == "ok"


def test_custom_composer_can_filter_by_agent_context_metadata() -> None:
    class StageComposer:
        def compose(self, base_prompt: str, context: AgentContext) -> ComposedPrompt:
            stage = context.metadata["stage"]
            constraint = f"only run {stage} checks"
            return ComposedPrompt(
                content=f"{base_prompt}\n{constraint}",
                constraints=[constraint],
                metadata={"stage": stage},
            )

    client = FakeChatClient({"content": "done"})
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(
            system_prompt="script-weaver",
            prompt_composer=StageComposer(),
        ),
    )

    result = run(
        loop.run(
            [Message(role="user", content="run stage")],
            agent_context={"metadata": {"stage": "draft"}},
        )
    )

    system_prompt = client.calls[0]["messages"][0].content
    assert "script-weaver" in system_prompt
    assert "only run draft checks" in system_prompt
    assert result.composed_prompts[0].constraints == ["only run draft checks"]
