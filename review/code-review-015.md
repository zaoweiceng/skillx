# 代码审查报告（015）

审查日期：2026-08-06

代码基线：`c57de5c2a762e6f1ec5e0798181e840c7cb70245`；审查开始时工作区 clean，本轮只新增
015 三份审查文档。

审查范围：`REPLAY-014.md`、Agent plan/Host/Tool Broker、AgentRun Artifact、TaskCheckpoint recovery、
Artifact reconcile、RevisionProposal、真实 E2E 与相关设计文档。Local Demo 不考虑鉴权。

## 1. 总结

014 修复了上一轮的四个直接问题：Agent 自报 retryable 不再覆盖 Host policy；结构化 PlanSnapshot 与
completed/failed/remaining 已落库；recent reconcile 不再扫描活动 Run；proposal response 已统一来自
持久记录。全量测试和三类真实专项均通过。

本轮没有 P0，发现 **2 个 P1、2 个 P2**。核心 P1 是执行权仍在 continuation Agent：平台验证并保存了
计划，但没有让 Host 按该计划生成或验证实际 Tool 调用。第二个 P1 是无计划任务进入一个无法回答的
`replan_required` 状态。

## 2. P1

### P1-1 — continuation 没有绑定 source PlanSnapshot，可无 ReplanPatch 改步骤和参数

[`task_recovery.py`](../backend/app/application/task_recovery.py) 校验 PlanSnapshot Artifact 后，只把
`remainingPlan` 序列化进 Recovery prompt；创建 continuation 时没有把 expected plan digest、revision 或
remaining step set 作为 Host 配置。`sourcePlanSnapshot` 虽写入 `ConversationRun.input_json`，
[`agent_run.py`](../backend/app/application/agent_run.py) 构造 `AgentHostRequest` 时并未使用它。

因此 continuation 可发送一个全新的 `agent.plan`。Host 会把它当 revision 1 的合法初始计划，而不是要求
基于 source plan 的 `agent.replan`。即使 Agent 保留相同 step id，
[`_validate_planned_tool_request()`](../backend/app/application/agent_host.py) 也只检查：

- planStepId 存在；
- capabilityRef 相同；
- dependsOn 已完成。

它不检查 request action、arguments 与 `argumentRefs`、effect/idempotencyKey，也不比较 source plan
digest/revision。实际反例中，计划声明 `read.local(path=safe.txt)`，Agent 请求
`action=delete, path=different.txt`，校验返回成功。

影响：所谓“冻结 remaining DAG”目前由 TraeX-compatible fixture 自觉遵守，而不是 Host 保证。真实
TraeX 若误解、受提示注入或主动偏离，可换 URL/文件/key、跳过节点或替换整个 remaining plan，并产生
与 checkpoint lineage 不一致的成功结果。这直接破坏通配型断点续做的核心不变量。

修复：TaskRecovery 应把校验后的 source PlanSnapshot/remaining state 作为受信参数传给 AgentHost。
默认 continuation 由 Host materialize 每个 step 的 arguments；若仍让 Agent发 ToolRequest，必须逐字段
比较 plan-derived arguments、action、effect 与 idempotency key。任何步骤变化只接受绑定 source
digest/revision 的 ReplanPatch，生成不可变 revision+1 后才能执行。

### P1-2 — `replan_required` 要求用户确认，但 checkpoint 没有可达的回答状态

无结构化计划时，[`_create_task_checkpoint()`](../backend/app/application/agent_run.py) 把
`remainingPlan.mode` 设为 `replan_required`，但 checkpoint status 仍沿用错误策略：transient 为 pending。
TaskCheckpointDriver 会领取 pending 并改为 running；`_load_plan_snapshot()` 随后要求
`userAnswer.continue=true`，否则抛 `TaskCheckpointError`。

公开 answer API 只接受 status=`awaiting_user`。所以用户不能在领取前回答，领取后也不能回答。Worker
捕获异常后 checkpoint 留在 running，等租约过期再被领取和失败，直到 claim budget 被耗尽。

影响：旧运行、未升级 TraeX、或合法选择不输出结构化 plan 的 transient failure 都无法受限 replan；
系统既没有明确终止，也没有真正请求用户，形成不可操作的“自动恢复中”状态。

