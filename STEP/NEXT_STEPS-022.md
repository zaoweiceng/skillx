# NEXT_STEPS-022：拆分 Agent Runtime 与 LLM Runtime

依据：[`REPLAY-021.md`](../REPLAY/REPLAY-021.md)、
[`review/design-compliance-review-022.md`](../review/design-compliance-review-022.md)、
[`review/code-review-022.md`](../review/code-review-022.md) 和 `docs/` 下设计/规范文档。

范围：单用户 Local Demo，不增加鉴权、RBAC、多租户或分布式部署。首要目标是保持现有完整流程可运行，同时消除
Agent 与模型端点的错误耦合，并实现：**LLM Runtime 未配置时，由 Agent Runtime 完成受约束的结构化生成。**

## 1. 022 完成定义

```text
Agent 任务
  → Agent Runtime（TraeX managed / OpenAI sidecar / fixture）

LLM 节点或 LLM Presentation
  → StructuredGenerationResolver
      ├─ LLM Runtime ready → Direct LLM
      └─ LLM Runtime unconfigured + Agent ready → Agent structured fallback
             allowed_tools=[] / strict schema / bounded one-step
```

同一 Run 必须记录实际 Runtime、选择原因、配置 digest、模型/Agent 身份、Token 完整性和费用归属。
仅“LLM 未配置或启动前明确不可用”允许默认 fallback；401、429、timeout、malformed output 等执行中失败不得静默换 Runtime。

## 2. P0：Runtime 边界

### P0-1：拆分配置与 readiness

- [ ] 将现有全局 `SKILLX_MODEL_*` 拆为：
  - `SKILLX_LLM_RUNTIME_TYPE=openai_compatible|unavailable`；
  - `SKILLX_LLM_ENDPOINT`、`SKILLX_LLM_MODEL`、`SKILLX_LLM_API_KEY_ENV`；
  - `SKILLX_AGENT_RUNTIME_TYPE=traex_managed|openai_sidecar|fixture|unavailable`；
  - `SKILLX_AGENT_COMMAND`、allowlist；只有 `openai_sidecar` 才要求自己的 Agent model 配置。
- [ ] 旧 `SKILLX_MODEL_*` 提供一个版本的显式兼容映射和 deprecation 日志，禁止静默猜测它属于 Agent 还是 LLM。
- [ ] `/health/ready` 分别返回 `agentRuntime` 与 `llmRuntime`，二者有独立 configured/protocol/model/probe/digest。
- [ ] `traex_managed` readiness 只通过 Agent 协议探测，不要求 Host 配置 LLM endpoint。
- [ ] Settings 增加两张独立 Runtime 卡片、各自 probe/费用说明和当前 fallback policy。

### P0-2：建立统一结构化生成接口

- [ ] 新建 `StructuredGenerationRequest/Result/Runtime`，字段至少包含 prompt、outputSchema、maxOutputTokens、
  runtime kind、usage、model/runtime identity 和 digest。
- [ ] `OpenAICompatibleAdapter` 实现 Direct LLM Runtime，不再直接读取 Agent 配置。
- [ ] 新建 `AgentStructuredGenerationAdapter`，通过现有 AgentHost 执行：
  - `requested_capabilities=()`；
  - `allowed_tools=[]`；
  - `max_steps=1`；
  - 严格校验 Output Schema；
  - 沿用 Host Token/time/payload/cancel 边界；
  - 不披露 workflow/browser/read 等工具。
- [ ] LLM 节点和 LLM Presentation 都只依赖 `StructuredGenerationResolver`，禁止各自实例化 Provider Adapter。

### P0-3：冻结并审计 fallback 决策

- [ ] Workflow 编译/发布后形成明确 `modelRuntimePolicy`，默认顺序为 `llm_runtime → agent_runtime`；该策略进入版本 digest。
- [ ] Run 开始时解析一次并冻结结果；依赖快照记录 `runtimeUsed`、`runtimeDigest`、`fallbackReason` 和 policy digest。
- [ ] `ModelUsage` 记录 `direct_llm|agent_fallback`、provider/model、input/output/total tokens 和 complete 状态。
- [ ] UI 在节点、RunDetail、Chat Turn 和 Presentation 显示“LLM Runtime”或“Agent fallback”，不把 fallback 冒充 Direct LLM。
- [ ] 仅配置缺失/启动前 readiness unavailable 触发默认 fallback；执行中错误继续走该 Runtime 的 retry/error policy。

