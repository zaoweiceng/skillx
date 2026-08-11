# REPLAY-023：让运行时状态只表达可证明事实

日期：2026-08-10

依据：[`NEXT_STEPS-023.md`](../STEP/NEXT_STEPS-023.md)、
[`review/design-compliance-review-023.md`](../review/design-compliance-review-023.md) 和
[`review/code-review-023.md`](../review/code-review-023.md)。

执行基线：`9f9fb43`。范围为单用户 Local Demo。

## 1. 结论

023 审查发现的代码问题已修复：

- fixture 明确返回 `protocolTestOnly=true`，Chat 自动选择只接受
  `traex_managed|openai_sidecar`；没有真实语义 Runtime 时，发送前和发送后均显示
  “自动选择不可用”，关闭工作流后仍可运行通用 Agent。
- Browser readiness 改由独立 Worker 实际启动 Chromium 后持久化心跳；API 和 Settings
  读取同一事实，不再用 allowlist 推断可用性。
- Run 启动候选改名 `runtimeSelection`；`runtimeUsed`、调用列表和 Token 只从实际
  `ModelUsage` 聚合。零模型 Run 显示“模型未调用 / 0 Token”。
- 终态 RunDetail 显示“实时流已关闭”，编辑器控件统一使用主题 token、尺寸和交互状态。
- 恢复提交 `9f9fb43` 删除的 39 份版本文档，并新增文档链门禁。

当前机器没有真实语义 Agent endpoint/model/key，也没有已确认兼容 Host 双向协议的
TraeX managed adapter。因此“上海天气如何”及七条真实语义最低集未执行，不能用 fixture
代替。**本报告确认代码修复与本地门禁通过，但不声明 023 外部真实语义验收或整体审查通过。**

## 2. 自动选择边界

`/health/ready` 的 `workflowSelectionMode` 现在只有：

| 状态 | 含义 |
|---|---|
| `real` | 已配置 `traex_managed` 或 `openai_sidecar` |
| `unavailable` | 未配置真实语义 Runtime；fixture 不提升该状态 |

fixture 仍可验证 Host、ToolResult、Schema、故障和 Usage 协议，但产品 Chat 不再调用它完成
自然语言路由。协议测试通过显式测试注入覆盖五种 `WorkflowSelection` action；生产入口另有
回归，断言 fixture 返回 `WORKFLOW_SELECTION_UNAVAILABLE`。

README 已分别给出 `traex_managed` 与 `openai_sidecar` 配置。普通交互式 `traex` CLI 不会被
描述为兼容 managed adapter；`openai_sidecar` 的 endpoint/model/key 与 Direct LLM
`SKILLX_LLM_*` 保持独立。

## 3. Browser Runtime 事实链

迁移 `0020` 新增 `browser_runtime_heartbeats`。Worker 启动及周期预检记录：

- `available|binary_missing|permission_denied|launch_failed|unconfigured`；
- Worker id、检查时间和 stale 判定；
- Chromium 路径/版本、Playwright 版本、实际启动结果；
- 有界错误类型和可执行修复建议，不记录 Secret。

本机正常权限下，真实 API、Worker、Vite 和 Chromium 两轮均报告 `available`，Settings
展示 Chromium 版本和检查时间。单元回归覆盖二进制缺失、MachPort/permission denied、
一般启动失败和过期心跳。当前环境不是受限权限环境，因此本轮没有新的真实
`permission_denied` 进程证据。

## 4. Runtime 审计

冻结快照与实际调用已拆分：

```text
runtimeSelection
  = Run 启动时冻结的结构化生成候选

runtimeUsed / runtimeCalls / totalTokens
  = 实际 ModelUsage 聚合
```

零 LLM 工作流的 `modelUsage=[]`、`runtimeUsed=null`、`modelCalled=false`、
`totalTokens=0`。Direct LLM、Agent fallback、Agent Runtime、fake structured 和失败后
未调用 Presentation 均继续保留各自唯一 Usage 事实；执行中 401/429 不切换 Runtime。

## 5. UI 与文档

- 终态 RunDetail 不显示脉冲 LIVE/SSE，空历史显示“本会话未加载历史事件”。
- `.inspector-fields` 与 `.inspector-field` 的 input/textarea/select/checkbox 已统一
  min-height、padding、圆角、hover/focus/disabled 和主题背景。
- Chat 在消息发送前读取 ready API，真实语义 Runtime 缺失时不再显示自动选择可用。
- 根目录恢复 `NEXT_STEPS-001..021` 与 `REPLAY-003..017/019..021`，内容 SHA-256 与
  `review/` 保留副本一致。
- `tools/check_doc_chain.py` 检查当前版本链、39 份历史文件和本地 Markdown 链接；
  `make quality-check` 已接入，当前检查 86 份版本化文档。
- `REPLAY-022.md` 已改为指向实际提交 `9f9fb43`。

## 6. 验证记录

| 门禁 | 结果 |
|---|---|
| `make demo-check` 连续两次 | 通过 |
| 后端 Ruff / Pytest | 通过；295 passed |
| Alembic 空库升级 | `0001→0020` 通过 |
| 前端 check / lint / test / build | 通过；11 files / 50 passed |
| 进程 E2E | 37 passed，两轮均通过 |
| `make browser-ui-check` 连续两次 | 2 passed + 2 passed |
| Browser readiness 正向事实 | Worker 实际启动 Chromium，`available` |
| 文档链 | 86 份检查通过 |
| `git diff --check` | 通过 |

## 7. 未完成验收

当前仅检查变量是否存在，未读取任何 Secret：

```text
SKILLX_AGENT_RUNTIME_TYPE: 未配置
SKILLX_AGENT_COMMAND: 未配置
SKILLX_AGENT_ENDPOINT/MODEL/API_KEY: 未配置
OPENAI_API_KEY: 未配置
ACEDATACLOUD_API_KEY/TOKEN: 未配置
```

本机虽有 `traex` CLI，但其公开命令不是本仓库 Agent Host 的双向 NDJSON/ToolResult
managed adapter，不能直接作为通过证据。配置兼容的真实 Runtime 后仍需执行：

1. “上海天气如何”首 Turn 直接 `run_full`，`city=上海`，当前消息 provenance，
   `serverVerified=true`；
2. 七条以上无 fixture 指令的单/多字段、数字、布尔、枚举、数组、URL、历史引用和歧义用例；
3. 记录真实 provider/runtime/model、Turn/Run/Usage/digest/耗时，不记录 key；
4. 在受限 OS 权限环境取得真实 `permission_denied` heartbeat 证据。

完成以上外部证据后，才能进入 024 审查并声明 023 整体通过。
