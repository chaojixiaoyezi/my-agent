# 长任务运行时 L 线交接

状态：实现、相关回归、最终定时子代理真实 TUI 及固定版本六小时长测已完成，候选待独立审查。长测持续执行通过，交付完整性仍失败，新增完成回执/验证披露/Compact 问题待修。仅本地提交及隔离测试部署，未推送远端、未发布生产。

## 接手先看

- 设计与原问题入口：[完整报告](../design/LONG_TASK_RUNTIME_REVIEW.md)。初审部分描述修复前基线，文末和本交接描述当前候选。
- 分支：`codex/long-task-runtime-l`；基线 `c52257ef52a05dd24ed99dbba133bf58b2b7d7dd`，包含原部署源 `0b938e57ef4c28e4f262ee494353a2930b6cde0d`。
- 当前代码提交：`134f43c3aea166be0210dd79dbbb3973143455b4`。之后仅补充证据文档时，包的源码身份仍以此提交为准。
- 第七候选 wheel SHA256：`f8d572da2343c2527b064eef4f1c8c345e5ab72e87e37095a42219faef5eb910`，安装前后核对 1610 个包文件，测试机 Python 3.11 语法检查通过。
- T 来源为共同基线上已暂存的 29 个代码文件；固定补丁 SHA256 `54b42fb212ba0dd1a151c650ef3cb4ec59296aaa6cf68c7acda841c8f314631a`。不是一个新的 T 提交。保留 [T 交接](TUI_STEER_CONTROL_HANDOFF.md)，没有改写 T 工作树。
- 证据在本工作树的忽略目录 `tmp/long-task-runtime-l-evidence/`；索引与 SHA256 见其 `manifest.json`，原始输入/ANSI/请求白名单/状态账与 JUnit 分开保存。不要把临时目录当作已入 Git 的报告附件。
- 固定版本长测另存 `tmp/long-task-runtime-l-retest/`，不覆盖旧失败包；先读其 README、manifest 和独立验收结果。此次只新增验收文档/外部证据，产品包仍为 `134f43c3`。

## 已实现及代码入口

以下路径均相对 `agent_py_agent/agent/`。

| 项目 | 当前行为 | 关键位置 |
| --- | --- | --- |
| M1/M2 | 显式选模在下次合法请求前采用；当前响应、已开始工具及同次请求重试保持旧依赖。选择、绑定、实际请求分别展示 | `agent_core/model/selection_refresh.py`、`selection_history.py`、`settings/model_scope.py`、`model_selection_status.py` |
| G1/G2 | 原 Goal 同事务记录变更来源、内容 revision、执行身份、验证引用和完整进度计数；旧 revision 的 complete 不能完成用户扩大的新目标 | `conversation/goal_transition.py`、`agent_goals.py`、`store_goals.py` |
| O1 | Goal/schedule/Gateway 状态按白名单输出纯元数据，查询不写入，异常和嵌套分支不输出正文 | `conversation/goal_tools.py`、`scheduler/tool.py`、`gateway_parts/control_service.py` |
| S1 | followup/isolated、inherit/fixed，原 run 物化独立无通道线程，冻结来源绑定，按原 outbox 投回来源 | `scheduler/execution.py`、`execution_policy.py`、`conversation/runtime.py` |
| S2 | skip/coalesce、未开始队列期限、失败退避与暂停；取得后台车道后才写 started_at | `scheduler/policy.py`、`repository.py`、`service.py` |
| S3 | 原 claim 内只读预检，完整输出比较；changed 才调用模型，no_change 零模型请求，失败保留原因；最终完成才确认 checkpoint | `scheduler/precheck.py`、`run_preparation.py`、`agent_core/tool_only_execution.py`、`tool_call_runtime.py` |
| 生命周期续接 | 从原 scheduler repository 核实 durable_task 来源，子代理完成事件不再查找不存在的聊天输入 | `conversation/runtime.py::_goal_runtime_context`、`_background_request_objective`、`_background_model_inputs` |
| 等待收口 | completed 之外还查原 claim/pending wake；确认最终交付，回执写原 run，再 handled wake 和 done，冻结重投不重复模型 | `scheduler/active_run_closeout.py::waiting_completion_pending`、`run_preparation.py::record_waiting_delivery`、`service.py::reconcile_waiting_run`、`conversation/runtime.py::_complete_wake_report` |
| 控制集成 | T 的真实 claim/attempt 绑定、停止、插话回执候选；补齐原停止 Goal 事务的 user_control 来源 | `gateway_parts/execution_target.py`、`execution_control.py`、`control_service.py::_pause_goal_for_stopped_task`、`conversation/claim_execution_binding.py` |

