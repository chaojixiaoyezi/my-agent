# P5-D 主会话自动选模型：合同审计与最小实施边界

状态：只读审计完成，尚未实现主会话自动采用。日期：2026-09-22。
工作线：decision-model-plan；审计基线：`ef497a904` 及本工作线当时的未提交变更。
负责人：decision_http_max。只新增本文，不修改生产代码、测试、配置或其他工作线文档。
本文由主线稍后登记到 `CODEBASE_TREE.md`、设计台账和任务导航。

已读 `AGENTS.md`、`LLM_GUIDE.md`、`docs/WORKSTREAMS.md`、决策集成设计、Goal 与原模型选择/预算测试。
所有“当前行为”依据本次实际源码；本文未调用真实模型、未修改任何模型目录、未启动 Gateway。

## 一、结论与当前可交付边界

安全采用点是一个明确的事务顺序：**已取得准确会话执行车道，重读本工作片的权威事实，在第一次模型相关
历史/Compact/工具协议准备之前，只采用一次并冻结完整模型配置。** 随后的请求、工具循环、同片 Compact、
重试和插话都使用这一个模型快照。

当前没有覆盖 Gateway、direct TUI 和后台的现成单一函数可直接插入这个步骤。Gateway 现在先冻结模型、
再等待车道；后台由 scheduler 先领取 claim，再进入模型作用域；direct TUI 直接进入 `agent.run`，不经过
Gateway 的会话车道。不能在 `agent.run` 或 `_render_tool_loop_prompt` 中加一次调用便声称三条入口已统一。

阻止立即开启自动采用的四个事实缺口：

1. `model_selection` 已登记且默认关闭，但没有主会话消费者；候选应沿原已保存/共享的 `agentic` 授权目录解析，不能借用子代理的选择结果。
2. 主会话只有 `model_profile_id`，没有通用的手动选择来源/版本或本工作片采用回执。
3. 完整请求纯 renderer 已存在；新工作片在采用前所需的全部冻结事实尚没有统一、无副作用的收集入口。
4. 原生历史的存储完整性不等于跨模型/跨 provider 的回放兼容性，现有模型目录也未提供完整模态/推理能力事实。

最小可实现切片应先做原线程选择合同与 Gateway 入口顺序，再做只观察，最后在请求面与兼容性均有证据时
开启采用。任一事实未知时保留原合法模型；原模型本身被撤销/删除时继续原明确错误，不能假借“保留原方案”
切到 owner 默认或任意另一候选。

按用户最新要求，本方案只约束时间、调用次数、输入 token 与窗口；不引入价格字段、费用预算或决策计费。
未来本地决策后端仍可复用同一调用/取消/输入统计合同。
决策增强由宿主自动执行：已有设置是范围和资源边界，不需要用户逐个选择候选、逐工作片确认或手工触发测试。

## 二、当前入口和权威事实

以下路径均相对 `agent_py_agent/`，函数名用于准确定位，不表示整份文件均需修改。

