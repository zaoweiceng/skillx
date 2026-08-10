# NEXT_STEPS-020：真实 Agent 主路径与前端终态一致性

依据：[`REPLAY-019.md`](REPLAY-019.md)、
[`review/design-compliance-review-020.md`](review/design-compliance-review-020.md)、
[`review/code-review-020.md`](review/code-review-020.md) 和 `docs/` 下设计/规范文档。

范围：单用户 Local Demo；不增加鉴权、RBAC、多租户或分布式部署。020 的第一目标仍是让用户从自然语言提问到最终答复的
完整流程真实跑通，其次才是补齐视觉和次要交互。

## 1. 目标与通过条件

020 只有同时满足以下条件才可通过：

```text
普通用户输入
  → 兼容的真实 LLM/Agent 判断是否使用工作流
  → 可验证的参数来源与 Schema 校验
  → Workflow 或通用 Agent 执行
  → Chat 自动收到最终 JSON / Presentation
  → 计时与 Token 在终态当下停止并可信显示
  → 历史、详情、主题与控件均可复查和使用
```

不接受以下替代证据：天气/城市正则、目录第一项、deterministic fixture 的固定分支、只刷新后正确、只跑单测或 HTTP 冒烟。

## 2. P0：阻塞主流程的问题

### P0-1：接入兼容 Host 协议的真实 Agent

- [ ] 为至少一个真实 LLM provider 实现可运行 adapter/sidecar，支持当前 Host 的双向事件与 ToolResult 回送：
  `agent.plan`、`tool.requested`、Host 执行、stdin ToolResult、`agent.step.completed`、`agent.completed`。
- [ ] adapter 必须支持超时、取消、max steps/token、非零退出、非法 JSON、Tool 越权和 usage 缺失，不得绕过现有 Host fencing。
- [ ] 把 `examples/traex_stub.py` 限定为协议/故障测试；默认 Demo 配置应能明确选择 `real|fixture|unavailable`，三者不能混称。
- [ ] `GET /health/ready` 返回 selection provider 类型、协议可用性、真实模型可用性和最后一次探测摘要；SettingsPage 展示同一事实。
- [ ] 未配置真实 provider 时页面明确提示“真实语义 Agent 未配置”，不得把 fixture 的 agent 模式作为完整验收通过。
- [ ] 用真实 provider 通过最低语义集：

| 输入 | 必须结果 |
|---|---|
| `上海天气如何` | 选择天气 Workflow，`city=上海`，不追问 city |
| `帮我看看天气` | 只追问 city |
| `总结 https://example.test/a` | 正确绑定 URL，并自主选择复用或继续探索 |
| `把 q12 的记录导出` | 绑定 q12，不因不是 URL 而丢失 |
| `比较上海和北京天气` | 按 Schema 绑定数组/组合；不支持时明确澄清，不静默取第一项 |
| 与目录无关的闲聊 | `continue_agent`，不强行运行工作流 |
| 工作流关闭后的同类问题 | 不出现任何 `workflow.*` Tool/Run |

### P0-2：把 binding 来源变成服务端可验证证据

- [ ] `WorkflowSelection` 对每个 binding 强制要求来源；`bindingSources=[]` 与多余/缺失字段均判非法。
- [ ] `user_message/history_message` 必须引用当前 Conversation 中可见的真实 message id，`quote` 必须是原文片段。
- [ ] `tool_result` 来源必须引用当前 disclosure session 已完成、未被 redact 替换语义的 ToolResult，并包含可验证 path。
- [ ] 规范化后的 binding 与 quote 不完全相等时，记录 normalization type（如地名去后缀、数值/日期解析）和服务端验证结果。
- [ ] 低置信、冲突、多义、引文不存在、来源越权或 Schema 不合法时统一转 `ask_user`；不得执行后再补证据。
- [ ] PendingSelection 合并新回答时重新验证所有来源，不把整句无条件塞进唯一字段。
- [ ] 持久化公开的验证摘要供 Chat/RunDetail 展示；不保存模型私有推理。
- [ ] 增加负向测试：空来源、伪造 message id、引文不在消息、跨 Conversation、伪造 ToolResult、字段错配、重复冲突来源。

### P0-3：统一 Turn 终态、时间、Token 与 Presentation 更新

- [ ] API 所有时间输出统一为带 `Z` 或显式 offset 的 RFC 3339；增加 SQLite round-trip 与非 UTC 浏览器时区测试。
- [ ] Agent Run succeeded/failed/cancelled 后必须刷新或推送其 Turn；不能只更新右侧 trace。
- [ ] Workflow Run succeeded 后，如 Presentation enabled，继续跟踪到 `completed|failed`；Chat 自动追加阅读结果，无需 reload。
- [ ] Turn 仅在最终 assistant reply 和所需 Presentation 终态后完成；失败接力沿用原 Turn，终态事件只发生一次。
- [ ] Usage 增加 `pending/processing` 或等价语义。账本尚未落地但已知有模型调用时显示“统计中”，不得显示 0。
- [ ] 只有执行计划能证明没有任何模型调用时才返回 `zero`；provider 不回 usage 时返回 unavailable/partial。
- [ ] 推荐新增 Turn 级 SSE/事件聚合，统一承载 Run、AgentRun、checkpoint、Presentation、elapsed 和 Usage；若保留轮询，必须有
  明确的终止条件、最后一次强制 refresh 和 timer cleanup。
- [ ] Chat 增加状态回归：终态当下立即显示最终耗时/Token，等待 2 秒不继续增长；reload 前后值一致；Asia/Shanghai 下不多 8 小时。
- [ ] Presentation 回归：结构化 JSON 先出现，文本稍后自动出现；两者各一份、各自可复制；Presentation failed 不影响 JSON。

