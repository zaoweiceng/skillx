# 代码审查报告（007）

审查日期：2026-08-04  
审查范围：当前工作区中 `REPLAY-006` 对应的后端、前端、迁移、E2E、README 与新增文件。  
范围约束：Local Demo，不审查鉴权/RBAC；版本锁、执行语义、恢复活性、副作用隔离和审计仍属于正确性。

## 1. 结论

完整门禁通过，006 也真实修复了失败历史覆盖、Browser 读取执行器、Host 取消和 MCP 数据迁移。但审查发现 **6 个 P1、6 个 P2**。最高风险集中在三处：真实 Agent 没有生产可固化证据；Recovery 租约只有领取没有续租/恢复；所谓 isolated replay 可以执行外部副作用却没有先证明 baseline 可复现。

## 2. P1 问题

### P1-1 — 生产 Agent 不产生 Proposal/mixed provenance，Proposal 的节点分区也未被 Compiler 实现

证据：[`agent_host.py`](../backend/app/application/agent_host.py) 只在 Runtime 事件已经带 `argumentBindings` 时透传；[`traex_stub.py`](../examples/traex_stub.py) 和生产 `build_trajectory()` 都不生成 `DistillationProposal`。全仓的 mixed `argumentBindings` 只在 [`test_wildcard_pipeline.py`](../backend/tests/test_wildcard_pipeline.py) 手工构造。

即使外部注入 Proposal，[`distillation.py`](../backend/app/application/distillation.py) 的 `_validate_proposal()` 也只生成 actionId→strategy，忽略 ProposedNode 的 `nodeId/actionIds` 分组、`variableCandidates` 和节点边界；`llm` strategy 没有专门处理，只有 `agent` 被特殊分支。强制 Agent 时还直接使用私有 `ModelCompiler("agent")`，其能力列表取 observations，可能丢失原 action capability。

影响：REPLAY 所称“TraeX 提议分区、Compiler 裁决并生成真实 mixed DAG”不是生产闭环。首次真实探索仍会把未声明的前序值冻结成 constant，或根本没有 Proposal。

建议：把 Action provenance 和 Proposal 定义为 AgentHost 成功轨迹的必需产物，或在单独的受限 Distillation Agent 阶段生成并持久化；Compiler 必须验证分组覆盖、边界输入输出和策略语义，禁止访问 registry 私有字段。增加真实 AgentRun→Distillation→publish→reuse E2E。

### P1-2 — Action Compiler 生成的 Browser 图不能走真实运行路径，动态网页 E2E 又绕过了该图和 TraeX

`BrowserActionCompiler` 默认生成 `runtime="contract"`，实际 Executor 在该模式要求运行输入含 `observedContent`；固化图的 input schema 只有 URL，因此发布后会失败。Compiler 还把 `extract` 放在 `config.extract`，而 `BrowserReadContract` 从 `config.pageContract.bodySelector/fields` 读取，首次轨迹中的 selector 会被忽略。

E2E-8 使用 [`seed.py`](../backend/app/seed.py) 手写 `runtime=playwright` 图，并把 summary 节点设为 `agentProfile=fake + echoInputField`。即使 Harness 配置了 TraeX，该分支也不会调用 `AgentHost`。测试证明的是“Playwright 读取→Fake 回显”，不是“首次探索固化→第二次交给 TraeX 总结”。

建议：Browser compiler 直接生成与 Executor 相同的 `BrowserReadContract`，明确选择受控 runtime；编译期用同一个模型校验。E2E 必须从 Conversation Agent 的 Browser 事件生成草稿，发布后调用非 fake TraeX，并断言 TraeX 输入含变化后的正文。

### P1-3 — Recovery checkpoint 的租约无法续租或回收，Worker 崩溃会永久卡住 Run

[`recovery.py`](../backend/app/application/recovery.py) 用 CAS 将 checkpoint 置为 `running` 并写 `lease_expires_at`，但之后没有 heartbeat task，也没有查询过期 running checkpoint 的 recovery。创建的 Recovery NodeRun 同样没有 `lease_expires_at`，标准 `WorkerService.recover_expired()` 无法接管。运行中取消只把数据库状态改为 cancelled，没有向 `run_recovery_agent()` 传 cancellation callback，Agent/Tool 仍可继续到超时。

