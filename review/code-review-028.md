# 代码审查报告（028）

审查日期：2026-08-11

代码基线：`d6431c0`。审查对象为 [`REPLAY-027.md`](../REPLAY/REPLAY-027.md) 对应工作树代码。
本轮未修改业务代码。

## 1. 结论

**不通过：2 个 P0、2 个 P1、1 个 P2。**

受信目录、天气特例移除、dispatch 审计和 MCP 名称碰撞修复有效，但真实 TraeX 探测和通用 Agent 均无法完成。
Presentation 的实现也把“通用”误解成“统一静态启发式”，没有实现由 Agent 根据任务目的判断并让用户审阅。

## 2. P0 Findings

### P0-1：真实 TraeX 的不可避免输入上下文大于 Host 探测预算，Provider 永远无法变为可用

位置：[`agent_provider.py:353`](../backend/app/application/agent_provider.py#L353)、
[`agent_provider.py:374`](../backend/app/application/agent_provider.py#L374)、
[`agent_host.py:738`](../backend/app/application/agent_host.py#L738)、
[`agent_run.py:89`](../backend/app/application/agent_run.py#L89)。

Provider probe 把 `max_total_tokens` 固定为 2,000，Host 在真实模型返回后用 input+output 总数执行上限。实际内置 TraeX
最小探测已产生 input 31,314、output 291、total 31,605，并且完成显式 dispatch 和部分协议处理；Host 随后抛
`TOKEN_BUDGET_EXCEEDED`。结果不是偶发超长回答，而是 TraeX 自身上下文基线已经高于预算一个数量级。

通用 Agent 默认总预算也只有 30,000，低于本次最小调用输入基线，因此即使严格 JSON 问题关闭，普通调用仍可能在
模型完成后被统一判失败。提高 2,000 到任意更大常量只能隐藏根因并增加不可见费用。

应先确定 `--ignore-user-config`/空 plugins/skills 后仍被加载的系统上下文和工具描述，缩减 bridge 基线；随后把
`maxInputTokens/contextBudget`、`maxOutputTokens` 和 `maxBillableTokens` 分离，预算在请求前冻结，并为内置 TraeX
采用经过真实测量的 profile。若 30k 输入不可避免，Settings 在付费探测前应显示估算，而不是以 2k 名义发出 31k 调用。

### P0-2：bridge 不把 JSONL `error/turn.failed` 当作终态，失败会被误报为严格 JSON/版本问题

位置：[`traex_builtin.py:270`](../backend/app/runtime/traex_builtin.py#L270)、
[`traex_builtin.py:295`](../backend/app/runtime/traex_builtin.py#L295)、
[`traex_builtin.py:302`](../backend/app/runtime/traex_builtin.py#L302)、
[`traex_builtin.py:311`](../backend/app/runtime/traex_builtin.py#L311)。

JSONL 读取循环只提取 dispatch、`turn.completed` Usage 和 agent message。进程结束后，只有 `returncode != 0` 才调用
`_error_code(stderr, raw_events)`；其余情况直接读取 output file 并 `json.loads()`。原生 CLI 可以通过 JSONL 返回
`error`/`turn.failed` 或结束时没有合法 output。此时真实认证/模型/协议错误被覆盖成“TraeX 未返回严格 JSON 终态”，
最终 UI 让用户升级版本。

实际关闭工作流的通用 Agent 已复现：事件中有 dispatch，但没有 JSON，5.3 秒后以
`AGENT_PROTOCOL_INCOMPATIBLE` 失败。直接 CLI 又明确报告 token expired，当前投影无法可靠说明同一次失败发生在哪一层。

bridge 必须把 native JSONL 状态机作为事实源：识别并冻结 `thread/turn started/completed/failed`、error code/message、
Usage 和 output；任何 `turn.failed` 都先规范化错误，再考虑进程退出码。只有成功终态缺少/违反 schema 才能归类为
malformed output；“版本不兼容”必须有能力协商或未知事件证据。

## 3. P1 Findings

### P1-1：Presentation 的受众判断来自节点类型，不是任务目的或 Agent 提议

位置：[`distillation.py:400`](../backend/app/application/distillation.py#L400)、
[`distillation.py:434`](../backend/app/application/distillation.py#L434)、
[`distillation.py:448`](../backend/app/application/distillation.py#L448)、
[`distillation.py:583`](../backend/app/application/distillation.py#L583)。

`machine_only` 在业务节点全部属于 transform/file/artifact/condition 时为 true，随后直接冻结
`enabled=false/audience=internal`；其他节点则按 Output Schema 在 template/LLM 间选择。这个规则没有读取用户任务
目的、结果消费者、顶层与 Fragment 调用位置，也没有询问 Distillation Agent。

因此同一个 file/transform 节点直接响应用户时会被错误关闭，而 MCP/Browser 作为组合中间步骤时会被错误开启。
标量输出统一生成 `field=value` 也只是格式转换，不一定是适合非专业用户的自然语言。天气特例虽然删除，但系统仍然
无法通配真实场景。

应扩展受限 Distillation Agent 输出契约，让其基于去实例化的 task intent、consumer、entrypoint/fragment、I/O schema
和数据流提出 Presentation；静态代码只校验 schema、预算和安全，并在 Agent 不可用/低置信时默认关闭或要求人工
决定，不能用节点技术类型代替语义。

### P1-2：所谓“可审阅提议”没有进入前端审阅流程，策略在草稿中已被自动冻结

位置：[`distillation.py:592`](../backend/app/application/distillation.py#L592)、
[`distillation.py:796`](../backend/app/application/distillation.py#L796)、
[`ChatPage.tsx:457`](../frontend/src/pages/ChatPage.tsx#L457)、
[`WorkflowEditorPage.tsx:454`](../frontend/src/pages/WorkflowEditorPage.tsx#L454)、
[`WorkflowEditorPage.tsx:572`](../frontend/src/pages/WorkflowEditorPage.tsx#L572)。

后端同时写入 `presentationPolicy` 和 `presentationPolicyProposal`，API analysis 包含 reason/evidence/confidence；Chat
收到结果后只保存 `draftWorkflowId` 并提供“打开固化草稿”。编辑器只读取最终 `presentationPolicy`，完全不读取或
展示 proposal。用户看不到提议来源、理由、证据和置信度，也没有 accept/reject/modified 状态可审计。

此外，编辑器选择 LLM producer 后没有 prompt、language、maxLength 控件，只能接受后端默认 Prompt 或切换 JSON
视图。这与“Agent 提议、用户审阅、发布冻结”不同。

应让 draft 保存 proposal 与 decision 两个对象；发布门禁要求用户明确接受、拒绝或修改。编辑器展示依据、置信度、
预计模型调用/费用，并提供 template/prompt/language/maxLength 全部通用字段。最终 WorkflowVersion 只冻结用户决定
后的 policy，同时保留 proposal digest 和 decision audit。

## 4. P2 Finding

### P2-1：REPLAY 所称“真实 CLI integration gate”实际仍是 Python fake executable

位置：[`test_traex_builtin.py:20`](../backend/tests/test_traex_builtin.py#L20)、
[`test_traex_builtin.py:77`](../backend/tests/test_traex_builtin.py#L77)、
[`REPLAY-027.md`](../REPLAY/REPLAY-027.md)。

`_fake_traex()` 是仓库测试脚本，round-trip 证明 subprocess/MCP/Host 协议，但不会复现原生 TraeX 的上下文 Token、
JSONL failed terminal、output-schema 行为、状态目录或认证。这正是自动化全部通过、真实探测却失败的原因。

保留 fake fixture 作为快速协议回归是合理的，但必须另设 opt-in real-provider gate，并把测试结果明确标为 fixture 或
real。REPLAY 不得把“在非 Git 目录启动 fake executable”称为真实 CLI integration。

## 5. 已确认有效的 027 修复

- `_trusted_working_directory()` 保持 artifact root 内隔离并拒绝越界；真实 TraeX 已越过上一版 Git 目录阻断。
- 天气 seed 不再包含 `presentationPolicy`；真实天气 v1 只有规范 JSON、MCP/Artifact 和 0 Token。
- 错误类型不再人为补发 dispatch；MCP tool 名称碰撞会 fail closed。
- Settings 的“候选 Agent Runtime（未验证可用）”措辞比上一版准确。
- 确定性组件、历史、RunDetail、SSE、明暗主题和工作流编辑器可真实操作。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| 审查前文档链 | 通过；103 个版本化 Markdown |
| 后端质量与测试 | Ruff 通过；318 passed |
| 迁移 | `0001 → 0023` 通过 |
| 前端质量与测试 | check/lint/build 通过；51 passed |
| 独立 API/Worker/Vite/Chromium E2E | 37 passed |
| 浏览器 UI 门禁 | 正常主机权限下 2 passed |
| 真实 TraeX 非 Git 目录启动 | 目录检查通过；随后直接 CLI 报 token expired |
| 应用内真实 Provider probe | **失败**；31,605 Token，`TOKEN_BUDGET_EXCEEDED` |
| 工作流开启自动选择 | **失败**；Provider unavailable |
| 工作流关闭通用 Agent | **失败**；无严格 JSON，`AGENT_PROTOCOL_INCOMPATIBLE` |
| 确定性问候与历史 | 通过；真实 Worker、JSON、1 条历史和 RunDetail |
| 天气普通测试样本 | 通过；MCP/Artifact/JSON，Presentation 未启用、0 Token |
| 固化 Proposal 前端审阅 | **失败**；理由/证据/置信度不可见 |
| 明暗主题和主要点击 | 通过；配色、布局和交互正常 |

修复范围和验收条件见 [`NEXT_STEPS-028.md`](../STEP/NEXT_STEPS-028.md)。
