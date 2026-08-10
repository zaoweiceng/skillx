# NEXT_STEPS-017：闭合失败接力的最后一跳

依据：[`REPLAY-016.md`](REPLAY-016.md)、
[`review/design-compliance-review-017.md`](review/design-compliance-review-017.md)、
[`review/code-review-017.md`](review/code-review-017.md)。

范围：单用户 Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 目标与最短路径

正常“探索—固化—复用—结果返回”已跑通；后端 remaining-only continuation 也已跑通。017 不应扩展新能力，
只解决一个事实：**恢复 Worker 得到最终答案后，原对话必须自动、持久、无歧义地显示该答案。**

```text
源 Agent 真实失败
  → checkpoint pending
  → 新 Worker 创建 continuation
  → continuation terminal
  → 原 Conversation 自动发现接力链
  → 成功 output 或最终 error 直接显示
  → 刷新后仍一致
```

## 2. A：建立 continuation 的持久对话关联（P1）

- [ ] 定义唯一契约：sourceRunId、checkpointId、continuationRunId、generation 和链终态。
- [ ] continuation 创建必须与 checkpoint `running→continuing` 的 fencing/CAS 同事务或具备等价幂等保证。
- [ ] 为 Conversation 写入可持久恢复的 continuation assistant message，或提供等价的服务端链式投影；不得只写
  checkpoint 私有 snapshot。
- [ ] 重复 claim、reconcile 或 Worker 重启不得产生重复结果 message。
- [ ] 保留源失败证据；不得把 source Run 原地篡改成 succeeded。
- [ ] 多代 continuation 失败时，链能指向下一 child checkpoint；最终只给用户一个明确终态，同时保留每代 lineage。
- [ ] cancelled、budget exhausted、integrity failed 和人工等待均有稳定 code/status，不得永久显示“恢复中”。

推荐最小实现：continuation 入队时创建带 `conversationRunId=continuation.id`、
`continuationOfConversationRunId`、`taskCheckpointId` 和 generation 的统一 `agent_run` message；利用现有 checkpoint
CAS 防重复。若消息可能在源页面停止轮询后才出现，Chat 还必须在可恢复 source error 上有界刷新 Conversation，
直到 checkpoint/chain terminal。

## 3. B：让 Chat 展示并持续跟踪接力链

- [ ] Chat 刷新时识别 continuation message/chain，加载 continuation Run 的 status、events、output/error。
- [ ] source failed 且带 `taskCheckpointId` 时显示“等待接力/接力中”，而不是立即把源错误当作最终答案。
- [ ] continuation succeeded 后直接显示 `output.reply` 或结构化 output。
- [ ] continuation failed/cancelled/terminal 后显示最终稳定错误，并区分源错误与接力错误。
- [ ] 页面不手动刷新也能从 source failed 过渡到 continuation terminal。
- [ ] 浏览器刷新后恢复同一链和最终结果；不能依赖当前页内 state。
- [ ] 页面提供 checkpoint/continuation lineage 和关联 Run 的可访问入口。
- [ ] 同一 terminal 状态重复轮询不新增 message、不重复渲染结果。

## 4. C：补全 checkpoint API 可观察性（P2）

- [ ] `GET /task-checkpoints/{id}` 返回已持久化的 `continuationStatus`、
  `continuationOutputDigest`。
- [ ] 返回 continuation generation、child checkpoint 或 next continuation 的稳定引用。
- [ ] API 测试覆盖 pending→running→continuing→succeeded/failed 的字段变化。
- [ ] output 本体继续从可信 `ConversationRun` 读取，digest 只用于一致性和变化检测。

## 5. D：修正真实浏览器门禁（P2）

- [ ] 保留当前 Workflow `INVALID_INPUT`/failed RunDetail 场景，但不要称其为 Agent failure handoff。
- [ ] 新增独立真实场景：Chat 自然输入 `continue_agent + planned_browser_summary`，目标站点返回真实 503。
- [ ] 源 Worker 以 `--disable-task-recovery` 启动；等待 source failed 和 checkpoint pending 后停止。
- [ ] 将站点恢复 healthy，启动新的正常 Worker，等待 continuation succeeded。
- [ ] 断言 `transientCount` 从有限重试后的 2 只增加到 3；已成功 preflight 不重做。
- [ ] 断言 checkpoint attempt 1 failed、attempt 2 succeeded，argumentsDigest 非空且一致。
- [ ] **必须在原 Chat DOM 中看到“恢复后的正文”**；刷新后仍看到相同结果。
- [ ] 断言源错误、checkpoint id、continuation id/generation 和最终 output 均可观察。
- [ ] 浏览器 console 无 error；业务断言失败不得转为 skip。

## 6. E：回归与文档

- [ ] 后端测试增加 continuation Conversation 关联的幂等、并发 claim 和多代链测试。
- [ ] ChatPage 测试使用 source failed → continuation queued/running/succeeded 的真实 API 形状。
- [ ] 更新 `docs/demo-acceptance.md`：Real Tool failure handoff 的通过条件明确包含最终结果回到原 Conversation。
- [ ] 在新的真实 handoff UI 门禁通过前，README/REPLAY 不得把完整闭环标记为 available/通过。
- [ ] 运行 `make demo-check`、`make browser-ui-check`、`git diff --check`。
- [ ] 不提交临时数据库、Artifact、日志或浏览器产物。

## 7. 018 完成矩阵

| 门禁 | 必须证明 |
|---|---|
| Normal flow | 首次探索、固化、发布、新对话复用、结果回显继续全绿 |
| Generic entry | Browser、lookup、file/read 自然入口继续可用 |
| Source evidence | 真 503、有限重试、checkpoint 与 source error 保留 |
| Remaining-only | 新 Worker 不重做 succeeded step，只重试 failed/remaining steps |
| Conversation linkage | continuation 可从原 Conversation 持久发现且不会重复 |
| Result delivery | 恢复成功 output 自动显示；刷新不丢失 |
| Terminal failure | 多代耗尽/取消/完整性失败给出最终明确错误 |
| Observability | source、checkpoint、continuation、generation 和 output lineage 可见 |
| Real UI gate | 实际 API/Worker/Vite/站点/Chromium 完成 handoff，不用单测模拟 |
| Quality | 全量门禁通过，文档结论与真实证据一致 |

满足以上门禁后，才可准确表述为：**通配型“首次探索—固化—复用—失败接力—修订/恢复—结果返回”闭环已在
Local Demo 真实环境完整跑通。**
