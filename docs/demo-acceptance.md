# Local Demo 验收规范

本文是 `NEXT_STEPS.md` M8 的可执行验收基线。每个场景必须通过真实 API、独立
Worker 和持久化数据库完成；仅存在类型、适配器或静态页面不计为通过。

| 场景 | 输入与路径 | 必须事件 | 通过条件 |
|---|---|---|---|
| E2E-1 确定性组件 | Form → Transform/SkillX Direct → Output | `run.created`、`node.*`、`run.completed` | 无模型配置仍成功，Token 为 0 |
| E2E-2 MCP 组件 | Form → 锁定 MCP ToolSnapshot → Output | `mcp.call.*` | Tool Schema/digest 锁定，模型调用为 0 |
| E2E-3 Hybrid 组件 | 固定 DAG → 单个 LLM → Output | `llm.call.completed` | 仅显式节点产生 Token，输出通过 Schema |
| E2E-4 对话调用组件 | 自然语言 → 候选/补参 → Workflow | 组件候选、`run.created` | Workflow 启动后 Agent 不接管锁定节点 |
| E2E-5 轨迹固化 | Agent Run → Distillation → Draft | `agent.*`、固化状态事件 | 草稿可编辑但不自动发布，新输入可复用 |
| E2E-6 浏览器提交 | 检查 → 填写 → 审批 → 提交 → 验证 | `browser.*`、`approval.*` | 授权一次性消费；页面、数据或身份变化即失效 |
| E2E-6b 页面漂移 | 审批后修改页面 → 重放 | `browser.*`、`approval.*` | 旧授权失效且不提交 |
| E2E-7 检查点接力 | Direct 失败 → Recovery → 剩余 DAG | `run.recovery_requested` | 原失败 attempt 不改写，非幂等副作用不重放 |
| E2E-7b Recovery crash | Worker 领取 checkpoint 后退出 → 新 Worker | `recovery.*`、lease heartbeat | 过期租约重领并完成；旧 Recovery NodeRun 留证 |
| E2E-7c Recovery question | Recovery → request_user → answer | `recovery.user_answered` | 同一 checkpoint 保存回答并续跑 |
| E2E-7d Recovery replan | Recovery → typed plan patch → new attempt | `recovery.*`、NodeRun | patch 经 Compiler 后执行；原失败 attempts 不改写 |
| E2E-8 动态网页读取 | Playwright read → summary → 页面变化 | `browser.read.*` | 第二次运行消费新正文 |
| E2E-8b 对话总结复用 | Conversation A Browser Agent → distill → 人工 publish → Conversation B 语义发现 → Runtime | `tool.requested/tool.result`、L0/L1、`run.*` | URL/正文变化后新对话发现首次 lineage 并成功；禁止创建 Component 后持 id 直调 |
| E2E-9 外部失败修订 | 外部读取真实失败 → immutable RevisionProposal/HumanPatch → ReplayEvidence → draft | `node.failed`、ReplayEvidence | 两个独立 Run 的契约证据达到阈值；真实 transport/timeout 不得获得 proposal；proof 锁定 base/patch/patched definition digest |
| E2E-9b 修订后复用 | Replay-approved draft → 人工审核 revision/digest → publish v2 → 原输入 + 新输入 | `run.*`、lineage | publish digest 必须等于 Replay-approved digest；v1 精确 ref 可解析，搜索仅返回 active v2 |
| E2E-10 渐进披露与组合 | search → describe → 两个 refs → PlanSnapshot | L0 → L1、`run.*` | 默认版本更新不改变已冻结 Run |
| E2E-11 Fragment safety | describe_fragment → run/非法裁剪 | L2、Compiler diagnostics | 只读闭包可运行；绕过审批/副作用的裁剪被拒 |
| E2E-12 固化建议 | 同 signature 多输入成功 → 接受建议 | suggestion、draft | 仅创建 draft，未经人工不发布 |
| Scale-1 Catalog 1000 | 1000 个已发布 Manifest → search | Registry 单元/集成门禁 | top-k≤8、总字节≤24KB、L0 不含 Schema/DAG |
| Fence-1 迟到 Recovery | A 超租约 → B 重领 → A 返回 | generation/token、两个 Recovery NodeRun | 仅 B 的决定、替代输出和终态可提交 |
| AgentRun handoff | AgentRun 只读 ToolResult → 失败 → pending TaskCheckpoint → Worker 崩溃 → 新 Worker continuation | `agent.handoff.*` | 新 Worker 领取 generation+1；claim handle 约束启动与终态；Host action ledger 复用结果且不增加外部调用 |
| Real Tool failure handoff | 首次 Browser 503/断连 → 有限重试耗尽 → Worker 重启 → 外部恢复 → continuation → distill/publish → 新对话复用 | `tool.result(status=failed)`、`agent.failed`、`agent.handoff.*`、L0/L1 | 主错误保持 Tool 标准 code；失败 Artifact 可校验；只重试失败 action，已完成 action 不重做 |
| Generic lookup continuation | lookup 成功 → transform 瞬态失败 → output 未运行 | `agent.plan`、`tool.result`、`agent.handoff.*` | 同一 ContinuationRequest 复用 lookup ledger，只执行 remaining transform/output |
| Generic file continuation | file/read 成功 → transform 失败 → Worker 重启 | `agent.plan`、PlanSnapshot Artifact、`agent.handoff.*` | 路径/内容参数化；恢复消费冻结 remaining plan，不缓存首次正文 |
| AgentRun unknown | Worker 租约丢失 → awaiting_user → answer → continuation | `agent.handoff.user_answered` | 回答只能消费一次；未确认的未知副作用不得继续 |
| Selection-5 | 五种 action → validate/execute | action 终态、diagnostics | 类型错配、未披露 ref 和超预算 fail closed |
| Disclosure-1 | 创建 session → L0/L1/L2 → 重启读取 | disclosure call audit | refs、level、剩余预算持久且跨 session 隔离 |
| Plan lifecycle | 过期前创建 Run → Plan 过期 → Worker 恢复 | plan status/source map | 禁止新 Run/固化，既有 Run 继续完成 |
| Catalog active | 发布 v2 → search v2 → resolve v1 ref | Manifest status | v1 不占 top-k，但精确 ref 仍可运行 |
| Real first browser | Agent browser.read → Browser Tool Broker | `tool.requested`、`tool.result` | Agent 输出逐字段 provenance 指向真实 ToolResult，正文非 TraeX fixture 常量 |
| Natural read-only entry | `/chat` message → continue_agent → Browser/lookup/file | `agent.run.created`、`tool.*` | 不传 `allowedCapabilities`；服务端只授予已启用的受控只读 Broker |
| Conversation result delivery | 新 Conversation → L0/L1 → Workflow Run terminal | `run.*`、持久 `runId` | succeeded output 或 failed code/message 直接显示在对话，刷新不丢失、不重复 |
| Real UI main flow | Chat → distill/check/publish → reuse → RunDetail | DOM、SSE、console | Vite/Chromium 真实交互；成功/失败详情不白屏，console 无未捕获异常 |

