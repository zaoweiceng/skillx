# 设计与规范达成度审查报告（014）

审查日期：2026-08-05

依据：[`REPLAY-013.md`](../REPLAY/REPLAY-013.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`4802aa8d1b14172e6ad34632d4c9bce5146fbd0e`（`feat: 闭合首次探索中的真实失败接力`）；
审查开始时工作区 clean，本轮新增 014 三份审查文档。

范围：Local Demo；按约定不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

当前版本在受控真实 Local Demo 中能够完成用户点名的完整场景：

```text
Conversation A 首次探索
  → Browser 真实访问外部测试服务并失败
  → Host 有限重试并保留 failed ToolResult
  → pending TaskCheckpoint
  → 旧 Worker 崩溃，新 Worker 领取 checkpoint
  → 外部服务恢复，continuation 续做并成功
  → 成功轨迹 distill 为草稿并人工发布
  → Conversation B 经 L0/L1 渐进发现
  → 用新输入重新运行固化 Workflow 并返回新内容
```

发布修订侧，REPLAY-012 要解决的因果链继续成立：RevisionProposal/HumanPatch、ReplayEvidence、
replay-approved draft、人工审核 revision/digest 和最终 WorkflowVersion 已由不可变 digest 与 CAS 绑定；
Replay 后编辑必须重新 Replay。

但“某个已写好的演示分支能跑通”和“通配型闭环已经完整实现”是两件事。当前仍有两个决定性缺口：

1. TaskCheckpoint 的 `remainingPlan` 固定为 `{"resumeSameTask": true}`，没有保存后续节点、依赖关系和
   预计输入。真实 E2E 中的 TraeX-compatible fixture 通过识别 `[[MODE:browser_failure_handoff]]`，从原始
   文本重新拼出 preflight/browser/summarize 流程。因此用例证明的是这一 fixture 分支可恢复，不是任意
   Browser、MCP、文件或其他操作都能按冻结计划从断点继续。
2. 无 failed ToolResult 时，Host 直接接受 Agent 自报的 `retryable`。Agent 可把 Host 策略规定为
   fail-closed 的 `PERMISSION_DENIED` 自报为可重试，触发 pending checkpoint 和自动接力，破坏“错误策略
   由 Host 拥有”的信任边界。

因此准确判定为：

- **REPLAY-012 发布证据链：完成。**
- **首次成功探索—固化—渐进复用：完成。**
- **受控 Browser 失败—Worker 接力—恢复—固化—第二次复用：可运行。**
- **跨操作类型的通配型失败接力：部分完成。**
- **整体闭环：operationally available，但 generalized closed-loop 仍为 partial。**

## 2. REPLAY-013 声明核验

| 能力声明 | 014 判定 | 证据或边界 |
|---|---|---|
| RevisionProposal/HumanPatch 不可变持久化 | 成立 | proposal、Agent run、prompt、fixture、人工 Patch 均落库 |
| ReplayEvidence 锁 base/patch/patched digests | 成立 | publish 重新计算 Compiler digest 并强制匹配 proof |
| Replay 后编辑使 proof stale | 成立 | `dirty_after_replay`、旧快照与旧 Evidence 均结构化拒绝 |
| publish 绑定审核快照且幂等 | 成立 | revision/digest/status CAS 与唯一约束均有测试 |
| WorkflowVersion/Manifest 可追溯 lineage | 基本成立 | 正常路径成立；并发 proposal 响应仍可能混用不同 AgentRun lineage |
| failed ToolResult 不被协议错误覆盖 | 成立 | 非法 completed、子进程退出均保留 Tool 主错误，协议错误只作附加审计 |
| transient 有限重试后进入 checkpoint | 成立 | `DEPENDENCY_UNAVAILABLE/TIMEOUT/RATE_LIMITED` 由共享 policy 给出预算 |
| 新 Worker 从失败 Artifact 接力 | 成立 | 真实 Browser 失败场景 generation 增长、failed Artifact 校验、恢复成功 |
| continuation 不重做已完成 action | 成立 | completed-action ledger 在 Host 层复用 preflight ToolResult |
| continuation 获得具体剩余计划 | 不成立 | checkpoint 只保存 `resumeSameTask=true`；fixture 从 marker 重建步骤 |
| 错误可重试性只由 Host 决定 | 不成立 | 独立 `agent.failed` 的 payload 可覆盖 Host policy |
| Artifact root/size/hash 完整性 | 基本成立 | final/staging 校验已统一；reconcile 仍可能清理活动 Run 的 staging |

## 3. 通配型五阶段连续性

| 阶段 | 判定 | 当前已实现 | 剩余边界 |
|---|---|---|---|
| 首次探索 | 完成 | Host Tool Broker 执行真实 Browser；成功输出逐字段绑定 ToolResult；失败输出保留标准错误 | 使用仓库内 TraeX-compatible 进程，不代表生产 TraeX 的规划质量 |
| 固化 | 完成 | continuation 成功轨迹可 distill；草稿可编辑且不自动发布 | 只固化成功完成后的轨迹，符合设计 |
| 渐进复用 | 完成 | 新 Conversation 通过 L0/L1 发现并运行已发布能力；不把完整目录塞进上下文 | Local Demo 使用轻量检索 |
| 失败接力 | 部分完成 | failed/completed ToolResult、checkpoint、generation/token、Artifact 校验和 no-redo 均存在 | 未冻结实际剩余节点；Agent 可自报 retryable 绕过 Host policy |
| 失败修订 | 完成 | 真实契约故障、proposal、Replay、同源 v2、旧版可解析和 active v2 搜索均成立 | 并发 proposal 的 HTTP 响应 lineage 需收紧 |

## 4. 真实场景测试

### 4.1 环境真实性边界

Harness 为每次运行创建独立临时目录、SQLite 数据库和 Artifact root，通过 Alembic `0001→0017`
建库，并启动独立 API、Worker、动态 HTTP 服务和 TraeX-compatible 子进程。网页由真实 Playwright
Chromium 读取；第二个对话不持有 Component/WorkflowVersion id，而是经公开 Catalog 的 L0/L1 发现。

这属于真实进程、真实数据库、真实浏览器和真实网络故障注入，但不是公网网站，也不是生产 TraeX
服务。它足以验证平台编排与持久化，不足以证明任意模型都会生成正确的剩余计划。

### 4.2 目标链路观测

专项：`test_real_tool_failure_handoff_distill_and_second_reuse`。

| 步骤 | 观测结果 |
|---|---|
| 外部服务返回 503 | Browser Tool 真实调用；Host 共尝试 2 次；主错误为 `DEPENDENCY_UNAVAILABLE` |
| 首次 Run 失败 | ConversationRun 保持 failed；TaskCheckpoint 为 pending；failed action/Artifact 可读取 |
| 旧 Worker 崩溃 | 新 Worker 领取 generation 1；源 Run 不被改写 |
| 外部服务恢复 | continuation 只新增失败 Browser 调用；preflight 命中 ledger，不增加外部调用 |
| continuation 完成 | 返回恢复后的动态正文；checkpoint 进入 succeeded；lineage 指向 source |
| distill/publish | continuation 轨迹生成草稿并按审核快照发布 |
| Conversation B | 经 L0/L1 找到首次 lineage；以新输入运行 active version并返回新正文 |

专项与另外两条关键真实 Browser 场景在沙箱外独立运行：`3 passed in 35.79s`。首次沙箱内执行的
`3 failed` 已定位为 macOS 拒绝 Chromium MachPort 注册，日志为 `Permission denied (1100)`，服务访问
计数为 0；在允许 Chromium 启动后同一代码和用例全部通过，故不归类为代码失败。

## 5. 全量门禁

| 检查 | 本次结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| Ruff | all checks passed |
| 后端 Pytest | 230 passed in 26.59s |
| Alembic | 空库 `0001→0017` 通过 |
| 前端 TypeScript / ESLint | 通过 |
| 前端 Vitest | 7 files / 28 tests passed |
| 前端 production build | 通过 |
| 全量黑盒 E2E | 22 passed in 56.99s；另一次独立运行 22 passed in 57.86s |
| 三条关键 Browser 专项 | 3 passed in 35.79s |
| Host policy 反例 | `PERMISSION_DENIED` policy=false，但显式输入后 accepted_retryable=true |
| `git diff --check` | 审查写文档前通过 |

## 6. 下一版本完成定义

只有同时满足以下条件，才可把通配型闭环标记为 `closed-loop`：

1. `agent.failed` 的 code/retryable 由 Host 校验并重新分类；Permission/Safety/Contract/非法事件不能由
   Agent 改成可重试。
2. checkpoint 保存结构化 PlanSnapshot，明确已完成、失败、待执行节点、依赖、输入引用和副作用；不能
   只保存“继续同一任务”。
3. continuation 的行为由 checkpoint 数据驱动，而不是由测试 marker 或特定网页分支重建。
4. 至少增加 Browser、MCP/lookup、文件/转换三种不同流程的失败接力用例，并证明同一恢复框架工作。
5. reconcile 不得删除仍被活动 NodeRun/Worker 使用的 staging。
6. 并发 proposal 的响应必须完整来自同一持久化 RevisionProposal。

具体任务见 [`NEXT_STEPS-014.md`](../STEP/NEXT_STEPS-014.md)。
