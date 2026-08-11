# NEXT_STEPS-007 执行回放（REPLAY-007）

依据：[`NEXT_STEPS-007.md`](../STEP/NEXT_STEPS-007.md)、[`review/design-compliance-review-007.md`](../review/design-compliance-review-007.md)、[`review/code-review-007.md`](../review/code-review-007.md)、[`REPLAY-006.md`](../REPLAY/REPLAY-006.md)。
执行范围：A（修正 006 的 Distillation、Browser、Recovery 与 Replay 语义）、B（Workflow Capability Manifest）、C（面向 TraeX 的渐进披露）、D（安全 Fragment）、E（引用式组合与 PlanSnapshot）、F（人工固化建议）、G（产品、可观测性与文档）。
非目标：鉴权、RBAC、多租户、分布式部署和生产级沙箱。

## 1. 总体结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff（app/tests/migrations/e2e/examples） | 通过 |
| 后端 Pytest | 194 passed（006 基线 188） |
| 空库 Alembic 升级 | 0001 → 0013 通过 |
| 前端 check / lint / test / build | 通过（28 passed） |
| 黑盒 E2E（`pytest e2e`） | 19 passed |

核心结论：007 已建立完整的主路径：

```text
已发布 WorkflowVersion
  → 不可变 WorkflowCapabilityManifest
  → 有界 L0 搜索
  → TraeX 输出受约束 WorkflowSelection
  → 按需 L1/L2 描述
  → Workflow/Fragment refs + Agent 组合
  → PlanCompiler 展开为冻结 DAG
  → PlanSnapshot 持久队列执行、恢复与审计
  → 高频稳定计划只建议人工创建 draft
```

同时，007 修复了 006 审查指出的真实语义断点：动态参数不再缺 provenance 时伪装成 constant；Proposal 的 grouping 和三种策略真正影响图；Browser 固化使用真实 Playwright；Recovery 支持崩溃重领、用户问题和 typed replan；Patch 资格只认 baseline reproduction，patched replay 成功后才生成 draft。

README 没有把上述能力整体提升为 `available`。外部读取 replay fixture 的生产捕获、复杂分支输出 provenance、直接 SkillX/MCP PlanStep 和更强语义检索仍需扩展，因此相关能力继续标记为 `partial`。

## 2. A：修正 006 的阻断语义

### 2.1 真实 Distillation 闭环

- **逐参数 provenance fail closed**（[`distillation_ir.py`](../backend/app/application/distillation_ir.py)）：未显式声明且无法证明来自用户输入的参数标为 `unknown`，Compiler 产生 `MISSING_ARGUMENT_PROVENANCE` error，不能发布；只有 Runtime 明确声明的值才能成为 `constant`。
- **独立 Distillation Agent**（[`distillation.py`](../backend/app/application/distillation.py)）：真实轨迹未携带 Proposal 时，通过零工具 `AgentHost` 请求受 Schema 约束的 `DistillationProposal`。成功时保存 `proposalSource=distillation_agent`、`sourceTrajectoryDigest` 和 `proposalDigest`；Runtime 不支持该 Schema 时记录 `proposalSource=none`，不伪造 Agent 提议事实。
- **Proposal grouping 真正生效**：`ProposedNode.nodeId/actionIds/strategy` 决定节点边界；每个 action 必须恰好属于一个组。`deterministic | agent | llm` 三种策略均进入 Compiler 裁决；同组多个 action 合并为一个概率节点，组内依赖不泄漏为悬空边，边界 capability 取原动作的最小并集。
- **Compiler 不再访问私有 Registry 成员**（[`action_compilers.py`](../backend/app/application/action_compilers.py)）：新增 `compile_as` 公共入口；Agent fallback 保留原动作最小 capability、输入 bindings、预算和输出 Schema。
- **Workflow output provenance**：成功轨迹记录逐字段 `outputProvenance`；Compiler 按 `actionId/path` 生成 output mapping。缺失或引用未知 action 时产生发布错误。上游 `outputSchema` 不存在的字段从 warning 提升为 error。
- 测试：`test_proposal_groups_multiple_actions_into_one_agent_node`、`test_valid_proposal_participates_in_adjudication`、`test_proposal_not_covering_every_action_is_rejected`、`test_distilled_draft_reuse_varies_with_new_input`。

