# SkillX Specification

---

## 摘要

SkillX 是一种面向 AI Agent 与确定性工作流的可移植能力包规范。

传统 Agent Skill 主要通过自然语言说明、参考资料和脚本，为 LLM 或 Agent 提供完成任务的指南。此类 Skill 通常缺少稳定的程序化调用入口、严格的输入输出契约、运行权限声明和副作用控制，因此难以被持久化工作流直接调用。

SkillX 在兼容 Agent Skills 基础目录和 `SKILL.md` 的前提下，增加：

* 结构化清单 `skillx.yaml`；
* 可被工作流直接调用的执行接口；
* JSON Schema 输入输出契约；
* Direct、LLM 和 Agent 三类执行模式；
* MCP Tool 和其他 SkillX 的能力依赖声明；
* 权限、Secret、副作用和人工确认规范；
* 超时、取消、重试和幂等性语义；
* 标准化结果、事件和错误模型；
* 版本兼容、测试和发布规则。

SkillX 的目标不是取代 MCP、Agent Skills 或工作流系统，而是在三者之间建立稳定的能力契约：

```text
Agent Skills：说明如何完成任务
MCP：提供原子工具和外部系统访问能力
SkillX：提供可调用、有契约的领域能力
Workflow：编排多个 SkillX、MCP 和控制节点
Component：向最终用户提供表单化任务入口
```

---

# 1. 状态与适用范围

## 1.1 规范状态

本文档为 SkillX `0.1.0-draft` 草案。

在 `1.0.0` 发布前：

* 字段名称可能调整；
* 运行协议可能调整；
* 兼容性规则可能收紧；
* 实现方不应假设不同草案版本之间自动兼容。

生产系统采用本草案时，必须固定所支持的 `apiVersion`。

## 1.2 适用范围

本规范定义：

1. SkillX 包目录结构；
2. `SKILL.md` 与 SkillX 清单的关系；
3. `skillx.yaml` 字段和语义；
4. SkillX 接口类型；
5. 输入输出契约；
6. 执行生命周期；
7. 依赖和能力绑定；
8. 权限与安全边界；
9. 副作用和用户确认；
10. 错误、重试和幂等性；
11. 包版本和兼容性；
12. Host 与 Package 的符合性要求。

## 1.3 不属于本规范的内容

以下内容不由 SkillX Core 规范定义：

* 通用工作流 DAG 文件格式；
* Agent 的具体推理算法；
* 模型供应商 API；
* MCP Server 的实现方式；
* 用户界面的具体布局；
* Secret 存储后端；
* 容器或虚拟机调度系统；
* SkillX Registry 的服务端 API；
* 计费和多租户实现。

实现方可以针对这些内容定义扩展规范。

---

# 2. 设计基础

SkillX 兼容 Agent Skills 的基本组织方式。Agent Skills 将 Skill 定义为至少包含一个 `SKILL.md` 的目录，并允许附带 `scripts/`、`references/` 和 `assets/` 等资源；其加载方式强调渐进披露，即先发现名称和描述，再按需读取完整说明。

SkillX 使用 JSON Schema Draft 2020-12 描述接口输入和输出。JSON Schema 用于描述 JSON 数据结构、约束和注释；Draft 2020-12 提供 `$defs`、`prefixItems`、动态引用等机制。

SkillX 可以将 MCP Tool 声明为能力依赖。MCP Tool 使用 JSON Schema 描述输入，并可以提供输出 Schema；MCP 不限定工具必须通过何种用户界面调用，因此工作流引擎可以在不经过 LLM 工具选择的情况下直接调用已绑定工具。

本文档中的 `MUST`、`MUST NOT`、`SHOULD`、`SHOULD NOT`、`MAY` 等大写词语，按照 BCP 14、RFC 2119 和 RFC 8174 的要求等级解释。

SkillX 包版本采用 Semantic Versioning 2.0.0。

---

# 3. 术语

## 3.1 Skill

符合 Agent Skills 目录结构，主要通过 `SKILL.md` 为 LLM 或 Agent 提供任务说明、规则、参考资料和可选脚本的能力包。

## 3.2 SkillX

包含 `SKILL.md` 和 `skillx.yaml`，并符合本规范的能力包。

## 3.3 SkillX Package

一个完整的 SkillX 目录或打包归档。

## 3.4 SkillX Host

发现、加载、校验和调用 SkillX 的应用程序。

例如：

* Agent 客户端；
* 工作流平台；
* IDE；
* CLI；
* 服务端任务执行器。

## 3.5 SkillX Runtime

实际执行某个 SkillX 接口的运行环境。

例如：

* Python venv；
* 独立进程；
* 容器；
* Host 内置运行时；
* 受约束的 LLM 调用；
* Agent Runtime。

## 3.6 Interface

SkillX 对外暴露的可调用入口。

一个 SkillX 可以有多个接口，例如：

* 单条天气查询；
* 批量天气查询；
* 文本分类；
* 完整 Agent 分析。

## 3.7 Direct Interface

不调用 LLM、不运行 Agent Loop，由确定性代码、MCP Tool 或平台算子完成的接口。

“Direct”表示控制过程不依赖模型，不保证外部数据、时间或网络结果完全一致。

## 3.8 LLM Interface

由 Host 执行一次受约束模型调用的接口。

LLM Interface 不允许自主发现和调用工具。

## 3.9 Agent Interface

允许 Agent 在限定工具、步骤、Token 和时间范围内执行动态规划的接口。

## 3.10 Capability

SkillX 接口可以调用的外部能力，包括：

* MCP Tool；
* 其他 SkillX 接口；
* Host 内置算子；
* 浏览器执行器；
* 文件、数据库或其他受控服务。

## 3.11 Effect

执行过程中对 SkillX 外部环境产生的可观察影响。

例如：

* 修改文件；
* 更新数据库；
* 提交表单；
* 发送消息；
* 删除资源；
* 支付；
* 修改权限。

## 3.12 Artifact

执行产生的文件型或二进制产物，例如：

* CSV；
* PDF；
* 图片；
* 截图；
* 压缩包；
* 补丁文件；
* 日志报告。

## 3.13 Invocation

某个 SkillX 接口的一次调用实例。

---

# 4. 设计原则

## 4.1 兼容而非替代

SkillX MUST 保留 `SKILL.md`。

一个现有 Skill 可以通过增加以下内容渐进转换为 SkillX：

```text
SKILL.md
skillx.yaml
schemas/
executors/ 或 prompts/
tests/
```

SkillX MUST NOT 修改 `SKILL.md` 在 Agent Skills 中的基本语义。

## 4.2 确定性优先

能够通过代码、参数映射、固定 MCP Tool 或平台算子完成的步骤，SHOULD 使用 Direct Interface。

仅在以下情况下使用 LLM 或 Agent：

* 需要理解非结构化语义；
* 需要生成自然语言内容；
* 需要根据上下文动态决策；
* 无法预先确定工具调用顺序；
* 确定性规则无法合理覆盖。

## 4.3 显式契约

工作流可调用接口 MUST 声明：

* 输入 Schema；
* 输出 Schema；
* 执行模式；
* 运行入口；
* 能力依赖；
* 权限；
* 副作用；
* 超时；
* 幂等性；
* 错误语义。

## 4.4 最小权限

所有权限默认拒绝。

SkillX 声明的权限是允许范围上限，不是 Host 必须授予的权限。

## 4.5 人类保留最终控制权

具有显著外部副作用的操作 MUST 支持用户检查和拒绝。

用户确认必须绑定具体执行内容，而不能成为无限期或无限范围授权。

## 4.6 可移植性

SkillX Core 不绑定：

* 特定模型；
* 特定 Agent；
* 特定云服务；
* 特定工作流平台；
* 特定 MCP SDK。

## 4.7 可审计性

每次调用 SHOULD 能够追踪：

* SkillX 名称和版本；
* 接口；
  -输入；
* 使用的能力；
* 权限；
* 用户确认；
* 输出；
* 错误；
* Artifact；
* 时间和资源消耗。

---

# 5. 包结构

## 5.1 最小目录

一个最小 SkillX 包必须包含：

```text
skill-name/
├── SKILL.md
└── skillx.yaml
```

只有这两个文件的包可以作为 Guide-only SkillX 使用，但不能宣称具有工作流可执行能力。

