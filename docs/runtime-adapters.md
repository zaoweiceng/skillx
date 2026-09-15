# 可切换执行体与独立恢复

## 使用

首页进入对话。输入区选择 Agent，选择对后续轮次生效；已排队运行使用创建时保存的配置。
设置页分别管理模型服务和 Agent：新增、编辑、停用、设为默认、测试连接。

- **TraeX**：使用 PATH 中的本机 `traex` 和本机登录。平台不保存登录 token。
- **Pi**：使用项目内 SDK bridge，不依赖全局 `pi`。在设置页创建 OpenAI 兼容模型服务，再创建引用该服务的 Pi Agent。当前配置探测成功后可设为默认。
- **直接 LLM**：工作流选择 LLM 节点和模型服务，不启动 Agent。支持 Chat Completions 文本流，以及 JSON Schema、JSON Object、提示词约束三种结构化输出；输出仍经过本地 Schema 校验。

模型服务填写 Base URL、模型名、密钥环境变量名称、超时和结构化模式。密钥值应配置在 API/Worker 进程环境中，不填写到配置表单。
首次启动只在没有任何保存配置时导入环境变量配置；后续启动不覆盖用户选择。

## Pi 安装与检查

需要 Node.js 22.19 或更高版本：

```bash
cd bridges/pi
npm ci
npm run build
npm run probe
```

SDK 与依赖通过 `package-lock.json` 锁定。bridge 使用原生 ES Module，build 执行语法检查。
`probe` 只验证 SDK 可加载，**不表示模型服务可用**；真实模型探测使用设置页的“测试连接”。
Pi 每次执行使用内存会话、独立进程与目录；不加载项目/用户的上下文文件、技能、扩展或历史。
工具请求通过双向 NDJSON 交给 Host Broker；不开放绕过 Host 的内置执行工具。

## 工作流和恢复

节点属性可选择 LLM / Agent。Agent 节点可指定 TraeX 或 Pi 配置。
失败策略引用单独的 Agent 节点，该节点的配置决定自动恢复执行体。
发布时保存节点配置引用的解析结果；运行创建时复制非密钥配置快照，修改默认配置不会改变既有运行和版本。

自动恢复复用现有检查点、审批、租约和 CAS。每次执行使用新的工作目录与会话；上下文只包括失败节点输入、契约、相关上游结果和剩余子图。
必需上下文超过预算时明确失败，不截断契约后继续执行。

失败运行详情提供“交给独立 Agent 接力”：选择最新失败节点、Agent 和原因，创建独立恢复尝试。
该入口也适用于没有 fallback 的历史版本，不改写原版本。取消的运行不能接力；副作用结果未知时需先核实。
已失败 NodeRun 保持不变；替代输出、恢复 NodeRun 与事件记录接力过程。
当前运行修订和永久修复仍沿用 RecoveryAttempt、回放及审核草稿流程。

## 模块边界

- `runtime/agent_contracts.py`、`agent_registry.py`：通用 Agent 协议与适配器装配。
- `runtime/execution_contracts.py`：节点执行、取消和运行上下文契约。
- `application/execution_sources.py`、`run_events.py`：执行源解析、恢复修订与原子事件序号。
- `application/workflow_publishing.py`：发布与校验应用服务。
- `application/runtime_profiles.py`、`manual_handoff.py`：配置和显式接力服务。
- `infrastructure/models/`：按工作流、运行、对话、能力和恢复拆分 ORM；`database.py` 保留连接管理及兼容导出。
- `frontend/src/features/`：对话业务状态、编辑器属性、运行配置与功能样式。

原有大型路由及客户端入口保留兼容导出，避免调用方一次性迁移。
新增接口的传输类型通过 OpenAPI 生成；执行 `make api-types` 更新 `frontend/src/api/schema.d.ts`。

## 数据与验收

增量 Alembic 迁移 `0024` 添加运行配置和对话选择。原工作流定义、发布 digest 与历史审计数据不重写。
升级时在 `backend` 目录执行 `uv run alembic upgrade head`，然后重启 API 和 Worker。

验证命令：

```bash
make quality-check
make demo-check
make browser-ui-check
make traex-real-check
make traex-real-product-check
```

真实 TraeX 产品测试覆盖对话、工作流、取消、固化发布复用及检查点恢复。
Pi SDK 已使用本地 OpenAI 协议服务验证结构化输出、工具往返和上下文隔离。
本轮按用户要求暂不进行 Pi 和直接 LLM 的真实模型服务验收；本地协议测试不能替代该项验收。

### 本次验证（2026-09-15）

| 检查 | 结果 |
| --- | --- |
| `make quality-check` | 后端 376 项、前端 61 项通过；迁移、类型、lint、构建通过 |
| `make demo-check` | 黑盒 E2E 37 项通过 |
| `make browser-ui-check` | 浏览器流程 2 项通过，覆盖主题、响应式布局和事件同步 |
| `make traex-real-product-check` | 真实 TraeX 5 项通过，包含独立失败恢复 |
| Pi 本地 SDK 契约 | 4 项通过，包含工具、上下文隔离与取消清理 |
| Pi / LLM 真实服务 | 按本轮验收范围暂缓 |
