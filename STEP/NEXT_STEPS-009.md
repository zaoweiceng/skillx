# NEXT_STEPS-009：闭合通配型探索、固化、复用、接力与修订

依据：[`REPLAY-008.md`](REPLAY-008.md)、
[`review/design-compliance-review-009.md`](review/design-compliance-review-009.md)、
[`review/code-review-009.md`](review/code-review-009.md)。

范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 009 目标与最短路径

目标不是再增加独立 REST API，而是让一个任务沿同一条持久证据链完成：

```text
Task/AgentRun
  → 真实 ToolRequest ↔ ToolResult
  → 成功轨迹 + Artifact
  → Distillation Draft
  → 人工发布 WorkflowVersion + Catalog Manifest
  → TraeX 渐进发现并换输入复用
  → 失败生成 Checkpoint，TraeX 从剩余节点接力
  → FailureObservation 判断瞬态/可复现
  → 受控 PatchProposal + ReplayEvidence
  → 人工发布修订版本并再次复用
```

必须维持以下不变量：

1. Agent 的下一步只能建立在 Host 返回的真实 ToolResult 上，不能建立在 Agent 自报 result 上。
2. Agent 负责语义选择和修订提议；Registry/Compiler/Runtime 决定可见范围、可执行性、能力锁、
   副作用和版本。
3. 每个阶段必须持久化上一阶段 source id/digest，不能靠 UI 或测试进程内变量串联。
4. 失败接力不重放已完成的非幂等副作用；结果未知必须转用户确认。
5. 瞬态故障只接力/重试，不修改固化版本；只有同源 baseline 可复现且 patched replay 成功才
   生成修订草稿。
6. Workflow 是通用框架；Browser、MCP、文件、API 只是可注册 Action Adapter。

## 2. A：先恢复可执行基线

### A-01 修复 P0

- [ ] 删除 `replay.py` 中未声明、未使用的 `pypolaris` import 和误导性注释；继续通过统一
  `expressions.py` 解析引用。
- [ ] 运行 Ruff、后端全量 Pytest、Alembic、前端检查和 E2E；更新测试数必须来自本次命令
  输出，不能沿用旧报告。
- [ ] `REPLAY-009.md` 记录 commit、环境、命令、退出码和各门禁计数。

完成条件：`make demo-check` 在干净依赖环境一次通过，API 可导入并启动。

## 3. B：实现通配、双向的 Agent Tool Broker

### B-01 ToolRequest/ToolResult 协议

- [ ] 定义判别联合：`tool.requested` 包含 callId、capabilityRef、arguments；
  `tool.result` 包含同 callId、status、typed output/error、evidence digest。
- [ ] TraeX 发出 request 后必须暂停；Host 校验 session、能力、锁、预算和副作用后执行 Adapter，
  再把 result 回送同一 Agent 进程。
- [ ] 替换当前 `stdin=DEVNULL` 单向协议；支持取消、超时、Agent/Host 任一侧崩溃重连和未闭合
  call 的确定终态。
- [ ] `agent.completed` 前必须保证所有 ToolRequest 已闭合，最终输出 provenance 只能引用真实
  ToolResult 或 Agent step output。
- [ ] 删除“收到 completed 后重写 result”的 Browser 特判；BrowserRead 作为一个注册 Adapter
  接入统一 Broker。

### B-02 通配 Adapter

- [ ] 为 Browser read、锁定 MCP、受控本地只读/Transform 建立同一 Adapter 接口：describe、
  validate、execute、redact、fixture policy、effect/idempotency。
- [ ] 未注册能力不得信任 Agent 自报结果；要么保留为显式 Agent 内部推理，要么 fail closed。
- [ ] Side-effect Adapter 在执行前走已有审批/一次性授权，结果未知进入 request_user。
- [ ] 首次探索的 trajectory 保存 request、真实 result digest、argument/output provenance 和锁，
  不保存 Secret 原值。

完成条件：随机网页正文和非网页动态数据各有 E2E；TraeX 的下一工具参数和最终回答都包含
Host 返回的随机值，修改 Agent fixture 常量不会改变结果。

## 4. C：把 Registry/Plan 变成 TraeX 的真实渐进工具循环

### C-01 WorkflowToolBroker 接入 AgentHost

- [ ] 将 search L0、describe L1、describe_fragment/slice L2、compile_plan 注册为 B 中的受控
  Tool Adapter；不得让 Agent 访问数据库或内部 HTTP。
- [ ] 同一 AgentRun 绑定唯一 Disclosure Session，保存 pending task、已披露 refs/level、调用
  budget、每次 request/response call id 和 expiry。
- [ ] Agent 先看 top-k L0，只有明确需要时才能请求 L1/L2；未选择 Workflow 的 Schema/DAG
  不进入上下文。
- [ ] compile_plan 失败时把结构化 diagnostics 回送同一 Agent，在剩余步数内修正。

