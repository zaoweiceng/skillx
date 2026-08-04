# Local Demo 验收规范

本文是 `NEXT_STEPS.md` M8 的可执行验收基线。每个场景必须通过真实 API、独立
Worker 和持久化数据库完成；仅存在类型、适配器或静态页面不计为通过。

| 场景 | 输入与路径 | 必须事件 | 通过条件 |
|---|---|---|---|
| E2E-1 确定性组件 | Form → Transform/SkillX Direct → Output | `run.created`、`node.*`、`run.completed` | 无模型配置仍成功，Token 为 0 |
| E2E-2 MCP 组件 | Form → 锁定 MCP ToolSnapshot → Output | `mcp.call.*` | Tool Schema/digest 锁定，模型调用为 0 |
| E2E-3 Hybrid 组件 | 固定 DAG → 单个 LLM → Output | `llm.call.completed` | 仅显式节点产生 Token，输出通过 Schema |
| E2E-4 对话调用组件 | 自然语言 → 候选/补参 → Workflow | 组件候选、`run.created` | Workflow 启动后 Agent 不接管锁定节点 |
| E2E-5 轨迹固化 | Agent Run → Distillation → Draft | `agent.*`、固化状态事件 | 草稿可编辑但不自动发布，新输入可复用 |
| E2E-6 浏览器提交 | 检查 → 填写 → 审批 → 提交 → 验证 | `browser.*`、`approval.*` | 授权一次性消费；页面、数据或身份变化即失效 |
| E2E-6b 页面漂移 | 审批后修改页面 → 重放 | `browser.*`、`approval.*` | 旧授权失效且不提交 |
| E2E-7 检查点接力 | Direct 失败 → Recovery → 剩余 DAG | `run.recovery_requested` | 原失败 attempt 不改写，非幂等副作用不重放 |
| E2E-7b Recovery crash | Worker 领取 checkpoint 后退出 → 新 Worker | `recovery.*`、lease heartbeat | 过期租约重领并完成；旧 Recovery NodeRun 留证 |
| E2E-7c Recovery question | Recovery → request_user → answer | `recovery.user_answered` | 同一 checkpoint 保存回答并续跑 |
| E2E-7d Recovery replan | Recovery → typed plan patch → new attempt | `recovery.*`、NodeRun | patch 经 Compiler 后执行；原失败 attempts 不改写 |
| E2E-8 动态网页读取 | Playwright read → summary → 页面变化 | `browser.read.*` | 第二次运行消费新正文 |
| E2E-8b 真实网页固化 | Browser Agent → Proposal → publish → 新正文 → Agent | `agent.*`、`browser.action.*` | provenance/Proposal 真实；summary 走非 fake AgentHost |
| E2E-9 Patch replay | 真实失败 → baseline → patched | `replay.*` | baseline 同 fingerprint；外部写为 0；最终 Output 成功 |
| E2E-10 渐进披露与组合 | search → describe → 两个 refs → PlanSnapshot | L0 → L1、`run.*` | 默认版本更新不改变已冻结 Run |
| E2E-11 Fragment safety | describe_fragment → run/非法裁剪 | L2、Compiler diagnostics | 只读闭包可运行；绕过审批/副作用的裁剪被拒 |
| E2E-12 固化建议 | 同 signature 多输入成功 → 接受建议 | suggestion、draft | 仅创建 draft，未经人工不发布 |
| Scale-1 Catalog 1000 | 1000 个已发布 Manifest → search | Registry 单元/集成门禁 | top-k≤8、总字节≤24KB、L0 不含 Schema/DAG |

## 通用失败判定

- 重复执行同一副作用，或迟到 Worker 覆盖新 attempt。
- Run 已显示 `cancelled`，Executor 仍继续并提交成功。
- WorkflowVersion 未锁定实际 Capability/MCP/接口版本。
- Secret 出现在数据库、事件、日志、API 或 Artifact。
- Browser L2/L3/L4 未按策略暂停或终止。
- 测试绕过 Alembic、API 或 Worker 主路径。

## 质量门禁

```bash
make demo-check
```

该命令必须完成空库迁移、后端检查与测试、前端检查与构建，以及本文件全部 E2E。
