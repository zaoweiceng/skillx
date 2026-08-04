# NEXT_STEPS-005 执行回放（REPLAY-005）

依据：[`NEXT_STEPS-005.md`](NEXT_STEPS-005.md)、[`review/design-compliance-review-005.md`](review/design-compliance-review-005.md)、[`review/code-review-005.md`](review/code-review-005.md)。
执行范围：A（统一 Agent Host 与审计边界）、B（MCP 多 Tool 不可变版本）、C（通用 NormalizedAction IR 与 Action Compiler）、D（通用 ExecutionCheckpoint 与显式 fallback）、E（失败可复现性与受控 WorkflowPatch）、F（两类通配黑盒验收）、G（产品闭环与剩余工程问题）。
非目标：鉴权、RBAC、多租户、生产级沙箱、分布式部署。

## 1. 总体结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff（app/tests/migrations/e2e/examples） | 通过 |
| 后端 Pytest | 169 passed（004 基线 124） |
| 空库 Alembic 升级 | 0001 → 0010 通过 |
| 前端 check / lint / test / build | 通过（28 passed，004 基线 23） |
| 黑盒 E2E（`pytest e2e`） | 10 passed（8 真实场景 + 2 harness 自测） |

核心结论：005 修复了 code-review 指出的两个系统级断点——**同一 TraeX 信任边界不再有两套安全实现**、**Distillation 不再把任意字典误编译为 lookup 且不丢动作**——并新增了三层此前缺失的能力：运行框架无关的 **NormalizedAction IR + Compiler 裁决**、**持久化 ExecutionCheckpoint 显式 fallback 接力**、**基于稳定 failureFingerprint 的受控 WorkflowPatch**。八项系统不变量全部满足，README 将“通用 Agent 探索与固化”“通用失败接力”标为 `available`。

## 2. A：统一 Agent Host 与审计边界

- 提取唯一 [`AgentHost.execute(request, event_sink, cancellation)`](backend/app/application/agent_host.py)：ConversationRun（[`agent_run.py`](backend/app/application/agent_run.py)）与 Workflow `AgentExecutor`（[`executors.py`](backend/app/runtime/executors.py)）都调用它，删除 Workflow Agent 内的第二套宽松事件循环与 `AgentToolViolation`。相同畸形输入在两个入口得到相同错误码。
- 统一执行 `requested ∩ host_granted`、事件判别联合、callId 配对、step/Token/time/payload 上限、输出 Schema 与取消。requested 按“任务所需集合”语义：存在 denied 时执行前终止 `PERMISSION_DENIED`，不产生误导性成功（P2-1）。
- 事件双投影（[`agent_events.py`](backend/app/application/agent_events.py)）：`validate_pair` 同时产出内部 `runtime_event`（完整 payload，仅进程内）与公开 `audit_event`（callId、能力锁、字段名/摘要、字节数、Artifact id）。大小限流改用 canonical JSON 的 **UTF-8 bytes**（`_canonical_bytes`）而非 `len(str(...))`，多字节内容不再绕过上限；`browser.action.started/completed` 与 `tool.*` 共用配对校验（P1-4）。
- Conversation Agent 轨迹接入 Artifact 表：`_stage_trajectory`→commit→`_promote_trajectory`，返回 `artifact://` URI + sha256，删除 API 中的绝对 `file://`（P2-2）。Worker 不再吞未知异常，改 `logger.exception` 记录 worker/run 上下文（P2-3）；启动与周期任务实际调用 `reconcile_recent_runs` 补落位提交后未完成的 Artifact 与轨迹（P2-4）。
- 测试：[`test_agent_host_parity.py`](backend/tests/test_agent_host_parity.py)（两入口对权限/预算/畸形事件/取消的终态一致）、[`test_agent_events.py`](backend/tests/test_agent_events.py)（双投影与 UTF-8 限流）。

## 3. B：修复 MCP 多 Tool 不可变版本

