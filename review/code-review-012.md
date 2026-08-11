# 代码审查报告（012）

审查日期：2026-08-05

代码基线：`bd2e45c`；审查开始时工作区 clean，本轮仅新增 012 三份文档。

审查范围：`REPLAY-011.md`、011 修改涉及的 Workflow revision、Replay、TaskCheckpoint、Agent Host、
Artifact reconcile、公开 E2E 和 docs。Local Demo 不考虑鉴权。

## 1. 总结

本轮没有 P0。完整门禁和真实主链均通过；011 修复了此前报告中的逻辑 Workflow 身份、瞬态分类、
TaskCheckpoint fencing、action ledger 和 Artifact staging 问题。

仍发现 **2 个 P1、4 个 P2**。两个 P1 指向同一根因：系统保存了“修订过程证据”，但没有把这些
证据不可变地绑定到最终发布的定义。因此执行链可用，审计和发布门禁尚未闭合。

## 2. P1

### P1-1 — Replay 通过后的 draft 可再次修改，publish 仍接受旧 proof

[`routes.py`](../backend/app/api/routes.py) 的 Patch 路径在 replay 后把 draft 标为
`pending_revision`，但发布没有锁 replay 批准的 definition digest：

- `update_draft()` 可以替换 `draft_json`，只把 metadata status 改成 `manual_draft`；
- `apply_workflow_contract_patch()` 可以修改同一 `draft_json`，且不使 pending revision proof 失效；
- `publish_workflow()` 只在 status=`pending_revision` 时检查 base version 仍是最新版本；
- publish 不比较当前 draft digest 与 ReplayEvidence/patched replay digest。

`update_draft()` 还会保留 metadata 中旧 observation、fixture、ReplayEvidence 和 proposal digest；
definition 内原 `revisionLineage` 也不会移除。于是用户或编辑器在 replay 后修改任意 compile-valid
字段，仍可发布 v2，并让旧 proof 看起来像是新定义的证据。

这不是鉴权问题，而是证据因果关系错误：即使只有一个本地用户，正常的“Replay 后继续编辑再发布”
也会产生错误审计。

修复：ReplayEvidence 保存 `baseDefinitionDigest/patchDigest/patchedDefinitionDigest`；draft 保存
`replayApprovedDraftDigest`。所有 draft/contract edit 必须原子清除 approval 或标记
`dirty_after_replay`。publish 对 revision draft 强制比较当前 compiled digest，未匹配返回 409 并要求
重新 replay。

### P1-2 — Revision Agent proposal 没有不可变记录，`proposalDigest` 不能证明来源

`propose_failure_revision()` 调用 Agent 后只把 proposal 放在 HTTP response 中，没有数据库实体。
随后 `apply_version_workflow_patch()` 接受调用方提交的任意受白名单约束 Patch，并把该提交内容重新
计算成 `proposalDigest`。

因此 draft metadata 中的 `proposalDigest` 只能证明“Patch endpoint 收到了这些字节”，不能证明：

- 该 Patch 是哪个 Revision Agent run 生成的；
- 它是否就是 proposal API 返回的内容；
- 使用了哪个 prompt version、fixture set 和 baseline；
- 人工是否在应用前修改过 proposal。

同时 definition 的不可变 `revisionLineage` 只保存 observation、ReplayEvidence、fixture 和 base
version，没有 proposal id/digest。proposalDigest 只存在于可变 Workflow draft metadata；下一次修订
会覆盖它，历史 v2 Run 无法独立还原 proposal 来源。

修复：持久化 `RevisionProposal`，至少保存 observation id、base version id、Agent run id、prompt
version、fixture digest、proposal JSON/digest 和状态。Patch endpoint 接受 `proposalId + digest`；若
人工编辑则生成显式 `HumanPatch` 记录。发布后的 WorkflowVersion/Manifest lineage 保存 proposal 或
human patch id/digest。

## 3. P2

### P2-1 — 公开 handoff 黑盒没有证明“重启 Worker 后领取 pending checkpoint”

[`test_demo_e2e.py`](../backend/e2e/test_demo_e2e.py) 的
`test_agent_run_handoff_public_api_and_completed_action_ledger` 先等待 continuation `succeeded`，然后才
执行 `restart_worker(crash=True)`。新 Worker 只把 `continuing` checkpoint reconcile 为 succeeded，
并没有领取 pending checkpoint，也没有在重启后从 Artifact 创建 continuation。

