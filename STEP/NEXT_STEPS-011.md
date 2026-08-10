# NEXT_STEPS-011：闭合修订版本身份、接力 fencing 与失败判定

依据：[`REPLAY-010.md`](REPLAY-010.md)、[`review/design-compliance-review-011.md`](review/design-compliance-review-011.md)、[`review/code-review-011.md`](review/code-review-011.md)。

范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 目标与最短路径

011 已证明“首次网页总结→固化→新对话二次复用”真实可用，下一版不再重复扩展这条主链。
剩余工作的本质是让故障链具备三个不能靠提示词或测试拼接替代的不变量：

```text
同一能力的修订仍属于同一 Workflow 逻辑身份
领取 checkpoint 的 token 约束 continuation 与终态
失败是否可修订由现实证据决定，而不是重放历史 error 自证
```

目标链：

```text
已发布 Workflow v1 的真实外部节点失败
  → 持久失败证据与 observation
  → 瞬态则接力/结束，不生成 Patch
  → 契约漂移则 Revision Agent 生成受控 proposal
  → 应用 Patch 并通过 baseline/patched replay
  → 在同一 Workflow 上形成人工可审核 draft
  → 人工发布为 v2
  → search 只返回 v2，v1 精确 ref 仍可运行
  → 原输入与新输入均成功
```

## 2. A：修复 Workflow 修订的逻辑身份

### A-01 Patch 产生同一 Workflow 的新草稿

- [x] `apply_version_workflow_patch()` 从 base version 解析 `workflow_id`，不得创建新 Workflow 行。
- [x] 在原 Workflow 上创建受 base version/draft revision 约束的修订草稿；并发草稿冲突返回 409，不静默覆盖。
- [x] 草稿 metadata 保存 `baseVersionId`、observation id、fixture digest、ReplayEvidence id 和 proposal digest。
- [x] 人工 publish 生成 `WorkflowVersion.version = max + 1`；v1 定义和历史 Run 不变。
- [x] API 返回 `draftWorkflowId` 时明确它等于逻辑 Workflow id，并暴露预期 nextVersion。

### A-02 Active/resolvable 门禁

- [x] 发布 v2 后，同 Workflow 的 v1 Manifest 变为 `resolvable`，v2 为唯一 `published`。
- [x] `workflow.search` 不返回 v1；旧 `workflowRef` 仍可 `describe` 和显式运行。
- [x] Fragment Manifest 同步处理旧版本状态，不能留下旧 fragment 继续占 active 目录。
- [x] 增加从 Patch→publish→search→resolve 的测试，禁止只手工插入 version_two。

完成条件：外部失败修订测试得到同一 `workflow_id` 的 v1/v2，而不是两个 Workflow 的 v1。

## 3. B：按证据类型重建失败复现策略

### B-01 区分“契约证据重放”和“外部状态复现”

- [x] ReplayFixture metadata 增加 `evidenceKind`：例如 `observed_contract_output`、`transport_error`、`timeout`。
- [x] PAGE_DRIFT/CONTRACT_VIOLATION：可用冻结 observed output 对冻结/修订契约重新校验。
- [x] DEPENDENCY_UNAVAILABLE/TIMEOUT：历史 error fixture 不得直接设置 `baselineReproduced=true`。
- [x] 网络类复现必须来自重试耗尽后的独立主动探测，或两个独立真实 Run 的同 fingerprint 观测。
- [x] 主动探测失败也只能生成诊断；不得通过 Agent 修改 URL、网络权限或 Secret。

### B-02 让 occurrence 阈值真正进入 eligibility

- [x] `observation_eligibility()` 同时检查 classification、evidence kind、baseline 结果和 occurrence。
- [x] 删除“注释说 occurrence≥阈值、代码不检查”的不一致。
- [x] 一次网络波动且之后恢复：classification=`transient`，Revision Agent 调用计数为 0。
- [x] 两次独立可复现契约漂移：允许 proposal，但 proposal 不自动应用或发布。
- [x] 记录 reproduction policy 与证据 id，UI/API 能解释“为什么修订/为什么不修订”。

完成条件：冻结一条 TIMEOUT/DEPENDENCY_UNAVAILABLE fixture 无法单独获得 Patch 资格。

## 4. C：闭合 TaskCheckpoint fencing 与“不重做”

### C-01 Claim handle 全程 CAS

- [x] 定义 `ClaimedTaskCheckpoint(id, owner, generation, token)`；`claim()` 返回该 handle。
- [x] `start_continuation()` 必须携带 handle，用单条 UPDATE CAS 完成 `running→continuing`。
- [x] 只有 CAS 成功者才能 enqueue continuation、写 `continuationRunId` 和 `agent.handoff.started`。
- [x] reconcile 使用 checkpoint generation/token、continuationRunId 和当前 status 做 CAS；终态事件只写一次。
- [x] user answer 也使用 status+digest CAS，避免读取后并发消费。
- [x] 增加同名 Worker A 超租约、B 重领、A 迟到继续的双 Session 测试；只能产生一个 continuation。

### C-02 执行级 completed-action ledger

