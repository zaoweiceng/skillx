# 代码审查报告（034）

审查日期：2026-08-13

代码基线：`3bad7d3` 加当前工作区中 [`REPLAY-033.md`](../REPLAY/REPLAY-033.md) 对应修改。
本轮未修改业务代码；Token 上限和消耗不纳入缺陷判定。

## 1. 结论

**主闭环代码通过；发现 1 个 P1 前端缺陷。**

033 的两个 P0 和一个 P1 已被正确修复：LLMExecutor 统一注入 resolved inputs，选择服务在参数提取前校验披露终态并允许一次
受限补全，边界异常会持久化为 assistant error，真实产品门禁也已校验 q4/q3 的业务语义。真实栈完整执行证明这些修复不是仅靠
单测成立。

唯一 finding 是亮色主题执行轨迹面板不可读。它不影响运行结果，但直接违反本轮现有 UI 验收条件，所以不能给出完全通过。

## 2. P1 Finding

### P1-1：执行轨迹抽屉写死深色背景，却继承亮色主题的深色文字

位置：[`index.css:1498`](../frontend/src/index.css#L1498)、
[`themeContrast.test.ts:1`](../frontend/src/lib/themeContrast.test.ts#L1)。

`.trace-drawer` 无条件使用 `background: #0e191f`，但没有同时锁定适合深底的前景色，也没有亮色主题覆盖。切换亮色主题后，
抽屉内标题继承 `--text: #172126`，空态文字继承 `--muted: #5d6b70`。真实浏览器计算结果分别为：

- 标题 `#172126` / 背景 `#0e191f`：约 **1.09:1**；
- 空态文字 `#5d6b70` / 背景 `#0e191f`：约 **3.23:1**。

因此“执行轨迹”等文本在亮色模式下几乎不可见。根因不是主题 Token 本身不合格，而是组件把固定色与全局主题变量混用；现有
`themeContrast.test.ts` 只验证 Token 对，没有验证组件最终组合，所以自动门禁出现假阴性。

修复应只选择一个一致策略：让抽屉背景使用主题面板 Token，或保留深色抽屉并为整个抽屉显式定义可读的深色面板前景/弱化文字
Token。随后在真实浏览器中校验最终 computed style 和可读性；不要通过单独给标题打补丁遗漏时间线、空态与错误文字。

## 3. REPLAY-033 代码审查结果

### 3.1 resolved inputs 契约：通过

[`executors.py:411`](../backend/app/runtime/executors.py#L411) 将输入规范化为排序 JSON，并放入明确的
`[RESOLVED_INPUT_JSON]` 边界；有 `{input}` 时只替换一次，无占位符时自动追加，fake/real 走同一入口。真实 q4、q1、q3 的
正确结果证明模型不再只看到 Schema 或通用任务说明。

[`distillation.py:472`](../backend/app/application/distillation.py#L472) 生成的 `final-response` 保留去实例化任务目标、字段语义和证据约束，
没有固化具体 q 值，也没有加入天气或其它领域模板。

### 3.2 渐进披露终态约束：通过

[`routes.py:4731`](../backend/app/api/routes.py#L4731) 按 action 校验 L1、Fragment L2 与 compiled plan；
[`routes.py:4976`](../backend/app/api/routes.py#L4976) 在披露不足时计算剩余预算，只开放 catalog 能力继续补全，并禁止更换既有
action/ref。补全后仍不满足时返回 `WORKFLOW_SELECTION_DISCLOSURE_INCOMPLETE`，不会带病执行。

真实 q3 先选择 Fragment、再补 L2 后成功运行；时间线完整保留两次选择调用及工具证据。

### 3.3 对话错误终态：通过

[`routes.py:4188`](../backend/app/api/routes.py#L4188) 捕获 HTTP、Catalog、PlanCompile 和 WorkflowValidation 边界异常，统一构造
可持久化 assistant error。定向回归覆盖披露补全失败，真实验收没有裸 500，数据库也没有 processing Turn。

### 3.4 真实产品门禁：通过

[`test_traex_product_real.py:216`](../backend/real_provider_tests/test_traex_product_real.py#L216) 不再只检查 succeeded/非空输入，
而是明确断言 q4 包含“第四季度/15%”、q3 包含“第三季度/营收持平”，并检查 L0/L1、必要时 L2、服务端校验参数来源和不可变 ref。
本轮 `make traex-real-product-check` 4 项全部实际通过。

## 4. 验证记录

| 检查 | 结果 |
|---|---|
| REPLAY-033 定向测试 | 62 passed |
| `make demo-check` | 358 backend、56 frontend、37 E2E 全通过 |
| `make browser-ui-check` | 2 passed |
| `make traex-real-check` | 1 passed，未 skip |
| `make traex-real-product-check` | 4 passed，q4/q3 业务语义与披露等级均通过 |
| 手动真实 q2→固化→发布→q4/q1→q3 | 通过；结果分别为 8%、15%、12%、营收持平 |
| 状态/日志/console | 无 processing/running、500、Traceback、ERROR 或 console error/warning |
| 亮暗主题人工视觉检查 | 暗色通过；亮色轨迹抽屉深底深字，失败 |

修复范围见 [`NEXT_STEPS-034.md`](../STEP/NEXT_STEPS-034.md)。

