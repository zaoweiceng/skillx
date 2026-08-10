# REPLAY-010：通配型探索、固化、复用、接力与修订闭环

日期：2026-08-05

依据：[`NEXT_STEPS-010.md`](NEXT_STEPS-010.md)、
[`review/design-compliance-review-010.md`](review/design-compliance-review-010.md)、
[`review/code-review-010.md`](review/code-review-010.md)。

执行基线：`24cca38`；本报告对应其上的当前工作区修复，尚未创建新提交。

范围：Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 结论

010 已完成设计文档要求的两条连续链路：

```text
主链：
真实 Conversation AgentRun
  → Host ToolResult
  → Distillation Draft
  → 人工 publish
  → 第二个新 Conversation 语义发现
  → L0/L1（fragment 时 L2）
  → Workflow Runtime 使用新输入成功执行

故障链：
已发布能力/外部节点失败
  → 持久 ToolResult/失败 fixture
  → checkpoint claim 或 baseline replay
  → transient/contract failure 分类
  → 受控 Patch
  → patched replay
  → 人工 publish
  → 原输入与新输入再次成功
```

网页和非网页 lookup 均通过新 Conversation 发现固化版本，不再通过测试创建 Component
并持有 id 直调。AgentRun checkpoint 已有实际消费者；外部 Browser 失败可保存 failure
fixture、重现原错误并驱动修订。基于本轮实际门禁，可将 Local Demo 范围内的专项状态
判定为 **closed-loop**。

## 2. 修复前后对比

| 项目 | 修复前 | 修复后 |
|---|---|---|
| 可运行基线 | `workflow_catalog.py` 导入未锁定 `pypolaris`，后端无法收集 | 删除伪依赖；白名单校验后直接使用 SQLite JSON path；新增 `app.main` 导入门禁 |
| 固化语义 | capability 复制原 prompt/URL，换输入后不稳定 | 结构化 `CapabilityDescriptor` 基于动作类别和 I/O Schema；拒绝 URL、Secret 和测试指令 |
| 第二次复用 | publish 后创建 Component，再持 id 直调 | 新 Conversation 通过 `workflow.search` L0、按需 L1/L2 选择发布版本并执行 |
| Manifest lineage | L0/L1 无首次来源摘要 | L1 暴露 source ConversationRun、trajectory digest、distillation id；L0 不扩张 |
| fragment | 只到 L1，空 bindings 也可被选择 | 选择前必须 `workflow.describe_fragment` 到 L2；服务端校验 boundary Schema 和 bindings |
| compose | `compile_plan` 在工具阶段写孤儿 PlanSnapshot | 工具阶段纯编译并返回 compiled plan/digest；最终选择只持久化一个 PlanSnapshot |
| ask_user | compose 计划无法完整恢复 | PendingSelection 保存 raw/compiled plan、refs、bindings 和明确 `resumeAction` |
| continue_agent | 再走未披露 Component heuristic | 直接创建持久 continuation AgentRun，保留披露摘要和剩余预算 |
| AgentRun handoff | TaskCheckpoint 只有生产者 | ToolResult Artifact、原子 claim、受限 continuation、一次性用户回答和完成状态已闭合 |
| 外部失败 replay | 只捕获成功 fixture；失败得到 `REPLAY_UNSUPPORTED` | 成功/失败统一 fixture envelope；保存原错误、输入/source digest、脱敏证据和 capture policy |
| 修订决策 | Transform 失败是主要正例 | 真实 Playwright Browser 读取契约失败可 baseline 同错误、Patch、发布并复用 |
| 输出 provenance | 只检查 call id 集合 | `outputProvenance` 逐字段校验 call/path/evidence digest；确定映射执行值级校验 |
| 终态 fencing | AgentRun check-then-write | success/failure/cancelled 均以 status/owner/generation/token 单条 CAS 提交 |
| 验收规范 | Browser 事件仍写 `browser.action.completed` | 更新为 `tool.requested/tool.result`，并增加连续复用、handoff、修订和 revised reuse 场景 |

## 3. 主链验证

### 3.1 网页总结

黑盒路径位于 `backend/e2e/test_demo_e2e.py::test_e2e_8b_real_browser_distillation_to_non_fake_agent`：

1. 动态页面正文设为 A，公开 Conversation AgentRun 调用真实 `browser.read`。
2. 断言最终输出、Host `tool.result` 和 Browser evidence 均来自 A。
3. 公开 distill API 创建草稿，人工 publish；测试不手写 Workflow definition。
4. 页面正文改为 B，URL 改为同授权域内的新 URL。
5. 创建第二个 Conversation，以自然语言“访问并总结”触发 Catalog。
6. 断言 L0/L1 timeline、选中版本和首次 ConversationRun lineage。
7. Workflow Runtime 执行，最终输出包含 B，且包含 Browser 与 Agent 节点事实。

### 3.2 非网页 lookup

`test_e2e_5_agent_distillation_reuse` 使用同一 Broker/Distillation/Catalog/Runtime 协议：

1. 首次 Conversation AgentRun 使用业务键 `q1` 获取真实 lookup ToolResult。
2. 固化并人工 publish。
3. 第二个新 Conversation 使用自然语言和业务键 `q2` 语义发现固化版本。
4. Runtime 返回 `q2` 对应的新结果，且无 LLM/Agent 运行事件。

