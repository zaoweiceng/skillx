# 代码审查报告（021）

审查日期：2026-08-10

代码基线：`774a1c6`，审查开始时工作树 clean。范围覆盖 `REPLAY-020.md`、Agent Provider、binding 来源、时间/Usage、
Presentation、Chat、主题、Settings 与浏览器门禁。Local Demo 不考虑鉴权。

## 1. 结论

**不通过：5 个 P1、3 个 P2，另有 1 个外部验收阻塞。**

binding coverage、精确消息证据、Turn 终态、组件筛选和正确模板 Presentation 均已按根因修复。剩余问题不是上一轮问题的
简单重复，而是修复范围不完整或真实页面暴露出的下一层契约问题。

## 2. P1 Findings

### P1-1：RFC 3339 只用于部分视图，Run 与工作流历史仍按错误时区展示

位置：[`routes.py:425`](../backend/app/api/routes.py)、[`routes.py:445`](../backend/app/api/routes.py)、
[`routes.py:591`](../backend/app/api/routes.py)、[`routes.py:3273`](../backend/app/api/routes.py)、
[`routes.py:4946`](../backend/app/api/routes.py)。

`turn_view()` 已使用 `rfc3339()`，但 `workflow_view()`、`run_view()`、NodeRun、WorkflowVersion、RunEvent 和
ConversationRunEvent 仍直接返回 SQLite datetime。FastAPI 将 naive 值序列化为无 offset ISO 字符串，浏览器按本地时间解释。

实测同一时刻 Chat 显示 15:12，组件历史和 WorkflowVersion 显示 07:12；API 返回
`2026-08-10T07:12:23.484276`，没有 `Z`。

修复：所有公开 datetime 必须经过唯一 serializer；禁止在 route 字典里直接放 ORM datetime。为每个列表/详情/event/SSE
契约增加 `Z` 断言，并在 Asia/Shanghai 浏览器验证显示。

### P1-2：903px 窗口下 Chat 的固定三列把消息区压到不可用

位置：[`index.css:1198`](../frontend/src/index.css)、[`index.css:1506`](../frontend/src/index.css)、
[`index.css:1987`](../frontend/src/index.css)。

Chat 固定为 `220px minmax(0,1fr) 300px`，同时桌面侧栏占 224px、页面还有左右 padding。只有 viewport `≤760px`
才切单列/隐藏 trace。实测 viewport 903px 时 Chat layout 约 607px，中间列理论上只剩约 87px，消息正文被挤成窄条；
composer 仍按 `right:356px` 定位。

修复：使用容器查询或至少把 Chat 断点提高到能容纳侧栏、历史、消息最小宽度和 trace 的阈值。中等宽度优先把历史变抽屉、
trace 变可切换抽屉，保证消息主列最小 420～480px。

### P1-3：Presentation 未校验模板占位符，静态检查可以发布残缺文本

位置：[`product_history.py:197`](../backend/app/application/product_history.py)、
[`product_history.py:322`](../backend/app/application/product_history.py)。

发布时只规范化 template 字符串，不解析 `{field}`；执行时只遍历实际 output key 做 `replace()`。不存在的字段不会报错。
实测 Output Schema 只有 `message`，模板 `{greeting}` 仍通过静态检查和发布，最终原样展示
`问候结果：{greeting}`。

修复：发布前解析占位符，只允许 `{json}` 和 Output Schema 顶层 property；未知字段、未闭合花括号和不支持的格式必须定位到
Presentation 字段并阻止发布。运行时再做防御性残留检查，失败只标记 Presentation failed，不污染 Run.output。

### P1-4：Chat 直接展示底层执行异常，泄漏内部路径且普通用户无法理解

位置：[`executors.py:656`](../backend/app/runtime/executors.py)、[`routes.py:445`](../backend/app/api/routes.py)、
[`ChatPage.tsx:692`](../frontend/src/pages/ChatPage.tsx)。

Browser executor 把完整 Playwright exception 写入 `error.message`；`run_view()` 直接公开；Chat 又原样渲染。实测错误卡包含
Chromium 完整启动命令、`/Users/...` 绝对路径、进程 PID、MachPort 和 kill 日志，占据大段页面。

修复：持久证据可保留受限 technical detail，但公共 Run projection 必须生成 `userMessage`；Chat 只显示稳定 code、简短可操作说明
和 RunDetail 链接。底层日志进入受控 evidence/artifact，并执行 secret/path/长度脱敏。

### P1-5：数组 binding 的“服务端验证”仍可被子串巧合绕过

位置：[`routes.py:4439`](../backend/app/api/routes.py)。

