# 代码审查报告（002）

审查日期：2026-08-03  
范围：当前后端、前端、迁移、测试和一键验收入口。  
约束：单用户 Local Demo，不把缺少鉴权作为本轮问题。

## 1. 结论

现有质量门禁全部通过，001 版最危险的租约、取消、版本锁定、路径逃逸和浏览器身份检查问题多数已被修复。本轮仍发现 **5 个 P1 正确性/验收问题和 6 个 P2 工程问题**。

最高优先级是 Browser L2：用户批准页面可解释变化后，代码会把 Browser 节点直接标为成功；对于本应提交的节点，实际提交不会发生。这会产生“运行成功但业务动作未执行”的假成功。

## 2. P1 问题

### P1-1 — Browser L2 审批会跳过重放和实际提交

证据：Browser Executor 在判断 `submit` 之前先对 L2 返回 `waiting_user`，审批快照的 effect 固定为 `browser.read`，[`executors.py`](../backend/app/runtime/executors.py#L595)。审批接口只有 `effect == form.submit` 才把节点放回 `ready`；其他审批一律把节点直接设为 `succeeded`，[`routes.py`](../backend/app/api/routes.py#L565)。

影响：提交型 Browser 节点遇到新增可选字段或 locator 变化时，用户点击“确认继续”后，节点和 Run 可能成功，但没有重新打开页面、重新计算授权摘要或点击提交。下游收到的还是通用 `{approved, snapshot}`，不是 Browser 契约输出。

根因：L2“允许继续执行”和普通 Approval“批准即完成”共用了同一决策分支，没有把 Browser 审批建模为可恢复执行状态。

建议：Browser L2 审批必须写入一次性 drift authorization，并将同一 NodeRun 放回 `ready`；重放时重新观察页面，确认 L2 差异和表单/身份未变化，然后继续提交审批或只读输出。为只读与提交型 Browser 各补一个 L2 测试。

### P1-2 — 对话候选在前端不可确认、不可补参，E2E-4 无法从 UI 完成

证据：后端只有请求带 `componentId` 和 `inputs` 才能启动组件，[`routes.py`](../backend/app/api/routes.py#L955)。前端 `sendMessage` 只发送 `{content}`，[`client.ts`](../frontend/src/api/client.ts#L200)；候选卡片只显示名称和缺失字段，没有按钮或表单，[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx#L116)。

影响：用户看到“发现匹配组件，请确认并补充参数后启动”，但没有任何方式完成确认。现有测试通过直接调用 Python 路由函数并构造 `MessageCreate(componentId, inputs)` 绕过了真实前端/API 使用路径。

根因：后端候选协议和前端交互状态没有共同定义“candidate → collecting_inputs → confirmed → run”的状态机。

建议：候选消息返回锁定的组件版本和 inputSchema；前端渲染 Schema 表单并显式确认；API 增加独立 confirm endpoint 或让 `sendMessage` 完整支持 `componentId + inputs`；用浏览器测试证明 Run 链接最终出现。

### P1-3 — “通用 Agent”实际是静态匹配或单次 LLM，轨迹不是 Agent 运行事实

证据：未绑定对话先用组件名称是否为输入子串做匹配，[`routes.py`](../backend/app/api/routes.py#L1000)；未匹配时直接调用一次 `OpenAICompatibleAdapter.generate()`，[`routes.py`](../backend/app/api/routes.py#L1035)，随后人为构造一个只含 LLM step 的 `ConversationRun`，却向前端标为 `agent_run`，[`routes.py`](../backend/app/api/routes.py#L1046)。另一个 `/agent-runs` 接口接受调用方提供的任意轨迹并直接标为 succeeded，[`routes.py`](../backend/app/api/routes.py#L1102)。

影响：UI 和 README 会让用户以为任务经过 Agent 探索和 Tool 轨迹采集，实际没有 TraeX、计划、Tool 事件或运行时约束。基于该记录生成草稿，不能证明设计目标中的“从成功 Agent 事实固化”。

根因：为了快速接通固化 UI，把“回答消息”“Agent 执行”“轨迹导入”合并成同一种记录。

建议：区分 `llm_response`、`agent_run` 和 `trajectory_import`；通用未知任务必须走 Agent Runtime 并由服务端从事件流生成不可伪造的 ConversationRun；组件匹配至少通过结构化意图结果，不应依赖名称子串。

### P1-4 — MCP Tool 重发现会静默忽略 Schema 变化

证据：`discover_tools()` 以 `(server_version_id, tool_name)` 查找快照；一旦存在就直接返回旧记录，不比较新发现契约的 digest，[`mcp.py`](../backend/app/application/mcp.py#L163)。

影响：同一 MCP ServerVersion 背后的服务若改变 Tool 输入/输出 Schema，健康检查和 discover 仍展示旧快照。工作流看似锁定契约，实际调用的 Tool 行为已经变化，破坏历史可复现性和兼容性扫描承诺。

根因：Server 配置版本与远端 Tool 契约版本被视为同一件事，但远端服务可以在配置不变时升级。

建议：每次发现计算契约 digest；与已存在快照不同时必须失败并要求注册新 ServerVersion，或生成新快照并执行依赖兼容性扫描，不能静默复用旧快照。补“同名 Tool Schema 改变”测试。

### P1-5 — `maxParallelNodes` 不是原子配额，并发 Worker 可能同时越过上限

证据：Worker 先查询当前 `running` 数量，再对某个候选 NodeRun 做条件 UPDATE，[`orchestrator.py`](../backend/app/application/orchestrator.py#L534)。这两个动作没有 Run 级锁或数据库配额令牌。现有 `test_max_parallel_nodes_limits_claims` 是顺序调用 claim，不覆盖两个事务同时读取相同 active count。

影响：并行分支在两个 Worker 同时领取时可能超过 DSL 上限；SQLite 下也可能表现为锁错误而不是稳定限流。上限若用于限制外部服务并发或浏览器资源，配置语义不可靠。

根因：节点所有权 CAS 只能阻止同一节点被重复领取，不能约束不同节点共享的 Run 级配额。

建议：为 Run 增加原子 running slot 计数或调度令牌；领取与 slot 增量在一个条件 UPDATE 中完成，提交/恢复时释放。补真正并发的双 session/双 Worker 测试。

## 3. P2 问题

### P2-1 — MCP Streamable HTTP 只支持 JSON 响应子集

`StreamableHttpMCPGateway` 对所有响应直接调用 `response.json()`，也没有 initialize、协议版本和 session id 流程，[`adapters.py`](../backend/app/runtime/adapters.py#L32)。合法的 `text/event-stream` 响应无法解析。README 将 “MCP HTTP/STDIO” 整体标为 available 过于乐观；应在实现标准会话和 SSE 前标为 partial。

### P2-2 — MCP Tool 测试接口绕过运行时契约与脱敏路径

`/mcp-tools/{snapshot_id}/test` 直接调用 `call_tool_snapshot()`，[`routes.py`](../backend/app/api/routes.py#L861)；后者不校验 inputSchema、不归一化/校验 outputSchema，也不阻止 Secret 回显，[`mcp.py`](../backend/app/application/mcp.py#L261)。因此“测试成功”不等于 Workflow MCPExecutor 会成功。应复用 MCPExecutor 的单一调用服务。

### P2-3 — Alembic 历史迁移依赖当前 ORM 元数据

`0001` 直接执行当前 `Base.metadata.create_all()`，`0001_initial.py`；`0003`、`0004` 等也从当前模型取表定义。这使旧迁移随 ORM 变化而变化，后续迁移的 `checkfirst` 又会掩盖问题。当前空库升级通过，不能证明迁移历史可重放。应把每个 revision 改成固定 `op.create_table/op.add_column` 定义。

### P2-4 — Readiness 把 Alembic head 写死为 `0005`

`/health/ready` 直接比较字符串 `0005`，[`routes.py`](../backend/app/api/routes.py#L181)。新增任何 migration 后，如果忘记同步修改路由，已升级到新 head 的数据库会被错误判为不可用。应从 Alembic ScriptDirectory 读取当前 head，或由构建生成唯一常量。

### P2-5 — Executor 先写 Artifact，CAS 失败会留下孤儿文件

Agent/Browser 在 Worker 提交结果前写入磁盘；若节点已被接管、取消或 CAS 失败，数据库不会登记这些 Artifact，但文件仍保留。应先写 staging 路径，CAS 成功后原子落位并登记；失败时清理 staging，或增加定期 GC。

### P2-6 — `make demo-check` 名称与实际验收范围不一致

Makefile 只串联 pytest、迁移和前端检查，[`Makefile`](../Makefile#L1)。六条测试多数直接调用服务对象或路由函数，Worker 也在测试进程中实例化；没有启动真实 API 和独立 Worker。应新增黑盒 harness，并让 `demo-check` 显式调用；否则将当前目标改名为 `quality-check`，避免误判验收完成。

## 4. 测试缺口

下一轮最有决策价值的测试不是继续增加单执行器 happy path，而是：

1. Browser L2 只读确认、L2 提交确认和审批后页面再次变化。
2. 两个数据库 session 同时 claim，验证 `maxParallelNodes=1` 永不超额。
3. MCP 同名 Tool Schema 改变、标准 Streamable HTTP SSE、测试接口与 Executor 结果一致。
4. 前端候选组件补参并确认，最终出现 Run 链接。
5. 真实 TraeX fixture 输出计划/Tool/完成事件，Host 拒绝 allowlist 外调用。
6. 真正启动 API 与 Worker 子进程的六条黑盒 E2E。

## 5. 已执行验证

| 命令 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| Ruff | 通过 |
| Pytest | 46 passed |
| TypeScript check | 通过 |
| ESLint | 通过 |
| Vitest | 4 passed |
| Vite build | 通过 |
| Alembic 空库升级到 0005 | 通过 |

质量门禁通过说明当前实现内部一致，但不抵消上述未覆盖分支和验收口径差异。


> 2026-09-16：以上为历史审计记录。旧数据库迁移链已移除，当前使用[固定初始结构](../backend/migrations/versions/0001_refactored_schema.py)，旧数据按要求清空重建。
