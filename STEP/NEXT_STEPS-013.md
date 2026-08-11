# NEXT_STEPS-013：闭合首次探索中的真实失败接力

依据：[`REPLAY-012.md`](../REPLAY/REPLAY-012.md)、
[`review/design-compliance-review-013.md`](../review/design-compliance-review-013.md)、
[`review/code-review-013.md`](../review/code-review-013.md)。

范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 目标与最短路径

不要继续增加新的工作流类型。012 的成功复用和发布修订证据链已经成立，013 只补一个根因：
**真实 Tool 失败必须保留原始错误，并进入与已知 AgentRun 失败相同的重试/Checkpoint/Continuation
状态机。**

目标链：

```text
首次 Agent 探索
  → Host Browser/MCP/HTTP ToolResult failed
  → 保留标准错误与已完成 action
  → 按统一策略有限重试
  → 重试耗尽后 pending TaskCheckpoint
  → Worker 可崩溃/重启
  → 新 Worker 从 Artifact + remaining plan 接力
  → 外部恢复后完成任务
  → 固化草稿并人工发布
  → 第二个新对话渐进发现并复用
```

## 2. A：定义 Host 拥有的失败终态

### A-01 Agent 失败协议

- [ ] 在 Agent 事件协议中增加结构化 `agent.failed`，字段至少包含 code、message、retryable 和失败
  call id；或由 Host 在 failed ToolResult 后直接生成等价终态。
- [ ] `agent.completed` 只表示成功输出，继续强制逐字段 provenance；不得用虚假 provenance 为失败文本
  背书。
- [ ] 当 Agent 在 failed ToolResult 后返回非法 completed 时，Host 优先保留已知 Tool 根因，同时另记
  protocolViolation 供审计，不得用次生错误覆盖主错误。
- [ ] Agent 子进程提前退出且最后一个可信事实是 failed ToolResult 时，也传播该 Tool 标准错误。

### A-02 统一错误策略

- [ ] 抽取共享 `ErrorPolicy`，由 Workflow Runtime、AgentHost、AgentRunService 和 TaskCheckpoint 共用。
- [ ] 明确 `DEPENDENCY_UNAVAILABLE`、`TIMEOUT`、限流和连接中断的 retryable/retry budget/backoff。
- [ ] Contract/Safety/Permission/invalid event 等确定性错误 fail closed，不自动交给 Agent 扩权修复。
- [ ] AgentRun 不再用 `{"TIMEOUT", "EXECUTION_FAILED"}` 硬编码决定 pending/terminal。

完成条件：API、事件、checkpoint 中看到的主 error code 与真实 failed ToolResult 一致。

## 3. B：把 failed ToolResult 变成可接力上下文

### B-01 Artifact 与 checkpoint

- [ ] failed 的只读 ToolResult 也保存有界、脱敏 Artifact，包含 call id、capability、arguments digest、
  标准错误、evidence digest 和 action signature。
- [ ] TaskCheckpoint 明确区分 completed actions、failed action、pending calls 和 remaining plan。
- [ ] checkpoint source digest 覆盖这些字段；Continuation 加载前校验 Artifact id/sha256/evidence digest。
- [ ] Artifact 缺失或损坏时 fail closed，并给出可诊断状态，不能静默重新执行未知 action。

### B-02 Continuation 语义

- [ ] 新 Agent 只获得冻结的已完成结果、失败事实、用户输入和剩余计划，不重新披露全 Catalog。
- [ ] 已完成 action signature 继续由 Host ledger 强制复用或拒绝重做。
- [ ] 对失败的纯读取 action，可在策略允许且外部恢复后重试；副作用 outcome unknown 必须先询问用户。
- [ ] continuation 成功后保留 source run failed、checkpoint、continuation run 和最终输出的完整 lineage。

完成条件：首次 Browser 失败后恢复，不重做失败前已经成功的 Tool，并能继续 distill。

## 4. C：稳定 Browser timeout 与 transport 分类

- [ ] 捕获 Playwright TimeoutError，稳定映射为标准 `TIMEOUT`。
- [ ] Browser 导航 timeout 必须小于节点总 timeout，给清理和结构化返回预留时间。
- [ ] 503、断连、DNS/连接失败映射 `DEPENDENCY_UNAVAILABLE/transport_error`；页面契约失败保持
  `PAGE_DRIFT/observed_contract_output`。
- [ ] 节点取消时记录首次确定的终止原因，Context 关闭引发的次生异常不得覆盖它。
- [ ] 分别独立重复 503、断连、timeout 场景至少 5 次，classification 与 evidence kind 必须稳定。

