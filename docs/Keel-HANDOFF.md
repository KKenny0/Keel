# 🥷 Keel HANDOFF：Python 服务里的持久化 Agent 运行器

## Summary

新项目名：**Keel**  
文件夹名：`keel`  
Python 包名：`keel_runtime`

Keel 的定位是：**让 Python 后端把 Agent 任务交给 pi 运行，并负责隔离、持久化、恢复和交付结果。**

它不是新的 Agent 框架，不负责教用户怎么编排 Agent；它只解决生产系统里最难收拾的部分：任务在哪里跑、现场怎么保存、失败怎么恢复、结果怎么拿回来。

当前已完成到 Phase 4.1：**FastAPI 服务提交一个 Agent Job，本地跑通 pi RPC，支持流式输出、停止、会话保存、工作区保存、重启后查询历史任务，并支持 Docker、Kubernetes Pod + PVC、超时、资源限制、任务清理、多任务依赖、前置产物传递和结构化模型 API 配置。**

## Product Rules

必须坚持：

- 只做 Agent 运行底座，不做泛 Agent 框架。
- 复用 pi，不重写 Agent 内核。
- 第一版只做单 Agent。
- 第一版不接 LangGraph、OpenAI Agents SDK、CrewAI、AutoGen。
- 每个任务必须有独立工作区。
- 会话、工作区、产物、任务状态要分开保存。
- 所有功能都用一句话筛选：**它是否让 Agent 更容易启动、隔离、恢复、保存结果？**

不做：

- 不做 RAG 平台。
- 不做流程图编辑器。
- 不做多 Agent 管理台。
- 不发明新的技能格式。
- 不绑定单一云厂商。

## Recommended Bootstrap

项目采用：

- Python 3.11+
- `pyproject.toml`
- `src/keel_runtime`
- `pytest`
- `ruff`
- `FastAPI` 示例
- 本地文件系统作为默认持久化
- pi RPC 作为第一版运行核心

建议目录：

```text
keel/
  pyproject.toml
  README.md
  src/keel_runtime/
    __init__.py
    specs.py
    jobs.py
    runtime.py
    stores.py
    events.py
    errors.py
  examples/
    fastapi_app/
  tests/
```

## Core Interfaces

第一版公开接口只保留这些概念：

- `AgentSpec`：Agent 定义，包含名称、系统提示词、技能路径、工具配置、模型配置。
- `AgentJob`：一次任务，包含任务 ID、输入、状态、会话路径、工作区路径、产物路径。
- `JobManager`：创建、查询、停止、恢复任务。
- `AgentRuntime`：负责启动 pi RPC、发送输入、接收输出、停止运行。
- `SessionStore`：保存和读取会话历史。
- `WorkspaceStore`：创建、保存、恢复工作区。
- `ArtifactStore`：保存和列出产物。

最小 API：

```python
create_job(spec, input, workspace=None) -> job_id
create_task(spec, input, dependencies=None, artifact_inputs=None) -> job_id
stream(job_id) -> event stream
stop(job_id) -> status
resume(job_id) -> event stream
get_status(job_id) -> status
describe_job(job_id) -> dict
list_artifacts(job_id) -> list
download_artifact(job_id, path) -> bytes
```

任务状态固定为：

```text
created
running
stopping
stopped
succeeded
failed
restorable
```

## Phase 1：Local MVP

目标：本地 Python 服务能跑一个 Agent 任务。

实现内容：

- 启动 pi RPC 本地进程。
- 创建独立任务目录。
- 保存任务状态。
- 保存会话历史。
- 流式返回 Agent 输出。
- 支持停止任务。
- 提供 FastAPI 示例。

验收标准：

- FastAPI 能创建任务并返回 `job_id`。
- 能实时看到 Agent 输出。
- 任务结束后能看到结果和产物。
- 中途停止不会破坏会话和工作区。
- Python 服务重启后还能查到历史任务。
- 两个任务不能互相污染工作区。

## Phase 2：Persistence

目标：任务现场可恢复。

实现内容：

- 本地目录持久化。
- S3/MinIO 存储实现。
- 会话、工作区、产物、任务状态分开保存。
- 支持恢复指定任务。
- 支持导出完整任务记录。

验收标准：

- 服务被杀掉后重启，历史任务不丢。
- 工作区能恢复。
- 会话能继续。
- 对象存储失败时，任务进入明确失败状态。
- 用户能下载任务产物和运行记录。

## Phase 3：Production Runtime

目标：支持普通服务器和 Kubernetes。

实现内容：

- 本地进程运行。
- Docker 运行。
- Kubernetes Pod + PVC 运行。
- 支持超时、资源限制、环境变量注入。
- 支持日志、错误原因、退出状态。
- 支持任务清理。

验收标准：

- 同一个任务能在本地、Docker、Kubernetes 跑通。
- 每个任务工作区隔离。
- Agent 崩溃后能看到明确失败原因。
- 超时任务能自动停止。
- PVC 里能看到会话和工作产物。
- 密钥不会出现在日志、会话、产物里。

## Phase 4：Job API And Light Orchestration

目标：用户能管理多个任务，但不做通用编排平台。

实现内容：

- 多任务创建、查询、停止、恢复。
- 任务依赖关系。
- 一个任务读取另一个任务产物。
- 统一查询状态、日志、产物、失败原因。
- 用户在自己的 Python 服务中写流程。

验收标准：

- 用户能提交多个任务。
- B 任务能读取 A 任务产物。
- A 失败时，B 不会误跑。
- 每个任务的输入、输出、状态可追踪。
- 不依赖外部 Agent 框架也能跑多步骤流程。

## Phase 5：Multi-Agent Collaboration

目标：多个 Agent 围绕一个目标协作。

实现内容：

- 多个 Agent 使用同一个项目工作区。
- 支持串行、并行、人工确认后继续。
- 支持任务之间传递上下文和产物。
- 支持失败重试。
- 支持恢复到中间状态继续。

验收标准：

- 多个 Agent 能处理同一个目标。
- 每个 Agent 做了什么可查。
- 一个 Agent 失败不污染其他任务。
- 可以从中间步骤恢复。
- 跑通真实流程：分析代码库 -> 修改文件 -> 生成报告 -> 人工确认。

## Test Plan

每个阶段都必须有这些测试：

- 正常任务：创建、运行、流式输出、完成、查看产物。
- 停止任务：中途停止后状态正确，现场不损坏。
- 恢复任务：服务重启后能恢复会话和工作区。
- 失败任务：pi 崩溃、模型失败、存储失败时状态明确。
- 隔离测试：两个任务不能互相污染。
- 存储测试：本地、S3/MinIO、PVC 分别跑通。
- 安全测试：密钥不出现在日志、会话、产物里。
- 示例测试：FastAPI、本地运行、Kubernetes 示例按文档能跑通。

## First Milestone Definition

第一个可交付版本只包含：

- `keel_runtime` Python 包。
- 本地 pi RPC 运行。
- 本地持久化。
- 单 Agent 任务。
- FastAPI 示例。
- 流式输出。
- 停止任务。
- 服务重启后查询历史任务。
- 基础测试。

第一版完成后，就可以拿一个真实 Python 后端项目试用。

## Assumptions

- pi RPC 足够作为第一版底层运行入口。
- Keel 不负责替代 pi，只负责 Python 生产接入。
- 第一版以本地运行为主，Kubernetes 从 Phase 3 进入主线。
- 对象存储优先支持 S3/MinIO。
- 产品名采用 **Keel**，除非后续明确改名。