## 5.2 推荐目录

```text
skill-name/
├── SKILL.md
├── skillx.yaml
├── schemas/
│   ├── query.input.schema.json
│   ├── query.output.schema.json
│   └── error.schema.json
├── executors/
│   ├── query.py
│   └── batch.py
├── prompts/
│   ├── classify.md
│   └── agent.md
├── contracts/
│   └── target-page.json
├── scripts/
├── references/
├── assets/
├── examples/
│   ├── query.input.json
│   └── query.output.json
├── tests/
│   ├── test_contract.py
│   ├── test_query.py
│   └── cases/
├── pyproject.toml
├── uv.lock
├── README.md
└── LICENSE
```

## 5.3 文件路径规则

包内路径：

* MUST 使用相对路径；
* MUST NOT 以 `/`、`\` 或盘符开头；
* MUST NOT 包含解析后逃逸包根目录的 `..`；
* MUST 使用 UTF-8；
* SHOULD 使用 `/` 作为规范路径分隔符；
* SHOULD 区分大小写；
* MUST NOT 依赖符号链接访问包外文件。

Host MUST 防止归档路径穿越和符号链接逃逸。

## 5.4 保留文件名

以下文件名由规范保留：

| 文件            | 要求          |
| ------------- | ----------- |
| `SKILL.md`    | 必需          |
| `skillx.yaml` | 必需          |
| `skillx.lock` | 可选，保存依赖解析结果 |
| `LICENSE`     | 推荐          |
| `README.md`   | 可选          |

实现方 MUST NOT 使用其他文件替代 `skillx.yaml`。

---

# 6. `SKILL.md` 要求

## 6.1 基础兼容性

`SKILL.md` MUST 符合 Agent Skills 的基础要求：

* YAML Frontmatter；
* 必需的 `name`；
* 必需的 `description`；
* Markdown 指南正文。

## 6.2 名称一致性

`SKILL.md` 中的：

```yaml
name: weather-query
```

MUST 与 `skillx.yaml` 中的：

```yaml
metadata:
  name: weather-query
```

完全一致。

## 6.3 描述一致性

`SKILL.md.description` 和 `skillx.yaml.metadata.description` SHOULD 一致。

如两者不同：

* Host SHOULD 发出验证警告；
* Registry 展示时 SHOULD 以 `skillx.yaml` 为结构化来源；
* Agent 激活时仍以 `SKILL.md` 为指南来源。

## 6.4 推荐 Frontmatter

```yaml
---
name: weather-query
description: 根据城市查询当前天气和天气预报。适用于用户提供城市名称并要求查询天气的场景。
license: Apache-2.0
compatibility: Requires a SkillX host with MCP tool binding support.
metadata:
  skillx.manifest: skillx.yaml
  skillx.version: "1.0.0"
---
```

## 6.5 指南正文

`SKILL.md` SHOULD 包含：

1. 能力目标；
2. 适用场景；
3. 不适用场景；
4. 输入含义；
5. 输出含义；
6. 执行规则；
7. 可用工具；
8. 失败处理；
9. 安全边界；
10. 示例。

`SKILL.md` 中的自然语言说明 MUST NOT 扩大 `skillx.yaml` 声明的权限。

---

# 7. SkillX 清单

## 7.1 文件格式

`skillx.yaml` MUST 是合法 YAML 1.2 文档。

其顶层结构为：

```yaml
apiVersion: skillx.dev/v1alpha1
kind: SkillX
metadata: {}
spec: {}
```

## 7.2 顶层字段

| 字段           | 类型     | 必需 | 说明            |
| ------------ | ------ | -: | ------------- |
| `apiVersion` | string |  是 | 清单 Schema 版本  |
| `kind`       | string |  是 | 固定为 `SkillX`  |
| `metadata`   | object |  是 | 包元数据          |
| `spec`       | object |  是 | 接口、环境、能力和安全规范 |
| `x-*`        | any    |  否 | 扩展字段          |

未知的非 `x-*` 顶层字段 MUST 被视为验证错误。

## 7.3 `apiVersion`

本草案定义：

```yaml
apiVersion: skillx.dev/v1alpha1
```

Host MUST 根据 `apiVersion` 选择对应解析器。

Host MUST NOT 静默地将未知主版本解释为当前版本。

## 7.4 `kind`

必须为：

```yaml
kind: SkillX
```

---

# 8. Metadata

## 8.1 完整结构

```yaml
metadata:
  namespace: org.example
  name: weather-query
  version: 1.0.0
  displayName: 天气查询
  description: 根据城市查询天气
  license: Apache-2.0
  authors:
    - name: Example Team
      email: team@example.org
  homepage: https://example.org/skills/weather-query
  repository: https://example.org/repo/weather-query
  keywords:
    - weather
    - forecast
  labels:
    domain: travel
    stability: stable
```

## 8.2 `namespace`

`namespace` 用于全局区分不同发布者的同名 SkillX。

规则：

* SHOULD 使用反向域名或组织命名空间；
* MUST 由小写字母、数字、点和连字符组成；
* MUST NOT 包含空格；
* SHOULD 在 Registry 内保持唯一。

示例：

```yaml
namespace: org.example
```

未提供时，包只能保证在本地 Skill 目录内唯一。

## 8.3 `name`

`name`：

* MUST 为 1 至 64 个字符；
* MUST 由小写字母、数字和单连字符组成；
* MUST NOT 以连字符开始或结束；
* MUST NOT 包含连续连字符；
* MUST 与 `SKILL.md.name` 一致。

推荐正则：

```regex
^[a-z0-9]+(?:-[a-z0-9]+)*$
```

## 8.4 `version`

`version` MUST 为有效的 Semantic Versioning 2.0.0 版本。

示例：

```yaml
version: 1.2.3
version: 2.0.0-beta.1
```

## 8.5 规范引用

SkillX 的完整规范引用格式为：

```text
<namespace>/<name>@<version>#<interface>
```

例如：

```text
org.example/weather-query@1.2.0#query
```

如果省略接口，则表示 `spec.defaultInterface`。

## 8.6 `displayName`

用于人类界面展示。

`displayName`：

* SHOULD 简短；
* MAY 使用任意语言；
* MUST NOT 用作程序标识符。

## 8.7 `description`

描述能力做什么以及何时使用。

`description`：

* MUST 非空；
* SHOULD 包含使用条件；
* SHOULD 避免仅使用“助手”“处理工具”等模糊描述。

## 8.8 `license`

`license` SHOULD 使用 SPDX License Expression。SPDX 提供标准化许可证短标识和组合表达式。

示例：

```yaml
license: Apache-2.0
license: MIT
license: Apache-2.0 OR MIT
license: LicenseRef-Proprietary
```

## 8.9 `authors`

作者信息 MAY 包含：

```yaml
authors:
  - name: Example Team
    email: team@example.org
    url: https://example.org
```

## 8.10 `labels`

`labels` 是供 Registry、UI 和策略引擎使用的字符串映射。

安全决策 MUST NOT 仅依赖包作者提供的 Label。

---

# 9. Spec 顶层结构

```yaml
spec:
  guide: {}
  compatibility: {}
  defaultInterface: query
  interfaces: {}
  capabilities: {}
  environments: {}
  permissions: {}
  policies: {}
```

| 字段                 |     必需 | 说明              |
| ------------------ | -----: | --------------- |
| `guide`            |      是 | `SKILL.md` 指南入口 |
| `compatibility`    |      否 | Host 和平台兼容范围    |
| `defaultInterface` | 有接口时必需 | 默认调用接口          |
| `interfaces`       |      否 | 可执行接口集合         |
| `capabilities`     |      否 | 外部能力依赖          |
| `environments`     |      否 | 执行环境定义          |
| `permissions`      |      否 | 包级权限上限          |
| `policies`         |      否 | 包级执行策略          |

---

# 10. Guide

## 10.1 定义

```yaml
guide:
  entry: SKILL.md
```

`entry` 在当前版本 MUST 为 `SKILL.md`。

## 10.2 使用方式

Host MAY：

* 将名称和描述加入 Agent 能力目录；
* 在任务匹配时加载完整 `SKILL.md`；
* 将 Guide 展示给用户；
* 使用 Guide 辅助创建工作流。

Guide 本身不是可执行接口。

---

# 11. Interface 模型

## 11.1 接口集合

```yaml
interfaces:
  query:
    kind: direct
    # ...

  classify:
    kind: llm
    # ...

  investigate:
    kind: agent
    # ...
