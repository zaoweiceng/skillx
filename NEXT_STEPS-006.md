# NEXT_STEPS-006：把通配骨架变成可证明的复用与恢复闭环

依据：[`REPLAY-005.md`](REPLAY-005.md)、[`review/design-compliance-review-006.md`](review/design-compliance-review-006.md)、[`review/code-review-006.md`](review/code-review-006.md)。  
范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。能力最小化、版本锁、恢复幂等、审计和升级正确性不能以 Demo 为由省略，因为它们直接决定工作流是否可复用。

## 1. 目标与不变量

006 的目标不是继续增加类型或 UI，而是让以下闭环第一次真实成立：

```text
首次 TraeX 探索
  → 带数据 provenance 的 Action IR
  → Agent 提出分区，Compiler 验证并生成可执行 mixed DAG
  → 人工发布
  → 第二次用新输入/新内容重新执行
  → 重试耗尽后冻结不可变 checkpoint
  → 唯一 fallback Agent 消费上下文并续跑
  → 可复现失败才生成受限 Patch
  → 真实 replay 通过后生成 draft，人工发布
```

必须保持八项不变量：

1. 网页只是 Adapter，文件/API/MCP 使用同一 IR、恢复和 Patch 框架。
2. Action 的调度依赖和数据依赖都显式，任何未解析引用都使草稿不可发布。
3. fallback-only Agent 在正常路径调用次数为 0，失败时至多被一个 Worker 领取一次。
4. 原失败 NodeRun 永远不可变；恢复是新 attempt/substitution，不覆盖历史。
5. Recovery Agent 只能读取 checkpoint 明确授权的值/Artifact/能力，不能猜测 Worker 内存。
6. 已完成非幂等副作用不重放；未知 outcome 必须转人工确认。
7. Patch 不能改变能力锁、权限或 Secret；真实 replay 成功也只生成 draft。
8. 任何 `available` 声明都必须由真实发布/运行黑盒测试支撑，不能由类型 round-trip 或 fixture 常量替代。

## 2. A：重建 fallback 执行模型

### A-01 fallback-only 激活语义

- [ ] 将 fallback policy 从普通 DAG 调度中分离；可选实现为 `activationMode=normal|fallback` 或独立 `RecoveryPolicy.target`。
- [ ] Compiler 验证 fallback target 不可被 normal edge/输出路径意外依赖；Orchestrator 正常扫描时永不创建 fallback-only NodeRun。
- [ ] 命中已发布 fallback 后，为 target 创建唯一 Recovery NodeRun；`RecoveryDriver` 执行该 NodeRun，而不是另起无归属 Agent 调用。
- [ ] 运行详情能区分 failed attempt、recovery attempt、substitution 和最终下游运行。

### A-02 原子领取与租约

- [ ] `ExecutionCheckpoint` 增加唯一约束 `(failed_node_run_id, attempt)`、`leaseOwner/leaseExpiresAt/heartbeatAt` 和 `running` 状态。
- [ ] 用单条条件更新/CAS 领取 `pending→running`；未领取成功的 Worker 不得调用 Agent。
- [ ] 定义租约过期语义：若 Agent/Tool outcome 不确定则 `waiting_approval`，只有能证明无副作用或有幂等键时才自动重试。
- [ ] Recovery Tool 调用携带持久 idempotency key，取消/重复投递不能执行两次副作用。

### A-03 人工审批 fallback

- [ ] `requireUserApproval=true` 命中后仍先建立 checkpoint，run 进入 `waiting_approval`，不直接失败。
- [ ] 增加批准/拒绝 API 与审计事件；批准后走同一个 Recovery claim，拒绝后保留原失败终态。
- [ ] Compiler 和 UI 明确展示错误码、能力集合、已提交副作用和将交给 Agent 的上下文摘要。

完成条件：正常运行 fallback target 调用 0 次；失败后双 Worker 并发仅调用 1 次；原失败 NodeRun 不变；审批允许/拒绝均可追溯。

## 3. B：标准 checkpoint 与不可变恢复历史

### B-01 可执行 checkpoint

Checkpoint 至少保存或引用：

- source workflow/version/digest、run、failed NodeRun/attempt；
- 已完成节点的必要输出、标准 Artifact id/hash 与 Secret handle；
- 失败节点解析后的输入、完整标准错误、重试历史和 outcome certainty；
- capability/MCP/model/page contract locks；
- 从已发布 DAG 计算的 remaining nodes、edges、input mappings 和 output contract；
- 已提交副作用、idempotency keys、外部 transaction handles；
- fallback target、最小 capabilities、预算和审批状态。

- [ ] digest 覆盖结构化 snapshot；大内容只保存 Artifact 引用，恢复前校验 hash/版本锁。
- [ ] remaining plan 排除 fallback-only target 和已完成节点，并验证所有下游输入可由 completed/substitution/run input 解析。

### B-02 真正把上下文交给 TraeX

