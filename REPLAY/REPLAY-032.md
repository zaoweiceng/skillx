# REPLAY-032：只修主闭环三个阻断点（审计/执行/发现）

日期：2026-08-13

依据：[`NEXT_STEPS-032.md`](../STEP/NEXT_STEPS-032.md)、
[`code-review-032.md`](../review/code-review-032.md)、
[`design-compliance-review-032.md`](../review/design-compliance-review-032.md) 和
[`REPLAY-031.md`](./REPLAY-031.md)。

## 1. 结论

032 审查指出的三个 P0（固化 IR 把失败重试当可执行步骤且丢失已验证 Browser Contract；生成式整体输出引用运行时
无法解析；固化 manifest 与源任务语言脱节导致无法自发现）和一个 P1（非法固化提议回滚成一次性 422，无可审阅 blocked
记录）已按现有架构、最小修改修复。本轮不新增产品需求、数据库迁移、鉴权/多租户/计费或领域模板。

全部门禁通过：`make demo-check`（backend 355、frontend 56、独立进程 E2E 37）、`make browser-ui-check`
（2 passed）、真实 TraeX probe（1 passed）以及拆分后的四个真实产品用例（4 passed）。新增真实用例在全新栈实际完成
lookup 探索、固化、Presentation 决策、静态检查、发布、不同 key 手动执行和新对话发现同一不可变版本并执行。
因此 032 定义的主闭环已跑通，不以 fixture 结果代替真实 Provider 事实。

## 2. P0-1：区分审计 attempts 与可执行 actions

`build_action_ir()` 现在只把 `status=succeeded` 的动作纳入可执行 IR（缺失 status 的历史轨迹按 succeeded 兼容），失败
尝试不再参与 Proposal 覆盖、节点依赖与输出来源。失败尝试仍是审计事实：新增
[`attempt_diagnostics()`](../backend/app/application/distillation_ir.py) 把 `effect=none` 的只读失败记为 info 级
`FAILED_ATTEMPT_DIAGNOSTIC`（不阻断发布），把 `effect` 未知/外部/终态不确定的失败记为 error 级
`UNCERTAIN_FAILED_ATTEMPT`（阻断发布，需人工确认），既不静默过滤也不误当业务步骤。诊断在
[`compile_agent_trajectory()`](../backend/app/application/distillation.py) 起始并入 analysis，`publishable` 只看 error 级。

`_request_distillation_proposal()` 只发送成功终态动作并显式携带 `status`，提示明确要求“不得覆盖未提供的动作”，因此
真实 TraeX 的“失败 browser.read + 成功重试”不会再被要求编入同一 deterministic 节点而抛 422。

Browser Contract 事实回到 Host：[`_normalize_action()`](../backend/app/application/agent_host.py) 在从可变 `arguments`
删除 `pageContract` 前，对成功 Browser ToolResult 用 `BrowserReadContract` 规范化校验，并作为 Host-owned
`observations.pageContract`（附 `action`/`runtime`/`resultSource=host_tool_broker`）保存；失败 ToolResult 的模型自报
contract 不写入权威证据。Browser compiler 只消费该 Host-owned contract；成功动作缺失时返回
`MISSING_BROWSER_PAGE_CONTRACT` 并阻断发布，上层仅为保留审计位置生成不可发布 Agent fallback，不能再把它表现为
“固化完成”。规则只依据 status/effect 与 adapter descriptor，没有为网页/lookup/天气/特定 selector 加分支。

## 3. P0-2：统一整体节点输出引用 `${node.output}`

[`expressions.py`](../backend/app/application/expressions.py) 抽出 `_resolve_reference()` 并统一约定：非 input 根的
`${node.output}` 返回该节点完整输出对象，`${node.output.<field>}` 返回字段；不再把 `output` 当成一个真实字段名去
查找（原实现只特判 `output.` 前缀，恰好等于 `output` 时报“无法解析路径段 output”）。字符串插值中的对象/数组显式
`json.dumps`（`ensure_ascii=False, sort_keys=True`），不再依赖 Python `str(dict)`。前端变量选择器无论是否有
outputSchema 都提供 `${node.output}`，有 Schema 时再提供逐字段路径，编辑器与运行时语义一致。

编译器侧无需新增分支：`_check_output_field` 只对 `output.<field>` 形态校验 Schema，整体 `output` 引用通过可达性
校验即可，因此固化生成的 `input → transform → llm → output` 生成式图既能静态检查通过，也能实际运行。前端
`variablePicker.ts` 的 `${nodeId.output}` 回退语义与此一致。

## 4. P0-3：固化结果可被同意图再次发现

新增 [`_deinstantiate_intent()`](../backend/app/application/distillation.py)：从源任务去掉 fixture 指令、URL 和每个
`user_input` 绑定的实例值，保留源语言的通用意图句子，并判定语言（zh/en）。该结果：

1. 交给 CapabilityDescriptor 阶段（`_request_capability_descriptor`），提示明确要求用与去实例化意图相同的自然语言
   撰写 name/summary/useWhen/doNotUseWhen/examples，tags 可保留稳定通用能力名——修复真实 TraeX 为中文任务生成
   全英文 manifest 的问题；
2. 写入固化 metadata 的 `deInstantiatedIntent`（不含 URL/key/Secret/正文），由
   [`build_manifest()`](../backend/app/application/workflow_catalog.py) 并入 `search_text` 作为稳定可检索语料，使
   源任务的同语言、同意图查询能召回自己，而不依赖 Descriptor 生成语言恰好一致。

