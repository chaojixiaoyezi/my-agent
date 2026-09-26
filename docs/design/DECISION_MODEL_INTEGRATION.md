# 可选决策模型接入与并行实施计划

状态：用户已授权 P1—P5 完整实施；P1—P4 有多项本地接线，P5-A/B/C/D 有首片，E1 有默认关闭的授权与预算原语，并在本地分支实现了 `/experiment` 授权入口、经验输入上界与发送硬门（2026-09-24，待审）。日期：2026-09-22。最初核对基线：9141dad9c。
完整 P1—P5 完成条件与逐项进度见[执行 Goal](../tasks/DECISION_MODEL_GOAL.md)，不得在完成 P1 后关闭整个 Goal。
本地配置、协议和假服务链已接通；隔离环境已做少量真实 Jev 与 Gateway TUI 验收，尚未发布或改日常 Gateway。
计划在独立 worktree 保存；另一任务正在使用的工作区及其未提交修改不纳入本轮修改。

## 1. 解决问题与目标

将 Jev 作为一种决策模型接入现有模型体系，用户像新增普通模型一样配置它，按接入点开启、观察或应用。
普通生成模型继续负责沟通、推理和执行；决策模型负责在有来源的候选之间选择、分类、评分。
增强关闭、超时、没额度或不可用时，基础流程继续成立；基础模型自身故障仍按原合同处理。
增强不能成为唯一权限门、记忆事实源、任务完成裁判或执行调度器。

用户可以手动配置，也可以授权 my-agent 读取配置、提出调整、做有预算的对照和应用指定范围的配置变更。
配置控制必须走宿主结构化服务，不能解析普通聊天文本作为开关，也不能依赖模型直接改私密文件。
自动试验默认关闭；开启某个决策点不等于授权后台试验、额外网络请求或自动扩大工具权限。
决策模型只统计输入 token、请求状态和耗时，不接入 USD 价格估算或 owner/run 费用累计；本地决策模型也使用同一合同。远端供应商可能自行收费，但宿主不把估算价格当作决策用量事实。

本计划替代早期“先作为 MCP 判断工具”的主要产品接入方向。原文的授权、来源、质量和上下文边界继续有效。
首版采用原生可选模型角色；插件以后可以贡献适配器或策略，但基础决策点不依赖插件装卸完成。

## 2. 已核对的复用基础与缺口

| 能力 | 当前入口 | 本计划的使用方式与缺口 |
| --- | --- | --- |
| 模型身份和私密连接 | agent/settings/model_provider_schema.py、model_profiles.py、shared_model_catalog.py | 扩展现有 agentic/embedding 用途，增加 decision；协议校验与角色分开，不另建凭据仓库 |
| 会话和子代理模型绑定 | agent/settings/thread_model_selection.py、model_scope.py；agent_core/orchestration/create_policy.py | 复用显式模型和父级继承；决策只提供可忽略的选择建议，不另造子代理创建链 |
| 请求预算和取消 | agent/backends/request_scope.py、gateway_helpers.py；agent/concurrency/interrupt.py | 已有单调时钟预算和连接取消；完整 JSON 请求的总期限、零重试及极短剩余预算仍需接线验证 |
| 辅助请求记账 | agent/conversation/auxiliary_model_call.py 与既有 ModelCallLedger | 复用开始、失败和 usage 的事实合同；决策不调用该生成包装的价格结算，现有 generate 包装会重抛错误，不能当成已具备可选失败隔离 |
| 并发和资源原语 | agent/llm_scale/ 的既有组件 | 复用原并发准入；决策没有 USD 价格门，不能声称已经具备所有进程共享的决策限额 |
| 失败分类 | agent/backends/gateway_helpers.py | 已区分部分硬额度错误与临时限流；复用结构化分类，不匹配自然语言错误说明决定路由 |
| 后台退避 | agent/conversation/background_supply_backoff.py | 原作用域是会话后台消费，非所有异常都吸收；不能直接用它把决策故障升级成整条后台车道故障 |
| Skill 与工具 | agent/tooling/registry.py；agent/capability/skill_snapshot.py、skill_search_tool.py | 复用授权快照、按需读取和版本核对；推荐可见性不得改变权限上界 |
| 记忆整理 | agent/memory_store/curator.py、curator_backend.py、curator_validation.py | 在原批次提取前附临时标注；仍由原整理器提取、验证、提交和推进游标 |
| 上下文和缓存 | agent/prompting_parts/cache_layout.py、agent_core/model/context_window.py、context_pressure.py | 复用稳定前缀、真实容量和 provider usage；不建立第二份历史或 Compact |

上述 agent 路径均相对 agent_py_agent/。这是针对相关入口的核对，不代表逐行审计全仓。
记忆整理当前是专用结构化模型调用，不能直接当成任意工具齐全的通用子代理。

### 外部参考及采用范围