| 层次 | 实际文件 / 函数 | 已核验行为及采用含义 |
| --- | --- | --- |
| TUI 显式模型选择 | `cli/chat_parts/tui_model_menu.py::_select_model`、`_request_data_sync` | 只提交结构化 `select` / `set_default`，成功后更新显示；不热改共享 Agent。文案明确“下个工作片生效，当前执行不被中断”。 |
| Gateway 配置认证 | `agent/gateway_parts/model_profile_service.py::handle_client_models` | owner 来自原认证 scope；通过原绑定得到 canonical thread。冷菜单不初始化 owner Agent，不执行模型。不能将自动决策放进读目录或保存配置。 |
| 新普通请求 | `agent/gateway_parts/request_client.py::submit_gateway_ask` | 原队列分配 request ID，携带可信 owner/channel/conversation 事实；没有按用户正文解释模型选择。 |
| 活动轮插话 | `agent/gateway_parts/input_delivery_service.py::bind_gateway_input_active_locked`、`gateway_input_status_payload` | `queued` 与 `active_turn_input` 是结构化区别。插话已经绑定 target turn，不是自动换模型的新工作片。 |
| Gateway 当前冻结 | `agent/gateway_parts/request_execution.py::_run_gateway_ask`、`_run_gateway_ask_with_model` | 先 `preflight_gateway_conversation`，再 `selected_model_scope`，然后才取得 execution lane 和刷新完整 conversation context。排队前 history 不能作为采用事实。 |
| 车道 / 停止 / 恢复 | `agent/gateway_parts/request_binding.py::gateway_conversation_execution_lane`、`GatewayActiveTurnTransition` | 请求绑定先落盘，再领取原 claim；T 锁核对 request、execution attempt、phase、cancel 和 terminal。恢复专属车道由原请求终态释放。 |
| 原会话准备 | `agent/gateway_parts/request_context.py::gateway_conversation_context` | 修复消息、建索引、调用 Compact、读取历史及 Goal/工作区等；不是可反复调用的纯候选预览。 |
| Gateway 同片重试 | `agent/gateway_parts/request_execution.py::_run_gateway_turn_with_conversation_compact` | 最多八次 Compact 尝试保留原请求、工具归档、用户插话和已评估展示；外层同一模型作用域一直有效。不能每次重选。 |
| 后台工作片 | `agent/conversation/background_claim.py::run_claimed`、`run_with_heartbeat` → `runtime.py::_invoke_background_main_agent` | 原 scheduler 先取得 claim、校验取消和任务，再冻结准确 thread 模型；该 claim 与普通前台共用原车道语义，部分 detached task 有独立 claim scope。不能只凭 `thread_id` 推断任意后台片均被同一互斥覆盖。 |
| 后台 Compact / 重投 | `agent/conversation/background_execution.py::run_background_turn_with_compact`；`runtime.py::redeliver_cached_wake` | 同片 Compact 不换模型。冻结交付重投不调用模型，不能顺带触发新决策。后台扩展需单独定义 wake/片身份，首片不宜同时上线。 |
| direct TUI / plain | `cli/chat_parts/tui_worker_local.py::_worker_local_path`、`plain_handlers.py::_plain_local_handle` | 本地控制句柄准入后直接 `agent.run`；历史来自本地注入，不走 Gateway 的新上下文加载。当前这些调用没有保证传入同一 canonical conversation thread。 |
| 原执行总入口 | `agent/agent_core/runtime_mixin.py::SimpleAgentRuntimeMixin.run`、`_run_with_params` | 先进入 `selected_model_scope`，后做 CLI conversation 绑定和 RuntimeDB run/attempt 绑定。更深处才有的身份不能倒填成采用前已存在事实。 |

`request_binding.py::gateway_runtime_authority` 是原 RuntimeDB 身份的传输投影，验证 request 和精确执行代次；
`gateway_request_is_active_turn_recovery` 读取原结构化恢复标记。不能从提示词、错误文案、run 名字或序号判断
是否重选。`agent.run` 内层 scope 发现同一 Agent 已绑定时直接复用，故在 lane 内再嵌一个 scope 不能替换
Gateway 已冻结的旧模型。

## 三、模型选择、授权与手动覆盖

### 已有合同

- `agent/settings/thread_model_selection.py::thread_model_profile_id` 通过原 `threads.update_atomic` 写
  `ConversationThread.model_profile_id`；老线程首次初始化一次，新 owner 默认不追改已打开线程。
- `selected_model_config` / `_resolved_profile` 在原私有目录和管理员明确共享目录解析 `agentic` 引用，
  验证启用、用途和认证。`profile_id` 是身份，显示模型名可能重名，不能反向作唯一权限键。
- 模型更改只清 `provider_context_observation`、`model_context_usage`，保留 summary、消息、Compact cursor、
  generation、checkpoint 和运行任务。状态显示用 `active_thread_model_name` 区分活跃快照与下一片选择。
