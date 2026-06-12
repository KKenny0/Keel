<p align="center">
  <img src="design/keel-logo-wordmark.svg" alt="Keel" width="260">
</p>

<p align="center">
  嵌入式 Agent 运行时工具包<br>
  Job 生命周期、上下文管理、工具协议、结构化输出、持久化恢复,开箱即用。
</p>

---

## Keel 是什么

Keel 是一个 Python 工具包。它嵌入你的项目,帮你快速获得 Agent 系统的通用运行能力:agent loop、context 管理、tool 执行、结构化输出、job 持久化和恢复。你只写领域差异部分(system prompt、output type、业务工具),Keel 提供所有通用机制。

```text
pip install keel-runtime
```

接入模式不是"部署 Keel 服务 再注册 agent",而是"加几行代码,你的代码获得生产级能力"。

## 设计原则

- **工具包,不是外壳。** pip install,加几行代码,你的代码获得能力。
- **通用机制,不提供领域模式。** agent loop、context、tool protocol 是通用的;agent 怎么定义、pipeline 怎么串,你决定。
- **Provider 协议,不锁实现。** ContextProvider、LLM client 都可以替换成你的实现。
- **架构对齐 OpenAI Agents SDK 模式,模型不锁 OpenAI。** LLM 调用层 provider-agnostic,加 adapter 即可接入 Anthropic、Google 等。
- **零外部依赖。** 核心包不依赖任何第三方库。

## 安装

Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,fastapi]"
```

S3/MinIO 持久化:

```bash
pip install -e ".[s3]"
```

## Quick Start

5 分钟路径:定义工具,把函数包成 `@keel.agent`,然后直接调用这个函数。
下面用 mock client 演示,不依赖真实 LLM。完整可运行版本在 `examples/quickstart_agent.py`。

```python
import asyncio
from typing import Any

import keel_runtime as keel


@keel.tool(name="get_weather", description="Get current weather for a city")
def get_weather(city: str) -> str:
    return f"{city}: 22C, sunny"


class MockClient:
    async def chat(self, messages, tools) -> dict[str, Any] | str:
        tool_names = {message.name for message in messages if message.role == "tool"}
        if "memory_record" not in tool_names:
            return {
                "tool_calls": [
                    {
                        "name": "memory_record",
                        "arguments": {
                            "title": "Weather quickstart",
                            "outcome": "Use get_weather for forecast questions",
                            "tags": ["quickstart"],
                        },
                    }
                ]
            }
        if "get_weather" not in tool_names:
            return {
                "tool_calls": [
                    {"name": "get_weather", "arguments": {"city": "Tokyo"}}
                ]
            }

        weather = next(
            message.content["output"]
            for message in messages
            if message.role == "tool" and message.name == "get_weather"
        )
        return f"Weather report: {weather}"


memory = keel.LocalMemoryProvider()


@keel.agent(
    client=MockClient(),
    tools=[get_weather],
    memory=memory,
    memory_scope="quickstart",
    system_prompt="You are a concise weather assistant.",
    max_iterations=5,
)
async def weather_agent(question: str) -> str:
    return question


async def main():
    result = await weather_agent("What is the weather in Tokyo?")
    print(result.status)    # succeeded
    print(result.output)    # Weather report: Tokyo: 22C, sunny
    print(memory.list_decisions(scope="quickstart")[0].title)


