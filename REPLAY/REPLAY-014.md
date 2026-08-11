# REPLAY-014：通配型计划接力

日期：2026-08-05

依据：[`NEXT_STEPS-014.md`](../STEP/NEXT_STEPS-014.md)、
[`review/design-compliance-review-014.md`](../review/design-compliance-review-014.md)、
[`review/code-review-014.md`](../review/code-review-014.md)。

执行基线：`4802aa8`。本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 结论

014 将 013 的 Browser 专用恢复分支收敛为通配型计划接力：

```text
结构化 agent.plan
  → Host 校验 capability / DAG / effect / budget
  → 不可变 PlanSnapshot Artifact
  → Host ToolResult 计算 completed / failed / remaining
  → checkpoint 校验 plan / step / Artifact / source digest
  → 统一 ContinuationRequest
  → Host ledger 复用 completed，只执行 remaining DAG
  → Browser、lookup/transform、file/transform 共用同一实现
```

`agent.failed.retryable` 不再控制状态机；API、事件和 checkpoint 区分
`reportedError` 与 Host `effectiveError`。Browser 恢复不再读取
`[[MODE:browser_failure_handoff]]` 重建流程。

## 2. Host-Owned Failure

- Agent 自报的 code/message/retryable 只形成 `reportedError`；
- Host 通过共享 `ErrorPolicy` 规范化 alias，并独立生成 `effectiveError`；
- Permission、Contract、budget、invalid event 和未知 code 即使自报 retryable 也保持 terminal；
- `callId` 存在时必须指向 Host 已见的 failed ToolResult，effective error 全取 ToolResult；
- AgentHost、AgentRun、ConversationRun API 与 TaskCheckpoint 使用同一 effective code/retryable；
- continuation generation 默认总上限为 3，达到上限后 checkpoint 进入 terminal。

参数化测试覆盖 transient code、alias、terminal code、未知 code、冲突 retryable 和伪造 callId。

## 3. PlanSnapshot 与断点

新增 Agent PlanSnapshot 协议，step 包含：

- 稳定 id、action、capabilityRef 和 dependsOn；
- argumentRefs、effect、idempotencyKey 和 outputName；
- retry/replan/abort/ask_user fallback 边界。

Host 校验 step 唯一性、能力锁、DAG 无环、effect 与 Adapter 契约及 step budget。结构化计划下，
每个 `tool.requested` 必须关联 `planStepId`。计划按 revision 保存为
`agent_plan_snapshot` Artifact，并记录 schema version、digest、size 和 sha256。

checkpoint 只从 PlanSnapshot 与 Host ToolResult 计算：

- `completedSteps`；
- `failedStep` / `failedAction`；
- `remainingPlan.steps`；
- `completedActionLedger` 和 pending calls。

source digest 覆盖 PlanSnapshot 引用、step 状态和 ToolResult Artifact 引用。加载前校验 Artifact
归属、路径、size、sha256、evidence digest 与 plan digest。缺少结构化计划的旧运行标记为
`replan_required`；计划损坏标记为 `integrity_failed`，不得从原 prompt 静默重做。

## 4. ContinuationRequest 与 Replan

恢复入口统一接收 `ContinuationRequest`：

- original input；
- reported/effective error；
- completed outputs / failed output；
- remaining plan 与 capability locks；
- attempt/generation/step/token budget；
- 一次性 user answer。

默认执行冻结 remaining DAG。completed action 只能命中 Host ledger；effect=none 的失败步骤可重试；
未知副作用仍要求用户确认。`summarize.text` 也进入 Tool Broker，Browser/file 的转换步骤均产生可信
ToolResult，不由 fixture 直接伪造终态。

新增 typed `agent.replan` / `ReplanPatch`：必须匹配 base plan digest/revision，只能替换 remaining
steps，Host 重新校验并持久化 revision+1 PlanSnapshot。无计划的 `UNKNOWN_OUTCOME` 只有在用户明确
确认外部状态后才允许受限 replan。

