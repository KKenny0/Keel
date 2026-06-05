# Keel Model API 配置层实施计划

## Summary

为 Keel Runtime SDK 增加结构化的模型 API 配置能力。SDK 不实现 LLM 调用本身，而是提供模型配置、密钥引用校验、声明式 fallback 和任务级用量记录，让 Python 服务可以稳定管理 OpenAI、Anthropic、Google、Azure OpenAI 和自定义模型提供商。

核心定位：

> SDK 管配置、管审计、管用量记录；实际 API 调用和 fallback 执行仍由 agent 进程负责。

本计划作为 **Phase 4.1：模型 API 配置层** 单独实施。它补强 Phase 4 的任务接口，但不改变 Phase 4 已完成的任务依赖和产物传递能力。

## Implementation Status

状态：已完成（2026-06-05）

已完成结构化 `ModelConfig`、`ModelUsage`、`ProviderRegistry`、声明式 fallback、模型配置 warning、显式 usage 解析、任务级用量记录、Docker/Kubernetes 密钥引用传递、README 示例和测试覆盖。

## Current State

`AgentSpec.model` 当前是无类型的 `dict[str, Any]`：

```python
model: dict[str, Any] = field(default_factory=dict)
```

当前行为：

- SDK 不校验 `model` 内容。
- SDK 不解析 provider、model、endpoint 或 API key 引用。
- `model` 原样进入 `spec.to_dict()`，再通过 `_paths_payload()` 传给 agent 进程。
- 实际模型调用发生在 agent 进程内部，SDK 只读取 stdout/stderr。
- 密钥通过 `env` / `secret_env` 注入，已有遮盖逻辑会避免密钥落入日志和任务记录。

这个边界必须保持：Keel 不成为 LLM 网关，不直接调用任何模型 API。

## Problem Statement

当前设计在生产系统中有四个问题：

1. **模型配置没有标准形状。** 不同用户可能用不同字段表达 provider、model、endpoint 和 API key。
2. **fallback 没有统一声明。** 主模型不可用时，备用模型配置散落在各自 agent 配置里。
3. **任务级用量不可记录。** SDK 目前没有结构化字段记录 provider、model、token 和成本信息。
4. **密钥引用关系不清楚。** `api_key_ref` 这类引用和 `secret_env`、Docker env、Kubernetes Secret 的关系没有标准规则。

## Design Principles

1. **只管配置，不管调用。** SDK 不发 HTTP 请求，不替代 litellm、instructor 或 provider SDK。
2. **旧格式绝对不变。** 现有 `model: dict[str, Any]` 原样透传，不自动包装、不改字段、不改变 payload 结构。
3. **新格式显式启用。** 只有用户传入 `ModelConfig` 时，SDK 才输出结构化模型配置。
4. **校验只告警，不阻断。** 未知 provider/model、缺失密钥引用等问题写入 session warning，但任务仍可运行。
5. **用量只从显式约定读取。** 不猜普通 stdout 文本。只识别约定前缀或约定文件。
6. **fallback 是声明，不是重试。** SDK 只把 fallback 结构传给 agent 进程，不启动多个 agent 进程。

## Target State

### 新增 `models.py`

新增 `src/keel_runtime/models.py`，包含这些公开类型：

- `ModelProvider`
- `ModelConfig`
- `ModelUsage`
- `ProviderRegistry`
- `parse_model_usage`

### `ModelProvider`

内置 provider：

- `openai`
- `anthropic`
- `google`
- `azure_openai`
- `custom`

`custom` 用于自定义 provider。未知 provider 不报错，只产生 warning。

### `ModelConfig`

字段：

- `provider: ModelProvider | str`
- `model: str`
- `api_key_ref: str | None`
- `endpoint: str | None`
- `params: dict[str, Any]`
- `fallback: ModelConfig | None`

规则：

- `api_key_ref` 只保存环境变量名或密钥引用名，不保存真实密钥。
- `params` 透传给 agent 进程，SDK 不建模 provider 私有参数。
- `fallback` 支持链式结构，但不允许出现循环。
- `to_dict()` 输出结构化配置。
- `from_dict()` 只用于显式结构化配置，不用于包装旧 dict。

### `ModelUsage`

字段：

- `provider: str`
- `model: str`
- `input_tokens: int`
- `output_tokens: int`
- `total_tokens: int`
- `cost_usd: float | None`

