# SkillX Workflow 下一步工作计划

制定日期：2026-08-03  
依据：`review/design-compliance-review.md`、`review/code-review.md`  
目标版本：Local Demo MVP

## 1. 目标与边界

本计划的目标不是生产化，而是在本地单用户环境中完成设计文档的核心闭环：

```text
Agent/对话完成首次任务
→ 保存结构化执行轨迹
→ 生成可编辑工作流草稿
→ 人工校验并发布组件
→ 组件按锁定版本确定性复用
→ 必要时仅在显式 LLM/Agent 节点调用模型
```

同时修复审查中会影响本地 Demo 正确性的代码问题：重复执行、取消失真、并发冲突、版本不可复现、浏览器契约绕过、编辑器冻结和 Artifact 覆盖。

### 本轮明确不做

- 登录、AuthContext、RBAC、组织/租户、owner 数据隔离。
- 公网部署、多人协作、水平扩容、Kubernetes、高可用。
- 容器、namespace、seccomp 等生产级不可信代码沙箱。
- 企业级 Secret Provider；本地仅支持环境变量引用，并保证不写入工作流和事件。
- 多 Agent 协商、自动发布、通用 BPMN、任意循环。
- 完整运营指标、计费和生产告警。

### 本地 Demo 仍必须遵守的边界

- API 默认只监听 loopback；README 明确禁止直接暴露到共享网络。
- SkillX 只能从配置的 `capability_root` 注册和执行，Workflow 不接受任意宿主机路径。
- API Key 等敏感值只从本地环境变量读取，不进入数据库、事件、日志和 Artifact。
- 有副作用的节点必须经过审批；取消、超时或租约异常不能伪装成成功。
- 发布版本必须锁定 Workflow、Capability、MCP Tool Schema 和执行接口的实际版本。

## 2. 完成顺序

依赖关系如下，前一里程碑未验收，不开始依赖它的后续工作：

```text
M0 规范收敛
  ↓
M1 运行内核正确性
  ↓
M2 能力版本与 SkillX Host
  ↓
M3 MCP 零 LLM 闭环
  ↓
M4 LLM / Agent / 对话
  ↓
M5 轨迹固化

M1 ─→ M6 浏览器安全重放

M2 + M3 + M4 + M5 + M6
  ↓
M7 可视化编辑与组件体验
  ↓
M8 Demo 验收与文档收口
```

其中 M6 的确定性浏览器与审批链路只依赖 M1；局部 Agent 恢复（M6-09）还依赖 M4。

## 3. M0：统一规范与验收口径

目标：消除两套 SkillX Manifest 和“存在骨架即已实现”的口径冲突。

### 工作项

- [x] **M0-01** 指定 `docs/skillx.md` 为 SkillX 唯一规范源，当前版本为 `skillx.dev/v1alpha1`。
- [x] **M0-02** 修订 `docs/schema.md`、`docs/workflow-dev-doc.md` 中旧的 `skillx.platform/v1` 示例，改为引用规范，不再复制另一套结构。
- [x] **M0-03** 在 README 增加能力状态表：`available`、`adapter-only`、`not-configured`，避免把 adapter 类描述为已交付节点。
- [x] **M0-04** 冻结 Local Demo 的 Workflow DSL；未实现的字段由 Compiler 拒绝，不能静默接受。
- [x] **M0-05** 建立 `docs/demo-acceptance.md`，列出 M8 的六条端到端场景及预期事件。

### 验收标准

- 仓库内只有一种可执行 SkillX Manifest 结构。
- 示例包、文档示例和 Host 测试使用同一 `apiVersion`。
- README 的“已实现”与 ExecutorRegistry 的实际可用执行器一致。

## 4. M1：修复运行内核正确性

目标：在接入 MCP、Agent 和浏览器副作用前，保证节点不会因租约、取消和并发被错误执行。

### M1.1 拆分事务与执行

- [x] **M1-01** 将 Worker 流程拆为“短事务领取 → 事务外执行 → 短事务 CAS 提交 → 调度推进”。
- [x] **M1-02** 外部调用、SkillX、子进程和浏览器执行期间不得持有数据库事务。
- [x] **M1-03** NodeRun 完成/失败时按 `id + status=running + worker_id + attempt` 条件更新；CAS 失败的迟到结果只记录诊断，不覆盖事实状态。
- [x] **M1-04** `advance_run` 增加单 Run 串行化或等价幂等机制；并行分支不能重复创建下游 NodeRun。
- [x] **M1-05** RunEvent 序号改为原子分配，不再依赖并发不安全的 `max(seq)+1`。