## 5. Reconcile 与 Proposal Lineage

- `reconcile_recent_runs()` 默认只扫描 succeeded/failed/cancelled Workflow Run；
- 两 Worker 交错测试证明 A 写入但未登记的活动 staging 不会被 B 的启动 reconcile 删除；
- 终态 Run 的已登记 staging 仍可恢复，孤儿文件仍按既有完整性规则清理；
- RevisionProposal commit 后，响应的 proposal id、AgentRun、prompt、fixture、baseline、JSON 与
  digest 全部从持久化 `proposal_record` 构造；
- 冲突回读后重新从持久 proposal 编译 diagnostics，不再混用败者局部变量；
- Workflow 校验 diagnostics 统一序列化，错误返回 422，不再因 `Diagnostic` 对象触发二次 500。

## 6. 三类真实黑盒

### Browser → summarize

真实 Browser 503，Host 有限重试两次后生成 pending checkpoint。新 Worker 恢复后：

- preflight 命中 ledger；
- Browser 只新增一次外部调用；
- summarize 由 Host Tool Broker 执行；
- continuation 成功后可 distill、人工 publish；
- 第二个新 Conversation 经 L0/L1 发现版本，并用新 URL/正文返回新内容。

输入使用普通 `planned_browser_summary` 计划模式，不包含 Recovery 专用 marker。

### lookup → transform → output

lookup 成功后模拟 transform 瞬态失败。checkpoint 固定：

- completed=`lookup`；
- remaining=`transform/output`；
- continuation 中 lookup 命中 ledger，剩余两步按 DAG 执行；
- 最终输出来自第三步新 ToolResult。

### file/read → transform

read.local 成功后模拟 transform timeout。Worker 重启后：

- read 命中 Artifact ledger；
- summarize.text 执行 remaining transform；
- 输出来自参数化文件内容，不缓存首次固定结果。

三类场景分别使用独立临时数据库、Artifact root、API、Worker 和外部服务，连续运行 5 次，结果均为
`5/5 passed`。最终全量 E2E 为 `24 passed`。

## 7. 验证记录

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `make demo-check` | 0 | 全量门禁通过 |
| `cd backend && uv run pytest -q` | 0 | 253 passed in 25.75s |
| Alembic 空库升级 | 0 | `0001→0017` |
| 前端 TypeScript/ESLint/Vitest/build | 0 | 28 passed，production build 通过 |
| `cd backend && uv run pytest e2e -q -p no:cacheprovider` | 0 | 24 passed in 69.34s |
| Browser continuation 独立重复 | 0 | 5/5 passed |
| lookup continuation 独立重复 | 0 | 5/5 passed |
| file continuation 独立重复 | 0 | 5/5 passed |
| `git diff --check` | 0 | 无空白错误 |

## 8. 完成矩阵

| 门禁 | 证据 |
|---|---|
| Host-owned failure | reported/effective 分离；冲突 retryable 与伪造 callId fail closed |
| Plan persistence | 不可变 PlanSnapshot Artifact + schema/revision/digest |
| Plan integrity | plan、step 状态、Artifact refs 进入 source digest并加载校验 |
| Generic continuation | Browser、lookup、file 共用 ContinuationRequest |
| No fixture routing | 删除 Browser Recovery marker 分支 |
| No redo | completed action 由 Host ledger 复用 |
| Bounded recovery | generation 总上限 + claim generation/token CAS |
| Artifact safety | recent reconcile 只扫终态；活动 staging 交错测试通过 |
| Proposal lineage | 响应全部来自持久 proposal_record |
| Existing closure | Browser 恢复后 distill/publish/L0/L1 第二次复用通过 |
| Quality | 全量门禁与三类 5 次稳定性门禁通过 |

NEXT_STEPS-014 的 A-G 在 Local Demo 范围内完成。准确表述更新为：
**跨 Browser、lookup/MCP 类只读操作和文件/转换操作的通配型失败接力已实现并通过真实黑盒。**
