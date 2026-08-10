# NEXT_STEPS-023：让“可配置”变成“真实可用”

依据：[`REPLAY-022.md`](REPLAY-022.md)、
[`review/design-compliance-review-023.md`](review/design-compliance-review-023.md)、
[`review/code-review-023.md`](review/code-review-023.md) 和 `docs/` 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。首要目标不是继续扩充框架，而是让用户用一条普通
自然语言消息完成“Agent 判断是否调用工作流 → 提取参数 → 执行 → 返回结构化和阅读结果”，并让 Settings 只展示有真实证据的
运行时状态。

## 1. 023 完成定义

以下场景必须在全新数据库、真实 API、独立 Worker、真实语义 Agent Runtime、Vite 和 Chromium 中一次完成：

```text
用户：上海天气如何
  → Agent 渐进搜索/披露天气工作流
  → 从同一消息绑定 city=上海，并记录可逆 quote/provenance
  → 不追问已提供的参数
  → Workflow Runtime 执行
  → 返回 JSON + 适合普通用户的阅读结果 + Run/Usage/Runtime 证据
```

fixture 只证明协议，不能作为本场景通过证据。禁止通过城市名、天气、URL 等业务正则伪造通配型语义能力。

## 2. P0：建立真实语义 Local Demo 基线

### P0-1：隔离 fixture 与产品可用状态

- [ ] `fixture` 明确标记为 `protocolTestOnly=true`，不得让 Settings 或 Chat 把它描述为真实语义可用。
- [ ] 工作流开启且没有真实语义 Agent Runtime 时，UI 明确提示“自动选择不可用”；允许用户关闭工作流继续普通 Agent，
  但不能假装自动选择已就绪。
- [ ] Local Demo 启动命令提供一条明确的 `traex_managed` 配置路径；Host 不要求额外 LLM endpoint。
- [ ] 如果选择 `openai_sidecar`，它自己的 endpoint/model/key 只属于 Agent Runtime；Direct LLM 配置保持独立可选。

### P0-2：真实语义选择与参数绑定

- [ ] 用真实 Agent 输出 `WorkflowSelection`，继续由服务端执行 Schema、disclosure、quote、messageId 和 provenance 校验。
- [ ] 一条消息中同时完成意图、候选比较和已有参数提取；仅真正缺失或歧义字段才 `ask_user`。
- [ ] 覆盖单字段、多字段、数字、布尔、枚举、数组、URL、历史消息引用和歧义拒绝；不得依赖 `[[SELECTION:*]]`。
- [ ] 最低回归必须包含“上海天气如何”，断言第一 Turn 直接 `run_full`、`city=上海`、source 为当前消息且
  `serverVerified=true`。
- [ ] 记录真实 provider/runtime/model、Turn/Run id、digest、耗时和 Token；不得记录 key。

## 3. P0：Browser Runtime 真实 readiness

- [ ] 由实际 Browser Worker 执行有界启动预检，而不是 API 根据 allowlist 推断。
- [ ] Worker 持久化带时间戳的 runtime heartbeat：Chromium 路径/版本、Playwright 兼容性、启动结果和证据摘要。
- [ ] 状态至少区分 `available`、`binary_missing`、`permission_denied`、`stale`、`unconfigured`。
- [ ] `/health/ready` 和 Settings 读取同一事实；allowlist 只表示网络策略已配置，不等于 Browser 可用。
- [ ] 在当前受限环境复验为 `permission_denied`；正常主机权限复验为 `available`，文案给出可执行的修复建议。

## 4. P1：修正 Runtime 审计语义

- [ ] 将启动时冻结的候选/策略命名为 `runtimeSelection`，不要提前写成 `runtimeUsed`。
- [ ] `runtimeUsed` 只由实际 LLM 节点或 Presentation 调用产生，并与 ModelUsage 一致。
- [ ] `zero_llm` Run 没有 ModelUsage 时显示“模型未调用 / 0 Token”，不得显示 Agent fallback。
- [ ] hybrid Run 分别展示选择策略、实际调用和每次 Usage；执行失败也保留唯一费用事实。
- [ ] 增加零 LLM、fake structured、Direct LLM、Agent fallback、未调用 Presentation 五类契约测试。

## 5. P1：恢复版本化审计文档

- [ ] 恢复提交 `9f9fb43` 删除的 39 份 REPLAY/NEXT_STEPS，或迁移到显式 `review/archive/`。
- [ ] 若归档，保留原文件名、内容、顺序、来源提交和 SHA-256 索引；根目录提供索引，旧链接可解析。
- [ ] 修正 `REPLAY-022.md` 的基线描述：实际改动已提交于 `9f9fb43`，不是当前未提交工作树。
- [ ] CI 增加文档链检查：`REPLAY-N`、三份 `N+1` 审查文档和引用目标不能静默断链。

## 6. P2：前端状态与视觉收尾

- [ ] 终态 RunDetail 不显示脉冲 LIVE/SSE；改为“实时流已关闭”，并展示持久事件或明确历史未加载。
- [ ] 为 `.inspector-fields` 的 input/textarea/select 统一最小高度、padding、圆角、hover/focus/disabled 状态。
- [ ] 删除 `.inspector-field` 的硬编码暗色背景，全部使用主题 token；主题化 checkbox/select。
- [ ] 亮暗主题逐页复验 Chat、Settings、Workflow Editor、Component、RunDetail；console 0 error / 0 warning。
- [ ] 760、903、1024、1280、1440px 检查无遮挡、无横向溢出、主要输入区可操作。

## 7. 真实验收矩阵

| 场景 | 必须结果 |
|---|---|
| 工作流开启 + 真实 Agent | 普通自然语言选择、绑定、执行一次完成 |
| 工作流关闭 + 真实 Agent | 全 Turn 不披露/调用 `workflow.*` |
| LLM ready + Agent ready | LLM 节点/Presentation 使用 Direct LLM |
| LLM 未配置 + Agent ready | 使用无 Tool Agent fallback，Schema/Usage 完整 |
| Direct LLM 401/429/timeout | 不切换 Runtime，错误与单一费用事实稳定 |
| zero LLM 工作流 | ModelUsage 空、0 Token、UI 显示模型未调用 |
| Browser 无 OS 权限 | 执行前 Settings 已显示 `permission_denied` |
| Browser 正常权限 | 首次探索、固化、二次复用、失败接力全部完成 |

## 8. 门禁与交付

1. `make demo-check` 连续两次通过；
2. `make browser-ui-check` 在目标本地环境连续两次通过；
3. 七条以上无 fixture 指令的真实语义用例全部通过，其中“上海天气如何”不得追问 `city`；
4. Browser readiness 负向/正向环境证据都通过；
5. 零 LLM Runtime 审计、终态文案和编辑器亮暗主题回归通过；
6. 历史文档链恢复并通过链接/哈希检查；
7. 新增 `REPLAY-023.md`，记录真实 Runtime 配置类别、Run/Turn/Usage、浏览器预检和视觉证据。

完成以上项目后再进行 024 审查。在真实语义输入一次完成、Browser readiness 不再误报之前，不得声明整体通过。
