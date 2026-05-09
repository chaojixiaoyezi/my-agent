# Subagent 质量契约与受控施工队设计

状态：部分落地

相关文档：
- `DESIGN_LEDGER.md`
- `SUBAGENT_RUNBOOK.md`
- `TEST_CHECKLIST.md`

## 背景

真实使用中发现，子代理“小傻妞”不是执行能力不足，而是经常只收到动作目标，没有收到成品质量目标。

主代理知道用户审美、历史成功样本、交付红线和当前坑点；子代理如果只收到“下载 / 翻译 / 生成 / 检查”，容易把文件存在、命令成功、页数匹配当成完成。高质量 PDF、文档、报告、日志分析和代码交付任务，真正困难的部分往往不是命令执行，而是边界判断和质量裁决。

典型失败点：
- 把“文件生成成功”当成“任务成功”。
- 用机器指标证明“不坏”，但没有证明“好到能交付”。
- 在主论文 / 附录、正文 / 参考文献、应翻译 / 应保留、原文问题 / 工具破坏之间做机械判断。
- 只看页数、存在性、命令退出码，不看用户真正会打开检查的页面或结果。
- 子代理汇报 PASS 后，父会话没有做反验收，低质量结果被放大成最终交付。

核心判断：
- 解决办法不是少派子代理，而是改变使用方式。
- 子代理从“独立负责人”降级为“受控施工队”。
- 子代理负责生产材料、局部检查、找问题和修指定缺陷。
- 父会话保留产品负责人职责：定义标准、拆分边界、反验收材料、判断质量、决定交付。

## 痛点到解决项映射

这批设计必须一直对着真实痛点验收，不能只变成漂亮架构。

可以工程化解决的痛点：
- 目标质量没有传下去：用 `QualityContract` 把用户可见目标、成功样本、坏版定义、禁止交付项、抽查计划和最终裁决者结构化传给每个 worker。
- 子代理把“生成成功”当“任务成功”：子代理默认只能输出 `AWAITING_REVIEW` / `NEEDS_REPAIR`，不能写最终 `DONE` / `VERIFIED`。
- 模糊边界处机械判断：用 quality profile 写清主任务/附录、正文/参考资料、应处理/应保留、工具破坏/原始缺陷等边界规则。
- 只做机器验收：父会话生成 `acceptance_plan`，要求抽查真实产物和高风险位置，不能只看文件存在、页数、退出码。
- 子代理 PASS 被直接相信：所有子代理汇报都进入 `acceptance_report`，作为待审核材料，不作为最终结论。
- 长 prompt 稀释重点：使用核心规约包 + 当前任务包 + 角色专用包，而不是给每个子代理无脑塞全量 50K。
- 多个“平行主我”标准不一致：workflow 只生成临时职责和写入边界，父会话保留唯一产品负责人和最终验收权。
- 成功样本丢在聊天记忆里：把 DeepSeek V19 这类成功经验沉淀为 quality profile，可被后续任务引用。
- 用户不想显式写派工模板：默认开启 workflow router，让用户只表达任务目标，系统自动选择模板、拆 worker、写证据要求。

不能完全自动决定、仍需用户或父会话表达的内容：
- 最终交付给谁看，以及“正式交付”的质量阈值。
- 速度、成本、质量之间的取舍。
- 哪个历史版本或样本最值得对标。
- 哪些降级绝对不可接受。
- 是否允许联网、真实模型调用、长任务后台运行和高成本返工。

设计验收标准：
- 如果一个方案只能让子代理“更会干活”，但不能阻止它 fake done、机械 PASS 或自判完成，就不算解决核心问题。
- 如果一个方案需要用户每次手写复杂派工模板，用户少说目标就没有达成。
- 如果一个方案让所有 worker 都像总负责人一样各自裁决，必须回退为父会话统一裁决。

## 设计目标

让用户少说，但系统不偷懒。

用户应该专注任务要求、灵感和偏好，不需要显式说明怎么拆 producer / critic / reviewer、怎么写模板、怎么落文件、怎么安排反验收。系统需要自动完成这些工程化动作，并把关键质量标准传递给子代理。

