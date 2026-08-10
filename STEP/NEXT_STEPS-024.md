# NEXT_STEPS-024：恢复可重复发布，并补齐真实语义与历史审计

依据：[`REPLAY-023.md`](../REPLAY/REPLAY-023.md)、
[`design-compliance-review-024.md`](../review/design-compliance-review-024.md)、
[`code-review-024.md`](../review/code-review-024.md) 和 [`docs/`](../docs/) 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。首要目标仍是“完整流程真实跑通”，
不是继续增加框架层或为单个天气例子写规则。

## 1. 024 完成定义

只有同时满足以下三个事实，下一版才能声明通过：

1. `make demo-check` 从头到尾可重复通过，文档报告中的命令和数字可在对应提交复现；
2. 接入真实语义 Agent Runtime 后，一条普通消息完成工作流判断、参数绑定、执行和双格式结果；
3. 任意历史 Run 刷新后仍能从数据库恢复节点、事件、Runtime/Usage、结构化结果和阅读结果。

fixture 继续只用于协议测试。禁止增加城市、天气、URL 等业务正则来伪造通配语义能力。

## 2. P0：修复版本文档的单一事实链

- [ ] 确认 `REPLAY/` 与 `STEP/` 为唯一规范目录，不在根目录再维护重复副本。
- [ ] 修改 `tools/check_doc_chain.py`，扫描 `STEP/NEXT_STEPS-*.md`、
  `REPLAY/REPLAY-*.md` 和 `review/*-review-*.md`。
- [ ] 最新版本链应验证 `REPLAY/REPLAY-(N-1)`、`review/*-review-N`、`STEP/NEXT_STEPS-N`，
  并明确缺号策略；不要把目录位置硬编码成旧布局。
- [ ] 重写迁移后全部 Markdown 相对链接，使链接从源文件所在目录解析后真实存在。
- [ ] 增加“扫描到的版本文件数量大于零”和已知断链 fixture，避免空扫描被误判通过。
- [ ] 修正 `REPLAY-023` 中无法复现的“86 份通过”表述，记录修复后真实命令、提交和输出摘要。
- [ ] 先单独运行 `python3 tools/check_doc_chain.py`，再运行完整 `make demo-check` 连续两次。

## 3. P0：接入真实语义 Agent Runtime 并完成最低集

- [ ] 提供一条与 Agent Host 双向 NDJSON/ToolResult 协议兼容的 `traex_managed` adapter，或配置可用的
  `openai_sidecar`；Host 不额外要求 Direct LLM endpoint。
- [ ] readiness 必须以真实握手/能力探测为依据，不因变量存在就宣称语义可用。
- [ ] 补齐内置工作流目录元数据：`summary`、`useWhen`、`doNotUseWhen`、输入输出摘要和最小示例，
  保持 L0/L1/L2 渐进暴露。
- [ ] 用真实 Agent 输出 `WorkflowSelection`；服务端继续验证 action、Schema、quote、messageId、
  provenance 和 `serverVerified`，不能直接信任模型参数。
- [ ] “上海天气如何”第一 Turn 必须直接 `run_full`、绑定 `city=上海`，不追问已在原文中的参数。
- [ ] 至少再覆盖多字段、数字、布尔、枚举、数组、URL、历史引用和歧义拒绝，全部使用普通自然语言，
  不含 `[[SELECTION:*]]` 测试指令。
- [ ] 记录 provider/runtime/model、Turn/Run、digest、耗时、Token、Tool 数和 fallback 原因，不记录 Secret。
- [ ] 工作流关闭场景必须全 Turn 不搜索、不披露、不调用任何 `workflow.*`。

## 4. P1：把 Browser readiness 移出主调度临界路径

- [ ] Worker 启动时创建独立 browser-health 后台 task；周期预检不得在领取 workflow/agent/recovery/
  presentation 的串行主循环内 `await`。
- [ ] 后台 task 使用独立 DB session、单飞锁、10 秒以内超时和异常隔离；失败只更新 heartbeat。
- [ ] Worker shutdown 显式取消并等待预检 task，避免孤儿进程和未提交 session。
- [ ] 增加回归：模拟预检卡满超时，同时普通工作流仍在既定轮询延迟内完成。
- [ ] Worker readiness 与 Browser readiness 分开表达；Browser 失败不能把非 Browser 执行能力标成不可用。
- [ ] `/health/ready` 删除本机绝对 `chromiumPath`；公开 basename/version/status，完整路径只进诊断证据。

## 5. P1：实现持久事件回放与 SSE 无缝合并

- [ ] 在前端 client 增加/复用 `GET /runs/{id}/events?after=<seq>`。
- [ ] RunDetail 挂载时从 `after=0` 分页拉取历史事件，按 `seq` 排序并写入 run event store。
- [ ] 运行中先加载历史，再从最后 `seq` 建 SSE；按 `(runId, seq)` 去重，不能丢失加载窗口内事件。
- [ ] 终态只加载持久事件，不创建 EventSource；空态只能在数据库确实无事件时出现。
- [ ] 真实浏览器回归：执行成功 → 打开 Run → 刷新 → 事件数量和关键 seq/type 不变。
- [ ] 同样覆盖失败、等待审批、恢复接力和 Presentation 事件。

## 6. P1：让模型调用审计表达 dispatch 事实

- [ ] 在 Direct LLM 和 Agent adapter 真正发出请求的边界记录 `requestDispatchedAt` 或等价布尔事实。
- [ ] ModelUsage 区分 `attempted`、`dispatched`、`completed`；预调用配置失败可留审计行，但不得算模型已调用。
- [ ] `modelCalled`、Runtime 调用次数和可能费用只聚合 `dispatched` 记录。
- [ ] 增加配置漂移、握手失败、DNS/连接失败、401、429、timeout、成功和 zero LLM 回归。
- [ ] 保持 401/429/timeout 后不跨 Runtime fallback，避免双请求和双费用。

## 7. UI 与真实场景回归

- [ ] 亮暗主题逐页检查 Chat、Settings、Workflow Editor、Component、RunDetail；刷新后主题保持。
- [ ] 工作流开关默认开启；不可用提示、一键关闭、计时和 Token 文案必须与服务端事实一致。
- [ ] 组件手动执行后，详情页历史包含输入、输出、耗时、状态、节点、事件、Usage 和 Presentation。
- [ ] 手动工作流同时验证 JSON 复制和自然语言阅读结果；不需要直接呈现的子工作流允许无 Presentation。
- [ ] 760、903、1024、1280、1440px 无遮挡、横向溢出和不可点击控件；console 0 error/warning。

## 8. 交付门禁

1. `python3 tools/check_doc_chain.py` 通过，且故意制造断链时会失败；
2. `make demo-check` 连续两次从 Ruff、迁移、前后端测试/构建一直运行到 37 条进程 E2E 并通过；
3. `make browser-ui-check` 在正常主机权限连续两次通过；受限环境稳定报告 `permission_denied`；
4. 真实语义最低集全部通过，“上海天气如何”不得追问 `city`；
5. Browser 预检超时时，非 Browser 工作流延迟不受其 10 秒超时影响；
6. 成功/失败 Run 刷新后持久事件完整，终态无 SSE 重连；
7. `modelCalled` 与真实 dispatch 一致，绝对本机路径不出现在公开 readiness；
8. 新增 `REPLAY-024.md`，只记录可在对应提交复现的事实和命令结果。

在总门禁、真实语义和历史事件回放三项同时完成前，不得声明整体通过。
