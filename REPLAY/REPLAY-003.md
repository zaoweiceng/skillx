# NEXT_STEPS-003 执行回放（REPLAY-003）

依据：[`NEXT_STEPS-003.md`](NEXT_STEPS-003.md)、[`review/design-compliance-review-003.md`](review/design-compliance-review-003.md)、[`review/code-review-003.md`](review/code-review-003.md)。
执行范围：A（真实 Agent 固化）、B（最终黑盒验收真实性）、C（MCP 副作用契约）、E（边界语义与工程收尾），并对 D（可视化编辑）做了非破坏性的高价值补强。
非目标：鉴权、RBAC、多租户、生产级沙箱、分布式部署、完整拖放画布。

## 1. 总体结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff（app/tests/migrations/e2e/examples） | 通过 |
| 后端 Pytest | 85 passed（002 基线 74） |
| 空库 Alembic 升级 | 0001 → 0007 通过 |
| 前端 check / lint / test / build | 通过（13 passed，002 基线 10） |
| 黑盒 E2E（`pytest e2e`） | 7 passed，全部执行标题描述的真实路径 |

核心结论：`make demo-check` 通过现在**名实一致**——E2E-5 走真实 Agent → ConversationRun → Distillation → 人工发布 → 新输入复用；E2E-6 走真实 Playwright 打开本地表单站点、一次性授权后提交，并断言站点只收到一次提交。

## 2. A：真实 Agent → 固化 → 复用

### A-01 唯一 Agent Run Service（已完成）