```

接口名称：

* MUST 在包内唯一；
* MUST 符合 SkillX 名称格式；
* SHOULD 描述操作而非实现方式。

## 11.2 公共字段

所有接口支持以下公共字段：

```yaml
interfaces:
  query:
    kind: direct
    title: 查询天气
    description: 根据城市查询当前天气
    inputSchema: schemas/query.input.schema.json
    outputSchema: schemas/query.output.schema.json
    examples:
      - input: examples/query.input.json
        output: examples/query.output.json
    capabilities:
      - weather-current
    execution: {}
    permissions: {}
    effects: []
    artifacts: {}
```

| 字段             | 必需 | 说明                       |
| -------------- | -: | ------------------------ |
| `kind`         |  是 | `direct`、`llm` 或 `agent` |
| `title`        |  否 | UI 展示名称                  |
| `description`  |  是 | 接口功能                     |
| `inputSchema`  |  是 | 输入 JSON Schema           |
| `outputSchema` |  是 | 输出 JSON Schema           |
| `examples`     |  否 | 输入输出示例                   |
| `capabilities` |  否 | 可调用能力 ID                 |
| `execution`    |  是 | 超时、重试、并发等                |
| `permissions`  |  否 | 接口权限                     |
| `effects`      |  否 | 副作用声明                    |
| `artifacts`    |  否 | Artifact 规则              |
| `x-*`          |  否 | 扩展字段                     |

## 11.3 默认接口

存在一个或多个接口时，`defaultInterface` MUST 指向已定义接口。

```yaml
defaultInterface: query
```

Host MUST NOT 根据名称猜测默认接口。

---

# 12. Direct Interface

## 12.1 定义

Direct Interface 由确定性运行时执行。

```yaml
interfaces:
  query:
    kind: direct
    inputSchema: schemas/query.input.schema.json
    outputSchema: schemas/query.output.schema.json
    runtime:
      environment: python
      protocol: skillx.python-callable/v1
      entry: executors/query.py:execute
```

## 12.2 Direct 约束

Direct Interface：

* MUST NOT 调用模型；
* MUST NOT启动 Agent Loop；
* MUST NOT动态发现未声明能力；
* MAY 调用已绑定 MCP Tool；
* MAY 调用其他 Direct SkillX；
* MAY 根据结构化数据执行分支和循环；
* MAY读取实时外部数据；
* MAY包含副作用，但必须按本规范声明和授权。

## 12.3 传递确定性

一个 Direct Interface 只有在其全部依赖均不调用模型时，才能被标记为“零 LLM”。

如果 Direct Interface 依赖：

* LLM Interface；
* Agent Interface；
* 可触发模型采样的 MCP 能力；

Host MUST 将其整体执行类别标记为非确定性模型执行。

## 12.4 Runtime

Direct Interface MUST 声明 `runtime`：

```yaml
runtime:
  environment: python
  protocol: skillx.python-callable/v1
  entry: executors/query.py:execute
```

或：

```yaml
runtime:
  environment: process
  protocol: skillx.stdio-json/v1
  command:
    - python
    - -m
    - weather_skillx
```

---

# 13. LLM Interface

## 13.1 定义

LLM Interface 由 Host 进行受控模型调用：

```yaml
interfaces:
  classify:
    kind: llm
    inputSchema: schemas/classify.input.schema.json
    outputSchema: schemas/classify.output.schema.json
    prompt:
      entry: prompts/classify.md
    model:
      profile: structured-classification
      maxCalls: 1
      maxInputTokens: 8000
      maxOutputTokens: 1000
```

## 13.2 LLM 约束

LLM Interface：

* MUST 由 Host 管理模型调用；
* MUST 提供输出 Schema；
* MUST NOT自主调用 Tool；
* MUST NOT运行 Agent Loop；
* 默认 MUST 只执行一次模型调用；
* MUST 将模型输出通过输出 Schema 校验；
* SHOULD 使用结构化输出能力；
* MUST 在 Schema 校验失败时返回契约错误。

需要多个模型调用或动态工具调用时，应使用 Agent Interface 或外部 Workflow。

## 13.3 模型配置

SkillX SHOULD 引用 Host 定义的模型 Profile，而不是绑定具体供应商：

```yaml
model:
  profile: structured-extraction
```

包可以声明能力要求：

```yaml
model:
  requirements:
    structuredOutput: true
    vision: false
    minimumContextTokens: 16000
```

Host 可以拒绝无法满足要求的调用。

---

# 14. Agent Interface

## 14.1 定义

```yaml
interfaces:
  investigate:
    kind: agent
    inputSchema: schemas/investigate.input.schema.json
    outputSchema: schemas/investigate.output.schema.json
    prompt:
      entry: prompts/investigate.md
    agent:
      profile: general-reasoning
      maxSteps: 8
      maxModelCalls: 8
      maxTotalTokens: 30000
      allowedCapabilities:
        - issue-get
        - issue-comments
```

## 14.2 Agent 约束

Agent Interface：

* MUST 声明最大步骤数；
* MUST 声明最大模型调用次数或总 Token；
* MUST 声明允许使用的能力；
* MUST NOT访问未声明能力；
* MUST 输出符合 Schema 的最终结果；
* SHOULD 保存可审计动作轨迹；
* SHOULD 支持取消；
* MUST 在达到限制时终止。

## 14.3 权限边界

Guide 或 Prompt 中出现的工具名称，不构成授权。

只有 `allowedCapabilities` 和 Host 实际授予的权限共同决定 Agent 可以调用的能力。

---

# 15. 输入输出契约

## 15.1 Schema 版本

所有输入和输出 Schema MUST 使用 JSON Schema Draft 2020-12。

推荐显式声明：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema"
}
```

## 15.2 输入必须为对象

SkillX Core v1 的接口输入根节点 MUST 为 JSON Object：

```json
{
  "type": "object"
}
```

无参数接口 SHOULD 使用：

```json
{
  "type": "object",
  "additionalProperties": false
}
```

## 15.3 输出必须为对象

接口输出根节点 MUST 为 JSON Object。

简单字符串结果应包装为：

```json
{
  "type": "object",
  "required": ["text"],
  "properties": {
    "text": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

## 15.4 严格字段

生产接口 SHOULD 设置：

```json
"additionalProperties": false
```

这样可以避免未声明字段在工作流中传播。

## 15.5 输入验证

Host MUST 在执行任何接口逻辑和副作用之前验证输入。

输入不符合 Schema 时：

* MUST 返回 `INVALID_INPUT`；
* MUST NOT调用模型；
* MUST NOT调用外部能力；
* MUST NOT产生副作用。

## 15.6 输出验证

Host MUST 验证最终输出。

输出不符合 Schema 时，调用 MUST 被标记为失败，并返回：

```text
CONTRACT_VIOLATION
```

Host MUST NOT将不合法输出作为成功结果传递给下游工作流节点。

## 15.7 默认值

Schema 中的 `default` 是注释信息，不自动意味着 Host 必须填充默认值。

是否应用默认值由 Host Profile 决定。

采用默认值的 Host MUST 在执行记录中保留默认值来源。

## 15.8 敏感字段

敏感输入 SHOULD 使用扩展注释：

```json
{
  "type": "string",
  "writeOnly": true,
  "x-skillx-sensitive": true
}
```

敏感字段：

* MUST 在日志中脱敏；
* MUST NOT出现在普通错误消息中；
* MUST NOT被写入 Artifact，除非用户明确授权。

---

# 16. UI Schema

SkillX MAY 提供独立 UI Schema：

```yaml
interfaces:
  submit:
    inputSchema: schemas/submit.input.schema.json
    uiSchema: schemas/submit.ui.schema.json
```

UI Schema 用于描述：

* 字段顺序；
* 控件类型；
* 分组；
* 帮助文本；
* 文件上传；
* Secret 选择器；
* 确认提示。

UI Schema MUST NOT改变输入 Schema 的数据约束。

---

# 17. Capability 依赖

## 17.1 声明

```yaml
capabilities:
  weather-current:
    kind: mcp-tool
    required: true
    selector:
      toolName: weather.get_current
    contract:
      inputSchema: schemas/capabilities/weather.input.schema.json
      outputSchema: schemas/capabilities/weather.output.schema.json