记录位置：

- `AgentJob.model_usage`
- `job.json`
- `describe_job()`
- `snapshot_job()`
- `export_job()` 生成的记录

### `ProviderRegistry`

职责：

- 校验 provider 字段是否为空。
- 校验 model 字段是否为空。
- 校验 fallback 链是否循环。
- 校验 `api_key_ref` 是否能在 `AgentSpec.secret_env` 或当前运行环境中找到。
- 对内置 provider 做轻量识别。
- 对未知 provider/model 产生 warning，不阻断任务。

不做：

- 不维护强制性的完整模型列表。
- 不因为模型列表过时而拒绝任务。
- 不联网查询 provider 最新模型。

## AgentSpec 变更

`AgentSpec.model` 类型扩展为：

```python
model: ModelConfig | dict[str, Any] = field(default_factory=dict)
```

兼容规则：

- 如果用户传入旧的 `dict[str, Any]`，SDK 原样保存、原样序列化、原样传给 agent。
- 如果用户传入 `ModelConfig`，SDK 调用 `ModelConfig.to_dict()` 序列化。
- `AgentSpec.from_dict()` 读取历史任务时，默认把 `model` 作为 dict 保留。
- 如需从 dict 显式构造 `ModelConfig`，用户调用 `ModelConfig.from_dict()`。

这条规则优先于原计划中的“dict 自动包装为 ModelConfig”。自动包装取消，因为它会改变老 agent 看到的 payload。

## Runtime 集成

不修改 `runtime.py` 的核心运行路径。

理由：

- `_paths_payload()` 已经通过 `spec.to_dict()` 发送完整 agent 配置。
- 本地、Docker、Kubernetes runtime 都只负责启动 agent 进程。
- 模型配置层属于 `AgentSpec` 和 `JobManager` 的职责。
- Docker/Kubernetes runtime 只补充 `api_key_ref` 对应的环境变量名或 Secret 引用传递，不改变任务启动和日志流逻辑。

### Payload 结构

旧格式保持不变：

```json
{
  "temperature": 0.7,
  "model": "legacy-model-name"
}
```

新格式只在显式使用 `ModelConfig` 时出现：

```json
{
  "provider": "openai",
  "model": "gpt-4.1",
  "api_key_ref": "OPENAI_API_KEY",
  "endpoint": null,
  "params": {
    "temperature": 0.7,
    "max_tokens": 4096
  },
  "fallback": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-20250514",
    "api_key_ref": "ANTHROPIC_API_KEY",
    "endpoint": null,
    "params": {},
    "fallback": null
  }
}
```

## 密钥管理

`api_key_ref` 和现有密钥注入机制的关系：

- `api_key_ref` 是引用名，不是密钥值。
- 本地进程运行时，引用名可以来自 `AgentSpec.secret_env` 或宿主环境变量。
- Docker 运行时，`api_key_ref` 对应的环境变量名必须被传入容器，但命令行参数中不能出现密钥值。
- Kubernetes 运行时，`api_key_ref` 应映射到 Secret 引用，不写明文值进 Pod manifest。
- 如果 `api_key_ref` 找不到对应来源，`ProviderRegistry.validate()` 产生 warning，不阻断任务。

安全验收：

- `job.json` 不出现真实 API key。
- `events.jsonl` 不出现真实 API key。
- `record.json` 和导出包不出现真实 API key。
- Docker 命令参数不出现真实 API key。
- Kubernetes manifest 不出现真实 API key。

## 用量追踪

### 明确上报格式

SDK 只识别两种显式格式。

格式一：agent 输出单行前缀：

```text
KEEL_MODEL_USAGE_JSON:{"provider":"openai","model":"gpt-4.1","input_tokens":100,"output_tokens":50,"total_tokens":150,"cost_usd":0.01}
```

格式二：agent 写入产物文件：

```text
artifacts/model-usage.json
```

文件内容为 `ModelUsage.to_dict()` 的 JSON。

### 解析规则

`parse_model_usage()`：

- 先读取 `artifacts/model-usage.json`。
- 如果文件不存在，再扫描输出中以 `KEEL_MODEL_USAGE_JSON:` 开头的行。
- 只解析完整 JSON。
- 解析失败不影响任务状态。
- 解析失败时写入 warning 事件。
- 普通 stdout 文本不做猜测解析。

