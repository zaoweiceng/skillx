# Skill 管理与 SkillX 转换

在侧栏的 **Skills** 页面添加、编辑、停用或删除 Skill。界面沿用工作流的卡片、搜索和筛选样式；设置入口位于侧栏底部，与主题按钮并排，只显示图标。

可以手动填写标识、名称、用途与指令，或读取本地 SKILL.md；有脚本、参考文件的 Skill 应通过 **导入目录** 保存整个资源包。路径指向运行服务所在电脑。导入最多 200 个文件、20 MB，拒绝符号链接，忽略依赖与缓存目录。SKILL.md 限制 128 KB。

## 转换方式

- **已有 SkillX 包**：校验 `skillx.yaml`、接口 Schema 和资源路径，复用接口及声明的依赖，发布平台专属的不可变版本。
- **直接执行脚本**：支持当前 Host 的 Python callable 协议 `skillx.python-callable/v1`。脚本需提供 `execute(request, context)`，从 `request.input` 读取输入，返回符合输出契约的对象。填写输入输出 JSON Schema 和至少两组不同输入的预期输出；平台通过现有 SkillX 子进程执行验证，全部符合契约后才发布。普通命令行脚本需要先适配该入口，不能仅改名就声称已转换。
- **语义 Agent 接口**：提供输入输出契约，默认接收 `task`、可选 `context`，返回 `answer`。完整指令与 Markdown 参考文件封装在不可变接口内部，只在调用时交给内部 Agent。该方式仍调用模型，并不授予额外的脚本、联网或工具权限；需要工具依赖的 Skill 应导入显式声明依赖的 SkillX 包，或适配为直接接口。

编辑已经转换的 SkillX 时，转换表单读取已发布版本的脚本、契约和验证样例。更新会发布新版本，历史版本不覆盖。

## Agent 加载规则

启用的原始 Skill 提供指令及资源根路径。成功转换的 Skill 只提供 SkillX 公共接口契约，原始 SKILL.md 不进入外层 Agent 上下文。外层任务通过统一能力目录调用 SkillX，已有契约满足任务时优先调用。任务目录已携带的契约不会在 AgentHost 中重复注入。

SkillX 内部 Agent 仅加载该版本的封装提示词及已绑定的依赖，不再注入全局 Skill 列表，以保持版本固定并避免循环调用自身。

修改原始 Skill 内容、名称或用途会使转换失效，需要重新转换；停用和删除立即从后续 Agent 的发现列表中移除。已绑定历史版本的工作流继续使用该不可变版本。新迁移为 `0009_managed_skills`，正常项目启动会自动执行。

API：`GET/POST /api/skills`、`POST /api/skills/import`、`GET/PUT/DELETE /api/skills/{id}`、`POST /api/skills/{id}/convert`。转换请求携带 `expectedRevision`，拒绝根据旧表单转换已更新的源内容。

## 从执行过程提炼可复用片段

后台固化会检查本次成功执行的证据，不默认复制整项任务。学习提案分别返回 `skills`（通用指令）、`skillx`（可验证的执行契约）和可选 `workflow`（步骤固定的流程）。Skill 和 SkillX 可以独立发布并出现在 Skills 管理中，不会自动包装成直通工作流；没有可复用内容时跳过。

每个 Skill 声明适用条件并引用真实成功调用。SkillX 仍需通过原任务证据与不同输入的执行断言。工作流声明 `workflowScope` 和 `workflowSourceInvocationIds`，说明片段边界并排除一次性结论；图级验证继续检查不同输入的实际结果。发布结果分别记录 Skill、SkillX 和工作流片段，前端据此展示固化产物。

后台使用独立于任务执行会话的专业 Agent：

1. **片段提炼 Agent** 读取执行证据，只识别复用边界、产物类型和依赖，不生成实现。
2. **Skill 整理 Agent** 按分配片段生成操作指令与适用条件。
3. **SkillX 构建 Agent** 按分配片段生成输入输出契约、实现与不同输入的验证样例，优先使用直接脚本。后端实际执行验证，不能由 Agent 自报验证成功。
4. **工作流编排 Agent** 只组合分配的固定流程。接收已验证 SkillX 的公共契约，不接收其源码或其他 Skill 正文；依赖被跳过或验证失败时不继续编排。

四类中文提示词独立保存于 `backend/app/prompts/learning/`，对应四种输出契约。没有相应片段时不调用对应专家。每次调用使用独立会话、运行 ID 和阶段记录，不继承任务 Agent 上下文，也不注入全局 Skill 指令。专业 Agent 只接收所属片段的调用证据，发布权限、执行验证和修复授权校验由后端控制。

`LearningJob.result.agentStages` 持久化阶段状态、产物、耗时、Token 和验证证明。Worker 重启后复用输入与提示词未变的已完成阶段；验证失败将反馈交给对应专家修订。已验证包只有内容摘要仍匹配时才复用。界面可展开查看各阶段产物，后台调用审计以 `learning.agent.audit` 记录，与前台任务模型调用区分。
