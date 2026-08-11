# 代码审查报告（008）

审查日期：2026-08-04  
审查范围：提交 `95f3fbb` 相对 006 基线的后端、前端、迁移、E2E、README 和 `REPLAY-007.md`。  
范围约束：Local Demo，不审查鉴权/RBAC；执行所有权、版本锁、副作用边界和审计完整性属于功能正确性。

## 1. 结论

完整门禁通过，007 新增的 Registry、Fragment、PlanSnapshot 和基础 PlanCompiler 都是真实实现，不是空壳。但审查发现 **8 个 P1、7 个 P2**。最高风险不是代码风格，而是三处状态机断点：TraeX 的非 full 决策被路由覆盖、Recovery 迟到 Worker 没有 fencing、RecoveryPlanPatch 可以替换冻结节点定义而不重做能力裁决。

## 2. P1 问题

### P1-1 — 对话路由忽略 TraeX 的四种 Selection，渐进披露没有形成 Agent 工具循环

[`routes.py`](../backend/app/api/routes.py) 的 `_select_workflow_candidate()` 允许 `run_full | run_fragment | compose | continue_agent | ask_user`，但 `create_message()` 只在 `run_full` 时读取所选 ref；其余 action 全部回退到 `workflow_cards[0]`，继续返回完整 Workflow 候选。`missingInputs` 和 `bindings` 也未消费。

L1 是服务端选中后直接读取，结果没有再次交给 TraeX；L2、slice 和 plan 仅是独立 REST API。`validate-selection` 也只检查 ref 是否披露，不校验 action 与 ref 数量/类型的判别联合。

影响：TraeX 不能请求补充信息、继续通用 Agent、选择 Fragment 或生成组合计划。用户要求的主场景尚未闭环。

建议：建立受控 `WorkflowToolBroker` 循环，逐步提供 search/describe/describe_fragment/slice/compile_plan；为每种 action 定义严格 Schema 和终态处理，路由不得隐式降级为第一个候选。

### P1-2 — Recovery 应用决定时没有 lease fencing，迟到 Worker 可覆盖新 owner

[`recovery.py`](../backend/app/application/recovery.py) 的 heartbeat 会校验 `lease_owner`，过期 checkpoint 也会被新 Driver 重领；但 Agent 返回后应用决定只检查 `checkpoint.status == running` 和 `run.status == recovering`，没有检查 owner 或 claim generation。

若 Worker A 超时、B 重领后 A 才返回，A 可以应用自己的旧决定，并通过当前 checkpoint 的 `recovery_node_run_id` 把 B 的 NodeRun 标为成功。这违反 [`demo-acceptance.md`](../docs/demo-acceptance.md) 中“迟到 Worker 不得覆盖新 attempt”的通用失败判定。

建议：领取时生成不可复用的 `claim_token/generation`，所有 heartbeat、决定提交和 NodeRun 终结都用单条 CAS 校验 checkpoint id + generation + owner + status。增加 A 迟到、B 已重领的竞争黑盒测试。

### P1-3 — typed replan 可任意替换剩余节点定义，能力锁校验可被绕过

`_validate_recovery_plan_patch()` 只要求 node id 集合等于原 remaining ids、边端点不越界，并比较调用方提交的 `locks` 是否属于旧集合。它不比较节点 type/config/sideEffect/fallback，也不重新执行 `validate_workflow_capabilities()`、`validate_mcp_nodes()` 或 Browser 契约裁决。

随后 [`orchestrator.py`](../backend/app/application/orchestrator.py) 的 `_merge_recovery_plan()` 用 `remainingNodes` 整体替换冻结定义。Agent 可以把 transform 改成 Agent/Browser/MCP、扩大 capability 或新增副作用，同时原样回传旧 locks。

建议：RecoveryPlanPatch 不接受完整节点定义。只允许受控 mapping、retry policy 或引用已发布 capability 的 replacement ref；合并后重跑与发布相同的 Compiler + Capability/MCP/Browser/side-effect 校验，并从结果重新派生 locks。

### P1-4 — Fragment 的输入端口契约没有参与执行，临时闭包也未包含所有 fallback 依赖

