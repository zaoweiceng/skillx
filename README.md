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
| 通用 Agent 探索与固化 | `available` | 首次轨迹归一化为运行框架无关的 NormalizedAction IR，Agent 提出分区建议但 Compiler 按能力锁/Schema/副作用/参数证据最终裁决（`compiled|agent|rejected`）；未知动作保留为显式 Agent 节点而非丢弃；真实动态结果不误编译为 lookup，混合轨迹保留确定前缀+Agent+确定后缀，仅 succeeded 运行可固化 |
| 通用失败接力 | `available` | 节点重试耗尽且命中已发布版本的显式 `fallback` 时冻结持久化 `ExecutionCheckpoint`（已完成输出/剩余子图/能力锁/已提交副作用），受限 Recovery Agent 只消费 checkpoint 从剩余子图接力，已完成非幂等副作用不重放；Direct/MCP/Browser 失败不隐式升级为 Agent，安全/契约/未知错误码默认需人工审批；取消 recovering 同步取消 checkpoint fail closed |
| 失败可复现性与受控修订 | `available` | 稳定 `failureFingerprint` 排除时间戳/随机 id；瞬态故障不提补丁，可复现故障才生成 WorkflowPatch 草稿；通用 WorkflowPatch 只表达节点/边/配置/契约受限变更，拒绝宿主机路径/Secret/扩权；应用总是创建 draft、回放校验并展示 diff，旧版本不变，人工发布后才生效（ContractPatch 为其 Browser 子类型） |
| MCP 副作用与幂等分类 | `available` | annotations 仅作提示且始终 `unreviewed`，分类创建**新的不可变快照**（旧快照/历史版本不变）；多 Tool Server 分类某 Tool 时克隆完整版本、分离远端 contract digest 与本地审核 digest，重复 discovery 稳定；发布保存完整 lock，运行前逐项核对 digest/effect/idempotent/timeout，漂移即 `MCP_CONTRACT_DRIFT` fail closed |
| 浏览器契约与一次性授权 | `available` | Playwright 隔离 Context、L2 漂移一次性授权重放、提交确认、L3 ContractPatch 草稿；人工审核补丁可应用到草稿（仅字段契约，L4 安全域不可改） |
| 两类通配黑盒 E2E | `available` | 真实 API + 独立 Worker + 本地表单站点子进程；E2E-5 证明非网页任务确定化提炼/成本降为零、E2E-6 走真实 Playwright 提交、E2E-7 证明检查点接力且非幂等副作用只执行一次；网页与非网页共用同一 IR/Compiler/Checkpoint/RecoveryDecision/WorkflowPatch 管线，`make demo-check` 执行；失败留现场并打印日志 |
| 可视化拖放编辑器 | `available` | 结构化 state 为唯一真相（JSON 降级为导入/导出视图），调色板新增节点、画布连线/删除、环检测、可编辑 Inspector 与 VariablePicker 写入映射、fallback 配置、发布治理；运行详情提供检查点接力视图与受控 WorkflowPatch diff/replay 入口 |

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
- `make demo-check`：在 `quality-check` 之上再执行六条真实 API + 独立 Worker 子进程的黑盒 E2E
  （`backend/e2e`），其成功等价于 `docs/demo-acceptance.md` 六条均成功。

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
