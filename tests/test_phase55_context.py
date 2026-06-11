from __future__ import annotations

import asyncio

from keel_runtime import ContextConfig, Message, PrefixStableContext, default_token_counter


def build(provider, system_prompt, history, new_messages, config=None):
    async def _build():
        return await provider.build_messages(system_prompt, history, new_messages, config)

    return asyncio.run(_build())


def content_list(messages: list[Message]) -> list[str]:
    return [str(message.content) for message in messages]


def test_short_context_under_budget_is_not_trimmed_or_compacted() -> None:
    provider = PrefixStableContext(max_tokens=1_000)
    history = [
        Message(role="user", content="write a short answer"),
        Message(role="assistant", content="first response"),
        Message(role="user", content="follow up"),
    ]
    new_messages = [Message(role="assistant", content="latest response")]

    result = build(provider, "system prompt", history, new_messages)

    assert result.trimmed_count == 0
    assert result.consumed_count == 0
    assert result.compaction_applied is None
    assert content_list(result.messages) == [
        "system prompt",
        "write a short answer",
        "first response",
        "follow up",
        "latest response",
    ]
    assert [message.metadata["context_section"] for message in result.messages] == [
        "system",
        "task",
        "task",
        "active",
        "active",
    ]
    assert result.cache_breakpoints == [0, 2]


def test_consumed_tool_results_are_cleared_when_context_is_under_pressure() -> None:
    provider = PrefixStableContext(max_tokens=60, keep_recent_turns=10)
    history = [
        Message(role="user", content="initial question"),
        Message(role="assistant", content="first answer"),
        Message(role="tool", content="x" * 120),
        Message(role="assistant", content="used the tool"),
    ]

    result = build(provider, "system", history, [])

    assert result.consumed_count == 1
    assert result.trimmed_count == 0
    assert result.compaction_applied == "clear_consumed_results"
    assert "[consumed]" in content_list(result.messages)
    consumed = next(message for message in result.messages if message.content == "[consumed]")
    assert consumed.metadata["consumed"] is True
    assert consumed.metadata["context_section"] == "active"


def test_over_budget_context_trims_history_after_clearing_consumed_results() -> None:
    provider = PrefixStableContext(max_tokens=70, keep_recent_turns=1)
    history = [
        Message(role="user", content="task"),
        Message(role="assistant", content="first"),
        Message(role="user", content="old question " + ("a" * 50)),
        Message(role="assistant", content="old answer " + ("b" * 50)),
        Message(role="tool", content="c" * 120),
        Message(role="assistant", content="tool consumed"),
        Message(role="user", content="another old question " + ("d" * 50)),
        Message(role="assistant", content="another old answer " + ("e" * 50)),
    ]
    new_messages = [Message(role="user", content="latest question")]

    result = build(provider, "system", history, new_messages)

    assert result.consumed_count == 1
    assert result.trimmed_count > 0
    assert result.compaction_applied == "clear_consumed_results+truncate"
    contents = content_list(result.messages)
    assert contents[0:3] == ["system", "task", "first"]
    assert "latest question" in contents
    assert "old question " + ("a" * 50) not in contents
    assert result.messages[0].metadata["context_section"] == "system"
    assert result.messages[1].metadata["context_section"] == "task"
    assert result.messages[2].metadata["context_section"] == "task"
    assert result.tokens_used <= 70


def test_system_and_task_sections_are_kept_even_when_budget_is_too_small() -> None:
    provider = PrefixStableContext(max_tokens=10, keep_recent_turns=0)
    history = [
        Message(role="user", content="important task " + ("x" * 20)),
        Message(role="assistant", content="first response " + ("y" * 20)),
        Message(role="user", content="old history " + ("z" * 40)),
    ]

    result = build(provider, "system prompt " + ("s" * 20), history, [])

    contents = content_list(result.messages)
    assert contents[:3] == [
        "system prompt " + ("s" * 20),
        "important task " + ("x" * 20),
        "first response " + ("y" * 20),
    ]
    assert "old history " + ("z" * 40) not in contents
    assert result.trimmed_count == 1


def test_token_counter_can_be_replaced() -> None:
    calls: list[str] = []

    def counter(message: Message) -> int:
        calls.append(str(message.content))
        return int(message.metadata.get("tokens", 1))

    provider = PrefixStableContext(max_tokens=10, token_counter=counter)
    history = [
        Message(role="user", content="task", metadata={"tokens": 2}),
        Message(role="assistant", content="first", metadata={"tokens": 3}),
    ]
    new_messages = [Message(role="user", content="latest", metadata={"tokens": 4})]

    result = build(provider, "system", history, new_messages)

    assert result.tokens_used == 10
    assert calls


def test_context_config_override_is_used_for_one_build() -> None:
    provider = PrefixStableContext(max_tokens=1_000)
    config = ContextConfig(max_tokens=20, keep_recent_turns=0)
    history = [
        Message(role="user", content="task"),
        Message(role="assistant", content="first"),
        Message(role="user", content="old " + ("x" * 100)),
    ]

    result = build(provider, "system", history, [], config)

    assert "old " + ("x" * 100) not in content_list(result.messages)
    assert result.trimmed_count == 1


def test_default_token_counter_returns_positive_estimate() -> None:
    assert default_token_counter(Message(role="user", content="abcd")) >= 1
