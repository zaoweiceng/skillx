# SkillX Workflow

SkillX Workflow 是一个契约驱动的可视化工作流平台。首次任务可由 Agent
探索，稳定任务发布为不可变工作流版本，并由独立 Worker 持久化执行。模型只在
显式的 LLM/Agent 节点介入。

## 已实现

- Workflow DSL 静态检查：DAG、引用可达性、JSON Schema、危险重试和 SkillX
  接口锁定。
- SQLite WAL 持久化：工作流版本、组件、Run、NodeRun、事件、审批、Artifact
  和能力版本。
- 独立 Worker：数据库领取、租约恢复、重试、取消、条件分支与审批暂停。
- 节点执行器：Input、Transform、Condition、Approval、Output、Artifact、
  SkillX Direct 和浏览器页面契约检查。
- REST API、SSE 事件补发、JSON Schema 表单、DAG、运行详情、审批和工作流编辑器。
- SkillX Python Callable Host、MCP Streamable HTTP/STDIO Adapter 和 TraeX
  子进程 Adapter。
- 示例 SkillX 包 `capabilities/skillx/text-stats` 和无模型示例组件。

LLM、真实 TraeX 命令、MCP Server、Secret Provider 和浏览器提交运行时需要按
部署环境配置。未配置时节点会返回明确错误，不会隐式切换到 Agent。

## 本地启动

要求 Python 3.12+、`uv`、Node.js 20+。

```bash
cd backend
uv sync --extra dev
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

访问 `http://localhost:5173`。OpenAPI 位于
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

测试覆盖工作流编译、循环与危险重试、确定性执行、审批暂停、页面漂移、
一次性授权摘要、SkillX 契约和幂等种子初始化。

## 安全边界

- Direct SkillX 只运行受信任包；Python venv 不是安全沙箱。
- SkillX 包路径会执行根目录约束，输入输出均通过 Draft 2020-12 校验。
- MCP 和 TraeX 子进程命令必须命中可执行文件白名单。
- 浏览器提交没有审批和有效快照时不得执行。
- Secret 仅允许保存引用；当前仓库不提供明文 Secret 存储。
