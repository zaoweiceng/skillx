# NEXT_STEPS-026：接通内置 TraeX fallback，完成真实语义主流程

依据：[`REPLAY-025.md`](../REPLAY/REPLAY-025.md)、
[`design-compliance-review-026.md`](../review/design-compliance-review-026.md)、
[`code-review-026.md`](../review/code-review-026.md) 和 [`docs/`](../docs/) 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。目标不是继续增加 fixture，而是让用户在没有
独立 LLM endpoint 时，直接使用宿主 Agent（TraeX）完成语义理解、工作流选择和结构化生成。

## 1. 026 完成定义

下一版只有同时满足以下事实才能声明通过：

1. 未配置 `SKILLX_LLM_*` 时，应用自动发现并使用本机已登录 TraeX，不要求额外模型 endpoint；
2. 默认开启工作流时，TraeX 通过渐进披露选择工作流、提取同消息参数并交给 Workflow Runtime；
3. 关闭工作流时，同一个 TraeX 只作为通用 Agent，全 Turn 不披露或调用 `workflow.*`；
4. 天气真实运行先显示规范 JSON，再显示可读中文结果，历史与刷新均可恢复；
5. pre-025 既有 Demo 数据库获得新版系统工作流，且旧版本、历史 Run 和用户修改不被覆盖；
6. `make demo-check`、`make browser-ui-check`、真实 TraeX 主流程和升级路径全部通过。

## 2. P0：交付内置、受信的 TraeX Agent Runtime

### 2.1 明确 Runtime 选择规则

- [ ] 显式 `SKILLX_AGENT_*` 配置优先；未显式配置且 PATH 中存在受支持的 `traex` 时，选择内置
  `traex_builtin` Agent Runtime。
- [ ] `LLM Runtime` 未配置时，LLM/Presentation/抽取等严格结构化生成在 Run 启动前冻结为
  `agent_fallback`；执行中的 401、429、timeout 不切换 Runtime。
- [ ] 工作流语义选择本来就是 Agent 任务，应直接使用同一个 TraeX Runtime，不再因缺少独立 LLM endpoint
  返回 unavailable。
- [ ] 未安装 TraeX、未登录、认证过期、模型不可用、协议不兼容分别返回可操作错误码，例如
  `TRAE_NOT_INSTALLED`、`TRAE_AUTH_REQUIRED`、`TRAE_MODEL_UNAVAILABLE`、`AGENT_PROTOCOL_INCOMPATIBLE`。

### 2.2 建立原生协议 bridge

- [ ] 不把原生 `traex` 直接塞给当前 `TraeXSubprocessAdapter`；它们的命令行和 stdin/stdout 协议不同。
- [ ] 在仓库内实现平台维护的 bridge，内容 digest 自动受信；外部自定义 adapter 仍要求显式 digest 信任。
- [ ] 对“禁止工具的严格结构化生成”，可使用 `traex exec --ephemeral --json --sandbox read-only` 和临时
  `--output-schema` 文件；解析 JSONL 终态、dispatch、Usage、模型身份和错误。
- [ ] 对需要 `workflow.*` 的语义选择，优先使用 TraeX ACP/app-server/MCP 等原生双向接口；把 Tool 请求交给
  AgentHost Broker，并把 Host ToolResult 回送给同一次 Agent Turn。若选用 CLI + MCP，MCP server 必须是每次
  Run 受能力集合和生命周期约束的本地桥，不能暴露全局无限权工具。
- [ ] 映射并验证 `model.request.dispatched`、Tool callId 配对、strict output、Token、超时、取消和 stderr；
  AgentHost 继续是权限与审计边界。
- [ ] bridge 启动 TraeX 时使用最小权限、隔离工作目录和显式 allowed tools；不得加载与当前任务无关的插件/规则。

### 2.3 Settings 与认证恢复

- [ ] Settings 区分“已安装未登录”“认证过期”“已登录待探测”“可用”，提供准确的 `traex login` 操作提示。
- [ ] 登录动作由用户在本机完成，应用不得读取、回显或持久化 TraeX token。
- [ ] 真实探测继续要求随机 challenge、显式 dispatch、ToolResult round-trip、strict output 和正 Token Usage；
  内置 bridge 的代码受信不等于账号/模型已经可用。
- [ ] 当前测试机 token 已于 2026-08-03 过期；真实验收前先重新登录，再从 Settings 执行探测。

## 3. P0：用真实 TraeX 跑通核心对话