影响：进程在领取后退出，Run 永久停在 `recovering`；网络调用或 Tool 副作用可能在用户取消后继续。双 Worker 同时竞争测试只证明单次 CAS，不证明 crash recovery 或 exactly-once outcome。

建议：RecoveryDriver 增加 heartbeat/cancellation watcher；过期租约按 capability effect、idempotency key 和 outcome certainty 转为重新领取或人工确认。Recovery NodeRun 与 checkpoint 使用同一 lease owner/attempt，并补“领取后杀进程→新 Worker 恢复”黑盒测试。

### P1-4 — RecoveryDecision 的两个分支是死路，checkpoint 声称的能力锁实际为空

`request_user` 只把 run 设为 `waiting_approval`、checkpoint 设为 `awaiting_user`，没有创建 Approval、问题 payload 或用户答复/恢复 API，运行无法继续。`retry_replanned` 只是写入 `{}` 的 skip substitution，不重试失败节点、不保存新 plan，也不验证下游能否执行。

`build_checkpoint()` 的 capability locks 来自 `getattr(run, "_capability_locks")`，全仓没有写入该动态属性，所以持久 snapshot 永远是空数组。`RECOVERY_DECISION_SCHEMA` 还允许 additionalProperties，parser 只是丢弃未知字段，与“严格判别联合”不一致。

建议：将 `request_user` 接到标准 Approval/Conversation continuation；将 `retry_replanned` 改为带版本锁和边界映射的 typed plan patch，经 Compiler 校验后形成新 RecoveryAttempt。能力锁从 WorkflowVersion.validation_json 正式加载并进入 checkpoint digest。

### P1-5 — “isolated replay”不隔离副作用，也不能证明补丁修复了原失败

[`replay.py`](../backend/app/application/replay.py) 没有先运行 baseline 并复现 observation，只执行 patched graph；它仅以 `fail_node_id` 状态为 succeeded 判定 passed，不要求最终 Output 或必要下游成功。循环忽略条件边、onError 和失败传播，还会执行拓扑序中的无关/fallback 节点。

只有 `side_effect && !idempotent` 被模拟；标记为 idempotent 的写 MCP/API/Browser 节点会调用真实 Executor，网络读取也可访问外部依赖。`replayRunId` 只是字符串，没有 Run/NodeRun/事件证据。现有正例测试手工插入一个实际上从未失败的 observation，再把 transform timeout 从 30 改到 90；原定义在同一输入上也会成功。

影响：无效补丁可被宣布修复；所谓回放可能真实修改外部系统。

建议：先以捕获 fixture 重放 base 并确认同 fingerprint，再在完全禁止外部写的 ReplayAdapter 集合上运行 patch；按真实边选择/失败传播执行，要求 fail node 和声明的验收输出均满足。两次 replay 都持久化证据和 diff，无法模拟的能力直接 `REPLAY_UNSUPPORTED`。

### P1-6 — Patch 资格没有绑定目标版本，失败 occurrence 也不等于可复现证据

Patch API 接受 `failureFingerprint` 时不校验 `observation.workflow_version_id == version_id`；按 `sourceRunId` 查询时也不校验 source run、observation 与目标版本一致。没有 source run 时，调用方可提交任意 `safeInputs` 让 replay 选择性通过。

生产只在“未进入 fallback 的 fatal”记录 FailureObservation；恢复过的失败从未记录 recovered/checkpoint。Orchestrator 没有传 capability lock 或 contract summary，同节点同 error code 的不同根因会聚合。达到两次 occurrence 就直接改成 reproducible，并没有隔离复现。`record_failure_observation()` 还是先 SELECT 后 INSERT，并发首次失败可能触发 unique 冲突并使外层事务进入失败状态；异常被 Orchestrator `suppress` 后仍可能阻断 run 终态提交。

建议：Observation 使用原子 upsert，绑定 source Run/NodeRun/checkpoint/version/locks；eligibility 只能基于一次成功的 baseline reproduction artifact，不能仅看计数。API 强制目标 version、source run 和 observation 一致，禁止客户端替代 safe input。

