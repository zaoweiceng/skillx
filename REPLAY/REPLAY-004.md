# NEXT_STEPS-004 执行回放（REPLAY-004）

依据：[`NEXT_STEPS-004.md`](NEXT_STEPS-004.md)、[`review/design-compliance-review-004.md`](review/design-compliance-review-004.md)、[`review/code-review-004.md`](review/code-review-004.md)。
执行范围：A（Agent 持久化长任务）、B（Host 权限/配额/事件契约）、C（MCP 分类不可变与运行锁定）、D（真实轨迹提炼）、E（最小可视化编辑闭环）、F（工程一致性收尾）。
非目标：鉴权、RBAC、多租户、生产级沙箱、分布式部署。

## 1. 总体结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff（app/tests/migrations/e2e/examples） | 通过 |
| 后端 Pytest | 124 passed（003 基线 85） |
| 空库 Alembic 升级 | 0001 → 0008 通过 |
| 前端 check / lint / test / build | 通过（23 passed，003 基线 13） |
| 黑盒 E2E（`pytest e2e`） | 9 passed（7 真实场景 + 2 harness 自测） |

核心结论：004 关闭了 review 指出的两个系统不变量断点——**长任务先持久化后执行**、**发布引用的能力快照不可变**——并把 Distillation 从“重跑 Agent”升级为“编译复用”，补齐了最小可视化编辑闭环。

## 2. A：Conversation Agent 改为持久化长任务

- `ConversationRun` 增加 `queued_at/started_at/finished_at`、`lease_owner/lease_expires_at/heartbeat_at`、`cancellation_requested_at`、`attempt`、`next_event_seq`、`config_json`（migration 0008）。状态集合 `queued → running → succeeded|failed|cancelled`。
- [`enqueue_agent_run`](backend/app/application/agent_run.py) 在短事务内创建 `queued` run + `agent.run.created` 事件 + 关联 assistant message 后返回；[`POST /conversations/{id}/agent-runs`](backend/app/api/routes.py) 改为 **202 + conversationRunId**，不等待 TraeX 终态。
- 独立 [`AgentRunWorker`](backend/app/application/agent_run.py) 领取租约、启动 TraeX、逐批提交事件与心跳；接入 [`app/worker.py`](backend/app/worker.py) 与工作流 Worker 同进程轮询，API 重启不影响已领取/待领取任务。
- 事件 seq 由数据库原子递增（`UPDATE ... RETURNING next_event_seq`），不由进程内局部变量维护。
- 新增 [`POST /conversation-runs/{id}/cancel`](backend/app/api/routes.py)：queued 直接 cancelled；running 记录取消请求，watcher 传播到子进程并写 `cancelled` 终态。
- 启动时 `recover_expired` 把过期租约的 running 运行按 `UNKNOWN_OUTCOME` 恢复为唯一失败终态，绝不盲目重跑可能已产生副作用的 Agent。
- 测试（[`test_agent_run_service.py`](backend/tests/test_agent_run_service.py)）：入队不执行、运行中事件可见、取消 queued 不执行、租约过期恢复、步数/Token 预算终止、202 handle 可固化。

## 3. B：Agent Host 权限、配额与事件契约

- 新增 Host policy 配置 `agent_host_capabilities/agent_max_steps/agent_max_total_tokens/agent_max_duration_seconds/agent_max_event_payload_bytes`（[`config.py`](backend/app/config.py)）。
- 实际能力 = `requested ∩ host_granted`（`resolve_effective_capabilities`）；越权能力记入 `agent.capabilities.narrowed` 并在运行中作为 `PERMISSION_DENIED` fail closed，请求方无法通过 body 扩权。
- Host 每收到 step/Token 事件独立比对上限，超限立即取消子进程并记 `STEP_BUDGET_EXCEEDED`/`TOKEN_BUDGET_EXCEEDED`；`asyncio.timeout` 强制时长。
- 新增 [`agent_events.py`](backend/app/application/agent_events.py) 判别联合事件契约：非对象 JSON、未知 type、非法 Token（StrictInt）、超大 payload、缺 callId/tool、started/completed 未配对、`agent.completed` 重复出现一律 `AGENT_EVENT_INVALID` fail closed。
- 原始轨迹写入 Artifact 文件，数据库事件只保存脱敏摘要与 `trajectoryArtifactUri` 引用。
- 测试（[`test_agent_events.py`](backend/tests/test_agent_events.py)）覆盖上述所有 fail-closed 分支。

## 4. C：MCP 分类不可变与运行锁定

- [`classify_snapshot`](backend/app/application/mcp.py) 不再原地 UPDATE：新建“分类修订” ServerVersion 并写入重分类后的**新** ToolSnapshot，旧快照 id/digest/effect 原样保留（已被 WorkflowVersion 锁定的历史版本语义不变）。
- discovery 中 annotations 仅作提示、审核状态一律 `unreviewed`，必须经本地 Registry 显式 `classify` 才能发布写节点。
- 发布时 [`validate_mcp_nodes`](backend/app/application/mcp.py) 保存完整 lock（id+digest+effect+idempotent+timeout）；运行前 [`resolve_mcp_node`](backend/app/application/mcp.py) 逐项核对，漂移抛 `MCPLockDriftError`，Orchestrator 映射为 `MCP_CONTRACT_DRIFT` 终态并不调用 Tool。
- 抽出唯一 [`auto_retry_allowed`](backend/app/application/retries.py)，发布校验、MCP Tool test 与 Orchestrator `_schedule_retry` 共用，消除测试页与运行时重试语义不一致。
- 测试（[`test_mcp_effects.py`](backend/tests/test_mcp_effects.py)、[`test_mcp_e2e.py`](backend/tests/test_mcp_e2e.py)、[`test_retries.py`](backend/tests/test_retries.py)）：分类产生新 id 且旧快照不变、未审核阻断发布、篡改 lock digest 运行 fail closed、重试矩阵。

