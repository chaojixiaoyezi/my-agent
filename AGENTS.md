# AI 开发规范

这份文档是给后续参与本项目开发的 AI 和人类开发者看的。修改代码前先读它；完成修改前按这里做收尾。

总入口是 [LLM_GUIDE.md](LLM_GUIDE.md)，含开工前/收工后清单、项目结构速览、编码规范和设计原则。本文件补充开发细节约束。

如果本轮交流产生了新的架构想法、命令语义、能力边界、长期方向或未落地设计，必须同步更新 `DESIGN_LEDGER.md`，并标明当前状态。超过约 100 行或明显属于单个模块的详细设计，要放到 `docs/design/` 的模块文档里，`DESIGN_LEDGER.md` 只保留导航和摘要。

## 基本原则

- 优先保持项目轻量、可读、可跨平台运行。
- 代码实现要贴合现有结构，不为了炫技引入大框架。
- Python 主链路优先使用标准库；新增第三方依赖必须说明原因、收益和替代方案。
- 不要把真实 API Key、Token、Cookie、个人路径等敏感信息写进仓库。
- 只改和当前任务有关的文件，不顺手重构无关模块。

## 语言和注释

- 面向用户的 CLI 文案、配置注释、Markdown 文档统一使用中文。
- 代码注释优先使用中文，解释“为什么这么做”，不要写“把变量赋值给变量”这类无效注释。
- 函数、类、变量名继续使用清晰英文，保持 Python 代码自然。
- 新增配置项必须在 `agent_py_agent/config/agent_config.yaml` 里写中文注释。

## 配置开关

新增能力时先判断是否需要配置开关。

必须加开关的情况：
- 会改变模型请求内容、记忆写入、自学习行为、工具执行范围或安全边界。
- 会带来额外 token 消耗、网络请求、文件写入、后台任务或长期状态。
- 默认行为可能让用户惊讶，或者不同用户会有明显偏好差异。

不一定要加开关的情况：
- 纯 bug fix。
- 文案修正。
- 内部结构整理，且外部行为不变。
- 明确无风险的小型展示优化。

配置项需要同时更新：
- `agent_py_agent/config/agent_config.yaml`：给用户看的默认值和中文说明。
- `agent_py_agent/agent/config.py`：`AgentConfig` dataclass 默认值。
- 相关测试或验证说明。

子代理、skill/tool 授权、能力上抛、capability request 相关参数不要塞进主配置。
这类参数统一放：
- `agent_py_agent/config/capability_config.yaml`
- `agent_py_agent/agent/capability_config.py`

能力路由配置里的数字限制项统一约定：`0` 表示不限制。

## 自学习功能约束

自学习功能由 `enable_self_learning` 控制，默认关闭。

开启后也必须遵守：
- 不直接修改正式 skill。
- 先生成学习候选草稿，等待用户确认。
- 学习候选要说明来源任务、触发原因、拟保存内容和适用场景。
- 用户确认后，才允许写入正式 skill 目录。
- 用户纠正过的内容优先作为学习信号，但不能覆盖用户未确认的长期偏好。

建议目录：

```text
agent_py_agent/data/learning_drafts/          # 自学习候选草稿
agent_py_agent/skills/                        # 内置 skill，随仓库发布
~/.my-agent/skills/                           # 用户长期 skill，默认不进仓库
<workspace>/.agent/skills/                    # 项目专属 skill
```

## Skill 设计方向

本项目后续的 skill 体系优先学习 长期助手 的“渐进加载”，但保持比 长期助手 更保守的确认机制。

目标加载顺序：

```text
Level 0: skill 索引，只包含 name、description、when_to_use、scope
Level 1: 需要时加载 SKILL.md 主体 procedure
Level 2: 需要时加载 references/templates/scripts 里的具体文件
```

推荐 skill 结构：

```text
skill-name/
|-- SKILL.md
|-- references/
|-- templates/
`-- scripts/
```

Skill 优先级建议：

```text
workspace > user > builtin
```

## 文件树维护

任何新增、删除、移动重要文件或目录，都要更新 `CODEBASE_TREE.md`。

文件树写法：
- 使用 `|--`、`` `--`` 和缩进画树，保持现有风格。
- 每个重要文件后面加一句中文说明。
- 运行时生成的大量文件不要逐个列，只列目录和用途。
- 如果只是临时文件、缓存、测试输出，不要加入文件树。

新增文件时通常要更新两处：
- `## Tree` 里的目录树。
- `## 关键文件说明` 里的职责说明，如果这个文件是长期维护入口。

## 文档和记录

- 用户可见的新能力要更新对应文档或说明。
- 新增设计想法、未来方向或暂未落地的架构约定时更新 `DESIGN_LEDGER.md`；长篇模块细节放到 `docs/design/`，并在主台账里链接。
- 修改项目结构时更新 `CODEBASE_TREE.md`。
- 修改测试策略或新增验证方式时更新 `TESTS.md` 或 `TEST_CHECKLIST.md`。
- 过程记录类文件如 `RUNLOG.md`、`RESULT.md`、`EVIDENCE.md` 按任务需要更新，不做无意义刷屏。
- 并行开发或多 worktree 开发时先读 `WORKSTREAMS.md`，完成后按 `HANDOFF_TEMPLATE.md` 写交接。

## 测试要求

改代码后至少做一种验证：
- 语法检查，例如 `python3 -m py_compile ...`
- 定向功能测试。
- 已有测试脚本。

如果没能运行测试，要在最终回复里说明原因。

涉及这些内容时要优先补测试：
- 配置解析。
- prompt 拼装。
- 工具调用。
- 记忆写入。
- 自学习草稿生成。
- skill 加载和优先级。

## 安全边界

- 写文件、发网络请求、执行命令、自学习落盘都要能被用户理解和追踪。
- 默认不要自动执行高风险操作。
- 自学习草稿可以自动生成，但正式保存必须由用户确认。
- 输出调试信息时不要打印完整 API Key；最多显示是否读取到和长度。

## 完成前检查清单

提交最终答复前快速检查：
- 配置项是否同步更新了 YAML 和 dataclass。
- 新增文件是否更新了 `CODEBASE_TREE.md`。
- 中文注释和文案是否清楚。
- 是否误写真实密钥或本地隐私路径。
- 是否运行了必要验证。
