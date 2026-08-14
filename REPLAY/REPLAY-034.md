# REPLAY-034：修复亮色主题执行轨迹抽屉深底深字

日期：2026-08-14

依据：[`NEXT_STEPS-034.md`](../STEP/NEXT_STEPS-034.md)、
[`code-review-034.md`](../review/code-review-034.md)、
[`design-compliance-review-034.md`](../review/design-compliance-review-034.md) 和
[`REPLAY-033.md`](./REPLAY-033.md)。

## 1. 结论

034 指出的唯一 P1 缺陷已按最小修改原则修复：执行轨迹抽屉 `.trace-drawer` 不再写死深色背景。切换亮色主题后，
抽屉背景随主题 Token 变化，内部继承全局前景变量的标题、空态、时间线、状态与错误文字重新清晰可读，暗色主题视觉
层级保持不变。

本轮没有新增产品功能、API、数据库迁移、领域模板、鉴权或计费逻辑，也没有改动对话布局、交互和 Workflow Broker
信息结构。改动只涉及一处 CSS 背景声明与一条组件级可读性回归，主闭环无回归。

## 2. P1：统一轨迹抽屉背景颜色契约

根因不是主题 Token 本身不合格，而是组件把固定色与全局主题变量混用。[`.trace-drawer`](../frontend/src/index.css#L1498)
基础规则无条件使用 `background: #0e191f`，却没有锁定适合深底的前景色，也没有亮色主题覆盖；抽屉内标题继承
`--text`、空态与工具/摘要文字继承 `--muted`、eyebrow/类型继承 `--accent`、错误码继承 `--danger`。切换亮色主题后，
这些深色前景落在深色固定背景上，标题对比度约 **1.09:1**、空态文字约 **3.23:1**，几乎不可见。

修复选择“让抽屉背景使用主题面板 Token”这一条一致策略：把基础规则的背景改为 `background: var(--panel)`。这与该
文件里已存在的移动端 media query（`.trace-drawer { background: var(--panel); }`）行为一致，因此不引入第二套配色，
也不需要为深色抽屉再定义一整套深色前景 Token。改动后抽屉在暗色主题仍取深色 `--panel`（`#111d24`），在亮色主题取
白色 `--panel`（`#ffffff`），标题/空态/时间线/状态 badge/错误码/工具结果的最终组合全部满足普通文字可读性目标，
而不是只给 `h2` 打补丁。

## 3. 回归收紧：校验最终组合而非抽象 Token 对

现有 [`themeContrast.test.ts`](../frontend/src/lib/themeContrast.test.ts) 只验证语义状态 Token 对，无法发现“固定背景色 +
主题前景变量”的混用，因此对本缺陷是假阴性。本轮新增组件级回归：

- 新增 `traceDrawerBackground()` 解析 `.trace-drawer` **基础规则**（行首、非 `@media` 内）的最终背景声明；
- 新增 `resolveColor()` 兼容 `var(--token)` 与写死十六进制两种写法，因此固定背景色不会被静默跳过；
- 新增用例校验 `--text`、`--muted`、`--accent`、`--danger` 四个抽屉内继承前景在该背景上的对比度均 `>= 4.5:1`。

验证过该测试能真正捕获缺陷：临时把背景改回 `#0e191f` 时四条新断言全部失败（`--danger` 约 2.92:1、标题继承色更低），
恢复为 `var(--panel)` 后全部通过。

## 4. 最小修改边界

- 仅改一处 CSS：`.trace-drawer` 基础规则背景 `#0e191f` → `var(--panel)`；未改抽屉边框、时间线、状态 badge、错误
  面板等既有样式，未改任何 TSX 结构或交互。
- 未新增亮色专用覆盖块或第二套深色前景 Token，避免引入并行配色来源。
- 测试仅在既有 `themeContrast.test.ts` 内追加纯函数与用例，未引入新依赖、未改既有断言。

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `npm run check`（tsc 类型检查） | 通过，无错误 |
| `npm run lint`（eslint） | 通过，无告警 |
| `npm run test`（vitest） | 通过；11 文件 60 passed（含轨迹抽屉 4 条新断言） |
| `npm run build`（tsc + vite build） | 通过；产物正常，2557 modules |
| 回归有效性反证 | 临时回退固定色后 4 条新断言失败，恢复后通过 |
| `git diff --check` | 通过，无空白错误 |

## 6. 待人工完成项

以下门禁需真实环境执行，不在本轮自动完成范围内，按 [`NEXT_STEPS-034.md`](../STEP/NEXT_STEPS-034.md) 第 4 节收尾：

- `make browser-ui-check`（真实启动 API、Worker、Vite 的 Browser 门禁）；
- `make traex-real-check` 与 `make traex-real-product-check`（真实 Provider/产品门禁，不得 skip）；
- 真实浏览器切换亮暗主题人工查看轨迹抽屉最终 computed styles，并确认主闭环无回归、无遗留服务与 console error。

本轮自动可验证的前端质量门禁（check/lint/test/build）与 `git diff --check` 均已通过，且未触碰后端、迁移与真实
语义断言，主闭环不受影响。
