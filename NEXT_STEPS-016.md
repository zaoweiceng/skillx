# NEXT_STEPS-016：先闭合用户可见主流程

依据：[`REPLAY-015.md`](REPLAY-015.md)、
[`review/design-compliance-review-016.md`](review/design-compliance-review-016.md)、
[`review/code-review-016.md`](review/code-review-016.md)。

范围：单用户 Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 目标与最短路径

015 后端已经能执行冻结计划和真实失败接力。016 不应继续扩展新能力类型，唯一优先级是让现有能力从用户
输入到结果展示完整跑通：

```text
自然语言任务
  → 渐进发现或受控首次探索
  → 页面可见 Agent 状态/结果
  → 固化、检查、人工发布
  → 新对话 L0/L1 复用
  → Workflow terminal output 直接返回对话
  → 运行详情可打开

任一步失败
  → 页面显示真实错误/checkpoint
  → Worker 可从冻结计划接力
```

本版本的完成标准不是“测试数量增加”，而是上述路径在实际 API、Worker、Vite 和浏览器中连续成功。

## 2. A：修复运行详情白屏（P0）

- [ ] `RunDetailPage` 不得在 Zustand selector 内创建新的空数组/对象。
- [ ] 定义稳定的模块级空常量，或让 selector 返回 `undefined` 后在外部回退。
- [ ] 保持 `useRunStream` 的 `onEvent` 引用稳定，确认不会因 refresh identity 重建 EventSource 循环。
- [ ] 增加使用真实 `useRunEvents` store 的组件测试，不得 mock 掉 store 订阅。
- [ ] 启动前后端，从 ChatPage 点击一个 succeeded、一个 running/failed Run 的“查看运行”，页面均正常显示。
- [ ] 浏览器 console 不得出现 `getSnapshot should be cached`、maximum update depth 或未捕获异常。

完成条件：`/runs/:id` 不白屏，节点、最终 output/error、Artifact 和依赖 lineage 可见。

## 3. B：让默认自然对话能执行通配型只读探索

### B-01 服务端能力策略

- [ ] 不再让 `continue_agent` 无条件继承仅 `search.web` 的默认集合。
- [ ] 定义 Local Demo 受控只读探索 capability policy，覆盖现有承诺的 Browser、lookup、file/read、
  summarize/transform；写/unknown effect 默认不授予。
- [ ] WorkflowSelection/计划产出的所需 capability 必须由 Host 验证并收窄到该策略，Agent 或前端不能扩权。
- [ ] 自动 `continue_agent` 和显式 `/agent-runs` 共用同一 capability resolution 与审计逻辑。
- [ ] 权限不足时，在入队前返回可解释诊断；不要创建一个注定失败且 UI 不可见的 Run。

### B-02 Agent message contract

- [ ] 抽取统一 `agent_run` assistant message builder。
- [ ] 自动与显式两条路径都写 `type/state/conversationRunId/runType/text`。
- [ ] 前端以 `type=agent_run` 为主判别，并兼容历史缺少 `runType` 的消息。
- [ ] 刷新 `/chat/:id` 后能恢复 active run、terminal 状态、事件、错误和固化按钮。

完成条件：用户只在 Chat 输入 Browser、lookup、file 三类任务，均能由默认自然入口成功探索；不需要手工调用
API 传 `allowedCapabilities`。

## 4. C：把 Workflow terminal output 返回 Conversation

- [ ] 明确唯一持久关联：assistant workflow message 的 `runId` 指向 Run，不复制不可信临时结果。
- [ ] ChatPage 加载历史消息时批量读取关联 Run；queued/running 有状态，terminal 渲染 output/error。
- [ ] 对活动 Workflow Run 使用 SSE 或有界轮询，完成后立即更新对应 message 卡片。
- [ ] 刷新页面后仍能显示相同 terminal output；不能只存在 React 临时 state。
- [ ] succeeded 时直接展示 `reply` 或按 Output Schema 渲染结构化结果，并保留“查看运行”。
- [ ] failed/cancelled 时显示稳定 code/message 和 checkpoint/修订入口，不把失败文案伪装成成功回复。
- [ ] 同一 Run terminal 事件重复到达不得创建重复 assistant 结果。

完成条件：第二次复用完成后，对话页直接显示网页的新摘要；用户无需进入运行详情才能获得答案。

## 5. D：闭合 replan revision budget

- [ ] 将 `maxRevisions/usedRevisions/remainingRevisions` 纳入可信 ExpectedPlanContract 和 source digest。
- [ ] Host 在接受 ReplanPatch 前检查并消费预算；达到上限返回稳定结构化错误。
- [ ] 明确 legacy 初始 revision 1 是否计入预算；文档、代码和事件使用同一语义。
- [ ] 重复同一 patch 的幂等回放不重复扣预算；不同 patch/revision 必须扣减。
- [ ] replan 次数不依赖 `agent.step.completed`，也不能只靠运行 timeout 限制。
- [ ] checkpoint/API 显示实际 used/remaining；continuation 继承而非重置预算。

