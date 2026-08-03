# 对话、工作流与 SkillX 执行规范

## 1. 双工作空间执行边界

平台前端包含两个主要工作空间：

| 工作空间       | 默认执行主体           | 主要输入方式    | 是否允许 LLM |
| ---------- | ---------------- | --------- | -------: |
| 通用对话工作空间   | LLM/Agent        | 自然语言      |     默认允许 |
| 组件与工作流工作空间 | Workflow Runtime | Schema 表单 |  仅指定节点允许 |

两者必须共享底层 Skill、SkillX、MCP、模型、执行记录和 Artifact，但不能共享默认路由逻辑。

---

## 2. 通用对话工作空间

### 2.1 默认行为

通用对话框始终以 LLM 交互为默认行为。

例如用户输入：

```text
成都
```

系统不应通过静态规则直接调用天气工作流，而应由当前对话中的 LLM 根据上下文判断用户意图。

LLM 可以：

* 继续询问用户。
* 直接回答。
* 调用已有组件。
* 调用 Skill、SkillX 或 MCP。
* 启动 Agent 探索任务。
* 建议用户切换到某个工作流组件。

### 2.2 调用现有组件

LLM 判断已有组件适合当前任务时，可以发起组件调用，但必须完成以下步骤：

```text
识别用户意图
→ 匹配组件
→ 提取组件输入
→ 检查必填字段
→ 必要时向用户补充询问
→ 启动工作流
```

此时 LLM 只负责路由和参数收集，工作流启动后不再由 LLM 全程控制。

### 2.3 对话中的组件上下文

当用户已经在对话中明确选择或绑定某个组件时，后续输入可以按该组件解释。

例如当前对话已经绑定“天气查询”组件：

```text
用户：成都
系统：将 city 映射为“成都”，启动天气工作流
```

组件上下文必须显式展示，并允许用户退出：

```text
当前组件：天气查询
[退出组件]
```

不能在用户不可见的情况下长期保留组件绑定。

---

## 3. 工作流工作空间

### 3.1 默认行为

进入组件或工作流页面后，系统默认使用 Workflow Runtime，不经过通用 LLM Planner。

用户操作路径为：

```text
选择组件
→ 查看工作流图
→ 填写结构化输入
→ 校验输入
→ 确认执行
→ 工作流引擎执行
```

### 3.2 LLM 介入条件

只有以下节点可以调用 LLM：

* 节点定义明确为 `llm`。
* 节点定义明确为 `agent`。
* 工作流异常策略允许局部 LLM 恢复。
* 用户主动选择由 LLM 辅助补全输入。

工作流执行器不得因为普通错误自动把整个任务交给 Agent。

### 3.3 控制权原则

局部 LLM 或 Agent 节点执行完成后，必须将结构化输出返回工作流引擎：

```text
Workflow Runtime
    ↓
LLM/Agent Node
    ↓
Schema 校验
    ↓
Workflow Runtime 继续调度
```

LLM 不得默认接管后续全部节点。

---

## 4. 浏览器表单工作流

## 4.1 执行阶段

浏览器表单工作流分为四个阶段：

```text
填写准备
→ 页面一致性检查
→ 用户确认
→ 自动提交
```

只有四个阶段均成功，才允许执行最终提交。

---

## 4.2 填写准备

工作流根据用户输入生成待填写数据：

```json
{
  "name": "张三",
  "phone": "13800000000",
  "reason": "申请内容",
  "attachment": "file.pdf"
}
```

填写前必须完成：

* 输入 Schema 校验。
* 必填字段校验。
* 字段格式校验。
* 文件存在性校验。
* 敏感字段提示。
* 页面登录状态检查。

---

## 4.3 页面一致性检查

提交前必须检查当前页面是否符合工作流记录的页面契约。

检查内容包括：

* 目标 URL 或路由是否匹配。
* 页面关键标题是否匹配。
* 表单关键元素是否存在。
* 元素语义标识是否匹配。
* 必填字段数量是否变化。
* 是否新增未识别输入框。
* 是否删除原有输入框。
* 字段类型是否变化。
* 下拉选项是否发生变化。
* 提交按钮是否仍然匹配。
* 页面是否出现新的协议、验证码或确认步骤。
* 页面是否出现异常提示或权限变化。