```

接口通过 ID 引用：

```yaml
interfaces:
  query:
    capabilities:
      - weather-current
```

## 17.2 能力类型

Core 定义以下能力类型：

| `kind`          | 说明           |
| --------------- | ------------ |
| `mcp-tool`      | MCP Tool     |
| `skillx`        | 其他 SkillX 接口 |
| `host-operator` | Host 内置确定性算子 |
| `browser`       | 浏览器执行能力      |
| `custom`        | 扩展能力         |

## 17.3 MCP Tool

```yaml
capabilities:
  weather-current:
    kind: mcp-tool
    selector:
      server: weather-service
      toolName: weather.get_current
    contract:
      inputSchema: schemas/weather-tool.input.json
      outputSchema: schemas/weather-tool.output.json
      schemaDigest: sha256:0123456789abcdef
```

SkillX 定义的 MCP Tool 版本和 Schema Digest 是 Host 级绑定信息，不改变 MCP 协议本身。

## 17.4 Schema 兼容检查

Host 在绑定能力时 SHOULD 检查：

* Tool 是否存在；
* Tool 输入是否接受 SkillX 提供的参数；
* Tool 输出是否满足 SkillX 期望；
* Schema Digest 是否匹配；
* Tool 权限是否满足；
* Tool 副作用是否与声明一致。

Schema 不兼容时，Host MUST NOT静默启动工作流。

## 17.5 SkillX 依赖

```yaml
capabilities:
  normalize-city:
    kind: skillx
    ref: org.example/city-normalizer@^1.2.0#normalize
```

Host SHOULD 在发布或安装阶段解析版本范围，并在运行时使用锁定版本。

## 17.6 内置算子

```yaml
capabilities:
  json-transform:
    kind: host-operator
    ref: skillx.operator/json-transform@1
```

Host MUST 明确声明所支持的 Operator Profile。

## 17.7 能力句柄

运行时不得直接向执行器提供所有系统能力。

执行器只应获得清单中声明且 Host 授权的能力句柄：

```python
result = await context.capabilities.call(
    "weather-current",
    {"city": "成都"}
)
```

---

# 18. Environment

## 18.1 定义

```yaml
environments:
  python:
    kind: python-venv
    python: ">=3.11,<3.13"
    projectFile: pyproject.toml
    lockFile: uv.lock
```

## 18.2 环境类型

Core 推荐：

| `kind`        | 说明           |
| ------------- | ------------ |
| `python-venv` | Python 虚拟环境  |
| `process`     | Host 启动的独立进程 |
| `host`        | Host 内置执行环境  |
| `container`   | 容器环境         |
| `custom`      | 扩展环境         |

## 18.3 Python venv

```yaml
environments:
  python:
    kind: python-venv
    python: ">=3.11,<3.13"
    projectFile: pyproject.toml
    lockFile: uv.lock
    installMode: locked
```

规则：

* `lockFile` SHOULD 存在；
* 依赖安装 SHOULD 与任务执行分离；
* Host SHOULD 缓存按内容哈希构建的 venv；
* 包更新后 SHOULD 生成新环境；
* venv 只提供依赖隔离，不构成安全沙箱。

非可信 SkillX 不能只依赖 venv 进行安全隔离。

---

# 19. Python Callable Profile

## 19.1 Profile 标识

```text
skillx.python-callable/v1
```

## 19.2 入口格式

```yaml
runtime:
  protocol: skillx.python-callable/v1
  entry: executors/query.py:execute
```

入口格式：

```text
<relative-python-file>:<callable-name>
```

## 19.3 函数签名

执行函数可以为同步或异步函数：

```python
async def execute(
    request: SkillXRequest,
    context: SkillXContext,
) -> SkillXResult:
    ...
```

## 19.4 Request

概念结构：

```python
class SkillXRequest:
    invocation_id: str
    interface: str
    input: dict[str, object]
    idempotency_key: str | None
    locale: str | None
    deadline: datetime | None
```

## 19.5 Context

Context SHOULD 提供：

```python
class SkillXContext:
    capabilities: CapabilityClient
    artifacts: ArtifactClient
    effects: EffectClient
    events: EventEmitter
    secrets: SecretAccessor

    def check_cancelled(self) -> None:
        ...
```

执行器 MUST NOT从 Host 全局对象中绕过 Context 获取未授权能力。

## 19.6 返回值

```python
class SkillXResult:
    output: dict[str, object]
    artifacts: list[ArtifactReference]
    metrics: dict[str, int | float | str]
    warnings: list[SkillXWarning]
```

---

# 20. STDIO JSON Profile

## 20.1 Profile 标识

```text
skillx.stdio-json/v1
```

## 20.2 基本方式

Host 为每次 Invocation 启动一个进程。

Host：

1. 向标准输入写入 Invocation JSON；
2. 关闭标准输入；
3. 从标准输出读取 Result JSON；
4. 从标准错误读取日志；
5. 根据退出码判断进程状态。

## 20.3 Invocation

```json
{
  "protocolVersion": "skillx.runtime/v1",
  "type": "invoke",
  "invocationId": "inv_01H...",
  "skill": {
    "namespace": "org.example",
    "name": "weather-query",
    "version": "1.0.0"
  },
  "interface": "query",
  "input": {
    "city": "成都"
  },
  "context": {
    "locale": "zh-CN",
    "deadline": "2026-08-03T04:00:00Z",
    "idempotencyKey": "weather-20260803-chengdu"
  }
}
```

## 20.4 Result

成功：

```json
{
  "protocolVersion": "skillx.runtime/v1",
  "type": "result",
  "invocationId": "inv_01H...",
  "output": {
    "city": "成都",
    "temperature": 31,
    "condition": "多云"
  },
  "artifacts": [],
  "warnings": [],
  "metrics": {
    "durationMs": 328
  }
}
```

失败：

```json
{
  "protocolVersion": "skillx.runtime/v1",
  "type": "error",
  "invocationId": "inv_01H...",
  "error": {
    "code": "DEPENDENCY_UNAVAILABLE",
    "message": "天气服务暂时不可用",
    "retryable": true,
    "details": {
      "capability": "weather-current"
    }
  }
}
```

## 20.5 标准输出约束

标准输出 MUST 只包含协议结果。

普通日志 MUST 写入标准错误。

Host MUST 将标准输出中的非协议内容视为运行协议错误。

---

# 21. 执行生命周期

## 21.1 状态

Invocation 状态：

```text
created
validating
resolving
ready
running
waiting-input
waiting-confirmation
succeeded
failed
cancelled
timed-out
```

## 21.2 标准流程

```text
创建 Invocation
→ 校验输入
→ 解析接口
→ 解析依赖
→ 检查权限
→ 准备环境
→ 执行
→ 验证输出
→ 保存 Artifact
→ 完成
```

## 21.3 输入补充

只有 Agent Interface 或显式支持交互输入的扩展接口可以进入：

```text
waiting-input
```

普通 Direct Interface 缺少输入时 MUST 返回 `INVALID_INPUT`，不得自行猜测。

## 21.4 取消

Host SHOULD 支持取消。

执行器：

* SHOULD 定期检查取消状态；
* MUST 尽快停止尚未执行的副作用；
* MUST NOT将已完成的副作用伪装为已取消；
* 无法确定副作用是否完成时，MUST 返回 `UNKNOWN_OUTCOME`。

## 21.5 超时

超时后：

* Host SHOULD 发送取消信号；
* MUST 终止后续调度；
* MUST 保留已发生副作用记录；
* MUST NOT在结果未知时自动重试写操作。

---

# 22. 事件模型

Host MAY 支持流式事件。

标准事件：

| 事件                     | 说明          |
| ---------------------- | ----------- |
| `invocation.started`   | 开始执行        |
| `progress.updated`     | 进度变化        |
| `log.emitted`          | 结构化日志       |
| `capability.started`   | 开始调用能力      |
| `capability.completed` | 能力调用完成      |
| `artifact.created`     | 创建 Artifact |
| `effect.requested`     | 请求副作用授权     |
| `effect.authorized`    | 用户授权        |
| `effect.denied`        | 用户拒绝        |
| `warning.emitted`      | 非致命警告       |
| `invocation.completed` | 成功          |
| `invocation.failed`    | 失败          |

事件 MUST 包含：

```json
{
  "eventId": "evt_01H...",
  "invocationId": "inv_01H...",
  "type": "capability.started",
  "timestamp": "2026-08-03T03:00:00Z",
  "payload": {}
}
```

事件顺序 SHOULD 可通过单调递增序号恢复。

---

# 23. Artifact

## 23.1 Artifact 引用

```json
{
  "id": "art_01H...",
  "name": "weather-report.csv",
  "mediaType": "text/csv",
  "size": 1832,
  "digest": "sha256:...",
  "uri": "artifact://art_01H..."
}
```

## 23.2 Artifact 规则

Artifact：

* MUST 有稳定 ID；
* SHOULD 提供媒体类型；
* SHOULD 提供大小和哈希；
* MUST 受访问控制；
* MUST 与 Invocation 关联；
* MUST NOT在路径中泄露 Secret；
* SHOULD 保存来源接口和创建时间。

## 23.3 接口声明

```yaml
artifacts:
  allowedMediaTypes:
    - text/csv
    - application/json
  maxCount: 10
  maxTotalSizeBytes: 104857600
