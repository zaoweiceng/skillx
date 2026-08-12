# 代码审查报告（029）

审查日期：2026-08-12

代码基线：`959848a`。审查对象为 [`REPLAY-028.md`](../REPLAY/REPLAY-028.md) 对应代码。
本轮未修改业务代码。

## 1. 结论

**不通过：2 个 P0、1 个 P1、3 个 P2。**

028 的原生终态解析、Provider probe 三维预算、Usage 审计、Presentation proposal/decision digest 和
发布门禁修复有效；真实 `traex` probe 已通过。但真实产品入口仍被两个独立根因阻断：WorkflowSelection
没有采用新预算模型且一次性注入全部目录工具；通用 Agent 没有把 output schema 变成明确、不可被用户指令
覆盖的终态要求。

## 2. P0 Findings

### P0-1：WorkflowSelection 仍使用旧 8k 总预算，并一次性披露五个 Tool Schema

位置：[`routes.py:4513`](../backend/app/api/routes.py#L4513)、
[`routes.py:4528`](../backend/app/api/routes.py#L4528)、
[`routes.py:4536`](../backend/app/api/routes.py#L4536)、
[`agent_host.py:287`](../backend/app/application/agent_host.py#L287)。

Provider probe 已改为 input/output/billable 三维预算，但 `_select_workflow_candidate()` 只传
`max_total_tokens=8000`。`AgentHost` 对未显式提供的三项上限全部回退为该值。实际输入
`上海天气如何` 产生 66,220 input + 945 output，在模型已经完成工作后才被 Host 以
`TOKEN_BUDGET_EXCEEDED` 拒绝。

更根本的问题是所谓 progressive selection 在首次调用前就把 `search`、`describe`、
`describe_fragment`、`slice`、`compile_plan` 五个完整工具描述全部注入 TraeX。Prompt 要求“先 search、
按需 describe”只控制调用顺序，不控制上下文披露；因此数据是渐进的，Tool Schema 不是渐进的。真实空工具/
单工具 probe 约 31k，而本次选择升至 66k，符合这一上下文膨胀事实。

应把目录发现做成真正的分阶段协议：首次只暴露单个 L0 入口，服务端根据受验证结果开启下一阶段，或提供一个
窄 Schema 的目录工具并由服务端执行 action 白名单。每阶段冻结独立 input/output/billable profile，记录 Tool
Schema 字节数与实际 Usage。不能只把 8,000 提高到 70,000，否则仍会让“渐进披露”失去成本与安全意义。

### P0-2：通用 Agent 直接使用用户原文，未声明严格 JSON 终态契约

位置：[`routes.py:5260`](../backend/app/api/routes.py#L5260)、
[`routes.py:5267`](../backend/app/api/routes.py#L5267)、
[`agent_run.py:108`](../backend/app/application/agent_run.py#L108)、
[`traex_builtin.py:425`](../backend/app/runtime/traex_builtin.py#L425)。

通用 Agent 的 `output_schema` 固定为 `{reply:string}`，但 enqueue 时默认 prompt 就是用户原文。Host 只把
Schema 作为 `--output-schema` 参数传给 TraeX，没有附加“最终只能输出符合 Schema 的严格 JSON”这一平台级
指令。用户要求“只回复 029-agent-ok”时，TraeX 合理地返回普通文本；适配器随后 `json.loads()` 失败并报告
`MODEL_MALFORMED_OUTPUT`。真实调用只用了 32 output Token，排除了输出过长和预算原因。

输出 Schema 是 Host 契约，不能依赖每个用户碰巧要求 JSON。应由统一 Agent request builder 把用户内容作为
数据包裹，加入高优先级终态规则、Schema 和最小示例；成功只接受严格对象。然后用真实 Chat API 同时验证普通
自然语言回答和工具调用回答，不应在 adapter 中把任意文本静默包装成成功 JSON。

## 3. P1 Finding

### P1-1：Presentation Agent 的 consumer、entrypoint 和 Fragment 证据仍是占位值

位置：[`distillation.py:921`](../backend/app/application/distillation.py#L921)、
[`distillation.py:924`](../backend/app/application/distillation.py#L924)、
[`distillation.py:930`](../backend/app/application/distillation.py#L930)。

Presentation 契约和审阅 UI 已完成，但提交给 Agent 的语义上下文把 consumer 固定为
`human_or_composition_unknown`，entrypoint 固定为 `top_level_with_optional_fragments`。此外
`hasCallableFragments` 从尚未 enrichment 的 `definition.metadata` 读取，正常固化顺序下通常恒为 false。

因此 Agent 并未真正获得“谁消费结果、这次是顶层还是 Fragment、是否会被组合”的事实，只能在 unknown 与猜测
之间选择。该实现无法证明同一种节点在“直接给人看”和“作为组合中间结果”时得到不同策略。

应在首次任务轨迹/固化请求中保存 result consumer 与 entrypoint 事实，先生成 Fragment proposals，再请求
Presentation；不确定时显式保留 unknown 并要求人工决定。三个跨领域真实样本必须验证策略差异，不得用天气或
节点类型模板替代语义。

## 4. P2 Findings

### P2-1：真实 Provider gate 只覆盖 probe，没有覆盖其声明中的取消和产品主路径

位置：[`test_traex_real.py:16`](../backend/real_provider_tests/test_traex_real.py#L16)、
[`Makefile:53`](../Makefile#L53)。

当前唯一 real-provider 用例只调用 `probe_provider()`。它能证明真实 binary、认证、ToolResult、严格输出和预算，
但不能发现本轮两个 P0，也没有验证 NEXT_STEPS-028 要求的取消、failed terminal、Chat workflow-on/off。

保留 probe 作为快速付费门禁，同时增加显式授权的产品级 real gate：API/Worker 下完成一次工作流选择、一次关闭
工作流回答和一次取消。否则“real gate passed”仍不能代表主流程通过。

### P2-2：成功主动探测后 Settings 仍提示“凭据未验证”

位置：[`agent_provider.py:295`](../backend/app/application/agent_provider.py#L295)、
[`SettingsPage.tsx:161`](../frontend/src/pages/SettingsPage.tsx#L161)。

`authStatus` 始终直接来自 `traex login status`，即使同一健康响应的
`capabilityStates.activeProbeAuthenticated=true`、`usable=true`，前端仍显示“本地凭据已存在但未验证，请执行主动
探测”。本轮真实 probe 后已复现，状态卡同时显示 Agent Runtime available，信息互相矛盾。

保留 credential-present 与 active-authenticated 两个事实是正确的；提示条件应在主动探测已认证且缓存有效时切换为
“主动验证成功”，不能让用户重复执行付费探测。

### P2-3：无条件 DAG 边把 `null` 渲染为可见标签

位置：[`WorkflowGraph.tsx:76`](../frontend/src/components/WorkflowGraph.tsx#L76)、
[`EditableCanvas.tsx:93`](../frontend/src/components/EditableCanvas.tsx#L93)。

后端 JSON 中 `edge.when` 可以是 `null`，前端只判断 `undefined`，随后执行 `String(null)`。组件详情实际显示两块
白色 `null` 标签。应使用 `edge.when == null ? undefined : String(edge.when)`，并覆盖详情图和编辑图。

## 5. 额外审计一致性建议

`WorkflowEditorPage` 允许用户编辑 policy 后点击“接受提议”；服务端只绑定 digest，不校验 accepted policy 与
proposal 一致。因此“accepted”和“modified”的审计语义可被误标。建议 accepted 必须使用规范化 proposal 原值，
任何字段变化只能记录为 modified。该问题不阻断当前发布门禁，纳入 P2 修复即可。

## 6. 已确认有效的 028 修复

- 真实 TraeX `turn.completed/failed/error` 已按原生终态处理，malformed output 不再冒充协议错误；
- Provider probe 的 40k/1k/41k profile 与实际 Usage 相符，真实 Tool round-trip 成功；
- Host Usage、dispatch、provider/protocol/runtime digest 能进入 Turn 与 Settings；
- Presentation proposal/decision 分离，编辑器展示依据并提供三种决定，digest 漂移阻止发布；
- Agent 不可用时 Presentation 使用保守 fallback，没有重新引入天气专用模板；
- 完整工程门禁、确定性组件、RunDetail、自然语言 Presentation、历史和主题均通过。

## 7. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；326 backend、52 frontend、37 E2E |
| `make browser-ui-check` | 通过；2 passed |
| `make traex-real-check` | 通过；1 passed，未 skip |
| 真实应用 Provider probe | 通过；31,482 Token、ToolResult、strict JSON |
| 工作流开启真实 Chat | **失败**；29.453s，67,165 Token，`TOKEN_BUDGET_EXCEEDED` |
| 工作流关闭真实 Chat | **失败**；4.693s，15,696 Token，`MODEL_MALFORMED_OUTPUT` |
| 确定性编辑/发布/运行/历史 | 通过；实际 API、Worker、Vite、Chromium |
| 页面筛选、导航、主题、复制 | 通过；console errors = 0 |
| DAG 空边显示 | **有缺陷**；显示 `null` 标签 |

修复范围和验收条件见 [`NEXT_STEPS-029.md`](../STEP/NEXT_STEPS-029.md)。