## 5. D：从“重跑 Agent”升级为真实轨迹提炼

- `agent_run` 轨迹保存成功 action 序列（callId/tool/参数/结果）与 config，不再是单个携带原 prompt 的 Agent 节点。
- [`compile_agent_trajectory`](backend/app/application/distillation.py) 把“参数来自用户输入、结果为可查表字典”的 Tool 调用参数化为确定性 `lookup` Transform 节点（整表冻结进 config，`${input.message}` 作为 key）；无可确定化 action 时才退回受限 Agent 节点并标注 `NO_DETERMINISTIC_ACTION`。
- 新增 `TransformExecutor` 的 `lookup` 操作；TraeX fixture 增加 `deterministic/many_steps/big_tokens` 模式。
- E2E-5 改为：202 handle → 运行中可见 → 提炼确定节点（`directNodes≥1`、`estimatedLlmCallsAfter==0`、`toolLocks`）→ 人工发布 → **用不同输入 q2 得到不同结果且零模型调用**，证明“编译复用”而非“重跑 prompt”。

## 6. E：最小可视化编辑闭环

- 以 `WorkflowDefinition` 结构化 state 为唯一真相，JSON 降级为高级导入/导出视图（[`WorkflowEditorPage.tsx`](frontend/src/pages/WorkflowEditorPage.tsx)）。
- 新增纯函数库 [`workflowEdit.ts`](frontend/src/lib/workflowEdit.ts)：addNode/connectNodes/deleteNode/disconnectNodes/updateNode/setInputMapping；连线立即做环检测与可达性校验，非法连线不写入。
- 新增 [`EditableCanvas`](frontend/src/components/EditableCanvas.tsx)：鼠标连线、选中、Delete 删除节点/连线并回写结构化 state；NodePalette 点击新增 MVP 节点；可编辑 Inspector 修改名称/sideEffect/config/输入映射，VariablePicker 点击可达上游变量写入具体 mapping 字段。
- 保留 save→validate→publish→createComponent 全流程 busy 守卫与 diagnostics/风险摘要。
- 统一前后端表达式语法：支持 `字段[整数索引]`，VariablePicker 生成 `.tags` 与 `.tags[0]`（不再输出后端无法解析的 `[]`）；新增后端表达式契约测试与编译器索引引用/字段兼容告警。

## 7. F：工程一致性收尾

- **F-01 E2E Harness**：新增 [`e2e/conftest.py`](backend/e2e/conftest.py) 的 `pytest_runtest_makereport` 钩子，模块级 stack fixture 在 call 阶段失败时 `stop_stack(failed=True)` 保留现场并打印日志；显式关闭父进程日志句柄；新增受控失败自测 [`test_harness_selftest.py`](backend/e2e/test_harness_selftest.py)。
- **F-02 Artifact 原子性**：改为“先提交数据库、后从暂存原子落位”，消除“文件已落位、DB 未提交”孤儿窗口；`promote_staged_artifact` 幂等；新增 [`reconcile_run_artifacts`](backend/app/application/orchestrator.py) GC 双向扫描（补落位缺失、回收孤儿、报告不可恢复缺失），保留 stagingKey 供恢复；补三类恢复测试。
- **F-03 已知 remaining**：
  - MCP Streamable HTTP 会话过期（404）后自动重新握手重连（[`adapters.py`](backend/app/runtime/adapters.py) + loopback 测试）；流式增量消费与 Last-Event-ID 断点续传仍为 `partial`。
  - ContractPatch 人工审核后 [`apply_contract_patch`](backend/app/runtime/contract_patch.py) 应用到草稿（仅字段契约、L4 安全域不可改），新增 [`POST /workflows/{id}/contract-patch`](backend/app/api/routes.py) 与前端 `applyContractPatch`。
  - 节点间 JSON Schema 兼容检查（编译器 `OUTPUT_FIELD_NOT_IN_SCHEMA` 告警）已落地；完整的 WorkflowVersion diff UI 仍未做。

## 8. 004 总验收矩阵

| 目标 | 证据 |
|---|---|
| Agent 长任务 | 202 handle、运行中 committed events、取消、API/Worker 中断按 UNKNOWN_OUTCOME 恢复（test_agent_run_service.py、E2E-5） |
| Agent 安全 | Host 权限交集、step/Token/time 强制、畸形事件 fail closed（test_agent_events.py） |
| MCP 不可变 | 分类产生新 Snapshot；旧 Workflow lock 不变；运行前 digest 校验（test_mcp_effects.py、test_mcp_e2e.py） |
| 真实固化 | Tool action → 确定 lookup 节点；不同输入不同结果；Agent/Token 调用降为 0（test_distillation.py、E2E-5） |
| 可视化编辑 | 不改 JSON 完成新增、映射、连线、校验、发布（workflowEdit.test.ts、WorkflowEditorPage.test.tsx） |
| Browser | 现有真实 Playwright 单次提交与漂移失效回归保持（E2E-6/6b） |
| 工程恢复 | E2E 失败留日志（harness 自测）；Artifact commit 失败可恢复/可 GC（test_artifact_staging.py） |
| 总门禁 | `make demo-check` 通过；新增中断/不可变性/预算测试进入默认门禁 |

## 9. 遗留（显式非阻断）

- MCP Streamable HTTP 流式增量消费与 Last-Event-ID 断点续传（当前仅实现会话过期重连）。
- WorkflowVersion 之间的可视化 diff UI（后端已有依赖 lock 与字段兼容告警）。
- 完整拖放（当前为点击新增 + 鼠标连线/删除，未做从调色板拖拽落点）。
