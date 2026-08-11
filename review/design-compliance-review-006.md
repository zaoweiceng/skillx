# 设计与规范达成度审查报告（006）

审查日期：2026-08-04  
审查基线：提交 `2b4dc5f`、[`REPLAY-005.md`](../REPLAY/REPLAY-005.md)、005 三份文档及 `docs/` 下设计/规范文档。  
范围约束：单用户 Local Demo；鉴权、RBAC、多租户、生产级沙箱和分布式部署不作为阻断项。

## 1. 结论

005 新增的领域对象和主路径是真实存在的：两个 Agent 入口已经共用 `AgentHost`，MCP 多 Tool 分类会克隆完整版本，Action IR/Compiler Registry、ExecutionCheckpoint、RecoveryDriver、WorkflowPatch 草稿和对应产品视图都已进入代码；完整门禁也全部通过。

但 [`REPLAY-005.md`](../REPLAY/REPLAY-005.md) 的“八项系统不变量全部满足”以及 README 将“通用 Agent 探索与固化”“通用失败接力”标为 `available`，**不符合实际运行语义**。当前实现完成的是通用框架骨架，不是可端到端复用的通配工作流：

1. 动作之间只有调度依赖，没有可靠的数据依赖；网页读取结果不会成为后续 TraeX 总结输入，生成的 Browser 节点也不符合现有 Browser Executor 的读取契约。
2. fallback target 仍是普通 DAG 节点；它会在正常路径被提前调度，失败后 `RecoveryDriver` 又另起一次 Agent，恢复目标没有“只在失败时激活”的执行语义。
3. checkpoint 虽保存了部分输出，但恢复 prompt 只给节点名和错误码，未把已完成内容、失败输入、Artifact 或剩余节点定义交给 TraeX；E2E 使用固定恢复值，不能证明从已有上下文续跑。
4. failure fingerprint 与可复现性判断没有接入生产路径；任意版本都可直接提交 WorkflowPatch，所谓 replay 只是静态 Compiler 校验，不是失败场景回放。

因此，对用户提出的通配型需求，006 的直接判断仍是：**当前版本不能端到端实现。**它可以人工编写并运行部分确定工作流，也能演示“失败后让另一个 Agent 返回 fixture 输出”，但不能证明首次探索可被正确固化、第二次可读取变化后的真实数据并交给 TraeX、意外中断时携带上下文从剩余节点继续，以及只在可复现时受控修订。

准确版本定位：**基础 Workflow Runtime 可用；统一 Agent Host 和治理骨架已建立；通配固化、上下文恢复和补丁学习仍为 partial。**

## 2. REPLAY-005 声明回归

| REPLAY-005 声明 | 006 判定 | 说明 |
|---|---|---|
| 两个 Agent 入口共用唯一 Host | 基本完成 | 权限、事件和输出校验已统一；终态 Token 可覆盖累计值且不复核，取消也只能在下一条事件到达时生效 |
| 事件/Artifact 不存 Secret 明文 | 未按声明完成 | audit event 已脱敏，但 `trajectory_json` 和轨迹文件仍保存原始 action 参数、结果和输出 |
| MCP 多 Tool 修订保持完整版本 | 新数据完成，升级不完整 | 新分类逻辑可用；0009 将旧 `digest` 直接回填为新 `contract_digest`，已有 0008 数据首次 discovery 会产生伪漂移 |
| NormalizedAction IR 不丢动作 | 结构上完成，语义部分 | 未识别动作保留为 Agent；但 prior-output provenance 未生成，Agent/LLM 节点忽略 action bindings |
| Agent 提议、Compiler 最终裁决 | 未形成闭环 | `DistillationProposal` 和 `proposed_strategy` 只有类型与 round-trip 测试，生产固化没有消费提议 |
| Browser 与非网页共用通配框架 | 类型层面完成，运行层面未完成 | 两个合成单测共用类；网页图没有发布/运行，Browser contract 与 Executor 不兼容 |
| 标准 checkpoint 可从剩余子图接力 | 未按声明完成 | snapshot 只是 id/output 摘要；恢复 prompt 不含输出值，失败 NodeRun 还会被改写为成功/跳过 |
| 非幂等副作用不重放 | 测试特例成立，系统不变量未证明 | E2E 只统计一个 fixture sensitive 节点；fallback Agent 正常路径会提前执行，且恢复领取没有原子租约 |
| 可复现故障才提出 WorkflowPatch | 未接入生产 | 判断函数仅被单测调用；API/UI 不要求 run、checkpoint、fingerprint 或复现证据 |
| Patch 不扩权、回放后生成草稿 | 部分 | 会创建新草稿且不改旧版本；但 `config` 可修改能力锁/白名单，replay 仅为静态编译 |
| 检查点/补丁产品视图 | 部分完成 | 页面和 API 已有；`fallbackTarget` 返回 failedNode，展示证据错误，人工审批恢复未实现 |

## 3. 通配型复用需求判定

需求本质不是“缓存一次网页步骤”，而是把一次带概率性的探索，转换为一份**数据依赖明确、能力锁定、可重新取数、可局部恢复、可审计修订**的执行计划。网页只是 Action Adapter 之一。

