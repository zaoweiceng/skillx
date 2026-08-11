# 设计与规范达成度审查报告（018）

审查日期：2026-08-07

依据：[`REPLAY-017.md`](../REPLAY/REPLAY-017.md)、[`docs/workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`docs/workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`docs/schema.md`](../docs/schema.md)、
[`docs/skillx.md`](../docs/skillx.md)、[`docs/demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`9ec1246d17636dcdb5ced2cab4bcc9dbbe0c1c3a`；修复差异为 `0a2237f..9ec1246`。
本轮未修改业务代码，只新增 018 三份审查文档。范围为单用户 Local Demo，不审查鉴权、RBAC、多租户和
分布式部署。

## 1. 结论

**通过。**

017 遗留的最后一跳已经闭合。当前版本能在真实 API、Worker、Vite、动态 HTTP 站点和 Chromium 环境中连续完成：

```text
自然语言首次探索
  → 结果可见
  → UI 固化、静态检查、人工发布
  → 新 Conversation 经 L0/L1 复用
  → 新结果直接返回 Chat

真实 503
  → source Run 保留 failed
  → checkpoint pending
  → 更换 Worker 后 remaining-only continuation
  → 原 Chat 无手动刷新自动出现最终结果
  → 刷新后结果和 lineage 仍可恢复
```

在 Local Demo 范围内，通配型“首次探索—固化—复用—失败接力—修订/恢复—结果返回”主流程已经完整跑通。

## 2. REPLAY-017 声明核验

| 声明 | 018 结论 | 证据 |
|---|---|---|
| continuation 与 Conversation message 持久关联 | 通过 | 原 Conversation 新增唯一 assistant message，关联 continuation Run/checkpoint/generation |
| message 与 checkpoint CAS 原子提交 | 通过 | 创建和 `running→continuing` 位于同一事务；既有竞争专项测试通过 |
| source failed 不被覆盖 | 通过 | source `cd448096-...` 在 continuation 成功后仍为 failed |
| checkpoint 链式 API | 通过 | 返回 continuation status/output digest、chain status/error 和 lineage |
| Chat 自动跟踪 | 通过 | 页面由“等待接力”自动过渡到“恢复后的正文”，没有手动刷新 |
| 刷新恢复且不重复 | 通过 | reload 后正文仍存在且精确计数为 1 |
| remaining-only | 通过 | Browser 仅从 attempt 1 failed 增加 attempt 2 succeeded；preflight 不重做 |
| attempt 证据 | 通过 | 两次 Browser attempt 的 argumentsDigest 非空且一致 |
| 真实浏览器门禁补洞 | 通过 | 独立 503/Worker 更换/原 Chat 回显场景纳入 `browser-ui-check` |
| 正常探索—固化—复用链无回归 | 通过 | 本轮手工真实浏览器再次完整执行 |

## 3. 真实环境测试

### 3.1 环境

使用全新临时 SQLite 数据库，只通过 Alembic `0001→0017` 建库并 seed，实际启动：

- FastAPI：`127.0.0.1:8018`；
- 源 Worker 与恢复 Worker 两个独立进程；
- Vite：`127.0.0.1:5118`；
- 动态本地站点：`127.0.0.1:8918`；
- TraeX-compatible 确定性 Demo 子进程；
- 真实 Chromium Browser Tool 和前端页面。

测试从页面输入和点击完成。确定性 TraeX stub 只替代外部模型，不替代 API、Worker、数据库、HTTP、
Browser Tool、Workflow Runtime 或 UI 交互。

### 3.2 正常主流程

1. Chat 自然输入 Browser 任务，Agent 返回“018首次探索正文：季度营收增长18%。”。
2. 页面显示 Tool 轨迹、结果和固化按钮。
3. 点击固化，在 WorkflowEditor 执行静态检查并人工发布 v1；组件入口成功创建。
4. 将网页改为“018第二次复用正文：利润增长28%。”，新开 Conversation 请求同类任务。
5. 系统经 L0→L1 命中本次首次探索 lineage；Run `bba47cba-465f-4202-a4d7-83da425a6d7c` succeeded。
6. 新正文直接显示在 Chat，刷新仍存在。
7. RunDetail 显示四个节点、23 个事件、最终 output、依赖 lineage 和 4 个 Artifact。

### 3.3 真实 503 跨 Worker 接力

1. 源 Worker 以 `--disable-task-recovery` 运行，动态站点返回真实 503。
2. source Run `cd448096-287d-4f95-b51c-47d39d9b5748` 在两次有限访问后 failed。
3. checkpoint `60144cfc-ed51-4ca0-95dc-1d071bcb19b5` 为 pending；Chat 显示“等待接力”和 checkpoint 链接。
4. 停止源 Worker，将站点恢复 healthy，启动新的恢复 Worker。
5. 原 Chat 无手动刷新自动新增第 1 代接力消息并显示“恢复后的正文”。
6. continuation `aac8f211-cd2d-4768-ba1b-3e70f1ea0706` succeeded；source 仍为 failed。
7. checkpoint `chainStatus=succeeded`，含 continuation status、output digest 和完整 lineage。
8. 站点 `transientCount` 从 2 增至 3；Browser attempt 1 failed、attempt 2 succeeded，argumentsDigest 相同。
9. Conversation 中 continuation message 只有一条；刷新后正文精确计数仍为 1。
10. 浏览器 console 无 error。

## 4. 完成矩阵

| 阶段 | 判定 | 说明 |
|---|---|---|
| 首次探索 | 完成 | 自然入口执行真实 Browser，结果和轨迹可见 |
| 固化与人工发布 | 完成 | UI 固化、检查、发布成功 |
| 渐进发现与复用 | 完成 | 新 Conversation 经 L0/L1 命中首次 lineage |
| 正常结果返回 | 完成 | Workflow output 直接显示且刷新恢复 |
| 失败接力执行 | 完成 | 真 503、跨 Worker、remaining-only continuation |
| 接力结果返回 | 完成 | 原 Chat 自动回显，持久且不重复 |
| 失败证据与观察 | 完成 | source/checkpoint/continuation/generation/digest/lineage 可访问 |

## 5. 验证记录

| 检查 | 结果 |
|---|---|
| `make demo-check` | 退出码 0 |
| 后端 Ruff / Pytest | all checks passed；258 passed in 28.95s |
| Alembic 空库迁移 | `0001→0017` 通过 |
| 前端 TypeScript / ESLint / build | 通过 |
| 前端 Vitest | 7 files / 35 tests passed |
| 进程级 E2E | 34 passed in 130.43s |
| `make browser-ui-check` | 2 passed in 18.98s |
| 手工真实正常闭环 | passed |
| 手工真实 503 接力闭环 | passed |
| 浏览器 console | 0 error |

功能结论为通过。非阻塞的文档编号/Git 状态说明见
[`review/code-review-018.md`](code-review-018.md)。
