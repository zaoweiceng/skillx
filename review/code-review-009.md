# 代码审查报告（009）

审查日期：2026-08-04

审查范围：提交 `cb93983` 相对 `95f3fbb` 的后端、前端、迁移、测试与
[`REPLAY-008.md`](../REPLAY/REPLAY-008.md)。

范围约束：Local Demo，不审查鉴权/RBAC；执行真实性、版本锁、恢复所有权、证据连续性和
可复现门禁属于功能正确性。

## 1. 结论

本轮发现 **1 个 P0、7 个 P1、4 个 P2**。P0 使当前后端在干净依赖环境无法导入；P1
集中在同一个根因：系统已经有很多正确的服务端原语，但 TraeX 与这些原语之间没有真实的
双向工具协议和连续任务状态机。

Recovery fencing、Fragment Port、PlanSnapshot 生命周期、共享 Runtime/Replay 分支逻辑
等 008 修复是真实代码，不应回退。下一步应修连接层和证据层，而不是重写这些底座。

## 2. P0 问题

### P0-1 — 未声明且未使用的 `pypolaris` 导入使质量门禁和后端启动同时失败

[`replay.py`](../backend/app/application/replay.py) 第 2 行导入
`pypolaris.safe_eval`，但该符号未使用，依赖也不在 `pyproject.toml` 或 `uv.lock`。

实际结果：

- `make demo-check` 在 Ruff 阶段因 import 顺序、unused import、超长行失败；
- 单独运行 Pytest 时，8 个导入 `routes.py` 的测试模块在收集阶段报
  `ModuleNotFoundError: No module named 'pypolaris'`；
- API/E2E 因应用模块无法导入而不可运行。

修复：删除该导入和误导性注释。若确实要更换表达式引擎，应先通过统一
`expressions.py` 适配、声明锁定依赖、迁移所有调用方并增加安全表达式测试，不能在 Replay
文件中旁路引入。

## 3. P1 问题

### P1-1 — 所谓 Browser Tool Broker 没有把真实结果返回 TraeX，首次探索仍消费 fixture

[`agent_host.py`](../backend/app/application/agent_host.py) 在收到
`browser.action.completed` 后执行 Playwright 并替换该事件的 result；但
[`adapters.py`](../backend/app/runtime/adapters.py) 启动 TraeX 时使用
`stdin=DEVNULL`，Host 没有任何通道把结果发回仍在运行的 Agent。

[`traex_stub.py`](../examples/traex_stub.py) 随后仍以固定“首次探索正文”发起总结、返回总结
和最终回答。E2E-8b 只断言被替换的 Browser action 轨迹等于网页，没有断言首次 Agent
输出等于真实网页。

影响：轨迹看起来像真实调用，但 Agent 的决策不是基于真实调用结果。网页示例不满足首次
探索，MCP/API/文件等通配工具更没有真实 Broker。

修复：定义双向 ToolRequest/ToolResult 协议。Agent 发出请求后暂停；Host 校验能力、执行
Adapter、持久化摘要并回送结果；Agent 收到结果后才允许下一 step。测试必须让页面内容
随机化，并同时断言工具结果、下一步参数和最终回答都消费该随机值。

### P1-2 — TraeX 没有 WorkflowToolBroker 工具循环，fragment/compose 仍被 Chat 主路径拒绝

[`routes.py`](../backend/app/api/routes.py) 的正常对话入口由服务端直接执行 search，然后调用
`_select_workflow_candidate()`；该 AgentHost request `max_steps=1`、能力列表为空，只看到
一次 L0 卡片。路由在 Selection 不是 `run_full/ask_user/continue_agent` 时明确返回 422，
提示 fragment/compose 需要先经 Broker。

独立的 `/workflow-tools/describe|slice|execute-selection` API 是平台原语，不是 TraeX 工具
循环。现有 Selection-5 测试只构造 Pydantic 对象；E2E 没有调用 `execute-selection`，也没有
TraeX 自主 fragment/compose 场景。

