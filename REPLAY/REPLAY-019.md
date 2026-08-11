# REPLAY-019：人工体验问题修复与产品化交互

日期：2026-08-10

依据：[`NEXT_STEPS-019.md`](../STEP/NEXT_STEPS-019.md)、
[`NEXT_STEPS-018.md`](../STEP/NEXT_STEPS-018.md)、
[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md) 和
[`docs/schema.md`](../docs/schema.md)。

执行基线：`f87ed67`。本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：单用户 Local Demo；不包含鉴权/RBAC、多租户、生产沙箱和分布式部署。

## 1. 结论

019 的代码实现、迁移、自动化门禁和真实浏览器产品交互已经完成：

```text
用户消息
  → 创建持久 ConversationTurn 并冻结 workflowModeSnapshot
  → Agent 语义选择或明确返回 selection unavailable
  → 来源可审计的 bindings + Draft 2020-12 Schema 校验
  → Workflow Runtime / 通用 Agent
  → 规范 JSON
  → 可选且独立的 Presentation
  → Turn 终态耗时 + ModelUsage 汇总
  → Conversation / Component / WorkflowVersion 历史恢复
```

工作流关闭不再只是 UI 或 prompt 提示：新 Turn 的 requested capabilities、Host 实际授权和服务端执行入口均拒绝
`workflow.*` 能力。未配置选择模型时不再取目录第一项，而是明确返回
`WORKFLOW_SELECTION_UNAVAILABLE`。

规范输出和阅读结果已分层。`Run.output_json` 始终是事实源；Presentation 有独立状态、事件、耗时、模型和错误，
失败不会污染成功 Run。`presentation.delta` 每个分片独立提交，SSE 可以在 `completed` 前观察增量。

自动化门禁全绿，真实 API/Worker/Vite/Chromium 产品交互通过。浏览器 Agent 场景使用
`examples/traex_stub.py` 验证 Host 协议和页面闭环；本机安装的 `traex exec --json` 不直接实现当前
`TraeXSubprocessAdapter` 的 ToolResult 回送协议。因此，019 的外部真实模型语义质量验收仍需在提供兼容
TraeX Agent command 后补跑，不能把 deterministic fixture 记作 `NEXT_STEPS-019.md` 6.2 中的真实 Agent 证据。

## 2. Conversation 模式与 Agent 选择

Conversation 新增持久 `workflow_mode=enabled|disabled`，默认开启。每条消息先创建 `ConversationTurn`，
再冻结 `workflow_mode_snapshot`；之后切换开关不会改变已经开始的 Turn 或暗中取消活动 Run。

新增：

- `PATCH /conversations/{id}/workflow-mode`；
- `GET /health/ready` 的 `workflowSelectionMode=agent|unavailable`；
- 关闭模式时把尚未执行的 `PendingSelection` 置为 `cancelled_by_mode_change`；
- 旧候选确认入口再次检查 Conversation 模式，防止绕过 UI 启动工作流。

工作流关闭时，通用 Agent 仍可运行，但有效 capabilities 不含 `workflow.*`。服务端 fencing 不信任模型输出；
即使构造工作流调用，也不能越过执行入口。

`WorkflowSelection` 新增 `bindingSources`，记录：

- 字段名；
- `user_message/history_message/tool_result` 来源；
- 来源 message id 和原文 quote；
- confidence；
- uncertain 标记。

选择 prompt 要求 Agent 先做 L0 搜索，再按需读取 L1/L2，并比较 `useWhen/doNotUseWhen`、Schema、副作用和成本。
服务端使用 `Draft202012Validator` 和 `FormatChecker` 校验 required、type、enum、format 和
additionalProperties。不确定来源转为追问，不进入 Run。

模型不可用时返回：

```json
{
  "state": "unavailable",
  "code": "WORKFLOW_SELECTION_UNAVAILABLE",
  "selectionMode": "unavailable"
}
```

目录第一项静态降级已删除；天气、上海、URL 或业务 ID 均未增加硬编码产品路由。

## 3. ConversationTurn 与 Usage 账本

迁移 `0018_product_conversation_history.py` 新增 `conversation_turns`，并把 Message、Run 和
ConversationRun 关联到同一 `turn_id`。失败接力 continuation 继承源 Turn，避免换 Worker 或 generation 后重新计时。

Turn 保存：

- user/assistant message；
- parent Turn；
- workflow mode snapshot；
- accepted/first-response/completed 时间；
- status；
- 最终 `elapsed_ms`。

Worker 周期调用 `finalize_ready_turns()`，以最终助手结果或 Presentation `completed/failed` 闭合 Turn。
Chat 发送后乐观计时，加载服务端时间后校准；刷新不会归零或创建多个 timer。