- 新增 [`agent_run.py`](backend/app/application/agent_run.py) 的 `AgentRunService`：启动受限 TraeX 子进程、归一化并持久化事件、执行 allowlist 与输出 Schema 校验、处理超时/取消，成功时写入结构化 output、规范化 steps、Token/步数与 Tool 事件摘要。
- 新增真实 endpoint [`POST /conversations/{id}/agent-runs`](backend/app/api/routes.py#L1332)，并新增 [`GET /conversation-runs/{id}`](backend/app/api/routes.py#L1400) 与 [`/events`](backend/app/api/routes.py#L1411) 用于状态/事件恢复。删除了指向不存在路由的注释。
- 只有 `AgentRunService` 会创建 `ConversationRun(run_type="agent_run")`；事件持久化到新表 `conversation_run_events`（migration 0007），刷新页面后可恢复，不依赖前端本地 state。
- 未知/格式非法的 Agent 事件与越权 Tool 事件默认拒绝（`AGENT_EVENT_INVALID` / `PERMISSION_DENIED`），不绕过 allowlist。

### A-02 对话产品路径（已完成）

- 未匹配组件时，`llm_response` 与 `MODEL_NOT_CONFIGURED` 提示均带 `offerAgentRun`，前端提供“启动通用 Agent 探索”入口。
- 候选卡片“继续通用 Agent”现在真正调用 Agent endpoint（[`ChatPage.tsx`](frontend/src/pages/ChatPage.tsx)），不再只隐藏卡片。
- 右侧轨迹抽屉订阅 `conversation-runs/{id}/events`，展示 plan/Tool/step/终态，并按终态停止轮询。
- 刷新后从服务端消息恢复最近一次 `agent_run` 并重新拉取事件。

### A-03 固化分析与发布（已完成）

- [`distill_conversation_run`](backend/app/application/distillation.py#L105) 现在只接受服务端生成的成功 `agent_run`；`llm_response` 与 `trajectory_import` 均被拒绝。
- 固化保留 `sourceRunId`/`sourceRunType` 与 step 摘要，用户人工发布，不自动发布。

覆盖测试：[`test_agent_run_service.py`](backend/tests/test_agent_run_service.py)（服务级 5 用例，含事件、越权、缺失输出、endpoint→固化、未配置降级）。

## 3. B：最终黑盒验收真实性

### B-01 重写 E2E-5（已完成）

[`test_demo_e2e.py`](backend/e2e/test_demo_e2e.py) 的 E2E-5 现在：HTTP 启动仓库内 TraeX-compatible Agent → 断言服务端生成成功 `agent_run` 及 `agent.*`/Tool 事件 → 固化只生成草稿、无 WorkflowVersion → 通过 API 人工发布并创建组件 → 用不同输入运行发布组件并验证复用输出。

### B-02 重写 E2E-6（已完成）

- 新增本地表单站点 [`local_form_site.py`](examples/local_form_site.py)（记录提交次数与最近提交数据，暴露 `/__stats`、`/__mutate`）。
- 发布组件“费用提交（真实浏览器）”使用 `runtime="playwright"`，不再依赖 `observedPage`/`fakeSubmitResult`。
- E2E-6：第一次执行完成检查/填写并等待提交审批；批准后重放、提交、成功断言，并断言 HTTP server 只收到一次提交。
- E2E-6b：审批前页面新增必填字段（`/__mutate`）触发 L3 漂移，旧授权失效、运行失败、未产生提交。

### B-03 Harness 可靠性（已完成）

[`harness.py`](backend/e2e/harness.py)：动态分配空闲端口（支持并行）；失败时打印 API/Worker/表单站点日志并保留临时目录，成功时清理；通过 Worker 的 `worker polling` 就绪日志确认已进入轮询循环（[`worker.py`](backend/app/worker.py#L23)），而非只检查进程存活；`make demo-check` 现只在 B-01/B-02 完成后宣称等价于六条验收。

## 4. C：MCP Effect 与幂等性

### C-01 Registry 契约（已完成）

- MCP ToolSnapshot 新增 `effect_source`、`effect_review_status`（migration 0007）。
- [`classify_effect`](backend/app/application/mcp.py#L51) 读取 annotations 仅作提示；未分类 Tool 默认 `effect="unknown"`、`idempotent=false`、来源 `unknown`，不再默认 `none/true`。
- 契约 digest 纳入 effect/idempotent/timeout；变更经 [`classify_snapshot`](backend/app/application/mcp.py#L348) 生成新 digest。新增 [`POST /mcp-tools/{id}/classify`](backend/app/api/routes.py#L1004) 供人工审核。

### C-02 发布与运行约束（已完成）

- [`validate_mcp_nodes`](backend/app/application/mcp.py#L385)：未分类/未审核 Tool 阻止发布；写/破坏性 Tool 必须声明 `sideEffect`；非幂等 Tool 的 `maxAttempts` 必须为 1。
- Tool test 接口返回 `effect`/`idempotent`/`autoRetryAllowed`/`effectReviewStatus`。
- 天气示例 Tool 增加 `readOnlyHint`/`idempotentHint`，保持 E2E-2 可发布。

覆盖测试：[`test_mcp_effects.py`](backend/tests/test_mcp_effects.py)（未分类默认、未审核阻止发布、非幂等禁重试、写操作需声明、分类改变 digest）。

## 5. E：边界语义与恢复

- `candidateToken` → `candidateSnapshotDigest`：明确其为 stale 检测的快照摘要而非授权/一次性令牌（[`conversation.py`](backend/app/application/conversation.py#L34)、routes、前端 client/ChatPage 同步更新）。
- VariablePicker：Input 节点变量来自 workflow `inputSchema` 生成 `${input.field}`（不再错误的 `${input.output}`），其他节点用 `outputSchema`，支持嵌套对象与数组路径；新增类型兼容提示 `typeCompatibility`（[`variablePicker.ts`](frontend/src/lib/variablePicker.ts)）。
- [`build_contract_patch`](backend/app/runtime/contract_patch.py#L38) 内部独立校验 `url_matches`，作为独立安全边界拒绝 URL 不匹配。
- Agent Tool 事件规范固定 event type 与字段；未知事件格式默认拒绝（`AgentRunService._normalize`）。
- README 能力状态更新：新增“通用 Agent 探索与固化”“MCP 副作用与幂等分类”为 `available`，六场景 E2E 说明补齐真实路径。

## 6. D：可视化编辑（非破坏性补强）

设计文档将完整拖放画布列为剩余项（非阻断）。本轮未实现完整画布，但完成两项高价值补强：
- 发布按钮在整个 save/validate/publish/create-component 流程保持 busy，阻止双击并发发布（[`WorkflowEditorPage.tsx`](frontend/src/pages/WorkflowEditorPage.tsx#L82)）。
- Inspector 通过修正后的 VariablePicker 正确展示 `${input.field}` 与嵌套/数组输出路径。

完整拖放/连线/删除/缩放画布仍为后续工作。

## 7. 验收矩阵对照

| 目标 | 证据 |
|---|---|
| Agent 对话 | `POST /conversations/{id}/agent-runs` HTTP 启动、`conversation_run_events` 持久化、仓库内 TraeX fixture、事件与超时/取消 |
| Agent 固化 | E2E-5：同一 ConversationRun 经 Distillation、人工发布、新输入复用 |
| Browser | E2E-6：独立 Worker 驱动真实 Playwright，本地站点 `submitCount == 1` |
| MCP 写操作 | 未分类阻止发布、非幂等不重试、写操作需声明 sideEffect（test_mcp_effects） |
| 可视化编辑 | 发布 busy 守卫 + 正确 VariablePicker；完整画布仍待办 |
| 总门禁 | `make demo-check` 退出码 0，测试实现与六条场景逐项一致 |

## 8. 变更文件清单（主要）

后端：`app/application/agent_run.py`(新)、`app/application/mcp.py`、`app/application/distillation.py`、`app/api/routes.py`、`app/application/conversation.py`、`app/runtime/adapters.py`、`app/runtime/contract_patch.py`、`app/infrastructure/database.py`、`app/worker.py`、`app/seed.py`、`migrations/versions/0007_agent_run_service.py`(新)。
测试：`tests/test_agent_run_service.py`(新)、`tests/test_mcp_effects.py`(新)、`tests/test_migrations.py`、`tests/test_conversation_component.py`、`tests/test_browser.py`、`e2e/harness.py`、`e2e/test_demo_e2e.py`。
示例：`examples/local_form_site.py`(新)、`examples/local_weather_mcp.py`。
前端：`src/api/client.ts`、`src/pages/ChatPage.tsx`、`src/pages/ChatPage.test.tsx`、`src/lib/variablePicker.ts`、`src/lib/variablePicker.test.ts`、`src/pages/WorkflowEditorPage.tsx`、`src/index.css`。
根：`README.md`、`Makefile`。

## 9. 仍待后续

- MCP Streamable HTTP 的流式增量与重连（保持 `partial`）。
- 完整可视化拖放画布（连线/删除/移动/缩放/MiniMap）。
- Artifact 提交后 finalize/outbox 与最终目录 GC 兜底（P2-3，本轮未改动核心 staging 逻辑）。
- 若确需 candidate 一次性语义，可持久化并原子消费 nonce（当前明确定位为 stale 检测摘要）。