## 通用失败判定

- 重复执行同一副作用，或迟到 Worker 覆盖新 attempt。
- 未携带有效 disclosure session 调用 Registry，或跨 session 使用 ref。
- 过期 PlanSnapshot 创建新 Run、固化草稿或进入目录建议。
- Run 已显示 `cancelled`，Executor 仍继续并提交成功。
- WorkflowVersion 未锁定实际 Capability/MCP/接口版本。
- Secret 出现在数据库、事件、日志、API 或 Artifact。
- Browser L2/L3/L4 未按策略暂停或终止。
- 第二次复用由测试手工创建 Component、持有 id 或 WorkflowVersion id 绕过 Catalog 发现。
- AgentRun checkpoint 无消费者，或 continuation 重做已有 ToolResult/副作用。
- 外部失败没有 failure fixture，baseline 退化为 `REPLAY_UNSUPPORTED`。
- 冻结 `transport_error`/`timeout` 历史错误后直接设置
  `baselineReproduced=true`，或 occurrence 未达到两个独立 Run 就调用 Revision Agent。
- WorkflowPatch 创建新的 Workflow 行，或发布修订后 v1 与 v2 同时处于
  `published`/active；旧 Fragment 仍占 active 目录。
- publish 未携带用户审核的 `draftRevision/draftDigest`，或最终 publish digest
  不等于 ReplayEvidence 的 `patchedDefinitionDigest` 和 draft 的
  `replayApprovedDraftDigest`。
- Replay 通过后 timeout、selector、outputSchema 等任意 definition 内容发生变化，
  旧 proof 仍可发布；相同 Compiler digest 的幂等保存除外。
- 客户端沿用 `proposalId` 提交不同 Patch 却未创建显式 HumanPatch，或发布版本 lineage
  无法追溯 proposal/HumanPatch、ReplayEvidence 与 base version。
- TaskCheckpoint 启动、reconcile 或 user answer 未使用
  status/digest/owner/generation/token CAS，导致迟到 owner 创建第二 continuation 或写第二终态。
- completed-action ledger 只写在提示词中，Host 仍执行相同
  capability ref + 参数 digest + effect + idempotency key。
- `agent.completed.outputProvenance` 引用无效 path/digest，或输出值与确定性来源不一致。
- failed ToolResult 后的非法 `agent.completed`、子进程退出或次生关闭异常覆盖原始 Tool
  `code/message/retryable`；`protocolViolation` 只能作为附加审计信息。
- Agent 自报的 code/retryable 改变 Host `ErrorPolicy`，或带 `callId` 却无法对应 Host 已见的
  failed ToolResult。
- checkpoint 以 `resumeSameTask` 或原始 prompt 重规划冒充断点继续；缺少结构化计划的旧运行必须
  标为 `replan_required`。
- PlanSnapshot、step 状态或 Artifact 引用未进入 source digest，或完整性失败后静默重做原任务。
- 测试绕过 Alembic、API 或 Worker 主路径。

## Agent 终态与接力

- `agent.completed` 只表示成功，输出必须通过 Schema 且逐字段 provenance 指向成功
  ToolResult；失败文本不得伪装成成功输出。