新增 `model_usage` 幂等账本，以 `(call_id, attempt)` 唯一约束去重。覆盖：

- 工作流选择 Agent；
- 通用 Agent；
- Workflow 内 LLM/Agent 节点；
- Presentation LLM；
- 失败接力实际执行的模型调用。

Usage 展示状态为：

- `zero`：确认全程无模型消耗；
- `complete`：provider 返回完整 usage；
- `partial`：已统计部分调用，另有调用缺 usage；
- `unavailable`：存在模型调用但无法取得可信统计。

事件重放和 SSE 重连只读取账本，不重复累计 Token。

## 4. 持久历史与直接运行

Conversation 新增 `last_message_at`。写入首条用户消息时，在同一业务流程中更新活跃时间，并把默认“新对话”
改为确定性截断标题。

新增或扩展 API：

| API | 用途 |
|---|---|
| `GET /conversations?cursor=&limit=&query=` | 稳定游标分页、最近活动排序、标题和 preview |
| `GET /runs?componentId=&workflowVersionId=&status=&cursor=&limit=` | 服务端组合过滤 Run 历史 |
| `GET /workflows/{id}/versions` | 已发布不可变版本历史 |
| `POST /workflow-versions/{id}/runs` | 使用已发布版本和 Schema 表单直接运行 |
| `GET /runs/{id}` | 返回 Turn、耗时、Usage 和 Presentation |

`0018` 增加 `(component_id, created_at, id)`、`(workflow_version_id, created_at, id)` 等索引。
前端不再先拉全部 Run 再过滤。

Chat 增加桌面历史侧栏和窄屏抽屉，支持新建、切换和加载更多。路由切换会清理旧会话临时状态和轮询，
随后从服务端恢复消息、Run、Agent 轨迹、Turn 和开关。

组件详情展示服务端调用历史；工作流编辑页展示发布版本与版本运行历史。两者均可进入 RunDetail 查看输入、
节点 attempt、事件、错误、规范结果和阅读结果。

## 5. JSON 与 Presentation 分层

WorkflowVersion 新增冻结的 `presentation_policy_json`，最少包含：

- enabled；
- audience；
- producer=`template|llm`；
- template 或 prompt；
- language；
- maxLength。

内部 Workflow 默认关闭 Presentation。模板 producer 不调用 LLM，支持 `{json}` 和顶层输出字段占位。

新增 `run_presentations`：

- run/revision/status/final text；
- producer/model；
- config digest；
- prompt/template digest；
- error；
- started/completed/elapsed 时间。

`PresentationDriver` 只领取 Presentation 已启用且尚未生成派生结果的 succeeded Run。事件顺序为：

```text
run.succeeded
  → presentation.started
  → presentation.delta（每个分片独立事务）
  → presentation.completed | presentation.failed
```

RunDetail 和 Chat 均先展示规范 JSON，再展示阅读结果，并分别复制。刷新后从
`GET /runs/{id}` 恢复最终 Presentation，不依赖已经结束的 SSE。

Presentation 失败只写 `PRESENTATION_FAILED`，不修改 Run status 和 `output_json`。

## 6. 主题与元数据闭环

主题统一使用根节点 `data-theme` 和 `skillx-theme` localStorage：

- 首次访问跟随 `prefers-color-scheme`；
- 用户选择优先于系统主题；
- React 挂载前应用主题，避免首屏闪烁；
- 按钮具备 `aria-label` 和 `aria-pressed`。

CSS 改为语义 token，覆盖背景、面板、输入、textarea、select、RJSF、React Flow、代码块、JSON/SSE 区域、
disabled/hover/focus 和移动端历史抽屉。

图 1 白色输入框的直接原因是 `.inspector-fields > label > input` 未命中旧规则。修复后 summary、useWhen、
doNotUseWhen、examples、tags 和 Presentation policy 均可在可视化编辑器中修改；保存、刷新、发布后仍存在。

目录召回包含 summary、useWhen、examples 和 tags；`doNotUseWhen` 不再作为正向 search text 提高分数，
而是留给 Agent 在 L1 比较时排除。搜索结果增加用户可读 `matchReason`。

## 7. 浏览器验收发现并修复的问题

真实 Chromium 验收额外发现两个自动化原先未覆盖的问题：

1. 已发布版本的 inputSchema 声明 Draft 2020-12 `$schema`，而 RJSF 默认 AJV validator 按 Draft 7
   装载，提交时报 `no schema with key or ref`。
2. React StrictMode 会重复执行 `/chat` 初始化 effect，产生两个“新对话”。

对应修复：

