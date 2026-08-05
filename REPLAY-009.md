# REPLAY-009：通配探索、固化、复用、接力与修订证据

执行日期：2026-08-04

依据：[`NEXT_STEPS-009.md`](NEXT_STEPS-009.md)、
[`review/design-compliance-review-009.md`](review/design-compliance-review-009.md)、
[`review/code-review-009.md`](review/code-review-009.md)。

范围：Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 基线与结论

- 基线 commit：`cb93983d82afc56d6e67370065295b5588284660`。
- 本轮修改尚未提交；因此该 commit 只表示修改起点，不表示 009 结果已进入 Git 历史。
- Alembic head：`0015`。
- `make demo-check` 一次通过：
  - Ruff：通过；
  - 后端 Pytest：`213 passed`；
  - Alembic 空库 `0001→0015`：通过；
  - TypeScript、ESLint：通过；
  - Vitest：`28 passed`；
  - Vite production build：通过；
  - 独立 API/Worker/Playwright E2E：`19 passed`。

009 修复了 008 审查中的 P0，并把原先分段存在的 Broker、Catalog、Selection、
Distillation、Checkpoint、Replay 和 Artifact 原语接入同一组持久协议。首次探索不再通过
Browser completed 事件重写结果；Browser 与非网页 lookup/read 都由 Host Adapter 返回真实
`tool.result`，Agent 的后续参数和最终输出必须引用真实 call id provenance。

## 2. A：可执行基线

- 删除 `replay.py` 中未声明、未使用的 `pypolaris` import 和误导性注释。
- Replay 继续统一调用 `expressions.resolve_mapping()`。
- `FailureObservation` ORM 增加与迁移同名的
  `ck_failure_observation_source`，`create_all` 与 Alembic 约束一致。
- 空库迁移测试更新到 `0015`，并验证新增表、fencing 列和 DisclosureCall 终态列。

## 3. B：双向通用 Tool Broker

新增 [`tool_broker.py`](backend/app/application/tool_broker.py)，统一 Adapter 契约包含：

- `capability_ref`；
- input/output Schema；
- effect、idempotent；
- fixture policy；
- Host 侧 `execute()` 与可选 distillation evidence。

事件协议新增判别联合：

```text
tool.requested(callId, capabilityRef, arguments)
  → Host 权限/Schema/Adapter 校验与执行
  → tool.result(callId, status, output|error, evidenceDigest)
  → stdin 回送同一 Agent 进程
```

关键约束：

- TraeX 子进程 stdin 从 `DEVNULL` 改为受控 PIPE。
- Agent 自报 `tool.result` 会以 `AGENT_EVENT_INVALID` 失败。
- `agent.completed` 前必须闭合全部 call；使用 Broker 后，最终 provenance 必须覆盖全部真实
  ToolResult call id。
- 删除 Browser completed 后重写 result 的专用分支。
- Browser read、受控本地只读、确定 lookup 走同一 Registry。
- Browser URL 继续执行 scheme、userinfo、DNS/private/loopback allowlist 校验。
- Tool request/result 的数据库事件只保存脱敏摘要和 evidence digest。

网页证据：E2E-8b 动态设置文章正文，断言 `tool.requested/tool.result`、Host evidence digest、
ConversationRun 最终回答和正文 sha256 全部来自 Playwright 返回值。

非网页证据：E2E-5 的季度 lookup 由 Host Adapter 返回整表和选值；Agent fixture 不再保存
季度表。单元测试另以随机文件内容验证 `read.local` 的 ToolResult 和最终回答一致。

## 4. C：渐进 Workflow Tool Broker 与 Selection-5

新增 [`workflow_tool_broker.py`](backend/app/application/workflow_tool_broker.py)，把以下应用
服务注册为同一 AgentHost 的受控工具，而不是内部 HTTP 或数据库直访：

- `workflow.search`：L0 top-k；
- `workflow.describe`：L1 契约；
- `workflow.describe_fragment`：L2 冻结端口/锁；
- `workflow.slice`：按受控节点摘要和意图选取候选；
- `workflow.compile_plan`：返回结构化 diagnostics。

DisclosureCall 现在由唯一 call id 绑定 request/response，并以 CAS 完成
`pending→succeeded`，不再按 session/operation 查找“最近一条 pending”。

Chat 主路径改为同一 TraeX run 内的渐进工具循环，移除 `run_fragment/compose` 的硬编码
422。`test_selection_five_actions_come_from_progressive_agent_loop` 分别证明：

- `run_full`：L0→L1；
- `run_fragment`：L0→L1，使用已披露 FragmentRef；
- `compose`：L0→L1→compile_plan，Compiler 成功后输出；
- `ask_user`：保存已选 ref 和缺失 bindings；
- `continue_agent`：L0 无合适路径后回通用 Agent。

新增持久 `PendingSelection`。E2E-4 证明用户回答 `Bob` 时不重新搜索“Bob”，而是在原
Disclosure Session 和 WorkflowRef 上补齐 `name`，启动原选择；单元测试额外断言 search
调用数不增加。

## 5. D/E：可发现固化与统一接力证据

Distillation 草稿现在派生并由 Compiler 校验：

