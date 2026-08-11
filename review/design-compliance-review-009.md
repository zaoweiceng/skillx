# 设计与规范达成度审查报告（009）

审查日期：2026-08-04

审查基线：[`REPLAY-008.md`](../REPLAY/REPLAY-008.md)、[`NEXT_STEPS-008.md`](../STEP/NEXT_STEPS-008.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)、[`docs/skillx.md`](../docs/skillx.md)

代码基线：`cb93983`（相对 `95f3fbb`）

范围：Local Demo；鉴权、RBAC、多租户和分布式部署不在本轮范围内。

## 1. 结论

当前版本**没有完整实现**通配型“首次探索—固化—复用—失败接力—修订”闭环。

008 确实补齐了多块重要底座：持久 Disclosure Session、Fragment Port、不可变
PlanSnapshot、Recovery fencing、受控 replan、Runtime/Replay 共享分支逻辑、Workflow/Plan
两类失败源，以及真实 Playwright 读取适配器。但这些能力仍是数段可单独调用的子系统，
没有形成同一任务、同一执行来源、同一证据链上的连续状态转换。

准确判定如下：

| 阶段 | 状态 | 关键判定 |
|---|---|---|
| 首次探索 | 部分实现 | AgentRun 可持久化轨迹；Host 会重写 Browser 完成事件，但真实工具结果没有回送给 TraeX，TraeX 仍按 fixture 继续推理 |
| 固化 | 部分实现 | 可从成功轨迹生成并人工发布草稿；产物缺少可发现语义、Fragment 声明和失败接力策略 |
| 复用 | 部分实现 | 人工创建 Component 后可换输入复用；TraeX 不能在正常对话中自主完成 L0→L1/L2→fragment/compose |
| 失败接力 | 条件实现 | 只有预先声明 fallback 的 Workflow 可进入 Checkpoint/Recovery；普通 AgentRun 和默认固化产物不能进入该路径 |
| 修订 | 部分实现 | 有受控 Patch 与 Replay API；没有 TraeX 生成 Patch 的链路，真实外部节点运行也没有捕获 Replay fixture |

因此当前能力应标记为 **partial**，不能标记为 available/closed-loop。更直接地说：
“组件能分别演示”已经成立，“任意一次探索产物能自然走完整五阶段”尚未成立。

## 2. REPLAY-008 声明核验

| 008 声明 | 审查结论 | 代码事实 |
|---|---|---|
| Registry、Fragment、PlanCompiler、Runtime、Recovery、Replay 已接成同一执行链 | 部分成立 | Plan Run 可复用 Runtime；但探索、目录选择、Recovery 和 Patch 没有同一 lineage 的编排器 |
| AgentHost 通过 WorkflowToolBroker 获得渐进工具 | 未成立 | 当前只有 REST 工具端点；对话入口由服务端先 search，TraeX 仅执行一次 L0 选择 |
| 五种 Selection 都有真实终态 | API 分支存在，Agent 主路径未成立 | `execute-selection` 有五分支；Chat 明确拒绝 `run_fragment/compose`，测试只验证 Pydantic 判别联合 |
| ask_user 回答后恢复同一会话 | 仅恢复 session id | 下一条消息用“回答文本”重新搜索，没有恢复原任务、候选、缺失绑定和选择状态 |
| 首次 Browser 结果来自真实 Tool Broker | 证据不充分且语义不成立 | Host 替换审计事件中的 Browser result；TraeX 子进程 stdin 为 `DEVNULL`，后续总结仍消费 fixture“首次探索正文” |
| Distillation 产物可进入目录复用 | 手工 Component 复用成立，语义发现不成立 | 固化 metadata 只有通用名 `conversation-agent-draft`，没有 capability summary/useWhen/tags |
| FailureObservation 支持 Workflow/Plan 并可修订 | 数据模型成立，通用修订未闭合 | Patch 需要外部提交 DSL；外部节点 Replay 依赖 `_replayFixtures`，真实 Run 没有捕获或保存它 |
| Conversation trajectory 复用统一 Artifact 生命周期 | 未成立 | 新 Artifact 没有 `storageKey`，统一下载端点必定返回 410，现有 GC/下载路径仍以 `run_id` 为目录 |
| `make demo-check` 全部通过 | 与当前提交不一致 | 当前首先因 Ruff 失败；绕过 Ruff 后后端测试因未声明 `pypolaris` 在收集阶段失败 |

## 3. 五阶段连续性分析

### 3.1 首次探索：真实执行结果没有进入 Agent 的下一步

当前 TraeX 适配器是单向 NDJSON 事件流：子进程 stdin 被关闭。`AgentHost` 收到
`browser.action.completed` 后才调用 Playwright，并把事件中的 `result` 替换为真实结果。
这个替换能改善轨迹证据，但无法让仍在运行的 TraeX 使用真实正文决定下一步。

E2E-8b 中 TraeX 随后发出的总结工具参数、总结结果和最终回答仍是 fixture 常量
“首次探索正文”；测试只断言 Browser action 的持久证据来自页面，没有断言首次 Agent
回答由真实页面生成。

要满足“首次探索”，协议必须是双向的：Agent 发出 tool request 后暂停，Host 执行真实
工具并回送 result，Agent 再继续下一步。该协议还必须适用于 MCP、文件、API、Browser 等
通配 action，不能只为网页特判完成事件。

### 3.2 固化：得到可执行图，但没有得到可发现、可恢复的能力