目标效果：
- 子代理不会自己定义完成标准。
- 子代理输出默认是待审核材料，而不是最终结论。
- 高质量任务默认带质量契约、证据要求、抽查计划和反验收。
- 主代理能用同一套结构比较子代理产物、挑错结果、修复结果和最终验收。
- 成功样本能沉淀成 profile，后续同类任务自动复用。

## QualityContract

`QualityContract` 是父会话写给子代理和验收链路的质量契约。它不只是 prompt 文案，而应该成为任务对象或 execution context 的结构化字段。

建议字段：

```text
quality_contract:
  user_visible_goal: 用户最终想拿到什么
  benchmark_sample: 对标哪个成功样本或质量 profile
  quality_bar: 什么算好、完整、可交付
  failure_conditions: 什么算坏版
  forbidden_delivery: 什么情况绝对不准交
  must_check: 必须检查哪些内容
  sampling_plan: 前/中/后/边界/高风险区域怎么抽查
  evidence_required: 必须落哪些证据
  risk_report_required: 不确定点必须怎么报告
  allowed_degradation: 哪些降级可接受
  final_judge: 谁能写 DONE / VERIFIED
```

约束：
- 子代理不能修改 `quality_bar` 和 `final_judge`。
- 子代理不能把自己的总结当最终验收。
- 子代理如果发现契约不完整，应输出风险，而不是擅自降低标准。
- 高质量任务里，`DONE` / `VERIFIED` 只能由父会话或明确的最终验收步骤写入。

## Context Pack

不要给每个子代理无脑塞完整 50K prompt。更稳的做法是给“足够像主会话、但仍然聚焦”的上下文包。

三层上下文：
- 核心规约包：所有子代理都必须知道的规则，例如不能 fake done、必须落证据、不能自己宣布完成、主会话才是最终裁判。
- 当前任务包：本次任务目标、用户原话、目录、已知坑、成功样本、失败样本、质量契约和当前状态。
- 角色专用包：按职责给局部规则，例如收集型、执行型、挑错型、修复型、验收型。

需要记录 `context_manifest`：

```text
context_manifest:
  core_pack_version: 固定规约版本
  task_pack_refs: 本轮任务上下文来源
  role_pack: 当前角色专用包
  required_read_paths: 子代理必须读的文件
  quality_contract_ref: 质量契约来源
  omitted_context: 故意没给的上下文和原因
  token_budget: 本轮上下文预算
```

设计原则：
- 找 URL 的子代理不需要完整排版细节。
- 排版 QA 的子代理不需要所有工具安装史。
- 翻译质量检查子代理必须知道“不准大片英文正文”和成功样本。
- 上下文给得越多，越需要明确本轮最重要的 5-10 条规则，避免重点被稀释。

## 角色拆分

高质量任务不应该只派一个“全能子代理”从头做到尾。

建议角色：
- `producer`：生产材料，例如下载、生成、翻译、跑工具、写候选清单。
- `critic`：专门挑错，目标是找失败点，不是证明完成。
- `repairer`：只修指定问题，不能顺手重做或扩大范围。
- `reviewer`：按质量契约做结构化审核，但仍不代替父会话的最终审美和裁决。

输出状态：
- `AWAITING_REVIEW`：子代理产物已提交，等待检查。
- `NEEDS_REPAIR`：发现具体问题，需要返工。
- `REPAIR_SUBMITTED`：修复已提交，等待复查。
- `PARENT_ACCEPTED`：父会话验收通过。
- `DONE` / `VERIFIED`：只能由父会话或最终验收链路写入。

## Worker 拆分算法

拆 worker 的目标不是“同时开很多会话”，而是把大任务切成局部可检查、写入边界清楚、能并行推进的施工单。

推荐顺序：

1. 先写质量契约。
   - 用户可见目标是什么。
   - 哪些情况不准交。
   - 必须产出哪些证据。
   - 必须跑哪些测试。
   - 谁拥有最终裁决权。