### M1.2 租约、心跳和恢复

- [x] **M1-06** 执行期间定期续租，心跳间隔不超过租约的三分之一。
- [x] **M1-07** 恢复过期任务时生成明确的新 attempt/takeover 记录，保留旧 attempt 的未知结果状态。
- [x] **M1-08** Worker 崩溃恢复不得让非幂等副作用自动重试；此类任务进入 `waiting_user` 或 `UNKNOWN_OUTCOME`。

### M1.3 取消与超时

- [x] **M1-09** 增加 `cancelling` 状态；收到取消后先停止新节点调度，再通知活动 Executor。
- [x] **M1-10** NodeExecutor 增加取消协议；HTTP、MCP、TraeX、浏览器和 SkillXContext 使用统一 cancellation token。
- [x] **M1-11** 只有所有活动节点完成取消归类后，Run 才进入 `cancelled`；副作用结果不明确时返回 `UNKNOWN_OUTCOME`。
- [x] **M1-12** 实施 `maxDurationSeconds`；Run 超时后停止后续调度并触发相同的取消流程。

### M1.4 DSL 运行语义

- [x] **M1-13** 实施 `maxParallelNodes`，领取前按 Run 统计活动节点数。
- [x] **M1-14** 明确实现 `onError=continue`：失败节点保留错误，下游只能通过显式错误映射引用；最终状态按实际输出决定。
- [x] **M1-15** 明确多个 Output 节点的合并规则；不再用定义顺序中的“最后一个成功输出”。
- [x] **M1-16** 若 `partially_succeeded` 不进入 Demo，先从公开状态和 DSL 中移除；否则补齐唯一、可测试的生成规则。

### 测试与验收

- [ ] 双 Worker 执行慢节点，心跳期间不会二次领取。
- [ ] 模拟租约过期和旧 Worker 迟到，旧结果不能覆盖新 attempt。
- [ ] diamond DAG 两个并行节点同时完成，只创建一个下游 NodeRun，事件 seq 连续且唯一。
- [ ] 运行中取消会终止 Fake Executor；Run 和 NodeRun 状态一致。
- [ ] 非幂等节点发生 timeout/worker crash 时不自动重试。
- [ ] `maxParallelNodes`、`maxDurationSeconds`、`onError` 各有状态机测试。

## 5. M2：能力版本锁定与 SkillX Host

目标：让发布版本引用不可变能力，而不是运行时重新读取 Workflow 中的任意路径。

### 工作项

- [x] **M2-01** Workflow SkillX 节点改为引用 `capabilityVersionId + interface`，移除 `config.package`。
- [x] **M2-02** 注册 SkillX 时要求包位于 `capability_root` 下；计算包内容 digest，并将规范化 Manifest、Schema 快照和入口元数据保存到 CapabilityVersion。
- [x] **M2-03** 发布编译阶段解析 CapabilityVersion，校验接口存在、输入输出 Schema、版本状态、权限、Effect 和 digest。
- [x] **M2-04** Executor 只加载锁定的 CapabilityVersion；包内容 digest 变化时拒绝执行并提示重新注册新版本。
- [x] **M2-05** 完整校验 Manifest：未知字段、SemVer、默认接口、`SKILL.md` 名称一致性、接口必填项、Schema 根对象、环境引用。
- [x] **M2-06** 实现本地环境变量 Secret 引用和 deny-by-default capability client；只注入 Manifest 明确声明的句柄。
- [x] **M2-07** 将 SkillX 代码放到独立工作目录/子进程执行，至少隔离 Worker 事件循环和同步阻塞；支持超时、取消和资源回收。
- [x] **M2-08** 标准化错误映射：输入错误、依赖不可用、认证、权限、超时、输出契约和未分类执行错误分别处理。
- [x] **M2-09** 修复 Artifact 存储：使用 UUID/content hash 作为物理文件名，展示名与存储键分离；同名产物不能覆盖。

### 测试与验收

- [ ] 注册包外路径被拒绝；合法包注册后产生稳定 digest。
- [ ] 修改已注册包内容后，历史 WorkflowVersion 拒绝静默执行变更代码。
- [ ] Direct 输入/输出契约、超时、取消、错误映射和 Artifact 均有测试。
- [ ] `text-stats` 在无模型、无网络环境中通过 Host E2E。

## 6. M3：完成 MCP 零 LLM 闭环

目标：用一个真实本地 MCP Server 证明“已知任务不调用模型”。

