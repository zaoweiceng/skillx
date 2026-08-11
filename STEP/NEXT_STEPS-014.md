# NEXT_STEPS-014：把“场景恢复”升级为通配型计划接力

依据：[`REPLAY-013.md`](../REPLAY/REPLAY-013.md)、
[`review/design-compliance-review-014.md`](../review/design-compliance-review-014.md)、
[`review/code-review-014.md`](../review/code-review-014.md)。

范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 目标与最短路径

013 已闭合特定 Browser 场景的真实失败接力。下一步不应继续增加网页专用分支，而应解决两个根因：
**Host 没有完全拥有失败分类，checkpoint 没有冻结可执行的剩余计划。**

目标链：

```text
Agent 生成受限结构化计划
  → Host 执行并记录可信 step 状态
  → 失败 code/retryable 由 Host 决定
  → checkpoint 冻结 completed / failed / remaining plan
  → 新 Worker 校验 Artifact 与 PlanSnapshot
  → continuation 从 remaining steps 继续或显式受限 replan
  → 成功后沿用既有 distill/publish/L0/L1 链复用
```

## 2. A：收回失败状态机控制权

### A-01 Host 校验 `agent.failed`

- [ ] `agent.failed.retryable` 改为 advisory 或从协议删除；checkpoint 状态只使用 Host `ErrorPolicy`。
- [ ] 无 failed ToolResult 时，仅允许明确的 Agent runtime code；未知 code 规范化为非重试错误。
- [ ] Permission、Safety、Contract、budget、invalid event 即使自报 `retryable=true` 也必须 terminal。
- [ ] `callId` 存在时必须指向 Host 已见的 failed ToolResult；effective error 全取 ToolResult。
- [ ] API、事件和 checkpoint 区分 `reportedError` 与 `effectiveError`。

### A-02 有界恢复

- [ ] 参数化覆盖所有 ErrorPolicy code、alias、未知 code 和冲突的 Agent retryable。
- [ ] 验证 AgentHost、AgentRun、TaskCheckpoint 三层 effective error 一致。
- [ ] continuation 设置总 generation/attempt 上限，防止持续 transient 形成无限接力链。

完成条件：Agent payload 无法改变 retry、handoff 或权限决策。

## 3. B：持久化真正的 PlanSnapshot

### B-01 结构化计划协议

- [ ] 扩展 `agent.plan`：step 至少包含稳定 id、action/capability、依赖、参数引用、effect、
  idempotency key、输出名和允许的 fallback/replan 边界。
- [ ] Host 校验 capability、DAG、effect 与预算。
- [ ] 计划作为不可变 Artifact/JSON 持久化，记录 schema version 与 digest。
- [ ] Tool request 必须关联 plan step id；动态子步骤也要进入受限计划 revision。

### B-02 从可信事实计算断点

- [ ] checkpoint 用 PlanSnapshot 与 Host ToolResult 计算 `completedSteps/failedStep/remainingSteps`。
- [ ] remaining step 保存依赖、已解析/未解析输入、所需 Artifact 与副作用状态。
- [ ] source digest 覆盖 PlanSnapshot digest、step 状态和 Artifact 引用。
- [ ] 移除 `{"resumeSameTask": true}` 占位；旧 checkpoint 显式进入 `replan_required`。
- [ ] PlanSnapshot 或依赖 Artifact 损坏时 fail closed，不从原始 prompt 静默重做。

完成条件：只看 checkpoint 即可回答“已完成什么、失败在哪里、下一步是什么”。

## 4. C：统一 ContinuationRequest

- [ ] 删除 `traex_stub.py` 对 `[[MODE:browser_failure_handoff]]` 的恢复专用流程重建。
- [ ] 定义统一 `ContinuationRequest`：original input、effective error、completed outputs、failed output、
  remaining plan、capability locks、attempt/generation budget。
- [ ] 默认按 remaining DAG 执行；改变计划时返回 typed `ReplanPatch` 并生成新 PlanSnapshot revision。
- [ ] completed step 只能命中 ledger；failed 的 effect=none step 可重试；未知副作用必须先询问用户。
- [ ] continuation lineage 保存 source checkpoint、plan revision、replan patch 和最终运行。