asyncio.run(main())
```

也可以直接运行仓库里的示例:

```bash
python examples/quickstart_agent.py
```

当你需要更细控制每轮迭代、历史或 job id 时,使用下面的 `AgentLoop` 底层 API。

## Building Blocks

| 模块 | 职责 |
| --- | --- |
| `AgentLoop` | LLM 调用,tool 执行,迭代控制,usage 上报 |
| `PrefixStableContext` | token 预算,前缀稳定分区,已消费 tool result 清理,历史裁剪 |
| `ToolRegistry` / `@tool` | 装饰器定义工具,自动生成 schema,注册和执行 |
| `parse_output` / `extract_json` | 从 LLM 文本中提取 JSON,Pydantic 校验,文本 fallback |
| `InProcessRuntime` | 把 Python async callable 包装成 Keel job |
| `JobManager` | 创建、运行、停止、恢复、查询 job 和 collaboration |
| `ModelConfig` | 结构化模型配置,声明式 fallback,provider 校验 |
| `AgentSpec` / `AgentJob` | Agent 定义,job 状态,依赖,资源限制 |
| `stores` | 本地文件系统 + S3/MinIO 持久化 |
| `events` | 流式事件系统 |
| `collaboration` | 多 Agent 协作,串行/并行,人工确认,重试 |
| `@agent` / `Agent` | 极简装饰器入口,串起 client、context、tools、memory 和 gate |
| `PromptComposer` | 技能注入协议 |
| `HumanGate` | 独立确认原语 |
| `MemoryProvider` / `LocalMemoryProvider` | 记忆存取协议和本地 JSONL 实现 |

## Agent Loop

`AgentLoop` 把 LLM 调用、ContextProvider 组装、工具执行、结构化输出解析串成完整循环。

LLM client 只需要满足最小签名 `async def chat(messages, tools) -> response`:

```python
class MyClient:
    async def chat(self, messages, tools):
        response = await openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[m.to_dict() for m in messages],
            tools=[{"type": "function", "function": t} for t in tools],
        )
        return response.choices[0].message
```

配置项:

```python
config = AgentLoopConfig(
    system_prompt="You are a research assistant.",
    max_iterations=10,
    parse_final_output=True,       # 自动解析最终输出为 JSON
    output_model=MyPydanticModel,  # 可选:用 Pydantic 校验
    fail_on_tool_error=False,      # tool 失败时是否终止 loop
    job_id="my-agent-job",
)
```

`AgentLoopResult` 包含:

- `status`: `succeeded` / `failed` / `max_iterations`
- `output`: 解析后的输出(如果 `parse_final_output=True`)
- `raw_output`: LLM 原始文本
- `iterations`: 实际迭代次数
- `messages`: 完整消息历史
- `tool_results`: 所有工具执行结果
- `events`: 所有 Keel 事件

## Context 管理

`PrefixStableContext` 在每次 LLM 调用前组装 messages,防止无限追加历史:

```python
from keel_runtime import PrefixStableContext

context = PrefixStableContext(
    max_tokens=64_000,          # token 预算
    keep_recent_turns=10,       # 保留最近 N 轮
    clear_consumed_results=True,  # 先清理已消费 tool result
    cache_control=True,         # 输出 cache breakpoint 元数据
)
```

四分区策略:

1. **SYSTEM**: system prompt,永不裁剪
2. **TASK**: 初始任务意图(第一个 user + 第一个 assistant),永不裁剪
3. **HISTORY**: 中间历史,token 压力下优先裁剪
4. **ACTIVE**: 最近活跃轮次,尽量保留

裁剪顺序:先清理已消费 tool result,再从 HISTORY 区头部裁剪。SYSTEM 和 TASK 区始终稳定。

你可以替换整个 ContextProvider 实现:

```python
from keel_runtime import ContextProvider, ContextResult

class MyContextProvider:
    async def build_messages(self, system_prompt, history, new_messages, config=None):
        # 你的组装逻辑
        return ContextResult(messages=..., tokens_used=..., cache_breakpoints=[])
```

## Tool 协议

用 `@tool` 装饰器定义工具,自动生成参数 schema:

```python
from keel_runtime import tool, ToolRegistry

@tool(name="search_web", description="Search the web for information")
async def search_web(query: str, max_results: int = 5) -> list[dict]:
    # 你的搜索逻辑
    return [{"title": "...", "url": "..."}]

@tool(name="read_file", description="Read a file from workspace")
def read_file(path: str) -> str:
    return open(path).read()

# 注册
registry = ToolRegistry([search_web, read_file])

# 查看生成的 schema
print(registry.to_list())
# [{"name": "search_web", "description": "...", "parameters": {...}}, ...]

