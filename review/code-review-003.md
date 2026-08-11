# 代码审查报告（003）

审查日期：2026-08-03  
审查范围：提交 `274dc68` 相对 002 审查基线的 47 个变更文件、测试和文档。  
约束：单用户 Local Demo，不审查鉴权/RBAC。

## 1. 结论

本轮代码质量显著提升，002 的 5 个 P1 均已实质处理，完整门禁通过。本轮发现 **4 个 P1 产品/安全语义问题和 5 个 P2 工程问题**。最核心的问题不是测试缺少，而是测试标题与实际执行路径不一致，造成 README 和 `make demo-check` 的完成度判断偏高。

## 2. P1 问题

### P1-1 — 没有代码路径产生真实 Agent ConversationRun，固化主链不可达

证据：路由注释两次声明真实 Agent 应由 `/conversations/{id}/agent-runs` 创建，[`routes.py`](../backend/app/api/routes.py#L1175)、[`routes.py`](../backend/app/api/routes.py#L1319)，但项目中没有该路由。当前 ConversationRun 只由单次 LLM 路径创建为 `llm_response`，或由导入接口创建为 `trajectory_import`。AgentExecutor 只产生 RunEvent 和 Artifact，[`executors.py`](../backend/app/runtime/executors.py#L399)，不会创建 ConversationRun。

影响：用户无法从通用对话启动一次真实 TraeX 探索，更无法把该次运行事实送入 Distillation。设计的核心价值“探索一次，固化复用”在生产路径上不可达。

现有测试为什么没发现：固化发布复用测试直接在数据库插入 `run_type="agent_run"` 的 ConversationRun，[`test_distillation.py`](../backend/tests/test_distillation.py#L126)，绕过了缺失的生产者。

建议：抽出 AgentRunService，由对话 API 启动受限 TraeX、持久化状态/事件/轨迹并在成功时创建 ConversationRun；Distillation 只接受该服务签发的成功记录。删除指向不存在路由的注释并补真实 API E2E。

### P1-2 — E2E-5/6 没有执行标题描述的路径，`demo-check` 会产生假阳性

证据：E2E-5 只运行 seed 中的“受限 Agent 摘要”组件并检查 `agent.completed`，[`test_demo_e2e.py`](../backend/e2e/test_demo_e2e.py#L102)；该组件使用 `agentProfile="fake"`，没有 ConversationRun、Distillation、发布和新输入复用。E2E-6 向 Browser 节点直接传 `observedPage`，并依赖工作流中的 `fakeSubmitResult`，[`test_demo_e2e.py`](../backend/e2e/test_demo_e2e.py#L111)，没有启动 Playwright 或本地页面。

影响：两个最能证明产品价值与副作用安全的场景未被黑盒门禁覆盖，但 README 声称 `make demo-check` 成功等价于六条验收成功，[`README.md`](../README.md#L103)。后续回归可能破坏 Agent 固化或真实 Browser，而门禁仍保持绿色。

建议：E2E-5 必须从真实 Agent API 开始并完成草稿发布复用；E2E-6 在 harness 中启动本地表单 HTTP 服务，Workflow 使用 `runtime=playwright`，审批后断言服务器收到且只收到一次提交。

### P1-3 — MCP Tool 一律被标记为无副作用且幂等

证据：Tool discovery 创建快照时固定 `side_effect="none"`、`idempotent=True`，[`mcp.py`](../backend/app/application/mcp.py#L218)。Adapter 的 `ToolDescriptor` 也没有承载 annotations/effect 元数据。发布校验随后相信这两个值决定副作用声明和自动重试安全性。

影响：发送消息、写工单、删除记录等写 Tool 会被当作读操作；用户无需显式声明 sideEffect，失败时还可能自动重试，违反文档对副作用和幂等性的核心约束。

根因：为了让天气 happy path 快速通过，把“未知”默认成了“安全”，而不是要求 Registry 明确分类。

建议：读取 MCP annotations 只能作为提示，Registry 仍需显式审核/覆盖 `effect + idempotent`；未知 Tool 默认 `effect=unknown`、`idempotent=false`，发布含该 Tool 的 Workflow 时阻止自动重试并要求确认。

### P1-4 — “继续通用 Agent”按钮只修改本地 UI，没有启动 Agent

证据：候选卡片的按钮只把 `dismissed[message.id]` 设为 true，[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx#L92)；没有 API 调用。对应测试只断言卡片变成“已选择继续通用 Agent”且 confirm 未调用，[`ChatPage.test.tsx`](../frontend/src/pages/ChatPage.test.tsx#L86)。后端未匹配路径也只调用单次 LLM，不运行 TraeX。

影响：用户选择了明确动作，却只看到卡片隐藏，任务没有继续执行。这是用户可见的假操作，也是 P1-1 在前端的表现。

建议：按钮调用真实 Agent Run endpoint，返回 ConversationRun id；前端订阅状态/事件，失败时显示可恢复错误，不得只保存瞬时本地状态。

## 3. P2 问题

### P2-1 — Candidate token 被称为“一次性”，实际可无限复用

`candidate_token()` 只是对公开的 component/version/schema 做无密钥 hash，[`conversation.py`](../backend/app/application/conversation.py#L34)。只要默认版本不变，同一 token 可重复调用 confirm 创建多个 Run。它能检测 stale version，但不是授权或一次性令牌。建议改名为 `candidateSnapshotDigest`；若确需一次性，持久化 nonce 并在 confirm 时原子消费。

### P2-2 — VariablePicker 对 Input 节点生成错误引用

无 outputSchema 时统一回退为 `${nodeId.output}`，[`variablePicker.ts`](../frontend/src/lib/variablePicker.ts#L30)，测试甚至把 `${input.output}` 固化为期望值，[`variablePicker.test.ts`](../frontend/src/lib/variablePicker.test.ts#L39)。运行时 Input 引用语义是 `${input.<inputSchema字段>}`，因此当前提示会生成无法解析的路径。应对 Input 使用 workflow inputSchema，对其他节点才使用 outputSchema，并增加嵌套 Schema 覆盖。

### P2-3 — Artifact 文件落位仍早于数据库事务提交

CAS 成功后在 session transaction 内先执行 `os.replace()`，再新增 Artifact 行，[`orchestrator.py`](../backend/app/application/orchestrator.py#L816)。若后续 append event、flush 或 commit 失败，数据库回滚但最终文件已经移出 staging，仍会成为孤儿。建议使用提交后 finalize/outbox，或保留可恢复 journal 并定期 GC 最终目录中无数据库记录的文件。

### P2-4 — E2E harness 的失败日志与资源管理不符合注释

fixture 正常 finally 总是调用 `stop_stack(stack)`，测试断言失败时不会传 `failed=True`，所以 harness 不会按注释打印 API/Worker 日志。[`harness.py`](../backend/e2e/harness.py#L143) 也不会删除成功运行的临时目录；端口固定为 8801，[`test_demo_e2e.py`](../backend/e2e/test_demo_e2e.py#L18)，并行或端口占用时会无关失败。建议动态端口、pytest hook 感知失败、成功清理/失败保留。

### P2-5 — ContractPatch 的强约束函数没有自行校验 URL

`build_contract_patch()` 的文档声明 URL 不得变化，但函数只检查 security warning、identity 和 business object，[`contract_patch.py`](../backend/app/runtime/contract_patch.py#L38)。Executor 当前先做 L4 分类，因此主路径暂时安全；但该函数作为独立安全边界可被其他调用者误用。建议内部再次调用 `url_matches()`，并增加错误 URL 的直接单元测试。

## 4. 仍缺的高价值测试

1. 真实对话 Agent API 创建 ConversationRun，并通过 Distillation 发布复用。
2. 独立 API/Worker + 本地表单服务器 + Playwright 的真实提交 E2E，验证只提交一次。
3. MCP 写 Tool 未分类时禁止发布；显式非幂等 Tool 不自动重试。
4. “继续通用 Agent”前端动作真的发请求并显示轨迹。
5. VariablePicker 的 Input、嵌套对象、数组及节点输出 Schema。
6. Artifact 文件落位后数据库 commit 失败的恢复/GC。

## 5. 已执行验证

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff | 通过 |
| 后端测试 | 74 passed |
| 空库 Alembic | 升级至 0006 通过 |
| 前端 check/lint/build | 通过 |
| 前端测试 | 10 passed |
| 黑盒测试目录 | 6 passed |

门禁绿色证明当前测试覆盖的行为稳定，不证明 P1-1/P1-2 所述的缺失路径已经存在。

