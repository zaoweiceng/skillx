# 设计与规范达成度审查报告（033）

审查日期：2026-08-13

依据：[`REPLAY-032.md`](../REPLAY/REPLAY-032.md)、
[`NEXT_STEPS-032.md`](../STEP/NEXT_STEPS-032.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`3bad7d3`，审查开始时工作区无未提交改动。本轮只新增 033 三份文档，不修改业务代码。
范围仍为单用户 Local Demo；按既定约束，Token 上限和消耗不作为缺陷或通过条件。

## 1. 结论

**不通过。**

REPLAY-032 的三项核心修复均有真实证据：真实网页第一次 `browser.read` 失败、第二次成功后可以固化；
`${node.output}` 已把完整 lookup 输出传入 LLM 节点；中文源任务生成了中文能力描述并能在新对话中被发现。

但完整闭环仍未达到“正确且稳定地跑通”：

1. 固化生成的 LLM 节点虽有非空 `input`，实际 prompt 没有注入该输入，q1/q3/q4 都返回与证据无关的通用句子；
2. 真实选择 Agent 会选择 L1 中披露的 Fragment，但未必继续披露 L2。服务端随后抛出未捕获的 `CatalogError`，
   对话接口返回 500，并留下永久 `processing` Turn；
3. `make traex-real-product-check` 因上述问题稳定失败，当前门禁又没有检查运行结果的业务语义。

因此当前可以完成“探索—固化—发布—执行”的结构动作，但不能保证得到用户所需结果，也不能稳定完成新对话复用。

## 2. REPLAY-032 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| 失败 attempt 与可执行 action 分离 | 通过 | 真实 Browser 一次失败、一次成功；失败只形成 `FAILED_ATTEMPT_DIAGNOSTIC` |
| Host-owned Browser Contract | 通过 | 成功重试编译为 Browser 节点，固化返回 201、`draft_created`、`publishable=true` |
| `${node.output}` 完整对象语义 | 通过 | q4 的 LLM 节点输入为 `{"evidence1":{"value":"第四季度：营收增长 15%"}}` |
| 发布版本实际调起 LLM/Agent fallback | 通过 | q4 Run 四节点均 succeeded，记录 `runtimeUsed=agent_fallback` |
| 能力描述与源任务语言一致 | 通过 | 中文任务生成中文 name/summary/useWhen/examples/tags |
| 新对话能发现新发布能力 | 通过 | q3 新对话可披露并选择同一不可变版本或其 Fragment |
| 渐进式披露后稳定执行 | **未通过** | Agent 选择 Fragment 但只有 L1 时，接口抛 500 |
| 模型节点基于上游证据生成正确结果 | **未通过** | q4 证据为“营收增长 15%”，输出却是“完成一次严格结构化生成” |
| 对话错误形成可恢复终态 | **未通过** | 500 后 Turn 留在 `processing`，没有 assistant error 消息 |
| 亮暗主题、主要按钮和历史 | 通过 | 真实浏览器点击主题、开关、历史、固化、检查、发布、运行、复制均正常 |
| 通配型完整闭环 | **未通过** | 结构可运行，但语义结果错误且复用选择存在稳定 500 分支 |

## 3. 自动门禁

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；355 backend、56 frontend、37 独立进程 E2E、迁移 0001→0023、build 与文档链 |
| `make browser-ui-check` | 通过；2 passed |
| `make traex-real-check` | 通过；1 passed，未 skip |
| `make traex-real-product-check` | **失败**；3 passed、完整复用闭环 1 failed，新对话提交返回 500 |
| 隔离重跑完整真实闭环 | **连续两次失败**；均在 q3 新对话复用返回 500 |
| 保留日志的第三次复现 | **失败**；精确栈为 `run_fragment` 未披露 L2 导致 `CatalogError` |

## 4. 真实场景结果

### 4.1 Browser 失败重试后可固化

全新 API、Worker 和本地网页站点中，真实 TraeX 执行：

`访问 http://127.0.0.1:<port>/article，读取当前正文并用一句中文总结`

第一次 `browser.read` 返回失败，第二次成功，最终正确总结“本季度营收增长 12%”。随后固化返回 201，创建可发布草稿；
分析中保留失败 attempt 的审计诊断，但 DAG 不包含失败步骤。032 对 Browser 轨迹的修复成立。

### 4.2 lookup 可以发布和执行，但结果语义错误

真实 UI 完成 q2 探索、固化、Presentation 审阅、静态检查、发布 v1，并分别从工作流编辑器和组件详情运行 q4、q1。
运行都显示 succeeded，LLM 节点也确实收到正确上游对象，但输出分别变成：

- q4：`完成一次严格结构化生成。`
- q1：`仅根据已提供的输入，返回符合 outputSchema 的 JSON 对象，且未调用任何工具。`

期望分别是“第四季度：营收增长 15%”和“第一季度：营收增长 12%”。运行状态成功不能替代任务结果正确。

### 4.3 新对话复用有成功分支，也有稳定 500 分支

一次 UI 手动执行中，q3 新对话完成 L0/L1、选择 `run_full` 并启动同一 v1，说明中文 manifest 与语义发现修复有效；
但返回内容仍是通用句子，不是“第三季度：营收持平”。

自动真实门禁及两次隔离复现中，Agent 从 Workflow L1 看到 Fragment 后直接选择 `run_fragment`，没有调用
`workflow.describe_fragment` 获取 L2。服务端 `_selection_input_contract()` 抛出：

`run_fragment 必须先调用 workflow.describe_fragment 披露 L2`

该异常没有被对话入口转换为结构化错误，最终 HTTP 500；对应 Turn 仍为 `processing`，且只有用户消息。

## 5. 前端验收

实际启动 Vite、API、Worker 后通过的交互包括：

- 亮色/暗色切换；两套主题对比度、卡片、表单和 DAG 可读；
- 对话历史、新建对话、默认开启工作流、关闭后走通用 Agent、计时与 Token 展示；
- 固化入口、Presentation 接受、静态检查、发布版本；
- 组件类型筛选、组件参数运行、调用历史回看；
- RunDetail 节点输入/输出、事件、用量、结构化结果、文本结果和复制 JSON；
- 浏览器 console error/warning 为空。

前端没有造成主闭环中断。它如实展示了后端的错误语义结果；500 分支则因服务端未返回可持久化 assistant 消息而无法恢复。

## 6. 最终判定

032 修复有效，但“执行成功”目前不等于“任务完成”，渐进式选择也存在稳定未捕获分支。033 **不通过**。
下一步只修复这两个主阻断和对应门禁，见 [`NEXT_STEPS-033.md`](../STEP/NEXT_STEPS-033.md)。

