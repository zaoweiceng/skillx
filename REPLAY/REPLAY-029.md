# REPLAY-029：真实终态契约、窄目录披露与 Presentation 语义闭环

日期：2026-08-12

依据：[`NEXT_STEPS-029.md`](../STEP/NEXT_STEPS-029.md)、
[`code-review-029.md`](../review/code-review-029.md) 和
[`design-compliance-review-029.md`](../review/design-compliance-review-029.md)。

## 1. 结论

029 指出的代码根因已按最小范围修复，fixture/工程门禁与 Browser UI 通过；真实外部 TraeX probe、
真实产品 Chat 主路径和三个真实跨领域闭环本轮未获外部调用授权，因此状态是 **代码门禁通过，真实产品验收
blocked**，不声明 NEXT_STEPS-029 整体通过。

本轮没有增加天气、网页、文件或问候的生产分支。问候只作为通用目录协议的测试数据。

## 2. WorkflowSelection：从五个描述符改为一个服务端状态机

选择入口只授予 `workflow.catalog`。其固定窄 Schema 以 `action` 枚举承载 search、describe、
describe_fragment、slice、compile_plan；新 disclosure session 的第一次调用由服务端强制为 search。
describe/slice/compile 继续复用既有 `require_disclosed_ref` / `require_plan_refs_disclosed`，模型不能自报 ref、
Manifest、DAG 或计划绕过 session。

同一消息的 binding、bindingSources、quote、messageId、confidence 与服务端重建校验未改变。timeline 对外仍投影
原来的 `workflow.search/describe/...` 语义和 L0/L1/L2，审计兼容而首轮不再注入五份 Tool descriptor。

Local Demo 的默认冻结上限统一提高到 input 1,000,000、output 250,000、billable 1,250,000；它们是长任务
故障护栏，不是计费策略。WorkflowSelection 与 probe 在 Settings 分开显示 profile；选择仍只有一个目录描述符。

## 3. 唯一严格 Agent 终态

`AgentHost` 新增唯一 `build_strict_terminal_prompt()`：

1. 业务/用户 Prompt 先编码为 JSON string 数据；
2. Host 在数据外声明最终输出必须是唯一严格 JSON object；
3. 明确附带 canonical output Schema，并禁止 fence、前后说明和注释；
4. adapter 仍严格执行 JSON parse 与 JSON Schema 校验，不包装普通文本、不隐式修复、不切换 Runtime。

通用 `{reply:string}`、WorkflowSelection、Capability、Distillation、Presentation、Revision、Recovery 与 Workflow
Agent node 都经过同一 Host 边界。fixture 会从 Host data packet 还原测试任务，但不绕过生产终态校验。

## 4. Presentation 与轨迹事实

成功 ConversationRun 的 trajectory 现在记录 `resultConsumer`、`entrypointKind`、`topLevelReply` 和
`intendedAsFragment`；缺失事实保持 `unknown`。Presentation 输入只投影去实例化 intent、consumer、entrypoint、
I/O Schema 与 action kind 数据流，不加入实例 URL、正文、Secret 或一次性参数。

固化顺序调整为：先插入受控 recovery 节点与 fallback，再计算 callable Fragment 闭包，再请求 Presentation。
第一次全量 E2E 曾发现 recovery 在 Fragment 之后插入会形成隐藏依赖；修复顺序后两个真实 Browser/失败接力
黑盒用例单独复跑 2/2，通过发布校验，未放宽 Fragment 闭包规则。

编辑器的“接受提议”只应用规范化 proposal；任何表单变更重置为 pending 并禁用该按钮，只能“修改后接受”。
服务端同时拒绝 accepted 与 proposal 不一致、modified 实际未修改，以及 rejected 仍启用的 policy。proposal、policy
与 decision digest 的既有绑定保持不变。

## 5. 前端一致性

- Settings 同时读取 credentialPresent 与新鲜的 activeProbeAuthenticated；主动认证有效时显示验证成功并禁止重复探测，过期后才恢复提示。
- Settings 分开显示 probe profile 与 WorkflowSelection profile。
- WorkflowGraph / EditableCanvas 统一把 null 与 undefined 条件转为无标签，false 等真实条件仍可见。
- Chat 与 RunDetail 的复制 JSON/文本操作显示成功或失败结果。
- Presentation 的接受/修改语义由前端交互测试和后端契约测试共同覆盖。

## 6. 真实 Provider 产品门禁

保留 `make traex-real-check` 快速 probe，新增 `make traex-real-product-check`。新门禁只接受 PATH 中实际
`traex`，用全新 SQLite、Alembic、独立 API/Worker，覆盖：

- workflow-on：自然语言问候 → 目录发现 → binding → 确定性 Run；
- workflow-off：严格 `{reply}`，并断言事件中零 `workflow.*`；
- running cancellation：确认进入 running 后取消并等待 cancelled 终态。

未设置显式授权变量或 PATH 缺少 traex 时只报告 blocked/skipped，不以 fixture 冒充。原生 failed、malformed、
timeout 的确定性映射继续由 331 个后端测试覆盖；由于真实外部模型无法可靠被测试指令强制产生这些故障，本轮没有
伪造“真实失败已验证”的证据。

## 7. 验证记录

| 检查 | 结果 |
|---|---|
| 针对性 backend | 90 passed，2 个真实产品用例按未授权 skipped；另有 61 passed 的固化/契约回归 |
| Progressive selection | 12 passed；五种 action 与 L0/L1/L2 timeline 保持兼容 |
| 前端 check / lint / test / build | 通过；11 files / 53 tests，生产构建成功 |
| 首次 `make demo-check` | E2E 暴露 Fragment 隐藏 recovery 依赖，2 failed；发布校验按设计阻断 |
| 隐藏依赖修复后针对性 Browser E2E | 2 passed |
| 修复后 `make demo-check` 连续两次 | 两次均通过；每次 331 backend、53 frontend、37 独立进程 E2E，迁移 0001→0023 |
| `make browser-ui-check` 连续两次 | 2 passed + 2 passed |
| `git diff --check` | 生成本文前通过；交付前复查 |
| `make traex-real-check` | 未执行；使用本机登录态向真实外部模型发送探测的权限请求在进程启动前被拒绝 |
| `make traex-real-product-check` | 未执行；同一外部调用授权缺失，测试默认明确 skipped/blocked |

## 8. 未完成的真实事实

以下只能在明确授权真实外部 TraeX 调用后完成，fixture、单元测试和 Browser UI 都不能替代：

1. 无工具、单目录工具和各目录 action 的真实 Schema bytes、固定上下文、Usage 与时延测量；
2. workflow-on / workflow-off 的真实 Chat 成功证据和真实 running cancellation；
3. 信息查询、动态网页/长文本、文件/组合 Fragment 三个完整探索—固化—发布—自然语言复用闭环；
4. 真实瞬态失败接力、只执行剩余节点，以及契约漂移后的 RevisionProposal/ReplayEvidence；
5. 真实原生 failed、malformed output 与 timeout 的产品级映射证据。

这些完成前，029 的整体结论保持 blocked，而不是 passed。
