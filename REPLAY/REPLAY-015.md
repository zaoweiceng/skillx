# REPLAY-015：冻结计划成为 Host 执行事实

日期：2026-08-06

依据：[`NEXT_STEPS-015.md`](../STEP/NEXT_STEPS-015.md)、
[`review/design-compliance-review-015.md`](../review/design-compliance-review-015.md)、
[`review/code-review-015.md`](../review/code-review-015.md)。

执行基线：`c57de5c`（`feat: 实现通配型计划接力功能`）。本报告对应当前未提交工作树；
本轮未创建 Git 提交。

范围：Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 结论

015 将 014 的“PlanSnapshot 持久化与提示”升级为 Host 强制执行事实：

```text
verified PlanSnapshot + step states + Artifact outputs
  → ExpectedPlanContract
  → Host 校验 continuation 首个 agent.plan
  → Host 物化并核对 Tool arguments
  → plan digest + step id + attempt 状态机
  → 仅允许绑定 source 的 ReplanPatch
  → 有界 continuationGeneration
```

continuation Agent 不能再换 URL、文件路径、lookup key、计划顺序或重复 succeeded step。所有偏离均在
Tool Adapter 执行前 fail closed。

## 2. Expected Plan Binding

`AgentHostRequest` 新增进程内可信 `ExpectedPlanContract`，包含：

- source PlanSnapshot、digest 和 revision；
- source checkpoint id；
- allowed remaining step ids；
- step states、全部 attempts 与 step-state digest；
- original input、已验证 step/Artifact outputs；
- continuation generation 与 legacy replan 标记。

TaskRecovery 只从已校验的 PlanSnapshot/ToolResult Artifact 构造 contract，不从 prompt 反向解析可信字段。
continuation 的首个 `agent.plan` 必须与 expected plan 完全一致；新 planId、revision、step 顺序、依赖或
内容变化均拒绝。成功轨迹保存实际 accepted plan digest/revision 和 step attempts lineage。

## 3. Host 参数权威

`argumentRefs` 改为严格判别联合，禁止扩展字段：

- `literal(value)`；
- `user_input(inputKey, extract=url)`；
- `step_output(stepId, path)`；
- `artifact(artifactId, path)`。

Host 从 original input 和已验证 ToolResult/Artifact outputs 物化 arguments，并逐字段比较：

- action/capability；
- effect/idempotencyKey/idempotent；
- plan digest/revision、step id/attempt；
- 完整 arguments。

URL A 改为 B、`input.txt` 改为 `different.txt`、lookup q2 改为 q4，均返回
`PLAN_ARGUMENT_MISMATCH`。Browser 偏离目标的外部 `alternateCount` 保持 0；偏离请求不产生
对应 `tool.result`。

## 4. Host-Owned Step State

每个 plan revision 的 step 显式保存：

- `remaining/running/succeeded/failed/unknown/skipped`；
- 以 `planDigest + stepId + attempt` 标识的全部 attempts；
- callId、arguments digest、终态和结构化 error。

依赖只认可 Host 已提交的 `succeeded`。succeeded step 不再由 continuation 重发命中 ledger，而是直接作为
Host 依赖事实；重发返回 `PLAN_STEP_ALREADY_SUCCEEDED`。failed step 只有在 fallback=retry、ErrorPolicy
允许、attempt 未超限、effect=none 且 Adapter idempotent 时才可进入下一 attempt。checkpoint 不再按
planStepId 用最后事件覆盖历史。

## 5. Replan 与 Legacy

ReplanPatch 必须绑定：

- source checkpoint id；
- base plan digest/revision；
- 当前 step-state digest；
- replaceStepIds 与 replacementSteps。

replaceStepIds 只能属于 remaining/failed closure，不能替换 completed step。replacement 使用与初始 plan
相同的 capability/DAG/effect/argumentRefs/budget 校验，并在新 step 执行前持久化 revision+1
PlanSnapshot Artifact。同一 patch digest 重复提交幂等返回同一 revision，不同 patch 仍按
base/state digest 冲突拒绝。公开黑盒证明直接换 plan 失败，而合法 patch 连续提交两次均指向
revision 2，并只生成同一内容 Artifact。

无结构化计划的 transient failure 直接进入 `awaiting_user`，保存 pendingQuestion、effective error、
Artifact、capability locks 和 replan budget。answer API 通过 snapshot digest CAS 一次性确认后转 pending，
第二次回答返回 409；Host 随后生成 legacy lineage 的 revision 1 PlanSnapshot 并完成。

