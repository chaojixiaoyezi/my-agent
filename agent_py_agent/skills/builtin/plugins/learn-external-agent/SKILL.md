---
name: learn-external-agent
description: 学习外部方法，按请求交付原生 Skill、Workflow 或经授权的分发包。
when_to_use: 用户要求学习外部 agent 或开源项目的方法时；明确的原生创作请求保持原生产物。
tags: 学外部agent, 原生Skill, 工作流, 能力包, 插件, learnpack, 许可证
scope: builtin
risk_level: medium
tools_required: [skill_search, read_file, list_files]
---

# 学外部 agent

学习方法、创作原生对象和分发安装是不同目标。先保持用户要求的对象，再选择必要工具和验证。

## 先确认产物

- 用户明确要原生 Skill（如 `.agents/skills/<skill-name>/SKILL.md`）或原生 Workflow，就按 `writing-skills` 使用正式创作与加载入口，不自动打包。要求 Workflow + Skill 时交付两个对象及准确绑定，不能用一个能力包代替。
- Skill 提供方法与必要资源；Workflow 组织输入、步骤和输出；Plugin 增加运行时工具或服务；Capability Pack 是分发容器。
- 只要求研究、解释或诊断时做只读学习，不自行创建、安装或部署。只有用户明确要求分发或安装，才进入下方能力包流程；分发也不自动授权安装。
- 简单任务不强制多子代理失败基线、多阶段确认或评审。高风险内容涉及权限、关键业务或长期运行行为时，保留相称验证、用户授权和适用的机器审批。

按当前目录和工具回执发现普通 Skill 根、网络读取、Workflow 或打包入口，不猜创建 API，不把工具可用误认为业务授权。

## 获取来源和核对许可

在授权任务工作区只读获取来源，记录真实地址、版本或提交号。跨轮学习或用户要求保留过程时，用 `learnpack-notes.md` 记来源、许可证、学到的方法、组织方式与实际进度；续做先读已有笔记，不重复学习。不为只读短答额外写文件。

复制、改编或分发外部内容前核对 LICENSE。只收符合本流程的宽松许可证：MIT、Apache-2.0、BSD、ISC、0BSD、Unlicense、CC0、CC-BY，并保留原文、署名与所需声明。

未写许可证、GPL/AGPL/LGPL、“仅供非商业”或自定义条款：停止收录原文和代码，说明限制；可用自己的话写学习笔记。不要凭“开源”推断有权复制。

阅读方法、流程、模板、例子和检查项，理解适用边界，不只复制步骤。原生 Skill/Workflow 按 `writing-skills` 做正式发现、稳定引用读回和代表性输入验收；preview 不是执行成功，缺分析器或必需 Skill 必须失败。

## 仅在分发或安装时进入能力包流程

1. **确认范围**：在此分支内，领域方法默认做能力包；离线自带的小工具可放 `tools/` 或 `checks/`。只有确需联网、账号或常驻服务的运行能力才考虑插件，并遵循 `write-my-agent-plugin` 的当前合同，不自行扩大授权。
2. **查同领域**：写包之前用 `skill_search` 查已有包；只能并入 `made_by_me=true` 的包。发现同领域对象时，先问用户并入还是独立成包，回答不等于安装确认。出现 `PACKAGE_BUILD_SAME_DOMAIN` 也先处理该选择，见[合并](references/merge.md)。
3. **写包**：目录在授权任务工作区，结构见[包的结构](references/pack-layout.md)。入口 `CAPABILITY.md` 写适用与不适用场景、用法和方法目录；`PROVENANCE.md` 写来源、提交号、许可证和改动。原始 LICENSE 随许可要求保留，不放密钥、口令或个人信息。
4. **声明语言**：写 `declaration` 时，`summary`、`capability.description`、`capability.keywords` 都用你和用户对话时用的语言写；原项目是另一种语言时两种都写（如中文说明、中英文关键词），使目录展示和关键词推荐匹配用户的说法。关键词用真实领域短词，避免各领域通用词和易误推荐的厂商、模型、脚本名称。
5. **构建**：仅在已授权分发范围内调用 `package_build`，使用真实文件回执里的 `source_dir`，填写 `kind`、`declaration`、`origin`、`license`。版本只用字母、数字和 `.+-_`；声明文字、来源、许可证用单行。按错误和 warnings 修正，不绕过校验；`PACKAGE_BUILD_KEYWORDS_SCRIPT` 要补用户语言的领域关键词。
6. **安装**：只有请求同时授权安装时才调用 `package_install`，传本次构建回执的 `sha256`。这是工具调用，不是让用户敲的命令。`user_notice` 原样告知；`host_notice_queued=true` 表示宿主将展示安装提示。
7. **确认**：照回执原文提醒用户，只使用本次回执给出的 `user_confirm_command`、`open_orders`，不复用旧行，不凭记忆判断开关，不替用户执行任何确认命令。说明命令用途与撤回方式，见[提醒用户](references/tell-the-user.md)。

不自行安装外部现成包或改开关；外部包由管理员走 `/plugins install`，开关由管理员用 `/settings` 改。文案、包构建或安装回执均不代替原生业务链路验收。

## 交付

说明学到了什么、交付对象及位置或稳定引用、Workflow 绑定、实际检查和未验证部分。有学习笔记则更新实际结果；未授权安装就明确未安装。只有实际安装了包时，才说明相关任务推荐、点名使用和 `/plugins#` 查看方式。
