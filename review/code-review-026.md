# 代码审查报告（026）

审查日期：2026-08-11

代码基线：`76f51f4`。审查对象为 [`REPLAY-025.md`](../REPLAY/REPLAY-025.md) 对应代码。
本轮未修改业务代码。

## 1. 结论

**不通过：1 个 P0、3 个 P1。**

025 对文档发布链、探测强度、Usage、目录门禁、SSE 和编辑器的修复有效，完整自动化与真实浏览器门禁均通过。
当前最高优先级问题是运行时接桥缺失：代码虽然声明“LLM 未配置时 fallback 到 Agent”，但无法直接使用本机
TraeX，导致用户要求的降级在真实环境中不存在。

## 2. P0 Finding

### P0-1：Agent fallback 是“配置存在性判断”，没有可使用原生 TraeX 的内置 Runtime

位置：[`runtime_config.py:100`](../backend/app/application/runtime_config.py#L100)、
[`structured_generation.py:79`](../backend/app/application/structured_generation.py#L79)、
[`agent_host.py:213`](../backend/app/application/agent_host.py#L213)、
[`adapters.py:285`](../backend/app/runtime/adapters.py#L285)。

`select_structured_generation_runtime()` 在 LLM Runtime 未配置时确实会选择 `agent_runtime`，但前提是
`agent_runtime_config().configured=true`。配置函数不会探测系统中的 TraeX；没有
`SKILLX_AGENT_RUNTIME_TYPE/SKILLX_AGENT_COMMAND` 就返回 unavailable，`AgentHost._resolve_adapter()` 随后抛
`MODEL_NOT_CONFIGURED`。

即使把 command 手工写成 `traex` 也不能工作。`TraeXSubprocessAdapter` 按项目私有 wrapper 约定追加
`--prompt`、`--max-steps`、`--max-tokens`、`--allowed-tools` 和内联 JSON `--output-schema`，并要求 stdout/stdin
使用 AgentHost 双向 NDJSON；本机原生 `traex exec` 使用另一套参数和 JSONL 事件协议。

真实证据：本机 `traex` 版本 0.200.19 可发现，但 Settings 仍显示 Agent Runtime unconfigured；关闭工作流后的
通用 Agent 以 `MODEL_NOT_CONFIGURED` 终止。直接最小调用能启动 TraeX，但当前登录令牌过期并返回 401。
重新登录只能解决认证，不能补上应用 bridge。

根因是把“Agent Runtime 抽象”与“可交付的宿主 Agent 适配器”当成同一件事。修复应提供仓库维护、内容 digest
受信的 TraeX bridge，并在没有显式 Agent 配置时自动发现它。无工具的结构化生成可使用
`traex exec --json --output-schema`；需要 `workflow.*` 的语义选择必须用 ACP/app-server/MCP 或等价双向协议把
Tool 请求和 ToolResult 接回 Host，同时保留能力、预算、取消和 strict output fencing。

不应通过取消 adapter 信任或把任意 PATH 中的二进制直接标成“可信”来修复。

## 3. P1 Findings

### P1-1：025 的 seed 元数据修复只对全新数据库生效，既有 Local Demo 永远跳过

位置：[`seed.py:28`](../backend/app/seed.py#L28)、[`seed.py:42`](../backend/app/seed.py#L42)、
[`seed.py:87`](../backend/app/seed.py#L87)、[`seed.py:97`](../backend/app/seed.py#L97)、
[`seed.py:110`](../backend/app/seed.py#L110)。

025 为所有 seed 定义补齐能力目录，但 `seed()` 的幂等条件只是“同名 Component 是否存在”。一旦组件存在，
函数既不比较系统样例 revision/digest，也不创建新的不可变 WorkflowVersion。迁移 0022 只增加 Usage error code，
没有回填能力目录。

因此从 024 或更早版本升级并复用数据库的用户继续得到旧目录；新代码和测试在全新库上通过，不能证明升级路径通过。
这会直接影响 Agent 的 L0/L1 选择质量，并让“所有内置目录已补齐”的声明依赖清库重建。

应给系统 seed 增加稳定 `seedKey/managedRevision`，只为未被用户接管的系统样例发布新版本并更新默认版本；保留旧
WorkflowVersion 和历史 Run，不覆盖用户草稿。必须增加从 pre-025 数据库升级的真实测试。

### P1-2：天气是面向最终用户的典型工作流，但没有 Presentation policy

位置：[`seed.py:233`](../backend/app/seed.py#L233)、
[`product_history.py:219`](../backend/app/application/product_history.py#L219)、
[`RunDetailPage.tsx:305`](../frontend/src/pages/RunDetailPage.tsx#L305)、
[`RunDetailPage.tsx:365`](../frontend/src/pages/RunDetailPage.tsx#L365)。

`_weather_workflow()` 的 metadata 只有 capability，没有 `presentationPolicy`。运行时默认
`enabled=false`，所以 RunDetail 虽然支持“先结构化结果、后阅读结果”，天气真实运行仍只显示 JSON：

```json
{"city":"上海","condition":"多云","temperature":19}
```

这正是 019 原始需求用于说明非专业用户体验的场景。天气不需要额外模型调用；应启用 `audience=end_user`、
`producer=template`，使用顶层字段生成稳定中文文本，继续保持 `zero_llm` 和 JSON 事实源。组件历史、RunDetail、
Chat 回看均应显示 Presentation 状态和最终文本。

### P1-3：TraeX 主动探测会被错误记录成 openai-compatible，Provider/Model 事实不可信

位置：[`agent_provider.py:303`](../backend/app/application/agent_provider.py#L303)、
[`agent_provider.py:327`](../backend/app/application/agent_provider.py#L327)、
[`runtime_config.py:46`](../backend/app/application/runtime_config.py#L46)。

`probe_provider()` 的成功和失败记录都把 `provider` 硬编码为 `openai-compatible`。这对内置
`openai_sidecar` 尚可表达协议类型，但对 `traex_managed` 是错误事实；同时 `AgentRuntimeConfig` 没有独立 provider
字段，managed 模式的 `model` 也可能为 `None`。

用户要求以 TraeX 作为 fallback 后，这会污染 readiness、Usage 和问题定位：界面可能把 TraeX 调用显示为
openai-compatible，无法回答实际用了哪个 Agent/模型。应把 `protocol`、`provider`、`model`、`adapterVersion`、
`adapterDigest` 分开；由受信 bridge/运行事件给出真实身份，Host 验证后冻结到 Turn/Run/Usage。

## 4. 已确认关闭的 025 前置问题

- `review/` 正式报告已跟踪，历史 STEP/REPLAY 链接已修复；HEAD 归档文档链可复现。
- Provider 探测要求随机 challenge、显式 `model.request.dispatched`、真实 Usage、Host ToolResult round-trip 和
  strict output；不再从 `agent.step.completed` 推断 dispatch。
- 工作流选择成功/失败均记录 Usage、dispatch、completed、Token 和 error code。
- 新建/新发布工作流缺少目录必需字段时会被拒绝；L0/L1/L2 披露边界已收紧。
- SSE 使用通用 `run.event`；手工天气 Run 首次页面已出现完整 seq 1..14，包括 MCP 与 Artifact。
- 工作流编辑器重复只读 DAG 已删除；暗色输入框配色正常。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| 文档链 / HEAD 干净归档 | 通过；95 个版本化 Markdown |
| 后端质量与测试 | Ruff 通过；303 passed |
| 迁移 | `0001 → 0022` 通过 |
| 前端质量与测试 | check/lint/build 通过；51 passed |
| 真实 API/Worker/Vite/Chromium E2E | 37 passed |
| 正常主机 Chromium UI | 2 passed |
| 手工组件/Run/历史/SSE | 通过；问候与天气成功，天气 live seq 1..14 完整 |
| 手工主题/编辑器/点击/console | 通过；亮暗主题正常，console 0 error/warning |
| 应用内 TraeX fallback | **失败**；Runtime unconfigured，通用 Agent 为 `MODEL_NOT_CONFIGURED` |
| 原生 TraeX 最小真实调用 | **失败**；CLI 可启动，但 access token 已过期，返回 401 |
| 天气 JSON + 阅读结果 | **失败**；只有 JSON，Presentation 未启用 |

修复范围和验收条件见 [`NEXT_STEPS-026.md`](../STEP/NEXT_STEPS-026.md)。
