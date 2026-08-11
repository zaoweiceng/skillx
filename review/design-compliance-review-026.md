# 设计与规范达成度审查报告（026）

审查日期：2026-08-11

依据：[`REPLAY-025.md`](../REPLAY/REPLAY-025.md)、
[`NEXT_STEPS-025.md`](../STEP/NEXT_STEPS-025.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`76f51f4`。审查开始时工作树干净。本轮只新增 026 三份审查文档，不修改业务代码。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户或分布式部署。

## 1. 结论

**不通过。**

025 已关闭上一轮的大部分工程问题：提交与干净归档的文档链均可通过；主动探测要求受信 adapter、随机
challenge、显式 dispatch、Host ToolResult round-trip、严格输出与完整 Usage；工作流选择失败也会持久化
Usage；内置新建工作流具备完整目录元数据；通用 SSE 可在首次页面看到 MCP 与 Artifact 事件；编辑器重复图已删除。

真实主机门禁全部通过：后端 303 项、前端 51 项、独立进程 E2E 37 项、浏览器 UI 2 项。手工启动全新
SQLite、API、Worker 和 Vite 后，明暗主题、工作流编辑器、组件执行、调用历史、RunDetail、对话历史刷新、
工作流开关、计时与 Token 状态均可正常使用，浏览器 console 无 error/warning。

但产品核心入口仍未达到用户要求：**没有独立 LLM/语义 Provider 时，应使用宿主 Agent（TraeX）完成语义判断和
结构化生成。当前应用不会自动发现本机 TraeX，也没有把原生 TraeX 协议接到 AgentHost。** 实测本机存在
`traex`，设置页仍显示 Agent Runtime `unconfigured`；工作流关闭后通用 Agent 以 `MODEL_NOT_CONFIGURED`
终止，工作流开启时自动选择直接不可用。当前 TraeX 登录令牌也已过期，最小真实调用返回 401；即使重新登录，
现有应用仍缺少协议 bridge，不能自动使用它。

此外，作为终端用户典型场景的天气工作流没有启用 Presentation，手动运行只显示 JSON；025 对 seed 定义的
目录修复也只作用于全新数据库，既有 Local Demo 不会获得新版定义。故不能以自动化门禁通过替代完整产品流程通过。

## 2. REPLAY-025 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| 正式报告进入 Git，历史链接修复 | 通过 | `git ls-files review` 有 48 个文件；HEAD 归档内文档链 95/95 通过 |
| 总发布门禁 | 通过 | `make demo-check`：303 backend、51 frontend、37 E2E 全部通过 |
| 可信主动探测 | 通过（代码/fixture） | 要求 adapter digest、随机 challenge、显式 dispatch、真实 Usage、ToolResult round-trip |
| 工作流选择 Usage 成功/失败统一 | 通过 | 成功和异常路径均写 attempted/dispatched/completed、Token 与 error code |
| 目录元数据发布门禁 | 通过（新建/新发布） | summary/useWhen/doNotUseWhen/examples/tags 缺失会拒绝发布 |
| 既有 Demo 获得 025 seed 修复 | **未通过** | `seed()` 对已有 Component 全部跳过，没有数据升级或新版本发布 |
| 通用 SSE 与持久事件一致 | 通过 | 手工天气 Run 首次页面即出现 seq 1..14，含 `mcp.call.completed`、`artifact.created` |
| 编辑器重复 DAG 删除 | 通过 | 页面只保留可编辑画布，契约、画布、发布历史信息流连续 |
| 无 LLM Provider 时用 TraeX Agent fallback | **未通过** | 本机有 `traex`，应用仍显示 Agent Runtime unconfigured；通用对话失败 |
| 普通自然语言自动选择与参数绑定 | **未通过** | 开启状态显示 selection unavailable，未进入 TraeX 语义选择 |
| JSON + 面向用户阅读结果 | **部分通过** | 基础设施存在；天气 seed 未启用 Presentation，真实运行只有 JSON |
| 组件执行与调用历史 | 通过 | 问候 Run 成功，返回组件页显示状态、耗时、输入、输出与详情链接 |
| 对话历史、开关、计时、Token | 通过（失败态） | 默认开启、可关闭；0.4s、Token 不可用和失败 lineage 可刷新恢复 |
| 亮暗主题、配色、布局与点击 | 通过 | 手工检查及 `make browser-ui-check` 2/2；console 0 error/warning |

## 3. 真实环境验收

### 3.1 自动化与独立进程

| 检查 | 结果 |
|---|---|
| `python3 tools/check_doc_chain.py` | 通过；95 个版本化 Markdown |
| HEAD 干净归档文档检查 | 通过；95 个版本化 Markdown |
| 后端 Ruff / Pytest | 通过；303 passed in 34.11s |
| Alembic 空库升级 | 通过；`0001 → 0022` |
| 前端 check / lint / Vitest / build | 通过；11 files / 51 passed，生产构建成功 |
| 独立 API/Worker/Vite/Chromium E2E | 通过；37 passed in 150.84s |
| `make browser-ui-check` | 通过；2 passed in 27.30s |

这些 E2E 实际启动全新数据库、API、独立 Worker、Vite、Chromium 和本地测试站点，不是新增单测冒烟模拟。

### 3.2 手工真实前后端与浏览器

使用全新 0022 SQLite，分别启动 API、独立 Worker 和 Vite：

- 亮色与暗色组件目录均清晰，背景、卡片、文字、状态色和焦点边框协调；主题切换立即生效。
- 工作流编辑器的元数据输入框在暗色下不再出现白块，保存、静态检查、JSON、发布和手动运行入口可见可点；
  页面只有一个 DAG。
- “确定性问候”输入 `026验收` 成功，Run 显示节点、事件、JSON 和 0 Token；返回组件页可见 1 条调用历史。
- 天气工作流输入 `上海` 成功，首次 Run 页面完整显示 seq 1..14、MCP、Artifact 和结构化结果
  `{city: 上海, condition: 多云, temperature: 19}`，但没有“阅读结果”。
- Chat 默认工作流开启；因 Agent Runtime 未配置，自动选择不可用。关闭后发送普通问题，Turn 进入真实 Agent 路径，
  但以 `MODEL_NOT_CONFIGURED` 结束；0.4s、Token 不可用、checkpoint 和失败 lineage 均持久化，刷新后仍存在。
- Settings 正确显示 SQLite、Worker 可用，Agent/LLM/Browser Runtime 未配置；console 无 error/warning。

### 3.3 TraeX fallback 实测

本机存在 `/Users/example/.local/bin/traex`，版本为 `traecli 0.200.19`。该 CLI 提供
`exec --json`、`--output-schema`、ACP、app-server 和 MCP server 能力，说明无需额外 LLM endpoint、直接使用
宿主 Agent 的方向可行。

当前项目没有使用这些原生入口。`AgentHost` 只接受显式 `SKILLX_AGENT_COMMAND`；现有
`TraeXSubprocessAdapter` 会追加 `--prompt/--max-steps/--max-tokens/--allowed-tools` 等自定义参数，原生
`traex exec` 并不实现这套 wrapper 协议。因此不能只把 command 填成 `traex`。

最小 `traex exec --ephemeral --json --sandbox read-only` 真实调用已到达 TraeX 后端，但因本机 access token
于 2026-08-03 过期而返回 401。该环境问题需要重新登录；它与应用缺少 bridge 是两个独立问题。

## 4. 预期功能能否完成

确定性 Workflow Runtime、持久队列、SSE、历史、失败接力、目录披露、编辑器和 UI 技术骨架已经能够运行。
025 的工程修复使提交本身也可复现。

完整目标仍不能完成，根因不是“必须再配置一个 endpoint”，而是运行时边界尚未闭合：

1. Direct LLM Resolver 已写出 `LLM Runtime → Agent Runtime` 的启动前 fallback 逻辑；
2. Agent Runtime 却只能消费项目自定义 sidecar 协议，不能消费本机原生 TraeX；
3. 因此没有 LLM endpoint 时，两条 Runtime 实际都不可用；
4. 工作流选择本身也依赖同一个 AgentHost，故既不能理解意图，也不能提取当前消息中的参数。

最短正确路径是交付仓库维护、内容 digest 受信的 TraeX bridge：自动发现本机 TraeX，使用其非交互结构化输出
能力，并通过 ACP/app-server/MCP 或等价双向通道把 `workflow.*` Tool 请求交回 Host。不能把任意 `traex` 可执行
文件直接视为可信，也不能用静态关键词匹配代替 Agent 判断。

天气场景应启用零 LLM 模板 Presentation，例如“上海当前多云，19℃”，保留原 JSON 事实源；内部 Fragment 继续
默认关闭。seed 修复必须以新的不可变 WorkflowVersion 升级既有系统样例，不能覆盖用户修改。

## 5. 最终判定

026 的确定性执行、工程门禁和 UI 验收通过，但“无独立语义 Provider 时使用 TraeX Agent 完成理解/选择/生成”的
核心要求未实现，天气双格式结果和既有 Demo 升级也未完成。因此 026 **不通过**。修复范围和真实通过门槛见
[`NEXT_STEPS-026.md`](../STEP/NEXT_STEPS-026.md)。
