# 代码审查报告（018）

审查日期：2026-08-07

代码基线：`9ec1246d17636dcdb5ced2cab4bcc9dbbe0c1c3a`；审查差异为 `0a2237f..9ec1246`。
范围为 `REPLAY-017.md`、TaskCheckpoint recovery、Conversation message、checkpoint API、ChatPage 和真实
浏览器门禁。Local Demo 不考虑鉴权。

## 1. 结论

**通过。没有发现新的 P0、P1 或 P2 代码问题。**

017 报告中的 1 个 P1、2 个 P2 均已按根因闭合：

- continuation 不再只存在于 checkpoint 私有状态，而是有持久 Conversation message；
- source failed 后 Chat 改由 checkpoint 链继续跟踪；
- checkpoint API 暴露终态、output digest 和多代 lineage；
- 浏览器门禁真正执行 503、Worker 更换和原 Chat 自动回显。

真实运行、全量测试和代码语义一致，没有发现需要进入下一修复版本的功能性 finding。

## 2. 关键代码审查

### 2.1 continuation 与消息原子关联

[`task_recovery.py`](../backend/app/application/task_recovery.py) 在同一事务内：

1. 从已验证 checkpoint 构造受限 continuation；
2. 写入 source/root/checkpoint/generation lineage；
3. 创建关联 continuation 的 assistant message；
4. 使用 owner/generation/token CAS 将 checkpoint 从 `running` 改为 `continuing`；
5. CAS 失败时删除本事务创建的 message 和 Run。

这保留了 source failure，同时让 continuation 能从 Conversation 持久发现。现有并发 claim 测试断言竞争下只有
一个 continuation Run、一个 message 和一个 handoff started 事件。

### 2.2 链式终态

`_reconcile_continuation()` 保存 continuation status/output digest/error 和 child checkpoint；多代失败通过
`sourceCheckpointId/rootSourceRunId/childCheckpointId` 串联，不覆盖上一代失败。

[`routes.py`](../backend/app/api/routes.py) 的 checkpoint API 有 visited fencing，沿 continuation 的 child
checkpoint 投影链末端状态，区分 succeeded、continuing、awaiting_user、cancelled、budget exhausted、
integrity failed 和普通 failed。output 本体仍从可信 ConversationRun 读取，没有复制到 checkpoint。

### 2.3 Chat 状态机

[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx) 从持久 messages 收集 AgentRun，再从 source error/message
收集 checkpoint。source failed 后不提前渲染为最终错误，而是有界轮询 checkpoint 链和 Conversation：

- pending 显示“等待接力”；
- continuation 创建后自动发现其 message/run；
- terminal 后停止 checkpoint 轮询；
- succeeded 只在 continuation message 显示 output；
- source 卡片保留失败事实和 lineage；
- reload 从服务端消息和 Run 恢复，不依赖临时结果 state。

本轮真实浏览器观测到从“等待接力”自动转为“恢复后的正文”，reload 后正文仍只有一份，说明状态机没有重复
message 或无限轮询问题。

### 2.4 真实门禁

[`test_ui_main_flow.py`](../backend/browser_e2e/test_ui_main_flow.py) 新场景使用真实 Vite、API、动态站点、两代
Worker 和 Chromium，覆盖 source pending 边界、站点恢复、remaining-only、attempt digest、唯一 message、
自动回显、刷新恢复和 console。原 Workflow `INVALID_INPUT` 场景仍作为独立失败详情测试，不再冒充 Agent handoff。

## 3. 017 finding 关闭情况

| 017 finding | 018 状态 | 证据 |
|---|---|---|
| P1-1 continuation 不回原 Conversation | closed | 唯一持久 message；原 Chat 自动显示 continuation output |
| P2-1 checkpoint API 缺少 continuation 投影 | closed | status/digest/chain error/lineage 均返回 |
| P2-2 浏览器门禁测试错误路径 | closed | 新增真实 Agent 503 handoff 场景，2 条 UI E2E 通过 |

## 4. 非阻塞文档卫生

当前工作区存在一处不影响运行的编号状态不一致：

- Git 中 `REPLAY-018.md` 显示删除；
- 工作区新增未跟踪 `REPLAY-017.md`；
- `REPLAY-017.md` 的一级标题仍是“REPLAY-018”；
- 该文档末尾多一个空行，`git diff --check 0a2237f..9ec1246` 会报告
  `REPLAY-018.md:145: new blank line at EOF`。

这不是代码或闭环阻塞，不降低本轮“通过”结论，但应在提交 018 文档前统一文件名、标题和 Git 索引，避免后续
审查引用错位。本轮遵守工作区保护要求，没有擅自修改或回退这组用户改动。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| 后端 | Ruff 通过；258 passed in 28.95s |
| 前端 | TypeScript/ESLint/build 通过；35 tests passed |
| Alembic | 空库 `0001→0017` 通过 |
| 进程级 E2E | 34 passed in 130.43s |
| 真实浏览器 E2E | 2 passed in 18.98s |
| 手工真实正常链 | passed |
| 手工真实 503 接力链 | passed；自动回显、reload、唯一结果均成立 |
| 浏览器 console | 0 error |

## 6. 最终判定

从原始需求看，系统现在能把执行事实、故障恢复和最终用户答复连成同一条持久链。**代码审查通过。**
