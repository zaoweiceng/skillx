# NEXT_STEPS-005：通配型固化、检查点接力与 005 审查修复计划

依据：[`REPLAY-004.md`](../REPLAY/REPLAY-004.md)、[`review/design-compliance-review-005.md`](../review/design-compliance-review-005.md)、[`review/code-review-005.md`](../review/code-review-005.md)。
目标：修复 005 代码审查问题，并让“首次 Agent 探索 → 部分/整体固化 → 任意类型任务复用 → 显式失败后 TraeX 从检查点接力 → 可复现时提出新版本草稿”在 Local Demo 中成立。
非目标：鉴权、RBAC、多租户、生产级沙箱、分布式部署。

## 1. 先确定系统不变量

后续实现必须同时满足以下约束；缺一项都不能把能力标为 `available`：

1. 网页只是 Action Adapter 的一种，固化、恢复、修订框架不得依赖 `Browser` 特判。
2. Agent 可以理解任务并提出“哪些节点确定化”的方案，但最终裁决必须由 Compiler 根据可参数化证据、能力锁、Schema、副作用和幂等性完成。
3. 未识别动作不能删除；要么保留为显式 Agent 节点，要么使草稿不可发布。
4. Direct/MCP/Browser 失败不得隐式升级为 Agent；fallback 必须写进已发布版本，并按 error code、风险和确认要求触发。
5. 恢复只消费持久化 checkpoint；不得依赖原 Worker 内存或重新猜测已完成步骤。
6. 已完成的非幂等副作用不得重放；`UNKNOWN_OUTCOME` 默认停下并请求人工确认。
7. Agent 只能提出 WorkflowPatch 草稿；旧 WorkflowVersion 永远不变，发布前必须 validate、replay、diff、人工确认。
8. 事件和 Artifact 不得保存 Secret 明文；大内容通过受控 Artifact 引用传递。

## 2. 最短实施顺序

顺序不能颠倒：先统一 Agent 信任边界和能力版本，再构建 Action IR；否则 fallback 和固化会复制当前两套不一致实现。

## 3. A：统一 Agent Host 与审计边界

### A-01 单一 AgentHost

- [ ] 提取唯一 `AgentHost.execute(request, event_sink, cancellation)`；ConversationRun 和 Workflow `AgentExecutor` 都调用它。
- [ ] 统一执行 `requested ∩ host_granted`、事件判别联合、callId 配对、step/Token/time/payload 上限、输出 Schema 和取消。
- [ ] 明确 requested capability 语义。本版本按“任务所需集合”处理：存在 denied 时执行前终止 `PERMISSION_DENIED`，不产生误导性成功。
- [ ] Workflow Agent 删除旧的宽松事件循环；相同畸形输入在两种入口得到相同错误码。

### A-02 事件最小化与 Artifact 生命周期

- [ ] `AgentEventValidator` 同时产生内部 runtime event 和公开 audit event；Tool arguments/result 只保存字段摘要、hash、大小和 Artifact id。
- [ ] canonical JSON 编码后按 UTF-8 bytes 限流；统一调用结构化 redactor，额外字段不得绕过脱敏。
- [ ] Conversation Agent 轨迹接入 Artifact 表、staging、hash、提交后 promote 与 reconcile；删除 API 中的绝对 `file://`。
- [ ] Worker 不再吞掉未知异常；记录 worker/run 上下文并形成可观察失败。
- [ ] Worker 启动和周期任务实际调用 Artifact reconcile。

完成条件：两种 Agent 入口共享同一组安全契约测试；数据库/API/日志中无法检索到测试 Secret；commit 后进程退出可由新 Worker 自动补落位。

## 4. B：修复 MCP 多 Tool 不可变版本

- [ ] 为“两 Tool Server，分类其中一个”先补失败测试。
- [ ] 分类修订克隆原版本全部 ToolSnapshot，只替换目标 Tool 的审核 policy；原版本所有行保持不变。
- [ ] ServerVersion digest 覆盖完整、稳定排序的 Tool 集合与 policy；current 只在完整版本构建成功后切换。
- [ ] 重复 discovery 不得把本地审核差异误判为远端 contract drift。优先把远端 descriptor digest 与本地 effect-review version 分离。
- [ ] 依赖扫描和发布 lock 继续锁定精确 snapshot/policy digest。

完成条件：分类前后旧 Workflow 可运行；新 current 拥有全部 Tool；重复 discovery 稳定；任一 Tool 的真实 contract 变化仍 fail closed。

## 5. C：建立通用 NormalizedAction IR

### C-01 轨迹契约

新增与运行框架无关的动作记录，至少包含：

