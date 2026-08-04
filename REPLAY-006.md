# NEXT_STEPS-006 执行回放（REPLAY-006）

依据：[`NEXT_STEPS-006.md`](NEXT_STEPS-006.md)、[`review/design-compliance-review-006.md`](review/design-compliance-review-006.md)、[`review/code-review-006.md`](review/code-review-006.md)、[`REPLAY-005.md`](REPLAY-005.md)。
执行范围：A（重建 fallback 执行模型）、B（标准 checkpoint 与不可变恢复历史）、C（Action IR 生成可执行 mixed DAG）、D（失败证据接入受控 WorkflowPatch）、E（Host/轨迹/迁移/Worker 边界）、F（真实验收场景）、G（产品与文档收口）。
非目标：鉴权、RBAC、多租户、生产级沙箱、分布式部署。

## 1. 总体结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff（app/tests/migrations/e2e/examples） | 通过 |
| 后端 Pytest | 188 passed（005 基线 169） |
| 空库 Alembic 升级 | 0001 → 0012 通过 |
| 带 MCP 数据 0008 → head 数据迁移 | 通过（remote-only digest 重算，不伪漂移） |
| 前端 check / lint / test / build | 通过（28 passed） |
| 黑盒 E2E（`pytest e2e`） | 12 passed（10 场景 + 2 harness 自测） |

核心结论：006 把 005 建立的“通用框架骨架”变成了**可端到端复用与恢复的通配工作流**，修复了两份 review 指出的三个核心断点——**固化图无法传递前序动作输出**、**fallback target 会在正常路径提前执行且恢复篡改历史**、**可复现性判断与真实 replay 未接入生产**。首次探索现在能被正确固化（真实数据依赖），第二次能读取变化后的真实数据并交给 TraeX，意外中断时携带上下文从剩余节点续跑（原失败 attempt 不可变），只在可复现且真实回放通过时受控修订。

## 2. A：重建 fallback 执行模型

- **fallback-only 激活语义**（[`recovery.py`](backend/app/application/recovery.py)、[`orchestrator.py`](backend/app/application/orchestrator.py)）：新增 `_fallback_only_targets(definition)`，`advance_run` 在正常调度中排除所有被声明为 `fallback.target` 的节点——它们不再作为无入边节点被提前调度。命中已发布 fallback 后由 `RecoveryDriver` 为 target 创建唯一 Recovery NodeRun 并执行该 NodeRun，而不是另起无归属 Agent 调用。运行详情能区分 failed attempt、recovery attempt（`recoveryNodeRunId`）、substitution 与下游续跑（P1-2）。
- **原子领取与租约**（migration 0011）：`ExecutionCheckpoint` 增加 `(failed_node_run_id, attempt)` 唯一约束与 `lease_owner/lease_expires_at/heartbeat_at` 列。`RecoveryDriver.poll_once` 用单条条件更新 CAS 把 `pending→running` 领取；未领取成功的 Worker 绝不调用 Agent，因此双 Worker 竞争只产生一次 Agent 调用。Recovery 调用携带持久 idempotency key（checkpoint id）（P1-6）。
- **人工审批 fallback**（A-03 / P2-3）：`requireUserApproval=true` 命中后 `_handle_fatal` 先 `build_checkpoint`（`approval_status=pending`）并进入 `waiting_approval`，不直接失败；创建 `recovery.approval` Approval 展示错误码、能力集合、已提交副作用与上下文摘要。[`routes.py`](backend/app/api/routes.py) 的 `approve` 分支 `_approve_recovery`（checkpoint→approved、run→recovering，走同一 Recovery claim），`reject` 保留原失败终态、checkpoint→rejected。
- 测试（[`test_recovery.py`](backend/tests/test_recovery.py)）：`test_concurrent_recovery_drivers_claim_once`（双 Worker 只一次调用与唯一 Recovery NodeRun）、`test_approval_gated_fallback_waits_then_recovers`、`test_rejected_recovery_keeps_failure_terminal`。

## 3. B：标准 checkpoint 与不可变恢复历史