### C-02 五种 Selection 真实终态

- [ ] `run_full`：解析 session 已披露的精确 WorkflowRef，校验 bindings 后启动锁定版本。
- [ ] `run_fragment`：只接受已披露 FragmentRef，展示并执行冻结端口/锁/审批边界。
- [ ] `compose`：只接受已披露 refs，编译并冻结 PlanSnapshot 后执行。
- [ ] `ask_user`：持久 PendingSelection；用户回答只补 bindings，恢复原任务和同一 session。
- [ ] `continue_agent`：回到同一个通用 Agent 任务，不丢失已完成工具结果和预算。
- [ ] 移除 Chat 中对 `run_fragment/compose` 的硬编码 422；所有 action 由统一状态机消费。

完成条件：五种 action 各有公开 API + 独立 Worker 黑盒；未披露 ref、错类型、过预算、过期
session、错误 mapping 和扩权全部 fail closed。

## 5. D：让固化产物可发现、可拆取、可恢复

### D-01 Capability 与 lineage

- [ ] DistillationProposal 增加受控 CapabilityDescriptor：name、summary、useWhen、
  doNotUseWhen、tags、examples；Compiler 校验类型、长度和 Secret 后写入 metadata。
- [ ] 固化草稿保存 sourceConversationRunId、sourceTrajectoryDigest、distillationId 和 proposal
  digest；发布 Manifest 继续引用这些 lineage。
- [ ] 避免所有固化产物都叫 `conversation-agent-draft`；相似能力仍由 logical workflow id 和
  active version 去重。

### D-02 Fragment proposal

- [ ] TraeX 可基于动作 DAG 提议可调用局部；Compiler 只接受连续/拓扑闭合且有完整端口
  provenance 的候选。
- [ ] 没有静态 Fragment 时，L2 Slicer 必须通过受控 node summary 选择候选，不要求 TraeX
  猜内部 node id。
- [ ] 临时 Fragment 默认私有且有 TTL；高频使用只建议人工写回 callableFragments。

### D-03 Recovery policy

- [ ] Distillation 根据 action effect/idempotency/error taxonomy 提议 fallback 边界；Compiler
  校验 fallback target、能力白名单、预算和是否要求用户确认。
- [ ] 所有外部读取/概率节点至少有明确策略：retry→checkpoint handoff 或 fail terminal；禁止
  “没有策略但报告已支持接力”。
- [ ] 发布前 preview 明确展示哪些节点可自动接力、哪些必须 request_user。

完成条件：固化产物可被自然语言重新搜索到，可按需运行局部，并在注入可恢复故障后产生
ExecutionCheckpoint，而不是直接 failed。

## 6. E：统一 AgentRun 与 WorkflowRun 的失败接力

### E-01 TaskCheckpoint

- [ ] 提取通用 Checkpoint envelope：source kind/id/digest、已完成步骤输出/Artifact、失败步骤
  输入和错误、剩余计划、锁、已提交副作用、outcome certainty、用户上下文。
- [ ] WorkflowRun 继续复用现有 ExecutionCheckpoint；AgentRun 在工具失败/Worker 丢失时也能
  创建 TaskCheckpoint，而不是直接丢为终态。
- [ ] 对 AgentRun 的未完成 ToolRequest 做确定性分类：只读且未提交可重试；已提交幂等操作按
  key 查询；未知非幂等结果只能 request_user。
- [ ] Recovery Agent 只接收有界脱敏上下文和剩余节点，不接收全库/全 Catalog。
- [ ] 接力成功后把新动作追加到同一连续 trajectory，供后续固化/修订归因。

### E-02 fencing 与状态机门禁

- [ ] 沿用 008 的 generation/token CAS，扩展到 TaskCheckpoint 和双向 Tool call。
- [ ] 增加 Agent A 超租约、B 接力、A 迟到返回；A 的 ToolResult、Agent output、Artifact 均不得
  提交。
- [ ] 保持已完成非幂等副作用不重放，未知结果只产生一个待确认事项。

完成条件：网页和非网页探索各能在真实工具失败后从持久上下文接力，API/Worker 重启不丢
进度，迟到 owner 无写权限。

## 7. F：闭合“可复现→修订→新版本复用”

### F-01 Replay fixture 证据

- [ ] Runtime 为可回放只读节点捕获脱敏、大小受限、通过 output Schema 的 fixture Artifact；
  保存 node id、source digest、input digest、output digest、capture policy。
- [ ] Replay 从 Evidence/Artifact 读取 fixture，不再从业务 `run.input_json._replayFixtures` 读取。
- [ ] 外部 fixture 漂移或缺失时明确返回 `REPLAY_UNSUPPORTED`，不能伪造成功；副作用使用审核过
  的模拟器且写入模拟证据。
- [ ] baseline reproduction 同时锁 execution source、safe input、fixture digest 和 failure
  fingerprint。

