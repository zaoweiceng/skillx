# NEXT_STEPS-019：人工体验问题修复与产品化交互

依据：人工审阅的修改要求、两张现场截图，以及
[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、
[`docs/schema.md`](../docs/schema.md) 和当前代码。

范围：单用户 Local Demo；本轮仍不做鉴权、RBAC、多租户和分布式部署。019 的首要目标不是继续扩展闭环能力，
而是让已经跑通的闭环以普通用户能理解、能复查、能稳定调用的方式呈现。

## 1. 结论

018 的“首次探索—固化—复用—失败接力—结果返回”执行闭环已经通过，但当前版本**不能通过 019 人工体验验收**。
人工审阅提出的需求均有明确缺口，其中最关键的不是页面样式，而是对话入口的职责错位：

```text
当前：用户文本 → 字符串目录排序/取首项 → 发现 required 字段 → 要求用户重新输入
目标：用户文本 + 对话上下文 → Agent 语义判断 → 渐进发现候选 → 提取并校验参数 → Workflow Runtime
```

图 2 中“上海天气如何”已经显式包含 `city=上海`，仍被要求补充 `city`。这不是天气工作流的特殊问题，而是当前
选择降级路径和 Demo TraeX stub 只识别 URL、`qN`，没有通用语义参数绑定导致的系统性问题。禁止以“上海/城市”
正则或给天气工作流加特判作为修复。

## 2. 当前缺口与根因

| 需求 | 当前事实 | 根因 | 019 目标 |
|---|---|---|---|
| 亮/暗主题 | CSS 只有暗色变量；`useTheme` 存在但未接入应用 | 主题状态、语义色 token 和组件样式未形成闭环 | 全站可切换、刷新保留、系统主题可作为首次默认值 |
| 历史对话 | `/chat/:id` 可恢复单个对话，但没有会话列表 API/UI | 只实现“按 ID 读取”，没有对话索引和活跃时间语义 | 新建、列出、切换、刷新恢复；消息与运行详情均可回看 |
| 图 1 工作流查询/编辑 | 能力摘要、适用场景输入框在暗色页面显示为白块 | `.inspector-fields label input` 未使用主题样式；目录质量又依赖这些元数据 | 两种主题均可读写；保存/发布后可被目录检索并解释命中依据 |
| 组件调用历史 | `Run.component_id` 已持久化，详情页只显示近 20 次统计值 | `/runs` 不能按组件过滤，组件详情页没有历史列表 | 组件详情可分页查看每次调用并进入完整 RunDetail |
| JSON + 自然语言结果 | Run 仅有 `output_json`；页面只渲染 JSON | 把执行契约和面向人的呈现当成同一层 | JSON 为事实源；按版本策略追加并持久化自然语言呈现 |
| Agent 决策与补参 | 未配置 TraeX 时直接取目录首项；stub 只抽 URL/`qN` | 静态降级路径承担了本应由 Agent 完成的意图判断和实体绑定 | Agent 决定是否调用、调用哪个以及 bindings；服务端只负责约束与校验 |
| 对话工作流开关 | 当前对话没有用户可控的工作流模式 | 是否披露/执行工作流完全由后端固定路径决定 | 每个对话默认开启；关闭后该对话的新任务全程使用 LLM/Agent |
| 耗时与 Token | 页面只有运行状态，没有一轮问题从发送到最终回复的总耗时；Token 未统一汇总 | Message、AgentRun、Run 和 Presentation 缺少统一 Turn 归属与用量账本 | 实时计时并持久化最终耗时；能可信统计时展示 Token 总量及来源 |

图 1 能直接确认的是表单样式缺陷；“查询是否正确”还必须通过“元数据保存 → 发布 Manifest → 搜索 → L0/L1
披露”整条链验收，不能只以输入框恢复可见作为完成。

## 3. 必须遵守的实现边界

### 3.1 决策权与执行权分离

- 通用对话中的意图理解、候选比较、参数提取由 LLM/Agent 负责。
- `workflow.search/describe/...` 继续作为 Agent 的渐进披露工具，不是最终路由器。
- Workflow/Fragment/临时 Plan 一旦选定并通过输入校验，后续节点仍由 Workflow Runtime 接管。
- 服务端必须校验 ref 已披露、输入符合 JSON Schema、权限/预算/副作用边界有效；不能盲信模型输出。
- 模型不可用时必须明确返回“语义选择服务不可用”并允许用户转通用 Agent/稍后重试，禁止静默退化为“目录第一项”。

`docs/schema.md` 中“必填参数不能依赖自然语言猜测”不禁止从用户明确表达中提取参数。正确边界是：Agent 可以将
“上海天气如何”中的“上海”绑定到 `city`，但不能凭空编造城市；每个绑定必须有来源和校验结果。

### 3.2 执行结果与展示结果分离

- `Run.output_json` 是规范、可组合、可复制、可重放的唯一业务事实源，不因自然语言展示而改变结构。
- 自然语言是可选的 Presentation，不得写回或覆盖规范输出。
- 只有直接面向最终用户且确有阅读价值的 Workflow 才启用 Presentation；内部 Fragment、被其他 Workflow
  组合的中间工作流默认关闭。
- 能用模板稳定表达时优先模板；只有需要归纳、解释或文本生成时才调用 LLM。
- Presentation 失败不能把已成功的 Workflow Run 改成 failed；页面仍应显示 JSON，并明确“文本整理失败”。
- 若自然语言本身就是业务输出的一部分，而非展示层，应在 DAG 中显式使用 LLM/Transform 节点并纳入
  Output Schema，不能借 Presentation 绕过运行契约。

### 3.3 历史必须以服务端事实为准

- 对话列表、组件调用历史、工作流调用历史和自然语言结果都必须持久化；禁止只保存在 React state/localStorage。
- localStorage 只保存主题偏好等设备级展示设置。
- Chat 消息引用 Run/ConversationRun；详情按引用读取，不从聊天文本反推执行事实。
- 历史 Run 必须继续绑定当时的 WorkflowVersion/PlanSnapshot，不能随默认版本升级而改写。

### 3.4 工作流模式与统计口径

- 工作流开关是**每个 Conversation 的显式设置**，新 Conversation 默认 `enabled`；切换后持久化，刷新不丢失，
  不影响其他 Conversation。
- `enabled` 表示 Agent 可以按任务需要自动发现、选择和执行 Workflow/Fragment/Plan，不表示每条消息都必须调用工作流。
- `disabled` 表示该对话后续新 Turn 的 Agent 工具授权中不包含 `workflow.search/describe/...`、工作流编译或工作流启动
  能力；不得只在 prompt 中写“不要调用”却仍把工具暴露给模型。
- 开关状态在发送消息时冻结到 Turn。切换只影响下一 Turn；已经开始的 Agent/Workflow Run 不被暗中取消，用户仍可通过
  原有取消入口显式终止。
- 关闭时若存在尚未执行的 PendingSelection，应将其标记为 `cancelled_by_mode_change`；旧候选卡不得在无提示的情况下
  继续启动工作流。组件/工作流详情页的人工运行入口不受 Chat 开关影响。
- 一轮耗时的权威口径为“服务端接受并持久化用户消息”到“该 Turn 的最终助手回复持久化完成”。前端从点击发送后立即
  乐观计时，收到服务端时间后校准。
- 最终回复包括成功答案、明确错误、取消、`ask_user` 或其他对本 Turn 的终止答复；如果还要流式生成 Presentation，
  应在 Presentation `completed/failed` 后停止总计时。失败接力期间计时继续，直到接力结果回到原对话。
- Token 只能汇总模型/provider 实际返回的 usage。未返回 usage 时显示“不可用/部分统计”，禁止填 0 或用字符数冒充 Token。

## 4. 实施任务

P0/P1 只表示推荐实施顺序，本章所有项目都是 019 通过条件，不代表 P1 可以延期。

### P0-1：改造对话中的 Agent 选择与参数绑定

- [ ] 在 Conversation 增加持久 `workflow_mode=enabled|disabled`，默认 `enabled`；提供读取/修改 API，并在 Chat 输入区
  附近增加清晰的“工作流：开启/关闭”控件和状态说明。
- [ ] 开启时由 Agent 根据任务自主决定是否使用工作流；普通闲聊和无合适候选任务仍走 Agent，不得为了体现“开启”而
  强制运行目录第一项。
- [ ] 开启状态下，Agent 选定工作流、bindings 校验通过且不需要审批时应自动启动 Workflow Runtime，不再要求用户点击
  “使用该组件”；存在副作用、必须审批或真实歧义时仍保留确认/追问边界。
- [ ] 关闭时从 AgentHost 的 requested capabilities、实际 ToolRegistry 和执行入口三层移除/拒绝工作流能力；服务端
  必须做最终 fencing，防止模型构造调用绕过 UI。
- [ ] 每条用户消息创建 Turn 时保存 `workflowModeSnapshot`；轨迹与历史消息展示该轮实际模式，避免用户切换后误解旧记录。
- [ ] 模式从开启切到关闭时闭合未执行的 PendingSelection；活动 Run 保持原状态并明确提示“切换将在下一条消息生效”。
- [ ] 删除 [`routes.py`](../backend/app/api/routes.py) 中未配置 `traex_command` 时“取 Registry 第一项并直接
  `ask_user/run_full`”的语义降级路径。
- [ ] 删除 [`traex_stub.py`](../examples/traex_stub.py) 中只识别 URL 和 `qN` 的 `concrete_input` 规则对产品验收的
  依赖；stub 仅可用于协议测试，不能作为“模型已判断”的证据。
- [ ] 扩展 `WorkflowSelection`，让 Agent 返回下列可审计信息：候选 ref、action、bindings、每个 binding 的
  用户文本/历史消息来源、置信度或“不确定”标记、未绑定字段和选择理由。
- [ ] Agent 必须先通过 L0 搜索获取候选；只对有希望的候选读取 L1/L2，比较 `useWhen/doNotUseWhen`、输入输出
  契约、副作用和成本后再决定 `run_full/run_fragment/compose/continue_agent/ask_user`。
- [ ] 服务端按选中版本的 Input Schema 对 bindings 做完整校验：required、type、enum、format、additional
  properties；不合法字段不得进入 Run。
- [ ] 只有在语义提取和 Schema 校验后仍缺少必要信息时才能 `ask_user`，问题只询问真正缺失或有歧义的字段。
- [ ] PendingSelection 继续支持多轮补参；新回答与已有 bindings 合并后重新校验，不能把整句无条件塞进唯一字段。
- [ ] 轨迹面板展示公开的决策摘要、L0/L1 调用、绑定来源和校验结果，不展示私有推理。
- [ ] 系统设置/健康信息明确显示当前选择模式：`agent` 或 `unavailable`；不得显示成“Agent”但实际走静态选择。

最低语义用例：

| 用户输入 | 预期 |
|---|---|
| `上海天气如何` | Agent 命中天气工作流，绑定 `city=上海`，不追问 city |
| `帮我看看天气` | 命中后只追问城市 |
| `总结这个网址 https://example.test/a` | 绑定 URL；如已有合适 Workflow 则复用，否则继续 Agent 探索 |
| `把 q12 的记录导出` | 绑定业务 ID，不因不是 URL 而丢失 |
| `比较上海和北京天气` | Schema 支持数组则绑定两项；只支持单城市时先澄清或组合，不静默取第一项 |
| 与目录无关的闲聊 | `continue_agent`，不得强行运行得分最高的工作流 |
| 工作流关闭后输入 `上海天气如何` | 不调用任何 `workflow.*` Tool，由通用 Agent 使用其获授权能力作答或明确能力不足 |

### P0-2：建立可查询的历史索引

#### 对话历史

- [ ] 新增 `GET /conversations?cursor=&limit=&query=`，按最近活动时间倒序返回
  `id/title/preview/lastMessageAt/status`，使用稳定游标分页。
- [ ] 新增消息时原子更新 Conversation 的 `last_message_at/updated_at`；不能依赖 ORM 在插入 Message 时自动触碰父表。
- [ ] 第一条用户消息后，将默认“新对话”生成可辨识标题；Local Demo 可先使用确定性截断，标题生成失败不得阻塞对话。
- [ ] Chat 页面增加历史侧栏：新建对话、当前项、标题、最近消息摘要、时间、加载更多和空状态。
- [ ] 路由切换时取消旧会话轮询/SSE，清空只属于旧会话的临时状态，再加载新会话，避免轨迹串线。
- [ ] 窄屏将历史侧栏折叠为抽屉；键盘可操作，当前会话有 `aria-current`。

本轮不把删除历史作为必需项；删除属于破坏性操作，待回收站/恢复语义明确后再做。可选的重命名不得阻塞 P0。

#### 组件与工作流调用历史

- [ ] 扩展 `GET /runs` 支持 `componentId/workflowVersionId/status/cursor/limit`，并新增
  `(component_id, created_at, id)` 等必要索引；不能拉取全部 Run 后在前端过滤。
- [ ] 组件详情页增加“调用历史”，展示状态、发起时间、耗时、输入摘要、规范输出摘要、Presentation 状态和实际
  WorkflowVersion。
- [ ] 点击记录进入现有 `/runs/:id`，完整查看输入、节点 attempt、事件、Artifact、错误、规范输出和文本结果。
- [ ] 手动组件调用、对话选择后调用和其他入口创建 Run 时都要正确写入 `component_id`；若直接运行
  WorkflowVersion，则保留 workflow lineage 并能在工作流详情查询。
- [ ] 增加“直接运行已发布 WorkflowVersion”的服务端入口和 JSON Schema 表单；草稿不能绕过发布直接进入正式
  运行历史，测试草稿应使用明确的 test/replay 语义。
- [ ] 工作流编辑/详情入口增加手动运行和该工作流版本的运行历史；直接运行产生的 Run 可没有 `component_id`，但必须
  记录实际 `workflow_version_id`，避免只能从组件反查。
- [ ] 列表 API 与详情 API 继续使用现有脱敏规则，历史页面不得暴露被 redact 的原值。

### P0-3：规范输出后流式补充自然语言结果

- [ ] 在发布版本中冻结 `presentationPolicy`，最少包含：`enabled`、`audience`、`producer=template|llm`、
  模板或 prompt/version digest、语言、最大长度。内部工作流默认 `enabled=false`。
- [ ] 固化分析根据“是否直接面向用户、输出是否可读、是否为其他流程中间节点”提出策略建议，由用户在发布前确认；
  不得仅凭工作流名称自动加 LLM。
- [ ] 新增持久 `RunPresentation`（推荐独立表，而非污染 `Run.output_json`），保存 run、状态、最终文本、producer、
  model/config digest、prompt/template digest、错误和时间。允许未来重新生成时保留 revision。
- [ ] Run 成功后先立即返回并展示规范 JSON；Presentation 随后通过 Run 事件/SSE 发送
  `presentation.started/delta/completed/failed`，前端逐步追加文本。
- [ ] `GET /runs/{id}` 返回已持久的 Presentation；刷新、历史列表进入详情和对话回看都能恢复最终文本，不能依赖
  已经结束的 SSE delta。
- [ ] RunDetail 分成“结构化结果”和“阅读结果”两个区域，分别提供复制按钮；复制 JSON 必须保持合法 JSON，复制文本
  只复制最终自然语言。
- [ ] Chat 中关联同一 `runId` 的消息展示相同双层结果，并确保 reload 后不重复生成、不重复插入消息。
- [ ] 记录 Presentation 的耗时、Token/模型（如使用 LLM）和失败原因；模板路径必须维持零 LLM。

建议数据关系：

```text
Run.output_json              规范事实，可组合/重放
  └── RunPresentation[*]    面向人的派生视图，可选、版本化、可失败

Message.content_json.runId ──→ Run ────────────→ NodeRun / RunEvent / Artifact
                                   └───────────→ RunPresentation
```

### P0-4：增加每轮计时与 Token 用量

- [ ] 建立持久 `ConversationTurn`（或等价聚合），至少包含：conversation、user/assistant message、父 Turn、
  `workflow_mode_snapshot`、status、accepted/first-response/completed 时间和最终 `elapsed_ms`。
- [ ] 用户消息、WorkflowSelection、ConversationRun、Workflow Run、continuation、Presentation 和最终助手消息均关联
  同一个 Turn；失败接力的子 Run 不能因为换了 Worker 或 generation 而丢失原 Turn 归属。
- [ ] Chat 在发送后立即显示单调递增计时器；刷新后根据服务端 `acceptedAt/status` 恢复，不得归零或同时启动多个 timer。
- [ ] 最终助手消息旁持久展示本轮总耗时。可另外展示 `timeToFirstResponseMs`，但不得用首个状态卡片的时间冒充完成耗时。
- [ ] 建立按模型调用记录的 Usage ledger，以稳定 `callId/attempt` 去重，至少保存 provider/model、阶段、input/output/
  cached/reasoning/total tokens、usage 是否完整和时间。
- [ ] Turn Token 汇总覆盖：工作流选择 Agent、通用探索 Agent、Workflow 内 LLM/Agent 节点、Presentation，以及失败接力
  后实际执行的模型调用；确定性节点不产生 Token。
- [ ] 同一次模型重试只有 provider 实际执行过的调用计入消耗；事件重放/SSE 重连不得重复累计。
- [ ] UI 在能完整统计时展示总 Token，并可展开查看各阶段；部分 adapter 不返回 usage 时显示“已统计 N，另有部分不可用”，
  全部不可用时显示“Token 不可用”，不能展示误导性的 `0`。
- [ ] `GET /conversations/{id}` 和历史接口返回 Turn 的模式、耗时和用量摘要；运行详情可以下钻到具体模型调用，刷新后一致。

建议数据关系：

```text
Conversation
  └── ConversationTurn (workflowModeSnapshot, elapsedMs, tokenSummary)
        ├── user/assistant Message
        ├── ConversationRun / continuation
        ├── Workflow Run / RunPresentation
        └── ModelUsage[*] (callId 去重、按阶段汇总)
```

### P1-1：完成亮色/暗色主题

- [ ] 在应用根节点接入现有 [`useTheme.ts`](../frontend/src/hooks/useTheme.ts)，提供可见的亮/暗切换按钮和文字/图标状态。
- [ ] 统一使用 `data-theme` 或等价单一机制；首次访问跟随 `prefers-color-scheme`，用户选择写 localStorage 并优先于系统值。
- [ ] 在首屏 React 挂载前应用已保存主题，避免主题闪烁。
- [ ] 把背景、面板、输入框、边框、正文、弱文本、代码块、成功/警告/错误、图节点和焦点环全部改为语义 token；
  清理散落的暗色硬编码。
- [ ] 原生 `input/textarea/select`、RJSF、React Flow、SSE/JSON 区域和 disabled/hover/focus 状态都必须覆盖两种主题。
- [ ] 主题按钮可通过键盘操作，具备 `aria-label/aria-pressed`；正文和关键控件满足 WCAG AA 对比度。

### P1-2：修复图 1 的目录元数据编辑与查询闭环

- [ ] 给 `CapabilityMetadataEditor` 的 label/input 使用统一表单组件或明确样式。当前 `.inspector-field input` 规则不会命中
  `.inspector-fields > label > input`，这是截图白块的直接根因。
- [ ] 能力摘要、适用/不适用场景、示例和 tags 均能在可视化编辑器读取和编辑，不应要求用户切到 JSON 视图完成核心目录信息。
- [ ] 保存后重新加载仍显示相同值；发布后的 `WorkflowCapabilityManifest.search_text` 必须包含这些字段。
- [ ] 对目录公开的 Workflow，发布检查至少要求非空 summary 和 useWhen；错误定位到具体字段。
- [ ] 搜索结果展示 `matchReason` 的用户可读摘要；`doNotUseWhen` 参与 Agent 排除，而不是仅作为正向搜索文本提高得分。
- [ ] 为中文无空格、同义表达和业务 ID 保留候选召回；最终选择仍由 Agent 基于 L1 契约判断，搜索分数不能直接等于执行决定。

## 5. 推荐实施顺序

1. 先定义 ConversationTurn、工作流模式快照、时间和 Usage 契约；否则后续各类 Run 无法稳定归属同一轮问题。
2. 完成 P0-1，让开关真正约束 Agent 工具并让开启状态下的语义选择正确。
3. 建立对话/Run 查询 API、Usage ledger 和持久 Presentation 数据模型，完成迁移与索引。
4. 完成 P0-2 历史 UI 和 P0-4 计时/Token UI，使每次手动/对话调用都有可追溯入口。
5. 完成 P0-3 双层输出及流式展示，确保刷新和历史回看一致。
6. 完成主题 token 与图 1 表单修复，并对两种主题做真实浏览器验收。
7. 最后跑完整真实场景，不以 stub 的正则分支、HTTP 冒烟或仅单元测试代替 Agent/浏览器交互。

## 6. 测试与验收门禁

### 6.1 自动化门禁

- [ ] 后端：Conversation 游标分页/排序、活跃时间并发更新、Run 组合过滤、Schema binding 校验、Presentation 持久化/
  失败降级/幂等，以及工作流模式的 capability fencing。
- [ ] Turn 状态机：默认开启、模式快照、关闭 PendingSelection、切换不影响活动 Run、终态耗时、失败接力归属和刷新恢复。
- [ ] Usage：各阶段汇总、retry/callId 去重、SSE 重放不重复、完整/部分/不可用三态和零 LLM 路径。
- [ ] 前端：主题偏好、会话切换状态隔离、工作流开关、历史分页、计时器恢复、Token 展示、Run 历史入口、JSON 与
  文本复制、Presentation 重载恢复。
- [ ] 契约：旧 Run 没有 Presentation 时仍可读取；迁移 `upgrade` 通过；如项目要求可回退，`downgrade` 也必须通过。
- [ ] `make demo-check` 保持全绿，现有探索—固化—复用—失败接力门禁不得回归。

### 6.2 真实前后端场景

实际启动 API、Worker、Vite、可用的 LLM/TraeX Agent 和真实浏览器，完成下列流程：

1. 在亮色主题打开工作流编辑器，填写摘要/useWhen，保存、刷新、发布；切暗色后字段仍清晰可读。
2. 新建对话，确认工作流默认开启。输入“上海天气如何”；轨迹显示 Agent 的 L0/L1 决策和 `city=上海` 的来源，
   不出现“请补充 city”，无副作用的天气 Workflow 无需再点候选卡即自动启动。
3. 发送时计时器立即开始；Run 先显示规范 JSON，再流式出现自然语言天气说明；最终文本完成后计时停止并显示总耗时及
   Token（provider 提供 usage 时）。刷新后时间、用量和两种结果均存在且各一份。
4. 关闭工作流后再次输入同类问题，确认全程没有 `workflow.*` Tool 或 Workflow Run，由通用 Agent 给出答复；历史中该轮
   `workflowModeSnapshot=disabled`。重新开启后下一轮可再次自动选择工作流。
5. 在工作流运行中切换开关，确认当前 Run 不被暗中取消且 UI 提示“下一条消息生效”；创建 awaiting-input 选择后关闭，
   确认 PendingSelection 被安全闭合。
6. 新建至少两个对话，再刷新浏览器；各对话的开关状态、历史顺序、计时/Token、消息、Run 和轨迹正确恢复且互不串线。
7. 从组件详情手动调用两次（一次成功、一次可控失败），历史列表出现两条，分别进入 RunDetail 查看完整输入、节点、事件、
   错误/结果和 Presentation。
8. 调用一个内部 Fragment/中间工作流，确认没有无意义的 LLM Presentation，规范输出仍可被组合；若全程零 LLM，
   Token 明确显示 0，若 adapter 不回 usage 则显示不可用而不是 0。
9. 执行一次真实失败接力，确认计时从原问题持续到 continuation 最终回复，Token 汇总包含接力调用且没有因事件重放重复。
10. 停用选择模型后发送新请求，页面明确显示选择服务不可用；不得静默选择目录第一项。
11. 浏览器控制台无 error；两种主题下无白块、不可读文字、焦点不可见或布局遮挡。

### 6.3 通过标准

以下条件必须同时成立，019 才能判定通过：

- 用户能够在亮/暗主题间切换，图 1 字段在两种主题均可读、可编辑、可持久化。
- Chat 有持久历史列表，切换/刷新不会丢消息、串轨迹、重置计时或重复最终结果。
- 新对话默认开启工作流；关闭后的新 Turn 服务端不会披露、选择或执行工作流，重新开启后恢复 Agent 自动选择。
- “上海天气如何”由 Agent 完成工作流选择和 `city` 绑定；模型不可用时不做静态伪选择。
- 组件详情能查询所有入口产生的该组件调用历史并进入完整运行详情。
- 面向用户的 Workflow 同时保留规范 JSON 与可选自然语言；先 JSON、后流式文本，历史回看一致。
- 每轮从服务端接受问题到最终回复都有持久总耗时；Token 在有可信 usage 时按阶段准确汇总，缺失时明确标注统计范围。
- 内部工作流默认不增加 LLM；Presentation 失败不污染成功 Run 的规范结果。
- 018 已通过的首次探索、固化、复用、失败接力和修订能力无回归。

## 7. 明确不接受的“完成方式”

- 为天气、上海或 `city` 增加硬编码正则。
- 用目录得分第一名代替 Agent 决策，或模型不可用时假装已由 Agent 判断。
- 工作流关闭时只修改按钮或 prompt，服务端仍向 Agent 暴露/接受 `workflow.*` Tool。
- 把工作流开关做成全局临时变量，导致刷新丢失、不同对话互相覆盖，或切换时暗中取消已运行任务。
- 把自然语言文本塞进任意 JSON 字段，导致既有 Output Schema/组合调用失效。
- 给每个 Workflow 无条件增加一次 LLM 调用。
- 只在当前页面内保存历史，刷新后依赖内存恢复。
- 前端一次拉取所有 Run 再过滤组件历史。
- 只用浏览器内存计时，刷新归零；或以首个中间状态代替最终回复耗时。
- provider 没有返回 usage 时显示 0 Token，或因 retry/SSE 重放重复累计 Token。
- 只修截图中的白色输入框，不验证元数据保存、发布和搜索。
- 用单测、mock/stub 或 HTTP 冒烟代替最终真实前后端和真实 Agent 场景。

**019 当前状态：待实现；完成上述门禁后再进入 020 审阅。**
