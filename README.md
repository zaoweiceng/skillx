# SkillX Workflow

SkillX Workflow 是一个契约驱动的可视化工作流平台。首次任务可由 Agent
探索，稳定任务发布为不可变工作流版本，并由独立 Worker 持久化执行。模型只在
显式的 LLM/Agent 节点介入；聊天由持久化外层 Task 自动选择并调用能力。

Workflow、代码、LLM 和 Agent 实现对外使用统一能力契约。外层 Agent 通过
`capabilityVersionId/interface/input` 调用，Host 决定内部执行方式；Workflow 与
SkillX 面板共用 Invocation 入口。工作流可通过 `capability_call` 组合锁定能力。

自动任务、人工交互、手动面板及当前实现边界见 [任务运行时说明](docs/task-runtime.md)。

当前迁移为 `0003_tasks`。已有 `0001_refactored` / `0002_traex_model` 数据库执行
`cd backend && uv run alembic upgrade head` 即可升级，无需清空。更早的旧迁移链不再支持。
已有本地数据库（包括停在 `0017` 或残留 `_alembic_tmp_*` 的数据库）请先停止 API/Worker，
在 `backend` 目录执行以下命令；**会删除全部历史数据和已发布版本**：

```bash
uv run python -m app.reset_database --confirm-reset
```

也可直接使用 `./dev.sh --reset-db` 完成重置、seed 和启动。全新数据库仍使用 `uv run alembic upgrade head`。

执行体配置、Pi 安装和失败接力见 [运行时适配器说明](docs/runtime-adapters.md)。

## 能力状态

| 能力 | 状态 | 说明 |
|---|---|---|
| Workflow Compiler、SQLite 队列、Direct 节点、审批、SSE | `available` | 已接入 API/Worker/UI 主路径 |
| SkillX Direct / LLM / Agent 与手动面板 | `available` | 不可变包与依赖锁、持久化 Invocation、结构化输入与人工授权 |
| 自动 Task、人工交互、工作流嵌套 | `available` | 自动派发及继续决策、持久化卡片、取消和审计；嵌套最大 8 层 |
| SkillX 优先自动固化 | `partial` | 纯 Python、锁定只读依赖和稳定模型能力可自动验证发布；已有写操作使用精确 fixture，新生成任意依赖/写操作包装仍保留候选 |
| MCP STDIO | `available` | Registry、Tool Snapshot、Executor 与本地 E2E 已接通 |
| MCP Streamable HTTP | `partial` | 已实现 initialize/会话 id/JSON+SSE、会话过期重连与 loopback E2E；仍缺流式增量消费与 Last-Event-ID 断点续传 |
| LLM 节点 | `available` | Executor 已接通；真实运行需环境配置，测试用 Fake Adapter |
| Agent/TraeX | `available` | 唯一 `AgentHost` 信任边界（ConversationRun 与 Workflow Agent 共用）：requested∩host_granted、判别联合事件契约、callId 配对、step/Token/time/payload 上限、输出 Schema 与取消；事件双投影，工具审计存摘要，Pi 模型请求/返回脱敏后分片持久化，Secret 不落库 |
| 通用对话调用组件 | `available` | 聊天自动创建 Task，按目标发现并派发锁定版本；补参使用持久化交互卡片，`/chat/:id` 可刷新恢复；独立 Agent 入口继续保留 |
| 通用 Agent 探索与固化 | `available` | 自然入口与显式入口共用 Local Demo 受控只读策略（Browser/lookup/file/summarize）和消息契约；首次 Browser 正文由 Host Broker 获取，刷新后仍可观察并固化 |
| 通用失败接力 | `available` | Host accepted PlanSnapshot 是 continuation execution source of truth；continuation 与 checkpoint CAS、Conversation assistant message 原子持久化；Chat 自动跟踪多代 lineage 并从可信 ConversationRun 回显最终 output/error；Browser、lookup、file 共用有界 ContinuationRequest |
| 失败可复现性与受控修订 | `available` | WorkflowVersion/PlanSnapshot 失败使用判别联合归因；Runtime/Replay 共用边选择与 Output Schema 终态，外部和副作用 replay 只接受捕获 fixture |
| Workflow 能力目录与临时组合 | `available` | 持久 disclosure session 审计 L0/L1/L2 与预算；Selection-5 严格互斥；FragmentPort、Plan source map、TTL 恢复和 active-version catalog 已接通 |
| MCP 副作用与幂等分类 | `available` | annotations 仅作提示且始终 `unreviewed`，分类创建**新的不可变快照**（旧快照/历史版本不变）；多 Tool Server 分类某 Tool 时克隆完整版本、分离远端 contract digest 与本地审核 digest，重复 discovery 稳定；发布保存完整 lock，运行前逐项核对 digest/effect/idempotent/timeout，漂移即 `MCP_CONTRACT_DRIFT` fail closed |
| 浏览器契约与一次性授权 | `available` | Playwright 隔离 Context、L2 漂移一次性授权重放、提交确认、L3 ContractPatch 草稿；Browser Read/Extract 作为只读 Adapter 用 `BrowserReadContract` 导航并抽取正文/结构化字段作为节点输出（URL 来自输入，第二次以新输入重新取数，不缓存首次结果）；人工审核补丁可应用到草稿（仅字段契约，L4 安全域不可改） |
| 通配黑盒 E2E | `available` | Runtime/Recovery/Catalog/Plan/Replay 纳入 `make demo-check`；`make browser-ui-check` 以真实 Vite/Chromium 验收自动任务、交互卡片→Worker 重启→回答恢复及两类手动面板 |
| 可视化拖放编辑器 | `available` | 结构化 state 为唯一真相（JSON 降级为导入/导出视图），调色板新增节点、画布连线/删除、环检测、可编辑 Inspector 与 VariablePicker 写入映射、fallback 配置、发布治理；运行详情提供检查点接力视图（fallback target/审批状态/唯一 Recovery NodeRun）与受控 WorkflowPatch eligibility/replay 入口 |

