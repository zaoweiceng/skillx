# NEXT_STEPS-029：先跑通真实 Chat 主路径，再完成通配型闭环验收

依据：[`REPLAY-028.md`](../REPLAY/REPLAY-028.md)、
[`design-compliance-review-029.md`](../review/design-compliance-review-029.md)、
[`code-review-029.md`](../review/code-review-029.md) 和 [`docs/`](../docs/) 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。天气、网页、文件都只能作为验收样本；
禁止在生产代码、seed、Prompt 路由和 Presentation 中增加领域专用分支。

## 1. 029 完成定义

下一版只有同时满足以下事实才能声明通过：

1. 工作流默认开启时，真实 TraeX 能渐进发现能力、从同一消息提取参数并启动 Workflow；
2. 工作流关闭时，真实 TraeX 返回符合 Host Schema 的普通回答，全程零 `workflow.*`；
3. 上述两条路径的预算在调用前冻结，UI 正确展示耗时、完整/部分 Token 和错误；
4. 成功 Agent trajectory 能固化、审阅、发布，并由下一次自然语言对话重新发现和复用；
5. 真实失败能从冻结 checkpoint 继续，不重做已完成 ToolResult，并可按证据生成受控修订；
6. 完整工程门禁、Browser UI、产品级真实 Provider gate 和人工点击验收全部通过。

## 2. P0：让 WorkflowSelection 真正渐进披露

### 2.1 先测量，再定协议

- [ ] 对真实 TraeX 分别测量无工具、仅 `workflow.search`、每个目录工具及当前五工具组合的 Tool Schema 字节数、
  input/output/total Usage 和时延；结果进入测试 Artifact，不记录用户正文或 Secret。
- [ ] 确认 66,220 input Token 的组成，区分固定 TraeX context、output schema、目录 Tool descriptors 和用户 Prompt；
  预算不能靠猜测常量。
- [ ] Settings 分别展示 probe profile 与 WorkflowSelection profile，避免用 32k probe 估算掩盖 66k 选择成本。

### 2.2 重构发现协议

- [ ] 第一阶段只向模型暴露 L0 `workflow.search`；不得在首轮同时注入 describe/slice/compile 的完整 Schema。
- [ ] 选择以下一种通用实现并写明不变量：
  - 多阶段 AgentHost 调用：每阶段只有本阶段 Tool，服务端把已验证 disclosure 作为下一阶段输入；或
  - 单一窄目录工具：`action` 枚举由服务端状态机和 disclosure session 限制，返回级别仍为 L0/L1/L2。
- [ ] 每次 describe/slice/compile 必须引用服务端已披露 ref；禁止模型自报 ref、Schema 或计划绕过 session。
- [ ] 每阶段冻结 `maxInputTokens`、`maxOutputTokens`、`maxBillableTokens` 和剩余步骤，Usage 汇总到同一 Turn；
  超预算应在能预估时发送前拒绝。
- [ ] 继续保持同消息 bindingSources、quote/messageId/confidence 和服务端参数重建验证；不得回退到关键词匹配。

### 2.3 通过条件

- [ ] 实际输入“请问候 Alice”时，由真实 Agent 发现问候组件、绑定 `name=Alice` 并成功运行；
- [ ] 实际输入“上海天气如何”可作为第二个样本，但只能复用同一目录机制；
- [ ] 无合适组件时返回 `continue_agent`，不是伪造候选或要求无关补参；
- [ ] 三种场景的 Tool timeline、disclosure level、binding sources、预算和最终 Run 均可刷新回看。

## 3. P0：统一构造严格的 Agent 终态 Prompt

- [ ] 新建唯一的 Host request builder，把用户文本作为引用数据包裹，附加平台级规则：最终输出必须是严格 JSON、
  必须匹配传入 Schema、不得输出 Markdown fence 或额外文本。
- [ ] 通用对话 `{reply:string}`、WorkflowSelection、Distillation、Capability 和 Presentation 全部复用同一终态构造；
  业务 Prompt 只能补充任务，不得覆盖终态契约。
- [ ] 继续让 adapter 严格校验 JSON 和 Schema；禁止把普通文本静默包成 `reply`，否则会掩盖 provider 不兼容。
- [ ] 普通用户要求“简短回答”“只回复某词”等不应破坏 JSON 终态，UI 最终只展示 `reply` 文本而非 JSON 外壳。
- [ ] 实测工作流关闭后的无工具回答、lookup 工具回答、browser.read 回答；三者都必须成功并保持零
  `workflow.*`。
- [ ] 对 malformed output 提供一次受预算约束的格式修复策略时，必须显式记为第二次模型调用；若不修复则准确失败，
  不得隐式重试或切换 Runtime。

## 4. P0：建立产品级真实 Provider 门禁