```

---

# 24. 权限模型

## 24.1 默认拒绝

未声明权限 MUST 被拒绝。

Host MAY 进一步缩小权限，但 MUST NOT静默扩大权限。

## 24.2 权限合并

实际权限为：

```text
包级权限
∩ 接口级权限
∩ 工作流授权
∩ 用户授权
∩ Host 安全策略
```

## 24.3 权限结构

```yaml
permissions:
  filesystem:
    read:
      - "${workspace}/inputs/**"
    write:
      - "${workspace}/outputs/**"

  network:
    connect:
      - host: api.example.org
        schemes:
          - https

  secrets:
    use:
      - WEATHER_API_KEY

  process:
    spawn:
      - python

  browser:
    domains:
      - forms.example.org
    actions:
      - navigate
      - read
      - fill
      - submit

  model:
    invoke: false
```

## 24.4 文件权限

文件权限 MUST 使用 Host 解析后的安全路径。

字符串前缀匹配不能作为唯一的目录逃逸防护。

## 24.5 网络权限

网络声明 SHOULD 包含：

* Host；
* Scheme；
* 可选端口；
* 可选 HTTP 方法。

DNS 解析结果变化时，Host SHOULD 防止访问本地或受保护网络。

## 24.6 Secret

SkillX 只能声明 Secret 名称：

```yaml
secrets:
  use:
    - WEATHER_API_KEY
```

SkillX 包 MUST NOT包含生产 Secret。

Secret：

* MUST 由 Host 在运行时注入；
* MUST 按接口最小化暴露；
* MUST 在日志中脱敏；
* SHOULD 支持轮换；
* SHOULD 记录使用审计。

## 24.7 模型权限

Direct Interface 的有效模型权限 MUST 为：

```yaml
model:
  invoke: false
```

即使包级权限允许模型，Direct Interface 也不得调用模型。

---

# 25. 副作用模型

## 25.1 标准 Effect 类型

| Effect              | 含义        |
| ------------------- | --------- |
| `data.read`         | 读取外部数据    |
| `data.write`        | 创建或修改数据   |
| `data.delete`       | 删除数据      |
| `file.write`        | 写入文件      |
| `message.send`      | 发送消息或邮件   |
| `form.submit`       | 提交外部表单    |
| `code.execute`      | 执行代码或命令   |
| `payment.commit`    | 支付或产生财务义务 |
| `permission.change` | 修改权限      |
| `credential.use`    | 使用身份凭据    |
| `external.other`    | 其他外部副作用   |

## 25.2 声明

```yaml
effects:
  - id: submit-form
    type: form.submit
    description: 向目标网站提交申请表
    confirmation: required
    idempotency: required
```

## 25.3 确认模式

| 值           | 含义                 |
| ----------- | ------------------ |
| `never`     | 无需确认，仅适用于低风险操作     |
| `required`  | 每次 Invocation 必须确认 |
| `policy`    | 由 Host 或组织策略决定     |
| `forbidden` | 当前接口禁止执行该副作用       |

以下 Effect 默认 SHOULD 使用 `required`：

* `form.submit`；
* `message.send`；
* `data.delete`；
* `payment.commit`；
* `permission.change`。

## 25.4 授权绑定

用户授权 MUST 绑定：

* Invocation ID；
* Effect ID；
* 目标对象；
* 实际提交数据摘要；
* 当前环境摘要；
* 有效时间。

示例：

```json
{
  "invocationId": "inv_01H...",
  "effectId": "submit-form",
  "target": "https://forms.example.org/apply",
  "payloadDigest": "sha256:...",
  "environmentDigest": "sha256:...",
  "expiresAt": "2026-08-03T04:30:00Z"
}
```

确认后，以下任一内容变化都 MUST 使授权失效：

* 提交字段；
* 上传文件；
* 目标 URL；
* 登录身份；
* 表单结构；
* Effect 类型；
* 提交按钮语义。

## 25.5 Effect 请求

执行器在执行需确认 Effect 前 MUST 请求授权。

未经授权不得先执行、后补确认。

## 25.6 未知结果

如果网络中断等原因导致无法确定副作用是否已完成，MUST 返回：

```text
UNKNOWN_OUTCOME
```

Host MUST NOT自动重试，除非存在可靠幂等键或查询确认机制。

---

# 26. 幂等性

## 26.1 声明

```yaml
execution:
  idempotency: supported
```

取值：

| 值           | 含义                     |
| ----------- | ---------------------- |
| `none`      | 不支持                    |
| `supported` | 提供 Idempotency Key 时支持 |
| `required`  | 调用必须提供 Idempotency Key |
| `inherent`  | 操作天然幂等                 |

## 26.2 Idempotency Key

Host SHOULD 为可重试调用提供稳定 Idempotency Key。

同一个 Key、接口和有效输入应代表同一逻辑操作。

执行器 MUST NOT将同一个 Key 用于不同提交内容。

## 26.3 有副作用接口

声明自动重试的写接口 SHOULD：

* 支持 Idempotency Key；
* 或提供执行前查询；
* 或提供事务；
* 或提供补偿机制。

---

# 27. 重试

## 27.1 声明

```yaml
execution:
  retry:
    maxAttempts: 3
    backoff:
      type: exponential
      initialMs: 500
      maxMs: 5000
    on:
      - RATE_LIMITED
      - DEPENDENCY_UNAVAILABLE
```

## 27.2 自动重试条件

以下错误 MAY 自动重试：

* `RATE_LIMITED`；
* `DEPENDENCY_UNAVAILABLE`；
* 临时网络错误；
* 明确可重试的读取操作。

以下错误 MUST NOT默认自动重试：

* `INVALID_INPUT`；
* `PERMISSION_DENIED`；
* `CONFIRMATION_DENIED`；
* `CONTRACT_VIOLATION`；
* `UNKNOWN_OUTCOME`；
* 已经可能产生副作用的操作。

## 27.3 Retry-After

错误 MAY 返回：

```json
{
  "retryAfterMs": 3000
}
```

Host SHOULD 尊重合理的 Retry-After。

---

# 28. 标准错误模型

## 28.1 结构

```json
{
  "code": "INVALID_INPUT",
  "message": "city 不能为空",
  "retryable": false,
  "details": {
    "path": "/city"
  },
  "retryAfterMs": null
}
```

## 28.2 标准错误码

| 错误码                       | 说明           |
| ------------------------- | ------------ |
| `INVALID_INPUT`           | 输入不符合 Schema |
| `UNSUPPORTED_INTERFACE`   | Host 不支持接口类型 |
| `MISSING_DEPENDENCY`      | 缺少依赖         |
| `CAPABILITY_NOT_FOUND`    | 能力不存在        |
| `CAPABILITY_INCOMPATIBLE` | 能力契约不兼容      |
| `PERMISSION_DENIED`       | 权限拒绝         |
| `AUTHENTICATION_FAILED`   | 身份认证失败       |
| `CONFIRMATION_REQUIRED`   | 需要确认         |
| `CONFIRMATION_DENIED`     | 用户拒绝         |
| `DEPENDENCY_UNAVAILABLE`  | 外部依赖不可用      |
| `RATE_LIMITED`            | 被限流          |
| `TIMEOUT`                 | 超时           |
| `CANCELLED`               | 已取消          |
| `CONTRACT_VIOLATION`      | 输出或运行契约不符合   |
| `ENVIRONMENT_DRIFT`       | 运行环境与契约不符    |
| `PAGE_DRIFT`              | 浏览器页面结构变化    |
| `EFFECT_FAILED`           | 副作用执行失败      |
| `UNKNOWN_OUTCOME`         | 无法确认副作用结果    |
| `INTERNAL_ERROR`          | 未分类内部错误      |

## 28.3 错误消息

公开错误消息：

* MUST 可供用户理解；
* MUST NOT包含 Secret；
* MUST NOT默认包含完整堆栈；
* SHOULD 包含可执行的恢复建议；
* SHOULD 将内部错误 ID 与详细日志关联。

---

# 29. Execution 配置

```yaml
execution:
  timeoutSeconds: 30
  maxConcurrency: 10
  idempotency: supported
  retry:
    maxAttempts: 2
    on:
      - DEPENDENCY_UNAVAILABLE
  cancellation:
    supported: true
