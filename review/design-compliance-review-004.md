# 设计与规范达成度审查报告（004）

审查日期：2026-08-03  
审查基线：提交 `274dc68` 上的未提交修复、[`REPLAY-003.md`](../REPLAY/REPLAY-003.md)、003 三份文档及 `docs/` 下设计/规范文档。  
范围约束：单用户 Local Demo；鉴权、RBAC、多租户、生产级沙箱与分布式部署不作为阻断项。

## 1. 结论

本轮把两个最明显的断点接通了：

- E2E-5 已经通过 HTTP 走 Agent → ConversationRun → Distillation → 人工发布 → 组件复用；
- E2E-6 已经由独立 Worker 驱动真实 Playwright，并用本地站点证明审批前零提交、审批后仅提交一次、页面结构变化使旧授权失效。

`make demo-check` 全部通过，当前项目可以判定为**核心演示路径可重复执行的 Local Demo**。003 中 candidate 命名、Input 变量路径、ContractPatch URL 边界也已修复。

但仍不能把设计文档定义的完整 MVP 判为完成。阻断点已经从“路径不存在”变为“路径的运行语义不成立”：

1. Conversation Agent 在 API 请求事务内同步运行，运行完成前 run/event 都不可见；API 中断会整体回滚，且没有取消入口。因此实时轨迹、刷新恢复和长任务持久化并未成立。
2. MCP 分类会原地修改本应不可变的 ToolSnapshot；已发布 Workflow 保存的旧 digest 在运行时不校验，发布版本的副作用语义可能被事后改变。
3. 当前 Distillation 没有从真实 Tool/step 轨迹提炼确定节点，而是固定生成一个携带原始 prompt 的 Agent 节点。它证明了“重新运行 Agent”，尚未证明设计核心“探索一次，编译复用”。
4. 可视化编辑器仍是 JSON 文本编辑 + 只读 DAG/Inspector，无法不改 JSON 完成节点新增、配置、连线和发布。

准确版本定位：**真实 Browser 验收已完成；Agent 固化链路可达但不具备持久长任务与真实提炼语义；MCP 安全分类仍破坏版本不可变性；可视化编辑未完成。**

## 2. REPLAY-003 声明回归

| REPLAY-003 声明 | 004 判定 | 说明 |
|---|---|---|
| 唯一 Agent Run Service | 部分完成 | 生产入口存在，但运行仍在 API 请求/事务内同步执行，不是持久化长任务 |
| Agent 事件可恢复、轨迹实时展示 | 未按声明完成 | 事件只 `flush`，路由在 Agent 完成后才 `commit` 并返回 run id；前端此后才开始轮询 |
| 超时/取消 | 部分完成 | 有内部超时终止；无用户取消 API、取消状态或重启恢复 |
| “继续通用 Agent”真正发请求 | 已修复 | 按钮调用真实 endpoint；但会阻塞到 Agent 终态 |
| 只固化服务端成功 agent_run | 基本修复 | 外部导入和 llm_response 被拒绝；服务端来源仍只靠 `run_type` 约定 |
| E2E-5 真实 Agent 固化复用 | 路径通过、语义证据不足 | 真实 HTTP 路径完整；草稿只是单 Agent 节点，fixture 的固定 reply 不能证明参数提炼 |
| E2E-6 真实 Playwright | 已修复 | 本地站点、审批、单次提交、审批后 L3 漂移均有黑盒证据 |
| Harness 动态端口/失败日志/清理 | 部分修复 | 动态端口、Worker 就绪、成功清理已实现；测试断言失败仍按成功路径清理，不打印日志 |
| MCP unknown 默认保守 | 已修复默认值 | 无 annotations 时为 `unknown/false/unreviewed`，未审核阻止发布 |
| MCP annotations 只作提示 | 未完全落实 | annotation 来源被自动标记 `reviewed`，未经过 Registry 显式确认 |
| MCP digest 纳入 effect/idempotent/timeout | 实现有缺陷 | digest 已纳入，但分类直接修改原 Snapshot，破坏不可变锁定 |
| candidateSnapshotDigest | 已修复 | 命名和文案准确表达 stale 检测，不再声称一次性授权 |
| VariablePicker Input/嵌套/数组 | 部分修复 | Input/对象路径正确；生成的 `[]` 数组表达式不被后端语法支持，类型提示函数未接 UI |
| ContractPatch 内部 URL 校验 | 已修复 | 函数自身调用 `url_matches()`，有直接单测 |
| 发布 busy 守卫 | 已修复 | 发布全流程有独立 `publishing` 状态 |
| Artifact finalize/GC | 未处理 | 最终文件仍早于数据库 commit 落位 |
| 完整拖放画布 | 未处理（声明的非目标） | 仍为只读 React Flow 预览和 JSON 编辑 |

## 3. MVP 能力矩阵

