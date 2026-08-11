# NEXT_STEPS-010：先打通真实二次复用，再闭合接力与修订

依据：[`REPLAY-009.md`](../REPLAY/REPLAY-009.md)、[`review/design-compliance-review-010.md`](../review/design-compliance-review-010.md)、[`review/code-review-010.md`](../review/code-review-010.md)。

范围：Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 010 目标与最短路径

010 不再以“新增了多少表/API”为完成标准，只接受两条连续结果：

```text
主链：首次对话总结
  → 真实 ToolResult
  → 固化草稿
  → 人工发布
  → 新对话语义发现
  → 第二次换输入复用成功

故障链：已发布固化版本失败
  → 持久 checkpoint 被领取
  → 从剩余步骤接力
  → 失败分类与 baseline
  → 必要时受控修订
  → 人工发布修订版本
  → 再次复用成功
```

实现顺序必须遵循因果关系：先恢复可运行基线，再证明主链；主链没有通过前，不继续扩 UI 或增加旁路 API。

## 2. A：恢复可信基线

### A-01 删除伪依赖

- [ ] 删除 `workflow_catalog.py` 的 `pypolaris` import。
- [ ] `operation` 通过 `_SESSION_LIMITS` 白名单后直接构造 SQLite JSON path。
- [ ] 增加从锁定依赖导入 `app.main`、收集全部测试的门禁。
- [ ] 不通过安装 `bytedpypolaris` 掩盖根因。

### A-02 重跑完整门禁

- [ ] Ruff、后端全量 Pytest、空库 Alembic `0001→0015`、迁移幂等检查。
- [ ] TypeScript、ESLint、Vitest、Vite build。
- [ ] 在允许 Chromium 启动的真实环境运行 Playwright 单元和黑盒 E2E。
- [ ] `REPLAY-010.md` 记录本次实际 commit、命令、退出码和测试计数，不沿用 009 数字。

完成条件：干净环境 `make demo-check` 一次通过，API 和 Worker 均可启动。

## 3. B：把“第二次复用”改成新对话真实发现

### B-01 生成稳定 CapabilityDescriptor

- [ ] Distillation Agent 输出结构化 `name/summary/useWhen/doNotUseWhen/tags/examples`，描述任务意图和输入类别，不复制 URL、Secret 或测试指令。
- [ ] 平台校验字段长度、类型、敏感内容和输入输出 Schema 后写入 metadata。
- [ ] 对网页总结使用 URL A 固化，用 URL B 的同类请求仍能搜索命中；对非网页 lookup/read 做同样测试。
- [ ] Manifest 的 L1 视图暴露必要 lineage 摘要：source ConversationRun/trajectory digest/distillation id，L0 不扩张上下文。

### B-02 新增一条不可绕路的公开黑盒

- [ ] 启动动态内容服务，首次正文使用随机值 A。
- [ ] 通过公开 Conversation AgentRun API 发起“访问并总结”，断言 Agent 最终输出和 ToolResult 都包含 A。
- [ ] 通过公开 distill API 创建草稿并人工 publish；不在测试中手写 Workflow definition。
- [ ] 修改为 URL/内容 B，创建第二个新 Conversation，以自然语言提出同类任务。
- [ ] 断言第二次对话真实出现 `workflow.search` L0 和按需 L1/L2，选择的 WorkflowVersion lineage 指向首次 ConversationRun。
- [ ] 由 Workflow Runtime 完成第二次运行，输出包含 B；测试不得创建 Component 后持 id 直调。

完成条件：删除或替换 E2E-8b 中“手工 create Component→直接 run”的闭环证据角色；保留它只能作为 Runtime 分段回归测试。

## 4. C：让 Selection-5 名实一致

### C-01 fragment

- [ ] Agent 选 fragment 前必须调用 `workflow.describe_fragment`，Disclosure level 到 L2。
- [ ] 根据 boundaryInputSchema 产生 bindings；缺输入时保存原 fragmentRef 并 ask_user。
- [ ] 回答后执行冻结 Fragment Plan，到 Worker `succeeded` 终态。

### C-02 compose

- [ ] 把 `workflow.compile_plan` 改成纯编译，不在工具调用阶段写 PlanSnapshot。
- [ ] Compiler diagnostics 回送同一 Agent；允许在剩余预算内修正。
- [ ] compose 缺输入时 PendingSelection 保存 raw/compiled plan、refs、bindings 和明确 `resumeAction=compose`。
- [ ] 最终选择后只持久化一个 PlanSnapshot，并运行到终态。

### C-03 五分支门禁

- [ ] `run_full`：公开 Chat→Run→Worker succeeded。
- [ ] `run_fragment`：L2→PlanRun→Worker succeeded。
- [ ] `compose`：compile diagnostics→单一 PlanSnapshot→Worker succeeded。
- [ ] `ask_user`：回答恢复原 task/ref/plan，不重新搜索回答文本。
- [ ] `continue_agent`：保留已完成 ToolResult 和预算回到通用 Agent，不再走未披露的 Component heuristic。
- [ ] 未披露 ref、错类型、缺 bindings、过预算、过期 session 均 fail closed。

## 5. D：实现真正的 AgentRun 失败接力

### D-01 可恢复证据

