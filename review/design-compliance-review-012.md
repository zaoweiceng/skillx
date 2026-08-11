# 设计与规范达成度审查报告（012）

审查日期：2026-08-05

依据：[`REPLAY-011.md`](../REPLAY/REPLAY-011.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)

代码基线：`bd2e45c`；审查开始时工作区 clean，本轮仅新增 012 三份文档。

范围：Local Demo；按约定不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

当前版本能够完成用户点名的真实主场景：

```text
首次 Conversation AgentRun 真实读取网页并总结
  → 成功轨迹固化为工作流草稿
  → 人工发布
  → 第二个新 Conversation 语义发现已发布能力
  → L0/L1 渐进披露
  → Workflow Runtime 用新 URL/新正文执行
  → 返回第二次网页内容并保留首次 lineage
```

本轮在真实 API、独立 Worker、Alembic 临时数据库、TraeX-compatible 进程、动态网页服务和
真实 Playwright 环境中重新执行，结果为 **1 passed in 5.41s**。因此“对话总结→固化→第二次
复用”在 Local Demo 内继续判定为 **available**。

011 对此前主要阻断的修复也基本成立：修订保留同一 Workflow 身份并发布为 v2；transport/timeout
历史错误不再获得 proposal 资格；TaskCheckpoint 的 owner/generation/token 已约束启动与终态；
Host 能按 action ledger 复用已完成 ToolResult。对应公开专项为 **3 passed in 12.31s**。

但 REPLAY-011 将整个故障修订链标记为 `closed-loop` 仍有一个决定性缺口：**Replay 证明没有绑定
最终发布的 draft digest**。Replay 通过后，通用 draft 或 contract-patch 接口可以再次修改定义；
publish 不要求重新 Replay，也不比较 ReplayEvidence 与当前 draft，仍会把修改后的内容发布为 v2。
此时 v2 继续携带旧 observation/ReplayEvidence lineage，证据与实际发布内容不一致。

所以 012 的判定是：

- **首次探索—固化—新对话复用：完成。**
- **TaskCheckpoint 接力、CAS 和 Host action ledger：实现并可运行。**
- **外部契约失败—Revision Agent—同源 v2—再次运行：执行路径可运行。**
- **修订证据与最终发布内容的不可变绑定：未实现。**
- **整体闭环：operationally available，但 evidence closure 仍为 partial，不能最终标记为 closed-loop。**

## 2. REPLAY-011 声明核验

| 011 声明 | 012 结论 | 证据或缺口 |
|---|---|---|
| Patch 保留原 Workflow.id，publish 形成 v2 | 成立 | Patch CAS 更新原 Workflow draft；publish 在相同 workflow_id 下生成 version=2 |
| v1 resolvable、搜索只返回 v2 | 成立 | Workflow 与 Fragment 旧 Manifest 均降为 resolvable；专项测试通过 |
| transport/timeout 历史 fixture 不得 proposal | 成立 | classification 固定 transient；eligibility 同时检查 evidence kind、occurrence 与 baseline |
| 两个独立契约失败后 Revision Agent proposal | 成立 | observation 按不同 Run id 去重；公开 Browser E2E occurrence=2 后调用 Agent |
| TaskCheckpoint claim handle 全程 fencing | 基本成立 | start/reconcile/answer 均使用 CAS；迟到 handle 测试通过 |
| Host action ledger 强制不重做 | 成立 | 相同 signature 的 effect=none 返回 Artifact 结果；计数 Adapter 调用次数为 0 |
| Worker 重启后从 Artifact 接力 | 部分成立 | 公开 handoff 用例在 continuation 已成功后才重启，重启 Worker 只完成 reconcile；没有在 pending checkpoint 前重启 |
| ToolResult staging/promote/reconcile 完整 | 基本成立 | 已采用 staging→DB→promote；但 reconcile promote 前不验 sha，且 target 缺 root 边界校验 |
| Revision lineage 包含 proposal/replay/v1/v2/run | 部分成立 | Run 可见 observation、ReplayEvidence、base v1 和当前 v2；proposalDigest 只在可变 Workflow draft metadata 中，不在不可变 v2 lineage 中 |
| 整体 closed-loop | 不成立 | 发布内容可在 Replay 后变化，旧 proof 仍被当作有效证据 |