两条链均禁止通过 Component id 或 WorkflowVersion id 绕过发现。

## 4. Selection-5 验证

`backend/tests/test_progressive_selection.py` 从公开 Chat 服务覆盖五种 action：

| action | 结果 |
|---|---|
| `run_full` | L0→L1→Run→Worker `succeeded` |
| `run_fragment` | L0→L1→L2→PlanRun→Worker `succeeded` |
| `compose` | compile diagnostics→唯一 PlanSnapshot→Worker `succeeded` |
| `ask_user` | 回答恢复原 task/ref/bindings，不重新搜索回答文本 |
| `continue_agent` | 不进入 Component heuristic，创建持久 AgentRun 并完成 |

未披露 ref、错误 ref 类型、缺 bindings、编译失败、调用预算和过期 session 继续 fail
closed。

## 5. AgentRun 失败接力

`TaskCheckpointDriver` 已接入独立 Worker 循环：

- 成功闭合的只读 ToolResult 保存为最大 64 KiB、脱敏、Schema 已验证且带 SHA-256 的
  Artifact；审计事件仍只保存摘要。
- checkpoint 保存 source digest、completed steps/Artifact refs、failed step、
  remaining plan、能力锁和 outcome certainty。
- `pending→running` 通过 generation/token/lease CAS 原子领取。
- continuation 只读取 checkpoint Artifact 和锁定能力，不读取全 Catalog。
- 原 ConversationRun 保留 `failed`；新 run 显式保存 checkpoint/source run 引用。
- `UNKNOWN_OUTCOME` 进入 `awaiting_user`；回答带 snapshot digest，且只能消费一次。
- continuation 完成后追加 `agent.handoff.started/succeeded/failed` lineage 事件。

`test_task_checkpoint_claims_and_continues_from_tool_artifact` 证明 lookup ToolResult 完成后
注入中断，恢复不重做 lookup，并从 Artifact 中得到随机业务结果。

## 6. 外部失败、Replay 与修订

成功和失败外部节点共用 ReplayFixture：

- 成功 fixture：`status/output/sourceDigest/inputDigest/outputDigest`。
- 失败 fixture：`status/error/observedOutput/sourceDigest/inputDigest/errorDigest`。
- 加载时重新关联冻结节点、NodeRun 输入和 Artifact hash；篡改或错关联返回
  `REPLAY_EVIDENCE_CORRUPT`。
- 网络/超时且未稳定复现归为 transient，不调用 Revision Agent。
- `PAGE_DRIFT`、`MCP_CONTRACT_DRIFT` 或可复现契约失败才允许进入受控修订。

`test_external_browser_failure_fixture_patch_publish_and_reuse` 使用真实 Playwright 外部读取：

1. Browser 读取成功，但冻结输出 Schema 与真实响应不符，运行以
   `CONTRACT_VIOLATION` 失败。
2. failure fixture 保存真实 observed output；baseline replay 重现相同错误。
3. Patch 只能调整受控 `pageContract` 字段，不能修改 URL pattern、能力锁、Secret、
   副作用或权限。
4. patched replay 使用同一 fixture 通过后创建草稿。
5. 人工 publish 后，原输入 A 与新输入 B 均成功。
6. revision lineage 保存 observation、fixture digest、ReplayEvidence 和 base version；
   新版本 id 由发布 Manifest 提供。

## 7. 并发与 provenance 不变量

- `agent.completed.outputProvenance` 必须恰好覆盖所有输出字段。
- 确定映射校验 call id、成功状态、path、evidence digest 和最终值。
- 生成式输出必须保存来源 call 集合、evidence digest 集合和 prompt version。
- “引用真实 call id 但返回 fixture 常量”由
  `test_output_provenance_rejects_fixture_constant` 拒绝。
- AgentRun 三种终态使用包含 status/owner/generation/token 的单条 CAS；CAS 成功后才
  写终态事件和 trajectory Artifact，失败清理 staging。

## 8. 最终门禁

| 命令 | 退出码 | 本次结果 |
|---|---:|---|
| `make demo-check` | 0 | Ruff、后端、迁移、前端、Playwright/E2E 全通过 |
| `cd backend && uv run pytest -q` | 0 | 217 passed |
| `cd backend && uv run pytest e2e -q -p no:cacheprovider` | 0 | 19 passed |
| 空库 `alembic upgrade head` 后再次 upgrade | 0 | `0001→0015` 通过且幂等 |
| `cd frontend && npm run check` | 0 | TypeScript 通过 |
| `cd frontend && npm run lint` | 0 | ESLint 通过 |
| `cd frontend && npm run test` | 0 | 7 files / 28 tests passed |
| `cd frontend && npm run build` | 0 | Vite production build 通过 |
| `git diff --check` | 0 | 无空白错误 |

`make demo-check` 的一次连续执行实际得到：

```text
Ruff: all checks passed
Backend: 217 passed
Alembic: 0001 -> 0015
Frontend Vitest: 28 passed
Frontend build: passed
E2E: 19 passed
```

## 9. 范围边界

本轮结论仅适用于 Local Demo。鉴权/RBAC、多租户、分布式 lease 协调和生产级 Artifact
存储不在 010 范围内；本轮未通过安装额外依赖或运行时 shim 掩盖问题。
