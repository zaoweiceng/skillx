# 设计与规范达成度审查报告（008）

审查日期：2026-08-04  
审查基线：[`REPLAY-007.md`](../REPLAY/REPLAY-007.md)、[`NEXT_STEPS-007.md`](../STEP/NEXT_STEPS-007.md)、[`docs/schema.md`](../docs/schema.md)、[`docs/skillx.md`](../docs/skillx.md)  
范围：Local Demo；鉴权、RBAC、多租户和分布式部署不在本轮范围内。

## 1. 结论

007 已经完成了此前不存在的关键底座：发布时生成 Workflow Capability Manifest、L0/L1/L2 API、显式和临时 Fragment、引用式 PlanCompiler、不可变 PlanSnapshot 数据源、人工固化建议，以及 Recovery/Replay 的第一轮状态机补强。完整门禁也全部通过。

但 `REPLAY-007` 所称“可检索、渐进披露、合法片段、引用组合、冻结执行、恢复与人工固化建议主链已完成”仍然高于代码事实。当前准确状态是：

- Registry、Compiler 和 Runtime 底座已形成，可由调用方手工串联 API 完成 L0→L1→Plan→Run。
- TraeX 在对话主路径只执行一次 L0 选择，没有获得 describe、fragment、slice、plan 工具，也不能自己完成局部提取或组合。
- Recovery、Fragment、Plan 和 Replay 仍存在会破坏版本锁、租约所有权或执行语义的 P1 缺口。

因此，用户要求的“Workflow 像 Skill 一样对 TraeX 渐进暴露，并能按需取局部、临时组合、稳定后固化”目前为 **partial，尚不能宣称完整实现**。

## 2. REPLAY-007 声明核验

| 007 声明 | 审查结论 | 证据边界 |
|---|---|---|
| 逐参数/output provenance fail closed | 基本完成 | 缺 provenance 会阻断发布；复杂分支仍被显式列为遗留 |
| Proposal grouping 和三种策略参与编译 | 部分完成 | grouping 生效，但未校验非连续分组的拓扑闭包，`variableCandidates` 未参与编译，动作组锁只保留最后动作引用 |
| Browser 固化走 Playwright，第二次交给非 fake AgentHost | 基本完成 | 第二次运行是真实 Playwright→AgentHost；首次探索的 Browser 结果仍由 TraeX fixture 直接宣告，并未真的访问页面 |
| Recovery 支持 heartbeat、崩溃重领、问题和 typed replan | 部分完成 | 分支均有正例，但迟到 Worker 应用决定时没有 lease fencing；replan 可替换任意剩余节点定义且未重做能力/MCP 裁决 |
| Patch 只认 baseline reproduction，patched 成功才建 draft | 部分完成 | 双阶段存在，但 Replay 不执行真实条件边语义、不排除 fallback-only 节点，也不校验最终输出 Schema |
| FailureObservation 绑定 checkpoint/recovery facts | 未完成 | 进入 Recovery 的失败提前 return，成功恢复后也没有补记 observation/recovered/checkpoint |
| 发布生成不可变 Manifest/Fragment | 基本完成 | 数据模型与同事务发布成立；目录同时保留所有旧版本为 `published`，检索会返回同一 Workflow 的多个旧候选 |
| TraeX 渐进披露 L0→L1→L2 | 未完成主路径 | TraeX 只看到 L0；L1 由服务端在选择后读取但不交回 Agent，L2/Plan 只可由外部 API 调用 |
| TraeX 可选择 full/fragment/compose/continue/ask | 未完成 | 对话路由只尊重 `run_full`；其它四种 action 均退回第一个完整 Workflow |
| Fragment 有确定边界和闭包 | 部分完成 | 显式片段检查直接依赖；`inputMapping` 保存但 PlanCompiler 不使用，临时 Slicer 只加入候选节点本身的 fallback target |
| PlanCompiler 冻结 refs 并复用 Runtime | 基本底座完成 | 两个简单 Workflow + Agent 正例通过；含 fallback 的 Workflow 展开会把 fallback target 当结果叶子，无法合法组合 |
| PlanSnapshot 有 TTL/status | 仅存储字段 | 创建 Run、固化和 Worker 加载均不检查 status/expiry |
| Plan Run 复用 Patch 路径 | 未完成 | Patch API 只接受真实 WorkflowVersion；Plan 失败记录为 `plan:<id>`，不能进入现有 version 路由 |
| 高频稳定计划只建议 draft | 基础正例完成 | 不会自动发布；手工调整次数只是同 signature 快照数量减一，尚不是实际编辑事实 |
| 产品页支持候选、组合预览和下钻 | 部分完成 | 有 L0 卡片、Manifest/Fragment 预览和 Plan Run 标识；没有对话内 describe/fragment/compose/plan preview 操作闭环 |

