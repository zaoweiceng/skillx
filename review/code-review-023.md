# 代码审查报告（023）

审查日期：2026-08-10

代码基线：`9f9fb43`。审查对象为 `REPLAY-022.md` 对应已提交代码。范围覆盖 Runtime 拆分、Agent fallback、
工作流选择、readiness、Usage、错误证据、SSE 和前端主题/交互。本轮未修改业务代码。

## 1. 结论

**不通过：3 个 P1、3 个 P2；另有 1 个真实语义 Provider 验收阻塞。**

022 的原有三个代码问题均已实质关闭：Runtime 已拆分且 fallback 可达；错误信封幂等并整体限长；终态 EventSource 已关闭。
新的主要问题集中在“产品声称可用”和“真实可用”之间的证据不一致。

## 2. P1 Findings

### P1-1：默认 Local Demo 的自动选择不会从普通自然语言提取单字段参数

位置：[`traex_stub.py:310`](../examples/traex_stub.py)、[`traex_stub.py:319`](../examples/traex_stub.py)、
[`traex_stub.py:325`](../examples/traex_stub.py)。

fixture 只自动提取 URL 或 `qN`；普通文本只有带 `[[SELECTION:run_full|...]]` 测试指令时才被当作参数。
默认 `auto` 路径把 `concrete_input` 设为 `None`，于是任何带一个必填字段的工作流都会 `ask_user`。

真实复现：全新数据库、API、Worker、Vite 和浏览器中输入“上海天气如何”，Agent 已找到并 L1 披露天气工作流，仍返回
“请补充输入：city”。第二条再发“上海”才成功。服务端的 prompt 已明确要求从用户原文提取 binding，根因是产品默认使用
一个不具备语义能力的 fixture，却让它承担语义选择，不是服务端 Schema 校验过严。

不要增加城市名正则。fixture 应只用于协议测试；可对用户开放的工作流开启模式必须有真实语义 Agent Runtime，并以
无 `[[...]]` 指令的自然语言用例验收。没有语义 Runtime 时应明确降级/阻止，而不是显示为可自动选择。

### P1-2：Browser readiness 只检查 allowlist，实际不可启动时仍显示 configured

位置：[`routes.py:550`](../backend/app/api/routes.py)、[`SettingsPage.tsx:83`](../frontend/src/pages/SettingsPage.tsx)。

`/health/ready` 仅依据 `browser_allowed_hosts` 非空返回 `configured`，没有检查 Chromium 二进制、Playwright 兼容性或 OS
启动权限。手工 Settings 显示 Browser Runtime=`configured`，但同一环境真实 `chromium.launch()` 立即因 macOS MachPort
权限失败。用户只能在工作流执行后才看到故障，违背 readiness 的含义，也未完成 `NEXT_STEPS-022` 明确要求的启动预检。

应由实际执行 Browser 的 Worker 启动一次有界预检并持久化 heartbeat/facts；API 展示
`available|binary_missing|permission_denied|stale|unconfigured`，不能由 API 根据 allowlist 推断 Worker 能力。

### P1-3：提交删除 39 份版本化修复文档，破坏审计链

位置：提交 `9f9fb43` 的根目录文档变更。

该提交除业务修复外，删除 `NEXT_STEPS-001..021` 和多份 `REPLAY-003..021`，当前 Git 只跟踪
`NEXT_STEPS-022.md` 与 `REPLAY-022.md`。本项目的修复流程明确依赖“根据上一版 REPLAY 和设计文档审查下一版”；删除历史
输入使基线、决策、未完成项和复验事实只能从 Git 历史猜测，且 `REPLAY-022` 仍写“未提交工作树”，与实际提交状态不符。

应恢复这些版本文档，或做一次显式、可索引且保留原文/digest 的归档迁移；不能在功能提交中静默删除。

## 3. P2 Findings

### P2-1：零 LLM Run 被标记为 Agent fallback，混淆“冻结候选”和“实际使用”