### 工作项

- [x] **M3-01** 增加 MCPServer、MCPServerVersion、MCPToolSnapshot 数据模型和 Alembic 迁移。
- [x] **M3-02** 实现 MCP 注册、discover、health、Tool test API；Local Demo 支持 STDIO 和 Streamable HTTP。
- [x] **M3-03** 发现 Tool 时规范化名称、描述、输入/输出 Schema，并计算 digest。
- [x] **M3-04** 发布编译阶段锁定 ToolSnapshot，检查参数 Schema 和节点权限/副作用配置。
- [x] **M3-05** 实现 MCPNodeExecutor：参数校验、超时、取消、错误映射、调用事件和结果摘要。
- [x] **M3-06** 提供仓库内本地天气/查询类测试 MCP Server，不依赖公网或模型。
- [x] **M3-07** 新增“天气查询”或等价组件：表单输入 → MCP → Transform → Output，全流程 Token 为 0。

### 测试与验收

- [ ] STDIO/HTTP 的 discover 和 call 契约测试通过。
- [ ] Tool Schema 变化会生成新快照，不改变已发布版本。
- [ ] 未声明 Tool、非法参数和超时得到标准错误。
- [ ] 无模型 Key 时，零 LLM 示例从 UI 启动并成功完成。

## 7. M4：接入 LLM、Agent 和通用对话

目标：让模型只在显式节点或通用对话中运行，并产生可审计的动作事件。

### M4.1 LLM Executor

- [x] **M4-01** 定义 Local Model Profile：endpoint、model、环境变量 Key、timeout、token limit。
- [x] **M4-02** 实现 LLMNodeExecutor：无自主 Tool、结构化输出 Schema、Token/延迟指标、取消和错误映射。
- [x] **M4-03** 增加 Fake LLM Adapter，测试不依赖真实模型；真实配置作为手工 Demo 路径。
- [x] **M4-04** 增加一个 Hybrid 示例，证明只有指定 LLM 节点产生 Token。

### M4.2 Agent/TraeX Executor

- [x] **M4-05** 将现有 TraeXSubprocessAdapter 接入 AgentNodeExecutor。
- [x] **M4-06** 实际传递 Tool allowlist、最大步数、Token、超时和工作目录，不只停留在数据类。
- [x] **M4-07** 规范化并持久化 `agent.plan`、`agent.step.*`、`tool.call.*`、`artifact.created` 和 `agent.completed` 事件。
- [x] **M4-08** 保存原始 trajectory 为 Artifact，但 UI 只显示行动摘要，不显示私有推理。
- [x] **M4-09** 取消 Run 时终止 TraeX 子进程并正确归类状态。

### M4.3 对话主路径

- [x] **M4-10** 对话消息进入配置的 LLM/Agent Runtime，不再固定返回 `MODEL_NOT_CONFIGURED`；未配置时保留明确错误。
- [x] **M4-11** 支持已有组件候选、参数提取、缺失字段追问和用户确认启动。
- [x] **M4-12** 组件执行后由 Workflow Runtime 接管，Agent 不得继续规划已锁定节点。
- [ ] **M4-13** 前端展示消息、组件候选卡片、Run 链接和实时行动摘要。

### 测试与验收

- [ ] Fake LLM 输出不合 Schema 时节点失败，不能把自由文本传给下游。
- [ ] Agent 不能调用 allowlist 外 Tool，超过步数/Token/超时会停止。
- [ ] 对话调用已有组件后，组件内部零 LLM 节点不产生模型调用。

## 8. M5：完成轨迹固化为工作流

目标：把一次成功的 Agent Run 转为可编辑草稿，但绝不自动发布。

### 工作项

- [x] **M5-01** 增加 Distillation 数据模型、API 和状态事件。
- [x] **M5-02** 从标准轨迹中提取固定 Tool、输入来源、参数映射、结果 Schema、副作用和不稳定步骤。
- [x] **M5-03** 按“删除 → 参数化 → Transform → 固定 MCP → Direct SkillX → LLM → Agent”的顺序分类节点。
- [x] **M5-04** 输出 Workflow 草稿、Input/Output Schema、缺失能力、风险节点和预计 LLM 使用变化。
- [x] **M5-05** 草稿必须经过现有 Compiler；无法确定的映射作为 diagnostic，不生成伪确定性节点。
- [ ] **M5-06** 前端增加“固化为组件”入口、分析结果页和“打开草稿”动作；不得自动发布。

### 测试与验收

