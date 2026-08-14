# NEXT_STEPS-033：只修正确输出与渐进选择的主闭环阻断

依据：[`REPLAY-032.md`](../REPLAY/REPLAY-032.md)、
[`design-compliance-review-033.md`](../review/design-compliance-review-033.md)、
[`code-review-033.md`](../review/code-review-033.md) 和 [`docs/`](../docs/) 下现有设计/规范。

本版不增加产品需求。范围仅是让现有“首次探索—固化—发布—新对话复用”返回正确结果并稳定结束。
仍为单用户 Local Demo；不增加鉴权、多租户、计费、Token 上限策略、领域模板或新的能力类型。

## 1. 034 完成定义

只有以下事实同时成立才能声明通过：

1. 发布工作流的 LLM/Agent fallback 实际收到已解析节点输入，输出内容与 ToolResult 一致；
2. Agent 选择 `run_full`、`run_fragment` 或 `compose` 前满足对应 L1/L2/Plan 披露契约；
3. 任意目录/披露契约错误都形成 assistant error 和终态 Turn，不再返回裸 500 或遗留 processing；
4. q2 探索发布后，q4 手动执行和 q3 新对话复用都返回正确业务值；
5. 工程、Browser、真实 Provider、真实产品闭环和 UI 点击验收全部通过。

## 2. P0：让模型节点真正消费 resolved inputs

- [ ] 在 `LLMExecutor` 定义统一请求契约：每次调用都把 resolved inputs 以规范 JSON 和明确边界放入 prompt；不得把输入注入
  依赖于可选 `{input}` 是否存在。
- [ ] 若保留 `{input}` 模板，明确替换一次的语义；无占位符时自动追加输入区段，避免重复或静默丢失。
- [ ] Distillation 生成的 `final-response` prompt 同时说明任务目标、输出 Schema 语义和“只依据证据”，但不得包含具体 q 值、
  网页正文或领域模板。
- [ ] 结构化输出通过 Schema 后，还要在已知 deterministic evidence 的验收中检查结果来自证据；状态 succeeded 不足以判定任务成功。
- [ ] 回归 q2→q4：LLM 节点 input 包含“第四季度：营收增长 15%”，最终 `reply` 必须包含相同事实。
- [ ] 回归 q2→q3 新对话复用：最终 `reply` 必须包含“第三季度：营收持平”。

## 3. P0：把渐进披露变成服务端终态约束

- [ ] 选择 Agent 返回终态后、参数提取前，按 action 校验服务端披露事实：`run_full` 至少 L1、`run_fragment` 必须 L2、
  `compose` 的全部 ref 与 compiled plan 必须满足现有契约。
- [ ] 披露不足时，将结构化 diagnostic 返回同一选择循环，并在剩余 `max_steps` 内允许 Agent 调用缺失的
  `describe`/`describe_fragment`/`compile_plan`；不能直接进入执行，也不能由提示词假设替代服务端校验。
- [ ] 若预算耗尽仍不满足，返回明确 `WORKFLOW_SELECTION_DISCLOSURE_INCOMPLETE`，持久化 tool timeline 与原因。
- [ ] `_extract_selection_bindings()`、`_prepare_selection()`、`_execute_selection_service()` 的
  `CatalogError`/`PlanCompileError`/`WorkflowValidationError` 必须在对话边界规范化为现有错误消息结构。
- [ ] 无论选择成功或失败，ConversationTurn 都必须进入 succeeded/ask_user/error 之一，并写入 assistant message；禁止裸 500
  留下 processing。
- [ ] 回归真实失败分支：Workflow L1 暴露 Fragment 后选择 `run_fragment`，系统必须继续披露 L2，再提取 q3 并启动同一不可变版本。

## 4. P1：收紧真实闭环门禁，避免假通过

- [ ] `test_real_product_distill_publish_discover_and_reuse` 对 q4 断言最终输出包含“第四季度”和“15%”。
- [ ] 同一用例对 q3 断言最终输出包含“第三季度”和“营收持平”，并验证对话中的结构化与文本结果一致。
- [ ] 门禁分别记录并断言 search L0、Workflow L1、必要时 Fragment L2、最终选择、参数来源和不可变版本 ref。
- [ ] 增加选择错误终态断言：任何不完整披露都不得产生 500，Turn 不得停在 processing。
- [ ] 保留现有真实 Browser failed→succeeded→distill 用例形态，防止修选择路径时回归 032 修复。

## 5. 真实栈验收顺序

1. 启动全新 API、独立 Worker、本地网页站点和 Vite，主动 probe 确认 `traex_builtin`；
2. 工作流关闭，真实 Agent 用 q2 探索并固化；
3. 审阅 Presentation、静态检查并发布 v1；
4. 从工作流编辑器用 q4 运行，核对 LLM 输入与最终事实；
5. 从组件详情用 q1 运行，刷新后核对调用历史与 RunDetail；
6. 新建默认开启工作流的对话，用 q3 触发发现、渐进披露、参数提取和同一 v1/Fragment 执行；
7. 核对 q3 最终结构化与文本结果、历史消息、Turn 终态和 console；
8. 另跑真实 Browser 一次失败后成功的读取轨迹，确认仍可固化。

每一步失败就记录该阶段根因，不用成功状态、非空 JSON 或 fixture 代替结果正确性。

## 6. 最终检查

- [ ] `make demo-check` 通过；
- [ ] `make browser-ui-check` 通过；
- [ ] `make traex-real-check` 实际执行且通过，不得 skip；
- [ ] `make traex-real-product-check` 完整通过，且含 q4/q3 语义断言；
- [ ] 实际点击亮暗主题、对话历史/开关、固化、Presentation、检查/发布、组件运行/历史、RunDetail/复制；
- [ ] 结束后无 processing Turn、running Run 或遗留服务；
- [ ] console error/warning、`git diff --check` 和文档链均通过。

在模型 prompt 仍不含 resolved inputs、q4/q3 内容仍错误、Fragment L2 不足仍返回 500，或错误后 Turn 仍为 processing 时，
不得声明 034 通过。
