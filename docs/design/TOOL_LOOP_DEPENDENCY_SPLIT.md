# 模型与工具循环的职责拆分

状态：第8.2采纳首片已本地集成50cd9e7a7；请求周期及第8.3消息／估算底座候选组合18文件456项通过、24项既有xfail。未推送部署，真实TUI待组合包。

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
| 当前回合 | `_execute_tool_loop_service` | 中断、续租、已排队工具、模型采纳、插话重检、自然回复、结束／工具分派顺序不变 |
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
- C组摘要来源／严格失败开关及scope链未移植；新增分页原语不等于Compact宿主已按scope读回完整来源。需要继续审同源视图、摘要基础链、输出预留和三宿主接线，不批量导入decision/Jev。

请求周期五文件164 passed／4既有xfail；A/B七文件186 passed；最终18文件组合456 passed／24既有xfail，分组有重叠，不累加为总数。所有结果都是本地组件验证。