### 2.2 Browser 固化与真实 TraeX

- **编译/执行共用契约**（[`action_compilers.py`](../backend/app/application/action_compilers.py)、[`browser.py`](../backend/app/runtime/browser.py)）：BrowserActionCompiler 直接构造并校验 `BrowserReadContract`；`bodySelector/fields/outputSchema/timeoutMs` 全部归入 `pageContract`。
- **生产 runtime 明确为 Playwright**：固化默认选择 `runtime=playwright`；Compiler 以 `BROWSER_TEST_ADAPTER_FORBIDDEN` 阻止 `observedContent` 测试 adapter 出现在可发布 Read/Extract 图。
- **redirect 后再次校验 URL**（[`playwright_browser.py`](../backend/app/runtime/playwright_browser.py)）：导航完成后重新用 `url_matches` 校验最终 `page.url`，越出 scheme/host/path 契约立即失败。
- **真实 mixed E2E**：`test_e2e_8b_real_browser_distillation_to_non_fake_agent` 从 Conversation Agent 的 `browser.action.started` 开始，经 Distillation Agent Proposal、draft、publish、页面正文修改，再运行 Playwright Browser → 非 fake AgentHost；断言第二次输出包含新正文。

### 2.3 Recovery 完整状态机

- **统一租约和取消**（[`recovery.py`](../backend/app/application/recovery.py)）：checkpoint 与 Recovery NodeRun 共用 owner/expiry/heartbeat；Agent 执行期间刷新两者租约并轮询 Run 取消，取消信号传给统一 `AgentHost`，可立即停止挂起 subprocess。
- **崩溃重领**：Driver 回收过期 `running` checkpoint。outcome known 回到 `pending` 并重领；outcome unknown 创建人工确认。标准 `WorkerService.recover_expired` 显式排除 Recovery NodeRun，避免与 RecoveryDriver 竞争。
- **标准用户问题**：`request_user` 创建 `Approval(effect=recovery.question)`，保存 question、answer 和 checkpoint digest；`POST /approvals/{id}/answer` 恢复同一 checkpoint。
- **typed replan**：`RecoveryPlanPatch` 必须包含 `remainingNodes/remainingEdges/inputMappings/locks`；校验节点完整覆盖、边界、输出边和能力锁不扩张后创建 `RecoveryAttempt(status=active)`，合并为附加冻结执行源并创建新的失败节点 attempt。空 substitution 不再代表重规划。
- **正式能力锁**：checkpoint 从 WorkflowVersion 或 PlanSnapshot 的 validation/locks 加载 capability/MCP locks，不再读取 Run 动态私有属性。
- **fallback target 专用**（[`compiler.py`](../backend/app/application/compiler.py)）：普通 incoming/outgoing edge 复用 fallback target 产生 `FALLBACK_TARGET_NOT_DEDICATED` 发布错误。
- **Context Budget**：大于 16KB 的 completed output 不复制进 checkpoint，改存 digest、字节数、NodeRun handle 和已有标准 Artifact refs；Recovery prompt 限定约 24KB，超限时进一步压缩输入和 Schema。
- 黑盒：E2E-7b 强杀已领取 Recovery 的 Worker 后由新 Worker 重领；E2E-7c 回答标准问题续跑；E2E-7d 执行 typed replan，新 attempt 成功且旧失败 attempts 不改写。

### 2.4 可证明的 Patch replay