| 所需能力 | 当前证据 | 006 判定 |
|---|---|---|
| 首次探索形成可编译 Action IR | 能记录动作顺序、kind 和部分能力证据；无法从实际 action 参数建立 prior-output/artifact 绑定 | 部分 |
| Agent 判断节点分区，Compiler 确定性裁决 | 有 Proposal 类型和 Registry；Proposal 未进入固化服务，分区仍由逐 action 默认规则决定 | 未完成闭环 |
| 部分或整个流程固化且不丢数据流 | Compiler 会生成混合节点；Agent/LLM 输入固定为 `${input.message}`，前序结果未传递 | 不满足 |
| 第二次复用重新执行确定步骤 | lookup 样例可随输入变化；网页和真实 mixed graph 未运行 | 仅特例满足 |
| 网页重新访问后把正文和提示词交给 TraeX | Browser 编译生成 `runtime=contract/action=read`，现 Executor 只支持 Playwright 表单或 `observedPage`，也没有正文抽取输出 | 不满足 |
| 非网页操作使用同一框架 | 合成 IR 单测共用类型；缺真实文件/API/MCP 首次探索→发布→换输入运行的黑盒链路 | 未证明 |
| 重试耗尽后携带上下文接力 | checkpoint 存了部分值，但 prompt 只发送节点 id；Agent 无法看到已完成内容或失败输入 | 不满足 |
| 从剩余节点继续且不篡改历史 | 恢复会把原失败 NodeRun 改成 `succeeded/skipped`；remaining 只是节点 id 列表 | 不满足 |
| 根据可复现性决定是否修订 | 有纯函数，无持久 FailureObservation、聚合或 API 门禁 | 不满足 |
| 修订不静默扩权且经真实回放 | 旧版本不变、需人工发布；但 patch 可改能力配置，未执行真实 replay | 不满足 |

## 4. 设计文档达成度

| 设计承诺 | 006 状态 | 主要缺口 |
|---|---|---|
| Workflow Compiler、不可变版本、持久 Worker | 基本完成 | fallback-only 节点没有独立激活语义；Executor 未捕获异常可击穿 Worker 循环 |
| SkillX/MCP/Transform 确定执行 | 基本完成 | MCP 0008→0009 数据升级不正确；自动固化还不能建立真实能力数据流 |
| LLM/Agent 只在显式节点介入 | 主路径部分完成 | fallback target 会作为普通无入边节点在正常路径执行 |
| 未知流程探索后固化 | 部分 | IR/Registry 已有；Proposal、provenance、Browser Read/Extract、输出映射未闭环 |
| 长任务取消、重试、恢复 | 部分 | Recovery 无 claim/lease；Agent 无事件期间取消不及时；失败历史会被覆盖 |
| fallback 必须显式声明 | Schema/Compiler 已接入 | 调度器未区分 normal/fallback edge；requireUserApproval 只阻止自动恢复，没有审批继续路径 |
| Browser 确定执行与局部恢复 | 表单路径可用，读取路径不可用 | 编译器输出的读取契约与 Executor/PageContract 不一致 |
| 失败后不得静默修改已发布版本 | 已完成当前边界 | 新建 draft + 人工发布成立；补丁产生依据、权限 diff 和真实回放未完成 |
| Artifact/审计可恢复 | 部分 | Agent 轨迹不进入标准 Artifact 表，原始内容仍同时进入数据库和磁盘 |
| 产品可解释恢复与版本治理 | 部分 | 有恢复/补丁视图，但证据字段错误，缺 recovery attempt、审批和 replay 结果 |

## 5. 测试与门禁

| 命令/阶段 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| Ruff | 通过 |
| 后端 Pytest | 169 passed |
| Alembic 空库升级 | 0001 → 0010 通过 |
| TypeScript check / ESLint | 通过 |
| Vitest | 28 passed |
| Vite production build | 通过 |
| 黑盒 E2E | 10 passed |

门禁通过说明现有测试定义的行为没有回归，不能证明 REPLAY 的全部系统不变量。尤其需要注意：

- [`test_wildcard_pipeline.py`](../backend/tests/test_wildcard_pipeline.py) 只检查合成 IR 生成的节点类型，没有发布并运行网页图；其中还存在恒真条件 `... or True`。
- 网页样例的 PageContract 缺少 Executor 所需字段，summary 动作使用常量“正文”，没有消费 Browser 输出。
- E2E-7 的 Recovery Agent stub 固定返回“恢复值”，结果不依赖 checkpoint 中任何已完成输出。
- 迁移测试只覆盖空库 0001→0010，没有覆盖带 MCP Snapshot 数据的 0008→0010。

## 6. 006 验收定义

只有以下证据同时成立，README 才可把通用固化与失败接力标为 `available`：

1. **真实混合固化**：第一次 TraeX 运行产生 Browser/MCP/文件与 Agent/LLM 混合动作，所有 prior-output/artifact 绑定都进入 IR；发布后第二次运行的数据流与首次轨迹等价。
2. **动态网页样例**：本地页面内容改变后，固化工作流重新读取新正文，并将新正文和提示词传给 TraeX；输出必须依赖新内容，不能使用首次结果或 fixture 常量。
3. **非网页样例**：同一套 IR、Compiler、Checkpoint、RecoveryDecision 和 Patch 治理完成文件或 MCP 任务的首次探索、固化、换输入复用和中途恢复。
4. **真实恢复语义**：fallback Agent 正常路径不运行；失败后只领取一次，能读取已完成输出/Artifact 与失败上下文；原失败 attempt 保持不可变，恢复产生新 attempt/substitution，副作用 exactly-once。
5. **受控修订**：只有持久化、可复现的失败证据能创建 Patch；补丁不能扩大能力锁；在安全 fixture 上真实重放成功后才生成 draft，仍由人工发布。
6. **升级与并发**：带数据的 0008→0010 升级稳定；两个 Recovery Worker 不会重复调用 Agent；审批型 fallback 可被人工继续或拒绝。

详细修复顺序见 [`NEXT_STEPS-006.md`](../STEP/NEXT_STEPS-006.md)。
