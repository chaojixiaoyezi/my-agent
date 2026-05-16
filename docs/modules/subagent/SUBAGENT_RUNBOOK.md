# Subagent / Capability / Runner Runbook

这份文档专门说明当前 subagent 架构怎么用、哪些东西已经落地、哪些只是记录或预留。

一句话总览：

```text
父代理创建工单
  -> 子代理在自己的工单目录里工作
  -> 缺能力时生成 capability_request
  -> 父代理路由 skill/tool card
  -> 命中则生成 capability_grant
  -> grant 变成 execution_context
  -> subagent-run 读取 execution_context
  -> runner 输出 [SUBAGENT_RESULT] JSON
  -> 系统把 evidence/request/artifacts/tests/patches/lessons 写回工单
  -> 父代理 dispatch 做 patch 审核、验收、路由、接管或重派
```

## 设计目标

这个模块不是简单的“拆几个子任务”。

它要解决的是这些问题：
- 子代理说完成了，但没有证据。
- 子代理卡住很久，父代理不知道。
- 子代理缺工具或 skill，却假装任务失败或任务完成。
- 多个子代理并行时，谁负责、谁验收、谁收口不清楚。
- 100+ 子代理时，异常任务被海量普通任务淹没。
- 工具失败和通道故障混在一起，导致盲目重派。
- 子代理输出只留在聊天里，压缩上下文后难恢复。

所以当前实现优先强调：
- 工单落盘。
- 机器 JSON 和人类 Markdown 双轨。
- 默认 dry-run。
- 能力授权可审计。
- DONE 需要证据。
- runner 不直接标记 DONE，只进入待验收。

## 质量契约与受控施工队模式

这轮讨论确认了一个重要边界：子代理不是“平行主代理”，而是受控施工队。

子代理可以更像主代理，拿到足够的上下文、记忆和质量标准；但它不能成为项目经理，不能自己定义完成标准，也不能直接决定最终交付。父会话负责定义标准、裁决边界、验收产物和决定是否交付。

完整模块设计见 `docs/design/subagent-quality-contract.md`；本 runbook 保留操作层规则和落地检查。

典型痛点：
- 子代理只收到“动作目标”，例如下载、翻译、生成、检查，却没有收到“成品质量目标”。
- 子代理容易把文件存在、命令成功、页数匹配、检查脚本通过当成任务成功。
- 模糊边界处最容易出错，例如主论文/附录/系统卡判定，正文/参考文献/补充材料判定，原文排版问题/翻译破坏判定。
- 父会话派工和验收如果只写工程化指标，会把低质量结果放大成“已完成”。
- 子代理总结不是结论，只是待审核材料；不能因为它说 PASS 就信。
- 全量塞入长 prompt 不等于继承责任感，反而可能稀释重点、增加成本和让多个子代理都误以为自己是总负责人。

因此，以后主代理派工前应尽量生成一份 `QualityContract`：

```text
user_visible_goal        用户真正要的交付效果，而不是动作列表
reference_standard       对标样本，例如 DeepSeek PDF V19
bad_version_conditions   什么算坏版、半成品或不可交付
must_check               必须执行的人工/机器检查
must_not_ship            出现哪些问题绝对不能交付
sampling_plan            抽查策略，例如前/中/后/附录/图表/参考文献
evidence_required        必须提交的证据、截图、路径、命令或日志
risk_reporting           哪些不确定性必须列风险，不能吞掉
final_decider            最终裁决人，默认父会话/用户
```

高质量任务不能只写“完成 X”。例如 PDF 翻译类任务要写清楚：

```text
对标 DeepSeek PDF V19。
不允许大片英文正文。
不允许只看页数匹配。
Appendix 不能一刀切保留，必须判断是不是正文型附录。
发现排版差、乱码、重叠、工具痕迹必须标红，不准说 PASS。
子代理只能提交 AWAITING_REVIEW，最终交付由父会话裁决。
```

### 上下文包策略

不要给每个子代理无脑塞完整 50K prompt。更稳的结构是三层上下文包：

```text
Core Context Pack      固定硬规则，约 2K-5K
Task Context Pack      当前任务关键背景，约 5K-15K
Role Context Pack      当前角色专用规则，约 2K-10K
```

Core Context Pack 应包含：
- 不能 fake done。
- 不能只说完成，必须落盘证据。
- 不确定必须列风险。
- 不准擅自降级目标。
- 只能提交待验收材料，最终裁决属于父会话/用户。
- 子代理输出必须足够让父代理复核，不能把关键事实只留在自己的推理里。

Task Context Pack 应包含：
- 用户原话和最终目标。
- 成功样本和失败样本。
- 当前任务目录、SPEC、已知坑和交付红线。
- 本轮必须检查的文件、页面、路径、命令或证据。

Role Context Pack 应按角色裁剪：
- 收集型：真实 URL、错链排除、主件/附件判定。
- 生产型：具体生成规则、不可改边界、输出路径。
- QA 型：只负责挑错，发现坏页/漏翻/乱码/错链必须 FAIL。
- 修复型：只修指定问题，不扩大范围。
- Reviewer 型：审查 evidence 和质量契约，不替施工方辩护。

每次派工最好落一个 context manifest，记录：
- 给了哪些上下文包。
- 哪些文件是必读。
- 哪些质量标准生效。
- 哪些内容故意没有给。
- 本子代理的角色和写入边界。

