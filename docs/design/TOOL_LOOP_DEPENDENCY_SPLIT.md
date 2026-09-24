# 模型与工具循环的职责拆分

状态：模型采纳／请求周期、有界历史及Compact候选已本地集成；工具事实和唯一循环入口组合随475c945a5集成，16文件349 passed／20既有xfail。跨请求工具编号误过滤正在修复；未推送部署，真实TUI待组合包。

解决问题：主循环同时承担请求准备、Compact事务、模型响应采纳、用户插话确认、工具调用记录和结束裁决；修改一种行为必须检查太多共享状态。按实际副作用边界分离，并保持原执行链唯一。

## 参考与核对范围

- Codex参考版本578c1b22：`codex-rs/core/src/session/turn.rs` 的 `run_turn`、`run_sampling_request`；前者负责回合推进与待处理输入，后者只负责一次采样及有界流重试，并携带产出响应的原输入。上下文超限显式返回上层，不在流重试里换一套历史。
- Hermes参考版本0a62610f1：`run_agent.py::_execute_tool_calls`、`agent/tool_executor.py::execute_tool_calls_segmented`；工具批次按原顺序分段，安全并行段与顺序屏障分开，整轮预算／插话只收一次。借鉴边界，不复制其完整agent参数或只转发包装。
- 已查两者合同文件索引与模块总表；索引只用于导航，上述具体函数才是本次源码阅读范围，不代表全仓逐行审查。
- 本仓核对 `_tool_loop_service.py` 的请求准备／采纳／循环、`tool_model_generation.py` 的物理调用、`runtime/goal_accounting.py`、`tool_loop/round_execution.py`，以及工具循环、输入引导、上下文溢出与中断直接测试。

## 原链路和不能改变的顺序

| 职责 | 现有权威入口 | 拆分约束 |
| --- | --- | --- |
| 工作片身份 | `runner/attempt_guard.py` | 旧attempt在开始Goal计量或发请求前拒绝；新模块不自行领取执行权 |
| 当前回合 | `execute_tool_loop` | 中断、续租、已排队工具、模型采纳、插话重检、自然回复、结束／工具分派顺序不变 |
| 请求准备 | `build_tool_loop_prompt`、`next_tool_loop_model_response` | 工具快照、孩子当前状态、原生历史和上下文压力仍沿原路径；恢复用当前请求配对，不混用上一轮prompt |
| 模型运输 | `generate_model_response` | 超时、流式、供应商调用账和取消仍用现有线程／执行器，不另起请求旁路 |
| 模型响应采纳 | `_model_turn_or_retry` | Goal开始→原瞬断重试→Goal用量→输入确认／恢复；异常中只有既有结构化条件允许返工 |
| 插话提交 | `runtime/guidance.py` | 已提交但结果不明不能自动重发；显式provider超限才恢复原输入批次；preflight不伪造服务商接受 |
| 工具轮 | `tool_loop/round_execution.py` | 原身份、审批、并发冲突与记录回调保留；模型原参数与宿主补全参数不混用 |
| Compact | 当前checkpoint／generation与会话Store | 读取、候选、校验、CAS、发布顺序不变；不新增摘要权威或独立执行器 |
| 完成 | `response_decision.py`、`completion.py` | 当前结构化响应、控制、直属孩子与未读事实裁决；工具成功不等于内容正确 |

新入口只能接收该职责需要的事实与操作。不能把整个Agent、Store或主循环服务换名装入新context；请求准备所需宿主能力在原装配点绑定。回调必须能对应上表一个明确副作用，不提供任意get/set访问器。

## 首片与后续顺序

1. 先分离模型响应采纳／失败恢复，保留产生请求与压缩的原调用点。拆分范围须覆盖旧attempt拒绝、Goal计量、瞬断重试、输入确认、空响应修复，并用现有集成测试检查顺序。
2. 再核对请求准备与Compact之间的真实依赖，逐片迁出；单轮工具执行继续使用现有模块，不新建工具执行器。
3. 最后清理主循环中只转发的旧入口及无调用导出，保持取消和结束决策仍可从主循环直接看清。
4. 每片先合同／fake验证；组合完成且严格gate通过后发同一wheel，按唯一TODO开展多路原生TUI长任务矩阵。

