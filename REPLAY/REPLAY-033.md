# REPLAY-033：修复模型输入丢失与渐进披露非终态

日期：2026-08-13

依据：[`NEXT_STEPS-033.md`](../STEP/NEXT_STEPS-033.md)、
[`code-review-033.md`](../review/code-review-033.md)、
[`design-compliance-review-033.md`](../review/design-compliance-review-033.md) 和
[`REPLAY-032.md`](./REPLAY-032.md)。

## 1. 结论

033 指出的两个主闭环根因已按现有架构修复：LLM 节点现在必定收到 resolved inputs；Workflow selection 在参数提取前
必须满足服务端持有的 L1/L2/compiled-plan 披露事实。披露不足会在原选择预算的剩余 steps 内进入一次受限 continuation，
仍不完整则持久化 `WORKFLOW_SELECTION_DISCLOSURE_INCOMPLETE` assistant error，并把 Turn 置为 `error`，不再返回裸 500
或遗留 `processing`。

本轮没有新增产品功能、API、数据库迁移、领域模板、鉴权、多租户或计费逻辑。修改只收紧既有 LLM 请求、渐进选择终态和
真实产品门禁。

## 2. P0-1：resolved inputs 成为 LLM 运行时硬契约

[`LLMExecutor`](../backend/app/runtime/executors.py) 新增统一 prompt 构造：输入先以 `ensure_ascii=false`、key 排序、紧凑
分隔符序列化为规范 JSON，再放入唯一的 `[RESOLVED_INPUT_JSON]...[/RESOLVED_INPUT_JSON]` 边界。

- prompt 有 `{input}` 时只在第一次出现处注入输入区段，后续重复占位符变为已注入标记，不复制证据；
- prompt 没有 `{input}` 时自动追加输入区段，不再依赖固化器记得写占位符；
- fake 与真实 StructuredGeneration 共用同一个最终 prompt，运行时路径没有分叉。

[`compile_agent_trajectory()`](../backend/app/application/distillation.py) 生成的 `final-response` prompt 同时携带去实例化任务
目标、outputSchema 字段语义和“仅依据已验证证据”的约束，并明确禁止复述 Schema。具体 q 值、ToolResult 正文和领域模板
不进入固化 prompt；真实输入仍只在执行时由 Executor 注入。

真实产品门禁不再以 `Run succeeded` 或 LLM input 非空作为成功标准：q2 固化发布后，q4 手动运行的最终 `reply` 已断言包含
“第四季度”和“15%”；q3 新对话复用的最终 `reply` 已断言包含“第三季度”和“营收持平”。两条路径均在真实 TraeX、
全新 SQLite、独立 API/Worker 上通过。

## 3. P0-2：披露事实成为 selection 的服务端终态

[`_selection_disclosure_diagnostic()`](../backend/app/api/routes.py) 在 Agent 返回结构化 selection 后、参数提取前按 action
校验服务端事实：

- `run_full`：精确 workflow ref 至少为 L1，且可解析到已发布不可变 manifest；
- `run_fragment`：精确 fragment ref 必须为 L2，且可解析 fragment boundary；
- `compose`：plan 的全部 ref 已披露，当前 plan digest 有成功的 `compile_plan` 调用，并且 Compiler 可再次验证。

首次终态不完整时，系统把原 selection 和结构化 diagnostic 交给同一 selection 能力边界，只允许
`workflow.catalog`，预算为首次运行剩余的 steps/tokens。continuation 只能补所缺的 `describe`、
`describe_fragment` 或 `compile_plan`；补全后再次执行同一服务端校验，action 或不可变 refs 发生变化也拒绝执行。

若剩余 step 为零、continuation 失败或补全后仍不满足，返回
`WORKFLOW_SELECTION_DISCLOSURE_INCOMPLETE`。disclosure calls、模型用量、tool timeline 和 diagnostics 均保留；
`create_message()` 写入 assistant error 后把 ConversationTurn 置为 `error` 和 `completedAt`。此外，参数提取、prepare、execute
阶段的 `CatalogError`、`PlanCompileError`、`WorkflowValidationError` 都在对话边界规范化为既有错误消息结构。

新增确定性回归刻意让第一次 Agent 从 Workflow L1 直接返回 `run_fragment`：正常分支由第二次受限调用补出 L2 后提取参数
并启动 Fragment；预算内仍不补齐的分支返回结构化错误，Turn 为 error，证明不再出现 500/processing。

## 4. P1：真实闭环门禁检查结果与披露语义

[`test_real_product_distill_publish_discover_and_reuse`](../backend/real_provider_tests/test_traex_product_real.py) 现在同时断言：

1. q4 最终业务值为“第四季度/15%”；
2. q3 最终业务值为“第三季度/营收持平”；
3. selection 的成功 timeline 至少包含 search L0、Workflow L1；选择 Fragment/compose 时必须包含 L2；
4. 最终 action 为现有三种执行形态之一，binding source 全部由服务端验证；
5. 执行 ref 仍锁定刚发布的不可变 workflow version 或其 Fragment。

032 的真实 Browser failed→succeeded→distill 形态未修改；完整 `demo-check`、Browser UI 和真实产品门禁通过，说明本轮收紧
selection 没有回归 Browser 固化与既有执行流程。

## 5. 最小修改边界

本轮复用现有 `AgentHost`、`workflow.catalog`、DisclosureSession/Call、动态 binding、Compiler、Turn 和错误消息结构。
没有绕过渐进披露直接由服务端猜选 ref，也没有新增“自动补 L2”产品能力；服务端只诊断已选终态缺失的事实，补充动作仍由
受限 Agent 调用现有 Catalog 完成。

测试 fixture 仅增加“第一次故意不披露”和“始终不披露”两种协议指令，用于稳定覆盖 continuation 与错误终态；生产逻辑不识别
这些指令。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| LLM 输入、固化 prompt、渐进选择定向回归 | 通过；62 passed |
| `make demo-check` | 通过；backend 358、frontend 56、独立进程 E2E 37、迁移 0001→0023、生产构建与文档链 |
| `make browser-ui-check` | 通过；2 passed |
| `make traex-real-check` | 通过；真实宿主 TraeX 1 passed，未 skip |
| `make traex-real-product-check` | 通过；4 passed，含 q4/q3 业务值、渐进披露、参数来源与不可变 ref 断言 |
| `git diff --check` | 通过 |

## 7. 真实栈事实

真实产品门禁使用 PATH 中的 TraeX 和全新独立栈完成：workflow-on、workflow-off、running cancellation、q2 探索与固化、
Presentation 决策、检查发布、q4 手动运行、q3 新对话渐进发现与复用。q4/q3 的最终结构化 `reply` 与已知
`lookup.table` 证据一致，真实选择 timeline 满足对应披露层级，四个用例均通过且未 skip。

真实 Provider probe 在默认受限沙箱中曾在模型 dispatch 前返回 `AGENT_PROTOCOL_INCOMPATIBLE`；按既有真实门禁权限在宿主环境
重跑后 1 passed。该失败没有改业务代码或放宽协议，最终通过结果来自实际 TraeX ToolResult round-trip。