## 3. 五阶段连续性

| 阶段 | 判定 | 已成立的事实 | 剩余边界 |
|---|---|---|---|
| 首次探索 | 完成 | ToolResult 由 Host Adapter 真实执行；Agent 自报 result 被拒绝；输出 provenance 逐字段验证 | TraeX 为仓库内兼容进程，不代表生产模型质量 |
| 固化 | 完成 | Distillation Agent 生成结构化 CapabilityDescriptor 和可编辑草稿；不自动发布 | 第二 URL 仍是同 path 的 query 变化，跨站泛化未覆盖 |
| 新对话复用 | 完成 | 第二 Conversation 经 L0/L1 发现并由 Runtime 执行新正文 | Catalog 仍是 Local Demo 的轻量词法检索 |
| 失败接力 | 基本完成 | 持久 checkpoint、Artifact、claim handle、用户回答和 action ledger 均有消费者 | 缺少“先停 Worker、再由新 Worker 领取 pending checkpoint 并复用 Artifact”的单条黑盒 |
| 失败修订 | 部分完成 | 外部 Browser 失败、proposal、Patch、Replay、同源 v2 和原/新输入运行可连续执行 | ReplayEvidence 没有锁当前 draft/compiled definition digest；编辑后仍可发布 |

## 4. 真实环境模拟

### 4.1 执行环境

专项命令：

```bash
cd backend
.venv/bin/pytest \
  e2e/test_demo_e2e.py::test_e2e_8b_real_browser_distillation_to_non_fake_agent \
  -q -p no:cacheprovider
```

Harness 为本次运行创建独立临时目录、SQLite 数据库和 Artifact root，只通过 Alembic `0001→0016`
建库，并启动 API、Worker、动态网页服务和 TraeX-compatible 子进程。Browser 节点使用真实
Playwright。

### 4.2 模拟过程

| 步骤 | 对话/系统动作 | 结果 |
|---|---|---|
| 1 | Conversation A 请求访问首次网页并总结 | AgentRun `succeeded`；出现 `tool.requested/tool.result` |
| 2 | 首次动态正文为“探索阶段正文” | Agent 输出精确来自 Host Browser ToolResult；evidence digest 有效 |
| 3 | 对首次 run 发起 distill | proposalSource 为 Distillation Agent；trajectory/proposal digest 有效 |
| 4 | 人工发布固化草稿 | 生成锁定 WorkflowVersion 和 L0/L1 Manifest |
| 5 | 页面改为“第二次运行的新正文：利润增长 21%。”，URL 改为 `?version=b` | 首次正文未被缓存复用 |
| 6 | Conversation B 以自然语言提出同类任务 | action=`run_full`；选择版本等于刚发布版本；timeline 为 L0/L1 |
| 7 | 检查 L1 lineage | `sourceConversationRunId` 指向 Conversation A |
| 8 | 独立 Worker 执行固化版本 | Run `succeeded`；输出包含“利润增长 21%”；Browser 与 Agent 节点均执行 |

结果：`1 passed in 5.41s`。

这证明主复用功能可用，但不证明修改后的 revision draft 仍与旧 ReplayEvidence 一致。

## 5. 011 新增故障链的实际能力

本轮另外执行：

```bash
.venv/bin/pytest \
  e2e/test_demo_e2e.py::test_agent_run_handoff_public_api_and_completed_action_ledger \
  e2e/test_demo_e2e.py::test_agent_run_unknown_outcome_answer_consumed_once \
  e2e/test_demo_e2e.py::test_e2e_9_reproducible_failure_gated_patch \
  -q -p no:cacheprovider
```