[`workflow_catalog.py`](../backend/app/application/workflow_catalog.py) 保存作者声明的 `inputMapping`，但 [`plans.py`](../backend/app/application/plans.py) 展开 Fragment 时完全不读取它，仍直接用 PlanStep input 替换源 Workflow 的 `${input.*}`。只要 Fragment 对外端口名不同于源 Workflow 输入名，Manifest 描述与实际执行即不一致。

临时 Slicer 只为最初 candidate 加入 fallback target；后来递归加入的祖先节点若有 fallback，其 target 不会进入闭包。显式 fragment 也没有校验 boundary Schema/mapping 能否由闭包独立产生。

建议：发布阶段编译 FragmentPort IR，校验 boundary mapping、Schema 可赋值和每个出口；Slicer 对闭包做不动点计算，包含祖先、control、approval、fallback 和必要补偿节点。PlanCompiler 只能消费已验证的端口 IR。

### P1-5 — 含 fallback 的 Workflow 无法被 PlanCompiler 正确组合

`_expand_definition()` 把所有内部叶子连接到合成的 step result。专用 fallback target 没有普通出边，因此会被当作叶子并新增 `fallbackTarget → stepResult`。这会使展开后的图违反 `FALLBACK_TARGET_NOT_DEDICATED`，或让 result 等待正常路径永远不会执行的 fallback 节点。

现有 Plan E2E 只组合无 fallback 的问候 Workflow，因此没有覆盖这一通用场景。

建议：展开时显式排除 fallback-only targets；step result 的依赖应从 Output mapping 的真实 provenance 推导，而不是连接全部叶子。增加含 Recovery fallback 的 full Workflow/Fragment 组合测试。

### P1-6 — PlanSnapshot 的 TTL/status 从未执行，过期计划仍可新建 Run 或固化

[`plans.py`](../backend/app/application/plans.py) 持久化 `expires_at/status`，但 `verify_plan_snapshot()` 只校验 digest。创建 Plan Run、Worker 加载、读取详情和 solidify 都不检查 status 或 expiry。

影响：TTL 只是展示字段，无法控制临时计划目录可见性或新执行；撤销/过期状态也没有语义。

建议：定义清晰规则：过期/retired snapshot 禁止创建新 Run 和固化，但已有 Run 可继续按冻结内容恢复；目录默认隐藏过期项。校验必须在 create_run 的事务内和 Worker claim 前执行。

### P1-7 — Replay 不执行真实分支/fallback 语义，也不验证最终输出 Schema

[`replay.py`](../backend/app/application/replay.py) 只按 Compiler 的全部 dependencies 顺序执行，没有调用 Orchestrator 的 `_edge_selected()` 语义；条件分支的未选节点不会被正确 skipped，fallback-only root 也可能被无条件执行。

Replay 的通过条件只是原失败节点和 Output Executor 状态为 succeeded；而 OutputExecutor 只是回传输入，Replay 没有像正常 Run 一样用 workflow output Schema 验证最终值。Schema 不合法的 patch 仍可能获得 passed。

建议：抽取 Runtime 的边选择、onError、fallback 和输出终态判定为共享纯状态机，由 Run 与 Replay 共用；Replay evidence 保存最终 Schema 校验结果和选中边集合。

### P1-8 — Plan/恢复失败没有可修订证据路径，与“复用 Patch 路径”声明不符

Orchestrator 在进入 fallback 时直接 return，不记录 FailureObservation；恢复成功后也没有以 `recovered=true/checkpoint_id` 补记。Plan 失败虽可能记录 `workflow_version_id="plan:<id>"`，但 Patch API 路径要求真实 `WorkflowVersion`，并校验 `source_run.workflow_version_id == version_id`，因此 PlanSnapshot 失败无法进入该路径。

建议：Observation 的 execution source 改为 `workflow_version | plan_snapshot` 判别联合，始终绑定 source Run/NodeRun/checkpoint/recovery outcome。Plan 修订应生成新的 PlanSnapshot 或 Workflow draft，不能伪装成 WorkflowVersion patch。

## 3. P2 问题

### P2-1 — Catalog 同时检索同一 Workflow 的所有旧版本，可能让 TraeX 选择陈旧候选