- [`classify_snapshot`](backend/app/application/mcp.py) 分类修订在同一事务克隆源 ServerVersion 的**全部** ToolSnapshot，只替换目标 Tool 的审核 policy；ServerVersion digest 覆盖完整、稳定排序的 Snapshot 集合与 policy，`current` 只在完整版本构建成功后切换（P1-3）。
- 分离两个 digest：`contract_digest`（远端 descriptor，migration 0009）与 `digest`（含 effect/idempotent 的完整契约）。`discover_tools` 比较 `contract_digest` 判定远端漂移，因此常见 `unknown → read/write` 本地审核不再被重复 discovery 误判为 contract drift；任一 Tool 的真实远端契约变化仍 fail closed。
- 测试：[`test_mcp_multi_tool.py`](backend/tests/test_mcp_multi_tool.py)（两 Tool Server 分类其一后新 current 含全部 Tool、旧版本不变、重复 discovery 稳定）。

## 4. C：通用 NormalizedAction IR 与 Action Compiler

- 新增运行框架无关的 [`distillation_ir.py`](backend/app/application/distillation_ir.py)：`NormalizedAction`（actionId/kind、capabilityRef、inputBindings+provenance、outputSchema、effect/idempotent、replayability、observations）；`build_action_ir(trajectory)` 缺 callId/能力名或输出无法关联时抛 `TrajectoryAnalysisError`（分析失败而非猜测）。
- 新增可扩展 [`action_compilers.py`](backend/app/application/action_compilers.py) `ActionCompilerRegistry`，每个 action kind 返回 `compiled | agent | rejected`：
  - `LockedCapabilityCompiler`（MCP/SkillX）：必须解析到 registryId+version+digest 才确定化，否则退回 Agent。
  - `TransformCompiler`：只有可证明的纯函数/查表（table + 用户输入 key）才确定化；删除“任意 dict 即 lookup”heuristic，无法证明返回 `rejected UNPROVEN_TRANSFORM`。
  - `BrowserActionCompiler`：需 `observations.pageContract`，固化为抽象 `browser` 能力与 Page/Action Contract，不暴露 Playwright 私有对象。
  - `ModelCompiler`：LLM/Agent 保留最小输入、提示词模板、输出 Schema、Tool 白名单与预算；未注册 kind → 显式 Agent 节点。
- Agent 提出 `DistillationProposal`，Compiler 逐项裁决：拒绝无能力锁、错误副作用、无法证明参数来源或 Schema 不兼容的确定化；未知动作保留为受限 Agent 节点而非丢弃；混合轨迹保留“确定前缀 + 局部 Agent/LLM + 确定后缀”。`toolLocks` 改为结构化 `capabilityLocks`，分析展示 direct/agent 节点数、未支持动作、风险与预计成本（P1-1）。
- 测试：[`test_action_compilers.py`](backend/tests/test_action_compilers.py)、重写的 [`test_distillation.py`](backend/tests/test_distillation.py)（真实 `{title, content}` 不误编译为 lookup、混合轨迹不丢动作、多输出走运行时 Schema）。

## 5. D：通用 ExecutionCheckpoint 与显式 fallback

