# REPLAY-027：受信工作目录契约、通用 Presentation 固化与准确审计

日期：2026-08-11

依据：[`NEXT_STEPS-027.md`](../STEP/NEXT_STEPS-027.md)、
[`design-compliance-review-027.md`](../review/design-compliance-review-027.md) 和
[`code-review-027.md`](../review/code-review-027.md)。

执行基线：`d6431c0`。当前未创建结果提交；本文记录待提交工作树的复现事实，不填写不存在的结果提交号。

## 1. 结论

027 针对 026 审查列出的 1 个 P0、3 个 P1、1 个 P2 完成了代码修复，本地交付门禁全部通过：

- P0-1：内置 TraeX bridge 对“平台本轮隔离目录”显式传 `--skip-git-repo-check`，并在启动前校验该目录
  确实位于平台 artifact 根目录内；越界、symlink 逃逸或任意外部 cwd 一律 fail closed，映射为独立错误码
  `TRAE_WORKDIR_UNTRUSTED`，不再被误判为版本不兼容；read-only sandbox、忽略用户配置/规则、空
  hooks/plugins/skills 与 allowlist 全部保留。
- P1-1：删除 `_weather_workflow()` seed 中的天气专用 `presentationPolicy`；固化阶段新增通用
  `presentationPolicyProposal`，只依据任务目的与数据流（机器消费/组合中间值、输出 schema 形态）决定
  disabled/template/llm，并携带理由、证据与置信度供审阅。
- P1-2：删除认证失败、模型不可用、限流退出时补发 `model.request.dispatched` 的逻辑；无可验证 dispatch 证据时
  `request_dispatched` 保持 false，`attempted` 由上层单独记录。
- P1-3：`traex login status` 只投影为 `credential_present_unverified`（登录仅证明本地凭据存在，不代表可用）；
  readiness 新增分离的 `capabilityStates`（installed/credentialPresent/activeProbeAuthenticated/
  protocolCompatible/usable），Settings 区分“候选 Runtime”与“已验证可用 Runtime”。
- P2-1：MCP tool 名称在规范化基础上追加 capability ref digest 后缀，一一对应；构建 descriptor 时用显式反向
  映射检测碰撞并 fail closed，Tool call/ToolResult/provenance 仍使用原始 `capabilityRef`。

`make demo-check` 在最终工作树连续两次通过（退出码 0），`make browser-ui-check` 连续两次通过。

本轮在当前 Agent 会话环境中仍未使用真实登录的 TraeX 跑通语义主流程：协议回归继续使用仓库维护的可执行
fixture（真实经过子进程、HTTP MCP、Host ToolResult round-trip 与 callId 配对），fixture 不作为真实 Provider
证据。因此本文确认**代码与本地交付门禁通过**，真实 TraeX 主流程需在允许 `traex` 访问自身状态目录并已
`traex login` 的普通主机终端完成验收。

## 2. P0-1：受信工作目录契约

[`traex_builtin.py`](../backend/app/runtime/traex_builtin.py) 新增 `_trusted_working_directory()`：把
`request.working_directory` 与 `request.working_directory.resolve()` 都对齐到 `settings.artifact_root.resolve()`，
仅当解析后路径等于根目录或位于其内部时才放行。`resolve()` 展开全部 symlink 组件，逃逸出根目录的软链在此被拒。

`traex exec` 命令在保留 `--ephemeral`、`--sandbox read-only`、`--ignore-user-config`、`--ignore-rules`、空
`hooks/plugins/skills` 与 `--allowed-tool` allowlist 的同时，显式加入 `--skip-git-repo-check`；`-C` 与 subprocess
`cwd` 仍指向该受信隔离目录，不切到仓库或用户目录，每轮隔离不变。

`_error_code()` 把 TraeX 自身 `Not inside a trusted directory / --skip-git-repo-check` 文案映射为
`TRAE_WORKDIR_UNTRUSTED`；[`error_policy.py`](../backend/app/application/error_policy.py) 为该码定义了不可重试的
策略与独立用户文案/恢复动作（`view_run`），不再归入 `AGENT_PROTOCOL_INCOMPATIBLE`。

真实 CLI integration gate：[`test_traex_builtin.py`](../backend/tests/test_traex_builtin.py) 现在把
`settings.artifact_root` 指到临时非 Git 目录，round-trip 与认证失败用例都在受信隔离目录内启动 fixture；新增
`test_builtin_bridge_rejects_untrusted_working_directory` 证明根目录之外的 cwd 直接 fail closed，绝不启动 TraeX。

## 3. P1-1：通用 Presentation 固化决策

[`seed.py`](../backend/app/seed.py) 的 `_weather_workflow()` 不再写入任何 `presentationPolicy`；通用平台 seed 只
产出规范 JSON。[`distillation.py`](../backend/app/application/distillation.py) 新增
`_propose_presentation_policy(definition, machine_only)`：

| 数据流事实 | 决策 |
|---|---|
| 业务节点仅为数据变换/文件/工件/条件等机器流水线 | `enabled=false`，`audience=internal`（机器/中间结果） |
| 直接面向用户且输出仅含可无歧义拼接的标量字段 | `enabled=true`，`producer=template`（按 schema 字段拼模板） |
| 直接面向用户但含嵌套对象/数组等复杂结构 | `enabled=true`，`producer=llm`（选定 Runtime 摘要/解释） |

