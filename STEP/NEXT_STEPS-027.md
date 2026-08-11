# NEXT_STEPS-027：先跑通真实 Agent，再完成通用固化与展示决策

依据：[`REPLAY-026.md`](../REPLAY/REPLAY-026.md)、
[`design-compliance-review-027.md`](../review/design-compliance-review-027.md)、
[`code-review-027.md`](../review/code-review-027.md) 和 [`docs/`](../docs/) 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。天气仅作为一个真实场景测试样本，**不得在通用
代码、seed、Prompt 分支或策略中加入天气专用模板/判断**。

## 1. 027 完成定义

下一版只有同时满足以下事实才能声明通过：

1. 应用在平台隔离目录中能启动真实 TraeX，并完成主动探测、普通 Agent 回复、工作流选择和参数绑定；
2. 未配置 Direct LLM Runtime 时，以上语义调用使用 Agent Runtime，不要求额外 endpoint；
3. 工作流开启/关闭两条 Chat 路径均符合开关语义，计时、Token、历史和审计事实准确；
4. Presentation 由通用固化策略按任务目的提出和冻结，不包含天气或其他领域特判；
5. 至少三个不同领域的真实样本证明“探索—固化—复用—失败接力—修订”使用同一机制；
6. `make demo-check`、`make browser-ui-check`、真实 TraeX 主流程和 UI 人工验收全部通过。

## 2. P0：修正内置 TraeX 的工作目录契约

- [ ] 保留平台创建的每轮隔离目录作为 `-C` 与 subprocess cwd，不把执行目录切到仓库或用户目录。
- [ ] 仅在已 resolve 且确认位于平台本轮 artifact/selection 根目录内时，为原生 `traex exec` 增加
  `--skip-git-repo-check`；越界、symlink 逃逸或任意外部 cwd 必须 fail closed。
- [ ] 保留 `--sandbox read-only`、`--ignore-user-config`、`--ignore-rules`、空 hooks/plugins/skills、MCP allowlist
  和最小环境；跳过 Git 检查不等于放宽文件或工具权限。
- [ ] 为该前置失败定义准确错误码，例如 `TRAE_WORKDIR_UNTRUSTED` 或 `AGENT_WORKDIR_INVALID`，不得归类为版本不兼容。
- [ ] 增加真实 CLI integration gate：用与生产相同的临时非 Git 目录运行受信 TraeX。fake executable 继续用于协议
  回归，但不能作为该验收项证据。

## 3. P0：恢复真实认证并跑通两条 Chat 主路径

- [ ] 由用户在终端重新执行 `traex login`；应用不得读取、回显或持久化 token。
- [ ] 把 `traex login status` 只标记为 `credential_present_unverified`。真实主动探测成功后才标记 authenticated/usable。
- [ ] Settings 探测必须完成随机 challenge、明确 dispatch、严格结构化输出、Host ToolResult round-trip、正 Token
  Usage 和 provider/model identity；任一步失败都保持 unavailable，并给出对应恢复动作。
- [ ] 未配置 `SKILLX_LLM_*`，新建 Chat 默认开启工作流；输入自然语言后由 TraeX L0 搜索、按需 L1 描述、选择精确
  ref、从同一消息绑定参数并执行工作流。不得增加关键词直匹配回退。
- [ ] 关闭工作流后再次输入相同需求，轨迹中不得披露或调用 `workflow.*`，应由通用 TraeX 完成或明确能力不足。
- [ ] 验证多字段、数字、布尔、枚举、数组、URL 等通用输入；缺失/歧义才 ask_user，无候选才 continue_agent。
- [ ] 记录 attempted/dispatched/completed、真实 Token、provider/model/runtime/protocol、选择理由、bindingSources、
  Turn/Run/checkpoint；刷新后可恢复，Secret 不落库。

## 4. P1：删除天气特例，建立通用 Presentation 固化决策

### 4.1 数据和运行时边界

- [ ] 从 `_weather_workflow()` seed 删除天气专用 `presentationPolicy`；通用运行时不得判断名称、tag、字段名或领域。
- [ ] 规范结构化输出始终是事实源，Presentation 只是可选派生结果；Presentation 失败不得改变成功 Run 或 JSON。
- [ ] 运行时只执行已冻结策略，不自行猜测是否需要自然语言，也不因内容像天气而改变 producer。

### 4.2 固化阶段的通用提议

