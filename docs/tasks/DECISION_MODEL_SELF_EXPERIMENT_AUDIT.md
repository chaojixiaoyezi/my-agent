# P5-E/F/G/H：有预算的自动对照、自测与设置调整审计

状态：**只读源码合同审计完成，运行时能力尚未实现**。本文件不把恢复原语、调用成功或测试命令通过称为自动调参收益。

- 日期：2026-09-22。
- 分支：`codex/decision-model-integration`；基线 HEAD：`ef497a90481203b7941c79b1d640142545173808`。
- 取证对象：该分支当前协作工作区，包括尚未提交的决策接入改动；不是仅审计上述 HEAD。
- 本线唯一写入：本文件。未修改生产代码、测试、配置、运行数据或共享设计；未调用真实模型，未启动 Gateway。
- 依据：`AGENTS.md`、`LLM_GUIDE.md`、`docs/WORKSTREAMS.md`、`docs/tasks/HANDOFF_TEMPLATE.md`、
  `docs/tasks/DECISION_MODEL_GOAL.md`、`docs/design/DECISION_MODEL_INTEGRATION.md`、
  `docs/design/THREAD_GOAL_LIFECYCLE.md`、`docs/modules/verification/03-purpose.md` 和 `04-structure.md`。

## 1. 结论与实施顺序

原系统已有可复用的任务身份、模型调用记录、验证证据、配置 CAS、取消和操作幂等入口。
但还没有把它们连接成“获授权、守预算、可比较、能自动应用并准确恢复”的自实验合同。
不能直接启动循环试错，然后把 Goal 预算、`mode=apply` 或 Jev 置信度充当缺少的约束。

推荐顺序为：**原设置事务的精确恢复原语 → 结构化实验授权与请求前预算 → 有界只观察对照 → 有可信指标时自动应用**。
主线已确认先做原服务的 `restore`：同一作用层、一次事务同时 set/unset、完整 owner/thread CAS、原前值与读回。
这只是 P5-G 的基础，不等于自动实验、可靠回滚策略或 P5-G 完整交付。

| 项目 | 现有基础 | 仍缺什么 | 本次结论 |
| --- | --- | --- | --- |
| P5-E 有预算的对照试验 | 原任务/Goal、DecisionStage 绝对期限、ModelCallLedger 与持久用量投影 | 实验授权、同一样本的基线/候选绑定、请求前 HTTP/input 预留、跨恢复边界 | 尚未实现；先限制到同一原任务的一次有界样本 |
| P5-F 基于证据自动调整 | 原字段白名单、两层 CAS、工具操作幂等 | 可核验效果指标、获准字段/范围、自动写入的来源与退出条件 | 能构造差异建议；当前不能据“更有信心/更快”自动推广配置 |
| P5-G 准确恢复前值 | 原服务返回 `before`，能独立 patch 或 reset | 同一事务混合 set/unset、恢复资格、原操作回执与提交不确定处理 | 恢复原语可直接实施；恢复策略仍需授权/身份/冲突证据 |
| P5-H 边界与收益验收 | 各点已有关闭、超时、失效、输入用量等 focused tests | 自动实验端到端组合、可信收益样本与适用范围 | 可先写验收合同；不能用基础回归替代效果验收 |

## 2. 用户授权与全自动边界

用户要求建议、选择、采用、失败回退在运行时自动完成。预先配置允许范围后，不要求用户逐个子代理、逐次
模型调用、逐次候选配置再操作一次。失败、预算耗尽、版本冲突和授权撤销应自动保留当前合法行为并记录原因。

默认关闭仍是产品边界。实验开关、范围和预算必须来自原宿主可信结构化设置或原授权操作。
普通自然语言可由主模型理解并提出结构化操作；运行时不能扫描“帮我优化”等文本建立授权，
也不能信模型参数中的 `authorized=true`、模型自述、置信度或 Task/Goal 的正文。
一次允许某决策点使用 Jev，不自动允许不断换模型、重跑任务、改其它点或扩大后台试验。

需要区分三个已有/待建事实：