## 3. P2 问题

### P2-1 — fallback target 可同时属于 normal graph，Orchestrator 会静默跳过合法节点

Compiler 只校验 target 存在且为 Agent，没有拒绝其普通入边/出边或被多个语义复用。Orchestrator 将所有 fallback target 无条件排除正常调度。如果作者把一个正常 Agent 同时设为 fallback，该节点及其正常下游语义会被静默改变。

建议：发布时要求 fallback target 为专用节点，禁止 normal incoming/outgoing（或引入显式 activation edge），并验证每个 target 的用途唯一。

### P2-2 — 分支和结构化确定节点的 Workflow output mapping 仍可能错误

Distillation 将所有叶子连到 Output，但 `_build_output_mapping()` 只使用 action 列表中的最后节点；其它分支结果被丢弃。对于非 Agent/LLM 的结构化输出，它仍把同一个 `${node.output}`/scalar 映射给每个 workflow output 字段。Compiler 对不存在的 outputSchema 字段只发 warning，草稿仍 publishable。

建议：Proposal/轨迹显式提供 workflow output provenance；Compiler 按字段路径生成映射并验证类型兼容。分支未消费输出要么显式忽略，要么成为 error。

### P2-3 — Browser Read 没有在重定向后重新校验最终 URL

Playwright 会在导航前检查初始 URL，但 `read_and_extract()` 直接读取 `page.url` 和正文，没有再次验证 redirect 后的 scheme/host/path。允许域可重定向到契约外地址并被读取。

建议：每次 navigation/redirect 后对最终 URL 重新执行 `url_matches`；必要时拦截 request，限制所有跳转和子资源边界。

### P2-4 — Agent 轨迹仍绕过标准 Artifact，且“Secret 不出现”只按字段名成立

Conversation trajectory URI 仍是手工拼接的 `artifact://conversation-agents/...`，没有 Artifact 表、标准下载或 GC 生命周期。`build_trajectory()` 原样保存 prompt，ConversationRun.output_json 也保留输出；redactor 只能识别敏感键，普通字段中的 token-like 文本不会被遮蔽。

建议：轨迹登记标准 Artifact；数据库只保存脱敏 IR 和引用。Secret 从 handle/schema provenance 识别，不依赖键名；增加 prompt、普通 `value` 字段和 output_json 的泄漏测试。

### P2-5 — checkpoint 把所有完成输出内联到数据库和 prompt，没有大小/Token 边界

大网页正文、文件结果或多节点输出全部进入 snapshot JSON，并再次拼进 recovery prompt；没有 Artifact threshold、按需加载或 context budget。复杂任务可能在进入 AgentHost 前就制造巨大数据库行和 prompt。

建议：小型结构化值内联，大值转标准 Artifact；Recovery Agent 先看到摘要和 handle，再按需读取授权片段，复用新增场景的渐进披露机制。

### P2-6 — 文档和测试名称继续高估覆盖范围

README 声称 E2E-8“交给 TraeX”，实际是 Fake Agent；E2E-9 只验证无 observation 时拒绝，没有走生产 failure→两次复现→replay→draft 正路径。README 仍写 `make demo-check` 执行六条 E2E，[`demo-acceptance.md`](../docs/demo-acceptance.md) 也只列 E2E-1~6，而实际已有十个场景测试和两个 Harness 测试。

建议：能力状态按可证明范围降级；验收规范成为唯一场景清单，每个声明链接到真实端到端测试，fixture/手写 observation 明确标为单元证据。

## 4. 新增场景的代码缺口

当前没有以下任何主路径类型或服务：Workflow capability manifest、TraeX catalog tools、WorkflowFragment/端口、WorkflowRef 节点、PlanCompiler、EphemeralPlanSnapshot、组合运行或复用签名。现有 `match_component()` 先于 LLM 执行，并一次读取全部 Component；`get_component()` 又直接返回完整 definition。这与“TraeX 渐进式选择和组合工作流”不是同一能力。

实现顺序与验收条件见 [`NEXT_STEPS-007.md`](../STEP/NEXT_STEPS-007.md)。
