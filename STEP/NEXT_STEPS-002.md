# 下一步工作计划（002）

依据：[`review/design-compliance-review-002.md`](review/design-compliance-review-002.md) 与 [`review/code-review-002.md`](review/code-review-002.md)。  
目标：在单用户 Local Demo 范围内，修完正确性问题并完成 `docs/demo-acceptance.md` 定义的六条真实 E2E。  
非目标：鉴权、RBAC、多租户、生产级沙箱、分布式数据库。

## 1. 完成定义

本计划完成不以“代码存在”或“单元测试通过”为准，必须同时满足：

- 六条场景均通过真实 HTTP API、独立 API/Worker 进程和由 Alembic 创建的临时 SQLite。
- Browser 不出现假成功、重复副作用或授权跨页面/数据/身份复用。
- Conversation 能从 UI 完成候选、补参、确认、运行和查看结果。
- 真实 Agent 轨迹能生成草稿，人工发布后可用新输入复用。
- `make demo-check` 一条命令执行上述验收和现有质量门禁。

## 2. 优先级与依赖

```text
W1 运行正确性 ─┬─> W2 对话组件闭环 ─┐
               ├─> W3 Agent 固化闭环 ├─> W5 六场景黑盒验收
               └─> W4 MCP 契约完整性 ┘

W6 编辑器与可观测性：不阻塞六场景，完成 MVP 文档承诺所需
```

先修事实正确性，再补用户闭环，最后用黑盒验收锁定行为。可视化编辑器重要，但不应抢占主路径闭环。

## 3. W1：运行正确性（P0 工作包）

### W1-01 Browser L2 恢复语义

- [ ] 区分 Browser `drift_confirmation`、`submit_confirmation` 和 `unknown_outcome` 三类 Approval。
- [ ] L2 批准后将原 NodeRun 重新置为 `ready`，保存一次性 drift authorization，不得直接 succeeded。
- [ ] 重放时重新检查 URL、身份、业务对象、字段和 form values；任何变化使旧授权失效。
- [ ] 提交型节点在 L2 批准后仍必须进入提交确认、点击和成功断言。
- [ ] 下游只接收 Browser 契约输出，不接收通用 `{approved, snapshot}` 包装。
- [ ] 增加 L2 只读、L2 提交、二次漂移、拒绝四类测试。

完成条件：批准 L2 只代表“允许重新评估并继续”，绝不代表动作已经完成。

### W1-02 原子执行配额

- [ ] 将 `maxParallelNodes` 从“count 后 claim”改为 Run 级原子 slot/令牌。
- [ ] claim、完成、取消、超时、租约恢复都正确获取或释放 slot。
- [ ] 增加两个独立 session 同时 claim 的测试，循环运行以暴露竞争。
- [ ] 断言上限为 1 时数据库任意时刻最多一个 running NodeRun。

完成条件：节点 CAS 保证单节点所有权，Run slot 同时保证跨节点并发上限。

### W1-03 Artifact 提交一致性

- [ ] Executor 先写 staging；NodeRun 结果 CAS 成功后再原子落位并创建 Artifact 记录。
- [ ] CAS 失败、取消和超时清理 staging。
- [ ] 增加迟到 Agent/Browser 结果不留下孤儿文件的测试。

## 4. W2：对话调用组件闭环（E2E-4）

### W2-01 统一候选协议

- [ ] 定义消息状态：`component_candidate` → `collecting_inputs` → `confirmed` → `component_run`。
- [ ] 候选响应锁定 `componentId`、`workflowVersionId`、`inputSchema` 和候选依据。
- [ ] 新增显式 confirm endpoint，或扩展消息 API 接受 `componentId + inputs + candidateToken`。
- [ ] 服务端确认候选仍指向同一版本，避免 UI 展示后默认版本变化。

### W2-02 前端确认与补参

- [ ] 候选卡片提供“使用该组件”与“继续通用 Agent”两个动作。
- [ ] 使用现有 RJSF 渲染 inputSchema，支持必填校验和修改。
- [ ] 确认后显示启动状态、Run 链接和最终状态。
- [ ] 将当前静态 trace drawer 接入 ConversationRun/Run 的可审计事件摘要。

### W2-03 移除静态名称路由

- [ ] 不再用组件名称是否为输入子串作为唯一候选依据。
- [ ] 使用结构化意图/组件目录匹配结果，返回置信度和候选原因；无模型配置时明确降级为手动选择组件。
- [ ] 测试自然语言不包含组件原名时仍可获得正确候选。

完成条件：用户只通过浏览器 UI 就能从自然语言进入候选、补参、确认、Run 成功，不需要手工调用 API。

## 5. W3：真实 Agent 与固化闭环（E2E-5）

### W3-01 分离运行类型

- [ ] 将单次模型回答标记为 `llm_response`，不得伪装成 `agent_run`。
- [ ] 将外部轨迹导入标记为 `trajectory_import`，不得直接作为成功 Agent 运行事实。
- [ ] `agent_run` 只能由服务端 TraeX Adapter 创建和结束。

### W3-02 TraeX 主路径

- [ ] 配置一个仓库内可重复的 TraeX-compatible fixture/runtime，用于 Demo 验收。
- [ ] 记录 `agent.plan`、Tool 调用、step、Artifact、Token/步数和最终结构化输出。
- [ ] Host 校验每个 Tool 事件都属于 `allowedCapabilities`，发现越权立即失败。
- [ ] 验证取消、超时、无最终输出和非法 Schema。

### W3-03 固化、发布与复用

