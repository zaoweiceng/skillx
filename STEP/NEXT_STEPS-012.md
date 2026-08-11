# NEXT_STEPS-012：把 Replay 证据绑定到最终发布版本

依据：[`REPLAY-011.md`](../REPLAY/REPLAY-011.md)、[`review/design-compliance-review-012.md`](../review/design-compliance-review-012.md)、[`review/code-review-012.md`](../review/code-review-012.md)。

范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 目标与最短路径

主复用、同源版本、失败分类和 TaskCheckpoint 执行原语已经存在。012 不再增加新的工作流类型，
只闭合一个根因：**通过 Replay 的内容必须与最终发布的内容逐字节等价。**

目标链：

```text
FailureObservation
  → immutable RevisionProposal / HumanPatch
  → apply Patch 得到 patchedDefinitionDigest
  → ReplayEvidence 锁 base/patch/patched digests
  → draft 锁 replayApprovedDraftDigest
  → 人工审核指定 draftRevision + digest
  → publish CAS 再校验 proof
  → immutable WorkflowVersion v2 lineage
```

任何 Replay 后编辑都必须回到：

```text
dirty_after_replay → 重新 compile/replay → 新 proof → 再发布
```

## 2. A：持久化 Revision proposal 与 Patch 来源

### A-01 RevisionProposal 实体

- [x] 新增 `revision_proposals` 表：id、observation id、base version id、Agent run id、prompt version、fixture digest、baseline digest、proposal JSON/digest、status、时间戳。
- [x] proposal API 在返回前持久化记录；同 observation/baseline/proposal digest 幂等复用。
- [x] Revision Agent 输出仍只能通过 WorkflowPatch Schema 和能力扩张白名单。
- [x] API 返回 `proposalId/proposalDigest`，不再只有无状态 JSON。

### A-02 区分 Agent proposal 与人工修改

- [x] Patch endpoint 默认接受 `proposalId + proposalDigest`，重新加载不可变 proposal，不信任客户端重复提交的 JSON。
- [x] 若用户编辑 proposal，创建 `HumanPatch` 或新的 proposal revision，保存 parent proposal id、编辑者和新 digest。
- [x] 不得把任意客户端 Patch 的 digest 命名为 Revision Agent `proposalDigest`。
- [x] Patch 与 base version、observation、fixture set 不匹配时返回 409。

完成条件：历史 WorkflowVersion 可独立解析“哪个 Agent proposal/人工 Patch 产生了该版本”。

## 3. B：让 ReplayEvidence 锁定定义内容

### B-01 扩展 ReplayEvidence

- [x] 保存 `baseDefinitionDigest`、`patchDigest`、`patchedDefinitionDigest`、`proposalId/humanPatchId`。
- [x] digest 统一使用 Compiler 规范化 definition，而不是请求 JSON 的字段顺序。
- [x] `passed=true` 只代表该组完全锁定的 digest 通过；证据记录不可原地修改。
- [x] fixture、safe input、failed node 与 execution source digest 继续参与 proof digest。

### B-02 Draft proof 状态机

- [x] Workflow draft metadata 保存 `replayEvidenceId` 和 `replayApprovedDraftDigest`。
- [x] `update_draft()`、`contract-patch`、可视化编辑器更新只要改变 definition digest，就原子置为 `dirty_after_replay`。
- [x] 失效时不得保留看似有效的 `pending_revision`；definition 中旧 revisionLineage 标记 superseded 或移除。
- [x] 相同 digest 的幂等保存不使 proof 失效。
- [x] 提供对当前 dirty draft 重新 Replay 的公开 API；通过后生成新 ReplayEvidence，不覆盖旧证据。

完成条件：Replay 后修改一个 timeout、selector 或 outputSchema，publish 必须拒绝，直到新 Replay 通过。

## 4. C：把人工发布绑定到审核快照

### C-01 Publish request 与 CAS

- [x] 定义 `PublishRequest(draftRevision, draftDigest, replayEvidenceId?)`。
- [x] publish 用 workflow id + draft revision + digest + status 单条 CAS 领取发布。
- [x] revision publish 必须重新计算 normalized compiled digest，并与 `replayApprovedDraftDigest` 和 ReplayEvidence 的 patched digest 相同。
- [x] base version 必须仍是 latest；不一致统一返回结构化 409。
- [x] 发布成功后保存 `publishedFromDraftRevision/digest`，并将 Workflow draft 同步到新版本。

### C-02 幂等与并发

- [x] 同一 draftRevision/digest 的重复 publish 返回同一 WorkflowVersion，不创建相同 v2/v3。
- [x] 两个并发 publish 只有一个创建 N+1；另一个得到同一结果或受控 409，不暴露 IntegrityError。
- [x] 未变化的 published draft 再次 publish 不生成新版本。
- [x] 增加双 Session/双 HTTP 请求交错测试。

