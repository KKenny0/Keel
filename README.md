# Keel

Keel 是给 Python 后端使用的持久化 Agent 运行器。它让后端服务把一次 Agent 任务交给 pi 运行，并负责工作区隔离、会话保存、任务状态、流式输出、停止任务和结果交付。

Keel 不是新的 Agent 框架，也不负责定义复杂编排。它的边界是：让 Agent 在生产服务里更容易启动、隔离、恢复和保存结果。

## 当前阶段

Phase 1 已完成：本地单 Agent MVP。

- 通过 pi RPC 在本机启动一个 Agent 任务。
- 每个任务创建独立工作区。
- 保存任务状态和会话历史。
- 支持流式读取 Agent 输出。
- 支持停止任务，并保留会话和工作区。
- 提供 FastAPI 示例，服务重启后仍可查询历史任务。

Phase 2 已完成：持久化和恢复。

- 支持失败或中断后的指定任务恢复。
- 支持导出一次任务的完整记录。
- 支持把任务状态、会话、工作区、产物同步到 S3/MinIO 这类对象存储。
- 对象存储不可用时，任务会进入明确失败状态。

Phase 3 已完成：生产运行边界。

- 支持本地进程、Docker 和 Kubernetes Pod + PVC 三种运行方式。
- 支持超时自动停止、资源限制、环境变量注入和失败原因记录。
- 支持任务结束后清理工作区或产物。
- 任务记录、日志和产物会遮盖常见密钥值。

Phase 4 已完成：任务接口和轻量编排入口。

- 支持一个服务里创建和管理多个任务。
- 支持任务依赖，前置任务失败时后续任务不会启动。
- 支持把前置任务产物复制到当前任务工作区。
- 支持统一查询任务状态、日志、产物、失败原因和依赖状态。

当前暂不包含多 Agent、复杂工作流引擎、RAG 平台或可视化管理台。

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
      security.py
      cleanup.py
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

如果要接入 S3/MinIO：

```powershell
python -m pip install -e ".[s3]"
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
- 恢复任务。
- 查看和导出完整运行记录。
- 创建带依赖关系的任务。
- 查询任务摘要，包括状态、日志、产物、失败原因和依赖状态。

如果需要让示例服务同步到 S3/MinIO，可以设置这些环境变量：

```powershell
$env:KEEL_S3_BUCKET = "your-bucket"
$env:KEEL_S3_PREFIX = "keel"
$env:KEEL_S3_ENDPOINT_URL = "http://127.0.0.1:9000"
```

如果需要切换运行方式：

```powershell
$env:KEEL_RUNTIME = "docker"
$env:KEEL_DOCKER_IMAGE = "your-agent-image"
```

```powershell
$env:KEEL_RUNTIME = "kubernetes"
$env:KEEL_K8S_IMAGE = "your-agent-image"
$env:KEEL_K8S_PVC = "keel-pvc"
$env:KEEL_K8S_NAMESPACE = "agents"
```

如果需要任务结束后自动清理工作区：

```powershell
$env:KEEL_CLEAN_WORKSPACE_ON_SUCCESS = "true"
```

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

## 轻量任务依赖

```python
from keel_runtime import AgentSpec, ArtifactInput, JobManager

manager = JobManager(root=".keel")

first_id = manager.create_task(
    spec=AgentSpec(name="first"),
    input={"task": "write-source"},
)

second_id = manager.create_task(
    spec=AgentSpec(name="second"),
    input={"task": "read-source"},
    dependencies=[first_id],
    artifact_inputs=[
        ArtifactInput(
            source_job_id=first_id,
            source_path="result.txt",
            target_path="inputs/source.txt",
        )
    ],
)
```

## 当前限制

- Phase 4 已落地任务接口和轻量编排入口。
- 第一版只面向本地单 Agent 任务。
- 底层 Agent 能力复用 pi，不重写 Agent 内核。
- 默认运行命令是 `pi rpc run`，本地没有 pi 时可以传入自定义命令做测试或适配。
- 默认持久化目标是本地文件系统。
- LangGraph、OpenAI Agents SDK、CrewAI、AutoGen 不作为核心依赖。
- 复杂工作流引擎和多 Agent 协作属于后续阶段。
