# 代码审查报告（031）

审查日期：2026-08-12

代码基线：`0e1b68c`。审查对象为 [`REPLAY-030.md`](../REPLAY/REPLAY-030.md) 对应的工作区代码。
本轮未修改业务代码。Token 上限和消耗不纳入缺陷判定。

## 1. 结论

**不通过：2 个 P0、2 个 P1、2 个 P2。**

两阶段绑定的架构方向正确，但 provenance 数据契约自相矛盾；固化链又丢失 Host 已验证的 Tool/输出来源事实。
fixture 恰好生成最小 quote、手工把 lookup 标为 transform，掩盖了真实 TraeX 事件形态。

## 2. P0 Findings

### P0-1：Prompt 允许上下文 quote，服务器只接受 quote 等于绑定值

位置：[`routes.py:4870`](../backend/app/api/routes.py#L4870)、
[`routes.py:5190`](../backend/app/api/routes.py#L5190)、
[`routes.py:5317`](../backend/app/api/routes.py#L5317)、
[`traex_stub.py:263`](../examples/traex_stub.py#L263)。

第二阶段 Prompt 要求 quote 是消息中的“连续原文”，没有要求它必须恰好等于规范化后的 binding。真实 Agent 合法返回
`name=Alice` 和 quote=`姓名参数是 Alice`；`_verify_binding_sources()` 先确认 quote 位于消息中，再调用
`_binding_normalization()`。后者对字符串仅接受 exact/whitespace 和少数地点后缀，因此返回 None。

纠正阶段只收到 `来源无法验证: ['name']`，没有获知失败是 quote span 过宽，两次输出都被拒绝。fixture 则通过正则
直接截取 `Alice` 并把它同时用作 value/quote，所以 15 个 progressive tests 全绿却无法代表真实模型行为。

应定义一个不矛盾的通用 SourceSpan 契约：messageId、原始 span（最好含 start/end）、raw value、服务器规范化后的
binding 和 normalizationType 各自职责明确。服务器可在已选 message 中确定性定位模型给出的字符串值并生成最小
canonical span；复杂类型使用受控 JSON/per-leaf source。纠正诊断必须返回字段级原因（span_not_exact、value_not_in_message、
normalization_failed 等），不得只返回字段名。不能用姓名、城市、URL 等领域正则修复。

### P0-2：真实 Tool 轨迹丢失动作和输出 provenance，固化草稿必然不可发布

位置：[`agent_host.py:1285`](../backend/app/application/agent_host.py#L1285)、
[`agent_host.py:1338`](../backend/app/application/agent_host.py#L1338)、
[`agent_run.py:1768`](../backend/app/application/agent_run.py#L1768)、
[`distillation.py:310`](../backend/app/application/distillation.py#L310)、
[`action_compilers.py:85`](../backend/app/application/action_compilers.py#L85)。

真实 Host ToolResult 已有 capability、adapter output schema、effect、idempotency 和 lookup distillation evidence；真实
`agent.completed` 也提供并通过 Host 校验的生成式 `outputProvenance`。但当前链路存在三次信息丢失：

1. `_tool_result_metadata()` 只从模型的 request payload 复制 actionKind/outputSchema/replayability/argumentBindings，
   没有把 Host adapter descriptor 作为权威事实写入 ToolResult；真实 TraeX 因而只留下 `kind=tool`；
2. `build_trajectory()` 不接收 AgentHost 已验证的最终 output provenance，而是重新猜测；看到 lookup observation 就无条件
   把最终 `reply` 标成直接来自 tool `value`，即使真实 reply 是模型基于整张表生成的自然语言；
3. 未识别 `tool` 被编译为 Agent 节点，使用整个 Conversation `{reply}` 作为节点 output schema，输入 provenance 缺失又
   形成 `key=null`，而 output mapping 仍引用 `.value`。

实际静态检查因此准确报 `OUTPUT_FIELD_NOT_IN_SCHEMA`。更严重的是数据库轨迹宣称 reply 来自 action_output.value，
但 Artifact 中真实终态明确是 `mode=generative`、sourceCallIds 指向 lookup 调用；这违反验收规范的输出来源不变量。

修复应以 Host 事实为源：ToolResult 写入 adapter-owned action kind、output schema、replayability 和安全的参数来源证据；
AgentHostResult 保留已经校验的 outputProvenance，`build_trajectory()` 原样消费，不再猜测。若最终 reply 是 generative，
固化图必须保留显式 LLM/Agent/Presentation 生成边界；若输出改为 Tool 结构化结果，则 output schema 和 mapping 必须与
Tool schema 一致。Distillation API 交付的草稿必须能通过 Compiler，或明确返回带诊断的 blocked draft，不能把无效图
表现为已完成固化。

## 3. P1 Findings

### P1-1：同步选择失败没有持久化助手错误，刷新后审计入口消失

位置：[`routes.py:4227`](../backend/app/api/routes.py#L4227)、
[`routes.py:5243`](../backend/app/api/routes.py#L5243)、
[`ChatPage.tsx`](../frontend/src/pages/ChatPage.tsx)。

`_extract_selection_bindings()` 在第二次失败时直接 finalize Turn、commit 后抛 HTTP 502。外层创建消息路径来不及创建
assistant message、更新 Conversation 标题或附带 tool timeline。实际数据库中 Turn 为 error、elapsed=44,781ms，
assistant_message_id 为空；刷新后页面只剩用户消息和“新对话”，错误、Usage 与 failed disclosure 均不可访问。

对话失败也是产品结果。应用层应返回受控 SelectionFailure，由唯一外层持久化助手 `state=error`、安全错误码/说明、
耗时、Usage 摘要和目录 timeline，再结束 Turn。HTTP 可以保持错误状态，但客户端必须能根据持久 handle 恢复；更适合
对话契约的是 201 + assistant error state。刷新后必须显示同一条失败记录，且不得暴露模型原文或 Secret。

### P1-2：消息请求持有 SQLite 写事务等待真实模型，阻塞独立 Worker

位置：[`routes.py:4117`](../backend/app/api/routes.py#L4117)、
[`routes.py:4134`](../backend/app/api/routes.py#L4134)、
[`routes.py:4180`](../backend/app/api/routes.py#L4180)、
[`routes.py:4219`](../backend/app/api/routes.py#L4219)、
[`worker.py:23`](../backend/app/worker.py#L23)。

`create_message()` 创建 Turn 和 User Message 后立即 `flush()`，取得 SQLite 写锁；随后同一个 Session 进入
`_unbound_conversation_message()`，在提交前执行目录发现和多次真实模型调用。本轮失败请求耗时 44.781 秒，期间独立
Worker 的 Browser Runtime heartbeat 写事务实际抛出 `sqlite3.OperationalError: database is locked`，模型请求结束后才
恢复。这不是压力场景：一个用户的一条普通消息已经足以阻塞 Worker 的心跳、租约领取和其它状态写入。

设计文档明确要求 SQLite 写事务保持短小，且外部调用不得位于数据库事务中。应把消息受理做成短事务：原子持久化
User Message、processing Turn 和可领取的 selection/agent job 后提交；Worker 在无写事务状态执行 search/describe/模型
调用；最后用短事务和状态 CAS 写入终态助手消息。若暂时保留 API 内执行，也必须在外部调用前提交并在回来后新开
事务、校验 turn generation/idempotency，但长期应统一进入已有的持久队列/租约模型。增大 busy_timeout 只能延后失败，
不能作为修复。

## 4. P2 Findings

### P2-1：声称冻结的消息 ID 集合在恢复时被直接丢弃

位置：[`routes.py:4274`](../backend/app/api/routes.py#L4274)、
[`routes.py:4382`](../backend/app/api/routes.py#L4382)。

创建 PendingSelection 时保存 `_bindingMessageIds`，恢复时却 `pop(..., None)` 后不校验、不投影到审计。当前恢复路径
主要使用已保存 source 和新回复，因此未直接造成此次失败，但实现没有满足 REPLAY 所称“消息 ID 集合冻结”。应将
其纳入 pending contract digest 并验证历史 source 必须属于冻结集合，新回复单独追加；或者删除该字段并修正文档，
避免形成虚假的安全不变量。

### P2-2：真实产品测试把 workflow-on/off 放在同一用例，首个失败掩盖第二条路径

位置：[`test_traex_product_real.py:42`](../backend/real_provider_tests/test_traex_product_real.py#L42)。

本轮产品门禁显示 1 failed、1 passed；passed 是 cancellation。workflow-on 在第 59 行失败后，workflow-off 完全没有
执行，只能靠人工 UI 补测。应把 probe 放 module fixture/cache，把 workflow-on、workflow-off、cancellation 拆成三个
独立测试，并分别报告是否调用真实模型、是否 skip 和终态。这样一个主路径失败不会抹掉其它真实事实。

## 5. 已确认有效的 030 修复

- 产品门禁使用生产 builtin resolver，并在 probe 前断言 runtimeType/source；
- cancellation 使用有效只读 capability，真实运行到 cancelled；
- disclosure failure 保存 action、digest、错误码与 retryable，不推进成功披露状态；
- 无工具成功 Run 不再显示固化按钮，有 Tool 的成功 Run 显示并能创建草稿；
- Presentation 提议/接受 UI 可用；
- 组件运行、RunDetail、复制、调用历史、工作流 JSON/检查/发布和主题均正常。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；339 backend、54 frontend、37 E2E |
| `make browser-ui-check` | 宿主权限下 2 passed |
| `make traex-real-check` | 通过；1 passed，未 skip |
| `make traex-real-product-check` | **1 failed、1 passed**；workflow-on 绑定失败，cancellation 通过 |
| 默认工作流真实 UI | **失败**；44.781s 后 502，无 Run、无持久助手错误 |
| API/Worker 并发 | **失败**；默认工作流选择期间 Worker heartbeat 报 `database is locked`，请求结束后恢复 |
| 工作流关闭无工具回答 | 通过；`031-ui-agent-ok`，零 workflow tools，正确隐藏固化按钮 |
| 工作流关闭 lookup 探索 | 通过；真实 Tool 调用、回答和固化草稿创建成功 |
| 真实 lookup 草稿静态检查 | **失败**；`OUTPUT_FIELD_NOT_IN_SCHEMA` |
| 人工确定性组件/工作流 | 通过；运行、历史、复制、编辑、检查、发布均正常 |
| 主题/导航/前端控制台 | 通过；console errors/warnings = 0 |

下一版任务见 [`NEXT_STEPS-031.md`](../STEP/NEXT_STEPS-031.md)。