```

## 29.1 超时

每个接口 MUST 声明或继承超时。

Host MAY 应用更短的上限。

## 29.2 并发

`maxConcurrency` 是接口声明的安全上限。

Host MAY 进一步限制。

## 29.3 资源限制

接口 MAY 声明：

```yaml
resources:
  cpu: "1"
  memoryBytes: 536870912
  temporaryStorageBytes: 1073741824
```

Host 不支持时可以拒绝安装或使用自身更严格限制。

---

# 30. 浏览器执行扩展

本章为推荐 Profile，不属于 SkillX Core 必需实现。

## 30.1 声明

```yaml
x-skillx-browser:
  pageContract: contracts/application-form.json
  driftPolicy:
    idChanged: confirm
    optionalFieldAdded: confirm
    requiredFieldAdded: stop
    fieldRemoved: stop
    targetChanged: stop
```

## 30.2 页面契约

```json
{
  "contractVersion": "1",
  "urlPattern": "https://forms.example.org/apply*",
  "pageIdentity": {
    "title": "申请表",
    "requiredText": [
      "提交申请"
    ]
  },
  "fields": [
    {
      "id": "name",
      "required": true,
      "type": "text",
      "locator": {
        "role": "textbox",
        "label": "姓名"
      }
    }
  ],
  "submit": {
    "role": "button",
    "name": "提交申请"
  }
}
```

## 30.3 页面漂移

以下情况 MUST 暂停自动提交：

* 新增必填字段；
* 字段含义变化；
* 原字段消失；
* 登录身份变化；
* 目标域名变化；
* 提交动作语义变化；
* 出现新的授权协议；
* 页面无法满足成功判定条件。

仅元素位置或 DOM 层级变化，但语义定位仍然匹配时，可以继续执行。

## 30.4 LLM 恢复

页面漂移后，Host MAY 调用局部 LLM 或 Agent 辅助重新定位元素。

但：

* MUST 先暂停工作流；
* SHOULD 向用户展示变化；
* 恢复结果 MUST 经过页面契约校验；
* 恢复结果 MUST NOT静默覆盖已发布版本；
* SHOULD 生成新的工作流或 SkillX 版本草稿。

---

# 31. 安全要求

## 31.1 不信任包内容

Host MUST 将以下内容视为不可信：

* SkillX 包；
* `SKILL.md`；
* Prompt；
* 脚本；
* MCP Tool 描述；
* 外部网页；
* 外部文档；
* 模型输出。

自然语言说明不能覆盖 Host 安全策略。

## 31.2 Prompt Injection

外部内容中出现的指令：

* MUST 被视为业务数据；
* MUST NOT自动改变接口权限；
* MUST NOT扩大 Agent Tool Allowlist；
* MUST NOT绕过确认；
* MUST NOT读取未授权 Secret。

## 31.3 能力描述

MCP 规范也强调工具可能表示任意代码执行路径，工具描述和注释不应在未经信任的情况下直接用于安全决策，用户应保留授权控制。

## 31.4 安装期和运行期分离

依赖安装通常能够执行构建脚本，因此：

* SHOULD 在受控环境完成；
* SHOULD 记录依赖锁文件；
* SHOULD 扫描供应链风险；
* MUST NOT在普通 Invocation 中隐式安装任意依赖。

## 31.5 非可信代码

运行非可信 Direct Executor 时，Host SHOULD 使用：

* 独立系统用户；
* 容器；
* Namespace；
* seccomp；
* 只读文件系统；
* 网络白名单；
* 资源限制。

venv 不能代替安全沙箱。

## 31.6 日志

日志：

* MUST 脱敏；
* MUST 不包含完整凭据；
* SHOULD 具有结构化级别；
* SHOULD 能关联 Invocation；
* SHOULD 区分用户可见日志和内部诊断日志。

---

# 32. 版本与兼容性

## 32.1 两类版本

SkillX 存在两个独立版本：

1. `apiVersion`：清单和规范版本；
2. `metadata.version`：具体 SkillX 包版本。

## 32.2 Major 版本变化

以下变化 MUST 提升 Major：

* 删除接口；
* 重命名接口；
* 新增必填输入且无默认处理；
* 缩小输入允许范围；
* 删除输出字段；
* 修改输出字段类型；
* 修改字段语义；
* 增加新的强制副作用；
* 扩大危险权限；
* 改变幂等性保证；
* 将 Direct Interface 改为 LLM 或 Agent；
* 修改标准错误的关键语义。

## 32.3 Minor 版本变化

以下变化通常可以提升 Minor：

* 新增可选接口；
* 新增可选输入字段；
* 新增向后兼容输出字段；
* 新增示例；
* 增加可选能力；
* 性能优化；
* 增加新的非默认执行模式。

## 32.4 Patch 版本变化

以下变化通常可以提升 Patch：

* 修复实现错误；
* 修正文档；
* 修复不影响契约的性能问题；
* 更新测试；
* 修复安全问题但不改变公开接口。

## 32.5 权限变化

权限扩大至少 SHOULD 提升 Minor。

涉及以下权限扩大时 SHOULD 提升 Major：

* 新增写权限；
* 新增删除权限；
* 新增消息发送；
* 新增表单提交；
* 新增付款；
* 新增权限修改；
* 新增广泛文件或网络访问。

## 32.6 锁定版本

持久化 Workflow 发布时 MUST 锁定：

* SkillX 包版本；
* 接口名称；
* Schema Digest；
* MCP 能力绑定；
* Runtime Profile；
* 权限快照。

生产 Workflow 不应始终自动使用 SkillX 最新版本。

---

# 33. Compatibility

```yaml
compatibility:
  host: ">=0.1.0 <2.0.0"
  agentSkills: ">=1"
  requiredProfiles:
    - skillx.python-callable/v1
    - skillx.mcp-binding/v1
```

Host 安装包前 SHOULD 检查：

* `apiVersion`；
* Host 版本；
* Runtime Profile；
* 环境；
* 操作系统；
* 能力类型；
  -扩展字段。

---

# 34. 扩展机制

## 34.1 扩展字段

未知扩展字段 MUST 以 `x-` 开头。

示例：

```yaml
x-example-company-policy:
  dataClassification: internal
```

## 34.2 扩展规则

扩展：

* MUST NOT改变 Core 字段语义；
* MUST NOT降低 Core 安全要求；
* SHOULD 使用组织唯一前缀；
* SHOULD 提供独立文档；
* SHOULD 明确是否影响执行。

Host 可以忽略不影响正确性和安全性的未知扩展。

如果未知扩展声明为必需，Host MUST 拒绝执行：

```yaml
compatibility:
  requiredExtensions:
    - x-example-browser-contract/v1