## 3. P0：真实验收矩阵

使用真实进程和至少一个真实 Agent Runtime；不得只用 fake LLM 或函数级 mock。

| LLM Runtime | Agent Runtime | 预期 |
|---|---|---|
| ready | ready | LLM 节点与 LLM Presentation 使用 Direct LLM，Agent 不参与 |
| unconfigured | ready | 两者使用 Agent structured fallback，Tool 数为 0，Schema 与 Usage 完整 |
| ready | unavailable | Direct LLM 正常完成 |
| unconfigured | unavailable | 发布可提示风险，运行稳定失败 `MODEL_RUNTIME_UNAVAILABLE` |
| ready 但返回 401/429/timeout | ready | 不静默 fallback；返回对应稳定错误并保留单一费用事实 |

- [ ] 复现并关闭 022 Run `4b05ed20-...`：同类非 fake 分类节点在 LLM 未配置、Agent 已配置时成功。
- [ ] 对 LLM Presentation 执行同一矩阵；结构化 Run 成功后阅读结果由 Agent fallback 生成。
- [ ] Agent fallback 输出不包含任何 `tool.requested`，Agent Host 日志证明有效 Tool 集为空。
- [ ] 七条无 fixture 指令的真实语义最低集全部通过，记录 provider/runtime/model、Run/Turn id、digest 和 Usage，不记录 key。

## 4. P1：技术证据契约

- [ ] 为持久 ErrorEnvelope 增加版本或类型标识，使 `persisted_error()`、`public_error()`、`technical_error()` 幂等。
- [ ] 禁止把已经截断的 `{summary, truncated}` 再 JSON 编码为新的 summary。
- [ ] `/runs/{id}/technical-error` 对整个响应设置明确字节上限，而非 Run/每个 Node 分别 4000 字符。
- [ ] Run 与 Node 的重复日志按 digest 去重；默认返回摘要和 evidence refs，完整日志保存为受控 Artifact。
- [ ] RunDetail 将 code、阶段、建议动作、摘要和 Artifact 分区展示，不直接渲染多层 JSON 字符串。
- [ ] 回归 022 Browser 失败：响应显著小于 8898 bytes、无 `summary` 套 `summary`、同一启动日志只出现一次。

## 5. P1：终态 SSE 生命周期

- [ ] `useRunStream` 接收 Run 状态；已终态时不创建 EventSource。
- [ ] 收到 `run.completed|run.failed|run.cancelled` 后立即移除监听并 `close()`。
- [ ] 如果连接异常但 Run 已终态，禁止 EventSource 自动重连。
- [ ] 增加真实浏览器断言：终态后等待 5 秒，`/events/stream` 请求数不增长。
- [ ] Chat 的 Run/Agent stream 同样审查终态关闭语义，避免只修 RunDetail。

## 6. 视觉与交互回归

- [ ] Settings 在亮暗主题同时展示 Agent Runtime、LLM Runtime、fallback policy 和独立状态，普通用户能理解当前执行路径。
- [ ] 760、903、1024、1280、1440px 检查 Chat 历史/trace 抽屉、Runtime 卡片、Editor、RunDetail 技术证据。
- [ ] 工作流关闭仍保证下一 Turn 无 `workflow.*`；Runtime fallback 不改变这个会话策略。
- [ ] 组件历史、正确当地时间、JSON/文本复制、Presentation、错误安全投影保持无回归。
- [ ] console 0 error / 0 warning；所有按钮键盘可操作，`aria-pressed/expanded` 与视觉状态一致。

## 7. 门禁与交付

1. `make demo-check` 连续两次通过；
2. `make browser-ui-check` 在目标本地运行环境连续两次通过；
3. Browser Runtime 启动预检能区分缺二进制与 OS 权限不足，Settings 给出明确状态；
4. 新增真实进程 Runtime 矩阵测试，禁止用 `modelProfile=fake` 证明 fallback；
5. 新增错误 envelope 幂等/整体大小测试和终态 SSE 不重连测试；
6. 新增 `REPLAY-022.md`，记录配置矩阵、实际 Runtime 选择、Run/Turn/Usage、浏览器和视觉证据。

完成以上项目后，再进行 023 审查；在真实 Agent Runtime、LLM fallback 和真实语义最低集都通过前，不得声明整体通过。
