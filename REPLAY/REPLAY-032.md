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

本地全部工程门禁通过：`make demo-check`（backend 354、frontend 55、独立进程 E2E 37）、`make browser-ui-check`
（2 passed）、生产构建与文档链均通过。**真实 TraeX 的 `traex-real-check` / `traex-real-product-check` 本轮没有获得新的
外部调用授权**，与 031 一致仍为 blocked，不能用 fixture 结果替代；因此完整真实闭环（新对话语义发现并复用）保持
需授权执行状态，代码根因与本地流程已修复。

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
contract 不写入权威证据。这样 Browser compiler 能消费 Host-owned contract 生成确定性 browser 节点，缺失时才退回
Agent 节点。规则只依据 status/effect 与 adapter descriptor，没有为网页/lookup/天气/特定 selector 加分支。

## 3. P0-2：统一整体节点输出引用 `${node.output}`

[`expressions.py`](../backend/app/application/expressions.py) 抽出 `_resolve_reference()` 并统一约定：非 input 根的
`${node.output}` 返回该节点完整输出对象，`${node.output.<field>}` 返回字段；不再把 `output` 当成一个真实字段名去
查找（原实现只特判 `output.` 前缀，恰好等于 `output` 时报“无法解析路径段 output”）。字符串插值中的对象/数组显式
`json.dumps`（`ensure_ascii=False, sort_keys=True`），不再依赖 Python `str(dict)`。

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

目录 `_score()` 仍是词法/字符重合，但 `matchReason` 增加 `matchKind="lexical"` 并把文案从“语义召回”改为“词法命中/
词法重合，语义判断由 Agent 完成”，不再把词法命中冒充语义召回；最终选择判断仍由 Agent 在 L0/L1 描述上完成。规则
对中英文与非 lookup 样本一致，无语言/领域特例。

## 5. P1-1：固化失败形成可恢复诊断

[`distill_conversation_run()`](../backend/app/application/distillation.py) 现在区分两类失败：轨迹本身不可分析（无成功
action、无法构建 IR 等请求级问题，`_trajectory_is_analyzable` 判定）仍抛 `DistillationError` 由路由返回 4xx；轨迹可分析
但 Proposal/Compiler 拒绝时，`_persist_blocked_distillation()` 持久化一个 `status=blocked`、无草稿 Workflow 的
Distillation + 结构化诊断（`DISTILLATION_REJECTED` + attempt 诊断），刷新后仍可审阅。

distill 路由的 `DRAFT_ALREADY_EXISTS` 判定改为只统计 `draft_workflow_id` 非空的记录，因此无 draft 的 blocked 诊断
不会永久占用重试入口；修复源 Run/编译规则后可重新发起固化。前端 `distillConversationRun` 类型放开 `draftWorkflowId`
为可空，ChatPage 在 blocked 或无 draft 时展示 error 级诊断，publishable 图才写入 drafts。

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

## 7. 最小修改边界

复用现有 NormalizedAction IR、ActionCompilerRegistry、AgentHost/Tool Broker、expressions、workflow_catalog、
Distillation/Workflow 数据结构，没有新增 API 资源、数据库迁移或能力类型。天气/问候/lookup/网页/文件仍只是测试
样本，生产代码未按领域分支。

## 8. 验证记录

| 检查 | 结果 |
|---|---|
| P0-1/P0-2/P0-3/P1-1 定向测试 | 纳入后端全量，全部通过 |
| `make demo-check` | 通过；backend 354、frontend 55、独立进程 E2E 37、迁移 0001→head、生产构建与文档链 |
| `make browser-ui-check` | 通过；2 passed |
| `make traex-real-check` | **blocked / 未执行**；本轮无真实 TraeX 外部调用授权，不用 fixture 替代 |
| `make traex-real-product-check` | **blocked / 未执行**；同上 |
| `git diff --check`、文档链 | 通过 |

## 9. 尚未形成的真实事实

本地 fixture、单元测试、独立进程 E2E 与 Browser UI 只能证明协议实现与本地产品流程，不能证明真实 Provider 行为。
以下 032 §6.2 真实栈验收仍需在明确授权真实 TraeX 外部调用后执行，失败重试仍导致 422、发布图仍 `INVALID_INPUT`
或新对话仍找不到刚发布版本时不得声明 032 通过：

1. 真实 Agent 只读探索网页（首次 browser.read 失败、重试成功）→ 固化不再 422、成功读取被编译为 browser 节点；
2. 真实 lookup 固化 `input→transform→llm→output`，发布后从组件页用不同 key 运行 succeeded、模型被调用；
3. 新对话以源任务同意图不同参数触发 catalog 披露、Agent 选择并启动同一 v1，刷新对话/组件历史/RunDetail 后结果与
   轨迹仍在；
4. 长模型调用期间无 SQLite lock，结束后无 processing Turn、running Run 或遗留服务。
