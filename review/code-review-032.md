# 代码审查报告（032）

审查日期：2026-08-13

代码基线：`2dac66c`。审查对象为 [`REPLAY-031.md`](../REPLAY/REPLAY-031.md) 对应代码。
本轮未修改业务代码；Token 上限和消耗不纳入缺陷判定。

## 1. 结论

**不通过：3 个 P0、1 个 P1、2 个 P2。**

SourceSpan、真实产品门禁和 SQLite 短事务修复有效。剩余问题集中在主闭环，不涉及新增产品能力。

## 2. P0 Findings

### P0-1：固化 IR 把失败重试当作可执行步骤，并丢失已验证 Browser Contract

位置：[`distillation.py:35`](../backend/app/application/distillation.py#L35)、
[`distillation_ir.py:165`](../backend/app/application/distillation_ir.py#L165)、
[`distillation.py:999`](../backend/app/application/distillation.py#L999)、
[`agent_host.py:1458`](../backend/app/application/agent_host.py#L1458)、
[`action_compilers.py:138`](../backend/app/application/action_compilers.py#L138)。

`distillation_eligibility()` 只统计 status=succeeded 的动作，说明失败尝试不是可复用业务步骤；但 `build_action_ir()` 对
trajectory.actions 全量建 IR，`_request_distillation_proposal()` 同样全量发送且不包含 status，并要求“每个 action
恰好归属一个节点”。真实 TraeX 的一次失败 browser.read + 一次成功重试因此被提议为同一个 deterministic 节点，
`_validate_proposal()` 在草稿创建前直接抛 422。

审计轨迹保留所有尝试是正确的；可执行固化 IR 应只消费成功终态。对 effect=none 的失败尝试应保留为 attempt diagnostics，
不参与节点覆盖；对副作用未知或可能已提交的失败尝试应阻止自动固化，不能静默删除。

同一真实轨迹还暴露第二个信息损失：`_normalize_action()` 对 Browser ToolResult 删除 `pageContract`，但 Broker 没有把已
验证的 contract 写入 observations。Browser compiler 只能看到 `resultSource`，无法生成确定性 Browser 节点。Host 应在
执行前验证 contract，执行后把规范化 contract 作为 Host-owned evidence 保存；模型自报 observations 不能成为权威事实。

### P0-2：固化生成运行时无法解析的整体输出引用，静态检查仍允许发布

位置：[`distillation.py:438`](../backend/app/application/distillation.py#L438)、
[`expressions.py:45`](../backend/app/application/expressions.py#L45)、
[`compiler.py:378`](../backend/app/application/compiler.py#L378)。

生成式最终节点把证据映射为 `${node_1.output}`。表达式解析器只在 path 以 `output.` 开头时去除命名空间；path 恰好等于
`output` 时会在真实上游对象中查找名为 output 的字段，产生“无法解析路径段 output”。静态检查只对
`output.<field>` 校验 Schema，又不会拒绝整体 `output`，所以无效 DAG 通过检查并发布。

真实 q4 Run 中 input/transform 成功，LLM 节点 input_json 为空并以 INVALID_INPUT 失败，模型调用数为 0。应统一定义
`${node.output}` 的语义：若表示整个节点输出，解析器、编译器、前端变量选择器和文档必须共同支持；若不允许，则
Distillation 必须生成显式字段对象，静态检查必须阻止发布。不能只改错误文案。

### P0-3：固化 manifest 与源任务语言脱节，词法目录无法发现刚发布的工作流

位置：[`distillation.py:958`](../backend/app/application/distillation.py#L958)、
[`workflow_catalog.py:626`](../backend/app/application/workflow_catalog.py#L626)。

CapabilityDescriptor Agent 只收到 kind、capability 和 I/O 字段，没有收到去实例化任务意图或语言；真实 TraeX 为中文
lookup 任务生成了全英文 name/summary/useWhen/tags/examples。目录所谓“语义召回”实际是字符/词法 `_score()`，且
`score > 0` 才披露。中文同意图查询无法与英文 manifest 重合，于是新对话只看到旧中文 seed 候选。

同库实测：`查询标识 q4 对应的本地示例数据` 返回天气、Agent 摘要和费用提交；`table lookup key value` 才返回新
Table Lookup。应把服务器已去实例化的 task intent、输入类别和源语言交给 Descriptor 阶段，并在发布门禁中验证“源
任务的同意图查询能够召回自己”。不得添加 lookup、天气或语言专用模板。

## 3. P1 Finding

### P1-1：非法固化提议在持久化前回滚，UI 只能显示一次性错误

位置：[`routes.py:6331`](../backend/app/api/routes.py#L6331)、
[`distillation.py:845`](../backend/app/application/distillation.py#L845)、
[`ChatPage.tsx:467`](../frontend/src/pages/ChatPage.tsx#L467)。

`distill_conversation_run()` 在创建 Distillation/Workflow 前校验 proposal。任何 DistillationError 都由路由 rollback 后
返回 422。真实网页固化失败后数据库没有 blocked Distillation，前端只在页面底部显示错误；刷新后没有诊断记录，也没有
可审阅草稿。代码虽然实现了 `result.status == blocked` 的 UI，但该错误路径永远到不了它。

对可分析但不可发布的轨迹应持久化 blocked + diagnostics；只有来源 Run 不合法等请求级错误才直接 4xx。修复 P0-1 后，
失败重试不应再触发 blocked；本项用于保证未来真实模型提议不合约时仍可审计，而不是增加新流程。

## 4. P2 Findings

### P2-1：固化单测没有运行“生成式输出”发布版本

位置：[`test_distillation.py:140`](../backend/tests/test_distillation.py#L140)、
[`test_distillation.py:376`](../backend/tests/test_distillation.py#L376)。

新测试断言 Host lookup + generative provenance 生成可发布图，但只检查结构；真正执行不同输入的测试使用 direct provenance，
不会生成 final-response LLM 节点。因此 `${node.output}` 的运行时错误没有被发现。需要对编译产物实际 publish、Worker
运行并断言模型节点收到非空 evidence 和 Run succeeded。

### P2-2：现有 UI/真实产品门禁没有覆盖真实“新对话发现并复用”

位置：[`test_ui_main_flow.py:51`](../backend/browser_e2e/test_ui_main_flow.py#L51)、
[`test_traex_product_real.py:57`](../backend/real_provider_tests/test_traex_product_real.py#L57)。

Browser UI 使用定制 fixture 指令，发布后直接到组件页手动运行，不经过新对话语义发现；真实产品门禁只覆盖 seed 问候、
workflow-off 和 cancellation。于是 fixture 的理想 Browser contract、无失败重试和英文/中文召回差异都未被验证。

门禁应增加一条现有主流程：真实或严格录制轨迹探索 → 固化 → 审阅 → 发布 → 新对话同意图不同输入 → 选择该 ref →
Run succeeded。失败时要分别报告固化、执行、发现阶段，不能用手动组件运行代替语义复用。

## 5. 已确认有效的 031 修复

- SourceSpan 服务器重建、字段级诊断和真实 Alice 绑定；
- PendingSelection 冻结消息集合与 digest 校验；
- workflow-on/off/cancellation 三个真实产品用例独立通过；
- 消息受理在外部调用前 commit，真实长调用期间无 SQLite lock；
- 选择目录失败 timeline、对话历史和 Usage 可持久化；
- lookup Host descriptor、argument provenance 和 generative output provenance 正确进入 trajectory；
- 主题、组件历史、RunDetail、复制、Presentation、静态检查和发布 UI 可用。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；343 backend、55 frontend、37 E2E |
| `make browser-ui-check` | 通过；2 passed |
| `make traex-real-check` | 宿主登录态下 1 passed |
| `make traex-real-product-check` | 3 passed |
| 真实网页探索 | 成功读取/总结；**固化 422** |
| 真实 lookup 固化/发布 | 草稿、Presentation、检查、发布成功 |
| 新发布 lookup 手动运行 | **失败**；final-response `INVALID_INPUT`，模型未调用 |
| 新对话 lookup 复用 | **失败**；未披露新 manifest，走 continue_agent |
| SQLite/Worker 并发 | 通过；日志无 `database is locked`，无遗留 processing/running |
| 主题/主要 UI/console | 点击正常；console error/warning = 0 |

下一版修复任务见 [`NEXT_STEPS-032.md`](../STEP/NEXT_STEPS-032.md)。
