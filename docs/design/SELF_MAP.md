## 2026-10-17 F5用户可见说明补充（待真实复测）

上一真实单prompt已读取准确self_map总览并先回复能力数量、完成文件读写回读，但把未展示的技能称作“见上方列表”，且没有读取skills明细。本轮仅在已有可关闭的SELF_MAP_GUIDANCE增加通用软提示：能力清单需读取原items并在面向用户的回复说明名称/用途；工具回执不等于用户已见。没有解析问题、任务专用合同、自动加载全部正文或新的权限/验收门。普通与Compact继续同一指引，原enable_self_map关闭时同时消失；最终同源真实复测另记，不把文案变化当模型一定遵循的证明。 当前原自我目录/普通与Compact模型面、权限及prompt七文件179项通过（0F/E/skip，10.346秒），输入前后不变。

# 自我目录：原读口的只读聚合

状态：本文维护自我目录的实现合同。2026-10-16 目录读取口径修复为独立离线切片，尚未合并、部署或进行修后真实复测；主体发行及验收状态以 [TESTS](../../TESTS.md) 和 [STATUS](../../STATUS.md) 为准。

## 用途与读取顺序

用户可以问“你现在有哪些工具”“自己的目录在哪里”“哪些能力还不能用”。模型在当前权限范围内调用 `self_map` 读总览，再按领域和条目继续查看。它不执行工具、测试服务连通性、刷新 Skill、创建线程或启动后台任务。

工具发现仍以原 `list_tools` 的 manifest v3、本人 `self_context` 和不可用原因作为事实来源；需要具体参数时仍经 `tool_search` 渐进加载 schema。地图只聚合各模块公开读口，条目或导航不授予调用、路径、账号和资源权限，也不替代原工具的执行时裁决。

```json
{}
```

空参数读取总览；以下是工具参数示例，不是终端斜杠命令：

```json
{"section":"tools","limit":20}
{"section":"tools","item":"<上一页返回的准确条目ID>"}
{"section":"tools","cursor":"<上一页返回的游标>"}
```

合法编号由领域注册表生成 schema。无参总览仍精确为 `directories`、`permissions`、`tools`、`skills`、`packages`、`plugins`、`mcp`、`memory`、`models`、`config`、`work`、`context` 十二领域；完整登记与schema另含下述四个按需专项。新增领域由所属模块登记reader，不在工具中维护第二份能力或权限清单。

## 2026-10-28 Wave9：Level0与Level1渐进寻址

实现位于本轮工作树，尚未部署；测试门禁和剩余限制以TESTS本日item9条目为准，不以目录存在证明真实模型寻址或外部推送已验证。

`SectionDescriptor.include_in_overview`默认`True`，`overview_descriptors()`只筛原登记声明，不调用reader。四个专项设为`False`：平时不进12项总览，显式查询可读；`descriptors()`、`model_spec.section.enum`、错误恢复的`valid_sections`均包含完整16项。`work`总览仍用轻量`work_summary`，不会因新增专项自动枚举节点或调度库。

