# 自动任务、SkillX 与人工交互

聊天入口始终创建持久化 Task。外层 Agent 对目标是否完成负责；Workflow 的成功只是一份执行结果。Workflow 和 SkillX 页面仍可独立运行，不创建外层 Agent 决策轮次。

外层发现和调用使用统一能力契约：`catalog.capabilities` 和 `kind=capability`，调用参数为 `capabilityVersionId/interface/input`。Workflow 是能力的内部图实现，代码、LLM、Agent 是其他实现；外层无需选择执行器。Host 内部保留原 WorkflowVersion/CapabilityVersion 和 Invocation 的执行类型以兼容历史数据。公开 Invocation 将它们统一投影为 `kind=capability`。

## 执行与恢复

`POST /api/conversations/{id}/messages` 返回 HTTP 202 和 `taskId`。独立 Worker 领取 Task，结合已发布能力目录、执行证据和人工回答决定下一步。调用通过 Invocation 持久化，工作流另关联 Run。进程内通知不是正确性的前提：Worker 会扫描持久化就绪状态。

Task 默认限制 20 次决策、30 分钟执行预算，模型 Token 限制沿用 Agent 配置。等待人工回答不启动下一轮决策。独立调用可以与问题同时入队，等待回答不阻止已经派发的工作。单 Worker 内使用独立节点执行槽，受工作流 maxParallelNodes 和资源冲突检查共同约束；审批只阻塞依赖分支。Run 的执行期限也扣除全图等待人工的时段。CAS、租约及 `(scope, call_key)` 唯一约束保护派发；同一个幂等键不得改变输入。

取消会向关联 Run、Invocation 和待回答卡片传播。执行租约失效时，本机命令根据不可重复派发凭据、独立监督进程心跳和阶段结果重新接管。监督进程独立于 Worker，取消标记会终止子进程组；缺少可靠凭据的旧执行标记 `UNKNOWN_OUTCOME`，相同操作不会因换 key 被自动重放。

聊天与手动入口创建的工作流默认支持无显式 fallback 的受限恢复，保留已完成节点和副作用，最多三轮检查点。恢复只能调整剩余图；替代输出必须满足契约，不能虚构副作用完成。鉴权、配置、拒绝及未知结果不会盲目转交恢复。无效恢复决定会保留原始错误并收敛为失败，不会无限领取。

## 人工交互

外层决策用 `human.request` 提交信息或选择问题。问题的对象 Schema 支持字符串、枚举、数组及表单。推荐项只展示，不自动提交。SkillX 要求确认的副作用由 Host 生成操作授权卡片，不接受模型自行签发授权。

回答接口：`POST /api/human-interactions/{id}/answer`，携带 `revision`、`snapshotDigest`、`idempotencyKey` 和 `answer`。服务端验证 Schema、版本和操作快照，CAS 更新卡片并唤醒任务。取消、过期快照和重复提交都有确定行为。人工回答不进入永久权限配置。

工作流审批与恢复检查点由 Worker 幂等投影为持久化 HumanInteraction，Chat 与运行详情使用同一组件。卡片回答、原审批消费与检查点唤醒在同一事务完成；旧审批接口仍兼容，外部已处理的卡片会失效。

## SkillX 与手动执行

SkillX 注册将包复制到 `.versions/<digest>`，同一 SemVer 不允许更换字节。输入输出 Schema、实现、提示词及依赖锁随版本固定。

- Direct：子进程运行 Python callable；使用精简环境，不继承完整宿主凭据。声明的 Secret 通过 Host 注入，输出包含其明文时拒绝持久化。
- LLM：结构化模型调用，使用队列创建时的模型快照。
- Agent：AgentHost；只开放接口声明且已锁定的能力别名，并遵守 `agent.allowedCapabilities` 的进一步限制。
- Direct 依赖不能指向模型接口。依赖可绑定精确 SkillX 版本、已审核 MCP Tool Snapshot 或 Host Adapter。

SkillX 面板支持版本、接口、Schema 表单和 JSON 输入；创建持久化 Invocation 后展示历史、取消、结果与卡片。旧 `/capabilities/{id}/execute` 也已改为 HTTP 202，客户端应查询 Invocation，不能再期待同步输出。

Workflow 与 SkillX 面板共用版本、接口、表单、JSON 输入及 Invocation 历史。能力列表使用 `/capability-catalog`，两类面板统一调用 `/capability-versions/{id}/invocations`。旧 Run 创建 API 继续兼容。取消 Invocation 会取消对应 Run，面板直接展示其人工交互。