这样子代理做差时，可以追溯是执行失败、上下文缺失，还是质量契约没有写清楚。

### 角色拆分

不要让同一个子代理既生产又证明自己质量好。高质量任务至少拆成两类：

```text
producer / 施工小傻妞   负责产出材料、文件、候选清单、初步报告
critic / 挑错小傻妞     负责找问题，不能修，不能替 producer 辩护
```

必要时再加：

```text
repairer / 修复小傻妞   只修父会话指定的问题
reviewer / 验收小傻妞   按质量契约审查，但仍不能代替用户最终审美
```

critic 的提示词要和 producer 不同。它的目标不是证明完成，而是找哪里不合格：

```text
你的任务不是证明它完成，而是找它哪里烂。
发现大片英文、错链、坏页、工具痕迹、偷懒，一律 FAIL。
不要给面子。
只提交证据和 FAIL/PASS，不许修。
```

### 反验收原则

子代理汇报必须反着验：
- 它说清单完整，父代理要查有没有漏、有没有错链。
- 它说 PDF 好了，父代理要抽后半段、图表页、参考文献页。
- 它说 QA PASS，父代理要看它到底测了什么。
- 它说没有问题，父代理默认先怀疑一次，直到 evidence 支撑。

机器验收只能证明“不坏”的一部分：
- 文件存在。
- 能打开。
- 页数一致。
- 命令成功。
- contact sheet 存在。

它不能证明：
- 好不好看。
- 顺不顺眼。
- 有没有偷懒。
- 是否像正式交付。
- 用户打开后是否舒服。

所以最终验收必须保留父会话/用户审美权。对于 PDF/文档交付，最终抽查至少覆盖：
- 第一页。
- 中间正文。
- 后半段。
- appendix 开头。
- 图表密集页。
- 参考文献页。
- 用户最可能打开看的部分。
- 最终 zip 或交付包。

### 成功样本库

像 DeepSeek PDF V19 这种成功版本，应沉淀成质量 profile，而不是只留在聊天里。

建议后续增加：

```text
agent_py_agent/quality_profiles/
  pdf_translation_deepseek_v19.md
  code_patch_safe.md
  log_analysis_first_response.md
```

同类任务派工时引用 profile：

```text
本任务质量对标：DeepSeek 2025 PDF V19。
不得低于该版本的中文覆盖、目录处理、排版观感和 QA 标准。
```

### 用户/父会话必须指定的内容

系统可以提供默认模板，但不能替用户猜主观质量标准。以下内容必须由用户或父会话在派工前说明：
- 对标哪个成功样本。
- 什么叫精品、半成品、坏版。
- 哪些内容必须翻译、哪些可以保留原文。
- 速度优先还是质量优先。
- 是否需要独立 critic。
- 是否允许自动返工，以及最多几轮。
- 最终是否必须等用户亲自看过才交付。

### 建议实现路线

短期：
- 在 `SubAgentTask` / `execution_context` 增加质量契约字段。
- runner prompt 明确“不能定义完成标准，只能提交待验收材料”。
- acceptance 报告显示质量契约是否存在、证据是否覆盖抽查计划。
- dispatch 支持 `producer` / `critic` / `reviewer` 角色。

中期：
- 增加 context pack / context manifest。
- 增加 quality profile 目录和引用机制。
- 增加 critic 阶段：producer 完成后默认生成挑错任务。
- 把质量失败沉淀成 eval/fixture，后续回归测试。

长期：
- 支持按任务类型自动选择质量 profile。
- 支持多轮 evaluator-optimizer：生产 -> 挑错 -> 修复 -> 再验收。
- 对高风险/高审美任务强制 human-in-the-loop，不允许自动交付。

### 用户少说模式

目标不是让用户学会怎么派工，而是让用户只说任务、灵感和偏好。系统负责把任务转换成质量契约、角色拆分、上下文包、验收闸门和落盘文件。

用户理想输入应该像这样：

```text
把这批 PDF 翻成可交付中文版，质量对标昨天那个 DeepSeek V19。
```

而不是这样：

```text
请创建 producer/critic/reviewer 三个子代理，分别注入 Core Context Pack、
Task Context Pack、Role Context Pack，并按 sampling_plan 抽查后半段和 appendix。
```

系统应自动完成：
- 识别任务类型：PDF 翻译、代码修改、日志分析、资料收集、报告写作、批量 QA 等。
- 选择 quality profile：例如 PDF 翻译默认使用 `pdf_translation`，如果用户提到 DeepSeek V19 则绑定该成功样本。
- 生成 `QualityContract`：把用户目标转成坏版条件、禁止交付条件、必须检查项和抽查计划。
- 决定派工拓扑：普通任务只派 producer；高质量交付默认 producer + critic；高风险或用户可见交付再加 reviewer。
- 生成 context packs：只给子代理相关上下文，不把全量 50K prompt 无脑复制给每个子代理。
- 落 context manifest：让父代理后续能追溯每个子代理拿到了什么标准和证据。
- 强制输出契约：子代理只能提交材料和证据，不能自行宣布最终交付。
- 自动反验收：critic / acceptance 不看总结先看证据、产物和抽查覆盖。

#### 哪些应该内置