### 任务完成时回填

`JobManager._run()` 成功路径：

- 收集 output。
- 写入 `result.txt`。
- 调用 `parse_model_usage()`。
- 如果得到 usage，回填 `job.model_usage` 并保存。
- 记录一条 `model usage recorded` 事件。

失败任务默认不回填 usage，除非 `model-usage.json` 已经存在且可解析。

## Fallback 机制

Fallback 是声明式配置，不是 SDK 运行时重试。

SDK 做：

- 序列化 fallback 链。
- 校验 fallback 链没有循环。
- 记录 fallback 配置到任务记录中。
- 记录校验 warning。

SDK 不做：

- 不判断哪个 provider 失败。
- 不重启 agent 进程。
- 不执行 retry/backoff。
- 不修改 agent 的业务逻辑。

## Public API Changes

新增导出：

- `ModelProvider`
- `ModelConfig`
- `ModelUsage`
- `ProviderRegistry`

现有 API 保持可用：

- `AgentSpec(model=dict)` 继续可用且行为不变。
- `AgentSpec(model=ModelConfig(...))` 启用结构化配置。
- `JobManager.create_job()` 参数不变。
- `JobManager.describe_job()` 自动包含 `model_usage`，因为它来自 `job.to_dict()`。

## File Changes

| 文件 | 变更类型 | 说明 |
| --- | --- | --- |
| `src/keel_runtime/models.py` | 新增 | 模型配置、provider、usage、registry 和 usage 解析 |
| `src/keel_runtime/specs.py` | 修改 | `AgentSpec.model` 支持 `ModelConfig \| dict`，旧 dict 原样透传 |
| `src/keel_runtime/jobs.py` | 修改 | `AgentJob` 增加 `model_usage` |
| `src/keel_runtime/manager.py` | 修改 | 创建任务时记录模型配置 warning；任务结束后回填 usage |
| `src/keel_runtime/runtime.py` | 修改 | Docker/Kubernetes 运行时传递 `api_key_ref` 引用名，不传明文值 |
| `src/keel_runtime/__init__.py` | 修改 | 导出新增模型配置类型 |
| `tests/test_models.py` | 新增 | 模型配置、fallback、校验、usage 解析测试 |
| `tests/test_phase4_model_configuration.py` | 新增 | 与 JobManager/runtime 集成的测试 |

不修改：

- `runtime.py` 核心流程不改，只补密钥引用名传递。
- `security.py` 不改，除非测试发现密钥遮盖有遗漏。
- `stores.py` 不改，存储层继续依赖 `to_dict()` / `from_dict()`。

## Implementation Steps

### Step 1：新增模型配置类型

创建 `src/keel_runtime/models.py`：

- 实现 `ModelProvider`。
- 实现 `ModelConfig.to_dict()` / `from_dict()`。
- 实现 `ModelUsage.to_dict()` / `from_dict()`。
- 实现 `ProviderRegistry.validate(config, secret_env=None)`。
- 实现 fallback 循环检测。

### Step 2：保持旧 dict 原样透传

修改 `src/keel_runtime/specs.py`：

- `model` 类型改为 `ModelConfig | dict[str, Any]`。
- `__post_init__` 不把 dict 转成 `ModelConfig`。
- `to_dict()` 中：
  - 如果是 `ModelConfig`，输出 `model.to_dict()`。
  - 如果是 dict，输出原 dict。
- `from_dict()` 中默认把 `model` 保留为 dict。

### Step 3：增加任务级用量字段

修改 `src/keel_runtime/jobs.py`：

- `AgentJob` 增加 `model_usage: ModelUsage | None = None`。
- `to_dict()` 输出 `model_usage`。
- `from_dict()` 支持读取 `model_usage`。

### Step 4：集成校验 warning

修改 `src/keel_runtime/manager.py`：

- `JobManager.__init__()` 可接收可选 `provider_registry`，默认使用 `ProviderRegistry()`。
- `create_job()` 中：
  - 仅当 `spec.model` 是 `ModelConfig` 时运行校验。
  - 有 warning 时写入 `model config warnings` 事件。
  - warning 不阻断任务创建。

### Step 5：集成 usage 解析

修改 `src/keel_runtime/manager.py`：