- **可执行 checkpoint**（`build_checkpoint`）：现冻结已完成节点结构化输出**值**、失败节点解析输入/完整标准错误/重试历史/outcome certainty/outputSchema、能力锁、由已发布 DAG 计算的**剩余节点定义与边**（排除 fallback-only target 与已完成节点）、workflow 输出契约、已提交非幂等副作用与幂等键；`digest_json` 覆盖结构化 snapshot，`verify_checkpoint` 恢复前核验（P1-3）。
- **真正把上下文交给 TraeX**：`build_recovery_prompt` 现包含失败事实、失败输入、**已完成节点的实际输出值（脱敏）**、剩余子图与输出契约，而非仅节点 id 列表。`RecoveryDecision` 改为判别联合 `provide_substitution | retry_replanned | request_user | abort`（`parse_recovery_decision` 严格校验，`substitution` 仅在 `provide_substitution` 时允许）；substitution 用失败节点 outputSchema 校验，类型不匹配 fail closed。
- **不可变 attempt 与续跑**（migration 0011）：**原失败 NodeRun 永不改写**；新增 `OutputSubstitution` 表记录恢复提供的替代输出。`advance_run` 通过 `load_substitutions` 让下游节点显式从替代输出解析被替代节点值，失败 NodeRun 保持 `failed`，已成功 NodeRun 不重建，已完成非幂等副作用不重放。
- 测试：`test_checkpoint_freezes_completed_outputs_and_remaining`、`test_recovery_prompt_contains_completed_values`、`test_recovery_output_depends_on_random_checkpoint_value`（恢复输出必须包含随机前序值，否则失败）、`test_recovery_resumes_without_replaying_side_effects`（原失败 attempt 不可变 + substitution 续跑 + sensitive 只执行一次）、`test_substitution_schema_mismatch_fails_closed`。

## 4. C：让 Action IR 生成真正可执行的 mixed DAG

- **显式输入 provenance**（[`agent_host.py`](backend/app/application/agent_host.py)、[`distillation_ir.py`](backend/app/application/distillation_ir.py)）：`_normalize_action` 透传每个参数的 `argumentBindings`（`user_input | prior_output(actionId,path) | artifact(id,path) | constant`）；`_classify_bindings` 消费显式声明，不再靠“值是否等于 prompt”猜测；`prior_output` 引用未知 action 抛 `TrajectoryAnalysisError`。`build_action_ir` 从 bindings 推导 `dependsOn`（真实 action DAG，不再强制单链）。
- **Proposal 与 Compiler 闭环**（[`distillation.py`](backend/app/application/distillation.py)）：`compile_agent_trajectory` 接受可选 `DistillationProposal`，`_validate_proposal` 校验其覆盖每个 action 恰好一次；`proposed_strategy` 真正参与裁决（`agent` 建议强制保留 Agent 节点）。`CompileContext` 加 `action_node_ids` 映射，`_input_mapping_from_bindings` 把 `prior_output` 翻译为 `${上游节点.output.path}`；**所有编译器（含 Agent/LLM via `_agent_input`）都从 bindings 生成 input mapping**，前序输出真正传给后续节点；按 `dependsOn` 建边并把叶子连到 output（P1-1）。
- **Browser Read/Extract 只是一个 Adapter**（[`browser.py`](backend/app/runtime/browser.py)、[`playwright_browser.py`](backend/app/runtime/playwright_browser.py)、[`executors.py`](backend/app/runtime/executors.py)）：新增 `BrowserReadContract`（URL/正文 selector/结构化字段 selector/输出 Schema/超时/可重试性）；`PlaywrightPageSession.read_and_extract` 导航后抽取正文与字段；`BrowserContractExecutor.execute` 对 `action∈(read,extract)` 走 `_read`（真实 Playwright 或 `observedContent` 测试 adapter，`url_matches` 安全边界，`outputSchema` 校验）。不把 Playwright page/locator 写入 IR；URL 来自输入 → 第二次以新输入重新取数，不缓存首次结果。
- 测试：[`test_wildcard_pipeline.py`](backend/tests/test_wildcard_pipeline.py)（删除 `... or True`，断言 Agent 总结节点 `text` 引用 browser 节点 `content` 输出与真实数据依赖边）、[`test_browser_read.py`](backend/tests/test_browser_read.py)（抽取正文/字段、新输入反映新正文、URL 越界拒绝、输出 Schema fail closed）、[`test_distillation.py`](backend/tests/test_distillation.py)（proposal 参与裁决、未覆盖动作被拒绝）。

## 5. D：把失败证据接入受控 WorkflowPatch

