# 设计与规范达成度审查报告（010）

审查日期：2026-08-05

依据：[`REPLAY-009.md`](../REPLAY/REPLAY-009.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)

代码基线：`cb93983`；009 修改仍在未提交工作区中。

范围：Local Demo；按约定不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

当前版本**不能完成设计文档要求的通配型“首次探索—固化—复用—失败接力—修订”闭环**，也不满足执行真实“对话总结→工作流固化→第二次复用”模拟的前置门禁。

009 的核心进展是真实的：TraeX 发出 `tool.requested` 后会等待 Host，Host 执行 Browser/local-read/lookup Adapter，再通过 stdin 返回真实 `tool.result`。这已经修复了 008 中“只改审计事件、Agent 仍消费 fixture”的根因。

但当前闭环仍在四处断开：

1. 后端因未声明的 `pypolaris` 依赖无法导入，正常测试在收集阶段终止。
2. 固化产物的 E2E 第二次运行通过人工创建 Component 并直接传 id，未经过“新对话→语义搜索→渐进披露→选择→执行”。
3. AgentRun 失败只创建 `TaskCheckpoint`，没有领取、恢复、用户确认或继续执行入口，所谓“接力”没有消费者。
4. Runtime 只捕获成功外部节点的 replay fixture；外部节点自身失败时没有失败证据，baseline 只能得到 `REPLAY_UNSUPPORTED`，无法对页面漂移、MCP 契约变化或网络故障完成修订判定。

因此本轮状态仍应标记为 **partial**，不能标记为 available 或 closed-loop。

## 2. 五阶段连续性判定

| 阶段 | 判定 | 已成立的事实 | 阻断闭环的事实 |
|---|---|---|---|
| 首次探索 | 部分实现 | [`agent_host.py`](../backend/app/application/agent_host.py) 已实现双向 Tool Broker；Agent 能收到 Host 的真实结果 | 当前后端不可导入；最终 provenance 只核对 call id 集合，不核对输出字段与 ToolResult 的实际依赖 |
| 固化 | 部分实现 | 成功 ConversationRun 可生成带 capability、lineage、Fragment 和部分 fallback 的草稿，仍需人工发布 | capability 语义直接取原始 prompt；网页示例的 prompt 主要是 URL，换 URL 后未证明能按“网页总结”语义发现 |
| 第二次复用 | 部分实现 | E2E-8b 的代码路径能在人工创建 Component 后换正文执行 Browser+Agent | 没有通过新对话和 Catalog 发现固化版本；`run_fragment/compose` 测试只验证 Agent 输出选择，没有验证公开主链跑到 Worker 终态 |
| 失败接力 | 未实现通用链路 | WorkflowRun 已有独立的 `ExecutionCheckpoint`/Recovery；AgentRun 会写 `TaskCheckpoint` | 全仓只有 TaskCheckpoint 建表和创建逻辑，无 claim/resume Driver/API；checkpoint 只保存脱敏摘要，缺少已完成 ToolResult 原值或 Artifact 引用 |
| 修订 | 部分实现 | 有受控 WorkflowPatch、ReplayEvidence 和 Revision Agent proposal | 真实外部失败节点无可回放失败 fixture；现有正例是纯 Transform，且没有“提议→应用→发布→再次复用”连续 E2E |

## 3. “对话总结→固化→第二次复用”专项分析

静态上，人工指定对象的最短路径已经基本存在：

```text
AgentRun + 真实 browser.read
  → Distillation Draft
  → 人工 publish
  → 人工 create Component
  → 直接 POST Component Run
  → Browser 读取新正文 + Agent 总结
```

这条路径由 [`test_demo_e2e.py`](../backend/e2e/test_demo_e2e.py) 的 E2E-8b 表达，但它不是用户要求的第二次“对话复用”：测试在发布后直接创建 Component，再持有 Component id 启动 Run，没有让 TraeX 看到固化能力的 L0 卡片、按需读取 L1/L2 并作出选择。

真正需要证明的主链应为：

```text
首次对话总结成功
  → 同源轨迹固化并发布
  → 新对话提出同类任务（输入已变化）
  → workflow.search L0
  → 按需 describe/describe_fragment L1/L2
  → TraeX 选择并补齐 bindings
  → Workflow Runtime 执行第二次任务
  → 输出引用固化版本与首次 lineage
```

当前不能证明该主链，原因包括：

- [`distillation.py`](../backend/app/application/distillation.py) 以清理后的原始 prompt 生成 summary/useWhen。E2E 网页 prompt 是 URL 加测试指令，未产生稳定的“网页读取并总结”语义描述。
- [`test_progressive_selection.py`](../backend/tests/test_progressive_selection.py) 的 Selection-5 参数化测试止于 `_select_workflow_candidate()`，不执行选择结果。
- `run_fragment` fixture 只做 L0→L1，未调用 `workflow.describe_fragment`；同时返回空 bindings。必填输入存在时，真实执行会在创建 PlanRun 时失败。
- `compose` 的 Agent fixture同样返回空 bindings；Compiler Adapter 还会先持久化一个未被后续执行使用的 PlanSnapshot。

