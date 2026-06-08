# Delivery Closeout

本文只描述当前主链路的验收和恢复建议。

## Structured Sources

- delivery closeout 的机器判断只读结构化合同、artifact refs、工具记录、文件事实和验证结果。
- `staging_contract.source_json_ref` 是主 staged checkpoint；如果 `collection_contract.source_json_ref`
  指向另一份 JSON，它也是结构化 collection source。
- 两类 JSON source 只要存在，都要走 checkpoint quality 检查，再生成 recovery action；不能只因为
  staging source 缺失，就忽略 collection source 里已经写下的 rows、columns 和 completion evidence。
- collection source 的缺列、数量不足、占位值、证据缺失等问题都应生成结构化 recovery action，
  供模型修复真实 checkpoint 后重新验收。
- 错误正文和普通 summary 只作为审计说明；不能从自然语言文本反推出验收状态。

## Coverage Freshness

- 如果存在显式结构化 `target_coverage_contract` 且 enforcement 为 `required`，closeout
  除了检查 coverage ledger 是否完成，还会检查最终交付物的工具写入时间是否晚于最后一次必要
  `read_file` 覆盖记录。
- 这条检查只比较工具账本里的 `created_at`、`run_id`、artifact provenance 和 coverage record；
  不扫描报告正文，不用中文关键词判断“有没有总结进去”。
- 如果交付物早于最后一次必要源码读取，closeout 会返回 `target_coverage_freshness_status.should_block=true`，
  要求模型更新最终交付物后重新 `submit_for_acceptance`；不要继续重复读已经完成覆盖的源码。
- 如果上一次 closeout 已经返回 `target_coverage_status.should_block=true`，下一轮模型不能在没有
  缺失源码目录 `list_files/read_file` 动作的情况下直接写最终产物或再次 submit。工具循环会追加
  `target-coverage-rework-guard` 上下文，让模型先按 repair hints 补覆盖。

## Coverage Evidence Projection

- 对 required `target_coverage_contract`，closeout 还会把 coverage ledger 中的
  `read_file` 源码证据与最终交付物正文做结构化一致性检查。
- 这条检查只提取机器型文件/模块 token，例如 `conversation_loop.py`、`src/Task.ts`、
  `cli/src/main.rs`，并比对当前 run 的 task output artifact；不会按中文描述、报告文风或
  “分析得够不够深”做语义评分。
- `target_coverage_contract.target_items[]` 里的结构化目标 label、项目目录名或明确 target id
  也是投影项。对 required 目标，最终交付物必须能看见对应目标名；只列零散源码文件名但漏掉
  项目/目标本身时，closeout 会要求返工。
- 如果大量已读取源码证据没有进入最终交付物，closeout 返回
  `target_coverage_projection_gate.status=NEED_REPAIR`，要求模型把对应文件、模块或
  source refs 写进最终报告后重新 `submit_for_acceptance`。
- 少量遗漏只作为 advisory finding，不把正常的大项目报告卡死。

## Task Progress Closeout

- `task_progress` 是当前 run 的结构化进度账本。普通 evidence 缺口、证据太少、
  覆盖说明不够细，可以作为 advisory finding 提醒模型补事实。
- 但 `submit_for_acceptance` / delivery closeout 是收口动作边界；如果当前 run 的
  `task_progress.items[]` 里仍有非关闭状态，closeout 必须返回 `NEED_REPAIR`，
  不得把当前任务标记为完成。
- 关闭状态只认当前协议里的 `done` / `skipped`。`completed`、`ok`、中文完成描述、
  报告正文里的“已完成”都不能被隐式当作关闭状态；模型需要把结构化状态改成协议值，
  或明确标成 `blocked` 并说明原因后再重新提交。
- 无 delivery contract 的 task output closeout 也必须执行同一条进度 gate；不能只因为
  task output 下出现一个报告文件就跳过进度账本。
- 如果 `task_progress.items[]` 的关闭项已经登记了 evidence，closeout 会检查当前
  task output 交付物是否包含这些 evidence 里的机器型文件/模块引用。这个检查只比对
  结构化账本里的 evidence token 和文件正文，不评价报告文风；缺失时返回 `NEED_REPAIR`，
  要求模型把来源文件、模块或 artifact ref 写进最终交付物后重新提交。