- [ ] 不配置 `SKILLX_LLM_ENDPOINT`、`SKILLX_AGENT_ENDPOINT` 或外部 API key，只使用已登录 TraeX。
- [ ] 新建对话默认开启工作流，输入“上海天气如何”；TraeX 必须先 L0 搜索、按需 L1 描述，选择天气精确 ref，
  绑定 `city=上海`，记录当前 message provenance，并执行 `run_full`。
- [ ] 输入包含多字段、数字、布尔、枚举、数组、URL 的普通自然语言，确认 Schema binding 正确；缺失或歧义才
  `ask_user`，无合适候选才 `continue_agent`。
- [ ] 关闭工作流后再次输入“上海天气如何”；轨迹中不得出现 `workflow.search/describe/run_full`，由 TraeX 通用
  Agent 回答或明确能力不足。重新开启后下一 Turn 恢复选择。
- [ ] 记录 provider=`traex`、真实 model、bridge version/digest、attempted/dispatched/completed、Token、Turn/Run、
  选择理由与 bindingSources；不记录 token/Secret。
- [ ] 故意制造 TraeX 认证过期、timeout 和 ToolResult 失败，确认失败接力从持久 checkpoint 继续，计时直到最终答复。

fixture 只保留协议、预算和故障回归，不得作为上述验收证据。

## 4. P1：升级既有系统 seed，而不是要求清库

- [ ] 为系统样例增加稳定 `seedKey` 和 `managedRevision`，区分平台管理样例与用户创建/修改的工作流。
- [ ] seed 定义 revision 增加时，为未被用户接管的样例创建新的不可变 WorkflowVersion，并更新 Component 默认版本；
  保留旧版本、digest、Run、Artifact 和审计 lineage。
- [ ] 用户已修改的草稿不得静默覆盖；应生成可审阅升级草稿或明确跳过并提示。
- [ ] 从 pre-025 数据库执行 `alembic upgrade head` 和 seed upgrade，逐个确认能力元数据、Presentation policy、
  默认版本与历史调用。
- [ ] 重复运行 seed upgrade 必须幂等，不重复发布相同 digest。

## 5. P1：为天气启用零 LLM 阅读结果

- [ ] 在天气工作流冻结 `presentationPolicy`：`enabled=true`、`audience=end_user`、`producer=template`。
- [ ] 使用顶层字段模板，例如 `{city}当前{condition}，温度{temperature}℃。`；维持 `zero_llm` 和 0 Token。
- [ ] RunDetail 先显示合法 JSON，再通过既有 Presentation 事件显示中文文本；两者分别复制。
- [ ] 组件调用历史和 Chat 历史显示 Presentation completed/failed 与最终文本；刷新后从数据库恢复。
- [ ] Presentation 失败不得修改成功 Run 或规范 JSON。内部 Fragment/组合中间步骤继续默认关闭。

## 6. P1：修正 Provider/Model 审计身份

- [ ] 从数据模型和事件中分离 `protocol`、`provider`、`model`、`runtimeType`、`adapterVersion`、
  `adapterDigest`，禁止对 `traex_managed/traex_builtin` 写死 `openai-compatible`。
- [ ] 受信 bridge 报告真实 provider/model；Host 校验并冻结到 probe、Turn、Run 和 ModelUsage。
- [ ] Settings、RunDetail 和 Chat 使用同一投影，能明确说明本轮是 Direct LLM 还是 TraeX fallback。
- [ ] 覆盖 TraeX 成功、认证失败、dispatch 前失败、dispatch 后 timeout 和不完整 Usage。

## 7. UI 与真实交付门禁

1. `make demo-check` 连续两次通过；后端、前端、空库迁移和 37 条独立进程 E2E 全部通过；
2. `make browser-ui-check` 连续两次通过，760、903、1024、1280、1440px 明暗主题无溢出、遮挡或不可点击控件；
3. 启动真实 API、Worker、Vite 和 Chromium，使用已登录 TraeX 完成“自动选择 → 参数绑定 → 工作流执行 →
   JSON → 阅读结果 → 历史回看”；
4. 同一环境验证工作流关闭路径、认证过期提示、失败接力和刷新恢复；console 0 error/warning；
5. 用 pre-025 数据库验证系统 seed 升级，无清库、无历史丢失、无用户草稿覆盖；
6. live SSE 与最终 DB seq 集合一致，Presentation 终态后才关闭；
7. 文档链和 `git archive HEAD` 干净归档均通过；
8. 新增 `REPLAY-026.md`，只记录对应提交或干净检出可复现的真实命令、runtime identity 和结果。

在“内置 TraeX fallback、真实自然语言主流程、天气双格式、既有数据升级”四项同时完成前，不得声明整体通过。
