# 设计与规范达成度审查报告（003）

审查日期：2026-08-03  
审查基线：提交 `274dc68`、`docs/` 下五份文档、002 两份审查报告及 `NEXT_STEPS-002.md`。  
范围约束：单用户 Local Demo；鉴权、RBAC、多租户与生产级隔离不作为本轮阻断项。

## 1. 结论

当前项目已经达到**可重复运行的工作流 Local Demo**：核心 Worker、SkillX、MCP、LLM/Agent 节点、Browser 状态机、对话调用组件、迁移、可观测性和独立 API/Worker 测试框架都已成形。002 报告中的 5 个 P1 问题均有针对性修改，质量门禁从 46 个后端测试、4 个前端测试提升到 74 个和 10 个，并新增 6 个黑盒用例。

但仍**不能把设计文档定义的完整 MVP 判为完成**。当前差距已经从“运行内核不可靠”收敛到两个产品事实问题：

1. 真实 TraeX Agent 可以作为 Workflow 节点运行，但没有主路径把这次执行转换为 `ConversationRun(agent_run)`；代码注释引用的 `/conversations/{id}/agent-runs` 实际不存在。因而“通用 Agent 探索 → 轨迹 → 固化草稿”仍未闭环。
2. 黑盒 E2E-5 只运行预置 Fake Agent 组件，没有 Distillation、人工发布和新输入复用；E2E-6 使用调用方提供的 `observedPage` 与 `fakeSubmitResult`，没有运行真实 Playwright 页面。因此 `make demo-check` 通过不等价于 `docs/demo-acceptance.md` 六条全部按目标效果通过。

此外，MCP 发现仍把所有 Tool 默认标为“无副作用、幂等”，这会让未知写 Tool 获得错误的自动重试语义；可视化拖放编辑器仍未实现。

准确版本定位：**运行时 Demo 基本完成，Agent 固化产品闭环和最终验收真实性未完成。**

## 2. 002 问题回归结果

| 002 问题 | 003 判定 | 说明 |
|---|---|---|
| Browser L2 审批跳过提交 | 已修复 | 漂移授权与提交授权分离；L2 批准后重放，并覆盖二次漂移测试 |
| 对话候选不可补参确认 | 已修复主路径 | confirm API、版本令牌、RJSF 候选表单和 Run 链接已接通 |
| 通用 Agent 是静态匹配/单次 LLM | 部分修复 | `llm_response` 与导入轨迹已分型；但通用 Agent 按钮和真实 Agent ConversationRun 仍未接通 |
| MCP 重发现忽略 Schema 变化 | 已修复 | 同版本 digest 变化/Tool 消失会失败，新版本可扫描依赖 |
| `maxParallelNodes` 非原子 | 已修复（SQLite 范围） | 配额计数进入 claim UPDATE，并有双 session 竞争测试 |
| MCP Streamable HTTP 仅 JSON | 基本修复 | initialize、session id、JSON/SSE 已覆盖；流式增量和重连仍未实现 |
| MCP test 绕过契约 | 已修复 | test 与 Executor 共用契约调用服务 |
| Alembic 依赖当前 ORM | 已修复 | 0001–0006 使用固定 migration 定义 |
| readiness 写死 revision | 已修复 | 动态读取 Alembic head |
| Artifact 迟到结果留孤儿 | 基本修复 | staging + CAS 后落位；数据库提交失败后的最终文件仍需 GC 兜底 |
| `demo-check` 无黑盒子进程 | 框架已修复、场景不完整 | 已启动真实 API/Worker/Alembic DB，但 E2E-5/6 没有执行其标题描述的完整路径 |

## 3. MVP 能力矩阵

| 设计承诺 | 状态 | 当前证据 | 剩余缺口 |
|---|---|---|---|
| Compiler、版本锁定、迁移 | 基本完成 | DAG/引用/Schema、Capability/MCP locks、固定 Alembic 历史 | 缺节点间 Schema 兼容检查、版本差异 UI |
| Worker 与持久化状态机 | 基本完成 | 租约、心跳、CAS、取消、恢复、超时、策略、并发配额 | SQLite 多 Worker 仅适合本地；无生产级队列要求 |
| SkillX Direct | 基本完成 | 版本/digest、根路径、子进程、Secret、契约与 E2E | 仍是受信任本地代码，不是安全沙箱 |
| MCP Registry/Gateway | 基本完成但有安全语义缺口 | STDIO/HTTP、SSE、Schema 漂移、统一调用服务 | Tool 副作用与幂等性未发现/配置，默认值过于乐观 |
| LLM 节点 | 基本完成 | 显式节点、结构化输出、Token 事件、Fake E2E | 真实模型兼容性只依赖环境配置，无黑盒 fixture |
| Agent/TraeX 节点 | 基本完成 | 子进程、计划/Tool/step、Token、输出 Schema、Tool 事件白名单 | 真实运行 fixture 只在进程内测试；未接 Conversation/Distillation 主路径 |
| 通用对话调用组件 | 基本完成 | 候选、版本锁定、补参、确认、Workflow 接管 | “继续通用 Agent”只隐藏卡片；匹配仍是启发式子串打分 |
| Agent 轨迹固化 | 未完成主路径 | Distillation 函数及人工发布/复用的服务级测试存在 | 没有真实 Agent → ConversationRun 的生产者；E2E-5 绕过该断点 |
| Browser 安全重放 | 基本完成 | 真实 Playwright 进程内测试、L2/提交双授权、L3 Patch、UNKNOWN_OUTCOME | 黑盒 E2E 用 Fake Browser；ContractPatch 尚无审核/发布 UI |
| 可视化工作流编辑器 | 未完成 | JSON 编辑、DAG、只读 Inspector、上游变量提示、风险摘要 | 无拖放/连线/删除/缩放；VariablePicker 仍非可插入控件且 Input 路径提示错误 |
| 组件与运行详情 | 基本完成 | 业务/技术视图、成功率、节点耗时、指标、依赖、审批、Artifact | 缺版本差异和更完整历史趋势 |
| 六条黑盒验收 | 部分完成 | 真实 API、独立 Worker、Alembic 临时 DB，6 个测试通过 | E2E-5 未固化；E2E-6 未真实浏览器；E2E-4 不覆盖真实前端交互 |

