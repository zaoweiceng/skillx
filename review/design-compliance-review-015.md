# 设计与规范达成度审查报告（015）

审查日期：2026-08-06

依据：[`REPLAY-014.md`](../REPLAY/REPLAY-014.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`c57de5c2a762e6f1ec5e0798181e840c7cb70245`（`feat: 实现通配型计划接力功能`）；
审查开始时工作区 clean，本轮只新增 015 三份审查文档。

范围：Local Demo；按约定不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

当前版本在真实 Local Demo 进程中可以完成三类失败接力：

```text
Browser → summarize
lookup → transform → output
file/read → transform

首次执行部分成功后失败
  → PlanSnapshot 与 ToolResult Artifact 落库
  → pending TaskCheckpoint
  → Worker 重启
  → completed action 命中 Host ledger
  → remaining steps 执行成功
```

Browser 场景还能继续完成：continuation 成功轨迹固化、人工发布、第二个新 Conversation 经 L0/L1
发现工作流、以新 URL/正文运行并返回新内容。全量门禁和三条独立专项均通过。因此 014 相比 013 的
进步是真实的：错误 reported/effective 已分离，checkpoint 不再只有 `resumeSameTask=true`，Artifact、
计划和 step 状态均有持久证据。

但当前仍不能把“冻结计划接力”判定为完整闭环。根因是：**PlanSnapshot 是提示词和审计事实，还不是
Host 强制执行的 source of truth。** TaskRecovery 把 remaining plan 放进 prompt，continuation 随后可
发送一个全新的 `agent.plan`；AgentHost 没有接收 checkpoint 的 expected plan digest/revision，也不校验
Tool 实参是否由 `argumentRefs` 解析而来。最小反例证明：计划声明读取 `safe.txt` 时，请求
`different.txt` 仍被接受；已经 completed 的同一 plan step 也可用新 callId 再执行。

此外，无结构化计划的 `replan_required` checkpoint 被创建为 pending，但用户回答 API 只接受
`awaiting_user`。Driver 领取后因缺少 userAnswer 抛错并留在 running，实际没有可达的人工确认路径。

所以 015 的准确判定是：

- **Host-owned error classification：完成。**
- **PlanSnapshot 持久化、完整性与断点可见性：完成。**
- **三类受控真实接力与 Browser 二次复用：可运行。**
- **冻结计划的 Host 强制执行：部分完成。**
- **无计划任务的受限 replan：未闭合。**
- **整体：operationally available，但 authoritative-plan closed-loop 仍为 partial。**

## 2. REPLAY-014 声明核验

| 014 声明 | 015 结论 | 证据或边界 |
|---|---|---|
| Agent retryable 不控制状态机 | 成立 | AgentHostError/AgentRunError 始终按共享 ErrorPolicy 生成 effective error |
| reported/effective error 分离 | 成立 | API、事件、ConversationRun 和 checkpoint 均可观察 |
| Plan capability/DAG/effect/budget 校验 | 基本成立 | 初始 plan 会校验；Tool 请求没有校验 action、argumentRefs 和 plan idempotencyKey |
| PlanSnapshot 不可变 Artifact | 成立 | schema/revision/digest/size/sha256 持久化并加载校验 |
| checkpoint 计算 completed/failed/remaining | 成立 | 由 plan step 与 Host ToolResult 生成，进入 source digest |
| 默认执行冻结 remaining DAG | 不成立 | remaining DAG 只进入 prompt；新 AgentHost 未绑定 source plan，可接受全新计划和不同参数 |
| completed 只能命中 ledger | 部分成立 | fixture 会重发 completed step 并命中 ledger；Host 不禁止同一 run 内重复 completed planStepId |
| typed ReplanPatch 约束 remaining-only | 部分成立 | 同一 AgentHost run 内有效；continuation 可通过发送全新 plan 绕过 base digest/revision |
| 旧运行进入 replan_required | 记录成立、流程不成立 | checkpoint 没有进入可回答状态，用户无法提供所需确认 |
| generation 总上限为 3 | 部分成立 | 同时混用 checkpoint claim_generation 与 source continuationGeneration；超限异常未原子置 terminal |
| reconcile 不删除活动 staging | 成立 | recent scan 只选择终态 Workflow/Conversation Run，交错测试存在 |
| proposal response 全来自持久记录 | 成立 | identity、proposal、digest 由 proposal_record 构造并重新编译 diagnostics |
| Browser/lookup/file 三类黑盒 | 成立 | 独立专项 3 passed；全量 E2E 24 passed |

## 3. 五阶段连续性

| 阶段 | 判定 | 当前能力 | 剩余边界 |
|---|---|---|---|
| 首次探索 | 完成 | 结构化 Agent plan、Host Tool Broker、逐字段 provenance、reported/effective error | 生产 TraeX 是否稳定输出结构化 plan 未验证 |
| 固化 | 完成 | Browser continuation 可 distill、人工 publish，输入继续参数化 | 仅成功轨迹固化，符合设计 |
| 渐进复用 | 完成 | 新 Conversation 通过 L0/L1 发现并运行 active version | Local Demo 使用轻量检索与兼容进程 |
| 失败接力 | 部分完成 | Plan/Artifact/checkpoint/ledger/Worker restart 均可运行 | Host 未把 checkpoint plan 作为执行约束；旧计划 replan 无入口 |
| 失败修订 | 完成 | Replay proof、审核 draft、发布 version 和 proposal lineage 保持闭合 | 本轮未发现新的发布证据断裂 |

## 4. 真实场景测试

### 4.1 环境

`make demo-check` 使用独立 API、Worker、SQLite、Alembic 数据库、动态 HTTP 服务、TraeX-compatible
子进程和真实 Playwright Chromium。每条 isolated E2E 使用独立临时目录与 Artifact root。Browser 第二次
复用不持有 Component/WorkflowVersion id，而是经公开 Catalog L0/L1 发现。

该环境验证真实进程、持久化、浏览器、Worker crash/restart 与 HTTP 故障注入；不等同于公网网站或生产
TraeX 模型质量验证。

### 4.2 三类专项观测

独立命令选择以下三条用例，结果为 `3 passed, 19 deselected in 20.20s`。

| 场景 | 已观察结果 | 真实性边界 |
|---|---|---|
| Browser → summarize | 真实 503、Host 两次尝试、pending checkpoint、新 Worker、浏览器恢复、summarize、distill/publish、第二 Conversation 新正文成功 | 起始模式仍由测试 directive 选择，但 Recovery 不再使用旧 Browser 专用 marker 分支 |
| lookup → transform → output | checkpoint 显示 completed lookup、remaining transform/output，continuation 成功 | 三个逻辑步骤实际都调用 `lookup.table`，transform/output 是 action 标签，不是独立生产转换器 |
| file/read → transform | `read.local` Artifact 命中 ledger，`summarize.text` 执行 remaining step，输出来自参数化文件内容 | 文件由兼容进程在隔离工作目录创建，未覆盖外部文件系统故障 |

这些测试足以证明同一恢复框架可承载多种只读 Adapter，但不能证明 Host 会拒绝与冻结计划不同的步骤或
参数；现有 fixture 恰好忠实复制了 prompt 中的 remaining plan。

### 4.3 计划约束反例

直接使用生产 `validate_agent_plan()` 和 `AgentHost._validate_planned_tool_request()`：

```text
Plan.argumentRefs.path = literal("safe.txt")
ToolRequest.arguments.path = "different.txt"
ToolRequest.action = "delete"

mismatched_arguments_accepted = true
completed_step_repeat_accepted = true
```

该反例不执行文件操作，但证明 Host 校验函数只检查 planStepId、capability 与依赖，没有执行参数绑定和
step 终态约束。

## 5. 全量门禁

| 检查 | 本次结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| Ruff | all checks passed |
| 后端 Pytest | 253 passed in 28.83s |
| Alembic | 空库 `0001→0017` 通过 |
| 前端 TypeScript / ESLint | 通过 |
| 前端 Vitest | 7 files / 28 tests passed |
| 前端 production build | 通过 |
| 全量黑盒 E2E | 24 passed in 72.16s |
| 三类通配接力独立专项 | 3 passed in 20.20s |
| Plan binding 反例 | mismatched arguments 与 completed step repeat 均被接受 |
| `git diff --check` | 写文档前通过 |

## 6. 下一版本完成定义

只有满足以下条件，才能把“冻结计划接力”标记为 closed-loop：

1. continuation AgentHostRequest 必须携带可信 expected PlanSnapshot digest/revision 与 remaining step set。
2. 默认 continuation 不能发送任意新 plan；改变步骤必须提交绑定 source digest 的 ReplanPatch。
3. Tool arguments 由 Host 根据 argumentRefs 和已验证输入/Artifact 解析，或逐字段比较后才执行。
4. 每个 plan step 有 Host-owned 状态机；成功 step 不能在同一 revision 内再次执行，retry 必须是显式 attempt。
5. `replan_required` 进入 `awaiting_user` 并提供一次性确认，或明确 terminal；不得 pending→running 后卡住。
6. continuation generation 上限使用单一链级计数，超限时原子转 terminal 并记录事件。
7. 新增恶意/偏离 Agent 黑盒：换 URL、换文件、跳步骤、重复步骤和绕过 ReplanPatch 全部 fail closed。

具体任务见 [`NEXT_STEPS-015.md`](../STEP/NEXT_STEPS-015.md)。
