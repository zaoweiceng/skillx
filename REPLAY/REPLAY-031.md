# REPLAY-031：统一来源契约与 Host 轨迹事实修复

日期：2026-08-12

依据：[`NEXT_STEPS-031.md`](../STEP/NEXT_STEPS-031.md)、
[`code-review-031.md`](../review/code-review-031.md) 和
[`design-compliance-review-031.md`](../review/design-compliance-review-031.md)。

## 1. 结论

031 审查指出的 SourceSpan 冲突、Host 事实丢失、失败结果不持久、SQLite 长写事务、PendingSelection
冻结声明失真和真实门禁互相遮蔽六个根因已按现有架构修复。未增加数据库迁移、领域路由、产品能力或 Token/计费
策略。

本地工程门禁连续两轮通过，Browser UI 门禁在宿主权限下连续两轮通过。fixture 和本地产品栈已证明主要流程可跑通；
真实 TraeX probe、三个真实产品用例和真实 Provider 完整闭环本轮没有获得新的外部调用授权，不能用本地 fixture 代替。
因此本轮结论为：**代码根因和本地流程已修复，真实 TraeX 验收仍 blocked，不声明 NEXT_STEPS-031 整体通过。**

## 2. P0：SourceSpan 与服务器重建成为唯一绑定契约

第二阶段绑定协议不再让模型返回可直接执行的 quote。模型只能指出冻结消息中的 Unicode code-point
`[start,end)`、原文和候选 canonicalValue；服务器随后：

- 校验 messageId 属于本轮冻结消息集合，且 source 类型与当前/历史消息一致；
- 校验 start/end/text 与服务器持久化原文完全一致；
- 对字符串在指定 span 内确定性定位候选值，并把持久证据收敛为值本身的最小 span；
- 对 integer、number、boolean、array 和 object 使用通用 Schema/JSON 转换；
- 以服务器重建值执行 JSON Schema 验证，只从通过验证的 evidence 重建 bindings；
- 返回字段级 `SOURCE_MESSAGE_NOT_ALLOWED`、`SPAN_INVALID`、`VALUE_NOT_IN_SOURCE` 或
  `NORMALIZATION_FAILED` 诊断，并将首轮诊断写入唯一一次纠正 Prompt。

持久化 BindingSource 现在同时保留 rawSpan、canonicalValue、serverVerified 和 normalizationType。协议 fixture
故意返回 `name=Alice`、上游 span=`姓名参数是 Alice`，服务端最终持久化的 canonical span 仍是 `Alice`，从而覆盖了
031 的真实失败形态，而不是继续依赖理想最小 quote。测试还覆盖 Unicode、数字、数组、JSON 对象和伪造 messageId。

PendingSelection 的 contract digest 现在包含 ref/plan、inputSchema digest、disclosure session、冻结历史 message IDs
和已验证 sources；恢复时先验证 digest 与历史来源，再把新用户回复作为独立 source 追加，且不重新 search/describe。

## 3. P0：Host 事实直接进入轨迹和可发布 DAG

Tool adapter descriptor 现在由 Host 补齐 action kind、input/output schema、effect、idempotency、replayability 和
capability identity。ToolResult 的这些字段全部由 descriptor 覆盖，模型在 `tool.requested` 中自报的同名字段不再成为
权威事实。

Host 根据可见的用户 Prompt 和前序 ToolResult 验证每个 Tool argument 的 provenance，并明确记录
`user_input`、`prior_output` 或 `unknown`。只有已验证为用户输入且确实存在于 Tool arguments 的字段才进入固化输入
Schema，避免继续生成 `key=null` 或把整个 Conversation `{message}` 当作真实 Tool 参数。

AgentHostResult 保存已经校验的 `agent.completed.outputProvenance`，ConversationRun finalizer 原样传给 trajectory；
`build_trajectory()` 不再根据 action 类型或返回值相等重新猜测最终来源。lookup.table 的冻结 table、Host 输入来源和
结构化输出可编译成 transform；生成式最终回复则在 evidence action 后保留显式 LLM 节点。若 provenance 引用未知
callId 或 output mapping 不符合真实 Schema，草稿状态为 `blocked`，前端显示可读诊断列表，不再展示为“固化完成”。

