# 设计与规范达成度审查报告（002）

审查日期：2026-08-03  
审查基线：`docs/` 下五份设计、开发与验收文档，以及当前后端、前端、迁移、示例和自动化测试。  
范围约束：本版本仅作为单用户 Local Demo；鉴权、RBAC、组织隔离不纳入本轮阻断项。

## 1. 结论

当前项目已经从 001 版的“确定性工作流原型”推进到**后端能力运行时基本成形的 Local Demo**：Worker 租约/CAS/取消/超时、SkillX 版本锁定、MCP STDIO、LLM/Agent 节点、轨迹草稿和真实 Playwright 提交均有实现与自动化证据。

但它仍**不能按 `docs/demo-acceptance.md` 宣布 MVP 验收完成**。根因不是能力类数量不足，而是用户主路径和验收证据没有闭环：

1. 对话页能展示组件候选，却不能补参、确认并启动该组件。
2. “通用 Agent 工作空间”实际只做组件名子串匹配或单次 LLM 请求，没有运行 TraeX Agent，也没有实时 Agent 轨迹。
3. 固化只证明“轨迹可生成草稿”，没有证明“真实 Agent 轨迹 → 人工发布 → 新输入复用”。
4. Browser 的 L2 审批会把节点直接置为成功；若该节点原本需要提交，审批后不会重放和提交。
5. `make demo-check` 虽然通过，但测试主要在进程内直接调用服务/路由，不符合“真实 HTTP API + 独立 Worker + 持久化数据库”的六场景验收口径。

因此，当前最准确的版本标识是：**Local Demo backend-complete / product-flow-incomplete**。可以演示单项运行能力，不应宣称设计文档定义的完整 MVP 已交付。

## 2. 相比 001 版的实质进展

| 001 版主要缺口 | 002 版状态 | 证据 |
|---|---|---|
| Worker 无续租、CAS、取消和策略语义 | 已补齐基础实现 | 心跳续租、迟到结果 CAS、取消传播、超时、`onError`、并发汇聚测试 |
| SkillX 使用可变路径、进程内执行 | 已补齐 Local Demo 边界 | Registry 锁定 SemVer/digest、根目录限制、独立子进程、Secret 句柄 |
| MCP 只有 Adapter 骨架 | STDIO 主路径已接通；HTTP 仍不完整 | Registry、ToolSnapshot、Executor、STDIO 天气 E2E |
| LLM/Agent 固定未配置 | 节点执行器已接通；真实 TraeX 未验证 | Fake LLM/Agent 测试、TraeX 子进程参数与事件适配 |
| 无轨迹固化 | 已能生成不自动发布的草稿 | ConversationRun、Distillation、可编辑 Workflow Draft |
| 无真实浏览器 | 已有真实 Playwright 提交闭环 | 隔离 Context、审批重放、页面/身份绑定、截图与 DOM 摘要测试 |
| 环图冻结前端 | 已修复 | 有界布局与环图测试 |
| SkillX 文档规范冲突 | 已收敛 | `skillx.dev/v1alpha1`、`spec.interfaces` 成为唯一口径 |

## 3. MVP 能力矩阵

判定口径：只有接入用户或执行主路径，并有与验收口径相符的可重复证据，才判为“完成”。

| 设计承诺 | 状态 | 已实现 | 剩余关键缺口 |
|---|---|---|---|
| Workflow Compiler 与不可变版本 | 基本完成 | DAG、引用、Schema、节点约束、Capability/MCP 锁定、digest | 缺节点间 Schema 兼容、版本差异和风险摘要；迁移历史不独立于当前 ORM |
| 独立 Worker 与持久化状态机 | 基本完成 | 租约、心跳、CAS、取消、超时、恢复、重试、审批、SSE | `maxParallelNodes` 领取不是原子配额；尚无真实双进程验收 |
| SkillX Direct Host | 基本完成 | Manifest 校验、根路径限制、版本/digest、子进程、输入输出契约、Secret 句柄 | 仍是受信任代码边界；取消/资源限制和更多规范符合性测试可加强 |
| MCP Registry/Gateway | 部分完成 | 注册、版本、ToolSnapshot、STDIO discover/call、输入输出校验、Secret 引用 | HTTP Adapter 不能处理标准 SSE/会话初始化；重发现忽略同版本 Tool Schema 变化 |
| LLM 节点 | 基本完成 | 显式节点调用、结构化输出、Token 事件、无配置错误 | 真实模型兼容性/错误映射测试不足；无 Profile 版本锁定和成本汇总 |
| Agent/TraeX 节点 | 部分完成 | 子进程、步数/Token/Tool 参数、结构化输出、轨迹 Artifact、取消 | 只用 Fake Agent 验证；Host 未验证工具调用是否服从白名单；对话页未接 TraeX |
| 通用对话调用组件 | 未完成 | 消息持久化、候选响应、后端可在显式传入组件和参数时启动 Workflow | 候选匹配是组件名子串；前端没有确认/补参动作；无法从 UI 完成 E2E-4 |
| Agent 轨迹固化 | 部分完成 | 结构化轨迹可确定性编译为草稿，未知步骤阻止发布 | 当前对话轨迹是单次 LLM 合成；无真实 Agent 轨迹、分析页、人工发布和新输入复用 E2E |
| Browser 安全重放 | 基本完成但有阻断缺陷 | URL/身份/字段漂移、隔离浏览器、一次性摘要、审批、提交验证、UNKNOWN_OUTCOME | L2 审批错误跳过重放/提交；L3 无 ContractPatch 草稿；缺 L2 自动化测试 |
| 可视化编辑器 | 未完成 | JSON DSL 编辑、DAG 预览、保存、校验、发布 | 无拖放、连线、Inspector、VariablePicker、Schema 兼容提示、版本差异 |
| 组件与运行详情 | 部分完成 | 列表、Schema 表单、执行分类、节点状态、审批、SSE、Artifact | 缺业务/技术双视图、节点耗时/Token/依赖版本、最近成功率和风险信息 |
| Secret 与脱敏 | 基本完成（Local Demo） | 环境变量引用、按声明注入、回显阻断、事件摘要、基础递归脱敏 | NodeRun 缺节点级 Schema 脱敏；真实 TraeX/MCP/异常文本覆盖仍需验证 |
| Auth/RBAC | 本轮不评估 | README 已明确仅 loopback 单用户 Demo | 若未来共享或联网部署，必须重新列为发布阻断项 |

