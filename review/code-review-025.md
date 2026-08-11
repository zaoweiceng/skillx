# 代码审查报告（025）

审查日期：2026-08-11

代码基线：`fdc8945`。审查对象为 [`REPLAY-024.md`](../REPLAY/REPLAY-024.md) 对应代码。
本轮未修改业务代码。

## 1. 结论

**不通过：1 个 P0、3 个 P1、2 个 P2；另有 1 个真实语义 Provider 验收阻塞。**

Browser 预检隔离、路径脱敏、终态事件回放和 ModelUsage 三阶段字段的主要实现方向正确；当前最高风险不在
执行器，而在“本地机器声称通过、提交本身无法通过”的审计事实链，以及真实语义 readiness 的证明强度不足。

## 2. P0 Finding

### P0-1：发布门禁依赖被 Git 忽略的本地报告，当前提交和干净检出都无法通过

位置：[`.gitignore:19`](../.gitignore#L19)、
[`check_doc_chain.py:14`](../tools/check_doc_chain.py#L14)、
[`check_doc_chain.py:49`](../tools/check_doc_chain.py#L49)、
[`REPLAY-024.md`](../REPLAY/REPLAY-024.md)。

`check_doc_chain.py` 把 `review/design-compliance-review-*` 和 `review/code-review-*` 作为版本链必需输入，
但 `.gitignore` 忽略整个 `review/`，`git ls-files review` 实际为 0。提交还没有重写历史 `REPLAY/`、`STEP/`
从根目录迁移后的相对链接，代码提交中也看不到对应文档修改。

实际结果：当前工作树的 `make demo-check` 在文档链阶段退出 2；用 `git archive HEAD` 构造干净检出后，除大量
历史断链外，还明确缺少两份 024 review。`REPLAY-024` 声称“连续两次通过”依赖未提交本地状态，不能代表提交事实。

根因不是 checker 少加一个目录，而是发布输入没有定义为版本库中的不可变集合。应移除 `review/` ignore 并跟踪报告，
或把正式报告迁入受跟踪的审计目录；校验器只允许依赖 `git ls-files` 可见文件，并在干净检出中运行。

## 3. P1 Findings

### P1-1：主动探测证明了固定协议回环，没有充分证明真实模型语义能力

位置：[`agent_provider.py:213`](../backend/app/application/agent_provider.py#L213)、
[`agent_host.py:685`](../backend/app/application/agent_host.py#L685)。

Provider 探测的 `ok` 只检查固定输出和 `tool.requested/tool.result` 是否出现，没有要求
`result.request_dispatched=true`。同时 AgentHost 为兼容旧 adapter，把任何 `agent.step.completed` 直接当成模型 dispatch。
一个脚本化 adapter 可以生成这些事件和固定答案，使 `workflowSelectionMode=real`，却没有真实模型调用或通配语义能力。

本轮把仓库 fixture 以 `traex_managed` 启动，探测虽然因没有完整 round-trip 返回 `ok=false`，但仍被标为
`requestDispatched=true`，直接证明“通用 step 完成 = 模型 dispatch”的推断对确定性 fixture 也会成立。

最低修复是：探测成功必须要求显式 `model.request.dispatched`、可信 adapter 版本/digest、真实 provider/model 标识和
Usage；删除从通用 step 推断 dispatch 的兼容逻辑。更根本的做法是交付平台维护的可信 adapter，而不是让任意 allowlist
脚本自行声明“真实模型”。

### P1-2：工作流选择失败不会写 ModelUsage，成功路径也丢弃显式 dispatch 事实

位置：[`routes.py:4498`](../backend/app/api/routes.py#L4498)、
[`routes.py:4510`](../backend/app/api/routes.py#L4510)。

选择成功时调用 `record_model_usage()` 没有传 `result.request_dispatched`，因此沿用默认 `dispatched=true`；
若 AgentHost 在模型请求已发出后 timeout/401/连接失败，`except AgentHostError` 直接返回 502，完全没有 Usage 行。
这会漏掉可能已经产生的费用，也破坏 attempted/dispatched/completed 在最关键语义入口的统一含义。

应在 try/except 两条路径都写同一个 call attempt：成功使用真实 `request_dispatched` 并 `completed=true`；失败使用
`exc.request_dispatched`、`completed=false`、错误码和不完整 Usage。不得用函数默认值推断事实。

### P1-3：只补了天气目录元数据，内置语义目录仍不完整

位置：[`seed.py:236`](../backend/app/seed.py#L236)、[`seed.py:282`](../backend/app/seed.py#L282)。

024 任务要求补齐内置工作流的 `summary/useWhen/doNotUseWhen/examples`。本次只修改 `_weather_workflow()`；
例如 `_hybrid_workflow()` 仍只有 `metadata.name`。真实 UI 中“反馈分类”明确显示“适用：未补充 / 不适用：未补充”。

渐进披露的 L0/L1 质量依赖目录元数据。真实 Agent 即使接通，也会面对信息不完整的候选，无法稳定判断何时选用或拒绝。
应为所有 seed/示例工作流建立同一 manifest 校验，发布时缺少必需目录字段直接失败，而不是只修天气样例。

## 4. P2 Findings

### P2-1：SSE 客户端手写不完整事件白名单，运行中会形成可见序号缺口

位置：[`useRunStream.ts:73`](../frontend/src/hooks/useRunStream.ts#L73)、
[`executors.py`](../backend/app/runtime/executors.py#L389)、
[`orchestrator.py`](../backend/app/application/orchestrator.py#L1524)。

前端监听列表没有 `mcp.call.completed`、`artifact.created` 等后端持久事件。手工天气 Run 首次完成时事件从 seq 8
跳到 11；刷新后通过 DB 历史才补齐 seq 9/10。数据库事实源和终态回放已正确，但用户在运行过程中看不到完整 Tool/
Artifact 轨迹。

应使用后端共享/生成的事件类型契约，或让 SSE 同时提供可由 `onmessage` 接收的通用 envelope；新增事件必须经过
契约测试，断言 live store 与最终 DB seq 集合完全一致。

### P2-2：编辑器重复渲染两个工作流图，第二个“总览小图”无标题且高度 420px

位置：[`WorkflowEditorPage.tsx:375`](../frontend/src/pages/WorkflowEditorPage.tsx#L375)、
[`index.css:627`](../frontend/src/index.css#L627)。

可编辑三栏画布后又渲染 `WorkflowGraph compact`，但 compact 高度仍为 420px，且页面没有标题解释其用途。
真实全页截图中同一 DAG 连续出现两次，显著拉长页面并割裂“编辑 → 发布历史”信息流。

如果总览没有独立信息，应删除；若保留，应放入可折叠、有标题的小地图区域，并把高度压到真正的摘要尺寸。

## 5. 外部真实语义验收阻塞

当前未配置 `SKILLX_AGENT_*`、`SKILLX_LLM_*`、OpenAI 或 AceDataCloud Provider。fixture 能证明 Host、Tool、
失败恢复和 UI 协议，但不能证明普通自然语言选择、参数提取、局部组合和自然语言整理质量。
“上海天气如何”本轮正确返回 unavailable，这证明安全降级，不是目标语义链路通过。

## 6. 已确认关闭的 024 前置问题

- Browser 预检在独立后台 task 运行，受限 Browser 不阻塞非 Browser 工作；
- `/health/ready` 不再公开 Chromium 绝对路径；
- RunDetail 终态刷新从 DB 恢复完整持久事件，不创建终态 EventSource；
- LLM/Agent 节点和 Presentation 已能记录 attempted/dispatched/completed；
- 天气工作流目录元数据完整；
- 亮暗主题、组件历史、计时、Token 和主要编辑器控件可用。

## 7. 验证记录

| 检查 | 结果 |
|---|---|
| 后端质量与测试 | Ruff 通过；300 passed |
| 迁移 | `0001→0021` 通过 |
| 前端质量与测试 | check/lint/build 通过；51 passed |
| `make demo-check` | **失败**；文档链阶段退出 2 |
| 正常主机进程 E2E | 37 passed |
| 正常主机 Chromium UI | 2 passed |
| 手工真实进程/UI | 对话、开关、天气/混合组件、历史、Run 回放、编辑器、主题可用；live SSE 有缺口 |
| 干净提交文档链 | **失败**；review 未跟踪且历史链接断裂 |
| 外部真实语义 Provider | 未执行；配置缺失 |

修复范围和验收条件见 [`NEXT_STEPS-025.md`](../STEP/NEXT_STEPS-025.md)。