- [ ] 固定 Fake Agent 轨迹可稳定生成相同 digest 的草稿。
- [ ] Tool 参数来自用户输入时生成 `${input.*}`，来自上游时生成可达引用。
- [ ] 动态规划步骤保留为受限 Agent 节点，不被错误转换为 Direct。
- [ ] 用户修改、校验并发布草稿后，可以不同输入重复运行。

## 9. M6：完成浏览器安全重放

目标：固定测试站点在页面一致时零 LLM 重放；页面或身份变化时阻止错误提交。

### M6.1 先修契约判定

- [x] **M6-01** 将 URL pattern 编译为锚定 matcher，校验 scheme、host、port、path 和必要 query；禁止“同域即匹配”。
- [x] **M6-02** 比较 PageContract 与 ObservedPage 的身份、业务对象、字段、required/type/options、提交动作和安全状态。
- [x] **M6-03** 完整定义 L0-L4：L0/L1 可继续，L2 等待确认，L3 阻止当前版本，L4 立即终止且不得 Agent 恢复。

### M6.2 真实 Browser Executor

- [x] **M6-04** 引入 Playwright；每个 Run 创建独立 BrowserContext 和下载目录。
- [x] **M6-05** 实现 navigate、inspect、fill、click、assert、submit 和截图/DOM 摘要 Artifact。
- [x] **M6-06** 提交节点分成“填写但不提交 → 生成确认快照 → 用户批准 → 重新读取页面/数据/身份 → 校验 digest → 一次提交 → 验证结果”。
- [x] **M6-07** Approval 的授权由服务端生成并一次性消费；客户端回传 digest 不能替代服务端重新计算。
- [x] **M6-08** 页面状态或表单值变化使授权失效，回到等待确认；提交结果不明确时返回 `UNKNOWN_OUTCOME`，不得自动重试。
- [ ] **M6-09** L2/L3 的 Agent 恢复只生成 ContractPatch 草稿，不修改当前发布版本。

### 测试与验收

- [x] 增加本地浏览器测试站点，覆盖成功提交、新增必填字段、错路径、错身份和提交后未知结果。
- [x] `apply*` 不匹配 `/admin/delete`；身份切换判定为 L4。
- [x] 没有 Approval、digest 变化或 Approval 已消费时都不能提交。
- [x] 页面一致时固定表单可以零 LLM 完成并保存证据。

## 10. M7：补齐可视化编辑与组件体验

目标：用户无需直接编辑整段 JSON 即可构建、诊断和运行工作流。

### 工作项

- [x] **M7-01** 先修 DAG 预览循环：使用有界拓扑算法，检测到环时显示 diagnostic，绝不无限循环。
- [ ] **M7-02** 实现 NodePalette、可拖放 Canvas、连线、删除、缩放和 MiniMap。
- [ ] **M7-03** 实现 NodeInspector：输入映射、配置、Schema、timeout、retry、Effect 和 fallback。
- [ ] **M7-04** 实现 VariablePicker，只显示可达上游输出，并使用 Compiler 返回的 Schema 摘要做兼容提示。
- [ ] **M7-05** 实现 ValidationPanel、发布前风险摘要和版本差异。
- [x] **M7-06** 组件详情的 `executionClass` 由 Compiler 计算为 `zero_llm`、`hybrid` 或 `agent`；移除固定“零 LLM”标签。
- [ ] **M7-07** Run 详情展示节点耗时、Token、依赖版本、Artifact 和审批；图结构与运行状态分离。
- [x] **M7-08** 发布工作流时更新或显式创建组件，避免每次发布静默生成重复组件。

### 测试与验收

- [ ] 循环 JSON 不冻结页面并显示 `CYCLIC_GRAPH`。
- [ ] 用户能从空白画布创建、连接、配置、校验并发布一个确定性工作流。
- [ ] 不可达变量不能被选择；Schema 不兼容在发布前可见。
- [ ] 三类组件标签与实际节点/传递依赖一致。

## 11. M8：Demo 验收、数据保护与文档收口

目标：用可重复脚本证明完整闭环，而不是依赖手工观察。

### 工作项

