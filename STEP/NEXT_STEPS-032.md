# NEXT_STEPS-032：只修主闭环的三个阻断点

依据：[`REPLAY-031.md`](../REPLAY/REPLAY-031.md)、
[`design-compliance-review-032.md`](../review/design-compliance-review-032.md)、
[`code-review-032.md`](../review/code-review-032.md) 和 [`docs/`](../docs/) 下现有设计/规范。

本版不增加产品需求。范围仅是让现有“首次探索—固化—发布—新对话复用”在真实栈中跑通，并修复直接阻断该流程的
代码缺陷。仍为单用户 Local Demo；不增加鉴权、多租户、计费、Token 上限策略、领域模板或新的能力类型。

## 1. 032 完成定义

只有以下事实同时成立才能声明通过：

1. 真实 Tool 先失败再成功重试时，成功业务步骤可以固化，失败 attempt 不会变成工作流节点；
2. Browser 成功动作保存 Host 验证后的 PageContract，固化草稿可以静态检查并实际运行；
3. lookup 的 `input → transform → llm → output` 发布版本用不同 key 实际运行成功，LLM/Agent fallback 收到非空证据；
4. 新发布 manifest 可以从源任务同语言、同意图的新对话中被发现、描述、选择并执行；
5. 全新真实栈从探索到第二次复用完整通过，工程、Browser 和真实 Provider 门禁全部通过。

## 2. P0：区分审计 attempts 与可执行 actions

- [ ] trajectory 继续保存所有 Tool attempts、status、error、effect 和 callId，保证审计事实不丢失。
- [ ] `build_action_ir()` 只把 succeeded action 放入可执行 IR；failed + effect=none 进入 attempt diagnostics，不参与
  Proposal 覆盖、节点依赖和输出来源。
- [ ] failed 且可能已产生副作用、effect unknown 或终态不确定的 action 必须使草稿 blocked，禁止静默过滤。
- [ ] `_request_distillation_proposal()` 只发送可执行 action，并显式携带 status；Proposal 只能覆盖该 action ID 集合。
- [ ] outputProvenance 引用的 callId 必须属于 succeeded action，否则 blocked 并给出 `INVALID_OUTPUT_PROVENANCE`。
- [ ] 用本轮真实形态回归：第一次 browser.read 因 contract 无效失败，第二次成功；最终图只能包含成功读取节点。

### Browser Contract 事实

- [ ] BrowserReadAdapter 在执行前规范化并验证 PageContract；成功 ToolResult 的 observations 保存规范化 contract、action、
  extract/output schema 和 runtime，来源标记为 Host-owned。
- [ ] `_normalize_action()` 可以从可变 arguments 删除 contract，但不得在删除前丢失上述权威证据。
- [ ] Browser compiler 只消费 Host-owned contract；缺失时生成 blocked diagnostic，不能把“固化完成”返回给用户。
- [ ] 禁止为网页、lookup、天气或特定 selector 添加分支；规则只依据 status、effect 和 adapter descriptor。

## 3. P0：统一整体节点输出引用

优先选择并一次性贯彻一种契约：

- `${node.output}` 明确定义为节点完整输出对象；
- `${node.output.field}` 定义为其中字段。

对应修复：

- [ ] `expressions.resolve_value()` 对 path=`output` 返回完整节点输出，对 `output.*` 返回字段；字符串插值中的对象必须明确
  JSON 序列化或禁止，不能依赖 Python `str(dict)`。
- [ ] Compiler 识别整体输出引用，验证引用节点可达；有 outputSchema 时把完整 Schema 传播给下游输入检查。
- [ ] 前端变量选择器、编辑器 JSON 和设计文档使用同一语义。
- [ ] Distillation 的 generative evidence mapping 使用受支持语法，不再生成静态检查通过但运行时失败的表达式。
- [ ] 回归必须实际运行已发布图：q2 固化，q4 运行；transform 输出进入 final-response，模型调用发生，最终 Run succeeded。

## 4. P0：保证固化结果可被同意图再次发现

- [ ] CapabilityDescriptor 阶段接收服务器去实例化后的 task intent、输入类别、输出用途和源语言；具体 URL、key、Secret、
  ToolResult 正文仍不得写入 manifest。
- [ ] Descriptor 输出使用源任务语言，通用 tags 可同时保留稳定 capability 名；不得按中文、英文、lookup 或天气写模板。
- [ ] 发布前/发布测试验证以源任务的去实例化查询调用 `search_manifests()` 时，新 workflowRef 位于 limit 内。
- [ ] 目录 UI/审计不得把纯词法命中标成“语义召回”；选择判断仍由 Agent 在 L0/L1 描述上完成。
- [ ] 本轮中文 lookup 源任务发布后，新中文 q4 对话必须披露并选择 Table Lookup；用另一非 lookup 样本证明没有领域特例。

## 5. P1：固化失败形成可恢复诊断

- [ ] 可分析轨迹的 Proposal/Compiler 拒绝应持久化 `Distillation(status=blocked)` 和结构化 diagnostics。
- [ ] UI 刷新后仍显示阻断原因；修复源 Run/编译规则后允许重新发起，不得让无 draft 的 blocked 记录永久占用
  `DRAFT_ALREADY_EXISTS`。
- [ ] 只有来源 Run 不存在、Run 未成功、无成功 action 等请求级问题直接返回 4xx。
- [ ] blocked 结果不得发布；也不得只在页面底部显示一次性异常字符串。

## 6. 回归门禁只覆盖现有主流程

### 6.1 自动回归

- [ ] 增加 failed-read → succeeded-read 轨迹测试，断言失败 attempt 不在 ProposedNode/DAG，诊断仍保留。
- [ ] 增加 Host Browser ToolResult 测试，断言规范化 PageContract 在 trajectory observations 中。
- [ ] 增加 `${node.output}` 表达式解析、静态检查和实际 Worker 运行测试。
- [ ] 现有 generative lookup 测试必须 publish 并实际运行，不再只断言 definition 形状。
- [ ] 增加 manifest 同语言自召回测试；英文源任务也使用同一通用机制。
- [ ] Browser UI 发布后必须从新对话复用，不得只跳到组件页手动运行。

### 6.2 真实栈验收顺序

1. 主动 probe 确认真正的 `traex_builtin`；
2. 默认工作流开启，真实 Agent 完成一次只读探索；
3. 固化、Presentation 审阅、静态检查、发布 v1；
4. 先从组件页用不同输入运行 v1，确认执行图本身成功；
5. 再开新对话输入同意图不同参数，确认 catalog 披露、Agent 选择并启动同一 v1；
6. 刷新对话、组件历史和 RunDetail，确认结果与轨迹仍在。

每一步失败就记录该阶段根因，不继续用后一步或 fixture 结果替代。

## 7. 最终检查

- [ ] `make demo-check` 通过；
- [ ] `make browser-ui-check` 通过；
- [ ] `make traex-real-check` 实际执行且通过，不得 skip；
- [ ] `make traex-real-product-check` 除现有三个用例外，加入完整固化/执行/发现/复用用例并通过；
- [ ] 实际点击亮暗主题、对话历史/开关、固化、Presentation、检查/发布、组件运行/历史、RunDetail/复制；
- [ ] 长模型调用期间无 SQLite lock，结束后无 processing Turn、running Run 或遗留服务；
- [ ] console error/warning、`git diff --check` 和文档链均通过。

在失败重试仍导致固化 422、发布图仍发生 `INVALID_INPUT`，或新对话仍找不到刚发布版本时，不得声明 032 通过。
