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

## 开发铁律总纲

- 自然语言负责沟通，不负责机器决策。用户 prompt、模型 summary、报告正文、guidance、角色描述和展示文案只能作为 LLM/人类上下文；运行时路由、权限、验收、恢复、派工、产物归属、状态变更必须读取结构化字段、状态码、refs、schema、工具结果、显式配置或文件系统事实。
- 硬门只守安全和客观事实。危险路径、危险命令、越权写入、破坏运行时、产物不存在、格式打不开这类可以硬拦；任务深度、报告质量、子代理汇总节奏、覆盖充分性这类默认走 warning、返工提示、证据要求或 closeout，不前置卡死主链路。
- 主链路优先，兜底最后。主链路没跑顺之前，不加 fallback、旧路径兼容、影子入口、只转发 facade 或“还能跑”的旁路；确定不用的旧字段、旧目录、旧兼容分支要删。
- 一个概念一个权威位置。task workspace、memory、artifact、subagent state、compact ledger、config 都必须有唯一 canonical path / canonical schema；projection/index 只能做查找和展示。
- 配置必须真的生效。用户配置、默认 YAML、dataclass 默认值、测试覆盖不能互相打架；不允许“配置里改了，运行时还读写死值”。
- 状态和控制必须结构化。停止、取消、暂停、恢复、接管、验收、授权要走 control event/tool 参数/status 字段，不解析普通自然语言触发。
- 状态别名不隐式兼容。旧标签、多语言词表和模型口头“完成/成功/失败”不能自动变成当前协议状态；确需迁移时要写显式结构化迁移记录。
- 大输出、compact、resume 是底层能力，不是 prompt 技巧。长文件、长工具输出、长任务必须依赖 chunk、cursor、coverage ledger、archive 和 resume summary。
- 优先复用已验证的通用实现。会话、活动回合、Compact、Skill、工具、计划、子代理、停止与引导统一适配 owner/thread/task 事实源；长期记忆、人格、持久调度和通道投递各守模块边界，不再并行维护第二套状态。
- 详细条款以 `docs/development/DEVELOPMENT_RULES.md#0-development-iron-rules--开发铁律` 为准。

## 架构铁律

- 禁止做任何专项合同。底层合同层只能抽象通用能力、通用状态、通用路径、通用工具协议、通用恢复动作；不能为了某一个具体任务、页面、论文、表格、网站、按钮、提示词样例去长一层专门逻辑。
- 自然语言只能作为软约束。可以用来提示“优先自己做”“优先先产出阶段文件”“如果不确定先看工具清单”这类引导，但不能拿自然语言文本做机器判断依据。
- 结构化事实优先。路径、产物、验收、恢复、状态机、工具可用性、权限、阶段产物、幂等信息，必须通过结构化字段、refs、schema、状态码、工具账本、文件系统事实表达。
- 并发代理身份必须显式。子代理、孙代理、工具调用、产物登记、状态事件都要优先携带 `run_id/task_id/parent_run_id/root_run_id` 等结构字段；`inspect_agent_tree` 是展示投影，不是身份来源；线程级上下文只能做权限上界。显式身份参数和当前 runner 上下文冲突时，不能静默猜，工具响应必须暴露 `scope_resolution/scope_warnings` 这类结构化裁决事实。消息、协作 case/request、证据提交、能力申请这类会写账的入口，也必须按这个规则处理：当前 runner 内只能代表自己，冲突参数只能成为 warning，不能冒充其它 run。
- 开放世界禁止封闭枚举。文件格式、产物类型、协议、MIME type 等会持续增长的概念，不能用写死映射表作为唯一判定路径；映射表可以作为已知类型优化，但必须允许合同/配置显式声明覆盖值，或从 key 本身推导合理默认行为，不能“不在表里就拒绝”。
- 真实任务测试时，任何时候 AI 开发者都只是测试者和旁观者，只能向被测对象给出一次 prompt 需求，然后观察被测对象自己的日志、合同、产物和验收结果；不能越界、越权替被测对象修改文件、补产物、执行任务或把旁路产物算作测试通过。
- 发现问题时，先读对应模块的合同、实现和回归测试，再改代码。需要引入外部依赖或实现时，先核对公开接口、维护状态与许可，记录适用边界；不能为了单个失败样例增加旁路或专项状态。
- 主代理和合同层开发默认走“合同单测 -> fake tool -> fake LLM -> replay -> 少量真实环境验收”的顺序。真实环境测试保留，但只能作为最终验收和失败样本来源，不能再作为主要开发反馈回路。
- 遇到真实任务跑不通、循环、卡死、工具误用、恢复失败、产物缺失时，不允许第一反应就是临时打补丁。先归类问题属于哪一层：状态机、工具协议、交付合同、恢复合同、工作区、消息、权限或提示注入，然后参考对照项目的成熟做法，再落到本仓库的通用合同层。
- 禁止为了测试样例写死行为。测试任务、真实任务、样例 prompt 可以存在于数据层和测试层，但不能反向驱动产品代码出现“如果是购物站 / 如果是家具页 / 如果是 xlsx / 如果是 PDF 就这样”的专项分支。

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