1. 原 owner/tool policy 决定某动作现在能否执行。
2. 原决策设置的 off/observe/apply 决定该点的建议如何消费。
3. **尚缺**的实验授权决定可对哪些样本、哪些字段、哪些已授权模型做多少次额外对照，以及能否自动保存设置。

第 3 项应在原设置服务登记；授权操作沿原 ToolExecutor/host command 身份与回执保存来源，不建实验授权数据库。
若原入口无法证明明确授权，只能保存建议或返回 `authorization_missing`，不能靠 Jev 补出许可。
这不意味着每一步弹窗：实现后用户只需事先给一次有界授权，授权内流程自动完成。

候选模型只来自已有可用、用途匹配且未撤销的模型目录。涉及用户指定的子代理模型范围时，MiniMax-M2.7
和 MiniMax-M3 使用官方原生连接，OpenCode 仅使用 deepseek-v4-flash；名称本身不证明来源。
本审计未验证这些连接或改变日常模型。开发代理的算力分配不是产品实验授权。

本方案只约束时间、请求数、输入 token 和状态，不引入价格、USD 或费用预算。

## 3. 已核对的原权威入口

以下路径均相对仓库根目录；函数名是后续分工的定位入口，行号会随并行开发变化。

### 3.1 Goal、任务和续跑

| 文件/入口 | 已有合同 | 对实验的含义 |
| --- | --- | --- |
| `agent_py_agent/agent/conversation/models.py::ThreadGoal` | goal/thread/task、状态、token/time 用量、内容 revision、metadata | 可引用准确 Goal；metadata 的存在不等于有实验授权 schema |
| `conversation/store_goals.py::GoalStore.create/update/account_usage/transition_guard` | 文件原子写、精确 Goal/状态/内容 CAS；用量不推进内容 revision | 原目标状态仍唯一；不能另造实验 Goal 状态镜像 |
| `agent_core/runtime/goal_accounting.py::begin_goal_model_turn/account_goal_model_response` | 按宿主 goal/task/thread 绑定计时及事后增量 | Goal token 口径是非缓存输入＋输出，不能冒充实验 input-only 预算 |
| `conversation/goal_clock.py::GoalClockGroup/shared_goal_clocks` | 同进程共享 monotonic 活跃计时 | 不是跨进程的绝对实验到期时间；重启不能借新时钟重置实验预算 |
| `conversation/store_tasks.py::TaskStore`、`models.py::ThreadTaskLink` | 任务链接是生命周期权威，workspace 状态是投影 | TaskLink 没有可直接使用的实验授权/指标信封 |
| `conversation/background_goal.py::continue_goal_after_report` | 精确 Goal/task、active 状态、子树阶段决定是否沿原入口续跑 | 自动试验若需续跑，必须留在原 Goal/任务执行链 |
| `conversation/goal_runtime.py::raise_goal_continuation_wake` | 原 wake 队列；去重键来自 goal ID | 不新增 timer、cron、轮询线程或实验 scheduler |

`GoalStore.create` 接受普通 metadata，但 `update` 当前仅维护目标内容、状态和 token_budget 等既定字段；
没有实验授权撤销和预算预留 API。不能绕过其更新合同直接塞可变授权或余额。
`account_usage` 在已有请求结束后递增，达到上限才进入 `budget_limited`；没有调用前预留，也没有请求次数额度。
把实验上限直接写进 Goal token_budget，会改变原目标口径且不能阻止单次超额。

持续 Goal 也不是实验必需的新对象：普通任务可在原执行片内做一次授权对照；只有本来明确获准持续执行时才用
原 Goal 续跑。不能为了补调度能力自动创建用户未请求的持续目标。

### 3.2 模型调用、输入 token 与期限

