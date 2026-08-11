# NEXT_STEPS-008：闭合 TraeX Workflow 工具循环与执行状态机

依据：[`REPLAY-007.md`](../REPLAY/REPLAY-007.md)、[`review/design-compliance-review-008.md`](../review/design-compliance-review-008.md)、[`review/code-review-008.md`](../review/code-review-008.md)。
范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。目标不是增加更多独立 API，而是把已有 Registry、Compiler、Runtime 接成一个可证明的闭环。

## 1. 008 目标

```text
用户任务
  → TraeX + WorkflowToolBroker
      → search L0
      → 按需 describe L1 / fragment L2
      → ask_user / continue_agent / run_full / run_fragment / compose
  → Selection/ExecutionPlan 严格校验
  → PlanCompiler 冻结 refs、端口、锁和风险
  → Workflow Runtime 执行、恢复、回放和审计
  → 高频稳定计划仅建议人工固化
```

必须保持以下不变量：

1. Agent 只看到当前步骤需要的 top-k/契约，不接收全量 DAG。
2. Agent 负责语义提议，Compiler 负责所有可执行性、能力和副作用裁决。
3. 任何状态提交都必须证明自己仍拥有当前 claim；迟到 Worker 无写权限。
4. 临时计划可过期但不能失去审计；过期只禁止新执行，不破坏已有 Run 恢复。
5. Fragment/Plan/Recovery 都不能扩大源 Workflow 的能力、写权限或审批边界。
6. Replay 与正常 Runtime 共用同一边选择和输出终态语义。

## 2. A：先修状态机正确性

### A-01 Recovery fencing

- [x] 为 ExecutionCheckpoint 增加单调 `claim_generation` 和随机 `claim_token`；Recovery NodeRun 保存相同 generation。
- [x] claim、heartbeat、Agent 决定提交、NodeRun 终结全部使用 CAS：`id + status + owner + generation + token`。
- [x] Agent 返回后若租约已丢失，只记录本地丢弃日志，不得修改 checkpoint、Run、Approval 或当前 Recovery NodeRun。
- [x] `_reclaim_expired()` 生成新 generation；unknown outcome 只创建一个未决人工确认。
- [x] 增加黑盒：A 被暂停超过租约、B 重领、A 迟到返回；只允许 B 的决定生效。

### A-02 受控 RecoveryPlanPatch

- [x] 删除 `remainingNodes` 任意完整定义替换；改成受限 `nodePatches`、`inputMappings`、`edgePatches` 或锁定 replacement ref。
- [x] 禁止修改 node id/type、sideEffect、idempotent、fallback、approval、model 和 allowedCapabilities，除非 replacement ref 自身已发布并重新裁决。
- [x] 合并后执行发布同级的 WorkflowCompiler、Capability、MCP、Browser Contract 和副作用/审批检查。
- [x] locks 由校验结果生成，拒绝 Agent 自报 locks；保存 before/after digest 和诊断。
- [x] RecoveryAttempt 只有一个 active；新 attempt 激活时关闭旧 attempt，终态后写 succeeded/failed/superseded。

完成条件：replan 扩权、改副作用、替换 MCP snapshot 和省略审批均被拒；合法纯映射修复能创建新 NodeRun attempt 并完成。

## 3. B：建立真正的 TraeX 渐进工具循环

### B-01 WorkflowToolBroker

- [x] 新增持久 `WorkflowDisclosureSession`：session id、conversation/agent run、已披露 refs/levels、调用预算、expiry、状态。
- [x] 去掉进程全局 `_SESSION_CALLS/_SESSION_REFS` 和默认共享 `local`；每个对话任务生成唯一 session。
- [x] AgentHost 通过受控 Broker 获得四个工具：search、describe、describe_fragment、compile_plan；可选 slice 只能提交候选 node ids。
- [x] 每次工具调用持久化 request 摘要、response digest、披露层级和预算，正文仍受 24KB/step 限制。
- [x] Agent 不得直接调用内部 REST 或数据库；Broker 只返回本次 session 已授权内容。

### B-02 Selection 判别联合

为五种 action 建立互斥 Schema：

