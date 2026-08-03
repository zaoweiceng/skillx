# 下一步工作计划（003）

依据：[`review/design-compliance-review-003.md`](review/design-compliance-review-003.md) 与 [`review/code-review-003.md`](review/code-review-003.md)。  
目标：补齐真实 Agent 固化与真实 Browser 黑盒验收，修复 MCP 副作用语义，再完成最小可视化编辑闭环。  
非目标：鉴权、RBAC、多租户、生产级沙箱和分布式部署。

## 1. 当前状态

002 计划中的运行内核、Browser L2、对话候选确认、MCP Schema 演进、迁移固定化、可观测性和黑盒 harness 已基本完成。003 不重复罗列已完成工作，只保留仍会改变“是否完成 MVP”判断的任务。

| 工作包 | 状态 | 003 出口条件 |
|---|---|---|
| A 真实 Agent 固化 | blocking | 对话启动 TraeX，生成 ConversationRun，固化发布后新输入复用 |
| B 最终验收真实性 | blocking | E2E-5/6 执行真实标题路径，README 与结果一致 |
| C MCP 副作用契约 | blocking | 未分类 Tool 不得被当作幂等读操作 |
| D 最小可视化编辑 | remaining | 用户不直接改 JSON 即可新增、配置、连线并发布节点 |
| E 工程收尾 | remaining | Token/Artifact/Patch 等边界语义准确且可恢复 |

## 2. A：真实 Agent → 固化 → 复用

### A-01 唯一 Agent Run Service

- [ ] 实现服务端 AgentRunService，负责启动 TraeX、状态持久化、取消、超时和事件归一化。
- [ ] 新增真实 endpoint，例如 `POST /conversations/{id}/agent-runs`；删除指向不存在路由的注释。
- [ ] 只有该 Service 可以创建 `ConversationRun(run_type="agent_run")`。
- [ ] ConversationRun 保存 input、结构化 output、规范化 steps、Tool 事件摘要、Token/步数和来源 Artifact。
- [ ] 服务端验证每个 Tool 事件属于 allowlist，最终输出符合 Schema。

### A-02 对话产品路径

- [ ] 未匹配组件时允许用户选择“单次 LLM 回答”或“启动通用 Agent”，语义明确分开。
- [ ] 候选卡片“继续通用 Agent”必须调用 Agent endpoint，不得只隐藏卡片。
- [ ] 右侧轨迹抽屉订阅 Agent 事件，展示 plan、Tool、step、Artifact 和终态。
- [ ] 刷新页面后仍能恢复 Agent 状态，不能依赖 React 本地 state。

### A-03 固化分析与发布

- [ ] Distillation 默认只接受 AgentRunService 生成的成功 `agent_run`；若允许 `llm_response` 固化，使用独立入口和文案。
- [ ] 分析页展示 direct/model 节点数、预计 Token 变化、缺失能力、风险和不可发布原因。
- [ ] 用户打开草稿、修改、校验并人工发布，不自动发布。
- [ ] 发布后使用不同输入启动新组件并验证输出。

完成条件：不存在手工插入 ConversationRun 的验收捷径；用户从对话开始即可完成“探索一次，固化复用”。

## 3. B：修正最终黑盒验收

### B-01 重写 E2E-5

- [ ] 通过 HTTP 创建 Conversation 并启动仓库内 TraeX-compatible Agent。
- [ ] 等待服务端生成成功 `ConversationRun(agent_run)`，验证 `agent.*` 和 Tool 事件。
- [ ] 调用 Distillation，确认只生成草稿、没有 WorkflowVersion。
- [ ] 通过 API 人工发布并创建/更新组件。
- [ ] 使用不同输入运行发布组件并验证输出。

### B-02 重写 E2E-6

- [ ] harness 启动本地表单 HTTP server，记录提交次数和提交数据。
- [ ] Browser Workflow 使用 `runtime="playwright"`，不接受 `observedPage` 或 `fakeSubmitResult`。
- [ ] 第一次执行完成检查/填写并等待提交审批；批准后重放、提交和成功断言。
- [ ] 断言 HTTP server 只收到一次提交。
- [ ] 增加页面字段、身份或 form data 在审批后变化时旧授权失效的黑盒分支。

### B-03 Harness 可靠性

- [ ] 动态分配空闲端口，支持并行测试。
- [ ] 测试失败时自动打印 API、Worker、TraeX、MCP 和表单站点日志。
- [ ] 成功时清理临时目录，失败时保留并输出路径。
- [ ] 明确验证 Worker 已进入轮询状态，而不只检查进程尚未退出。
- [ ] `make demo-check` 只在 B-01/B-02 完成后宣称等价于六条验收。

