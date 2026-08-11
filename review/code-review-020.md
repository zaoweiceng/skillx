# 代码审查报告（020）

审查日期：2026-08-10

代码基线：`f87ed67`；审查对象为当前未提交工作树。范围覆盖 `REPLAY-019.md`、019 新增的对话/历史/
Presentation/Usage/主题实现及其与既有运行时的连接。Local Demo 不考虑鉴权。

## 1. 结论

**不通过：3 个 P1、4 个 P2。**

底层迁移、队列、运行历史、JSON 结果和模式 fencing 的实现总体完整；问题集中在模型信任边界、Chat 终态一致性、
真实 Agent 可运行性以及前端交互/样式。以下 finding 均可在当前代码或真实页面中复现。

## 2. Findings

### P1-1：服务端没有校验 binding 来源，模型可提交无证据参数直接执行

位置：[`plans.py:55`](../backend/app/application/plans.py)、
[`routes.py:4305`](../backend/app/api/routes.py)。

`WorkflowSelection.binding_sources` 默认为空，模型校验器没有要求每个 `bindings` 字段都对应一条来源。
`_prepare_selection()` 只做 JSON Schema 校验，并把 `uncertain=true` 的来源转成追问；当来源列表为空或模型把伪造来源标为
确定时，只要值符合 Schema 就会进入 Run。

服务端也没有验证：

- `source=user_message/history_message` 的 `messageId` 是否属于当前 Conversation；
- `quote` 是否真实出现在对应消息；
- `tool_result` 的 call/path 是否属于当前 disclosure session；
- 来源字段是否与 binding 字段一一对应；
- 标称“已校验”的规范化值是否可以从引文确定性得到。

影响：错误或被提示注入影响的 Agent 可以凭空构造 `city`、业务 ID、URL 等参数，绕过 019 明确要求的“模型负责理解，
服务端负责约束与校验”。这是执行信任边界问题，不是展示缺陷。

修复方向：把来源校验所需的 Conversation/user message/tool evidence 显式传给 `_prepare_selection()`；对每个 binding 要求
恰好一个或明确的多来源证据，并在服务端解析/比对。任何缺失、越权或无法验证的来源必须转 `ask_user`，不得执行。

### P1-2：Chat 在 Agent/Workflow 终态不刷新 Turn，计时、Token 和 Presentation 会停在旧状态

位置：[`ChatPage.tsx:195`](../frontend/src/pages/ChatPage.tsx)、
[`ChatPage.tsx:259`](../frontend/src/pages/ChatPage.tsx)、
[`ChatPage.tsx:360`](../frontend/src/pages/ChatPage.tsx)、
[`product_history.py:138`](../backend/app/application/product_history.py)。

Agent polling 仅在 failed 且有 checkpoint 时调用 `refresh()`；succeeded 后停止。Workflow polling 只看
Run 的 queued/running，一旦 Run succeeded 就停止，不关心 Presentation 是否仍为 running，也不刷新 ConversationTurn。

同时 `acceptedAt` 直接使用 SQLite 取回值的 `isoformat()`。SQLite 会丢失 timezone，前端 `new Date(acceptedAt)` 把无
offset 字符串当本地时间，导致 Asia/Shanghai 下运行中计时多出约 8 小时。`usage_summary()` 在尚无账本行时直接返回
`zero`，也无法区分“确定零 LLM”和“模型调用仍在统计”。

真实页面已复现：轨迹 succeeded/20 Token 时消息显示 `28802.5s / 0 Token`，reload 后才成为 `0.1s / 20 Token`。
同一状态机还会让 Run 成功后异步完成的 Presentation 不能自动出现在 Chat。

修复方向：以 Turn/Presentation 为终态所有者；Agent/Run 进入终态时至少再刷新 Conversation，且 Presentation 启用时持续
订阅到 `completed|failed`。API 时间统一输出带 `Z`/offset 的 RFC 3339；Usage 增加 processing/pending 语义，只有确认
Turn 全程没有模型调用时才返回 zero。优先使用一个 Turn 级 SSE/事件聚合，避免三套轮询互相丢状态。

### P1-3：当前仓库没有可用于产品验收的真实 Agent command

位置：[`traex_stub.py`](../examples/traex_stub.py)、
[`SettingsPage.tsx:43`](../frontend/src/pages/SettingsPage.tsx)。

真实 stack 和浏览器 E2E 都把 `SKILLX_TRAEX_COMMAND` 指向 deterministic fixture。它的渐进选择只识别 URL 和 `qN`，
所以“上海天气如何”稳定进入 ask_user。本机 `traex exec --json` 又不实现当前 `TraeXSubprocessAdapter` 所需的 stdin
ToolResult 双向回送协议。

影响：代码可以证明“如果 Agent 返回合法 WorkflowSelection，Host 会执行”，但不能证明需求本身——Agent 能理解普通用户
输入、决定是否调用并传入合适参数。当前 Local Demo 的首要用户路径实际失败。

