# REPLAY-008：TraeX Workflow 工具闭环与执行状态机

依据：[`NEXT_STEPS-008.md`](../STEP/NEXT_STEPS-008.md)。

## 1. 结论

008 将 Registry、Fragment、PlanCompiler、Runtime、Recovery 和 Replay 接成同一条
可审计执行链。数据库迁移推进至 `0014`；验证入口仍为 `make demo-check`。

## 2. Recovery fencing 与 Replan

- [`database.py`](../backend/app/infrastructure/database.py)：Checkpoint 和 Recovery
  NodeRun 持久化 `claim_generation/claim_token`。
- [`recovery.py`](../backend/app/application/recovery.py)：claim、heartbeat、Agent 返回提交、
  异常、取消和 NodeRun 终结均校验 `id/status/owner/generation/token`。
- 过期 claim 先提升 generation；unknown outcome 只保留一个待处理人工确认。
- `RecoveryPlanPatch` 只接受 `nodePatches/inputMappings/edgePatches`。Agent 不再提交
  完整节点定义或 locks；平台重新运行 Workflow、Capability 和 MCP 校验并保存
  before/after digest、diagnostics 与生成锁。
- [`test_recovery.py`](../backend/tests/test_recovery.py) 覆盖 A 超租约、B 重领、A 迟到返回，
  只允许 B 的替代输出和决定生效。

## 3. Disclosure 与 Selection-5

- migration `0014` 新增 `WorkflowDisclosureSession/WorkflowDisclosureCall`，持久化
  conversation/agent run、已披露 ref/level、剩余预算、expiry 和每次调用摘要/digest。
- Registry API 不再使用进程全局字典或默认 `local`；调用前必须显式创建 session。
- `WorkflowSelection` 对五种 action 执行互斥字段校验。
- `POST /workflow-tools/execute-selection` 分别处理 ask、continue、full、fragment 和
  compose；run_full 解析精确 WorkflowVersion，compile 失败返回结构化 diagnostics。
- Chat 展示 disclosure level、session、Agent action 和选择理由；问题分支可恢复原 session。

## 4. Fragment 与 Plan

- 发布 Fragment 时冻结 entry/exit ports、boundary Schema、input/output mapping、closure
  和 port digest；隐藏依赖、副作用缺审批、越界输出均拒绝。
- PlanCompiler 通过 Fragment `inputMapping` 展开别名，结果边只连接 output provenance，
  不再连接所有叶子；fallback-only target 不进入正常结果。
- PlanSnapshot 保存 source node 到 namespaced node 的双向映射。
- 新 Run 要求 Plan active 且未过期；既有 Run 只校验 digest/locks，因此过期后仍可恢复。
- search 只返回 logical Workflow 的 active Manifest；旧精确 ref 保持 resolvable。

## 5. Runtime、Replay 与 Observation

- [`state_machine.py`](../backend/app/runtime/state_machine.py) 是 Runtime/Replay 共用的
  edge readiness、条件选择、onError、fallback-only、skip 和 Output Schema 纯逻辑。
- Replay 的外部与副作用节点必须使用捕获 fixture；fixture 不满足输出契约时返回
  `REPLAY_UNSUPPORTED`。Evidence 包含 selected edges、skipped nodes、Schema diagnostics
  和 fixture digests。
- FailureObservation 使用 `workflow_version_id | plan_snapshot_id` 判别联合，并保存 source
  NodeRun、checkpoint、locks、contract 和 recovered outcome。
- Workflow 失败仍生成 Workflow draft；Plan 失败可经 `POST /plans/{id}/plan-patch`
  生成新的未发布 PlanSnapshot。

## 6. Catalog、Distillation 与产品

- Catalog 在 SQL 层预筛选并限制候选，再执行有界 rerank；metadata 有类型、长度和
  secret-like 扫描。
- Distillation action group 必须连续且拓扑闭合；分组保留所有 capability refs/digests。
  已删除公开但不生效的 `variableCandidates`。
- Conversation trajectory 登记标准 Artifact 行，沿用统一下载、校验和 GC 生命周期。
- AgentHost 对 `browser.read` 通过真实 Playwright Browser Tool Broker 执行；TraeX
  提供的 fixture result 不进入首次探索轨迹。
- Run detail 展示 Plan 状态/expiry、source step 和 Recovery generation；编辑器提供
  capability metadata 与 callable Fragment 结构化表单。

## 7. 门禁证据

| 门禁 | 证据 |
|---|---|
| Selection-5 / disclosure | `test_workflow_catalog_plans.py`、API E2E-10/11 |
| Recovery fencing / no escalation | `test_recovery.py` |
| Fragment ports / Plan fallback / lifecycle | `test_workflow_catalog_plans.py`、E2E-11 |
| Replay branches / Output Schema | `test_runtime_state_machine.py`、`test_workflow_patch.py` |
| Observation sources | `test_workflow_patch.py`、Plan patch API |
| Catalog active version / bounded search | `test_workflow_catalog_plans.py` |
| Real first browser | E2E-8b 的 `resultSource=browser_tool_broker` 与真实正文断言 |

最终 `make demo-check` 通过：Ruff、202 项后端测试、Alembic 空库升级、前端类型检查/
ESLint/28 项测试/生产构建，以及 19 项真实 API + 独立 Worker E2E 全部通过。