位置：[`structured_generation.py:212`](../backend/app/application/structured_generation.py)、
[`orchestrator.py:277`](../backend/app/application/orchestrator.py)、
[`RunDetailPage.tsx:446`](../frontend/src/pages/RunDetailPage.tsx)。

每个 Run 无论是否含模型节点都会调用 `runtime_snapshot_for_definition()`，并把启动时可选路径写成 `runtimeUsed`。
手工天气 Run 是 `zero_llm`，没有 `llm.call.completed` 或 ModelUsage，页面却显示 `Agent fallback / LLM_RUNTIME_UNCONFIGURED`。
这会让费用和执行路径审计产生假事实。

应将字段拆成 `runtimeSelection`（冻结候选）与由 ModelUsage/实际调用产生的 `runtimeUsed`；零 LLM Run 显示“模型未调用”。

### P2-2：终态页面正确不建 SSE，但 UI 仍显示“LIVE / SSE / 等待新事件”

位置：[`RunDetailPage.tsx:217`](../frontend/src/pages/RunDetailPage.tsx)、
[`RunDetailPage.tsx:228`](../frontend/src/pages/RunDetailPage.tsx)。

`useRunStream()` 已在终态不创建 EventSource，浏览器请求计数也稳定；但直接打开终态 Run 时本地 event store 为空，页面仍显示
脉冲 live 标签和“等待新事件”。功能无泄漏，状态表达却相反。终态应显示“实时流已关闭”，并优先展示持久事件或明确“本会话
未订阅历史事件”。

### P2-3：工作流编辑器元数据表单仍缺少完整主题化尺寸

位置：[`index.css:1699`](../frontend/src/index.css)、[`index.css:1713`](../frontend/src/index.css)、
[`index.css:1838`](../frontend/src/index.css)。

`.inspector-fields input` 只有宽度、边框和颜色，没有最小高度、内边距和圆角；亮色页面看起来像多条空横线。
`.inspector-field input` 又硬编码暗色背景 `#091216` 和亮字，导致节点表单在亮色主题中风格不一致。checkbox/select 仍保留
明显原生外观。内容可读、可点击，但不满足与 Settings/Chat 同等级的视觉完成度。

## 4. 真实语义验收阻塞

当前环境中 `SKILLX_LLM_*`、`SKILLX_AGENT_*` endpoint/model/key、`OPENAI_API_KEY` 和
`ACEDATACLOUD_API_KEY` 均未配置。现有 fixture 是独立真实子进程，足以证明 Host 协议、fallback、Schema、Tool 禁止和 Usage，
但输出“fixture/已完成任务”不能证明自然语言决策、参数抽取、function calling 或内容整理质量。

## 5. 已确认关闭的 022 Findings

- Agent Runtime 与 LLM Runtime 独立配置、readiness、Settings 和兼容映射已实现；
- LLM 节点与 LLM Presentation 共用冻结的 `StructuredGenerationResolver`；
- Agent fallback 为无 Tool、单步、严格 Schema，Usage 与来源完整；
- Direct LLM 401 后不切换 Agent，避免双费用与语义漂移；
- ErrorEnvelope 版本化、幂等、去重且技术响应整体有界；
- 未知/终态 Run 不建 EventSource，终态事件到达后主动关闭；
- 迁移 0019、后端/前端/进程 E2E 和正常权限 Chromium 门禁通过。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；290 backend、49 frontend、37 process E2E |
| Alembic / build / lint | 通过；迁移到 0019，前端生产构建成功 |
| Browser UI（受限沙箱） | 2 failed；MachPort 权限，证明 readiness 假阳性 |
| Browser UI（正常主机权限） | 2 passed in 26.60s |
| 手工独立进程/UI | 主题、历史、开关、组件执行/历史、RunDetail、Presentation 均可用 |
| 普通自然语言参数 | 未通过；“上海天气如何”仍追问 `city` |
| Agent fallback | 通过协议级；节点与 Presentation 各 20 Token、Tool 0 |
| 外部真实语义 Provider | 未执行；配置缺失 |

修复范围和验收条件见 [`NEXT_STEPS-023.md`](../STEP/NEXT_STEPS-023.md)。