| 按需section | 原权威及公开字段 | 权限和后续入口 |
|---|---|---|
| `remote_peers` | 当前owner的`config/remote_endpoints.json`，及宿主`remote_push/endpoints.json`中启用且caller ACL包含本人之项；owner同名优先。投影别名、URL、scope、enabled和私网许可 | 子代理与`isolated/task_local/control_plane`在读库前返回空集。同文件存储下经`read_public_push_endpoints`在数据源出口即剥离`token_ciphertext`/`token_hash`等凭据键，目录层只接收公开白名单字段，不取用、不解密、不暴露任何秘密或散列、不做DNS/HTTP或写盘。详情为`peer:<ref>`；`work`复用启用节点摘要 |
| `settings` | 原`resolve_thread_compact_policy(use_frozen=False)`与`compact_idle_parameters`；生效比例、来源、强制锁及闲置阈值/时长 | 精确线程缺失或错owner/home不可回落默认；受限上下文不读父策略。普通用户不见管理员默认值和全局编辑面；导航指向按需展开的`compact_policy`工具（只读`status`及受原会话归属/群actor/管理员锁约束的阈值调整）与`user_config` |
| `schedules` | 原`_schedules`及SchedulerRepository；`source_thread_id`（旧记录回退`thread_id`）显示为`bound_thread_id`，原`delivery`显示为`delivery_policy` | 复用原schedule授权和群`resolve_host_actor`，子/隔离先拒绝。`work`同步相同字段；详情仍走`schedule(action=get, job_id=...)`，收起的工具先发现再读 |
| `assets` | 原冻结SkillSnapshot的普通方法数量、原包summary及本次工具状态 | 普通方法/包数量不混算，快照缺失或不完整保留null/partial；只提供scripts/workflows/datasources列表及skill_search/tool_search导航，不执行列表或读取正文、订单、构建历史 |

专项同样保留默认短页预算、`item_columns`/`detail_next_read`和原分页合同；需要完整字段应沿准确item回读，而不是把紧凑行当作全部详情。

### 设置与通知摘要口径

- settings观察点为`current_resolved_policy`，是当前治理/线程状态的只读重读，不冒充活动回合已冻结的preflight策略。管理员字段按当前身份裁剪；它不修改压缩策略或触发compact。
- 当前集成起点没有注册名为`compact_policy`的模型工具。条目如实返回`configuration_tool_state=unavailable`，保留`tool_search`发现导航和原`/compact status`读口，不能描述成已可直接调用。
- `user_config`沿原mutating中央门，目录不绕过其effect判定：先给`tool_search`入口，再按公开`read_parameters`读取；普通用户仅`decision_read`，管理员可读准确参数。工具展开不等于允许修改或取得管理员权限。
- `follow_active_thread`只映射原`delivery.follow_latest_channel_session`，未另存策略。`channels`只列显式local/channel_binding目标的通道标签；仅binding_id时复用原认证目录补标签，失效引用保持null及partial。`channels_source`明确该范围：隐式origin、`include_origin`和原绑定的动态通知路由不在此枚举，空列表不表示不通知；`bound_thread_id`是计划原绑定，不是外发成功或当前收件人的证明。完整策略与发送前认证仍由原调度读口/执行链裁决。

### 常驻提示与证据边界

Level0仅在原`enable_self_map and self_map_available`条件下通过`SELF_MAP_GUIDANCE`唯一注入，普通与Compact继续同源。索引含settings/schedules/remote_peers/assets及治理、监控、单向推送、技能/自动化工具导航；数量仍须核对原counts/完整性，清单仍须读items向用户展示，unknown/partial不可当0或没有，路径沿准确回执，不授予执行或外部验证。

本轮合并home整理约定、workspace日期叮嘱和default清单说明，规则仍留在原作用域，不挪到动态段伪装减量；原安全边界、专用确认与测试锁定短语保留。离线估算器不是供应商精确tokenizer，数值见TESTS；没有据此声称线上总上下文、缓存命中率或真实模型遵守已通过。

## 范围与公开事实

