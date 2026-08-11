# 代码审查报告（014）

审查日期：2026-08-05

代码基线：`4802aa8d1b14172e6ad34632d4c9bce5146fbd0e`；审查开始时工作区 clean，本轮新增
014 三份审查文档。

审查范围：`REPLAY-013.md`、Revision/Replay/publish、Agent 事件与 Host、Tool Broker、AgentRun
TaskCheckpoint、Task recovery、Artifact reconcile、Playwright、真实 E2E。Local Demo 不考虑鉴权。

## 1. 总结

上版报告指出的真实 failed ToolResult 覆盖、统一错误策略、失败 Artifact、Worker 接力、Browser timeout
分类和两类 Artifact 完整性问题，均已在主路径修复。全量门禁与目标真实场景通过。

本轮没有 P0，发现 **2 个 P1、2 个 P2**。两个 P1 都指向通配型失败接力的根因：Host 没有完全拥有
失败决策，checkpoint 也没有保存可执行的剩余计划。当前真实 E2E 能通过，但依赖仓库 fixture 对特定
marker 的流程重建，不能外推到任意操作。

## 2. P1

### P1-1 — Agent 可覆盖 Host 的 retryable 决策，使 fail-closed 错误进入自动接力

[`agent_host.py`](../backend/app/application/agent_host.py) 在无 failed ToolResult 的 `agent.failed` 分支中，
把 Agent payload 的 `retryable` 原样传给 `AgentHostError`；其构造器在显式值存在时覆盖共享
`ErrorPolicy`。[`agent_run.py`](../backend/app/application/agent_run.py) 随后直接按该布尔值创建 pending 或
terminal checkpoint。

最小反例已执行：

```text
Host policy: PERMISSION_DENIED.retryable = false
Agent self report: code=PERMISSION_DENIED, retryable=true
AgentHostError.accepted_retryable = true
```

影响：TraeX 子进程属于不可信执行边界。它可把权限、Safety、Contract 或未知错误伪装成 transient，
驱动无意义的自动 continuation、消耗资源，并违反验收规范“Permission/Safety/Contract/非法事件
fail closed”。虽然 Host 不因此授予新能力，但失败状态机的控制权已经泄漏给 Agent。

修复：`agent.failed.retryable` 只能作为诊断提示，不能控制状态。Host 必须对 code 做 allowlist、别名规范化
和 `error_policy(code)` 重分类；若 `callId` 指向 failed ToolResult，则完全采用 Tool error；若没有 Host
可信证据，只有明确允许的 Agent 运行时错误可进入 pending。增加每个 fail-closed code 携带
`retryable=true` 仍为 terminal 的参数化测试。

### P1-2 — checkpoint 没有保存剩余节点，真实接力用例依赖 fixture marker 重建流程

[`agent_run.py`](../backend/app/application/agent_run.py) 创建 checkpoint 时虽然读取全部事件，却只从
Tool 事件计算 completed/failed/pending call id；`remainingPlan` 被固定写成：

```json
{"resumeSameTask": true}
```

`agent.plan` 的 summary/steps 没有进入冻结 source digest，更没有节点 id、依赖、输入引用或副作用。
[`traex_stub.py`](../examples/traex_stub.py) 的恢复分支则检查原始文本中的
`[[MODE:browser_failure_handoff]]`，硬编码重新发起 lookup 和 browser 调用。于是 E2E 的成功来自 fixture
知道这个场景，而不是 Recovery Agent 从 checkpoint 获得“之后需要完成的节点信息和内容”。

影响：换成未编码 marker 的网页流程、MCP→文件→转换流程，或在失败后存在分支/副作用时，continuation
只能重新规划整个任务；它可能遗漏后续步骤、重复失败前的非 ledger 动作，或选择与首次计划不同的路径。
这直接阻断用户要求的通配型失败接力。

修复：把 Agent 计划规范化为可持久的 PlanSnapshot，至少含 step id、capability/action、依赖、参数引用、
effect、状态和允许的重规划边界。checkpoint 从计划与可信 Tool 事件计算 completed/failed/remaining，digest
覆盖完整快照；Recovery Agent 必须消费该快照。没有结构化计划时应显式进入受限 replan 状态，不得把
`resumeSameTask=true` 描述为断点续做。测试应移除 marker 分支，并用至少三类流程复用同一恢复实现。