这些属于系统默认能力，不应该要求用户每次说：
- 子代理不能定义完成标准，不能直接 `DONE/VERIFIED`。
- 子代理必须提交 evidence、checks、failures、risks 和 needs_parent_decision。
- producer 与 critic 的目标必须分离。
- critic 默认站在找问题的立场，不替 producer 辩护。
- 高质量交付默认有抽查计划和反验收。
- execution context 默认包含 core context pack。
- dispatch 默认写 context manifest。
- acceptance 默认检查质量契约覆盖情况。
- 子代理输出和父代理验收全部落盘，便于恢复和复查。

#### 哪些应该做成开关

这些会影响成本、速度、模型调用或用户体验，应该可配置：
- `enable_subagents`: 是否启用子代理。
- `subagent_mode`: 本地硬化、平衡、严格三档；普通本地开发默认 `trusted_local_hardening`。
- `max_subagents`: 单任务登记上限，默认给得很高，避免把正常任务误卡住。
- `subagent_workspace`: 子代理运行记录目录。
- `subagent_role_template_dirs`: 额外角色模板目录；空列表表示内置模板 + 工作区模板。
- `subagent_debug_trace_level`: 0-5 调试追踪等级，默认关闭。
- `acceptance_execute_tests` / `acceptance_test_timeout_seconds`: 父级验收是否真实执行测试，以及单条测试超时。

其他更细的质量模式、上下文预算、QA 拓扑、修复轮数和调度细节先由系统内部策略/LLM 判断，不再作为普通用户默认配置项。需要面向企业/外部用户暴露时，再通过 workflow 或高阶 profile 统一打开，而不是继续堆几十个微参数。

#### 哪些仍需要用户或父会话表达

系统可以推断和提供默认值，但这些主观标准不能完全自动猜：
- 对标哪个成功样本。
- 最终交付给谁看。
- 速度优先、成本优先还是质量优先。
- 哪些内容属于“必须好看”的核心区域。
- 哪些降级可以接受，哪些不能接受。
- 是否允许外部下载、联网、调用真实模型或长时间运行。
- 是否必须等用户亲自打开看过才算完成。

如果用户没有说，父代理应该用模板先生成保守默认，并在关键不确定时只问一个短问题，而不是把派工细节甩给用户。

### 后续开发步骤

#### Phase 1: 契约字段和模板骨架

目标：
- 让每个 subagent 工单都能携带质量契约、角色和上下文 manifest。

主要改动：
- `SubAgentTask` 增加 `role`、`quality_contract`、`context_manifest`、`quality_profile`。
- `execution_context.json` 增加质量契约和角色上下文。
- 新增 `agent_py_agent/agent/subagents/quality.py`，定义 `QualityContract`、`ContextPack`、`ContextManifest`。
- 增加默认 core context pack。

验收：
- 创建工单后能看到质量契约字段。
- runner prompt 能显示“不能定义完成标准，只能提交待验收材料”。
- 旧工单缺字段时兼容读取。

#### Phase 2: 质量 profile 和自动任务分类

目标：
- 用户少说时，系统能自动选择模板。

主要改动：
- 新增 `agent_py_agent/quality_profiles/` 或 `agent_py_agent/config/quality_profiles/`。
- 内置 profile：`generic_delivery`、`pdf_translation`、`code_patch`、`log_analysis_case`、`research_collection`。
- dispatcher 根据用户目标、文件类型、关键词和风险等级选择 profile。
- 支持命令/配置覆盖 profile。

验收：
- “翻译 PDF”自动命中 `pdf_translation`。
- “修代码并测试”自动命中 `code_patch`。
- “日志分析 case”自动命中 `log_analysis_case`。
- 未命中时回落 `generic_delivery`，不崩溃。

#### Phase 3: producer / critic 编排

目标：
- 让系统自动把高质量任务拆成施工和挑错，而不是让用户手写。

主要改动：
- dispatch 增加 role-aware planner。
- producer 完成后，根据质量模式自动创建 critic 工单。
- critic 只读 producer 产物和质量契约，默认不写生产文件。
- critic 输出 `failures`、`evidence`、`sampling_coverage` 和 `recommended_repairs`。

验收：
- 高质量任务能自动生成 producer + critic。
- critic 不允许把自己的 PASS 当最终交付。
- producer 与 critic 的上下文包不同。

#### Phase 4: 质量验收器

目标：
- acceptance 从“是否有 evidence”升级到“evidence 是否覆盖质量契约”。

主要改动：
- acceptance 检查 `must_check`、`sampling_plan`、`must_not_ship`。
- 报告里显示：覆盖了哪些检查，缺哪些检查，哪些失败阻断交付。
- 对文档/PDF 类 profile，默认要求前/中/后/appendix/图表/参考文献抽查证据。
- 对代码类 profile，默认要求测试命令、diff 摘要、风险说明。

验收：
- 只有文件存在但没抽查证据时不能 VERIFIED。
- critic 发现阻断问题时不能 VERIFIED。
- 父代理可以明确 override，但必须写审计原因。

#### Phase 5: 用户少说的入口体验

目标：
- 用户自然语言任务自动触发质量派工，除非配置关闭。

主要改动：
- 主代理创建子代理前先运行 `quality planner`。
- `spawn-subagents` / orchestration tool 支持自动 quality contract。
- `status` / `subagents` 显示质量模式、profile、critic 状态和阻断原因。
- chat/gateway 中只在必要时问短问题，不暴露内部派工细节。

验收：
- 用户只说任务目标时，系统能生成合理质量契约。
- 用户提到“对标某版本”时，该样本进入 reference_standard。
- 用户不需要手写 producer/critic/reviewer。

