# 监控与 Session 记录

入口：主导航「监控」，路径 `/monitoring`。每张卡片独立支持最近 1、3、7 天和自定义日期；自定义截止日期包含当天，浏览器将本地日期转换为带时区的时间戳。刷新更新相对时间窗口，自定义窗口保持不变。

统计口径：

- 任务按创建时间选择。成功率 = succeeded / (succeeded + failed + blocked)，失败率使用同一分母；取消、运行中、等待人工任务不进入分母。无样本显示 `—`。
- 任务耗时排行榜展示已结束且有会话起止时间的任务端到端耗时，包含人工等待，取消除外，按耗时从长到短排列。
- Token 汇总任务及学习的已知用量；独立模型调用按记录时间计入，排除已归入任务的调用。部分用量不完整会明确标注，不估算未上报用量。输入 Token 已包含缓存，输出已包含推理 Token，不再重复相加。
- SkillX 包含 Invocation 和 Workflow 中直接执行的 SkillX 节点。同一节点重试不重复计数。Workflow 包含 Invocation 和未关联 Invocation 的独立运行。
- 失败原因按错误代码分组；没有代码时使用错误消息或阻塞说明。
- 对话缓存命中率为已报告缓存读取量 / 已报告输入量，按 Token 加权；有有效数据时显示“缓存命中XX.XX%”；部分调用未报告时在悬停说明中标注，新对话或没有有效数据时隐藏。
- Pi 从 OpenAI Chat Completions 原始 SSE usage 的 `prompt_tokens_details.cached_tokens` 读取缓存量，缺失保留 null；原始 usage 另存于响应 trace 的 `rawUsage`。旧记录中 SDK 补出的 0 无法追溯成真实值。
- 任务结束且用量、模型耗时完整时，在 Token 后显示平均输出速度（tokens/s）：各次输出 Token 之和 / 各次模型请求耗时之和。包含响应等待，不含工具执行耗时；不是输入输出总 Token / 任务耗时。缺少测量的历史记录不估算速度。

每次 AgentHost 执行会在 `SKILLX_ARTIFACT_ROOT/sessions/YYYY/MM/DD/rollout-<uuid>.jsonl` 保存一个独立 Session。默认位于后端的 `artifacts/sessions`。采用 TraeX 的 timestamp/type/payload 行结构，记录 session_meta、turn_context、model_context、event_msg、native_event（TraeX）及 session_end。Pi 的模型请求与响应通过 model.trace 分片记录。工具参数与结果保留完整结构，凭证字段脱敏，文件权限为 0600。

面板支持按时间筛选 Session、分页查看原始记录和刷新运行中的记录。历史数据库记录不会被伪造为完整原始记录；启用后新执行才产生 JSONL。每次执行使用独立 UUID，重试、并行运行分别记录，runId 用于关联任务。进程被强制杀死时可能没有 session_end，已追加的记录仍保留。记录写入失败会写服务日志，不改变业务执行结果。

## 模型请求与缓存诊断

`turn_context.outputSchema` 是 Host 的完整校验契约，不一定原样发给模型。Pi 在 `json_schema` 模式且没有 Host 工具时，将任务决策契约投影为四个提交工具；实际请求不带 `response_format`。应拼接同一 requestId/phase 的 `model.trace` 分片，检查最终 `messages`、`tools` 和响应 `rawUsage`，不能仅比较上层 prompt。

Pi 的进程仍按执行隔离，但模型上下文由 Conversation 级数据库记录恢复。`conversation_context_exchanges` 在成功决策与任务状态提交的同一事务内追加实际发出的 user 输入（含图片引用）和规范化的 assistant 决策 JSON；不保存为推理正文或重放提交工具的副作用。后续请求使用原生 `user/assistant` 消息追加当前目标与新观察，不再把旧消息改写进新的快照。目录未变化时不重复发送。无 Conversation 的独立调用和其他运行时保持原有方式。

provider 的固定 system/developer 消息不含 SDK 自动追加的 Task 工作目录，也不含动态输出 schema。Task 目录仅控制实际运行位置，所需业务路径位于当前任务消息；能力目录文件使用 Conversation 目录且路径放在目录内容之后。长请求通过临时私有文件传给 Pi，避免追加历史触发命令行长度上限；文件在执行结束或取消后清理。