# 执行
from keel_runtime import ToolCall
result = await registry.execute(ToolCall(name="search_web", arguments={"query": "keel agent"}))
print(result.ok, result.output)
```

## 结构化输出

从 LLM 文本响应中提取结构化数据:

```python
from keel_runtime import parse_output, extract_json

# 自动提取 JSON(处理 markdown code block)
text = 'Here is the result:\n```json\n{"score": 8.5, "summary": "Good"}\n```'
data = parse_output(text)
# {"score": 8.5, "summary": "Good"}

# 用 Pydantic 校验
from pydantic import BaseModel

class Review(BaseModel):
    score: float
    summary: str

review = parse_output(text, model=Review)
# Review(score=8.5, summary='Good')

# 只提取 JSON,不校验
raw = extract_json(text)
```

## InProcessRuntime

把 Python async callable 包装成 Keel job,不需要起子进程:

```python
from keel_runtime import JobManager, InProcessRuntime, AgentSpec

manager = JobManager(runtime=InProcessRuntime(), root=".keel")

async def my_handler(payload):
    return {"result": payload["task"] + " done"}

spec = AgentSpec(name="worker", command=my_handler)
job_id = manager.create_job(spec, input={"task": "process data"})

async for event in manager.stream(job_id):
    print(event.message)
```

## Job 生命周期

```python
from keel_runtime import AgentSpec, JobManager

manager = JobManager(root=".keel")
spec = AgentSpec(
    name="writer",
    system_prompt="You write concise summaries.",
)

# 创建并运行
job_id = manager.create_job(spec, input="Summarize this repo.")

# 流式输出
async for event in manager.stream(job_id):
    print(event.message)

# 查询状态
status = manager.get_status(job_id)

# 查看产物
artifacts = manager.list_artifacts(job_id)

# 停止
manager.stop(job_id)

# 恢复中断的任务
manager.restore_job(job_id)

# 导出完整记录
manager.export_job(job_id)
```

## 模型配置

```python
from keel_runtime import AgentSpec, ModelConfig

spec = AgentSpec(
    name="writer",
    model=ModelConfig(
        provider="openai",
        model="gpt-4.1",
        api_key_ref="OPENAI_API_KEY",
        fallback=ModelConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_ref="ANTHROPIC_API_KEY",
        ),
    ),
    secret_env={"OPENAI_API_KEY": "sk-..."},
)
```

## 任务依赖

```python
from keel_runtime import AgentSpec, ArtifactInput, JobManager

manager = JobManager(root=".keel")

first = manager.create_task(
    spec=AgentSpec(name="researcher"),
    input={"task": "analyze"},
)

second = manager.create_task(
    spec=AgentSpec(name="writer"),
    input={"task": "report"},
    dependencies=[first],
    artifact_inputs=[
        ArtifactInput(
            source_job_id=first,
            source_path="result.txt",
            target_path="inputs/research.txt",
        )
    ],
)
```

## 多 Agent 协作

```python
from keel_runtime import AgentSpec, ArtifactInput, JobManager

manager = JobManager(root=".keel")
collab_id = manager.create_collaboration(
    goal="Review and improve code",
    workspace=".",
)

step1 = manager.add_collaboration_step(
    collab_id, AgentSpec(name="analyst"), {"task": "analyze"}
)
job1 = manager.get_collaboration_step(collab_id, step1).job_id

step2 = manager.add_collaboration_step(
    collab_id,
    AgentSpec(name="editor"),
    {"task": "apply fixes"},
    dependencies=[job1],
    artifact_inputs=[
        ArtifactInput(source_job_id=job1, source_path="result.txt", target_path="input.txt")
    ],
)

# 人工确认
step3 = manager.add_collaboration_step(
    collab_id,
    AgentSpec(name="reviewer"),
    {"task": "final review"},
    requires_confirmation=True,
)
manager.confirm_collaboration_step(collab_id, step3, note="approved")
```

## 开发和测试

```bash
pytest
ruff check .
```

## License

MIT