- [ ] 平台 prompt 包含失败事实、允许决策和小型脱敏值；大输出以只读 Artifact mount/handle 提供。
- [ ] Recovery Agent 能按 node id/path 读取已完成值、失败输入和剩余节点目标；不能只看到 id 列表。
- [ ] `RecoveryDecision` 使用判别联合：`provide_substitution | retry_replanned | request_user | abort`；每种 action 禁止额外字段。
- [ ] substitution 输出用失败节点或显式 recovery contract 校验，类型不匹配 fail closed。

### B-03 不可变 attempt 与续跑

- [ ] 禁止修改失败 NodeRun 的 status/output/error；新增 `RecoveryAttempt`/`OutputSubstitution` 表或等价事件流。
- [ ] 输入解析器能显式从 substitution 取值，随后只调度 remaining plan；已成功 NodeRun 不重建。
- [ ] 恢复、取消、审批、Agent 终态和下游续跑各有 outbox/event，运行详情由事实聚合而非改写历史。

完成条件：测试让前序节点生成随机值，Recovery 输出必须包含并转换该值；不提供该上下文时测试必失败。原失败记录仍可查询，已完成副作用计数不增加。

## 4. C：让 Action IR 生成真正可执行的 mixed DAG

### C-01 显式输入 provenance

- [ ] AgentHost action started/completed 事件为每个 argument 写入 `user_input | prior_output(actionId,path) | artifact(id,path) | constant`，不再靠“值是否等于 prompt”猜测。
- [ ] `build_action_ir()` 验证引用目标、路径、Schema、DAG 无环和 action 输出关联；无法证明则 diagnostic/rejected，不能冻结首次值冒充 constant。
- [ ] 保留真正的 action DAG，不再强制所有动作串成单链。

### C-02 Proposal 与 Compiler 闭环

- [ ] 首次成功运行后由 TraeX 生成受 Schema 约束的 `DistillationProposal`，保存 proposal 与 source trajectory digest。
- [ ] Compiler 验证 proposal 覆盖每个 action 恰好一次、节点顺序/数据依赖不丢失、strategy 与能力锁/副作用/replayability 一致。
- [ ] `proposed_strategy` 真正参与裁决；Agent 建议不合法时返回可解释 diagnostics，不静默换成错误图。
- [ ] 所有 Action Compiler，包括 Agent/LLM，都使用统一 bindings 生成 input mapping；workflow output mapping 按字段路径和 Schema 生成。

### C-03 Browser Read/Extract 只是一个 Adapter

- [ ] 定义 `BrowserReadContract`：URL/input、导航策略、正文/结构化字段 selector、输出 Schema、漂移证据、超时与可重试性。
- [ ] 实现受控 Browser Executor 的 `navigate/read/extract`，输出正文或结构化字段；编译期用同一 Pydantic/JSON Schema 校验 contract。
- [ ] 不把 Playwright page/locator 句柄写入 IR；Browser adapter 只产生通用 provenance、Artifact 和能力锁。
- [ ] 文件/API/MCP adapter 使用同一 NormalizedAction 和 Registry 扩展点，不创建第二套 Distillation 服务。

完成条件：动态网页图和非网页图都能由真实首次轨迹生成、发布并用变化后的输入运行；删除合成 fixture 仍可通过。

## 5. D：把失败证据接入受控 WorkflowPatch

### D-01 持久可复现性

- [ ] 新增 `FailureObservation`：source run/checkpoint、稳定 fingerprint、classification、环境/contract 摘要、recovery 结果和 occurrence。
- [ ] 瞬态错误默认只记录；只有同版本/节点/锁的稳定 fingerprint 在隔离 fixture 可复现时才允许 proposal。
- [ ] API 不接受前端自报 `reproducible=true`；资格由后端事实计算并返回理由。

### D-02 能力不可扩张的 Patch DSL

- [ ] 按节点类型定义字段级 patch union；拒绝任意 `config` 浅合并。
- [ ] `allowedCapabilities`、capability/MCP/version/digest、model、Secret、sideEffect、idempotent 和风险级别不得通过普通 Patch 修改。
- [ ] 必须变更能力/锁时，生成独立高风险 draft change 并要求明确人工确认，不继承“修复补丁已安全”的结论。
- [ ] 键名规范化后递归校验，避免大小写/嵌套绕过；边变更重新做 reachability、fallback 和数据依赖检查。

### D-03 真实 replay 与统一治理

- [ ] 在隔离、受限环境用 source checkpoint 的安全输入/fixture 运行 patched graph 到原失败点及必要下游。
- [ ] 保存 replay run id、输出 diff、副作用模拟/幂等证据、能力 lock diff 和 diagnostics。
- [ ] replay 通过后才创建 draft；旧版本不变，仍需人工发布。
- [ ] ContractPatch 作为 typed WorkflowPatch 进入同一 eligibility、replay 和 draft 服务；删除两套独立路由语义。

完成条件：一次 TIMEOUT 无 Patch；可复现 contract drift 能生成 proposal；扩权 Patch 被拒；真实 replay 失败不创建 draft；成功后仍不自动发布。

## 6. E：修复 Host、轨迹、迁移和 Worker 边界

### E-01 Agent Host