| 文件/入口 | 可复用事实 | 限制 |
| --- | --- | --- |
| `contracts/model_call_ledger.py::ModelCallLedger.started_retained/finished/timeout/provider_attempt/cumulative_summary` | 同一 call 的终态、真实 HTTP attempt、身份、输入来源、用途分区；重复事件幂等 | 内存账与有界明细；尚无实验预留 API |
| 同文件 `_partitioned_usage` | provider 报告值与估算值分开，按字段区分缺失和真实零 | 失败/超时不能解释成零输入；候选胜出不能靠缺 usage 获得更低数值 |
| `conversation/store_usage.py::ModelUsageStore.append_snapshot_once` | 原累计快照按 thread/request/run/task/usage_scope_id 幂等保存单调增量 | 持久摘要不是请求前预算预留，也不能从被裁剪明细重建完整样本 |
| `agent_core/_finalization_service.py::FinalizationService.settle_model_usage` | 成功、错误、取消收口使用原用量入口 | 实验也要沿此账投影，不再生成一套 token 累加文件 |
| `conversation/decision_model_call.py::invoke_decision_model_call/_started_params/_invoke_worker` | 原 call_id、operation/input digest、purpose=decision、实际 worker HTTP 观察、无内部自动重试 | metadata 当前没有实验/基线/候选维度；记录中的 model 是请求模型，实际模型还须从结构化响应取证 |
| `conversation/decision_service.py::begin_decision_stage/decide/_invoke/decision_outcome_is_current` | 阶段与接入点共同绝对期限、当前身份/策略/连接校验、失败保留原方案 | 没有跨阶段实验总量、累计请求数或实验授权 |
| `backends/decision_protocol.py::DecisionRequest/DecisionResponse` | 深冻结输入；摘要绑定 policy/candidate/source；实际模型与 usage 保留在响应 | 同一正文换 policy 后 digest 可不同，不能只拿 digest 相等判断配对样本 |
| `llm_scale/hot_path.py::global_llm_admission_slot` | 原进程并发闸，可选调用不排队且保留普通名额 | 当前 decision 热路径接的是并发，不是实验预算 |
| `llm_scale/token_budget.py::TokenBudget`、`admission.py::AdmissionController` | 已有租户窗口的预扣/结算实现，可参考其预留模式 | 不能另实例化一份当实验账；当前 decision 热路径未接这套 token 预算 |

`started_retained` 与 worker 持有的保留令牌能让迟到 HTTP 事实仍写入准确调用，worker 退出后才释放。
`ModelUsageStore` 以累计代次和摘要去重，前后台交接不会重复加同一快照；新进程/新代次不能减去旧代次余额。
这些机制值得复用，但它们记录已经发生的调用，并不自动授予下一次调用。

### 3.3 验证与效果证据

`verification/runtime.py::record_tool_verification/_record_command/_mark_writes` 被动读取 canonical ToolCall/ToolResult。
只有规范命令的真实进程退出事实才落证据；成功文件写入使旧证据 stale，任意 stdout 或模型“测过了”不生效。

`verification/project_facts.py::project_facts_for/classify_verification_commands/_verify_commands` 从项目文件识别规范命令，
并区分 full/targeted。`verification/repository.py::VerificationEvidenceRepository.record/status/mark_edited` 在原 owner
SQLite 中保存 command、退出码、范围、root、owner/thread/task、事件 ID 和 stale 状态。

这足以证明“哪个任务的哪个项目执行过什么验证命令”，不能证明：

- 某模型完成业务更好、某提示排序更好、某次未召回没有漏掉关键信息。
- 两次命令用的是同一版本代码、同一数据和同一测试集合；当前记录没有这些内容 hash。
- 一个 `full` 命令覆盖了业务全部要求；`full` 是原命令分类，不是质量保证。
- 基线和候选之间存在因果收益；当前没有实验 arm、评价规则版本或可信质量测量 schema。

原 verification 继续作为客观执行证据，不能升级成通用评分/任务完成门。领域工具若原本有可核验结构化指标，
可以引用其原结果和 artifact refs；没有就给 `not_comparable`/`insufficient_evidence`，不让 Jev 为自己的建议打分后自动晋升。

### 3.4 设置 CAS、恢复与后改优先

`settings/decision_settings.py::execute_decision_settings_operation` 共用 read/patch/reset；
`_execute_transaction` 固定 owner→thread 锁顺序；`_check_revision` 要求完整 `{owner, thread}`。
`_update_thread` 只替换最新线程中的 decision_settings，保留 Compact、summary、模型选择等其它字段。
保存后的通知在锁外执行；通知失败不会把已保存改称未保存。

