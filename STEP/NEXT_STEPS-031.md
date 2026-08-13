# NEXT_STEPS-031：统一来源契约，先让真实轨迹可发布复用

依据：[`REPLAY-030.md`](../REPLAY/REPLAY-030.md)、
[`design-compliance-review-031.md`](../review/design-compliance-review-031.md)、
[`code-review-031.md`](../review/code-review-031.md) 和 [`docs/`](../docs/) 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。Token 上限和消耗不纳入本版任务；保留统计
即可。天气、问候、lookup、网页和文件都只能作为真实验收样本，禁止增加领域专用路由或参数模板。

## 1. 031 完成定义

下一版只有同时满足以下事实才能声明通过：

1. 默认开启工作流时，同一句中的参数由真实 TraeX 提取并经服务器验证，一次启动 Workflow；
2. 参数来源的 Prompt、JSON Schema、服务器 normalization 和持久化审计使用同一个无歧义契约；
3. 真实 Tool 探索的 Host 事实完整进入 trajectory，最终输出 provenance 不被重新猜测；
4. 真实 Agent lookup/文件/网页至少一个轨迹固化后静态检查通过、人工发布，并在新对话以不同输入复用；
5. 所有选择/绑定失败都形成持久助手结果，刷新后仍能查看错误、耗时、Usage 和目录 timeline；
6. 模型/Tool 外部调用不持有 SQLite 写事务，长选择期间 Worker 心跳和任务领取持续正常；
7. 快速 probe、拆分后的三个真实产品门禁、工程门禁和实际 UI 点击全部通过。

## 2. P0：建立唯一 SourceSpan 与 normalization 契约

### 2.1 数据模型

- [ ] 明确定义 `BindingEvidence`：field、source、messageId/callId/path、rawSpan、canonicalValue、confidence、
  uncertain、serverVerified、normalizationType；raw span 推荐包含 UTF-8/Unicode 安全的 start/end 和原文。
- [ ] Prompt 必须明确 rawSpan 是“值本身的最小连续原文”还是“允许上下文短语”；JSON Schema 和服务器实现必须一致。
- [ ] 字符串精确值由服务器在指定消息中确定性定位并生成 canonical span；这属于证据验证，不是意图/领域路由。
- [ ] 数字、布尔、数组、对象和允许的规范化使用版本化、通用转换；复杂对象可采用 JSON span 或 per-leaf evidence。
- [ ] messageId 必须属于本轮冻结消息集合；tool_result 必须绑定已验证 callId/path/evidence digest。
- [ ] 服务器只从验证后的 evidence 重建 bindings，模型返回值不能直接成为执行输入。

### 2.2 纠正与缺参

- [ ] 第一次失败返回字段级结构化诊断，如 `SPAN_NOT_MINIMAL`、`VALUE_NOT_IN_SOURCE`、
  `NORMALIZATION_FAILED`、`SOURCE_MESSAGE_NOT_ALLOWED`，并写入第二次纠正 Prompt。
- [ ] 第二次仍不合约时持久化 `SELECTION_BINDING_INCOMPLETE`；不能向用户谎报“未提供参数”。
- [ ] 只有证据不存在或真正歧义才 ask_user；补充后不重新 search/describe。
- [ ] PendingSelection digest 冻结 ref、input schema、disclosure session、历史 message IDs 和已验证 sources；恢复时验证，
  新用户回复作为单独 source 追加。

### 2.3 测试

- [ ] fixture 不得总是生成理想最小 quote；加入 `name=Alice, quote=姓名参数是 Alice`、多次出现、Unicode、数字、数组、
  JSON 对象、伪造 messageId/path 等协议样本。
- [ ] 真实产品门禁继续使用 `请问候名叫 Alice 的人，姓名参数是 Alice`，断言首条消息直接得到 verified source 和 Run。
- [ ] 增加一个非问候动态 Schema 样本，证明没有领域规则。

## 3. P0：让 Host 事实成为可执行轨迹的唯一来源

### 3.1 Tool 动作事实

- [ ] `ToolAdapterRegistry.describe()` 或版本化 descriptor 提供 action kind、input/output schema、effect、idempotency、
  replayability 和 capability identity；Host ToolResult 必须携带这些 Host-owned facts。
- [ ] adapter distillation evidence 只能补充已验证的抽象语义；不得依赖模型在 tool.requested 中自报 actionKind、schema、
  effect 或 replayability。
- [ ] 对 Tool arguments 建立 Host 验证的 provenance：来自用户输入、前序 ToolResult/Artifact 或显式 constant；未知来源
  保持 unknown 并阻止确定化，不能写成 `null` 后继续生成可发布候选。
- [ ] `lookup.table` 若存在冻结 table、输入 key provenance 和 output schema，应由通用 evidence 编译为 transform；
  其它动态 Tool 保留锁定 Tool/Agent 节点，不按返回 dict 形状猜测。

### 3.2 最终输出事实

- [ ] `AgentHostResult` 保存 Host 已校验的 `agent.completed.outputProvenance`，ConversationRun finalizer 原样传给
  `build_trajectory()`；删除基于 action 类型和值相等的重新猜测。
- [ ] direct provenance 必须验证最终值确实等于对应 ToolResult path；generative provenance 保留 sourceCallIds、
  evidenceDigests 和 promptVersion。
- [ ] 若最终 reply 为 generative，固化图保留显式 LLM/Agent/Presentation 节点；若决定只输出 Tool 结构化结果，必须
  显式改变 output contract 并经人工审阅，不能把生成式 reply 偷换成 tool value。