### 页面契约示例

```json
{
  "page_id": "expense-application-form",
  "url_pattern": "/expense/apply",
  "required_elements": [
    {
      "field_id": "applicant_name",
      "locator": {
        "role": "textbox",
        "label": "申请人"
      },
      "input_type": "text",
      "required": true
    },
    {
      "field_id": "amount",
      "locator": {
        "role": "spinbutton",
        "label": "金额"
      },
      "input_type": "number",
      "required": true
    }
  ],
  "submit_element": {
    "role": "button",
    "name": "提交申请"
  }
}
```

---

## 4.4 页面漂移等级

页面变化需要划分等级。

### L0：完全一致

所有关键元素和约束匹配。

处理方式：

```text
继续填写
→ 展示确认信息
→ 用户确认后提交
```

### L1：非关键变化

例如：

* 页面样式变化。
* 元素位置变化。
* 非关键说明文字变化。
* DOM 层级变化，但语义定位仍然有效。

处理方式：

* 允许继续。
* 记录页面指纹变化。
* 不自动修改工作流版本。

### L2：可解释但需要确认

例如：

* 元素 ID 变化。
* 新增可选输入框。
* 字段顺序变化。
* 提交按钮名称轻微变化。
* 原定位失败但存在高置信度候选元素。

处理方式：

```text
暂停工作流
→ 展示变化内容
→ 用户选择：
   1. 确认继续
   2. 允许 LLM 辅助匹配
   3. 终止执行
```

### L3：结构性变化

例如：

* 新增必填字段。
* 原必填字段消失。
* 字段含义发生变化。
* 表单步骤数量变化。
* 出现新的授权协议。
* 提交行为发生变化。
* 页面进入不同业务流程。

处理方式：

* 禁止自动提交。
* 默认要求用户处理。
* 可以启动局部 LLM/Agent 分析。
* 分析结果只能生成工作流修订草稿，不能直接覆盖已发布版本。

### L4：安全或身份异常

例如：

* 登录身份与记录不一致。
* 页面域名变化。
* 出现证书或安全警告。
* 页面疑似被劫持。
* 操作对象与用户选择不一致。
* 提交内容可能影响其他主体。

处理方式：

* 立即终止。
* 不允许 LLM 自动恢复。
* 要求用户重新验证环境。

---

## 4.5 用户确认

用户确认必须发生在所有字段填写完成之后、最终提交之前。

确认页面应展示：

* 目标网站。
* 目标业务。
* 当前登录身份。
* 所有待提交字段。
* 上传文件。
* 隐藏字段或系统自动填写字段。
* 可能产生的副作用。
* 页面变化情况。
* 是否使用过 LLM 恢复。
* 提交按钮的实际含义。

用户执行：

```text
确认信息无误并提交
```

之后，工作流才获得一次性的提交授权。

该授权只对：

* 当前 Run。
* 当前页面。
* 当前数据快照。
* 当前提交动作。

有效。

任何字段或页面状态在确认后发生变化，都必须使原授权失效。

---

## 4.6 自动提交

用户确认后，工作流可以自动点击提交按钮。

提交后必须验证：

* 页面是否出现成功提示。
* 是否生成业务编号。
* 是否跳转到预期页面。
* 后端返回是否成功。
* 是否出现部分失败。
* 是否发生重复提交。

最终结果至少包含：

```json
{
  "status": "succeeded",
  "submitted_at": "2026-08-03T11:30:00+08:00",
  "target": "expense-application-form",
  "business_id": "EXP-20260803-001",
  "evidence": {
    "confirmation_snapshot": "artifact://...",
    "result_snapshot": "artifact://..."
  }
}
```

---

## 5. SkillX 定义

## 5.1 设计目标

SkillX 解决传统 Skill 只能为 LLM 提供指南、不能直接被工作流调用的问题。

SkillX 同时支持两种使用模式：

```text
SkillX
├── Guide Interface：供 LLM/Agent 阅读和遵循
└── Execute Interface：供工作流引擎直接调用
```

因此 SkillX 不是简单的可执行脚本，也不是纯 Prompt，而是带有明确契约的复合能力包。

---

## 5.2 与 Skill 的关系