完成条件：同一恢复实现可处理网页、MCP/lookup、文件和转换操作。

## 5. D：收紧 Artifact reconcile 活动边界

- [ ] `reconcile_recent_runs()` 默认只扫描 succeeded/failed/cancelled 等终态 Run。
- [ ] running Run 只处理数据库已登记且 owner lease 已过期的 Artifact。
- [ ] orphan staging GC 增加最小年龄，并核对 NodeRun status/worker/attempt/lease。
- [ ] Worker 启动 reconcile 不得删除另一个存活 Worker 正在写入的 staging。
- [ ] 增加两 Worker 交错测试：A 写 staging 后暂停，B reconcile，A 仍可提交并 promote。

## 6. E：修正并发 RevisionProposal 响应 lineage

- [ ] commit 后 response 的 proposal、digest、AgentRun、prompt、fixture、baseline 全部从
  `proposal_record` 构造。
- [ ] 冲突回读后不得混用败者请求的局部变量。
- [ ] 双 Session/双 HTTP 并发测试断言响应 identity 完全一致。
- [ ] 通过 `proposalId` 读取数据库记录，逐字段与响应核对。

## 7. F：通配型真实黑盒

保留现有 Browser 失败闭环，并让以下场景共用同一种 PlanSnapshot/ContinuationRequest：

### F-01 Browser → summarize

- [ ] 503/断连后接力、恢复、固化和第二次复用。
- [ ] 输入不得包含 Recovery fixture 专用 marker。

### F-02 lookup/MCP → transform

- [ ] 第一步成功、第二步瞬态失败、第三步未运行。
- [ ] 恢复只重试第二步并继续第三步，第一步命中 ledger。

### F-03 file/read → transform → output

- [ ] 中间步骤失败后重启 Worker，并从 remaining plan 继续。
- [ ] 更换路径和内容后二次复用，证明参数化而非缓存首次结果。

三条用例必须分别单独运行并连续执行至少 5 次；断言外部调用计数、PlanSnapshot digest、checkpoint
generation、source/continuation/version lineage 和最终新内容。

## 8. G：文档与门禁

- [ ] `docs/demo-acceptance.md` 定义 reported error 与 Host effective error 的优先级。
- [ ] 文档定义 PlanSnapshot、step 状态、replan revision 与 continuation budget。
- [ ] 明确“从原始 prompt 重新规划”不等于“从断点继续”。
- [ ] TraeX Adapter 文档要求生产实现输出结构化 plan/step id。
- [ ] 运行 `make demo-check`，记录实际退出码与测试计数。
- [ ] F-01/F-02/F-03 分别独立运行并做 5 次稳定性验证。
- [ ] 运行 `git diff --check`，确认无临时数据库、浏览器产物和 marker 状态文件入库。

## 9. 015 完成矩阵

| 门禁 | 必须证明 |
|---|---|
| Host-owned failure | Agent payload 不能把 terminal 错误改成 retryable |
| Plan persistence | checkpoint 含结构化 completed/failed/remaining steps |
| Plan integrity | PlanSnapshot、Artifact、source digest 加载前全部校验 |
| Generic continuation | Browser、MCP/lookup、文件/转换共用恢复实现 |
| No fixture routing | Recovery 不读取场景 marker 决定步骤 |
| No redo | completed action 在 Host 层复用；未知副作用先确认 |
| Bounded recovery | retry 与 continuation generation 有统一上限 |
| Artifact safety | 活动 staging 不被 reconcile 删除；crash staging 可恢复 |
| Proposal lineage | 并发响应逐字段来自同一持久 proposal |
| Existing closure | Replay proof、审核 draft、发布 version 继续 digest 一致 |
| Quality | 全量门禁全绿，三类关键黑盒独立且重复通过 |

在这些门禁完成前，准确表述是：**受控 Browser 失败闭环可运行；跨操作类型的通配型失败接力尚未完整
实现。**