`_patch_settings` 的 patch 写值，reset 删字段，两者当前分别递增 revision 并分别提交。
返回值含 `before`，`decision_settings_projection.py::decision_settings_projection` 同时返回原 overrides、effective 和 sources。
因此可以知道原覆盖是否存在，不能仅保存 effective 后用 patch 回填：那会把原本继承变成永久覆盖。

`tooling/user_config_tool.py::UserConfigTool._decision` 从可信 runner 取 thread，拒绝模型传身份和旧 revision，
沿 mutating/serial/operation policy 与原工具操作审计。不具备持续实验授权操作，也不具备逐字段最后写入者记录。

后续恢复必须区分：

- **没有后改**：原自动操作提交后的完整 revision 仍匹配，当前值也属于该操作，才可在一次事务恢复原字段存在性和值。
- **已有后改**：保持用户值并返回冲突。不能“重新 read 最新 revision，再用旧 before 覆盖”。即使值改回相同值，revision 变化仍是冲突。
- **只改其它字段**：当前粗粒度 CAS 仍可安全拒绝整体恢复。若以后要恢复未被用户碰过的字段，需要原配置服务的逐字段写入来源/版本，当前没有。
- **原 profile 已删除/共享授权已撤销/字段不再合法**：不能为恢复旧值重建模型配置、凭据或权限；报告不可恢复或冲突。

### 3.5 取消与幂等

`runtime_db/host_commands.py::HostCommandIdentity/HostCommandRequest/insert_host_command` 将宿主已鉴权 actor/owner/thread/request
和输入/环境摘要冻结到原 TaskRun；同请求换输入不能变成新执行。此模块明确不负责授予权限。

`runtime_db/host_command_execution.py::execute_host_command/query_host_command/_operation` 复用原 ToolExecutor 和操作账，
完整旧回执只读重放，不再次执行 handler；坏账、UNKNOWN 或不完整回执不能冒充成功。
`tooling/tool_operation_coordinator.py::execute_tool_operation/replay_completed_tool_operation` 是副作用原执行边界。
UNKNOWN 的恢复必须沿原只读 reconciler、真实 `source_ref` 和 generation/holder CAS；没有这些事实不重放。

`runtime_db/run_cancellation.py::cancel_runtime_run` 精确绑定 task/run/agent_run/attempt，并关闭原执行权限。
其 `authority_closed` 不等于进程已退出或业务副作用已撤销。旧 attempt 的停止不能跟随当前指针取消新 attempt。

`decision_policy.py::notify_decision_settings_changed` 当前通知注册在本进程；发送/采用前 `_stale` 还会复读原设置。
不能把进程内通知夸成跨进程即时网络中止。实验撤销应在每个安全边界拒绝后续动作，在途调用受原 deadline/取消约束。
`decision_service._invoke` 区分设置失效/可选超时与真正的用户取消；不能把超时传播成用户 `/stop`。

Goal pause、当前回合 interrupt、任务 stop 的语义继续分开。撤销实验只停止未来实验，不替用户暂停整个 Goal；
任务 stop 则服从原任务权限关闭和资源清理。恢复配置也不能复活原任务或重放已执行的业务工具。

## 4. 最小通用合同草案（尚未实现）

### 4.1 原设置中的单一授权信封

建议给原决策设置 schema 增加独立、默认关闭的实验授权对象；有效授权整份来自一个可信作用层，不把 owner/thread
的半份 allowlist、半份预算拼成更大授权。Task/Goal 仅引用授权 ID/revision，不能保存另一份可修改授权。