## 独立候选边界

Compact／媒体开发线与主线存在大量decision/Jev前置改动。以f2bc98626比较共同祖先，累计391文件、约4.4万新增行；不能把候选整枝当成第8步重构合并。
先由原owner提供函数级依赖闭包，分别判断MessageStore有界读取、scope过滤、完整投影、估算与分段哪些能独立移植。选模、菜单、decision配置不因同枝出现就成为本轮必需功能。任何最小移植仍须检查原生媒体身份和请求完整性，不能删能力来躲测试。
当前原owner继续 `compact_request_budget.py`、独立source helper、`memory_archive/tokens.py`；主线先处理 `_tool_loop_service.py` 的响应采纳边界。候选自身的fake Store接口失败必须在集成时修正，不能称整体验证通过。

## 失败样本与验收

保留TUI225的父级验证后孩子覆盖摘要、226遗漏汇总、227检查脚本误报与不存在的shell工具申请。这些是后续模型／工具事实传递的检查输入，不在核心新增CSV、平方和或特定提示词分支。观察者不得修产物或通过第二条需求替模型完成任务。

开发验证关注同源prompt/response、同一输入只提交／确认一次、旧attempt不发请求、可恢复空响应次数、超限和中断不错误消费输入。真实验收仍按第8步完整矩阵，不能用本片单测或第7步TUI替代。

## 首片实现与验证

`tool_loop/model_turn.py::sample_and_accept_model_response` 只接请求、重试许可、用量记录、输入恢复、输入确认五项绑定操作，以及原policy/on_chunk。模型采样仍使用原瞬断恢复器，先记录用量，再根据结构化超限来源恢复或确认输入；请求和响应原样成对返回。
旧attempt拒绝、Goal开始、插话接替空响应、一次修复和成功后清零仍在原装配点。`runtime/guidance.py` 统一查询原submission ID及待确认ID集合，模型轮和物理超时重试共用；pre-I/O原提交边未移动。删除原两个重复查询和一次使用的旧超限转发判断，不新增状态或配置。

新增十项合同覆盖采纳顺序、响应身份、失败时不确认、瞬断后动态投递状态阻止重发。三个原xfail空响应测试迁到实际native参数和当前工作目录，保留3／3／5次模型调用及真实工具／文件断言。完整十文件为212 passed、24 xfailed；尚未完成第8步整体发布与真实矩阵。

## 请求周期和有界历史候选

- `request_model_response` 在同一选择结果上绑定prompt组装、实际生成、输入恢复、原Compact回收、重试上限读取和原临时工具集合。首次返回后才读上限；provider超限先恢复再回收，成功后重新组装；最终prompt与response配对，异常／超限／辅助回执不消费工具声明。原`next_tool_loop_model_response`保留回执选择与绑定职责，删除三个迁完的旧helper。prompt正文、模型运输和Compact CAS未改归属。
- A组来自467f3cac3的四个消息底座文件及测试：`message_scan`统一冻结完整LF尾界和有字节预算的前向页；原`append_once`锁内扫描全部行，最多持一行和首个命中，不加载完整历史。命中后仍检查后续坏行；缺LF的尾行显式拒绝追加，防止拼坏记录，不自动修复。普通页的原游标合同保留，`through/max_bytes`为显式可选参数。
- A独立审阅的Unicode空白与极大时间戳问题已三项红转绿：先解码再按旧Unicode空白规则过滤；字段数值溢出归坏账，返回原游标，不错分为不可恢复程序错误。
- B组来自425bcb3a9的估算器及测试：按原JSON编码顺序流式累计字符和UTF8字节，结构开销和异常优先级等值；最大单值和字典排序仍会占用内存，不宣称完全常量内存。无调用的旧全文转换helper删除。
- 本片当时尚未移植C组摘要来源／严格失败开关及scope链（后续C最小来源片见下一节）；新增分页原语不等于Compact宿主已按scope读回完整来源。需要继续审同源视图、摘要基础链、输出预留和三宿主接线，不批量导入decision/Jev。