修复方向：提供一个兼容 Host 协议的真实 provider adapter/sidecar，并将 fixture 明确限制为协议测试。ready API 应分别
报告“协议 fixture 可用”和“真实语义 provider 可用”，真实验收只接受后者。

### P2-1：组件分类按钮是无行为的装饰控件

位置：[`ComponentsPage.tsx:35`](../frontend/src/pages/ComponentsPage.tsx)。

四个按钮没有 state、`onClick` 或服务端过滤参数，列表始终对 `components` 全量 `map()`。真实点击“零 LLM”后仍显示
AGENT、HYBRID 和 ZERO LLM 共 8 个组件。

修复方向：实现 executionClass 过滤并给选中项 `aria-pressed`；组件量小时可本地过滤，若后续分页则应把筛选条件加入 API
游标契约。若不准备实现，应删除这些按钮，避免制造虚假功能。

### P2-2：编辑器元数据复用了错误的通用布局规则

位置：[`WorkflowEditorPage.tsx:524`](../frontend/src/pages/WorkflowEditorPage.tsx)、
[`index.css:1062`](../frontend/src/index.css)、[`index.css:1605`](../frontend/src/index.css)。

`CapabilityMetadataEditor` 使用 `panel source-panel`，继承为源码编辑器设计的 `min-height: 680px; display:flex`，造成巨大空白。
`.inspector-fields > div { grid-template-columns: 100px 1fr }` 又无差别命中 `.presentation-policy-editor`，把整个表单当成
`dt/dd` 行，导致复选框、标签和 select 严重挤压换行。

修复方向：按语义拆分 source editor、metadata form、definition-list 三类 class；禁止用宽泛的直接子元素选择器决定复杂组件
布局。至少在桌面 1440/1920 宽度和窄屏下做截图/交互回归。

### P2-3：亮色侧栏对比度不合格，系统状态卡还是硬编码假状态

位置：[`index.css:206`](../frontend/src/index.css)、
[`SettingsPage.tsx:5`](../frontend/src/pages/SettingsPage.tsx)。

`.runtime-status` 固定背景 `#101b21`，亮色主题标题继承 `#172126`，对比度约 1.07:1；弱文本 `#5d6b70` 约
3.17:1，均不满足普通文字 WCAG AA。系统页除了 selection mode 外，Worker、TraeX、Browser、Secret 状态全部来自静态数组；
本轮 Worker/TraeX fixture 正在运行时仍显示“需启动进程/未配置”。

修复方向：runtime 卡全部使用语义 token，并以 ready/health 返回的真实 capability 状态渲染；无法检测的项明确写“未检测”，
不要把设计占位文案展示成运行事实。

### P2-4：浏览器 E2E 的文本定位没有跟随双层结果 UI 更新

位置：[`test_ui_main_flow.py:130`](../backend/browser_e2e/test_ui_main_flow.py)、
[`ChatPage.tsx:691`](../frontend/src/pages/ChatPage.tsx)。

正常复用结果同时出现在 `.structured-summary` 和 `.json-block`，旧的
`page.get_by_text("016 UI 第二次复用正文")` 在 Playwright strict mode 命中两处，导致 1 个浏览器场景失败。

产品结果本身已经出现，但浏览器门禁不再可靠。应按可访问角色/区域定位“结构化结果”中的摘要或使用精确 test id，并增加
Presentation 独立区域断言，不能简单改为 `.first()` 掩盖重复渲染语义。

## 3. 已确认通过的实现

- Conversation 工作流模式持久化、默认开启、Turn snapshot 和服务端 `workflow.*` fencing；
- Conversation 历史分页、切换与刷新恢复；
- Run 按 component/workflow version 查询及组件调用历史；
- 已发布 WorkflowVersion 手动运行和 RunDetail 证据链；
- Run.output 与 Presentation 持久结构分离，Presentation 失败不覆盖 Run 成功事实；
- JSON 复制；
- Alembic `0018` 空库升级；
- 现有后端、前端单测和 34 项进程级 E2E 无回归；
- 本轮浏览器 console 无 error/warning。

## 4. 验证记录

| 检查 | 结果 |
|---|---|
| Ruff / 后端 Pytest | 通过；262 passed |
| Alembic | `0001→0018` 通过 |
| TypeScript / ESLint / Vitest / build | 通过；39 tests |
| 进程级 E2E | 34 passed |
| 浏览器 E2E | 1 failed, 1 passed |
| 真实 API/Worker/Vite/Chromium | 已执行；发现 P1-2、P1-3、P2-1、P2-2、P2-3 |
| console | 0 error / 0 warning |

修复和验收顺序见 [`NEXT_STEPS-020.md`](../STEP/NEXT_STEPS-020.md)。
