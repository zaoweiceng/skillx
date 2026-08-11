# REPLAY-011：修订身份、接力 fencing 与失败判定闭环

日期：2026-08-05

依据：[`NEXT_STEPS-011.md`](../STEP/NEXT_STEPS-011.md)、
[`review/design-compliance-review-011.md`](../review/design-compliance-review-011.md)、
[`review/code-review-011.md`](../review/code-review-011.md)。

执行基线：`24cca38`。本报告对应当前未提交工作树；本轮未擅自创建 Git 提交。

范围：Local Demo；不包含鉴权/RBAC、多租户和分布式部署。

## 1. 结论

011 的三个核心不变量已经由后端实现和公开黑盒共同闭合：

```text
WorkflowPatch 保留原 Workflow.id，人工发布形成 v2
TaskCheckpoint claim 的 owner/generation/token 约束 continuation 与终态
契约证据与 transport/timeout 历史错误使用不同复现策略
```

主复用链仍通过；外部 Browser 失败修订链现在从公开 API 连续完成
observation → Revision Agent proposal → Patch → ReplayEvidence → 原 Workflow draft
→ publish v2 → Catalog 发现 → 原/新输入运行。AgentRun handoff 通过公开 API、
Worker 重启、持久 Artifact 和 Host action ledger 完成。

## 2. A：Workflow 修订逻辑身份

| 条目 | 实际结果 |
|---|---|
| A-01 同一 Workflow 草稿 | Patch 从 `WorkflowVersion.workflow_id` 解析逻辑身份，不再插入新 Workflow |
| base/draft fencing | 只允许最新版本且当前草稿等于 base definition；`draft_revision` CAS 冲突返回 409 |
| 草稿 metadata | 持久化 base version、observation、fixture、ReplayEvidence、proposal digest |
| 发布版本 | publish 使用同一 Workflow 的 `max(version)+1`；发布后草稿规范化为版本 definition |
| API | `draftWorkflowId == workflow.id`，返回 `expectedNextVersion` |
| A-02 active/resolvable | publish v2 后 v1 Workflow/Fragment Manifest 变为 `resolvable` |
| 搜索与精确解析 | search 只返回 v2；新增 exact resolve API 解析 v1；v1 仍可显式运行 |

连续测试
`test_external_browser_failure_fixture_patch_publish_and_reuse`
从 Patch 入口发布 v2，并断言同一 Workflow id、v1 resolvable、search 仅 v2。
`test_search_returns_only_active_version_but_old_ref_resolves`
同时验证旧 Fragment 为 resolvable。

## 3. B：失败证据与资格

迁移 `0016_revision_identity_and_evidence.py` 新增：

- `ReplayFixture.evidence_kind`；
- `FailureObservation.evidence_kind`；
- `reproduction_policy_json`；
- 去重后的 `evidence_ids_json`。

证据策略：

| evidenceKind | baseline 语义 | Revision Agent |
|---|---|---|
| `observed_contract_output` | 冻结 observed output 对 base/patched 契约重校验 | 两个独立 Run 且 baseline 同错误后允许 proposal |
| `deterministic_error` | 隔离执行复现 | 两个独立 Run 且 baseline 成立后允许 proposal |
| `transport_error` | 历史错误仅诊断 | 禁止 proposal，不得改 URL、网络权限或 Secret |
| `timeout` | 历史错误仅诊断 | 禁止 proposal，不得改 URL、网络权限或 Secret |

`observation_eligibility()` 同时检查 classification、evidence kind、
baseline 和 occurrence。fingerprint 排除 input digest，保留 execution source/contract
等稳定摘要，因此不同输入的同一契约漂移可正确聚合，重复登记同一 Run 不会增加 occurrence。

公开 E2E 先产生一次 `DEPENDENCY_UNAVAILABLE` 并确认 proposal 返回 422，随后用两个独立
Playwright Run 产生同 fingerprint 的 `CONTRACT_VIOLATION`，Revision Agent 才被调用。

## 4. C：TaskCheckpoint fencing 与 action ledger

`ClaimedTaskCheckpoint` 是不可变 handle，字段为 `id/owner/generation/token`。

- `claim()` 以 CAS 递增 generation 并返回 handle；
- `start_continuation(handle)` 以单条 UPDATE 完成 `running→continuing`；
- 只有 CAS 成功事务才保留 continuation、lineage 和 started 事件；
- reconcile 以 status/generation/token/continuation id 快照约束终态；
- user answer 以 status + snapshot digest CAS，只消费一次；
- 双 Session 测试证明同名 owner 超租约重领后，迟到 handle 无法创建 continuation。

checkpoint 的 completed-action ledger 保存 capability ref、参数签名、effect、
idempotency key 和结果 Artifact digest。Host 在 Broker 调用前计算同一规范签名：

- `effect=none` 且 Artifact 可用：返回保存结果，标记 `reusedFromLedger=true`；
- 已完成结果不可复用：拒绝重做；
- 副作用结果未知：保持 `awaiting_user`，不得由回答改写历史结果。

