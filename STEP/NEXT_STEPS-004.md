# 下一步工作计划（004）

依据：[`review/design-compliance-review-004.md`](../review/design-compliance-review-004.md) 与 [`review/code-review-004.md`](../review/code-review-004.md)。
目标：先建立可信的 Agent 长任务与 MCP 不可变契约，再完成真实轨迹提炼和最小可视化编辑闭环。
非目标：鉴权、RBAC、多租户、生产级沙箱和分布式部署。

## 1. 当前状态与优先级

真实 Browser 黑盒主路径已经完成，不应继续投入同类 happy-path 用例。当前最短路径是先修两个系统不变量：

1. **先持久化、后执行**：任何长任务在执行前必须有可恢复 handle；API 进程不能持有唯一事实。
2. **发布引用不可变**：WorkflowVersion 锁定的 ToolSnapshot 不能被分类接口原地修改。

| 工作包 | 优先级 | 004 出口条件 |
|---|---|---|
| A Agent 持久长任务 | blocking | POST 立即返回，Worker 执行，运行中事件可见，取消/重启可恢复 |
| B Agent Host 安全边界 | blocking | Tool 权限取 Host 交集，步数/Token/事件格式由 Host 强制 |
| C MCP 不可变分类 | blocking | 分类创建新快照，已发布版本运行前校验 id + digest |
| D 真实轨迹提炼 | blocking for full MVP | 至少一个 Tool action 被参数化为确定节点，新输入改变结果并降低 Agent 调用 |
| E 最小可视化编辑 | remaining | 不改 JSON 可新增、配置、连线、校验和发布 |
| F 工程一致性收尾 | remaining | E2E 失败留现场；Artifact 可恢复；重试策略统一 |

## 2. A：Conversation Agent 改为持久化长任务

### A-01 数据模型与状态机

- [ ] `ConversationRun` 增加 `queued_at/started_at/finished_at`、`lease_owner/lease_expires_at/heartbeat_at`、`cancellation_requested_at`、`attempt`。
- [ ] 明确状态集合：`queued → running → succeeded|failed|cancelled`；租约过期只允许安全接管，不能制造两个 TraeX 进程。
- [ ] 创建 run、`agent.run.created` 事件和关联 assistant message 在一个短事务中提交。
- [ ] 事件 seq 使用数据库原子递增，不由进程内局部变量维护。

### A-02 API 与 Worker

- [ ] `POST /conversations/{id}/agent-runs` 只创建任务并返回 `202 + conversationRunId`，不得等待 TraeX 终态。
- [ ] 独立 Agent Worker 领取租约、启动 TraeX、逐批提交事件与心跳；API 重启不影响已领取/待领取任务。
- [ ] 增加 `POST /conversation-runs/{id}/cancel`，把取消请求传播到子进程并最终记录 `cancelled`。
- [ ] 启动时恢复 queued 与过期 lease；对可能已执行副作用但结果未知的任务使用 `UNKNOWN_OUTCOME`，禁止盲目重跑。
- [ ] 对话 URL 或持久化选择保存 conversation id；刷新不得自动创建并切换到新会话。

### A-03 验收

- [ ] 使用慢速 TraeX fixture：POST 在 1 秒内返回，另一个 DB/API 连接能在任务完成前读到 `running` 和增量事件。
- [ ] 杀死 API 后 Worker 继续完成；杀死 Worker 后租约恢复并形成唯一终态。
- [ ] 取消测试证明子进程退出、状态为 cancelled、后续无 completed 事件。

完成条件：Agent Run 的存在、状态和事件不依赖 HTTP 连接、React state 或某个进程内存。

## 3. B：Agent Host 权限、配额与事件契约

### B-01 权限与配额

