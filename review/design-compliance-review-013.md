# 设计与规范达成度审查报告（013）

审查日期：2026-08-05

依据：[`REPLAY-012.md`](../REPLAY/REPLAY-012.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`093ab312f05e75d37655c0e3b8b763c8befb825c`；审查开始时工作区 clean，本轮只新增 013 三份文档。

范围：Local Demo；按约定不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

012 要解决的核心问题已经关闭：**Replay 通过的定义、人工审核的草稿快照和最终发布的
WorkflowVersion 已由 digest/revision/CAS 连成同一条不可变证据链。** Replay 后再编辑会使 proof
失效；重新 Replay 后才能发布同一逻辑 Workflow 的 v2。RevisionProposal、HumanPatch、
ReplayEvidence 和发布版本 lineage 均可追溯。

真实“首次总结→固化→第二个新对话复用”也可以完成：首次 Browser ToolResult 来自 Host，成功轨迹
生成草稿，人工发布后，第二个 Conversation 通过 L0/L1 渐进披露选择已发布能力，用新 URL 读取新正文
并返回总结。独立专项及连续三次重复均通过。

但从最初需求的通配型闭环看，当前只能判定为：

- **成功路径的首次探索—固化—新对话复用：完成。**
- **已固化 Workflow 的失败观察—受控修订—Replay—同源 v2：完成。**
- **已知 AgentRun 失败的 checkpoint—Worker 重启—Artifact 接力：完成。**
- **首次探索中的真实外部 Tool 失败—重试耗尽—上下文接力：未闭合。**

额外真实运行中出现过一次 Browser Tool 失败。Host 已持有原始 failed ToolResult，但
TraeX-compatible 进程随后发送缺少 `outputProvenance` 的 `agent.completed`，最终错误被覆盖为
`AGENT_EVENT_INVALID`；TaskCheckpoint 状态为 `terminal`，无法交给新 Agent 从失败点继续。这正是用户
要求覆盖的“网络波动且重试无法恢复后，把上下文和剩余节点交给 TraeX”边界。

因此 013 的最终判断是：**012 发布证据链可标记为 closed；通配型全流程仍为 partial，不应宣称完整
closed-loop。**

## 2. REPLAY-012 声明核验

| 012 声明 | 013 结论 | 证据或边界 |
|---|---|---|
| RevisionProposal/HumanPatch 持久化 | 成立 | proposal id/digest、Agent run、prompt、fixture、人工 Patch 均落库 |
| ReplayEvidence 锁 base/patch/patched digests | 成立 | 三个 digest 与 proposal/HumanPatch id 均持久化，publish 强制复核 |
| Replay 后编辑使旧 proof 失效 | 成立 | draft 进入 `dirty_after_replay`；旧快照与旧 proof 分别返回结构化 409 |
| 重新 Replay 后才能发布 | 成立 | 生成新 Evidence，`replayApprovedDraftDigest` 与当前 Compiler digest 一致后发布成功 |
| publish 绑定 revision/digest 且幂等 | 成立 | CAS、唯一索引和并发测试均通过；同一快照只生成一个版本 |
| WorkflowVersion/Manifest 保存修订 lineage | 成立 | v2 Run detail 和 L1 Manifest 均可读取 revision lineage |
| pending TaskCheckpoint 由新 Worker 接力 | 成立 | 新 Worker claim generation=1，continuation 复用 action ledger，源 Run 保持 failed |
| 真实 transport/timeout 不触发 Revision Agent | 基本成立 | 503、断连、延迟均走真实 Playwright；proposal 返回 422；但 timeout 分类存在竞态 |
| Conversation Artifact reconcile 校验 root/hash | 成立 | 越界、损坏 staging、损坏 final 均有专项覆盖 |
| 整体 Local Demo closed-loop | 部分成立 | 发布修订闭环已关闭；首次探索 Tool 失败无法可靠进入可接力 checkpoint |

## 3. 通配型五阶段连续性

| 阶段 | 判定 | 当前能力 | 剩余缺口 |
|---|---|---|---|
| 首次探索 | 基本完成 | Host Tool Broker 真实执行 Browser；输出逐字段绑定 ToolResult | Tool 失败后的原始错误会被 Agent 协议错误覆盖 |
| 固化 | 完成 | Distillation Agent 生成结构化草稿；人工发布；输入参数化 | 失败中的探索不能在接力完成后继续固化 |
| 渐进复用 | 完成 | 新 Conversation 经 L0/L1 搜索、描述、选择并运行；不把全部 Workflow 塞入上下文 | Local Demo 仍使用轻量语义检索和仓库内 TraeX-compatible 进程 |
| 失败接力 | 部分完成 | 人工构造的已知失败可冻结 checkpoint、跨 Worker 复用 Artifact 且不重做 action | Browser/MCP 等真实 failed ToolResult 没有统一进入 pending checkpoint |
| 失败修订 | 完成 | 外部契约失败可产生 proposal、ReplayEvidence、同源 v2，并保留旧版本可解析性 | timeout 与 Playwright transport 的边界分类偶发不稳定 |

## 4. 真实环境模拟：首次总结、固化与第二次复用

### 4.1 环境

Harness 每次创建独立临时目录、SQLite 数据库和 Artifact root，只通过 Alembic `0001→0017` 建库；
启动独立 API、Worker、动态网页服务和 TraeX-compatible 子进程。网页访问使用真实 Playwright，第二次
对话不持有 WorkflowVersion id，而是经公开 Catalog 工具完成发现和执行。

正式专项命令：

```bash
cd backend
.venv/bin/pytest \
  e2e/test_demo_e2e.py::test_e2e_8b_real_browser_distillation_to_non_fake_agent \
  -q -p no:cacheprovider
```

结果：`1 passed in 6.25s`；随后独立连续重复三次，结果为 `3/3 passed`（5.94s、4.76s、4.83s）。

### 4.2 模拟对话与系统行为

| 步骤 | 对话/系统动作 | 结果 |
|---|---|---|
| 1 | Conversation A 请求访问文章并总结 | AgentRun succeeded；Host 产生 `tool.requested/tool.result` |
| 2 | 首次网页正文为“探索阶段正文” | 输出精确等于真实 Browser ToolResult；content hash 与 evidence digest 有效 |
| 3 | 对成功 ConversationRun 发起 distill | proposalSource 为 Distillation Agent；轨迹、proposal digest 均有效 |
| 4 | 人工按 draft revision/digest 发布 | 生成不可变 WorkflowVersion 与 L0/L1 Manifest |
| 5 | 网页改为“第二次运行的新正文：利润增长 21%。”，URL 改为 `?version=b` | 没有复用首次正文缓存 |
| 6 | Conversation B 输入“访问并总结这个网页” | action=`run_full`；通过 L0/L1 发现刚发布版本 |
| 7 | 检查 L1 lineage | `sourceConversationRunId` 指向 Conversation A |
| 8 | 独立 Worker 运行固化 Workflow | Run succeeded；输出包含“利润增长 21%”；Browser 与 Agent 节点均真实执行 |

这证明成功主链可用，也证明工作流不是只针对固定网页结果：URL 是运行输入，首次轨迹被编译为 Browser
与 Agent 节点，第二次重新取数并总结。

## 5. 012 修订与接力专项

故障修订专项独立通过，并额外连续重复三次全部通过。它证明：

1. 真实 Playwright 的 503、断连和延迟错误不会调用 Revision Agent；
2. 两个独立契约失败 Run 达到阈值后生成不可变 proposal；
3. Replay-approved 草稿再次编辑后，旧 snapshot/proof 均被拒绝；
4. 重新 Replay 产生新 Evidence，发布同源 v2；
5. Catalog 只搜索到 active v2，v1 精确 ref 仍可解析；
6. v2 对原输入和新输入都成功。

pending TaskCheckpoint 专项也通过：旧 Worker 在 continuation 尚未创建时崩溃，新 Worker 领取
generation=1，从持久 ToolResult Artifact 创建 continuation；重复 lookup 命中 Host ledger，外部 action
没有重做。

## 6. 额外失败注入揭示的未闭环边界

一次额外主场景运行在首次 Browser 调用阶段失败。保留数据库显示：

```text
tool.result.status = failed
agent.run.error.code = AGENT_EVENT_INVALID
agent.run.error.message = outputProvenance 必须恰好覆盖所有输出字段
task_checkpoint.status = terminal
```

因果链是：

```text
真实 Tool 失败
  → Host 返回结构化 failed ToolResult
  → Agent fixture 仍发送 agent.completed，但没有失败输出的合法 provenance
  → Host 用协议错误覆盖原 Tool 错误
  → AgentRun 只把 TIMEOUT/EXECUTION_FAILED 视为可接力
  → terminal checkpoint，无 continuation
```

这不是“成功率不够”的测试噪声，而是错误语义没有贯穿 Host、AgentRun 和 TaskCheckpoint。只要任一真实
Browser/MCP/HTTP Tool 在首次探索中失败，就可能落入同一路径。

另一次故障专项运行中，外层节点超时与 Playwright 导航超时都设为 1 秒，竞态使延迟请求被记录为
`transport_error` 而不是 `timeout`。两者都正确保持 transient 且禁止 proposal，所以安全决策没有反转，
但证据分类和独立验收不稳定。

## 7. 门禁记录

| 检查 | 本次结果 |
|---|---|
| `make demo-check` | 通过 |
| Ruff | all checks passed |
| 后端 Pytest | 224 passed in 25.63s |
| Alembic | 空库 `0001→0017` 通过 |
| TypeScript / ESLint | 通过 |
| 前端 Vitest | 7 files / 28 tests passed |
| 前端 production build | 通过 |
| 黑盒 E2E | 21 passed in 37.88s |
| 主复用独立专项 | 1 passed in 6.25s；额外连续 3/3 passed |
| Browser revision 独立专项 | 1 passed in 13.98s；额外连续 3/3 passed |
| pending checkpoint restart | 通过 |
| `git diff --check` | 通过 |

完整门禁全绿不抵消额外故障注入暴露的问题：现有 E2E 只验证 Browser 成功和人工构造的 Agent 失败，
没有把“真实 Tool 失败→AgentRun 接力”连成同一条黑盒。

## 8. 013 完成定义

只有满足以下条件，通配型闭环才能从 `partial` 升级为 `closed-loop`：

1. Agent 协议能表达 Tool 失败终态，或由 Host 直接以原始结构化错误结束，不允许被
   `AGENT_EVENT_INVALID` 覆盖。
2. 重试策略和 TaskCheckpoint 使用同一错误分类；可恢复的 Browser/MCP/HTTP transient 在重试耗尽后
   生成 `pending` checkpoint。
3. failed ToolResult 和已完成 action 以 Artifact/ledger 进入 continuation；新 Agent 从剩余计划继续，
   不重复已完成调用。
4. 增加单条公开黑盒：首次探索 Browser 503/断连→重试耗尽→旧 Worker 崩溃→新 Worker 接力→服务恢复
   →完成总结→固化→第二个新对话复用。
5. Browser 内层 timeout 与节点外层 timeout 有确定优先级，重复运行不会在 `timeout` 与
   `transport_error` 间漂移。
6. Workflow Run Artifact reconcile 与 Conversation Artifact 使用相同的 root、size、sha256 校验。

具体任务见 [`NEXT_STEPS-013.md`](../STEP/NEXT_STEPS-013.md)。