该函数不识别任何领域名称、tag 或字段名。`_enrich_distilled_definition` 把冻结策略写入
`metadata.presentationPolicy`，把带 `source/reason/evidence/confidence` 的提议写入
`metadata.presentationPolicyProposal`，并透出到 `distill` 分析结果供固化审阅 UI 展示；用户可在通用
`.presentation-policy-editor` 确认或修改后发布，`normalize_presentation_policy` 在发布时冻结进不可变
WorkflowVersion。template 仍受 schema 字段静态校验，LLM producer 仍走 Runtime 选择与失败隔离。

跨领域验收（不检查 seed 天气字面量）：

- 简单结构化结果（标量字段）→ 模板文本：`test_presentation_proposal_simple_structured_uses_template`，且
  E2E-2 通过“通用 draft 编辑 → 发布 → 运行”的入口建立天气组件的可读文本（seed 本身无模板）。
- 复杂结果（嵌套对象/数组，如网页/长文本总结）→ LLM/Agent Presentation：
  `test_presentation_proposal_complex_result_uses_llm`。
- 机器/组合中间结果 → 默认关闭：`test_presentation_proposal_machine_result_is_disabled`。
- 静态检查 `test_presentation_no_domain_specific_branch_in_generic_runtime` 断言
  `distillation.py`/`product_history.py` 不含 `天气/weather/温度/℃` 字面量，也不按 capability 名称或 tag 分支。

## 4. P1-2 / P1-3：dispatch 与可用性事实

内置 bridge 只在收到 TraeX 明确 `item.started/completed`、`turn.completed` 等模型事件时才 yield
`model.request.dispatched`；认证/模型/限流/工作目录失败在模型请求前退出，不再伪造 dispatch。对应单测
`test_builtin_bridge_maps_auth_failure` 改为断言 `request_dispatched is False`。

[`agent_provider.py`](../backend/app/application/agent_provider.py) 的登录探测返回
`credential_present_unverified`；readiness 增加分离的 `capabilityStates`，`usable` 仍等价于既有的
`modelAvailable`（新鲜、adapter 受信、主动探测 ok）。[`SettingsPage.tsx`](../frontend/src/pages/SettingsPage.tsx)
据此把生成路径区分为“已验证可用 Agent Runtime”与“候选 Agent Runtime（未验证可用）”，并对
`credential_present_unverified` 给出“登录不等于可用，请执行主动探测”的指引。

## 5. P2-1：MCP tool 名称无碰撞

[`traex_builtin.py`](../backend/app/runtime/traex_builtin.py) 的 `_tool_name()` 生成
`<slug>__<sha256(ref)[:12]>`，满足 MCP/TraeX 字符与长度限制且与原始 ref 一一对应；`_tool_descriptor_map()`
用显式反向映射检测重复，任何碰撞抛 `AGENT_PROTOCOL_INCOMPATIBLE`，不允许字典推导静默覆盖。测试覆盖
`a.b`/`a_b` 标点差异、Unicode、超长 ref、多 Fragment 组合以及强制碰撞 fail closed。

## 6. 最终验证

| 门禁 | 最终结果 |
|---|---|
| `make demo-check` 第一次 | 通过；后端 318 passed，前端 51 passed，E2E 37 passed |
| `make demo-check` 第二次 | 通过；后端 318 passed，前端 51 passed，E2E 37 passed in 140.10s |
| Alembic 空库升级 | `0001 → 0023` 通过 |
| `make browser-ui-check` 第一次 | 2 passed in 27.64s |
| `make browser-ui-check` 第二次 | 2 passed in 26.00s |
| 后端 Ruff | 通过（`app tests migrations e2e` 与四个 example）|
| 前端 check / lint / build | 通过；11 files / 51 passed，生产构建成功 |
| `git diff --check` | 通过 |

`make demo-check` 同时覆盖 Ruff、后端单测、前端 check/lint/test/build、文档链（102 个版本化 Markdown）和独立
进程 E2E。

## 7. 未完成的真实 TraeX 验收

以下事实需在允许 `traex` 访问 `~/.trae/cli` 状态目录、且已在终端 `traex login` 的普通主机上完成，应用不得读取、
回显或持久化 token：

1. Settings 主动探测完成随机 challenge、显式 dispatch、strict output、Host ToolResult round-trip、正 Token
   Usage 与 provider/model identity，并借修复后的 `--skip-git-repo-check` 越过目录检查；
2. 工作流开启时“自然语言 → L0/L1 → 精确 ref 选择 → 同消息参数绑定 → Worker 执行 → JSON → 可选 Presentation
   → 历史回看”，关闭时轨迹零 `workflow.*`；
3. 天气、内容总结、非终端/组合任务三个领域走完首次探索、固化审阅、发布、二次复用、故障接力与修订，证明
   通用 Presentation 固化与失败接力使用同一机制；
4. 真实制造认证过期、模型超时与 ToolResult 失败，确认错误码、dispatch、Token、checkpoint 与恢复动作准确。

在上述真实 TraeX 主流程验收完成前，本文只声明“代码与本地交付门禁通过”，不声明 027 整体通过。