## 4. 分阶段验收

### 阶段 A：运行内核 — 基本通过

核心状态机问题已修复，测试覆盖心跳、防重复接管、迟到结果、取消、超时、`onError` 和 diamond 汇聚。剩余风险是并发领取时 `maxParallelNodes` 采用“先计数、后领取”两步逻辑，多个 Worker 可能同时越过配额；现有测试是顺序 claim，不能证明并发安全。

### 阶段 B：能力运行时 — 部分通过

SkillX Direct 和 MCP STDIO 的零模型路径已经成立；Hybrid 也能证明只有显式 LLM 节点产生 Token。MCP Streamable HTTP 只实现 JSON POST 子集，真实 Agent 只实现适配器、未做目标 TraeX 命令验收，因此不能判为完整通过。

### 阶段 C：前端与组件 — 部分通过

组件运行、JSON 编辑、DAG、Run/审批/Artifact 页面可用。设计要求的可视化编排、变量选择、版本治理和完整运行指标尚未实现。

### 阶段 D：对话与 TraeX — 未通过

后端显式传入 `componentId + inputs` 时能把控制权交给 Workflow，但前端没有发送这两个字段。未绑定对话以组件名子串匹配候选；否则只调用一次 LLM，并把该调用包装成 `agent_run`。这不是“Agent 探索 → Tool 轨迹 → 固化”的产品路径。

### 阶段 E：浏览器固化 — 部分通过

L0/L3/L4 和提交审批的主链路已经接近设计目标，真实 Playwright 测试也有价值。但 L2 发生在提交判断之前，审批接口又只对 `effect=form.submit` 重放，导致 L2 被批准后节点直接成功而没有提交。ContractPatch 仍未实现。

## 5. 六条 Demo E2E 判定

| 场景 | 功能证据 | 按 `demo-acceptance.md` 判定 | 原因 |
|---|---|---|---|
| E2E-1 确定性组件 | 有 | 未正式通过 | 服务层/同进程 Worker 测试，未走真实 API 和独立 Worker |
| E2E-2 MCP 组件 | 有 | 未正式通过 | 真实 STDIO Tool，但注册、运行与 Worker 均在测试进程内 |
| E2E-3 Hybrid 组件 | 有 | 未正式通过 | 使用 Fake Model，未走真实 API/独立 Worker |
| E2E-4 对话调用组件 | 后端局部有 | 未通过 | 测试直接调用路由函数并显式传参；UI 无确认和补参能力 |
| E2E-5 轨迹固化 | 局部有 | 未通过 | 只到草稿；无真实 Agent、人工发布和新输入复用 |
| E2E-6 浏览器提交 | 较强 | 未正式通过 | 有真实浏览器，但 Worker/审批仍在测试进程内；L2 分支错误 |

`make demo-check` 当前能稳定通过静态检查、46 个后端测试、空库迁移、4 个前端测试和生产构建，但 Makefile 没有额外启动 API/Worker 进程并执行六条黑盒场景。因此“门禁命令通过”和“六条验收通过”是两件事。

## 6. 验证结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 通过 |
| `backend/.venv/bin/ruff check app tests migrations` | 通过 |
| `backend/.venv/bin/pytest -q` | 46 passed |
| `frontend/npm run check` | 通过 |
| `frontend/npm run lint` | 通过 |
| `frontend/npm run test` | 4 passed |
| `frontend/npm run build` | 通过 |
| 空 SQLite `alembic upgrade head` | 通过 |

这些结果证明现有单元/集成能力没有明显回归，但不能替代 `docs/demo-acceptance.md` 定义的黑盒验收。

## 7. 最短达成路径

1. 先修 Browser L2 与并发领取两个运行正确性问题，避免“显示成功但动作未发生”和超额并发。
2. 打通对话候选卡片的确认、Schema 补参和 Workflow 启动，形成最短用户闭环。
3. 将通用对话接到真实 TraeX 轨迹，固化分析必须使用运行时产出的轨迹，不再用单次 LLM 合成替代。
4. 完成“固化草稿 → 人工发布 → 新输入复用”闭环，再实现 ContractPatch。
5. 建立真实 API + 独立 Worker 的六场景验收 harness，并让 `make demo-check` 调用它。
6. 最后补可视化编辑器和运行指标；这些不应阻塞前述核心价值闭环。

