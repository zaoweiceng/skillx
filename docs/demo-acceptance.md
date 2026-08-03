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

该命令必须完成空库迁移、后端检查与测试、前端检查与构建，以及六条 E2E。