| 能力         | Skill   | SkillX |
| ---------- | ------- | ------ |
| 面向 LLM 的说明 | 支持      | 支持     |
| `SKILL.md` | 必需      | 必需     |
| 工作流直接调用    | 不保证     | 支持     |
| 输入 Schema  | 可选或自然语言 | 必需     |
| 输出 Schema  | 可选或自然语言 | 必需     |
| 确定性入口      | 无       | 可选但推荐  |
| LLM 入口     | 默认      | 支持     |
| 运行约束       | 较弱      | 明确     |
| 权限声明       | 可选      | 必需     |
| 自动测试       | 可选      | 必需     |
| 版本兼容检查     | 较弱      | 必需     |

SkillX 必须保持对现有 Skill 的向下兼容。

一个普通 Skill 可以被平台正常加载，但只能作为 Agent 指南使用；只有完成 SkillX 扩展后，才能作为工作流中的直接能力节点使用。

---

## 5.3 SkillX 目录规范

```text
skill-name/
├── SKILL.md
├── skillx.yaml
├── executors/
│   ├── default.py
│   └── batch.py
├── schemas/
│   ├── input.schema.json
│   └── output.schema.json
├── prompts/
│   └── agent.md
├── scripts/
├── references/
├── assets/
├── examples/
│   ├── input.json
│   └── output.json
└── tests/
    ├── test_contract.py
    ├── test_executor.py
    └── cases/
```

### 必需文件

* `SKILL.md`
* `skillx.yaml`
* 输入 Schema
* 输出 Schema
* 测试用例

如果 SkillX 仅提供受约束的 LLM 节点，不要求存在 Python 执行器；如果声明支持 Direct 模式，则必须提供确定性执行入口。

---

## 6. SkillX 执行模式

一个 SkillX 可以声明一个或多个入口。

### 6.1 Guide 入口

供 Agent 阅读并理解任务方法。

```yaml
interfaces:
  guide:
    entry: SKILL.md
```

该入口本身不保证能够脱离 LLM 执行。

### 6.2 Direct 入口

供 Workflow Runtime 直接调用。

```yaml
interfaces:
  direct:
    runtime: python
    entry: executors/default.py:execute
```

特点：

* 不调用 LLM。
* 输入输出受 Schema 约束。
* 执行结果可预测。
* 必须具备自动测试。

### 6.3 LLM 入口

用于一次受限的模型调用，而不是完整 Agent Loop。

```yaml
interfaces:
  llm:
    prompt: prompts/extract.md
    model_profile: structured-extraction
    output_schema: schemas/output.schema.json
```

适用于：

* 分类。
* 抽取。
* 结构化生成。
* 内容判断。

### 6.4 Agent 入口

允许 Agent 在限定范围内使用工具。

```yaml
interfaces:
  agent:
    prompt: prompts/agent.md
    allowed_tools:
      - issue.get
      - issue.get_comments
    max_steps: 6
```

只有确实需要动态工具选择时才使用。

---

## 7. SkillX 清单规范

示例：

```yaml
apiVersion: skillx.platform/v1
kind: SkillX

metadata:
  name: weather-query
  displayName: 天气查询
  version: 1.0.0
  description: 根据城市查询当前天气

compatibility:
  skillSpec: ">=1.0"
  platform: ">=0.1.0"

interfaces:
  guide:
    entry: SKILL.md

  direct:
    runtime: python
    entry: executors/default.py:execute
    inputSchema: schemas/input.schema.json
    outputSchema: schemas/output.schema.json

execution:
  defaultMode: direct
  timeoutSeconds: 30
  maxRetries: 2
  idempotent: true
  sideEffect: false

dependencies:
  mcpTools:
    - id: weather.get_current
      version: ">=1.0,<2.0"

permissions:
  network:
    - weather-service
  filesystem:
    mode: none
  secrets:
    - WEATHER_API_KEY

observability:
  redact:
    - WEATHER_API_KEY
  metrics:
    - duration
    - mcp_call_count

tests:
  contract:
    - tests/test_contract.py
  runtime:
    - tests/test_executor.py
```

---

## 8. SkillX 输入输出约束

### 8.1 输入 Schema

所有工作流可调用入口必须定义输入 Schema。

