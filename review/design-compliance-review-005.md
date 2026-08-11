# 设计与规范达成度审查报告（005）

审查日期：2026-08-04  
审查基线：提交 `be64409`、[`REPLAY-004.md`](../REPLAY/REPLAY-004.md)、004 三份文档及 `docs/` 下设计/规范文档。  
范围约束：单用户 Local Demo；鉴权、RBAC、多租户、生产级沙箱与分布式部署不作为阻断项。

## 1. 结论

REPLAY-004 对基础工程结果的声明基本可信：`make demo-check` 全部通过，Conversation Agent 已改为持久化长任务，MCP 运行锁、最小可视化编辑、ContractPatch 应用和 Artifact 提交后落位均已进入主路径。

但“通用 Agent 探索与固化”为 `available` 的结论不成立。当前实现只证明了一种人工构造的“输入字符串 → 冻结字典查表”特例，不能证明真实 Tool、Browser、文件或 API 操作可被部分/整体固化，更不能支持任意已发布工作流在重试耗尽后把检查点交给 TraeX 接力。

对新增需求的直接回答是：**当前版本不能端到端实现。**网页总结只是一个验收样例；真正缺少的是与节点类型无关的三层能力：

1. **通用动作表示与固化裁决**：将首次轨迹归一化为带能力版本、输入绑定、输出 Schema、副作用和可重放性的 Action IR；Agent 可以提出分区建议，但 Compiler 必须作最终确定性裁决。
2. **显式恢复图与标准检查点**：任何节点重试耗尽后，只能按已发布版本的 `fallback` 规则，把已完成输出、Artifact 引用、失败事实、能力锁和剩余子图交给受限 TraeX 节点。
3. **受控修订闭环**：恢复成功后按稳定 failure fingerprint 判断是瞬态故障还是可复现缺陷；Agent 只能生成 WorkflowPatch 草稿，必须经过校验、回放、diff 和人工发布，不能静默改当前版本。

准确版本定位：**基础 Workflow Runtime 已是可重复 Local Demo；首次 Agent 长任务可审计；固化编译器仍是 lookup fixture；通配型复用、通用恢复接力和修订学习尚未实现。**

## 2. REPLAY-004 声明回归

| REPLAY-004 声明 | 005 判定 | 说明 |
|---|---|---|
| Agent 先持久化后执行、202 handle | 已完成 | queued/running/终态、租约、取消和运行中事件已落地 |
| 过期 Agent 租约可恢复 | 部分完成 | 能形成 `UNKNOWN_OUTCOME` 唯一失败终态，但不会从已完成上下文继续 |
| Host 能力交集与预算 fail closed | 部分完成 | Conversation Agent 有校验；被拒能力只记录 narrowed 后继续；Workflow Agent 仍走旧执行器 |
| 原始轨迹只进 Artifact，数据库仅脱敏摘要 | 未按声明完成 | 每条 Tool 参数和结果仍原样写入 `conversation_run_events` |
| MCP 分类产生不可变新版本 | 部分完成 | 旧 Snapshot 不再被改；但分类修订只复制被选 Tool，多 Tool Server 的 current 版本不完整 |
| Tool action → 确定节点，完成真实编译复用 | 未按声明完成 | 任意字典结果都可误判为 lookup；真实能力没有版本锁；混合轨迹中的未知动作会被丢弃 |
| 最小可视化编辑闭环 | 基本完成 | 结构化 state、节点新增、连线、删除和 Inspector 已有；完整拖放和版本 diff 仍缺 |
| Artifact 提交后落位与 GC | 部分完成 | Workflow Artifact 已提交后落位；GC 只有函数和单测，未接定时/启动恢复；Agent 轨迹绕过该生命周期 |
| ContractPatch 人工应用到草稿 | 已完成当前边界 | 仍是 Browser 字段契约特例，不是通用 WorkflowPatch |
| MCP HTTP 会话过期重连 | 已完成声明范围 | 增量流消费与 Last-Event-ID 仍未实现 |

## 3. 新增通配型复用需求判定

| 所需能力 | 当前状态 | 证据与边界 | 判定 |
|---|---|---|---|
| 首次未知任务由 TraeX 探索并留可编译轨迹 | 部分 | 只规范化 `tool.call.started/completed`；没有 Browser action、文件、API、LLM 调用的统一动作描述 | 不满足 |
| Agent 判断哪些步骤交给确定工作流、哪些保留 Agent | 未实现 | [`distillation.py`](../backend/app/application/distillation.py) 是固定 Python heuristic，不接收分区方案；Compiler 也没有 action compiler registry | 不满足 |
| 部分或整个流程固化 | 特例 | 只能生成 lookup Transform，或完全退回单个 Agent；不能保留“确定前缀 + Agent 总结”的顺序混合图 | 不满足 |
| 第二次复用可执行网页以外的操作 | 手工可做、自动固化不可做 | 人工编写的 MCP/SkillX/Transform 工作流可运行；首次轨迹不能可靠生成这些锁定节点 | 不满足端到端需求 |
| 网页访问后把内容和提示词交给 TraeX 总结 | 未实现 | Browser Executor 面向表单契约，只输出 drift/formValues；没有通用读取/抽取正文能力，也没有 BUA 轨迹事件 | 不满足 |
| 失败重试耗尽后携带上下文续跑 | 未实现 | Node 只有 `onError=fail_run|continue`；`resume_token` 未消费；Orchestrator 没有 RecoveryRun/Checkpoint/Fallback 调度 | 不满足 |
| 已完成部分不重复执行 | 基础事实可取，语义未实现 | NodeRun 保存输出，但没有冻结的 remaining DAG、Artifact/锁引用或副作用去重边界供 TraeX 接力 | 不满足 |
| 根据可复现性决定是否修订固化流程 | 未实现 | 只有 Browser ContractPatch；没有 failure fingerprint、聚合、通用 WorkflowPatch、回放或版本 diff | 不满足 |