平台 fallback descriptor 同样只按源语言和去实例化意图生成，不再按 browser/file/lookup 样本分支。目录 `_score()`
仍是词法/字符重合，但 `matchReason` 增加 `matchKind="lexical"` 并把文案从“语义召回”改为“词法命中/
词法重合，语义判断由 Agent 完成”，不再把词法命中冒充语义召回；最终选择判断仍由 Agent 在 L0/L1 描述上完成。规则
对中英文与非 lookup 样本一致，无语言/领域特例。

## 5. P1-1：固化失败形成可恢复诊断

[`distill_conversation_run()`](../backend/app/application/distillation.py) 现在区分两类失败：轨迹本身不可分析（无成功
action、无法构建 IR 等请求级问题，`_trajectory_is_analyzable` 判定）仍抛 `DistillationError` 由路由返回 4xx；轨迹可分析
但 Proposal/Compiler 拒绝时，`_persist_blocked_distillation()` 持久化一个 `status=blocked`、无草稿 Workflow 的
Distillation + 结构化诊断（`DISTILLATION_REJECTED` + attempt 诊断），刷新后仍可审阅。

distill 路由的 `DRAFT_ALREADY_EXISTS` 判定改为只统计 `draft_workflow_id` 非空的记录，因此无 draft 的 blocked 诊断
不会永久占用重试入口；修复源 Run/编译规则后可重新发起固化。Schema 本身非法的 runtime Proposal 也走同一持久化路径，
不再提前抛一次性 422。ConversationRun 摘要返回最新 Distillation，ChatPage 刷新后继续显示 error 级诊断并保留重试
按钮；publishable 图才写入 drafts。

## 6. 回归门禁（现有主流程）

新增/更新的确定性回归（均纳入后端全量，全部通过）：

- `test_failed_read_then_succeeded_read_only_keeps_success`：失败 browser.read + 成功重试，最终图只含成功读取节点，
  失败 attempt 保留为 info 诊断、可发布；
- `test_uncertain_failed_side_effect_blocks_publish`：副作用不确定的失败尝试阻断发布；
- `test_normalize_action_saves_host_owned_browser_contract` / `_ignores_failed_browser_contract`：成功 ToolResult 的
  规范化 pageContract 进入 observations，失败/模型自报 contract 不成为权威事实；
- `test_whole_node_output_reference_returns_full_object` / `_in_string_is_json_serialized`：`${node.output}` 解析与
  JSON 序列化语义；
- `test_generative_lookup_graph_publishes_and_runs_with_new_key`：生成式 `input→transform→llm→output` 实际 publish +
  Worker 用新 key 运行成功，LLM 节点收到非空证据、模型调用发生、Run succeeded；
- `test_source_language_query_recalls_own_manifest` / `test_english_source_uses_same_generic_self_recall`：中英文源任务
  发布后用源语言去实例化查询能召回自己，证明同一通用机制无语言特例；
- `test_rejected_proposal_persists_blocked_distillation`：非法 Proposal 覆盖持久化 blocked + 诊断，不回滚成一次性 422。
- `test_malformed_proposal_persists_blocked_distillation`：Proposal Schema 非法同样持久化 blocked；
- ChatPage 回归：刷新后显示持久化固化诊断且无 draft 的记录仍允许重试；
- 真实产品第四用例：真实 lookup 探索 → 固化 → 审阅 → 检查 → 发布 → q4 运行 → 新对话 q3 披露并执行同一 v1。

## 7. 最小修改边界

复用现有 NormalizedAction IR、ActionCompilerRegistry、AgentHost/Tool Broker、expressions、workflow_catalog、
Distillation/Workflow 数据结构，没有新增 API 资源、数据库迁移或能力类型。天气/问候/lookup/网页/文件仍只是测试
样本，生产代码未按领域分支。

## 8. 验证记录

| 检查 | 结果 |
|---|---|
| P0-1/P0-2/P0-3/P1-1 定向测试 | 纳入后端全量，全部通过 |
| `make demo-check` | 通过；backend 355、frontend 56、独立进程 E2E 37、迁移 0001→0023、生产构建与文档链 |
| `make browser-ui-check` | 通过；2 passed |
| `make traex-real-check` | 通过；宿主登录态真实执行 1 passed，未 skip |
| `make traex-real-product-check` | 通过；workflow-on、workflow-off、cancellation、完整固化/发现/复用共 4 passed，未 skip |
| `git diff --check`、文档链 | 通过 |

## 9. 真实栈事实与边界

本轮真实 TraeX 产品栈使用全新 SQLite、Artifact 目录、独立 API/Worker 和本地 Tool Adapter，按顺序形成以下事实：

1. workflow-on 的 Alice SourceSpan、workflow-off 和 running cancellation 独立通过；
2. 真实 Agent 用 q2 调用 Host `lookup.table` 并成功完成探索；
3. 固化生成可发布草稿，人工门禁明确拒绝 Presentation 后静态检查、发布 v1；
4. 发布图用不同 key=q4 实际 Run succeeded，证明 `${node.output}` 给生成节点提供了非空证据；
5. 新对话用同语言同意图 key=q3，目录披露并由 Agent 选择刚发布的不可变 ref，第二个 Run succeeded，刷新后 runId 仍在；
6. 测试栈 finally 统一停止 API、Worker、表单服务和 TraeX 子进程，运行日志未出现 `database is locked`。

本地 Browser UI 仍故意不允许协议 fixture 冒充产品语义选择；它验证实际点击固化、Presentation、检查/发布、组件页
不同输入运行、刷新、历史、RunDetail、失败接力、主题与 console。新对话语义发现由上述真实产品用例承担，这是安全
边界而不是验收缺口。