#### Phase 6: 回归样本和持续改进

目标：
- 把失败案例变成可复测资产，而不是只靠记忆。

主要改动：
- 质量失败写入 `validation/quality_cases/` 或 future eval store。
- 增加 PDF/文档、代码、日志分析三类质量 fixture。
- Live Lab 增加“高质量交付任务”case。
- 失败样本可生成 profile 改进草稿，但不自动改 profile。

验收：
- 曾经的坏模式有回归测试。
- profile 修改前后能比较质量检查结果。
- 质量检查失败能变成明确 backlog，而不是聊天里的一句抱歉。

## 核心概念

### SubAgentTask

文件里仍叫 `SubAgentTask`，兼容早期代码；语义上它已经是一个轻量 `SubAgentRun`。

它记录：
- `id`
- `goal`
- `thought`
- `plan`
- `parent_id`
- `root_id`
- `depth`
- `owner`
- `supervisor`
- `final_owner`
- `allowed_skills`
- `allowed_tools`
- `used_skills`
- `used_tools`
- `capability_requests`
- `capability_grants`
- `capability_gaps`
- `evidence`
- `status`
- `verification_status`
- 标准工单路径

### CapabilityRequest

子代理遇到能力缺口时上抛。

典型字段：
- `problem`：遇到什么问题。
- `needed_capability`：需要什么能力。
- `expected_output`：拿到能力后想产出什么。
- `tried`：已经试过什么。
- `evidence`：失败证据或观察。
- `constraints`：安全、权限、方法等约束。

子代理不应该自己搜索全局 skill/tool 宇宙；它只描述问题。

### CapabilityGrant

父代理或更高层级下发的授权。

它可以包含：
- skill 名称。
- tool 名称。
- capability card。
- 授权原因。
- 约束。
- 是否仅当前任务有效。

grant 会合并进子代理的 `allowed_skills` / `allowed_tools`。

### CapabilityGap

最终找不到能力时留下的缺口。

它是后续自学习或工具建设的输入，不是失败后随手写一句话。

### Execution Context

`execution_context.json` 是子代理 runner 真正读取的最小上下文。

它只包含：
- 当前任务目标。
- 思路和计划。
- owner / supervisor / final_owner。
- allowed skills / tools。
- granted cards。
- 写入边界。
- 验收要求。
- 已有证据。
- open request / gap。
- 执行硬规则。

它刻意不包含全局 skill/tool registry。

### Runner Result

runner 执行后会写：
- `RUNNER_RESULT.md`
- `reports/runner_result.json`
- `logs/runner_prompt.md`
- `logs/runner_response.md`
- `output.json`

这些文件是后续验收器、集成器和父代理接管的事实源。

## 标准工单目录

创建一个子代理 run 后，会生成类似目录：

```text
agent_py_agent/data/subagents/<run_id>/
|-- task.json
|-- run.json
|-- thought.md
|-- STATUS.md
|-- WORK_LOG.md
|-- ACTION_RECEIPTS.md
|-- ACCEPTANCE.md
|-- TEST_CHECKLIST.md
|-- BUGS.md
|-- SKILL_USAGE.md
|-- HANDOFF.md
|-- DEBRIEF.md
|-- TAKEOVER.md
|-- CHANNEL_PROBE.md
|-- EXECUTION_CONTEXT.md
|-- execution_context.json
|-- output.json
|-- dependencies.json
|-- RUNNER_RESULT.md
|-- data/
|-- output/
|-- tests/
|-- reports/
|   `-- runner_result.json
|-- logs/
|   |-- runner_prompt.md
|   |-- runner_response.md
|   `-- last_channel_probe.json
`-- scratch/
```

不是每个文件一开始都有内容，但路径会尽量初始化，方便接管和验证。

## 命令流

### 创建子代理工单

```bash
python3 -m agent_py_agent spawn-subagents "实现一个功能并验收" --count 2
```

真实层级 E2E 要先创建主节点/root coordinator 时，用显式 role：

```bash
python3 -m agent_py_agent spawn-subagents "主节点任务说明" --count 1 --role coordinator --agent-name root-coordinator
```

这条命令只负责创建一个能调度下层的 root/coordinator。后续必须由 root runner 自己调用 `schedule_child_subagents` 创建子代理；子代理再创建孙代理；孙代理再创建孙孙代理。外层测试控制器不要直接替下层创建任务。

root/coordinator seed 会拿到调度、看板、基础读写、web 取证和 task-local 报告写入工具；shell/exec 仍要走受控网关或能力申请，不靠角色模板暗开。中文理解：coordinator 可以写计划、分工、证据、协调报告，也能查资料核证，但最终业务代码、页面、文档产物仍优先交给 worker/writer/leaf_worker。

层级创建还有一个硬边界：同一次 `schedule_child_subagents` 不能同时创建 coordinator/lead 和 leaf/leaf_worker。系统会返回 `mixed_coordinator_leaf_children`，要求父节点先只创建下一层 coordinator，再让下一层 coordinator 自己创建它的 leaf。中文理解：不能让上一层一口气把领导和最底层工人都造出来，否则任务树会被拍平，后续接管、验收和责任归属会乱。

输出里会看到 run id，例如：

```text
subagent-1777390000-abcd1234
```

### 查看看板

```bash
python3 -m agent_py_agent subagents
```

默认展示 hot list 或最近任务。

查看全部：

```bash
python3 -m agent_py_agent subagents --all
```

按状态过滤：

```bash
python3 -m agent_py_agent subagents --status BLOCKED
```

### 查看单个 run

```bash
python3 -m agent_py_agent subagent <run_id>
```

### 巡检 due-check

```bash
python3 -m agent_py_agent subagents-due-check
```

会检查：
- 工单文件是否缺失。
- DONE 是否缺证据。
- DONE 是否未验收。
- 是否长时间无心跳。
- 是否运行超时。
- 是否有 open capability request。
- 是否有 open capability gap。
- 通道是否 BROKEN / DEGRADED。

### 通道探测

```bash
python3 -m agent_py_agent subagents-probe <run_id>
```

当前 probe 先检查本地工单现场：
- 关键文件是否存在。
- JSON 是否可读。
- scratch 是否可写。
- probe 证据是否能写入。

未来可以接模型 session、ACP adapter、远端工具通道。

### 动作计划

```bash
python3 -m agent_py_agent subagents-plan-actions
python3 -m agent_py_agent subagents-plan-actions --root-id <root_run_id> --all
```

它只生成 dry-run 动作计划，不修改任务。
多棵真实任务树共用同一个 workspace 时，优先带 `--root-id`，避免其他测试树的问题混入当前动作计划。

常见动作：
- `repair_work_order`
- `reopen_for_evidence`
- `run_acceptance`
- `takeover_or_reassign`
- `recover_coordinator_leadership`
- `route_capability_request`
- `triage_capability_gap`
- `probe_or_repair_channel`

### 批量 coordinator 领导权恢复计划

如果一个 coordinator 挂掉，但它下面还有很多孩子，不要急着把所有孩子塞给同一个 leader。先生成分摊计划：

```bash
python3 -m agent_py_agent subagents-leadership-recovery-plan \
  --root-id <root_run_id> \
  --leader <leader_run_id_a> \
  --leader <leader_run_id_b> \
  --max-children-per-leader 3
