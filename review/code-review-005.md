# 代码审查报告（005）

审查日期：2026-08-04  
审查范围：`REPLAY-004` 对应提交 `9a9ac46..be64409` 的后端、前端、迁移和测试。  
范围约束：单用户 Local Demo，不审查鉴权/RBAC；能力隔离、版本锁、审计、幂等和恢复仍在范围内。

## 1. 结论

完整门禁通过，本轮确实修复了 004 的长任务持久化、旧 Snapshot 原地修改、表达式索引和基础编辑闭环。审查发现 **4 个 P1、5 个 P2**。最高风险不是“缺少更多节点”，而是两项核心声明仍不能成立：当前 Distillation 会把不满足查表语义的真实 Tool 误编译并丢动作；同一个 TraeX 信任边界在 Conversation Agent 和 Workflow Agent 中有两套安全实现。

## 2. P1 问题

### P1-1 — Distillation 把任意字典结果当查表，且混合轨迹会静默丢弃未识别动作

证据：[`distillation.py`](../backend/app/application/distillation.py) 第 67–100 行只要某个参数等于原 prompt 且结果是 `dict`，就把整个结果冻结为 `lookup.table`。它没有验证 Tool 类型、结果是否真是 key→value 全表、能力版本、输出 Schema 或参数来源。只要存在一个可转 lookup 的动作，第 102–141 行就不再创建 Agent 节点；其它不能参数化的动作已在循环中被 `continue` 丢弃。所有输出字段还会被映射到同一个 scalar `last_result_ref`。

实际网页获取会典型触发误编译：参数 `url` 等于用户输入、结果是 `{title, content}`，于是第二次用 URL 查询这张字典，只会得到默认值；它不会重新访问网页。多字段 outputSchema 还会收到重复 scalar，可能编译通过但运行语义错误。

现有测试为什么没发现：[`test_distillation.py`](../backend/tests/test_distillation.py) 只使用人工构造的 `LOOKUP_TABLE={q1:..., q2:...}`，恰好满足唯一 heuristic；没有真实 MCP/Browser/API 动作、混合动作、多输出或能力锁断言。

建议：引入带 `kind/capabilityRef/version/digest/inputBindings/outputSchema/effect/idempotent` 的 NormalizedAction；每类动作由注册的 action compiler 明确返回 `compiled|agent|rejected`。未知动作必须按原顺序保留为受限 Agent 节点，不能丢弃。发布前验证节点输出到 workflow output 的 Schema 映射，并以真实能力 lock 而不是 Tool 名称字符串作为证据。

### P1-2 — Workflow Agent 绕过新的 Host 事件契约、权限策略和步数/Token 上限

证据：Conversation Agent 使用 `AgentEventValidator`、Host capability 交集和预算检查；但 [`executors.py`](../backend/app/runtime/executors.py) 第 430–560 行的 `AgentExecutor` 仍直接读取 `event.get()`，把节点配置中的 `allowedCapabilities` 原样交给 TraeX。Tool 事件只有在 payload 恰好包含 `tool` 时才检查；未知 type、缺 callId、started/completed 不配对和超大 payload均可进入事件流。代码只累计 step/Token，从未与 `maxSteps/maxTotalTokens` 比较。

影响：已发布 Workflow 中的 Agent 节点——也正是通配型 fallback 需要使用的节点——安全语义弱于对话 Agent。畸形 Runtime 可绕过 Tool 校验或预算后仍成功，REPLAY-004 的 Host fail-closed 只覆盖了一个入口。

建议：只保留一个 Agent Host 执行服务，ConversationRun 与 NodeRun 共享事件 validator、能力交集、预算、取消、输出 Schema 和脱敏逻辑；差异只在持久化 sink。删除 `AgentExecutor` 内的第二套事件循环或让它成为统一 Host 的薄适配器。

### P1-3 — MCP 分类修订只复制一个 Tool，却把不完整版本设为 Server current

证据：[`mcp.py`](../backend/app/application/mcp.py) 第 358–426 行为一次分类新建 ServerVersion，但只创建被分类的 `old.name` 一个 Snapshot，随后把 `server.current_version_id` 指向该版本。原版本的其它 Tool 没有复制。再次 discovery 时，未分类的其它 Tool 可以补建，但被分类 Tool 的 registry effect digest 往往与远端 annotation/unknown digest 不同，第 248–253 行会把它判为同版本契约漂移并中断。

影响：多 Tool MCP Server 分类任意一个 Tool 后，current 版本立即丢失其它 Tool；常见的 `unknown → write/read` 审核版本也无法通过再次 discovery 自愈。单 Tool 单测只能证明旧行没被修改，不能证明 ServerVersion 是完整不可变快照集合。

建议：分类修订必须在同一事务中克隆原 ServerVersion 的全部 Snapshot，只替换目标 Tool 的审核字段；ServerVersion digest 覆盖完整有序 Snapshot 集合与本地 policy。更稳妥的方案是把远端 contract snapshot 和本地 effect review policy 分成两个独立版本对象，避免 discovery 与人工分类争夺同一个 digest。

### P1-4 — Tool 参数和结果仍原样写数据库，违反“仅保存脱敏摘要”与 Secret 边界

证据：[`agent_run.py`](../backend/app/application/agent_run.py) 第 386–415 行验证事件后立即 `_append_committed(..., event["payload"])`，`tool.call.started.arguments` 和 `tool.call.completed.result` 会完整进入 `conversation_run_events.payload_json`。[`agent_events.py`](../backend/app/application/agent_events.py) 第 19–20 行允许 payload 任意扩展字段，且大小限制使用 `len(str(payload))` 而非规范 JSON 的 UTF-8 字节数。之后同一原始事件又写入 trajectory 文件。

