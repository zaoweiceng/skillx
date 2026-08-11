# REPLAY-013：闭合首次探索中的真实失败接力

日期：2026-08-05

依据：[`NEXT_STEPS-013.md`](../STEP/NEXT_STEPS-013.md)、
[`review/design-compliance-review-013.md`](../review/design-compliance-review-013.md)、
[`review/code-review-013.md`](../review/code-review-013.md)。

执行基线：`093ab31`。本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 结论

013 已闭合首次探索中的真实外部失败接力：

```text
Host Browser failed ToolResult
  → 统一 ErrorPolicy 有限重试
  → 保留 Tool code/message/retryable
  → failed ToolResult Artifact + pending TaskCheckpoint
  → 旧 Worker 崩溃
  → 新 Worker 校验 Artifact 并创建 continuation
  → completed-action ledger 阻止重做
  → 只重试失败 Browser action
  → 外部恢复后成功
  → distill / 人工 publish
  → 第二个新对话经 L0/L1 发现并复用
```

failed ToolResult 后即使 Agent 返回非法 `agent.completed`，或子进程提前退出，主错误也不再
被 `AGENT_EVENT_INVALID`/adapter error 覆盖；次生错误只进入 `protocolViolation` 审计字段。

## 2. 失败协议与统一策略

- Agent 事件协议新增结构化 `agent.failed(code/message/retryable/callId)`；
- `agent.completed` 继续只表示成功，输出必须通过 Schema 和逐字段 provenance；
- [`error_policy.py`](../backend/app/application/error_policy.py) 统一
  `DEPENDENCY_UNAVAILABLE/TIMEOUT/RATE_LIMITED` 的标准 code、retryable、budget、
  backoff 和 evidence kind；
- AgentHost 对只读 Tool 按策略有限重试；确定性 Contract/Permission/Safety/非法事件
  fail closed；
- Workflow Runtime、MCP、Tool Broker、AgentRun 与 TaskCheckpoint 使用同一分类，不再以
  `{"TIMEOUT", "EXECUTION_FAILED"}` 硬编码 checkpoint 状态。

真实失败公开事件保留脱敏后的标准 `code/message/retryable`。API、事件、ConversationRun 和
TaskCheckpoint 中的主 code 均为原始 Tool code。

## 3. Artifact、Checkpoint 与 Continuation

只读 ToolResult 无论成功或失败，均保存有界脱敏 Artifact。失败 Artifact 包含：

- call id、capability、arguments digest；
- 标准 error、evidence digest；
- action signature、effect/idempotent 和 result digest。

TaskCheckpoint 现在明确区分：

- `completedSteps` / `completedActionLedger`；
- `failedAction`；
- `pendingToolCalls`；
- `remainingPlan`。

source digest 覆盖 handoff 状态。Continuation 加载前校验 source/checkpoint digest，以及
Artifact 的 id、归属、路径、size、sha256 和 evidence digest；缺失、越界、损坏或非法 JSON
均 fail closed。成功只读 action 进入 Host ledger，失败 action 不进入 ledger，允许在外部恢复后
单独重试；未知副作用仍需人工确认。

## 4. Browser 分类稳定性

- Playwright `TimeoutError` 稳定映射为 `TIMEOUT/timeout`；
- 导航 timeout 至少比节点总 timeout 小 250ms，为结构化返回和 Context 清理留出窗口；
- 503、断连、DNS/连接失败映射为
  `DEPENDENCY_UNAVAILABLE/transport_error`；
- 页面/URL 契约失败保持 `PAGE_DRIFT`，契约输出失败保持
  `CONTRACT_VIOLATION/observed_contract_output`；
- Context/browser/playwright 清理按顺序执行，已有主异常时忽略关闭阶段次生异常。

E2E 中 503、断连和 timeout 各执行 5 次；分类与 evidence kind 全部稳定。

## 5. Artifact Reconcile 与幂等

新增共享安全路径和 `size + sha256` 校验：

- Workflow Run 与 ConversationRun 的 final/staging 均先校验 root；
- 损坏 final 有有效 staging 时恢复，否则删除并报告 `corrupt`；
- 越界 storage/staging key 报告 `pathRejected`；
- `promote_staged_artifact()` 仅在 target 内容匹配时视为幂等成功；
- 报告统一包含 `recovered/missing/corrupt/pathRejected/orphanRemoved`。

RevisionProposal 唯一索引冲突会 rollback 后回读同内容 proposal；无法回读时返回结构化
`REVISION_PROPOSAL_CONFLICT`，不泄漏 `IntegrityError`。no-op draft 回填缺失 digest 时会提交，
避免 API 响应与数据库不一致。既有双 Session/双 HTTP publish 幂等测试保持通过。

## 6. 公开黑盒 F

新增独立用例
`test_real_tool_failure_handoff_distill_and_second_reuse`：

1. Conversation A 执行 preflight lookup 后访问真实 Browser 503；
2. Browser 由 Host 调用两次，重试耗尽后记录 failed ToolResult；
3. 源 Run 以 `DEPENDENCY_UNAVAILABLE` 失败，checkpoint 为 `pending`；
4. 旧 Worker 崩溃，外部服务切为正常，新 Worker 领取 generation 1；
5. continuation 的 preflight lookup 命中 ledger，外部调用不增加；
6. 只重试 Browser，返回“恢复后的正文”；
7. continuation 成功轨迹生成草稿并人工发布；
8. Conversation B 以新 URL/新正文自然语言请求同类任务；
9. L0/L1 选择发布版本，Runtime 返回新正文；
10. source、checkpoint、continuation、distillation、version 和第二次 Run lineage 连续。

该用例使用 function-scoped 独立临时数据库、Artifact root、API、Worker 和 Browser 服务。
五个独立栈并行重复结果为 `5/5 passed`：
`9.73s / 9.15s / 8.82s / 8.98s / 9.17s`。

## 7. 验证记录

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `make demo-check` | 0 | 全量门禁通过 |
| `cd backend && uv run pytest -q` | 0 | 230 passed |
| `cd backend && uv run pytest e2e -q -p no:cacheprovider` | 0 | 22 passed |
| Alembic 空库升级 | 0 | `0001→0017` |
| 前端 TypeScript/ESLint/Vitest/build | 0 | 28 passed，production build 通过 |
| 主复用独立专项 | 0 | 1 passed in 5.31s |
| Browser revision/transport/timeout 专项 | 0 | 1 passed in 24.19s；三类错误各重复 5 次 |
| pending checkpoint restart 独立专项 | 0 | 1 passed in 5.93s |
| 新 F 独立专项 | 0 | 1 passed；额外独立重复 5/5 passed |
| Agent/Artifact/Browser 核心专项 | 0 | 54 passed |
| MCP/Revision/Tool 专项 | 0 | 29 passed |
| `git diff --check` | 0 | 无空白错误 |

最终 `make demo-check` 的实际计数：

- Ruff：all checks passed；
- 后端：230 passed in 25.13s；
- 前端：7 files / 28 tests passed；
- E2E：22 passed in 56.36s。

## 8. 完成判定

NEXT_STEPS-013 的 A-G 在 Local Demo 范围内完成。主复用、真实 Tool 失败保真、统一有限重试、
跨 Worker 接力、不重做、恢复后固化、第二次渐进复用、Browser 分类稳定性和两类 Artifact
完整性均有独立测试与全量门禁证据。

未完成项：无（Local Demo 范围内）。
