# Subagent 质量契约与受控施工队设计

状态：设计中

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

## 分阶段开发

Phase 1：任务结构基础
- 在 `SubAgentTask` 或 `execution_context` 增加质量契约字段。
- 增加角色字段，例如 producer / critic / repairer / reviewer。
- 增加 `context_manifest` 落盘。
- runner prompt 默认加入核心规约包。
- 子代理默认输出 `AWAITING_REVIEW`，不直接写最终完成。

Phase 2：Quality Profile
- 增加 `quality_profiles/` 或等价配置目录。
- 提供 PDF 翻译、日志分析、代码补丁三个首批 profile。
- 增加任务类型到 profile 的简单匹配。
- 允许父会话或用户覆盖 profile。

Phase 3：自动编排
- dispatch 支持 producer / critic / reviewer 拓扑。
- 高质量任务默认 producer -> critic -> parent acceptance。
- critic prompt 使用更硬的挑错规则。
- repairer 只能修指定缺陷，不能扩大范围。

Phase 4：验收链路
- acceptance 按 `QualityContract` 和 `sampling_plan` 检查。
- 验收结果写结构化 `acceptance_report`。
- `DONE` / `VERIFIED` 只能在验收通过后写入。
- 子代理总结和验收结论分离展示。

Phase 5：入口自动化
- chat / gateway / spawn 入口自动生成质量派工。
- 用户少说时默认选择合理 profile 和拓扑。
- 暴露少量开关，而不是让用户手写派工模板。
- 在状态和日志中展示本轮质量契约和验收进度。

Phase 6：失败沉淀
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