- `agent/settings/model_scope.py::selected_model_scope` 用 ContextVar 隔离 config/backend/prompts/tools；
  `_profile_backend` 对实际连接与参数摘要缓存，不能仅改 model name 留下另一个模型的 backend 或工具面。
- 当前子代理线已补充：显式同值选择也会在原 thread metadata 中终结其 pending 子代理建议。
  这是 `SUBAGENT_MODEL_ADVICE_KEY` 的既有窄合同，**不是主会话通用选择版本**；本片不能借它存主会话状态。

### P5-D 需要的最小扩展

1. 沿原已保存/明确共享的模型目录解析可访问 `agentic` 候选，原 `model_selection` 模式决定是否自动执行。
   当前该点没有专属 `candidate_profile_ids` 字段，只有子代理点有。若后续需要进一步限制候选，可在原 schema
   增加可选范围过滤，沿原 CAS/继承/投影实现；不能把逐个填写候选 ID 或逐次授权当自动选择的必要流程。
2. 在原 thread 选择合同加入单调选择版本及来源/覆盖事实；显式同 ID 选择也前进版本。不能以
   `updated_at`、当前名称或“原选择 ID 没变”代替此事实。
3. 手动选择至少使全部旧建议无效，并对其覆盖的工作片有优先权。未来新工作片是否自动执行，由原有效设置
   与明确的结构化覆盖策略决定；不额外发明永久 pin，也不要求用户每次重新开启自动模式。
   老线程缺 provenance 时不得推断其过去的手动意图；迁移只建立可验证的新版本，不补造历史事件。
4. 实际采用沿原线程更新保存选中引用、选择版本和准确工作片结果；记录 `applied`、`retained` 或
   `not_evaluated` 等宿主事实，不从 Jev 文案反推。无需第二模型目录、第二会话注册表或独立选择日志库。

Jev 只输出已授权候选引用或 `keep_original / need_data / not_needed / no_match / abstain`。
超时、坏响应、权限变化和配置读取失败须有独立结构化原因，不能都记成“不需要换模型”。

## 四、唯一安全顺序与并发约束

推荐第一片只接 Gateway 普通新请求，保持原 API/调用链：

```text
可信请求与原 thread preflight
→ 原 write-ahead request binding / 领取 execution lane
→ 重读 thread、原手动选择版本、request/attempt 开闭状态及原消息/Compact 版本
→ 原配置明确开启才准备候选和可用的冻结请求事实
→ 沿 decision_service 的同一绝对阶段 deadline 请求一次建议（不持文件锁）
→ 宿主复核授权、完整容量/协议、来源版本、设置及手动选择版本
→ 原线程事务提交本工作片结果并冻结同一完整模型配置
→ 原 conversation context / Compact / 首次请求 / 工具循环 / 同片重试
→ 原终态及用量收口
```

这是对当前 Gateway 层次的必要重排，不是现有行为描述。当前关闭路径在排队前冻结模型；若直接整体把 scope
移动到 lane 后，关闭状态下排队请求也会改为使用后来选择的模型。最小实施须显式保留 off 路径原快照时点，
例如在原时点冻结 off 的配置事实、由同一执行链消费；不要复制第二条执行路径。自动模式在 lane 内采用，
或者经单独合同变更统一两者时点并补测试。不得把这种变化隐藏为内部整理。

车道是原有、带心跳的执行租约，可以覆盖实际模型等待；owner/thread/T 的文件事务锁应短持，禁止持锁发 Jev
或做 probe。`decision_settings.py::_execute_transaction` 的锁序是 **owner → thread**。
不能在已持 thread 锁的更新 callback 中反向调用读取 owner 的服务；`decision_outcome_is_current` 虽非阻塞，
也不能被当成跨文件原子提交接口。最终采用需在同一原 owner → thread 事务中比较已读取事实，再在离开锁前
返回被采用的配置快照，不能提交 ref 后又无条件重新解析另一版配置。

