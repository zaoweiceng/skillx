# 代码审查报告（006）

审查日期：2026-08-04  
审查范围：`REPLAY-005` 对应提交 `be64409..2b4dc5f` 的后端、前端、迁移、测试和文档。  
范围约束：单用户 Local Demo，不审查鉴权/RBAC；能力边界、数据流、审计、幂等、恢复和升级正确性仍在范围内。

## 1. 结论

完整门禁通过，005 也确实建立了 Agent Host、Action IR、Checkpoint 和 Patch 的统一骨架。但代码审查发现 **7 个 P1、5 个 P2**。最关键的问题不是缺少 UI，而是 REPLAY 所声称的三个核心闭环均没有真实成立：固化图不能传递前序动作输出，fallback target 会在正常路径提前执行，Recovery Agent 没有拿到可续跑的上下文。

## 2. P1 问题

### P1-1 — 固化后的动作只有顺序边，没有可执行的数据依赖；网页读取图无法运行

证据：[`distillation_ir.py`](../backend/app/application/distillation_ir.py) 的 `_classify_bindings()` 只把“参数值完全等于原 prompt”识别为 `user_input`，其它全部冻结为 constant；注释声称 prior output 由事件显式声明，但函数和 `build_action_ir()` 都没有消费这种声明。后者只是把每个动作的 `dependsOn` 指向前一动作。

[`action_compilers.py`](../backend/app/application/action_compilers.py) 虽支持 `prior_output` mapping，但 Agent/LLM 编译器完全忽略 bindings，固定生成 `{"message":"${input.message}"}`。因此 Browser/MCP 的结果不会传给后续 TraeX 总结；边只保证先后执行，不传数据。

Browser 路径还有第二个断点：`BrowserActionCompiler` 生成 `runtime=contract`、`action=read`，现有 [`executors.py`](../backend/app/runtime/executors.py) 的 Browser Executor 只实现 Playwright 表单和外部 `observedPage` 校验，没有访问、读取或正文抽取输出；测试中的 PageContract 也缺 [`browser.py`](../backend/app/runtime/browser.py) 要求的 `fields`。

影响：网页总结的第二次复用既不会真正访问 URL，也不会把正文交给 Agent。对于非网页 mixed graph，同样会把前序输出丢失或误用首次常量。

建议：AgentHost action 事件必须携带每个参数的显式 provenance（user input、prior action/path、artifact、constant）；IR 验证 action DAG 与引用完整性；所有 Compiler 都从 bindings 生成 input mapping。新增真正的 BrowserRead/Extract 契约和 Executor，并在编译期校验 PageContract 与输出 Schema。用“首次探索→生成草稿→发布→修改页面→第二次运行”黑盒测试验收。

### P1-2 — fallback target 是普通 DAG 节点，会在正常路径执行，恢复时又启动第二个 Agent

证据：fallback target 被放在 `spec.nodes` 中，但没有普通入边。Compiler 只在 reachability 校验中把 fallback 当虚拟边；[`orchestrator.py`](../backend/app/application/orchestrator.py) 的调度只读取 `spec.edges`，所有无入边且没有 NodeRun 的节点都会立即进入 ready。调度器不知道该 Agent 是 fallback-only。

真正失败后，[`recovery.py`](../backend/app/application/recovery.py) 的 `RecoveryDriver` 又直接调用 `run_recovery_agent()`，并不执行此前那个 target Agent 的 NodeRun。

影响：恢复 Agent 可能在正常运行开始时产生一次模型调用或副作用，失败后再执行一次；“Agent 只在显式 fallback 命中时介入”和副作用 exactly-once 均不成立。

建议：把 recovery policy 与 normal DAG 分离，或为节点/边引入明确的 activation mode。调度器必须排除 fallback-only target；命中策略时为该 target 创建并领取唯一 Recovery NodeRun，RecoveryDriver 不能另起一套游离执行。

### P1-3 — checkpoint 有数据但 Recovery Agent 看不到；恢复还会改写原失败历史

证据：`build_checkpoint()` 保存了 `completedOutputs`、失败输入和错误，但 `build_recovery_prompt()` 只发送 failed node id/type/error code、已完成节点 id 列表和 remaining id 列表，不发送已完成输出值、失败输入、错误详情、run input、Artifact 内容或能力锁。Recovery working directory 还是新空目录。

`remainingSubgraph` 只是“所有未成功的非 input/output 节点 id”，包含 fallback 节点，也不含节点定义、边、解析后的输入和幂等键，不能单独执行。`RecoveryDecision.failedOutput` 允许任意 object，未与失败节点 output contract 校验。

