# REPLAY-012：Replay 证据绑定最终发布版本

日期：2026-08-05

依据：[`NEXT_STEPS-012.md`](../STEP/NEXT_STEPS-012.md)、
[`review/design-compliance-review-012.md`](../review/design-compliance-review-012.md)、
[`review/code-review-012.md`](../review/code-review-012.md)。

执行基线：`bd2e45c`。本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 结论

012 将故障修订的因果链绑定到最终发布字节：

```text
FailureObservation
  → immutable RevisionProposal / HumanPatch
  → ReplayEvidence(base/patch/patched digests)
  → replayApprovedDraftDigest
  → PublishRequest(draftRevision/draftDigest)
  → publish CAS
  → immutable WorkflowVersion lineage
```

Replay 后定义发生变化会进入 `dirty_after_replay`。旧审核快照和旧 proof 均返回结构化
409；只有公开 `/workflows/{id}/replay` 生成新 ReplayEvidence 后才能发布。

## 2. 持久化与状态机

迁移 `0017_replay_publish_binding.py`
新增：

- `revision_proposals`：observation/base version、Agent run、prompt version、fixture/baseline、
  proposal JSON/digest 和状态；
- `human_patches`：parent proposal、editor、Patch JSON/digest；
- ReplayEvidence 的 base/patch/patched definition digest 与 proposal/HumanPatch ID；
- Workflow 的 `draft_digest`；
- WorkflowVersion 的 `published_from_draft_revision/digest` 和不可变 revision lineage。

proposal API 对同 observation/baseline 复用记录。Patch API 必须提交匹配的
`proposalId/proposalDigest`；客户端修改 JSON 默认 fail closed，只有显式 `humanEdit=true`
才创建 HumanPatch。

## 3. Replay 与发布绑定

- definition digest 均来自 Compiler 规范化结果；
- draft 同 digest 保存幂等，不增加 revision、不使 proof 失效；
- draft/contract edit 改变 digest 时，旧 lineage 标记 superseded，并保存 HumanPatch；
- revision publish 同时校验当前 digest、`replayApprovedDraftDigest`、ReplayEvidence patched
  digest、Patch digest、base latest 和 evidence ID；
- publish 以 revision/digest/status 单条 CAS 领取；
- 同一审核快照重复或并发发布返回同一 WorkflowVersion，不创建重复版本；
- WorkflowVersion 和 Manifest 均保存可独立读取的 revision lineage。

## 4. 黑盒连续性

公开 Browser 修订场景证明：

1. 两个真实契约失败 Run 生成不可变 proposal 和 ReplayEvidence；
2. Replay 后通过 draft API 修改 timeout；
3. 旧 revision/digest 发布返回 `DRAFT_SNAPSHOT_CONFLICT`；
4. 新 revision 携带旧 evidence 返回 `REPLAY_PROOF_STALE`；
5. 重新 Replay 生成新 proof，发布同源 v2；
6. v2 lineage 指向 proposal、HumanPatch、新 proof 和 base v1。

TaskCheckpoint 场景在 checkpoint 仍为 pending 时崩溃旧 Worker。新 Worker 领取
generation 1，从 ToolResult Artifact 创建 continuation；重复 action 命中 Host ledger，
`reusedFromLedger=true`，源 ConversationRun 保持 failed。

真实瞬态对照由 Playwright Browser 访问动态服务的 503、断连和延迟响应。两个独立 Run
分别达到 `transport_error`/`timeout` 阈值后，proposal 仍返回 422，专项断言
`AgentHost.execute` 调用计数为 0；服务恢复后新 Run 成功，Workflow 不产生修订草稿。

## 5. Artifact 完整性

Conversation Artifact reconcile 在访问 target/source 前验证 run root 边界；final 与 staging
均校验 size/sha256。损坏内容不会 promote，并分别报告 `corrupt`、`pathRejected`、
`missing`、`recovered` 和 `orphanRemoved`。专项覆盖越界 storageKey、损坏 staging 和损坏
final 文件。

## 6. 验证记录

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `make demo-check` | 0 | 全量门禁通过 |
| `cd backend && uv run pytest -q` | 0 | 224 passed |
| `cd backend && uv run pytest e2e -q -p no:cacheprovider` | 0 | 21 passed |
| Browser revision/stale proof 专项 | 0 | 1 passed |
| pending checkpoint restart + unknown 专项 | 0 | 2 passed |
| Alembic 空库升级 | 0 | `0001→0017` |
| 前端 TypeScript/ESLint/Vitest/build | 0 | 28 passed，production build 通过 |
| `git diff --check` | 0 | 无空白错误 |

未完成项：无（Local Demo 范围内）。

## 7. 最终判定

主复用、失败接力、真实瞬态拒绝和故障修订发布证据链均已通过独立黑盒与统一门禁。
Local Demo 范围内可将发布证据链标记为 `closed-loop`。

> 2026-09-16：以上为历史审计记录。旧数据库迁移链已移除，当前使用[固定初始结构](../backend/migrations/versions/0001_refactored_schema.py)，旧数据按要求清空重建。