最终提交还须和 `GatewayActiveTurnTransition` 的 stop/closing 语义协调；该 T 锁的嵌套顺序需要实现前与
Gateway owner 精确核对，本文未证明任意新嵌套均安全。获取失败、版本变化或锁忙时结束此次可选采用，
保持当前合法模型，不循环重新决策。配置关闭使待采用结果失效；已发出的普通主模型请求仍按原控制合同执行，
不会因为自动功能关闭而热切换或中断。

不同会话可并发，禁止修改共享部署 `agent.config/backend`。同一会话的两个窗口共用 canonical thread；
后来的显式选择必须赢过较早的决策结果。owner 模型/共享授权/凭据变更既要重新验证引用，还要验证实际连接摘要，
决策设置 revision 本身不足以证明候选连接未变。

## 五、历史、Compact 和 250K / 1M 容量

### 可复用入口及真实边界

| 原入口 | 可以复用 | 不能据此宣称 |
| --- | --- | --- |
| `agent/prompting_parts/builder.py::PromptRenderInput`、`render_prepared_prompt` | 冻结 system/索引/动态段后复用同一字节布局；`PromptBuilder.build` 也调用同一 renderer。 | `prepare_render_input` 的文件/索引收集是纯计算。 |
| `agent/agent_core/tool_request_projection.py::ToolLoopRequestInput`、`project_tool_loop_request` | 完整事实时投影真实 prompt/system/native messages/tools/ToolChoice；缺字段返回 typed unknown，无读盘、probe、消费 mailbox 或网络。 | `ready` 就代表目标模型有足够容量或支持所有历史块。 |
| `agent/agent_core/_tool_loop_service.py::_render_tool_loop_prompt` | 是实际轮次的准备顺序参考；同一 `tool_loop_prompt_request` 消费真实 runtime injections、workspace、execution facts。 | 可在每个候选上调用它做无副作用预览；它依赖当前宿主事实，Goal/执行投影准备仍可能变动状态。 |
| `agent/agent_core/model/context_pressure.py::model_visible_context_snapshot`、`preflight_context_pressure_response` | 原输入估计、schema/IR/history 计入、共享 Compact 阈值、已知出站输出预留及 provider 观测校准。 | 原估计器是目标 provider 精确 tokenizer，或另一模型的校准可继承。 |
| 同文件 `_known_shared_window_output_reserve` | 只有实际 HTTP backend、显式共享窗口和确实发出的正输出 cap 才返回可证预留。OAuth Responses 未发送 cap 时保持未知。 | 返回 0 表示目标模型不需要输出空间。选择器必须把它视为未证明，不伪造安全余量。 |
| `agent/conversation/compact.py::prepare_conversation_context`、`_commit_compact_candidate` | 复用原连续覆盖、checkpoint、expected-generation CAS、取消和失败保护；当前模型下 Compact 完成才推进正式状态。 | 为评估每个候选可分别执行 Compact，或为选小窗口模型直接删除原消息。 |
| `agent/conversation/compact_provider_surface.py::prepare_conversation_compact_provider_surface` | 已沿真实工具/Skill 展示和原历史面准备摘要请求。 | 它是纯准备；内部有 `prepare_for_run` 与原工具协议准备，不应用于候选枚举。 |
| `agent/conversation/compact_request_budget.py::generate_bounded_compact_response` | 摘要请求自身的有界发送和连续分段覆盖，live 摘要也已接通；失败/停止不提前删原 IR。 | 摘要一定能压至任意候选窗口，或分段成功等于目标普通请求必定能发送。 |