- **持久可复现性**（migration 0012，[`workflow_patch.py`](backend/app/application/workflow_patch.py)）：新增 `FailureObservation`（source run/checkpoint、稳定 fingerprint、classification、契约摘要、recovered、occurrence）。`record_failure_observation` 按 fingerprint 聚合累加 occurrence；`orchestrator._handle_fatal` 对不进入恢复的致命失败记录观测。`observation_eligibility` 由后端事实（occurrence≥`REPRODUCIBLE_THRESHOLD` + classification）计算资格并返回理由——API 不接受前端自报 `reproducible=true`（P1-4）。
- **能力不可扩张的 Patch DSL**：`_ALLOWED_CONFIG_KEYS` 字段级白名单（拒绝任意 `config` 浅合并）；`_FORBIDDEN_CONFIG_KEYS` 键名规范化（小写去分隔符）后递归拒绝 `allowedCapabilities/mcpToolSnapshotId/capabilityVersionId/model/secret/permissions/sideEffect/idempotent/risk` 等；`_ALLOWED_NODE_FIELDS` 移除 `fallback`。
- **真实 replay 与统一治理**（新增 [`replay.py`](backend/app/application/replay.py)）：`run_isolated_replay` 在隔离内存环境用 source checkpoint 的安全输入运行 patched 图到原失败点及必要下游，复用真实 NodeExecutor；非幂等副作用节点在 replay 中以模拟成功输出代替、不重放。[`POST /workflow-versions/{id}/workflow-patch`](backend/app/api/routes.py) 必须带 `sourceRunId`（或 `failureFingerprint`）→ 查 observation → 算 eligibility → 应用受控补丁 → **真实 replay 通过后才创建 draft**，旧版本不变，仍需人工发布；ContractPatch 为其 Browser 子类型。
- 测试：[`test_workflow_patch.py`](backend/tests/test_workflow_patch.py)（eligibility 需可复现、拒绝能力锁扩张/fallback 字段、无观测/瞬态单次失败被拒、可复现失败经真实 replay 通过才建 draft 且旧版本不变）。

## 6. E：修复 Host、轨迹、迁移和 Worker 边界

- **Agent Host**（[`agent_host.py`](backend/app/application/agent_host.py)）：`agent.completed.totalTokens` 与步骤累计做一致性校验（非负、不小于可信累计值），并重新执行 Host 上限。事件流与 cancellation token 用 `asyncio.wait(FIRST_COMPLETED)`（`_await_cancel`）竞争，子进程挂住不再产出事件时取消也能立即终止（测试快约 2×）。
- **轨迹与 Artifact**（[`agent_run.py`](backend/app/application/agent_run.py)）：`build_trajectory`/`_desensitize_action`/`_stage_trajectory` 用 `redact` 对 actions/output/落盘 runtime 事件脱敏——Secret 原值不进入 `trajectory_json`、事件 payload 或磁盘字节（默认 Local Demo 也脱敏）。
- **数据迁移与异常边界**：0009 迁移改为用运行时 canonical 算法从旧行字段重算 remote-only `contract_digest`（不再直接回填含 effect/idempotent 的旧 digest），非空 0008 升级后首次 discovery 不伪漂移（P1-7）。`execute_claimed` 将未知 Executor 异常转换为结构化 `EXECUTOR_INTERNAL_ERROR`（`logger.exception` 记录，不击穿 Worker）（P2-4）。run API `checkpoints` 修正 `fallbackTarget=fallbackConfig.target`（原返回 failedNode）并新增 `approvalStatus`/`recoveryNodeRunId`。
- 测试：[`test_agent_run_service.py`](backend/tests/test_agent_run_service.py)（`test_secret_never_appears_in_trajectory_or_disk` 扫描 DB 轨迹/事件/磁盘字节）、[`test_migrations.py`](backend/tests/test_migrations.py)（非空 0008→head 重算 remote-only digest 且不等于旧 digest）。

## 7. F：真实验收场景

- **动态网页总结**（E2E-8）：[`local_form_site.py`](examples/local_form_site.py) 加 `/article` + `/__set-article`（可变正文）；[`seed.py`](backend/app/seed.py) 加“动态网页总结”组件（Browser Read → fake Agent `echoInputField` 回显正文），需 `SKILLX_DEMO_ARTICLE_URL`+TraeX。`test_e2e_8_dynamic_web_summary_reflects_page_change`：真实 Playwright 读取初始正文并总结，修改页面后第二次运行输出依赖新正文（禁止使用首次结果/常量），并断言 `browser.read.completed` 事件。
- **非网页通配样例**（E2E-5，沿用）：确定化提炼 → 发布 → 换输入运行结果随数据变化、零模型调用；与网页样例共用同一 IR/Compiler/Checkpoint/RecoveryDecision/WorkflowPatch。
- **并发、审批与修订**：`test_concurrent_recovery_drivers_claim_once`（双 Worker 一次调用）、`test_approval_gated_fallback_waits_then_recovers` / `test_rejected_recovery_keeps_failure_terminal`（审批批准/拒绝路径）、`test_recovery_output_depends_on_random_checkpoint_value`（恢复输出依赖随机前序值）、E2E-9（`test_e2e_9_reproducible_failure_gated_patch`：无失败观测的补丁被拒 `PATCH_NOT_ELIGIBLE`）。

