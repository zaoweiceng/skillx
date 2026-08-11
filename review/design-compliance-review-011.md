# 设计与规范达成度审查报告（011）

审查日期：2026-08-05

依据：[`REPLAY-010.md`](../REPLAY/REPLAY-010.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)

代码基线：`24cca38`；REPLAY-010 对应修改仍在未提交工作区中。

范围：Local Demo；按约定不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

当前版本已经能够完成用户点名的真实主场景：

```text
首次 Conversation AgentRun 真实读取网页并总结
  → 从成功轨迹生成工作流草稿
  → 人工发布
  → 第二个新 Conversation 以自然语言提出同类任务
  → Catalog L0/L1 渐进发现
  → Workflow Runtime 使用新 URL/新正文执行
  → 输出关联首次 ConversationRun lineage
```

本轮在真实 API、独立 Worker、临时迁移数据库、TraeX stub、动态内容服务和真实
Playwright 下专项执行该路径，结果为 **1 passed in 6.30s**。因此“对话总结工作流固化后
第二次复用”在 Local Demo 范围内可判定为 **available**，可以进行真实环境模拟，不再是
分段原语拼接。

但 REPLAY-010 将更大的通配型“首次探索—固化—复用—失败接力—修订”整体标记为
`closed-loop`，该结论仍然过早。修订路径创建的是新的逻辑 Workflow，而不是原 Workflow
的新版本；TaskCheckpoint 的 claim token 没有约束 continuation 创建；“不重做已完成步骤”
只依赖提示词；外部失败的连续验收又绕过了公开 API、独立 Worker 和 Revision Agent。

所以 011 的总体判定是：

- **探索—固化—新对话发现—第二次复用：完成。**
- **渐进披露、Selection-5、输出 provenance：实现且门禁通过。**
- **通用失败接力：原语可运行，但并发与“不重做”不变量未闭合。**
- **外部失败—修订—新版本优先复用：部分实现，未满足连续验收。**
- **整个通配型闭环：partial，不应继续标记为 closed-loop。**

## 2. REPLAY-010 声明核验

| 010 声明 | 011 结论 | 证据或缺口 |
|---|---|---|
| 可运行基线恢复 | 成立 | `make demo-check` 一次通过，后端 217、前端 28、E2E 19 |
| CapabilityDescriptor 与具体 URL 解耦 | 基本成立 | 结构化描述受 Schema 校验并拒绝 URL/Secret/测试指令；主链换 query 与正文后仍被发现 |
| 第二次新 Conversation 语义发现并执行 | 成立 | E2E-8b 经公开 Conversation API、L0/L1、Runtime 到 `succeeded`，不创建 Component 持 id 直调 |
| L1 暴露首次 lineage、L0 不扩张 | 成立 | L1 包含 source ConversationRun/trajectory/distillation；L0 卡片不含 lineage/Schema/DAG |
| Selection-5 已进入真实执行 | 基本成立 | 服务测试覆盖五分支及 Worker 终态；公开黑盒仍未逐项覆盖五分支 |
| AgentRun checkpoint 已完整闭合 | 部分成立 | Driver、Artifact、回答 API 和 continuation 已存在；claim token 未约束启动，且禁止重做只由 prompt 表达 |
| 网络瞬态不进入修订 | 不成立 | eligibility 不检查 `REPRODUCIBLE_THRESHOLD`；冻结 failure fixture 可以把一次外部网络错误“重放”为同错误 |
| 外部失败可驱动 Revision Agent→Patch→发布→复用 | 不成立 | Browser 正例手写 Patch 并直接调用 service 函数；Revision Agent 是另一条 Transform 单测，未形成同源连续链 |
| 修订发布后旧 ref 仅 resolvable、搜索只返回 active | 不成立 | Patch 创建新 `Workflow`，发布时只降级同一 `workflow.id` 的旧 Manifest；原失败 Workflow 仍为 published |
| 通配型闭环 closed-loop | 不成立 | 主复用链完成，但接力与修订链仍违反验收不变量 |