- owner、当前 HostActor、线程、父级允许快照和冻结调用范围由宿主绑定，模型参数不能填写另一身份。普通用户、群用户和子代理只见当前可见事实；子代理不回退父线程或读取管理员库存。
- 路径沿原访问裁决投影。普通用户不获得全机目录或其它 owner 的可枚举路径；缺少冻结目录范围只标未观测，不猜当前运行环境。
- 读写分别标注。`read_access` 明确对应内置文件工具的路径门（`read_access_scope=builtin_file_tools`）：同次调用冻结原 `exact_read_roots` 的精确上界和原路径政策，普通非 exact 模式不把 cwd 误作本人全部读取范围；原墙外授权还须在同次预检工作根内。`workspace_read_access` 单独说明插件只读协议的 cwd 范围，插件更窄不代表内置 `read_file` 同样拒绝。实际工具授权、资源存在和正文可打开仍由原执行口复核，目录不执行探测。
- 没有内置读门冻结事实时，原路径政策拒绝仍为 `false`；政策放行只能为 `null`。冻结精确空根明确拒绝，不能从写根猜读权。读未观测、写已拒绝可以同时存在；owner 根节点含人格保护，也不等于全部业务子路径禁写。
- `workspace` 与位置卡优先显示同次 invocation 冻结 cwd。`workspace_skills` 独立沿本人已绑定 `SkillSnapshot.workspace_root` 和 `SkillService.base_roots` 声明，不随文件工具 cwd 推造 Skill 来源；未绑定、不唯一或原路径政策不可见时位置标未观测。普通/群/子代理不因此获得其它 owner 或父级库存。
- 权限详情的 `filesystem.read_roots` 明确标为插件工作区范围；内置文件读门另列 `filesystem.builtin_read_scope` 及冻结来源、根数量，旧 `read_roots` 增加 `read_roots_scope=plugin_workspace_context` 标记，不新增可枚举的精确根或墙外路径。
- 工具用途和状态复用原注册表；已有凭据卡、群共享引用、群监控授权、机器人订阅、原生及引用图片等能力继续按各自公开事实和权限显示。看到工具不等于已经拿到凭据、群授权、媒体或服务连接。
- Skill 沿原逐轮快照、元数据和显式正文加载。已撤回的旧快照不给正文或资源补权；新 Skill 仍服从既有用户确认、版本与 CAS 生命周期。
- 模型读取原 owner/共享模型投影，保留来源不完整状态，默认模型别名不当成额外正式档案；不输出端点、环境变量名、请求头或密钥。库存声明不代表端点已验证。
- F4 规则来源仅读已有仓库 `configuration()` 的安全元信息，不调用 `load()` 或读取正文，不输出私有 locator。配置修订不代表正文实时新鲜；显式用户操作回 `/agents`。
- 工作与队列只聚合当前范围内已有公共读口。缺少可按 owner 读取的队列接口时返回未知，不旁读全局队列。零结果也不证明外部已经查全，仍沿原零查询复核提示。
- MCP/插件的声明、安装、连接与验证状态分开；地图不连接服务或执行插件。普通用户不因为目录可见而继承管理员的安装和管理能力。

## 分页、完整性与配置

默认列表使用 `item_columns` 解释列式行，`detail_next_read` 加上行内准确 `item` 可重建详情请求；列表是摘要，`detail_complete=false`，不能把短用途当完整参数。游标绑定当前来源和范围指纹；来源或权限变化返回 `SELF_MAP_CURSOR_STALE`，应重新读取本领域。最小结构也放不下时明确返回 `SELF_MAP_RESPONSE_TOO_LARGE`，不截坏 JSON 或制造原地翻页循环。

`count=null`、`not_observed`、`unavailable`、`partial` 各自保留，不能说成“没有”或“已查全”。总览条目数不等于正式记忆、档案或外部资源数量。外部未验证必须如实说明。

`enable_self_map` 默认 `true`，属于部署配置边界。关闭时不注册入口，也不向普通请求或 Compact 注入相关指引；模型不能通过 `user_config` 自己打开。现有 `list_tools/tool_search` 仍按原合同工作。该配置不改变其它模块权限或开启网络。

## 与人工确认的边界

