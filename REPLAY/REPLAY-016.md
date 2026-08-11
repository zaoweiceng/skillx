# REPLAY-016：用户可见主流程闭合

日期：2026-08-06

依据：[`NEXT_STEPS-016.md`](../STEP/NEXT_STEPS-016.md)、
[`review/design-compliance-review-016.md`](../review/design-compliance-review-016.md)、
[`review/code-review-016.md`](../review/code-review-016.md)。

执行基线：`0a146a0`（`feat: 完成 REPLAY-015 冻结计划成为 Host 执行事实`）。
本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：单用户 Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 结论

016 闭合了 015 后端执行事实到用户结果展示之间的主路径：

```text
自然语言任务
  → 服务端受控只读探索策略
  → 可刷新恢复的 AgentRun 消息/轨迹
  → UI 固化、静态检查、人工发布
  → 新 Conversation L0/L1 发现首次 lineage
  → Workflow terminal output 返回对话
  → succeeded/failed RunDetail 可观察

真实 Tool 失败
  → argumentsDigest 完整 attempt 证据
  → 冻结计划 continuation
  → Host 强制 revision budget
```

`make demo-check` 与独立 `make browser-ui-check` 均通过。Local Demo 范围内，
“首次探索—固化—复用—失败接力—结果返回”用户可见主流程现已完整跑通。

## 2. RunDetail 白屏修复

`RunDetailPage` 的 Zustand selector 只返回 store 内已有引用或 `undefined`，组件外使用模块级
稳定空数组回退。`refresh` 保持 `useCallback` 引用稳定，`useRunStream` 不因 render 重建
EventSource。

组件测试不再 mock Zustand store，实际验证：

- 空事件 store 正常显示；
- store 新事件触发页面更新；
- 同一 Run 只创建一个 EventSource；
- cleanup 正确关闭订阅。

真实浏览器分别打开 succeeded 与 failed Run。成功页显示节点、最终输出、lineage 和 4 个
Artifact；失败页显示源错误、FailureObservation、checkpoint、修订入口和 Artifact。console
没有 `getSnapshot should be cached`、maximum update depth 或未捕获异常。

## 3. 受控自然探索与消息契约

Local Demo 自然 Agent 策略只包含 Host 已注册的 effect=none Broker：

- `lookup.table`；
- `browser.read`；
- `read.local`；
- `summarize.text`。

默认入口取“只读策略 ∩ Host grants”；显式 `allowedCapabilities` 与 Selection
`requiredCapabilities` 只能收窄。策略外或 Host 未启用能力在创建 Run 前返回
`AGENT_CAPABILITY_DENIED`，不会留下一个注定失败的 queued Run。

自动 `continue_agent` 和显式 `/agent-runs` 共用：

- capability resolution；
- `enqueue_agent_run` capability audit；
- `build_agent_run_message`；
- `type/state/conversationRunId/runType/text` 消息字段。

前端以 `type=agent_run` 为主判别，并兼容历史缺少 `runType` 的消息。刷新后可恢复最近
AgentRun、状态、事件、错误和固化按钮。进程级 E2E 只调用 `/messages`，未传
`allowedCapabilities`，Browser、lookup、file/read 三类均 succeeded。

## 4. Workflow 结果返回 Conversation

assistant workflow message 只持久关联可信 `runId`。ChatPage 加载历史消息时去重并读取关联
Run，活动 Run 使用 800ms 有界轮询直到 terminal：

- queued/running 显示状态；
- succeeded 优先显示 `output.reply`，否则显示结构化 output；
- failed/cancelled 显示稳定 code/message；
- 刷新重新从 Run 恢复相同结果；
- 重复 terminal 读取只更新 `runId → Run` 映射，不创建重复消息。

AgentRun terminal output 同样从 `ConversationRun` summary 显示；结果和错误容器增加
`status/alert` 语义。

真实 UI 中，第二个新 Conversation 经 L0/L1 命中本轮首次探索 lineage，Browser 读取变化后的
“016 UI 第二次复用正文”，对话直接显示该结果；运行详情显示同一 output。

## 5. Replan Budget

可信 `ExpectedPlanContract` 新增：

- `maxRevisions`；
- `usedRevisions`；
- `remainingRevisions`。

三者进入 checkpoint source evidence/digest、continuation request、运行 lineage 和 API。
Host 在接受新 patch 前检查预算：