结果：`3 passed in 12.31s`。

已证明：

- 公开 AgentRun 失败能产生 TaskCheckpoint 和 ToolResult Artifact；
- continuation 重复请求相同 lookup 时，Host 返回 ledger 中保存的结果；
- UNKNOWN_OUTCOME 经公开 GET/answer API 只消费一次回答；
- 两个独立 Browser 契约失败可驱动 Revision Agent proposal；
- Patch 在原 Workflow 上形成 draft，人工发布为 v2；
- Catalog 只返回 v2，v1 精确 ref 可解析；
- v2 对原输入和新输入均运行成功。

未证明：

- known-failure handoff 在 TaskCheckpoint 仍为 pending 时重启 Worker，再由新 Worker 领取并从 Artifact
  继续；当前用例是 continuation 成功后才重启，重启只负责 reconcile；
- 真实 Browser/HTTP transport error fixture 的拒绝路径；公开瞬态对照使用的是 Transform
  `fail_dependency`；
- Replay 后修改 draft 会被 publish 拒绝。

## 6. 发布证据为何尚未闭合

`apply_version_workflow_patch()` 在 replay 通过后把 `ReplayEvidenceId`、base version 和 observation
写入 draft，并将 Workflow 标为 `pending_revision`。但之后：

1. `PUT /workflows/{id}/draft` 可以替换 definition，只把 status 改为 `manual_draft`，仍保留旧的
   Replay metadata 和 definition 内的 `revisionLineage`；
2. `/workflows/{id}/contract-patch` 可以再次修改 Browser contract，甚至不会改变 pending status；
3. `POST /workflows/{id}/publish` 只在 status 仍为 `pending_revision` 时检查 base version 是否最新，
   不比较 current draft digest、proposal digest、patched replay definition digest；
4. `ReplayEvidence` 没有保存 base/patched definition digest 或 patch digest；Revision Agent proposal
   也没有独立持久实体。

结果是：旧 proof 可以为未经过 replay 的新内容背书。这违反设计文档中“发布前展示回放结果和版本
差异”“局部 Agent 修复只能生成草稿、经校验后发布”的因果关系。

## 7. 门禁记录

| 检查 | 本次结果 |
|---|---|
| `make demo-check` | 通过 |
| Ruff | all checks passed |
| 后端 Pytest | 221 passed in 25.42s |
| Alembic | 空库 `0001→0016` 通过 |
| TypeScript / ESLint | 通过 |
| 前端 Vitest | 7 files / 28 tests passed |
| 前端 production build | 通过 |
| 黑盒 E2E | 21 passed in 33.15s |
| 专项对话总结固化复用 | 1 passed in 5.41s |
| 接力、unknown、Browser revision 专项 | 3 passed in 12.31s |
| `git diff --check` | 通过 |

## 8. 012 完成定义

只有同时满足以下条件，才能把整个 Local Demo 闭环最终标记为 `closed-loop`：

1. Revision Agent proposal 作为不可变记录持久化，Patch 必须引用 proposal id/digest，或显式标记为
   human-edited patch。
2. ReplayEvidence 保存 base definition、patch 和 patched definition digest。
3. revision draft 保存 `replayApprovedDraftDigest`；任何 draft/contract edit 都使 proof 失效。
4. publish 请求携带用户审核过的 draftRevision/digest，并以 CAS 校验；revision publish 还必须验证
   当前 digest 与通过的 ReplayEvidence 完全一致。
5. 新增“Replay 后修改 draft→publish 409→重新 Replay 后才可发布”的公开黑盒。
6. known-failure handoff 在 pending checkpoint 前重启 Worker，由新 Worker 领取、复用 Artifact 并完成。
7. 外部 transport/timeout 使用真实外部 Adapter 失败验证，而不是 Transform 错误码替身。

具体工作见 [`NEXT_STEPS-012.md`](../STEP/NEXT_STEPS-012.md)。
