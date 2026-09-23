# 决策模型第 12 项：完整输入与 Compact 容量审计

日期：2026-09-22。性质：源码和公开接口审计，附后续 live 有界摘要实施记录；不是完整窗口验收完成报告。

本文首版只新增审计文档；随后按独立授权完成 live 有界摘要切片，改动和验证见文末。没有调用收费模型。
开始时基线为 `ffb44df21`，收尾复核到 `ef497a904`；同时读取父线未提交的 Jev 两层窗口守卫。
其他线正在修改同一 worktree，本文的函数名是主要定位依据，行号仅对应本次读取。

## 结论

现有执行链已经有完整模型输入估算、独立 child 会话、原生工具历史、provider usage 校准和事务化 Compact。
不需要第二份 tokenizer、历史、账本或 Compact。尚不能把第 07 项的创建前粗估当成完整容量证明：

1. `decision_subagent._input_budget` 算的是部分创建字段、父工具快照和父输出配置，遗漏真实 child runner 系统提示及执行输入；也有父工具全集、展示说明和原始 context pack 的多算。
2. 普通请求 preflight 只检查当前输入压力，没有结合本请求真正发送的输出上限；应补请求可容纳性，保留 Context 显示及 usage 的输入口径。
3. 同工作片 live Compact 保留当前选择投影；transcript Compact 的重建合同没有该投影，且其容量估算未使用完整 model surface。
4. 首次审计发现 live 摘要未复用 transcript 有界分段入口；此项已在后续切片接通，并验证分段失败/取消不删 IR 或提交代次。
5. 目标模型的模态/推理容量仍缺充分结构化元数据；通用字节启发式不等于供应商 tokenizer，校准指纹也尚未绑定完整连接版本。

Jev 原先只有 JSON 资源帽的缺口已经由父线补上。不得继续把“仅 256 KiB”“最多 64 题”作为当前实现结论。

## 一、创建前估算与实际 child 输入

| 层次 | 已核对的入口 | 当前事实及差异 |
| --- | --- | --- |
| 原创建规格 | `agent_core/orchestration_tools.py::_prepare_items/_decision_children`；`agent_core/hierarchy_tools.py::_decision_children` | 根创建沿原 `CreateRunParams`；递归创建沿原 `_child_create_params` 得到 canonical 规格。建议不创建另一份任务身份；原幂等复用项不重选。 |
| 当前估算 | `agent_core/orchestration/decision_subagent.py::_input_budget`，108 行 | `goal/description/thought/plan/context_packs`、短 `provider_system_instruction`、父冻结 snapshot 全部 specs 和父 `config.max_tokens`。这不是完整 runner 输入。 |
| 真实模型配置 | `settings/model_profiles.py::selected_model_config`，214 行；`agent_core/runner/worker.py` | 原 profile 连同连接/窗口一起冻结；目标 `max_tokens=min(原配置, 目标窗口//4)`。当前候选共用父输出值，没有逐候选应用这条原规则。 |
| child 系统与任务 | `agent_core/runner/prompts.py::subagent_runner_system_prompt/_build_subagent_runner_prompt` | 包含真实角色/身份、持续执行纪律、Runner Contract、Context Bundle Gate、精简执行上下文和输出要求，远多于短 provider instruction。 |
| 上下文事实 | `subagents/services/runner_context_service.py::build_execution_context/_make_execution_context` | 原任务、权限及 grants、角色模板、写边界、协作/直属孩子状态、恢复、待处理能力请求等参与实际组装。`write_execution_context` 会写文件，不能当创建前纯预览调用。 |
| 实际投影 | `agent_core/runner/prompt_context_summary.py::runner_context_summary_payload`，20 行 | 目标/计划、身份、refs、权限、写边界、conversation、context packs、recovery 等按原规则投影。`description` 是创建展示字段；context packs 只取前 5 项并有界化，当前预算却算原始值。 |
| 完整模型请求 | `agent_core/subagent/run_flow.py::_subagent_model_run_params`；`agent_core/_tool_loop_service.py::_render_tool_loop_prompt` | 真正使用 child system override、task_local、原 prompt builder、注入、scoped tools、loaded schemas 和 child 自己的 `conversation_history_seed`。 |

`_input_budget` 对 manifest 的 required/hint read paths、task pack、role pack、quality ref 或 pack 的 path/ref 返回未知，沿原模型。
这是目前保守边界；不能据此说“文件长度已经纳入窗口”。实际启动 prompt 通常仅包含 refs，正文要经原工具读取和分块后才进入上下文。
也不能把父线程全部历史当 child 起始输入：child 有自己的 canonical thread；父传递材料由原创建合同决定。

创建前还会把完整 `materials/tools` 交给 `decision_json` 检查。其 256 KiB/节点限制属于决策快照资源保护，
并非 250K/1M 生成模型的容量：长内联 child 材料可能在能否选模型之前使可选建议整体放弃。
将来应区分“本地完整输入估算”和“真正发送给 Jev 的紧凑客观事实”，保留各自资源限制，不把完整材料偷偷截断。

现有目录只声明 agentic 用途、窗口、模型和 backend；native tools 能力明确标为
`unknown_until_original_runner_probe`。原 runner 的真实 probe 仍负责原生工具支持，没有已验证依据就不能在建议中声称支持。
不应恢复文本 fallback，也不应为自动选模型新增收费探针或要求候选窗口必须大于父窗口。

## 二、唯一计数与请求预算的复用位置

| 复用位置 | 能做什么 | 必须保留的边界 |
| --- | --- | --- |
| `memory_archive/tokens.py::estimate_tokens`，85 行 | 当前通用 UTF-8/字符及结构开销启发式 | 没有目标模型 tokenizer 或图片计数；估算不能写成 provider 实测 tokens。 |
| `agent_core/model/context_pressure.py::model_visible_context_snapshot/model_visible_context_tokens` | 完整输入、组件和既有校准入口 | 使用真实 agent/params/prompt；不能塞入父 prompt 后声称得到了目标 child 容量。 |
| 同文件 `_model_visible_context_components`，434 行 | native 的 system、稳定 prompt、canonical provider history、当前 typed IR、尚未转发的 runtime guidance、真实 schemas | `project_native_prompt_history` 与发送链共用，避免用户输入/动态事实重复计数；本层仍是 canonical Anthropic 形状，最终不同 provider wire 有差异。 |
| `agent_core/model/context_window.py::resolve_model_context_window_tokens` | 显式配置、backend 元数据和原默认窗口优先级 | 继续尊重原显式配置，不从模型名称推断容量、不为容量主动联网探测。 |
| `agent_core/model/call_runtime.py::max_output_tokens`；`backends/http.py::bounded_output_tokens` | 原 backend/config 输出限额以及本请求覆盖 | 准入应对齐真正出站的协议字段；不能只读父配置。 |
| `agent_core/runtime/context_compactor.py::runtime_compact_policy` | 原 trigger、recovery、tail、失败熔断及持久权限 | 不另配一个 child 专用阈值，不把“可能生成的输出”计作已经占用的 Context。 |

普通 `preflight_context_pressure_response`（166 行）目前比较完整输入与 `window*trigger_percent`/window。
例如窗口 250,000、trigger 90%、输入估算 220,000、实际出站输出限额 50,000：输入还没到 225,000，
但输入加所请求输出已达 270,000。这是静态示例，不是一次真实供应商拒绝记录。

应在原预算层区分 `input_estimate` 与 `request_input_ceiling`：后者可约束 `window - 实际输出预留`，
达到后仍返回原 typed context-pressure/Compact 路径；不要把未来输出加进 Context 展示或 billing。
`tests/test_gateway_conversation_compact.py::test_conversation_pressure_does_not_reserve_unspent_future_output`
已明确保护这一输入显示口径，须保留。
`test_runtime_context_pressure.py::test_preflight_uses_configured_threshold_without_a_second_ceiling`
和 `test_subagent_runtime_compact.py::test_task_local_preflight_uses_the_configured_exact_compact_threshold`
还保护原触发百分比。新增的供应商客观准入边界应明确区分，不能偷偷改 trigger 或删除旧断言；
对应 fixture 须显式说明可用输出限额，未知上限不能用一个大于测试窗口的 dataclass 默认值伪装成协议事实。

推理预留也应读供应商协议事实。目前检查到的 Chat/Anthropic 发送 max_tokens，Responses 发送 max_output_tokens；
`backends/responses.py::_generate` 在 ChatGPT OAuth 模式会移除 max_output_tokens。
不能把 config 上限冒充该协议的硬输出上限，也不能统一加一个不存在的 reasoning reserve，或对已包含推理的总输出限额重复预留。
同时需区分供应商总窗口与独立 input limit：原 metadata 入口接受两类别名，只有确认输入输出共享窗口的协议才能直接相减。

原校准已绑定精确 thread、Compact generation 和稳定请求指纹；成功 provider input usage 才能更新，Compact 提交会失效旧观测。
但 `_stable_context_surface_fingerprint`（535 行）只有 backend 名、model 名、protocol、system、prompt 和 tools，
没有连接/配置版本。同名模型换端点或连接后可能复用不合适的旧校准。
建议在原 fingerprint 加入原配置冻结产生的安全版本身份，沿原观察存储失效；不新增校准 store，不把凭据原文写入状态。
创建前候选不得套用父线程或另一 profile 的 provider usage 比例。

## 三、历史与模态的证据边界

`agent_core/subagent/run_flow.py::_run_subagent_conversation_turn` 使用 child 自己的 thread，
`prepare_subagent_thread_turn` 准备历史；真实 context-overflow 才进入原 `_compact_subagent_overflowing_turn`，随后重建原 RunParams。
原 canonical messages 保留工具调用/结果及供应商推理块，不能为窗口筛选从正文猜配对或截掉中间轮次。

标准 child runner task 和 `ToolResult.render_for_model_prompt` 是文本；
`conversation/native_history.py::_normalized_native_messages` 仍按开放块合同保留 JSON content，不把未来 block 类型封死。
本次在 backend/model/context 路径没有发现可供候选筛选复用的图片尺寸/patch token/模态能力预算。
这不证明整个项目永远不支持图片，也不证明历史中不可能出现图片块；它说明“JSON 字节估算通过”不足以证明图片可送入目标模型。

最小处理是由原规范化输入提供结构化模态事实，原 backend 元数据说明已知支持/未知；无法证明兼容时保留继承并记录原因。
图片、音频不能让 Jev 直接读取；不应自动下载媒体、做 OCR 或增加一次模型请求来满足可选建议。
历史大于目标窗口时先走现有分段 Compact；不可压缩的当前用户要求、固定 system/schema 本身超窗时，
排除该候选或交原任务拆分，不静默截断要求，也不能循环压缩已经不能继续缩小的输入。

## 四、Compact 与第 10 项展示投影

### 同一个正在执行的工作片：主要接缝已有

`_render_tool_loop_prompt` 将 `selected_skill_ids/required_skill_ids` 交原 `ToolSections`，选中名卡进入动态推荐段。
`tool_ir_history.py::project_native_prompt_history` 把分段动态事实按 source 放进原 typed IR；
`drop_tool_call_pairs` 保留每个来源最新 RuntimeFactsTurn、真实 UserTurn 和未退休工具尾部。
`_native_compact_floor_tokens` 用 `replace(params,...)` 做副本，保留 selection 和真实工具快照。
`_native_tool_history_summary` 传当前 provider_prompt、同一 resolve_native_tools 结果和 canonical history；
`compact_semantic_summary._compact_cache_safe_prompt` 只追加摘要指令，保留稳定前缀。
因此源码不支持“所有 Compact 都丢失 selected Skill”这一结论。

### transcript Compact／溢出重建：展示事实没有传进去

`conversation/compact_provider_surface.py::ConversationCompactModelSurface` 只有
allowed_tools、prompt_files、system override、context_scope、loaded_tool_names。
`prepare_conversation_compact_provider_surface` 重新 prepare/snapshot 并用没有 selection 的 `ToolSections` 构造提示：

- conversation/default 会恢复原 Skill 索引，而选中展示原本只在稳定区留下固定发现说明。
- progressive 收起的 direct schemas 没有传来，可能恢复为原默认 schema 集合；真实 loaded 仍另行保留。
- task_local 仍守隔离，不能据此说泄露 owner 人格；但该工作片的选中动态名卡不在这次新构造中。

这是容量和缓存请求面的一致性缺口，不是新增权限。相关构造方是
`agent_core/subagent/run_flow.py::_subagent_compact_model_surface`、
`gateway_parts/request_context.py::_load_gateway_compact_context`、
`conversation/background_execution.py` 和手动 Compact 的 `gateway_parts/control_service.py`。
手动/全新工作片没有旧选择时维持 None 合同；不要凭摘要重建选择，也不要持久化活 handler 或造第二 Registry。
同工作片可显式携带原不可变展示事实；新工作片重建授权 snapshot 后可按既有阶段策略重新判断。
跨新工作片的选择变化本来就可能合法，不能承诺无条件缓存命中。

另一个已有缺口是 `conversation/compact.py::_projected_context_tokens`（830 行）：
它调用默认 `agent.prompts.build`，再估算摘要/证据/history，未接 model_surface、真实 child system、实际 native schemas。
legacy/native 取 max 可防漏掉原生历史，却不能补上这些缺项。应让检测、分段和摘要发送复用同一完整 surface。

### live 摘要请求自身的容量边界：后续切片已接通

初次审计时，`memory_archive/compact_semantic_summary.py::_resolve_generate_with_messages` 直接调用
`generate_auxiliary_model_response`，后者有原并发/传输/账本，但没有整包输入窗口 preflight。
`_fit_native_ir_to_shared_budget` 先取得摘要再回收旧 IR，单次巨大工具结果可使待摘要请求也超窗。

当前实际 agent 分支已复用 `conversation/compact_request_budget.py::generate_bounded_compact_response`：
它对完整 prompt/messages/tools/system 用原 estimate_tokens 估算，按 `window*0.8-max_output_tokens` 预留，
够用时保留原请求对象；超大或明确 ProviderContextWindowError 才走顺序分段覆盖。
其全部完成后才由原 Compact 提交；失败/取消不推进覆盖游标，工具配对原记录和 checkpoint/CAS 不变。
原 run/线程取消回调已贯穿每段请求及候选返回前检查；未修改通用辅助调用、原分段算法或失败分类。

## 五、Jev 公开容量与当前实现