完整首请求至少需要：准确 user/system/PromptRenderInput、本会话 summary 与消息覆盖版本、canonical provider
history、同片 IR、未转发 runtime guidance、conversation state、冻结 ToolProtocolSnapshot、真实 native schemas、
原 ToolChoice、工具/Skill 展示投影、工作区/权限事实，以及该候选实际出站输出上限。候选窗口和正文长度两项远远不够。
父片/另一个模型的 ToolRuntimeSnapshot、空历史或手工补字段的测试对象不能替代这些事实。

现有 `_projected_context_tokens` 仍构建基础 prompt，并取 legacy/native 历史估算较大值；未将整个
`model_surface`、系统指令与最终原生 schema 全量接入该估算。真正发送前的完整预检仍有必要；不能以这份
transcript 投影单独为跨窗口选择背书。

输入与输出容量分开：采用原估计器和已知共享窗口 cap，至少满足 `input < window - sent_output_cap`，并遵守
本次是否允许持久 Compact 的原阈值。未来输出不计入当前 Context 输入显示或决策输入使用量。
250K 候选可以胜任实际输入较小的工作，不应永久设置“候选窗口至少等于当前模型窗口”；实际 400K 输入则不能
仅因模型语义评分较高就给 250K。1M 的配置也不能消除工具 schema、历史、图片或推理预留的未知项。

初版采用建议只接受**无需为换模型额外 Compact、且当前完整输入可证明容纳**的候选。这是初版范围，不能写成
永久只升不降门槛。后续要支持“先按原合同压缩、再换更小窗口”，须显式设计 Compact 后输入版本与采用的事务
边界；压缩失败、覆盖缺失或取消时不改变模型、不提交候选摘要、不删除原历史。

### 跨模型历史与校准

- `agent/conversation/native_history.py::provider_history_messages_from_rows` 用原结构化消息关联去重，保留开放
  content blocks；`_normalized_native_messages` 仅规范顶层 role/content，不验证目标协议。
- `agent/backends/responses_wire.py::message_items` 只向相同 model 回放 `responses_reasoning` 密文；原测试明确
  model-a 的密文不会发送给 model-b。不能为“完整历史”破坏这个安全合同，也不能把未回放密文说成已兼容保留。
- `agent/backends/openai_chat.py::_openai_assistant_messages` 转换 text/thinking/tool_use，
  `_openai_user_messages` 转换 text/tool_result；这不是通用 image/未知块支持声明。Anthropic thinking signature、
  redacted thinking、Responses 密文与模态能力都需要真实结构化兼容事实，不能从模型名字推断。
- 本次只证明这些适配器有各自的现存回放规则，没有证明任意已保存历史可无损切到任意模型。应在原适配层提供
  可复用的“可回放/未知/不可兼容”事实，未知保留原模型；不要复制第二份转换器，更不能为通过预算静默抹掉思考/图片。
- 主线已实现 `context_pressure` 复用 `decision_policy.connection_revision` 的进程盐 HMAC，连接/凭据轮换会
  使旧校准不再匹配；Compact 代次/稳定请求面也必须匹配。该摘要是进程内版本比较，不是跨重启稳定凭据版本，
  禁止把它当成持久恢复证书。当前生成 profile schema 没有通用、可跨重启验证的配置 revision。

## 六、崩溃、恢复和可选调用账

1. 决策绑定使用已有可信 thread 与 request/工作片 identity；真实 run/attempt 尚未建立就保持缺失，不创建
   第二套假 run。`decision_service._identity` 允许 thread 范围在无 run 时使用宿主 thread，Gateway request ID
   可以关联原调用账；最终主执行仍由 RuntimeDB 绑定真实 run/attempt。
2. “一次选择”按原稳定 operation/request 定义，不按易旋转的传输 attempt 或 Compact 循环计数。
   `gateway_request_is_active_turn_recovery` 已识别同回合恢复；该回合应复用已提交结果，不重选、不重复调用 Jev。
3. 原 request binding 和 thread 选择目前分属不同持久写入，没有已实现的原子模型采用事务。实现必须选择一个
   canonical receipt 位置，并用原 write-ahead / 原恢复扫描消化中间态；不要同时在两个文件各保存一份选择权威。
   thread 仍是有效模型引用权威，request/attempt 上只存指向它的准确采用/观察事实。