## 3. 分阶段达成度

| 阶段 | 判定 | 已成立的事实 | 剩余阻断 |
|---|---|---|---|
| 首次探索 | 完成 | TraeX `tool.requested` 由 Host 真实执行；Browser 结果、evidence digest 和逐字段 provenance 均被校验 | 本轮无主链阻断 |
| 固化 | 完成 | 成功轨迹经受限 Distillation Agent 生成结构化能力描述和可发布草稿；仍由人工发布 | 第二 URL 只改变 query，跨 path/domain 泛化仍是覆盖边界 |
| 渐进发现与复用 | 完成 | 第二个新 Conversation 经 L0/L1 选择锁定版本并由 Runtime 执行新输入 | 搜索目前是轻量词法打分，适合 Demo，不代表生产语义检索质量 |
| 失败接力 | 部分实现 | Worker 已消费 TaskCheckpoint；只读 ToolResult Artifact 可恢复；UNKNOWN_OUTCOME 可回答 | continuation 创建缺 token/generation CAS；平台不强制禁止已完成 capability 再调用；没有公开黑盒接力场景 |
| 失败修订 | 部分实现 | 外部失败 fixture、digest 校验、baseline/patched replay、受控 Patch 与人工 publish 原语存在 | 瞬态复现判定不可信；Browser 连续链没有 Revision Agent；修订草稿不是同一 Workflow 的下一版本 |
| 修订后复用 | 未达标 | 手工 Patch 后原输入和新输入能执行成功 | 旧失败 Manifest 仍可占搜索结果；没有公开 API 的 E2E-9b 证明 active/resolvable 和完整 lineage |

## 4. 真实环境模拟结果

### 4.1 环境

专项命令：

```bash
cd backend
.venv/bin/pytest \
  e2e/test_demo_e2e.py::test_e2e_8b_real_browser_distillation_to_non_fake_agent \
  -q -p no:cacheprovider
```

Harness 为本次运行创建临时目录和 SQLite 数据库，只通过 Alembic 建库，并启动独立的：

- FastAPI/Uvicorn API 子进程；
- Workflow、AgentRun、Recovery、TaskRecovery 共用的 Worker 子进程；
- 动态文章服务；
- TraeX-compatible 子进程；
- Playwright Browser Runtime。

### 4.2 对话与状态序列

| 步骤 | 模拟输入/动作 | 实际断言 |
|---|---|---|
| 1 | Conversation A：首次访问文章并总结 | AgentRun `succeeded`；出现 `tool.requested/tool.result` |
| 2 | 动态页正文为“探索阶段正文” | 最终输出精确为该正文；Browser evidence 来源为 `host_tool_broker` |
| 3 | 对首次 ConversationRun 发起 distill | proposal 来自 Distillation Agent；trajectory digest 与 publishable 均有效 |
| 4 | 人工发布草稿 | 创建锁定 WorkflowVersion 和 Manifest |
| 5 | 页面改为“第二次运行的新正文：利润增长 21%。”，URL 增加 `?version=b` | 新输入没有复用首次正文 |
| 6 | Conversation B：自然语言“访问并总结这个网页” | 返回 `agentAction=run_full`；选择的 version 等于刚发布版本 |
| 7 | 检查渐进披露 | timeline 恰含 L0/L1；L1 lineage 指向 Conversation A 的 run id |
| 8 | 独立 Worker 执行固化工作流 | Run `succeeded`；输出包含“利润增长 21%”；Browser 与 Agent 节点均真实执行 |

结果：`1 passed in 6.30s`。

该模拟证明的是“输入实例变化后，已固化结构由新对话发现并复用”；它不证明跨域授权、生产
搜索质量，也不替代失败接力和修订链的专项黑盒。

## 5. 设计与验收冲突

### 5.1 修订没有形成同一逻辑工作流的新版本

