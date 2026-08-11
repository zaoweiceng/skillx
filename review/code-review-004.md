# 代码审查报告（004）

审查日期：2026-08-03  
审查范围：提交 `274dc68` 之上的 REPLAY-003 修复代码、迁移、前端和测试。  
约束：单用户 Local Demo，不审查鉴权/RBAC；Tool 权限、幂等性、版本锁定和副作用安全仍在范围内。

## 1. 结论

完整门禁通过，本轮实质关闭了 003 的 Browser 黑盒真实性、candidate 命名和 ContractPatch URL 边界问题。新增代码结构清晰，AgentRunService 与 MCP 分类也有针对性单测。

本轮发现 **4 个 P1 和 4 个 P2**。最高风险集中在两个不变量被破坏：长任务必须先持久化再执行；发布版本引用的能力快照必须不可变。当前测试都在快速成功路径完成后检查结果，因此没有覆盖这两个边界。

## 2. P1 问题

### P1-1 — Agent Run 在单个 API 事务内同步执行，运行中事实不可见且进程中断会丢失

证据：路由先 `await agent_run_service.start_agent_run(...)`，Agent 终态后才添加消息并 `commit`，[`routes.py`](../backend/app/api/routes.py#L1394)。Service 创建 run/event 只调用 `flush`，随后在同一 session 中遍历最长 120 秒的子进程事件，[`agent_run.py`](../backend/app/application/agent_run.py#L110)。前端只有等 POST 返回后才拿到 `conversationRunId` 并开始轮询，[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx#L120)。

影响：

- 运行期间其它请求看不到未提交的 run/event，右侧抽屉不是实时轨迹；
- API 超时、重启或连接取消会回滚整次运行，连失败事实也不保留；
- 没有 `queued/running` handle、取消 API、租约或恢复所有者；
- 页面刷新时组件重新创建 Conversation，[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx#L33)，也无法恢复原会话。

现有测试为什么没发现：E2E-5 等 POST 直接返回 succeeded 后才 GET events；前端 mock 同样让 `startAgentRun` 立即成功。

建议：POST 只在短事务内创建 `queued` ConversationRun、created event 和关联消息后提交并返回 202；独立 Worker 通过租约执行 TraeX，逐批提交事件与心跳；增加 cancel endpoint、启动时恢复 stale lease、基于路由或持久化选择恢复 conversation。

### P1-2 — Agent 安全限制由请求方定义，Host 没有独立执行权限/步数/Token 边界

证据：API 允许调用方提交任意 `allowedCapabilities`，并直接写入 config，[`routes.py`](../backend/app/api/routes.py#L1410)。Service 将同一集合传给 TraeX，再仅检查返回的 Tool 是否属于这份调用方集合，[`agent_run.py`](../backend/app/application/agent_run.py#L146)。代码累计 `step_count` 和 `total_tokens`，但从未与 `max_steps`/`max_total_tokens` 比较，[`agent_run.py`](../backend/app/application/agent_run.py#L163)。

同一信任边界还有一个失败路径：Adapter 对任意合法 JSON 都直接 yield；`_normalize()` 假设 raw 是 dict，Token 字段也直接 `int()`。非对象 JSON或非法数字会抛出未捕获的 `AttributeError/ValueError`，路由返回 500 并回滚审计记录。

影响：请求者可扩大 Tool 集合；不遵守 CLI 参数的 Runtime 可超过步数/Token 后仍被记为成功；畸形子进程事件能绕过结构化失败收尾。即使不考虑鉴权，这也违反 Host 与 Agent 之间的能力隔离契约。

建议：配置 Host 允许能力集合，实际能力取 `requested ∩ host_granted`；Host 每收到 step/Token 事件都独立检查限额并终止超限进程；对 raw event、字段类型、终态唯一性做 Pydantic/JSON Schema 校验；捕获所有适配器边界异常并持久化 `AGENT_EVENT_INVALID`。

### P1-3 — MCP 分类原地修改不可变 ToolSnapshot，已发布 Workflow 的锁定语义可被事后改变

证据：`classify_snapshot()` 直接修改同一行的 `side_effect`、`idempotent`、审核状态和 `digest`，[`mcp.py`](../backend/app/application/mcp.py#L348)。WorkflowVersion 发布时保存的是当时的 Snapshot id + digest，[`routes.py`](../backend/app/api/routes.py#L359)；运行时 `resolve_mcp_node()` 只按 Snapshot id 读取当前行并把当前 digest 交给 Executor，没有核对版本中保存的旧 digest，[`mcp.py`](../backend/app/application/mcp.py#L427)。

影响：一个已发布为“只读、幂等、可重试”的工作流，其 Snapshot 可随后被改成“写、非幂等”；Workflow 定义中的 `sideEffect/idempotent/retry` 不会同步变化，历史版本继续运行时可能重复写操作。所谓不可变版本和契约锁定因此失效。

附带问题：discovery 将 annotation 来源直接标记为 `reviewed`，[`mcp.py`](../backend/app/application/mcp.py#L247)，与“annotations 只作为提示、本地 Registry 显式确认”的计划不一致。

建议：禁止 UPDATE 已存在的 ToolSnapshot；人工分类创建新 Snapshot（或新 ServerVersion）并保留旧行。Workflow 运行前必须比较锁定的 snapshot id + digest，任何不一致 fail closed。annotation 只能生成 `suggestedEffect/suggestedIdempotent`，审核状态保持 unreviewed，直到本地 classify 明确确认。

### P1-4 — Distillation 没有提炼真实轨迹，只把原任务重新包装成单个 Agent 节点

证据：AgentRunService 虽然收集 `normalized_events` 和 `tool_events`，但 `_build_trajectory()` 不消费这些动作，固定生成一个 `kind="agent"` step，并把原始 prompt 原样写入节点配置，[`agent_run.py`](../backend/app/application/agent_run.py#L157)、[`agent_run.py`](../backend/app/application/agent_run.py#L265)。E2E-5 的原 prompt 内含 `[[REPLY:季度复盘要点]]`，复用不同输入后仍断言同一个固定 reply，[`test_demo_e2e.py`](../backend/e2e/test_demo_e2e.py#L102)。

影响：当前闭环证明“第一次运行 Agent 后，发布一个再次运行同一 Agent 的组件”，没有证明 Tool 调用被锁定、参数被识别、确定步骤被编译，或模型成本下降。它与设计文档“不是把 Agent 对话画成流程图，而是编译为固定输入、确定节点、明确权限的工作流”的核心目标仍有差距。

建议：持久化带 call id、能力版本、输入模板、输出摘要的规范化 action；Distillation 依次尝试删除、参数化、转 Transform、锁定 MCP/SkillX、单次 LLM，最后才保留 Agent。E2E fixture 应根据新输入产生不同结果，并断言固化后确定节点/模型调用数/Tool lock，而不是依赖原 prompt 中的固定 reply 指令。

## 3. P2 问题

### P2-1 — E2E 断言失败时仍删除现场，不会打印声明的日志

fixture 的 `finally` 无条件调用 `stop_stack(stack)`，[`test_demo_e2e.py`](../backend/e2e/test_demo_e2e.py#L18)；只有 `start_stack()` 自身启动失败才传 `failed=True`，[`harness.py`](../backend/e2e/harness.py#L179)。因此测试正文断言失败时会删除临时目录，不打印 API/Worker/form 日志，REPLAY 的 B-03 声明仍不成立。

建议：通过 `pytest_runtest_makereport`/fixture request 获取 call 阶段结果，失败时调用 `stop_stack(failed=True)`；同时保存并关闭父进程日志文件句柄。

### P2-2 — VariablePicker 生成后端不支持的数组表达式，类型兼容提示也未接入 UI

前端对数组生成 `${node.output.tags[]}`，[`variablePicker.ts`](../frontend/src/lib/variablePicker.ts#L41)；后端 Compiler/Expression 的路径正则只允许 `[\w.-]+`，不接受 `[]`，[`expressions.py`](../backend/app/application/expressions.py#L7)。此外 `typeCompatibility()` 只被单测调用，Inspector 仍只展示 code chip，没有目标字段和类型参数，[`WorkflowEditorPage.tsx`](../frontend/src/pages/WorkflowEditorPage.tsx#L197)。

影响：数组 chip 若被复制到 JSON 会被拒绝或按普通文本处理；UI 实际没有 REPLAY 所称的兼容/不兼容提示。

建议：先统一表达式规范（例如只提供整个数组 `${node.output.tags}`，或正式支持索引/通配符语法），再由后端解析器与前端 Picker 共用契约测试；把 Picker 接到具体 mapping 字段并展示 source/target type。

### P2-3 — Artifact 最终文件仍早于数据库 commit 落位

CAS 后先 `os.replace()` 到最终目录，再新增 Artifact/Event，外层事务之后才提交，[`orchestrator.py`](../backend/app/application/orchestrator.py#L816)。若 append/flush/commit 失败，数据库回滚但文件已离开 staging，仍会成为孤儿。REPLAY 已明确把此项留到后续，本轮没有回归恶化，但仍是代码一致性问题。

建议：使用 outbox/待 finalize 状态，在数据库提交后落位并二次确认；或建立可恢复 journal，同时定期双向扫描“DB 无文件/文件无 DB”。

### P2-4 — MCP Tool test 展示的自动重试语义与运行时不一致

Tool test 仅在 `idempotent && effect == none` 时返回 `autoRetryAllowed=true`，[`routes.py`](../backend/app/api/routes.py#L1051)；运行时只禁止“有副作用且非幂等”的重试，因此显式幂等写 Tool 可以重试，[`orchestrator.py`](../backend/app/application/orchestrator.py#L889)。

影响：Registry/Test 页面可能告诉用户“不可自动重试”，而同一个锁定 Tool 所在 Workflow 实际会重试，降低审计可信度。

建议：抽出唯一 `retry_policy(effect, idempotent, outcome)`，发布校验、Tool test 和 Orchestrator 共用；对 read/write/destructive × idempotent × timeout/unknown outcome 做表驱动测试。

## 4. 仍缺的高价值测试

1. Agent POST 立即返回 202；另一个连接在运行中能看到 committed events；API/Worker 重启后 run 恢复。
2. 用户取消 Agent、Agent 超 step/Token、Runtime 输出非对象 JSON/非法 Token 时均形成持久化终态。
3. 请求 Tool 集合大于 Host grant 时只得到交集，越权 Tool fail closed。
4. 已发布 Workflow 后重新分类 MCP Tool，不得改变旧 Snapshot；运行时 digest 不一致必须拒绝。
5. Agent 轨迹中一个确定 Tool 被提炼为锁定 MCP/Transform 节点，新输入改变输出且 Agent 调用数下降。
6. E2E 正文断言失败时日志被打印、临时目录被保留。
7. 前后端共享的变量表达式测试覆盖对象、数组、索引和非法路径。
8. Artifact promote 后数据库 commit 失败的恢复/GC。

## 5. 已执行验证

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff | 通过 |
| 后端测试 | 85 passed |
| 空库 Alembic | 升级至 0007 通过 |
| 前端 check/lint/test/build | 通过，13 passed |
| 黑盒 E2E | 7 passed |
| `git diff --check` | 通过 |

门禁绿色证明当前快速成功路径稳定，不覆盖 P1 所述的中断、并发、不可变性与真正提炼语义。

