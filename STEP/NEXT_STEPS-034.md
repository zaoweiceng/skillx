# NEXT_STEPS-034：只修亮色主题执行轨迹可读性

依据：[`REPLAY-033.md`](../REPLAY/REPLAY-033.md)、
[`design-compliance-review-034.md`](../review/design-compliance-review-034.md)、
[`code-review-034.md`](../review/code-review-034.md) 和 [`docs/`](../docs/) 下现有设计/规范。

本版不增加产品需求。主闭环已经真实跑通；下一步只修复亮色主题下执行轨迹抽屉深底深字的现有缺陷，并防止回归。
仍为单用户 Local Demo；不增加鉴权、多租户、Token 策略、领域模板或其它功能。

## 1. 035 完成定义

只有以下事实同时成立才能声明通过：

1. 亮色与暗色主题下，执行轨迹标题、空态、时间线、状态和错误文字都清晰可读；
2. 修复作用于轨迹抽屉的统一颜色契约，不是只覆盖当前截图中的单个标题；
3. q2 探索—固化—发布—q4/q1 手动运行—q3 新对话复用继续完整跑通；
4. 工程、Browser、真实 Provider 和真实产品门禁继续通过。

## 2. P1：统一轨迹抽屉背景与前景颜色契约

- [ ] 在 `.trace-drawer` 选择一致的主题策略：使用 `var(--panel)` 等主题背景，或为固定深色抽屉定义成套的深色面板前景、弱化、
  边线、状态与错误颜色；禁止固定背景继续搭配全局亮色前景变量。
- [ ] 检查轨迹标题、空态说明、timeline item、状态 badge、错误码和工具结果，不得只修 `h2`。
- [ ] 保持暗色主题现有视觉层级，不改变对话布局、交互和 Workflow Broker 信息结构。
- [ ] 在真实浏览器切换两种主题，读取最终 computed styles 并人工查看；普通文字目标至少 4.5:1，大字号/非正文按现有设计规范验收。
- [ ] 补组件级回归，覆盖亮色主题下 `.trace-drawer` 的最终背景/前景组合；现有仅校验 Token 对的测试不足以发现固定色混用。

## 3. 回归范围

- [ ] 主题切换后刷新页面，选择状态和轨迹内容保持；
- [ ] 工作流关闭的纯 Agent 对话仍显示计时、Token、历史和轨迹；
- [ ] 工作流开启的新对话仍完成 L0/L1/L2、参数绑定与正确 q3 结果；
- [ ] 固化、Presentation、静态检查、发布、组件运行、调用历史和 RunDetail 继续可点击；
- [ ] 不修改已通过的 resolved-input、选择补全、错误终态或真实语义断言。

## 4. 最终检查

- [ ] REPLAY-033 定向回归通过；
- [ ] `make demo-check` 通过；
- [ ] `make browser-ui-check` 通过，并新增/收紧轨迹抽屉亮色可读性断言；
- [ ] `make traex-real-check` 实际执行且通过，不得 skip；
- [ ] `make traex-real-product-check` 完整通过；
- [ ] 真实启动 API、Worker 和 Vite，人工点击亮暗主题与主闭环；
- [ ] 结束后无 processing Turn、running Run、console error/warning 或遗留服务；
- [ ] `git diff --check` 与文档链通过。

在亮色主题轨迹抽屉仍出现深底深字，或为修配色导致主闭环回归时，不得声明 035 通过。

