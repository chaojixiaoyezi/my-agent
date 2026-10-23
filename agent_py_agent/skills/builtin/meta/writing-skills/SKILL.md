---
name: writing-skills
description: 按行为风险编写和验收原生 Skill，保持产物类型并验证真实加载与使用结果。
when_to_use: 用户要求新增、修改普通 Skill，或创建引用 Skill 的原生 Workflow 时。
tags: Skill编写, 原生工作流, 风险分级, 验收, create skill, author skill
scope: builtin
risk_level: low
---

# 编写原生 Skill

保持用户要求的对象，以足够而最小的流程完成编写、正式加载和代表性验收。方法文档不能替代缺失的运行能力。

## 产物与入口

- **Skill**：`SKILL.md` 与确有需要的脚本、模板或参考资源，提供可复用方法。
- **Workflow**：正式的输入、步骤、输出定义，引用 Skill 或脚本来组织执行。
- **Plugin**：增加运行时工具或服务能力，不能靠写方法文档模拟安装或授权。
- **Capability Pack**：分发容器，不是普通 Skill 或 Workflow 的默认存储形式。

用户明确要原生 Skill/Workflow 时走原生创作，不自动打包。要求 Workflow + Skill 时交付两个对象及准确的 `skill_ids` 绑定。只有用户明确要求分发或安装，才进入能力包构建/安装分支；创建不等于授权安装。

从本轮 `self_map`、`skill_search`、`tool_search` 的真实结果确认已有对象、当前格式、授权普通 Skill 根（如 `.agents/skills/<name>/SKILL.md`）和工具参数。未知时不猜旧路径，不把包安装路径当工作目录。Skill 搜索只读，不存在通用的 `skill_create`。

## 按风险选择流程

### Tier 1：简单任务，普通 Skill（Fast Path）
目标路径为工作区 `.agents/skills/<name>/SKILL.md` 或用户目录 `<owner_home>/skills/<name>/SKILL.md`。1-2 步直接编写 `frontmatter` 与正文并做一次本地代表性验收；不调用 `create_subagents`、不调用 `package_build`，不强制多子代理失败基线、多阶段确认/审查或大型计划。

### Tier 2：含脚本、外部接口或复杂业务
补齐资源、依赖、真实输入与结果验证（可用 `bind_skill` 绑定并先跑 `dry_run` / `preview` 核对结构，再跑真实输入）；按实际边界增加反例，不为形式制造失败实验。

### Tier 3：高风险，涉及权限、关键业务或长期运行行为
保留针对该风险的更强测试、必要独立评审和用户授权，不得绕过适用的机器审批。已有有效授权不重复索取，新风险不能借原授权扩张。

## 编写

使用授权文件工具写入已确认的普通 Skill 根。按当前正式 frontmatter 规范填写 `name`、`description`、`when_to_use`、`tags`、`scope`、`risk_level`；`name` 使用加载器接受的名称。描述短，触发条件写场景，正文写可执行步骤。

长领域材料放 `references/`，已有脚本和模板用精确资源引用复用。只创建确实需要的文件，不制造空目录、重复副本或额外报告。不得修改人格、权限配置、安装记录或运行账本来模拟激活。

## 原生验收

通过正式加载器检查 frontmatter、资源路径和依赖；由同一 SkillsService 发现，以稳定引用读回并核对正文。新对象进入受控新快照/工作片后，再验证当前版本。

普通 Skill 至少验证：可解析、可发现、读回正确，代表性输入得到符合输入/输出约定的结果。单独的文件存在或解析通过不够。旧快照无结果不等于文件不存在；若尚不能刷新，明确哪一步未验证以及需要在哪个新工作片复核。

版本、输入和观察点不变时复用有效证据；发现问题或修改后才补相关验证。简单任务充分验证后结束，不无限改写方法本身。

## Workflow 绑定与运行

先读取正式 workflows schema，保存输入、步骤和输出定义。`skill_ids` 使用同一解析器认可的精确稳定引用，不自行剥掉前缀，也不把包 stable_id 当普通 Skill ID。

绑定后读回并运行一个受控样本，确认 `run_inputs`、`step_inputs` 到达真实分析器，必需 Skill 可读且版本正确，业务输出满足契约。缺分析器必须以 `ANALYZER_UNAVAILABLE` 失败；缺必需 Skill 必须以 `SKILL_NOT_FOUND` 失败，不能用摘要、空结果或预览伪装成功。

preview 不等于执行成功；文件存在、tool ok 或单独的 `status=succeeded` 也不证明业务分析完成。检查实际分析行为与输出。

暂停时保留准确的 `workflow_run_id` 和 patch 合同；恢复后，下游必须引用被确认步骤的真实输出，不猜初始输入已被覆盖。

## 交付

说明对象类型、稳定引用或文件位置、Workflow 绑定、实际执行的验证及未验证部分。缺加载器发现、分析回调或依赖时如实报告缺口，不用文案替代底座能力。目标满足即交付，不附加无关学习、评审或持续目标。
