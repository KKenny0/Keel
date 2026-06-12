from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from keel_runtime import (
    AgentContext,
    AgentLoop,
    AgentLoopConfig,
    AgentSpec,
    ComposedPrompt,
    EventType,
    InProcessRuntime,
    JobManager,
    JobStatus,
    LocalMemoryProvider,
    Message,
    PrefixStableContext,
    ToolRegistry,
    tool,
)


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


def run(coro):
    return asyncio.run(coro)


class ScriptWeaverStageComposer:
    def compose(self, base_prompt: str, context: AgentContext) -> ComposedPrompt:
        stage = str(context.metadata["stage"])
        skill_names = [f"{stage}-refiner"]
        constraints = [
            f"activate only {stage} stage skills",
            "preserve existing screenplay domain decisions",
        ]
        return ComposedPrompt(
            content=(
                f"{base_prompt}\n"
                f"Skills:\n- {skill_names[0]}: refine the current stage\n"
                "Constraints:\n"
                + "\n".join(f"- {constraint}" for constraint in constraints)
            ),
            skill_names=skill_names,
            constraints=constraints,
            metadata={"stage": stage},
        )


class ScriptWeaverClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        tool_messages = {message.name for message in messages if message.role == "tool"}
        if "memory_record" not in tool_messages:
            system_prompt = messages[0].content
            assert "idea-refiner" in system_prompt
            assert "storyboard-refiner" not in system_prompt
            return {
                "tool_calls": [
                    {
                        "name": "memory_record",
                        "arguments": {
                            "title": "Idea stage framing",
                            "outcome": "Keep the protagonist conflict explicit",
                            "tags": ["idea", "script-weaver"],
                        },
                    },
                    {
                        "name": "write_artifact",
                        "arguments": {
                            "name": "outline.md",
                            "body": "# Refined idea\nConflict stays explicit.",
                        },
                    },
                ]
            }
        return {
            "content": json.dumps(
                {
                    "status": "refined",
                    "stage": "idea",
                    "kept_domain_logic": True,
                }
            )
        }


def test_script_weaver_fixture_wraps_existing_agent_without_rewriting_domain_logic(
    tmp_path: Path,
) -> None:
    client = ScriptWeaverClient()
    memory = LocalMemoryProvider()
    domain_calls: list[str] = []

    def existing_domain_agent_logic(draft: str) -> str:
        domain_calls.append(draft)
        return f"domain-refined:{draft}"

    async def script_weaver_handler(payload: dict[str, Any]) -> dict[str, Any]:
        artifact_dir = Path(payload["artifact_path"])
        stage = str(payload["input"]["stage"])
        domain_draft = existing_domain_agent_logic(str(payload["input"]["draft"]))

        @tool(name="write_artifact", description="Write a script artifact")
        def write_artifact(name: str, body: str) -> dict[str, str]:
            path = artifact_dir / name
            path.write_text(body, encoding="utf-8")
            return {"path": name, "body": body}

        loop = AgentLoop(
            client,
            PrefixStableContext(max_tokens=1_000),
            ToolRegistry([write_artifact]),
            AgentLoopConfig(
                system_prompt="script-weaver agent wrapper",
                prompt_composer=ScriptWeaverStageComposer(),
                memory_provider=memory,
                memory_scope="script-weaver",
                job_id=payload["job_id"],
            ),
        )
        result = await loop.run(
            Message(role="user", content={"stage": stage, "draft": domain_draft}),
            agent_context={"metadata": {"stage": stage}},
        )
        return {
            "loop_status": result.status,
            "output": result.output,
            "domain_draft": domain_draft,
            "tool_names": [tool_result.name for tool_result in result.tool_results],
            "memory_titles": [
                decision.title
                for decision in memory.list_decisions(scope="script-weaver")
            ],
            "composed_skills": result.composed_prompts[0].skill_names,
        }

    manager = JobManager(
        root=tmp_path / "data",
        runtime=InProcessRuntime({"script-weaver-agent": script_weaver_handler}),
    )
    job_id = manager.create_job(
        AgentSpec(name="script-weaver-agent"),
        {"stage": "idea", "draft": "A cartographer loses the only true map."},
    )

    events = collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    result = json.loads(manager.download_artifact(job_id, "result.txt").decode("utf-8"))
    outline = manager.download_artifact(job_id, "outline.md").decode("utf-8")
    assert job.status == JobStatus.SUCCEEDED
    assert result["loop_status"] == "succeeded"
    assert result["output"]["kept_domain_logic"] is True
    assert result["domain_draft"].startswith("domain-refined:")
    assert domain_calls == ["A cartographer loses the only true map."]
    assert result["tool_names"] == ["memory_record", "write_artifact"]
    assert result["memory_titles"] == ["Idea stage framing"]
    assert result["composed_skills"] == ["idea-refiner"]
    assert "Conflict stays explicit" in outline
    assert any(event.message == "in-process callable started" for event in events)
    assert any(event.type == EventType.OUTPUT for event in events)


class OmniSearchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        text_results = [
            message
            for message in messages
            if message.role == "tool" and message.name == "text_search"
        ]
        image_results = [
            message
            for message in messages
            if message.role == "tool" and message.name == "image_search"
        ]
        if not text_results and not image_results:
            return {
                "content": "plan: search text and image evidence",
                "tool_calls": [
                    {
                        "name": "text_search",
                        "arguments": {"query": "keel agent runtime"},
                    },
                    {
                        "name": "image_search",
                        "arguments": {"query": "agentic rag architecture diagram"},
                    },
                ],
            }
        if len(text_results) == 1:
            return {
                "content": "insufficient context: need sufficient-context evidence",
                "tool_calls": [
                    {
                        "name": "text_search",
                        "arguments": {"query": "sufficient context iterative retrieval"},
                    },
                    {
                        "name": "memory_record",
                        "arguments": {
                            "title": "OmniSearch retrieval checkpoint",
                            "outcome": "Text and image evidence were collected before synthesis",
                            "tags": ["open-omnisearch", "agentic-rag"],
                        },
                    },
                ],
            }
        return {
            "content": json.dumps(
                {
                    "answer": "Keel can assemble iterative multimodal retrieval.",
                    "sufficient_context": True,
                    "searched_again": True,
                }
            )
        }


def test_open_omnisearch_fixture_builds_agentic_rag_loop_with_refill_search() -> None:
    text_queries: list[str] = []
    image_queries: list[str] = []
    memory = LocalMemoryProvider()
    client = OmniSearchClient()

    @tool(name="text_search", description="Search text evidence")
    def text_search(query: str) -> dict[str, Any]:
        text_queries.append(query)
        return {
            "query": query,
            "results": [f"text evidence for {query}"],
        }

    @tool(name="image_search", description="Search image evidence")
    def image_search(query: str) -> dict[str, Any]:
        image_queries.append(query)
        return {
            "query": query,
            "results": [f"image evidence for {query}"],
        }

    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=72, keep_recent_turns=0),
        ToolRegistry([text_search, image_search]),
        AgentLoopConfig(
            system_prompt="open-omnisearch agentic rag",
            memory_provider=memory,
            memory_scope="open-omnisearch",
            max_iterations=5,
        ),
    )
    history = [
        Message(role="user", content="original multimodal research request"),
        Message(role="assistant", content="initial plan"),
        Message(role="user", content="stale retrieval trace " + ("x" * 400)),
    ]

    result = run(
        loop.run(
            "Answer with text and image evidence, then check if context is sufficient.",
            history=history,
        )
    )

    assert result.status == "succeeded"
    assert result.output == {
        "answer": "Keel can assemble iterative multimodal retrieval.",
        "sufficient_context": True,
        "searched_again": True,
    }
    assert text_queries == [
        "keel agent runtime",
        "sufficient context iterative retrieval",
    ]
    assert image_queries == ["agentic rag architecture diagram"]
    assert [tool_result.name for tool_result in result.tool_results] == [
        "text_search",
        "image_search",
        "text_search",
        "memory_record",
    ]
    assert memory.list_decisions(scope="open-omnisearch")[0].title == (
        "OmniSearch retrieval checkpoint"
    )
    assert len(client.calls) == 3
    assert result.context_results[0].trimmed_count == 1
    assert any(
        tool["name"] == "memory_record"
        for tool in client.calls[0]["tools"]
    )