当前规则（2026-10-25 自动审核恢复的集成约定）取代 2026-10-16 对所有正式记忆一律人工确认的约定；来源可学本身仍不等于批准。记忆实现的合入与验收状态见 [memory 进度](docs/modules/memory/02-progress.md)，本约定不代表运行版本已经切换。
- `AGENTS.md` 和 `SOUL.md` 仍仅限当前用户当场显式修改/确认；AGENTS 沿 `/agents` 完整预览确认或用户直接编辑，SOUL 沿原人格修改与宿主审批。模型、子代理、定时与后台不能签发两者的正式修改权。
- 普通正式 Memory（`long_term`）、用户画像偏好（`user`），以及达到重复门槛的经验规则（`lesson` / `hot`），在通过宿主学习来源核验、证据核验与安全门后，由后台记忆整理（`MemoryCurator` / `MemoryPromotionService`）自动完成确认与归档；保留原 owner/actor、范围、冲突、配额和目标版本 CAS，不把模型声明当核验或用户指令。
- 普通/群 IM 本人仍可用 `/memory candidates` 查看候选，再 `preview <候选ID>`，复制该预览给出的 `confirm <预览编号>`；人工确认继续绑定原 owner/actor/thread、完整版本与目标 CAS。来源不可学、证据不足或未过安全门的候选不得自动晋升。
- 超过 `rejected_candidate_days`（默认 30 天）仍未晋升的陈旧非 `promoted` 候选由定期 retention 自动清理，不把已晋升的正式记忆当陈旧候选删除。
- 正式 Skill 沿 main 现有用户确认、版本与 CAS 链，内置、共享、插件、工作区和用户手写内容不由自动流程覆盖。输出合同、重名、数量、脱敏、frontmatter 和安全扫描仍是必要条件，不能替代确认。
- 自学 Skill 的所有权仍只由 `<owner_home>/data/skill_learning/registry.json` 认定；内容 hash 不符即视为用户修改，版本全文与原 ledger 保留，撤回和删除不新建第二清单。
- 学习材料与账本不保存密钥；用户纠正可成为候选信号，但不能绕过来源、证据、安全及目标专属门。自动维护仅按上述显式策略晋升、保留或清理，临时指令不能固化为长期偏好。
- `self_map` 只聚合原公开读口，不授权、不执行、不解析正文作权限；普通/群/子代理不得借目录扩大范围，unknown/partial 不能当作空。实现与边界见[自我目录](docs/design/SELF_MAP.md)和[规则归属](docs/design/PERSONA_AGENTS_OWNERSHIP.md)。

建议目录：

```text
<owner_home>/data/skill_learning/                     # 自动总结 Skill 的请求队列、登记表、账本、版本全文与删除归档
<owner_home>/skills/learned/<name>/SKILL.md           # 用户确认后发布的自学 Skill（category=learned）
<owner_home>/data/skill_proposals/<proposal_id>.json  # 子代理经验 Skill 提案（经原用户确认后安装到 <owner_home>/skills/lesson-*）；不得改用 learning_drafts，Curator 迁移会清理该名字的目录
agent_py_agent/skills/                        # 内置 skill，随仓库发布
~/.my-agent/skills/                           # 用户长期 skill，默认不进仓库
<workspace>/.agent/skills/                    # 项目专属 skill
```

## Skill 设计方向

本项目的 Skill 体系采用逐轮 snapshot（快照）、元数据索引和显式读取正文。长期运行沿用同一份 Skill 身份与加载记录；确认和权限继续由结构化 owner/tool policy 控制。

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

## 代码注释规范（双层注释硬门已于 2026-10-29 退役）

- 2026-10-29 起，不再强制每个模块、class、函数、类方法机械补齐 `# LLM:` + `# 函数用途:` / `# 类用途:` / `# 模块用途:` 双层注释标签，也不把双层注释格式当作代码复核或提交的硬阻塞项。
- 关键模块、类、函数建议保留一条清晰、自然的中文注释或 docstring，说明真实用途、契约边界与副作用（如写文件、发请求、启动进程、改状态、更新索引）；存量双层注释无需刻意删除。
- 不为满足行数、参数数、Mixin 数量或固定注释前缀而拆分实现、增加转发层或机械刷注释；按真实职责、状态所有权、调用边界和接口合同决定拆分或合并。

## 文档和记录