2. 再按写入范围拆。
   - CLI worker 只改 CLI 和 CLI 测试。
   - storage worker 只改 storage 和 storage 测试。
   - detector worker 只改 detector / case / report。
   - docs worker 只改文档和索引。
   - reviewer worker 默认只读，不改文件。

3. 再按产物类型拆。
   - producer 负责产出代码、fixture、文档或报告。
   - critic 负责找问题，不修。
   - repairer 只修父会话指定的问题。
   - reviewer 按质量契约验收，但不能宣布最终交付。

4. 最后由父会话集成。
   - 看 diff。
   - 跑组合测试。
   - 复现关键链路。
   - 更新验收记录。
   - 决定是否提交。

一个 worker 派工单必须包含：
- 当前仓库路径。
- 它不是唯一 worker，不能回滚别人改动。
- 明确写入范围。
- 明确禁止修改范围。
- 明确验收测试。
- 明确输出格式：改动文件、测试结果、残留风险。

适合交给 worker 的任务：
- 独立 CLI 子命令。
- 独立 parser / adapter / backend。
- fixture 和测试补充。
- 局部 bug 修复。
- 局部文档或索引维护。
- 只读 review / critic。

不适合直接交给 worker 独立裁决的任务：
- 最终质量标准。
- 用户是否会满意。
- 是否引入重依赖。
- public API 的长期取舍。
- 多模块大重构的最终边界。
- 高审美或高风险交付的最终验收。

## 反验收机制

子代理汇报不是结论，只是待审核材料。

父会话或 critic 应该反着验：
- 它说清单完整，就查有没有漏项、错链和误收。
- 它说文件好了，就抽前半段、后半段、附录、图表密集页、参考文献页。
- 它说 QA PASS，就看它到底测了什么，证据是否覆盖高风险点。
- 它说没有问题，默认还要做一次独立抽查。

反验收产物应该落盘：

```text
acceptance_report:
  checked_artifacts: 检查了哪些真实产物
  checked_samples: 抽查了哪些页、文件、日志片段或功能路径
  evidence_paths: 截图、日志、命令输出、diff、测试结果路径
  pass_fail_by_item: 每个检查项 PASS/FAIL
  unresolved_risks: 未解决风险
  parent_decision: 父会话最终决定
```

## 成功样本库

成功样本应该变成质量 profile，而不是留在聊天记忆里。

示例 profile：
- `pdf_translation_deepseek_v19`：中文覆盖、目录处理、正文/附录/参考文献策略、排版观感、contact sheet QA。
- `log_analysis_first_response`：日志输入识别、时间线、严重性分类、证据引用、可执行建议。
- `code_patch_small_safe`：小范围补丁、最小测试、diff 可读性、兼容入口不破坏。

profile 应包含：

```text
quality_profile:
  name: profile 名称
  applies_to: 适用任务类型
  benchmark: 对标样本
  must_do: 必须做
  must_not_do: 禁止做
  sampling_defaults: 默认抽查策略
  evidence_defaults: 默认证据要求
  critic_prompt_rules: 挑错角色默认规则
```

## 内置默认、配置开关、用户输入

可以直接内置的默认行为：
- 子代理不能 fake done。
- 子代理不能自己宣布最终完成。
- 子代理必须落证据和风险。
- 高质量任务默认生成 `QualityContract`。
- 高质量任务默认 producer / critic 分离。
- critic 的目标是找问题，不是给施工方背书。
- 父会话最终验收前，不能把子代理 PASS 当最终 PASS。

适合做成配置开关：
- `quality_mode`：off / standard / strict。
- `auto_critic`：是否自动加挑错子代理。
- `auto_repair`：是否允许自动返工。
- `review_rounds`：最多几轮 review / repair。
- `context_budget`：上下文预算。
- `full_context_for_subagents`：是否允许近似全量上下文。
- `require_human_final_confirmation`：是否必须用户亲自确认最终交付。
- `quality_profile_override`：手动指定 profile。
- `dispatch_topology`：单代理、producer+critic、producer+critic+reviewer 等拓扑。