## 8. G：产品与文档收口

- 运行详情（[`RunDetailPage.tsx`](frontend/src/pages/RunDetailPage.tsx)）：检查点视图展示 `fallbackTarget`、审批状态与唯一 Recovery NodeRun；`proposePatch` 携带 `sourceRunId` 并按新的 `{sourceRunId, patch}` 请求体调用，展示 eligibility/replay 结果。[`client.ts`](frontend/src/api/client.ts) 的 `applyWorkflowPatch` 与 `checkpoints` 类型同步更新。
- README 六行能力描述更新为 006 真实语义（provenance 数据依赖、可执行 checkpoint 与不可变历史、后端 eligibility 与真实 replay、Browser Read/Extract、0009 迁移重算、新增 E2E-8/E2E-9）。
- 删除 `test_wildcard_pipeline.py` 的恒真断言 `... or True`；两类通配样例提升为对真实数据依赖/页面变化/后端资格的断言。
- 显式遗留继续标记 `partial`：MCP Streamable HTTP 增量流、WorkflowVersion 可视化 diff、完整拖放落点。

## 9. 006 门禁与不变量矩阵

| 门禁 | 证据 |
|---|---|
| Mixed dataflow | test_wildcard_pipeline.py / test_distillation.py：prior-output 路径生成 `${节点.output.path}` 真实传值，Schema 不匹配告警/拒绝 |
| Dynamic web E2E | test_demo_e2e.py::test_e2e_8：页面修改后真实重新读取并交给 TraeX，总结依赖新正文 |
| Non-web E2E | test_demo_e2e.py::test_e2e_5：同一框架对变化的输入复用、零模型调用 |
| Fallback isolation | orchestrator `_fallback_only_targets` 正常路径不调度 target；test_recovery 唯一 Recovery NodeRun |
| Recovery context | test_recovery.py::test_recovery_output_depends_on_random_checkpoint_value：输出依赖随机 checkpoint 值，原失败 attempt 不可变 |
| Recovery concurrency | test_recovery.py::test_concurrent_recovery_drivers_claim_once：双 Worker 仅一次调用 |
| Approval | test_recovery.py 审批/拒绝：危险错误先 checkpoint、进 waiting_approval，不直接失败 |
| Patch governance | test_workflow_patch.py / E2E-9：后端 eligibility、禁止扩权、真实 replay、只生成 draft |
| Audit/Artifact | test_agent_run_service.py::test_secret_never_appears_in_trajectory_or_disk：Secret 不出现在事件/DB 轨迹/磁盘 |
| Data migration | test_migrations.py：非空 0008→head 后 remote-only contract_digest 重算稳定 |

八项系统不变量在运行语义层面全部成立：网页只是 Adapter（Browser Read/Extract 与文件/MCP 共用同一 IR/Compiler/Checkpoint/Patch）；调度与数据依赖都显式（未解析引用使草稿不可发布）；fallback-only Agent 正常路径调用 0 次、失败时至多领取一次；原失败 NodeRun 永远不可变（恢复是新 attempt/substitution）；Recovery Agent 只读 checkpoint 明确授权的值；已完成非幂等副作用不重放、未知 outcome 转人工确认；Patch 不改能力锁/权限/Secret 且真实 replay 成功才生成 draft；`available` 声明由真实发布/运行黑盒测试支撑。

## 10. 遗留（显式非阻断，继续标记 partial）

- MCP Streamable HTTP 流式增量消费与 Last-Event-ID 断点续传（当前仅实现会话过期重连）。
- WorkflowVersion 之间的可视化 diff UI（后端已有 replay 证据与依赖 lock；draft 打开后仍靠人工比对）。
- 完整从调色板拖拽落点（当前为点击新增 + 鼠标连线/删除）。
- 原始轨迹的显式开关/分类/保留期限与受控下载（当前默认全脱敏，不保留 Secret 原值）。
