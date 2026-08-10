# REPLAY-024：恢复可重复门禁与持久审计

日期：2026-08-10

依据：[`NEXT_STEPS-024.md`](../STEP/NEXT_STEPS-024.md)、
[`design-compliance-review-024.md`](../review/design-compliance-review-024.md) 和
[`code-review-024.md`](../review/code-review-024.md)。

执行基线：`7b8906c`。验证对象为基于该提交的当前工作树；本轮尚未创建交付提交，
因此本文不填写不存在的结果提交号。

## 1. 结论

024 的本地代码与发布门禁修复完成：

- `STEP/`、`REPLAY/` 成为唯一版本文档目录，迁移后的 Markdown 链接可解析；
- Browser 预检移出唯一 Worker 调度循环，公开 readiness 不再返回本机绝对路径；
- RunDetail 刷新后从数据库分页恢复事件，再从最后 `seq` 接续 SSE；
- ModelUsage 明确区分 attempted、dispatched、completed，聚合只统计 dispatched；
- 真实语义自动选择只在 Host/ToolResult 主动能力探测成功后开放，内置天气能力补齐语义目录元数据。

`make demo-check` 连续两次通过，`make browser-ui-check` 修正门禁定位后连续两次通过。

当前环境没有配置真实语义 Agent/LLM Provider。普通消息最低语义集未执行，不能用 fixture
或脚本化模型替代。因此本文确认**本地发布门禁和代码修复通过**，但按
`NEXT_STEPS-024` 的完成定义，**不声明 024 整体通过**。

## 2. 文档事实链

`tools/check_doc_chain.py` 现在只扫描：

- `STEP/NEXT_STEPS-*.md`
- `REPLAY/REPLAY-*.md`
- `review/design-compliance-review-*.md`
- `review/code-review-*.md`

历史策略为 `NEXT_STEPS-001..N` 连续存在；REPLAY 的 `001`、`002` 早于制度建立，
`018` 是已记录的纠错缺号，其余 `REPLAY-003..N-1` 必须存在。新增单元回归证明
空扫描和已知断链都会失败。`REPLAY-023` 已加入勘误，不再声称无法复现的“86 份通过”。

## 3. 运行与事件审计

- Worker 启动独立 `browser-health` task，预检使用自己的 DB session 和 10 秒内有界超时；
  主循环不等待 Chromium，退出时显式取消并等待后台 task。
- `/health/ready` 仅公开 `chromiumExecutable`、版本、状态和摘要；完整路径仍只保存在本地诊断事实。
- `GET /runs/{id}/events` 支持 `after` 与分页 `limit`；前端先加载历史、按 `seq` 排序去重，
  再用 `after=<lastSeq>` 建 SSE。终态只读持久事件，不创建 EventSource。
- 迁移 `0021` 为 ModelUsage 增加 `attempted_at`、`request_dispatched_at`、`completed_at`。
  Direct LLM 在 HTTP 发送边界标记 dispatch；Agent sidecar 发送受契约校验的
  `model.request.dispatched`。配置失败和握手前失败不计调用，401、429、连接失败及 timeout
  保留 dispatched 事实，也不跨 Runtime fallback。

## 4. 真实语义边界

readiness 不再因配置变量或可执行文件存在就声明语义可用。`traex_managed` 与
`openai_sidecar` 都必须通过同路径 Agent Host、function calling、ToolResult 和 strict output
主动探测，探测缓存未成功或过期时 `workflowSelectionMode=unavailable`。

本机仅检查配置是否存在，未读取任何 Secret；以下均未配置：

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

因此“上海天气如何”首 Turn 和多字段、数字、布尔、枚举、数组、URL、历史引用、歧义拒绝
未形成真实 Provider 证据。当前正确行为是明确 unavailable，不回退到 fixture。

## 5. 验证记录

| 门禁 | 结果 |
|---|---|
| `python3 tools/check_doc_chain.py` | 通过；生成本文前扫描 90 份，生成后扫描 91 份 |
| 文档负向测试 | 2 passed；空扫描、断链均失败 |
| 后端 Ruff / Pytest | 通过；300 passed |
| Alembic 空库升级 | `0001→0021` 通过 |
| 前端 check / lint / test / build | 通过；11 files / 51 passed，构建成功 |
| `make demo-check` 第一次 | 通过；进程 E2E 37 passed in 140.53s |
| `make demo-check` 第二次 | 通过；进程 E2E 37 passed in 142.42s |
| `make browser-ui-check` | 首次因事件回放后文本定位歧义失败；改为 Artifact link 定位 |
| 修正后 Browser UI 两轮 | 2 passed in 27.03s；2 passed in 25.47s |
| `git diff --check` | 通过 |

Browser UI 门禁实际覆盖 760、903、1024、1280、1440px、亮暗主题、刷新恢复、
终态无 SSE 重连和 console 0 error/warning。首次失败是测试定位同时命中事件 payload 与
Artifact 链接，不是产品执行失败；门禁已改为角色定位并要求至少观察到一次带 `after` 的 SSE。

## 6. 未完成验收

配置兼容的真实 Provider 后，仍必须执行 `NEXT_STEPS-024` 第 3 节的普通自然语言最低集，
记录 provider/runtime/model、Turn/Run、digest、耗时、Token、Tool 数和 fallback 原因，
且不得记录 Secret。该项完成前不得把本报告改写为“024 整体通过”。