仍需要用户或父会话表达的内容：
- 对标哪个成功样本。
- 交付对象是谁，是自己看、正式提交，还是批量内部材料。
- 速度、成本、质量之间怎么取舍。
- 哪些区域必须好看、完整、舒服。
- 哪些降级不可接受。
- 是否允许联网、真实模型、长任务和后台执行。
- 是否必须用户亲自最终确认。

## 用户少说模式

用户只说任务本身时，系统应自动推断：
- 任务类型和风险等级。
- 是否需要 quality profile。
- 是否需要独立 critic。
- 是否需要抽查计划。
- 哪些上下文包必须给子代理。
- 哪些结果必须落证据。

父会话生成派工时，应该自动补齐：

```text
dispatch_plan:
  task_type: 自动识别的任务类型
  quality_profile: 选择的质量 profile
  quality_contract: 生成的质量契约
  roles: producer / critic / reviewer / repairer
  context_packs: 每个角色拿到的上下文
  acceptance_plan: 父会话验收步骤
```

这让用户不用说“请派一个施工子代理，再派一个挑错子代理，再写验收文件”。用户只需要表达目标和偏好，系统负责把它翻译成受控派工流程。

## Workflow 模板层

Workflow 模板应该描述“任务如何拆、证据如何落、父会话如何验收”，而不是固定定义一批永久子代理角色。

不要内置固定角色：
- 不固定写死“研究员 / 工程师 / 审查员 / PM / QA”。
- 每次任务运行时，根据模板 phase 动态生成临时 worker 职责。
- 同一个 `critic` phase 在 PDF 任务里可能是排版挑刺，在代码任务里可能是回归风险 reviewer，在日志任务里可能是证据链审计。

模板应该定义：
- 适用任务类型。
- 不适用场景。
- phase 拓扑，例如 single、parallel、producer->critic->repair、map-reduce。
- 每个 phase 的输入、输出、证据和禁止事项。
- worker 写入边界和冲突规则。
- 父会话验收清单。
- 失败后是否允许自动 repair。

建议配置：

```text
subagents:
  workflow_mode: auto   # auto | manual | off
  builtin_workflows: true
  user_workflow_dirs:
    - .agent/workflows/user
```

模式语义：
- `auto`：默认模式。系统自动选择 workflow、生成派工单和父会话验收计划。
- `manual`：系统只推荐 workflow，不自动执行，等待用户或父会话确认。
- `off`：关闭模板派工，保留自由派工和裸 subagent 行为。

模板加载优先级：
1. 用户明确指定的模板。
2. 用户自定义模板。
3. 内置模板。
4. 没有命中时回退到普通子代理派工。

目录建议：

```text
.agent/workflows/
  builtin/
    single_worker_verified.yaml
    code_feature_split.yaml
    bugfix_regression.yaml
    producer_critic_repair.yaml
    parallel_research_synthesis.yaml
    map_reduce_batch.yaml
    security_investigation.yaml
    design_then_build.yaml
  user/
    my_pdf_translation_v19.yaml
    my_log_analysis_strict.yaml
```

约束：
- 内置模板最好只读；用户修改时复制到 `user/` 目录，避免升级覆盖。
- 用户模板可以覆盖内置模板，但必须通过 schema 校验。
- 每次自动选择 workflow，都要落盘解释：为什么选它、拆了哪些 worker、父会话保留哪些验收权。

首批内置模板建议：
- `single_worker_verified`：简单局部任务，一个 worker 做，必须交证据和测试。
- `code_feature_split`：代码功能开发，按写入边界拆 worker，父会话集成测试。
- `bugfix_regression`：bug 修复，复现、定位、修复、回归分离。
- `producer_critic_repair`：高质量交付，生产、挑错、返修、父验收。
- `parallel_research_synthesis`：调研任务，多方向并行，父会话综合。
- `map_reduce_batch`：批处理任务，文件/日志/PDF 分片处理再汇总。
- `security_investigation`：安全日志分析，证据优先，所有结论可追溯。
- `design_then_build`：需求不清时先 SPEC，再施工。

