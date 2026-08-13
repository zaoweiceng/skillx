# 设计与规范达成度审查报告（031）

审查日期：2026-08-12

依据：[`REPLAY-030.md`](../REPLAY/REPLAY-030.md)、
[`NEXT_STEPS-030.md`](../STEP/NEXT_STEPS-030.md)、
[`workflow-design-doc.md`](../docs/workflow-design-doc.md)、
[`workflow-dev-doc.md`](../docs/workflow-dev-doc.md)、[`schema.md`](../docs/schema.md)、
[`skillx.md`](../docs/skillx.md) 和 [`demo-acceptance.md`](../docs/demo-acceptance.md)。

代码基线：`0e1b68c`，审查对象为工作区内 REPLAY-030 对应的未提交修复代码。本轮只新增 031 三份
审查文档，不修改业务代码。范围为单用户 Local Demo；按用户要求，Token 上限和消耗只作观测，不作为缺陷或
通过条件。

## 1. 结论

**不通过。**

030 的局部修复有效：真实产品门禁已能启动生产 `traex_builtin` Runtime，运行中取消通过；目录失败审计、无动作
固化资格、主题、组件运行历史和工作流编辑发布均正常。但两条最重要的真实产品路径仍被代码根因阻断：

1. 默认开启工作流时，真实 Agent 已正确提取 `name=Alice`，却因 provenance quote 契约前后矛盾被服务器拒绝，
   两次提取后返回 502，未启动工作流；
2. 工作流关闭后的真实 Agent 可以调用 `lookup.table` 并生成固化草稿，但草稿把真实 Tool 动作编译成输入为
   `null` 的 Agent 节点，并把 `reply` 错误映射到不存在的 `value` 输出路径，静态检查阻止发布。

因此当前只能分别完成“真实 Agent 探索”“生成草稿”和“人工创建确定性工作流”，不能完成设计要求的通配型
“首次探索—固化—发布—新对话复用”主闭环，更不能继续验证失败接力后的修订闭环。此外，默认工作流选择在持有
SQLite 写事务时等待真实模型，实测使 Worker 心跳遭遇 `database is locked`，不符合短事务设计约束。

## 2. REPLAY-030 达成矩阵

| 目标 | 结果 | 真实证据 |
|---|---|---|
| 生产 Runtime resolver 的真实产品门禁 | 基本通过 | 正确识别 `traex_builtin/auto_discovered`，不再在 probe 前失败 |
| 同消息动态 Schema 参数提取 | **未通过** | Agent 返回 `name=Alice`；quote=`姓名参数是 Alice` 被服务器拒绝，两次后 502 |
| 缺参后复用冻结选择 | fixture 通过、真实未完整验证 | 代码复用 ref/schema/session；保存的冻结消息 ID 集合在恢复时被直接丢弃 |
| 真实 workflow-off | 通过（实际 UI） | 8.6s 后返回 `031-ui-agent-ok`，事件中零 `workflow.*` |
| 真实 running cancellation | 通过 | 产品门禁第二个用例进入 running 后到 cancelled |
| 目录失败诊断与持久化 | 后端通过、对话展示未通过 | 数据库保存 failed action/code/digest；同步 502 后 Chat 无可回看 timeline |
| 无动作固化资格 | 通过 | 无工具回答显示“本次没有可复用动作”，不再显示固化按钮 |
| 有动作固化资格 | 通过 | 真实 lookup Agent 成功后显示固化按钮并创建草稿 |
| 真实轨迹编译为可发布工作流 | **未通过** | 草稿静态检查报 `OUTPUT_FIELD_NOT_IN_SCHEMA` |
| Presentation 提议与人工决定 | 部分通过 | 真实草稿生成提议并可接受；底层 DAG 无效，无法发布 |
| 确定性组件运行与历史 | 通过 | 实际 Worker 运行成功，RunDetail、复制和组件历史均正常 |
| 工作流编辑、检查、发布 | 通过（人工示例） | JSON 应用、静态检查、发布 v1 均可点击并成功 |
| 对话历史与失败持久化 | **未通过** | 参数绑定失败只剩用户消息；刷新后错误、耗时、Usage 和目录轨迹不可见 |
| SQLite 短事务与 Worker 并发 | **未通过** | API 持有写事务等待约 45s 模型调用；同期 Worker 心跳实际报 `database is locked` |
| 亮暗主题和主要页面点击 | 通过 | 亮色初始页、暗色全流程、导航/筛选/按钮正常，console 为空 |
| 通配型完整闭环 | **未通过** | 真实固化草稿不可发布，无法进入新对话语义复用 |

## 3. 门禁结果

| 检查 | 结果 |
|---|---|
| `make demo-check` | 通过；Ruff、339 backend、迁移 0001→0023、54 frontend、build、37 独立进程 E2E |
| `make browser-ui-check` | 最终通过；2 passed。sandbox 首次仅因 Chromium MachPort 权限无法启动 |
| `make traex-real-check` | 通过；1 passed，未 skip |
| `make traex-real-product-check` | **失败；1 failed、1 passed** |