`list_extraction` 只检查 `all(str(item) in quote for item in value)`，不验证顺序、分隔、重复、类型边界或字段专用 parser。
例如 quote `2026` 可以为数值数组 `[20, 26]` 提供“已校验”结果；这证明元素字符出现，不证明用户表达了该数组。

修复：列表只接受 JSON/明确分隔格式或按 schema 注册的确定性 parser，并保存 parser/version；无法确定性还原时转 ask_user。
`serverVerified=true` 只能表示值能从证据确定性重建，不能退化为文本包含。

## 3. P2 Findings

### P2-1：亮色 notice/status 仍使用暗色主题浅色，严重低对比度

位置：[`index.css:734`](../frontend/src/index.css)、[`index.css:761`](../frontend/src/index.css)。

`.notice-panel` 固定 `color:#89d3ad`，在亮色混合背景上的对比度约 1.30:1；status success/warning/neutral 在白色面板上约
1.76、1.82、2.10:1，字号又只有 8px。截图中 fixture banner 和多个状态 badge 明显发白。

修复：为状态建立主题 token，亮色使用深色 foreground；普通文字至少 4.5:1，大号/非文字控件至少 3:1。增加计算型对比度测试，
不能只做截图存在性断言。

### P2-2：`health/ready` 每次请求同步调用真实模型，且未计入 Usage

位置：[`agent_provider.py:31`](../backend/app/application/agent_provider.py)、
[`routes.py:498`](../backend/app/api/routes.py)。

real 模式下每次 `/health/ready` 都调用 `OpenAICompatibleAdapter.generate()`。这会让 Settings 打开、部署探针或监控轮询产生
付费模型请求和最高 60 秒延迟；调用不进入 ModelUsage。探测还走普通 structured-output adapter，不走 sidecar 的 function
calling + ToolResult 路径，因此 `modelAvailable=true` 也不能证明真实 Agent 主路径可用。

修复：ready 只读取有 TTL 的后台探测状态；昂贵 probe 显式触发/限频并计入独立 operational usage。真实 Agent readiness 应探测
同一 sidecar 协议路径，区分 endpoint reachable、structured output、function calling 和 Host round-trip。

### P2-3：浏览器门禁重复执行安装步骤，实际运行不稳定

位置：[`Makefile:16`](../Makefile)、[`Makefile:23`](../Makefile)、[`Makefile:27`](../Makefile)。

`browser-install` 是 phony，`e2e-check` 和 `browser-ui-check` 每次都依赖它。注释称“浏览器安装独立执行”，实际却在每次门禁
重复执行 `playwright install chromium`。本轮 `make demo-check` 中该步骤通过，紧接着 `make browser-ui-check` 又卡住两分钟
无输出；直接运行既有浏览器用例最终 2 passed。

修复：把安装改为显式 bootstrap 命令或带版本 marker 的幂等目标，测试门禁只校验 executable 存在并立即失败提示。避免网络/
安装锁影响产品回归结果。

## 4. 外部验收阻塞：真实 Provider 未执行

[`openai_agent_sidecar.py`](../examples/openai_agent_sidecar.py) 和本地 mock provider 测试证明了协议形状，但当前没有任何外部
真实模型调用。Sidecar 同时要求 provider 支持 function calling 与 strict `json_schema` response format；“OpenAI-compatible”
并不天然保证两者组合可用。

在至少一个真实 provider 上完成无测试指令的最低语义集前，只能判定“代码路径存在、fixture 协议通过”，不能判定原始需求通过。

## 5. 已确认关闭的 020 Findings

- 每个 binding 有且仅有一个来源，精确 message/ToolResult 来源可服务端核验；
- Agent/Workflow 终态会刷新 Turn，Presentation 终态继续跟踪；
- Turn 时间输出带 `Z`，运行中 Usage 使用 pending，不再显示虚假 0；
- 工作流关闭路径的 0.2s/20 Token 在终态和 reload 后一致；
- 正确模板的 Chat Presentation 自动出现并持久恢复；
- 组件四分类过滤和 `aria-pressed` 可用；
- runtime card 不再亮色黑底黑字；
- Settings 不再把 fixture 冒充真实语义 Agent；
- capability metadata 与 Presentation 不再受旧 `100px 1fr` 规则挤压。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| 后端 | Ruff 通过；266 passed |
| 前端 | TypeScript/ESLint/build 通过；41 passed |
| Alembic | `0001→0018` 通过 |
| 进程 E2E | 34 passed |
| 浏览器用例 | 直接重跑最终 2 passed；Makefile 包装未稳定完成 |
| 人工真实页面 | 已执行；复现 P1-1～P1-4、P2-1 |
| console | 0 error / 0 warning |
| Git | 审查开始时 clean；`git diff --check` 通过 |

修复顺序和验收条件见 [`NEXT_STEPS-021.md`](../STEP/NEXT_STEPS-021.md)。
