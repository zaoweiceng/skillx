# REPLAY-021：真实语义验收与用户界面一致性

日期：2026-08-10

依据：[`NEXT_STEPS-021.md`](NEXT_STEPS-021.md)、
[`review/design-compliance-review-021.md`](review/design-compliance-review-021.md) 和
[`review/code-review-021.md`](review/code-review-021.md)。

执行基线：`774a1c6`。本报告对应当前未提交工作树；本轮未创建 Git 提交。

范围：单用户 Local Demo；不增加鉴权、RBAC、多租户或分布式部署。

## 1. 结论

021 审查发现的代码缺口已修复：

```text
统一 UTC RFC 3339 Z 时间
  → 760～1440px 消息主列优先
  → 持久错误信封 + 用户安全投影 + 按需技术证据
  → 可逆数组 binding
  → Schema 约束的 Presentation
  → 亮暗主题语义状态色
  → 显式、限频、同路径 Provider probe
  → 可重复浏览器门禁
```

`make demo-check` 连续两次通过，`make browser-ui-check` 连续两次通过。

外部真实 Provider 最低语义集仍未执行：当前环境没有 endpoint、model 或 API key，
`ACEDATACLOUD_API_TOKEN` 也未配置。该项不能用 fixture 或本地 mock provider 替代，因此本报告不宣称
P0-1 真实语义验收通过。

## 2. P0 修复

### P0-2：统一公开时间契约

- 复用唯一 `rfc3339()` serializer，SQLite naive datetime 按 UTC 恢复并输出 `Z`。
- Workflow、WorkflowVersion、Run、NodeRun、RunEvent/SSE、ConversationRun、
  ConversationRunEvent、Artifact、Approval、ExecutionCheckpoint、TaskCheckpoint、
  FailureObservation、ReplayEvidence、Turn 和 Presentation 的公开时间均显式序列化。
- RunEvent 列表与 SSE 对同一事件返回相同 `createdAt`。
- 新增源码门禁，禁止 route 把 ORM `*_at` 直接放入 `*At` 响应。
- 回归值 `2026-08-10T07:12:23Z` 在 Asia/Shanghai 浏览器显示为当地 15:12，不再显示 07:12。

### P0-3：760～1200px Chat 可用

- composer 移入消息列的 flex 布局，删除与 trace 耦合的 `right:356px`。
- viewport `≤1200px` 时历史和 trace 均为左右抽屉，消息主列独占内容宽度。
- 真实 Chromium 在 760、903、1024、1280、1440px 检查消息列宽度不小于 420px，
  composer 始终位于 viewport 内。
- 编辑器窄屏 source/preview 最小高度由 600px 降为 360px，标题操作保持不拆字。

### P0-4：用户安全错误投影

- 持久错误统一包含 `code`、`userMessage`、`technicalDetailRef`、`retryable`、`action`
  和受限 `technicalDetail`。
- Run、NodeRun、AgentRun、事件、Checkpoint 和 Presentation 默认 API 只返回用户安全层；
  Chat 不再渲染 Playwright 命令、PID、绝对路径和底层日志。
- `GET /runs/{id}/technical-error` 仅在 RunDetail 用户主动点击后读取技术证据。
- 技术证据统一处理 credential/header、URL query secret、绝对路径、嵌套大文本和 4000 字符上限。
- Provider 401、429、不支持 strict json_schema/function calling、503 和 malformed output
  分别映射到稳定兼容性错误码。

## 3. P1 修复

### P1-1：Presentation 模板

- 使用 `string.Formatter` 解析模板，仅允许 `{json}` 和 Output Schema 顶层字段。
- 未知字段、未闭合花括号、format spec/conversion、对象/数组直接格式化和超过 8000 字符均阻止静态检查/发布。
- 编辑器显示 `{json}` 和所有顶层输出字段 chip，点击插入模板。
- Runtime 按已发布 Output Schema 再校验；失败只把 Presentation 标为 failed，Run JSON 保持 succeeded。
- `{message}` 通过，只有 `message` Schema 时 `{greeting}` 失败。

### P1-2：binding normalization

