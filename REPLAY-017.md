# REPLAY-017：失败接力结果返回原对话

日期：2026-08-07

依据：[`NEXT_STEPS-017.md`](NEXT_STEPS-017.md)、
[`review/design-compliance-review-017.md`](review/design-compliance-review-017.md)、
[`review/code-review-017.md`](review/code-review-017.md)。

执行基线：`0a2237f`。本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：单用户 Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 结论

017 指出的最后一跳已经闭合：

```text
source AgentRun 真实失败
  → TaskCheckpoint pending
  → 新 Worker 原子创建 continuation + Conversation message
  → continuation terminal
  → checkpoint 链式投影终态
  → 原 Chat 自动显示 output 或稳定 error
  → 刷新后从持久消息恢复同一结果
```

源 `ConversationRun` 始终保留 failed 和原始 Tool 错误，没有被改写为 succeeded。成功正文只从
continuation `ConversationRun.output` 读取；checkpoint 只保存 output digest 和 lineage。

`make demo-check` 与 `make browser-ui-check` 均通过。Local Demo 范围内，通配型
“首次探索—固化—复用—失败接力—修订/恢复—结果返回”闭环已在真实环境完整跑通。

## 2. 持久 Conversation 关联

`TaskCheckpointDriver.start_continuation()` 在同一事务中完成：

- 校验 checkpoint owner/generation/token；
- 创建 continuation `ConversationRun`；
- 创建唯一 assistant message，持久保存
  `conversationRunId`、`continuationOfConversationRunId`、`taskCheckpointId` 和 generation；
- 以同一 claim handle CAS `running→continuing`。

CAS 失败会删除本事务创建的 Run/message。重复 claim、迟到同名 Worker 和重复 reconcile
不会产生第二条 continuation message。专项测试同时断言只有一个 continuation Run、一个
`agent.handoff.started` 事件和一条关联 message。

多代失败时，父 checkpoint 持久保存 `childCheckpointId`，子 checkpoint 保存
`sourceCheckpointId/rootSourceRunId`。每代 source、checkpoint、continuation 和 generation
均保留，不覆盖历史失败。

## 3. 链终态与 API

`GET /task-checkpoints/{id}` 新增：

- `continuationStatus`、`continuationOutputDigest`；
- `sourceCheckpointId`、`childCheckpointId`、`nextContinuationRunId`；
- `chainStatus`、`chainError` 和完整 `lineage`。

API 从请求 checkpoint 向 child 遍历，以链末端投影
`continuing/succeeded/failed/cancelled/awaiting_user`。预算耗尽、完整性失败、契约失败和取消
分别返回稳定 code，不会永久停留在“恢复中”。

测试覆盖 `pending→running→continuing→succeeded` 的字段变化。正文不进入 checkpoint API，
仍从可信 continuation Run 获取。

## 4. Chat 自动跟踪

Chat 刷新时从持久 assistant messages 加载全部关联 AgentRun，再从 source error 或 message
提取 checkpoint id：

- source failed 且链可恢复时显示“等待接力/接力中”，不提前显示源错误；
- source Run 已终态后，改由 checkpoint 链以 800ms 间隔有界刷新 Conversation；
- continuation message 出现后自动加载其 status/events/output；
- succeeded 只在最终 continuation message 显示正文和固化入口；
- failed/cancelled/awaiting_user 显示最终稳定错误，并单独标注源错误；
- lineage 中每代 checkpoint 和 Run 均提供可访问的 API 入口。

轮询只更新按 id 索引的状态和服务端消息，不创建本地结果 message。terminal 重读和浏览器刷新
不会重复正文。

## 5. 真实 503 handoff

新增独立 Chromium 门禁，使用全新 Alembic 数据库、API、Vite、动态站点和两个 Worker：

1. Chat 自然输入 `continue_agent + planned_browser_summary`，未直接调用 Agent endpoint。
2. 动态站点真实返回 503；Browser 有限尝试后 source Run failed，checkpoint pending。
3. 源 Worker 以 `--disable-task-recovery` 运行；确认 pending 后停止。
4. 站点恢复 healthy，再启动正常新 Worker。
5. continuation 复用成功 preflight，只执行 Browser attempt 2 和 remaining summarize。
6. `transientCount` 从 2 增至 3；两个 Browser attempt 的 `argumentsDigest` 非空且一致。
7. 原 Chat 无手动刷新显示“恢复后的正文”，正文只有一份。
8. 刷新同一 `/chat/:id` 后仍显示相同正文；source Run 仍为 failed。
9. Conversation 中只有一条 continuation message，generation 为 1。
10. 浏览器 console 无 error。

既有 Workflow `INVALID_INPUT`/failed RunDetail 场景继续保留，只作为 Workflow 失败详情门禁，
不再承担 Agent failure handoff 证明。

## 6. 回归覆盖

后端专项增加：

- continuation message 与 checkpoint CAS 的原子关联；
- 同名迟到 Worker 不产生重复 Run/message/event；
- checkpoint API 逐态字段、output digest 和 lineage；
- source failed 不被 continuation 成功改写。

ChatPage 增加：

- source failed → continuation running → succeeded 自动过渡；
- 刷新后从持久消息恢复相同正文；
- 完整性失败时区分最终接力错误和源错误；
- terminal output 不重复。

既有多代 generation 预算、Browser/lookup/file remaining-only、Artifact 完整性、取消和人工回答
测试继续全绿。

## 7. 验证记录

| 命令 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| 后端 Pytest | 258 passed in 27.89s |
| Alembic 空库升级 | `0001→0017` 通过 |
| 前端 TypeScript/ESLint/Vitest/build | 35 passed，production build 通过 |
| 进程级 E2E | 34 passed in 123.76s |
| `make browser-ui-check` | 2 passed in 20.53s |
| 真实 handoff UI 专项 | 1 passed in 9.81s |
| `git diff --check` | 通过 |

## 8. 完成矩阵

| 门禁 | 证据 |
|---|---|
| Normal flow | 既有探索、固化、发布、新对话复用和 RunDetail Chromium 场景继续通过 |
| Generic entry | Browser、lookup、file 自然入口进程级 E2E 全绿 |
| Source evidence | 真 503、有限重试、source failed 和 checkpoint pending 均保留 |
| Remaining-only | preflight 不重做，Browser 仅新增 attempt 2，外部计数 2→3 |
| Conversation linkage | continuation message 与 checkpoint CAS 原子提交，竞争只产生一条 |
| Result delivery | 原 Chat 自动显示 continuation output，刷新后仍一致且不重复 |
| Terminal failure | budget/integrity/cancel/awaiting_user 有稳定 chain status/code |
| Observability | source、checkpoint、continuation、generation、digest 和 lineage 可访问 |
| Real UI gate | 实际 API/Worker/Vite/动态站点/Chromium 完成 handoff |
| Quality | `demo-check`、`browser-ui-check`、`git diff --check` 全绿 |