模块边界：
- `WorkflowRouter`：根据任务、风险和用户开关选择 workflow。
- `WorkflowTemplateStore`：加载内置模板和用户模板，处理覆盖和校验。
- `WorkflowCompiler`：把模板、任务上下文和质量契约编译成 worker 派工单。
- `WorkflowPolicy`：控制 auto/manual/off、风险任务确认、模板覆盖和自动返修上限。
- `ParentAcceptancePlanner`：生成父会话验收清单和反验收动作。

## 分阶段开发

Phase 1：workflow 基础骨架（已落地）
- 解决问题：先把 workflow、模板和质量契约做成可配置骨架，避免用户每次手写派工，也避免子代理拿不到统一规约后自由发挥。
- Worker A：配置与开关。
  - 解决问题：让用户能选择自动套模板、手动确认或完全关闭，避免系统强迫所有任务进入同一种派工方式。
  - `workflow_mode: auto | manual | off`。
  - `builtin_workflows`。
  - `user_workflow_dirs`。
  - doctor 检查和配置文档。
- Worker B：模板 schema 和加载器。
  - 解决问题：让内置模板和用户模板可校验、可覆盖、可解释，避免模板随意写坏后静默影响派工质量。
  - workflow YAML/JSON schema。
  - 内置模板目录。
  - 用户模板目录。
  - 模板覆盖规则和错误提示。
- Worker C：Quality Contract / Context Pack。
  - 解决问题：把父会话质量标准、上下文边界和最终裁决权传给 worker，避免 worker 只收到动作目标后 fake done。
  - 核心规约包。
  - 当前任务包。
  - 角色专用包。
  - `cannot_self_accept` 和 `parent_final_gate`。

落地记录：
- 配置字段：`subagent_workflow_mode`、`subagent_builtin_workflows`、`subagent_user_workflow_dirs`、`subagent_workflow_review_rounds`。
- 模板库：`agent_py_agent/agent/subagent_workflows/`，当前支持 JSON 内置模板和用户 JSON 覆盖模板。
- 任务结构：`SubAgentTask` / `SubAgentExecutionContext` 已接入 `QualityContract`、`ContextManifest` 和 `context_packs`。
- 验证：workflow 专项组合 14 passed，全量 `python -m pytest` 186 passed。

Phase 2：任务结构基础
- 解决问题：把质量契约写进任务结构和落盘上下文，解决上下文稀释、worker 自判完成、fake done 无证据的问题。
- 在 `SubAgentTask` 或 `execution_context` 增加质量契约字段。
- 增加角色字段，例如 producer / critic / repairer / reviewer。
- 增加 `context_manifest` 落盘。
- runner prompt 默认加入核心规约包。
- 子代理默认输出 `AWAITING_REVIEW`，不直接写最终完成。

Phase 3：Router / Compiler / Parent Gate
- 解决问题：让系统能按任务类型自动选路、生成派工和父会话验收门，减少用户手动拆任务，并防止机械 PASS 和 worker 自述被直接采信。
- Worker D：Workflow Router。
  - 解决问题：把用户自然语言任务稳定映射到合适 workflow，避免靠父会话临场感觉派错工或漏掉 critic。
  - 识别代码开发、bugfix、调研、批处理、高质量文档、安全日志、UI/frontend 等任务类型。
  - 根据 `workflow_mode` 决定自动执行、只推荐或关闭。
- Worker E：Workflow Compiler。
  - 解决问题：把抽象模板变成可执行施工单，避免 worker 拿到空泛任务说明、不知道写入边界、证据要求和禁止事项。
  - 把 workflow phase 编译成具体 worker prompt。
  - 自动填入写入范围、禁止范围、证据要求、测试命令和风险汇报格式。
- Worker F：Parent Gate / Acceptance Planner。
  - 解决问题：在派工前就生成父会话验收闸门，避免 worker 汇报 PASS 后父会话才临时想起要查什么。
  - 生成父会话验收清单。
  - 标出不能信 worker 自述的项。
  - 生成返工条件和最终交付条件。

