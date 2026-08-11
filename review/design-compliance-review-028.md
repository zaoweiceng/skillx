# 设计与规范达成度审查报告（028）

审查日期：2026-08-11

依据：[`REPLAY-027.md`](../REPLAY/REPLAY-027.md)、
[`NEXT_STEPS-027.md`](../STEP/NEXT_STEPS-027.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`d6431c0`；审查对象为工作树中的 REPLAY-027 修复。本轮只新增 028 三份文档，不修改业务代码。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户或分布式部署。

## 1. 结论

**不通过。**

027 已正确关闭部分前置问题：真实 TraeX 可以在平台非 Git 隔离目录中越过目录检查；越界目录会 fail closed；天气
专用 Presentation 已从生产 seed 删除；dispatch 不再由错误类型伪造；MCP tool 名称增加稳定 digest 后缀并检测
碰撞；Settings 能区分候选 Runtime 与已验证可用 Runtime。完整工程门禁通过，确定性组件、历史、RunDetail、明暗
主题和工作流编辑器在真实前后端中均可正常使用。

但真实 Agent 主路径仍未跑通。Settings 主动探测确实到达模型并记录显式 dispatch，却产生 31,605 Token，而探测
Host 上限写死为 2,000 Token，最终以 `TOKEN_BUDGET_EXCEEDED` 失败。Provider 因此保持 unavailable，默认开启工作流
时不能进行语义选择。关闭工作流后的通用 Agent 也未返回严格 JSON，约 5.3 秒后被归类为
`AGENT_PROTOCOL_INCOMPATIBLE`。因此“无独立 LLM Runtime 时使用 TraeX Agent”仍只是候选路径，不是可用能力。

当前主机还存在独立认证事实：`traex login status` 显示 Logged in，但直接真实调用返回 access token 已于
2026-08-03 08:58:44 UTC 过期。应用主动探测又能产生模型 Usage，说明“凭据存在、认证成功、模型可调用、协议完成”
必须作为独立事实观测，不能再用一个 `probe_ok` 同时代表。

通用 Presentation 也只完成了“去天气特例”，没有完成“由 Agent 判断”。固化代码按节点类型和 Output Schema
静态推断：纯 transform/file/artifact/condition 一律视为机器中间结果；含 MCP/Browser/Agent 等则视为面向用户；
标量字段自动生成 `field={field}` 模板。节点类型不能回答结果的真实受众，例如面向用户的文件转换会被关闭，而组合
中的 MCP 中间结果可能被开启。API 虽返回 reason/evidence/confidence，前端固化草稿却不展示这些依据，也没有明确
接受/拒绝状态。因此还不能证明跨领域的“探索—固化—复用—失败接力—修订”闭环。

## 2. REPLAY-027 达成矩阵

| 目标 | 结果 | 证据 |
|---|---|---|
| 受信非 Git 工作目录启动 TraeX | 通过 | 真实命令加入 `--skip-git-repo-check` 后越过目录阻断并到达服务端 |
| 越界/symlink 工作目录 fail closed | 通过（代码/门禁） | `_trusted_working_directory()` 校验 artifact root，返回独立错误码 |
| 真实 Provider 主动探测 | **未通过** | dispatch=true，但 31,605 Token 超过 2,000 上限，`TOKEN_BUDGET_EXCEEDED` |
| 默认开启工作流后由 Agent 选择/绑定 | **未通过** | Provider unavailable，Chat 显示“自动选择不可用” |
| 关闭工作流后通用 Agent 完成回答 | **未通过** | 实际 Worker 调用后无严格 JSON，`AGENT_PROTOCOL_INCOMPATIBLE` |
| 认证、协议、可用性独立投影 | **部分通过** | 字段已拆出，但 authenticated/protocolCompatible 都直接等于整体 `probe_ok` |
| dispatch 审计不再由错误码推断 | 通过 | dispatch 前环境失败为 false；真实模型事件才置 true |
| MCP tool 名称碰撞 fail closed | 通过 | digest 后缀、反向映射和碰撞守卫已实现 |
| 天气不进入通用 Presentation 特判 | 通过 | seed v1 真实运行只有 JSON；通用运行时无天气分支 |
| Presentation 由 Agent 按任务目的判断 | **未通过** | 当前是节点类型/Schema 静态启发式，不是 Agent 提议 |
| Presentation 提议可审阅后冻结 | **未通过** | API 有 proposal；编辑器不显示 reason/evidence/confidence，也无接受状态 |
| 跨领域真实固化/二次复用 | **未通过** | Agent 首次探索失败；三类自动化是函数/fixture 证据，不是真实闭环 |
| 组件执行、调用历史、RunDetail | 通过 | 问候与天气均由真实 Worker 成功执行，历史和详情可回看 |
| 亮暗主题、配色、布局和点击 | 通过 | 手工浏览器验收正常；正常主机权限下 UI 门禁 2/2 |
| 总工程门禁 | 通过 | 318 backend、51 frontend、37 独立进程 E2E 全部通过 |

## 3. 真实环境验收

### 3.1 自动化与独立进程

| 检查 | 结果 |
|---|---|
| 审查前文档链 | 通过；103 个版本化 Markdown |
| 后端 Ruff / Pytest | 通过；318 passed in 36.82s |
| Alembic 空库升级 | 通过；`0001 → 0023` |
| 前端 check / lint / Vitest / build | 通过；11 files / 51 passed，生产构建成功 |
| 独立 API/Worker/Vite/Chromium E2E | 通过；37 passed in 154.06s |
| `make browser-ui-check`（正常主机权限） | 通过；2 passed in 25.02s |

UI 门禁在受限命令沙箱中首次因 macOS Chromium Mach port 权限失败；相同代码在正常主机权限下立即 2/2 通过，
属于验收运行环境限制，不计为产品缺陷。

### 3.2 真实 TraeX

- 本机版本为 `traecli 0.200.19(internal edition)`；`login status` 返回 Logged in。
- 在非 Git `/tmp` 目录直接使用 `--skip-git-repo-check`，目录校验问题已消失；真实服务端随后明确返回 token expired。
- 启动全新 migration 0023 SQLite、真实 API、独立 Worker 和 Vite 后，Settings 探测使用内置
  `traex-cli-jsonl+mcp`，得到 `requestDispatched=true`、input 31,314、output 291、total 31,605，随后因 Host
  2,000 Token 上限失败，Usage incomplete。
- 默认开启工作流时自动选择被正确禁用。关闭工作流发送“请只回复 028-agent-host-ok”，真实 Worker 产生
  `model.request.dispatched`，但没有严格 JSON 终态，以 `AGENT_PROTOCOL_INCOMPATIBLE` 结束，Token 未落为完整 Usage。
- 第一次在受限 Worker 中执行所产生的状态目录权限失败，不作为产品结论；在正常主机权限 Worker 中复测后得到上述
  真实协议失败。

### 3.3 真实前后端与 UI

使用全新数据库分别启动 API、Worker、Vite，并在浏览器中实际操作：

- 亮色组件目录留白、层级、边框和文字对比清晰；暗色背景、卡片、强调色和焦点框协调，无白块或明显溢出。
- Settings 准确显示候选 Agent Runtime、Provider unavailable、Worker available 和探测 Token；探测冷却期间按钮禁用。
- Chat 新建对话默认工作流开启，开关可切换；历史对话、5.3 秒耗时、Token 不可用、失败 checkpoint 和刷新状态可见。
- “确定性问候”输入 `028验收` 成功，真实 Worker 执行 3 个节点，JSON 为
  `{"message":"你好，028验收"}`；返回组件页可见 1 条历史、424ms、输入/输出和 RunDetail 链接。
- 天气发布版本输入上海成功，MCP、Artifact、seq 1..14 和 JSON 完整，Presentation 未启用、0 Token。这证明天气只
  是普通测试样本，生产 seed 没有自然语言特例。
- 工作流编辑器的 JSON、保存、静态检查、发布、Presentation checkbox、template/LLM producer 和手动运行入口均
  可点击；暗色全页布局正常。选择 LLM producer 后没有 prompt/language/maxLength 可视化编辑项。
- 固化草稿页面没有展示 `presentationPolicyProposal` 的 reason/evidence/confidence；只能直接编辑已写入的最终策略。

## 4. 预期闭环能否完成

目前确定性执行、持久历史、失败 checkpoint、能力目录、编辑器和 Presentation Runtime 已能独立工作，但闭环仍断在
两个关键决策点：

1. 真实 TraeX 的最小上下文/Token 契约未校准，主动探测无法把候选 Runtime 提升为 usable；
2. Presentation 的“是否面向用户”没有从任务目的获得可信语义，只由节点技术类型代替。

最短正确路径不是提高一个常量或增加更多领域模板。Agent bridge 应先识别真实 JSONL 失败终态、减少不可避免的上下文，
再把 input/context、output 和总成本预算分开；固化 Agent 则应从用户目的、工作流作为顶层/Fragment 的调用位置、消费者
类型和输出契约提出 Presentation，静态规则只做校验与保守 fallback。天气、网页总结、文件转换只是同一通用机制的
不同验收样本，任何一个都不得成为平台分支。

## 5. 最终判定

028 的工程门禁、确定性 Runtime、历史和 UI 通过；真实 Agent 语义主流程、通用 Presentation 判断与跨领域闭环未
通过。因此 028 **不通过**。修复范围和真实通过条件见
[`NEXT_STEPS-028.md`](../STEP/NEXT_STEPS-028.md)。
