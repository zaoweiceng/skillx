# NEXT_STEPS-021：真实语义验收与用户界面一致性

依据：[`REPLAY-020.md`](../REPLAY/REPLAY-020.md)、
[`review/design-compliance-review-021.md`](../review/design-compliance-review-021.md)、
[`review/code-review-021.md`](../review/code-review-021.md) 和 `docs/` 下设计/规范文档。

范围：单用户 Local Demo；不增加鉴权、RBAC、多租户和分布式部署。021 首要目标仍是让普通用户在真实模型和常见窗口尺寸下，
从提问到最终结果完整跑通且信息可信。

## 1. 021 完成定义

```text
无测试指令的自然语言
  → 真实 Agent 渐进发现并确定性取证
  → Workflow / 通用 Agent
  → 用户安全错误或 JSON + 可读 Presentation
  → 可信时间/Token/历史
  → 亮暗主题和 760～1440px 均可操作
```

fixture、城市正则、目录第一项、刷新后才正确、只跑单测或只在大屏可用，都不能作为通过证据。

## 2. P0：主流程阻塞项

### P0-1：用真实外部 Provider 完成最低语义集

- [ ] 选择至少一个实际支持 function calling 与 strict structured output 的 provider/model；密钥只通过环境/Secret 引用，不写仓库。
- [ ] ready 显示 `type=real`、同一 sidecar 协议可用、model/tool round-trip 可用后再开始业务验收。
- [ ] 不使用 `[[SELECTION:*]]`、`[[MODE:*]]` 或其他 fixture 指令，完成：

| 输入 | 通过条件 |
|---|---|
| `上海天气如何` | 一轮内选择天气 Workflow、`city=上海`、不追问 |
| `帮我看看天气` | 只追问 city；回答后恢复原 selection |
| `总结 <本地动态 URL>` | 自动选择已有 Workflow 或继续 Agent，最终正文正确 |
| `把 q12 的记录导出` | 正确提取 q12 |
| `比较上海和北京天气` | 按 Schema 数组/组合；不支持时澄清 |
| 普通闲聊 | continue_agent，不强行调用 Workflow |
| 关闭工作流后的同类输入 | 无任何 `workflow.*` Tool/Run |

- [ ] 真实 Provider 测试记录 provider/model、sidecar digest、Run/Turn id、L0/L1、usage 完整性和结果；不记录 key。
- [ ] 网络错误、限流、provider 不支持 json_schema/function calling 时返回明确兼容性诊断，不冒充语义失败。

### P0-2：统一所有公开时间契约

- [ ] 创建唯一 API datetime serializer/DTO；Workflow、WorkflowVersion、Run、NodeRun、RunEvent、ConversationRunEvent、
  Artifact、Approval、Checkpoint 等所有公开时间统一输出 UTC RFC 3339 `Z`。
- [ ] 禁止 route response 直接携带 ORM datetime；代码检查或类型层面阻止遗漏。
- [ ] 列表、详情、SSE 与历史接口对同一事实返回完全相同的时间字符串。
- [ ] 在 UTC 与 Asia/Shanghai 浏览器各跑一次，组件/工作流/Run/Chat 历史显示同一当地时刻。
- [ ] 回归本轮案例：15:12 的 Run 不再显示为 07:12。

### P0-3：保证 760～1200px Chat 可用

- [ ] 以消息主列为第一优先级，定义最小可用宽度；不足时历史改抽屉、trace 改抽屉或 tab，不再固定占 520px。
- [ ] composer 跟随消息容器布局，不使用与 trace 固定宽度耦合的 `right:356px`。
- [ ] 推荐使用 container query；若使用 viewport breakpoint，必须计入 224px 桌面侧栏和 page padding。
- [ ] 在 760、903、1024、1280、1440px 实测：消息、JSON、Presentation、错误卡、输入框和历史均不横向遮挡或纵向挤成窄条。
- [ ] 工作流编辑器同一宽度范围内，标题/按钮不拆字，单列 palette/inspector 不保留 600px 无内容最小高度。

### P0-4：建立用户安全错误投影

- [ ] Error 持久模型区分 `code`、`userMessage`、`technicalDetailRef`、retryable/action；公共 API 默认只返回用户安全层。
- [ ] Chat 只显示稳定 code、简短说明、建议动作和“查看运行”；禁止展示命令行、PID、绝对路径、堆栈或 Chromium 日志。
- [ ] RunDetail 的技术视图按需读取脱敏/限长证据，Local Demo 也不默认把数千字符日志塞入正文。
- [ ] Browser/Agent/MCP/LLM 的异常统一走该投影；Secret、header、URL query、路径和大文本使用同一 redact policy。
- [ ] 回归 Playwright launch、DNS、HTTP 503、provider 401/429、timeout 和 malformed output。

## 3. P1：契约与视觉质量

### P1-1：校验 Presentation 模板

