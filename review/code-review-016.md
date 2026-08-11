# 代码审查报告（016）

审查日期：2026-08-06

代码基线：`c57de5c2a762e6f1ec5e0798181e840c7cb70245`；015 实现位于当前未提交工作树。

审查范围：`REPLAY-015.md`、Agent plan/Host/Tool Broker、AgentRun/TaskCheckpoint、Conversation 选择路径、
前端 Chat/RunDetail 以及真实进程测试。Local Demo 不考虑鉴权。

## 1. 总结

015 修复的 authoritative-plan 后端链成立：计划、参数、step state、attempt、ReplanPatch 和 chain budget
均比 015 审查基线明显收紧，真实 503 接力与 33 条 E2E 通过。

本轮发现 **1 个 P0、3 个 P1、2 个 P2**。P0 是所有真实运行详情页在首次加载时都可能因 Zustand selector
返回不稳定空数组而无限更新并白屏。三个 P1 分别阻断默认首次探索、自动 Agent 的 UI 恢复，以及最终结果
返回用户。自动化全绿不能覆盖这些问题，因为前端测试 mock 了 store/EventSource，E2E 只验证 HTTP 后端。

## 2. P0

### P0-1 — `RunDetailPage` 的 Zustand selector 在无事件时创建新数组，真实页面无限更新并白屏

位置：[`RunDetailPage.tsx`](../frontend/src/pages/RunDetailPage.tsx) 第 22 行。

```ts
const events = useRunEvents((state) => (id ? state.events[id] ?? [] : []))
```

当 `events[id]` 尚不存在时，每次 store snapshot 都返回新的 `[]` 引用。Zustand/React 的 external-store
订阅要求 selector snapshot 稳定；真实浏览器产生 `getSnapshot should be cached`，随后
`Maximum update depth exceeded`，`/runs/:id` DOM 变为空。

影响：第二次 Workflow 复用虽然后端成功，用户点击唯一的“查看运行”入口后只得到白屏；所有首次加载且尚无
本地 SSE event 的运行详情都受影响，审批、失败修订和 Artifact 观察入口也一并不可用。

测试漏检原因：[`RunDetailPage.test.tsx`](../frontend/src/pages/RunDetailPage.test.tsx) 完全 mock
`useRunEvents/useRunStream`，selector 只执行一次，没有真实 Zustand 订阅。

修复：在模块级定义稳定 `EMPTY_EVENTS`，selector 返回 `state.events[id]` 或 `undefined`，在 selector 外回退到
常量；增加真实 store 的组件测试，并用已启动前端从 `/chat` 点击 `/runs/:id` 做浏览器验收。

## 3. P1

### P1-1 — 自然对话 `continue_agent` 只授予 `search.web`，通配型首次探索默认失败

位置：[`agent_run.py`](../backend/app/application/agent_run.py) 第 86-100 行；
[`routes.py`](../backend/app/api/routes.py) 第 3543-3578 行。

`AgentRunConfig.default()` 的 `allowed_capabilities` 固定为 `("search.web",)`；WorkflowSelection 返回
`continue_agent` 后，路由直接使用该默认配置。ChatPage 的 `startAgentRun()` 也没有能力参数。因此用户在
自然对话提出网页、lookup 或文件任务时，Agent 即使生成正确 ToolRequest，仍会被 Host 以
`PERMISSION_DENIED` 拒绝。

本轮真实页面复现：网页任务自动创建 AgentRun `d5720bf9-...`，请求 `browser.read`，运行 failed。手工通过
公开 API 传 `browser.read + summarize.text` 后，同一类型任务立即 succeeded，证明根因是入口 capability
contract，而不是 Browser/Worker。

修复：定义专门的“Local Demo 只读探索能力策略”，至少覆盖设计已承诺的 Browser/lookup/file/summarize；
由服务端根据已验证任务计划收窄，不能让客户端或 Agent 自行扩大。自然入口和显式 `/agent-runs` 应共享同一
能力解析结果和审计事件。

### P1-2 — 自动创建的 Agent message 缺少 `runType`，刷新后不追踪状态也不能固化

位置：[`routes.py`](../backend/app/api/routes.py) 第 3585-3590 行；
[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx) 第 51-69、443-445 行。

显式 `/agent-runs` 写入 `runType: "agent_run"`，但 `_unbound_conversation_message()` 的自动
`continue_agent` message 只写 `type: "agent_run"`。前端却以
`content.runType === "agent_run"` 过滤 run id 和渲染动作。

影响：真实运行即使 failed/succeeded，页面仍停留在“已由通用 Agent 继续处理”，不显示状态、事件、错误或
“固化为组件”；刷新也无法恢复 active run。这个问题与 P1-1 叠加，使默认入口既执行失败又不可观察。

修复：后端统一使用一个 Agent message builder，确保两个创建路径输出完全相同的判别字段；前端优先按
`type === "agent_run"` 判别，并对历史缺少 `runType` 的消息兼容。增加“sendMessage 自动
continue_agent → refresh → terminal → 固化按钮”集成测试。