- **Observation 原子 upsert**（[`workflow_patch.py`](../backend/app/application/workflow_patch.py)）：SQLite `ON CONFLICT DO UPDATE` 聚合 occurrence、source Run/checkpoint 和恢复事实，避免并发首次失败 unique 冲突。
- **occurrence 不再冒充复现**：`FailureObservation.baseline_reproduced` 是独立持久事实；`observation_eligibility` 只认成功的 baseline reproduction，出现次数仅作为热度。
- **来源强绑定**（[`routes.py`](../backend/app/api/routes.py)）：目标 WorkflowVersion、observation、source Run 必须一致；禁止客户端提交替代 `safeInputs`。
- **双阶段 proof**（[`replay.py`](../backend/app/application/replay.py)）：先运行 base 并要求同错误码失败，再运行 patched；依赖失败会阻止下游，最终通过要求原失败点和 Output 均 succeeded。
- **副作用隔离**：所有 `sideEffect` 节点一律模拟，不因 idempotent 标记执行真实写；MCP/SkillX/Browser/LLM/Agent 外部读取必须提供捕获 fixture，否则返回 `REPLAY_UNSUPPORTED`。
- **持久证据**：`ReplayEvidence` 保存 baseline snapshot、patched snapshot、diff 和 passed；失败只保存证据，不创建 draft。
- 黑盒：E2E-9 通过 API 发布确定失败工作流，真实 Worker 形成 observation；Patch API 复现 `DEPENDENCY_UNAVAILABLE`，patched 最终 Output 成功后才创建 draft。

## 3. B：Workflow Capability Manifest

- **不可变数据模型**（migration 0013，[`database.py`](../backend/app/infrastructure/database.py)）：新增 `WorkflowCapabilityManifest`，保存 workflowVersionId、workflow digest、manifest JSON/digest、状态和 search text；每个 WorkflowVersion 至多一个 manifest。
- **发布即生成**（[`workflow_catalog.py`](../backend/app/application/workflow_catalog.py)、[`routes.py`](../backend/app/api/routes.py)）：`publish_workflow` 在同一事务中生成 manifest；升级创建新版本和新 manifest，不修改旧记录。
- **自动派生字段**：I/O synopsis、executionClass、sideEffects、approvalRequired、estimated LLM calls、step summary、capability/MCP locks 和 entrypoints 从冻结版本派生；`summary/useWhen/doNotUseWhen/examples/tags` 从作者审核的 `metadata.capability` 读取。
- **最小披露**：manifest 不包含完整 prompt、节点 config、Secret 或 Artifact 内容。
- **可靠性按需计算**：L1 describe 基于精确 version 最近 20 次终态 Run 计算 sample size 和 success rate，不回写不可变 manifest。
- 测试：`test_catalog_search_is_bounded_and_progressively_disclosed`、E2E-10 的默认版本升级与旧 ref 继续运行。

## 4. C：面向 TraeX 的渐进披露

- **四个受控工具入口**：
  - `POST /workflow-tools/search`：L0 top-k 卡片；
  - `POST /workflow-tools/describe`：L1 完整 I/O Schema、限制、步骤摘要、成本、可靠性、版本锁和 fragment 列表；
  - `POST /workflow-tools/describe-fragment`：L2 边界、closure digest、锁、副作用和审批；
  - `POST /workflow-tools/validate-selection`：校验受 Schema 约束的 `WorkflowSelection`。
- **L0 有界**：`limit≤8`，总响应预算 24KB；支持 required inputs、output type、zero LLM 和 sideEffects 约束。1000 个 Manifest 的集成测试仍只返回有界卡片，且不含完整 Schema/DAG。
- **TraeX 负责语义选择**：对话先由 Registry 生成 L0，再由零工具 `AgentHost` 仅基于这批卡片输出 `run_full | run_fragment | compose | continue_agent | ask_user`。Host 拒绝本次 L0 未披露的 ref，之后才读取所选项 L1。无 TraeX 时明确标记 `selectionSource=ui_fallback`，不冒充 Agent 决策。
- **会话边界**：按 `sessionId` 限制 search/describe/slice/plan 调用次数；L1/L2/Plan 只能使用当前会话已披露 refs。
- 黑盒：E2E-4 断言 `selectionSource=traex`、`disclosureLevel=L0` 和锁定 workflowRef；E2E-10 证明 L0→L1→组合执行。

## 5. D：安全的 Workflow Fragment

