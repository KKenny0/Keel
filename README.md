# Keel

Keel 是给 Python 后端使用的持久化 Agent 运行器。它让后端服务把一次 Agent 任务交给 pi 运行，并负责工作区隔离、会话保存、任务状态、流式输出、停止任务和结果交付。

Keel 不是新的 Agent 框架，也不负责定义复杂编排。它的边界是：让 Agent 在生产服务里更容易启动、隔离、恢复和保存结果。

## 当前阶段

Phase 1 目标是本地单 Agent MVP：

- 通过 pi RPC 在本机启动一个 Agent 任务。
- 每个任务创建独立工作区。
- 保存任务状态和会话历史。
- 支持流式读取 Agent 输出。
- 支持停止任务，并保留会话和工作区。
- 提供 FastAPI 示例，服务重启后仍可查询历史任务。

Phase 1 暂不包含多 Agent、任务编排、Kubernetes、S3/MinIO、RAG 平台或可视化管理台。

## 目录结构

当前目录结构如下：

```text
keel/
  README.md
  pyproject.toml
  src/
    keel_runtime/
      __init__.py
      specs.py
      jobs.py
      runtime.py
      stores.py
      events.py
      errors.py
  examples/
    fastapi_app/
      app.py
  tests/
  docs/
    Keel-HANDOFF.md
    Pi-Python-Agent-Runtime-SDK-IMPLEMENTATION-PLAN.md
```

## 安装

推荐使用 Python 3.11+：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev,fastapi]"
```

## 测试

```powershell
python -m pytest --basetemp .pytest_tmp
python -m ruff check .
```

## 运行 FastAPI 示例

```powershell
uvicorn examples.fastapi_app.app:app --reload
```

示例服务的预期能力：

- 提交任务并返回 `job_id`。
- 按 `job_id` 查询任务状态。
- 实时读取 Agent 输出。
- 停止正在运行的任务。
- 查看任务结果和产物。

## 最小 Python 用法

```python
import asyncio

from keel_runtime import AgentSpec, JobManager

spec = AgentSpec(
    name="writer",
    system_prompt="You write concise project summaries.",
)

manager = JobManager(root=".keel")
job_id = manager.create_job(
    spec=spec,
    input="Summarize this repository.",
)

async def main():
    async for event in manager.stream(job_id):
        print(event.message)

    status = manager.get_status(job_id)
    artifacts = manager.list_artifacts(job_id)
    print(status, artifacts)

asyncio.run(main())
```

## 当前限制

- Phase 1 已落地本地单 Agent MVP。
- 第一版只面向本地单 Agent 任务。
- 底层 Agent 能力复用 pi，不重写 Agent 内核。
- 默认运行命令是 `pi rpc run`，本地没有 pi 时可以传入自定义命令做测试或适配。
- 默认持久化目标是本地文件系统。
- LangGraph、OpenAI Agents SDK、CrewAI、AutoGen 不作为核心依赖。
- Docker、Kubernetes、S3/MinIO、多任务依赖和多 Agent 协作属于后续阶段。
