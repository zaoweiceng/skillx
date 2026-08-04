# SkillX Workflow

SkillX Workflow 是一个契约驱动的可视化工作流平台。首次任务可由 Agent
探索，稳定任务发布为不可变工作流版本，并由独立 Worker 持久化执行。模型只在
显式的 LLM/Agent 节点介入。

## 能力状态

| 能力 | 状态 | 说明 |
|---|---|---|
| Workflow Compiler、SQLite 队列、Direct 节点、审批、SSE | `available` | 已接入 API/Worker/UI 主路径 |
| SkillX Direct | `available` | Registry 锁定 digest，独立子进程执行 |
| MCP STDIO | `available` | Registry、Tool Snapshot、Executor 与本地 E2E 已接通 |
| MCP Streamable HTTP | `partial` | 已实现 initialize/会话 id/JSON+SSE、会话过期重连与 loopback E2E；仍缺流式增量消费与 Last-Event-ID 断点续传 |
| LLM 节点 | `available` | Executor 已接通；真实运行需环境配置，测试用 Fake Adapter |
| Agent/TraeX | `available` | 唯一 `AgentHost` 信任边界（ConversationRun 与 Workflow Agent 共用）：requested∩host_granted、判别联合事件契约、callId 配对、step/Token/time/payload 上限、输出 Schema 与取消；事件双投影，审计只存脱敏摘要与 Artifact 引用，Secret 不落库 |
| 通用对话调用组件 | `available` | 结构化意图候选、锁定版本快照摘要、补参确认启动 Workflow；单次模型答记为 `llm_response`；conversationId 写入 `/chat/:id` 路由可刷新恢复，候选卡片绑定来源用户消息 |
| 通用 Agent 探索与固化 | `partial` | 已强制逐参数/output provenance、受限 Distillation Agent Proposal、节点 grouping 与 proposal/source digest；真实 Browser→publish→Playwright→非 fake AgentHost 黑盒已通过，复杂分支输出仍需扩展 |
| 通用失败接力 | `partial` | Recovery checkpoint 已有统一租约/心跳/取消、崩溃重领、标准用户问题与可执行 typed replan，四条路径均有自动化证据；在 Replay A 项全部关闭前按 007 总体门禁保持 partial |
| 失败可复现性与受控修订 | `partial` | Observation 原子 upsert；eligibility 只认持久 baseline reproduction，patched replay 禁止外部写并要求最终 Output，通过后保存 evidence、创建 draft；外部读取 fixture 捕获仍需扩展 |
| Workflow 能力目录与临时组合 | `partial` | 发布生成不可变 Manifest/Fragment；TraeX 在有界 L0 中输出受约束 Selection，再按需加载 L1/L2；PlanCompiler 展开两个锁定 refs + Agent 为 PlanSnapshot，版本锁、非法裁剪、固化建议黑盒已通过；检索语义模型与更复杂跨 Schema 组合仍需扩展 |
| MCP 副作用与幂等分类 | `available` | annotations 仅作提示且始终 `unreviewed`，分类创建**新的不可变快照**（旧快照/历史版本不变）；多 Tool Server 分类某 Tool 时克隆完整版本、分离远端 contract digest 与本地审核 digest，重复 discovery 稳定；0009 迁移用运行时 canonical 算法从行字段重算 remote-only contract_digest，非空 0008 升级后首次 discovery 不伪漂移；发布保存完整 lock，运行前逐项核对 digest/effect/idempotent/timeout，漂移即 `MCP_CONTRACT_DRIFT` fail closed |
| 浏览器契约与一次性授权 | `available` | Playwright 隔离 Context、L2 漂移一次性授权重放、提交确认、L3 ContractPatch 草稿；Browser Read/Extract 作为只读 Adapter 用 `BrowserReadContract` 导航并抽取正文/结构化字段作为节点输出（URL 来自输入，第二次以新输入重新取数，不缓存首次结果）；人工审核补丁可应用到草稿（仅字段契约，L4 安全域不可改） |
| 通配黑盒 E2E | `partial` | `docs/demo-acceptance.md` 当前场景已纳入 `make demo-check`；复杂分支输出与外部读取 replay 证据补齐前不标记 available |
| 可视化拖放编辑器 | `available` | 结构化 state 为唯一真相（JSON 降级为导入/导出视图），调色板新增节点、画布连线/删除、环检测、可编辑 Inspector 与 VariablePicker 写入映射、fallback 配置、发布治理；运行详情提供检查点接力视图（fallback target/审批状态/唯一 Recovery NodeRun）与受控 WorkflowPatch eligibility/replay 入口 |

