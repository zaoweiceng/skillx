# 设计与规范达成度审查报告（017）

审查日期：2026-08-06

依据：[`REPLAY-016.md`](../REPLAY/REPLAY-016.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`0a2237f688f9ee91b6f924c0b169e448c3d48e7a`。本轮未修改业务代码，只新增 017 三份审查文档。
范围为单用户 Local Demo，不审查鉴权、RBAC、多租户和分布式部署。

## 1. 结论

**不能直接判定通过。**

016 已经打通正常路径：自然输入可执行 Browser、lookup、file/read 三类受控只读探索；成功结果可见并能从
页面固化、检查、人工发布；第二个新 Conversation 能经 L0→L1 发现首次 lineage，执行发布后的 Workflow，
把变化后的正文直接显示在对话中；刷新和 RunDetail 也正常。

但通配型闭环的最后一段仍缺失：真实 503 后，新的 Worker 能从冻结计划只执行剩余步骤，continuation 也确实
成功产生最终输出；该 continuation 没有持久关联到原 Conversation，Chat 只能继续显示源 Run 的失败。

```text
首次探索 → 固化 → 发布 → 第二次复用 → 结果返回        通过
真实失败 → checkpoint → 新 Worker → continuation 成功  通过
continuation 最终结果 → 原对话                         不通过
```

因此当前准确状态是：**正常主流程可用；失败接力的后端执行可用；“失败接力—最终答复用户”尚未闭合。**

## 2. REPLAY-016 声明核验

| 016 声明 | 017 结论 | 证据或边界 |
|---|---|---|
| 自然只读探索策略 | 通过 | 真实前端分别执行 Browser、lookup、file/read；无需调用 API 补 capability |
| Agent 消息刷新恢复 | 通过 | 自动 Agent 成功/失败状态、事件、错误、输出可从持久 Run 恢复 |
| UI 固化、检查、发布 | 通过 | 从首次 Browser Agent 卡片创建草稿，静态检查通过并人工发布 v1 |
| 新 Conversation 渐进复用 | 通过 | L0→L1 命中首次探索 lineage，读取变化后的新正文 |
| Workflow 结果返回 Conversation | 通过 | 结果直接显示，刷新后仍存在；RunDetail 显示相同 output |
| RunDetail 稳定 | 通过 | 真页面显示节点、SSE、最终输出、依赖和 Artifact；console 无 error |
| replan budget | 通过 | Host 强制 max/used/remaining，既有专项测试通过 |
| attempt 参数证据 | 通过 | 真实 checkpoint 中 failed attempt 1 与 succeeded attempt 2 均有一致的 argumentsDigest |
| “失败接力—结果返回”用户闭环 | **不通过** | continuation succeeded，但原 Conversation 没有 continuation message/output |
| 整体主流程完整跑通 | **不成立** | REPLAY-016 将后台 handoff 成功等同于用户获得恢复结果，超出实际证据 |

## 3. 真实环境测试

### 3.1 环境

本轮创建全新临时 SQLite 数据库，只通过 Alembic `0001→0017` 建库并 seed，实际启动：

- FastAPI：`127.0.0.1:8017`；
- 两代独立 Worker：源任务 Worker 与恢复 Worker；
- Vite：`127.0.0.1:5117`；
- 可动态改变正文和健康状态的本地 HTTP 站点：`127.0.0.1:8917`；
- TraeX-compatible 确定性 Demo 子进程；
- 真实 Chromium Browser Tool 与实际前端页面。

测试通过浏览器输入和点击完成，没有用新单测或 HTTP 冒烟替代 UI 主流程。确定性 TraeX stub 只代替外部模型，
不代替 API、Worker、数据库、HTTP、Chromium、Workflow Runtime 或页面交互。

### 3.2 正常主流程：通过

1. 在 `/chat` 输入 Browser 自然任务，自动 `continue_agent` 成功，页面显示“017首次探索正文：季度营收增长17%。”、
   Tool 轨迹和“固化为组件”。
2. 点击固化，打开 draft `b6165427-0c32-44c8-9fd5-86600a7d6025`，静态检查通过并人工发布 v1；
   组件入口 `5a998ec3-...` 创建。
3. 将站点正文改为“017第二次复用正文：利润增长27%。”，新开 Conversation 输入带新 query 的 URL。
4. 系统经 L0→L1 选择首次 lineage；Workflow Run `c4b53004-76d4-4af1-bb59-19998e5f0a38` succeeded。
5. 新结果直接显示在 Chat；刷新仍显示相同结果。
6. 点击“查看运行”，页面显示四个节点、23 个事件、最终 output、依赖 lineage 和 4 个 Artifact。
7. 浏览器 console 无 error。

同一套真实前后端还从 Chat 执行了：

- lookup：`q2` 返回“第二季度：营收增长 8%”；
- file/read：`source.txt` 返回“017自然入口文件正文”。

这证明当前固化框架不是只针对网页入口。

### 3.3 真实 503 跨 Worker 接力：后台通过，用户闭环失败

源任务使用结构化 `planned_browser_summary`：`preflight → browser → summarize`。

1. 站点切为 503，源 Worker 有限重试两次；Run `73ce7a59-b887-497d-b4c3-ef03926fe8f3` failed。
2. checkpoint `9dbb70f8-321f-476e-8ea5-380fc82572eb` 为 pending，保存：
   `preflight=succeeded`、`browser=failed`、`summarize=remaining`，以及完整 PlanSnapshot、attempt 和预算。
3. 停止源 Worker，恢复站点，启动新的恢复 Worker。
4. continuation `edc641e9-99bc-426c-9692-43ebcb9f2c8b` succeeded，输出
   `{"reply":"恢复后的正文"}`；站点 `transientCount` 仅从 2 增至 3，证明只恢复 Browser 一次，未重做 preflight。
5. Browser attempt 1 为 failed、attempt 2 为 succeeded，两次 argumentsDigest 相同且非空。
6. checkpoint 最终为 succeeded，源轨迹出现 `agent.handoff.started/succeeded`。

但是原 Conversation 仍只有源 Run 的一条 assistant message。刷新 Chat 后页面继续显示
`DEPENDENCY_UNAVAILABLE`，不显示 continuation 的“恢复后的正文”，也没有 continuation 的结果卡片或链接。

### 3.4 浏览器门禁为何仍为绿色

`backend/browser_e2e/test_ui_main_flow.py` 的失败段在发布网页 Workflow 后提交普通 transient URL，断言的是
Workflow 输入绑定失败 `INVALID_INPUT` 和 Workflow RunDetail。它没有使用 `continue_agent +
planned_browser_summary`，也没有停止 Worker、恢复站点、等待 continuation 或检查最终答复。因此
`make browser-ui-check` 通过不能证明 F-03。

## 4. 五阶段完成度

| 阶段 | 判定 | 说明 |
|---|---|---|
| 首次探索 | 完成 | Browser、lookup、file/read 自然入口均成功 |
| 固化与人工发布 | 完成 | UI 固化、静态检查、人工发布成功 |
| 渐进发现与复用 | 完成 | 新 Conversation 经 L0/L1 命中首次 lineage |
| 失败接力执行 | 完成 | 真 503、Worker 更换、remaining-only continuation 成功 |
| 失败后的结果反馈 | **未完成** | continuation 未关联回 Conversation，用户仍看到旧错误 |

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff / Pytest | all checks passed；258 passed in 28.43s |
| Alembic 空库迁移 | `0001→0017` 通过 |
| 前端 TypeScript / ESLint / build | 通过 |
| 前端 Vitest | 7 files / 32 tests passed |
| 进程级 E2E | 34 passed in 129.36s |
| `make browser-ui-check` | 1 passed in 9.86s |
| 手工真实 UI 正常闭环 | passed |
| 手工真实 lookup/file 自然入口 | passed |
| 真 503 + Worker 更换 + continuation | 后台 passed；Conversation result delivery failed |
| 浏览器 console | 0 error |
| `git diff --check 0a146a0..0a2237f` | 通过 |

修复任务见 [`NEXT_STEPS-017.md`](../STEP/NEXT_STEPS-017.md)。