| 字段 | 含义 |
|---|---|
| `actionId/kind` | 稳定动作 id；`mcp/skillx/browser/api/file/transform/llm/agent` 等 |
| `capabilityRef` | registry id、版本、digest、接口；不能只保存名称 |
| `inputBindings` | 常量、用户输入、前序输出、Artifact 引用及其 provenance |
| `outputSchema` | 动作的结构化输出契约 |
| `effect/idempotent` | 副作用、幂等和确认策略 |
| `replayability` | `deterministic/parameterizable/agent_only/unsupported` 及证据 |
| `observations` | 耗时、错误、结果摘要；大内容只存 Artifact 引用 |

- [ ] 扩展 TraeX 事件契约，支持 `browser.action`、artifact、LLM 和通用 capability action；内部结构转换为 IR，不直接绑定 TraeX 私有格式。
- [ ] Action 必须按原始依赖/顺序形成 DAG；缺事件、未闭合调用或输出无法关联时，分析失败而不是猜测。

### C-02 可扩展 Action Compiler Registry

- [ ] 为每个 action kind 注册编译器，返回 `compiled node | explicit agent node | rejected diagnostic`。
- [ ] MCP/SkillX 只有解析到已发布能力版本和 digest 才可确定化；API/File 必须先注册为受控能力，不能把任意命令固化进 config。
- [ ] Browser 固化为抽象 `browser` 能力与 Page/Action Contract；DSL 不暴露 Playwright/BUA 私有对象。
- [ ] Transform 只有可证明的纯函数/查表才确定化。删除“任意 dict 即 lookup” heuristic。
- [ ] LLM/Agent 节点保留最小输入、提示词模板、输出 Schema、Tool 白名单和预算。

### C-03 Agent 建议、Compiler 裁决

- [ ] TraeX 输出受 Schema 约束的 `DistillationProposal`：节点边界、变量候选、保留 Agent 的理由、预期成本。
- [ ] Compiler 逐项验证 proposal；拒绝无能力锁、错误副作用、无法证明参数来源或 Schema 不兼容的确定化建议。
- [ ] 混合轨迹必须保留所有必要动作，生成“确定前缀/中间节点 + 局部 Agent/LLM + 确定后缀”；不能因为存在一个确定节点就丢掉其它动作。
- [ ] `toolLocks` 改为结构化 capability locks；分析展示 direct/agent 节点数、未支持动作、风险和预计成本。

完成条件：真实动态结果 `{title, content}` 不会误编译为 lookup；多输出、混合动作、未知动作和依赖顺序都有正反测试。

## 6. D：通用 ExecutionCheckpoint 与显式 fallback

### D-01 DSL 与编译校验

把 `docs/schema.md` 已定义但未实现的 fallback 纳入 `WorkflowNode`：

```yaml
fallback:
  on: [DEPENDENCY_UNAVAILABLE, TIMEOUT, PAGE_DRIFT]
  target: recovery_agent
  requireUserApproval: false
```

- [ ] Compiler 校验 target 存在、只能处理声明的错误码、无 fallback 环、风险级别允许、Agent 有最小能力集合。
- [ ] `PERMISSION_DENIED/AUTHENTICATION_FAILED/CONTRACT_VIOLATION/UNKNOWN_OUTCOME` 默认不得自动交给 Agent；只有明确策略和必要审批才可进入恢复。
- [ ] `onError=continue` 保留兼容但不得承担 Agent 接力语义。

### D-02 标准检查点

新增持久化 `ExecutionCheckpoint`/`RecoveryRun`，至少保存：

- source run/workflowVersion/failed node run/attempt；
- 已完成节点的结构化输出与 Artifact ids；
- 失败节点输入、标准错误、重试历史和 outcome certainty；
- capability/version/digest locks 与最小安全上下文；
- 由已发布 DAG 计算出的 pending/remaining subgraph；
- 已提交副作用和 idempotency keys；Secret 只保留 handle。

- [ ] Checkpoint digest 固定内容，恢复前再次核验版本锁和 Artifact 完整性。
- [ ] `NodeResult.resume_token` 要么正式接入 checkpoint protocol，要么删除，禁止保留无效恢复暗示。

### D-03 TraeX 接力

- [ ] Recovery Agent 的 prompt 由平台模板和 checkpoint 摘要生成，不把整个数据库/原始私有轨迹拼进 prompt。
- [ ] 输出严格匹配 `RecoveryDecision`：`resume_remaining | provide_failed_output | request_user | abort`，并可附可选 `workflowPatchProposal`。
- [ ] Orchestrator 只接受 Schema 合法、权限允许且输出契约匹配的结果，再从 remaining subgraph 调度；已成功 NodeRun 不重建。
- [ ] 非幂等已完成动作的调用次数必须保持不变；无法证明 outcome 时进入人工确认。

