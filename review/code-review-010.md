# 代码审查报告（010）

审查日期：2026-08-05

审查范围：`REPLAY-009.md` 对应未提交修改，以及其影响的后端、迁移、前端和 E2E。Local Demo 不考虑鉴权。

## 1. 总结

本轮确认 009 的双向 Tool Broker、DisclosureCall 唯一 call id、PendingSelection、Replay Artifact 和 Conversation Artifact 路径均有实质实现，不是只有模型或接口声明。

但当前存在 1 个 P0、5 个 P1 和 4 个 P2。P0 使后端无法在锁定依赖环境导入；P1 则说明即使移除该导入，也不能宣称“第二次对话复用—失败接力—修订”已经闭合。

## 2. P0

### P0-1 — 未声明且不必要的 `pypolaris` 使后端无法导入

[`workflow_catalog.py`](../backend/app/application/workflow_catalog.py) 顶部直接导入 `pypolaris`，并在 `consume_session_call()` 中调用 `safe_sql_escape()`。该包不在 `pyproject.toml`/`uv.lock`，导致正常 Pytest 11 个模块收集失败；Ruff 也先因 import block 未排序终止质量门禁。

根因不是“漏装一个包”，而是把已经过固定白名单验证的 `operation` 又交给外部 SQL 转义库。JSON path 的字段只可能来自 `_SESSION_LIMITS = {search, describe, slice, plan}`，可直接构造；新增依赖既不增加安全性，又破坏可复现构建。

修复：删除导入和调用，白名单校验后使用 `f"$.{operation}"`；增加 clean-lock import 测试。

## 3. P1

### P1-1 — `TaskCheckpoint` 只有生产者，没有领取、恢复或用户回答状态机

[`agent_run.py`](../backend/app/application/agent_run.py) 在普通失败和租约过期时创建 TaskCheckpoint；[`database.py`](../backend/app/infrastructure/database.py) 也预留了 lease/generation/token 字段。但全仓没有 TaskCheckpoint claim、Recovery Driver、resume API 或 `awaiting_user` 回答 API。

更关键的是 checkpoint 的 `completedSteps` 来自数据库审计事件。`tool.result` 审计只保存字段、digest、bytes 等摘要，不保存真实输出；checkpoint 又没有引用 ToolResult Artifact。即使新增消费者，也无法从已完成结果继续推理，只能重做或丢上下文。

影响：首次 Agent 总结中断后不能把“已完成内容 + 剩余节点”交给 TraeX；UNKNOWN_OUTCOME 会永久停在 `awaiting_user`。

修复：先定义可恢复 envelope 的实际数据来源，为闭合的只读 ToolResult 保存有界 Artifact 引用；实现原子 claim、受限恢复 Agent、用户确认和完成/失败终态。

### P1-2 — Selection-5 的 fragment/compose 只验证“Agent 说了什么”，没有真实执行终态

[`traex_stub.py`](../examples/traex_stub.py) 的 `run_fragment` 在 L1 返回的 fragmentRef 上直接结束，没有调用 `workflow.describe_fragment` L2，并返回空 bindings。`compose` 也返回空 bindings。对于测试中的必填 `name`，Chat 随后调用 `_execute_selection_service()` 会因空输入无法创建 Run。

[`test_progressive_selection.py`](../backend/tests/test_progressive_selection.py) 只调用 `_select_workflow_candidate()` 并断言 action/level，不执行 selection；所以测试名“five actions ... loop”高于实际覆盖。

此外，ask_user 恢复逻辑只根据 ref 类型推断 `run_full` 或 `run_fragment`，无法保存并恢复一个缺输入的 compose 计划。

修复：Selection 必须携带已验证 bindings 或进入可恢复 PendingSelection；fragment 在选择前读取 L2；五个 action 都必须经公开 API、独立 Worker 到 waiting/succeeded/continue 终态。

### P1-3 — `workflow.compile_plan` 声明无副作用，却在选择阶段持久化孤儿 PlanSnapshot

[`workflow_tool_broker.py`](../backend/app/application/workflow_tool_broker.py) 的所有 Workflow Adapter 继承 `effect = none`，但 `WorkflowCompilePlanAdapter.execute()` 调用 `create_plan_snapshot()` 并写入数据库。Agent 最终输出 compose 后，`_execute_selection_service()` 又对同一 raw plan 调用一次 `create_plan_snapshot()`。

结果是一次 compose 至少生成两个 snapshot：第一个仅用于返回 diagnostics/id，后续执行不引用它。调用预算重试还会继续积累孤儿，且违背 Tool 描述的无副作用/幂等语义。

修复：拆出纯 `compile_plan()` 返回 compiled definition/digest/diagnostics；只在 Agent 最终选择 compose 时原子持久化一次，或让最终 Selection 引用并消费第一次冻结的 snapshot id。

### P1-4 — 外部节点自身失败时没有 Replay 证据，核心故障无法进入修订