| 结构化事实 | 来源与约束 |
| --- | --- |
| 授权 ID、revision、状态、来源 operation/request/actor | 原宿主认证和设置提交；模型不能自填可信来源 |
| owner/thread/task/可选 goal 绑定 | 当前原运行/原对象，缺失或冲突拒绝；不能从样本正文反推 |
| 允许点、允许配置字段与有限候选值/profile refs | 原字段 schema 与原模型目录校验；不接受任意 Python、shell 或凭据变更 |
| 允许操作 | 读取已有证据、额外影子判断、临时尝试、保存设置分别授权；保存不是观察的隐式后果 |
| wall duration、有效截止时间、最大真实 HTTP 请求数、最大输入 token | 显式有限上限；缺上限不开持续实验，耗尽不自动续额 |
| 样本范围、评价规则引用、失效/恢复策略 | 原 task/evidence refs、可信规则版本；未声明规则只允许观察 |

初期仅支持一个 thread 内一次任务范围、一个原执行片、一个配置作用层，默认串行；不跨 owner、不自动开子 Goal。
扩展到后台前先定义原 Goal/wake 的续跑资格与持久预算，不另建后台运行器。
授权撤销和新一轮授权是不同版本；恢复开关不能让已撤销的旧实验复活。

### 4.2 对照样本与消费

每个样本冻结：原 owner/thread/task/run/attempt、point、证据 refs、内容 hash、候选集版本、生成/决策配置版本、
请求及实际模型来源、原权限版本、基线/候选各自的 call/operation 引用。正文仍在原 archive/artifact，不复制缓存。
对照结果可作为原任务产物登记，状态/用量仍只从原账读；产物不是新的执行账本。

比较用独立的“原材料/候选集合指纹”和明确的 variant，原 `DecisionRequest.input_digest` 保留其完整绑定含义。
不能为了让两个 digest 相等而去掉真实请求中的策略、来源或权限绑定。

先复用已读且获准的任务材料与既有验证结果。只有样本输入、测试/评价版本和其它关键条件可比较时才计算差异。
不能为了凑基线重跑已发送消息、文件写入、发布、支付或 UNKNOWN 工具。新增验证命令也沿原工具权限与原操作 ID。
代码测试会执行项目代码、可能产生副作用；不是看到“test”就视作天然可安全重放。

影子候选沿原 decision service/原后端/原模型账调用，仍核对当前配置与授权；不得直接用 HTTP 绕开它们。
当前 service 只读正式配置，尚无可信实验候选投影入口。未来若增加，只接宿主验证的只读候选，不建立第二份配置服务；
不能在对照期间反复改用户全局配置来影响其它任务。

`baseline=off` 时零额外决策调用是有效基线。candidate 可记录是否实际改变展示/选择、失败原因、延迟和输入 token。
“候选建议被自动采用”只证明消费发生，不能等同任务质量更好。

### 4.3 预算必须在请求前生效

复用原模型账扩展预留/结算合同，或在原模型准入入口对同一本账做原子检查；不得另建 experiment token 表、内存余额
缓存或 CSV 累计账。调用的实验/variant 标签属于原 ModelCallRecord 的有界结构化 metadata，不是第二套调用 ID。

| 上限 | 准入与结算要求 |
| --- | --- |
| 时间 | 准备前建立实验绝对 deadline；点/阶段/任务/实验取最早截止，准备、序列化、排队、验证和恢复收口都消耗时间；不得每个子调用重新给全额时间 |
| 请求数 | 在每次真实传输之前原子预留；基线、候选、辅助评价、探测和重试均计入，不按成功次数或逻辑 call 次数代替 HTTP 次数 |
| 输入 token | 对完整实际发送载荷预留；结算使用原账 provider input 来源，估算和未知单列；不减去缓存输入冒充较小的输入总量 |
| 未知结果 | 已可能送达的超时保留占用，不退成零；只有可证明未发送才能释放相应预留，迟到 HTTP 事实仍附原调用 |
| 并发 | 第一片串行；以后并发需在同一权威锁/事务完成检查和预留，不能两路都看见旧余额后各自发送 |

deadline 首先约束继续发请求和采用结果。原 bounded worker 允许逻辑超时后物理执行仍未退出，设置写事务也没有
宿主可传的有限写锁等待参数；因此当前不能承诺“包括物理清理在内必在该秒数内结束”。后续需预留收尾时间，
对未完成清理保留真实状态，不能因时间已到就丢弃恢复责任或报告已恢复，也不能延长期限继续试验。