- [ ] 配置 Host 允许 Tool 集合；请求只能缩小范围，实际权限为 `requested ∩ host_granted`。
- [ ] 若请求包含未授予 Tool，创建前返回明确错误，或在 run 中记录 `PERMISSION_DENIED`；不得静默扩大。
- [ ] Host 独立统计并强制 `maxSteps/maxTotalTokens/maxDurationSeconds`，超限立即 cancel 子进程并记录稳定错误码。
- [ ] 工作目录、网络范围和可执行文件白名单作为 Host policy 保存，不从客户端 body 接受。

### B-02 事件规范

- [ ] 为 `agent.plan`、`agent.step.completed`、`tool.call.started/completed`、`agent.completed` 建立 Pydantic 判别联合模型。
- [ ] Tool 事件必须包含 `callId/tool`，started/completed 一一对应；终态只能出现一次。
- [ ] 非对象 JSON、未知 type、非法 Token、超大 payload 均 fail closed，并持久化 `AGENT_EVENT_INVALID`。
- [ ] 原始轨迹写入 Artifact，数据库事件只保存脱敏摘要和 Artifact 引用。

完成条件：不信任 TraeX 是否遵守命令行参数；Host 自己能证明权限和预算没有越界。

## 4. C：MCP 分类与版本锁定重构

### C-01 不可变 Snapshot

- [ ] 禁止 `classify_snapshot()` UPDATE 现有 Snapshot；分类创建新 ToolSnapshot 或新 ServerVersion。
- [ ] annotations 写入建议字段，`effect_review_status` 保持 `unreviewed`；只有本地 Registry 显式确认后才能 reviewed。
- [ ] 数据库/ORM 默认值改为 `effect=unknown`、`idempotent=false`、`unreviewed`，让保守不变量在所有创建路径成立。
- [ ] 已被 WorkflowVersion 引用的 Snapshot 永不修改或删除。

### C-02 发布与运行锁定

- [ ] WorkflowVersion 保存 snapshot id、digest、effect、idempotent、timeout 的完整 lock。
- [ ] `resolve_mcp_node()` 必须接收 WorkflowVersion lock 并逐项核对；不一致返回 `MCP_CONTRACT_DRIFT`，不调用 Tool。
- [ ] 重分类后提供“受影响版本/升级草稿”分析，不自动改历史版本或 Component 默认版本。
- [ ] 统一重试策略函数，Tool test、发布校验和 Orchestrator 共用。

### C-03 测试矩阵

- [ ] annotation 声称只读时仍需人工确认。
- [ ] 分类产生新 id，旧 Snapshot/digest/历史 Workflow 不变。
- [ ] 人工篡改 Snapshot 或 lock digest 时运行 fail closed。
- [ ] read/write/destructive × idempotent × timeout/unknown outcome 的重试结果一致。

完成条件：分类和服务升级不能改变任何已发布 WorkflowVersion 的执行语义。

## 5. D：从“重新运行 Agent”升级为真实轨迹提炼

### D-01 可提炼轨迹

- [ ] ConversationRun 保存规范化 action：能力 id/version/digest、参数模板、结果 Schema、effect、耗时、Token、Artifact 引用。
- [ ] Distillation 不再根据合成单 Agent step 工作，而是消费成功 action 序列。
- [ ] 依次判断动作能否删除、参数化、转 Transform、锁定 MCP、锁定 SkillX Direct、转单次 LLM；最后才保留受限 Agent。
- [ ] 分析结果展示 direct/model 节点数、缺失能力、风险、固化前后预计调用数与不可发布原因。

### D-02 最小真实样例

- [ ] 新增一个 TraeX fixture：首次探索调用确定性 Tool（例如读取/转换本地数据），输出取决于输入而非固定 reply 指令。
- [ ] 固化草稿至少包含一个锁定 MCP/Transform 节点，不是只有 Agent 节点。
- [ ] 用两个不同输入比较首次 Agent 与固化组件结果；断言结果随输入变化且固化后 Agent 调用数/Token 下降。
- [ ] 人工发布前允许编辑和校验，仍禁止自动发布。

完成条件：E2E-5 证明的是“编译复用”，不是“把原 prompt 再交给 Agent”。

## 6. E：最小可视化编辑闭环

