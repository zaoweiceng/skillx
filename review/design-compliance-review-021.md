# 设计与规范达成度审查报告（021）

审查日期：2026-08-10

依据：[`REPLAY-020.md`](../REPLAY/REPLAY-020.md)、[`NEXT_STEPS-020.md`](../STEP/NEXT_STEPS-020.md)、
[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md) 和 [`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`774a1c6`，审查开始时工作树 clean。本轮只新增 021 三份审查文档，没有修改业务代码。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

**不通过。**

020 已关闭上一轮最直接的 Turn 刷新、组件筛选、元数据表单和 fixture 真值问题。真实 API、Worker、Vite、SQLite、
Workflow Runtime 与 Chromium 环境中，以下流程已经可用：

```text
精确参数证据 → 服务端核验 → Workflow Run
工作流关闭 → 通用 Agent → 稳定终态指标
JSON → 异步 Presentation → 独立复制 → reload 恢复
组件手动调用 → RunDetail → 组件历史
```

但不能完成无条件的预期功能：

1. 当前环境仍没有真实模型 endpoint/model/API key；“上海天气如何”在实际 Demo 中仍追问 `city`。真实 LLM/Agent
   是否能自主选择、提参和组合工作流没有完成验收。
2. RFC 3339 修复只覆盖 Conversation/Turn/Presentation，Run、NodeRun、WorkflowVersion 和事件 API 仍返回无时区时间。
   真实页面把 15:12 的组件调用显示为 07:12。
3. 真实 903px 窗口下 Chat 仍使用 `220px + 1fr + 300px` 三列，中间消息区仅剩约 87px，内容被严重挤压；
   只有到 760px 以下才切移动布局。
4. Presentation 模板的占位符没有按 Output Schema 校验。不存在的 `{greeting}` 能通过静态检查、发布并原样显示给用户。
5. 亮色主题的 notice/status 文本继续使用暗色主题硬编码浅色，实测对比度只有约 1.30:1～2.10:1。
6. Workflow 失败时 Chat 原样展示完整 Playwright 启动日志、内部绝对路径和底层异常，不符合面向普通用户的错误模型。

因此，020 的数据与执行骨架基本成立，但 **真实语义入口、跨页面时间一致性和中等窗口下的核心对话体验仍阻塞交付**。

## 2. 测试环境与边界

本轮实际启动：

- FastAPI：`127.0.0.1:8047`；
- 独立 Worker；
- Vite：`127.0.0.1:5147`；
- 本地动态 HTTP 站点：`127.0.0.1:8947`；
- 全新 Alembic `0001→0018` SQLite 数据库；
- 真实 Chromium 页面点击、输入、路由、剪贴板、reload、截图和 console；
- `SKILLX_AGENT_PROVIDER_TYPE=fixture` 与真实 Host/ToolResult 协议。

当前环境检查结果：`SKILLX_MODEL_ENDPOINT`、`SKILLX_MODEL_NAME`、模型 API key、AceDataCloud token 和 OpenAI key
均未配置。现有统一模型接入技能也要求缺失的 token，且其认证说明文件不存在，无法安全补充真实模型证据。

fixture 只替代外部模型，不替代 API、Worker、数据库、MCP、Workflow Runtime 或 UI。测试结束后所有临时服务均已停止。

## 3. 完成矩阵

| 020/设计目标 | 判定 | 真实证据 |
|---|---|---|
| real / fixture / unavailable 显式区分 | 通过 | Settings 显示 fixture，并明确“仅验证协议，不代表真实语义能力” |
| 外部真实 Agent 最低语义集 | 不通过 | 无真实 provider；普通输入“上海天气如何”仍返回“请补充输入：city” |
| binding 来源覆盖与服务端核验 | 部分通过 | URL exact、多轮 `city=上海` 均显示 `serverVerified=true`；数组 `list_extraction` 仍仅做子串存在检查 |
| 工作流默认开启与关闭 fencing | 通过 | 关闭后无 `workflow.*` 轨迹；通用 Agent 成功，0.2s/20 Token，等待 2 秒不增长，reload 一致 |
| Chat 自动执行与终态刷新 | 通过 | fixture 强制 run_full 后 JSON 自动显示，Turn 指标终态稳定 |
| JSON + Presentation | 部分通过 | 正确 `{message}` 模板可自动出现、持久化并分别复制；未知 `{greeting}` 仍通过发布并产生残缺文本 |
| Conversation 历史 | 通过 | 多 Conversation 新建、排序、切换、reload 和模式恢复正常 |
| Component/Workflow 调用历史 | 部分通过 | 手动运行、RunDetail、历史入口均可用；显示时间少 8 小时 |
| 主题切换和持久化 | 部分通过 | 亮/暗切换与 reload 有效；亮色 notice/status 对比度不合格 |
| 工作流编辑器 | 部分通过 | 元数据/Presentation 不再压入 100px 列；903px 下标题和操作按钮换行，palette/inspector 保留大块空白 |
| 核心页面响应式 | 不通过 | 903px Chat 消息列几乎不可用，历史和 trace 占据绝大多数宽度 |
| 错误展示 | 不通过 | Chat 向最终用户输出完整 Playwright transport 日志和内部路径 |
| 组件筛选 | 通过 | 四分类真实过滤，`aria-pressed` 正确；Agent 分类只显示 3 个 Agent 组件 |
| 系统状态真值 | 部分通过 | fixture/数据库/Browser/Secret 状态真实；Worker 诚实显示 unknown；真实模式探测仍有同步付费调用问题 |

## 4. 真实场景结果

### 4.1 工作流选择、binding 与多轮补参

使用 fixture 协议路径强制执行网页 Workflow 时：

- `workflow.search → workflow.describe → run_full` 正常；
- URL 来源展示为 `100% · 服务端已校验 (exact)`；
- Turn 为 1.0s / 30 Token。

普通输入“上海天气如何”仍进入 ask_user。第二轮回答“上海”后：

- PendingSelection 被恢复；
- `city ← “上海” · serverVerified (exact)`；
- 天气 Run succeeded；
- JSON 为 `上海 / 多云 / 19℃`；
- 该确定性补参 Turn 为 0.3s / 0 Token。

这证明补参、证据和 Runtime 链可用，但不能替代“一次自然语言输入由真实模型直接提参”的原始目标。

### 4.2 Turn、Token 与双层结果

关闭工作流后的通用 Agent：

```text
工作流关闭 · 0.2s · 20 Token
```

等待 2.2 秒值不增长，reload 后保持一致，上一轮 8 小时计时错误已在 Turn 路径关闭。

临时启用问候 Workflow 的模板 Presentation 后：

- Chat 先显示 `{"message":"你好，问候 Alice"}`；
- 随后自动显示“问候结果：你好，问候 Alice”；
- Turn 为 0.4s / 30 Token，等待 2 秒不增长，reload 后一致；
- “复制 JSON”和“复制文本”分别得到合法 JSON 与纯文本。

但把模板写成 `问候结果：{greeting}` 时静态检查仍通过、v2 发布成功，最终阅读结果保留未解析占位符。改成
`{message}` 后 v3 才正确。这是发布契约缺口，不是 Presentation Driver 本身失败。

### 4.3 组件运行与历史时间

“本地天气查询”组件输入北京后：

- Run succeeded；3 个节点、事件、依赖版本和 replay Artifact 均可查看；
- JSON 为 `北京 / 小雨 / 21℃`；
- 组件详情出现 1 条调用历史。

但 API 原始值为 `2026-08-10T07:12:23.484276`，没有 `Z` 或 offset，组件历史直接显示 07:12；同一时刻 Chat
使用已修复的 RFC 3339 路径显示 15:12。WorkflowVersion 历史也有相同问题。

### 4.4 真实网页 Workflow 与错误呈现

本轮人工网页总结 Run 在 Worker 内启动 Chromium 时遇到 macOS MachPort permission denied，Run 正确进入
`DEPENDENCY_UNAVAILABLE`。相同真实进程浏览器自动化场景随后独立重跑通过，因此不能仅凭本次偶发 transport 失败认定
Browser Runtime 代码必然不可用。

但是 Chat 把 `BrowserType.launch` 的完整命令、内部 `/Users/...` 路径、Chromium 日志和 kill 细节全部放进普通用户错误卡。
无论 transport 失败是否由环境造成，这个展示行为都不应存在。

## 5. 视觉与交互审查

### 5.1 通过项

- 暗色 Settings、Components、RunDetail 和宽内容区整体风格一致；
- 亮色 runtime card 已改为语义背景，上一轮黑底黑字问题关闭；
- 组件筛选、主题、导航、Chat 开关、发送、历史、发布、运行和复制均可点击；
- 元数据与 Presentation 表单字段可见、可编辑；
- 浏览器 console 最终为 0 error / 0 warning。

### 5.2 未通过项

- `.notice-panel` 在亮色背景上的文本对比度约 1.30:1；status success/warning/neutral 约 1.76～2.10:1，且字号仅 8px。
- 实际窗口宽 903px 时，应用仍保留 224px 侧栏；Chat 内容区再固定分给历史 220px、trace 300px 和页面 padding，
  中间消息列只剩约 87px，正文纵向挤压并被遮蔽。
- Chat 只在 viewport `≤760px` 才隐藏历史/trace，断点没有考虑固定侧栏后的可用容器宽度。
- 同一 903px 窗口下，工作流标题和“JSON 视图/保存/静态检查/发布版本”按钮文字换行；editor 变单列后
  `.source-panel/.preview-panel` 仍保持 600px 最小高度，产生大面积空白和过长滚动。

## 6. 自动化验证

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过，退出码 0 |
| 后端 Ruff / Pytest | 通过；266 passed in 27.53s |
| Alembic | `0001→0018` 通过 |
| 前端 TypeScript / ESLint / build | 通过 |
| 前端 Vitest | 10 files / 41 tests passed |
| 进程级 E2E | 34 passed in 129.25s |
| `make browser-ui-check` | 未完成：第二次 `playwright install chromium` 检查无输出卡住，人工终止 |
| 浏览器用例直接重跑 | 首次 2 skipped；再次 2 passed in 18.24s |
| 人工真实 API/Worker/Vite/Chromium | 已完成，发现上述功能与视觉问题 |
| console | 0 error / 0 warning |
| `git diff --check` | 通过 |

浏览器产品用例最终可通过，但 Makefile 每次检查都依赖 phony `browser-install`，本轮没有稳定完成声明的
`make browser-ui-check` 命令，门禁可重复性仍需修正。

## 7. 最终判定

020 的核心修复方向正确，确定性和 fixture 场景已经大幅接近目标；但真实 Agent 证据、时间契约、Chat 响应式和
Presentation/错误展示仍有用户可见阻塞。完成 [`NEXT_STEPS-021.md`](../STEP/NEXT_STEPS-021.md) 后再申请通过。