- 涉及用户可见能力、公开命令或长期架构边界变化时，同步更新相关说明文档；多分支并行开发时，共享台账（如 `TESTS.md`、`DESIGN_LEDGER.md`、`CODEBASE_TREE.md`、`02-progress.md`、`04-structure.md`）统一在主分支集成收尾阶段按需更新，不在分片分支强制打卡。
- 新增设计想法、未来方向或暂未落地的架构约定时更新 `DESIGN_LEDGER.md`；长篇模块细节放到 `docs/design/`，并在主台账里链接。
- 修改项目结构（新增、删除、移动长期维护文件）时更新 `CODEBASE_TREE.md`。
- 过程记录类文件如 `RUNLOG.md`、`RESULT.md`、`EVIDENCE.md` 按任务需要更新，不做无意义刷屏。
- 并行开发或多 worktree 开发时先读 `docs/WORKSTREAMS.md`，完成后按 `docs/tasks/HANDOFF_TEMPLATE.md` 写交接。

## 完成汇报规范

- 每次完成一轮开发、验收、合并、打包或交接，都必须在汇报末尾写 `建议下一步`。
- `建议下一步` 要说清推荐先做什么、为什么先做它、是否适合其他 agent 并行，以及需要守住的风险边界。
- 其他 agent、子代理和新线程交接也遵守这条：不能只说“已完成”，必须给接手者一个明确的后续动作。
- 如果当前没有代码改动，也要说明下一步建议是继续开发、等待评审、合并、跑 CI、清理 worktree，还是暂停。

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

### 远端提交前强制本地严格验收

线上 GitHub Actions 在跑（Lint、Test 的 Python 3.11/3.12、Cross-platform guard 的 macOS/Windows、每晚 Full Tests）。每次推送 `main` 后必须确认这些工作流全绿（用户 2026-10-07 要求：别让推送一直报错）；本地严格验收是推送前的最低要求，不能代替线上 CI。只要准备把代码推到远端、更新远端分支、开/更新 PR、或合并到 `main`，就必须先在本地跑最严格验收。

远端提交前最小严格 gate：

```bash
python3 -m pytest <与改动直接相关的测试文件> -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_import_boundaries.py --json
git diff --check
python3 scripts/check_clean_package.py .
```

要求：
- 默认只跑与改动直接相关的 focused tests；不要为了常规小中型改动重复跑全仓 pytest。
- 仅当本轮生产代码和测试代码的新增、删除累计达到约 10,000 行，或用户另行明确要求时，才在 focused tests
  之外追加 `python3 -m pytest -q --tb=short` 全仓测试。
- Ruff、导入边界、diff 和 clean-package 属于远端提交前严格 gate，不受上述 pytest
  频率限制。
- 2026-10-28 起代码尺寸数值门退役；2026-10-29 起双层注释格式硬门与 `scripts/check_doc_sync.py` 机械打卡硬门（强制每个 `.py` diff 带 `#` 注释及每次必改 `02-progress.md` / `04-structure.md`）退役。不再把这些形式主义检查作为提交或复核阻塞条件。
- 任一命令失败时，默认不得推送远端、不得合并到 `main`；除非用户明确要求绕过，并且最终汇报写清失败命令、风险和原因。
- 推送 `main` 前先看上一轮线上 CI 是否全绿；推送后跟到全部工作流全绿为止，红了先修，不在红的 `main` 上接着推新功能。
- 只在线上才暴露的问题，推送前要在接近 GitHub runner 的环境复现：Linux 用非 root 用户、Python 解释器不在系统目录、3.11/3.12 版本（支持范围 Python 3.11–3.14）、单用例 60 秒超时；改到文件系统、进程、路径这类平台相关代码时，先在分支上手动触发 Cross-platform guard（Windows 只能在线上跑）。
- 如果线上 CI 被禁用或被 billing 阻塞，最终汇报必须明确说明“本地严格 gate 已通过/未通过”，以及线上 CI 没有作为验收来源。
- 如果本轮不提交远端，只是本地探索、草稿或小切片开发，继续按改动风险运行 focused tests、语法检查、doc sync 或必要 guard；不强制每次都跑全量严格 gate。

## 安全边界

- 写文件、发网络请求、执行命令、自学习落盘都要能被用户理解和追踪。
- 默认不要自动执行高风险操作。
- 自学习沿“自学习功能约束”的显式集成约定执行：AGENTS/SOUL 仍须当前用户当场修改/确认；其余允许自动晋升的记忆候选必须通过宿主来源、证据、安全与重复门槛核验，不扩大跨 owner 来源授权。实现是否合入及运行版本是否切换须分别核实。
- 输出调试信息时不要打印完整 API Key；最多显示是否读取到和长度。

## 完成前检查清单

提交最终答复前快速检查：
- 配置项是否同步更新了 YAML 和 dataclass。
- 新增文件是否更新了 `CODEBASE_TREE.md`。
- 中文注释和文案是否清楚。
- 是否误写真实密钥或本地隐私路径。
- 是否运行了必要验证。
- 最终汇报是否包含 `建议下一步`，并说明其他 agent 是否能并行接手。