请求周期五文件164 passed／4既有xfail；A/B七文件186 passed；最终18文件组合456 passed／24既有xfail，分组有重叠，不累加为总数。所有结果都是本地组件验证。


## 第8步 Compact 候选与提交边界

状态：14文件组合333 passed、20项既有xfail；尚未发布部署及原生 TUI 验收。

- `tool_ir_compact.reduce_native_compact_candidate` 只借用原 IR、tool_context、归档引用三个列表和原完整请求估算回调；负责整对回收、摘要安装及窗口提示重估，不接 Agent、Store、权限或提交能力。
- 原装配点保留快照、取消、真实触发线和 checkpoint/CAS。只有提交前失败可恢复旧列表；提交后用量观察失效、运行态投影、TUI 与日志异常继续上抛，不再恢复已被 canonical 提交替换的历史，也不记作压缩提交失败。无持久绑定的临时回合没有 CAS，仍保留原投影失败回滚语义。
- 局部参考 Codex 578c1b22 `core/src/compact.rs` 的历史替换→用量重算→完成事件顺序；本次只读该局部，不宣称全仓审查。
- C 来源片只移植顺序字符窗口、两遍长度/hash、预算内分段及取消检查；移除未启用的 strict 参数，保持原摘要重试与明确降级摘录。非文本块不得仅凭 JSON 引用获得正文覆盖，出错保留原来源与检查点。
- 估算器只对精确内置、无环、深度有界且最坏JSON UTF8不超过512 KiB的载荷走公开dumps，其余仍流式。探测不调用自定义转换，保持JSON失败优先于UTF8错误的原回退；不修改GC策略，不放宽峰值断言。
- 持久格式、原 checkpoint ID、工具身份及 CAS 次序不变。独立线 v3/scope 来源闭包不是本次重构的隐式前置；完整迁移应另核合同，不能为方便移植整枝 Jev。
- 现有旧链将不同 request/attempt 的裸 tool_call_id 合并过滤的风险仍待独立修复；本片不声称完成 scope 隔离，也不把删掉的 strict 能力计入验收。

建议下一步：完成有界估算组合回归与剩余工具轮拆分，再统一发布并开展第8步真实矩阵；独立审阅可并行，同一生产模块只保留一个写入者。


## 第8步工具执行事实投影

解决问题：TUI213的进程清理回执留在canonical ToolResult的handler_details中，但正文归约和跨工作片恢复都丢掉process，模型只能看到退出码而无法区分命令结束与资源清理。

`tool_context/runtime_facts.py`只接结构化Mapping，统一原verification块及必要process投影；reducer的内联、指定live正文和外置摘要在最终脱敏前共用它。原工具正文不参与事实提取，ToolResult状态、错误、effect_outcome、调用身份和执行器不变。归档既有tool_result_envelope只补同一有界process投影，恢复入口再次读取同一结构和数量，原schema与持久权威不新增。

- 字段按存在性保留False、0、None；退出码两个原键不互补，child termination与session cleanup独立。
- 只投影短字符串、64bit以内整数和有限浮点；畸形或超大值略去，不变成零／成功。PID及实例列表只带数量，命令、路径、输出和诊断正文不复制。
- 当前与恢复投影均经过原脱敏；verification原块的顺序、标签及内容保持。此为已有执行事实缺失的修复，无新增开关、请求或写账动作。
- MCP及安装插件的远端structuredContent保持外部正文，不提升为handler envelope。管理员显式加载的进程内Python扩展本来就是受信代码，本片不新增来源证明或宣称可防御其伪造。
- 局部核对Codex `tools/mod.rs::format_exec_output_for_model`的正文／执行事实分离和Hermes `agent/tool_executor.py`归档后统一模型投影，不移植其执行链。

