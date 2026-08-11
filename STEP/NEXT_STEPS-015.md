# NEXT_STEPS-015：让冻结计划成为 Host 的执行事实

依据：[`REPLAY-014.md`](../REPLAY/REPLAY-014.md)、
[`review/design-compliance-review-015.md`](../review/design-compliance-review-015.md)、
[`review/code-review-015.md`](../review/code-review-015.md)。

范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 目标与最短路径

014 已经把计划、ToolResult 和断点持久化。015 不再增加新场景类型，核心目标只有一个：
**checkpoint 中通过校验的 remaining plan 必须成为 Host 的执行输入，而不是给 Agent 的建议文本。**

目标链：

```text
verified PlanSnapshot + step states + Artifact inputs
  → Host materialize next executable step
  → Agent 只能执行该 step，或提交绑定 source digest 的 ReplanPatch
  → Host-owned step attempt/state
  → checkpoint 只由可信 attempt 更新
  → 有界 continuation chain
```

## 2. A：把 source PlanSnapshot 绑定到 continuation Host

### A-01 Expected plan contract

- [ ] `AgentHostRequest` 增加 expected plan digest/revision、allowed remaining step ids 和 source checkpoint id。
- [ ] TaskRecovery 从已校验 Artifact 构造该 contract，不从 prompt 再解析可信字段。
- [ ] continuation 的首个 `agent.plan` 必须与 expected plan 完全一致，或改为 Host 直接注入 plan、不允许 Agent 重报。
- [ ] 新 plan/不同 planId/revision/steps 默认拒绝；只有合法 ReplanPatch 可以产生 revision+1。
- [ ] continuation output lineage 保存实际执行的 source/revised plan digest。

### A-02 Host materialize Tool arguments

- [ ] 定义 `argumentRefs` 的严格 Schema：literal、user_input、step_output、artifact；禁止任意扩展字段。
- [ ] Host 从 original input、已验证 ToolResult Artifact 和 step outputs 解析 arguments。
- [ ] 推荐 Host 直接生成 ToolExecutionRequest；若保留 Agent ToolRequest，则逐字段比较 arguments、action、
  capability、effect、idempotencyKey 与 plan-derived request。
- [ ] URL、文件路径、lookup key 和 prior output path 的任何偏离都 fail closed。
- [ ] 参数解析结果与 source Artifact digest 进入 action signature 和事件审计。

完成条件：计划 `safe.txt` 时，Agent 无法执行 `different.txt`；计划 URL A 时无法静默改成 URL B。

## 3. B：建立 Host-owned plan step 状态机

- [ ] 每个 plan revision 的 step 状态显式为 remaining/running/succeeded/failed/unknown/skipped。
- [ ] Tool request 以 plan digest + step id + attempt 做唯一键；callId 只用于传输配对。
- [ ] succeeded step 禁止再次执行；依赖只认 Host 已提交的 succeeded 状态。
- [ ] retry 仅在 fallback=retry、ErrorPolicy 允许、attempt 未超预算且 effect/idempotency 安全时创建新 attempt。
- [ ] replan 只能替换未开始或失败后允许替换的 remaining closure，不能删除已完成依赖事实。
- [ ] checkpoint 保存全部 attempts 和最终选择，不能按 planStepId 用最后事件覆盖历史。

完成条件：重复 step、跳依赖、先成功后失败、先失败后成功都有确定且不可伪造的状态。

## 4. C：闭合 `replan_required` 人工路径

- [ ] 没有结构化 plan 且错误可恢复时，checkpoint 直接进入 `awaiting_user`，不是 pending。
- [ ] 保存明确 pendingQuestion、effective error、已有 Artifact、capability locks 和 replan budget。
- [ ] answer API 使用 snapshot digest CAS，一次性确认后转 pending；拒绝后转 cancelled/terminal。
- [ ] 受限 replan 生成新的 PlanSnapshot revision 1，并明确 lineage 指向 legacy source run。
- [ ] 不支持 replan 的错误直接 terminal，不进入 Worker 领取循环。
- [ ] `start_continuation()` 捕获完整性/计划/预算错误后原子写诊断状态，不遗留 running lease。

## 5. D：统一 continuation generation budget