每次发布都新增 `status=published` Manifest，旧 Manifest 不降级；search 对所有 published 行打分，也不按 logical workflow 去重或优先 Component default。相同名称/语义的旧、新版本会一起占用 top-k。

建议：目录将“可发现状态”和“精确 ref 可解析状态”分开。默认搜索每个 logical workflow 只返回 active/default 版本；旧 ref 仍可 describe/run，但不参与普通发现。

### P2-2 — 会话披露和调用配额是进程内全局字典，默认 `sessionId=local` 使会话互相污染

`_SESSION_CALLS/_SESSION_REFS` 没有 TTL、容量上限、持久化或清理；API 默认 session 均为 `local`。一个对话披露的 ref 可被另一个默认会话使用，累计调用还会让后续请求永久达到 429，进程重启又会丢失全部披露状态。

建议：至少将 disclosure session 建模为带 expiry 的持久记录，并强制对话/Agent run 使用唯一 id；本地 Demo 可用数据库表，不需要引入分布式缓存。

### P2-3 — Proposal 动作组没有拓扑闭包校验，分组锁只保留最后动作

非连续动作可被分到同一 ProposedNode。编译第一项时，组内后续动作依赖的组外中间动作尚未生成，依赖会被静默丢弃。`_grouped_action()` 汇总 capability 名称用于 Agent 白名单，但 `capabilityLocks` 只从合成动作保留的最后一个 `capability_ref` 生成。

建议：只允许连续、拓扑闭合的 action group，或先构建完整 action DAG 再收缩图；组锁必须保存所有来源 ref/digest，不能只保存名称并丢弃版本。

### P2-4 — Manifest 的“无 Secret”只过滤字段名，不过滤作者语义字段的值

`_semantic_metadata()` 只允许五个键，这能避免节点 prompt/config 进入 Manifest，但 summary/examples/tags 的字符串值没有 redaction 或 Secret-handle provenance 检查。作者误填 token、URL 凭据或 prompt 内容时仍会进入 search text 和 L0/L1。

建议：发布时对语义字段执行长度、类型和 secret-value 扫描；只允许文本/字符串数组，拒绝 Secret handle、凭据 URL 和超长内容。

### P2-5 — 1000 Manifest 门禁只证明响应有界，没有证明检索成本有界

`search_manifests()` 每次把所有 published Manifest 及完整 manifest JSON 加载到 Python，再逐行评分。随着版本数增长，数据库读取和内存仍线性增长；24KB 只限制最终响应。

建议：把候选预筛选、active-version 过滤和 limit 下推数据库；中文 gram/FTS 可先做本地索引，语义 rerank 仅处理有界候选集。

### P2-6 — E2E-8b 的首次 Browser 证据来自 fixture 宣告，不是实际第一次网页访问

`traex_stub.py` 的 `browser_summary` 模式直接发出 `browser.action.completed`，正文固定为“首次探索正文”，没有真的访问测试页面。第二次复用确实走 Playwright 和非 fake AgentHost，但测试名称及 REPLAY 的“真实 Browser 首次探索”表述需要收窄。

建议：让 TraeX fixture 经 Host Tool Broker 调用真实 Browser Read adapter，或明确把首次阶段标为“Runtime event contract fixture”。测试应把初始页面改为非固定文本并断言轨迹结果来自页面。

### P2-7 — 产品层只有展示，没有 Fragment/Compose/Plan preview 的操作闭环

Chat 页能展示 L0 候选，但不能请求 L1/L2、选择 fragment、编辑/确认 plan 或查看 Plan 编译诊断；Component 页是只读预览；Run detail 也未展示 Plan sourceRefs 的业务折叠层级。

建议：先完成后端 ToolBroker 状态机，再增加一个统一的 Selection/Plan preview 卡片，避免为每个 REST API 单独堆页面。

## 4. 测试结论

`make demo-check` 全部通过：194 后端、28 前端、19 黑盒 E2E、空库迁移和生产构建均成功。需要新增的反例门禁和修复顺序见 [`NEXT_STEPS-008.md`](../STEP/NEXT_STEPS-008.md)。
