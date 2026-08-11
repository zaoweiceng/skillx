# 设计与规范达成度审查报告（022）

审查日期：2026-08-10

依据：[`REPLAY-021.md`](../REPLAY/REPLAY-021.md)、[`NEXT_STEPS-021.md`](../STEP/NEXT_STEPS-021.md)、
[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)，以及本轮新增需求：
拆分 Agent Runtime 与 LLM Runtime；LLM Runtime 未配置时，使用 Agent Runtime 完成受约束的 LLM 调用。

代码基线：`774a1c6`。审查对象为 `REPLAY-021.md` 对应的未提交工作树。本轮只新增 022 三份审查文档，
没有修改业务代码。范围仍为单用户 Local Demo，不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

**不通过。**

021 针对 UTC 时间、Chat 响应式、默认错误投影、Presentation 模板、binding 取证、亮色对比度、
Provider 主动探测和浏览器门禁的修复方向正确，正常本地环境中的真实 API、Worker、Vite、SQLite、
本地 HTTP 站点与 Chromium 主流程已经可以完整运行。

但 022 的新增 Runtime 需求尚未实现：当前仍只有一组 `SKILLX_MODEL_*` 配置，Agent Sidecar、工作流 LLM 节点和
LLM Presentation 共用同一模型端点。LLM Runtime 未配置时，LLM 节点直接失败为 `MODEL_NOT_CONFIGURED`，
即使 Agent Runtime 已配置也不会接管。

此外还存在两个用户可观察的运行质量问题：技术证据重复包装并超过整体限长，终态 RunDetail 持续重连 SSE。
外部真实 Provider 最低语义集仍无凭据可执行，不能用 fixture 代替通过。

## 2. 021 修复达成矩阵

| 目标 | 结果 | 真实证据 |
|---|---|---|
| 所有公开时间为 UTC RFC 3339 `Z` | 通过 | Run/组件/工作流历史在 Asia/Shanghai 显示同一正确本地时刻；自动化覆盖 serializer |
| 760～1440px Chat 可用 | 通过 | 沙箱外真实 Chromium 在 760/903/1024/1280/1440px 验证消息列 ≥420px、composer 不越界 |
| 默认页面不暴露底层日志 | 通过 | Browser 失败时 Chat/Run 默认只显示稳定 code、用户文案、action 和技术引用 |
| 技术证据脱敏且可读、整体限长 | 未完全通过 | 已脱敏路径和凭据，但一次真实响应为 8898 bytes，出现多层转义 `summary` |
| Presentation 模板阻止未知字段 | 通过 | `{greeting}` 被静态检查阻止；`{city}/{condition}/{temperature}` 发布和运行成功 |
| JSON 与自然语言双层结果 | 通过 | 深圳天气同时得到结构化 JSON 和“深圳：小雨，22℃”，复制按钮可点击 |
| binding 可逆取证 | 通过 | 数组子串漏洞删除；单值补参显示 `serverVerified (exact)` |
| 亮暗主题与语义色 | 通过 | 亮暗 Settings、Chat、Editor、RunDetail 实际切换正常；亮色自动对比度 7 项 ≥4.5:1 |
| Provider readiness 不隐式付费 | 通过 | `/health/ready` 只读事实，显式 probe 才发起模型调用 |
| 浏览器门禁稳定且不 skip | 通过（正常环境） | 沙箱外连续两次 `2 passed`；受限沙箱内正确失败而非 skip |
| 真实外部 Agent 语义集 | 未验证 | endpoint/model/key 均未配置，只能确认 fixture 协议 |
| Agent Runtime / LLM Runtime 拆分 | 未实现 | Settings、readiness、配置和执行器仍共用单一 Provider 概念 |
| LLM 未配置时由 Agent 执行 | 未实现 | Agent fixture 可用时，真实非 fake LLM 节点仍失败 `MODEL_NOT_CONFIGURED` |

## 3. Runtime 新需求分析

### 3.1 当前实际调用关系

```text
对话与 Agent 节点
  → AgentHost
  → TraeXSubprocessAdapter
  → 当前示例 openai_agent_sidecar
  → SKILLX_MODEL_ENDPOINT

工作流 LLM 节点 / LLM Presentation
  → OpenAICompatibleAdapter
  → SKILLX_MODEL_ENDPOINT
```

Agent Runtime 只是受控规划、Tool Broker 和事件协议边界，不等于 LLM Runtime。当前 OpenAI sidecar 自己需要模型端点，
但一个已自带模型配置的 TraeX Runtime 不应被 Host 强制要求 `SKILLX_MODEL_ENDPOINT`。