## 3. P2

### P2-1 — reconcile 扫描所有 Workflow Run，会删除活动节点尚未登记的 staging

[`reconcile_recent_runs()`](../backend/app/application/orchestrator.py) 查询最近 Run 时没有终态过滤；
`reconcile_run_artifacts()` 在处理完数据库已有 Artifact 后，会无条件删除 `.staging` 下剩余文件。
而正常 Executor 是“先在节点执行阶段写 staging，后在事务中登记 Artifact，再 commit/promote”。

单 Worker 同一事件循环通常不会并发触发，但本地 Worker 重启短暂重叠、误启动第二个 Worker，或未来
部署多个 Worker 时，reconcile 可在活动节点完成前删除它刚写出的 staging。随后节点提交 Artifact 记录，
promote 找不到 source，最终留下数据库记录但文件缺失。

修复：最近 Run 只自动 reconcile terminal 状态；对 running Run 必须校验 NodeRun 已终态或租约过期，且
只处理数据库已登记的 staging。孤儿 GC 应有年龄阈值和 owner/lease fencing。增加两个 Worker 交错测试：
一个暂停在“写 staging、未登记”，另一个执行 reconcile，文件不得被删除。

### P2-2 — 并发相同 RevisionProposal 时，响应会混用胜者 proposalId 与败者 agentRunId

[`_commit_revision_proposal()`](../backend/app/api/routes.py) 在唯一索引冲突后正确回读已存在 proposal，
但 API 响应只有 `proposalId` 取自回读记录；`agentRunId/promptVersion/fixtureDigest/proposal/proposalDigest`
仍取自本次失败的局部变量。

两个并发请求生成相同 proposal digest 时，败者响应可能是：

```text
proposalId = 胜者持久记录
agentRunId = 败者本次 AgentRun
```

调用方若保存该响应，会得到无法从数据库还原的 lineage，违背 REPLAY-012 要求的不可变 proposal 来源。

修复：commit 后响应的所有 identity 与 proposal 字段都从 `proposal_record` 读取；必要时重新编译
`proposal_record.proposal_json` 生成 diagnostics。双 Session/双 HTTP 并发测试必须断言两个响应逐字段
一致，且每个 `agentRunId` 都等于其 `proposalId` 所指记录。

## 4. 已确认的有效修复

- `agent.failed` 已加入结构化事件；Host 已知 failed ToolResult 时保留 Tool 主错误。
- `DEPENDENCY_UNAVAILABLE/TIMEOUT/RATE_LIMITED` 使用共享有限 retry budget/backoff。
- failed 和 succeeded 的只读 ToolResult 都保存有界 Artifact；checkpoint 加载校验 owner、id、路径、
  size、sha256 与 evidence digest。
- 新 Worker 可领取 pending checkpoint；source Run 保持 failed，continuation lineage 独立。
- completed-action ledger 在 Host 执行层阻止重复只读调用。
- Playwright 导航 timeout 小于节点总 timeout；503/断连/timeout 的重复分类用例通过。
- Workflow 与 Conversation Artifact 都进行 root/size/hash 校验；损坏 final 可由有效 staging 恢复。
- Replay proof、审核 draft 和发布版本 digest 继续保持绑定；旧 proof 不可为编辑后的定义背书。
- proposal 唯一约束冲突已不再泄漏裸 IntegrityError；剩余问题仅是响应 lineage 混用。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 | 230 passed in 26.59s |
| Alembic | `0001→0017` 通过 |
| 前端 | 28 passed；TypeScript/ESLint/build 通过 |
| 全量黑盒 E2E | 22 passed in 56.99s |
| 独立全量 E2E 复跑 | 22 passed in 57.86s |
| failure handoff + main Browser reuse + revision 专项 | 3 passed in 35.79s |
| policy 反例 | `PERMISSION_DENIED: host_policy=false, accepted_retryable=true` |
| `git diff --check` | 写文档前通过 |

修复顺序与验收标准见 [`NEXT_STEPS-014.md`](../STEP/NEXT_STEPS-014.md)。
