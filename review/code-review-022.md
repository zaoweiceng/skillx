# 代码审查报告（022）

审查日期：2026-08-10

代码基线：`774a1c6`，审查对象为 `REPLAY-021.md` 对应的未提交工作树。范围覆盖 Agent/LLM Runtime、
Provider readiness、LLM/Agent 节点、Presentation、错误证据、SSE、主题和浏览器门禁。

## 1. 结论

**不通过：2 个 P1、1 个 P2，另有 1 个真实 Provider 验收阻塞。**

021 的 UTC、响应式、binding、模板静态校验、默认错误投影、主题和门禁修复有效。当前主要问题已经从页面基础可用性，
转移到 Runtime 边界和诊断数据契约。

## 2. P1 Findings

### P1-1：Agent 与 LLM 共用全局模型配置，LLM 未配置时没有 Agent fallback

位置：[`config.py:25`](../backend/app/config.py)、[`agent_provider.py:33`](../backend/app/application/agent_provider.py)、
[`agent_provider.py:76`](../backend/app/application/agent_provider.py)、
[`adapters.py:292`](../backend/app/runtime/adapters.py)、[`models.py:49`](../backend/app/runtime/models.py)、
[`executors.py:402`](../backend/app/runtime/executors.py)、
[`product_history.py:410`](../backend/app/application/product_history.py)、
[`SettingsPage.tsx:42`](../frontend/src/pages/SettingsPage.tsx)。

当前只有 `model_endpoint/model_name/model_api_key_env`，同时承担三种不同职责：

1. 为示例 OpenAI Agent sidecar 提供模型；
2. 为工作流 LLM 节点提供结构化生成；
3. 为 LLM Presentation 提供文本整理。

`LLMExecutor` 只在 fake 与 `OpenAICompatibleAdapter` 之间二选一；Presentation 也直接实例化同一 Adapter。
AgentHost 已配置时没有 resolver 或 bridge。Provider readiness 又把所有 real Agent 都硬编码为必须提供 endpoint/model/key，
无法表达“TraeX 自带模型、Host 不持有 endpoint”。Settings 只展示一个“语义 Agent”卡片，且文字仍为
“外部 Runtime 未配置时明确失败，不允许隐式升级到 Agent”，与 022 需求冲突。

真实复现：Agent fixture 已配置、LLM endpoint 未配置，发布一个非 fake LLM 节点后，Run
`4b05ed20-ba7b-4b05-b65a-e140e3fde62d` 失败 `MODEL_NOT_CONFIGURED`，没有任何 ModelUsage。

修复必须建立两个独立 Runtime 和统一结构化生成 resolver。Agent fallback 需要 `allowed_tools=[]`、严格 Schema、单步预算和
完整 Usage/来源审计，不能把普通 Agent 的 workflow/browser Tool 权限带入 LLM 节点。

### P1-2：技术错误信封重复包装，限长只作用于单个字段而非整个响应

位置：[`error_policy.py:89`](../backend/app/application/error_policy.py)、
[`error_policy.py:165`](../backend/app/application/error_policy.py)、
[`security.py:66`](../backend/app/application/security.py)、[`routes.py:3116`](../backend/app/api/routes.py)、
[`RunDetailPage.tsx:274`](../frontend/src/pages/RunDetailPage.tsx)。

`technical_error()` 对已经持久化的 envelope 再调用 `persisted_error()`，而 `public_error()` 又会再次调用；长日志由
`bounded_technical_json()` 变成 `{summary: encoded-json}` 后，后续包装会继续对这个 summary 编码。
路由同时返回 Run error 和每个 Node error，每项分别允许约 4000 字符，没有对整个响应做限制。

真实 Browser 失败中，`GET /runs/7b5a8c95-.../technical-error` 返回 8898 bytes，页面出现三层转义的
`summary`，同一 Playwright 启动日志在 Run 与 Node 中重复。路径和凭据已经脱敏，但信息不可读，也不满足“整体有界”。

应让 error envelope 具备可识别版本并保证幂等；技术接口返回单一结构化摘要、去重后的 evidence refs 和整体字节上限，
原始大日志应作为受控 Artifact 按需查看。

## 3. P2 Finding

### P2-1：Run 终态后 EventSource 自动重连，持续请求已关闭的 SSE

位置：[`useRunStream.ts:35`](../frontend/src/hooks/useRunStream.ts)、
[`RunDetailPage.tsx:49`](../frontend/src/pages/RunDetailPage.tsx)、[`routes.py:3397`](../backend/app/api/routes.py)。

后端在 Run 已终态且没有新事件时结束 SSE；浏览器原生 EventSource 会自动重连。`useRunStream()` 只以 runId 为依赖，
没有接收 run status，也没有在 `run.completed|run.failed|run.cancelled` 后 `close()`。人工停服前 API 日志出现大量相同
`/events/stream` GET，失败 Run 页面保持打开时会无限产生噪音与无意义负载。

应在收到终态事件时立即关闭 stream，并在已加载 Run 为终态时根本不创建 EventSource；新增浏览器测试确认终态后数秒内
请求数不再增长。

## 4. 真实 Provider 验收阻塞

当前环境的 `SKILLX_MODEL_ENDPOINT`、model、API key、OpenAI key 和 AceDataCloud token 均未配置。
因此 OpenAI sidecar 的真实 function calling、严格结构化输出、真实 Token 和七条自然语言最低语义集仍未执行。
fixture 只能证明 Host 协议、权限、恢复和 UI，不证明真实模型决策质量。

## 5. 已确认关闭的 021 Findings

- 所有已审查公开时间使用 UTC RFC 3339 `Z`，组件与工作流历史显示正确当地时间；
- 760～1200px Chat 将历史和 trace 改为抽屉，消息主列优先；
- 普通 Chat/Run API 不再暴露命令、PID、路径和底层日志；
- Presentation 未知 placeholder 会阻止静态检查和发布；
- 数组 binding 不再通过纯子串匹配声明 serverVerified；
- 亮色 notice/status 使用独立 token，自动对比度通过；
- readiness 不再同步发起付费调用，probe 显式且计入 operational usage；
- 浏览器安装与执行门禁已拆开，正常环境连续两次执行通过且不允许 skip。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| 后端 | Ruff 通过；283 passed |
| 前端 | TypeScript、ESLint、build 通过；48 passed |
| Alembic | `0001→0018` 通过 |
| 进程级 E2E | 34 passed |
| 浏览器门禁 | 沙箱外连续两次 2 passed；受限沙箱内因 MachPort 权限正确失败 |
| 人工真实进程/UI | Chat、补参、开关、组件历史、RunDetail、Editor、Presentation、亮暗主题均执行 |
| 非 fake LLM + Agent 已配置 | 失败 `MODEL_NOT_CONFIGURED`，证明 fallback 缺失 |
| 技术证据响应 | 8898 bytes，多层 summary，复现 P1-2 |
| Git | `git diff --check` 通过；未修改审查对象业务代码 |

修复范围与验收条件见 [`NEXT_STEPS-022.md`](../STEP/NEXT_STEPS-022.md)。