4. 必测崩溃点：建议返回前、返回后提交前、thread 提交后 scope 进入前、Compact 前后、首个 provider 提交后。
   无法证明是否已采用时不得重新掷一次模型选择；应保留原明确恢复结果或报告恢复所缺事实。
5. 同 ID profile 的编辑/撤销也会影响重启。现有进程内配置快照不能跨崩溃复活，且不能持久复制秘密作为恢复方案。
   若要证明恢复使用原连接版本，需要在原模型目录增加正式版本合同或明确让已变化连接的旧自动建议失效；
   这是待协调事项，不能仅存 HMAC 后宣称已解决。
6. 复用 `begin_decision_stage → decide → decision_outcome_is_current`，准备前开始一个 absolute monotonic
   deadline；off 在候选/后端准备前短路，observe 只记录建议。用户取消传播，不吞成普通增强失败。
7. 复用 `invoke_decision_model_call` 的 optional bounded worker、准确 interrupt handle、原 admission、HTTP
   observer 和 `purpose=decision` 原账本。超时 caller 返回后 worker 仍占资源，不能再开同资源重试；不建第二
   executor、账本或计数器。
8. 原 `model_call_summary` 优先按 request ID 汇总，finalizer 可收口同 request 的前置决策输入；但若决策后
   在进入 `agent.run` 前失败，普通 finalizer 尚未执行，必须沿原 request 终态/既有 snapshot 结算入口覆盖此分支。
   当前 `decision_model_call` 只刷新原内存账和指标，本身不持久化整个 scope；不能把 pending 主调用快照抢先结算。
   重复收口沿原 `append_snapshot_once` 的范围/摘要幂等合同。
9. 展示只需成功/失败/超时、逻辑调用数/物理 HTTP 尝试、耗时、输入 token 的已报告/估算/未知事实。
   缺 usage 不是 provider 报告 0；不新增价格表、费用推断或货币预算。

## 七、最小实施分片与文件归属建议

此表是待认领清单，不表示本文已获源码修改权；涉及并行线的文件先交主线分配。

| 阶段 | 最小职责与建议文件 | 验收出口 |
| --- | --- | --- |
| A：原选择合同 | `settings/thread_model_selection.py`；必要的 `conversation/models.py/store_threads.py`；仅必要时扩展原设置 schema/default/config/YAML/TUI 投影 | 手动选择版本/来源、同值失效、原目录候选边界、旧数据迁移、原 CAS；off 无额外请求，无逐片确认。 |
| B：工作片边界与观察 | `gateway_parts/request_execution.py/request_binding.py`；必要拆出当前 `request_context.py` 的只读事实收集；一个窄主会话 decision 适配模块 | 获取准确车道后只决策一次；请求/手动/配置版本固定；observe 不改模型/history；queue、插话、恢复与同片 Compact 不重选。 |
| C：完整请求与采用 | 原 `tool_request_projection.py` / PromptBuilder 准备输入；`context_pressure` 和原 backend adapter 的可复用容量/回放事实；原 `model_scope.py` 接受被核对的完整快照 | 同输入投影与实际出站一致；未知不采用；250K/1M 逐候选 cap；合法采用只改原 thread 模型和准确回执，当前片配置不可变。 |
| D：持久恢复与其他入口 | 原 Gateway 恢复/终态收口、后台 `runtime/background_claim`；direct 入口另行核对 canonical 绑定 | 精确崩溃矩阵、无重复决策、失败前置输入统计；后台 wake 及本地入口有同等身份/历史证据后再开放。 |

阶段 A 的 schema/迁移测试可以与阶段 C 的纯请求及回放只读/合同测试并行。Gateway 边界重排、最终采用和
恢复最好由同一 owner 串行实施，避免两个 agent 同时改变 scope、lane 与 provider 提交顺序。
不要直接复用子代理的 pending metadata 名称、父工具快照或创建 ID；主会话没有子代理创建事务。