- [ ] Distillation 只读取服务端生成的成功轨迹，保留来源 run id 和事件摘要。
- [ ] 前端展示 direct 节点数、模型节点数、预计调用变化、缺失能力和风险 diagnostic。
- [ ] 用户打开草稿、修订、校验并人工发布；系统不得自动发布。
- [ ] 发布后用不同输入启动组件并验证输出，证明固化结果可复用。

### W3-04 Browser ContractPatch

- [ ] L3 只生成 ContractPatch 草稿和差异说明，不修改当前 WorkflowVersion/PageContract。
- [ ] ContractPatch 不得扩大 URL、身份、Tool allowlist 或权限范围。
- [ ] 人工审核发布新版本后才允许重放。

完成条件：真实 Agent 运行事实可以被审计、固化、人工发布并用新输入复用。

## 6. W4：MCP 契约完整性（E2E-2）

### W4-01 Tool Schema 演进

- [ ] discover 每次计算 Tool 契约 digest。
- [ ] 同一 ServerVersion 的同名 Tool digest 改变时拒绝静默覆盖，要求创建新 ServerVersion。
- [ ] 新版本发现后扫描依赖 WorkflowVersion，标出兼容或破坏性变化。
- [ ] 增加 input/output Schema 改变和 Tool 消失测试。

### W4-02 单一调用路径

- [ ] 抽取 MCP Call Service，统一执行输入校验、Gateway 调用、输出归一化、输出校验、Secret 回显阻断和错误映射。
- [ ] Workflow Executor 与 `/mcp-tools/{id}/test` 共用该服务。
- [ ] 测试接口返回与真实节点运行一致的结构化结果。

### W4-03 Streamable HTTP

- [ ] 实现 initialize、协议版本、session id 和 JSON/SSE 两类响应。
- [ ] 对本地 loopback HTTP fixture 完成 discover/call/timeout/error 测试。
- [ ] 完成前将 README 状态改为 “STDIO available / HTTP partial”。

## 7. W5：六场景黑盒验收（MVP 门禁）

### W5-01 验收 Harness

- [ ] 为每次验收创建临时目录、SQLite 和 Artifact 根目录。
- [ ] 只通过 `alembic upgrade head` 建库并 seed；禁止 `Base.metadata.create_all()`。
- [ ] 启动独立 API 和 Worker 子进程，等待 `/health/ready` 与 Worker heartbeat。
- [ ] 通过 HTTP/SSE 驱动场景，结束后优雅停止进程并保留失败日志。

### W5-02 六条场景

- [ ] **E2E-1** Form → Transform/SkillX Direct → Output；无模型配置、Token 为 0。
- [ ] **E2E-2** Form → 锁定 MCP ToolSnapshot → Output；STDIO 和 HTTP 至少各一条，模型调用为 0。
- [ ] **E2E-3** 固定 DAG → 单个 LLM → Output；只有显式节点产生 Token，非法输出被拒绝。
- [ ] **E2E-4** 浏览器 UI/API → 候选 → 补参 → 确认 → Workflow；Agent 不接管锁定节点。
- [ ] **E2E-5** TraeX Agent → Distillation → 草稿 → 人工发布 → 新输入复用。
- [ ] **E2E-6** Browser 检查 → 填写 → L2/提交审批 → 提交 → 验证；页面/数据/身份变化使授权失效。

### W5-03 门禁整合

- [ ] 将黑盒 harness 加入 `make demo-check`。
- [ ] 将当前静态/单元/进程内集成检查保留为较快的 `make quality-check`。
- [ ] 浏览器安装从每次检查中拆出，避免门禁依赖不必要的重复下载。
- [ ] 失败输出明确指出场景、Run、NodeRun、事件序列和子进程日志。

完成条件：`make demo-check` 的成功等价于 `docs/demo-acceptance.md` 六条均成功。

## 8. W6：编辑器与运行可观测性（MVP 文档剩余项）

- [ ] NodePalette、拖放 Canvas、连线、删除、缩放和 MiniMap。
- [ ] NodeInspector：输入映射、Schema、timeout、retry、Effect、fallback 和能力版本。
- [ ] VariablePicker 只显示可达上游输出，并给出 Schema 兼容提示。
- [ ] ValidationPanel、发布前风险摘要和版本差异。
- [ ] Run 详情展示节点耗时、Token、Agent steps、MCP 延迟、依赖版本和重试历史。
- [ ] 组件页补业务/技术双视图、副作用、确认要求和最近成功率。

完成条件：用户无需直接编辑 JSON 就能创建、理解、校验和发布 MVP 工作流。

## 9. 工程清理

- [ ] 将 Alembic 0001–0005 改为固定 migration 定义，不再导入当前 `Base.metadata`。
- [ ] `/health/ready` 动态读取 Alembic head，移除硬编码 `0005`。
- [ ] 更新 README 的 MCP HTTP、通用 Agent、E2E 状态，保证“available”只用于已完成主路径和黑盒验收的能力。
- [ ] 为对话页、工作流发布、Run 详情和 Browser 审批增加前端测试。

## 10. 建议迭代顺序

| 迭代 | 范围 | 出口条件 |
|---|---|---|
| 002-A | W1 | Browser 无假成功；并发配额可靠 |
| 002-B | W2 + W4 | 对话调用组件闭环；MCP 契约不漂移 |
| 002-C | W3 | 真实 Agent 可固化、发布、复用 |
| 002-D | W5 | 六条黑盒 E2E 全部进入 `make demo-check` |
| 002-E | W6 + 工程清理 | 满足剩余 MVP UI/可维护性承诺 |

最终验收只看可重复证据，不以任务勾选数量替代结果。