```

这个命令只读 `coordinator_heartbeat_stale` 问题，并写 `subagent_leadership_recovery_plan.json` / `SUBAGENT_LEADERSHIP_RECOVERY_PLAN.md`。它不会修改 `parent_id`，不会标记旧 coordinator，也不会自动执行报告里的 future command；分批 apply 入口后续单独做。

按计划重挂指定 child 子集：

```bash
python3 -m agent_py_agent subagents-leadership-recovery-apply \
  --root-id <root_run_id> \
  --coordinator <stale_coordinator_run_id> \
  --leader <new_leader_run_id> \
  --child-run-id <child_run_id_a> \
  --child-run-id <child_run_id_b> \
  --apply
```

默认不传 `--apply` 时只是 dry-run。apply 前会校验 root 一致、leader 健康、child 仍是旧 coordinator 的直接孩子，以及可选 `--max-children-per-leader` 容量；任一校验失败就整次阻断，不做半截移动。旧 coordinator 还有剩余孩子时不会标记 `TAKEN_OVER`；所有孩子都移动完后才标记。

### 执行动作

默认 dry-run：

```bash
python3 -m agent_py_agent subagents-apply-actions --dry-run
```

显式 apply：

```bash
python3 -m agent_py_agent subagents-apply-actions --apply --action reopen_for_evidence --run-id <run_id>
```

接管任务：

```bash
python3 -m agent_py_agent subagents-apply-actions --apply \
  --action takeover_or_reassign \
  --run-id <run_id> \
  --take-over-by parent-supervisor \
  --locked-file src/example.py
```

接管会写 `TAKEOVER.md`，并把 final owner 切给接管者。

恢复 coordinator 领导权：

```bash
python3 -m agent_py_agent subagents-apply-actions --apply \
  --action recover_coordinator_leadership \
  --run-id <stale_coordinator_run_id> \
  --take-over-by <new_leader_run_id>