修复：创建 checkpoint 时只要 mode=`replan_required` 就进入 awaiting_user，保存 pendingQuestion 与允许的
replan capability/budget；answer CAS 后转 pending。若产品不支持 legacy replan，则直接 terminal 并返回
明确错误。`start_continuation()` 的所有 TaskCheckpointError 也必须 CAS 到可诊断终态/等待态，不能遗留
running lease。

## 3. P2

### P2-1 — generation 上限混用了“领取重试次数”和“接力链代数”

[`TaskCheckpointDriver.claim()`](../backend/app/application/task_recovery.py) 用当前 checkpoint 的
`claim_generation` 对比 `agent_max_continuation_generations`；这实际是同一 checkpoint 的领取/租约代数。
`start_continuation()` 又读取 source ConversationRun 的 `continuationGeneration` 做链级检查。达到链级上限
时，它在 checkpoint 已变成 running 后抛异常，没有原子写 terminal。

现有单元测试通过手工把 `checkpoint.claim_generation=1` 来证明上限，测试的是领取次数，不是“continuation
失败→新 checkpoint→下一 continuation”的真实链。

影响：链级预算耗尽不会立即稳定终止；checkpoint 会等待 lease、重复 claim，并把基础设施重领与业务
接力代数混入同一个门禁和审计字段。

修复：新增不可变/单调的 `continuationGeneration` 到 checkpoint source evidence，claim 前按链级值判断；
claim_generation 只做 fencing。超限以 CAS 写 terminal、budgetExhausted 和事件。新增跨三个连续失败
continuation 的真实状态机测试。

### P2-2 — 同一 PlanSnapshot step 可在一个 AgentRun 内重复执行，最终状态被最后一次结果覆盖

AgentHost 在成功 ToolResult 后把 step id 加入 `completed_plan_steps`，但后续
`_validate_planned_tool_request()` 不拒绝 `step_id in completed_plan_steps`。Agent 只需换一个 callId 就能再次
执行同一步。最小反例已确认 completed step repeat 仍被接受。

checkpoint 构造又用 `{planStepId: event}` 字典归并结果，因此重复 step 的最后一个 ToolResult 覆盖之前
attempt：先成功后失败会被记为 failed，先失败后成功会被记为 completed，历史 attempt 不在 step state 中。

当前默认 Agent Tool 均为 effect=none，主要影响重复外部读取、模型费用和断点准确性；未来接入 write
Adapter 时可能升级为副作用风险。

修复：建立 Host-owned step state machine（remaining→running→succeeded/failed）。成功 step 在同一 plan
revision 内禁止再调用；retry 必须携带 attempt 并符合 step fallback、ErrorPolicy 和 idempotency 约束。
checkpoint 保存所有 attempts，不能用字典静默覆盖。

## 4. 已确认的有效修复

- AgentHostError/AgentRunError 忽略调用方 retryable，统一采用 ErrorPolicy。
- reportedError 与 effectiveError 均可审计，伪造 callId 会 fail closed。
- Plan schema 会校验 step 唯一性、能力集合、DAG、effect 与基本 budget。
- PlanSnapshot 和 ToolResult 使用独立有界 Artifact，加载前校验路径、size、sha256、digest 与归属。
- checkpoint 明确保存 completed、failed、remaining、pending calls 和 action ledger。
- Browser、lookup、read.local、summarize.text 均通过 Host Tool Broker 产生可信结果。
- recent Artifact reconcile 只扫描终态 Run，活动 staging 交错测试通过。
- RevisionProposal 并发冲突后响应 identity 与 proposal 内容来自同一持久记录。
- Replay/发布不可变证据链未出现回归。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 | 253 passed in 28.83s |
| Alembic | `0001→0017` 通过 |
| 前端 | 28 passed；TypeScript/ESLint/build 通过 |
| 全量黑盒 E2E | 24 passed in 72.16s |
| Browser + lookup + file 独立专项 | 3 passed in 20.20s |
| 计划参数偏离反例 | accepted |
| completed step 重复反例 | accepted |
| `git diff --check` | 写文档前通过 |

修复顺序与验收标准见 [`NEXT_STEPS-015.md`](../STEP/NEXT_STEPS-015.md)。
