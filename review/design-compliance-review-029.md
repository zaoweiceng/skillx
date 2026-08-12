# 设计与规范达成度审查报告（029）

审查日期：2026-08-12

依据：[`REPLAY-028.md`](../REPLAY/REPLAY-028.md)、
[`NEXT_STEPS-028.md`](../STEP/NEXT_STEPS-028.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`959848a`。审查对象为 REPLAY-028 修复后的已提交代码；本轮只新增 029 三份文档，
不修改业务代码。范围仍为单用户 Local Demo，不审查鉴权、RBAC、多租户或分布式部署。

## 1. 结论

**不通过。**

028 对原生 TraeX JSONL 终态、三维 Token 预算、真实 Provider probe 和 Presentation
提议/审阅的修复大部分在代码与工程门禁层面成立。真实 `traex` 主动探测本轮首次通过：完成随机
challenge、Host ToolResult、严格 JSON 和 Usage，Provider 进入 `real/usable`；完整工程门禁、
Browser UI 门禁也全部通过。确定性工作流在实际启动的 API、Worker、Vite、Chromium 中可编辑、发布、
运行和回看，前端主要按钮与历史均正常。

但“主流程能完整跑通”的首要目标仍未达到：

1. 默认开启工作流时，真实语义选择一次性向 TraeX 注入五个目录工具，仍沿用旧的 8,000 Token
   单一预算。输入“上海天气如何”实际产生 67,165 Token，29.453 秒后以
   `TOKEN_BUDGET_EXCEEDED` 失败，未完成 L0/L1 发现、参数绑定或工作流执行。
2. 关闭工作流后，最简单的“只回复 029-agent-ok”虽到达真实模型，但通用 Agent 未被明确要求返回
   `{reply:string}` JSON。TraeX 返回普通文本，4.693 秒后以 `MODEL_MALFORMED_OUTPUT` 失败。

因此真实 Provider 可用不等于产品入口可用；首次探索、固化、第二次语义复用、失败接力和修订无法在
真实主路径中串成闭环，本版不能声明通过。

## 2. REPLAY-028 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| 原生 JSONL 成功/失败终态分类 | 通过 | fixture 回归与真实成功 probe 均通过；malformed output 得到独立错误码 |
| 输入、输出、总费用预算分离 | 部分通过 | Host/Probe 已分离；WorkflowSelection 仍只给 `max_total_tokens=8000` |
| 真实 Provider 主动探测 | 通过 | 31,285 input、197 output、31,482 total，Tool round-trip/strict output 均成立 |
| Provider 可用性独立投影 | 基本通过 | `real`、`usable`、认证/模型/协议/Tool 状态可查询；成功后仍显示“凭据未验证”提示 |
| 默认开启工作流语义选择与同消息绑定 | **未通过** | 67,165 Token 后预算失败，未启动天气组件 |
| 关闭工作流由通用 Agent 回答 | **未通过** | 真实模型返回非 JSON，`MODEL_MALFORMED_OUTPUT` |
| Presentation 由受限 Agent 提议 | 部分通过 | Schema、fallback、审阅门禁已实现；consumer/entrypoint 仍为硬编码 unknown，Fragment 事实未进入提议 |
| Presentation 明确接受/修改/拒绝 | 通过（fixture/编辑器） | 提议证据、三种动作、digest 绑定和发布阻断均有门禁 |
| 真实 Agent 首次探索后固化 | **未通过** | 两条 Chat 入口均失败，无法产生可固化的真实成功轨迹 |
| 已发布确定性组件执行与历史 | 通过 | 实际发布问候工作流，3 节点成功，JSON/阅读结果/历史/RunDetail 可回看 |
| 前端主题、导航和主要点击 | 通过（有次要显示问题） | 主题持久化、五页导航、筛选、编辑、检查、发布、复制和历史均可点击；console 为空 |
| 完整工程门禁 | 通过 | 326 backend、52 frontend、37 独立进程 E2E；Browser UI 2/2 |
| 真实 Provider gate | 通过但覆盖不足 | 1 passed；只覆盖 probe，没有覆盖真实 Chat、取消和失败接力 |

## 3. 真实环境验收

### 3.1 自动化门禁

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；Ruff、326 backend、迁移 0001→0023、52 frontend、build、37 E2E |
| `make browser-ui-check` | 通过；2 passed |
| `make traex-real-check` | 通过；1 passed，未 skip |
| 真实应用 Provider probe | 通过；provider=traex，protocol=`traex-cli-jsonl+mcp`，31,482 Token |

`make demo-check` 和 Browser UI 使用可复现 fixture/本地服务验证工程契约；真实 Provider gate 使用 PATH
中的实际 `traex`。三者互不替代。

### 3.2 实际启动与 Chat 主路径

本轮使用全新 SQLite 与 artifact 目录，独立启动 API（18029）、Worker 和 Vite（15174），再用 Chromium
操作页面。

**工作流开启：**

- 输入：`上海天气如何`；
- 结果：29.453 秒后失败，UI 显示“Agent 输入上下文超过 Host 上限 8000”；
- Usage：input 66,220、output 945、total 67,165，状态 partial；
- 审计：stage=`workflow_selection`、dispatched=true、completed=false、
  error=`TOKEN_BUDGET_EXCEEDED`；
- 未产生 assistant 结果、候选、参数绑定或 Workflow Run。

**工作流关闭：**

- 页面实际点击“工作流：开启”切换为“工作流：关闭”；
- 输入：`请只回复 029-agent-ok，不要调用任何工具`；
- 结果：4.693 秒后失败，`MODEL_MALFORMED_OUTPUT`；
- Usage：input 15,664、output 32、total 15,696，状态 partial；
- 运行事件完整显示 created/started/dispatched/step completed/failed，且没有 `workflow.*` 调用。

这两次失败都发生在真实模型调用之后，不是服务未启动、认证、网络或 fixture 问题。

### 3.3 确定性流程与前端交互

在同一真实栈中完成了以下操作：

- 组件四类筛选可切换，主导航可进入对话、组件、工作流、能力和系统页；
- 暗色主题可切换，刷新后保持；Browser UI 门禁同时覆盖亮色与多种宽度；
- 创建示例工作流，切换/应用 JSON，执行静态检查并发布不可变版本；
- 在组件详情填写姓名 `029真实界面`，启动真实 Worker，input/transform/output 三节点成功；
- RunDetail 同时显示 `{"message":"你好，029真实界面"}` 与模板生成的阅读结果；
- “复制 JSON”、返回组件、调用历史和历史 Run 链接均可点击，控制台无 error；
- 调用历史刷新后仍为 succeeded，输入、输出、耗时和 Presentation 状态完整。

暗色整体布局和对比度正常。DAG 两条无条件边显示了白色 `null` 标签，属于前端空值处理缺陷，
不阻断运行但应修复。

## 4. 预期闭环能否完成

当前能独立完成“人工创建/发布确定性工作流—手动复用—查看历史”，但不能从自然语言入口完成设计文档
要求的通配型闭环：

```text
真实对话意图
  ├─ 工作流开启 → WorkflowSelection 预算失败
  └─ 工作流关闭 → 通用 Agent 终态 Schema 失败

两条路径均无法产生成功 Agent trajectory
  → 无法真实固化
  → 无法由下一次对话渐进发现并复用
  → 无法验证失败接力后的固化修订
```

修复重点不是增加天气模板或调高单一常量。工作流目录的渐进披露必须同时约束模型上下文：当前虽要求先
search L0，却预先暴露了五个完整 Tool Schema，已经违背“按需披露”的成本目的。通用 Agent 则必须由 Host
构造不可被用户原文覆盖的严格输出契约提示。天气继续只作为一个测试样本，不得形成任何领域分支。

## 5. 最终判定

原生 TraeX probe、确定性 Runtime、Presentation 审阅框架、历史和主要 UI 通过；真实 Chat 两条入口与
完整探索闭环未通过。因此 029 **不通过**。修复任务和下一版通过条件见
[`NEXT_STEPS-029.md`](../STEP/NEXT_STEPS-029.md)。