完成条件：用户确认的版本就是实际发布版本；不存在“确认 A、发布 B”。

## 5. D：补齐证据连续性黑盒

### D-01 Replay 后编辑必须失效

- [x] 公开 Browser 故障链生成 proposal、ReplayEvidence 和 approved draft。
- [x] 通过公开 draft API 修改一个允许字段。
- [x] 使用旧 draftRevision/digest publish 返回 409。
- [x] 使用新 revision 但旧 ReplayEvidence 仍返回 `REPLAY_PROOF_STALE`。
- [x] 重新 Replay 后人工 publish 成功；v2 lineage 指向新 proof，旧 proof 保留但不再 active。

### D-02 Proposal lineage

- [x] v2 Run detail 能看到 proposal id/digest、Agent run/prompt version、Patch digest、ReplayEvidence 和 base v1。
- [x] 修改客户端 proposal JSON 但沿用 proposalId 时 fail closed。
- [x] 人工编辑 proposal 时 lineage 明确显示 HumanPatch，不能伪装成 Agent 原提议。

## 6. E：补齐失败接力与瞬态真实黑盒

### E-01 Worker 重启后领取 pending TaskCheckpoint

- [x] E2E 提供受控 Worker role/测试门，使 source failure 后 TaskRecovery 暂不 claim。
- [x] checkpoint 为 pending 时 crash 旧 Worker，再启动新 worker id。
- [x] 新 Worker claim generation+1，从 ToolResult Artifact 创建 continuation。
- [x] continuation 故意重复相同 action；Host ledger 复用结果，外部 Adapter 调用计数不增加。
- [x] 原 Run 保留 failed，checkpoint 与 continuation lineage 完整。

### E-02 真实 transport/timeout 对照

- [x] 动态外部服务支持受控断连、503 和超时。
- [x] 由真实 Browser/HTTP/MCP Adapter 产生 transport_error/timeout fixture，不使用 Transform 替身。
- [x] 两个独立 Run 达到 occurrence 阈值后，proposal 仍返回 422，Revision Agent 调用计数为 0。
- [x] 外部恢复后新运行成功，不生成 Workflow 修订草稿。

## 7. F：收紧 Artifact reconcile

- [x] `reconcile_conversation_artifacts()` 在任何文件操作前验证 target/source 都位于 run root。
- [x] staging promote 前比较 Artifact size/sha256；不一致标记 `corrupt` 并删除或隔离 staging。
- [x] target 已存在时也校验 hash，不能仅因 `is_file()` 就视为有效。
- [x] 报告区分 recovered、missing、corrupt、pathRejected 和 orphanRemoved。
- [x] 添加篡改 storageKey、损坏 staging、损坏 final file测试。

## 8. G：规范与最终门禁

- [x] `docs/demo-acceptance.md` 增加“Replay-approved digest 必须等于 publish digest”。
- [x] 增加“任何 Replay 后编辑使 proof stale”的通用失败判定。
- [x] 明确 proposal、HumanPatch、ReplayEvidence、WorkflowVersion 的不可变 lineage 字段。
- [x] `REPLAY-012.md` 记录实际 commit、命令、退出码、测试计数和未完成项。
- [x] 完整运行 `make demo-check`；D-01、E-01、E-02 必须能独立运行，不依赖模块共享状态。

## 9. 013 门禁矩阵

| 门禁 | 必须证明 |
|---|---|
| Main reuse | 首次总结→固化→新对话 L0/L1→第二次新内容成功 |
| Proposal persistence | Agent proposal 有不可变 id/digest/prompt/fixture lineage |
| Replay binding | base/patch/patched definition digests 全部锁定 |
| Edit invalidation | Replay 后任意定义变化使旧 proof stale |
| Publish snapshot | 发布使用人工审核的 draftRevision/digest |
| Publish concurrency | 并发/重复请求不会创建重复版本或返回裸 IntegrityError |
| Version lineage | v2 可独立追溯 proposal/HumanPatch、proof、base v1 |
| Restart handoff | 新 Worker 领取 pending checkpoint 并从 Artifact 续做 |
| No-redo | 重启后的 continuation 不增加外部 action 调用次数 |
| Real transient | 真实 transport/timeout 不调用 Revision Agent、不创建 draft |
| Artifact integrity | 越界/损坏 staging 不 promote，最终文件 hash 有效 |
| Quality | `make demo-check` 一次全绿；专项黑盒独立通过 |

这些门禁完成前，可表述“主复用和故障修订执行路径 available”，但不得把发布证据链标记为
`closed-loop`。