影响：Tool 参数/结果中的 Secret、网页正文、文件内容或大对象会长期留在数据库并通过 events API 返回，与 README 和 REPLAY 的“数据库只保存脱敏摘要”相反；中文等多字节内容还可超过配置的真实字节上限。

建议：validator 输出分为 `runtime_event` 与 `audit_event`；后者只含 callId、能力锁、参数字段名/摘要、结果摘要、字节数和 Artifact 引用，统一调用结构化 redactor。大小用 canonical JSON UTF-8 bytes 计算，超大参数/结果只允许写受生命周期管理的 Artifact。Secret 值在进入任何 sink 前按 handle/provenance 脱敏。

## 3. P2 问题

### P2-1 — 请求包含 Host 未授予能力时只记录 narrowed，未按函数契约失败

[`agent_run.py`](../backend/app/application/agent_run.py) 第 91–101 行声明 denied 会以 `PERMISSION_DENIED` fail closed；第 345–355 行却只追加 `agent.capabilities.narrowed` 后继续执行。请求仅包含被拒能力时，TraeX 会收到空集合，可能返回一个与原任务无关的“成功”。

建议：明确产品语义并只保留一种：如果 requested 表示完成任务所需能力，任一被拒即在启动执行前终止 `PERMISSION_DENIED`；如果表示上限，则字段重命名为 `capabilityCeiling`，实际计划缺能力时由 Runtime 返回结构化失败。当前文案、实现和测试不得互相矛盾。

### P2-2 — Conversation Agent 轨迹绕过 Artifact 表、staging 和 reconcile

[`agent_run.py`](../backend/app/application/agent_run.py) 第 596–633 行在数据库成功事务前直接写最终目录，并返回绝对 `file://` 路径。它没有 Artifact 行、`artifact://id`、stagingKey 或 hash，也不受 [`reconcile_run_artifacts`](../backend/app/application/orchestrator.py) 管理。终态提交失败会留下孤儿，API 还暴露宿主机绝对路径。

建议：复用 `write_json_artifact`/Artifact outbox；先登记 metadata 和 staging，再在提交后落位。API 只返回 artifact id/受控下载入口，不返回绝对路径。

### P2-3 — Agent Worker 异常被静默吞掉，运行只能等待租约过期

[`worker.py`](../backend/app/worker.py) 第 29–33 行捕获 `agent_worker.poll_once()` 的所有异常并不记录。若异常发生在领取后、内部终态处理前，用户只能等租约过期后得到 `UNKNOWN_OUTCOME`，日志中没有根因。

建议：记录结构化异常与 run/worker 上下文；若已知 run id，尝试持久化 Worker 内部错误；仅对预期的 SQLite 竞争降级为下一轮，未知异常至少输出 stderr/日志指标。

### P2-4 — Artifact reconcile 只有函数和测试，没有运行入口

全仓只有 [`test_artifact_staging.py`](../backend/tests/test_artifact_staging.py) 调用 `reconcile_run_artifacts()`；Worker 启动、租约恢复和 API 均未调用。REPLAY 把它称为 GC，但当前 demo 正常运行不会自动修复“DB 有记录、promote 未完成”的窗口。

建议：Worker 启动及周期任务扫描未完成/近期 Run，调用 reconcile 并发审计事件；限制目录与批量大小。测试覆盖进程在 DB commit 后、promote 前退出，再启动 Worker 自动补落位。

### P2-5 — 对话刷新创建新 Conversation，Agent 消息也不会随终态更新

[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx) 第 33–38 行每次挂载都创建新对话，所以第 40–50 行所谓“刷新后恢复最近 Agent”只在仍持有同一 conversationId 时成立。服务端 Agent 成功/失败只更新 ConversationRun，不更新关联 assistant message；第 306–322 行因此在 queued 状态就展示“固化为组件”，终态后正文仍是“已进入队列”。候选卡片的“继续 Agent”还取全局最近 prompt，而非产生该候选的用户消息。

建议：conversationId 进入 URL 或持久导航状态；assistant message 保存 run 引用并由展示层实时 join 状态，只有 succeeded 才展示固化按钮；候选消息保存 sourceUserMessageId/sourcePrompt。

## 4. 缺失的高价值测试

1. 真实 `{title, content}` Tool 结果不得编译为 lookup；混合轨迹的未识别动作不能消失；多字段输出必须通过运行时 Schema。
2. Workflow Agent 与 Conversation Agent 对未知事件、缺 tool/callId、超步数/Token、越权 Tool 得到完全相同的终态。
3. 两个以上 Tool 的 MCP Server：分类其中一个后，新版本仍包含全部 Tool，重复 discovery 稳定，旧版本不变。
4. Tool 参数包含 secret-like 字段和多字节大 payload 时，数据库/API 不出现原值且真实 UTF-8 上限生效。
5. Agent Artifact 在 commit/promote 各故障点可自动 reconcile，无 `file://` 泄漏。
6. 刷新 `/conversations/{id}` 恢复同一会话；queued/failed 不可固化，succeeded 可固化。
7. 通用 checkpoint 接力测试：已完成节点不重放，剩余节点可由显式 Agent fallback 继续。

## 5. 验证结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff | 通过 |
| 后端 Pytest | 124 passed |
| Alembic 空库升级 | 0001 → 0008 通过 |
| 前端 check / lint / test / build | 通过，23 passed |
| 黑盒 E2E | 9 passed |

门禁通过不改变上述 finding：现有测试主要证明预设 fixture 路径，尚未覆盖多能力、恶意/畸形边界和通配恢复语义。