更严重的是，`apply_recovery_decision()` 把原失败 NodeRun 直接改成 `succeeded` 或 `skipped`，清除 error；原始 attempt 证据被破坏。E2E 使用 [`traex_stub.py`](../examples/traex_stub.py) 固定返回 `{"recovered":true,"value":"恢复值"}`，没有消费上下文。

建议：checkpoint 保存或引用脱敏后的必要输出、Artifact ids/hash、能力锁、remaining execution plan、解析输入和 idempotency keys；通过受控 prompt/挂载 Artifact 交给 Agent。失败 attempt 永远不可变，新增 RecoveryAttempt/Substitution 记录并将其输出显式绑定给下游；`failedOutput` 必须通过目标 outputSchema。

### P1-4 — WorkflowPatch 的“可复现才生成”没有接入生产，且补丁可改变能力边界

证据：[`workflow_patch.py`](../backend/app/application/workflow_patch.py) 的 `failure_fingerprint()`、`classify_failure()`、`should_propose_patch()` 只在测试中被调用。API [`POST /workflow-versions/{id}/workflow-patch`](../backend/app/api/routes.py) 接受任意请求体，不要求 source run、checkpoint、fingerprint、出现次数或 recovery/replay 证据；前端对任意失败节点都可直接拼一个 timeout 补丁。

“回放”实际只调用 `compiler.compile(patched)` 做静态校验。`_ALLOWED_NODE_FIELDS` 允许修改整个 `config/input/fallback/retry`，黑名单只拦截少数大小写敏感键，因此可写入 `allowedCapabilities`、`mcpToolSnapshotId`、`capabilityVersionId`、模型、outputSchema 或 pageContract，造成扩权或换锁。

影响：一次网络波动也能生成补丁；补丁能改变执行权限/能力来源却仍显示 publishable；静态编译通过不代表原失败已修复。

建议：持久化 FailureObservation/Fingerprint 聚合，API 只能从满足策略的 source run/checkpoint 创建 proposal。按 node type 定义结构化 diff allowlist，能力、Secret、锁和风险变化走单独审批且默认拒绝。使用捕获的安全输入/fixture 真正重放失败路径，通过后才创建 draft；ContractPatch 应复用同一服务而不是仅共享“人工发布”概念。

### P1-5 — 原始 Tool 参数和结果仍写入数据库轨迹及非托管文件

证据：[`agent_host.py`](../backend/app/application/agent_host.py) 的 normalized action 保留原始 `arguments/result`；[`agent_run.py`](../backend/app/application/agent_run.py) 的 `build_trajectory()` 将完整 actions 和 output 写入 `ConversationRun.trajectory_json`，`_stage_trajectory()` 又把完整 runtime events/output 写入磁盘。现有脱敏只覆盖公开 `ConversationRunEvent`。

轨迹 URI 虽长得像 `artifact://conversation-agents/...`，却没有创建标准 `Artifact` 数据库行，也没有标准下载、retention、hash/outbox 生命周期。现有 parity 测试只扫描 audit event，不扫描 `trajectory_json` 和文件字节。

影响：Secret、网页正文和文件内容仍可能长期存在两份；REPLAY 的“事件/Artifact 不存 Secret 明文”和统一 Artifact 生命周期均不成立。

建议：数据库只保存脱敏 IR、摘要和 Secret handle；需要保留的原始轨迹必须显式分类、加密/限期并登记标准 Artifact 行。统一 staging/outbox/promote/download/GC；测试同时检查事件、数据库和落盘内容。

### P1-6 — RecoveryCheckpoint 没有原子领取或租约，多 Worker 会重复执行 Agent

证据：`ExecutionCheckpoint` 没有 `lease_owner/lease_expires_at`，也没有约束同一 failed NodeRun 只能有一个 checkpoint。`RecoveryDriver.poll_once()` 读取一个 `pending` 行后就结束事务，但不先 CAS 为 running；多个 Worker 可同时读到同一行并各自调用 TraeX，最终只有应用阶段检查 status。`_handle_fatal()` 的“先查询、再插入”同样没有数据库唯一约束。

影响：重复恢复调用已经发生后才会被状态检查挡住；如果 Agent 有 Tool，副作用可能重复，且成本/审计不一致。

建议：增加唯一键 `(failed_node_run_id, attempt)`；用条件更新原子领取 `pending→running`，记录 lease owner/expiry/heartbeat；过期恢复必须按 outcome certainty 决定重试或人工确认。恢复 Tool 调用使用持久 idempotency key。

### P1-7 — 0009 对已有 MCP 数据的 digest 回填会制造契约漂移

证据：`0009_mcp_contract_digest.py` 新增 `contract_digest` 后直接执行 `contract_digest = digest`。但旧 `digest` 包含本地 effect/idempotent，新的 remote contract digest 只覆盖 name/description/input/output/timeout，两者不是同一算法。