## 八、验证证据与尚需补的测试

本次仅运行已有定向测试，未修改测试文件：

```bash
python3 -m pytest agent_py_agent/tests/test_thread_model_selection.py agent_py_agent/tests/test_gateway_model_profiles.py agent_py_agent/tests/test_runtime_context_pressure.py agent_py_agent/tests/test_responses_backend.py agent_py_agent/tests/test_tool_request_projection.py -q --tb=short -o addopts=
```

结果：**75 passed in 2.52s**。证明现有模型 scope/可信 owner、手动选择保留历史、纯投影、输出 cap 和连接校准
合同；不证明 P5-D 已有生产消费者，也不证明真实 250K/1M 输入或跨模型业务质量。

实施时应增加如下确定性事件/fake backend/replay 测试，不先依赖收费真实模型：

| 测试组 | 必须核对的事实 |
| --- | --- |
| 默认关闭、观察、缺配置 | 候选目录/后端/网络零额外调用；原输出及冻结时点保持；observe 对 thread 模型和历史字节零修改。 |
| 两窗口与取消竞态 | Jev 等待时显式改 B、同值重选 A、关模式、删候选、撤共享、换凭据；迟到结果不覆盖手动、不使用撤销候选；停止不发主请求。 |
| 真正 lane 时点 | 用 Event 卡住前片，后片排队期间追加真实 transcript/Compact/手动选择；采用依据取得 lane 后准确版本；同片主模型从头到尾不变。 |
| 真实出站一致性 | 从同一冻结输入捕获 system/stable prefix/dynamic sections/native messages/schema/ToolChoice，比较纯投影；缺事实 typed unknown 且无 probe/写盘/消费 mailbox。 |
| 250K / 1M | 同一完整输入在两窗口、不同真实输出 cap 下分别判定；边界前/等号/超限、超大 schema/工具结果/参数、summary、推理块和图片未知；不设只升不降门。 |
| Compact | 原 checkpoint/CAS/连续覆盖；分段或最终失败、取消和 generation 变化时不提交选中模型/不删历史；同片重试只决策一次且保持展示/拒绝记忆。 |
| 历史协议 | Responses 同模型/异模型密文、Anthropic 签名/遮蔽思考、Chat reasoning、工具配对、image/未知块；不得靠删除未知内容让候选“通过”。 |
| 恢复与账本 | 上节五个崩溃点；恢复无再次选择、无双 provider 提交；前置决策后主执行未开始也记录输入用量，重复终态不重复累计。 |
| 权限和范围 | 同 owner 不同 thread、跨 owner/shared、foreground 与 background、direct 未绑定 thread；不从正文或显示名称生成授权。 |

可扩展现有 `test_thread_model_selection.py`、`test_gateway_model_profiles.py`、`test_runtime_context_pressure.py`、
`test_responses_backend.py`、`test_tool_request_projection.py`；Gateway 原 Compact/恢复及 decision service 测试
按具体认领函数选取。本文未运行全仓 pytest、远端 gate 或真实模型验收。

## 交接与建议下一步

本次唯一改动是本文；无配置、代码、模型目录、Gateway 或生产行为变化。主线应先固定手动覆盖的结构化范围、
原目录候选复核及工作片恢复记录的归属，再认领阶段 A/B。建议优先用确定性竞态测试锁定 lane、手动覆盖和
旧建议失效，然后再实现自动采用；完整纯请求/历史兼容事实可与该合同片并行。

主线须将本文登记到共享树/设计导航，并保留 P5-D“未实现采用”的状态。风险边界是原请求身份、不可变在途模型、
手动优先、原历史/Compact 完整性，以及未知容量保留原合法模型；不能为赶进度让语义建议直接改变运行 backend。
