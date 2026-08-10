# REPLAY-022：拆分 Agent Runtime 与 LLM Runtime

日期：2026-08-10

依据：[`NEXT_STEPS-022.md`](NEXT_STEPS-022.md)、
[`review/design-compliance-review-022.md`](review/design-compliance-review-022.md) 和
[`review/code-review-022.md`](review/code-review-022.md)。

执行基线：`1c9c7e3`。本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：单用户 Local Demo；不增加鉴权、RBAC、多租户或分布式部署。

## 1. 结论

022 审查中的三个代码问题已关闭：

```text
Agent 任务
  → 独立 Agent Runtime

LLM 节点 / LLM Presentation
  → StructuredGenerationResolver
      ├─ LLM Runtime configured → Direct LLM
      └─ LLM Runtime unconfigured + Agent ready → Agent structured fallback
           requested_capabilities=() / allowed_tools=[] / max_steps=1
```

- Run 启动时冻结 Runtime、policy digest、runtime digest、identity 和 fallback reason。
- 401、429、timeout、malformed output 或配置漂移不会切换 Runtime。
- 成功及已发起后失败的调用都写单一 `ModelUsage` 费用事实。
- 技术错误信封幂等，响应按 digest 去重且整体小于 6000 bytes。
- 终态 Run 不创建 SSE；收到终态事件立即关闭；真实 Chromium 等待 5 秒请求数不增长。

`make demo-check` 在最终代码上连续两次通过，`make browser-ui-check` 连续两次通过。

外部真实 Provider 七条最低语义集仍未执行：当前没有 Agent/LLM endpoint、model 或 API key。
因此本报告确认代码修复和本地真实进程矩阵通过，但按 `NEXT_STEPS-022.md` 的完成定义，
**不宣称 022 外部真实语义验收或整体审查通过**。

## 2. Runtime 边界

### 2.1 配置与 readiness

新增独立配置：

| Runtime | 配置 |
|---|---|
| LLM | `SKILLX_LLM_RUNTIME_TYPE`、`SKILLX_LLM_ENDPOINT`、`SKILLX_LLM_MODEL`、`SKILLX_LLM_API_KEY_ENV` |
| Agent | `SKILLX_AGENT_RUNTIME_TYPE`、`SKILLX_AGENT_COMMAND`、`SKILLX_AGENT_ALLOWED_EXECUTABLES` |
| Agent sidecar 专属 | `SKILLX_AGENT_ENDPOINT`、`SKILLX_AGENT_MODEL`、`SKILLX_AGENT_API_KEY_ENV` |

`traex_managed` 和 `fixture` 只要求 Agent command/协议，不要求 Host 配置 LLM endpoint。
旧 `SKILLX_AGENT_PROVIDER_TYPE`、`SKILLX_TRAEX_*`、`SKILLX_MODEL_*` 保留一个版本；
只有旧 `agent_provider_type=real` 会把旧模型配置显式映射到 `openai_sidecar`，并记录 deprecation warning。
旧模型配置不会被猜成 Direct LLM。

`/health/ready` 现在分别返回 `agentRuntime`、`llmRuntime` 和
`modelRuntimeFallbackPolicy`，包含 configured、protocol/probe、model、source 和 digest。
Settings 显示两张独立 Runtime 卡片、当前执行路径、费用提示及“执行中错误不 fallback”策略。

### 2.2 统一结构化生成

新增：

- `StructuredGenerationRequest`
- `StructuredGenerationResult`
- `StructuredGenerationRuntime`
- `StructuredGenerationResolver`
- `AgentStructuredGenerationAdapter`

`OpenAICompatibleAdapter` 只读取 LLM 配置；`TraeXSubprocessAdapter` 只读取 Agent 配置。
LLM 节点和 LLM Presentation 均通过同一 resolver，不再各自实例化 Provider。

Agent fallback 的 Host 边界为：

```text
requested_capabilities=()
allowed_tools=[]
max_steps=1
strict outputSchema
Host Token / duration / payload / cancel limits
```

### 2.3 冻结与审计

Workflow DSL 新增默认策略：

```json
{
  "modelRuntimePolicy": {
    "order": ["llm_runtime", "agent_runtime"],
    "fallback": "startup_unavailable_only"
  }
}
```

该字段进入 WorkflowVersion digest。迁移 `0019` 新增：

- `runs.runtime_snapshot_json`
- `model_usage.runtime_used`
- `model_usage.runtime_digest`
- `model_usage.fallback_reason`

Worker 和 Presentation 只消费 Run 冻结快照；运行期间配置 digest 变化会失败
`MODEL_RUNTIME_CONFIG_DRIFT`，不会改用另一 Runtime。

## 3. Runtime 矩阵

自动化覆盖使用真实 HTTP、真实 API/Worker 子进程、临时 SQLite 和真实 TraeX-compatible
Agent 子进程；未使用 `modelProfile=fake` 证明 fallback。

| LLM | Agent | 结果 |
|---|---|---|
| HTTP 200 | ready | `direct_llm` 成功，Usage 完整 |
| unconfigured | ready | LLM 节点与 LLM Presentation 均 `agent_fallback` |
| unconfigured | unavailable | 稳定失败 `MODEL_RUNTIME_UNAVAILABLE` |
| HTTP 401 | ready | `MODEL_AUTH_FAILED`，保留 `direct_llm complete=false`，不 fallback |
| HTTP 429 | ready | `RATE_LIMITED`，保留单一 Direct Usage，不 fallback |

