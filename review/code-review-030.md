# 代码审查报告（030）

审查日期：2026-08-12

代码基线：`a59a7c1`。审查对象为 [`REPLAY-029.md`](../REPLAY/REPLAY-029.md) 对应的工作区代码。
本轮未修改业务代码。

## 1. 结论

**不通过：2 个 P0、1 个 P1、1 个 P2。**

029 已有效修复严格 JSON 终态、单目录描述符、Presentation 决定一致性、Settings 状态、DAG 空标签和复制反馈。
但真实默认工作流入口仍不能使用同一句中的必填参数，且新增的真实产品门禁自身不可运行。目录失败缺少诊断，
使失败重试的根因无法从运行历史判断。

## 2. P0 Findings

### P0-1：选择终态允许遗漏已明确给出的必填绑定

位置：[`plans.py:70`](../backend/app/application/plans.py#L70)、
[`plans.py:122`](../backend/app/application/plans.py#L122)、
[`routes.py:4512`](../backend/app/api/routes.py#L4512)、
[`routes.py:4991`](../backend/app/api/routes.py#L4991)。

`bindings` 与 `bindingSources` 都以空值为默认。`validate_binding_source_coverage()` 只比较二者现有字段集合；二者
同时为空就合法。传给 Agent 的 output schema 也没有所选 Workflow 的动态 required 字段，因为真实输入 Schema
要等 Agent 选中 ref 后才在 `_prepare_selection()` 中加载。

真实 UI 输入 `请问候名叫 Alice 的人，姓名参数是 Alice` 后，Agent 正确选择问候 workflow，却返回空绑定；
服务端发现 required `name` 缺失，只能把结果改写为 `ask_user`。第二条消息回复 Alice 后才成功。这不是证据校验
太严格，而是选择协议把“选能力”和“按该能力 Schema 提取参数”压在一个无法表达动态约束的终态中。

应拆成受控的两阶段语义过程：目录 Agent 选定 ref 后，服务端解析其真实 input schema，再用同一 Agent Runtime
执行一次严格参数提取；输出必须包含每个可从当前消息确认的 required binding 及一一对应的 source。服务端继续
验证 messageId、quote、confidence、normalization 和 JSON Schema。若模型遗漏当前消息中可验证的值，应做一次
结构化纠正或返回准确的 selection failure，不能伪装成用户缺参。禁止用关键词或领域模板兜底。

### P0-2：真实产品门禁使用废弃配置，取消用例又提交无效能力集

位置：[`test_traex_product_real.py:23`](../backend/real_provider_tests/test_traex_product_real.py#L23)、
[`test_traex_product_real.py:95`](../backend/real_provider_tests/test_traex_product_real.py#L95)、
[`routes.py:277`](../backend/app/api/routes.py#L277)。

门禁设置 `SKILLX_AGENT_PROVIDER_TYPE=real` 和 `SKILLX_TRAEX_COMMAND`。当前 runtime 配置将该 legacy 组合解析为
不可用的 sidecar 路径，而不是 REPLAY 声称要测试的 builtin auto-discovery；第一个用例因此在 provider probe
得到 409 `PROVIDER_PROBE_UNAVAILABLE`。

取消用例又显式发送 `allowedCapabilities: []`。API 的 fail-closed 契约要求有效能力集非空，所以在创建运行时直接
返回 422 `AGENT_CAPABILITY_UNAVAILABLE`，根本没有进入 running/cancelled。

实际以当前支持的 auto-discovery 配置启动全新栈后，真实 probe、workflow-off 和带
`lookup.table` 的 running cancellation 均可通过，说明这里是门禁实现错误而非外部环境阻断。应让测试直接复用
生产 runtime resolver，断言最终 runtimeType/source/provider，再用最小只读 capability 触发可取消任务。该目标
必须在本地登录可用时真实执行，而不是 skip、fixture 或人工脚本替代。

## 3. P1 Findings

### P1-1：目录调用失败只显示“失败”，缺少可审计诊断

位置：[`workflow_tool_broker.py:272`](../backend/app/application/workflow_tool_broker.py#L272)、
[`routes.py:4543`](../backend/app/api/routes.py#L4543)。

真实 workflow-on timeline 中 search/describe 都出现失败后重试，失败项只显示 catalog capability，披露 level 为
空，未提供 action、参数校验错误或 disclosure 状态诊断；持久化披露记录又只保留成功调用。Agent 最终虽找到候选，
但用户和开发者无法从运行历史判断为何失败、是否重复了相同错误。

单一 `action` 工具方向正确，但 broker 必须为 schema/adaptor/state-machine 三类失败生成去敏诊断事件：catalog
action、错误码、session 当前级别、可重试性和参数 digest。失败也必须进入 Turn timeline/审计，且相同确定性错误
不得重复重试。不要记录用户正文、ToolResult 全文或 Secret。

## 4. P2 Finding

### P2-1：无动作的成功回答仍展示不可用的“固化为组件”

位置：[`ChatPage.tsx:603`](../frontend/src/pages/ChatPage.tsx#L603)、
[`distillation.py:379`](../backend/app/application/distillation.py#L379)。

前端只判断 ConversationRun `status === 'succeeded'` 就显示固化按钮。真实无工具回答成功后点击，后端正确返回
“轨迹不含任何可分析动作”。应由运行摘要暴露 `distillable`/action count/reason，前端只在可固化时启用按钮；
不可固化时可显示简短原因，但不应让用户通过一次失败请求才得知。

## 5. 已确认有效的 029 修复

- `AgentHost` 统一包裹业务 prompt 和 canonical output schema；真实工作流关闭回答通过；
- WorkflowSelection 只授予一个 `workflow.catalog` 描述符，服务端仍校验 disclosure session 与 ref；
- trajectory 已携带 result consumer、entrypoint、top-level/Fragment 事实，Presentation 只接收去实例化上下文；
- accepted/modified/rejected 的前后端语义一致性门禁已补齐；
- Settings 主动认证提示不再矛盾，probe 与 selection profile 分开显示；
- WorkflowGraph/EditableCanvas 不再渲染 `null`，复制操作有成功反馈；
- 确定性发布、真实 Worker 执行、RunDetail 和组件调用历史均可正常使用。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；331 backend、53 frontend、37 E2E |
| `make browser-ui-check` | 最终通过；2 passed |
| `make traex-real-check` | 通过；1 passed，未 skip |
| `make traex-real-product-check` | **失败；2 failed** |
| 正确 auto-discovery 下真实 probe | 通过；31,734 Token、strict JSON、ToolResult |
| 默认工作流真实 UI | **未一次完成**；43.3s / 104,535 Token 后错误追问 `name` |
| 补充消息后的确定性运行 | 通过；0.6s / 0 Token，返回 `你好，Alice` |
| 工作流关闭真实 UI | 通过；9.3s / 15,845 Token，零 `workflow.*` |
| 真实 running cancellation | 通过（人工修正门禁配置及能力集） |
| 组件编辑/发布/运行/历史 | 通过；真实 API、Worker、Vite、Chromium |
| 页面主题、导航、筛选、复制 | 通过；console errors/warnings = 0 |

修复任务和验收条件见 [`NEXT_STEPS-030.md`](../STEP/NEXT_STEPS-030.md)。

Token 上限与消耗在当前 Demo 阶段不作为代码缺陷或通过条件；表中的 Token 仅保留为真实调用和前端统计的观测证据。