- 在任务结束路径解析 usage。
- 优先读取 `artifacts/model-usage.json`。
- 再读取 output 中 `KEEL_MODEL_USAGE_JSON:` 行。
- 解析成功后保存到 `job.model_usage`。
- 解析失败写 warning，不改变任务状态。

### Step 6：导出和示例

- `__init__.py` 导出 `ModelProvider`、`ModelConfig`、`ModelUsage`、`ProviderRegistry`。
- README 增加一个结构化 `ModelConfig` 示例。
- FastAPI 示例不需要新增字段，因为 `spec.model` 已经来自请求体。

## Validation

### 单元测试

- `ModelConfig` 序列化和反序列化。
- fallback 链序列化和循环检测。
- `ProviderRegistry.validate()` 对缺失 provider/model、未知 provider/model、缺失 api_key_ref 的 warning。
- `AgentSpec(model=dict)` 输出完全保持原 dict。
- `AgentSpec(model=ModelConfig)` 输出结构化 dict。
- `ModelUsage` 序列化和反序列化。
- `parse_model_usage()` 解析前缀行。
- `parse_model_usage()` 解析 `model-usage.json`。
- `parse_model_usage()` 对坏 JSON 返回 warning，不抛出导致任务失败。

### 集成测试

- 旧 dict model 任务 payload 不变。
- `ModelConfig` 任务 payload 包含 provider/model/api_key_ref/fallback。
- 创建任务时模型配置 warning 写入 session。
- 任务完成后 `describe_job()` 返回 `model_usage`。
- `snapshot_job()` 和 `export_job()` 包含 `model_usage`。
- API key 明文不出现在 job/session/artifact/export/Kubernetes manifest/Docker args 中。

### 验证命令

```powershell
python -m pytest --basetemp $env:TEMP\keel_pytest_model_config
python -m ruff check .
python -m compileall -q src examples tests
```

在项目 `.venv` 中也要跑同样检查：

```powershell
.\.venv\Scripts\python.exe -m pytest --basetemp $env:TEMP\keel_pytest_model_config_venv
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q src examples tests
```

## Risks

### 1. Agent 进程不识别新结构

风险：显式 `ModelConfig` 会让 agent 看到新的结构化 payload。

缓解：旧 dict 完全不变。只有用户主动使用 `ModelConfig` 才启用新结构。

### 2. 用量记录为空

风险：agent 进程不输出 `KEEL_MODEL_USAGE_JSON:`，也不写 `model-usage.json`。

缓解：usage 是可选审计字段。缺失 usage 不影响任务成功。

### 3. provider/model 校验过时

风险：模型名称更新很快，硬编码列表会滞后。

缓解：校验只 warning，不阻断。默认不强制维护完整模型列表。

### 4. 密钥引用在 Docker/Kubernetes 下不可用

风险：本地环境有 key，但容器或 Pod 没有注入。

缓解：校验 warning 明确指出 `api_key_ref` 没有对应来源。Docker/Kubernetes 测试覆盖引用名传递，确保不泄露明文。

## Out of Scope

- SDK 直接调用 LLM API。
- SDK 实现 streaming response。
- SDK 实现 prompt template 管理。
- SDK 替代 litellm、instructor 或 provider SDK。
- SDK 执行 fallback retry/backoff。
- 模型负载均衡。
- 实时成本仪表板。
- 跨任务用量聚合 API。
- 联网拉取 provider 最新模型列表。

## Dependencies

- 无新外部依赖。
- 使用标准库 `dataclasses`、`enum`、`json`、`os`、`re`。

## Rollback

本阶段不做数据迁移。回滚方式：

- 移除 `models.py` 和相关导出。
- 移除 `AgentJob.model_usage` 字段读取和写入。
- 保留历史 `job.json` 中的 `model_usage` 字段不会破坏旧版本，因为旧版 `AgentJob.from_dict()` 会忽略未知字段之外的新增字段不可用；若需要完全回滚，先删除测试生成数据即可。

## Completion Criteria

本阶段完成的判断标准：

- 旧 `model` dict 的序列化结果逐字保持不变。
- 显式 `ModelConfig` 可以进入 payload。
- fallback 链可以序列化并检测循环。
- `api_key_ref` 不保存真实密钥。
- usage 可以通过约定前缀或约定文件进入 `AgentJob.model_usage`。
- 所有新增能力都有测试。
- README 和本计划文档同步更新。
- 全量测试、ruff、compileall 在当前环境和 `.venv` 中通过。