| 设计承诺 | 状态 | 当前证据 | 剩余缺口 |
|---|---|---|---|
| Compiler、版本与迁移 | 基本完成 | DAG/引用校验、不可变 WorkflowVersion、0001→0007 空库迁移 | 缺节点间 Schema 兼容检查、版本差异 UI |
| Worker 与持久化状态机 | 基本完成 | Workflow Run 有租约、CAS、取消、恢复、超时、并发配额 | Conversation Agent 未接该持久化 Worker 模型 |
| SkillX Direct | 基本完成 | 版本/digest、子进程、Secret、契约与 E2E | Local Demo 仍执行受信任本地代码 |
| MCP Registry/Gateway | 部分完成 | STDIO/HTTP、Schema 漂移、保守 unknown、发布校验 | Snapshot 分类原地修改；annotations 自动审核；HTTP 无增量重连 |
| LLM/Agent 节点 | 基本完成 | 显式节点、输出 Schema、Tool 事件、TraeX fixture | Host 未独立执行步数/Token 上限，Tool 权限可由请求方扩大 |
| 通用对话调用组件 | 基本完成 | 候选、快照摘要、补参确认、Workflow 接管 | 匹配仍为启发式；旧候选“继续 Agent”可能取最近一条用户输入 |
| Conversation Agent | 部分完成 | endpoint、ConversationRun/Event 表、前端轨迹抽屉 | 同步请求、事务末尾提交、无取消/恢复、刷新会新建对话 |
| Agent 轨迹固化 | 部分完成 | 成功 run 可生成草稿并人工发布 | 未将 Tool/step 参数化为确定节点，固化后仍整体运行 Agent |
| Browser 安全重放 | 基本完成 | 真实 Playwright 黑盒、双阶段审批、L2/L3、UNKNOWN_OUTCOME | ContractPatch 尚无审核/发布 UI |
| 可视化编辑器 | 未完成 | JSON 编辑、只读 DAG、只读变量 chip、风险摘要 | 无 palette、拖放、连线、删除、可编辑 Inspector/VariablePicker |
| Artifact 一致性 | 部分完成 | staging、CAS 迟到清理 | commit 失败后最终目录仍可能产生孤儿 |
| 六条黑盒验收 | 基本通过 | 7 个真实 API/Worker E2E，E2E-6 为真实浏览器 | E2E-5 不证明运行中恢复及确定化提炼；前端仍以 mock 为主 |

## 4. 分阶段判定

### 阶段 A：运行内核 — Workflow Runtime 通过，Conversation Agent 未通过

已发布 Workflow 的独立 Worker 状态机满足 Local Demo。新 AgentRunService 另建了一套同步 API 内执行模型，没有复用队列、租约、取消和恢复能力，违反“长任务状态不能以 API 进程为唯一事实来源”的设计约束。

### 阶段 B：能力运行时 — 基本通过，但 MCP 版本语义阻断完整通过

SkillX、MCP、LLM、Agent、Browser 均有真实或可重复 fixture。当前最危险的不是未知 Tool 默认值，而是分类会修改同一个 Snapshot；这使已经发布的锁定版本不再真正锁定。

### 阶段 C：前端与组件 — 部分通过

组件运行、审批、运行详情、候选补参、Agent 按钮和轨迹展示已接通。刷新页面仍创建新 Conversation，画布与 Inspector 仍不可编辑，因此不能通过设计中的产品闭环。

### 阶段 D：对话与 TraeX — 路径基本通过，持久化语义未通过

真实 Agent API 和固化入口已存在；但启动请求直到完成才返回，运行中事件不可观察，API 进程退出会丢失整次运行事实，也没有取消入口。当前更接近“同步执行后保存结果”，不是设计的 Agent Run Handle。

### 阶段 E：浏览器固化 — 真实提交主路径通过

E2E-6/6b 已覆盖独立 Worker、真实页面、审批重放、唯一提交与结构漂移失效。剩余工作集中在 ContractPatch 的人工审核发布闭环，而不是浏览器执行真实性。

## 5. 六条 E2E 判定

| 场景 | 004 判定 | 理由 |
|---|---|---|
| E2E-1 确定性组件 | 通过 | 独立 API/Worker/Alembic DB，无模型事件 |
| E2E-2 MCP 组件 | 通过当前 STDIO 读 Tool 路径 | 固定 Snapshot、真实子进程、零模型；不覆盖分类后版本不变性 |
| E2E-3 Hybrid 组件 | 基本通过 | 真实 Workflow 节点路径，模型为可重复 Fake Adapter |
| E2E-4 对话调用组件 | 通过 API 路径 | 候选、补参、确认、Workflow 成功；未覆盖真实浏览器前端 |
| E2E-5 Agent 轨迹固化 | 基本通过路径 | 完整 HTTP 链存在；未覆盖运行中可见/中断恢复，草稿未提炼确定节点 |
| E2E-6 浏览器提交 | 通过 | 真实 Playwright、本地站点、一次性审批、单次提交 |
| E2E-6b 审批后漂移 | 通过 | 新增必填字段后旧授权失效且未提交新数据 |

因此“7 passed”准确证明七个测试实现的路径稳定，但不能证明完整 MVP 已完成。

## 6. 验证结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| Ruff | 通过 |
| Pytest | 85 passed |
| Alembic 空库升级 | 0001 → 0007 通过 |
| TypeScript check / ESLint | 通过 |
| Vitest | 13 passed |
| Vite build | 通过 |
| `pytest e2e` | 7 passed |
| `git diff --check` | 通过 |

## 7. 最短完成路径

1. 把 Conversation Agent 改为“先持久化 queued run 并返回 202，再由独立 Worker 领取”，事件逐批提交，补取消与重启恢复。
2. 将 Tool 权限改为请求需求与 Host policy 的交集，并由 Host 独立执行 step/Token/time/output 限制。
3. 保持 MCP ToolSnapshot 不可变：分类产生新 Snapshot/ServerVersion，运行时校验 WorkflowVersion 中锁定的 id + digest。
4. 让 Distillation 消费真实 tool/step 轨迹，至少提炼出一个参数化的确定节点；E2E 用新输入证明行为随输入变化且 Agent/Token 成本下降。
5. 完成最小可视化编辑闭环，再处理 Artifact finalize/GC 与 MCP HTTP 增量重连。

