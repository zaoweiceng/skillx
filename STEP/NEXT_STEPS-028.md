# NEXT_STEPS-028：校准真实 TraeX 协议与预算，交付 Agent 驱动的通用固化决策

依据：[`REPLAY-027.md`](../REPLAY/REPLAY-027.md)、
[`design-compliance-review-028.md`](../review/design-compliance-review-028.md)、
[`code-review-028.md`](../review/code-review-028.md) 和 [`docs/`](../docs/) 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。天气、网页总结、文件操作都只允许作为验收样本；
通用代码、seed、Prompt 路由和策略不得按这些领域名称或字段写分支。

## 1. 028 完成定义

下一版只有同时满足以下事实才能声明通过：

1. 真实 TraeX 主动探测在明确、合理的上下文/输出/费用预算内成功，Provider 变为 usable；
2. 工作流开启时 Agent 完成 L0/L1 选择和同消息参数绑定，关闭时完成普通回答且零 `workflow.*`；
3. 原生 JSONL 的成功、认证失败、模型失败、超时和 malformed output 被准确区分，审计事实完整；
4. Presentation 由受限固化 Agent 根据任务目的提出，用户能看到依据并明确接受/修改/拒绝；
5. 至少三个不同领域用真实 Agent 走完首次探索、固化、发布、第二次复用、失败接力与修订；
6. 完整工程门禁、真实 Provider gate 和人工 UI 验收全部通过。

## 2. P0：定义并实现原生 TraeX JSONL 状态机

- [ ] 保存并解析原生事件的受支持版本：thread/turn started、item started/completed、turn completed/failed、error、Usage、
  model/provider identity；未知事件只记录安全摘要。
- [ ] `turn.failed`/`error` 优先于进程退出码和 output file；从事件内容规范化 `TRAE_AUTH_REQUIRED`、
  `TRAE_MODEL_UNAVAILABLE`、`RATE_LIMITED`、`TIMEOUT` 等错误。
- [ ] 只有 `turn.completed` 且 output file/last agent message 通过严格 Schema 才成功；成功终态缺少 JSON 才是
  `MODEL_MALFORMED_OUTPUT`，不得统一归为版本不兼容。
- [ ] `AGENT_PROTOCOL_INCOMPATIBLE` 只用于能力协商失败、事件版本不支持或无法解释的协议结构，并给出实际版本证据。
- [ ] dispatch 只来自明确模型事件/请求 ID；`turn.started`、认证预检和错误事件不算 dispatch。
- [ ] 覆盖原生 CLI 可能以 0/非 0 退出的 failed turn，并确保两者得到相同业务错误。

## 3. P0：重新设计 Token 与费用预算

- [ ] 记录真实 TraeX 在空工具、单工具、工作流目录工具三种情况下的系统/输入上下文基线；查明 31,314 input Token
  的来源，验证 user config、插件、技能和无关规则确实未进入执行上下文。
- [ ] 若 `--ignore-user-config` 与空 plugins/skills 仍扫描或注入无关内容，选择原生支持的最小 harness/config；不能仅
  提高预算掩盖上下文膨胀。
- [ ] 把 `maxInputTokens/contextBudget`、`maxOutputTokens`、`maxBillableTokens` 分离；所有上限在调用前冻结并进入
  Runtime snapshot/Usage。
- [ ] Provider probe 使用真实测量后的专用 budget profile；发起前在 Settings 展示预计上下文量级和可能费用。
- [ ] Host 应尽可能在发送前拒绝超预算请求；Provider 返回 Usage 后仍执行硬上限，但不得让标称 2k 的探测实际消费
  31k 后才报错。
- [ ] 通用 Agent、工作流选择、固化、Presentation 分别配置合理预算，不能共享一个低于基础输入上下文的 30k 常量。

## 4. P0：恢复认证并跑通真实主流程

- [ ] 用户在终端执行 `traex logout`/`traex login` 刷新当前已过期 token；应用不读取、不回显、不保存 token。
- [ ] installed、credentialPresent、activeAuthenticated、modelReachable、protocolCompatible、toolRoundTrip、usable 分别
  由自己的证据更新，禁止都赋值为整体 `probe_ok`。
- [ ] Settings 探测完成随机 challenge、ToolResult round-trip、strict output、真实 provider/model/Usage 后才 usable。
- [ ] 工作流开启输入普通自然语言，由 Agent 渐进披露、选择精确 ref、从同一消息提取参数并执行；禁止关键词回退。
- [ ] 工作流关闭输入相同需求，整轮零 `workflow.*`，由通用 Agent 直接回答或准确说明能力不足。
- [ ] 验证刷新、历史、计时、Token、checkpoint 和失败接力；认证过期提示必须是重新登录，不得提示升级版本。

