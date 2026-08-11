# 代码审查报告

审查日期：2026-08-03  
结论：静态检查、现有测试和前端构建均通过，但存在 1 个 P0 部署阻断、6 个 P1 正确性/安全问题和若干 P2 语义缺口。以下按优先级排序；行号以本次审查版本为准。

## P0 — 共享或联网部署缺少任何安全边界

证据：

- 所有读写、发布、审批、下载接口都没有 AuthContext 或授权依赖，例如 [`routes.py`](../backend/app/api/routes.py#L133-L159)、[`routes.py`](../backend/app/api/routes.py#L215-L252)、[`routes.py`](../backend/app/api/routes.py#L388-L456) 和 [`routes.py`](../backend/app/api/routes.py#L525-L536)。
- `owner_id` 和 `decided_by` 固定为 `local-user`，列表/详情也不按 owner 或 organization 过滤。
- 能力注册接受调用方提供的任意 `packagePath`，[`routes.py`](../backend/app/api/routes.py#L556-L585)；Workflow SkillX 节点也直接使用定义中的路径，[`executors.py`](../backend/app/runtime/executors.py#L149-L172)。配置中的 `capability_root` 没有被使用。
- Python 文件随后在 Worker 进程中动态导入执行，[`skillx.py`](../backend/app/runtime/skillx.py#L118-L126)、[`skillx.py`](../backend/app/runtime/skillx.py#L157-L173)。

影响：任何能访问 API 的主体都可读取所有 Run 输入输出/Artifact、审批或取消他人任务、发布工作流和注册能力。若攻击者能让一个 SkillX 包出现在宿主机可读路径中，还可使 Worker 加载其中代码。当前系统只能绑定 loopback 用作单用户开发演示，不能暴露到共享网络。

根因：数据模型预留了 owner 字段，但请求上下文、Repository 查询约束、发布策略和可信代码边界没有进入执行路径。

建议：在扩展功能前先引入统一 AuthContext；所有资源查询强制 owner/org scope；发布、能力注册、审批、Artifact 分别授权；SkillX 只接受 Registry 中审核通过且 digest 锁定的 CapabilityVersion，禁止 Workflow 直接携带宿主机路径；Worker 使用低权限隔离进程。

## P1 — Worker 租约会导致同一节点重复执行并允许过期 Worker 覆盖结果

证据：领取时只写一次 `heartbeat_at` 和 `lease_expires_at`，[`orchestrator.py`](../backend/app/application/orchestrator.py#L283-L316)；执行期间没有续租。恢复任务到期后把同一个 NodeRun 直接改回 `ready`，[`orchestrator.py`](../backend/app/application/orchestrator.py#L269-L281)。旧 Worker 完成时没有以 `worker_id + attempt + running` 做条件更新，[`orchestrator.py`](../backend/app/application/orchestrator.py#L318-L364)。

影响：任何执行超过默认 30 秒租约的节点都可能被第二个 Worker 再领取。对发送、写入、提交类节点会产生重复副作用；旧 Worker 还可能覆盖新 Worker 的状态或事件。

根因：租约被当作一次性时间戳，而不是需要续期且完成时必须校验所有权的并发协议。

建议：独立心跳协程续租；完成/失败使用条件 UPDATE 验证 `id/status/worker_id/attempt`；恢复生成新 attempt 或明确记录 takeover；过期 Worker 的结果只能记为迟到事件，不能改变事实状态。补双 Worker + 慢执行器集成测试。

## P1 — 浏览器契约未校验 URL 路径和当前身份

证据：[`browser.py`](../backend/app/runtime/browser.py#L42-L94) 只比较 hostname；`urlPattern` 含 `*` 时，任意同域路径都会返回可继续的 L1。`PageContract.page_identity` 与 `ObservedPage.identity` 从未比较。实测合约 `https://forms.example.org/apply*` 对 `https://forms.example.org/admin/delete` 且身份变化返回 `L1, can_continue=true`。

影响：一旦接入真实填写/提交 Adapter，同域错误页面或错误登录身份可能绕过安全漂移检查；这直接违反“页面 + 数据 + 身份”绑定原则。

根因：URL 模式被通过删除 `*` 后仅用于提取 hostname，并没有做完整 glob/正则匹配；身份字段只是建模，没有进入判定逻辑。

建议：编译 `urlPattern` 为锚定、安全的 URL matcher，严格比较 scheme/host/port/path/query 规则；按 PageContract 明确比较业务对象和身份，异常直接 L4；增加同域错路径、相似域、编码路径、重定向和身份切换测试。

## P1 — 取消会把 Run 提前标为 cancelled，但运行中节点仍继续执行

证据：取消只更新 `pending/ready/retry_waiting`，随后立刻把 Run 设为 `cancelled`，[`orchestrator.py`](../backend/app/application/orchestrator.py#L84-L96)。NodeExecutor 协议没有 `cancel`，Worker 也不轮询 `cancellation_requested_at`，[`executors.py`](../backend/app/runtime/executors.py#L27-L32)、[`orchestrator.py`](../backend/app/application/orchestrator.py#L318-L364)。SkillXContext 的 `check_cancelled()` 永远不做任何事，[`skillx.py`](../backend/app/runtime/skillx.py#L46-L51)。

影响：UI/API 会告诉用户任务已取消，但进程、网络请求或副作用仍可完成；之后 NodeRun 甚至会变成 succeeded，而 Run 保持 cancelled，形成自相矛盾的审计记录。

根因：取消只实现了 Run 状态写入，没有定义“请求取消 → Executor 停止 → 结果归类 → 最终状态”的协议。

建议：Run 先进入 `cancelling`；Worker/Executor 获取 cancellation token；子进程、HTTP、浏览器实现 cancel；只有所有活动节点终止或结果被归为 UNKNOWN_OUTCOME 后才能进入终态。

## P1 — 外部执行被包在数据库事务中，并发推进没有 Run 级串行化

证据：`run_once()` 在 `session.begin()` 内调用 `execute_claimed()`，[`orchestrator.py`](../backend/app/application/orchestrator.py#L415-L425)；后者在同一事务内等待整个 Executor，[`orchestrator.py`](../backend/app/application/orchestrator.py#L318-L364)。`advance_run()` 读取状态后批量创建下游 NodeRun，没有 Run 锁或幂等 upsert，[`orchestrator.py`](../backend/app/application/orchestrator.py#L80-L217)。事件 seq 也通过 `max(seq)+1` 计算，[`orchestrator.py`](../backend/app/application/orchestrator.py#L27-L45)。

影响：慢外部调用持有长事务；并行分支完成时可能遇到 SQLite lock、重复创建同一 NodeRun 或 RunEvent seq 唯一键冲突。设计中的 `maxParallelNodes > 1` 会把这个风险变成正常路径。

根因：执行、状态提交和调度推进没有拆成短事务；Orchestrator 没有单 Run 并发控制。

建议：领取短事务 → 事务外执行/持续续租 → CAS 短事务提交结果 → 对 Run 加锁或使用单一调度令牌推进；NodeRun 创建和事件序号用数据库原子机制。补并行 diamond DAG 压测。

## P1 — 发布版本没有锁定能力版本，历史运行不可复现

证据：编译器只要求 SkillX 节点出现 `config.interface`，[`compiler.py`](../backend/app/application/compiler.py#L139-L179)，没有解析 CapabilityVersion、包 digest、接口 Schema、权限或 Effect。发布后的定义仍保存 `config.package` 本地路径，执行时重新读取当前文件，[`executors.py`](../backend/app/runtime/executors.py#L155-L167)。

影响：不修改 WorkflowVersion，只替换路径下的 Manifest/代码就能改变历史版本行为。digest 只覆盖 Workflow JSON，不覆盖实际能力内容，因而“不可变版本”和回放承诺不成立。

根因：Capability Registry 数据模型与 Compiler/Executor 是两条断开的路径。

建议：Workflow 节点仅引用 `capabilityVersionId + interface`；发布阶段解析并固化能力 digest、Schema 和权限快照；执行阶段按已发布 CapabilityVersion 加载并验证 digest。

## P1 — 环形草稿会使前端编辑器进入无限循环

证据：DAG 预览的分层算法用 `while (changed)` 反复提高节点层级，没有访问计数或环检测，[`WorkflowGraph.tsx`](../frontend/src/components/WorkflowGraph.tsx#L30-L51)。编辑器在保存/后端校验之前就会对可解析 JSON 实时渲染，[`WorkflowEditorPage.tsx`](../frontend/src/pages/WorkflowEditorPage.tsx#L30-L35)、[`WorkflowEditorPage.tsx`](../frontend/src/pages/WorkflowEditorPage.tsx#L127-L140)。

影响：用户只要在编辑器中加入一条回边，页面主线程就会永久循环，无法再修改或保存草稿。

根因：显示层重新实现了拓扑分层，却假设输入已经通过后端 DAG 校验。

建议：前端使用有界 Kahn 算法；若处理节点数小于总数，立即展示 cycle diagnostic 并停止渲染；为循环、孤立节点和大图增加组件测试。

## P2 — DSL 声明了策略，但运行时静默忽略

- `onError: continue` 被 Pydantic 接受，[`workflow.py`](../backend/app/domain/workflow.py#L30-L43)，但任一最新失败节点都会直接失败 Run，[`orchestrator.py`](../backend/app/application/orchestrator.py#L119-L130)。
- `maxDurationSeconds`、`maxParallelNodes` 只存在模型中，[`workflow.py`](../backend/app/domain/workflow.py#L52-L65)，调度器从不读取。
- `partially_succeeded` 被视为终态，但没有生成路径；多个 Output 节点只取定义顺序中的最后一个成功输出。

影响：发布校验会接受平台实际上不支持的语义，用户配置看似生效、实际无效。

建议：未实现前由 Compiler 明确拒绝非默认值；实现后为每个策略写状态机测试，禁止“解析即支持”。

## P2 — 敏感数据会原样进入 API、SSE 和前端

证据：Node 完成事件直接保存完整输出，[`orchestrator.py`](../backend/app/application/orchestrator.py#L338-L348)；Run 详情返回完整输入、输出、Node 输入输出和审批快照，[`routes.py`](../backend/app/api/routes.py#L92-L119)、[`routes.py`](../backend/app/api/routes.py#L345-L373)；前端直接 JSON 展示，[`RunDetailPage.tsx`](../frontend/src/pages/RunDetailPage.tsx#L102-L123)。没有读取 Schema 的 `sensitive`/`writeOnly` 标记，也没有统一 redactor。

影响：Secret、个人数据或浏览器表单内容会持久化并推送给任何能访问接口的人。

建议：事件只保存摘要和 Artifact 引用；在持久化前统一递归脱敏；API 根据权限决定字段级展示；对错误、日志、Artifact 元数据复用同一 redactor。

## P2 — Artifact 同名写入会破坏历史完整性

证据：文件路径仅由 `run_id + basename` 组成并用 `write_bytes` 覆盖，[`executors.py`](../backend/app/runtime/executors.py#L258-L292)；数据库允许同一 Run 多条 Artifact 指向同一 URI，下载时不校验记录中的 SHA-256，[`routes.py`](../backend/app/api/routes.py#L525-L536)。

影响：两个节点使用同名 Artifact 时，第一条元数据的 hash/size 与实际下载内容不一致。

建议：物理对象名使用 artifact UUID 或内容 hash，展示名与存储键分离；下载前校验所有权和可选内容 hash。

## P2 — SkillX Host 把所有执行异常归类为契约错误

证据：[`skillx.py`](../backend/app/runtime/skillx.py#L157-L185) 除 PermissionError 外统一返回 `CONTRACT_VIOLATION`。依赖暂时不可用、认证失败、业务失败和执行器 bug 都丢失分类，也没有使用 Manifest retry/timeout/effect 配置。

影响：重试和告警决策失真，运维无法区分包输出不合约与依赖故障。

建议：定义 SDK 标准异常到错误码的明确映射；仅输出 Schema 不合法使用 CONTRACT_VIOLATION；未知异常使用 EXECUTION_FAILED 并保留服务端诊断 ID。

## P2 — 前端对所有组件固定标记“零 LLM”

证据：[`ComponentDetailPage.tsx`](../frontend/src/pages/ComponentDetailPage.tsx#L45-L52) 无条件显示该标签，没有检查节点类型、SkillX 传递依赖或版本元数据。

影响：包含 LLM/Agent 节点的组件会误导用户对成本、隐私和确定性的判断。

建议：由发布编译器计算并持久化 `executionClass`，前端只显示服务端签名后的分类，不在 UI 猜测。

## 测试缺口

当前后端 11 个测试、前端 2 个测试全部通过，但缺少以下能捕获上述问题的测试：

- 双 Worker 慢节点、租约续期、stale completion 和并行 diamond DAG。
- 运行中取消、超时、UNKNOWN_OUTCOME、副作用不重复。
- API 身份隔离、审批权限、Artifact 权限、敏感字段脱敏。
- 浏览器同域错路径、身份切换、授权摘要生成/消费、提交后验证。
- CapabilityVersion digest 锁定、包内容变更和历史回放。
- MCP/LLM/Agent Executor 契约与 E2E。
- 前端循环图、发布失败、组件执行类型标签、审批交互。

优先修复顺序：P0 安全边界 → Worker 租约/取消/并发 → 能力版本锁定 → Browser 合约 → 前端环处理 → 其余 P2。这个顺序先保证“不会越权、不会重复执行副作用、历史可审计”，再扩展产品能力。