### 3.2 需求能否在当前版本实现

不能。当前缺少：

- 独立的 Agent Runtime 与 LLM Runtime 配置、readiness、probe 和 UI 状态；
- 统一 `StructuredGenerationRuntime` 接口；
- 在 LLM Runtime 缺席时将结构化生成请求转交 AgentHost 的 resolver；
- Agent fallback 的无 Tool、单步、严格 Output Schema 限制；
- Run/Usage 中的 `runtimeUsed`、`fallbackReason`、runtime digest 和完整 Token 证据；
- LLM 节点与 LLM Presentation 共用的相同 fallback 语义。

fallback 只能在节点开始前因“LLM Runtime 未配置/未就绪”触发。401、429、timeout 或生成失败后不应静默切换 Agent，
否则会产生双重费用、语义漂移并违反设计文档的入口锁定原则。

## 4. 真实场景测试

使用全新 Alembic 数据库，实际启动 API `8052`、独立 Worker、本地表单/文章站点 `8952`、Vite `5152` 和浏览器。
未使用单测代替以下人工流程。

### 4.1 通过流程

- 新建对话默认开启工作流；“上海天气如何”在 fixture 下诚实地追问 city，回复“上海”后恢复原 selection，
  服务端显示 `city ← 上海 · 100% · serverVerified (exact)` 并完成天气 Run。
- 关闭工作流后，下一 Turn 只走通用 Agent，产生 Agent 轨迹和稳定耗时/Token，不出现 `workflow.*`。
- 手动调用天气组件成功；RunDetail 展示三个节点、事件、依赖和 Artifact；组件详情刷新后出现 1 条历史。
- 工作流编辑器中 `{greeting}` 被 `PRESENTATION_TEMPLATE_INVALID` 阻止。
- 改为 `{city}：{condition}，{temperature}℃` 后静态检查、发布 v2、手动运行均成功；JSON 与阅读结果同时出现。
- 亮色和暗色在 Chat、Settings、Workflow Editor、RunDetail 均可切换并持久化；配色层级清晰，无白块或不可读状态字。
- 组件筛选、历史链接、工作流列表、保存、静态检查、发布、手动运行、复制 JSON/文本均可点击。

### 4.2 LLM fallback 失败复现

在同一真实进程环境中保持 Agent Runtime=`fixture`，不配置任何模型 endpoint；将“反馈分类”工作流的 fake LLM 改为
真实 LLM 节点并发布 v2，然后运行输入“这个产品很好”。Run `4b05ed20-ba7b-4b05-b65a-e140e3fde62d` 结果：

```text
Agent Runtime: configured (fixture)
LLM Runtime: unconfigured
LLM node: failed
error.code: MODEL_NOT_CONFIGURED
modelUsage: []
```

这直接证明新增 fallback 需求尚未实现。

### 4.3 Browser 环境边界

默认受限沙箱内，Worker 的 Playwright 因 macOS MachPort `Permission denied (1100)` 失败；产品默认页面正确显示用户安全错误。
同一 `make browser-ui-check` 在沙箱外连续两次通过，分别为 `2 passed in 23.91s` 和 `2 passed in 22.13s`，
包括首次探索、固化、二次自然语言复用、Presentation、503 接力、响应式尺寸、主题和 console 检查。
因此浏览器代码主路径可用，沙箱失败属于运行权限边界；部署前仍应做 Browser Runtime 启动预检。

## 5. 自动化记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| 后端 Ruff / Pytest | 通过；283 passed |
| Alembic | `0001→0018` 通过 |
| 前端 TypeScript / ESLint / build | 通过 |
| 前端 Vitest | 11 files / 48 passed |
| 真实进程 E2E | 34 passed in 128.01s |
| `make browser-ui-check`（受限沙箱） | 连续两次 2 failed，均为 Chromium MachPort 权限 |
| `make browser-ui-check`（沙箱外） | 连续两次 2 passed |
| 亮色对比度 | 7 项均 ≥4.5:1 |
| `git diff --check` | 通过 |

## 6. 最终判定

当前版本已把 021 的多数用户可见问题关闭，且正常本地环境的“探索—固化—复用—失败接力”fixture 主流程可以跑通。
但 022 新增的 Runtime 架构与 fallback 是明确的核心功能，不是文档补充；真实非 fake LLM Run 已证明它尚不存在。
完成 [`NEXT_STEPS-022.md`](../STEP/NEXT_STEPS-022.md) 并补齐真实 Provider 证据后才能判定通过。