UNKNOWN_OUTCOME 用例证明了重启 Worker 能恢复 running Agent，但没有已完成 ToolResult/action ledger。
两个测试仍未覆盖 NEXT_STEPS-011 要求的组合不变量。

修复：在 source failed、checkpoint pending 且 continuation 尚未创建时停止旧 Worker；新 Worker 必须
claim 新 generation、从持久 Artifact 创建 continuation，外部调用计数保持不变。

### P2-2 — “外部瞬态”公开对照实际是 Transform，不是 transport fixture

E2E-9 的瞬态部分使用 Transform `fail_dependency`，不是 Browser/MCP/HTTP Adapter 的真实连接失败或
超时。它证明 error code 被分类为 transient，但没有证明外部失败 fixture 会标记
`evidenceKind=transport_error/timeout`、不会靠 fixture baseline 获得 proposal。

单元测试同样使用 Transform seeded failure。实现静态上会 fail closed，但 REPLAY-011 把它列为公开
故障链证据高于实际覆盖。

修复：动态站点增加可控 503、断连和超时；通过真实 Browser/HTTP Adapter 产生 transport/timeout
fixture。即使两个独立 Run 达到 occurrence 阈值，proposal 仍必须 422，Agent 调用数为 0。

### P2-3 — Conversation Artifact reconcile promote 前不校验路径和 hash

[`orchestrator.py`](../backend/app/application/orchestrator.py) 的
`reconcile_conversation_artifacts()` 会把 `(root / storageKey).resolve()` 作为 target，但在
`target.is_file()`、`mkdir` 和 `source.replace(target)` 前没有验证 target 仍位于 root；promote staging
时也没有按 Artifact.sha256 校验内容。

正常生产者会生成安全 storageKey，但 reconcile 是崩溃恢复的最终完整性边界，不应信任数据库 metadata
或损坏 staging。当前损坏 staging 可被报告为 recovered，直到下载或 checkpoint load 才暴露 hash
不一致。

修复：target/source 均先做 `is_relative_to(root)`；promote 前校验 size/sha256，失败则隔离或删除 staging、
报告 `corrupt`，不得写入最终路径。

### P2-4 — publish 没有 draft revision/digest CAS，也不是幂等操作

`POST /workflows/{id}/publish` 没有 request body，无法表达用户实际审核的是哪个 draft revision/digest。
两个并发 publish 都可能读取同一个 max version 并尝试插入 N+1；唯一索引只会让其中一个以数据库异常
失败，而不是受控 409。对同一未变化 draft 连续点击 publish 还会产生内容相同的 v2/v3。

修复：publish 请求必须携带 `draftRevision` 和 `draftDigest`；用 Workflow revision/status CAS 领取发布，
重复请求返回同一 version 或受控 409。只有 draft 自上次 publish 后变更才生成新版本。

## 4. 已确认的有效修复

- Patch 在原 Workflow 上以 draft revision CAS 创建草稿，发布产生同源 v2。
- Workflow 与 Fragment 的旧 Manifest 都会变为 resolvable。
- FailureObservation occurrence 按不同 Run id 去重，evidence kind 和 reproduction policy 已持久化。
- transport/timeout 始终分类为 transient，不调用 Revision Agent。
- `ClaimedTaskCheckpoint` 携带 owner/generation/token；start 和 reconcile 使用 CAS。
- user answer 使用 status+snapshot digest CAS，只消费一次。
- Host action signature 覆盖 capability、参数 digest、effect 和 idempotency key；已完成只读结果从 ledger 返回。
- ToolResult 已改成 staging→DB 登记→commit→promote，并检测 duplicate call id hash 冲突。
- `continue_agent` 现在持久化有界 disclosure context，而不是只有 call/status。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| 后端 | 221 passed in 25.42s |
| 前端 | 28 passed；build 通过 |
| E2E | 21 passed in 33.15s |
| 对话总结固化二次复用专项 | 1 passed in 5.41s |
| handoff + unknown + Browser revision 专项 | 3 passed in 12.31s |
| `git diff --check` | 通过 |

修复顺序见 [`NEXT_STEPS-012.md`](../STEP/NEXT_STEPS-012.md)。