```

---

# 35. 打包与分发

## 35.1 目录形式

开发环境中的规范形式是目录。

## 35.2 `.skillx` 归档

Registry MAY 使用 `.skillx` ZIP 归档：

```text
org.example.weather-query-1.0.0.skillx
```

归档根目录直接包含：

```text
SKILL.md
skillx.yaml
schemas/
executors/
...
```

不应额外嵌套随机目录。

## 35.3 安全检查

Host 解压前 MUST 检查：

* 绝对路径；
* `..` 路径穿越；
* 重复规范化路径；
* 符号链接逃逸；
* 超大解压比例；
* 文件数量限制；
* 总大小限制。

## 35.4 内容摘要

Registry SHOULD 为包计算 SHA-256 摘要：

```text
sha256:<hex>
```

工作流版本 SHOULD 保存包摘要。

## 35.5 签名

数字签名不属于 Core v0.1 强制范围。

生产 Registry SHOULD 支持：

* 发布者身份；
* 包摘要签名；
* 签名验证；
* 撤销状态；
* 来源证明。

---

# 36. `skillx.lock`

## 36.1 作用

`skillx.lock` 保存已经解析的依赖版本和摘要。

示例：

```yaml
lockVersion: 1

package:
  ref: org.example/weather-query@1.0.0
  digest: sha256:...

capabilities:
  weather-current:
    kind: mcp-tool
    provider: weather-service
    toolName: weather.get_current
    schemaDigest: sha256:...

skillx:
  city-normalizer:
    ref: org.example/city-normalizer@1.3.2#normalize
    digest: sha256:...
```

## 36.2 规则

* `skillx.yaml` 是作者意图；
* `skillx.lock` 是一次解析结果；
* Lock 文件 MUST NOT扩大 Manifest 权限；
* Host MAY 重新生成 Lock；
* 生产发布 SHOULD 使用 Lock；
* Lock 变化 SHOULD 触发兼容检查。

---

# 37. 测试要求

## 37.1 契约测试

每个可执行接口 MUST 测试：

* 有效输入；
* 缺少必填字段；
* 错误字段类型；
* 未声明字段；
* 有效输出；
* 输出 Schema 违规。

## 37.2 Direct 测试

Direct Interface MUST 验证：

* 不调用模型；
* 不使用未声明能力；
* 超时有效；
* 取消有效或明确声明不支持；
* 权限不足时失败；
* Secret 不进入日志。

## 37.3 Effect 测试

有副作用接口 MUST 测试：

* 未授权时不能执行；
* 授权内容变化后失效；
* 用户拒绝后停止；
* 重试不会重复提交；
* 结果未知时返回 `UNKNOWN_OUTCOME`。

## 37.4 LLM 测试

LLM Interface SHOULD 测试：

* 输出 Schema 通过率；
* 非法输出恢复；
* Prompt Injection 样例；
* 最大 Token；
* 不调用工具；
* 敏感字段不泄露。

## 37.5 Agent 测试

Agent Interface SHOULD 测试：

* 最大步骤数；
* Tool Allowlist；
* 无权限工具调用拒绝；
* 输出契约；
* 任务失败轨迹；
* Token 上限；
* 取消行为。

## 37.6 示例一致性

`examples/` 中的所有输入输出 SHOULD 通过对应 Schema。

---

# 38. 符合性等级

## 38.1 Package Conformance

### SX-GUIDE

要求：

* 有效 `SKILL.md`；
* 有效 `skillx.yaml`；
* Guide 一致；
* 可以没有可执行接口。

SX-GUIDE MUST NOT宣称可被 Workflow 直接执行。

### SX-EXECUTABLE

要求：

* 满足 SX-GUIDE；
* 至少一个接口；
* 输入输出 Schema；
* 运行入口；
* 权限和 Effect 声明；
* 契约测试。

### SX-DIRECT

要求：

* 满足 SX-EXECUTABLE；
* 至少一个 Direct Interface；
* 传递依赖不调用模型；
* Direct 契约测试通过。

### SX-AGENTIC

要求：

* 满足 SX-EXECUTABLE；
* Agent 限制、Tool Allowlist 和输出契约完整。

## 38.2 Host Conformance

### Host-Core

支持：

* 发现包；
* 读取 `SKILL.md`；
* 解析 Manifest；
* 校验 Schema；
* 展示接口。

### Host-Direct

额外支持：

* Direct Interface；
* 至少一个 Direct Runtime Profile；
* 输入输出验证；
* 权限控制；
* Artifact；
* 超时和错误。

### Host-LLM

额外支持：

* LLM Interface；
* Schema 约束输出；
* Token 限制；
  -模型 Profile。

### Host-Agent

额外支持：

* Agent Interface；
* Tool Allowlist；
* 最大步骤；
* 轨迹和取消。

### Host-Effect

额外支持：

* Effect 声明；
* 用户确认；
* 授权绑定；
* Idempotency Key；
* `UNKNOWN_OUTCOME`。

Host MUST 公布其符合性 Profile。

---

# 39. 验证流程

推荐验证命令：

```text
skillx validate .
skillx inspect .
skillx test .
skillx pack .
```

## 39.1 `validate`

检查：

1. 目录结构；
2. YAML 语法；
3. Manifest Schema；
4. 名称一致性；
5. 路径安全；
6. Schema 有效性；
7. 接口引用；
8. 环境引用；
9. 能力引用；
10. 权限与运行模式矛盾；
11. Effect 和确认策略。

## 39.2 `inspect`

输出：

* 包引用；
* 接口；
* 执行模式；
* 输入字段；
* 输出字段；
* 权限；
* 副作用；
* 依赖；
* LLM 使用情况；
* 可否零 LLM 执行。

## 39.3 `test`

执行：

* 契约测试；
* Runtime 测试；
* 权限测试；
* Effect 测试；
* 示例测试。

## 39.4 `pack`

打包前 MUST：

* 通过校验；
* 排除 venv、缓存和临时文件；
* 生成内容清单；
* 计算摘要；
* 记录版本。

---

# 40. 从 Skill 迁移到 SkillX

## 40.1 第一步：保留原 Skill

不得删除：

```text
SKILL.md
scripts/
references/
assets/
```

## 40.2 第二步：识别输入输出

将自然语言描述中的变量转成 JSON Schema。

例如：

```text
“查询某个城市的天气”
```

转换为：

```json
{
  "type": "object",
  "required": ["city"],
  "properties": {
    "city": {
      "type": "string",
      "minLength": 1
    }
  },
  "additionalProperties": false
}
```

## 40.3 第三步：分类步骤

| Skill 步骤  | SkillX 转换                       |
| --------- | ------------------------------- |
| 固定字段转换    | Direct Executor 或 Host Operator |
| 固定 API 调用 | MCP Tool                        |
| 固定脚本      | Direct Interface                |
| 单次分类或抽取   | LLM Interface                   |
| 动态工具选择    | Agent Interface                 |
| 人工判断      | Workflow Approval 节点            |
| 无法结构化     | 保留在 Guide                       |

## 40.4 第四步：定义能力依赖

不要将外部系统凭据和调用代码直接隐藏在 Prompt 中。

外部系统能力 SHOULD 抽象为：

* MCP Tool；
* 受控 Capability；
* 明确的 Direct Runtime 依赖。

## 40.5 第五步：声明副作用

识别：

* 是否写数据；
* 是否提交表单；
* 是否发送消息；
* 是否删除；
* 是否付款；
* 是否修改权限。

## 40.6 第六步：增加测试

至少增加：

* 正常输入；
* 边界输入；
* 失败输入；
* 输出契约；
* 权限；
* 副作用确认。

## 40.7 转换结果

转换可能得到：

### Guide-only

仍然只能由 Agent 阅读执行。

### Direct SkillX

完全不需要模型。

### LLM SkillX

只在单个结构化节点调用模型。

### Agent SkillX

保留受限 Agent Loop。

一个 Skill 不应为了宣称“零 LLM”而将原本需要语义推理的过程伪装为 Direct。

---

# 41. 完整示例：天气查询

## 41.1 目录

```text
weather-query/
├── SKILL.md
├── skillx.yaml
├── schemas/
│   ├── query.input.schema.json
│   └── query.output.schema.json
├── executors/
│   └── query.py
├── examples/
│   ├── query.input.json
│   └── query.output.json
└── tests/
    └── test_query.py
```

## 41.2 `skillx.yaml`

```yaml
apiVersion: skillx.dev/v1alpha1
kind: SkillX

metadata:
  namespace: org.example
  name: weather-query
  version: 1.0.0
  displayName: 天气查询
  description: 根据城市名称查询当前天气
  license: Apache-2.0
  keywords:
    - weather
    - forecast