影响：008 的核心目标“Workflow 像 Skill 一样渐进暴露，由 TraeX 选择 full/局部/组合”
仍未实现。

修复：把 Registry/Compiler 暴露为 AgentHost 的受控工具集，并让一个持久 Agent run 在预算
内循环调用。Compiler diagnostics 必须作为 tool result 返回同一 Agent 修正，不能由外部
客户端接管。

### P1-3 — ask_user 只复用 session id，没有恢复原任务和选择状态

当上轮 assistant state 为 `ask_user` 时，Chat 会取回 Disclosure Session；但下一轮仍用用户
回答文本重新调用 `search_manifests()` 和新的单步选择。原始任务、候选、Selection、缺失
字段和已有 bindings 都没有持久化或合并。

例如“运行季度报告，缺少日期”得到问题后，用户回答“昨天”，系统会搜索“昨天”，而不是
在原 WorkflowRef 上绑定日期继续执行。

修复：持久化 PendingSelection（原任务、已选 refs、已披露契约、bindings、missingInputs、
Agent step），回答只补绑定并恢复同一 Agent/Compiler 状态；过期后再明确重新规划。

### P1-4 — 固化产物缺少目录语义、Fragment 与 fallback，无法自然进入后续三阶段

[`distillation.py`](../backend/app/application/distillation.py) 生成的 metadata 只有固定名称
`conversation-agent-draft`。它不生成 `capability.summary/useWhen/tags/examples`、
`callableFragments` 或 node fallback。

影响：

- 多个固化能力在 Catalog 中名称相同，未来自然语言任务难以搜索到正确版本；
- 即使图中有可复用局部，也没有渐进披露所需的 Fragment 描述；
- 固化后节点重试耗尽不会创建 Recovery Checkpoint，五阶段在固化→失败接力处断开。

修复：DistillationProposal/Compiler 必须同时产出受控 CapabilityDescriptor、来源 lineage、
可选 Fragment proposal 和 RecoveryPolicy proposal；平台校验后写入草稿，仍由人工发布。

### P1-5 — ConversationRun 失败没有通用接力，只有预置 Workflow fallback 能恢复

[`agent_run.py`](../backend/app/application/agent_run.py) 对 Agent Worker 租约过期直接写
`UNKNOWN_OUTCOME` 失败终态；普通 Agent 执行错误也直接终结。它不创建
ExecutionCheckpoint，不保存已完成动作/剩余计划，也不启动受限 Recovery Agent。

当前 Recovery E2E 使用预置“检查点接力演示”Workflow，与首次探索和固化 E2E 不是同一
产物。即使首次探索中途网络重试耗尽，也没有用户要求的“把上下文和剩余节点交给 TraeX
继续”。

修复：抽象统一 TaskCheckpoint，至少支持 AgentRun 与 WorkflowRun 两类 source；对已知
结果的只读动作可接力，对未知副作用必须 request_user。接力后仍应生成可固化的连续轨迹。

### P1-6 — 外部节点 Replay fixture 没有从真实 Run 捕获，Browser/MCP/Agent 工作流无法修订

[`replay.py`](../backend/app/application/replay.py) 对 Browser、MCP、SkillX、LLM、Agent 和
副作用节点只读取 `safe_inputs["_replayFixtures"][node_id]`。Patch 路由传入的是脱敏后的
`source_run.input_json`；正常 Workflow 输入 Schema 通常 `additionalProperties=false`，既不
允许调用者注入该字段，Runtime 也没有把真实节点输出写回 Run input。

现有 Patch E2E 只修一个纯 transform，未覆盖用户核心的网页/外部操作场景。

影响：最需要根据页面漂移、接口契约和外部依赖故障修订的工作流，baseline reproduction
会返回 `REPLAY_UNSUPPORTED`，无法生成修订草稿。

修复：运行时为可回放只读节点捕获有界、脱敏、Schema 校验后的 fixture Artifact，并在
FailureObservation/ReplayEvidence 中锁 digest。Replay 从证据存储读取，不从业务输入读取；
副作用只使用已审批的模拟器或明确拒绝。

