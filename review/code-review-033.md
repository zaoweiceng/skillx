# 代码审查报告（033）

审查日期：2026-08-13

代码基线：`3bad7d3`。审查对象为 [`REPLAY-032.md`](../REPLAY/REPLAY-032.md) 对应代码。
本轮未修改业务代码；Token 上限和消耗不纳入缺陷判定。

## 1. 结论

**不通过：2 个 P0、1 个 P1。**

Browser attempt 过滤、Host-owned Contract、整体输出引用和中文 Descriptor 修复有效。剩余问题都直接影响现有主闭环，
不涉及新增产品能力。

## 2. P0 Findings

### P0-1：LLM 节点保存了证据输入，却没有把证据放入模型 prompt

位置：[`distillation.py:475`](../backend/app/application/distillation.py#L475)、
[`executors.py:423`](../backend/app/runtime/executors.py#L423)。

固化器为 generative output 创建 `final-response`，其节点 input 正确引用 `${node.output}`；但 config prompt 只是
“仅根据输入的已验证证据生成……”，没有 `{input}`。`LLMExecutor` 只用字符串 `replace("{input}", ...)` 注入输入，
因此没有占位符时模型根本看不到 resolved inputs。

真实 q4 Run 的持久化节点输入明确包含“第四季度：营收增长 15%”，模型输出却是“完成一次严格结构化生成”。q1 与 q3
同样返回关于 Schema 的通用句子。JSON Schema 只能约束形状，不能证明内容来自证据；把这种 Run 标为 succeeded 会向用户
返回事实错误的结果。

根因修复应把“节点输入必定进入模型请求”定义为运行时契约：Executor 构造规范化、带边界的输入区段，不依赖某个生成器
记得写占位符；同时生成器仍可显式使用 `{input}`。发布/回归必须断言实际业务值，而不只是输入非空和状态成功。

### P0-2：选择终态未校验披露等级，`CatalogError` 逃逸为 500 并留下 processing Turn

位置：[`routes.py:4188`](../backend/app/api/routes.py#L4188)、
[`routes.py:4480`](../backend/app/api/routes.py#L4480)、
[`routes.py:5307`](../backend/app/api/routes.py#L5307)。

真实选择 Agent 可在 Workflow L1 中看到 Fragment ref，并直接返回 `run_fragment`。当前 `_select_workflow_candidate()` 只校验
`WorkflowSelection` 的数据形状，不校验该终态是否已完成所需披露。随后 `_extract_selection_bindings()` 调用
`_selection_input_contract()`；后者正确要求 Fragment L2，但抛出的 `CatalogError` 不在该层和 `create_message()` 的
`except HTTPException` 范围内。

保留日志的真实复现栈精确落在：

`_selection_input_contract → CatalogError: run_fragment 必须先调用 workflow.describe_fragment 披露 L2`

结果是 HTTP 500、Turn 永久 `processing`、没有 assistant 终态消息。`make traex-real-product-check` 和两次隔离重跑均在
同一 q3 复用阶段失败；这不是网络波动。

选择循环必须在接受模型终态前校验 action 所需的 L1/L2/compiled plan，并把不足反馈给同一选择 Agent 在剩余预算内继续；
边界层还必须把所有 Catalog/Plan 契约异常转换为持久化的结构化错误，保证 Turn 终态，不能依赖模型总会按提示调用工具。

## 3. P1 Finding

### P1-1：真实产品门禁只验证“模型收到值且 Run succeeded”，没有验证回答正确

位置：[`test_traex_product_real.py:216`](../backend/real_provider_tests/test_traex_product_real.py#L216)。

新增用例只断言 LLM 节点 succeeded 且 `input.values()` 非空；q3 复用也只断言 Run succeeded 和消息持久化。于是 q4 输出
“完成一次严格结构化生成”仍能通过执行阶段断言。REPLAY-032 中“真实产品闭环通过”的结论因此可能是假阳性。

门禁必须校验已知真实 ToolResult 与最终结构化/文本结果的语义一致性：q4 包含“第四季度/15%”，q3 包含
“第三季度/营收持平”。同一断言要覆盖手动版本运行和新对话复用，不能只检查 DAG、非空 input 或 HTTP 状态。

## 4. 已确认有效的 032 修复

- 真实 Browser failed→succeeded 轨迹可固化，失败 attempt 只保留为诊断；
- Browser 成功动作携带可编译的 Host-owned PageContract；
- `${node.output}` 在运行时返回完整对象，前端变量选择器语义一致；
- 可分析的固化拒绝可持久化为 blocked，前端刷新可读取诊断；
- 中文源任务生成中文能力描述，新对话可以发现刚发布版本；
- 亮暗主题、历史、编辑、发布、组件运行、RunDetail 和复制交互可用。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 355 backend、56 frontend、37 E2E 全通过 |
| `make browser-ui-check` | 2 passed |
| `make traex-real-check` | 1 passed，未 skip |
| `make traex-real-product-check` | **3 passed、1 failed**；q3 新对话返回 500 |
| 隔离真实闭环重跑 | **连续两次失败**；同一阶段 500 |
| 保留日志复现 | `run_fragment` 缺 L2 的 `CatalogError` 未捕获；Turn 留在 processing |
| 真实 Browser 重试固化 | 通过；一次失败、一次成功、正确总结 12%、草稿可发布 |
| q4/q1/q3 结果语义 | **失败**；输入证据正确，输出为通用 Schema 文案 |
| 主题/主要 UI/console | 点击正常；console error/warning = 0 |

下一版修复任务见 [`NEXT_STEPS-033.md`](../STEP/NEXT_STEPS-033.md)。