`available` 表示已接入主路径并有自动化测试；`partial` 表示主路径部分实现、仍有已知缺口；
`not-configured` 不应在 UI 中标记为可运行能力。SkillX 的唯一规范源是
[`docs/skillx.md`](docs/skillx.md)，版本为 `skillx.dev/v1alpha1`。

## 本地启动

要求 Python 3.12–3.13（提供 `python3` 命令）、`uv`、Node.js 22.19+、`npm`。在项目根目录执行：

```bash
# 日常启动：安装缺失依赖、迁移、seed，并启动 API / Worker / 前端
./dev.sh

# 从旧数据库切换到重构版本（永久清空旧数据，仅首次需要）
./dev.sh --reset-db
```

浏览器打开 `http://127.0.0.1:5173`，OpenAPI 位于 `http://127.0.0.1:8000/docs`。
按 `Ctrl+C` 同时停止全部服务；任意服务异常退出时，脚本也会停止其余进程。
日志保存在 `.dev/logs/{api,worker,frontend}.log`。后端代码修改后重新运行脚本；前端保留 Vite 热更新。
启动锁由操作系统管理，异常退出后可直接重试，无需删除 `.dev/lock`。若仍有服务运行，请先停止服务；不要删除 `.dev/supervisor.lock`，它的存在不代表锁被占用。

```bash
./dev.sh --skip-install       # 依赖已安装时跳过安装检查
./dev.sh --setup-only         # 仅初始化数据库和示例数据
API_PORT=8001 FRONTEND_PORT=5174 ./dev.sh  # 自定义端口
```

脚本仅监听本机。API 和 Worker 共用 `backend/.env` 及同一数据库配置。
需要单独管理服务时，可在 `backend` 目录使用 `uv run alembic upgrade head`、
`uv run python -m app.seed`、`uv run uvicorn app.main:app` 和
`uv run python -m app.worker --worker-id worker-local`；前端在 `frontend` 目录运行 `npm run dev`。

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

Local Demo 默认使用 **Pi Agent（SDK 0.85.1）**，`./dev.sh` 自动安装项目内 Pi 和迁移数据库。
打开设置 → 模型服务与 Agent，手动添加 Base URL、API Key 和多个模型。首个服务自动成为默认；
对话输入区可选择具体服务/模型，以及已声明支持的推理强度。

每轮 LLM 请求、返回内容、工具调用、Token（含缓存读取/写入）和耗时会持久化，
可在任务、Agent 和工作流的轨迹中展开查看。推理内容只记录服务实际返回的部分。
密钥保存在 `backend/.runtime-credentials/` 的本机受限文件中，接口不回传密钥，数据库和运行快照只保存引用。
API/Worker 必须使用同一个 `SKILLX_RUNTIME_CREDENTIALS_PATH`（默认在相同工作目录）。

