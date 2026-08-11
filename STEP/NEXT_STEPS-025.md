# NEXT\_STEPS-025：让提交本身可发布，并接通可信真实语义入口

依据：[`REPLAY-024.md`](../REPLAY/REPLAY-024.md)、
[`design-compliance-review-025.md`](../review/design-compliance-review-025.md)、
[`code-review-025.md`](../review/code-review-025.md) 和 [`docs/`](../docs/) 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。优先级按“提交可复现 → 真实语义可证明 →
实时审计完整 → UI 收尾”排列。

## 1. 025 完成定义

下一版只有同时满足以下事实才能声明通过：

1. 可以连续两次完成 `make demo-check`；
2. 可信真实 Agent Runtime 用普通自然语言完成选择、参数绑定、执行和双格式结果；
3. live SSE 与最终 DB 事件集合一致，模型调用审计覆盖成功与失败；
4. 所有可发布内置工作流都具备可供 Agent 判断的完整目录元数据。

fixture 继续只用于协议测试。禁止通过城市、天气或固定探测答案伪造语义能力。

## 3. P0：交付可信真实语义 Runtime

- [ ] 提供平台维护的 `traex_managed` adapter，或完整配置并验证 `openai_sidecar`；记录可信 adapter 版本和 digest。
- [ ] 探测成功必须要求显式 `model.request.dispatched`，禁止从普通 `agent.step.completed` 推断模型调用。
- [ ] 探测证据必须包含 provider/model、adapter digest、随机 challenge、真实 Usage、ToolResult round-trip 和 strict output；
  `requestDispatched=false` 时无条件保持 unavailable。
- [ ] 对可执行文件 allowlist 与可信 adapter digest 分开治理；“能启动脚本”不等于“具备真实模型语义”。
- [ ] 首次配置后的探测/刷新流程必须明确：若需用户确认真实费用，Chat 给出直达探测入口；探测过期后给出可操作状态，
  不能让默认开启工作流永久停在不可用且无恢复路径。
- [ ] “上海天气如何”第一 Turn 直接 `run_full`、绑定 `city=上海`、当前消息 provenance、`serverVerified=true`。
- [ ] 再覆盖多字段、数字、布尔、枚举、数组、URL、历史引用和歧义拒绝；全部使用普通自然语言且不含 fixture 指令。
- [ ] 工作流关闭时全 Turn 不搜索、不披露、不调用 `workflow.*`。

## 4. P1：补齐所有内置能力目录并在发布时强制验证

- [ ] 为 seed 和 examples 中每个可发布工作流补齐 `summary`、`useWhen`、`doNotUseWhen`、`examples`、`tags`、
  输入输出摘要；不只修天气。
- [ ] 建立 manifest Schema/发布诊断：面向语义发现的工作流缺少上述字段时禁止发布或标为不可发现。
- [ ] L0 只暴露名称/摘要/tags，L1 暴露适用/不适用和输入输出，L2 再暴露 Fragment/Schema。
- [ ] 浏览器逐一检查 8 个内置组件，不得再出现“适用/不适用：未补充”。
- [ ] 真实语义最低集记录候选排序和拒绝理由，验证元数据确实参与 Agent 选择。

## 5. P1：统一工作流选择的模型 Usage 审计

- [ ] workflow selection 成功时显式传入 `result.request_dispatched`、`completed=true` 和实际 provider/model/runtime。
- [ ] 捕获 `AgentHostError` 时先写 Usage，再返回 502；记录 attempted、`exc.request_dispatched`、completed=false、
  错误码、已知 Token 和唯一 call/attempt。
- [ ] 覆盖握手前失败、dispatch 前校验失败、DNS/连接、401、429、timeout、畸形输出和成功。
- [ ] 费用/Token 聚合只计算 dispatched；未 dispatch 的尝试仍保留审计，但不能显示“模型已调用”。
- [ ] operational probe、workflow selection、general agent、workflow node、Presentation 使用同一语义，不允许调用点依赖默认值。

## 6. P1：保证实时 SSE 与持久事件完全一致

- [ ] 不再手写不完整的前端事件数组；从共享 Schema/生成类型获得标准事件集合，或使用通用 SSE envelope。
- [ ] 至少覆盖 `mcp.call.completed`、`artifact.created`、审批、恢复、Presentation、取消和 timeout 全部事件。
- [ ] 建立真实运行断言：live store 的 seq 集合在终态时与 `GET /runs/{id}/events` 完全一致且连续。
- [ ] 历史加载与 SSE 切换继续按最后 seq 接续、按 `(runId, seq)` 去重；刷新后不得依赖“补救”才能看到 Tool/Artifact。
- [ ] 未知新事件应以通用样式展示，不能静默丢弃。

## 7. P2：编辑器视觉与信息架构收尾

- [ ] 删除可编辑画布下方的重复只读图；若保留总览，增加标题/折叠并把高度压缩到摘要尺寸。
- [ ] 保持“能力契约 → 编辑画布/Inspector → 发布历史/手动运行”的连续信息流，避免同一 DAG 连续出现两次。
- [ ] 亮暗主题逐页复验 Chat、Settings、Workflow Editor、Component、RunDetail。
- [ ] 760、903、1024、1280、1440px 无遮挡、横向溢出和不可点击控件；console 0 error/warning。

## 8. 真实交付门禁

1. `make demo-check` 连续两次通过；
2. 后端/前端质量、空库迁移和 37 条独立进程 E2E 全部通过；
3. `make browser-ui-check` 在正常主机权限连续两次通过，受限 Browser 正确降级且不阻塞 Worker；
4. 真实语义最低集全部通过，记录 provider/model/adapter digest/Turn/Run/Usage，且不记录 Secret；
5. 所有内置能力目录完整，真实 Agent 能解释选择和拒绝理由；
6. live SSE 与 DB 最终 seq 集合一致，Tool/Artifact 不需要刷新才出现；
7. workflow selection 成功/失败的 attempted/dispatched/completed 和费用事实准确；
8. 新增 `REPLAY-025.md`，只记录对应提交或干净检出可复现的命令和结果。

在“干净提交总门禁、可信真实语义、实时事件一致性”三项同时完成前，不得声明整体通过。