- [ ] 为闭合的只读 ToolResult 保存有界、脱敏、Schema 校验后的 Artifact；审计事件继续只存摘要。
- [ ] TaskCheckpoint 引用这些 Artifact，并保存 source digest、已完成步骤、失败步骤、剩余计划、能力锁和 side-effect outcome certainty。
- [ ] 对未知非幂等结果保存待确认问题，不保存或猜测“已失败”。

### D-02 Driver 与状态机

- [ ] 实现 TaskCheckpoint 原子 claim：`pending→running`，带 lease/generation/token。
- [ ] 受限 Recovery Agent 只读取 checkpoint 和允许的剩余能力，不读取全 Catalog。
- [ ] 恢复成功追加到同一 lineage/trajectory；原 ConversationRun 保留失败事实，新 continuation run 显式引用 checkpoint。
- [ ] 实现 `awaiting_user` 查询/回答/继续 API；回答只能消费一次。
- [ ] 迟到 owner 的 ToolResult、终态和 Artifact 全部由 CAS 拒绝。

完成条件：首次网页总结与非网页 read/lookup 各注入一次可恢复中断，重启 Worker 后从持久 checkpoint 继续，已完成步骤不重做。

## 6. E：让外部失败可以正确决定“接力还是修订”

### E-01 Failure fixture

- [ ] 成功外部节点继续保存 output fixture。
- [ ] 失败外部节点保存 failure fixture：原错误码、冻结契约 digest、输入 digest、脱敏页面/响应证据、capture policy。
- [ ] Replay 重新计算并校验 sourceDigest/inputDigest/output 或 error digest；错关联和篡改返回 `REPLAY_EVIDENCE_CORRUPT`。
- [ ] 网络瞬态、页面契约漂移、MCP 契约漂移使用不同模拟器和分类规则。

### E-02 修订决策

- [ ] 一次网络波动且接力成功：classification=transient，不调用 Revision Agent。
- [ ] 同一冻结输入/fixture 可复现 PAGE_DRIFT 或 MCP_CONTRACT_DRIFT：才调用 Revision Agent。
- [ ] Revision Agent 只能输出现有受控 Patch Schema，不得扩大能力、网络、Secret 或副作用。
- [ ] proposal 本身不发布；应用 Patch 后执行 patched replay，通过后才创建草稿。

### E-03 修订后复用

- [ ] 人工发布新 WorkflowVersion，旧精确 ref 仍可解析，新搜索只返回 active 版本。
- [ ] 用原失败输入和一个新输入各运行一次，均成功。
- [ ] lineage 必须包含 observation id、fixture digest、ReplayEvidence id、base version id 和新 version id。

完成条件：至少一类真实外部读取节点完成“真实失败→baseline 同错误→Patch→发布→再运行”；纯 Transform 不能作为唯一正例。

## 7. F：收紧证据和并发不变量

### F-01 输出 provenance

- [ ] `agent.completed.outputProvenance` 按输出字段声明来源 call id/path/evidence digest。
- [ ] Host 校验来源 call 存在、已成功闭合、path 合法且 digest 一致。
- [ ] 确定映射执行值级校验；生成式总结至少保存来源集合和提示词版本。
- [ ] 增加“列出真实 call id 但返回 fixture 常量”的拒绝测试。

### F-02 终态 CAS

- [ ] `_finalize_success/_failure/_cancelled` 使用包含 status/owner/generation/token 的单条 CAS。
- [ ] CAS 成功后才写事件和 Artifact；失败则清理 staging。
- [ ] 增加双 Session 交错：旧 Worker 判断后、新 Worker 接管、旧 Worker 返回；旧写入必须为 0。

## 8. G：更新规范和产品证据

- [ ] `docs/demo-acceptance.md` 将 Real first browser 事件更新为 `tool.requested/tool.result`。
- [ ] 新增“Conversation summary reuse”连续场景，明确禁止手工 Component id 绕过发现。
- [ ] 新增 AgentRun handoff、external failure revision、revised reuse 三个连续场景。
- [ ] Chat 显示被选择的 manifest/ref/digest、Disclosure 时间线、PendingSelection 和最终 Run。
- [ ] Run detail 把首次 source、distillation、published version、第二次 run、checkpoint、observation、replay 和 revision 以 id/digest 链接起来。

## 9. 011 门禁矩阵

| 门禁 | 必须证明 |
|---|---|
| Build baseline | clean lock 下 `make demo-check` 全绿，无运行时临时 shim |
| First summary | Agent 输出消费 Host 返回的随机真实 ToolResult |
| Semantic solidification | CapabilityDescriptor 与输入实例解耦，URL/业务 key 变化仍可发现 |
| New-conversation reuse | 第二个 Conversation 经 L0/L1/L2 选择固化版本并成功执行 |
| Selection-5 | 五种 action 均由公开主路径到真实终态 |
| AgentRun handoff | checkpoint 被领取并继续；未知副作用有可回答状态 |
| External replay | 成功/失败 fixture 都锁 digest，篡改 fail closed |
| Revision decision | 瞬态不修订；可复现漂移才生成 proposal |
| Revised reuse | 人工发布修订版本后原输入和新输入均成功 |
| Lineage | 每一阶段持久引用上一阶段 id/digest，无测试进程内拼接 |
| Genericity | 网页与非网页各一条连续黑盒，共用 Broker/Checkpoint/Replay 协议 |

在上述两条连续黑盒通过前，不再使用“闭环完成”“Selection-5 已落地”或“AgentRun handoff 已实现”的表述。
