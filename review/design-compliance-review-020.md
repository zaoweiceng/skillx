# 设计与规范达成度审查报告（020）

审查日期：2026-08-10

依据：[`REPLAY-019.md`](../REPLAY/REPLAY-019.md)、[`NEXT_STEPS-019.md`](../STEP/NEXT_STEPS-019.md)、
[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md) 和 [`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`f87ed67`；审查对象为该基线上的当前未提交工作树。本轮只新增 020 三份审查文档，没有修改业务代码。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

**不通过。**

019 已把主题、历史、工作流模式、调用历史、Presentation、Turn 和 Usage 的数据与页面骨架接入，API、Worker、
数据库、工作流运行时和大多数页面交互可以真实运行；但当前版本还不能完成 019 期望的完整用户流程：

1. 当前 Demo 实际配置的是确定性 `traex_stub.py`，输入“上海天气如何”后仍返回“请补充输入：city”，没有实现
   `city=上海` 的语义提取；真实 LLM/Agent 尚无兼容当前 Host 双向协议的可运行接入。
2. Agent Run 到达终态后 Chat 不刷新 ConversationTurn；真实页面先显示 `28802.5s / 0 Token`，刷新后才变成
   `0.1s / 20 Token`。同一轮的 Presentation 也可能因为 Run 已 succeeded 而停止轮询。
3. 绑定来源只是 Agent 自报字段，服务端没有验证来源存在且引文确实来自用户消息/历史/ToolResult，尚未达到
   “服务端不信任模型”的边界。
4. 前端存在可见和可操作性缺陷：工作流编辑器元数据面板布局破坏、亮色侧栏状态卡不可读、组件分类按钮不执行筛选。
5. 浏览器 E2E 当前为 1 通过、1 失败，不能宣称浏览器门禁全绿。

因此，018 已跑通的确定性执行、固化、复用和失败接力能力没有被推翻，但 **019 新增的产品化交互与通用语义入口
尚未达到可交付状态**。

## 2. 真实环境测试范围

本轮从全新临时数据库启动并实际操作：

- FastAPI：`127.0.0.1:8020`；
- 独立 Worker；
- Vite：`127.0.0.1:5120`；
- 本地动态 HTTP 站点：`127.0.0.1:8920`；
- 真实 Chromium 页面点击、输入、路由、剪贴板和 console；
- 实际 SQLite、队列、Workflow Runtime、MCP Tool、Run/Event/Artifact 持久化；
- Agent 外部进程为 `examples/traex_stub.py`。

这是真实前后端/Worker/浏览器联调，不是单测或 HTTP 冒烟；但 deterministic stub 只验证 Host 协议，不能作为
真实大模型语义质量证据。测试结束后 API、Worker、Vite 和本地站点均已停止。

## 3. 需求达成矩阵

| 019 目标 | 判定 | 实测与代码证据 |
|---|---|---|
| 亮/暗主题切换与持久化 | 部分通过 | 切换、`aria-pressed`、刷新保留均有效；暗色主体协调，亮色侧栏状态卡对比度不合格，编辑器布局在两主题下均有结构问题 |
| 普通聊天式历史记录 | 通过 | 可新建两个 Conversation、按最近活动展示、点击切换并刷新恢复；消息、模式和轨迹没有串线 |
| 工作流元数据编辑/查询 | 部分通过 | 字段已不再是截图中的白块，保存/发布链有自动化覆盖；但 `source-panel` 强制 680px 高度，Presentation 区被错误压入 100px 列，实际页面不美观且难用 |
| 组件手动调用历史 | 通过 | “本地天气查询”手动输入北京后 succeeded；详情出现 1 条历史，能进入完整 RunDetail |
| 工作流手动调用与历史 | 通过 | 已发布天气版本可用 Schema 表单直接运行；RunDetail 展示 3 个节点、事件、结构化结果和证据 |
| JSON 与自然语言双层结果 | 部分通过 | 数据模型和 RunDetail 渲染存在；JSON 复制实测正确。Chat 在 Run succeeded 后停止轮询，不能保证稍后完成的 Presentation 自动出现 |
| Agent 决定是否调用及自动传参 | 不通过 | 开启时确实走 `workflow.search → workflow.describe`，但“上海天气如何”仍追问 city；没有真实模型接入证据 |
| 工作流默认开启/可关闭 | 通过 | 新对话默认开启；关闭后新 Turn 没有 `workflow.*` 轨迹，通用 Agent 正常完成，刷新后模式快照仍为 disabled |
| 总耗时与 Token | 不通过 | 数据刷新后值正确，但终态当下显示 `28802.5s / 0 Token`；刷新才校正为 `0.1s / 20 Token`，不满足实时可信展示 |
| 各按钮/功能可点击可用 | 部分通过 | 导航、新建/切换对话、主题、发送、工作流开关、运行、历史、刷新和复制均可用；组件页三个分类按钮没有 handler，点击后列表不变 |
| 配色和整体美观 | 部分通过 | 暗色组件、对话、运行详情整体统一；亮色 runtime 卡正文与背景对比度约 1.07:1，弱文本约 3.17:1；工作流元数据/Presentation 排版明显破坏 |

## 4. 真实场景观测

### 4.1 对话与 Agent 选择

工作流开启的新 Conversation 输入“上海天气如何”：

```text
workflow.search (L0) → workflow.describe (L1) → ask_user
最终回复：请补充输入：city
```

页面记录 0.1 秒、30 Token 和完整 Tool 时间线。说明渐进披露和审计事件可以运行，但最重要的语义绑定目标没有实现。
当前 stub 只抽取 URL 或 `qN`，所以这个结果是可复现的系统配置限制，不应为天气增加城市正则特判。

新建 Conversation 并关闭工作流后输入“请直接回答：关闭工作流测试”：

- 页面明确显示“本轮已冻结为工作流关闭模式”；
- Agent 轨迹 succeeded，1 步、20 Token；
- 没有 `workflow.search/describe/...` 事件；
- 刷新后历史、模式、最终回复、0.1 秒和 20 Token 均可恢复。

模式 fencing 本身通过，但终态刷新存在下一节的问题。

### 4.2 计时与 Usage

关闭模式的 Agent 已在右侧轨迹显示 succeeded/20 Token 时，消息旁仍显示：

```text
28802.5s · 0 Token
```

刷新页面后变为：

```text
0.1s · 20 Token
```

约 28800 秒正好对应 Asia/Shanghai 与 UTC 的 8 小时时差。服务端从 SQLite 读取时间后用无 offset 的 ISO 字符串返回，
前端把它解释成本地时间；同时 Agent 终态轮询没有重新拉取 Turn，Usage 账本完成后 UI 也未更新。

### 4.3 手动运行与历史

在“本地天气查询”组件详情输入“北京”并启动：

- Run succeeded，输出 `{"city":"北京","condition":"小雨","temperature":21}`；
- 组件详情历史出现 1 条记录，展示时间、496ms、版本、输入、输出和 Presentation 状态；
- 点击历史进入 RunDetail，可查看 3 个 NodeRun、事件、依赖版本和 replay Artifact；
- 点击“复制 JSON”，剪贴板得到合法、格式化的同一 JSON。

这部分达到 019 的人工运行、历史和结构化复制目标。

### 4.4 页面与视觉

- 暗色的 Chat、组件列表、组件详情和 RunDetail 配色统一，焦点与状态可辨认。
- 亮色组件列表主体清晰，但侧栏 `.runtime-status` 固定使用暗背景，标题仍使用亮色主题的暗文字，几乎不可读。
- 工作流编辑器的能力元数据被通用 `.source-panel` 强制为至少 680px 高，形成大面积空白；
  `.inspector-fields > div` 又把整个 Presentation 编辑器压成 `100px 1fr`，复选框和标签严重换行。
- 组件页“零 LLM / 局部 LLM / Agent 工作流”点击后仍显示全部 8 个组件，代码中没有筛选状态或 click handler。
- 系统页能显示 `workflowSelectionMode=agent`，但“LLM / TraeX”“独立 Worker”等卡片状态是硬编码，运行中仍显示
  “未配置/需启动进程”，不是真实运行状态。
- 浏览器 console 最终为 0 error、0 warning。

## 5. 自动化验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过，退出码 0 |
| 后端 Ruff / Pytest | 通过；262 passed in 28.03s |
| Alembic 空库迁移 | `0001→0018` 通过 |
| 前端 TypeScript / ESLint / build | 通过 |
| 前端 Vitest | 9 files / 39 tests passed |
| 进程级 E2E | 34 passed in 129.46s |
| 浏览器 E2E（直接使用项目 venv） | **1 failed, 1 passed in 17.30s** |
| 真实产品页面测试 | 完成；发现语义绑定、终态指标、筛选和视觉问题 |
| 浏览器 console | 0 error / 0 warning |

浏览器失败点不是业务结果错误，而是新 UI 同时在摘要 `<p>` 与 JSON `<pre>` 中显示同一正文，旧的
`get_by_text()` 严格定位命中两处。它仍意味着当前浏览器门禁没有随 UI 契约更新，不能标记为通过。

## 6. 最终判定

当前版本已经具备 019 所需的大部分持久化和执行基础，但用户从自然语言发起、实时看到可信指标和可读结果的主路径仍有
阻塞问题。完成 [`NEXT_STEPS-020.md`](../STEP/NEXT_STEPS-020.md) 的 P0 项，并在兼容的真实 Agent 上重跑上海天气场景后，
才可以重新申请“通过”。