| action | 必填 | 禁止 |
|---|---|---|
| `ask_user` | question、missingInputs | refs/plan |
| `continue_agent` | reason | refs/plan |
| `run_full` | 恰好一个 workflowRef、bindings | fragment/plan |
| `run_fragment` | 恰好一个 fragmentRef、bindings | workflow-only ref |
| `compose` | ExecutionPlan 或 plan draft | 未披露 ref |

- [x] 路由逐项处理 action，不允许回退为第一个候选。
- [x] `run_full` 可直接锁定精确 WorkflowVersion，不依赖 Component default 恰好相等。
- [x] `ask_user` 保存问题并在回答后恢复同一 disclosure session。
- [x] `compose` 编译失败时把结构化 diagnostics 返回 TraeX，允许在有限步数内修正，不执行未通过计划。

完成条件：五种 action 各有 API/AgentHost 黑盒；未披露 ref、action/ref 类型错配和超预算均 fail closed。

## 4. C：修正 Fragment 与 PlanCompiler

### C-01 FragmentPort IR

- [x] 发布 Fragment 时生成并冻结 entry/exit ports、boundary Schema、input/output mapping、closure 和 digest。
- [x] 校验每个 boundary input 能映射到闭包节点的源输入，每个 required output 都来自闭包内已声明路径。
- [x] PlanCompiler 使用 FragmentPort `inputMapping` 展开，不再把 PlanStep input 直接假设成源 Workflow 输入。
- [x] Slicer 对 ancestors/control/approval/fallback/compensation 做不动点闭包；任何隐藏依赖均拒绝。
- [x] 作者声明的 side-effect fragment 必须保留源审批/授权，`allowSideEffects` 不能绕过执行级确认。

### C-02 正确展开 Workflow/Fragment

- [x] 结果节点依赖从原 Output mapping provenance 计算，不再连接全部叶子。
- [x] fallback-only target 保留为恢复专用节点，不得连入正常 result。
- [x] 条件边、onError 和 fallback 引用在 namespace 后保持原语义。
- [x] source node↔expanded node 映射进入 PlanSnapshot，供运行详情折叠和 Patch/Recovery 归因。
- [x] 增加含 fallback、条件分支、审批和 MCP lock 的组合测试。

### C-03 PlanSnapshot 生命周期

- [x] `verify_plan_snapshot()` 区分 `for_new_run` 与 `for_existing_run`：新 Run 要求 active 且未过期；已有 Run 只要求 digest/locks 未漂移。
- [x] solidify、目录列表和建议接口拒绝或隐藏 expired/retired snapshot。
- [x] 状态迁移限定为 active→expired/retired；后台清理只移除无 Run 引用的临时目录数据，不删除审计快照。
- [x] Worker claim 前再次验证 digest 和已有 Run 可执行规则。

完成条件：过期 Plan 不能新建 Run/固化，过期前已创建的 Run 重启后仍可完成。

## 5. D：统一 Replay、FailureObservation 与修订源

### D-01 共享执行状态机

- [x] 抽取 edge readiness/selection、条件、onError、fallback-only、依赖失败、Output Schema 终态为 Runtime 纯逻辑。
- [x] Orchestrator 和 ReplayEngine 共用该逻辑；ReplayAdapter 只替换节点执行，不复制调度语义。
- [x] 所有 sideEffect 模拟结果必须满足捕获的 output fixture/Schema；无法模拟返回 `REPLAY_UNSUPPORTED`。
- [x] Replay passed 同时要求：baseline 同 fingerprint、patched 原失败点成功、必要下游成功、最终输出 Schema/断言通过。
- [x] evidence 保存选中边、skipped 节点、Schema 诊断、fixture digests 和 baseline/patched diff。

### D-02 Observation 执行源判别联合

- [x] FailureObservation 保存 `workflow_version_id | plan_snapshot_id`、source NodeRun、checkpoint、locks、contract 和 recovered outcome。
- [x] 进入 Recovery 前记录初始失败；恢复结束后原子更新 recovered/decision/checkpoint，不丢失已恢复故障。
- [x] WorkflowVersion patch 仍生成 Workflow draft；PlanSnapshot patch 生成新 PlanSnapshot 或人工 Workflow draft。
- [x] baseline reproduction 必须绑定同一 execution source digest、fixture digest 和安全输入 snapshot。

完成条件：正常 Workflow、恢复成功 Workflow 和临时 Plan 三类失败都能形成可追踪 observation；只有各自合法的修订产物可生成。

