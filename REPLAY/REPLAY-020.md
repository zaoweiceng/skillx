# REPLAY-020：真实 Agent 主路径与前端终态一致性

日期：2026-08-10

依据：[`NEXT_STEPS-020.md`](../STEP/NEXT_STEPS-020.md)、
[`review/design-compliance-review-020.md`](../review/design-compliance-review-020.md) 和
[`review/code-review-020.md`](../review/code-review-020.md)。

执行基线：`c075ebb`。本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：单用户 Local Demo；不增加鉴权、RBAC、多租户或分布式部署。

## 1. 结论

020 的代码缺口、自动化门禁和 fixture 浏览器链已经修复：

```text
用户消息
  → real | fixture | unavailable 显式 Provider
  → Agent 逐字段提交 bindingSources
  → 服务端核验消息归属、原文 quote、ToolResult path 与 Schema
  → Workflow / 通用 Agent
  → JSON → 可选 Presentation
  → Turn 在最终回复与 Presentation 终态后闭合
  → RFC 3339 UTC 时间与可信 Usage
```

`make demo-check` 和 `make browser-ui-check` 全绿。真实 OpenAI-compatible sidecar 已实现并通过双轮模型
Tool Calling、Host ToolResult 回送和最终结构化输出的进程测试。

本机没有配置真实模型 endpoint、model 或 API key，因此没有执行
`NEXT_STEPS-020.md` 定义的外部真实 Provider 最低语义集。该项不能用 `traex_stub.py` 或本地模拟模型服务替代，
故本报告不宣称 020 完整验收通过。

## 2. P0-1：真实 Agent Provider

新增 [`examples/openai_agent_sidecar.py`](../examples/openai_agent_sidecar.py)：

- 调用 OpenAI-compatible Chat Completions；
- 支持 function calling；
- 模型只产生 `tool.requested`，Tool 仍由既有 Host 执行；
- 通过 stdin 接收同一 `callId` 的 Host `tool.result`；
- 最终发送 `agent.step.completed` 和 `agent.completed`；
- 继续由 Host 强制 allowlist、超时、取消、步数、Token、payload、Schema 和非法事件 fencing。

配置新增 `SKILLX_AGENT_PROVIDER_TYPE=real|fixture|unavailable`。默认值为 `unavailable`；
fixture 只用于协议和故障测试，不再显示为真实语义 Agent。

`GET /health/ready` 现在返回：

- selection Provider 类型；
- subprocess 协议是否可启动；
- 真实模型是否探测成功；
- 最后探测时间和摘要；
- Database、Worker、Browser 和 Secret Provider 状态。

SettingsPage 直接展示同一份 ready 事实。探测失败有错误态；无法证明 Worker 在线时明确显示 `unknown`，
不再用静态“运行中/未配置”占位冒充事实。

验证：`test_real_agent_sidecar.py` 使用本地 OpenAI-compatible HTTP 测试服务进行两轮调用。第一轮模型请求
`lookup.table`，Host 执行并回送 ToolResult，第二轮模型消费真实结果并完成结构化输出。该测试证明协议接入，
不作为外部真实模型语义证据。

## 3. P0-2：Binding 来源信任边界

`WorkflowSelection` 现在要求每个 binding 有且仅有一个同字段 `bindingSource`，空来源、多余来源、字段重复或
缺失均为非法 Agent 输出。

服务端逐项验证：

| 来源 | 验证 |
|---|---|
| `user_message` | message id 属于当前 Conversation、为当前用户消息、quote 是原文片段 |
| `history_message` | message id 属于当前 Conversation、角色为 user、quote 是原文片段 |
| `tool_result` | call id 来自当前 Host 执行结果、path 可解析、path 值等于 binding |

低置信、`uncertain`、跨 Conversation、伪造 id、引文不存在、字段错配、无法证明的规范化和 Schema 错误均转
`ask_user`，不会先执行再补证据。

服务端验证后公开 `serverVerified=true` 和 `normalizationType`。当前支持 exact、whitespace、
place suffix removal、number、boolean、list extraction、JSON 和 ToolResult path；不保存模型私有推理。

PendingSelection 合并新回答时只接受显式表单字段，或在唯一缺失字段且答案为紧凑文本时绑定整段回答；
新回答生成当前 message source，旧来源降级为 history source，随后重新验证全部字段。

负向测试覆盖空来源、伪造 message id、跨 Conversation、引文不存在、字段错配和伪造 ToolResult。

## 4. P0-3：Turn、时间、Usage 与 Presentation

新增统一 `rfc3339()`，SQLite 返回的 naive datetime 按 UTC 恢复并输出 `Z`。Conversation、Message、Turn 和
Presentation 的公开时间均使用显式 UTC。测试覆盖 SQLite round-trip，不再在 Asia/Shanghai 多出 8 小时。

