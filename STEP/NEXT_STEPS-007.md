# NEXT_STEPS-007：修正 006 语义，并实现可渐进披露、可裁剪组合的 Workflow 能力

依据：[`REPLAY-006.md`](../REPLAY/REPLAY-006.md)、[`review/design-compliance-review-007.md`](../review/design-compliance-review-007.md)、[`review/code-review-007.md`](../review/code-review-007.md)。
范围：Local Demo，不做鉴权/RBAC、多租户和分布式部署。版本锁、副作用边界、临时计划审计和恢复活性属于功能正确性，不能省略。

## 1. 目标与不变量

目标链路：

```text
已发布 WorkflowVersion
  → 生成轻量 Workflow Capability Manifest
  → TraeX 搜索 top-k 卡片（L0）
  → 按需读取契约/主要步骤（L1）或合法片段（L2）
  → 选择整个 Workflow、一个 Fragment，或组合多个引用
  → PlanCompiler 校验并展开为冻结执行 DAG
  → 临时 PlanSnapshot 或 Workflow draft
  → Workflow Runtime 执行、恢复和审计
  → 高频稳定计划只建议固化，人工发布
```

必须保持：

1. TraeX 永远不一次接收全部 Workflow definition；搜索只返回有界 top-k 摘要。
2. Agent 负责语义选择，Registry 负责检索，Compiler 负责 Schema/锁/风险的最终裁决。
3. 执行引用必须锁定 WorkflowVersion/Fragment digest；默认版本变化不影响已启动计划。
4. 任意节点裁剪不合法；片段必须有确定的依赖闭包、边界端口、输出契约和副作用摘要。
5. 临时只代表“不发布”，仍需不可变 PlanSnapshot、digest、TTL、Run/NodeRun/Event 和恢复证据。
6. 组合不能扩大各引用能力权限；审批和副作用在展开后仍可见且不可绕过。
7. 高频使用只能生成“建议固化”或 draft，禁止 Agent 自动发布。
8. 在新增能力前，006 的 Recovery、Distillation 和 replay 缺口必须先修，否则组合只会放大错误语义。

## 2. A：先修复 006 的阻断问题

### A-01 真实 Distillation 闭环

- [ ] AgentHost 成功轨迹必须包含逐参数 provenance；缺少 provenance 的动态值不得回退为 constant 并标为 publishable。
- [ ] 增加受限 Distillation Agent 阶段生成 `DistillationProposal`，保存 source trajectory/proposal digest；真实 Runtime 不提供时不能伪称 Agent 已提议。
- [ ] Compiler 实现 ProposedNode grouping、nodeId、`deterministic|agent|llm` 三种策略、variable candidates 和边界 I/O；每个 action 恰好归属一个节点。
- [ ] Agent fallback 节点保留原 action 的最小 capability，禁止通过私有 registry 成员编译。
- [ ] Workflow output 每个字段必须有显式 provenance，分支和结构化输出做 Schema 类型校验；未知字段从 warning 提升为发布错误。

### A-02 Browser 固化与真实 TraeX E2E

- [ ] BrowserActionCompiler 与 Executor 共用同一个 `BrowserReadContract` 模型；selector 全部放在 pageContract，发布时完成校验。
- [ ] 生产固化选择真实受控 runtime；`observedContent` 仅作为明确测试 adapter，不能出现在 publishable 生产图。
- [ ] 导航和每次 redirect 后重新校验最终 URL。
- [ ] E2E 从 Conversation Agent 首次 Browser 事件开始：Distillation→draft→publish→改页面→第二次运行；summary 节点必须走非 fake AgentHost，断言 TraeX prompt/输入含新正文。

### A-03 Recovery 完整状态机

- [ ] Recovery checkpoint/NodeRun 共用 lease owner、expiry 和 heartbeat；Driver 监听取消并立即取消 Agent subprocess。
- [ ] 实现过期 running checkpoint 恢复：无副作用或有持久幂等键可重领；outcome unknown 转人工确认。
- [ ] `request_user` 创建标准 Approval/Conversation question，保存回答并可恢复同一 checkpoint。
- [ ] `retry_replanned` 改为 typed `RecoveryPlanPatch`，包含 remaining nodes/edges/input mappings/locks；Compiler 校验后形成新 RecoveryAttempt，禁止空 substitution 冒充重规划。
- [ ] 从 WorkflowVersion.validation_json 加载 capability/MCP/model/page locks，进入 checkpoint digest。
- [ ] fallback target 必须专用，Compiler 禁止 normal edge 复用。

### A-04 可证明的 Patch replay

- [ ] Observation 使用数据库原子 upsert，并绑定 workflow version、source Run/NodeRun/checkpoint、capability lock、contract summary 和 recovery outcome。
- [ ] eligibility 要求成功的 baseline reproduction，不再用 occurrence≥2 代替复现。
- [ ] API 强制 observation/source run/目标 version 一致，禁止客户端提供替代 safe input。
- [ ] ReplayEngine 使用全套 ReplayAdapter：所有外部写一律模拟或拒绝；读取使用捕获 fixture，禁止真实网络/MCP 写。
- [ ] 先运行 base 并得到同 fingerprint，再运行 patch；按条件边/onError/失败传播执行，并要求原失败点、必要下游和最终输出断言通过。
- [ ] baseline/patched replay 都保存 PlanSnapshot、NodeResult、diff 和 Artifact；失败不创建 draft。