- [ ] `agent.completed.totalTokens` 与步骤累计做一致性校验并重新执行预算上限。
- [ ] event stream 与 cancellation token 并发竞争，无事件挂起时也能立即 cancel subprocess。
- [ ] 两入口继续共用同一 Host；补终态大 Token、少报 Token、无事件取消测试。

### E-02 轨迹与 Artifact

- [ ] `ConversationRun.trajectory_json` 只保存脱敏 Action IR、摘要和标准 Artifact id，不保留原 arguments/result/Secret。
- [ ] 原始轨迹若确有调试价值，需显式开关、分类、保留期限和受控下载；默认 Local Demo 也不应复制 Secret。
- [ ] 创建标准 Artifact 行并复用 staging/outbox/promote/hash/reconcile/GC；删除手工伪造 URI 生命周期。
- [ ] 测试扫描 audit event、trajectory_json、Artifact 元数据和磁盘字节，确保 Secret 原值不可见。

### E-03 数据迁移与异常边界

- [ ] 0009 从旧 MCP Snapshot 字段按运行时 canonical 算法重算 remote-only `contract_digest`。
- [ ] 增加带两个 Tool 和人工分类数据的 0008→0010 升级/rediscovery 测试。
- [ ] `execute_claimed()` 将未知 Executor 异常转换为结构化 NodeResult，记录 traceback，始终停止 heartbeat 并推进标准状态机。
- [ ] 修正 run API `fallbackTarget`，补后端和前端契约测试。

## 7. F：真实验收场景

### F-01 动态网页总结

- [ ] 启动本地动态站点，第一次由 TraeX 使用 Browser 能力访问 URL、抽取正文并总结。
- [ ] 固化为“确定性 navigate/read/extract + 显式 TraeX summary”mixed graph；summary 输入必须引用 Browser 输出和参数化提示词。
- [ ] 发布后修改页面正文，第二次运行必须输出新内容；禁止使用首次 Artifact、常量“正文”或固定 stub 结果。
- [ ] 注入网络失败：幂等重试耗尽后 fallback target 正常路径未调用，恢复能读取前序正文/错误并只继续剩余节点。

### F-02 非网页通配样例

- [ ] 选择“读取变化的本地 JSON/CSV → 确定转换 → TraeX 归纳 → Artifact”或真实 MCP/API 等价任务。
- [ ] 使用与 F-01 完全相同的 Proposal、IR、Compiler、Checkpoint、RecoveryDecision 和 Patch 类型。
- [ ] 第二次替换输入，结果随新数据变化；中途失败后只继续 remaining plan，已完成文件写入/副作用不重复。

### F-03 并发、审批与修订

- [ ] 两个 Recovery Worker 竞争同一 checkpoint，只产生一个 RecoveryAttempt 和一次 Agent 调用。
- [ ] requireUserApproval 的批准/拒绝路径均有黑盒测试。
- [ ] Recovery fixture 的输出必须依赖 checkpoint 的随机前序值；固定输出测试不计为接力证据。
- [ ] 同一可复现 fingerprint 多次出现并真实 replay 成功才产生 draft；旧版本始终不变。

## 8. G：产品与文档收口

- [ ] 运行详情按时间线展示原失败、checkpoint、审批、recovery attempt、substitution、remaining 节点和最终输出。
- [ ] 补丁入口只在后端返回 eligible 时出现；展示 source failure、replay run、能力 diff 和发布状态。
- [ ] 修正 README 能力状态：在 F 全部通过前，“通用 Agent 探索与固化”“通用失败接力”“自动 Patch 建议”标为 `partial`。
- [ ] 删除测试中的 `or True`，禁止以类型 round-trip、静态编译或固定 fixture 作为端到端验收。
- [ ] MCP Streamable HTTP 增量流、WorkflowVersion 可视化 diff 和完整拖放仍可保持后续 `partial`，不阻断本轮内核修正。

## 9. 默认门禁与出口

`make demo-check` 必须继续通过当前 Ruff、169+ 后端测试、迁移、前端 check/lint/test/build 和 10+ E2E，并新增：

| 门禁 | 必须证明 |
|---|---|
| Mixed dataflow | prior-output/artifact 路径真实传值，Schema 不匹配不可发布 |
| Dynamic web E2E | 页面修改后重新读取并交给 TraeX，总结依赖新正文 |
| Non-web E2E | 同一框架对变化的文件/API/MCP 输入复用 |
| Fallback isolation | 正常路径 target 0 次，失败时唯一 Recovery NodeRun |
| Recovery context | Agent 输出依赖随机 checkpoint 值，失败历史不可变 |
| Recovery concurrency | 双 Worker 仅一次调用，租约过期按 outcome certainty 处理 |
| Approval | 危险错误先 checkpoint，再批准/拒绝，不直接失败 |
| Patch governance | 后端 eligibility、禁止扩权、真实 replay、只生成 draft |
| Audit/Artifact | Secret 不出现在事件、DB 轨迹和磁盘；标准 Artifact 可 reconcile |
| Data migration | 非空 0008→0010 后 unchanged discovery 稳定 |

只有这些出口全部通过，才能完成 006 修复并在下一份 REPLAY 中声称通配复用与失败接力为 `available`。