- 删除 `item in quote` 的 `list_extraction`。
- 数组只接受完整 JSON 数组或版本化明确分隔解析 `delimited_array_v1`。
- 重建时逐项校验顺序、重复、bool/int/float/string 类型和完整元素边界。
- 覆盖 `2026 → [20,26]`、反序、重复、无分隔中文和明确“上海和北京”。
- 无法唯一重建时继续 `ask_user`，不会设置 `serverVerified=true`。

### P1-3：亮色状态语义色

- notice、error、neutral/success/active/warning/danger 使用亮暗主题 token。
- status 字号由 8px 提升到 10px。
- `themeContrast.test.ts` 从真实 CSS 读取亮色 token，自动计算 7 组前景/背景对比度并要求至少 4.5:1。

### P1-4：Provider readiness

- `/health/live` 和 `/health/ready` 不再发起模型生成，只读取协议与缓存事实。
- `POST /health/provider-probe` 是显式付费探测入口，Settings 提示费用并提供“重新探测”。
- probe 复用 AgentHost、真实 sidecar、function calling、Host ToolResult 和 strict output 路径。
- 缓存绑定 endpoint/model/sidecar digest，带 TTL、冷却时间、最近结果、过期状态和下次允许时间。
- probe usage 以 `operational_probe` 单独记账，并在 Settings 显示 Token，不混入用户 Turn。

### P1-5：浏览器门禁

- `make browser-install` 保持显式 bootstrap。
- `browser-check` 只读取 Playwright 锁定版本安装目录并检查 executable，不启动 driver、不下载。
- `e2e-check` 和 `browser-ui-check` 依赖 `browser-check`；缺失时立即提示
  `make browser-install`。
- Chromium 启动失败使用 `pytest.fail`，真实浏览器发布门禁不再 skip 变绿。

## 4. 验证记录

| 门禁 | 结果 |
|---|---|
| `make demo-check` 连续两次 | 通过 |
| 后端 Ruff | 通过 |
| 后端 Pytest | 283 passed |
| Alembic 空库升级 | `0001→0018` 通过 |
| 前端 TypeScript / ESLint | 通过 |
| 前端 Vitest | 11 files / 48 passed |
| 前端 production build | 通过 |
| 进程级 E2E | 34 passed，两轮均通过 |
| `make browser-ui-check` 连续两次 | 2 passed + 2 passed |
| Chromium 响应式尺寸 | 760/903/1024/1280/1440px 通过 |
| console | 0 error / 0 warning |
| sidecar Host ToolResult round-trip | 通过 |
| Provider 401/429/unsupported/503 诊断 | 4 个进程级用例通过 |
| 自动对比度 | 7 个亮色语义组合均 ≥4.5:1 |
| `git diff --check` | 通过 |

## 5. 外部真实 Provider 未通过项

当前环境只确认到以下事实，未读取或记录任何 secret：

```text
SKILLX_MODEL_ENDPOINT: 未配置
SKILLX_MODEL_NAME: 未配置
SKILLX_MODEL_API_KEY: 未配置
ACEDATACLOUD_API_TOKEN: 未配置
OPENAI_API_KEY: 未配置
```

`ai-chat` skill 要求的共享认证文档在本机不存在，workspace 与 `backend/.env` 也没有 AceDataCloud token。
因此没有 provider/model、Run/Turn id、L0/L1、usage 或 sidecar digest 的外部调用证据可记录。

恢复方式：

```bash
export SKILLX_AGENT_PROVIDER_TYPE=real
export SKILLX_MODEL_ENDPOINT=https://provider.example/v1/chat/completions
export SKILLX_MODEL_NAME=provider-model
export SKILLX_MODEL_API_KEY='...'
export SKILLX_TRAEX_COMMAND='["python3","../examples/openai_agent_sidecar.py"]'
export SKILLX_TRAEX_ALLOWED_EXECUTABLES='["python3"]'
```

先在 Settings 主动执行一次付费探测。只有 ready 显示 `type=real`、`protocolAvailable=true`、
`modelAvailable=true`，且 probe 记录同一 sidecar digest、ToolResult round-trip 和完整 usage 后，
才执行 `NEXT_STEPS-021.md` 的七条无 fixture 指令最低语义输入与 10 步真实验收流程。
