# 代码审查报告（011）

审查日期：2026-08-05

审查范围：`REPLAY-010.md` 对应未提交修改，以及后端 AgentRun、TaskCheckpoint、Catalog、
Replay/Patch、Worker、E2E 和验收文档。Local Demo 不考虑鉴权。

代码基线：`24cca38` 加当前工作区修改。

## 1. 总结

本轮没有 P0。构建、迁移、前后端测试和 19 条黑盒均通过；真实“首次网页总结→固化→新对话
复用”专项也通过。

代码审查仍发现 **5 个 P1、2 个 P2**。最重要的问题不是功能原语缺失，而是三个系统不变量
没有真正成立：修订必须属于同一逻辑工作流的新版本、领取 token 必须约束后续提交、瞬态失败
不能通过重放自己获得修订资格。

## 2. P1

### P1-1 — WorkflowPatch 创建了新 Workflow，旧失败版本仍会进入 Catalog 搜索

[`routes.py`](../backend/app/api/routes.py) 的 `apply_version_workflow_patch()` 在 replay 通过后创建：

```python
draft = Workflow(
    id=str(uuid4()),
    name=f"patch-of-{version.workflow_id[:8]}",
    draft_revision=1,
    draft_json=patched,
)
```

发布时版本号只按 `WorkflowVersion.workflow_id == item.id` 计算；
[`workflow_catalog.py`](../backend/app/application/workflow_catalog.py) 的 `build_manifest()` 也只把
同一 `workflow.id` 下旧 Manifest 从 `published` 改成 `resolvable`。

所以修订发布得到的是“新 Workflow 的 v1”，不是原 Workflow 的 v2。原失败版本的 Manifest
仍为 `published`，搜索可能同时返回旧、新两个能力，直接违反设计中的逻辑身份/不可变版本模型和
E2E-9b/Catalog active 验收。

现有 `test_search_returns_only_active_version_but_old_ref_resolves` 只手工创建同一 Workflow 的
v2，无法覆盖 Patch 实际路径；外部 Browser Patch 测试也没有搜索 Catalog 或断言旧 ref 状态。

修复：Patch 应在原 `Workflow.id` 上创建受 base version 约束的新 draft revision，人工发布时生成
version N+1。增加从 Patch 入口到 publish、search、resolve 的连续测试，断言新 ref 是 v2、旧 ref
可解析但不进入 top-k。

### P1-2 — TaskCheckpoint 的 claim token 没有约束 continuation 创建和终态 reconcile

[`task_recovery.py`](../backend/app/application/task_recovery.py) 的 `claim()` 使用 CAS 更新
generation/token，却只返回 checkpoint id。`start_continuation(checkpoint_id)` 只检查：

```python
checkpoint.status == "running"
checkpoint.lease_owner == self.worker_id
```

它没有接收或比较本次 claim 的 generation/token，也没有用单条 CAS 将 `running→continuing`。
如果进程在 claim 后暂停，租约过期后另一个使用相同配置 worker id 的实例重领，迟到进程恢复时
仍会通过 owner 检查并创建重复 continuation。`_reconcile_continuation()` 同样直接 ORM 更新，多个
Driver 可重复写 handoff 终态事件。

修复：claim 返回不可变 `{id, owner, generation, token}`；start 使用这些字段执行
`running→continuing` CAS，只有 rowcount=1 才 enqueue continuation 和写事件。reconcile 也用
`continuing + continuationRunId + generation/token` CAS。增加同名 Worker、租约过期、迟到启动的
双 Session 交错测试。

### P1-3 — “已完成步骤不重做”只由提示词约束，Host 没有执行级防线

TaskCheckpoint 会加载已完成只读 ToolResult Artifact，但 continuation 的
`allowed_capabilities` 仍是原运行能力与 locks 的交集。Recovery prompt 写了“禁止重做已完成步骤”，
Host/Broker 却没有 completed call/action ledger、deny set 或幂等键来拒绝相同 capability+arguments。

当前测试中的 TraeX stub 恰好直接使用 Artifact 输出，所以测试通过；换成真实自主 Agent 后，它仍可
再次调用 lookup/browser。对只读调用会产生语义漂移和额外成本；若未来放宽可恢复 effect，风险会升级
为重复副作用。

修复：checkpoint 保存已完成 action 的规范化 signature 和 outcome；Recovery Host 在执行前拒绝
已完成 signature，未知副作用只能在用户确认后按明确策略处理。验收要统计外部服务调用次数，不能仅
断言 continuation 输出。

### P1-4 — 外部失败修订没有一条符合验收规范的连续黑盒

[`test_workflow_patch.py`](../backend/tests/test_workflow_patch.py) 的 Browser 正例使用真实 Playwright，
但它直接调用 `Orchestrator.create_run()`、`WorkerService.run_once()`、
`apply_version_workflow_patch()` 和 `publish_workflow()`；Patch 也是测试手写的。

公开 [`test_demo_e2e.py`](../backend/e2e/test_demo_e2e.py) 的 E2E-9 虽经过 HTTP 和独立 Worker，
失败节点却是 Transform `fail_dependency`，并且只验证创建 draft，没有人工发布、原输入/新输入复用或
Catalog active。Revision Agent proposal 由另一条单元测试覆盖，未消费 Browser failure observation。