spec:
  guide:
    entry: SKILL.md

  compatibility:
    host: ">=0.1.0"
    requiredProfiles:
      - skillx.python-callable/v1
      - skillx.mcp-binding/v1

  defaultInterface: query

  interfaces:
    query:
      kind: direct
      title: 查询当前天气
      description: 查询指定城市当前天气
      inputSchema: schemas/query.input.schema.json
      outputSchema: schemas/query.output.schema.json

      runtime:
        environment: python
        protocol: skillx.python-callable/v1
        entry: executors/query.py:execute

      capabilities:
        - weather-current

      execution:
        timeoutSeconds: 30
        maxConcurrency: 20
        idempotency: inherent
        retry:
          maxAttempts: 2
          on:
            - DEPENDENCY_UNAVAILABLE
            - RATE_LIMITED

      permissions:
        model:
          invoke: false

      effects:
        - id: read-weather
          type: data.read
          confirmation: never

  capabilities:
    weather-current:
      kind: mcp-tool
      required: true
      selector:
        server: weather
        toolName: weather.get_current
      contract:
        inputSchema: schemas/capabilities/weather.input.schema.json
        outputSchema: schemas/capabilities/weather.output.schema.json

  environments:
    python:
      kind: python-venv
      python: ">=3.11,<3.13"
      projectFile: pyproject.toml
      lockFile: uv.lock

  permissions:
    network:
      connect:
        - host: weather.example.org
          schemes:
            - https
```

## 41.3 输入 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": [
    "city"
  ],
  "properties": {
    "city": {
      "type": "string",
      "minLength": 1,
      "maxLength": 100,
      "description": "需要查询天气的城市"
    },
    "units": {
      "type": "string",
      "enum": [
        "metric",
        "imperial"
      ],
      "default": "metric"
    }
  },
  "additionalProperties": false
}
```

## 41.4 输出 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": [
    "city",
    "temperature",
    "condition",
    "observedAt"
  ],
  "properties": {
    "city": {
      "type": "string"
    },
    "temperature": {
      "type": "number"
    },
    "condition": {
      "type": "string"
    },
    "observedAt": {
      "type": "string",
      "format": "date-time"
    },
    "source": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

## 41.5 Python Executor

```python
from __future__ import annotations

from typing import Any


async def execute(request: Any, context: Any) -> dict[str, Any]:
    city = request.input["city"]
    units = request.input.get("units", "metric")

    weather = await context.capabilities.call(
        "weather-current",
        {
            "city": city,
            "units": units,
        },
    )

    return {
        "output": {
            "city": weather["city"],
            "temperature": weather["temperature"],
            "condition": weather["condition"],
            "observedAt": weather["observedAt"],
            "source": weather.get("source", "weather-current"),
        },
        "artifacts": [],
        "warnings": [],
        "metrics": {},
    }
```

该接口：

* 不调用 LLM；
* 不运行 Agent；
* 不动态选择 Tool；
* 由工作流直接调用；
* 可以在没有模型配置的 Host 中运行。

---

# 42. 完整示例：浏览器表单提交

```yaml
apiVersion: skillx.dev/v1alpha1
kind: SkillX

metadata:
  namespace: org.example
  name: application-form-submit
  version: 1.0.0
  displayName: 申请表提交
  description: 在目标网站填写并提交申请表
  license: Apache-2.0

spec:
  guide:
    entry: SKILL.md

  defaultInterface: submit

  interfaces:
    submit:
      kind: direct
      description: 填写申请表，确认后提交
      inputSchema: schemas/submit.input.schema.json
      outputSchema: schemas/submit.output.schema.json

      runtime:
        environment: python
        protocol: skillx.python-callable/v1
        entry: executors/submit.py:execute

      capabilities:
        - browser-session

      execution:
        timeoutSeconds: 300
        idempotency: required
        retry:
          maxAttempts: 1

      effects:
        - id: submit-application
          type: form.submit
          description: 向目标系统提交申请
          confirmation: required
          idempotency: required

      permissions:
        browser:
          domains:
            - forms.example.org
          actions:
            - navigate
            - read
            - fill
            - upload
            - submit
        model:
          invoke: false

      x-skillx-browser:
        pageContract: contracts/application-form.json
        driftPolicy:
          idChanged: confirm
          optionalFieldAdded: confirm
          requiredFieldAdded: stop
          fieldRemoved: stop
          loginIdentityChanged: stop
          targetChanged: stop

  capabilities:
    browser-session:
      kind: browser
      required: true

  environments:
    python:
      kind: python-venv
      python: ">=3.11,<3.13"
      projectFile: pyproject.toml
      lockFile: uv.lock
```

执行过程：

```text
校验用户输入
→ 打开目标页面
→ 验证页面契约
→ 填写字段
→ 生成提交数据摘要
→ 向用户展示待提交内容
→ 请求 Effect 授权
→ 用户确认
→ 再次检查页面和数据摘要
→ 自动提交
→ 验证成功结果
→ 保存业务编号和截图
```

如果元素 ID 变化或新增输入框：

```text
检测 Page Drift
→ 暂停执行
→ 用户确认或局部 Agent 分析
→ 生成修订建议
```

系统不得在用户不知情的情况下继续提交。

---

# 43. Manifest 完整模板

```yaml
apiVersion: skillx.dev/v1alpha1
kind: SkillX

metadata:
  namespace: org.example
  name: example-skill
  version: 0.1.0
  displayName: 示例 SkillX
  description: 描述能力做什么以及何时使用
  license: Apache-2.0
  authors:
    - name: Example Team
      email: team@example.org
  homepage: https://example.org
  repository: https://example.org/repository
  keywords: []
  labels: {}

spec:
  guide:
    entry: SKILL.md

  compatibility:
    host: ">=0.1.0"
    agentSkills: ">=1"
    requiredProfiles: []
    requiredExtensions: []

  defaultInterface: default

  interfaces:
    default:
      kind: direct
      title: 默认接口
      description: 接口说明

      inputSchema: schemas/input.schema.json
      outputSchema: schemas/output.schema.json
      uiSchema: schemas/ui.schema.json

      runtime:
        environment: python
        protocol: skillx.python-callable/v1
        entry: executors/default.py:execute

      capabilities: []

      execution:
        timeoutSeconds: 60
        maxConcurrency: 1
        idempotency: none
        cancellation:
          supported: true
        retry:
          maxAttempts: 1
          on: []

      permissions:
        filesystem:
          read: []
          write: []
        network:
          connect: []
        secrets:
          use: []
        process:
          spawn: []
        browser:
          domains: []
          actions: []
        model:
          invoke: false

      effects: []

      artifacts:
        allowedMediaTypes: []
        maxCount: 0
        maxTotalSizeBytes: 0

  capabilities: {}

  environments:
    python:
      kind: python-venv
      python: ">=3.11,<3.13"
      projectFile: pyproject.toml
      lockFile: uv.lock
      installMode: locked

  permissions: {}

  policies:
    redactLogs: true
    retainInvocationDays: 30
```

---

# 44. 开放问题

以下问题计划在后续版本中继续规范：

1. SkillX Registry API；
2. 包签名和供应链证明；
3. 工作流子图接口；
4. 多轮交互协议；
5. 长任务持久化句柄；
6. 分布式 Artifact 协议；
7. 通用浏览器页面契约；
8. SkillX over MCP；
9. WASM Runtime Profile；
10. 容器 Runtime Profile；
11. 标准 UI Schema；
12. 能力 Schema 兼容算法；
13. 事务和补偿操作；
14. 模型评测和输出质量断言。

---

# 45. 核心要求摘要

一个能够被持久化工作流直接调用的 SkillX，至少必须满足：

1. 包含 `SKILL.md`；
2. 包含 `skillx.yaml`；
3. 包含至少一个可执行接口；
4. 声明接口类型；
5. 声明输入 JSON Schema；
6. 声明输出 JSON Schema；
7. 声明运行入口；
8. 声明能力依赖；
9. 声明权限；
10. 声明副作用；
11. 声明超时和幂等性；
12. 通过契约测试；
13. 固定版本后才能被生产工作流引用；
14. Direct Interface 不得调用 LLM；
15. 有副作用的操作必须遵循确认和授权绑定机制。

SkillX 的最终目标是让同一个能力同时具备：

```text
对 Agent 可理解
对工作流可调用
对平台可验证
对用户可审计
对安全策略可约束
```
