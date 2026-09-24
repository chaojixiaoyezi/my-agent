# P5-E1 授权与输入预算原语交接

- 日期：2026-09-22；分支：`codex/decision-model-integration`。
- 范围：主线授权的共享 worktree 内，原 decision settings、ModelCallLedger、决策服务、user_config、TUI 能力字段及定向测试。
- 状态：**本地授权保存/撤销与预留原语已实现；完整 E1 和联网实验未实现、未验收。** 2026-09-24 第二片（见文末）实现 `/experiment` 授权入口、经验输入上界与发送硬门，已合入 main `dfa8e498b`，2026-09-25 两次真实授权发送已完成（见第二片末节）。2026-09-25 第三片（见文末）在本地实现 E2 对照记录、F1 只读证据评估与 `/experiment apply` 授权内自动晋升，待审；真实 apply 验收未做。
- 没有启动或修改 Gateway，没有请求真实 Jev/MiniMax，没有提交、推送、部署或操作日常用户数据。

## 解决问题与真实边界

接入点开启、工具执行权限和模型建议都不能自行变成额外自测许可；事后统计 token 也不能阻止下一次请求超额。
本片先把有限许可保存在原设置，将串行预留放在原模型账的同一把锁和累计容器内，并关闭尚未具备条件的实验发送路径。
不计算输出 token、缓存折扣、价格、USD 或费用。

`HostCommandIdentity` 的类型、owner/thread 匹配只证明字段形状和归属，**不证明用户明确确认、原 operation 已持久提交或已获执行权**。
`authorize_decision_experiment` 是留给未来宿主显式用户控制入口的保存原语；调用者必须先完成原用户授权与操作幂等链。
本片没有添加产品授权菜单、宿主命令或模型建立许可入口；测试中的宿主来源是夹具，不能称为产品授权闭环验收。
普通 `user_config` 只读/撤销许可；即使它能开启 `experiment_enabled`，也不能发放许可或重置预算。
TUI 仅补齐能力字段的中文布尔编辑和恢复继承，明确展示“这里只开启能力，不建立实验许可；当前联网实验不可用”。

`reserve_input_budget` 接收的整数是调用者声明的完整输入上界，**该整数本身不是 tokenizer、服务端额度或实际载荷证明**。
当前 Jev UTF-8 窗口门和 `estimate_tokens` 均未被升级为 proof；原 HTTP observer 会吞观察异常，不能当发送硬门。
因此 `begin_decision_stage(experiment=True)` 不发布可准备点，`decide` 复读后返回不可用；直接实验模型调用也在估算/建账前拒绝。
保存信封中的 `active` 不代表具有实际联网资格。不得把这片记为“已经具备实际输入 token 硬预算”。

## 实际实现

1. 原 `decision_settings.v1` 显式只读迁移到 v2，保留 revision 和全部 overrides，只增加空 `experiment_authorization`。
   v2 必须有完整信封字段，坏结构和未知版本不被当成无授权。实际写入仍沿 owner→thread 锁与完整 CAS。
2. `decision_experiment_enabled` 在原 YAML/dataclass 默认关闭，经原 defaults 映射生效。开启不授权。
   宿主授权只支持单一 thread、task、run、attempt、request 与同一本 ModelCallLedger 代次，只允许 `observe`。
3. 授权保存完整来源、允许接入点、固定期限、HTTP 上限及完整输入 token 上限；不接受零、布尔、无限或缺项预算。
   任何后续设置 revision 变化使旧许可失效，包括同值修改；普通 patch/reset/restore 不接触授权字段。
4. 读取只展示当前 thread 的整份信封；不把 owner/thread 的半份许可合并。owner 层授权不受支持。
   原 `user_config` 增 `decision_experiment_revoke`，仍走原工具权限、串行策略和操作账，提交准确许可 ID 及完整 CAS。
5. 原 ModelCallLedger 增实例代次及显式输入预留方法，状态保存在原 `_scope_aggregates`，不另建数据库或余额文件。
   检查与预留同锁，第一片一个预算同时只允许一个待结算 call；普通 started/finished/HTTP 观察不自动接预算。
6. 每次预留保守占用一次 HTTP 及完整输入上界。只有原成功记录有一次 HTTP 且有 provider 完整输入来源时结算差值。
   输出与缓存命中不抵扣输入。失败、超时、缺 usage、无 HTTP 证明或 HTTP 次数不符保留占用并关闭后续预留。
   已声明上界被 provider 事实突破会显示真实输入并关闭预算；这种失败不证明原上界正确。
7. 授权到期前原累计容器额外保留预算（包括撤销态），不挤占普通 scope 的原 LRU 配额；无预算时不新增时钟读取。
   避免 LRU 裁剪后重新给额度；同编号/相同声明只读返回，不能重置。
   新账代次不能接旧许可；缺账只失败，不从信封恢复余额。没有跨进程恢复能力或物理清理时限承诺。

## 文件归属