- 领域模型（[`domain/workflow.py`](backend/app/domain/workflow.py)）新增 `FallbackPolicy`（on/target/requireUserApproval）与 `WorkflowNode.fallback`、`NON_RECOVERABLE_ERROR_CODES`；删除无效的 `NodeResult.resume_token`。
- 编译器（[`compiler.py`](backend/app/application/compiler.py)）`_validate_fallbacks` 校验 target 存在且为 agent、只处理声明的错误码、无 fallback 环、target 有最小 `allowedCapabilities`；`PERMISSION_DENIED/AUTHENTICATION_FAILED/CONTRACT_VIOLATION/UNKNOWN_OUTCOME` 需显式 `requireUserApproval` 才能进入恢复；`_validate_reachability` 计入 fallback target。
- 新增持久化 `ExecutionCheckpoint`（migration 0010）与 [`recovery.py`](backend/app/application/recovery.py)：`build_checkpoint` 冻结已完成输出/Artifact 引用、已提交非幂等副作用、失败输入/标准错误、由已发布 DAG 计算的 remaining 子图、能力锁与 fallback 受限配置；`digest_json` 固定内容，`verify_checkpoint` 恢复前核验。
- `run_recovery_agent` 走 `AgentHost`，prompt 由平台模板 + checkpoint 摘要生成（不拼整库/原始私有轨迹），输出严格匹配 `RecoveryDecision`（`resume_remaining | provide_failed_output | request_user | abort`）。`apply_recovery_decision` 从 remaining 子图继续调度，已成功 NodeRun 不重建，已完成非幂等副作用调用次数不变；无法证明结果进入人工确认。
- Orchestrator（[`orchestrator.py`](backend/app/application/orchestrator.py)）`_handle_fatal`：命中 fallback 且错误码可自动恢复时 `build_checkpoint` + `status=recovering`（对同一失败 attempt 幂等），否则按原语义 fail run；`advance_run` 对 recovering 让位；[`RecoveryDriver`](backend/app/application/recovery.py) 与 WorkerService/AgentRunWorker 同进程轮询。Direct/MCP/Browser 失败不隐式升级为 Agent。
- 测试：[`test_recovery.py`](backend/tests/test_recovery.py)（冻结/接力/不重放副作用、request_user、篡改 fail closed、checkpoint 幂等、二次应用不复活终态、取消 recovering 同步取消 checkpoint）。

## 6. E：失败可复现性与受控 WorkflowPatch

- [`workflow_patch.py`](backend/app/application/workflow_patch.py)：`failure_fingerprint` 覆盖版本/节点 id/type/能力锁/错误码/稳定契约摘要，排除时间戳、随机 id、原始 Secret；`classify_failure` 区分 `transient/contract_drift/deterministic_input/platform_bug/unknown`；`should_propose_patch` 仅对可复现非瞬态故障返回 True——一次网络波动不提版本修改。
- 通用 `WorkflowPatch` 只表达节点/边/配置/契约受限变更（`_ALLOWED_NODE_FIELDS`），`apply_workflow_patch` 拒绝 `command/secretRef/token` 等敏感/越权字段（`_FORBIDDEN_CONFIG_KEYS`），不得改 type/sideEffect/idempotent/permissions 或扩权。
- [`POST /workflow-versions/{id}/workflow-patch`](backend/app/api/routes.py) 应用总是创建 draft、Compiler 回放校验并返回 diagnostics/publishable，旧 WorkflowVersion 永不变；ContractPatch 作为其 Browser 子类型共用同一发布治理。
- 测试：[`test_workflow_patch.py`](backend/tests/test_workflow_patch.py)（指纹稳定、瞬态无 patch、可复现漂移生成 draft、拒绝越权字段、并发应用生成独立 draft 且基线不变）。

## 7. F：两类通配黑盒验收

- [`test_wildcard_pipeline.py`](backend/tests/test_wildcard_pipeline.py)：证明网页与非网页任务共用同一 `NormalizedAction`/`DistillationProposal`/Compiler/Checkpoint/`RecoveryDecision`/`WorkflowPatch` 类型，网页只是一个 Action Adapter，不存在两套独立恢复实现。
- E2E-5（非网页样例）：确定化提炼 → 人工发布 → 用不同输入运行，结果随输入变化且零模型调用（`capabilityLocks`/`agentNodes`/`estimatedLlmCallsAfter==0`）。
- E2E-7（[`test_demo_e2e.py`](backend/e2e/test_demo_e2e.py)）：真实 API + Worker，risky 节点重试耗尽 → checkpoint → 恢复 Agent 从剩余子图接力 → 成功；断言出现 `run.recovery_requested` 且 sensitive（非幂等副作用）只执行一次。[`executors.py`](backend/app/runtime/executors.py) 新增 `fail_dependency` 注入可重试失败；[`seed.py`](backend/app/seed.py) 新增“检查点接力演示”组件。