### 3.1 为什么不能把它实现成 Browser 特例

需求的本质不是“第二次打开网页”，而是“把一次动态探索转为可验证、可恢复、可版本化的执行计划”。网页、MCP、文件处理和 API 调用的差别应被限制在 Action Adapter；Distillation、Checkpoint、Fallback、恢复决策和 WorkflowPatch 必须共用同一套领域模型。

推荐边界：

```text
TraeX 首次探索
  → NormalizedAction[]
  → Agent 提出分区建议
  → Compiler 按能力锁/Schema/副作用/参数证据裁决
  → Draft Workflow（确定节点 + 显式 Agent 节点）
  → 人工发布
  → Workflow Runtime 复用
  → 显式失败规则命中时生成 ExecutionCheckpoint
  → Recovery Agent 续跑/提出 WorkflowPatch 草稿
```

Agent 不能成为最终裁决者。否则“是否确定、是否可重试、是否锁定同一能力”仍由概率模型决定，固化就失去意义。Agent 负责语义理解和提出方案；Compiler 负责证明方案符合契约。

## 4. 现有设计文档达成度

| 设计承诺 | 005 状态 | 主要缺口 |
|---|---|---|
| Workflow Compiler、版本、持久 Worker | 基本完成 | fallback DSL、恢复子图和 checkpoint 契约未实现 |
| SkillX/MCP/Transform 确定执行 | 基本完成 | MCP 多 Tool 分类修订缺陷；轨迹无法生成真实锁定节点 |
| LLM/Agent 只在显式节点介入 | Workflow 主路径部分完成 | Workflow Agent 与 Conversation Agent 的 Host 边界不统一 |
| 未知流程探索后固化 | 未完成 | Action IR、参数识别、混合图编译和能力锁缺失 |
| 长任务取消、重试、恢复 | Workflow/Agent 各自部分完成 | 没有跨节点类型的局部恢复与续跑；Agent 过期直接失败 |
| fallback 必须显式声明 | 文档有、代码无 | [`schema.md`](../docs/schema.md) 已给 `fallback.on/interface/requireUserApproval`，领域模型未接入 |
| Browser 确定执行与局部 Agent 恢复 | 表单主路径完成、局部恢复未完成 | 无读取/抽取型 Browser 能力；L3 只产生 ContractPatch |
| 失败后不得静默改已发布版本 | Browser 特例满足 | 通用 WorkflowPatch 和 replay/diff/manual publish 尚无实现 |
| Artifact/审计可恢复 | 部分完成 | Agent 轨迹未登记 Artifact；Tool payload 未脱敏；GC 未调度 |
| 可视化编辑与发布治理 | 基本完成 | fallback、恢复证据、版本 diff 和补丁审核 UI 未实现 |

## 5. 测试与门禁结果

| 命令/阶段 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| Ruff | 通过 |
| 后端 Pytest | 124 passed |
| Alembic 空库升级 | 0001 → 0008 通过 |
| TypeScript check / ESLint | 通过 |
| Vitest | 23 passed |
| Vite production build | 通过 |
| 黑盒 E2E | 9 passed |

现有门禁证明已有路径没有明显回归，但没有覆盖以下关键命题：真实动态获取而不是冻结首次结果、混合轨迹不丢步骤、非网页操作复用、失败检查点接力、已完成副作用不重放、可复现故障才提出版本修订。

## 6. 005 版本验收定义

要把新增需求判为完成，至少需要四组黑盒证据：

1. **通用混合固化**：首次 TraeX 轨迹包含两个以上不同 kind 的动作；固化后既有锁定的确定节点，也有显式 Agent/LLM 节点，顺序和数据依赖不丢失。
2. **网页样例**：第二次运行实际重新获取变化后的页面内容，再把正文与固定/参数化提示词交给 TraeX 总结；不能返回第一次缓存结果。
3. **非网页样例**：使用同一 Action IR、Compiler 和 Recovery 机制固化 MCP/API/文件处理任务；实现中不得出现仅为 Browser 分叉出的复用框架。
4. **失败接力与修订**：在若干节点成功后注入可重试失败，重试耗尽后 Recovery Agent 收到标准 checkpoint 并从剩余子图继续；已完成副作用调用次数不增加。一次瞬态故障不提补丁，同 fingerprint 可复现故障才生成草稿，且旧版本保持不变。

详细实施顺序与完成条件见 [`NEXT_STEPS-005.md`](../STEP/NEXT_STEPS-005.md)。