| 文件 | 本片内容 |
| --- | --- |
| `agent/settings/decision_experiment_schema.py`、`decision_experiment.py` | 新授权结构及宿主保存/撤销原语 |
| `agent/settings/decision_settings_schema.py`、`decision_settings.py`、`decision_settings_projection.py` | v2 迁移、原 CAS 事务及整份只读投影 |
| `agent/settings/config.py`、`config/agent_config.yaml` | 独立能力开关默认值及中文说明；defaults 继续使用原字段映射 |
| `agent/contracts/model_call_budget.py`、`model_call_ledger.py` | 原累计容器中的原子预留、保守结算和代次拒绝 |
| `agent/conversation/decision_experiment.py`、`decision_service.py`、`decision_model_call.py` | 输入准备前/实际调用前失败关闭，不接实验网络 |
| `agent/tooling/user_config_tool.py` | 只读/撤销，拒绝模型建立许可 |
| `cli/chat_parts/tui_decision_menu.py`、`tests/test_tui_decision_menu.py` | 能力布尔值中文编辑、明确提示及恢复继承，不提供授权入口 |
| `tests/test_decision_experiment_authorization.py`、`test_model_call_input_budget.py` | 新原语、并发和失败关闭测试 |
| `tests/test_decision_model_profiles.py`、`test_decision_skill_tool_settings.py` | 原 v1 数据显式迁移至 v2 的断言；同步新 TUI 字段后的菜单位置 |
| `tests/test_external_material_order_integration.py` | 仅同步新增能力字段后的 TUI 接入点菜单位置，不改生产逻辑 |

以上路径除文档外均相对 `agent_py_agent/`。主线统一更新 CODEBASE_TREE、DESIGN_LEDGER、TESTS、Goal 和其余共享导航。

## 本地验证

新 E1 定向测试 **52 passed**：默认关闭不准备/不建实验账、一般开关不授权、来源/身份范围、CAS 并发唯一赢家、
模型无法授权、原工具拒绝与撤销、同值后改、到期、不同点/任务/账代次隔离、整数上界拒绝、串行预留、HTTP/input 临界值、
provider 真零与缺报、可能已发送超时、不退未知占用、同编号重开/裁剪恢复拒绝、上界被突破的真实状态、普通累计 LRU 不被预算挤占。
最终新旧设置/决策调用/原模型账/工具/TUI 组合 13 文件 **347 passed**（含以上 52 项和 16 项 TUI）；
额外复跑 `test_external_material_order_integration.py` **9 passed**，共 **356 passed**。
首次组合回归发现一处旧 TUI pipe 测试菜单位置未同步，修正后组合重跑全绿；不存在未处理的失败项。
这些是临时原本地存储、原工具执行器与纯账本夹具，不证明真实模型质量或可靠 tokenizer/input proof。

`ruff check agent_py_agent scripts`、`python3 scripts/check_doc_sync.py` 与 `git diff --check` 均通过。
严格 AST/code-size 使用原 `scripts/check_code_size.py` main、`--mode strict --baseline CODE_SIZE_BASELINE.json`，
只将报告路径改到临时目录并随后删除，避免覆盖主线共享 CODE_SIZE_REPORT；结果 `blocked=False`，基线未修改。
没有推送，线上 CI 不作为本片验收来源。

## 已读成熟参考

- 本地 `codex_contract_code_files.xlsx`：相关命中只有 `goal_validation.rs`，索引未覆盖本次全部运行接缝。
- 本地 `codex-main` HEAD `578c1b22`：`ext/goal/src/accounting.rs` 的 accounting permit、固定快照和提交后推进游标；
  `app-server/src/config_manager_service.rs` 的 batch_write、expected_version 和受管理字段边界。
- 本仓库原 `llm_scale/token_budget.py` / `admission.py` 的预扣模式，以及 ModelCallLedger、HostCommand、ToolExecutor 的原身份/操作边界。
  没有另实例化其估算 TokenBudget 当作实验账，也没有借参考项目宣称得到 provider-input 硬证明。

## 尚未完成与建议下一步

### 后续只读审计：真实输入上界与发送接缝