- **显式发布片段**：作者通过 `metadata.callableFragments` 声明稳定业务组。发布时计算 closure、boundary schemas/mappings、source workflow/version digest、capability/MCP locks、sideEffects、approvalRequired 和 closure digest；Fragment 不复制独立可变定义。
- **隐含依赖检查**：声明节点若依赖未包含且非 Input 的节点，发布失败；包含副作用但没有审批或显式允许时拒绝。
- **确定性临时 Slicer**：Agent 只给 candidate node ids；Slicer 自动加入所有祖先和 fallback target，拒绝 Input/Output 伪业务节点、隐藏 Secret、副作用绕过审批和无法形成输出边界的裁剪。
- **私有 FragmentSnapshot**：临时裁剪生成 `fragment-snapshot:` ref，状态为 `private`，继续复用相同 Fragment resolver、PlanCompiler 和 Runtime，不进入公共目录。
- 黑盒：E2E-11 描述并执行 `format-only` 只读片段；尝试只裁剪 Browser submit 副作用节点返回 `FRAGMENT_SLICE_REJECTED`。

## 6. E：引用式组合与 PlanSnapshot

- **ExecutionPlan IR**（新增 [`plans.py`](../backend/app/application/plans.py)）：支持 `workflow | fragment | transform | llm | agent` step、显式 edges、input/output Schema、mappings 和 policies；模型只提交 ref/mapping，不复制 Workflow definition。
- **PlanCompiler 最终裁决**：
  - 校验 ref/version/digest 和 fragment closure；
  - 校验 required inputs、基础 JSON Schema 类型可赋值、DAG 和输出 required 完整性；
  - Agent/LLM capability 必须是 Host 授权集合子集；
  - 展开 Workflow/Fragment 为 namespaced frozen nodes/edges；
  - 保留 source refs、namespaced locks 和业务 step result 节点。
- **不可变 PlanSnapshot**（migration 0013）：保存 definition/digest、source refs、locks、createdByConversationRun、TTL/status 和隐私安全 planSignature。
- **Run 判别联合**：数据库 check constraint 强制 `workflow_version_id` 与 `plan_snapshot_id` 恰有一个；Worker 每次调度、claim、租约接管前重新验证 PlanSnapshot digest，再执行冻结 DAG。
- **复用现有 Runtime**：Plan Run 使用同一队列、NodeRun、RunEvent、Artifact、Approval、Recovery 和 Patch 路径，不引入递归子 Run。
- **版本锁**：Plan 创建后源 Component 默认版本升级不改变 PlanSnapshot 的 source ref/digest 或执行结果。
- 黑盒：E2E-10 组合两个锁定 Workflow refs + 非 fake Agent，创建临时 PlanSnapshot；升级默认版本后运行仍得到旧版本语义。

## 7. F：高频计划只建议人工固化

- **隐私安全 planSignature**：只包含 refs/digests、step kinds、mapping shape 和 I/O Schema；常量只保留类型，不含输入值或 Secret。
- **稳定性聚合**：按 signature 统计成功数、失败率、输入变化数、手工调整数和平均 Token；只有成功达到阈值、失败为 0、至少两个输入变体时返回 `suggest_draft`。
- **人工固化**：`POST /plans/{id}/solidify` 从冻结 definition 创建 Workflow draft，保留 `sourcePlanSnapshotId/sourceRefs`；响应固定 `published=false`，不会自动发布或升级 ref。
- 黑盒：E2E-12 用三个不同输入成功运行同一 Plan，出现建议；接受建议后只生成 draft，原 PlanSnapshot 和源 Workflow 不变。

## 8. G：产品、可观测性和文档

- **对话候选卡片**（[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx)）：展示 L0、匹配分数、required inputs、副作用、审批、LLM 次数和锁定 ref。
- **运行详情**（[`RunDetailPage.tsx`](../frontend/src/pages/RunDetailPage.tsx)）：区分 WorkflowVersion 与临时 PlanSnapshot，展示 plan id/digest、来源锁、临时标记、检查点和恢复决定。
- **组件披露预览**（[`ComponentDetailPage.tsx`](../frontend/src/pages/ComponentDetailPage.tsx)）：展示 TraeX 的 L0/L1/L2 manifest、useWhen/doNotUseWhen、tags、entrypoints、fragments 和 digest。
- **单一验收清单**：[`docs/demo-acceptance.md`](../docs/demo-acceptance.md) 记录 E2E-1～12、Recovery 分支和 Scale-1；Makefile/README 不再硬编码旧“六条 E2E”口径。
- **能力状态保守同步**：真实 Browser 固化、Recovery 分支、Replay proof 和临时组合已有证据，但仍存在下节扩展项，因此 README 继续使用 `partial`。

