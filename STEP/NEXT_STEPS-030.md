# NEXT_STEPS-030：先让同消息参数一次绑定，再恢复真实产品门禁

依据：[`REPLAY-029.md`](../REPLAY/REPLAY-029.md)、
[`design-compliance-review-030.md`](../review/design-compliance-review-030.md)、
[`code-review-030.md`](../review/code-review-030.md) 和 [`docs/`](../docs/) 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。天气、问候、网页和文件只能作为黑盒验收
样本；禁止在生产代码、seed 路由、参数绑定或 Presentation 中加入领域专用判断。

## 1. 030 完成定义

下一版只有同时满足以下事实才能声明通过：

1. 工作流默认开启时，真实 TraeX 从用户同一条消息发现 workflow、提取并验证全部明确参数，一次启动运行；
2. 无匹配能力或参数确实缺失时才由 Agent 继续或追问，系统不以空 binding 掩盖模型遗漏；
3. 真实产品门禁使用与生产相同的 runtime resolver，workflow-on/off 与 running cancellation 全部通过；
4. 目录失败的原因与重试过程可审计，相同确定性错误不会重复执行；
5. 无动作回答不会提供不可用的固化操作；
6. 至少一个非领域特化的“探索—固化—发布—新对话复用—失败接力—受控修订”真实闭环通过。

当前为 Demo 阶段，Token 上限与消耗不纳入修复范围或通过条件；只保留现有统计展示，便于观测真实调用。

## 2. P0：把能力选择与参数提取变成可验证的两阶段协议

### 2.1 协议

- [ ] 第一阶段只负责渐进发现并返回 action/ref/plan/reason，不要求在尚未知动态 Schema 时同时完成 bindings。
- [ ] 服务端获得选中 ref 后解析真实 input schema，并冻结 ref、schema digest、当前/历史消息 ID 和 disclosure session。
- [ ] 第二阶段仍调用 Agent Runtime，输出严格的 `bindings`、`bindingSources`、`missingInputs`；Schema 要求每个
  返回 binding 有且仅有一个 source，并明确列出所选 workflow 的 required 字段。
- [ ] 服务端继续逐字段验证 messageId、原文 quote、tool result path、confidence、normalization 和 JSON Schema；
  所有可执行输入必须由服务端从受信证据重建，不能直接信任模型值。
- [ ] 若 required 值在当前消息中存在且可验证，但模型遗漏，最多允许一次结构化纠正；仍失败则
  返回 `SELECTION_BINDING_INCOMPLETE` 和审计事实，不能对用户伪报“你没有提供”。
- [ ] 只有证据确实不存在、存在多个无法消歧的值或 schema 校验失败时才创建 pending selection 并 ask_user。
- [ ] follow-up 继续复用冻结的 ref/schema/session，且不得重新做已完成的 search/describe。

### 2.2 黑盒通过条件

- [ ] 同一句 `请问候名叫 Alice 的人，姓名参数是 Alice` 一次完成选择、`name=Alice` 来源验证和 Workflow Run；
- [ ] 参数缺失的 `请问候一个人` 只追问一次，补充后不重新搜索；
- [ ] 同一句含两个候选姓名时由 Agent 说明歧义，不按关键词位置猜测；
- [ ] 非问候的文件路径、URL、数字、枚举和嵌套对象样本复用同一协议，证明没有领域特化；
- [ ] 刷新页面后 binding source、disclosure timeline、耗时、Token、runId 和最终结果仍可回看。

## 3. P0：修复真实产品门禁本身

- [ ] 删除测试中的 legacy `SKILLX_AGENT_PROVIDER_TYPE=real` / `SKILLX_TRAEX_COMMAND` 组合；通过生产
  `agent_runtime_config()` 使用 PATH auto-discovery，或只设置生产明确支持的 builtin 开关。
- [ ] 栈启动后先断言 runtime `configured=true`、`runtimeType=traex_builtin`、source 为 auto-discovery，再付费 probe；
  配置不符应 fail，不得把错误配置报告为外部登录 blocked。