### F-02 TraeX Patch proposal

- [ ] 新增 Revision Agent 阶段：输入 FailureObservation、Checkpoint/recovered outcome、锁和
  Replay diagnostics，输出受现有 `WorkflowPatch/RecoveryPlanPatch` Schema 限制的 proposal。
- [ ] 瞬态/网络波动恢复后默认不提 Patch；baseline 不可复现不得创建草稿。
- [ ] Compiler 重新派生能力/MCP/Browser/副作用锁；Agent 不得自报或扩大锁。
- [ ] Workflow source 生成新 Workflow draft；Plan source 生成新 PlanSnapshot 或人工固化建议，
  不能混用 execution source。
- [ ] Replay 通过后仍需人工发布；发布后 Catalog active version 切换，旧 ref 保持可解析。

### F-03 修订后再运行

- [ ] 保存 `patchOfObservationId/replayEvidenceId/baseVersionId` lineage。
- [ ] 人工发布后，用原失败输入和一个新输入分别运行修订版本；旧失败不再复现，输出通过
  Schema，未扩大副作用。
- [ ] 失败再次出现时按新版本生成新 fingerprint，不覆盖旧版本历史。

完成条件：Browser/MCP/Agent 至少一类真实外部节点可完成 baseline reproduction、受控修订、
发布新版本和再次复用；纯 transform 正例不能作为唯一证据。

## 8. G：修 Artifact、Schema 与产品证据

### G-01 统一 Artifact 生命周期

- [ ] 定义 `ArtifactSource = run | conversation_run` 判别联合和统一 StorageKey；路径解析不再
  假设 `run_id` 非空。
- [ ] Conversation trajectory 登记 storageKey，标准下载端点可下载并校验 sha256。
- [ ] promote/reconcile/GC 同时支持两类 source；数据库提交失败、文件落位失败和孤儿清理均有
  测试。

### G-02 数据约束与审计

- [ ] ORM 增加 `ck_failure_observation_source`，与迁移保持一致。
- [ ] DisclosureCall 用唯一 call id 绑定响应，支持 pending/succeeded/failed/expired 终态。
- [ ] 删除 Manifest/Fragment 重复列表循环并验证 locks 不重复。

### G-03 产品界面

- [ ] Chat 展示真实的 L0/L1/L2 工具调用时间线、PendingSelection、compile diagnostics 和五种
  action，而不只是 badge。
- [ ] fragment/compose 执行前展示 refs/digests、端口、模型节点、副作用、审批、TTL。
- [ ] Run detail 展示完整 lineage、Checkpoint owner generation、Observation 分类、Replay
  fixture digest、Patch 和修订版本链接。

## 9. 010 门禁矩阵

| 门禁 | 必须证明 |
|---|---|
| Build baseline | 干净环境 `make demo-check` 全绿，无未锁依赖 |
| Real Tool Broker | Agent 下一步与最终输出消费 Host 返回的随机真实 ToolResult |
| Generic adapters | Browser 与非网页操作走同一 request/result 协议 |
| Progressive TraeX | 同一 AgentRun 按需 L0→L1/L2，未选内容不进上下文 |
| Selection-5 | 五种 action 均由 TraeX 主路径得到并执行真实终态 |
| Ask resume | 用户回答恢复原 Selection/bindings，不重新搜索答案文本 |
| Distilled discovery | 固化能力可由新自然语言任务语义搜索到 |
| Distilled recovery | 同一固化版本故障后生成 checkpoint 并接力 |
| AgentRun handoff | 首次探索中断也可跨 Worker 接力，未知副作用转人工 |
| Replay evidence | 外部节点 fixture 来自 source Run Artifact，digest 锁定 |
| Revision decision | 瞬态不修订，可复现故障才生成 Patch proposal |
| Revised reuse | 修订版本发布后原失败输入与新输入均成功 |
| Lineage | 五阶段每一步都引用上一阶段 source id/digest |
| Artifact lifecycle | Conversation trajectory 可下载、校验、补落位和 GC |
| Full-loop web | 网页场景连续走完五阶段，不拼接预置演示组件 |
| Full-loop non-web | 非网页场景连续走完五阶段，证明框架通配 |

## 10. 实施顺序

1. A：先恢复后端可运行基线。
2. B：建立真实双向 Tool Broker；这是首次探索真实性的根。
3. C：在同一 Broker 上接 Registry/Plan，闭合 TraeX 渐进选择。
4. D、E：让固化产物天然可发现、可拆取、可接力，并统一 Agent/Workflow checkpoint。
5. F：捕获真实 Replay 证据并接入 Revision Agent。
6. G：修 Artifact/Schema/产品证据，最后执行两条完整五阶段黑盒。

在两条完整闭环 E2E（网页 + 非网页）通过前，不应再把单点 API 或分段演示写成“闭环完成”。
