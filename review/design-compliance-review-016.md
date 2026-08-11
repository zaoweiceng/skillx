# 设计与规范达成度审查报告（016）

审查日期：2026-08-06

依据：[`REPLAY-015.md`](../REPLAY/REPLAY-015.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`c57de5c2a762e6f1ec5e0798181e840c7cb70245`；015 实现位于当前未提交工作树。本轮没有修改业务代码，
只新增 016 三份审查文档。范围为单用户 Local Demo，不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

**本轮不能直接判定通过。**

015 的核心后端目标已经成立：冻结 PlanSnapshot 能成为 continuation Host 的执行事实；真实 Browser 503
失败后，新 Worker 能从持久 checkpoint 继续失败 step 和后续 step，不重跑已成功 step。通过公开 API
启动正确能力锁的首次探索后，也能在真实 UI 中固化、静态检查、人工发布，并由第二个新 Conversation
经 L0/L1 发现刚发布的 lineage，读取变化后的网页正文并成功完成 Workflow Runtime。

但用户可见的默认主路径仍未闭合：

```text
自然语言对话
  → continue_agent 自动创建 AgentRun
  → 默认只授权 search.web
  → browser.read 被 Host 拒绝
  → 消息又缺少 runType，页面看不到失败/固化入口

即使绕过入口并成功发布、复用
  → Workflow Run 后端成功并得到新摘要
  → 对话消息不回填结果
  → 点击“查看运行”触发 React 无限更新，页面空白
```

因此准确判定为：

- 015 Host authoritative-plan 修复：**通过**。
- 真实失败接力：**通过**。
- 显式 API 探索 → UI 固化/发布 → 新对话复用的后端链：**通过**。
- 默认自然对话首次探索：**不通过**。
- 第二次复用结果返回用户：**不通过**。
- 运行详情可观察性：**不通过**。
- 通配型完整用户闭环：**partial，不可标记为 available/通过**。

## 2. REPLAY-015 声明核验

| 015 声明 | 016 结论 | 本轮证据或边界 |
|---|---|---|
| ExpectedPlanContract 绑定 source plan | 成立 | continuation 输入含 source digest/revision/checkpoint/remaining/state digest；真实接力接受原计划 |
| Host 物化并核对参数 | 成立 | Browser continuation 的 URL/pageContract 与冻结 argumentRefs 一致；既有反向 E2E 全绿 |
| succeeded step 不重做 | 成立 | 真实 503 接力中 `preflight` 只执行一次，continuation 从 `browser` attempt 2 开始 |
| retry/attempt 状态机 | 基本成立 | checkpoint 保存 step 状态和 attempts；但源 attempt 的 `argumentsDigest` 实际为 `null` |
| ReplanPatch 绑定 source | 成立但预算不完整 | digest/revision/state/checkpoint 校验存在；`replanBudget` 只保存未强制消费 |
| legacy 一次性回答 | 成立 | 公开 answer CAS 与重复回答 409 已纳入 33 条 E2E |
| claim 与 continuation generation 分离 | 成立 | 真实接力显示 claimGeneration=1，continuation 输入 generation=1 |
| Browser/lookup/file 正反黑盒 | 成立 | 全量 33 条 E2E 通过 |
| 整体能力可标记 available | 不成立 | 默认 UI 入口、结果返回和运行详情存在主路径阻塞 |

## 3. 真实环境测试

### 3.1 环境

本轮不是新增单元测试模拟。实际启动了以下独立进程：

- FastAPI：`127.0.0.1:8000`；
- Worker：独立 `python -m app.worker` 进程；Browser 场景使用可启动真实 Chromium 的本地进程权限；
- Vite 前端：`127.0.0.1:5173`；
- 动态本地 HTTP 站点：`127.0.0.1:8975`；
- TraeX-compatible 子进程：仓库内确定性 Demo runtime；
- SQLite：全新临时库，只通过 Alembic `0001→0017` 创建，再执行 seed；
- 浏览器：从实际前端页面输入、点击固化、静态检查、发布和第二次对话复用。

这证明本地部署、进程边界、持久化、真实 HTTP/Chromium 和 UI 集成；不代表公网网络或生产模型质量。

### 3.2 默认自然对话入口：失败

在 `/chat` 输入：

```text
请探索并总结 http://127.0.0.1:8975/article
```

并让 WorkflowSelection 返回 `continue_agent`。API 确实创建了 ConversationRun，但配置来自
`AgentRunConfig.default()`，只含 `search.web`。TraeX 请求 `browser.read` 后，Host 以
`PERMISSION_DENIED` 终止。

同一 assistant message 的后端内容含 `type=agent_run` 和 `conversationRunId`，却不含前端筛选所需的
`runType=agent_run`。页面只显示“已由通用 Agent 继续处理”，没有失败状态、轨迹或固化入口。

### 3.3 显式能力的首次探索与固化：后端/UI 通过

通过公开 `POST /conversations/{id}/agent-runs` 显式传入
`browser.read + summarize.text` 后：

- ConversationRun `ce906a80-a3a5-4773-8f80-398fe55b8b68` succeeded；
- Host Browser Tool Broker 读取真实正文“016探索阶段正文：季度营收增长16%。”；
- Browser evidence 记录 `resultSource=host_tool_broker` 和正文 sha256；
- UI 正确展示 Agent 事件和“固化为组件”；
- UI 固化得到 workflow draft `9e387c40-b0bd-4b85-afaf-260a7e90a7a7`；
- UI 静态检查通过并人工发布不可变 v1，创建组件入口。

这说明探索、轨迹、distillation、草稿和人工发布本身可用；问题在默认入口能力配置，不在 Browser
或 distillation 主体。

### 3.4 第二次新对话复用：后端通过，用户结果返回失败

将网页改为“016第二次复用的新正文：利润增长26%。”后，在新 `/chat` 输入新 URL。系统经 L0/L1
选择刚发布的 Workflow，`agentAction=run_full`，Run
`e46dec6c-708d-4b77-8e25-2c07da9e80f8` succeeded：

- lineage.sourceConversationRunId 指向首次成功探索 `ce906a80-...`；
- Browser 节点重新读取带新 query 的 URL；
- Agent 节点消费新正文；
- 最终 output 为 `{"reply":"016第二次复用的新正文：利润增长26%。"}`。

但是对话页完成后仍只显示“选择已执行，后续由 Workflow Runtime 接管”和“查看运行”，不显示 status 或
output。点击“查看运行”后 `RunDetailPage` 抛 `Maximum update depth exceeded` 并呈现空白页。因此规范中
“通用对话完成任务”以及需求中的“最后将总结内容返回给用户”没有完成。

### 3.5 真实 Browser 503 失败接力：通过

运行 `planned_browser_summary`：

1. `preflight` 成功；
2. Browser 访问真实 `/transient?mode=503`，Host 按策略共访问 2 次后失败；
3. source run `fbe7652f-...` 生成 pending checkpoint `0ced6c93-...`；
4. checkpoint 保存 plan digest/revision、`preflight=succeeded`、`browser=failed`、`summarize=remaining`；
5. 停止源 Worker，将站点切换 healthy，再启动新 Worker；
6. continuation `da18d4d1-...` 只执行 `browser` attempt 2 和 `summarize`；
7. `/__stats.transientCount` 从 2 变为 3，证明没有重跑 preflight 或额外重复 Browser；
8. continuation succeeded，输出“恢复后的正文”，源 checkpoint 随之 succeeded。

该观测直接支持 015 的核心修复结论。

## 4. 五阶段连续性

| 阶段 | 判定 | 说明 |
|---|---|---|
| 首次探索 | 部分完成 | 显式 API 能成功；默认自然对话 `continue_agent` 权限不足且 UI 不显示终态 |
| 固化与人工发布 | 完成 | 成功 AgentRun 可在 UI 固化、检查、发布 |
| 渐进发现与复用 | 后端完成 | 新 Conversation L0/L1 命中首次 lineage，Workflow 使用新输入成功 |
| 失败接力 | 完成 | 真实 503、持久 checkpoint、Worker 重启、remaining-only continuation 跑通 |
| 结果反馈与观测 | 未完成 | 对话不显示 Workflow output；运行详情页崩溃 |

## 5. 全量门禁

| 检查 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| Ruff | all checks passed |
| 后端 Pytest | 254 passed in 27.33s |
| Alembic | 空库 `0001→0017` 通过 |
| 前端 TypeScript / ESLint | 通过 |
| 前端 Vitest | 7 files / 28 tests passed |
| 前端 production build | 通过 |
| 进程级 E2E | 33 passed in 124.83s |
| 真实 UI 首次自然探索 | failed：`PERMISSION_DENIED browser.read`，页面未显示失败 |
| 真实 UI 固化/发布 | passed |
| 真实新对话复用 | 后端 passed；对话结果展示 failed；运行详情 failed |
| 真实 503 + Worker 重启接力 | passed |
| `git diff --check` | 通过 |

## 6. 016 完成定义

只有同时满足以下条件才可判定完整通过：

1. `/chat` 的自然 `continue_agent` 能执行 Browser/lookup/file 等受控只读探索，不依赖调用方手工传能力。
2. 自动创建的 Agent 消息可在刷新后显示真实状态、轨迹、错误和固化入口。
3. Workflow Run 完成后，其 status/output 在所属对话中可持久恢复并直接返回用户。
4. `/runs/:id` 在空事件 store、运行中和终态三种状态下均不崩溃。
5. replan revision budget 由 Host 强制，不只是 prompt/checkpoint 元数据。
6. checkpoint 每个 attempt 均记录非空 arguments digest。
7. 上述四条用户主路径必须以实际 API + Worker + 前端 + 浏览器验收，而非仅靠 mock/Vitest。

具体修复任务见 [`NEXT_STEPS-016.md`](../STEP/NEXT_STEPS-016.md)。
