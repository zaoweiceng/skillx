# 代码审查报告（027）

审查日期：2026-08-11

代码基线：`76f51f4`。审查对象为 [`REPLAY-026.md`](../REPLAY/REPLAY-026.md) 对应的工作树代码。
本轮未修改业务代码。

## 1. 结论

**不通过：1 个 P0、3 个 P1、1 个 P2。**

026 补齐了上一轮缺失的内置 TraeX bridge 和受管理 seed 升级，自动化与真实浏览器门禁均通过。但 bridge 的真实
启动参数与应用工作目录契约不一致，导致所有正常应用调用在模型请求前失败。天气专用模板、错误的 dispatch 推断和
不可靠的认证/错误状态还会分别破坏通用架构、审计事实和可恢复性。

## 2. P0 Finding

### P0-1：内置 TraeX 在平台正常隔离目录中必然失败

位置：[`traex_builtin.py:93`](../backend/app/runtime/traex_builtin.py#L93)、
[`traex_builtin.py:142`](../backend/app/runtime/traex_builtin.py#L142)、
[`traex_builtin.py:155`](../backend/app/runtime/traex_builtin.py#L155)。

adapter 使用 `request.working_directory` 创建 schema/output，并把它同时传给 TraeX `-C` 和 subprocess `cwd`。
平台的这些每轮隔离目录不是 Git 仓库；命令却没有 `--skip-git-repo-check`。真实执行因此在模型请求前退出：

```text
Not inside a trusted directory and --skip-git-repo-check was not specified.
```

Settings provider probe 和通用 Chat 都复现了该错误，Usage 均为 `requestDispatched=false`。fixture 测试只检查命令
形状和模拟 JSONL，没有使用 TraeX 对平台真实目录执行，因而漏掉了生产前置条件。

修复必须对“由平台创建且经过边界校验的本轮工作目录”显式传递原生跳过 Git 检查参数，继续保留 read-only sandbox、
工具 allowlist、忽略用户规则/插件和最小环境。不能把 cwd 改到仓库根目录，因为那会扩大 TraeX 可见文件范围并破坏
每轮隔离。验收必须直接使用真实 `traex` 和与生产相同的 artifact/selection 工作目录。

## 3. P1 Findings

### P1-1：天气 Presentation 被硬编码为平台特例，固化链没有通用策略

位置：[`seed.py:423`](../backend/app/seed.py#L423)、
[`seed.py:439`](../backend/app/seed.py#L439)、
[`distillation.py:613`](../backend/app/application/distillation.py#L613)、
[`distillation.py:648`](../backend/app/application/distillation.py#L648)、
[`distillation.py:658`](../backend/app/application/distillation.py#L658)。

`_weather_workflow()` 直接写入中文模板 `{city}当前{condition}，温度{temperature}℃。`。这让测试场景渗入通用
产品定义，也掩盖了真正缺口：固化流程会请求 `capabilityDescriptor`，但不产生或审阅 `presentationPolicy`。

天气只是一个验收样本，不能代表网页总结、文件操作、数据查询、组合 Fragment 等不同输出语义。应删除天气专用
Presentation 默认值，在通用 `DistillationProposal`/固化阶段增加 Presentation 提议：依据 `audience`、是否直接
展示、是否为机器消费/中间结果、输出 schema 和表达复杂度，选择 disabled/template/LLM，并携带理由与证据供用户
审阅。发布后把选定策略冻结到 WorkflowVersion。回归测试至少使用三个不同领域，禁止断言天气字面量存在于 seed。

### P1-2：bridge 在没有 dispatch 证据时人为生成 `model.request.dispatched`

位置：[`traex_builtin.py:240`](../backend/app/runtime/traex_builtin.py#L240)、
[`traex_builtin.py:242`](../backend/app/runtime/traex_builtin.py#L242)、
[`traex_builtin.py:247`](../backend/app/runtime/traex_builtin.py#L247)。

进程以认证失败、模型不可用或限流退出且尚未观察到 dispatch 时，代码仍主动 yield
`model.request.dispatched`。这把错误类型当成计费/发送证据。认证可以在任何模型请求前失败；限流也可能发生在网关
预检阶段，不能据此断言请求已发送。对应单测还把该错误行为固化为预期。

`dispatched` 只能来自 TraeX 明确、版本化的 JSONL 事件或其他可验证的模型请求 ID/Usage 证据。没有证据时必须
保持 false，并单独记录 `attempted=true`。修复 P0 后，当前过期 token 就会走到这里并制造错误审计，故不能延后。

### P1-3：认证活性、协议错误和 Settings 可用性被混为一体

位置：[`agent_provider.py:140`](../backend/app/application/agent_provider.py#L140)、
[`agent_provider.py:175`](../backend/app/application/agent_provider.py#L175)、
[`error_policy.py:79`](../backend/app/application/error_policy.py#L79)。

`traex login status` 返回 0 且含 “logged in” 就被投影为 `logged_in_unverified`；当前主机同时满足这一条件和真实
401 token expired。该结果本可作为“本地凭据存在”，但页面又显示“当前结构化生成路径：Agent fallback”，容易被
理解为可用。另一方面，所有未识别错误都落入 `AGENT_PROTOCOL_INCOMPATIBLE`，工作目录缺少参数也被用户消息解释成
“升级 TraeX”。

应分离 installed、credential-present-unverified、active-probe-authenticated、protocol-compatible 和 usable。
只有真实随机 challenge + strict output + ToolResult round-trip + 正 Usage 的主动探测能标记 usable。为受信目录、
认证、模型、协议分别定义错误码和恢复动作；Settings 必须显示“候选 Runtime”和“已验证可用 Runtime”的区别。

## 4. P2 Finding

### P2-1：MCP tool 名称归一化不是一一映射，可能静默覆盖能力

位置：[`traex_builtin.py:24`](../backend/app/runtime/traex_builtin.py#L24)、
[`traex_builtin.py:103`](../backend/app/runtime/traex_builtin.py#L103)。

`_tool_name()` 把所有非字母数字字符替换成 `_`，因此 `a.b` 与 `a_b` 会得到相同名称；字典推导会静默覆盖前者。
通配型系统允许不同来源的 workflow/fragment 组合，不能假设 capability ref 永不碰撞。结果可能是 Agent 看到一个
工具描述，却执行另一个能力。

应使用稳定前缀加 digest/hash 后缀的可逆索引，建立显式 `toolName → capabilityRef` 映射；构建时检测重复并 fail
closed。测试需覆盖标点差异、长 ref、Unicode 和组合多个 Fragment。

## 5. 已确认有效的 026 修复

- migration 0023 和 `seedKey/managedRevision` 支持系统样例创建不可变升级版本，并保留用户草稿。
- 内置 Runtime 能自动发现 TraeX，协议、provider、runtimeType 和 Usage 字段已进入投影。
- 每轮 loopback MCP、工具 allowlist、严格 output schema、临时工作目录和安全环境的总体边界正确。
- 通用 JSON + Presentation 运行、事件、RunDetail 与历史展示链可工作；编辑器支持 disabled/template/LLM。
- 明暗主题、组件历史、对话开关、计时、Token 不可用状态和刷新恢复均正常。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| 审查前文档链 | 通过；99 个版本化 Markdown |
| 后端质量与测试 | Ruff 通过；309 passed |
| 迁移 | `0001 → 0023` 通过 |
| 前端质量与测试 | check/lint/build 通过；51 passed |
| 真实 API/Worker/Vite/Chromium E2E | 37 passed |
| 浏览器 UI 门禁 | 2 passed；明暗主题正常，console 0 error/warning |
| 应用内真实 TraeX provider probe | **失败**；非 Git 工作目录被拒绝，dispatch=false |
| 关闭工作流后的真实通用 Agent | **失败**；同一错误，约 2.0s，Token 不可用 |
| Git 仓库内原生 TraeX 请求 | **失败**；到达服务端后返回 401 token expired |
| 真实确定性组件与 Presentation | 通过；Worker/MCP、JSON、文本、历史与详情完整，但文本来自天气特例 |

修复范围和验收条件见 [`NEXT_STEPS-027.md`](../STEP/NEXT_STEPS-027.md)。