## 5. P1：让 Presentation 成为受限 Agent 的通用提议

### 5.1 提议契约

- [ ] 扩展 `DistillationProposal` 或独立受限 Schema，至少包含 `resultAudience`、`consumer`、`terminalOrFragment`、
  `enabled`、`producer`、template/prompt、language、maxLength、reason、evidence、confidence。
- [ ] 输入只包含去实例化任务意图、能力语义、顶层/Fragment 位置、I/O Schema、数据流和调用者类型，不复制 Secret、
  页面正文或一次性值。
- [ ] Agent 判断是否直接面向人、是否为机器/组合中间结果、是否需要总结；静态代码只做 schema、安全、成本校验。
- [ ] Agent 不可用、证据不足或置信度低时，默认关闭或标记 `needs_human_decision`，不得按节点类型自动猜测。
- [ ] template 只能使用已验证字段；复杂解释走选定 LLM/Agent Runtime，并冻结 Prompt、预算和失败隔离策略。

### 5.2 审阅与发布

- [ ] 草稿分别保存 proposal 和 decision，不要在 proposal 生成时直接把它冒充最终 policy。
- [ ] 固化后在 Chat 显示提议摘要；编辑器显示 source/reason/evidence/confidence、预计模型调用和费用。
- [ ] 提供“接受提议”“修改后接受”“拒绝/关闭”三个明确动作，并持久化 reviewer decision、时间和 proposal digest。
- [ ] template、LLM prompt、language、maxLength 均可在可视化编辑器修改；JSON 视图只是高级降级入口。
- [ ] 发布门禁要求 decision 已完成；不可变 WorkflowVersion 冻结最终 policy 和审计 lineage，运行时不再猜测。

## 6. P1：真实跨领域闭环验收

- [ ] 简单信息查询样本：Agent 可提议模板或简短展示；天气可以使用，但策略必须经通用固化 Agent/审阅产生，seed 无模板。
- [ ] 网页/长文本总结样本：Agent 提议 LLM/Agent Presentation，第二次复用重新取数据再总结，不缓存首次正文。
- [ ] 文件转换或组合 Fragment：分别覆盖直接面向用户和机器中间结果，证明相同节点类型能因任务目的得到不同决策。
- [ ] 每类样本完成首次探索、固化审阅、发布、不同输入二次复用、可恢复失败接力与受控修订。
- [ ] 增加禁止领域分支检查，但不能用该静态检查替代真实跨领域行为验收。

## 7. P2：建立明确的 real-provider gate

- [ ] 保留 `_fake_traex` 作为快速 subprocess/MCP 协议回归，测试名、日志和 REPLAY 明确标记 fixture。
- [ ] 新增 opt-in `make traex-real-check`，使用实际 PATH 中 `traex`、平台非 Git artifact 目录和当前账号；不得替换成 fake。
- [ ] real gate 至少验证成功 probe、严格 JSON、一个 Host ToolResult、失败终态映射、Usage/budget 和取消。
- [ ] 没有真实登录或用户未授权费用时报告 skipped/blocked，不得把 fixture passed 写成 real passed。
- [ ] `make demo-check` 继续无外部费用可复现；发布报告同时列出 demo gate 与 real-provider gate，不能互相替代。

## 8. UI 与最终交付门禁

1. `make demo-check` 连续两次通过，后端、前端、迁移和 37 条独立进程 E2E 全部通过；
2. `make browser-ui-check` 在正常主机权限连续两次通过，明暗主题和 760–1440px 无遮挡、溢出或不可点击控件；
3. `make traex-real-check` 使用已刷新登录的真实 TraeX 通过，并记录版本、provider/model、预算和 Usage；
4. 启动全新 SQLite、API、Worker、Vite 和浏览器，实际完成工作流开启/关闭两条 Chat 路径；
5. 实际完成三个跨领域闭环，组件历史、RunDetail、Chat 历史、Presentation 决策和失败接力刷新后保持一致；
6. Settings 对认证、预算、协议和模型错误给出准确恢复动作；不得把认证/JSON/预算问题提示为升级版本；
7. 亮暗主题、工作流编辑器、proposal 审阅、保存、检查、发布、运行与复制均人工点击通过；
8. 文档链、`git diff --check` 和提交归档检查通过；REPLAY 区分 fixture、受限环境和真实 Provider 事实。

在“真实 TraeX 主流程、预算契约、Agent 驱动 Presentation、跨领域闭环”全部通过前，不得声明整体通过。