- [ ] 区分 `claimGeneration`（租约 fencing）和 `continuationGeneration`（业务接力链）。
- [ ] checkpoint source evidence 保存单调 chain generation，后续 continuation 不得自报或回退。
- [ ] claim 前检查 chain generation；达到上限直接 CAS terminal 并记录 budgetExhausted 事件。
- [ ] Worker crash/reclaim 只增加 claimGeneration，不消耗 continuationGeneration。
- [ ] 新增“continuation 连续失败三代”和“同一代 Worker crash 三次”对照测试。

## 6. E：收紧 ReplanPatch

- [ ] ReplanPatch 必须引用 checkpoint source plan digest/revision 和当前 step-state digest。
- [ ] replacementSteps 只能覆盖 remaining closure，不能借新 plan 绕过。
- [ ] Host 对 replacement 的 argumentRefs、effect、能力、DAG、预算执行与初始 plan 相同的校验。
- [ ] replan revision Artifact 在执行任何新 step 前提交并原子落位。
- [ ] 并发/重复 patch 幂等返回同一 revision，冲突返回结构化错误。

## 7. F：真实反向黑盒

现有三条成功接力继续保留，并增加不可信 Agent 场景：

### F-01 参数偏离

- [ ] Browser 计划 URL A，continuation 请求 URL B，Host 拒绝且 B 的服务访问计数为 0。
- [ ] file plan `safe.txt`，请求 `different.txt`，Host 拒绝且不同文件不被读取。
- [ ] lookup literal q1，请求 q4，Host 拒绝且无 q4 ToolResult。

### F-02 计划偏离

- [ ] continuation 发送全新 plan、改变 step 顺序、删除依赖或跳过 remaining step，全部 fail closed。
- [ ] 同样变更通过合法 ReplanPatch 后才能执行，并产生 revision+1 Artifact。

### F-03 重复与预算

- [ ] succeeded step 用新 callId 重复请求，Adapter 调用计数不增加并返回结构化拒绝。
- [ ] effect=none failed step 在允许预算内重试；超预算进入 terminal。
- [ ] 三代 continuation 失败后终止；三次 Worker reclaim 不消耗业务代数。

### F-04 legacy replan

- [ ] 无结构化 plan 的 transient failure 显示 awaiting_user。
- [ ] 用户确认一次后生成受限 plan并完成；第二次回答返回 409。

每条反向场景必须走公开 API、独立 Worker 和持久数据库；不能只调用私有校验函数。

## 8. G：规范与门禁

- [ ] `docs/demo-acceptance.md` 明确 PlanSnapshot 是 Host execution source of truth。
- [ ] 文档区分 Agent proposal plan、Host accepted plan、ReplanPatch 和 executable step attempt。
- [ ] 明确 claimGeneration 与 continuationGeneration 的不同语义。
- [ ] 记录 REPLAY-015 的实际 commit、命令、测试计数和未完成项。
- [ ] 运行 `make demo-check`；现有三条成功接力和 F-01～F-04 分别独立运行。
- [ ] 对参数偏离、重复 step、三代失败做至少 5 次稳定性验证。
- [ ] 运行 `git diff --check`，确认无临时数据库、Artifact 或 marker 状态文件入库。

## 9. 016 完成矩阵

| 门禁 | 必须证明 |
|---|---|
| Expected plan binding | continuation Host 绑定 source digest/revision/remaining set |
| Argument authority | Tool 实参由 Host 解析或严格匹配 argumentRefs |
| Replan-only mutation | 任何计划变化只能经绑定 source 的 ReplanPatch |
| Step state | succeeded 不重做，retry/unknown/skipped 状态可审计 |
| Legacy recovery | replan_required 有一次性用户确认路径 |
| Bounded chain | chain generation 与 lease claim 分离并稳定终止 |
| Negative Browser | 偏离 URL 的外部访问计数为 0 |
| Negative file/lookup | 偏离路径/key 不产生 ToolResult |
| Existing success | Browser、lookup、file 三类成功接力继续通过 |
| Existing closure | distill/publish/L0/L1 与 Replay 证据链无回归 |
| Quality | 全量门禁全绿，正向与反向黑盒独立通过 |

在这些门禁完成前，准确表述是：**PlanSnapshot 已可持久和恢复，但冻结计划尚未成为 Host 强制执行的
事实。**