Phase 4：Quality Profile
- 解决问题：把不同任务的验收标准显式化，避免代码、PDF、日志分析共用一套空泛标准导致检查漏项或只给形式化 PASS。
- 增加 `quality_profiles/` 或等价配置目录。
- 提供 PDF 翻译、日志分析、代码补丁三个首批 profile。
- 增加任务类型到 profile 的简单匹配。
- 允许父会话或用户覆盖 profile。

Phase 5：内置模板库
- 解决问题：补齐旧任务结构之外的可复用 workflow，让常见场景有现成拓扑、输出合同和验收合同，降低派工随意性。
- 并行编写首批 8 个模板。
- 每个模板必须包含适用场景、不适用场景、解决问题、phase 定义、worker 输出合同、parent 验收合同、示例任务和测试 fixture。
- 不急着一次写几十个模板；先让 8 个基础模板可组合、可验证。

Phase 6：自动编排
- 解决问题：把 producer、critic、reviewer、repairer 的协作固定成系统行为，避免用户必须手写编排，也避免同一个 worker 既生产又自我验收。
- dispatch 支持 producer / critic / reviewer 拓扑。
- 高质量任务默认 producer -> critic -> parent acceptance。
- critic prompt 使用更硬的挑错规则。
- repairer 只能修指定缺陷，不能扩大范围。

Phase 7：验收链路
- 解决问题：建立从质量契约到结构化验收报告的闭环，确保 `DONE` / `VERIFIED` 来自父会话验收，而不是 worker 总结里的机械 PASS。
- acceptance 按 `QualityContract` 和 `sampling_plan` 检查。
- 验收结果写结构化 `acceptance_report`。
- `DONE` / `VERIFIED` 只能在验收通过后写入。
- 子代理总结和验收结论分离展示。

Phase 8：入口自动化和可解释性
- 解决问题：把质量派工接入 chat / gateway / spawn 入口，在用户少说或不懂派工时自动选择 profile 和拓扑，同时让本轮契约、进度和开关可解释。
- chat / gateway / spawn 入口自动生成质量派工。
- 用户少说时默认选择合理 profile 和拓扑。
- 暴露少量开关，而不是让用户手写派工模板。
- 在状态和日志中展示本轮质量契约和验收进度。
- 新增 CLI：
  - `workflows list`
  - `workflows show <id>`
  - `workflows copy <id> <new_id>`
  - `workflows validate <path>`

Phase 9：真实任务接入
- 解决问题：用 LOG 模块验证整条链路不是纸面设计，覆盖自动路由、模板覆盖、质量契约注入、父会话验收落盘等真实交付风险。
- LOG 模块作为首个实战接入。
- 日志分析任务自动路由到 `security_investigation`、`code_feature_split` 或二者组合。
- 验证 auto/manual/off、用户模板覆盖、worker prompt 包含质量契约、父会话验收可落盘。

Phase 10：失败沉淀
- 解决问题：把 fake done、漏验收、上下文丢失、成功样本未复用等结果沉淀为 fixture / eval / profile 更新，防止同类失败反复出现。
- 把质量失败变成 fixture / eval。
- 接入 Live Lab 回归。
- 把成功样本和失败样本持续沉淀进 profile。
- 定期清理过时 profile，避免旧标准污染新任务。

## 验证策略

需要补的测试或检查：
- 创建子代理任务时，质量契约能落盘。
- 子代理 prompt 包含核心规约和角色专用规则。
- 子代理不能直接把高质量任务标成 `DONE`。
- producer / critic 可以分离运行，且 critic 输出 FAIL 不会被覆盖。
- acceptance 能读取 sampling plan 并生成结构化报告。
- 用户未指定派工方式时，入口能自动选择默认 profile 和拓扑。

真实场景验证：
- PDF 翻译类：抽第一页、中段、后半段、附录、参考文献、图表密集页。
- 日志分析类：抽异常时间线、错误栈、来源证据、建议动作。
- 代码补丁类：抽 diff 范围、测试结果、兼容入口、未解决风险。
## 2026-04-30 Implementation Note: Workflow Router / Compiler / Parent Gate