`test_completed_action_ledger_reuses_result_without_external_call`
使用计数 Adapter，断言外部调用次数为 0。

## 5. D：公开黑盒

### D-01 AgentRun handoff

`test_agent_run_handoff_public_api_and_completed_action_ledger` 只使用公开
Conversation/AgentRun/TaskCheckpoint API：

1. Agent 完成随机只读 lookup 后注入可恢复失败；
2. checkpoint 引用持久 ToolResult Artifact；
3. continuation 故意请求同一 lookup，Host 从 ledger 返回保存结果；
4. continuation 成功后 crash/restart Worker，并由持久状态完成 reconcile；
5. 原 ConversationRun 保持 failed，continuation 保存显式 lineage。

`test_agent_run_unknown_outcome_answer_consumed_once` 通过公开 API：

1. crash 正在运行的 Agent；
2. 新 Worker 将其恢复为 `UNKNOWN_OUTCOME/awaiting_user`；
3. GET checkpoint 后回答；
4. 第二次相同回答返回 409；
5. continuation 从确认后的剩余任务完成。

### D-02 Browser 修订与复用

`test_e2e_9_reproducible_failure_gated_patch` 只使用 HTTP API、独立 Worker、
Alembic 临时数据库和真实 Playwright：

1. 创建并发布 Browser v1；
2. 两个独立 Run 产生真实 observed contract failure；
3. observation API 调用 Revision Agent，测试不手写最终 Patch；
4. 白名单应用 proposal，baseline 同错误、patched replay 成功；
5. 原 Workflow 形成 draft，人工发布 v2；
6. Catalog 对原输入 A、新输入 B 分别发现并运行 v2；
7. search 仅返回 v2，exact resolve 返回 v1；
8. revision lineage 校验 observation、fixture、proposal digest、ReplayEvidence、
   base v1、v2 和 Run。

## 6. E：Artifact 与 continuation 上下文

ToolResult 使用 staging → owner CAS/DB 登记 → commit → atomic promote。Artifact metadata
保存 `storageKey/stagingKey/actionSignature/resultDigest`。重复 call id 的 hash 一致时幂等，
不一致时报 `EVIDENCE_CONFLICT`。

Worker startup/periodic reconcile 同时处理成功、失败、取消的 ConversationRun：

- DB 已登记但未 promote：从 staging 补落位；
- DB 未登记的 staging：删除；
- 最终目录无 DB 引用文件：删除；
- `_load_completed_outputs()` 拒绝 run artifact root 外路径。

`continue_agent` 持久化有界 disclosure context：候选 ref、L0/L1 synopsis、排除条件、
level、result digest 和剩余预算，不复制完整 Manifest/DAG。测试断言 continuation
直接使用该上下文且不重新搜索。

## 7. F：规范与门禁

[`docs/demo-acceptance.md`](../docs/demo-acceptance.md) 已增加：

- 修订必须保持同一 Workflow 逻辑身份；
- transport/timeout 与 contract evidence 的不同 reproduction policy；
- TaskCheckpoint handle fencing、user answer CAS 和 action ledger 失败判定；
- E2E-9/E2E-9b 同源连续链。

实际命令：

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `make demo-check` | 0 | 全量门禁通过 |
| `cd backend && uv run pytest -q` | 0 | 221 passed |
| `cd backend && uv run pytest e2e -q -p no:cacheprovider` | 0 | 21 passed |
| D-01 handoff 专项 | 0 | 1 passed in 4.84s |
| D-01 unknown 专项 | 0 | 1 passed in 6.75s |
| D-02 Browser revision 专项 | 0 | 1 passed in 7.10s |
| 空库 `alembic upgrade head` | 0 | `0001→0016` |
| 前端 TypeScript/ESLint/Vitest/build | 0 | 28 passed，production build 通过 |
| `git diff --check` | 0 | 无空白错误 |

## 8. 最终门禁矩阵

| 门禁 | 结果 |
|---|---|
| Main reuse regression | 通过 |
| Logical version identity | 通过，同一 Workflow v1/v2 |
| Transient policy | 通过，历史 transport/timeout fixture 不可获得 proposal |
| Contract reproduction | 通过，observed output 稳定重校验 |
| Revision Agent continuity | 通过，同一 Browser observation 驱动 proposal/Patch/v2 |
| Task fencing | 通过，迟到 handle 无法启动第二 continuation |
| No-redo | 通过，Host ledger 复用 Artifact，计数 Adapter 为 0 |
| Public handoff | 通过，API + Worker restart + 持久 DB |
| Revised reuse | 通过，v2 原/新输入成功，Catalog 仅 active v2 |
| Lineage | 通过，持久 id/digest 可追溯 |
| Quality | `make demo-check` 一次全绿 |

011 完成后，Local Demo 范围内的主复用、失败接力和受控修订链可标记为
`closed-loop`。该结论不扩展到鉴权、多租户、分布式 lease 或生产 Artifact 存储。