真实进程 fallback 复核证据：

| 字段 | 值 |
|---|---|
| WorkflowVersion | `6c30b13f-7a32-41b6-b9ed-536805231d4a` |
| Workflow digest | `sha256:5b1e51a596d8930831b42fac99a2981c2643197f8b1d4645be933f9740921841` |
| Run | `26e26c14-e9fd-43b6-b68c-deeed2b2a62a`，`succeeded` |
| Presentation | `5de3e2c5-fe62-4898-846f-41745d87c575`，`completed` |
| Runtime | `agent_fallback` / `fixture` |
| Runtime digest | `sha256:1eaef4fcebf9f0984e8a224cd3875989f251b516226c59d88d6cbcdfdc33495b` |
| Policy digest | `sha256:60ec0d8f90a9954579570fe99a0df2ef78af165c2bd506e68b725549ba8bcdbb` |
| Fallback reason | `LLM_RUNTIME_UNCONFIGURED` |
| Workflow LLM Usage | 12 input + 8 output = 20，complete |
| Presentation Usage | total 20，complete |
| Tool requests | 0 |

上述临时验收栈已清理；ID 仅用于本次 Replay 关联，不含 key。

## 4. 技术证据与 SSE

### 4.1 ErrorEnvelope

- 持久信封新增 `errorEnvelopeVersion=1`。
- `persisted_error()`、`public_error()`、`technical_error()` 重复调用保持幂等。
- 已截断 `{summary,truncated}` 不再被 JSON 编码为下一层 summary。
- 技术接口返回 `errors[]` 与按 digest 去重的 `evidence[]`。
- 12 个 Node 与 Run 复用同一长日志时，13 个 ref 只保留 1 份 evidence。
- 整个响应按 UTF-8 bytes 限制为小于 6000，不再按 Run/Node 分别分配 4000 字符。
- RunDetail 分区显示 code、scope、建议动作、evidence digest/ref 和脱敏详情。

### 4.2 SSE

- `useRunStream` 接收 Run status；状态未知或已终态时不创建 EventSource。
- 收到 `run.completed|run.failed|run.cancelled` 后立即解绑并 `close()`。
- 真实 Chromium 在终态 RunDetail 等待 5 秒，`/events/stream` 请求数不增长。
- Chat 当前使用持久事件轮询，不存在另一处 EventSource 自动重连入口。

## 5. 验证记录

| 门禁 | 最终结果 |
|---|---|
| `make demo-check` 连续两次 | 通过 |
| 后端 Ruff | 通过 |
| 后端 Pytest | 290 passed |
| Alembic 空库升级 | `0001→0019` 通过 |
| 前端 TypeScript / ESLint | 通过 |
| 前端 Vitest | 11 files / 49 passed |
| 前端 production build | 通过 |
| 进程级 E2E | 37 passed，两轮均通过 |
| `make browser-ui-check` 连续两次 | 2 passed + 2 passed |
| Chromium 终态 SSE 5 秒断言 | 通过 |
| console | 0 error / 0 warning |
| `git diff --check` | 通过 |

## 6. 未完成的外部验收

本机仅检查变量是否存在，未读取或记录任何 secret：

```text
SKILLX_LLM_ENDPOINT: 未配置
SKILLX_LLM_MODEL: 未配置
SKILLX_LLM_API_KEY: 未配置
SKILLX_AGENT_ENDPOINT: 未配置
SKILLX_AGENT_MODEL: 未配置
SKILLX_AGENT_API_KEY: 未配置
OPENAI_API_KEY: 未配置
ACEDATACLOUD_API_TOKEN: 未配置
```

因此以下项目不能用 fixture 或本地确定性 HTTP Provider 代替：

- 外部真实 Agent Runtime 的 function calling/结构化生成质量；
- 外部真实 LLM Runtime 的模型兼容性与真实计费；
- 七条无 fixture 指令最低语义集及其 Run/Turn/Usage；
- `NEXT_STEPS-022.md` 要求的外部 Provider 最终通过结论。

恢复配置示例：

```bash
export SKILLX_AGENT_RUNTIME_TYPE=openai_sidecar
export SKILLX_AGENT_COMMAND='["python3","../examples/openai_agent_sidecar.py"]'
export SKILLX_AGENT_ALLOWED_EXECUTABLES='["python3"]'
export SKILLX_AGENT_ENDPOINT='https://provider.example/v1/chat/completions'
export SKILLX_AGENT_MODEL='provider-agent-model'
export SKILLX_AGENT_API_KEY_ENV='SKILLX_AGENT_API_KEY'

export SKILLX_LLM_RUNTIME_TYPE=openai_compatible
export SKILLX_LLM_ENDPOINT='https://provider.example/v1/chat/completions'
export SKILLX_LLM_MODEL='provider-llm-model'
export SKILLX_LLM_API_KEY_ENV='SKILLX_LLM_API_KEY'
```

配置后先在 Settings 执行 Agent 主动探测，再执行七条语义集。只有真实 provider/model、
Run/Turn、runtime/policy digest 与完整 Usage 均记录后，才可进入 023 整体审查。