本次只读核对 [官方 Models](https://docs.typesafe.ai/models)：`jev-latest` 当前指向 `jev-1.13.0`，
总 state 加所有题目 64k tokens，state 加最长题 32k；仅文本输入。
[API](https://docs.typesafe.ai/api) 的 questions 是 map；Choice 的 255 是每题候选项数量。
[Choice](https://docs.typesafe.ai/primitives/choice) 说明各题独立并行，不能设计依赖其它槽位已选答案的题目。

读取官方 Python SDK 固定提交 `0ffd094c72ed9445223060b24ffd7a56aa781fb4`：
[Request.questions](https://github.com/typesafe-ai/typesafe-sdk-python/blob/0ffd094c72ed9445223060b24ffd7a56aa781fb4/src/typesafe_sdk/_schemas/models.py#L210)
只有 min_length=1；[normalize_questions](https://github.com/typesafe-ai/typesafe-sdk-python/blob/0ffd094c72ed9445223060b24ffd7a56aa781fb4/src/typesafe_sdk/_core/questions.py#L10)
校验非空/题型；[endpoint](https://github.com/typesafe-ai/typesafe-sdk-python/blob/0ffd094c72ed9445223060b24ffd7a56aa781fb4/src/typesafe_sdk/_core/endpoints.py#L17)
原样发送 normalized map。本次所查 API/SDK 未发现统一 64 题上限；这不等于服务无限题量承诺。

`typesafe_decision.py::decide` 发 HTTP 前调用
`typesafe_decision_wire.py::validate_typesafe_request_window`，取配置窗口与官方 64k/32k 的较小值。
首次用序列化 UTF-8 字节数直接比较 token 上限；全仓回归发现能力推荐的 78,419 字节请求因此在网络前误拒，六个 localhost HTTP 用例失败。现改用项目原 `estimate_tokens` 留一成余量筛掉明显超窗请求；供应商才是最终容量权威，估算不能保证容纳，也不能当 usage。原 256 KiB/节点/深度是独立资源硬帽，旧 MAX_QUESTIONS=64 已删除；无新 tokenizer/store。
本次两组 focused **24 passed**，包括原先失败的 localhost 成功、超时、在途修改及 401/500 回退。
父线的 7 次真实接口记录见 [真实验收记录](DECISION_MODEL_REAL_VALIDATION.md)；本文没有重新调用或独立重做该验收。

## 六、最小修复拆分与定向证据计划

| 顺序 | 最小修复位置 | 必要证据，不需真实收费模型 |
| --- | --- | --- |
| 1 | 原 `model/context_pressure.py`、`context_window.py` 与既有输出限额入口 | 250K/1M 配置分别捕获实际 prompt/messages/tools/system；输入近窗且 output cap 大时在发送前进入原 Compact。保留未用输出不计 Context 的测试；OAuth 无出站 cap 不伪报保证。 |
| 2 | 原 `compact_provider_surface.py`、`compact.py` 及实际 surface 构造方 | 冻结展示下 transcript Compact 与恢复请求 stable prefix/schema 一致；None 旧字节不变、loaded/显式 allowed/搜索可达不变；task_local 不恢复 owner 内容；新工作片不复用过期权限。 |
| 3 | 原 `compact_semantic_summary.py::_resolve_generate_with_messages` 接既有 `compact_request_budget` | 巨大 tool arguments/result 和 reasoning 历史触发有界摘要，每段确在目标输入预算内、源覆盖连续完整；停止或任一片失败不提前删 IR、不提交新 generation；普通够用请求面不变。 |
| 4 | `decision_subagent.py::_input_budget`、原 runner prompt/context 的纯组装接缝 | 通过真实 ToolExecutor 创建，再以 fake backend 捕获真实 child 首请求；完整系统/角色/工具 schema 让 250K 不够而 1M 可用；小 child 可选小于父窗口的模型；不同输出 cap 逐候选计算。 |
| 5 | 原 `context_pressure` 指纹和 backend/input 元数据 | 同名模型换连接版本失效旧校准；不复制父校准；未知图片/推理能力保留原方案。结构化 image block 测试只证明合同处理，不声称已核对供应商实际图片计价。 |

第 4 项不存在可直接调用的完整无副作用 pre-create renderer：真实 context 当前从持久 task 构建。
最小可维护方案是从原 runner/context 拼装中抽出共享纯投影，消费原 CreateRunParams、原授权 snapshot 和目标冻结配置；
创建前未确定的宿主身份/引用必须明确未知或使用有根据的保守包络，不能生成假任务 ID、写临时 task 后宣称预览无副作用。
真实 worker 在首请求前仍必须使用完整当前输入复核；静态预估不能替代这一边界。
无法取得完整客观事实的项继续 capacity_unknown，不能为了标记第 12 项完成只测无工具小样例。

可复用的现有定向测试：`test_decision_subagent.py` 的真实 ToolExecutor 冻结 specs 测试；
`test_runtime_context_pressure.py` 的首次工具前 schema 计数和 provider 校准；
`test_subagent_runtime_compact.py` 的 child 独立 thread、原生历史、强制压缩和稳定前缀；
`test_compact_request_budget.py` 的完整分段覆盖/typed overflow/取消；
`test_decision_skill_projection.py`、`test_decision_capability_consumer.py` 的真实渲染投影。
这些现有测试源码提供复用起点，不等于已覆盖上述交叉组合。只读审计阶段未运行 pytest；后续 live 切片结果见文末。

## 交接与建议下一步

首次交接完成源码审计、公开 SDK 题数核对和最小修复计划；新增文件用
`git diff --no-index --check /dev/null docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md` 检查，没有空白诊断；
另以 Python 确认 UTF-8 可读、末尾换行、没有行尾空格及个人绝对路径。
未核验目标 provider 的真实 tokenizer、图片 token 规则、缓存命中率，也未证明 250K/1M 真实业务完成质量。
没有配置变更或提交；共享导航和 TODO 状态由主线统一维护。

建议下一步：先修原完整请求预算与 Compact 请求面，避免建议成功却在真实发送时超窗；再把创建前建议接到同一纯投影。
可由两位 agent 分别处理 model 预算/校准与 Compact surface/有界摘要，但须先划清 `context_pressure.py` 及 surface 调用方归属。
验收先用 fake provider 捕获实际输入和完整源覆盖，再由主线按已有授权做少量真实模型对照；守住原身份、权限、取消、历史和 CAS，不引入第二状态系统。

## 后续实施交接：live 摘要复用原有界发送

状态：2026-09-22 本地切片完成，未提交。此切片不代表第 12 项整体完成。

修改文件：

- `memory_archive/compact_semantic_summary.py`：实际 agent 分支改走原有界入口；`LiveToolHistorySummaryRequest` 增加可选只读 `interrupt_check`，开始和返回候选前检查停止。
- `agent_core/_tool_loop_service.py::_native_tool_history_summary`：仅将原 `_native_compact_interrupted(params)` 回调传入摘要；原计划、IR 回收、checkpoint 和 CAS 均未改动。
- `tests/test_compact_request_budget.py`：实际 live 调用完整覆盖超大工具参数、结果、thinking/signature 及原先历史，每个分段请求在原估算预算内，源 IR 不变。
- `tests/test_native_tool_ir_compact_and_orphan_sweep.py`：真实 main/child Compact 在第二段失败或第一段后 run 取消时不回收 IR、不提交 checkpoint/generation；更新原 fake backend 完整签名，区分一次逻辑 Compact 与多次物理摘要调用，保留连续源覆盖断言。
- 本文：记录本地完成部分和剩余边界。未修改原 `compact_request_budget.py` 或共享文档。

实现前对照本机成熟源码：`codex-main/codex-rs/core/src/compact.rs` 的历史副本、typed 中断和窗口错误分支；
`pi-main/packages/coding-agent/src/core/compaction/compaction.ts::generateSummary` 的原生历史转换、输出预留与 AbortSignal 传递。
只借鉴职责和取消边界；未复制 Codex 删除最旧源项的策略，继续采用本项目现有连续完整覆盖方案。

验证命令：

```bash
python3 -m pytest agent_py_agent/tests/test_compact_request_budget.py agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py -q --tb=short
python3 -m pytest agent_py_agent/tests/test_subagent_runtime_compact.py -q --tb=short
ruff check agent_py_agent/agent/memory_archive/compact_semantic_summary.py agent_py_agent/agent/agent_core/_tool_loop_service.py agent_py_agent/tests/test_compact_request_budget.py agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py
```

结果：四个测试文件合计 113 项通过；Ruff、认领文件 diff 空白检查通过。
使用原 code-size checker 的 `_check_ast`、原 baseline 和 `compute_strict_blockers` 只读检查两个生产文件，新增 strict blocker 为 0，未写共享报告。
已有真实协议捕获测试继续证明能容纳的摘要保留原 system/tools/message 缓存面；没有把 fake 用量当真实 tokenizer 或缓存命中率。

剩余边界：没有 agent 的历史测试/裸 backend 适配保留原直接调用，生产 live 路径始终传入实际 agent；
原 live Compact 的参与条件（包括可回收工具对数量）、transcript 展示投影、创建前估算和目标协议输出限额均未扩大或改写。
有界发送沿原启发式计数及明确 provider overflow 缩段，不声称提供供应商精确 tokenizer 保证。
空/截断/工具调用回复沿原分段有限纠正和标注降级；传输异常或用户停止仍在候选阶段失败，不能以部分成功授权回收。

建议下一步：主线先联合检查这两处生产接线，再继续独立的 transcript surface 和完整请求预算切片；
可以与创建前纯投影调查并行，但避免同时修改 `_tool_loop_service.py` 同一函数。保持原失败/取消/覆盖/CAS 合同，不另造摘要状态或 executor。


## 后续实施前设计：创建前容量应共享真实准备对象

状态：2026-09-22 完成只读调查与接口设计，随后获主线授权落地准备、路径、原创建链和候选输出配置四个切片，详见本节末尾交接。负责人：child-capacity 线。
本线基线 `ef497a904`；初次调查仅追加本文本节，未改 `decision_subagent.py`、测试、共享导航或其他线文件。
原认领范围不足以提取完整首请求；主线已要求先提交精确设计，不在选择器中复制 runner 或虚构任务。

### 新核对的客观证据

- 原 `test_decision_subagent.py` 共 27 项通过，定向 Ruff 通过；这些用例覆盖选择、权限、幂等、期限和锁外请求，未捕获真实 child 首请求。
- 临时目录诊断通过真实 `ToolExecutor` 创建一个 canonical child，再走原 `run_subagent`，仅 backend 使用已有 fake：普通目标“核对输入容量”、关闭工具、输出配置 4,096 时，创建前返回 4,180，其中输入只占 84；原 `context_pressure._model_visible_context_components` 捕获首请求 raw input 为 9,471，fake 实际收到的 prompt 单项为 7,647。共一次模型调用、零次摘要、一个 child，runner 返回 ok。
- 上述数字是本地原启发式的诊断结果，临时路径/版本会影响数值；它只证明首请求漏项，不是 250K/1M 容量验收、provider usage、tokenizer 或业务质量证据。
- 原 `selected_model_config` 的临时配置诊断：父 `max_tokens=100000` 时，250K 候选实际配置为 62,500，1M 候选为 100,000；现 `_input_budget` 仍统一加 100,000。这个差异可使小窗候选被误排除，不能只修漏算而继续共享父输出。
- 原 worker 还在选择 profile 后执行 `apply_task_runtime_config_overlay`；候选评估必须使用与 worker 相同的最终冻结配置和来源，不能把裸 profile row 当最终发送合同。
- 主线指定的隔离配置在读取时仅有官方 `MiniMax-M2.7` 生成候选：`anthropic_compatible`、配置窗口 200,000、官方 `https://api.minimaxi.com/anthropic` 端点。隔离 TUI YAML 未显式配置输出上限。DeepSeek-V4-Flash、MiniMax-M3 由主线另行添加，本线未声称它们已经就绪、未改默认、未发收费请求或打印凭据。

成熟参考核对：先读取 `codex_contract_code_files.xlsx` 与 `free-code_contract_code_files.xlsx`；
Codex 索引未命中本次精确目标文件，随后直接查 `codex-rs/core/src/agent/control/spawn.rs` 的原生成配置、继承和单次创建预留。
Free-Code 索引定位 `src/tools/AgentTool/forkSubagent.ts`；该文件传递父级已渲染 system 字节与完整工具池以保持缓存，
只支持借鉴“已冻结输入沿原链传递”的模式，不能据此把本项目 task-local child 改成父历史 fork，亦未复用其文本身份判断。

### 必须共享的接口，而不是选择器内的第二份 renderer

以下是初次调查的接口建议；前两行已按本节末尾的第一切片落地，后续接口仍待实现，落地前须由主线扩展文件所有权。

| 原权威位置 | 建议提取的接口及输入/输出 | 必须保留的边界 |
| --- | --- | --- |
| `subagents/services/base.py::_prepare_run/_build_task/create_run` | 原准备阶段产生**随后由同一原创建流程直接提交**的 `SubAgentTask`，继续消费原 `CreateRunParams`；将其物化部分保留为唯一提交入口。 | 使用原 ID 生成和原路径/权限/role 规范化；同一对象只创建一次。不另建预览任务，不写临时 task，不造假 run/thread ID。网络后仍重查父权限、配额、幂等和替换条件；不把内存准备当成创建成功。 |
| `runner_context_service.py::ExecutionContextBuildRequest/_make_execution_context` | 让既有 request 显式携带已冻结的 context bundle、write boundary、role、直属孩子和协作投影；提取纯 `project_execution_context(request) -> SubAgentExecutionContext`，真实 runner 与创建前共同调用。 | 外层负责读取事实，纯组装不访问 manager、不 `save`、不调用 `write_execution_context`。新 child 的空历史必须来自原新建身份语义，不能用父 thread；启动前无法固定的事实返回未知。 |
| `runner/prompts.py`、`runtime/loop_support.py`、`_tool_loop_service.py::_render_tool_loop_prompt` | 从实际准备后的原 `RunParams`/`ToolLoopExecuteParams` 与原 child tools/protocol/prompt/Skill 快照投影首请求，再交原 `model_visible_context_snapshot` 计数。 | system override、task-local owner 边界、原 runner prompt、当前 typed input、schemas、选中展示与注入都沿原方法；不直接调用带消费、文件写入、MCP 连接或网络推荐的 prepare/build_tool_loop_prompt。 |
| `settings/model_profiles.py`、原 task overlay、原请求预算层 | 按候选解析最终生成配置，提供真实协议出站 cap；取原输入快照与原请求预算判定。 | 250K/1M 分别计算输出；OAuth 没发送 cap、模态未知、工具协议未经原 probe 等不能标成完整证明。连接版本仅用安全摘要，不复制父 usage 校准。 |
| `decision_subagent.py` | 消费上述只读结果，内部以候选 ID 映射完整输入估算、实际输出预留、窗口与来源版本；发送给 Jev 的仅为有界客观摘要。 | 本地完整输入不套 Jev 的 256 KiB JSON 资源帽；完整性或兼容性未知则保留继承。采纳前复核同一准备对象及候选版本，不扩权、不换已存在的 child。 |

原 `_prepare_run` 当前生成真实 run ID，`_session_identity_fields` 从同一 ID 生成 child thread ID；
`build_work_order_paths` 本身是路径计算，落盘在后续正式物化。这里可以提取原准备/提交接缝，
但必须一并调整根创建和递归创建的原准备对象，使网络后重建校验不会偷偷丢弃该对象再生成另一任务。
这属于原创建生命周期调整，超出只修改 `decision_subagent.py` 的最小切片，不能在私有选择器中绕开。

工具还有独立未知边界：`ToolRegistry.runtime_snapshot` 的可用性检查无副作用，但
`prepare_for_run` 会同步插件/MCP；`select_tool_protocol` 可触发原 backend probe。
父 `ToolInvocationContext` 的快照只证明父授权上界，不能自动充当真实 child 的工具/协议快照。
若创建前拿不到已验证、可版本复核的 child surface，应返回 `capacity_unknown`；不要为了可选选择提前联网或启动后台进程。

能力推荐发生在 child 的 `_execute_runtime_loop`，选中 Skill/schema 还可能变化。
共享投影需要显式冻结同一展示事实，或使用原代码可证明覆盖所有实际展示的上界；不能猜一次推荐结果、重发一次 Jev、
或仅把父全部 schema 相加就宣称是完整上界。新工作片授权/展示变化仍由真实发送前的完整 preflight 兜住。

### 下一切片的最小验收与交接

先实现并验纯准备/投影接缝，再接选择器；不能先用近似值把第 12 项勾选。
必要 focused 验收必须从真实 `ToolExecutor` 开始并捕获 fake backend 首请求，至少覆盖：

1. 系统、角色和原 schemas 共同令 250K 不够而 1M 可用；输入与 cap 逐候选计算，小 child 仍可选小于父窗口的模型。
2. root/child/grandchild 同一原准备/物化链；无预览落盘、无额外身份/线程、无 provider probe、无额外推荐请求。
3. 未知 external refs、模态、协议或未冻结展示保留继承；源历史/权限变化、撤销、停止、超时使旧建议失效。
4. 真正执行时 system、stable prefix、native schemas 和原计数请求面一致；不得继承父校准或把未来输出放入 Context 展示。

本轮验证命令：

```bash
python3 -m pytest agent_py_agent/tests/test_decision_subagent.py -q --tb=short
ruff check agent_py_agent/agent/agent_core/orchestration/decision_subagent.py agent_py_agent/tests/test_decision_subagent.py
```

结果：27 项通过，Ruff 通过。诊断只用临时目录和原测试 fake，目录随诊断结束清理，未新增生产或测试文件。
本文以 UTF-8、末尾换行、无行尾空格和新增文件 diff 空白检查复核；未提交，原创建前粗估风险仍在。
共享 `LLM_GUIDE`、设计台账、Goal/TODO 与测试导航由主线统一更新；本线没有把尚未实现的接口写成已落地能力。

建议下一步：主线先评审上述原准备/提交接缝的范围，再指定单一 owner 提取共享投影；当前只改选择器无法闭合完整容量。
可与原请求预算和 Compact 工作并行，但涉及 `loop_support.py`、`_render_tool_loop_prompt` 与原创建服务前必须重新认领，
守住原创建锁外网络、幂等、授权、停止、唯一身份和真正首请求 preflight；真实三模型验收仍由主线协调。

### 第一切片实施交接（2026-09-22，本地未提交）

本线目标：在原创建服务内准备随后原样提交的 `SubAgentTask`，并提取 live runner 共用的只读上下文投影。
本线只在主线指定独立 worktree 的 `codex/decision-model-integration` 分支开发，基线仍为 `ef497a904`；
不修改共享日常 checkout，不部署或提交远端。

实际完成及改动文件：

- `subagents/services/base.py`：新增内存 `PreparedSubagentRun` 与 `prepare_run`；可选准备对象由原
  `create_run` 在 creation guard 内复核规范化参数、父 canonical 代次、身份和权限，再按原顺序提交同一对象。
  原 ID/路径/role/权限算法未复制；复核只重建同身份的比较投影，不物化第二任务，重复发布拒绝在写入之前。
- `subagents/services/runner_context_service.py`：原事实读取集中到 `prepare_execution_context`，
  纯 `project_execution_context` 同时服务 live build 和准备对象，返回独立副本；原写出入口保留。
  request 显式保留缺失 canonical workspace 字段，不把尚未物化的路径当成完整首请求。
- `tests/test_subagent_manager_core.py` 与 `tests/test_subagent_effective_runtime_context.py`：覆盖 root/child/grandchild
  同一对象/身份提交、原线程和父链、无落盘准备与投影、输入/权限/父代次漂移拒绝、重复提交及 live 共用投影。
- `docs/modules/subagent/02-progress.md`、`04-structure.md` 与本文本节同步第一切片状态；未新增源文件、配置或持久 schema。

测试命令与结果：

```bash
python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_subagent_effective_runtime_context.py agent_py_agent/tests/test_subagent_context_bundle.py agent_py_agent/tests/test_subagent_context_bundle_prompting.py agent_py_agent/tests/test_decision_subagent.py agent_py_agent/tests/test_orchestration_create_subagents_idempotency.py -q --tb=short -o addopts=
ruff check agent_py_agent/agent/subagents/services/base.py agent_py_agent/agent/subagents/services/runner_context_service.py agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_subagent_effective_runtime_context.py
```

结果：95 项通过，Ruff、`check_doc_sync.py` 和定向 `git diff --check` 通过。复用原 `check_code_size` 的 AST 与 baseline
检查本线两份生产文件，新增 strict blocker 为 0，没有改写共享 size report。初次测试曾被其他线工具 schema
不支持 `uniqueItems` 阻断，主线修复该 schema 后上述完整命令通过；不是跳过失败用例。

影响和主线重点复查：此片改变原服务内部准备/组装方式，但可选准备提交尚无生产选择器调用；不新增网络、
token 消耗或后台任务。上层原幂等、取消、权限和配额门仍是调用前提，准备结果不授予任何新权限。
现有 normalized 参数必须相同才可提交，选择后直接改 `host_model_ref` 会被拒绝；下一片必须定义原链的重新冻结时点。

剩余风险：canonical workspace 路径只在原保存中生成，不能靠此片的空 `unresolved_fields` 证明完整容量；
尚未提取 child 的真实 prompt/tool/Skill 展示面，未对齐逐候选协议 cap，未做 250K/1M 或三真实模型容量验收。
TODO12 保持未完成；未知容量仍应保留继承，不能把近似输入改称完整上界。

建议下一步：主线先指定单一 owner 从原 workspace adapter 提取无副作用路径计算，并在原准备/提交链明确模型引用
重新冻结，再接实际 runner surface 与选择器。Compact 与请求预算可并行推进；触及 workspace adapter、
`loop_support.py`、`_tool_loop_service.py` 或选择器前重新认领，继续守住锁外 Jev、原身份及停止/权限复核。

### 第二切片实施交接：共用 canonical 路径投影（2026-09-22，本地未提交）

目标与范围：主线授权继续原路径切片，工作区、分支、基线和 owner 与第一片一致；
先核对原调用链并告知额外文件，再修改 `task_workspace_adapter.py` 与 `memory_archive/task_workspace/__init__.py`。
`persistence/service.py` 仅读；本片未修改 `base.py`、选择器、runtime loop、Compact 或外部模型配置。

原权威链与副作用：`persistence/service.py::_prepare_and_write_state` 调用原
`task_workspace_adapter.sync_task_workspace_fields`，后者调用 `ensure_subagent_task_workspace`。
`ensure` 仍唯一负责 mkdir、任务身份文件、父状态锁内合并、共享工作区、agent-run 文件、artifact 索引、
日账及 timeline；静态路径计算不执行以上操作。

实际改动：

- `memory_archive/task_workspace/__init__.py`：把原 root/run/task ID 与受信 task root 解析提取到
  `_task_workspace_path_inputs`；只读 `subagent_task_workspace_paths` 与正式 ensure 共用它和原 `_paths_for`。
- `subagents/services/task_workspace_adapter.py`：正式 sync 与 `project_task_workspace_fields` 共用静态字段映射，
  后者只修改 task 副本。工单、runner、共享、artifact 索引地址均沿原算法；日账路径/event ID 只接受实际 ensure 结果。
- `subagents/services/runner_context_service.py`：在路径副本上收集原上下文事实，继续使用第一片纯投影。
  准备对象和 run/thread 身份保持原值，提交仍用原对象；路径已可计算不表示目录已存在或模型请求已完整冻结。
- 两份原 focused tests 扩充默认/显式 task root 下的 root、child、grandchild，核对预览与真实保存后的
  `workspace_refs`、write boundary、context 路径、artifact manifest 地址一致；前后比对完整目录树和文件字节。
  另核对冲突 task root 沿原信任规则处理，预览既不新增也不覆盖日账事实。
- 同步子代理及记忆模块的 `02-progress.md`、`04-structure.md` 和本交接；未新增文件、持久 schema 或配置。

定向验证：

```bash
python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_subagent_effective_runtime_context.py agent_py_agent/tests/test_subagent_context_bundle.py agent_py_agent/tests/test_subagent_context_bundle_prompting.py agent_py_agent/tests/test_subagent_task_workspace_state.py agent_py_agent/tests/test_orchestration_create_subagents_tool_workspace.py agent_py_agent/tests/test_child_external_workspace_scope.py agent_py_agent/tests/test_decision_subagent.py agent_py_agent/tests/test_orchestration_create_subagents_idempotency.py -q --tb=short -o addopts=
```

结果：136 项通过，Ruff、`check_doc_sync.py` 和本线 `git diff --check` 通过；两片共四份生产文件复用原
strict AST/baseline 检查，新增 blocker 为 0，未改写共享 size report。新改测试的两处导入排版经 Ruff 修正后
纳入本次完整测试，没有跳过用例。静态路径取自原代码，不建立独立路径表，也未运行付费模型、部署或提交远端。

主线重点复查与剩余风险：`TaskWorkspacePaths` 保留原 runtime refs 默认结构，预览只消费静态路径，
不把默认 daily/pending 地址或空事件当成实际日账；原写入才回填真实日账。
第一片的 `unresolved_fields` 现在可为空，但它只说明字段可算，不证明文件存在或首请求容量完成。
模型引用重新冻结、工具/Skill 真实展示面、逐候选协议输出 cap 仍未实现；第 12 项保持未完成。

建议下一步：由同一创建链 owner 明确选中模型引用如何重新冻结到原准备对象，再与 runner surface owner
对齐真实首请求。可以与 Compact、请求预算并行，但改动 `base.py`、选择器或 runtime loop 前重新认领；
守住锁外 Jev、原身份、权限/父代次复核和未知保留继承，不因路径缺口已补就采用容量未知候选。

### 第三切片实施交接：原创建链提交同一准备对象（2026-09-22，本地未提交）

目标与范围：先只读报告准确创建接缝，获得主线 ownership 后依次接根路径和递归路径。
工作区、分支、基线与前两片一致；未改日常 checkout、外部模型配置、远端或首请求 renderer。

实际修改九份生产文件：`agent_core/orchestration_tools.py`、`hierarchy_tools.py`、
`orchestration/create_constraints.py`、`orchestration/decision_subagent.py`、`subagents/manager.py`、
`subagents/services/base.py`、`hierarchy/scheduler.py`、`hierarchy/schedule_idempotency.py`、
`hierarchy/scheduler_models.py`。其中最后三份位于 `subagents/services/`。

- 根和递归准备载体只在本批次内存里保存 `PreparedSubagentRun`。原幂等检查先于准备，锁外网络返回后
  重走原规范化、取消、权限和配额门，再将同一准备对象交给原 resolver；竞争请求已经创建的任务直接复用，
  不重选、不改其模型，也不发布原等待对象。关闭选择时不调用准备 callback。
- `base_service.refreeze_run` 沿原 creation guard 重建当前规范字段，并写回同一未发布 `SubAgentTask`。
  宿主采用模型引用或同批兄弟更新父 revision 后，保留原 run ID、created_at、父/root/thread/session 身份；
  重建比较不物化任务。正式提交仍经原唯一保存顺序，已发布对象在任何修改前拒绝重新冻结。
- 选择指纹绑定实际准备身份、有效权限及写根；采用前保留原目录、连接、用户候选范围、期限和配置复核。
  显式用户 `model` 继续优先。上层 `create_policy.add_current_conversation_attrs` 原有的父会话提升/绑定仍在原位置；
  不能将整个上层规范化误称为无副作用，纯 base 准备不会额外创建 child、线程或工作区。
- 共享首请求面尚未接通，生产 `_input_budget` 明确返回未知，沿原模型创建并记录
  `retained/capacity_unknown`。删除旧 goal/schema 加父输出 cap 的粗估，避免本片上线后假称容量已证明。
  候选 `provider_tool_support` 仍是 `unknown_until_original_runner_probe`，窗口不代替工具能力证明。

三份 focused tests 的本片改动：`test_subagent_manager_core.py` 覆盖重新冻结及发布保护；
`test_decision_subagent.py` 覆盖根/子/孙同一对象与身份、父 revision 更新、并发复用、权限变化、部分取消及
生产未知保留。采纳绑定用例明确注入测试容量事实，真实配置、存储与创建链仍使用原实现，不能算完整窗口验收；
`test_orchestration_create_subagents_tool.py` 的原取消观察 helper 透传新可选 `prepared`，不增加生产兼容分支。

分段验证：根路径 68 项通过，递归与创建组合 101 项通过；最终 23 个直接相关测试文件 **337 项全部通过**。
最终组合包含两条创建入口、root/child/grandchild、原合同/幂等/配额/写权限/QA/tool roles、上下文 bundle 与 workspace 状态。
首次完整组合的 1 项失败是原测试观察 helper 未接收可选参数，修正透传后同组完整重跑通过。
本片九份生产文件定向 Ruff 与原 strict AST/baseline 检查通过，新增 strict blocker 为 0；未写共享 size report。
同步子代理模块进度、结构与本文；未新增源文件、配置、持久 schema 或收费模型调用。

剩余边界：同一对象的创建与重新冻结已闭合，完整首请求容量尚未闭合；仍需让实际 child system、runtime injections、
workspace/execution facts、工具/Skill 展示、native IR/messages 与逐候选最终输出配置进入原共享请求投影。
缺少客观来源时继续 unknown，不能回退旧粗估；候选工具支持也需独立证据。

建议下一步：与请求投影 owner 核对稳定 API 和每个冻结输入的原权威来源，再认领唯一容量接线范围。
可并行进行文档/验收审计；创建链和请求面接线各由单一 owner 修改，守住锁外 Jev、原取消/权限、唯一身份，
真实 MiniMax-M3、DeepSeek-V4-Flash 选模验收由主线协调，完整首请求未闭合前不标记第 12 项完成。

### 第四切片实施交接：逐候选最终配置与输出 cap（2026-09-22，本地未提交）

范围：主线确认后仅修改 `settings/services/runtime_config_task.py`、`orchestration/decision_subagent.py`，
以及 `test_model_profiles.py`、`test_decision_subagent.py` 和本线文档。未改 renderer、runtime loop、worker、
后端发送或共享 `context_pressure`；后三者仅读核对并复用原接口。

- 原 `apply_task_runtime_config_overlay` 改为消费 `project_task_runtime_config_overlay`；路径解析、字段优先级、
  模型来源和记忆配置规范化仍只有一份实现。project 只读文件，不应用 `apply_log_level`；正式 apply 保留原副作用和无覆盖时对象身份。
- 创建准备逐 child、逐授权 profile 调用原 `selected_model_config`、只读 overlay 和 `get_backend`。
  已核对后端构造不会发请求、执行工具、探针或读取/刷新 OAuth token；不初始化第二 Agent 或写候选状态仓。
- 输出 cap 复用 `context_pressure._known_shared_window_output_reserve` 的原 HTTP/显式共享窗口判据；
  正式工具生成不提供额外 `max_output_tokens` 覆盖，所以它与对应原发送组包一致。返回 0 在候选侧记为未知，
  不能当零预留。ChatGPT OAuth Responses 的发送路径会删除该字段，保持 unknown；没有按模型名建立 cap 表。
- 输入和输出分开：`_input_budget` 仍因完整 child 首请求缺失而返回未知；当已有明确输入事实时，
  每个候选比较自己的输入加输出与窗口，等于窗口也沿原 preflight 边界拒绝。测试中的已知输入依然是绑定用例替身。
- `candidate_request_limits` 独立给出窗口、输出 cap/status/reason 与 `provider_tool_support`。
  enable_tools 为真时现有候选没有可复用的已验连接事实，仍是 `unknown_until_original_runner_probe`，
  即使父 ToolExecutor 快照存在、窗口和 cap 都已知，也保留继承。未提前探针以假称候选已可执行。
- 采用前复读相同候选的最终配置，按原进程盐 HMAC 比较；配置来源、系统、输出、overlay 或连接变化均使旧建议失效，
  不把秘密配置或 overlay 路径放入决策请求或任务诊断。

证据：先 106 项 focused 通过，再与原创建、递归、上下文、workspace、配置、OAuth 和真实发送前预检合并，
**28 个直接相关文件共 427 项全部通过**。新增离线测试截获原 Messages/Chat 适配器 `request_json` 之前的请求体，
核对 MiniMax-M2.7、MiniMax-M3、DeepSeek-V4-Flash 测试 profile 的窗口和 cap；这些是显式测试配置，
不是官方参数发现或三个真实模型可用性验收。该捕获只断言输出 cap，不证明完整 wire payload、工具 schema 或首请求容量；
后续仍需分别核对官方 MiniMax 的 Anthropic 兼容适配和 OpenCode 的 OpenAI 兼容适配。未发送外部模型请求。
另验 250K 候选使用原 62,500 cap、1M 候选使用原 100,000 cap，不共用父预算；
原 worker 与投影配置完全相同，文件/目录字节和父配置未变，只有正式 apply 调整日志。
一次新增 overlay 变更用例最初使用已非配置字段的 `tool_timeout_seconds`，原解析器正确忽略；
改为原真实字段 `runner_timeout_seconds` 并核实原创建的 ref 后通过，没有为该用例修改产品解析规则。
Ruff、`check_doc_sync.py`、定向 `git diff --check` 与审计文档 UTF-8/换行/空白检查通过；
两份本片生产文件复用原 strict AST/baseline 检查，新增 blocker 为 0，未改写共享 size report。

建议下一步：先审查下述 A/B 生命周期方案并确定单一 owner，完整首请求未知继续保留原模型。
可并行审计配置与协议证据；真正的异模派工仍需真实首次输入、候选工具支持及模型引用原子冻结共同闭合。

### Hybrid 首个合同切片交接（2026-09-22，本地实现，完整容量未闭合）

工作线：`decision_child_capacity`；分支：`codex/decision-model-integration`；基线 `ef497a9`。
目标是保留 create 前一批一次的 Jev 语义建议，并把建议与实际模型采用分开。本片不修改真实 worker 请求链。
最终必须由主代理派工流程全自动完成 Jev 建议→宿主首请求/候选核验→采用或保留→执行，
不能要求用户逐个 child 选择、确认、补步骤或采用。pending 只等待宿主验证，不是用户审批；
显式 model/菜单选择优先仅作为已有配置/任务硬约束防竞态。本片不是 P2/P4 完成或实际异模派工验收。

- `decision_subagent.py` 仍在原父批次期限内复核设置、连接、候选范围、task 指纹和逐候选最终配置；
  未知输入容量、输出 cap 或工具能力不会成为采用证明。返回值为 init-only typed advice，原 `host_model_profile.v1` 不变。
- 根/递归包装器把建议绑定原 `PreparedSubagentRun`，`refreeze_run` 保留同一 task/run/time/谱系；
  参数或权限变化丢弃旧建议。原幂等复用优先，已落盘任务不重新建议或重冻。
- `PendingSubagentModelAdvice` 只含稳定 profile ID、来源 owner/thread 设置 revision、创建 operation 和源/child 身份；
  `pending_metadata` 校验准确 child，绝不存原响应正文、monotonic deadline、进程 HMAC、密钥或价格/费用。
- `ensure_subagent_thread` 只接受显式宿主参数，不从 task attributes 恢复建议；原工具创建策略移除伪造的 pending 属性。
  `agent_thread_store.ensure_agent_thread_record` 只在新建 thread 的原子写入保存 `host_subagent_model_advice.v1/pending`。
  existing 分支沿主线已修复的 `update_atomic` 合并最新线程，不重植建议，不覆盖模型或 Compact 状态。
- `thread_model_profile_id(select=...)` 将用户模型选择和 pending→retained 一次写入；选择同值也终结建议，
  原因是 `explicit_model_selection`。只有实际换模型才清除旧模型用量校准，其余 metadata 保留。
- 本片没有 adopted 入口。普通读、重启、原 worker 都继续使用 thread 的有效 `model_profile_id`。
  创建后设置/连接变化的首启重新验证仍未接通；持久 pending 即使尚未终结也没有采用能力。

崩溃边界：thread 初始化及 pending 是单文件原子写；随后 Goal、runtime.db、canonical task、父 child 链仍各自提交，
`creation_guard` 不提供跨存储回滚。若 thread 已写而 task 未发布，重试可校验同一身份；已有 thread 的显式选择和 retained
不能被旧准备载体覆盖。当前没有跨进程 Jev exactly-once 或“请求未发”的持久证明，不能从空内存账本推断首次资格。

影响范围：仅开启 subagent_model 的建议准备，以及存在 pending 时的显式选模。默认关闭不生成建议、额外请求或新状态。
本片仍可能留下已经按继承模型执行过的 pending；后续采用必须有真实首次请求栅栏/资格证明，不能自动升级旧 pending。

验证：使用真实配置、manager 和 canonical thread，替换 Jev/发布网络；去掉原测试中固定 8192 输入容量假设。
覆盖根/子/孙同一准备对象、兄弟更新父 revision、显式模型、关闭/观察、配置及连接失效、取消、持久幂等、
新线程字段白名单、同值/异值显式选择的原子终结、重启读取不采用、伪造属性、线程已写/task 未发布的断点重试。
收尾组合 278 项通过，命令如下；不以 fake 测试或 cap-only payload 捕获证明真实完整首请求可用。

```bash
python3 -m pytest agent_py_agent/tests/test_decision_subagent.py agent_py_agent/tests/test_thread_model_selection.py agent_py_agent/tests/test_conversation_store.py agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_orchestration_create_subagents_idempotency.py agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_model_profiles.py agent_py_agent/tests/test_subagent_hierarchy_contracts.py agent_py_agent/tests/test_subagent_effective_runtime_context.py -q --tb=short
```

本线 8 个生产文件和 focused test Ruff 通过；doc-sync、定向 diff whitespace、原 strict code-size AST 判据均通过，
新增 strict blocker 为 0。code-size 使用原 collector/baseline 只读检查本线文件，没有覆盖共享报告。
测试同时修正旧候选删除用例错误操作名：原 `delete` 不受支持，异常被可选建议边界吸收，不能作为真实撤销证据；
现使用原 `delete_model/save_provider` 服务，并验证配置变更确实完成。

建议下一步：先评审此唯一线程权威和原子终结合同，再认领原 startup/request 生产链，接完整首次请求验证和发送栅栏。
模型配置/权限/协议与可见输入必须来自同一实际 child，未知 retained；不能恢复旧粗估或父快照。
自动接续入口必须覆盖 `runner/worker._run_subagent_worker` 的原准确 attempt/lease，
`subagent/run_flow._run_subagent_conversation_turn` 中早于任何 Compact 请求的首次资格边界，
`runtime/loop_support._tool_snapshots_for_run/_execute_runtime_loop/_tool_loop_execute_params` 的原工具 probe 和展示，
以及 `_tool_loop_service` 的真实 prompt/Goal/workspace/IR 冻结。通过后在原 child thread 一次 CAS 写 profile 和终态，
`tool_model_generation.generate_model_response` 发送前必须看见已终结状态；原模型 scope/worker 复用同一验证配置和后端。
任何缺首次资格、设置/连接/权限变更、probe 失败或容量未知都由宿主自动 retained，取消继续传播。
旧 pending 不能因升级而获得首次资格；这部分需要下一片明确的持久发送栅栏，不得靠空回复或内存记录猜测。
可并行审计两种 provider 的原出站组包，但启动编排及最终采用只能由一个 owner 接线。
共享 Goal、DESIGN_LEDGER、TESTS、LLM_GUIDE/导航由主线协调；本线不编辑这些共享文件、不提交远端。

### 首请求生产方与下一生命周期方案（早期 A/B 只读比较，后续方向见上述 hybrid 切片）

共享 `ToolLoopRequestInput` 已提供无副作用投影，但不提供缺失运行事实。当前准确来源如下：

| 冻结事实 | 原生产入口与当前限制 |
| --- | --- |
| child context、runner task/system | `runner_context_service.prepare_execution_context/project_execution_context` 与 `runner/prompts.prepare_subagent_runner_prompt/render_subagent_runner_prompt/subagent_runner_system_prompt` 已同源。真实 `subagent_mixin._build_subagent_prompt` 在激活/通道 probe 后写出上下文；`prompt_context_summary._runner_identity_payload` 明确发送 status、runner_attempts 和错误，恢复 preflight 也进正文，不能用 PENDING 创建态替代。 |
| 激活/恢复状态 | `subagents/services/lifecycle_runner_attempts.prepare_runner_attempt` 在 creation guard 内按 exact pending 激活 RUNNING、更新 attempt 和 `runner_recovery_preflight`，并写日志。`services/channel_probe.probe_channel` 另有原检查/状态写入；二者不能为预览直接调用。 |
| 独立 child thread/history | `conversation/agent_thread.ensure_subagent_thread` → `agent_thread_store.ensure_agent_thread_record` 是唯一线程创建；`prepare_subagent_thread_turn` 写入当前输入并经原 Compact 后生成 history seed。新线程的初始空历史可以从原构造合同提纯证明，但当前尚无该纯生产接口，不能直接填 `[]` 冒充。 |
| child 工具与协议 | `runtime_mixin._run_once_with_params` 调用原 `tools.prepare_for_run`，随后 `loop_support._tool_snapshots_for_run` 取得 child scope 的 snapshot，并经 `native_tool_protocol.select_tool_protocol` 原探针确认候选能力。父快照只证明父权限上界；backend 正探针缓存只绑定原实例，当前候选目录不携带可验证缓存引用。 |
| Skill/schema 展示 | `loop_support._execute_runtime_loop` 中唯一 `recommend_capabilities` 产生原本片展示；它绑定 child/attempt/连接/窗口，可能请求 Jev。不能为了预览再推荐一遍，也不能把父展示套用给孩子。 |
| 原 native 首轮载体 | `loop_support._tool_loop_execute_params` 从真实 history seed 与 carried archive 得到 loaded、IR/prior、workspace 和 attempt；转换分别复用 `_native_initial_tool_ir_history`、`_native_provider_history_messages`。`resolve_native_tools` 和 `tool_model_generation._model_turn_tool_choice` 唯一确定发送 schema/choice。 |
| 最终 prompt 事实 | `build_tool_loop_prompt` 原位刷新直属孩子、领取插话并整理窗口；`_render_tool_loop_prompt` 收集 runtime injections/workspace/execution facts。Goal helper 会写本轮 attrs 的 CAS revision。只应把这些原准备结果交给 `tool_loop_prompt_request` 和 `PromptBuilder.prepare_render_input`，不在纯投影中重做副作用。 |
| 确定发送与恢复 | `tool_model_generation.generate_model_response` 先物化 IR、统一 preflight，再建立原 model call 记录并发送。`ModelCallLedger` 是有界内存记录，缺少记录不能证明重启前未请求；仅检查空回复/零 token/runner_attempts 也不能证明尚未生成。 |

方案 A：创建前确定性保守上界。

- 可继续用同一 prepared.task、原上下文/runner/prompt renderer 和已知 config/cap，但只接有客观来源的输入。
  完整授权 schema/Skill 名卡若要作上界，必须由原展示逻辑证明覆盖实际选择，不能假设“更多工具文本”总包含全部动态段。
  状态/恢复、history、插话、插件刷新或候选工具支持没有确定上界时，结果仍 unknown。
- 优点是保留原整批一次 Jev 和创建前冻结，不改执行生命周期。限制是创建到启动之间还允许事实变化；
  任意延期派工或新插话无法仅靠当前快照证明未来首请求可容纳，真正发送前仍必须走原 preflight。
- 下一最小实现若选择 A，先认领 `conversation/agent_thread.py`、`agent_thread_store.py` 和
  `subagents/services/lifecycle_runner_attempts.py` 的纯新线程/首次激活投影提取，原写入继续唯一；
  `runner_context_service.py` 消费其结构化事实，`decision_subagent.py` 只组合共享投影。
  在真实启动等价测试证明这些源之前，不为“创建前”名义手填 RUNNING、空 history 或 attempt。
  工具/展示仍须另行提取，因此这片本身仍不足以开放三个真实有工具候选。

方案 B：原激活和原工具准备后、首次生成前选择并持久冻结。

- 此处“首次生成”指 child 的业务请求；Jev 和有界工具协议 probe 也是实际模型请求，需沿原账本记录，不能称为零模型调用。
- 原创建照常生成一个 task/thread，并记录“本次新建、未显式 model、允许首次选择”的宿主结构化事实；
  原复用和历史任务不能重新进入。显式 model 从创建起已终结选择资格。
- 在真实 `run_flow._run_subagent_conversation_turn` 和 `runtime_mixin._run_once_with_params` 的第一模型轮，
  消费已激活任务、原 thread seed、child 工具准备及原协议事实。候选先作为未采用建议；需要工具时，只能在
  原候选 backend 的 probe 确认后采用，不能拿继承 backend 的 probe 给异模候选背书。
  最终候选必须以自己的 config/连接和同轮展示重建共享请求面，容量与工具能力均已知后才能冻结。
- 应拆开原 runtime 的“收集事实/确定模型/展示/发送”接缝，使能力推荐只执行一次；换候选后依赖连接/窗口的
  展示不能沿用旧绑定。所有 Jev/probe 在锁外；回锁复核 exact attempt、取消、owner/候选目录/显式选择和配置版本。
- profile 的唯一持久权威已经是 `ConversationThread.model_profile_id`；不能只改旧 task 的初始化 ref。
  首次选择的 pending/frozen/retained 状态宜与 profile 在同一个原 thread atomic update 中提交，并绑定 run、
  当前 attempt、创建来源和配置版本。请求发送必须在该提交之后；崩溃恢复读 frozen/retained 而不重选，
  缺少该新建资格记录的旧任务保持原模型。用户显式切模型与取消赢得 CAS 时，旧建议丢弃。
- 最小生产文件范围需要重新认领：`orchestration/decision_subagent.py`（沿用候选/版本/建议逻辑）；
  `orchestration_tools.py`、`hierarchy_tools.py`（原创建来源与显式选择资格）；
  `conversation/agent_thread_store.py` 与 `settings/thread_model_selection.py`（唯一 profile/首次选择原子状态）；
  `agent_core/subagent/run_flow.py`、`runtime/loop_support.py`、`runtime_mixin.py`（真实第一轮事实准备与选中依赖绑定）；
  `_tool_loop_service.py`、`tool_model_generation.py`（原最终请求冻结、发送前门与恢复封口）。
  `settings/model_scope.py` 只在需要为未采用候选共用原依赖绑定时加入；`native_tool_protocol.py` 的现有 probe 仅读复用，
  若需增加只读已验缓存接口再单独认领。不得用新 Agent/任务执行旁路完成预览。
- B 更贴近真实首次请求可用性，但会把现有整批建议变为各 child 首启前的选择；若必须保留整批一次 Jev，
  还需原调度器协调整批首请求准备，范围和等待行为更大。不能声称此替换只是原创建点的内部小重构。

推荐先评审 B 的“一次新建资格 + 原 thread profile 原子冻结”合同，再以同一创建的 fake backend 首请求为 oracle，
接通一个真实首次轮生产入口。第一验收必须覆盖冷/热候选工具 probe、显式 model、启动前取消、提交前后崩溃、
同创建重放、根/子/孙身份和实际请求体与共享投影逐字等价；不把内存 call ledger 的缺记录当可重新选择证明。
当前仅设计，未扩改上述生命周期文件，未标记 Goal 12/13 完成。

## 后续实施前设计：transcript Compact 的当前展示面传递

状态：2026-09-22 只读调查与隔离复现完成，尚未修改生产代码或测试。负责人：transcript-compact 线。
基线 `ef497a904`；本线只追加本节，未触碰其他线已有 diff，不提交。当前认领的
`conversation/compact_provider_surface.py`、`conversation/compact.py` 无法单独闭合同工作片恢复。
主线已要求先说明传递接口；以下均为待实施方案，不代表已修复或第 12 项通过。

### 新核对的接缝与复现

- `loop_support.py::_execute_runtime_loop` 的 `presentation` 是本次函数局部值；选中 Skill 经 `RuntimeToolLoopSeed` 到 `ToolLoopExecuteParams`，工具短名单在原 `ToolRuntimeSnapshot` 的两个 presentation 字段里。现 `RunParams`、`RuntimeLoopResult`、`AgentRunResult` 均不回传这些字段。
- child、Gateway、background 的外层 overflow 循环只能从原工具归档恢复 `loaded_tool_names`。这证明真实的一次性加载，不证明推荐选择；不得从工具名、模型文本或 compact 摘要反推短名单。
- `prepare_conversation_compact_provider_surface` 重新取得工具快照，未消费以上选择。它还只保存 `layout.stable_prefix`；Skill 名卡实际位于 `volatile_sections` 的 `prompt.tool_recommendations`，所以仅给 `ToolSections` 增加 selected 参数仍不完整。
- `_projected_context_tokens` 不接收当前 model surface；原始 preflight、候选大小和 inspect 都走默认 `agent.prompts.build`，没有 child 的真实 system override 或 native schemas。不能把它称为完整下一次请求计数。

隔离复现使用现有 `test_decision_capability_consumer.surface/provider/model_input`、
`test_tool_presentation_projection.prepared`、`conftest.skill_catalog_factory` 及 `_SummaryBackend`；
仅 provider 回答使用原 fake，设置服务、Skill、Registry、PromptBuilder、schema 转换和 Compact surface 均为原实现。
临时 home/目录随脚本结束清理，没有收费模型调用、真实密钥输出、生产工具执行或新增测试文件。
一次 fake Jev apply 后直接比较当前 `_render_tool_loop_prompt` 与重新准备的 Compact surface：

| 原实现观测项 | 当前已选择展示 | transcript Compact 重建 |
| --- | --- | --- |
| `presentation_optional_b` schema | 收起 | 重新出现 |
| 完整索引中的 `method-059` | 不在稳定前缀 | 重新出现在稳定前缀 |
| 稳定前缀字符数 | 3,023 | 8,035 |
| 原工具权限 `snapshot_hash` | 与推荐前相同 | 未将展示差异解释为新增授权 |

该轮只有一次决策调用，选中 Skill 为 `workspace:method-001`，稳定前缀比较为不相等。
另在同一临时宿主上，原 `_projected_context_tokens` 返回 5,749；实际 `task_local` surface 使用短系统时，
按原 `estimate_tokens` 对 system/prompt/tools 计数为 10,220；换成 12,000 次重复的测试系统段后为 82,217。
这些是本地启发式诊断数值，且默认 scope 与 task-local 请求面并不相同；只证明当前接口无法接收应计的 surface，
不是供应商 tokenizer、真实缓存命中、精确漏算比例或 250K/1M 可用性证明。

参考核对：先读 `codex_contract_code_files.xlsx` 与 `pi_contract_code_files.xlsx` 的匹配项，前者无精确命中，
后者定位 `packages/coding-agent/src/core/compaction/compaction.ts`；再直接读 Codex `core/src/compact.rs`。
Codex 的 `InitialContextInjection` 区分 mid-turn 与 manual/pre-turn，`build_compaction_initial_context` 返回同一
`WorldState` 与渲染结果；可借鉴这种显式同轮上下文传递。Pi 的 `generateSummary` 使用独立摘要 system，
只作为取消/请求职责对照，不能据此宣称本项目当前工具 schema 或缓存前缀已一致。

### 最小安全接口与后续 ownership

优先采用原宿主 callback 模式传回一个只含不可变展示数据的值，避免把活快照写进公共结果序列化链。
建议值仅携带 `selected_skill_ids`、`required_skill_ids`、`presentation_deferred_names`、
`presentation_shortlist_names` 和当前有效性核对所需的原 scope/Skill/工具/配置版本事实；不包含 handler、Registry、
执行权或加载正文。`None` 与空选择不可合并；取消、权限变化、失效配置及新工作片不得复用旧建议。

| 待认领文件及准确接缝 | 建议改动与边界 |
| --- | --- |
| `agent_core/runtime/loop_models.py::RunParams/RuntimeLoopParams` | 增加默认 None 的本片展示输入和宿主回传 callback；仅显式内部调用携带，不从 task attributes、历史或持久配置反序列化。 |
| `agent_core/runtime/loop_support.py::_runtime_loop_params/_execute_runtime_loop` | 沿原参数传递；在唯一推荐接缝复核并采用有效的同片投影，回传真实采用结果。冻结当前 prompt/schema 的只读请求面时复用真实准备与渲染，不能另发 Jev。 |
| `capability/decision_recommendation.py` | 复用原 Skill/Registry 范围和现有推荐有效性核对；只 replace 展示字段，当前授权、runtimes、协议、发现入口和实际 loaded 工具继续由原机制管理。需新增纯值类型或 helper 时放原模块，不造第二选择状态仓。 |
| `RuntimeLoopResult/AgentRunResult` 与 finalize 链 | 推荐 callback 方案不改这些类型；若改走返回值，必须显式贯穿 `FinalizeParams`、`FinalizeContext`、`runtime_mixin._build_finalize_context`、`_finalization_service`，不得借 `tool_runtime_evidence` 偷渡运行控制或持久化 handler。 |
| `agent_core/subagent/run_flow.py::_run_subagent_conversation_turn/_subagent_model_run_params/_pending_subagent_compact_model_surface` | 只在当前 `AgentThreadTurnInput.turn_id` 的 outer retry 局部值中携带；进入下一个 Goal turn 即重置。`attempt_id` 跨 Goal turn 复用，不能单靠它判同片。 |
| `gateway_parts/request_execution.py::_run_gateway_turn_with_conversation_compact/_gateway_compact_overflowing_turn`；`request_context.py::GatewayConversationLoadRequest/_load_gateway_compact_context` | 当前宿主请求的 Compact 与随后重建 RunParams 消费同一纯值；新请求/进程重启没有该内存值时维持 None，不从旧日志恢复推荐。 |
| `conversation/background_execution.py::run_background_turn_with_compact/_compact_background_main_thread` | 同一次后台工作片的内层 overflow retry 保留，下一次后台 slice 重新判定。工具归档、输入释放、失败和 Compact CAS 顺序不变。 |
| `conversation/compact_provider_surface.py` | 接当前已冻结展示与请求面；保留所需动态 section 的 typed source，复用原 native history 投影，而不是把名卡移进稳定 system 或从正文解析 section。手动 Compact 和新片保持 None。 |
| `conversation/compact.py::_prepare_compact_request/_compact_pending/_build_compact_candidate/_projected_context_tokens` | 检测、摘要与候选评估共享同一已准备请求面；历史候选变化仅替换原 summary/tail，原 schemas/system/当前输入沿公共纯投影计数。 |

所有 callback 都由当前宿主闭包持有；复用条件由原 owner/thread/request/run/attempt 加宿主真实 turn 身份核对，
缺失事实时不猜身份。展示名称与当前原授权集合相交仍不等于执行授权；发现入口、原已加载 schema 和显式 allowed 限制不得改写。
当前 `ToolRuntimeSnapshot.snapshot_hash` 包含 run/schema/exposure，但不含活 handler 或插件激活代次，
因此它不能单独证明旧插件绑定仍有效；复核应沿原当前 snapshot/availability，不把同名工具当成旧实例。

完整预算还依赖上节提出的公共纯 renderer：当前 Compact `prepare_for_run` 可能同步工具，协议选择也可能 probe。
不能为改计数把它直接移到每次 inspect/preflight 前，造成读状态时启动插件或联网。
应由原执行准备提供已经核对的 model surface，再复用公共计数；尚未准备出的动态注入或未知协议须明确保留证据边界，
真实发送前完整 preflight 仍是权威，不以几项 token 相加或重建默认 prompt 冒充完整请求。

目前创建前投影线仅认领 `subagents/services/base.py`、`runner_context_service.py`；本线没有修改这些文件。
下一切片由主线指定单一 owner 接 core 展示 carrier/纯 renderer，再由 Compact 线接上述三个宿主循环。
涉及 `loop_support.py`、`loop_models.py` 或 `_render_tool_loop_prompt` 前必须再次协调，不能同时各建一份 renderer。

### 验收与交接

本轮执行下列原有测试和静态检查：

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_subagent_runtime_compact.py agent_py_agent/tests/test_decision_skill_projection.py agent_py_agent/tests/test_decision_capability_consumer.py -q --tb=short
ruff check agent_py_agent/agent/conversation/compact_provider_surface.py agent_py_agent/agent/conversation/compact.py agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_subagent_runtime_compact.py agent_py_agent/tests/test_decision_skill_projection.py agent_py_agent/tests/test_decision_capability_consumer.py
```

结果：82 项通过、Ruff 通过，认领生产/测试文件无 diff；本节只做文档 UTF-8/空白检查。
原测试通过不覆盖以上复现出的交叉缺口。后续新增 focused 验收须真实贯通主/child/grandchild 的选中输入、
transcript 摘要与重建首请求，核对 native schemas、稳定前缀及原动态名卡位置；同时覆盖默认关闭字节等价、
新工作片 None、搜索加载保留、权限收缩/插件撤销失效、不额外决策/探测及摘要失败不推进 checkpoint。
共享设计台账、入口、Goal/TODO 与测试导航由主线统一更新，本线没有将只读设计写成已落地能力。

建议下一步：先确定 core 展示回传与公共纯 renderer 的唯一 owner，再并行接 Compact 宿主循环和创建前容量消费。
守住授权、身份、原工具绑定、未知容量、取消与 Compact CAS；本片保留源码缺口，不进行真实模型验收或提交远端。

## 后续实施交接：本片展示纯值与原运行接缝

状态：2026-09-22 第一有界切片本地实现，未提交；仅完成 core carrier，不代表 transcript Compact 或第 12 项完成。
本线仍在 `ef497a904` 的原独立 worktree；未改其他线的生产文件，新增文件为零。

### 实际完成与文件

- `capability/decision_recommendation.py`：新增 frozen `CapabilityPresentationSelection`，只含原 `DecisionBinding`、安全连接摘要、展示版本、attempt 和不可变 Skill/tool 展示名称。构造拒绝可变集合及活对象，不包含原模型响应、handler、Registry 或原请求 deadline。
- 同模块的 `recommend_capabilities` 保留原新建议调用与最终采用门；冻结纯值后仍检查原绝对期限。提供合法已采用值时，`_restore_presentation` 只读核对原身份、配置/连接、当前 Skill/工具范围、必要引用、输入摘要、模型窗口和协议事实，不再次调用 Jev。
- 已采用的展示不是等待中的决策响应，所以不套原请求已耗尽的 deadline。复用沿原 `decision_service._stale` 的身份/配置/连接核对，不伪造响应、不延长原模型调用期限。更改原 service 的 stale 合同时要同步检查此调用方。
- 复用只在调用方传入的当前原 `ToolRuntimeSnapshot` 上替换两个 presentation 字段；runtimes、权限、snapshot hash 和 loaded 工具来源不变，活 handler 始终来自该当前快照。失效或无效载体返回原快照及 `selection=None`，同次调用不隐式重发决策。
- `agent_core/runtime/loop_models.py`：`RunParams`、`RuntimeLoopParams` 新增 `capability_presentation` 与 `capability_presentation_callback`，默认均为 None；新工作片不隐式继承。
- `agent_core/runtime/loop_support.py`：`_runtime_loop_params` 转交上述两字段；`_execute_runtime_loop` 在唯一推荐接缝回调真实采用的纯值或 None，再使用原 seed/renderer。回调失败在 `ToolLoopService.execute` 之前传播，不静默假定宿主保存成功。
- `RuntimeLoopResult`、`AgentRunResult`、finalization、所有工具/历史序列化结构均未新增字段；活快照与 callback 不进入结果。载体只用于宿主内存接缝，没有新增状态文件、Registry、配置项或授权机制。
- `tests/test_decision_capability_consumer.py`：新增纯值不可变、None/空选择、无额外决策、失效/取消、实际 renderer 字节一致、原加载 schema 保留、callback 与结果边界、首次期限等定向验证。
- 本文：仅追加本节；入口、台账、Goal/TODO、测试导航由主线集成时统一维护。

`presentation_revision` 只绑定对展示有意义的结构化范围、能力、必要引用及请求输入摘要，
不把 `conversation_compact_generation` 或历史注入当作新授权，避免单纯推进 Compact 代际就无条件丢弃合法展示。
自然语言只作为原建议输入的字节摘要参与失效核对，不解析其中词语决定路由、权限或控制。

### 验证

```bash
python3 -m pytest agent_py_agent/tests/test_decision_capability_consumer.py agent_py_agent/tests/test_decision_skill_projection.py agent_py_agent/tests/test_tool_presentation_projection.py -q --tb=short
python3 -m pytest agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_subagent_runtime_compact.py -q --tb=short
ruff check agent_py_agent/agent/agent_core/runtime/loop_models.py agent_py_agent/agent/agent_core/runtime/loop_support.py agent_py_agent/agent/capability/decision_recommendation.py agent_py_agent/tests/test_decision_capability_consumer.py
```

结果：48 + 10 + 24 + 38 + 12 = **132 项通过**；Ruff 与本线 diff 空白检查通过。
复用原 code-size `_check_ast`、baseline、`compute_strict_blockers` 只读检查三个生产文件，新增 strict blocker 为 0，
没有改写共享 `CODE_SIZE_REPORT.md`。本文另验 UTF-8、末尾换行、行尾空白；没有收费模型调用、提交或远端验收。

重点覆盖：一次 fake Jev 后重复经过原 `_execute_runtime_loop`，同片复用实际 prompt 字节不变、决策调用仍为一次；
空 Skill 选择仍为 `()` 而不是 None；关闭/观察/策略、连接密钥、Skill、handler 可用性、必要动作、允许工具、
模型/窗口、输入或身份变化都保留原展示，不发额外请求。新回调接到 None 时宿主可清除旧值；用户取消继续传播。
测试还核对原 `runtimes`/`allowed_tools`/hash、不提前执行测试工具、已加载 schema 仍存在，以及两种结果 DTO 无载体字段。

### 未接线边界与后续 ownership

本片没有修改 `run_flow.py`、`request_execution.py`、`request_context.py`、`background_execution.py`、
`compact.py`、`compact_provider_surface.py`、`_tool_loop_service.py`、`base.py` 或 `runner_context_service.py`。
因此目前生产宿主尚未传入 callback/carry；正常新建议可产生纯值，但实际 transcript Compact 的缓存面缺口仍保留。

下一位实现者在每个宿主当前真实 turn 的 outer retry 内保存回调值，并将同一值交给下一次 `RunParams` 与 Compact surface；
回调 None 必须清除保存值。下一 Goal turn、后台新 slice、新请求或进程恢复都回到默认 None，不能从旧结果或日志反序列化。
载体绑定原 owner/thread/run/task/operation 与 attempt；child 的 attempt 会跨 Goal turn 复用，
所以真实 `AgentThreadTurnInput.turn_id` 的局部生命周期仍必须由宿主接线明确守住，不能只比较 attempt。

恢复时当前工具快照仍是唯一执行权威；纯载体没有活 handler，也不提供旧插件实例身份。
不得为了匹配 carrier 去改原快照哈希、重挂 handler、扩大 allowed_tools 或重新读取被撤销的 Skill。
若宿主重新准备了不同的有效快照/能力/必要引用，现有只读核对会放弃旧值；后续新工作片可按原策略重新判断。

Compact 线还需保留原 `prompt.tool_recommendations` 的动态 Skill 投影，而不是仅转发稳定前缀，
并接公共纯 renderer 让摘要/候选容量与当前真实 model surface 一致；本片没有解决这些问题，也没有新增近似计数。
创建前投影线仍独占原 `base.py`/`runner_context_service.py` 接缝；跨到 `loop_models.py`/`loop_support.py` 前由主线再次协调。

建议下一步：主线先复核本片 core carrier，再由 Compact owner 接三个宿主的真实 turn 生命周期；
可与创建准备纯投影继续并行，但 renderer 与 core 参数链只能由一位 owner 编辑。守住未知容量、权限、取消和原 checkpoint/CAS，
完整组合测试通过后再做少量真实验收；当前不关闭第 12 项，不推送远端。

### 本片评审补充：生成连接变化使旧展示失效

主线评审发现：仅绑定生成模型名、窗口和协议能力不足以区分同名模型的端点/请求头变化。
已在本线 `decision_recommendation.py` 复用原 `decision_policy.connection_revision` 的进程随机盐 HMAC，
只读当前 backend/config 的端点、认证引用、密钥、自定义头和模型配置引用；carrier 中仍只有最终版本哈希，
不保存连接原文、不读取凭据库、不探测 provider、不写新增状态。展示版本在决策前冻结，返回后再次比较，
所以请求期间同名连接变化也不能被当成原建议采用；首次建议仍受原绝对期限控制。
新增测试覆盖运行时/配置端点、原地修改自定义头、密钥、认证引用、profile 引用变化，并检查 carrier 无明文秘密。
本次最新定向结果：`test_decision_capability_consumer.py` **58 项通过**，定向 Ruff/diff 通过，
原 strict AST/baseline 只读检查该生产文件新增 blocker 为 0。没有进入宿主 Compact 接线或提交远端。

## Goal12 实施交接：完整冻结请求的共享纯投影

状态：2026-09-22 本地切片完成，未提交、未发真实模型请求。解决创建前容量检查若另拼简化任务文本，
会漏掉完整 system、动态文件、Skill 名卡、原生 schema、历史、图像及推理块的问题。
本片只统一请求材料与格式，不把 `ready` 当作窗口足够或模型能力已探测，也未完成创建前容量消费接线。

### 原入口与副作用边界

- `prompting_parts/builder.py` 的原 `PromptBuilder.build` 保持输入参数和采集顺序，改为
  `prepare_render_input(PromptBuildRequest)` → `render_prepared_prompt(PromptRenderInput)`。
  准备阶段仍按原 scope 读取动态文件、persona、Skill 元数据和工作区时间；纯 renderer 只消费冻结字符串。
  文本与 native 分段共用原布局，没有第二套 prompt formatter。
- `_tool_loop_service.py::_render_tool_loop_prompt` 仍按 Goal injection → workspace → execution facts →
  builder 的顺序处理宿主事实，再经公共 `tool_loop_prompt_request` 组装原请求。
  Goal 修订号和运行账等副作用仍只在真实准备阶段发生；容量投影不能调用此函数当作纯入口。
- `runner/prompts.py` 的真实 `_build_subagent_runner_prompt` 共用
  `prepare_subagent_runner_prompt` → `render_subagent_runner_prompt`。
  前者可能读取 refs 的存在性和原角色目录，后者只消费 `SubagentRunnerPromptInput`；原系统提示继续用
  `subagent_runner_system_prompt`。Audit 来源分支沿用原格式，不另建启动路径。
- 新 `agent_core/tool_request_projection.py` 的 `project_tool_loop_request` 不接收 agent 或任何回调；
  不读盘、不刷新直属孩子、不消费 mailbox、不调用 `prepare_for_run`、不探测、不发网、不写 IR/seen。
  所有原生追加都发生在输入副本，最终转换与孤儿清扫调用 `tool_ir_history.project_native_provider_messages`。
- 真实 `tool_model_generation._native_provider_messages` 保持原运行引导的提交时机和去重集合，
  只把转换/孤儿清扫交给同一纯函数。孤儿补齐、原历史顺序、图片与 thinking/signature 的协议形状不变。
  `project_native_prompt_history` 新增显式 `conversation_state` 可选输入；未传时原调用方继续读取原内存状态。

参考核对：本地 Codex `codex-rs/core/src/client_common.rs` 的 `Prompt` 将 instructions、tools 和
typed input 放在同一请求值中，`get_formatted_input_for_request` 在副本上投影。本片复用此职责边界，
没有复制它的 provider 协议或建立另一份 token 计数器。

### 调用合同：缺失与明确为空必须区分

`tool_loop_prompt_request(params, *, runtime_injections, workspace_context, execution_facts)` 是无宿主读取的
原 builder 参数组装入口。调用方必须先取得完整的宿主三项事实；对容量路径不得以 `None` 工作区触发
builder 的时钟补采集，再声称投影纯净。原 `PromptBuilder.prepare_render_input` 属于准备层，不能放入纯投影。

`ToolLoopRequestInput` 的要求如下，`None` 表示未知；空字符串、空 tuple、空 frozenset 表示明确为空：

| 冻结字段 | 唯一来源与约束 |
| --- | --- |
| `prompt_input` | 原 builder 准备出的 `PromptRenderInput`，含完整 system、memory、owner、动态文件、工作区、注入、目录、名卡、执行事实和用户任务 |
| `system_instruction` | 原 `provider_system_instruction(backend)` 的实际独立系统通道正文，不能拿它代替完整 builder system |
| `tool_protocol_snapshot` | 原固定协议快照；不在这里构造或探测供应商能力，必须与 prompt 的 native 标志一致 |
| `native_tools` | 原 `resolve_native_tools` 使用当前原快照、allowed 与 loaded 状态产生的完整 schema；未知不能写成空目录 |
| `tool_choice` | 原 `_model_turn_tool_choice` 的宿主值；投影只复用 `tools_for_choice`，不重新裁决动作策略 |
| `tool_ir_history` | 原真实初始/续跑 IR；创建前必须复用原首轮构造和 carried handoff 口径，不能随手填空历史 |
| `provider_history_messages` | 原完成回合的 provider prefix，包含原消息模态；不能只取自然语言摘要 |
| `tool_context` | 原本轮上下文，非 IR 的运行引导由原过滤器处理 |
| `forwarded_guidance` | 原已转发集合的冻结副本；投影不消费去重事实 |
| `conversation_state` | 原 `conversation_runtime_state_section` 的冻结结果，保留当前 Compact 代次与权威来源 |

后七项只在 native 路径必需。原 IR/schema/message 容器在输入构造时复制，结果再次与输入脱离；
不把活 handler、Registry、任务管理器或回调放进请求值。冻结值只是本次材料，不能作为新授权或持久状态。

结果 `ToolLoopRequestProjection` 有 `status=ready|unknown`、`missing_fields` 和
`prompt / provider_prompt / system_instruction / messages / tools / tool_choice`。
`prompt` 保留完整归档布局，`provider_prompt` 去掉已进入 IR 的动态重复尾部；实际后端仍按原缓存适配器处理。
`tools` 已按原 choice 的目录规则投影，`none` 不带 schema；`specific` 原样验证指定工具属于当前目录。
缺字段或协议冲突先返回 typed unknown，renderer/adapter 不会被调用；原目录错误仍抛原 ValueError，
不改成能力已知或成功。纯入口不判断输入容量、输出/推理预留、图像 token 或计费。

### 验收与剩余集成

新增 `test_tool_request_projection.py` 共 **27 项**，包括：

- 真实 Anthropic backend 的 payload 捕获：首轮、完整续轮、孤儿工具历史分别配合 auto/none/specific，
  与正式 prompt materialize / `_native_provider_messages` 路径逐字等价；覆盖图片、thinking 签名、完整工具参数与结果、
  原动态文件、完整 runner system 和 Skill 动态名卡。HTTP 发送被替换为内存捕获，未调用网络。
- 10 个缺失字段分别返回 unknown，并把 renderer、adapter、读文件入口设为禁止；显式空集合仍是已知空。
- 冻结后改原文件、配置、IR、引导不改变投影；禁止读盘、刷新、消费和生成；修改结果不会反写输入。
- 原准备顺序保持，原文本/native 布局一致；普通和两种 Audit runner 提示纯渲染不再读上下文或 refs。

原 PromptBuilder、runner、native IR/protocol、model generation、能力消费、Skill 投影定向回归均通过。
另从当前 HEAD 载入旧 builder/runner 源码，与新路径对照 19 组 native/text、四种 scope、系统覆盖及 runner 分支，
正文和 cache layout 逐字相同。定向 Ruff、diff 空白检查通过；六个认领生产文件按原 AST/baseline 检查新增 strict blocker 为 0，
未改写共享 code-size 报告。`CODEBASE_TREE.md` 已登记纯请求模块及本审计开发入口；共享 Goal/设计/测试导航由主线收口。

主线并行已核实事实：`context_pressure.py` 的连接校准指纹复用原 `decision_policy.connection_revision` 的
进程随机盐 HMAC，覆盖同端点密钥轮换，不持久化凭据明文；主线该片 focused 24 项通过。
上文旧审计的连接指纹缺口保留为历史发现，不代表此处已完成的主线修复仍未落地。

建议下一步：主线将 prepared child 的原执行上下文和原首轮事实接到此输入，再复用原预算入口判断候选窗口。
完整冻结事实尚不可得时保留继承并记录 `missing_fields`，不能以简化 prompt 宣称候选可用。
可与 transcript Compact 宿主继续并行，但共享 renderer 由本片唯一维护；守住原授权、身份、IR 配对、取消、
未探测能力与未知模态预算。全链组合通过后再做少量真实验收，本片不提交、不部署，不关闭整个 Goal12。

## 第二有界切片交接：Gateway 同 turn 的 transcript Compact 展示

状态：2026-09-22 本地实现与 focused 验证完成，未提交；负责人仍为 transcript-compact 线。
目标是同一真实 Gateway turn 在供应商溢出、transcript Compact 和重建 RunParams 后保留已采用的展示，
不把推荐变成授权、不重复付费决策，也不把完整容量缺口写成已闭合。仍以 `ef497a904` 为基线。
开工复核原 Gateway 请求/RuntimeDB 绑定、原 Compact 合同与上一节参考；Codex 的
`InitialContextInjection` / `build_compaction_initial_context` 继续作为显式同轮投影的边界对照。

### 实际完成与准确文件

- `gateway_parts/request_execution.py::_run_gateway_turn_with_conversation_compact`：选择、已评估事实及 callback 只在当前调用局部存在；新请求从 `None/False` 开始。callback 同步保存真实采用值或清为 None，同时更新当前宿主 RunParams，避免后面的第二次会话加载恢复旧值。
- 同文件 `_gateway_compact_load_request`：复用原 `gateway_runtime_authority` 及 `run_params_with_request_id` 得到当前请求事实，创建已有 frozen `RuntimeContextRequest` 的只读副本。复制当前 prompt、task attributes、allowed 范围、prompt 文件和 system override；不从 carrier.binding 推导 run/task，不持有旧 handler。
- `gateway_parts/request_context.py::GatewayConversationLoadRequest/_load_gateway_compact_context`：可选模型面只沿原调用参数到 Compact；原 preflight 仍构造原默认面，不加到会话结果或持久记录。
- `conversation/compact_provider_surface.py`：用当前 Registry/protocol 重建原快照；非 None carrier 只走原推荐入口的只读复核。当前身份/配置/权限不符回基础面，原内存 callback 清宿主值；没有 carrier 时绝不请求 Jev。原 `prompt.tool_recommendations` 动态段经 `RuntimeFactsTurn` 和原 adapter 追加到摘要历史，不移入稳定 system，也不读 Skill 正文。
- `conversation/compact.py::_summarize`：仅把已准备的 typed 动态段交给原 provider messages 投影，摘要指令仍在最后；原物理有界摘要调用、checkpoint/CAS 和存储结构不变。
- `agent_core/runtime/loop_models.py`、`loop_support.py::_runtime_loop_params`、`capability/decision_recommendation.py`：经主线明确扩权后增加并原样传递两个仅内存字段。`capability_presentation_evaluated=False` 为新片默认；若已评估且没有选择，消费者直接保留基础面，连新 decision stage 都不建立。取消仍传播。
- `capability_presentation_turn_id=""` 为旧路径默认；Gateway 从原 `execution_attempt_id`（缺省 request_id）提供。本片审查证明每次真实 `agent.run` 都会轮换 RuntimeDB attempt，因此 carrier 的原 `attempt_id` 比较位置优先使用这个明确宿主 turn，否则仍使用原 `params.attempt_id`。request/run/task/owner/thread/policy 绑定继续核验，数据库生命周期没有改动。
- 测试：新增 `tests/test_gateway_capability_compact.py`，并扩展 `tests/test_decision_capability_consumer.py`；新增测试文件由本线独占，文件树和共享导航由主线统一登记。

### 验证及证据边界

执行记录：

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_capability_compact.py agent_py_agent/tests/test_decision_capability_consumer.py agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_subagent_runtime_compact.py -q --tb=short
python3 -m pytest agent_py_agent/tests/test_gateway_capability_compact.py agent_py_agent/tests/test_decision_skill_projection.py agent_py_agent/tests/test_tool_presentation_projection.py -q --tb=short -o addopts=
python3 -m pytest agent_py_agent/tests/test_gateway_chat_conversation_context.py -k 'compact or overflow' -q --tb=short -o addopts=
```

第一组当时为 122 项；随后新增 4 项 Gateway 用例，第二组最新为 50 项，第三组 6 项通过。
按不重复用例计，本片最新覆盖 **166 项**：Gateway 展示 16、consumer 60、原 Gateway Compact 38、child 12、
Skill 10、tool 展示 24、原 Gateway overflow 6。定向 Ruff 与 diff 检查通过；七个生产文件用原
`_check_ast` / strict baseline 只读检查均为零新增 blocker，没有改写共享 code-size 报告。

真实组合使用原 `SimpleAgent.run`、RuntimeDB、Gateway 请求绑定、原设置服务、Skill/Registry、renderer、
transcript Compact 和原历史提交；fake 仅在决策 provider 回答与最终工具循环响应边界，未访问真实模型服务。
原数据库确有两个不同 attempt、相同 run；普通请求、摘要及恢复请求的 stable prefix/schema 一致，
动态名卡内容仍一致。一个 Gateway turn 只有一次 Jev；空选择保持 `()`，不是 None。
新请求明确从 `None/False` 开始并可再决策；第一次关闭、观察或失败后同 turn 都保持基础面。
Compact 检出生成端点变化时清旧值，摘要返回时端点恢复也不复活选择、不再额外发请求。
另覆盖错 run/task/thread/turn、缺当前输入、权限收缩、原已加载 schema 保留及 carrier/callback 不进入结果/请求文件。
原多次压力与 active-turn Compact 六项通过；这不代表 active-turn archive 的独立摘要器新增了此模型面。

本线运行 `check_doc_sync.py` 仍提示 Gateway 需同步 `docs/modules/gateway/02-progress.md` 与
`docs/modules/gateway/04-structure.md`，已交主线统一更新；本线没有越界改共享入口、台账或导航。
因此当前只声明定向验证通过，不声明本地完整严格 gate 或远端 CI 通过。

### 未闭合部分、交接和 ownership

`_projected_context_tokens` 尚未接公共完整请求纯 renderer；preflight、inspect 与候选下一轮容量仍不是完整
system/prompt/native schema/current input 的统一投影。实际摘要发送已含当前动态面并继续走原有界调用链，
但不能因此宣布第 12 项整体完成。原 surface 准备可能同步 Registry 或探测协议，不得直接挪到只读 inspect。
child / background 宿主未接本片 carrier；本线没有修改它们、`_tool_loop_service.py` 或子代理创建文件。

本片交付后释放 `loop_models.py`、`loop_support.py`、`decision_recommendation.py` 及所认领 Gateway/Compact
生产与 focused test 文件的编辑 ownership，供主线评审和后续召回接线；保留本交接的历史证据。

建议下一步：主线先复核宿主 turn 与 DB attempt 的边界及 Gateway 共享文档，再由单一 owner 接完整请求纯 renderer。
child/background 接线可在各自文件内并行，但若碰 core 参数/推荐入口须重新协调；守住 None/空展示、失效不复活、
当前权限、取消和原 checkpoint/CAS。当前没有提交、部署或真实模型验收。

## P2/P4 首次发送与真实输入前置切片（历史记录，已由下节扩展）

本片在同一集成 worktree 完成发送资格、实际请求材料和临时依赖绑定；用户不需要为任何 child 选模型、确认采用或补资料。
当前缺验证材料时由宿主自动保留继承模型，不能把本片记为自动异模派工验收完成。

- `conversation/agent_thread_store.py` 是 `host_subagent_first_request.v1` 的唯一初始化权威。只有真正新建且收到
  typed `PendingSubagentModelAdvice` 的线程同次保存 `unsubmitted`、创建 operation 和精确 child run/thread；
  普通关闭路径没有新 marker，已有 thread/旧 pending 不补发。新 child 同次使用原通用选择字段
  `revision=1/source=inherited/last_explicit_revision=0`；既有非空模型不重标来源。
- `subagent/run_flow.py` 在原 `attempt_executor` 登记后进入 `subagent/model_selection.py` 的临时首请求范围。
  身份来自 canonical task 与原激活 attempt；`RUNNING`、当前 attempt、取消/废弃/替代由原 lifecycle/control 核对。
  `task_attributes` 中的线程编号、自然语言和空模型账本都不能产生首次资格。
- `_tool_loop_service.py` 保留原 Goal、workspace、执行事实读取顺序，只在这条真实范围里分离原
  `PromptBuilder.prepare_render_input` 和 `render_prepared_prompt`，采集一次实际动态文件/Skill/工具展示。
  `tool_model_generation.py` 在原 IR 提交前读取 child 自己的 canonical history seed、真实 IR、provider history、
  ToolChoice、schema、guidance 去重和 system，产出原 `ToolLoopRequestInput`。纯投影不重新读取宿主。
- `tool_model_generation._invoke_backend_generate` 在 provider I/O 前调用同一线程的原子发送栅栏。
  它先核对精确执行轮，再把首次发送意图及当前自动保留结果写入原 `threads.update_atomic`；写入失败零 I/O。
  标记表示请求可能已经发出，不能作为已送达证明。原 guidance 提交仍有自己的权威事务，二者不能伪称跨文件单一事务。
- `settings/model_scope.py` 的 `prepare_model_dependencies` / `model_dependencies_scope` 与原
  `selected_model_scope` 共用后端缓存、权限和 prompt/tools 视图；显式候选只绑定 ContextVar，不热改共享 Agent。
  `model_dependency_lifetime` / `activate_model_dependencies` 将已采用依赖保持到原 `_run_once_with_params`
  的 finalization；复制到 HTTP I/O 线程的上下文只读，不能在另一线程登记需由父线程清理的 token。
- Anthropic `_request_payload`、OpenAI Chat `_request_payload` 是实际发送与 `project_generate_payload` 的
  同一组包函数；后者不取认证、不探针、不落私有 dump，不发网。SSE 字段与实际 gateway 出站一致。
  Responses 和重写发送实现返回未知，不借继承声称支持 Chat 容量。

核对范围：原 `CONTRACTS_MAP.md`、`WORKSTREAMS.md`、runner/lifecycle/lease、原工具 protocol probe、
`ToolLoopRequestInput`、backend/native-IR 测试；本地参考为 pi 的 `anthropic.ts` 与
`openai-completions.ts` 的 `buildParams`→真实发送段，只借鉴组包与 I/O 分离，未复制 provider 策略。

失败与恢复边界：

| 位置 | 持久结果 | 自动行为 |
| --- | --- | --- |
| 创建前建议超时/错误/need_data | 无新建议与资格 | 原创建、原模型继续 |
| 新 thread 后、首请求准备前崩溃 | pending + unsubmitted，非采用证明 | 重启重读所有来源；资料未知则保留 |
| 旧 pending、没有新标记 | 不补首次资格 | 首次遇到原业务边界时 terminal retained |
| 当前 attempt 缺失、被替换或取消 | 不提交发送标记 | 中断传播，零业务 I/O |
| 原子发送写入失败 | 原状态保留 | 零业务 I/O，不模拟成功 |
| 栅栏成功后、本地异常或不确定网络结果 | submitted + retained 保留 | 不恢复 pending，不重新获得选择资格 |
| 显式同值或不同值选择 | 原通用 CAS 递增版本并终结 pending | 未来自动采用必须尊重该覆盖；无需用户新增操作 |

已验证：两种适配器的 24 种文本/native/ToolChoice/SSE 组合，以实际 gateway payload 为 oracle；
原 pending/create/存储/模型生成定向组通过；真实 `SimpleAgent.run_subagent` 的首轮输入捕获走原 prompt、
原 probe 与原 provider 组包，未用父快照或空占位历史。真实模型网络验收尚未执行。

下一步必须串行合并原模型目录 durable generation（私有、共享发布与 OAuth 变化），将它随 pending 保存并在
首次采用前锁内比较。仅有 decision settings revision 无法发现 create→首启间修改 endpoint/key 的事实。
随后整体替换原 ToolLoopService 的候选 protocol snapshot/params，完成同片依赖绑定与 thread CAS；
fake backend 必须继续证明首轮及后续工具轮均使用同一候选，然后才能做隔离 M2.7/M3/DeepSeek 真实验收。

## P2/P4 自动采用续片（本地 fake provider 已验，真实服务待验）

本片已从中间 pending 合同接到原首轮业务生成：主代理创建时的一次批量 Jev 建议无需用户操作，
真实 child 自动验证后采用或保留。未知、need_data、超时与普通候选验证错误均自动保留；取消传播。
日常 checkout、默认模型和远端未改动，真实模型网络验收由主线统一执行。

### 同一权威和提交顺序

1. `decision_subagent._candidates` 在启用准备时先调用原 `model_profile_generation(initialize=True)`，再解析
   原模型配置。generation 只存原引用及随机代次，公开 Jev 材料不含该信封或连接摘要。
   `PendingSubagentModelAdvice.source_model_generation` 仅在宿主 typed 初始化时保存；旧 pending 缺失不补 ready。
2. `subagent_first_request_scope` 在原 `attempt_executor` 登记后，读取 canonical run/thread 并在 creation guard
   下复查 RUNNING、精确 attempt 与原取消权威，原子把新建标记从 unsubmitted 领取为 preparing。
   同 attempt 重入和另一个进程都不能重复领取；准备中崩溃不会自动重跑建议。
3. 原 `_render_tool_loop_prompt` 固定当前 Goal、workspace、执行事实与 `PromptBuildRequest`；原 `PromptBuilder`
   冻结真正的 child 输入。`ToolLoopRequestInput` 来自 child 自己的 canonical seed、IR/history、schema、
   ToolChoice、guidance 和 provider system，没有父快照、手填空历史或第二 renderer/tokenizer。
4. `select_first_request_model` 在原模型请求前、IR 提交前核验一次。候选依赖复用原 `model_scope`；
   原 native probe 在锁外通过原 `call_with_deadline` 与 HTTP request budget 有界执行，迟到 worker 无提交权。
   原 Anthropic/Chat `project_generate_payload` 与真实发送共用组包函数；完整 payload 使用原 `estimate_tokens`，
   加实际出站正输出 cap 必须落在明确共享窗口内，还须通过原 context preflight。
   这是原估算合同的容量核对，并非供应商返回的精确 token 测量；不含价格或费用控制。
5. 最终原 generation guard 持 owner→admin/shared 目录锁，之后父 thread→creation→child thread。
   当前配置/共享授权、来源设置 revision、apply 开关和候选范围、任务权限/原规格、当前 attempt、选择版本和 pending
   同时复核。目录变化、显式同值选择和首资格消费均失效。原 child `update_atomic` 同次提交有效 profile、
   revision+1/source=automatic、advice=adopted、first-request=reserved；不改已持久任务或其创建模型引用。
6. 同一准备依赖绑定至原片 finalization；ToolLoopService 显式接纳完整候选 params，重试、后续工具轮、
   异常历史与最终协议证据不再使用继承模型的旧协议。下一 Goal 片、重启和溢出 Compact 通过
   `canonical_subagent_model_scope` 读取唯一 child profile，不消费旧 pending，不热改共享 Agent。
7. 原 `_invoke_backend_generate` 在真正 provider 边界前写 submitted/attempt/call_id。提交回执不确定或提交后
   依赖绑定失败必须停止该次发送，不能反向发继承模型。RuntimeDB、线程文件、guidance 和 HTTP 仍各守原事务；
   reserved/submitted 都不是网络已送达或业务成功证明。

默认关闭创建不产生 marker、probe 或第二决策请求。旧/未知 generation、未知完整输入、未支持 payload adapter、
不明正输出 cap、原工具 probe 不支持或容量超限全部保留。当前仅 Anthropic 兼容与 OpenAI Chat 的原纯 payload
合同可证明本片采用；Responses/OAuth Responses 或重写发送器不借继承类冒称容量已验证。

### 失败与重启矩阵

| 时点或竞争 | canonical 结果 | 业务行为 |
| --- | --- | --- |
| create 建议失败/need_data/off/显式 model/幂等复用 | 原创建结果，无新采用许可 | 沿原模型 |
| pending 写入后、原首次资格领取前重启 | unsubmitted | 只在真实首次 attempt 领取后重新核对所有事实 |
| preparing 后崩溃或重入 | 旧 preparing，不补新资格 | 自动 retained，不重放建议 |
| 目录端点/key/OAuth/共享发布变化或删除 | generation 失效 | 自动 retained |
| 设置关闭、候选范围或来源 revision 变化 | 建议失效 | 自动 retained |
| 显式同值选择 | revision 前进、pending retained | 自动 CAS 不覆盖当前选择 |
| probe 未证明 native、窗口/cap/完整输入未知 | retained + 客观原因 | 原模型执行 |
| probe 超时且 worker 迟到 | retained，迟到无提交权 | 原模型执行，无用户补步骤 |
| attempt 停止或被替换 | 不采用，不发业务 | 原取消传播 |
| 采用原子写后回执丢失 | 若已写则 adopted + reserved | 零本次业务 I/O；恢复只读有效 profile |
| 发送意图成功后传输失败 | submitted 保留 | 不恢复 pending，不重新选择 |

### 定向证据与交接边界

`test_subagent_first_request_selection.py` 使用真实 `SimpleAgent.run_subagent`、原任务创建/激活、原工具执行器、
原设置/目录和 canonical thread，fake 仅替代最终 provider transport。
M2.7（200K）、M3（1M）与 DeepSeek-V4-Flash（1M）分别捕获首次与后续 read_file 工具轮 payload；
首轮与完整冻结输入经同源适配器投影结构相同，输出 cap 分别验证 50K/65536 与配置收紧关系。
另有原 `CreateSubagentsTool`→一次 Jev→pending→原首轮自动采用整链，以及撤销、显式同值竞争、未知版本、
容量不足、工具失败、准备重入、迟到 probe、写后失联测试。没有把模型的“我已选择”文字当作证据。

本轮编辑：`decision_subagent.py`、`settings/thread_model_selection.py`、`conversation/agent_thread_store.py`、
`subagent/model_selection.py`、`subagent/run_flow.py`、`runtime/loop_support.py`、`runtime_mixin.py`、
`_tool_loop_service.py`、`tool_model_generation.py`、`settings/model_scope.py`、两种 backend 组包、
对应 focused tests 和本文/子代理模块文档。目录 generation 实现由另一线提供；共享导航/Goal/TESTS 由主线维护。
recall helper 改名后仅同步 `test_decision_recall.py` 与 `test_memory_recall_v2.py` 的关键字，50 项通过，未改召回语义。

收尾验证（各组存在重叠，不合计为唯一用例数）：

- 新增首请求选择测试最终 **30 passed**；含 probe 中通过原 durable stop 取消，业务 provider 调用为零。
- loop/runner/decision/projection/timeout 组合 **251 passed、25 xfailed**；xfail 为原已有标记。
- backend/线程选择/目录/Compact/存储组合先为 **306 passed、1 failed**；唯一失败来自并行 Gateway 新增
  `captured` 参数后旧 fake 的签名。该 owner 同步 fixture 后，失败项单独复跑通过，没有将首轮失败隐藏为整组全过。
- 定向 Ruff、`check_doc_sync.py`、`git diff --check` 通过；全仓 strict code-size 为零 blocker，
  最后再按原 `_check_ast` 与同一 strict baseline 只读复核本线 12 个生产文件，仍为零 blocker。
  本轮未提交远端，未运行 clean-package 或全仓 pytest，不宣称完整远端提交 gate 或线上 CI 已通过。

`runtime/loop_support.py` 已释放给主线接 P5-A 补充召回；本线 `_execute_runtime_loop` 的候选 params 传递保留。
其余上述本线文件在本交接完成后释放，后续编辑按 WORKSTREAMS 重新认领；共享文件树登记由主线收口。

建议下一步：先复核同一 profile/params/协议贯穿后续轮与崩溃边界，再由主线在隔离 owner 上执行官方 M2.7/M3、
OpenCode DeepSeek 的真实自动派工，保留 Jev、canonical profile、首请求 payload 元数据与真实工具证据。
可以并行做只读审查和隔离验收；运行文件接续必须重新分配 ownership，不能把 fake 结果当真实服务或整体 Goal 完成。

## 全仓回归补充：普通 task_local 的发送边界

后续全仓回归在 `test_home_runtime_bootstrap.py::test_task_local_run_does_not_inject_main_context_bundle` 发现：
普通 `task_local` 运行虽有自己的 `run_id`，但并非已登记子代理；首次发送栅栏直接 `manager.load(run_id)` 抛
`FileNotFoundError`，使原局部运行无法发模型请求。现只在该运行没有 canonical 子代理记录时跳过子代理专用栅栏；
已登记 child 仍照常读取原 thread、marker、attempt 并在发送前原子提交，不从 task attributes 猜身份。
原失败文件与 `test_subagent_first_request_selection.py`、`test_subagent_effective_runtime_context.py` 联合 **55 passed**，Ruff 通过。
这修复普通 task_local 回归，不把第 12 项完整容量/Compact/缓存验收提前算完成。

## 第 12 项最新切片：原生预检与出站计量共源（本地，整体未完成）

`context_pressure` 过去直接翻译 IR 并把待发送 guidance 单列估算，未执行真实发送前的孤儿工具清扫，也未应用本轮 `ToolChoice`。因此已关闭工具仍可能算入 schema，实际不发送的孤立结果仍可能触发 Compact，预检与引导提交后的口径也不同。

本片让预检与原完整请求投影共用 `project_native_provider_messages` 的转换及清扫；冻结 guidance 只追加到副本，不消费原去重集合。`model_turn_tool_choice` 迁到原 `native_tool_protocol` 唯一实现，生成、主会话/子代理选择和容量核验共用。真实 IR 与 guidance 的提交顺序未改。

`projected_model_context_components` 只消费已准备的 `ToolLoopRequestProjection`，沿原 `estimate_tokens` 和整数占比分摊计量，不读取宿主、注册表、文件或校准，不联网；未知投影拒绝计数，不能补成零。原外层 snapshot 仍负责运行期校准，其 hydration 不是纯操作，不能被 Compact 候选直接调用。计量口径改变后观测升为 `provider_context_observation.v3`，旧 v2 不转换估算比例，等真实模型返回新 usage 后再校准。

本地四文件 **83 passed**，包含 auto/none/specific、完整/孤立工具历史、冻结材料不变、预检与实际引导提交前后总量相同、未知拒绝及旧观测失效；原 native Compact、消息流、子代理首请求、Gateway 采用/观察、上下文预算、工具统一与线程存储八文件 **266 passed**。纯投影仍是本地 token 估算，不是供应商 wire tokenizer 或模态容量上界。

独立审查后将 payload 对照的实际侧接到原 `_do_backend_generate`，补上真实包装的 `thinking_disabled`。由此修正 Gateway 的 `choice.none` 候选投影：不能把筛选后的空 schema 当成“原工具参数不存在”，否则预期 payload 和实际发送不同而拒绝采用。子代理在原始工具参数为空时也不应额外关闭 thinking 或向 OpenAI 多传 `tool_choice:none`。两条宿主准备链分别覆盖 tools 开/关及 none，child 增加 Anthropic/OpenAI 双协议并核对记录的 input estimate 与最终 wire payload 原估算相同；首请求文件34项、计量及主子采用六文件153项联合通过。没有新增 provider 策略或设置开关。

尚未完成：三个宿主的完整准备载体、Compact 候选的原恢复输入重建、输入加输出预留的统一接受条件，以及真实 provider 缓存证据。此次没有把旧 `_projected_context_tokens` 冒充完整投影；第 12 项继续进行中。

建议下一步：以此纯计量入口连接真实恢复准备，候选必须对照恢复后首个实际 payload；子代理/后台同 turn 展示载体可按精确文件独立推进，共享 core 参数与 renderer 保持单一 owner。

### 同 turn 展示接续与后台执行身份（本地续片）

child 和后台宿主沿原 `RunParams` 的展示回调携带本轮选择及 evaluated 事实；明确空选择与已评估无选择分开，下一 Goal turn／后台工作片清零。Compact 仍重新核对原权限、配置和能力快照；失效时回传 `None`，即使配置恢复也不在同轮再次发决策。child 的“transcript 未推进→活动轮归档→再次准备”读取回调后的参数，不能复活第一次准备前冻结的旧选择。没有新增持久展示账或执行权。

独立审查发现后台恢复已有主任务时，实际 `run_id` 可能由 core 从临时任务编号改绑为旧主 run；外层参数不会被 `replace` 自动回写。后台现复用 `LocalRunControl` 及原 `bind_runtime_authority` 发布缝隙，准确接收本次 canonical run/attempt，再交给 Compact。宿主原回调的拒绝、不可调用值、关闭和迟到发布均保留；不从推荐载体倒填身份，也不二次查询猜测 current run。

后台完整 runtime、Gateway 展示 Compact、新后台12项和本地控制句柄四文件211项通过；child 新展示11项、原 Compact12项、原同 attempt Goal 续轮2项共25项通过。计量/主子采用及宿主接续三组共389项通过。这里只证明展示同轮接续与相同请求前缀/schema，仍不等于完整恢复输入计量或供应商缓存命中。

建议下一步：第12项的12.1—12.3本地片已收口，接12.4完整恢复准备与12.5真实输出预留；可并行只读核对下一请求，但共享 core/Compact 由主线串行修改，不以继续添加展示载体替代完整容量验收。

## 12.5 输出预留接受门（本地首片；仍依赖12.4完整计量）

发现原 transcript 候选只看 recovery target/trigger：即使达到健康目标，也可能与真正请求的输出 cap 一起超过共享窗口。普通模型 preflight 已有该规则，本片直接抽出 `model_request_input_ceiling` 复用，不新增百分比、配置或 token 计数器。自动触发与候选接受共用 `min(原 trigger, window - 已知实际输出 cap)`，候选必须严格低于边界后才能按恢复目标或原保留候选提交。Context 显示仍只报输入占用和原 trigger。

已知 cap 的条件仍由原实现决定：内置 HTTP、显式共享窗口和正数输出限额；OAuth Responses 实际移除输出上限、未声明窗口或未知 cap 时不猜预留。未通过的候选沿原 `COMPACT_CANDIDATE_TOO_LARGE`、失败计数和熔断链退出，不能写 checkpoint 或推进历史游标。

Gateway Compact 与 runtime context pressure 两文件72项通过。新增10种受控估算矩阵覆盖：低于trigger但因输出额度触发自动Compact；候选恰好等于剩余输入边界；低于恢复目标仍不可容纳；可容纳但高于恢复目标的原保留候选；输出占满窗口；零/未知cap；非显式窗口；OAuth移除cap；原trigger边界。这里只隔离摘要响应与计量值，接受、checkpoint/CAS和失败记账走生产路径；不是供应商实际容量测量。

完整 `_projected_context_tokens` 仍缺宿主恢复准备，12.4未完成，因此12.5整项暂不勾选。旧实现注释已明确收紧证据边界，不再称其为“完整下一轮输入”。

建议下一步：以真实恢复后的 `ToolLoopExecuteParams` 和原冻结 `PromptRenderInput` 接12.4；`_prepare_runtime_context` 会召回并写上下文包，`_tool_loop_execute_params` 恢复归档时还可能做摘要，不能对每个候选重复调用。完整准备只能在原生命周期执行一次，候选只替换结构化历史/代次/注入，再由相同已准备请求继续发送。具体接缝须在共享core owner下串行实施。

### 12.4 接缝设计（Gateway overflow已落地，其余入口未完成）

先打通 Gateway 已绑定请求的 overflow 恢复轮。它已有 `GatewayModelObservation` 的完整宿主作用域，即使模型采用关闭也存在；扩展原 render/select 时点，在原发送 IR materialize 前保存冻结 prompt 输入并进入 Compact，不放到过晚的 before_send。候选经原宿主历史投影产生预计代次的 `ConversationHistorySeed`，由原宿主 renderer 重建操作证据注入，并重算 `conversation_runtime_state_section`；其余准备沿同一次恢复参数。

原先 `PromptRenderInput.injected` 已拼平；现已改为冻结 `injection_fragments` 元组，`injected` 只按原 join 规则派生，不保留第二正文副本。候选按宿主准备时已知位置替换，禁止搜索自然语言正文。Gateway临时候选投影已交原 Compact options，原 checkpoint/CAS 成功后安装获选 seed、注入和请求输入，继续本次已准备的生成，不再次退出后调用 `agent.run`。取消、CAS失败或输入/模型变化时丢弃候选，缺完整输入或自定义builder不能复用时明确unknown。此设计不改变既有持久历史权威，也不把初次加载、手动Compact或未接宿主的粗估当完整证明。

首条验收同时捕获“候选payload等于恢复后首个实际payload”、准备副作用只执行一次、CAS失败零业务发送；Gateway证据见下节，再接child和后台。注入片段保留重复文字、空片段及内嵌标题，调用方后续修改不影响冻结值；替换后原生稳定前缀保持。其余宿主接线尚未实施，不能计入12.4整项完成。

### Gateway overflow完整恢复请求（本地实现与验证）

解决问题：旧transcript接受门仅估算摘要和历史，可能提交一个实际恢复请求仍放不下的候选；简单重跑准备又会重复记忆召回、推荐、上下文包写入和归档摘要。

- `load_conversation_compact_source`复用原canonical未压缩行及当前未完成后缀排除。`GatewayConversationLoadRequest.defer_compact`是内部只读来源开关，跳过摘要/提交，保留repair、索引、历史作用域及工作区准备；`force_compact=False`继续表示原自动判断，不能当只加载。
- 有来源时在原`GatewayModelObservation.render/select`中冻结恢复请求，复用原`ToolLoopRequestInput`、IR转换、ToolChoice、计量及provider builder；候选不重复调用整轮准备、召回、推荐、Goal或文件读取。摘要缓存面也从此次已准备材料派生，不再另行准备Registry/提示。
- 只替换候选摘要/历史、操作证据、首次checkpoint应有的canonical引用、预计代次及运行事实。注入索引来自Gateway原组装顺序；context bundle插在完整原注入之后，因此不改变会话段位置。用户相同文本不参与定位。
- 每个候选携带自己的临时请求材料；原checkpoint/CAS成功后才安装，并由同次工具循环继续生成。保留候选回退取回该候选的材料，不取最后一次回调结果。后续工具轮仍沿已更新的params；外层最终持久化和再次overflow读取新的conversation。
- 无transcript来源时先走原active-turn archive Compact/CAS，再准备下一次运行；实际工具账仍完整用于去重，已覆盖工具往返不再重放。没有第二Compact账、模型目录或持久恢复计划。
- 停止复用原线程中断与run-owned token检查；成功后在恢复业务发送前发布原typed Compact boundary。CAS冲突、取消、未知投影不安装候选；摘要瞬时错误/首事件超时包装为Compact错误，不进入普通业务重试。已领取但未提交的恢复再次进入也拒绝发送旧请求。
- 当前真实运行仅支持`native`工具协议（`select_tool_protocol`明确拒绝text），本片沿该事实，不重新开放已移除协议。原生Anthropic与OpenAI出站、工具开/关及自动选模的逐字对照通过；这证明材料一致，不证明供应商tokenizer或真实缓存命中。

验收路径保留原Gateway车道、真实SimpleAgent准备、原store/checkpoint/CAS、实际`read_file`和provider builder；HTTP为内存替身。恢复首轮与第二工具轮、无历史活动回合回退、坏原文、候选回退及失败不发送均有定向证据，准确数量见TESTS。

边界：初次普通加载与手动Compact仍走原估算；child、后台完整恢复请求尚未接入。若准备中的deferred工具先触发live Compact并推进代次，原CAS会拒绝旧来源，不覆盖新状态；该组合尚未单独验收。未提交的自动模型采用与延迟Compact并存时明确拒绝，尚未实现自动撤销后恢复。12.4整体保持未完成，未部署或使用真实供应商。

建议下一步：先把共享完整投影接至child/后台并补上述代次/采用组合，再做跨窗口、真实缓存和已安装TUI验收；共享生命周期由主线串行修改，可并行只读评审或独立测试文件。

### child overflow共享完整恢复（本地接入）

基线 `5aa08c7e4` 后，Gateway已验证的冻结/计量/摘要错误隔离/原CAS算法移至 `agent_core/compact_request_recovery.py`，原选模与恢复共用 `tool_request_capture.py`。Gateway不保留第二实现；通用模块不知道宿主目录或持久身份，只有原调用方绑定的投影回调。

child的 `prepare_subagent_thread_turn(defer_compact=True)` 保留原消息幂等追加、身份核对和取消检查，只读取未窗口化来源。`project_agent_thread_context` 从结构化view纯渲染原注入和历史seed，候选不再读任务/文件。原overflow循环在下一次真实运行准备时安装短生命周期scope，选中候选原CAS后继续同次生成，成功新上下文传回原循环；无transcript来源仍先压活动归档再准备。

子代理的历史注入来自原 `RunParams.inject[0]`，与用户正文内容无关；系统、上下文包、工具、权限及模型选择保持。展示失效现在在真正恢复准备时清除，摘要沿同一冻结请求保留原builder的无候选占位段；测试检查准备后状态及实际wire无旧卡片，不要求提前重跑准备。

后台调查已确认不能直接复制child：`context_markdown` 可能写任务进度，需要一次冻结结构化上下文再纯渲染；detached任务的锚点/lineage历史范围也需随同次准备保存。窄审计事件的seed=None是隔离约定，不能补入owner历史来让容量检查通过。后台尚未改代码，初次加载/手动入口及12.5—12.7仍保持待办。

建议下一步：先补后台一次准备的上下文与历史范围，再接同一恢复器。共享core串行修改，独立payload/失败测试与只读评审可并行；所有真实供应商和部署证据仍单独验收，不扩大本片结论。

### 后台一次性准备与作用域前置（本地）

基线 `468fec0a9` 后，`background_context.py` 将原 `context_markdown` 拆为prepare和纯render。prepare仍按原顺序净化wake、读取上下文与对账任务进度，冻结原预算及控制策略；render只对冻结材料执行原预算器和格式化。既有调用入口继续同样的准备→渲染，候选可持有准备值而不重复读盘或写进度。

`background_history_seed.py` 从同次成功加载的bundle构造 `BackgroundHistoryProjection`，包含原TaskScopeDecision、scoped thread和预算；正常种子也通过公共纯投影生成。锚点、lineage和摘要继承规则未改变；disabled/unreadable没有可用projection，不可被解释为合法空历史。

本片不把后台接到新恢复器。Astra max 使用真实 SimpleAgent、临时 ConversationStore 与原checkpoint/CAS（只替身摘要）复现三项原有边界：detached transcript提交v1检查点后，generation=1、source_messages=3，而后台seed为ready但summary和messages均空；detached活动工具提交v2检查点后，原工具可见数从1变0、seed摘要仍空；narrow audit同样隐藏已压工具，但seed按原约定disabled。原始transcript/工具账仍在，问题是恢复材料无法取得替代摘要，尚未进行实际HTTP。需在原append-only消息和committed checkpoint chain内明确适用范围与替代关系，不能另建第二摘要库或从自然语言猜作用域。

建议下一步：依据作用域复现实验先收口原Compact来源/替代关系，再接后台完整恢复和实际payload对照；共享checkpoint修改由主线串行，冻结/隔离测试可并行。本片不关闭12.4或真实缓存/TUI验收。

#### 作用域修复方案（审查建议，尚未实现）

继续使用同一committed checkpoint chain与唯一generation CAS，不新建任务摘要库或per-task状态表。拟在原来源/候选中携带结构化scope、摘要基础checkpoint ref和精确覆盖refs；scope只从冻结的TaskScopeDecision、conversation_turn_id及原run/attempt身份形成，不扩大权限。

原checkpoint拟显式升版：previous_checkpoint_id仍代表提交链，summary_base_checkpoint_id单独代表摘要语义继承，scope/base/coverage纳入候选身份；v1/v2按明确的旧全线程合同读取。局部提交不能推进全局transcript cursor或把局部摘要写成全线程摘要；thread上的全线程投影须绑定相应checkpoint，其他视图只解析同链。

只读resolver返回当前请求实际适用的摘要和覆盖refs，校验摘要基础链与来源。detached无适用摘要时从canonical原文和安全基础重新投影，不能套最新全局cursor；使用精确覆盖而非局部高水位，避免lineage后来增加时跳过未压行。活动工具也仅隐藏本请求实际消费摘要覆盖的精确refs，删除整链无条件union。seed/provider历史与活动IR应消费同一视图。

窄审计保持conversation_history_seed=None，仅压本活动回合的工具/IR，并以精确conversation_turn_id读取替代摘要；不能只凭task_id继承另一审计事件。摘要器必须读取适用base，不能继续直接使用binding.thread.summary。最后扩共享完整恢复器支持没有transcript但有active IR的来源，沿原一次准备、候选纯投影、CAS与同次发送完成接线。

以上是下一片待核实实施的通用合同方向，尚无新schema或resolver落地。需要覆盖交错任务、旧checkpoint读取、并发CAS、取消、坏来源、前台/child正常历史，以及窄审计不泄漏旧聊天的回归；先验证基础迁移，再改变宿主发送路径。


#### v3检查点与精确来源底座（本地实现，宿主仍待接）

基线`3cbbba540`后，原writer统一写v3；commit head与generation CAS保持唯一。scope、summary_base_checkpoint_id、精确message IDs/工具refs、摘要/证据与创建时间共同封印，ID带v3前缀。提交链前驱和摘要语义基础分开；reader只沿实际适用摘要的base收集覆盖，旧v1/v2显式按全线程格式读取。局部CAS的publish_thread_view=False只推进同一head与计数/清理旧代遥测，不覆盖全线程摘要、游标和摘要时间。

独立审查实际复现“只改schema v3→v2即可把局部摘要暴露为全线程”，已通过ID版本与schema一致性、旧格式拒绝新范围字段修复。创建时间影响任务可继承的边界，故包含于ID：新时间重试是新候选，唯一CAS仍只允许一份提交，未依赖候选ID去重代替CAS。

工具覆盖必须按原ToolCall的run/attempt/模型turn/call四元组。原native IR在摘要前捕获调用与结果配对身份，删除后做精确集合差；活动归档按记录身份切分来源/保留区，合法同call_id跨轮不拒绝也不相互隐藏。call_id列表仅用于展示，旧缺身份的归档不可精确命中，需替代未知来源时明确拒绝候选，不能补当前attempt。

原工具外置路径先于完整archive写入，现从第一次externalize起保留原attempt/turn；原index、source refs和carried重载沿用这些值。归档去重/运行中合并不按裸ID消掉不同轮；未知旧条目不冒充新执行进展。同正文同call但不同执行身份的新版artifact路径包含身份摘要，避免旧ref指向被改写的身份。旧路径不重写、不迁移。

进一步真实生成函数复现：child Goal多轮复用attempt，每轮新状态的序号均从1开始，原ToolCall.turn_id因此碰撞，使未摘要的新调用被旧覆盖误删。`tool_model_generation._begin_model_turn_identity` 现只在原turn_id添加每次模型调用的唯一nonce，原sequence语义不变，不新增身份账或改变宿主conversation_turn_id。参考本地Codex的`core/src/session/mod.rs::new_submission_id`与`session/inject.rs`的结构化唯一提交身份；实际修复仍使用本项目原ToolCall合同。

本片未将detached/narrow宿主绑定到新scope，也未完成同一个应用view的摘要注入/来源隐藏与完整恢复首请求。普通路径目前默认thread范围，后台原三类复现不能据此宣布修复。下一片仍须沿原准备与TaskScopeDecision绑定来源、支持窄审计seed=None的活动IR摘要、接公共完整计量并做实际payload对照；不能做普通后台可用而其它范围回旧旁路的收尾。

另有并行TUI线的合成历史容量证据：1.11一万行M2.7续聊410.81秒后成功，generation=1、4次provider调用、0重试，仅证明该样本；千万行无compact字节游标的after_compact_report在384MiB受限子进程立即MemoryError。后者明确未通过，需有界原文读取和append去重组合验收，不能替代真实长开发任务验收。对_segment_end的O(N²)初步判断已撤回（原实现已有二分），本线未据此修改预算算法。


#### 后台实际应用视图接线（本地，完整容量仍待接）

解决问题：全线程cursor不能替代detached任务的摘要覆盖，活动工具被隐藏前必须确认该请求实际使用同一摘要。`AppliedCompactContext`只是原checkpoint的请求内投影，经RunParams/RuntimeLoopParams/ToolLoopExecuteParams传递，不建立另一权威或权限。

后台普通轮选thread范围，detached复用原TaskScopeDecision的创建锚点、时间及精确lineage，narrow只选原conversation_turn_id与task_id。历史从完整canonical行先按原范围筛选，再排除同view覆盖；v1前缀终点在完整行中验证，不借全局cursor删除局部材料。operational准备复用已读bundle，再将摘要/证据对齐该view。窄事件保持seed=None，不吞旧聊天。

transcript source携同context，预检验证原链和来源身份；摘要候选使用view.summary/evidence，writer显式固定scope/base，原CAS仅thread范围发布全局摘要/游标。native/carried摘要也使用该base，活动归档只按该view四元refs隐藏；旧冻结view不会因为后来提交而偷偷多隐藏。原完整archive、运行预算和去重仍保留。

本片不关闭12.4：后台仍需接共享PreparedCompactRecovery的完整候选与首个实际payload；narrow已知空历史及active IR来源须接同一容量合同。Gateway/child原种子也须在其准备边界绑定同view，消除旧摘要/最新覆盖竞态；不能保留双轨作为最终实现。CAS后候选context要按获胜提交更新，不能继承候选旧base或重读别人更晚的head。初次/手动入口、超大历史有界读取、真实缓存均另验。

建议下一步：直接接公共完整恢复器并以实际HTTP材料核对，不继续扩新状态；Gateway/child准备可独立核对，公共捕获与CAS接缝串行。测试机1.9已只读确认可达，尚未部署；不能将本地测试当安装版验收。


#### 三宿主同视图准备与后台完整恢复（本地接线）

解决问题：候选计量必须覆盖恢复后的实际请求，后台重试不能重新扩大任务范围，候选提交后也不能继续携带旧摘要基础。
Gateway、child及后台共享 `load_conversation_compact_source(scope=...)`：从canonical行按同次scope裁决、解析旧覆盖并产生唯一AppliedCompactContext；选择器必须返回原对象的有序子序列。Gateway窄审计以真实request ID和task ID绑定turn范围。后台同工作片冻结初次TaskScopeDecision和上下文bundle，溢出仅刷新原scope的检查点与原文。完整历史读取目前仍无界，未解决已记录的超大历史内存失败。

后台在真实overflow后安装公共PreparedCompactRecovery，原runtime准备一次，然后纯替换第0上下文注入、seed、摘要及代次。普通/独立任务先压transcript；已知空transcript可沿原active-turn archive摘要器、writer及CAS压carried工具交接。窄审计仍seed=None，不读取旧聊天；空source与不可读来源明确不同。候选使用原完整模型计量和已知输出预留，未知、过大或取消不取得覆盖权。

原IR的CompactionSummary增加内部source，仅原构造点标记carried_tool_handoff/applied_compact，不解析文本。纯投影保持用户媒体、插话和运行事实顺序，只替换已知交接位置；原完整归档、审批、拒绝记录和预算不裁剪。活动来源缺完整四元身份的旧记录保留可见，不能因无法覆盖而丢弃。混入真实ToolCall/ToolResult的未知组合拒绝候选，不能造工具对假装恢复。

恢复宿主在该次生成安全点拥有Compact，原自动压缩不能抢先推进来源代次；普通工具轮仍沿原行为。CAS后使用获胜result.thread解析真实checkpoint/base/coverage并回填参数，不读取别人的更晚head，也不重新render已测请求。后台模型输入不再包含writer时才产生的内部checkpoint ID，避免提交前后字节漂移。同片首次业务不占压缩配额，之后最多八次真实提交才公平让出，未提交的恢复不能被当作健康进展。

状态：本地实现和定向验收中，准确结果见TESTS；未部署或使用真实供应商。12.4继续未完成：初次/手动入口、真实native IR组合、transcript与active同时很大时的联合候选仍待处理；Gateway/child无transcript的旧活动归档入口尚需迁移至完整请求计量。12.5—12.7和旧全仓八项失败仍独立保留。

建议下一步：先完成本片实际HTTP和取消/CAS组合验证，串行收口公共恢复器；独立测试可并行。之后迁移其它活动归档入口和初次/手动，最后安排测试机1.9隔离真实验收，不重启既有Gateway。


#### 混合来源单候选与三宿主活动归档（本地切片已验）

为处理transcript与carried同时过大的情况，在原transcript候选中携带可选工具分区，复用活动归档的近期预算及精确四元身份。源记录和保留记录由同一纯分区产生；未知身份仍原序保留，不假装已有覆盖。候选摘要同时读取原消息和所选工具的完整模型可见投影；不能用有界carried handoff的截短结果授予全部来源覆盖。过长材料沿原generate_bounded_compact_response分段，无第二摘要调度器。

原ConversationCompactView可携保留工具记录（None表示不替换，空tuple表示已知无保留工具）；公共恢复器先执行宿主历史投影，再统一替换已标记的活动交接。候选同时封印source_message_ids与source_tool_refs，v3原reader已支持双来源；仍只经原writer和generation CAS提交一次，接受前必须满足完整恢复请求容量。未完成实现或测试前仍不关闭12.4。

强制恢复的内部工具近期预算为0，表示选中全部身份完整且尚未被当前view覆盖的carried记录；它不是新增用户配置，也不改变原非完整projector入口的近期预算。全部选中记录经原逐条脱敏/模型投影和原分段器，外置完整输出仍按引用读取，不声称已读外部文件。未知身份留在原位；覆盖refs、摘要、保留区和完整投影属于同一个候选，候选回退不得拼接最后一次材料。

空摘要或模型返回工具调用时沿原机械回退，但必须附上全部所选工具材料；过大就拒绝提交，不能既丢掉中段又声明全部来源已覆盖。普通故障后的可接受候选回退先复查停止；ToolCancelled/InterruptedError中性丢弃，不增加熔断。摘要/投影及92%提交回调的typed取消明确透传，原检查点落盘后、CAS之前再次检查停止；可能留下孤立候选，不推进已提交代次。

Gateway/child无transcript的活动归档已删除先粗估提交后重新prepare的旧旁路：在真实恢复prepare里生成、计量候选，CAS后同次发送。child保留空的第0注入槽并同步已清除能力展示，渲染字节和同轮不重决策语义保持。三宿主混合路径共用同一工具替换逻辑，没有第二模型目录、摘要账本或恢复状态机。

参考复核：本地Codex `codex-rs/core/src/compact.rs` 的历史快照和替换边界，借鉴候选绑定原历史并一次替换；未移植其删最老消息重试策略。实际差异、所测覆盖和剩余边界见本轮交接及TESTS。

剩余：初次/手动入口、carried交接之外真正混入ToolCall/ToolResult的native IR、媒体线集成、超大canonical有界读取及供应商真实usage缓存。旧全仓八项失败未关闭；12.4继续未完成，本地假HTTP结果不能代替测试机1.9安装版验收。

16K窗口的原恢复安全点已验：同一短摘要和冻结输入，纯transcript投影15,174，混合投影14,084，原门14,400；已知1,024输出预留也通过。原HTTP payload等于获选候选、单次CAS。该对照使用原计量器，数值只是本地估算；没有推导供应商缓存或精确token结论。

本片32文件联合738 passed（75.48秒），严格本地gate通过，源码与测试失败适配过程见TESTS；不以本片验收关闭12.4其余入口或旧全仓失败。


#### 首次自动准备与手动来源（本地切片验收中）

首次Gateway/child只加载原Compact来源，在真实PromptBuilder准备后、child和主代理选模之前执行公共prepare_request_context。后台首次同样接入，force=False按完整请求与原输出预留判断；overflow继续force=True。容量充足保留同一次冻结输入，后续工具轮可继续；没有可覆盖来源也不制造摘要提交，原选模与实际发送压力门仍独立工作。取消、未知投影、失败不伪装成无操作。

上下文提交早于候选模型绑定及发送前拒绝回退基线：拒绝候选时只回退模型，继续使用已提交摘要。Gateway采用器仅更新本次获胜Compact代次，保留原profile/revision守门；child更新已有首次准备的注入和工具上下文，不重领资格或重跑原业务准备。候选模型自己的原依赖准备与探针合同保持。

手动Compact无法提前知道下一条业务prompt、工具面或媒体，因此只报告“会话历史估算（不含下一请求）”。原执行车道内冻结THREAD_COMPACT_SCOPE来源，以该source.messages判空，并把同一source交给原Compact；task局部head不能使全线程待压记录被误判为空。控制回执仍幂等，不追加普通用户消息或启动业务agent.run；未来业务请求自行验证完整容量。

当前生产已经删除text工具协议，禁用工具仍使用native snapshot。本片不恢复已删除协议，也不为其增加兼容投影。真实ToolCall/ToolResult混入carried的完整来源合同仍未完成，12.4继续保留未完成状态。首次/恢复真实供应商缓存、媒体、超大历史有界读取及旧八项全仓失败没有随本片关闭。

本片联合1005项通过；手动helper收口128项、补空来源回归Gateway16项通过，数字有重叠。后台首次真实提交计入原八次预算。具体失败过程、严格gate和现场测试隔离事故见TESTS，后者不计产品验证。


### 12.4 原生工具 IR 来源（本地实现与验收中）

解决归档 preview 短于真实 ToolResult 时，仅摘要 preview 却覆盖全部工具回放的遗漏风险。同一次恢复先冻结 archive 和 IR，再按原 ToolCall 的 run/attempt/turn/call 四元身份划分完整往返；ToolResult 没有完整身份，归属只由原 AssistantTurn 区间和 call_id/tool_name 一对一关系证明。跨轮同 call_id 不相互删除，未知、孤儿、不完整、重复身份、插话打断和不透明媒体组保留，关联归档不获得覆盖。

仍使用原 CarriedToolCompactSource、摘要 adapter/分段器、writer 和单次 CAS。摘要输入同 ref 优先完整原 IR，剩余归档沿原模型投影。候选显式保存 retained_ir_history，None 表示不替换，空 tuple 表示已知全部替换。三宿主只改历史和注入，公共投影处理 IR/工具：确有完整配对回放的归档不重复生成交接；没有旧交接且仍有 archive-only 材料时按原位置插入，不能依赖 tool_context 绕过 IR 后自行可见。归档全账、审批和执行预算不变。

严格恢复的直接空回复/工具调用回退，在有界概览之后完整保留旧摘要、所选 transcript 原文及工具投影，再由完整容量门决定能否提交；分段修复仍失败则抛 typed 错误，不能靠截断摘录取得覆盖。默认手动旧回退不扩大范围。

证据边界：纯冻结 IR 的下层安全点验收与外层溢出自动接续分开。当前外层重跑 agent.run 会从归档重建交接，未保留前轮原生 ToolCall/ToolResult；本切片不能宣称外层原生 IR 接续完成。媒体线、真实供应商缓存、超大历史有界读取及旧全仓八项失败仍开放，12.4不勾选。测试机192.168.1.9可用于后续已协调的隔离验收，本片尚未部署或重启。

建议下一步：先完成本切片隔离联验，再接外层原生 IR 传递与媒体整合；共享执行参数和恢复桥接由一个 owner 修改，独立测试和只读审查可并行。


### 12.4 外层原生IR carry（基线95ffee08a，本地联验中）

三宿主同进程、同owner/request/run/task/逻辑turn/view携带真实IR和tool_context，主attempt可由原DB轮换，child保持来源attempt；原工具四元引用不改。深复制后AssistantTurn恢复普通类型，旧工具轮归并标记不跨循环。UserTurn沿原插话包带input_ids，真实循环在overflow后沿原ledger释放，宿主只按精确释放ID过滤；部分包释放拒绝，异常仍先保存完成事实。

每轮重新准备权限/工具/provider前缀；完整archive只恢复预算、幂等及工具载入，不重复做待丢弃的语义摘要。prefix接管新摘要时同步移除IR中旧applied_compact；没有transcript或完整工具来源的强制恢复明确COMPACT_SOURCE_EMPTY。历史能力卡原序保留，当前展示读最新结构来源。无任务后台保持首次解析request_id，typed视图与显式当前线程交叉校验，不通过task属性误升任务。

Gateway/child真实read_file、完整中段标记、IR-only零archive、候选实际HTTP等价及单CAS均有隔离测试；fake HTTP不能作为供应商/TUI验收。最终联合结果和首轮失败见TESTS。媒体线、真实缓存、超大历史及旧全仓八项失败仍开放，12.4不勾选。

建议下一步：先收口本片定向与严格gate，再协调媒体合入及授权1.9隔离安装验收；只读审查和测试可并行，writer/身份/共享Gateway由单一owner操作。

本片最终44文件联合1154 passed、4项既有xfail（135.39秒），日志 `/tmp/compact_carry_final_20260923.log`，清单同名 `.files`；taskless不误建会话任务补充断言后两协议另跑2 passed。Ruff、doc sync、import boundaries 0、strict code-size hard=0且基线未改、diff与clean-package通过。没有push、部署或真实供应商调用，线上CI没有作为验收来源。12.4仍开放，11/18清单数不变；建议下一步协调媒体集成与授权1.9隔离验收，公共状态单owner、独立测试可并行。

## 第 12.4 项媒体集成与未知模态边界（本地切片已验）

解决媒体引用被当作完整文字计量、或未读附件却取得摘要覆盖的问题。以 `81bdf9579` 为基线整合媒体线 `3adb61904`；媒体原件仍在原owner内容寻址目录，UserTurn保留input_ids及media，三宿主原生carry不另建存储。原生发送和出站投影复用同一后端组包。

共享 `backends/request_content.py` 在适配器过滤前检查原IR和原始消息。当前模型可继续携带原文字思考；跨模型候选不搬运供应商推理签名。未知模态不额外探测候选，保留原模型；普通原模型媒体请求继续发送。自动Compact跳过未知计量，供应商已报overflow的强制恢复返回COMPACT_REQUEST_PROJECTION_UNKNOWN，不摘要、不提交checkpoint。字节预算只约束文件展开，不是视觉token或窗口容量证明。

transcript Compact只摘要首个非文本原生信封所在完整回合之前的安全前缀，媒体回合及其后所有行原序保留，游标不越过未读来源。分段器在JSON化前拒绝非文本块，不能通过引用字符串取得覆盖；工具原账和旧检查点事实不重写。无完整文字前缀时typed拒绝，不伪造空摘要。

媒体线此前官方M3图片/视频/重连证据属于原提交，不能当作本次集成版真实验收。本次pytest采用隔离HOME、真实本地媒体读取和业务工具，仅末端HTTP替身；尚未部署测试机或重启共享Gateway。12.4、真实缓存、超大历史及旧全仓八项失败仍开放，11/18清单数不变。

建议下一步：联合定向与严格gate后保留可审查本地提交，再协调192.168.1.9验收窗口。测试和只读审查可并行，共享Gateway及恢复writer保持单一owner。

本片55文件联合 **1419 passed、4项既有xfail**，退出码0；清单 `/tmp/compact_media_joint_20260923.files`，日志同名 `.log`。pytest配置和命令各带一次-q，原日志只有逐项结果和进度；统计为1419个通过标记及4个预期失败标记，不补猜运行秒数。初次媒体兼容检查144 passed、2 failed来自UserTurn新增字段的旧位置参数，已改显式media关键字并纳入上述联验；容量/选模91项、媒体工具轮4项、transcript26项均被最终联合覆盖，不累加计数。首次guard的导入排序和深层嵌套已修，新增文件暂未登记的打包提示在纳入本片后消除。

Ruff、doc sync、import boundaries零发现、strict code-size hard=0且基线未改、diff和clean-package通过。本地严格gate已通过；没有push、部署、真实供应商调用或Gateway重启，线上CI没有作为验收来源。未知媒体的强制恢复仍明确拒绝，不能宣称已实现完整多模态容量计量；旧全仓八项失败保持。

### 超大canonical来源剩余工作（只读审计，未实施）

`load_conversation_compact_source(limit=0)`不能仅替换成分页：四宿主还把完整来源冻结为tuple，scope筛选复制全量行，v3基础链合并所有source_message_ids，checkpoint reader也全量读取。现有MessageStore前向字节游标可复用，但需固定完整行EOF和页字节上限；history_page为完整工作片可越过行数目标，不能作为硬内存界。append_once及历史索引重建也存在全量化。

建议先在原MessageStore补有界页/幂等扫描，再沿原writer/CAS设计可验证连续范围覆盖；task稀疏覆盖仍须精确，不以全线程游标跳过未读行。detached锚点不存在的退回语义需先有界确认，不能逐页套列表selector。只读核对Codex compact.rs发现其移除最旧消息的恢复分支不满足本项目来源覆盖要求，不直接照搬。本轮未实现此后续方案，12.4继续开放。


### 固定尾界与扫描底座（本地实施中）

MessageStore前向页复用原路径/字节游标，新增可选through和max_bytes；complete_offset_report按64KiB块确定完整LF尾界，半行与后续追加不归本次扫描。页预算不足保留下一完整行，首行超预算返回MessagePageBudgetExceeded，原游标不动；不把资源限制说成数据损坏。未传参数的显示调用保持原行为。尾界只固定append-only文件的范围，不是抗等长篡改快照，也不能代替Compact来源覆盖证明。

append_once沿原锁流式扫描固定物理EOF的全部记录，命中后仍检查后续坏行、排除display并保留首个key；只保留当前行与匹配项，内存O最大单行，尚无单行硬帽，不宣称绝对内存上限。额外修复原有效JSON但缺LF尾行被当成已提交的漏洞：append_once现在拒绝所有未终止尾行，避免原追加器把新JSON拼接到半行后；原文件保持不变。线程更新及唯一JSONL追加不变。原history_page完整LF算法已移入store_io供两种方向共用。参考本地Codex rollout/ordinal.rs的BufReader逐行解析、session_index.rs的逐行索引查找；只借鉴流式读取，后者跳坏行语义不适用于本项目canonical来源，未照搬。

本片不替换Compact全量tuple/scope/checkpoint链，不改变writer/schema/摘要覆盖；完成此底座后仍须接通范围冻结与分批摘要，12.4不勾选。

本片交接：基线1b5c71efb，分支codex/decision-model-integration，由本线实现、Sol high只读复核；已与模块重构owner确认MessageStore/只读helper和共享完整LF算法范围无冲突。未修改Gateway/runner恢复、Compact writer/schema或测试机进程。新增message_scan与定向测试，修改store_messages/store_io/history_page及文档。

六文件联合 **153 passed（3.96秒）**，日志 `/tmp/decision_message_scan_final_20260923.log`。覆盖UTF-8/长行字节界、固定EOF后迟到追加、超预算半行等待、首行预算失败、坏行后的原游标、扫描期间真实截断、命中后的损坏、display/CRLF、并发同key及2000行大历史。大历史测试禁止recent_report入口，并用tracemalloc证明峰值低于文件体积三分之一，不把此阈值说成通用内存上限。首轮五文件130通过；随后扩展命令曾误写不存在的测试文件名，收集失败且未运行测试，修正后才取得153通过。

建议下一步：在原Compact来源/作用域读取接入固定尾界，再沿原writer/CAS处理连续范围证明，不能直接把tuple改成分页就声称完成。其它agent可以只读复核或独立构造存储边界测试，不并行改共享扫描、作用域选择和writer。本片仍O历史长度扫描时间，单行解析无硬帽；固定字节尾界不防违规等长替换，显示页暂未启用新字节预算，整项12.4继续开放。

本片Ruff、doc sync、import boundaries零发现、strict code-size hard=0且原基线未改、diff和clean-package均通过，本地严格gate已通过；未推送、未部署，线上CI未作为验收来源。页预算约束返回行的原始字节总量，边界探测最多额外读取1字节，完整尾界查找另有最多64KiB块；解码对象和既有单行本身仍需要对应内存，不宣称进程RSS等于页预算。


### 固定来源范围筛选（本地实现，接口集成未验完）

显式scope的Compact加载不再先recent_report(limit=0)取得全部正文再筛选：在原MessageStore完整LF尾界内第一遍验证非display身份/顺序与创建锚点，第二遍只保留宿主范围内未覆盖正文，并比较同范围原字节SHA256；迟到追加留待下一次读取，坏行、重复身份、缺legacy终点及两遍之间改写均拒绝。selector改为基于只读位置的逐行bool，不能返回另造行或重排来源。v3精确ID排除及legacy前缀终点沿原视图，无新checkpoint或覆盖写入。

Gateway沿原visible谓词；后台detached创建锚点/时间加精确lineage统一编译成同一谓词，缺锚点保持仅精确任务。后台operational的_detached_task_messages也沿同一原文扫描，只在选完范围后应用原展示limit，不先全量to_dict。

本片内存仍包含O消息数的ID位置和O未压正文，单行按原JSON解析完整读取，没有增加任意正文截断或行大小硬门；仍有旧未显式scope入口和checkpoint覆盖链待迁移。其意义是移除已覆盖/范围外大正文的全量常驻，不等于全部Compact有界。后续须接流式分段与同一writer/CAS连续覆盖证明。

后台有任务时通过原context_bundle_report(include_messages=False)延后正文；同一次任务事实决定范围，普通任务再沿原recent_limit读取，detached沿固定来源筛选后才应用窗口。无任务与其他调用默认行为保持；0仍表示不限制。读取错误进入原load_errors，禁止空历史继续生成。

本片交接：基线467f3cac3，分支codex/decision-model-integration，决策线负责原只读加载及范围筛选；已与模块重构owner对齐。生产文件为message_scan/message_selection、compact、background_context/background_history_seed、store及gateway_parts/request_context；未改runtime、claim、Compact writer/schema或主线独占test_background_main_agent_runtime.py。不推送、不部署、不调用真实模型。

最终12文件联合 **326 passed（30.05秒）**，日志 `/tmp/decision_scope_scan_final_20260923.log`；覆盖固定EOF后追加、两遍间改写、排除行仍验坏数据、只读位置、legacy完整前缀、锚点被覆盖/缺失、0与1窗口及读取错误。1000行8KB正文的tracemalloc检查仅证明本样本避免全量常驻，不是RSS硬上限。此前225/71/29项属于重叠中间验证，不累加。

**仍有4项明确失败，整体gate未通过**：主线独占test_background_main_agent_runtime.py中的test_background_context_overflow_resumes_after_committed_recovery_same_slice、test_background_compact_slice_yields_after_eight_progressful_generations[False/True]、test_background_empty_transcript_carries_active_turn_into_committed_recovery。结构化load_errors均为TypeError：fake Store.context_bundle_report不接受include_messages；日志 `/tmp/decision_scope_failures_typed_20260923.log`。owner要求保留其7.10文件独占，由第8步集成适配；本线不增加生产兼容分支来隐藏测试接口差异。原8项历史全仓失败不被此片覆盖。

建议下一步：主线适配四个fake Store后联验；本线另片修复Compact已保留历史在history_projection等宿主投影再次受字符窗口裁剪的问题，并比较候选与真实HTTP。普通展示窗口继续保持。之后才沿原摘要分段器接可重读来源，原生信封必须跨页完整分组，writer/CAS继续唯一。SQLite临时索引及v4覆盖分页只是审查提出的候选方向，尚未采纳或实现；不能将其当成本片交付。只读审查和独立测试可并行，恢复写入及共享Gateway维持单owner。

本片非pytest守卫现已通过：Ruff、doc sync、import boundaries零发现、strict code-size hard=0（未改基线）、diff和clean-package。首次Ruff发现新增测试的两处导入格式，doc sync发现Gateway注释/模块文档遗漏，clean-package发现两份新文件未纳入版本管理；均已修正。主线独占4项测试接口失败仍开放，因此整体本地严格gate未通过，不推送；线上CI未作为验收来源。

Sol high独立只读复核未发现可确认的新增缺陷：核对普通0/1窗口、延后读取错误、detached锚点与lineage、固定EOF/hash及legacy边界；未重复运行测试，不以审查替代上述测试。


### Compact保留历史完整投影（本地已验，基线5c8183651）

解决已裁决的未压历史/媒体后缀在宿主字符窗口中被再次丢弃、进而低估候选容量的问题。history_projection增加仅内部preserve_complete模式，必须显式传入来源；Gateway初次来源及恢复候选、后台scope/coverage后的来源及候选、child原Compact视图沿同一路径完整投影。普通非显式读取和展示窗口保留；原scope/role/current-request排除及终态工具折叠不变。没有配置开关，这是保留历史和计量一致性的修复，不增加摘要writer或权限。

先核对本地Codex参考compact.rs的整体history替换和重算口径；其溢出时删最旧输入的策略不照搬，本项目仍要求先证明完整摘要覆盖。Sol high只读跟踪seed→tool loop→Anthropic/OpenAI Chat/Responses组包：未发现通用条数/字符再裁剪；协议转换仍会处理不支持块，不能宣称任意媒体完全等价。完整恢复器当前仅支持native协议，文本seed完整渲染不等于文本恢复链已验。

新测试用超过原展示预算的完整行及最早媒体/工具配对验证三个宿主seed；两协议完整原宿主执行捕获最终post_json载荷。小材料完整发送；60万字符大来源完整进入捕获输入，原容量门触发压力，未知媒体在强制恢复拒绝，零业务HTTP、零摘要覆盖/CAS。初版4通过6失败源于误把超容量样本期望为可发送；原production容量门保持，修订为可发送/必须失败两组后16通过（6.48秒）。随后补充真实冻结输入上的三个候选投影，低字符窗不删保留行且纯投影不提交，六文件联合73 passed（15.88秒）；随后10文件相邻回归416 passed（62.41秒），重叠部分不累加。日志分别为 `/tmp/decision_retained_joint_20260923.log` 和 `/tmp/decision_retained_adjacent_20260923.log`。HTTP为内存替身，无真实供应商调用。

本片只写history_projection、agent_thread、background_history_seed、gateway_compact_recovery与request_context，新增独立test_compact_retained_history；未碰主线runtime/claim和其独占后台runtime测试。上一片4个fake Store接口失败仍由主线集成，整体gate未通过；11/18与12.4开放状态不变。全链流式来源/覆盖、超大摘要分段及供应商缓存组合仍待验。

建议下一步：联合验证保留候选与发送载荷，再沿原摘要分段器减少全量正文常驻。独立测试和审查可并行，writer/CAS和共享Gateway保持单owner。

本片Ruff、doc sync、import boundaries零发现、strict code-size hard=0且原基线未改、diff和clean-package通过；初次Ruff的测试lambda/导入已改partial及格式，模块文档已同步。Sol high再次只读核对五个生产diff，未发现新增缺陷，确认角色/当前请求/Audit/任务范围过滤仍在完整保留前执行。新测试HTTP为post_json最终JSON捕获，不是socket或供应商验收。原4个主线fake签名失败仍保留，整体严格gate未通过；不推送部署，线上CI未作为验收来源。主线7.10发布窗口继续独占本机/.10 Gateway，本线未操作任何测试机进程。


### 摘要序列化与当前片段窗口（本地已验，基线f2bc98626）

解决摘要分段前整份json.dumps及二分第一次复制半份来源的问题。compact_text_source只为既有摘要循环提供字符视图：JSONEncoder.iterencode首遍测总字符/hash，第二遍按需取窗口，分段成功才释放前缀，全部结束核对EOF/hash；不写文件或增加持久索引。两遍hash只证明读取到的编码序列一致，不承诺原对象在读过以后永久未变。字符位置不作为canonical消息字节游标或摘要覆盖权威。

分段器先指数探测再二分有限窗口，沿原累计摘要、纠正次数、供应商窗口错误缩预算、usage和取消链；单次小请求保留同一request对象与原缓存面。Astra max只读复核验出旧纠正预算缺口：1472预算首次刚好1472，EMPTY/TOOL_CALL/TRUNCATED纠正后分别1508/1531/1517。现按完整空材料请求选最重纠正提示预留，并在每次实际发送前复验；不新增重试策略或降级权限。来源EOF后、估算后与fitting响应后补取消检查，迟到结果不能成为成功摘要。

memory_archive/tokens.estimate_tokens复用唯一原公式，按相同sort_keys/default JSON编码流累计UTF8/字符长度及原结构开销；未新建估算器，TypeError/ValueError只捕获编码迭代，UnicodeEncodeError先暂存，编码流正常结束才抛出，后续JSON失败仍优先走原str回退；因此连“前面孤立surrogate、后面混合key排序失败”的异常优先级也保持。审查曾发现嵌套孤立surrogate误被当ValueError回退，本片在验收前修正并补测试。删除只有原估算器调用的旧_payload_to_text，不加兼容转发。

本片内存边界：仍持有原messages和最大JSON单字符串，排序需最大字典宽度，异常str(payload)及非strict机械digests仍可随来源增长；只移除全量序列化副本和无界二分探测，不能称整个Compact已有绝对内存上限。新1200条约两百万字符样本通过完整源hash、连续首中尾及tracemalloc峰值低于编码字符数一半断言；这不是RSS保证。单大消息、Unicode、替换/追加/截短、当前片段重读、释放旧段、EOF取消和迭代器关闭均按各自合同验证。

交接：主线明确确认compact_request_budget、新source helper及tokens.estimate_tokens和独立tests/docs无冲突；writer/schema/runtime/claim和主线独占后台runtime测试未改。参考前片原摘要/存储合同以及本地Codex整体历史替换，未另建摘要循环或照搬其丢旧输入行为。全链初始原文载入、检查点精确ID覆盖与供应商实测仍待推进，原4项主线fake Store签名失败保持；11/18与12.4仍开放。

验证：初始43项通过；新增流式与估算回归62项通过（3.65秒）；9文件176项通过（11.67秒），补发送估算/响应取消后15文件315项通过（24.59秒）；审查再补UTF8错误与后续JSON失败组合的异常优先级用例后重跑最终联合。此前计数相互重叠不累加。无真实网络请求、Gateway操作或部署，线上CI未作为证据。

建议下一步：完成最终联合与守卫后给主线可审查本地提交，继续沿原source/coverage减少正文全量驻留，保持原生信封跨分页完整分组。只读复核和独立测试可并行，来源及writer各守单owner。

最终15文件 **316 passed（24.86秒）**，日志 `/tmp/decision_stream_verified_20260923.log`，替代同范围315项中间结果，不累加。Ruff、doc sync、import boundaries零发现、strict code-size hard=0且原基线未改、diff均通过，clean-package在纳入新文件后通过。严格全链状态仍受原4个主线独占测试接口失败限制，不能称整体gate通过；没有推送、部署或真实供应商调用。审查提出的JSON失败优先于UTF8失败组合已修并纳入最终用例。

主线8.1已明确整枝含decision/Jev大量历史，不能整枝合入refactor；本线正在独立核对Compact/source/projection函数级移植闭包与前置commit，菜单、决策选模和配置不作为必需功能搭载。本片不要求主线暂停其_tool_loop_service职责拆分。


### 主线最小移植闭包交接（2026-09-23，未合并）

- workstream/owner：decision-model-integration，本线实现与验证，Sol high 独立只读核对依赖。
- 基线：主线 `85050017d687694f8167499798cbad07dd087b85`；来源 `425bcb3a9ab816e12ebc3b72b36a68ae58e5f2e0`。
- 目标：主线可以独立取得消息扫描和摘要内存修复，不必同时接入 Jev、自动选模、菜单或 decision 配置。
- 操作边界：仅临时 Git index 组合与仓库外导出验证；主工作区、分支、Gateway 和测试机未改。

可审查补丁分三组，路径均相对 `agent_py_agent/`：

| 组 | 生产文件 | 验证文件及前置 |
| --- | --- | --- |
| A 消息扫描 | `agent/conversation/store_io.py`、`message_scan.py`、`store_messages.py`、`history_page.py` | `tests/test_conversation_message_scan.py`；来自 `467f3cac3^..467f3cac3`，无 decision 依赖 |
| B 等值 token 估算 | `agent/memory_archive/tokens.py` | `tests/test_archive_tokens.py`；来自 `425bcb3a9^..425bcb3a9`，独立于 A |
| C 摘要顺序窗口 | `agent/conversation/compact_request_budget.py`、`compact_text_source.py`，以及 `agent/backends/request_content.py` | `tests/test_compact_text_source.py`；针对上述主线基线到来源的文件差异，按 B 后验证 |

A 包括流式幂等去重和未终止 LF 尾行拒绝，不只是新分页 API；不包含 `5c8183651` 才增加的 `scan_message_snapshot`。B 保持原估算数值及异常优先级。C 不是只抽取最新 commit：它还带 `95ffee08a` 的可选严格摘要失败参数及 `319004926` 的非文本来源拒绝；纯谓词 `request_content.py` 是因此必需的自包含依赖，默认严格参数关闭，没有自动选模调用方。主线审查时必须明确这两个行为，不可把 C 称为纯性能改动，也不必搬入这两个前置提交的其余文件。既有 `test_compact_request_budget.py::_request` helper 在基线可直接使用。

仓库外工件目录 `/tmp/decision-compact-port-20260923/` 中有三份 patch、`manifest.json` 和验证日志；工件可能被系统清理，以上固定提交、文件清单和 diff 区间可重新生成。各 patch SHA256：

- A-message-scan.patch：`a7ba56bda3a15cf466465d9cd2e13e4687f36a4ca529553bf5121b536e3bc722`
- B-token-estimate.patch：`dfe25fcb0464b17755d539908189a324c3ff6bd8587fc888e8fd4b5bfd698c79`
- C-summary-stream.patch：`33130b9539cfa64196b88806e1f3fcb288282ab6c3a2067b45cb0e75ad04a097`

三组按 A→B→C 在临时 index 逐组 `git apply --cached --check` / apply 成功，结果 tree 为 `42e46031307e36c6d3c0a4bef7197ff63c338839`。从这个树导出独立临时代码副本进行运行验证，使用原 pytest HOME 隔离 fixture，无真实供应商调用。先四文件（message_scan、archive_tokens、compact_text_source、compact_request_budget）97 项通过；再五个基线相邻文件（conversation_store、conversation_message_stream、conversation_history_paging、conversation_history_display、gateway_conversation_compact）**139 passed，6.61秒**。两批是不同测试文件，日志分别 `validation.log` / `adjacent.log`；11 个变动文件 Ruff 通过。没有运行完整发布严格 gate；补丁尚无主线文档适配，不作为可直接发布证明。

scoped Compact 后续闭包不能省略：

1. 只读选择器：在 A 之上抽取 `5c8183651` 的 `message_scan.scan_message_snapshot` 与 `message_selection.select_message_snapshot`。两遍同 EOF/hash 校验，仍 O(全部 ID＋选中正文)；该原语不自行建立持久覆盖权威。
2. 持久范围：`7f473c44f` 的 `CompactScope`、v3 checkpoint、`summary_base_checkpoint_id`、精确 `source_message_ids/source_tool_refs`、提交链校验和 `resolve_compact_summary_view`。主线当前 v1/v2 全线程 cursor 不能代替局部覆盖；必须沿摘要基础链排除来源，不能把同提交链上的兄弟任务误算已压缩。
3. 宿主同源接线：`AppliedCompactContext`、`7c73c5d96` 后台 scope/view 刷新及 `5aa08c7e4` 的 `ConversationCompactSource`/Gateway 恢复投影；后台延后正文要同时适配原 `context_bundle_report(include_messages=False)`，复用同一次任务事实。
4. 完整下一请求预检：`002039663` 输出预留、`289f29ce1` 冻结注入、`468fec0a9` child 共享恢复、`dbb2d6983` 后台、`896cde5c4` 混合来源、`95ffee08a`/`81bdf9579` 原生重试及来源保留。按主线接口抽取通用函数，不整提交搬运；包含选模集成的 `6a65cc708` 也不是必要整包依赖。
5. 最后才接 `f2bc98626` 的完整保留投影。仅移除字符窗却没有以上完整来源/容量门，会扩大请求但没有相应预检。

需要主线协调：继续保持 `_tool_loop_service.py` 的 8.1 职责拆分独占；scoped 接口适配后，原四项 `test_background_main_agent_runtime.py` fake Store 签名由主线 owner 更新。此次 A/B/C 没有接该接口，97/139 项通过不能消除决策分支原四项失败，也不覆盖旧全仓八项历史失败。文档应由主线按实际采用切片更新，不能把本线 Jev 文档作为生产依赖整包搬运。

建议下一步：主线可先审查 A/B，再审查 C 的两项额外行为并运行其发布 gate；scoped 合同按上述顺序另片接入。本线继续第 12.4 完整来源/覆盖与真实缓存验收，11/18 不变。只读审查与独立测试可以并行，共享 writer/CAS、主线拆分文件和 Gateway 保持单 owner。


### 12.5 完整请求与输出预留组合收口（本地）

解决此前只用固定token数字验证接受公式、缺少真实宿主完整输入组合证据的问题。本片不改生产代码或输出政策；沿现有200K显式共享窗口、50K实际输出cap，使用原Gateway/child/后台入口、完整冻结投影、原估算和唯一Compact提交。Chat与Anthropic各覆盖可容纳与拒绝场景，Gateway启用原生工具schema；摘要内容和末端HTTP返回为测试替身，供应商usage不作证据。

当前不可压缩要求含明确首尾与完整正文：Gateway/后台使用原系统配置，child使用原runner instruction入口，不能假定父system_prompt会成为child系统提示。候选和实际payload必须完整保留该正文与50K输出cap；允许的候选只提交一次并与首个实际payload相同。拒绝组完整输入仍低于180K原trigger，却高于或等于150K剩余输入界，返回原COMPACT_CANDIDATE_TOO_LARGE；无业务HTTP、无checkpoint/summary/byte cursor推进。并未为通过而缩短当前要求、减输出或修改估算数字。

Responses另两项捕获原_generate最终request_json参数：普通max_output_tokens等于原预留，OAuth明确不发送该字段、预留保持未知；返回0只表示不推断额外预留，不是零输出耗用保证。Responses当前完整纯payload投影返回None，不能借Chat继承冒充自动模型采用已支持。等号、窗口未知、cap未知、未知投影与active来源的精确失败边界沿本轮联跑的已有测试，不宣称新HTTP组合覆盖每一个单元边界。

新增 `test_compact_output_reserve.py` 14项；最初child fixture错误依赖父system_prompt，后改成真实instruction入口；工具schema启用后首次大样本高于trigger，调整输入尺寸使它明确落在输出预留独有的拒绝区间。生产门未修改，测试失败不是产品修复证明。Sol high只读审查未发现明确生产缺口；自定义HttpBackend子类仍不能凭父类字段假定真实出站行为，本片严格证据限于所测内置协议。

十文件联合 **187 passed（25.52秒）**，日志 `/tmp/decision_output_reserve_joint_20260923.log`。包含新组合、gateway_conversation_compact、runtime_context_pressure、active_turn_compact_projection、三宿主recovery、mixed contract/recovery和responses_backend。随后增强Gateway schema非空及持久失败码断言，14项重新通过；日志 `/tmp/decision_output_reserve_20260923.log`，与187重叠不累加。估算不是供应商精确token保证，也不证明真实缓存命中。

本片12.5本地合同与组合完成，18项大清单仍11/18。12.4全链大来源/覆盖驻留、12.6跨窗口协议故障组合、12.7真实缓存继续开放；原四项主线独占fake Store签名和旧八项全仓失败不在此片修复或抹除。没有真实网络、Gateway重启、部署或远端提交，线上CI未作为验收来源。

建议下一步：继续12.4来源/覆盖内存瓶颈和12.6配置变化组合，然后在已授权独立测试机核验Compact后的实际缓存。只读审查和独立测试可以并行；主线8.1拆分、writer/CAS和共享Gateway保持单owner，不因本片通过提前整枝合并。

本片新测试Ruff、doc sync、strict code-size（hard=0，基线未改）、diff和clean-package通过；只改测试和文档，未新增生产模块/配置。原四项主线fake Store接口失败仍开放，因此不宣称整枝本地严格gate通过。


### 12.6 跨窗口、并发与校准组合（2026-09-23，本地）

解决候选只在短输入、单会话环境验证而遗漏同一Agent共享状态的问题。新增 `test_model_selection_isolation.py` 共12项，复用原Gateway队列、线程CAS、模型目录、发送前复核和调用账；仅HTTP/决策回复为内存替身，无真实网络、部署或Gateway操作。

- 八项双会话组合：250K→1M及1M→250K分别与关闭决策、显式选模、可选准入忙、未知Responses投影交错。一条保留原模型，另一条采用候选；原配置、backend、窗口、请求账和ContextVar隔离。名额3验证正常并发，名额2验证原主模型保留策略，不绕过准入。事件只固定待测交错，并不证明任意竞争顺序都会采用；锁争用时仍允许原保守拒绝。
- 两项近窗口组合：相同76万字符完整系统要求、旧原生tool_use/tool_result及实际工具schema，在原1M模型下运行；250K候选拒绝且业务仍用原模型，1M候选采用。使用真实本地估算与原容量门，输出4096不减、材料不裁剪、未生成Compact覆盖。大材料放系统要求是为了测试实际业务完整输入，并未放进超过决策运输限额的state。
- 两项native/text校准组合：同名同profile更换连接和窗口，活动模型快照继续原参数，新工作片拒绝复用旧校准；同owner另一线程也不继承。旧持久观测保留历史，未把新连接明文写进观测账。usage为合成夹具，只证明失效机制。

六文件联合 **123 passed（24.09秒）**：model_selection_isolation、gateway_model_adoption、subagent_first_request_selection、runtime_context_pressure、request_content_capacity、model_scope_dependencies（均为test_前缀）。日志 `/tmp/decision_selection_matrix_joint_20260923.log`。既有文件同时覆盖未知模态保留、候选探测失败、凭据/目录/期限变化、CAS不确定等；不把它们描述为每项均在同一并发场景下组合。初版夹具分别误触原准入保留、非阻塞写锁竞争和冻结参数赋值，调整测试编排后通过，没有为通过修改产品选模或容量规则。

12.6本地验收完成；12.4全链超大来源/覆盖驻留和12.7真实缓存仍开放，18项大清单11/18不变。真实tokenizer、未知媒体能力与跨模型缓存不能由这些假HTTP推断。原四项主线独占fake Store签名及旧八项失败仍保留，不宣称整枝严格gate通过。

### 消息扫描移植审查反馈与来源兼容修复（2026-09-23）

主线在其独立候选 `ef355f822` 复现扫描A组的两类兼容缺陷：巨大created_at整数的float转换溢出被归成程序错误，以及byte.strip不能识别原读取器接受的Unicode空白。该提交含主线其它工作，未整提交移植。本线同步精确修复并补查后续scoped来源扫描，发现同样空白缺陷。

共享私有解析器捕获OverflowError并报告data_corruption；幂等扫描先检查完整LF，再严格UTF8解码并按str.strip跳空白；来源扫描显式启用相同空白语义，普通分页保留原拒绝空白规则。所有原字节仍计入来源hash，visitor不收到空记录，不改变writer/CAS、权限或持久schema。

兼容红灯3 failed/7 passed（`/tmp/decision_scan_compat_red_20260923.log`），scoped新增红灯2 failed/1 passed（`/tmp/decision_scope_blank_red_20260923.log`）。修复后五文件 **145 passed（1.46秒）**：conversation_message_scan、conversation_message_selection、conversation_store、conversation_message_stream、conversation_history_paging；日志 `/tmp/decision_scan_compat_green_20260923.log`。前述固定A补丁及hash未覆盖本次修复，也未被静默替换；主线已有其A组修复，本线额外scoped修复只在采用该来源选择器时需要。

建议下一步：完成本地守卫并将精确修复交接主线，继续12.4来源内存和12.7真实缓存；已授权独立测试机可承担真实组合，部署前同步使用窗口。独立测试/只读审查可并行，主线拆分、writer/CAS与Gateway保持单owner。

本片全目录Ruff、doc sync、strict code-size（hard=0，原基线未改）、diff及clean-package检查通过。两个定向测试批次互不相同，但不据数量推算完成比例；原四项主线测试适配缺口仍使整枝验收未收口。未推送，线上CI未作为验收来源。


### 12.4 检查点读取释放旧摘要正文（2026-09-23，本地）

解决原 `committed_compact_checkpoint_chain` 经通用JSONL reader把全账本文本、每行文本、所有记录及orphan一并读入，恢复视图又保留全部历史摘要的问题。实现仍读取同一owner账本及原thread head，writer、v3身份封印、scope/base、generation CAS、公开完整chain的tuple形状和旧到新顺序不变。

`compact_checkpoint_scan.py` 在单次同一文件描述符内冻结EOF，完整扫描严格UTF8/JSON对象；只保留ID到字节位置、长度、原行hash的临时地址。重复ID沿原last-wins；Unicode空白、CRLF、合法无LF末行仍接受，损坏的orphan仍拒绝。按head逆向读取时重验原行hash、线程、代次、版本及封印。`resolve_compact_summary_view` 完整消费并检查每个已提交摘要/范围/覆盖，再验证全部base关系，只留一个适用摘要及必要覆盖元数据；不能找到适用摘要就提前返回。公开完整chain接口仍会驻留其返回的全部正文，生产view不走该接口。

边界：晚追加留给下一次读取；文件描述符固定原inode，选中行在首扫后原地改写或截短拒绝。孤儿行首扫后等长改写不会再次全文件hash，本片继续依赖canonical append-only合同，不承诺对任意外部改写提供原子文件快照。索引/精确覆盖仍为O(ID数量及refs)，读取至少容纳最大单行及适用摘要；不是整条Compact常量内存。异常/提前退出关闭描述符，不增加持久索引、后台线程或配置开关。

参考核对：本仓库common/json_io.py的Unicode空白、仅LF合同与对象错误报告；Pi的 `packages/agent/src/harness/session/jsonl-storage.ts` 保留parentId及结构化坏行事实。相邻 `pi_contract_code_files.xlsx` 按jsonl-storage/session-tree查找未命中对应条目，不能据此称索引全面覆盖。Pi仍有整份读取，本片不复制其实现，也未新增依赖。

首批新增13项测试，其中两组内存先红：50条、每摘要131072字符、共约6.5MB账本，旧峰值 **26,349,731 / 26,341,779 bytes**。实现后三文件30项通过，两组峰值 **685,993 / 1,230,734 bytes**（1提交+49orphan / 50提交）。使用tracemalloc只度量读取期Python分配；不是生产RSS、延迟承诺或整个Compact峰值。坏orphan JSON/UTF8/非对象、重复ID最后值、Unicode内部换行字符、空白、CRLF及无LF末行、固定EOF晚追加、原地等长改写/截断、未采用范围的坏summary仍失败及文件关闭均有断言。日志 `/tmp/decision_checkpoint_stream_red_20260923.log`、`/tmp/decision_checkpoint_stream_green_20260923.log`。

九文件联合 **98 passed（10.84秒）**：compact_checkpoint_stream、compact_scoped_checkpoint、compact_scoped_transcript、background_scoped_compact、active_turn_compact_projection、mixed_compact_contract、mixed_compact_recovery、gateway_child_compact_scope_application、compact_output_reserve（均为test_前缀），日志 `/tmp/decision_checkpoint_joint_20260923.log`；30项包含其中，不累加。没有真实供应商、Gateway操作、部署或推送。原四项主线fake Store签名与旧八项失败仍未被本片修复，不称整枝严格gate通过。

建议下一步：继续原生历史投影的重复深拷贝和选中消息正文驻留，再在授权独立测试机验证实际缓存；本片不触碰主线tokens/IR职责。只读审查可并行，writer/CAS、主线文件与Gateway保持单owner。


Sol high独立只读复核未发现必须修复的scope/base/legacy回归，确认未选orphan改写边界；建议的公开完整chain显式closing已补齐。随后新增空head不读取坏orphan、非空head缺文件报缺链两项，并在完整chain关闭修改后复验新文件 **15 passed（0.48秒）**；两组峰值686,105/1,230,902 bytes，日志 `/tmp/decision_checkpoint_final_20260923.log`，与98项有重叠不累加。全目录Ruff、doc sync、strict code-size（hard=0，基线不改）、diff通过；clean-package在纳入新文件后通过。原四项主线测试适配缺口仍开放，不称整枝本地严格gate通过；线上CI未作为证据。


### 12.4 原生历史隔离复制与估算修复复用（2026-09-23，本地）

解决 `canonical_native_messages_from_metadata` 已经复制嵌套content后，`provider_history_messages_from_rows` 和 `conversation_compact_provider_messages` 又重复deepcopy的问题。前者直接转交本次独占的已隔离副本，identified回合仍只输出一次；匿名相同key重复出现时，第二份起仍另复制以保持输出之间不互相修改。Compact复用该独占投影，原 `strip_orphaned_tool_blocks` 纯函数仍负责孤儿结果/缺失结果修补，schema、顺序、内容、权限及canonical写入不变。

独立只读复核确认两条输出分支都产生dict，删除再次过滤不扩大原生消息类型；normalizer仍只认user/assistant顶层角色并允许未来content类型。工具schema冻结的复制不在本片删除范围。不把所有字符串说成重复正文副本：Python deepcopy通常复用不可变字符串，本片节省的是大量嵌套list/dict容器及复制期间memo。

新增六项实际函数测试，首轮2 failed/4 passed：4000块嵌套结果的单次隔离基线3,021,668 bytes，原Compact投影有identity/匿名峰值4,888,715 / 4,880,596 bytes。修复后3,030,107 / 3,021,988 bytes，约接近一次必要复制。验证修改输出不会改canonical或另一调用，修改canonical不会改已返回投影，重复匿名行的两份输出独立，identified回合覆盖可见行一次及孤儿修补逐值相同。tracemalloc只统计读取期Python分配，不是整个Compact峰值、生产RSS或供应商成本证据。日志 `/tmp/decision_native_projection_red_20260923.log`、`/tmp/decision_projection_tokens_green_20260923.log`。

同时复用主线已提交 `b4ffb34755434bd8dffb0ae19b81aebc4bed71f8` 的 `memory_archive/tokens.py` 和 `test_archive_tokens.py` 精确文件差异：`_payload_lengths` 在只读证明内置无环结构的JSON UTF8上界不超过512KiB时调用公开dumps，未知类型/子类/深层/大来源仍流式，不私调编码器或GC。该结构上界不是tokenizer或业务容量门；原数值、sort/default、UTF8/JSON异常顺序和str回退保持。原主线反馈的反复小请求iterencode闭包积累是此片原因；不移入其IR/CAS变更，也不整提交合并。两边唯一实现同步，无新增依赖、持久状态或配置。

三文件（新native测试、archive_tokens、compact_text_source） **72 passed（3.34秒）**；随后11文件组合 **350 passed / 1 failed（23.73秒）**，失败为主线认领 `test_native_tool_use_ir_messages_flow.py::test_native_completed_conversation_precedes_current_user_without_rewriting` 的旧SimpleNamespace缺task_attributes，原loop_support随后还需carried_active_turn_user_inputs。HEAD eb9efc129的函数已直接读取两字段，本片未改该生产入口或IR测试。已向owner发出仅适配该fixture两字段的协调请求，业务断言不改；尚不能称该联合通过。日志 `/tmp/decision_native_projection_joint_20260923.log`。

建议下一步：按所有权协调完成该测试接口适配，再继续选中消息正文的内存生命周期与真实缓存；旧四项后台fake Store及旧八项失败不由本片抹除。只读审查可并行，IR/主线共享入口仍由原owner改动，未触碰Gateway或测试机。


主线owner随后明确授权本线仅在该SimpleNamespace补 `task_attributes={}` 与 `carried_active_turn_user_inputs=[]`，生产IR及业务断言保持。适配后整个IR测试文件 **29 passed（0.38秒）**，日志 `/tmp/decision_native_ir_fixture_20260923.log`；该数与原350有重叠，不累加为379。独立Sol high复核native两函数未发现必须修复的别名或调用者回归。全目录Ruff、doc sync、strict code-size（hard=0，基线不改）及diff通过；clean-package在登记新测试后通过。原四项后台fixture缺口及旧全仓失败仍开放，尚不称整枝严格gate通过，线上CI未作为证据。
