# 设计与规范达成度审查报告（025）

审查日期：2026-08-11

依据：[`REPLAY-024.md`](../REPLAY/REPLAY-024.md)、
[`NEXT_STEPS-024.md`](../STEP/NEXT_STEPS-024.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`fdc8945`。审查开始时工作树干净。本轮只新增 025 三份审查文档，不修改业务代码。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户或分布式部署。

## 1. 结论

**不通过。**

024 对执行链的修复大部分有效：Browser 预检已移出 Worker 主循环；公开 readiness 不再暴露绝对路径；
RunDetail 刷新能恢复完整持久事件；ModelUsage 已增加 attempted/dispatched/completed；天气能力目录元数据已补充。
正常主机权限下，真实独立进程 E2E **37/37 通过**，真实 Chromium UI 主流程 **2/2 通过**；手工启动的
API、Worker、Vite 和本地站点也能完成对话降级、组件执行、历史回看和编辑器操作。

但 024 仍未达到“完整流程真实跑通”的完成定义：

1. `make demo-check` 在文档链阶段退出 2。历史 `REPLAY/`、`STEP/` 链接没有随目录迁移重写；更根本的是
   校验器依赖 `review/`，而该目录被 `.gitignore` 排除且 Git 跟踪文件数为 0，干净检出必然失败。
2. 当前仍没有真实语义 Agent/LLM Provider，“上海天气如何”只能正确返回自动选择不可用，不能证明
   Agent 判断、同消息参数提取和工作流组合能力。
3. 真实语义探测只验证固定 ToolResult round-trip，且把通用 `agent.step.completed` 推断为模型已 dispatch，
   无法从协议脚本中可靠区分真实模型语义能力。
4. 持久事件刷新已修复，但运行中的 SSE 监听白名单漏掉 `mcp.call.completed`、`artifact.created` 等事件；
   手工天气 Run 在完成前缺少 seq 9/10，刷新后才补齐。

因此当前版本是“本地执行和 UI 主链可运行，但发布事实链与真实语义入口未完成”，不能直接说通过。

## 2. REPLAY-024 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| 文档目录与链接形成单一事实链 | **未通过** | `make demo-check` 报大量断链；历史文件未重写链接 |
| 文档门禁可在干净检出复现 | **未通过** | `git ls-files review` 为 0；`git archive HEAD` 后校验缺两份 024 review 及历史链接 |
| Browser 预检不阻塞 Worker | 通过 | 浏览器为 `permission_denied` 时，天气和混合组件仍在 302ms/短时内完成 |
| Browser readiness 安全投影 | 通过 | API 只返回 `chromiumExecutable` 和版本/状态，不再返回本机绝对路径 |
| 持久事件刷新回放 | 通过 | 天气 Run 刷新后恢复 seq 1..14，包括 MCP 和 Artifact 事件 |
| 运行中 SSE 事件连续 | **部分通过** | 首次完成页面缺 seq 9/10，刷新后才出现 |
| 模型调用 attempted/dispatched/completed | 部分通过 | 节点/Presentation 主路径已接入；工作流选择失败没有 Usage 记录 |
| 真实 Agent readiness 只表达可证明事实 | **未通过** | 探测成功条件不要求显式 dispatch；通用 Agent step 被当成 dispatch 证明 |
| 内置能力目录元数据完整 | **部分通过** | 天气完整；“反馈分类”等仍显示“适用/不适用：未补充” |
| 普通自然语言自动选择与绑定 | **未验证** | 当前无真实语义 Provider；fixture 被正确隔离 |
| 工作流默认开启、关闭后全程 Agent | 通过 | 手工 Chat 验证 unavailable → 一键关闭 → Agent 成功回复 |
| 对话历史、计时、Token | 通过 | 历史恢复；不可用为 0.0s/Token 不可用；Agent 为 0.2s/20 Token |
| 组件手动执行及调用历史 | 通过 | 天气 `city=广州` 成功；组件页展示输入、输出、302ms 和 Run 链接 |
| 结构化结果与阅读结果 | 通过（既有链路） | 浏览器 E2E 覆盖 JSON、Presentation 和第二次复用 |
| 亮暗主题、响应式和主要交互 | 通过 | 手工亮暗主题可读；浏览器门禁覆盖五档宽度和 console |
| 总发布门禁 | **未通过** | `make demo-check` 未进入其内置进程 E2E 阶段 |

## 3. 真实环境验收

### 3.1 自动化与独立进程

| 检查 | 结果 |
|---|---|
| 后端 Ruff / Pytest | 通过；300 passed |
| Alembic 空库升级 | 通过；`0001→0021` |
| 前端 check / lint / Vitest / build | 通过；11 files / 51 passed，生产构建成功 |
| `make demo-check` | **失败**；文档链阶段退出 2 |
| 正常主机独立进程 E2E | 通过；37 passed in 144.87s |
| 正常主机 `make browser-ui-check` | 通过；2 passed in 27.45s |
| 干净提交归档文档检查 | **失败**；`review/` 不在 Git，且历史链接仍按旧根目录解析 |

独立进程 E2E 和浏览器门禁实际启动全新 SQLite、Alembic、API、Worker、本地 HTTP 站点、Vite 和 Chromium；
覆盖首次探索、固化、发布、二次复用、失败接力、Presentation、刷新恢复、终态 SSE、主题、响应式和 console。

### 3.2 手工真实进程与浏览器

使用全新 0021 数据库，启动 API `8055`、独立 Worker、本地站点 `8955` 和 Vite `5155`：

- Settings 亮暗主题均清晰；受限 Worker 正确显示 Browser=`permission_denied`，Worker 仍为 available。
- Chat 默认开启工作流；“上海天气如何”稳定返回 `WORKFLOW_SELECTION_UNAVAILABLE`，没有用 fixture 假装理解。
  一键关闭工作流后发送普通问题，Agent 成功完成，记录 0.2s 和 20 Token。
- 手动天气组件输入广州，得到广州、小雨、31℃；零模型审计为 `modelCalled=false`、0 Token。
- 首次打开天气 Run 时事件序列从 8 跳到 11；API 中 seq 9/10 分别是 `mcp.call.completed` 和
  `artifact.created`。刷新后页面恢复完整 seq 1..14，证明 DB 回放正确但 SSE 实时订阅不完整。
- 组件详情显示 1 条调用历史、输入输出、耗时、Presentation 状态和运行链接。
- “反馈分类”可运行并展示 attempted/dispatched/completed；但目录元数据仍显示“适用/不适用：未补充”。
- 工作流编辑器的保存、静态检查、JSON 视图、发布/运行入口均可点击，静态检查通过。

### 3.3 前端视觉

Settings、Chat、组件和 RunDetail 的亮暗主题、文字层级、边框和状态色正常，没有暗色白块、遮挡或不可点击控件。
正常主机浏览器门禁覆盖 760、903、1024、1280、1440px，并断言 console 0 error/warning。

工作流编辑器整体可用且表单主题统一，但在可编辑画布下方又渲染一个无标题、420px 高的只读总览图；它与上方画布
内容重复，使长页面明显冗余。“小图便于总览”的实现尺寸与目的不一致，属于视觉/信息架构收尾项，不是主流程阻塞。

## 4. 预期功能能否完成

在 fixture 和正常主机 Chromium 条件下，通配型“首次探索—固化—复用—失败接力—修订”的技术骨架可以跑通；
对话开关、组件历史、结构化/阅读结果和运行审计也基本可用。

但面向真实用户的关键入口仍不能验收：当前没有真实语义 Runtime，且 readiness 探测本身只证明固定协议回环，
没有充分证明实际模型语义能力。继续增加 fixture 规则或固定探测答案不能解决本质问题。最短正确路径是提供可信的
真实 Agent adapter/Provider，补齐所有内置能力目录，再用普通自然语言最低集和真实 dispatch/Usage 事实验收。

发布层面必须先让所有审计输入成为 Git 中的可重复事实；依赖开发机上被忽略的 `review/` 文件，即使当前机器偶然
通过，也不能称为发布门禁。

## 5. 最终判定

024 的执行、回放和 UI 改造多数有效，但总门禁不可复现、真实语义未验收、实时事件和审计仍有缺口。
因此 025 **不通过**。下一步任务与通过门槛见
[`NEXT_STEPS-025.md`](../STEP/NEXT_STEPS-025.md)。