## 4. 分阶段判定

### 阶段 A：运行内核 — 通过 Local Demo 验收

状态机、并发配额、取消、恢复和事件序列均有自动化证据。002 中最影响副作用正确性的风险已经关闭。

### 阶段 B：能力运行时 — 基本通过

SkillX、MCP STDIO/HTTP、LLM 和 Agent 节点均能执行。阻止完整通过的是 MCP Tool 的 Effect/Idempotency 没有可信来源：注册时一律写入 `side_effect="none"`、`idempotent=True`，不适用于写操作 Tool。

### 阶段 C：前端与组件 — 部分通过

组件使用、审批、运行详情、候选补参和 JSON 编辑可用；但设计文档把可视化编辑器列为 MVP 必须项，当前只有 JSON 编辑加只读 Inspector，不能判为完成。

### 阶段 D：对话与 TraeX — 部分通过

组件候选与 Workflow 接管已经成立，TraeX 节点也能独立运行。但两条路径没有连接：通用对话不会启动 TraeX，Agent 节点完成后也不会创建可固化的 ConversationRun。右侧轨迹抽屉仍是静态占位。

### 阶段 E：浏览器固化 — 基本通过实现、未通过最终黑盒验收

真实 Playwright 的检查、填写、审批重放、提交和验证已有进程内集成测试；L2/L3 分支也补齐。剩余问题是最终黑盒 E2E 仍使用 Fake Browser，且 ContractPatch 只生成数据，没有人工审核发布闭环。

## 5. 六条 E2E 重新判定

| 场景 | 003 判定 | 理由 |
|---|---|---|
| E2E-1 确定性组件 | 通过 | 真实 API、独立 Worker、Alembic DB，无模型事件 |
| E2E-2 MCP 组件 | 通过 STDIO 路径 | 固定 ToolSnapshot、真实 STDIO 子进程、零模型 |
| E2E-3 Hybrid 组件 | 基本通过 | 真实 Workflow 节点路径，但模型为 Fake Adapter |
| E2E-4 对话调用组件 | 通过 API 路径 | 自然语言候选、补参确认、Workflow 成功；前端仅有 mock 测试 |
| E2E-5 Agent 轨迹固化 | 未通过 | 黑盒测试只运行预置 Agent 组件，没有 Distillation、发布或新输入复用 |
| E2E-6 浏览器提交 | 未按目标通过 | 黑盒测试使用 fake observed page/submit result，没有真实 Playwright 页面 |

因此 `backend/e2e` 的“6 passed”是测试函数数量，不是六个设计场景全部完整达成。

## 6. 验证结果

| 命令 | 结果 |
|---|---|
| `make demo-check` | 命令通过 |
| Ruff | 通过 |
| Pytest | 74 passed |
| Alembic 空库升级 | 0001 → 0006 通过 |
| TypeScript check / ESLint | 通过 |
| Vitest | 10 passed |
| Vite build | 通过 |
| `pytest e2e` | 6 passed |

## 7. 最短完成路径

1. 建立唯一的通用 Agent Run Service：对话请求启动 TraeX，服务端归一化事件并创建 `ConversationRun(agent_run)`。
2. 让“继续通用 Agent”真正调用该服务，并把事件接入对话轨迹抽屉。
3. 重写 E2E-5，使其经过 Agent → ConversationRun → Distillation → 人工发布 → 新输入复用。
4. 重写 E2E-6，在 harness 内启动本地表单站点并使用 `runtime=playwright` 完成真实提交。
5. 为 MCP Tool 引入显式 Effect/Idempotency 分类，未知 Tool 默认不可自动重试。
6. 完成最小可视化编辑闭环，而不是继续扩展只读 JSON 辅助面板。