- [TypeSafe 官方接口](https://docs.typesafe.ai/introduction/quickstart)：通过 HTTP 发送 state/questions，解析各题结果与 usage；无需把 TypeScript 工程安装进 Python 核心。
- [官方 confidence 说明](https://docs.typesafe.ai/confidence)：Choice/Score 的 confidence 来自分布；Noul 不提供相同字段。置信度不是正确性证明，不跨类型编造统一阈值。
- [jev-harness 实现](https://github.com/AntonioCoppe/jev-harness/blob/main/src/harness.ts)：参考结果解释、策略分离和观察模式；其 run 入口直接等待 SDK，不把它当作已解决本项目 2 秒取消和故障隔离的实现。
- [jev-harness README](https://github.com/AntonioCoppe/jev-harness)：参考离线对照思路及保持原始会话完整的边界，不迁入整个 harness、recipes、日志系统或其示例业务。

该段最初是实施前参考记录。如今已通过隔离 Gateway/TUI 实际请求 Jev、官方 MiniMax 与 OpenCode DeepSeek；实际次数、来源和未验范围以[真实验收记录](../tasks/DECISION_MODEL_REAL_VALIDATION.md)为准，不按公开演示承诺延迟。
如后续复制源码，应核对固定版本许可并按仓库 NOTICE 规则保留来源；目前只记录设计参考。

## 3. 默认时间规则：短等待，立即让原流程继续

以下数值是可修改的建议默认值，不是写死的运行限制，也不是当前运行值。用户可在设置中修改，
my-agent 也可根据用户明确要求或既有授权，通过同一配置服务修改；两种入口读写同一有效配置。

| 场景 | 单次调用预算 | 同一阶段累计决策预算 | 请求重试 |
| --- | --- | --- | --- |
| 前台模型选择、召回、能力推荐 | 2 秒 | 4 秒 | 默认 0 次 |
| 后台记忆整理前置标注 | 4 秒 | 4 秒 | 默认 0 次 |
| 用户显式测试决策连接 | 由测试操作明确指定，默认 4 秒 | 使用同一测试预算 | 默认 0 次 |

### 时间可以调，但预算必须真正生效

- 提供通用默认单次期限、阶段累计期限，以及接入点的可选时间覆盖；不填写覆盖值就继承，清除覆盖即可恢复继承。
- 首版不增加服务商、项目、模型、线程等多套重复时间层级。请求级临时覆盖只在用户明确指定范围时复用已有任务/会话配置能力。
- 支持正数秒及小数，例如 1、2、4、5 或 1.5 秒；拒绝布尔、负数、NaN、无穷大和用 0 表示无限等待。关闭使用独立开关。
- 阶段累计时间同样可调。界面和工具回执同时显示填写值、继承来源、有效上限和限制来源，不能只说“保存成功”。
- 单次设成 5 秒而阶段只剩 4 秒时，实际最多 4 秒；不得暗中提高累计预算，也不得告诉用户本次能等满 5 秒。
- 时间修改用于后续决策请求，不延长正在等待的请求，也不重置当前阶段已消耗的时间；新阶段才读取新的完整累计预算。
- 设置提交后不需要重启 Gateway。关闭会立即阻止旧建议应用，并请求取消仍在等待的决策调用；已经执行的业务动作不因关闭而回滚。
- 时间设置控制最长等待，不保证服务商必定在期限内完成；超时保留基础方案的规则不随时间调整改变。
- my-agent 经 `user_config decision_patch` 自调等待时间时受能力配置上下限约束（默认 1—30 秒，0 为不限），越界拒绝、不夹取；
  用户在菜单里修改不受此限。见[决策开关、超时自调与审计](DECISION_AUDIT_AND_ADMIN_CONTROLS.md#2-决策超时可调my-agent-可在上下限内自调)。

1. 使用单调时钟。期限从该决策阶段开始准备输入时计算，包含额外数据准备、排队、DNS、连接、TLS、发送、读取、解码和校验。
2. 后续调用只能使用剩余预算：实际可用时长取单次上限、阶段剩余预算和调用方剩余预算的最小值，不能每进入一层重新得到 2 秒。
3. 阶段指宿主现有的一次模型请求准备、一次整批子代理创建，或一次 Curator 批次，不新增阶段调度状态机。
4. 一批 10 个子代理共用该批阶段预算；优先一次请求带多道独立问题，超过输入限制则有界处理，来不及判断的项保留原选择。不能串行等待 10 次 2 秒。
5. 网络/认证/额度/解析错误一旦返回，立即结束本次增强，不等到预算耗尽。SDK、HTTP 和宿主不得各自再重试。
   原恢复分类表登记决策响应无效、连接探测失败、设置版本冲突及供应商响应超限；前两者保留普通模型主链，版本冲突重读 CAS，响应超限缩小请求，不把它们降为 `UNKNOWN_ERROR` 或原样重试。
6. 没有响应、收到响应头、持续滴流或只返回半份 JSON，都不重置总期限。完整合法结果必须在期限内完成校验。
7. 首次无需数据即可判断时不额外调用模型。缺数据只允许在同一预算内进行一次有新输入的补充判断；零重试不禁止这种语义不同的请求，但不得重置预算。
8. 数据读取、输入大小和响应大小同时有界，避免在网络前后耗尽 CPU/内存而绕开期限。CPU 密集解析不能声称只靠 socket timeout 可以中断。
9. 主模型的首事件/慢流合同保持原样。新增总期限仅用于可选决策请求，不能把长任务或正常生成强制限制为 2 秒。

### 超时后的资源与结果

- 主流程到期即采用基础方案，不等待旧请求清理完成，也不为清理额外阻塞 0.5 秒或数秒。
- 传输层收到精确请求取消并关闭连接。复用原取消机制，不新建每次超时后失去句柄的裸线程。
- 若 DNS/第三方阻塞尚未退出，保留准确句柄和受限名额，在退出前不叠加同一资源的新请求；迟到结果永不获得业务提交权。
- 需分别证明“调用者及时返回”和“底层资源可回收/有界”。Future.result(timeout) 或 wait_for 本身不证明阻塞线程已停止。
- 用户取消仍停止原任务，不能被当作决策失败后继续工作。决策超时的取消作用域不能取消主模型或兄弟任务。
- 客户端中断不保证远端服务商未执行或未收费；缺 usage 记为未知，不能记成输入 token 为零。

P1-C/D 本地组件已验：原 ConcurrencyLimiter 的占用与保留量由同一 Condition 原子核对，
`global_llm_admission_slot(optional=True)` 立即尝试，并在已配置上限时给普通模型保留 1 个名额；
上限为 1 时跳过可选调用。默认普通请求仍读原等待时间，未配置全局上限时保持原行为。
名额必须由实际 worker 持有到退出，等待者到期不能释放未退出调用；实际决策服务已经复用这个入口。
通用有界 worker 已从 Curator 迁出复用，单进程残留安全上限为 32，可选调用保留 1 个正常后台名额；
到期即返回时合作线程也可能暂时仍存活，Curator 据实记 still-running，退出前不能缩批重叠重试。

### 避免每轮重新等 2 秒

增加最小的请求准入抑制，优先复用既有原语：某决策连接失败后短暂冷却，建议初值 30 秒；冷却期直接使用原流程。
认证/配置错误等待配置修正或显式重试；额度耗尽按更长冷却或明确恢复操作处理，不做高频健康轮询。
恢复探测由之后的真实需求触发，同一作用域只允许一个；不启动新后台守护任务。
冷却记录限于决策 profile/连接/凭据归属及其配置版本，不因同一 thread 使用主模型就把主模型一起禁用。
记录有数量上限和过期清理；跨进程/跨机器共享限制必须走已有资源准入，不能把本进程字典宣称为全局限额。
这属于请求资源状态，不持久化第二套任务状态，也不擅自把用户的“已开启”改为“永久关闭”。

## 4. 最小结构和唯一执行入口

    原流程准备基础方案与合法候选
        → 读取本次冻结的决策策略
        → 关闭/冷却/预算不足：直接保留基础方案
        → 可用：在截止时间内取得结构化建议
        → 验证身份、候选、输入版本、权限、上下文及开关状态
        → 应用允许的局部调整，或保留基础方案
        → 原入口执行一次

只有三类新增职责：公共决策协议、Jev 协议适配、可选调用边界与消费点的有限接线。
公共协议放现有后端合同层，供应商实现在 backends；配置放 settings/原配置模块，消费逻辑放各所属模块。
协议支持独立 decide 操作，不让 Jev 假装生成 assistant 正文、工具调用或任意 JSON 推理。
不得为每个接入点新建 executor、logger、scheduler、fallback agent、数据库或只有转发作用的 facade。
若需要提取复用代码，必须从现有实现迁出并同步迁移调用方，删除旧副本，不能复制一份稍改名字。

P1-B 本地实现使用 `decision_protocol.py` 固定输入快照与宿主绑定，`typesafe_decision_wire.py` 按
[TypeSafe API](https://docs.typesafe.ai/api) 校验问题/答案，`typesafe_decision.py` 只暴露 `decide`。
不安装 SDK 或 harness，不把分类结果转成聊天正文。请求带原绝对期限，模型实际返回的版本与原 usage 留在响应中。
原 HTTP 新增 request-local deadline、max_retries 和 max_response_bytes，默认 None 保持原生成路径。
显式严格 JSON 在原 strict_json 解析前做 64 层资源预检（字符串/转义不计容器），领域输入/答案仍按 16 层及节点上限校验；
解析成功和失败都复核同一期限。零重试的 HTTP 错误按状态立即返回，不为诊断而等待错误正文。
本地协议与传输组件不等于可选调用服务已完成：有界 worker、主资源保留与取消唤醒已有原语组合验证，
实际策略和冷却已经接通，业务消费仍须接线。设置、账本及服务的当前本地边界见下节；不能据此声称真实 Jev 已可用。

### 4.1 共用设置与原账本的本地合同

`settings/decision_settings.py::execute_decision_settings_operation` 是界面与原 `user_config` 共用入口。
`read/patch/reset` 只访问原 owner 模型目录与原线程字段，不构造 Agent、不测试服务、不复制凭据。
`patch.changes` 使用扁平路径，`reset.fields` 删除覆盖恢复继承；写入必须携带读取到的
`expected_revision={owner,thread}`。读回字段是 `revision`（单数）、`effective`、`sources`、两层 `overrides`；
修改另返回 `before`。锁序固定 owner→thread；版本冲突要求重读，不自动重放旧写入。
原 `user_config` 增加 `decision_read/decision_patch/decision_reset`，线程只取原可信 runner，模型不能传任意身份。
TUI 决策菜单（`cli/chat_parts/tui_decision_menu.py`）的接入点清单直接取 schema 的 `POINTS`，本地只配中文显示名，缺显示名时显示原键。此前菜单自带一份清单，漏了 `pre_recall`：界面设不了召回前补充查询，已有该点覆盖时打开"恢复继承"会因取不到显示名而抛 KeyError（2026-09-25 修复，分支 `claude/decision-tui-points`）。

原模型目录最初以 v4 和 conversation_thread.v10 显式迁移旧数据；当前私有目录 v5、共享发布 v2 增加随机持久代次，原 `decision_settings.v1` 覆盖仍在同一目录/线程权威内。普通读取不迁移落盘，已启用的子代理建议准备才可在原锁内初始化旧目录代次；详情见[目录代次交接](../tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md)。
默认值归原 AgentConfig、CapabilityConfig 与 MemorySettings；覆盖内不存默认值、阶段余额或运行状态。
共用 profile_id 与逐点 profile_id 都引用原模型目录；只有本次新写入的引用需要检查有效用途，
已有配置失效不能阻止关闭。共享撤销仍按原授权读取，`configured` 只证明本地配置，不证明网络在线。
阶段时钟仍由实际消费点持有，服务读回的 `max_request_seconds` 只表示配置上限，不承诺运行中的剩余时间。
完整字段、迁移、错误码及当前菜单未接线边界见 [P1-E 交接](../tasks/DECISION_MODEL_P1E_HANDOFF.md)。

原 `ModelCallLedger` 终态只记录首次结束，迟到 first_token/finished/timeout 不重开调用；
HTTP 尝试可补记为物理事实。显式 retain 句柄只在原账本保留准确明细及其累计范围，实际 worker 退出才释放。
`model_call_purpose_breakdown.v1` 在原摘要内按 main/auxiliary/decision 互斥分区，复用相同累计算法，不另建账本。
`provider_usage_fields` 区分逐字段真值与估算；历史缺字段保留历史解释，不能反推已报。
输入已报且为零、输入缺报但输出已报、全部未知分别保留，决策无正文时不虚构正文输出估算。

用量存储的累计快照事件编号由原范围及内容摘要派生，迟到 HTTP 尝试不再与相同调用数的旧快照冲突。
显式旧 event_id 仍按原冲突规则校验；用途桶使用同一增量规则，不把分区再次加入根总量。
原统计行额外显示“决策 ≈N token · 成功 X · 失败 Y”（2026-09-25 起，原“决策入 …+?”）：成功为 finished，失败为 failed+timed_out；
N 按已报调用的平均输入外推到全部决策调用，一次都没报显示 `≈?`，不显示成 0；约数只供显示，输出留白、无价格。
见[决策开关、超时自调与审计](DECISION_AUDIT_AND_ADMIN_CONTROLS.md#4-tui-统计行决策-n-token--成功-x--失败-y)。
决策不占普通模型轮数、不覆盖最近生成缓存/工具/速度；后台用量文件变化令原显示基数失效。
这些是本地统计与设置接线；实际执行消费者、设置菜单及真实 TUI 仍按完整 TODO 单独验收。

### 原设置菜单与显式测试（TODO11 本地接线）

原 `/model` 的“决策模型设置”复用 provider/model 表单，保存仍不联网。
`execute_model_profile_operation` 在普通生成模型初始化之前分派 `decision_read/patch/reset`，
嵌套 `decision` 参数交共用服务；身份只取原 Gateway 认证会话或本地会话解析。
`decision_models` 只返回原 owner/已共享 decision 配置的脱敏投影；原 `user_config` 可用它选择已保存引用。
`decision_probe` 只在显式动作时测试已保存且可用的连接，参数为 profile_id 与有限正 timeout_seconds；
不保存开关、不替换主模型、不附聊天材料。原生单题走同一后端构造、原有界 worker/准入/身份头/账本。
成功仅证明本次连接与协议，并清除该准确连接的临时冷却；其他 owner/连接不受影响。
探测使用独立 request_id，原 standalone 用量结算追加到原会话账本；只刷新用量，不覆盖主模型轮次、pending 或工具状态。
未知输入为 null，供应商原输出内部保留；菜单仅展示输入，输出留白，无价格。
前台 HTTP 等待为用户探测秒数加 10 秒运输余量，模型自身仍使用准备前冻结的绝对期限且不重试。
本地文件事务不承诺强制抢占；没有新增后台结算器、配置文件或第二份调用账。

配置读回新增 field_scopes；Curator 的 runtime_scope 固定 owner_background，只读 owner enabled/profile/后台预算。
线程不再允许新增 background_timeout_seconds 或 points.curator.*；历史覆盖仍可读，并能按原双层 CAS reset 清理。
投影与服务共用 POINT_RUNTIME_SCOPES；错误 point/stage 范围不发请求。前台修改不延长原已开始阶段。
菜单每次保存一字段，冲突后重读，不自动重放。my-agent 的测试动作仍走原工具权限，不开放凭据写入。
本地真实按键、原设置和双 HTTP 组合与收费模型/安装版 TUI 分别验收，详见 TODO11 交接。

### 4.2 已实现的可选服务边界

`conversation/decision_service.py` 提供 `begin_decision_stage` 和 `decide`，宿主在准备额外材料前冻结一次阶段；
同阶段调用共享绝对截止时间，点级超时只缩短本次请求，不补回阶段时间。发送前后复核 owner/thread、配置和连接。
`DecisionOutcome.may_apply` 只表示信封和模式允许消费，消费者仍逐题检查答案、当前候选和来源，不因此获得写入权。
消费者刷新来源或整理候选之后，还须调用同一个 `decision_outcome_is_current`：用响应冻结的连接摘要、
操作身份和本次实际截止时间复查，不复制策略算法、不重新分配预算。普通失效保留当前合法基础方案，用户取消继续传播。
`decision_policy.py` 的进程内有界索引仅负责精确设置通知与连接/点位冷却；不拥有 worker 或持久配置。
普通传输失败首次冷却 30 秒，额度失败 300 秒，认证/配置错误等待配置变化或显式重试；输入错误、锁忙和准入忙不冷却连接。
同一连接（owner、profile、连接修订）冷却过期后的重试再失败时冷却翻倍（30→60→120→240 秒，封顶 300 秒）；冷却期内才返回的并发失败算同一次故障，不加级；额度失败固定 300 秒，但计入连续次数。
连接返回过一次响应、连接测试成功或显式重试即清除连续次数，从 30 秒重新计起。冷却表只在进程内，重启从零开始。
这样处理的原因：第 13 项的挂起真实样本里，固定 30 秒冷却让持续挂起的供应商每个冷却窗口后的第一轮都多等一个完整期限。

修订（2026-09-26）：超时只冷却出问题的那个点位。
- **键和原因**：冷却键为 (owner, profile, 连接修订, 点位)，结果原因是 `point_backoff`。连接被拒、5xx、DNS、额度、配置这些错误仍冷却整条连接，原因是 `connection_backoff`。成功时同时清除连接键和点位键。
- **为什么改**：真实样本里 Jev 经本机代理访问 `api.typesafe.ai`，单次约 2.5–5 秒。选模型每轮最先调用又常超时，把整条连接的冷却推到 300 秒，其它点位长期拿不到调用机会。超时只说明这条线路对本点位的预算不够，服务仍能响应。
- **代价**：如果服务只接受连接、从不应答，每个点位要各自超时一次才进入冷却。

决策结果日志（2026-09-26）：`decide()` 的每个返回按点位追加一行到 owner 规范路径 `owner_decision_outcomes_jsonl`，即 `<owner_home>/data/decision/outcomes.jsonl`。
- **内容**：只记点位、范围、模式、状态、原因、耗时和宿主身份编号，不含状态、题目、候选或回答正文。成功、超时、冷却跳过、配置不可用都留痕，用户中断不记。
- **边界**：最多保留 1000 条；写失败只记日志，不改变决策结果。
- **读取**：`audit_records` 的 decision 主题据此给出每个点位各状态的次数和最近几条。用量账只按用途汇总，不能拿它推断单个点位是否接通。

故障矩阵（2026-09-24，`test_decision_fault_matrix.py` 经真实传输栈钉住，冷却期内不发起新尝试）：

| 故障 | 本次结果 | 之后 |
| --- | --- | --- |
| 连接被拒、5xx | error | 冷却 30 秒，到期后由下一次真实需求重试；故障仍在则下次冷却 60 秒 |
| DNS 解析失败 | error（决策请求零重试，解析失败立即结束；解析本身卡住才按超时记 deadline） | 冷却 30 秒；故障仍在则下次冷却 60 秒 |
| 超时 | deadline | 只冷却本点位 30 秒（point_backoff），连续超时逐次翻倍；其它点位照常请求（2026-09-26 修订） |
| 429 额度耗尽或限流 | error | 冷却 300 秒 |
| 402/401/403、TLS 失败 | configuration_required | 不随时间解除；设置修订变化或连接测试成功后才重试 |

更正（2026-09-25）：DNS 一行原写作 deadline，是开发机经本机 HTTP 代理（127.0.0.1:7890）跑出的结果——请求先连代理，测试注入的解析失败根本没走到，代理解析拖过 0.3 秒期限。决策请求 `max_retries=0`，直连时解析失败立即以 error 结束；CI（ubuntu-24.04 直连）首次暴露此差异。故障矩阵现在清掉代理环境并核对注入的解析确实被走到。

宿主的非阻塞设置读取（`blocking=False`，决策阶段、调用前后复核和连接测试使用）不取文件锁：模型目录与线程文件都经临时文件替换原子写入，单次写事务只改其中一个文件，读到的总是已提交版本。
原先读取也拿排它锁，同一 owner 的并发读取互相挤成 `settings_busy`：12 个会话同时决策只有 1 个真正发出，另外 11 个静默回退。写锁被占用时，现在也按已提交版本继续；旧建议仍由调用前后的版本复核、`decision_outcome_is_current` 和设置变更的在途取消挡住。
资源键按 owner/thread/profile/连接划分：同一会话同一时刻只有一个决策在途，第二个返回 `admission_busy`；不同会话各自独立，共用可选准入的 31 个名额。
设置通知单调合并 owner/thread 版本，恢复继承也按新的实际路由取消；跨进程修改由结果采用前复读拒绝旧建议，
当前不承诺跨进程主动即时中断 socket。

宿主关闭时的在途取消（P4-F，2026-09-25，已合入 main `25650830d`）：Gateway 停止收尾在置位停止事件后立即调用 `decision_policy.cancel_active_decisions_for_shutdown()`。它在索引锁内置位进程关闭标记并标记全部在途决策，再在锁外取消各自句柄。等待中的前台和后台调用立即返回 `stale/host_shutdown`：不冒充用户停止，不进入连接冷却；调用账按原取消路径记 `DECISION_CANCELLED`，worker 仍沿 bounded_call 自行退出。关闭开始后新的决策在登记时即返回 `stale/host_shutdown`，不联网。收尾里这一步用 try/except 包住，出错只记异常类型事件 `gateway_decision_cancel_failed`，不中断后续清理。它只取消本进程内登记的句柄，不读写持久状态。主线 owner 的 `settle_open_model_calls_for_shutdown()`（`db4d46398`）在停 HTTP 与收线程之后，把仍未结束的模型调用结清为 `MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`；排空窗口内正常结束的调用照常结算。非 Gateway 的本地 TUI 退出即进程结束；Gateway 模式下关掉 TUI 不会停止 Gateway 里的回合，这是既有设计。

Curator 与插件相关点的并发组合（P4-F，同一分支，`test_decision_curator_plugin_concurrency.py` 经真实本地传输栈验证）：
- 后台 `curator` 慢响应时，前台 `skill_tool`（插件工具短名单所用的点）照常在期限内完成；两者各记一条 purpose=decision 的原账。
- 线程设置变更只提前取消该线程的前台决策；owner 级改 `curator` 只提前取消后台决策。
- 已知取舍：采用前复核比较的是整份策略版本（两层 CAS 加全部有效值，自 `e131b1f6e` 起的保守设计），所以 owner 级任何设置改动也会让同 owner 其他点的在途建议在返回后作废为 `policy_changed`。两者都回原方案、都不挂起，只可能丢掉一条建议。
- 冷却分两种键（2026-09-26 修订）：后台 Curator 超时只让 `curator` 自己进入 `cooldown/point_backoff`，同一连接上的前台点照常请求；连接被拒、5xx、DNS、额度、配置等连接错误才冷却整条连接（owner、profile、连接版本），冷却期内前后台都直接返回 `cooldown/connection_backoff` 保留原方案，不发请求。见 `test_decision_curator_plugin_concurrency.py`。
- 宿主关闭时两者一起被取消。

`decision_model_call.py` 在实际 worker 安装原 HTTP observer 和身份头，并持有原可选准入与原账本 retain；
caller 先完成/失败/超时入账，真实 worker 退出前不释放其资源。稳定资源键不含阶段编号，不能换阶段绕过残留限制。
返回和异常边界复用 `publish_model_metrics(usage_only=True)` 刷新活动行：保留原生成的 pending/工具/缓存/速度，
不在短决策后等待会话写锁；持久显示在下个原模型边界刷新，持久用量仍沿原 finalizer，不提前结算混合活动范围。
本地 HTTP 组合验证实际发送、错误、超时、关闭和活动 TUI 数据流；不证明真实 Jev 判断质量或已安装产品体验。

### 4.3 Curator 用户后台接入（本地已验）

宿主显式 `begin_decision_stage(..., scope="owner_background")` 绑定原后台 run，thread 必须为空；
活动 runner 带会话时不能改称用户后台。后台只读取 owner 覆盖与 `background_timeout_seconds`，不新建会话，
不把历史消息的 thread/run 当成当前身份。`enabled_points` 是开始阶段的准备提示：关闭就不编码材料，
发送和采用仍由原服务复查，不能把该快照当授权。原会话 scope 和默认调用方式保持。

原 Curator 收集完整批次后调用一次可选标注，随后仍走原提取、验证、提交和游标链。
分类/优先级建议附在动态批次输入中，原消息、审计和正式记忆不删不重排；最多 32 来源各两道题，
超出来源仍完整交原提取。注释按原 refs/hash 过滤，缩批不带走已移除来源的建议。
not_needed、need_data、no_match、abstain 与调用错误分开；缺项只引用宿主绑定来源，
没有补资料/工具权限时仍交原提取判断，不凭建议创建任务或推进游标。注释挤占原字符预算时整份放弃注释。

`extraction_budget_seconds` 为提取与 lease 共用的唯一原预算公式；后台决策最多借用真实剩余提交缓冲的一半，
另一半和全部原提取预算保留。原 lease 到期转换为只缩短的 caller deadline，不另延长租约。
当前决策调用按真实后台 run 留在原 ledger；原 Curator 没有独立持久用量结算，本片不伪造前台归属。
实际供应商质量、运行中产品和后台累计展示仍按后续验收处理，见 [Curator 交接](../tasks/DECISION_MODEL_P2_CURATOR_HANDOFF.md)。

先准备基础方案不等于偷偷执行原动作；在选择确定之前，不创建两批子代理、不提前记忆提交、不调用两个主模型竞速。
已执行的模型选择和配置版本跟随原 operation/run 记录；同一创建请求重送、恢复和重试不得重新抽签换模型。
子代理执行模型候选复用原 `agentic` 模型目录和决策设置的结构化 `candidate_profile_ids`：空数组沿原可用目录，非空数组只缩小候选集合，用户和 agent 经原设置服务按 owner 或 thread 修改。列表仅保存原 profile ID，不能按模型名称猜服务商，也不授予共享模型或工具权限；禁用、撤销、连接或列表版本变化使在途建议失效。隔离验收仅允许官方 MiniMax-M2.7、官方 MiniMax-M3 与 OpenCode DeepSeek-V4-Flash 的三个已核对 profile，日常模型保持原状。
子代理的逐次模型选择是主代理发起派工后的自动执行链：宿主准备合法候选、请求 Jev 建议、核对该 child 的实际首轮输入与工具/窗口能力，然后自动采用合法建议或保留原继承模型并继续创建运行。用户不逐个选择、确认、补决策资料或触发采用；现有显式模型配置若存在，只作为宿主硬约束和并发覆盖事实，不是本流程所需的人机步骤。创建阶段只拿到建议而首轮事实未知时，建议暂存于 canonical child thread，启动核对后自动决定；暂存本身不算切换成功。
保留时 child thread 的建议记录 `status=retained` 与结构化原因码。提交阶段（锁内最后复核）每个失败点各有原因码，以便真实样本直接归因：
- 目录与锁：`model_catalog_busy`、`model_catalog_changed`、`source_thread_busy`、`source_thread_missing`。
- 复核：`settings_changed`、`task_changed`、`permission_changed`、`commit_deadline`。
- child 线程 CAS：`advice_changed`、`first_request_consumed`、`selection_revision_changed`。
- 其它：`advice_source_is_child`，以及锁内其它非阻塞锁占用的 `commit_lock_busy`。
owner 级决策设置写在模型目录里，改它会先表现为 `model_catalog_changed`。此前这些都记成 `selection_changed`，第四轮真实样本因此无法补推原因。

## 5. 配置、界面与自己操作的边界

### 配置权威

- 原 owner 模型目录负责 provider、密钥引用、模型 ID、用途与协议，扩展 decision 角色；主模型和子代理执行模型只接受 agentic 角色。
- 一般开关和默认时间策略归现有 AgentConfig/YAML；子代理、Skill/tool 能力路由的绑定和开关归 capability_config；记忆接入点归原记忆配置。
- 每个字段只由一个配置模块负责，其他层只引用解析结果。不能同时维护 decisions.json、菜单私有偏好和 YAML 三份独立权威。
- 界面和模型操作都调用相同配置服务，读取最终有效值与来源。实施前核对既有 owner 覆盖保存路径，缺口扩展原服务，不新增配置存储系统。
- YAML、dataclass、中文说明和校验同步。模型目录若增加持久字段，使用显式版本迁移，不默默放宽旧 schema。
- 无效新增配置在保存前拒绝，保留原有效配置；可选连接缺凭据、失效或不可达只影响该决策能力，不阻断 Gateway 启动和原模型使用。原配置存储整体损坏仍按原错误合同处理，不能假装正常。
- 通用期限必须是有限正数，放在通用模型策略中；开关独立表达关闭，不用 0 同时代表关闭或无限等待。能力路由中的既有配额项仍保留 0 不限制的约定。

P1-A 本地实现：唯一 owner 模型目录已升 `owner_model_profiles.v3`，新增 `decision` 用途与
`typesafe_decision` 协议。v1/v2 只读显式迁移，下一次管理写入才保存 v3；模型编号、原连接和默认选择保留。
普通生成解析默认只接受 agentic，决策消费者须显式声明用途；共享发布仍只保存原配置引用，撤销后不可解析。
公开字段 `available` 保持原聊天可选含义，`available_for` 表示实际可用用途，不能把两者混用。
原服务商管理表单已支持保存/编辑决策用途，避免静默降为生成协议；快捷新增聊天选项保持原样。
本片未接 Jev 网络或设置开关，不是 P1 整体完成；旧版本程序不能读 v3，回退需使用升级前备份。

P1-E 实施约定：当前 `user_config` 写进程 YAML，不是 owner 设置服务。owner 长期决策覆盖和 revision
扩展原模型目录，使用原文件锁完成比较、修改和原子保存；只保存配置覆盖及 profile 引用，不复制默认值或凭据。
会话临时覆盖沿原 ConversationThread/ThreadStore 原子更新；全局 YAML 工具仍受本机管理员边界约束。
字段定义仍分别归 AgentConfig、CapabilityConfig 与原记忆设置；菜单和模型工具只调用同一提交入口。

### 用户看到的操作

1. 在原模型管理入口新增“决策模型”，填写端点、私密凭据、模型名与能力信息；保存不调用 API。
2. 展示可绑定接入点及其作用、等待上限和消耗，逐项选择关闭、只观察、应用；默认全部关闭。
   界面上每个点是“开启”与“观察模式”两个勾选（关=off，开+观察=observe，开+不勾观察=apply），存储值不变。
   同一页面提供总开关、时间修改、按点覆盖和恢复继承。总开关关闭保留各点原设置，重新开启按原设置恢复，不等于开启所有点。
3. 只观察也会发请求，但不改业务决策；开始前清楚展示请求与输入 token 消耗范围。远端是否收费由供应商决定，宿主不估算决策价格。
4. 可单独测试连接、用有界样本比较策略；测试与保存分开，结果不能只看菜单“保存成功”。
5. 显示配置开启状态、当前服务状态、本次是否采用建议及原因；复用状态和账本，不每轮向历史追加故障提醒。
   TUI 用量沿用现有 LLM 用量行，额外显示决策输入 token，不展示价格；输出位置预留，当前留白。
   底层保留接口原 usage，未知用量不补零；内部期限、调用次数、输入量和窗口验收仍保留。
   接线复用 `conversation/model_metrics.py` 的原账本投影和 `tui_model_metrics.py` 的同一行渲染，
   按可信调用用途区分决策输入，不新建 UI 用量账、不将决策调用伪装成聊天轮次或最近输出速度。
6. 决策配置变化在新请求快照生效；关闭和权限撤销立即阻止尚未应用的旧建议。不能热改已经创建的 child 或已提交的记忆事务。

### 用户操作与 my-agent 代操作必须同等可用

首版就要同时支持用户设置界面和 my-agent 按用户要求代操作，不能把后者推迟到自动调参阶段。
例如用户可以说“把决策最长等待改成 4 秒”“只关闭记忆整理的决策增强”“这一轮先不用决策模型”。
主模型将意愿转换为结构化配置动作；宿主依当前 owner、授权范围、字段校验及版本完成修改，不按普通文本关键词触发设置。

两种入口共用读取、修改、恢复继承与读回服务；若缺模型可调用动作，扩展原配置服务的工具接线，不为每个模块新增控制器。
开关和配置服务不调用 Jev，也不依赖它在线；直接设置界面不依赖生成模型，模型不可用时用户仍能手动关闭或修正配置。

| 用户意愿 | 允许的行为 | 生效与回执 |
| --- | --- | --- |
| 明确修改时间、开启或关闭指定点 | 在既有权限内直接修改，不再为同一明确操作机械重复确认 | 读回有效值、作用范围和下一次生效位置 |
| 仅本任务/本会话暂用某设置 | 复用原有作用域覆盖，不写成长期偏好 | 回执注明临时范围与结束条件 |
| 更改长期设置 | 保存当前 owner 的正式配置，不修改其他用户或管理员上界 | 原子保存，返回新版本及可恢复的前值 |
| “你觉得是否该调整” | 给建议或差异预览 | 不把讨论本身当成写入授权 |
| 明确授权自动调整 | 只在授权字段、时间范围、接入点及请求数/输入 token 预算内调整 | 原账本记录变更来源与实际结果，用户可随时收回 |

用户明确请求开启已配置能力，本身可以构成该范围内的操作授权；不因此授权其他接入点、持续试验或额外模型采购。
未说明作用范围时沿现有配置界面的默认范围并在回执中说明；确实有会影响不同用户/任务的歧义时才补问必要信息。
写入使用 expected revision/原有并发控制，冲突必须重新读取；旧试验和旧指令不能覆盖用户后来手动修改。
采用字段级修改而非整份配置回写，避免切一个开关时抹掉其他设置。成功必须读回，失败保留原有效配置。
变更来源、前后差异和关联用户请求进入现有配置/操作记录，不另建决策配置账本，也不把密钥或整份设置塞进聊天历史。

用户可授权一个有时间、请求数和输入 token 上限的自动试验范围；试验保存当前配置版本、样本范围和实际输入量，产出差异报告。
自动应用只在授权范围内，版本冲突不能覆盖用户中途修改；失败用原配置服务恢复前值，不重放已执行业务。
首版提供用户手动设置、按用户指令代操作和结果观察；主动试验/自动调参放在接入点已有真实收益证据之后。

## 6. 输出合同、标注和缺数据

模型业务结果与请求运行状态分开保存，不能把请求失败映射为“不需要”。

| 业务结果 | 含义 | 消费要求 |
| --- | --- | --- |
| selected | 从本次合法候选中选择 | 只接受存在且仍可用的 ID，通过宿主约束后才能采用 |
| not_needed | 当前子问题无需额外处理 | 仅对允许跳过的可选动作生效，不豁免权限或覆盖用户明确要求 |
| need_data | 缺少指定材料 | 描述缺哪个字段/引用、必需或可替代、允许的数据源和负责补充者 |
| no_match | 存在需求，但没有合适候选 | 保留原行为或报告能力缺口，不越权购买、安装、授权或创建新模型 |
| abstain | 无法可靠判断 | 不强选，沿该接入点的原行为继续 |

运行状态另含关闭、冷却、到期、错误、取消、过期等结构化事实；不是业务选项。
每个子问题独立返回结果；允许部分题目成功，不能用整批平均置信度掩盖一个关键字段无法判断。
输入至少带接入点、owner/thread/run/task/operation 的可信引用、配置/策略版本、候选版本、必要状态与来源。
输出至少带子问题 ID、结果、候选 ID/标注、实际模型版本、输入摘要、时间、用量引用和是否采用。
不同协议的置信度字段各守其义；阈值按接入点的中文样本校准，首版不写死一条通用 0.8 规则。

本地协议快照保留宿主 owner/operation/策略/候选版本和来源 refs；这些绑定不会隐式发送给供应商，
只有消费点明确准备的 state/questions 发出。请求快照建立后，外部修改原 dict 不能换掉候选或输入摘要。
响应只能返回该次题目与候选，不能自行声明身份。合法 JSON 内的缺题/类型/候选错误独立记为题目失败；
重复键、坏 UTF-8、非有限数或整体复杂度超限仍按原 strict_json 边界整包拒绝，不放宽传输合同来保留兄弟题。
当前宿主输入上限 256 KiB、响应上限 1 MiB、深度 16、节点 16384；先检查累计字符串和整数位数再编码，
不靠“先完整序列化再拒绝”防止放大。这些是内存边界，不代表模型 token 窗口验收已经完成。
TypeSafe 支持的 Choice/Score/Noul 原样校验；need_data/not_needed 等含义由宿主显式候选绑定解释，
不能仅靠候选名称猜业务状态。缺失 usage 不补零，输出 token 即使暂不在 TUI 展示也保留原事实。

need_data 的 A/B/C 缺项由候选化声明表达，也能说明“A 或 B 任一即可”；不能默认 Jev 会生成任意补资料计划。
宿主先补已有结构化信息；新数据读取走原工具与权限。无新输入不再询问同一问题，超出阶段预算就停止增强。
若原任务本来就必须由用户补充信息，则由主模型按原流程解释；可选增强自己的失败不能迫使用户不断补资料。

标注不等于长期事实：子代理模型建议交创建入口，工具选择交可见目录，记忆标注交 Curator 原批次输入。
只有主模型需要理解的标注才进入其动态上下文，并带来源/适用范围；固定说明放稳定前缀，动态结果不改旧前缀。
不用把路由细节、错误栈、全部候选或概率表塞给每个模型。工具执行仍需真实 schema，Skill 实际使用仍需相应正文。

## 7. 接入点全表与失败行为

下表列完整接入范围；部分已有默认关闭的首片，完整验收仍逐项见执行 Goal。优先级表示推荐顺序，不代表默认开启。

| 接入点 | 输入与触发 | 结果交给谁 | 不可用/无法判断时 | 批次 |
| --- | --- | --- | --- | --- |
| 子代理执行模型选择 | 创建前，任务摘要、合法模型与能力/窗口元数据 | 原子代理创建参数 | 保留显式模型或父级继承；显式错误仍按原合同报错 | 首批 |
| 记忆整理前标注 | 原 Curator 已收集的批次、来源和已有记忆引用 | 原提取请求中的临时注释 | 原始批次照常整理，未处理数据不推进游标 | 首批 |
| 召回后筛选/重排 | 原检索后的授权候选与当前问题 | 原上下文打包入口 | 原排序及原预算 | 第二批 |
| Skill/tool 推荐 | 工作片/派工阶段的合法索引与任务需要 | 原快照和按需发现入口 | 原发现机制；不能返回空权限假装没有工具 | 第二批，等插件接口稳定 |
| 召回前判断 | 当前问题的有限查询片段、原召回已选事实和剩余预算 | 原正式上下文准备入口 | 原召回全保留；不跳过用户要求的历史 | 后续，补充查询首片已本地接入 |
| 记忆候选分类/合并建议 | 提取候选、证据与冲突引用 | 原验证/晋升流程的提示输入 | 原验证与晋升；不因分数直接入库、删除或修改人格 | 后续 |
| 检索与外部材料排序 | 原工具返回的有来源候选 | 主模型或检索模块 | 原结果分页与读取方式 | 后续 |
| 规划与派工建议 | 当前原 Todo read 的精确 open ID 与本轮问题 | 原工具回执中的软提示 | 主模型按原规则规划，不自动制造子代理或 Goal | 后续，现有 Todo 优先级首片已本地接入 |
| 页面/工具动作候选 | 插件只读观察工具返回、宿主铸 ID 的结构化候选（DOM 已有 browser-lite；OCR/computer_use 尚未声明观察） | 原工具回执后的软提示，候选 ID 由主模型自行决定是否使用 | 原主模型判断，原审批、宿主新鲜度复核与插件代次复核保持 | 后续，`action_candidate` 已接入并用 browser-lite 真实验收（默认关闭） |
| 交付质量提示 | 当前产物引用与原工具证据 | 主模型复核上下文 | 原收尾；不增加强制续跑或完成评分门 | 后续，交付复核焦点首片已合入并做过真实样本（无收益证据） |
| 自学习候选筛选 | 原 runner lesson Candidate 与可信来源；正式 Skill 另需用户授权 | 唯一 Skill 提案/确认入口（S1 已合入 main `e9ead5ae3`）上待确认提案的审核顺序 | 原候选与提案不变；Jev 不能生成正文、确认、拒绝或写正式 Skill | 后续，S1 入口已合入，S2 审核顺序点 `skill_proposal_review` 已本地实施待审（默认关闭） |
| 主会话自动换模型 | 用户授权的候选范围和新工作片 | 原会话模型选择服务 | 保持当前选择，不切正在请求的模型 | 最后评估 |

“记忆整理前标注”首版只加标签和优先级，不丢弃原始材料；召回前跳过与长期写入判断后置，避免早期错误造成静默遗漏。
P5-A 的首片只允许默认关闭的 `pre_recall` 建议一次有界补充查询：原完整问题先检索，追加项仅用原 scope 与未用的 `memory_top_k`/字符空间；P3 排序和该点共用阶段期限。普通聊天没有可信的显式查历史结构化意图，因此 Jev 不能决定跳过原记忆。真实 Jev 的漏召回、时延和输入用量尚待隔离对照，见[审计与实施记录](../tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md)。
P5-C 规划首片只在当前主代理读取已有多项 Todo 时追加一个 exact ID 的软优先提示；原计划/Goal/派工权威不变，真实 Jev 质量尚待验，见[交接](../tasks/DECISION_MODEL_PLANNING_HANDOFF.md)。
自学习点的前置合同见[只读审计](../tasks/DECISION_MODEL_SELF_LEARNING_AUDIT.md)。S1 提案/确认链已合入 main（`e9ead5ae3`）：`enable_self_learning` 默认关闭；开启后 runner 结果先记录 lesson Candidate，再由 `capability/skill_proposals.py` 为 `subagent_lesson`、带精确 task/run 来源的候选按固定模板生成提案，O_EXCL 幂等写入 `<owner_home>/data/skill_proposals/`（不用会被 Curator 迁移清理的 `learning_drafts`），生成失败只记工作日志。只有用户 `my-agent skills proposals confirm <id> --expected-revision N` 能在 owner 锁内复核版本、草稿 hash、来源 Candidate（未脱敏、hash 未变、状态有效）与目标不存在，并通过 `parse_skill_file(require_frontmatter=True)`、`scan_skill(agent_generated)` 与不 force 的 `install_decision` 后，把 Skill 原子装到 `<owner_home>/skills/lesson-*`；失败不写目标、提案保持待确认。2026-09-26 起按用户决定（自学习不逐条审批），自学习开启时新提案立即以 `confirmed_by=auto` 走同一确认链，主代理任务另有自动总结 Skill（S3，见[自动总结 Skill 设计](SKILL_AUTO_SUMMARY.md)）；上面的“只有用户能确认”描述的是 S1 当时语义。S2 已合入 main（`1132fd9d0`，见下文“P5-C 自学习 S2”），现在只对遗留的待确认提案生效：独立决策点定名 `skill_proposal_review`（审计里暂称 `self_learning`，改名以表明它只管待确认提案的审核顺序），默认关闭，Jev 只能给已存在的待确认提案排审核先后，不能生成正文、确认、拒绝或写 Skill。
如果用户显式要求一次 Jev 分析而服务不可用，应明确报告该分析未完成；不能拿普通模型结果冒充 Jev。

### P5-B 第一片：提取前的来源—正式条目关系建议

当前状态：第一片已本地实现并通过离线合同与原 Curator 组合验证，未部署；本片不等同于完整的候选分类或语义合并功能。
要解决的问题是：原 Curator 已同时拿到新来源与有界正式条目，但可能遗漏二者间的重复、更新或冲突关系。
增加独立 `curator_relation` 接入点，默认 `off`，只允许 `owner_background` 用户长期设置；
沿原 `annotate_batch` 的同一阶段和绝对 caller deadline，与现有标签/优先级共享后台时间，沿原模型账本计量。

- 只比较本批已授权的完整消息与完整、短正文、带原仓库版本的 active long-term 条目。
  消息按原 UTF-8 哈希核对；正式正文按原规范化哈希、精确 authority ref/ID 和 `MemoryRecord.version` 绑定。
  正式条目宿主元数据可增加版本/完整长度，但原 `to_model` 投影和默认关闭时的提取请求字节保持不变。
- 每个来源—条目对独立判断 `possible_duplicate`、`possible_update`、`possible_conflict`、`no_match`、
  `need_data` 或 `abstain`。提示明确只覆盖被展示的这一对，不得外推成全库无冲突或已经完成合并。
  条目数/字符/协议输入上限仍是本地资源边界；未比较来源和正式条目完整留在原批次。
- 缺版本、缺正文、截断、audit 大输出覆盖未知、正式读取失败或超预算时，返回结构化 `need_data` 诊断或保留原批次，
  不凭短预览判重；首片 lesson/HOT 也保持原链，不伪造版本。没有额外补读工具或新后台任务。
- 采用前重新读取原 owner long-term 仓库，精确核对正式引用、正文哈希和版本；原批次/配置/期限变化均使建议失效。
  结果只增加临时 prompt 注释；缩批或来源/正式绑定不匹配时不发送旧注释。
- 原提取、`validate_extraction`、`_prepare_outputs`、CandidateService 和 Promotion 继续生成候选身份、证据与动作，
  建议不能直接改候选类型/action/target、晋升、删除、覆盖人格或推进游标，也不新建候选 store。
  SOUL 确认、原证据门、精确目标和版本 CAS 均保持。

验收先用 fake 决策和真实本地记忆仓库：默认关闭字节相同、独立开关/作用域、完整关系阳性、缺资料保留、
版本/正文/批次变化失效、同阶段期限、原缩批及提取失败不提交；模型语义质量另做少量真实验收，不能由离线合同测试推断。

### P5-C 第一片：已归档网页的临时阅读顺序

`external_material_order` 是独立 thread 接入点，默认关闭。仅原 `web_fetch mode=extract` 成功取得并归档多个页面，且原 ToolCall/ToolResult、run/task、归档哈希、`external_data/default` 安全投影均配对时才准备决策。当前只对原 `title/preview` 和本轮问题作有界安全投影；原 URL、查询串、调用参数、headers/body、artifact 正文和路径不发送给决策模型。元数据不完整、超期限、连接故障或资料不足时仍返回原工具展示。

完整合法的建议只把原页码排序作为可忽略的文字附在已归档结果之后；原工具结果、页序、失败项、引用、账本、权限及 text/native IR 的执行事实保持。`observe` 仍会发请求但不附提示；用户取消传播，设置关闭和来源变动令在途建议失效。首片本地生产/归档/设置组合 349 项通过；隔离真实 Jev 的一组非选择保留原展示、另一组自动追加2→3→1，但页面源为本地受控材料，不能外推实际检索质量。`web_search`、本地检索、历史检索、规划、工具动作、质量和自学习点不由这片冒充完成，详见[P5-C 交接](../tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md)。

### P5-C 质量提示首片：交付复核焦点 `delivery_quality`

当前状态：已合入 main（`8c6d29c5f`），默认关闭；离线合同、fake 后端组合与变异验证通过，2026-09-25 两个真实 TUI 任务各跑 off/apply，链路成立但没有实际追加提示，不证明交付质量提升（见[真实验收](../tasks/DECISION_MODEL_REAL_VALIDATION.md)第 15 节）。依据是[只读审计](../tasks/DECISION_MODEL_DELIVERY_QUALITY_AUDIT.md)的最小安全接缝：首片只用验证事件做候选，不用 artifact ref。

要解决的问题：一轮里跑过局部测试、改过文件、又跑全量测试后，主模型可能只盯最新一次结果，漏掉更早的失败或“其后有修改”的旧验证。此点在一次 `run_command` 刚产生新验证事件后，可选地请 Jev 从本轮已有验证焦点里挑一个“交付前最值得先复核”的，宿主把它渲染成一句可忽略的提示。它不是完成评分、验收门或测试命令生成器。

- **接线**：独立 thread 接入点，AgentConfig/YAML 三字段 `decision_delivery_quality_mode/_timeout_seconds/_profile_id` 默认 off/null/null，原 owner/thread 设置服务、`user_config` 工具和 TUI 菜单（“交付复核焦点”）共用。`_tool_loop_service._record_tool_call` 在原归档、账本写入之后调用 `_optional_result_hints`，依次调用 `external_material_order_hint` 与 `delivery_quality_hint`；两点按工具名互斥，每条记录至多一次决策请求，提示追加到 text/native 共用的同一 `result_rendered`。
- **触发（全部结构化）**：当前工具是 `run_command`、`handler_executed`，结果带验证事件且与归档信封中的同一事件一致；归档与调用在 tool/id/run/task/scoped_call_id 上一致且以同一对象位于本 run 的 `archive_tool_calls`；主代理（无当前子代理 run）；无重复失败或未知副作用收口标记；`user_prompt` 非空且不超过 1,024 字符。焦点取同 run/task 的 `run_command` 归档（`&&` 串联整体通过时读信封里完整有序的 `verification_evidence_chain`，其末项必须等于 `verification_evidence`，不一致即放弃），每个 (root, kind, scope) 只留最新一条，同一事件编号重复出现即放弃；其后同 root 出现 `verification_state.status=stale` 记为 `edited_after`。只有 2—12 个焦点且至少一个 failed 或 edited_after 才准备材料；非 run_command 与身份不符的记录在扫描归档前即返回。
- **材料上限**：外发 state 只有经外部材料首片同一 `external_data/default` 脱敏、含 URL 查询串即放弃的当前请求，以及 `focuses: [{candidate: focus_i, project: project_j, kind, scope, status, exit_code, edited_after, order}]`；项目根只以 `project_j` 别名出现，本地路径、原命令、工具输出、改动路径和时间只进本地版本摘要。唯一单选题 `review_focus` 的候选是 `focus_i` 与 `not_needed/no_match/abstain/need_data`，`need_data` 明确“不补读、不跑测试”。
- **采用与回退**：只接受一个无逐题错误的 choice 回答且值为本次宿主生成的 `focus_i`；渲染只含宿主事实，例如“交付前可先复核本轮验证事件 #11（test/targeted，failed，其后有修改）；范围与结果以原事实为准，targeted 不代表全量”，上限 512 字符。选中本次调用自己的事件不追加。`off` 不准备材料、不发请求；`observe` 照常请求并记原账但不追加；非选择、坏答案、超时、错误、冷却、配置或来源变化（采用前在 `decision_outcome_is_current` 之后重比参数与材料版本及同一绝对期限）都只返回空串。ToolResult、归档、验证账、Goal、Todo 与收口从不修改；用户取消与中断照常上抛。

### P5-C 动作候选 `action_candidate`

当前状态：已接入（随分支 `claude/decision-action-candidate` 合入，基于 main `6a50d84aa`），默认关闭。新鲜度只问插件线的 `plugin_observation.observation_is_current`；离线合同 82 项、19 种变异全杀。2026-09-25 在隔离 owner 上用 browser-lite 完成 off/observe/apply/过期四档真实验收，见[真实验收](../tasks/DECISION_MODEL_REAL_VALIDATION.md#15-动作候选-action_candidate-的-browser-lite-真实验收2026-09-25)。前置结构见[插件观察候选结构](PLUGIN_OBSERVATION_CANDIDATES.md)。

要解决的问题：插件的只读观察工具（如 browser-lite `read`）一次返回多个可操作对象时，主模型可能先去操作不相关的那个。此点在观察结果归档之后，可选地请 Jev 从宿主铸造的候选里挑一个"下一步最值得先核对的"，宿主把它渲染成一句可忽略的提示。它不执行动作，也不生成参数、选择器或坐标。

- **接线**：独立 thread 接入点。AgentConfig/YAML 三字段 `decision_action_candidate_mode/_timeout_seconds/_profile_id` 默认 off/null/null，与原设置服务、`user_config` 工具和 TUI 菜单（"动作候选"）共用。`_optional_result_hints` 依次调用三个点，按结构化触发事实互斥（web_fetch 归档 / run_command 验证事件 / 插件观察信封），每条记录至多一次决策请求。
- **触发（全部结构化）**：
  - 当前调用成功执行（`handler_executed` 且 `ok`），归档与调用在 tool/id/run/task/scoped_call_id 上一致，信封带 `observation`；
  - 主代理、无收口标记，`user_prompt` 非空且不超过 1,024 字符；
  - 观察形状合规：宿主铸的 `obs-`/`cand-` 编号，短标识 `target_kind`/`role`，本地目标事实齐全，1—64 个候选且编号不重复，label 不超过 120 字，每个候选 1—8 个动作工具名（宿主注册名）；
  - 候选至少 2 个，至少一个候选的动作工具在本轮快照中可用；
  - 新鲜度权威 `plugin_observation.observation_is_current`（owner 权威库 `agent.subagents.runtime_db`，按 run/task 归属）确认仍为当前。

  任一不满足就零请求。
- **材料上限**：外发只有脱敏后的当前请求、`target_kind`，以及放在同一个 `external_data` 块里的候选别名 `c_i`、role 与 label。候选编号、插件 key、目标引用、代次和动作工具名只进本地版本摘要。唯一单选题 `next_candidate` 的选项是 `c_i` 与 `not_needed/no_match/abstain/need_data`。label 或请求里含 URL 查询串时整点放弃。
- **采用与回退**：
  - 只接受一个无逐题错误的 choice 回答，且值是本次生成的别名；所选候选的动作工具须在本轮可用。
  - 采用前依次复核 `decision_outcome_is_current`、参数与材料版本（这一步会再问一次新鲜度权威）以及同一绝对期限。
  - 提示只含宿主铸的 candidate_id 与 role，例如"下一步可先核对候选 cand-…（button）；是否操作、如何操作仍由你按原工具与审批决定"，上限 512 字符。
  - `off` 不准备材料；`observe` 照常请求但不追加；其余失败只返回空串；用户取消与中断照常上抛。
- **待插件线**：`plugin_observation.py` 落地后改用真模块回归，再在隔离 owner 上用 browser-lite 做 off/observe/apply 真实验收。

### P5-C 自学习 S2：待确认 Skill 提案的审核顺序 `skill_proposal_review`

当前状态：本地实施（分支 `claude/self-learning-proposal-review-order`，待审），默认关闭；离线合同、真实 CLI 组合（只替换 HTTP 发送）与变异验证通过，未做真实 Jev 验收。

要解决的问题：S1 之后，子代理经验会陆续积累待确认的 Skill 提案，用户逐条审核时不知道先看哪条，也不容易发现可能重复的提案。此点只在用户执行 `my-agent skills proposals list` 时，可选地请 Jev 给每条待确认提案一个审核先后建议，宿主据此重排列表并附固定标签。它不是审核门，不决定确认或拒绝。

- **接线**：独立 `owner_background` 接入点，AgentConfig/YAML 三字段 `decision_skill_proposal_review_mode/_timeout_seconds/_profile_id` 默认 off/null/null，超时留空继承 `background_timeout_seconds`；只有用户长期（owner）设置可写，线程覆盖被原设置服务拒绝，线程菜单不显示。配置放 AgentConfig 而不放 CapabilityConfig：此点只排展示、不授予 Skill 或工具权限，和 `enable_self_learning` 同属主配置；CLI 也只加载主配置，放在这里用户改 YAML 才真正生效。TUI 菜单名“Skill 提案审核顺序（用户长期）”。
- **触发**：`skills proposals list` 列出的提案中，待确认的有 2—30 条，且本点为 observe/apply、决策总开关开启。0—1 条、本点关闭或总开关关闭时零请求，输出逐字节不变（已与 origin/main 的 CLI 子进程逐字节对照）。不要求 `enable_self_learning` 开启：该开关只管自动生成，已有提案在关闭后仍可审核。CLI 没有会话或 runner，宿主以 owner 路径和配置作决策宿主，每次生成一次性 run 编号，以 owner_background 身份建阶段；同一阶段期限覆盖准备、发送和采用。
- **外发材料**：每条待确认提案只给 `proposal_i` 别名、创建顺序、来源任务数和运行数，以及 description、when_to_use 与经验段前 240 字摘录。三者整体经 `external_data/default` 投影（脱敏加不可信数据边界）。经验摘录按 S1 固定模板的“经验”段截取，不含来源段。提案编号、路径、owner/候选/任务/运行编号和目标 Skill 名只进本地版本摘要。按原渲染函数重算的草稿 hash 与记录不符时整点放弃、零请求。30 条最长草稿仍在 Jev 单题窗口门内（估算约 25.7k/28.8k token）。
- **题目与采用**：每条提案一道 choice，候选为 `review_first/normal/review_later/possible_duplicate`，非选择为 `not_needed/need_data/abstain`；说明写明只是审核顺序，不决定确认或拒绝。只接受恰好每题一个合法候选；缺题、多答、错题号、逐题错误或任一非选择都整体保留原序。采用前依次核对：候选版本、`decision_outcome_is_current`、重读待确认提案（编号、版本、状态、创建时间、记录和重算的草稿 hash 全等）、同一绝对期限。然后稳定排序：review_first → normal → review_later/possible_duplicate。可能重复与稍后同组，便于先看原提案再对照；组内保持原创建顺序。只替换原列表里待确认提案所在的位置，已确认/已拒绝的条目位置不动。每条附宿主固定标签“建议优先审核/建议稍后/可能与其他提案重复”，不复制模型文字，列表首行下加一行说明。`--json` 增加 `review_order` 块：point、mode、status=applied，以及别名→proposal_id/suggestion/label 的映射。全部为 normal 时顺序不变、没有标签，但仍标明已按建议排列。
- **边界**：observe 照常请求并记原调用账（CLI 进程内），输出不变；错误、超时、冷却、配置或来源变化都保留原输出。用户取消与中断照常上抛，CLI 以 `SKILL_PROPOSAL_CLI_INTERRUPTEDERROR` 结束，不打印列表。本点只调用 `SkillProposalService.list`，从不确认、拒绝、改写提案或 Skill，也没有模型可调用入口；测试证明提案目录、skills 目录与候选账本逐字节不变。

### P5-G 基础原语：同次事务恢复设置覆盖

自动试验将来若需恢复原设置，`patch` 后另发一次 `reset` 会暴露中间值，也可能在两次写入之间覆盖用户的新修改。原设置服务增加仅供宿主使用的 `restore` 操作：一份完整 owner/thread `expected_revision`、一组 `set` 原覆盖及一组 `unset` 原本未覆盖的字段，在原 owner→thread 锁序内一次校验、一次提交、一次正式读回；成功版本只前进一次。`set` 仍按原字段范围和模型引用校验，`unset` 只清已登记字段，交集或空操作拒绝。任何用户或其他任务先行写入都使旧版本冲突，本次恢复不写文件；已删或撤销的模型引用不被旧快照复活。

这只是可供未来实验使用的恢复原语，不等于已有自主试验、实验授权、请求前预算、收益判断或自动回滚策略。未来实验必须保存原操作回执与精确前值，且不能回滚已经执行的业务副作用。用户授予一次范围和预算后，范围内的试验、应用与失败回退由宿主自动执行，不逐步询问用户。

### P5-E1 有界自测：授权入口、经验输入上界与发送硬门（2026-09-24，本地实施待审）

用户于 2026-09-24 批准接受**经验（非供应商保证）**的输入上界，要求明确标注。实现分支 `claude/decision-experiment-send-gate`；`experiment_enabled` 默认仍关闭，首次真实授权发送尚未进行。

- **授权入口**：`/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>` 与 `/audit 名称 prepare` 共用任务命令机制；HTTP `/ask` 丢弃客户端 `system_task`，只从已鉴权正文重推并冻结进排队请求，模型只见任务正文。只开放 `observe` 与已标定的 `skill_tool`。主轮在 `_bind_main_agent_turn_params` 发布 run/attempt 后、首个模型调用前，由 Gateway 写入器在同一 `GatewayActiveTurnTransition` 内写 `experiment_grant=granting`，以 `HostCommandIdentity`（owner、请求 user_id、channel、thread、request_id）调用 `authorize_decision_experiment`，写回 `granted(authorization_id)` 或 `rejected(code)`；失败只提示用户，不阻断业务。回执存在即不再授权；Compact 再入换 attempt 得 `experiment_identity_changed`，重启换账本代次得 `experiment_ledger_changed`。模型只有 `user_config` 读/撤销，不能授权。
- **信封 v2**：必带 `input_bound_policy="empirical:jev_wire_bytes.v1"`，这是用户接受经验上界的结构化记录；v1 与未知口径可读可撤销，但准入分别返回 `input_bound_policy_missing` / `input_bound_policy_unsupported`，永不发送。
- **经验上界 C**：`jev_empirical_input_bound()`（Jev wire 适配器）以最终 wire 字节 B、题数 Q、state wire 字节 S 计算 `C = ceil(B/2) + 256×Q + 1024`；只在 skill_tool、Q≤64、S≤4096、C≤57,600（0.9×64k）内使用，越界 `input_bound_out_of_calibration`，不预留不发送。标定：4 次真实 64,921–65,063 字节/27 题请求计费 17,352–17,383（约 2.3 倍余量），官方示例 173 字节/1 题计费 296（C=1,367）。常量是版本化代码方法，不是配置。普通请求与上界共用唯一编码器 `gateway_request_body()`，普通字节不变。
- **原账**：`reserve_input_budget` 只接受 `InputTokenBound(kind="empirical", …)` 并同时签发发送绑定；调用记录存 `input_bound`，预算快照存 `input_bound_kind`，调用的 `input_tokens` 仍是估算。
- **发送硬门**：`GatewayRequest.send_permit` 存在时强制零重试、禁重定向、有期限；`_gateway_request_attempt` 在最终 `req.data` 生成与期限复核之后、`started` 遥测与任何 DNS/连接之前调用 `admit`，不包 try/except。许可按“静态绑定→中断/设置撤销/期限→复读设置重跑准入并比对策略与当前连接代次（不复用把 off 当关闭的 `_stale`）→原账锁内单次消费”复核；拒绝抛 `ProviderSendRefused`，决策服务映射为 `send_refused:<code>`，不触发连接退避。
- **结算**：终态写入后结算。成功按 provider 实际输入扣减并记比例（>0.8 仅警告，>C 关闭为 `input_bound_violated`）；许可未消费且零 HTTP 记 `refused_before_send` 并以 `send_refused` 关闭，不退款；有 HTTP 却无已消费许可记 `gate_bypassed` 并关闭；其余保留一次 HTTP 与整个 C，关闭为 `usage_unknown`。迟到 HTTP 只追加。实验结果恒为 observe（`may_apply=False`），只在该点普通模式为 off 时运行，只写 finding 与能力推荐观测（mode=observe、adopted=false），不改变模型可见输入。

边界：经验上界不是供应商保证，只覆盖已标定的 skill_tool 形态；本地 TUI 直连模式和后台执行没有授权钩子，不会发送实验；本机文件队列属于同一 owner 的可信通道（与 `/audit` 准备轮相同）。细节与验证见 [E1 交接](../tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第二片experiment-授权入口经验输入上界与发送硬门2026-09-24)。

### P5-E2/F1 对照记录、证据评估与授权内自动晋升（2026-09-25，本地实施待审）

要解决的问题：真实运行里一次只观察实验的结算快照只在内存原账里，回合结束即丢，留不下“基线对候选、结果、来源、配置版本”的持久记录；也没有任何环节能把实验证据变成设置建议，或在用户授权内应用它。

- **E2 对照记录，只写原权威**：没有 token 表、旁路恢复文件或第二本账。`settle_input_budget` 的返回值改为结算视图（预算快照加本调用的结算码、调用编号、原估算输入和声明上界），经 `DecisionExperimentCall.settlements` 带回决策服务，挂到 `DecisionOutcome.experiment`（普通调用恒为 None）。只观察路径据此生成 `decision_experiment_record.v1`：身份 refs（owner/thread/run/task/attempt/request/operation/call）、授权编号、设置两层 revision、策略与连接版本、候选版本、输入上界口径；**基线**=点关闭时原 Registry `model_visible_specs` 实际展示的工具（数量、名称集合 sha256、前 64 个名称）；**候选**=按 apply 同一规则（`selected_capabilities`+`_project`）投影 Jev 回答后的短名单/延迟名单（各至多 256 个，超出显式标记截断）与 Skill 计数；**结算**=原账返回视图的白名单字段。条目经能力观察出口拆出，在同一请求记录 `experiment_records.entries` 内写入：record_id=原调用编号去重、最多 8 条、盖 Gateway 执行代次；写入走精确回合转换锁（回合关闭/停止时抛中断、不写），其它写盘失败只放弃这一条。只有进入过原账预留的调用才有记录。
- **实际用量**：`_execute_gateway_conversation_turn` 在模型回合正常返回后、持久化答复前调用收尾：只有宿主协议结束原因为 completed 且结构化工具账 `archive_tool_calls` 每条都有工具名时才记 `known=true` 与去重工具名（前 64 个）；其它记 `known=false` 与原因码，不从正文补猜。只补写本执行代次的条目；停止/关闭的回合不补写，异常只记日志、不改变本轮结果。普通请求在观察与收尾处都零 I/O、不写任何键。
- **F1a 只读评估**：`conversation/decision_experiment_evaluation.py` 只读这些条目并逐条核对 owner/thread。样本=最近 8 条已完成条目；可比较=结算 charged、实际用量已知、候选已投影且召回可算。短名单召回=实际工具中在短名单的比例，分母只含本次快照内的工具（短名单或延迟名单中的），快照外工具不计；名单截断而无法归类、或没有可归类工具时召回未知（样本不可比较，但不阻断）。规则四条同时成立才提出 `points.skill_tool.mode off→apply`：可比较样本≥3、窗口内每个样本 charged、每个可比较样本召回=1.0、每个可比较样本延迟数>0；否则 `keep_observing` 与原因码。常量属于已审计规则，不设配置项。跨请求证据只沿授权回执 v2 的 `previous_request_id`（授权时被替换信封的来源请求）回读原请求记录，至多 16 条，每条须是本会话 granted 回执，编号按文件名规则校验。`user_config decision_read` 在会话已有授权信封时附只读 `experiment_evaluation`（插在信封之后），模型可见；没有信封时输出逐字节不变。
- **F1b 授权内自动晋升**：`/experiment apply skill_tool …` 与 observe 同一语法，信封 operations 为 `["observe","apply"]`（仍是 v2；旧版程序无法读取含 apply 的信封，撤销与 reset 都不删除信封）。实验调用本身仍只观察；apply 只授权宿主在回合收尾时晋升：锁外只读评估，锁内复读设置，核对授权仍是本请求那份、身份一致、active、含 apply、未到期、设置 revision 与授权时一致、能力仍开、点有效模式仍为 off，再经原设置服务 `patch`（thread 范围、`expected_revision` 取锁内读数，完整 CAS）写 apply。冲突或任何用户后改（线程/用户层修改、默认配置改变有效模式、撤销、到期、被新授权替换）都跳过、绝不覆盖。
- **回执权威选请求记录**：`experiment_records.promotion` 与触发它的证据同处一份原请求记录；授权信封是纯授权、会被下一次授权整份替换，把回执写进它还会改变信封 schema 与发送门的读取者。晋升先写 `promoting` 再改设置，已有任何回执即不再尝试（内存快判加锁内复核，重放与重启幂等）；崩溃遗留的 `promoting` 表示结果不确定，不重试、不反向恢复。回执含状态/原因码、目标字段、证据摘要（记录编号与计数）以及前后值（线程覆盖是否存在及值、有效模式、两层 revision）。到期或撤销不回滚已晋升设置，需要恢复继承时对 `points.skill_tool.mode` 执行 reset。
- **不变的边界**：Jev 回答只经宿主投影成候选名单，不能直接改变评估或触发晋升；模型没有授权或晋升工具路径（`user_config` 仍只有读取/撤销实验授权）。节省只按延迟数计，工具 schema 字节随协议序列化而变，不作为结构化事实记录。

### 决策开关、超时自调、统一审计与管理员管控（2026-09-25，本地实施待合入）

六项用户要求一次落地：接入点“开启/观察模式”两个勾选、my-agent 在上下限内自调等待时间、选模型输入精简
（摘要去原文锚点段并限长、候选公共声明只写一次，约减 63% 输入）、统计行“决策 ≈N token · 成功 X · 失败 Y”、
统一只读审计工具 `audit_records`（本人范围，管理员明确许可才可跨用户）、管理员专用 `admin_controls`
（每用户 Jev/审计开关，Jev 禁用在唯一调用入口硬拦）。详见[决策开关、超时自调与审计](DECISION_AUDIT_AND_ADMIN_CONTROLS.md)。

## 8. 模型窗口、缓存与输入用量

### 模型选择先满足客观条件

候选先由宿主按授权、协议、模态、工具支持、输入/输出窗口和显式用户约束过滤，Jev 再比较语义匹配。
容量、时间、计数等确定性事实由代码判断；元数据未知则不能伪造可用容量。
请求预算覆盖系统内容、工具 schema、历史、材料、预留输出及协议需要的推理容量，并按目标模型计数/估算规则执行。
观察到的 token 数和校准信息绑定原模型配置版本，不把一个 tokenizer 的数直接当另一个模型的实际值。

400K 材料不能直接送进 250K 窗口。优先为 child 准备足够的任务材料和原文引用；必要时按既有 Compact/分段链处理。
若必要信息仍放不下，排除小窗口候选或让原主模型拆任务，不截断用户要求、工具配对记录或原生推理块来强行通过。
1M 窗口也不意味着每次应该发送 1M；Jev 自己的输入上限独立检查，仅接收本次判断需要的状态。
子代理创建前选择已接线；后续主会话 Stage C 仅在准确 Gateway 新工作片的首次完整请求上尝试一次自动采用，
而不是每轮重选。原完整 prompt、工具、历史及候选 provider payload 验证后，才可在真实发送前以原目录代次、
准确车道和 thread CAS 提交；未知模态/容量保留原模型。text/tool 容量采用 UTF-8 字节加协议余量的工程估计，
不是供应商 tokenizer 的精确保证；供应商拒绝仍走原 Compact/错误路径，不跨模型重发。采用模式的问题说明照实描述宿主行为（完整首请求准备后独立核对容量、工具与版本，通过才采用，否则保持原模型），请决策模型按任务语义挑最合适的候选，当前模型同样合适时选 retain_original；观察与采用两种模式共用同一句用途标签说明。真实主会话跨模型自动采用已在测试机验证（325k 字任务由 M2.7 自动换到 M3 并答对），见[主会话真实交接](../tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。
候选材料可带用户在模型档案里显式填写的用途标签 `usage_tags`（开放的小写标识符，例如 long_document、vision、low_cost）。主会话与子代理两处候选都只在填写了时带上，问题说明写明它只供语义匹配参考，不是能力、容量或授权证明；宿主不据此路由，采用仍须通过上述完整请求验证。标签只存于模型目录（`validate_model` 登记、读取重校验），不进 `resolved_model`、也不进 AgentConfig；子代理候选的版本摘要包含标签，改标签会使在途建议失效。

### 三种数据分清

1. 原历史/记忆：唯一事实源，关闭增强不能删除、搬迁或不可读。
2. 决策结果复用：先复用原 operation/run 已保存选择，保证重送不重选；需要性能缓存时再加有界进程缓存，不建第二份持久历史。
3. Provider prompt cache：保持稳定前缀和追加式事实，实际命中以 usage 为准，不保证跨模型或跨供应商共享。

局部结果缓存键必须包含 owner/权限范围、接入点、输入摘要、候选与配置/策略版本、实际模型版本；动态数据另受新鲜度约束。
命中后仍复核权限和启用状态，不重用失效授权；失败不能缓存成 not_needed，关闭则不应用旧缓存。
首版先保证幂等和前缀稳定，不把复杂缓存当接入前置依赖；有重复样本证据后再启用性能缓存。
Skill/tool 可见集尽量在工作片或任务阶段边界确定，能力缺口再增量发现，不为了每轮省几个 token 反复改整个前缀。

验收看完整任务的输入 token、缓存读写、普通模型用量、交接/压缩、返工及延迟；不能只看一次决策调用。
缓存命中的 token 仍占上下文。没有实际 usage 的决策输入保持未知，不能报成零或宣称一定节省 token。
决策并发、请求次数、期限与输入量受原资源和预算合同约束；观察和自动试验同样记录用量，不能挤占主任务的全部可用资源。

## 9. 分批开发与交付门

| 阶段 | 解决的问题与交付物 | 验收后才能进入什么 |
| --- | --- | --- |
| P0 计划与接线清单，本轮 | 本文、导航、代码复用表、默认预算、责任边界；实施前确认精确文件/函数和稳定基线 | 启动实现 |
| P1 配置与最小请求链 | 扩展原模型角色/配置，用户与 agent 共用的时间/开关读写及读回，Jev 适配、结果协议、总期限/零重试、取消、原账本；全部消费点关闭 | 配置等价与假服务超时/故障/隔离通过后才接真实业务 |
| P2 两个首批接入点 | 子代理创建前选模型；Curator 提取前标注；各自 off/observe/apply 和原行为 | 同一用例关闭、正常、失败三路对照通过 |
| P3 召回和能力推荐 | 先召回后重排；插件目录稳定后接 Skill/tool 推荐；按需缓存 | 证明没有漏必要信息、失效权限和明显缓存退化 |
| P4 用户完整操作与组合验收 | 原模型菜单和配置服务完整接线，实际 TUI 增删/切换/关闭、真实故障对照、中文质量及输入 token 证据 | 发布候选及逐项开放使用 |
| P5 扩展与受控自调 | 召回前判断、写入建议等逐项验证，再提供有预算的试验与自动应用 | 每项独立开关、基线和收益证据，不打包全开 |

P1 即应能通过用户设置和受权 agent 动作设置、读取时间/开关，不把基本代操作能力留到 P5；P4 补齐可见操作和真实集成体验。
不按预估天数宣称完成；每批以具体证据通过为完成点。某点效果不好，可以保持关闭，不阻塞其他点或基础产品。

## 10. 与另一任务并行的边界

已收到“模块重构”的主动归属通知，并只读核实原工作区 HEAD 4974fe760ccf7c4b1b14c9e96b127c157ba67ac8 当时干净。
对方当前负责插件显式命令的 TUI/Gateway 交互审批，包括 cli/chat_parts/ 下的 plugin_command_client.py、
tui_plugin_commands.py、tui_params.py、slash_command_types.py、tui.py、slash_commands.py、tui_runtime.py，
以及 agent/gateway_parts/plugin_command_service.py、新命令传输模块和对应测试。
本线已回复：当前只做独立计划，产品代码认领为空，不接触上述文件，也不要求对方等待或改变 Goal。
原工作区的 CODEBASE_TREE、DESIGN_LEDGER、TESTS、CODE_SIZE_REPORT 由对方统一维护；本线导航差异仅在独立 worktree。
计划集成时发送基于新 HEAD 的精确段落，不能把旧版共享文档整体覆盖回去；LLM_GUIDE、ROADMAP 和设计索引也须先分区。
用户现已授权 P1—P5 实施，对方已确认 P1-A 配置/后端范围无重叠；完整共享入口仍按切片逐项认领，见执行 Goal。
原十步重构文档明确 Jev 属于独立范围；本计划不把它追加到对方 Goal 的完成条件。

| 范围 | 拟负责方 | 并行方式 |
| --- | --- | --- |
| 插件启停、调用、HostCommand、审批、注册和当前真实装卸验收 | 原“模块重构”任务 | 保持原进度；本线不编辑它当前的 plugin_*、runtime_db/host_command*、tooling/plugin_registration.py 及相关测试 |
| 决策协议、适配、预算、专用合同测试 | 决策接入线 | 独立 worktree；可以与插件工作并行，公共 HTTP 入口先确认文件/函数归属 |
| Curator 标注与召回 | 决策接入线或独立记忆实施者 | 复用 P1 接口，在原记忆模块内开发；不改 Gateway 调度和工具审批 |
| 子代理模型接线 | 单一共同确认的实施者 | 纯选择与测试可先做；对方进入第 7 步生命周期整理时，共享 create_policy/model_scope 等入口必须串行落地 |
| Skill/tool 接线 | 等原插件目录接口稳定后再接 | 不复制 Registry 或维护临时目录；等待期间推进记忆与离线验收 |
| TUI、配置公共服务、原账本/HTTP、主文档索引 | 每批指定一个集成负责人 | 两边提供精确变更需求，避免同时搬文件或覆盖同一段实现 |
| 发布、部署、Gateway 重启和真实运行环境 | 一个集成负责人 | 同机单 Gateway；不同 TUI 会话可并行，安装/重启不能并发 |

实施前进行一次有范围的事实对齐：HEAD、未提交文件、认领的文件/函数、稳定接口版本、测试环境与验收标准。
使用已授权的任务通信或已经显式连接的 Agent Bridge；没有邀请不能扫描房间或自行注册。
未取得共同确认的共享入口不动，先开发可独立的部分，不要求对方暂停等本线。
采用独立 worktree，分支按 codex/ 前缀；不 reset、stash、清理或提交对方的脏文件。
协议以真实提供的接口为准，不建立双份实现来规避冲突；短暂跨线适配须由集成负责人一次完成并删除旧入口。
每批合并前对齐最新基线；只集成本线提交/差异，再跑相邻模块组合回归。原工作区已在变化，不能把本次快照当长期锁定。

## 11. 验证矩阵与完成标准

遵守合同单测 → fake tool → fake model/本地假服务 → replay → 少量真实 TUI 的顺序。
不是通过修改被测任务的产物、提示词或运行目录来让验收通过；真实任务只给一次普通中文需求，之后观察原账本和产物。

| 验证组 | 必须覆盖的事实 |
| --- | --- |
| 关闭等价 | 不发决策请求、不启动专属后台任务；模型继承、工具可见性、记忆输入/游标与原行为一致 |
| 配置与双入口 | 用户界面和 agent 写同一事实源；2 改 4 秒、按点覆盖/恢复继承、主开关保留各点设置、临时与长期范围、失效服务仍可关、部分修改和并发版本冲突；读回值与实际请求期限一致 |
| 时间 | 期限前/后响应、无响应、慢响应头、持续滴流、超大/残缺 JSON、DNS/连接卡住、队列占满；阶段预算不重置 |
| 请求数 | 默认一次物理请求；HTTP/SDK/宿主零叠加重试；缺数据仅一次新输入且总期限不延长 |
| 错误 | 401/403、硬额度、普通 429、5xx、断网、错误类型/候选/NaN/缺题；立即保留基础方案，诊断入原账 |
| 取消与恢复 | 用户取消、关闭开关、改配置、撤销权限、服务恢复；迟到结果不生效，不取消主任务或兄弟请求 |
| 在途配置变化 | 改时间不延长旧请求或补回阶段已用预算；关闭取消当前决策且阻止旧建议应用；后续请求读取新值，用户的新设置不被旧自动任务覆盖 |
| 资源 | 连接清理、未退出请求有界、连续故障不堆线程/句柄、批量 child 不形成重试风暴；主模型仍能获得资源 |
| 子代理 | 显式模型优先、父子孙继承、跨 owner 拒绝、批次部分判断失败、重复创建/恢复不重选、不重复创建 |
| 记忆 | 标注有原始来源；故障不丢批次、不改变处理游标/未解决集合、不直接晋升、删除或覆盖人格 |
| 窗口与缓存 | 250K/1M 等合成配置、不同计数、预留输出、必要材料超限、工具 schema 变化、稳定前缀、权限变化后缓存失效 |
| 用量与观察 | 失败/取消/未知 usage、观察模式、缓存创建/读取、决策输入 token 与原任务用量；无数据不报节省 |
| 多会话与插件 | 一个 Gateway 多 TUI，决策正常/故障/关闭与插件装卸并行；旧插件候选不能绕过实际撤销 |

时钟边界单测用可控时间，不用长 sleep；本地慢服务验证真实传输取消和墙钟，预先说明有限的平台调度误差。
不能只测外层函数已经返回：还要核对活动请求、线程/连接数量、重复请求数及原业务记录。
真实 Jev 测试使用用户已有的合法配置与明确的时间/请求数范围，不能为测试安装整个外部 agent 或改日常模型。
普通执行模型沿官方 MiniMax-M2.7，并核对 provider/端点；新增跨模型测试使用显式配置的候选，不静默替换日常选择。

质量对照使用固定的中文、多义、缺信息、无候选、无需处理和冲突材料样本。
记录 P50/P95/P99、超时率、实际采用率、错误建议/关键遗漏、原任务完成质量和全流程输入 token；不把“接口很快”当成净收益。
各点先达到预先约定的质量边界，再考虑默认建议；收益不足保留关闭，不为证明 Jev 有用放宽错误标准。

已有定向回归优先扩展：test_model_profiles.py、test_gateway_model_profiles.py、test_thread_model_selection.py、
test_gateway_helpers.py、test_model_call_ledger.py、test_orchestration_create_subagents_items.py、
test_orchestration_create_subagents_idempotency.py、test_memory_curator_v2.py、test_model_context_window.py、test_runtime_context_pressure.py。
新增决策合同/传输测试只验证真实可失败边界，不照抄实现断言；实际文件清单随阶段认领登记。
远端提交前执行 AGENTS.md 的 focused pytest、Ruff、doc sync、strict code-size、diff、clean-package；全仓频率遵守原规则。
真实功能和模型判断质量分别记结果；文档和假服务通过不等于真实 TUI/远端接口通过。

## 12. 明确不扩大的范围

- 不安装或搬入 jev-harness 整个工程，不新引入 Node 服务或强制 SDK，不增加未经必要性说明的第三方依赖。
- 不复制记忆、任务、工具、调度、模型目录、凭据、账本、Compact 或插件生命周期。
- 不允许 Jev 自己扩权、控制审批、判定 Goal 完成或重试未知副作用。
- 不把逐行删除历史当作缓存优化，也不因为决策故障改主模型供应商或用户持久开关。
- 首版不做全接入点自动调用、自动学习阈值、跨供应商自动竞价或复杂实验平台。
- 可选调用的异常隔离不等于任意第三方 Python 的进程隔离；未来执行外部代码仍沿原插件独立进程边界。

## 13. 本轮交接与建议下一步

以下保留规划轮交接；后续实施进度以[执行 Goal](../tasks/DECISION_MODEL_GOAL.md)为准。

- 工作线：决策模型规划；基线 9141dad9c；独立 managed worktree，未合并到原工作区。
- 实际完成：设计与阶段计划、可调时间及双入口开关、预算/失败语义、配置和缓存边界、接入矩阵、拟定并行分工和验收清单。
- 实际未做：产品代码、模型请求、运行配置、Goal 修改、提交/推送/部署。
- 协作进展：已回复另一任务主动发来的归属通知，确认本线代码认领为空、原共享文档由对方负责、实际实施及发布前再次对齐；没有向对方追加工作。
- 文档变更范围：本文件、DESIGN_LEDGER、ROADMAP、LLM_GUIDE、CODEBASE_TREE、设计索引、原 Jev 评估和 TESTS 导航。
- 本轮验证只检查文档同步、差异空白和新增链接；运行时能力全部保持待实施。验证结果以本轮最终汇报为准。
- 需要主线协调：共享 HTTP/配置入口的认领、子代理第 7 步与本线接线顺序、插件接口稳定点和最终单 Gateway 验收窗口。

建议下一步：先确认 P1 的共享入口归属，完成“默认关闭 + 用户/agent 共用可调时间与开关 + 可取消的短期限决策 + 故障走原流程”的最小切片。
插件线继续其当前工作；本线先做协议、假服务与记忆输入准备，避免等待共享文件时停工，也避免提前改对方代码。

## TODO10 上下文减量实施约定（2026-09-22，本地验收通过）

不能以“增加推荐文字但仍发送全部名卡/schema”代替减量目标。原Skill快照/工具Registry仍是唯一授权来源；
宿主只为当前工作片建立不可变展示投影。None保持旧字节路径；关闭、观察、失败、弃权、无匹配和缺数据仍用原输入。
明确not_needed表示对应候选与任务无关，可产生空短名单；明确必要能力和发现入口仍保留。
Skill选中/明确required名卡进入原动态推荐段，稳定区只保留固定发现说明；省略项可从原skill_search找回。
任务局部范围仅在显式非None投影时展示当前run受限快照的名卡，不恢复owner人格/项目等隔离材料；isolated/control_plane保持不注入。
工具短清单只影响展示，不约束搜索。progressive仅收起原配置明确可选类别的未选中direct schema；
原发现入口、显式allowed和真实loaded保持，原tool_search必须能返回被收起项的完整原schema，否则拒绝该隐藏投影。
不暗改默认deferred类别，不伪造loaded或权限，不跟随同名插件新activation/transport。
原ToolRuntimeSnapshot新增的host-only展示字段不改变注册snapshot_hash，真实执行仍在原ToolExecutor/MCP发送前准入。
`points.skill_tool.context_policy`已区分metadata/progressive，默认progressive；`points.skill_tool.optional_categories`
默认`["plugins"]`，空列表不额外收起工具schema，类别是开放字符串，不增写死分类枚举。
默认值只归原CapabilityConfig/YAML；同一owner/thread设置服务、原user_config及原菜单支持修改/恢复继承。
决策总开关与接入点仍默认关闭。原decision_settings.v1覆盖加法接受这两个可选字段，旧记录不补默认；旧程序不认识新字段时明确拒绝，
不能静默抹除覆盖。有效值变更纳入原在途取消签名和采用前版本复核；不建立第二份设置或目录。
原`_execute_runtime_loop`在工具循环种子前调用一次；选择沿RuntimeToolLoopSeed→ToolLoopExecuteParams→ToolSections传递。
每轮渲染及Compact的原目录构造不请求决策模型；不同工作片可重做建议，不建立长期结果缓存。
真的发起过决策时，结果附一条结构化观测（`capability_presentation_observation.v1`：mode/status/reason 码、阶段操作编号、候选版本摘要、题数、是否采用、保留原因码，采用时加短名单与延迟名单的工具名各至多 64 个和 Skill 计数；不含题目、回答或用户正文）。它经循环参数上独立的 `capability_presentation_observer` 交给宿主，原展示回调的语义不变；Gateway 把它追加进请求记录的 `capability_presentation_observation.entries`（最多保留 8 条，与模型观察同一 active-turn 事务，内存与文件同步），只供观察，不参与判定。子代理和后台暂不写，后续按同一形状写 thread metadata。
工具候选只取原授权运行快照并交集显式allowed，Skill只取当前scoped快照；不发送task_attributes原文，只用摘要绑定。
真实Jev验收否定了多个选择槽跨题去重的设计：Jev各题独立并行，8槽实际全部答成无需，漏掉相关能力。
现改为每候选独立choice（include/not_needed/缺数据等），候选说明只在对应instructions里发送一次，宿主保留原引用映射。
不再借槽位序号暗示另一题已选结果。原适配器自设64题上限已移除，官方未声明该题数上限；仍受原JSON节点、256KiB和每Choice255选项限制。
96题本地完整协议通过；整体材料超限仍保持原输入、不截断尾部。12已接Jev发送前的两层容量估算筛查：官方总64k、`state`加最长题32k，同时尊重用户填写的更小窗口。
供应商尚无本项目可复用的精确tokenizer；全仓回归证实 UTF-8 字节数直接作 token 上界会误拒合法批量请求，现复用原 `estimate_tokens` 并留一成余量。估算不是严格容量证明或供应商usage；服务端超窗仍按原业务回退，不截断材料重试。原 JSON 256 KiB 资源硬帽不变；完整child换模容量、缓存仍继续调查。
缺数据选项明确区分任务目标、工具合同、Skill正文和环境事实；本接入点不自动补读/加权限，而是保持原上下文由主模型继续处理。
消费前刷新原Skill范围及版本，检查固定handler的无副作用availability；插件沿原activation/transport检查，不按同名新工具重绑。
原配置、实际主模型/窗口、任务属性或候选发生变化时拒绝旧建议，最后再过公开decision_outcome_is_current及绝对期限。
普通自然语言没有可信required refs时，不靠字符串匹配宣称“已识别明确工具”；保留完整用户需求与原搜索找回路径。
验证要比较真实provider输入字节/token、原搜索可达、撤销和实际cache事实，不能只检查建议对象或承诺缓存收益。
本地原生成输入已证明名卡/schema减量，选中名卡在动态区；metadata模式保持完整native schema。
这不证明收费token净节省、真实缓存命中率或Jev判断质量，完整窗口/缓存属于12，真实模型属于13。

**按插件分组出题（2026-09-24，本地分支 `claude/decision-plugin-grouping`）**：
- 起因：主线真实 TUI 启用 11 个插件时，用户要求用 design-lite 做海报，design-lite 的工具却被判 not_needed。原因是逐工具出题，候选行里只有哈希后的工具名和通用 MCP 文案，看不出属于哪个插件，也没有插件简介。
- 主线已做两处通用修复：代理工具说明改成"插件 <ID>（<简介>）"；被隐藏的插件工具在提示里按插件 ID 列出。另加了结构化字段 `ToolModelHints.provider_id`（插件代理为 `plugin:<ID>`）。
- 决策线：工具候选行在 `provider_id` 非空时带上它；`group_provider_candidates` 只按这个结构化字段，把同一插件的工具合成一题，题的位置取第一个成员的位置，候选列出成员名、说明和版本。
- 选中后展开回全部成员工具；未选中则整体进入延迟名单，由原提示按插件列出，模型仍可用 tool_search 找回。
- 内置工具和 Skill 仍逐项判断。不按描述、关键词或用户原话归组，也不做"原话出现插件 ID 就强制可见"。
- 量化（仓库内置插件声明，8 个带工具的插件共 21 个工具）：每次请求的题数 21→8，题目序列化大小 30,691→19,520 字节（−36.4%）。

真实接口同时暴露逐项概率的百分位舍入：总和可为0.99/1.01。wire按每项半百分位累计误差校验总量和评分，
高精度分布仍走严格误差；零总量、缺键、越界、非有限数、明显不一致仍拒绝，不归一化或改写供应商概率。
原始脱敏失败样本进入协议replay，业务权限仍不来自分数。来源、实际请求和未验范围见[真实验收记录](../tasks/DECISION_MODEL_REAL_VALIDATION.md)。


## TODO12.4 宿主历史来源与请求生命周期（2026-09-23，实施中）

解决跨窗口Compact时完整历史被宿主seed、原生请求、冻结请求及恢复闭包同时保留的问题。来源到摘要的地址重放已在464df65c1本地验收；三宿主seed仍重新物化，同一4.2M字符来源的准备峰值约8.6–9.0MB，完整调用链峰值另测，不能用source单片代替。

第二片按两个可验证边界实施，最终均归12.4，不缩减整项目标：

1. **显式来源种子**：`ConversationHistorySeed` 的具体messages/canonical_messages与只读source严格二选一。source只携带同次canonical地址及已冻结的范围/投影规则，不建第二历史库；不将磁盘对象伪装成原tuple或运行时provider list。三宿主复用原行选择、工具折叠和native投影规则，范围事实只裁决一次，媒体/匿名信封及原生工具往返完整保留。
2. **旧请求释放**：初始完整计量后，处理params、冻结输入、宿主外层帧、partial与闭包的实际持有关系。初始计量必须继续代表完整原请求，不能把“空历史”当成释放后的代用品。获选候选仍携带自己的完整请求材料；未提交失败不得继续发送旧请求。该部分接口与实现尚待独立审查，不能以第一步验收宣称摘要期驻留已解除。

显式来源只在原native/text准备边界解析，之后仍为具体list/tuple；`capture_tool_loop_request`、`ToolLoopRequestInput`、纯projector及运行时provider list原位更新保持原合同。原生来源投影沿native_history做必要隔离，已有具体seed继续原复制语义；未知、明确空来源和读取失败不可混为一类。source不能与具体内容同时成为权威，也不能在重放时重新读取当前权限/配置扩大范围。

模块重构owner已转交本分支两个精确函数的机械接线：`runtime/loop_support.py::_native_provider_history_messages`、`_tool_loop_service.py::_text_conversation_history_section`。其它native plan/commit/live-tool函数仍由原owner负责；不修改对方工作区或部署。

**2a 实施状态（2026-09-23，本地分支 `claude/decision-12.4-2a`）**：
- 新增 `conversation/history_seed.py`：`ConversationHistorySource`（冻结行加原单行投影函数）与 `ProjectedHistoryRows`（按需投影的只读序列）；`seed_provider_history_messages`/`seed_text_messages` 是两个边界唯一的解析入口，具体种子沿原深拷贝规则。
- `history_projection.history_row_selected`/`project_history_row` 与 `agent_thread.project_agent_history_row` 是从原循环中抽出的单行规则，具体投影与来源重放共用。
- Gateway 上下文只保留 `history_source`，移除具体 history/canonical 副本和只剩完整路径使用的 `_gateway_conversation_refs`；恢复候选沿同一规则。child 冻结 view 行。后台 scope_applied 入口冻结已裁决行，非预选入口保留原范围算法。
- 地址视图冻结时去掉准备期取消回调；来源同时冻结投影时刻，终态工具折叠的热尾/冷折叠不随解析时间变化；后台来源保留空正文行，与原后台规则一致。
- 源文件被改写、截短或替换时解析抛 `DataCorruptionError`，被删除时抛 `FileNotFoundError`，不会变成空历史；因此 canonical 文件在整个运行期间只允许追加。
- Gateway `_conversation_prompt_section` 删掉生产不可达的 `include_transcript` 正文/摘要分支，已结束历史只经种子边界提供。
- 与主线 owner 约定：两个共享函数只在入口解析来源。等价测试覆盖两个边界、下游孤儿清扫、媒体和匿名信封；4.2M 字符的峰值对比写进容量审计。

**2b 实施状态（2026-09-24，已合入 main `911d0d14d`，尚未部署）**：
- `PreparedCompactRecovery.select` 在自动 noop 返回之后、调用摘要之前，解绑原参数的 `provider_history_messages`，并把 frozen 换成空历史（经主线 owner 同意用解绑而非原地清空）。
- 4.2M 字符下，三宿主摘要入口驻留降约 8.3–8.5MB，摘要期峰值从 10.2–11.7MB 降到 1.7–3.3MB；建循环时的首次物化峰值不变。
- 失败、取消、超限收尾不读已解绑的历史（用读取即报错的替身钉住），自动 noop 原样发送原请求。
- 副作用（2026-09-26 集成方发现并修复）：解绑发生在“压缩前”计量之前。三个宿主对原请求视图都用冻结输入重量，于是 checkpoint 的 `projected_tokens_before` 和进度事件的 `before_tokens` 只剩系统提示和工具，真机记成 35,915（实际约 31.3 万），还比压后小。现在 `select` 在解绑前按自动判定同一口径（`_full_request_tokens`）量一次完整旧请求，transcript 与活动回合两条路径都用这个数。这个字段不参与是否压缩的判定，但 Gateway 在压缩开始时会把它存成本会话的上下文用量快照并推给 TUI 状态条（`request_context._gateway_compact_progress_callback`），修复前强制压缩期间状态条会显示偏小的数字（Codex 复核指出）。代价：自动路径比原来多一次完整投影，只在已决定摘要时发生。
- 详见[容量审计](../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#摘要期释放旧请求历史2b2026-09-24本地)。

验收分别记录source/seed、首次完整预检、摘要驻留和首实际provider payload峰值。必要发送本身仍有完整材料成本，不能把未发送旧超大预检峰值归入必要出站成本；三宿主的scope、失败不提交、媒体和候选/实际payload逐值一致需相邻验证。详细证据与剩余边界见[容量审计](../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
