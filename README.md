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
| 通用 Agent 探索与固化 | `available` | 首次轨迹归一化为运行框架无关的 NormalizedAction IR，每个参数带显式 provenance（user_input/prior_output/artifact/constant）形成真实数据依赖 DAG；Agent 提出 `DistillationProposal`，Compiler 校验其覆盖每个动作恰好一次并按能力锁/Schema/副作用/参数证据最终裁决（`compiled|agent|rejected`）；所有编译器（含 Agent/LLM）从 bindings 生成节点间引用，混合图的前序输出真正传给后续节点；未知动作保留为显式 Agent 节点而非丢弃，仅 succeeded 运行可固化 |
| 通用失败接力 | `available` | 节点重试耗尽且命中已发布版本的显式 `fallback` 时冻结可执行 `ExecutionCheckpoint`（已完成输出值/失败输入/完整错误/剩余节点定义与边/能力锁/已提交副作用与幂等键），受限 Recovery Agent 通过含已完成值的 prompt 消费上下文并输出判别联合 `RecoveryDecision`（provide_substitution/retry_replanned/request_user/abort）；原失败 NodeRun 永不改写，恢复以 `OutputSubstitution` 续跑，已完成非幂等副作用不重放；恢复原子领取（`(failed_node_run_id, attempt)` 唯一约束 + CAS 租约）保证双 Worker 只调用一次 Agent；危险错误码先 checkpoint 进入 `waiting_approval`，批准走同一 Recovery claim、拒绝保留失败终态 |
| 失败可复现性与受控修订 | `available` | 每次失败按稳定 `failureFingerprint` 聚合为持久 `FailureObservation`，资格由后端 occurrence/classification 事实计算（不接受前端自报 reproducible）；瞬态故障不提补丁；WorkflowPatch 走字段级 DSL 白名单，能力锁/权限/模型/Secret/副作用/幂等键名规范化后递归拒绝；补丁在隔离环境用 source 安全输入**真实 replay** 到原失败点，通过后才创建 draft，旧版本不变，人工发布后才生效（ContractPatch 为其 Browser 子类型） |
| MCP 副作用与幂等分类 | `available` | annotations 仅作提示且始终 `unreviewed`，分类创建**新的不可变快照**（旧快照/历史版本不变）；多 Tool Server 分类某 Tool 时克隆完整版本、分离远端 contract digest 与本地审核 digest，重复 discovery 稳定；0009 迁移用运行时 canonical 算法从行字段重算 remote-only contract_digest，非空 0008 升级后首次 discovery 不伪漂移；发布保存完整 lock，运行前逐项核对 digest/effect/idempotent/timeout，漂移即 `MCP_CONTRACT_DRIFT` fail closed |
| 浏览器契约与一次性授权 | `available` | Playwright 隔离 Context、L2 漂移一次性授权重放、提交确认、L3 ContractPatch 草稿；Browser Read/Extract 作为只读 Adapter 用 `BrowserReadContract` 导航并抽取正文/结构化字段作为节点输出（URL 来自输入，第二次以新输入重新取数，不缓存首次结果）；人工审核补丁可应用到草稿（仅字段契约，L4 安全域不可改） |
| 两类通配黑盒 E2E | `available` | 真实 API + 独立 Worker + 本地表单站点子进程；E2E-5 证明非网页任务确定化提炼/成本降为零、E2E-6 走真实 Playwright 提交、E2E-7 证明检查点接力（唯一 Recovery NodeRun、原失败 attempt 不可变）且非幂等副作用只执行一次、E2E-8 真实 Playwright 读取动态网页正文交给 TraeX 且页面改变后输出依赖新正文、E2E-9 证明无失败观测/瞬态失败不生成补丁；网页与非网页共用同一 IR/Compiler/Checkpoint/RecoveryDecision/WorkflowPatch 管线，`make demo-check` 执行；失败留现场并打印日志 |
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