```

这个动作要求 `<new_leader_run_id>` 是已存在的 subagent run；它会把旧 coordinator 标记为 `TAKEN_OVER`，把其子任务重挂到新 leader 名下，并同步更新 `parent_id`、`depth`、`supervisor` 和 `final_owner`。孙级节点不会换父级，但 depth 会跟随刷新。

### 能力路由

先 dry-run：

```bash
python3 -m agent_py_agent subagents-route-capabilities --dry-run
```

只处理某个 run：

```bash
python3 -m agent_py_agent subagents-route-capabilities --dry-run --run-id <run_id>
```

真正 apply：

```bash
python3 -m agent_py_agent subagents-route-capabilities --apply --run-id <run_id>
```

命中时：
- 创建 `CapabilityGrant`。
- request 标记为 `GRANTED`。
- grant 合并进 `allowed_skills` / `allowed_tools`。
- 写全局审计日志和任务 `WORK_LOG.md`。

未命中时：
- 创建 `CapabilityGap`。
- request 标记为 `GAP`。
- 写审计日志。

### 生成执行上下文

```bash
python3 -m agent_py_agent subagent-context <run_id>
```

输出：
- `execution_context.json`
- `EXECUTION_CONTEXT.md`

### 运行 runner

默认 dry-run：

```bash
python3 -m agent_py_agent subagent-run <run_id>
```

这只会生成：
- runner prompt。
- runner result。
- 工单日志。

不会调用模型。

真正执行：

```bash
python3 -m agent_py_agent subagent-run <run_id> --execute
```

执行前默认 channel probe。

如果通道 BROKEN：
- 不调用模型。
- run 标记 `CHANNEL_ERROR`。
- 记录原因。

如果通道可用：
- 读取 execution context。
- 只注入 allowed tools。
- 调用模型。
- 解析 `[SUBAGENT_RESULT]`。
- 写回工单。

## Runner 输出协议

模型最后必须输出：

```text
[SUBAGENT_RESULT]
{
  "status": "AWAITING_ACCEPTANCE",
  "summary": "本轮完成或卡住的摘要",
  "used_tools": [],
  "used_skills": [],
  "evidence": [
    {
      "kind": "command",
      "summary": "验证摘要",
      "command": "",
      "path": "",
      "url": "",
      "ok": true
    }
  ],
  "capability_requests": [
    {
      "problem": "缺少什么",
      "needed_capability": "能力名",
      "expected_output": "希望得到什么",
      "tried": [],
      "evidence": [],
      "constraints": {}
    }
  ],
  "artifacts": [
    {
      "path": "产物路径",
      "kind": "file|report|log",
      "summary": "产物说明"
    }
  ],
  "tests": [
    {
      "name": "测试名称",
      "command": "运行命令",
      "ok": true,
      "summary": "测试结果摘要"
    }
  ],
  "patches": [
    {
      "path": "改动文件",
      "status": "applied|planned|blocked",
      "summary": "改了什么或准备改什么"
    }
  ],
  "lessons": [
    "可沉淀经验，未来可能变成 skill 或规则"
  ],
  "next_actions": [
    "建议父代理下一步动作"
  ],
  "blocked_reason": "",
  "failure_type": ""
}
[/SUBAGENT_RESULT]
```

### 字段含义

`status`

runner 不应该直接让任务变成 DONE。

推荐：
- `AWAITING_ACCEPTANCE`：任务执行完，等待验收。
- `BLOCKED`：缺能力、缺上下文、缺权限或遇到明确阻塞。
- `FAILED`：执行失败。

`summary`

给人看的本轮摘要。

`used_tools` / `used_skills`

只能填写 execution context 授权的能力。

如果模型填了未授权工具：
- 不会进入 `used_tools`。
- 会写入 `output.json.structured_output.ignored_unauthorized_tools`。

`evidence`

会写入 `VerificationEvidence`。

常见 kind：
- `command`
- `file`
- `url`
- `log`
- `note`

`capability_requests`

会自动写成 open `CapabilityRequest`。

用于表达：
- 缺工具。
- 缺 skill。
- 缺权限。
- 缺上下文。
- 当前授权不足以完成验收。

`artifacts`

产物记录。

它只记录事实，不保证文件已经存在。验收器后续需要检查。

`tests`

测试记录。

可以记录命令、结果、摘要。

`patches`

补丁意图或补丁状态。

当前不会自动 apply patch。

推荐 status：
- `applied`：已经通过授权路径完成。
- `planned`：建议父代理或集成器后续处理。
- `blocked`：需要能力或权限。

`lessons`

可沉淀经验。

当前会写入 `output.json` 和 `DEBRIEF.md`；打开 `enable_self_learning=true` 后会生成 learning draft 候选，但不会自动生成正式 skill。

`next_actions`

给父代理或下一层调度器看的建议。

如果同时存在 capability request，系统级 `next_action` 会优先是 `route_capability_request`。

## 写回规则

runner 执行后：

```text
evidence -> task.evidence
capability_requests -> task.capability_requests
used_tools -> task.used_tools，未授权项忽略并审计
used_skills -> task.used_skills，未授权项忽略并审计
artifacts -> output.json.artifacts
tests -> output.json.tests
patches -> output.json.patches
lessons -> output.json.lessons + DEBRIEF.md；enable_self_learning=true 时另生成 learning draft 候选
next_actions -> output.json.next_actions + DEBRIEF.md
blocked_reason -> output.json.blockers + RUNNER_RESULT.md
```

runner 不会：
- 自动标记 DONE。
- 自动 apply patches。
- 自动生成正式 skill。
- 自动扩大 allowed tools。

## Patch 审核

默认 dry-run：

```bash
python3 -m agent_py_agent subagents-patches
```

指定 run：

```bash
python3 -m agent_py_agent subagents-patches --run-id <run_id>
```

真正写回：

```bash
python3 -m agent_py_agent subagents-patches --apply --run-id <run_id>
```

审核器会检查：
- `output.json.patches` 是否存在需要审核的记录。
- patch 状态是否只使用 `applied` / `planned` / `blocked`。
- `planned` / `blocked` patch 不能审核通过。
- 未知状态 patch 不能审核通过。
- 只有全部 patch 都是 `applied` 时，才会写回 `review_status=APPROVED`。

输出：
- 全局 `subagent_patch_review_report.json`
- 全局 `SUBAGENT_PATCH_REVIEW.md`
- 单任务 `reports/patch_review.json`
- 单任务 `PATCH_REVIEW.md`
- apply 时追加 `subagent_patch_review_log.jsonl` 和 `PATCH_REVIEW_LOG.md`

注意：
- patch 审核器只审核 runner 已声明的 patch 状态。
- 当前不会自动应用未知 diff 或改动文件。
- applied patch 如果没有 `review_status=APPROVED`，父代理验收会继续阻断。

## 父代理调度

完整 CLI 参数手册见 [CLI_REFERENCE.md](CLI_REFERENCE.md)。

默认 dry-run：

```bash
python3 -m agent_py_agent subagents-dispatch
```

真正写回低风险动作、能力路由、patch 审核和验收：

```bash
python3 -m agent_py_agent subagents-dispatch --apply
```

真正调用 runner 模型：

```bash
python3 -m agent_py_agent subagents-dispatch --apply --execute-runners
```

watch 模式：

```bash
python3 -m agent_py_agent subagents-dispatch --watch --interval 30
```

配置驱动前台 daemon：

```bash
python3 -m agent_py_agent daemon
```

`daemon` 读取 `agent_config.yaml` 里的 `daemon_*` 配置，适合把常驻参数收进配置文件，日常启动时少打长命令。

配置分两层：
- 子代理用户层配置：默认只暴露 `enable_subagents`、`subagent_mode`、`max_subagents`、`subagent_workspace`、`subagent_role_template_dirs`、`subagent_debug_trace_level` 和父级验收两项。普通用户不需要判断每个角色用什么工具、上下文给多少、一次派几个叶子。
- 能力路由配置：`capability_config.yaml` 默认只保留开关、上抛层数和授权过期。具体给哪个子代理什么工具，默认由任务包、角色模板和 LLM 判断，不要求用户逐个填写。
- 当前前台 daemon 高级参数：`daemon_max_runners: "auto"` 会先映射成保守值 1；`daemon_max_cycles=0` 表示持续运行；`daemon_limit=0` 表示不限制记录条数；`daemon_max_cards=0` 表示不限制能力卡数量；`daemon_interval=0` 通常只用于测试或单轮验证。

父代理 planner 模式：

```bash
python3 -m agent_py_agent subagents-dispatch --watch --planner --interval 30
```

`--planner` 会先跑 heartbeat gate：只要存在 active task、due-check issue、action item、runner candidate、patch review、acceptance、open capability request/gap，就调用父代理 LLM planner。模型在 gate 非空时只返回 `HEARTBEAT_OK` 会被标为失败。

测试 watch 一轮：

```bash
python3 -m agent_py_agent subagents-dispatch --watch --max-cycles 1 --interval 0
```

调度顺序：
- `due_check`：扫描工单风险。
- `action_apply`：执行或预览低风险动作。
- `capability_route`：处理 open capability request。
- `runner`：挑选可执行 run 生成上下文；只有 `--apply --execute-runners` 才调用模型。
- `parent_planner`：开启 `--planner` 后，有待处理事项时调用父代理 LLM，生成审计化行动建议和 runner 补充指令。
- `patch_review`：审核 runner 输出里的 patch 记录。
- `acceptance`：把通过验收的 run 收口到 `DONE/VERIFIED`。

输出：
- 全局 `subagent_dispatch_report.json`
- 全局 `SUBAGENT_DISPATCH.md`
- apply 时追加 `subagent_dispatch_log.jsonl` 和 `DISPATCH_LOG.md`
- watch 模式写 `subagent_dispatch_watch_report.json` 和 `SUBAGENT_DISPATCH_WATCH.md`
- watch 模式追加 `subagent_dispatch_watch_log.jsonl` 和 `DISPATCH_WATCH_LOG.md`
- watch 模式持续更新 `subagent_dispatch_watch_heartbeat.json`
- planner 模式写 `parent_planner_report.json`、`PARENT_PLANNER.md`、`parent_planner_prompt.md`、`parent_planner_response.md`、`parent_planner_log.jsonl` 和 `PARENT_PLANNER_LOG.md`

注意：
- `subagents-dispatch` 默认不是常驻进程，只执行一轮。
- `--watch` 会持续循环；`--max-cycles 1` 可用于 CI 和人工安全验证。
- `daemon` 是配置驱动的前台常驻入口，当前还不是后台 gateway/service。
- watch 会创建 `subagent_dispatch_watch.lock`，阻止两个父代理同时调度同一批工单。
- `--apply` 会写审计日志，但默认不调用模型 runner。
- `--execute-runners` 必须和 `--apply` 一起使用，才会请求真实模型 API。
- `--planner` 可能请求真实模型 API，用来避免 watch 只是空心 heartbeat。
- 如果确认旧 lock 是异常退出残留，可以用 `--force-lock` 覆盖。
- 独立 daemon 可以在这条稳定的 watch 命令之上再实现。

## 父代理验收

默认 dry-run：

```bash
python3 -m agent_py_agent subagents-acceptance
```

指定 run：

```bash
python3 -m agent_py_agent subagents-acceptance --run-id <run_id>
```

真正写回：

```bash
python3 -m agent_py_agent subagents-acceptance --apply --run-id <run_id>
```

验收器会检查：
- 工单现场是否完整。
- run 是否处于 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`。
- channel 是否不是 `BROKEN`。
- runner 结构化输出是否可解析。
- 是否至少有一条 ok evidence。
- 是否没有失败 evidence。
- 是否没有 open capability request / gap。
- `output.json.blockers` 是否为空。
- `output.json.tests` 是否没有失败项。
- `output.json.patches` 是否没有 `planned` / `blocked` 未处理项。
- `output.json.patches` 是否没有未知状态项。
- `applied` patch 是否已经通过 patch 审核。