## 3. 目标场景能力判定

需求的最短正确链路应是：

```text
TraeX
  → workflow.search（L0）
  → 按需 workflow.describe / describe_fragment（L1/L2）
  → run_full / run_fragment / compose / ask_user / continue_agent
  → PlanCompiler 最终裁决
  → 冻结 PlanSnapshot + 同一 Runtime
  → 统计稳定复用，只建议人工固化
```

当前实际链路是：

```text
服务端 search L0
  → TraeX 一次性输出 Selection
  → 仅 run_full 被采纳
  → 服务端读取 L1 并返回传统 Component 卡片

外部调用方可另行手工调用 describe / slice / plans API
```

| 场景能力 | 状态 | 判定 |
|---|---|---|
| TraeX 知道 Workflow 名称、I/O 摘要和主要用途 | 已有 L0 卡片 | 满足基础发现 |
| 只在需要时读取完整契约 | API 已有，Agent 工具循环没有 | 部分满足 |
| 根据 L1 再决定 full/fragment/ask | 对话主路径不支持 | 不满足 |
| 选择并执行完整 Workflow | `run_full` 可转传统组件卡片 | 基本满足，但仅默认 Component 版本可直接启动 |
| 提取合法片段 | API/Compiler 有正例 | 部分满足，边界映射和 fallback 闭包不完整 |
| TraeX 组合多个引用与 Agent | 只能由测试/调用方手写 Plan | 不满足 Agent 自主组合 |
| 临时计划可恢复、可审计 | Plan Run 复用队列/NodeRun/Event | 基本满足；TTL/status 与 Patch 路径未闭环 |
| 高频计划建议固化 | 正例可用 | 基础满足 |

## 4. 与第一性原理的一致性

设计方向是正确的：TraeX 负责语义判断，Registry 负责有界发现，Compiler 负责确定性裁决，Runtime 负责可恢复执行。问题不在于缺少更多 API，而在于这四个职责还没有通过一个可恢复的 Agent 工具循环连接起来。

下一步不应继续增加平行入口。应先建立唯一 `WorkflowToolBroker` 会话状态机：Agent 每一步只能使用已披露 ref，工具响应升级披露层级，最终 Selection/ExecutionPlan 由 Compiler 接受或返回结构化修正信息。这样 full、fragment 和 compose 才是同一协议的不同结果，而不是三套手工 API 流程。

## 5. 门禁结果

| 门禁 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| 后端 Ruff | 通过 |
| 后端 Pytest | 194 passed |
| Alembic | 空库 0001→0013 通过 |
| TypeScript / ESLint | 通过 |
| Vitest | 28 passed |
| Vite production build | 通过 |
| 黑盒 E2E | 19 passed |
| `git diff --check` | 通过 |

通过的测试证明已编码正路径可运行，不覆盖以下关键反例：非 `run_full` Selection、迟到 Recovery Worker、replan 扩权、过期 Plan、含 fallback 的组合、Fragment 输入端口别名、条件 Replay、输出 Schema 失败和恢复后的 observation。

## 6. 008 完成定义

008 至少需要同时证明：

1. TraeX 在同一对话任务中按需调用 L0/L1/L2，并分别正确处理五种 Selection action。
2. `run_fragment` 和 `compose` 由 TraeX 生成引用式计划，Compiler 拒绝未披露 ref、错误 mapping、扩权和副作用绕过。
3. Recovery 决定带 claim generation/fencing token，旧 Worker 在重领后无法提交任何状态。
4. RecoveryPlanPatch 只能修改明确允许的映射/参数或受控节点补丁，并重跑 Capability/MCP/Browser/side-effect 校验。
5. Fragment 端口 mapping 真正参与展开；候选、祖先及其 fallback/approval 形成完整闭包。
6. PlanSnapshot TTL/status 控制新运行和目录可见性；已存在 Run 仍能使用其冻结快照恢复。
7. Replay 使用真实条件/onError/fallback 语义并校验最终 output Schema；Plan 与恢复失败也有可修订证据路径。

具体任务见 [`NEXT_STEPS-008.md`](../STEP/NEXT_STEPS-008.md)。
