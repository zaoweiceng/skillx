# 设计与规范达成度审查报告（027）

审查日期：2026-08-11

依据：[`REPLAY-026.md`](../REPLAY/REPLAY-026.md)、
[`NEXT_STEPS-026.md`](../STEP/NEXT_STEPS-026.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`76f51f4`；审查对象为工作树中的 REPLAY-026 修复。本轮只新增 027 三份文档，不修改业务代码。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户或分布式部署。

## 1. 结论

**不通过。**

026 已实现内置 TraeX 自动发现、原生 JSONL/MCP bridge、Agent/LLM Runtime 身份记录、受管理 seed 升级，以及
结构化结果与 Presentation 的通用执行和展示基础。工程门禁全部通过：后端 309 项、前端 51 项、独立进程 E2E
37 项、浏览器 UI 2 项。手工启动全新 SQLite、API、Worker、Vite 后，组件执行、历史、RunDetail、对话开关、
计时、Token 状态、明暗主题和常用点击均正常，console 无 error/warning。

但核心流程在真实 TraeX 上仍无法运行。内置 bridge 把每轮隔离目录作为 TraeX 工作目录，却没有传递
`--skip-git-repo-check`。这些目录不是 Git 仓库，TraeX 因而在模型请求前稳定返回：

```text
Not inside a trusted directory and --skip-git-repo-check was not specified.
```

真实 Settings 探测与关闭工作流后的通用 Agent 均以 `AGENT_PROTOCOL_INCOMPATIBLE` 失败，分别没有产生模型
dispatch 或 Token。界面还把这个工作目录错误解释成“TraeX 版本不兼容”，不能给出正确恢复动作。即使修复该启动
参数，当前主机凭据实际请求又返回 401 token expired；`traex login status` 却仍报告 Logged in，登录状态只证明
本地存在凭据，不能证明 Runtime 可用。

另外，026 把天气中文模板直接写进通用平台的 seed。天气只能是一个真实验收用例，不能成为产品中的特殊分支。
当前固化链只生成能力目录，不会按工作流用途提出、审阅并冻结通用 Presentation policy，因此删除天气特例后，
“首次探索—固化—复用”的终端结果仍不会自然获得可读输出。正确目标是让固化阶段按受众、是否直接展示、是否作为
组合中间步骤等通用事实决定 Presentation，而不是识别“天气”领域。

## 2. REPLAY-026 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| 自动发现本机 TraeX | 通过 | Settings 识别 `traex_builtin`，版本 `traecli 0.200.19` |
| 原生 JSONL + 每轮 MCP bridge | 通过（代码与 fixture） | bridge 建立 loopback MCP、严格 schema、工具 allowlist 与 JSONL 映射 |
| 无 LLM Provider 时使用 Agent Runtime | **未通过** | 实际隔离目录被 TraeX 拒绝，探测和通用 Agent 均在 dispatch 前失败 |
| 工作流开启时由 Agent 选择并绑定参数 | **未通过** | Provider probe 失败，Chat 显示自动选择不可用，未进入语义选择 |
| 工作流关闭时全程使用通用 Agent | **未通过** | 路由正确进入 Agent，但约 2.0s 后以 `AGENT_PROTOCOL_INCOMPATIBLE` 终止 |
| 真实认证状态与恢复提示 | **未通过** | `login status` 显示已登录，真实请求为 401 token expired；UI 错误动作不准确 |
| 模型 dispatch / Token 审计 | **部分通过** | 本次失败正确记 0/不可用；但代码仍会对部分失败伪造 dispatched=true |
| 受管理 seed 原位升级 | 通过（门禁与代码） | migration 0023 增加 seed key/revision，未接管样例按不可变版本升级 |
| JSON + 可读结果基础设施 | 通过 | 真实 Run 同时显示规范 JSON 与 Presentation，刷新可恢复 |
| 通用化 Presentation 决策 | **未通过** | 天气模板硬编码在 seed；固化流程不生成 presentation policy |
| 组件执行、历史、RunDetail | 通过 | 实际 Worker 执行成功，组件页可回看输入、输出、状态和详情 |
| 对话历史、开关、计时、Token | 通过（失败态） | 开关可用，失败 Turn、约 2.0s、Token 不可用、checkpoint 可刷新恢复 |
| 亮暗主题、配色、布局与点击 | 通过 | 手工明暗主题及 `make browser-ui-check` 2/2；console 0 error/warning |
| 总发布门禁 | 通过 | `make demo-check`：309 backend、51 frontend、37 E2E 全部通过 |

## 3. 真实环境验收

### 3.1 自动化与独立进程

| 检查 | 结果 |
|---|---|
| 审查前 `python3 tools/check_doc_chain.py` | 通过；99 个版本化 Markdown |
| 后端 Ruff / Pytest | 通过；309 passed in 35.27s |
| Alembic 空库升级 | 通过；`0001 → 0023` |
| 前端 check / lint / Vitest / build | 通过；11 files / 51 passed，生产构建成功 |
| 独立 API/Worker/Vite/Chromium E2E | 通过；37 passed in 160.21s |
| `make browser-ui-check` | 通过；2 passed in 27.48s |

这些 E2E 会启动独立 API、Worker、Vite 和 Chromium，但其 Agent 协议部分使用受控测试 Runtime；因此它能证明
应用边界和回归门禁，不能代替下面的真实 TraeX 主流程。

### 3.2 实际 API、Worker、Vite 与浏览器

使用全新 migration 0023 SQLite，分别启动 API、独立 Worker 和 Vite：

- Settings 主动探测返回 `provider=traex`、`runtimeType=traex_builtin`、
  `protocol=traex-cli-jsonl+mcp`，但 `ok=false`、`AGENT_PROTOCOL_INCOMPATIBLE`、
  `requestDispatched=false`、Usage 不完整。
- Chat 默认开启工作流，因自动选择不可用而给出关闭工作流提示。关闭后发送“请只回复 027-agent-ok”，实际进入
  通用 Agent，约 2.0s 后以相同错误失败；数据库 Usage 的 selection probe 和 general agent 均为
  `dispatched=false/completed=false`。
- 直接从应用调用内置 adapter 捕获到明确根因：隔离工作目录不受 TraeX 信任且没有
  `--skip-git-repo-check`。这不是版本兼容性问题。
- 在 Git 仓库内直接运行原生 `traex exec` 能越过目录检查并到达服务端，但返回 401：access token 已于
  2026-08-03 08:58:44 UTC 过期。该环境问题需要用户重新登录，应用不得读取或保存 token。
- 手动运行本地天气组件，实际 Worker/MCP Run 成功，事件 seq 1..17、结构化结果和阅读结果均可见，0 Token。
  这只证明通用 Runtime/Presentation 展示链可工作；其中文模板来自天气专用 seed，不能作为通用设计通过证据。
- 组件页、运行历史、RunDetail、工作流编辑器的通用 Presentation 开关、模板/LLM producer 控件都可点击。
  明暗主题配色清晰，无白块、明显溢出或遮挡，浏览器 console 无 error/warning。

## 4. 通配型闭环能否完成

当前确定性 Workflow Runtime、目录渐进披露、执行历史、checkpoint、SSE、结构化结果与 Presentation 渲染已经形成
可复用骨架，但完整闭环仍不能完成：

1. 真实 Agent 在应用隔离目录启动失败，所以首次探索、语义选择、参数绑定和失败接力的智能节点都无法开始；
2. 登录状态不是活性证明，系统当前不能可靠区分“本地凭据存在”“真实认证有效”“协议可用”；
3. Presentation 是通用执行能力，却没有进入通用固化决策；当前仅靠手工编辑或天气 seed 特例启用；
4. 因而第二次复用可以执行已经人工准备好的确定性工作流，但不能证明系统能从任意领域的首次成功轨迹自动形成
   合适的“结构化事实 + 可选阅读结果”。

Presentation 的通用规则应从原始目的出发：规范 JSON 始终是事实源；只有结果直接面向用户且可读文本能提升决策
效率时才启用 Presentation；作为 Fragment、组合中间结果或机器消费结果时默认关闭。稳定、简单、可由字段无歧义
拼接的结果可用模板；复杂摘要才调用选定的 LLM/Agent Runtime。该判断应由固化 Agent 提议、说明依据并经人工审阅，
随后冻结进不可变 WorkflowVersion。天气、网页总结、文件整理等只是验证同一规则的不同样本，不得进入平台分支。

## 5. 最终判定

027 的工程门禁、确定性执行和前端 UI 验收通过，但真实 Agent 主路径因工作目录参数失败；认证活性、dispatch 审计
以及通用 Presentation 固化仍不满足设计目标。因此 027 **不通过**。修复范围和真实通过门槛见
[`NEXT_STEPS-027.md`](../STEP/NEXT_STEPS-027.md)。