地图没有确认权。规则修改由本人 `/agents` 预览并确认；记忆候选用 `/memory candidates preview <候选ID>` 后复制当前预览给出的确认命令。IM 仅开放 `candidates` 子树，裸 `/memory [查询内容]` 仍仅终端搜索。AGENTS、Memory 和 Skill 各用原控制服务，不从普通自然语言或模型 `confirmed` 字段签发批准。命令详情见 [CLI_REFERENCE](../../CLI_REFERENCE.md#人工规则与记忆候选控制组合候选未发布)。

## 维护入口与验证

装配入口是 `agent_core/self_map_bootstrap.py`；聚合合同在 `tooling/self_map_registry.py`，查询与展示在 `self_map_tool.py`、`self_map_display.py`。各领域实现由原模块持有，没有新增状态库、后台索引或第二正式清单。

分线新增 38 个合同、权限、fake LLM 和 replay case；独立 F5 隔离代表集为 21 文件 293 passed。F4 人工候选 followup 的普通/群真实临时认证、目标 CAS、过期、unknown 和动态 help 已有独立复核，阶段结果不相加为不同覆盖数。真实供应商、飞书租户、OS/网络边界与最终组合严格 gate 未由这些离线证据代验。

目录读取口径回归入口是 `agent_py_agent/tests/test_self_map_read_authority.py`：在真实 Registry 和内置 `read_file` 上对照普通/群/子代理、cwd 外 exact 文件、精确空根、墙外授权与预检交集、跨 owner/宿主凭据拒绝、并发冻结和缺观测；没有调用真实模型或修改实际读取门。原精确基线加同一最终新测试为 21 项中 17 失败、4 通过，修后相关 8 文件 113 项实际通过、零跳过。此为离线因果证据，不能回填旧真实复测或视为修后模型验收。

建议下一步：先合并独立投影修复并完成组合 focused 与严格 gate，再由原负责人安排准确候选包验收。文档与只读审阅可以并行，实际权限、服务切换与真实 prompt 仍单点控制。

## 2026-10-17 真实能力说明准确性补充（候选）

同一普通需求真实复测已先说明能力，再完成读写读；未重现完全漏答，但就绪数量误报、Skill数量缺当轮读口证据。原 `list_tools` 的 ready/deferred/unavailable 与可执行总数不同；名卡有独立预算，展示条数不能当普通 Skill 总数。数量权威已在 `self_map.tools` 与 `self_map.skills` 的当轮 counts/completeness，缺失或 partial 不补零。目录回执已给准确绝对 path，首个相对路径错误由模型自行修正，不新造路径 fallback。

本次只加强原 `SELF_MAP_GUIDANCE` 静态软指引：分别按原分块核对工具状态和方法数量，未核实不报数，路径使用原准确值。仍受原 enable_self_map 与当次工具可用性控制；不读用户正文分派、不加新计数器或质量硬门、不改运行权限和目录裁决。新真实结果须单独复验，不因 prompt 存在或离线通过算模型遵守。

## 2026-10-17 第三次原生单需求复测

既有静态指引补充后的新会话先读原self_map概要与list_files，再向用户说明能力，随后完成读/写/回读。概要实际53工具（36直接、13按需、4不可用）和29普通Skill，与回复计数一致；没有重复拼接cwd。未单独枚举完整Skill名单和目录明细，不能把概要准确外推全部权限说明或所有后续任务必然正确。仅用户正文和原工具块留证，测试实例正常关闭，日常配置不变，见TESTS。

## 2026-10-17 真实复测结果：部分通过

同一条普通中文需求只提交一次；真实运算、写入与完整回读成功，能力数量有回复，但工具/技能的名称与用途仍漏列。新版SELF_MAP_GUIDANCE、原用户需求与schema已进入全部业务模型请求，实际工具顺序包含self_map和原文件读写；工具调用参数未在原账保留，不能补猜为默认参数。索引分页、next_read与权限事实未证存在新平台错误，结果仍按部分通过记录。

软提示不会保证模型逐项执行。当前没有为此解析用户措辞、增加硬验收门、强制查询步骤或自动装载全部技能正文。后续可评估总览显式标注仅为summary，但这只是未实施的呈现建议；根因与收益仍需独立证据，不以原切片测试或工具回执代替用户已看到完整说明。