工具定义中的证据 UUID 枚举、数量和条件必填项会随执行变化，因此 provider 工具只描述固定结构；有效 UUID、完成证据和继续执行的 evidenceGap 约束由 Host 的原始动态 schema 校验。历史成功调用的 ID 可以跨轮引用，但新任务无关问题不被强制引用旧证据；本任务有成功执行时仍要求有效证据。完成条件的运行时补齐也不进入工具定义。

2026-10-13 对一次七轮任务的 trace 排查：输入合计 149,833 tokens，缓存读取 1,536（1.03%）；developer 消息保持一致，工具定义每轮变化。同一模型服务的合成请求对照中，相同输入重复请求命中 3,200 / 3,226 tokens，仅修改工具参数 schema 中一个证据 ID 后降至 256，再重复新 schema 恢复至 3,200。修复后使用实际四个提交工具，分别从零、一、两个成功调用生成契约，仅改变合成输入末尾的轮次；热身后的两轮均命中 5,632 / 5,735（98.20%）。这些实验隔离了动态工具 schema 的影响，不依赖沿用会话；数值是该服务当次合成输入实测，不是原业务任务修复后的命中率或缓存保证。

跨 Task 的第二处前缀变化来自 SDK 注入的 `Current working directory: …/tasks/<uuid>`。修复并接入追加历史后，在隔离数据库中使用真实 Pi/模型链路验证：首轮执行一次本地 Python 计算得到 count=100、total=5050，之后两个不同 Task 连续追问并引用同一执行 ID，无重复执行。三次模型请求输入分别为 6,248 / 6,714 / 7,105 tokens，缓存读取为 0 / 6,144 / 6,656；两次追问命中率分别为 91.51% / 93.68%。实际请求的消息前缀逐条相等，工具定义保持一致。此为真实模型的合成业务验证，不代表所有任务都能达到该比例。

同一 Conversation 按 Task 接受顺序执行，其他 Conversation 可并行；失去 lease、无效决策或事务回滚不会追加历史。旧版对话首次使用时，从已有 UI 消息和执行记录建立初始上下文，不伪造缺失的原始模型历史。`SKILLX_CONVERSATION_CONTEXT_MAX_CHARACTERS`（默认 160000，并受模型 contextWindow 的保守字符预算限制）控制显式 checkpoint。历史图片超过 8 张或 20 MiB、丢失或新模型不支持图片时也触发 checkpoint。压缩后消息明确标记档案路径、最近决策和有限证据预览，证据 ID 与原始记录保留；下一轮在新 checkpoint 后继续追加，不滑动改写旧消息。升级需要迁移 `0012_conversation_context` 并重启后端 Worker。

接口：`GET /api/monitoring/metrics/{metric}`、`GET /api/monitoring/sessions`、`GET /api/monitoring/sessions/{uuid}`。所有范围左闭右开，必须携带时区，记录分页不接受文件路径。

## 趋势折线图

五个数值指标同时展示区间汇总和分时折线。任务耗时改为折线图下方的全宽排行榜，按耗时降序展示任务、状态、耗时和创建时间，支持独立时间筛选、分页及跳转到关联对话。Token 增加最近 1、3、12 小时选项，数字及纵轴自动换算 K（千）、M（百万）、B（十亿），悬停仍可查看精确 Token 数。

分桶粒度由选定范围决定：不超过 1 小时按 5 分钟、不超过 3 小时按 15 分钟、不超过 12 小时按 30 分钟、不超过 3 天按小时、7 天按天。自定义长区间自动合并天数，最多约 91 个点。时间桶按浏览器时区偏移对齐，起止的不足一桶部分仅统计范围内数据。接口追加 `series`（start/end/value/samples）和 `intervalSeconds`；`timezone_offset` 为东向 UTC 偏移分钟数。

计数/用量的空桶为 0；没有已结束任务的比例/耗时桶为 null，折线跨过空桶连接前后有效点，悬停仍显示“无样本”。区间总体比例和平均耗时按样本加权，不平均各桶百分比。任务指标继续按任务创建时间归桶，Token 曲线按任务创建时间归属任务及学习总量（并非模型实时吞吐）；独立模型调用按用量记录时间。SkillX 节点跨小时重试也只归入首次执行的时间桶。