这与 [`docs/demo-acceptance.md`](../docs/demo-acceptance.md) “每个场景必须通过真实 API、独立
Worker 和持久化数据库”的要求冲突，也无法证明 TraeX 得到故障上下文后能为同一个外部失败提出可用
修订。

修复：新增单条 E2E-9/E2E-9b：真实 Browser 契约失败→公开 observation/proposal API→TraeX
proposal→公开 Patch API→patched replay→人工 publish v2→原输入和新输入运行→Catalog/lineage
断言。测试不得调用 route/service 函数或手写最终 Patch。

### P1-5 — 一次外部瞬态错误可以靠冻结 failure fixture 被判为“可复现”

[`workflow_patch.py`](../backend/app/application/workflow_patch.py) 定义了
`REPRODUCIBLE_THRESHOLD = 2`，`observation_eligibility()` 的注释也说 occurrence 达阈值才可修订，
但实际 eligibility 只读取 `baseline_reproduced`，完全不检查 occurrence。

同时失败 fixture 会把捕获的 `DEPENDENCY_UNAVAILABLE/TIMEOUT` 原样交给 isolated replay；baseline
返回同一错误后就把 `baseline_reproduced=True`。`classify_failure()` 对已经“复现”的依赖错误不再
返回 transient，而会落到 `platform_bug`，随后 `should_propose_patch()` 允许 proposal。

冻结契约响应可以证明 Schema/页面契约漂移；冻结一次网络错误只能证明历史错误存在，不能证明外部
状态可复现。当前单测甚至明确允许 occurrence=1 的 `DEPENDENCY_UNAVAILABLE` 调用 Revision Agent，
与 REPLAY-010 的“网络/超时且未稳定复现归为 transient”声明相反。

修复：按失败类型定义 reproduction policy。网络/超时忽略 injected error fixture，必须经过重试后的
独立主动探测或至少两次独立真实观测；contract drift 可用冻结 observed output 重新校验契约。eligibility
必须同时校验 policy、occurrence、baseline evidence kind 和 classification。

## 3. P2

### P2-1 — Agent ToolResult Artifact 在数据库事务前写入最终路径

[`agent_run.py`](../backend/app/application/agent_run.py) 的 `_persist_tool_result_artifact()` 先
`target.write_bytes(content)`，再打开数据库事务登记 Artifact。虽然 lost ownership 时会 unlink，
但数据库提交异常、进程在写文件后崩溃或 duplicate row 早退时仍可能留下或覆盖最终文件。

Workflow replay/trajectory 已采用“先 staging、事务提交后原子 promote、GC reconcile”的一致模型；
ToolResult 应复用同一生命周期，而不是建立第二套较弱语义。

修复：先写 `.staging`，CAS/Artifact 行提交后 promote；metadata 保留 stagingKey，Worker 启动时统一
reconcile。duplicate 时比较 hash 后清理 staging，不覆盖既有最终文件。

### P2-2 — `continue_agent` 声称保留 ToolResult，实际只传披露审计摘要

[`routes.py`](../backend/app/api/routes.py) 的 `continue_agent` 上下文只包含 `callId`、
`capabilityRef`、`level`、`status` 和 diagnostics，没有 search cards、describe Schema 或任何工具结果。
Prompt 却称“保留以下已完成 ToolResult 摘要”。

这不会阻断当前“不存在合适工作流时继续 Agent”的简单 fixture，但真实 Agent 无法基于已经披露的候选
和契约继续推理，可能重新搜索，或在缺少结果时做出不同决策。

修复：保存有界、脱敏的 disclosure result Artifact/digest，并在 continuation 中通过受限上下文引用；
至少传递选中过/排除过的 ref、关键 input/output synopsis 和结果 digest，同时保持 L0/L1 字节预算。

## 4. 已确认的有效改进

- 删除未锁定 `pypolaris`，clean-lock import 门禁恢复。
- Agent Host 的确定性输出逐字段校验 call/path/evidence digest/value；生成式输出锁来源和 prompt version。
- AgentRun success/failure/cancelled 终态已改成 owner/generation/token CAS。
- Distillation 使用结构化 CapabilityDescriptor，拒绝 URL、Secret 和测试指令进入语义描述。
- 第二次复用已从“创建 Component 持 id 直调”改为新 Conversation 的 L0/L1 发现和 Runtime 执行。
- fragment 在执行前进入 L2；compose 工具阶段纯编译；PendingSelection 能保留 compose 恢复信息。
- 成功/失败外部节点均保存 replay fixture，并重新校验 source/input/output-or-error digest。
- TaskCheckpoint Driver 已接入真实 Worker 循环，不再是无消费者的数据表。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；后端 217、前端 28、E2E 19 |
| 专项 E2E-8b | 1 passed in 6.30s |
| TaskCheckpoint + Browser Patch + Catalog active 回归 | 3 passed in 2.87s |
| `git diff --check` | 通过 |

修复顺序和验收标准见 [`NEXT_STEPS-011.md`](../STEP/NEXT_STEPS-011.md)。