输出：
- 全局 `subagent_acceptance_report.json`
- 全局 `SUBAGENT_ACCEPTANCE.md`
- 单任务 `reports/acceptance_review.json`
- 单任务 `ACCEPTANCE_REVIEW.md`
- apply 时追加 `subagent_acceptance_log.jsonl` 和 `ACCEPTANCE_REVIEW_LOG.md`

写回规则：
- 通过并 apply：`DONE + VERIFIED`
- 不通过并 apply：`BLOCKED + FAILED`
- dry-run：只写报告，不修改任务状态。

## 状态流

常见状态：

```text
PLANNING
RUNNING
BLOCKED
AWAITING_ACCEPTANCE
DONE
FAILED
TIMEOUT
CHANNEL_ERROR
TAKEN_OVER
```

当前 runner 真执行成功后：

```text
AWAITING_ACCEPTANCE + NEEDS_ACCEPTANCE
```

如果结构化输出有 `capability_requests` 或 `blocked_reason`：

```text
BLOCKED + UNVERIFIED
```

如果结构化 JSON 解析失败：

```text
BLOCKED + UNVERIFIED
failure_type = structured_output_parse_error
```

## 防 Fake Done 规则

硬规则：
- 子代理没有证据不能直接 DONE。
- runner 不直接 DONE。
- DONE 任务如果 evidence 数量不足，会被 due-check 标红。
- DONE 但 verification_status 不是 VERIFIED，也会被 due-check 标红。