影响：非空 0008 数据库升级后，首次对未变化的远端 Tool 执行 discovery 会比较两个不同 hash 并报 `MCPContractDriftError`。空库 0001→0010 门禁发现不了该问题。

建议：迁移用与运行时代码完全一致的 canonical 算法，从旧行字段重新计算 remote-only digest；增加先在 0008 插入至少两个 Tool、再升级到 0010并重复 discovery 的数据迁移测试。

## 3. P2 问题

### P2-1 — AgentHost 终态 Token 可覆盖累计值且不重新校验预算

`agent.step.completed` 会累计并校验 Token，但 `agent.completed.totalTokens` 随后直接覆盖 `result.total_tokens`，没有再次与 `maxTotalTokens` 比较，也没有校验终态总数与步骤累计的一致性。只在终态报告大数或报告更小数都可绕过/污染审计。

建议：终态 Token 必须非负、不得小于可信累计值，并再次执行 Host 上限；Runtime 未逐步报告时也只能使用经校验的终态数。

### P2-2 — Agent 取消只有 Runtime 产出下一条事件后才会生效

`AgentHost.execute()` 在 `async for adapter.events()` 循环体内检查 cancellation。若子进程挂住、不再产出事件，取消 watcher 只写数据库/事件标记，不能及时调用 adapter.cancel；现有测试允许等待接近 stub 的 30 秒睡眠。

建议：将 adapter event stream 与 cancellation token 用 `asyncio.wait(FIRST_COMPLETED)` 竞争；取消一到立即终止子进程并持久化一致终态。

### P2-3 — `requireUserApproval` 只有“禁止自动恢复”，没有审批后继续能力

Compiler 允许危险错误在 `requireUserApproval=true` 时配置 fallback；但 Orchestrator 只在 `not require_user_approval` 时创建 checkpoint，否则直接 `_finish_failed()`。没有 Approval 记录、批准/拒绝 API 或批准后恢复入口。

建议：先冻结 checkpoint 并进入 `waiting_approval`，审批只决定是否激活已声明 fallback；拒绝保持失败，批准后走同一原子 Recovery claim。

### P2-4 — Executor 的非约定异常可能击穿 Worker，恢复视图还返回错误 target

`execute_claimed()` 只捕获取消类异常；如果生成的 Browser PageContract 在 Executor 内抛出 Pydantic/KeyError 等非 `NodeResult` 异常，函数不会 `_commit_result()`，上层 Worker 循环可能退出并留下租约。与此同时，运行详情 API 的 `fallbackTarget` 实际返回 `snapshot.failedNode`，前端看到的恢复目标是错误证据。

建议：在 Worker 边界把所有非取消异常转换为结构化 `EXECUTOR_INTERNAL_ERROR`，记录日志并交给标准重试/失败状态机；修正 checkpoint snapshot/API target 字段并补契约测试。

### P2-5 — “网页与非网页黑盒验收”实际是合成单测，包含恒真断言

[`test_wildcard_pipeline.py`](../backend/tests/test_wildcard_pipeline.py) 只构造 trajectory 并断言节点类型，没有 API、发布、Worker、真实 Browser/MCP/文件输入或第二次运行；网页 action 的 summary 参数还是常量“正文”。`assert ... or True` 使“未泄漏 Playwright 私有对象”断言恒真。

E2E-7 只证明 `fail_dependency` 配合固定 RecoveryDecision 可把 run 变成功，并未断言 Recovery Agent 收到哪个输出、fallback target 未提前执行、失败历史不变或多 Worker exactly-once。

建议：删除恒真断言；把两个样例提升为真实端到端测试，并让输出必须依赖变化后的输入/页面和 checkpoint 前序值。新增双 Recovery Worker 竞争与 fallback target 调用次数断言。

## 4. 修复优先级

1. 先修 fallback activation、checkpoint 不可变历史和 Recovery 原子领取；否则恢复会提前执行、重复执行并篡改证据。
2. 再修 Action provenance、mixed graph 输入映射和 Browser Read/Extract；否则“固化”没有等价的数据流。
3. 然后接通 FailureObservation→Patch→真实 replay，并封死能力扩张。
4. 最后统一轨迹 Artifact/脱敏、取消与预算边界、带数据迁移和产品证据。

具体任务与出口条件见 [`NEXT_STEPS-006.md`](../STEP/NEXT_STEPS-006.md)。

> 2026-09-16：以上为历史审计记录。旧数据库迁移链已移除，当前使用[固定初始结构](../backend/migrations/versions/0001_refactored_schema.py)，旧数据按要求清空重建。