完成条件：崩溃恢复、request_user、真实 mixed distillation、baseline→patched replay 四组黑盒测试全部通过后，README 才可恢复 006 相关 `available`。

## 3. B：Workflow Capability Manifest

新增不可变、可检索的 `WorkflowCapabilityManifest`，由已发布 WorkflowVersion 生成并允许作者补充语义字段：

```yaml
workflowRef: workflow:<workflowId>@<version>#<digest>
name: web-article-summary
summary: 读取网页正文并生成结构化摘要
useWhen: [需要获取当前网页内容并总结]
doNotUseWhen: [需要登录后写入或提交表单]
tags: [browser, read, summarize]
inputSynopsis: {required: [url], properties: {url: string}}
outputSynopsis: {properties: {reply: string}}
executionClass: agent
sideEffects: none
approvalRequired: false
estimatedCost: {llmCalls: 1}
entrypoints: [full, read-only]
```

- [ ] 数据模型保存 manifest、workflowVersionId、workflow digest、manifest digest、状态和搜索文本；只索引 published version。
- [ ] 自动派生 I/O synopsis、execution class、能力锁、副作用/审批、业务节点摘要和最近成功率；`summary/useWhen/doNotUseWhen/examples/tags` 由作者审核。
- [ ] 默认版本更新创建新 manifest，不修改旧 manifest；运行始终使用返回给 Agent 的精确 versionRef。
- [ ] Manifest 不能包含 Secret、完整 prompt、节点配置或 Artifact 内容。

完成条件：发布/升级 Workflow 会生成不可变 manifest；旧对话持有的 versionRef 仍可运行原版本。

## 4. C：面向 TraeX 的渐进披露协议

通过 Host Tool Broker 或本地受控 MCP 暴露四个原子工具；不能让 TraeX 直接访问数据库/API：

### C-01 L0 搜索

`workflow.search(query, constraints, limit≤8)` 返回：ref、name、summary、tags、I/O 摘要、executionClass、sideEffect、approval、cost/reliability 摘要和匹配理由。

- [ ] 支持语义/关键词混合检索和结构化约束（required inputs、输出类型、零 LLM、副作用、可用能力）。
- [ ] 服务端限定 top-k、总字节和每会话调用次数；1000 个 Workflow 时 prompt 大小仍有界。
- [ ] 替换当前 LLM 前置的全表 substring matcher；无模型时可保留为 UI 降级推荐，但不能冒充 TraeX 决策。

### C-02 L1 描述

`workflow.describe(workflowRef)` 返回完整 input/output Schema、业务步骤摘要、例子、限制、风险/审批、版本锁、成本和可用 fragment 列表，不返回完整技术 DAG。

### C-03 L2 片段描述

`workflow.describe_fragment(fragmentRef)` 返回片段边界 Schema、主要工作、锁、副作用、审批、可组合限制和 fragment digest。只有 Agent 确实选择该候选时才加载。

### C-04 执行选择

TraeX 输出受 Schema 约束的 `WorkflowSelection`：`run_full | run_fragment | compose | continue_agent | ask_user`，包含选择理由、版本化 refs、参数绑定和缺失输入。Host 只接受 Registry 返回过的 refs，Compiler 仍作最终裁决。

完成条件：对话中 TraeX 能在至少 100 个组件中找到 top-k，按需 describe 一个候选、补齐参数并启动锁定版本；未选候选的完整 Schema/DAG 不进入上下文。

## 5. D：安全的 Workflow Fragment

### D-01 显式发布片段

新增 `WorkflowFragmentManifest`：

- fragmentRef/version/digest；
- source workflowVersion/digest；
- entry nodes、exit outputs、依赖闭包；
- boundary input/output Schema 与 mapping；
- capability locks、side effects、approvals、fallback 和补偿要求；
- business summary/useWhen/doNotUseWhen。

- [ ] 作者可在 Workflow 中声明稳定业务组为 callable entrypoint；发布 Compiler 计算闭包并验证无隐含输入。
- [ ] Fragment 不复制可变节点定义；引用源版本和确定 closure digest。

### D-02 临时裁剪

- [ ] Agent 只能提出语义目标或候选 node group；确定性 Slicer 计算所有祖先依赖、必要 control/approval/fallback 和输出闭包。
- [ ] 缺 boundary mapping、跨越未提交副作用、需要隐藏 Secret 或无法独立恢复时拒绝裁剪。
- [ ] 临时裁剪结果生成私有 FragmentSnapshot，并使用与发布片段相同的 Compiler/Runtime，不创建公共能力。

