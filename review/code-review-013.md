# 代码审查报告（013）

审查日期：2026-08-05

代码基线：`093ab312f05e75d37655c0e3b8b763c8befb825c`；审查开始时工作区 clean，本轮只新增 013 三份文档。

审查范围：`REPLAY-012.md`、迁移 0017、Workflow revision/publish、Agent Host、TaskCheckpoint、
Artifact reconcile、Playwright 运行时、公开 E2E 和前端调用。Local Demo 不考虑鉴权。

## 1. 总结

012 的两个 P1 和四个 P2 均已按目标主路径修复：Replay proof 绑定最终 definition digest，proposal 与
人工 Patch 可追溯，publish 具备审核快照 CAS/幂等，pending checkpoint 可由重启后的新 Worker 领取，
真实 transport/timeout 不会触发 Revision Agent，Conversation Artifact reconcile 会校验路径和 hash。

本轮没有 P0，发现 **1 个 P1、2 个 P2**。P1 不在发布链，而在更上游的首次探索失败语义：Host 已经
获得真实 failed ToolResult，却可能把它变成不可恢复的 Agent 协议错误，因而未满足通配型失败接力需求。

## 2. P1

### P1-1 — failed ToolResult 被 `AGENT_EVENT_INVALID` 覆盖，真实探索失败无法进入接力

[`agent_host.py`](../backend/app/application/agent_host.py) 会把 Tool Broker 的失败作为
`tool.result(status=failed, error=...)` 回送给 Agent，但最终输出 provenance 校验只接受
`status=succeeded` 的 ToolResult。仓库内 TraeX-compatible 进程在 Browser 失败分支仍发送
`agent.completed`，且没有 `outputProvenance`，于是 Host 抛出：

```text
AGENT_EVENT_INVALID: outputProvenance 必须恰好覆盖所有输出字段
```

原始 Browser/MCP 错误因此丢失。随后 [`agent_run.py`](../backend/app/application/agent_run.py) 只把
`TIMEOUT` 和 `EXECUTION_FAILED` 创建为 `pending` TaskCheckpoint，其余错误均为 `terminal`。额外真实
运行已经复现：事件中 `tool.result.status=failed`，最终 ConversationRun 却是
`AGENT_EVENT_INVALID`，checkpoint 为 terminal，无 continuation。

影响：用户要求的“网络波动且重试无法恢复后，把上下文和之后节点交给 TraeX 接着做”在首次探索阶段
不成立。Host 也无法向操作者展示原始依赖错误，诊断被次生协议错误污染。

修复不应给 fixture 单独补一个伪 provenance。应定义统一失败终态：

- Agent 协议增加受 Host 校验的 `agent.failed`，或 Host 在存在 failed broker result 且无合法成功输出时
  直接传播该结构化错误；
- retryable 判定由共享 error policy 决定，至少覆盖可恢复的 `DEPENDENCY_UNAVAILABLE/TIMEOUT`；
- 重试耗尽后保存 failed ToolResult、已完成 action ledger 和 remaining plan，创建 pending checkpoint；
- continuation 只重试失败/剩余步骤，已完成 action 仍由 Host ledger 阻止重做。

## 3. P2

### P2-1 — Workflow Run Artifact reconcile 仍只检查“文件名存在”，没有复用 012 的完整性边界

012 收紧的是 `reconcile_conversation_artifacts()`；
[`reconcile_run_artifacts()`](../backend/app/application/orchestrator.py) 仍通过 `list_final_files()` 判断
storage key 是否存在：

- final 文件存在即视为正常，不比较 `Artifact.size/sha256`；
- staging promote 继续调用较弱的 `promote_staged_artifact()`，target 已存在时直接跳过；
- source staging path 没有在 reconcile 层独立验证位于该 Run 的 `.staging` root；
- 报告只有 recovered/missing/orphan，不能区分 corrupt/pathRejected。

下载接口会在最后一步返回 409，但损坏文件不会被 reconcile 修复；ReplayFixture 读取也可能因此失去
可用证据。两个 Artifact 生命周期实现不同，增加后续修复遗漏概率。

修复：抽取一个共享 reconcile/promote 实现，Workflow Run 与 ConversationRun 都先验证 root，再校验
size/sha256；损坏 final 可由有效 staging 修复，否则报告 corrupt；越界 metadata 报 pathRejected。

### P2-2 — Browser 内外层 timeout 同值导致 evidence kind 有竞态，独立 E2E 偶发不稳定

E2E timeout 节点把 `timeoutSeconds=1`，BrowserReadContract 同时设置 `timeoutMs=1000`。
[`orchestrator.py`](../backend/app/application/orchestrator.py) 的外层 `asyncio.wait(..., timeout=1)` 与
Playwright `page.goto(timeout=1000)` 同时到期：

- 外层先到期时得到 `TIMEOUT`；
- Playwright/Context 关闭异常先返回时，Browser Executor 把它映射为 `DEPENDENCY_UNAVAILABLE`。

一次独立故障专项因此没有生成 `evidenceKind=timeout`，而是落入 transport 类；随后同一专项连续三次
通过。两种分类都保持 transient，未错误触发 Revision Agent，但验收和指标不确定。

修复：明确 timeout 层级，推荐 Browser 导航 timeout 小于节点总 timeout，并单独捕获 Playwright
TimeoutError 映射为 `TIMEOUT`；取消/关闭引发的 TargetClosed 不应反向覆盖已经确定的超时原因。

## 4. 已确认的有效修复

- 迁移 0017 新增 RevisionProposal/HumanPatch 以及 Replay/发布 digest 字段，空库升级通过。
- proposal API 持久化 Agent run、prompt、fixture、baseline 和 proposal digest。
- Patch API 校验 proposal id/digest；人工编辑生成独立 HumanPatch。
- ReplayEvidence 锁 base definition、Patch 和 patched definition digest。
- draft/contract edit 改变 Compiler digest 后进入 `dirty_after_replay`，旧 proof 不再可发布。
- publish 请求携带 draft revision/digest/evidence id，并以状态 CAS 领取；重复/并发请求幂等。
- WorkflowVersion 与 Manifest 都保存 revision lineage。
- TaskCheckpoint start/reconcile 使用 owner/generation/token；重启后的新 Worker 可领取 pending checkpoint。
- Host completed-action ledger 能返回持久 ToolResult，避免 continuation 重做相同只读 action。
- 真实 Playwright 的 transport/timeout 历史证据不会获得 Revision Agent proposal。
- Conversation Artifact reconcile 会拒绝越界路径和损坏 staging/final。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| 后端 | 224 passed in 25.63s |
| Alembic | `0001→0017` 通过 |
| 前端 | 28 passed；TypeScript/ESLint/build 通过 |
| 黑盒 E2E | 21 passed in 37.88s |
| 主复用独立专项 | 1 passed in 6.25s；额外连续 3/3 passed |
| Browser revision 独立专项 | 1 passed in 13.98s；额外连续 3/3 passed |
| pending checkpoint restart | 通过 |
| 额外探索期 Browser 故障 | 复现 failed ToolResult 被改写为 `AGENT_EVENT_INVALID`，checkpoint terminal |
| `git diff --check` | 通过 |

修复顺序与验收标准见 [`NEXT_STEPS-013.md`](../STEP/NEXT_STEPS-013.md)。
