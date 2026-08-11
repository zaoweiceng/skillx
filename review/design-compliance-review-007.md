# 设计与规范达成度审查报告（007）

审查日期：2026-08-04  
审查基线：提交 `2b4dc5f` 加当前工作区中的 [`REPLAY-006.md`](../REPLAY/REPLAY-006.md) 对应修改、006 三份文档及 `docs/` 设计/规范。当前修复尚未提交，因此本报告以工作区内容而不是新 commit 为准。  
范围约束：单用户 Local Demo；鉴权、RBAC、多租户、生产级沙箱和分布式部署不作为阻断项。

## 1. 结论

006 的多数基础改造是真实的：正常调度已排除 fallback target，恢复不再覆盖失败 NodeRun，Browser Read/Extract 有真实 Playwright 路径，AgentHost 终态 Token 与无事件取消得到修复，MCP 非空数据迁移也已重算 remote-only digest。完整门禁全部通过。

但 [`REPLAY-006.md`](../REPLAY/REPLAY-006.md) 将这些改造概括为“可端到端复用与恢复的通配工作流”，仍然过度。当前至少有四个系统级断点：

1. 生产 Agent 轨迹并不产生 `DistillationProposal` 或 mixed-action 的显式 provenance；这些证据只存在于合成测试，首次探索不能自动形成测试中的可执行图。
2. Action Compiler 生成的 Browser 节点默认走需要 `observedContent` 的测试 runtime，且 selector 配置位置与 Executor 不一致；动态网页 E2E 使用手写 Workflow 和 Fake Agent 回显，不是“首次 TraeX 探索后固化并再次交给 TraeX”。
3. Recovery checkpoint 虽有租约列，但没有心跳、过期领取恢复或运行中取消；Worker 领取后崩溃会永久卡在 `running`。`request_user` 和 `retry_replanned` 也没有可继续的产品语义。
4. Patch replay 没有先复现基线故障，只看 patched fail node 是否成功；还会真实执行幂等副作用节点、忽略条件边和下游失败，因此既不是隔离回放，也不能证明补丁修复了原问题。

准确版本定位：**基础 Runtime 与若干关键构件已可用；通配固化、恢复接力和 Patch 学习仍为 partial，README 不应标为完整 `available`。**

对新增场景的直接回答是：**当前版本不能实现。**现有对话路径由服务端一次查询全部 Component 后做字符串打分，TraeX 看不到工作流目录、输入输出契约或工作内容；系统也没有渐进披露工具、可调用子图接口、Workflow/Fragment 引用节点或临时执行计划。

## 2. REPLAY-006 声明回归

| REPLAY-006 声明 | 007 判定 | 说明 |
|---|---|---|
| fallback-only target 正常路径不执行 | 部分完成 | Orchestrator 会跳过 target；Compiler 未禁止 target 同时出现在 normal edge，合法业务节点可能被静默移除 |
| 双 Worker 只调用一次 Recovery Agent | 同时竞争时完成，崩溃恢复未完成 | pending→running CAS 有效；无 heartbeat/reclaim，领取 Worker 崩溃后永不恢复 |
| 唯一 Recovery NodeRun 执行 target | 记录层部分 | 创建了 target id 的 NodeRun，但实际调用仍是平台固定 recovery prompt，未执行 target 的 prompt/input，NodeRun 也不保存决定输出 |
| 审批型 fallback 可批准/拒绝 | 基本完成 | 预先审批可用；Recovery Agent 返回 `request_user` 后没有 Approval/继续入口 |
| 可执行 checkpoint 含锁和剩余计划 | 部分 | 有输出值、节点和边；`capabilityLocks` 实际总为空，Artifact/大值没有标准引用，remaining plan 未被独立执行 |
| `retry_replanned` 从剩余计划重试 | 未实现 | 实际写入空 substitution，既不重试失败节点也不提交新 plan |
| 原失败 attempt 不改写 | 已完成 | OutputSubstitution 保留失败 NodeRun；该项证据可信 |
| TraeX 产生 provenance 与 Proposal | 未按声明完成 | Host 只透传 Runtime 可选字段；实际 TraeX fixture/build_trajectory 不生成 proposal，mixed provenance 仅测试手工构造 |
| Proposal 决定节点边界 | 未实现 | Compiler 忽略 ProposedNode 的 nodeId/action grouping、variableCandidates 和 `llm` strategy，只读取逐 action strategy |
| Browser mixed graph 可由首次轨迹固化 | 未完成 | 合成图未发布运行；编译结果 runtime/selector 与 Browser Executor 不匹配 |
| 动态网页重新读取并交给 TraeX | 重新读取完成，TraeX 未验证 | E2E 使用手写 seed 和 `agentProfile=fake/echoInputField`，只证明 Browser→回显数据流 |
| 后端事实决定 Patch 资格 | 部分 | 有 FailureObservation；恢复过的失败不记录，两个 occurrence 被直接当成复现，来源未绑定目标版本 |
| 真实隔离 replay 后才建 draft | 未按声明完成 | 未复现 baseline、未比较差异、会执行幂等副作用、只检查原 fail node |
| Secret 不出现在事件/DB/磁盘 | 部分 | 敏感键会脱敏；prompt、普通键中的 secret-like 值和 ConversationRun output 仍可保留，轨迹也未登记标准 Artifact 行 |
| 0008→head MCP 数据升级 | 已完成当前证据 | 迁移算法与运行时 canonical 结构一致，带数据测试通过 |

## 3. 新增“Workflow as Skill”场景判定

需求本质是让 TraeX 使用一个**可检索、分层披露、版本锁定、可组合的 Workflow 能力目录**。它不是把完整 DAG 复制到上下文，也不是把静态组件推荐改名为 Agent 选择。