- [ ] output mapping 在草稿创建前按节点真实 output schema 校验。不可发布时 API 返回 blocked draft + diagnostics，UI
  明确显示阻断原因；不得以“固化完成”掩盖无效 DAG。

### 3.3 真实闭环通过条件

- [ ] 真实 lookup 探索 → 固化 → Presentation 审阅 → 静态检查 → 发布 → 新对话不同 key 发现并运行；
- [ ] 新运行消费新输入，不冻结 `alpha`、旧 ToolResult 或 callId；
- [ ] 固化后节点类型、input mapping、output schema/path 和最终自然语言策略全部可解释；
- [ ] 文件或动态网页再完成一个同机制样本，证明 lookup 不是生产特例。

## 4. P1：所有对话终态都持久化

- [ ] Selection/Binding application service 返回 typed success/ask_user/continue/error，而不是在深层 commit 后抛出使外层
  无法保存 assistant 的 HTTPException。
- [ ] 外层统一创建 assistant message、更新标题、首响应/完成时间和 Turn 状态；错误正文只含安全 code/message。
- [ ] 失败消息保存 disclosure session、safe timeline、ModelUsage IDs 和 retry/next-action 信息；不保存 Prompt、ToolResult
  全文或 Secret。
- [ ] Chat 对 error 与 unavailable 像普通历史消息一样渲染；刷新、切换历史和重启服务后保持同一结果。
- [ ] 失败时历史标题来自用户输入，不再显示“新对话”；右侧执行轨迹可查看 failed catalog/binding 阶段。
- [ ] 覆盖 selection Agent 失败、binding contract 失败、provider unavailable、目录 Tool 失败和执行 Run 失败。

## 5. P1：切断数据库事务与外部调用

- [ ] 消息受理短事务只持久化 User Message、processing Turn、workflow mode snapshot 和可领取 job，然后立即提交。
- [ ] search/describe、参数提取、Agent/LLM 和 Tool 调用在数据库写事务外执行；优先复用持久队列、Worker 租约、heartbeat
  和 generation CAS，不为选择链再造一套内存后台任务。
- [ ] 每个目录调用的 disclosure 记录用独立短事务落库；终态用新 Session + Turn 状态 CAS 一次性写入 assistant、Usage、
  timeline、标题和 completed/error 状态。
- [ ] API 重试、Worker 重启或响应断开不能重复执行外部副作用；selection job 具有 idempotency key 和可恢复 checkpoint。
- [ ] 增加一个真实慢模型并发验收：选择持续时间超过 heartbeat 周期时，Worker heartbeat、Run claim 和取消均不中断，日志
  中无 `database is locked`；不得用调大 busy_timeout 代替事务拆分。

## 6. P2：修复真实门禁与冻结声明

- [ ] 把真实产品门禁拆为独立的 workflow-on、workflow-off、running cancellation 三个用例；probe 可按 module 复用。
- [ ] 每个用例单独报告 executed/passed/skipped/failed；任一失败不得阻止其它路径收集事实。
- [ ] workflow-on 还要断言失败时持久助手记录存在，成功时 binding source 与 Run 可刷新回看。
- [ ] `_bindingMessageIds` 要么进入 pending contract digest 并实际校验，要么删除字段和“冻结集合”声明。
- [ ] 真实门禁退出后确认 API、Worker、表单服务和 TraeX 子进程全部终止。

## 7. 完成通配型闭环

在上述 P0 修复后，按真实 UI 至少完成：

1. 通用 Agent 首次完成只读任务并记录结构化轨迹；
2. 固化草稿的输入、Tool/Agent 节点、输出和 Presentation 都来自可验证事实；
3. 人工审阅、静态检查并发布不可变 v1；
4. 新对话默认开启工作流，以不同输入渐进发现 v1 并直接执行；
5. 制造可恢复瞬态失败，接力只执行 remaining steps；
6. 对两个独立 Run 可复现的契约漂移生成 ReplayEvidence 和 revision v2；transport/timeout 不得触发修订。

再以动态网页/长文本和文件/组合 Fragment 做差异样本。Presentation 是否生成自然语言由 consumer/entrypoint 决定，
不是由天气、lookup、文件名或节点类型模板决定。

## 8. 前端与最终交付门禁

- [ ] 实际点击亮/暗主题、对话历史、新对话、工作流开关、失败记录、计时/Token、固化、Presentation 审阅、组件
  筛选/历史/RunDetail/复制、工作流 JSON/检查/发布、能力和系统页。
- [ ] 真实固化诊断使用可读列表，不直接把后端 JSON 错误串作为唯一提示。
- [ ] 760、903、1024、1280、1440 宽度无重叠和不可点击区；console error/warning 为空。
- [ ] `make demo-check` 连续两次、`make browser-ui-check` 连续两次通过。
- [ ] `make traex-real-check` 及拆分后的三个 `make traex-real-product-check` 用例都实际通过，不得 skip。
- [ ] 真实慢选择期间持续观测 Worker heartbeat/claim；无 SQLite lock，终止后无遗留 processing Turn/job。
- [ ] 全新真实栈至少一个完整探索—固化—发布—复用闭环通过，刷新后所有证据仍在。
- [ ] `git diff --check`、文档链和临时文件检查通过；报告严格区分 fixture、probe、产品门禁和人工真实场景。

在真实 quote 仍被协议误拒绝，或真实 Tool 轨迹生成的草稿仍不能通过静态检查时，不得声明整体通过。