默认调度仍是 followup/inherit、预检关闭；新策略按 job 显式开启。独立上下文不等于文件系统隔离。固定档案固定引用，不复制凭据；删除/撤销时报错。模型 complete 仍是声明，宿主 verification 未执行时保持 not_evaluated，不增质量硬门。

## 本地验证

各行是不同阶段的相关回归，不能相加宣传为独立用例总数。

| 阶段 | 结果 | JUnit |
| --- | --- | --- |
| L 独立切片 | 723 passed | `focused.xml` |
| L/T 组合，53 文件 | 1190 passed | `final-focused.xml` |
| 来源模型字段/schema | 98 passed | `v3-focused.xml` |
| 预检 cwd 修复 | 91 passed；原实现有效目录用例先红 | `v4-focused.xml`、`v4-real-cwd-red.xml` |
| 定时子代理来源 | 436 passed；新增旧版 6 failed/2 passed | `v5-focused.xml`、`v5-child-wake-red.xml` |
| 原生 stop 的 Goal 来源 | 142 passed；旧版 2 failed | `v5-stop-source-focused.xml` |
| waiting 最终交付竞态 | 503 passed；旧版交错 4 failed | `v6-focused.xml`、`v6-waiting-red.xml` |
| waiting 的重启/活执行者补测 | 24 passed | `v6-waiting-complete.xml` |
| nullable 指针/schema/执行预检 | 50 passed；旧 schema 1 failed/1 passed | `v7-schema-focused.xml`、`v7-null-pointer-red.xml` |

各代码候选的 Ruff、doc sync、strict code-size、diff check、clean-package 均通过；代码候选结果文件为 `v7-strict-gates.json`，文档收口后的完整基线检查为 `final-review-gates.json`。未执行全仓 pytest，规模未达到仓库阈值；线上 CI 没有作为验收来源。

复现：使用项目 Python 在该工作树运行 `python -m pytest <证据 local/final-focused-files.json 所列 53 文件> -q -o addopts= --tb=short`；新增范围另见各 JUnit 的文件名。不要把这个带尖括号的说明直接当 shell 命令。pytest 外需要构造 Agent 时，必须显式设置独立 my_agent_home。

首次组合验收并行运行 code-size 改写了 tracked 报告，触发仓库保护；串行同组重跑通过，后续尺寸报告均写临时目录。预检红测早期把 cwd 放在 owner 外导致权限回退，修正为合法 owner 内真实文件后才取得有效红绿证据；错误准备不算通过。

## 真实测试结果

官方 MiniMax-M2.7，独立测试机单 Gateway、多原生 TUI。被测 agent 只收到一次初始业务需求；测试者只执行预先列明的控制/输入变化，未补源码、产物或任务结果。