**当前 input 硬上限的真实缺口**：`_started_params` 使用 `estimate_tokens`，后端的窗口校验/输入字节限制不等于
供应商实际输入计数的证明。未验证的估算不能保证实际 provider input 永远不超过预算。
如产品坚持严格实际输入上限，需先有适用模型的可靠 tokenizer/可证明上界或服务端硬额度；无法证明时该联网试验
不得声称满足硬上限。可先做零额外请求的原证据比较，不能把“估算达到即停”改名成严格预算。

同一原账中建议分别展示 `provider_input`、`estimated_input`、`unknown_usage_calls` 与预留状态，
不能把未知数加成准确总量，也不能为取得一个成功样本无限重试。
预算包含为实验新增的所有模型调用；原业务已发生调用可引用，不重复扣作新增实验请求，但应标明复用来源。

第一联网切片只允许同一进程/原执行片完成。当前 ModelCallLedger 不是持久预留账；崩溃后保留原运行/操作的不确定状态，
停止该授权实例的后续请求，不凭新进程内存或新 attempt 重置额度。
跨进程恢复需要扩展原用量/操作存储的预留事实与幂等结算，完成前明确不支持，不能旁建“实验恢复文件”。

### 4.4 差异、采用与回退

Jev 可以给候选和解释；机器采用条件由可信评价规则与结构化事实确定。建议结果保存证据 refs、字段差异、
适用样本、未命中原因和用量来源。无可信业务指标时只生成有界建议，不自动把更低 token/延迟转换为永久质量提升。

自动应用必须同时满足：授权仍有效、字段与候选在范围内、预算未到期、证据当前可比较、评价规则满足、
原运行仍有执行权、完整 settings revision 匹配、实际 profile/连接与权限未撤销。
最终写入由原 settings 服务在锁内再次核验相关前提；Jev 不直接保存配置。

临时尝试结束时自动恢复，成功晋升仅对显式允许保存的字段生效。不开审批循环；不满足条件自动保留基线或当前用户值。
撤销授权后停止未来试验；是否撤销已经正式采用的配置由原授权的保存/恢复策略决定，不能把“停止实验”猜成“撤销所有历史配置”。

## 5. 最小首片：原 settings restore 原语

此片可先实施，独立于 Jev 和收益指标。它不增加联网、后台循环或自动改设置策略。

建议入口仍为 `execute_decision_settings_operation`，增加一个已校验操作：同一 scope 内提交 `set` 与 `unset` 字段集合，
要求不交叠、只用原字段白名单、同样验证值/引用/作用层，仍需要完整 owner/thread expected_revision。
字段不存在与存在但值为空必须可区分；不把 `null` 自行解释成删除。

原 `_execute_transaction` 的同一锁区间完成读取、CAS、全部字段校验、一次保存、原读回与 before。
无论改一个还是多个字段，只前进一次该层 revision；失败不部分提交，不整文件回写旧 snapshot。
不同时恢复 owner 和 thread 两个文件；两层同时改需要另审多文件原子性，首片只恢复同一层。

实验宿主未来只能从原已提交操作回执取得要恢复的 before 存在性和值，不能由模型重新编造。
原语本身不判断“谁拥有这个字段”、不推断是否该回滚；该资格必须由上层授权/原操作事实检查。
只有 CAS 匹配才恢复；冲突结果保持现场，不刷新 revision 盲重试。

保存成功而 ToolOperation 完成回执写入失败是独立崩溃窗口。仅看当前值等于目标不能证明本次写入成功，尤其不能区分
用户后来的同值编辑。此时沿原 `unknown` 与只读核对，不重复写入或反向 restore。
如后续要求跨崩溃自动恢复，需在原配置事务/操作记录中建立可证明的提交来源关联；不新建“回滚账本”。

首片建议文件归属（这里只提案，未认领或修改）：