验收建议：
- 不只检查文件存在。
- 要检查真实入口、命令结果、日志、URL、截图或用户路径。
- 多子代理并行后，要做统一集成验证。

## 能力路由策略

当前策略：
- 子代理只提交 capability request。
- 父代理用 Capability Router 查 skill/tool card。
- 命中则 grant。
- 未命中则 gap。
- 中间层不需要展开 skill 正文，只转发 card / grant。

这样可以支持未来 100+ 子代理场景：
- 下级不需要知道全局能力宇宙。
- 上级拥有更大权限和更完整能力索引。
- 缺能力可以逐层上抛。
- 找到能力后沿链路下发。

下一阶段要接入受控 shell / MCP / 外部工具：
- 子代理不能直接拿裸 `exec`，只能通过受控 gateway 提交 shell/tool 请求。
- Capability grant 决定能不能用、能用哪些命令/工具、在哪些目录/域名/预算内用。
- Gateway 决定怎么执行、如何限制 cwd/path/network/output、如何写审计和 trace。
- 子代理不直接拿 `rm`；删除类需求走 task-local `trash/`，并写 manifest 以便恢复和审计。
- 本机新增工具、Playwright/Chrome tools、curl、日志分析 CLI、MCP 工具和 skill 自带脚本都应注册成 capability/tool card，再由父级按需 grant。
- 大输出默认不完整外置：stdout/stderr、artifact 和任务累计输出必须有预算；大日志用 `rg`/`tail`/`head`/slice/采样/索引，不允许子代理无脑 `cat` 1G/1T 文件。
- 找不到解决办法时必须写 capability gap / finding / shared blackboard / skill_spark 候选，不能让问题只停在模型自然语言里。

## 安全边界

默认安全行为：
- `subagent-run` 默认 dry-run。
- `subagent-run --execute` 才调用模型。
- `subagent-run --execute` 会走 runner worker timeout 边界；`runner_timeout_seconds` 为固定值时用固定值，为 `auto` 时按任务规模和动态 timeout 配置计算；`off` / `none` / `disabled` / `0` 表示不套外层超时，适合真实长任务测试。若同时开启 `subagent_debug_trace_level=3`，debug trace 会记录 `runner_model_request_started`、`runner_model_response_received`、`runner_model_request_failed`、`runner_tool_call_started`、`runner_tool_call_finished`，用于判断卡在模型请求、模型响应后处理还是工具调用阶段；后续 watchdog 仍负责把长期无进展变成可恢复动作。
- 执行前默认 probe。
- 工具有 allowlist。
- 未授权工具调用会失败。
- patches 不自动 apply。
- lessons 不自动写 skill。
- 自学习默认关闭。

## 当前还缺什么

优先级高：
- worker / session pool 升级：当前已有保守 runner 并发线程池，但还缺真正的进程级 worker pool、session pool、启动速率控制和长期心跳治理。
- subagent 独立服务化：当前 `daemon` 是前台常驻 dispatch；还缺 subagent 自身的系统服务封装、外部停止控制和更正式的 scheduler。
- 验收层增强：从 `output.json.tests` / `artifacts` 自动生成验收任务，并把证据事实、worker 自述和父级结论分层展示。
- patch 集成器深化：当前已有 patch review/apply service，后续还要按权限、owner、审核结果和集成测试做更完整的受控集成验收。
- learning draft 提升链路：当前 `enable_self_learning=true` 会生成候选草稿；还缺从 accepted draft 到正式 skill / rule / profile 的人工确认提升流程。
- 跨层能力上抛：父代理找不到时继续向爷代理或更高层抛。
- ACP / 外部 agent session：接远端执行器或外部 agent session；现有 adapter/file 和 QQ/飞书通道不等于完整 ACP 执行器。

## 睡前检查清单

一轮开发收尾时建议做：

```bash
python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py
python3 -m agent_py_agent --help
python3 -m agent_py_agent subagent-run --help
python3 -m agent_py_agent subagents-patches --help
python3 -m agent_py_agent subagents-dispatch --help
git diff --check
git status --short
```

收口时运行完整 `python3 agent_py_agent/tests/run_tests.py`，它会触发真实 API，并使用临时配置隔离测试数据。
