# P5-E1 授权与输入预算原语交接

- 日期：2026-09-22；分支：`codex/decision-model-integration`。
- 范围：主线授权的共享 worktree 内，原 decision settings、ModelCallLedger、决策服务、user_config、TUI 能力字段及定向测试。
- 状态：**本地授权保存/撤销与预留原语已实现；完整 E1 和联网实验未实现、未验收。**
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