- [x] checkpoint 为已完成 ToolResult 保存规范化 action signature：capability ref/version、参数 digest、effect、idempotency key、result Artifact digest。
- [x] Recovery Host 在 Broker 执行前检查 ledger；相同已完成 signature 返回已保存结果或拒绝重做。
- [x] 只读步骤默认复用 Artifact；需要刷新时必须由剩余计划显式声明新输入/新 signature。
- [x] effect!=none 且 outcome unknown 必须 `awaiting_user`；用户回答不能隐式把未知结果改写为失败。
- [x] 测试统计 lookup/browser/side-effect 服务调用次数，证明 continuation 没有重做已完成步骤。

完成条件：Prompt 即使故意要求重复调用，Host 仍拒绝已完成 action；迟到 owner 无法创建或完成 continuation。

## 5. D：补齐两条不可拼接的公开黑盒

### D-01 AgentRun handoff 黑盒

- [x] 只通过公开 Conversation/AgentRun/TaskCheckpoint API 驱动，不调用 `_enqueue/_drive/Driver`。
- [x] 独立 Worker 完成一个随机只读 ToolResult 后注入可恢复失败。
- [x] 重启 Worker，TaskCheckpoint 被新 owner 领取并从 Artifact 继续。
- [x] 原 ConversationRun 保留 failed；continuation 有显式 lineage；外部调用计数不增加。
- [x] UNKNOWN_OUTCOME 分支通过公开 GET/answer API，回答只消费一次。

### D-02 Browser 外部失败修订与复用黑盒

- [x] 通过公开 API 创建/发布 v1，并让真实 Playwright 输出触发可复现契约失败。
- [x] 通过公开 observation API请求 Revision Agent proposal；测试不得手写最终 Patch。
- [x] proposal 经平台白名单校验后应用；baseline 同错误、patched replay 成功后才产生原 Workflow draft。
- [x] 人工 publish 为 v2；用原输入 A 和新输入 B 分别运行成功。
- [x] `workflow.search` 只返回 v2，v1 精确 ref 可解析；lineage 串联 observation、fixture、proposal、ReplayEvidence、v1/v2 和两个 Run。
- [x] 同场景增加一次网络瞬态对照，断言不调用 Revision Agent。

完成条件：D-01/D-02 均使用临时 Alembic 数据库、真实 API、独立 Worker 和持久 Artifact；不得用多条单元测试拼成闭环。

## 6. E：统一 Artifact 与 continuation 上下文

### E-01 ToolResult Artifact 生命周期

- [x] `_persist_tool_result_artifact()` 改为 staging→DB CAS/登记→commit→atomic promote。
- [x] metadata 保存 stagingKey，复用 Worker 启动 reconcile 和 GC。
- [x] duplicate call id 比较 hash；一致则幂等复用，不一致则报 evidence conflict。
- [x] 加入“写 staging 后事务失败/进程退出”的恢复测试，最终目录不留无 DB 引用文件。
- [x] `_load_completed_outputs()` 校验 resolved path 必须位于该 run 的 artifact root 内。

### E-02 `continue_agent` 保存真正的渐进披露结果

- [x] 不再把只有 callId/status 的 timeline 称作 ToolResult。
- [x] 保存有界脱敏的 search/describe result 或 Artifact 引用与 digest。
- [x] continuation 至少获得候选 ref、关键 synopsis、排除理由、披露 level 和剩余预算。
- [x] 继续保持 L0/L1/L2 渐进暴露，不把完整 Manifest/DAG 一次性塞入上下文。
- [x] 增加 Agent 依据已披露结果继续、且不重新搜索的断言。

## 7. F：规范、回归与报告门禁

- [x] `docs/demo-acceptance.md` 明确“修订版本必须保持同一 Workflow 逻辑身份”。
- [x] 把 transient reproduction policy 和 evidenceKind 写入验收规范。
- [x] AgentRun handoff 增加 checkpoint fencing 与 action ledger 失败判定。
- [x] E2E-9/E2E-9b 合并为同源连续场景或共享持久 source ids，禁止测试进程内伪关联。
- [x] `REPLAY-011.md` 逐条回放本文件任务，记录实际 commit、命令、退出码和测试计数。
- [x] 完整执行 `make demo-check`，并单独运行 D-01、D-02 证明它们不是被其他测试的共享状态带过。

## 8. 012 门禁矩阵

| 门禁 | 必须证明 |
|---|---|
| Main reuse regression | 首次总结→固化→新对话 L0/L1→新输入成功仍通过 |
| Logical version identity | Patch 发布得到同一 Workflow v2；v1 仅 resolvable |
| Transient policy | 一次 transport/timeout fixture 不获得 proposal 资格 |
| Contract reproduction | observed output 对契约重校验可稳定证明 drift |
| Revision Agent continuity | 同一 Browser observation 产生 proposal、Patch、Replay 和 v2 |
| Task fencing | 同名迟到 owner 无法创建第二 continuation 或写第二终态 |
| No-redo | Host ledger 阻止已完成 action；外部调用计数不增加 |
| Public handoff | API + 独立 Worker + 持久 DB 完成 checkpoint 接力与 user answer |
| Revised reuse | v2 对原输入、新输入均成功；Catalog 只返回 active v2 |
| Lineage | observation/fixture/proposal/evidence/v1/v2/run 全部持久引用并校验 digest |
| Quality | `make demo-check` 一次全绿；专项黑盒可独立重复运行 |

上述门禁完成前，可以继续表述“主复用链 available”，但不得把整个通配型失败接力与修订闭环
标记为 `closed-loop`。