## Directory Coverage Materialization

- 从用户 prompt 自动派生源码目录 coverage 时，只能把真实源码项目目录放进 target items。
- task/output/data/memory/local_store/backup/.agent* 这类内部、生成、缓存或备份目录不能成为
  required coverage。
- 如果用户给的是一个项目集合目录，runtime 可以根据文件系统事实把其中像源码项目的子目录
  展开成 `target_coverage_contract.target_items[]`；如果只是单个项目目录，则只覆盖该项目本身。
- 这条规则只约束 coverage target 的派生，不靠自然语言判断任务是否完成，也不从“不要分析”
  这类普通话术反推机器状态。

## Output Path Materialization

- delivery materializer 只能把明确的输出文件路径放进 `artifacts`；用户要求阅读、参考、
  检查或提到的源码/草稿路径不是交付物。
- 如果用户明确要求最终报告但没有指定输出路径，报告 artifact 默认在当前 task workspace
  的 `output/` 下定位。用户用普通自然语言列出的报告维度、分析角度和对比口径是内容意图，
  不会自动升级成 `validation_contract.required_sections`。
- `validation_contract.required_sections` 和 `validation_contract.min_size` 只来自结构化产物合同；
  Markdown 最终交付物缺少结构化合同明确要求的章节或尺寸不足时，closeout 必须返工。
  `document_quality_contract` 仍然是内容质量 advisory，不和这些确定性格式要求混在一起。
- 同一需求里如果同时出现裸文件名 `report.md` 和更具体的同名路径
  `.../output/.../report.md`，裸文件名只当作引用，不再生成第二个 `artifacts[]` 条目。
- 这条规则只比较结构化路径形态，避免把“刚才的 report.md”解析成当前 workspace 根目录下的
  假交付物；不按普通自然语言推断验收状态。
- Markdown 最终交付物如果写出明确的本地源码路径，例如 `project/src/file.py` 或目录树里的
  具体文件项，artifact acceptance 会用当前任务的源码根/coverage root 做文件存在性检查。
  只有能锚定到源码根现有顶层目录的具体文件路径才会成为硬 finding；`TypeScript/Node.js`
  这类普通描述、未锚定的相对片段和自然语言说明不参与机器判断。
- 当前 task output/work 下同一路径已经存在时，模型后续省略 `write_file.mode` 的写入按
  续写处理：运行时会把它转成 append，避免长报告或过程事实分段时用半截内容覆盖已有
  任务产物。
- 这类隐式 append 会在工具结果中返回软反馈，提醒模型如果意图是替换干净最终版，下一
  次必须显式 `mode="overwrite"`。
- 如果确实要替换 task output/work 里的任务产物，工具调用必须显式传 `mode="overwrite"`；
  普通 workspace 文件仍保持 `write_file` 省略 mode 时覆盖的原语义。
- unclosed `write_file.content` 不是有效动作边界，不能写入 task output/work 或普通
  workspace 文件。运行时必须返回结构化 parse error 和 `write_recovery`，让模型改用完整
  闭合的 overwrite/append 小块继续。
- `write_recovery` 里的小块大小是恢复建议，不是流式硬截断值。完整闭合的中等长度写入
  应先走正常工具边界；只有超过共享 inline hard limit 或仍未闭合时，才进入结构化恢复。
- closeout 读取同一路径的最后一次 `write_file` 记录；如果历史记录或异常内部输入里仍出现
  `__partial_unclosed_write=true`，该 artifact 必须标为 invalid，主代理继续返工。无显式
  delivery contract 的 task output closeout 也适用同一条规则，不能因为 output 目录里出现了
  `.md` 报告文件，就把半截分片登记成 ready 交付物。
- 无显式 delivery contract 的 task output closeout 也要执行基础 artifact acceptance。
  Markdown 交付物如果结尾停在一个新标题、代码围栏未闭合，或者同一路径多次发生
  `TOOL_CALL_UNCLOSED` 后只用很小的 `overwrite` 分片覆盖，必须标为 invalid。
  这条只读取文件结构和工具记录，不根据报告正文里的普通自然语言判断“是否完成”。