- [ ] workflow-on 用例断言首条消息直接得到 `workflow_run`，并校验 bindings、verified sources 和最终输出。
- [ ] workflow-off 断言成功 `{reply}` 且 events 中零 `workflow.*`。
- [ ] cancellation 使用最小已授权只读 capability（例如通用 lookup fixture adapter），确认真实进程已 running 后取消；
  不得用空能力集绕过 Host fail-closed 契约。
- [ ] 门禁退出必须停止 API、Worker 和模型子进程；只输出 provider/model、Usage、状态、digest，不保存正文和凭据。
- [ ] 保留快速 `traex-real-check`，但文档与 CI 明确区分 probe passed 和 product passed。

## 4. P1：让目录失败可诊断、可归因

- [ ] 所有 catalog 调用（成功和失败）都进入 Turn timeline；投影 action、disclosure level、session digest、参数 digest、
  错误码、retryable 和 attempt。
- [ ] 区分 tool schema 拒绝、未知 action、未披露 ref、状态机顺序错误、adapter/runtime 失败和真实瞬态错误。
- [ ] 前端失败项显示简短可行动原因；详情可查看去敏 diagnostics，不能只显示 level 空值和“失败”。
- [ ] disclosure 历史与 ModelUsage 能关联同一 callId；失败不写入成功披露状态，但必须保留审计记录。
- [ ] 日志、事件和 Artifact 禁止写入用户全文、ToolResult 全文、URL Secret 或登录凭据。

## 5. P2：固化按钮按轨迹资格显示

- [ ] 后端 ConversationRun 摘要增加 `distillationEligibility`：eligible、actionCount、reasonCode；资格由唯一领域服务计算。
- [ ] 前端只在 eligible 时启用“固化为组件”；无工具普通回答显示“本次没有可复用动作”等静态说明。
- [ ] 后端继续保留最终校验，防止客户端绕过；按钮隐藏不能代替 API guard。
- [ ] 覆盖无工具成功、单工具成功、多工具成功、失败/取消和已有草稿五种 UI 状态。

## 6. 完成通配型真实闭环

主路径修复后，至少选择一个不依赖天气或问候特化的真实任务完成：

1. 新对话中由通用 Agent 使用受控只读 Tool 完成首次探索；
2. 保存成功 trajectory，生成固化草稿与 Fragment；
3. Presentation Agent 基于真实 consumer/entrypoint 提议，人工接受或修改；
4. 静态检查后发布不可变版本；
5. 第二个新对话以不同输入渐进发现并直接复用，参数来自同一句话；
6. 制造可恢复的真实瞬态失败，接力只执行剩余节点；
7. 对可复现契约漂移生成 RevisionProposal/ReplayEvidence，人工发布新版本；网络波动不得触发工作流修订。

随后再以动态网页/长文本和文件/组合 Fragment 各做一个差异样本，验证工作流既可顶层输出自然语言，也可作为
机器消费的中间结果；Presentation 策略由 consumer/entrypoint 决定，不由节点类型或领域名决定。

## 7. 前端与最终交付门禁

- [ ] 亮/暗主题下实际点击对话历史、新对话、工作流开关、计时/Token、组件筛选、调用历史、RunDetail、
  JSON/文本复制、工作流 JSON/画布、检查、发布、能力和设置。
- [ ] 760、903、1024、1280、1440 宽度无重叠、截断、不可点击区；console error/warning 为空。
- [ ] `make demo-check` 连续两次通过；迁移、后端、前端、独立进程 E2E 和文档链全部通过。
- [ ] `make browser-ui-check` 连续两次通过。
- [ ] `make traex-real-check` 与修复后的 `make traex-real-product-check` 都实际通过，不能 skip。
- [ ] 全新真实栈中 workflow-on/off、running cancellation 和至少一个完整闭环通过，历史刷新后证据仍在。
- [ ] `git diff --check`、文档链和临时文件检查通过；报告严格区分 fixture、probe、产品门禁和人工真实场景。

在首条消息参数仍被错误追问、真实产品门禁仍不能启动目标 Runtime 时，不得声明整体通过。
