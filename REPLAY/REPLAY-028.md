# REPLAY-028：原生 TraeX 终态、分离预算与 Presentation 审阅闭环

日期：2026-08-11

依据：[`NEXT_STEPS-028.md`](../STEP/NEXT_STEPS-028.md)、
[`design-compliance-review-028.md`](../review/design-compliance-review-028.md) 和
[`code-review-028.md`](../review/code-review-028.md)。

执行基线：`fa06e4bc3e59d6e36db3ee8ba06b8f097d303a90`。当前未创建结果提交；本文记录工作树中可复现的事实。

## 1. 结论

本轮按根因修复 028 审查中的 2 个 P0、2 个 P1，并新增独立的真实 Provider gate：

- 内置 bridge 现在以原生 JSONL 终态为事实源；`turn.failed`/`error` 优先于进程退出码和残留 output file，退出码 0/非 0 得到相同业务错误。只有 `turn.completed` 后缺少、非 JSON 或不符合 Schema 的输出才是 `MODEL_MALFORMED_OUTPUT`；缺少受支持终态才是 `AGENT_PROTOCOL_INCOMPATIBLE`。
- dispatch 只来自带模型消息/推理/MCP call 身份的 `item.started/completed`，`turn.started` 和 `turn.completed` 本身不算 dispatch。成功审计保存脱敏、限长的原生事件类型/ID/version 摘要；失败保留 native terminal 和事件类型证据，失败 Usage 不再丢失。
- Host 把 `maxInputTokens`、`maxOutputTokens`、`maxBillableTokens` 分离并在请求前冻结。Provider probe 使用 40,000/1,000/41,000 profile，Settings 在点击付费探测前显示预计 32,000 输入 Token 与三项硬上限，不再以 2,000 名义发出 31k 请求。
- readiness 不再把整体 `probe_ok` 同时赋给认证和协议；分别投影 installed、credentialPresent、activeProbeAuthenticated、modelReachable、protocolCompatible、toolRoundTrip、usable。
- Presentation 由独立受限 Agent 基于去实例化 task intent、consumer、顶层/Fragment、I/O Schema 和数据流提出。Agent 不可用时静态代码不按节点类型猜测，默认关闭并标记人工决定。
- proposal 与 decision 分开保存；编辑器展示 source/reason/evidence/confidence 和受众/消费者/位置，提供“接受提议 / 修改后接受 / 拒绝并关闭”，并可编辑 template、prompt、language、maxLength。服务端绑定 proposal/policy digest；未决定或决定后内容漂移均阻止发布。
- `make traex-real-check` 只接受 PATH 中真实 `traex`，缺安装或认证失效报告 skipped/blocked，不以 `_fake_traex` 替代。

无费用工程门禁和 Browser UI 已通过。真实付费 TraeX gate、本机 token 刷新后的真实主流程以及三个领域的真实 Agent 闭环本轮没有执行，因此不声明 NEXT_STEPS-028 整体通过。

## 2. TraeX 原生协议与预算证据

[`test_traex_builtin.py`](../backend/tests/test_traex_builtin.py) 使用明确标记的可执行 fixture 覆盖：

1. `turn.failed` 在 CLI 退出码 0 和 7 时都优先映射为 `TRAE_AUTH_REQUIRED`，残留合法 output file 不得覆盖失败；
2. 原生 `error` 分别映射模型不可用、限流和超时；
3. 成功终态返回普通文本时得到 `MODEL_MALFORMED_OUTPUT`，不是协议版本错误；
4. 31,314 input + 291 output 在冻结的 40,000 input、1,000 output、41,000 billable profile 下成功，总计 31,605；旧的 2,000 总量字段不再覆盖显式三维预算；
5. Host ToolResult round-trip、provider/model、Usage、dispatch 和脱敏 native protocol audit 同时成立。

Provider probe 把实际 budget profile、input/output/total Usage 和独立能力事实写入结果。失败的 Conversation Agent 也把已观测 Usage 写入 `ModelUsage`，不再统一落为 unknown。

## 3. Presentation proposal / decision

[`distillation.py`](../backend/app/application/distillation.py) 的 `PresentationPolicyProposal` 契约包含
`resultAudience`、`consumer`、`terminalOrFragment`、enabled、producer、template/prompt、language、maxLength、reason、evidence 和 confidence。输入不包含实例 URL、Secret、页面正文或一次性参数值。

草稿初始只有：

```text
presentationPolicyProposal + presentationPolicyDecision(status=pending)
```

用户明确操作后，服务端补齐 proposal/policy digest。发布门禁要求 accepted、modified 或 rejected 状态和两个 digest 与当前字节一致。E2E 的固化发布路径也通过真实 draft update 明确记录 rejected 决定，不再绕过审阅门禁；Browser UI 路径实际点击“修改后接受”后再检查、发布。

## 4. 验证记录

| 命令/检查 | 结果 |
|---|---|
| 针对性 backend 回归 | 49 passed；随后新增预算/映射审计用例纳入全量 |
| `make demo-check`（正常主机权限，最终） | 通过；326 backend、52 frontend、37 独立进程 E2E |
| Alembic 空库升级 | `0001 → 0023` 通过 |
| 前端 check / lint / test / build | 通过；11 files / 52 tests，生产构建成功 |
| 文档链 | 生成本文前 106 个版本化 Markdown；新增本文后重新检查 |
| `make browser-ui-check`（正常主机权限，最终） | 2 passed in 27.02s |
| `git diff --check` | 生成本文前通过；交付前重新检查 |
| `traex --version` | `traecli 0.200.19(internal edition)` |
| `traex login status` | `Logged in using Trae`；只证明凭据存在，不证明 token 有效 |
| `make traex-real-check` | **未运行**；该命令可能产生真实模型费用，本轮没有用户费用授权 |

受限沙箱中的首次全量 backend 有 4 个 Chromium/本地站点用例因 Browser 运行权限失败；同一工作树在正常主机权限下 326/326 通过，不作为产品失败。首次 UI gate 暴露旧验收脚本未点击新的 reviewer decision；补齐“修改后接受”这一真实用户动作后 2/2 通过。

## 5. 未完成项

以下属于真实外部事实，不能由 fixture 或静态测试替代：

1. 用户在终端执行 `traex logout` / `traex login` 刷新已知过期凭据；
2. 明确授权费用后执行 `make traex-real-check`，记录真实 provider/model、Usage、ToolResult、严格输出和取消；
3. 用真实 Agent 完成工作流开启/关闭两条 Chat 路径；
4. 用三个不同领域完成首次探索、Presentation 审阅、发布、不同输入复用、失败接力与受控修订；
5. 在上述真实证据完成前，Provider usable 和 NEXT_STEPS-028 总体状态保持未通过。