### E-01 单一结构化状态

- [ ] 以 `WorkflowDefinition` 结构化 state 为唯一真相；JSON 仅作为高级导入/导出视图。
- [ ] NodePalette 支持 MVP 节点拖入；Canvas 支持移动、连线、删除、缩放和 MiniMap。
- [ ] 新连线立即做环检测与可达性校验，失败不写入 state。

### E-02 可编辑 Inspector 与 VariablePicker

- [ ] Inspector 编辑 input mapping、config、timeout、retry、sideEffect/idempotent 和能力版本引用。
- [ ] 先统一前后端表达式语法；数组只提供后端能解析的路径，或正式实现索引语法。
- [ ] VariablePicker 插入具体 mapping 字段，展示 source/target type 兼容结果，不再只是只读 chip。
- [ ] MCP/SkillX 选择器只允许锁定 reviewed、compatible 的不可变版本。

### E-03 发布治理

- [ ] 发布前展示 Compiler diagnostics、权限/Effect、依赖 lock、与上一版本 diff。
- [ ] 保留现有全流程 publishing guard；补保存冲突、校验失败、重复点击和创建组件失败测试。

完成条件：用户无需直接编辑 JSON，即可新增一个节点、配置输入、连线、校验并发布组件。

## 7. F：工程一致性收尾

### F-01 E2E Harness

- [ ] fixture 感知 pytest call 结果：失败调用 `stop_stack(failed=True)`，打印日志、保留路径；成功清理。
- [ ] 显式关闭父进程日志句柄；启动中途失败时清理已启动的子进程。
- [ ] 增加一个受控失败的 harness 自测，验证日志与目录策略，而不是依赖注释。

### F-02 Artifact 原子性

- [ ] 选择提交后 finalize/outbox 或 journal + GC，不再把“文件已落位、DB 未提交”当作成功状态。
- [ ] 增加 promote 后 commit 失败、文件缺失、孤儿文件三类恢复测试。
- [ ] 暂存目录和最终目录均有定期 GC，保留正在运行/待 finalize 的租约豁免。

### F-03 已知 remaining

- [ ] MCP Streamable HTTP 增量消费、Last-Event-ID/会话重连与超时清理。
- [ ] ContractPatch 的人工审核、应用到新草稿、校验和发布闭环。
- [ ] 节点间 JSON Schema 兼容检查与 WorkflowVersion diff UI。

## 8. 004 总验收矩阵

| 目标 | 必须证据 |
|---|---|
| Agent 长任务 | 202 handle、运行中 committed events、取消、API/Worker 中断恢复 |
| Agent 安全 | Host 权限交集、step/Token/time 强制、畸形事件 fail closed |
| MCP 不可变 | 分类产生新 Snapshot；旧 Workflow lock 不变；运行前 digest 校验 |
| 真实固化 | Tool action → 确定节点；不同输入不同结果；Agent/Token 调用下降 |
| 可视化编辑 | 不改 JSON 完成新增、映射、连线、校验、发布 |
| Browser | 保持现有真实 Playwright 单次提交和漂移失效回归 |
| 工程恢复 | E2E 失败留日志；Artifact commit 失败可恢复/可 GC |
| 总门禁 | `make demo-check` 通过，且新增中断/不可变性测试进入默认门禁 |

## 9. 建议迭代顺序

| 迭代 | 范围 | 出口条件 |
|---|---|---|
| 004-A | A | Agent 先持久化后执行，运行中可见并可取消 |
| 004-B | B | Host 独立强制权限、预算和事件格式 |
| 004-C | C | MCP 分类不再修改历史快照，运行锁定可验证 |
| 004-D | D | E2E-5 证明真实参数化与成本下降 |
| 004-E | E | 最小可视化编辑闭环完成 |
| 004-F | F | Harness、Artifact、重连与 Patch 收尾 |

每个迭代先补失败测试，再改实现；出口以可观察运行事实为准，不以 endpoint、表或测试函数名称为准。