本片先以fake handler经过真实executor、archiver、循环记录和native adapter验证；不把组件测试称为原生TUI验收。建议下一步与空转发层清理组合后，继续完整第8步发布矩阵；两个生产范围分开写入。


## 唯一工具循环入口

删除只保存agent并转发同名函数的ToolLoopService及旧私有入口。runtime/loop_support直接调用execute_tool_loop；原循环仍按中断、租约、延迟工具、模型采纳、插话重检、自然回复及工具轮的既定顺序运行。工具回调仅用partial绑定原agent，不新增执行器或状态。

原11文件218 passed／20项既有xfail，相邻两文件104 passed／4项既有xfail；保留原断言并核对当前宿主身份。多次驱动同一测试时使用局部monkeypatch作用域，避免上一轮闭包污染下一轮。与工具事实片的组合验收另列TESTS。


## 发布前缺口：Compact逐调用来源（已确认，修复中）

旧committed_live_tool_compact_source_ids把整条thread链的source_tool_call_ids合并为裸编号集合，active_turn_compact再按call_id／id／scoped_call_id任一命中隐藏。后续普通请求或另一attempt复用厂商编号时，新结果会从恢复模型上下文消失；原执行账仍存在。仅比较checkpoint的request_id／attempt_id不够：它们标识提交者，后台与子代理carried来源可能混合多个早先请求。

修复边界：在现有live-tool checkpoint增加显式版本的逐调用来源引用，复用原canonical run_id／attempt_id／call_id，按存在性保留turn／operation等一致性事实。保留原checkpoint编号算法、顶层提交者语义、既有字段及锁／CAS顺序，不移植独立Jev的v3摘要scope链。新字段是来源引用，不另造工具执行身份或第二份状态。

- native在压缩前从真实ToolCall及配对结果冻结来源；carried逐条读原归档身份，不能用当前运行补齐，不解析scoped_call_id字符串猜身份。
- 源／尾选择按实际记录位置和精确来源，不能用裸编号集合相减；不同来源的同号调用允许分别属于源和尾。计数按真实来源，不因同号去重少算。
- archive、工具索引及carried重建贯穿已有attempt／turn字段，避免上游有身份、下游又丢掉。
- 新记录的精确来源才能授权隐藏；存量裸编号缺足够来源时保留并暴露结构化不确定事实，不静默绑定当前请求。孤立candidate仍不能隐藏任何内容。
- 验证需覆盖跨请求／attempt同号、混合来源checkpoint、精确旧调用隐藏而新调用保留、来源缺失、孤立候选及取消／CAS边界。当前为修复设计，不是已验证实现。

并行范围：逐调用来源片独立写上述入口；工具并发段判定片只写round_execution的段选择及窄模块，不移动真实线程执行、审批和记录顺序。其余完整Agent依赖有真实消费者，不能仅改context名称假装拆完。


## 收口依赖收窄（本地候选）

四份结束路径原样重复组装请求、生成回复、处理剩余工具请求与标记未完成；现在由closeout的三项原绑定操作和一次延后原因读取统一推进。原装配点仍决定轮限／显式硬门／未知副作用／无动作闸，Goal持久读取及阶段提示留原位置。删除迁完的三份旧收口入口及一次使用的阈值转发；失败软提示去掉未使用Agent依赖。

顺序保持build→generate→strip→读取原因→typed unfinished，返回产出这份响应的实际prompt。未知副作用的原因不能提前读：已执行且effect未知时，即使错误码可重试，也不得改成可自动续跑。新模块无执行器、请求重试、工具执行或持久写账。对应模型／工具请求仍由原ModelGenerateParams和原without_tool_call_after_limit执行。

验证比较本地基线和候选，并单独覆盖四阶段异常／中断不重试、不执行后续步骤、当前宿主绑定、响应与用量保留以及剥离后才读halt事实。未代替真实TUI。
