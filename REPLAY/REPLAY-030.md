# REPLAY-030：动态 Schema 两阶段绑定与真实产品门禁修复

日期：2026-08-12

依据：[`NEXT_STEPS-030.md`](../STEP/NEXT_STEPS-030.md)、
[`code-review-030.md`](../review/code-review-030.md) 和
[`design-compliance-review-030.md`](../review/design-compliance-review-030.md)。

## 1. 结论

030 指出的两个 P0、一个 P1 和一个 P2 的代码根因已按最小范围修复；未新增业务能力、领域路由或 Token 策略。
两轮完整工程门禁和两轮 Browser UI 门禁通过。真实 TraeX probe/product gate 的实现已修正，但外部调用在启动前
因缺少对本机登录态及外部模型调用的明确授权被安全策略拒绝，因此本轮结论是：**代码修复与本地产品流程通过，
真实 TraeX 产品验收 blocked，不声明 NEXT_STEPS-030 整体通过。**

## 2. P0：选择与参数绑定拆成两阶段协议

第一阶段 `WorkflowSelection` 只允许返回 action、ref/plan、reason 与所需能力，不再在尚未知动态输入契约时接收
bindings，也不允许把模型遗漏改写成 ask_user。

服务端取得已披露 ref 后解析真实 input schema，并用同一个 Agent Runtime 执行第二阶段
`WorkflowBindingExtraction`。第二阶段输出 Schema 按所选工作流动态生成，要求：

- 每个 required 字段恰好有一条 found/missing/ambiguous assessment；
- 每个 found binding 恰好有一个 binding source；
- source messageId 只能来自冻结的当前/历史消息集合；
- quote、confidence、uncertain、tool result path、normalization 和 JSON Schema 继续由服务端验证；
- ref、input schema digest、消息 ID 集合和 disclosure session 在 pending selection 中冻结，恢复时检查 Schema 未漂移；
- 首轮若报告缺失/歧义或结构/来源不完整，只允许一次结构化纠正；第二次仍违反契约时返回
  `SELECTION_BINDING_INCOMPLETE` 并保留 ModelUsage/Turn 失败事实。

证据确实不存在或存在无法消歧的候选时才创建 pending selection。补充消息继续复用原 ref、Schema 和 disclosure
session，不重新执行 search/describe。

定向黑盒覆盖了同消息 `name=Alice` 一次启动、缺参只追问并恢复、两个姓名不猜测、伪造 quote 拒绝，以及
selection/binding 两次模型调用的独立 Usage 审计。生产代码没有姓名、问候、天气、URL 或文件领域分支；问候仅是测试
Manifest 和 fixture 样本。

## 3. P0：真实产品门禁改用生产 Runtime resolver

产品门禁启动全新栈时不再注入 legacy `SKILLX_AGENT_PROVIDER_TYPE`、`SKILLX_TRAEX_COMMAND` 或 allowed executable
配置，而是启用生产支持的 builtin resolver，并在 probe 前断言：

- `configured=true`；
- `runtimeType=traex_builtin`；
- `source=auto_discovered`。

workflow-on 用例改为首条消息包含完整 `Alice` 参数，并断言直接得到 `workflow_run`、binding source 经服务端验证、
最终输出为 `你好，Alice`。running cancellation 使用最小只读能力 `lookup.table`，不再以空能力集绕过 Host
fail-closed 契约。测试栈仍统一负责停止 API、Worker、表单服务及其子进程。

默认运行该文件会明确显示 2 skipped/blocked，不使用 fixture 冒充真实产品结果。实际外部 gate 本轮未获授权，详见
第 7 节。

## 4. P1：目录失败诊断与审计

目录工具 schema、状态机与 adapter 确定性失败现在映射为稳定、不可重试的错误码，并通过 Host error policy 阻止相同
错误的内部重试。失败的 disclosure call 也写入持久化历史，但不更新成功披露状态。

持久化记录只包含 action、参数 digest、model call ID、tool call ID、错误码和 retryable，不保存用户全文、ref 原文、
ToolResult 全文或 Secret。Turn timeline 同时投影 disclosure level、session digest、参数 digest、attempt、retryable 和
去敏 diagnostics；前端显示 action、尝试次数及可行动错误原因。

## 5. P2：固化资格由服务端唯一裁决

`distillation_eligibility()` 统一计算 eligible、actionCount 和 reasonCode；ConversationRun 摘要返回
`distillationEligibility`，distill API 在进入编译前使用同一规则拒绝客户端绕过。

前端只在 eligible 时显示“固化为组件”。无动作成功回答显示“本次没有可复用动作”，已有草稿显示对应静态说明。
后端测试覆盖无动作、单动作、多动作、failed、cancelled 和已有草稿六种状态；前端覆盖有动作成功与无动作成功。

## 6. 最小修改边界

本轮复用既有 AgentHost、WorkflowSelection、PendingSelection、Tool Broker、ModelUsage 和 Distillation 数据模型，没有
新增数据库迁移、API 资源、业务功能或领域特化规则。未修改 Token/billable 上限；Demo 继续沿用现有长任务上限与
统计展示。

## 7. 验证记录

| 检查 | 结果 |
|---|---|
| Progressive selection 定向测试 | 15 passed |
| 选择、目录与 Tool Broker 回归 | 27 passed |
| 固化、AgentRun 与两阶段绑定定向回归 | 67 passed |
| 前端 check / lint / test | 通过；11 files / 54 tests |
| `make demo-check` 连续两次 | 两次均通过；每次 339 backend、54 frontend、37 独立进程 E2E、迁移 0001→0023、生产构建及文档链 |
| `make browser-ui-check` 连续两次 | 宿主权限下 2 passed + 2 passed；sandbox 内首次仅因 Chromium MachPort permission denied 未启动页面 |
| 真实产品测试默认状态 | 2 skipped，明确提示需设置 `SKILLX_RUN_TRAEX_REAL_PRODUCT_CHECK=1` 授权 |
| `make traex-real-check` | **blocked**；安全策略在进程启动前拒绝使用本机登录态调用外部模型 |
| `make traex-real-product-check` | 未继续执行；同一明确授权条件未满足 |
| `git diff --check` | 通过 |

## 8. 尚未形成的真实事实

只有在用户明确授权“使用本机已登录 TraeX 向外部模型发送测试任务并可能产生费用”后，才能补齐：

1. 真实 TraeX 快速 probe；
2. 全新栈 workflow-on 同消息绑定、workflow-off 零 workflow 工具与 running cancellation；
3. 非问候的真实探索—固化—发布—新对话复用—失败接力—受控修订完整闭环；
4. 动态网页/长文本、文件/组合 Fragment 的差异样本和刷新后审计证据。

在这些事实完成前，030 的整体状态保持 blocked；fixture、单元测试和 Browser UI 结果只证明实现与本地产品流程，
不能替代真实 Provider 验收。
