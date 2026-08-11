# REPLAY-026：内置 TraeX bridge、托管 seed 升级与天气阅读结果

日期：2026-08-11

依据：[`NEXT_STEPS-026.md`](../STEP/NEXT_STEPS-026.md)、
[`design-compliance-review-026.md`](../review/design-compliance-review-026.md) 和
[`code-review-026.md`](../review/code-review-026.md)。

执行基线：`76f51f4`。当前未创建结果提交；本文记录待提交工作树的复现事实，不填写不存在的结果提交号。

## 1. 结论

026 的代码修复、本地交付门禁、天气双格式和 pre-025 数据升级已经跑通：

- 未显式配置 `SKILLX_AGENT_*` 时自动发现 PATH 中的 TraeX，内置 bridge 通过原生 JSONL 和
  每 Run loopback MCP 接回唯一 `AgentHost`；
- 显式 Agent 配置保持优先，Direct LLM 未配置时仍只在 Run 启动前冻结为 `agent_fallback`；
- 系统 seed 使用 `seedKey/managedRevision` 发布新的不可变版本，保留旧版本和历史 Run，并跳过用户草稿；
- 天气继续以 JSON 为事实源，新增零 LLM 中文 Presentation；
- Runtime 审计分离 `protocol/provider/model/runtimeType/adapterVersion/adapterDigest`。

最终工作树中，`make demo-check` 连续两次通过，`make browser-ui-check` 连续两次通过。

本机 TraeX 已安装且 `traex login status` 返回已登录，但当前 Agent 会话的外层沙箱禁止 TraeX 初始化
`~/.trae/cli` 状态目录。真实 `traex exec` 没有产生 JSONL dispatch，主动探测不能完成。因此本文确认
**代码与本地交付门禁通过**，但不声明 026 整体通过，也不把假 TraeX 子进程测试当作真实 Provider 证据。

## 2. 内置 TraeX Runtime

Runtime 选择顺序为：显式 `SKILLX_AGENT_*`、兼容旧配置、PATH 自动发现 `traex`、不可用。当前本机解析结果：

```text
runtimeType=traex_builtin
command=/Users/example/.local/bin/traex
protocol=traex-cli-jsonl+mcp
provider=traex
model=null
adapterVersion=traex-builtin/v1
adapterDigest=sha256:9f1b116ca0d1c90ace68856bee29e8b51c5511034df97b8260c09686c8e92513
source=auto_discovered
```

内置 bridge 每次运行创建隔离工作目录、临时 output schema 和仅监听 `127.0.0.1` 的 MCP server。MCP
只列出本轮 Host 已批准的能力；`tools/call` 生成 Host `tool.requested`，等待同一 `callId` 的 ToolResult，
再返回原 TraeX Turn。TraeX 以只读 sandbox、忽略用户规则/配置、禁用无关 hooks/plugins/skills 和显式
allowed tools 启动。

JSONL 映射覆盖显式模型 dispatch、Usage、provider/model、严格 JSON 终态、超时、取消和 stderr。
稳定错误码包括 `TRAE_NOT_INSTALLED`、`TRAE_AUTH_REQUIRED`、`TRAE_MODEL_UNAVAILABLE`、
`AGENT_PROTOCOL_INCOMPATIBLE`。测试使用临时可执行假 TraeX，但实际经过子进程、HTTP MCP、
Host ToolResult round-trip 和 callId 配对。

## 3. 托管 seed 升级

迁移 `0023` 为 Component 增加 `seed_key/managed_revision`，并为 ModelUsage 增加 Runtime 身份字段。
系统样例升级只接受单一旧版本、默认版本未变化、草稿未修改的旧 seed。升级时创建新的
WorkflowVersion 并切换 Component 默认版本；相同 digest 不重复发布。检测到用户草稿或多版本时明确跳过。

真实升级验证从基线 `76f51f4` 的归档创建 `0022` 数据库并执行旧 seed，再用当前代码运行
`alembic upgrade head` 和两次 seed：

| 事实 | 结果 |
|---|---|
| Alembic revision | `0022 → 0023` |
| 天气版本数 | `1 → 2` |
| 天气默认版本 | 指向新版本，`seedKey=local-weather`、`managedRevision=2` |
| 历史天气 Run | 仍引用旧 WorkflowVersion，输入输出未改变 |
| 重复 seed | 保持 2 个天气版本，无重复发布 |
| 人为修改的问候草稿 | 明确提示跳过，草稿和默认版本均未覆盖 |

## 4. 天气双格式

天气工作流冻结以下模板策略：

```text
enabled=true
audience=end_user
producer=template
template={city}当前{condition}，温度{temperature}℃。
language=zh-CN
```

独立真实 API/Worker/MCP 运行得到：

```json
{"city":"上海","condition":"多云","temperature":19}
```

Presentation 为：

```text
上海当前多云，温度19℃。
```

该 Run 的 `model_usage_count=0`。持久事件 seq 为连续 `1..17`，`run.completed` 后依次出现
`presentation.started`、`presentation.delta`、`presentation.completed`；Presentation 不修改成功 Run
及规范 JSON。

## 5. 最终验证

| 门禁 | 最终结果 |
|---|---|
| `make demo-check` 第一次 | 通过；后端 309 passed，前端 51 passed，E2E 37 passed in 152.54s |
| `make demo-check` 第二次 | 通过；后端 309 passed，前端 51 passed，E2E 37 passed in 139.65s |
| Alembic 空库升级 | `0001 → 0023` 通过 |
| `make browser-ui-check` 第一次 | 2 passed in 27.85s |
| `make browser-ui-check` 第二次 | 2 passed in 24.39s |
| pre-025 真实数据库升级 | 通过；旧版本/Run 保留、用户草稿跳过、重复 seed 幂等 |
| `git diff --check` | 通过 |

`make demo-check` 同时覆盖 Ruff、后端单测、前端 check/lint/test/build、文档链和独立进程 E2E。

## 6. 未完成的真实 TraeX 验收

当前本机：

```text
traecli 0.200.19(internal edition)
Logged in using Trae
```

应用 readiness 能自动发现并信任内置 bridge，但真实 `traex exec` 输出：

```text
WARNING: proceeding, even though we could not update PATH: Operation not permitted
failed to initialize state runtime at /Users/example/.trae/cli
```

等待约 60 秒仍没有 JSONL dispatch 后人工终止。此前同路径主动探测返回
`AGENT_PROTOCOL_INCOMPATIBLE`、`requestDispatched=false`、Usage 不完整。该失败发生在 TraeX 初始化自身
状态时，早于应用 MCP bridge 和模型请求，不能作为 bridge 或真实模型通过证据。

需要在允许 TraeX 访问自身状态目录的普通主机终端重新执行 Settings 主动探测；成功后再完成：

1. “上海天气如何”的 L0/L1 选择、`city=上海` 绑定和 `run_full`；
2. 多字段及歧义输入的真实 Schema binding；
3. 工作流关闭时轨迹中不存在 `workflow.*`；
4. 真实 provider/model、dispatch、正 Token Usage、Turn/Run 和 bindingSources 审计；
5. 认证过期、timeout、ToolResult 失败及 checkpoint 接力。

这些事实完成前，026 仍应标记为“本地交付通过，真实 TraeX 主流程待主机环境验收”。