Chat 的 Agent 与 Workflow 轮询现在：

- Agent 进入任一终态后强制刷新 ConversationTurn；
- Workflow succeeded 后若 Presentation 已启用，继续跟踪到 `completed|failed`；
- 每次终态都执行最后一次 Conversation refresh；
- 所有 timer 在 effect cleanup 中清理；
- 最多跟踪 5 分钟，超时显示可恢复提示。

Turn 只在关联 Run/AgentRun 终态且所需 Presentation 终态后闭合。Presentation 完成时间参与最终 elapsed，
失败不修改成功 Run 的 JSON。

Usage 状态新增 `pending`。无账本且 Turn 未完成时显示“Token 统计中”；只有能证明全程没有模型调用时才是
`zero`。Provider 未返回 usage 时写 `unavailable`，部分调用缺失时为 `partial`。

真实 Chromium 回归验证：

- JSON 先出现，模板 Presentation 随后自动出现；
- 两层结果各一份并按独立区域定位；
- 终态指标等待 2 秒不增长；
- reload 后时间、Token、JSON 和 Presentation 一致。

## 5. P1 页面与交互

工作流编辑器：

- Capability metadata 改用独立 `capability-metadata-panel`，不再继承源码编辑器 680px 高度；
- 删除宽泛的 `.inspector-fields > div` 两列规则；
- Presentation 和 Fragment 使用独立响应式网格；
- 桌面与窄屏均不会把复杂表单压入 `100px 1fr`。

主题与系统状态：

- runtime card、nav hover/active 使用语义 token；
- 亮色 runtime card 不再使用固定暗背景；
- 系统页完全读取 ready API，并处理 loading/error/fixture/unavailable。

组件分类：

- 实现全部、零 LLM、局部 LLM、Agent 工作流四类本地过滤；
- `hybrid` 映射到局部 LLM，`agent` 映射到 Agent 工作流；
- 使用 `aria-pressed`，支持点击、焦点和 Enter；
- 空分类显示明确空状态。

## 6. 浏览器门禁

旧用例不再使用宽泛 `get_by_text()` 或 `.first()` 掩盖双层结果。结构化摘要按
`aria-label=结构化结果` 和 `.structured-summary` 精确定位，Presentation 使用独立“阅读结果”区域。

浏览器场景增加：

- Presentation 编辑、发布和异步完成；
- Turn 终态等待 2 秒不增长及 reload 一致；
- 组件筛选键盘操作和 `aria-pressed`；
- 主题切换与 reload 持久化；
- Settings fixture 真值；
- console 0 error / 0 warning。

Makefile 设置项目可写的 `UV_CACHE_DIR=/tmp/skillx-workflow-uv-cache`，避免全局 uv cache 权限阻塞门禁。

## 7. 验证记录

| 门禁 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| 后端 Ruff | 通过 |
| 后端 Pytest | 266 passed |
| Alembic 空库升级 | `0001→0018` 通过 |
| 前端 TypeScript / ESLint | 通过 |
| 前端 Vitest | 10 files / 41 passed |
| 前端 production build | 通过 |
| 进程级 E2E | 34 passed |
| `make browser-ui-check` | 通过 |
| Chromium | 2 passed；console 0 error / 0 warning |
| `git diff --check` | 通过 |
| 真实 sidecar Host ToolResult 协议测试 | 通过 |
| 外部真实 Provider 最低语义集 | 未执行：本机无 endpoint/model/API key |

## 8. 未通过项与恢复方式

唯一未完成的验收证据是外部真实 Provider 最低语义集。当前环境检查结果：

```text
SKILLX_MODEL_ENDPOINT: 未配置
SKILLX_MODEL_NAME: 未配置
SKILLX_MODEL_API_KEY: 未配置
ACEDATACLOUD_API_TOKEN: 未配置
```

配置时不得把 API key 写入仓库。以 OpenAI-compatible Provider 为例：

```bash
export SKILLX_AGENT_PROVIDER_TYPE=real
export SKILLX_MODEL_ENDPOINT=https://provider.example/v1/chat/completions
export SKILLX_MODEL_NAME=provider-model
export SKILLX_MODEL_API_KEY='...'
export SKILLX_TRAEX_COMMAND='["python3","../examples/openai_agent_sidecar.py"]'
export SKILLX_TRAEX_ALLOWED_EXECUTABLES='["python3"]'
```

随后使用全新 Alembic 数据库启动 API、Worker、Vite、本地站点和 Chromium，执行
`NEXT_STEPS-020.md` 的六条最低语义输入与 020 完整验收流程。ready 必须显示
`selectionProvider.type=real`、`protocolAvailable=true`、`modelAvailable=true` 后，结果才可补记为真实语义证据。