- `agent.failed` 必须包含 `code/message/retryable/callId`。payload 形成 `reportedError`，其中
  `retryable` 仅作诊断提示。状态机只读取 Host `ErrorPolicy` 生成的 `effectiveError`。
- `callId` 存在时必须指向 Host 已见的 failed ToolResult，effective code/message/retryable 全取
  Host ToolResult 与 ErrorPolicy；无 ToolResult 时未知、Permission、Safety、Contract、budget 和
  invalid event 一律 fail closed。
- 共享 ErrorPolicy 将 `DEPENDENCY_UNAVAILABLE`、`TIMEOUT`、`RATE_LIMITED` 视为瞬态；
  只读或幂等 action 可按有限 budget/backoff 重试。Permission、Safety、Contract、非法事件
  fail closed，未知副作用进入人工确认。
- 重试耗尽后，可恢复错误创建 `pending` TaskCheckpoint；claim 后为 `running`，创建
  continuation 后为 `continuing`，最终随 continuation 进入 `succeeded/failed`。checkpoint
  分别保存 completed/failed/remaining plan steps、pending calls 和 action ledger，并在加载前
  校验 source digest、PlanSnapshot digest 与 Artifact id/size/sha256/evidence digest。

## Agent PlanSnapshot 与 Continuation

- Host accepted PlanSnapshot 是 continuation 的 execution source of truth。Agent proposal plan 只有通过
  capability/DAG/effect/budget 校验并持久化后才成为 accepted plan；checkpoint 只绑定 accepted digest。
- 结构化 `agent.plan` 使用 schema version、plan id、revision 和稳定 step id。每个 step 包含
  action/capability、依赖、参数引用、effect、idempotency key、输出名和 fallback。
- Host 校验能力锁、DAG、effect 和 step budget；结构化计划下每个 `tool.requested` 必须携带
  `planStepId`。计划作为不可变 `agent_plan_snapshot` Artifact 持久化。
- continuation HostRequest 必须携带 expected plan digest/revision、source checkpoint、允许的 remaining
  step set 与 step-state digest。首个 `agent.plan` 必须与 expected plan 完全相同；任何变化只能经
  绑定 source checkpoint/plan/state digest 的 typed ReplanPatch。
- `argumentRefs` 只允许 literal、user_input、step_output、artifact。Host 从 original input 和已验证
  ToolResult/Artifact 物化实参，并逐字段核对 action/capability/effect/idempotency/arguments；
  URL、路径、lookup key 或 prior output path 偏离均在 Adapter 前 fail closed。
- checkpoint 只从 PlanSnapshot 与 Host ToolResult 计算 `completed/failed/remaining`。无结构化
  计划的旧运行进入 `replan_required`；损坏计划进入 `integrity_failed`。
- `ContinuationRequest` 固定包含 original input、reported/effective error、已完成/失败输出、
  remaining plan、capability locks 和 attempt/generation budget。默认执行 remaining DAG；
  replan 只能提交 typed patch 并产生新 revision。
- completed step 只能命中 Host ledger；effect=none 的 failed step 可重试；未知副作用必须先由用户
  确认。总 continuation generation 默认上限为 3。
- Host 为每个 revision 保存 remaining/running/succeeded/failed/unknown/skipped 状态和全部 attempts；
  唯一键是 plan digest + step id + attempt，callId 只承担传输配对。succeeded step 禁止重发。
- revision 1 初始结构化计划不消耗 patch budget；legacy 首次生成 revision 1 消耗一次。
  相同 patch digest 幂等重放不重复扣减，不同 patch 超限返回 `REPLAN_BUDGET_EXHAUSTED`。
- 每个有 ToolResult Artifact 的 attempt 必须保存非空 `argumentsDigest`，且 event、Artifact metadata
  与 Artifact envelope 三方一致；缺失或冲突进入 `integrity_failed/terminal`。
- `claimGeneration` 是同一 checkpoint 的 lease fencing 代数；`continuationGeneration` 是业务接力链
  代数。Worker crash/reclaim 只增长前者，连续 continuation 失败才增长后者。
- legacy `replan_required` 必须先进入 `awaiting_user`；snapshot digest CAS 一次性确认后，Host 才允许
  在原 capability locks/replan budget 内生成 revision 1。
- 从原始 prompt 重新规划不是从断点继续，不得记录为 continuation 成功证据。

## 不可变修订 lineage

- `RevisionProposal`：observation/base version、Agent run、prompt version、fixture/baseline、
  proposal JSON 与 digest。
- `HumanPatch`：parent proposal、editor、Patch JSON 与 digest。
- `ReplayEvidence`：base definition、Patch、patched definition digest，以及 proposal 或
  HumanPatch ID。
- `WorkflowVersion`：`publishedFromDraftRevision/digest` 与完整 revision lineage；历史记录
  不原地修改。

## 质量门禁

```bash
make demo-check
make browser-ui-check
```

两个命令必须分别完成空库迁移、后端/前端门禁、进程级 E2E，以及真实
Vite/Chromium 用户主流程。Chromium 无权限时只能明确 skip 原因，不能用 mock 记为通过。