## 9. 007 门禁矩阵

| 门禁 | 证据 | 状态 |
|---|---|---|
| Real distillation | E2E-8b：Browser events → Proposal → draft → publish → 新正文 → 非 fake AgentHost | 通过 |
| Recovery crash | E2E-7b：领取后强杀 Worker，新 Worker 过期重领；旧 Recovery NodeRun 留证 | 通过 |
| Recovery branches | E2E-7c request_user；E2E-7d typed replan + 新 attempt | 通过 |
| Replay proof | E2E-9：baseline 同错误码失败，patched Output 成功后才建 draft | 通过 |
| Catalog scale | `test_catalog_search_is_bounded_and_progressively_disclosed`：1000 Manifest、top-k≤8、≤24KB | 通过 |
| Progressive disclosure | E2E-4/E2E-10：L0 选择后才加载 L1，ref/digest 稳定 | 通过 |
| Full workflow selection | E2E-4：TraeX Selection → 补参 → 锁定 Workflow Run | 通过 |
| Fragment safety | E2E-11：合法片段运行；副作用绕过裁剪被拒 | 通过 |
| Temporary composition | E2E-10：两个 refs + Agent → PlanSnapshot → 同一 Runtime | 通过 |
| Version pinning | E2E-10：默认版本升级不改变已创建 PlanSnapshot/Run | 通过 |
| Solidification suggestion | E2E-12：稳定多输入成功只建议并创建 draft | 通过 |
| Context budget | Recovery 大输出摘要测试；Catalog 1000 项仍只返回有界 L0 | 通过 |

## 10. 数据模型与迁移

Migration `0013_workflow_capabilities.py` 新增：

- `workflow_capability_manifests`；
- `workflow_fragment_manifests`；
- `plan_snapshots`；
- `recovery_attempts`；
- `replay_evidence`；
- `runs.plan_snapshot_id` 与执行源判别约束；
- `failure_observations.baseline_reproduced`。

空库 `0001→0013`、ORM metadata 清理和迁移测试均通过。

## 11. 显式遗留与状态边界

以下能力不影响当前 Local Demo 主路径和 007 门禁，但在补齐前不应把相关整体能力标为 `available`：

1. **外部读取 replay fixture 生产捕获**：ReplayEngine 已禁止真实外部写并要求 MCP/Browser/Agent 读取 fixture，但生产路径尚未为所有 adapter 自动捕获、版本化和登记标准 Artifact。
2. **Replay 标准运行实体**：当前 `ReplayEvidence` 保存 baseline/patched snapshots 与 diff；尚未把每次 replay 完整建模为普通 Run/NodeRun/Artifact 联合。
3. **复杂分支输出 provenance**：逐字段 mapping 和 Schema 错误已实现；多分支 union/数组深层路径仍需更完整的 Schema 可赋值推导。
4. **直接 SkillXRef/MCPRef PlanStep**：Plan 可展开包含 SkillX/MCP 的 Workflow/Fragment，但尚未提供顶层直接 `skillx | mcp` step kind。
5. **检索质量**：当前为关键词、中文字符 gram、作者语义字段和结构化约束的混合排序；尚未接入独立 embedding/语义模型。
6. **专用 Manifest 编辑表单**：组件页已有披露预览，作者仍主要通过 Workflow metadata/JSON 编辑语义字段和 callable fragments。

因此，准确声明是：**007 的可检索、渐进披露、合法片段、引用组合、冻结执行、恢复与人工固化建议主链已完成并通过门禁；扩展语义和更完整 replay 证据模型继续标记 partial。**