## 6. E：收紧 Catalog 与 Distillation

### E-01 Catalog 活跃版本与有界检索

- [x] Manifest 区分 discoverable 与 resolvable；普通 search 每个 logical Workflow 只返回 active/default 版本。
- [x] 旧精确 ref 仍可 describe/run，不占 top-k。
- [x] 将候选预筛选和 limit 下推 SQLite FTS/gram 索引，再对有限候选 rerank；1000/10000 规模同时测响应和查询成本。
- [x] 语义 metadata 限定类型/长度，扫描 secret-like value、凭据 URL 和超长 prompt 内容。

### E-02 Distillation 分组与 Artifact

- [x] ProposedNode 只允许连续且拓扑闭合的 action group，或实现完整 DAG contraction 后再编译。
- [x] `variableCandidates` 要么进入明确的 Compiler 裁决，要么从公开 Schema/声明中删除，禁止“接收但忽略”。
- [x] grouped node 保存所有 capability refs/digests，Agent 白名单和版本锁保持一致。
- [x] 首次 Browser 固化 E2E 经真实 Browser Tool Broker 访问页面，不再由 fixture 硬编码正文。
- [x] Conversation trajectory 登记标准 Artifact 行，复用统一下载、校验、保留与 GC 生命周期。

## 7. F：产品与文档

- [x] Chat 统一展示 disclosure session：已加载 L0/L1/L2、Agent action、缺失输入和选择理由。
- [x] `run_fragment/compose` 在执行前展示 Plan preview：refs/digests、端口 mapping、Agent/LLM、审批、副作用、TTL。
- [x] Plan 编译失败展示结构化 diagnostics，并允许继续 Agent 修正或返回普通对话。
- [x] Run detail 按 source step 折叠 namespaced nodes，显示 sourceRefs、Plan status/expiry、Recovery generation 和 Replay evidence。
- [x] Component/Workflow 编辑器提供 capability metadata 与 callable fragment 端口表单，不要求手写 JSON。
- [x] README 和 [`docs/demo-acceptance.md`](../docs/demo-acceptance.md) 按可证明范围同步；首次 Browser fixture 与真实访问明确区分。

## 8. 008 门禁矩阵

| 门禁 | 必须证明 |
|---|---|
| Selection-5 | TraeX 五种 action 都有真实终态，任何 action 不被静默改成第一个 full Workflow |
| Progressive tool loop | 同一 Agent run 只按需 L0→L1→L2；未选内容不进上下文 |
| TraeX composition | Agent 选择 Fragment + Workflow + Agent 生成引用式 Plan 并运行 |
| Disclosure persistence | API 重启后 session 预算/ref 仍正确，跨 session ref 不泄漏 |
| Recovery fencing | A 迟到、B 重领时只有 B 可提交 |
| Replan no escalation | type/capability/MCP/sideEffect/approval 扩张全部拒绝 |
| Fragment ports | 输入别名、输出路径、fallback/approval 闭包正确执行 |
| Plan fallback | 含 fallback Workflow 正常路径可组合，失败时仍进入 Recovery |
| Plan lifecycle | 过期后禁新 Run，既有 Run 可恢复 |
| Replay branches | true/false 条件、onError、fallback-only 与最终 Schema 和正常 Runtime 一致 |
| Observation sources | Workflow、recovered Workflow、Plan 三类失败均可归因和修订 |
| Catalog active version | 新版本发布后普通 search 只返回 active，旧 ref 仍可精确运行 |
| Real first browser | 首次 TraeX 探索正文来自真实页面，不是 fixture 常量 |

## 9. 实施顺序

1. 先完成 A：fencing 和 replan 扩权属于数据正确性，必须最先关闭。
2. 完成 C、D：修正 Fragment/Plan/Replay 的共享执行语义。
3. 完成 B：在可靠底座上接入 TraeX 工具循环和五种 Selection。
4. 完成 E、F：优化目录、固化证据和产品交互。
5. 最后运行完整门禁，并用 `REPLAY-008.md` 逐条链接到测试和持久证据。

只有上述门禁全部通过，才能把“Workflow 对 TraeX 渐进可见、可局部提取、可临时组合和稳定固化”从 `partial` 提升为 `available`。
