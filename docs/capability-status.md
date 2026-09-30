# 能力状态

此页记录实现范围，不代表当前环境已完成配置，也不替代本次测试结果。


| 能力 | 状态 | 说明 |
|---|---|---|
| Workflow Compiler、SQLite 队列、Direct 节点、审批、SSE | `available` | 已接入 API/Worker/UI 主路径 |
| SkillX Direct / LLM / Agent 与手动面板 | `available` | 不可变包与依赖锁、持久化 Invocation、结构化输入与人工授权 |
| 自动 Task、人工交互、工作流嵌套 | `available` | 自动派发及继续决策、持久化卡片、取消和审计；嵌套最大 8 层 |
| SkillX 优先自动固化 | `partial` | 纯 Python、锁定只读依赖和稳定模型能力可自动验证发布；已有写操作使用精确 fixture，新生成任意依赖/写操作包装仍保留候选 |
| MCP STDIO | `available` | Registry、Tool Snapshot、Executor 与本地 E2E 已接通 |
| MCP Streamable HTTP | `partial` | 已实现 initialize/会话 id/JSON+SSE、会话过期重连与 loopback E2E；仍缺流式增量消费与 Last-Event-ID 断点续传 |
| LLM 节点 | `available` | Executor 已接通；真实运行需环境配置，测试用 Fake Adapter |
| Agent/TraeX | `available` | 唯一 `AgentHost` 信任边界（ConversationRun 与 Workflow Agent 共用）：requested∩host_granted、判别联合事件契约、callId 配对、step/Token/time/payload 上限、输出 Schema 与取消；事件双投影，工具审计存摘要，Pi 模型请求/返回脱敏后分片持久化，Secret 不落库 |
| 通用对话调用组件 | `available` | 聊天自动创建 Task，按目标发现并派发锁定版本；补参使用持久化交互卡片，`/chat/:id` 可刷新恢复；独立 Agent 入口继续保留 |
| 通用 Agent 探索与固化 | `available` | 自然入口与显式入口共用 Local Demo 受控只读策略（Browser/lookup/file/summarize）和消息契约；首次 Browser 正文由 Host Broker 获取，刷新后仍可观察并固化 |
| 通用失败接力 | `available` | Host accepted PlanSnapshot 是 continuation execution source of truth；continuation 与 checkpoint CAS、Conversation assistant message 原子持久化；Chat 自动跟踪多代 lineage 并从可信 ConversationRun 回显最终 output/error；Browser、lookup、file 共用有界 ContinuationRequest |
| 失败可复现性与受控修订 | `available` | WorkflowVersion/PlanSnapshot 失败使用判别联合归因；Runtime/Replay 共用边选择与 Output Schema 终态，外部和副作用 replay 只接受捕获 fixture |
| Workflow 能力目录与临时组合 | `available` | 持久 disclosure session 审计 L0/L1/L2 与预算；Selection-5 严格互斥；FragmentPort、Plan source map、TTL 恢复和 active-version catalog 已接通 |
| MCP 副作用与幂等分类 | `available` | annotations 仅作提示且始终 `unreviewed`，分类创建**新的不可变快照**（旧快照/历史版本不变）；多 Tool Server 分类某 Tool 时克隆完整版本、分离远端 contract digest 与本地审核 digest，重复 discovery 稳定；发布保存完整 lock，运行前逐项核对 digest/effect/idempotent/timeout，漂移即 `MCP_CONTRACT_DRIFT` fail closed |
| 浏览器契约与一次性授权 | `available` | Playwright 隔离 Context、L2 漂移一次性授权重放、提交确认、L3 ContractPatch 草稿；Browser Read/Extract 作为只读 Adapter 用 `BrowserReadContract` 导航并抽取正文/结构化字段作为节点输出（URL 来自输入，第二次以新输入重新取数，不缓存首次结果）；人工审核补丁可应用到草稿（仅字段契约，L4 安全域不可改） |
| 通配黑盒 E2E | `available` | Runtime/Recovery/Catalog/Plan/Replay 纳入 `make demo-check`；`make browser-ui-check` 以真实 Vite/Chromium 验收自动任务、交互卡片→Worker 重启→回答恢复及两类手动面板 |
| 可视化拖放编辑器 | `available` | 结构化 state 为唯一真相（JSON 降级为导入/导出视图），调色板新增节点、画布连线/删除、环检测、可编辑 Inspector 与 VariablePicker 写入映射、fallback 配置、发布治理；运行详情提供检查点接力视图（fallback target/审批状态/唯一 Recovery NodeRun）与受控 WorkflowPatch eligibility/replay 入口 |

`available` 表示已接入主路径并有自动化测试；`partial` 表示主路径部分实现、仍有已知缺口；
`not-configured` 不应在 UI 中标记为可运行能力。SkillX 的唯一规范源是
[`docs/skillx.md`](skillx.md)，版本为 `skillx.dev/v1alpha1`。