- [ ] 静态检查解析模板，只允许 `{json}` 和 Output Schema 顶层字段。
- [ ] 未知字段、未闭合/转义错误、对象直接格式化和超长模板定位到具体字段并阻止发布。
- [ ] 编辑器展示可选输出字段 chip，点击插入，降低用户记忆字段名的成本。
- [ ] 运行时检查残留 placeholder；若出现则 Presentation failed，结构化 Run 保持 succeeded。
- [ ] 测试 `{message}` 通过、`{greeting}` 对只有 message 的 Schema 失败。

### P1-2：收紧 binding normalization

- [ ] `serverVerified=true` 必须表示能从 evidence 通过已版本化 parser 唯一重建 binding。
- [ ] 数组只接受 JSON、明确分隔格式或字段/schema 专用 parser；校验顺序、重复、数值边界和 item type。
- [ ] 删除纯 `item in quote` 的 list_extraction 判定。
- [ ] 增加 `2026 → [20,26]`、重复元素、子串重叠、无分隔中文和数组顺序负向测试。
- [ ] 无法确定性验证时 ask_user，并允许表单化补参。

### P1-3：完成亮色状态语义色

- [ ] notice、success/warning/danger/neutral badge 使用主题 token，不共享暗色浅前景。
- [ ] 普通文字对比度 ≥4.5:1；大字号和非文字状态 ≥3:1；8px 状态字建议提升到至少 10～11px。
- [ ] 覆盖 Settings、RunDetail、Chat、Component/Workflow 历史、approval 和 error。
- [ ] 加入自动对比度计算和亮/暗截图回归；存在性断言不能代替可读性。

### P1-4：重构 Provider readiness

- [ ] `/health/live` 和 `/health/ready` 不同步发起付费模型生成。
- [ ] 真实模型探测改为显式或后台限频任务，带 TTL、超时、最近成功/失败时间和下一次允许探测时间。
- [ ] probe 必须走与 Agent 相同的 sidecar/function calling/Host ToolResult 路径，避免探测不同代码路径产生假阳性。
- [ ] operational probe usage 单独记账；不得混入用户 Turn，也不得完全不可见。
- [ ] Settings 只读取缓存事实，并提供用户主动“重新探测”按钮及费用提示。

### P1-5：让浏览器门禁可重复

- [ ] 将 Chromium 安装移出 phony 测试依赖，提供显式 `make browser-install` bootstrap。
- [ ] 门禁只检查锁定版本 executable 是否存在；缺失时立即给出安装指令，不在测试中静默下载/等待。
- [ ] 消除首次 skip、再次 pass 的环境抖动；skip 必须使要求真实浏览器的发布门禁失败而不是绿灯。
- [ ] `make browser-ui-check` 连续运行两次均应在固定时间内 2 passed。

## 4. 推荐实施顺序

1. 先完成 P0-2、P0-3、P0-4，让现有每次运行的时间、页面和错误都可信可用。
2. 完成 P1-1、P1-2，收紧模型进入 Runtime 前后的数据契约。
3. 完成 P1-3、P1-4、P1-5，恢复视觉与门禁可信度。
4. 最后接入真实 Provider 跑 P0-1；发现兼容问题应修 adapter，不允许改回 fixture 特判。

## 5. 021 真实验收流程

使用全新 Alembic 数据库，实际启动 API、Worker、Vite、本地 HTTP 站点、真实 Provider 和 Chromium：

1. 在 903px 亮色窗口新建 Chat；历史/trace 为抽屉，消息主列和 composer 正常。
2. 输入“上海天气如何”，真实 Agent 一轮提取并服务端验证 `city=上海`，天气 JSON 与可读说明自动出现。
3. 终态耗时/Token 等待 2 秒不增长；reload 后一致；Run/组件/工作流历史均显示当地正确时间。
4. 关闭工作流后发送同类问题，确认无 `workflow.*`；重新开启只影响下一 Turn。
5. 配置正确 Presentation 模板并发布；未知 placeholder 必须在静态检查阻止。
6. 触发一次 Browser transport 失败，Chat 只显示用户安全错误；RunDetail 可进入受控技术证据。
7. 手动组件成功/失败各一次，历史、RunDetail、JSON/文本复制和 Presentation 状态正确。
8. 在 760、903、1024、1280、1440px 与亮/暗主题遍历 Chat、Components、Editor、RunDetail、Settings。
9. 执行真实 503 失败接力，确认 source/checkpoint/continuation/原 Chat/Turn/Usage 无回归。
10. 连续两次运行 `make demo-check` 与 `make browser-ui-check`；全部通过且 console 0 error/0 warning。

## 6. 交付清单

- [ ] P0/P1 全部关闭；
- [ ] 真实 Provider 最低语义集通过；
- [ ] 所有公开时间带 `Z`；
- [ ] 903px Chat 可用；
- [ ] Presentation 未知字段无法发布；
- [ ] 用户页面无底层日志；
- [ ] 亮色状态对比度达标；
- [ ] 浏览器门禁连续两次稳定通过；
- [ ] 新增 `REPLAY-021.md`，记录真实 provider、Run/Turn/时间/视觉证据和所有未通过项。