| 文件 | 精确范围 | 验证 |
| --- | --- | --- |
| `agent_py_agent/agent/settings/decision_settings.py` | `_validate_request`、`_patch_settings`/通用字段变换、原事务分派；保留原锁序及通知位置 | set+unset、完整 CAS、一次 revision、无部分写 |
| `agent_py_agent/agent/settings/decision_settings_schema.py` | 仅确有共用操作校验需要时扩展；不添加实验假状态 | 字段/值/作用层校验继续共用 |
| `agent_py_agent/agent/tooling/user_config_tool.py` | 若本片对原工具开放 restore，增 action/schema/白名单；继续原权限、operation 和 runner 身份 | 跨 owner/thread、额外字段、过期版本拒绝 |
| `agent_py_agent/tests/test_decision_settings.py` 或独立 focused 文件 | 原服务临时 owner/thread 真实锁与存储 | 并发只有一个赢家、覆盖缺失/空值、原线程其它字段保留 |
| `agent_py_agent/tests/test_user_config_decision_operations.py` | 仅工具动作确有修改时 | action→服务→原错误合同 |

生产函数/类变更同步双层注释与所属文档。主线统一登记导航和共享设计，避免与 P5-A/B 的 settings/memory 线冲突。

## 6. 后续阶段、文件边界与验收

| 阶段 | 接线位置/原权威 | 必须验证的事实 | 完成边界 |
| --- | --- | --- | --- |
| G0 恢复原语 | 上节原 settings 服务 | absent/present、继承、完整 CAS、混合 set/unset 一次提交、profile 撤销、并发与通知失败 | 只关闭恢复原语缺口 |
| E1 授权与预算 | 原 decision settings schema/defaults/配置、原 user_config/宿主控制、ModelCallLedger、decision_model_call；仍沿原 RuntimeDB/ToolExecutor | 默认关闭不准备输入、不联网、不写实验状态；伪造身份/授权拒绝；撤销/到期自动失效；并发预留不超发 | 没有可靠 input 上界时，联网硬预算仍不得验收 |
| E2 一次只观察对照 | 原 decision_service 和一个已完成接入点的纯投影/消费接缝；原模型账/归档 | 冻结材料、baseline/candidate 绑定；观察字节不变；同任务/不同任务隔离；来源与权限复核；失败不影响原任务 | 只证明对照运行，不能证明质量收益 |
| F1 有证据自动应用 | 可信评价来源＋原配置服务＋原 operation 回执 | 超范围字段拒绝；Jev 建议不能授权；基线/候选证据完整；自动写入前 CAS；用户后改优先 | 无可信指标的点仍保持观察 |
| G1 自动收尾/恢复 | 原配置事务、ToolOperation 回执/UNKNOWN reconciliation、精确取消 | 临时试验自动恢复；set/unset 原值一致；回执不确定不重放；ABA/并发/撤销不覆盖用户；不逆转业务工具 | 不能以 G0 代替本阶段 |
| H 组合与少量真实验收 | 原测试/回放链、隔离 owner/profile、原用量/归档记录 | 关闭等价、错误隔离、实际消费、输入来源、预算、恢复、真实效果与局限 | 没有收益证据的能力仍默认关闭 |

既有测试依据已读：

- `test_decision_settings.py`：owner/thread CAS、同版本并发赢家、继承与原字段保留；
  `test_decision_settings_notifications.py`：关闭竞态、通知顺序和锁外通知。
- `test_model_call_ledger_partitions.py`：终态不重开、迟到 HTTP 事实、缺用量与零用量、用途分区、retention 与裁剪。
- `test_verification_repository.py`、`test_verification_runtime.py`：精确任务范围、成功修改后 stale、失败/targeted 不升级。
- `test_runtime_run_cancellation.py`：精确执行身份、UNKNOWN 保留、旧 attempt 不接管新 attempt。
- `test_host_command_operation_replay.py`、`test_tool_operation_idempotency.py`：原结果重放、坏账/不确定不冒充完成。
- `test_bounded_call.py`：取消与 deadline 竞态、迟到 worker 仍占资源、不协作 worker 有界保留。
- `test_goal_lifecycle_recovery.py`：同 Goal/task 的 attempt 轮换、原 wake 和 Compact 保留。