完成条件：可独立执行“网页工作流中的 read-only 片段”；试图只取 submit 节点而绕过检查/审批必须被 Compiler 拒绝。

## 6. E：引用式组合与临时执行计划

### E-01 ExecutionPlan IR

定义：

```text
PlanStep = WorkflowRef | FragmentRef | SkillXRef | MCPRef | Transform | LLM | Agent
PlanEdge = source output path → target input path
ExecutionPlan = plan input/output Schema + steps + mappings + policies
```

- [ ] TraeX 只生成 refs 和 mappings，不复制 raw Workflow definition。
- [ ] PlanCompiler 验证版本/digest、Schema 可赋值、DAG、能力交集、副作用、审批、预算和输出完整性。
- [ ] Compiler 将 Workflow/Fragment ref 展开为 namespaced frozen DAG，复用当前 Orchestrator/Checkpoint/Patch；第一阶段不引入递归子 Run 状态机。
- [ ] 展开时保留来源 ref/node 映射，运行详情可折叠为业务步骤并下钻技术节点。

### E-02 EphemeralPlanSnapshot

- [ ] 新增不可变 `PlanSnapshot`：definition/digest、source refs、locks、createdByConversationRun、TTL/status。
- [ ] Run 可绑定 `workflow_version_id` 或 `plan_snapshot_id` 的判别联合；不能把未冻结 dict 直接交给 Worker。
- [ ] 临时计划通过同一持久队列运行，有 NodeRun/Event/Artifact/Approval/Recovery；TTL 只控制目录可见性，不删除审计中的引用快照。
- [ ] 计划执行前向用户展示选择理由、输入、输出、步骤摘要、副作用和是否会调用 Agent。

完成条件：TraeX 组合“Workflow A 的只读片段 → Workflow B → Agent 总结”并临时执行；重启后可恢复，源 Workflow 默认版本变化不影响本次 Run。

## 7. F：从高频临时计划到人工固化

- [ ] 计算隐私安全的 `planSignature`：锁定 refs/digests、step kinds、mapping shape、I/O Schema；排除具体输入值和 Secret。
- [ ] 聚合同一 signature 的成功次数、失败率、手工调整次数、成本和输入变化；只对稳定且多次成功的计划显示“建议固化”。
- [ ] 用户确认后从 PlanSnapshot 创建 Workflow draft，保留来源 refs、测试样例和成本对比；再次走 Compiler/test/review/publish。
- [ ] Agent 可以建议，不能自动创建 published Workflow，也不能自动升级引用版本。

完成条件：同一临时计划达到配置阈值后出现建议；忽略建议不影响后续运行；接受只生成 draft，旧 PlanSnapshot 和源 Workflow 均不变。

## 8. G：产品、可观测性和文档

- [ ] 对话展示 Workflow 候选卡片、匹配理由、已加载层级、锁定版本、缺失输入和执行/继续 Agent 选择。
- [ ] 组合前展示 Plan 预览；运行详情支持 source Workflow/Fragment 折叠、临时标记、TTL、版本锁和恢复点。
- [ ] 组件页可维护 manifest 和 callable fragments；发布前预览 TraeX 的 L0/L1/L2 内容。
- [ ] 更新 [`demo-acceptance.md`](../docs/demo-acceptance.md) 为唯一 E2E 清单，README 的数量和能力状态从该清单生成或同步校验。
- [ ] 在 A 全部完成前，将通用固化、恢复、Patch replay 和“交给 TraeX”降为 `partial`。

## 9. 新增验收门禁

`make demo-check` 保持现有 188+ 后端、28+ 前端和 12+ E2E，并新增：

| 门禁 | 必须证明 |
|---|---|
| Real distillation | 真实 Agent 产生 provenance/Proposal，生成图可发布和换输入运行 |
| Recovery crash | 领取后杀 Worker，新 Worker 按 outcome certainty 恢复；取消立即停止 Agent |
| Recovery branches | request_user 可回答续跑；replan 有 typed plan，不接受空替代 |
| Replay proof | baseline 同 fingerprint 失败，patched 成功；所有外部写均未发生 |
| Catalog scale | 1000 个 Workflow 只向 TraeX 返回有界 top-k L0 内容 |
| Progressive disclosure | 未选择候选不加载 L1/L2，选择后按需加载且 ref/digest 稳定 |
| Full workflow selection | TraeX 选择合适 Workflow、补参并启动锁定版本 |
| Fragment safety | 合法只读片段可运行；绕过审批/依赖的裁剪被拒 |
| Temporary composition | 两个 refs + Agent 组合为 PlanSnapshot，重启可恢复但不发布 |
| Version pinning | 源默认版本更新不改变已创建 PlanSnapshot/Run |
| Solidification suggestion | 高频成功只建议 draft，未经人工不发布 |
| Context budget | Catalog/Checkpoint 大数据使用摘要+按需 Artifact，不把全部内容塞入 prompt |

只有 A–G 的出口全部通过，007 才能声称“Workflow 对 TraeX 可见、可渐进披露、可局部提取和临时组合”为 `available`。
