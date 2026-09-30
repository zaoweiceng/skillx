# 可切换执行体与独立恢复

## 使用

首页进入对话。输入区选择 Agent，选择对后续轮次生效；已排队运行使用创建时保存的配置。
设置页分别管理模型服务和 Agent：新增、编辑、停用、设为默认、测试连接。

- **TraeX**：使用 PATH 中的本机 `traex` 和本机登录。平台不保存登录 token。设置页可选择默认模型，对话输入区可覆盖本次会话的模型。列表来自 `traex models --json`，执行通过 `traex exec --model` 传入；加载失败会明确提示并保留原选择。
- **Pi**：使用项目内 SDK bridge，不依赖全局 `pi`。默认执行体，固定 SDK 0.85.1（修复 0.85.0 的 SDK 导入问题，避开后续接口变更）。设置页创建 OpenAI 兼容 Chat Completions 模型服务，即可供默认 Pi 使用。测试连接验证服务可用性。
- **直接 LLM**：工作流选择 LLM 节点和模型服务，不启动 Agent。支持 Chat Completions 文本流，以及 JSON Schema、JSON Object、提示词约束三种结构化输出；输出仍经过本地 Schema 校验。

模型服务手动填写 Base URL、API Key，支持添加多个模型，以及各自上下文窗口、最大输出 Token 和推理能力；服务设置默认模型和超时。编辑密钥留空保留原值。
多模态模型需在对应模型下勾选「支持图片」。对话支持粘贴或通过附件按钮添加 PNG、JPEG、WebP、GIF：最多 8 张、单张 5 MB、合计 20 MB。缩略图可删除和放大，图片也可单独发送；未声明图像能力的模型会在接受消息前明确拒绝。
图片以独立文件保存，历史消息保留预览引用；Pi 使用原生图像输入将完整图片送入模型，不把 Base64 混入文字提示词。后续轮次最多携带 8 张近期图片，优先保留当前消息附件。请求轨迹只保留图像格式和占位说明，省略庞大的二进制数据。
密钥写入权限 0600 的本地文件（目录 0700）；数据库、事件和执行快照只保存引用。API/Worker 共用 `SKILLX_RUNTIME_CREDENTIALS_PATH`。修改密钥创建新引用，既有排队任务保持原配置。历史环境变量引用继续兼容。
首次升级会迁移默认执行体到 Pi；后续启动保留用户选择。已有运行的快照不变。

## Pi 安装与检查

需要 Node.js 22.19 或更高版本：

```bash
cd bridges/pi
npm ci --ignore-scripts
npm run build
npm run probe
```

`./dev.sh` 默认安装 Pi；也可执行 `make pi-install`。
SDK 与依赖通过 `package-lock.json` 锁定。bridge 使用原生 ES Module，build 执行语法检查。
`probe` 只验证 SDK 可加载，**不表示模型服务可用**；真实模型探测使用设置页的“测试连接”。
Pi 每次执行使用内存会话、独立进程与目录；不加载项目/用户的上下文文件、技能、扩展或历史。
每轮模型请求在 SDK `onPayload` 边界采集，返回内容从 `message_end` 采集。请求/响应按 Unicode 字符分片持久化，界面重组完整轨迹；每轮记录模型、Token、缓存 Token、HTTP 状态和耗时。密钥和推理签名会脱敏；只展示服务实际返回的推理内容，不估算没有上报的用量或费用。
工具请求通过双向 NDJSON 交给 Host Broker；不开放绕过 Host 的内置执行工具。

## 工作流和恢复

节点属性可选择 LLM / Agent。Agent 节点可指定 TraeX 或 Pi 配置，使用该配置保存的模型。

对话切换 Agent 时清除 TraeX 专用模型覆盖；切换模型仅影响后续执行。运行保存包含模型的配置快照，排队或执行中的任务不随对话设置变化。
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

当前迁移链从 `0001_refactored` 开始，包含后续增量迁移。正常启动时 `./dev.sh`
执行 `alembic upgrade head`，保留当前迁移链的数据。新数据库也使用同一迁移命令。
仅更早且不兼容的旧数据库可能需要重建；先备份并停止 API/Worker，再自行选择
`./dev.sh --reset-db`。该命令会永久删除历史数据和已发布版本，不是正常升级步骤。

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

## 2026-09-16 界面与模型选择

前端采用 React 18 + Ant Design 6，统一黑白灰双主题。导航、对话、组件筛选、能力表格和配置弹窗采用一致的控件与间距；管理页面按路由加载。

数据库增量迁移 `0002_traex_model` 增加会话模型选择字段，重启 `./dev.sh` 自动升级，无需清空数据库。

验收：后端 382 项、前端 63 项、黑盒 E2E 37 项、浏览器 2 项通过。真实模型选择测试使用 `Doubao-Seed-2.1-Turbo` 成功完成任务，并验证修改会话选择不改变已创建运行的模型。

## 2026-09-30 Pi 默认接入

`./dev.sh` 安装并校验固定的 Pi SDK 0.85.1；增量迁移 `0008_pi_model_selection` 保留历史数据。
设置支持手动 API Key、同服务多个模型、默认模型、上下文窗口、最大输出 Token 和推理能力。
任务用量从持久化调用事件聚合，缺失返回或缺失用量的调用保持明确的不完整状态。

验证：后端全量 454 通过、2 跳过；Pi 配置/任务整链路包含服务与模型选择、密钥隔离与轮换、
中文长请求分片及缓存 Token 统计。前端 96 通过、1 项既有工作流页面测试失败（旧 mock 缺少 API）。
类型检查、lint、构建、增量迁移及桌面/手机表单浏览器检查通过。外部真实服务需配置用户 API Key。
