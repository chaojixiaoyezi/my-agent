# Subagent Role Selection Strategy

## 目标

让主代理、coordinator、lead 这类派工角色在创建下级之前，先能用稳定规则判断“这件事该派谁”。角色模板本身仍然外置化，策略只负责选择和组合角色，不把每个模板的完整 prompt 常驻塞进上下文。

大白话：主代理要像知道自己家里有哪些工具一样，先知道有哪些常用角色、什么时候叫谁；真正开始派工时，再打开那个角色的详细说明。

## 常驻信息和按需信息

- 常驻信息：角色 id、中文名、适用场景、不适用场景、能力标签。
- 按需信息：默认工具、完整中文 prompt、输出合同、细节边界。
- 原则：普通聊天和简单任务只看常驻索引；进入派工或角色选择不确定时，才读取当前候选角色详情。
- 模板文件路径只用于开发者调试或账本排查，不进入模型常驻工具说明。

## 角色使用规则

| 角色 | 什么时候用 | 不该什么时候用 | 主要产出 |
| --- | --- | --- | --- |
| `coordinator` / `lead` | 任务需要拆分、多代理协同、跨阶段推进、广播纠偏、接管失联分支时 | 只有一个明确小改动，直接 worker 能做完时 | 子任务计划、派工、进度汇总、阻塞报告 |
| `worker` | 明确要实现代码、修改文件、生成结构化产物时 | 只需要查资料、测试、找错、验收时 | 真实业务产物和证据 refs |
| `writer` | 文档、文案、说明、报告、PPT 文本等写作型产物 | 需要实际实现代码或跑测试时 | 写作产物和引用说明 |
| `researcher` | 信息不足，需要查资料、对比方案、整理事实源时 | 已有明确方案，只差执行时 | 研究摘要、来源 refs、可执行建议 |
| `tester` | 需要设计或执行可验证测试、复现流程、浏览器/CLI 验证时 | 只是在主观评价“好不好看”时 | 测试计划、测试记录、失败复现 |
| `bug_finder` | 需要找风险、找反例、挑毛病、审查边界时 | 需要直接修复或写最终产物时 | findings、风险等级、复现线索 |
| `checker` | 产物和测试事实基本齐全，需要最终收口建议时 | worker 还没产出、测试还没跑、证据缺失时 | 验收结论、通过/阻断原因、下一步建议 |

## 派工顺序

这些不是硬编码流程，只是 LLM 的默认判断顺序。真正执行时，主代理和 coordinator 应该先读取当前任务、已有 child 状态、产物 refs、用户约束和 workflow 提示，再决定要不要偏离默认顺序。代码层只负责挡明显危险或不成立的动作。

1. 先判断任务是不是单点小任务；如果是，root 可以直接派 `worker` / `writer`。
2. 如果任务需要多人、多文件、多阶段或用户明确要求多层，就先派 `coordinator` / `lead`。
3. 产出型工作先派 `worker` / `writer` / `leaf_worker`，不要先创建或执行 `tester`、`bug_finder`、`checker` 空转。
4. 事实不足时先派 `researcher`，再把研究结论交给 worker/writer。
5. 产物出来后，可以让一个 `tester` 检查多个 worker 的结果。
6. 风险较高或用户要求严格时，加一个或多个 `bug_finder` 找问题；它们可以横向检查多个产物。
7. 测试和找错都收口后，再派 `checker` 做最终收口建议；最终状态仍由closeout 决定。

## QA 阶段门

大白话：QA 小傻妞不是一开始就站在那里等，也不是看到空目录就认真报错。它应该在有东西可测时出现，或者至少在某个 work 批次已经到“可测试/可验收”状态后再跑。

这里的“阶段门”只是一条红线，不是固定剧本。系统只判断“现在有没有可测对象、QA 有没有越权、父级有没有跳过失败 child”；至于派一个 QA 还是多个 QA、按局部 work group 测还是整体验证、失败后回原 worker 还是新建 repair worker，优先交给 LLM 根据上下文和 workflow 判断。

- 全部 work 都结束：创建或激活一组 tester / bug_finder；它们通过后再创建或激活 checker。
- 部分关联 work 结束：只给这组关联 work 建 QA，QA 结果绑定 `work_group_id`、产物 refs 和测试 refs；通过表示这组可集成，不代表整个父任务完成。
- 单个 work 结束：默认进入 `ready_for_batch_qa`；只有它是独立交付单元或阻塞后续工作时，才立即派 QA。
- QA 通过：work 不删除、不污染长期 memory，进入 `QA_PASSED` 或 `DONE`，保留 workspace、artifact refs、test refs，等待 checker 或closeout。
- QA 不通过：work 进入 `NEEDS_REPAIR`，优先让原 worker 修或派 repair worker；QA report 必须带失败 refs，不能只写自然语言。
- QA 自己失败：不等于产品失败。要区分 `QA_TOOL_FAILED` / `QA_BLOCKED` 和 `PRODUCT_FAILED`；前者重跑或替换 QA，后者才返修 work。
- worker runner 可以结束，但 task workspace 不能删；后续 QA、repair、checker 都必须能从 refs 接上。
- 大型并行任务要引入 `work_group_id` / `dependency_group` / `qa_scope`，让一个 QA 检查一组相关 work，而不是扫全局或和 worker 一一对应。

## 派工角色必须知道的边界

- 上层权限可以覆盖下层，但上层不应该默认替下层写最终业务产物。
- coordinator 可以写自己的报告、看板和纠偏消息；真实产物默认转派 worker/writer。
- 后代专属工具能力，例如 `controlled_exec`、shell、network、skill，应由真正要执行的 child/leaf 自己申请；父级检查。
- 一个检查类角色可以检查多个产出角色，不需要和 worker 一一绑定。
- root 不固定必须创建 coordinator；是否创建 coordinator 由任务复杂度和当前 prompt 约束决定。
- 显式要求 4 层链路时，root 只能启动第一层，后续必须由上层逐级创建下层。

## 选择策略落地计划

1. 先把上表作为角色选择策略文档和主 prompt 短规则。
2. 增加 `role_selection` 小型决策包：输入任务目标、阶段、已有 child 状态和证据 refs，输出推荐角色、理由、是否需要 QA wave。
3. 在 `create_subagents` / `schedule_child_subagents` 工具说明里引用短规则，但不展开完整模板。
4. coordinator/lead 派工时，如果要创建多个下级，先生成 refs-only role plan，再分批创建。
5. 真实 E2E 覆盖每个内置角色：至少验证能正确选择、正确创建、正确使用工具、正确写报告、正确被最终收口。
6. 后续接入 workflow 模板时，workflow 只能建议角色组合，不能绕过角色权限和最终收口 gate。

## 用户可扩展口子

- 用户可以通过外置 JSON 增加角色模板，但模板必须是广义职责，不能为一个很小的临时动作单独建角色。
- 用户模板必须写中文说明、适用/不适用场景、默认工具和输出合同。
- 自定义角色进入索引后，主代理只能先看到短说明；完整 prompt 仍然按需加载。
