# 设计与规范达成度审查报告（024）

审查日期：2026-08-10

依据：[`REPLAY-023.md`](../REPLAY/REPLAY-023.md)、
[`NEXT_STEPS-023.md`](../STEP/NEXT_STEPS-023.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`7b8906c`。审查开始时工作树干净。本轮只新增 024 三份审查文档，不修改业务代码。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户或分布式部署。

## 1. 结论

**不通过。**

023 已实质修复上一轮的大部分产品问题：fixture 不再冒充真实语义 Runtime；工作流开启但语义 Runtime
不可用时会稳定降级；Browser readiness 来自 Worker 实际启动预检；零 LLM Run 的 Runtime/Token 展示正确；
终态 SSE 不再重连；编辑器亮暗主题、表单尺寸和交互状态明显改善。正常主机权限下，独立 API、Worker、SQLite、
本地站点、Vite 和 Chromium 的全流程 E2E 为 **37/37 通过**，浏览器主流程为 **2/2 通过**。

但不能声明整体通过，原因有三类：

1. `make demo-check` 被文档链门禁阻断。023 将文档迁移到 `REPLAY/`、`STEP/`，校验器却仍只查根目录，
   且迁移后的历史文档链接没有重写；`REPLAY-023` 声称“86 份检查通过”无法在当前提交复现。
2. 当前没有可用的真实语义 Agent/LLM Runtime，所以“上海天气如何”及七条最低语义集只能得到明确的
   `WORKFLOW_SELECTION_UNAVAILABLE`，无法验证一次消息内选择、参数提取和自然语言整理。
3. 终态 RunDetail 刷新后没有读取已经持久化的事件，只显示“本会话未加载历史事件”；这与设计中的
   “DB 为事件事实源、历史运行可审计”不一致。

因此当前版本是“协议和执行骨架可跑通、真实语义入口尚不可验收、发布总门禁不通过”，不是完整可交付状态。

## 2. REPLAY-023 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| fixture 仅用于协议测试 | 通过 | readiness 返回 `protocolTestOnly=true`；产品自动选择不使用 fixture |
| 无真实语义 Runtime 时安全降级 | 通过 | Chat 默认开启工作流并明确提示不可用；可一键关闭后继续通用 Agent |
| 工作流开关默认开启、关闭后全程 Agent | 通过 | 手工浏览器中关闭后发送普通消息，Agent 成功回复且无 workflow 选择 |
| 真实语义选择和同消息参数绑定 | **未验证** | `SKILLX_AGENT_*`、`SKILLX_LLM_*` 及外部 Provider 凭据均未配置 |
| Browser Worker 实际 readiness | 通过 | 受限进程报告 `permission_denied`；正常主机浏览器门禁报告 `available` |
| Runtime 选择与实际调用拆分 | 通过 | 手工天气 Run：`runtimeUsed=null`、`modelCalled=false`、0 Token |
| 计时和 Token 展示 | 通过 | 不可用回复显示 0s/Token 不可用；Agent 回复显示 0.5s/20 Token |
| 终态 SSE 关闭 | 通过 | 浏览器门禁验证终态无重连；页面显示“实时流已关闭” |
| 持久化运行事件可回看 | **未通过** | 手工 Run 成功并有事件；刷新 RunDetail 后事件区为空 |
| 组件手动调用与历史 | 通过 | 天气组件以 `city=广州` 成功，组件详情显示输入、输出、耗时和运行链接 |
| JSON 与自然语言展示 | 通过（既有链路） | 浏览器 E2E 覆盖 Presentation、结构化结果与阅读结果 |
| 亮暗主题与视觉收尾 | 通过 | Settings、Chat、Workflow Editor 手工切换/刷新；控件可读、可点击且主题一致 |
| 文档审计链 | **未通过** | `python3 tools/check_doc_chain.py` 报根目录文档缺失和大量断链 |
| 总发布门禁 | **未通过** | `make demo-check` 在文档链阶段退出 2，未进入其内置 E2E 阶段 |

## 3. 真实环境验收

### 3.1 自动化与独立进程

| 检查 | 结果 |
|---|---|
| 后端 Ruff / Pytest | 通过；295 passed |
| Alembic 空库升级 | 通过；`0001→0020` |
| 前端 check / lint / Vitest / build | 通过；11 files / 50 passed，生产构建成功 |
| `make demo-check` | **失败**；文档链报错后退出 2 |
| 受限环境独立进程 E2E | 30 passed / 7 failed；7 个均为 Chromium OS 权限拒绝 |
| 正常主机权限独立进程 E2E | 通过；37 passed in 146.58s |
| 正常主机 `make browser-ui-check` | 通过；2 passed in 28.13s |

正常主机 E2E 与浏览器门禁实际启动独立 API、Worker、SQLite、本地 HTTP 站点、Vite 和 Chromium，
不是用函数或单测替代进程。它覆盖首次探索、固化、发布、二次复用、失败接力、组件调用、Presentation、
刷新恢复、终态 SSE、响应式宽度、主题和 console。

### 3.2 手工浏览器验收

使用全新 0020 数据库，实际启动 API `8054`、独立 Worker、本地表单站点 `8954` 和 Vite `5154`：

- Settings 在亮暗主题下信息层级清楚；受限 Worker 的 Browser 卡片正确显示 `permission_denied`，不再误报可用。
- Chat 默认开启工作流。发送“上海天气如何”得到稳定的自动选择不可用说明，不虚构已经调用工作流；点击
  “关闭工作流，下一条交给通用 Agent”后发送普通问题，fixture Agent 正常回复并记录 20 Token。
- 手动执行天气组件 `city=广州`，得到广州、小雨、31℃；节点、Artifact、结构化输出和零模型审计均正确。
- 组件详情加载 1 条调用历史，能查看输入、输出、耗时、Presentation 状态和运行详情链接。
- 工作流编辑器的能力摘要、Presentation 开关、LLM 选择、保存和静态检查均可点击；静态检查通过。
- 直接刷新已完成 Run 后，节点和输出仍在，但“PERSISTED EVENT HISTORY”为空。后端已有持久事件 API，
  前端没有在页面挂载时取回，这是功能缺口，不是测试环境问题。

### 3.3 配色与样式

亮色和暗色主题的背景、文字、边框、强调色与错误状态对比正常；Chat、Settings、组件页和工作流编辑器
没有发现遮挡、不可点击或暗色白块。工作流编辑器的输入框、select、checkbox 已比 023 前统一，页面视觉达到
Local Demo 可用水平。正常主机浏览器门禁同时验证 760、903、1024、1280、1440px 和 console 0 error/warning。

视觉本身不再是发布阻塞项。当前阻塞来自事实链和真实 Runtime，而非“再调一点 CSS”。

## 4. 预期功能能否完成

通配型“首次探索—固化—复用—失败接力—修订”框架、渐进披露、局部/组合执行、组件历史和双格式结果的
执行骨架，在正常主机权限下已经可以跑通。当前仍不能证明面向用户的完整目标，因为最关键的“由真实 Agent
理解普通自然语言、选择工作流并绑定参数”没有可用 Runtime，也没有真实语义测试证据。

最短路径不是给 fixture 增加天气或城市正则，而是接入一个与 Host 双向协议兼容的 Agent Runtime；在此之前，
现有明确 unavailable 的行为是正确降级，应保留。与此同时必须修正文档迁移和运行事件回放，否则即使语义 Runtime
接通，也仍不满足可重复发布与可审计要求。

## 5. 最终判定

023 的主要修复方向正确，真实进程链路和前端交互大部分通过；但 `make demo-check` 不通过、历史事件不可回看、
真实语义最低集未执行。因此 024 **不通过**。下一轮任务与通过门槛见
[`NEXT_STEPS-024.md`](../STEP/NEXT_STEPS-024.md)。