`capability_call` 用 `capabilityVersionId/interface/digest` 引用任意已发布能力；Host 转成 SkillX 节点或展开工作流，检查循环和最大 8 层深度，保留来源映射。`workflow_call` 仍兼容。已发布父版本不跟随子版本变化。

## 本机探索与证据

`code` 支持 Python/JavaScript；`install` 支持任务目录内 pip/npm 安装。Python 使用独立 venv，Node 使用本地 node_modules。包参数不接受命令选项、URL 或路径。记录源码、命令、退出码、日志、安装锁及进程信息；取消终止进程组。本机目录和 venv 不是 OS 安全沙箱。

查询入口：

- `GET /api/tasks/{id}`、`POST /api/tasks/{id}/cancel`
- `GET /api/tasks/{id}/events` 与 `/events/stream`；SSE 支持 Last-Event-ID
- `GET /api/tasks/{id}/session-export`
- `GET /api/conversations/{id}/session-export`
- `GET /api/tasks/{id}/artifacts`：源码/日志等文件的路径、大小及 SHA-256
- `GET /api/tasks/{id}/artifacts/{path}`：下载执行证据
- `POST /api/capability-versions/{id}/invocations`
- `GET /api/invocations/{id}`、`POST /api/invocations/{id}/cancel`、`GET /api/invocations/{id}/events[/stream]`

前端关闭不影响 Worker。当前 Chat 使用轮询投影，事件接口提供断线续传。记录默认不自动清理。大于 64 KiB 的消息/输入输出/事件字段存为按 SHA-256 寻址的 JSON Artifact，加载及下载时验证内容，只有归属于该任务的记录允许通过任务 Artifact 接口下载。Session 导出自动还原完整内容。

## 自动固化与维护边界

单次已有能力正常复用时不创建 LearningJob，与内部实现无关。新的探索结果和多次成功调用的组合触发后台复用分析，由学习器判断是否值得固化；可修复的工作流故障按故障指纹和版本去重，等待所属 Task 终结后维护。缺参数、拒绝或等待人工不作为实现缺陷。

自动发布验证器支持纯 Python SkillX、带锁定只读 SkillX/Host 依赖的 Direct，以及无工具依赖的稳定 LLM/Agent SkillX。验证原任务证据、至少两个不同输入、输入输出契约及完整工作流结果。Direct 在全新 venv 使用原始精确输入 fixture 验证，发布后通过 Broker 调用真实依赖；模型接口使用真实 Host/模型执行业务断言，不接受 fixture 作为发布证据。生成或验证错误最多反馈重试 3 次；失败保留候选，不激活。

已有 SkillX 的新组合支持模型/只读能力真实复验，带外部写操作的能力只回放同版本、同接口、同输入的真实调用 fixture，保留原确认要求；新输入缺少 fixture 不发布。新生成的任意第三方包代码、写操作包装及带工具依赖的模型 SkillX 仍保留候选，尚不自动发布。已有非 SkillX 工作流故障维护复用原 WorkflowPatch/Replay 门禁。维护按故障指纹与版本去重，同一 Task 的多个不同故障分别保留；fallback 成功不会丢失原故障上下文。

系统安装可以显式选择 `install scope=system`（npm/brew），Host 展示本次操作授权卡片；需要提权时必须显式 `elevated=true`，执行使用非交互 sudo，失败不会尝试获取密码。

任务执行区间进入审计，预算按区间并集合计，避免并行分支重复计时；人工等待不计主动执行。

## 数据库与验收

升级运行 `cd backend && uv run alembic upgrade head`，迁移至 `0004_learning_faults`（`0003_tasks` 创建任务表，`0004_learning_faults` 保留数据并移除过粗的任务级维护去重约束）。启动不会清空数据库。需要重建开发数据库时，先停 API/Worker，再显式运行 `uv run python -m app.reset_database --confirm-reset`。

验证包含后端任务/交互/嵌套/SkillX/候选验证测试、前端交互卡片测试、Chromium 面板与重启恢复测试，以及 opt-in 的真实 TraeX 测试。真实测试覆盖连续派发、人工回答、生成代码、SkillX 优先固化和不同输入复用；协议 fixture 仅用于确定性的 UI/故障回归。