## 6. Generation 与错误终态

两个 generation 已明确分离：

- `claimGeneration`：同一 TaskCheckpoint 的 lease fencing；Worker crash/reclaim 才增长；
- `continuationGeneration`：跨 ConversationRun 的业务接力代数；创建下一代 continuation 才增长。

claim 前只按 continuationGeneration 检查总预算。达到 3 后 CAS 写 terminal、`budgetExhausted` 和
`agent.handoff.budget_exhausted`。三次 Worker reclaim 测试中 claimGeneration=3，而业务代数仍为 0。

`start_continuation()` 的计划、完整性或预算错误由 Driver 捕获，并使用 owner/generation/token CAS 写入
terminal `continuationError` 与 `agent.handoff.rejected`，不遗留 running lease。

## 7. 正向与反向黑盒

### 既有成功链

- Browser 503 → Host 重试 → checkpoint → Browser/summarize remaining → distill/publish →
  新 Conversation L0/L1 复用新正文；
- lookup → transform → output；
- file/read → transform。

三类继续共用 ExpectedPlanContract/ContinuationRequest，独立专项 `3 passed`。

### 参数偏离

- Browser URL 改为 `/alternate`：Host 拒绝，访问计数 0；
- file 计划路径改为 `different.txt`：Host 拒绝，无 read ToolResult；
- lookup q2 改为 q4：Host 拒绝，无 q4 ToolResult。

三类合并专项连续运行 5 轮，每轮 `3 passed`，合计 15/15。

### 计划、重复与预算

- 全新 plan、跳依赖均 fail closed；
- succeeded step 使用新 callId 重发，连续 5/5 返回结构化拒绝；
- 合法 ReplanPatch 生成 revision 2 Artifact；
- 三代 continuation 连续失败后 terminal，连续 5/5 稳定；
- 同代 Worker reclaim 三次不消耗业务代数。

### Legacy

- transient legacy checkpoint 为 awaiting_user；
- 一次确认后 revision 1 replan 成功；
- 第二次回答返回 409。

所有反向场景均通过公开 API、独立 Worker、持久 SQLite 和 Artifact root 执行。

## 8. 验证记录

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `make demo-check` | 0 | 全量门禁通过 |
| `cd backend && uv run pytest -q` | 0 | 254 passed in 26.79s |
| Alembic 空库升级 | 0 | `0001→0017` |
| 前端 TypeScript/ESLint/Vitest/build | 0 | 28 passed，production build 通过 |
| `cd backend && uv run pytest e2e -q -p no:cacheprovider` | 0 | 33 passed in 118.40s |
| Browser/lookup/file 正向专项 | 0 | 3 passed in 20.69s |
| legacy/replan/三代预算专项 | 0 | 3 passed in 18.32s |
| 参数偏离稳定性 | 0 | 5 轮，每轮 3 passed |
| 重复 succeeded step 稳定性 | 0 | 5/5 passed |
| 三代 continuation 失败稳定性 | 0 | 5/5 passed |
| `git diff --check` | 0 | 无空白错误 |

## 9. 完成矩阵

| 门禁 | 证据 |
|---|---|
| Expected plan binding | HostRequest 绑定 source digest/revision/remaining/state digest |
| Argument authority | Host 物化并逐字段比较四类 argumentRefs |
| Replan-only mutation | 新 plan 拒绝；绑定 source 的 patch 才产生 revision 2 |
| Step state | 全部状态与 attempts 可审计；succeeded 禁止重做 |
| Legacy recovery | awaiting_user + 一次性 CAS + revision 1 |
| Bounded chain | chain generation 与 lease claim 分离，三代稳定终止 |
| Negative Browser | 偏离 URL 的外部访问计数为 0 |
| Negative file/lookup | 偏离 path/key 不产生 ToolResult |
| Existing success | Browser、lookup、file 三类继续通过 |
| Existing closure | distill/publish/L0/L1 与 Replay 证据链无回归 |
| Quality | 全量门禁、正向与反向黑盒全部通过 |

NEXT_STEPS-015 的 A-G 在 Local Demo 范围内完成。准确表述更新为：
**冻结 PlanSnapshot 已成为 continuation Host 的强制执行事实。**