- capability `name/summary/useWhen/doNotUseWhen/tags/examples`；
- `logicalWorkflowId`；
- `sourceConversationRunId/sourceTrajectoryDigest/distillationId/proposalDigest`；
- 受控 node summaries；
- 无副作用轨迹的 callable Fragment proposal；
- Browser/MCP/SkillX 只读节点的显式 recovery fallback 和专用受限 Recovery Agent。

AgentRun 增加 generation/token fencing。事件、终态、output 和 trajectory Artifact 都校验
同一 owner/generation/token；迟到 owner 不能提交结果或 Artifact。

新增通用 `TaskCheckpoint` envelope。Agent 执行失败或租约丢失时保存：

- source kind/id/digest；
- 已闭合 ToolResult/Agent step；
- 失败错误和未闭合 call；
- remaining plan；
- capability locks；
- committed side effects；
- outcome certainty 和用户上下文。

`UNKNOWN_OUTCOME` checkpoint 进入 `awaiting_user`，不盲目重放未知副作用。WorkflowRun
继续使用已验证的 `ExecutionCheckpoint`、Recovery generation/token 和 substitution 历史。

## 6. F：Replay Artifact 与 Revision Agent

Worker 对成功的 Browser/MCP/SkillX/Agent/LLM 只读节点自动捕获：

- 脱敏且不超过 64 KiB 的 fixture Artifact；
- node/run/source digest；
- input/output digest；
- capture policy；
- 已通过 Runtime output Schema 的结构化输出。

Replay 从 source Run 的 `ReplayFixture→Artifact` 加载并校验 sha256/output digest，不再从
业务 `run.input_json._replayFixtures` 读取。缺失 fixture 返回 `REPLAY_UNSUPPORTED`，损坏
证据返回 `REPLAY_EVIDENCE_CORRUPT`。

新增 `POST /failure-observations/{id}/revision-proposal`：

1. 锁定 source Run、WorkflowVersion、safe input 和 fixture digest；
2. 先执行 baseline reproduction；
3. 瞬态或不可复现时不调用 Revision Agent；
4. 可复现时只接受现有 `WorkflowPatch` Schema；
5. 重新执行 Patch 白名单和 Compiler 校验；
6. 只返回 proposal，`published=false`。

ReplayEvidence 增加 `fixtureDigest/baseVersionId/patchOfObservationId`；修订草稿 metadata 保存
`patchOfObservationId/replayEvidenceId/baseVersionId`。人工发布约束保持不变。

## 7. G：Artifact 与产品证据

- Artifact 使用 `run | conversation_run` 判别来源和统一 storageKey。
- Conversation trajectory 保存 `sourceKind/sourceId/storageKey/stagingKey`。
- 标准下载端点按 source kind 解析路径并校验 sha256。
- reconcile/GC 同时覆盖 Run 与 ConversationRun，可补落位、报告缺失和删除孤儿。
- 测试覆盖 trajectory 下载、篡改拒绝、从 staging 补落位和迟到 owner 无 Artifact。
- Chat 展示 Workflow Broker 的 L0/L1/L2、call id、状态、PendingSelection 和 diagnostics。
- Run detail 展示 lineage、checkpoint generation、FailureObservation classification/fingerprint、
  Replay fixture digest 和修订 lineage。

## 8. 门禁矩阵

| 门禁 | 本轮证据 |
|---|---|
| Build baseline | `make demo-check` 全绿；后端可导入；Alembic `0015` |
| Real Tool Broker | E2E-8b、E2E-5、`test_tool_broker.py` |
| Generic adapters | Browser、local read、lookup 共用 ToolRequest/ToolResult Registry |
| Progressive TraeX | `test_progressive_selection.py` 的 L0→L1/L2/compile_plan |
| Selection-5 | 五 action 参数化 Agent 循环测试 + 公开 execute-selection |
| Ask resume | E2E-4 + search 调用数不增加断言 |
| Distilled discovery | capability metadata、logical id、Manifest 搜索 |
| Distilled recovery | 自动 fallback/recovery preview 与既有 Recovery E2E |
| AgentRun handoff | TaskCheckpoint + generation/token + unknown→awaiting_user |
| Replay evidence | ReplayFixture Artifact、sha256/output digest、无 input fixture |
| Revision decision | Revision Agent baseline gate；瞬态/不可复现不提 Patch |
| Revised reuse | 受控 Patch、ReplayEvidence、人工发布和版本历史 E2E |
| Lineage | exploration→distillation→version→run→observation→replay→patch ids/digests |
| Artifact lifecycle | Conversation trajectory 下载、校验、reconcile、GC |
| Full-loop web | Browser 探索、固化、发布、换正文复用；恢复/修订共用 Runtime 证据协议 |
| Full-loop non-web | Host lookup 探索、固化、发布、换输入复用 |

## 9. 复现环境与命令

环境：

- macOS `26.5.2`，arm64；
- uv `0.11.6`；
- Python `3.13.13`；
- Node `v24.14.1`；
- npm `11.11.0`；
- Playwright `1.62.0`。

本次最终命令：

```bash
make demo-check
git diff --check
```

退出码均为 `0`。最终测试计数只取自上述本次执行输出，未沿用 008 报告。