新增测试应按合同→fake tool→fake LLM→replay→少量真实验收展开。fake 只证明协议与边界，不称为 Jev 效果。
真实样本使用普通中文需求，开发者不替被测 Agent 改结果、补业务产物或筛掉不理想回答；记录全部尝试与未命中原因。

真实验收至少覆盖：off 零额外 HTTP 且原请求/展示等价；observe 真实判断但业务行为不变；apply 无人工逐步操作；
时间/次数/input 临界值；超时但可能已送达；用户后改；授权撤销；同值后改；进程退出；坏账与 UNKNOWN；来源/hash/refs 保持。
已有自然任务若缺对照条件，只记录观察，不能拿两次不同问题的耗时作候选更好的证据。

本线没有运行上述 pytest，也没有宣称它们本轮通过；它们是源码审计和后续改动的 focused test 入口。
本次仅文档变更：`python3 scripts/check_doc_sync.py` 返回 `DOC_SYNC_PASS`；`git diff --check` 通过。
另对本新增文件检查尾空格、结尾换行和个人绝对路径，均正常。真实运行验证留给授权后的实现片。

## 7. 成熟参考与证据边界

先核对本地参考项目的合同索引，再读匹配源码；本次不是全仓逐行审阅，也没有引用“框架名气”替代合同证据。
下列路径均相对本机的开源项目参考目录，不把个人路径写入仓库。

| 参考 | 实际读取入口 | 借鉴与边界 |
| --- | --- | --- |
| `codex_contract_code_files.xlsx` | 文件明细 224 条，相关命中 `codex-rs/tui/src/chatwidget/goal_validation.rs` | 索引未覆盖本次所有 Goal/config 接缝，额外用源码定位；不能说索引提供了完整实验合同 |
| `codex-main`，本地 HEAD `578c1b22` | `codex-rs/ext/goal/src/accounting.rs` 的 `progress_accounting_permit/progress_snapshot/mark_progress_accounted_for_status` | 并发快照→持久写成功→推进已记游标；monotonic 时钟与目标身份分离。不是 input-only 硬预算实现 |
| 同上 | `codex-rs/app-server/src/config_manager_service.rs::batch_write/apply_edits` | 一批字段修改共用 expected_version、允许路径和托管字段限制；本次未宣称它已具备实验回滚/最后写者策略 |
| `langgraph_contract_code_files.xlsx` | 文件明细仅 4 条 SDK auth 记录 | 未覆盖 checkpoint 主链；覆盖不足已明确，不用目录统计冒充已审实现 |
| `langgraph-main`，本地 HEAD `77a60e8` | `libs/checkpoint/langgraph/checkpoint/base/__init__.py::Checkpoint/CheckpointTuple`；`memory/__init__.py::InMemorySaver.put/put_writes` | 明确 thread/namespace/checkpoint/task 与父快照，按稳定写入身份去重；checkpoint 不等于副作用回滚，也不提供效果指标 |

这些参考支持复用稳定身份、原版本比较和原记账边界；没有一个入口能替本仓库提供用户授权、可信质量指标或
“重跑任何任务都安全”的保证。本次未引入依赖、移植框架或验证参考项目自身的运行行为。

## 8. 交接与建议下一步

实际完成：定位可复用权威、明确授权/硬预算/效果/恢复缺口、给出通用合同与分阶段测试，唯一新增本审计文档。
本线释放本文件及本轮所有 ownership；源码/配置未被本线修改，主线负责 `CODEBASE_TREE.md` 和共享设计登记。

**建议下一步**：先由一个 owner 实施并验证原 settings 的 G0 恢复原语，因为它能独立修齐精确前值事务，不依赖未建立
的效果指标。独立 agent 可并行只读制定授权来源与 input 上界合同，但不要同时改 settings/schema/原模型账。
待 G0、授权和预算闭合后，再选择一个已有接入点做只观察对照；未具备可信指标的点不进入自动长期保存，
仍保持默认关闭、运行自动回退、无第二调度/账本、无逐步用户操作。