- 中文说明：这一片把“用户只说目标，系统自己选择派工方式”的第一刀做出来。Router 负责选 workflow，Compiler 把 workflow 阶段编译成 worker 任务规格，Parent gate 负责生成父级验收计划，防止 worker 自己说 PASS 就算完成。
- Status: Phase 3 first slice landed in code and focused tests.
- Solves: users can describe the task in natural language while the system selects a workflow template, compiles template phases into reviewable worker dispatch specs, and creates a parent-side acceptance plan before trusting worker output.
- Router: supports `auto`, `manual`, and `off`; honors explicit template ids; falls back safely; recognizes English and common Chinese code/quality task wording.
- Compiler: turns workflow phases into worker specs with dependencies, write boundaries, evidence requirements, non-rollback warnings, and `cannot_self_accept`.
- Parent gate: merges template acceptance and `QualityContract` checks, adds anti-worker-PASS checks, and requires a critic gate for producer/critic/repair workflows.
- Later status: real task creation and dispatch apply can now consume these plans. Remaining work is broader chat/gateway/spawn explanation, more built-in templates, and stronger structured acceptance reporting.

## 2026-04-30 Implementation Note: Workflow Planner Facade

- 中文说明：这一片把 router、compiler、parent-gate 的调用包成一个门面函数。父级不用每次手动串多个服务，只要调用 `plan_workflow_for_goal()` 就能拿到路由结果、模板、派工计划、验收计划和问题列表。
- Status: dry-run planning facade landed.
- Solves: parent code no longer has to call router, compiler, and parent-gate planner separately for every workflow experiment.
- API: `plan_workflow_for_goal()` returns one `WorkflowPlanningResult` with the route decision, selected template, dispatch plan, parent acceptance plan, and accumulated issues.
- Guardrail at landing time: it did not create or run subagents yet. Current code has since added a controlled apply path: task creation can persist a workflow plan, and dispatch apply in `auto` mode can materialize worker child runs.
- User effect: moves us closer to "user only states the goal" because the system can infer workflow shape and acceptance gates before asking the user for dispatch details.
- Remaining after later integration: expose a clearer explanation to users when `auto/manual/off` changes behavior, and keep extending template/profile coverage.

## 2026-04-30 Implementation Note: Workflow Plan CLI

- 中文说明：这一片把 workflow 预览暴露成 CLI。用户或父会话可以先看系统打算怎么拆任务、怎么验收，再决定是否真的创建子代理；这个命令本身不创建、不执行、不验收。
- Status: dry-run CLI preview landed.
- Command: `my-agent subagents-workflow-plan "<goal>" [--template-id <id>] [--json]`.
- Solves: users and the parent session can inspect the selected workflow, worker split, and parent acceptance checklist before creating any subagent task.
- Guardrail: the command does not create files, create subagents, call runners, or mark anything accepted.
- User effect: this is the first visible bridge from "user only states the goal" to "system explains the controlled dispatch plan".
- Later status: the apply path now exists through `create_run(... workflow_mode="plan|auto")`, `ensure_workflow_plan()`, and `realize_workflow_plan()`. Remaining work is richer policy/confirmation UX, stronger end-to-end tests, and clearer dispatch evidence for the exact plan used.

## 2026-04-30 Implementation Note: Preview Persistence

- 中文说明：这一片让 dry-run 预览可以显式保存到指定目录。好处是后续父级验收和调度记录能引用“当时具体看过哪份计划”，而不是只靠聊天记忆。
- Status: explicit dry-run preview persistence landed.
- Command: `my-agent subagents-workflow-plan "<goal>" --output-dir <dir>`.
- Solves: parent acceptance can now reference the exact JSON/Markdown workflow preview used before dispatch, instead of relying on memory of the conversation.
- Guardrail: persistence is opt-in and still does not create subagents or run workers.
- Remaining after this slice: keep linking real dispatch records to the selected workflow plan and any persisted preview artifacts when they exist.