## 3. P1：页面可用性与真实状态

### P1-1：修复工作流编辑器布局

- [ ] 将源码编辑器、Capability metadata form、node inspector、definition list 使用独立 class，不复用 `source-panel` 高度规则。
- [ ] 将 `.inspector-fields > div` 改为只命中真正的 `dt/dd` 行；为 `.presentation-policy-editor` 定义完整宽度网格。
- [ ] summary/useWhen/doNotUseWhen/examples/tags/Presentation 在 1440、1920 和窄屏下无挤压、遮挡或巨大无意义空白。
- [ ] 保存、刷新、静态检查、发布和直接运行按钮在两种主题下均可用。

### P1-2：完成亮色主题和系统状态真值

- [ ] `.runtime-status`、nav hover/active、图标和 disabled 状态全部使用主题语义 token，正文/弱文本至少满足 WCAG AA。
- [ ] SettingsPage 删除静态运行状态数组，改读 ready/health；Worker、真实 Agent、fixture、Browser adapter、Secret provider 分别报告。
- [ ] 探测失败要有 catch/错误态，不能保留默认值冒充真实状态。
- [ ] 两种主题逐页检查 Chat、Components、ComponentDetail、Workflows、Editor、RunDetail、Settings；切换和 reload 不闪烁、不丢失。

### P1-3：实现或删除组件分类按钮

- [ ] 用 executionClass 实现“全部/零 LLM/局部 LLM/Agent 工作流”筛选；明确 `hybrid` 与 agent 的映射。
- [ ] 选中状态使用 `aria-pressed`，键盘和点击均可操作，空结果有提示。
- [ ] 当前组件量可前端过滤；一旦列表分页，筛选条件必须进入服务端游标查询，不能只过滤当前页。
- [ ] 若产品暂不需要筛选，删除按钮而不是保留无行为控件。

## 4. P1：恢复浏览器门禁可信度

- [ ] 修复 `test_ui_main_flow.py` 对双层结果的定位，分别断言“结构化结果”和“阅读结果”，不使用 `.first()` 掩盖重复命中。
- [ ] 新增真实浏览器用例：Conversation 创建/切换/reload、工作流默认开启和关闭 fencing、终态计时/Token、Presentation 异步完成。
- [ ] 新增视觉/交互用例：亮/暗主题持久化、runtime card 对比度、元数据和 Presentation form、组件筛选。
- [ ] 真实 Agent 语义场景与 fixture 协议场景分组；发布门禁必须明确要求前者时，不得因 fixture 通过而跳过。
- [ ] `make browser-ui-check` 应使用项目可写的 uv/cache 配置，避免本机全局 cache 权限让门禁无法启动。

## 5. 推荐实施顺序

1. 先修 P0-3 的时间序列化和 Turn 终态刷新，让每次后续联调都有可信观测。
2. 完成 P0-2 的来源信任边界，再接入 P0-1 的真实 Agent，避免把模型错误直接放大为执行错误。
3. 用真实 Agent 跑完最低语义集，确认自然语言入口能真正启动 Workflow。
4. 修复编辑器、亮色主题、Settings 真值和组件筛选。
5. 更新浏览器门禁，最后跑完整正常链与失败接力链。

## 6. 020 验收流程

使用全新 Alembic 数据库，实际启动 API、Worker、Vite、本地 HTTP 站点、兼容的真实 LLM/Agent 和 Chromium：

1. 亮色打开天气 Workflow，编辑目录元数据与 Presentation，保存、reload、静态检查并发布；界面无挤压或低对比度。
2. 新建 Chat，确认工作流默认开启；输入“上海天气如何”，轨迹显示真实 Agent 的 L0/L1、经服务端验证的
   `city ← 上海` 证据，并自动启动天气 Workflow。
3. 发送后计时立即开始；JSON 先出现，Presentation 稍后自动出现；终态当下计时停止，Token 从“统计中”变为可信终值。
4. 不刷新页面等待 2 秒，耗时不再增长；再 reload，模式、时间、Token、JSON、文本和轨迹保持一致且各一份。
5. 关闭工作流后发送同类问题，确认 requested/Host/服务端三层均无 `workflow.*`；通用 Agent 返回结果或明确能力不足。
6. 创建、切换至少两个 Conversation；刷新后历史顺序、每轮模式与结果正确，轮询/SSE 不串线。
7. 从组件详情手动运行成功与可控失败各一次；历史出现两条，RunDetail 能查看输入、节点、事件、错误/结果、Presentation 并复制。
8. 点击四个组件分类，列表内容与选中项一致；用键盘完成同样操作。
9. 系统页状态与实际启动/停止 Worker、真实 Agent、fixture 配置一致。
10. 执行真实 503 失败接力，确认 source failure、checkpoint、remaining-only continuation、原 Chat 最终回复、Turn/Token 归属均无回归。
11. 运行 `make demo-check` 和 `make browser-ui-check`，必须全绿；浏览器 console 为 0 error/0 warning。

## 7. 完成定义

- [ ] 020 P0/P1 项全部关闭；
- [ ] `make demo-check` 全绿；
- [ ] `make browser-ui-check` 全绿；
- [ ] 真实 Agent 最低语义集全绿；
- [ ] 真实正常链与 503 失败接力链全绿；
- [ ] 两种主题无阻塞级视觉/交互问题；
- [ ] 新增 `REPLAY-020.md` 记录真实 provider、命令摘要、Run/Turn/checkpoint 证据和未通过项，不以 fixture 替代真实语义证据。