[`orchestrator.py`](../backend/app/application/orchestrator.py) 的 `_capture_replay_fixture()` 只接受 `result.status == succeeded`。而 [`replay.py`](../backend/app/application/replay.py) 对 Browser/MCP/SkillX/LLM/Agent 没有 fixture 时统一生成 `REPLAY_UNSUPPORTED`。

因此：

- 页面漂移发生在 Browser 节点时，不会捕获该失败现场；
- MCP 契约变化发生在 MCP 节点时，同样没有 fixture；
- baseline replay 得到的是 `REPLAY_UNSUPPORTED`，不是原始 `PAGE_DRIFT`/`MCP_CONTRACT_DRIFT`/`DEPENDENCY_UNAVAILABLE`，`baselineReproduced` 必然为 false。

现有 Revision Agent 和 Patch E2E 使用纯 Transform `fail_dependency`，没有覆盖用户真正需要修订的外部节点。

修复：为失败外部节点保存受控 failure fixture（错误码、冻结契约、脱敏响应/页面证据、输入和 source digest），按错误类型选择模拟器；一次网络波动应分类 transient，不应伪造成可修订契约漂移。

### P1-5 — 固化能力的语义描述来自原始 prompt，换输入后不保证可发现

[`distillation.py`](../backend/app/application/distillation.py) 直接用清理后的 prompt 生成 `capability.summary/useWhen/examples`。E2E-8b 的 prompt 实质是 URL 加模式指令，因此发布后的搜索文本主要包含旧 URL 和通用英文 tag，而不一定包含“访问网页并总结”的任务语义。

同一个 E2E 又绕过搜索，直接创建 Component 并传 id 运行，所以无法暴露这个问题。

修复：让受限 Distillation Agent 输出受 Schema/长度/Secret 校验的 CapabilityDescriptor；增加“首次 URL A，第二次 URL B，但意图同为网页总结”的 Catalog 命中测试。非网页场景也应使用同一描述生成器。

## 4. P2

### P2-1 — Agent 最终 provenance 只核对 call id，不能证明输出依赖真实结果

[`agent_host.py`](../backend/app/application/agent_host.py) 只检查每个 broker call id 是否出现在 `agent.completed.provenance`。`path` 不校验、输出字段不绑定、值也不核对；Agent 可以列出真实 call id，同时返回无关或 fixture 输出。

修复：定义 `outputProvenance` 的字段级映射，至少校验目标输出字段、来源 call id、来源 path 和 evidence digest 存在且一致；对可确定映射执行值级校验，生成式总结保留可审计来源集合。

### P2-2 — AgentRun 终态 fencing 是 check-then-write，不是同一条 CAS

[`agent_run.py`](../backend/app/application/agent_run.py) 的 `_finalize_success/_failure/_cancelled` 先 `session.get()` 后用 Python `_owns()` 判断，再由 ORM 按主键更新。owner/generation/token 没有出现在最终 UPDATE 的 WHERE 条件中。

在读取后、提交前发生接管时，旧 Worker 仍可能写终态和 Artifact；当前“迟到 owner”测试只覆盖调用前已经换 owner，没有覆盖判断后并发接管窗口。

修复：终态改为带 status/owner/generation/token 的原子 UPDATE CAS；只有 rowcount=1 才登记 Artifact/事件。增加双会话交错测试。

### P2-3 — Replay 元数据被记录但未完整校验

`_load_replay_fixtures()` 校验 Artifact sha256 和 outputDigest，却没有根据当前冻结节点和 source Run 输入重新计算 `sourceDigest/inputDigest`。数据库行被错误关联或 source input 漂移时，fixture 仍可能被接受。

修复：按锁定 WorkflowVersion 节点 config/type 和 source Run 的节点解析输入重新计算 digest；不一致返回 `REPLAY_EVIDENCE_CORRUPT`。

### P2-4 — 验收文档和当前事件协议、覆盖事实不同步

[`docs/demo-acceptance.md`](../docs/demo-acceptance.md) 的 Real first browser 仍要求 `browser.action.completed`，当前实现和 E2E 已切到 `tool.requested/tool.result`。文档也没有把首次对话、发布、新对话发现、第二次执行写成一个场景。

修复：先更新验收场景，再写测试；禁止用 E2E-8b、E2E-10、Recovery demo 和 Transform Patch 四段结果拼接成同一闭环结论。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| Ruff | 失败：1 个 import-order 错误 |
| 后端正常 Pytest | 失败：11 collection errors，缺少 `pypolaris` |
| 后端 shim 诊断 | 210 passed；3 个真实 Playwright 用例受当前沙箱 Chromium 启动权限阻断，不计正式门禁 |
| 空库迁移到 0015，并再次执行 | 通过 |
| 前端 Vitest | 28 passed |
| 前端 build | 通过 |
| `git diff --check` | 通过 |
| 真实全流程模拟 | 未执行；P0 和功能分析均未通过前置门禁 |

修复顺序和验收标准见 [`NEXT_STEPS-010.md`](../STEP/NEXT_STEPS-010.md)。