| 所需能力 | 当前实现 | 判定 |
|---|---|---|
| TraeX 能发现已发布 Workflow | `match_component()` 由服务端查询所有 Component 并打分，AgentHost 没有 workflow catalog tool | 不满足 |
| 先看到名称/简介，再按需读取契约 | `/components` 给 UI 返回摘要，`/components/{id}` 一次返回完整 definition；没有 Agent 分层协议 | 不满足 |
| 知道输入、输出和主要工作 | Component 只有 name/description；Schema 藏在完整 WorkflowVersion，缺 purpose/useWhen/examples/risk/步骤摘要 | 不满足 |
| 根据上下文选择合适工作流 | 选择发生在 TraeX 之前，采用空格分词+子串打分；中文描述通常不能形成有效词元 | 不满足 |
| 执行整个已发布工作流 | 用户/服务端可按 Component 启动完整版本 | 基础能力已有，但 TraeX 无调用入口 |
| 提取其中一部分执行 | 无 WorkflowFragment、entry/exit port、依赖闭包或片段 digest | 不满足 |
| 组合多个 Workflow/片段 | NodeType 无 workflow/subflow ref；Compiler 无引用展开与跨契约映射 | 不满足 |
| 临时组合而不固化 | `Run.workflow_version_id` 必填；没有 EphemeralPlan/PlanSnapshot/TTL | 不满足 |
| 高频临时计划建议固化 | 无 plan signature、复用统计、推荐 draft 或人工确认 | 不满足 |

现有设计已经给出方向：[`skillx.md`](../docs/skillx.md) 明确要求“先发现名称和描述，再按需读取完整说明”，并把“工作流子图接口”列为开放问题；[`schema.md`](../docs/schema.md) 要求由 LLM 匹配组件、补参后交给 Runtime。代码目前只实现了最终启动卡片，没有实现中间的 Agent 能力发现层。

## 4. 推荐目标架构

```text
TraeX
  → workflow.search(query, constraints)        # L0：少量能力卡片
  → workflow.describe(versionRef)              # L1：I/O、风险、主要步骤、入口
  → workflow.describe_fragment(fragmentRef)    # L2：片段端口、锁、依赖闭包
  → 提交 ExecutionPlan（只含版本化引用与映射）
  → PlanCompiler 校验 Schema/锁/副作用/审批并展开为冻结 DAG
  → Workflow Runtime 执行
  → 临时 PlanSnapshot（可审计、有 TTL、不公开）
  → 高频且稳定时建议生成 Workflow draft，仍需人工发布
```

关键边界：

- TraeX 负责理解意图和选择候选；Registry 负责检索；Compiler 负责最终可执行性与安全裁决。
- “临时”只表示不发布为公共 Workflow，仍必须冻结 plan digest、版本锁和运行证据。
- 任意裁剪节点不安全。片段必须是发布时声明的 entrypoint，或由确定性 Slicer 计算依赖闭包、边界输入输出和副作用后生成。
- 组合计划只引用 `workflowVersion/fragment digest`，不让模型复制和改写完整 definition。

## 5. 设计规范达成度

| 设计承诺 | 007 状态 | 主要缺口 |
|---|---|---|
| 对话由 LLM 判断并匹配组件 | 未完成 | 当前是 LLM 之前的静态 substring matcher |
| Capability Registry 管理子流程 | 未实现 | 只有 Component/WorkflowVersion，没有 WorkflowCapability/Fragment |
| Agent Skills 渐进披露 | SkillX 文档有规范，Workflow 无实现 | 缺 L0/L1/L2 manifest 和 Agent tools |
| Workflow Compiler 锁定引用 | 单工作流完成 | 无跨 Workflow/Fragment 引用、展开和锁定 |
| 临时与持久执行可审计 | 持久 Workflow 完成 | 无临时 PlanSnapshot；不能组合后直接运行 |
| 未经人工不得自动发布 | 已完成现有范围 | 新场景必须继续保持：频繁使用只建议 draft，不自动发布 |

## 6. 门禁结果与证据边界

| 阶段 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| Ruff | 通过 |
| 后端 Pytest | 188 passed |
| Alembic | 空库 0001→0012、带 MCP 数据 0008→head 通过 |
| TypeScript / ESLint | 通过 |
| Vitest | 28 passed |
| Vite production build | 通过 |
| E2E | 12 passed |

测试通过证明已编码路径无明显回归，但不证明上述过度声明：E2E-8 是手写图+Fake Agent；E2E-9 只有“无观测时拒绝”的负例；[`demo-acceptance.md`](../docs/demo-acceptance.md) 仍只定义六条 E2E，与 README/REPLAY 的新增场景不一致。

## 7. 007 完成定义

007 至少需要同时证明：

1. 真实 Conversation Agent 产生 provenance 和 Proposal，固化出的 Browser→TraeX 图可发布、第二次真实运行并消费新正文。
2. Recovery Worker 崩溃可按 outcome certainty 恢复；`request_user`、重规划、取消和 capability locks 都有完整状态机。
3. Patch replay 先复现 baseline，再执行 patched plan；任何外部副作用都模拟/禁止，最终输出和失败点均通过才算修复。
4. TraeX 面对大量 Workflow 时只收到 top-k L0 卡片，按需读取 L1/L2，并能选择一个完整 Workflow 执行。
5. TraeX 能选择一个合法 Fragment，与另一个 Workflow/Agent 步骤组合为临时 Plan；Plan 被持久冻结但不发布。
6. 同一 Plan 多次成功后只出现“建议固化”草稿入口，未经人工确认不创建已发布版本。

具体实施顺序见 [`NEXT_STEPS-007.md`](../STEP/NEXT_STEPS-007.md)。