| 验证 | 实际结果 |
| --- | --- |
| 同规则 10/100/300 条 | 8/10、100/100、300/300 正确；10 条虽 complete 仍判内容失败 |
| 活动 Goal 扩大 50→300 | 原生控制时 active rev1→rev2，complete rev3 的 previous_revision=2；300/300 正确、无重复，未配置进度账则仍 untracked |
| 运行中切档案 | 普通处理约 4.28 秒、Compact 时约 27.12 秒、流式采样时约 37.57 秒采用；已有 child 保持原选择 |
| 实际 wire | 同名 MiniMax 不同档案的温度/来源/HTTP 200 关联；继承随来源 C/rev3，固定 B 始终 temperature 0.8。不是跨真实模型品牌测试 |
| 双插话 | 两条原生 TUI 普通补充都绑定原 task/turn，在同一批次 consumed |
| `/stop` | 合成主、子计数进程及包装进程按 PID+出生时间均退出，两个文件保持 42 条；原长任务停止复核 paused/source=user_control、Task interrupted |
| 原会话单计划 | 模型正确配置 followup/inherit，自行写出记录；去掉换行后版本文字一致，不能说字节完全相同 |
| 预检 | 22 个 no_change run 的实际模型调用数均为 0；缺文件保留 PATH_NOT_FOUND，并按阈值暂停 |
| 旧轮长任务 | 一次需求，最后真实工具进展在 3 小时 2 分 56 秒，498 次工具完成、主 Compact 7 次；混合版本运行 |
| 旧轮交付 | 公开检查 228/600、隐藏 497/1200，通过口径为行值和列数；另有 48/108 处列标签差异，README/report 缺失，判未完成 |
| 固定版本长测 | M2.7、200K/90%，一次需求自然到 6 小时 00 分 27 秒；主 829 工具/11 Compact，六个子级共 838 工具/7 Compact；同一 Gateway，1610 个安装文件始终一致 |
| 新轮独立交付 | 公开 600/600、保留 1197/1200、额外合同 10/11；单测 40/48、README 示例失败；列标签另有 60/118 处差异，仍判交付不完整 |
| 第七候选最终子代理用例 | 原 waiting run 等待孩子和最终执行结束，再 done；模型自主写入并读回 child-summary.json，12/686 正确，最终回复保存在原 run 且显示于 TUI；6 次主体实际请求均为固定 B/0.8/HTTP 200 |

最终复验：job `job_904b6a73e17841dabd365e57e3286137`、run `srun_a6ffdb8d7ca444298544b075597f2c37`。观察到待处理 child wake 及活执行 claim 时仍 waiting/checkpoint=null；最后主级事件为 `1791181723.0111022`，run 的 ended_at 与 job checkpoint.confirmed_at 同为 `1791181723.1491773`。外部 14 项事实核验全部通过，文件见 `remote/scheduler-child-v7-final.json`，轮询不是连续无间隙的竞态证明，精确交错由离线四例补足。本地通道 delivery_status 为 not_applicable/root_subagents_terminal，证明 TUI 展示，不冒充真实 IM 外发确认。结束时测试 owner 无活动计划、run、请求或 claim，只有一个空闲 Gateway；观察脚本已停止。

旧轮没有同版本不干预的三小时通过证据；本次新固定版本已补齐六小时持续运行证据，两轮不能混算。旧轮第四次直接停启网关触发既有非计划续跑上限 3，原请求 TURN_RESUME_LIMIT_EXCEEDED；停止由测试升级方式触发，不是模型 complete 或固定三小时上限。安全重启仍应走原接班流程，不删除保护或伪造恢复标记。

新轮 task `gwreq-1791185382-6d63a7ec8f294a878c231cd9ff71678a`、Goal `goal-9b9d90e8e9fb4803`。Goal transition 的 source=model_tool、revision 2、verification=not_evaluated；随后主 Task completed、claim finished，六个孩子 DONE。三小时处曾提出的六小时观察停止计划已被用户“不要停”覆盖，最终未发送停止控制或第二次业务需求。测试机时钟与本地日期不同，时长只取同机提交及终态差值。