- [ ] 扩展 `DistillationProposal` 或等价契约，生成 `presentationPolicyProposal`，至少包含：
  `enabled`、`audience`、`producer`、`language`、`maxLength`、模板/提示词（适用时）、理由、证据和置信度。
- [ ] 判断只依据任务目的和数据流：结果是否直接展示给人、是否为机器消费、是否为 Fragment/组合中间值、输出
  schema 是否能无歧义稳定表达、文本是否需要摘要/解释。
- [ ] 默认规则：内部步骤、Fragment 和机器消费结果关闭；直接面向用户且可读文本有价值时开启；简单稳定字段映射可
  选 template；复杂总结/解释才选 LLM/Agent Runtime。
- [ ] 在固化审阅 UI 同时展示结构化契约和 Presentation 提议。用户确认或修改后再发布，并冻结到不可变
  WorkflowVersion；记录 proposal source/digest 与审阅结果。
- [ ] template 必须通过 schema 字段静态校验；LLM producer 必须遵循 Runtime 选择、预算、Usage、失败隔离和历史投影。

### 4.3 跨领域验收

- [ ] 用天气查询验证“简单结构化结果 → 可选模板文本”，但测试通过通用固化/编辑/发布入口建立策略，不检查 seed
  中存在天气字面量。
- [ ] 用网页或长文本总结验证“复杂结果 → LLM/Agent Presentation”。
- [ ] 用文件操作、数据转换或组合 Fragment 验证“机器/中间结果 → Presentation 默认关闭”。
- [ ] 三类样本使用同一提议契约、审阅 UI、发布和 Runtime；新增静态检查，防止代码出现领域专用分支。

## 5. P1：修正 dispatch、认证与可用性事实

- [ ] 删除认证失败、模型不可用、限流时补发 `model.request.dispatched` 的逻辑，并修正对应单测。
- [ ] 只有 TraeX 明确版本化事件、请求 ID 或可验证 Usage 才能证明 dispatched；否则保持 false，attempted 单独为 true。
- [ ] 把 installed、credential present、active authenticated、protocol compatible、tool round-trip、usable 分开持久化和展示。
- [ ] Runtime Resolver 可以把内置 TraeX 标为候选 fallback，但在 probe 失败时不得把“当前生成路径”展示成已可用。
- [ ] 将工作目录、认证、模型不可用、限流、协议不兼容、malformed output 映射为不同错误码、用户文案和恢复动作。
- [ ] 覆盖 dispatch 前认证失败、dispatch 后 timeout/429、成功但 Usage 缺失、成功完整 Usage 四类真实审计。

## 6. P2：让 MCP tool 名称无碰撞

- [ ] 生成符合 TraeX/MCP 限制的稳定 tool name，并加入 capability ref digest/hash 后缀。
- [ ] 保存显式双向映射；构建 descriptor 时检测重复，任何碰撞都 fail closed，不允许字典静默覆盖。
- [ ] 覆盖 `a.b`/`a_b`、Unicode、超长 ref、相同短名不同版本和多个 Fragment 组合。
- [ ] Tool call、ToolResult、provenance 和审计始终使用原始 capability ref，不把规范化名称当业务身份。

## 7. 真实交付门禁

1. `make demo-check` 连续两次通过；后端、前端、空库迁移和独立进程 E2E 全部通过；
2. `make browser-ui-check` 连续两次通过，760、903、1024、1280、1440px 明暗主题无溢出、遮挡或不可点击控件；
3. 启动真实 SQLite、API、Worker、Vite、Chromium，只使用重新登录的 TraeX，完成 Provider probe；
4. 工作流开启时完成“自然语言 → Agent 选择 → 同消息参数绑定 → Worker 执行 → JSON → 可选 Presentation →
   历史回看”，关闭时确认零 `workflow.*`；
5. 使用天气、内容总结、非终端/组合任务三个领域走完首次探索、固化审阅、发布、第二次复用、故障接力与修订，
   证明不存在领域专用实现；
6. 真实制造认证过期、模型超时和 ToolResult 失败，确认错误码、dispatch、Token、checkpoint 与恢复动作准确；
7. 组件历史、RunDetail、Chat 历史、计时和 Token 刷新后一致；console 0 error/warning；
8. 文档链、`git diff --check` 和提交归档检查通过；REPLAY 只记录可复现的真实命令、环境身份和结果。

在“真实 TraeX 主流程、准确审计、通用 Presentation 固化、跨领域闭环”全部通过前，不得声明整体通过。