### P1-3 — Workflow 成功输出没有回到 Conversation，需求的最后一跳缺失

位置：[`routes.py`](../backend/app/api/routes.py) 的 `_selection_result_message()`；
[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx) 的消息刷新和 workflow run 渲染。

`run_full` 创建的 assistant message 永久保存为“选择已执行，后续由 Workflow Runtime 接管”，只带
`runId`。ChatPage 既不轮询这些 Workflow Run，也不读取/渲染其 output。后端 Run
`e46dec6c-...` 已 succeeded 并输出新摘要，但刷新对话后仍没有结果；唯一链接又被 P0-1 阻断。

影响：系统完成了工作，却没有“将总结内容返回给用户”。从用户目标看闭环仍未完成，不能因 Run 表中有 output
而标记通过。

修复：以 `runId` 为持久关联，在 ChatPage 加载/轮询 Workflow Run 的 terminal status/output，或由 Worker
终态事务写入一条幂等的 assistant result message。无论采用哪条路径，刷新后都必须可恢复，且 failed/
cancelled 也要显示结构化结果，不能依赖临时 React state。

## 4. P2

### P2-1 — `replanBudget` 只存储不执行，Host 可在时限内接受任意多 revision

位置：[`agent_run.py`](../backend/app/application/agent_run.py) 第 1387-1399 行；
[`agent_host.py`](../backend/app/application/agent_host.py) 第 480-568 行。

legacy checkpoint 保存 `maxRevisions=1/remainingRevisions=1`，但该字段没有进入 `ExpectedPlanContract`，
仓库中也没有任何消费或递减逻辑。AgentHost 对每个合法连续 ReplanPatch 都可创建下一 revision；replan
事件不计入 step budget，实际只受总运行时限约束。

影响：REPLAY-015 所称“原 replan budget 内”并非 Host 保证；异常 Agent 可持续制造 PlanSnapshot Artifact
和事件，造成状态/存储膨胀，并绕过 legacy 只允许一次受限 replan 的设计。

修复：把可信 `max/used/remainingRevisions` 纳入 ExpectedPlanContract 和 source digest；Host 在接受 patch
前原子/进程内扣减，达到上限返回稳定错误。初始 legacy revision 1 与后续 patch revision 要分别计数并测试。

### P2-2 — checkpoint 的源 attempts 丢失 `argumentsDigest`

位置：[`agent_host.py`](../backend/app/application/agent_host.py) 第 1086-1119 行；
[`agent_events.py`](../backend/app/application/agent_events.py) 的 Tool 审计投影；
[`agent_run.py`](../backend/app/application/agent_run.py) 第 1196-1215 行。

Host 生成的 ToolResult runtime payload 有 `argumentsDigest`，Artifact metadata 也保存它；但
`AgentEventValidator._audit_event()` 没有把该字段投影到持久 `tool.result` 事件。checkpoint 又从事件读取
`item.payload_json.argumentsDigest`，结果真实 checkpoint 中 preflight/browser attempts 均为 `null`。

影响：Host 参数核对本身仍有效，但“每个 plan digest + step + attempt 都可审计参数摘要”的 015 声明不成立；
故障分析无法仅靠 checkpoint 区分不同 attempt 是否实际使用相同参数。

修复：优先从已校验 ToolResult Artifact metadata 构造 attempt，或把 `argumentsDigest` 加入 tool.result
审计白名单；生成 checkpoint 时要求有 ToolResult 的 attempt digest 非空并与 Artifact metadata 一致。

## 5. 已确认有效的 015 修复

- continuation HostRequest 绑定 source plan digest/revision、checkpoint、remaining set 和 state digest。
- Tool action/capability/effect/idempotency/arguments 在 Adapter 前与 Host 物化结果比较。
- succeeded step 重发、跳依赖、偏离 URL/path/key 和无 patch 换计划均 fail closed。
- step 状态和全部 attempts 不再按最后一个 event 静默覆盖。
- legacy checkpoint 进入 awaiting_user，answer 使用 snapshot digest CAS 且只能消费一次。
- claimGeneration 与 continuationGeneration 分离；三代失败预算 E2E 通过。
- 真实 Browser 503 接力只重试 failed step，已成功 preflight 没有重跑。
- distillation/publish、Catalog L0/L1 和首次 lineage 在后端链上保持闭合。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 | 254 passed in 27.33s |
| 前端 | 28 passed；TypeScript/ESLint/build 通过 |
| Alembic | `0001→0017` 通过 |
| 进程级 E2E | 33 passed in 124.83s |
| 真 API/Worker/Chromium 503 接力 | passed |
| 真 UI 探索/固化/发布/复用 | 后端链完成；自然入口、结果回传和运行详情失败 |
| 浏览器 console | `getSnapshot should be cached`；`Maximum update depth exceeded` |
| `git diff --check` | 通过 |

修复顺序和真实验收标准见 [`NEXT_STEPS-016.md`](../STEP/NEXT_STEPS-016.md)。