### P1-7 — Conversation trajectory Artifact 无法通过统一下载/GC 生命周期使用

AgentRun 成功时创建的 Artifact 使用 `run_id=None`、`conversation_run_id=<id>`，URI 指向
`conversation-agents/<id>/trajectory.json`，但 metadata 没有 `storageKey`。
[`download_artifact()`](../backend/app/api/routes.py) 首先要求 `storageKey`，随后又固定拼接
`root / artifact.run_id / storage_key`，因此该 Artifact 必定返回 410，且现有 Run Artifact
reconcile/GC 只查询 `Artifact.run_id`。

影响：`REPLAY-008` 所称“复用统一下载、校验和 GC 生命周期”不成立；轨迹证据登记了数据库
行，却无法通过标准能力消费。

修复：统一 ArtifactStorageKey（显式 source kind/id + relative key），下载、promote、reconcile
和 GC 都按该键解析；增加 ConversationRun trajectory 下载、hash 篡改、补落位和清理测试。

## 4. P2 问题

### P2-1 — FailureObservation 的判别联合约束只存在于 Alembic，不存在于 ORM metadata

迁移 0014 创建 `ck_failure_observation_source`，但
[`FailureObservation`](../backend/app/infrastructure/database.py) 的 `__table_args__` 只有唯一
索引。使用 `Base.metadata.create_all()` 的单元测试库不会有该约束，迁移库和测试库行为不一致。

修复：在 ORM 声明同名 CheckConstraint，并在迁移测试中实际插入“二者皆空/皆非空”断言
数据库拒绝。

### P2-2 — Fragment/Manifest 列表推导存在重复循环，产生重复锁并放大数据

`build_manifest()` 的 approvals 推导重复 `for node in nodes`；
`create_private_fragment_snapshot()` 的 mcpLocks 推导也重复遍历同一个锁列表。前者最终只取
bool，后者会把每个锁复制 N 次并改变 Manifest digest、Plan locks 和响应体大小。

修复：去掉重复循环，并增加“两个节点/两个锁输出恰好两项”的断言。

### P2-3 — Disclosure response 审计在同 session 并发调用时可能串单

`consume_session_call()` 创建一条 `response_digest=pending` 的记录，但不把 call id 返回给
调用方；`record_disclosure_response()` 仅按 session + operation 查最新 pending。两个并发
describe 可能互相写入对方的响应 digest，审计无法证明 request/response 对应关系。

修复：消费预算时返回 `disclosure_call_id`，响应只能用该 id CAS `pending→digest`；超时或
异常写显式终态。

### P2-4 — 009 关键路径缺少同来源链测试，现有门禁名称高于覆盖事实

测试分别覆盖：Agent 固化复用、预置组件 Recovery、纯 transform Patch、外部客户端手写
Catalog/Plan。没有一条测试让同一固化 Workflow 经 Registry 发现、换输入复用、失败、接力、
生成修订、发布新版本并再次成功。

Selection-5 也只有模型校验，没有五个 API/AgentHost 黑盒；Plan Patch 路由没有测试命中。

修复：新增基于 lineage id 的闭环 E2E，测试必须只走公开 API + 独立 Worker，并对每个阶段
断言上一阶段的 source id/digest 被下一阶段引用。

## 5. 验证结果

| 检查 | 结果 |
|---|---|
| `make demo-check` | 失败：Ruff 3 errors |
| `uv run pytest -q` | 失败：8 collection errors，缺少 `pypolaris` |
| Alembic 0001→0014 | 通过 |
| 前端 typecheck/lint | 通过 |
| 前端 Vitest | 28 passed |
| 前端 production build | 通过 |
| 后端黑盒 E2E | 未运行，应用导入失败 |
| `git diff --check` | 通过 |

修复顺序与验收矩阵见 [`NEXT_STEPS-009.md`](../STEP/NEXT_STEPS-009.md)。