天气查询示例：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["city"],
  "properties": {
    "city": {
      "type": "string",
      "minLength": 1,
      "description": "需要查询天气的城市"
    },
    "date": {
      "type": "string",
      "format": "date"
    }
  }
}
```

### 8.2 输出 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["city", "condition", "temperature"],
  "properties": {
    "city": {
      "type": "string"
    },
    "condition": {
      "type": "string"
    },
    "temperature": {
      "type": "number"
    },
    "source": {
      "type": "string"
    }
  }
}
```

### 8.3 契约规则

SkillX 必须满足：

1. 不接受 Schema 外的隐式参数。
2. 必填参数不能依赖自然语言猜测。
3. Direct 入口不能返回任意未声明结构。
4. 错误必须使用标准错误类型。
5. 输出 Schema 校验失败视为节点失败。
6. 不允许通过日志返回主要业务输出。
7. 文件输出必须注册为 Artifact。
8. Secret 只能通过 Secret Reference 注入。

---

## 9. SkillX 标准错误模型

```json
{
  "code": "DEPENDENCY_UNAVAILABLE",
  "message": "天气服务暂时不可用",
  "retryable": true,
  "details": {
    "dependency": "weather.get_current"
  }
}
```

标准错误类型至少包括：

* `INVALID_INPUT`
* `DEPENDENCY_UNAVAILABLE`
* `AUTHENTICATION_FAILED`
* `PERMISSION_DENIED`
* `TIMEOUT`
* `CONTRACT_VIOLATION`
* `PAGE_DRIFT`
* `USER_CONFIRMATION_REQUIRED`
* `EXECUTION_FAILED`
* `UNSUPPORTED_OPERATION`

工作流引擎根据错误类型决定：

* 自动重试。
* 暂停并等待用户。
* 调用局部 LLM。
* 终止工作流。
* 进入补偿流程。

---

## 10. Skill 到 SkillX 的转换

现有 Skill 的迁移过程不能简单地自动生成一个执行函数，而应先分析其本质。

```text
读取 SKILL.md
→ 识别输入
→ 识别输出
→ 分析执行步骤
→ 分类每个步骤
→ 绑定现有 MCP 或算子
→ 决定可执行模式
→ 生成 SkillX 草稿
→ 补充 Schema
→ 实现入口
→ 测试
→ 发布
```

### 10.1 步骤分类

每个 Skill 步骤应被分类为：

| 类型        | 转换结果                 |
| --------- | -------------------- |
| 固定数据转换    | Direct 算子            |
| 固定 MCP 调用 | Direct MCP 节点        |
| 固定脚本      | Direct 执行入口          |
| 单次语义处理    | LLM 入口               |
| 动态工具选择    | Agent 入口             |
| 人工决策      | Approval 节点          |
| 无法确定      | 保留为 Guide，不提供 Direct |

### 10.2 转换结果

转换后可能出现三种 SkillX。

#### Direct SkillX

所有步骤均可确定执行：

```text
Input → Direct Executor → Output
```

#### Hybrid SkillX

大部分步骤确定，部分步骤需要 LLM：

```text
Input
→ Direct
→ LLM
→ Direct
→ Output
```

#### Guide-only SkillX

暂时只能为 Agent 提供指南：

```text
Guide Interface
→ Agent Runtime
```

即使已经增加 `skillx.yaml`，如果没有可执行入口，也不能标记为零 LLM能力。

---

## 11. SkillX 与工作流的关系

SkillX 内部可以包含多个步骤，但平台需要控制抽象层级。

### 建议原则

* 单个 MCP Tool 保持原子能力。
* SkillX 表达一个稳定的领域能力。
* Workflow 负责编排多个业务能力。
* Component 面向最终用户提供完整任务入口。

示例：

```text
MCP Tool：
weather.get_current

SkillX：
weather-query
- 校验城市
- 调用天气 MCP
- 规范化天气数据

Workflow：
travel-preparation
- 查询天气
- 查询交通
- 生成出行清单

Component：
出行准备助手
```

不应把所有业务流程都塞进一个超大型 SkillX。

---

## 12. SkillX 节点展示

工作流 DAG 中的 SkillX 节点应明确展示运行模式：

```text
天气查询
SkillX 1.0.0
模式：Direct
LLM：不使用
依赖：weather.get_current
```