`compile_agent_trajectory()` 能保留动作顺序、数据依赖和确定/概率节点，这是有效实现。
但生成的 Workflow：

- metadata 使用固定通用名称，没有从任务提炼 capability summary/useWhen/tags/examples；
- 不生成 callable Fragment，也没有可供 Slicer 使用的语义节点摘要；
- 不生成 fallback/recovery policy；
- 不把来源 ConversationRun/Distillation id 写入发布后可追踪的 lineage。

结果是固化图可被人工拿到 id 后发布、建 Component、直接运行，却不能可靠地被未来 TraeX
按语义发现，也不能在节点失败时自动进入接力。

### 3.3 复用：外部 API 可以串联，TraeX 自主选择不成立

正常 Chat 路径实际是：

```text
服务端 search L0
  → TraeX 单步选择（maxSteps=1，无工具）
  → run_full 可继续
  → run_fragment / compose 直接 422
```

L1/L2、slice、plan 和 execute-selection 只能由外部调用方另行串 REST API。它们证明了
平台原语可用，不证明 TraeX 能像使用 Skill 一样渐进发现、按需取片段和临时组合。

### 3.4 失败接力：专用 Workflow 可恢复，固化产物默认不可恢复

Checkpoint、Recovery Agent、OutputSubstitution、typed replan 和 fencing 已形成较完整的
Workflow Runtime 恢复机制。但入口依赖失败节点预先声明 fallback。Distillation 不生成
fallback，因此由首次探索固化的 Workflow 默认在重试耗尽后直接失败。

首次探索本身使用 `ConversationRun`。该状态机在 Worker 丢失时写
`UNKNOWN_OUTCOME` 终态，不创建 ExecutionCheckpoint，也没有把已完成动作、剩余动作和
任务上下文交给新的 TraeX 接力。故障接力目前不是“通配 Agent 运行”的共同能力。

### 3.5 修订：有安全执行器，没有修订决策者和真实外部证据

Workflow/Plan Patch API 能限制可修改字段，并在 Replay 通过后生成新草稿/快照，这是正确
方向。但闭环还缺三件事：

1. 没有服务把 FailureObservation、Checkpoint、恢复结果和失败上下文交给 TraeX 生成
   受控 Patch proposal；当前 Patch 完全由客户端自带。
2. Replay 对 Browser/MCP/Agent 等外部节点只接受 `_replayFixtures`；真实 Run 没有把节点
   输出按 digest/Schema 捕获到 ReplayEvidence，且正常输入 Schema 通常不允许调用者塞入
   `_replayFixtures`。
3. Patch 产物需要人工另行发布、重新建入口和再次运行，没有“修订版本→同任务再复用”的
   lineage 与门禁。

## 4. 通配性判定

代码已经建立统一 `NormalizedAction → ActionCompilerRegistry → Workflow` 的方向，网页和
非网页可以进入同一 IR，这是通配性的有效基础。但实际 Tool Broker 只实现
`browser.read` 的 Host 侧事件替换；其它工具仍由 TraeX 自报完成事件和结果。

所以当前通配性只存在于**轨迹编译层**，尚未存在于**真实首次执行、失败接力和修订层**。
下一步必须定义统一 ToolRequest/ToolResult、Checkpoint 和 ReplayFixture 协议，让适配器按
能力注册，而不是继续给 Browser 增加专用分支。

## 5. 门禁结果

| 门禁 | 本轮结果 |
|---|---|
| `make demo-check` | **失败**；Ruff 在 `replay.py` 报 3 项错误 |
| 后端 Pytest | **失败**；8 个测试模块收集时报 `ModuleNotFoundError: pypolaris` |
| 后端 E2E | 未运行；后端导入失败，不能启动黑盒栈 |
| Alembic 空库 0001→0014 | 通过 |
| TypeScript / ESLint | 通过 |
| Vitest | 28 passed |
| Vite production build | 通过 |
| `git diff --check` | 通过 |

门禁失败不是环境能力不足：`pypolaris` 不在 `pyproject.toml/uv.lock` 中，而且导入本身未被
使用。`REPLAY-008` 中“202 后端、19 E2E 全部通过”的记录不能作为当前 `cb93983` 的可复现
证据。

## 6. 009 后续完成定义

只有同时满足以下条件，五阶段闭环才能提升为完整实现：

1. 同一 TraeX run 通过双向 Broker 使用真实工具结果继续推理，Browser 与非 Browser 各有
   黑盒证据。
2. TraeX 在同一 Disclosure Session 内自主完成 L0→按需 L1/L2→五种 Selection；非 full
   action 不需要外部手工串 API。
3. 固化产物带可检索 capability、来源 lineage、明确的可恢复策略和可选 Fragment 描述。
4. 固化产物换输入复用后发生可注入故障，能从持久 checkpoint 交给 TraeX 接力，且不重放
   已完成副作用。
5. 真实运行自动捕获有界 Replay fixture；可复现故障触发受控 Patch proposal，瞬态故障不
   触发修订。
6. Patch 经 baseline/patched Replay、人工发布后，原任务可自动选择新版本再运行，并保留
   `exploration → distillation → version → run → observation → recovery → patch → version`
   完整 lineage。
7. 上述过程至少由一条网页场景和一条非网页场景端到端证明，且 `make demo-check` 全绿。

具体实施任务见 [`NEXT_STEPS-009.md`](../STEP/NEXT_STEPS-009.md)。