产品门禁中 cancellation 通过；workflow-on/off 被放在同一个测试函数内，workflow-on 先失败后 workflow-off 没有执行。
本轮在全新真实前后端和 UI 中单独验证 workflow-off 成功。

## 4. 全新真实前后端验收

本轮使用全新 SQLite、artifact 目录，实际启动 API、独立 Worker、本地网页服务和 Vite，再用应用内 Chromium 点击。
Settings 中真实 probe 成功，Provider 为 real，Agent Runtime、Worker 和 Browser Runtime 均为 available。

### 4.1 默认开启工作流

输入：`请问候名叫 Alice 的人，姓名参数是 Alice`

结果：44.781 秒后 502 `SELECTION_BINDING_INCOMPLETE`，未产生助手消息或 Workflow Run。真实模型的参数提取
终态为：

```json
{
  "bindings": {"name": "Alice"},
  "bindingSources": [{
    "field": "name",
    "messageId": "当前用户消息",
    "quote": "姓名参数是 Alice",
    "confidence": 1,
    "uncertain": false
  }]
}
```

该 quote 满足 Prompt 所写的“对应消息中的连续原文”，且包含在用户消息中；服务器
`_binding_normalization()` 却只接受字符串 value 与 quote 完全相等（少数地点后缀例外），所以拒绝来源。
第二次纠正只得到笼统的“来源无法验证”，无法指导模型改成最小值 span，结果再次失败。

数据库记录了 error Turn 和三次真实模型调用，前端当时只显示“参数提取未满足冻结 Schema 与来源契约”。刷新后只
剩用户消息，Conversation 标题仍为“新对话”，assistantMessageId 为空，失败原因和目录 timeline 消失。

该请求还在创建 Turn/User Message 并 `flush()` 后，继续使用同一个 Session 完成 search、describe 和两次真实模型
调用，直到最后才 commit。实测此约 45 秒窗口内，独立 Worker 的 Browser Runtime 心跳写入触发
`sqlite3.OperationalError: database is locked`，随后才恢复。即使本次没有直接造成 Run 丢失，它也会阻塞心跳、领取和
其它写操作，违反 `workflow-dev-doc.md` 的“写事务保持短小”和“外部调用不在数据库事务中执行”。

### 4.2 工作流关闭与固化资格

- 无工具输入 `请只回复 031-ui-agent-ok，不要调用任何工具`：8.6 秒成功，零 `workflow.*`；页面显示耗时、
  Token 和“本次没有可复用动作”，修复通过。
- 只读探索输入 `查询标识 alpha 对应的本地示例数据并告诉我结果`：9.9 秒成功，真实调用
  `lookup.table`，页面显示“固化为组件”；点击后生成 `Lookup Table Value` 草稿和 Presentation 提议。

人工接受 Presentation 后执行静态检查，结果为：

`OUTPUT_FIELD_NOT_IN_SCHEMA: 引用字段 value 不在上游节点 node_1 的 outputSchema 中`

真实草稿的核心定义为：

- `node_1.type=agent`，`input.key=null`；
- node output schema 只有 `reply`；
- Workflow output 却映射 `reply=${node_1.output.value}`；
- prompt 只是 `复现动作 <callId>`，没有可复用的任务/参数契约。

所以草稿虽被创建，却不能发布或第二次复用。

### 4.3 组件、工作流与界面

实际完成：

- 切换暗色主题；暗色配色、文字对比、表单和卡片布局正常；
- 组件分类筛选，打开“确定性问候”，输入 `031界面验收` 并由真实 Worker 成功运行；
- RunDetail 显示三个成功节点和 `{"message":"你好，031界面验收"}`，复制反馈为“已复制 JSON”；
- 返回详情后调用历史显示 1 条 succeeded、输入/输出、耗时和详情入口；
- 新建示例工作流、切换/应用 JSON、静态检查、发布不可变 v1；DAG 无 `null`；
- 能力页和设置页正常加载；浏览器 console 无 error/warning。

人工创建的确定性流程正常，不等于 Agent 轨迹固化流程正常。

## 5. 预期功能能否完成

当前结果是：

```text
自然语言默认复用
  → 能找到工作流和提取值
  → provenance 契约冲突
  → 不能启动

真实 Agent 首次探索
  → Tool 调用成功
  → 固化草稿生成
  → 动作类型/输入来源/输出来源丢失
  → 静态检查阻断
  → 不能发布和第二次复用
```

因此不能完成 docs 预期的主闭环。修复必须保持通用：天气、问候和 lookup 都只是测试样本，不得形成生产分支。

## 6. 最终判定

基础工程、真实 Runtime、取消、工作流关闭、人工确定性 Runtime 和主要 UI 通过；默认工作流参数来源协议、真实
轨迹固化和长事务隔离仍未通过。031 **不通过**。下一版任务见 [`NEXT_STEPS-031.md`](../STEP/NEXT_STEPS-031.md)。