## 5. D：统一两类 Artifact reconcile

- [ ] 抽取共享的安全路径解析和 `size + sha256` 校验函数。
- [ ] `reconcile_run_artifacts()` 对 final 与 staging 执行与 Conversation Artifact 相同的 root/hash 校验。
- [ ] 损坏 final 在有有效 staging 时恢复；否则删除或隔离并报告 corrupt。
- [ ] stagingKey/storageKey 越界时报告 pathRejected，任何文件操作前先验证 root。
- [ ] `promote_staged_artifact()` 不得仅因 target 存在就成功；必须验证目标内容。
- [ ] 增加 Workflow Run 的 escaped key、corrupt staging、corrupt final 和 replay fixture 回归测试。

## 6. E：并发与幂等收口

- [ ] 增加 RevisionProposal 并发创建测试；唯一索引冲突必须回读同一 proposal 或返回受控 409，不能泄漏
  IntegrityError。
- [ ] 保持 publish 双 Session/双 HTTP 幂等测试；验证服务重启后重复审核快照仍返回同一 version。
- [ ] no-op draft 保存若回填了缺失 digest，必须提交回填或完全不修改 ORM，避免响应与数据库状态不一致。

## 7. F：单条真实通配闭环黑盒

新增一条不依赖其它用例状态的公开 E2E：

1. 新建 Conversation A，请求访问动态网页并总结。
2. Browser 服务先返回 503/断连，Host 记录原始 failed ToolResult。
3. 有限重试耗尽，产生 pending TaskCheckpoint；断言错误不是 `AGENT_EVENT_INVALID`。
4. continuation 创建前 crash 旧 Worker，启动新 worker id。
5. 新 Worker claim 新 generation，从 Artifact 加载上下文；服务切回正常。
6. continuation 只重试失败 Browser action，完成总结。
7. 对 continuation 成功轨迹 distill，人工按 revision/digest 发布。
8. 创建 Conversation B，用新 URL/新正文自然语言请求同类任务。
9. TraeX 经 L0/L1 选择固化 Workflow，Runtime 成功返回新内容。
10. 断言 source/continuation/distillation/version/second run lineage 连续，已完成 action 调用数不增加。

该用例必须能单独运行；连续执行 5 次不得依赖模块级共享数据、测试顺序或上一个 Browser Context。

## 8. G：文档与门禁

- [ ] 更新 `docs/demo-acceptance.md`：加入“真实 failed ToolResult 不得被 protocol error 覆盖”。
- [ ] 明确 Agent 成功终态、失败终态、retryable policy 与 TaskCheckpoint 的状态转换。
- [ ] `REPLAY-013.md` 记录实际 commit、命令、退出码、测试计数和任何未完成项。
- [ ] 运行 `make demo-check`。
- [ ] 主复用、Browser revision、pending restart 和新 F 场景分别独立运行。
- [ ] 对 Browser timeout/transport 与新 F 场景执行重复稳定性门禁。
- [ ] 运行 `git diff --check`，确认没有测试 Artifact 或临时数据库进入仓库。

## 9. 014 完成矩阵

| 门禁 | 必须证明 |
|---|---|
| Main reuse | 首次成功总结→固化→新对话 L0/L1→第二次新内容成功 |
| Tool failure fidelity | 最终 error code 保留真实 Tool 根因，不被 Agent 协议错误覆盖 |
| Retry policy | Browser/MCP/HTTP transient 按同一策略有限重试 |
| Failure handoff | 重试耗尽生成 pending checkpoint，新 Worker 可接力 |
| Context continuity | failed/completed ToolResult、remaining plan、用户上下文均可校验加载 |
| No redo | completed action ledger 在 Host 执行层阻止重复调用 |
| Resume then distill | continuation 成功结果可以继续固化并发布 |
| Second reuse | 新 Conversation 渐进发现并运行恢复后固化的能力 |
| Revision binding | Replay proof、审核 draft 和发布 version digest 一致 |
| Timeout stability | timeout/transport 重复运行分类稳定，不受关闭竞态影响 |
| Artifact integrity | Workflow 与 Conversation Artifact 都执行 root/size/hash 校验 |
| Quality | 全量门禁一次全绿，关键专项独立且重复通过 |

在这些门禁完成前，准确表述是：**成功复用与发布修订闭环已完成；首次探索真实外部失败的自动接力仍未
完成。**