`available` 表示已接入主路径并有自动化测试；`partial` 表示主路径部分实现、仍有已知缺口；
`not-configured` 不应在 UI 中标记为可运行能力。SkillX 的唯一规范源是
[`docs/skillx.md`](docs/skillx.md)，版本为 `skillx.dev/v1alpha1`。

## 本地启动

要求 Python 3.12+、`uv`、Node.js 20+。

```bash
cd backend
uv sync --extra dev
uv run playwright install chromium
uv run alembic upgrade head
uv run python -m app.seed
uv run uvicorn app.main:app --reload
```

另开终端启动 Worker：

```bash
cd backend
uv run python -m app.worker --worker-id worker-local
```

启动前端：

```bash
cd frontend
npm install
npm run dev
```

API 默认只允许按上述命令监听 `127.0.0.1`。本版本是单用户 Local Demo，禁止
直接绑定 `0.0.0.0` 或暴露到共享网络。访问 `http://localhost:5173`。OpenAPI 位于
`http://127.0.0.1:8000/docs`。

## 工程结构

```text
backend/app/
├── api/              # REST、SSE 与 DTO
├── application/      # 编译、表达式、编排与 Worker
├── domain/           # 不依赖框架的工作流契约
├── infrastructure/   # SQLAlchemy、SQLite 与存储模型
└── runtime/          # 节点、SkillX、MCP、TraeX、浏览器适配器
frontend/src/
├── api/ components/ hooks/ pages/
capabilities/skillx/  # 可移植 SkillX 包
examples/             # 工作流示例
docs/                 # 产品、开发与规范文档
```

API 和 Worker 必须使用相同的 `SKILLX_DATABASE_URL`。生产启动应先执行
Alembic 迁移，并设置 `SKILLX_AUTO_CREATE_SCHEMA=false`。

SkillX 注册只接受 `SKILLX_CAPABILITY_ROOT` 下的相对路径。Secret API 只保存
`name -> environmentVariable` 引用，值由 Worker 进程环境提供；只有 Manifest
`permissions.secrets.use` 声明的句柄会注入 SkillXContext。MCP STDIO 的
`environment` 同样只接受 `{"API_KEY": {"secretRef": "name"}}`，禁止明文值。

真实模型通过
`SKILLX_MODEL_ENDPOINT`、`SKILLX_MODEL_NAME` 和
`SKILLX_MODEL_API_KEY_ENV` 指向的环境变量配置；TraeX 命令通过
`SKILLX_TRAEX_COMMAND` 配置。Secret 值不得写入 `.env` 以外的工作流定义、
事件或 Artifact。

## 验证

```bash
cd backend
uv run ruff check app tests migrations e2e
uv run pytest -q

cd ../frontend
npm run check
npm run lint
npm run test
npm run build
```

在仓库根目录：

- `make quality-check`：较快的静态/单元/进程内集成检查与前端构建。
- `make demo-check`：在 `quality-check` 之上执行真实 API + 独立 Worker 子进程黑盒 E2E；
  场景数量和完成状态以 `docs/demo-acceptance.md` 为准。

测试覆盖双 Worker 租约/CAS、原子并发配额、取消、超时、并行汇聚、SkillX 版本锁定、
MCP STDIO/HTTP 与契约漂移、Hybrid/真实 TraeX Agent、轨迹固化与发布复用、浏览器一次性
授权与 L2 漂移重放、Artifact 暂存落位、迁移历史与前端环图阻断。

## 安全边界

- Direct SkillX 只运行受信任包；Python venv 不是安全沙箱。
- SkillX 包路径会执行根目录约束，输入输出均通过 Draft 2020-12 校验。
- MCP 和 TraeX 子进程命令必须命中可执行文件白名单。
- 浏览器提交没有审批和有效快照时不得执行；点击后断言失败归类为
  `UNKNOWN_OUTCOME`，不得自动重试。
- Secret 仅允许保存引用；当前仓库不提供明文 Secret 存储。