定向测试证明 Host lookup + generative reply 生成 `input → transform → llm → output` 的可发布图；Browser UI 使用动态
网页样本实际完成首次探索、固化、Presentation 审阅、静态检查、发布和不同 URL 的第二次组件运行，第二次运行消费
`url` 输入而不是冻结旧 message 或 callId。

## 4. P1：失败结果持久化与 SQLite 短事务

消息受理先用短事务持久化 User Message、processing Turn、workflow mode snapshot、标题和时间戳，然后在任何
目录/模型/Tool 外部调用前提交。目录调用和 ModelUsage 各自使用短事务记录；不再持有最初的 SQLite 写锁等待慢模型。

选择或绑定阶段抛出的受控错误由外层统一转成 assistant `state=error` 并持久化，同时完成 Turn。记录包含安全错误码、
说明、retry/nextAction、ModelUsage call IDs、disclosure session、去敏 timeline 和字段诊断，不保存 Prompt、完整
ToolResult 或 Secret。Chat 历史把 error/unavailable 当普通助手消息渲染；刷新后仍可看到失败结果，Conversation 标题
也不再停留在“新对话”。

并发回归让 AgentHost 停在慢选择阶段，同时要求独立 SQLite Session 在 0.5 秒内完成写入；该测试通过，证明初始写锁
已在外部调用前释放。本轮遵循最小修改原则，没有另造一套选择后台队列；持久 Worker/job 的进一步统一不作为本次
流程修复的新增功能。

## 5. P2：冻结校验和真实产品门禁拆分

`_bindingMessageIds` 不再在恢复时直接丢弃，而是进入 pending contract digest 并用于校验全部历史来源。真实产品测试
把 probe 提升为 module fixture，并拆成三个独立用例：

1. workflow-on：同消息 Alice 绑定、Run 和刷新后的 verified source；
2. workflow-off：零 workflow 目录调用；
3. running cancellation：进入 running 后取消。

默认未授权状态会分别报告 3 个 skip，一个路径失败不会阻止其余路径收集事实；测试栈仍统一清理 API、Worker、表单
服务及 TraeX 子进程。

## 6. 最小修改边界

本轮复用现有 BindingSource、PendingSelection、AgentHost、Tool Broker、ConversationRun、Distillation、ModelUsage 和
Turn 数据结构，没有新增 API 资源或迁移。Browser 门禁只把固化组件的过期 `message` 输入选择器改为 Host 事实产生的
`url` 输入。天气、问候、lookup、网页和文件仍只是测试样本，生产代码没有按领域分支。

## 7. 验证记录

| 检查 | 结果 |
|---|---|
| SourceSpan、失败持久化、SQLite 并发、Host provenance 定向测试 | 纳入后端全量，全部通过 |
| `make demo-check` 连续两次 | 两次均通过；每次 343 backend、55 frontend、37 独立进程 E2E、迁移 0001→0023、生产构建及文档链 |
| `make browser-ui-check` 连续两次 | sandbox 内因 Chromium MachPort permission denied 未启动页面；首次宿主运行暴露旧 `root_message` 选择器，改为 Host Schema 的 `root_url` 后连续 2 passed + 2 passed |
| 真实产品测试默认状态 | 3 skipped，workflow-on/off/cancellation 分别报告需显式授权 |
| `make traex-real-check` | **blocked / 未执行**；没有新增使用本机 TraeX 登录态调用外部模型的授权 |
| `make traex-real-product-check` | **blocked / 未执行**；同上，不能用 fixture 结果替代 |
| `git diff --check`、文档链 | 通过 |

## 8. 尚未形成的真实事实

本地 fixture、单元测试和 Browser UI 只能证明协议实现与本地产品流程，不能证明真实 Provider 行为。以下验收仍需在
明确授权真实 TraeX 外部调用后执行：

1. 快速 probe 以及拆分后的 workflow-on、workflow-off、running cancellation 三个产品用例全部 executed/passed；
2. 真实慢选择期间持续观察 Worker heartbeat/claim/cancel，无 SQLite lock 和遗留 processing Turn；
3. 真实 lookup 或文件/网页探索—固化—人工发布—新对话不同输入复用完整闭环；
4. 两个独立 Run 的可复现契约漂移、ReplayEvidence 和 revision v2，以及动态网页/文件/Fragment 差异样本。

这些真实事实补齐前，031 的整体状态保持 blocked。