- 初始结构化 plan revision 1 不计 patch budget；
- legacy 首次生成 revision 1 消耗一次；
- 相同 patch digest 幂等重放返回同一 revision，不重复扣减；
- 不同 patch 超限返回 `REPLAN_BUDGET_EXHAUSTED`。

专项测试直接驱动 Host，证明第一次 patch 后 `used=1/remaining=0`，相同 patch 重放仍保持
该值，第二个不同 patch 被拒绝。

## 6. Attempt 参数证据

Host 生成的 `tool.result` audit 现在保留 `argumentsDigest`。构造 checkpoint 时，每个 planned
attempt 必须同时满足：

- planDigest、stepId、attempt、callId、status 完整；
- argumentsDigest 非空；
- event payload 与 Artifact metadata 一致；
- Artifact 文件 hash/size 正确；
- Artifact envelope 与 event/metadata 一致。

任一缺失或冲突都会生成 `remainingPlan.mode=integrity_failed` 且 checkpoint terminal。
ConversationRun API 返回成功 trajectory 的全部 `stepAttempts` 和 `replanBudget`。

真实 Browser 503 接力证明 browser step 可区分 failed attempt 1 与 succeeded attempt 2；
两个 attempt 的 argumentsDigest 非空且一致。篡改 Artifact metadata 的测试稳定进入
terminal integrity failure。

## 7. 真实主流程

本轮使用全新 Alembic 数据库、独立 API/Worker、Vite、动态本地站点和 Chromium：

1. 从 `/chat` 自然输入 Browser 任务，自动 Agent succeeded，页面显示 Tool 轨迹、结果和固化按钮。
2. 点击固化，在 WorkflowEditor 执行静态检查并人工发布 v1。
3. 修改网页正文，新开 Conversation 自然请求同类任务。
4. 观察 L0→L1、首次 lineage、Browser 新正文和 Workflow succeeded。
5. 新结果直接显示在 Chat；succeeded RunDetail 显示同一 output。
6. 动态站点返回真实 503，Chat 显示 terminal 错误；failed RunDetail 显示源失败、
   checkpoint、修订入口和 Artifact。
7. 逐页访问 Chat、Components、WorkflowEditor、RunDetail；console 无 error。

`make browser-ui-check` 将上述用户主路径固化为独立门禁。Chromium 因权限、sandbox 或缺少
可执行文件不能启动时，只允许带明确原因 skip；业务断言失败不会降级为 skip。

## 8. 验证记录

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `make demo-check` | 0 | 全量质量与进程级门禁通过 |
| 后端 Pytest | 0 | 258 passed in 27.77s |
| Alembic 空库升级 | 0 | `0001→0017` |
| 前端 TypeScript/ESLint/Vitest/build | 0 | 32 passed，production build 通过 |
| `cd backend && uv run pytest e2e -q -p no:cacheprovider` | 0 | 34 passed in 121.32s |
| Browser/lookup/file 自然入口专项 | 0 | 1 场景内三类均 succeeded |
| replan/legacy/真实 Browser attempt 专项 | 0 | 3 passed |
| `make browser-ui-check` | 0 | 1 passed in 9.74s |
| `git diff --check` | 0 | 无空白错误 |

## 9. 完成矩阵

| 门禁 | 证据 |
|---|---|
| Natural exploration | `/messages` 默认入口执行 Browser/lookup/file；只授予已注册只读 Broker |
| Agent visibility | 统一 message builder；刷新恢复 status/events/error/output/solidify |
| Distill/publish | 真实 UI 固化、静态检查、人工发布 v1 |
| Progressive reuse | 新 Conversation L0/L1 命中首次 lineage，无 id 直调 |
| Result delivery | Workflow terminal output/error 直接显示，刷新恢复且不重复 |
| Run detail | 真 Zustand + SSE；成功/失败页均正常，console 无错误 |
| Failure handoff | 真 503、Worker 接力、remaining-only、结果可观察 |
| Replan budget | Host 强制 max/used/remaining；幂等重放不重复扣减 |
| Attempt evidence | event/metadata/envelope 三方一致；failed 1/succeeded 2 可区分 |
| Quality | `demo-check`、独立真实 UI 门禁和 `git diff --check` 全绿 |

NEXT_STEPS-016 的 A-G 在 Local Demo 范围内完成。准确表述更新为：
**后端 authoritative plan、失败接力与用户可见主流程均已跑通。**
