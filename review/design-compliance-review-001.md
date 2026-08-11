# 设计与规范达成度审查报告

审查日期：2026-08-03  
审查范围：`docs/` 下四份文档、后端、前端、迁移、示例能力包与现有自动化测试。

## 1. 结论

当前项目**能够作为阶段 A/C 的演示原型继续演进，但尚不能完成设计文档定义的 MVP 闭环，也不具备生产部署条件**。

已经成立的最小闭环是：创建并发布确定性 DAG → 结构化表单启动 → SQLite 持久化 NodeRun → Worker 执行 Input/Transform/Condition/Approval/Output/Artifact/SkillX Direct → SSE 展示结果。这个闭环有代码和测试证据。

文档的核心产品闭环“Agent 首次探索 → 结构化轨迹 → 生成工作流草稿 → 人工补齐/发布 → 浏览器安全重放”尚未形成。MCP、LLM、Agent 和真实 Browser 节点没有接入执行器；轨迹固化、权限、Secret、审计和发布审核也未实现。因此，README 中“已实现”的部分表述应理解为接口或骨架存在，不能理解为端到端能力已交付。

建议把当前版本正式标记为 **Milestone A：deterministic workflow prototype**，不要标为设计文档中的 MVP。

## 2. 审查口径

文档存在三层内容：

1. `workflow-design-doc.md`：产品目标和 MVP 承诺，是主要验收基线。
2. `workflow-dev-doc.md`：工程实现、阶段验收和 Definition of Done，是研发门禁。
3. `schema.md` 与 `skillx.md`：协议规范；其中 `skillx.md` 是更新、更完整的规范，应优先作为 SkillX 一致性基线。

判定规则：只有“实现已接入主路径，并有自动化或可重复运行证据”才记为完成；仅有类型、适配器类、静态页面或说明文字记为部分完成。

## 3. MVP 能力矩阵

| 设计承诺 | 状态 | 实现证据 | 关键缺口 |
|---|---|---|---|
| 通用对话工作空间 | 未完成 | 有对话页面和消息持久化 | 每条消息固定返回 `MODEL_NOT_CONFIGURED`；无 LLM/Agent、组件匹配、参数补齐、轨迹或固化入口 |
| 组件列表、详情、表单运行 | 基本完成 | 组件 API、RJSF 表单、锁定 WorkflowVersion、运行跳转 | 无权限、历史对比、风险/成功率；所有组件被错误标为“零 LLM” |
| 可视化工作流编辑器 | 部分完成 | JSON 编辑、DAG 预览、保存、校验、发布 | 无拖放、连线、节点配置面板、变量选择器、版本差异和发布审核；循环图会冻结预览 |
| 运行内核与持久化 | 部分完成 | Run/NodeRun/Event、Worker 领取、重试、审批暂停、SSE | 心跳、租约所有权、取消、并发推进和运行策略语义不完整，不能保证副作用只执行一次 |
| 工作流编译与版本锁定 | 部分完成 | DAG、可达性、引用、Schema 语法、危险重试、WorkflowVersion digest | 无节点 Schema 兼容、能力解析、权限/风险检查；SkillX 仍引用可变本地路径，没有锁定能力快照 |
| SkillX Direct | 部分完成 | 示例包、输入/输出校验、Python callable、无模型执行测试 | 未完整验证 Manifest、权限、Effect、环境、Artifact、取消、超时；代码在 Worker 进程内直接加载 |
| MCP Registry/Gateway | 未完成 | HTTP/STDIO adapter 类存在 | 无数据库模型/API/注册/发现主路径；MCP 节点固定返回未配置 |
| LLM/Hybrid 节点 | 未完成 | NodeType 存在 | 执行器固定返回未配置，无模型 Profile、结构化输出、Token/错误统计 |
| Agent/TraeX 节点 | 未完成 | 子进程 adapter 骨架存在 | 未接入 Executor；无结构化轨迹归一化、Tool 白名单的实际传递、取消联动或草稿提炼 |
| Browser 安全重放 | 未完成 | 页面字段漂移函数和摘要函数存在 | 无 Playwright/BrowserContext、填写、提交、提交前重新计算授权摘要、成功验证或截图；URL/身份检查有缺陷 |
| 轨迹生成工作流草稿 | 未完成 | 无主路径实现 | Distillation API、模型、数据结构和 UI 均缺失 |
| 身份、RBAC、权限交集 | 未完成 | 数据表中有固定 `owner_id` | API 全部匿名，以 `local-user` 代替身份；无组织、scope、资源鉴权和发布审核 |
| Secret、脱敏与审计 | 未完成 | README 描述边界 | 无 Secret Provider；Run/API/SSE 展示完整输入输出；无统一脱敏和审计表/事件 |
| Artifact | 部分完成 | 文件落盘、hash、元数据、下载 API | 无访问控制；同名 Artifact 会覆盖既有文件；无内容完整性复核 |
| 可观测性与运维 | 少量完成 | 运行事件、live/ready、SQLite WAL | 无指标、结构化应用日志、审计、Worker heartbeat；ready 未检查迁移版本、可写性和 Artifact |

## 4. 分阶段验收