## 4. C：MCP Effect 与幂等性

### C-01 Registry 契约

- [ ] MCP ToolSnapshot 增加可信的 effect 分类来源和审核状态。
- [ ] MCP annotations 只作为提示；本地 Registry 允许显式设置/确认 `effect` 与 `idempotent`。
- [ ] 未分类 Tool 默认 `effect="unknown"`、`idempotent=false`，不能默认 `none/true`。
- [ ] Tool 契约 digest 纳入 effect/idempotent/timeout，变更生成新快照或新 ServerVersion。

### C-02 发布与运行约束

- [ ] 写 Tool 必须要求 Workflow 节点声明 sideEffect。
- [ ] 非幂等 Tool 的 `maxAttempts` 必须为 1；超时/租约丢失进入 UNKNOWN_OUTCOME。
- [ ] Tool test 页面明确展示 Effect、幂等性和是否允许自动重试。
- [ ] 增加创建、更新、删除类 Tool 的失败与恢复测试。

完成条件：任何未知或写 MCP Tool 都不会因为默认值被重复执行。

## 5. D：最小可视化编辑闭环

### D-01 可编辑画布

- [ ] NodePalette 支持 MVP 节点拖入 Canvas。
- [ ] 支持连线、删除、移动、缩放和 MiniMap；循环边立即阻止并显示 diagnostic。
- [ ] 画布修改与 JSON DSL 使用同一份结构化 state，禁止双向文本解析漂移。

### D-02 Inspector 与 VariablePicker

- [ ] Inspector 可编辑 input mapping、config、timeout、retry、effect 和版本引用。
- [ ] Input 节点变量来自 workflow `inputSchema`，生成 `${input.field}`，禁止 `${input.output}`。
- [ ] 其他节点变量来自锁定接口/outputSchema，支持嵌套对象和数组路径。
- [ ] VariablePicker 可插入映射字段，并给出类型兼容或不兼容提示，不只是只读 code chip。

### D-03 发布治理

- [ ] 发布前展示 Compiler diagnostic、Effect/权限摘要、依赖版本和与上一版本的差异。
- [ ] 发布按钮在整个 save/validate/publish/create-component 流程中保持 busy，阻止双击并发发布。
- [ ] 新增编辑、校验、发布的前端测试。

## 6. E：边界语义与恢复

- [ ] 将 `candidateToken` 改名为 `candidateSnapshotDigest`；若保留“一次性”语义，则持久化并原子消费 nonce。
- [ ] Artifact 采用 commit 后 finalize/outbox，或增加 final 文件与数据库记录双向 GC。
- [ ] `build_contract_patch()` 内部独立校验 URL pattern、身份和业务对象。
- [ ] Agent Tool 事件规范固定 event type 和字段；未知 Tool 事件格式默认拒绝，而不是绕过 allowlist 检查。
- [ ] README 在 A/B 完成前将“轨迹固化”和“六场景 E2E”调整为 `partial`。

## 7. 验收矩阵

| 目标 | 必须证据 |
|---|---|
| Agent 对话 | HTTP 启动、持久化状态、真实 TraeX fixture、事件与取消 |
| Agent 固化 | 同一 ConversationRun 经 Distillation、人工发布、新输入复用 |
| Browser | 独立 Worker 驱动真实 Playwright，本地站点仅收到一次提交 |
| MCP 写操作 | 未分类阻止发布，非幂等不重试，UNKNOWN_OUTCOME 可审计 |
| 可视化编辑 | 不编辑 JSON 完成新增节点、映射、连线、校验和发布 |
| 总门禁 | `make demo-check` 通过且测试实现与六条场景逐项一致 |

## 8. 建议迭代顺序

| 迭代 | 范围 | 出口条件 |
|---|---|---|
| 003-A | A-01、A-02 | 通用 Agent 主路径可达，产生真实 ConversationRun |
| 003-B | A-03、B-01 | Agent 固化 E2E 真正闭环 |
| 003-C | B-02、B-03 | Browser 黑盒使用真实 Playwright，门禁名实一致 |
| 003-D | C | MCP 写操作不会被默认重试 |
| 003-E | D、E | 达到设计文档剩余的编辑器和工程边界要求 |

最终完成条件仍是可重复的运行事实，不是测试函数名称或 README 状态标签。
