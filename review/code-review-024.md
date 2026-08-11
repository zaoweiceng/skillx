# 代码审查报告（024）

审查日期：2026-08-10

代码基线：`7b8906c`。审查对象为 [`REPLAY-023.md`](../REPLAY/REPLAY-023.md) 对应代码。
本轮未修改业务代码。

## 1. 结论

**不通过：2 个 P1、3 个 P2；另有 1 个外部真实语义验收阻塞。**

023 已关闭上一轮 fixture 冒充语义 Runtime、Browser readiness 假阳性、零 LLM 审计错误、终态 SSE 重连和
编辑器主题不一致等主要问题。新的高优先级问题集中在发布门禁不可复现，以及健康探测阻塞单 Worker 主循环。

## 2. P1 Findings

### P1-1：文档归档目录与校验器约定相互矛盾，导致发布总门禁必然失败

位置：[`check_doc_chain.py:13`](../tools/check_doc_chain.py#L13)、
[`check_doc_chain.py:35`](../tools/check_doc_chain.py#L35)、
[`REPLAY-023.md`](../REPLAY/REPLAY-023.md)。

提交把 `NEXT_STEPS-*` 移到 `STEP/`、把 `REPLAY-*` 移到 `REPLAY/`；校验器仍使用
`ROOT.glob("NEXT_STEPS-*.md")`、`ROOT.glob("REPLAY-*.md")`，并继续要求 39 份历史文件位于根目录。
迁移后的 Markdown 链接也保留了原来的相对路径，例如 `REPLAY/REPLAY-023.md` 中的
`NEXT_STEPS-023.md` 会解析为 `REPLAY/NEXT_STEPS-023.md`。

实际执行 `python3 tools/check_doc_chain.py` 会同时报告“根目录缺少 NEXT_STEPS 文档”、39 份历史文件缺失和
大量断链；`make demo-check` 因此退出 2。`REPLAY-023` 中“86 份检查通过”的记录无法在所述提交重现。

根因是文件布局迁移没有把“目录结构、链接规则、校验规则、Make 门禁”作为同一个原子变更。应确定唯一规范：
建议保留 `REPLAY/` 与 `STEP/`，让校验器扫描真实目录并一次性重写全部相对链接；不要再复制一套根目录兼容文件。

### P1-2：Browser 周期预检串行阻塞唯一的 Worker 调度循环

位置：[`worker.py:68`](../backend/app/worker.py#L68)、[`worker.py:98`](../backend/app/worker.py#L98)、
[`browser_readiness.py`](../backend/app/application/browser_readiness.py)。

Worker 在同一个 `while` 循环里依次领取 workflow、agent、recovery、presentation，然后直接
`await record_browser_runtime_heartbeat()`。浏览器启动预检超时为 10 秒、默认每 60 秒执行一次；当 Chromium
启动慢、权限异常或系统资源紧张时，所有与 Browser 无关的工作领取也会一起暂停最多 10 秒。

readiness 本应旁路观察执行系统，不能反过来降低整个执行系统的可用性。应将预检放入独立后台 task，使用独立 DB
session、单飞和有界取消；主调度循环只读取最后心跳，Worker 停止时显式取消并等待该 task。

## 3. P2 Findings

### P2-1：终态 RunDetail 标称“持久事件历史”，实际刷新后不请求持久事件

位置：[`RunDetailPage.tsx:25`](../frontend/src/pages/RunDetailPage.tsx#L25)、
[`RunDetailPage.tsx:218`](../frontend/src/pages/RunDetailPage.tsx#L218)、
[`routes.py`](../backend/app/api/routes.py#L3456)。

页面只从 `useRunEvents` 的内存 store 读取事件，并只调用 `getRun()`；直接打开或刷新终态 Run 时不会调用后端已有的
`GET /runs/{id}/events`。因此真实成功 Run 虽然数据库里有完整事件，页面仍显示“本会话未加载历史事件”。

这不是文案问题。应在页面挂载时分页加载 `after=0` 的持久事件，按 `seq` 写入 store，再把 SSE 增量与历史去重合并；
终态只加载历史，不创建 EventSource。

### P2-2：`modelCalled` 用“存在 Usage 行”代替“请求已经发往模型”

位置：[`routes.py:3148`](../backend/app/api/routes.py#L3148)、
[`orchestrator.py:1392`](../backend/app/application/orchestrator.py#L1392)。

API 使用 `bool(usage_rows)` 生成 `modelCalled`。Orchestrator 对失败结果只要 metrics 带
`runtimeUsed=direct_llm|agent_fallback` 也会写 ModelUsage；配置漂移、启动前校验或 adapter 预调用失败可能尚未向 Provider
发请求，却被展示为“模型已调用”。这会影响费用与故障审计。

应在 Runtime adapter 的真正 dispatch 边界记录 `requestDispatchedAt`/`callStarted`，让 `modelCalled` 只聚合该事实；
Usage 行仍可保留预调用失败，但必须区分 `attempted` 与 `dispatched`。

### P2-3：公开 readiness 响应暴露本机 Chromium 绝对路径

位置：[`browser_readiness.py:205`](../backend/app/application/browser_readiness.py#L205)。

`/health/ready` 直接返回 `heartbeat.chromium_path`。手工响应包含用户目录下的完整 Playwright/Chromium 路径。
Local Demo 虽不考虑鉴权，但设计仍要求默认安全投影，前端也不需要完整路径来判断可用性。

公开响应只应返回版本、可执行文件名、状态和修复建议；完整路径作为本地诊断证据留在数据库/日志或显式技术详情接口。

## 4. 外部真实语义验收阻塞

当前环境没有 `SKILLX_AGENT_*`、`SKILLX_LLM_*`、OpenAI 或 AceDataCloud 可用配置。fixture 是真实独立子进程，
可以证明 Host 协议与降级行为，但不能证明 Agent 对普通自然语言的工作流选择、参数提取、组合判断和自然语言整理。
本轮“上海天气如何”得到明确 unavailable，行为符合 023 设计，却不能作为目标功能通过证据。

另外，内置天气工作流的目录元数据仍缺 `useWhen`/`doNotUseWhen`，手工组件页显示“适用/不适用：未补充”。
真实 Runtime 接入前应补齐内置样例的语义目录信息，否则候选发现质量无法稳定验收。

## 5. 已确认关闭的 023 前置问题

- fixture 已标记 `protocolTestOnly`，自动选择只接受真实 Runtime；
- Browser 状态来自实际 Worker 启动和新鲜心跳，受限环境不再误报；
- `runtimeSelection` 与 `runtimeUsed` 已拆分，零 LLM Run 显示模型未调用和 0 Token；
- 终态 SSE 正确关闭，浏览器门禁验证无重连；
- 编辑器输入、select、checkbox 和亮暗主题已统一；
- 组件手动执行、组件历史、结构化输出与 Presentation 既有链路可用。

## 6. 验证记录

| 检查 | 结果 |
|---|---|
| 后端质量与测试 | Ruff 通过；295 passed |
| 迁移 | `0001→0020` 通过 |
| 前端质量与测试 | check/lint/build 通过；50 passed |
| `make demo-check` | **失败**；文档链阶段退出 2 |
| 正常主机进程 E2E | 37 passed |
| 正常主机浏览器门禁 | 2 passed |
| 手工 API/Worker/Vite/浏览器 | Chat、开关、组件、历史、编辑器和主题可操作；Run 事件刷新回放失败 |
| 外部真实语义 Provider | 未执行；配置缺失 |

修复范围和验收条件见 [`NEXT_STEPS-024.md`](../STEP/NEXT_STEPS-024.md)。