混合模式示例：

```text
投诉分析
SkillX 2.1.0
模式：Hybrid
LLM 节点：1
MCP 调用：2
```

点击后展示：

* Guide 内容。
* 可用入口。
* Input Schema。
* Output Schema。
* 依赖 MCP。
* 所需权限。
* 是否有副作用。
* 当前运行模式。
* 当前绑定版本。
* 测试状态。

---

## 13. 运行时入口选择

Workflow Compiler 在发布时确定 SkillX 入口，运行时不得自由猜测。

```yaml
- id: query_weather
  type: skillx
  capability: weather-query@1.0.0
  interface: direct
  input:
    city: "${input.city}"
```

如指定：

```yaml
interface: direct
```

运行时禁止退化为 LLM 或 Agent。

如希望失败后允许局部 LLM 恢复，必须显式配置：

```yaml
fallback:
  on:
    - PAGE_DRIFT
  interface: agent
  requireUserApproval: true
```

---

## 14. 核心状态机

浏览器写操作节点状态建议扩展为：

```text
pending
running
page_validating
waiting_input_confirmation
authorized
submitting
verifying
succeeded
drift_detected
waiting_user
waiting_llm
failed
cancelled
```

其中：

* `authorized` 表示用户已确认当前数据快照。
* 任何页面或数据变化都会将状态重新退回 `waiting_input_confirmation`。
* 未进入 `authorized` 状态不得执行提交。

---

## 15. 验收标准补充

### 对话与工作流

1. 通用对话框默认由 LLM 处理。
2. 通用对话框不会仅根据单个城市名称直接调用天气工作流。
3. 已绑定组件时，对话输入可以映射为组件输入。
4. 工作流页面默认不启动通用 Agent Planner。
5. 工作流只在显式声明的节点调用 LLM。
6. 局部 LLM 节点完成后，控制权返回工作流引擎。

### 浏览器提交

7. 提交前必须完成页面一致性检查。
8. 用户必须确认最终填写数据。
9. 用户确认绑定当前数据和页面快照。
10. 确认后发生任何字段变化，原授权立即失效。
11. 新增必填字段时禁止自动提交。
12. 元素 ID 变化时按页面漂移策略处理。
13. 高风险环境异常不得由 LLM 自动恢复。
14. 局部 LLM 修复不能直接覆盖已发布工作流。
15. 提交后必须验证结果并保存证据。

### SkillX

16. 普通 Skill 可以继续作为 Guide 使用。
17. SkillX 必须兼容 `SKILL.md`。
18. 工作流可调用入口必须声明输入输出 Schema。
19. Direct SkillX 在无模型配置时仍能运行。
20. Guide-only SkillX 不得标记为零 LLM。
21. SkillX 发布前必须通过契约测试。
22. Workflow 发布时必须锁定 SkillX 版本和接口。
23. Direct 接口失败时不得隐式切换到 Agent。
24. MCP、权限和 Secret 依赖必须显式声明。
25. SkillX 的副作用和幂等性必须明确标记。

---

## 16. 仍需明确的 SkillX 规范问题

### 1. SkillX 是否允许独立于原 Skill 存在

建议不允许。SkillX 应始终包含 `SKILL.md`，以保证 LLM 和人类都能理解其能力与边界。

### 2. SkillX 是否允许内部定义完整 DAG

建议第一阶段允许简单内部步骤，但复杂编排应提升为独立 Workflow，避免 SkillX 与 Workflow 职责重叠。

### 3. Direct 入口支持哪些运行时

建议第一阶段仅支持：

* Python 函数。
* MCP Tool 组合。
* 平台内置算子。

暂不直接支持任意 Shell，以降低安全风险。

### 4. SkillX 的创建权限

建议：

* 普通用户可以发起转换请求。
* 能力开发 Agent 可以生成草稿。
* 能力开发者负责修改和测试。
* 发布者负责审核。
* 具有副作用的 SkillX 需要管理员审核。

### 5. SkillX 是否作为开放标准

建议第一阶段作为平台内部规范，但从一开始保持：

* 清单可导出。
* Schema 使用标准 JSON Schema。
* 入口协议与具体框架解耦。
* 不绑定 TraeX 的内部数据结构。

这样后续才有可能独立发布规范。