官方 [Jev 模型说明](https://docs.typesafe.ai/models)列出整请求 64k、`state + 最长题` 32k 的上下文限制；当前公开说明没有明确保证这就是包含服务端包装与错误路径的 `usage.input_tokens` 上限。官方 [API 示例](https://docs.typesafe.ai/api)的完整请求按当前紧凑编码为 164 UTF-8 字节、按本项目实际 `json.dumps` 为 173 字节，而示例回执报告输入 296 token。因此请求字节数或任意固定小余量不能充当真实输入的硬上界。公开 [SDK 请求字段](https://docs.typesafe.ai/sdk/javascript/api/interfaces/SystemOneRequestPayload)也未给出可核验的输入计数接口或请求级 `max_input_tokens` 硬参数。这是公开合同审计，不是新发的真实请求，也不证明供应商永远不会提供其他合同。

最小后续路径：优先取得绑定固定 endpoint、模型版本和计数口径的供应商完整请求输入上限 `C`，每次在原账预留整个 `C`，实际成功后仅凭 provider 输入回执结算；拿不到可靠 `C` 就保持 Jev 实验关闭。发送许可须绑定最终 `GatewayRequest` 的 `req.data` 摘要、端点、模型及连接代次，并在实际 `_gateway_request_attempt` 发出请求字节之前检查。现有 `_emit_provider_attempt` 会吞观察异常，只能做遥测，不能改作硬门；`decision_json` 的紧凑正文也不是最终 wire 编码。

授权需要原 HostCommand 的明确用户动作、冻结参数、成功 operation 回执和当前业务 run/attempt/ledger 的同一绑定。单独的 `HostCommandIdentity` 不能证明用户确认；`/client/models` 的冷配置 host 没有当前业务账，不能在它上面发许可。消费前还须复核原运行执行权、设置 revision、撤销/期限及预算同锁预留。现有 `_started_params` 缺 owner/attempt 字段，且不能先 `started_retained` 再调用 `reserve_input_budget` 建同一个 call；接线时须用一次预留取得原 call 与 retention。排队及连接期间撤销的边界、可能已发送后的未知占用都要在原传输层验明。本轮没有实现这些动作，实验联网仍失败关闭。

完整 E1 仍须先闭合：宿主明确用户授权及原 operation 提交/幂等证据、完整实际载荷可靠输入上界、每次实际传输前硬门、
原运行执行权/撤销在发送边界的联合检查、跨进程预算持久化或明确拒绝恢复的端到端证据。
未知占用暂不释放；未来只有原传输可证明未发送的结构化事实才能增加释放动作，不能仅凭未收到 HTTP 观察就退款。

E2 同样本基线/候选对照、F 可信效果指标与自动应用、G 自动收尾/精确恢复策略、H 组合与真实收益验收均未实现。
已有 G0 settings restore 只是原语，不是 G 完成。一次真正有界用户授权后，范围内动作应自动执行，不新增逐子代理或逐步骤确认。

**建议下一步**：先由一个 owner 设计并证明宿主授权来源和实际发送硬门，再选择有可靠输入上界的后端；期间保留实验联网关闭。
其他 agent 可并行只读审查 tokenizer/服务端硬额度与原 operation 证据，但不要同时修改 settings、ModelCallLedger 或传输入口。

## 第二片：`/experiment` 授权入口、经验输入上界与发送硬门（2026-09-24）

- 分支：`claude/decision-experiment-send-gate`，基于 origin/main `55f72b40c`，单个本地提交，未推送、未部署。
- 状态：**已实施，本地定向与严格 gate 通过，待审**。`experiment_enabled` 默认仍关闭；首次真实授权发送尚未进行。
- 用户于 2026-09-24 批准接受**经验（非供应商保证）**的输入上界，要求处处标注 `empirical`。上一片“拿不到可靠 C 就保持关闭”的前提由这一批准替换为“只在已标定范围内使用经验 C，越界拒绝”。
- 没有启动 Gateway、没有请求真实 Jev/MiniMax、没有 ssh；测试只连本机临时 HTTP 服务。

### 授权入口

- `/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>` 与 `/audit 名称 prepare` 共用 `ConversationTaskCommand`：入口生成 `system_task={"kind":"decision_experiment","attributes":{mode,point,duration_seconds,max_http_requests,max_input_tokens}}` 冻结进排队请求，模型只收到任务正文。HTTP `/ask` 原本就丢弃客户端 `system_task`，现只从已鉴权正文重推；非法格式在入口返回用法、不入队。只开放 `observe` 和已标定的 `skill_tool`，时长单位 `s/m/h/d`。
- 授权时点：`_run_with_params` 在 `_bind_main_agent_turn_params` 发布 run/attempt 后、首个 `_run_once_with_params` 前调用绑定回调的 `grant_decision_experiment`；目前只有 Gateway 的 `GatewayTaskBindingWriter` 提供（新方法，委托新模块 `gateway_parts/request_experiment.py`，`request_binding.py` 未改已有行）。
- 同一 `GatewayActiveTurnTransition`（phase `experiment_grant`，要求回合 open、未取消、执行代次一致）内：先以原 JSON 原子写 `experiment_grant.status=granting`（已有任何回执即放弃），再以 `HostCommandIdentity(owner, 请求 user_id, metadata.channel, thread, request_id)` 调用 `authorize_decision_experiment`，写回 `granted(authorization_id)` 或 `rejected(code)`，随后经本轮 `on_chunk` 发一行提示。拒绝码只按异常类型给出：`experiment_disabled`、`source_identity_invalid`、`settings_conflict`、`access_denied`、`invalid_identity`、`invalid_request` 或预算原因。失败不阻断业务回合。
- 重放不再授权（回执已存在，含崩溃遗留的 granting）；Compact 再入换 attempt，旧授权得 `experiment_identity_changed`；重启换账本代次得 `experiment_ledger_changed`。模型仍只有 `user_config` 的读/撤销。
- 信封升级 `decision_experiment_authorization.v2`，必带 `input_bound_policy="empirical:jev_wire_bytes.v1"`；原语新增必填参数且只接受该值。v1 与未知口径可读、可撤销，准入分别返回 `input_bound_policy_missing` / `input_bound_policy_unsupported`，永不发送。
- 未晋升会话任务的主轮：授权、准入与预留的 task 身份都按 RuntimeDB 同一规则投影为 run（`experiment_task_id`）。

### 经验输入上界 C 与原账

- `backends/typesafe_decision_wire.py::jev_empirical_input_bound(body, payload, point=)`，方法 `jev_wire_bytes.v1`：B=最终 wire 正文字节，Q=题数，S=state 用同一编码器的字节；`C = ceil(B/2) + 256×Q + 1024`。只在 `skill_tool`、Q≤64、S≤4096、C≤57,600 内有效，否则 `input_bound_out_of_calibration`，不预留、不发送。常量是版本化代码方法，不是配置。
- 标定事实：4 次真实 64,921–65,063 字节/27 题请求计费 17,352–17,383 输入（C=40,397–40,468，约 2.3 倍余量）；官方示例 173 字节/1 题计费 296（C=1,367）。测试夹具的 60 个中文 Skill 完整请求约 119,585 字节/62 题，C≈76.7k 超出标定，被作为真实越界样本拒绝（中文经 `\uXXXX` 转义约 6 字节/字）。
- 唯一编码器 `gateway_helpers.gateway_request_body()` 取代 `_urllib_request` 内联的 `json.dumps(payload).encode("utf-8")`，普通请求字节逐字节不变。Jev 后端拆为无网络 `prepare()`（期限、载荷、窗口门、最终字节）与必须携带许可的 `send()`；普通 `decide()` = prepare + 原发送。
- `InputTokenBound(tokens, kind="empirical", method, body_bytes, questions, state_bytes)`；`reserve_input_budget(params, budget_id=, input_bound=, send_binding=)` 只接受该对象（裸整数 `input_bound_unlabeled`），同时签发 `send_permit=issued` 绑定（方法、端点摘要、正文 sha256、模型、连接版本）。调用 metadata 记 `input_bound`，快照记 `input_bound_kind`；调用的 `input_tokens` 仍是原估算。

### 发送硬门

- `GatewayRequest.send_permit`（默认 None，不进 repr/比较）；有许可时 `validate_request_limits` 要求 `max_retries==0`、`allow_redirects=False`、有 deadline 且许可可调用 `admit`。
- `_gateway_request_attempt` 在 `req=_urllib_request(request)` 与期限复核之后、`started` 遥测与 `_gateway_urlopen`（任何 DNS/连接）之前调用 `permit.admit(ProviderSendAttempt(method, req.full_url, sha256(req.data), model, attempt))`，不包 try/except。拒绝抛 `backends/provider_send_gate.py::ProviderSendRefused(RuntimeError)`，不属于 `post_json` 会重新包装的异常族。
- `conversation/decision_send_permit.py::DecisionSendPermit.admit` 顺序：静态绑定（POST、端点、正文摘要、模型、attempt==0）→ 运行态（用户中断仍抛 `InterruptedError`；设置撤销句柄 `settings_changed`；期限 `deadline_exhausted`）→ 在发送线程恢复调用线程的 runner 身份后非阻塞复读设置，经 `experiment_current` 重跑 `experiment_admission`（状态、期限、身份、revision、账本代次、v2 口径）、要求点普通模式仍为 off、比较策略版本与当前 profile 的 `connection_revision`（不复用把 off 当关闭的 `_stale`）→ 原账锁内 `consume_send_permit`（预留仍属本调用且未终结、预算开放未到期、零 HTTP、绑定逐项相等，issued→consumed 单次）。
- `decision_service` 以 `_route` 区分普通/实验路由，`_invoke` 的失败分类拆到 `_failure_outcome`：`ProviderSendRefused` → `send_refused:<code>`，预算/上界错误 → 原 reason，均不调用 `record_failure`。实验结果 mode 固定 `observe`、`may_apply=False`。

### 结算

- `invoke_decision_model_call` 在终态写入后的 finally 调 `settle_input_budget`：成功（finished、许可已消费、恰好一次 HTTP、provider 报告 input）按实际扣减、释放 C−actual 并记 `input_bound_ratio`，>0.8 只记 `input_bound_ratio_high` 警告，actual>C 沿 E1 关闭为 `input_bound_violated`；许可仍 issued 且零 HTTP 记 `refused_before_send` 并以 `send_refused` 关闭，不退款；有 HTTP 却无已消费许可记 `gate_bypassed` 并关闭；其余（消费后超时/取消/失败、缺 usage、HTTP≠1）保留一次 HTTP 与整个 C、`unknown_usage_calls+1`，以 `usage_unknown` 关闭。迟到 HTTP 只追加，不重开。预留失败或上界越界不产生调用记录。
- 结算关闭原因覆盖 `active` 与 `revoked`：撤销后被拒的那次显示 `send_refused`，授权信封本身仍是 `revoked`。成功路径不改状态，撤销仍可见。

### 消费者与观测

- `recommend_capabilities` 只在普通阶段无错、`skill_tool` 普通模式为 off、`experiment_available`（普通阶段同次读取得到的零 I/O 提示：能力开或本线程有信封）且无携带展示时进入 `_experiment_observe`；默认关闭不增加读取。`begin_decision_stage(experiment=True)` 只发布普通模式为 off 的授权点。
- 实验只写 `skill_tool_decision:experiment:<reason|status>` finding，原快照原样返回；rebase 后与主线新增的能力推荐观测合同对齐：真正发起过实验决策时同样附 `observation`（mode=observe、adopted=false、retain_reason=结果码），由宿主写入请求记录。

### 文件归属

| 文件 | 本片内容 |
| --- | --- |
| `agent/conversation/control_commands.py`、`agent/command_catalog.py` | `/experiment` 解析、冻结参数严格校验、公共目录条目 |
| `agent/gateway_parts/request_experiment.py`（新）、`request_binding.py`（仅新方法） | 单次授权、回执与提示 |
| `agent/agent_core/runtime_mixin.py` | 绑定后、首个模型调用前的授权钩子 |
| `agent/settings/decision_experiment_schema.py`、`decision_experiment.py` | v2 信封、口径参数、task 投影 |
| `agent/conversation/decision_experiment.py` | 口径准入、实验点发布、实验路由与在途复核 |
| `agent/conversation/decision_send_permit.py`（新）、`agent/backends/provider_send_gate.py`（新） | 发送许可与传输层拒绝异常 |
| `agent/conversation/decision_model_call.py`、`decision_service.py`、`agent/capability/decision_recommendation.py` | 实验调用、映射与只观察消费者 |
| `agent/backends/gateway_helpers.py`、`gateway_request_limits.py`、`typesafe_decision.py`、`typesafe_decision_wire.py` | 唯一编码器、硬门位置、许可信封约束、prepare/send、经验上界 |
| `agent/contracts/model_call_budget.py` | 带标签上界、发送绑定、单次消费与结算 |
| `tests/test_decision_experiment_send_gate.py`（新）、`test_decision_experiment_command.py`（新）及四个扩展测试文件 | 见下 |

以上路径均相对 `agent_py_agent/`。

### 本地验证

- 新增/扩展五文件从干净字节码（`PYTHONDONTWRITEBYTECODE=1`、删除全部 `__pycache__`）200 passed：`test_decision_experiment_send_gate.py` 39（统计 TCP accept：9 类缺授权/缺预算零连接、5 类预留后绕过零连接且预算 `send_refused`、抛错遥测不改变门、默认关闭不建实验阶段零连接、授权单次发送结算与只观察的推荐观测、X=C+1 关闭、挂起超时保留整个 C 且迟到不重开、路由变化零连接、许可静态绑定与运行态顺序、C 的样本余量/单调/越界/边界）、`test_decision_experiment_command.py` 34、`test_model_call_input_budget.py` 38、`test_decision_experiment_authorization.py` 35、`test_gateway_strict_request.py` 54。
- 定向相关集（grep 触及传输、GatewayRequest 限制、决策服务/调用/实验、预算/账本、handle_ask 命令解析、Audit 准备、命令目录、request_binding、runtime_mixin 的测试文件）118 文件从干净字节码 3421 passed、2 skipped、4 xfailed、1 xpassed（基于 `55f72b40c`）。
- 变异 36 项全部被杀（首轮 33/34，存活的“许可忽略设置撤销句柄”因设置复读同样拒绝而无可观察差异，已补运行态顺序单测后被杀；rebase 接入能力推荐观测后补 2 项消费者变异，并为“忽略零 I/O 提示”补默认关闭单测）；每项在子进程 `PYTHONDONTWRITEBYTECODE=1` 下运行并按 sha256 原样恢复，结束后删除字节码重跑。清单：去掉硬门、吞掉拒绝、硬门移到遥测之后、去掉许可信封约束、改编码器、去掉正文/端点/重试静态检查、去掉设置复读、忽略撤销句柄、去掉连接版本比较、去掉单次消费、去掉消费时预算状态检查、去掉绑定比较、拒绝后退款、去掉绕过检测、未知结果退款、超上界不关闭、接受裸整数、C 的题系数/取整/题数/state/总量/点位放宽、去掉 v2 口径检查、阶段/路由不要求普通模式 off、拒绝触发退避、实验结果可采用、去掉拒绝错误码、重放再授权、授权移到模型调用之后、入口保留客户端 system_task、实验资格忽略零 I/O 提示、实验结果丢失推荐观测。
- 严格 gate 见提交说明；线上 CI 不作为验收来源。

### 偏差与边界

- 许可 `admit` 接收单个 `ProviderSendAttempt`（method、url、body_sha256、model、attempt），不是 5 个位置参数：避免新增参数计数发现；事实与顺序同设计。
- `/experiment` 在入口只接受 `observe` 与 `skill_tool`：其它点没有标定上界和实验消费者，授权也无法发送，提前拒绝更清楚。
- 结算关闭原因覆盖 `revoked`（设计要求撤销后被拒记 `send_refused`）；成功路径不覆盖。
- “伪造阶段”在测试中实现为预留后改变阶段身份（attempt），发送门以 `experiment_identity_changed` 拒绝；stage 级伪造在准入/decide 已有 E1 覆盖。
- 本地直连 TUI 与后台执行没有授权钩子，不会发送实验；本机文件队列与 `/audit` 准备轮同为同一 owner 的可信通道，能直接写 Gateway inbox 的进程可伪造 system_task（与现有任务命令同一信任边界）。
- 经验上界不是供应商保证，只覆盖已标定的 skill_tool 形态；中文较多或候选较多的请求会因 C 超 57,600 被拒。

### 建议下一步

先由决策线 owner 审阅本分支；合入后在隔离 owner 做首次真实授权发送：开启 `enabled` 与 `experiment_enabled`、`skill_tool` 普通模式 off、`/experiment observe skill_tool 10m 1 50000 <短任务>`，核对请求记录的 `experiment_grant` 与能力推荐观测、原账快照 `charged=provider=X`、`input_bound_ratio`，并保存脱敏样本扩展标定。其他 agent 可并行只读核对更多真实 skill_tool 请求字节与计费以扩展标定范围，但不要同时修改 ModelCallLedger、传输入口或设置信封；E2 同样本对照与 F/G/H 仍未开始。

### 第二片的首次真实授权发送（2026-09-25）

在测试机隔离目录里，用户命令 `/experiment observe skill_tool 10m 1 50000 <任务>` 两次都完成了授权、单次发送与按实际结算：
- 请求为 65,070 字节、27 题，C = 40,471。
- 供应商计费 17,375 / 17,376，结算快照中的 charged 与 provider 相等，比例 0.43；预留 1/1 次 HTTP，未知用量为 0。
- 能力推荐只观察、未采用。

已知缺口：结算快照没有持久化（只在进程内账本），对照记录与基于证据的提议留给下一片（E2/F1）。详见[真实验收](DECISION_MODEL_REAL_VALIDATION.md)第 17 节。

## 第三片：E2 对照记录、F1 证据评估与授权内自动晋升（2026-09-25）

- 分支：`claude/decision-experiment-records`，基于 origin/main `1132fd9d0`（开发时基于 `dfa8e498b`，提交前变基吸收主线的回执显式参数修正与文档更新），单个本地提交，未推送、未部署。
- 状态：**已实施，本地定向、组合与严格 gate 通过，待审**。`experiment_enabled` 默认仍关闭；真实 `/experiment apply` 验收尚未做。
- 没有启动 Gateway、没有请求真实 Jev/MiniMax；测试只连本机临时 HTTP 服务。

### 解决的问题

第二片的真实运行（2026-09-25）证明授权→只观察实验→结算的链路成立，但留下两个缺口：
1. 结算快照只在内存原账里，回合结束即丢；一次实验留不下“基线对候选、结果、来源、配置版本”的持久记录（P5-E 要求）。
2. 没有任何环节把实验证据变成设置建议，也没有在用户授权内应用它的路径（P5-F）。

### E2：对照记录只写原请求记录

- **结算视图**：`contracts/model_call_budget.py::settle_input_budget` 返回预算快照加本调用的 `call_id`、`outcome`（charged/usage_unknown/refused_before_send/gate_bypassed/input_bound_violated）、原估算 `estimated_input_tokens` 与声明上界 `input_bound_tokens`；重复结算只读返回同一视图。`decision_model_call._settle_experiment` 把它追加到 `DecisionExperimentCall.settlements`（不参与比较/哈希；结算失败记 `settlement_failed`）。
- **结果携带**：`decision_service._invoke` 拆成 `_invoke`（外壳）与 `_invoke_call`（原逻辑）；只有调用确已进入原账预留时，`_with_experiment_facts` 才在 `DecisionOutcome.experiment` 挂上授权编号、设置两层 revision、策略/连接版本、上界口径与结算视图。普通调用恒为 None。
- **条目构造**：`capability/decision_experiment_sample.py::experiment_sample_record` 生成 `decision_experiment_record.v1`：refs（owner/thread/run/task/attempt/request/operation/call）、config、decision（状态/原因/题数）、**baseline**（点关闭时原 Registry `model_visible_specs` 实际展示的工具：数量、名称集合 sha256、前 64 个名称）、**candidate**（`decision_recommendation._experiment_candidate` 用 apply 同一规则 `selected_capabilities`+`_project` 投影 Jev 回答：短名单/延迟名单各至多 256 个、截断标记、Skill 计数；非成功/非选择/投影失败只给原因码）、**settlement**（结算视图白名单）、`realized=None`。不含用户正文、题目、候选说明、模型回答、凭据或端点。
- **写入**：条目随能力观测一起交给观察出口；`gateway_parts/request_experiment_records.py::observe_capability_presentation` 把 `experiment_record` 拆出，原观测照旧写 `capability_presentation_observation`，条目经 `record_decision_experiment_sample` 在精确回合转换锁内追加进 `experiment_records.entries`：record_id=原调用编号去重、最多 8 条、盖 Gateway 执行代次；回合关闭/停止抛中断且不写（不留半截记录），其它写盘失败只放弃这一条，内存 request 同步。
- **实际用量**：`request_execution._execute_gateway_conversation_turn` 在模型回合正常返回后、持久化答复前调用 `finish_decision_experiment_turn`：只有 `result_turn_end_reason==completed` 且 `archive_tool_calls` 每条都是带非空 `tool` 的字典时记 `known=true`、去重工具名（前 64 个）与调用次数；其它记 `known=false` 与 `turn_not_completed`/`archive_unavailable`，不从正文补猜。只补写本执行代次的条目（崩溃恢复的新代次不接管旧候选）；停止/关闭的回合不补写，异常只记日志、不改变本轮结果。普通请求（无条目、无 apply 授权）零 I/O。

### F1a：只读证据评估

- `conversation/decision_experiment_evaluation.py` 只读这些条目，按 schema/point/record_id 与 refs 的 owner/thread 逐条核对，其它一律忽略；样本=最近 8 条已完成条目（按 record_id 去重）。
- 可比较样本=结算 `charged`、实际用量已知、候选已投影且召回可算。召回=实际工具中在短名单的比例，分母只含快照内工具（短名单或延迟名单里的）；快照外工具（如模型编造的名称）不计。名单截断而无法归类、实际名单截断或没有可归类工具时召回未知：样本不可比较，但不阻断。
- 规则四条同时成立才给 `proposal`（`points.skill_tool.mode` off→apply，thread 范围）：可比较样本≥3（`insufficient_samples`）、窗口内每个样本 charged（`settlement_not_charged`）、每个可比较样本召回=1.0（`recall_below_one`）、每个可比较样本延迟数>0（`no_savings`）；否则 `keep_observing` 与原因码。常量（3、1.0、8、charged）是审计过的规则，写在代码并有中文说明，不设配置项：可调阈值等于让人绕过证据门，行为本身已由 `experiment_enabled` 与逐次显式 `/experiment apply` 授权把关。
- **证据链**：授权回执升 `gateway_decision_experiment_grant.v2`，另记 `thread_id`、`operations` 与 `previous_request_id`（授权时被替换信封的来源请求，与本次 CAS 同一次读取）。评估从链头沿该指针回读原请求记录（终态→处理中→done→failed），至多 16 条；每条须是本会话 granted v2 回执，编号按文件名规则（无 `/`、`:`、前导 `.`）校验，否则终止。没有目录扫描、索引文件或第二本账。
- **读路径**：`GatewayTaskBindingWriter.decision_experiment_evaluation` → `thread_experiment_evaluation`；`user_config decision_read` 在会话已有授权信封、当前运行由 Gateway 写入器承载时，在 `experiment_authorization` 之后附只读 `experiment_evaluation`（含 `authorized_operations` 与链头请求的 `latest_promotion`）；读取失败只给 `unavailable/evidence_unreadable`；没有信封时输出逐字节不变。工具描述未改，普通请求的工具 schema 字节不变。

### F1b：授权内自动晋升

- 语法：`/experiment apply skill_tool <时长> <HTTP次数> <输入token上限> <任务>`；信封 `operations=["observe","apply"]`（仍是 v2，`experiment_operations` 只接受这两种精确列表）。实验调用本身仍只观察（`may_apply=False`），apply 只授权宿主在规则满足时晋升。
- `request_experiment_promotion.py::promote_skill_tool_if_ready` 只在本请求 granted 回执含 apply 时由收尾调用：锁外只读评估；锁内（phase `experiment_promotion`，回合须 open 且未请求停止）复读设置并依次核对：仍是本请求那份授权（`authorization_replaced`）、身份（`identity_changed`）、active（`authorization_revoked`）、含 apply、未到期（`authorization_expired`）、设置 revision 与授权时一致（`settings_changed`）、能力仍开、点有效模式仍为 off（`point_not_off`，覆盖默认配置变化）；再经原设置服务 `patch`（thread 范围，`expected_revision` 取锁内读数，完整 CAS）写 apply。冲突记 `settings_conflict`，写入前校验失败记 `promotion_rejected`，写入结果不明记 `uncertain/settings_write_uncertain`；都不重试、不覆盖用户值。
- **回执权威：本请求记录 `experiment_records.promotion`**。理由：它与触发晋升的证据同处原请求记录；授权信封是纯授权，会被下一次授权整份替换，把回执写进去还要改信封 schema 及发送门的所有读取者。顺序是先写 `promoting` 再改设置、最后写终态回执；已有任何回执即不再尝试（内存快判加锁内复核），重放与重启幂等；崩溃遗留的 `promoting` 表示结果不确定，保持原样。回执含状态/原因码、目标字段、证据摘要（状态、原因、样本数、记录编号）与前后值（线程覆盖是否存在及值、有效模式、两层 revision）。
- 撤销或到期只停止未来实验与晋升，不回滚已晋升的设置；需要恢复继承时对 `points.skill_tool.mode` 执行 reset。Jev 回答只经宿主投影成候选名单；模型没有授权或晋升的工具路径（`user_config` 仍只读/撤销实验授权）。

### 文件归属

| 文件 | 本片内容 |
| --- | --- |
| `agent/contracts/model_call_budget.py` | 结算视图（快照＋结算码/调用编号/估算/上界） |
| `agent/conversation/decision_send_permit.py`、`decision_model_call.py`、`decision_service.py` | 结算视图带回、`DecisionOutcome.experiment`、`_invoke` 拆分 |
| `agent/capability/decision_experiment_sample.py`（新）、`decision_recommendation.py` | 条目构造（基线/候选/结算）与只观察路径接线 |
| `agent/conversation/decision_experiment_evaluation.py`（新） | 只读规则评估 |
| `agent/gateway_parts/request_experiment_records.py`（新）、`request_experiment_promotion.py`（新） | 写入、收尾实际用量、证据链、读路径、授权内晋升与回执 |
| `agent/gateway_parts/request_experiment.py`、`request_binding.py`、`request_execution.py` | 回执 v2（显式参数，无 `**kwargs`）、只读评估入口、观察出口与收尾接线 |
| `agent/settings/decision_experiment_schema.py`、`decision_experiment.py`、`agent/conversation/control_commands.py`、`agent/command_catalog.py` | observe/apply 动作校验、授权原语动作参数、`/experiment apply` 语法与帮助 |
| `agent/tooling/user_config_tool.py` | `decision_read` 附只读 `experiment_evaluation` |
| `tests/test_decision_experiment_records.py`、`test_decision_experiment_evaluation.py`、`test_decision_experiment_promotion.py`、`test_decision_experiment_gateway_turn.py`（新）；`test_decision_experiment_command.py`、`test_model_call_input_budget.py`（改） | 见下 |

以上路径均相对 `agent_py_agent/`。

### 本地验证

- 新增四文件 82 项：`test_decision_experiment_records.py` 27（结算字段等于原账快照、基线/候选名单、无正文泄露、未预留不写、普通决策无条目、观察拆分与去重、8 条上界、关闭/停止/换代次不写、写盘失败吞掉、实际用量来自工具账、6 类未知、只补写本代次、普通收尾零 I/O、停止收尾不补写）、`test_decision_experiment_evaluation.py` 33（规则矩阵、不可比较样本不计不阻断、快照外工具不稀释召回、外来/未完成/畸形条目忽略、去重与窗口、证据链跨目录/越界编号/缺失/跨会话/成环/上限、`decision_read` 暴露/不变/读取失败）、`test_decision_experiment_promotion.py` 18（apply 语法与授权、一次 CAS 晋升及回执、重放与重启幂等、崩溃标记不重试（内存与锁内两种）、observe 永不晋升但可读建议、读写之间用户后改、三种后改、撤销/到期/被替换、四种弱证据、被拒授权与模型无路径）、`test_decision_experiment_gateway_turn.py` 4（真实 Gateway 回合：本地 HTTP 实验、真实工具调用入账、普通回合零键、链上 apply 晋升与 observe 不晋升）。
- 相关改动文件共 10 个从干净字节码（删除全部 `__pycache__`、`PYTHONDONTWRITEBYTECODE=1`）246 passed；全部 `test_decision_*.py` 与 `test_gateway_*.py`（变基到 `1132fd9d0` 后 90 文件）2196 passed、2 skipped；相邻 20 文件（守卫、命令、插件目录、user_config、原账、TUI 菜单、工具展示等）604 passed；`scripts/check_import_boundaries.py` 0 findings。
- 变异 37 项，36 项被杀，每项在子进程 `PYTHONDONTWRITEBYTECODE=1` 下运行、按 sha256 原样恢复，结束后删除字节码重跑：结算视图丢结算码、结算不带回、事实丢结算、无预留也挂事实、基线改读原始快照、候选名单对调、写入去重去掉、样本写入放行关闭回合、未完成回合算已知、畸形工具账部分接受、收尾不看执行代次、只在 apply 时补写、链不核对会话、加载器不校验编号、观测保留实验条目、两样本即可、结算规则只看可比较样本、去掉召回规则、去掉节省规则、接受外来 owner、未完成条目计入、快照外工具算漏掉、窗口放宽、去掉 revision 核对、去掉到期核对、去掉撤销核对、覆盖 promoting 标记、CAS 改用新读版本、去掉点模式核对、observe 授权可晋升、语法接受未知动作、回执丢上一请求指针、apply 命令只授 observe、读路径无授权也读取、去掉收尾接线、信封动作不校验。存活的 1 项是“去掉收尾函数开头的早返回”：内层两个条件已保证普通请求零 I/O，为等价变异，保留早返回只为可读性。首轮另有 3 项存活（加载器编号校验、锁内已有回执即放弃、CAS 必须用锁内读数），都是纵深防御守卫：已补直接调用加载器、在内存快判之后注入并发标记、在锁内首次读之后注入用户修改三个用例，复跑后全部被杀。
- 严格 gate（变基后的提交上）：`ruff check agent_py_agent scripts`、`check_doc_sync.py --base HEAD~1`、`check_code_size.py --mode strict`（与 origin/main `1132fd9d0` 临时 worktree 的发现集合逐项比对，新增 0）、`git diff --check`、`check_clean_package.py .`、`check_import_boundaries.py` 均通过；线上 CI 不作为验收来源。

### 偏差与边界

- **召回口径**：分母只含本次快照内（短名单或延迟名单里）的工具，快照外工具不计，没有可归类工具时召回未知。字面口径会把模型编造的工具名算成“漏掉”，与候选无关却永远阻断晋升；这里的口径仍比“只看被移出初始展示的工具”更保守（同一工具若基线也按目录类别收起，仍按漏掉计）。
- **可比较样本要求实际用量已知且有可归类工具**：实际用量未知或没调用任何工具的样本没有召回证据，不计入 3 个，但结算为 charged 时也不阻断。
- **“每个样本都 charged”作用于窗口内全部已完成样本**：旧的 usage_unknown 样本会阻断建议，直到它移出最近 8 条窗口；这是按规则字面取的保守口径。
- **节省只按延迟数**：工具 schema 字节随协议序列化而变，不是快照的结构化事实，未记录。
- **读路径只在 Gateway 运行内可用**：owner 作用域 Agent 的 `gateway_workspace` 按 owner 重新解析，不指向原队列目录；因此读取器由当前请求的写入器（持有原请求路径）提供。TUI 菜单与本地直连没有这个入口，只有 `user_config decision_read` 暴露。
- **旧版回退**：含 apply 的信封旧版程序无法读取（会拒绝该线程整份决策设置），撤销与 reset 都不删除信封；回退前须用一次 `/experiment observe` 授权替换，或使用升级前备份。v1 回执（第二片写的）没有指针与 thread，不进入证据链。
- **晋升后不自动撤销授权**：晋升写入推进了 revision，旧授权随之失效；点模式变为 apply 后实验只在普通模式 off 时运行，因此不会再对该点实验。
- 与主线并行修正的合并：`request_experiment._receipt` 原用 `**fields`，违反“产品代码不用 var-keyword 服务接口”守卫；主线 `0bb6f3a88` 已改为显式 `authorization_id`/`code`。本片变基后保留显式关键字参数，改为 `code`/`grant`（`grant` 是 `_authorize` 返回的 v2 授权事实：授权编号、thread、动作、上一来源请求），非空才写入。

### 建议下一步

1. **先审阅本分支再做一次真实 apply 验收**（测试机、隔离 owner，不动日常用户数据）：开启 `enabled` 与 `experiment_enabled`、`skill_tool` 普通模式 off、决策连接指向官方 Jev；同一会话连续发 3 次 `/experiment apply skill_tool 10m 1 50000 <会实际调用工具的短任务>`（任务需真的调用 1—2 个工具，否则样本没有召回证据）。每次核对请求记录的 `experiment_grant`（v2、`previous_request_id` 指向上一次）、`experiment_records.entries[0]`（结算 `charged` 且等于原账、候选名单、`realized.known=true`）；前两次收尾回执应为 `skipped/evaluation_keep_observing`（`insufficient_samples`），第三次在规则满足时为 `applied`，设置读回 `points.skill_tool.mode` 为 thread 覆盖 `apply`、revision 正好前进一次；再用 `user_config decision_read` 看 `experiment_evaluation` 与 `latest_promotion`，最后 reset 该字段恢复继承。若召回不满足，记录 `recall_below_one` 与涉及的工具名作为真实证据，不改规则。
2. 可并行：其他 agent 只读整理更多真实 skill_tool 请求的 wire 字节与计费扩展经验标定；另一线可做 P5-G 的临时试验自动收尾（沿本片回执与原 `restore` 原语）。都不要同时改 `experiment_records` 写入器、授权信封或 ModelCallLedger 结算视图。
3. 风险边界：不要把阈值做成可调配置，不要把 Jev 置信度或模型自述接进评估；跨进程恢复仍不支持（崩溃遗留 `promoting` 只保留不确定事实）。