首次升级会将默认执行体切换为 Pi；已创建运行仍使用原快照。TraeX 配置保留用于历史兼容。
显式 managed adapter 仍可按原协议配置：

```bash
export SKILLX_AGENT_RUNTIME_TYPE=traex_managed
export SKILLX_AGENT_COMMAND='["/path/to/traex-managed-adapter"]'
export SKILLX_AGENT_ALLOWED_EXECUTABLES='["traex-managed-adapter"]'
export SKILLX_AGENT_TRUSTED_ADAPTER_DIGESTS='["sha256:<adapter-digest>"]'
```

该 managed command 必须实现本仓库 Agent Host 的双向 NDJSON/ToolResult 协议；原生
`traex` 由内置 bridge 接入，不能作为 managed command 直接替代。

也可使用 `openai_sidecar`，其 endpoint/model/key 只属于 Agent Runtime；Direct LLM
通过独立的 `SKILLX_LLM_*` 配置，二者不共享 endpoint 或 key：

```bash
export SKILLX_AGENT_RUNTIME_TYPE=openai_sidecar
export SKILLX_AGENT_COMMAND='["python3","../examples/openai_agent_sidecar.py"]'
export SKILLX_AGENT_ALLOWED_EXECUTABLES='["python3"]'
export SKILLX_AGENT_ENDPOINT='https://provider.example/v1/chat/completions'
export SKILLX_AGENT_MODEL='provider-agent-model'
export SKILLX_AGENT_API_KEY_ENV='SKILLX_AGENT_API_KEY'
export SKILLX_AGENT_API_KEY='<provider-api-key>'
```

`fixture` 只用于协议和故障测试，并标记为 `protocolTestOnly`；它不会让 Chat 的自动工作流
选择进入可用状态。仓库内 `openai_agent_sidecar.py` 按内容 digest 自动受信；其他 adapter
必须单独配置可信 digest。配置完成后在 Settings 显式执行真实 Provider 探测，
探测成功且未过期时才开放语义选择。Secret 值不得写入工作流定义、事件或 Artifact。

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
- `make browser-ui-check`：启动全新数据库、API、Worker、Vite、动态站点和 Chromium，
  验证 fixture 协议 Agent（工作流关闭）、固化发布、显式组件复用、terminal output、
  成功/失败运行详情，以及真实 Browser 503 接力结果自动返回原 Conversation。真实语义
  自动选择需另行配置 `traex_managed` 或 `openai_sidecar`，fixture 不计作该项证据。
- `make traex-real-check`：执行真实 PATH `traex` 探测；未安装或登录失效只报告
  skipped/blocked，绝不以 fixture 替代。
- `make traex-real-product-check`：在全新 SQLite、独立 API/Worker 中验证真实 TraeX 的
  workflow-on、workflow-off、运行中取消、固化发布复用及检查点恢复；未显式执行时不计作通过。

测试覆盖双 Worker fencing/CAS、原子并发配额、取消、超时、并行汇聚、SkillX 版本锁定、
MCP STDIO/HTTP 与契约漂移、Hybrid/真实 TraeX Agent、轨迹固化与发布复用、浏览器一次性
授权与 L2 漂移重放、Artifact 暂存落位、迁移历史与前端环图阻断。

## 安全边界

- Direct SkillX 只运行受信任包；Python venv 不是安全沙箱。
- SkillX 包路径会执行根目录约束，输入输出均通过 Draft 2020-12 校验。
- MCP 和 TraeX 子进程命令必须命中可执行文件白名单。
- 浏览器提交没有审批和有效快照时不得执行；点击后断言失败归类为
  `UNKNOWN_OUTCOME`，不得自动重试。
- Secret 仅允许保存引用；当前仓库不提供明文 Secret 存储。

### 界面与模型选择

前端使用 React + Ant Design，支持浅色、深色和移动端布局。默认 Pi 可选择设置页中任一模型服务下的模型；一个服务支持多个模型及各自上下文窗口、最大输出 Token 和推理能力。切换仅影响后续执行，当前运行保持原模型。

更新代码后停止旧启动脚本，再运行 `./dev.sh`，脚本会安装新增前端依赖并自动执行数据库增量迁移。无需重置数据库。