完成声明的真实缺口：完成前模型已收到单测 8 项失败，最终未披露；同套测试在 SQLite 上亦有 5 项失败，既有测试预期问题，也有引擎错误。README 使用错误接口，示例运行 TypeError；core smoke 未调用已定义的结果断言。Goal 完成时仍有 9 个未关闭进度项，结构化回执记录正确，但 `conversation/goal_tools.py::_goal_response` 仍给无条件 Goal achieved 指引，应优先改为消费现有验证/进度事实的准确软提示，不新增质量硬门。

Compact 新样本：主第 4 代 provider_invalid_summary_shape 完整回退 426286 字符，下一工具后重压并恢复；worker-4 五代 after=41632/69954/86708/107541/117323，固定保留两条早期带产物引用的 apply_patch 回执，最终 DONE。后续沿 compact_tool_partition、tool_ir_history、compact_semantic_summary 和既有 COMPACT_GENERATION_FACTS 复核，未证明旧引用是现场增长的唯一根因。完整结果、证据命名和修复优先级见报告末节。

保留的失败还包括：模型未配置预检却声称不支持、把 followup 配成 isolated、漏掉计划中的最终交付步骤、重复建计划、误填 JSON pointer；框架预检 cwd 和子代理来源错误已修复，等待最终投递的竞态已补红绿。MiniQL 一个长 child 在第 19 代 Compact 以 COMPACT_CANDIDATE_TOO_LARGE 失败，父级后来继续派工；该失败及主回复重复现象另交 Compact/模型质量线，不给业务样例添加特殊逻辑。

## 环境、版本与回滚

- 测试机：报告约定的 L 机，隔离根 `<test-root>` 下的 `home/venv/gateway.yaml/evidence`，端口 8420，只运行一个测试 Gateway。安装事实以 `candidate-v7-installed.json` 为准。
- 原日常启动器、生产发布目录和生产模型配置未替换。没有读取原业务会话正文；所有完整内容检查都来自本轮合成测试。
- 第七候选只改指针 schema，若需测试环境回滚，可在确认空闲后安装已保存的第六候选 wheel（SHA256 `e5a7b7fd93caeed0ee7d36d20c7a285efe2da014e8e14d67506986d499f10dec`），核对其 manifest 再启动同一配置。
- 部署前同时检查 scheduler 活动 run、Gateway pending/processing 和后台 claim；升级脚本记录的是当时的精确 PID，不可原样重复使用旧 PID。存在工作时使用原安全重启；无工作才直接停启测试进程。不要在生产首次尝试恢复流程。
- 一次 pytest 外调试漏设 home，误建了默认 owner 合成计划。已按确切 job/run ID 暂停、TaskStore CAS 取消并确认终态，再标 deleted/next_run_at=0；证据 `local-debug-isolation-incident.json`。不能声称开发过程默认目录从未写入；其他记录未处理。

## 尚未关闭的事项

1. 独立 agent 审查本候选和测试方法，再由主线单一负责人集成。当前未发布生产，不能说在线 agent 已获得修复。
2. T 原交接的 T22/T23、全部真实 IM 组合、公共 drain 仍按原负责线完成；导入 T 代码不等于这些项通过。
3. 新版 AGENTS 学习来源合同不在此基线，S11 组合未完成；沿原上游接口集成，不增加第二套来源名单。
4. 固定版本多小时持续运行已关闭；交付完整性、复杂计划生成可靠性、Compact 退化及故障组合仍开放。优先修正完成回执与已知失败披露，再诊断回退/保留水位，不用更长时长替代质量检查，不把一次 MiniMax 结果外推为 DeepSeek 对照。

## 建议下一步

先独立审查请求切模、Goal CAS、waiting/checkpoint 与新长测证据，优先处理完成回执和已知失败披露；按固定新候选验证后再由主线集成。只读审查和 Compact 诊断可并行；Goal/closeout、Compact 写入口与 Gateway 部署各保持单一负责人，不改本轮产物、不影响在线其他 agent。
