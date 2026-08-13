# 设计与规范达成度审查报告（032）

审查日期：2026-08-13

依据：[`REPLAY-031.md`](../REPLAY/REPLAY-031.md)、
[`NEXT_STEPS-031.md`](../STEP/NEXT_STEPS-031.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`2dac66c`，工作区在审查开始时无未提交改动。本轮只新增 032 三份文档，不修改业务代码。
范围仍为单用户 Local Demo；按用户要求，Token 上限和消耗不作为缺陷或通过条件。

## 1. 结论

**不通过。**

REPLAY-031 修复的 SourceSpan、短事务、真实产品门禁拆分均在真实环境得到确认：同消息 Alice 参数可以由真实 TraeX
验证并启动问候工作流，workflow-off 和运行中取消也通过；长模型调用期间 Worker 心跳持续正常，没有再次出现 SQLite
锁冲突。

但本轮实际启动全新 API、Worker、前端和本地网页站点后，通配型主闭环仍不能完成：

1. 真实网页探索读取和总结成功，但一次失败 Tool 尝试加一次成功重试使固化直接返回 422，未创建草稿；
2. 真实 lookup 探索可以生成草稿、静态检查并发布 v1，但手动运行 v1 在 LLM 节点前因无效引用失败；
3. 同一已发布 lookup 工作流在新对话中无法被中文任务发现，系统再次交给通用 Agent，没有发生复用。

所以当前只跑通了“探索—部分轨迹固化—发布”，没有跑通“发布版本执行—新对话发现—不同输入复用”。

## 2. REPLAY-031 达成矩阵

| 目标 | 结果 | 真实证据 |
|---|---|---|
| SourceSpan 与服务器重建 | 通过 | 真实 workflow-on 的 Alice 绑定、`serverVerified` 和问候 Run 全部通过 |
| PendingSelection 冻结消息集合 | 代码与回归通过 | message IDs 进入 digest 并在恢复时校验，不再直接丢弃 |
| workflow-off | 通过 | UI 关闭后真实 TraeX 运行，零 `workflow.*` 调用 |
| running cancellation | 通过 | 真实产品门禁进入 running 后成功 cancelled |
| 消息受理短事务 | 通过 | 长选择和多个 Agent 调用期间 Worker heartbeat 持续写入，日志无 SQLite lock |
| 选择失败持久化 | 基本通过 | 选择目录失败记录可刷新回看；本轮未再出现 SourceSpan 终态失败 |
| Host Tool 事实进入轨迹 | 部分通过 | lookup 的 transform/schema/provenance 正确；Browser 成功动作丢失 pageContract |
| 真实 Tool 重试后固化 | **未通过** | failed browser.read 与 succeeded retry 被一起要求固化，422 `deterministic ProposedNode` |
| 固化草稿静态检查 | 表面通过 | lookup 草稿静态检查通过，但实际运行立刻 `INVALID_INPUT` |
| 发布版本实际执行 | **未通过** | `${node_1.output}` 不能被运行时解析，LLM 未调用，Run failed |
| 已发布工作流渐进发现 | **未通过** | 中文任务未召回英文 `Table Lookup` manifest，英文查询才可召回 |
| 新对话不同输入复用 | **未通过** | q4 新对话走 `continue_agent`，没有选择已发布 v1 |
| 组件运行与历史 | 通过 | 失败 Run 和成功问候 Run 均在组件历史可回看 |
| 亮暗主题、导航和主要按钮 | 通过 | 两套主题视觉正常，筛选、编辑、审阅、检查、发布、运行和复制均可点击 |
| 通配型完整闭环 | **未通过** | 已发布固化版本既不能稳定执行，也不能从同意图新对话发现 |

## 3. 自动门禁

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；343 backend、55 frontend、37 独立进程 E2E、迁移 0001→0023、build 与文档链 |
| `make browser-ui-check` | 通过；2 passed |
| `make traex-real-check` | 宿主权限下通过；1 passed，未 skip |
| `make traex-real-product-check` | 通过；workflow-on、workflow-off、cancellation 共 3 passed |

受限 sandbox 首次真实 probe 返回 `activeAuthenticated=false`；使用用户已授权的宿主登录态后立即通过，因此这是执行环境
权限差异，不计入产品缺陷。

## 4. 全新真实栈验收

本轮使用全新 SQLite 和 Artifact 根目录，实际启动 API、独立 Worker、本地网页站点和 Vite。系统页主动探测显示
Provider=`real`、Agent Runtime=`available`、Worker=`available`、Browser Runtime=`available`。

### 4.1 真实网页探索：任务完成，固化失败

对话默认开启工作流，输入：

`访问 http://127.0.0.1:18042/article，阅读并用一句中文总结网页正文`

目录没有合适候选后，真实 Agent 调用 `browser.read`。第一次因模型给出的 PageContract 不合法失败，第二次重试成功，
页面返回“季度营收增长 12%”的正确总结，耗时 50.8s。点击“固化为组件”后返回：

`deterministic ProposedNode 必须恰好包含一个 action`

真实轨迹保留了 failed 和 succeeded 两个 browser action。固化资格只统计 succeeded action，但 IR 和 DistillationProposal
又要求覆盖两个 action，且提议阶段没有把 status 告诉模型。结果是失败尝试被错误当作应复现的业务步骤。由于异常发生在
创建 Distillation 之前，数据库中没有 blocked 记录，页面刷新后也没有可审阅草稿。

此外，Host 虽验证并执行了第二次请求中的 PageContract，归一化时却从 arguments 删除该字段，又没有放进 Host-owned
observations；即使先过滤失败 action，Browser compiler 仍会因缺 PageContract 而退回 Agent 节点。

### 4.2 真实 lookup：发布成功，运行失败

工作流关闭后输入：

`查询标识 q2 对应的本地示例数据并用一句中文告诉我结果`

真实 Agent 调用 `lookup.table` 并正确返回第二季度结果。固化生成 `input → transform → llm → output`，Presentation
提议可接受，静态检查通过并成功发布不可变 v1。

在组件页用新参数 `q4` 手动运行 v1：input 和 transform 成功，`final-response` 在模型调用前失败：

`INVALID_INPUT: 无法解析路径段 output`

草稿把完整上游证据写成 `${node_1.output}`。表达式运行时只特殊处理 `output.` 前缀，不支持恰好为 `output` 的整体对象
引用；静态检查也只检查 `output.<field>`，因此错误定义被允许发布。Run 的 `modelCalled=false`，不是 Provider 问题。

### 4.3 新对话语义复用失败

发布后新建默认开启工作流的对话，以 `q4` 请求相同任务。目录只披露天气、Agent 摘要和费用提交三个旧候选，未披露刚
发布的 Table Lookup，最终 `continue_agent`。

数据库中 Table Lookup manifest 已是 published，但 CapabilityDescriptor Agent 只看到 action kind/name/I/O 字段，
没有看到去实例化后的原始任务语义，也没有语言约束，因此生成全英文 manifest。目录 `_score()` 是字符/词法重合且要求
score>0。对同一个库直接验证：中文查询返回三个旧中文候选，英文 `table lookup key value` 才返回 Table Lookup。

这违反“已固化工作流对 Agent 渐进可见，并由 Agent 选择复用”的设计目标。

## 5. 前端验收

实际点击验证通过：

- 亮色/暗色切换，两种配色、对比度、卡片、表单和 DAG 可读；
- 对话历史、新建对话、工作流开关、计时、Token、目录 timeline、取消入口；
- 组件筛选、组件参数表单、RunDetail、失败证据、成功结果复制和调用历史；
- 固化、Presentation 接受、静态检查、发布版本、工作流与能力/系统导航；
- 浏览器 console error/warning 为空。

前端没有阻止主流程；失败来自后端固化、表达式契约和目录召回。

## 6. 最终判定

031 已修复上一轮的绑定、短事务和真实门禁问题，但真实通配型闭环仍在固化、发布执行和语义发现三个位置中断。
032 **不通过**。下一步只修复这些现有流程缺陷，见 [`NEXT_STEPS-032.md`](../STEP/NEXT_STEPS-032.md)。
