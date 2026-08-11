# REPLAY-025：可信语义入口、完整目录与实时事件一致性

日期：2026-08-11

依据：[`NEXT_STEPS-025.md`](../STEP/NEXT_STEPS-025.md)、
[`design-compliance-review-025.md`](../review/design-compliance-review-025.md) 和
[`code-review-025.md`](../review/code-review-025.md)。

执行基线：`fdc8945`。当前未创建结果提交；本文记录待提交工作树的复现事实，不填写不存在的结果提交号。

## 1. 结论

025 的本地代码修复与发布门禁已跑通：

- `review/` 正式报告不再被 Git 忽略，历史 `STEP/`、`REPLAY/` 链接按迁移后目录修正；
- 内置 `openai_sidecar` 以 adapter 内容 digest 受信，主动探测要求随机 challenge、
  Host ToolResult round-trip、strict output、显式 `model.request.dispatched` 和真实 Usage；
- workflow selection 成功与失败统一记录 attempted/dispatched/completed、runtime、Token 和错误码；
- 所有 seed 工作流补齐完整语义目录，发布缺少任一必需字段时返回诊断并拒绝；
- SSE 改为通用 `run.event` envelope，成功 Run 会等待 Presentation 终态后关闭；
- 编辑器删除重复只读 DAG，Chat 提供直达 Runtime Settings/探测入口。

`make demo-check` 在最终代码状态连续两次通过，`make browser-ui-check` 在最终 SSE
状态连续两次通过。

当前环境没有配置外部真实 Agent Provider。普通自然语言语义最低集未执行，不能用 fixture
替代。因此本文确认**本地交付门禁通过**，但不声明 025 整体通过。

## 2. 可信 Runtime 与 Usage

Agent Host 不再从 `agent.step.completed` 推断模型 dispatch。探测只有同时满足以下事实才置
`modelAvailable=true`：

1. adapter digest 受信，且记录 adapter version/digest、provider/model；
2. 收到显式 `model.request.dispatched`；
3. 模型使用随机 key 调用 Host `lookup.table`，并把随机 ToolResult 原样返回；
4. strict output 通过，Usage 完整且 Token 大于零。

仓库维护的 `examples/openai_agent_sidecar.py` 支持独立 `SKILLX_AGENT_*` 配置；其他 adapter
需显式加入 `SKILLX_AGENT_TRUSTED_ADAPTER_DIGESTS`。可执行文件 allowlist 与 adapter digest
信任相互独立。

迁移 `0022` 为 `model_usage` 增加 `error_code`。选择失败在返回 502 前持久化唯一 call/attempt；
未 dispatch 的尝试保留但不计入费用，已知的部分 Token 不因 Usage 不完整而丢失。

## 3. 目录与渐进披露

发布门禁要求 `summary`、`useWhen`、`doNotUseWhen`、`examples`、`tags` 全部为非空有效值；
输入输出摘要从 Schema 生成。seed 的基础及条件型工作流均已补齐。

- L0：名称、摘要、标签、精确 ref/digest 与匹配原因；
- L1：适用/不适用、示例、输入输出摘要及 Fragment 索引；
- L2：Fragment 边界 Schema、锁与编译结果。

服务端执行仍从精确锁定的 WorkflowVersion 读取完整 Schema，不依赖 Agent 披露内容。

## 4. 实时事件与 UI

SSE 不再维护前端事件类型白名单。所有持久事件使用统一 `run.event` envelope，真实类型位于
envelope 的 `type` 字段；未知新事件也进入相同 store 和通用展示。

Browser UI 门禁在刷新前断言 Artifact 可见，并比较页面 live event seq 与
`GET /runs/{id}/events`：集合完全一致且为 `1..N` 连续序列。成功 Run 启用 Presentation 时，
流在 `presentation.completed` 或 `presentation.failed` 后关闭，不再在 `run.completed`
时提前结束。

## 5. 最终验证

| 门禁 | 最终结果 |
|---|---|
| `make demo-check` 第一次 | 通过；后端 303 passed，前端 51 passed，E2E 37 passed in 135.24s |
| `make demo-check` 第二次 | 通过；后端 303 passed，前端 51 passed，E2E 37 passed in 138.34s |
| Alembic 空库升级 | `0001 → 0022` 通过 |
| `make browser-ui-check` 第一次 | 2 passed in 25.67s |
| `make browser-ui-check` 第二次 | 2 passed in 26.27s |
| 文档链候选干净目录 | 通过；生成本文前扫描 94 份正式版本文档 |
| `git diff --check` | 通过 |

Browser UI 继续覆盖 760、903、1024、1280、1440px、亮暗主题和 console 0
error/warning，并新增 live seq/DB 一致性及刷新前 Artifact 断言。

## 6. 未完成验收

以下变量均未配置，仅检查“是否存在”，未读取 Secret：

```text
SKILLX_AGENT_RUNTIME_TYPE
SKILLX_AGENT_COMMAND
SKILLX_AGENT_ENDPOINT
SKILLX_AGENT_MODEL
SKILLX_AGENT_API_KEY
OPENAI_API_KEY
ACEDATACLOUD_API_KEY
ACEDATACLOUD_TOKEN
```

因此“上海天气如何”首 Turn、多字段、数字、布尔、枚举、数组、URL、历史引用和歧义拒绝
仍缺真实 Provider 证据。配置真实 Provider 后必须从 Settings 执行付费探测，再记录
provider/model/adapter digest、Turn/Run/Usage 和选择/拒绝理由；完成前不得改写为“025 整体通过”。