### 阶段 A：运行内核 — 部分通过

通过项：

- 确定性示例工作流可持久化并成功完成。
- DAG 基础静态检查、Input/Transform/Condition/Approval/Output/Artifact 已存在。
- 审批等待会释放 Worker；SSE 能按序补发数据库事件。
- SQLite 初始 Alembic 迁移可从空库执行。

未通过项：

- 文档要求的“取消、失败和恢复”没有可靠实现：运行中节点不会收到取消；租约无续期；过期 Worker 仍可写回。
- `maxDurationSeconds`、`maxParallelNodes` 和 `onError` 只被解析，没有执行语义。
- 多 Worker/并行分支没有状态机或数据库并发测试。

### 阶段 B：能力运行时 — 未通过

SkillX Direct 的最小 happy path 可用，但 MCP 和 LLM Executor 未接入；不存在天气/MCP 验收组件，也无法证明混合工作流只在指定节点消耗 Token。

### 阶段 C：前端与组件 — 部分通过

组件列表、组件详情、Schema 表单、DAG、Run 详情、审批 UI 和 JSON 草稿编辑可用。设计要求的可视化编排、变量选择、节点 Schema 兼容提示、版本差异、发布管理和业务/技术视图没有实现。

### 阶段 D：对话与 TraeX — 未通过

页面和 adapter 骨架不能构成验收能力。当前没有 Agent 执行、轨迹事件、组件匹配、参数收集或成功轨迹生成草稿。

### 阶段 E：浏览器固化 — 未通过

当前仅能对调用方提供的 `observedPage` JSON 做局部比较；没有真实浏览器执行、隔离会话、填写/提交、一次性授权消费、结果验证或 ContractPatch 草稿。

## 5. SkillX 规范符合性

示例 `text-stats` 包与 `skillx.md` 的 `skillx.dev/v1alpha1` 基本结构一致，具有 `SKILL.md`、Direct Interface、Draft 2020-12 输入输出 Schema、权限/Effect 声明和可运行入口。现有测试证明正常输入可执行、非法输入会在调用前被拒绝。

但当前 Host 不能声明 `Host-Direct` 符合性，因为规范要求的权限控制、Artifact、环境隔离、取消/超时和完整错误语义没有实现。Manifest 解析也没有校验未知字段、SemVer、`SKILL.md` 名称一致性、默认接口、接口公共必填项、权限/Effect 合并和依赖确定性。

此外，文档自身存在阻塞性冲突：

- `docs/skillx.md` 和实际示例使用 `apiVersion: skillx.dev/v1alpha1`，且接口位于 `spec.interfaces`。
- `docs/schema.md` 与 `docs/workflow-dev-doc.md` 使用 `apiVersion: skillx.platform/v1`，接口位于顶层 `interfaces`。

必须先指定唯一规范源并修订旧示例，否则“是否符合 docs”没有单一答案。建议以 `skillx.md` 为准，其他两份文档只引用规范版本，不再复制 Manifest 模板。

## 6. 验证结果

| 命令 | 结果 |
|---|---|
| `backend/.venv/bin/ruff check app tests migrations` | 通过 |
| `backend/.venv/bin/pytest -q` | 11 passed |
| `frontend/npm run check` | 通过 |
| `frontend/npm run lint` | 通过 |
| `frontend/npm run test` | 2 passed |
| `frontend/npm run build` | 通过 |
| 临时 SQLite 执行 `alembic upgrade head` | 通过 |

现有测试总量很小，且后端测试通过 `Base.metadata.create_all()` 建库，不是通过 Alembic；没有 API、SSE、权限、并发 Worker、租约、取消、MCP、TraeX、浏览器提交或前端关键页面测试。因此这些结果只能证明当前样例没有回归。

## 7. 最短达成路径

不要同时铺开所有页面和 Runtime。按风险闭环推进：

1. **先修运行内核正确性**：心跳续租、完成时 CAS 校验、取消传播、并发推进锁、事件序列、策略执行和并发测试。否则接入任何写操作都会放大重复副作用风险。
2. **再建立安全边界**：AuthContext、owner/org 过滤、发布权限、能力根目录/可信签名、Worker 隔离、敏感字段脱敏、Artifact 鉴权。
3. **完成一个真实零 LLM 组件**：MCP Registry/Gateway → MCP Executor → 版本/Schema 快照 → 无模型 E2E。这是核心价值的最短证明。
4. **完成浏览器提交闭环**：真实页面契约、身份校验、填写、服务端生成并消费一次性授权、提交后验证。未完成前禁止把 Browser 标为可提交。
5. **最后接 Agent 与固化**：TraeX Executor、标准事件、轨迹 Artifact、Distillation 草稿。它依赖前四步提供稳定、安全的目标节点。
6. **补齐可视化编辑器与发布治理**：当编译器能返回真实节点契约和风险诊断后，再做拖放、VariablePicker、VersionDiff，避免 UI 建在不稳定 DSL 上。

达到设计文档 MVP 前，至少需要新增：并发/恢复集成测试、API 鉴权测试、MCP E2E、浏览器审批提交 E2E、Agent 轨迹固化 E2E，以及历史版本回放测试。
