# 代码审查报告（017）

审查日期：2026-08-06

代码基线：`0a2237f688f9ee91b6f924c0b169e448c3d48e7a`；审查差异为
`0a146a0..0a2237f`。范围为 `REPLAY-016.md`、Conversation/AgentRun、TaskCheckpoint recovery、
Workflow result delivery、Chat/RunDetail 和真实浏览器门禁。Local Demo 不考虑鉴权。

## 1. 总结

016 对原 P0/P1/P2 的主体修复有效：RunDetail 不再无限更新；默认自然入口使用服务端只读 capability policy；
Agent 消息契约统一；Workflow terminal output 返回 Chat；Host 强制 replan budget；attempt 参数证据完成三方校验。

本轮发现 **1 个 P1、2 个 P2**。没有发现新的 P0。P1 是失败接力结果没有回到原 Conversation，直接阻断用户
要求的最后一步；两个 P2 是恢复可观察字段和浏览器门禁没有覆盖真实 handoff。

## 2. P1

### P1-1 — continuation 成功后没有持久 Conversation 关联，Chat 永远只读取源失败 Run

位置：[`task_recovery.py`](../backend/app/application/task_recovery.py) 第 333-385、440-497 行；
[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx) 第 55-132、336-373 行。

`TaskCheckpointDriver.start_continuation()` 调用 `enqueue_agent_run()` 创建 continuation，并把 id 写入 checkpoint；
`_reconcile_continuation()` 只更新 checkpoint 和向源 Run 追加 `agent.handoff.*` 事件。两处都没有创建或更新
Conversation `Message`。

Chat 的可发现集合只来自 `conversation.messages[].content.conversationRunId`。因此它只加载源 Run；源 Run 一旦
变为 `failed`，轮询立即停止。即使后台 continuation 后来 succeeded，页面也没有路径发现其 id/output。

真实复现：

- source Run：`73ce7a59-b887-497d-b4c3-ef03926fe8f3`，failed；
- checkpoint：`9dbb70f8-321f-476e-8ea5-380fc82572eb`，最终 succeeded；
- continuation：`edc641e9-99bc-426c-9692-43ebcb9f2c8b`，succeeded，output 为“恢复后的正文”；
- 原 Conversation 列表仍只有指向 source Run 的 assistant message；刷新 UI 后仍显示 503 错误。

影响：系统后台完成了任务，用户却收到失败结论；“失败接力—结果返回”闭环不成立，REPLAY-016 的整体通过
结论过度标记。

修复原则：为 continuation 建立可恢复、幂等的 Conversation 关联，并让 Chat 在源失败后继续观察接力链，直到
链终态。不能只把 output 放在临时 React state，也不能覆盖源失败证据。

## 3. P2

### P2-1 — TaskCheckpoint API 隐藏了已经持久化的 continuation 终态与输出摘要

位置：[`task_recovery.py`](../backend/app/application/task_recovery.py) 第 460-464 行；
[`routes.py`](../backend/app/api/routes.py) 第 4194-4232 行。

reconcile 已把 `continuationStatus` 和 `continuationOutputDigest` 写入 snapshot，但
`GET /task-checkpoints/{id}` 没有返回这两个字段。API 只暴露 `continuationRunId`。

影响：即使前端以后沿 checkpoint 链观察恢复，也无法直接区分 continuation 仍在运行、成功、失败或摘要是否
变化，只能额外请求 run 并自行推断；当前页面更是没有调用该 API。该缺口放大了 P1-1 的不可观察性。

修复：公开已持久化的 `continuationStatus/continuationOutputDigest`，并为 checkpoint → continuation → child
checkpoint 定义稳定的链式摘要；字段必须来自服务端持久状态。

### P2-2 — 真实浏览器门禁的“失败段”测试了错误的路径，无法阻止 P1-1 回归

位置：[`test_ui_main_flow.py`](../backend/browser_e2e/test_ui_main_flow.py) 第 133-142 行。

测试在发布网页 Workflow 后提交普通 transient URL，然后预期 `INVALID_INPUT`。这实际走的是 `run_full`
Workflow 输入错误，不是 AgentRun 真实 Tool 503 handoff。测试没有：

- 强制 `continue_agent/planned_browser_summary`；
- 在源失败时禁用 recovery；
- 停止源 Worker并恢复站点；
- 启动新 Worker；
- 等待 continuation terminal；
- 断言最终 output 返回原 Chat。

影响：`make browser-ui-check` 全绿仍会遗漏完整闭环的最后一个主路径阻塞，并给 REPLAY 报告提供错误的通过信号。

修复：保留现有 Workflow failed RunDetail 场景，但新增独立 Agent handoff UI 场景，按真实进程边界完成 503、
Worker 更换、站点恢复和最终结果回显。

## 4. 已确认有效的 016 修复

- `RunDetailPage` 使用稳定空事件引用；真实 Zustand/SSE 页面不再白屏。
- 自动与显式 Agent 入口共用 `build_agent_run_message()` 和服务端 capability resolution。
- `requiredCapabilities` 只能收窄 Local Demo 只读集合，策略外能力在入队前拒绝。
- Chat 根据持久 `runId` 加载/轮询 Workflow Run，terminal output/error 刷新可恢复。
- Host revision budget 的 max/used/remaining 校验、消费和幂等 replay 语义成立。
- `argumentsDigest` 进入 ToolResult audit；checkpoint 校验 event、Artifact metadata 和 envelope。
- 真实成功链能从首次 Browser Agent 固化并由新 Conversation 复用；lookup/file 自然入口也成功。
- 真实 503 continuation 只执行 failed Browser attempt 2 和 remaining summarize，没有重做已成功 preflight。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 | 258 passed in 28.43s |
| 前端 | 32 passed；TypeScript、ESLint、build 通过 |
| Alembic | 空库 `0001→0017` 通过 |
| 进程级 E2E | 34 passed in 129.36s |
| `make browser-ui-check` | 1 passed in 9.86s，但不覆盖 Agent handoff result delivery |
| 真实 UI 正常链 | passed |
| 真实 503 continuation | backend passed；Chat result delivery failed |
| 浏览器 console | 0 error |
| `git diff --check` | 通过 |

修复顺序和验收定义见 [`NEXT_STEPS-017.md`](../STEP/NEXT_STEPS-017.md)。
