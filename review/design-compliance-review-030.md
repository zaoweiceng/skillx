# 设计与规范达成度审查报告（030）

审查日期：2026-08-12

依据：[`REPLAY-029.md`](../REPLAY/REPLAY-029.md)、
[`NEXT_STEPS-029.md`](../STEP/NEXT_STEPS-029.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`a59a7c1`，审查对象为工作区内 REPLAY-029 对应的未提交修复代码。本轮只新增 030 三份
审查文档，不修改业务代码。范围仍为单用户 Local Demo，不审查鉴权、RBAC、多租户或分布式部署。

## 1. 结论

**不通过。**

029 修复了上版两个直接阻断：统一严格 JSON 终态后，工作流关闭的真实 Agent 回答可以成功；目录工具也已
收敛为单个 `workflow.catalog` 描述符。Settings 主动探测状态、DAG `null`、复制反馈、Presentation 决定语义
和确定性组件历史均已修复。完整工程门禁、Browser UI 门禁、真实 TraeX 快速 probe 均通过。

但默认开启工作流的真实主路径仍未一次完成。用户在同一消息明确给出 `Alice` 时，真实 Agent 找到了正确的问候
工作流，却返回空 `bindings` / `bindingSources`，系统随后要求补充 `name`；第二条消息只发送 `Alice` 后才在
0.6 秒内成功运行。该行为违反“由 LLM/Agent 判断、从本轮输入传递合适参数”的核心要求，也使默认开启工作流的
体验仍不能像普通聊天助手一样一次解决问题。

同时，新增的 `make traex-real-product-check` 本身使用了已废弃的 Provider 配置，并在取消用例显式提交空能力集，
2 个用例均在真实产品断言前失败。因此 REPLAY-029 所称的产品门禁尚未真正建立。

## 2. REPLAY-029 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| 唯一严格 Agent JSON 终态 | 通过 | 工作流关闭的真实 UI 对话成功返回 `030-ui-agent-ok`，没有 `workflow.*` 事件 |
| 单一窄目录描述符与服务端 disclosure 状态机 | 基本通过 | 请求只授予 `workflow.catalog`；真实对话完成 search/describe 并选中问候工作流 |
| 同消息参数提取与 binding source 验证 | **未通过** | 同一句中两次出现 Alice，仍返回空绑定并追问；第二条只回复 Alice 才成功 |
| 无匹配时继续 Agent、开启/关闭模式隔离 | 部分通过 | 关闭模式已真实通过；无匹配的真实通用场景本轮未形成完整闭环证据 |
| 真实 Provider 主动探测 | 通过 | 实际 TraeX 完成 challenge、ToolResult、严格 JSON 与 Usage，total 31,734 Token |
| 产品级真实 Provider 门禁 | **未通过** | 2 failed；运行时配置错误，取消请求又被空 capability guard 拒绝 |
| 真实运行中取消 | 通过（人工修正配置） | 允许 `lookup.table` 后真实进程进入 running，再到 cancelled |
| Presentation consumer/entrypoint 事实 | 代码/fixture 通过 | trajectory 与提议输入已携带语义字段；真实固化样本仍不足 |
| accepted / modified / rejected 审计语义 | 通过（代码/测试） | 服务端拒绝 accepted 漂移、modified 未修改；UI 编辑后重置决定 |
| 设置状态展示 | 通过 | 主动 probe 后显示 real/usable，不再提示重复探测；probe/selection profile 可查询 |
| 工作流编辑、发布、手动运行与历史 | 通过 | 实际发布 v1、真实 Worker 运行成功、详情/历史/JSON 复制均可用 |
| 对话历史、主题、计时与 Token | 通过（本轮路径） | 多轮消息持续显示；暗色主题正常；两条真实 Agent 路径显示耗时和 Token |
| 前端主要点击与控制台 | 通过（有次要 UX 问题） | 导航、筛选、模式切换、编辑、检查、发布、运行、复制均可点击；console 为空 |
| 通配型完整闭环 | **未通过** | 真实首次参数绑定未一次完成；跨领域固化、复用、接力、修订证据仍缺失 |

## 3. 真实环境验收

### 3.1 自动化门禁

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；Ruff、331 backend、迁移 0001→0023、53 frontend、build、37 独立进程 E2E |
| `make browser-ui-check` | 最终通过；2 passed。首次受 macOS Chromium sandbox 权限阻断，改在宿主权限下复跑通过 |
| `make traex-real-check` | 通过；1 passed，未 skip |
| `make traex-real-product-check` | **失败；2 failed，均未验证预期产品路径** |

快速 probe 只能证明真实 binary、认证、协议、ToolResult 和严格终态可用；它不能替代 Chat、工作流执行和取消门禁。

### 3.2 全新真实前后端与默认工作流对话

本轮使用全新 SQLite 和 artifact 目录，实际启动 API、独立 Worker 与 Vite，并通过应用内 Chromium 点击页面。
运行时使用当前支持的 TraeX 自动发现配置；主动 probe 成功，input 31,551、output 183、total 31,734。

工作流默认开启时，在 UI 输入：

`请问候名叫 Alice 的人，姓名参数是 Alice`

结果：

- 43.3 秒后返回 `ask_user`，累计 104,535 Token；
- Agent 正确发现并选择“确定性问候”工作流；
- 返回的 `bindings` 和 `bindingSources` 均为空，页面要求补充 `name`；
- timeline 可见 search/describe 失败后重试，但失败项没有可操作诊断；
- 再发送 `Alice` 后，系统复用待定选择，在 0.6 秒、0 Token 的确定性执行中返回
  `{"message":"你好，Alice"}`，来源验证为当前用户消息。

这证明目录发现、待定选择和运行恢复是可用的，但也证明“参数明明在首条消息中却被追问”的核心问题尚未修复。

### 3.3 工作流关闭与真实 Agent

在新对话中实际关闭工作流，输入：

`请只回复 030-ui-agent-ok，不要调用任何工具`

9.3 秒后成功显示 `030-ui-agent-ok`，total 15,845 Token，执行轨迹中没有 `workflow.*`。这验证了统一严格
终态和“关闭后全程使用 LLM/Agent”的本轮修复。

成功后页面仍显示“固化为组件”。实际点击得到“轨迹不含任何可分析动作”。后端拒绝无动作轨迹是正确的，前端却
在点击前把不可执行操作展示为可用，属于次要但明确的交互缺陷。

### 3.4 确定性流程与前端点击

同一真实栈中还完成了：

- 切换暗色主题并进入设置页；Provider 显示 real/usable，主动探测成功状态与 probe/selection profile 正常；
- 新建示例工作流、切换并应用 JSON、静态检查、发布不可变 v1；DAG 不再显示 `null`；
- 在“确定性问候”组件详情输入 `030界面验收`，真实 Worker 约 270ms 完成三节点执行；
- RunDetail 显示 `{"message":"你好，030界面验收"}`，复制按钮反馈为“已复制 JSON”；
- 返回组件后调用历史仍显示 succeeded、输入、输出、耗时和详情入口；
- 组件筛选、对话模式切换、能力页、工作流页、主要返回按钮均可点击；浏览器 console 无 error/warning。

暗色配色、文字对比和整体布局正常；自动 Browser UI 同时覆盖多种宽度。未发现阻断点击的样式问题。

## 4. 根因判断

这不是问候领域或某个参数名的特例。`WorkflowSelection` 的 Schema 允许 `bindings={}` 与
`bindingSources=[]`，只校验“已有 binding 必须有来源”，没有要求选中工作流后覆盖其必填输入。工作流的必填
Schema 又只在 Agent 返回终态后由服务端解析，所以 Agent 可以输出一个结构合法但语义不完整的选择，随后通用
补参逻辑把它转成 `ask_user`。

正确方向是在选中 ref 并获得真实输入 Schema 后建立受约束的参数提取阶段，或使同一个选择契约携带并强制校验
必填字段；所有值仍必须来自可验证的消息/ToolResult 证据。不得新增关键词、正则或天气/问候模板。

本项目当前处于 Demo 阶段，Token 上限与消耗不作为本版通过条件。报告中的 Token 仅作为运行观测数据，便于确认
模型调用真实发生以及前端统计链路正常；本轮不要求降低消耗或调整上限。

## 5. 最终判定

严格终态、真实 Provider、确定性 Runtime、主要 UI 和历史已经可用；默认开启工作流的一次性参数绑定、可执行的
真实产品门禁以及通配型完整闭环仍未通过。因此 030 **不通过**。修复范围和下一版通过条件见
[`NEXT_STEPS-030.md`](../STEP/NEXT_STEPS-030.md)。