保留现有 `make traex-real-check` 的快速 probe，并新增显式付费授权的产品级门禁，例如
`make traex-real-product-check`：

- [ ] 使用 PATH 中真实 `traex`、全新 SQLite、真实 API 与独立 Worker；不得替换成 fixture executable；
- [ ] workflow-on 完成 search→describe→参数绑定→确定性 Run；
- [ ] workflow-off 完成普通回答且无 workflow tools；
- [ ] 运行中取消能够终止真实子进程并冻结 `cancelled` 终态；
- [ ] 原生 failed terminal、malformed output、超时至少各验证一次准确映射；
- [ ] 输出只记录 provider/model、Usage、事件类型和 digest，不保存 Prompt、登录 token 或敏感正文；
- [ ] 无登录或未授权费用时明确 skipped/blocked；fixture gate 不得代替 product gate passed。

## 5. P1：补齐 Presentation 的真实语义输入

- [ ] 在成功首次任务或固化请求中记录 `resultConsumer`、`entrypointKind`、是否顶层回复、是否计划作为 Fragment 组合等
  可审计事实；未知必须保留 unknown，不能硬编码“human_or_composition_unknown”。
- [ ] 先生成 callable Fragment proposals，再调用 Presentation Agent，使 `hasCallableFragments` 来自真实定义。
- [ ] Presentation Agent 输入只含去实例化 intent、consumer、entrypoint、I/O Schema 和数据流，不含实例 URL、正文、
  Secret 或一次性参数。
- [ ] 顶层直接给用户看的任务与组合中间结果，即使节点类型相同，也必须能够产生不同 enabled/producer 决策；
  证据不足时默认关闭并要求人工选择。
- [ ] accepted 必须与规范化 proposal policy 完全一致；编辑后只能记录 modified；rejected 固定为关闭。
- [ ] proposal、decision、两个 digest、reviewer/time 和最终不可变 policy 刷新后保持一致。

## 6. P1：完成真实“探索—固化—复用—失败接力—修订”闭环

主路径修复后，按通用机制完成至少三个差异样本：

- [ ] 信息查询：首次由 Agent 选取只读 Tool，固化后不同输入直接复用；Presentation 可由 Agent 提议模板；
- [ ] 网页或长文本：首次真实读取并总结，第二次重新读取变化内容；Presentation 可提议 LLM/Agent 整理；
- [ ] 文件/组合 Fragment：同时覆盖顶层人类结果与机器中间结果，证明 consumer/entrypoint 会改变策略。

每个样本必须完成：首次成功 trajectory → 固化草稿 → Presentation 明确审阅 → 静态检查 → 人工发布 →
新对话自然语言发现 → 不同输入二次复用。至少一个样本还要完成真实瞬态失败、有限重试耗尽、Worker 接力、
外部恢复后只执行剩余节点；可复现契约漂移再进入 RevisionProposal/ReplayEvidence，transport/timeout 不得直接修订。

## 7. P2：修复前端状态与显示一致性

- [ ] Settings 同时使用 credentialPresent 和 activeProbeAuthenticated：新鲜成功 probe 后显示“主动验证成功”，
  不再提示重复付费探测；过期后再提示重新验证。
- [ ] `WorkflowGraph` 与 `EditableCanvas` 对 `null/undefined` 的 `edge.when` 都不渲染标签，并增加回归测试。
- [ ] Presentation “接受提议”按钮只应用 proposal；任意表单变更将决定状态重置为 pending，用户只能“修改后接受”。
- [ ] 在 760、903、1024、1280、1440 宽度检查亮暗主题；DAG、能力摘要、表单和历史不得重叠、截断或产生不可点击区。
- [ ] Browser console error/warning 保持为空，复制 JSON/文本需给成功或失败反馈。

## 8. 最终交付门禁

1. `make demo-check` 连续两次通过，后端、前端、迁移和独立进程 E2E 全部通过；
2. `make browser-ui-check` 连续两次通过，实际点击主题、导航、编辑、检查、发布、运行、复制和历史；
3. 快速真实 probe 与产品级真实 Provider gate 都通过，报告明确 Usage 与费用授权；
4. 全新真实栈中 workflow-on/off 两条 Chat 路径均成功，刷新后历史、计时、Token、Tool timeline 保持一致；
5. 三个跨领域闭环和一次真实失败接力/受控修订完成；天气不得出现在任何通用分支；
6. Settings 不再显示矛盾状态，DAG 不显示 `null` 标签；
7. 文档链、`git diff --check` 和提交归档检查通过；报告严格区分 fixture、真实 Provider probe 和真实产品流程。

在真实 Chat 两条入口和至少一个完整闭环跑通前，不得因 `make demo-check` 或 probe 通过而声明整体通过。
