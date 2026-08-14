# 设计与规范达成度审查报告（034）

审查日期：2026-08-13

依据：[`REPLAY-033.md`](../REPLAY/REPLAY-033.md)、
[`NEXT_STEPS-033.md`](../STEP/NEXT_STEPS-033.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`3bad7d3` 加当前工作区中 REPLAY-033 对应修改。本轮只新增 034 三份审查文档，不修改业务代码。
范围仍为单用户 Local Demo；按既定约束，Token 上限和消耗不作为缺陷或通过条件。

## 1. 结论

**主流程通过；整体验收不通过。**

REPLAY-033 针对 033 的两个主阻断已经修复并在真实栈成立：

1. 固化工作流的 LLM 节点实际消费 resolved inputs，q4、q1、q3 均返回与上游证据一致的业务结果；
2. Agent 选择 Fragment 时，服务端发现缺少 L2 后会在同一选择流程中继续披露，再执行同一不可变版本；
3. 对话、固化、发布、手动运行、组件历史和新对话复用全部成功收口，没有 500、`processing` Turn 或 `running` Run 残留；
4. 四个自动门禁全部通过，真实 Provider 与真实产品门禁均实际执行，未 skip。

但本轮明确要求同时验收前端配色。人工切换到亮色主题后，执行轨迹抽屉仍使用固定深色背景 `#0e191f`，标题却继承
亮色主题正文色 `#172126`，对比度仅约 **1.09:1**；空态说明文字也只有约 **3.23:1**。轨迹标题和内容肉眼难以辨认，
因此不能声明 034 完全通过。该问题不阻断主闭环，但违反现有亮色主题可读性要求。

## 2. NEXT_STEPS-033 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| LLM/Agent fallback 实际收到已解析节点输入 | 通过 | q4/q1 Run 的 LLM 节点输入、事件和模型用量均持久化；`runtimeUsed=agent_fallback` |
| 最终内容与 ToolResult 一致 | 通过 | q4 返回“第四季度营收增长 15%”；q1 返回“第一季度营收增长 12%”；q3 返回“第三季度营收持平” |
| `run_full`/`run_fragment`/`compose` 满足披露契约 | 通过 | 单元回归覆盖三类终态；真实 q3 选择 `run_fragment` 并完成 L0、L1、L2 |
| 不完整披露不再产生 500 或遗留 processing | 通过 | 真实 q3 由服务端诊断后继续 `describe_fragment`；数据库中 Turn/ConversationRun/Run 全部 succeeded |
| q2 探索、固化和发布 | 通过 | 真实 Agent 返回 q2 正确事实；固化草稿、接受 Presentation、静态检查和发布 v1 均成功 |
| q4 手动执行 | 通过 | 四节点全部 succeeded，结构化结果与阅读结果均包含“第四季度/15%” |
| q3 新对话复用 | 通过 | 默认开启工作流，Agent 自主选择 Fragment、校验参数来源并执行；返回“第三季度/营收持平” |
| 历史、详情与复制入口 | 通过 | 对话历史、组件 1 条调用历史、RunDetail 事件/结构化结果/阅读结果/用量均可查看，复制按钮自动门禁通过 |
| 亮暗主题均正常可读 | **未通过** | 暗色正常；亮色执行轨迹抽屉为深底深字，标题对比度约 1.09:1 |
| 通配型闭环 | 通过 | 当前既定“探索—固化—发布—复用—披露补全—执行”目标真实跑通，未加入领域模板或新需求 |

## 3. 自动门禁

| 检查 | 结果 |
|---|---|
| REPLAY-033 定向回归 | 通过；62 passed |
| `make demo-check` | 通过；358 backend、56 frontend、37 独立进程 E2E、迁移 0001→0023、build 与文档链 |
| `make browser-ui-check` | 通过；2 passed，真实启动 API、Worker、Vite 并点击页面 |
| `make traex-real-check` | 通过；1 passed，未 skip |
| `make traex-real-product-check` | 通过；4 passed，包含 q4/q3 业务语义和 L0/L1/L2 断言 |

自动门禁没有覆盖“固定深色组件背景 + 亮色主题变量文字”的组合对比度，因此未发现本轮人工验收暴露的轨迹抽屉问题。

## 4. 真实场景结果

### 4.1 运行环境

启动全新 SQLite、API（8000）、独立 Worker、本地网页站点（18042）和 Vite（5173），再由系统页执行真实主动探测。
结果为：SQLite available、Worker available、Browser Runtime available、Agent Runtime available、工作流选择 Provider real；
Direct LLM Runtime 未配置时，实际按设计使用已验证的 TraeX Agent fallback。

### 4.2 首次探索、固化与手动运行

新对话关闭工作流后提交：

`查询标识 q2 对应的本地示例数据并用一句中文告诉我结果`

真实 Agent 返回“第二季度：营收增长 8%”，页面展示 11.0 秒和 32094 Token，并持久化历史和轨迹。随后从同一消息固化草稿，
接受面向最终用户的稳定模板提议，通过静态检查并发布不可变 v1。

从工作流编辑器输入 q4 后，Run 四节点全部 succeeded；LLM 节点通过 `agent_fallback` 调用，结构化结果与阅读结果均为
“查询结果：第四季度营收增长15%”。从组件详情输入 q1 后，同样正确返回“第一季度：营收增长 12%”；返回详情页后可见
1 条 succeeded 调用历史，包含输入、输出、耗时、版本和 Presentation 状态。

### 4.3 新对话渐进披露并复用

新建对话时工作流默认开启。提交 q3 后，真实选择 Agent 先后执行 L0 search、L1 describe；首次终态选择 Fragment 时尚缺 L2，
服务端没有进入执行或抛 500，而是要求同一 Agent 继续 `describe_fragment`。L2 成功后，系统以 99% 置信度、服务端已校验的
`source_span_v1` 将 `key` 绑定为 `q3`，执行同一不可变 Fragment。

最终 Turn 为 succeeded，结构化结果为 `{"reply":"结果是第三季度：营收持平。"}`。过程中模型两次给出不符合 catalog schema 的
工具参数，Host 均返回 `INVALID_TOOL_ARGUMENTS`，Agent 自行纠正后完成任务；这验证了错误可观测和恢复路径，没有形成主流程阻断。

### 4.4 状态与日志

真实验收结束前数据库状态为：2 个 ConversationTurn succeeded、1 个 ConversationRun succeeded、3 个 Run succeeded、
0 个 PendingSelection；不存在 processing/running。API、Worker 和表单日志无 Traceback、500、`database is locked` 或 ERROR，
浏览器 console 无 error/warning。验收后 5173、8000、18042 均已停止监听。

## 5. 前端验收

已实际点击并确认：主导航、组件筛选、系统主动探测、对话历史、新建对话、工作流开关、发送、固化、Presentation 接受、
静态检查、发布、工作流手动运行、组件手动运行、组件历史、RunDetail、结构化结果与阅读结果。自动 Browser 门禁另覆盖复制等交互。

暗色主题配色和布局正常。亮色主题主体、侧栏、卡片、输入框与按钮正常，但执行轨迹抽屉不可读：抽屉背景写死为深色，内部文字
仍继承亮色全局变量。这是本轮唯一阻止“完全通过”的问题。

## 6. 最终判定

REPLAY-033 的主闭环修复 **通过**，当前版本已经能完整跑通预期功能；034 整体验收因亮色主题轨迹抽屉可读性缺陷
**不通过**。下一步只修这一项现有缺陷并补对应回归，不新增功能，见
[`NEXT_STEPS-034.md`](../STEP/NEXT_STEPS-034.md)。