## 6. E：补全 attempt 参数证据

- [ ] ToolResult audit 或 Artifact metadata 向 checkpoint 提供可信 `argumentsDigest`。
- [ ] checkpoint 构造时逐 attempt 校验 planDigest/stepId/attempt/callId/argumentsDigest/status。
- [ ] 有 ToolResult Artifact 的 attempt 若 digest 缺失或冲突，checkpoint 必须 integrity_failed/terminal，不能静默写 null。
- [ ] continuation 新 attempt digest 与 Host 物化 arguments 的 digest 一致。
- [ ] API 返回的 `stepAttempts` 可区分失败 attempt 1 与成功 attempt 2。

## 7. F：真实主流程验收（不得用单测替代）

### F-01 自然入口探索

- [ ] 启动全新 Alembic 数据库、API、独立 Worker、Vite 和本地动态站点。
- [ ] 只从 `/chat` 输入网页任务；不得用 curl 补 `allowedCapabilities`。
- [ ] AgentRun succeeded，UI 显示 Browser Tool 轨迹、摘要和固化按钮。
- [ ] lookup 与 file/read 各执行一次同类入口验收。

### F-02 固化、发布、第二次复用

- [ ] 从 F-01 的成功卡片点击固化。
- [ ] 在 WorkflowEditor 做静态检查并人工发布。
- [ ] 修改源内容和 URL 参数，新开 Conversation 用自然语言请求同类任务。
- [ ] 观测 L0→L1、选择首次 lineage、Browser 重新取新内容、Workflow succeeded。
- [ ] 新摘要直接出现在对话消息；点击运行详情正常显示同一 output。
- [ ] 不得持 Component/WorkflowVersion id 直调来冒充渐进发现。

### F-03 真实失败接力

- [ ] Browser 站点返回真实 503，确认 Host 有限重试后生成 pending checkpoint。
- [ ] 停止源 Worker、恢复站点、启动新 Worker。
- [ ] 已成功 preflight 调用数不增加；Browser 仅增加一次恢复调用；continuation output 返回对话。
- [ ] UI 显示 source failed、checkpoint/continuation lineage 和最终恢复结果。

### F-04 真实前端稳定性

- [ ] 浏览器逐页访问 Chat、Components、WorkflowEditor、RunDetail。
- [ ] 检查 DOM、关键交互和 console error；不能只验证 HTTP 200 或 production build。
- [ ] 对自然 auto-agent、显式 agent、workflow run 三种消息刷新恢复。

## 8. G：自动化门禁补洞

- [ ] 保留 `make demo-check` 全绿。
- [ ] 新增 `continue_agent` message contract 测试，断言 `runType` 与服务端能力集合。
- [ ] ChatPage 测试必须覆盖 sendMessage 直接返回 auto AgentRun，而不只覆盖候选卡片再点 Agent。
- [ ] RunDetailPage 至少一条测试使用真实 Zustand store。
- [ ] 增加 Workflow succeeded 后 Chat 渲染 output、刷新恢复、terminal 幂等测试。
- [ ] 增加 replan budget 耗尽和 attempt argumentsDigest 非空/一致测试。
- [ ] 真实浏览器主路径作为独立验收命令；CI 环境无 Chromium 权限时必须明确 skip 原因，不能以 mock 替代 passed。
- [ ] 更新 README/`docs/demo-acceptance.md`：主流程验收全绿前，将整体状态从 `available` 改为 `partial`。
- [ ] 运行 `git diff --check`，不提交临时 DB、Artifact、日志或浏览器产物。

## 9. 017 完成矩阵

| 门禁 | 必须证明 |
|---|---|
| Natural exploration | Chat 自然输入可执行 Browser/lookup/file，只授予受控只读能力 |
| Agent visibility | 自动 Agent 消息刷新后可见 status/events/error/solidify |
| Distill/publish | UI 固化、检查、人工发布成功 |
| Progressive reuse | 新 Conversation 经 L0/L1 命中首次 lineage，不持 id 直调 |
| Result delivery | Workflow terminal output 直接显示在对话且刷新可恢复 |
| Run detail | 真 Zustand + SSE 页面不白屏，console 无无限更新错误 |
| Failure handoff | 真 503 + Worker 重启只执行 remaining steps，最终结果回到对话 |
| Replan budget | Host 强制 revision 次数，幂等重复不重复扣减 |
| Attempt evidence | 每个 attempt 的 argumentsDigest 非空且与 Artifact/Host 一致 |
| Quality | 全量门禁与真实浏览器验收均绿，文档不再过度标记 available |

在这些门禁完成前，准确表述是：**后端 authoritative-plan 与失败接力可用，但用户可见的“首次探索—固化—
复用—失败接力—结果返回”主流程尚未完整跑通。**