- `rjsfSchema()` 只在 RJSF 展示层移除 `$schema` 元声明，不改变持久 Schema 或后端 Draft 2020-12 校验；
- Chat 使用请求级 `useRef` 防止 StrictMode 重复创建，路由落到 `/chat/:id` 后释放门闩。

专项回归测试覆盖元声明剥离不修改原对象，以及 StrictMode 下 `createConversation` 只调用一次。

## 8. 数据迁移

Alembic head 从 `0017` 升到 `0018`，新增：

- `conversations.workflow_mode/last_message_at`；
- `conversation_turns`；
- `messages.turn_id`；
- `runs.turn_id`；
- `conversation_runs.turn_id`；
- `workflow_versions.presentation_policy_json`；
- `run_presentations`；
- `model_usage`；
- Conversation 和 Run 历史索引。

迁移支持 upgrade/downgrade。ConversationTurn 与 Message 的双向逻辑引用采用分阶段写入，避免同一 flush 中的循环
外键排序和 SQLite foreign-key failure。

## 9. 验证记录

| 门禁 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| 后端 Ruff | 通过 |
| 后端 Pytest | 262 passed |
| Alembic 空库升级 | `0001→0018` 通过 |
| 前端 TypeScript/ESLint | 通过 |
| 前端 Vitest | 39 passed |
| 前端 production build | 通过 |
| 进程级 E2E | 34 passed |
| `git diff --check` | 通过 |
| 真实 API/Worker/Vite/Chromium | 产品交互通过；console 无 error |

最终代码调整后分别重跑后端 262 项、前端 39 项与 E2E 34 项，均通过。临时 API、Worker 和 Vite 服务已停止。

## 10. 真实浏览器证据

已完成：

1. 亮色主题打开编辑器，修改 summary/useWhen，保存、刷新、发布。
2. 切换主题并刷新，主题偏好和字段可读性保持。
3. 使用已发布 WorkflowVersion 的 JSON Schema 表单输入 `Alice`。
4. RunDetail 先保留合法结构化 JSON，再显示模板 Presentation“你好，Alice”。
5. Chat 创建、切换并刷新多个 Conversation，历史按最近活动时间恢复。
6. 关闭工作流后发送新 Turn，服务端保存 `workflowModeSnapshot=disabled`。
7. 关闭模式下通用 Agent 成功，Turn 持久保存 `elapsedMs=529` 和完整 `20 Token`。
8. 刷新后模式、消息、轨迹、耗时和 Usage 均恢复。
9. 浏览器 console 无 error，仅有 Vite 开发态 React DevTools info。

未计作真实 Agent 证据：

- deterministic TraeX fixture 的语义选择结果；
- stub 对 Tool 事件、Token 或绑定来源的固定输出；
- HTTP 冒烟或单测中的 mock selection。

要关闭该缺口，需要提供实现当前 Host 双向协议的真实 TraeX command，至少支持
`agent.plan/tool.call.started/tool.call.completed/agent.completed` 及 stdin ToolResult 回送，然后重跑
`NEXT_STEPS-019.md` 6.2 的上海天气自动选择、L0/L1 轨迹、`city=上海` 来源和关闭模式对照场景。

## 11. 完成矩阵

| 019 门禁 | 状态 | 证据 |
|---|---|---|
| 亮/暗主题与图 1 字段 | 通过 | 保存、刷新、发布和真实 Chromium 验证 |
| 持久 Conversation 历史 | 通过 | 游标 API、侧栏/抽屉、切换与刷新恢复 |
| 工作流模式持久化 | 通过 | Conversation 设置、Turn 快照、PendingSelection 闭合 |
| capability fencing | 通过 | requested/Host/执行入口三层约束及专项测试 |
| 禁止目录第一项降级 | 通过 | 模型不可用返回稳定 unavailable |
| Agent bindings 与 Schema 校验 | 代码通过 | 来源模型和 Draft 2020-12 校验已实现；外部真实模型场景待补证 |
| Component/WorkflowVersion 历史 | 通过 | 服务端过滤、索引、直接运行和详情入口 |
| JSON + Presentation | 通过 | 独立持久化、SSE delta、失败隔离和刷新恢复 |
| Turn 耗时与 Usage | 通过 | 稳定归属、幂等账本、完整/部分/不可用状态 |
| 018 能力无回归 | 通过 | 后端、前端与 34 项进程级 E2E 全绿 |
| 外部真实 TraeX Agent | 待补证 | 本机 CLI 与当前双向 Host 协议不兼容，fixture 不作替代证据 |

结论：019 实现和本地产品化交互已完成；在外部真实 Agent 证据补齐前，不将
`NEXT_STEPS-019.md` 6.2 的完整人工验收标记为无条件通过。