## 8. G：产品闭环与剩余工程问题

- conversationId 写入 `/chat/:id` 路由（[`App.tsx`](frontend/src/App.tsx)、[`ChatPage.tsx`](frontend/src/pages/ChatPage.tsx)），刷新可直接恢复同一会话，不再每次挂载创建新对话；assistant Agent 消息按 Run 终态渲染，只有 `succeeded` 展示“固化为组件”，其余状态显式提示不可固化；候选卡片绑定产生它的来源用户消息，不再取全局最近 prompt（P2-5）。
- 编辑器（[`WorkflowEditorPage.tsx`](frontend/src/pages/WorkflowEditorPage.tsx)）新增 `FallbackEditor`：勾选可恢复错误码后写入 `fallback.on/target/requireUserApproval`，target 仅可选 agent 节点，保留完整发布 busy guard。
- 运行详情（[`RunDetailPage.tsx`](frontend/src/pages/RunDetailPage.tsx)）新增检查点接力视图（已完成节点/剩余子图/已提交副作用/恢复决定，脱敏）与受控 WorkflowPatch 治理入口（对失败节点提补丁 → 展示 replay diagnostics → 打开 draft 审核 diff 并发布）；后端 `GET /runs/{id}` 暴露 `checkpoints` 证据。
- 补测：后端 [`test_observability.py`](backend/tests/test_observability.py) 检查点证据、[`test_recovery.py`](backend/tests/test_recovery.py) 取消/重复恢复、[`test_workflow_patch.py`](backend/tests/test_workflow_patch.py) 并发应用；前端 [`ChatPage.test.tsx`](frontend/src/pages/ChatPage.test.tsx)（路由恢复/来源绑定/仅 succeeded 可固化）、[`WorkflowEditorPage.test.tsx`](frontend/src/pages/WorkflowEditorPage.test.tsx)（fallback 配置）、[`RunDetailPage.test.tsx`](frontend/src/pages/RunDetailPage.test.tsx)（恢复视图/补丁流程）。

## 9. 005 门禁与不变量矩阵（§10）

| 新门禁 | 证据 |
|---|---|
| Agent Host parity | test_agent_host_parity.py：两入口对权限/预算/畸形事件/取消终态一致 |
| MCP multi-tool revision | test_mcp_multi_tool.py：分类后完整 current、旧版本不变、重复 discovery 稳定 |
| Mixed distillation | test_action_compilers.py、test_distillation.py：真实能力锁、动作不丢、动态输入不读旧缓存 |
| Generic checkpoint recovery | test_recovery.py、E2E-7：已完成节点不重放、剩余子图接力、未知结果需确认 |
| Patch governance | test_workflow_patch.py：瞬态无 patch、可复现才生成 draft、旧版本不变 |
| Web + non-web E2E | test_wildcard_pipeline.py、E2E-5/6/7：同一框架覆盖两类任务，网页仅是 Adapter |
| Audit/Artifact | test_agent_events.py、reconcile：Secret 不落事件；Agent Artifact 可自动 reconcile |

八项系统不变量全部满足：网页只是 Adapter；Compiler 最终裁决；未识别动作不删除；fallback 显式且不隐式升级；恢复只消费 checkpoint；非幂等副作用不重放；Agent 只提 WorkflowPatch 草稿、旧版本不变；事件/Artifact 不存 Secret 明文。

## 10. 遗留（显式非阻断，继续标记 partial）

- MCP Streamable HTTP 流式增量消费与 Last-Event-ID 断点续传（当前仅实现会话过期重连）。
- WorkflowVersion 之间的可视化 diff UI（后端已有 replay diagnostics 与依赖 lock；draft 打开后仍靠人工比对）。
- 完整从调色板拖拽落点（当前为点击新增 + 鼠标连线/删除）。
