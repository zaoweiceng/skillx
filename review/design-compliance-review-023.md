# 设计与规范达成度审查报告（023）

审查日期：2026-08-10

依据：[`REPLAY-022.md`](../REPLAY/REPLAY-022.md)、[`NEXT_STEPS-022.md`](../STEP/NEXT_STEPS-022.md)、
[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md) 和 [`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`9f9fb43`。工作树在审查开始时干净。本轮只新增 023 三份审查文档，没有修改业务代码。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户或分布式部署。

## 1. 结论

**不通过。**

022 的核心 Runtime 改造已经落地：Agent Runtime 与 LLM Runtime 配置、readiness 和 UI 已拆分；非 fake LLM 节点与
LLM Presentation 在 LLM 未配置时可共用受约束的 Agent fallback；fallback 无 Tool、单步、严格 Schema，并记录
Runtime digest、选择原因和 Token；Direct LLM 请求开始后的 401 不会改走 Agent；技术错误信封与终态 SSE 问题也已关闭。

但当前 Local Demo 的默认可运行路径仍不能完成用户最基本的自然语言调用：真实输入“上海天气如何”仍先追问 `city`，
没有从同一条消息提取“上海”。同时，Settings 把仅配置 allowlist 的 Browser Runtime 显示为 `configured`，实际 Chromium
可能因 OS 权限完全无法启动。外部真实语义 Agent/LLM 未配置，因此 fixture 的确定性输出不能证明 Agent 判断、参数提取和
自然语言整理质量。首要目标“整个流程在真实使用方式下一次跑通”尚未达到。

## 2. REPLAY-022 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| Agent / LLM Runtime 配置拆分 | 通过 | `/health/ready` 独立返回 `agentRuntime`、`llmRuntime` 和 fallback policy；Settings 有独立卡片 |
| TraeX managed 不强制 Host 持有 LLM endpoint | 通过 | `agent_runtime_config()` 仅对 `openai_sidecar` 要求 endpoint/model/key |
| 统一结构化生成 Resolver | 通过 | LLM 节点和 LLM Presentation 均使用 `StructuredGenerationResolver` |
| LLM 未配置时 Agent fallback | 通过（协议级） | Run `a986bfbb-...` 的节点与 Presentation 均为 `agent_fallback`，各 20 Token |
| fallback 无 Tool、单步、严格 Schema | 通过 | 真实 API/Worker/Agent 子进程 E2E；节点 `toolRequestCount=0` |
| Direct LLM 执行错误不切换 Runtime | 通过 | 进程级 HTTP Provider 200/401 矩阵；401 保持 `direct_llm` 并失败为 `MODEL_AUTH_FAILED` |
| Runtime / Usage 审计 | 部分通过 | 模型路径证据完整；零 LLM 天气 Run 仍被 UI 标成 `Agent fallback`，与实际使用不符 |
| 技术错误信封幂等、去重、整体限长 | 通过 | 版本化 envelope、digest 去重和 `<6000` 回归均通过 |
| 终态 SSE 不重连 | 通过 | Hook 在未知/终态不建流，终态事件主动关闭；真实浏览器等待 5 秒请求数不增长 |
| 亮暗主题、历史对话、工作流开关 | 通过 | 手工浏览器切换并刷新保持；历史会话可恢复；关闭后无 workflow 选择 |
| 组件调用历史和运行详情 | 通过 | 手动天气 Run `2e6368d1-...` 成功，组件页回写 1 条输入/输出/耗时历史 |
| JSON + 自然语言阅读结果 | 通过（协议级） | Run `a986bfbb-...` 同时展示结构化结果、LLM Presentation、复制按钮和两条 Usage |
| 自然语言工作流选择与参数提取 | **未通过** | “上海天气如何”选择天气工作流后仍返回“请补充输入：city” |
| Browser Runtime 真实 readiness | **未通过** | Settings 显示 `configured`，同一环境 Chromium 启动实际报 MachPort permission denied |
| 外部真实 Agent/LLM 最低语义集 | **未验证** | endpoint/model/key/OpenAI/AceDataCloud 变量均未配置 |
| 版本化修复文档可追溯 | **退化** | 提交 `9f9fb43` 删除 39 份历史根目录 REPLAY/NEXT_STEPS 文档 |

## 3. 真实环境验收

### 3.1 自动化与进程门禁

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| 后端 Ruff / Pytest | 通过；290 passed |
| Alembic | `0001→0019` 通过 |
| 前端 TypeScript / ESLint / build | 通过 |
| 前端 Vitest | 11 files / 49 passed |
| 独立进程 E2E | 37 passed in 143.58s |
| `make browser-ui-check`（受限沙箱） | 2 failed；Chromium MachPort `Permission denied (1100)` |
| `make browser-ui-check`（正常主机权限） | 2 passed in 26.60s |

浏览器门禁实际启动 API、独立 Worker、SQLite、本地 HTTP 站点、Vite 和 Chromium，覆盖探索、固化、二次复用、失败接力、
响应式宽度、主题、console 和终态 SSE；不是函数级或单测冒烟模拟。

### 3.2 手工真实进程栈

使用全新迁移 0019 数据库，实际启动 API `8053`、独立 Worker、本地表单/文章站点 `8953`、Vite `5153`，
再用应用内 Chromium 操作：

- Settings 亮暗主题均可读，刷新后主题保持；Agent Runtime=`fixture`、LLM Runtime=`unconfigured`、路径=`Agent fallback`。
- 对话默认开启工作流；“上海天气如何”成功完成 L0/L1 披露，但错误追问 `city`。回复“上海”后恢复原 selection，
  服务端展示 `city ← “上海” · 100% · serverVerified (exact)`，天气 Run 成功。
- 新建对话并关闭工作流后，下一 Turn 全程走 Agent，显示 0.4s、20 Token 和 Agent 轨迹。
- 手动运行“本地天气查询”得到北京小雨 21℃；RunDetail 有三个节点、事件、MCP Artifact，组件详情回写调用历史。
- 将“反馈分类”发布为非 fake LLM 节点并启用 LLM Presentation。LLM 未配置时，节点和 Presentation 都由独立 Agent
  子进程完成，Runtime/Usage/Tool 数证据正确；页面同时显示 JSON 与阅读结果。
- 工作流列表、编辑器、静态检查/发布入口、组件筛选、历史链接、运行详情、复制 JSON/文本均可操作。

### 3.3 视觉结论

整体信息架构、亮暗主题、状态色和卡片层级可读，未再出现截图中的暗色白块问题；自动对比度测试 7 项通过。
但工作流编辑器的目录元数据输入框缺少统一高度和内边距，亮色下近似细横线；checkbox/select 仍有明显原生控件风格，
与其余界面不一致。终态 RunDetail 不建立 SSE 是正确的，但仍显示脉冲 “SSE / 等待新事件”，文案与真实状态相反。

## 4. 预期功能能否完成

框架层面的“首次探索—固化—复用—失败接力—修订”和“渐进披露—选择—局部/组合执行”已有完整执行骨架，自动化与
正常权限下浏览器链路可跑通。当前不能据此认定用户需求已完成，原因不是缺少更多规则，而是默认 Local Demo 使用一个
明确不具备自然语言理解能力的 fixture，却把自然语言参数提取交给它。给城市写正则只会修补示例，无法覆盖通配型工作流。

最短正确路径是：将 fixture 限定为协议测试；Local Demo 的工作流开启模式必须连接一个真实语义 Agent Runtime，或者明确
显示“语义 Runtime 不可用”并阻止声称自动选择成功。随后用无测试指令的自然语言集验证一次消息内完成选择和参数绑定。

## 5. 最终判定

022 的 Runtime 拆分和 fallback 代码目标已经大体完成，正常主机权限下完整技术链路可运行；但真实用户首条输入仍不能一次
完成参数绑定，Browser readiness 也会给出假阳性，且没有真实模型语义证据。因此 023 **不通过**。修复和复验任务见
[`NEXT_STEPS-023.md`](../STEP/NEXT_STEPS-023.md)。