完成条件：在 A、B 成功后让 C 网络读取重试耗尽，TraeX 能消费 A/B 的输出继续 C/D；恢复后 run 只有一个最终输出，A/B 的副作用计数不增加。

## 7. E：失败可复现性与受控 WorkflowPatch

- [ ] 定义稳定 `failureFingerprint`：workflowVersion、node id/type、capability lock、error code、稳定 contract/环境摘要；排除时间戳、随机 id 和原始 Secret。
- [ ] 区分 `transient/contract_drift/deterministic_input/platform_bug/unknown`，保存恢复是否成功及证据。
- [ ] 一次网络波动或不可复现失败不提版本修改；同版本、同 fingerprint 可重复复现且修复回放成功时，才允许生成 WorkflowPatch 草稿。
- [ ] 通用 WorkflowPatch 只表达节点/边/配置/契约的受限变更；不能携带宿主机路径、Secret 或扩大能力权限。
- [ ] 应用补丁总是创建 draft；展示 source version diff、锁变化、风险变化和 replay 结果；人工发布后才成为新版本。
- [ ] ContractPatch 作为 WorkflowPatch 的 Browser 子类型接入同一审核框架，避免两套发布逻辑。

完成条件：瞬态故障无 patch；可复现故障产生 draft；拒绝 patch 不影响旧版本；批准后生成新不可变版本且旧 run 可追溯。

## 8. F：两类通配验收，而不是网页单测

### F-01 网页内容总结样例

- [ ] 首次 TraeX 使用受控 Browser/BUA 能力访问本地动态站点、抽取正文并总结，生成 NormalizedAction。
- [ ] 固化结果至少为“确定性访问/抽取 + 显式 TraeX 总结”混合图；若页面与提示词完全固定且输出可直接确定，Compiler 可提出全确定化，但必须证明不会缓存过期数据。
- [ ] 第二次修改站点内容后运行，必须重新获取新正文，并把正文与参数化提示词传给 TraeX；结果包含新内容而不是首次 Artifact。
- [ ] 注入网络波动，先走幂等重试；耗尽后按显式 fallback 从 checkpoint 接力。

### F-02 非网页样例

- [ ] 选择一个 MCP/API/文件任务，例如“读取本地数据文件 → 结构化转换 → TraeX 归纳 → 生成 Artifact”。
- [ ] 使用与网页样例完全相同的 NormalizedAction、DistillationProposal、Compiler、Checkpoint、RecoveryDecision 和 WorkflowPatch 类型。
- [ ] 第二次替换输入文件/查询参数，确定节点重新读取新数据；只在显式 Agent 节点调用 TraeX。
- [ ] 在中间节点注入失败并证明从剩余子图继续，已完成文件/副作用不重复处理。

不得以两个彼此独立的 BrowserWorkflow/GenericWorkflow 恢复实现通过验收。

## 9. G：产品闭环与剩余工程问题

- [ ] conversationId 写入路由并可刷新恢复；assistant 消息按 Run 状态渲染，只有 succeeded 可固化。
- [ ] 候选卡片绑定 source user message，不再取全局最近 prompt。
- [ ] 编辑器增加 fallback 配置、checkpoint/recovery 运行视图、WorkflowPatch diff 与 replay 结果。
- [ ] 保持完整发布 busy guard；补并发应用补丁、重复恢复和取消恢复测试。
- [ ] 完成 MCP HTTP 增量流消费/Last-Event-ID、WorkflowVersion diff UI 和调色板拖放；这些不阻断通配恢复内核，但继续标记为 partial。

## 10. 默认门禁与出口

`make demo-check` 必须继续包含 Ruff、124+ 后端测试、迁移、前端 check/lint/test/build 和既有 9 个 E2E，并新增：

| 新门禁 | 必须证明 |
|---|---|
| Agent Host parity | 两入口对权限、预算、畸形事件、取消的终态一致 |
| MCP multi-tool revision | 分类后完整 current、旧版本不变、重复 discovery 稳定 |
| Mixed distillation | 真实能力锁、动作不丢、动态输入不读旧缓存 |
| Generic checkpoint recovery | 已完成节点不重放，剩余子图可接力，未知结果需确认 |
| Patch governance | 瞬态无 patch；可复现才生成 draft；旧版本不变 |
| Web + non-web E2E | 同一框架覆盖两类任务，网页仅是一个 Adapter |
| Audit/Artifact | Secret 不落事件；Agent Artifact 可自动 reconcile |

只有上述证据全部通过后，README 才能把“通用 Agent 探索与固化”和“通用失败接力”标为 `available`。在此之前应分别标为 `partial` 与 `not-implemented`。