- [x] **M8-01** 建立统一 redactor，依据 `writeOnly`、`x-skillx-sensitive` 和配置路径处理 RunEvent、错误、日志、审批与 Artifact 元数据。
- [x] **M8-02** SSE 只发送摘要和 Artifact 引用；Run 详情可在本地显示业务输入输出，但 Secret 永不回传。
- [x] **M8-03** `/health/ready` 检查数据库可写、Alembic 版本和 Artifact 目录可写。
- [x] **M8-04** 测试数据库通过 Alembic 从空库建立，不再仅依赖 `Base.metadata.create_all()`。
- [x] **M8-05** 增加一条 `make demo-check` 或等价脚本，执行后端检查、迁移、测试、前端检查、构建和 Demo E2E。
- [ ] **M8-06** 更新 README：实际能力、启动顺序、本地模型/MCP/TraeX/Browser 配置、限制和故障排查。

### 最终六条 E2E

- [ ] **E2E-1 确定性组件**：结构化输入 → Transform/SkillX Direct → Output，无模型成功。
- [ ] **E2E-2 MCP 组件**：结构化输入 → 固定 MCP Tool → Output，无模型成功且 Tool 版本锁定。
- [ ] **E2E-3 Hybrid 组件**：固定 DAG 中只有一个 LLM 节点产生 Token，输出符合 Schema。
- [ ] **E2E-4 对话调用组件**：自然语言 → 组件候选/补参 → Workflow 接管 → Run 成功。
- [ ] **E2E-5 轨迹固化**：首次 Agent Run → Distillation → 可编辑草稿 → 人工发布 → 新输入复用。
- [ ] **E2E-6 浏览器提交**：契约检查 → 填写 → 审批 → 一次性提交 → 结果验证；页面或身份变化会阻止提交。

## 12. 每个工作项的 Definition of Done

一个工作项只有同时满足以下条件才能勾选：

- 实现已接入真实 API/Worker/UI 主路径，不是孤立类或占位返回。
- 数据模型变更包含 Alembic migration，并验证空库升级。
- 错误使用标准错误码，用户可见消息能指出下一步动作。
- 至少有一条正常路径和一条关键失败路径自动化测试。
- 不破坏已有 WorkflowVersion、CapabilityVersion 或 SkillX fixture；若存在破坏性 DSL 变更，提供迁移说明。
- 后端 Ruff/pytest 与前端 typecheck/lint/test/build 全部通过。
- README 或相应 docs 与实际行为同步更新。

## 13. 全程质量门禁

每个里程碑完成时运行：

```bash
cd backend
.venv/bin/ruff check app tests migrations
.venv/bin/pytest -q

cd ../frontend
npm run check
npm run lint
npm run test
npm run build
```

还必须执行该里程碑新增的集成/E2E 测试。仅通过现有 11 个后端测试和 2 个前端测试，不视为里程碑完成。

## 14. 进度记录

| 里程碑 | 状态 | 完成条件 |
|---|---|---|
| M0 规范收敛 | completed | 唯一 SkillX 规范、README 状态准确 |
| M1 运行内核 | completed | 双 Worker、续租/CAS、取消、超时、策略测试通过 |
| M2 SkillX Host | completed | 能力锁定、独立执行、Secret 句柄和历史可复现均有测试 |
| M3 MCP | completed | STDIO discover/call、Tool Snapshot 和零 LLM E2E 通过 |
| M4 LLM/Agent/对话 | in_progress | 对话模型与轨迹 Artifact 已完成；真实 TraeX 命令仍需目标环境验证 |
| M5 轨迹固化 | in_progress | 数据模型、API、确定性草稿已完成；前端分析页待补 |
| M6 Browser | in_progress | 真实 Playwright 与一次性授权 E2E 已完成；ContractPatch 待补 |
| M7 编辑器与组件 | in_progress | 环图阻断、执行分类、组件更新已完成；拖放/Inspector 待补 |
| M8 最终验收 | in_progress | `make demo-check` 通过；剩余项依赖 M4-M7 |

### 2026-08-03 本轮实现证据

- 后端自动化测试增至 46 项，覆盖 Worker 租约/CAS、迟到结果、取消、超时、
  `maxParallelNodes`、`onError`、SkillX digest、MCP、LLM/Agent、对话接管、
  Distillation、Browser 授权和 Alembic 空库迁移。
- 前端增加环图有界布局测试，组件执行类别改为服务端发布结果。
- 根目录 `make demo-check` 串联后端静态检查/测试、空库迁移、前端
  typecheck/lint/test/build。
- Secret Store 只保存环境变量引用；真实值仅向 Manifest 白名单内的
  SkillXContext/MCP 子进程临时注入，回显值会被阻止持久化。
- Playwright 使用隔离 BrowserContext 执行检查、填写、审批重放、单次提交与结果
  断言，并保存截图和 DOM 摘要证据。