设计要求已发布版本不可变、修改产生新版本；验收要求发布 v2 后 v1 仍可精确解析但不再进入
搜索。当前 `apply_version_workflow_patch()` 新建一个 `Workflow(id=uuid, draft_revision=1)`；
随后 publish 只在这个新 id 下创建 v1。`build_manifest()` 又只会把同一 `workflow.id` 的旧
Manifest 降为 `resolvable`。因此原失败工作流仍是另一个 active/published 能力。

这不是显示问题，而是逻辑身份错误：系统无法知道两个 Workflow 哪一个是同一能力的当前版本。

### 5.2 外部失败的“可复现”是证据重放，不一定是现实复现

冻结成功输出用于验证契约变化是合理的；冻结一次 `DEPENDENCY_UNAVAILABLE/TIMEOUT` 再让
Replay 返回同一错误，只能证明 Artifact 中记录了该错误，不能证明外部依赖现在仍失败。
当前 eligibility 又没有使用已定义的 occurrence 阈值，所以一次瞬态错误可能被分类为
`platform_bug` 并调用 Revision Agent。

### 5.3 TaskCheckpoint 的状态机存在未封闭的并发窗口

claim 的 UPDATE 会递增 generation 并生成 token，但 `claim()` 只返回 checkpoint id；
`start_continuation()` 只比较 status 和 `lease_owner`。若同名 Worker 在租约后重领，迟到旧进程
仍可能创建第二个 continuation。`_reconcile_continuation()` 也没有 CAS，不能保证终态事件唯一。

### 5.4 验收层级不一致

`docs/demo-acceptance.md` 要求每个场景通过真实 API、独立 Worker 和持久数据库。当前：

- AgentRun handoff 测试直接调用 `_enqueue/_drive/TaskCheckpointDriver`；
- 外部 Browser 修订测试直接调用 Orchestrator、WorkerService、Patch route 函数和 publish 函数；
- 公共 E2E-9 使用 Transform，且只到创建 draft，没有 publish/reuse；
- Revision Agent proposal 的测试又是另一条 Transform 数据链。

这些测试证明原语存在，但不能作为同一故障的连续闭环证据。

## 6. 门禁记录

| 检查 | 本次结果 |
|---|---|
| `make demo-check` | 通过 |
| Ruff | all checks passed |
| 后端 Pytest | 217 passed |
| Alembic | `0001→0015` 通过 |
| TypeScript / ESLint | 通过 |
| 前端 Vitest | 7 files / 28 tests passed |
| 前端 production build | 通过 |
| 黑盒 E2E | 19 passed |
| 专项对话总结固化复用 | 1 passed in 6.30s |
| 接力、外部 Browser Patch、Catalog active 三项回归 | 3 passed in 2.87s |
| `git diff --check` | 通过 |

测试全绿说明当前实现稳定地满足了现有断言，不消除上述“断言对象与设计验收不一致”的问题。

## 7. 011 完成定义

011 后续只有同时满足以下条件，通配型闭环才能从 `partial` 升为 `closed-loop`：

1. Patch 草稿保留原 `Workflow.id`，发布生成真正的 v2；v1 仅精确可解析，搜索只返回 v2。
2. 外部瞬态失败不能靠冻结 error fixture 获得可复现资格；occurrence/主动探测策略进入后端门禁。
3. 同一个 Browser/MCP 失败完成“公开 API 观测→Revision Agent proposal→应用→Replay→人工发布→原/新输入复用”。
4. TaskCheckpoint claim 返回并全程校验 owner/generation/token；启动、reconcile 和回答使用 CAS。
5. 已完成步骤通过执行账本/deny set 被 Host 强制禁止重做，而不是只写在 Recovery prompt 中。
6. AgentRun handoff 和 external revision 均新增真实 API、独立 Worker、持久数据库黑盒。

具体修复顺序见 [`NEXT_STEPS-011.md`](../STEP/NEXT_STEPS-011.md)。
