# SkillX Workflow

SkillX Workflow 是一个契约驱动的可视化工作流平台。首次任务可由 Agent
探索，稳定任务发布为不可变工作流版本，并由独立 Worker 持久化执行。模型只在
显式的 LLM/Agent 节点介入。

## 能力状态

| 能力 | 状态 | 说明 |
|---|---|---|
| Workflow Compiler、SQLite 队列、Direct 节点、审批、SSE | `available` | 已接入 API/Worker/UI 主路径 |
| SkillX Direct | `available` | Registry 锁定 digest，独立子进程执行 |
| MCP HTTP/STDIO | `available` | Registry、Tool Snapshot、Executor 与本地 E2E 已接通 |
| LLM、Agent/TraeX | `available` | Executor 已接通；真实运行需要环境配置，测试使用 Fake Adapter |
| 轨迹固化 | `available` | 成功 ConversationRun 可生成可编辑草稿，不自动发布 |
| 浏览器契约与一次性授权 | `available` | Playwright 隔离 Context、重放审批、截图与 DOM 摘要 E2E 已接通 |
| 可视化拖放编辑器 | `not-configured` | 当前仍以 JSON DSL 编辑和有界 DAG 预览为主 |

`available` 表示已接入主路径并有自动化测试；`adapter-only` 只表示安全状态机或协议适配代码
存在；`not-configured` 不应在 UI 中标记为可运行能力。SkillX 的唯一规范源是
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
uv run ruff check app tests migrations
uv run pytest -q

cd ../frontend
npm run check
npm run lint
npm run test
npm run build
```

也可在仓库根目录执行 `make demo-check`。测试覆盖双 Worker 租约/CAS、取消、
超时、并行汇聚、SkillX 版本锁定、MCP、Hybrid/Agent、轨迹固化、浏览器一次性
授权、迁移和前端环图阻断。

## 安全边界

- Direct SkillX 只运行受信任包；Python venv 不是安全沙箱。
- SkillX 包路径会执行根目录约束，输入输出均通过 Draft 2020-12 校验。
- MCP 和 TraeX 子进程命令必须命中可执行文件白名单。
- 浏览器提交没有审批和有效快照时不得执行；点击后断言失败归类为
  `UNKNOWN_OUTCOME`，不得自动重试。
- Secret 仅允许保存引用；当前仓库不提供明文 Secret 存储。