## 4. REPLAY-009 声明核验

| 009 声明 | 审查结论 | 当前证据 |
|---|---|---|
| `make demo-check` 一次通过、后端 213 passed | 不成立 | 本次 `make demo-check` 在 Ruff 失败；正常 Pytest 有 11 个模块因 `ModuleNotFoundError: pypolaris` 无法收集 |
| 双向通用 Tool Broker | 基本成立 | stdin PIPE、Host 生成 ToolResult、Agent 自报 result 被拒绝，Browser/local-read/lookup 共用 Registry |
| Selection-5 都有真实终态 | 不成立 | 参数化测试只断言 Selection；fragment/compose 未由公开 Chat/API+Worker 跑到终态 |
| ask_user 恢复原选择 | run_full/fragment 基本成立 | PendingSelection 可补 bindings 且不重新搜索；但 resumeAction 只推断 full/fragment，不能保留 compose 计划 |
| 固化产物可发现 | 部分成立 | 有 Manifest 和搜索字段；没有“固化版本被新对话发现”的测试，网页语义又主要来自 URL |
| AgentRun handoff | 不成立 | 只有 checkpoint 生产者，没有消费者；UNKNOWN_OUTCOME 进入 `awaiting_user` 后也没有回答入口 |
| Replay 支持真实外部修订 | 不成立 | 只为成功外部节点采集 fixture；失败节点无法复现原错误 |
| 五阶段 lineage 已串联 | 不成立 | 各实体有部分 id/digest，但没有一条同源公开主链跨过五阶段；Manifest 也未暴露固化 lineage |
| Artifact 生命周期统一 | 基本成立 | Conversation trajectory 有 sourceKind/storageKey，下载、hash 校验和 reconcile 路径已补齐 |

## 5. 真实环境模拟决策

用户要求“分析能够完成时，再进行真实环境模拟”。本轮静态分析和启动门禁均判定**不能完成**，因此没有启动 API/Worker/TraeX 的全流程模拟，也没有用分段 fixture 冒充成功闭环。

阻断证据：

| 检查 | 本次结果 |
|---|---|
| `make demo-check` | 失败；Ruff 在 `workflow_catalog.py:1` 报 import block 未排序 |
| 正常后端 `uv run pytest -q` | 失败；11 个测试模块收集错误，均为 `ModuleNotFoundError: pypolaris` |
| 临时进程内 shim 诊断 | 210 passed；3 个 Playwright 用例因当前 macOS 沙箱禁止 Chromium Mach port 注册而无法启动。该 shim 未改代码，也不计为验收通过 |
| Alembic 空库 `0001→0015` | 通过；同一新库连续执行两次均成功 |
| 前端 Vitest | 28 passed |
| 前端 production build | 通过 |
| `git diff --check` | 通过 |
| 后端黑盒 E2E | 未运行；应用导入门禁已失败 |

`pypolaris` 不在 `pyproject.toml` 或 `uv.lock` 中；而 `operation` 在构造 JSON path 前已经通过固定白名单 `_SESSION_LIMITS` 校验，所以该依赖既未锁定也没有必要。

## 6. 文档一致性

[`docs/demo-acceptance.md`](../docs/demo-acceptance.md) 仍把 Real first browser 的必须事件写成 `browser.action.completed`，与当前双向协议的 `tool.requested/tool.result` 不一致；也没有一条“首次对话→固化发布→新对话语义发现→第二次复用”的连续验收场景。

文档当前描述的是多个分段 E2E，不足以防止再次把“原语存在”误报为“闭环完成”。010 后续工作必须先更新验收规范，再以同一 source id/digest 贯穿公开 API 和独立 Worker。

## 7. 010 完成定义

只有同时满足以下条件，才能将专项场景判定为完成：

1. 干净依赖环境中 `make demo-check` 一次通过，后端可导入和启动。
2. 首次总结的最终输出消费 Host 返回的随机真实 ToolResult。
3. 固化版本由第二个新对话语义搜索到，而不是测试持有 id 直接调用。
4. TraeX 的 `run_full/run_fragment/compose/ask_user/continue_agent` 均有公开入口到真实终态的证据。
5. AgentRun/WorkflowRun 的失败 checkpoint 都有可执行消费者；接力继续剩余步骤且不重放已完成副作用。
6. 外部失败证据可区分瞬态、可复现漂移和未知结果；只有可复现失败进入受控修订。
7. 修订 proposal 经应用、Replay、人工发布后，用原失败输入和新输入再次运行成功。
8. 网页与至少一个非网页场景各有一条连续黑盒，证明框架不是 Browser 特例。
