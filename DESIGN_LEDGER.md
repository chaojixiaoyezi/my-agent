# 设计台账

## 学外部 agent 做成能力包或插件、自己装（learnpack，3a，2026-10-06；状态：第一期开发中，3a 单人，分支 claude/3a-learnpack-p1）

- **第一期定稿（用户 10-06 后三轮，以此为准）**：学外部 agent 默认做成能力包，要联网/账号/常驻服务的做成插件；内部技能只从她自己的总结来（"让她总结一下"走现有自学习流水线），不再问"内部技能还是能力包"。名片照旧：学来的技能和能力包都和内置技能一样每次带名片、正文用到才读（靠字面打分判断相关不可靠）；下面"本体干净"条里的"常驻只放一句"和第 10 节第 1 级作废。两个开关「能力包自动装」「插件自动装」默认关、只有管理员能改：开着她自己装（只装她自己做的），关着她给一行确认、用户发回来由宿主只执行一次；能力包里要宿主运行的检查程序跟插件开关走。新工具 3 个（打包、安装、总结一下），都默认收起。估算改按会话·天算，原"15–35 元"作废；第一期 3a 单人约 24–28 小时，不花 DeepSeek，真实验收只用 MiniMax-M2.7 且要用户点头。详见设计文档第 11 节。
- **第 2–3 步代用户做的实现决定（3a，2026-10-06）**：
  - 存储放 `<owner home>/data/learnpack/`：owner 根 `data/` 是 H3 宿主运行状态，模型只读，所以"是不是她做的、摘要对不对"靠得住。
  - 打包工具的读权限借用同一注册表里 `read_file` 的裁决。第一期主代理只能读 owner 目录，待打包目录要放在任务工作区；这样偏保守。
  - 文本文件里认出密钥就整包拒绝（复用日志脱敏的识别），只报文件名。
  - 她自造插件第一期只收 v6 文件包，事件/收紧插件（v8）不在范围内。
  - 两个打包脚本改成薄壳，打包规则只在产品里一处；插件脚本保留原读文件口径，不影响开发者。
  - 安装走管理员同一串 `/plugins` 命令，不另起执行链。代确认只覆盖"运行她自己做的程序"；联网和读写别的目录永远交给用户。
  - 自动装的账记在专用 learnpack 线程，不改用户"最近会话"。
  - 确认行第一期是 `/plugins confirm <单号>`，第 4 步给能力包换成 `/plugins#<包名> 安装 <单号>`，两种写法都认。

- **用户要的**：让 my-agent 学一个外部专精 agent，学完问做成内部技能还是能力包；学的时候就说了"做成能力包"的，直接做好给自己装上。要新开关，她要自己知道开关状态，按新手提醒怎么开、怎么关。提问隔很久才回也要接得上。插件和能力包是两码事，能力包用 `/plugins#`，插件用 `/plugins@`。
- **方案要点**：
  - 内置学习技能，学习笔记写成文件，在同一对话里回复就能接上；
  - 当场写内部技能，复用自学习的发布闸门；
  - 打包函数从 `scripts/` 迁进产品，做成工具；
  - 新边界开关：只有管理员 `/settings` 能改，改完马上生效；
  - 开关开着才能自己装，且只装自己做的、只含内容的包；带检查程序的不自动启用；
  - 打包和安装回执里带开关事实和现成命令，她照着回执提醒；
  - 装上后默认只推荐，不自动挑包。
- **工作量和花费**：约 3 个 agent 工作日；做功能约 15–35 元（DeepSeek）加 3 次 MiniMax 验收；以后每学一次约 1–6 元。第二期 TUI 选项和飞书按钮约 2 天。
- **追加：多包与融合**。用户目标是只用 my-agent 一个 agent。离线核对：相关请求推荐对路，3 条无关请求零推荐，另有 2 条弱误推。第一期加推荐门槛（约 0.25 天）；同领域合并成一个多方向的包，可回退（约 0.5–1 天）。靠专门工具或外部服务的能力走插件（要用户确认，她不自装）。详见设计文档第 8 节。
- **追加：本体干净**（用户原则：能力包是外部心法，可带自己的程序和工具；插件是装备；学得再多也不污染本体）。更正：已装的包和学来的技能每轮都在常驻目录里各占一行，学来的 28 个技能让常驻目录约翻倍，每次请求多约 3–6K token。第一期改为常驻只放一句'已装 N 个能力包'，具体包只在相关时出现（约 0.5 天）；学领域默认做成能力包；离线小工具随包走，联网或服务类做成插件（启用要用户确认）。详见设计文档第 9 节。
- **追加：对照 deepseek-harness 的四级目标**。dsh 一切皆插件、连 loop 和模型适配器都可从配置替换；my-agent 现状是工具进程级注册、后端写死工厂、插件只能观察+收紧。四级（从易到难）：①渐进式披露（本体干净，1–1.5 天）②自造自装自删（4–5 天）③工具按包/插件登记成任务级正式工具（1.5–2 天）④插件能改写/接管事件（对标 dsh Mod，权限极大，另立设计线）。详见设计文档第 10 节。
- 详见 `docs/design/LEARN_TO_PACK.md`。

## 回合中归档摘要复用主请求前缀（compactcache，3a，2026-10-05；状态：已实现，待部署后用账本核对辅助调用命中率）

- **来源（结构化事实）**：生产 model_usage 账本 48 小时压缩类辅助调用（purpose=auxiliary）命中 0%：DeepSeek 88 次、4290 万输入，GPT 两个订阅模型约 3300 万；cache_read 每次都上报了，值就是 0。17k 起前台 transcript 压缩已复用前缀（模拟器 ≥90%），但长任务回合里多数压缩走恢复链的回合中归档（`compact_carried_active_turn_archive`）。
- **离线复现**：缓存模拟器 + 真实 Gateway ask，新会话里两次工具往返后注入 typed overflow。同一次恢复里先后两个 `compact_live_tool_summary` 请求：工具循环 PTL 回收摘要命中 92%；回合中归档摘要命中 4/61234。原因有二：cachecompact2 让它发空工具 + `none`、历史只带上一代摘要；`CompactRequestRecovery.select` 在摘要前就卸掉了主请求历史（为了摘要期间不驻留旧请求），这条路拿不到原样前缀。
- **做法**：`select` 在卸历史前（只在没有已结束历史、要走回合中归档时）冻结 `ActiveTurnCacheFork`：主请求提示、之前的原生历史、当前回合原生 IR，以及同一份完整投影给出的工具与 system 指令；交给 `ActiveTurnArchiveCompactRequest.cache_fork`，发送摘要即清。`active_turn_compact._summary_source_material` 在 `_cache_fork_history` 成立时（每个被替代调用的结果都恰好在主请求 IR 里出现一次）用“主请求前缀截到最后一个被替代调用的结果 + 末尾摘要要求”，tool_choice 交有界发送按 auto 走（模型若真返回工具调用，沿原兜底改走无工具分段链，调用永不执行）；否则退回 cachecompact2 的窄输入。被替代范围、候选计量、checkpoint/CAS 都不变。
- **代价与边界**：摘要请求的总输入变大（带着主请求前缀），但这部分在服务端缓存里；离线场景总计量 15,067 → 66,568、其中 61,980 命中。截断点之前夹着的保留调用会被摘要多看到（无害，覆盖范围不变）。卸历史后的内存释放推迟到这次摘要发送之后。
- **取代**：cachecompact2 节的“active-turn 边界”（明确不承诺前缀）只在结构化条件不满足时继续成立。
- **单次摘要上限（同一轮发现的第二个根因）**：缓存安全单次摘要原先也受 `compact_summary_budget`（窗口 80% 减输出预留，09-10 定的）限制；压缩触发点改成窗口 90%（用户决定）后，要压缩时主请求已经 ≥90%，任何带主请求前缀的摘要都超这个预算、改走分段（summary-only system、无工具，永不命中）。生产窗口/触发点：DeepSeek 100 万/90 万、luna 50 万/45 万、sol 27.2 万/24.48 万——这就是辅助调用一直 0% 的主因。新增 `compact_cache_surface_budget`（窗口减输出预留，与主请求同一口径）：只给带主请求工具的缓存安全单次请求用；分段、无工具请求仍用 80%。供应商仍判超窗时沿原逻辑减半预算改分段。
- **兜底来源分离**：分叉把主请求整段前缀放进 history 只为命中缓存；机械兜底与完整回退改读新字段 `LiveToolHistorySummaryRequest.fallback_history`（分叉路径给被替代来源，其余调用方为 None、沿用 history）。不分开时，子代理 9 轮超窗用例里兜底摘要把任务原文、运行时事实也抄进去，主请求每代多涨约 7K（实测之前历史 8.9K→52K，修后与原代码一致 1.5K→11K）。
- **验证**：见 TESTS.md 同名节。未验证：真实 DeepSeek/GPT 的命中与计费（上线后用 `3a-scripts/usage_by_model.py` 看 auxiliary 行）。

## 订阅登录请求带 session-id 缓存亲和头（gptcache，3a，2026-10-05；状态：已实现，待部署后用生产账本核对命中率）

- **来源（结构化事实）**：10-05 按 model_usage 账本统计近 24 小时主调用：DeepSeek（官方，Chat 接口）6152 次、缓存命中 98%；`gpt-6-luna` 4387 次、命中 71%、未命中输入约 3.5 亿 token；`gpt-6.1-sol` 810 次、命中 10%、未命中约 1.15 亿。两个 GPT 配置都走同一个 ChatGPT 订阅登录服务商（WebSocket）。线程最后一次调用的缓存诊断显示请求前缀（system/tools/历史）没变，服务端仍未命中——问题在路由，不在前缀。
- **根因**：官方 Codex（`codex-rs/core/src/client.rs` 的 `responses_session_id`，注释“ChatGPT derives cache affinity from the Responses session-id header”；头名见 `codex-api/src/requests/headers.rs` 的 `build_session_headers`）每个请求都带 `session-id` 头，根代理的值与请求体 `prompt_cache_key` 相同。我们只在请求体带了 `prompt_cache_key`，订阅服务商配置的 `session_header` 为空，所以没有这个头。
- **做法**：`provider_headers.chatgpt_session_uuid()` 把宿主会话编号（owner+thread 的 sha256）前 128 位排成 UUID；`responses._generate` 在 `auth mode=chatgpt` 时请求头加 `session-id`、请求体 `prompt_cache_key` 改用同一个 UUID（官方根代理两者同值）；WebSocket 握手沿用同一份请求头。未绑定会话不加（缓存亲和是优化），绝不生成随机值；非订阅服务商请求逐字节不变。
- **代价**：订阅线程的缓存键形态变了，上线后每个活跃线程第一次调用冷启动一次。
- **没做（下一步候选）**：① 官方在一个回合内复用同一条 WebSocket，并回放服务端给的 `x-codex-turn-state` 粘性路由令牌；我们每个请求新开连接、不回放令牌。是否还需要，等 session-id 上线后看命中率再定。② Compact 等辅助调用的命中率是 0%（DeepSeek 一天约 2800 万未命中输入）：摘要请求的前缀和主线程不同，下一刀可以让摘要请求复用主线程的前缀（系统提示 + 工具 + 历史，最后追加摘要指令）。
- **验证**：见 TESTS.md 同名节。

## 命令源长度上限：解析前的结构化拒绝（cmdcap + cmdcap-fix，2026-10-05，worker/cmdcap，基于 17l 头 886965f3a；本地验收完成，待 3a 终审）

- **来源（结构化事实）**：ds5 初审 shellwrap5 时实测，顶层命令没有长度上限：1MB 的单个参数在命令策略检查里光 stdlib shlex.split 就要 18.4 秒（纯 Python 逐字符），会卡住对应线程；嵌套源已有 32K 上限（COMMAND_NESTED_SOURCE_TOO_LARGE）。
- **做法**：`contracts/gates/command_policy.py` 的 `evaluate_command_policy` 与 `analyze_command`（两条 shlex 入口）在解析前加长度闸门：str 形态超限直接返回 `COMMAND_SOURCE_TOO_LARGE`（新登记码：与嵌套码并列、恢复提示面向"整条命令太长"场景，写"大段内容请改用 write_file 写文件"；argv 形态不经过 shlex 不做闸）。实测 1MB 拒绝耗时 0.01ms（原 18.4s）。**判定收敛到唯一共享函数 `command_source_too_large(text)`**，阈值是模块常量 `_COMMAND_SOURCE_MAX_CHARS = 65_536`（注释含 1KB≈0.02ms / 64KB≈0.9ms / 256KB≈3.8ms / 1MB≈18.4s 实测耗时）。
- **cmdcap-fix（luna6 初审三条必须改 + 3a 改判）**：
  - **①owner 串扰（初版缺陷）**：初版把上限放在模块级可变变量、由注册入口注入——网关一个进程服务多个 owner，A 配 0、B 配 65536 时"最后注册的说了算"，会串到别的 owner。**3a 裁定改成模块常量、去掉配置项**：`command_policy_max_source_chars` 从 yaml / AgentConfig / RegistryParams / 注册入口全部删除，前端配置目录重新生成。**3a 理由（对原任务第 2 条"可配置"的改判）**：这是解析成本的资源上界，与嵌套 Shell 源的 32K 上限同类（后者本来就没有开关）；按 owner 传值要穿过 10 个调用点（8 个文件），其中几处拿不到 owner 配置，容易漏，也容易再出现"配置改了不生效"；大段内容本来就该用 write_file，没有调大上限的需求。
  - **②controlled_exec 慢解析（初版缺陷）**：`subagents/controlled_exec_gateway.plan_controlled_exec` 先调 `_parse_command`（直接 shlex.split）之后才进 plan_shell_command——长输入在被拦之前已慢解析一遍。现在入口最前先过 `command_source_too_large`，超限按同一码拒绝。
  - **③负数配置（初版缺陷）**：初版 `max(0, ...)` 把负数变 0（=不限制）——配置项删除后此路径消失。
  - **④其它 shlex 盘点**：子代理文本解析点接入共享判定——`capability_request_identity._command_name`、`capability_scope._requested_command_name`（超长按"无命令名"fail-closed）、`patch_apply_helpers.validate_patch_test_command` + `_patch_test_argv`（超长按"阻止"返回、空 argv 跳过）；其余逐个确认不接：`shell_source`（嵌套源解析，位于命令策略闸门之后）、`verification/project_facts`（事后核验已执行命令，位于闸门之后）、`command_arguments`（插件命令参数域，非模型命令文本）、`shell_syntax`（无 shlex，纯字符串扫描）、`sandbox`/`plugin_completion`（shlex.quote，非解析）。
- **验证**：见 TESTS.md 同名节（9 条新用例 + 3 变异全杀 + 38 文件连带 + 配置类 3 文件 + guards9）。
- **已知边界**：argv（list）形态不受闸（不经过 shlex）；上限按字符数（非字节）。

## opp 集成到 17l（opp-final，2026-10-05，`worker/opp-final`，基于 17l 头 `e665afdb6`；已实现，3a 接手修复后集成到 17m）

- **3a 接手修复（opp-final-fix2，2026-10-05）**：沙箱外跑插件相关 146 个测试文件（基线对照见 TESTS 同名节）定位并修复：
  - 老用例 28 条：`/plugins enable` 改为两步确认（`PLUGIN_CONFIRMATION_REQUIRED` 是安全边界，保留），用例统一改走 `plugin_enable_fixtures.enable_with_confirmation`（同次预览的确认命令、目录版本与授权编号）；只测调用/注册/卸载语义、不测沙箱的老链路显式用 `unrestricted_service`（与 opp 之前的运行条件一致），原有断言不放宽；两步确认后 attempt 计数 2→3、按授权编号查状态，随协议更新。
  - **产品缺陷**：`gateway_parts/plugin_command_service.execute_plugin_control` 先 `replace(scope, event_hub=hub)`，scope 不是 dataclass 时抛 TypeError，被兜底吞成 `OUTCOME_UNKNOWN`（“未能确认插件请求结果”），违背函数自身合同；改为仅 dataclass 实例才 replace（来源 M1 B6 `24c3aec0f`，生产 IM scope 恒为 dataclass，属潜伏缺陷）。
  - opp 自带用例 `test_unavailable_sandbox_refuses_enable_and_explicit_calls` 假设“先确认后拒绝”，与产品合同（`_preflight_failure`：安全拒绝都在确认与副作用之前）相反，按合同改正用例顺序。

- **修复补记（opp-final-fix，2026-10-05）**：v1 解码回归根因——旧协议迁移（v1/v2/v3）的字段集检查不认 v4 新增的顶层 `legacy_permission_grant`，而"由新格式删字段模拟的旧表"会带上它。修法：`_legacy_fields_match` 在三个旧协议入口允许并忽略该字段（迁移产物强制 None，不给新权限任何 grandfather 入口）；schema 断言一行随 v4 升级更新。全批对照新增 0。

- **范围**：opp 线 11 个提交按序 cherry-pick 到 17l 头，并压成 5 个正式提交（盘点文档 → 授权事实与管理员配置 → 确认与重新启用 → B7 启动复核 → 身份比对修复与用例）。冲突只在 DESIGN_LEDGER/TESTS 顶部插入：两边内容都保留；撤回的 tref2/SLP-1A/estcache 段落是 17l 现状，未被 opp 上下文带回。常数目录重生一致（912 项，无 diff）。
- **数据迁移（上线口径）**：v3 安装表在加载时按显式旧来源迁移；`migrate_v3_entry` 只把"升级前已启用、非内容型、无新权限"的激活标成 `legacy_compat`（六字段：policy_version/mode/source_schema/activation_id/package_sha256/installation_ref）；迁移是纯重放、幂等（v4 表不再迁移），新表随下一次写操作落盘；失败/超预算抛错不覆盖原表，原字节摘要存 `migration.source_sha256`，17i 回滚用 `export_plugin_installations_v3.py` 导出。停用的老插件与已重新确认的不加兼容，需管理员按新规则重新确认。
- **验证**：72 个引用文件分批跑（每批 subprocess 超时 900s）；与基线 `e665afdb6` 逐条对照（交集 61 文件）：基线 75 条失败 vs 当前 76 条，**新增 1 条回归**（见下），无"变好"项；guards9 190 passed；ruff/import/doc_sync（--base e665afdb6）/code-size hard=0/size_diff 新增 0/diff-check/clean 全过。
- **新增回归（必须修）**：`test_plugin_configuration.py::test_v1_is_read_only_until_atomic_migration_with_configuration` 在基线上通过、在本分支稳定失败（`PluginInstallationError: 安装状态不可读`，`plugin_install_store.py:77`）——opp 的 v1/v2/v3 迁移链改动使 **v1 安装表解码失败**（只读 `snapshot()` 即抛）。属产品回归；修后重跑该文件与全批。
- **沙箱外必跑清单**（沙箱内 75 条既有失败 + 1 条回归）：test_plugin_any_language*、test_plugin_enable、test_plugin_invocation、test_plugin_mcp_transport、test_plugin_registry、test_plugin_release、test_plugin_removal、test_plugin_update、test_plugin_workspace_context、test_plugin_sandbox_v8、test_workspace_peek_package、test_host_command_stream、test_plugin_m1_joint_*、test_plugin_legacy_*。

## 工具默认收起第二阶段与出厂默认开启（toolfold，2026-10-05，分支 `worker/toolfold`，基于 17l `e665afdb6`；待初审）

- **背景**：10-02 工具瘦身第一期已给 8 个“又大又少用”的工具声明 `default_deferred`（开关默认关）；生产按用户授权开启三天，每次调用 `tool_schema_tokens` 约 1.7 万且全程稳定（评测出厂默认下为 28.3K）。本段按“生产近 3 天 + 评测 90 次运行双零使用”再收 6 个管理/通道类工具：`cancel_session_task`、`memory_search`、`publish_audit_update`、`send_message`、`send_session_message`、`stop_named_work`（合计约 4.4K token/次；生产口径每次调用从 ~21.4K 降到 ~17.0K）。
- **出厂默认改开启**：`tool_default_deferral_enabled` 默认值 YAML 与 `AgentConfig` 同步改 `true`（依据：10-02 用户授权“工具这一块按你的来，别影响工作”、生产开启三天无异常、省量见上）；关回 `false` 即恢复全量直出，注释写明方法。
- **保持直出的既有合同**：`audit_records`（I6 实测收起后模型没去搜索、IM 用户自查失败）、`create_goal`/`update_goal`/`cancel_subagents`（递归代理和持续目标控制属于主链，orchestration/goal 默认直出）、生产在用的 `terminal_session`/`read_artifact`/`session_search`/`process_session` 等。
- **观测增强（不改判定）**：`cache_diagnostics.request_surface` 增记工具名清单摘要（SHA256）与工具个数；`compare_request_surfaces` 在 run 第一次调用（无 previous）也附上本次自己的 `current_tool_count`/`current_tool_names_digest`，供下次定位“第一次调用后收窄”的机制；公开投影按白名单只收数字与 64 位十六进制摘要。
- **后台续跑豁免（3a 2026-10-05 裁定）**：`background_tool_policy.BACKGROUND_CONTINUATION_REQUIRED_TOOLS`（所有后台 profile 工具目录的并集）里的工具不受 `default_deferred` 折叠、始终直出——后台回合没有用户在场，靠 tool_search 找回工具的窗口极窄；过滤处 `registry._declared_deferred_names` 引用该常量，不另写名单。`test_background_runtime_snapshot_contains_continuation_tools` 不改断言通过；豁免作用于同一过滤入口（前台同样直出），非续跑声明工具照常收起。
- **收益口径（2026-10-05 精算）**：新装用户（默认配置、全工具注册）相比本次改动前再省 **~1.46K token/次**（memory_search 312 + publish_audit_update 961 + stop_named_work 184；cancel_session_task/send_message/send_session_message 因进入后台续跑目录被豁免）。**生产（现状基线）实际再省 ~184 token/次**（stop_named_work；其余 5 个：3 豁免 + memory_search 未注册 + publish_audit_update 普通会话不可用）。生产实测 tool_schema 约 1.7 万，改动后约 16.8K。
- **已知边界**：`memory_search` 默认不注册（`enable_memory_search_tool` 默认关），其标记只在开启时生效；`publish_audit_update` 只在 Audit 准备回合可用，测试按快照可用性跳过。

## 续跑产出回写原请求的结构化关联（contref，2026-10-05，ds9 实现、3a 非作者审查并收尾；随 17p 集成）

- **来源（问题清单 #21「续跑产出未回写请求」+ subwait/subwait2 只读调查 + 3a 裁定 b2）**：子代理完成触发的续跑回合，产出会经 canonical 消息送达 TUI/飞书（用户无需操作即可看到），但请求级记录（`terminal_response`）停在第一段回合的回复；评测/程序化取数从请求记录取数时拿不到最终回复。3a 裁定采纳 b2：请求照旧收口，续跑回合交付 final 时把结构化关联记到原请求名下。
- **权威位置与字段**：`conversations/tasks/<原请求 id>.json`（ThreadTaskLink）新增 `continuation_refs`（每条含 `turn_id`、`message_id`、`content_chars`、`delivered_at`、`background_delivery_reason`、`source_run_id`，不存正文）与 `continuation_diagnostics`（解析失败/冲突时的固定原因诊断，与 refs 互斥）。空集合完全省略键，旧档案重写后字节不变（与 capability_selection 同一序列化口径）。
- **身份链路（不猜）**：只用结构化来源——wake 信封的 `source_agent_id`（来源 run）+ `parent_agent_id`（父 run），并用来源 run 的持久父级（`agent.subagents.load(...).parent_id`）交叉验证；不一致按冲突处理（不静默选一侧，两侧事实进诊断），取不到写固定原因诊断（`continuation_source_missing` / `_source_load_failed` / `_parent_missing`）。禁止按时间或线程推断。
- **写入点**：`conversation/runtime._complete_background_slice` 交付收口后调 `continuation_refs.record_continuation_ref`——只处理 `subagent_runner_finished` 且 `persisted=True`（canonical final 落盘）的交付；中间回合（partial/mailbox 抑制、message-tool 直投）不写。幂等按 `message_id` 判重（重复 wake/冻结重投不重复记账）；写入失败不影响交付主链路。诊断写到 wake 自带的 `root_task_id` 档案（目标档案缺失时同处落 `continuation_target_missing`）。
- **管理员完整结果**：`gateway_parts/http_handlers._send_archived_terminal_result` 管理员分支附 `continuation` 投影（`ref_count` + `latest` 结构化指向，不复制正文）；公开投影（IM/TUI 公开面）不带该键，展示行为不变。
- **不加开关（理由）**：只新增结构化事实（任务档案字段 + 管理员投影键），用户可见行为不变、无额外 token/网络/后台任务；按项目「配置开关」口径属「内部结构整理，外部行为不变」。
- **工具说明（候选 c）**：`create_subagents` 说明补一句——回合结束后子代理完成会自动触发续跑回合、结果写入会话（TUI/飞书可见）但不更新已返回的本次响应；需要子代理结果作为本次交付时应在结束回合前等它们完成或先交付阶段结果。**缓存影响**：工具定义变化一次 → 所有会话的 prompt 前缀首次 miss 一次，之后恢复；无其它运行时影响。
- **3a 审查与收尾（2026-10-05 晚）**：身份链路用生产结构化事实核对——主工作区子代理记录的 parent_id（gwreq 930、srun 32、goal-task 26、subagent 6）全部有同名会话任务档案，且子代理完成事件契约要求 `parent_agent_id == root_task_id`，所以“父 run id 即原请求档案 id”成立。size_diff 的 4 条近上限告警已拆平：交付三件套收成 `ContinuationDelivery`（`record_continuation_ref(runtime, delivery)`、`_build_ref_row(delivery, resolution)`）；`_send_archived_terminal_result` 改收 `server`（取 paths 与 agent），返回体拼装抽到 `_archived_result_body`。补 HTTP 返回体用例（管理员有 continuation、公开视图没有）。
- **已知局限**：`_archived_result_body` 用 Gateway server 的 agent（本机主账号的会话档案）读投影；管理员查看其他 owner（如飞书用户）的请求时读不到对方档案，按“无续跑”处理、不报错。要支持需按请求 owner 解析会话存储，留作后续。
- **验证**：见 TESTS.md 同名小节（新用例 10 条 + 变异 6/6 + 连带测试 + 门禁 + Linux 车道）。

## estcache 挑入 17l 后撤回（estcache，2026-10-05，3a）

- **撤回原因（Linux 车道发现，macOS 复现）**：`test_compact_text_source.py::test_many_message_summary_avoids_whole_json_copy` 断言压缩取材峰值低于整段 JSON 的一半，estcache 后峰值 2,385,150 > 1,239,645。3a 实测：对 1200 条（每条约 2KB）消息估算一次，估算缓存留住约 1.6MB 的指纹结构（嵌套元组，平均每条约 1.3KB，`tokens.py:229/252/253/264`），被算进压缩峰值——不是整段拷贝，但指纹留存过重。二分：630be9dcc 通过、2b059ec2d 失败。
- **漏网原因**：该用例不直接引用估算器（经压缩层间接调用），按引用 grep 的连带清单没有收进它。
- **退回作者的要求**：缓存键改成紧凑摘要（如单个整数或短字节摘要，写清碰撞口径），缓存留存按字节或条目给出可测上界；本用例与全部 tracemalloc 内存用例（`git grep -l tracemalloc agent_py_agent/tests`）必须通过；估算数值逐位不变的对拍保留。
- 撤回提交：`c3333797d`（注释校准）、`33602a988`（estcache 本体）。

## 输出上限截断的轮内续跑对真实供应商响应可达（truncfix，2026-10-05，分支 `worker/truncfix`，基于 17l 头 `630be9dcc`；待非作者初审）

- **来源（隔离测试实例的结构化事实）**：小说方向 DeepSeek deepseek-v4-flash 6 次中 1 次失败（NOV02-ds-t2）：`request_state=failed`、`error_code=MODEL_RESPONSE_TRUNCATED`、`turn_end_reason=max-tokens`，16 次调用、401.9 秒。逐次事件：末次调用输出恰为 **65536 tokens 且全部是思考**（thinking_delta 65536 条、无正文、无工具调用）——模型把 64K 上限全花在推理上、没产出任何可交付物；写文件参数没有被截断（两次 write_file 参数进度 2.2KB 都正常 ready）。
- **上限出处**：`DEFAULT_MODEL_MAX_TOKENS = 65536`（`settings/defaults.py:10`，用户 2026-09-27 定的"所有模型统一 64K"）；`effective_max_output_tokens = min(配置值, 窗口//4)`（窗口 1M → 仍是 65536），后端工厂把它写进 `backend.max_tokens` 并作为请求的 `max_tokens` 发送（`backends/factory.py:54`、`backends/http.py:214`）。代码里没有 deepseek-v4-flash 的单独输出上限登记（`sampling.py` 只处理 top_p）；官方上限未联网核对。**结论：不是配置错误**——供应商按 65536 执行，问题在恢复链。
- **根因**：`agent_core/tool_loop/response_decision.py` 的 R248 轮内续跑（`_TRUNCATED_OUTPUT_RESUME`，预算 2 次）挂在"无 runtime 字段"分支上；而 `backends/response_completion.py:36` 对 length/max_tokens 停因**总会**写 runtime 三件套（unfinished / MODEL_RESPONSE_TRUNCATED / model_provider），`_no_tool_calls_decision` 的通用 runtime-status break（`response_decision.py:855`）先命中，续跑对真实截断**永远不可达**（本运行续跑 0 次；写恢复 `_native_truncated_write_decision` 也 0 次，因为写没被截断）。单测 `test_truncated_output_resume.py` 用的是不带 runtime 字段的响应形态，所以合同一直绿、真实路径一直死。
- **改法（最小通用）**：`_no_tool_calls_decision` 的 runtime-status 分支先试 `_truncated_output_resume_decision`——只认既有结构化三件套（`_is_provider_length_truncated`），消费同一条 `truncated_output_repairs` 预算（2 次），超限或非长度截断原样 break；续跑指令补一句"上一轮只有推理顶到上限时不要再展开长推理、直接产出第一块"。写恢复与 native 协议门不动。
- **边界**：文本协议下 provider 长度截断此前直接 break（f0f7fdedd 起），修复后与原生协议一致进入同一续跑预算；native 写恢复仍只在原生协议生效（`test_real_length_stream_does_not_enter_native_write_recovery_in_text_scope` 已按新语义更新并保留原守卫断言）。续跑预算用尽后仍按 unfinished/MODEL_RESPONSE_TRUNCATED 收口（不吞供应商终态）；部分正文随响应对象保留，请求级失败语义不变。
- **验证**：见 `TESTS.md` 同名小节（provider 形态 3 条新用例 + 全量引用文件清扫 + 4 变异全杀）。

## tresume 初审修正（tresume2，2026-10-05，分支 `worker/ds10-tresume-fix`，在 `4a6c66f31` 之后；待 3a 复核）

- **来源**：ds10 初审（必须改 1 + 小问题 2）；3a 10-05 裁定必须改按推荐 a 落地。
- **改法**：
  - 用满提示：`conversation/turn_resume_notice` 新增 `PROVIDER_RESUME_LIMIT_NOTICE`（“这一轮被模型接口故障反复打断，已停止自动续跑；发‘继续’可以接着做。”）；`request_execution._provider_transient_turn_resume_marker` 在 `count >= max_count` 时把该句写进失败答复的 `user_error`/`error`，TUI 与 IM 从同一份答复字段读取（沿用 TURN_RESUME_LIMIT 先例的三处一致口径）。
  - 标记清理：`request_worker._finish_claimed_gateway_request` 在重排写盘失败退回正常归档前清掉 `provider_transient_resume`，不让它落进终态响应与 responses 投影。
  - 删除 `recovery.py` 里一段重复的函数注释（refactor 复制残留）。
- **验证**：见 TESTS.md 同名节（用例 12 passed；m6/m7 变异全杀；12 文件必跑 173 passed；guards9 全绿；门禁全过）。
- **未验证**：真实 Gateway 上用满场景的 TUI/IM 端到端（3a 真机复核）。

## tresume 重排失败退回的插话收口（tresume3，2026-10-05，分支 `worker/ds10-tresume3`，基于 17l 头 `886965f3a`；已实现，待初审）

- **来源**：3a 终审发现：`_handle_gateway_request` 拿到续跑标记时跳过 `_settle_pending_gateway_guidance`（留给续跑回合认领），但供应商标记到 worker 收尾才真正重排；重排失败退回正常归档时本回合插话没人收口（探针实测：读失败/文件消失形态插话停在 reserved、归档也失败）。
- **改法（做法 A，改动小、身份来源清楚）**：`request_worker._finish_claimed_gateway_request` 重排失败退回分支先清标记、再显式 `_settle_fallback_turn_guidance(conversation_store, request_id)`（防御式取 `guidance.recovery.reject_pending(request_id, reject_reserved=True)`，失败静默留给 recovery 兜底）；身份只有 request_id（与本回合同源）。正常归档时 terminalize 也会收口（写失败形态本来就被它覆盖），显式收口覆盖归档失败（读不到请求文件等）的形态且幂等。重排成功路径不变（插话留给续跑回合）。
- **对照做法 B（把重排提前到 `_handle_gateway_request`）**：能拿到 failure，但要重构 finally 时序、给 worker 加“已重排”信号（否则二次重排）、在 request_execution 里复现失败收口流程——改动大、新状态多；provider 场景的 failure 属瞬时类（释放不计次），传 None 与传异常等价，A 的信息劣势不存在。
- **验证**：见 TESTS.md 同名节。

## 供应商临时故障的回合级自动续跑（tresume，2026-10-05，分支 `worker/tresume`，基于 17l 头 `630be9dcc`；待非作者初审）

- **来源**：transient-investigate 只读调查 + 3a 裁定：允许回合级供应商故障自动续跑（与进程崩溃重排同族，不违背“普通任务不自动续跑”的 wake 口径）；每个请求最多 2 次、跨 Gateway 重启累计；加配置开关；白名单只认结构化错误码；续跑从已落盘历史继续、不重放工具；提示走 turn_resume_notice；不改发给供应商的请求内容。
- **改法**：
  - `gateway_parts/request_execution._provider_transient_turn_resume_marker`：失败收口按白名单（`request_errors.PROVIDER_TRANSIENT_TURN_RESUME_CODES`：MODEL_STREAM_INCOMPLETE 需本回合已有完成工具往返、PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED 无条件）+ 配置开关 + 未达上限（`provider_resume_count` 跨重启累计）判定；命中返回 `provider_transient_resume` 标记，回合不写失败终态。
  - `gateway_parts/request_worker._finish_claimed_gateway_request`：收尾见到该标记时调 `recovery.requeue_provider_transient_processing` 把请求重排回 inbox（status=pending、priority=recovery、固定退避 30/120 秒、active_turn_recovery 写 cause=provider_transient_resume）；重排失败退回正常失败归档。
  - `gateway_parts/recovery`：新 `requeue_provider_transient_processing`、`provider_resume_count`；`_active_turn_recovery_marker` 重启重排时原样保留 provider_resume_count（跨 Gateway 重启累计）。
  - `conversation/turn_resume_notice`：新 cause 文案“模型接口临时故障打断了这一轮，已自动续跑。”；用满上限走现有 TURN_RESUME_LIMIT 提示。
  - 配置：`agent_config.yaml` + `AgentConfig.provider_transient_turn_resume_max_count: 2`（0=关闭）；前端配置目录已重新生成。
- **边界**：请求拒绝类（403）不续跑；零产出流中断照旧走响应级重试；用户已停止不续跑；坏计数 fail-closed 不续跑。续跑那一轮与用户手动“继续”走同一条组装路径。
- **验证**：新用例 7 条 + 3 变异全 KILLED；58 个相关测试文件全跑（2 个沙箱基线失败；1 个既有断言按新字段更新后全绿）；guards9 全绿。见 `TESTS.md` 同名节。
- **未验证**：真实 Gateway 上的供应商故障端到端（需要真实故障注入）；Linux 车道由 3a 跑。

## 启动指纹三态比较收口（starttime3 3a 终审修复，2026-10-05）

- **macOS 改用 sysctl 读启动时刻（3a，2026-10-05，17n 收口时发现）**：starttime 线把 runtime_db/scheduler 的读取统一到 heartbeat 后，macOS 每次 attempt 激活、claim 都要起一个 `ps -o lstart` 子进程，与 opp 的“插件配置不得启动进程”用例冲突（macOS 才失败，Linux 读 /proc 不受影响）。更要紧的是 `ps -o lstart` 的输出随调用方语言和时区变化（实测同一进程：C 下 `Mon Oct  5 20:26:21 2026`，zh_CN 下 `一 10月/ 5 20:26:21 2026`，TZ=UTC 下是次日 03:26），录入方与核对方环境不同就会把活进程判成 different（唯一死亡证据）。改法：`common/heartbeat._read_darwin_starttime` 用 ctypes 调 `sysctl(kern.proc.pid.<pid>)`，读 `kinfo_proc` 开头的 `p_starttime`（timeval），返回“秒.微秒”数字串；进程不存在时内核返回 0 字节 → None；非 Linux 非 macOS 返回 None；删除 ps 读取，不留兜底。迁移：17m 在 macOS 上 runtime_db/scheduler 记录的是 None，tool_operations 的旧 lstart 文本与新数字不可比 → unverifiable，不判死（租约到期和 PID 不存在照常接管）。未改：`gateway_parts/daemon_metadata`、`tooling/process_registry` 仍用 `ps -o lstart`，同样受语言/时区影响（网关与 TUI 环境不同时可能误判代际），留作下一刀，改格式时要处理新旧进程混跑期。
- **插件释放的执行器校验（3a 根因定位，2026-10-05）**：17m 收口时 Linux 车道和 macOS 都发现 starttime 线让插件停用返回 outcome_unknown。根因：`plugin_release.preparation_exit` 在删除插件环境前按数字（`_valid_time`）校验执行器 `start_time`，而 starttime 线把它统一成 heartbeat 字符串指纹，校验抛 `preparation_executor_missing`，释放失败 → 停用的工具操作停在 UNKNOWN → 收尾判 unknown。旧代码在 macOS 读不到启动时间（None）恰好跳过校验。修法：heartbeat 新增 `start_fingerprint_is_valid`（与 `start_time_relation` 同一归一规则），plugin_release 改用它（None 允许，布尔/空串/NaN 含字符串 "NaN" 拒绝）。教训：改记录格式要盘点所有读者（ds5 沙箱里的批量失败曾被归为环境问题，掩盖了这一处）。
- **来源**：ds6 初审发现 `start_time_matches` 的字符串分支在“一侧数字、一侧 macOS lstart”时返回 False（判死），`scheduler._runner_liveness` 的字符串分支不等即判死，与“不可核验绝不判死”的口径相反；空串记录同样会判死。
- **改法**：`common/heartbeat.start_time_relation` 作为唯一三态实现（same / different / unverifiable）：同格式数字按数值比，同格式字符串按去空白后相等比；格式不可比、空串、NaN/inf、布尔、缺失一律 unverifiable。`start_time_matches` 改为它的布尔投影（只有 different 为 False）；scheduler 判活改用它（same→alive、different→dead、其余 unverifiable），删除旧的 `_legacy_numeric_liveness`。
- **状态**：已实现，随 starttime 线并入 17m。

## 启动指纹读取统一与 schedstart 初审修正（starttime2，2026-10-05，基于 17l `33a569b3e`；待初审）

- **来源**：schedstart（`41991b6b0`）初审"必须改"两条——① runtime_db 的 runner 指纹比较仍是 `float()` 比较，与 heartbeat 字符串指纹不等，Linux 活进程会被误标 unknown；② heartbeat 的 ps 读取不看返回码，非零返回码带输出被当有效指纹。本分支 cherry-pick 后一并修复，并继续 starttime2 原计划（统一其余读取点）。
- **schedstart 修正**：heartbeat 新增 `start_time_matches`（旧数字记录与新字符串指纹双口径；任一侧缺失、跨表示、非有限一律不可核验、不判死）；`_read_ps_starttime` 对非零返回码/超时/空输出一律 None；runtime_db `_mark_dead_runner_attempt_unknown` 改用双口径比较；executor_liveness 本地 `_same_start_token` 删除、改用同一函数。迁移口径：旧数字记录在 Linux 上按数值与字符串指纹比较；macOS 的 lstart 与数字不可比时不判死。
- **starttime2 原计划**：`local_storage/tool_operations` 的启动标记读取（原含 Linux /proc 与 darwin /bin/ps 两条分支）与 `runtime_db._start_token`（原仅 Linux /proc，comm 含空格会错位、非 Linux 恒空）统一走 `common.heartbeat.process_start_time`；落盘类型（字符串）与比较语义（空值保守判活）不变；Linux 值逐字节不变，macOS 由 `/bin/ps` 等价改为 `ps`。**读端一并统一**：`runtime_db/operations.holder_is_alive` 的内联 /proc 读取同改（否则写端 rsplit 与读端 split 在 comm 含空格时不一致；macOS 也因此首次能核对启动指纹，存量空指纹记录仍保守判活）。
- **未改（下一刀）**：`gateway_parts/daemon_metadata._get_process_start_time`、`tooling/process_registry.capture_process_birth_token`；差异盘点见本轮交接报告。
- **验证**：新增/改写用例（ps 三态、/proc 解析、比较矩阵、runtime_db 新旧格式矩阵、tool_operations 判活三态）；变异 3/3 KILLED；连带测试与门禁结果见 TESTS.md 同名小节。未验证：真实 macOS ps 输出链路（沙箱内 ps 被禁，用打桩覆盖）、非作者初审。

## starttime3：锁写端 _start_token 统一与 tool_operations 比较双口径（starttime3，2026-10-05，在 c16ec16da 之后；待初审）

- **修正 starttime2 的不实描述（3a 核对）**：上一轮报告/注释声称 `runtime_db._start_token`（写端）已统一走 heartbeat，实际代码从未改过（仍是 /proc 全文 split、非 Linux 恒空）。后果：macOS 锁记录指纹恒空（读端保守判活，"可核对"未实现）；Linux 进程名含空格时写端取错字段、读端（rsplit）正确，比较不等 → 活持有者的锁可能被接管。本提交把 `_start_token` 改走 `_proc_start_time`（heartbeat 字符串指纹），与读端 `holder_is_alive` 真正同源。
- **tool_operations 比较双口径**：`_operation_holder_is_live` / `_reconciliation_marker_is_live` 由 `expected == actual` 改走 `start_time_matches`（旧数字 token 与字符串指纹数值等价时不判死；确证不同才判死）。
- **验证**：新增 7 用例（写端走 heartbeat、写读同源、macOS lstart 写读、空值、旧数字 token 三态）；变异 3/3 KILLED；连带全量清单 211 文件分批跑完（含 batch3 拆小重跑），失败 57 条全部在基线 `c16ec16da` 复现（含 `test_host_files_access.py` 超时——基线同样超时），无本轮引入失败；门禁全过。见 TESTS.md 同名小节。

## SLP-1A 挑入 17l 后撤回（slp1a，2026-10-05，3a）

- **撤回原因（Linux 车道发现）**：过期 claim 的原执行者进程仍存活时（常见于同一个 Gateway 进程里某回合异常结束、没释放 claim），SLP-1A 判为 recovery_pending；而 `_acquire_conversation_run_claim` 的等待循环没有时限，`test_plugin_m1_joint_tool_gate.py` 卡在 run_claim.py 的领取循环 40 分钟。生产上会让该会话之后所有回合一直“等待执行车道”，直到重启 Gateway。
- **退回作者的要求**：同进程且没有活动续租的过期 claim 允许接管（或以进程内注册表判活）；等待有上限并给出结构化结果；挑入前补跑 test_plugin_m1_joint_tool_gate.py、test_plugin_m1_joint_e2e.py 等用到会话车道的联动测试。撤回提交 39f9f45cd、efa6a0496，代码回到挑入前。

## 逻辑引用编号误当写路径守卫（artrefguard，2026-10-05，基于 17l `1fc40dd1d`；待初审）

- **来源**：owner home 调查（ownerhome-investigate）确认 `gwreq-…:call_…` 目录是模型把工具输出的逻辑引用（scoped_call_id）当相对路径传给文件写工具、在 cwd（无任务工作区时 = owner home 根）下落地的垃圾；本件是三层修法的第一层（守卫）。
- **实现**：新增 `common/logical_reference_ids.py`——产品编号前缀（gwreq-/run-/subagent-）与 call_ 前缀的唯一共享常量 + `is_logical_reference_segment`（前缀开头且含 ":call_"）；`gateway_parts/io.new_gateway_request_id`、`agent_core/runtime/run_params.py`、`agent_core/_finalization_service.py` 的生成点改为引用同一常量。`filesystem_artifact_guard.logical_reference_write_path_error` 对相对路径逐段判定；写工具统一入口 `resolve_write_path` 在任何解析/落盘前拒绝（`WriteScopeError` + `ARTIFACT_REF_AS_WRITE_PATH`，error_taxonomy 已登记为 artifact 类、REPAIR_TOOL_ARGUMENTS）。绝对路径与既有 blobs/tool_outputs 重定向不变。
- **边界**：`request-` 前缀没有产品生成点（只在历史正则出现），未纳入；subagent- 的复合形态（"subagent-run:xxx:call_…"）按前缀开头判定命中。
- **验证**：新 `test_filesystem_logical_ref_guard.py` 5 用例（判定集合/绝对路径不拦与中间段命中/统一 resolver 拒绝/正常路径/三写工具真实入口错误码、零文件效应及模型视图无宿主临时路径）；相关回归一批 87 项全过；4 个单点变异全 KILLED；常数目录无变化（只收数值常量，`--check` 一致）。命令见 TESTS。
- **未验证**：真实会话里模型行为回归（需真实链路观察）、非作者初审。

## 清扫与收口诊断的两处收口修正（obsfix12b，2026-10-05，分支 `worker/obsfix12`，基于 `fe40b3bc7`）

- **来源（obsfix12 初审判"必须改"）**：第 2 条的清扫把"未启动 CLAIMED"（handler_started_at=0）也翻成 UNKNOWN——它是 `_active_turn_operation_recovery_report` 唯一可安全忽略的形态（其余一律进 blockers），清扫抢先翻后 `recover_recorded_active_turn_attempt` 从 recovered 变 blocked（operation_outcome_uncertain），唯一的自动恢复出口被卡死。**修法选"清扫排除未启动 CLAIMED"**（而非直接写 CANCELLED）：与既有语义分工一致——未启动没有副作用风险，留给恢复报告的安全忽略与 `_cancel_unstarted_tool_operations` 的取消路径；清扫的"翻 UNKNOWN"语义前提是"副作用可能已发生"，对确定未启动的占位不适用。已启动的 CLAIMED（handler_started_at>0）与 EXECUTING 照翻。
- **小问题 1**：status_conflict 去重查询补 attempt_id——/recover 后新执行者再次死亡时，第二次诊断不再被旧 attempt 的记录吞掉。
- **小问题 2**：补"attempt 状态终态但 ended_at=0 不清扫"的负例（初审判定 `aa.ended_at > 0` 条件缺用例）。
- **用例**：含未启动工具占位的死亡根回合清扫后仍能自动恢复（recovered=True 且占位被取消）；同一 run 换代后的新 attempt 仍能写出自己的 status_conflict；ended_at=0 不清扫（两巡幂等）。
- **验证**：见 `TESTS.md` obsfix12b 节（含 3 个变异、连带测试与基线复核）。

## runner 收口 unknown 空转修复（obsfix12 第 1 条，2026-10-05，分支 `worker/obsfix12`，基于 17l 头 `f7849d7ff`）

- **来源（生产实证）**：每天 400–1000 条 `status_conflict` 事件全是同一个 unknown run 刷出来的。根因：执行器死亡把 run 置 `unknown`（`executor_liveness.mark_exited_attempt_unknown`；unknown 只有人工 /recover 能变），该 run 有一条子代理结果收口 WAL，60 秒一巡的 `recover_pending_closeouts` 每次重试 `settle_agent_run` 都 fail-closed 追加一条无去重的 `status_conflict`，返回被当成"可重试"。
- **修法 A（诊断去重）**：`settle_agent_run` 的 unknown 分支写事件前查同 `agent_run_id` + `reason=unknown_run_status` 的既有 `status_conflict`，有则不写（照 `orphan_reclaim_blocked` 的"同身份+原因只写一次"模式）。状态机与返回值不变。
- **修法 B（收口重试退避）**：`runtime_closeout.advance_pending_closeout` 在 settle 前做退避——`closeout_state` 属于可重试集（write_error/unknown_status/missing_run）且 `attempts > CLOSEOUT_RETRY_BACKOFF_AFTER_ATTEMPTS_COUNT` 时，要求 `now − updated_at ≥ min(CLOSEOUT_RETRY_BACKOFF_MAX_SECONDS, CLOSEOUT_RETRY_BACKOFF_BASE_SECONDS × attempts)` 才再试，否则返回 `backoff_wait`，不刷新事实、不写事件。选退避而非"移出可重试改挂起"的理由：退避对所有可重试形态一并生效、避免扫描风暴，且 /recover 后最迟 1 小时内自动重新推进，不需要新增唤醒路径；"挂起"会让 run 修好后也要再等一次人工触发。三处常量在模块内并带中文说明。
- **用例**（`test_closeout_recovery_dependencies.py`）：三连跑 status_conflict 恰好 1 条且三次都真的重试；attempts 恰在阈值时第 2/3 次退避不再 settle（计数替身）、诊断不增长；/recover 后窗口期过即成功收口、清账、通知一次；/recover + 新回合后旧 WAL 被拒（stale_attempt）且 run 不被误收口。
- **验证**：见 `TESTS.md` obsfix12 节（含 4 个变异与门禁结果）。

## 终态 attempt 孤儿工具操作清扫（obsfix12 第 2 条，2026-10-05，分支 `worker/obsfix12`）

- **来源（生产实证）**：11 条 `EXECUTING` 工具操作从 9 月挂到现在。根因：`EXECUTING→UNKNOWN` 只有换代事务（`_supersede_noncurrent_attempts_conn`）一条路；执行器死亡（attempt 直接标 unknown）、孤儿回收、人工 /recover 都不翻 op，`find_orphaned_attempts` 又只看非终态 attempt 的锁，看不到终态 attempt 下的残留。
- **改法**：`reconcile_superseded_attempts` 同一巡加清扫 `_cleanup_terminal_attempt_operations_conn`——attempt 已终态（done/failed/cancelled/unknown/recovered 且 ended_at>0）而 op 仍 CLAIMED/EXECUTING 时，按 `mark_operation_unknown` 语义只翻 op 为 UNKNOWN（保留 claim 元数据 + 原因码 `terminal_attempt_op_cleanup`），每个 op 一条 `tool_operation.terminal_attempt_cleanup` 审计事件；不动 attempt/run/锁。幂等：只匹配未结状态，翻过不再匹配。
- **用例**（`test_runtime_db_operations.py`）：unknown+EXECUTING、recovered+EXECUTING、cancelled+CLAIMED(已开始) 三条被翻并留事件、claim 元数据保留；running+EXECUTING 跳过；重复巡检幂等。
- **未验证 / 已知边界**：macOS 上 `holder_is_alive` 没有 /proc 时一律判活——本次只在交接记录，不改（另有 scheduler 判活补丁在审）。
- **验证**：见 `TESTS.md` obsfix12 节。

## 锁失败语义与跨进程证明补钉（obsfix34c，2026-10-05，分支 `worker/obsfix34c`，基于 17m 头 `886965f3a`；待初审）

- **来源**：luna4 初审两条必须改 + 小问题。变基到 886965f3a：文档冲突两边保留并删掉 tref2 旧标题，常量目录重新生成（921 项）。
- **必须改 1（锁失败不再当数据损坏）**：`gateway_parts/io.read_json_file_report` 在 catch-all 前单独识别 `BlockingIOError`/`InterruptedError`，报告新增 `lock_busy` 标记；`input_delivery_service` 新增 `_raise_for_input_read_report`，receipt 读取/已有请求/turn 索引四处“load_error→DataCorruptionError”按“锁忙→BlockingIOError（可重试）/其余→数据损坏”分流；端到端用例从锁身份核对重试耗尽一路到 input_delivery 断言 BlockingIOError。
- **必须改 2（跨进程互斥常驻证明）**：新增 `test_cross_process_lockers_never_overlap`（spawn 3 进程 × 20 轮，enter/exit 日志断言任何时刻 max_active==1）；补 `try_locked_file_transition` 身份不匹配 fail-closed 用例。
- **小问题**：`common/json_io._descriptor_matches_lock_path` 补 `# LLM:` 注释。
- **未做（超时）**：顺带项（TRANSIENT_ERROR/PROVIDER_TIMEOUT 进续跑白名单）未做，见交接报告。
- **验证**：见 TESTS.md 同名节。

## 请求锁 sidecar 清理协议加固（obsfix34b，2026-10-05，worker/obsfix34b，基于 2a3a7f21c；本地验收完成，待 3a 终审）

- **背景**：obsfix34 初审用探针实证"先 close 再 unlink"竞态窗口；3a 补充判断"阻塞等待的迟到者"在持锁 unlink 下仍可拿到旧 inode 锁。本件按"两边一起改"的标准协议修复，并实证了初审未发现的两个问题。
- **清理者**（`gateway_parts/io._remove_orphan_lock_file`）：① 改为在**锁文件本体**上非阻塞试锁——原实现经 `_open_lock_handle`（数据文件路径入口）会再加一层 `.lock`，实际探测 `<name>.lock.lock`，**活锁保护形同虚设**（探针实测：真实持有者持锁时照样删）；② 试锁成功后**持锁 unlink** 再 close；Windows 上 unlink 失败放弃本次删除（不回退成先 close 再删）。
- **加锁方**（`gateway_parts/io._locked_file_path`、`common/json_io._locked_file_path`、`try_locked_file_transition`）：拿锁后核对 `os.fstat(fd)` 与 `os.stat(路径)` 的 `(st_dev, st_ino)` 一致；不一致说明锁文件被换/删（阻塞迟到者场景），close 后重开重试（上限 `_LOCK_IDENTITY_RETRY_COUNT`/`_JSON_LOCK_IDENTITY_RETRY_COUNT`=5），超上限抛 `BlockingIOError`；非阻塞探测路径身份不符按"没拿到"返回（fail-closed）。
- **quota 映射核实**：任务第 3 条的前提不成立——`ProviderQuotaExhaustedError` **自带 `error_code='PROVIDER_QUOTA_EXHAUSTED'`**，`fallback_error_code` 第一步原样保留，从未落 `UNKNOWN_ERROR`；已撤销临时映射分支并加用例钉住真实来源。
- **验证**：新测试 `test_lock_sidecar_cleanup_protocol.py` 5 条（活锁不删回归、Windows unlink 失败不删、清理+阻塞迟到者+新来者交错 max_active==1、重试上限、json_io 重置换锁后正常）；`test_gateway_io` 活锁构造改用锁文件本体持锁；4 变异全杀（双重后缀、去核对、上限写死、quota 映射）；性能实测单次 `locked_json_path` 52µs（含 fstat+stat）；相关 8 文件 + guards9 全过；size_diff 新增 0（首轮 2 条 nesting 经拆平归零）。
- **已知边界**：Windows 分支只按"unlink 抛 OSError 即放弃"实现与测试，未在真实 Windows 跑；inode 复用（unlink 后立即创建复用同一 inode）会让核对恰好通过——不影响正确性（同 inode 即同锁）。

## 请求锁 sidecar 周期安全清理（obsfix34 问题3，2026-10-05，分支 `worker/obsfix34`，基于 17l 头 `f7849d7ff`；待非作者初审）

- **来源（结构化事实）**：生产 Gateway `requests/pending` 下 1435 个、`processing` 下 1588 个空 `.lock` 文件，最老超过 7 天，而真正在跑的请求只有 19 个（obsfix 只读调查第 3 条）。
- **根因**：请求文件的每次读/写都经 `_locked_file_path` 给数据文件建同名 sidecar `<name>.lock`（`gateway_parts/io.py` 与 `common/json_io.py` 同一写法）；flock 锁的是 inode，**不能**在释放时顺手 unlink（等待者会锁到旧 inode、新读者锁到新 inode，互斥失效），于是所有建过锁的目录（队列四个目录、终态副本、响应目录、每个请求都会建锁的 `turn_transitions`）都在累积，且全仓没有任何删除路径。
- **改法**：新增周期安全清理，挂在派发 tick 的第五个后台段（`cli/gateway_loops.py`，节拍 1 小时、独立退避），实现 `gateway_parts/io.py:cleanup_orphan_lock_files`。只删**同时满足**三条的 sidecar：① 对应数据文件已不存在；② mtime 早于 24 小时宽限（`_ORPHAN_LOCK_MIN_AGE_SECONDS`，避开“文件刚删、旧句柄还在读”的窗口）；③ 非阻塞 `flock(LOCK_EX|LOCK_NB)` 拿得到（拿不到=活锁，跳过）。单文件失败只跳过，绝不抛给维护巡；宽限不设配置开关（纯维护动作、无行为影响）。
- **边界**：只清孤儿 sidecar，不动数据文件、不动活锁；`turn_transitions/*.transition.lock` 同机制同清理（`.transition` 数据文件存在则保留）。
- **验证**：见 `TESTS.md` 同名小节（a/b/c/d 四态用例 + 幂等 + 目录清单；常数目录 912→914）。

## 兜底错误码映射到已登记码（obsfix34 问题4，2026-10-05，分支 `worker/obsfix34`，基于 17l 头 `f7849d7ff`；待非作者初审）

- **来源（结构化事实）**：失败请求的 error_code 里出现 PROVIDERTRANSIENTERROR、PROVIDERTIMEOUTERROR、RUNTIMECONFLICTERROR、VALUEERROR 这类“异常类名大写”的码（obsfix 只读调查第 4 条）——两处兜底直接把 `type(exc).__name__.upper()` 当码写账。
- **根因**：`gateway_parts/request_execution.py`（请求失败兜底）与 `conversation/runtime.py`（后台 wake claim 失败收口）都没有把无码异常映射到 `contracts/error_taxonomy` 的登记码；`ProviderTransientError`/`ProviderTimeoutError`（无 error_code 属性）与 `RuntimeConflictError`、裸 `ValueError` 都会泄漏类名，客户端拿不到文案与恢复语义。
- **改法**：新增唯一映射 `runtime_errors.fallback_error_code(exc)`——保留异常自带 `error_code` 第一优先；`ProviderTimeoutError→PROVIDER_TIMEOUT`、`ProviderTransientError` 族（含子类）`→TRANSIENT_ERROR`、`RuntimeConflictError→RUNTIME_CONFLICT`、其余（含 ValueError）`→UNKNOWN_ERROR`；两处调用点共用这一份。`RUNTIME_CONFLICT` 在 `contracts/error_taxonomy` 新登记（category=state、不可原样重试、先重读运行状态再决定）。
- **验证**：见 `TESTS.md` 同名小节（四类映射 + 未知兜底 + 已有码保留 + 登记守卫，6 条）。

## 自改工作树提示补一句“按集成者指定的工作树做”（selfdevrule，2026-10-05，3a）

- **问题**：Owner Scope 提示词的“my-agent 自身代码”一段写着“不改其他检出目录”。10-05 sol3（gpt-6.1-sol）把它当成高于任务的规则，两次拒绝在集成者分派的 worker 工作树里改代码，只能改派。
- **改法**：同一句后补“集成者在任务里明确指定了别的工作树时，就在那个工作树里做”。仍是工作约定，不放宽也不收紧写权限；写权限照旧由 Full Access 的结构化边界裁决。
- **影响**：管理员会话的系统提示变一句，随 17l 部署只付一次提示词缓存变化。用例：`test_prompting_builder.py` 断言新句子存在。

## PTY 会话泄漏修复（ptyleak，2026-10-05，分支 `worker/ptyleak`，基于 17j 头 `da6e38093`；ds10 初审，3a 终审修正后挑入 17l）

- **来源（真机实锤）**：生产 Gateway 名下 `bash -o pipefail -c git grep ... && git grep ...` 进程挂约 9 小时（CPU 0；0/1/2 号文件都是伪终端 `/dev/ttys024`；子进程 git grep 处 S+）。判断：命令在 PTY 会话里跑，git 看到 TTY 起分页器等按键永不退出；回合早已结束，PTY 会话没有被收回。
- **根因**：`pty_sessions.py` 的会话只在显式停止路径（`/stop`、`freeze_process_stop`、子代理取消、后台资源报告）经 `request_stop` 收回；**回合正常收口与任务结束都没有回收调用点**。`_prune_finished` 只清“已结束且 fd 已关”的历史对象；`_drain` 读不到 EOF 不退出。分页器等按键的进程永远不产生 EOF，于是进程与会话永久残留。
- **改法（三层网）**：
  1. **回合/任务收口回收**：`FinalizationService.finalize()` 收尾调用 `_reclaim_turn_pty_sessions()`（`agent_core/_finalization_service.py`）。身份只取宿主结构化字段——owner 地址与工具侧同源（工具拿的是写边界 `canonical_owner_home_root` ← `home_paths.owner_home_dir`，**不是** `effective_owner_scope_root`：本机管理员 Full Access 时后者为空，用它会对不上而静默漏收）；线程取 `task_attributes.conversation_thread_id`；任务取 `conversation_task_id` 或本轮 `task_id`；run 取本轮 `run_id`。回收实现 `reclaim_pty_sessions()`（`pty_sessions.py`）：先按 (owner, thread, task) 收整个任务，再叠一次按 run 精确收（覆盖子代理树根差异），去重后写结构化回执 `{scope, status, session_ids, error_type}`；`finalize` 里回收失败只记回执、不影响收口结果。
  2. **空闲兜底**：`PtySessionRegistry.set_idle_timeout_minutes()` + `sweep_idle()` + 唯一 daemon 巡检线程（间隔=阈值/4，夹在 0.5~30 秒）。阈值来自新配置 `pty_session_idle_timeout_minutes`（默认 30 分钟，0=不限制）；装配点在 `registry_bootstrap._register_network_tools`，未装配时注册表保持关闭（单测/嵌入式不受打扰）。
  3. **防呆默认**：`_subprocess_text_env` 默认 `PAGER=cat`、`GIT_PAGER=cat`（宿主环境已显式设置的值保持不动），PTY 与普通命令共用同一环境构造函数，git 不再起分页器。
- **配置三处同步**：`agent_config.yaml`（中文注释）、`AgentConfig` dataclass（默认 30）、参数登记（`parameter_registry` 从 AgentConfig 自动派生）；并显式登记 `_BOUNDARY_NAMES`——模型不能把它调大或关掉（同 `plugin_tool_gate_timeout_ms` 口径），用户改配置文件。
- **验证**：见 `TESTS.md`“PTY 会话泄漏修复（ptyleak）”小节（含 3 个变异与全门禁结果）。
- **3a 终审修正**：① 新配置项 `pty_session_idle_timeout_minutes` 同步重新生成前端配置目录（ds10 初审的必须改项，原先 `test_backend_config_catalog.py` 红）；② 巡检线程改为持锁登记并启动——原实现在锁外 `start()`，并发设置阈值时可多开巡检线程；③ 补用例钉住“effective_owner_scope_root 为空时仍按 home_paths 的 canonical owner home 回收”。
- **未验证 / 已知边界**：真机（沙箱外）sandbox-exec 路径与 `termination.confirmed=True` 由 3a 用强制能力模式复跑（`test_reclaim_pty_sessions_confirms_termination_outside_sandbox`）；子代理树根差异只做了合并回执设计，没有真机子代理用例；**Gateway 停机路径没有新增回收调用**（本次范围只到回合/任务收口；停机时 PTY master 关闭会给前台进程组发 SIGHUP，但忽略 HUP 的进程仍可能残留，交由空闲兜底与后续 17k 计划评估）；真实 Gateway/TUI/IM 端到端未跑。

## scheduler 判活跨平台启动指纹（schedstart，2026-10-05，基于 17l `dea220623`；待初审）

- **来源**：SLP-2A 终审接受的已知边界——`scheduler/repository._process_start_time` 只读 Linux /proc，macOS（生产）恒 None，判活只剩“进程号在不在”，PID 复用会一直判活、旧 claim 卡住。
- **改法**：runner 启动指纹改用 `common/heartbeat.process_start_time`（Linux /proc、其它平台 `ps -o lstart`，统一字符串），记录（`claim_run` 的 `runner_start_time`）与比较（`_runner_liveness`）同一种表示。兼容旧落盘记录：数字（/proc ticks）只在当前指纹可转数字（Linux）时比较，转不了（macOS lstart）一律 unverifiable、不判死；None 仍只看进程号。三态语义不变：只有“进程不存在”或“指纹可读且不同”才算死亡。
- **共用方随迁**：`runtime_db/repository` 与 `runtime_db/executor_liveness` 原借用 scheduler 的 `_process_start_time`（RUN-01 共用判死实现，注释即“禁止另写第二份”），随迁到 heartbeat；executor_liveness 的 replaced 判定改为字符串直接比、数字仅在可转时比，不可比不报替换（不误判死亡）。
- **验收**：新增 macOS 风格指纹判死、旧数字记录三态兼容用例；改写现有判活用例全部通过；4 个单点变异全 KILLED；scheduler 17 文件与 runtime_db 相关组通过（9 项 `test_scheduler_waiting_deadlock` fixture 失败为基线既有、节点一致）。命令见 TESTS。
- **盘点（未改）**：另有 4 处各自实现的启动令牌/时间：`local_storage/tool_operations._read_process_start_token`、`tooling/process_registry.capture_process_birth_token`、`runtime_db/repository._start_token`、`gateway_parts/daemon_metadata._get_process_start_time`；收口建议与迁移注意见本轮交接报告（3a 收进问题清单）。
- **未验证**：真实 macOS 的 ps 输出链路（沙箱内用替身覆盖）、生产 claim 恢复真实场景、非作者初审。

## SLP-2A Scheduler claim 栅栏（slp2a，2026-10-05，基于 17k `812828b98`；ds8 初审通过，3a 终审修正后挑入 17l）

- **3a 终审修正（必须改项）**：死亡证明成立后按结构化状态分流——`claimed`（还没 mark running，动作未开始）转 `queued` 由新 epoch 领取；`running`（动作可能已产生副作用）转 `unknown` 终态、不自动重跑，与 SLP-2A 之前的崩溃恢复一致。原实现把 running 也重新排队，在 SLP-2B（定时动作复用 operation_id、经操作账本回放）接好之前会重复副作用，违背 SLEEP_RESUME 第 3.2 节“确认死亡后再决定恢复、unknown 或终态”和“未知不自动重试”。
- **已知边界（接受）**：macOS 没有 /proc，`_process_start_time` 读不到，判活只剩进程号；进程号被复用时会判活并一直保持旧 claim（fail-closed，不误领但可能卡住，需人工或 SLP 后续补 macOS 启动时间）。

- **改动**：普通 `claim_run` 与 `recover_interrupted_executions` 共用 runner PID/starttime 三态死亡证明；lease 过期但 runner 活着或身份不可核验时不另发 claim。death proof 后先在同一 owner store 锁内持久 CAS 为 `queued`，之后只由 `claim_run` 生成 `claim_epoch + 1` 的新 token。heartbeat、release、mark-running、finish 校验 claim id 与 epoch；旧 run 缺 epoch 时按 legacy epoch 0 读取。
- **边界**：实现限于 scheduler repository 与聚焦测试；未改 `scheduler/service.py`，未碰 conversation/tooling；SLP-2B operation_id/操作账本接线仍未做。misfire grace 政策未改。因 `check_doc_sync` 要求同步模块文档，另更新 Gateway 进度与结构文档。
- **验证结论**：最终 19 文件 scheduler sweep 仅有 `test_scheduler_waiting_deadlock.py` 9 项失败，均在任务基线 `812828b98` 复现；其余相关测试通过。guards9 前 11 项通过，第 12 项因基线缺该测试文件而未运行；import boundaries 0 findings、Ruff、doc-sync、strict code-size、size_diff（新增告警 0）通过。具体命令及结果见 [TESTS.md](TESTS.md) SLP-2A 节；设计合同见 [SLEEP_RESUME.md](docs/design/SLEEP_RESUME.md)。

## Gateway 插件事件线程解析缺失统一短路与瞬时续跑状态（tref3，2026-10-05，worker/tref3；已实现，待初审）

- **问题与合同**：`gateway_event_context` 曾允许空线程进入 Gateway 发布上下文，导致 `turn_started`、`command_executed` 可产生空 `thread_ref`；原 `prompt_queued` 会自行早退，但没有统一诊断。m1_joint 要求每个回合事件具有非空会话引用。
- **实现**：统一在 `gateway_event_context` 先确认开关，再验证线程 id 为非空文本；缺失/无效时不构造上下文，调用 `warn_event_assembly_failure(PLUGIN_EVENT_THREAD_UNRESOLVED)`，只写固定 `event`/`reason_code`，不带渠道、会话号或错误正文。prompt 线程解析失败交由该权威入口处理；开关关闭仍零路由读取、零线程解析。`turn_ended` 依据结构化 `restart_resume` 或 `provider_transient_resume` 标记发布 `interrupted`，供应商瞬时故障重排不是失败。
- **调用方盘点**：`event_points.prompt_queued`、`turn_started`、`command_completed` 与 `agent_core/tool_call_runtime._tool_event_context` 都仅为插件事件装配上下文；线程分别来自 Gateway preflight、控制回执 `conversation_thread_id` 或 RunScope 中的同一会话线程属性，均适用缺线程不发布规则，没有例外。
- **边界**：只短路可选观察，不改变队列、命令回执、工具执行或续跑业务状态；真实 Gateway/外部沙箱验收由 3a 后续复核。

## 插件事件 thread_ref 统一为会话线程 + channel_conversation_ref（tref2/tref2b，2026-10-05，worker/tref2b；**撤回原因已修复，待非作者初审**）

- **撤回原因（3a，10-05 10:5x，Linux 车道发现、macOS 复现）**：`test_plugin_m1_joint_tool_gate.py` 7 条失败——同一回合 3 个事件的 thread_ref 出现两种值（prompt_submitted 为空、其余为线程哈希），违反“每个事件的会话引用不许为空、同一回合只有一个引用”；`test_plugin_m1_joint_e2e.py::test_event_watch_real_gateway_six_counts_and_readonly` 也失败。挑入时只跑了插件事件三个测试文件，漏了这两组联动测试。撤回提交 9e3073882、c7851feef，代码回到挑入前。

- **问题（ds4 在 jb6b 发现，tref 只读调查）**：同一回合里插件事件的 `thread_ref` 两类取值不同——Gateway 侧四事件（prompt_submitted / turn_started / turn_ended / command_executed）取 `channel_conversation_id`（渠道会话号），工具侧两事件（tool_call_started / tool_call_finished）取 task attribute `conversation_thread_id`（ConversationStore 线程），插件按会话归组会归不上。
- **权威判定**：会话线程的权威是 `ConversationStore thread_id`（`request_execution.py` 写进 task attributes 的 `conversation_thread_id`/`session_id`，注释“结构化 thread 是当前长期 IM/TUI 对话的耐久身份”；记忆 recall canonicalize 为 `session:<thread_id>`）；`channel_conversation_id` 是渠道侧键（ChannelBinding），不是线程身份。
- **修法（3a 裁定采纳方案 A，不加配置开关，事件目录升 v2）**：`thread_ref` 统一为会话线程哈希（`plugin_events/points.py` 的哈希规则不变，改喂给它的输入）；Gateway 侧事件新增 `channel_conversation_ref`（渠道会话哈希，同一规则）。逐点：turn_started/turn_ended 在回合解析后传入线程（`request_execution._gateway_turn_thread_id`，只读 preflight、与执行路径同一解析入口，取消/跳过路径不经过；解析失败留空）；prompt_submitted 入队时点线程可能未解析 → `thread_ref` 留空 + 渠道 ref 有值（A2）；command_executed 回执新增 `conversation_thread_id`（`control_operation_service` 执行时按冻结渠道身份只读解析一次，多 owner 先物化 owner agent，失败留空；重放/对账路径不重解析、不回填旧行）；工具侧不改（本就是线程）。
- **兼容**：字段名与值形态不变、旧插件无需改码；按旧语义建过索引的插件在切换点断一次（发布说明）。宿主不落观察事件（PLUGIN_EVENT_HOOKS D6），无账本回填对象；runtime.db 的 `plugin_gate.decided` 行字段白名单不含会话字段，不受影响。
- **验证**：见 TESTS 同名节（167 passed、3 变异全杀、门禁结果）。
- **3a 终审收紧**：`turn_started` 改收 `thread_id_of`（可调用），确认插件事件开启后才解析会话线程；关闭时（生产默认）一次都不调，不为事件多查一次会话库。ds8 提的两条风险按现状接受：握手版本不升、旧插件按旧语义建的索引在切换点断一次（写进发布说明）；`channel_conversation_ref` 与 `thread_ref` 同为 sha256 哈希，对所有事件订阅者可见，插件由管理员安装，暂不加按字段的权限门。
- **tref2b 返工（2026-10-05，worker/tref2b）**：撤回原因根因是 `prompt_queued` 的 `thread_id` 硬编码为空（A2 设计）；m1_joint 合同（“每个回合事件都有非空会话引用”）不接受留空。修法：提交时点用与执行路径同一预检入口（`preflight_gateway_conversation`，`get_or_create` 幂等）解析会话线程，解析不出线程时不发事件；关闭零解析（91664648d 初衷）保持；`channel_conversation_ref` 语义不变。A2 与“prompt_submitted thread_ref 为空”的旧断言全部修订；m1_joint_e2e 事件字段集合随 v2 目录加 `channel_conversation_ref`（合同演进，非放宽）。4 变异 KILLED；25 文件连带测试仅 3 条嵌套沙箱既有失败（基线 `31492d002` 复核一致）。

## J16 第 9 节：观察截图 _meta 协商（vision2，2026-10-05，worker/vision2，基于 d2ef24d53；ds8 初审通过并补断言 7960c7577，3a 终审收紧后挑入 17l）

- **目标**：采纳 vision1r 初审结论，把"要不要截图"从"适配器无条件生成 + 宿主兜底剥离"改成宿主按档案结构化协商——一次修掉三件事：没声明时正文多一行 `[image content, mimeType=image/png]` 占位（与"逐字节相同"冲突）、适配器白付的缩略图成本、占位对不支持图模型的误导。
- **协商链**：`tool_call_runtime` 把 `model_accepts_images(agent)` 写进 `trusted_run_context` → `executor._handler_arguments` 注入内部参数 `__observation_screenshot`（模板同 `__process_completion_target`）→ `mcp_registration._declared_proxy` 只给观察工具（`observation` 类）声明该参数 → `observation_binding.screenshot_meta` 附进 `_meta` 扩展键 `my-agent/observation-screenshot`（v1，常量在 `plugin_observation`）→ 适配器 `_screenshot_requested` 从严解析（版本匹配且 enabled 为 true；缺键、旧宿主、坏形状一律不要图）→ `ScreenObserver.observe(want_screenshot=...)` 才生成缩略图。
- **从严与兼容**：旧适配器不认识新键 → 保持原行为，宿主侧 vision1 的剥离/落盘兜底不动；旧宿主不发键 → 适配器默认不要图。`observation_binding.execute` 的动作后处理收窄到 action_meta，观察工具不再误走 `_with_action_fact`。
- **注入侧换模型**：`_with_pending_observation_screenshot` 注入前按当前档案复核；落盘时支持图、注入时换到不支持图的模型 → pending 丢弃（已 pop）、不注入。
- **共享判定**：`_model_accepts_images` 从 `tool_call_archive_record` 提为 `conversation/input_media.model_accepts_images`（归档、协商、注入三处同源）。
- **验证**：新增 6 条用例（适配器计数探针、_meta 严格形状、真实渲染路正文逐字节/无占位、换模型丢弃、中断/重试不重复注入、执行器注入）+ 4 个假 observer 签名更新 + binding 断言更新；相关 7 文件 170 条（1 条既有失败 `test_mcp_client`，merge-base `d05a0d0753` 复核同败）；guards9 存在的 11 文件 187 passed（第 12 个 `test_backend_signature_guardrails.py` 本分支不存在）；变异 4/4 全杀；门禁全过（size_diff 新增 0/消失 50）。详见 TESTS 同名节。
- **3a 终审收紧**：剥离和落盘只对宿主核验过的观察结果生效——结果信封里有 `observation_binding.attach` 写入的 `observation` 才可能落盘，有 `observation_rejected` 只剥离不落盘；其它工具（任意 MCP 工具）返回的 image 块保持原样，不会被标成“observe_window 截图”注入下一次请求。
- **已知边界**：真实适配器（macOS 真机）与真实模型看到附图后的行为仍未验证；缩略图文件按内容寻址留 owner 附件目录，暂无自动清理（vision1 既有）；非观察工具返回的图片仍按 vision1 之前的老样子留在结果里（不剥离也不附图），要不要给它们开通用看图另议。

## J16 第 9 节：观察截图随观察结果附给能看图的模型（vision1，2026-10-05，worker/vision1；待非作者初审）

- **目标**：档案声明 `input_modalities` 含 `image` 时，`observe_window` 的结果附一张缩小后的窗口截图；没声明时行为完全不变；截图只给当次模型请求，不写进会话持久历史。
- **做法**：复用 ef 第 14 条"owner 附件 + 用户轮媒体块"的图片通道，不另开一条。适配器 `screen_observation.observation_screenshot` 生成缩略 PNG（宽度 ≤ `OBSERVATION_SCREENSHOT_MAX_WIDTH_PX`=1024、字节 ≤ `OBSERVATION_SCREENSHOT_MAX_BYTES`=256KB，纯标准库 zlib，失败静默）→ `computer_use_observation_tools.encode_call_result` 把截图转成独立 MCP image 内容块（不进正文/structuredContent）→ 宿主 `tool_call_archive_record.extract_observation_screenshot` 在归档投影前剥离图片块，并按档案模态决定是否落盘（`conversation/input_media.import_input_media_bytes`，与入站附件同一内容寻址目录）→ 引用进 `ToolResult.metadata.observation_screenshot` → `tool_ir_history.record_tool_call_ir` 登记 pending → `tool_model_generation._native_provider_messages` 注入一次（user 消息 + `local_file` 图片块）并消费。持久化路径（`_completed_turn_native_messages` 直接走 adapter 投影）不经过注入点，图片不进 IR/transcript。
- **为什么落盘**：通道的发送边界只展开 `local_file` 媒体块（哈希校验 + 体积上限）；内存直传会变成第二条通道。单张 ≤256KB，内容寻址写在 owner 私有附件目录。
- **fail-open**：没声明、坏 base64、缺 owner home、写盘失败都只剥离图片块，观察结果照常返回。适配器总是生成缩略图（档案判定在宿主侧；跨进程"要不要图"协商留待后续）。
- **验证**：用例 4 条（声明了附图、没声明不附、尺寸/字节上限、不进持久历史+只注入一次）+ 2 条接口用例；变异 2 个全杀；相关 9 文件 145 passed、guards9 全过；ruff/import boundaries/doc sync/strict code-size/clean package/diff-check 全过。
- **补单位表**：`_PX`（像素）补进 `scripts/build_constants_catalog.py` 的单位后缀表并重新生成目录（902 项）。
- **已知边界**：①没声明图的模型也会让适配器生成一次缩略图（CPU+传输，成本有界）；②档案不支持时工具结果正文里仍有 `[image content, mimeType=image/png]` 占位文本；③缩略图文件按内容寻址留在 owner 附件目录，暂无自动清理；④真实适配器（macOS 真机）与真实模型看到附图后的行为未验证。

## 缓存命中运维报告脚本（cachereport，2026-10-05，分支 `worker/cachereport`，基于 17k 头 `02acd6446`；待初审）

- **起因**：17k 上线（压缩调用也命中缓存）后要核对"省钱是否真的生效"，此前只能临时写脚本统计。本件把口径固化成仓库内只读工具 `scripts/cache_hit_report.py`。
- **口径（与 `store_usage.ModelUsageStore.summary` 同源）**：provider 分区取 call_count/input_tokens/cache_read_input_tokens/output_tokens；命中率 = Σcache_read ÷ Σinput（cache_write 不算命中）；estimated 分区单列——`estimated.call_count` 是缺报调用数（供应商未回报、只有本地估算），不与供应商口径混加；`purpose_breakdown` 是互斥分区（有则用、无则回退根 usage_breakdown 标 "(未分区)"）；天分组按本地时区。
- **路径发现**：复用 `scripts/reproject_model_usage.discover_ledger_paths`（owner home 三种已知布局：workspace/runtime/workspaces/*/conversations、conversations、data/conversations），不写死路径。
- **输出**：日期×用途表格（--by-source/--by-model 可选细分、`conversation_compact` 单独成行）；--since/--until 时间窗；--compare 输出基准日前后两列（调用数/输入/命中率/未命中）；--json 机器可读。只读、不写文件；坏行与读不了的文件跳过计数、不中断。
- **验证**：见 TESTS「缓存命中运维报告」；变异 2/2 KILLED（cache_write 混进命中、估算混进供应商调用数）。
- **未验证**：真实 owner home 数据（沙箱不读真实 owner home）；"省钱生效"的真机核对由 3a 用真实数据跑。

## 容器估算缓存的指纹内存修复（estcache2，2026-10-05，`worker/estcache`，在 `02614e198` 之后；本地验证完成，待复审）

- **来源**：estcache（02614e198）12 片 Linux 全量车道抓出回归——`test_compact_text_source.py::test_many_message_summary_avoids_whole_json_copy` 的 tracemalloc 峰值 2,385,150 超整段 JSON 一半（1,239,645）；1200 条 2KB 消息估算缓存留存 1.61MB，几乎全是指纹嵌套 tuple/frozenset（平均 1.3KB/条）。已从 17l 撤回（c3333797d、33602a988）。
- **修法**：指纹改成**单个 64 位整数摘要**（边遍历边把子摘要折进滚动摘要，每节点只建立即释放的小 tuple，不再保留与消息同量级的嵌套结构）；缓存值打包成**单个整数**（字符数左移 63 位 | UTF8 字节数）。条目上限 4096 不变。
- **碰撞口径**：两条内容不同的条目摘要碰撞概率约 2^-64/对（1200 条约 4e-14）；后果是那一条统计取错、估算数值偏差（估算本身是启发式，不影响状态机）。字典按插入顺序折入：内容同序必同摘要，键序不同只是多算一次，方向安全。
- **验证**：对拍 3017 形状 0 不符 + 53 个固定刁钻形状；43 文件（estimate_tokens 35 ∪ tracemalloc 9）约 3700 条全过；guards9 192 passed；内存 1200 条留存 173KB（阈值 242KB）；性能复测 35 万档估算 131.8→65.7ms、85 万档 348.1→126.4ms（收益保留且更快）。详见 TESTS.md。

## 容器估算合成缓存（estcache，2026-10-05，`worker/estcache`，基线 `6145136a8`；本地实现及验证完成，待非作者复审及 3a 终审）

- **来源**：luna2 剖析（35 万 token 合成回合、2 轮工具、3 次模型调用）指出整轮 CPU 2.53s 中 token 估算 0.51s 是最大一块；每次模型调用在 `agent_core/model/context_pressure.py` 把整段历史估算两遍（`projection.messages` 一遍、含 messages 的整份 payload 一遍），压缩试算与其它检查点还会再算。
- **改法（`memory_archive/tokens.py` 唯一估算器）**：容器估算加"内容指纹 → 直接子项长度"进程内 LRU 缓存（`_ITEM_LENGTH_CACHE`，上限 `_ITEM_LENGTH_CACHE_MAX_ENTRY_COUNT`）；序列/映射按 `"[" + 元素编码 + ", " 连接 + "]"`、`"{" + 排序键值 + "}"` 逐位合成，`estimate_tokens` 先试合成、失败回退原口径。
- **正确性边界**：指纹相同 ⟹ JSON 编码相同是命中前提（str 用 hash 摘要、标量与 float 用值/repr、容器递归组合、非 str 键与未知对象返回 None）；合成只在能证明"逐元素独立编码与整段编码逐位一致"时使用；超大元素（未过 512 KiB 有界证明）、环、编码异常、非 str 键一律放弃合成回退原口径；缓存不落盘、不跨进程、估算数值不因缓存状态变化。缓存只记顶层容器的直接子项，子结构纯算不记账（避免每层节点挤爆上限引发反复淘汰）。
- **验证**：等价性 3017 个形状（多形状 + 3000 随机结构）与原口径 0 不符；tokens 文件 51 passed；四变异 KILLED（id 键/漏分隔符/不淘汰/去有界检查）；详见 `TESTS.md`。
- **实测收益（合成历史、假后端、3 次模型调用，数字只作交接参考）**：35 万 token 回合估算 583.1→131.8ms（-77.4%）、整轮 1067.6→697.9ms；85 万 token 回合 1243.8→348.1ms（-72.0%）、整轮 1722.4→1060.9ms。
- **状态/边界**：不加配置开关（内部性能优化、结果不变）；真实 Gateway/TUI/IM 与生产负载未验证；`estimate_tokens_from_json_parts` 未改。

## 授权头凭据清洗泛化与转储清洗（rejectdiag4，3a，2026-10-05；状态：已实现，随 17o 集成）

- **来源**：rejectdiag3 初审（ds7）+ 3a 安全终审三条：① `_AUTHORIZATION_RE` 只认 Bearer 且值到空白就停，`Authorization: Basic …`、`Token …`、`Proxy-Authorization: …` 的凭据残留，并经拒绝诊断摘要进 user_error、落盘与审计；② `"authorization": {"type": "string"}` 的 `{` 被当值吃掉、破坏结构；③ `_dump_provider_rejection`（需显式 `MY_AGENT_PROVIDER_DUMP`）原样写响应头与正文，注释却写“不含密钥”。
- **做法**：
  - `common/log_redaction`：正则 `_AUTHORIZATION_KEY_RE` 只定位“键前引号 + 键名（含 proxy-）+ 键后引号 + 分隔符 + 值开引号”，值边界交 `_authorization_value_end` 线性扫描（值有开引号→同种未转义引号；键带引号值裸露→JSON 标量到 `,}]` 或空白；等号赋值→空白或 `&;,`/引号；其余请求头形状→换行或未转义引号；裸露请求头行里 `name="..."` 的 Digest 参数整段跳过；扫描上限是下一个授权键）。整个值都当凭据：保留方案名（RFC 7235 的 auth-scheme，便于排查），其后整段过 `_mask_secret_value`（`Bearer $TOKEN` 纯变量引用照旧保留）；选“保留方案名”而非整段替换，是因为方案名不是秘密、对排查“认证方式不对”有用。labels 模式（拒绝诊断摘要）沿 rejectdiag3 口径把键名到值结束整段换标记。值以 `{`/`[` 开头视为结构原样保留，并从键后继续找（嵌套里的真凭据照样遮）。
  - `backends/gateway_helpers._dump_provider_rejection`：响应头按“头名: 值”一起清洗后去前缀（`_dump_header_value`），正文先取前 `_DUMP_SCAN_CHARS`（16000）字清洗再截 4000 字；注释改成与实现一致。
- **调试中发现并修掉的两处**：Basic 值末尾 base64 填充 `=` 紧挨字符串闭引号时被当成 Digest 参数、越界吞掉后一个键（只在裸露请求头行启用参数跳引号）；为“值不跨下一个授权键”先扫到行尾再截回会退化成平方（7000 个键 10.7 秒，ReDoS 用例抓到），改成扫描上限直接定在下一个键前。
- **验证**：见 TESTS.md rejectdiag4 节（四方案端到端全字节扫描、转储用例、ReDoS 计时、7 个变异）。

## 拒绝诊断凭据清洗补全（rejectdiag3，2026-10-05，分支 `worker/rejectdiag3`，基于 9ffe07fc1）

- **来源（rejectdiag2 初审实测）**：两个清洗缺口——① JSON/键值形状的 `authorization` 键不被文本清洗覆盖（`{"authorization": "Bearer …"}`、`authorization=…` 原样泄漏；`_AUTHORIZATION_RE` 只认无引号 `Authorization: Bearer 值` 一种形态）；② `_safe_header_value`（白名单头值与 content_type 共用）只折叠+截断、不过凭据清洗，服务端在 x-request-id 等头值里回显密钥时明文进诊断、落盘与展示。
- **改法 A（授权头形状）**：扩 `_AUTHORIZATION_RE` 覆盖三种键值形状（无引号头、JSON 双/单引号键、等号赋值，大小写不敏感）：键名后可带一层引号与 `:`/`=`，值前的方案名（Bearer）留在结构里、值部分仍过 `_mask_secret_value`（rdv2"纯变量引用保留"合同不变），组 3 吃掉闭引号。**authorization 不进 `_CREDENTIAL_KEY_NAMES`**——实测词表路径会让无引号头被 `_SECRET_ASSIGNMENT_RE` 二次处理、把方案名当值打码（`Bearer $TOKEN` 变 `<redacted> $TOKEN`，被既有用例抓住），破坏合同；两份词表分工保持"文本清洗词表管普通键值对、授权头由专用规则管"。
- **改法 B（头值清洗）**：`_safe_header_value` 顺序改为"折叠 → 清洗 →（www-authenticate 裁剪方案名）→ 截断"，与正文摘要同口径；清洗先于截断，密钥被截断点切剩前缀碎片时不残留。
- **改法 C（权威入口）**：`rejection_diagnostics.py` 不再从 `tooling.mcp_client` 引兼容别名 `sanitize_credentials`，直接引 `common/log_redaction.redact_sensitive_text`（参数与别名一致：`redacted_marker="[REDACTED]"`、`redact_assignment_labels=True`），按"一个概念一个权威位置"收口。
- **用例**：授权头三形状打码 + 反例（authorization_mode/unauthorized 不误伤）；头值与 content_type 清洗 + 反例（req 编号、cf-ray 值不变）；清洗先于截断（跨边界无前缀残留）；集成用例遍历临时目录全部文件字节，断言明文不进回执、落盘与审计索引。
- **验证**：见 `TESTS.md` rejectdiag3 节（含 3 个变异、门禁与 2 个既有失败的基线复核）。

## 供应商拒绝诊断收尾（rejectdiag2，2026-10-05，分支 `worker/rejectdiag2`，基于 `54b48b625`；待非作者初审）

- **来源**：rejectdiag 初审（ds9）一条必须改 + 两条小问题：正文摘要会原样带出服务端回显的密钥（实测复现）；白名单头个数无上限（探针 200 个头撑到 16.6KB）；展示出口（TUI/飞书失败提示）没有原因码。
- **改法**：
  - 摘要清洗：`provider_rejection_diagnostic` 的 `body_excerpt` 先过统一凭据清洗（`sanitize_credentials`，与其它出站诊断同源）再折叠截断；顺序固定"折叠→清洗→截断"，避免截断把密钥切成半截逃过匹配。
  - 词表补漏：`common/log_redaction.py` 凭据词表补入 `cookie`（HTTP 凭据头；`set-cookie` 以 `-cookie` 结尾自动命中），服务端回显 Cookie/set-cookie 时不再漏遮。
  - 头上限：`rejection_headers` 加条数（≤16）与总量（≤1024 字符）双上限，超出丢弃尾部并记结构化 `headers_truncated` 标记；固定名单（x-request-id/cf-ray/retry-after/www-authenticate）优先于 x-ratelimit-* 前缀，重要字段不被限流头挤掉。
  - 展示出口：3a 裁定要显示。失败提示唯一投影位置=`user_error`（TUI 直接显示、IM 公开结果 error 回填 user_error、CLI 同源）；在 `request_execution` 失败分支（投影合流后）追加"原因码 + 请求编号类字段 + 已清洗的服务端说明"（`provider_rejection_user_notice`），不另开展示通道。模型可见投影与瞬时族投影键集合不变。
  - error 字段：`response["error"]` 会随 response 落盘并进审计索引，先过同一凭据清洗再入账。
- **边界**：`_dump_provider_rejection`（显式 `MY_AGENT_PROVIDER_DUMP` 调试通道）仍写原始正文，属用户显式开启的调试工具，不在本批范围。
- **验证**：见 TESTS；5 个变异全杀（去清洗、去头上限、展示漏原因码、error 不清洗、cookie 词表移除）；单元 15 条 + 集成 1 条。
- **未验证**：真实供应商 403 端到端（沙箱不连真实 provider）。

## 供应商拒绝诊断（rejectdiag，2026-10-05，分支 `worker/rejectdiag`，基于 17l 头 `6145136a8`；待初审）

- **背景**：17k 上线后 5 个 ChatGPT 订阅会话同分钟收到 HTTP 403 空响应体；回执只有“模型服务拒绝了本次请求，具体原因请查看请求诊断”，但“请求诊断”投影（`gateway_provider_error_projection`）刻意只暴露 typed `http_status`，403 空体没有任何可看的原因。
- **改法**：新增 `backends/rejection_diagnostics.py`——拒绝诊断唯一构造点：状态码、内容类型、响应体字节数、正文安全摘要（去控制字符、截断 200 字）、白名单响应头（x-request-id / cf-ray / retry-after / x-ratelimit-* / www-authenticate 方案名）、原因码（429→rate_limited、402→quota、401→auth、403→forbidden_unknown；其它不猜）。`_runtime_http_error` 各分支把诊断放进异常 details；`gateway_provider_error_projection` 对拒绝族追加 `cause_code`+诊断、对 429 等瞬时族只追加诊断与原因码（不改 user_error 与恢复语义）；WebSocket 握手 403 走同一 `_runtime_http_error` 自动获得诊断。
- **安全边界**：请求头、认证、cookie、完整响应头一律不记；白名单之外的头一律不记；正文摘要先替换 C0/C1 控制字符再折叠截断。
- **错误码**：4 个 cause 码登记进 error_taxonomy（`PROVIDER_REJECTION_*`；回执 error_code 仍是 `PROVIDER_REQUEST_REJECTED`）。
- **验证**：见 TESTS；3 个变异（白名单失效、控制字符不去、WS 不接头信息）全杀；guards9（12 文件）与静态门禁全过。
- **未验证**：真实供应商 403 端到端（沙箱不连真实 provider）。

## 缓存诊断组合反例返工（cachediag3，2026-10-05，`worker/cachediag3`，基线 `ffca71c54`；本地实现及定向验证完成，WIP：完整守卫缺文件；待非作者复审及 3a 终审）

- **来源**：cachediag2r 初审两条产品入口反例获 3a 认可：新持久基数＋旧内存身份重发把 1 算成 2；31→32、4097→4098 旧末块改写后追加误报纯追加。旧节为历史实施记录，以本节合同为准。
- **累计同源**：`cache_change_counts` 与不可逆调用身份集合 `cache_counted_calls` 同属较新 `model_metrics` 快照，经原 `model_usage.update_metrics` 和线程锁一起持久化、恢复；移除回合内单独身份及逐码 max 拼接。旧状态、清状态、交错重发均按同一身份集合去重。
- **有界身份**：新结算时收敛到原账本仍保留的调用明细；裁掉的调用无法经 exact call 再取到诊断，不会加次数。没有新诊断时不裁恢复的身份。只保存 SHA256，不保存 call_id 原文；旧快照缺身份可读且保留次数，但不反推升级前已计数调用。
- **尾块选保守不可比**：不加快照字段、不保存额外摘要、不重算正文。同 index 的共同链值不同仍证明改写；若没有改写证据，又没有共同点证明较短历史完整稳定，则 `comparable=false / partial=true`、不报历史原因，只保留已证明前缀。实际只追加也不能越过这条证明边界；组件/分区选项继续独立报告。
- **明确边界**：4096→4097 可证明纯追加；4096→4064 可证明纯截短；4096→4095 缺末点不可比，保留共享 4064。链值仍扫描全部消息，128 点仅限制定位，不是旧“只散列 512 条”。
- **验证/状态**：反例先红（10 项失败），相关九文件 273 passed、原十一守卫 187 passed、三处独立变异全部 KILLED；其余静态与尺寸差集通过、新增告警 0。当前外部清单第十二项 backend_signature_guardrails 不在本树或基线，完整清单执行 exit 4，待补，详见 `TESTS.md`。不改请求组装、预算、Compact，不加配置；真实 DeepSeek、真实 Gateway/TUI/IM 未验证。

## 缓存诊断修正：共享前缀证明边界、v1 基线、发布幂等、跨重启累计、别名去重（cachediag2，2026-10-05，分支 `worker/cache-diag`，基于 `c17b3596b`；待 sol3 复审）

- **来源**：sol3 对 cachediag 的初审判“必须改”（报告 `decision-evidence/deepseek-cache-audit-1004/cachediagr-review-sol3.md`，原始事实 `/private/tmp/claude-501/m-cachediagr/checkpoint-facts.json`），五个问题逐条修，每条配用例；另修窗口滚动定位并给 `change_codes()` 补 LLM 注释。
- **改法（`backends/cache_diagnostics.py`）**：
  - **共享前缀不再虚报**：`_divergence_point` 按检查点自己的 index 对齐比较（窗口滚动会裁掉最前面的检查点，不能按列表位置 zip），共享前缀只报到“第一个不同检查点之前的那个相同检查点”为止；检查点不同只能定位到块，不能声称块内更早的消息相同（64 条改第 1 条从“报 31”改为 0；600 条改第 600 条从“报 599”改为 576）。
  - **v1 基线不再误报**：基线 schema 与本次不同（旧 v1 快照没有链式检查点）时 `comparable=False`，不报 `history_*`，只比较两边都有的组件与整体 options；投影层保留该结构化标志（旧记录缺该键按可比较读）。
  - **别名去重**：`reasoning` 与 `reasoning_effort` 用“键名集合 + 有效档位摘要”签名比较，换写法与真换档位都只报一次（原来报两条 `reasoning_effort_changed`，或落进 `options_changed` 兜底）。
  - **窗口滚动定位**：4096→4097 只追加不再误报成块 32 的改写（现在报 `history_appended`、共享前缀 4096）；4096→4095 报 `history_shortened`（共享前缀 4064，只报到最后一个共同检查点）。
  - `change_codes()` 补 LLM 层注释（说明它是持久化白名单单一来源）。
- **改法（`conversation/model_metrics.py`）**：
  - **发布幂等**：只有“带调用身份的新结算诊断”才累加一次；重发布同一份统计（无新模型调用、pending 状态刷新）不重复计数——原来会把继承的诊断再累加（1 → 2）。
  - **跨重启只增不减**：累计基数取内存显示副本与持久线程记录里同一原因码的较大值；换新 ledger、清空回合状态后从持久记录恢复（原来内存和持久计数都会变空）。结算时找不到调用记录不再把已持久化的最近一次诊断清掉。
- **性能实测**（`request_surface` 全历史链式扫描，本机）：2000 条约 4.7 ms、4000 条约 9.5 ms、10000 条约 23 ms——低于任务给的 50 ms 阈值，**不实现增量化**；用例里留一个 2 秒的宽松守卫只抓数量级退化（防慢 CI 假红）。
- **变异**：六个修复点各一个，全部 KILLED（共享前缀退回虚报、v1 当成可比、发布幂等回退、跨重启丢持久基数、别名比较丢键名、窗口滚动退回位置 zip），产品文件按字节还原。
- **验证**：见 `TESTS.md`“缓存诊断修正（cachediag2）”小节。
- **未验证**：真实 DeepSeek 请求上的端到端读数（要联网与密钥）；`test_cache_prefix_*.py` 在 `worker/cache-sim` 分支、本工作树没有，未跨分支跑（本分支只动诊断与投影，未改那组测试覆盖的接口）。

## 缓存诊断补全：链式摘要去盲区、分区选项单报、累计计数（cachediag，2026-10-04，分支 `worker/cache-diag`，基于 17j 头 `f8858179c`；待初审）

- **来源**：用户点名的 DeepSeek 缓存底座配套件。3a 官网实测（`decision-evidence/deepseek-cache-audit-1004/server_probe_facts.md`）：缓存按“思考开关 + 推理强度”分区，换任何一个整段不命中；两个根因（思考被误关、压缩请求档位不一致）分别由 sol1、luna3 修。产品自带的诊断有三个缺口：① 只散列前 512 条消息，长线程一律 `partial=true`，第 512 条之后被改写看不见；② `options_changed` 把 thinking / reasoning_effort 和别的选项混在一起，看不出换了缓存分区；③ 只保存最近一次比较，历史上断了多少次、因为什么断，事后查不到。
- **改法（只动诊断模块、账本比较点和指标投影；不碰 `openai_chat.py` 与压缩请求组装）**：
  - `backends/cache_diagnostics.py` 换成 **`request_surface.v2`**：每条消息的链值把上一条链值算进去（`_chained_digest`），再按 **32 条一块** 抽稀成检查点，**最多 128 块**，并保证最后一条消息本身也是一个检查点（末链值）。覆盖上限 = 32 × 128 = **4096 条**；超过就只保留尾部窗口，`partial` 为真。比较时先比末链值，不同就用 `_first_divergent_index` 找出第一个链值不同的块，报 `history_prefix_changed` 并带 `changed_block_index` 与 `shared_message_prefix`。
  - `partial` 语义重定义（写进模块注释与 `public_cache_diagnostic`）：**不再表示“截到 512 条”，而是“尾部窗口之外还有消息没被链覆盖”**。旧记录里的 `partial=true` 按同一含义读，不会被当成另一种状态。
  - 选项变化分两类：`thinking` / `reasoning_effort`（含 `reasoning` 别名）各自报 `thinking_changed` / `reasoning_effort_changed`——它们会切换服务端缓存分区；其余选项仍报 `options_changed`，且分区选项已变时不重复报第二条。只看 payload 的结构化键，不解析正文。
  - `conversation/model_metrics.py` 新增 `cache_change_counts`：在 `_publish_metrics` 里按原因码逐次累加（只增不减），随显示副本持久化；白名单与 `cache_diagnostics.change_codes()` 共用一份，避免名单漂移。
- **持久字段安全性**：`model_metrics` 在 `conversation/models.MODEL_HIDDEN_THREAD_FIELDS` 里，不进模型上下文；grep 过 `conversation/` 全目录，**没有任何对 thread 或 model_metrics 做内容摘要/指纹的地方**，新增键不会让旧记录读失败，也不会让已有摘要变样。新键只在有值时出现（无变化时不写空字典），旧记录缺它时按“从未发生”读。
- **顺带修掉两个真缺陷**（都是写用例时实测暴露的）：`change_codes()` 初版漏收 `history_prefix_changed` / `history_shortened` / `history_appended` 三个历史码，会让累计计数静默丢原因；`_add_cache_change_counts(None, …)` 对 `None` 取 `items()` 会抛异常（被 `publish_model_metrics` 的兜底吞掉，表现为整条统计丢失）。
- **验证**：见 `TESTS.md`“缓存诊断补全（cachediag）”小节。
- **未验证**：真实 DeepSeek 请求上的端到端读数（需要联网与密钥，本轮不跑）；sol1 的思考开关修复、luna3 的压缩档位修复在本分支不存在，本件只保证“修复后有没有回退”能从结构化事实看出来。
- **不做**：不改 `openai_chat.py`、不改压缩请求组装、不新增配置项。

## cachecompact2：Gateway压缩分区与active-turn前缀边界（2026-10-05，worker/cache-compact；本地实现与回归/静态门禁通过，真实服务指标未验证）

- **Gateway遗漏复核**：旧 strict xfail 测试使用非生产 purpose=`compact`，且构造的辅助请求没有线程历史、tools；它不能证明 Gateway 前台压缩真的落在 default 分区。测试现改走真实 Gateway ask，首次成功请求建立历史，typed overflow 后由原恢复链执行 Compact，再重发业务请求。真实摘要入口传 `conversation_compact_summary` 与精确 thread_id，主/摘要请求同为 `(deepseek-v4-flash, enabled, max)`，模拟前缀命中达到 90% 门槛；生产线程档位转接无缺陷，已去掉 xfail。单次变异移除该 purpose 识别后，分区断言按 default/max 不一致变红。
- **active-turn边界**（2026-10-05 起由 compactcache 取代：有缓存分叉时复用主请求前缀，下述只在结构化条件不满足时成立）：该摘要只合并上一代 thread summary 与当前选中的工具 IR，不携带完整主线程 provider history；因此明确不承诺主请求缓存前缀。为此请求显式发送空工具目录与 `ToolChoice.none("active_turn_summary_no_prefix")`；bounded 路径只对非空工具目录强制 `auto`，空目录的 none 保持至出站，避免无谓工具选择与额外重试。真实 HTTP 假传输测试检查出站字段、一次请求完成；将 active-turn 改回带 surface.tools 的单次变异被 none 断言抓红。
- **区分其他路径**：完整缓存面可复用的单次 transcript/tool-loop 摘要仍保留 system/tools/history 与 `auto`；结构化工具调用不执行，走已有的一次无工具/`none`重试。超窗分段、active-turn 窄视图和 `compact_carried_summary` 均不宣称与主请求共享完整前缀。
- **证据边界**：当前只有本地 Gateway/假传输及缓存模拟器证据；真实 DeepSeek 缓存命中率、服务端计费和实际节省未验证，不以旧估算冒充实测。完整测试与门禁结果见 `TESTS.md` 本节后续记录。
## B7 第四段：老格式插件启动复核与 restricted 经统一底座施加（opp6，2026-10-05，WIP，待沙箱外复跑与终审）

- **实现**：`plugin_permissions/sandbox.py` 重写为 `legacy_launch_policy(owner, installation)`：每次启动读固定 `permission_json`，重算插件/包/激活身份、激活计划（canonical JSON 全等）、受限根 inode 与程序内容（`permission_paths` 重读）、解释器事实（`verified_runtime_command`，stat 优先）、模式与网络；变化抛结构化 `LegacyLaunchDenied`（`legacy_permission_changed`/`legacy_permission_missing`/`legacy_permission_invalid`）。`installation_ref` 随提交漂移，只用于确认码绑定，不作复核判据（实测坑：准备/发布后 ref 变化，最初把它当判据导致全部复核误拒）。
- **四入口接线**：`PluginMCPClient.__init__`（候选 `_candidate` 与业务/面板构造共用）复核并投影 `PluginRestrictedSandbox`；覆盖 `start()` 在真正拉起进程前再复核一次（业务 registry、面板连接池、`reconnect()` 都走同一入口）。restricted 强制进统一底座（与 v8 同 `plugin_sandbox_spec` + `sandboxed_plugin_argv`），`_require_legacy_policy_ready` 与启用预检 `_legacy_sandbox_problem` 两处对沙箱不可用（含 Linux `network:true` 端口隔离缺口）结构化拒绝；wide/`legacy_compat` 只复核身份、不进受限沙箱。
- **清理**：`_retire_before_enable` 移除 `legacy_sandbox_pending` 拒绝（restricted 正常走准备→候选→发布）；`retirement_committed` 与 `_LegacyRetireFacts` 未用字段随删；`sandbox_status` 由 `pending_b7` 改为 `platform_sandbox`；确认文案改为"收紧模式由统一 B7 沙箱底座在每次启动时施加"。
- **验收**：新 `test_plugin_legacy_launch_guard.py` 7 用例（四入口"事实变化 → 零启动"、撤销拒绝、restricted argv/spec 合同、wide 显式模式）；`test_plugin_legacy_management/transitions/drift` 改断言（restricted 经沙箱启动、假 report 在有旧代时 `revision_conflict`）；新增预检强制沙箱用例。5 个单点变异全 KILLED（构造不复核、start 不复核、绕过 B7、变化不拒绝、预检不强制）。既有沙箱失败与改动前逐条一致（13+3）。
- **初审补强（opp6-idtest，2026-10-05）**：补 `test_construct_rejects_tampered_fixed_identity`（固定授权身份与安装不符 → 构造拒绝、零启动）；初审 M2 变异（`_verify_identity` 身份比对恒假）由存活变 KILLED。
- **legacyid 修复（2026-10-05）**：`_verify_identity` 对 legacy_compat 只比对记录里实际存在的 `package_sha256`/`activation_id`（v3 迁移六字段集合不含 `plugin_id`）；修复前 legacy_compat 记录在身份复核恒被拒，违背 4.1“冻结策略可启动”口径。3 个变异 KILLED；48 文件连带测试与基线 `d46515d2c` 失败名单逐条一致（无新增）。
- **未验证**：真实 macOS/Linux 沙箱子进程（沙箱内不可嵌套，3a 沙箱外复跑）；真实 TUI/飞书/桌面链；生产回滚；本段非作者/9b 终审。整系列仍不可单独部署。

## opp 变基到 17k（opp6 第一步，2026-10-05，WIP，B7 第四段待接，整系列不可挑入）

- **变基**：`worker/opp5` 6 个提交逐个重放到 17k 头 `02acd6446`（重放头 `96a565ace`→`75d33ea2e`→`a065ea50f`→`b874a17fb`→`36279bdca`→`65ebf6317`）。合并方向与 17j 版（opp5a2 段）相同；唯一代码冲突是 `USER_SETTINGS_BOUNDARY_KEYS` 合并为单 frozenset（`plugin_tool_gate_timeout_ms` + `plugin_legacy_sandbox_default` 两键都在），DESIGN_LEDGER / TESTS 顶部插入两段都保留。
- **对照**：opp 17 文件与 B7 集合在变基前（`758eac616`，临时工作树 `/private/tmp/claude-501/opp6-base`）与变基后失败节点逐条一致（13 + 3 条既有沙箱失败）；17k 新增 2 条用例（1 通过、1 沙箱内 skip）。命令、XML 与精确集合见 `TESTS.md` 同名节。
- **状态**：B7 第四段（四种启动入口重核授权事实 + restricted 经 B7 施加）未接；真实链、OS 隔离与生产回滚仍未验证；整系列仍不可单独部署。

## TUI 本地会话登记恢复（sessrec，2026-10-05；待非作者初审）

- **调查**：单会话目录的直接删除入口是 `SessionManager.delete_session`（`agent/session/manager.py`），全仓未发现生产调用方；未找到 TUI 退出、TTL 或会话专用维护回收。`OwnerObjectStore.restore` 会先递归删除整个 `owner_home` 再按清单恢复，清单缺少的会话文件可能随之消失；仅凭当前源码不能认定它就是这次丢失的实际原因。新会话由 CLI chat 创建，显式 resume 在本地登记缺失时原本 fail-closed。
- **选择与边界**：采用 resume 时恢复登记，而非给无法证明被调用的删除 API 增加 Gateway 活动探测。仅使用 `ConversationStore` 的 `chat/local-agent + session_id` 精确绑定，且 `owner_id` 与解析后的 `owner_home` 都须与当前 owner 一致；先写 `SESSION_REGISTRY_RESTORE_AUTHORIZED` 审计，再重建相同 ID 的本地登记。审计关闭、thread 不存在/损坏或任一归属不匹配都拒绝；不从请求正文、ID 名称或其他用户记录推断。
- **验证与状态**：tmp_path 隔离 home 下删除登记、保留同 owner thread 后恢复成功并核对无正文审计；owner_id 与 owner_home 两个负例及对应单点变异均验证。详细测试与本地门禁见 `TESTS.md`；状态为待非作者初审，未读取真实 owner home，也未推断这次历史丢失的具体触发入口。

## Adapter 外发意图恢复（slp5，2026-10-05；已实现，待初审，基线 `e180f4185`）

- 来源：睡眠恢复设计第 3.5 节。原 pending/sent schema 7 增 dispatch-started，外发前记录消息序号、稳定键、正文摘要、进程身份和 epoch，不另建存储。
- 旧 owner 活着或不可核验时不接管；明确死亡后先查询或在渠道去重窗口内同键恢复，否则收现有 unknown 停止自动重发。progress/final 分身份，迟到 epoch 不覆盖新态；进度游标偶发写失败只重试同 epoch 本地 CAS。
- 飞书现有 create/reply 已接 uuid，本片补一小时去重能力声明，不承诺无限期幂等。QQ 继承 Base 默认无幂等/查询声明，不复制第二份能力事实。
- 续做已由脚本刷新常数投影，35 文件相关回归 **510 passed**（1 条 Starlette 弃用警告）；十二守卫 **190 passed**，三单点变异 **3/3 KILLED**，逐次备份/还原字节一致。全 Ruff、imports、doc-sync、strict size、clean-package、diff 已执行；线上告警身份差集新增 **0**、消失 **55**，生成的 CODE_SIZE_REPORT 已还原。严格扫描仍含存量告警，不等于全仓无尺寸债务。
- `6777f8c95` 是前轮 WIP 检查点；本轮仅收本切片离线证据，待独立初审、3a 终审。未连接真实渠道/Gateway，真实睡眠与真实送达仍未验证；详见 TESTS。

## WebSocket 流在信封绝对期限处收口（wsdeadline，2026-10-05，分支 `worker/wsdeadline`，基于 17k 头 `07d7b3306`；待初审）

- **来源**：fix3ar 初审实测（探针 B3）：`_events` 的期限检查只在 `recv` 超时分支执行，且 idle 阶段每次事件把截止刷新为 `now + request.timeout`——服务端持续滴事件时信封绝对期限（`request.deadline`）永远不生效，与 HTTP 路径 `_StreamIdleWatchdog` 的不可续期 hard_deadline 不一致。3a 裁定随 17k 修。
- **修法**（`responses_websocket._events`，两处配合）：
  1. idle 阶段的截止取 `min(now + request.timeout, request.deadline)`（无 deadline 时行为不变）；
  2. 每条已收消息也检查一次当前阶段截止——不能只靠 recv 超时分支：持续滴事件时 recv 每次都成功，检查会被跳过（假连接实测只有 ① 时仍不收口，这是 ① 单独不够的原因）。
- **收口口径**：到点抛 `ProviderTimeoutError`，stage 与 HTTP 路径一致（`_StreamIdleWatchdog.timeout_stage`：首包阶段 `first_event`、见过数据后 `stream_idle`）；异常沿原 `iter_responses_websocket` finally 走 `close_socket()`，连接不泄漏、半截事件不当完成。
- **用例**：`test_websocket_stream_stops_at_envelope_deadline_despite_live_events`（滴流 + 0.5s 期限：stream_idle、<2s 收口、连接已关、滴数受限）；`test_websocket_stream_without_deadline_keeps_live_events`（无期限对照，主模型长流不受影响）。
- **变异 4 个（全部抓住）**：idle 截止不取 min；到期不关连接；idle 阶段 stage 写错；去掉逐消息检查。
- **未验证**：真实 websockets 服务端与真实 ChatGPT 订阅链路（需真实账号）。

## 逻辑回合身份覆盖 native 出站内容（retrycount-fix，2026-10-05，分支 `worker/retrycount`，基于 `eb8680753`；待复审）

- **起因**：luna5 初审 retrycount 发现必须改——身份种子只有渲染 prompt 指纹，而 native 模式下工具内容与修复注入是单独投影成 provider messages 的（`tool_ir_history.project_native_provider_messages`）。修复路径把纠正内容追加到 `tool_context` 后，provider messages 变了、渲染 prompt 与 `tool_rounds` 不变 → 身份不变：重跑被误并进旧回合，超时"至多一次重试"的门槛也按旧身份计数、误拒新回合该有的重试（去掉 input_tokens 之前这类内容变化会顺带改身份，洞没暴露）。
- **改法**：新增 `tool_ir_history.provider_messages_fingerprint(params)`——对"此刻会出站的 native messages"做规范化指纹（与真实出站同源的纯投影 `project_native_provider_messages`；只读：不消费 seen、不写 IR；判定或投影失败返回空串，绝不中断模型主链）；`call_runtime.logical_model_call_id` 的种子新增该项（text 模式为空串，保持 prompt 指纹语义）。同一次请求的断流重试在指引迁移（tool_context → IR）前后指纹一致，身份稳定；内容真变化（修复注入、压缩、插话）按新回合计。
- **验证**：见 TESTS「逻辑回合身份覆盖 native 出站内容（retrycount-fix）」；变异 4 个：种子不纳入指纹 / 忽略 guidance / 忽略 IR → KILLED；不做 seen 去重 → SURVIVED（等价变异：record 层按同 source+text 去重兜底，真实重试路径不出现部分转发状态）。
- **已知边界**：指纹覆盖 provider messages 与渲染 prompt 两类出站内容；`_native_provider_messages` 的其它行为差异（如孤儿清扫细节）不单独入种子。luna5 的模块文档 `de32e4604` 已摘入（`72736e123`）。
- **未验证**：真实 provider 端到端（沙箱不连真实模型）。

## 逻辑回合身份去 input_tokens（retrycount，2026-10-05，分支 `worker/retrycount`，基于 17k 头 `100df7ad7`；待复审）

- **起因**：streamretryr 初审发现的 `model_retry_count` 少算——断流响应带 usage 时，第一次响应的 usage 会校准第二次的 `input_tokens` 估算，而 `logical_model_call_id` 的种子含 `input_tokens`，同一回合的重试被拆成两个逻辑回合（实测 `logical=2 physical=2 retry=0`，应为 1）。
- **改法（唯一权威位置）**：`agent_core/model/call_runtime.py::logical_model_call_id` 的种子去掉 `input_tokens`，只保留重试间稳定的事实：`request_id | run_id | task_id | tool_rounds | prompt 指纹`。重试（响应级/异常）重建的 prompt 逐字节稳定（工作区块快照与执行事实在 run 内冻结、插话未确认投递被 retry_guard 挡住），所以重试与首次尝试得到同一身份；内容真变化（压缩重试、修复注入）按新回合计。聚合端 `contracts/model_call_ledger._logical_call_id` 不改：按 `metadata.logical_call_id` 字符串去重，新旧记录一视同仁；旧持久化记录保持原值。
- **验证**：见 TESTS「逻辑回合身份去 input_tokens（retrycount）」；4 个变异 KILLED（身份加回 input_tokens / 种子去掉 tool_rounds / 物理号不递增 / 种子去掉 prompt 指纹）。
- **已知边界**：重试期间若 prompt 因运行时状态刷新（如子代理状态变化）而变，仍按新回合计——语义上属于"请求内容真变化"；部署切换瞬间同一回合跨新旧算法的记录不会归并（部署要求会话空闲，实际不出现）。
- **修订**：retrycount-fix（2026-10-05，见上段）把 native 出站 messages 指纹纳入种子——本节"只保留 prompt 指纹"的表述被其上修订取代，其余结论不变。
- **未验证**：真实 DeepSeek/Anthropic 断流端到端（沙箱不连真实 provider）；跨版本旧记录的实机聚合数据。

## 模型流未完整结束的回合内重试（streamretry，2026-10-05，分支 `worker/streamretry`，基于 17k 头 `768c73272`；待初审）

- **起因**：10-05 凌晨 DeepSeek 长回复流频繁被中途切断（近一小时三成请求失败，码 `MODEL_STREAM_INCOMPLETE`），每次切断整个回合直接失败、会话从头再来（费时费钱）。链路核查：流在完成标记前结束 → `stream_parsers` 返回 `stream_eof` → `response_completion.incomplete_response_fields` 归一成**响应字段**（`runtime_status=error`、`runtime_reason=MODEL_STREAM_INCOMPLETE`、`truncated=True`；工具块被丢弃、`truncated_tool_names` 保留）——它不是异常，所以只重试异常的 `run_with_provider_transient_auto_resume` 没有机会介入；工具循环 `_no_tool_calls_decision` 见 `_is_runtime_status_response` 直接 break，回合以 error 收口。
- **改法（复用既有设施，不新造）**：`run_with_provider_transient_auto_resume` 增加**响应级重试**——新增可选 `callbacks.should_retry_result`，operation 正常返回但结果被判为"可原样重发"时，走与异常重试**完全相同**的延迟阶梯（10/25/45/100/180 秒 + 抖动）、总预算（1800 秒）、重试通知与中断检查点；**耗尽时返回结果本身（不抛异常）**，保持上层既有失败语义（异常路径仍原样上抛）。两个重试回调收成 `ProviderTransientRetryCallbacks` 载体，主入口保持 4 参数（code-size 参数守卫）。
- **判定（`model_turn._stream_incomplete_retryable`）**：只读结构化字段——`runtime_reason == MODEL_STREAM_INCOMPLETE` 且 `runtime_source == model_provider`，且 **text 为空、`truncated_tool_names` 为空、`tool_use_blocks` 为空**。已输出文本或已开始工具调用的不重试（避免重复内容与重放副作用）；判据不解析正文。
- **记账与可见进度**：每次重试都经 `generate_model_response → _start_model_generation` 新建物理尝试记录（同一 logical turn），账本 `model_retry_count` 自动 +1；重试通知走既有 `write_provider_retry` typed sink / 文本回调（"模型接口临时不可用或被限流，等待 X 秒后自动重试当前模型回合"）。
- **验证**：见 TESTS「模型流未完整结束的回合内重试」；变异 2/2 KILLED（去掉响应级重试 → 4 条用例红；去掉"工具已开始不重试" → tool-trace 用例红）。
- **未验证**：真实 DeepSeek 流被切断的端到端（沙箱不连真实 provider）；本树用假后端走完整模型回合 + 真实账本验证。真机复测建议在 DeepSeek 高峰时段观察"流被切断后同回合自动重试、账本出现 retry 计数"。

## stale-waiting 睡眠恢复（slp4b，2026-10-05，worker/slp4b；已实现，待审）

- **范围**：从指定基线 `875eb7b74` 接手；产品实现仅限 `agent/scheduler/active_run_closeout.py` 与聚焦测试，另外按守卫重生成派生常数目录，并同步 `DESIGN_LEDGER.md`、`TESTS.md`、`SLEEP_RESUME.md` 与 gateway 模块文档。不改 scheduler repository/service、conversation 或 tooling 的实现。
- **实现**：stale-waiting 收口前按准确 TaskLink 读取 `cancellation_scope` 并选择共享/独立 ClaimStore lane；先核验 claim 的 thread、scope、task、claim_id，再解释 status。只有精确匹配的 running claim 且 `process_identity_is_live(...) is False` 才能作为执行者已死亡的证据；live、unverifiable、读错或身份字段不全一律继续 waiting。合法 foreground 默认来自 ThreadTaskLink 结构化模型，detached 只使用精确 `detached_task_claim_scope_id`。
- **暂停处理**：同进程墙钟相对单调钟多走超过 60 秒只清空缺失确认，不代表执行者死亡；样本更新与重置由同一把锁保护。冷启动没有旧样本时不推断暂停，仍查 claim。
- **保留合同**：follow-up 每次重读、两次缺失确认及至少 60 秒间隔、unreadable 六倍宽限、blocked 与 waiting-run finish CAS 均保持。
- **验证状态**：stale-waiting 相关子集 39 passed；12 个相邻 scheduler 文件 142 passed，另 5 个 claim/endtask/store 文件 117 passed；guards9 190 passed。全量范围内的 process-completion fixture 错误已在 `875eb7b74` 复现；Ruff、import-boundary、doc-sync、strict code-size、clean-package 与 size_diff 均有本地证据，详见 TESTS。本节只表示本地实现待审，不表示生产部署或真实 Gateway 验收。

## stale-waiting 睡眠恢复初始测试准备（sol3，2026-10-05，worker/slp4；历史检查点）

- **当前范围**：基线 `875eb7b74`，承接 [SLEEP_RESUME.md 第 3.4 节](docs/design/SLEEP_RESUME.md#34-风险四长暂停让-stale-waiting-收口过早)。只允许修改 `scheduler/active_run_closeout.py` 和聚焦测试，后续进 17l，不进 17k；不改并行的 conversation claim、scheduler repository/service 或 tooling。
- **已核对**：waiting 会清 scheduler `claim_id/claim_expires_at`，遗留 `runner_pid` 不证明当前尝试仍在执行。已有 `ClaimStore.load_report` 能区分空记录和坏账；共享车道与 `detached_task_claim_scope_id` 定位的独立任务车道需分别按精确 task/thread、claim_id、status、owner_process 核对。`conversation_task_execution_state` 受 TTL 和进度策略影响，不能直接当死亡证明。
- **仅完成测试准备**：既有显式 `now` 的时间推进用例配同步单调钟，隔离缺失确认计数和拟用的进程内样本表；原断言未删改。短检查 14 passed / 72 deselected，详见 TESTS。
- **未实施**：存活/不可核验保护、暂停后确认重置、冷启动新用例、至少三个变异及完整门禁。产品文件未改，设计 3.4 仍未标为“已实现”；不把本检查点当修复或集成交付。按 3a 的 17k 部署窗口通知暂停，部署后同代号、原工作树续做。

## G2b 客户端收尾小修（g2bfix4，2026-10-05，分支 `worker/g2bfix4`，基于 g2bfix3 头 `51b3efb20`；待终审）

- **背景**：g2bfix3r 初审给 g2bfix3 判"小问题，可以交终审"：① 解码层 `auth_denied` 置位无直接断言（TUI 侧用替身构造，两层接缝没有端到端钉住）；② 投递线程兜底会每秒一条 warning 刷屏，且 `except Exception` 会吞 `InterruptedError`/`BlockingIOError`（项目里 `InterruptedError` 是真实中断信号）；③ `/progress` 对不存在记录回无码 403，与 `/result`、`/input-status` 的"不存在回 404"不一致。3a 采纳服务端做法 A（不存在的记录回 404、无权限保持 403、客户端不改），由初审者直接修。
- **② 投递兜底加固**（`agent/adapter/delivery.py`）：`_run` 增加 `except (InterruptedError, BlockingIOError): raise` 显式放行中断类；`except Exception` 保持只捕 Exception（KeyboardInterrupt/SystemExit 等 BaseException 穿过兜底，补护栏用例）；新增 `_log_loop_error` 按 60 秒窗口（`_LOOP_ERROR_LOG_INTERVAL_SECONDS`）对同一异常限频——窗口内只累计次数、下次真正记录时先输出被抑制条数。
- **① 解码层置位断言**（测试侧）：新增 `test_active_turn_input_result_sets_auth_denied_only_for_typed_denials` 直接调用 `_active_turn_input_result` 覆盖五格（401/403/带码 404 置位；无码 404、普通 202 不置位），两条既有端到端用例补 `auth_denied` 断言。
- **③ /progress 三态**（`agent/gateway_parts/http_handlers.py`）：`_can_read_finished_request` 由 bool 改为三态枚举 `_FinishedRequestAccess`（ALLOWED / NOT_FOUND / DENIED），`handle_progress` 未找到回 404、无权限保持 403，两者都经 `_denial_body` 补缺凭据码；`_all_user_access`（管理员）分支不变。与 `/result`、`/input-status` 的"记录不存在 404 / 别人 403"口径统一。
- **验证**：见 TESTS「G2b 客户端收尾小修（g2bfix4）」；6 个变异全杀（去限频、去中断放行、吞 BaseException、不置位 auth_denied、未找到当允许、未找到回 403）；相关 7 文件 307 passed / 1 skipped、guards9 187 passed，静态门禁全过，size_diff 新增 0。
- **未验证**：真实 TUI/飞书端到端与真实 Gateway 仍由 3a 在沙箱外复核；本树覆盖解码层、投递循环与 `/progress` HTTP 层。

## G2b 客户端线收 9b 三条建议（g2bfix3，2026-10-04，分支 `worker/g2bfix3`，基于 17j 头 `45d8cdcc5`；待初审）

- **TUI 鉴权分流按结构化标志**（9b 建议 2）：`ActiveTurnInputResult` 新增 `auth_denied`（由状态码决定：401/403、以及带码 404），对账器按它分流到 `on_auth_rejected`，不再按 `reason_code` 是否为空。修前不带码的 401/403 会错走 `on_rejected`——提示"当前回合结束，已排到下一轮"并把同一条消息**重发一次**，`on_auth_rejected` 的通用文案分支永远走不到。远程 TUI 带错 token 会碰到。
- **投递线程加兜底**（9b 建议 3）：`GatewayReplyDeliveryWorker._run` 单轮意外异常（如持续落盘失败时 `release_claim` 抛错）原来会让线程静默退出——线程一死之后所有通道回复都停了且无信号。现在记 warning 后按轮询间隔退避继续，停止请求仍生效。
- **`/progress` 无码 403**（9b 建议 4，**只核清未实现**）：服务端 `handle_progress` 先过 `_can_read_finished_request`，请求记录（processing/inbox/terminal/done/failed）**不存在时直接返回 False → 403 且不带码**（`_denial_body` 只在"强制档+回环+没带凭据"那一格加码）。客户端按"401/403 一律 auth"处理，会给用户发"鉴权失败，请重启"。只在请求记录消失后还在轮询时出现（以前是静默隔离）。两种做法见交接报告，推荐服务端改 404；**等 3a 定，本次不实现**。
- **验证**：见 TESTS「G2b 客户端线收 9b 建议」；变异 2/2 被杀（TUI 退回按 reason_code 分流、投递循环去掉兜底）；7 项静态门禁全过，size_diff 拆平后新增 0（测试 helper 参数收成 `_AuthRejection` 数据类 + 模块级单例）。


## 后端传输签名守卫（sigguard，2026-10-05，分支 `worker/sigguard`，基于 17k 头 `75a26ca37`；待初审）

- **起因**：cabfix 把 `HttpBackend._gateway_request`、`request_stream_iter` 改成 `(StreamCall, options)` 后，OAuth 两个覆盖与 Responses 流式调用点没跟上，账号登录与 Responses 请求直接 TypeError；Mac 测试因替身 `**kwargs` 遮住签名错位全过，最后靠 Linux 车道抓到（修复 `75a26ca37`）。本件只加防复发守卫，不改产品代码。
- **守卫一（覆盖签名）**：按类关系发现 `backends` 包全部 `HttpBackend` 子类（不写死类名；含 mixin 祖先，如 `_OAuthMixin`），四个传输方法（`_gateway_request`/`request_json`/`request_stream`/`request_stream_iter`）的覆盖必须与基类同参数名/同种类（位置 vs 关键字专用）/同默认值有无；失败信息带类名、方法名、逐位差异。防"守卫空转"下界：至少检查到 2 个覆盖。
- **守卫二（调用点）**：ast 扫描产品代码（`agent_py_agent` 包，tests/ 除外）的 `self.xxx(...)`/`super().xxx(...)` 直接调用，关键字必须 ∈ 基类签名；`request_stream_lines` 按签名分派不算；`**kwargs` 展开跳过。
- **验证**：见 TESTS「后端传输签名守卫」；变异 4/4 KILLED（两个 75a26ca37 回退各打红一个守卫 + 两个新变异）。
- **建议**：把 `test_backend_signature_guardrails.py` 加进 guards9 清单（跨文件签名合同，收尾必跑）。

## 睡眠与长暂停后的执行恢复（sleepdesign，2026-10-05，分支 `worker/sleepdesign`；状态：设计，待 3a 定）

- **问题**：墙钟 lease 到期可能早于执行者或具体 attempt 结束；外部副作用与本地回执之间存在不确定窗口，睡眠后只凭 TTL 恢复可能重复执行、重复领取或过早终止。本文不把八个失败回合归因于睡眠。
- **设计**：风险前五、结构化执行者/attempt/epoch 与副作用回执、时钟边界、故障注入矩阵及首两项的可派工切片见 [SLEEP_RESUME.md](docs/design/SLEEP_RESUME.md)。证据按指定 slp2 报告，并以 step17k 的已提交 HEAD `43b320bcd` 只读复核；本轮未改产品代码。

## 选包入口独立计费，消除投递顺序偏置（selfix3，2026-10-05，worker/selfix3；本地实现及已执行门禁通过，2项沙箱skip，待3a终审）

- **来源**：selfix2r 初审用两个异质入口、978 总预算及独立 home 证明，原均分后仍用加入前后的整体 token 差裁剪，受前页编码和上取整影响；三包也能出现交付字符数差异。下节保留 selfix2 当时记录，其中“换序结果相同”的承诺由本节修正，不能当作旧版本已验证结论。
- **通用改法及上界**：扣公共头后剩余预算整除为每包份额，余数、失败包份额不转借，份额可为零。每页只紧凑编码自己的完整回执、正文和 continuation，加一个逗号后独立估算，不读取前页或剩余预算；最终文本使用相同紧凑编码并仅按结构化 package_id 排列可见正文，诊断保持输入顺序。紧凑编码只去掉 JSON 排版空格，不改变原正文/字段。原字符串估算按 UTF8 字节/3 上取整，分项上取整之和不小于拼接整体；公共头已含空数组，实际n页仅需n-1个逗号而分项预留n个，因此公共头预算加各份额覆盖最终文本，无需按顺序二次删页。
- **三分法不变**：准入不过不 pin、无有效页不 pin、读成功但放不下仍 pin。唯一 pin 在原 reader 校验后、返回有效页之前的任务锁内完成；caller 不补 pin。读成功后裁剪内部取消保留已固定版本，交付前复查上抛取消，不交付正文。
- **诊断与核验边界**：entry_status 只记录投递形态；当前主/子调用方仅消费 text/warning_codes，not_delivered 不能表示是否已固定，更不表示模型采用。宿主匹配仍在每包自己的 deliverables/verifiers 内完成；合法重叠声明可以双匹配，不能无条件承诺某包永不检查另一任务产物。
- **验证与后续**：同归档字节分别安装到独立 home 的两包978正反序、三包1800六排列，比较完整正文、来源和续页（仅排除重新安装的 activation）；21显式文件（含完整11文件guards9及packaging）491 passed、2 skipped。退回整体差计费被三包用例抓住，偏向先到的份额被两/三包用例抓住；每次字节恢复、33项入口回归通过。静态门禁和尺寸新增0详见 TESTS 本节。未运行真实 Gateway、模型、TUI/IM 或沙箱外检查器；本节不宣称部署、方法采用或全仓通过，不改默认预算/权限/原 pin 账。

## 选包入口预算均分 + 选中即固定（selfix/selfix2，2026-10-04，分支 `worker/selection-pins`，基于 17j 头 `1e1aadb80`；待非作者初审）

- **来源（故障）**：能力包 B 0.3.3 冻结重跑里 `B05-t501/t502` 两次都按 A 包交付。selb05 只读调查定责为**宿主选包底座**，不是模型也不是 B 包描述：`prepare_package_entry_context`（`package_selection_context.py`）**按 `references` 顺序逐个读入口、边读边扣预算**，`remaining <= 0` 就 `break`。`capability_bundle_max_tokens` 默认 **3000**，而单个入口文档（`CAPABILITY.md`）实测 A 包 3479 token、B 0.3.3 3504 token——**任一单包都超总预算**，所以 `selected_count=2` 时必然“第一个活、第二个被 `CAPABILITY_SELECTION_ENTRY_BUDGET_EXHAUSTED` 丢掉”；谁排第一取决于模型返回的 `selected_ids` 顺序。被丢掉的包不会被 pin（pin 当时只在 `read_package_page` 成功交付后发生），于是 `selected_count=2` 而 `pins` 只有 1 个。证据 `capability-packs-v2-b8/rerun-b033-166e8c372/selb05-ds1.md`。
- **改法（3a 裁定，不改默认预算）**：
  1. **pin 与“读成功”绑定、与“投递多少正文”解耦**（三分法）：① 准入不通过（ask/deny/越权）→ 不 pin；② 准入通过但读不出有效页（撤销、激活代次失效、取消、读失败）→ 不 pin，取消照常上抛；③ 准入通过且读出有效页、只是总预算放不下正文 → **要 pin**。实现上不再有“为预算补 pin”的独立调用：`read_package_page` 在页通过校验后 pin 的动作本身就落在“读成功之后、裁剪之前”，正是判据③要求的位置。裁剪期间可能已被取消，交付前再 `authority.check()` 一次，取消照常上抛、这一页不投递。
  2. **预算在选中包之间均分**，不再先到先得：`_entry_share(max_tokens, count) = (max_tokens - 包头) // count`，只取决于包数与总预算，**与 refs / 模型返回顺序无关**；每个包只用自己的份额裁剪（`_fit_entry_page(..., allowance=min(max(剩余,0), share))`）。同一组选中包换个顺序得到同样的投递形态。
  3. **告警码语义按新规则写清**：`CAPABILITY_SELECTION_ENTRY_PARTIAL` = 这一包读到了但正文只交付一段（`has_more`）；`CAPABILITY_SELECTION_ENTRY_BUDGET_EXHAUSTED` = 准入通过、读成功后，这一包的份额放不下最小骨架，正文一律不投递（`entry_status.status = not_delivered`，引用仍已固定）。两者都不再表示“引用丢没丢”。
  4. **回执加结构化 `entry_status`**：每个选中包一行 `{package_id, status: full|partial|not_delivered, reason?, has_more?, delivered_chars?, total_chars?, continuation?}`，投递形态不再靠告警码拼。
- **改动文件**：`agent_py_agent/agent/capability/package_selection_context.py`（核心；删掉死代码 `_pin_selected_reference`，原有 `_budget_plan`/`_skeleton_cost` 收敛成 `_entry_share`）。`subagent_package_entries.py` **不需要改代码**：它 `:107` 用的是同一个 `capability_bundle_max_tokens`、`:111` 调的是同一个 `prepare_package_entry_context`，行为自动一致（见“核对结论”）。
- **核对结论（selfix 第 4 点两问）**：
  1. **宿主核验不会串到别的包**。`pack_verification_scope.py:48 pinned_verification_packages()` 逐条取本任务 pins，按 `activation_id` 从安装表取原安装项并要求 `entry.package_sha256 == ref["content_sha256"]`，只把有核验声明的包列进来；真正决定“哪个检查器跑在哪个产物上”的是 `pack_verification_service.py:269 _applicable()`——`relpath` 先按**该包自己的 `deliverables` 路径模式**匹配，再取 `verifier.applies_to in ids` 的检查器。A 包被固定不会让 B 任务的产物去跑 A 的检查器。
  2. **子授权入口是同一个问题、同一份实现**，所以一起改（本次未单独验证子入口的独立行为差异，只验证它随共享函数变化——新增用例见 TESTS）。
- **旧合同用例改写**：`test_multiple_packages_cannot_pin_a_later_entry_omitted_by_total_budget`、`test_budget_too_small_for_source_receipt_does_not_pin_an_invisible_package` 两条旧用例钉的是“预算不足 → 不读不 pin”旧语义，与新判据③冲突，已删除并由 `test_readable_but_unaffordable_entry_is_pinned_and_marked_not_delivered`、`test_zero_budget_still_reads_and_pins_selected_package`、`test_access_denied_selected_package_is_never_pinned`、`test_unreadable_selected_package_is_never_pinned` 四条新用例取代（理由见 TESTS.md 同名小节）。
- **验证**：见 `TESTS.md`“选包入口预算均分 + 选中即固定（selfix2）”小节。
- **未验证**：真实 Gateway 里两包同轮选中的端到端没跑（不启 Gateway 是工作规则）；窗口实现细节（`_entry_share` 的均分与余数）只由单测钉住；`test_pack_verification*.py` 只跑相关子集，未跑该目录全部；子入口未做独立行为差异实测。

## 前缀运行器命令值的删除拦截修复（shellwrap5，2026-10-05，本地已实施，待 ds2 初审）

- **解决问题**：`env --` 后的 `NAME=VALUE`、`env -S/--split-string` 与 `flock -c/--command` 命令字符串曾被解析器当作普通参数跳过，导致内层删除命令未进入统一策略检查；表外 `--option=value` 和嵌套 argv 的 unknown option 也曾漏报。
- **修法**：运行器规格使用结构化 `command_options` 标记命令字符串值，按现有 shell 解析和 nested depth=4 复用同一危险检查；`--` 只结束选项扫描，不结束规格声明的赋值扫描；表外等号选项及嵌套未知选项统一 fail-closed。caffeinate 补齐 `-m` 已知开关。
- **状态与验证**：实现与测试已提交本地分支，等待 ds2 初审；定向测试、变异、guards9 和本地严格门禁结果见 TESTS.md 同名节。本地证据不代替 3a 集成或真实端到端验收。

## 包一层 shell 的删除命令绕过修复（shellwrap + shellwrap2 + shellwrap3 + shellwrap4，2026-10-04/05，分支 `worker/shellwrap` → `worker/shellwrap2`，基于 17j 头 `6e31e5855`；初审 shellwrapr 两处真漏已修，终审又发现 xargs 选项解析真漏（shellwrap3）和前缀运行器缺口（shellwrap4）；待 3a 终审）

- **问题（9b 证据 review-rm-wrap-gap）**：裸 rm/rmdir/unlink 被 `command_policy` 硬拒，但 `sh -c "rm -rf …"`、`bash -c`、`env sh -c`、`cd … && sh -c`、`eval`、`xargs rm`、`find … -delete` 都判 unknown/read_only、没有 finding；ActionPolicy 对 unknown 直接放行，Full Access 下会真执行。
- **修法（9b 定方向，唯一权威入口 contracts/gates/command_policy.py）**：
  - 新增 `contracts/gates/shell_source.py`：`-c` 字面程序提取与 heredoc 剔除下沉为唯一提取器；`tooling/shell_syntax.py` 改为导入（依赖方向 tooling → contracts 不变）。
  - `evaluate_command_policy` 递归拆包：sh/bash/zsh/dash/ksh 的 `-c` 程序、`eval` 参数、`xargs`（跳过 -n/-I/-L/-P/-s/-d/-E/-a/-J 及值、长选项 `--opt=value`/`--opt value` 两种写法都消耗值）、`find -exec/-execdir/-ok/-okdir`（到 `+`/`;`/段尾）按嵌套 argv 重跑危险模式与受管删除检查；`find -delete` 按受管删除拦；finding evidence 带 `nested_depth`。
  - 深度上限 4（超限 `COMMAND_NESTED_DEPTH_EXCEEDED`）、单条嵌套源 32k 字符上限（超限 `COMMAND_NESTED_SOURCE_TOO_LARGE`），都 fail-closed 不放行；两个新 code 已注册进 `error_taxonomy`。
  - 分类取更严：find 带 -delete/-exec*/-ok*/-fprint*/-fls 不再算只读（mutating）；外层包装保持 unknown 不因嵌套只读降级（`bash -lc "ls"` 仍 unknown）。
- **覆盖边界（9b 定）**：这条前置拒绝是"常见写法的结构化拦截 + 引导到可恢复删除"（apply_patch / task_trash），不是安全边界；python -c、perl -e、node -e、脚本文件等开放世界写法不覆盖、不做字符串扫描；真正边界是执行层沙箱。已写进 command_policy 模块头、DEVELOPMENT_RULES 与手册 §4.1/5/6.2/7.1（探针统一换 printf 形状）。
- **验证**：见 TESTS.md 同名节（44 条新用例 + 5 变异全 KILLED；环境类失败已逐文件在基线 `6e31e5855` 对照）。
- **收尾（接着做）**：手册 `PLUGIN_EVENT_HOOKS_ACCEPTANCE.md` 的 rm-guard 探针统一改为 `printf '%s\n' 'rm -rf placeholder' > …/rm-guard-probe/ran.txt`（宿主判 mutating 放行、插件字面命中；被拒后 `ran.txt` 不存在即"未执行"证据），§4.1 规则表同步（`sh -c "rm -rf …"` 与 `find -delete` 改判"直接拒绝"），"宿主删除硬拒对照"负例同时放裸 rm 与 sh -c rm；DEVELOPMENT_RULES 已加覆盖边界段。完整 guards9 全过；ruff、doc_sync、strict code-size（2199/hard=0/blocked=False）、`git diff --check`、clean-package、size_diff（新增 0 / 消失 44）全过。待非作者初审，之后交 9b 终审。
- **shellwrap2 修复（初审 shellwrapr 的两处真漏）**：
  - **漏 1（xargs 值选项不全）**：`-a file`（GNU 从文件读输入）、`-J %`（BSD/macOS 替换串）、`--max-args 1`（长选项空格形式）会让值被当成命令、其后的 rm 逃过拆包。修法：`_XARGS_VALUE_OPTIONS` 补 `-a`/`-J`；长选项按 `--opt=value`（单 token）与 `--opt value`（分离，保守消耗下一个非选项值）两种写法处理，`--` 作选项终止符不消耗；抽出单层 `_xargs_after_long_option` 避免尺寸告警。
  - **漏 2（find 多段终止符被截断）**：`_command_args` 在第一个 `;`（shell operator）处截断，转义 `\;` 解析后与分隔符同形，导致 `find . -exec echo {} \; -exec rm {} \;` 第二个及以后的 `-exec` 段被丢弃。修法：find 的动作扫描改用从命令位到 argv 末尾的完整序列，`;` 只当 find 段终止符继续往后扫（宁严；`;` 后的独立命令仍由自己的命令位拦）。
  - 用例：xargs 新形状 4 条、find 多段 4 条 + `;` 后接 shell 命令 1 条、误伤回归 3 条；变异 4 个（M2' `+` 改 `;`、M4' xargs 不跳值选项、M6 回退截断、M7 长选项不消耗值）**4/4 KILLED**（含初审时存活的两个）。相关 97 passed、guards9 全过；ruff、doc_sync（--base a2f5f1b2e）、strict code-size、diff-check、size_diff（新增 0）全过。
- **shellwrap3 修复（3a 终审实测的第三类真漏：xargs 选项解析）**：
  - **问题**：`_xargs_after_long_option` 把"长选项后面跟一个不以 - 开头的词"一律当值吃掉，短选项连写整块当开关跳过、值选项清单缺 `-R`/`-S`。实测放行的有：开关型长选项吃掉命令（`--null`/`--no-run-if-empty`/`--verbose`/缩写 `--nu`）、可选值长选项（`--eof`/`--replace`/`--max-lines`，只能用 = 带值）、短选项连写（`-0n 1`/`-rn 1`/`-tI {}`）、BSD 取值选项（`-R`/`-S`）。
  - **修法**：改成**按已知选项表逐个判定**、线性一遍扫描。长选项：带 `=` 整体跳过；必须带值的（`--arg-file`/`--delimiter`/`--max-chars`/`--max-procs`/`--max-args`/`--process-slot-var`）吃下一个词；开关与可选值（`--null`/`--no-run-if-empty`/`--verbose`/`--interactive`/`--open-tty`/`--exit`/`--show-limits`/`--help`/`--version`/`--eof`/`--replace`/`--max-lines`）不吃；表外的（含 GNU 缩写）给新 finding `COMMAND_XARGS_UNKNOWN_OPTION`（已注册 error_taxonomy），提示改用 `--选项=值` 或 `--`。短选项按字母走：取值字母（`adEIJLnPRsS`）连写或吃下一个词、可选值字母（`eil`）不吃下一个词、开关字母（`0oprtx`）继续、未知字母给同一 finding。选项表放模块常量并注明来源（GNU findutils xargs 与 macOS/BSD 手册）。
  - 用例：12 条原放行写法全部转拦 + 3 条照旧拦 + 6 条误伤回归；`--nu`/`-z` 单列断言 unknown finding。变异 4 个（N1 删 `--null`、N2 删 `S`、N3 未知不给 finding、N4 删 `--eof`）**4/4 KILLED**。相关 119 passed、guards9 全过；ruff、doc_sync（--base 897388630）、strict code-size、diff-check、size_diff（新增 0，首轮 `_xargs_after_short_options` nesting 拆平后归零）全过。
- **shellwrap4 修复（3a 终审发现的既有缺口：前缀运行器后面的删除没拆开）**：
  - **问题**：env/nice/timeout 能拦靠的是 `_unwrap_command_position` 里硬编码的 `_skip_env_wrapper`/`_skip_timeout_wrapper`/`_skip_nice_wrapper`；stdbuf/ionice/chrt/taskset/setsid/flock/caffeinate/unbuffer 不在任何表里、全放行；`sudo -u root rm -rf /tmp/x` 也放行（`_skip_simple_wrapper` 只跳 `-` 开头的 token，`root` 被当命令）。
  - **修法**：前缀运行器改表驱动——`_CommandWrapperSpec`（取值选项 / 开关选项 / 固定位置参数个数 / 是否跳过 `NAME=VALUE` 与 `-`），`_COMMAND_WRAPPER_SPECS` 登记 17 个运行器（sudo/env/nice/timeout/nohup/time/command/exec/builtin/stdbuf/ionice/chrt/taskset/setsid/flock/caffeinate/unbuffer；来源：POSIX/coreutils/sudo/util-linux/macOS/expect 手册）；`_unwrap_wrapper_chain` 沿链推进到内层命令并收集表外选项；表外选项从严（按“不吃值”继续检查、给新 finding `COMMAND_WRAPPER_UNKNOWN_OPTION`，已注册 error_taxonomy，与 xargs 同一处理口径）；解析线性一遍扫描；旧 `_COMMAND_WRAPPERS` 常量与四个 `_skip_*_wrapper` 函数删除。
  - **用例**：23 条前缀运行器“包着 rm -rf 必须拦”（finding 带 `executable` 证据、不是嵌套程序路径所以不带 nested_depth）+ 2 条组合（`timeout 5 stdbuf -o0 xargs --null rm -rf x`、`stdbuf -o0 sh -c 'rm -rf x'`，带 nested_depth）+ 单列 `sudo --weird rm -rf /tmp/x`（`COMMAND_WRAPPER_UNKNOWN_OPTION` + `COMMAND_DESTRUCTIVE_DELETE_BLOCKED` 两个 code，带 option/recovery 证据）+ 误伤回归 7 条（`timeout 60 python3 x.py`、`nice -n 10 make`、`env FOO=1 pytest -q`、`stdbuf -oL tail -f log`、`sudo -u root ls`、`flock /tmp/l echo hi`、`caffeinate -t 60 make`）。
  - **变异**：4 个（P1 删 stdbuf 表项、P2 timeout 位置参数改 0、P3 flock 位置参数改 0、P4 表外选项不再给 finding）**4/4 KILLED**，逐个 sha256 还原、还原一致。
  - **门禁**：相关 152 passed（新文件 109 + test_command_policy 43）、guards9 现存 11 文件 187 passed；ruff、doc_sync（--base 993357744）、strict code-size（2199/hard=0/blocked=False）、diff-check、clean-package、size_diff（新增 0 / 消失 44）全过。

## 编程错误不再按消息文字被判可重试（progerr，2026-10-05，worker/progerr，基于 17k 头 `9e2f0eb69`；待 3a 复审）

- **问题**：裸的编程错误（TypeError/AttributeError/KeyError 等）走 `contracts/provider_error_classifier` 的文本兜底，消息里恰好含 `deadline`/`timeout`/`rate_limit` 之类的词就被判成可重试，进入 `provider_transient_auto_resume` 的退避重试，白白等到预算用完、界面一直显示"正在重试"，把真实问题掩盖成"供应商故障"（3a 在 17k 头实测：`TypeError("...total_deadline_seconds")` → TIMEOUT/retryable；`AttributeError("...timeout")`、`KeyError("rate_limit")` 同理）。
- **修法（唯一权威位置：分类器）**：新增 `_PROGRAMMING_ERROR_TYPES` 名单（TypeError、AttributeError、NameError、LookupError 含 KeyError/IndexError、AssertionError、NotImplementedError、ImportError、SyntaxError），在 typed 检查之后、文本/状态码兜底之前按类型拦截为 `UNKNOWN`/不重试；不提取消息里的数字当状态码。**ValueError 故意不在名单里**（JSONDecodeError 是它的子类，截断的供应商响应可能需要重试），保持文字兜底原行为。
- **为什么改分类器而不是重试判断**：全仓 grep 确认 `classify_provider_error` 生产调用方只有 `provider_transient_auto_resume.py` 一处、其 `.retryable` 是唯一消费点，无展示/记账消费；分类器是"给异常定性"的权威位置，改这里后未来任何消费方都拿到正确分类，重试判断一行未动（避免两处各改一半）。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「编程错误不按消息文字判可重试」小节。15 个直接 import 相关模块的测试文件 **313 passed / 1 skipped**；guards9（11 文件）**187 passed**；4 个变异全 KILLED（删 TypeError、改按消息文字、LookupError 收窄成 KeyError、ValueError 误加名单）。
- **未验证**：没有连真实 provider 构造"编程错误链"端到端（规则禁止联网）；17k 收口时建议在沙箱外复核一次真实链。

## B5 联合用例转正与 ask 三出口账本（jb6b，2026-10-05，分支 `worker/jb6b`，基于 17k 头 `9d5165809`；待 3a 复审）

- **来源**：jb6（sol3）留了两条 strict xfail，理由写的是"B5 审批出口不承接门决定条目"，修复归 `worker/b5fix9b`。b5fix9b（`b037ce5e8`）已挑进 17k，两条应当转正——本轮把它转正并补齐同一族缺口。
- **rebase**：`git rebase 9d5165809`。6 个文档文件冲突（两边各自在顶部加小节），**两边都保留**；两个测试文件自动合并。
- **转正**：删掉两条 `@pytest.mark.xfail`，同时删掉 `_PendingGateLedger` 这个"空账本就豁免"的专用异常与 `_assert_ledger_and_info` 里的豁免分支——**读到 0 行现在直接失败**。
- **新增 4 条联合用例**（真实 gateway 回合 → 真实审批链 → 真实 runtime.db → 真实 `/plugins info`）：批准后 handler 真跑且账本留行；不可交互（客户端未声明 `tool_approval`）时 ask 收紧为 `PLUGIN_GATE_APPROVAL_UNAVAILABLE` 且终态投影正确；审批消费者答 unavailable 时终态在审批阶段投影（专门钉 b5fix9b 的 `unavailable=True` 承接）；同 owner 多行"最近决定"按新到旧渲染。
- **测试驱动改造（产品零改动）**：`_run_case` 收 `_RunSpec(name, arguments, decision, interactive)`；审批侧只替换"用户选了哪个决定词"，其余走原真实链路。
- **一个观察（交 3a/9b 判定，本轮不修）**：同一回合里 turn 事件的 `thread_ref` 来自 `conversation.channel_conversation_id`，tool 事件的 `thread_ref` 来自 task attribute `conversation_thread_id`，**取值不同**（实测 `joint-gate-thread` vs `thread-00318810477a4d4a`）。两处来源已核实（`gateway_parts/event_points.py::prompt_queued` 与 `agent_core/tool_loop/recovery.py::runtime_run_scope`），是产品行为不是用例问题；插件按会话归并两类事件会归不上。
- **验证**：两联合 11 passed / 1 skipped / 0 xfailed；两联合 + b5s5 节 27 文件清单 **606 passed**；guards9 全过；ruff / boundaries=0 / DOC_SYNC_PASS / diff-check / strict code-size hard=0 / size_diff 新增 0 / clean-package 全过。变异 2/2 KILLED（去掉拒绝出口承接、去掉无法审批出口承接）。详见 TESTS。

## B5真实样例联合验收（jb6，2026-10-04，worker/jb6，WIP：观察已证实，ask账本待worker/b5fix9b）

- **来源与基线**：从真实17j头 `1ec5ed155d6b393f6112c3e3341c4f935bb94dc8` 开出，`git show e388b85e7` 取sol2两个联合文件和七份文档差异；文档适配现有17j上下文，保留下方jb5历史，不搬临时B5产品或重放已集成提交。
- **观察根因**：原样复现4 failed/3 passed/1 skipped。夹具给rm-guard和event-watch同一个 `joint-activation`，B2按owner/activation复用rm连接，事件能力未声明而unavailable；只改测试的各插件独立激活就让普通删除观察/真实写账/info断言通过。新增共用池两连接归属、真实三事件正对照与failed/unavailable零断言，不新建池、不用空帧冒充零工具事件。
- **账本待外线修复**：观察恢复后，插件ask被用户拒绝的canonical结果重建丢门决定，实际库0行。本轮曾临时验证保留专键的修复；3a随后明确同处账本、UNAVAILABLE终态及interactive归ds6 `worker/b5fix9b`（基于 `4081a0df3`），已撤销本线全部产品修改。两ask后SQL/info用例仅匹配专用 `_PendingGateLedger` 的strict xfail，原因标外线；此前观察/协议/审批失败仍普通报红，不吞断言、不手补行。
- **命令与截断**：裸rm宿主硬拒断言保留；用已有规则差集 `sh -c "rm -rf build"` 进入零副作用计数handler的原执行器，由真实样例ask/RM_RF并消费用户拒绝，绝不真正运行删除命令。当前样例已保截断可见deny，原strict xfail实际XPASS后转普通断言；不可见删除仍ask/ARGUMENTS_TRUNCATED。
- **当前验证**：撤销产品修复后显式29文件（两联合＋原b5s5 27清单，含B8及完整guards11）589 passed/1 skipped/2 strict xfailed，0失败/错误；唯一skip为无Node event-watch样例。四B5联合实际跑观察/协议及原拒绝；两直接deny真实唯一行/info通过，两ask确实到SQL读0行才xfail，完整账本未通过。旧591是临时修复实验结果，不代当前头；命令、反证、门禁见TESTS。
- **初审与下一步**：已接非作者内联静态初审，采纳必备B5字段改强断言、只读event-watch目标帧并核三事件/同thread_ref；不是独立运行复审或9b终审。多行“最近”排序、批准/取消/无法审批及interactive须等ds6修复后真实联合补证。安装/激活与启动适配、手工processing不代表生产启用/B7/worker/TUI/IM/Gateway/Linux；未部署、不push，不改保护文档。

## 插件第一期联合证据收窄与收紧联合草稿（jb5，2026-10-04，历史WIP，来自e388b85e7）

- **解决问题与来源**：按3a和luna1对 `d6617f196` 的复核，仅迁入 `804a0fbb3..d6617f196` 的联合测试，不带临时B8产品提交。口径是“隔离handler/执行路径接真实样例，队列消费手动推进”，不是worker自动认领测试。
- **真实部分与边界**：原样event-watch/rm-guard stdio、B3 Hub、B2共用池、展示；安装记录合成、启动测试适配，不证明产品安装/启用或B7。旧AST导入后pytest.fail提醒换为真实收紧草稿，缺B5仅按执行器结构字段skip。
- **当时事实**：临时 `73de50cb8` 组合B5两段，仅验证、不提交产品；两文件4 failed/3 passed/1 skipped。rm宿主前置硬拒；三个patch观察空/unavailable；账本/info未执行到，根因当时未确认。可见删除截断专用strict xfail不能吞观察/账本失败。新阶段事实以上方jb6为准，历史不回写成通过。
- **接口交接**：第3/4段变化时同步guard_joint组合根/reviewer、_run_case执行/审批消费、查询/binding；S5/B6另核SQL/info。源命令、验证与旧边界见TESTS历史jb5节。

## 出站字节稳定补丁：结构化路径护栏与已知边界（ck3fix，2026-10-05，分支 `worker/ck3fix`，基于 `d76f04e61`；待复审）

- **来源**：cachekey3 初审（cachekey3r）结论「小问题 5 项」；按 3a 新规则，只涉及测试与注释的小问题由审阅人直接补。产品逻辑不动。
- **补了什么**：
  1. 结构化 prompt（`CacheStructuredPrompt`）路径的重载字节一致用例——该路径是宿主真实主路径（`tool_ir_history`/`builder`/`compact_semantic_summary`/`compact_provider_surface` 等 5 个生产构造点），原来只有 legacy 路径有对照用例；现在两条路径都有回归钉。
  2. 声明外键保留用例——声明表之外的键必须追加保留（不丢字段），此前无用例。
  3. `tool_result.content` 块列表的已知边界：注释写明「不递归、嵌套键序保持原样；当前生产构造点恒为字符串，不触发分叉；将来改递归时边界用例会红提醒」，并补行为用例记录当前行为。
  4. WS 首包 ensure_ascii 变化核实：`\uXXXX` 转义与 UTF-8 原文 JSON 解析等价、token 不变；字节层只有切换瞬间的一次性变化；WS 与 HTTP 统一到唯一编码器后不再有口径漂移。无功能影响，无需改产品代码。
- **变异**：3 个（分别对应 3 条新用例），全部抓住；产品文件原样还原（sha256 一致）。
- **未验证**：真实服务端缓存命中率改善（需联网与真实账号）；真实 Gateway 端到端。

## 出站字节稳定：只规范化历史消息块，不碰工具与 schema（cachekey3，2026-10-04，分支 `worker/cache-key2`，基于 17j 头 `c544b358d`；本轮返工，含 cachekey2 的最终口径；待复审）

- **背景**：cachereload（`8b42b07b5`）修了 OpenAI/Responses 路径「工具参数 JSON 键序在重载前后不同」。cachekey2 查其它出站协议，发现 Anthropic 路径分叉更大：`tool_use.input` 是 **JSON 对象**而不是字符串，整个请求体由唯一编码器 `gateway_helpers.gateway_request_body` 落字节；实测重载前后请求体有 **24 段差异**——不只是 `input` 的键序，`{"type","text"}`、`{"type","thinking",…}`、`{"type","tool_use",…}`、`{"type","tool_result",…}` 等**所有消息块的键序**都变（磁盘读回是字母序，进程内构造是自然序）。
- **cachekey2 的修法被否（3a 裁定，理由成立）**：在唯一编码器里整包 `sort_keys` 会顺带改掉两类**与缓存无关**的顺序——工具 JSON Schema 的属性顺序、结构化输出 schema 的顺序。后者会改变模型行为：严格结构化输出按 schema 顺序生成字段，curator 的 `content` 会被排到 `confidence`、`conflicts_with` 之后，等于让模型「先写结论、后写依据」。这是夹带的模型行为变化，不能接受。
- **最终修法（cachekey3）**：
  1. `gateway_request_body` **恢复不排序**，保持「调用方给的键序就是线上字节」的原合同；`test_gateway_strict_request.py` 两条测试恢复原断言。
  2. 稳定性只做在**来自历史的部分**：`anthropic_prompt_cache.anthropic_messages_with_optional_cache`（Anthropic 消息的唯一投影入口）先调 `normalize_anthropic_history_blocks`，按 `_BLOCK_KEY_ORDER` 声明重写**已知块**的键序；`tool_use.input` 用排序往返固定（与 `_openai_function_call` 同口径）。声明外的键追加在后面、保持相对顺序，未知块类型原样返回——不丢字段、不猜结构。**工具定义与任何 schema 完全不经过这里。**
  3. 选「按块重建」而不是「整棵 messages 子树排序」：子树排序会把 `tool_result.content` 里的 JSON、媒体块 `source` 等**与历史键序无关**的嵌套结构也改掉；按块重建只动已知块自己的键，影响面最小且可枚举。
  4. `responses_websocket._send_create_message` **保留**复用 `gateway_request_body`：WebSocket 首包本质就是模型请求体，本来就该走唯一编码器，不能自己另写一份 `json.dumps` 而与 HTTP 口径漂移（现在的口径是「保持调用方顺序」，与 HTTP 一致）。
- **已核无需改**：工具结果结构化内容、媒体块元数据、`RuntimeFactsTurn`、`CompactionSummary` 只产出 dict，最终统一经唯一编码器；`input_media.py` 与 `decision_protocol.py` 各自的 `json.dumps` 已是 `sort_keys=True`。
- **老会话的影响**：**没有额外的一次性未命中**。老 transcript 里的 envelope 本来就是字母序写入；规范化后当轮新构造的部分也产出同一规范序，两边第一次请求即一致。唯一过渡是上线瞬间服务端缓存里存的仍是旧字节，首次请求对不上、之后稳定。
- **测试**：`test_backends_provider_history_reload_identical.py` 保留 Anthropic 重载字节一致，新增「三个后端工具 schema 顺序不变」「openai/anthropic 结构化输出 schema 顺序不变」「tool_use.input 规范键序」「唯一编码器保持调用方顺序」；`test_responses_websocket.py` 的首包用例改成「走同一编码器且保持调用方顺序」。结果见 TESTS。

## B 包 0.3.3：出镜缺参考升级为错误 + 计划中参考的交接登记（pb33，2026-10-04，分支 `worker/pack-b-033`，基于 ds7 的 B 0.3.2 `c779811bc`；待复审）

- **来源**：B 包 0.3.2 重跑上一轮没过的 B05、B10 各两次，独立业务审阅 4 例都不合格（结构检查全过）。两类不合格：① B-RELATIONS 3 例——镜头里出现的角色没连到参考（如 SH03 的动作节拍有 C02，`references` 里却根本没有 REF-C02，SH03 只连了 REF-L01），检查器当时报 `shot_character_reference_missing` 但只算提醒，模型看到提醒就没改；② B-MEDIA 2 例——交接里没把这些缺的参考列成缺项。合格的 B05-t402 正是给 C02、P01 各新建一条 `planned` 参考、连进镜头并在 `additions` 里登记，说明模型做得到、只缺硬要求。另有 B05-t401 的 C-BOUNDARY：模型在工作区外 owner 根下的 `output/` 又写了一份 `project.json`、`handoff.json` 副本（模型行为，包只能在说明里写清楚）。
- **改法（两条都收紧判定，不是放松）**：
  - `scripts/check_continuity.py`：`shot_reference_warnings` → `check_shot_character_references`，`shot_character_reference_missing` 从提醒升为**错误**，条目带 `{path, character_id}`；纯环境镜头（引用节拍里没有角色字段）不报，项目完全没有人物参考计划时不报。
  - 新增 `check_handoff_reference_coverage` + `project_index`：被至少一个镜头 `reference_ids` 引用、且 `state` 是 `planned`/`missing` 的参考（人物、地点、道具都算）必须在交接的 `additions` 或 `unresolved_differences` 里有一条**指向它**的登记（指针指向 `/references/<下标>`），否则报 `handoff_missing_planned_reference_entry` 错误。没被任何镜头引用的参考不强制；只在给了 `--handoff` 时核对。
  - 两条新 hint 在 `error_hint()` 里按结构化字段现算（`shot_character_reference_missing` 报镜头号+角色 ID，`handoff_missing_planned_reference_entry` 报参考 ID）；0.3.2 那批写进 `GENERIC_HINTS` 的占位符条目已删（`error_hint()` 只替换 `{path}`，别的占位符会原样漏给模型——上一轮的真实缺陷，本轮用通用用例钉住）。
  - `CAPABILITY.md`、`methods/workflow.md` 写清两条新规则，并加“交付只写本次工作区内（建议 `output/`），不在工作区外另写副本”。
  - 示例数据同步：`resources/example-project.json` 的 SH02 补上 `REF-C02`（它引用的 B02 说话人就是 C02，0.3.3 起这处原本就缺人物参考）；示例项目必须自身合规。
  - 版本：`declaration.json` 与脚本 `PACKAGE_VERSION` 同步升 0.3.3；`PROVENANCE.md` 新增 0.3.3 修订节（含接受集合变化）。**接受集合变化**：`shot_character_reference_missing` 由提醒变错误（会触发返工）；新增 `handoff_missing_planned_reference_entry`。
- **handoff 登记语义的口径（本轮关键修正）**：3a 第三轮裁定以 (b) 为主——参考“已登记”指交接里有一条地址**指向该参考**（`additions[].target` 或 `unresolved_differences[].refs[]` 的指针为 `/references/<下标>`），只认结构化地址、不解析 `reason` 等说明文字。第一版实现按文字里出现 ID 来认，导致示例自带的三条 `planned` 参考（写进 reason 也没用）在 8 条既有用例里仍被判“没登记”；改为按指针认定后 10 条红用例全绿。
- **验证**：见 `TESTS.md`“B 包 0.3.3（pb33）”小节。
- **未验证**：真实模型重跑 B05、B10 各两次没做（按 3a 安排，等本包挑入后由 3a 打包重跑）；宿主侧是否把 `hint` 转给模型这一步在本分支不可验证（跨分支，等合并后复核）。

## B 包 0.3.2：交接报错带出“该填什么”（pb32，2026-10-03，worker/pack-b-032，待复审；等 B 类 9 例审完可能再补）

- **来源**：B 包 0.3.1 重跑的第一批业务审阅两个不合格，都是交接文件的结构问题、内容侧没问题。① 交接里某条映射的指针指向镜头内的 `reference_ids` 字段，模型把 `object_id` 写成了字段名 `reference_ids`（应写所在镜头的 ID）；检查器只报“对不上”，没说该填什么，宿主三次核验全失败、模型返工后仍照错写。② 另一条未决差异的 `refs` 写成空数组，报 `bounded_references_required`，模型只回了一句“等待宿主核验”就收手，交接没修完。
- **改法**（只增强报错与指引，不放松判定）：
  - 检查器 `scripts/check_continuity.py` 的 `object_id_mismatch` 报错带上 `pointer`、`expected_object_id`（指针所指位置所在对象的 ID）、`actual_object_id` 和一句 `message`；算不出所在对象 ID 时 `expected_object_id` 为 `null` 并在 `message` 里说明“这个位置没有对象 ID”，不再笼统报“对不上”。
  - `bounded_references_required` 报错带上 `index`（是哪一条）、`count`（当前条数）、`requirement`（至少一条、每条用 file_id + pointer 指向这条差异涉及的对象）和 `message`。
  - 判定口径明确化（3a 裁定，接受集合确有放宽）：0.3.1 要求 `object_id` 必须**直接**指向带 `id` 的对象，指针指向对象内部字段时一律算错；0.3.2 起改为填“指针所指位置所在对象”的 ID 即可——`/shots/0`、`/shots/0/seconds`、`/shots/0/reference_ids` 都写 `SH01`。字段名、与所在对象 ID 不符、指针不存在仍是错误；该位置往上层找不到带 `id` 的对象仍报错（expected 为 null）。`refs` 判定不变（至少一条、只能指向该阶段输入或输出），只是报错更具体。
  - 报错可照做：0.3.2 起 `--host-json` 的每条错误可带可选 `hint`（宿主只转这一句、最多 200 字符）：`object_id_mismatch` 说清指针、实际值和该改成什么；`bounded_references_required` 说清是哪一条、refs 要加什么；另有 30 多个改法明确的错误码各有通用改法，判断不出怎么改的不给 hint。
  - **hint 不许诱导模型"一删了之"**（3a 复审指出）：凡删掉会丢交付内容的错误，hint 一律先引导"补上/改对"，删除放最后并写清条件。已改两处：`uncovered_beat` 从"加进某个镜头的 beat_ids 或删掉这个节拍"改成"把它加进讲到这段内容的镜头的 beat_ids；只有这个节拍本来就不该存在时才删除"；`unexpected_field` 从"删掉它"改成"把里面的内容移到约定字段里；确认它确实不该存在时才删除"。删了不丢交付内容的（`unknown_reference_mention`、`handoff_claim_without_change`）按 3a 裁定保留原措辞。用例钉住这两句的先后顺序。
  - 包内指引：`methods/workflow.md` 写清 `object_id` 是“指针所指位置所在对象的 ID，不是字段名”，给通用正反例；每条未决差异至少一条 `refs`，给通用例子和“不要留空数组”；并写明宿主核验没通过要按报错把文件改对再收尾、别只回复“等待核验”。`CAPABILITY.md` 同步一句规则和版本说明。
- **同时修的第二类（B05-t303）**：未决差异的 `refs` 写成空数组，`bounded_references_required` 只说“不满足”，模型没修完就收尾。检查器报错带上是哪一条（下标）、当前条数和要求说明；`methods/workflow.md` 补每条未决差异至少一条 `refs` 的写法与通用例子；两处文档写明宿主核验没通过要按报错把文件改对再收尾，别只回复“等待核验”。
- **版本与文档**：0.3.2 同步到 `declaration.json`、脚本 `PACKAGE_VERSION`、`CAPABILITY.md`、`PROVENANCE.md`（新增 0.3.2 修订节，只写问题类别）、`examples/capability-packages/README.md` 与四处测试断言。
- **验证**：B 包 6 个测试文件 202 passed（含 hint 三例与措辞一条）；guards9 173 passed；三个 `test_handoff_*` 30 passed；`test_background_handoff.py` 22 个失败在真基线 `ce646f783`（`git merge-base claude/3a-step17i HEAD`）上同样 22 个、名单完全相同，属沙箱环境既有失败；变异三组共 11 个全部被抓（第二轮 3 个：hint 丢掉 / 期望值与实际值写反 / refs hint 不说明加什么，其中“写反”首轮存活、补强断言后才被抓；第三轮 2 个：`uncovered_beat` 与 `unexpected_field` 退回“一删了之”的旧措辞）；`leak_check.py` 6 字/5 字除已接受的「短剧制作资料」外无新命中；`import_boundaries` 0、ruff 通过、`DOC_SYNC_PASS`、`git diff --check` 干净、`check_clean_package.py` OK、strict code-size `hard=0 blocked=False`、size_diff 新增 0。命令与真实结果见 TESTS.md。
- **未验证**：真实模型重跑 B 类 9 例没做（等 3a 安排）。宿主侧由 ds4（worker/pack-host-hints）配合改投影：宿主只转 `hint`。我这边已把检查器侧的 `hint` 供出来，**宿主真的把 hint 转给模型这一步没在我这边验证**（跨分支，等 17j 合并后复核）。
- **不做**：B 类其余审阅结果对应的改动，等 3a 另外派。

## 辅助（压缩）调用的可靠时限（cabfix→cabfix2，2026-10-04/05，worker/cab-fix，基于 luna3 调查提交 `c39250781`；cabfix2 已按 3a 裁定修完初审风险 1 与联动口径，待 3a 复审）

- **来源**：luna3 的调查（`decision-evidence/compact-aux-timeouts-cab-luna3.md`）证明压缩这类辅助调用没有任何可靠的调用级时限：①主模型有按输入估算的动态首包预算，压缩没传，大上下文的压缩会被基础读超时过早判成 `first_event` 超时；②socket 读超时是"两次读之间的间隔"不是调用总期限，慢滴流能无限期拖住一次压缩；③只要一直有有效事件，流式就能无限续期。1800 秒重试预算只在异常返回后决定要不要再试，也不是单次调用的硬期限。
- **修法**：
  - ① `auxiliary_model_call._auxiliary_first_event_budget` 复用主模型同一个估算函数（`estimate_first_token_timeout` + `first_token_timeout_options`），按辅助调用自己的输入量算，不另写一套；估不出或没开动态超时配置时返回 None，退回原基础读超时。
  - ②③ 新增 `AUXILIARY_CALL_FLOOR_SECONDS`（240 秒兜底）与 `AUXILIARY_STREAM_TOTAL_TIMEOUT_SECONDS`（1800 秒流式总时限）。非流式取 `max(request_timeout, 兜底)`；流式取 `max(request_timeout, 流式总时限, 首包预算 + request_timeout)`（最后一项是 cabfix2 的联动修正，见下）——**流式不叠兜底**，否则兜底会顶掉更小的总时限、让"宽但有限"这个口径失真。
  - 绝对期限走既有 `ProviderRequestOptions.total_deadline_seconds` → `GatewayRequest.deadline` → `_StreamIdleWatchdog.hard_deadline`（新增的不可续期硬期限：有效事件只推后 idle，推不动 hard）。**不新增配置项**：两个窗口都从既有 `request_timeout` 同源推导，避免又多一个几乎不会调的用户旋钮。
  - 超时按结构化原因收口：`_record_auxiliary_failure` 把 `ProviderTimeoutError` 记成账本的 `timed_out`（带 `timeout_stage`），其余异常仍记 `failed`；记账失败不顶掉原始异常。
- **cabfix2（按 3a 裁定修初审风险）**：
  - **风险 1（首次排程忽略 hard）**：`_StreamIdleWatchdog.start()` 原来只按 `_idle_deadline` 排首次定时器，而首包预算上限来自 `dynamic_timeout_max`（默认 10800 秒），大于 1800 秒总时限——大上下文压缩卡在首包前时硬顶形同虚设。改成 `_first_deadline_locked()`：hard 为 None 时退化为原值，否则取 `min(idle_deadline, hard_deadline)`；注释写明"touch 只推 idle、不重排定时器，所以首次排程必须把 hard 算进去"。
  - **流式总时限与配置联动**：流式分支从 `max(base, 1800)` 改成 `max(base, 1800, 首包预算 + base)`，让 `dynamic_timeout_max` 真的生效（调大它会同步放宽硬期限）；`agent_config.yaml` 的 `dynamic_timeout_max` 中文注释补了这条口径。非流式 `max(request_timeout, 240)` 不变。
  - **风险 3 护栏**：新增静态用例确认所有生产后端的流式入口（`request_stream`/`request_stream_iter`）都接受 `options`（第 1 档），将来有人加只接受 `first_event_timeout_seconds` 的后端会变红；另锁 `GatewayRequest.deadline` 字段与 `post_stream` 首参契约。不改分派逻辑。
  - **拆平 size_diff**：新用例引入两条新增告警（`_production_stream_targets` nesting、`total_target` 参数个数），已拆平：抽 `_module_stream_entries` 生成器（嵌套 4→2）、第 2 档替身改 `*args` + 两个 kw-only（参数 5→3；不能收 `**kwargs`——`_accepts_kwarg` 把 VAR_KEYWORD 误判成接受 `options`，会让分派器走错档）；补两条断言核对关键字与位置参数送达。
- **不改主模型路径**：`total_deadline_seconds` 默认 None，主模型的 `deadline` 仍是 None、持续有效事件照旧不被总墙钟误杀（`gateway_helpers` 里那句"持续有效 data 不得再被整次请求的总墙钟误杀"的语义保持）。
- **签名兼容**：`request_stream_lines` / `_gateway_request` 按被调方签名分派，旧替身和假后端保持原调用形态；新参数收进 `StreamCallOptions` / `StreamCall` / `_StreamObservers` / `AuxiliaryTimeoutPlan` / `AuxiliaryBackendSurface` 等小数据类，参数门禁从 hard=1 回到 0。
- **期限全覆盖（3a，10-05，ds6 fix3ar 实测）**：Anthropic 非流式也把辅助调用绝对期限交给传输层；OAuth 信封与基类一样走 `provider_request_timeout`（后台预算生效）；WebSocket 空闲阶段的绝对期限另由 wsdeadline 补。见 [TESTS](TESTS.md)「Anthropic 非流式绝对期限与 OAuth 后台预算」。
- **传输签名一处收口（3a，10-05，17k Linux 车道实测）**：`_gateway_request` 的两种调用形态与绝对期限换算只在 `http.stream_call_parts`/`stream_call_deadline`；覆盖 `_gateway_request`/`request_stream_iter` 的子类（OAuth）必须与基类同签名，协议后端调流式入口一律经 `request_stream_lines` 按签名分派。见 [TESTS](TESTS.md)「OAuth 与 Responses 后端跟上 cabfix 的新传输签名」。
- **只估算一次（3a，10-05，17k 终审）**：一次辅助调用的输入只在记账时估算一次，首包预算和绝对期限都用这一份（`_auxiliary_timeout_plan`）；不要在分派或期限函数里复算。见 [TESTS](TESTS.md)「辅助调用只估算一次输入」。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「辅助调用可靠时限（cabfix）」小节。luna3 的刻画测试从"记录现状"改成"断言修复后行为"（晚首事件不再过早超时、慢 body 在绝对期限处结构化超时、持续有效事件在总时限处停止）。cabfix 3 个变异全被抓（去掉首事件预算、去掉非流式绝对期限、watchdog 忽略硬期限）；cabfix2 复核 4 个变异全 KILLED（start 退回只按 idle、流式硬期限退回固定 1800、删 options 第 1 档分派、第 2 档替身改 **kwargs），size_diff 新增告警归零（0 / 消失 35）。

## 断网时模型调用卡住不超时（hang，2026-10-04，睡眠归因已记录；睡眠恢复待设计）

- **调查结论**：3a 的电源日志显示 2026-10-03 17:17:38 合盖进入 Clamshell Sleep，直到 2026-10-04 04:29:12 才完全唤醒。8 个结构化请求结果全部早于完全唤醒，且集中在 DarkWake 或睡眠转换边缘；墙钟事件跨度不能证明进程在睡眠时持续执行。当前证据更支持“单调时钟/等待在系统睡眠期间暂停，短暂唤醒时请求继续并失败”，而不是已经证明 240 秒超时失效。DarkWake 仅是电源状态事实，不证明请求线程连续得到调度或网络可用。
- **计时边界**：`request_timeout=240` 的流式首事件/idle watchdog 和 socket 读取使用 `time.monotonic()`；只有完整 SSE `data:` 行刷新 idle，不是收到任意字节就刷新。`dynamic_timeout_max=10800` 是动态估算上限，不是每次请求固定等待 3 小时。回合级重试预算默认 1800 秒，同样按单调时钟，仅在异常返回后决定是否发起下一次重试，不会中止正在进行的请求；传输层 2/5/15 秒重试另算。Compact 的真实慢滴流对照见 `TESTS.md` CAB 节：辅助 stream 首事件没有主链的输入动态预算，辅助 non-stream 慢 body 超过基础 `request_timeout` 仍成功，说明存在睡眠无关的预算缺口。
- **睡眠恢复后续（状态：待设计，未落地）**：优先评估平台睡眠/唤醒通知与墙钟—单调钟差值作为审计信号的组合；只有确认真实 suspend/resume 后，才关闭在途 socket、记录结构化 `interrupted_by_sleep`，并进入可验证的 provider retry / 回合续跑链。**风险清单**：①系统校时或长调度停顿不能误判成睡眠；②重试可能重复供应商计费；③不能重放已提交工具副作用；④不能接受已放弃 attempt 的迟到响应；⑤必须正确释放中断句柄、连接及 Compact 账本。DarkWake 不证明网络恢复；跨 macOS/Linux 检测与 exactly-once 语义需单独设计，本次不实现。
- 逐文件电源时点、代码位置、假服务测试命令及未验证边界见 `TESTS.md` 同名节。没有连外网/真实 Gateway，没有让机器睡眠，也没有修改产品代码；没有将任何失败判作“既有失败”。

## Compact 辅助调用预算（cab，2026-10-04，本机复现确认；修法待 3a 决定）

- 慢滴流 loopback 假服务已分别走主模型与 `auxiliary_model_call`。`request_timeout=1s` 时：大提示主模型 stream 的动态首事件预算（下限 3s）在 1.626s 收到事件；Compact stream 因回退基础 timeout 在 1.009s 以 `first_event` 超时。每 0.45s 一个有效 SSE data 的健康流，主模型/Compact 分别 2.736/2.730s 完成，说明 idle watchdog 可被有效事件持续刷新。
- non-stream 慢 JSON body 的主模型在 1.012s 由外层 `wall_clock` 守卫终止；Compact 在 3.231s 仍完整返回（body 每 0.4s 送片），说明 auxiliary 的 socket read timeout 不是单次调用总期限。Fake monotonic 又验证单个 operation 即使跨过 1800s 重试预算才成功返回，也不被重试包装中断。
- **结论：独立的 Compact 预算缺陷，应修；不是睡眠事故根因证据。** 修法建议：对出站输入计算并传入 request-local 动态首事件预算；non-stream auxiliary 加绝对请求期限，超时用结构化 `ProviderTimeoutError` 和 timed-out 台账结算；另由 3a 决定是否给 Compact stream 加总 active-time 上限（否则持续有效 data 可以无限续期，1800s 预算不取消 in-flight）。实现与本机回归约 1–2 小时。此轮只加测试/文档，未改产品代码。详见 `TESTS.md` CAB 节。

## A 包 0.5.4 复审问题收敛（pa54fix，2026-10-05；3a 终审通过，已并入 step17k）

- **3a 终审（10-05）**：核对 QUOTE_PAIRS 统一后半角 `"` 的开闭交替正确；所有出现位置的检查做了“只看第一次出现”的变异，用例抓到。A 包相关 34 个测试文件 878 passed，leak_check A 6 无重合。A 包 0.5.2→0.5.4 全线（含 pah、pahfix）并入 17k；版本仍是 0.5.4，真实模型重跑 A 类没过的用例由 3a 安排。

- **来源与边界**：ds4 初审指出 `verbatim_not_quoted` 在 A 类无引号散文素材上不会触发；按 3a 裁定不新增语义判据，明确它只适用于带成对引号的原文，A05-t401 改归“③ 需语义判断，未解决”。
- **引号检查**：同一句在引号外先出现时，继续检查 passage 的所有匹配位置；必须至少有一个完整匹配位于引号内容区间。`QUOTE_PAIRS` 是唯一引号对定义，供 `QUOTED_SPAN` 和 `quoted_spans` 共用，含半角双引号；跨闭引号文本仍报错。
- **hint 来源守卫**：源码 AST 测试扫描所有含 `"hint"` 键的字典字面量，只允许 `HINT_CODE_TEMPLATES[...]` 或 `_dynamic_hint(...)`。pahfix 的 R5 硬编码 `PROP_ORIGIN_HINT` 变异已被该测试抓住；逐码真实触发与宿主投影守卫仍保留。
- **文档与版本**：同步 `PROVENANCE.md`、`CAPABILITY.md`、`TESTS.md`；包版本保持 0.5.4，不升 0.5.5。
- **状态**：最终 A 包测试 globs 与 guards9 全部通过；防泄露 A6/A5 均无命中；import boundaries、Ruff、doc-sync、strict code-size、`size_diff`、clean-package 与 `git diff --check` 均通过。修复已正式提交，版本仍为 0.5.4，等待非作者初审。

## A 包 hint 守卫与动态 ID 加固（pahfix，2026-10-04，0.5.4，ds5 初审通过，R5 已在 pa54fix 收口；已并入 step17k）

- **来源**：pahr 初审发现原守卫没有真实触发所有带 hint 的错误/警告，且 U+0085 控制字符变异存活。
- **改动**：检查器结构化登记全部 12 个 hint code；静态提示由发射点读取登记表，动态提示经 `_dynamic_hint(...)` 按登记码构造。源码 AST 守卫检查所有带 `"hint"` 键的字典字面量，值只能是 `HINT_CODE_TEMPLATES[...]` 或 `_dynamic_hint(...)`；真实合成交付仍逐码触发，并核对 registry 与实际输出的 hint code 双向相等。
- **输入安全**：所有动态来源段落 ID、镜内交接 holder/prop ID 在拼接前过滤 Unicode General Category `C*`、ASCII 花括号转全角；每片最多 20 字符、来源 ID 最多列 3 个，最终 hint 不超过 200 字。检测器仅输出单行提示，不涉及允许换行的 hint。
- **版本**：0.5.4 尚未打包，保留版本号，不升 0.5.5；`PROVENANCE.md` 的 0.5.4 节已注明本轮修订。
- **状态**：已提交到本地分支；ds5 初审确认 R1–R4 被抓，R5 的源码来源缺口已由 pa54fix 的 AST 守卫补上并实测拦截。版本仍为 0.5.4，待非作者终审。

## A 包 0.5.4：按 9 例业务审阅补两条结构检查（pa54，2026-10-04，worker/pack-a-053；ds4 复审，问题由 pa54fix 收口；已并入 step17k）

- **来源**：A 0.5.2 三批业务审阅 9 例只过 1 例（A10-t403）。先做了失败归类表（8 例逐条，见 PROVENANCE 0.5.4 节的表），分三类：① 0.5.3 已覆盖、② 能做成结构检查、③ 只能靠模型质量的语义问题。
- **① 逐例核实**：用 0.5.3 的检查器跑 8 例真实产物，规则①（道具名在原文里却标成新增）确实抓住了 A05-t402 的纸袋、A10-t402 的停航牌；规则③抓住了说明与原文矛盾；规则②抓住了新增对白未标。这部分不需新工作。
- **② 本次做两条**（只做误报风险低的）：
  - `verbatim_not_quoted`（错误）：只在原文段落带成对引号时，核对台词是否有完整出现位于引号内部；原文无引号时不触发，是明确已知边界，不证明第三人称叙述确为台词。A05-t401 的 P02 无引号，仍属③需语义判断、未解决。
  - `intra_shot_handoff_unstated`（**警告**）：同一镜头内道具从一个人换到另一个人手里（`prop_states` start→end 的 `holder_id` 不同）时提醒。A10-t401 的 SH06 就是这类。**为什么是警告**：作者可能觉得"action 里已经写了递灯"就够了，规则无法证明他写错，强判错误会误伤；作为提醒推动补 `prop_handoffs`。一端为 `null`（拿起/放下）不算交接。
  - 新增可选字段 `shot.prop_handoffs[]`（`{prop_id, to_holder_id, note}`）；示例与模板都补了该字段。
- **③ 不做**（写进 PROVENANCE 已知边界）：自由文字里的新增细节没标改编、可见性声明与 action 文字矛盾、prop_states 与叙事结局冲突、对白质量。**特别说明**：3a 提的"动作节拍 `character_ids`"是 **B 包**字段，A 包 `shots` 没有它，所以那条在 A 包不适用。
- **版本与文档**：0.5.4 同步 `declaration.json`、`PACKAGE_VERSION`、`CAPABILITY.md`、`PROVENANCE.md`（新增 0.5.4 节，含归类表、接受集合变化、已知边界）、`methods/workflow.md`（新增写法节）、`examples/capability-packages/README.md` 与两处测试断言。
- **验证**：能力包相关 9 个测试文件 **292 passed**（含新文件 10 个用例）；变异 4 个全部被抓；`leak_check.py` A 6/5 字均无命中；`resources/example-delivery.json` 与模板通过。命令与真实结果见 TESTS.md。
- **未验证**：真实模型重跑 A 类 9 例未做（等 3a 打包后跑）。

## A 包 0.5.3：来源与改编标注（pa53，2026-10-04，worker/pack-a-053，待复审）

- **来源**：A 包 0.5.2 重跑的业务审阅第三批，结构检查和宿主核验全过，但不合格集中在来源与改编标注：道具在原文里逐字有、却标成"新增/推断"；改编说明和它引用的原句对不上；新增的对白/字幕大面积没标 `adaptations`（6 个镜头只有 1 个有）。这些以前只靠作者自觉（0.5.1 的 PROVENANCE 里就写着"第 2 条漏标新增完全靠作者自觉"）。
- **改法**（只加通用字段规则，不写入任何特定任务的人物、道具、情节或原文片段）：
  - `prop_origin_marked_new_but_in_source`：道具 `origin.kind=adaptation` 时，若 `name` 逐字出现在某个原文段落里，报错并带 `found_in_source_ids`。退出：改成 `kind=source` 并引该段落，或确认不是同一件道具后改名。
  - `adaptation_unmarked`：镜头台词/字幕要能说清"是原文"还是"是新增"。合规路径任一：整句是某段原文逐字子串 / 被 `verbatim_source_id` 或 `embedded_quotes` 覆盖 / 本镜 `adaptations` 非空。三条都不满足才报错。
  - `adaptation_claim_contradicts_source`：说明里用了"原文未出现/原文没有/并非原文"这类固定短语，而它引用的原句（`original_quote`，或说明里成对引号括起来的内容）其实能在原文里逐字找到，就报错并给段落 ID。
  - 三条都带 ≤200 字 `hint`，都给了合法退出写法。
- **豁免设计（宁可漏报也不误伤）**：`adaptation_unmarked` 对四类字面形状放行——去掉标点空白后不足 3 字（极短语气词）、整条只有标点、整条是称呼/招呼（如"哥""老板"）、占位文本。不做相似度判断，避免放过真正的未标新增。另外只要本镜有任意一条非空 `adaptations`，本镜就不再逐句检查（查的是"有没有标"，不是"标得对不对"）。
- **版本与文档**：0.5.3 同步到 `declaration.json`、脚本 `PACKAGE_VERSION`、`CAPABILITY.md`、`PROVENANCE.md`（新增 0.5.3 节）、`methods/workflow.md`（新增方法节）、`examples/capability-packages/README.md`（表格、构建命令、A0.5.2/A0.5.3 修订段）与两处测试断言。
- **验证**：能力包相关 7 个测试文件 **274 passed**（含新文件 13 个用例）；变异 5 个全部被抓；`leak_check.py` A 6 字与 5 字**均无命中**；`resources/example-delivery.json` 未改即通过。命令与真实结果见 TESTS.md。
- **补一条通用守卫（pah）**：ds3 在 B 包 0.3.3 踩到"几条 hint 写成带 `{reference_id}` 的模板，但生成时只替换 `{path}`，占位符原样漏给模型"。A 包同属"每条错误可带 hint"的口径，已加 `test_capability_package_drama_text_hint_guard.py`：hint 清单**从脚本模块现取**（扫所有 `*_HINT` 常数 + 真实触发一遍走 `host_result` 投影），逐条断言不留 `{占位符}`、≤200 字、无控制字符；另自带一条"坏样本"用例证明守卫真的会红。见 TESTS.md。
- **未验证**：真实模型重跑 A 类 9 例未做（等 3a 安排）；本轮只做了组件级验证。
- **注意**：本轮由 ds7 实现，下一轮 A 的业务审阅不会派给同一人。

## TUI 状态刷新失败的客户端去抖（tuisync，2026-10-04，分支 `worker/tui-sync`，基于 17j 头 `f45e20dac`；ds10 初审，3a 终审，已并入 step17k）

- **起因**：用户反馈 TUI 时不时显示"状态刷新失败，显示上次状态；正在重试"、头部变"! 状态未同步"。3a 只读排查（`~/.my-agent/decision-evidence/final-report-1004/tui-refresh-fail-1004.md`）：`_background_notice_loop` 每轮 POST `/client/notices`、客户端只等 2 秒，**第一次失败就提示**；生产 Gateway CPU 采样 99% 超 100%（中位 138%），连最轻的 `/status` 60 次里 5 次超 2 秒 → 属于过载下的可见降级，不是数据错。
- **本次改动（客户端去抖）**：`tui_threading.py` 新增两个常量 `TUI_BACKGROUND_NOTICE_WARN_AFTER_FAILURE_COUNT=2`、`TUI_BACKGROUND_NOTICE_WARN_AFTER_SECONDS=3.0`，并把判定抽成模块级 `_NoticeWarnDebounce`；循环只在"连续失败 ≥2 次 **且** 距上次成功 >3 秒"时才发布 `ok=False`，成功立即清除。退避节奏（0.5 翻倍到 8 秒）与轮询节奏（1 秒/空闲 5 秒）**未改**。
- **用例**：`test_background_notice_display.py` 新增/更新 5 条——单次超时不提示、两个门槛都要满足、时间够但次数不够仍不报、达到门槛才报、恢复后立即清除；既有 `test_notice_loop_exposes_failed_refresh_without_changing_task` 的期望按去抖后的真实行为更新（第 1 轮失败不置位、第 2 轮才置位）。
- **变异**：V1 去掉次数门槛、V2 去掉时间门槛，**两个都 KILLED**（各自打掉一条门槛用例）。V1 首轮曾 SURVIVED，暴露出缺一条"时间够、次数不够"的用例，已补 `test_tui_notice_loop_needs_failure_count_even_when_slow` 后杀掉。重构后（见下）重跑仍 2/2 KILLED。
- **尺寸与常数目录**：去抖逻辑首版把 `_background_notice_loop` 撑出 2 条 soft 告警（function + nesting），已抽成模块级 `_NoticeWarnDebounce` 小类（`should_publish(snapshot_ok, now)`），size_diff 回到**新增 0**；新增常量按目录规则命名为 `TUI_BACKGROUND_NOTICE_WARN_AFTER_FAILURE_COUNT`（`_COUNT` 单位后缀）与 `TUI_BACKGROUND_NOTICE_WARN_AFTER_SECONDS`，并重新生成 `constants_catalog.json`（901 项，`--check` 一致）。
- **验证**：相关 6 文件 255 passed；guards9 187 passed；ruff / boundaries=0 / doc_sync(--base f45e20dac) / strict code-size(hard=0) / diff-check / clean-package 全过；size_diff 新增 0、消失 43。
- **服务端"无变化"快速路径**：按任务要求**只写方案、未实现**（方案在交接报告）。
- **未验证**：真实 TUI 端到端与真实 Gateway 下的效果（本树只有合成负载测量）；测量数字只进交接报告，不进仓库。

## cachecompact 初始阶段记录（2026-10-04，worker/cache-compact；历史状态，以本节上方 2026-10-05 跟进为准）

- **3a 挑入时核实（10-05）**：ds7 用两条独立构造路径做字节级比较，tool-loop 路径通过（压缩请求 = 主请求完整字节 + 末尾压缩指令，tools 与档位一致）。两处遗漏：① Gateway 前台压缩路径（cachesim 护栏 test_compaction_call_reuses_the_conversation_history_prefix）仍落在 (enabled, default) 分区，没带线程的 max 档位，护栏继续 strict xfail；② active-turn 路径不带之前的历史（provider_history 从空记录重放），第 1 条消息就分叉。已转作者 luna3 补，补完去掉护栏标记。

- **目标与范围**：只修压缩请求缓存面，不接 CAB 超时修复。显式压缩 purpose 按 thread 读取主请求 reasoning options；非压缩辅助任务不改。
- **单次可容纳请求**：使用同一 system/tools/历史和 `tool_choice=auto`。若结构化 `tool_use_blocks`/`tool_calls` 出现，丢弃且不执行，只追加一次无 tools/`none` 的摘要重试；保留同 thread thinking/reasoning_effort。
- **路径首个分叉点**：`conversation_compact_summary` 与 `compact_live_tool_summary`（tool-loop、active-turn）先复用 system/tools/历史；第一个新内容是尾部 user 压缩指令。`compact_carried_summary` 为文本协议，没有主请求 system/tools/历史，第一条 user 内容即不同。超窗分段先换摘要专用 system，再省 tools 并用 `none`，messages 只含局部区间；不声称完整主前缀命中。
- **验证**：payload fake transport 14 passed（含 media digest 目的及 tool-loop 线程上下文）；bounded 与 live 的 typed `CacheStructuredPrompt` 实际出站 payload 均断言 system/tools/历史相等、压缩指令只追加在最后 user 内容；active-turn 也断言传递线程 ID。全部 `test_*compact*.py`（74个文件）、`test_backends*.py` + `test_reasoning_effort.py`、guards9（11个文件）通过。删 tools/改 system/历史头部插入三种变异均被合同抓红且已还原。import boundaries=0、Ruff、DOC_SYNC、strict code-size、clean-package、diff-check全过；`size_diff.sh` 新增0、消失50。
- **实测边界与估算**：非作者只读初审未发现阻断问题；尚未实测修后DeepSeek缓存命中率或费用。若同模型/同thread分区且完整历史前缀仍驻留、逐字节命中，基于旧2.2% system-only命中线索估算理想输入token命中可接近98%，尾部指令/动态字段会降低比例。
- **协作注意**：本次改 `compact_request_budget.py` 的单次/分段分流；CAB ds5 合入时对照该文件，避免覆盖超时修复。

## B7 最低启动读取、链接链与同步 cwd（rdfloor2，2026-10-05，`worker/rdfloor2`，基线 `4a935d200`；已实现，stdin 补修待外部复跑与初审）

- 新反馈与补修：3a 沙箱外同 31 文件清单已确认 `af59b5c62` 修好原 allowlist/uv 启动及此前进程卡点，但分支新增两条绕过初始化的 stdin 替身 AttributeError。仅补测试所需完整 spec/平台，产品 run 不改；全仓指定构造检索仅一处。当前两条 attempt stdin 真进程/原断言通过，完整 stdin 仍两 foreground 5 秒超时，原六文件仍有本地进程失败；十一守卫 187 passed。详见 TESTS 顶部，新版本同 31 文件沙箱外复跑、非作者初审和生产链尚未验收。以下保留此前阶段记录，不把旧局部绿或失败覆盖为新版本完整结果。

- 按 3a 在 macOS 26.4 / Darwin 25.4 沙箱外的实测修规则：allowlist 仅补 literal `/` read-data；逐分量 lstat/readlink，最多 40 次解引用，所有系统/R/E/W 放行根的链接节点仅获 metadata literal，hide_home 共用。不写 uv 专用分支、不加配置/依赖/状态源。
- Linux 显式终端 symlink 原本会直接恢复到获准 realpath；但终端为普通文件、只有中间目录为链接时，旧 is_symlink 筛选漏入口。本片按原路径与 realpath 差异恢复授权根的精确入口，不挂未授权父目录/兄弟数据。argv 单测从真实基线转红再转绿，真实 bwrap 启动未验证。
- 首轮 WIP `dbaf575ed` 后，3a 报告外部六文件 129 passed、16 skipped、1 failed，启动不再 SIGABRT/execvp EPERM；余下 import/getcwd PermissionError 来自同步 Popen 继承白名单外 cwd。这是旧版本外部记录，不替代续修后的复验。
- 将插件原目录根覆盖判定移动到 `tooling/sandbox.py::allowlisted_directory` 唯一实现；插件工厂与同步 run 共用。Darwin 显式 `Popen(cwd=spec.attempt_view)`，Linux 保留命名空间 `--chdir`。allowlist 未覆盖时在 Popen 前以 `RESTRICTED_CWD_NOT_ALLOWLISTED` 拒绝，无继承宿主 cwd、扩读写根或降宽回退。四字段 `SandboxProcessOptions` 只传参数，不新增执行链。
- 新 cwd 八项先得五项有效红、再全绿；最终核心/参数及取消前零启动、正常 stdin/callback 用例 20 passed、1 skipped。新增两单点变异去 cwd 传递/去拒绝分别三/两项行为失败、errors=0，原样还原；首轮三项未改规则的有效变异记录继续保留。十一适用守卫 187 passed，基线缺第十二文件，按 17k 续做通知不跨版补入。
- 五文件逐文件限时已执行，但不全绿：attempt 三项失败、v8 两项失败，真实 Seatbelt 返回 71/sandbox_apply EPERM；任务真实基线同五项失败。shell 边界文件 150 秒超时，卡在两个真实用例的 `_communicate_process`，基线同点同超时；不删测试或加 skip 压绿，旧 600 秒没有日志的结果不冒充通过。
- 详见 [PLUGIN_PROCESS_SANDBOX.md](docs/design/PLUGIN_PROCESS_SANDBOX.md) 与 TESTS 顶部。实现与局部验证可交审，完整真实隔离、插件及生产 Gateway 未验证；本次续做超原约 90 分钟窗口，不以局部绿或正式提交代替外部验收。

## B7 读模式与有限读底图（rdfloor，2026-10-05，`worker/rdfloor`，基于 step17k `602d276e7`；已实现，待 3a 终审和沙箱外真进程验收）

- **问题**：B7 当前 `hide_home` 形态是整根只读后隐藏 Gateway 用户家目录、再开放有限根；老格式 restricted 策略需要的是“只可读列出的根”，不能靠隐藏单个 HOME 冒充全盘白名单。
- **公共合同**：`PluginRestrictedSandbox.read_mode` 只有 `hide_home` 与 `allowlist` 两种结构化值，默认 `hide_home`；v8 显式映射 `hide_home`。allowlist 不因 cwd、owner_home 或执行根而隐式扩大读权限，cwd 必须被系统、显式读根或写根覆盖。
- **平台实现**：Seatbelt 先拒绝所有文件读取，再逐根放行平台系统根、宿主授权读根和写根；父目录只获 `file-read-metadata`，不获列目录权限。Linux bwrap 从空 tmpfs 根创建最小目录骨架、挂载系统与授权只读根、只挂明确写根为可写，最后锁只读底图；不再 `--ro-bind / /`。R/W/E 符号链接仍挂真实目标并恢复经核验 alias，隐藏路径的最终拒绝保持。
- **系统根边界**：Linux 与 Darwin 分别使用结构化目录表，并对绝对路径、realpath 和 `/`、`/Users`、`/home`、`/private` 及其别名做失败关闭校验；`/dev`、`/proc` 与 `/tmp` 是隔离器的特殊空/伪文件系统入口，不作为宿主目录白名单挂载。
- **兼容与状态**：v8 `hide_home` 的 Seatbelt profile 与 bwrap argv 以改前快照钉住；本地已补有限读、系统根、祖先 metadata 与 symlink 执行根覆盖。真实 Seatbelt 与 Linux bwrap 进程由 3a 沙箱外车道验收后，才能判定完整隔离链成立；结果见 `TESTS.md` rdfloor 小节。
- **初审修正**：读模式在跨平台 `build_argv` 分派前统一校验，未知值在 Linux/macOS 都以 `SANDBOX_UNAVAILABLE: RESTRICTED_READ_MODE_INVALID` 结构化拒绝，不再由 macOS 静默降级到 `hide_home`。ds4 补件 `11d914773` 已摘入；系统读根宽度、symlink alias 跳过分支均有用例，祖先目录读测试直接断言不能列目录。

## B7 受限策略的符号链接执行根（b7lnx，2026-10-04，`worker/b7lnx`，基于 `c544b358d`；已并入 step17k，3a 沙箱外 macOS 强制模式复跑通过，Linux 真进程用例由 17k 车道验证）

- **问题**：B7 的统一受限策略已保留 R/W/E 根的 alias 与 realpath，但 bwrap 将 alias 也作为 bind destination；执行根若是 `/usr/local/bin/python` 这类符号链接，bubblewrap 会拒绝挂到 symlink destination，导致子进程无输出且无法启动。
- **改动**：沿用收窄读的 alias 处理入口；Linux 只挂载已核验的 realpath，再用 `--symlink <realpath> <alias>` 恢复授权的读、写、执行根入口。Seatbelt 以精确路径规则同时允许 alias 与目标；写授权仍只对应解析后的已核验写根，不放宽到父目录。候选预检、业务连接、面板连接仍共用 `plugin_sandbox_spec` / `AttemptSandboxSpec`，G5 Landlock 端口拒绝未改。
- **验证状态**：读/写/执行 alias argv 与受限策略 Seatbelt/spec 聚焦用例通过；移除 alias 恢复调用的变异被 argv 用例抓住，恢复后 SHA-256 一致且同用例通过。三文件相关测试集本机退出 1（环境观察与门禁见 `TESTS.md`）；guards9 与规定静态门禁通过。Linux 真解释器 symlink 启动/读包用例已补，但本机 Darwin 跳过，必须由 3a Linux lane 实跑。

## B5×B7 接线：总开关收口 + `/plugins info` 原因 + 收紧预算管理员可改（b5b7wire，2026-10-04，分支 `worker/b5b7wire`，基于 17j 头 `662439745`；ds4 初审无必须改，3a 终审通过并补用例，已并入 step17k）

- **终审补件（3a）**：ds4 小问题 1“总开关关着 + 宿主 deny 无用例”。补 `test_host_deny_never_relaxed_with_or_without_reviews`：宿主 deny 在无征询、插件放行、插件 ask 三种情况下都保持 deny。首版只测放行时，删掉 merge 里 deny 短路的变异存活，加上插件 ask 后变异被杀。小问题 2（`is not True` 与 `is False` 当前等价，无法区分）记为已知，现写法是 fail-closed，不改。

- **裁定落地**：设计稿 267/337 行——总开关 `plugin_events_enabled` 关掉后，已启用的 v8 插件"事件不投、收紧不问"。B4 的事件点已先判，B5 这次补上。
- **只在装配点判一次**（`core._build_plugin_gate_reviewer`）：这是唯一拿得到宿主 config 的权威位置，且与 B7 启用闸同一时机，语义与 yaml"修改后重启 Gateway 生效"一致。关掉时 reviewer 为 None，执行器照宿主原裁决走——**少一层收紧，不是变松**（收紧层只会把 allow 变严，从不放行）。
- **`/plugins info`**：关着时"事件订阅"与"收紧工具"两段写结构化原因（复用既有原因码 `plugin_events_disabled`，不新造近义码）；TUI 与飞书走同一渲染函数，文字一致。
- **收紧预算进管理员白名单**：`plugin_tool_gate_timeout_ms` 加进 `USER_SETTINGS_BOUNDARY_KEYS`——管理员可改（调大只会多等，超时仍 ask），模型 set/reset/revert 仍按边界拒。前端目录重新生成（260 字段，`--check` 通过）。
- **已知边界（B7）**：v8 沙箱隐藏的是 `Path.home()`（整个家目录），**家目录以外的公共目录（如 `/tmp`）对插件可读**。要"补全被截断的参数"仍不现实，但用户放在公共同步目录的敏感文件插件读得到——记在这里，施工和评审都按这条理解。
- **预热协同（B4/B5）**：两者共用 B2 池，插件进程懒启动 + 常驻（空闲 120 秒关），冷启动多发生在"本回合第一次工具调用"或"空闲 120 秒后的第一次"；写进设计稿 16.1。
- **验证**：见 TESTS「B5×B7 接线」；变异 3/3 被杀（装配点不判开关、info 不显示原因、参数白名单退回）。

## 模拟器补 tool_choice 两条规则 + 压缩用例最终口径（cachesim2，2026-10-04，分支 `worker/cache-sim`，基于 `6e31e5855`；3a 复审通过，已并入 step17k）

- **来源**：3a 追加实测（`ds_probe4/5.out.txt`）：① 开思考时 `tool_choice=none` 被接受，`required` 和指定工具回 400（“Thinking mode does not support this tool_choice”）；② `tool_choice=none` 时服务端不渲染工具定义，prompt token 等于“不带 tools”（4029 对 4029，auto 是 4430），所以 none 的前缀在 system 之后就和 auto 请求分叉，长历史命中 0；压缩或辅助请求要复用主对话缓存，必须同分区 + 同一套 tools + auto + system 和历史逐条相同，压缩指令放最后一条 user。
- **模拟器**（`cache_prefix_simulator.py`）：新增 `unsupported_tool_choice`（思考模式只认 auto/none）与 `renders_tools`（none 时不把 tools 放进前缀序列），`prefix_units` 与 `serve` 按它们分支；两种 400 各带对应官网原文。顺带把 `wire` 的探针分支从纯文本改成带同一 nonce 的结构化工具调用（原来按文档直接 `monkeypatch.setattr(http, "post_json", sim.wire)` 用时探针会判“不支持原生工具”而跑不起来；新增单测钉住）。
- **用例**：新增 `test_cache_prefix_simulator.py`（4 条纯单测：tool_choice 400、none 前缀等于不带 tools、400 只在思考模式生效、探针响应形状）。压缩那条 strict xfail 改成新口径 `test_compaction_call_reuses_the_conversation_history_prefix`：用调用前后记录差识别压缩调用，断言它与主对话**同分区**、**命中量 ≥ 主对话历史前缀的 90%**（luna3 的 `worker/cache-compact` 修好会 XPASS，届时去掉标记）。现状对照实测：压缩请求落在 `(model, enabled, default)`、不带历史（prompt 46 字符）、命中 0。
- **变异**：① 去掉“none 不渲染 tools”→ `test_none_tool_choice_prefix_equals_a_request_without_tools` 变红；② 去掉 400 规则 → `test_thinking_mode_rejects_required_and_named_tool_choice` 变红。两个都按字节还原。
- **验证**：见 `TESTS.md`“模拟器 tool_choice 规则与压缩用例最终口径（cachesim2）”小节。
- **未验证 / 未做**：真实 DeepSeek 请求上的端到端读数（要联网与密钥）；压缩修复合入前，压缩用例按已知未修保持 xfail。

## 缓存修复回归护栏：模拟 DeepSeek 前缀缓存（cachesim，2026-10-04，分支 `worker/cache-sim`，基于 17j 头 `6e31e5855`；3a 复审通过，已并入 step17k）

- **来源**：用户要求“保证后续 API 价格能降下来”，修好缓存（sol1 的思考开关、luna3 的压缩档位）以后不能悄悄回退。3a 官网实测（`decision-evidence/deepseek-cache-audit-1004/server_probe_facts.md`）：缓存按 `(model, thinking 开关, reasoning_effort)` 分区，换任何一个整段不命中；思考模式下最后一条 user 之后的 assistant 缺 `reasoning_content` 会被 400。
- **做法**：新增**测试辅助** `agent_py_agent/tests/cache_prefix_simulator.py`（前缀缓存模拟器 + 假传输层）与端到端用例 `agent_py_agent/tests/test_cache_prefix_regression.py`。只替换 `backends.http.post_json`，其余走真实代码：真 SimpleAgent、真 Gateway 前台回合（`_run_gateway_ask`）、真 openai_compatible 组包、真工具循环。**不改任何产品代码**。
- **模拟规则（全部来自实测，不连网）**：分区键 = `(model, thinking 分区, reasoning_effort 分区)`，其中“不带档位 / high / 显式开思考”同区，`low`、`max`、`disabled` 各自独立；命中量 = 同分区里以前请求的最长公共前缀（按 system → tools → messages 逐条比较，字符数当 token 近似值）；思考模式下最后一条 user 之后的 assistant 缺 `reasoning_content` → 回 400。
- **场景**：三轮真实对话，每轮一次工具调用；第二轮最终答复**故意不带思考内容**（官网实测这种答复合法，但旧实现会因此把之后所有请求关思考）；中间插一条用户消息；线程档位 `max`（与生产一致）；端点用真实 `api.deepseek.com` 主机名——思考开关只对已核对端点生效，换别的地址会绕过要护栏的那条分支。
- **断言与现状**：跨轮首调命中 ≥ **90%**（实测 0.956–0.967；阈值理由见用例注释）；整个场景只有一个缓存分区；没有请求被判 400；**历史里确实累积着缺思考的旧答复时，请求不得被切到 `disabled` 分区**。压缩类调用一条按现有事实标 **strict xfail（原因写 luna3 的 `worker/cache-compact`）**；该用例已在 cachesim2 里改成“同分区 + 命中量 ≥ 主对话历史前缀 90%”的最终口径，修好会 XPASS 失败提醒去掉标记。
- **变异**：把 `_thinking_mode_supported` 退回“查全部历史”，护栏用例变红并明确指出第 2–7 次调用被切到关思考分区；原文件按字节还原。
- **验证**：见 `TESTS.md`“缓存修复回归护栏（cachesim）”小节。
- **未验证 / 未做**：真实 DeepSeek 请求上的端到端读数（要联网与密钥）；跨轮历史改写（sol1/ds7）没有独立的 xfail 用例——本轮只覆盖了思考开关这一条已知未修；压缩档位修复合入后需去掉 xfail 并复跑。

## opp 变基到 17j（opp5a2，2026-10-05，WIP，B7 未接，整系列不可挑入）

- **变基**：`worker/opp5` 5 个提交逐个重放到 17j 头 `da6e38093`（重放头 `48bcec9f3`→`5561c9598`→`e4b0ff6f8`→`39238c85f`→`4a8cbb10d`）。合并按 sol1 方案第五节：保留 17j 的 `PluginEnablePolicy` 构造入口与 v8 判定，不让 opp 覆盖 B7 前置门；老格式 v1–v6 授权链作受控分流（policy 带可选授权输入，工具内组装 `LegacyEnableContext`）；按包类型显式三分支（v8 / 内容包 / 老格式），不把“不是 v8”当老格式；`self.plan` 只留一个，刷新时 runtime/target/plan/grant 同组更新。
- **冲突解法**：`plugin_enable_tool.py` 按上述分流重写合并；`plugin_management.py` 保留 17j 的 policy 构造点、老格式输入进 policy；`test_plugin_manifest_v8.py` 的 v8 用例继续走 `PluginEnablePolicy(...)`、v6 用例走真实管理链取完整确认命令与授权身份；DESIGN_LEDGER/LLM_GUIDE/TESTS/02-progress 顶部两段都保留。
- **两条守卫用例（ds4 opp4r 小问题 1、2）**：撤旧换操作号 → 同版本号不同事实必须 `revision_conflict`、零发布（钉 `retire_for_enable` 后的第二道 `entry != target` 守卫）；同身份、新事实下旧确认码必须重新预览、零执行。
- **小修**：夹具 `preview_enable`/`confirm_enable` 改名 `preview_legacy_enable`/`confirm_legacy_enable`，名字写明只给老格式真实链，防 v6/内容包用例误用。
- **尺寸拆平**：变基引入 3 条新增告警——抽 `_preflight_failure`/`_sandbox_problem`/`_legacy_context`/`_retire_before_enable`/`_needs_verifier_consent` 到模块级、`execute` 的 `elif` 链扁平化；`size_diff` 回到 **新增 0 / 消失 44**，strict code-size `hard=0 blocked=False`。
- **验证与边界**：opp 17 文件失败集合与变基点 `4a8cbb10d` 逐条一致（13 条既有沙箱失败）；drift 34 全绿；guards9 188 passed；3 个独立变异全 KILLED。真实链/OS/渠道、B7 第四段与本段非作者/9b 终审仍待；整系列仍不可单独部署。

## 沙箱内按能力跳过、沙箱外强制真跑（capsk + capsk2，2026-10-04，分支 `worker/sandbox-cap-skips-v2`；3a 沙箱外默认、强制两种模式复跑验收，已并入 step17j）

- **问题**：共享测试设施的能力门曾在收集阶段后加 `usefixtures`，未进入已收集用例的 fixture 列表，导致强制失败不可靠；模块级标记还会跳过无关模拟测试；Seatbelt 用例缺少 macOS 平台条件；`ps` 只看返回码会把空/错误 PID 输出误判为可用。
- **改法**：`_probe_ps` 用 `ps -o pid= -p <pid>`，必须返回码为 0 且去空白输出精确等于本进程 PID。能力门与既有桌面守卫共用唯一 `pytest_runtest_setup` 包装钩子：先进入内层 hook 让 `skipif` 处理平台条件，再读取 `sandbox_capability` marker；缺能力默认 `pytest.skip`，强制模式直接 `pytest.fail`。原因码 `SANDBOX_CAPABILITY_MISSING[能力名]` 与 `user_properties.sandbox_capability_missing` 同时保留缺项，不解析说明文案。
- **范围**：六个文件移除模块级标记，按无标记实测只标真正需要后台身份/Seatbelt 的 test function；macOS Seatbelt 用例保留 `skipif(not IS_MACOS)`，LAN case 还有非回环地址前置 skip。POSIX PTY 资源控制只在 Darwin 标记 nested Seatbelt，Linux 路径不因该能力恒不可用而误报。
- **验证**：7 个直接相关文件共 254 个 test case。默认模式 196 passed、58 skipped，其中能力缺失 57（nested 11、后台身份 46），另 1 条无 LAN 地址 skip；强制模式 196 passed、57 个 setup error、1 skip。JUnit XML 显示 57 errors、0 failures，全部错误都含能力门原因码且缺项 user property 齐全。拆除宽泛标记的实跑表和命令见 `TESTS.md`。
- **未验证**：当前宿主缺少上述能力，不能据本地结果判断能力齐全环境、沙箱外 macOS 或 Linux lane 的实际业务路径；由 3a 按强制模式复跑后确认。

## 补上 `POST /stop` 用例的前置条件（skipfix2，2026-10-04，分支 `worker/skipfix`，接在 `65366d700` 之后；3a 复审通过，已并入 step17j）

- **背景**：skipfix 收窄宽 except 后暴露出 `test_stop_endpoint` 一直吃 `HTTP Error 409`。**这是测试缺前置条件，不是产品问题**：`MockGatewayPaths` 从不登记 gateway 进程身份，`handle_stop` 拿不到身份就按设计回 409（`http_handlers.py` 的 `POST /stop`：先 `get_running_pid`，再 `write_targeted_gateway_stop_request`，拿不到身份回 `409 gateway process identity unavailable`）。
- **改法**：只改测试，**产品代码一行未改**。用例改用产品自己的写 pid 入口 `write_pid_record(mock_paths.pid)` 登记身份，再断言 200 + `{"status": "stopping"}` + 停止请求文件写在 `mock_paths.stop_request` 且 `target_process.pid` 等于登记进程；另加一条对照用例钉住"没有身份时回 409 且不写文件"这一有意的 fail-closed 行为。
- **安全核实（写之前先核）**：`POST /stop` **不发信号**——`handle_stop` 只写"定向停止请求"JSON 文件，`write_targeted_gateway_stop_request` 体内无 `os.kill`/`signal.`；**唯一会消费该文件的进程是 supervisor**（`gateway_parts/supervisor.py`），而 `run_supervisor` 只能从 `cli/supervisor.py` 进入；**测试路径不起 supervisor**——`GatewayHTTPServer.start()` 只起 HTTP serve 线程，`test_gateway_http.py` 里也没有任何 supervisor 引用。因此这个文件写完没有监视循环去读，不会给 pytest 或任何真实进程发信号，**不需要**替换型替身。
- **验证**：`test_gateway_http.py` 整文件 **24 passed、0 failed、0 skipped**（skipfix 时是 1 failed）；guards9 全过；ruff / import boundaries=0 / DOC_SYNC_PASS / `git diff --check` / strict code-size hard=0 / size_diff 新增 0 / clean-package 全过。变异两个（不写停止请求直接回 200、把 409 改成 200）**全部 KILLED**。
- **未验证**：真实 Gateway 进程的端到端停机（规则禁止连真实 Gateway），本用例只覆盖到"写停止请求文件"这一段。详见 TESTS。

## 测试卫生：收窄"环境不可用才跳过"的宽 except（skipfix，2026-10-04，分支 `worker/skipfix`，基于 17j 头 `1e1aadb80`；3a 复审通过，已并入 step17j）

- **来源**：9b 终审顺带指出：有些用例用 `except Exception: pytest.skip(...)` 包住整段，**断言失败也会被显示成"跳过"**，把真实失败藏起来；`test_gateway_http` 那条 409 的跳过就是这种。
- **改法**：只动测试，10 个文件 16 处宽 except 收窄成"环境真的不可用"的精确类型（连接类 errno 白名单 / SQLAlchemy `OperationalError`+`InterfaceError` / `SandboxUnavailableError`）。**不新增 skip、不放宽断言、不改用例原意**；跳过的原因词（"无可用 PostgreSQL""HTTP server not reachable"）与原来一致，只是判定变严。
- **为什么不用一个共享 helper**：`OSError` 覆盖面太宽（含 ENOSPC/EIO 这类与"连不上"无关的失败），把判定集中成"凡是 OSError 都算"等于换个方式继续藏问题。所以 gateway 两文件按 **errno 白名单**逐条列出可跳的 errno，PG 组用驱动层的连接类包装异常。
- **收窄暴露出的真失败（1 条，只报告不修）**：`test_gateway_http.py::test_stop_endpoint` 实际拿到 `HTTP Error 409: Conflict`。**既有失败**——基线 `1e1aadb80` 上同一条的跳过原因原文就是 `HTTP Error 409: Conflict`，同一个 409 一直在，只是被宽 except 显示成"跳过"。根因在 `gateway_parts/http_handlers.py:1827`：`POST /stop` 拿不到 gateway 进程身份时返回 `409 gateway process identity unavailable`，而测试的 `MockGatewayPaths` 不提供 PID 文件。修它要补测试前置条件（产品或测试改动），不在本任务范围。
- **未改**：`test_tool_operation_managed_gate.py` 的 `except OSError`（只包 `os.symlink`、try 内无断言）本来就对；`test_gateway_http.py:458` 那个 `except Exception` 只是收集错误进 list，不 skip，也不动。
- **验证**：改进文件 90 passed / 1 failed（即上面那条 409）；PG 组 21 passed（本机有 PG，真跑）；guards9 全过；ruff / import boundaries=0 / DOC_SYNC_PASS / `git diff --check` / strict code-size hard=0 / size_diff 新增 0 / clean-package 全过。变异：在 try 内注入 `assert False`，收窄后会报 `AssertionError`，换回旧宽 except 则显示 `SKIPPED`——证明收窄真的堵住了"吞断言"。详见 TESTS。

## G2b 拒绝路径的结构化类别判据（g2bfix2b，2026-10-04，分支 `worker/g2b-denial-client-fix`，基于 `3e698eb88`；9b 终审通过，已并入 step17j）

- **9b 终审必须改（3a 挑入时修）**：飞书 `/ask` 被拒时 `_submit_gateway_payload` 调了两次 `_gateway_http_error_code(exc)`，响应体只能读一次，第二次读空，用户看到通用文案而不是“本机凭据无效或缺失”。改为先读一次存下原因码，码和文案共用；用例断言改为专用文案，并补“无码 401 走通用文案”的对照。9b 的进度游标落盘探针转正为 `test_progress_cursor_commit_failure_once_still_delivers_final`，钉住 g2bfix2b 新捕获面（底版异常会逃出 run_once）。
- **9b 建议、未在本次处理**：TUI 不带码的 401/403 走 on_rejected 并把同一条消息重排（应加结构化鉴权拒绝标志分流）；投递 worker `_run` 无兜底，意外异常会让线程静默退出；`/progress` 对不存在 id 带有效凭据也回无码 403。三条另开小任务，不挡 G2b。

- **起因**：g2bfix2 的 auth 可见回复判据是**原因字符串正则**（`_AUTH_DENIAL_REASON = r"_auth_http_(?:401|403)\b"`），漏掉了 `/input-status`、`/result` 的**带码 404**——这两个端点在 G2b 下正是靠 404 报"没带凭据"，结果仍会静默丢消息（g2bfix2r 初审实测：`gateway_result_auth_http_404` → 用户可见回复 False，且当时无任何用例区分 404 的静默与可见）。
- **改法（AGENTS.md：机器判断只用结构化事实）**：`GatewayReplyQuarantineError` 增加**类别字段 `category`**，由 `manager._gateway_poll_quarantine_category(status_code, denial_code)` 判定一次——401/403 一律 `auth`；404 只有带 `LOCAL_CREDENTIAL_REQUIRED` 才算 `auth`，真"记录不存在"仍是 `config`。`delivery._terminalize_quarantine` 只读这个字段决定"是否给用户一句可见原因"，不再做任何原因字符串匹配（`_AUTH_DENIAL_REASON` 正则与只供展示的 `is_auth_denial` 派生属性一并删除，判据只留一处）。原因串仍由同一函数生成，保证类别与原因同源。
- **顺带收口**：`_quarantine_aware` 的异常日志恢复分阶段文案（`gateway progress poll failed` / `gateway response poll failed`）；`_deliver_available_progress` 经该 helper 包裹后新增的捕获面（`commit_claim` 抛错 → release_claim 重试）在有意的位置写明注释并记入 TESTS。
- **验证**：见 TESTS「G2b 拒绝路径的结构化类别判据」；指定变异两个都杀（去掉 category 判断退回字符串只认 401/403 → 带码 404 用例红；不带码 404 也当 auth → 对照用例红）。
- **未验证**：真实飞书/TUI 端到端与真实 Gateway 的 403/404 响应仍由 9b/3a 在沙箱外复核；本工作树覆盖到解码层、轮询入口、ingress 提交与 outbox 状态机。

## G2b 拒绝路径的客户端收口（g2bfix2，2026-10-04，分支 `worker/g2b-denial-client`，基于 17j 头 `35b1647e8`；初审 g2bfix2r 判"必须改"1 条，已由 g2bfix2b 修复）

- **起因**：G2b 强制打开后，客户端收到 401/403 的处理与 G3"凭据读不到"不一致——飞书轮询把 401/403 当不可恢复隔离、只写终态不发正文（用户一句话都收不到）；飞书 `/ask` 提交的 401/403 落进无上限退避重试；TUI 插话把 403 当 UNKNOWN 一直退避重排。ds5 只读分析（`~/.my-agent/decision-evidence/g2b-adapter-denial/g2bad-ds5.md`）逐条坐实。
- **服务端约定**（g2bfix1，`0d838e585`）：强制档 + 回环 + 没带凭据时响应体带 `error_code=LOCAL_CREDENTIAL_REQUIRED`（挂闸端点 403、`/progress` 403、`/input-status` 与 `/result` **404**）；回环带错或过期凭据同样带码，远程不可信来源、来源未知不带（9b 终审口径，3a 挑入时更正）。
- **改法**：① 飞书轮询——auth 类隔离（401/403，或带该码的 404）先给用户一条可见失败原因再写 quarantined 终态，其它隔离保持静默；② `/ask` 提交——401/403 收口成 G3 同款 `credential_error`，不再无上限重试；③ TUI——401/403 与带码 404 按 REJECTED 处理并提示重启，持久 outbox 条目收终态、重启后不重发。判断只用状态码 + `error_code` 字段。
- **验证**：见 TESTS「G2b 拒绝路径的客户端收口」；变异 8 个全杀（401/403 放回重试、去掉可见回复、/ask 继续重试、TUI 退回 UNKNOWN、鉴权拒绝不转终态、带码 404 退回等待、带码 404 归 config 类、TUI 不认带码 404）。
- **未验证**：真实飞书/TUI 端到端与真实 Gateway 拒绝由 3a 在沙箱外复核；本工作树只覆盖到解码层、轮询入口、ingress 提交与 outbox 状态机。

## DeepSeek 跨轮思考分区复核（cachecmp，2026-10-04，worker/cache-prefix；规则复核通过；本段测试与文档已并入 step17j，其余排查 WIP）

- **来源与变更归属**：按3a最新官网实测，只查最终出站最后一条 `user` 后的所有 `assistant`，纯文本同样必须有非空思考。非作者复核 `a2f7aca08`，本树 cherry-pick 为 `34810dc5f`；产品文件逐字节相同，本线没有再改 `_thinking_mode_supported`，新增离线载荷合同与记录。此前只查工具 assistant 的未提交方案已撤回，不是交付实现。
- **第一个已确认分叉**：旧轮最终答复无思考，使旧 `_thinking_mode_supported` 对整段历史判否，出站新增 `thinking: disabled` 并去掉 `reasoning_effort: max`；不是 JSON 键顺序问题。该路径足以改变3a实测的缓存分区，但没有据此断言所有约16万token未命中都由它造成。
- **离线证据**：真实 OpenAI builder 假传输捕获流/非流、开/关思考的相邻调用；固定历史逐条、system、tools及工具参数保持相等，无思考最终答复追加后再追加下一轮user，开思考与max不丢。本轮最后user之后工具/纯文本缺思考仍禁用，避免400；不伪造reasoning。`UserTurn`（插话）、`RuntimeFactsTurn`、`CompactionSummary` 最终均为user；纯tool_result的中间user容器最终转tool，不重置边界。
- **扫描仍有明确例外**：真实 `_do_backend_generate` 把任何非auto的工具选择视为forced，none/required/specific均关思考且不带max，回到auto又恢复max；主/子线程两条路径均离线复现。因此不能写“全线程相邻分区始终一致”。普通无工具/auto续跑按现读线程档位；档位读失败/空值仍回全局默认。原生工具能力探针当前auto且不传线程档位，属于独立探针身份，不能直接认定它造成主对话断缓存。
- **边界与分工**：本轮捕获是合成历史经真实builder/请求入口，不是实际N轮落盘后N+1轮宿主重新加载全链。历史重放/存储改动归ds7，压缩请求档位及前缀归luna3，本线未修改两路。其它强制策略是否取消none的关闭、完整自然答复/续跑宿主链需后续裁定/复核，不用大重构冒充小修。
- **3a 官网补测（10-04，合成内容，见 decision-evidence/deepseek-cache-audit-1004/ds_probe4、ds_probe5）**：开思考 + max 时 tool_choice=none 被接受，required/指定工具回 400（Thinking mode does not support this tool_choice），所以 required/specific 继续按 forced 关思考是对的。但 none 时服务端不渲染工具定义（prompt token 等于不带 tools），前缀在 system 之后就和 auto 请求分叉，长历史命中 0；所以“none 不再关思考”也换不回主对话缓存。压缩/辅助请求要复用主对话缓存，必须同一套 tools + auto + 同档思考 + 逐字节相同的 system 与历史，压缩指令放最后一条 user（已转 luna3 cachecompact 按此收口）。
- **条件估算而非实测**：若总输入354000、仅尾部追加2000 token，历史前缀及思考/档位分区均保持且服务端缓存有效，则历史全命中对应99.44%；受影响的跨轮开头至多避免约16万输入token按未命中价重算。费用差额为 `160000 × (未命中单价－命中单价) / 1000000`，未取得本轮单价/真实usage，不报实测金额或整体新命中率。详见 [智能程度第12节](docs/design/REASONING_EFFORT.md#12-deepseek-跨轮思考分区复核2026-10-04cachecmp)。

## G2b 读路径补最后一处：归档读不出的 403 也带缺凭据码（g2bfix1c，2026-10-04，worker/g2b-denial-server-fix2，基于 g2bfix1b 头 `e8c4b6d2d`；9b 终审通过，已并入 step17j）

- **来源**：g2bfix1br 初审做全面 grep 时查出——`_send_archived_terminal_result` 的 `load_error` 分支（归档文件存在但读不出）仍是裸 `_send_json(status, body)`。它与已修的三处同在"三个读端点降匿名后自行拒绝"这条路径上，普通用户拿 403 却不带码，客户端在"归档坏掉"时仍只能靠状态码猜。
- **改动**：`agent/gateway_parts/http_handlers.py` 一处——`handler._send_json(status, body)` → `handler._send_json(status, _denial_body(handler, body))`；`_denial_body` 的 LLM 注释同步补成"这三个端点每一处自判拒绝（含归档读不出）都必须走本函数"。状态码、`error`、`request_id` 与管理员侧 500 + `result_load_error` 全都不变。
- **为什么不影响管理员侧**：`_denial_body` 只在"强制档 + 回环 + 没带凭据"为真，而这一格普通用户（`_all_user_access` 为假）恒走 403，管理员走 500，加码分支不可能命中管理员。
- **覆盖**：新用例 2 条（23 passed）；变异 1 个（退回裸 `_send_json`）被抓。详见 TESTS.md 同名节。
- **未验证**：真实 Gateway 端到端（沙箱内不起 Gateway）；客户端消费仍归 ds10。

## G2b 读路径补齐：/result 自判 403 也带缺凭据码（g2bfix1b，2026-10-04，worker/g2b-denial-server-fix，基于 g2bfix1 头 `0d838e585`；9b 终审通过，已并入 step17j）

- **来源**：g2bfix1r 初审结论"必须改"——`_send_pending_state` 的两个 403（记录损坏、记录属于别人）与 `_send_archived_terminal_result` 的 403 仍是手写裸 403，没走 `_denial_body`。它们和 `/progress` 的 403 同在"三个自判读端点降匿名后自行拒绝"这条路径上，导致同一端点"记录不存在"带码、"记录坏掉/是别人的"不带码，客户端仍只能靠状态码猜（正是本次修复要解决的事）。
- **改动**：`agent/gateway_parts/http_handlers.py` 三处 `handler._send_json(403, {...})` 改为 `handler._send_json(403, _denial_body(handler, {...}))`——`_send_archived_terminal_result` 的 `_can_read_payload` 失败分支、`_send_pending_state` 的损坏分支与非本人分支。**不改任何拒绝语义**：状态码、`error`、`request_id` 原样保留，只在"强制档 + 回环 + 没带凭据"这一格追加 `error_code`。`_denial_body` 的 LLM 注释补写"这三处必须走本函数"的约束。
- **不改的一处**：`handle_control_status` 的 `_can_read_control_operation` 失败 403 保持裸返回——该端点前置已挂 `require_trusted_source`，没带凭据的请求到不了这一支（已核对实现）。
- **覆盖**：新用例 5 条（21 passed）；三个变异全红（两处 pending 403 退回裸、归档 403 退回裸、`_denial_body` 去掉判据）。详见 TESTS.md 同名节。
- **未验证**：真实 Gateway 端到端（沙箱内不起 Gateway）；客户端消费归 ds10。

## G2b 本机凭据缺失的结构化拒绝码（g2bfix1，2026-10-04，worker/g2b-denial-server，基于 17j 头 `35b1647e8`；9b 终审通过，已并入 step17j；终审更正“回环带错或过期凭据同样带码”的注释与文档措辞）

- **来源**：只读分析 g2bad 指出——G2b（`gateway_require_local_credential`）打开后，回环请求没带凭据被拒时只回 `403 {"error":"forbidden","message":"untrusted source:..."}`，客户端光看状态码分不清"本机客户端没换代码/没带凭据"（重启即可）和"远程来源真的越权"。设计稿 `docs/design/GATEWAY_LOCAL_TRUST.md` 1.2 节（9b 建议）原计划就要这个结构化码，一直没有落地。
- **先坐实三个自判端点**（3a 要求，用真实 HTTP handler + 随机端口实测，写入用例）：强制档 + 无凭据时，`/progress/{id}` 返回 **403**、`/input-status/{id}` 与 `/result/{id}` 返回 **404**（记录不存在时先于身份判定返回）。这三个端点**不挂** `require_trusted_source`，是先把身份降为匿名、再由端点自己按 `_can_read_*` 拒绝——所以它们和挂闸端点是两条不同路径，要分别处理。
- **实施**：
  - `agent/auth/middleware.py`：新增模块级 `LOCAL_CREDENTIAL_REQUIRED = "LOCAL_CREDENTIAL_REQUIRED"`；新增 `AuthMiddleware.needs_local_credential(peer_ip, headers)`——只有"强制档 + 回环来源 + 没带有效凭据"这一格返回 True。`require_trusted_source` 在该格给 403 响应体补 `error_code`，**状态码与既有 error/message 原样保留**，只追加字段。
  - `agent/gateway_parts/http_handlers.py`：新增共享 `_denial_body(handler, body)`，按同一判据给响应体补码；接到 `/progress` 的 403、`/input-status` 的 404 与 403、`/result` 的 404 上。
- **不改拒绝语义**：允许/拒绝结果、状态码、既有 error/message 全部不变，只多一个可选字段。回环带错或过期凭据同样带码（9b 终审更正：代码与用例本来如此，处置同为重启客户端）；远程不可信来源、来源未知、开关关着（迁移档）不带这个码。
- **判定只读结构化事实**：开关档位、对端 IP、凭据常量时间比对结果；不解析路径、正文或 message（g2bad 里强调的纪律）。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「G2b 本机凭据缺失码（g2bfix1）」小节。覆盖挂闸三个入口（`/ask`、`/control`、`/client/notices`）+ 三个自判读端点 + 开关关着不变 + 远程不带码。3 个变异全被抓：① 去掉中间件的 error_code；② 去掉回环判断让远程也带码；③ 开关关着也返回码。
## B5 审批阶段的门决定承接与 interactive 取证（b5fix9b，2026-10-04，worker/b5fix9b，基于 17j 头 `4081a0df3`；ds5 复核无功能缺陷，已并入 step17k；出口用例由 luna1 b5fix9bt 补齐）

- **来源**：9b 对 B5 的终审结论——拦截这一层成立，但有两条"记下的结构化事实不对"：
  1. 插件要求确认之后，`_resolve_tool_approval` 每个出口都换掉了 result，换出来的 result 不带 metadata，第一次真实征询的条目就丢了：批准、拒绝两条路交给归档的都是空；无法审批那条条目还在但 `final_status` 记成 `ask`。设计第 9 节"每次收紧征询一条"在最主要的 ask 路径上不成立，B6 的"最近 10 次决定"和"无法审批"计数都漏。
  2. 生产接线用"on_chunk 有没有 `request_permission` 方法"判断 `interactive`，但 Gateway 流写入器总有这个方法；非管理员 IM 与没声明 tool_approval 的客户端 `interactive_approvals=False` 却仍被传成 true，插件据此选 ask，到审批时才变无法审批（handler 没跑，安全结果对，但设计 8.5 承诺插件能看到"不可交互"、自己选 deny）。
- **修法 1（条目承接）**：`plugin_gate_policy.carry_gate_decisions(original, replacement, *, unavailable=False)`——把原 execution 的条目与替换结果自带的新增条目按门身份 `(plugin_id, activation_id, gate_id, call_id, outcome)` 去重后合并，写回替换结果的 metadata；`unavailable=True` 时把 `final_status` 投影成 `PLUGIN_GATE_APPROVAL_UNAVAILABLE`。接在 `_resolve_tool_approval` 的各个"返回的不是原 execution"的出口：发审批前关门、无 consumer、等审批中关门、批准后重跑、无法审批、拒绝/取消、重复拒绝。
  - **去重不是可选项**：`plugin_gate_approval_unavailable` 自己就复制原 metadata，不按身份去重会把同一次征询写两遍（实测发现，已写进注释与用例）。
  - 用户的选择仍沿审批事实走，不改条目的 `verdict`。
- **修法 2（interactive 取证）**：`tool_call_runtime._interactive_approvals` 先读 `on_chunk.interactive_approvals`（bool 才认），没有这个属性才沿用原 callable 判断——后台 sink 要在等待期才知道有没有消费者，保留 true 由结算时投影纠正。
- **结构守卫（9b 裁定，不改运行时）**：现在**不加** `call_id + gate_id + activation_id` 去重——I4 续跑会对同一调用合法地再问一次，按门身份去重会吞掉真实的第二次征询。改为一条 AST 静态扫描用例，确认 `append_plugin_gate_decision` 与 `persist_tool_runtime_ledger` 各只有一个生产调用点。将来真要加第二个写入点，正确做法是在 `GateReview` 构造时生成征询 ID、按 `(call_id, 征询 ID)` 去重，而不是按门身份——这条约定写在用例注释里。
- **验证**：见 TESTS.md 同名节（四条出口各一条走完 resolve→归档的用例 + "每条只写一条不重复" + interactive 两条真实链路用例 + 两条结构守卫；4 个变异全部被抓）。
- **未验证**：真实插件子进程与真实 Gateway 端到端未跑；9b 复核时要重跑它的两个探针并复看每个出口的变异。

## M1 B4 直接隔离与一次性装配诊断（b4gs，2026-10-04，基于 `33d903aab`；已实施，待非作者复审）

- **来源**：3a 转 ds2 两条建议；直接调用 `gateway_event_context` 注入构造异常，不让四个调用方的兜底掩盖 M2 变异。
- **诊断**：确认 `plugin_events_enabled is True` 后的 Gateway/提示 owner/提示字段/回合/命令/工具上下文异常，统一记 `event=plugin_events.assembly_failed` 与固定 `reason_code`。同一进程同原因锁保护去重，日志 sink 抛错也不反噬业务；无配置、开关未知或关闭仍静默，不读后续事件字段，合法归属过滤不告警。只写诊断，不增加状态源、执行路径、配置项或插件访问。
- **证据与边界**：首轮两个直接文件 **54 passed**；再补启用后 Gateway 局部导入故障的有效红→绿，先准备共享诊断再导入装配入口。当前全部六份 `test_plugin_event*.py`、managed_gate 与十一份 guards9（含 packaging/constants_catalog）共十八文件 **349 passed**；M2、缺 warning、缺去重、关闭不静默、日志异常穿透、异常正文泄漏六个源码副本变异 **6/6 被抓到**，磁盘字节未改。门禁、复现和未验生产范围见 TESTS；不把本机隔离回归扩大为生产 Gateway/TUI/IM 或 B7 验收。

## B5×B7 跨件用例：收紧征询遇上慢/起不来的插件（b5x，2026-10-04，分支 `worker/b5x`，基于 17j 头 `1e1aadb80`；待复审）

- **起因**：b5b7x 只读交叉审查提出 B5 的加固门与 B7 的沙箱底座缺少合起来的用例。本次做其中不依赖 B7 接线的三条。
- **做法**：只加测试。新文件 `test_plugin_gate_cross_bfail.py` 用真 B2 池 + 假 client/transport 走真 `PluginGateReviewer` → 真 `merge` → 真 `ToolExecutor`，被替换的只有插件进程本身。
- **结论**：冷启动撞预算、沙箱起不来、进程被杀后下一次征询，三条都收紧成 **ask**（绝不放行）；另钉住合并方向（插件 `allow_as_is` 不会把宿主 ask 降成 allow）。
- **发现（不改代码）**：`merge` 的"不许放宽"有两处独立钳制，单改一处会被另一处兜住（防御纵深）；变异脚本需同时去掉两处。
- **验证**：见 TESTS「B5×B7 跨件」；变异 2/2 被杀（`_failure_reply` 改成放行、`merge` 改成取更松）。
- **未做**：第四条"总开关关掉就不再征询"等 B7 挑入后接线时一起做。

## DeepSeek 缓存：磁盘重载后历史逐字一致的修复（cachereload，2026-10-04，分支 `worker/cache-reload`，基于 `f8858179c`；3a 初审通过（去掉排序的变异被抓住），已并入 step17j）

- **问题**：DeepSeek 官网实测显示服务端 KV 缓存按前缀匹配；历史里任何一处字节变化都会让该点之后整段未命中。3a 观察到主对话每轮约一笔固定 16 万 token 的未命中。本轮只查一个分支：**会话从磁盘重新加载后，下一轮请求的历史是否与上一轮实际发出的逐字一致**（触发场景：Gateway 重启/换版、内存淘汰、子代理续跑、TUI 重连）。
- **查法**：不联网、不连真实接口；用真实读出链路（`ConversationStore` → `conversation_history_rows` → `_gateway_history_source` → `seed_provider_history_messages`）与真实后端 `_request_payload`，把「同进程续跑」和「磁盘重载」两条路径的第 N+1 轮 payload 逐字节比对。
- **找到的分叉点**：**工具调用参数的 JSON 键序**。`store_messages.MessageStore.append` 写 transcript 时用 `append_private_jsonl_records(..., sort_keys=True)`，会把 `canonical_native_messages` envelope 里 `tool_use.input` 的键按字母序重排。同一调用在进程内是 dict 插入序（如 `{"path","content"}`），落盘重载后变成 `{"content","path"}`；出站 `arguments` 字符串随之不同，前缀缓存从该条 tool_call 起整段失效。其余观察点（工具结果截短/归档引用、`reasoning_content`、tool_call id、`RuntimeFactsTurn` 位置、content 的 list/str 形态、CompactionSummary、插话位置、40 轮长历史）**实测逐字一致**。
- **修复**（最小、通用，不改存储格式）：出站序列化统一用稳定键序，使两条来源可复现同一字节。
  - `backends/openai_chat.py::_openai_function_call`：`json.dumps(..., sort_keys=True)`。
  - `backends/responses_wire.py::message_items` 的 `function_call`：同上（Responses 路径走同一份落盘历史）。
  - `backends/message_adapter.py` 未改：它只产出 dict，不产生字节。
- **注意**：`_clean_hint`/`store_messages` 的 `sort_keys=True` 本身是刻意的字节稳定设计（`test_private_writes_conversation.py` 有依赖），本轮**不改存储层**，只让出站对键序不敏感。
- **另一条已报告、不在本轮范围的现象**（交 sol1）：`_thinking_mode_supported` 要求历史里**每条** assistant 都有非空 `reasoning_content`，而 DeepSeek 服务端只要求「最后一条 user 之后、带 tool_calls 的 assistant」必须有。一条无思考的最终答复（常见）会让后续请求被写成 `thinking={"type":"disabled"}`：既丢掉 `reasoning_effort`，也切到服务端另一个缓存分区。实测该现象**与重载无关**，同进程每轮都发生；本轮只报告，不改 `openai_chat.py` 的该判定。
- **测试**：`agent_py_agent/tests/test_backends_provider_history_reload_identical.py`（4 条：payload 逐字一致、工具参数稳定键序、`reasoning_content` 保留、Responses 路径 live/重载一致）。结果见 TESTS。

## 9b 终审 pw3r 的必须修：只改用例（pw3t2，2026-10-04，分支 `worker/pw3r-tests`，基于 pw3t `bb476bd33`；只改用例；9b 复跑通过，已并入 step17j）

- **来源**：9b 对 pw3r（`5f34d1548`）+ pw3t（`bb476bd33`）的安全终审结论——产品代码没问题，1 条必须修，只改用例。证据 `~/.my-agent/decision-evidence/review-pw3r-5f34d1548/9b/`。
- **必须修**：`test_plugin_event_gateway.py::test_prompt_fault_or_disable_never_breaks_queue[queue_error]` 失败（期望 500 实得 202）。原因是这条 B4 用例给 `Path.write_text` 打桩模拟队列写失败，而 pw3r 把旧 /ask 改成私有原子写（`http_handlers.py:710`）后不再走 `Path.write_text`，桩落空、故障没真正注入。修法：给模块属性 `http_handlers.write_private_json_file_atomic_no_newline` 打桩抛 OSError（调用点 `http_handlers.py:7` 就是按这个名字导入的，模块属性打桩才命中真调用点）；在不注入故障的 `disabled` / `publish_error` 两个参数里补断言：inbox 队列文件恰好一个、权限 0600。这样它同时成为旧 /ask 私有写的端到端用例，也覆盖了 9b 的 P1 变异。
- **建议补**：`test_private_writes_batch3.py` 新增 `TestMessageRepairs`，直接调 `_queue_gateway_conversation_repair`（`request_history.py:578`），断言 `message_repairs/<id>-assistant.json` 是 0600、父目录（刻意缺省）由私有写建成 0700；覆盖 9b 的 P2 变异（message_repairs 退回非私有写）。
- **变异复跑**：P1（旧 /ask 退回普通 `write_text`）KILLED；P2（message_repairs 退回 `write_json_file`）KILLED。两个都还原、`git status` 干净。
- **未验证**：只改用例，没有改产品行为；Windows/无 dir_fd 平台照旧未实测。

## pw3r 初审补测（pw3t，2026-10-04，分支 `worker/pw3r-tests`，基于 pw3r `5f34d1548`；只补用例、注释与文档；已并入 step17j）

- **来源**：pw3r 非作者初审（pw3rr）的三条小问题。实测变异 M3/M4 存活，确认下面第 1 条是真实覆盖缺口，不是"用例写得太浅"的错觉。
- **做了什么**：
  1. `session/manager.py:25`、`session/cross_channel.py:39` 的 `ensure_private_dir(self._session_root)` 各补 2 条用例：umask 022 下 `_session_root`（`<sessions>` 这一级）是 0700（证明权限来自显式 mode，不跟 umask 走）、已存在的 0755 目录一位不动。原有用例只断言更深一级的子目录（那一级是私有写函数建的），所以 `__init__` 的建目录没人钉。
  2. `gateway_parts/adapter.py` 的 `_ensure_adapter_dirs` 补双层注释（`# LLM:` + `# 函数用途:`），写清建 inbox/processing/done/failed/outbox 五个目录、缺失时 0700、已存在不动。
  3. TESTS.md 的 pw3 同名节：把"未验证"里 `http_handlers` 旧 /ask 与 `request_history` message_repairs 两点写清（无独立端到端用例、只由同原语 + 静态核对覆盖），并补本次补测记录。
- **验证**：`test_private_writes_batch3.py` 14→18 条全过；M3/M4 变异（把 `__init__` 的 `ensure_private_dir` 退回 `mkdir`）现在被新用例杀死。详细命令与结果见 TESTS.md 同名节。
- **未验证**：只加用例与注释，没有改产品行为，也没有跑真实 Gateway；Windows/无 dir_fd 平台照旧未实测。

## B7 Linux 前缀别名与受限策略接口（b7fix，2026-10-04，分支 `worker/m1-b7-on17j`，基于 `1da20daac`；已并入 step17j，3a 沙箱外 Mac 验证通过，Linux 车道与 9b 终审待做）

- **起因**：sol1 复审发现 Linux bwrap 在 tmpfs 隐藏 Gateway 用户家目录后，只挂回 Python 安装前缀的 realpath，丢失家目录内的 symlink alias；另缺少给后续旧插件权限复用的受限 R/W/E 规格工厂，且启用注释/进程沙箱文档已过期。
- **改动**：Linux 收窄读路径仅在 `read_only_root` 规格中保留宿主已核验的原路径与 strict realpath 两个挂载点，普通 shell 的既有根规范化不变；G5 Landlock 端口拒绝包装逻辑保留。新增 `PluginRestrictedSandbox` 与 `plugin_restricted_sandbox_spec`，输入宿主核验的读根、写根、程序根、网络和隐藏根，产出同一 `AttemptSandboxSpec`；候选预检、插件业务连接与面板客户端不复制平台规则。程序根目录按目录开放；单文件根只开放文件本身与真实目标，不扩到父目录；授权根覆盖隐藏根时 fail-closed。
- **注释/设计**：修正 `plugin_enable_tool.py` 模块、类说明和旧 B1 口径；同步 `PLUGIN_PROCESS_SANDBOX.md`、`PLUGIN_EVENT_HOOKS.md` 的家目录/G4/G5 与受限策略合同。TESTS 中另补 ds10 对 17j catalog 重生成和 8 条沙箱基线失败的来源说明。
- **验证状态**：Linux alias argv 与规格单测已按测试先行 red→green；K4/K8/K9/K10 聚焦变异已由对应断言杀死并逐项 SHA-256 还原。本机 macOS 未验证 Linux 真解释器 alias 进程；受限策略真进程/Seatbelt 未在本机形成隔离证据。完整测试与门禁结果见 TESTS.md，3a Linux lane 与 9b/3a 沙箱外 Seatbelt 复核待完成。

## DeepSeek 思考开关只看本轮（thinking-rule，2026-10-04，3a，分支 `claude/3a-thinking-rule`，基于 17j `15feb6b91`；用户点名的缓存费用底座修复；9b 终审通过，已并入 step17j）

- **问题**：`backends/openai_chat.py` 的 `_thinking_mode_supported` 要求出站历史里每一条 assistant 都带 `reasoning_content`，否则整条请求写 `thinking: disabled`。模型的最终答复经常没有思考内容，于是之后的请求都被切到“关思考”。
- **为什么是费用问题**：DeepSeek 官网的上下文缓存按“思考开关 + 推理强度”分区（不带档位、high、显式开思考共用一份；low、max、关思考各自一份），切换就从头全不命中，连 system 都不命中；模型也从此不再思考。
- **服务端真实要求**（官网实测，事实文件 `~/.my-agent/decision-evidence/deepseek-cache-audit-1004/server_probe_facts.md`）：最后一条 user 之后的 assistant 都必须带 reasoning，纯文本也一样，缺了就 400；最后一条 user 之前的不带也接受。
- **修法**：只检查最后一条 user 之后的 assistant；本轮内真缺思考时仍显式关思考，避免 400。属于让 R233 原意准确生效的 bug fix，不加开关。
- **上线影响**：以前被卡在“关思考”的老会话，新一轮会重新思考，输出 token 会变多；用户想关思考，用显式档位 off（reasoning_control.py）。9b 另测了 opencode Go（/zen/go/v1），新规则在那边也不会引出 400。
- **未做**：压缩请求的档位对齐（luna3 `worker/cache-compact`）、跨轮和重新加载的前缀一致性（sol1、ds7）、诊断补全（ds3）另行交付。

## 只看档加进管理员 /settings 白名单（obsset，2026-10-04，分支 `worker/obs-user-setting`，基于 17j 头 `35b1647e8`；3a 初审、9b 终审通过，已并入 step17j）

- **来源**：用户拍板 17j 上线后在生产打开屏幕观察的“只看档”（`computer_use_observation_enabled=true`），完整档 `computer_use_enabled` 保持关；上线清单与验收手册 A4 都写“用 /settings 设”。3a 在隔离环境实测 17j 头：管理员 `/settings` 作用域里 `set computer_use_observation_enabled true` 返回 **PARAMETER_BOUNDARY**——该键在 `parameter_registry._BOUNDARY_NAMES` 里，但不在 `settings/user_config_capability.USER_SETTINGS_BOUNDARY_KEYS` 白名单里。
- **改法**：把 `computer_use_observation_enabled` 加进 `USER_SETTINGS_BOUNDARY_KEYS`（`user_config_capability.py`）。白名单只放宽“已认证管理员 /settings 写作用域”这一条路径；模型来源的 `set`/`reset`/`revert` 仍按边界拒绝（由 `_USER_SETTINGS_WRITE` 上下文变量区分调用方，未改判定逻辑）。**`computer_use_enabled` 刻意不加**：它会交出上游鼠标/键盘/截图全套执行面，继续只能手改配置文件。
- **写入位置**：该键登记来源是 `agent`（不是 capability），写在用户配置文件（`WritePaths.user_path`），不是 owner 的 `capability_config.yaml`；文件不存在时以 0600 新建。
- **生效时机**：`restart_gateway`。读点在 `core.py:_build_tool_registry`（`observation_enabled = config.computer_use_observation_enabled`），装配点在同模块 `SimpleAgent.__init__`；`config` 是进程启动时加载的，写盘不热加载，所以要 `/restart`。**不新加机制**：`set_parameter` 回执已带 `effect_when="restart_gateway"` 与“保存后需要重启 Gateway 才生效……发 /restart”的 `effect_text`，`/settings show` 也在运行值与保存值不同时标注“发 /restart 后生效”。
- **入口**：TUI 与飞书等 IM 走同一个 Gateway 入口——TUI 侧 `cli/chat_parts/control_runtime.py` 把 `/settings …` 原样发给 Gateway，宿主侧 `gateway_parts/control_service.py` 调 `gateway_parts/settings_control_service.execute_settings_control`（身份授权与写作用域都在那里）。没有为飞书新加入口。
- **文档**：`docs/design/computer-use.md` 新增“只看档怎么打开、什么时候生效”；`docs/design/J16_SCREEN_OBSERVATION.md` 第 8 节配置表写明两个开关各自的开关方式与生效时机；`agent_py_agent/config/agent_config.yaml` 中文注释同步并重新生成前端参数目录。
- **验证**：见 `TESTS.md`“只看档加进管理员 /settings 白名单（obsset）”小节。
- **未验证**：真机 `/restart` 后新进程按新值装配观察工具这条端到端路径没在沙箱里跑（不启动 Gateway 是工作规则）；发送方向只有单测与代码位置核对。

## B5 第5段叠到17j、组合转正与六条小项（b5s5，2026-10-04，worker/m1-b5-on17j，已并入 step17j，9b 终审待做）

- **来源**：在b5r交付 `65f47f2c6a45ee13f35318c7837993fcccf58581` 上三方应用 `git diff ff5d761d9 d517a8b7b | git apply -3 --index`；源第5段两个提交 `0e1a9550b` / `d517a8b7b` 属 `worker/m1-b5-seg5-on-s3`，不是本次迁移SHA。本轮迁移/行为提交为 `f86e9b141bd7a8509964d591a9b458b1679807e4`（2026-10-04 10:52 PDT，中文WIP提交，20文件+1012/-39）；本次补记只改文档，不改变已验证产品。
- **冲突**：`executor.py` 两个import都保留，B4 `EventPointContext/on_handler_started` 不删；DESIGN_LEDGER/TESTS以17j完整内容为底，保留b5r与源第3/4段历史，再追加本轮第5段来源、当前验证及修正取舍。常数目录产品脚本重新生成899项，未手合。
- **组合实际暴露并修复**：源第5段只从 `merged.requirements`（仅ask）生成条目，直接deny/allow会丢；源reader又读归档顶层，但真实builder把专键放进 `tool_result_envelope`。组合去xfail后有有效业务红（实读0条），改为所有review生成决定且排除 `approval_applied=True`，writer只读canonical信封，不给假顶层数据补旁路。收尾另有有效红：不可交互混合ask/deny的真实拒绝被账本错记无法审批，投影条件补“最终仍为ask”，保留实际deny和B6计数口径。拒绝/允许/混合门及不可交互终态都有断言。
- **写入边界**：归档只复制executor metadata中的专键，其余仍走原白名单；原 `persist_tool_runtime_ledger` 唯一调用点追加 `plugin_gate.decided`，每门一条，无门不写。AST实际核对只有一个 `append_plugin_gate_decision` 调用；新增第二调用点前必须先加幂等键。精确批准跳门没有协议征询，绝不当作决定入账，不新增批准账或共用池。
- **六条初审小项**：①主claim换轮/子DONE后scope失效，零轮询立即unavailable并清精确旧行；②直接resolve换claim/attempt，不借展示或wait校验，旧行字节不变；③I4门引用每个字段及参数哈希相等，传输调用除新call_id外逐字段相等（恢复的call_id必须是新身份，不强塞旧ID）；④同activation/version/gate仅换plugin_id仍重新ask；⑤注释说明外层五字段约束宿主批准归属、内层四字段约束插件征询身份，两层缺一不可；⑥write_boundary异形安全退化为不跳门、重新征询。
- **实际验证**：当前版本27显式文件 **563 passed**，0 failed/errors/skipped/xfail；其中完整guards9现场11文件187项。组合三项均转正，删除零handler/零工具事件且真正由归档/持久化写一条决定并被B6 info读到，允许更新仍真执行并发成对事件。第4段六变异与第5段三变异全部业务杀死；另n3、停止分支、只漏plugin_id、两写账接缝及混合终态六项反证均有效，终态修正后十五项全部重跑，产品字节前后不变。尺寸差异最初新增2个测试函数告警，原样抽出等待/引用断言和假传输准备后消除；未升baseline，报告还原不提交。Ruff/import/strict/常数与尺寸门禁结果及原XML见TESTS；中间版本91/19/562与最终563重叠、不相加。
- **边界与下一步**：源第5段16项/三变异只作ds2来源交付事实，不代本树563项。sol2/b4g保留点核对仍与17j相同，由3a先挑b4g再挑本线；9b终审、18项真实宿主准备失败的外部复验、B7真沙箱、真实TUI/IM/Gateway及12片Linux全量仍未验证。本线不部署、不push、不改STATUS/ROADMAP/COMPLETED。下方保留各历史时点。

## B5 第1–4段搬到17j（b5r，2026-10-04，worker/m1-b5-on17j，已并入 step17j，9b 终审待做）

- **来源与基线**：17j 真正基线 `ebe621d8714e4e04c9c0a02c592e0328dd2d579e`；应用 `git diff b6ede99e0 ff5d761d9` 的第1–4段，来源头 `ff5d761d91142b6283864fedd6fb13a9c2885d00`。采用 `git apply -3`，不是把源分支提交号当本次迁移号；迁移产品提交：`d793da549707157d6f5e53754cd1937150d04e01`（2026-10-04 08:43 PDT，中文WIP提交；第5段和外部验收仍待叠入），本段提交号补记仅改文档，不改变已验证产品字节。
- **产品冲突**：`gateway_parts/plugin_panels_http.py` 保留17j的B3/B4唯一 hub、发布与停机逻辑，仅追加当前共用池读取入口；`tooling/registry.py` 保留唯一 `_registry_approval_mode` 和B4宿主事件上下文，追加B5 reviewer，不重复读取审批模式；`tooling/executor.py` 同时保留B4真实handler回调与B5宿主来源/征询字段。`agent_core/tool_loop/round_execution.py` 保留17j停机前后复核及原审批事实，在原流程上附加插件审批引用和拒绝说明；`tool_call_runtime.py` 只追加宿主 interactive 事实。
- **不可改变的顺序**：B5在宿主 `ActionPolicy.decide` 之后、原审批之前只能收紧；B4工具事件仍只在handler真正开始时发。插件直接拒绝返回 `handler_executed=False / not_started`，不发工具started/finished。sol2负责的 `_tool_event_context` 和 `event_points.prompt_queued` 的 `agent.config` 读取保持17j原样，后续由3a叠修复。
- **生成与文档冲突**：常数目录不手合，用产品 `scripts/build_constants_catalog.py` 重生并核对899项；文档保留17j条目与B5历史说明，TESTS以17j为底重新记录本次验证，避免旧测试结果覆盖新基线。
- **组合证据**：新增 `test_plugin_gate_event_combination.py`，真实rm-guard样例协议、原注册表和执行器，临时安装、不启用，激活及传输使用隔离替身。拒绝删除零handler/零事件、允许更新真实handler并发成对事件，2 passed；实际 `/plugins info` 决定读取单独 `xfail(strict=True)` 等第5段，绝不手插账本行。
- **回归**：26相关文件及完整10文件guards9联合697项：678 passed、13 failed、5 errors、1 strict xfailed。18个失败/错误在真正基线 `ebe621d87` 的detach临时worktree复跑，失败身份完全一致：13项停在插件启用准备 `outcome_unknown`，5项为 `managed background launcher identity unavailable`；不将这些业务入口写为通过，不仅凭无响应归因沙箱。临时worktree已移除。
- **后续**：第5段由ds2在 `worker/m1-b5-seg5-on-s3` 接源 `ff5d761d9` 搬移，3a再叠到本分支，不等待、不代做；安全复审、真实宿主启用/沙箱、TUI/IM/Gateway和12片Linux全量仍未验证。本轮只本地提交，不部署，不碰STATUS/ROADMAP/COMPLETED。门禁命令与结果见TESTS本节。

## 插件使用说明改成"合入后目标状态"（guidefix，2026-10-04，分支 `worker/17j-plugin-guide`，基于 `eca1d3dd8`；只改文档；已并入 step17j，连同 guide17j，3a 核对差异）

- **这份说明描述什么**：`docs/guides/PLUGIN_GUIDE.md` 写的是 **B5（工具把关）、B7（插件总开关与隔离底座）、obsset（观察开关进用户可写白名单）** 三件事**合入后**的目标状态，不是 17j 现状。各段落的生效条件在文中逐条标明：第四节（工具把关）标注"B5 合入后生效"，第六节（总开关与隔离底座）标注"B7 合入后生效"，第七节第 2 步（怎么打开只看档）标注"obsset 合入后生效；要不要重启以 obsset 结论为准（3a 挑入时补）"，第十二节列清哪些还没生效。
- **本次改了什么**（ds6 初审小问题 + 3a 指定）：
  - 第八节的旧文本写"打开后旧客户端会拿到结构化错误码 `LOCAL_CREDENTIAL_REQUIRED`"——该码**代码里不存在**，只出现在设计稿 `docs/design/GATEWAY_LOCAL_TRUST.md` 的待办里（实测 grep 只在设计稿命中）。改成按代码事实写：G2b 打开后不带凭据的本机请求降为匿名（`auth/middleware.py` 的 `extract_identity`），本机凭据自身的问题码是 `LOCAL_CLIENT_CREDENTIAL_*` 系列（例：`LOCAL_CLIENT_CREDENTIAL_MISSING`、`_PERMISSIONS`、`_INVALID`，均在 `gateway_parts/local_client_token.py`，且 `tests/test_gateway_local_trust_enforcement.py` 有断言）。
  - 正文里的仓库内部路径、函数名、代码行号收进文末新增的「给维护者的核对位置」小节；正文只留用户能用的事实。
  - 第七节第 2 步原来只写"改成 true"，没说怎么改；3a 实测 17j 头：管理员发 `/settings set computer_use_observation_enabled true` 会被拒（`PARAMETER_BOUNDARY`，不在用户可写白名单）。按 obsset 合入后的样子改写成"TUI 和飞书都发 `/settings set computer_use_observation_enabled true`"，并标明 obsset 合入后生效、`computer_use_enabled` 仍不能用聊天命令改、是否重启以 obsset 结论为准。
  - 第 111 行原写"登记在 `_BOUNDARY_NAMES`"，核对 B7 分支：该键在 `parameter_registry._BOUNDARY_NAMES` 和 `settings/user_config_capability.py` 的 `USER_SETTINGS_BOUNDARY_KEYS` 两处都登记，已按代码补全；并点明 B7 合入前该参数不存在、`/settings` 回 `PARAMETER_UNKNOWN`。
- **未验证**：只改文档，没跑产品代码测试；手册本身不是运行证据。

## M1 B4 全入口观察装配故障隔离（b4g，2026-10-04，基于 `2ad257314`；ds2 初审可以挑入，已并入 step17j）

- **来源与边界**：3a 的 17j 定向回归指出工具观察直接读取缺失 config，使 handler 未启动；补充 HTTP 提示入口同类故障。只隔离可选观察配置、路由和上下文装配，原权限/写边界、唯一执行器、核验与业务异常不吞掉。
- **实施**：工具 `_tool_event_context` 和 Registry 转交口安全降为 None；Gateway 提示、回合开始/收口、命令全部先安全检查开关，再读事件字段，装配失败不发。关闭不读取事件路由、回执、响应或 owner，不做事件 I/O。
- **证据**：真实 Registry/Executor/计数 handler 的缺配置与观察配置读取故障回归、HTTP handler 入队回归、正常主/子/J16 及真实 HTTP 正例均覆盖。去保护的三个源码副本变异被抓到，原文件字节不变；33 文件含原四老文件、experiment 文件及完整 guards9：803 passed、2 skipped。3a 再补 Gateway 鉴权、身份与 tool_loop 六个同根因用例，八文件逐项复核为 233 passed、2 skipped、20 xfailed；点名六项及 f2/h/h2/h3/n_long 全通过，产品源码不再改动。详细命令、逐文件结果、沙箱跳过与未验边界见 TESTS。

## 带前缀凭据键名打码（rkey + rkey2 + rkey3，2026-10-04，worker/redaction-var-refs-v2；9b 三次复跑都通过，已并入 step17j）

- **rkey3：JSON 键名同口径 + 去掉键名长度上限（2026-10-04，同一分支接着 `1d89a87a3`）**。
- **已知边界（9b 复跑 rkey3 时确认，接受）**：按文本打码时 `{"token": "${GW_TOKEN}"}` 会保留变量引用；按字段名判断的结构化对象路径会把它整个打码。这是往“多打码”方向的口径差异，安全，不补代码。证据 decision-evidence/review-rdv-a5143365b/9b/rkey3-dde18f895/。
  ① 9b 建议 2 记账的那处口径差异：`_SECRET_JSON_FIELD_RE` 原来是**精确字段名**匹配，`{"db_password": "x"}`、
  `{"api-token": "x"}`、`{"AWS_SECRET_ACCESS_KEY": "x"}` 这类带前缀的 JSON 键一律不打码，而文本赋值规则
  （rkey/rkey2）已按完整末尾片段认了同一批键名——同一个值写成 YAML 会被打码、包成 JSON 就漏出去。改法与
  rkey2 一致：正则只用不嵌套的单量词取整段带引号键名（分组 1=键名开引号、2=键名、3=引号+冒号、4=值开引号、
  5=值、6=闭引号），判定搬进新回调 `_redact_json_field`，用同一个 `_is_credential_key_name`；不是凭据键
  整段原样返回。源码模式（`code_file=True`）本来就在调用处整段跳过 JSON 与赋值规则，行为不变；纯变量引用
  值（`"$DB_PASSWORD"`）照 rdv2 保留。
  ② 9b 建议、3a 采纳：**去掉键名 128 字符上限**（文本赋值规则与 JSON 键名规则两处都去）。200 字符前缀加
  `-password=` 的键名在上限下匹配不到、值原样漏出，相对 rdv2 是回归；上限对性能也没必要——键名是单个
  量词、前面又要求非键名字符，回溯是线性的。超长键名用例实测 2 万字符前缀 < 0.001 秒。
  ③ 性能用例加了 SIGALRM 墙钟兜底：性能断点是 0.1 秒，但真退化成指数级时光跑一次就要小时级，计时断言
  永远等不到、测试进程会挂死；兜底在 10 秒硬中断让用例直接变红。4 条原有性能用例去掉上限后重跑，
  1 万~5 万字符输入实测 0.0010–0.0027 秒，两条新增长 JSON 用例 0.0016–0.0020 秒。

- **rkey2：修 ReDoS（2026-10-04，同一分支接着 `6a5440cf9`）**。9b 复核发现 rkey 的键名前缀
  `(?:[A-Za-z0-9_.]*[_-])*` 是嵌套量词，下划线既能被内层吃也能被外层吃，匹配失败时回溯指数增长：
  21 个下划线 0.15 秒、29 个 37.7 秒（对照 rdv2 一万个下划线 0.0003 秒）。打码函数要处理每一段工具输出、
  每一行日志、技能落盘和错误消息，Markdown 分隔线 / ASCII 表格 / 日志横线这类长串下划线就能卡住回合与
  Gateway 线程。改法：正则只用一个不嵌套的 `[A-Za-z0-9_.-]{1,128}` 整段取键名，判定搬进替换回调
  （`_is_credential_key_name`，口径与 `is_credential_key` 一致；不是凭据键就原样返回整段）。**不用**原子组
  或占有量词（项目最低 Python 3.10，那两个要 3.11）。修复后 9b 的回溯探针全部 0.0000s。
- **rkey2 顺带修掉一个漏打码**：判定搬进回调后词表同时用于正则拼装与集合判定，而原词表里的 `api_?key`
  是正则语法，`split("|")` 成集合后 `api_key` 根本不在里面 → `api_key = ...` 一度被判"不是凭据键"放过
  （9b 的 9 样本里 `DB_PASSWORD=$ecretPassw0rd` 也从"未打码"变成"正确打码"）。词表改成纯字面量
  （补 `api-key`、`api_key`、`apikey` 等写法）并在 import 时断言不含正则元字符，另加用例钉住。
- **全文件复查**：`log_redaction.py` 13 条正则逐条在长输入上实测，最长 0.4ms，**没有第二处嵌套量词**。

- **缺口**：`_SECRET_ASSIGNMENT_RE` 的键名前原来是 `\b`，而 `_` 是单词字符，`\b` 断不开，于是 `DB_PASSWORD=hunter22`、`api_token=abcd1234`、`MY_SECRET: xyz12345` 这类**带前缀的键名在基线里就不打码**，值原样进日志与模型上下文。
- **改法**：键名改成按**完整末尾片段**认，与 `settings/user_config_capability.is_credential_key` 同口径——键名等于凭据词，或以 `_凭据词` / `-凭据词` 结尾才算，**不按子串**。前缀允许由多段 `[_]`/`[-]` 连接（`X-Gateway-Token`、`AWS_SECRET_ACCESS_KEY` 都要认）。
- **反例必须挡住**：`max_tokens=4096`（复数 token 不以 token 结尾）、`token_count=12`（token 在开头）、`password_hint=x`、`tokens=4096`、`secretary=...` 一律不误伤。
- **词表不共用**：`common/` 不能反向依赖 `settings/`（`check_import_boundaries` 会拦），且这里还要认 `-` 连接的写法（命令行/HTTP 头风格），settings 那份只管下划线参数键。两份各管一处，代码注释里写明。
- **其它路径不变**：源码模式（`code_file=True`）本来就在调用处整段跳过赋值规则，行为不变；纯变量引用豁免照旧——`DB_PASSWORD=$DB_PASSWORD`（全大写）仍保留原文。
- **验证**：`test_log_redaction_variable_refs.py` 51 passed；全部 `test_log_redaction*` + 审计/IM 脱敏 129 passed；变异 m1（去掉末尾片段匹配）与 m2（放宽成子串匹配，`max_tokens` 被误打码）都被抓到。见 TESTS。

## M 线真实验收手册改到能用（rb2，2026-10-04，待复审）

- **来源**：luna3 的手册（`worker/m1-acceptance-runbook` 的 `4ffb9c69f`）经 `git diff ea574ce26 4ffb9c69f | git apply -3` 引入本分支；luna4 初审结论"必须改"四条由 rb2 落实。
- **四条必须改**：① 补 `allow_as_is`（`NO_MATCH`）安全命令照常放行场景与超长参数截断场景（含"已 deny 仍 deny、已 ask 保留原码"的完整口径）；② 新增 §6.5 B7 断网/收窄读探针——**用插件进程自己的探针**，五项一起判（外网/回环/宿主敏感区 blocked，自己的包与数据目录、解释器 allowed），明确 `read_file` 等宿主工具代证无效；③ 新增 §6.6 非 local/main 身份负例（预期 `plugin_events_owner_not_allowed`）；④ tmux 全程改用私有套接字（`-L "$M1_TMUX_SOCKET_DIR/default"`，目录 0700），启动/发命令/清理都用它，收尾先关会话再删隔离目录。
- **其余调整**：版本前置明确写成 17j，并在开头列出三个「没就位就停」的前提（B5、B7、老插件权限）；真实模型固定 MiniMax-M2.7；重申绝不碰 8420；新增 §6.7 说明联合冒烟与逐渠道矩阵的关系；§8 通过矩阵补四行（三裁决齐全、截断只加严、B7 探针、非 local/main 负例）。
- **附录 A**：把 3a/acc 写的 17j 验收清单里插件之外的七项并进同一份手册（G2b、锁与私有写、数据根收紧、屏幕观察只看档、飞书限流脚本、后台收尾、能力包 hint），带前提/入口/结构化判据/token 与等待估算，以及"最快暴露问题"的执行顺序和各项不通过写法。
- **验证**：本分支只改文档。`check_doc_sync.py` DOC_SYNC_PASS；`git diff --check` 干净；strict code-size blocked=False；`check_clean_package.py .` OK；`size_diff.sh` 新增告警 0。手册未执行（真实验收待 B5/B7/老插件权限合入后由 3a/9b 在隔离环境执行）。
- **未验证**：手册本身是操作指引，本轮没有跑任何一步真实验收；§6.5 的探针插件是设计描述，尚未写出实际插件代码。

## 17j 状态字校正（stfix，2026-10-04，分支 `worker/17j-status-fix`，基于 17j 头 `f9a6f6c6e`；只改状态字与措辞，不改正文）

- **为什么**：audit17j 只读核对发现，一批已经并入 step17j 的改动，状态字仍停在"待复审/待 9b 终审/WIP"，与 17j 实际状态不符；另外 ds2 的上线清单用"`plugin_commands.py` 里 grep `plugin_gate` 计数为 0"断言 B6 展示层没进 17j，该判据不成立。
- **改了什么**（只改状态那一句和一处归属说明，正文与段落顺序不动）：
  - A 类 11 处改成"已并入 step17j + 提交号"：TESTS 的 rdv(9b 终审通过 `a67c5df3b`)、tdeny(`2fb2eb718`)、rcb/rco(`39350230a`)、phh(`1a21ae0d9`)、flc(`cb16433d7`)、B6(`24c3aec0f`)、G2b(9b 终审通过 `32551798d`)；gateway 02-progress 的 rcb/G2b/B6 三条。
  - B 类私有写整包（pdp、pw2、pwf、lkp、lkf、ds3l）改成"9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`"，证据 `~/.my-agent/decision-evidence/review-private-bundle-2ad257314/`；DESIGN_LEDGER 8 处、TESTS 5 处、gateway/memory/subagent 三个 02-progress 共 7 处。
  - C 类：trs 改"luna2 初审通过（TESTS 归因已修 477611a6d），已并入 step17j `c74bf5893`"；b9tr 改"luna6 初审小问题已随 trs2 补，已并入 step17j"。
  - B6 归属更正：展示层已并入 step17j `24c3aec0f`，四段渲染 `plugin_commands.py:render_plugin_event_details`、查询 `runtime_db/repository.py:plugin_gate_decisions`、TUI 与飞书同一份输出；缺的是 B5 写入方，真实 `/plugins info` 第 4 段暂显示"暂无记录"。
- **未验证**：只改文档，没有跑产品代码测试；手册/台账本身不是运行证据。

## 活动回合插话不重复发 prompt_submitted 的回归用例（st2，2026-10-04，分支 `worker/17j-small-tests-2`，基于 17j 头 `f9a6f6c6e`；3a 复审，已并入 step17j）

- **起因**：luna6 复审 B4 owner 修复时提出——活动回合里成功插话（`/ask` 返回 `active_turn_input`）不能发布 `prompt_submitted`。实现靠 `gateway_parts/http_handlers.py::_handle_idempotent_ordinary_ask` 的"只有 `created and receipt.state == 'queued'` 才调 `prompt_queued`"，但没有专门用例钉住；ds8 试过两条路都造不出活动回合。
- **做法**：**只加测试，不动产品代码**。复用仓库现成写法——`test_plugin_event_gateway.py` 的 `gateway` fixture（真 `handle_ask` + 观察 `publish_plugin_event`）与 `test_steer_delivery_recovery.py` 的真实 Gateway 客户端思路；活动回合按 `control_service._active_request` 的结构化判据构造（`processing` 里恰好一条同渠道/会话/用户、`turn_phase=open`、未被 detach 的请求）。
- **用例**：新增 `test_plugin_event_gateway.py::test_active_turn_steer_does_not_publish_a_second_prompt_submitted`——首条经真实 `handle_ask` 排队（1 次 `prompt_submitted`）→ 搬进 `processing` 标 `turn_phase=open` → 再经 `handle_ask` 插话；断言插话回执 `disposition == "active_turn_input"`、`status != "queued"`、`target_turn_id` 指向原回合、未另开排队请求，且事件流里 `prompt_submitted` 恰好一次。
- **验证**：`test_plugin_event_gateway.py` + `test_plugin_event_e2e.py` **20 passed**；guards9 **187 passed**；ruff / boundaries=0 / doc_sync PASS / strict code-size `hard=0 blocked=False` / `git diff --check` / clean_package 全过；size_diff **新增 0**。变异 2 个都被杀（`if created` → 新用例红；无条件发 → 新用例 + 既有重放用例红）。
- **未验证**：真实 TUI/IM 与真实运行回合中的插话由 3a 在真实验收里核对；本用例覆盖"活动回合记录存在时插话不再发 `prompt_submitted`"这一层。


## B9 的测试期望跟上新技能（b9fix，2026-10-04，worker/b9-seed-fix；ds4 初审可以挑入，已并入 step17j）

- **起因**：B9 新增内置技能 `plugins/write-my-agent-plugin`（按 Skill 设计带 `references/`、`templates/`），17j 上两条老断言过时，7 条用例从合入点起失败：`test_skill_tree_and_recall` 写死 `len(snapshot.entries) == 27`（实际 28），`test_builtin_seed` 断言 `shared/builtin` 下除 `SKILL.md` 外没有任何文件（实际多了该技能的 6 个附属文件）。
- **改法一（数量同源）**：把写死的 27 换成 `builtin_seed._skill_dirs(builtin_seed._BUILTIN_SRC)` 的长度，以后再加内置技能不用改数字。**路由预期一个字没动**——6 条代表任务的路由断言本身仍然全过，说明新技能的 description / when_to_use 没有抢走原有路由，不需要收窄技能元数据。
- **改法二（保住原意）**：原断言想防的是"复制完留中间态"。改成调 `_assert_builtin_mirror_is_complete`，三层核对：① 除 `SKILL.md` 外的文件只能落在某个技能目录的 `references/`、`templates/`、`scripts/` 子树里（允许再往下分层，如 `templates/python/src/`）；② 每个镜像文件都在源里存在且逐字节一致，源里的文件也都要被镜像（不漏拷）；③ 没有隐藏文件，也没有 `.tmp`/`.part`/`.swp`/`.bak` 这类临时或半截文件。
- **验证**：`test_builtin_seed.py` + `test_skill_tree_and_recall.py` 23 passed；加 `test_write_my_agent_plugin_skill.py` 共 62 passed（1 skipped，与本改动无关的既有跳过）；guards9 176 passed；静态门禁全过。变异 3 个全被抓：数量源换回写死 27（6 条路由用例红）、镜像里多一个临时文件（seed 用例红）、镜像文件与源不一致（seed 用例红）。命令与结果见 TESTS。
- **未验证**：没有改产品代码，所以没有真实链路要复核；沙箱外行为与沙箱内一致（纯文件系统断言）。
- **补（同一分支 `be6ae1f8f`）**：`test_write_my_agent_plugin_skill.py` 里还有 2 条用例从 Git 历史读"基线字节"（旧 Python 模板、旧构建脚本），在只拉一个提交的车道容器/CI 里会报 `fatal: Not a valid object name`，从源码包装的环境更是没有 git。把这两份字节**原样存成测试夹具** `agent_py_agent/tests/fixtures/b9_legacy_git_bytes/`（来源提交与逐文件 sha256 写在同目录 README），用例改读夹具、不再调用 git。构建脚本夹具带 `.txt` 后缀，避免被代码尺寸扫描当成在运源码。另加一条校验：本地对象库有这两个提交时逐字节核对夹具与 git 对象，没有时**只跳过这一条校验**、不跳过原用例。三种环境实测：完整历史通过、无历史对象的 shallow clone 通过（只跳过来源校验）、完全没有 git 也通过。详见 TESTS。

## 宿主私有 JSON 写保持 0600（sclk3，2026-10-04，claude/3a-sclk3，基于 17j `a67c5df3b`，3a 实现；9b 复跑通过，已并入 step17j）

- **背景**：9b 复核 sclk2 时发现，同一进程再次抢同一把 scoped lock 会走刷新分支，`daemon_metadata._write_json_file` 用 `tmp.write_text` 再 `os.replace`，新文件按 umask 落成 0644；心跳定期刷新，所以抢到锁几秒后锁文件就变回世界可读。只泄露 pid 和元数据，别人卡不住锁，但 sclk 这一包的目标就是锁文件收私。
- **改动**：临时文件改用 `os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600)` 新建再替换（新增 `_write_private_tmp`），替换后的正式文件就是 0600；上级目录从裸 `mkdir(parents=True, exist_ok=True)` 改成 `nofollow_fs.ensure_private_dir`（缺失各级 0700，已存在的不动）。仍在 `_flocked_sidecar` 锁内，不再套 json_io 的锁。同一函数也写 PID 记录、运行时状态、停止请求，一起收私。
- **用例与变异**：`test_scoped_lock_private_permissions.py` 加两条（连续抢锁两次仍 0600；直接写记录 0600、缺失目录 0700、已存在目录不动、不留临时文件）。变异 2/2 被杀：临时文件退回 `write_text`、建目录退回裸 `mkdir`。

## 打码变量引用收口（rdv2，2026-10-04，worker/redaction-var-refs-v2，基于 rdv `a5143365b`；9b 复跑 9 样本探针通过，已并入 step17j）

- **背景**：rdv 让整段值恰好是纯变量引用（`$NAME` / `${NAME}`）时保留原文，免得复审把 `$GW_TOKEN` 看成 `<redacted>` 而误判脚本没带凭据。9b 终审查出必须改的一条：变量名原来收 `[A-Za-z_][A-Za-z0-9_]*`，于是 `$Summer2024`、`$uper_Secret_1`、`$abcDEF123xyz` 这类**以 `$` 开头、后面只有字母数字下划线**的真密码也被当变量引用放过了（4 个样本：YAML `password: "$Summer2024"`、Python `api_key = '$uper_Secret_1'`、`Authorization: Bearer $abcDEF123xyz`、`token=$ADMIN123`）。
- **3a 裁定与改动**：变量名只认**环境变量写法**（全大写 `[A-Z_][A-Z0-9_]*`，`$NAME` 与 `${NAME}` 两种）。`$GW_TOKEN`、`${GW_TOKEN}`、`$TOKEN` 照样保留；大小写混合与全小写（含 `$token`）一律打码。不另开"只对工具输出例外"的第二套口径——判定只在 `_PURE_VARIABLE_REFERENCE_RE` 一处，走全部调用方。
- **两边代价不对等**：多打一次码，最多让复审误会；少打一次码，凭据就进了模型上下文和日志。所以这一档取严。
- **已知残留边界（有意接受）**：全大写的 `$` 开头字面量（如 `$ADMIN123`）形状上与变量引用完全一致，打码分不出"值在别处"还是"值就是这个"，**当前会保留**。要在这档更严会把 `$GW_TOKEN` 这类正确脚本一起打掉，代价方向相反。由 `test_all_caps_dollar_literal_is_a_known_accepted_boundary` 钉住。
- **原交付说明需更正**：rdv 交接里"没有新增任何放行真值的路径"这句**不成立**——唯一放宽的是"整段为全大写纯变量引用"，残留边界就是上面那条全大写 `$` 开头字面量。
- **9b 建议 1 已补**：直接把"引用 + 字面量"（`$TOKEN:abc`、`${TOKEN}abc`、`$TOKEN$OTHER`）喂 `_mask_secret_value`，两层防线都钉住——`\Z` 与 `fullmatch`。要注意正则内部已带 `\Z` 时，`.match(` 与 `.fullmatch(` 语义等价（都要求吃到串尾），单独把 fullmatch 换成 match **抓不到**；真正要防的是两层同时放松，用例同时核 `pattern.endswith("\\Z")` 与 fullmatch 行为。
- **9b 建议 2（只记账，不改代码）**：JSON 字段名那条路径（`_SECRET_JSON_FIELD_RE`）是**精确字段名**匹配，与文本规则（`_SECRET_ASSIGNMENT_RE`）的"末尾片段"口径不一致——`{"db_password": "..."}` 这类带前缀的 JSON 键不在 JSON 规则命中范围。原样如此，本次不动；记在此供后续统一。
- **验证**：`test_log_redaction_variable_refs.py` 51 passed；全部 `test_log_redaction*.py` + 审计/IM 脱敏 129 passed；变异 m1（变量名退回 `[A-Za-z_]`）抓到 6 条、m2（去掉 `\Z`）抓到 2 条；m3（fullmatch→match）经查证为**等价变异**（正则自带 `\Z`），已在上文说明。见 TESTS。

## 工具输出打码不再误伤纯变量引用（rdv，2026-10-04，分支 `worker/redaction-var-refs`；ds1 初审、9b 终审必须修 1 条已由 rdv2 修好，已并入 step17j）

- **现象与根因（文件:行）**：ds10 复审 `scripts/feishu_limit/*.sh` 时读到 `GW_AUTH_HEADER=(-H "X-Gateway-Token: <redacted>")`，判成"请求头写死占位符、凭据没带上"；实际文件里是 `X-Gateway-Token: $GW_TOKEN`（3a 已用 `git show` 核实）。工具输出（读文件、跑命令）经过的统一打码路径是 `agent/tooling/output_projection.py:45` → `redact_sensitive_text(text, code_file=…)`；命中变量的两条规则都在 `agent/common/log_redaction.py`：`_AUTHORIZATION_RE`（:61，`Authorization: Bearer <值>`）和 `_SECRET_ASSIGNMENT_RE`（:62-66，`…Token: <值>` 这类赋值），两处都无条件把值替成 `<redacted>`。
- **危害不只是难看**：变量名不是秘密，被打码后复审会话会误判正确代码；模型照着看到的内容改文件，还可能把字面量 `<redacted>` 真写进去。本轮做这个修复时，我自己的多个探针脚本就因为这个机制在写入/回读时被改写，一度把结论带偏（见 TESTS 记录）。
- **改法（很窄）**：只有被打码的"值"整段恰好是一个纯变量引用——`$NAME` 或 `${NAME}`（NAME 为 `[A-Za-z_][A-Za-z0-9_]*`）——才保留原文；其余一律照旧打码，包括 `${TOKEN:-默认值}`、`$(cmd)`、`${TOKEN}abc`、`"$A$B"`、普通字面量和真密钥。新增 `_PURE_VARIABLE_REFERENCE_RE`、`_mask_secret_value`、`_strip_wrapping_quote`，接进上面三处替换点。`_strip_wrapping_quote` 只因 `Authorization` 那条正则会把结尾引号一起 captured（值形如 `$TOKEN"`），只脱一层引号，不是值的一部分；值里再拖字面量（`$TOKEN"x`）仍打码。
- **边界未变**：`code_file=True`（源码模式）本来就跳过赋值类规则，本次不动；已知密钥扫描（`_KNOWN_SECRET_RE`）、私钥、URL 明文凭证、查询串规则全部保持原样，仍在所有替换之后执行。没有新增任何放行真值的路径。
- **验证**：新增 `agent_py_agent/tests/test_log_redaction_variable_refs.py` 共 17 点（正例保留、反例照旧打码、真实形状 `curl -H "Authorization: Bearer $TOKEN"` 与 `X-Gateway-Token: ${GW_TOKEN}` 保留、真密钥仍打码、字面量 `<redacted>` 不被这条规则放行）；相关 4 个测试文件 92 passed；变异 5 个全部被杀（含额外两个：去掉引号剥离、引号剥离放宽）。命令与结果见 TESTS。
- **未验证**：真实宿主工具输出在 TUI/飞书里的最终呈现未跑（规则禁止连真实 Gateway）；`source_code` 模式下"${TOKEN:-默认值}"这类本来就不过赋值规则，属于既有口径，本次未改也不声称修好。

## 私有写整包 9b 终审修复（pbfix，2026-10-04，分支 `worker/private-bundle-fixes`，基于 17j 头 `312a4fec4`；9b 复核通过，已并入 step17j）

9b 终审结论：两条裁定（只动自己建的、有效目录链接跟随一次）在实现里都成立，5 个变异全杀，no-follow 与 TOCTOU 成立；以下 2 条必须修。

- **修复 1（回归）**：`test_memory_archive_permissions.py::test_archive_writes_create_private_json_files_and_directories` 在 `632b53592`/`2ad257314` 失败（`22e02c668`/`73de50cb8` 通过）。根因：`memory_archive/tokens.py` 先用普通 `mkdir` 建目录（0755），pdp 之后私有写不再收紧已存在目录，目录停在 0755。修法：删掉这处 mkdir（目录已由 `write_private_text_file_atomic` 内部的 `ensure_private_dir` 负责），并清理同类点——`memory_store/retention_apply._trash_conversation` 3 处（落点目录、payload 子路径、回滚源父目录）改走 `ensure_private_dir`；`agent_core/run_task_workspace_writer` 是用户工作区产出，按裁定不动。
- **修复 2（多级 0700）**：`mkdir(parents=True, exist_ok=True, mode=0o700)` 只保证最后一级 0700，中间新建各级仍是 0755（9b 实测 `a/b/c` → a、b 是 0755）。修法：把原 `json_io._ensure_private_dir` 公开成 `nofollow_fs.ensure_private_dir`（缺失段逐级经 no-follow 原语按 0700 建、已存在一律不动、有效符号链接锚点跟随一次），pdp 新增的这批建目录点改用它；紧跟着私有写的直接删掉 mkdir。
- **修复 3（Windows）**：`os.fchmod` 在 Windows 的 Python 3.12 不存在，每次加锁会抛 AttributeError；`open_private_lock_beneath_tightened` 加 `hasattr` 守卫，没有就跳过自愈（不影响其它平台）。
- **修复 4（9b 不阻塞建议）**：①注释改准确（`ensure_parent_private` 参数说明、`ensure_private_directory_chain` 的"收紧"、`json_io` 锁描述）；②"找最近的已存在祖先"循环原来复制了 6 份，收进 `nofollow_fs.split_existing_anchor`（json_io / io.jsonl / gateway io / daemon_metadata / dispatch lock / directory_lock 全部改用它）；③删除无调用方的 `io/jsonl.append_line_locked`（含 `io/__init__` 导出与三个测试文件的引用，行为由 `append_jsonl` 覆盖）。
- **用例**：新增 `test_ensure_private_dir_creates_every_missing_level_with_0700`（`a/b/c` 三级都必须 0700）；回归用例转绿；`append_line_locked` 相关用例改为覆盖 `append_jsonl`。
- **变异**：①`ensure_private_dir` 退回 `mkdir(parents=True, mode=0o700)` → 多级用例变红（`(448, 493, 493) != (448, 448, 448)`）；②`tokens.py` 加回普通 mkdir → archive 回归复红。均 KILLED、原字节还原。
- **scoped_locks 的同类点由 ds10 在另一分支处理**，本批不碰。
- **验证**：见 TESTS.md 同名节。

## Gateway scoped lock 收私（sclk2，2026-10-04，分支 `worker/scoped-locks-private-v2`，在 pbfix `3b5144114` 之上重做；含 ds1 必须改；9b 复核通过，已并入 step17j；刷新后 0644 由 sclk3 修）

- **起因**：ds3 给 9b 写终审简报时扫出 `gateway_parts/scoped_locks.py` 建锁文件用裸 `os.open(O_CREAT|O_EXCL|O_WRONLY)`（不带 mode），`mkdir` 也不带 mode，权限跟着 umask 走（umask 022 下世界可读 0644）。整包其它锁都已走 `common/nofollow_fs` 的私有锁原语。
- **改法**：`open_private_lock_beneath` 加显式 `exclusive` 参数（任务书授权在统一函数里加参数，不另写一套）：`exclusive=True` 保持 `O_CREAT|O_EXCL` 的"已存在抛 FileExistsError"语义（不打开、不改权限），默认 `False` 维持原 flock 语义。`scoped_locks._create_lock_file` 改走它（新文件 0600、新目录 0700 由原语保证），`acquire_scoped_lock` 里的裸 `mkdir` 删除（目录创建交给原语；已存在的目录一律不改权限）。portable 分支同步支持 `exclusive`。
- **同批核对的三处**：① `agent/task_progress.py:79` —— 调本模块自己的 `_write_json_file_atomic`（temp+replace 的普通状态写），**不是锁或私有凭据创建点**，未改。② `concurrency/optimistic_lock.py:28` —— `task_dir.mkdir(parents=True, exist_ok=True)` **确实是不带 mode 的目录创建点**，但它是子代理工作区里的任务目录（用户可见工作产物），不属 XDG 私有状态目录；不在本次范围，**未改**（建议作为独立一项评估，见交接报告）。③ `user_space/run_workspace.py:137` —— 那是**白名单集合**（`allowed_files` 里允许的文件名），**不是创建点**，未改。
- **验证**：见 TESTS「Gateway scoped lock 收私（sclk2）」；4 个变异（文件 mode 放宽、目录 mode 放宽、回裸 open、丢 O_EXCL）全部被新用例杀死。
- **未验证**：真实 gateway 启动路径下的锁文件权限未实测（需真实 Gateway 运行），由 3a 在沙箱外复核。
- **sclk2 返工（2026-10-04，在 pbfix `3b5144114` 之上重做，含 ds1 必须改）**：sclk 从 `lock_path.parent.parent` 反推 root，而原语要求 root 已存在；`<XDG_STATE_HOME>/my-agent` 与 `locks` 两级都不存在时（全新机器、自定义 XDG_STATE_HOME）基线能建出来、sclk 抛 FileNotFoundError。现在从 `_get_lock_dir()` 出发用 `split_existing_anchor` 拆"最近已存在祖先 + 其余缺失段"，相对段取锁目录到锁文件的整条路径（锁路径以后多一层也不会被丢）。新增 3 条用例（两级都缺、root 不随层数漂移、portable 分支独占）与 3 个变异（去掉 walk-up / 去掉 portable 独占检查 / root 回到 parent.parent）全部 KILLED。
- **另修**：`test_private_dirs_policy.py` 4 个测试名少下划线（`testensure_` → `test_ensure_`），pytest 能收集但名字不对；3a 复核 pbfix 时发现。
- **sclk2 重做 + ds1 必须改（2026-10-04）**：sclk 在 pbfix 之前从 `312a4fec4` 分叉，本次在 pbfix `3b5144114` 之上重做（`nofollow_fs.py` 冲突按"两边都要"解决：保留 pbfix 的 `ensure_private_dir`、`split_existing_anchor`、`fchmod` 守卫，加上 sclk 的 `exclusive` 参数与 `_open_lock_leaf`）。
  - **必须改**：sclk 从 `lock_path.parent.parent` 反推 root，而原语要求 root 已存在（`_open_verified_root` 第一步就 `root.lstat()`）；`<XDG_STATE_HOME>/my-agent` 与 `locks` 两级都不存在时（全新机器、自定义 XDG_STATE_HOME）基线能建出来、sclk 抛 FileNotFoundError。现在 `_create_lock_file` 从 `_get_lock_dir()` 出发，用 `split_existing_anchor` 拿"最近已存在祖先 + 其余各段"，相对段取"锁目录起、到锁文件为止"的整条路径（锁路径以后多一层也不会被丢）。
  - **用例**：状态目录两级都不存在 → acquire 成功且每一级 0700、锁文件 0600；root 跟随 `_get_lock_dir` 而非 `path.parent.parent`（锁路径多嵌一层）；portable 分支（强制 `_supports_dir_fd()=False`）独占语义与私有权限（首次 True/0600、第二次 False），含嵌套变体。
  - **变异（3 个，均 KILLED、原字节还原）**：①去掉 walk-up（`anchor, missing = lock_dir, ()`）→ 两级缺失用例变红；②去掉 portable 分支独占检查（ds1 的 M5，原分支存活）→ portable 用例变红；③root 回到 `lock_path.parent.parent` → 多嵌一层用例变红。

## B9 模板补参数级 deny 示例（tdeny，2026-10-04，worker/b9-template-deny-example，基于 17j 头 477611a6d；3a 复审通过，已并入 step17j）

- **背景**：luna2 对 trs 的初审建议——B9 写插件技能的两个模板只有"看到 `rm -rf` 升 ask"的示例，没有"看参数就直接拒绝"的示例，所以"截断不能把看到的拒绝降级"这条规则在模板上体现不出来（两种结构在模板上等价已由 luna2、ds6 确认）。
- **改了什么**：两个模板（`templates/python/src/server.py`、`templates/node/src/server.js`）的 `review_visible`/`reviewVisible` 各加一个**通用参数级 deny 示例**：命令参数里出现 NUL 空字节这类畸形内容时直接 `deny` + `MALFORMED_ARGUMENTS`。选它与现有门无关（任何精确工具参数都适用）、不照抄 rm-guard 的规则，且正好演示"看到就必须拒绝"的量级——`deny` 在截断合并前早返回，标记不会把拒绝降成 `ask`。
- **用例**：`test_write_my_agent_plugin_skill.py` 的 `_hook_requests()` 加两帧（无截断 / 带 `arguments_truncated: true`），断言都回 `deny` + `MALFORMED_ARGUMENTS`；`test_plugin_m1_b8_samples.py` 的 `_assert_truncation_behavior` 补齐 `# LLM:` 契约注释（说明截断语义、与 B5 同源、以及改动时要同步哪些文件）。
- **文档**：`references/author-contract.md` 的"截断只能更严"推荐做法补一句指向新示例。
- **变异**：删掉两个模板里的参数级 deny（退回"截断直接 ask"的旧形态）→ python/node 两条用例都变红（`deny`→`allow_as_is`）；还原后 42 passed。
- **验证**：技能测试 + B8 样例测试 42 passed / 1 skipped（作者沙箱无 node，跳过 Node 用例）；3a 沙箱外（有 node v25）技能测试 37 passed / 1 skipped，Node 模板用例真跑通过，跳过的是“B5 未合入时自动跳过”的那条；guards9 176 passed；静态门禁与 size_diff（新增 0）全过。见 TESTS。

## 17j 代码结构文档修补（cbt，2026-10-04，worker/codebase-tree-17j，基于 17j 头 1a21ae0d9，3a 核对删除行一致，已并入 step17j）

- **背景**：17j 集成分支挑进十几个分支（B4/B6/B8/B9、锁收私与私有写整包、飞书脚本凭据、凭据头守卫、G2b、dsst、只看档、phh）。各分支在自己那里改过 `CODEBASE_TREE.md`，合进来时文档冲突两边都保留，出现漏条目与重复条目。
- **核对方法**：`git diff --name-status d05a0d075 1a21ae0d9` → **37 个新增、135 个修改、无删除无改名**（所以不存在"删除/改名未同步"的情况）；再逐个把新增文件与 `CODEBASE_TREE.md` 的 Tree 段和关键文件说明段比对。
- **补了什么**：
  - Tree 段补 3 条漏条目：`agent_py_agent/tests/test_plugin_event_points.py`（B4 事件点投影）、`agent_py_agent/tests/test_plugin_event_display.py`（B6 账本与展示）、`agent_py_agent/tests/test_private_dirs_policy.py`（pdp 私有目录口径）。
  - 关键文件说明段补 2 条：B4 事件点投影与 Gateway 装配（`plugin_events/points.py` + `gateway_parts/event_points.py`）及其组件验收；`common/json_io.py` 的私有写与私有锁整包（锁收私 + 私有写入两批 + pdp）及对应测试组。私有写整包此前在说明段**完全没有条目**。
- **删了什么重复**：Tree 段内 4 条内容完全相同的重复行——`COMPACT_GENERATION_FACTS.md`、`TUI_INPUT_MEDIA.md` 各 1 条重复；`message_scan.py` 1 条（保留说明更完整的那条）。均为同清单段内同名同行文重复。
- **未改**：`LLM_GUIDE.md` 的结构速览本轮核对后未发现需要修正处；其它同名条目（各目录下的 `__init__.py`、`declaration.json`、`README.md` 等）是不同目录的各自文件，不是重复。
- **验证**：`check_doc_sync.py --base 1a21ae0d9` → DOC_SYNC_PASS；`git diff --check` 干净；ruff / import boundaries=0 / strict code-size hard=0 / size_diff 新增 0 / clean-package OK。只改文档，未跑测试。

## 插件门截断只能更严（trs，2026-10-04，3a 裁定；分支 `worker/truncation-stricter`，基于 17j 集成头 `1d394a308`；含 trs2 原因码口径和真值判断，luna2 初审通过（TESTS 归因已修 477611a6d），已并入 step17j `c74bf5893`）

- **起因**：b8tr 的“截断就直接 ask”把看得到片段里已有的拒绝也降成了确认——补丁删除段（本应 `deny`）被截断标记一冲就变 `ask`，判定被放松。3a 新裁定：截断只能更严，不能更松。
- **规则**：最终结论 = 按看得到的片段正常判出的结论，与 `ask` 两者中更严的那个（`deny` > `ask` > `allow_as_is`）。片段已 `deny` → 保持 `deny`（原原因码）；片段本来就 `ask` → **保留原原因码**、消息补半句“参数还被截断了”（3a 补充裁定：用户看到“看到了什么”比“被截断”更有用）；只有片段可放行才升到 `ask` + `ARGUMENTS_TRUNCATED`；字段缺失、`false` 或 `1`/`"true"` 这类真值（只认布尔 `true`）→ 完全照旧。
- **改动（4 处）**：两个 rm-guard 样例（Python、Node）与 B9 写插件技能的两个模板都改成“先判后并”——`review_visible`/`reviewVisible` 只看片段做原判定，`review_gate`/`reviewGate` 统一做截断合并；`references/author-contract.md` 的推荐做法改成“截断只能更严”。
- **用例与变异**：B8 截断表 5 组（含“截断+删除段→deny 保持”“截断+rm-rf→保留 RM_RF”“1/`"true"` 按没截断”）；B9 加对应帧。变异 8 个全红：原因码变异（截断一律 `ARGUMENTS_TRUNCATED`）4 处、真值变异（放宽成普通真值判断）4 处；另实测“精确退回旧结构”在模板上行为等价（模板暂无参数级 deny），如实记录。详见 TESTS.md。
- **未验证**：真实宿主截断链路与真实 TUI/IM 审批展示未跑；模板下“退回旧结构”的差异要等作者加了 deny 判定才会显现。

## 历史悬挂 run 一次性补收口迁移（rcb，2026-10-04，分支 `worker/run-closeout-backfill`，基于 rco 头 `d97bb2687`；已实现，ds2 初审可以交终审，已并入 step17j）

- **做什么**：把 rco 修好主链路之前已经停在 created 的历史悬挂记录（attempt 已结束、run/task_run 未收口）做**一次性结构化迁移**补收口——不是永久兜底清扫：迁移记录写 runtime.db 的 metadata（key=`run_closeout_backfill.v1`，含迁移 ID/执行时间/分类计数），处理完成后同一库重复调用是空操作；处理本身幂等（已补的 run 不再满足候选条件），中途崩溃后重跑只补剩余。
- **触发点**：Gateway 的 owner 维护循环（`cli/gateway_loops.py` 的 `_start_owner_maintenance_loop` → `user_space/owner_maintenance.py::run_owner_retention_if_due`），对每个 owner home 的 runtime.db 各执行一次；回执写进 maintenance.json 的 `run_closeout_backfill` 键（只加键，不改旧键）。
- **判定（只读结构化字段）**：候选 = run 未终态（''/created）+ current attempt 已结束（ended_at>0）且结束时间超过宽限下限（`RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS` = 6 小时；理由：维护默认每天一次，6h 足以让真实收口/续跑/重试先完成，又远小于"隔夜悬挂"）。逐条排除：attempt 不在静止集（终态去掉 unknown——结果不明必须等人工恢复）、有未结算操作（`unsettled_attempt_operations`）、有活跃执行权锁（`has_active_exec_lock`，持主存活即视为在跑）、无 `agent_attempt.completed` 结束事实（不新造分族判据，保守跳过——recovered 例外见下）。
- **分族（与 rco 同口径）**：ok→done、取消族（user_stop/conversation_control）→cancelled、等用户族（needs_user_input/approval_required）与可续跑族（共享 gate `should_continue_task` True）→ 不动、其余不可续跑族 → failed。rcb 侧独立实现（避免 runtime_db → agent_core 跨层 import），与 `runtime_mixin._nonterminal_run_closeout_status` 的一致性由对拍用例钉住。
- **recovered 口径（rcb-v1 修订，2026-10-04，3a 裁定）**：attempt 状态是 recovered（人工处置过 unknown、没有正常完成）且无未结算操作/无活跃锁/超宽限 → 补收口成 cancelled、runtime_reason 记 `attempt_recovered`（与 run_takeover 的 TAKEN_OVER→cancelled 同族）；判定只看 attempt 的结构化状态，不看任何文案；其余没有结束事实的记录仍跳过。迁移 ID **不升版本**（直接改 v1 规则）：生产 21 个 owner 与开发环境都没有库执行过 v1、v1 尚未并入任何部署分支，升 v2 只会留下"v1 键在但规则已变"的歧义。预览与迁移记录新增 `recovered_cancelled` 细分计数。
- **写账**：每条补收口用 `settle_agent_run` 精确 CAS（expected_attempt_status）收口 run（写 `agent_run.completed`），随后追加 `run_closeout.backfilled` 审计事件（run_id/旧状态/新状态/依据的结构化原因）；涉及的 task_run 用树终态 CAS 补关（link 闸留给发现层的补关通道——本模块没有会话存储访问，活跃 link 的 task_run 由发现层按原口径处理）。
- **只读预览**：`preview_run_closeout_backfill(db_path)` 用 `RuntimeRepository(read_only=True)`（SQLite URI mode=ro）只读打开，返回会补哪些类、各多少条、哪些跳过；不改任何字节（用例断言文件字节与 mtime 不变）。供 3a 在生产只读副本上核对数字后再决定上线。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「历史悬挂 run 一次性补收口迁移（rcb）」小节。9 项用例（含 recovered→cancelled 与 recovered+未结算/锁不动）+ 5 个定点变异（去掉在途操作检查/执行锁检查/时间下限/迁移记录/recovered 的无未结算操作检查）全部被抓住；guards9 与静态门禁全过；size_diff 新增告警 0。
- **未验证**：生产库的实际数字与执行未跑（本分支不碰生产数据）；真实 Gateway 维护循环的端到端由 3a 沙箱外复核。
- **已知边界（ds2 初审，3a 接受，不改行为）**：
  - 迁移只保证可逆，不保证不与延后续跑发生一次“终态—重开”往返：attempt 已结束、锁已释放、超过 6 小时宽限、但发现层稍后才 create_attempt 续跑的 run，会先被收成终态，续跑时由 create_attempt 重开（终态 run 不拦截）。
  - _scan_candidates 没有专门索引，owner 历史很大时按 ended_at 加索引再跑。
  - 结束事实只认 agent_attempt.completed 一种事件类型；task_run 补关的会话 link 闸留给发现层的补关通道。
  - 生产只读副本预览（10-04）：local/main 68 条候选 → failed 43、cancelled 22（recovered）、跳过 3（有未结算操作），其它 20 个 owner 都是 0。

## 回合收口不推进 agent_run/task_run（rco，2026-10-04，分支 `worker/run-closeout`，基于 step17i 头 `ce646f783`，已实现，ds1 初审 4 条小修已落实，已并入 step17j）

- **现象（结构化证据）**：隔离测试 home 的试次 runtime.db 里，主回合 Gateway 已 done（turn_end_reason=blocked，模型连续 3 次工具协议违规），但 `agent_run` 与 `task_run` 都停在 `created`；attempt 已 `done`（有 `agent_attempt.completed`，无 `agent_run.completed` / `task_run.closed`）。等 task_run 收口的测试工具等到超时。3a 在生产副本统计：agent_runs 停 created 94 条（84 条超 1 小时）、task_runs 停 created 超 1 小时 119 条，分布在 blocked / 恢复后 / 仅创建等多条路径。
- **根因（两层叠加）**：
  1. `_settle_main_agent_run_status`（`agent_core/runtime_mixin.py`）：非终态 `runtime_status`（blocked / unfinished / needs_user_input / approval_required 等）→ `terminal=""` → 只调 `settle_agent_attempt` 关 attempt，**run 保持 created**；不可续跑族的 `failed` 兜底此前只存在于 CLI 一次性 run 分支。
  2. `settle_terminal_task_run`（`conversation/task_run_closeout.py`）：run 不是终态就直接 return——**run 非终态就不关 task_run**。
  3. 子代理侧同病：`closeout_target_run_status`（`subagents/services/runtime_closeout.py`）此前只映射 DONE / FAILED / CANCELLED，BLOCKED / CHANNEL_ERROR / TIMEOUT 一律不收口。
- **修法（已实施，统一收口，不为单条路径打补丁；3a 裁定 2026-10-04）**：
  - 主代理 `_settle_main_agent_run_status`：非终态统一分族（新辅助 `_nonterminal_run_closeout_status`）——不可续跑族（blocked / 协议违规 / 执行错误 / 超时 / UNKNOWN 等，共享 gate `should_continue_task` 判定 False）尝试结束时收口 failed，把 CLI one-shot 兜底口径推广到所有路径；可续跑族保留非终态等续跑——技术续跑族（TOOL_ROUND_LIMIT_REACHED 等）由 resume/Goal 驱动，等用户族（`needs_user_input`/`approval_required`）由用户动作驱动。`runtime.db` 没有 waiting 类状态，等用户族如实保持 created，不新造状态。结束事实收成 `RunCloseoutFacts` 小数据类（只读结构化字段；参数 7→4，size_diff 原 params 告警档位变化归零）。
  - 子代理 `closeout_target_run_status`：失败族（`SUBAGENT_FAILURE_STATUSES` 减去可恢复等待 BLOCKED，即 FAILED / CHANNEL_ERROR / TIMEOUT）随 FAILED 收口 failed；**rcob 修正（2026-10-04，3a 裁定）**：BLOCKED 是可恢复等待（等批复/续派/外部输入），收口会把等待误判成结束，保持非终态——与"可续跑保持非终态"同一口径；rcb 迁移与对拍用例同步核对（子代理 BLOCKED 不产生 `agent_attempt.completed` 事实、attempt 也不终态，天然不在候选）。
  - 判定只读结构化字段（runtime_status / runtime_reason / runtime_source 与共享 gate），不解析文案。
- **收口成终态不破坏续跑（已核）**：`create_attempt` 对终态放行（`runtime_db/repository.py`，docstring 明确"run 级终态不拦截，任务级生命周期闸才是该不该重跑的裁决者"），同事务重开已关闭 task_run（`task_run.reopened` 事件）；现有测试 `test_run_audit_terminal.py`、`test_r103_run_reuse_no_split.py` 已钉住终态重开；发现层未完成扫描用任务层大写词表（`owner_wake_discovery.py`）不依赖 run 的 created；task_run 关闭另有会话 link 闸与树终态 CAS。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「回合收口统一推进（rco）」小节。四条结束路径（正常完成 / 协议违规 blocked / 取消 / 执行错误）+ 等用户族 + 可续跑族 + 子代理映射与端到端共 9 项用例；两个定点变异（去掉统一收口、去掉 BLOCKED 映射）均被抓住；相关回归 15 文件 303 项全绿；guards9 与静态门禁全过；size_diff 新增告警 0。
- **历史记录（不修数据，后续项）**：已经停在 created 的历史悬挂记录（判定条件：attempt 已终态（`ended_at>0` 且无在途操作）且 `agent_run.status ∈ {'', 'created'}` 且无执行权锁）由 owner 维护 / 发现层补收口为对应终态（failed / cancelled 按结构化 `runtime_reason` 选），判定只读结构化字段，不解析文案；本分支不修数据。
- **未验证**：真实 Gateway / TUI / IM 端到端与生产历史悬挂记录补收口未跑（规则禁止连真实 Gateway、不读生产数据）；留给 3a/9b 复核。

### 补（rcob2，2026-10-04，分支 `worker/rco-blocked-halt-v2`，基于 17j 头 `a1a849c55`；3a 复审，已并入 step17j）

- **现象**：从 rco + rcb 合入（`39350230a`）起，`test_subagent_authorization_failure_halt.py::test_child_stops_blocked_and_parent_receives_structured_lifecycle_event`（:358）与 `test_subagent_identical_failure_halt.py::test_fake_llm_child_stops_blocked_and_parent_wake_carries_identical_reason`（:164）两条失败，都是 `assert (result.status, result.turn_end_reason) == ("BLOCKED", "blocked")` 实得 `("RUNNING", "")`；rcob（`ebe621d87`）没有修好它们。已在 `312a4fec4`（rco 前）与 `39350230a`（rco+rcb）两个分离检出上复核：前者两条通过、后者两条失败。
- **根因**：子代理 `task_local` 回合的 `RunParams.run_id` **就是子代理自己的 canonical run id**（`subagent/run_flow.py:515`），回合结束时同样走 `_settle_main_agent_run`（`runtime_mixin.py:692`）。rco 把"不可续跑族收 failed"从只作用于 CLI one-shot 推广到**所有路径**（`runtime_mixin.py:484-489`），于是 halt 收口那轮的 `runtime_status=blocked` / `runtime_reason=REPEATED_TOOL_AUTHORIZATION_FAILURE` 被主代理侧收口逻辑落成 `run.status=failed`。随后 runner 结果落盘走准入 `reject_stale_runner_result` → `_managed_runtime_result_conflict`（`runner_result_admission.py:130-146`），判定"与运行账终态冲突"（`run=failed`、incoming 非终态），拒收这条合法结果，返回 `make_rejected_runner_result`；它把仍停在 RUNNING 的 task 状态回填给调用方，`run_subagent` 于是返回 `("RUNNING", "")`，父级也拿不到 BLOCKED 收口。逐层证据见探针 trace（`_settle_main_agent_run` → `settle_agent_run(status=failed, payload runtime_status=blocked)`）。
- **修法（最小、只堵这条越界）**：`_settle_main_agent_run` 在 `context_scope == "task_local"` 时直接返回——子代理 run 的权威终态只由 `commit_runner_result` / `runtime_closeout` 在结果落盘后按 runner 结论收口（rcob 已把 BLOCKED 排除出失败族），主代理回合收口不该代劳。主代理与 CLI 路径的收口分族完全不变。**不改测试预期**，两条用例钉住的合同（halt 以 BLOCKED 收口、父级收到 BLOCKED + 原因码）原样保留。
- **两侧合同同时成立**：halt 两条转绿；rcob 的 `test_blocked_runner_result_does_not_settle_runtime_run` 继续绿（BLOCKED 仍不进失败族、不被 settle 成 failed）。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「rco 的子代理 halt 收口回归（rcob2）」小节。3 个变异全被抓：①把 task_local 早返回删掉 → 两条 halt 用例红；②把 BLOCKED 放回失败族（`_RESUMABLE_WAIT_TASK_STATUSES` 置空）→ rcob 那条红；③把判据放宽成对所有 scope 生效 → 主代理收口 8 条用例红（证明判据只对子代理生效）。
- **既有失败（非本片引入）**：`test_dispatch_liveness_and_revive.py::test_supervision_kills_fully_stalled_source_worker_host:893`（`running_reclaimed` 期望 2 实得 0）在 `312a4fec4` 与 `39350230a` **两个基线**上同样失败，与本片改动无关，照实保留、未跳过。
- **未验证**：真实 Gateway 端到端未跑（规则禁止连真实 Gateway）；留给 3a/9b 复核。

## 宿主私有写入第三批（pw3，2026-10-04，分支 `worker/private-writes-batch3`，基于 17j 头 `1a21ae0d9`；pw3r 已搬到 17j 头 `35b1647e8`；ds1 初审、9b 终审通过（含 pw3t、pw3t2），已并入 step17j）

- **背景**：9b 终审私有写整包时指出，下列写入点带了内容（提示词、模型输出、回复、任务摘要），却还没走私有写——新文件 0644、有的也不是原子写；ds3 简报第 3 节另列了一张同类表。
- **改法**：逐个改走整包现有私有写函数（新文件出生 0600、缺失目录按 0700 建、原子替换），格式、调用方语义、读取路径不变：
  - Gateway：`http_handlers.py` 旧 /ask（无幂等 ID）inbox 写入 → `write_private_json_file_atomic_no_newline(sort_keys=False)`；`stream_writer.py` 流事件追加 → `append_private_text`，chunk 目录 0700；`adapter.py` outbox 三处改用已私有的 `write_json_file_atomic`，adapter 五目录 0700；`adapter_late.py` 迟到登记追加/重写 → 私有 append/私有原子；`request_history.py` message_repairs 改用已私有的 `write_json_file_atomic`；`scoped_locks.py` 的锁记录 0600/锁目录 0700 在 17j 已由 sclk（`open_private_lock_beneath` exclusive）覆盖，本批未重复改（pw3r）。
  - 会话与存储：`task_progress.py` Todo 账本 → `write_private_json_file_atomic_no_newline`（与旧输出逐字节一致：indent=2、sort_keys、无尾换行）；`store_tasks.py` 任务摘要 → `write_private_text_file_atomic`；`session/manager.py` session.json → `write_private_json_object`、会话根 0700；`session/cross_channel.py` channels.json → 私有原子（indent=2、不排序、无尾换行）。
  - 子代理与运行数据：`subagents/services/persistence/{projections,service,agent_run_state}.py` 投影/状态/locator → 私有写；`local_storage/records.py` blob 正文 → `write_private_text_file_atomic`；`common/rotating_log.py` 主日志追加与 sidecar → 私有 append/私有原子；`tooling/background_process_launch.py` 启动 spec → `write_private_json_file_atomic`（去掉显式 mkdir/chmod，交给私有原语）；`user_space/home_backup.py` 备份清单 → `write_private_json_object`；`user_space/home_layout.py` 配额策略迁移 → 私有原子。（pw3r：`concurrency/optimistic_lock.py` 的版本锁记录与任务目录恢复原样——ds10/ds1 裁定该目录在子代理工作区（用户可见产出区）、非私有范围；17j sclk 段已登记“非私有状态目录，未改”。）
- **建目录口径**：能由私有写函数顺带建缺失目录的一律交给它们（rotating_log、background_process_launch、local_storage、home_backup、subagents locator、adapter_late）；显式准备的目录（chunk、adapter 五目录、session 根、通道目录）在 pw3r 已改用 pbfix 的统一原语 `nofollow_fs.ensure_private_dir`（缺失段逐级 0700、已存在一律不动），不再用 `mkdir(mode=0o700)`（它只保证最后一级）。
- **保留原样（理由）**：`home_layout.py` 的初始化种子写入（`_write_seed_file`/`_write_seed_json`/模板升级）——写的是用户直接阅读编辑的文档种子（SOUL/USER/AGENTS/记忆模板），属用户资产，保持默认权限；`memory_store/migration.py` 一次性迁移工具本批未动（写迁移目标与记录，需单独判定）；`optimistic_lock.py` 按裁定恢复原样（见上）、`run_workspace.py:137` 是白名单集合不是创建点；`gateway_parts/io.py` 的 `write_json_file` 定义保留（本批只把 adapter、request_history 两个调用方迁到私有原子版）。
- **验证**：见 TESTS.md 同名节（pw3 轮 3 个变异——流事件追加、Todo 落盘、迟到登记各退回普通写——全部被用例杀死；pw3r 在 17j 头 `35b1647e8` 复跑：新增 14 条用例 + 相关回归 459 passed / 1 skipped + guards9 与静态门禁全过）。
- **pw3r 搬运说明（相对原 `cab1f7702`）**：① `scoped_locks.py` 未搬——17j 的 sclk2/sclk3 已用 `open_private_lock_beneath(exclusive=True)` 覆盖本批要做的锁记录 0600/锁目录 0700；② `constants_catalog.json` 未搬目录变更——17j 已由 3a 重新生成，本批在 17j 上重新生成核对（897 项一致）；③ `optimistic_lock.py` 恢复原样（裁定见上）；④ 显式 mkdir 点（chunk、adapter 五目录、session 根、通道目录）改用 pbfix 的 `nofollow_fs.ensure_private_dir`；⑤ 其余 17 个产品文件、测试与文档段按原样搬入。
- **未验证**：Windows/无 dir_fd 平台未实测（与整包同一残余风险）；存量旧文件只在下次写入收紧；`http_handlers.py` 旧 /ask 与 `request_history.py` message_repairs 两点由同原语+静态核对覆盖，没有各自独立的端到端用例（入口深、依赖 handler/agent 装配）。
## 返工提示转检查程序 hint（phh，2026-10-04，worker/pack-host-hints，基于 step17i 头 d05a0d075，ds7 初审可以交终审（变异 5/5），9b 看脱敏与清洗两处，已并入 step17j）

- **背景**：能力包重跑里 B 包两例不合格，都是交接文件的一个字段填错；检查程序报了错、宿主核验判失败、返工了好几次，模型还是改不对——返工提示里每条错误只有 code 和 location，看不出“应该填什么”。ds7 在做 B 包 0.3.2：检查程序的报错会带上期望值（hint），宿主不转模型就看不到。
- **统一合同（3a 2026-10-04 定，ds7 按同一合同改检查程序）**：检查程序 host JSON 输出里，每条错误可以带一个可选字段 `hint`（字符串）；宿主生成返工提示时，有 hint 就附在这条错误后面——去掉控制字符和双向控制符、换行压成空格、最多 200 个字符（超了截到 199 加省略号）；没有 hint 的错误照旧；只转 hint 这一个自由文本字段，别的自由文本一律不转；判定和计数照旧只看 code，不看 hint。
- **改了什么**：
  - `capability/pack_verifier_runner.py`：`_error_samples` 读取可选 `hint`，经 `_clean_hint` 清洗+截断后存进错误样例（无 hint 或清洗后为空不写键，旧形状逐字不变）；hint 不参与判定（`_code_counts`/`valid` 逻辑不动）；`MAX_VERIFIER_HINT_CHARS=200`、`_BIDI_CONTROLS` 为新常数。
  - `capability/pack_verifier_redaction.py`：`redact_error_samples` 对 hint 复用与 location 相同的宿主路径替换表（新 `_redact_sample`），仍含宿主路径的整条置 `<redacted>`（与 location 同口径；hint 与 location 同源同类，不能开新泄露口）。
  - `capability/pack_verification_service.py`：`_rework_text` 用新 `_sample_text` 把 hint 附在对应错误后面（`code @ location（hint）`）。
  - 宿主通知（HostNotice）不带 hint：`pack_verification_notice_text` 只读计数/状态字段、不读 error_samples，本批不改它。
- **处理位置说明**：清洗与截断放在解析层（`_error_samples`）而不是提示拼装层——hint 与 location 一样会进账本、写工具回执和返工提示，入口统一规范化可避免同一字段四处各自处理；判定/计数仍只看 code。
- **验证**：见 TESTS.md 同名节（新增 5 条用例，变异 3 个全 KILLED）。
- **未验证**：真实沙箱链路（本机沙箱不允许嵌套 Seatbelt，两条真实沙箱用例 skip，3a 在沙箱外复核）；ds7 的新检查程序端到端（B 包 0.3.2 重跑时看）。

## 屏幕观察只看档（vho，2026-10-04，分支 `worker/vh-observe-only`，基于生产版 `d05a0d075`；3a 真机实测通过（全新环境按清单安装、只交 observe_window、解锁时真实观察 44 个候选），待交叉复审与 9b 终审，已并入 step17j）

- **起因**：生产按计划只装了观察要用的依赖（pyobjc 三件套、mss、rapidocr、pillow），没装 pyautogui 与 computer-control-mcp。3a 真机核对发现：按生产开关组合起适配器，`computer_use_server.py` 顶层 `import pyautogui` 直接失败，适配器退出，一个工具都交不出来——观察核心本身没问题。
- **档位（3a 定，显式不是兜底）**：`computer_use_enabled=false` + `computer_use_observation_enabled=true` 定义为"只看档"：同一个服务只交出 `observe_window`，审批照旧 always 每次都问本人；不交出 `click_candidate` / `type_into_candidate`，不交出任何上游工具；适配器进程不 import pyautogui / computer_control_mcp。其它三种组合行为不变。
- **改法**：宿主侧 `computer_use_mcp_servers` 新增 `observe_only` 结构化档位（`core.py` 由两个开关派生），只看档用单独一份声明表（一眼可读档位边界）；服务环境写 `MY_AGENT_COMPUTER_USE_OBSERVE_ONLY=1`。适配器侧 `computer_use_server.py` 把上游 import 全部收进 `_load_upstream()`，只在完整档调用；`build_adapter_server` 按标记与档位只注册 `observe_window`。没有 try/except ImportError 兜底——完整档缺依赖照旧明确失败。
- **跨调用点一致**：`computer_use_mcp_servers` / `with_computer_use_observation` 在仓库里只有 `core.py` 一处生产调用点（`/tools` 展示与工具目录都从同一 registry 派生），档位在这一处定死。
- **验证**：见 TESTS「屏幕观察只看档（vho）」；5 个变异（只看档泄漏点击工具、适配器忽略只看档、宿主丢弃只看档、标记写反、上游 import 回到模块顶层）全部被用例杀死。
- **vho 补丁（2026-10-04，同一分支）**：3a 真机实测暴露两处沙箱测不到的问题。① `computer-use-observe` 清单漏了 MCP 的 Python SDK——适配器顶层 import 它，生产原来靠上游 `computer-control-mcp` 带进来，本 extra 没装，只看档直接 `ModuleNotFoundError`；两个平台各补 `mcp==1.13.0`（与上游钉的同一版）。② 宿主/适配器接缝断掉：只看档宿主只写只看标记（`with_computer_use_observation` 见它早返回，不再写观察标记），适配器却只认观察标记，于是 `tools/list` 是空的；改由 `observation_tools_enabled` 认两个标记中任意一个（只看标记本身即意味着要装载观察工具），注册范围仍由档位收窄。补两条守卫：扫只看档路径的第三方 import 核对 extra 清单（含未登记包的反向检查），以及**不手写环境变量**的接缝用例（拿宿主产出 env 直接喂适配器）。
- **vho 补丁二（2026-10-04，锁屏）**：3a 真机在锁屏状态调 observe_window，拿到的是 `OCCLUDED`（"窗口被上层窗口完全盖住"）——结论没错但对模型和用户说明不清。macOS 后端现在在列窗口之前先查锁屏（`CGSSessionScreenIsLocked`，以及活动显示器数为 0），命中返回 `screen_locked`，消息"屏幕已锁定，解锁后再观察"；检查只读、不弹窗，查不到按"无法确认"继续原流程（不因查不到锁屏而拒绝观察）。位置在屏幕录制权限预检之后、列窗之前。Linux X11 无此概念，行为不变。错误码在设计稿码表与 `screen_observation.py` 模块头登记（观察码是开放集合，宿主只提升 stale / not_found）。
- **vho 补丁三（2026-10-04，执行面 fail-closed；ds2 初审通过，变异 5/5，已并入 step17j）**：ds3 初审通过，但提了一条生产前要做的纵深建议——"只看不点"原来只在目录层成立（`tools/list` 一个工具 + 宿主只转发目录里的工具）；接管层 `call_observation_tool` / `observation_call_handler` 不看档位，绕开目录直接点名 `click_candidate` / `type_into_candidate` 会真的执行 observer 的点击、输入（ds3 只读探针 P2 实测 `is_error=False`）。现在执行层按同一个结构化环境标记 `MY_AGENT_COMPUTER_USE_OBSERVE_ONLY` 收窄：只看档下除 `observe_window` 外的工具名一律返回结构化错误 `tool_not_available_in_observe_only`，不调用 observer 的任何方法。档位在装配处读一次并透传（`build_adapter_server` → `install_observation_handler` → `observation_call_handler`），不新增来源、不按工具名或调用方身份猜。4 个变异（删执行层检查、检查永不触发、handler 不传档位、目录层忽略只看档）全部被用例杀死。
- **本补丁的验证边界**：同前几轮——沙箱里只做装配级与接管层单元验证（`test_computer_use_observe_only.py` 12 条 + J16 全组 202 passed / 7 skipped）；真机适配器子进程与真观察仍由 3a 在沙箱外跑。

## B9 用例临时目录清理容忍访达 .DS_Store（dsst，2026-10-04，分支 `worker/b9-test-dsstore`，基于 17j 头 `1d394a308`；3a 复审通过，已并入 step17j）

- **起因**：3a 在 17j 上跑 `test_write_my_agent_plugin_skill.py` 时偶发 `OSError: [Errno 66] Directory not empty: .../tmp/b9-m1b9-xxxx/release-source/agent_py_agent`——`author_workspace` 把临时目录建在工作树 `tmp/` 下（沙箱只能写工作树，wheel/pip 临时文件也得放这儿），清理时正好撞上访达往目录里写 `.DS_Store`，`rmtree` 报非空。单独重跑就过，但只要有人在访达里浏览，门禁就可能随机变红。
- **改动（只动这一个测试文件的夹具与清理，模板用例未动，方便 ds6 在 `worker/truncation-stricter` 上改模板用例后合并）**：
  - 新增 `_rmtree_tolerating_finder_metadata(path)`：先正常 `rmtree`；失败且现场只剩 `.DS_Store`/`.AppleDouble`/`.LSOverride` 时删掉这些再重试一次；其它情况把原始错误抛出。**没有用 `ignore_errors=True`**，真错误不会被吞掉。
  - 夹具本体抽成 `_isolated_author_workspace(monkeypatch)` 上下文管理器，`author_workspace` 夹具保留原名只做转发；这样用例能直接验"用完是否留下临时目录"，不用跟 pytest 内部结构较劲。
- **.gitignore 结论（任务第 2 条）**：**`tmp/` 已经在忽略名单里**（`.gitignore:77`），`git check-ignore -v tmp/` 命中、`git check-ignore -v tmp/.DS_Store` 也命中。3a 说的"不在忽略名单"是命令写法差异造成的误判（不带尾斜杠、目录又不存在的 `git check-ignore tmp` 不命中）。**无需新增忽略规则**。`check_clean_package.py` 不受影响：它按 Git tracked/untracked 事实与名称模式判定，`tmp/` 不在它的 `RUNTIME_ROOTS` 里，`.DS_Store` 本来就在它的 `DIRTY_NAMES` 里。
- **验证**：本文件 36 passed / 2 skipped（Node 解释器缺失与 B5 文件缺失，均为设计内跳过）；变异（去掉 `.DS_Store` 重试路径）→ 新用例以 `Errno 66 Directory not empty` 变红，还原后复绿。跑完确认 `tmp/` 下无 `b9-m1b9-*` 残留；访达写在 `tmp/` 根的 `.DS_Store` 不属于夹具清理范围（且已被忽略）。命令与结果见 TESTS。
- **未验证**：真实访达竞态只能在用户机器上自然发生；本地用打桩 `rmtree` 首轮抛 Errno 66 的方式模拟，没有在真开着访达浏览的机器上复现过原始偶发失败。

## 脚本凭据头不得写死字面量（hdrg，2026-10-04，分支 `worker/header-literal-guard`，基于 17j 头 `cb16433d7`；3a 复审通过，已并入 step17j）

- **起因**：ds10 初审 flc 守卫（`test_scripts_gateway_http_calls_carry_credentials`）时指出判据太宽——只看逻辑行里有没有出现过 `x-gateway-token` 字样，所以 `-H 'X-Gateway-Token: 固定串'` 这种写死字面量也能过。flc 脚本本身是对的（用 `$GW_AUTH_HEADER`），但同类写法出现在别的脚本里守卫会假绿，G2b 打开后变成真故障（每个部署发同一个编死的假串，请求全被当匿名）。
- **改动（只动守卫与自检样本，产品脚本与产品代码一行未动）**：判据升级成"凭据头的**值**必须来自变量引用或产品入口"：
  - shell：`SHELL_CREDENTIAL_HEADER_RE` 取 `X-Gateway-Token:` 之后的取值，必须以 `$` 开头（`$VAR`/`${VAR}`）或含数组展开 `@]` 才算变量，纯字面量判违规；违规消息区分"值写死"与"根本没带头"。
  - Python：`_python_calls_credential_entry` 用 AST 判该调用所在作用域里**真的调用了** `gateway_script_headers()`/`gateway_client_credentials()`，不再是把名字写进字符串或注释就算。
  - **顺带修掉一个原有漏洞**：`_credential_variables` 原先"行里有 `x-gateway-token` 就收集行内全部 `$VAR`"，会把同行 `$GW/ask` 里的 `GW` 也当凭据变量，写死字面量靠它混过判据；现在只认**取值位置**引用的变量。
- **自检样本**（`test_gateway_script_credential_rule_self_check`）两侧都补：写死 `<redacted>`、写死普通串、写死 `abc123`（shell 与 Python 各一条）必须红；`$VAR`、`${VAR}`、数组展开、`gateway_script_headers()`、`gateway_client_credentials()` 必须放行。
- **验证**：守卫文件 12 passed（含新样本）；全仓 `scripts/` 扫描无误报；变异两处——①判据退回"有字样就算"→ 写死样本变红；②在真实脚本 `d3_quota_selfheal.sh` 里把数组展开换成写死值 → 守卫精确报出 `:218 ... 写死成了字面量`。还原后复绿，`bash -n` 通过，`git diff --stat scripts/feishu_limit/` 无输出（脚本零残留）。命令与结果见 TESTS。
- **未验证**：静态判据只覆盖到"变量引用"这一层；更绕的间接写法（变量里存整条 header 再展开）当前 `scripts/` 下没有，未设计对应判据。

## B8 样例处理截断标记（b8tr，2026-10-04，分支 `worker/b8-delete-gate`，基于 B8 头 `9461868c6`；已并入 step17j；3a 新裁定“截断只能更严、不放松已看到的拒绝”待返工）

- **起因**：B5 给收紧请求加了截断标记（sol3 `57f465dca`）：声明 `arguments: "full"` 的门，参数超出 4000 字预算时请求的 call 带 `arguments_truncated: true`，未截断为 `false`，`arguments: "none"` 时没有这个字段。样例插件此前只看得到的片段判——截断时可能误 allow（漏看危险部分）或误判。
- **改动**：两个 rm-guard 样例（Python、Node）的两个门在 gate 命中后先看 `call.arguments_truncated`：精确为 true 时不再按看到的片段判，直接回 `ask`（原因码 `ARGUMENTS_TRUNCATED`，消息"参数太长被截断，看不全，先确认一次"）；`false` 或字段缺失（旧宿主）照旧。判定仍只读结构化字段。
- **用例与变异**：共享用例表加 4 组输入（两个门 × 看到的片段"无害/危险"各一），每组同时断言字段缺失、显式 false 照旧与 true 转 ask；Python、Node 各跑一遍。两个实现各自去掉截断检查的变异都被新用例抓到，原字节还原校验通过。
- **未验证**：真实宿主截断链路（需 B5 并入后的宿主侧）与真实 TUI/IM 审批展示未跑；本样例只按第 8 节协议直连测试。命令与结果见 TESTS.md。

## B8 删除门改拦 apply_patch（b8dg，2026-10-03，3a 裁定；分支 `worker/b8-delete-gate`，基于 B8 头 `a0ec1b0dd`；已并入 step17j）

- **起因**：b8align 对接核对发现宿主没有 `delete_file` 工具（宿主工具全集里没有它，`git log --all -S 'name="delete_file"'` 也搜不到历史），两个 rm-guard 样例的 `guard-delete` 门在真实宿主永不命中；模型删文件走 `apply_patch` 的 `*** Delete File: ` 段或 `run_command`。3a 裁定选 A：门改拦 `apply_patch`。
- **做法**：`guard-delete` 的 `tools` 从 `["delete_file"]` 改成 `["apply_patch"]`、`arguments` 从 `none` 改成 `full`（要读补丁文本）；判定只认行首严格是 `*** Delete File: `（冒号后一个空格）的删除段头，与宿主 `_filesystem_patch.py` 的解析同款（先归一换行再按行切分，不做子串匹配，正文行里的同样字样不算）；有带路径的删除段回 `deny` + `DELETE_FILE_BLOCKED`，只改不删的补丁回 `allow_as_is`，补丁字段缺失/非字符串/空路径按 `ARGUMENTS_UNAVAILABLE` 回 `ask`。Python 和 Node 两版行为逐条一致；旧 `delete_file` 声明删掉不留兼容。
- **用例**：`_DELETE_PATCH_CASES` 9 条（删除段、Update/Add、多段含删除、加号行/上下文行字样不算删除、CRLF 与 `\r` 归一、空路径）＋补丁字段缺失/无 patch/非字符串 3 条 ＋ 旧 `delete_file` 工具名回 `OUT_OF_SCOPE`；清单断言改 `apply_patch` + `full`。
- **变异**：5 个全部被抓到（Python/Node 删除段判定漏掉、Python/Node deny 写 allow_as_is、门工具名写回 `delete_file`），原字节还原校验通过。
- **设计稿同步**：`docs/design/PLUGIN_EVENT_HOOKS.md` 第 4 节示例、第 13 节 B8 行、第 14 节验收步骤、第 16 节风险句全部改按 `apply_patch`；第 5 节 observe 示例补上 `channel`/`thread_ref`/`actor` 三个公共字段（与第 6 节、B3 实现对齐）。
- **边界**：本分支不含 B3–B7，真实验收（TUI、飞书）待合入后另派；沙箱里启用链失败与基线 `a0ec1b0dd` 一致（既有环境限制）。

## M1 B8 样例（m1b8，2026-10-03，分支 `worker/m1-b8-samples`，基于 `claude/3a-step17i` `c47d023b6`；初审通过、3a 沙箱外 275 passed，已并入 step17j，真实验收待 B3–B7 合入）

- **做了什么**：三个 v8 样例插件。`plugins/event-watch/`（Python）订阅全部 6 类事件（只读结构化事实、不要正文），在只读 table 面板显示每类"收到"与"合并丢弃"（`dropped_before` 累计）；`plugins/rm-guard/`（Python）与 `plugins/rm-guard-node/`（Node）在 `run_command` 出现 rm 加 -r 和 -f 的组合（`-rf`、`-fr`、`-r -f`、`--recursive --force` 等）时回 `ask`（`RM_RF`），`delete_file` 直接回 `deny`（`DELETE_FILE_BLOCKED`），其它命令 `allow_as_is`。三个包都过 v8 校验与构建脚本。
- **用例**：`agent_py_agent/tests/test_plugin_m1_b8_samples.py`——假宿主按第 5 节协议直接和插件进程对话（不经 Gateway）：三个包构建与 v8 校验；event-watch 握手位、6 类计数、`dropped_before`、面板只读（幂等、不吐正文）；两个收紧样例的 rm 写法、普通命令、delete_file、回复格式与向前兼容；Node 用例无 node 时跳过。
- **变异**：7 个全部被抓到（Python/Node 漏 `-fr`、`ask` 写 `allow_as_is`、`delete_file` 不拒、计数不加 `dropped_before`、面板带出正文、未知类型计错）。
- **初审补强（b8t，2026-10-03，只改测试）**：采纳 b8r 初审两条补强——①共享用例表补 `rm -Rf` 大写变体（Python/Node 同时覆盖）；②三个样例握手断言改精确集合比较（多声明未实现的能力位也会被抓到）。两条各一个提交；对应变异复跑均被抓（见 TESTS.md）。
- **边界**：本分支不含 B3–B7，**真实验收（TUI、飞书各一遍）待 B3–B7 合入后另派**；启用链在命令沙箱里因 `plugin_endpoint_failed` 失败（已知环境限制，交 3a 沙箱外复核）。详见 TESTS.md 同名小节。

## B9 模板处理截断标记（b9tr，2026-10-04，分支 `worker/b9-facts-table`，基于 B9 头 `10c40256f`；luna6 初审小问题已随 trs2 补，已并入 step17j）

- **起因**：B5（sol3 的 `57f465dca`）给收紧请求加了截断标记——声明 `arguments: full` 的门，参数超出 4000 字预算时宿主在请求的 `call` 里带 `arguments_truncated: true`，完整是 `false`，`arguments: none` 的门没有这个字段（`plugin_events/tool_gate.py` 的 `PluginToolGate.request_payload`）。B9 的两个作者模板此前不看这个字段，会拿被截断的残片做判断并放行，形成绕过面。
- **改了什么（产品代码一行未动，只动技能资产与用例）**：
  - Python `templates/python/src/server.py` 的 `review_gate`、Node `templates/node/src/server.js` 的 `reviewGate`：在 `OUT_OF_SCOPE` 之后、`ARGUMENTS_UNAVAILABLE` 之前加一个早返回——`arguments_truncated` 严格为 `true` 时回 `{verdict: "ask", reason_code: "ARGUMENTS_TRUNCATED", message: "参数被截断，看不全命令内容，先确认一次"}`；字段缺失或 `false` 走原路径，行为不变。
  - `references/author-contract.md` 新增「参数截断标记 `arguments_truncated`」一节：三态表格（`true`=看不全 / `false`=完整 / 字段不存在=该门不是 full），并写明推荐做法——依赖完整参数的门截断时必须回 `ask`，否则别人可以用填充内容把危险参数挤到 4000 字之后绕过；字段缺失或 `false` 照旧（`false` 就是完整，不必额外保守）。
  - `SKILL.md` 速查表下加一句指路，让写 `tool_gates` 的作者知道要处理这个字段。
- **用例**：`test_write_my_agent_plugin_skill.py` 两处——①`_hook_requests` 追加 id 15/16 两条 review 请求（截断 `true`、`false`），`test_templates_execute_packaged_stdio` 断言前者回 `ARGUMENTS_TRUNCATED`、后者仍按 `rm -rf` 回 `RM_RF`（证明 `false` 不误伤）；②新增 `test_author_contract_truncation_field_matches_tool_gate_implementation`，校验文档字段名、推荐原因码和两个模板分支都与 B5 实现同源，**B5 的 `tool_gate.py` 未合入本分支时 `pytest.skip`**（分支差异不是错），合入后自动生效。
- **验证**：本分支 34 passed / 2 skipped（跳过的是 Node 无解释器、B5 文件缺失）；PY 模板去掉截断分支 → stdio 用例失败并精确报出 `ARGUMENTS_TRUNCATED` 期望值与 `RM_RF` 实际值；在临时 worktree 放入 B5 的 `tool_gate.py` 后，那条 skip 的用例真跑**通过**；再把文档字段名改成 `arguments_cut` → 用例失败。还原后复绿，临时树已删。命令与结果见 TESTS。
- **未验证**：模板的实际宿主联调（B5 打开 `arguments: full` 截断后真实插件进程收到 `true`）本轮没跑——B5 不在本分支、也不启动 Gateway；由 3a 在 17j 合入后于沙箱外按真实链路复核。Node 模板的 stdio 执行在本机 pytest 环境被跳过（`shutil.which("node")` 找不到），只做了 `node --check` 语法验证与文件级断言。

## B9 补事件字段表（b9ft，2026-10-03，分支 `worker/b9-facts-table`，基于 B9 头 `cd9e678f6`；已并入 step17j）

- **起因**：b9align 的 B9↔B3/B4/B5 对接核对里第 2 条改进（3a 采纳）——技能正文没有逐字段列观察事件的字段名，模型写插件时容易自己猜，猜错就读不到数据。
- **改了什么**：`references/author-contract.md` 新增「观察事件字段表（别猜字段名）」一节，三块内容：①九个公共字段（`event_id`/`type`/`seq`/`occurred_at`/`dropped_before`/`channel`/`thread_ref`/`actor`/`facts`）各自含义；②六类事件各自的 `facts` 字段与逐字段含义；③正文（`content`）规则——只有 `prompt_submitted` 可能带正文，需清单声明 `content: "text"` + 用户在确认码里同意，脱敏后截 4000 字，其余五类一律无正文、工具调用只给 `args_hash`。表里注明**以实现为准**（公共字段来源 `plugin_events/protocol.py`，`facts` 来源 `points.py` 的 `_EVENT_FACT_FIELDS`），并写明 B4 定稿后 3a 再核一次。`SKILL.md` 速查表下加一句指路：写观察事件插件先看这张表、不要猜字段名，且**不认识的 `type` 要跳过、不认识的字段要忽略**（向前兼容）。
- **用例**：`test_write_my_agent_plugin_skill.py` 新增 `test_author_contract_event_table_matches_event_point_projection`——用 AST 读 `points.py` 的 `_EVENT_FACT_FIELDS`，与从 `author-contract.md` 解析出的表逐类逐字段比对。B4 的 `points.py` 未合入本分支时 `pytest.skip`（分支差异不是错），17j 合入后自动开始严格比对。
- **验证**：本分支该用例跳过、其余 43 项通过；另在 `git worktree` 临时树里放入 B4 的 `points.py` 模拟 17j 合并，用例**通过**（证明合入后真的会比对）；两个变异（把表里 `args_hash` 写成 `arg_hash`、删掉 `command_executed` 整行）**均被用例抓到**，还原后复绿。命令与结果见 TESTS。
- **未落地**：表的正确性依赖 B4 的 `points.py` 最终定稿；**B4 定稿后要重跑这条用例确认仍绿**，若 B4 改了字段，本表与用例会同时红，按实现改表即可。

## M1 B9：插件作者技能按清单 v8 完成（2026-10-03，m1b9，`worker/sol2-m5`，实现 `de395322e`，已实现，主体 be 复审通过，文案尾补 3a 已核；已并入 step17j）

- **be 复审后尾补（3a，2026-10-03）**：`11427c22c` 无必须改项。按 3a 接受的两条说明，明确 v1–v5 wheel 构建期工具有意允许仓外本地工程、后端会执行构建钩子；授权工作区由会话沙箱和调用方保证，脚本不做路径围栏，安装/启用不用它。同时提醒不需提示文字时将该事件 content 降为 none，缩小确认范围。只补模块注释/docstring、技能及参考，不改构建门或模板默认值；验证见 TESTS。

- **来源**：原 M5 两个 WIP 变基到 B1 已合入的 `add244a92`；B9 只升级作者合同、模板与验收，不实现 B7 宿主接线。
- **实现**：工具/面板与观察/工具收紧三类指引；Python、Node 同为 v8 文件入口。正文仅提示事件，工具开始只给结构化事实；full 只订精确工具、空 effects；network 默认 false。双能力握手、观察空回执及三个合法裁决均有包内协议测试。
- **边界**：B7 前 v8 不能生产启用，返回 `plugin_events_disabled`；强制沙箱、默认断网、收窄读及仅 local/main 是启用前置合同。模型不安装、不启用、不取码代填；用户本人确认范围包括网络与订阅，不能借 shell、内部 API、界面或子代理绕过。
- **验收**：真实构建/读包/包内 stdio、纯订阅合法包、模型管理快照不可见；保留生产 wheel 资源原字节及九包旧新构建器字节回归。四类双语言真实副本变异全部抓到，同场景纸上复测补齐旧技能的 v8 知识缺口。
- **证据与未验**：命令、结果及工作树 cleanliness 例外见 [TESTS](TESTS.md)；协议与 B9 状态见 [事件设计](docs/design/PLUGIN_EVENT_HOOKS.md)。未安装启用到真实 home，未验证 B7 隔离、真实 Gateway/TUI/IM 或生产部署，交 be 复审和 3a 沙箱外复核。

## M5：内置插件作者技能与双语言模板（2026-10-03，sol2，`worker/sol2-m5`，早期记录，已由上面 M1 B9 段取代；已并入 step17j）

- **解决问题**：用户描述需要的工具后，模型缺少插件清单、打包和审批的作者指引。新增 `write-my-agent-plugin`，采用索引→正文→模板/参考三层读取，不增加管理工具或运行时分支。
- **实现**：Python 标准 wheel/v1 和 Node v6 `entry/files` 各一个只读 `count_text`；目录与声明同源，Unicode 码点统计，不读用户文件、不联网、不写业务数据。
- **构建**：Python 构建器接受显式受信的本地 `pyproject.toml + src` 工程；只在作者授权范围内执行离线后端，保留本地许可，版本仍按结构化扩展选 v1–v5。输出与 `TMPDIR` 放工作区，最终包由产品读包器判定。
- **安全边界**：`requested_effect` 不是免审批或 OS 沙箱；包不能下调宿主默认要求。安装、启用与确认码只由用户操作，不通过 shell、内部函数或其它代理绕过；不新增配置。
- **续作补证**：生产 wheel 实际构建后，新技能全部资源与源码原字节一致；九个既有插件工程用原/新构建器的最终包字节一致。当前新测试及 guards9 共 196 项通过；运行失败与 clean-package 工作树阻塞仍保留，详见 TESTS。
- **验证范围**：真实索引、镜像、双模板构建和包内 stdio；纸上压力复测仅证明合同知识补齐，不代表产品端到端。命令及当前结果见 [TESTS](TESTS.md)，作者合同见 [插件包文档](docs/design/PLUGIN_PACKAGES.md#m5内置插件作者技能2026-10-03)。宿主安装/启用、真实模型/TUI、Gateway 和生产部署未验证。

## feishu_limit 脚本补本机凭据（flc，2026-10-03，分支 `worker/feishu-limit-creds`，基于 step17i 头 `de87ff62a`；ds10 初审通过（其“请求头写死成占位符”一条经 3a 核原文为工具输出打码误判），已并入 step17j）

- **起因**：G2b 前置盘点（credinv）发现 `scripts/feishu_limit/` 四个脚本的 11 处 curl（`/ask`、`/result`）不带本机客户端凭据；G2b 打开后回环不再自带信任，这些脚本会被降匿名。
- **改动**：四个脚本统一经产品入口 `gateway_script_headers()`（`cli/gateway_client_headers.py`）取一次凭据放进 `GW_TOKEN`，用条件化数组 `GW_AUTH_HEADER` 逐处注入（`${GW_AUTH_HEADER[@]+"${GW_AUTH_HEADER[@]}"}`，bash 3.2/4.x 空数组兼容）；降级口径与 G3 客户端一致——开关关且凭据不可用照常发并提示一句，开关开则停下说明；token 只进变量，不回显、不落盘。新增常驻守卫 `test_architecture_guardrails.py::test_scripts_gateway_http_calls_carry_credentials`：扫 `scripts/` 下全部 .sh/.py，指向本机 Gateway（127.0.0.1/localhost + 8420 或地址变量）的 curl/urllib 调用必须带凭据头或访问公开路由（/status、/metrics），按结构扫描、不写死文件名；附合成样本自检。
- **验证**：见 TESTS「feishu_limit 脚本补本机凭据（flc）」小节；变异（删 d2 一处凭据头）被守卫抓到并原字节还原。
- **未验证**：脚本真实运行（需真实 Gateway）未跑，由 3a 在测试机复核；守卫只扫仓库根 `scripts/`，`agent_py_agent/scripts/` 不在扫描范围。

## M1 B6 账本与展示（m1b6，2026-10-03，分支 `worker/m1-b6-ledger-display`，提交 `d9f158e8d`、文档补丁 `e80db5918`、**初审 M1 已修（b6f）**、**确认门回归已修（b6f）**，基于 B3 返工后的头 `0a3064078`，已实施，**已并入 step17j（3a 沙箱外复跑 491 passed）；查询与展示完成，写账待 B5 第 5 段**）

- **起因**：设计稿 [插件事件与收紧钩子](docs/design/PLUGIN_EVENT_HOOKS.md) 第 9 节（账本与展示）与第 13 节 B6 行。B6 原依赖 B5（收紧钩子）写入 `plugin_gate.decided`；本轮先做不依赖 B5 的部分：查询、观察计数读取与 `/plugins info` 四段展示，B5 合入后接上写入方。
- **展示层归属更正（audit17j 核实）**：展示层已完整并入 step17j（`24c3aec0f`）——四段渲染在 `plugin_commands.py:render_plugin_event_details`，账本查询与字段白名单在 `runtime_db/repository.py:plugin_gate_decisions` / `_PLUGIN_GATE_DECISION_FIELDS`，TUI 与飞书走同一入口（`plugin_command_service._scope_management`）是同一份输出。在 `plugin_commands.py` 里 grep `plugin_gate` 得 0 不能证明 B6 缺失（查询不写在该文件）。缺的是 B5 写入方：没有它，真实 `/plugins info` 第 4 段显示“暂无记录”。
- **做了什么**：
  - `runtime_db/repository.py` 新增只读查询 `plugin_gate_decisions(plugin_id, limit=10)`（按 `plugin_id` 取最近 N 条 `plugin_gate.decided`，按 `seq` 倒序；投影只经 `_PLUGIN_GATE_DECISION_FIELDS` 白名单，`message` 等 payload 其它键不外泄）与 `plugin_gate_unavailable_count(plugin_id)`（累计 `final_status = PLUGIN_GATE_APPROVAL_UNAVAILABLE`，不设窗口）。owner 隔离由“一 owner 一 runtime.db”本身承担。
  - `plugin_commands.py` 新增 `PluginEventDetails` 与 `render_plugin_event_details`：订阅（含正文范围）、收紧工具（含参数范围）、网络与沙箱、最近 10 次收紧决定＋“无法审批”计数＋观察计数四段；措辞复用 `plugin_events/confirmation.py` 的同一函数（`event_description` / `gate_description` 由私有改公开名），确认码与展示不会各写一套说法。
  - `plugin_management.py`：`/plugins info` 在使用卡后追加四段；`PluginManagementContext` 新增只读 `event_hub` 字段；模块级 `_gate_ledger`（库不存在或读取失败按“暂无记录”）与 `_observed_counts`（hub 为 None 或读取异常按“暂无记录”）。
  - Gateway 侧 hub 穿透（**初审 M1 修复后**）：事件中心的唯一来源是 Gateway server 上的 `plugin_event_hub`。TUI 的 `/client/plugins` 在 `plugin_command_service` 里取；IM 链在 `http_handlers._handle_persistent_control_operation` 取（那里是唯一能拿到 server 的位置），经 `control_operation_service` → `control_service` → `execute_plugin_control` 按 `event_hub` 关键字参数透传（原 `_hub_for_control(base)` 已删，**不再从 `server.agent` 取**——server 与 server.agent 是两个不同对象）。读不到就是 `None`，按“暂无记录”展示，不新建 hub、不触发投递。
  - **初审 M1（b6f 修）**：原实现让 IM 链读 `server.agent.plugin_event_hub`，而 hub 挂在 server 上，IM 的观察计数恒“暂无记录”；现在两条链共用 server 上的同一个 hub。用例按真实拓扑（TUI 走 `/client/plugins`、IM 走 `_handle_persistent_control_operation`，只在 server 上挂 hub）断言计数逐字相同、且不是“暂无记录”；另加反向断言（server 没挂 hub 时 IM 链降级成“暂无记录”，不报错、不新建 hub）。
- **边界与未验证**：写账方（B5）未合入，账本行由测试按第 9 节字段直接插入；v8 启用门未开（B7 前），展示靠直接构造的 list/direct 入口验证，未启用真实 v8 插件。真实 TUI、飞书 IM 客户端与真实 Gateway 未验证；观察计数只在 Gateway 进程内存里，TUI 直连（direct 入口）没有 hub，按“暂无记录”展示。
- **验证与变异**：`tests/test_plugin_event_display.py` 12 项通过（含真实拓扑下 TUI/IM 两条入口计数逐字相同、跨 owner 不串、message 不外泄、老插件与空记录降级）；6 个原变异全部被检出，b6f 另加 1 个（IM 链退回从 `server.agent` 取 hub）被检出，命令与结果见 [TESTS](TESTS.md) 顶部「M1 B6 账本与展示」小节。
- **给 B5 的接缝（payload 形状，必须照做）**：B5 写 `runtime_events(event_type="plugin_gate.decided")` 时，第 9 节的字段要放进 **`payload_json`**（`plugin_id`/`version`/`activation_id`/`gate_id`/`tool`/`call_id`/`operation_id`/`args_hash`/`actor`/`outcome`/`verdict`/`reason_code`/`latency_ms`/`host_status`/`final_status`）；查询用 `json_extract(payload_json, '$.plugin_id')` 过滤。**形状不对查询会静默返回空**。错误合同里已有的 `PLUGIN_GATE_APPROVAL_UNAVAILABLE` 若与 `runtime_db/repository.py` 的同名常量重复，合并时统一引用一处。
- **已知边界**：“无法审批”计数（`plugin_gate_unavailable_count`）不设窗口、只增不减（全表统计，不受最近 10 条展示窗口影响）。以后再定要不要改成“最近 N 天”或“本激活代次内”。
- **二次修复（b6f）：确认门回归**。`plugin_command_service.py:155` 曾用 `_scope_management(base, scope, event_hub=...)`
  给一个会被测试打桩的内部函数传关键字，替身 `lambda *_: service` 只收位置参数 → `TypeError` → 被紧邻的
  `except Exception` 吞成 `PLUGIN_COMMAND_OUTCOME_UNKNOWN`，`test_real_confirmation_gate_*` 两条真宿主用例从
  `d9f158e8d` 起变红（3a 沙箱外逐提交核对：基线 38 passed，`1a4860903` 起 2 failed）。修法是 hub 一律走
  `scope.event_hub`、调用点不传关键字。另把 `GatewayControlScope.event_hub` 声明为
  `field(compare=False, hash=False, repr=False)`（展示依赖不参与相等/哈希/repr）。**教训**：
  核对“是不是既有失败”必须用 `git merge-base` 作基线，不能用带改动的交付头自证（我上一轮就是拿 `1a4860903` 当基线，结论错了一半）。

## Gateway G2b 启动预检补齐（g2bf2，2026-10-04，worker/g2b-enforce，9b 复核通过，已并入 step17j）

- 9b 复核指出唯一遗留必须改：启动预检用的是**客户端只读**凭据逻辑（`gateway_client_credentials(agent).headers()`）——配了 `gateway_auth_token` 时根本不读本机凭据（预检形同放行），凭据缺失时也不生成（Gateway 永远起不来，违背"缺了就生成"）。
- 修法（提交 90a73b2ca）：把 `GatewayHTTPServer._prepare_local_credential` 里"推数据根 + `ensure_local_client_credential`"抽成模块级 `prepare_local_client_credential` / `local_credential_data_root`（只依赖 agent）；启动预检与 `start()` 都调它。缺失时生成、不看配置 token、只有凭据确实用不了才拒绝；`ensure` 幂等。
- 新用例 2 条：开关开+凭据缺失 → 走到 setup 且凭据已生成、权限 0600；开关开+配 token+凭据坏 → exit 2 且 setup 未被调用。
- 变异 2/2 KILLED：预检退回只读客户端逻辑、预检不调 ensure。

## Gateway G2b 服务端强制开关（g2bf，2026-10-03/04，worker/g2b-enforce，9b 复核通过，已并入 step17j）

- 变基到 77be520f1（luna4 的 G1 加固）：用例按新合同对齐（伪造 Agent 需带 `owner_home_dir`、缺 `home_paths` 报 `agent_contract`），原因码统一为 `LOCAL_CLIENT_CREDENTIAL_NO_DATA_ROOT`（提交 ad0d8a41e）。
- 必须修 1（提交 8a6507b3c）：`/plugin-host/query` 的有效令牌必须同时来自本机回环，开关开、关同一口径；远程来源一律 403。9b 探针曾证改前远程+有效令牌开关关也 200。
- 必须修 2 + 3（提交 88fb6536e）：`cmd_gateway_run` 在 `_cmd_gateway_run_setup`（启动恢复、重新排队、起线程）之前预检凭据，不可用即带原因码 exit 2；补 `_build_gateway_auth_middleware`（R5）与 `_start_gateway_http`（R6）两条接线用例，两个变异都被杀。
- 必须修 4（提交 8c48b7733）：重新生成前端配置目录，`sync-backend-config.mjs --check` 通过。
- G1 加固补用例（同 8c48b7733）：N2（root 与推导数据根不一致 → `agent_contract` 且两边都不写凭据）、N4（推不出数据根 → 不可用且不写凭据，不退回落盘）；两个变异都被杀。
- 变异汇总：M1（去掉回环检查）、M2（去掉启动前预检）、R5、R6、N2、N4 全 KILLED，跑完原样还原。

## Gateway G1 加固（g1h，2026-10-03，worker/g1-hardening，已并入 step17i：luna4 初审提出的 3 条由 luna4 修复，3a 沙箱外 20 个相关文件 432 passed，作者沙箱里 test_plugin_sandbox 和 test_directory_lock_wait 的 5 个失败在沙箱外都过；9b 核对这 3 处）

- 真 Agent 的凭据数据根从 `home_paths.owner_home_dir` 经 `agent_home_root_for_owner` 推导，并校验等于 `home_paths.root`；非空 Agent 缺失/不符合同属性时报 `unavailable:agent_contract`，不把合同破坏伪装成正常无根降级。
- `agent=None` 没有可信 `home_paths`，记 `unavailable:LOCAL_CLIENT_CREDENTIAL_NO_DATA_ROOT`，G2a 仍启动和观察回环请求，但不从队列目录猜根、不创建凭据文件。有效 owner 布局下凭据路径与插件沙箱 H2 隐藏路径同源。
- 生成前用 no-follow 目录锁、读取与原子写逐级守住数据根下的 `secrets/`；目录/文件符号链接拒绝为结构化 invalid，父目录权限不合规则拒绝而非静默修复。
- G2a 语义不变：本机凭据准备失败只投影原因码、匿名回环照常放行并计数；G2b 服务端强制不在本次范围。测试、变异及门禁结果见 TESTS。

## IM 加模型补用例：接口列表外的模型名（2026-10-03，imadd，worker/im-model-add-test，已并入 step17i（3a 复审））

- 背景：17i 双入口核对（entries）缺口 1——TUI 手动填写允许加服务商接口列表里没有的模型名（`test_tui_model_add.py` 覆盖），IM 侧（`manage_models` 工具）缺对应用例；将来若有人给 IM 也加“只许选列表里的”限制不会被抓到。
- 改动：只加用例，不动产品代码。`agent_py_agent/tests/test_model_profile_tool.py` 新增两条：`add` 与 `add_models` 在接口列表不含目标模型名时照样加成功、list 可见；断言全程不拉取接口列表，且 `add`/`add_models` 两条路径的存储字段一致（除 provider 编号与展示名）。
- 变异 2 个（分别给 `add`、`add_models` 临时加“只许列表里”的限制）都被新用例抓到；还原后原 15 条全绿。
- 未验证：IM 真实通道（飞书）端到端未跑（规则禁止连真实 Gateway）。

## 台账 10-03 段落状态对账（ledgerst，2026-10-03，基于 step17i 头 `31335e4e6`，纯文档，已并入 step17i：3a 复审，并按 3a 裁定补改无日期的 pa51 段）

- **做什么**：核对本文档里 2026-10-03 的段落（标题带 2026-10-03 或 10-03 的共 42 条），把状态仍写着「待复审」「待 9b 终审/复核」但实际已并入 step17i 的段落改成真实状态；只改状态那一句，正文、段落顺序、数量都不动。
- **怎么判**：只靠 git 事实——在 `git log db963ea66..31335e4e6` 的提交说明里找该段的原始提交号、执行代号或特征词；找不到依据的不动，并列入交接报告。
- **结果**：改动 6 处（pb31、pa51、j16f 正文状态行、g3f、sol1c、sol1g+ds1g），每处的 step17i 提交号与依据词见交接报告；M1 设计稿段「待拆块派活」等按规则未动并说明。
- **验证**：`scripts/check_doc_sync.py` 通过、`git diff --check` 干净。纯文档改动，未跑代码测试、未改任何代码。

## 用户可见正文误判内部标记、从下标处截断（2026-10-03，vtm，worker/visible-text-markers，已并入 step17i：3a 复审，大小写必须改已修）

- **现象**：回复正文里只要出现方括号下标写法（Python 里按下标取缓存、按索引取列表这类），正文从那里往后整段消失。mtcr 的初审报告两次都在同一行代码处被截断，投递状态 `internal_protocol_removed`；完整原文其实已经产生，只是投递前被净化器砍掉。
- **根因**：旧判定是整段子串匹配——正文任意位置出现某类内部标记前缀就截断到该位置；方括号工具块的块正则还允许缺闭标签时一直吞到文本结尾。两者都只看形状、不看结构，所以正常代码和内部协议长得像时无法区分。
- **改法**（只按结构认，标签名精确匹配，不做模糊规则）：① 前缀类标记（运行/子代理标签）必须出现在行首才算协议，正文中段提及不算；② 完整方括号标签（工具调用/结果）必须独占本行（后面只剩空白）才算协议；③ XML 形态工具标记按行首前缀认，仍走各自的成对块正则；④ 方括号工具块只认“行首开标签 + 成对闭标签”，缺闭标签不再吞掉后文；⑤ 其余方括号文本一律当正文放行。真正的内部标记照旧剥掉，出口仍只有 `conversation/channels.py` 的 `project_user_reply` / `_plain_user_reply_projection`，不放任各 IM adapter 自建名单。
- **实现**：`agent/conversation/user_visible_text.py` 的行首标记判定拆成前缀类 / 完整方括号标签 / XML 三张精确清单，方括号块正则补上成对闭合要求。
- **补丁（3a 复审后，必须改）**：行首标记正则补 `re.IGNORECASE`。旧实现先 casefold、大小写都认；新正则漏了忽略大小写，会让模型截断留下的全大写开标签、大写/混写的运行标签行漏给用户。标记表里专列的大写条目随之删除，大小写只保留一个口径；结构判定（行首、独占一行、成对闭标签）不因大小写放宽。见 TESTS「补丁」小节。
- **验证**：`test_user_visible_text_markers.py` 全覆盖（下标写法在行内代码、代码块、普通文字三种位置都原样送达且投影状态不是内部协议被移除；每种真实内部标记各有反例仍被剥掉；行内单独提一次工具标签不再吞段；大写/混写真标记仍被剥、行中间的大写标记不剥；本轮被截断原文的回归）。变异 6 个全部被抓。命令与真实结果见 TESTS。
- **未验证**：真实 TUI、真实飞书通道的端到端投递未跑（规则禁止连真实 Gateway/渠道）；guards9 里 TUI 插件类 22 条失败已用基线 `631fbb608` 证实是沙箱环境限制，与本改动无关。
- **3a 挑入时的一处调整**：方括号工具块正则吃掉了闭标签后的换行，块前块后两段之间的空行没了（test_gateway_chat_conversation_context 的旧用例抓到）。改成只向前看、不吃换行，排版与旧投影一致；相关 5 个测试文件 256 passed。

## Gateway 宿主抓取工具隔离（G6，2026-10-03，worker/luna1-g6-fetch-port，已并入 step17i：ae 首审 2 条必须改已修，ds4 再审通过、10 个变异全抓到；25 文件矩阵的沙箱失败经 ds4 在基线复现、属环境，3a 沙箱外全过）

- 私网授权按每次工具调用克隆后的当前属性重新构造，并在每个 URL/重定向跳重新检查；web_fetch 不再把初始私网策略冻结到构造时。
- 受保护端口统一来自 G4 的 `gateway_bound_ports()` 并合并正数 `gateway_port` 配置（覆盖 TUI/跨进程情况）。端口为 0 或没有端口事实时 G6 不适用；端口非法/读取失败时只拒确认的本机地址，公网继续；检查端口命中时对解析 IP 做无缓存 UDP bind 探测：成功=本机、`EADDRNOTAVAIL`=非本机、其它错误=无法确认并拒。
- 本机判断不调用 `getaddrinfo(gethostname())`，也不缓存接口快照；回环、IPv4-mapped、未指定地址仍按地址属性判断。每跳 DNS 固定已检查 IP，重定向逐跳重查；不附本机客户端凭据。
- G6 与 G4/G5 的 `gateway_isolation` 状态无关联：G4/G5 不可用的平台上，模型仍可能通过 `run_command` 连 Gateway；G6 只保护宿主侧 `web_fetch` 和 `watch_stream`。二者只共享 Gateway 端口来源注册表。所有 HTTP 测试限自身随机端口假服务，未启动 Gateway、未碰 8420、无外网。实际环境链路待 3a 复核。
- **3a 挑入后 12 片车道抓到 1 条**：test_r223_audit_regressions 直接调用了 G6 搬成模块级函数的 WebFetchTool._cache_put/_cache_get（行为不变）。用例改成调 web_fetch_tools._cache_put/_cache_get，54 passed。G6 的 25 文件清单和两轮初审都没覆盖这个文件。

## 修法 B 收口提示走结构化通道（rfs，2026-10-03，分支 `worker/retry-final-sink`，初审补齐 `worker/rfs-followups`，基于 step17i `9490cdf90`，已并入 step17i：ds6 初审、ds6 补齐、3a 复审并沙箱外复跑）

- **起因**：修法 B 到总时长上限的收口提示此前只走文本回调（`_emit_retry_notice` 的 final 分支），富客户端（TUI 状态行、飞书卡片）只能看到一段文字，看不到结构化的「已停止重试」。
- **做了什么**：收口提示改为先走 typed sink（`write_provider_retry` 新增可选收口参数包 `params={"final": True, "error_code": PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED}`），sink 不认识参数包（旧签名 TypeError）、抛异常或返回 False 时照旧退回原文本回调；`provider_retry_event` 的收口事件在 `retry` 载荷带 `final/error_code`，文案复用 `request_errors.gateway_client_error_message` 已登记的用户文案；`BufferedChunkStreamWriter`（TUI 流）、`BackgroundTranscriptSink`（后台正文/IM）、`AgentActivity`、`GatewayForegroundTranscriptSink`、`GatewayMainActivitySink` 按结构化字段显示收口信息，不再显示「N 秒后重连」；普通重试事件一个字段都不加，保持旧客户端兼容。
- **边界**：判断只看结构化字段，不解析文案；收口文案单一来源（request_errors 已登记）；文本回退逐字不变；未改配置、未启停 Gateway、未做真实 TUI/飞书验收。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「修法 B 收口提示结构化」小节；3 个定点变异全部被检出；size_diff 新增 0。
- **初审补齐（rfsf）**：状态行收口短句统一登记为 `request_errors.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED_STATUS_TEXT`，`AgentActivity`/`GatewayMainActivitySink` 改为引用该常量、不再各自硬编码；补 3 条用例（agent_activity 状态行、main_activity 状态行、foreground 转发）把初审 3 个存活变异全部转红；回退文本断言升级为完整字符串逐字 `==`；新增 ast 守卫用例——扫产品代码所有 `write_provider_retry` 实现方，必须显式声明 `params`、不得用 `**kwargs` 静默收下（协议约定写进 `provider_transient_auto_resume._publish_typed_retry_notice` 注释），`**kwargs` 变异被守卫抓住。

## G3 插件命令降级回归（g3r，2026-10-03，分支 `worker/g3-tui-regression`，基于 step17i 头 `99a1558a9`）

- **状态：已并入 step17i（3a 复审）**。改动只在测试侧，产品代码未动；作者追加进 guards9 的 4 个文件已由 3a 移出（它们是 G3 定向用例，会话沙箱里跑不了）。
- **现象**：12 片 Linux 车道抓到 G3 并入后的回归——`test_tui_input.py::test_plugin_submit_uses_real_dispatch_without_chat_guidance_or_stop` 的 20 个 `use_gateway=True` 变体、以及 `test_tui_plugin_directory_pipe.py` 的 `[manual]`/`[after_command]` 全失败，断言拿到的是"无法读取当前插件目录"。二分确认由 G3（`2adfd8518`）引入。
- **根因（一行说清）**：G3 把共用传输 `post_gateway_json` 的签名从 `(port, owner, path, payload, *, timeout)` 改成 `(agent, path, payload, *, timeout)`，并同步更新了 `test_plugin_command_client.py` 的 mock，但漏掉了 `test_tui_input.py:97` 和 `test_tui_plugin_directory_pipe.py:31` 里仍是旧签名的 mock。旧 mock 接不到新调用 → 抛 `TypeError` → 被 `plugin_command_client._request` 的 `except Exception` 兜成 `plugin_catalog_unavailable()`，于是显示"无法读取当前插件目录"。**产品降级口径本身是对的**（`client_credentials.headers()` 开关关时记一次 warning 并返回空头继续发送）；坏的是测试替身没跟上签名。
- **修法**：不再 mock 传输层函数，改成起一个本机假 Gateway（`agent_py_agent/tests/_gateway_stub.py`，`ThreadingHTTPServer` 监听 127.0.0.1 随机端口，走真实 HTTP、真实 `post_gateway_json`、真实凭据分流），测试只提供响应器。这样这两条用例从此覆盖"无凭据 + 开关关 → 照常发送"的真实路径（隔离 home 下没有凭据文件，`headers()` 自然降级）。
- **新增用例**：`test_plugin_submit_degrades_without_local_credential_when_switch_off` 显式钉住"开关关 + 凭据文件不存在时 /plugins 提交照常派发、不带 `X-Gateway-Token`、只记一次 warning"。
- **未改**：`client_credentials.py`、`plugin_command_client.py`、`chat_client_context.py` 等产品代码与 G3 逻辑一行未动；没有为了让测试变绿而放宽断言（断言反而更强：Gateway 变体现在要求"请求必须真实到达假 Gateway"）。

## B 包 0.3.1：交接唯一性、字段名收紧与拆分指引（pb31，2026-10-03，分支 `worker/pack-b-031`，基于 step17i `0ae0efe5e`；已并入 step17i（642d749dd；ds2 初审））

- **起因**：能力包 v2 块 8 冻结重跑的独立业务审阅：①交接文件被写成两份（交付件与 `tmp/` 模板副本），宿主“恰好一份”的匹配不成立，交接没进标准检查链（B08-t202）；②动作节拍写成单数 `character_id`，检查器只报提醒、不触发返工（B08-t203）；③“角色、道具和镜头参考各写各的”拆分要求没落实（B10-t202）；④核对报告把产物里已有的引用写成缺失、还建议补一个已经存在的条目，且与同一报告的表格自相矛盾（B05-t203）。
- **改了什么**：`methods/workflow.md` 新增交接文件清单（写在哪、叫什么、只保留一份、怎么绑）与完整示例，并补“各写各的”拆分小节；方法、模板、检查器统一 `character_ids` 口径；`scripts/check_continuity.py` 把“动作节拍写成单数 `character_id`（非空）”从 `beat_character_missing` 提醒升级为 `beat_character_id_singular` 错误（完全没写仍是提醒，对白节拍不受影响；安全性评估见包内 PROVENANCE）；`methods/review.md` 新增第 9 条（写“缺什么”前先回产物逐项复核、表格与结论一致），`CAPABILITY.md`、`templates/handoff.json` 同步；版本升 0.3.1。
- **验证**：见 `TESTS.md`“B 包 0.3.1（pb31）”小节。
- **未验证**：真实 TUI/宿主链与 B 类 9 例重跑留给集成者；本分支不重跑冻结用例。
- **补两条边界用例**（pb31t，2026-10-03，分支 `worker/pack-b-031-tests`）：ds2 初审的两个存活变异都在同一处——升级条件漏了两个边界。按 ds3 原用例的写法补上「合法非空列表旁残留非空单数不报新错误」与「单数值不是字符串时不升级、仍只给原提醒」两条（`test_capability_package_drama_workflow_v031.py`，用例数 6→8）；检查程序逐字节未改。两个变异重跑均被杀（M1 放宽成"只要有 character_id 就报错"、M7 非字符串值也报错），另加一个针对第一处边界的变异（列表非空也报单数错）也被新用例抓到。详见 TESTS。

## A 包 0.5.1 复审返工（2026-10-03，pa51，worker/pack-a-051，已并入 step17i（fd7e92c28；ds10 初审、3a 复审））

- **起因（ds10 初审）**：包内举例直接用了冻结用例的原文片段，等于把答案教给模型。`methods/workflow.md` 的结局保留一节写"原文写'把牌子翻到朝外的一面'……换成'把牌子翻下'"，其中"翻到朝外的一面"与用例原文连续重合 7 个字，"翻下"又正好来自那次失败交付；`PROVENANCE.md` 的修订表同样写了那次的改写内容。
- **改法**：举例换成与冻结用例无关的通用说法（"原文写'推开窗'，就只能拍成推窗；换成'关上窗''钉死窗'都算改了结局的关键动作"）；`PROVENANCE.md` 修订表只写"改写了结局里的一个方向类关键动作"这类问题类别，不写用例内容。同时按 ds10 意见把 `methods/workflow.md` 交付命名那句改成直说"扩展名必须是 `.json`"（原列举式写法容易被读成"写 `drama_text_delivery.v3` 也行"），并把 `CAPABILITY.md` 交付规则里挤在一句的两件事拆成"原文已有的不算改编"与"新增的必须逐条标注"两条。
- **核对方法与结果**：写脚本把包内**所有文件**（不限扩展名）正文与 9 个 A 类试次的 `case.prompt_zh` + `inputs/story-source.json` 做连续汉字重合比对（只比汉字；ASCII 的 schema/字段名是格式约定，本就必须一致）。修复前 2 个文件各命中 7 字重合（`翻到朝外的一面`）；修复后 6 字阈值 0 处，加严到 5 字仍 0 处。脚本做了变异自查：把原句塞回去立刻变红 9 处，还原后复绿。
- **未落地**：这是"内容不得泄露样例"的人工核对，仓库里没有常驻守卫；下次改包仍要靠这条脚本或人工比对。

## A 包 0.5.1（pa51）

- **状态：已并入 step17i（fd7e92c28；ds10 初审、3a 复审）；块 8 工具重跑 A 类 9 例在做（rerun-50479a440）**（2026-10-03，分支 `worker/pack-a-051`，基于 step17i 头 `0ae0efe5e`）。
- **起因**：A 类 9 例冻结重跑的独立业务审阅有 4 例不合格，全部是“模型读得到包里的指引却没照着做”的可写清项（宿主侧提示另由 step17i `e5d833498` 修）。本轮只改包内指引、方法、模板和示例，检查程序的检查逻辑一行未改。
- **四类改动**（对照见 `examples/capability-packages/drama-text-a/PROVENANCE.md` 的 0.5.1 节）：
  1. 交付文件名统一成 `output/drama_text_delivery.json`，扩展名必须 `.json`（A08-t201、A10-t202 写成 `drama_text_delivery.v3`，宿主按 `**/*.json` 认不出交付物）；
  2. 原文没有的动作/台词/道具状态必须逐条写进本镜 `adaptations`，给了正反例（A08-t203 有新增创作却留空数组）；
  3. 结局的动作/朝向/状态默认原样保留，只改表达不改事实；确实删改要写进删改说明且不动结局关键事实（A10-t201 把“翻到朝外的一面”改成“翻下停航牌”）；
  4. `cast[].text_names` 与非空退出理由互斥，给了不冲突例子和冲突例子（A10-t202 报 `conflicting_text_match_declaration`）。
- **版本**：declaration.json / CAPABILITY.md / PROVENANCE.md 同步 0.5.1；`scripts/check_delivery.py` 只把 `PACKAGE_VERSION` 常量随版本升（与 0.5.0 升级同样做法），检查逻辑与其它字节未动。包内哈希清单由构建脚本按实际字节现算，仓库不存静态清单。
- **未落地**：第 2 条（漏标新增创作）目前只靠作者自觉——检查程序只看 `adaptations` 形状，不读正文判断“这段动作原文里没有”。要加确定性检查需要新的对账输入，方案见本轮交接报告，本轮不动 `scripts/`，因此这条**不能宣称已由检查兜住**。
- **验证**：相关 10 个测试文件 327 项通过（能力包声明与打包、两包检查程序、示例构建、核验声明测试）；guards9 与静态门禁结果见 `TESTS.md` 顶部同名节。

## 台账标题状态校正（lhc，2026-10-03，基于 step17i 头 `0ae0efe5e`，并入 step17d–17h 的段落共 42 条，纯文档，3a 复审并入 step17i）

- **做什么**：逐条核实本文档里标题还写着「待集成」「待上线」「待 be 审」「实施中」这类旧状态的段落，确认其实是否已经并入 step17c–17i；确认并入的，只把标题末尾的状态措辞改成真实的「并入 step17x」，正文、段落顺序、数量都不动。
- **怎么判**：只靠 git 事实。分支头用 `merge-base --is-ancestor` 判是否 HEAD 祖先；换了 sha 的挑入提交按提交信息里的 `cherry picked from commit` 映射（HEAD 上共 342 条映射），仍拿不准的再用 `git cherry`（补丁等价）复核。没有 git 线索、或 `git cherry` 显示补丁仍未并入的，标题保持原样并列入交接报告。
- **结果**：改动 42 条。并入 step17h 1 条、step17g 1 条、step17f 3 条、step17e 10 条、step17d 27 条。
- **保留原样（核实不了或确实未并入，共 6 条）**：`claude/be-capability-packs-content-b2`（分支已不存在、相关提交均不在 HEAD）、`claude/be-bench-more-points`、`claude/38-p14-embedding-fixes`、`worker/sol2-c14-mb1`（以上四条按挑入映射核查，相关提交没有任何挑入记录）、以及 2 条只有日期没有提交号、无法给出 git 依据的旧段（Compact 校准两段）。
- **本就不该动的 3 条**：`claude/be-gateway-local-trust`（标题写「实施中，并入 step17i」，已注明并入）、`claude/be-m1-design`（设计稿，剩余 6 个文档提交未挑入，标题「待拆块派活」属实）、`worker/luna5-pack-guide`（标题「已完成，ae 已复审，并入 step17h」，状态本就准确）。
- **判断口径的坑（留给后来人）**：`git cherry` 单独看会误判——挑入后若又经过返工修订，补丁就不再等价、会显示 `+`（`worker/sol56-c8c9-recover`、`worker/sol2-anthropic-budget` 都是这样），但提交信息里的 `cherry picked from commit` 映射是权威的。本轮的并入判断以映射为准，`git cherry` 只作辅助。
- **验证**：`scripts/check_doc_sync.py` 通过（`DOC_SYNC_PASS`）、`git diff --check` 干净。纯文档改动，未跑代码测试；未改任何代码。
## 屏幕观察的 macOS 后端依赖显式钉进新 extra（2026-10-03，cux，worker/computer-use-extra `10094bb11`，3a 复审并入 step17i）

- **起因（3a 定）**：J16 设计稿第 34–37 行本来要求“实施时把用到的这几个写进 computer-use extra，显式钉版本”，但一直没做——`pyproject.toml` 的 `computer-use` extra 只有上游 `computer-control-mcp==0.3.13`，装它会带进 222 个包，生产没法照仓库声明只装观察用到的那几个。
- **边界（先 grep 清楚）**：新观察后端（`computer_use_macos.py` / `computer_use_x11.py`）**不调上游 `computer_control_mcp` 的任何内部对象**，只用系统与库的公开接口；而上游那 16 个执行工具仍在用（`tooling/computer_use_server.py:16` `from computer_control_mcp import core`，`computer_use_profile.COMPUTER_USE_PACKAGE` 也钉着它）。所以**旧 extra 不删**，另开一个只给观察用的。
- **改法**：新增 `computer-use-observe` extra，按平台钉死：macOS 六条（pyobjc 的 Quartz / ScreenCaptureKit / ApplicationServices、mss、rapidocr-onnxruntime、pillow），Linux 四条（python-xlib、mss、rapidocr-onnxruntime、pillow），都用 `sys_platform` 标记。macOS 版本来自 3a 在生产运行时克隆上的实测；Linux 版本不照抄文档，而是进桌面层车道镜像 `my-agent-linux-test:py312-desktop` 读实际装到的发行版元数据核对（python_xlib 0.33、mss 10.2.0、rapidocr_onnxruntime 1.2.3、pillow 11.3.0）。**pyautogui 不进观察 extra**：它只给自动执行的点击用，生产保持关。旧 `computer-use` extra 的注释补上两者区别。
- **文档**：`docs/design/computer-use.md` 顶部加“怎么装（管理员）”：两个 extra 各装什么、何时用哪个，以及**屏幕录制**与**辅助功能**两个系统权限要给“启动 Gateway 的那个终端程序”（权限认进程，换终端要重授权）。
- **验证**：新增 `test_packaging.py::test_computer_use_observe_extra_pins_platform_dependencies`（解析 TOML，逐条核对名称/版本/平台标记，并要求无 pyautogui）；命令与结果见 TESTS。
- **未验证**：没有真的在 macOS 上按这个 extra 装一遍并跑通截图/AX；Linux 侧也没有按新 extra（而非上游整包）重建过车道镜像。
## M 线第一期真实验收手册（m1ar，2026-10-03；手册已写，真实验收待执行）

- 状态：**3a 终审通过，已并入 step17j；等 B5、B7（以及第 6.6 节要的老插件权限）合入后在沙箱外执行**。rb5：§6.5 改结果通道、加宿主侧对照、收窄异常（rb5 收口）；rb4 补全探针声明 + §6.8；rb3 落实 ds1 复审 3 条必须改**。rb3 做的是手册第三版：tmux 从 `-L` 全改 `-S`（`-L` 收名字不收路径，会把路径拼到 `/tmp/tmux-<uid>/` 下直接报错，已实地验证 `-S` 的套接字落在隔离目录且 `srw-------`）；§2 脚本加 `umask 077` 并补 macOS/Linux 两套 `stat` 权限核对（配置/三哨兵/三 zip 须 `-rw-------`、目录 `drwx------`）；版本前置统一为 B3–B9（补 B9 行）；把 `plugin_events_enabled`（B7 提供）与 `plugin_tool_gate_timeout_ms`（B5 提供）挪进 §1.1 前提清单——这两个键在 17j 里**根本不存在**，`/settings` 也管不了，只有对应分支合入后本节才成立；§6.5 附可直接复制的探针插件骨架（以 B9 Python 模板为底）。之前 rb2 段落的"待 17j 合入 B3–B8"写法随之作废。
  - **rb4 补充（2026-10-04）**：①§6.5 的 `probe-net/declaration.json` 从"关键片段"改成**完整声明**，补 `summary`/`default_action`/`actions`/`tools`/`settings_schema`；原片段照抄会打包失败（rb3r 实测 `PluginPackageError: 插件包描述无效`），补全后 `build_plugin_files_package.py` exit=0（rb4 又实测一次）。②新增 **§6.8「活动回合里插话（prompt_submitted 只记一次）」**：TUI 与飞书各一遍，通过条件是 `prompt_submitted` 只 1 条且 `facts.request_id` 属原回合、插话 `disposition` 为 `active_turn_input`、原回合 `turn_started`/`turn_ended` 各一次；**插话落成 `queued` 时记「未命中」不算通过**（否则"只有 1 条"会假通过）。§8 通过矩阵补对应行。
- 手册：[PLUGIN_EVENT_HOOKS_ACCEPTANCE.md](docs/design/PLUGIN_EVENT_HOOKS_ACCEPTANCE.md)，覆盖 17j 前置、隔离 home/专用端口、三样例联合冒烟、TUI/飞书矩阵、结构化账本核对与收尾。
- B8 的 rm-guard 删除门由 ds2 更新为 `apply_patch` 删除段 → `deny / DELETE_FILE_BLOCKED`；README/声明尚未随本手册现场验证，最终构件不匹配时不得开始。


## 能力包 v2 块 6b：写后核验复用已知工作区候选（p6b，2026-10-03，分支 `worker/pack-6b-scan-reuse`，基于 `c47d023b6`，3a 复审并入 step17i）

- **问题**：每次写后 `turn_output` 输入解析都会走访并哈希当前工作区；20 次写后加收尾在 1.9 万文件工作区中触发 22 次整盘扫描。
- **做法**：写后候选路径来自本 run 已落账的 written 集合；只对这些路径读取当前存在性、大小和摘要，再与基线中同路径摘要比较；按包声明路径模式过滤，重复路径只处理一次。当前写回执成功记入账本后，同步加入该次检查 scope。
- **保持边界**：`task_input`、路径/glob 与字段匹配、目标排除、恰好一条才交输入、匹配数与结果/缓存账字段不变；收尾仍用完整 `scan_workspace`，Shell 新建或改动且未记入 `written` 的文件只在收尾发现。不新增配置或账本字段。
- **截断**：已知候选的排序沿稳定目录/文件顺序，仍受 2 万候选路径、512 个当前匹配普通文件和 16 MB 哈希上限约束；收尾原来的完整扫描与 `current_truncated` 事实不变。
- **验证**：旧实现下计数与 Shell 可见性用例先红；实现后定向匹配/服务/输入测试及五项变异见 TESTS.md“能力包 v2 块 6b”。工作区/真实子进程沙箱复核仍交集成线处理。

## J16 macOS 叠放顺序与程序坞遮挡（j16f，2026-10-03，3a 代码复审并入 step17i；真机复核待屏幕解锁时做，过了才在生产打开 observe）

- **状态**：已并入 step17i（dda164428；3a 复审）；真机复核由 3a 做。
- **真机事实与修法**：V-H 证明 OptionAll 不代表叠放次序、OnScreenOnly 才按前→后返回；默认目标排序改为用 OnScreenOnly 事实，OptionAll 仍保留全部实例。程序坞存在 layer=Dock、alpha=1 且外框覆盖整显示器的背景窗；遮挡侧只在系统 Dock 层级与整屏几何同时匹配时排除，其他层级及非全屏 Dock 仍参与遮挡。`observe` / `recheck` 复用后端 `above_rects`。
- **范围**：只改 macOS 观察适配器、对应假 Quartz 回归和第 3.2 节第 5 条；未访问真实屏幕，真实复核留给 3a。

## 只看 mtime 的缓存（mtc，2026-10-03，分支 `worker/mtime-cache-fix`，基于 step17i 头 `c47d023b6`；ds5 初审两条必须改由 3a 挑入时修：子代理按 luna6 口径、补模块文档；并入 step17i）

- **起因**：子代理 `task.json` 的原子替换事故（同 mtime 时间片里 RUNNING 被旧缓存写回 PLANNING）修完后，ae 的复审清单还列出另外两处“只看 mtime”的读缓存：`common/json_io.read_text_lines_cached`（签名 `(mtime_ns, size)`）、`gateway_parts/response_renderer` 的两个“变了才读”入口（`read_gateway_response_file_when_ready` 与 `read_gateway_terminal_response_file_when_ready`，签名 `(mtime_ns, size)`）。
- **改法（与子代理同一口径）**：抽出一份共享实现 `agent/common/cache_freshness.py`——指纹 `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`（原子替换必然换 inode）+ 常数 `CACHE_TRUST_AGE_SECONDS = 2.0` 的“够老才可信”判断；**mtime 距今不足 2 秒时读到的内容照常返回，但不放进缓存**（窗口内先读后写的 ABA 不会被缓存住）。三处消费方（json_io、response_renderer、subagents persistence）都引用同一判断，`GatewayInboxScanGate` 的 2 秒口径保持不变但没有改动它。
- **子代理侧**：`SubAgentPersistenceService._RUN_CACHE_COARSE_MTIME_GUARD_SECONDS` 改为引用 `CACHE_TRUST_AGE_SECONDS`，判断改成共享函数，行为与 `c47d023b6` 一致（阈值与方向都没变）。
- **行为影响（需要集成者知道）**：`json_io` 行缓存命中的前提从“append 会变 size”变成“代次指纹相同且文件已离开窗口”，纯追加场景的首次命中会比以前晚最多 2 秒；response_renderer 两个入口在窗口内会对同一代文件重复读取一次（调用方是幂等覆盖，不会重复展示或重复收口）。
- **证据**：三个新测试文件（共享 helper、response 轮询去重、json_io 扩用例）；5 个变异全部被杀（含“只修 json_io、response_renderer 退回 mtime+size”这一跨点变异）；命令与结果见 `TESTS.md` 顶部同名节，变异。
- **未验证**：真实 Gateway/TUI/飞书轮询、Linux 车道（本机只在 macOS 跑）。全仓 pytest 由 3a 的车道跑。

## G2b 服务端强制：开关打开后回环无凭据按匿名（2026-10-03，g2b，worker/g2b-enforce，已实现，开关默认关，9b 终审通过，已并入 step17j；生产打开要等 /status 无凭据计数归零）

- **范围（设计 G2b 行）**：开关 `gateway_require_local_credential` 打开后，鉴权只认有效本机/配置凭据——回环不再自带信任、拿不到对端地址（`peer_ip=None`）按不可信；不带/错/空凭据的回环请求降匿名（`extract_identity` 原分支，不认身份头、绝不给管理员），要身份的接口照原规则拒绝。开关关时行为与 G2a 完全一致（只计数）。
- **启动 fail-closed**：`GatewayHTTPServer._prepare_local_credential` 在开关开且凭据不可用（损坏/权限/数据根不可读或不可生成）时拒绝启动，抛 `GatewayLocalCredentialRequired`（reason_code 带 G1 原因码）；凭据缺失仍是 G1 的「缺则生成」正常路径、照常启动；开关关保持 G1 返工后的降级启动。
- **判定只读结构化事实**：按 `http_routes` 路由模板（不是原始路径）计数与判定；令牌用 `hmac.compare_digest` 常数时间比较；不解析文案。
- **插件令牌豁免**：`handle_plugin_host_query` 先验 `X-Plugin-Host-Token`，有效即满足这条路（不要求客户端凭据、不看回环），令牌无效才回落 `require_trusted_source`——开关打开时插件不断。
- **TUI 启动预检（3a 插话①）**：`make_gateway_chat_client` 构造后调 `preflight_gateway_credential`（只读凭据、不发请求），开关开且凭据不可用时拒绝启动并给原因码；开关关保持降级。选「启动时先检查一次」这一支（「GatewayChatClientAgent 统一接住」未做，其余调用点行为不变）。
- **降级 warning 去重（3a 插话②）**：`_warn_credential_degraded` 的去重是进程级、有意的——凭据修好又坏时同一原因不再重复提示；当前状态从 `/status` 的 `local_credential` 看。注释已写明。
- **已知边界（be 四条）**：①凭据文件 0600，同一系统用户本来就读得到——它多挡住的是非 Full Access 的模型命令（沙箱读不到 `secrets/`）与其它系统用户，不写成「有凭据就全挡住」；②`submit_gateway_ask` 是文件队列入口、不走 HTTP 端口，端口凭据门管不到它，靠文件权限自己把关，本次不改；③插件令牌路由不受影响（见上）；④`peer_ip=None` 按不可信。
- **验证**：新 `test_gateway_local_trust_enforcement.py` 19 项；相关回归 13 文件全绿 + guards9 全绿；五项变异（开关关也强制/空凭据当有效/插件令牌要凭据/None 当回环/按原始路径）全部被抓并原字节还原；静态门禁与 size_diff 见 TESTS.md。未验证：真实 Gateway/TUI/IM、生产迁移与计数归零、跨平台。

## G3 本机客户端附带凭据：返工为「读不到即降级，只在 G2b 开关打开时拒绝」（2026-10-03，g3f，worker/sol1-g3-clients，已并入 step17i（2adfd8518；9b 集成终审通过））

## 老格式插件权限启用回归尾补（opp4，2026-10-04，WIP，B7 未接，整系列不可挑入）

- **来源与路线**：3a 转交9b终审：第三段 `3578038b5` 本身通过，外部 wide 真实环境/候选探针走通预览→完整确认→active；该证据不代表 restricted 隔离。第二段起五文件21条真实启用用例仍沿旧协议，现按完整 `confirm_command`、`authorization_id` 请求身份与嵌套 `runtime` 更新；成功/active、程序固定、业务调用、沙箱包装/写限制、清理/UNKNOWN 判据保留，不删不 skip。
- **默认与发布**：产品 `plugin_legacy_sandbox_default=true` 不改；真实旧链回归夹具显式选 wide，不改共用 manager 默认，不以夹具绕过默认 restricted 的 `legacy_sandbox_pending`。3a 定整个 opp 等 B7 正式接线后一起进17j，赶不上整体放17k；v4回滚有成本，不拆两次上线。
- **ds8/9b尾补**：准备前最后守卫前漂移断言零 prepare/候选，撤旧前安装快照变化断言 revision_conflict/旧 active 不变/零 retire；独立单处变异分别验证，结果见 TESTS。`self.*` 刷新是有意的：旧码与新事实不匹配只能重新预览，进入 `_enable` 时消费用户最后确认的同一授权。
- **B7 第四段验收（待实现，不能只在启用时核）**：候选、业务、面板和重连的**每次实际启动前**都要核当前完整授权事实与该固定激活的 `permission_json` 一致；包/安装/激活代、策略、R/W/E 路径身份与程序内容、解释器或网络授权漂移应零启动，不自动换绑或回退 wide。第四段须实际接到正式公共入口并补逐入口用例，本次只登记，不复制 OS 规则或提前实现。
- **验证与边界**：本线对 `22e02c668`/`b33175b41` 分离检出同五文件，b33的21失败节点与9b日志一致；本机22基线另有16失败/1跳过，不能声称本线基线全过。实际数字/命令见 TESTS 与本树 tmp/opp4；真实链路、OS/渠道及本次新版本终审仍待外部复跑，不根据旧探针替新版本背书。

## 老格式插件权限第三段（opp，2026-10-04，WIP，B7 未接，不可部署）

- **确认竞态修复**：真实管理员/HostCommand 入口复现构造后和准备期间根/程序漂移继续启用。执行时刷新全部有限路径/程序/解释器事实，漂移重给下一请求完整预览；链接/缺失拒绝，撤旧前核实际安装快照；准备与候选启动/发现/清理后的发布都复核原固定授权，不自动换授权或发布。
- **ds1 第二段意见**（3a 转述）：五项清单通过但 C10 两例必须修，采用保留原“启用前需要你确认”首句后追加完整新段，原断言不改；本线已在 merge-base `22e02c668` 定向复核，不称既有失败。显式 authorization/request 与前缀、非管理员携授权负例补齐；撤旧两处独立计算有意由完整目标相等守卫核对；commit_state 使用原表同提交 release 事实，不再信报告存在。
- **验证边界**：相关337项、guards173项零失败/错误/跳过；八处本段单变异均真实红→原字节恢复→同目标绿。32根长预览仅验证原客户端/IM/HTTP handler 与管理服务的完整保留/确认/重送合同，不代表真实 TUI 渲染、飞书分段完整送达或 OS 隔离。命令与失败修复记录见 TESTS，细节见设计6.5。
- **下一步与写界**：候选事实复验已接；业务/面板与候选统一 B7 仍待精确接口/集成，不复制 OS 规则，不启停生产 Gateway。真实平台/样例/桌面/G2b/渠道/生产回滚及本段非作者与9b仍待，由3a协调；整体权限目标未完成。

## 老格式插件权限第二段（opp，2026-10-03，WIP，B7 未接，不可部署）

- **管理入口**：沿原 `/plugins enable` 接 `--read-root`/`--write-root`/`--network`/`--program-root`。完整事实、下一授权请求身份和目录版本进入同一确认码；原 HostCommand 登记/查询/重送仍是唯一运输，不另造授权账。普通身份在提交/查询前拒绝；25 个根的预览不截断，但真实 TUI/飞书送达未验证。
- **重新授权**：删除老进程插件的 enabled 直返；兼容、restricted、wide 在 `enable_mode` 一处判断。预览不撤旧；确认后沿原精确撤权/释放链，旧退出未知不开放新代，仍 `outcome_unknown`。仅原表相同撤旧子提交可补“撤权已观察、清理未验证”，不暴露 UNKNOWN 工具结果。
- **配置真消费**：YAML → 正式管理上下文 → 原执行器冻结授权。默认开走 restricted，默认关走完整 wide 确认；已经 restricted 的重新授权不降级。B7 未到时确认后明确 `legacy_sandbox_pending`、零候选启动，不能把这一拒绝说成已隔离。
- **回滚**：**v4 表对 17i 不可读，会 fail-closed**；旧读取返回 `invalid_state`，不覆盖原记录。提供新运行时两步 `scripts/export_plugin_installations_v3.py`，独立导出 v3，安装/启用/原代次保留，去掉新权限和兼容授权，修复旧回执摘要并完整披露损失。用固定 `22e02c668` 旧读取函数核对；部署前先导出、备份并维护停写，详见 [回滚步骤](docs/design/PLUGIN_LEGACY_PERMISSIONS.md#64-17i-回滚兼容与显式-v3-导出)。实际生产回滚未验证。
- **回归**：补原管理入口、YAML 真消费、更新/重装三种权限来源、v1/v2/v3→v4→旧可读 v3、独立 CLI 确认/源漂移/坏表/链接/不覆盖；保留 v8 关闭门全部行为断言，v6 夹具改为实际完整授权构造，旧清单字节不改。命令和实际结果见 TESTS。
- **下一步**：真实长预览运输、候选/业务/面板统一 B7 接线、启动时根/程序身份漂移复核、非作者本段审查与 9b/真 OS 验收；本段不复制沙箱规则，不启动生产 Gateway。

## 老格式插件权限第一段（opp，2026-10-03，WIP，未接启用运输/B7，不可部署）

- **裁定**：3a 2026-10-03 定，见 [设计稿第 6.1 节](docs/design/PLUGIN_LEGACY_PERMISSIONS.md#61-裁定3a-2026-10-03-定)。一次性兼容仅升级前已启用的旧激活；R/W/N/E 明确授权，N 含回环/公网/监听，E 通用程序路径/前缀；D 沿旧激活已知边界；v7 当次输入读墙另片后续。
- **本段**：`plugin_permissions/` 生成静态授权、确认码事实、固定计划绑定与 B7 构造器请求；授权进入原激活摘要，原安装表升 v4，显式 v3 来源仅旧 active 迁移兼容，查询不写盘；撤销清兼容、同代发布/撤销不换授权。管理列表/详情共用不泄普通身份路径的文字，授权策略与 OS 未验证分开。
- **配置**：`plugin_legacy_sandbox_default=true` 同步 YAML/dataclass/normalize、参数边界和管理员 settings 范围，模型返回 PARAMETER_BOUNDARY。当前仅策略合同与管理投影读取，启用执行器尚未消费，不能把新配置当已上线收紧。
- **验证**：6 个聚焦文件全绿；guards9 十文件全绿；六处真实单变异红→原字节恢复→同目标绿。静态与尺寸结果见 TESTS。
- **未完成**：权限参数入口、原确认运输完整接线、重新启用撤旧代并重授权（原 enabled 幂等分支尚未替换）、更新/重装完整转换回归、B7 候选/业务/面板接入及非作者初审。现有合同不证明新启用已受保护；G2b 服务端强制与 OS 真机/真实 TUI 飞书未验证。

## G3 本机客户端附带凭据：返工为「读不到即降级，只在 G2b 开关打开时拒绝」（2026-10-03，g3f，worker/sol1-g3-clients，返工完成，待复审）

- **起因（3a 定）**：现在是 G2a 阶段，服务端只计数、不拦。客户端在凭据缺失/权限不对/内容损坏时于发 HTTP 前就拒绝，等于提前做了 G2b 的事——新运行时的 TUI 连上还没生成凭据的旧 Gateway（部署窗口里会遇到），或凭据文件权限被人改过，TUI、飞书适配器、插件命令就全断了。
- **改法**：凭据读不到时客户端照常发请求，只是不带 `X-Gateway-Token`，由服务端按 G2a 计入“无凭据”；客户端记一条只带 G1 原因码的结构化 warning（不含凭据内容与路径），同一原因同一进程只记一次，不刷屏。只有配置项 `gateway_require_local_credential` 为 true（G2b 的开关，默认 false；9b 终审：目前只有客户端读它，服务端强制等 G2b 落地）时，才按原写法在发请求前拒绝、零请求并给原因码。服务端强制在 G2b 落地前不生效，落地后两边口径一致。非空 `gateway_auth_token` 优先且不读本机凭据、direct 模式不读凭据，两条不变。
- **实现**：`gateway_parts/client_credentials.py` 的 `GatewayClientCredentials.headers()` 按 `require_local_credential` 分流，新增 `_warn_credential_degraded`（模块级去重集合 + 锁）；开关进 `settings/config.py` 与 `config/agent_config.yaml`（默认 false，注释写明退场计划）；`user_config_capability._CREDENTIAL_SWITCH_KEYS` 把它排除在凭据脱敏之外（回显要能看到 true/false），它仍是安全边界项、模型不能改。所有原身份头调用点接口不变，只改注释。
- **验证**：三类原因 × 开关关降级（请求照发、无令牌头、warning 只记一次）/ 开关开拒绝（零请求 + 原因码）× TUI/CLI/IM 全绿；原有“附上凭据”用例全部保留。变异与命令见 TESTS。
- **未验证**：真实 Gateway、真实 TUI/飞书渠道、旧客户端迁移与 `/status` 计数归零仍未做；G2b 强制的服务端半边仍归 G2b 那一块。

## 私有写/私有锁的符号链接锚点：允许跟随一次（pdp 后续，2026-10-04，分支 `worker/private-dirs-policy`，ds4 初审、3a 裁定已落实，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **背景**：ds4 初审小问题——「最近的已存在祖先本身是符号链接、下面要新建子目录」时私有写直接抛 NoFollowPathError；用户的项目目录很可能就是符号链接（指到外置盘），`tooling/shell.py` 在工作区下建 `.background_jobs` 会失败，后台任务起不来。生产 owner home 里也确实有目录链接。
- **3a 裁定（与「只动自己建的东西」一致）**：最近的已存在祖先可以**跟随一次**（它是已存在的目录，权限照旧一位不动），从它的真实目录往下新建缺失的各段；新建段不跟随符号链接、按 0700 建。私有锁 `open_private_lock_beneath` 同一口径（两边一个规则）。
- **实现**：`nofollow_fs.resolve_existing_symlink_anchor`（共享辅助）：锚点是符号链接 → `os.path.realpath` 解析到真实目录；目标不是目录（断链/指向文件）抛 NoFollowPathError；非链接原样返回。`json_io._ensure_private_dir` 与 `open_private_lock_beneath` 都过它；新建段仍逐段 no-follow（段内链接一律拒绝）。
- **用例**：`test_private_dirs_policy.py` 三条——工作区根链接 → `.background_jobs` 在链接目标里 0700 建好、链接与目标权限不动；祖先位置是断链/指向文件的链接 → 拒绝（原语层 parts 段已存在链接也拒绝）；私有锁在链接锚点下把锁文件建到真实目录 0600。
- **验证**：见 TESTS.md 同名节（3 个失败文件 56 passed、29 相关文件 323 passed、guards9 172 passed、门禁全过、变异 2 KILLED + 1 等价存活说明）。


## 私有写也只动自己建的东西（pdp，2026-10-03，分支 `worker/private-dirs-policy`，基于集成头 `3a42f457d`，ds4 初审，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **背景**：pw2 初审（pw2r）检查 4 发现口径冲突——私有写会顺带把直接父目录收紧到 0700（`_ensure_private_dir` 对已存在目录 chmod），而锁收私（lkp/ds8）已定为「只动自己建的东西」；同一次写入里锁不动、写却动。3a 裁定两处统一。
- **3a 裁定**：
  1. 自己新建的目录按 0700 建、新建文件 0600；文件本身仍做 fchmod 自愈（这条保留）。
  2. **已经存在的目录，不管是谁的、权限多宽，一律不改权限**。理由：文件 0600 已经挡住内容；调用方可能把目录指到用户能看到的地方（如 `shell_gateway_execution` 的 artifact_dir、可配置的 audit_log_path）；与锁口径一致。
  3. 存量宿主目录的收紧归 owner 维护那一处负责，不在每次写入时做（本次不改维护侧，记后续项）。
- **改了什么**：
  - `common/json_io._ensure_private_dir`：删除对已存在目录的 stat+chmod；缺失目录改走锁同款「已存在最近祖先 + 缺失段」手法，经 `open_directory_beneath(create=True)` 逐段按 0700 新建（no-follow、不跟随符号链接）；已存在（含符号链接）直接返回一位不动。
  - 「先普通 mkdir 再私有写」的建目录点全部改 `mkdir(..., mode=0o700)`（只影响缺失目录的新建权限）。
  - **目录表（缺失时新建权限；已存在的一律不动）**：

    | 目录 | 谁先建 | 改前 | 改后 |
    | --- | --- | --- | --- |
    | memory/daily | DailyMemoryStore.__init__ 普通 mkdir（靠首次 append 收紧） | 0755 | 0700 |
    | memory/candidates、lessons、routing、memory-hot | 各仓库 __init__ 普通 mkdir | 0755 | 0700 |
    | memory/curator/transactions（含每 run 子目录） | CuratorCommitter 普通 mkdir | 0755 | 0700 |
    | .background_jobs | shell 普通 mkdir | 0755 | 0700 |
    | 审计根（audit_log_path） | AuditLogger 普通 mkdir | 0755 | 0700 |
    | collaboration cases/requests/evidence/participants/decisions | store 普通 mkdir | 0755 | 0700 |
    | Gateway 队列/状态目录（write_json_file 先建时） | gateway io 普通 mkdir | 0755 | 0700 |
    | scheduler 根 | SchedulerRepository 普通 mkdir | 0755 | 0700 |
    | owner home 身份/生命周期/运行工作区目录 | 各模块普通 mkdir | 0755 | 0700 |
    | jsonl 账本目录（append_line_locked） | io/jsonl 普通 mkdir | 0755 | 0700 |
    | local store 根/files/events 目录 | schema 普通 mkdir | 0755 | 0700 |
    | conversations 各托管目录（threads/messages/observations/...） | ConversationStorage.ensure_dirs 普通 mkdir | 0755 | 0700 |
    | subagent 任务各目录、debug details、trash、测试报告 | 各模块普通 mkdir | 0755 | 0700 |
    | **已存在的目录（任何来源，含用户目录）** | —— | 被收紧 0700 | **一位不动** |

- **用例**：新增 `tests/test_private_dirs_policy.py`（10 条）；旧断言按新口径逐条改（batch2 7 处、memory_file_permissions 5 处、global_index 1 处、context_bundle 2 处、private_lock 1 处、conversation 1 处——见 TESTS.md）。
- **顺带修复**：pw2 引入的 `subagents/execution/report.py` 导入点数错误（`....common` 应为 `...common`），该模块此前无法导入、CLI 构建解析器会崩；已在基线 `3a42f457d` 复核确认为既有失败。
- **第一批（ds9）函数注释补齐**：39 个改动函数补 `# LLM:` + `# 函数用途:` 双层注释（audit / collaboration / gateway / local_storage / subagents / user_space 共 15 个文件）。
- **已知边界**：inbox 的 `.lock` 文件会随请求累积（pw2 遗留），清理留给 owner 维护后续项；本次不改。
- **验证**：见 TESTS.md 同名节。


## 宿主数据私有写入第二批（pw2，2026-10-03，分支 `worker/private-writes-batch2`，基于 pwf 头 `9bd2fc318`，已实现，ds3 初审，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **背景**：pwf 把 ds9 清单里能明确判定的宿主数据写入点收成了私有原语，并把三类拿不准的列出来交 3a 定。3a 裁定三类**全部要收**：①`append_jsonl_records` / `append_jsonl_capped` 家族剩余调用点；②`gateway_parts/io` 的通用 JSON 写函数；③subagents 里剩余的整份报告 / 任务文件 `write_text`。
- **口径**（与第一批相同）：新文件 0600、新目录 0700、已有宽权限文件下次写入收紧；**内容逐字节不变、调用方接口不变**；用户工作区里的文件（模型交付物、`output/`、用户项目文件）不动。
- **新增私有原语**（`common/json_io.py`，均为已有公开原语的权限私有版，格式逐字节一致）：
  - `append_private_jsonl_capped`（有界追加；对应 `append_jsonl_capped`）
  - `write_private_json_file_atomic_no_newline[_unlocked]`（不带尾换行；对应 Gateway 原实现）
  - `write_private_json_object`（带尾换行；对应 `write_json_object`）
- **A 组：append 家族 7 个调用点**

| 位置 | 数据 | 处理 |
| --- | --- | --- |
| capability/persona_repository.py:750 | persona 版本账本 `versions.jsonl` | `append_private_jsonl_records` |
| scheduler/repository.py:882 | 调度器历史账本 | `append_private_jsonl_records` |
| agent_core/run_task_workspace_writer.py:201 | 任务 `work/timeline.jsonl` | `append_private_jsonl_records` |
| settings/parameter_changes.py:264 | 参数修改账本 `settings-changes.jsonl` | `append_private_jsonl_capped`（并删掉自建 0600 空文件的 `_ensure_private_file`，私有 capped 新建即 0600） |
| tooling/shell.py:825 | `.background_jobs/registry.jsonl` | `append_private_jsonl_capped` |
| capability/skill_learning_store.py:270 | skill learning `ledger.jsonl` | `append_private_jsonl_capped` |
| memory_store/operations.py:47、:78 | 记忆操作审计 `ops.jsonl`、硬删除墓碑 | `append_private_jsonl_capped` |

- **B 组：`gateway_parts/io.py` 三个通用写函数**
  - `write_json_file_atomic`、`update_json_file_atomic`、`write_gateway_request` 改走私有原子写；原签名、锁协议、序列化格式（`indent=2`、`sort_keys=True`、无尾换行）不变。
  - **调用方核对（AST 扫描，共 42 个模块从 `gateway_parts.io` 导入这三个函数）**：`collaboration/store`、`conversation/{goal_progress_fuse,session_pair_rate,session_tasks,store_audits,store_claims,store_goals,store_guidance,store_guidance_acknowledgements,store_guidance_ledger,store_guidance_recovery,store_guidance_submission,store_observations,store_progress,store_tasks,store_threads,store_wake_attempts,store_wakes}`、`gateway_parts/{control_operation_service,control_service,http_handlers,http_service,input_delivery_service,lease_service,permission_bridge,queue_service,recovery,request_binding,request_client,request_experiment,request_experiment_records,request_worker,restart_service}`、`memory_store/retention_apply`、`owner_wake_discovery`、`scale_downstream`、`cli/{adapter,gateway_process,gateway_restart_handover,chat_parts/tui_control_delivery,chat_parts/tui_input_delivery}`。
  - **这些写的是 Gateway 根下的宿主运行数据**：请求队列（inbox/processing/done/failed/responses）、会话与任务存储、控制面/租约/实验记录状态、adapter 状态（`adapter_state.json`）、停启请求（`stop_request`）、重启交接状态、TUI 投递状态。读写方（TUI、适配器、派活工具、后台服务）都在同一系统用户下，收紧到 0600 不影响它们。
- **C 组：subagents 9 个文件的写点**

| 文件 | 数据 | 处理 |
| --- | --- | --- |
| patch/patch_service.py:178,227 | 补丁复审 Markdown、`output.json` | `write_private_text_file_atomic` |
| patch/patch_apply_task.py:231,349 | `output.json` | `write_private_text_file_atomic` |
| execution/report.py:104,108 | 测试执行报告 JSON/MD | `write_private_text_file_atomic` |
| result_processors.py:166,168,169,170,201 | runner 提示词/回复/结果 JSON、DEBRIEF 头 | `write_private_text_file_atomic` / `write_private_json_file_atomic_no_newline` |
| probe.py:118 | 通道探针证据文件 | `write_private_text_file_atomic` |
| manager_work_orders.py:136 | 接管文件 | `write_private_text_file_atomic` |
| services/hierarchy/service.py:63,67,87,91 | 领导恢复计划/应用报告 JSON/MD | `write_private_text_file_atomic` |
| task_trash.py:111 | 回收站清单 | `append_private_text` |
| shell_gateway_execution.py:190,198 | 审计 JSONL（另 `write_bytes` 输出产物不动） | `append_private_text` |
| runner_context_bundle_files.py:39,43,58,59 | 上下文快照 JSON/MD | `write_private_json_object` / `write_private_text_file_atomic` |
| utils.py:56,64 | 工单模板文本/JSON（`_write_if_missing`、`_write_json_if_missing`） | `write_private_text_file_atomic` / `write_private_json_object` |

- **不动**：`patch/patch_file_ops.py` 写的是**用户补丁的目标文件**，按口径不改；`shell_gateway_execution._write_output` 写的是命令产物（可能落用户目录），不改。
- **语义保持的细节**：`_write_if_missing` / `_write_json_if_missing` 的"文件已存在就不碰"语义不变（因此已存在的宽权限文件不会被这两个函数收紧，只有新建走私有）；`update_json_file_atomic` 的读-改-写仍在一把锁内完成。
- **验证**：新增 `tests/test_private_writes_batch2.py`（18 条，A/B/C 三组）；7 个变异（A 组 2 个、B 组 3 个、C 组 2 个，均为"退回跟随 umask 的公开写法"）全部被抓；相关回归与门禁结果见 TESTS.md。

## 私有锁只动自己建的东西（lkp，2026-10-03，同一分支 `worker/ds3-lock-private`，基于 `459354e1b`，已实现，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **背景**：锁收私与 step17i 上下文快照口径相撞。快照的私有写入要拿 json_io 的锁，而 `open_private_lock_beneath` 会把锁所在的、**已经存在**的目录 `fchmod(0o700)`；快照的口径是「归档目录是符号链接时，链接所在级和下级目录的权限不动」，于是链接目标里的日期目录被从 0755 改成 0700，两条符号链接用例失败（3a 沙箱外实测）。
- **3a 裁定（私有锁只动自己建的东西）**：
  1. 自己新建的目录用 0700 建（no-follow 原语的 `mkdir(mode=0o700)` 保证，不受 umask 放宽）；
  2. 锁文件本身 0600，拿到锁后对锁文件 `fchmod` 自愈（这条不变）；
  3. **已经存在的目录，不管是谁的，一律不改权限**。理由：别人抢锁要先能打开锁文件，锁文件 0600 就已经挡住了；目录权限不是防线。这样与上下文快照、owner 维护的符号链接口径一致。
- **改了什么**：`common/nofollow_fs.open_private_lock_beneath` 删除对父目录的 `os.fchmod(parent_fd, 0o700)`（锁文件 0600 自愈与 no-follow 校验不变）；四个调用方（json_io / jsonl / dispatch / daemon_metadata）的注释与 gateway 模块文档同步；`test_private_lock_permissions.py` 按新口径改用例并补 2 条（10 → 12 项）。
- **用例变化**：
  - 新建场景保持「锁 0600、新建目录 0700」（json_io / dispatch / daemon）；
  - jsonl 场景：目录由 `append_line_locked` 先按 umask 建（0755），锁不再替调用方收紧，断言改为「目录 0755 不动」；
  - 两处存量用例：预置 0644 锁 / 0755 目录，获取后锁收紧到 0600、**目录保持 0755 不动**；
  - 新增 `test_lock_beneath_symlinked_ancestor_keeps_existing_dir_modes`：已存在的、经符号链接进入的目录里拿锁（step17i 等价场景），目录权限不变、锁文件 0600、符号链接不被替换；
  - 新增 `test_primitive_leaves_existing_dir_mode_untouched`：原语级，已存在的 0750 目录里拿锁，目录一位不动。
- **变异（4 个，全部被抓到，跑完原字节还原 sha256 一致）**：
  1. **对已存在目录 chmod**（把 `fchmod(parent_fd, 0o700)` 加回去；替换原「父目录不收紧」变异）→ **5 条失败**；
  2. 新建锁不传权限（0o600 → 0o222）→ 8 条失败；
  3. 不做 fchmod 自愈 → 2 条失败；
  4. 锁叶子符号链接跟随（放行 S_ISLNK + 去 O_NOFOLLOW + 去打开后身份复核）→ 2 条失败。
- **验证**：17 文件 222 passed / 1 failed（唯一失败为沙箱环境限制，基线同样失败）；guards9 172 passed；静态门禁全过。详见 TESTS.md「私有锁只动自己建的东西」。
- **未验证**：与 step17i 合跑的两条符号链接用例由 3a 沙箱外验证；真实快照链路与真实 Gateway 未跑。

## 宿主数据私有写入后续（pwf，2026-10-03，分支 `worker/private-writes-followup`，基于 ds3 头 `199879045`，已实现，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **背景**：ds3 锁收私后，仍有一批宿主运行数据走跟随 umask 的 `append_jsonl` / `write_text` / `write_json_file_atomic`（新建 0644 文件、0755 目录）。本次把写宿主数据的地方统一收成私有原语：新文件 0600、新目录 0700、已有宽权限文件下次写入时收紧；只改写入方式，调用方接口与内容逐字节不变（`sort_keys` 语义原样保留，例如审计账仍不排序键）。
- **逐个核对表**（写什么数据 / 目录 / 父目录改前状态，按代码事实；本轮不读真实 owner home，未做 lstat 复核）：

| 位置 | 数据 | 目录 | 改前父目录 | 处理 |
| --- | --- | --- | --- | --- |
| conversation/store_usage.py | 每会话模型用量 JSONL | 会话用量目录（Store 注入） | 0755（mkdir 跟随 umask） | 改私有追加 |
| conversation/agent_transcript.py | 子代理展示事件 JSONL（含压缩重写） | `<conversation root>/agent_transcript_events/` | 0755 | 改私有追加 + 私有原子写 |
| conversation/store_guidance.py、store_guidance_ledger.py | 插话队列与回执投影 JSONL | guidance 目录 | 0755 | 改私有追加 |
| conversation/store_messages.py、store_observations.py、store_wakes.py、store_wake_publication.py、compact_checkpoint.py | 消息账 / 观察账 / 唤醒信号 / 发布回执 / checkpoint（ds3 清单未列，同属会话宿主数据） | conversation root 下 | 0755 | 一并改私有 |
| audit/logger.py | 审计账 `audit-*.jsonl`、旁路错误账 | `<audit_log_path>` | 0755 | 改私有追加 |
| local_storage/events.py | events.jsonl | LocalStore root | 0755 | 改私有追加 |
| gateway_parts/io.py | 请求历史 `gateway_requests.jsonl`；history transition 锁 | gateway root | 0755 | 改私有追加/原子写；**补第四处锁写法** |
| collaboration/store.py | 参与者 / 证据 / 裁决 / 请求 JSONL | `<collab root>/{participants,evidence,decisions,requests}/` | 0755 | 改私有追加 |
| user_space/identity_store.py | provider 身份索引、canonical 档案、关联账、canonical 记忆 note | owner home 下 | 0755 | 改私有追加 / 原子写 |
| user_space/owner_lifecycle.py | owner_status.json、owner 审计账 | owner home 根 | 0755 | 改私有 |
| user_space/run_workspace.py | work/task.yaml、run_workspace.json、state.json、timeline.jsonl、协作种子 | `<owner_home>/tasks/.../work/` | 0755 | 改私有（`output/` 交付物目录与模型写入的用户文件不动） |
| memory_store/retention_apply.py | owner 审计、tombstone、回滚写回 | owner trash/retention 等 | 0755 | 改私有 |
| memory_archive/compact_apply | ledger/context/handoff 等 | apply workspace | 已是私有（此前批次） | 无需改（ds3 清单该项已过时） |
| subagents/dispatch、capability、patch、debug_trace、recovery、actions、parent_planner | 派工 / 守望 / 路由 / 补丁 / 审阅 / 恢复 / 动作 / 父规划账与 `.md` 展示日志、调试明细、任务工作日志 | 子代理 workspace / 任务目录 | 0755 | 改私有 |

- **第四处锁写法（ds3 遗漏）**：`gateway_parts/io.py` 自己的 `_open_lock_handle` 原来 `lock_path.open("a+") + mkdir` 跟随 umask（0644 锁 / 0755 目录）。本次按 ds3 同一手法改经 `open_private_lock_beneath_tightened`（0600、父目录 0700、存量 0644 打开时收紧），flock / Windows byte-lock / `blocking` 语义不变。
- **边界**：只动列出的宿主数据写入点；模型写的交付物、用户项目文件、`output/` 目录权限照旧；不改 Windows 回退分支；不做全盘扫描、不碰第三方包锁。
- **未改（列给 3a 定，同类但不在本次清单）**：
  - `append_jsonl_records`（批量追加）调用点：`agent_core/run_task_workspace_writer.py`、`capability/persona_repository.py`、`capability/skill_learning_store.py`、`scheduler/repository.py`、`settings/parameter_changes.py`、`tooling/shell.py`、`memory_store/operations.py`。
  - `gateway_parts/io.py` 的通用 `write_json_file_atomic` / `update_json_file_atomic` / `write_gateway_request`（请求队列 JSON 与大量调用方；改它等于全模块口径变更，本次未动）。
  - subagents 里剩余的整份报告 / 任务文件 `write_text`（patch_service、patch_apply_task、execution/report、hierarchy/service、result_processors、probe、task_trash、manager_work_orders、shell_gateway_execution 等）；`patch_file_ops` 写的是用户补丁目标文件，按规则不动。
- **验证**：新增 5 个测试文件（umask 0o022 断言 0600/0700、存量收紧、内容逐字节不变）；每组 1 个变异（退回 `append_jsonl`）全部被抓到；相关回归、guards9 与静态门禁结果见 TESTS.md。

## 写锁 sidecar（.wlock）同批收私（lkf，2026-10-03，同一分支 `worker/ds3-lock-private`，基于 `199879045`，已实现，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **起因**：ds1（lockr）初审了 ds3 的锁收私交付，结论「必须改 1 + 小问题 4」。3a 采纳后把范围收缩：初审的必须改 1 是 `gateway_parts/io.py` 自带的 `_locked_file_path`/`_open_lock_handle`（仍是 `mkdir` + `open("a+")`，0644 且会静默跟随符号链接），但**那处已由 ds9 在 `worker/private-writes-followup` `17dc9781b` 修改，本分支不再碰 io.py 的锁，3a 合并时用 ds9 的版本**；本轮只收本分支归属的同类旧写法。
- **本轮的必须改**：`gateway_parts/daemon_metadata._flocked_sidecar` 的 `.wlock` 原来用 `lock_path.open("a+")`（初审实测 0644）。现经 `_open_private_wlock_descriptor` → `common/nofollow_fs.open_private_lock_beneath_tightened` 拿 fd：文件 0600、缺失目录按 0700 新建、已存在的目录一律不动（lkp 2026-10-03 修正，见顶部条目）、符号链接/硬链接锁抛 `NoFollowPathError`、存量 0644 下次加锁即自愈；只 flock 不写内容，阻塞语义与 Windows 降级告警不变。`.wlock` 后缀刻意避开 `scoped_locks` 的 `glob("*.lock")` 扫描，不改。
- **锁路径父目录核对（逐个核对，结论：都在宿主自己的目录里，没有用户工作区目录）**：
  | 锁 | 路径来源 | 父目录口径 |
  |---|---|---|
  | `gateway.pid.wlock` / `gateway_state.json.wlock` / `gateway_stop.request.wlock` | `GatewayPaths.root`（当前 owner 的 service runtime：`workspace/runtime/services/gateway/`） | 宿主运行目录，收紧 0700 可接受 |
  | scoped lock 的 `*.lock.wlock` | `XDG_STATE_HOME/my-agent/locks`（`scoped_locks._get_lock_dir`） | 机器级状态目录，同上 |
  | `_write_json_file` 的临时文件与原子替换 | 与上述目标同目录 | 不变 |
  这套 helper 只服务 PID 记录 / 运行时状态 / scoped lock 心跳 / 停止请求，路径全部由宿主构造，没有一处跟着用户任务目录走。
- **非阻塞语义**：`try_gateway_turn_transition` 走 `try_locked_file_transition`（线程锁 `acquire(blocking=False)` + `_try_flock_exclusive`），与阻塞入口是两套实现；本轮补 3 条用例钉住「被占用时立即返回 False、空闲时 True、释放后可再取」，并用变异（改成阻塞入口）验证——该变异会让用例永久挂住（120 秒超时），正说明这条用例真的守着非阻塞语义。
- **已知边界（本轮写入，来自初审风险 2/3）**：
  - 「向上找最近已存在父目录」会**穿过更高层符号链接**：直接父目录是符号链接会拒绝，但祖父级是符号链接时锁会建到链接指向的真实目录，不报错。lkp 2026-10-03 已补用例钉住这条路径的权限行为（经符号链接进入的已存在目录权限不变），见顶部条目。
  - `open_private_lock_beneath_tightened` 的 `fchmod` 失败被 `except OSError: pass` 吞掉：宁可「收紧失败不拦业务」是有意的，但「自愈没生效」运行时不可观测，没有埋点或 `/status` 口径；要看最终收敛效果得另加只读计数。
- **验证**：新增 6 条用例（wlock 三项 + 非阻塞三项）；变异 4 个全部被抓到；直接相关 26 文件 329 passed / 4 xfailed，guards9 172 passed，静态门禁全过。详见 TESTS.md「写锁 sidecar（.wlock）同批收私」。

## 三套锁写法统一成私有（ds3l，2026-10-03，分支 `worker/ds3-lock-private`，基于 `claude/3a-step17i` `b6ede99e0`，已实现，ds1 初审，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **问题**（be 2026-10-03 只读评估）：本仓库建锁文件有三套写法，权限口径不统一。
  - A（对）`open_private_lock_beneath`：文件 0600、缺失目录按 0700 新建、O_NOFOLLOW/O_EXCL、空内容。
  - B（错）`common/json_io._locked_file_path`（`locked_json_path` 全部调用方）与 `io/jsonl._locked_text_file`（`append_jsonl`/`append_line_locked`）：用 `Path.open("a+")` + `mkdir()` 不显式给 mode，按 umask 落成 **0644 文件 / 0755 目录**。
  - C（没补齐）`_DispatchWatchLock`：新建给 0600，但父目录 `mkdir` 成 0755、不防符号链接、对已存在文件不 re-chmod，还把 `{token, pid, created_at}` 写进锁文件，pid 会随权限外泄。
  - 真实 home 实测：本项目空锁 **2158 个 0644**、仅 247 个 0600。同机他用户能 `open(O_RDONLY)` 后 `flock(LOCK_EX)`（flock 不要求写权限）卡住宿主对权威状态的写入。
- **修法**：
  - `nofollow_fs` 新增 `open_private_lock_beneath_tightened`：在 `open_private_lock_beneath` 之上，打开后**无条件把已存在锁收紧到 0600**（只收紧不放松、chmod 失败只放弃收紧）。自愈存量 0644 锁，不做全盘扫描、不碰第三方包锁。
  - B 两处与 C 都改成经该原语拿 fd 再 flock / 写元数据；调用方接口不变，`blocking=False`（同一 OS 锁的 LOCK_NB）与进程内线程锁层保留。
  - 绝对路径先拆成“已存在的最近父目录 + 其余段”（同 `directory_lock._existing_anchor` 手法），让缺失目录仍统一经 no-follow 原语按 0700 创建；**已存在的目录一律不改权限**（lkp 2026-10-03 修正，与上下文快照的符号链接口径一致）。
- **行为变化（需明确）**：锁叶子是符号链接/非单链接普通文件时，现在按 A 的既有口径抛 `NoFollowPathError`（原来 B 会静默跟随、C 会跟随）。这是加固，但会让“锁被换成符号链接”的畸形存量在**每一轮**都失败，而不是悄悄跟着写。锁与数据写入不同，故保留 fail-closed 而不是照 context_bundle 那次的“跳过收紧、记警告、照常工作”。
- **边界**：只动这三处已知锁路径；不碰 Windows msvcrt 回退分支的既有行为；`global_index/*.jsonl`、`memory/daily/*.jsonl` 的数据与目录权限是提交 2。
- **验证**：新增 `tests/test_private_lock_permissions.py`（8 项，umask 固定 0o022）；4 个变异全部被抓到。**lkp 修正后**为 12 项、变异重跑（含「对已存在目录 chmod」）全部被抓到，见顶部条目；详见 TESTS.md 同名条目。

## global_index 数据文件和目录收私（ds3l，2026-10-03，同一分支，已实现，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **问题**：be 顺带发现 `global_index/*.jsonl` 的**数据文件**（不只是锁）是 0644、目录 0755。它的追加还在走跟随 umask 的 `append_jsonl`。
- **改法**：`user_space/home_indexes.py` 的追加改走 `append_private_jsonl_records`，整份重建改走 `write_private_text_file_atomic_unlocked`；文件 0600、目录 0700、已有 0644 下次写入即收紧，内容逐字节不变。
- **对 be 观察的一处修正**：be 同批提到的 `memory/daily` **目录**经复核不成立——daily 分片本来就走 `write_private_text_file_atomic_unlocked`，其 `_ensure_private_dir` 已把 daily 目录收紧到 0700；已在干净基线 `b6ede99e0` 上实测确认（构造 store 并 append 后目录就是 0700）。故本次**不改 daily**，也没有对应用例。真正 0755 的是 daily 的 `.lock` 旁目录，已由提交 1 覆盖。
- **边界**：只改 global_index 一处，不扩到别的目录。**其它同类未修**（见交接报告）由 3a 定是否另开。

## G3 本机客户端附带凭据（2026-10-03，sol1c，worker/sol1-g3-clients，已并入 step17i（2adfd8518；9b 集成终审通过））

- 代码提交 a1c617fec（WIP）；新增39项和guards9均通过，相关回归保留9条失败，不能当完整端到端验收通过。两条已在G1基线复现失败，其余尚待3a复核；命令与真实结果见 TESTS。
- 在原身份头位置复用 G1 load_local_client_credential；TUI/CLI、插件普通与交互运输、IM ask/四个对账入口、仓库 HTTP 验收脚本统一附 X-Gateway-Token。文件队列与 direct 不改，不增加 HTTP 旁路。
- 配置 gateway_auth_token 优先且唯一，不先读本机文件；G1 原因码保持，失败在 HTTP 前明确拒绝并给安全处置提示。IM durable 未发送故障交原 worker 收口，不循环重试；插件/Goal 不把未发送说成执行未知。
- 凭据仅宿主进程内使用，不进日志、env、argv、异常、状态、持久队列或模型上下文。公开探针和其它服务（代理健康）不带 Gateway 凭据。
- 本分支只基于 G1，不含 G2a；不做服务端强制、浏览器或仓库外工具。真实渠道、生产旧客户端重启与计数观察、平台隔离未验证，仍由集成者确认后再启 G2b。测试/变异/边界见 TESTS 与 [设计稿](docs/design/GATEWAY_LOCAL_TRUST.md)。

## Gateway 本机客户端凭据与迁移观察 G1+G2a（2026-10-03，sol1g + ds1g 返工，worker/sol1-g1-g2a，已并入 step17i（6480a6624；9b 集成终审通过））

- 解决部署重启导致客户端凭据失效及凭据进入插件读范围的风险；凭据缺失才用 token_urlsafe(32) 生成，私有原子写落数据根 secrets/（0700、文件0600），停机不删，重启复用。
- 稳定宿主接口 load_local_client_credential(data_root) 严格校验属主、权限、普通文件和固定编码；缺失、权限、损坏分别有原因码，不返回空串、不打印底层路径/内容。
- 生产启动根只读 Agent 的 home_paths.root；凭据内容只保存在宿主内存，不进日志/env/argv；/status 只投影 `local_credential` 状态码（ok / unavailable:<原因码>），不含路径与内容。插件沙箱登记文件与目录（Linux H2只覆盖目录）；关闭沙箱的插件与宿主同信任域，Full Access 下不声称文件保密。
- G1 已独立提交 `cbb80b4360c168e50473d9371a73b497b6bf3c9d`（返工 `db1334395`），可供 G3 接续。G2a 识别本机/配置两种凭据，保留无凭据回环和未知 peer 的原身份规则，只观察、不拦截。
- ds1g 返工（9b 终审 2 条必须改 + 3a 采纳建议 3，提交 `db1334395`）：①凭据准备失败（坏文件/权限/无数据根）不再拦启动，降级为 /status 的 `local_credential` 状态码，Gateway 照常启动、回环请求照常计数、不轮换不覆盖坏文件；G2b 强制时改 fail-closed。②补用例钉住“空密钥不匹配空请求头”（9b 变异 G1 此前存活）。③增量：非回环来源出示本机凭据现在也算可信（符合 G2 定义，非“逐字节一样”）。
- `http_routes.py` 是 26 条实际分发的唯一结构化表；只计 credential/admin 两档，public/plugin_token 不计。`/status` 的 `uncredentialed_loopback_by_endpoint` 只含模板 → `{count,last_at}`，进程内加锁、不存请求编号/身份/凭据，重启清零。
- G2b/G3/G4/G5/G6 不在本片；G2a 并未关闭“无凭据回环仍是管理员”的过渡缺口。临时数据/随机端口组件验证不代替真实客户端或平台隔离。详见 GATEWAY_LOCAL_TRUST 与 TESTS。

## M1 B4 HTTP 覆盖与提示 owner 安全尾补（m1b4，2026-10-04，已实施，已并入 step17j；luna6 复核中，9b 随 B5 终审）

- luna6 完整初审与 3a 核实：频道白名单不能证明主 owner，local 非当前用户/tui 可解析为独立 owner。保持原 HTTP 入队事件点，复用 worker 的唯一 owner 解析，仅基础 owner 可发；未知失败不发、严格关闭零解析，不创建 scoped Agent 或另写身份映射。
- 新真实随机回环 HTTP 服务→请求 worker 认领/owner 解析→隔离 echo 回合→终态链覆盖三事件顺序、精确字段；实际 local-agent 正例、alice local/tui 负例及清线程断言。修前两负例有效红、修后两文件 19 项绿；去掉 owner 判定的生产源码副本变异被两负例抓到。最终回归和边界见 TESTS。
- 保留入队语义避免提示延后到认领或恢复重发；不将本机测试服务扩大为生产 Gateway、插件启用或 TUI/IM 验收。设计第 13/18 节 B4 两表已对齐“已实施，待复审”。
- b4g（2026-10-04）补全提示、回合、工具、命令的可选观察装配保护：缺配置/读取/路由异常不发事件且不影响业务，关闭零事件字段读取；只保护观察，不放宽原权限合同，验证见顶部同名节。

## M1 B4 拒绝组合及提前初审尾补（m1b4，2026-10-03，luna6 完整初审过，已并入 step17j）

- 真实 canonical 证明原 schema 类型错误应为 TOOL_PARAMETER_TYPE_INVALID，修用例不改执行器；另补保护目标策略拒绝、真实审批文件桥用户拒绝，核心为零 handler、零两种工具事件。聚焦四文件 78 项通过；最终扩展范围含完整 guards9 共 567 项通过，三项独立源变异杀死，全部静态门禁通过、尺寸新增 0，收尾见 TESTS。
- ds9 对 ce10d5fde..68c82ed07 提前初审“可以交终审”，无必须改；按 3a 转述补装配关闭、观察输入无参数独立检查。B3 normalize_event_fact 仅提示可保留正文，非提示剥除作为纵深防御，不扩大 text 授权。
- turn 按每次真实执行一对，重启 interrupted 后续跑另发一对，不新增跨重启去重。不等于完整 B4 生产启用，未验边界保持。

## M1 B4 真实工具请求装配续段（m1b4，2026-10-03，已完成，已并入 step17j）

- 主/子身份来自已有宿主 RunScope；决策 actor 从 J16 自动执行的不可变请求传入，审批重入保留该身份，不读工具参数。Registry 接上原事件接缝，未扩改 B5 正在修改的执行器。
- Gateway 只转交已解析会话的通道；工具观察复用原权限链已生成的 RunScope，不重复读取任务库。审批模式拆成独立单层读取器，原错误仍保守 unavailable。
- 主/子/决策真实入口测试通过；假模型与计数工具的真实 Gateway/Agent 回合及控制命令已向握手假插件送齐六事件。拒绝组合的假模型错误码断言仍失败，待核对 canonical 结果；代码和正式非作者交叉初审仍按 WIP 交接，不标完整 B4 已实施。

## M1 B4 事件点续做（m1b4，2026-10-03，已完成，已并入 step17j）

- **基线**：按 3a 指定变基至 step17i `0ae0efe5e`，原两提交变为 `ce10d5fde`、`0c4355469`；文档保留双方有效内容，逐提交 diff-check 通过。原清单当前为 492 项（上游补一条默认线程数用例），全部通过；尺寸差集新增 0、消失 17。
- **Gateway 段**：队列首次成功写入后发布提示，幂等重放和活动插话不重复发；实际请求执行前/收口发布回合观察，重启接续投影 interrupted；新控制 completed 回执落定后发布命令观察，不重复副作用或观察。新增 `gateway_parts/event_points.py`，关闭先返回，复用已冻结 owner 路径与 B3 发布口，不读安装表或等待插件。
- **验证边界**：真实队列/控制回执/Gateway 执行入口、故障隔离用例通过；模型边界使用离线替身。多用户提示入队还没有冻结规范 owner 时保守不发布，不能把渠道身份猜成基础 owner。工具三来源生产装配和六事件假插件组合仍待下一段；B7 开关与真实隔离不在本块。

## 模型回合放弃时回收在途调用（luna3a，2026-10-03，分支 `worker/luna3-abandoned-calls`，提交 `a250c3936`，luna2 初审通过，DNS 与握手两处边界 3a 接受，并入 step17i）

- **现象与根因**：9b 的结构化只读证据显示，回合已报 `PROVIDERTIMEOUTERROR` 后，Responses 模型 worker 仍留在无首包的网络读取里；墙钟守卫此前只放弃账本句柄，没有把取消传给 worker/传输层。
- **修法**：墙钟超时先放弃这次调用，对精确 worker 置中断并按既有 1 秒排空窗口 join，再由上层捕获超时、收口原调用账（luna2 初审核对的实际顺序；排空窗口里 /status 仍可能把它算作在途）；SSE 关闭当前 response/socket，WebSocket 取消/异常直接 shutdown 当前 socket，正常终态才走 `close_timeout=5` 的关闭握手。WebSocket `response.create` 发送单独复用请求首事件预算，到期关 socket；错误仍沿既有 `first_event` 分类与重试规则。
- **后续项**（luna2 初审）：补一条“WebSocket 发送卡住时收到取消”的用例，区分直接关 socket 和正常关闭握手。
- **公开状态**：`GET /status` 增加 `in_flight_model_call_count` 与 `oldest_in_flight_model_call_age_seconds`，只从原模型调用账本只读计算，仅投影数字，不加第二状态源或模型/请求/回合身份。
- **等待点与取消事实**：

  | 等待点 | 当前期限 | 回合放弃后的结束方式 |
  | --- | --- | --- |
  | 系统 DNS `getaddrinfo` | **无本请求级可取消期限**；标准库同步解析不受 socket connect timeout 控制 | 若卡在系统解析器，取消回调尚无可关闭 socket，不能保证短时退出；这是明确的系统边界/剩余风险，不把它说成已解决。 |
  | SSE TCP/TLS 连接 | `_bounded_connect_timeout` 从 `connect_timeout` 与首读预算取较小值；TLS 复用同一连接预算 | 连接对象已绑定时 open guard shutdown/close socket；DNS 解析期间仍受上一行限制。 |
  | SSE POST 请求体发送 | 已连接 socket 用 `_request_initial_read_timeout(request)`（由现有首读/`request.timeout` 预算推出） | open guard shutdown/close 当前连接，唤醒阻塞写入。 |
  | SSE 响应头 | `_request_initial_read_timeout(request)` 设置 HTTP socket read timeout | open guard 关闭已登记连接；等待 DNS 时仍受 DNS 行边界限制。 |
  | SSE 首个事件 | `_stream_first_event_timeout(request)` 驱动 watchdog 和 read timeout | watchdog/中断 guard 关闭当前 socket，读取退出后原账本正常终态化。 |
  | WebSocket TCP/TLS/升级握手 | `open_timeout=max(1s, connect_timeout 或 10s)`，是一次打开操作的总预算，不在升级时重置 | 握手中未暴露可关闭连接句柄，取消后最迟依赖 open timeout 返回；连接打开后中断回调可直接 close socket。 |
  | WebSocket `response.create` 发送 | `_stream_first_event_timeout(request)`；独立 Timer 到期关闭当前 socket | 中断回调 `close_socket()` 唤醒 `sendall`，发送期限到期也直接 shutdown/close。 |
  | WebSocket 首事件与流内空闲 | 首事件用首读预算；每条有效消息后用 `max(1s, request.timeout)` 滚动期限，recv 最多每秒检查取消 | recv 超时抛原阶段超时；放弃/停止直接 close socket，不等待服务端关闭握手。 |
  | SSE 流内空闲 | 首条有效 data 后按 `request.timeout` 滚动 watchdog/socket read timeout | watchdog或中断 guard shutdown/close 当前响应；不等待无上限的 `readline()`。 |
  | 正常关闭/错误清理 | WebSocket 正常终态关闭握手 `close_timeout=5s`；SSE response close 为本地清理 | 正常关闭有上限；错误、取消和超时直接中止 WebSocket socket，避免关闭握手延迟线程退出。 |

- **验证边界**：本地随机端口假 WS/SSE 测试覆盖首事件、首事件后空闲、阻塞发送、wall-clock 放弃和账本结清；真实 Gateway、生产模型及 OS DNS 挂死未验证。没有新增配置或常数。

## 能力包：交付物未匹配（pdm，2026-10-03，分支 `worker/pack-deliverable-unmatched`，基于 `claude/3a-step17i` `c47d023b6`，3a 复审，并入 step17i）

- **起因**：B8 重试点 A10-t102（`decision-evidence/capability-packs-v2-b8/repilot-c47d023b6/`）：模型把交付物写成 `drama_text_delivery.v3`——内容是含正确 schema 的 json，只是文件名不匹配包的路径模式 `**/*.json`。宿主核验一个目标都没查（`closeout.target_count=0`）、包内检查程序一次没跑，返工提示还说“本回合没有写出”——把“换了文件名”误报成“没写”，模型重复收尾 2 次也不改名，回合照常结束。
- **根因**：`scan_workspace` 只记路径匹配任一包模式的文件，`_changed_paths` 从它算“本回合改过什么”——不匹配模式的文件在宿主眼里完全不存在：既不算交付、也进不了检查目标，最终事实里只有 `DELIVERABLE_MISSING`。
- **修法**（3a 定：不做前置硬拦、不改检查程序、不改包内容）：判定和 `DELIVERABLE_MISSING` 原因码不变，把两处提示改准确——返工提示说“没找到符合该模式的交付物；如果交付物用了别的文件名，请按包的约定命名”，宿主通知说“没找到符合 <路径模式> 的文件”；返工上限沿用 2 次，只读回合照旧不查。
- **验证**：新增 A10 形状用例（未匹配被准确报出、改名后通过、检查程序跑起来）；相关 7 个 `test_pack_verification_*` 文件与 guards9 全过；6 个变异全杀（不看基线、返工超上限、提示不带路径模式、提示不带改名指引、不记结构化事实、正常交付也返工）。命令与结果见 `TESTS.md`。

## 能力包块 6c 收口（p6c，2026-10-03，分支 `worker/pack-6c-wrapup`，基于 step17i `c47d023b6`，3a 复审，并入 step17i）

- **范围**：能力包 v2 的收口片——用户说明（`docs/guides/CAPABILITY_PACK_GUIDE.md`）补“反复改不对时，写后检查会怎么节流”（错误一次列全、连续失败 6 次暂停写后检查，P1–P3 的用户视角），并把“打开前的三个前提”更新为三条都已完成（第三条 `/stop` 打断已由块 6a 并入 step17i）；设计稿 `docs/design/CAPABILITY_PACKS_V2.md` 的状态行与第 7 节进度表更新到当前（块 1–5、7 已上线 main；6a、P1–P3 在 step17i；6b 在做；6c 本分支；块 8 全量重跑进行中）。
- **脱敏已知边界核实（9b 的中文贴路径，3a 记的后续项）**：探针实测维持现状——“找不到/Users/me/x.json”这类词字符直接贴路径不置空；等价的 lookbehind 改写不改变行为；任何能修好这个漏检的边界放宽（中文也算分隔）都会把“中文目录名相对路径”（`交付/tmp/x.json`、`交付/Users/x.json`）误伤成 `<redacted>`，两者在正则层面同构、区分不了。判定为取舍变更而不是安全修复，维持 9b 复核的取舍；设计稿补记 6c 核实结论，新用例 `test_redact_location_word_glued_paths_stay_a_known_cost` 锁定现状（改动必须同步用例与设计稿）。
- **块 4 遗留 strict xfail 核查**：无遗留。`test_full_access_shell_outside_the_task_tree_cannot_forge_the_ledger` 的 strict xfail 已在 `6141d332b`（随 H3 修复）去掉，`_recorded` 已按数据根新传法调整；当前 `test_pack_verification_protection.py`、`test_host_files_access.py` 都没有 xfail。
- **验证**：见 TESTS.md“能力包块 6c 收口”一节（两个变异均被拦截：边界放宽、去掉行首分支）。
- **未验证**：真实 TUI/飞书提示、生产部署、v2 冻结业务重跑（块 8 在跑）；沙箱内无法复验真实 Seatbelt/bwrap 用例与插件宿主类测试，留集成者沙箱外复核。

## 两件小后续（sfu，2026-10-03，分支 `worker/ds3-small-followups`，基于 `claude/3a-step17i` `2c5b8ed34`；3a 复审，并入 step17i）

- **决策设置撤销批次直接用提交的返回**（`agent_py_agent/agent/conversation/decision_policy.py`，提交 `e0354302e`）：`_try_commit_collected` 复核通过时把 `_commit_batch` 标记出的撤销请求直接带出来（`tuple | None`），不通过返回 `None` 交给调用方整批重算；`_mark_settings_batch` 的复核通过路径与重算用尽路径都直接用 `_commit_batch` 的返回，不再出锁后重读 `settings_cancelled`。删除 `_batched_cancels`（原注释写“调用方持 `_LOCK`”但调用点已出锁，合同与用法对不上）。行为不变：取消哪些句柄、顺序、结果码都与改前一致；新增用例钉住“返回的就是本批被标成撤销的那些”。
- **上下文快照补“符号链接 + 当天目录不存在”用例**（`agent_py_agent/tests/test_main_context_bundle_contract.py`，不改产品代码）：归档根是符号链接、当天日期目录还不存在时，目录链准备阶段就要建好目录、首写成功（文件 0600）、链接目标权限不变、记一条 `private_directory_symlink_skipped` 警告；删掉 `_skip_private_directory_chain` 里 `target.mkdir` 的变异会被该用例抓到。
- 详见 TESTS.md 同名小节。

## M1 B4 事件点（m1b4，2026-10-03，`worker/m1-b4-event-points`，基于 `d8970996c`，已并入 step17j）

- **解决问题**：把宿主六类事件减量成精确字段，防止观察接口泄露参数、输出和路径；事件点关闭时零发布，观察故障不反噬业务。
- **投影段**：新增 `plugin_events/points.py`，固定 facts 白名单、会话 SHA-256 引用和三种宿主 actor；提示走现有统一脱敏后截 4000 字。B3 在后台按已确认激活的清单订阅决定是否给该插件正文，事件点不读安装表。
- **实施计划**：投影合同先红后绿并提交；再按 Gateway 提示/回合/控制回执和执行器实际 handler 入口分别接线、补真实入口回归并提交。最后做至少六个源副本变异及规定门禁。禁止新增仓库交接文件，本段兼作实施导航。
- **执行器阶段**：`ToolExecutorRequest.event_context` 只由宿主注入，观察回调在 registry 参数准备完成后的实际 handler 边界触发；拒绝、审批等待、准备失败和幂等重放不发；只读重试只发一对。收口只复制 canonical 结果标量，不带参数/输出。独立文本评审指出嵌套事实值和参数准备时序缺口，已补反例并修正。
- **边界 / WIP**：尚未接 Gateway 提示、回合、控制命令，也尚未把生产 Registry、主/子/决策模型请求上下文装配到 B3 发布口。当前仅投影和执行器接缝完成，不能作为完整 B4 合入；第 13/18 节按 WIP 同步，不虚标“已实施”。B7 总开关配置及真实隔离不在本块，不声称生产启用。

## B3 事件中心：观察投递（M 线第一期，m1b3，2026-10-03，分支 `worker/m1-b3-event-hub`，提交 `c9571b037`、返工 `11a56269a`、返工 2 `d8970996c`、拆平 `815151369`，基于 step17i `b6ede99e0`，9b 两轮复审、ds2 初审通过，并入 step17i）

- **起因**：设计稿 [插件事件与收紧钩子](docs/design/PLUGIN_EVENT_HOOKS.md) 第 7 节：宿主事件按 owner 分区合并投给「已启用 + 清单订阅 + 握手声明 `my-agent/events`」的插件；投递走 B2 共用通道；事件点接线在 B4，本块只提供 publish 入口与组装。
- **做了什么**：新 `agent/plugin_events/protocol.py`（事件公共字段、`my-agent/events` 握手门、payload 组装与输入归一化）与 `agent/plugin_events/hub.py`（`PluginEventHub`）；`gateway_parts/plugin_panels_http.py` 加 `plugin_event_hub(server)`（懒建唯一 hub、与面板服务共用同一个池）与 `publish_plugin_event(server, owner, event)`（事件点统一入口），停机由 `close_plugin_channel` 一并关闭 hub。
- **投递语义**：只留最新（`dropped_before` = 槽建立后发布计数差）、一批最多 6 条一次 `events.observe`、每个（owner, 激活）同时只有一个发送任务（单在途；返工 2 起，此前按 owner）；publish 只加锁更新待发并调度后台线程池，永不阻塞、永不抛；退避分级沿用通道（连接级故障退避整条连接、请求级错误只算这一次）；回收只摘自己 acquire 过的失效激活（先取 `current_seq` 时间界再读表，不碰面板服务的连接）；计数只在内存，`hub.stats(owner_key)` 供 B6 展示。
- **边界**：B4 未接（本块无事件点）；v8 启用门未开（B7 前），全部用假插件与测试替身验证；不含收紧钩子（B5）。
- **返工（3a 复审后）**：`seq` 语义定为按（owner, 插件激活）各自计数、换代从 1 重新开始（此前实现按（owner, 插件）计数、换代保留序号）；新槽（中途启用）的水位对齐到「当前最新一条之前」，`dropped_before` 只算槽建立之后被合并的条数（此前从零水位起算，会把启用前的发布算进去）。补双插件/双 owner/换代 seq 与中途启用 dropped=0 用例；两条真线程池用例改门闩 + submit 计数同步，不靠 sleep 窗口。变异 10 个（原 8 + seq 全局、零水位）全部杀死。
- **返工 2（9b 复审后）**：调度粒度从「每 owner 一个投递循环」改成「每（owner, 激活）一个发送任务」（`_inflight` 标记、线程池 4 个、与面板服务同档），一个插件挂住只占自己的线程；`publish_plugin_event` 补 `except Exception` 兜底；删锁内恒真版本比较死代码；hub 每轮对自管连接调 `close_idle`；`MAX_BATCH_EVENTS` 直接引用 `MAX_PLUGIN_EVENT_COUNT`；hub 池调用改用独立 `pool_clock`（原先 acquire 传墙钟，会让面板侧空闲关闭失效、hub 侧误关面板刚用过的连接）。补 H1/H9 用例与挂住插件隔离、空闲关闭、池时钟、publish 兜底用例。调度改动后类 span 237 超 near-soft，把发送/收尾/记账与池维护拆成模块级函数（`815151369`，纯结构重构、行为不变），15 个变异同步锚点后全部重跑。
- **验证与变异**：事件中心 32 项用例通过；15 个变异全部被杀死；命令与结果见 [TESTS](TESTS.md) 顶部「B3 事件中心」小节。

## memory_archive/context_bundles 快照私有写（luna2cb，2026-10-03，并入 step17i）

- **问题**：上下文包快照含会话上下文；此前首次写入、latest 副本和合同字段重写走 `Path.write_text`，新文件/目录可能成为 0644/0755。
- **修复**：三处统一使用 `common/json_io.write_private_text_file_atomic`；新增锚定 memory_archive 根、逐级 0700 创建/收紧 snapshots、context_bundles、日期目录的 helper，不 chmod memory_archive 根及其父目录。旧路径、文件名、JSON/Markdown 字节格式、返回值和调用顺序不变。
- **验证**：临时目录的 0600/0700、旧 latest 收紧、重写与字节稳定性、三项变异及所有门禁见 TESTS.md 同名节；未读取真实 owner home。
- **9b 复审补正**：memory_archive 根或其下任一级遇到符号链接时，跳过链接所在级及下级目录 chmod，以结构化 warning 记录 `reason_code=private_directory_symlink_skipped`；四个快照/latest 文件仍用私有原子写为 0600，回合不因链接抛错。
- **目录锚点**：build_main_context_bundle 的保存入口只准备一次目录链；显式携带 archive root、目标目录与是否跳过权限收紧的结果供首次写入、latest 刷新及合同字段重写复用，不再按 `parents[]` 反推。复审后的红绿、C1–C6 与门禁回执见 TESTS.md 同名节。
## M1 B5 第4段精确批准重跑（2026-10-04，m1b5，接 `471b7b4fc`，整体 WIP）

- 第3段主/子与隔离I4已提交 `471b7b4fc4ef47a284925ad43da89edccefd1b2f`，父 `57f465dca`。本段只读原宿主write_boundary.approved_actions，外层APPROVED/approval_id/tool/run身份与内层六身份逐字段核对；auto/宿主不要求确认时也能精确跳门，不另建批准源。
- 共用池原review循环每轮复读安装；只跳已批准的当前插件/version/activation/gate，其它门继续征询。版本鲜活复核红测修正去重/撤销身份；主/子等审批时换代仍ask，不能贴已应用批准事实；用户拒绝保留APPROVAL_REJECTED并带同源插件/原因前缀。
- 十四相关文件+当前完整十守卫 **539 passed in 75.83s**；六本段变异均有效AssertionError、原pytest exit=1/errors=0。具体红绿/误用与静态证据见TESTS，不把组件或隔离链当生产验收。
- 合并判定函数签名不变；GateCall新增尾字段approved_gate_refs、GateReview新增尾字段approval_applied。后者True是宿主已批准跳门、没有协议征询，ds2第5段决定账本应排除这类投影，不虚构plugin_gate.decided。第5/B6实读仍归ds2/3a，B7关闭门保持，完整复审/上线未做。

## M1 B5 第3段主/子消费者与 I4（2026-10-04，m1b5，接 `57f465dca`，本地隔离验证、整体 WIP）

- 网络恢复后核对原树原分支，HEAD 仍为 `57f465dca`，没有未提交遗留可丢弃。
- 原执行器引用经原轮审批进入真实隔离 ConversationStore、主 claim、子 attempt、notices 与用户决定入口；两身份各覆盖旧会话缓存、owner 长期授权、等待切 auto、auto 强制确认，handler 未执行且旧授权不写回。无人接收返回独立权限码。
- 有效红测发现子代理旧插件审批仍出现在恢复后的新 attempt；原 ToolApprovalScope 及原审批行补冻结 `execution_attempt_id`，展示、决定、等待共同逐字段核对，旧记录不跨轮。main claim 合同不变，不新建批准账或 schema。
- I4 走隔离真实 Gateway 请求处理和恢复入口：首次挂确认时注入宿主停机异常，重建宿主对象/死 claim 观察后原请求重排，再征询插件并等待新的决定。供应商与插件传输是假线路，不是实际 Gateway 重启、TUI/IM 验收。
- 联合十三相关文件与当前完整十守卫 489 项通过；两新增变异及静态收尾见 TESTS。第4六身份批准重跑仍未做，批准后仍可能重新 ask。3a 已把第5账本/final_status/B6 实读改派 ds2 独立树，本线不重复实现；B7 关闭门保持，不可上线。

## M1 B5 第3段执行端续接（2026-10-03，m1b5，接 `c70c895a6`，部署窗口WIP）

- 执行器生成稳定JSON引用，原审批ID算完后再附binding；前缀同源清洗放最前，只“仅本次/拒绝”。Gateway请求固定选项，五点注释“守门永远是第一句”。
- luna6拒绝码在本树唯一contracts/error_taxonomy.py修复：DENIED与APPROVAL_UNAVAILABLE均不可重试权限类；实际ToolExecutor不再UNKNOWN_ERROR。full带arguments_truncated，none不带；设计要求截断ask，B8/B9不改。200ms慢启动隔离用例验证原deadline收口timeout/ask。
- 执行器→原轮审批→Gateway隔离链接通，十一相关文件+完整十守卫首次468通过；后续变异/静态按TESTS实录，不等同真实消费者验收。
- **断点**：第3段真实主/子消费者引用集成与I4恢复未验；第4段六身份批准重跑未实施，当前批准后仍可能重新ask；第5段决定账本未做。B6固定be00891b8查询位置已定位，尚未写行/兼容实读。B7关闭门不改，不可上线。
- 3a外部七文件228通过，已确认invocation启用失败为环境原因，不再作为当前待处理项；下文保留旧发生时记录。

## M1 B5 第 3 段消费端守门（2026-10-03，m1b5，接 `6ec0fc7f3`，部分已实现、整体 WIP）

- 单一 `plugin_gate_required` 供 Gateway/主子后台两请求入口、两等待入口及自主提供者使用；先于会话缓存、owner 长期授权和自主轮询，不读写旧授权。
- 九条业务红测钉住已有缓存/长期批准和两个等待提供者仍会放行；已补消费端守门。等待参数收成 `ToolApprovalWaitOptions`，不引入任意关键字接口，旧默认仍读各入口原常量。
- **未完成**：执行端尚未附加 plugin_gate_ref，审批前缀与选项收窄、I4、不可交互错误码、精确批准重跑及 runtime_events 未做；消费端用隔离请求反证不等于完整执行链已接通，不能上线。
- 外部 guards9 曾新增四文件，本树缺 `test_gateway_client_credentials.py`，收集 exit=4、无测试执行；未改外部清单或其他分支。收尾读取清单已回到十文件且无缺失，当前完整复跑 172 passed；此前失败保留，聚焦回归与变异见 TESTS。

## M1 B5 第 2 段 ds10 初审修正（2026-10-03，m1b5，接 `3771f67ca`，组件已修、整体 WIP）

- 3a 采纳 ds10 三条：故障统一 `failure_review`，构造时保证非 ok 必须 ask、revoked 不参与合并；直接 GateReply 与解码同用 `clean_gate_message`，审批前缀后续也同源。
- 新增 24 条有效红测后修复，三文件 129 passed；不弱化断言，原启用准备 13 失败仍保留，原因未确认，交 3a 外部复核。
- 设计 8.5/13 点名 Gateway `permission_bridge.wait_for_gateway_permission_decision`，第 3 段必须和主/子请求、主/子等待及自主提供者共用守门。构造与清洗两条变异本段执行，等待绕过变异随第 3 段。
- 第 3–5 段尚未实现；B7 前 v8 关闭门不改，不能上线。当前阶段门禁和真实变异结果见 TESTS 顶部。

## M1 B5 第 2 段：共用池征询与超时边界（2026-10-03，m1b5，`worker/m1-b5-tool-gate`，本段组件已实现，整体 WIP，待交叉初审及 9b 终审）

- **来源/解决问题**：续接合同头 `b531c5fab`，将纯收紧合同挂在宿主 decide 后、审批前；用户宿主命令不被插件挡住，模型不能伪造来源或延长等待预算。
- **接线**：registry/自检默认 model，仅 management/invocation 两个宿主构造显式设 host_command。组合根注入 owner、配置和征询回调，权限视图共用；实际请求按 acquire→request 复用 Gateway 原 B2 池，不建立第二连接池。
- **期限/撤销**：匹配插件并行，同插件排队由 B2 单在途保证；排队、启动、回应和换代共用一次总预算。握手缺失、超时、错误、非法回复均 ask；只依据当前安装快照撤销旧代，通道关闭但激活仍有效不能误作停用而放行。
- **配置归属（3a 新裁定）**：`plugin_tool_gate_timeout_ms` 从 B7 移到 B5，默认 2000、范围 200–10000；YAML、dataclass、规范化及参数中心同时登记，并显式加入 `_BOUNDARY_NAMES`，模型 user_config 修改被拒，保留 PARAMETER_BOUNDARY 结构化事实。
- **证据/边界**：当前组件三文件 105 passed、完整 guards9 十文件 172 passed；其他回归和门禁见 TESTS。第 3–5 段的强制审批、精确批准重跑、拒绝模型回执和 runtime_events 未实施，不能上线；B7 前 v8 关闭门未改。复审按最新安排：非作者 my-agent 交叉初审 → 9b 安全终审 → 3a 沙箱外复跑。

## M1 B5 备审补充：审批双入口与可信来源（2026-10-03，m1b5，`worker/m1-b5-tool-gate`，补充合同已纳入，代码待第 2–4 段）

- **来源/解决问题**：3a 转述 ae 备审补充；只守 Gateway 会漏掉子代理/后台主代理的缓存、长期授权及自主轮询，错误的宿主构造点会使用户 `/plugins@` 被收紧。静态核对当前头 `4738fc659`，不是运行验收。
- **合同**：两条审批入口在缓存/长期授权前共用 `plugin_gate_required(request)`，等待自主提供者也共用；插件批准仅本次，不写会话或长期授权。`plugin_gate_ref` 用稳定 JSON 字符串，先按旧算法计算 `permission_id` 再附加引用；原 `grant_key` 哈希顺序不改。
- **来源与重跑**：`plugin_management._prepare` 和 `plugin_invocation._prepare_invocation` 显式设 `host_command`，registry 默认 `model`；host_command_execution 的 replace 只保留来源，不新增豁免权。另有 foundation 合同自检构造，保持默认 model。自主模式宿主 allow 仍须收紧成强制 ask；收紧门自行从原 `approved_actions` 核对已批准引用的完整身份，不能依赖宿主 `approval_applied`。
- **验收队列**：Gateway、子代理/后台主代理各覆盖旧同参缓存、长期授权、等待切自主与无授权写回；字符串往返/旧 ID 不变；auto 强制 ask 到精确批准重跑。详见 [M1 设计第 8/13 节](docs/design/PLUGIN_EVENT_HOOKS.md) 和 TESTS 顶部待实施矩阵。
- **边界**：本次只补设计、验证说明与原待办，未改产品/测试代码，不推进安全裁定；B5 仍仅第 1 段已实现，第 2–5 段未接线，B7 前 v8 拒绝门保持。

## M1 B5 第 1 段：收紧纯合同（2026-10-03，m1b5，`worker/m1-b5-tool-gate`，功能提交 `9cf60731d`，本段已实现，整体 WIP，待 be、ae 复审）

- **解决问题**：先固定只能比宿主更严的合并合同，避免后续执行/审批接线时将插件回复误当授权或参数修改。
- **实现范围**：`plugin_events/tool_gate.py` 的 `PluginToolGate` 仅做精确工具/效果匹配、规范请求投影、严格回复解析与稳定最严合并。原 `ToolCall` 仍是调用身份权威，激活对象只是只读投影，本段不读安装表、不使用连接、不执行、不批准、不写账。
- **投影**：完整参数先递归去掉全部 `__` 内部键，再复用统一脱敏；4000 字符按紧凑 JSON 总量计费，保持合法对象、保留能容纳的成员及字符串前缀，不新增伪参数。插件消息最多 80 字符，去控制符、双向控制字符及 Unicode 换行。回复额外字段全部忽略。
- **验证**：七种宿主/插件组合、所有返回排列的最严与主因稳定、撤销回答不算、参数不变/哈希同源、严格回复和投影边界已做组件测试；本段五个真实内存变异被抓到。命令、数字和门禁见 TESTS。
- **未完成**：第 2–5 段（B2 共用池征询、来源、总预算、执行器、审批防自动批准、精确重跑、拒绝回执、账本及错误码）尚未接线；不能将纯合同视作 B5 已端到端实施。安全裁定仅交 be、ae，不提前实施 B7 或宣称真实 v8 启用。

## M 线 B7 f841 阶段记录（b7f，2026-10-04；历史记录，不代表当前 17j 验收）

- **本轮尺寸与打包**：拆平五条既有新增告警，并拆分测试夹具的 otool 依赖解析；最终 `size_diff.sh` 实测新增告警 0、消失 11 条。随后 `check_clean_package.py .` 通过。
- **测试夹具与拒绝用例**：Python Seatbelt 用例按 `otool -L` 将当前安装中的依赖链接到临时 venv；本机夹具冒烟确实链接了 `libpython3.12.dylib`，但没有启动 Seatbelt。新增启用时隐藏根推导失败和 Linux argv 缺失隐藏根两个 fail-closed 用例，定向结果 2 passed。
- **文档与配置**：前端配置目录已同步并 `--check` 通过（258 fields），确认已无 `plugin_tool_gate_timeout_ms`；`plugin_sandbox.py` 与设计稿第 10 节说明均已同步 v8 整个家目录隐藏、插件/解释器白名单、macOS Gateway 端口拒绝和 Linux `network:true` 在 G5 前禁用。
- **本轮验证**：`test_plugin_manifest_v8.py` 与 `test_plugin_management.py` 全文件通过；`test_plugin_sandbox.py` 的 v8 强制沙箱/网络拒绝选择 3 passed，另外非真实沙箱纯选择 9 passed；`test_plugin_sandbox_v8.py` 排除真实 Seatbelt 场景后 10 passed、3 skipped；常数目录 11 passed；guards9 的 10 个测试文件退出码 0。import boundaries 0 findings、Ruff、doc sync、strict code-size（`strict_scope_total=2224, hard=0, blocked=False`）、`git diff --check` 均通过。严格尺寸报告已还原。
- **剩余本地失败**：`test_plugin_sandbox.py` 的 `test_client_wraps_launch_and_points_tmpdir_into_data_dir` 在 MCP 启动时报 `managed background launcher identity unavailable`；已确认是宿主启动指纹未取得，但底层原因未确认。`test_unavailable_sandbox_refuses_enable_and_explicit_calls` 的 relaxed 重启用断言也失败，未单独定位其底层原因；不据此声称沙箱拒绝或权限拦截。
- **尚未验证**：修改后的 Python 真 Seatbelt 用例需 3a 沙箱外复跑；Linux K8/完整 Linux lane 未由当前 macOS 环境验证。本轮未重跑 K4/K8/K9/K10 变异。此前 3a 对旧提交的 Node 真 Seatbelt 通过不能替代本轮验收。
- **状态**：本地尺寸及所列门禁已收口；保留 WIP，待 3a 复跑真 Seatbelt/Linux 项并完成独立复审。

## M 线 B7 安全底座移植 17j（b7r，2026-10-04，移植提交 `b6f6fc992`；已并入 step17j，9b 终审待做）

- **合并**：以 17j 基点 `2ad257314` 为目标，按 `git diff add244a92 f84182b5d | git apply -3` 移植；24 个文件进入本地 WIP 提交。五处冲突（台账、测试记录、确认文案、`plugin_sandbox.py`、设计表格）已保留目标与来源两边信息。前端配置目录通过同步脚本重生成并 `--check`，259 fields。
- **17j共享碰撞文件**：本线确实改了 `agent_py_agent/agent/plugin_management.py`（传入总开关、local/main owner 判定与 v8 沙箱事实）、`agent_py_agent/agent/settings/config.py`（新增默认关闭 `plugin_events_enabled`）、`agent_py_agent/agent/settings/parameter_registry.py`（该开关登记为模型不可改边界）、`agent_py_agent/config/agent_config.yaml`（中文配置说明）。B5/sol1 后续合入这四个文件时应按 B7 语义逐项整合；本线未代处理它们。
- **平台边界**：v8 `network:false` 断网；macOS `network:true` 由 G4 实际绑定端口表拒绝 Gateway 端口。Linux 继续以 `gateway_port_isolation_unavailable` 拒绝启用 `network:true`：G5 Landlock `CONNECT_TCP` 端口拒绝尚未接入 v8 插件沙箱，接入后需先在 Linux 车道确认目标端口拒绝、其它端口可达，再考虑放开。第 16 节已记录该原因和条件。
- **真 Seatbelt 端口用例**：移植保留 `test_macos_v8_narrows_read_and_enforces_network`，由真实子进程检查普通端口可达、G4 登记的 Gateway 端口被拒。本地执行时 `sandbox-exec` 返回 `sandbox_apply: Operation not permitted`，该隔离行为未验证，需 3a 在沙箱外复跑。
- **本轮测试**：B7 相关 13 文件定向命令退出码 1。`test_plugin_sandbox_v8.py` 的 Gateway 端口、Python 前缀、Node 真 Seatbelt 用例均因上述 `sandbox_apply` 错误未完成；Linux K8 测试在 Darwin 跳过。旧版启用/进程测试及一个 Shuohao 宿主启用测试也失败，不能据此称通过。常数目录、参数元数据/登记/边界四文件聚焦组退出 0；mutation 聚焦基线组退出 0（K8 单项 skip）。
- **基线对照**：在临时 detached worktree `2ad257314a784b4430695f4d7d108944ec1da78d` 对同一组 9 个旧 attempt/enable/legacy sandbox 测试选择器复跑，命令退出码 1。输出明确列出 `test_macos_view_write_allowed`（`sandbox_apply: Operation not permitted`）、旧 enable 用例（`outcome_unknown`）和清理记录用例（`StopIteration`）等失败；其中旧用例在给定移植基线上已失败。其它失败的底层原因未确认，不推断为沙箱或本轮代码问题。B7 新增的真 Seatbelt 与 network 策略仍需 b7r 版本外部验证。
- **门禁**：guards9 的 10 个文件退出 0、到达 100%；import boundaries `findings=0`；Ruff、doc sync 通过；strict code-size `strict_scope_total=2207, hard=0, blocked=False`（报告已还原）；clean-package 通过；`size_diff.sh` 原始结果为新增告警 0、消失告警 35。
- **变异与独立意见**：K4、K9、K10、家目录根收窄变异均由对应聚焦测试杀死，变异源文件按 SHA-256 复原；K8 是 Linux-only，本机跳过，需 3a 对 b7r 在 Linux lane 重跑。ds2 对 `f84182b5d` 的只读结论认为五处尺寸拆平行为等价，供 9b 抽查；这不是 b7r 行为验证。
- **外部旧提交证据**：3a 对 `f84182b5d` 报告 macOS 20 文件 469 passed、12 skipped、0 failed，以及 Linux 12 分片 0 失败、K8 未跳过；仅属于 f84182b5d，不替代 b7r。

## M 线 B7 插件安全底座原始实现（be，2026-10-03，分支 `claude/be-m1-b7`，基于 B1 `add244a92`，已实现，待 9b、ae 复审）

- **做什么**：把 B1 的两道过渡关闭门换成真判定，并给 v8 事件插件套上安全底座。
- **策略门**（`plugin_enable_tool.py`、`plugin_management.py`）：v8（清单 `permissions` 非空）启用要求 `plugin_events_enabled=True`，否则 `plugin_events_disabled`（可安装、不可启用，提示去 /settings）；且只有本机管理员（local/main，`is_complete_local_admin_owner`）能启用，否则 `plugin_events_owner_not_allowed`；v8 和旧版可执行插件一样构造运行计划，隔离在 PluginMCPClient 强制套上。替换了 B1 的构造期计划排除和执行期确认码前拒绝两道门，用例相应改写（见 TESTS）。
- **强制沙箱**（`plugin_enable_tool.process_sandbox = process_sandbox or is_v8`、`plugin_runtime.PluginMCPClient`）：v8 不管全局 `plugin_process_sandbox` 开没开都进插件进程沙箱；沙箱不可用则启用失败（`sandbox_unavailable`），不退回无沙箱。
- **断网 + 收窄读**（`plugin_sandbox.plugin_sandbox_spec`、`tooling/sandbox._read_only_root_argv`、`attempt/sandbox._linux_argv`）：`network:false` 断网（Linux `--unshare-net`、macOS `deny network*`），`network:true` 才放开；收窄读隐藏整个 my-agent 数据根（会话、记忆、G1 的 secrets 都读不到），只放行插件环境目录和数据目录。读基底设成插件环境本身（不是 owner home/数据根，否则 `_private_read_rules` 会把数据根重新放行、泄漏——实测过）；macOS 走 `private_read_roots` 拒读 + `_ancestor_metadata_rules` 让 realpath 成立，Linux 整根只读形态 tmpfs 盖数据根再把环境/数据目录挂回去。
- **两配置项**：`plugin_events_enabled`（默认 false）、`plugin_tool_gate_timeout_ms`（默认 2000，范围 200–10000，range 在 B5 消费处收口）；都进 `USER_SETTINGS_BOUNDARY_KEYS`，模型经 `user_config` 改被拒（PARAMETER_BOUNDARY），只有用户 /settings 能改。
- **已知边界**：Linux `--unshare-net` 在 Docker LinuxKit 车道建回环会失败（内核限制，真实主机不受影响），所以车道上断网只在 argv 上断言、不真连；收窄读在车道用 `network:true` 真进程验。证据 `~/.my-agent/decision-evidence/b7-prep-20261003/`（挂法实测）与 B7 真进程用例。
- **验收**：真进程两平台（macOS 真 Seatbelt、Linux 车道真 bwrap），布局为“放行目录嵌在拒读根里”，真实 Python 启动 + realpath + 读自己的包/数据、读不到会话/记忆/secrets。详见 TESTS 同名节。
## 老格式插件权限（opp，2026-10-03，分支 `worker/plugin-legacy-permissions`，基于 step17i `c47d023b6`；状态：方案待 3a 定）

- **本轮仅方案**：逐项盘点 `plugins/` 的 17 个样例、SDK 构建/入口模板及 v1–v7 能力，给出读/写/网络/host_api 的源码或清单依据、逐样例最小授权和兼容破坏点。没有改产品代码、配置或真实安装表，没有运行插件、连接 Gateway、做真实 TUI/飞书验收。
- **建议方向**：可执行 v1–v6 新启用默认复用 M/B7 的藏家读墙与进程沙箱，基础只放行自身包/数据/必要解释器依赖/系统资源；额外项目读写、运行依赖及网络由管理员预览并按确认码授权，不改旧清单字节。v7 内容和可选检查器仍走原独立合同，v8 不受旧默认开关影响。
- **兼容迁移**：推荐仅升级前已启用的固定激活保留原有效策略，列表显示“权限待收紧，重新启用时会按新规则确认”；新装、停用后启用、更新/重装、新项目或权限变更按新规则。不能把“缺新字段”当永久兼容资格，也不能直接翻旧 `plugin_process_sandbox` 开关。拟一个默认 true 的管理员边界项 `plugin_legacy_permissions_enforced`，模型不可改；本轮未新增配置。
- **待裁定风险**：回环监听/CDP/host_api 同样需要网络通路；网络布尔 true 是全部网络，不是“仅宿主 API”。浏览器/tesseract 的家目录依赖需有限额外读根，桌面 IPC/沙箱外默认应用不能靠读写联网三项保证。持久进程只能承诺固定授根的激活级 OS 隔离，不把逐次 `_meta` 当恶意代码隔离；旧兼容窗口与 Gateway 强制凭据链仍是明确边界。v7 可选检查器当前虽强制断网/临时写，读规格仍为整根只读，建议当次 target/关联输入读墙另切片，不能从“老格式权限”总目标漏掉。
- **验证与下一步**：本轮四个指定清单/摘要/读写上下文合同测试文件退出码 0；尺寸差集“新增告警 0、消失告警 16”（基点对线上清单，不是本轮消除告警）。新授权/迁移/OS 沙箱及真实渠道均未验证，待 3a 裁定兼容集合、网络宽度、外部依赖/桌面处理及配置语义后另派实现。逐项证据、渠道文案和后续必测见 [老格式插件权限方案](docs/design/PLUGIN_LEGACY_PERMISSIONS.md)。

## Gateway 本机来源信任收紧（be，2026-10-03，分支 `claude/be-gateway-local-trust`，基于 `claude/3a-step17h` `a602d6ad6`，9b、ae 评审通过，3a 2026-10-03 定稿，实施中，并入 step17i）

- **实施进度**：G4（macOS 端口拒绝）在 `claude/be-g4-port-deny`（基于 step17i `3a81e6c4b`，头 `c4113baa8`，ae 两轮复审通过，并入 step17i）实现——进程内绑定端口注册表（Gateway 启停登记/注销实际 `server_address` 端口）、`AttemptSandboxSpec.deny_gateway_ports` 只对模型命令沙箱置位、macOS Seatbelt `(deny network-outbound (remote tcp "*:<port>"))`、`full_access` 也拒、插件沙箱不填。真 Seatbelt 17 条用例 + 6 变异（见 TESTS 同名节）。ae 复审 G4（必改预登记 + 应改 isolation→/status，已补）。
  isolation 事实单一来源 `gateway_isolation_status`，`/status` 加 `gateway_isolation` 字段。
- **G5（Linux Landlock 启动器）**在 `claude/be-g5-landlock`（基于 G4 头 `c4113baa8`，加固 `0b10b074b`，ae 两轮复审通过，并入 step17i）实现：自包含启动器 `attempt/landlock_launcher.py`（exec bwrap 前声明 CONNECT_TCP ruleset、给被拒端口外的 65535 个端口加允许规则、`no_new_privs`、`restrict_self`，再 `os.execv(bwrap)`；系统调用号按架构查表、未知架构不支持；任何施加失败 fail-closed 非零退出不 exec）；就绪探针 `landlock_net_readiness`（架构、ABI≥4、bwrap 非 setuid 逐项探，非 Linux 返回 NOT_LINUX）；`_linux_argv` 就绪时用启动器包 bwrap argv、不就绪不包并如实报 unavailable；`gateway_isolation_status` 的 Linux 分支改为按就绪探测给 applied/unavailable:<原因>。单元用例（平台无关）+ Linux 车道真跑（被拒 EACCES、IPv4 映射也拒、其它端口通、穿透 bwrap 子进程、unavailable 分支）。G1/G2a/G6 由 my-agent 会话做。ae 复审 G5。
  - 已知边界（ae 复审）：施加失败的结构化标记写在 stderr，模型的命令能伪造；目前只用作这条工具结果的 error_code，没有宿主行为依赖它。以后要让它驱动宿主动作，得换成模型写不到的通道（例如 O_CLOEXEC 错误管道）。只有同步 shell 路径把 97 映射成 GATEWAY_ISOLATION_APPLY_FAILED，PTY 和后台命令显示的是普通的 96、97 退出码。

- **缺口**：Gateway 判“可信”只看对端是不是回环（`auth/middleware.py:66`），不带身份头的回环请求直接当本机管理员（`:82`）；模型命令沙箱默认联网、不区分回环（`attempt/sandbox.py:67`）。两者叠加，模型发本机 HTTP 请求就落在“本机管理员”一侧。文件通道（审批决定、请求队列）已被 H3 挡住，缺口全在 HTTP。
- **方向**（3a 定，两层都要）：(1) 模型命令沙箱按端口连不到 Gateway；(2) 特权入口只认宿主持有、模型读不到的凭据。
- **修复**：
  - 第 (2) 层（权威）：Gateway 持久化一份本机客户端凭据（`secrets.token_urlsafe(32)`，缺则生成、不随停机删，避免部署即失效）落 `secrets/` 0600；本机客户端经稳定函数 `load_local_client_credential(data_root)` 读取；可信=带这份凭据或原有 `gateway_auth_token`。**分两步上线**：第一版只发凭据+客户端附带（不强制），`/status` 按端点计“不带凭据回环请求”；计数归零、旧客户端重启后，第二版由开关 `gateway_require_local_credential`（默认 false，退场时删）打开强制，不带凭据的回环降匿名。网页端浏览器读不到文件，凭据由宿主服务端注入（留给接入时设计）；插件宿主 API 的 per-activation 令牌不受影响。`/status`、`/metrics` 公开白名单；无鉴权档、Full Access（模型能读数据根）写明为已知边界。
  - 第 (1) 层（纵深）：macOS Seatbelt 按端口拒绝连 Gateway（已实测：被拒端口 v4/v6 EPERM、其它端口与自测监听照常）；Linux 用 Landlock TCP 连接规则，先在车道实测，内核不支持时结构化报 unavailable、绝不退回放行。与凭据无关，可独立先上。
- **实施**：G1 凭据（缺则生成、持久、读取函数、进插件 H2 隐藏路径、不进日志/env/cmdline/status）→ G3 客户端附凭据 → G2a 第一版计数不强制（计数键用路由模板、只计要凭据/要管理员两档、插件令牌路由不计）→ G2b 开关强制（降匿名、去掉 peer_ip is None 旁路、`LOCAL_CREDENTIAL_REQUIRED` 提示重启）；G4 macOS（`*:<port>`、只置位模型命令沙箱 spec 字段不进共用 _network_rules）、G5 Linux（Landlock CONNECT_TCP 白名单、启动器 exec 前上、unavailable 分两种）、G6 宿主抓取工具对 Gateway 端口单独拒绝，三块独立先上。
- **评审并入**（2026-10-03）：ae 第 (1) 层 4 条（Seatbelt 用 `*:<port>` 挡 IPv4 映射、Landlock 实测挂法、unavailable 两种、覆盖范围与三类挡不住路径），9b 第 (2) 层 3 条必须改（插件令牌单列一档、“非唯一防线”三分法 + 文件队列第二入口 + 插件同信任域、计数/判据细节）+ 4 条建议（凭据同宿主身份不入日志env、web_fetch/watch_stream 排除 Gateway 端口、LOCAL_CREDENTIAL_REQUIRED、Landlock 车道实跑）。26 条路由档位清单见 ENDPOINT_TIERS.md。新头在复审后更新。
- **证据**：`~/.my-agent/decision-evidence/gateway-local-trust-20261003/`（Seatbelt 端口拒绝探针，随机端口假服务，没碰生产 8420）。详见 `docs/design/GATEWAY_LOCAL_TRUST.md`。

## 子代理缓存复审加固：粗 mtime 窗口与稳定派发断言（2026-10-03，luna6i，`worker/luna6-idem-flake` `44580b8a5`、`7c8a00bcc`，ae 复审通过、补强由 3a 复审，并入 step17i）

- **状态**：对 ae 针对 `9264b0bf5` 的建议已分两轮实现；本轮修正窗口语义、固定 ABA 探针并保留原提交历史，完成后交 ae 复看。
- **缓存边界**：签名一致且已缓存的旧条目可直接复用；缓存 miss 时若 mtime 年龄不足 2 秒，canonical 读取只返回给调用方并移除该 run 的旧条目，不写入缓存；只有 mtime 离开窗口后才存解析结果，避免窗口内读到的旧状态跨窗口成为 ABA 旧缓存。
- **派发回归**：两个幂等复用用例以回执桩固定状态；一个验证 3 个 `PLANNING` 全部派发，另一个验证 `DONE` 与 `RUNNING` 均排除、`PLANNING` 必须派发。
- **本轮补强**：模块级 `_now()` 只控制缓存时效判断，测试不再修改进程全局 `time.time`；正式 ABA 探针验证窗口内读后保存 `RUNNING`、伪造相同旧文件指纹并拨过窗口，最终仍读到 `RUNNING`。缓存验证夹具使用窗口外 mtime。
- **变异证据**：近期 mtime 回归在把保护阈值置 0（等效移除保护）时失败；将缓存签名退化为仅 mtime、并把文件设在 2 秒保护窗外时，`RUNNING` 回滚回归也失败；状态回归在把 `RUNNING` 加入 `DISPATCHABLE_STATES` 时失败。临时变异均已还原。

## 子代理任务缓存按文件代次失效（2026-10-03，luna6i，分支 `worker/luna6-idem-flake`，基于 `claude/3a-step17i` `b78b42265`，ae 复审通过，并入 step17i；两条应改另做）

- **问题**：`SubAgentPersistenceService` 原先仅用 `task.json.st_mtime_ns` 命中解析缓存。文件原子替换在时间戳精度较低的文件系统上可能沿用同一 mtime；items 幂等复用随后把缓存中的完整任务快照再次保存，普通 `RUNNING` 状态与旧 `PLANNING` 快照处于同一 runner attempt 时，不受终态保护或 attempt-generation fence 保护，可能把 canonical `RUNNING` 写回 `PLANNING` 并再次放入 auto_start。
- **修复**：缓存签名改为 `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`。原子替换的 inode/ctime 变化可在 mtime 不变时使缓存失效；线程锁、深拷贝、列表排序及公开派工合同不变。无新增配置。
- **验证**：受控固定 mtime 的高层回归先在旧写法下失败（`RUNNING` 被覆盖为 `PLANNING`），改后通过；DONE 排除测试改为集合/结构化身份判断，同时仍断言 DONE child 绝不进入 auto_start。定向测试、guards9 与静态门禁见 `TESTS.md` 顶部同名节。

## 新增模型目录非空时可手动补填（luna4m，2026-10-03，分支 `worker/luna4-manual-entry`，起因 9b 的 17h 冒烟第 5 项；ae 两轮复审通过，并入 step17i）

- **问题**：服务商目录返回非空时，只能勾选目录项；目录缺少用户实际需要的型号，就无法从「新增模型」添加。
- **做法**：共享 `choose_models` 接受默认关闭的异步手动表单回调。仅新建连接流程显式传入 `_manual(catalog, from_nonempty_catalog=True)`；非空列表末尾增加手动填写项，用户可只手填或和目录模型一起勾选，结果仍由原 `add_models` 一次保存。
- 目录为空或读取失败继续直接打开既有手动表单；订阅账号和已有连接的勾选流程不传回调，因此不显示手动项、不改变行为。
- ae 复审补：手动表单通过结构化 `from_nonempty_catalog` 区分入口；非空目录提示“列表里没有的模型，可以在这里手动填写”，空/失败仍显示原“没拿到模型列表”提示。取消手动表单只放弃补填，不丢弃此前勾选的目录模型。
- **验证**：假目录下真实 prompt_toolkit TUI 覆盖「非空列表手填 Embedding」「勾列表项并补手填」「空列表直接手填」；订阅和已有连接回归断言无手动入口。最终门禁与变异见 TESTS.md 本节。未调用真实服务或 Gateway。

## 能力包写后检查：回执列全错误、一次改完、连续失败到上限只记账（块 8 试点后，ae，2026-10-03，分支 `claude/ae-pack-feedback`，基于 `claude/3a-step17i` `911116770`，已实现，9b 复审通过（含两条必须改 96fd11f2f），并入 step17i）

- **起因**：块 8 试点 A05 同一交付物写后检查失败 20 次才过，单例用了 389 万 token（C13 同例 44 万–62 万）。查结构化事实（`decision-evidence/capability-packs-v2-b8/pilot-ede890374/A05-post-write-analysis.md`）是三件事叠在一起：
  - 回执只带前 3 条样例、不标截断；
  - 合同 v1 不带数字；
  - 模型每次只改一处。
- **3a 定的修法**（P1–P3，P4 第二步再定）：
  - P1：写工具回执带检查结果里保存的全部样例（最多 10 条），错误更多时 `error_samples_truncated=true`；返工提示每个目标也列到 10 条，并写明另有几条没列出。
  - P2：回执文字软提示“把这次列出的错误在下一次写入里一起改完，多处修改可以一次写整份”。只是提示，宿主不据此判断。
  - P3：同一个检查对象（包、检查程序、目标）写后连续失败 6 次（`pack_verification_service.MAX_POST_WRITE_CONSECUTIVE_FAILURES_COUNT`，模块常数，不做配置项）后：
    - 后面的写后检查不再跑，只记 `not_run/post_write_feedback_limit`，交给收尾检查那一次返工；
    - 中间通过一次就清零，收尾检查和别的目标的失败不算；
    - 暂停记录复用键留空，收尾会照常跑真实检查；没有正常收尾时，最终事实跳过暂停行，每个目标报最后一次真实检查（9b 复审补）。
    - 取消优先（和块 6a 合并时补，9b 提醒）：本 run 已取消时不记暂停行，交给 `_run_once` 记 `cancelled/verifier_cancelled`。
- **待定**：P4（合同允许错误带最多 4 个数字或短标识，A 包出 0.5.1）。等 P1–P3 上线后 A05、A10 重跑的结果出来再定。
- **之后**：上线后先重跑 A05、A10 各 1 次，报循环次数和 token，按新数据重估块 8 全量；必须在 4,000 万以内才跑。详见 [设计 3.1 节](docs/design/CAPABILITY_PACKS_V2.md) 和 TESTS.md。

## 设置改动撤销整批在途决策调用：结果码变确定（ds3f，2026-10-03，分支 `worker/ds3-flaky-observe`，基于 `claude/3a-step17h` `db963ea66`，ae 两轮复审通过（582750c18），并入 step17i）

- **起因**：Linux 12 片高负载车道偶发失败 `test_decision_observe_nonblocking.py::test_settings_change_revokes_queued_calls_without_sending`，实际 `[('stale','settings_changed'), ('stale','disabled')]`，期望两条都是 `settings_changed`。Mac 单跑连过 3 次。
- **不确定点有具体名**：`decision_policy.notify_decision_settings_changed` 原实现**逐条**处理在途请求——对每条 active 依次“推进快照 → 置 `settings_cancelled` → `handle.cancel()`”。两条请求之间的那个窗口里，第二条的后台发送线程可以取到它：`decision_service._NonblockingSend.__call__` 的 `_revoked()` 还看不到 `settings_cancelled`（返回 None），接着 `_invoke_call` 的 `_stale()` 只读文件层、看到点位已被改成 `off`，于是把结果码定成 `disabled`。
- **唯一顺序被冻结在两个来源里**：
  1. **执行器占着索引锁**——`notify_decision_settings_changed` 在 `_LOCK` 内先把整套受影响请求的快照与 `settings_cancelled` 一次标完，收集待取消句柄，然后才出锁；
  2. **调用已被 0.2 秒上限判失效**——发送线程若在这一整段之外才拿到请求，`_revoked()` 必然能读到 `settings_cancelled`，结果码只能是 `stale/settings_changed`。
  两处合起来让“设置改动撤销排队调用”对整批在途请求是原子的：谁先被处理不再由线程调度决定。
- **改动**（`agent_py_agent/agent/conversation/decision_policy.py`）：
  - `notify_decision_settings_changed` 走 `_mark_settings_batch`：锁内只做纯字典合并（`_pending_advances` 收集受影响请求与新旧快照、记下 `_action_generation` 通知代次）→ 锁外逐条比路由 → 再进锁复核代次后由 `_commit_batch` 一个锁段写完整批 → 锁外逐个 `handle.cancel()`；
  - 新增 `_pending_advances(targets, result)`：持锁纯字典合并，承接原 `_advance_notification` 的合并与逆序拒绝逻辑，不改任何请求状态；
  - 新增 `_commit_batch(batch, changed)`：持锁把整批快照推进与 `settings_cancelled` 一次写完并 `_action_generation += 1`；
  - 新增 `_routing_changed(active, previous, updated)`：纯比较，异常时仍按“已改变”fail-closed；**必须在锁外调用**；
  - 新增 `_MAX_MARK_ATTEMPTS_COUNT = 3`：比路由期间代次变了就整批重算；用尽上限按“变了”整批撤销（宁严勿松），不死循环；
  - 删除 `_advance_notification`。
- **ae 复看后修的 1 条（必须改）**：上一稿把路由比较留在了索引锁内，与函数注释和设计稿写的“锁内不读文件、不调路由”相反。`routing_signature` 一路走到 `decision_defaults`，每条受影响请求算两次，每次都新建 `AgentConfig`/`CapabilityConfig`/`MemorySettings`；上下文没有能力配置快照时还会读并解析 `capability_config.yaml`（生产 agent 刚启动时快照就是 None）。`_LOCK` 是全进程决策索引锁（登记/注销/撤销检查都要拿），设置改动那一刻会让所有在途决策线程等这些计算和文件读。现改为“锁内纯合并 → 锁外比路由 → 再进锁复核代次后一次写完整批”，整批原子性保持不变。
- **没动的边界**：结果码语义、`_ACTIVE` 索引结构、`InterruptHandle` 句柄合同、设置文件层、`cancel_active_decisions_for_shutdown` 的既有顺序，全部保持原样。这次只改“通知处理的原子性”，不改任何产品语义。
- **验证**：新增回归用例 `test_decision_settings_notifications.py::test_settings_change_marks_the_whole_batch_before_releasing_the_index_lock`（探针在第一条标记完成那一刻非阻塞试探索引锁，断言锁仍在调用方手里 + 两条都被标记）；变异验证：改回逐条写法该用例失败、还原即通过。目标用例在 8 路 CPU 加压下连跑 40 次全过。详见 TESTS.md 同名条目。

## 握手超时加只用于诊断的等待位置 timeout_wait_phase（ds2p，2026-10-03，分支 `worker/ds2-timeout-phase`，基于 `claude/3a-step17h` `db963ea66`，ds1 初审、luna5 返工（29f5d19c2），9b 终审通过，并入 step17i）

- **问题**（9b 复审修法 A 时的建议）：修法 A 把握手超时归成 `first_event` 才能被回合层退避重试，代价是账本里 `timeout_stage` 都记 `first_event`——be 这次诊断是靠这个字段才认出「是握手阶段超时」，之后统计就分不清「握手没连上」和「连上了但首个事件超时」。
- **修法**：超时异常与账本各多一列**只用于诊断**的 `timeout_wait_phase`；WebSocket 握手超时标 `handshake`，其余路径留空串（不猜“已连接”，空串=调用方没标注）。`backends/responses_websocket._handshake_error` 仍显式要求 `stage="first_event"`，只多传一个 `wait_phase`；`gateway_helpers._stream_timeout_error` 多收一个默认空串的参数。
- **不改的边界**：`TIMEOUT_STAGES` 封闭集合不动、回合层放行规则不动、修法 A 的 `first_event` 分类不动（新用例逐条钉住；stage 集合里仍然没有 `handshake`）。新增的 `TIMEOUT_WAIT_PHASES = {"", "handshake"}` 是**独立**封闭集合，异常构造（`ProviderTimeoutError`）与账本写入（`ModelCallLedger.timeout`）两侧都 fail-closed；诊断值绝不回写 `stage`，也不参与任何退避/放行判定。
- **命名**：决策观测投影里已有 `transport["timeout_phase"]` 表示 HTTP 传输阶段（`transport_timing.TRANSPORT_PHASES`），本字段因此取名 `timeout_wait_phase`，避免两个概念撞名。
- **落点**：异常 `ProviderTimeoutError.wait_phase` → 生成层 `_record_provider_timeout`（`getattr(exc, "wait_phase", "")`）→ `record_model_call_timeout` → 账本记录 `timeout_wait_phase` 与 `to_dict`；决策路径 `decision_model_call._record_error` 同步透传。续跑事实表 `_provider_timeout_resume_turns` 也顺手记一份位置，**不参与** `provider_timeout_resume_eligible` 的放行判定。
- **来源**：`timeout_wait_phase` 直读传参，不做任何推断；历史记录与未标注路径保持空串，读取端不得用空串回推“已连接”。
- **本轮 9b 初审修正**（luna5p，2026-10-03；状态仍为待 9b 复审）：修正决策统计测试的超时落账新签名，移除未调用 helper；让变异验证脚本使用当前解释器和系统临时目录，补入初审 V1–V4；增加决策错误落账的 `wait_phase` 回归用例，并同步测试计数与复跑记录。

## ChatGPT 订阅（Responses WebSocket）握手超时照 SSE 交回合层退避重试（wsto，2026-10-03，分支 `worker/ws-handshake-timeout`，基于 `claude/3a-step17h` `7e421024f`，已实现，9b 复审通过，并入 step17h）

- **问题**（be 只读核对生产，2026-10-03）：上一波 8 次整轮失败全是 `PROVIDERTIMEOUTERROR`，都发生在 chatgpt.com 的 Responses WebSocket **握手阶段**（7 次 TLS 握手超时 `_ssl.c: The handshake operation timed out`、1 次升级握手超时 `timed out while waiting for handshake response`），其中 6 次挤在 25 秒内，是一次网络抖动。这些回合已经跑了 13–33 轮工具、50–76 分钟，一次握手超时（生成层立刻重试一次也超时）就整轮失败。证据目录：`~/.my-agent/decision-evidence/model-timeout-ws-handshake-20261003/README.md`（只有结构化事实）。
- **根因**：SSE 路径把连接阶段的超时归成 `first_event`（`gateway_helpers` 的 `if _is_timeout_exception(exc): raise _stream_timeout_error(request, "first_event")`），所以回合层 `provider_transient_auto_resume` 会按 10/25/45/100/180 秒退避重试；WebSocket 路径少了这一步，握手超时走 `_runtime_network_error` 落成 `stage=provider_declared`，而这个 stage 恰恰是回合层明确不放行的（非流式请求里它包含“请求已发出后读超时”，重放会让服务商可能已处理的请求被重发）。
- **修法**（只动握手一段）：`backends/responses_websocket._handshake_error` 里，无状态码的失败先判超时——是超时就返回 `gateway_helpers._stream_timeout_error(request, "first_event")`，复用现成的 `_is_timeout_exception`，不另写判断。握手阶段连接还没打开、`response.create` 还没发出，所以归 first_event 不会重放任何已发出的采样请求（新用例用假连接钉住 send 次数）。
- **不改的边界**（守住现有合同）：
  - `gateway_helpers._is_transient_network_error`（所有 HTTP 请求共用）不动——超时仍然不是它眼里的“瞬时错误”，所以传输层自己的握手重试次数不变，重试交给回合层；
  - 回合层 `_raise_unless_provider_transient` 的放行集合不动（**不放行 provider_declared**）；
  - 连接打开、`response.create` 发出之后的超时分类不动（first_event / stream_idle 照原样）；
  - 握手的非超时失败分类不动（带状态码走 `_runtime_http_error`，连接被拒等走 `_runtime_network_error`）；
  - 握手中用户停止仍优先返回 `InterruptedError`。
- **验证**：新增 9 个用例（`tests/test_responses_websocket.py`），覆盖握手超时归 first_event + send 为 0、回合层退避后完成并带重试进度、生成层 `_retry_once_after_timeout` 返回 None、连接后静默超时 stage 不变、非超时分类不变、用户停止优先；相关 7 个测试文件 95 项全过；4 个变异（仍归 provider_declared / 归 stream_idle / 非超时也归 first_event / 去掉用户停止优先）全部被测出。详见 TESTS.md 同名条目。

## 能力包 v2 块 6a：`/stop` 打断宿主核验（2026-10-03，sol1 实现，ae 整合，分支 `claude/ae-b6a-17h`，基于 `claude/3a-step17h` `ede890374`，已实现，9b 复核通过（沙箱侧 + 补充用例 dbccc1d14），并入 step17i）

- **起因**：写后钩子和收尾的同步核验不看本 run 的取消信号，用户 `/stop` 后还要等所有检查程序跑完（最长各 120 秒）。
- **做法**：
  - 每次检查前看取消；已取消不再起新程序，剩余目标逐项记 `cancelled`（原因码 `verifier_cancelled`），取消结果不当缓存复用。
  - 正在跑的检查程序经新的 `attempt/process_run.py` 整组回收：TERM→宽限→KILL，宽限期内每 0.05 秒看组是否已退出，退了立即返回。
  - 被取消的回合，收尾的输入原件、交付物、检查结果三段都只记事实、不返工；最终事实和宿主提示写明“被取消”，只剩“被取消”这一样时也照发。
  - 组信号被拒、回收后管道不 EOF 两种情况沿用改造前的合同（不抛异常，补杀组长，超时仍返回 143）。
- **整合时补的**（3a 定）：取消时输入原件/交付物两段不记返工；事实为空的判断加上“被取消”；宿主提示的触发键加上 `cancelled`；9b 的宽限期轮询。每处一条用例、一个变异。
- **边界**：不改包声明、同意、沙箱权限，开关仍默认关。实际 TUI/IM `/stop`、生产和块 8 冻结重跑没验证。详见 [设计 3.2 节](docs/design/CAPABILITY_PACKS_V2.md) 和 TESTS.md。

## 没有入口回执的插话，收口时内容不能丢（3a 10-03 定，ds2steer 实现，分支 `worker/ds2-steer-noreceipt`，基于 `claude/3a-step17h` `a602d6ad6`，已实现；be 复审必改 1 已补（d6cf11ea8），be 复核通过，并入 step17i）

- **起因**（9b 复审 38 那批时发现的老问题，不是那批引入的）：38 的收口保证的是“有入口回执（`gateway_input_request_id`，例如 TUI 输入）的插话不丢”——收口拒收后入口对账会把它排成备用下一轮。没有入口回执的插话（`/steer` 控制命令且 scope 里没带入口请求号，IM 多是这种）收口时被拒收、也不起备用下一轮，内容就丢了；普通回合结束时同理。
- **两级处理**（3a 定的方向，只按结构化事实判断，不读正文）：
  - 拿得到可重放的结构化输入（回执/scope 里有会话、渠道、会话 ID、用户身份、插话去重键）→ 经**同一个**结构化入口排成“备用下一轮”，不另写一条路，内容只出现一次。
  - 拿不到可重放的输入 → 不排轮次，给该会话一条宿主提示：**“你刚才补充的话没有被处理，请重新发送。”**，带结构化原因码；TUI 灰行与 IM【提示】都看得到。
- **做法**：
  - 会话层（`conversation/store_guidance_recovery.py`）：`settle_unconsumed_receipt` 的兜底拒收分支（没有 `gateway_input_request_id` 的那条）在回执的 `migration.closeout_replay` 打 `pending`；`closeout_replay_receipts(turn_id)` 按这个标记列出本轮“内容还没着落”的插话，`mark_closeout_replay(dedupe_key, outcome)` 落终值（`replay_queued` / `replay_unavailable`），同一回合收口重跑不会重复排队或重复提示。只写回执级字段，`entry.metadata` 一个字不动（参与正文指纹）。
  - 入口层（`gateway_parts/input_delivery_service.py` 的 `settle_gateway_inputs_for_turn`）：入口对账跑完后，对这批回执逐条分流。可重放的用 `gateway_parts/steer_closeout_replay.py` 组装成一条 Gateway 排队请求：请求号由插话身份哈希推出（`steer-replay-<hash>`），经 `load_or_prepare_gateway_input_locked` + `queue_gateway_input_locked` 写回执、物化 inbox——和入口对账排备用下一轮是同一条路。重放请求不绑旧回合（去掉 `expected_turn_id`）。
  - 计数器：`settle_gateway_inputs_for_turn` 新增 `steer_replay_queued` / `steer_replay_unavailable`；续跑上限收口把它们一起写进答复的 `guidance_settlement`。
  - 判定只看结构化字段：回执级 `closeout_replay=pending`、`entry.metadata` 里的会话身份、以及回执自己的去重键；缺会话（无处提示）、缺去重键（排不出幂等请求）都算重放不了。不解析正文，不为单个渠道写专项分支。
  - 覆盖两种收口：续跑上限收口（`settle_dead_turn`）与普通回合结束（`reject_pending(reject_reserved=True)`）都经同一个 `settle_gateway_inputs_for_turn`，所以两级分流对两条路同时生效。
- **顺带修一个渲染缺陷**（本批新用例暴露）：`host_notices_from` 只认字典，不认 `HostNotice`；而 `take_host_notices` 返回的正是 `HostNotice`。于是“取走提示再渲染”（IM 与 TUI 的真实出口都是这样取的）会把提示整条丢掉，用户看不到、也不报错。已改成两种都认。
- **本轮补修（be 复审 2026-10-03，必改 1）——崩溃窗口里会双份**：`queue_steer_replay` 原来返回 enqueue 的 `created`（“这次有没有新建入口项”）。在“enqueue 已经成功、`mark_closeout_replay` 还没落盘就非计划重启（I4 续跑）”的窗口里，重跑收口时插话回执仍是 `pending`，会再处理一遍：重放本身幂等、队列里仍只有一条，但第二遍把这条**已排队**的插话误判成排不上，记 `steer_replay_unavailable=1` 并给用户发“请重新发送”——用户一重发就是双份。
  - 改法：按**回执终态**判断，只读结构化的 `state`。`load_or_prepare` 返回的 `updated` 回执里 `state == "queued"` 才算“内容已在队列里”，成功；`state == "consumed"` 时 `queue_gateway_input_locked` 根本不排队、原样返回，正好被排除。
  - 由此明确 `steer_replay_queued` 的语义是**“内容现在已在队列里”**（不再等于“这次新排了几条”），同一请求第二次命中记 1；`unavailable` 与重发提示只在真的排不上时出现。
  - 用例：幂等用例补 be 探针的两条断言（重跑后 `unavailable == 0`、无重发提示）；新增 consumed 状态算“不在队列”的用例。
- **验证**：见 TESTS.md 同名节（上一轮 6 个变异 + 本轮 3 个，全部被拦截）。

## 模型回合瞬时错误重试加总时长上限（修法 B，ds1b，2026-10-03，分支 `worker/ds1-turn-retry-cap`，提交 `65464a201`、返工 `f3ad77e4e`，基于 `claude/3a-step17h` `880aee17b`，已实现，9b 终审通过（两条补用例由 3a 合并时加），并入 step17i）

- **起因**（be 调查结论）：现在有三层重试——传输层（HTTP 408/429/5xx，等 2/5/15 秒最多 3 次）、生成层（超时后立刻再试一次）、回合层（`provider_transient_auto_resume` 按 10/25/45/100/180 秒退避、最多 5 次，界面显示 1/5…5/5，只放行 `first_event` 与 `stream_idle` 两种超时）。回合层没有总时长上限：最坏是 6 次 × 300 秒再加等待，约 36–39 分钟。
- **做了什么**：新增运行护栏配置项 `provider_transient_auto_resume_total_budget_seconds`（默认 1800 秒，`0` 表示不限）。计时从**这一次逻辑模型调用的第一次尝试**算起，尝试本身耗时、退避等待与它的抖动都算进去；再退避一次就会超过上限时不再重试，抛 `ProviderTransientError` 并带结构化码 `PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED`（原异常留在 `__cause__`），文案写明"已到总时长上限"。
- **只读结构化事实**：预算判断只看时刻（`time.monotonic`）与尝试次数，不读任何报错文字；上层按 `error_code` 识别。
- **不碰的边界**：`provider_declared` 的放行规则不变（那是修法 A 的事），`responses_websocket` 一行未改；`/stop` 照旧最优先（中断检查在预算判断之前，退避等待里的中断也直接上抛）。
- **配置落点**：加在 `runtime_guard_config.yaml`（该文件是运行护栏的权威来源，进程重启生效，用户不可通过聊天写入），**不进** `agent_config.yaml`/`AgentConfig`；参数中心登记表按同一 YAML 自动收录（该来源键数 22 → 23），常数目录重新生成（883 → 884 项）。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「模型回合重试总时长上限」小节；6 个定点变异全部被检出。
- **返工（第二轮，ds1b，2026-10-03，提交 `f3ad77e4e`；依据 9b 真实链路预审与初审）**：
  - **到上限不再换异常类型**（9b 必须改 2）：改为给触发判断的**原异常**就地补 `error_code` 与 `retry_budget_seconds` 后原样重新抛出（上面"抛 `ProviderTransientError`、原异常留在 `__cause__`"的旧写法作废）。
    背景：修法 B 主要针对 `ProviderTimeoutError`（first_event/stream_idle），它本来不是 transient；换类型会让后台 claim 被释放、唤醒后重跑，子代理失败类型从 `PROVIDER_TIMEOUT` 变 `TRANSIENT_ERROR`，Goal 的 `usage_limited` 判定也会失真。改后这些分路与"跑满阶梯"完全同口径。
  - **到上限发可见收口提示**（9b 必须改 1 + 初审）：经 `_emit_retry_notice`（final）发一条"已到自动重试的总时长上限、本轮不再重试"；用户可见文案在 `gateway_parts/request_errors.gateway_client_error_message` 与 `agent/runtime_errors._provider_supply_template` 按结构化码登记（不再说"系统会自动退避重试"），`contracts/error_taxonomy` 登记同一码。
  - **配置解析口径对齐**（9b 建议）：坏值、NaN、负数都回落默认 1800 秒，只有显式 0 表示不限（原实现负数按不限、与注释不符）。
  - **中断检查**：补用例钉住"退避等待正常返回后中断必须阻止下一次尝试"；复跑发现单删"等待后检查"或"循环后检查"都是等价变异（循环开头/等待后/循环后三处互为兜底），用"三处全删"的组合实验证明用例会红。
  - **已知边界**（3a 裁定）：子代理重派上限（`provider_transient_redispatch_limit`，默认 8）不因本上限收缩，最坏耗时≈重派上限×总时长上限，另立一项；次数耗尽时 `runtime_errors` 的"会自动退避重试"文案同样不准，属原有行为不在本次范围。
  - **验证与变异**：见 TESTS.md「修法 B 返工」；10 个定点变异 8 个被检出，2 个等价变异附组合实验证据。

## 嵌入用量与召回方式看得到（S7，be 实现、75 接手收尾，2026-10-02，分支 `claude/75-embedding-usage-facts`（接 `claude/be-embedding-usage-facts`），基于 `claude/3a-step17h` `afb15947b`，已实现，9b 复审通过，并入 step17h）

- **起因**（3a 查生产）：
  - 写入时嵌入在工作：embo-01、1536 维，向量 24 → 25。
  - 两个嵌入客户端直接发 HTTP，没有任何计数，嵌入调用也不进 model call ledger。
  - 召回方式只在 `memory_search` 的工具正文里，按规矩不读。
  - 所以 goal S6 要核对的两件事（召回真的走 semantic、嵌入次数和 token 与预估相符）核对不了，用户问的“每天要跑多少”也答不出真实数字。
- **做法**（3a 定口径）：
  - 每个进程一份内存计数 `retrieval/embedding_usage.EMBEDDING_USAGE`，线程安全，不写盘，不加开关（只是展示，不改请求）。
  - 嵌入按用途计：记忆写入、召回、重建、工具检索；没标注的归 other，不猜。每种用途记请求次数、文本条数、失败次数、供应商回报的 token，以及没回报 token 的请求数（不估算）。
  - 计数只有一处：两个嵌入客户端的 `embed` 都经 `count_embedding_request`，它们的 `_request` 返回供应商回报的 token（OpenAI 兼容取 `usage.total_tokens`，没有时取 `prompt_tokens`；MiniMax 取顶层 `total_tokens`）。
  - 用途在操作入口用 `counted_as` 标注：JsonlMemory 的写入（`_index_vector`）、召回（`_search_scoped`、`_semantic_records_report`）、重建（`_embed_rebuild_rows`），以及工具语义检索（`VectorToolSearchProvider._semantic_search`）。
  - 召回方式计数 semantic/keyword/none，留最近一次的 mode 和 fallback_reason，每次检索记一次：scoped 检索（自动召回、memory_search、决策补充召回）由 `_scoped_retrieval_facts` 记；不带作用域的 `search()`（Gateway 与 IM 的 `/memory`、`agent.recall`）由 `search` / `_fuse_semantic` 按同一口径记（嵌入失败 `embedding_failed`，没有嵌入端 keyword 加不可用原因）。原先写的“`search()` 只计嵌入、不计方式”在 embo-01 真实对账里表现为“召回请求 2 次、召回方式 0 次”，用户看到的两行对不上，be 2026-10-03 改为也计（待 9b、3a 确认）。
  - 展示：管理员发 `/model vector`（TUI 与 IM 同一个 Gateway 入口）时多几行“本次 Gateway 启动以来”，只有数字和原因码。
    - 计数是全进程的，含其他用户，所以普通用户看不到这几行。
    - `my-agent memory vectors status` 是另起的进程，计数为空，那里不加。
- **顺带修日志刷屏**：`core._build_memory_embedder` 每物化一个 owner 的 agent 就打一行“语义记忆档案不可用（profile_not_found）；当前使用关键词召回”，17f 的 start.log 里有三百多行，都是其它 owner 按设计（S4）退回关键词。改成同一进程里同一（owner, 原因）只打一次（`core._first_profile_warning`），结构化诊断 status 照旧每次写。
- **为什么不进 model_usage / model call ledger**：那里有准入、停机关门、预算、上下文校准等语义，是给对话模型调用的；嵌入不经过这些门，硬塞进去会让关门、预算判断把嵌入也算进去，或者反过来被嵌入的记录污染校准。嵌入只需要“次数和 token 是多少”，所以单独一份只读计数。
- **测试与证据**：见 TESTS 同名条目；真实核对（隔离 home、embo-01）在 `~/.my-agent/decision-evidence/embedding-usage-facts-s7/`。
- **9b 两条后续（2026-10-03，状态：已做；分支 `worker/luna1-s7-followups`，提交 `c8da199c8b35e9d7162797c3bf6da567b68c9960`）**：
  - 空库时不带作用域的 `search` 也记召回方式 `none` 并提前返回，不发送查询嵌入；判定看 active JSONL 是否有记忆，不按关键词候选为空误判，保留非空库的语义检索机会。
  - MiniMax 请求类型按结构化输入角色区分：查询用 `type=query`，持久文档写入和重建用 `type=db`；HybridRetriever 与工具检索分别把 query 和候选文档标清。OpenAI 兼容请求体保持不变。
  - 身份复核：`embedding_identity` 仍只取 profile_id、protocol、endpoint_digest、model_name，不含请求类型；本机桩服务下，切换类型前后身份相同，旧身份仍可打开并检索已有向量，不触发身份失配或重建。
  - 定向回归、3 项变异和门禁见 TESTS.md 同名后续记录；没有对 Gateway、真实用户或外部 MiniMax 服务做本轮验收。

## 决策结果日志至少保留一周（luna6，2026-10-03，3a 复审通过，并入 step17h）

- **解决问题**：原决策结果账只有 1000 行上限，生产增长约每 3.7 天就会覆盖一周观察统计所需的前半段结果。
- **保留规则**：写入时按 `created_at` 裁掉早于最近 7 天的行；与 `decision_reach_counts._RETAIN_SECONDS` 使用同一时长口径。
  7 天内的行优先保留；仅当超过硬上限 20000 行时才按最旧 `created_at` 丢弃。
- **写入合同**：仅 `conversation/decision_outcome_log` 使用专用留存写法；在同一 per-path 锁内读改写并原子替换。
  公共 `append_jsonl_capped` 的“最近 N 条”行为不变。失败继续只写日志、不影响决策；不新增配置。
- **读取合同**：汇总字段与时间窗不变，`recent` 仍最多展示 20 行；到达计数本身已保留 7 天，本次不改。
- **验证**：定向用例覆盖超过 1000 行保留、7 天外清除、硬上限最旧裁剪与写盘失败隔离；命令与变异证据见 [TESTS](TESTS.md) 同名节。
- **后续**：3a 复审后集成；本线不启 Gateway 或做真实 owner/Gateway 验收。

## TUI 新增模型直接选择 Embedding 用途（luna4，2026-10-03，3a 复审通过，并入 step17h；真实 TUI 核对随发布冒烟）

- OpenAI Chat 新增表单增加用途选择：「对话（默认）」保持原 agentic 保存请求；「Embedding」把结构化用途传给原子 `add_models` 操作。
- `execute_model_profile_operation` 仍是唯一目录写入口；同一锁内将 embedding 同时写入模型档案和服务商能力，复用服务商时合并能力，不另建 TUI 写路径。
- Embedding 保存后提示用户到「选择模型」→「向量模型」选用，并在重启 Gateway 后生效。未运行真实 TUI/Gateway、未连真实供应商；保存通过只证明本地组件链路，不证明端点可用或重启后实际采用。
- 用例、三项以上变异和门禁结果以 TESTS.md 本节为准。下一步 3a 独立审 diff 并在集成版本真实复核菜单、用途选择、向量模型列表和重启后的运行状态。

## 记忆归档目录收紧权限：memory_archive 写入私有化 + owner 维护收紧已有文件（luna3，2026-10-03，分支 `worker/luna3-archive-private`，基于 `claude/3a-step17h` `b453f8883`，已实现，3a 复审、9b 补审通过，并入 step17h）

- **背景**：S2 把候选、日事件、lesson/HOT、Curator 事务目标与前镜像改成私有原子写，迁移备份也已私有化；S2 台账里记为"单独立项"的 `memory_archive/`（任务/运行事实索引、Compact 快照、任务工作区、外置工具输出）这批会话与任务数据仍是目录 0755、文件 0644。
- **做法 1（新写入）**：复用 S3 的 `common/json_io.write_private_text_file_atomic_unlocked` 底层，同一文件补薄封装（不另写一套）：自取锁的 `write_private_text_file_atomic`、`write_private_json_file_atomic`，持锁的 `write_private_json_file_atomic_unlocked`，整文件 `write_private_jsonl_records`，追加 `append_private_text` / `append_private_jsonl_records`（新文件出生 0600、已有文件先收紧再追加）。
  - `memory_archive/` 模块 14 个文件的写入点全部改走私有原子写：`agent_run_workspace`（state/checkpoint/agent.yaml/task.md/summary/timeline/events/artifacts）、`shared_workspace`（blackboard/messages/evidence/index）、`task_workspace`（state/rendering/payloads）、`storage`（hooks 快照与 audit 事件）、`runtime_fact_source`（runtime_facts/<id>/task.json）、`tokens`（会话 token 账）、`compact_circuit_breaker`、`compact_apply`（context/handoff/self-check/ledger）、`tool_output_externalizer`（正文与 index.jsonl）、`artifact/registry`、`daily_ledger`。
  - 新建的任务工作区与运行时事实目录按 0700；生产里已有的 0644/0755 文件与目录靠"下次写入收紧"覆盖不到的部分见做法 2。
- **做法 2（已有文件）**：`memory_archive/storage.tighten_memory_archive_permissions` 递归收紧 owner `memory_archive/` 下已有文件到 0600、目录到 0700：只收紧不放松（不比目标宽就不动）、不跟随符号链接（跳过并计数）、chmod 失败按原因码计数继续；返回结构化事实（`tightened_count`/`tightened_files`/`tightened_directories`、`failed_count`、`failure_codes`、`symlink_skipped_count`）。
  - **挂到现有维护入口**（不另起维护框架）：`user_space/owner_maintenance.run_owner_retention_if_due` 新增一步，回执写 `O/data/maintenance.json` 的 `memory_archive_permissions` 键；Gateway 的 owner-maintenance 循环默认每天跑一次，与正文哈希缓存回收、global_index 压缩同一挂法。
  - 只改权限不改内容；memory_archive 不存在时返回全零（还没建归档属正常）。
- **验证**：见 TESTS.md 同名节。

## M1 B1：清单 v8、订阅确认与静态构建（2026-10-03，m1b1，`worker/m1-b1`，原功能提交 `20a930cd2`，两条裁定（`ad6007106`）与 ae 意见（`d6e9daccb`）已实施，ae 复审通过，并入 step17i）

- **解决问题**：让事件类型、正文范围、工具收紧范围和网络需求成为可校验的不可变声明，并进入原启用确认码；不另建授权或执行链。
- **已落地**：`plugin_events/declarations.py` 严格校验三项新字段；v8 只走 v6 文件入口，旧 v1–v7 不加空字段且固定字节回归；v8 与 `host_api` 冲突保留 `events_with_host_api_unsupported`。构建器按显式订阅/权限键生成并读回 v8，非法包不发布。
- **确认**：原 `confirmation_details` 加 `events`、`tool_gates`、`network`、`sandbox=required`；相同包摘要下订阅/正文/参数范围/网络变化仍改变确认码。安全文字是 B7 的准入要求，B7 就绪前不向 v8 索取确认码、不运行包。
- **原交付证据**：v8 合同 106 项通过，guards9 十文件 172 项通过，四个变异全被抓到；沙箱内五文件历史为 262 通过、6 失败。3a 转述 ae 沙箱外复核：基线 `274cedb1e` 与 B1 头 `bf520e911` 的 `test_plugin_any_language.py` 都是 **35 passed**，六失败来自 my-agent 命令沙箱环境，本线未亲自沙箱外复验。历史与本轮命令详见 [TESTS](TESTS.md)。
- **本轮裁定证据**：先红测 8 失败/107 通过，实现后合同 115 通过；五文件 271 通过/原六失败；guards9 172 通过；新增两变异与原四变异都抓到，尺寸新增告警 0。纯事件/纯收紧无工具面板包经真实 CLI 构建、读包及原确认预览断言通过，未启用插件。
- **3a 2026-10-03 裁定**：观察 text 只允许提示提交，工具开始只能 none；工具参数只走精确工具的 full 收紧门，不增 `events[].tools`（第二期再议）。v8 非空事件或收紧门算贡献，可不带工具/面板；v1–v7 原规则不改、空 v8 订阅仍拒绝。本轮在原分支追加提交，不 amend。
- **ae 复审修订**：三处数量常数补中文用途/上限理由，沿原生成器重建只读目录；`PluginEnableTool` 在确认前对 `permissions` 非空包返回 `plugin_events_disabled`、`not_started/not_committed`，构造不生成 v8 环境计划；安装仍允许。探针不固化本机临时目录，分支增量的文档命令也改用现场变量。
- **本轮验证**：当前合同 125 通过、常数目录测试 11 通过且 `--check` 877 项一致、guards9 172 通过；五文件 281 通过/原六失败。拒绝门变异抓到 9 失败，常数排除/单位/说明三变异抓到 3/4/3 失败，随后正常新进程通过。门禁与尺寸新增告警 0，命令及环境证据来源见 TESTS。
- **边界**：D9 的 local/main 启用门、真正总开关和强制沙箱仍属 B7；B7 落地时须一起替换暂时拒绝与构造期计划排除，接入真实准入判定，不能只删关闭门。`sandbox=required` 不是隔离已生效的证据。
- **下一步**：ae 对原提交、两裁定和本轮修订一并复审，3a 挑选集成；B2 可并行，B3/B7/B9 按设计依赖接入。v8 在 B7 前只能安装、不能启用，整期 M1 不因此记完成。

## B2 五轮返工：只补用例——4 个窗口钉死 + 2 条防线用例（M 线第一期，ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，提交 `24cf10edb` 之后，基于 `8b6344045`，已实施，be、ae、9b 复审通过，并入 step17i）

- **起因**：9b 复核第 4 轮（`24cf10edb`）判定产品代码全部改对（停机终态、先取时间界再读表、真子进程压力 627 个客户端关闭后 0 残留、错误全为撤销），只差 4 条用例没测到各自的窗口。本轮**只改测试文件、不动产品代码**。
- **4 条用例分别钉住**：
  1. MU4：`before_send` 之前连接被摘除的窗口（包 `_ensure_started`，返回传输前摘除；断言撤销且 `before_send` 未被调用）；
  2. MU7：`retire_stale` 的扫描必须在池锁内（dict 子类在每次遍历时断言池锁持有）；
  3. N1：时间界必须在读安装表**之前**取（走真实 `panels()` 入口，读表期间别的查询为新启用激活建的连接必须被保留）；
  4. N3：回收必须看"自己管"（面板服务没有槽的别人连接，即使过期也不碰）。
- **附带补的 2 条**：第 4 轮引入 `RetireScope.managed` 后，ae-N1（有效集合只算有面板的）与 ae-N2（读表失败当空集合）两个老变异变成等价存活（scope 第二道防线兜住，行为仍正确，但 valid 计算这层失去保护）。补两条用例把这一层重新钉住：读表失败时自己管过的连接也不能摘；收紧成无面板但仍启用的激活必须留在有效集合里。补完后两个变异重新被杀死。
- **验证与变异**：相关 4 个文件 73 passed；26 个变异（老 23 个，其中 P4 即 9b 的 MU4；再加 9b 的 MU7/N1/N3）全部被杀死；命令与结果见 [TESTS](TESTS.md) 顶部「B2 五轮返工」小节。

## B2 四轮返工：停机终态、回收的过期证据、在途摘除按撤销（M 线第一期，ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，提交 `5aec88524` 之后，基于 `8b6344045`，已实施，be、ae、9b 复审通过，并入 step17i）

- **起因**：9b 并发复审（`~/.my-agent/decision-evidence/review-b2-concurrency-5aec88524/`）判定锁序无环、649 个真子进程压力下关闭后 0 个活进程，但 B3/B5 接上同一个池后会出现 3 条真问题。
- **根因与修法（每条）**：
  1. **停机后还能再建出一个没人关的池**。根因：`server_close()` 不等还在跑的工作线程，`stop()` 又把 `plugin_display`/`plugin_channel_pool` 置空且没拿 `_SERVICE_LOCK`；晚到的面板请求或事件中心取池于是重新建一个新池，它拉起的插件进程之后再也没人收。修法：新增 `close_plugin_channel(server)`，在 `_SERVICE_LOCK` 内先置关闭标记、再摘掉服务与池引用，锁外真正 close（close 会 stop 插件进程，可能阻塞，不能占锁）；`plugin_display_service` 与 `_locked_shared_pool` 见到标记抛 `PluginChannelRevoked`；面板入口转成"通道已关闭"的空面板响应；`GatewayHTTPServer.stop()` 调用它（幂等）。
  2. **回收缺少正面过期证据**。根因：面板服务拿 `owner_keys` 快照算 keep，再交给 `retire_stale` 在池锁内重扫；快照之后别的调用方为一个刚启用的激活建的连接既不在快照里、也不在本轮 valid 里，于是被误摘（B3 丢一次事件，B5 多弹一次审批框）。修法：池加单调递增的创建序号（`created_seq` / `current_seq()`）与 `RetireScope(managed, created_before)`；`retire_stale` 只在"归自己管 **且** 建在时间界之前 **且** 不在 valid 里"时摘；服务在读有效集合之前取界。用序号而不是墙钟，只表达结构化的先后。
  3. **在途请求撞上停用会报"连接断开"并标退避**。根因：摘除时 stop 客户端，进行中的 `transport.request` 先抛 `MCP_CONNECTION_CLOSED`，`request()` 按连接级故障标退避并原样抛给调用方。对 B5 意味着"插件已停用"被当成"连接坏了"，按需要确认处理。修法：`request()` 的通用 except 先 `holds()`；连接已不在表里就抛 `PluginChannelRevoked`（`from exc`）且不标退避。
  4. **两份合同注释补齐**：客户端 `stop` 必须幂等（停用撞后台启动会 stop 两次，第二次才兜住漏关）；遍历连接表一律走 `pool.owner_keys` 快照。
- **不改的边界**：连接身份、退避分级（连接级按连接、请求级按面板）、池的唯一所有权、沙箱开关取值都不变；`retire_stale` 不带 scope 时保持旧语义（供非共享场景与既有测试使用）。
- **验证与变异**：9b 的 3 条探针转正为仓库用例（第 4 条真子进程压力测试在沙箱外由 9b 跑）；23 个变异（be 10 + 前几轮 8 + 本轮 5）全部被杀死，其中 MU4（`_current_client` 不核对连接在表里）与 MU7（`retire_stale` 扫描移出池锁）正是 9b 报的存活项；命令与结果见 [TESTS](TESTS.md) 顶部「B2 四轮返工」小节。

## B2 三轮返工：错误码用例对齐与池连接快照（M 线第一期，ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，提交 `b4e5a1e12` 之后，基于 `8b6344045`，已实施，be、ae、9b 复审通过，并入 step17i）

- **起因**：ae 复看 `b4e5a1e12` 认定上一轮三条都改到了，但新引入 2 条。
- **根因与修法（每条）**：
  1. **改了错误码但漏改一个用例**。根因：上一轮把远端 JSON-RPC 错误回复从 `MCP_PROTOCOL_ERROR` 改成独立的 `MCP_REMOTE_ERROR`，`test_mcp_client.py::test_call_unknown_tool_surfaces_protocol_error`（标了 integration）不在当时跑的 4 个文件里，仍按旧码断言。修法：期望改成 `MCP_REMOTE_ERROR`，用例改名 `..._surfaces_remote_error`；该文件用默认参数与 `-o addopts=` 两种方式各跑一遍，确认 integration 用例真的执行到。
  2. **`_retire` 在池锁外遍历池的活字典**。根因：算保留集合时写 `{key[1] for key in self._pool.connections ...}`、末尾又写 `for key in list(self._pool.connections)`，两处都没拿池锁；集合推导在字节码层逐项迭代，期间后台渲染遇到撤销会 `_revoke`、以后 B3 也会建/删连接，撞上就抛 `RuntimeError: dictionary changed size during iteration`，面板请求直接失败。修法：池新增 `owner_keys(owner_key)`，在池锁内返回该 owner 连接键的不可变元组快照；`_retire` 两处遍历都改用它（回收后再取一次新快照）。
- **不改的边界**：回收语义与上一轮一致（保留集合 = 启用快照 ∪ 不在本服务槽表里的连接；`None` 时完全不回收）；退避分级、连接身份、池所有权都不变。
- **验证与变异**：18 个变异（be 的 10 + 本轮累计 8）全部被杀死，其中 N7「`owner_keys` 交出活字典」、N8「服务改回直接遍历池活字典」是本轮新增；命令与结果见 [TESTS](TESTS.md) 顶部「B2 三轮返工」小节。

## B2 二轮返工：共用池的召回范围与退避分级（M 线第一期，ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，提交 `fcb638182` 之后，基于 `8b6344045`，已实施，be、ae、9b 复审通过，并入 step17i）

- **起因**：ae 复审 B2 返工（`fcb638182`）判定 be 的 6 条都修到了，但共用池真正要被第二个调用方（B3 事件、B5 收紧钩子）用起来还差两处，另有三条小修。证据目录 `~/.my-agent/decision-evidence/m1-b2-rereview-20261003/`：两条共用池探针在头上都失败。
- **根因与修法（每条）**：
  1. **面板服务会摘掉共用池里别的调用方的连接**。根因：`panels` 算"还有效的激活"时只收有面板的插件，再对**共用池**调 `retire_stale`——同一 owner 下已启用但没有面板的插件（只带事件的 v8 插件）每次有人看面板就被摘掉、进程被停；安装表读一次失败（返回空）更会把整个 owner 的连接全摘掉。修法：交给池的**保留集合**改成"这个 owner 全部已启用的激活 ∪ 池里不归面板服务管的连接（别人建的）"，所以面板查询只回收自己管过、且已失效的激活；安装表读不到时返回 `None`，`_retire` 收到 `None` 就一条都不回收。撤销本来就有请求前后代次复核兜底，不靠这一步。
  2. **退避把请求级错误也当成连接级**。根因：`request()` 对撤销与退避以外的所有异常都 `_mark_backoff`，包括面板能力检查拒绝和插件对单个请求回的 JSON-RPC 错误；一个面板渲染出错会让同一条连接上的事件投递与收紧征询也停 5 秒（收紧征询拿不到回答按"要求确认"处理，用户莫名多弹审批框）。修法：新增 `_is_connection_failure(exc)`，只在超时（`PluginChannelTimeout`）、`OSError` 家族、或错误码在 `_CONNECTION_FAILURE_CODES` 里时退避整条连接；请求级失败抛给调用方，由面板服务按面板 `retry_after` 自己退避。为让判定只看结构化字段：远端错误回复改用独立码 `MCP_REMOTE_ERROR`（原先与本地帧错误共用 `MCP_PROTOCOL_ERROR`，无法区分），连接启动失败统一包成新的 `PluginChannelStartFailed`。
  3. **`_exchange` 在锁外读 `connection.client` 传给 `before_send`**。根因：并发摘除时会把 `None` 交出去，面板能力检查据此把该槽误标成"插件未声明展示能力"。修法：新增 `_current_client(connection)`，在池锁内取一次并核对"连接仍是表中当前对象且 client 非空"，不成立抛撤销。
  4. **`request` 不更新 `last_used`**（使用约定，非缺陷）。修法：把"每次请求前必须先 `acquire` 刷新使用时间，再 `request`"写进池的类注释与 `acquire` 注释，并在设计稿第 21 节（原 B2 分支里的第 19 节，并入时顺延编号）写明 B3 的四步使用顺序。
  5. **`_new_pool` 只是转发**。修法：删掉，调用方直接调 `_locked_shared_pool`。
- **行为变化一处（修正上一轮的表述）**：退避不是整体"按连接"，而是**分级**——连接级故障按连接退避（同插件其它面板一起等 5 秒），请求级错误仍按面板退避（面板 A 出错不影响面板 B，也不影响以后接上来的事件与征询）。
- **不改的边界**：连接身份不变（每个 (owner, 激活代次) 一条连接、单在途、3 秒请求超时、5 秒退避、120 秒空闲关闭）；池的唯一所有权仍在 `plugin_panels_http`；沙箱开关取值与语义不变。
- **验证与变异**：ae 的 2 个探针原样文件全过并转正为仓库用例；be 的 10 个变异连同本轮 6 个新变异共 16 个全部被杀死；命令与结果见 [TESTS](TESTS.md) 顶部「B2 二轮返工」小节。

## B2 复审返工：共用通道的关闭竞态与注入式共用池（M 线第一期，ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，提交 `af3dd299b` 之后，基于 `8b6344045`，已实施，be、ae、9b 复审通过，并入 step17i）

- **起因**：be 复审 B2（`e62593fc6`、`af3dd299b`）判定不能挑，必须改 6 条。证据目录 `~/.my-agent/decision-evidence/m1-b2-review-20261003/`：4 个竞态探针在 B2 头上全部失败，10 个变异只杀死 4 个。
- **根因与修法（每条）**：
  1. **停用/关闭撞上后台启动会漏关插件进程**。根因：`_publish_transport` 只看 `connection.client is client`，连接被 `retire_stale`/`close` 摘除后仍会把传输发布上去，刚启动的客户端没人 `stop()`。修法：发布时在池锁内核对"连接仍是表里当前对象且客户端未换"，不是就返回 False；启动线程拿到 False 就在锁外停掉刚启动的客户端，并按撤销交给等待者。
  2. **池关闭后旧连接上的请求还会拉起新客户端**。根因：`close()` 只是清表，`acquire`/`_ensure_started` 不看池状态，旧连接对象仍能触发一轮新启动。修法：池加 `_closed` 终态标记，`acquire` 与 `_ensure_started` 见到它、或连接已不在表里，一律抛 `PluginChannelRevoked`。
  3. **`close` 不像 `retire_stale` 那样摘除并置空**。修法：`close()` 走 `_detach`，逐条摘除并清空 `client`/`transport`（同时置 `_closed`）。
  4. **请求在途时连接被摘除，复核抛 AttributeError**。根因：`_require_current` 对已被回收（client 为 None）的连接取 `activation_ref` 直接崩。修法：客户端为空或没有 `activation_ref` 时按撤销处理，抛 `PluginChannelRevoked`。
  5. **共用一个池**：面板服务改成接受注入的池；`plugin_panels_http` 建唯一一个池挂在 server 上（`plugin_channel_pool`），事件中心以后从这里取；`GatewayHTTPServer.stop` 负责关池，面板服务 close 只关自建的池。
  6. **行为变化一处**：退避从"按面板各记一份"变成**分级**——连接级故障按连接退避（同一个插件的其它面板一起等 5 秒），请求级错误仍按面板退避；详见本文件顶部「B2 二轮返工」。
- **不改的边界**：连接身份不变（每个 (owner, 激活代次) 一条连接、单在途、3 秒请求超时、5 秒退避、120 秒空闲关闭）；面板专属的待发合并、结果缓存、面板错误文案都留在展示服务里；沙箱开关的取值与语义不变（只是改成建池时固定一次）。
- **验证与变异**：be 的 4 个探针原样文件全过（已另存为 `test_plugin_channel_lifecycle.py` 防止回归），10 个变异全部被杀死；命令与结果见 [TESTS](TESTS.md) 顶部「B2 复审返工」小节。

## 插件共用通道 B2：面板与事件共用的连接管理（M 线第一期，ds1，2026-10-03，分支 `worker/ds1-b2-channel`，提交 `e62593fc6`，基于 `claude/be-m1-design` `8b6344045`，已实施，be、ae、9b 复审通过，并入 step17i）

- **起因**：M1 设计稿（[插件事件与收紧钩子](docs/design/PLUGIN_EVENT_HOOKS.md)）第 13 节拆块 B2：面板服务已有“每个（owner, 激活代次）一条连接、单在途、代次复核、空闲关闭”，事件中心要复用同一套，不能写第二遍；同时补上面板服务的缺口——连接启动不受渲染超时约束。
- **做了什么**：新增 `agent_py_agent/agent/plugin_channel/`（`pool.py` 与包入口），把连接与在途管理抽成 `PluginChannelPool`；`plugin_display/service.py` 改为持有它，面板专属逻辑（待发合并、结果缓存、退避重试）留在展示服务里。常数（请求 3 秒、退避 5 秒、空闲 120 秒）取值不变。
- **为什么独立成包而不是 `plugin_display/channel.py`**：B3 事件中心、B5 收紧钩子都要用，放展示包下会让事件线反向依赖展示包；独立包保持 `plugin_display` → `plugin_channel` 单向依赖。
- **补的缺口**：连接启动放进后台线程，等待只到本次请求的超时预算；启动超过超时就按超时返回，后台继续启动，下一次请求复用同一条连接。
- **给 B3 的接口**：`acquire` / `request(ChannelCall)` / `retire_stale` / `close_idle` / `close`，以及 `PluginChannelTimeout`、`PluginChannelRevoked`、`PluginChannelBackoff` 三个异常；通道不含任何展示或事件专属概念。
- **边界**：本轮是纯抽取重构，外部行为不变；面板原有用例一行未改照过。真实插件进程、真实 TUI/IM、宿主启用链未验证（沙箱内不起真插件），由 3a 在沙箱外复核。
- **验证与变异**：见 [TESTS](TESTS.md) 顶部「B2 共用插件通道」小节。

## 插件事件订阅与只收紧的工具调用钩子（M 线第一期 M1，be，2026-10-03，分支 `claude/be-m1-design`，基于 `claude/3a-step17h` `b453f8883`，设计稿，ae、9b 评审已吸收，3a 已定 D1–D9，待拆块派活）

- **起因**：goal 2026-10-03 第四节对标 Claude Code Mods。my-agent 插件“装、管、跑”都有了，缺“插进宿主流程”：插件看不到宿主事件，也不能在工具调用前把关。第一期只做“只看、只收紧”。
- **要点**（详见 [插件事件与收紧钩子](docs/design/PLUGIN_EVENT_HOOKS.md)）：
  - 6 类只读事件：提示提交、回合开始/结束、工具开始/结束、命令执行；默认只给结构化事实。3a 2026-10-03 裁定：观察正文仅提示文字；工具参数只经精确工具的 full 收紧门及确认码同意，先去 `__` 内部键、走统一脱敏，不通过观察事件给。
  - 清单 v8（在 v6 任意语言包上加 `events`、`tool_gates`、`permissions.network`）；握手扩展 `my-agent/events`、`my-agent/tool-gate`。
  - 投递照面板服务：每个插件单在途、按类型只留最新并报 `dropped_before`、3 秒超时、停用即撤销；连接管理抽成面板和事件共用的通道。
  - 收紧钩子挂在 `ActionPolicy.decide` 之后、执行之前，只对模型发起的调用生效（执行器请求新增结构化来源 `call_origin`，用户的 `/plugins`、`/plugins@` 宿主命令设成 `host_command`、不受收紧）；插件只能回照原样、要求确认、拒绝，与宿主决定取更严；超时或出错按“要求确认”；插件要求的确认不被自主模式、会话缓存、长期授权自动批准，审批框和 IM 文字写明“[插件 X 要求确认]”；拒绝给 `PLUGIN_GATE_DENIED`。
  - 每次收紧征询写 `runtime_events`（`plugin_gate.decided`），`/plugins info` 在 TUI 和 IM 显示最近决定和观察计数。
  - 安全底座：总开关 `plugin_events_enabled` 默认关，和钩子超时一起是管理员边界项；第一期只 local/main 能启用 v8（待 3a 定）；v8 插件强制进程沙箱、断网（声明网络的除外）、收窄读；确认码覆盖订阅、收紧、正文范围和网络；用户的管理命令（`/plugins disable` 等）永远不受收紧。
  - 原立场 [PLUGIN_LIFECYCLE](docs/design/PLUGIN_LIFECYCLE.md)“不先开放任意核心状态拦截”改为“只开放收紧，不能放宽、改写或接管”。
- **拆块**：B1 清单 v8 → B2 共用通道 → B3 事件中心 → B4 事件点 → B5 收紧钩子 → B6 账本与展示 → B7 安全底座 → B8 样例与验收 → B9 写插件的技能（M5）。B5、B7 由 be 或 9b 复审。
- **决定点**：D1–D9 由 3a 定（设计稿第 15 节），第一期只开放 local/main（别的 owner 启用拒绝，码 `plugin_events_owner_not_allowed`）。
- **依赖与派活**：账本和审批文件不被模型篡改靠 H3，B5–B8 排在 H3 合入之后；派活清单（基线、分支、复审人）见设计稿第 18 节。
- **第二期（不做）**：改写提示词或参数、UI 按钮和输入框、模型请求转发、插件市场。

## 同一回合因非计划重启最多自动续跑 3 次，用完停止续跑、提示用户发“继续”（I4 续，3a 定，2026-10-02，分支 `claude/38-resume-limit`，基于 `claude/3a-step17g` `72ddc2b5c`，已集成 step17g `7ed9e5c07`；插话终态与提示分句并入 step17h，分支 `claude/38-limit-steer-note`）

- **起因**：I4 之后，被停机打断的回合重启后都会自动续跑，但启动续跑一直没有次数上限。本身会把进程弄崩的回合，每次重启都会再续一次，形成崩溃循环。3a 定：加上限。
- **规则**：
  - 同一个 Gateway 用户回合，因“非计划重启”自动续跑最多 `MAX_UNPLANNED_RESUME_COUNT` = 3 次。这是 `gateway_parts/recovery.py` 里的常数，注释写了理由。
  - 用完后再被打断就不再续跑：请求收成 failed，错误码 `TURN_RESUME_LIMIT_EXCEEDED`（已登记，恢复动作 request_user_input）。
  - TUI 和 IM 都显示同一句：“这一轮被打断太多次，已停止自动续跑；发‘继续’可以接着做。”文案在 `conversation/turn_resume_notice.py`。
  - 用户发“继续”是新请求，没有续跑标记，从 0 算，不受这个上限影响。
- **什么算“非计划重启”**：启动恢复、且不是安全重启接班，和续跑原因 `gateway_restart` 是同一个判断。进程崩溃、被杀、直接 stop 再 start 都算。
  - **安全重启接班不计（我定，3a 可改）**，理由三条：
    - 旧进程是排空后确认退出的，不是崩溃；
    - `restart_gateway` 另有冷却和防循环；
    - 本模块原有约定是“部署多次也不能把健康的长任务停掉”。
    - 要把安全重启也算进去，只改 `_counts_as_unplanned_resume` 一行。
  - 服务内租约过期（`processing_lease_expired`）不计：它有自己的卡死预算（`processing_failure_count` 加 `gateway_request_max_attempts`）。
  - 这两种都不会把已用掉的次数清零。
- **计数放哪**：请求记录现有的 `active_turn_recovery` 标记里新增 `unplanned_resume_count`，不新建账本。
  - recovery 每次重排时在旧标记上累加；认领时整条记录原样保留，所以能跨重启累计。
  - 旧标记没有这个字段，按 0 算。
  - 坏值（非整数、布尔、负数）按数据损坏 fail-closed：请求留在 processing，恢复报告带出诊断，不当 0 把次数重置。
- **怎么收口**：和卡死超时共用同一条带 CAS 的终态提交（新拆出的 `_commit_stale_processing_failure`）。
  - 先按原执行编号、租约代次、心跳等核对，再归档：释放会话车道、收口插话，然后写 response、history 和流终态事件 `request_aborted`。
  - 失败结果的 `user_error` 和 `error` 都是那句提示：TUI 显示 `user_error`；IM 公开结果显示 `user_error`，管理员拿完整结果时 IM 显示 `error`。三处一致。
  - 续跑次数和上限放在结构化字段 `unplanned_resume_count` / `max_unplanned_resume_count`，供诊断读取。
- **插话怎么办**（9b 复审建议 1 + step17g 冒烟观察；3a 10-02 定，step17h 实现，分支 `claude/38-limit-steer-note`）：
  - 原则（3a）：用户的插话不能悄悄悬着。续跑上限收口时每条插话都要有终态和用户看得到的说明，内容不丢、也不送两遍。
  - 查清的事实：插话只有在模型答复后提交确认批次、回执记成 consumed 之后，才写进会话历史（去重键 `active-turn-input:<插话编号>`，只在 `transcript_dedupe_key` 一处生成）；被续跑回合取走、送进主调用（submitted）本身不写历史。
  - 规则（只看结构化事实：回执状态、确认批次、历史里有没有这条插话的去重键）：
    - 没被取走的（pending / reserved）：照旧拒收，入口回执把它排成“备用下一轮”马上跑，没有续跑标记、计数从 0。
    - 已被续跑回合取走、送进主调用、随进程一起死掉的（submitted，确认批次修复后仍没有，历史里也没有）：和没被取走的一样拒收、起备用下一轮。step17g 冒烟看到的“悬着”就是这种。
    - 已写进历史的（submitted 但历史里已有它的去重键；正常流程不会出现，按 3a 要求兜住）：收成 consumed，`migration.settle_reason=recorded_in_transcript`，不再起备用下一轮，免得模型看到两遍。
    - 随进程死掉的那次提交都记在 `migration.dead_submission`（提交编号、认领尝试）。
  - 范围：只在续跑上限收口时这样处理。`TURN_RESUME_LIMIT_EXCEEDED` 只在启动恢复里产生，提交插话的进程已证明死亡；卡死超时等收口时进程未必死了、结果未知，已提交的插话照旧不动。
  - 提示（3a 定）：按插话结算的结构化计数选句，三句都在 `conversation/turn_resume_notice.py`（`turn_resume_limit_notice`）：
    - 有备用下一轮：“这一轮被打断太多次，已停止自动续跑；你补充的话会作为新的一轮马上处理。”
    - 只有已记在历史的：“这一轮被打断太多次，已停止自动续跑；你补充的话已记在会话里，发‘继续’会一起处理。”
    - 都没有：原句。
    - 两种都有时说前一句：备用的那一轮带着同一会话历史跑，已记下的补充也会被看到。
  - 做法：
    - `GuidanceRecovery.settle_dead_turn`（续跑上限收口专用，等于 `reject_pending(reject_reserved=True)` 再加上已提交插话的定终态）、`settle_dead_submission`、`turn_end_labels`；汇总计数新增 `dead_submissions` / `recorded_in_transcript` / `backup_turns`。
    - Gateway 两处收口（terminalize，以及“上限答复已封存、插话还没结算时进程又挂了”的启动补交）都经 `_settle_turn_guidance` 分流。
    - 上限收口时，计数写进答复 `guidance_settlement`，提示句同步写进 `user_error` 和 `error`；封存过的答复只在归档前改。terminalize 内联的插话收口抽成助手，函数反而变短。
  - **没有入口回执的插话**（9b 10-03 复审发现的老问题；3a 定、ds2steer 实现，分支 `worker/ds2-steer-noreceipt`，待 9b 复审）：上面那条“拒收后由入口回执排成备用下一轮”只对有 `gateway_input_request_id` 的插话成立。`/steer` 控制命令且 scope 里没带入口请求号（IM 多是这种）的插话，收口时被拒收又没有下一轮，内容就丢了。现在按两级处理，判据只看结构化字段（回执级 `migration.closeout_replay` 标记、`entry.metadata` 里的会话身份、回执去重键），不读正文：
    - 有可重放的结构化输入 → 用同一条结构化入口（`load_or_prepare_gateway_input_locked` + `queue_gateway_input_locked`）排成“备用下一轮”，请求号由插话身份推出（`steer-replay-<hash>`，同一插话重放两次只排一条）；计数 `steer_replay_queued`。
    - 拿不到可重放的输入 → 不排轮次，给该会话一条宿主提示“你刚才补充的话没有被处理，请重新发送。”（`STEER_CLOSEOUT_UNAVAILABLE_NOTICE`，来源 `gateway_steer_closeout`、原因码 `steer_closeout_unavailable`），TUI 灰行与 IM【提示】共用；计数 `steer_replay_unavailable`。
    - 两种收口（续跑上限收口、普通回合结束还没取走的插话）都经同一个 `settle_gateway_inputs_for_turn`，所以两级分流对两条路同时生效；有入口回执的旧路径行为不变。
    - 顺带修：`host_notices_from` 原来只认字典、不认 `HostNotice`，而 `take_host_notices` 返回的正是对象，导致取走的提示在渲染时整条消失；现已两种都认。
  - 已知边界：答复已经归档之后才发生的结算失败，提示不会再改（终态归档是完成权威）；入口对账失败照旧留给周期对账重试。
- **已知边界 / 后续项**（I4 留下的，这次没动）：
  - 仍没有“打断太久就不续”的时间上限；
  - 停机那一刻仍不会立刻提示“将会续跑”。
- **验证**：见 TESTS.md 同名节。

## 能力包说明补“宿主核验”一节（文档，luna5，2026-10-03，分支 `worker/luna5-pack-guide`，基于 ae 能力包块 5 头 `58300131c`，已完成，ae 已复审，并入 step17h）

- **做什么**：`docs/guides/CAPABILITY_PACK_GUIDE.md` 新增“六、宿主核验”一节（原六、七顺延为七、八），面向用户用中文大白话讲清：
  - 宿主核验是什么：任务固定住的能力包带检查程序时，宿主在写工具写出交付物后马上跑一遍（写完就查）、回合结束前再查一遍（收尾再查），结论以宿主为准，不靠模型自述；
  - 开关 `capability_pack_host_verification_enabled`：默认关、在能力配置里、只能管理员经 `/settings` 或配置文件改、改后重启 Gateway 生效；打开前要满足的三个前提（核验账本受宿主只读保护、主/子代理写不进账本、`/stop` 能打断检查程序），现状：前两条已做完（H3 缺口已修），第三条 `/stop` 打断仍在开发（ae 复审时按现状改）；
  - 用户会看到什么：TUI 灰色系统行、IM 以 `【提示】` 前缀放在回复正文前；三类事实（检查结果、输入原件被就地改、必需交付物缺失或打不开）各配代码里的真实文案；
  - 返工：检查不过最多 1 次、输入原件被改最多 1 次、缺交付物最多 2 次；三类各自计次、同时出现合成一条；只要审阅不要交付物时在答复里说明；
  - 已知限制：不调工具的回合查不到、检查程序只在沙箱断网跑、location 脱敏的代价（JSON Pointer 首段是本机根目录名时整条置空）。
- **依据**：`capability/pack_verification_service.py`、`pack_verification_report.py`、`pack_verification_inputs.py`、`pack_verification_deliverables.py`、`gateway_parts/request_pack_verification_notice.py`、`docs/design/CAPABILITY_PACKS_V2.md`（文案与常数逐条对照代码）。
- **验证**：`scripts/check_doc_sync.py` 与 `git diff --check` 通过；本轮只改文档，未改代码、未跑代码测试。

## 记忆整理补跑后续：默认补跑推进过的组，下一批会话模型只试 1 次（75，2026-10-02，分支 `claude/75-curator-fallback-threshold`，基于 `claude/3a-step17g` `8a832d4e1`，已实现，并入 step17g）

- **生产事实**（3a，step17f 上线后 local/main 的 curator/runs）：13:58 qwen3.8-flash 那组用 MiniMax-M2.7 补跑成功，带 `curator_thread_model_failed:CURATOR_MODEL_FAILED:transient`，修复生效；13:59 这个会话来了新消息，又先试 qwen3.8-flash，失败。按原规则还要再失败一次才换默认模型。
- **做法**（be 留的后续项，3a 定）：这组最近一次推进本组游标的运行，如果就是“连不上让给默认”的补跑，下一批的阈值从 2 降到 1（`CURATOR_TRANSIENT_REPEAT_FALLBACK_FAILURE_COUNT`）。会话自己的模型每批还有一次机会，失败一次就直接换默认，不再每批白白失败两次；会话模型自己成功推进后，阈值回到 `CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT`=2。
  - 只读已有运行账：`group_breaker_history` 筛过的本组历史里第一条成功，就是最近一次推进本组游标的运行；看它的警告里有没有 `transient_fallback_warning(<连接类失败码>)`，按全文比对。不加新状态。
  - 警告码格式只在 `curator_routing.transient_fallback_warning` 一处生成：补跑写记录（`curator._transient_fallback`）和判断阈值用同一个函数。
  - 确定性失败（坏 JSON）的补跑警告不带 `:transient`，不降阈值；账里看不到成功（太早）按 2 处理。
- **验证**：见 TESTS.md 同名节。

## 只读 CLI 的只读启动（CLI_READONLY_STARTUP，sol3，2026-10-03，分支 `worker/sol3-cli-ro`，基于 H3 `d160a2c6d`，**结论：不做（3a 10-03 定乙）**，be 复审通过，并入 step17i）

- **现状**：八条查询命令都会先构造完整 `SimpleAgent`。S2 的 home/种子初始化与 S3 的 owner 登记都只补缺失项；在既有 home 上不写。第一个必然命中的硬写点是 `LocalStore._init_schema`。be 的 SQLite WAL/真实仓储探针证实：最后一个连接正常关闭后只剩主库，沙箱拒写目录里的 `mode=ro` 返回 `SQLITE_CANTOPEN`；仓储没有长连接，即使 Gateway 空闲也会落入该状态。
- **裁决**：不在模型沙箱里支持 CLI 只读启动，保持 H3 的拒写与结构化错误 `CLI_HOST_STATE_READ_ONLY`，提示改用内置工具。甲方案“宿主保持长连接”将引入新的连接/sidecar 生命周期不变量，不为这件事实施；与“模型沙箱碰不到宿主状态”的安全方向一致。
- **工具覆盖及明确缺口**：逐命令核实的内置工具名称、能力边界和探针证据见 [CLI_READONLY_STARTUP 现状与裁定](docs/design/CLI_READONLY_STARTUP.md)。本次无产品逻辑改动；同步源内注释和测试说明；原稿 S5–S10 及相关副作用因结论为不做而未核。

## 宿主托管文件对模型只读（H3，be，2026-10-02，分支 `claude/be-host-config-guard`，基于 `claude/3a-step17g` `8a832d4e1`，已实现，9b 三审通过，并入 step17h）

- **起因**：
  - 管理员的文件工具和命令能直接写 `~/.my-agent/config/`，绕过参数中心的 `BOUNDARY_KEYS`、修改账本和 `manage_models` 的 M1 检查。
  - 3a 扩项后并进两件：9b 发现的“隔离 Shell 能写自家 `runtime.db`”，以及 ae 能力包块 4 的核验记录。
- **做法**：唯一声明在 `path_access_policy`（路径片段，插件 SDK 照样只依赖标准库）。
  - 保护范围分三类：
    - 宿主配置：数据根 `config/`、`system/config/`，各 owner 的 `config/`；
    - 宿主运行状态（9b 盘点、3a 定口径）：各 owner 的策略文件及 `.lock`、审计流水、`runtime.db` 及伴随文件、`workspace/runtime/`、`audit/`、Curator 事务、记忆流水与候选、记忆归档、缓存、回收站、owner 级正式 skill、`agents/`、owner 根的整个 `data/` 等，按路径拒写，不管存不存在；
    - 规范任务根的 `data/pack_verification/`。
  - **A/B 两类，每条路径只在一处**（3a 定）：
    - 上面这些是 A 类，绝对只读，唯一声明在 `path_access_policy`。原 `tool_runtime_ledger` 控制面清单里的绝对项已搬过来、从那里删掉。
    - B 类留在 ledger：只剩 `runs/`、`tasks/`，只在隔离模式挂，可被本任务工作目录穿透。
    - owner 根的 `data/` 整体归 A 类（3a 2026-10-02 定）：那里全是宿主状态，宿主以后还会加新目录；逐个列子目录是封闭清单、新目录默认没保护，违反开放世界铁律。模型该写的 `data/` 路径只能作显式例外（写明原因和用例），目前没有。`agents/` 也归 A 类（旧子代理运行状态和派工报告，不是任务树）。
    - 不保护：`artifacts/`、`workspace/` 其余、`tmp/`、记忆正文。
  - 文件工具、写边界、插件写入上下文都走 `check_write`。拒写码是 `PATH_HOST_CONFIG_WRITE_BLOCKED` 和 `PATH_HOST_STATE_WRITE_BLOCKED`，提示指向 `user_config` / `manage_models`。写边界把这些具体码原样传给动作策略和 registry，真实链路里模型看到的是具体码，不是通用的 `WRITE_FORBIDDEN`。
  - 宿主凭据（管理员密码、模型目录、共享模型档案、数据根 `config/` 里的 YAML 配置及备份、owner home 之外的 `secrets` 目录）对文件工具读写都拒，码是 `PATH_HOST_CREDENTIAL_BLOCKED`。命令只拒写不拒读（读取不在本项范围）。
  - 数据根只有一个权威来源：`PathAccessPolicy.from_values(agent_home_root=...)` 由宿主解析出的数据根结构化传入，Full Access 下不再读进程环境变量（二审必须改 1）。
  - 命令类工具在 `_sandbox_exec` 里加只读覆盖，任何模式都生效，Full Access 也是。
  - 宿主路径判定一律大小写无关：macOS 上 `CONFIG/Desktop.YAML` 就是 `config/desktop.yaml`。H2 插件库原来也有这个口子，一并修好。
- **沙箱通用规则**：
  - 探针发现：macOS 上命令可以先把上级目录改名，再写原本只读的路径。人格文件、H2 隐藏路径、`runtime.db` 的覆盖都有这个口子。
  - 修法：给所有受保护路径的上级目录加 literal 写拒绝，排在隐藏路径之前、断网之前。
  - 还不存在的受保护路径在 macOS 上也写拒绝规则。
- **顺带**：`restart_gateway` 任何审批模式都要确认（3a 定）。
- **补洞（ae 块 4 发现，2026-10-02）**：
  - 问题：Full Access 下命令在任务树外跑、写根又不含本任务时，本任务的核验记录没进只读覆盖。
  - 修法：`registry_invoke` 改从写边界的结构化 `task_root` 推出本任务的宿主托管位置，交给命令沙箱，不再依赖工作目录在哪。
  - 二审建议：macOS 再加 Seatbelt 正则，盖住 owner 下全部任务的 `data/pack_verification/`（别的任务的、还没建的都算）；Linux 只保护本任务。
  - 三审补（9b 实测“改名别的任务的 `data/`、写入、再改回”能伪造记录）：布局各级目录（`runs`、`runs/<日期>`、`runs/<日期>/<键>`、`tasks` 同理、`audits/<编号>`）和任务根下的 `data` 目录本身也按正则拒写，只拦它们自己的改名、删除、新建；可选层级写成显式分组，Seatbelt 不认 `{m,n}`。副作用：模型的命令不能自己新建或改名任务根。
- **模型在命令里跑 my-agent CLI 会失败**（二审实测，3a 最终裁定这次不修 CLI）：CLI 每条命令都要构造完整的 SimpleAgent，启动时要写 `workspace/runtime` 下的本地库，沙箱里写不了。Full Access 下 `status` 等只读命令从 rc=0 变成 rc=1；隔离 owner 原来就这样。请模型改用内置工具。起不来时 CLI 输出结构化错误 `CLI_HOST_STATE_READ_ONLY`：沙箱给命令设 `MY_AGENT_HOST_STATE_READ_ONLY=1`，CLI 只看这个标记和异常的 errno / sqlite 错误码，不解析报错文字。
- **后续项**：
  - 只读子命令走只读启动：**不做（3a 2026-10-03 定乙；探针证据、替代工具与缺口见 [CLI_READONLY_STARTUP](docs/design/CLI_READONLY_STARTUP.md)）**。
- **已知边界**：
  - Windows 上命令不进沙箱；
  - 命令能读凭据；
  - Linux 上还不存在的路径挡不住（休眠 owner 的 `config/`，宿主没开库时的 SQLite 伴随文件）；
  - Linux 上命令只保护本任务的核验记录；
  - 沙箱外早已存在的硬链接；
  - 插件沙箱关闭时的插件进程；
  - 用户配置放在数据根以外时不受保护。
- 详细设计见 [HOST_CONFIG_WRITE_GUARD](docs/design/HOST_CONFIG_WRITE_GUARD.md)；测试见 TESTS 同名条目。

## 记忆整理：会话自己的模型连续连不上时让给 owner 默认模型（be，2026-10-02，分支 `claude/be-curator-transient-fallback`，基于 `claude/3a-step17f` `f6b63ab35`，已实现，并入 step17f）

- **生产事实**（3a，K1 上线后 local/main 的 curator/runs，只看结构化字段）：13:12 某会话选的 qwen3.8-flash 那组 `CURATOR_MODEL_FAILED`，两次 ProviderTransientError，各约 22 秒。
- **风险**：原来只有 `CURATOR_SCHEMA_INVALID` 会用默认模型补跑。如果某会话的模型持续连不上（额度、服务商故障），含最早待处理消息的那组一直排第一、一直失败；瞬时失败不计熔断、只退避，这个 owner 的整理就长期卡在这组，别的会话也整理不了。
- **做法**（3a 给了两条路，取第二种的变体）：
  - 非默认组在同一起始游标（投影到本组会话）上连续 `CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT`=2 次连接类失败（`CURATOR_MODEL_FAILED`、`CURATOR_MODEL_TIMEOUT`）后，**下一次运行**这组不再试它自己的模型，直接用 owner 默认模型。成功和失败记录都带 `curator_thread_model_failed:<码>:transient`。
  - 计数只读已有的运行账（`group_breaker_history` 按会话自己的模型身份筛本组历史），不加新状态；别的组夹在中间成功不清零，推进了本组游标的成功（含这次默认模型补跑成功）清零。新消息来了，会话自己的模型重新有机会。
  - 改用默认模型时运行身份先改成默认模型：默认模型自己的瞬时失败照旧退避，确定性失败照旧按它自己的历史熔断；本组历史不被打断，所以默认模型没成功前每次都直接用它。
  - 不改成按组隔离熔断，不改退避口径；默认组自己的失败不受影响（没有别的模型可换）。读运行账出错按没达到处理。
- **为什么不在失败的那次运行里马上补跑**：一次运行的租约只有一整次提取的预算加 90 秒（`_lease_seconds`）。超时类失败已经把它用掉，再补跑一次会丢租约，提交被拒。下一次运行直接用默认模型，还省掉对一个已知连不上的服务商再烧一轮重试（生产那次约 44 秒）。代价：每批新消息最多先失败 2 次（每次之后退避 30–300 秒）才换到默认模型。
- 实现：`memory_store/curator_routing.py`（常数与纯函数 `transient_fallback_code`）、`memory_store/curator.py`（`_CuratorRunMixin._transient_fallback`）。测试见 TESTS 同名条目。
- **后续（已做，75）**：默认补跑推进过本组游标后，下一批只给会话模型 1 次机会，见本台账“记忆整理补跑后续”条目。

## 被宿主停机打断的回合重启后统一自动续跑，TUI 和 IM 都看得到（I4，第 8 条②，用户选 B，2026-10-02，分支 `claude/38-resume-rule`，基于 `claude/3a-step17f` `f6b63ab35`，已集成 step17g）

- **现状（查清）**：Gateway 停机时，在跑的用户回合有两种结局，全看时间先后，不是策略：
  - 被拒（A）：收尾关闭模型调用准入后，回合的下一次模型调用被拒（`MODEL_CALL_ADMISSION_CLOSED`）。请求写成 failed，重启后不续跑，用户只看到一句泛泛的“模型本轮响应未完成”。
  - 消失（B）：进程先退出，请求留在 processing。重启时启动恢复按死进程把它重排（`gateway_restart` / 安全重启是 `gateway_safe_restart`）并自动续跑；TUI 显示“已自动续跑”，IM 什么都没有。
  - 安全重启也受影响：排空第一段到时后没跑完的回合，如果下一步是模型调用，就会走 A，和“没跑完的回合由新进程续跑”的承诺不一致。
- **规则（我定，用户 10-02 选 B 后交给我）**：被宿主停机打断的用户回合，重启后**都自动续跑**，并且 TUI、IM 都看得到“这一轮被重启打断、已自动续跑”。
- **为什么选“都续上”，不选“都不续、只提示”**：
  - 停机是宿主的事，不是任务失败。`MODEL_CALL_ADMISSION_CLOSED` 的合同本来就写着“不是供应商失败也不是任务失败”，写成 failed 等于让用户为宿主的维护动作买单。
  - 续跑本来就是现有设计：崩溃恢复和安全重启都会续跑，A 是唯一的例外，而且只是时间巧合造成的。反过来让 B 也不续，要在启动恢复里新造一条“写失败终态”的路，还会打破安全重启的承诺。
  - 用户被打断后习惯发“继续”。自动续跑之后：
    - 用户什么都不用做；
    - 习惯性发的“继续”照现有输入投递规则进同一会话，不会让旧回合从头再跑：续跑从耐久工具账接着往下走，已执行的工具不重做。
  - 安全：A 停在“模型调用边界”上，关门之后的工具不会启动（I3）；已执行的工具由续跑恢复从耐久工具账带回，不重做；没有执行中的工具，就不会出现“结果不确定”。
- **做法**（只认结构化事实，不读异常文本）：
  - `request_execution._host_shutdown_resume_marker`：失败沿显式原因链认出宿主停机准入拒绝（`find_model_call_admission_error`）时返回 `restart_resume` 标记（准入拒绝码 + 关门原因码）。
    - 这时不收口插话、不记审计；`request_worker._finish_claimed_gateway_request` 见到标记就直接返回，请求原样留在 processing。
    - 这样 A 的持久状态就和 B 一样，重启后走同一条恢复：按死进程重排；续跑认领时写 `turn_resumed` 边界；死进程留下的会话车道占用立即接管；还没提交给模型的插话预留按死掉的 attempt 退回待处理，由续跑回合再认领。
  - 被拒那次 attempt 已记 failed，不挡续跑：续跑核对按精确 task+run 查（不看任务账是否已关），对已终态的 attempt 返回 not_required，同一主 run 开新 attempt。真实链路用例跑通了这条路。
  - 用户停止优先：执行中用户已发过停止（请求上的 `cancel_requested`）的，不留给续跑，照常按取消收尾。普通失败照旧写 failed。
  - IM 看得到：续跑回合的最终结果在 `channel_delivery.host_notices` 最前面补一条宿主提示（来源 `gateway_turn_resume`，code 是 cause），飞书等适配器把它渲染在回复正文前。
    - TUI 已在续跑边界显示同一句话，且不读 `channel_delivery`，不会重复。
    - 文案表从 TUI 挪到 `conversation/turn_resume_notice.py`，两边共用。
  - `MODEL_CALL_ADMISSION_CLOSED` 的恢复提示和 `runtime_errors` 的 host_stopping 说明，改成“Gateway 前台用户回合重启后自动续跑；子代理 run 不自动重跑”。
- **不做**（goal 第 8 条）：发送层硬门；子代理停机中断后自动续跑不占次数（子代理照旧由父级按 recovery_decision 续派）。
- **已知边界 / 后续项**：
  - 启动续跑没有次数上限（B 原本就是这样）。如果某个回合本身会把进程弄崩，每次重启都会再续一次。3a 10-02 定做，已在 `claude/38-resume-limit` 实现：非计划重启最多续 3 次，见“同一回合因非计划重启最多自动续跑 3 次”那节。
  - 没有“打断太久就不续”的时间上限：停机一天后再启动，那一轮照样续跑。
  - 停机那一刻 TUI/IM 不会立刻提示“将会续跑”：TUI 看到的是 Gateway 断开 / 正在安全重启，提示在续跑开始（TUI）和续跑完成（IM）时出现。
- **验证**：见 TESTS.md 同名节。

## F1 候选投影：发给 Jev 的能力候选按字段字节上限截短（T2 续，75，2026-10-02，分支 `claude/75-jev-candidate-cap`，基于 `claude/3a-step17e` `4dd56f627`，已实现，真实 Jev 一次通过，并入 step17f）

- **问题**：生产 skill_tool 候选 53 题（52 个 Skill + 1 个插件组），Jev 请求 129,941 字节，按 `jev_wire_bytes.v1` 上界 79,563 > 57,600，实验路径 `input_bound_out_of_calibration` 拒发，F1 晋升走不下去（step17b 生产记录）。
- **为什么只截字段不够**：请求体用 `json.dumps` 默认转义，一个汉字 6 字节、按公式 3 个上界 token。每题重复的 question/boundary/criteria 加结构约 1,441 字节，53 题光固定部分上界就是 53,292；只留名字也还有 56,658。
- **做法（3a 定方案 A，只改发给 Jev 的投影，Skill 原文不动）**：
  - 候选只带白名单字段 kind/name/description/when_to_use/tools_required，插件组的 tools 只留每个工具的 name/description；ref/version/tool_refs/keywords/category 不上线。答案按题号映射回宿主保留的完整行（`selected_capabilities` 原本就按下标）。
  - 每题都一样的 question/boundary 两句话挪进 `state.instructions` 只发一次（Jev 每题都看到 state + 本题，语义不变）；插件组的说明和 criteria 仍逐题。
  - 文字字段按“转义后字节”从前往后截最长前缀，不按内容挑词：name 96（约 16 个汉字）、description 180（约 30 个汉字）、when_to_use 144（约 24 个汉字）；插件组内工具名和说明用同一上限。上限是代码常数（`decision_candidates.py`），注释写标定依据，不加开关（投影规则，不是能力授权）。
  - 截断计数写进能力推荐观察记录 `candidate_projection`（`jev_candidate_projection.v1`：各字段上限、截了几条、截掉多少转义字节），只有计数没有正文；宿主 state 里的完整行和计数都不上线（`_wire_state`）。
- **标定核验**：最坏情况（53 题、state 撑到 4,096 字节、三段文字全按上限填满汉字）上界 57,204 ≤ 57,600，再加 1 题就拒发；真实 53 题（生产 owner Skill 只读副本）B=77,954、上界 53,569，截了 50 条 description（11,244 字节）和 49 条 when_to_use（11,582 字节）。题数更多或插件工具很多时照旧 `input_bound_out_of_calibration` 拒发，不外推。
- **真实 Jev**：真实 Jev 一次（`81f8e169a`，隔离 home、私有端口 8487；目录副本只含生产 Jev 档案 jev-1.13.0 和 MiniMax M2.7；生产 owner Skill 只读副本 + workspace-peek 插件，共 53 题）：`/experiment apply skill_tool 10m 1 60000 数一下当前目录里 notes.txt 一共有多少行`。B=77,982 字节、state 1,237 字节、经验上界 53,583，在标定范围内，预留后结算 charged；Jev 实际计费输入 21,489（上界的 0.40），输出 4,442。截断 description 52 条（11,491 字节）、when_to_use 49 条（11,582 字节）。Jev 选 3 项：workspace-peek 插件（include 概率 0.66，合理）、subagent-read-scope-check（0.56，不对；它的完整说明同样只讲子代理，不是截断造成）、verification-before-completion（0.41，边缘）；其余 50 项 not_needed。晋升评估 keep_observing（insufficient_samples、no_savings：插件被选中，没有可收起的工具）。证据 `~/.my-agent/decision-evidence/f1-jev-candidate-cap-81f8e169a/`。
- **T3 注意**：上界约 5.4 万，`/experiment apply skill_tool … <输入token上限>` 要给到 57,600 以上（例如 60000）；原流程写的 50000 会在预留时被 `input_budget_exhausted` 拒掉。
- **代价与未做**：Jev 每个 Skill 只看到约 30 + 24 个汉字的说明，选得准不准要看真实样本。criteria 每题约 938 字节（53 题约 2.5 万上界 token）仍逐题重复；改成共享说明或换 UTF-8 编码（要重新标定 v2）留作后续选项，本轮不做。
- **验证**：见 TESTS.md 同名节。

## 停机关门后迟到的模型响应里的工具不执行（I3，第 8 条①，2026-10-02，分支 `claude/38-late-tool-fence`，基于 `claude/3a-step17e` `4dd56f627`（原基于 `e75cf6061`），已集成 step17f；9b 复核小意见随 I4 分支交）

- **问题**：J17 栅栏关门时（`close_model_call_admission`），只在账本里把在途模型调用记成 failed（原因码 `MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`）。
  - 物理 HTTP 调用还在跑，响应稍后照常回来；`_finish_model_generation` 不看账本终态（迟到的成功也不会重开终态），响应原样交回工具循环。
  - 于是响应里的工具调用照常执行，例如写文件。用例复现：不加这次的屏障，迟到响应里的 `write_file` 真的把文件写出来了（变异 i01）。
- **做法**（只认结构化事实，不看文本）：
  - 工具轮里每个未启动调用前本来就有一道顺序屏障（`round_execution._defer_unstarted_calls_if_needed`，原先管取消和 compact 延后）。现在它先读本进程准入的关门事实 `model_call_admission_closure()`。
  - 关门后，当前及后续调用一律不启动，各配一条宿主结果：新登记的 `HOST_SHUTDOWN_TOOL_NOT_STARTED`（status=cancelled、handler 未执行、effect not_started，恢复动作 stop）。
  - `metadata.host_shutdown` 带关门原因：`reason_code` / `error_type` 和账本把这次模型调用结清成 failed 时用的是同一份 `ModelCallAdmissionClosure`，另记准入拒绝的固定码 `admission_error_code=MODEL_CALL_ADMISSION_CLOSED`。所以账本和工具账对得上：模型调用是“停机中断、未结算”，它带来的工具调用是“停机、未执行”。
  - 轮中途才关门时，已启动的调用照常收尾，只拦还没启动的；停机优先于普通取消（两者都命中时记停机）。
  - 之后下一次模型调用照旧被准入拒绝（`ModelCallAdmissionClosedError`），请求不发出，回合按宿主停机收尾（运行错误报告 `host_stopping`），和关门后被拒的回合是同一条路。
- **不做**（goal 第 8 条）：发送层硬门；子代理停机后自动续跑不占次数。被拒回合重启后的续跑规则是 I4，单独交付。
- **顺带（3a 定）**：`restart_gateway` 的审批说明按代码更正：ask 弹确认，auto / 完全放行不弹，安排后先等空闲再换进程。改了配置注释、工具模块头和 GATEWAY_SAFE_RESTART.md。
- **9b 交叉复审后的修改**（证据 `~/.my-agent/decision-evidence/review-i3-7a55bd047/`）：
  - 必须修：审批通过之后不再读准入，等审批期间关门、之后被批准的调用照样执行（探针 A 实测）。现在 `_resolve_tool_approval` 在批准之后、重新执行之前读一次 `model_call_admission_closure()`，已关门就配 `HOST_SHUTDOWN_TOOL_NOT_STARTED`、不执行、不记批准绑定；串行步和并行段之后补审批都经这一处。
  - 落盘对账：工具账（任务工作区 `tool_outputs/index.jsonl` 的 `tool_execution`，以及续跑用的精简执行事实）按同一白名单带上 `host_shutdown`（reason_code / error_type / admission_error_code，round_cancelled 只认 True），事后能和模型调用账本逐字段对上。
  - 停机和取消同时命中：错误码仍是 `HOST_SHUTDOWN_TOOL_NOT_STARTED`，给模型的提示改用取消那句（“停止派发新动作，保存已有进展后收尾”），并记 `round_cancelled=true`。用户 /stop 后 24 小时内续跑时，历史里的提示不能引导模型重做用户喊停的动作。
  - 文案：`restart_gateway` 注释按 9b 的措辞写清两段排空（先等在跑的回合，最多 turn_wait 秒，没跑完的停在下一个副作用工具前、由新进程续跑；再等执行中的副作用工具，最多 drain_timeout 秒，超时取消这次重启、不强杀），`gateway_restart_drain_timeout_seconds` 上方原来那句“超过就先强制收尾”改对。
- **9b 复核小意见**（3a 让随 I4 分支 `claude/38-resume-rule` 一起交）：
  - 审批路径发请求之前也读一次准入（`_closed_admission_execution`，批准之后那次复读保留）。已关门就直接配停机结果、不再询问，停机后 TUI/IM 不会再多弹一张没用的审批卡。
    - 并行段补审批时，第一张卡批准时已关门，第二条不再发请求。
  - 档案侧（`tool_call_archive_record`）和索引侧（`tool_output_externalizer`）两份白名单合成一个清洗函数 `tooling.runtime_facts.project_host_shutdown_facts`，以“单行、不超过 128 字”为准：
    - 非字符串、跨行（含 Unicode 换行符）的丢掉，超长的截断；
    - round_cancelled 只认布尔 True。

## 记忆正文的其它文件也按私有原子写（S2，75，2026-10-02，分支 `claude/75-memory-private-writes`，基于 `claude/3a-step17e` `4dd56f627`，已实现，并入 step17f）

- **问题**：38 的 S3 只把记忆本体和向量文件改成私有写；候选、日事件、lesson/HOT、Curator 事务备份这些也装着记忆正文，仍是普通原子写：新文件按 umask（一般 0644），替换时还把旧文件的 0644 抄回去。
- **做法**：复用 S3 的 `common/json_io.write_private_text_file_atomic_unlocked`（临时文件一建出来就是 0600，替换时不抄回旧权限，直接父目录 0700），不另写一份写法：
  - `candidates.py` 候选整文件替换；`daily.py` 日事件分片替换；
  - `lessons.py` lesson 文件、`routing/INDEX.md`（每条带正文摘录 `topic`）、HOT 晋升写入与超预算降级重写；
  - `curator_commit.py` 事务前镜像 `before-*.txt`（在各自路径锁下写，事务目录 0700）、事务目标替换 `_write_target`（日事件、候选等）、回滚写回 `_restore_transaction`。
  - 生产里已有的 0644 文件靠下一次写入收紧，不做一次性批量 chmod。
- **全仓排查，没改的写入口**（都不装记忆正文，或不是记忆库）：
  - `operations.py` 的 ops.jsonl、Curator 运行审计与状态文件、事务 `manifest.json`、retention 的 tombstone/绑定：按合同只记编号、哈希和元数据。
  - retention 回收用 `shutil.move`、owner 备份用 `copytree/copy2`：沿用源文件权限，源文件收紧后副本也是 0600；已有的旧备份不动。
  - `migration.py` 一次性旧数据迁移：MEMORY.md/HOT 只重置成模板，不动。
- **补：迁移备份私有复制**（3a 定做，单独提交，基于 step17f `f6b63ab35`）：`_backup` 原来用 `copy2`，旧文件是 0644 时 `backups/<run_id>/` 里的副本也是 0644。改成 `_copy_private`：在本次新建的 `backup_dir` 里以 0600 新建，只拷字节和访问/修改时间（回滚用 `copy2` 拷回原位，恢复出的文件随之是 0600，修改时间仍是原值）；`backup_dir` 及其下每级目录 0700，不碰共享的 system backups 目录；manifest 只有路径和是否存在，照旧。已有的旧备份不批量 chmod。
- **单独立项（3a 记后续项）**：`memory_archive/` 那批会话与任务数据的收紧。
  - `memory_archive/`（任务/运行事实索引、Compact 快照、任务工作区、外置工具输出）属于会话与任务数据，不是记忆库正文，这轮不动。
- **测试顺带修正**：`test_hot_budget.py` 两个用例原来直接写 `/tmp/hot-budget-*.md`；HOT 改成私有写后会尝试把直接父目录收紧到 0700（以 root 跑测试时 `/tmp` 真会被改），改成写 `tmp_path`。
- **验证**：见 TESTS.md 同名节。

## 能力包 v2 块 7：A 包 0.5.0、B 包 0.3.0 的流程、模板、检查器和核验声明（be，2026-10-02，分支 `claude/be-capability-packs-content-b2`，基于 ae 块 2 `ce2b833a7`，已实现，待 ae 审）

- **依据**：冻结重跑逐条归因（`capability-packs-v2-design/attribution.md`）里 K 类 7 次、P 类 2 次，按 ae 的设计（[CAPABILITY_PACKS_V2](docs/design/CAPABILITY_PACKS_V2.md) 第 2 节）补包内容。只改 `examples/capability-packages/` 下两个包，不改宿主。
- **A 包 0.5.0**：
  - 新检查：error 有 `placeholder_text`、`embedded_quote_not_verbatim`（另有 `embedded_quote_not_in_line`）、`adaptation_original_not_in_source`；warning 有 `prop_states_missing`、`prop_origin_unstated`、`named_character_offscreen`、`shot_too_short`（`--min-shot-seconds`，默认 2 秒）。
  - 新可选字段：改编条目 `{text, original_quote}`、台词 `embedded_quotes`、道具 `origin`；资料仍是 `drama_text_delivery.v3`。
  - 模板的自由文字改成 `<…>` 提示，照抄不填会被 `placeholder_text` 抓到。
- **B 包 0.3.0**：
  - 新检查：error 有 `missing_table`、`missing_foreign_key`（键不在时；键在但值不对仍是原错误码）、`unknown_reference_mention`（参考 ID 写法从本项目 references 表推出前缀，不写死格式）、`baseline_beat_changed`、`baseline_relation_changed`、`baseline_schema_or_duration_changed`、`handoff_claim_without_change`；warning 有 `beat_character_missing`（动作节拍可选 `character_ids`）、`shot_character_reference_missing`（项目有人物参考计划时才查）。
  - **约定变化**：0.2.0 的“基线差异只提醒”改为四类改动（节拍增删或台词、对应关系、schema、时长）没在交接里列出就是 error；“列出”看交接地址，文件按 `files[].sha256` 对应改前改后的项目。其余差异仍只提醒。
  - 交接模板的改动条目写成“基线值 → 新值：为什么改”，整段照抄 `<…>` 提示报 `placeholder_text`。
- **宿主核验输出**（按 ae 定的 `pack_verifier_result.v1`）：两包都加 `--host-json`，`valid` 等于 errors 为空，条目只有 code/location，metrics 只留数字且不超过 16 个键，写出即退 0；读不了交付物记 `target_unreadable`。B 在宿主模式下没有 `--input-file` 时，交接文件按摘要对应宿主交来的项目和基线，对不上的只提醒、不读，结论不依赖它们。不加参数时两包原报告和退出码都不变。
- **核验声明**（`capability.verification`，按 ae 块 2 的 `inputs` 协议，经真实校验器检查）：
  - A：交付物 `delivery`（`**/*.json`、顶层 `schema` 等于 `drama_text_delivery.v3`，必需）；检查程序 `scripts/check_delivery.py`，参数 `--delivery {target} --host-json`，超时 20 秒；`--source` 取任务开始时已有、`schema` 为 `drama_text_source.v1` 的文件，必需。
  - B：交付物 `project`（`schema` 为 `drama_workflow_project.v1`，必需）；检查程序 `scripts/check_continuity.py`，参数 `--project {target} --host-json`；`--handoff` 取本回合写出的 `drama_workflow_handoff.v2`，`--baseline-project` 取任务开始时已有的项目，都非必需。交接不声明成必需交付物：新建项目的任务没有交接（ae 同意）。
  - 两包 `input_policy` 都是 `preserve_originals`（另存新文件，不就地改输入）。
  - 启用时要管理员确认（`kind=capability_verifiers`），确认说明列出这些输入。
- **宿主模式不能崩**（ae 审阅的必须修）：模糊测试发现 4 处遇到列表或对象就 TypeError（A `prop_mention_warnings`；B `shot_reference_warnings`、`stage_addresses`、`beat_warnings`，后两处是旧函数）。崩溃时宿主只能记 `verifier_output_invalid`，整次结论丢失。修法是这几处只收字符串、坏形状跳过（结构错误另由原检查报出），不在入口套全局 try（那会把检查器自己的 bug 算成交付物错误，逼模型返工）。
- **交接摘要过期**（ae 的建议，采纳）：带基线时交接 files 里没有一条摘要等于本次项目实际字节，B 报 `handoff_target_not_matched` 提醒，告诉模型是交接摘要过期，而不是让它只看到一串 `baseline_*_changed` 去猜。
- **验证**：见 TESTS.md 同名节。

## 工具瘦身第一阶段：工具自己声明“默认收起” + 精简重复说明（T1，2026-10-02，分支 `claude/75-tool-default-defer`，基于 `claude/3a-step17e` `60f909b12`（原基于 `c6f28b150`），已实现，并入 step17e；开关默认关，真实验收后由 3a 在生产打开）

- **问题**：生产前台回合每轮把全部基础工具的原生 Schema 发给模型。实际发出的工具是 `model_visible_specs` 那一份（类别收起已生效，web 类的 watch_stream/web_fetch/web_search 本来就不发）；其中约 40 个工具占首轮约 3.65 万 token 的八成。一批又大又少用的工具每轮都在交钱。
- **做法**（不改名、不合并、不改任何工具行为）：
  - `ToolModelHints` 加两个软字段：`default_deferred`（工具自己声明默认收起）和 `deferred_summary`（收起后目录里的一句用途，声明收起时必填）。它们不进 snapshot_hash、tool_manifest 或授权。
  - 新开关 `tool_default_deferral_enabled`（`agent_config.yaml` + `AgentConfig`，默认 false）。打开后，ToolRegistry 把声明收起的工具并进“结构性收起”，与 `tool_catalog_deferred_categories`、决策点展示投影 `presentation_deferred_names` 取并集：可见 Schema、收起搜索范围、目录三处共用 `_extra_deferred_names`。
  - 保护：显式 `allowed_tools` 的回合不收起（与类别收起同口径）；本快照里 tool_search 不可用、模型不可见或被类别收起时不收起（宁可多发也不能藏到找不回）；发现入口 tool_search/list_tools/skill_search 永不收起。
  - 模型要知道它们存在：目录末尾的折叠提示里，声明收起的工具单列成“默认收起的工具：名字：一句用途”的索引，不受决策点短名单裁剪；用途句也并进关键词检索和语义检索文档（只对有用途句的工具，其他工具检索文本逐字不变）。开关关闭时提示词逐字不变。
  - 与能力精选（skill_tool 决策点）的合并：决策点默认候选只有 plugins 类和 Skill，和本批内置工具不重叠；并集不会打架。若管理员把某个声明收起工具的类别加进 optional_categories 且决策选中它，本轮它仍收起、需要一次 tool_search（已知取舍）。
- **收起名单**（按生产 local/main 近 30 天工具调用账，1205 个 run，只读工具名字段；括号里是调用次数/涉及 run 数和估算 token）：user_config（47/10，约 3.8k）、manage_models（65/2，约 1.9k）、update_persona（16/9，约 0.85k）、schedule（80/3，约 0.73k）、admin_controls（0，约 0.46k）、restart_gateway（1/1，约 0.29k）、gateway_status（32/7，约 0.28k）、watch_stream（1/1，本来按 web 类别收起，声明后不再依赖类别配置，并在索引里露出用途）。常用的读写文件、命令、派子代理（create_subagents 218/43）、进度（task_progress 854/204）、记忆（remember）、目标不收起。audit_records（24/4，约 1.1k）第一版收起过，MiniMax M2.7 真实验收里飞书普通用户说“查一下审计记录：我今天的请求有没有报错”时模型没去 tool_search、改读文件（I6，只发一次），改前对照同一句直接调用 audit_records 成功；它是 IM 用户自查“发消息报错/没回复”的入口，只省约 1.1k，所以不收起。
- **精简说明**（只删逐字重复，字段、类型、枚举、必填不变）：create_subagents 的 items[] 字段与顶层同义的改写成“同顶层 X。”（goal/description/role 本项专属说明，以及 covers 与第 14 条的 input_media_refs 完整说明保留，原文比引用还短的也保留；input_media_refs 只在 subagent_input_media_enabled 打开时出现，关闭时说明里没有它）；remember 批量项的 scope 改成“同顶层 scope。”；user_config 的各接入点 `points.*.profile_id` 改成“同 profile_id”，并删掉工具说明里与参数说明逐字重复的 reason/fields/probe 子句；task_progress 的 items 删掉与工具说明重复的返工句。
- **盲调兜底**（真实验收 D4 发现后补上）：M2.7 会照目录索引里的名字直接调用收起的工具、不先 tool_search，在没有参数定义时猜参数（audit_records 连错 3 次后改用 search_text 绕开）。现在 `tool_loop/deferred_schema_reload.reload_schema_after_blind_call` 只读结构化事实：开关打开、调用失败、参数出错（`tool_execution.failure_stage=validation`，或错误码的错误合同推荐动作是 `repair_tool_arguments`——工具执行阶段自己判参数无效也算，真实验收 D8 的 update_persona 就是这种）、工具在本快照里且下一次请求本来不会带它的定义，就把它加进 `loaded_tool_names`（与 tool_search 同一个“下一次调用临时可见”机制），并在结果后附一行 `[tool-schema-loaded]` 提示；模型下一步拿着原生完整定义重试。开关关闭时不触发。
- **F1（第 9 条）上界**：skill_tool 候选是 52 个 Skill（内置 27 + owner 25）+ 1 个插件组 = 53 题；内置工具不在候选里，所以本阶段瘦身不改变 Jev 请求。按 `jev_wire_bytes.v1`（C = ceil(B/2) + 256Q + 1024，上限 57,600）和标定数据（27 题约 65KB，每题约 2.4KB）估算：53 题约 7.8 万 token，仍超上限；按现有每题体积最多约 38 题，或每题文字压到约 1.6KB（约减三分之一）才进标定范围。要回到范围内需缩小参与实验的候选或精简 Skill 说明，属新设计项，交 3a/用户定。（已定：见本台账“F1 候选投影”一节。）

## 宿主死在“已预留、未激活”窗口里的子代理，重启后永久卡住（根修，I5 前提探针发现，2026-10-02，分支 `claude/9b-runner-admission`，基于 `claude/3a-step17e` `20125c9d2`，已实现，并入 step17e）

- **现象**（9b 探针，证据 `~/.my-agent/decision-evidence/i5-restart-pickup-probe-20261002/`）：派工已经预留执行轮、写下后台启动记录，进程在派工线程收尾前就没了（kill -9、停机时正落在这个窗口；进程内线程没有 pid，或 local/main 派工子进程的 pid 已死），启动记录冻在 launching/running。重启后：
  - 派工候选判定已按过期放行（没有 pid 的过 180 秒；pid 已死的立刻）；
  - 但 `_start_background_dispatch` 里的 `existing_runner_launch` 不判过期，把冻住的记录当成现存启动原样复用，回 started + reused_run_ids，实际什么都不派；
  - 孤儿监督每轮还把它计进 `orphans_revived=1`，推到一天后仍然如此——永久卡住，日志一直报假复活。
  - 同步派工路径（唤醒前预扫、watch、CLI 经 `collect_runner_candidates` → `reserve_runner_start(launch_id="")`）能接上，但只在相应事件发生时才走到。
- **做法**：
  - 过期判定只有一处权威：`subagents/process_control.background_start_record_stale`（pid 存活 > runner 会话心跳 > `BACKGROUND_START_STALE_SECONDS`=180 秒），派工候选 `runner/dispatch._background_start_active` 与重复投递复用 `runner_start.existing_runner_launch` 都直接调它。原先它在 `agent_core/runner/dispatch.py`，但 subagents 层不能导入 agent_core（`check_import_boundaries`），而它的输入（pid 判活、会话心跳）和启动记录的构造、回收本来就在 subagents，所以挪到这里，原处不留副本；`scripts/watch_harness/restart_recovery_harness.py` 的替换点随之改到新位置。
  - 过期的旧启动不算现存接纳，并在 creation 锁内先收掉旧记录（`lifecycle.update_background_start` 不许 replace_launch 覆盖仍是 launching/running 的记录）：
    - 受管模式：标成 reclaimed，随后 `reserve_runner_start` 沿用同一 pending attempt，launching 以 replace_launch 换成新启动记录；万一旧宿主其实还活着，它和新启动只有一个能在 RuntimeDB 原子激活。
    - 文件模式：`revoke_file_runner_start` 撤销旧预留（旧执行轮进放弃名单，旧宿主再也激活不了），随后重新预留新执行轮。
  - 计数：自动启动回执拆成“真派出去的”和“复用现存启动的”（`capability_auto_sweep._split_auto_start_result`）。监督摘要 `orphans_revived` 只算前者，复用另记 `orphan_launches_reused`；定向续派 `auto_start_orphan_run` 同样给 `started` / `launch_reused`。Gateway 监督日志仍只按 `orphans_revived` 触发。
- **验证**：见 TESTS.md“已预留未启动的子代理”节。

## Runner 启动边界统一读准入关门（I5，2026-10-02，分支 `claude/9b-runner-admission`，基于 `20125c9d2`，已实现，并入 step17e）

- **问题**：宿主停机关闭本进程模型调用准入后，已经排进批次的 runner 仍会激活执行轮，第一次模型调用被拒，写成 FAILED/model_call_admission_closed、白占尝试次数。75 复审 5a56714dc 建议在 `gate._counted_worker` 读关门事实；9b 核实发现只有一个任务的批次（单个孤儿复活、单个子代理自启，最常见）和 `runner_concurrency=1` 走 `_run_sequential_batch` → `run_single_runner`，不经过 `_counted_worker`。
- **做法**：检查点放在两条批次路的公共入口 `agent_core/runner/worker._run_subagent_worker`（`_admission_closed_refusal`）：
  - 关门时不建 worker、不激活执行轮、不写结果也不写 FAILED，不续派、不唤醒父级；返回只在内存里的拒绝结果（status=PENDING，`turn_end_reason=model_call_admission_closed`，`runner_last_error=MODEL_CALL_ADMISSION_CLOSED`）。`handle_runner_failure` 只处理 BLOCKED/TIMEOUT，所以也不注入失败记忆。
  - 预留原样保留（RuntimeDB pending attempt / 文件 background_start）：派工线程收尾成 finished 时，重启后孤儿监督第一轮就按同一执行轮拉起；进程来不及收尾、记录冻住时，由上一节的根修接上。
  - 这一处同时盖住 `runner_batches.collect_runner_candidates` 派出的批次、唤醒前预扫 `capability_auto_sweep._redispatch_stalled_subagents`、watch_service 和 CLI 派工——它们都经 `run_runner_batch` 进 worker。
  - 预览（dry_run）不发模型调用，不拦。
  - **local/main 的派工子进程**：它有自己的准入表，Gateway 关门不影响它，它也不随 Gateway 退出；这里对它没有影响，行为保持不变。
- **验证**：见 TESTS.md 同一节。没有做 sdkit 故障注入的真实 kill -9 演练（3a 列为加分项）。

## 语义记忆复审必须修：manage_models 不能把当前向量模型挪到别的主机（M1），自配只比对启动时的默认对话模型（S1）（be 复审 e75cf6061，2026-10-02，分支 `claude/38-semantic-memory-m1`，基于 `claude/3a-step17e` `3c960c1d9`，已实现，并入 step17e）

- **M1（必须修）**：同主机 `set_embedding` 之后，同一个 manage_models 能用 `save_provider(editing)` 改那个服务商的地址，或用 `save_model` 把档案挪到别的服务商；两步都不审批、也不再核对主机。重启后嵌入客户端就用新地址，提问和要现嵌的记忆正文都发到新主机。
  - 做法（3a 定做法 a）：manage_models 的目录写入，只要会改变当前向量档案（用户配置里的保存值、进程里的运行值都算）解析出的端点主机，就不写，回 `needs_user_choice` / `EMBEDDING_HOST_DIFFERS`，请用户去 /model 自己处理。“改变”包括改地址、挪服务商、删掉、从无到有（先在菜单里删了，再让模型用同一个编号在别处建）。
  - 实现：`model_profiles.model_profile_write_check` 写前检查作用域。`execute_model_profile_operation` 在同一把目录锁里、落盘前调用检查（写前快照 vs 写后快照），抛错就不落盘，没有“查完再写”的竞态，函数签名不变。检查本身是 `embedding_selection.catalog_write_check`，只比较 scheme + 主机 + 端口。
  - 管理员在 TUI / IM 里亲手改，不进这个检查（人的动作）。不影响向量档案主机的修改照常通过。
- **S1（一起修）**：三步绕过——先 `set_default` 换成另一家，再 `set_embedding` 另一家的嵌入档案，再 `set_default` 换回来。
  - 做法：`model_set_embedding` 比对的“默认对话模型主机”改成组合根启动时记下的那一份（`core._wire_memory_authorities` 调 `remember_startup_chat_host`，和嵌入客户端同一时刻定下，写在 `agent.embedding_chat_host_snapshot`），不再读目录当前值。
  - 同一次运行里先改默认再设向量会被拦；重启是用户的动作，重启后快照更新。没有快照或当时解析不出来，一律请用户自己选。
- **S2（记成后续项，这轮不做）**：候选、日事件、经验、整理事务备份这些文件也装着记忆正文，还是普通写入（没有 0600 / 0700）。要不要统一走私有写，后续定。（已做：见本台账“记忆正文的其它文件也按私有原子写（S2）”一节。）
- **验证**：be 的原样探针在本分支上，M1、S1 两条“绕过成立”的断言都失败了（save_provider 回 `EMBEDDING_HOST_DIFFERS`、嵌入地址没变；换默认后 set_embedding 回 `EMBEDDING_HOST_DIFFERS`）；用例与变异见 TESTS.md 同名节。

## 子代理被宿主停机打断时界面显示“宿主停机中断”（ae step17d 冒烟发现，2026-10-02，分支 `claude/9b-shutdown-label`，基于 `claude/3a-step17e` `2e5a36af0`，已实现，并入 step17e）

- **问题**：子代理被宿主停机打断后结构化状态是对的（`failure_type=host_shutdown_interrupted` 或 `model_call_admission_closed`，`current_step="宿主停机中断"`），但用户看到的是"失败"：
  - TUI 子代理状态条 `tui_block_renderer._subagent_status_display` 只按 status 映射，FAILED/TIMEOUT/CHANNEL_ERROR 一律"失败"（名册行与子代理页头部两处）；
  - `agent_activity._subagent_current_activity` 的终态文字也只按 status（FAILED→"执行失败"）；
  - IM 的 `/status` 只给"异常 N"。
- **做法**（一个概念一个权威位置）：
  - `subagents/runner_display_projection` 新增 `runner_failure_label(status, failure_type)`：失败类型的专属标签（宿主停机中断、额度不足、等待授权、结果格式异常），没有时为空、RUNNING 为空；`runner_display_label` 复用它。
  - Gateway 名册行 `_subagent_row` 带出标量 `failure_label`；TUI 三道白名单（runtime、view model、导航）放行，导航快照与渲染上下文带出，渲染缓存键随它变化。
  - TUI 徽标只在终态与等待处理分支用 `failure_label` 替换文字（图标、颜色仍按状态）；没有专属标签时照旧，其它失败仍是"失败"。TUI 不另写失败类型映射。
  - 终态活动文字：终态或等待处理时有专属标签就用它；排队重跑中的任务可能残留上次的失败类型，不套用。
  - IM `/status`：Gateway `_subagent_status` 把"异常"按 `runner_display_label` 细分（`ConversationTaskStatus.subagent_other_labels`），文字成"异常 3：宿主停机中断 2，失败 1"；没有异常时原文逐字不变。TUI 走 Gateway 时有界解析这项细分。
- **验证**：见 TESTS.md 同名节。

## 能力包 v2：宿主核验交付物、保护输入原件、要求交付（第 10 条选 A + 第 11 条，2026-10-02，块 1 已进 `claude/3a-step17e`，块 2 分支 `claude/ae-capability-packs-v2-b2` 基于 `2e5a36af0`，已确认设计、已实现，并入 step17e）

- **为什么做**：冻结重跑业务审阅只有 10/27。模型改写检查器后谎报“已验证”、就地改原输入、不交付，宿主都看不见；检查器也漏了占位、编造引用 ID、整理变改写等可以确定性查出的问题。
- **设计**见 [CAPABILITY_PACKS_V2](docs/design/CAPABILITY_PACKS_V2.md)，三样通用机制都由包的结构化声明触发：
  - **第 11 条**：宿主在 `AttemptExecutionSandbox` 里跑按 sha 钉住的原版检查程序（断网、目标只读、只写临时目录，没有沙箱就不跑），写完就查加收尾再查，有错误返工 1 次，结论用 HostNotice 给出；
  - **输入保护**：首读记基线、存不超过 1 MB 的原件副本，原件被就地改就返工 1 次；
  - **交付存在**：缺失或打不开返工 2 次。
  - 开关 `capability_pack_host_verification_enabled` 仓库默认 false。
- **块 2 已实现**：`capability/pack_verifier_runner.py`。它只从安装 blob 取钉住的原件，在 `AttemptExecutionSandbox` 里跑（断网、整根只读、只写临时目录），只认 `pack_verifier_result.v1`。`AttemptSandboxSpec.network_access=False` 在 macOS 补了 `(deny network*)`，就绪检查会实际试一次断网，失败就不跑。macOS 和 Linux 车道都实测过，车道要加 `NET_ADMIN` 才能真跑断网用例。
- **块 5 已实现**（与块 4 同在分支 `claude/ae-capability-packs-v2-b45-17h`，基于 `claude/3a-step17h` `7e421024f`；旧分支 `b5`、`b5h3` 作废）：
  - 只在本回合改过工作区（有块 3 基线）时检查钉住包的必需交付物（3a 定）；
  - 缺了记 `DELIVERABLE_MISSING`，路径匹配但打不开记 `DELIVERABLE_UNREADABLE`，最多返工 2 次；
  - 纯问答和只读审稿回合不触发；不调任何工具、直接在答复里贴内容的回合查不到，写进已知限制。
- **块 4 已实现**（分支 `claude/ae-capability-packs-v2-b45-17h`，基于 `claude/3a-step17h` `7e421024f`，含 be 的 H3 全部提交；旧分支 `b4r`、`b4h3` 作废）：
  - 输入原件清单在任务第一次改工作区前记一次，范围是已启用、声明了核验的包的声明模式（3a 同意的偏离：原设计是“首读记”）；
  - 不超过 1 MB 的原件存副本；原件在本回合被改或删就记 `INPUT_MODIFIED_IN_PLACE`，返工 1 次，提示里给出副本位置和 cp 恢复建议；
  - task_input 改为只从原件清单找；
  - 宿主托管文件（账本、清单、副本）统一落在规范任务根的 `data/pack_verification/`，对模型只读靠 be 的 H3。细节见设计文档第 4 节。
  - 9b 的开关前提 (b) 已有回归用例。原先的结构性缺口（命令工作目录和写根都在任务树外时账本目录不在只读覆盖里）已由 be 的 H3 修复，用例去掉了 strict xfail。
- **块 3 已实现**（分支 `claude/ae-capability-packs-v2-b3-17f`，基于 `claude/3a-step17f` `f6b63ab35`）：
  - 本 run 第一次改工作区前记基线；写工具成功后马上检查，回执附有界摘要；收尾时对本回合新建或改过的交付物再查（shell 写的也算），有错误返工 1 次；
  - 结果、返工次数都记在每 run 一本的核验账本，Compact 和重启后不重置；
  - 回合结束发宿主提示（`source=pack_verification`），并写 `channel_delivery.pack_verifications`；
  - 开关 `capability_pack_host_verification_enabled` 默认 false，是管理员边界项。细节见设计文档 3.1 节。
    生产打开这个开关之前必须满足 9b 复审定的两个前提：（a）be 的 H3 已合入，核验账本所在的 `<任务根>/data/pack_verification` 在受保护写路径里；（b）块 4 有回归用例证明主代理和子代理、Shell 和文件工具都写不进 `<任务根>/data/pack_verification`；（c）块 6a 已合入：用户按 /stop 时，正在跑的检查程序会被打断、剩下的不再起（3a 10-03 定，见设计文档块 6 方案）。
- **声明协议调整（块 2 内，ae 定，3a 已审同意：旧 baseline 键直接删、不做兼容；必需输入找不到就不跑、不返工）**：检查程序的 `baseline` 换成 `inputs` 列表（最多 4 条，每条 `{flag, source, path_patterns, field_match?, required}`）。起因是 be 对齐包内容时发现，A 的检查器要另一种 schema 的原文，B 要本回合写出的交接文件，只有一个同交付物基线不够用。来源 `task_input`/`turn_output` 是开放字符串，恰好匹配一个才算找到；必需的找不到就不跑（`verifier_input_unresolved`），非必需的找不到就不传。`valid` 必须等于“errors 为空”，退出码不参与判定。
- **块 1 已实现**：
  - v7 可选块 `capability.verification`（交付物按路径模式加可选结构化字段识别，格式、运行方式、输入策略都是开放字符串）；
  - 声明了检查程序的包启用前要确认码，同意摘要写进内容激活，为空时旧记录与代次逐字节不变。
  - 测试见 TESTS.md 同名节。
- **分工**：宿主线（块 1–6）ae；包内容 A 0.5.0、B 0.3.0（块 7）be，ae 审；重跑和独立审阅（块 8）ae。验收按 C16：某个包冻结重跑 9/9 才装进生产。

## 隔离模式的 Shell 碰到沙箱边界时让模型知道并换个做法（第 16 条，用户 10-02 拍板，2026-10-02，分支 `claude/9b-sandbox-boundary-facts`，基于 `claude/3a-step16z` `3c7565bf2`，已实现，并入 step17e）

- **问题**：owner 隔离（普通 IM 用户都是这个模式，本机管理员偶尔也是）的 Shell 跑在系统沙箱里。macOS 上递归扫描等操作碰到被拒读的目录会报 EPERM 中途退出（祖先目录元数据已放行，见下方"第一步的回归与热修复"），模型只看到命令失败，不知道是沙箱边界，常见反应是换个写法原样重试。
- **做法**（只用结构化事实，不解析 stderr，不放宽沙箱）：
  - 回执信封 `result_envelope.sandbox` 在 owner 隔离时新增 `sandbox_active=true` 与 `allowed_roots`（`read_write` / `read_only`）。
    - 数据来自本次启动沙箱用的同一份根：`request.sandbox_roots`（写根为空时沙箱把本次工作目录当可写根，这里同样投影）与 owner home。
    - 拒读根（my-agent 数据根、用户家目录）不列出，不把宿主结构交给模型。
  - 命令真的在沙箱里跑起来并以非零码退出时（`COMMAND_FAILED` 且 `process.status=exited`、带退出码）再加 `boundary_hint`：`may_be_sandbox_boundary=true` 与建议码 `limit_to_allowed_roots` / `request_capability` / `ask_user`。只说"可能"，由模型结合输出判断；超时、取消、沙箱不可用、起不来都不带。
  - `tooling/runtime_facts.render_tool_runtime_facts` 在带 `boundary_hint` 时多渲染一节 `[runtime-sandbox-facts]`：允许目录、下一步建议的中文说明与 JSON。成功的命令不渲染，不增加 token。
  - `run_command` 说明的提示里多一条静态边界提示（不含具体路径），只在 owner 隔离且开关打开时出现。
- **开关**：`shell_sandbox_boundary_facts`（默认开），会改变模型看到的内容，按规矩可关；关掉后回执与说明都恢复旧样子。经 `AgentConfig` → `ToolRegistryParams` → `ShellToolOptions` 传到工具。Full Access 不受影响。
- **下一步怎么走**：主代理直接告诉用户需要什么访问权限（用户可经 `/permissions` 等宿主入口决定）；子代理用已有的 `capability_request` 申请；或把范围缩到允许目录。安全边界不变。
- **验证**：见 TESTS.md 同名节。真实 MiniMax-M2.7 验收（TUI 本机管理员 + 假飞书私聊普通用户经真实 Gateway，证据 `~/.my-agent/decision-evidence/sandbox-boundary-facts-bbd6bef1e/`）：两边越界的 `du` 都在沙箱里非零退出，模型看到的工具结果带着边界事实；TUI 随后换了一条命令成功，IM 没有再原样重试 Shell 而是收尾答复。另记一条发现："统计 .md 文件"这类需求模型优先用专用文件工具（由文件工具自己的 owner 墙管），不走 Shell，本条功能不触发。

## 语义记忆整包：/model 向量模型入口、my-agent 同主机自配、向量文件私有写、第一次召回不重嵌（S1–S5，用户 10-02 拍板，2026-10-02，分支 `claude/38-semantic-memory`，基于 `claude/3a-step17d` `3c7565bf2`，已实现，并入 step17e）

- **背景**：用户拍板生产要开语义召回（MiniMax embo-01；真正打开是 S6，由 3a 发版时做）。原来只有管理员在 `/settings` 里填档案编号，模型改不了；向量文件按 644 写；第一次召回会把已存的记忆全部重嵌一遍。
- **查清的事实（S4）**：`embedding_model_profile` 和 `memory_semantic_recall` 是**全局配置**。owner 池建作用域 agent 时只换 owner 三字段（`owner_scoped_pool._config_with_owner`），其余原样继承 Gateway 配置。
  - 全局档案编号指向管理员目录里的档案，普通 owner 的目录里没有它 → 语义通道关闭、只走关键词，诊断 `MEMORY_EMBEDDING_PROFILE_UNAVAILABLE` / `profile_not_found`；
  - 全程只读普通 owner 自己的目录、不建嵌入客户端，绝不会拿管理员的密钥去嵌普通用户的记忆（已钉用例）。
- **入口（S1）**：唯一入口 `settings/embedding_selection.py`。
  - TUI：`/model` →「选择模型」→「向量模型」（本人目录里用途为 embedding 的档案 +「关闭」），本地与 Gateway 同走 `embedding_list/select/off`；
  - IM：`/model vector` 查看、`/model vector <编号|档案编号>` 选用、`/model vector off|关闭` 关闭；TUI 键入同一命令时原样保留 vector 子命令；
  - 写入走参数中心 `set_parameter`，包在 `user_settings_write_scope` 里（与管理员 `/settings` 同一边界授权，只写这两项），每项各记一笔账；
  - 选中先写档案再开召回；关闭先关召回再清档案（显式写 `""`/`false`），任一步失败都停在“不会把记忆发出去”的一侧；
  - 保存后重启 Gateway 才生效（嵌入客户端在组合根构建），菜单、回执和列表都写明，列表同时给出保存值、运行值和 `restart_pending`；
  - 只给身份完整的本机管理员（local/main），普通 owner 一律 `PARAMETER_BOUNDARY`、不写文件；`tool_vector_search_enabled` 不跟着变；仓库默认不变（召回关、档案空）。
- **my-agent 自配规则（S2，用户 10-02 拍板，3a 同意最小做法）**：`manage_models` 加 `set_embedding` / `disable_embedding`。
  - 只给本机管理员；目标必须是**本人目录里**用途为 embedding 的档案（`shared:` 引用、不在本人目录的编号按 `TOOL_PERMISSION_DENIED`）；
  - 端点主机（scheme + 主机 + 端口，去掉账号口令、补齐默认端口，不看路径）与本 owner 默认对话模型相同 → 直接写；
  - 主机不同，或默认对话模型本身解析不出来 → 不写，回 `needs_user_choice` + `EMBEDDING_HOST_DIFFERS`（新登记，恢复动作 request_user_input），请用户自己在 `/model` →「选择模型」→「向量模型」里选；
  - 其余情况照旧 `PARAMETER_BOUNDARY`；参数中心写入失败按 `TOOL_EXECUTION_FAILED`、原码进 `reported_error_code`，副作用未知交给对账；
  - `BOUNDARY_KEYS` 里 `embedding_model_profile` 的说明和 `user_settings_write_scope` 的注释同步写明这条唯一例外；
  - `list` 回执附带 `semantic_memory` 视图（只给本机管理员）：运行值、保存值、是否等待重启、可选嵌入档案和下一步提示；工具说明写明“用户要求开/关语义记忆就是授权，直接调用”。由来见下方真实核对。
- **向量文件（S3）**：
  - 记忆本体、`memory_vectors.json`、`memory_text_vectors.json` 都按私有原子写：临时文件一建出来就是 0600，替换时不抄回旧文件的 0644；`long_term` 目录收紧到 0700（尽力而为）；
  - 第一次召回先复用 `memory_vectors.json` 里**身份一致、文本完全一致**的向量（写入与重建都按 `index_text` 存同一段文本），剩下的才查正文哈希缓存，再不行才嵌。
- **真实核对**（隔离 home、127.0.0.1:8441、env -i 假 HOME；模型目录副本只含 MiniMax 官网服务商并选 M2.7，经产品“新增模型”入口加 embo-01，0600，用完连同 home 删除；决策模型关、审批 auto；每次只发一次“帮我开语义记忆”）：
  - 提交号说明：核对时分支基于 `e7e6c4563`，之后变基到集成头 `3c7565bf2`（只多两个无关测试文件的末尾空行修正），代码不变；`f95a38b40` → `214465417`，`8eb9f8771` → `5236901ea`。
  - 第 1 次（`f95a38b40`）**未通过**：模型调 `list` 后直接回答，没调 `set_embedding`，配置没变（58,938 tokens）。契约缺口：`list` 看不到语义记忆现状和下一步，说明里“请用户去 /model 选”太显眼。修法即上面的 `semantic_memory` 视图与说明（`8eb9f8771`，变基后 `5236901ea`）。
  - 第 2 次（`8eb9f8771`）**通过**：模型 `list` → `set_embedding`（embo-01，ok）→ 自己调 `restart_gateway` 安排了安全重启（120,244 tokens）。配置读回：档案 = embo-01、召回开、`tool_vector_search_enabled` 未动；账本 2 笔 actor=model。重启后 TUI `/model vector` 显示“当前运行：档案…，语义记忆开”；新组合根探针真实调用 embo-01：写 1 条嵌 1 次，第一次召回检索方式 semantic、只嵌查询 1 条，`memory.jsonl` / `memory_vectors.json` 0600、目录 0700。
  - 证据：`~/.my-agent/decision-evidence/semantic-memory-20261002/`（run1、run2 各有 SUMMARY.md 和结构化 JSON，不含正文和密钥）。
- **S6 注意（3a 发版时做）**：goal 第 5 条要求生产里工具语义检索保持关。仓库默认 `tool_vector_search_enabled: true`，以前只是因为档案为空才没生效；生产 desktop.yaml 没有覆盖这个键（只用 grep -c 核对）。所以开生产语义记忆时要先 `/settings set tool_vector_search_enabled false`（参数中心可写，重启生效），再选向量模型，两项共用一次重启。本分支不动这个开关。
- **观察（已处理）**：`enable_gateway_restart_tool` 的配置注释原写“其它模式按危险动作审批”，实际 auto 模式下非 always 的危险动作不逐次询问（`action_policy._approval_decision`），第 2 次核对里重启没弹确认。3a 定：以代码行为为准改注释（ask 弹确认，auto/完全放行不弹，安全重启先等空闲），在 I3 分支 `claude/38-late-tool-fence` 里改了配置注释、工具模块头和 GATEWAY_SAFE_RESTART.md。生产 S6 由 3a 自己控制重启。
- **后续项（未做）**：
  - 普通用户各自开语义记忆：要有 owner 级配置、各自的嵌入档案和费用归属；现在普通用户只按关键词召回。
  - `tool_vector_search_enabled` 默认开：选了向量模型后工具检索也会用同一档案发嵌入请求。要不要和语义记忆一起管，待定。
  - TUI「新增模型」只按接口推出用途（聊天接口一律 agentic），加嵌入模型要再改连接（勾 Embedding）和模型（用途 Embedding）两处；manage_models `add` 带 `capability=embedding` 可以一步加好。要不要在「新增模型」里直接给用途选项，待定。
- **验证**：见 TESTS.md 同名节。

## 记忆整理：同一条消息里同主题、不同内容各存一条（用户拍板第 6 条，2026-10-02，分支 `claude/be-curator-content-identity`，基于 `claude/3a-step16z` `5e972003e`，已实现，并入 step17d）

- **问题**（J13 复测登记，见下方 J13 条目里的“待定”）：用户说“我对花生过敏，也对芒果过敏。”，模型给出两条候选，主题键都是 `health.allergy`。宿主的观察身份不含内容，两条算出同一个观察键；芒果那条在候选库合并时按观察键找到花生那条，被当成同一次观察的改写并进去，候选库只剩“花生”。
- **是哪一步合的**：不是模型输出合的，是写入时合的。
  - `curator_validation._canonical_observation_id` 只用来源（消息/工具/产物/任务/运行）加类型化主题（类型、主题键、范围、动作、目标、晋升目标）算哈希，不含内容；Curator 把它填进每条观察的 `observation_id`。
  - `candidates.merge_candidate_observations` 先按候选编号找（候选编号本来就含规范化内容，花生和芒果编号不同），找不到再按观察键找；芒果按观察键命中花生那条，`_merge_candidate` 保留原内容，只加来源。
- **做法（用户拍板：按内容区分，真正重复的照旧合并，按结构化字段，不看关键词或文案）**：
  - 观察身份的哈希材料加 `content_hash = memory_content_hash(content)`。它和 `stable_candidate_id` 用同一套正文规范化（`normalized_memory_content`：`fold_key` 加空白折叠），身份里只放哈希、不放正文。
  - 同主题、内容不同 → 观察键和候选编号都不同 → 各存一条；规范化后内容相同（大小写、多余空白不同）→ 同一观察，照旧合并，出现次数不增加。
  - 候选库的合并规则、`_same_candidate_identity` 和 J13 的冲突剔除都不改。
- **代价（用户已知并接受）**：从结构上分不开“对同一件事换了说法”和“同主题的另一件事”。所以同一来源、同一主题换了说法时，现在会各存一条：
  - 同一批里换说法：两条候选；
  - 安全重放（上一批没声明已处理的消息在下一批再整理一次）且模型换了说法：多一条候选。原样重放仍是同一观察。
  - 不会虚增出现次数：候选编号本来就含内容，换说法的那条是另一条候选、出现次数从 1 起，原候选的次数不变。重复候选照常走晋升审核。
  - 部署边界（3a 集成审查补记）：观察键公式变了，上线前已提交、上线后又被安全重放的同一观察，会按新键再记一次出现（`occurrence_count` 是观察键个数，+1），只影响跨部署的那一批。按铁律不加旧公式兼容；晋升仍要过审核。
- **真实核对**（隔离 home、官方 DeepSeek flash `ff961d14` 档案副本 0600 用完删、1 次调用约 8 秒、提示 5,807 字）：合成消息“我对花生过敏，也对芒果过敏。另外我每天早上只喝一杯美式咖啡，不加糖。”整理成功，3 条候选全部入库、出现次数各 1。
  - 这次模型自己把主题键分成了 `user.allergy.peanut` 和 `user.allergy.mango`，没走到同主题这条路径。所以这次只证明新身份规则下整条链路照常工作；同主题不同内容的路径由用例覆盖（真实候选库和提交链路、脚本化后端）。
  - 证据：`~/.my-agent/decision-evidence/j13-curator-retest/content-identity-20261002/`。
- **验证**：见 TESTS.md 同名节。

## Esc／/interrupt 中断后过一会儿再发消息仍接着原任务：账本自愈认出"用户中断"（第 7 条，用户 10-02 选 A，2026-10-02，分支 `claude/9b-interrupt-resume`，基于 `claude/3a-step16z` `5a56714dc`，已实现，并入 step17d）

- **问题**（38 在 C12 观察里报告，见下方 C12a/C12b 条目的待定项）：
  - TUI 里 Esc 与 `/interrupt` 同一入口，只中断本轮，任务关联保持 active。
  - 约 2–4 分钟后发现层账本自愈（`owner_wake_discovery._project_task_ledger_terminal`，operator `wake-discovery-ledger-heal`）看到"主执行轮已 cancelled、关联还 active"，把任务收成 cancelled、关掉 TaskRun。
  - 之后同会话的新消息开新任务目录，不再接着中断的任务；只有中断后马上发消息才会接着做。用户的习惯是 Esc 后过一会儿发"继续"。
- **为什么不能只看运行库的原因码**：`/interrupt` 与 `/stop` 打断执行循环后落的是同一组 `runtime_reason=user_stop`、`runtime_source=conversation_control`，运行库本身分不出两者。
- **为什么不把关联改成 interrupted**：Gateway 选本轮会话工作区任务时只认精确绑定的请求、未完成 Goal 和 active 任务（`request_context._gateway_workspace_task`），`completed/interrupted` 粘性任务"不能偷偷取得新任务的执行选择权"，改了反而接不上。
- **做法**（只认结构化事实，不看用户发的文字）：
  - 新模块 `runtime_db/user_interrupt.py` 是唯一事实源：中断真的打到一轮时，给被中断的主执行代次追加一条 runtime_events `agent_run.user_interrupted`（payload：task_id、来源、窗口秒数）。
    - Gateway 入口：`control_service._stop_active_task(interrupt_only=True)` 在确认中断到一轮之后调 `_record_user_interrupt`，按任务取主执行轮当前代次；
    - 本地 chat 入口：`control_runtime._execute_local_stop` 的中断分支用 `LocalRunControl` 已发布的精确身份记。
    - 记录失败只退回旧行为，不影响中断本身；没有运行中回合的空闲 `/interrupt` 不记。
  - 账本自愈 `_filter_by_runtime_authority`：主执行轮 cancelled 时，先看当前代次（`current_attempt_id`）有没有这条事件、是否还在窗口内。
    - 在窗口内：不投影、不写 status_conflict 诊断、也不驱动；窗口截止时刻进 `deadline_out`，发现判定缓存到点重算。
    - 事件绑定精确代次：用户再发消息开出新代次后自然失效，新代次被别的原因取消时照旧收口。
  - 用户再发消息：关联仍是 active，Gateway 照常选中原任务，同一任务目录、同一主执行轮开新代次（与"中断后马上发消息"同一条路）。
- **过期规则**（用户不回来时不能永远挂着）：窗口 `USER_INTERRUPT_RESUME_SECONDS` = 24 小时，从中断事件写入时刻算。过了窗口，账本自愈照原规则把关联记成 cancelled、任务账本 CANCELLED、关 TaskRun，审计原因记 `user_interrupt_expired`（普通陈旧账本仍是 `terminal_run_ledger_stale`）。
  - 窗口长度是模块常量，事件里同时记下当时的窗口秒数；发现层按设计不读配置，所以没有做成配置项。3a 集成时定：24 小时、模块常量、不做配置。用户的场景是“按 Esc 去吃饭、隔夜回来发继续”，24 小时够用；要改再议。
- **`/stop` 不变**（C12 那组不退回）：
  - 窗口内关联仍 active，`/stop` 走正常任务停止：关联改 interrupted、冻结并回收本任务的后台进程与子代理、暂停目标（与"中断后马上 `/stop`"同一条路）。
  - 过了窗口自愈收口后，`/stop` 走 C12a 的会话遗留资源回收。
  - 中断本身仍不碰资源；过期收口也不自动杀进程（与原自愈一致），遗留进程照旧由 `/stop` 或 `gateway stop --stop-background` 收。
- **影响**：被中断任务在窗口内保持 active、TaskRun 保持打开，期间不会被自动续跑（运行库权威终态照旧排除驱动）。窗口内用户发的下一条消息，无论内容，都接着原任务目录，与原先"中断后马上发消息"的行为一致。
- **验证**：见 TESTS.md 同名节。真实 MiniMax 链路未跑（组件 + 真实 Gateway ask 加脚本化假模型已覆盖），可在集成验收时补一轮。

## 自然停机带走的子代理重启后按“宿主停机中断”收尾（step17c 停机预演观察 1，2026-10-02，分支 `claude/38-shutdown-label-v2`，基于 `claude/3a-step16z` `5a56714dc`，已实现，并入 step17d）

- **问题**（ae 观察 1）：自然停机时，网关进程里的子代理是守护线程，随进程消失，碰不到关门后的拒绝。重启后宿主把它收尾成 `runner_error`（“执行器已退出但没有返回结果”），用户看不出原因是停机。
- **为什么不用网关停机事件判定**：3a 举的例子是 `gateway_model_calls_interrupted` 事件和账上的中断码，但都不好用。
  - 账本在内存里，重启就没了。
  - 停机事件写在 Gateway 主 agent 的本地库里，而 local/user 这类 owner 的子代理由各自 owner 的 agent 做重启收尾，读不到那份库。
  - 按“调用编号 = attempt 编号”去匹配，依赖的是一个约定。
  - 子代理停机时可能正在跑工具而不是在等模型，这时根本没有在途调用可匹配。
- **做法**：结构化事实写在这个 run 自己的 attempt 上。
  - Gateway 收尾在关门结清之后调用 `runtime_db/executor_liveness.mark_in_process_executors_host_shutdown()`：给本进程仍在执行区间里的每个子代理执行器，在它自己的 attempt 元数据上按 token CAS 写 `executor.host_shutdown`（pid + 时间）。
    - 不改 status，不判死。线程如果还来得及自己收尾，就照常写退出和结果。
    - 子进程 runner 不在本进程的执行区间表里，不受影响。kill -9 这类非正常退出走不到这里，不会有记号。
    - 打上记号的个数写进收尾载荷 `host_shutdown_executors`。出错只记 `gateway_executor_shutdown_mark_failed{error_type}`，不中断收尾。
  - 重启收尾：`exited_attempt_facts` 带出 `host_shutdown`，`recover_exited_runner` 按优先级选失败类型：
    1. 有未确认的工具效果：仍然是 `executor_effects_unknown`，先核对；
    2. 有停机记号：新失败类型 `host_shutdown_interrupted`，界面显示“宿主停机中断”；
    3. 其它：照旧 `runner_error`。
  - `host_shutdown_interrupted` 登记了错误码 `HOST_SHUTDOWN_INTERRUPTED`（不可重试），不进自动重跑名单。
    - 重启后不自动重跑，父级按 recovery_decision 用同一 run 续派，不改“不自动重跑”的取舍。
    - Audit 来源例外：和 `model_call_admission_closed` 同属 `HOST_SHUTDOWN_FAILURE_TYPES`，结果归并改回 PENDING 时保留原因，重启后接续。
- **验证**：见 TESTS.md 同名节。

## 第 14 条：派子代理时把图片一起传过去——按 media_ref 结构化引用，走现有附件/媒体管线（2026-10-02，ef，分支 `claude/ef-subagent-media`，基于 `claude/3a-step16z` `5e972003e`，已实现，并入 step17e；默认关）

- **问题**：J11 让主会话带图时按模态自动选模，但父代理用 `create_subagents` 派活没有入口把本轮图片交给子代理，子代理带图的真实链路没有入口。
- **做法**（开关 `subagent_input_media_enabled` 住在 capability 配置 `config/capability_config.yaml` + `capability/config.py`，
  仓库默认 false，打开由 3a 验收后决定；运行时三处读法都走 `capability_config_for_agent`，只认 True，主配置里没有这个键；
  它在 `USER_SETTINGS_BOUNDARY_KEYS` 里：模型不可写，管理员 `/settings` 可开关）：
  - **引用 = 附件内容哈希**。开关打开且用户轮带 typed media 时，`runtime/loop_support._with_input_media_manifest` 在当前回合初始 IR 末尾追加一条
    宿主事实 `RuntimeFactsTurn(source=input_media_manifest)`：`[INPUT_MEDIA_MANIFEST]` + JSON（每个附件 `media_ref`=sha256、name、media_type、
    size_bytes，不含路径）。放在开头项/交接/插话之后，不动 `_current_turn_opener_count` 的位置约定；主会话与子代理同一入口，
    所以 child 也能把图再派给孙代理。它是模型可见请求内容的变化，故受开关控制；关闭时逐字节不变。
  - **工具合同**：`create_subagents` 顶层与 `items[]` 多 `input_media_refs`（字符串数组）；说明与 schema 由 `CreateSubagentsTool.__init__`
    按本 agent 开关决定（`build_create_subagents_model_spec(input_media=True)`，只认 `True`，MagicMock 不算），关闭时与原说明逐字节一致
    （schema_hash 相同）。`tool_spec_data.py` 仍 ≤120 行。
  - **宿主解析（唯一入口 `orchestration/input_media_refs.bind_subagent_input_media`，挂在 `create_policy.create_task_attributes`，根与递归共用）**：
    先清掉模型塞进 `attributes.input_media` 的同名值（host-owned 键）；引用格式必须是 64 位十六进制；先在父级当前回合的
    `task_attributes.input_media`（主会话由 Gateway 校验后写入，child 是自己首轮的属性）找，没找全再按父级 transcript
    （child 用 `agent_thread_id`，主会话用 `conversation_thread_id`）用 `messages.visit_all_report` 顺序扫 canonical 行里顶层用户
    `local_file` 媒体块；不查别的线程、不按 owner 附件根里“文件存在”放行。找到的引用用 Gateway 同一函数
    `validate_input_media`（owner 附件根、`input_media_max_bytes/files`）重验，写进 child 任务属性 `input_media`——与主会话同键。
  - **拒绝**（创建任何 run 之前，整批 `not_started`）：开关关 → `SUBAGENT_INPUT_MEDIA_DISABLED`（不可重试，结构化开关事实）；
    格式错/重复/找不到/超限 → `SUBAGENT_INPUT_MEDIA_INVALID`，回执 `invalid_media_refs` 逐项 `reason_code`（malformed/duplicate/not_found/limit）。
    两个码登记在 `error_taxonomy.py` 末尾独立块。
  - **child 侧零新路径**：`context_bundle_refs.runtime_task_attributes` 原样投影 `input_media` → runner 的 `_native_turn_opener` 把它挂到
    `UserTurn.media` → 适配器渲染 `local_file` 块 → 发送边界展开 base64。首轮选模（`subagent_model` 点位、首请求模态过滤）读的是同一份
    冻结请求，J11 的 `candidate_input_modality_decision` 自然看到 image：不声明的候选不适用，全无兼容候选保留原模型并记结构化原因。
    child 线程 canonical 行保留 `local_file` 引用，压缩侧 `media_archive_facts` 统计得到；恢复/续派重新从任务属性投影，
    文件被删或改写按确定失败（`InputMediaError`）整批拒绝，与主会话一致；`replacement_for_run_ids` 接管的新 child 由父级重新点名引用。
- **边界/未做**：没有为“图片”写专项分支，按 `media_type` 的 image/video 声明处理；视频同样走这条路；不放宽跨模型 reasoning 限制；
  被 `project_input_media` 按预算归档或被压缩摘要掉的历史附件，模型手里可能没有 `media_ref`（归档占位文字只给名字/路径），
  这与主会话重新添加附件的现状一致，本轮不改占位文字。工具结果里现在没有图片通道（ToolResult 只有文本），J16 截图应复用
  `UserTurn.media` 这条通道。
- **验证**：见 TESTS.md 同名节；真实 MiniMax M3 隔离核对已做一次（父代理按 media_ref 派工、子代理首请求带图并正确描述形状颜色），
  证据 `~/.my-agent/decision-evidence/subagent-media-8378ff9a9/`。
- **开关位置修正（2026-10-02，3a 车道 `test_config_normalize` 抓到）**：首版把开关放进了主配置 `agent_config.yaml`，违反 AGENTS.md
  “子代理、skill/tool 授权、能力上抛相关参数不进主配置”的规矩（该用例钉住主配置允许出现的 `subagent_*` 键）。已按 C4 的
  `subagent_takeover_hint_enabled` 挪到 capability 配置：主配置/`AgentConfig`/bool 名单删键，`CapabilityConfig` + 随包
  `capability_config.yaml` 加键（中文注释），三处读法改 `capability_config_for_agent(agent).subagent_input_media_enabled`，
  并加进 `USER_SETTINGS_BOUNDARY_KEYS`。行为不变；真实 M3 核对是在挪位置之前做的，当时开关在主配置，判断逻辑相同。
- **3a 集成补**：ef 的 M3 真实核对里，子代理先说“没有解码附件的工具”才去看图。清单在标记行后加一句软提示“这些附件已作为本轮消息里的图片/视频直接给你，可以直接看，不需要工具读取或解码。”（只是提示，不参与任何判断）。

## 记忆整理跟着消息来源会话的主代理模型走（用户拍板第 2 条细化，2026-10-02，分支 `claude/be-curator-thread-model`，基于 `claude/3a-step16z` `5a56714dc`，已实现，并入 step17e）

- **用户原话**：“那不能按主代理的走吗？我可能开 5-10 个 tui，普通用户就 1 个飞书或者别的 IM。”
- **之前**：`memory_curator_model_profile` 留空时，整批都用 owner 当前选中的模型，后端在建 agent 时一次性建好。
- **现在**（留空时；指定了档案仍固定用它，和以前一样）：
  - 模型身份只读结构化事实：组合根经 `settings/curator_profile.curator_thread_profiles` 直接 `threads.load` 读 `ConversationThread.model_profile_id`，不走会写默认值的 `thread_model_profile_id`。
  - 会话没选过模型 → owner 默认；选的档案解析失败（`ModelProfileError.reason`，如 `profile_not_found`）、目录读不了（`catalog_unreadable`）、连接字段缺（`model_configuration_missing`，如订阅登出）、后端建不出来（`backend_unavailable`）→ 退回 owner 默认，原因按条数记进运行记录 `curator_thread_model_fallback:<原因>:<条数>`。
  - 一批里按生效档案分组，每组是一次完整运行（租约、提取、校验、一次事务提交都沿原合同），一次触发依次跑完（最多 `CURATOR_MODEL_GROUP_MAX_COUNT`=8 组）。
    - 组的顺序：批内第一条消息（按会话最近更新时间从早到晚收集）所在的组先跑。
    - 游标只推进本组会话；工具审计事件只跟 owner 默认那组走（审计游标是全局连续前缀，拆开就推不动）。默认组没有消息但有审计事件时，最后单独跑一次。
    - 某组没成功就停在那组：已提交的组不会重复落账，失败组游标不动、按原规则安全重放。
  - 会话自己的模型在提取或校验阶段确定性失败（`CURATOR_SCHEMA_INVALID`，比如不擅长严格 JSON）时，本次运行用 owner 默认模型补跑一次（3a 建议）。
    - 这时还没写任何东西，幂等不受影响；成功就照常提交，运行记录写实际用的默认模型，并加 `curator_thread_model_failed:<码>`。
    - 补跑也失败就只记这一次运行失败：熔断按运行计，不按尝试计。
    - 网络、额度、超时、提交失败都不在同一次运行里补跑。每组每次最多补跑一次。连接类失败连续 2 次后，下一次运行直接用默认模型；上一批就是这样补跑推进的，新一批失败 1 次就换（见“会话自己的模型连续连不上时让给 owner 默认模型”与“记忆整理补跑后续”条目）。
  - 熔断仍是 owner 级（退避和熔断码与以前相同），但“同一输入”的判断改成按组：只看同一模型的历史运行、游标投影到本组会话（默认组再加审计游标）；没推进本组游标的成功不打断计数。所以别的组夹在中间成功，也不会让一直失败的组无限重放。
  - 运行记录与返回结果的 provider/model 写本组实际用的模型；多组时返回汇总结果（计数相加，状态取最后一组），加 `curator_model_groups:<组数>`。
- **每个 owner 在自己的目录里解析**（3a 要求，step17c 背景：step17b 时全局指定的档案只在 local/main 的目录里，别的 owner 解析成 `profile_not_found`，整理一直不跑，日志 257 条）：
  - 会话模型和 owner 默认模型都用该 owner 的 agent（`agent.home_paths`）经 `selected_model_config` 解析，管理员共享给它的（`shared:<编号>`）和管理员指定的初始模型都算；解析不到按上面的结构化原因退回。
  - owner 默认模型和主代理走同一条解析（`model_profiles._default_for_owner`）：自己选过就用自己的；没选过的普通用户在管理员指定了初始模型时用它，否则用部署配置。用到 owner 默认时运行记录（成功和失败都）写 `curator_default_model_source:admin_initial|deployment_default`（自己选过的不记）。
  - 没有自己档案的 owner（model-less）：生产里飞书用户 `ou_16d7…` 和测试 owner `tui-matrix/p1-r141-local-compact` 就是。核对（10-02，只看结构化字段）：管理员没设初始模型（`shared-model-profiles.json` 的 `initial_profile` 为空）、部署配置没有模型；这个飞书用户 6 次主代理运行（09-25）全部失败，从来没聊通过。所以它们的记忆整理和主代理一样解析不到模型，失败是 `CURATOR_MODEL_NOT_CONFIGURED`，现在失败记录会写 `curator_default_model_source:deployment_default`，不会越过主代理去找别的模型。要让它们能用：管理员在 /model 里指定“其他用户的初始模型”，或共享一个模型让用户自己选。
- **全局指定了档案、但某个 owner 解析不到时**（3a 定，2026-10-02）：
  - 这个 owner 改用自己的默认模型（`settings/curator_profile.curator_model_config_with_fallback`），每次运行的运行记录排最前写 `curator_profile_unavailable_fallback:<原因>`；建实例时只记一条 info 日志，不再刷 warning。
  - 指定档案只对能解析到它的 owner 生效；能解析到的 owner 和以前完全一样（不路由、建实例时建一次后端）。解析不到的 owner 每次运行重新解析一次，档案后来能用了就自动用它。
  - 退回时不按会话分组，整批用该 owner 默认模型；owner 默认模型也不可用时，仍是带指定档案编号和原因的类型化失败（`CURATOR_MODEL_NOT_CONFIGURED`，按原规则退避一小时）。
  - `/settings show memory_curator_model_profile` 写“本用户不可用：<原因>（改用本用户默认模型：<型号>）”。这改了 P12 原来“指定档案失效不回退”的约定。
- **`CURATOR_SCHEMA_INVALID` 能直接看出哪一项不合格**（3a 要求：ae 的 step17d 冒烟里 M2.7 手动整理失败，记录看不出原因）：
  - 解析和 schema 校验改抛 `curator_schema.CuratorOutputViolation`（仍是 `ValueError`，消息原文和失败码都不变），带宿主短码 `violation_code` 和只由 schema 字段名、下标拼成的 `violation_path`。
  - 失败诊断 `failure_diagnostic=` 里加这两个键（证据不合格时沿用 `CuratorEvidenceError.detail_code`），不带模型正文，也不带模型自己写的键名。键表见 `docs/modules/memory/04-structure.md` 的 R257 节。
  - 补跑规则不变：非默认组的 `CURATOR_SCHEMA_INVALID` 用 owner 默认模型补跑一次；默认模型自己出这个错照原熔断规则计数。
- **有意不做**：按组隔离失败（一组坏了别的组照跑）需要按组记失败与退避，熔断也要按组持久化，工作量约翻倍，记成后续可选项。现在一个模型持续失败时，熔断前后都会挡住所有组，和改动前（一个模型、全体受阻）相同。
- **已知局限**：前台忙闲判断（`foreground_model_active`）仍按服务启动时建的默认后端判断，没有按每组的后端判断。
- **真实核对**（隔离 home、官方 MiniMax M2.7 设为 owner 默认、官方 DeepSeek flash 给会话 A，档案副本 0600 用完删）：一次触发分两次运行、2 次真实调用约 18.7 秒；DeepSeek 处理会话 A、MiniMax 处理会话 B，各 1 条候选，两个会话游标都推进。证据 `~/.my-agent/decision-evidence/j13-curator-retest/thread-model-routing-20261002/`。
- **文档**：`agent_config.yaml` 与 `AgentConfig` 里这个键的注释（留空的含义、指定档案解析不到时的退回都变了）、MODEL_GUIDE 常见疑问、CODEBASE_TREE。
- **验证**：见 TESTS.md 同名节。

## 唤醒回合用量行带上模型身份：增量行按“本行调用”记后端与模型（ae step17c 冒烟观察，2026-10-02，分支 `claude/9b-wake-usage-models`，基于 `claude/3a-step16z` `c6f28b150`，已实现，并入 step17d）

- **问题**：step17c 冒烟基本链路（`decision-evidence/step17c-shutdown-rehearsal-38d7c8615/`）里，父代理被子代理完成唤醒的那一轮，
  用量行 `source=background_main_agent` 的 `models` 与 `purpose_breakdown.main.models` 是空列表，物理调用却是 1 次、输入 40,570 token。不是回归。
- **根因**（不是唤醒链少传字段）：
  - 唤醒回合沿用前台请求的 `request_id`，模型账本按 request 取同一个累计容器（`usage_scope_id` 相同），所以唤醒回合落盘的是同一本累计账的第二次快照。
  - `store_usage._model_usage_snapshot_delta` 对计数做数值差（3−2=1，对），对 `backends`/`models` 却做集合差，即“本范围新出现的名字”。
    前台那行已经记过同一个模型，唤醒行就被减成空；用途桶复用同一规则，同样为空。
  - 前台回合每次是新 request，没有同范围的先行行，所以一直没暴露。
- **做法**（只认结构化字段）：
  - 模型账本 `_ModelCallTotals` 新增按名物理尝试次数 `backend_attempt_counts` / `model_attempt_counts`，由 `to_summary` 导出，用途桶复用同一实现。
    只在 `observe_new` 里加：后端和模型在 `started` 时定下，之后的更新不改，所以只增不减。
  - 落增量行时由 `_identity_delta` 用次数差值算本行名单：差值大于 0 的名字就是本行调用用到的。本行的次数增量（只留正数）随行保存，供下次相减。
    行里的 `backends`/`models` 从此表示“本行调用用到的名字”。
  - 求和 `_sum_model_call_summaries` 由 `_add_identity_counts` 把两个次数按名相加，不进整数求和。
  - 旧快照没有按名次数时退回旧口径（本范围新出现的名字），也不补写次数键。同一范围不会跨进程续账，新旧行不会混在一个范围里。
- **影响**：
  - 每条新用量行多两个映射键；会话总量 `summary()` 的用途行也带上这两个映射。
  - 按行读 `models` 的地方（`scripts/reproject_model_usage.py`、外部采集脚本）从此拿到本行真实用到的模型。
  - 快照事件编号由摘要内容派生，加键只影响同一进程内的幂等重放，不跨版本。
- **边界**：已经落盘的空名单历史行不回填。
- **验证**：见 TESTS.md 同名节。

## 用户 10-02 拍板：生产设置与后续方向（2026-10-02，3a 记录，状态分项标注）

- **生产设置（已执行）**：
  - 默认对话模型改为 MiniMax-M2.7（官方 MiniMax 档案 d9607663），只影响新会话；
  - 记忆整理 `memory_curator_model_profile` 复位为空，跟随 owner 当前选中的模型（变更 f5b589f755e0，重启生效）；
  - 22 条不挂会话线程的 owner 历史 unknown 执行轮经 `/recover owner` 按 abandoned 处置（22/22，处置前 sqlite backup）。
- **维持现状（已定）**：生产 11 个决策点保持 observe，观察一段时间后再用 decision-line 核对脚本汇总，决定哪些改 apply。
- **已定、进行中**（各项完成后按各自条目更新状态）：
  - 嵌入模型 embo-01 先做隔离端到端验证。向量存 owner home 本地文件，不引外部向量库。
  - Curator 同一消息同主题、不同内容分开存（J13 遗留）。
  - 派子代理时可结构化引用父会话媒体（J11 遗留，带开关，仓库默认关）。
  - C12c 补关扫描跟随运行时实际的会话存储根（conversation_workspace）。
  - 工具瘦身第一阶段：由工具规格声明“默认收起”，tool_search 按需加载；精简说明。不改名、不合并、不改行为，带开关，仓库默认关。合并类改动留第二阶段。
- **待用户定**：Esc 中断后自愈收成 cancelled、停机边角四项、F1 重试、能力包后续、包内检查宿主证实、付费出图确认码、J16。解释见 3a 最终报告。

## 探测计入用量账的四处小尾巴：未知用途键计数、/status 诊断出口、真实形状两行用例、取证脚本去写死行号（2026-10-02，ef，分支 `claude/ef-probe-usage-tails`，基于 `claude/3a-step16z` `c6f28b150`，已实现，并入 step17d）

- **来源**：3a 派活。ds2 两轮加固（C7 计量修正、三件小加固、M1 修正）be 审过之后剩下四处可选尾巴，都不需要用户拍板。
- **① 未知用途键留结构化计数（`conversation/store_usage.py`）**：开放世界读法照旧——“值是对象”的未知用途键放行并当新桶求和，不改回报错。
  `_sum_purpose_breakdowns` 读到不在 `_PURPOSES` 里的键时记进程内诊断计数 `unknown_purpose_key_counts()`：按键名计次
  （拼成 `probe:tool_capabilty` 这类一眼能看出来），不同键名最多单独记 `_UNKNOWN_PURPOSE_KEY_MAX_COUNT`（16）个，超出只进
  `overflow_count`，坏文件撑不爆内存。计的是“读到的行次”（同一持久行每被求和一次 +1），用来发现有没有、是什么键，不是行数。
  不写日志、不影响求和结果、不持久化。
- **② 没绑记账范围的探测次数挂到 GET /status（`gateway_parts/http_handlers.py`）**：`unaccounted_probe_attempt_count()` 原来只有测试读。
  现在 `/status` 响应多一段 `usage_accounting`：`unaccounted_probe_attempt_count` 与 `unknown_purpose_keys`（`{keys, overflow_count}`）。
  **挂 /status 不挂审计的理由**：两项都是 Gateway 进程内存里的进程级计数，和 `loop_health` 同一种事实、同一个出口（直接读内存、不经磁盘）；
  `audit_records` 是按 owner 范围读持久记录（设置、用量账、请求记录）的工具，进程级计数跨 owner，放进 owner 审计会把别的用户的探测次数
  泄露给当前用户，也没有持久来源可读。线上 `curl /status` 看 `usage_accounting.unaccounted_probe_attempt_count` 非零就说明有探测没进
  用量账，`unknown_purpose_keys.keys` 非空就说明用量账里有不认识的用途键。只读、不清零、不写盘；两项的定义仍在各自模块，/status 只投影。
- **③ 补真实账本形状两行用例**：`_ModelCallAggregate.to_summary()` 同范围连写两行、都没探测用量 → 两行都没有 `probe:tool_capability` 键，
  第二行是增量。原来靠“一行真实形状”和“两行假形状”两个用例拼起来覆盖，现在一个用例锁住组合。产品代码不变。
- **④ 取证脚本去写死行号（`agent_py_agent/scripts/b_acceptance/timeout_budget_evidence.py`）**：`is_probe_production_sites` 原写死
  `llm_activation_readiness.py:178`，探测计量上线后 `backends/http.py` 也有埋点，原行号也已漂到 186。改为按源码结构化事实扫描：
  ast 找 `agent_py_agent/agent`、`agent_py_agent/cli`（不含 tests）里 `is_probe=True` 关键字实参，输出“相对路径:行号”；
  `is_probe=params.is_probe` 这类转发不算埋点。同一字典里 `timeout_stage_production_values` 仍写死 `["provider_wall"]`，不在本轮范围，原样保留。
- **边界**：没有新配置、没有新文件、不改读写合同；两个计数都是进程内、重启清零、不持久化；常数目录已重新生成。
- **验证**：见 TESTS.md 同名节。
- **追加提交（2026-10-02，3a 指派 + be 复审两条建议修）**：
  - **timeout_stage 也改结构化扫描**：同一取证字典里 `timeout_stage_production_values` 原写死 `["provider_wall"]`（A 阶段“全库唯一生产取值”，
    门槛 2 之后早已不止）。现在 `timeout_stage_production_sites()` 按源码扫描四种“产生阶段值”的结构位置——`stage`/`timeout_stage`
    关键字实参、同名参数默认值（`ProviderTimeoutError.__init__` 的 legacy `provider_wall`）、赋给同名变量（含元组赋值按位置对齐）、
    同名字典键的值（恢复状态载荷）——只认 `TIMEOUT_STAGES`（账本层单一事实源）里的字面量，输出“相对路径:行号:值”；值集合由它推出。
    纯比较（`exc.stage in {...}`）、集合定义本身和别的概念的同名字面量（runner 活动 phase 的 `stream_idle`）都不算来源。
    当前扫出 14 处、5 个值，正好覆盖全部登记阶段；取证要看的是反向——每个登记阶段有没有真实生产来源。
  - **S1 未知键名脱敏截断**：未知用途键名进诊断计数前只保留 ASCII 字母、数字和 `:_-.`，其它字符替换成 `_`，再截到
    `_UNKNOWN_PURPOSE_KEY_MAX_CHARS`（64）；坏文件里的超长或怪字符键名不会原样出现在 /status。读取端求和仍用原键，脱敏只在诊断层；
    不同原键脱敏后可能撞名，计数合并，诊断可接受。
  - **S2 tests 目录按相对包根判断**：`_production_source_files` 原按绝对路径 `path.parts` 排除 `tests`，仓库检出在名叫 tests 的上级目录下会把
    全部文件排除、扫描结果变空；改为看相对包目录（`agent`/`cli`）的路径段。

## 停机后不再从两条自动入口续派子代理：Audit 来源归并续派、嵌套子代理收尾唤醒父级（sol2/75 复审 38d7c8615 的必须修，2026-10-02，分支 `claude/38-audit-redispatch`，基于 `claude/3a-step16z` `c6f28b150`，已实现，并入 step17d）

- **问题**：
  - **sol2**：Audit 来源仍有工作时，结果归并把 `FAILED/model_call_admission_closed` 改成 PENDING。随后 session 收尾的 `_continue_pending_run_after_session → auto_start_orphan_run → auto_start_tasks` 在停机进程里续派，绕过了“停机后不自动重跑”。新 attempt 第一次调用又被拒，尝试次数 +1。
  - **75**：嵌套子代理收尾时，`_resume_direct_parent_after_session → _reconcile_parent_wait` 先删父级的等待标记，再经同一入口在停机进程里启动父级。父级被拒成 FAILED、尝试次数 +1，还逐层往上传。
    - 进程直接退出时，父级反而保留 PENDING + 等待标记，重启后等待调和照常续上。也就是说，关了栅栏反倒多丢了一层自动续跑。
- **做法**：统一按“本进程准入已关”这个结构化事实拦截。新增只读查询 `contracts/model_call_ledger.model_call_admission_closure()`，账本拒绝新调用读的也是这一个事实。
  - **自动派发公共入口** `orchestration/background/dispatch.auto_start_tasks`：最先判断，在任何 `reserve_runner_start`/后台启动之前返回 `blocked/host_shutdown`，不改 run。
    - 回执带 `error_code=MODEL_CALL_ADMISSION_CLOSED`、`reason_code`= 关门原因。
    - 经过这里的有：session 收尾续派、孤儿复活、卡住扫描、控制入口、建子代理时的自动启动。
  - **等待调和** `subagents/direct_parent_lifecycle._reconcile_parent_wait`：在释放标记之前判断。要续跑的父级不释放等待标记、不续跑（reason=host_shutdown），保持 PENDING + 等待标记，重启后等待调和照常续上；父级已结束时照旧释放。会话收尾、控制入口、定期监督都经过这里。
  - **结果归并**：来源仍有工作时照旧改回 PENDING，不改成不可恢复的 FAILED，重启后照常接续；但保留 `model_call_admission_closed` 这个停机原因，其它失败类型照旧清空。
  - **恢复提示**：错误码表、运行错误报告、失败类型注释三处统一成实际行为：
    - 普通子代理 run 重启后不会自动重跑，父级按 recovery_decision（状态机给 REPAIR）用同一 run 续派；
    - Audit 来源例外：保持 PENDING，重启后接续；
    - 被拒的用户回合不会续跑，需要用户重新发送。
- **不变**：local/main 子进程派工的语义不动；没有停机时，各入口行为不变。
- **边界**：
  - 关门前已启动的后台派发线程，在批次内部逐个启动候选（`runner_batches.collect_runner_candidates`）时不经过 `auto_start_tasks`。这些 runner 第一次调用会被拒，记成 `model_call_admission_closed`。
  - 发送层硬门、普通 run 重启后自动续跑，仍是待办。
- **sol2 建议修**：以下两组都已钉成用例：
  - 原因链边界：无匹配的环能终止、1 万层深链能找到、只有 `__context__` 的不算；
  - 唤醒毒丸：中间穿插停机，不清零已有的真实失败。
- **验证**：见 TESTS.md 同名节。

## 停机准入拒绝的调用方收尾：决策、子代理、运行错误报告、唤醒毒丸按“宿主停机”处理（sol2 复审 J17 栅栏，2026-10-02，分支 `claude/38-fence-callers`，基于 `claude/3a-step16z` `00bcf7d45`，已实现，并入 step17d）

- **问题**：sol2 复审 J17 栅栏（`bc639caa7`）后确认：没有锁倒置，窗口确实拦住了。但三个调用方不认识新的准入拒绝 `ModelCallAdmissionClosedError`，把正常停机说成了别的错误。
  - **决策入口**（必须修）：ActiveDecision 已登记、进模型账本前关门，`started_retained()` 被拒，`_failure_outcome` 兜底成 `error/enhancement_failed`；`active.shutdown_cancelled` 已置位却没用上。sol2 实测 active_decisions_cancelled=[1]、backend_decide_calls=0、cooldown_entries=0。
  - **子代理**（必须修）：`_subagent_run_failure_type()` 不认识新异常，返回默认 `runner_error`，并写进结果和恢复快照的 error_code。`runner_error` 还在自动重跑名单里，`MODEL_CALL_ADMISSION_CLOSED`“不可重试、停止”的合同丢了。
  - **运行错误报告**（建议修）：`runtime_error_report` 把它归成 `programmer_bug`。
  - **唤醒毒丸**（38 修复时发现的同类第 4 处）：`wake_poison.verdict_for_error` 把它当普通异常计数。每次重启部署，在途唤醒都会被记一次失败，满 5 次就把健康唤醒隔离掉。
- **做法**：
  - `contracts/model_call_ledger.find_model_call_admission_error(exc)` 是四处共用的唯一判定。它只沿显式 `__cause__`（raise ... from）找 `ModelCallAdmissionClosedError`，不沿 `__context__`、不读异常文本，有环防护。
  - **决策**：`_failure_outcome` 在设置撤销之后、通用兜底之前，认两个宿主停机事实，都按现有停机结果合同返回 `stale/host_shutdown`（保留原方案、不调 record_failure、不进连接冷却）：
    - 在途决策已被停机取消（`active.shutdown_cancelled`）；
    - 这次调用登记时被准入拒绝。
    - 先后顺序与 `_revoked` 一致：设置撤销优先。同步和后台 observe 都走这一处。
  - **子代理**：新增失败类型 `FailureType.MODEL_CALL_ADMISSION_CLOSED`（值 `model_call_admission_closed`，大写后正是已登记的错误码）。
    - 失败分类最先认它，结果的 failure_type 和恢复快照的 error_code 都写它。
    - 它不进 `RETRYABLE_RUNNER_FAILURE_TYPES`：本进程正在停机，自动重跑只会再被拒，还白白烧掉重跑次数。
    - 界面标签显示“宿主停机中断”。
  - **运行错误报告**：最先认准入拒绝，报 `category=host_stopping`、`recoverable=False`，并带两个结构化原因码，都取自异常属性：
    - `error_code=MODEL_CALL_ADMISSION_CLOSED`；
    - `reason_code`= 关门原因（Gateway 停机是 `MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`）。
  - **唤醒毒丸**：`_is_transient` 认准入拒绝（含原因链里的），判不计数，与取消、压缩让出同类；原因码仍按结构化规则生成。
- **边界**：
  - 重启后不会自动续跑这类 run。父级按完成唤醒里的失败类型决定是否续派。之前 runner_error 会被自动重跑，但要占一次重跑次数；停机期间的重跑还会被栅栏立即拒掉。
  - 如果要“重启后自动续跑、不占重跑次数”，另开一条，在派发处按本进程准入状态判定。
  - “已登记、未发出的调用在发送层硬门”仍是待办。
- **验证**：见 TESTS.md 同名节。

## J17 必须修：Gateway 停机准入栅栏——结清之后不再接新的模型调用（2026-10-02，分支 `claude/38-j17-shutdown-fence`，基于 `claude/3a-step16z` `7b21f38b9`，已集成 `bc639caa7`，并入 step17d）

- **问题**（sol2 只读审查）：停机结清只拿一次账本快照，再逐本把在途调用记 failed，没有同时关掉新调用的准入。
  - Gateway 对三条循环各等 2 秒，排空不完整也照样结清；之后还活着的 worker 能在已结清的账本上登记新的 started。
  - 快照之后才登记的账本（迟到完成构建的 runner worker、owner 池 agent）不在那次遍历里。
  - sol2 复现：`settled=["visible-before-shutdown"]`、`still_open=["started-after-shutdown-snapshot"]`。J17 之前的结清函数也有这个窗口，不是 J17 引入的回归。
- **做法**（结构化栅栏，不靠再扫一次）：
  - `contracts/model_call_ledger.py` 新增本进程唯一的模型调用准入表 `_ModelCallAdmissionRegistry`：账本弱引用集合，加关门原因 `ModelCallAdmissionClosure`（error_type/error_code）。
  - 每本 `ModelCallLedger` 构造时在准入表锁内登记，所以关门快照一定包含关门前建的全部账本；关门后新建的账本同样受准入表约束。
  - `ModelCallLedger.started` 在本账本锁内检查准入：已关门就抛 `ModelCallAdmissionClosedError`，不建记录，调用方不发请求。
    - 错误码 `MODEL_CALL_ADMISSION_CLOSED` 已登记错误分类：不可重试，处理动作为停止。
    - 重复登记同一 call_id 仍只返回原记录。
  - `close_model_call_admission(closure)` 分两步：先在准入表锁内置关门原因、取在册账本；再逐本在账本锁内把在途调用记 failed。
    started 和结清在同一把账本锁里排队，所以每次登记要么早于这本账被结清（被结清成 failed），要么被拒绝，没有窗口。
    锁序固定为“账本锁 → 准入表锁”，结清方不会在持有准入表锁时去拿账本锁。
  - `call_runtime.settle_open_model_calls_for_shutdown()` 改成调用它，不再带 agent 参数。关门原因沿用原来的 `MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN` / `HostShutdownInterrupted`。
    Gateway 收尾顺序、事件 `gateway_model_calls_interrupted` 和投影字段都不变。
  - 删掉 J17 的显式登记 `track_shutdown_ledger` 与 `_SHUTDOWN_LEDGERS`，runner worker 构建、owner 池建 agent 两处调用一并删掉。
    构造即登记已经覆盖它们，还覆盖了原来没登记的进程内账本，例如 gateway supervisor 建的 agent。
  - 只有停机结清会关门，进程内不重开。
    - 测试在 `tests/conftest.py` 里按用例换一张新的准入表（`_isolate_model_call_admission`）。
    - 不换的话，一条跑过收尾的测试之后，同一进程里后面的测试全部拒绝调用。
- **已登记调用照原规则结清**（首个终态规则）：
  - 停机记的 failed 是逻辑终态，物理线程可能还在退出；
  - 迟到的 HTTP 观察照记，迟到的成功或失败不重开终态；
  - 保留句柄照常释放，全部放掉后明细才按原规则被裁（sol2 建议 2，已加用例）。
- **累计容器**（sol2 建议 1）：在途调用所属的 request/run 累计容器不被 LRU 淘汰（`_live_open_scope_keys`，COMPACT_KEYERROR 修复里已做）。停机结清和迟到观察之后，累计数仍对得上。
- **边界**：
  - 栅栏只拦“新调用的登记”。关门前已登记、还没真正发出 HTTP 的调用，账上已记 failed，但物理上仍可能发出去。要拦实际发送，得在各发送点复核，不在这次范围。
  - 被拒的调用由调用方按普通异常处理（回合失败）。关门只发生在进程退出前。
  - local/main 子进程 runner 不在本进程，各自收口；kill -9、断电仍靠启动对账。
- **验证**：见 TESTS.md 同名节。

## C4 接替提示开关进管理员 /settings 白名单（2026-10-02，3a，集成分支 `claude/3a-step16z`，已实现，并入 step17d）

- **问题**：`subagent_takeover_hint_enabled` 在 capability 配置里，按 P17 规则默认是安全边界（会改变父级模型看到的内容，模型不能改），但没进 `USER_SETTINGS_BOUNDARY_KEYS`，结果管理员 `/settings set` 也被 `PARAMETER_BOUNDARY` 拒绝，只剩手改文件一条路；已定做法 5 要求“生产打开并给关闭命令”，做不到。部署前用参数中心预演时发现。
- **做法**：加进 `user_config_capability.USER_SETTINGS_BOUNDARY_KEYS`，与 `enable_memory_search_tool` 同类：模型工具仍拒绝，已认证管理员 `/settings` 的短生命周期写作用域可以开关，写进运行时实际读取的 `<owner home>/config/capability_config.yaml`（不存在时新建 600），重启 Gateway 生效。随包 `capability_config.yaml` 注释补一句修改途径。
- **验证**：`test_subagent_takeover_hint.py::test_switch_is_a_boundary_only_the_admin_settings_command_can_flip`（模型被拒且不建文件；管理员作用域开/关、回读一致、文件 0600）；变异“从白名单删掉该键”被拦下后还原。

## 交付复核焦点题面写明“全部通过且没改动时选 not_needed”（delivery_quality 压线核查）（2026-10-02，分支 `claude/be-delivery-criteria`，基于 `claude/3a-step16z` `d8474300f`，已实现，并入 step17d）

- **问题**：J12 续基准里 delivery_quality 16/20，刚好压线（阈值 0.8）。
  - 错的 4 题全是“全部通过且之后没改动”的两个用例（dq-03、dq-09），两遍都选了最新焦点 focus_2，而不是 not_needed/no_match。
  - focus_2 正是当前回执自己的焦点。产品在选中当前焦点时本来就不追加提示，所以这 4 题用户看到的效果和 not_needed 一样，没有给出错误建议。
- **是不是措辞造成的**：是。
  - 旧题面只写“选一个最值得复核的焦点”，not_needed 只写“无需额外复核建议”，没说什么情况该选它，模型就硬选了一个。
  - 同一时段旧措辞对照（不登记）仍是 16/20，错题完全相同；新措辞 20/20，dq-03、dq-09 两遍都答 not_needed，其余 8 个该选焦点的用例全对。
- **做法**：只改说明文字，候选键、题目结构、焦点候选和非选择回答的处理都不变。
  - 题面写明：验证没通过的、或验证之后同一项目又改过文件（edited_after 为 true）的焦点值得先复核；所有焦点都通过且之后没再改过文件时不要硬选，选 not_needed。
  - not_needed 写明适用情况；no_match、abstain 写明“选它本次不给建议”；need_data 不变。
- **结果**：16/20 → **20/20**（准确率 1.0），Jev 20 次调用全部成功，期望答案没有改，成绩已登记进 `results.json`。
  - 耗时：新措辞中位 1.6 秒、最长 3.4 秒（20 次里 1 次超过 3 秒）；同时段旧措辞中位 2.1 秒、最长 3.7 秒（6 次超过 3 秒）。
  - Jev 共 40 次调用（对照 20 + 新措辞 20），跑前写下的预计是 40 次，上限 120 次。
  - 证据：`~/.my-agent/decision-evidence/j12-quality-bench/*delivery-*`。
- **开关**：点位仍默认关闭，生产是否打开由集成方定。
- **验证**：见 TESTS.md 同名节。

## 外部材料阅读优先级逐页题的候选措辞修正（照 J12b）（2026-10-02，分支 `claude/be-material-criteria`，基于 `claude/3a-step16z` `b5be542e8`，已实现，并入 step17d）

- **问题**：
  - J12 续基准里 external_material_order 只有 22/36（0.61，阈值 0.8）。14 道错题全是无关页答 `not_needed`/`no_match`，而不是 `later`。
  - 任何非排序回答都会让整次不给阅读顺序提示，所以 apply 下这些调用都白跑。
  - 根源和 recall 改前一样是措辞：`not_needed` 写“无需额外建议”，`no_match` 写“无法匹配优先级”，Jev 把它们读成“这一页用不上”。
- **做法**：只改 `_CHOICES`、`_NON_SELECTIONS` 的说明文字和逐页题题面；候选键、题目结构、`_RANKS` 和非排序回答的处理都不变，采用复核与返回路径没动。
  - `first`、`normal`、`later` 写明这一页与当前问题“直接相关”“有些关系”“无关或很弱，放到后面读、原页仍保留”。
  - `not_needed`、`no_match`、`abstain` 写明“选它会让本次不给任何阅读建议”，前两个提示“只是与问题无关请选 later”。
  - 题面写明三档各对应什么情况。
- **为什么不把逐页的 `not_needed`/`no_match` 直接当作 later**：开发铁律规定状态别名不隐式兼容；改措辞只动给模型看的软材料，不动机器语义。
- **结果**：重跑本点位基准，22/36 → **36/36**（准确率 1.0，阈值 0.8），Jev 12 次调用全部成功，期望答案没有改，成绩已登记进 `results.json`。
  - 同一时段用旧措辞对照跑一遍（不登记）：仍是 22/36，所以提升来自措辞。
  - 耗时：新措辞中位 2.2 秒、最长 3.5 秒（12 次里 1 次超过 3 秒）；同时段旧措辞中位 2.2 秒、最长 4.1 秒。
    早上首轮是中位 1.4 秒，变慢来自这个时段的网络，不是措辞（旧措辞同时段 12 次里 2 次超过 3 秒）。这个点位是前台点位（默认 3 秒），打开前要考虑这一点。
  - 每页题的说明多了约 200 个汉字，离决策输入上限（256 KB）很远。
  - 证据：`~/.my-agent/decision-evidence/j12-quality-bench/*material-*`。
- **开关**：点位仍默认关闭，生产是否打开由集成方定。
- **验证**：见 TESTS.md 同名节。

## C7 计量与说法修正：探测计入用量账 + 自动检测成本上界（2026-10-02，分支 `worker/ds2-effort-probe-metering`，基于 `claude/3a-step16z` `b5be542e8`，已实现，并入 step17d）

- **问题**：①每个模型第一次被用时宿主先发一次 `probe_tool_capability`（工具能力探测，按模型缓存），该调用不进
  `model_usage`，用量账不完整；②`/effort` 自动检测回执写“约需 1～5 分钟、会额外消耗少量 token”，实测 MiniMax-M2.7
  上 9 次检测约 23 分钟、单次输出最多 32867 token、合计约 10 万输出 token，说法低估且成本无上界。
- **做法**：
  - 探测每次真实 `generate` 都记账（重试也算）：开始记 `ModelCallStartedParams(is_probe=True, metadata={"purpose": "probe:tool_capability"})`，
    成功用供应商 usage 收尾、失败记 failed；按模型缓存的行为与命中缓存不重复记账均不变。
    用途独立成桶：`model_call_ledger._PURPOSE_BUCKETS` 与 `store_usage._PURPOSES` 同步加 `probe:tool_capability`（两者不同步会在收口时报
    DataCorruptionError，已用测试和变异钉住）。记账范围经 `probe_accounting_scope`（ContextVar）从 `select_tool_protocol` 的三个调用点
    （loop_support、model_selection、gateway_model_adoption）透传到后端，附 request_id/run_id；无宿主绑定 scope（直调、测试替身）不记账。
  - 新常数 `reasoning_probe.PROBE_MAX_OUTPUT_TOKENS = 40000`（进常数目录 constants_catalog.json，名字带 `_TOKENS` 单位后缀、上方有中文说明）；
    每次检测请求经 `ProviderRequestOptions(max_output_tokens=...)` 显式带上限，与档案输出上限取小后才是真实发送值；
    base/anthropic/openai_chat 适配器 `generate()` 透传该字段。
  - 回执改为按结构化事实生成：“每次输出上限 40000 token，最多约 360000 token；长输出模型可能更久”，删除写死的“1～5 分钟/少量 token”。
    不改变“自动检测是否默认触发”（开关与五条触发条件原样）。判定所需最低输出差仅 200 token，上限远高于此，截断不会压平 low/max 差异。
- **副作用与边界**：探测开始进用量账是行为变更（用量账更完整）；探测的 request_id 来自触发它的选模/网关调用点，缓存命中时无新记录。
  `AnthropicCompatibleBackend` 为压回代码尺寸做了内部重构：`_thinking_fields`/`_stream_text_once` 外移为模块级函数（回调收成
  `_StreamCallbacks` 小数据类）、`generate_json` 委托模块级函数，协议方法签名与行为不变。
- **验证**：见 TESTS.md 顶部同名节；5 个变异全部被拦截。

### C7 三件小加固（2026-10-02，分支 `worker/ds2-probe-accounting-hardening`，基于 `claude/3a-step16z` `7b21f38b9`，已含上一轮 C7 计量修正，已实现，待集成）

38 审查上一轮 C7 提交后建议的三件加固（无必须修项）：

- **用途桶开放世界（store_usage.py）**：写入端 `probe:tool_capability` 只在本快照或同范围先前行真有探测用量时才落键，老三个桶照写，
  多数线程的行与上一版逐字节一致（`_purpose_breakdown_delta` 模块级小函数按需组装，避免加深 `_model_usage_snapshot_delta` 嵌套）；
  读取端 `_purpose_rows` / `_sum_purpose_breakdowns` 接受不认识的用途键，按“已知键 + 出现过的键”求和并原样保留（以后再加用途桶，
  旧读法不再读坏新行，回滚实测 17b 写 → 17a 读的 DataCorruptionError 由此消除）。三条严格规则照旧：purpose_breakdown 值必须是对象、
  桶值不能嵌套 purpose_breakdown、schema 版本不对报 DataCorruptionError。
- **压缩入口绑定探测记账（compact.py + http.py）**：`_build_compact_candidate` 在范围内确有媒体块解析视觉能力事实时，照选模调用点用
  `probe_accounting_scope` 绑定账本 + request_id/run_id，让压缩触发的工具能力探测也进 `probe:tool_capability` 桶；缓存命中不重复记账。
  对“没有绑定记账范围”的真实探测尝试留进程内结构化计数（http.py `unaccounted_probe_attempt_count()` 只读诊断入口 + 测试用 reset），不静默。
  视觉探测本身（看图那次 generate）记账是另一件事，本轮不做，列入待办台账。
- **/effort 回执按实际生效上限算（reasoning_probe.py）**：每次输出上限与总上限改用 `min(PROBE_MAX_OUTPUT_TOKENS, effective_max_output_tokens(档案))`；
  默认窗口 128000 的档案输出上限 32000，回执写“每次输出上限 32000 token，最多约 288000 token”，不再按常量 40000 虚报。
- **验证**：4 组用例全过（`test_store_usage_open_world.py` 新增 6 个、`test_probe_tool_capability_metering.py` 新增 3 个、
  `test_reasoning_probe.py` 新增 1 个）；4 个变异（读端恢复封闭集合、写端恢复无条件写、压缩移除绑定、回执恢复常量）全部被拦且逐字节恢复；
  相关回归 86 + 149 + 54、guards9 170 全过；size_diff 新增 0。详见 TESTS.md 顶部同名节。
- **M1 修正（2026-10-02，be 审查必须修，追加提交）**：写端“只在真有探测用量时才写探测桶”原用字典真假判断
  （`if not current_row and not prior_row`），生产上从不生效——线上实时快照来自账本 `_ModelCallAggregate.to_summary()`，
  四个桶固定都在，无探测时 probe 桶是全 0 骨架字典（含 backends、accounted_input_tokens 等字段）判断为“真”，首行就写空桶；
  先前累计由 `_sum_model_call_summaries` 求和也得到骨架字典，同样为“真”，同范围第二行起照写。改为结构化计数判断：
  新增 `_purpose_bucket_has_usage(row)`，桶里调用/尝试/token 等加法字段（`_ADDITIVE_USAGE_FIELDS`，与
  `_model_usage_snapshot_delta` 共用同一名单）任一大于 0 才算有，当前快照与先前累计共用；判断只读数值字段，
  不看字典真假、不看文字。读取端开放世界与三条严格规则不变。配套：`http.py` 未绑定探测计数器改名
  `_UNACCOUNTED_PROBE_ATTEMPT_COUNT`（带 `_COUNT` 单位后缀）并重新生成常数目录（816 项，--check 一致）。
- **上线说明（回滚兼容）**：修好后，没用过探测的线程写出的行不再含 `probe:tool_capability` 键，回滚到 17a 照常能读；
  用过探测的线程行里带探测桶，17a 的 `summary()` 严格读法仍会报错（生产链路没有调用方）。

## 选模型两个点位的仓库默认期限定为 5 秒（已定做法 10“前台默认 3 秒，选模型保持 5 秒”）（2026-10-02，分支 `claude/be-selection-timeout-default`，基于 `claude/3a-step16z` `a44f2ad62`，已实现，并入 step17d）

- **背景**：J5 把前台通用期限默认改成 3 秒，但选模型（`model_selection`、`subagent_model`）在冷连接下 Jev 实测约 3.2 秒，按 3 秒会超时、保留原模型。
  生产已经用覆盖层对齐（通用 3 秒、这两个点位 5 秒、阶段 5 秒、后台 15 秒，revision 9）；这次把同样的做法写进仓库默认，新装的用户也是这样。
- **做法**：沿用点位默认机制，不新增配置项。
  - `decision_settings_defaults.py` 新增 `SELECTION_POINT_TIMEOUT_SECONDS = 5.0` 和登记表 `POINT_TIMEOUT_FLOOR_SECONDS`（只登记这两个点位）。
  - 投影时点位没有覆盖，就取 max(通用期限, 5 秒)，来源记 `point_default:5`；TUI 显示“本点位默认（不低于 5 秒）”。
  - 用户长期设置、本会话临时设置里对点位的覆盖照旧优先，想低于 5 秒就单独覆盖这个点位。
  - 通用期限调到 5 秒以上时，这两个点位跟着变长（取较大值），不会被下限拉短。
- **阶段默认 4→5 秒（需要集成方知道）**：前台点位实际等待取点位期限与阶段期限的较小值，阶段默认还是 4 秒的话，5 秒的点位默认实际只能等 4 秒。
  所以 `decision_stage_timeout_seconds` 的仓库默认（dataclass 与随包 YAML）一起从 4 改到 5，与生产现值一致；
  影响：前台阶段内所有点位累计最多多等 1 秒（召回前后、能力推荐等 3 秒点位单次期限不变）。常数目录和前端参数目录已重新生成。
- **生产**：覆盖层已经是这些值，这次改动在生产上没有效果。
- **验证**：见 TESTS.md 同名节。

## 模型采用标记 `send_intent_uncertain` 的含义：故意不结清，审计附说明（2026-10-02，分支 `claude/ae-adoption-settle`，基于 `claude/3a-step16z` `ee3bd09f7`，已实现，并入 step17d）

- **问题**：J11 真实复核时发现，主会话自动采用后，请求记录里的选模型标记停在 `send_intent_uncertain`，请求 done 后也不变，看记录的人容易误读成“发送状态没确认”。
- **复核结论：这是故意不结清**，所以不改状态机。依据：
  - 采用事务（`gateway_model_adoption`）在发送前的线程 CAS 里只记“发送意图”。
  - `record_adoption` 的合同是“发送意图不能写成 HTTP 已接收、终态后重试不覆盖既存事实”。
  - 设计交接（`DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md` 第 5 条）与 gateway 结构文档都写明：HTTP 事实只读原调用观察账，请求标记只是结果投影。
  - 现有用例 `test_actual_gateway_adopts_only_after_full_payload_and_persists_intent` 跑完整个业务请求后，仍断言这个状态。
  - 如果在请求终态把它改成 `adopted_sent` 一类的值，等于把 HTTP 事实再存一份，违反“一个概念一个权威位置”。
- **含义**：
  - `send_intent_uncertain`：已采用该模型，线程选择已提交，只证明发送意图。实际发往哪个模型、成功几次，以会话用量账（`model_usage`）为准。
  - `commit_unknown`：线程写入没能确认；本请求不发业务请求，也不跨模型重发。
- **做法**：
  - `gateway_parts/request_audit_records` 投影选模型观察时，对这两个状态附一句固定的 `status_note`，按结构化 status 取，其它状态不附。
  - `audit_records` 工具说明补一句。
  - TUI 和 IM 没有直接展示这个标记的界面，用户都是经审计工具的回答看到它，所以说明加在审计投影上。
- **验证**：见 TESTS.md 同名节。

## 模型调用账不再裁掉进行中的调用（生产缺陷：sol 会话压缩 COMPACT_KEYERROR，2026-10-02，分支 `claude/38-compact-keyerror`，基于 `claude/3a-step16z` `df6033475`，已实现，并入 step17d）

- **现场**：step17a 上 sol 会话（thread-de866e7291f846db，gpt-6.1-sol）的压缩在 04:32:22 开始，约 11 分钟后失败，
  失败码 COMPACT_KEYERROR。账上这次的压缩辅助调用是“完成 1、首字 1”：第二次调用既没完成也没失败。
- **根因**：同一个 agent 共用一本 ModelCallLedger，明细只留最新 128 条（`_trim_records`）。只有拿了活令牌的调用不被裁，
  目前只有决策调用和带预算的调用拿令牌，主回合与辅助调用（压缩摘要）都没有。sol 压缩期间，同一 agent 上另一会话
  （gwreq-1790940678）发了约 139 次调用，把进行中的摘要调用明细挤掉了。摘要调用收尾时，`record_model_call_finished`
  在 `_require_record` 查不到调用编号，抛 KeyError；except 分支的 `record_model_call_failed` 又抛一次；最后被包成 COMPACT_KEYERROR。
- **复现**：把该线程数据复制到隔离目录（700/600，用完即删），走手动 /compact 入口加假模型。
  - 无负载时第 23 代正常生成，数据本身没问题。
  - 在摘要调用进行中模拟另一会话发 129 次调用，调用栈与生产一致。
  - 修复后，同样负载下第 23 代正常生成。
- **修法（账本层，覆盖所有调用方）**：
  - 裁剪只针对已结束的历史：进行中（started/first_token）且近期有活动的调用一律保留，调用方之后照常写首字、活动和终态；
  - 停机结清（J17，现为 `close_model_call_admission`）也能看到这些调用；
  - 这些调用所属的 request/run 累计容器同样不被 LRU 淘汰，否则收尾时会落进新建的空容器，请求用量和状态计数就丢了；
  - 显式活令牌的语义不变，仍可多留已终态记录，给迟到的物理观察用。
- **防泄漏**（3a 要求）：进行中的调用超过 `open_call_stale_seconds`（默认 6 小时）没有任何活动就算失联，可以像已结束明细一样被裁。
  - 裁掉的数量累计在 `ModelCallLedger.stale_open_calls_trimmed()`，同时打一条只含计数的警告。
  - 6 小时远超任何供应商的首字或静默超时，正常长调用不会被误判；永远收不到终态的明细不会无限堆积。
- **改动的既有用例**：`test_model_call_ledger_partitions.py` 里 3 个用例原本断言“没带令牌的在途调用会被裁掉”，这正是这次要改掉的规则。
  改为先让调用正常结束，再释放令牌或灌入新调用，原意（令牌释放后恢复普通裁剪、被裁记录不被重建）保持不变。
- **结构**：判定和计数放在模块级函数 `_is_live_open_call` / `_live_open_scope_keys` / `_count_stale_open_trimmed`，
  `ModelCallLedger` 类回到 350 行硬线以内，告警身份与底版相同。

## 工具操作持有者的同主机判定统一到 process_host_id（2026-10-02，分支 `claude/38-host-compare`，基于 `claude/3a-step16z` `a8586712e`，已实现，并入 step17d）

- **背景**：网关 host_id 修复后，`tool_operations` 和 `managed_operation_store` 里还有三处直接拿 `socket.gethostname()` 判同主机：
  - 工具操作租约 `_operation_holder_is_live`；
  - 本地核对标记 `_reconciliation_marker_is_live`；
  - runtime.db 核对标记 `_managed_reconciliation_claim_is_live`。
  持有者的 host 也写的是主机名。macOS 换网络后主机名会变，本机持有者被当成另一台主机，进程死了也要等租约到期才能接管。
- **做法**：
  - 新增 `tool_operations.tool_operation_host_id()`，返回 `daemon_metadata.process_host_id()`（进程内缓存的稳定主机身份）。
    懒导入，避免 local_storage 和 gateway_parts 循环导入。
  - `new_tool_operation_holder` 的 host 和 holder_id 里的主机段改用它；三处比较也改为和它比。
  - 空 host 的原有语义不变：工具操作两处当作另一主机，runtime.db 那处走 PID 核验。
- **holder_host 格式变化与过渡**：
  - 新记录写的是 32 位主机身份摘要，不再是主机名。
  - 升级前写下的主机名记录和本机身份不相等，一律按“另一主机”处理：租约到期前当作还活着，到期后才可接管。
    也就是老记录只是等一次 TTL，不会被误接管。
  - 新旧版本同时在跑（部署切换）时，双方都把对方的记录当成另一主机，同样只等 TTL。
  - 诊断里看到的 holder_host 从主机名变成摘要，可读性下降，换来主机名变化后判断不出错。
- **没动的**：`curator_state` 里记的 `host` 和 `runtime_db/repository` 的 instance_id 只做展示，不参与同主机判定，仍用主机名。

## step17a 文档状态对账（2026-10-02，分支 `worker/ds2-docs-reconcile-step17a`，本轮改动，未随 step17a 上线）

- **背景**：step17a 已上线（main `de222698b`，10-02 03:43 部署到本机生产），但 `DESIGN_LEDGER.md`、`STATUS.md`、
  `docs/ROADMAP.md`、`docs/COMPLETED.md`、`LLM_GUIDE.md`、`TESTS.md`、`docs/design/PARAMETER_CENTER.md` 与各模块
  `02-progress`/`04-structure` 里，很多随它上线的条目还写着“待集成”“本地待集成”“已实现，待集成”“未集成/部署”。
- **做法**：只改状态字样，正文、结论、数字一律不动。每个条目先用 `git log -S'<标题原文>'` 找到引入提交，
  再用 `git merge-base --is-ancestor <引入提交> de222698b` 判定是否随 step17a 上线；只有判定在线的才改写。
  未判定的保持原状并列入交接报告，不误标。
- **唯一判据**：引入提交是 `de222698b` 的祖先 = 已上线；不是 = 留在“待上线（下一版）”。不按“基于某个基线”推断。
- **清掉合并残留**：`TESTS.md` 里同一 `##` 标题出现两次的（合并时留下的短片段），保留正文更长的那一份，删掉另一份。
  纯结构判定（同名标题分组 + 正文行数），不按语义猜。
- **明确不动的**：与集成分支无关的其它工作流记录；引入提交在 `de222698b` 之外的条目（C14 2c、P14 修复、C5 车道闸、J9、
  主机名修复、IM 路径说明、J16 观察候选、插件来源越权回执等）。

## 主机名变化后 SIGTERM 仍能停网关：主机身份进程内缓存 + macOS 硬件 UUID（2026-10-02，分支 `claude/38-host-id`，基于 `claude/3a-step16z` `a52ac109c`，已实现，并入 step17d）

- **现场**（3a，step17a 切换）：macOS 没有 /etc/machine-id，`process_host_id` 退回 `socket.gethostname()`；换网络后主机名变成 anonymous。
  网关 SIGTERM 处理函数 `_record_gateway_signal_stop_request` 现算 `build_process_identity()`，host_id 和启动时记下的对不上，
  主循环把停止请求当成发给别的进程的而一直忽略，网关停不下来。当时 3a 用 `write_targeted_gateway_stop_request`（从 pid 记录取 host_id）绕过去。
- **做法**：
  1. 信号处理函数用启动时记下的本代身份：`_install_gateway_signal_handlers(paths, process_identity)` 和 `_run_gateway_service_loop`
     都取 `_gateway_context_process_identity(context)`，是同一份；`_record_gateway_signal_stop_request` 的身份改为必传，不再现算。
  2. `process_host_id()` 在进程内只算一次（`functools.lru_cache`），pid 记录、信号停止请求、主循环、租约核验拿到同一个值。
  3. 稳定来源：先 machine-id（Linux/容器，取值与改前相同）；macOS 再取硬件 UUID，用 libc `gethostuuid()`（ctypes），
     它和 ioreg 显示的 IOPlatformUUID 是同一个值（本机已核对相等）；都取不到才用主机名。结果仍是 sha256 摘要，原始 UUID 不落盘。
- **为什么不用 ioreg 子进程**：首次取身份可能发生在任何代码路径里，起子进程会撞上调用方或测试对 subprocess 的替换（开发中真的撞到：
  `test_gateway_start_force_restart` 替换了 Popen，首次取身份时 ioreg 调用直接报错），还可能吃掉测试预设的返回值。gethostuuid 不起进程。
- **跨进程核验在主机名变化后的表现**：
  - 有稳定来源（Linux machine-id、macOS 硬件 UUID）：主机名变化不影响，`process_identity_is_live` 照常判断。
  - 只剩主机名可用时：本进程内前后一致（缓存）；主机名变后新起的进程算出另一个值，对旧记录给 None（无法判断，按 TTL 兜底），
    不会误判已死。别的进程发停止请求走 pid 记录里的身份，不受影响。
- **一次性过渡**：macOS 上 host_id 从“主机名摘要”换成“硬件 UUID 摘要”。升级后，上一版进程写的身份记录（租约、唤醒尝试）按“另一主机”处理，
  只是等 TTL，不会误接管；上一版网关停止仍走 pid 记录，不受影响。Linux 取值不变。
- **没动的**：`tool_operations`、`managed_operation_store` 里还有三处直接拿 `socket.gethostname()` 判断同一主机。主机名变了它们按“另一主机、
  租约到期前视为存活”处理，是保守方向，不会误判已死；要统一到 `process_host_id` 会改变落盘的 holder_host 格式，留作后续。

## C5 登记组件探针固化（2026-10-02，分支 `worker/ds2-run-claim-probe-tests`，基于 `claude/3a-step16z` `78fc5c209`，已实现，并入 step17d）

- **来源**：sol2 只读审查 C5 的“建议修”（`gwreq-1790933078` 报告），要求把审查时临时使用的登记组件探针固化为仓库测试。
- **做法**：只新增测试文件 `agent_py_agent/tests/test_run_claim_probe_paths.py`，不改任何产品语义；测试按 C5 修复后（车道闸）的实现写。
- **覆盖**：领取车道失败、回合执行抛错、收尾阶段抛错、心跳启动失败、同会话两个等待者分别取消、跨会话隔离、跨 owner 路径隔离，共 7 项。
  每项都断言登记计数回到 0、车道可再次获取、登记在撤销前确实生效过，并直接核对登记表内容。
- **变异**：5 个，全部被拦住（去掉 finally 撤销、撤销不减计数、跨会话共用键、撤销只置 0 不删键、登记调用被去掉）。
- **已知边界**：“登记要过车道闸”只影响互斥时序、不改可观察状态，黑盒测试无法区分（实测删掉 `gate.lock` 后本文件仍全绿）；
  这条留给 C5 自己的确定性交错用例守。登记键的“线程为空返回空串”分支当前无用例覆盖。详见 TESTS.md。

## 管理员判定收拢：插件管理与 user_config 工具改用统一函数（2026-10-02，分支 `claude/38-admin-fold`，基于 `claude/3a-step16z` `6980e5f41`，已实现，并入 step17d）

- **背景**：P14 新增了 `owner_access.is_complete_local_admin_owner`（三项身份都是非空字符串，且是本机 local/main），/settings 的 `_is_admin` 和记忆向量重建已经改用它。
  另外两处仍是内联写法：插件管理（IM 路径，`plugin_command_service._scope_management`）和 user_config 工具的本机配置动作（`user_config_tool._is_main_owner`）。
- **做法**：两处都改为调用 `is_complete_local_admin_owner`，规则本身一字不改。
  - 插件管理传入和 /settings 同一种 home（`home_paths_with_owner(base, resolve_owner_home(...))`）；
  - user_config 传入 agent 的 `home_paths`，缺上下文时得到 None，判为非管理员（和原来一样）。
- **行为对照**：
  - 对系统自己构造的身份（`OwnerIdentity.local_main`、经 `safe_path_segment` 的 provider_user/group、命令流校验过的非空字段），结果和改前完全相同。
  - 唯一的差别在纯空白身份字段。插件管理原来按真值判断完整性，`"  "` 算有值，`is_local_admin_owner` 又会去掉空白后当成本机，
    结果 provider 为纯空白、kind 为 main 的身份会被认作管理员；/settings 一直不认。收拢后两边都不认，方向是变严格。
  - user_config 原本就去空白，没有变化。

## 决策账补结构化“结果类别”，分清选中 / 非选择 / 被丢弃（J10 复测观察）（2026-10-02，分支 `worker/ds1-decision-outcome-category`，基于 `claude/3a-step16z` `78fc5c209`，已实现，并入 step17d）

- **来源**：ae 做 J10 真实复测时的观察。决策结果日志只记 mode、status，Jev 调用成功之后看不出它是选中了某个候选，还是给了 `not_needed`/`need_data`/`abstain`
  这类非选择；也看不出宿主有没有因为来源、设置、期限变化把建议丢掉。于是“没出提示”到底是 Jev 没选、还是选了被丢，只能靠猜。
- **取值族**（封闭但可扩展，只记类别与结构化取值/原因码，不记候选文字与正文）：
  - `selected`：至少有一题给了可判定的选择。
  - `non_selection:<取值>`：Jev 明确说不需要或给不了；取值原样透传（各点位词表不同，收敛判据见下）。
  - `dropped:<原因码>`：宿主把建议丢了，原因码见下方码表（本批新增 4 个消费者采用前复核码 + 宿主既有 `identity_changed`），不新造同义码。
  - `no_selection_recorded`：成功但没有可判定的选择（无响应、逐题错误、空答案、旧替身）；`unrecorded` 只在统计时给“写该行时还没有这个字段”的旧记录，不写回日志。
- **唯一写入处**：`conversation/decision_outcome_log` 的投影 `decision_outcome_row` 加 `result_category`，由纯函数 `decision_result_category(outcome)` 从结构化事实推导，
  判定优先级：带 `dropped_reason` → 逐题答案里命中非选择取值 → 至少一题有可判定选择 → 兜底。不做点位专项分支，不解析模型正文。
- **宿主丢弃怎么落账**：原结果行在 `decide()` 落盘后不可改写，所以消费者在丢掉建议时调 `record_decision_dropped(agent, stage, outcome, reason)`
  另追加一行补充记录（`record_kind="dropped"`，复用同点位/身份 + `result_category=dropped:<原因码>`）。只在“确有可采用的建议”（`may_apply` 且带响应）、
  且响应真的选中了候选（`decision_result_category == "selected"`）时记——被丢弃是“选中”的子集，模型没选（`non_selection`）即使来源/期限复核
  不通过也不追加 dropped 行，避免把“没选”报成“选了被丢”；原因码为空不写；写失败只记日志，不影响采用逻辑。审计按点位与时间把两行配对，就能分辨“选了被丢”。
- **共用复核门是唯一强制接入点**：`decision_service.decision_outcome_is_current` 拆成薄门 + `_adoption_review(...)`，后者返回第一个不通过原因码
  （新增码 `adoption_deadline` / `review_failed`，以及 `_stale` 的既有码 `identity_changed` / `disabled` / `policy_changed`，实验路径另有 `experiment_*` / `connection_changed`；
  `settings_changed` 是 model_selection 的选择原因、`host_shutdown` 是关闭取消路径的码，均不来自 `_stale`），门内在返回 False 前调 `record_decision_dropped`
  （自带“选中才登记”守卫）。另外 10 个消费者点位（delivery_quality、action_candidate、external_material_order、recall、pre_recall、
  planning、skill_tool、curator、curator_relation、skill_proposal_review、subagent_model）的既有丢弃出口按变化性质登记
  **本批新增的 4 个丢弃原因码**（模块级常量，消费者与复核门共用，避免字符串漂移；与 decide 内部的 stale/deadline 不同义）：

  | 原因码 | 含义 | 哪些点位用 |
  | --- | --- | --- |
  | `sources_changed` | 采用前复核发现候选来源/材料/上下文版本变化 | planning、decision_subagent、action_candidate、delivery_quality、external_material_order、skill_proposal_review、curator、curator_relation、recall、pre_recall、decision_recommendation |
  | `runtime_changed` | 采用前复核发现运行环境变化（工具集/后端/策略/主模型身份/任务属性） | decision_recommendation、recall、pre_recall |
  | `adoption_deadline` | 采用期限已过 | planning、decision_subagent、action_candidate、delivery_quality、external_material_order、skill_proposal_review、recall、pre_recall、decision_recommendation + 复核门 `_adoption_reason`（curator、curator_relation 不用此码，只用 `sources_changed`） |
  | `review_failed` | 采用前复核自身抛异常（兜底） | 复核门 `decision_outcome_is_current` |
  | `identity_changed`（宿主既有码，`_stale` 也在用） | 身份不再匹配 | 复核门 `_adoption_reason` |
- **汇总与展示**：`decision_outcome_summary` 加 `result_categories`（缺字段旧行归 `unrecorded`，按次数降序再按名排序）；丢弃补充行不进 `points`/`not_sent`（不是一次独立调用），
  只进类别统计与 recent。`result_category_label` 是唯一翻译函数，两个展示面共用：`audit_records` 的 recent 行加 `result_category_label`（保留结构化 `result_category` 供机器读），
  TUI 决策菜单 `_diagnosis` 行尾显示次数最多的一项（“最近结果：…”），旧记录显示“未记录”。
- **与 `_record_retained` 的边界**：`gateway_model_adoption` 的 `candidate_validation_unavailable`、`first_request_not_selected`、`candidate_rejected_before_provider` 等
  属于“候选保留/未提交给模型”，不是“建议被丢”，这次不动、也不并进 `dropped:`；两者语义不同，文档在 `DECISION_AUDIT_AND_ADMIN_CONTROLS.md` 里写明。
- **验证**：见 TESTS.md 同名节；变异验证 5 个（全部被现有测试拦下并还原）。
- **返工（2026-10-02，3a 审查后追加提交）**：
  - 丢弃记录去重：`external_material_order` 的唯一强制门改由调用方直接调 `decision_outcome_is_current`，不通过就 `return ""`
    （门自己已按真实原因码登记一行 dropped），`_material_stale` 只复核来源与期限，不再返回门的原因码 → 同一条建议只记一行。
  - 逐点位结果类别收成一个权威位置：`decision_outcome_summary` 新增 `result_categories_by_point`（按时间窗口全部行、含 dropped 补充行，
    逐点归 `{类别: 次数}`），TUI 决策菜单（`model_profiles._with_point_diagnostics`）与审计（`audit_records._decision_owner_report`）都从这一处取
    再传给 `decision_point_diagnostics` 第 4 参；低频点位在窗口内有结果就显示，不再受最近 20 行限制。删掉 `_category_calls_by_point` 与
    `_label_recent_categories`（recent 行已带 label，是重复逻辑）。
  - 恢复 `decision_recommendation` 文件头三行模块注释（重建时丢失，按 `78fc5c209` 原样恢复）。
  - 代码风格：`decision_recall._StaleStage` 挪到全部 import 之后；`_pre_recall_stale` 注释写实（两处复核都比对主模型，第二次比原口径更严是有意的）；
    `decision_planning._drop` 挪到常量之后；`decision_outcome_log` 的 `# LLM:` 续行改 `#   `。
  - 补测试：强制门不通过时日志恰好一行 dropped（原因码是门给的码）；低频点位早于最近 20 行但在 24 小时窗口内，TUI 与审计诊断都能看到且一致。
  - 变异验证 2 个（本轮）：去掉“不重复登记”、审计诊断不传类别，均被新测试拦下并还原。
- **第二次返工（2026-10-02，be 独立只读审查后追加提交，分支 `worker/ds1-decision-outcome-category-fix2`）**：
  - M1（必须）：`record_decision_dropped` 只给“响应真的选中了候选”登记（`decision_result_category == "selected"`）——模型没选（非选择）即使来源/期限复核在解析选择之前失败也不追加 dropped 行，
    be 探针（external_material_order 三页 not_needed + 采用期限过）不再多出 `('dropped', 'dropped:adoption_deadline')`；强制门 `decision_outcome_is_current` 的登记同走此函数一并生效。
  - S1：`_answer_category` 多题非选择取值去重后按名排序、以 `+` 连接（如 `non_selection:no_match+not_needed`），题数/组合/顺序变化不再产生新键，逐点计数不被拆散。
  - S2（语义）：`selected` 只表示“至少一题选了真实选项”，不等于宿主会采用；采用与否看 finding/status（recall、external_material_order 要求全部题都是排序值才采用，部分非排序时按规则保留原顺序）。
  - S3（说法统一）：`sources_changed`/`runtime_changed`/`adoption_deadline`/`review_failed` 是**新增的 4 个丢弃原因码**（消费者采用前复核的新事实，与 decide 内部 stale/deadline 不同义），
    `identity_changed` 是宿主既有码；`decision_outcome_is_current`/`_adoption_reason` 注释按 `_stale` 实际返回值写实（`identity_changed`/`disabled`/`policy_changed`，实验路径另有 `experiment_*`/`connection_changed`），
    `settings_changed` 是 model_selection 的选择原因、`host_shutdown` 是关闭取消路径的码，均不来自 `_stale`；`_adoption_reason` 的 `"identity_changed"` 字面量改用 `DROP_IDENTITY_CHANGED` 常量。
  - S4（统计语义）：同一次调用在类别统计里记两行（主行 selected + 补充行 dropped），“选中了 N 次”包含后来被丢弃的；被丢弃是选中的子集（M1 修好后成立），补充行仍不进 `points`/`not_sent`/`model_versions`。
- **集成补漏（3a，step17b）**：审计 `scope=current_thread` 时 `points` 按会话过滤，但点位诊断按 owner 统计（`diagnostics_scope=owner`）；类别计数改由 `audit_records_tool._owner_point_categories` 另读一份 owner 范围，与检查/调用次数同口径。`test_decision_transport_timing` 的 recent 行形状断言补上 `result_category`、`result_category_label` 两个展示字段。

## C5 剩余竞态：熔断判定与用户回合登记在同一把车道闸里（2026-10-02，分支 `claude/38-c5-fuse-race`，基于 `claude/3a-step16z` `4c624ecd4`，已实现，并入 step17d）

- **来源**：sol2 只读审查第 1 条。“用户回合在场”的查询只短暂持有登记表锁，之后的 `record_continuation_fuse` 落账不受保护。
  查询返回“不在场”之后、暂停真正落盘之前，进来的用户回合照样会被熔断抢先暂停。sol2 用屏障复现到 paused/3；用户消息写入后变成 paused/0，暂停仍在。
- **做法**：`conversation/run_claim` 给每条车道一把进程内闸（键同登记表，即本会话执行租约文件路径）。
  - 后台 Goal 熔断用 `user_input_turn_gate(store, thread_id)` 持闸，在闸内读“在场”并完成 `record_continuation_fuse` 落账。
  - 用户回合登记（+1）也要过同一把闸。撤销（-1）不经过闸：用户回合离开后再记的空片照常计数。
  - `GoalContinuationDependencies.user_turn_on_lane`（只读查询）换成 `user_turn_gate`（持闸并给出在场与否），运行时接线同步改。
- **判定时点**：熔断取得车道闸的那一刻。
  - 用户回合在这之前登记：这一空片不计入。
  - 在落账之后才登记：暂停已经先发生，用户消息写入后按 D4 只清计数、保留暂停，不靠用户消息自动恢复（守住 D4）。
- **锁顺序**：车道闸 → GoalStore 迁移锁和目标文件锁。
  - 持闸期间不等执行租约，不回调登记；宿主“无进展”提示在闸外发。
  - 登记只持闸做一次计数，不在闸内等租约。
  - 用户回合最多等一次熔断落账（一次目标文件写入）。
- **闸表**：弱值字典，和 `json_io._PathLock` 同一做法。没人持有或等待时自动回收，表只随活跃车道数增长。
- **范围**：只在进程内。登记本来就只在进程内（网关前台与后台调度器同一进程），跨进程没有登记，也就不需要跨进程闸。
- **没做**：sol2 的“建议修”（把领取失败、执行抛错、收尾抛错、心跳启动失败、双等待者、跨会话与 owner 隔离等登记探针固化成测试）这次没做；
  第 2 条 Anthropic 小上限预算区间属于既有缺陷，不在本轮范围。

## IM 出口脱敏了宿主绝对路径时，整条消息末尾统一附一次说明（C14 复核 2c 后续）（2026-10-02，分支 `claude/be-im-path-note`，基于 `claude/3a-step16z` `4c624ecd4`，已实现，并入 step17d）

- **问题**：飞书等对外通道按规则把宿主绝对路径缩成最后一段，插件越界提示就成了“请把插件包放到 main 下再试”，用户不知道这是被脱敏过的路径。
- **做法**（集成方定 (a)：脱敏规则不开例外，做成通用做法）：
  - 新增 `project_host_paths_report`：同一规则，额外返回“这次替换了几处宿主绝对路径”。这是结构化事实，不读正文语义。
  - 新增 `project_message_paths_for_channel`：替换过至少一处时，在末尾附 `HOST_PATH_REDACTION_NOTE`（“路径只显示最后一段，完整路径请在本机 TUI 查看。”）。
  - 只用在整条消息的出口：投递服务 `DeliveryService.deliver`（所有 IM 回复与主动消息共用）、请求历史的最终回复正文与逐条正文、消息工具的投递记录。
  - 摘要片段（审批摘要、权限摘要）与逐段流式进度仍用 `project_host_paths_for_channel`，不附说明，免得一条消息里出现多次。
- **只附一次的保证**：已投影过的正文里不再有宿主绝对路径，说明本身也不含路径；同一条消息再经一次出口时计数为 0，不会重复附加。不靠在正文里搜这句话。
- **不受影响**：TUI、CLI 等本机私有通道仍原样保留完整路径，也不附说明。
- **验证**：见 TESTS.md 同名节。

## J16：屏幕识别——结构化观察 + 动作候选（2026-10-02，已确认设计；片 A/B/C 已集成 step17g，片 D/E/F/G 已集成 step17h）

- **来由**：用户第 13 条要做、要“功能做全”，先像观察模式那样只给建议、不自动点；并把原来“只组合上游公开 MCP 工具”的边界放宽为“别等上游”。3a 审定的设计见 [J16_SCREEN_OBSERVATION](docs/design/J16_SCREEN_OBSERVATION.md)（分支 `claude/ae-j16-screen-observation`，ae 起草）。
- **设计要点**：
  - **新工具**：在自家 Computer Use 适配器里新增 `observe_window`（read_only，审批策略 always），直接用操作系统和库的公开接口（X11/EWMH、Quartz、ScreenCaptureKit、mss、RapidOCR），给出稳定窗口身份、代次、坐标原点和缩放、遮挡、成功状态。还有 `click_candidate`；`type_into_candidate` 随片 G 注册。
  - **三件套共用**：插件线观察三件套抽成 `ObservationBinding`，插件和 MCP 共用，几何是通用扩展，只进归档。
  - **片 F 已实施（2026-10-02，ef，分支 `claude/ef-j16-slice-f`，基于 `claude/3a-step17h` afb15947b）**：真实验收在 Linux 车道容器里用 M3 + 生产同一个 Jev 端点跑了
    四档各一次 prompt（第二轮全部跑通，auto 档宿主以 actor=decision 自动点击、两本账带 decision_ref；第一轮 prompt 写了窗口标题导致 observe_window not_found，两轮都留证据）；
    `/plugins list` 末尾加 MCP 服务段（运行/发布状态、工具数、拒绝原因码），TUI 直连读本进程注册表、Gateway/IM 只借已加载的 owner 实例。真实验收暴露的可用性问题
    （模型把窗口标题传给 `observe_window`）按 3a/ae 的规则修了：`window` 接受与展示标题完全相同的唯一匹配、多个同名报 `window_ambiguous`，not_found/ambiguous 带可见窗口清单
    （设计稿第 3 节）。真实验收发现重新观察后 Jev 再选同一按钮、宿主按观察编号幂等又点一次（重复提交）——ae 定、3a 同意后收紧为“同一 run 最多自动执行一次”
    （`AUTO_EXECUTIONS_PER_RUN_MAX_COUNT = 1`，跳过记 `run_limit_reached`，模型自己的动作不计），并把本 run 已自动执行的动作作为外部数据进 Jev 材料（设计稿第 7 节）。
  - **片 D 已实施（2026-10-02，ef，分支 `claude/ef-j16-slice-d`，基于 `claude/3a-step17g` 72ddc2b5c，ae 定规则）**：决策材料多一项归一化粗位置
    （有 region 和 frame 才有，先夹后舍入）；能力开关 `action_candidate_auto_execute_enabled`（默认关，管理员边界）打开且建议已采用时，宿主按结构化条件
    （本机管理员主代理、所选候选恰有一个只凭候选编号的动作、同一观察没被碰过）计划一次 `actor=decision` 的宿主调用，走模型调用同一条执行/审批/记录链；
    归档、`tool_completed` 事件与决策账补充行都带 `actor` 与 `decision_ref`，动作调用的 `observation_action` 事实是幂等依据；失败/拒绝/取消只记账不重试不改选；
    宿主记录不进原生 IR 配对，以 `[host-action-record]` 块进模型上下文。细节见设计稿第 7 节“片 D 实施定稿”。
  - **片 C 已实施（2026-10-02，ef，分支 `claude/ef-j16-slice-c`，叠在片 B 上）**：车道 Xvfb 集成用例（关掉再开、改内容、/stop 中断慢 OCR、闪动光标误判统计 0.15–0.20 只记录不调）；观察处理器放工作线程加锁，宿主马上拿到 CANCELLED、后续观察排在被丢弃的 OCR 之后，不被堵的是事件循环；已取消的调用拿到锁不执行、点击前再查一次取消标记；派生镜像定义与运行脚本待 3a 落位；变异 12/12。
  - **片 B 已实施（2026-10-02，ef，分支 `claude/ef-j16-slice-b`，基于 `claude/3a-step17f` f6b63ab35，ae 定复核细节）**：主配置 `computer_use_observation_enabled`（默认关，管理员边界）；Linux X11 后端 + `observe_window`（read_only、always）/ `click_candidate`（dangerous）；适配器层五项复核按代次找快照不要求最新、通过后立刻点击；快照每窗 4 代、区域摘要 16×8/16 级/≤4 格、遮挡按叠放+override-redirect+边框；适配器改成底层 Server 单一运行路径以满足 isError+structuredContent 合同。细节见设计稿 3.2 节第 4 条。
  - **片 E 已实施（2026-10-02，75，分支 `claude/75-j16-slice-e`，基于 `claude/3a-step17g` `c22e58290`，ae 定设计）**：macOS 后端 `computer_use_macos.MacBackend`（Quartz 列窗、`OnScreenAboveWindow` 取遮挡、ScreenCaptureKit 单窗口截图，拿不到退回 mss 区域截图并带 `capture_fallback{reason}`；窗口不在可分享内容里、用户拒绝授权都不回退）；后端选择 `computer_use_backends.select_backend()`；OCR 共用 `screen_ocr.RapidOcrReader`。核心改两处：所有后端的截图统一返回 `ScreenCapture(buffer, kind, fallback_reason)`（X11 改报 `screen_region`，删掉写死的 `CAPTURE_KIND`），`frame.scale` 取截图自己的像素/点比、复核时 scale 变了判 `stale`；后端主动抛的结构化码原样透传，新增 `screen_recording_not_permitted`（码集合开放，宿主只提升 stale / not_found）。测试防线放测试侧：conftest 会话级把真实框架入口换成直接失败，扫描守卫要求“开观察 + 拉子进程”的测试带车道标记。ae 复审 4 条应修已改：X11 的真实桌面库也只经 `load_real_x11_libraries()` 一个入口、测试里同样拦住（车道容器放行）；扫描守卫递归扫 tests/ 下所有 `.py`；非整数比时 scale 往上挪到“点数 × scale ≥ 像素数”，贴边候选不再被宿主整份拒绝；点击前（复核之前）先查辅助功能权限，没有就 `accessibility_not_permitted`，不给自动执行留假的成功事实。细节见设计稿 3.2 第 5 条。
  - **片 G 已实施（2026-10-02，75，分支 `claude/75-j16-slice-g`，基于 `claude/3a-step17h` `afb15947b`（含片 D），ae 定设计）**：macOS 无障碍控件候选（`computer_use_macos_ax`：只查不弹窗的授权检查、按外框和标题对上 AX 窗口、广度优先读树，深度 16 / 节点 600 / 预算 2 秒 / 消息超时 0.5 秒，屏外子树整棵跳过）与 OCR 合并去重（`screen_ui_candidates`：OCR 面积一半以上落在控件里归最里层控件，key `ax:<n>` / `ocr:<n>`）；新工具 `type_into_candidate`（dangerous，只在后端声明能给控件候选时注册；text 必填，结构上不会被自动执行）：点击 → 等焦点 → AX 全选 → 键盘输入，焦点确认后、打字前各看一次取消，点击之后的失败带 `clicked: true`。控件候选复核改比结构化事实（不比像素），密码框不读值、不给输入，任何控件的值不存不记不回读；控件树不完整时结果顶层带 `ui_tree{status, reason}`。Linux AT-SPI 只评估、写安装说明。细节见设计稿第 6 节“片 G 实施定稿”。
  - **片 A 已实施（2026-10-02，ef，分支 `claude/ef-j16-slice-a`，基于 `claude/3a-step17e` 0dab27111，ae 定边界）**：`tooling/observation_binding.ObservationBinding` 共用三件套，绑定为 None 的 MCP 代理不变；来源字段统一为 `provider_id`；MCP 逐工具声明表 `tool_approvals`（只能更严，never 非法）/ `tool_observations`（v5 同形），坏项整服务拒绝并记客户端 `publication`，未发现只提醒；几何 `frame` / `region` 校验，region 进内容摘要、frame 不进；换代后旧候选判 stale。细节见设计稿第 4 节“片 A 实施定稿”。
  - **两层复核**：宿主先确认是最新观察；适配器再重新采样，核对实例、几何、遮挡和候选区域像素。
  - **决策与执行**：`action_candidate` 生产用 observe；自动执行由能力开关 `action_candidate_auto_execute_enabled` 控制，默认关，只限点击，走同一个 Tool Gateway。
  - **属主范围**：同现有 Computer Use，只限结构化 local/main、Full Access、`computer_use_enabled`，非 local/main 的 owner 看不到也调不了。
- **不新增第三方依赖**：pyobjc 和 python-xlib 已由 `pywinctl` 带入。Linux AT-SPI 只作可选评估，不进默认依赖。
- **测试**：只在 Linux 车道容器里用 Xvfb，不碰用户真实屏幕；真实验收用 M3 + Jev。
- **分工**：ef 做完第 14 条后按 A→G 分片实施，ae 做每片设计评审。给 M3 附截图要复用第 14 条的图片通道。
- **原阻塞记录**（上游 0.3.13 缺稳定窗口身份、截图代次、坐标变换，按窗 OCR 切焦点）保留在 [插件观察候选结构第 7 节](docs/design/PLUGIN_OBSERVATION_CANDIDATES.md#7-j16computer-use--ocr-接入设计与上游合同阻塞2026-10-02)。

## 决策质量基准补齐全部 12 个点位（J12 续）（2026-10-02，分支 `claude/be-bench-more-points`，基于 `claude/3a-step16z` `b35796a60`，已实现，待集成）

- **做了什么**：
  - 给其余 8 个点位补了适配函数和中文用例：planning、external_material_order、model_selection、skill_proposal_review、delivery_quality、action_candidate、skill_tool、subagent_model，共 60 个用例。
  - 都走各点位真实的材料构造代码。subagent_model 会加载真实后端的那一步用 mock 换成用例数据；model_selection 按 apply 模式换上采用时的题面。
- **首轮成绩**：Jev 120 次调用，0 失败；结果已登记进 `results.json`。
  - 7 个达标：planning、model_selection、action_candidate、skill_tool 满分；skill_proposal_review 0.92；subagent_model 0.9 与 delivery_quality 0.8 都刚好压线。
  - **external_material_order 0.61 未达标。**
  - 证据：`~/.my-agent/decision-evidence/j12-quality-bench/*8points*`。
- **external_material_order 的原因与方向（已按改措辞实施，见顶部同名条目，重跑后 36/36）**：
  - 原因与 recall 改措辞前完全相同：无关页答 `not_needed`/`no_match`，而不是 `later`；按现有规则任一非排序回答就整次放弃，所以这些调用在 apply 下都不会给出阅读顺序提示。
  - 方向：照 J12b 改逐页题的候选说明（写明“这一页”的含义、无关页选 later），不做隐式别名；改完重跑本基准。
- **压线点位的错题**：
  - delivery_quality：全部通过且无改动时，Jev 选了最新焦点而不是 not_needed。题面没写“都没问题时选 not_needed”，这条期望偏严；跑后不改期望。
    （已按改题面实施，见顶部 delivery_quality 压线核查条目，重跑后 20/20。）
  - subagent_model：“补一行注释”答 need_data，效果同保留原模型。
  - skill_proposal_review：“密钥泄露立刻轮换”给了 normal。
- **点位默认值一律不变**（全部 off）。哪个点位改成默认打开，由集成方按成绩、阈值和真实收益决定；`test_decision_quality_bench.py` 仍强制“默认打开必须有当前有效且达标的成绩”。
- **验证**：见 TESTS.md 同名节。

## Anthropic 小输出上限的空预算区间（2026-10-02，sol2，`worker/sol2-anthropic-budget`，基于 `b35796a60`，已实施，并入 step17d/真实入口核对）

- **解决问题**：合法小窗口会经工厂派生出 max_tokens=1024，旧夹紧把预算强行抬到 1024，等于输出上限；这是 C7 之前已有的边界缺陷。
- **合同**：预算区间为 `[1024, max_tokens-1024]`。区间为空时不发送 thinking，不抬高输出上限，也不把“未发送”说成服务商已经关闭思考。
- **同源事实**：`reasoning_control._anthropic_budget_decision` 返回冻结预算或 `reasoning_budget_interval_empty`；组包和回执共用此裁决。原因码仅供宿主回执，不进入供应商载荷。
- **回执边界**：配置回执的常规请求上限取 `effective_max_output_tokens`，与工厂同源；具体发送仍使用本次请求上限。无上限的档位说明只写条件规则，强制工具关闭优先级不变。
- **验证**：先红后绿、工厂边界 1024/2047/2048/2049、真实组包/投影的流式和非流式替身、控制服务与参数中心回执、正常大上限及三个独立变异；结果和门禁见 TESTS.md。没有新配置，未做真实供应商验收或 Gateway 操作；早期夹具遗漏的假地址外部传输尝试保留在 TESTS.md。
- 详细合同见 [智能程度第 10 节](docs/design/REASONING_EFFORT.md#10-anthropic-小输出上限的预算边界2026-10-02)。真实供应商及实际 TUI/IM 客户端未验证，由 3a 集成后复核。

## C8/C9：/recover 按编号处置与管理员 owner 历史恢复入口（2026-10-02，分支 `worker/sol56-c8c9-recover`，基于 `claude/3a-step16z` `b35796a60`，审查返工 `1e1ed1040`、尾补 `cee06c42c`，已实现，并入 step17d）

- **C8 指定编号**：`/recover <处置> <编号>` 的编号只接受 `common/opaque_id.py` 规则；写入口在同一个 `BEGIN IMMEDIATE`
  事务里重新定位编号并复核 thread、未关闭 TaskRun、current attempt 仍为 unknown，再复用主链唯一 unknown→recovered CAS。
  不存在、越界、范围内重号、已非 unknown 分别返回 `target_not_found`、`target_out_of_scope`、`target_ambiguous`、`target_not_unknown`，统一沿
  `RUN_RECOVERY_REJECTED`。不带编号时旧行为不变：唯一一条才处置，多条只列清单。
- **C9 owner 历史入口**：仅完整可信的本机 `local/main` 管理员可用 `/recover owner`。只读投影仅取本 owner、
  `tasks.thread_id=''`、current attempt=unknown 的根/子代理编号、角色、开始时间和未确认操作数，不读 title、goal 或会话正文；
  历史 TaskRun 已关闭也不替代 current attempt 的 unknown 权威事实。
- **预览与确认**：`/recover owner <处置>` 生成绑定 owner、处置和完整 `(编号, attempt_id)` 目标集合的 12 位确认码；
  `/recover owner <处置> --confirm <确认码>` 在同一写事务里重读集合，集合变化即 `target_set_changed`、不写库。
  每条仍走共享 CAS，事件带 `recovery_target`、`recovery_source=owner_history`、owner 和确认码；批次事件保存目标/成功/跳过及原因码，
  相同确认重送只读原批次回执，不重复恢复或追加事件。
- **入口与边界**：TUI 仍只把命令原文交给 Gateway，飞书经同一个 `control_service` 分派；正文自述、actor 或 metadata 不授权。
  只有显式处置写库，不新增自动处置、过期规则、静止规则或 TaskRun 树规则，也不跨 owner。
- **审查返工**：按编号写入口在事务前拒绝空白 thread，避免新 IM 会话把 `tasks.thread_id=''` 的 owner 历史当成本线程；
  同范围重号拒绝而不替用户挑；owner 批次按 `success_count` 区分全成、部分成和零成功，部分成功如实返回已提交计数；
  根代理编号回执与主链共用继续提示常量，幂等 LIKE 粗筛已注释其对事件 JSON 默认分隔符的耦合。
- **复审尾补**：纯空白 thread 与空 thread 一样在事务前拒绝；部分成功批次同码重放以“该确认已处理过”开头，复用原计数且不新增事件。
- **验证状态**：临时 runtime.db 的三份聚焦测试 30 项通过；原五个、返工三个与尾补两个变异均被捕获并还原。真实运行中 Gateway、TUI 和飞书未在本分支启动或验证，待集成后复核。

## 插件来源越权时，回执给用户看的说明写明允许放包的目录（C14 复核 2c）（2026-10-02，分支 `claude/be-plugin-source-root`，基于 `claude/3a-step16z` `25882221f`，已实现，并入 step17d）

- **问题**：插件包放在当前 owner 范围外时，允许的根目录只拼在给模型看的异常文字里；用户在 TUI 和 IM 看到的仍是通用的“插件、来源文件或配置无效，或读取未获授权。”。
- **做法**：
  - 安装、更新两个工具共用 `plugin_source_error_envelope(exc, policy)` 生成回执信封：`reason=source_unauthorized`、`source_base`、`allowed_root`。
  - `allowed_root` 只在越权时给，且只取工具手里路径策略冻结的当前 owner 根（`PathAccessPolicy.owner_scope_root`），不含其它 owner 或宿主路径。`PluginSourceError` 本身的参数不变。
  - `plugin_management._reply` 经 `_reason_message` 按结构化 reason 选专用说明：`reason=source_unauthorized` 时用 `source_unauthorized_message` 按信封字段生成，与 `confirmation_required` 同一处、同一写法，不解析输出文本。模型看到的异常文字也用同一个函数生成，两边一句话。
  - 没有 owner 根时（不是按用户隔离的部署），说明改为提示放到当前会话工作区或显式授权的目录。说明不回显用户给的越权路径。
- **验证**：见 TESTS.md 同名节。

## J9：主模型的只读长期记忆检索工具 `memory_search`（P5-A 缺口 2）（2026-10-02，分支 `claude/ae-j9-memory-tool`，基于 `claude/3a-step16z` `25882221f`，已实现，并入 step17d）

- **缺口**：自动召回漏掉的事实，主模型自己查不到（`remember list` 整表列出、不过滤不截断；`session_search` 查的是会话历史）。
- **做法**：新增只读工具 `memory_search`，参数为 `query`、`limit`（1–10，默认 5）、可选 `kind`，返回条目编号、类型、范围、更新时间和前 300 字摘录。
  - 只调记忆模块的检索接口（新增 `search_scoped_candidates_report`，与自动召回同一混合检索）。
  - 检索结果里带结构化事实 `retrieval.mode`（`semantic`/`keyword`/`none`）和降级原因。
- **边界**：
  - 只读：schema 不收其它字段，写参数在执行前就被拒；不写访问信号，结果不进自动召回。
  - 范围：只查当前 owner；scope 与自动召回共用 `runtime_long_term_scope`。
  - 不可用：自动召回被抑制的回合（子代理 task_local、control_plane、记忆总闸关）不可用。
- **开关**：`enable_memory_search_tool`，默认关，属于安全边界。模型不能自己打开，用户可经 `/settings` 或配置文件打开。
- **开销**：语义开着时，每次多 1 次查询嵌入，加上范围内未缓存条目的正文嵌入；没有模型调用。
- 设计见[决策模型接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)“P5-A 缺口 2”节，验证见 TESTS.md 同名节。
- **集成后续**（2026-10-02，分支 `claude/ae-j9-identity-fix`，基于 `6980e5f41`）：
  - 和 P14 修复交叉后，语义用例要配档案编号才能算出空间身份，已补上。
  - 语义通道被关掉时，`fallback_reason` 改为按存储层结构化诊断写明原因（如 `embedding_identity_unavailable`、`semantic_recall_disabled`），不再笼统写 `embedder_unavailable`。

## 召回后排序逐条题的候选措辞修正（J12b）（2026-10-02，分支 `claude/be-recall-criteria`，基于 `claude/3a-step16z` `58c674d46`，已上线 step17a（main de222698b，2026-10-02））

- **问题**：
  - J12 基准里 recall 只有 12/30。对无关或次要的记忆，Jev 回答 `not_needed`/`no_match`，而不是 `later`/`normal`。
  - 任何非排序回答都会让整批保持原顺序，所以重排几乎不会生效。
  - 根源是旧措辞：`not_needed` 写的是“不需要额外重排”，`no_match` 写的是“当前优先级候选不匹配”，Jev 把它们读成“这条记忆用不上”。
- **做法**：只改说明文字，候选键、题目结构和非排序回答的处理都不变。
  - `first`、`normal`、`later` 分别写明“直接相关”“有些关系”“无关或很弱，放到后面、仍保留”。
  - `not_needed`、`no_match`、`abstain` 写明“选它会让整批保持原顺序”，并提示“只是与问题无关请选 later”。
  - 题面写明三档各对应什么情况。
- **为什么不把逐条的 `not_needed`/`no_match` 直接当作 later**：开发铁律规定状态别名不隐式兼容，把一个协议回答悄悄当成另一个，正是被禁止的做法。改措辞只动给模型看的软材料，不动机器语义。
- **结果**：重跑基准，recall 30/30（准确率 1.0，阈值 0.75），Jev 12 次调用全部成功。期望答案没有改。成绩已登记进 `results.json`。
  - 证据：`~/.my-agent/decision-evidence/j12-quality-bench/*recall-reworded*`。
  - 局限：基准只给有明确期望的题计分；未计分题是否也都回答了排序值，这次没有记录。
- **开关**：recall 点位仍默认关闭，生产是否打开由集成方定。
- **验证**：见 TESTS.md 同名节。

## P14 必须修：向量空间身份、同代快照、重建如实计数、实际维度、管理员入口（2026-10-02，分支 `claude/38-p14-embedding-fixes`，基于 `claude/3a-step16z` `f8ae11fe5`，待上线（下一版），未随 step17a 上线）

- **来源**：sol 只读审查 P14（`918285cc1`）的 6 个必须修，外加 4 条建议修。生产还没开嵌入，这批要在任何人打开嵌入之前合入。
- **决策 1：空间身份 = 档案编号 + 线路协议 + 端点摘要 + 模型名，从实际发请求的客户端对象取**（第 1、2 条）。
  - 不加目录里的 provider_id：解析结果里没有它。嵌入请求只由协议、端点、模型和密钥决定；换服务商基本都会换端点，
    端点和模型都相同就是同一个空间。
  - 端点摘要：scheme 和主机转小写、去掉末尾斜杠，路径和查询原样保留；先剥掉 userinfo 再取 sha256。
    换账号或换密钥不算换空间，不会触发无谓的重建，凭据派生值也不进快照。
  - 协议取客户端类上显式声明的 `protocol`，不用类名，改类名不会让所有向量库失配；没声明协议的新客户端拿不到身份，语义通道直接关闭。
  - 档案编号保留在身份里，换档案就要求重建（保守做法）。
- **决策 2：身份建不起来就关闭语义通道**（第 2 条）。
  - `core._memory_semantic_channel` 返回 (None, None)，并记 `MEMORY_EMBEDDING_IDENTITY_UNAVAILABLE`。
  - `VectorStore(identity=None)` 保留为不管理身份的通用模式，只给测试和通用检索用。
  - 这种模式的写入会把快照身份置空，因为它没法为新向量担保；之后带身份的读者会得到 `VECTOR_META_MISSING`。
- **决策 3：单文件快照 + 正式跨进程锁**（第 3 条）。格式 `my-agent.memory-vectors.v2`，字段：schema_version、identity、dim、generation、written_at、items。
  - 写：在 `locked_json_path` 里重读最新快照，裁决身份和维度，再整份原子替换。
    `remove` 不做身份裁决，保留原身份，保证硬删除的隐私清理不会被失配挡住。
  - 读：每次比对 (inode, size, mtime_ns) 指纹，变了就重载；指纹和内容用同一个文件句柄读取。
    `search` 在同一份快照上再裁决一次；前置的 `identity_status` 只是为了省一次付费嵌入。
  - 空库可以被任何身份接管。
  - 文件读不了时：带身份就报 `VECTOR_STORE_UNREADABLE` 并拒绝增量写，等管理员重建；不带身份沿用旧行为，当成空库。
  - 旧格式（整份 id→条目）按“没有身份”读入。P14 第一版的旁路 `memory_vectors.json.meta.json` 不读、不写、不迁移；
    生产没开过嵌入，开发机上如果有可以手动删。
- **决策 4：重建全有或全无，首个失败即停**（第 4 条）。
  - 逐条嵌入，方便准确计数。一条失败后整次重建注定作废，再继续只会白花请求（端点宕机时每条还要等 30 秒超时），所以直接停。
  - 返回 attempted、embedded、failed（嵌入失败条数，0 或 1）、failed_stage（embed 或 write）、failure（原因码或异常类型名）、
    rebuilt（实际写入数）和 vector_count；失败时旧库不动。
  - 代价：如果某条记忆单独嵌不出来（例如超长），重建会一直失败，管理员需要按 failure 先处理那条记忆。
- **决策 5：维度只认实际向量**（第 5 条）。
  - 客户端的 `dim` 只是声明值，不进身份。
  - 同一批写入必须等长；库非空时必须等于快照维度；query 维度不同就报 `VECTOR_DIMENSION_MISMATCH`。
  - 请求里不发 dimensions 参数，因为不是所有兼容端点都支持。
- **决策 6：管理员判定只有一份**（第 6 条）。
  - 新增 `owner_access.is_complete_local_admin_owner`；/settings 的 `_is_admin` 改为调用它。
  - `memory vectors rebuild` 的预览和执行都先过这个判定；被拒时在任何嵌入或读写向量库之前返回 `MEMORY_VECTORS_ADMIN_ONLY`。
  - `memory vectors status` 是只读预览，本轮不加门（3a 只点名了 rebuild）。插件管理和 user_config 工具里还有两处同规则的内联写法，可以后续收拢。
- **建议修**：
  - 7 个原因码登记进 error_taxonomy。
  - YAML 里的重建提示改成 CLI 用法 `my-agent memory vectors rebuild --confirmed`；聊天命令族里没有这个入口。
  - 没有嵌入客户端时，状态预览报真实数量，读不了报 None。
- **结构**：管理动作挪到 `_JsonlMemoryVectorAdminMixin`，召回 mixin 不再变大。对精确底版做告警身份比对：新增 0。
- **未做 / 未验证**：
  - 没调真实嵌入，只用了测试替身和伪端点。
  - TUI 和 IM 里没有 vectors 管理入口（本来就只有 CLI）。
  - 没测真实的多个网关进程同时写同一 owner 的向量库，用“子进程持锁”用例代替。

## J17：Gateway 停机时一并结清进程内 runner worker 与 owner 池 agent 的在途模型调用（2026-10-02，分支 `claude/38-j17-stop-settles-runner-calls`，基于 `claude/3a-step16z` `51e52f04a`，已上线 step17a（main de222698b，2026-10-02））

- **缺口**：停机结清（`call_runtime.settle_open_model_calls_for_shutdown`）原来只结网关 agent 自己的账本。
  - 非 local/main 的 owner（飞书、local/user）走进程内线程派工，每个 runner worker 都新建一个 SimpleAgent，各有一本账；
  - owner 池里各 owner 的作用域 agent 也各有一本账；
  - 这些 agent 和网关同进程，停机会切断它们的在途调用，但账上永远停在 started，没有“被停机中断”的记录。
- **做法**（3a 同意：一个机制一起结清）：
  - `call_runtime` 新增进程内弱引用登记表与 `track_shutdown_ledger(agent)`。只登记 agent 上已存在的账本，不新建；agent 被回收后自动消失。
  - 两个登记点：
    - 每个建好的 runner worker（`runner/worker._attach_worker_runtime`，含按有效配置重建的那个）；
    - owner 池建的作用域 agent（`owner_scoped_pool.build_owner_scoped_agent`）。
  - `settle_open_model_calls_for_shutdown` 先结网关 agent 的账，再结登记表里的账。同一本账出现两次也无妨：`fail_open_calls` 只结仍在途的调用。
  - 原因码、用量按缺报、事件 `gateway_model_calls_interrupted` 都不变，只是多了这些调用；靠 run_id、request_id 区分来源。
- **范围**：
  - 覆盖本进程的账本：网关 agent、进程内 runner worker、owner 池作用域 agent；
  - local/main 的子进程 runner 带 `start_new_session`，网关停了它还在，在自己的生命周期里收口，不在这次范围。
  - kill -9、断电这类非正常退出，仍靠启动对账。
- **验证**：`test_gateway_model_call_shutdown_settlement.py` 加 3 项（原 5 项不变）；5 个变异全部被抓住。见 TESTS.md 同名节。
- **后续**：停机准入栅栏（台账顶部“J17 必须修”节）删掉了 `track_shutdown_ledger` 显式登记，改为账本构造即登记，并在结清时关闭新调用准入。

## Curator 整批提交不再被单条候选或长警告卡死，同一批反复失败会熔断（J13 根因修复）（2026-10-02，分支 `claude/be-curator-identity`，基于 `claude/3a-step16z` `85740cde6`，已上线 step17a（main de222698b，2026-10-02））

- **起因**：
  - J13 在隔离环境真实复测时，同一批输入 3 次 `CURATOR_COMMIT_FAILED`，宿主报错“candidate observation identity was reused for a different typed subject”；生产 10-02 也有一次同类提交失败（原因类型 ValueError）。
  - 提交失败后游标不动，同一批被反复重放，每次都是一次 4–5 万输出 token 的大调用。这是额度被快速吃掉的放大器之一。
- **根因**：观察身份的计算和同身份比对用的规范化口径不一样。
  - 计算身份（`_canonical_observation_id`）时，主题键会转小写，范围键取规范键，而且不算内容。
  - 比对身份（`_same_candidate_identity`）时，用的却是原样主题键和原样范围。
  - 所以同一条消息里两条候选的主题键只差大小写时，身份相同、比对却不一致，于是整批回滚。
- **修法**（共用合并函数仍保持严格）：
  1. **同口径比对**：主题键按 `fold_key` 比较（与 `stable_candidate_id` 一致），范围按规范键比较（与观察身份一致）。只差书写形式的主题按同一观察合并。
  2. **冲突单条剔除**：合并时的身份冲突改抛 `CandidateIdentityConflictError`。它仍是 ValueError，原调用方的全有或全无语义不变。Curator 在生成运行记录前，用同一个合并函数逐条预演（`split_identity_conflicts`），只剔除冲突的那几条，并记警告 `curator_candidate_identity_conflict_dropped:<条数>`、打日志告警；其余照常提交，游标照常推进。
  3. **模型警告先截短限数**：解析时模型警告允许每条 500 字、最多 32 条，运行账却只收 300 字以内、总共 32 条。一条长警告同样会让整批记账失败。现在写运行账前先截到 300 字、留一个位置给宿主警告。
  4. **重放熔断**：
     - 同一起始游标连续 `CURATOR_REPLAY_BREAKER_FAILURE_COUNT`（3）次确定性失败（提交被拒或输出解析失败），state 改记 `CURATOR_REPLAY_BREAKER_OPEN`，退避 `CURATOR_REPLAY_BREAKER_RETRY_SECONDS`（3600 秒）。
     - 退避由唯一的 `curator_failure_retry_seconds` 给出，owner 唤醒发现层同源。
     - 运行账保留真实失败码，并加警告 `curator_replay_breaker_open:<真实失败码>`，打日志告警。
     - 中间有一次成功、或游标变了，就重新计数。网络、额度、超时类失败不计入，它们各自有退避。
- **J13 终态**：
  - “不是严格 JSON”未复现，输出上限维持不变。
  - 依据：隔离环境 7 次大批次全部严格解析，没有截断（`stop_reason=stop`）；生产 09-29 以来 115 次成功也是 0 次解析失败。
  - 真实小批次补测改用官方 DeepSeek（`ff961d14`，与生产 Curator 新档案同款），见真实验收记录。
- **同消息同主题不同内容会被合并（用户 10-02 拍板第 6 条：按内容区分，已按“观察身份含内容指纹”实施，见顶部同名条目；下面是当时的分析）**：
  - **现象**：用户说“我对花生过敏，也对芒果过敏。”，模型提炼出两条候选，主题键都是 `health.allergy`，内容分别是“用户对花生过敏”和“用户对芒果过敏”。观察身份不含内容，第二条被当成第一条的改写合并掉，账本里只剩“花生”。运行显示成功、候选数 2，实际少了一条事实。
  - **为什么观察身份不含内容**：防止同一来源被重放时模型换了说法，被算成新的一次出现，使出现次数虚增（HOT 资格要求至少 3 次独立来源）。
  - **做法一：同批去重时把内容摘要放进观察身份**（例如只在同一批内按规范化正文哈希区分，跨批仍按旧口径）。
    - 好处：花生、芒果各成一条，不依赖模型。
    - 代价：要另外界定“同批的新事实”和“跨批重放的改写”；如果做成全局都带内容摘要，重放改写会虚增出现次数、提前满足 HOT 资格；实现和解释都更复杂。
  - **做法二：要求 `subject_key` 细分**（例如 `health.allergy.peanut`、`health.allergy.mango`）。在 Curator 提示和 schema 说明里写明“同一条消息里的不同事实必须用不同主题键”；宿主再加结构化检查，同批同消息同主题键出现不同内容时记警告。
    - 好处：身份规则不变，出现次数口径不受影响。
    - 代价：依赖模型遵守，不遵守时仍会合并（只是多一条可见警告）；改提示会影响所有提取输出，需要重跑提取质量对照；主题键变细后，召回与晋升里按主题匹配的逻辑也要复核。
- **验证**：见 TESTS.md 同名节。

## 执行器退出且效果未知时保留宿主给的失败类型（2026-10-02，分支 `claude/38-executor-failure-type`，基于 `claude/3a-step16z` `00ec92b77`，已上线 step17a（main de222698b，2026-10-02））

- **来源**：C4 真实核对的顺带发现。`executor_recovery.recover_exited_runner` 在“执行器已退出、有工具效果未确认”时给
  `failure_type=executor_effects_unknown`，但这个值不在 `FailureType` 枚举里。`runner_result_state._apply_unstructured_failure` 只认枚举里的
  类型，于是把它改写成通用 `runner_error`。
  - 父级唤醒里分不出“效果未知、要先核对”；
  - `runner_error` 还属于可自动重跑族，和“已停止自动重跑，等待核对”的本意相反。
    重跑实际被 attempt 的 UNKNOWN 封存挡住，所以以前没有出事。
- **做法**：在 `FailureType` 里正式登记 `EXECUTOR_EFFECTS_UNKNOWN`（不放进可自动重跑族），`recover_exited_runner` 改用枚举值。
  宿主显式给的类型从此原样保留到任务和父级唤醒。
- **验证**：`test_executor_exit_recovery.py` 加 1 项；变异 2 个，都被抓住：
  - 恢复成改前状态；
  - 效果未知时仍记 runner_error。

## P8/P17 验收后续（P18 缺陷修复，2026-10-02，ds2，分支 `worker/ds2-p17-p8-followups`，基于 `a3a4eb28a`，已上线 step17a（main de222698b，2026-10-02））

- **capability 配置文件唯一位置**（最重要）：`default_capability_config_path` 原来有两个候选
  （`<root>/agent_py_agent/config/` 与 `<root>/config/`，哪个存在用哪个），P18 验收发现参数中心在 owner
  原本没有文件时新建在第一个候选、开发模式下第一候选就是随包默认文件（可能被写坏）、有人在 `<root>/config/`
  下放文件会被静默盖住。现定**唯一用户位置 `<owner home>/config/capability_config.yaml`**：
  - `default_capability_config_path(root)` 只返回该位置；运行时读取（`capability_config_for_agent`）用户位置
    存在就读它，否则回落**随包默认**（`bundled_capability_config_path()`，只读、不缓存——建好用户文件后
    下一次调用就能读到）；展示层 `running_value` 与运行时同一解析（`resolve_capability_config_path`）。
  - 参数中心写入永远落在用户位置；capability_path 解析成随包默认时写入口拒绝（新错误码 `CAPABILITY_IS_PACKAGED`）。
  - 旧候选 `<root>/agent_py_agent/config/capability_config.yaml` 不再当用户配置读：存在且内容不是随包默认
    时给结构化告警（挂在 `CapabilityConfig.config_warnings`，/settings 配置告警里能看到），不静默合并；
    内容等于随包默认（开发模式 root=仓库目录）不告警。
- **enable_capability_package_selection free → 边界项**（C16：开不开由用户决定）：从 `_EXTRA_FREE_KEYS`
  移除，加入 `BOUNDARY_KEYS` 与 `USER_SETTINGS_BOUNDARY_KEYS`（照 P12 `memory_curator_model_profile`）——
  用户经 /settings 在 `user_settings_write_scope` 内可改，模型 user_config set 一律按边界拒绝。
- **P8 读取方/归属模块统计排除定义/规范化/登记类文件**：`_memory_coercion`、`services/_normalize`、
  `user_config_capability`、`model_provider_schema` 不再当读取方（P18 发现约 50 个键的读取方落在这类文件上，
  例 `memory_compact_auto_trigger_percent` 显示 user_config_capability、实际是 context_compactor）。
  排除后该键读取方为 `agent/agent_core/runtime/context_compactor`；`log_level` 经 `apply_log_level` 应用
  （定义文件已排除、无直接引用），补显式映射到真实消费方 `runtime_config_env`。其余键按引用最多重选。
- **/settings show 展示修复**：capability 来源键的“用户配置里”算上 capability 文件里的覆盖（`_override_text`）；
  runtime_guard 来源键“能否修改”一行与 set 被拒同说法（“属于 runtime_guard 配置，运行时只读随包文件、
  没有用户覆盖层，改了也不会生效；只能查看和搜索。”）；回执“原来 是默认值/现在是 默认值”去掉多余空格。
- **参数中心新建目录 0700**（新建文件已是 0600）：`_create_config_file` 与 `_ensure_private_file` 的
  `mkdir` 加 `mode=0o700`，已存在目录不改变权限。
- **测试**：每条修复都有用例，经 `execute_settings_control`/`run_settings_control` 真实调度；7 个变异全部抓住
  （唯一位置、旧位置告警、边界项、show 覆盖、runtime_guard 说法、0700、读取方排除）。
- **验证**：相关测试全过（test_parameter_sources 22、test_settings_chat_control 34、test_parameter_metadata、
  capability 相关 323、前端/会话工具 49 等）；门禁与 size_diff 见 TESTS.md P18 后续节。

## P10 常数整改第九批（最后一批，2026-10-02，ds2，分支 `worker/ds2-p10-batch9`，基于 `d55cb266c`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：P10 白名单按模块分批清理，本批是最后一批——白名单主组 groups[0] 剩余全部 41 个常数
  （`agent/backends/` 约 26 个、`agent/settings/` 约 10 个、agent 根/零散几个；`_PROBE_MAX_ATTEMPTS`
  在 http.py 与 vision_capability.py 两处定义），目标 **41→0 清空主组**。
- **做法**：
  - 按生成器后缀表改名 19 个唯一名：数量上限补 `_COUNT` 17 个（`_MAX_RETAINED_CALLS→…_COUNT`、
    `_PROBE_MAX_ATTEMPTS→_PROBE_MAX_ATTEMPT_COUNT` 两处定义同步、`MAX_TEXT_CALLS→MAX_TEXT_CALL_COUNT`、
    `MAX_DELIVERY_ATTEMPTS→MAX_DELIVERY_ATTEMPT_COUNT`、`PROBE_ROUNDS→PROBE_ROUND_COUNT` 等）、
    字符补 `_CHARS` 1 个（`_MAX_ERROR_TEXT→_MAX_ERROR_TEXT_CHARS`）、字节补 `_BYTES` 1 个
    （`INSTALLATION_STATE_LIMIT→INSTALLATION_STATE_LIMIT_BYTES`）；全仓词边界改引用 23 文件 65 处，重 grep 旧名 0 残留；
  - 补上方中文说明 34 处（含 vision_capability.py 的 `_PROBE_MAX_ATTEMPT_COUNT` 补紧邻说明）；
  - 无物理单位 5 个（深度 `MAX_DECISION_JSON_DEPTH`/`_STRICT_JSON_MAX_DEPTH`、换算率 `_JEV_BOUND_TOKENS_PER_QUESTION`、
    除数 `MODEL_OUTPUT_WINDOW_DIVISOR`、比率 `_MIN_RATIO`）只补说明、挪入白名单无单位组；
  - `test_constant_names_unique._ALLOWED` 键 `PROBE_MAX_ATTEMPTS→PROBE_MAX_ATTEMPT_COUNT`。
  - 数值一律不变、不碰其它模块。
- **白名单/目录**：主组 41→0 清空、无单位组 39→44；目录重建 804 项 `--check` 一致。
- **验证**：守卫 13 passed；相关 22 个测试文件全过（累计 492）；guards9 168 passed；import 0 条、ruff 过、
  doc-sync PASS、code-size strict hard=0、diff-check 过、clean-package OK、size_diff 新增 0。详见 TESTS.md 第九批节。

## 决策模型中文质量基准与每个点位的阈值（J12）（2026-10-02，分支 `claude/be-jev-quality-bench`，基于 `claude/3a-step16z` `8ec88808d`，已上线 step17a（main de222698b，2026-10-02））

- **做什么**：
  - 一组固定的中文用例，按点位放在 `scripts/bench/decision_quality/cases/<点位>.json`。
  - 运行器 `scripts/bench/decision_quality_bench.py` 用产品的决策后端直接问 Jev，按每题可接受答案集合打分。
  - `thresholds.json` 给全部 12 个点位定阈值，包括准确率、计分题数和调用失败率；`results.json` 登记各点位最近一次成绩。
- **用例怎么变成请求**：
  - 用例只写结构化输入，适配层 `decision_quality_adapters.py` 把它交给各点位**真实的材料构造代码**，所以发出去的请求和产品逐字节同形。
  - pre_recall 的材料是在补充查询入口里内联拼的，适配层截获这份材料。
  - 成绩里记着用例摘要和材料摘要。构造代码一改，旧成绩就失效。
- **默认打开的前提（强制）**：
  - 随包默认模式不是 `off` 的点位，必须有一份用例与材料摘要都和当前一致、并达到阈值的登记成绩。
  - `test_decision_quality_bench.py` 检查这一条。用户自己在菜单里开哪个点位不受影响。
- **为什么用测试强制**：这条规则管的是开发时“改仓库默认值”的门槛，不是运行时硬门；主链路行为不变。
  只写在文档里，默认值被顺手改掉时没人能看出来。“配置必须真的生效”要求它有结构化检查。
- **阈值怎么定**：跑之前按 apply 模式的危害定，不按结果回调：
  - 会换模型的点位（选模、子代理选模）要 0.9；
  - 往上下文里加东西或会引导替换的点位（pre_recall、curator_relation）以及能力推荐（skill_tool）要 0.85；
  - 只排序或只加提示的点位要 0.75—0.8。
- **首轮真实成绩**（Jev 52 次，全部成功，`jev-1.13.0`）：

  | 点位 | 准确率 | 结论 |
  | --- | --- | --- |
  | pre_recall | 14/16 | 达标 |
  | curator | 38/38 | 达标 |
  | curator_relation | 32/32 | 达标 |
  | recall | 12/30 | **未达标** |

  证据：`~/.my-agent/decision-evidence/j12-quality-bench/`。
- **recall 未达标的原因与方向（已按改措辞实施，见顶部 J12b 条目，重跑后 30/30）**：
  - 对无关或次要的记忆，Jev 回答 `not_needed`/`no_match`，而不是 `later`/`normal`。
  - 按现有规则，有任一非排序回答就整次保留原顺序，所以这 12 次在 apply 下一次都不会重排。
  - 根源是逐条记忆题的候选措辞：`not_needed` 写的是“不需要额外重排”，Jev 读成“这条用不上”。
  - 方向：改逐条题的非排序候选措辞，或把逐条的 `not_needed`/`no_match` 当作“稍后参考”。两种都会改变产品行为，要单独做并重跑本基准。
- **没有用例的点位**：model_selection、subagent_model、skill_tool、planning、delivery_quality、action_candidate、external_material_order、skill_proposal_review 只有阈值，因此不能默认打开。
  适配层有现成入口：盘点结论是除 subagent_model 外都能用轻量假对象调用真实构造函数。
- **局限**：用例是测试方编写的合成样本，数量少，不能外推为线上整体质量。达标只是默认打开的必要条件，还要看真实收益与代价。

## C14 第一批两个插件验收后续：确认回执、越界提示、管理文案、跳过语义与说明对齐（2026-10-02，ds1，分支 `worker/ds1-c14-followups`，基于 `claude/3a-step16z` `d55cb266c`，已上线 step17a（main de222698b，2026-10-02））

针对 C14 验收记录（docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md C14 节）里 ae 审查提出的建议逐条收尾，均不挡已通过的验收，但要做到位：

1. **非 Python 插件 enable 的确认预览回执**：之前末尾会显示"错误码：TOOL_INVALID_ARGUMENTS"，像失败。
   - 结构化判定只用 `details.reason == "confirmation_required"`；回执带自己的状态 `state = "confirmation_required"`，文本不再追加通用错误码。
   - error_taxonomy.py 正式登记 `PLUGIN_CONFIRMATION_REQUIRED`（category=state、retryable=True、RecoveryAction.REQUEST_USER_INPUT），不拿文案判断，也不再像参数错误。
   - TUI（plugin_http_response）与 IM（execute_plugin_control 持久控制回执）都经 `plugin_command_service._log_rejection` 统一生效。
2. **安装包越界提示**：包放在 owner 允许范围外时不再只报"读取未获授权"，改用路径策略的结构化事实（`policy.owner_scope_root`）告诉用户当前允许放置插件包的根目录；只有路径策略本身可用时给出该目录，否则提示放到当前会话工作区（或显式授权目录）。不泄露其它 owner 或宿主的私有路径。
2b. **C10 管理动作文案**：普通用户看 `/plugins` 与 `/plugins help install` 时，被权限拒的管理动作不再标"（尚未开放）"，按与 PluginManagement 授权判定同一来源的结构化事实（当前身份是否管理员）标"（仅管理员可用）"；真正未开放的动作仍标"（尚未开放）"。TUI 与 IM 两个入口一致。
3. **shuohao-novel-gates 跳过语义**：按数据内容跳过的门不再报"通过"——cast 没有名字 → no-names 跳过；大纲没有 props 字段 → prop-cap 跳过；shots 为空数组 → shot-recipe 跳过（上游对 --shots 目录要求至少一张卡）。跳过就报 skipped，不计通过；declaration.json 的 shots input_schema 加 `minItems: 1`。
4. **drama-media-shell 说明与元数据对齐实际行为**：
   - `production_collect` 说明改为"重新生成同一份夹具字节"，行为不变；
   - `production_prepare`/`production_confirm` 保留 read_only 声明（宿主合同里 read_only 只管工作区写权限，见 docs/design/PLUGIN_WORKSPACE_WRITE.md），说明补写清会写插件私有数据目录的作业记录/一次性确认回执；
   - PROVENANCE 补注 MP4 夹具是本仓新增的最小 ftyp isom 容器字节，不是上游的；
   - pyproject.toml 许可元数据改为 `Apache-2.0 AND MIT`（包装代码 Apache-2.0、随包上游 MIT）。
5. **待定（不改行为，已记账）**：确认码由 prepare 直接返回，模型自己就能 confirm。当前不接付费生成（已定做法第 8 条），所以暂不处理；但接任何真实供应商之前，必须改成宿主层面的本人确认（确认码只经宿主回执给本人、模型不可直接持有并自行 confirm），改之前不得接入付费链路。

- **验证**：宿主侧 141 个相关测试全过（test_plugins_chat_control、test_gateway_plugin_commands、test_plugin_management、test_plugin_commands、test_plugin_command_catalog）；shuohao 33 passed、1 failed、1 skipped；drama 包 9 passed。具体命令与结果见 TESTS.md 同名节。宿主安装/启用与沙箱子进程类用例在沙箱里失败/跳过（环境限制），由 3a 在沙箱外复核。

## 补充查询片段材料：先预检每个片段能新增的事实（J8，P5-A 缺口 1）（2026-10-02，分支 `claude/be-jev-snippet-facts`，基于 `claude/3a-step16z` `918285cc1`，已上线 step17a（main de222698b，2026-10-02））

- **问题**：09-25 语义召回实验里，K3a 两批都选了主题已被原召回覆盖的片段，补不出东西。原因是 Jev 只看得到基线摘要和片段文字，不知道哪个片段真能补出新事实。
- **做法**：
  - 新增 `points.pre_recall.fragment_material`。默认 `query_text`，只给片段文字，请求与改动前逐字节相同。
  - 设成 `with_new_facts` 时，宿主在问 Jev 之前，用与采用时同一套候选检索、同一套名额/字数截取，预检每个片段选中后会追加哪些正式事实。预检用 `search_scoped_candidates`，不记访问。
  - 只把能新增事实的片段留作选项；请求 `state` 里加 `fragment_additions`（每个片段的新增条数与摘要）。
  - 全都补不出就不调 Jev，到达诊断记 `no_new_facts`；预检检索出错记 `preview_failed`。
  - 采用时直接用 Jev 看到的那份新增事实。基线核对未变后，最终仍经 `confirm_scoped_access` 重读正式源确认。
  - 配置 `memory_decision_pre_recall_fragment_material` 默认 `query_text`。owner 与会话两层都能覆盖：TUI 决策菜单有“片段材料”单选，IM 经 my-agent 的 `user_config decision_patch`。
- **为什么不做“有空槽就按阈值确定性补位”**：确定性补位会把检索命中但和问题无关的事实直接塞进上下文。交给 Jev 至少有一次看摘要、选不补的机会；Jev 仍不能删改原召回。
- **代价**：开着时每个片段多一次候选检索。语义召回下每次多一次查询嵌入，事实向量已有正文哈希缓存；最多 4 个片段。请求多出预检材料，实测约 +40%—50%（约 450 字节）。
- **真实对照**（`~/.my-agent/decision-evidence/j8-fragment-material/`）：
  - 环境：隔离 home，真实 Jev。只把候选检索写死成模拟语义召回的结果，因为嵌入模型不在授权名单；5 个样本 × 2 种材料 × 2 遍。
  - 能补出目标的三个样本，两种材料下 Jev 都选对，K3a 那种失误没有复现，所以选择准确率的提升没有被证明。
  - 确定的收益有两点：被覆盖的片段不再出现在选项里；没有可补时不调 Jev，对照样本每轮省 1 次调用、约 1.4 秒。
  - 只能补出不太相关事实的干扰样本里，Jev 两种材料下都选了它。
  - 语义召回下的端到端效果未验证。
- **打开与关闭**：仓库默认 `query_text`；生产不打开（3a 10-02）。要试用时 owner 级 `decision_patch`：`{"changes":{"points.pre_recall.fragment_material":"with_new_facts"}}`。
- **验证**：见 TESTS.md 同名节。

## P10 常数整改第八批：capability 目录 49 条常数合规（2026-10-02，ds1，分支 `worker/ds1-p10-batch8`，基于 `f0bb62254`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：P10 白名单按模块分批清理。本批接第七批之后，范围是 `agent_py_agent/agent/capability/` 目录（16 个文件）
  的 49 条待整改常数，不碰其它目录（其它会话并行处理各自的批）。
- **做法**：
  1. **A 补说明 28 个**：已有 `_CHARS/_SECONDS/_BYTES/_PERCENT/_TOKENS/_REQUESTS` 等单位后缀、缺上方中文说明的，
     在定义上方补一句人话（含 `_APPROX_BYTES_PER_TOKEN` 每 token 字节估算）。
  2. **B 改名 20 个**：按 `_UNIT_SUFFIXES` 后缀表补单位改名——数量类复数→单数+`_COUNT`
     （`_MAX_ATTACHMENTS→_MAX_ATTACHMENT_COUNT`、`_MAX_EVIDENCE_REFS→_MAX_EVIDENCE_REF_COUNT`、
     `MAX_REQUEST_ATTEMPTS→MAX_REQUEST_ATTEMPT_COUNT`、`MAX_SOURCE_RUNS→MAX_SOURCE_RUN_COUNT`、
     `_MAX_SKILLS_PER_ROOT→_MAX_SKILLS_PER_ROOT_COUNT`、`TRACE_LIMIT→TRACE_COUNT` 等），
     字符类 `_BASELINE_NAME_LIMIT→_BASELINE_NAME_LIMIT_CHARS`、`_CANDIDATE_NAME_LIMIT→_CANDIDATE_NAME_LIMIT_CHARS`，
     `_DEFAULT_SKILL_METADATA_CHAR_BUDGET→_DEFAULT_SKILL_METADATA_MAX_CHARS`（预算语义保留，后缀归 `_CHARS`）。
     全仓引用（含 import、`__all__`、模块注释）词边界一起改，旧名在 capability 范围零残留。
  3. **同名异义处理**：`_MAX_LIMIT`/`_MAX_WINDOW` 在 `agent/tooling/audit_records_tool.py` 与
     `cli/chat_parts/tui_subscription_models.py` 还有同名但不同义的元组解包常数（不在目录、不在白名单、
     守卫按单赋值扫描不覆盖）。词边界替换按文件限定跳过这两个文件，避免第七批误伤 audit_records_tool 的重演。
  4. **C 无物理单位 1 个**：`_MAX_SCAN_DEPTH`（扫描深度）只补说明、挪入白名单无单位组。
- **白名单/目录**：主组 164→115（删 49 个）、无单位组 36→37（+1）；目录重建 **803 项** `--check` 一致。数值一律不变。

## C4：执行器退出后父级唤醒回执里的结构化接替提示（2026-10-02，分支 `claude/38-c4-takeover-hint`，基于 `claude/3a-step16z` `e0a6d53af`，已实现，开关默认关）

- **来源**：O1 待定项“结构化接替提示”。被 SIGKILL 的子代理只以 BLOCKED 通知父级，`blocked_reason` 是子代理自己写的，被杀时为空；
  宿主侧的事实是执行器退出（`executor_process_died`）。O1 复测 4 次，模型都没有在 `create_subagents` 里声明 `replacement_for_run_ids`。
- **做法**（通用合同层，不针对任何具体任务）：
  - 合同 `contracts/subagent_completion.py`：
    - `subagent_takeover_hint(run_id, exit_reason, uncertain_effects)` 生成固定形状：版本、run_id、退出原因码、是否有未确认效果，
      以及 `declare_with={tool: create_subagents, replacement_for_run_ids: [run_id]}`；
    - `completion_takeover_hint_facts` 校验版本、非空字段，并按固定形状重建，不复制其它键。
  - 宿主写入：只在 `executor_recovery.recover_exited_runner`（宿主证实执行器已退出、这一轮没有结果）且开关打开时，随这一份结果附提示；
    结果服务每次写回都“有就写、没有就清”（`record_takeover_hint`），所以提示只属于那一份结果。
  - 交出：完成信封 `completion_handoff_payload` 只在 BLOCKED/FAILED 时带出，受控取消等其它通知不带旧提示。
  - 消费方共用同一合同投影（合同要求字段变更同步所有消费方）：
    - 生命周期唤醒事件（父级模型看到的回执，`lifecycle_wake_event._CHILD_FIELDS`）；
    - 观察摘要的一句话（哪个 run、原因码、怎么声明接替；有未确认效果时加“接手前先核对”）；
    - 前台活动回合事件、后台完成清单、递归父级的直属孩子快照。
  - 开关 `subagent_takeover_hint_enabled`（`capability_config.yaml` 与 `CapabilityConfig`，默认 false）：
    - 由执行器退出回收扫描（`capability_auto_sweep._reclaim_dead_running_runs`）经唯一 capability 快照读取；
    - 关闭时唤醒回执与改前一字不差；
    - 前端参数目录已重新生成。
- **真实模型核对**（MiniMax-M2.7，隔离 home，场景与 O1 场景 A 相同、提示原文相同，证据
  `~/.my-agent/decision-evidence/c4-takeover-hint-20261002/`）：
  - 第 1 次是测试侧失误：开关文件放错了目录，网关代理根是 owner home，所以实际按关闭运行，当作基线记录。
    结果是父级另派了新子代理，但没有声明接替；原子代理一直 BLOCKED，运行账 unknown。
  - 第 2 次开关生效，唤醒里带了完整提示。父级没有声明接替（**接替声明未命中，0/1**），而是按提示里的“接手前先核对”去读
    子代理报告和 `ws/notes/child-note.txt`。文件在被杀前已经写好，所以它更新进度后结束，没有另派。
  - 这个杀点（写完之后、结果落账之前）本身不需要接替。要验证“声明接替”这条路，需要换一个工作没做完的杀点，那是另一个场景。
  - 第 3 次（3a 指定的新场景，宿主侧故障注入，只发一次）：h1 杀点，在子代理执行器登记之前就 SIGKILL，开关生效。
    - 宿主把子代理收成 FAILED（executor_process_died，没有未确认效果），唤醒里带提示。
    - 父级唤醒片在 `create_subagents` 里声明了 `replacement_for_run_ids=[被杀子代理]`，**接替声明命中（1/1）**。合计 u2 0/1、h1 1/1。
  - 同一次运行把 O1 一起核对了（ae 不用另测），用产品 `SubAgentManager` 只读核对：
    - 来源 TAKEN_OVER，`takeover_by` 是新子代理，`taken_over_successor` 一致；
    - 新子代理 DONE，TaskRun 在它完成后关闭（done）。
    - “来源运行账收成 cancelled”这一支不适用：h1 杀在任何工具之前，来源接替前已是 FAILED，运行账已经是终态 failed，
      `settle_taken_over_run` 按设计不改已终态的运行。这一支只对执行轮未终态的 BLOCKED 来源生效，真实运行仍只有合同单测覆盖。
- **顺带发现（2026-10-02 已修，见上方“执行器退出且效果未知时保留宿主给的失败类型”）**：`recover_exited_runner` 传的 `failure_type=executor_effects_unknown` 不在 `FailureType` 枚举里，被
  `runner_result_state._apply_unstructured_failure` 改成了 `runner_error`，所以唤醒里的 failure_type 不能区分“未知效果”。接替提示里
  `uncertain_effects` 已带这个事实，所以不影响 C4。
- **验证**：`test_subagent_takeover_hint.py` 8 项；13 个变异全部被抓住。见 TESTS.md 同名节。

## /settings internal 与 P17 统一调度的接缝（2026-10-02，3a，step16z，已修）

- P18 真实验收发现 `/settings internal` 在 TUI 和飞书都被兜底成“参数暂时读不到”：P17 给所有子命令统一传 `capability_path`，
  P10 新加的 `_internal` 没接这个参数。约定：`/settings` 子命令处理函数一律接受同一组关键字参数（用不到也要接住），
  新子命令要经 `run_settings_control` 真实调度测一次，不能只直接调处理函数。

## P10 常数整改第七批：agent_core 目录 64 条常数合规（2026-10-02，ds1，分支 `worker/ds1-p10-batch7`，基于 `b3195b809`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：P10 白名单按模块分批清理。本批接前几批之后，范围是 `agent_py_agent/agent/agent_core/` 目录（34 个文件）
  的 64 条目录条目（61 个唯一名字），不碰其它目录（其它会话并行处理各自的批）。
- **做法**：
  1. **A 补说明 20 个**：已有 `_CHARS/_SECONDS/_PERCENT/_TURNS/_BYTES` 等单位后缀、缺上方中文说明的，在定义上方补一句人话。
  2. **B 改名 40 个**：按 `_UNIT_SUFFIXES` 后缀表补单位改名——数量类 `_LIMIT`→`_COUNT`（`PENDING_TURN_INPUT_INVALIDATION_LIMIT→…_COUNT`、
     `_ISSUE_LIMIT→_ISSUE_COUNT`、`DEFAULT_HARD_FAILURE_HALT_THRESHOLD→…_THRESHOLD_COUNT`、`_MAX_RECORDS→_MAX_RECORD_COUNT` 等），
     秒类 `_MAX_TIMEOUT→_MAX_TIMEOUT_SECONDS`，字符类 `_REF_TEXT_LIMIT→_REF_TEXT_LIMIT_CHARS`、`_MAX_INLINE_JSON→_MAX_INLINE_JSON_CHARS`，
     token 类 `DEFAULT_COMPACT_RECENT_TAIL_TOKEN_CAP→DEFAULT_COMPACT_RECENT_TAIL_MAX_TOKENS`（沿用本文件
     `DEFAULT_COMPACT_TRIGGER_MAX_TOKENS` 风格）；`_MAX_CANDIDATES→_MAX_ARTIFACT_CANDIDATE_COUNT` 避开
     `agent/plugin_observation.py` 既有 `MAX_CANDIDATE_COUNT`（观察候选 64 vs 产物候选 128，不同义，起具体名）。
     全仓引用（tests、import、`__all__`、注释）词边界一起改，旧名零残留。
  3. **C 无物理单位 4 个**：`DYNAMIC_TIMEOUT_SAFETY_MARGIN`（倍数）、`_PTL_DROP_FRACTION`（比率）、
     `_DEFAULT_MAX_COMPACT_AUTO_CONTINUE_DEPTH`/`_DEFAULT_DEPTH`（深度）只补说明，挪入白名单无单位组。
  4. 数值一律不变；`test_constant_names_unique._ALLOWED` 把 `MAX_INLINE_JSON` 键名随改名更新为 `MAX_INLINE_JSON_CHARS`（两个文件的内联 JSON 上限仍是不同点位）。
- **白名单/目录**：主组 225→164、无单位组 32→36（只减不增）；目录重建 **802 项** `--check` 一致。
- **验证**：目录守卫 13 passed；引用改名的 16 个测试文件 217 passed；guards9 168 passed；import boundaries 0；
  ruff/doc_sync/code-size strict/diff --check/clean_package 全过；size_diff 新增告警 0（消失 2）。

## 配置与账本写回保留权限，参数中心新建文件一律 0600（2026-10-02，3a，step16z，已实现；生产权限已手工收回）

- **来源**：ae 做 P18 时发现，`/settings set` 后隔离环境的 gateway.yaml 从 600 变成 644，新建的 settings-changes.jsonl 也是 644。
  查生产（只看权限位）：`~/.my-agent/config/desktop.yaml`（含飞书 app secret）已经是 644，账本也是 644；9/27、9/29 的备份仍是 600，
  说明是之后某次参数写回放宽的。config 目录本身是 700，其它本机用户进不去，但文件本身不该放宽。
- **根因**：`config_io._write_config_lines` 和 `set_simple_yaml_value` 用 `write_text` 写固定名 `<name>.tmp`（权限随 umask，644），
  再替换原文件，原文件的 600 就丢了；固定临时名在并发写或同名残留时也会出错。`json_io` 的原子写（JSON、JSONL 账本）同样让目标
  继承临时文件权限，所以账本每次整文件重写都会变回 644。参数中心新建配置文件、账本时也按 umask 建。
- **做法**（通用，不针对某个文件）：
  - `json_io._replace_with_retry` 替换前先把目标原有的权限位抄到临时文件（`_keep_target_mode`），所有原子写共用；目标不存在时不改。
  - `config_io._write_config_lines` 改用同目录 `mkstemp`（0600 起步）+ 抄原权限 + `os.replace`；`set_simple_yaml_value` 也走它。
  - 参数中心新建配置文件（`_create_config_file`）和首次建账本（`_ensure_private_file`）一律 0600。
- **生产处置**（3a，10-02 01:2x）：`chmod 600` desktop.yaml、settings-changes.jsonl、desktop.yaml 旧备份、shared-model-profiles.json、
  模型档案的修改账本与推理探测文件、config/tests 下的测试配置；复查 config 目录下没有组/其他可读的文件。
- **验证**：`test_config_write_permissions.py` 5 项；变异 5 个全抓到（不抄权限、json_io 不保留、账本 0644、回到固定 .tmp 名、配置新建 0644）。

## C5/O4：持续目标空片熔断时用户消息先处理（2026-10-02，分支 `claude/38-c5-user-msg-before-fuse`，基于 `claude/3a-step16z` `f9ca83242`，已上线 step17a（main de222698b，2026-10-02））

- **现象**（真实链路复现，代码 `f9ca83242`，隔离 Gateway + TUI + 脚本化假模型）：
  - 第 3 个空片还在跑时，用户从 TUI 发来一条消息；
  - 网关先把它当插话绑到后台任务，约 0.8 秒后对账把插话判为没被领取（插话回执 rejected、从没提交给模型），消息转成下一轮排队；
  - 第 3 片结束记账时熔断暂停（idle 3）；用户回合随后才写入会话，按 D4 只清计数；
  - 结果：目标仍暂停、原因码还在，用户还得 `/goal resume`。
- **规则：用户消息先处理。**
  - 空片结束记账时，如果本会话执行车道上有携带用户消息的回合（排队中或执行中），这一片不计入：不记账，计数保持原值，续跑链照常接下一片。
  - 有进展的片照常记账清零。
  - 计数清零仍按 D4：用户消息写入会话后清零；写不进去就不清（D4 原有用例不变）。已经暂停的目标仍只由 `/goal resume` 恢复。
- **做法**：
  - `conversation/run_claim.py`：`ConversationRunLaneRequest` 新增 `carries_user_input`（缺省 False）。
    - 带这个标记的回合，从开始排队到退出车道，在进程内登记“用户回合在场”，键是本会话执行租约文件路径；
    - 新查询 `user_input_turn_on_lane(store, thread_id)`；
    - 原来持有车道的逻辑挪到 `_held_conversation_run_lane`，执行权语义不变。
  - `gateway_parts/request_binding.gateway_conversation_execution_lane`：网关前台车道标 `carries_user_input=True`。
    - 网关前台请求都是用户提交：TUI/IM/HTTP、scale 转发，以及恢复重跑同一条请求（本次重新核对了收件箱写入方）；
    - 手动 Compact 车道不标。
  - `conversation/background_goal._record_continuation_slice`：空片且用户回合在场就直接返回、不记账。
    - 判断经 `GoalContinuationDependencies.user_turn_on_lane`；
    - 由 `conversation/runtime._background_goal_dependencies` 接到同一 store 的车道登记。
- **为什么登记要一直保持到退出车道**：后台片在 `run_once` 里就释放车道，调度器之后才记账。这段空档里，用户回合可能已经拿到车道。
  如果只在排队期间登记，就会漏掉这种情况（变异 M3 验证过）。
- **为什么不在受理时提前清零**：D4 定了“写不进会话就不清”（`test_unpersisted_gateway_message_does_not_reset_the_count`），提前清零会破坏这条。
  所以改成“这一片不计入”：消息写进会话才清零。
- **不加配置开关**：这只是宿主记账口径，不改模型请求，也不多调模型。最多让排队期间结束的空片不计数；用户回合写入后照常按 D4 清零。
  与 D4 一样，按缺陷修复处理。
- **边界**：
  - 登记只在本进程内。网关请求工作线程和后台调度跑在同一个网关进程里（真实链路是同一个 pid）；不在同一进程时查不到登记，行为与改前相同。
  - 车道没有优先级。用户回合每 0.05 秒抢一次车道，下一片要等调度器的下一次 tick（复现里约 0.9 秒），实际总是用户回合先拿到。
    就算后台先拿到，那一片结束时用户回合仍在场，同样不计入。
  - **没处理**：插话在被拒之前就被后台片后续的模型调用领走。这时消息在那一片里已经处理，不走网关前台回合，那一片按自己有无进展记账。
    这种情况在 D4 里本来就不清零，这次没复现，也没改，留待定。
- **验证**：
  - 合同测试 3 项（`test_goal_fuse_user_turn_first.py`），网关端到端 1 项（`test_gateway_goal_fuse_reset.py`）；
  - 端到端用真实 echo Gateway、真实后台调度器和真实车道等待，按 O4 的先后固定顺序；
  - 7 个变异全部被抓住；
  - 真实链路改前、改后对照见证据 `~/.my-agent/decision-evidence/c5-o4-user-msg-first-20261002/`（仓库外）：
    改后第 3 片记账时 `user_turn_on_lane=True`、不记账；用户回合写入后清零；之后第 4、5、6 片计 1、2、3，第 6 片熔断，熔断照常有效。

## P10 常数整改第六批（2026-10-02，ds2，分支 `worker/ds2-p10-batch6`，基于 `aea277a92`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：P10 白名单按模块分批清理。本批范围是 `agent_py_agent/agent/tooling/` 目录 74 个待整改常数
  （30 个文件、73 个唯一名，`_MAX_OWNERS` 在 admin_controls_tool.py 与 audit_records_tool.py 两处定义）。
- **做法**：
  - 按生成器后缀表改名 29 个唯一名：数量上限类补 `_COUNT` 24 个（`_MAX_OWNERS→_MAX_OWNERS_COUNT`、
    `_MAX_SESSIONS→_MAX_SESSION_COUNT`、`_NEAR_NAME_MAX_SUGGESTIONS→_NEAR_NAME_MAX_SUGGESTION_COUNT` 等），
    字符/长度类补 `_CHARS` 1 个（`_NEAR_NAME_MAX_NAME_LENGTH→_NEAR_NAME_MAX_NAME_LENGTH_CHARS`），
    时间类补 `_SECONDS` 4 个（`_DEFAULT_CONNECT_TIMEOUT→…_SECONDS`、`_FINAL_CONFIRM_SECONDS_WITH_HANDLE→_FINAL_CONFIRM_WITH_HANDLE_SECONDS` 等）；
  - 已有单位后缀 42 个只补上方中文说明；无物理单位 2 个（`_FIREWALL_NOT_RUNNING` firewall-cmd 退出码协议值、
    `_MAX_DISCOVERY_DEPTH` 深度）只补说明、挪入白名单无单位组。
  - 数值一律不变、不碰其它模块；改名引用全仓同步（脚本 tmp/sync-batch6-refs.py，词边界、按长度降序，22 文件 98 处），
    重 grep 旧名 0 残留；`test_constant_names_unique._ALLOWED` 键 `MAX_OWNERS→MAX_OWNERS_COUNT`。
- **白名单/目录**：待整改 225→152（groups[0]，只减不增），无物理单位组 32→34；目录重建 802 项 `--check` 一致。
- **验证**：守卫 13 passed；guards9 168 passed；相关单元测试全过；import 0 条、ruff 过、doc-sync PASS、
  code-size strict hard=0、diff-check 过、clean-package OK、size_diff 新增 0。真实子进程类测试在沙箱失败/超时
  （基线 aea277a92 同样失败，环境限制，3a 沙箱外复核）。详见 TESTS.md 第六批节。

## 接替自停的结束原因口径（2026-10-02，分支 `claude/38-takeover-stop-reason`，基于 `claude/3a-step16z` `6f09b1853`，已上线 step17a（main de222698b，2026-10-02））

- **来源**：C12d 的留意项。运行中的子代理被接替后由 runner 自停，运行账 `agent_run.completed` 事件记的是
  `runtime_reason=InterruptedError`、`runtime_source` 为空；静止来源走 `settle_taken_over_run` 记的是 `subagent_takeover`／`taken_over`。
  同一件事两种写法，审计时要分别认。
- **做法**：
  - `agent_core/runtime_mixin._settle_main_agent_run_exception` 改为经新函数 `_exception_closeout_reason` 取原因和来源；
  - 只有“中断异常，且 `subagents.taken_over_successor(run_id)` 返回接替者”时写 `taken_over`／`subagent_takeover`
    （来源常量复用 `runtime_db/run_takeover.TAKEOVER_RUNTIME_SOURCE`）；
  - 主代理、子代理记录读不到、普通中断、其它异常都照旧记异常类名、来源为空；
  - 只改这两个结构化字段：运行状态仍是 cancelled，状态机和 runner 的停止判据都不变；读失败回落类名，不会盖住原异常。
- **判据选择**：用 `taken_over_successor`，不直接比 `status == TAKEN_OVER`。它是“这个 run 是否被接替”的唯一判据，
  `record_takeover` 的运行账收口和网关 `/recover` 都用它。拆分产生的 TAKEN_OVER（`split_into`，没有 `takeover_by`）不算接替，
  静止来源那边也不按接替补账，两边一致。
- **验证**：`test_subagent_takeover_runtime_closeout.py` 加 1 项（被接替后自停 → 接替口径；普通中断 → 仍记 `InterruptedError`）；
  3 个变异全部被杀。见 TESTS.md 同名节。

## C12e：可执行插件工具的审批前复核与批准后复核（2026-10-02，分支 `claude/38-c12e-plugin-approval-recheck`，基于 `claude/3a-step16z` `ed64438fc`，复核通过，不改产品代码）

- **来源**：G05 恢复边界（09-28）只用内容包验证了“审批等待跨过停用后，本轮不能再用旧代”；可执行插件工具的审批前复核和批准后复核，当时标为另行安排。
- **机制**（09-24 就已在位，这次补的是真实组件上的核对）：
  - 执行器的 `_precheck_before_approval`（弹审批前）和批准后复核（claim 之前），都会调用 handler 的 `precheck_availability`。
  - `PluginProxyTool` 用固定激活引用 `activation_ref.require()` 鲜活读安装表，失效时报 `PLUGIN_ACTIVATION_UNAVAILABLE`。
  - 免审批调用在 handler 发送前再核对一次（`TOOL_UNAVAILABLE`／`not_started`）。
  - 新一轮建快照时，`availability()` 已把停用的插件标成不可用。
- **核对方式**：真实托管 MCP 插件进程，加真实安装表的激活与撤销（`PluginInstallStore.change_activation`），加真实 `ToolExecutor` 审批裁决；插件每收到一次业务调用就记一行。快照在本轮开头冻结（插件仍可用），之后再撤销激活。结果：
  - 审批等待期间停用、随后迟到批准：在批准后复核处拒绝，`PLUGIN_ACTIVATION_UNAVAILABLE`，handler 没执行。
  - 同轮内先停用：在审批前复核处拒绝，不弹审批。
  - 免审批的只读调用：`TOOL_UNAVAILABLE`、`not_started`。
  - 下一轮：快照本身已标不可用，运行时门直接拒绝。
  - 以上四种情况，插件进程都没收到调用。插件保持启用时，批准后照常执行一次。
- **结论**：没有缺陷，不改代码。新增集成测试锁住这四个复核点。
- **没覆盖**：真实 TUI 审批框加 `/plugins disable` 命令这一段没再跑，它们用的是同一个执行器和同一张安装表；G05 情况 1 已用内容包在 TUI 上跑过迟到批准。
- **验证**：见 TESTS.md 同名节。

## P10 常数整改第五批（2026-10-02，ds2，分支 `worker/ds2-p10-batch5`，基于 `ed64438fc`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：P10 白名单按模块分批清理。本批接第一~四批之后，范围是 `agent_py_agent/agent/conversation/` 目录
  （35 个产品文件）的 **96 个**待整改常数；不碰 tooling/agent_core/capability/backends/settings（留给后续批次）。
- **做法**：
  1. **改名 57 个**：数量上限类补 `_COUNT`（45 个：`ACTION_CANDIDATES_MIN→ACTION_CANDIDATES_MIN_COUNT`、
     `_MAX_RECORDS→_MAX_RECORDS_COUNT`、`_SCAN_INDEX_MAX_RECORDS_PER_FILE→…_COUNT` 等）；字符/长度类补 `_CHARS`
     （10 个：`_TEXT_LIMIT→_HOST_NOTICE_TEXT_LIMIT_CHARS`——因为 skill_learning_request.py 已有 `TEXT_LIMIT_CHARS`，
     剥下划线后撞名，改带模块前缀的名字避开；`BACKGROUND_TRANSCRIPT_TEXT_LIMIT→…_CHARS` 等）；token 类补 `_TOKENS`
     （`INPUT_MEDIA_TOKEN_RESERVE→INPUT_MEDIA_TOKEN_RESERVE_TOKENS`）；`_HOUR→_HOUR_SECONDS`（3600 是一小时的秒数）。
     全仓引用（agent/cli/tests 的 .py 与文档代码引用）词边界、按旧名长度降序同步（脚本 tmp/sync-batch5-refs.py，
     84 文件 423 处），重 grep 旧名 0 残留。
  2. **补说明 73 处**：35 个已有单位后缀的只补中文说明；4 个无物理单位的也补说明。脚本 tmp/add-batch5-comments.py
     按生成器 `_description` 同判定（最近非空注释行含中文字符）逐处插入。
  3. **无物理单位 4 个**：`REQUIRED_RECALL`（比率 1.0）、`_NO_PROGRESS_MAX_BACKOFF_MULTIPLIER`（倍数 8）、
     `_WAKE_FACT_DEPTH_LIMIT`（深度 3）、`_CHARS_PER_TOKEN_WINDOW`（每 token 字符换算率 3）只补说明，挪入白名单无单位组。
  4. 数值一律不变；`test_constant_names_unique._ALLOWED` 删掉过期条目 `MAX_RECORDS`（decision_outcome_log 改名后
     `MAX_RECORDS` 不再重复）。
- **白名单/目录**：待整改白名单 320→225（groups[0]）＋无物理单位组 28→32，只减不增；目录重建 800 项，`--check` 一致。
- **验证**：目录守卫 13 passed；改名直接相关 30 个测试文件全过；guards9 全量 168 passed；import boundaries 0；
  ruff/doc_sync/code-size strict/diff --check/clean_package 全过；`size_diff.sh` 新增告警 1（`test_gateway_conversation_control.py`
  的测试函数 soft 告警，该文件本轮未改、由集成分支 7e0fbcec1 引入，非本批所致）/消失 3。

## P13+P14：嵌入改引用模型档案、向量库记录生成模型（2026-10-02，ds1，分支 `worker/ds1-p13-embedding-profile`，基于 `ed64438fc`，已上线 step17a，main de222698b，2026-10-02；P14 的旁路 meta 方案已被顶部“P14 必须修”取代）

- **背景**：嵌入连接此前是 4 个平铺键（embedding_model/api_base/api_key/api_key_env），`/model` 目录已有 embedding 能力档案却无运行时消费者；
  `memory_vectors.json` 不记录生成它的模型，同维度换模型会静默混用两个向量空间。
- **P13 档案引用**：新键 `embedding_model_profile`（档案编号，必须有 embedding 能力）取代 4 个平铺键；旧键仅告警、不留别名、
  生产未设无需迁移。`core._embedding_client` 按档案的服务商凭据与端点构建，空档案 = 不建客户端、只走关键词；
  档案失效（不存在/无能力/停用/缺凭据/目录损坏）按结构化 reason 降级（MEMORY_EMBEDDING_PROFILE_UNAVAILABLE / INIT_FAILED / MODEL_MISSING），
  不回退聊天模型。该键是边界项（BOUNDARY_KEYS + USER_SETTINGS_BOUNDARY_KEYS），模型与聊天动作不能改；`/settings show` 展示编号/模型名/失效原因。
- **P14 向量身份**：向量库旁写 `memory_vectors.json.meta.json`（schema my-agent.memory-vectors-meta.v1：档案编号/服务商/模型名/维度/写入时间）；
  读取侧身份不一致或无元数据 → 已有向量视为不存在、语义召回退回关键词并给原因码（VECTOR_META_MISSING / VECTOR_IDENTITY_MISMATCH），
  不静默混用；写侧身份不一致拒绝混写（`_ensure_identity_writable`），首次写入（库空）落新 meta。
- **重建入口**：`memory vectors status`（预览数量/身份/维度）+ `memory vectors rebuild --confirmed`（未确认只预览；确认后清库全量重嵌并落新 meta），
  TUI 与 IM 同一命令族（cli/memory_admin_parser.py 注册）。
- **验证**：新测试 26 条全过（test_embedding_model_profile.py 8 案例档案失效 + 空值 + 边界权限 + settings 展示；test_vector_identity.py 10 条身份一致/不一致/
  无 meta/重建/CLI）；迁移相关 13 个测试文件全过（唯一 2 个失败为沙箱 Popen 环境性失败，基线 ed64438fc 复现同样失败）；guards9 167 passed；
  import boundaries 0；ruff/doc_sync/code-size strict/diff --check/clean_package 全过；3 个变异全部被测试拦截；size_diff 新增告警 0。

## P10 常数整改第四批（2026-10-02，ds1，分支 `worker/ds1-p10-batch4`，基于 `35f68d0f8`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：P10 白名单按模块分批清理。本批接第一、二批之后，范围是 `agent_py_agent/cli/` 目录（41 个文件）的 100 个待整改常数，
  不碰 `cli/chat_parts/tui_effort_menu.py`（sol 的 C7）与 ds2 第三批的 memory_store/gateway_parts/core.py。
- **做法**：
  1. **A 补说明 43 个**：定义上方补一句中文说明（管什么、为什么是这个值），满足生成器“最近注释行含中文字符”规则。
  2. **B 改名 56 个**：按 `_UNIT_SUFFIXES` 后缀表补单位改名（`_LIMIT_COUNT`→`_COUNT`、`_LINES`→`_LINE_COUNT`、
     `_ENTRIES`→`_ENTRY_COUNT`、`_WORKERS`→`_WORKER_COUNT`、`_ITEMS`→`_ITEM_COUNT`、宽度类加 `_CHARS` 等），
     全仓引用（agent/cli/tests 的 .py）词边界一起改，旧名零残留；`_BACKGROUND_THREADS_PER_OWNER` 因 `_PER_OWNER`
     后缀不匹配，改为 `_BACKGROUND_PER_OWNER_THREAD_COUNT`。
  3. **C 无物理单位 1 个**：`_SUBAGENT_HIERARCHY_DEFAULT_MAX_DEPTH`（深度）只补说明，挪入白名单无单位组。
  4. 数值一律不变；`test_constant_names_unique._ALLOWED` 无需改动（POLL_SECONDS 两个定义处仍同名不同义、条目保留）。
- **白名单/目录**：待整改白名单 497→398（主组 372 + 无单位组 26，只减不增）；目录重建 800 项，`--check` 一致。
- **验证**：目录守卫 13 passed；cli 相关 57 个测试文件除 1 个沙箱环境性失败外全过（该失败与本次改动无关，已用 HEAD 基线
  worktree 复现同样失败）；guards9 全过；import boundaries 0；ruff/doc_sync/code-size strict/diff --check/clean_package 全过；
  size_diff 新增告警 0（消失 2）。

## C12d：被接替时仍在运行的来源会不会停（2026-10-01，分支 `claude/38-c12d-takeover-stop`，基于 `claude/3a-step16z` `2ab1c20e3`，真实链路未复现，不改代码）

- **来源**：“被接替的子代理在运行账里补终态”条目的待定项——接替落账只改 canonical 状态，仍在运行的来源不在那里停执行轮。
- **真实链路核对**（隔离 Gateway + 脚本化假模型，代码 = `2ab1c20e3`）：
  - 父代理派出子代理，子代理在独立 runner 进程里执行 `sleep 120`；
  - 测试者用产品自己的 `manager.record_takeover` 对它落一次接替（宿主侧注入）。
  - 12 秒内：runner 进程和它的 `sleep` 子进程都退出了，`agent_run`、`attempt` 都是 cancelled，canonical 状态 `TAKEN_OVER`。
- **原因**：
  - runner 自己的心跳每 5 秒（`_RUNNER_SESSION_HEARTBEAT_SECONDS`）调用一次 `subagents/runner_control.runner_attempt_cancelled`；
  - 这个判据把 canonical `TAKEN_OVER` 当作“本轮已停”，于是 runner 中断自己，并结束子进程。
  - 所以运行中的来源本来就会停，不需要接替落账再开一个停止入口；另开一个只会和 runner 的自停抢着收尾，违反“不加兜底旁路”。
- **做法**：不改产品代码。在 `test_subagent_takeover_runtime_closeout.py` 加一项，锁住这条判据：接替后 `runner_attempt_cancelled` 必须为真。
- **留意**（2026-10-02 已改，见上方“接替自停的结束原因口径”）：这种情况下运行账结束事件里的原因原来是 `InterruptedError`，
  `runtime_source` 为空；静止来源走 `settle_taken_over_run` 时记的是 `subagent_takeover`／`taken_over`。现在两边同一口径。
- **证据**：`~/.my-agent/decision-evidence/c12-observations-20261001/c12d/`（仓库外）。

## J10 交付复核焦点覆盖改后未复核（2026-10-01 已实施；2026-10-02 本地复核，已上线 step17a（main de222698b，2026-10-02））

- **解决问题**：最后一次验证后又改文件，若不再运行命令，旧触发点不会提示多个过期焦点的复核顺序。
- **实现**：沿 `_optional_result_hints` 的原信封接缝消费成功写入的 `verification_state.status=stale` 和正整数
  `last_verification_id`，核对同 run/task 较早焦点；仍要求本轮 2—12 个焦点，至少两个 stale 才请 Jev 选择，候选只含 stale。
  `ToolLoopExecuteParams` 的本轮集合与 `run_command` 共用每条记录一次，失败/超时/非选择不重试，不新增持久账。
- **边界不变**：子代理、重复失败和未知副作用收口不触发；设置、来源、权限/参数及绝对期限变化丢弃建议。
  默认 off，observe 只记原账，apply 只追加同一 text/native 展示；不读正文、不加完成门或收尾触发、不强制续跑。
- **本地证据**：四文件定向 208 项通过；三个独立变异分别产生 1/4/1 个预期失败，原字节恢复后 208 项再通过。
  真实 Jev/模型采用与交付质量未验证，由 be 在 3a 集成后复测；命令及门禁结果见 [TESTS](TESTS.md)。

## J11 按结构化输入模态选模型（2026-10-02，sol56，分支 `worker/sol56-j11-modality`，本地已上线 step17a（main de222698b，2026-10-02））

- **事实来源**：主会话只读 Gateway 已校验的 `request.input_media`；真实请求捕获后只读 canonical
  `UserTurn.media` 和 provider 顶层 user `local_file` image/video 块。模型能力只读档案
  `input_modalities`。不读取普通正文，不按文件名、扩展名、模型名或模型自述猜模态。
- **共享判定**：`tool_request_projection.py` 新增候选适用性和候选集过滤值对象/纯函数。纯文本兼容未声明档案；
  image/video 必须显式声明，未声明与声明缺失分别返回 `candidate_input_modalities_undeclared` /
  `candidate_input_modalities_missing`；冻结历史缺失、未知内容块或不可迁移 reasoning 保持
  `history_modality_unknown`。
- **两条采用链**：Gateway 在向 Decision 暴露候选前过滤，并在完整候选投影中复核；子代理首轮在原目录代次、设置、
  task/权限/attempt 和 child thread CAS 之外复用同一判定。所有候选都不满足时保留原模型，记录
  `no_candidate_supports_input_modalities` 与逐候选结构化原因，不调用 Decision 或候选探针。
- **事务边界**：没有新增授权、第二份选模状态或媒体正文落盘；媒体支持不放宽跨模型 reasoning 限制，附件字节数不充当
  视觉 token/窗口证明。显式选中的不兼容模型仍由真实服务拒绝。
- **验证**：TDD 先得到 4 个预期行为失败；最终五个直接相关测试文件 205 项通过，尺寸拆分涉及的插件控制测试 18 项通过；
  三个变异分别杀死共享支持、child 拒绝和 Gateway 全不满足保留分支，测试均变红后恢复；`size_diff.sh` 新增告警 0、消失 8。
  guards9 168 项、导入边界 0、Ruff、文档同步、strict code-size、diff 与 clean package 全部通过，完整命令见 TESTS。
  真实 Gateway/TUI/收费模型和真实带图自动换模未验证。

## P10 常数整改第二批（2026-10-02，ds1，分支 `worker/ds1-p10-batch2`，基于 `8172c08c0`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：P10 定案“常数留在读取点、目录只是投影”后，待整改白名单按模块分批清理。本批接第一批之后，
  范围是 subagents/contracts/plugin_display/memory_archive/retrieval/common/verification/concurrency/llm_scale、
  attempt/io/local_storage 与 13 个单文件（owner_wake_discovery/ingress_queue/plugin_manifest/plugin_observation/
  worker_handler/memory_push/plugin_files_environment/plugin_host_api/startup_recovery/capability_package_manifest/
  gateway_model_observation/migrations/owner_scoped_pool），不碰 memory_store、core.py 与 ds2 的
  ingestion/scheduler/session_lock/user_space/adapter。
- **做法**：
  1. **A 补说明 32 个**：定义上方补一句中文说明（管什么、为什么是这个值），按生成器“最近注释行含中文字符”规则合规。
  2. **B 改名 53 个**：按 `_UNIT_SUFFIXES` 后缀表补单位改名（`_LIMIT_COUNT`→`_COUNT`、裸秒→`_SECONDS`、裸字节→`_BYTES`、
     裸字符→`_CHARS`、裸 token→`_TOKENS` 等），全仓引用（agent/cli/tests 的 .py，含 `__all__`）词边界一起改，旧名零残留。
  3. **C 无物理单位 18 个**：深度/维度/比率/倍数/协议值/权限位/小时点/水位/ngram/BM25 参数等后缀表无合适单位，
     只补中文说明，在白名单单列一组（reason 注明“无物理单位”）。
  4. 数值一律不变；`test_constant_names_unique._ALLOWED` 删除随改名消失的 `MAX_CANDIDATE_COUNT` 残留条目
     （原键实为 MAX_CANDIDATES，改名后重复消除）。
- **白名单/目录**：待整改白名单 685→600（删 A 32 + B 旧名 53；C 18 挪入新组，只减不增）；目录重建 800 项，`--check` 一致。
- **验证**：目录守卫 13 passed；26 个相关测试文件 + 5 个引用新名测试文件全过；guards9 167 passed；import boundaries 0；
  ruff/doc_sync/code-size strict/diff --check/clean_package 全过；size_diff 新增告警 0（消失 2）。

## 常数整改第三批：memory_store/gateway_parts/core.py 54 个常数合规（2026-10-02，分支 `worker/ds2-p10-batch3`，基于 `fa8666950`，已上线 step17a（main de222698b，2026-10-02））

- **起因**：P10 白名单（575 个待整改名字）继续分批清账；本批覆盖 memory_store 23 个、gateway_parts 28 个、core.py 3 个
  （合计 54 个，其中 `_POLL_SECONDS` 在两处定义，白名单按名字记 53 个）。数值一律不变、行为不变。
- **做法**：数量上限类补 `_COUNT`、字符/长度类补 `_CHARS` 后缀改名（如 `LOOP_GUARD_LIMIT→LOOP_GUARD_LIMIT_COUNT`、
  `_BRIEF_MAX→_BRIEF_MAX_CHARS`、`CURATOR_MAX_RETRIES→CURATOR_MAX_RETRY_COUNT`、`_TIMEOUT_SHRINK_FLOOR→_TIMEOUT_SHRINK_FLOOR_COUNT`），
  改名引用全仓同步（含测试）；时间/字符/字节类补中文说明；BM25 参数 `_BM25_B/_BM25_K1` 无物理单位只补说明并挪入白名单单独组
  （reason“无物理单位”）。`_WRITE_TIMEOUT_SECONDS` 的 tooling/pty_sessions.py 定义处（非本批范围）仍不合规，名字保留在白名单。
  目录重新生成 800 项 `--check` 一致。
- **验证**：守卫＋改名相关 7 文件全过、memory_store 19 文件与 gateway_parts 14 文件相关测试全过（插件宿主类沙箱限制除外）、
  guards9 168 passed、import 0、ruff/doc-sync/code-size strict/diff/clean-package 全过；size_diff.sh 新增告警 0。详见 TESTS.md。

## C7：/effort 八档与声明过滤（2026-10-02，sol，已上线 step17a（main de222698b，2026-10-02））

- **解决问题**：模型已声明 xhigh/ultra，但菜单、命令与派工只有六档，用户选不到。
- **做法**：用户扩为八档；中文标签、Responses/Chat/Messages 候选及预算只在 reasoning_control 一张表定义。
  Responses 的 xhigh→xhigh/high、ultra→ultra/max/xhigh/high；Chat/Messages 沿原 high/max，显式声明过滤。
  budget 的 xhigh=24576、ultra 同 max，原夹紧、关闭优先、线程/子代理冻结和两处同源投影不变。
- **边界**：服务商声明仍开放，交集为空不发字段；回执说出实际档位。未新增配置项或选模旁路，未调用真实模型。
- **验证与状态**：本地组件和三个独立变异有证据，扩展测试的既有失败保留，门禁记录见 TESTS；真实核对由 3a 集成后做。
  原“GPT xhigh/ultra 档位”待定项按要求登记为已实施（2026-10-01），详细合同见 [智能程度](docs/design/REASONING_EFFORT.md) 第 9 节。

## J15 自学习收尾四项（2026-10-01，ds1，分支 `worker/ds1-self-learning-tail`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：`DECISION_MODEL_FINAL_HANDOFF.md` 剩余风险"自学习"一条的四个收尾项：
  取消的 run 不进教训账本、S1 草稿场景标签措辞不准、S2 缺 privacy_url 跳过、冷却按进程算重启清零。
- **做法**：
  1. **取消记账**：`lesson_ledger.py` 新增 run 状态行（`kind=run_status`，独立于经验行，各自有界、
     幂等、读回按结构化字段分流），`cancellation.py` 在取消收口时按结构化状态写 `cancelled` 一笔，
     不读模型文字；经验行合同与上限不变。
  2. **S1 措辞**：`skill_proposals.py` 草稿适用场景标签由"来源任务目标"改为"适用场景"，
     description/when_to_use 与固定模板渲染不变，hash 语义不变（草稿正文未改，只改人读标签）。
  3. **S2 privacy_url 跳过**：`decision_skill_proposal_review.py` 草稿外发编码后按结构化字段
     `_URL_WITH_QUERY` 检查，命中即抛 `DecisionPrivacySkip`（reason=privacy_url），由
     `material_or_skip` 记 skipped 审计，URL 不外发；无查询串的 URL 照常发送。
  4. **冷却持久化**：`decision_policy.py` 新增 `persist_cooldown_snapshot`/`restore_cooldown_snapshot`
     （schema 带版本、owner 校验、32KB 上限、过期条目加载即弃、monotonic↔墙上时间换算、原子替换）；
     S2 入口每次调用在 `owner_skill_proposals_dir/.cooldown.json`（点开头避开提案 `*.json` 扫描）
     恢复/落盘。有界（32KB + 过期即清），可清理（删文件即回退纯进程内冷却，安全方向）。
- **验证**：相关测试 156 passed（含 6 个新增测试文件/用例）；6 个变异逐一验证均红后还原；
  收尾门禁见 TESTS.md。

## 常数整改第一批：5 模块 115 个常数合规（2026-10-01，分支 `worker/ds2-p10-batch1`，基于 `45610148e`，已上线 step17a（main de222698b，2026-10-02））

- **起因**：P10 建常数目录时 685 个历史不合规常数进待整改白名单（无单位后缀或无上方中文说明）；按阶段 3 分批整改，
  本批是第一批，覆盖 ingestion / scheduler / session_lock / user_space / adapter 5 个模块 115 个。数值一律不变、行为不变。
- **做法**：时间/长度/个数类按生成器后缀表补后缀改名（`_COUNT/_CHARS/_BYTES/_SECONDS/_PERCENT/_TOKENS` 等），改名引用全仓同步
  （含测试）；全部补上方中文说明；无物理单位常数（倍数/指数/深度 7 个名字）只补说明并挪入白名单单独组（reason“无物理单位”）。
  白名单 685→575＋7；`_ALLOWED` 删 TIMEOUT_S（改名后不再重复）、加 REQUEST_TIMEOUT_SECONDS（两处含义不同）；
  目录重新生成 799 项 `--check` 一致。
- **验证**：守卫 13 passed、相关 26 文件 438 passed、guards9 167 passed、import 0、ruff/doc-sync/code-size strict/diff/clean-package
  全过；size_diff.sh 新增告警 0。详见 TESTS.md。

## H2：宿主托管存储（插件安装库、包库）对模型的文件工具和 shell 不开放（2026-10-01，分支 `claude/38-h2-host-store-read`，基于 `claude/3a-step16z` `efaedfab2`，已上线 step17a（main de222698b，2026-10-02））

- **现象**（ae 的 C3 真实补测）：Goal 续跑时宿主正确地把旧 pin 投影成不可用，模型随后用 `run_command` 执行
  `unzip -p data/plugins/packages/<旧包摘要>.zip steps/step2.md`，读出已停用、已换代的旧包，照旧完成任务。这等于绕过了能力包的停用和换代。
- **为什么没挡住**：插件库就在 owner home 里（`data/plugins`：`installations.json`、`packages/`、`environments/`、`data/`）。
  - 路径策略在 owner 墙内整个放行自己的 owner home；没有墙时，整个数据根豁免；Full Access 更是不限。
  - shell 沙箱一样：Linux bwrap 挂载整个 owner home；macOS Seatbelt 只拒读数据根里本 owner 以外的部分。
  - 宿主自己的读包路径是对的，漏洞只在模型工具对宿主托管存储的读写权限。
- **做法**（一条通用规则，不写专项判断）：
  - **唯一声明**：`path_access_policy.HOST_MANAGED_OWNER_STORE_PARTS = (("data", "plugins"),)`，即规范布局的 `plugins_dir`；
    `owner_home_containing` 是 `owner_resolver._owner_home_dir` 的逆运算。这个模块会原样打进插件 SDK，只能依赖标准库，所以写成路径片段；
    守卫用例钉住它与规范布局一致。以后新增同类存储只在这里加。
  - **文件工具**：`PathAccessPolicy.check` 最先判定，在 owner 墙、Full Access、数据根豁免之前。数据根下任何 owner 的托管存储一律拒绝，
    错误码 `PATH_HOST_MANAGED_STORE_BLOCKED`（已登记 `error_taxonomy`，恢复提示指向宿主能力工具）。
    - 隔离插件进程按协议字段重建的策略同样拒绝。
    - `list_files`、`find_files` 的遍历不交出存储里的条目；`search_text` 逐条经 `resolve_path`，本来就拒绝。
  - **shell**：前台、后台、终端会话都经 `tooling/shell._sandbox_exec`，由它填 `AttemptSandboxSpec.hidden_paths`。
    - owner 隔离时只盖本 owner 的存储；其它 owner 的家 Linux 不挂载、macOS 已整体拒读，不为隐藏它们去新建挂载点暴露路径。
    - Full Access 盖数据根下全部 owner 的存储。
    - Linux bwrap 在所有挂载之后盖一层只读空 tmpfs；macOS Seatbelt 在规则最后同时拒读和拒写。
    - 插件进程沙箱不填这个字段，插件仍能读自己的环境目录。
- **不误伤**：只认 owner home 下的规范位置。用户工作区里自己的 zip、项目里同名的 `data/plugins` 目录，照常可读写。
- **边界与代价**：
  - macOS 上从 owner home 递归扫描（`grep -r`、`find`）时，这个目录会报 Operation not permitted；Linux 上它是空目录。
  - 没有 owner 墙的管理员，如果数据根不在默认位置、又没设 `MY_AGENT_HOME`，文件工具推不出数据根，这条规则不生效（生产用默认位置）。
  - 子代理经批准的 `controlled_exec` 不走 OS 沙箱，不在这次范围。
- **真实模型复核**：照原场景用 MiniMax-M2.7 跑了一次，再加一条新的自然需求，模型两次都没有尝试绕路（未命中原触发）。
  同一个真实隔离 home 上的宿主侧复核确认：本分支读不到旧包，base 代码原样读出。详见 TESTS.md。

## 选模型只在结构变化时问（J4）（2026-10-01，分支 `claude/be-jev-selection-cadence`，基于 `claude/be-jev-keepalive-z` `24f1bd287`，已上线 step17a（main de222698b，2026-10-02））

- **做法**：
  - 选模型点加专属字段 `points.model_selection.cadence`（every_turn / structure_change），配置 `decision_model_selection_cadence` 默认 every_turn（关）。
  - owner 与会话两层覆盖都能改：TUI 决策菜单有“询问节奏”单选，IM 经 my-agent 的 `user_config decision_patch`。
  - 结构 = 压缩代数 + 候选目录版本 + 当前冻结的模型档案；新会话没有指纹也算变化。
  - 结构没变就不提交、不调用，请求标记 `skipped / structure_unchanged`，到达诊断同名原因。
  - 只在成功拿到回答时记指纹，observe 转后台的在完成回调里记。指纹存 owner 决策数据目录的 `model_selection_structure.json`，最多 500 个会话。
- **为什么不用 `catalog_generation`**：目录文件每次保存（包括改决策设置）都会换新值，拿它判“目录变化”会把每次调设置都当成变化。候选摘要只在候选模型变化时才变。
- **当前模型也算结构**：用户在会话里手动换了模型，下一轮应该重新给建议。
- **真实验收**：
  - 环境：隔离 home、真实 Gateway + TUI、MiniMax 主模型 + Jev，打开观察不挡回复。
  - 同一会话 3 条只问 1 次；`/model` 换 M3 后重问；新会话第一条问、第二条跳过。
  - Jev 共 3 次调用，与预计一致。
  - 证据：`~/.my-agent/decision-evidence/j4-selection-cadence/`。
- **打开与关闭（生产由集成方做）**：owner 级 `decision_patch`，`{"changes":{"points.model_selection.cadence":"structure_change"}}`；关闭改回 `every_turn`。
- **验证**：见 TESTS.md 同名节。

## Jev 决策调用复用长连接（J1，B 第 1 步）与后台等待 5→15 秒净效果（J2）（2026-10-01，分支 `claude/be-jev-keepalive-z`，基于 `claude/3a-step16z` `efaedfab2`，已上线 step17a（main de222698b，2026-10-02））

- **先确认空闲上限**（经本机代理实测，探针只发轻量 GET）：
  - Jev 支持保活（Cloudflare 后，`Connection: keep-alive`），同一连接上第二次请求约 0.3 秒，新连接光 TLS 就约 0.9 秒。
  - 空闲连接在约 103–131 秒被直接断开，没有 TLS 关闭通知。Google、cloudflare.com 也一样，所以这是代理链（FlClash 或其上游节点）的上限，不是 Jev 的；DeepSeek（60 秒）、MiniMax（120 秒）是服务端自己正常关的。
  - Jev 空闲 30/60/120 秒复用成功，240/480 秒失败。
  - 据此取空闲上限 60 秒（离最早断开留 40 秒余量），取出前再用零超时 select 检查对端是否已关。
- **做法**：
  - 新模块 `backends/keepalive_transport.py` 实现连接池和保活打开。`GatewayRequest.connection_pool` 只允许零重试、禁止重定向、有绝对期限的严格请求。
  - 打开语义与 urllib `do_open` 对齐：同一代理解析、双超时连接类、中断守卫、发送许可。非 2xx 抛 HTTPError，不重试；复用连接上的发送失败照常算一次失败，不换连接重发。
  - 只有正文读完、未中止、服务端没要求关闭的连接才放回池。代理带凭据或明文经代理时不复用。
  - 计时：复用那次把建连、隧道、TLS 记 0 毫秒，标 `connection_reused`；同一连接每次从原方法重新包裹。
  - 决策服务按主配置 `decision_connection_reuse_enabled` 传进程级池；显式探测不传。仓库默认关（已定做法第 5 条），Mac 线上验收后由集成方用 /settings 打开。
- **A/B**（产品 Jev 后端，同一时段交替，2026-10-01 23:45–10-02 00:12，各 40 次，0 失败）：
  - P50 1390→395 毫秒，P90 1998→1513 毫秒，平均 1507→621 毫秒。
  - 复用 33/40，复用时建连+TLS 为 0。
  - 超过 3 秒 1/40→0/40，超过 2 秒 4/40→0/40。
  - 合入条件（connect/TLS ≈0、P50 至少快 0.3 秒、不新增失败）都满足。
- **J2 后台等待 5→15 秒的净效果**（同一工具，后台整理那种约 5k token 的大请求，复用开，00:12–00:24，30 次，0 失败）：
  - 最慢 1.76 秒，P90 1.47 秒，没有一次超过 5 秒。
  - 这一时段 15 秒比 5 秒既没救回调用，也没多占后台执行器（平均占用都是 0.80 秒）。
  - 结合 09-29 复测：慢时段直接度量约 2% 的调用要 5–15 秒，主要卡在新建连接的 TLS；复用之后这部分进一步减少。
  - 结论：保持 15 秒。后台调用没人在等，平时零成本，慢时段能救回少量调用。
- **待定**：
  - 这次的采样只覆盖历史慢时段（00–09 点）的开头。03:30 起补测一轮慢时段（J1 交替 20 轮 + J2 20 次），结果补进 TESTS 同名节。
  - 代理链的空闲上限取决于用户当前的代理节点；换节点后若上限低于 60 秒，取出前的 select 检查仍能挡掉已断开的连接，只是复用率会降低。
- **验证**：见 TESTS.md 同名节。

## 熔断体验修复 code-size 拆平（2026-10-01，ds1，分支 `worker/ds1-goal-fuse-ux`，已上线 step17a（main de222698b，2026-10-02））

- **背景**：`fa781d169` 合入后相对 step16y 告警基线多出 3 条 code-size 高风险（纯重构，行为不变）：
  class `BackgroundMainAgentRuntime` 202/250、params `_background_external_delivery`、测试函数过长。
- **做法**：
  - runtime.py：run_once 里空转片交付改写抽成模块级 `_background_goal_delivery_mode`（复用
    `_background_goal_idle_slice`，判据不变），类里只留一行委托；类行数 202 → 199（< near-soft 200）。
  - background_delivery.py：`_background_external_delivery` 的 `frozen_retry` 与 `local_transcript_only`
    收进 frozen dataclass `_ExternalDeliveryOptions`，参数回到 4 个；参数名用 `external_options`，
    避开 guard 对 params/options/request 打包参数 +1 的特判。
  - test_goal_idle_delivery.py：60 行的推送测试拆成两个（feishu 推送并取走、internal 只排队不推送）。
- **验证**：warn 基线相对 step16y（3598 条）三个目标文件 **0 新增**（全部新增集合也为空）；
  strict code-size 2239 blocked=False；相关测试 217 passed（5+8+204）、guards9 166 passed。

## 删除死配置 log_analysis_config.yaml，参数中心改三来源（2026-10-01，分支 `worker/ds2-del-log-analysis`，基于 `claude/3a-step16z` 的 `904b441b4`，已上线 step17a（main de222698b，2026-10-02））

- **起因**：P17 修订时核实 log_analysis 产品代码无任何读取点；3a 确认 `agent/log_analysis/` 包早已不存在，
  `log_analysis_config.yaml`（20 键）是死配置。按铁律“确定不用的旧字段、旧目录、旧兼容分支要删；配置必须真的生效”删除。
  仓库根目录 `mutation_testing.py`/`mutation_tester.py` 是开发辅助脚本仍引用旧模块，按 3a 指示本轮不动。
- **做法**：删随包文件；参数中心去掉 log_analysis 来源——`parameter_registry` 删 `SOURCE_LOG_ANALYSIS` 与登记/运行值分支
  （`running_value` 未知来源回落 `spec.default`）、`parameter_changes._effective` 删 log_analysis 加载器分派、只读来源集合只剩
  `runtime_guard`、`settings_control_service`/`user_config_tool`/`user_config_capability` 只读标注同步；前端
  `sync-backend-config.mjs` 去来源/分类/categoryFor/requiresUnlock/source 文案并重新生成目录（270→250 项，`--check` 通过）；
  `scripts/code_size_rules.py` 清掉 JUNK_NAME_BASELINE 指向已删包的两条死条目。
- **验证**：相关 11 文件 186 passed、guards9 全量 167 passed、import 0、ruff/doc-sync/code-size strict/diff/clean-package 全过；
  warn 基线比对 step16y → ADDED=0；size_diff.sh 新增告警 0。详见 TESTS.md。
- **size_diff 顺手拆平**：warn 首跑发现 `runtime.py::BackgroundMainAgentRuntime`（class high-risk，202 行）相对 step16y 是
  集成分支其它提交引入的新增；按规则 9 拆平——`_run_agent` 尾部收尾段抽成模块级函数 `_finalize_background_execution`
  （参数命名避开 `request` 名单的 params +1 规则），行为逐行不变，拆后 size_diff 新增告警 0。

## P17 合入后的 code-size 拆平（2026-10-01，分支 `worker/ds2-p17-size-fix`，基于 `1aabb7f13`，已上线 step17a（main de222698b，2026-10-02））

- **起因**：集成分支合入 P17 后，相对已上线 step16y 多出 3 条 high-risk（UserConfigTool 类行数接近软上限、
  revert_change / read_config_fact 嵌套 3 层）。本轮只做结构拆平，行为、错误码、回执字段一律不变。
- **做法**：`_decision`/`_decision_model_operation` 移成模块级函数（函数名不变，保持基线告警身份）；
  `revert_change` 抽 `_revert_entry` 早返回压平；`read_config_fact` 抽 `_fact_paths` 按 source 早返回路径。
- **验证**：相关 174 passed、guards9 167 passed、import 0、ruff/doc-sync/code-size strict/diff/clean-package 全过；
  warn 基线比对 step16y → ADDED=0。详见 TESTS.md。

## A/B 能力包补确定性检查（C13 第二部分）：A0.4.0、B0.2.0（2026-10-01，分支 `claude/ae-c13-pack-checks`，基于 `claude/3a-step16z` `a4e97dd69`，已上线 step17a（main de222698b，2026-10-02））

- **依据**：固定最终矩阵 11 次业务失败的独立审计（A 9 次、B 2 次），逐条判断哪些能靠结构化字段确定性核对。
  - **A**：大多数失败是散文语义问题，例如漏标新增、出处说错、意译冲突、同镜自相矛盾，检查器抓不到，仍靠方法和审阅。
  - **B**：两次失败原检查器本来能抓，只是模型没跑，或没带交接文件和绑定。
  - **B 的真正缺口**：换了原说话人、配一份摘要正确但映射为空的交接，旧版两项都通过。
- **做法**：只改包自己的检查器、方法、模板和示例，不加宿主硬门；所有新输入字段都是可选的，交付 v3、项目 v1、交接 v2 格式不变，报告只增加键。
  - **A 0.4.0**：
    - `lines[]` 结构化台词：0 条只提醒；说话人不在本镜声明里报错。
    - 逐字引用：`verbatim_source_id` 和 `source_quotes[]` 必须是所引段落的连续子串，不归一化，属 SHT-26 子集。
    - 道具状态：`props`/`prop_states` 相邻接续提醒、持有人声明报错，`continuity_break` 声明有意跳接，属 CON-01 结构子集。
  - **B 0.2.0**：
    - `--baseline-project` 逐 ID 对比：换说话人（含重编号）、节拍类型改变、对象删除都提醒。
    - 交接覆盖真实改动：根地址不算覆盖，没覆盖的提醒。
    - 声称的新增或省略没真发生：报错。
  - **两包共同**：报告加 `checker`（包 ID、版本、按本文件实际字节算出的 sha256），帮助区分原包程序与模型自写脚本。
    - 限制：重跑中有模型自写脚本，把原 sha 当常量打印。所以这个字段只在对照"实际执行的文件字节"时才有意义，单看报告不能证明跑的是原程序。
- **否掉的候选**（实测噪声太大）：
  - 画外角色名出现在镜头文字里就提醒：9 份交付 14 处命中，只有 2 处是真问题。
  - 项目里出现模板外字段就提醒：会误伤 2 个通过的 B 例。
- **流程边界**：冻结清单 `holdout_policy` 规定，据失败改动后原用例就成了"已见回归"。所以本次之后再跑 A05/A08/A10/B05/B08/B10 三轮，记为已见回归验收；新的最终保留集要另外冻结，交 3a/用户决定。
- **验证**：见 TESTS.md 同名节。
- **冻结语义三次重跑结果（2026-10-02，已见回归）**：业务 10/27 通过（原最终矩阵 16/27）。
  - A 1/9，B 1/9（原口径 2/9），N 8/9。
  - 新检查抓到了真问题，但模型或不处理，或改用自写或改写的检查器；B 没有一次用 `--baseline-project`。
  - 依此做 C16 判定：没有包满足"最后 3 次重跑业务全部通过"，不装生产。
  - 详见 [CAPABILITY_PACK_ACCEPTANCE.md](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md) 的 C13 第二部分和 C16 两节。

## 飞书上 Goal 空转片逐片推送、熔断提示不主动推送（2026-10-01，C11 实测观察 → 已实施）

- **现象**（假飞书 + 真网关 + 真模型，`6b2f56dc7`）：
  - 用户在飞书设了一个"等我发笔记"的持续目标。前台确认之后，3 个续跑片每片都给出一句很短的"在等你"，每句都按后台送达规则主动推给了用户（`reason=thread_goal_continue delivery=sent`），10 秒内连收 3 条。
  - 熔断暂停后，提示 `GOAL_CONTINUATION_NO_PROGRESS` 只进了线程的 `pending_host_notices`，要等用户下一次说话才随回复送达。用户不说话，就只看到 3 条"在等你"，不知道续跑已经停了。
- **现有合同**：两条都符合现有设计。续跑片的最终回复按后台送达规则外发；宿主提示随下一次正常回复送出，停止与失败不取走。所以这不是缺陷。
- **实施定案（2026-10-01，ds1，分支 `worker/ds1-goal-fuse-ux`，已上线 step17a（main de222698b，2026-10-02））**：
  - **空转片不外发**：续跑片按熔断同一口径判空片——`runtime._background_goal_idle_slice` 复用
    `goal_progress_fuse.slice_has_progress`（只读结构化字段，不另写一套）；判为无进展的片，回复正文不主动推到
    外部通道，只经 `background_delivery.record_background_response(local_transcript_only=True)` 落权威转录
    （TUI 可见、不冻结重投、wake 按 local_only 确认）。有进展的片照常外发。内部/TUI 线程（无外发通道）行为不变。
  - **熔断主动提示**：`goal_progress_fuse.push_goal_no_progress_notice` 先按原规则
    `queue_host_notice(replace_same_code=True)` 排队，再对有外发通道的线程主动投递一次
    （`DeliveryContext(mode="proactive")`，幂等键 `goal-no-progress:{goal_id}`）；投递成功后
    `take_host_notices` 把同一条提示取走（已读），并用 `_record_no_progress_pushed_message` 落一条会话消息，
    保证同一提示只推一次、下一条回复不再重复带出；投递失败不取走，留待送达队列由下一次回复补出。纯 TUI 线程只排队。
  - 不加新配置。

## 参数中心 P10：模块级数值常数的“只读目录 + 守卫”（2026-10-01，分支 `worker/ds1-constants-catalog`，基于 `claude/3a-step16z` 的 `f15b0a19f`，已上线 step17a（main de222698b，2026-10-02））

- **设计定案**：常数留在读取它的地方（那里是唯一权威），不搬进中央常数模块、不变成用户配置项；目录是投影——由
  `scripts/build_constants_catalog.py` 用 ast 静态扫描（不 import 产品代码）生成随包 JSON
  `agent_py_agent/config/constants_catalog.json`（**799 项**、328 个文件），统一查找和查看；改常数就是改那一行源码。
  与原计划“每批先把常数收进登记表并证明读取方真的读到，再删原常数”的差别：迁移仍按模块分批做（改名、合并、补说明），
  但不再把常数搬进参数登记表——登记表只收用户可调参数，常数只进只读目录。
- **生成器**：收集模块级、全大写、值为数字字面量或简单算术（四则/整除/取模/乘方/正负）的常数，每条记名字、文件、
  计算值、单位（后缀推导：_SECONDS/_MS/_CHARS/_BYTES/_TOKENS/_COUNT/_PERCENT 等，推不出记空）、类别（上限预算/超时/
  比例阈值/重试/其它，按名字关键字顺序命中）、说明（紧挨着上方的中文注释行）；协议类不收（名字含 VERSION/STATUS/EVENT/
  SCHEMA/PROTOCOL/CODE 或以 _TYPE 结尾）；`--check` 模式只比较不写，守卫测试直接调它。
- **守卫**（`agent_py_agent/tests/test_constants_catalog.py`）：目录与源码一致（不比较行号）；不合规常数（无单位后缀或无上方中文说明）
  只允许出现在 `tests/fixtures/constants_catalog_pending_fixes.json` 的待整改白名单里（当前 685 个名字，历史遗留快照），
  名单外的常数必须合规，名单里的常数已合规或已删除时必须删条目（只短不长）；协议类排除、单位/类别推导、扫描行为样例钉住；
  只在常数上方插入空行（行号变了、常数没变）时 --check 必须仍通过。
- **查看入口**（只读）：`/settings internal <关键词>`（TUI 与 IM 共用 Gateway 控制通道）按名字/说明/文件搜索，显示
  文件:行、值、单位、类别、说明；user_config 的 action=search 同数据源附 constants 结果并标明“代码常数，只读，改动需改代码”。
  运行时只读模块 `agent_py_agent/agent/settings/constants_catalog.py` 读随包 JSON（缺失/损坏返回空目录）。
- **目录不存行号（2026-10-01 修订，3a 审查意见，新提交）**：目录唯一失效时机是常数增删、改名、改值、改说明或改单位/类别；
  行号由查看入口运行时只读打开那一个文件按名字定位（`locate_line`/`entry_with_line`，定位不到只显示文件），
  避免“每个开发分支都顺带改 JSON、天天冲突”。变异验证：把行号重新加回条目后，插空行用例与一致性用例都变红（证明用例真的钉住）。
- **打包**：pyproject.toml package-data 加 `config/*.json`，test_packaging 加断言。
- **验证**：新增守卫 9 项 + 相关既有测试 185 项通过；3 个变异（协议排除清空、单位推导恒空、说明提取恒空）全部被抓住；
  详情见 TESTS.md 同名节。

## 参数中心 P17 修订：capability 写入/运行值走运行时实际路径，runtime_guard/log_analysis 改只读来源（2026-10-01，分支 `worker/ds2-registry-sources`，基于 `claude/3a-step16z` 的 `f15b0a19f`，在 5a51735c1 上修订，已上线 step17a（main de222698b，2026-10-02））

- **起因**：3a 评审发现初版把 capability 写进“用户配置同目录”的 `capability_config.yaml`，但运行时唯一读取入口是
  `capability_config_for_agent(agent)`（`agent.capability_config_path` 或 `default_capability_config_path(agent.root)`，
  对 Gateway owner 即 `<owner home>/config/capability_config.yaml`），写了不生效；runtime_guard/log_analysis 运行时没有用户覆盖层。
- **capability**：`runtime_config_reload` 新增公开函数 `capability_config_path_for(agent)`（与运行时入口同一路径逻辑）；
  写入口 `parameter_changes` 新增 `WritePaths(user_path, capability_path)`，capability 写运行时路径，文件不存在时新建、只写被改的键，
  用同一个 `load_capability_config` 回读，不一致恢复原文件（或删除新建文件）并报 PARAMETER_NOT_EFFECTIVE；账本记在 agent 用户配置旁；
  回执如实写生效时机（capability 改动重启 Gateway 才生效）。
- **runtime_guard/log_analysis**：`_writable_spec` 先于安全边界抛 PARAMETER_SOURCE_READ_ONLY（结构化原因“只能查看和搜索”），
  set/reset/revert 一律拒绝，不新造运行时读不到的文件。
- **运行值**：`running_value` capability 按运行时路径读（owner 有覆盖时显示覆盖值），只读来源显示随包默认并标「随包默认、不可覆盖」。
- **前端**：三份 YAML 补行尾注释后，`backend-config-catalog.json` 14 处占位说明同步为后端注释；前端两个目录守卫跑通。
- **验证**：相关 233 passed（含重写后的 `test_parameter_sources.py` 14 项）、guards9 167 passed、前端两守卫 5 passed、
  import boundaries 0、ruff/doc-sync/code-size strict/diff-check/clean-package 全过；3 个变异（写目标改回旧路径、去掉只读拒绝、
  running_value 忽略运行时路径）均被杀红并还原。详见 TESTS.md。

## 参数中心 P17：capability/runtime_guard/log_analysis 三份配置纳入登记表与写入口（2026-10-01，分支 `worker/ds2-registry-sources`，基于 `claude/3a-step16z` 的 `f15b0a19f`，已上线 step17a（main de222698b，2026-10-02））

- **来源维度**：`ParameterSpec` 加 `source`（agent/capability/runtime_guard/log_analysis），登记表从 224 键扩到 297 键
  （capability 31、runtime_guard 22、log_analysis 20）；说明/类型/默认值从各随包 YAML 与 dataclass 取，规则与主配置一致。
- **统一入口**：/settings 与 user_config 的 search/view/history/revert 四来源全覆盖；写入口仍是 `parameter_changes` 唯一一套——
  `_write_target` 按来源重定向到用户配置同目录 `<source>_config.yaml`，`_effective` 按来源选正式加载器（capability→`load_capability_config`、
  runtime_guard→`runtime_guard_policy`、log_analysis→`load_simple_yaml`）回读校验，写后回读不一致恢复原文件并记 settings-changes 账本。
- **安全等级**：默认按边界（授权/权限/路径/执行权威链/运行时门/审批/白名单/执行开关/audit 模型不可改），capability 的 17 个能力数值上限键
  为显式 free 名单（`_EXTRA_FREE_KEYS` 逐键带理由），runtime_guard/log_analysis 全部边界。
- **展示**：`/settings show` 加「来源：<source>_config.yaml」行；非 agent 键运行值走 `running_value`（从随包文件实时读）。
- **验证**：相关 153 passed（含新 `test_parameter_sources.py` 10 项）、guards9 167 passed、import boundaries 0、ruff/doc-sync/code-size
  strict/diff-check/clean-package 全过；3 个变异被杀红并还原。详见 TESTS.md。

## P12：记忆整理引用固定模型档案（2026-10-01，sol，`worker/sol-curator-profile`，本地已上线 step17a（main de222698b，2026-10-02）/部署）

- 解决问题：聊天模型套餐或连接失败时，后台记忆整理也跟着失效；只覆盖协议/型号、却借用聊天凭据的旧组合容易配错。
- 新键 `memory_curator_model_profile` 默认空，沿用 owner 当前选中模型；非空只引用原模型目录，完整采用该档案的服务商凭据、端点、协议与选项。
  原 `memory_curator_provider`、`memory_curator_model` 删除，不留别名或自动转值，残留按原未知配置键规则告警。
- 引用必须存在、启用并有 agentic 用途；失效不回退，复用 `ModelNotConfiguredError` / `UnconfiguredBackend`，
  原运行账 `failure_diagnostic` 补 `profile_id`、`profile_reason`，仍是 `CURATOR_MODEL_NOT_CONFIGURED` 和一小时退避，run v2 键集不变。
- 新键是流量去向边界，登记表对模型始终不可写。仅可信管理员用户 `/settings` 控制调用内开启短生命周期授权，
  set/reset/revert 复用同一校验与账本，不按 actor 标签授权，退出即清理；其余边界项不放行。
- TUI/IM 共用 `/settings show` 回执，显示运行编号、型号及待重启的保存编号/型号；`/model` 文本和 TUI 模型列表展示稳定档案编号。
- 本地定向 235 passed、架构守卫 167 passed；三个独立变异均被断言拦截，逐字节恢复后 99 passed，命令见 TESTS。
- 边界：连接在 owner 实例装配时冻结，配置变更需重启；自动总结 Skill 继续共用原 Curator 后端。私有编号仅在本人目录解析，
  跨 owner 使用须有原共享授权，不能读取管理员私有凭据作为回退。本线不改生产，不启动 Gateway；3a 部署后再绑定实际 deepseek-v4-flash 档案并验收。

## C14 M-B1：五阶段只读 Node 门插件（2026-10-01，`worker/sol2-c14-mb1`，本地候选已实现、待外部复验/审查/集成）

- 解决问题：上游 B 的离线 CLI 尚不能经宿主逐次权限作为工具使用，分镜默认门日志还会写工作区。
- `shuohao-novel-gates` 为 v6 Node 18+ 插件，五工具声明 read_only；复用 hello-node 的读取上下文移植，
  主文件、参考、原文、日志和每张卡片逐份裁决，不另建授权账。直接调用上游导出函数，不解析自然语言。
- validate/checkup 保留真实阶段差异，独立 stats 只在分镜读取已有 JSONL 日志；角色仅 validate 且 book 必填。
  同进程计算不进入 CLI main/logGates，分镜等价于 --no-log；缺依赖门 skipped/passed=null，不算通过。
- 原 LICENSE、NOTICE、固定来源与逐文件摘要随包，characters 原样 NUL 配 -text；不迁付费路线、install.sh、作者图片。
  六份自检、样例与 report 只在测试闭包，M-B2 写工具未实施。
- 宿主测试原嵌套 JSON 子串断言改为分层 json.loads，钉住 passed is True、阶段/操作、门计数和持久输出一致。
  本机聚焦 29 passed、1 failed、1 skipped，守卫 166 passed；失败在解释器确认启用处，沙箱跳过发生在 Node 启动前。
  3a 沙箱外对修改前候选报告 30 passed、1 failed、0 skipped，固定提交完整复验仍由 3a 执行，不能改记本机全绿。
- 真实 TUI/模型未验证；Node 打开后复核不等同 Python SDK 的逐段目录描述符竞态防护。
  严格静态门禁及含 NUL 的源树 clean-package 通过；2026-10-02 补跨语言样例为 2 passed、2 failed，均在宿主启用确认处失败。
  3a 明确后续只跑定向文件与 guards9，不再执行全仓或无文件列表的-x；此前全仓由 3a 主动结束，
  历史日志保留但不作为 M-B1 当前验收门，不修改无关旧接口失败。
  完整证据见 [TESTS](TESTS.md)，能力边界见 [迁移设计](docs/design/CAPABILITY_UPSTREAM_MIGRATION.md)与 [README](plugins/shuohao-novel-gates/README.md)。

## C14 第一批 M-A1 + M-A2：drama-media-shell（2026-10-01，分支 `worker/sol56-c14-ma12`，已上线 step17a，main de222698b，2026-10-02）

- **范围**：按固定上游 `zenstory-ai/drama-skills@0e8929881bb59248618c4f402707c64723adc017` 迁移四个纯离线提示词检查器，
  并把 `production_tool.py` 的 prepare/confirm/run/status/audit/collect 作业合同收进同一 Python 插件；运行依赖只有精确
  `my-agent-plugin-api==0.2.0`，业务代码只用标准库。
- **付费边界**：包内只有 `fixture_adapter.py`，没有 `provider_adapters.py`、任意供应商网络调用或任意适配器/子进程入口；
  `production_run` 只接受内置 fixture，回执和输出都标“夹具，不是真实生成”，真实供应商请求返回结构化
  `PROVIDER_NOT_CONFIGURED`。工具说明统一写明“夹具产物不算生成成功”。
- **权限与状态**：工作区读取/写入权限只从每次 MCP 调用 `_meta` 恢复；写入执行 SDK 0.2.0 的
  `check → anchor → no-follow 原子写`。作业元数据只进绝对 `MY_AGENT_PLUGIN_DATA_DIR`，不回退 cwd、用户目录或安装目录，
  不创建宿主任务账。
- **许可与来源**：随包保留 A 的 MIT 原文和 `Copyright (c) 2026 drama-skills contributors`；`PROVENANCE.md`
  记录固定提交、每个迁移来源的路径/sha256 和改造说明。明确排除 Remotion、限用途小说及派生样例和付费适配器。
- **打包修复**：`build_plugin_package.py` 的外层 ZIP 成员改为固定时间戳、权限与压缩方式；此前 wheel 可复现而外层包仍带
  构建时钟，本插件的重复构建测试实际跨过 DOS 两秒粒度后抓到该问题。
- **验证边界**：实际包、`tools/list` 同源性、重复构建字节、上游 selftest 合同、读写上下文、符号链接拒绝、fixture 状态机
  和 MCP 进程组件已由仓库测试覆盖；真实 TUI 安装/调用与真实模型自然调用按分工留给 3a/ae 集成后验收，不把组件测试外推。

## 脱敏补两种写法 + LandmarkOptions 同名不同义改名（2026-10-01，分支 `worker/ds1-mask-rename`，基于 `claude/3a-step16z` 的 `02568822d`，已上线 step17a（main de222698b，2026-10-02））

- **P15 脱敏补两种写法**（唯一实现 `user_config_capability.masked_structure`/`mask_value`，未另写一份）：
  - (a) 请求头/环境变量容器开关（`--headers`、`--env`）后“名字 值”分成两项时，名字和值两项都遮（名字可能是 Authorization/x-api-key
    这类敏感请求头名）；凭据名开关（`--api-key`、`--pass`）仍是单值写法，只遮值，不把后面的参数误当“值”。
  - (b) 不紧跟开关、名字又不像凭据的单独一项 `名字=值` 里，值形如密钥（常见 token 前缀 sk-/ghp_/eyJ 等，或 >=24 字符的字母数字
    混用长串）也遮值；规则只看结构（长度、字符集、前缀），不写死服务商；普通值（主机名、纯数字、网址、路径、短串）不遮。
  - 测试：`test_structured_masking.py` 新增两例（分两项遮 + 密钥形态遮/不误遮），`test_value_display_parity` 等守卫通过。
- **P9 同名不同义改名**：压缩摘要“原话备份”段的 `LandmarkOptions.max_tokens` 改名 `landmark_budget_tokens`（整段备份的 token 预算，
  不是模型输出上限），compact_landmarks.py 与 compact.py（render/minimum_tokens 的 replace）及测试（test_compact_landmarks /
  test_goal_lifecycle_recovery / test_gateway_conversation_compact）全量同步，数值不变；只被测试调用的
  `memory_archive.tokens.check_token_budget`（连同 `TokenBudgetResult` 与阈值表、`__init__` 导出、`test_archive_snapshots` 用例）删除。
- **文档**：PARAMETER_CENTER.md 脱敏节 209-210 漏网写法改为已补；第 2 节目标第 5 条补记落地。
- **验证**：脱敏/改名相关测试 204 passed（详见 TESTS.md）。

## 上游未迁内容：许可核对与迁移清单（C14）（2026-10-01，分支 `claude/ae-c14-migration-list`，清单已定；M-B1 本地候选见顶部）

- **结论**：
  - A（MIT）、B（Apache-2.0 + NOTICE）的代码可以迁，随迁移文件保留许可原文和来源说明；
  - Toonflow 不迁：补充协议要求产品分发前取得书面商业授权，且不得删改标识；
  - Remotion 不打包、不自动安装：员工超过 3 人的公司须购买 Company License；
  - A 的限用途评测小说及其派生样例、B 的作者二维码和推广图不迁。
- **清单**：
  - B 的 5 个 CLI 做成 Node 插件（v6 interpreter），分两批：先上只读门工具（outline 14、art 11、script 10、storyboard 17 道，加 characters 的逐字核对）；
    写文件的工具要等 Node 写入上下文移植好、补齐一致性用例后再上；
  - A 的 4 个提示词检查器加上生产作业外壳（只配离线夹具）做成 Python 插件，**不带付费供应商适配器**；
  - ffmpeg 剪辑为可选第三批。
- **分工**：实现派给 my-agent 会话，ae 审查。详见[迁移清单](docs/design/CAPABILITY_UPSTREAM_MIGRATION.md)。

## 压缩触发绝对上限默认改为 300000（2026-10-01，分支 `claude/3a-cap-default`，已上线 step17a（main de222698b，2026-10-02））

- **事故**：生产 `memory_compact_auto_trigger_max_tokens` 09-29 设为 300000 后，09-30 07:49Z 被一个 my-agent 会话经 user_config `reset`（账本 actor=model）退回当时的默认 0，即不封顶；之后 1M 窗口的 deepseek-v4-flash 会话要到约 90 万才压缩，10-01 把 gpt-6-luna 档案窗口改为实测的 900000 后也一样（约 81 万）。这正是 09-29「1M 窗口、90% 触发」429 的形状。ae 在 C1 准备时只读发现，集成者核实后已经正式写回 300000（变更号 77e58ca0038e，重启 Gateway 生效）。
- **根修**：默认值改为 300000（AgentConfig、MemorySettings、随包 YAML 与运行时 `DEFAULT_COMPACT_TRIGGER_MAX_TOKENS` 一致），`reset` 回到安全值；显式 0 仍表示不封顶；乱填、负数、布尔与缺失都回到 300000（配置解析与运行时同一口径，原来回到 0）。
- **影响面**：窗口 ≤333K 的模型按 90% 先触发，不受影响；只有 1M 级窗口会在 30 万处压缩。参数仍是 free（模型可改），改为 0 会在账本留下记录。
- **验证**：见 TESTS.md 同名节；属于共享默认值，推送前跑 12 片全量车道。

## decision_patch / decision_reset 接受可选 reason（2026-10-01，分支 `worker/ds1-decision-reason`，基于 `claude/3a-step16y` 的 `e08507ea4`，已上线 step17a（main de222698b，2026-10-02））

- **触发条件已核实（3a 10-01 真实探测）**：隔离环境用真实 deepseek-v4-flash 做 4 条“改决策设置”自然语言请求，decision_patch 首次调用
  3 次里错 2 次，都是多带 `reason` 被拒（TOOL_INVALID_ARGUMENTS，unknown_fields=reason），第二次才改对；gpt-6-luna 3 次全对；
  9-28 生产那 3 次错误也全是多带 `reason`（DESIGN_LEDGER 原 1118–1125 行附近，见下方 2026-09-28 条目）。
- **落地**：decision_patch / decision_reset 的允许字段加可选 `reason`（字符串，截断 200 字，超长回执与账本标明 reason_truncated）；
  reason 只记录与展示（写进模型档案旁的修改账本 `profile_ledger_path`，/settings history 一类入口可显示），不参与任何机器判断，
  不改 overrides/revision、不影响结果。
- **工具面**：user_config 工具说明（普通视图与 decision-only 视图）与 schema 的 reason 描述同步改写；其余多带字段仍整笔拒绝并在
  unknown_fields 列出，不替模型删字段。
- **按动作拆分 schema 不做**：会改模型可见的工具面，部分 provider 对 oneOf 支持不稳，且接受 reason 后模型不再需要删字段重试。
- **测试**：`test_decision_settings_reason.py` 5 项（patch/reset 带 reason 成功并记录、超长截断并标记、多余字段仍拒、相同 changes 不同
  reason 结果一致）；`test_user_config_decision_patch.py` 同步更新（_PATCH_FIELDS 含 reason、fake model 第一次带 reason 直接成功）。
  3 个变异实测被抓住（去掉 reason 允许 / 不写账本 / 不标截断）。
- **验证**：相关 decision 测试 123 passed（详见 TESTS.md）。

## 参数登记表元数据：单位/范围/归属模块/读取方（2026-10-01，分支 `worker/ds2-registry-metadata`，已上线 step17a（main de222698b，2026-10-02））

- **P8（三线收尾）设计目标 1 落地**：登记表每条参数补「单位、范围、归属模块、读取方」四项元数据，全部由
  `agent/settings/parameter_metadata.py` 自动推导、不手工抄，推不出的留空：单位按键名后缀（_seconds/_ms/_chars/_bytes/_tokens/
  _percent/_days/_hour/_turns/_files/_requests），范围取自现有规范化/校验规格（_memory_coercion._FIELDS、TOOL_INT_FIELDS、
  services/_normalize 的 Service 规格与枚举、backends/sampling.validate_top_p 0-1），读取方按 test_config_field_readers 同一套
  属性访问/字符串键引用扫描（decision_*/memory_decision_* 经 decision_config_fields() 映射补，读取方 decision_settings_defaults）。
- **展示**：`/settings show` 与 user_config 的 view/search 只在有值时显示这四项（settings_control_service._metadata_line、
  user_config_tool._spec_view）。
- **顺带修复**：test_settings_chat_control.py:225 既有 SIM300（Yoda 条件，f89247a29 引入）改为语义等价写法，只为收尾 ruff 门禁。
- **验证**：test_parameter_metadata.py 7 项 + 3 个变异（删 _seconds 后缀/删 request_timeout 范围/field_readers 跳过全部文件）均被杀红；
  相关 pytest 96+90 passed、架构守卫 182 passed、import boundaries 0 条、ruff/doc sync/diff --check 通过。详见 TESTS.md。

## 参数减量收口：model_auth_ref 只隐藏、目标改为实际下限（2026-10-01，分支 `claude/3a-p11-close`，已上线 step17a（main de222698b，2026-10-02））

- **决定（集成者，三线收尾 goal P11）**：不再按“约 100 项”的数量目标减量。219 个随包键里 218 个在 `/settings` 列表与搜索出现；
  `model_auth_ref`（/model 登录生成的 OAuth 运行引用，注释写“请勿手填”）归入新集合 `MANAGED_ELSEWHERE_KEYS`：只从列表与搜索隐藏，
  字段、登记与边界等级不变，`/settings show model_auth_ref` 仍能查看。
- **其余候选保留**：分类时的保留理由（急停开关、安装扫描上限、租约时长）仍成立，生产未覆盖；理由与复核清单见
  [参数中心](docs/design/PARAMETER_CENTER.md) 的“减量收口”一条。
- **验证**：见 TESTS.md 同名节。

## 前端设置页与前端数据清理：删除已不存在的配置项（2026-10-01，分支 `worker/ds1-frontend-settings`，基于 `claude/3a-step16y` 的 `5761f77bf`，已上线 step17a（main de222698b，2026-10-02））

- **现象**：设置页有 29 个表单项对不上任何当前配置键（`label="键（` 逐一核对，均不在三份随包 YAML 与 `backend-config-catalog.json` 的
  270 项权威键集合里）：真删过的键（scheduler_mode、task_max_grandchildren、Dispatch Loop 三项、enable_watchdog 等）和从来不是
  AgentConfig 字段的键（qq_*、memory_limit 等）混在一起；`settingsStore.ts` 与 `frontend-runtime-config.json` 也留着这些已删键。
- **做法（P4）**：
  - 删 29 个死表单项：SettingsDispatch（Dispatch Loop 段、scheduler_mode、Daemon 段）、SettingsModel（timeout）、SettingsMemory
    （Basic Memory 段）、SettingsGateway（enable_watchdog）、SettingsAdapters（feishu_webhook_url、QQ 段）、SettingsSecurity
    （Security Policy 段、Notifications 段）、SettingsSubagents（工作流/验收/孙代理相关 7 项）。
  - store/JSON 同步：dispatch/memory/acceptance/security/notification/watchdog 整组删除；daemon 保留 runner_instruction、runner 保留
    5 个在用键、subagent 保留 task_max_subagents、workflow 保留 enable_self_learning、memoryAdvanced/tools/model 死字段删除。
  - 外部引用一并处理：Tools.tsx 预算窗口卡片（引用已删的 window_seconds）、authStore.ts 敏感字段清单里的已删键。
  - 保留：audit/localStore/session 组、log_level、daemon_runner_instruction（在权威集合）；SettingsTools 的“检索限制”是前端本地设置
    （label 明确“不对应后端配置键”），保留。
- **P5 守卫**：新增 `agent_py_agent/tests/test_frontend_settings_labels.py`（不依赖 node）：设置页所有 `label="键（` 必须都在权威键集合
  （失败列差异），settingsStore 引用的运行时配置组必须存在。
- **验证**：守卫 2 passed；设置页 label 键 46 个全部在权威集合（dead=0）。`bun run build` 因本机无 `frontend/node_modules`
  （tsc: command not found）跑不了，未联网安装；tsc/vite 构建验证留待集成环境。
- **文档**：PARAMETER_CENTER.md、ROADMAP.md 同步（两个“未排期/暂留”条目标完成）。

## 参数中心 P7 全量默认值一致性测试与 P3 补齐 8 个保留键说明（2026-10-01，分支 `worker/ds2-param-parity`，已上线 step17a（main de222698b，2026-10-02））

- **P7**：新增 `agent_py_agent/tests/test_config_defaults_parity.py`——用正式加载器 `load_config` 读随包 agent_config.yaml，对 `AgentConfig` 全部字段逐个断言加载值等于 dataclass 默认值（219 个 YAML 键当前全部一致，值级白名单为空）。5 个加载器运行时元数据键（config_layers/config_path/config_sources/config_warnings/memory_config_warnings）跳过值比较但断言仍存在；值级白名单结构保留且要求“放进去的键必须仍不一致”，防止白名单烂掉。api_key 由 `api_key_env` 环境变量注入，测试先清 `AGENT_API_KEY` 再加载，避免本机环境污染。实现了 PARAMETER_CENTER.md 目标第 2 条“随包 YAML、AgentConfig 默认值和文档一致，由测试核对”的全量落点。
- **P3**：基线 `parameter_description_baseline.json` 第二组 8 个保留 advanced 键补中文说明（写进 agent_config.yaml 各键正上方注释：dynamic_timeout_min、home_lesson_stale_caveat_days、local_store_fts_enabled、memory_curator_daily_finalize_hour、memory_curator_model、memory_curator_provider、memory_hot_min_occurrences、memory_lesson_min_occurrences）；后台 Memory Curator 整段挂在 `memory_curator_enabled` 上的注释块拆开归到各自键，删掉已删除键（batch_message_limit、max_retries）的说明。基线只剩 5 个加载器元数据键，`test_parameter_registry.py` 通过。
- **验证**：`test_parameter_registry.py` + `test_config_defaults_parity.py` + `test_config_validation.py` + `test_merged_config_knobs.py` 共 134 passed；架构守卫 guards9 与静态 gate 全过（详见 TESTS.md）。

## 前端参数目录生成器修正、重新生成与守卫（2026-10-01，分支 `worker/ds1-config-catalog`，基于 main `34e4d874e`，已上线 step17a（main de222698b，2026-10-02））

- **现象**：`frontend/config/backend-config-catalog.json` 过期：缺 `goal_continuation_idle_limit`、`model_reasoning_levels`；
  `5c224e6df` 手插一项后头部 fieldCount 写 216 实际 217、后续 order 未顺移，`node frontend/scripts/sync-backend-config.mjs --check` 必然失败。
- **做法（P6，生成器以后端为准，不改后端）**：
  - 注释归属对齐 `parameter_registry._descriptions_from_lines`：空行或任何非注释行都中断注释块（原实现只在首键前清空、且最多取最后 8 行注释）；
    无上方注释时取行尾注释，对齐 `config_io.yaml_trailing_comment` 的引号规则。逐键比对后 27 处说明差异清零。
  - 删除已删键的映射条目：实测 13 个不在三个随包 YAML 键集合里（choiceMap 5：scheduler_mode、runner_failure_policy、daemon_max_runners、
    daemon_reviewer、subagent_workflow_mode；unitMap 8：tool_agent_budget_window_seconds、tool_artifact_read_budget_window_seconds、
    gateway_heartbeat_interval、gateway_stale_seconds、lease_heartbeat_interval_seconds、subagent_due_check_interval、
    dynamic_timeout_safety_margin、memory_hook_retention_days）。
  - `restartRequired` 不再按键名猜：后端每个参数 `effect=EFFECT_GATEWAY_RESTART`（`user_config_capability.py` 的 TUNABLE_KEYS 也没有
    next_session 的键），配置修改一律要重启 Gateway 才生效，因此全部标记为需要重启（true）。
- **P1 重新生成**：270 项（agent_config 219、capability_config 31、log_analysis_config 20），`--check` 通过；目录 270 项 description 与
  后端 `_descriptions_from_lines` 逐键比对 0 差异。
- **P2 守卫**：新增 `agent_py_agent/tests/test_backend_config_catalog.py`（3 项，不依赖 node）：各随包 YAML 键集合 == 目录键集合（分别比，
  失败列差异）、目录 description 与后端一致、`restartRequired` 全 true；YAML 注释或键变化后目录未重新生成时会立刻失败。

## Jev 前台默认期限 2→3 秒（J5）与实际版本号落结构化事实（J19）（2026-10-01，分支 `claude/be-jev-deadline-version`，基于 main `34e4d874e`，已上线 step17a（main de222698b，2026-10-02））

- **J5 前台默认期限**：`decision_timeout_seconds` 的仓库默认值（dataclass 与随包 YAML）从 2.0 改为 3.0；点位不单独设期限时继承它，阶段默认 4 秒不变。
  - 为什么：Jev 经本机代理单次常在 2–3 秒，2 秒下约六成超时，超时的请求供应商多半照样计费却没有产出。
  - 选模型保持用户在生产设的 5 秒：那是覆盖层的值，改默认值不影响它。
  - 生产现状（只读核对）：owner 覆盖层把通用期限、阶段期限和每个点位都显式设成 5 秒，后台 15 秒，所以这次默认值改动在生产上没有效果。
    要让生产前台点位也用 3 秒，需要集成方用 decision_patch 删掉这些点位覆盖并把通用期限改成 3 秒；选模型的 5 秒和阶段 5 秒保留。
  - 前端参数目录在基线上本来就过期（参数中心 P1），这次只手改了这一项的默认值，没有整体重新生成。
- **J19 实际版本**：决策结果日志成功拿到供应商响应的行另记 `requested_model` / `model_version`；`audit_records` 决策主题的 points 多 `model_versions`（请求名、实际版本、次数），最近行带 `model_version`。只读结构化字段，不改调用和采用逻辑。
  - 钉住规则：供应商给出带版本号的 id 才钉住。官方文档写明 `jev-1.13.0` 这类 id 可直接请求，`jev-latest` 当前指向它，所以条件满足。
  - 钉住是档案数据：把决策档案的模型名改成 `jev-1.13.0`（产品 `save_model` 编辑操作），代码里没有默认模型名要改。生产档案由集成方改，改前改后都能在 `model_versions` 里核对。
  - 代价：钉住后供应商发新版本不会自动跟上；以后换版本要人工改档案，`model_versions` 里请求名与实际版本一致说明钉住生效。
- **真实验收**（隔离 home、私有端口 8438，目录副本只放 Jev 与 MiniMax，Jev 实际 2 次调用）：请求 `jev-latest` 返回 `jev-1.13.0`；钉住后请求 `jev-1.13.0` 返回 `jev-1.13.0`；两次选模型观察都成功（1.3 秒、1.5 秒），TLS 握手各约 0.9 秒（每次新建连接，留给 J1）。
- **验证**：见 TESTS.md 同名节。

## C12a／C12b：被中断任务的后台进程 Gateway 里 /stop 收不回；gateway stop 后的孤儿进程（2026-10-01，分支 `claude/38-c12-stop-after-interrupt`，基于 main `34e4d874e`，已上线 step17a（main de222698b，2026-10-02））

- **C12a 现象**（R10 深度切片 09-28）：回合被 `/interrupt` 后，同会话 `/stop` 回"当前没有运行中的内容"，任务的后台进程一直跑到 Gateway 停止以后。
- **C12a 原因**（10-01 在隔离 Gateway + 假模型 + 真 TUI 上复现，代码与 step16x 相同）：
  - `/interrupt` 只结束本轮，任务关联仍是 active。中断后马上 `/stop` 走正常任务停止，能收回（复现 1）。
  - 约 2–4 分钟后，发现层账本自愈（`owner_wake_discovery._project_task_ledger_terminal`，operator `wake-discovery-ledger-heal`）看到"主执行轮已 cancelled、关联还 active"，把任务记成 cancelled、关掉 TaskRun，但后台进程不动。之后 `/stop` 找不到可控任务，只能回"没有运行中的内容"（复现 2）。R10 原证据的 runtime_events 也是这样：中断后 230 秒 ledger-heal 关单，32 秒后 `/stop` ok=false。
  - 09-28 的修复 3f17b1e7a 只给本地 chat 入口加了"无回合时回收本会话遗留资源"。默认 TUI 和飞书走的 Gateway 入口一直没有。
- **C12a 做法**：
  - Gateway `/stop` 在没有热请求、也没有可控根任务时，改为回收本会话遗留的托管后台进程（`control_service._stop_session_leftovers`）。选择规则：
    - 只认本会话精确 thread_id 的登记记录；
    - 本会话仍 active 的任务（审计、分离任务、子代理投影，各有自己的入口）和执行状态里仍在跑的任务，一律不碰；
    - 每条记录按自己的 (thread, root_task, run) 走任务停止同一个 `freeze_process_stop`，进程组回收交给锁外线程，和 `/stop` 停任务同一条路；
    - 会话任务记录或执行状态读不出，就报 `TASK_RESOURCE_STOP_UNCONFIRMED`，一个都不停。
  - 回执列出每个 pid 和 task/run 归属。TUI 和飞书都经这一个入口。
  - 没有运行中回合时，`/interrupt` 在两个入口都只回"当前没有执行中的回合，无需中断。"，不碰任何资源（中断不冒充资源清理）。本地入口原来会顺手回收，这次一并对齐。
  - 回执渲染 `background_resource_lines` 从 cli 移到 `gateway_parts/background_resource_report.py`，两个入口共用。
- **C12b 复核**（R10 边界项"gateway stop 后两个任务后台进程由 init 接管继续跑"）：
  - 09-28 的 2ffb30f51 合入在 R10 被测版本之后，已经处理了这一项。托管后台进程跨 Gateway 停止或重启存续是设计，生产依赖这一点。`gateway stop` 会列出它们（pid、所属任务、状态），并提示加 `--stop-background`；加上这个参数就按进程组回收。
  - 10-01 在 base 代码上实测：普通 `gateway stop` 后，进程被列出并由 init 接管（PPID 1）继续跑；`gateway stop --stop-background` 后整组退出（SIGTERM，确认，组内无残留）。Gateway 再起来后，用户也能在原会话里用 `/stop` 收（见 C12a）。
  - 结论：不改代码。
- **待定**：
  - （已按第 7 条处理，见台账顶部同名条目）中断后几分钟，自愈会把仍 active 的中断任务记成 cancelled，之后同会话的新消息会开新任务目录。现在自愈认出用户中断（运行库事件 `agent_run.user_interrupted`，绑定被中断的执行代次），24 小时窗口内不收口，用户再发消息接着原任务。
  - 先 `/interrupt` 再马上 `/stop` 时，TaskRun 不在 `/stop` 当下关，而是约 2 分钟后由发现层按 `conversation_task_interrupted` 关（实测）。不算泄漏，不改。
- **验证**：见 TESTS.md 同名节。证据（仓库外）：`~/.my-agent/decision-evidence/c12-observations-20261001/`。

## J6 决策实验自动晋升提示（已实施（2026-10-01），分支 `worker/sol2-promotion-notice`，已上线 step17a（main de222698b，2026-10-02））

- **解决问题**：授权内 off→apply 改了本会话决策设置，但用户在 TUI 和飞书都看不到提示。
- **实现**：回执 `gateway_decision_experiment_promotion.v1` 追加 `promotion_id`（授权编号派生，每授权最多一份）和冻结的 `evaluation.rule`。
  只把本次真正持久化的 `applied` 回执变成中文提示，说明点位、前后有效模式、样本数/门槛和恢复继承入口；不读模型正文。
- **唯一送达链**：复用 Goal 熔断的 `pending_host_notices → host_notice → canonical final/channel_delivery → 原 IM DeliveryService`。
  晋升发生在收尾，新提示在当轮追加发布并合入原最终提交批次；不重发开轮提示，不新增发送线程、通知账或配置。
- **去重**：`notice_id=promotion_id`；原请求已有任何晋升回执便不再返回新回执，消费后重建 Store/执行代次不补投；IM 沿原 final/sent 回执去重。
- **撤销**：`/model → 选择模型 → 决策模型 → 本会话临时设置 → 逐字段恢复继承 → points.skill_tool.mode`；也可让助手经原设置服务恢复本会话继承。
  `/experiment` 的授权撤销不是设置回滚，不编造 `/experiment revoke` 命令。
- **验证边界**：111 项聚焦回归、166 项架构守卫与 3 项变异已有本地证据，详见 TESTS 同名节。已随 step17a 上线（main de222698b，2026-10-02）；真实终端及真实飞书收信仍未验证，待沙箱外复核。
  沿原“提交即已读”口径；回执写盘与线程提示队列非跨文件事务，极端中断/排队失败可能漏提示，不扫旧回执补发。

## 工具参数互斥组单处声明与模型可见冲突详情（2026-10-01，分支 `worker/sol56-arg-conflicts`，基于 main `34e4d874e`，已上线 step17a（main de222698b，2026-10-02）与真实模型复测）

- **现象**：能力包真实验收里，模型三次把 `write_file.source_ref` 与 `content` 同传。旧 handler 虽以 `TOOL_INVALID_ARGUMENTS` 拒绝且回执完整，但没有机器可读的冲突参数列表；模型随后改为手抄正文或自建脚本，没有恢复原样复制链。
- **合同**：工具在唯一 `ToolModelSpec.input_schema` 中用宿主扩展 `x-exclusive-argument-groups` 声明互斥组。canonicalizer 在快照构造时校验组至少两个字段、字段不重复且都属于同层 `properties`；声明参与 `schema_hash`，公共输入 validator 按声明顺序检查所有对象节点。判断只看参数名是否实际出现，空串和 `null` 也不会被静默丢弃。
- **错误与模型提示**：冲突沿用登记过的 `TOOL_INVALID_ARGUMENTS`，每条 issue 的 `details` 以及 ActionDecision 的 `evidence.details` 都带 `conflicting_arguments` 和 `exclusive_group`。Executor 只从这些结构化字段渲染有界 JSON 提示，真实 `ToolResult.render_for_model_prompt()` 可见冲突参数名；不回显参数值，也不自动替模型选择保留项。
- **provider 边界**：宿主扩展留在冻结 schema 中供运行时和 manifest 使用，发给 Anthropic/OpenAI 前从隔离副本递归剥离，避免把第三方未承诺支持的自定义关键字送出；标准 schema 约束保持不变。
- **首个使用者**：`write_file` 只声明一组 `content`／`data_base64`／可选 `source_ref`。旧 handler 中两份互斥判断删除；`source_ref` 仅允许 overwrite、至少需要一个正文来源、base64 解码等不同业务约束仍留在原位置。
- **验证边界**：声明解析、单组和多组冲突、无冲突、provider 投影、真实 write_file schema、handler 未执行及模型可见结果已有仓库测试和变异覆盖。本分支按任务约束没有启动 Gateway 或运行真实模型，改善真实模型恢复行为仍待后续复测。
- **验证**：见 TESTS.md 同名节。

## C10：IM 的 /plugins 入口（2026-10-01，sol，已实现，已集成 step16z `f93968a95`）

- **解决问题**：插件和能力包装卸只有 TUI 入口，IM 用户无法直接查看或管理。
- **做法**：会话层只识别公共插件命名空间并保留原文；`/ask`、`/control` 进入原持久控制回执，
  `plugin_command_service` 为 IM 与 TUI 组装同一 `PluginManagement`，不另写参数解析、执行器或审批账。
- **权限**：查看及业务调用沿原插件规则；管理动作仅管理员。IM 与 `/settings` 一样，以完整且已解析的
  `local/main` 身份判断管理员（含已绑定管理员的私聊），正文中的角色、owner 自述无效。拒绝保留结构化及可见错误码。
- **确认**：含可执行文件或外部解释器的包沿原启用预览与 `--confirm <确认码>`，未确认前不启动；
  普通危险工具审批仍受原工具策略约束，本轮不另造 IM 确认通道。IM 纯文本不实现 TUI 本地面板动作。
- **回执口径**（3a 集成时核定，2026-10-02）：IM 回执是公共持久控制回执，只与 TUI 共用 `ok/message/request_id/error_code`；
  TUI 目录回执里的细分 `reason`（如 invalid_plugin_id、unclosed_quote）不进 IM 回执，机器判断只看 `error_code`。
  关闭按用户分 owner（`gateway_per_user_owner_scoping: false`）时，所有 IM 请求本来就在本机 local/main 上运行，
  按同一规则算管理员，与 `/settings` 一致。旧的 `/ask`、`/control` 短路用例已按此更新（`7e0fbcec1`、`b6f25978c`）。
- **状态**：本地开发验证见 TESTS；未启动或部署 Gateway，真实飞书/QQ 收发尚未验证。

## 能力包版本字段抄错不再报"快照失效"，改为可修参数（H1，同分支含 H3、H4）（2026-10-01，分支 `claude/ae-skill-continuation-mismatch`，基于 main `626b8576c`（初版在 `34e4d874e`），已上线 step17a（main de222698b，2026-10-02））

- **来源**：能力包验收 C1 第 1 次真实运行（gpt-6-luna）。模型读 A 包时把 `expected_package_sha256` 抄错（前 36 位对，后面是编的），
  `skill_search` 回 `SKILL_SNAPSHOT_UNAVAILABLE`（details `SKILL_SNAPSHOT_STALE`），模型看到的恢复建议是"停止使用旧授权，由父代理按当前
  快照重新授权"。主线程没有父代理，包其实就在当前快照里；模型试 3 次后放弃 A 包，0 集产出。
- **判断**：`_package_action` 走到版本比对时，包已经由当前（可能受限的）快照解析成功，说明这一代已获授权；真正的授权失效
  （受限快照里授权的代次已变）在取快照时就由 `restricted` 抛出，走不到这里。所以此处的不符只是模型参数问题（抄错，或沿用旧版本
  建议），应归参数错误，和 C18 的"未声明成员"同一类。
- **做法**：
  - `package_read.continuation_mismatches` 按固定顺序列出与当前包不符的显式版本字段名；宿主读取入口 `validate_package_continuation`
    改为复用它，仍抛 `SKILL_SNAPSHOT_STALE`，行为不变。
  - `skill_search` 遇到不符时返回 `TOOL_INVALID_ARGUMENTS`（原恢复合同选 `repair_tool_arguments`），回执带 `error=CAPABILITY_PACKAGE_CONTINUATION_MISMATCH`、
    `continuation_mismatch`（字段名列表）和提示：旧页码作废，不带这些字段重新 search，再原样用当前 `next_read` 从入口重读。
- **守住的边界**：
  - 不回显当前摘要或激活代次，不给同名新一代的 `next_read`；模型必须自己重新检索，"不沿旧建议静默换代"的原合同保留。
  - 不读正文、不 pin。未知或未授权的包、受限快照代次失效、成员真实缺字节或摘要变化仍走原快照错误。
  - 没有新增错误码，也不改工具 schema。
- **验证**：见 TESTS.md 同名节。修复版重跑 C1 的结果记在 `docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md` 的 C1 节。
- **同分支追加 H3（C3 实测，3a 指派）**：
  - **问题**：主任务固定过的包被停用或换代后，任务快照会把它剔除，并留下结构化诊断（`CAPABILITY_PACKAGE_PIN_STALE`／`_PIN_UNAVAILABLE`）。但 `skill_search` 仍回 `SKILL_SNAPSHOT_UNAVAILABLE`，配的是子代理口径的提示“由父代理重新授权”。主线程没有父代理，模型照提示去调 `resolve_capability_requests`，失败。
  - **做法**：现在先看有没有这条 pin 诊断。有的话回新登记的 `CAPABILITY_PACKAGE_TASK_PIN_UNAVAILABLE`（`report_blocker`），提示变成：本任务不能再用这个包，不要申请授权，也不要从磁盘读包；请告诉用户，或在新请求里重来。回执带 `details.error_code`，不给新一代的读取建议，也不读、不 pin。
  - **不变的部分**：没有 pin 诊断的缺包，以及子代理受限快照失效，仍走原快照错误。
- **同分支追加 H4（C1 第 2 次实测）**：
  - **问题**：子代理的 `capability_request` 在写申请账之前，会因参数不全（`TOOL_PARAMETER_REQUIRED`）、root 不可申请（`TOOL_NOT_ALLOWED`）等被拒。这些拒绝没有声明 `effect_outcome`，又不在执行前确定失败白名单里，操作账因此落成 `UNKNOWN`（`TOOL_OPERATION_OUTCOME_UNKNOWN`，reconcile）。这和 D2 是同一类问题。
  - **做法**：唯一的失败出口 `_capability_error` 固定声明 `effect_outcome=not_started`，操作账改落 failed。

## C12c：发现层补关"根本没有会话任务"的执行总账（D3 崩溃窗口）（2026-10-01，分支 `claude/38-c12c-discovery-no-link`，基于 main `34e4d874e`，已上线 step17a（main de222698b，2026-10-02））

- **来源**：D3 修复的待定项。主执行轮已在运行库收口、TaskRun 收口边还没跑时进程崩溃，发现层的崩溃重放（`owner_wake_discovery._reconcile_terminal_conversation_task_runs`）只认可读的终态关联，分不出"没有关联"和"关联读坏"，没人补关。
- **真实链路复现**（隔离 home，测试侧暂停钩子停在 `task_run_closeout.settle_terminal_task_run` 入口、再 SIGKILL；base = step16x 代码）：
  - Gateway 前台请求：崩溃后请求停在 processing；网关重启时请求恢复把它重跑一遍（多一次模型调用，attempt 第 2 代），新执行轮的收口边按 `no_conversation_task` 关掉了 TaskRun。所以这条路自己会补上，不留永久泄漏。
  - CLI 一次性 `run`（没有请求恢复）：崩溃后 TaskRun 永远 `created`。base 网关跑 200 秒加一次显式发现扫描都不关；换成本分支的网关，启动后第一轮发现就关掉（operator `wake-discovery-task-run-reconcile`，reason `no_conversation_task`）。
- **做法**：发现层重放补上与运行时收口边同一条规则：
  - 关联文件确实不存在，就交给代理树决定。
  - "确实不存在"用文件系统事实判断，并且在 CAS 前一刻现查：规范目录 `*/conversations/tasks` 下有任何 `<task_id>.json`，不管读不读得出来，都保持打开；目录列不全也保持打开。
  - 还要求根主执行轮已终态才去试 CAS；整棵树是否终态或静止，仍由 `settle_task_run_if_agent_tree_terminal` 判断，unknown 不算静止。
- **生产影响**（只读试算，把各 owner 的 runtime.db 拷到 scratch 上试跑同一个 CAS）：上线后第一轮发现会一次性补关 338 条历史总账，全部是建成超过一天的，绝大多数是 step16v 之前 D3 留下的。分布：local/main 327 条，飞书用户 6 条，测试 owner 5 条。另有 55 条树未结束（含 unknown）继续保持打开，不受影响。读开放 TaskRun 的只有发现层和 /recover 子代理分支，后者要求 unknown 执行轮，这类行本来就不会被关。
- **边界**：原先补关扫描只认默认布局下的会话存储，配置了 `conversation_workspace` 指向别处时，关联文件不在扫描范围内，会被当成不存在而误关。
  已按第 15 条修复（用户拍板：补关扫描跟着配置走；2026-10-02，分支 `claude/9b-taskrun-scan-conv-root`，基于 `claude/3a-step16z` `5e972003e`，待集成）：
  - 基础 owner 的补关入口是会话运行时的低频唤醒对账（`_reconcile_wake_queue`）。它把本 agent 实际会话存储的 `storage.tasks_dir` 交给 `unfinished_task_ids`。
    这个目录由 `runtime_paths` 的同一解析入口算出，与 `ConversationStore` 一致，配置了 `conversation_workspace` 时就是配置的位置，不另推路径。
  - `_conversation_task_link_scan` 是唯一扫描入口：它在默认布局通配之外时额外扫这个目录，并补进补关判断"关联是否存在"的目录清单；列不全时补关一律保持打开。
  - 3a 集成时补一条：配置的目录整个不在时也按列不全处理、保持打开。会话存储初始化时就建好这个目录（`store_layout.managed_dirs`），它不在只可能是外接盘没挂上这类异常，不能把"看不到"当成"没有关联"。
  - Gateway 发现层只扫 `owners/providers/*` 下的 owner。这些 owner 的作用域 agent 清空了运行路径覆盖（`_config_without_runtime_paths`），会话存储一定在默认布局，所以发现层不传目录，行为不变。
  - 默认布局行为逐字不变；传进来的目录本身就在默认布局里时不重复扫描。
  - 万一误关一条，之后的新执行会照常 `task_run.reopened`，可以恢复。
- **验证**：见 TESTS.md 同名节。证据（仓库外）：`~/.my-agent/decision-evidence/c12-observations-20261001/c12c/`。

## Responses 失败事件按服务商错误码分类（2026-10-01，分支 `claude/3a-responses-failed`，基于 main `0ca852195`，已上线 step17a（main de222698b，2026-10-02））

- **现象**：主会话（gpt-6.1-sol，ChatGPT 订阅 Responses）的一次派活请求在第 6 轮工具后以 `ProviderResponseError: Responses 服务返回失败事件`
  结束，错误码只是异常类名 `PROVIDERRESPONSEERROR`；当时估算上下文 235358／272000（触发线 244800）。宿主把服务商给的错误对象整个
  丢掉：诊断查不到原因，上下文超限走不到既有的压缩恢复，限流／临时故障也走不到各自的重试路线。
- **做法**：新文件 `backends/responses_failure.py`。`responses_wire.collect_response`（SSE 与 WebSocket 共用）遇到 `error`／`response.failed`
  时取服务商错误对象（`response.error`，或 error 事件的 `error`／顶层 code、message、param；只留白名单字段并限长），与 HTTP 同一组判定：
  上下文超限 → `ProviderContextWindowError`（主循环 `context_pressure_response` 压缩后重试）；硬额度 → `ProviderQuotaExhaustedError`；
  限流码 → `ProviderUsageLimitError`；服务端繁忙／内部错误 → `ProviderTransientError`；其余 → `ProviderResponseError`（新登记错误码
  `MODEL_RESPONSE_FAILED`），服务商错误码与消息留在 details 供诊断。
- **守住的边界**：只看结构化错误对象，不按模型名或地址分支；已知错误码表只是优化，认不出的码保留原文、不会因此被丢弃；用户可见文字
  只带错误码，服务商原始消息只进诊断；重试、压缩策略本身不改。
- **真实核对**：gpt-6-luna 下故意请求不存在的模型，服务商回 `error` 事件（`error.type=invalid_request_error`，无 code），宿主按新逻辑
  分到 `MODEL_RESPONSE_FAILED` 并保留原因；短请求正常。详见 TESTS.md 同名节。
- **待定**：
  - 触发这次修复的那一单，真实原因已无法追溯（当时没有记录）；上线后若再出现，看诊断里的 `provider_error.code`。
  - 观察：gpt-6-luna 实际接受了约 55.9 万输入 token（档案窗口 272000，宿主在估算 244800 时压缩），服务商实际上下文明显更大。
    要不要按服务商实际能力上调窗口（会推迟压缩、单次请求更贵）交用户决定；在那之前保持现状，宁可早压缩。
  - 2026-10-01 C6 实测（ae，6 次真实调用）：800018、900018 输入 token 通过；922000、922018、950018、1000018 都被服务商以
    `context_length_exceeded` 拒绝，宿主归为 `ProviderContextWindowError`（`MODEL_CONTEXT_WINDOW_EXCEEDED`）。实际上限在 900018（含）到
    922000（不含）之间（经产品自己的 Responses 后端发出）。3a 决定按建议改为 900000，由 3a 用正式入口改生产档案；
    生产另有 300000 的触发封顶，不会一次吃进 80 万。
- **验证**：见 TESTS.md 同名节。

## 一次能力包选择在 gpt-6-luna 上失败：严格 schema 关键字 + 失败原因可诊断（2026-10-01，分支 `claude/ae-selection-failure-cause`，基于 main `0ca852195`，已实现，step16x 已上线并经真实 gpt-6-luna 复核）

- **来源**：能力包验收 D5。链式 G01 重跑时，宿主一次选择在 gpt-6-luna 上 `outcome=failed`（`CAPABILITY_SELECTION_MODEL_FAILED`、`CAPABILITY_SELECTION_USAGE_NOT_REPORTED`）。`select_capability_packages` 用宽泛的 `except Exception` 吞掉了异常，没有日志，也没有结构化原因，从现有事实里查不出根因。
- **根因（真实 luna 实测）**：选择用的 response schema 在数组上写了 `uniqueItems` 和 `maxItems`。Responses 订阅接口按严格模式校验 `text.format` 的 json_schema，用失败事件拒绝：`invalid_json_schema`，param `text.format.schema`，type `invalid_request_error`。
  - 同一份代码只把这两个关键字加回去，就复现失败，诊断字段正好记下上述原因；去掉后 luna 和 MiniMax 都正常选中。
  - 订阅模式下 `instructions` 为空串并不影响：修复后的同一调用成功了。所以不改 instructions，也不改 Responses 后端。
- **做法**：
  - schema 只用严格模式都接受的关键字（数组只写 items/enum）；不重复、不越界继续由 `_selected_references` 在本地严格判定。
  - 新增 `capability/package_selection_failure.py`：辅助调用抛异常时，从异常的结构化属性取出类型名、宿主错误码、HTTP 状态和服务商错误的 code/type/param。都是短标记，不合格的值丢弃，不读正文。
  - 结果写进 `host_capability_selection.v1` 的新字段 `failure`：只在 failed 时允许，构造时校验键和值，为空时不序列化，旧记录字节不变。同时记一行宿主日志；不进模型上下文。
  - 没有新增错误码，沿用登记表里的 `CAPABILITY_SELECTION_MODEL_FAILED` 和 provider 原有码。
- **依赖说明**：流式失败事件里的服务商 code/param 来自 3a 的 Responses 失败事件分类（`claude/3a-responses-failed`，46a9eac86）。它合入前，这类失败只记得到 `error_type`；HTTP 4xx 拒绝不依赖它。
- **边界**：旧版程序读到带 `failure` 的失败记录时，会把整个标记当损坏处理，按既有规则只警告、保留原值、不阻断任务。成功和空选的记录不受影响。
- **验证**：
  - 新增 `test_package_selection_failure.py` 共 12 项：原因提取不含正文、自由文本与越界值丢弃、schema 只用严格关键字、本地仍拒绝重复和未知 id、订阅 Responses 假传输的请求体与失败事件、回执校验与往返。
  - `test_capability_package_selection_runtime.py` 新增一项：provider 400 的原因写进回执，但不进模型上下文。
  - 12 个变异全部被抓住。
  - 真实运行：luna 修复后 selected；luna 修复前复现 invalid_json_schema；MiniMax 修复后 selected。证据在 `~/.my-agent/decision-evidence/d5-selection-schema/`（仓库外）。

## 能力包 G01 生产规模真实运行与串行重跑（2026-10-01，3a 裁定；两条均已出结论）

- **G01 压缩后原资源链仍未覆盖**：ae 在 step16v 上用 gpt-6-luna 跑了一次生产规模长任务（36 份输入、约 58 万字），产物全对、
  止损线一条没碰，但模型自然地把工作分给 6 个子代理，主线程上下文峰值约 135.6K，低于触发线 244800，全程没有压缩。按规矩记“未命中”，
  不为凑压缩改写需求诱导模型不派子代理。随后用“每一步依赖上一步”的串行长任务（40 份链式记录、约 121 万字）再跑一次 gpt-6-luna：
  模型没派子代理，而是写脚本批量处理整条链，原文没进上下文（峰值约 75K），40/40 正确，仍然没有压缩。两次真实运行模型都自然避开
  上下文膨胀，G01/G02 按“取决于模型做法、未命中”记账（提交 `57cdeef19`／`106fc3b5d`），见[链式重跑](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#g01g02-链式长任务真实重跑gpt-6-luna2026-10-01)。
- **前台回复不可用、任务在后台做完（2026-10-01 已核实：送达，不改代码）**：前台请求在 +451 秒以 `USER_REPLY_UNAVAILABLE` 结束
  （模型在 2 个子代理仍在跑时连续给出空正文，宿主按有界重试后如实失败，这是既有合同），之后父级后台续跑写完 report.md，TaskRun 在约
  +975 秒关闭为 done。真机当时没记投递事实；用真实网关循环加脚本化假模型在 step16w 运行时上复现同一形状（前台只有思考、两次补问后
  `USER_REPLY_UNAVAILABLE`，子代理随后结束），结论是已送达：
  - 后台路线按线程自己的通道绑定解析（`background_routing.resolve_background_route`），与前台请求成败无关；TUI 线程走 `chat` 这个本地
    transcript 通道。
  - 末个子代理结束的 wake 经 5 秒成功完成合批后由父级后台片收尾，投递判定 `root_subagents_terminal`；回复落到 canonical 会话记录
    （助手 final 带 `background_delivery_reason`），本地通道投递状态为 `not_applicable`（不欠外发）。
  - TUI 轮询的后台消息页（`read_background_response_page`，Gateway 后台通知接口读的就是它）返回这条 `assistant_response`；复现里正在运行的
    TUI 也显示了这条回复。
  - 飞书等外发通道走同一条路线解析，外发义务由 `background_owner_delivery_committed` 把关（未送达不确认、冻结重投）；这次没有在真实飞书上复现。
  - 不需要另加“完成通知”。`test_background_reply_after_foreground_failure.py` 钉住这条链（真实 echo Gateway、真实工具循环、真实后台调度器与
    `agent.delivery_service`），变异 5/5。证据：`~/.my-agent/decision-evidence/bg-reply-after-fg-failure-20261001/`（仓库外）。
  - 真实模型复核（MiniMax-M2.7，step16w 运行时，隔离环境，用户要求尽量用真实模型）：自然需求下模型派出子代理后前台约 12 秒以 done 结束、
    子代理仍在跑；子代理写完 3 个文件后父级后台片收尾，投递 `root_subagents_terminal`、本地通道 `not_applicable`、落 canonical，线程多一条
    带投递原因的助手 final，TUI 后台消息页返回 1 条 `assistant_response`。前台“失败”形状这次真实模型没走到，由假模型复现与上面的测试覆盖；
    第一次措辞（“派出去后直接告诉我已经派了”）模型没调任何工具，记为模型未命中。证据在同目录 `real-model-minimax/`。

## Gateway 前台新消息清零 Goal 空片计数（D4）与“熔断后又起一片”裁定（O2）（2026-10-01，分支 `claude/38-goal-fuse-gateway-reset`，基于 main `b7058e33a`，D4 已实现，step16w 已上线并经真实模型复核）

- **D4 现象**（ae 复测 G05）：TUI/飞书经 Gateway 发来的新消息（`gwreq-1790863471`）之后 `idle_slices` 仍是 3；设计是新用户消息清零计数。
- **D4 原因**：`reset_goal_progress_fuse` 只接在 `conversation/runtime.ChannelMessageRuntime.receive` 与 `agent_core/cli_run_conversation`
  （非续跑消息）两个入口；Gateway 前台 `gateway_parts/request_execution._execute_gateway_conversation_turn` 用
  `append_gateway_conversation_message(role="user")` 写入用户消息后没有重置。
- **D4 做法**：同一函数里用户消息写入成功后调用 `reset_goal_progress_fuse(store, thread_id)`（默认 `clear_reason=False`），与另两个入口同口径：
  只清计数，熔断暂停的 Goal 保留暂停与原因码，恢复仍走 `/goal resume`；写入失败时不清。没有新配置项（纯缺陷修复，沿用既有语义）。
- **D4 边界**：Goal 自动续跑片是后台 wake（`background_goal.continue_goal_after_report` → `goal_runtime.raise_goal_continuation_wake` →
  后台调度器 `run_once`），不经过 Gateway 前台回合，所以不会被这次重置清零；Gateway 前台请求的生产者只有 TUI/IM/HTTP 用户提交
  （含用户输入的系统任务命令）与 scale 转发，没有后台自造的前台请求。合同测试同时钉住两个方向。
- **O2 现象**（同一 G05）：熔断在 +32.9 s 暂停 Goal 后 0.1 s，同一 main run 又起了一片（`attempt-1790862721-b5a7ea38`，+33.0–37.7 s，0 工具），
  找不到对应的新 wake，之后再无新片。
- **O2 裁定：不是宿主竞态，不改代码。** 用真实 Gateway 循环加脚本化假模型三轮复现（即时、贴近真实时长、再加前台收尾占住会话 3 秒），
  都是 3 个空片、第 3 片报告记账时熔断、之后没有任何新片或新 wake。关键事实：熔断把 Goal `updated_at` 记成调度 tick 开始时传入的 `now`
  （`record_goal_continuation_fuse` 的 `request.now`），而这一刻早于本片模型运行；复现里暂停后的 `updated_at` 正好等于第 3 片的 tick 开始，
  比第 3 片 attempt 早 0.05 s，熔断真正记账在 5 秒之后。ae 看到的“暂停后又起一片”就是触发熔断的第 3 片本身：ae 的后台 attempt 共 3 个
  （+20、+27、+33），对应 3 个计数 wake，暂停后没有新片。前台占住会话时后台多次进入同一 wake 批次都被推迟，不计数也不运行。
- **观察口径**：判断“暂停之后是否还有片”请用 fuse 记账或 runtime_events/attempt 的真实时间，不要用 Goal `updated_at`。是否给熔断记录另存
  真实记账时刻，留待以后有需要再定，本次不改。
- **证据**：`~/.my-agent/decision-evidence/o2-goal-fuse-timestamp-20261001/`（仓库外：hook 计时事件、假模型日志、脚本、README）。
- **上线与复核**：step16w（main `8d6a5401b`，10-01 08:08 上线）。ae 用 MiniMax-M2.7 在隔离环境复核通过：计数为 1 时发消息清零、之后仍要连续
  3 个空片才暂停；暂停后发消息只清计数、保留暂停和原因码；`/goal resume` 后恢复。证据 `~/.my-agent/decision-evidence/d4-step16w/`。
- **O4（2026-10-02 已实施，见上方“C5/O4：持续目标空片熔断时用户消息先处理”）**：原记录如下。用户消息要等正在跑的那一片结束才处理；
  如果那一片正好是第三个空片，熔断先暂停，消息之后只清零计数，用户还要再 `/goal resume`。当时记为待定：要改成“消息排队期间不记第三个空片”，
  需要会话层在熔断记账前知道本线程有用户消息在排队。现在由执行车道的“用户回合在场”登记提供这个事实。
- **验证**：见 TESTS.md 同名节。

## TUI 单独 /effort 打开档位菜单，查看回执列出可选档位（2026-09-30，分支 `claude/3a-effort-picker`，基于 step16v `d9abcb5ec`，已实现，step16v 已上线）

- **现象**（用户反馈“effort 只能最高，想按自己想法换档好像不行”）：生产控制账里用户 6 次 `/effort` 全是查看，从没设成过别的档位。
  原因有三：全局默认 `model_reasoning_effort` 在 09-27 按用户要求设成了 max，所以每个会话都显示“最高（全局默认）”；TUI 里从补全
  选中 `/effort` 回车会立刻提交（`submit_on_enter`），没机会输入档位；查看回执也不说能选哪些档位、怎么改。
- **做法**：
  - TUI：单独 `/effort` 在 Gateway 模式打开本地档位菜单（新文件 `cli/chat_parts/tui_effort_menu.py`，与 `/model`、`/permissions`
    同一套弹窗和互斥标志）。菜单行是查看、auto/off/low/medium/high/max、default、probe，值就是 `/effort` 参数；选中后经既有控制
    出站箱发 `/effort <值>`，Esc 不发任何命令。带参数的文字形式（含 `/effort status`）照旧直接发给 Gateway。本地模式照旧提示改用 Gateway。
  - Gateway（TUI 与飞书同一服务）：`/effort` 查看回执末尾多一行“可选档位……发送 /effort 加档位只改本会话；/effort default 回到全局默认”，
    由 `reasoning_control.describe_level_choices` 按档位表生成；设置、检测、撤销回执不加。
- **守住的边界**：不改 `/effort` 语法（`conversation/control_commands.py` 属 Codex 重构区，未动）、不改档位换算与发送；菜单不读
  当前档位（TUI 拿不到结构化值，也不解析回执文字），所以默认停在“查看”一行；不改全局默认（那是用户自己的设置，`/settings` 可改）。
- **验证**：见 TESTS.md 同名节。上线后（10-01 06:35，`runtime-step16v-234636ed`）在正式 TUI 里实测：单独 `/effort` 弹出档位菜单，
  Esc 关闭后 Gateway 控制账条数不变（没有发出任何命令）。
- **GPT xhigh/ultra 档位：已实施（2026-10-01）**：C7 将用户档位、菜单、命令、配置与派工扩为固定八档，
  Responses 按当前模型声明选 xhigh/ultra 或明确降档，Chat/Messages 与预算沿原合同；不是按模型名加专项分支。
  服务商声明继续开放，用户界面按本次明确合同显示八档，不动态透传未知原始值。组件验收记录日期 2026-10-02，
  尚未集成/部署，真实核对留给 3a。详见 [智能程度](docs/design/REASONING_EFFORT.md) 第 9 节和 TESTS。

## /recover 能看到并处置本会话子代理留下的未知执行轮（2026-09-30 第一阶段已合入；2026-10-02 C8/C9 已实施，并入 step17d）

- **现象**（ae 真实模型验收 O1）：子代理 runner 在写操作 handler 返回后被 SIGKILL，attempt 与 agent_run 停在 unknown，write_file 停在
  EXECUTING；父级用替补接替（来源 TAKEN_OVER），主代理 done。TaskRun 因 unknown 子代理按树规则一直不关，而 `/recover` 只看
  `thread.workspace_task_id` 的根主代理，回答“没有待核对项”，用户没有任何入口。
- **做法**（第一步，3a 批准）：
  - 新增 `runtime_db/child_recovery.py`：只读投影按 `tasks.thread_id` 列出本线程未关 TaskRun 里“当前执行轮为 unknown 的非根代理”；
    写入口在同一事务里复核目标仍在这个范围内，再走与主链同一个 unknown→recovered CAS，事件带 `recovery_target=child_agent_run`、
    `thread_id`、`task_run_id`。
  - `/recover`（`gateway_parts/turn_recovery_control.py`）：主链阻塞时行为不变，查看时多一句“另有 N 个子代理待核对”；主链不阻塞时
    列出子代理编号、角色、接替情况和未确认操作；恰好一条时才处置，多条拒绝并列清单（沿用 `RUN_RECOVERY_REJECTED`，不新增错误码）。
  - 子代理恢复后：经 `SubAgentManager.taken_over_successor` 判定子代理记录 `status=TAKEN_OVER` 且有 `takeover_by` 时，用现有
    `settle_taken_over_run` 收成 cancelled；再用会话层 `conversation/task_run_closeout.settle_terminal_task_run`按任务走 D3 同一条 TaskRun 收口。没被接替的子代理留在 created（执行轮
    recovered），是否续跑由主代理决定；续跑时 `create_attempt` 会重新打开 TaskRun。
  - TUI 把 `/recover` 原文转给 Gateway，飞书走同一服务，两边入口一致；命令目录文案不再承诺“接着原任务继续”。
- **守住的边界**：只有用户显式输入处置才写库；不加任何自动处置或过期规则；静止规则与 TaskRun 树规则不变；范围只到本线程未关
  TaskRun，不跨线程，不按正文或最近任务猜目标；工具操作行照旧停在 EXECUTING，处置只记在恢复事件上（与主链 `/recover` 一致）。
- **C8 已实施（2026-10-02）**：`conversation/control_commands.py` 已支持 `/recover <处置> <编号>`；多条子代理可按查看清单逐条处置。
  写入口不使用查看快照选目标，而是在同一写事务里按编号复核 thread、未关闭 TaskRun 和 current unknown，因此消除了“唯一一条”换目标窗口；
  不带编号的“恰好一条”兼容行为不变。
- **C9 已实施（2026-10-02）**：新增仅完整可信 `local/main` 管理员可用的 `/recover owner` 查看、预览、确认入口，覆盖本 owner
  不挂任何会话线程的历史 unknown 根代理和子代理。确认码绑定本次完整目标集合，集合改变即失效；批量处置复用同一个
  unknown→recovered CAS，并给出目标/成功/跳过和原因码的结构化回执。09-30 生产库副本里的历史数量只是需求来源，
  本轮测试绝未读取或修改真实运行库。
  - 结构化接替提示（2026-10-02 已实施，开关默认关，见上方 C4 条目；原记录如下）（10-01 真实复测后 3a 裁定先观察）：被 SIGKILL 的子代理只回 BLOCKED，`blocked_reason` 是子代理自己写的字段，
    被杀时为空；宿主侧的事实是执行轮 unknown（executor_process_died）。复测 4 次模型都没在 `create_subagents` 里声明
    `replacement_for_run_ids`，“已被接替的来源收成 cancelled”只有合同单测覆盖。是否在唤醒回执里给父级结构化的接替建议是新设计项；
    先在以后的真实任务里观察模型是否自然声明接替，不为测试改提示。
- **分层边界修正**（2026-09-30，分支 `claude/38-recover-child-import-boundary`，基于 step16v `d9abcb5ec`）：首版网关直接导入
  `agent_core.runtime_mixin` 与 `subagents.models`，违反 `scripts/check_import_boundaries.py` 对 gateway_parts 的规则，step16v 因此
  没有上线。现在：
  - TaskRun 收口的判定（D3 规则）整体移到会话层 `conversation/task_run_closeout.py` 的 `settle_terminal_task_run`；
    `runtime_mixin._settle_terminal_conversation_task_run` 只做委托，网关直接调用会话层，两处共用一份实现。没有改成 agent 方法，
    因为 `SimpleAgentRuntimeMixin` 再加方法会进入 code-size 的 mixin high-risk 区。
  - 接替判定经网关已持有的 `owner_agent.subagents` 调用 `SubAgentManager.taken_over_successor`（实现在
    `subagents/manager._taken_over_successor`，记录读不到返回 None，只记 superseded_by 的不算接管）。
  - 边界规则和白名单都没改；`check_import_boundaries.py` 0 条。
- **验证**：见 TESTS.md 同名节与“/recover 子代理分支的分层边界修正”节。

## 能力申请裁决的确定拒绝不再记成“结果未知”（2026-09-30，分支 `claude/ae-resolve-refusal-code`，基于 main `a8c71f0e0`，已实现，已合入 main `7430a0000` 并经真实模型复核）

- **来源**：能力包真实模型验收 G03 缺陷 D2。父级 `resolve_capability_requests(decision=grant)` 遇到 write_roots 全部越界（`/etc/hosts`），回执 `ok=false` 但不带错误码。
- **原因**：`tool_operation_coordinator._operation_status_for_result` 的判定是：handler 已执行过的失败，只有显式声明 `effect_outcome=failed/not_started`，或错误码在执行前确定失败白名单里，才落 FAILED；其余一律 UNKNOWN。这个回执两样都没有，于是被包成 `TOOL_OPERATION_OUTCOME_UNKNOWN`／manual_review，操作行停在 UNKNOWN。
- **做法**：只改工具返回处，不改协调器判定，也不新造分类。
  - 裁决记录带登记表里的现有码：越界目录 `PATH_OUTSIDE_WORKSPACE`，工具或 Skill 超出父级 `MISSING_CAPABILITY`，裁决阶段异常 `TOOL_ERROR`。
  - 单条拒绝统一由 `_refusal` 生成（同时让 `_mark_grant` 变短）。
  - 新增 `_resolution_outcome`：失败时取第一条 error 的码，并声明 `effect_outcome=failed`。裁决跑完时，每条申请批了或没批都写在 payload 里，结果是确定的。
- **边界**：
  - 裁决之后的落账或唤醒抛异常时，仍按结果未知处理，不自动重做。
  - 部分批准、部分拒绝时，已批的照常落账；整体回执为 failed，模型按 payload 看每条结果。
  - `MISSING_CAPABILITY` 不在白名单里，只靠 `effect_outcome=failed` 才落 FAILED。回归测试专门覆盖这一点。
- **验证**：`test_resolve_capability_requests_tool.py` 新增：
  - 经原执行器和 LocalStore 操作账的两条回归：越界、工具超出父级，都落成 failed，没有 unknown_reason；
  - Skill 不可用、裁决异常各一条；
  - 原有三条拒绝用例补断言错误码和 effect_outcome。

  9 个变异全部被抓住。其中只去掉 effect_outcome 时，操作账复现为 `TOOL_OPERATION_OUTCOME_UNKNOWN`。

## 《模型管理使用说明》更新：子代理实况、逐模型思考档位实测与回执一致性（2026-09-30，文档，已完成）

- **内容**（补进 docs/guides/MODEL_GUIDE.md）：①派子代理后用 `list_agents` 可看子代理实际用的模型和智能程度（节点 `model`＝"名称（编号）"/「继承会话默认」/「未知」，`reasoning_effort`＝档位/「默认」）；②各模型思考档位实际能不能调的 2026-09-30 逐模型实测大白话表（ChatGPT 订阅能调无关闭档、DeepSeek 官方能调能关、opencode v4.1-flash 能关思考档位作用弱、opencode v4-flash 与 MiniMax-M2.7 不支持、MiniMax-M3 只能开/关，只写 REASONING_EFFORT.md 复测表有依据的）；③`/effort` 回执与实际发送一致（不支持关闭思考时如实说「本设置不改变请求」，服务商声明换算时写实际发送档位如「实际发送 high」）；④常见疑问新增「设了 max 回执说实际发送 xhigh/high？——服务商只声明到那一档」。
- **依据**：agent_py_agent/agent/agent_core/agent_tree/node_rendering.py（list_agents 节点投影）、docs/modules/subagent/ORCHESTRATION_TOOL_REFERENCE.md（节点字段说明）、docs/design/REASONING_EFFORT.md 第 2 节「2026-09-30 复测」表与第 5 节、agent_py_agent/agent/backends/reasoning_control.py（describe_config_reasoning_effect 回执）。
- **验证**：纯文档改动，未改代码；`scripts/check_doc_sync.py` 与 `git diff --check` 通过（详见 TESTS.md 同名节）。

## 没有会话任务的请求，代理树结束后 TaskRun 也关闭（2026-09-30，分支 `claude/38-taskrun-close-without-conversation-runtime`，基于 main `a8c71f0e0`，已上线 step17a（main de222698b，2026-10-02））

- **现象**（ae 真实模型验收 D3，bf4f740c3 与 10041de02 各复现）：请求 record 没有 `conversation_runtime`、`conversations/tasks/<请求>.json`
  不存在（只调 list_agents、管理员 `manage_models set_shared`、不调工具），请求 done、主 agent_run done，TaskRun 却永远
  `created`、`closed_at=0`。证据 `capability-real-bf4f/host-defect-candidates.json`。
- **原因**：TaskRun 的唯一收口边 `runtime_mixin._settle_terminal_conversation_task_run`（判定已于 O1 分层边界修正时移到
  `conversation/task_run_closeout.py`）要求会话任务关联已终态，`tasks.load`
  返回空时直接返回；发现层重放同样只认终态关联。从未升格成会话任务的请求没有关联，于是没有任何路径关闭它的 TaskRun。
- **修法**（权威写入点不变，仍是 `settle_task_run_if_agent_tree_terminal` 的树终态 CAS）：同一个收口边里，关联文件确实不存在时
  改由代理树决定，`operator=agent-runtime`、`reason=no_conversation_task`。只认真正的“不存在”：关联读坏、没有会话存储、任务身份为空、
  关联未终态都保持开放。之后若同一 run 再挂 attempt，`task_run.reopened` 照常重开。
- **不放宽**：unknown attempt 仍不算静止（树判定没改）。被 SIGKILL 的子代理 O1（写操作停在 EXECUTING、agent_run=unknown、TaskRun 不关、
  /recover 看不到待核对项）是另一个问题；第一步已单独处理，见上方“/recover 能看到并处置本会话子代理留下的未知执行轮”。
- **待定**：进程在主 run 收口与 TaskRun 关闭之间崩溃的窗口，发现层 `_reconcile_terminal_conversation_task_runs` 不补——它按文件内容
  收集关联状态，读坏的文件会被跳过，分不出“没有关联”和“关联读坏”，不能据此关闭。
  （2026-10-01 C12c 已实施：发现层按关联文件是否存在区分两者，见上方 C12c 条目。）
- **改动范围**：只改 `agent_core/runtime_mixin.py` 一个函数；没动 `conversation/runtime.py`（Codex 重构区）与
  `orchestration/tools/capability.py`（ae 修 D2）。
- **验证**：`test_task_run_close_without_conversation_task.py` 6 项；5 个变异全部被杀。见 TESTS.md 同名节。

## 能力包使用说明实操核对：补入口文档必须列入 files（2026-09-30，分支 `worker/ds1-guide-qa`，已修正，已合入 main `199c1933e`）

- **做法**：按 `docs/guides/CAPABILITY_PACK_GUIDE.md` 第五节在临时目录实操构建一个小能力包（CAPABILITY.md + 模板 + 示例脚本 + 声明 JSON），用说明书命令 `scripts/build_capability_package.py` 构建成功，并用 `inspect_plugin_package`/`read_plugin_member` 进程内读回核对成员、摘要与声明一致。
- **发现**：说明书字段表没说入口文档必须同时列入 `files`；实测 `capability.entry_document` 不在 `files` 时构建失败（错误为笼统的"插件包描述无效"）。已在说明书补注意说明。`settings_schema` 写 `{}` 实测可行。
- **状态**：仅改说明书与测试记录，未改产品代码；未安装、未运行 `/plugins`、未启动 Gateway。

## 子代理接替写专门审计事件 subagent_takeover_recorded（2026-09-30，分支 `worker/ds1-takeover-event`，已实现，已合入 main `6da387942`）

- **来源**：能力包验收 G03 发现接替已结束子代理时，追加式事件日志里只有普通保存（`subagent_run_saved`）或状态记录，没有专门的“接替”条目；接替事实只在权威 takeover_records 与 TAKEOVER.md 里。要求只读审计投影，不建第二份状态。
- **做法**：在接替落账唯一入口 `services/takeover/record.py::record_takeover_edge` 落盘核对通过、TAKEOVER.md 写完后，追加一条 `subagent_takeover_recorded` 事件（复用 `manager.log_local_record`，与 `subagent_run_saved` 同一写入通道）。payload 带 `source_run_id`（来源）、`successor_run_id`（接替者）、`disposition`（superseded 或 taken_over）、`record_id`、`created_at`。
- **边界**：它是审计投影，权威仍是 takeover_records / superseded_by / takeover_by；不改任何状态语义；事件写入失败与同通道其它事件一致（内部吞异常只记 warning），不影响落账主链；二次接替同一来源被预检拒绝时不产生新事件（不会走到落账入口）。
- **验证**：`test_subagent_done_supersede.py` 新增三用例（DONE→superseded、BLOCKED→taken_over 各一条事件且字段正确；二次接替被拒不新增事件）；连同 orchestration、local_store 相关共 63 passed。

## 被接替的子代理在运行账里补终态（2026-09-30，分支 `claude/38-agent-run-closeout-status`，基于 main `10041de02`，已实现，已合入 main `fffafb47a`）

- **现象**（G03 验收第二条观察）：子代理被宿主收口成 BLOCKED 后又被 `replacement_for_run_ids` 接替，canonical 转 TAKEN_OVER，
  但 runtime.db 的 agent_run 永远停在 created（attempt 已 done）。证据批次 `bg-subagent-paths-9f88` 的 runtime.db 副本：13 个 agent_run
  只有这一个停在 created，事件只有 `agent_run.started` 和 `agent_attempt.completed`，没有 `agent_run.completed`。
- **原因**：BLOCKED 收口走 `settle_agent_attempt`，按设计只结束执行片、保留 created 以便续跑；只有 DONE/FAILED/CANCELLED 的 runner 结果
  会调 `settle_agent_run`。接替落账（`record_takeover_edge`）只改 canonical 状态，从来没有入口给这条运行写终态。这不是有意设计：
  `runner_result_admission._runner_runtime_terminal_status` 已经把 TAKEN_OVER 映射成运行终态 cancelled，只是接替路径没有落这个账。
- **修法**：新增 `runtime_db/run_takeover.py`：`settle_taken_over_run` 读权威行，执行轮已静止（终态集去掉 unknown）才用 `settle_agent_run`
  写 cancelled，并把读到的 attempt 状态作为 CAS 条件；已终态、没有权威行、执行轮仍在运行/排队/未知都不写。
  `SubAgentBaseService.record_takeover` 在接替落账后、来源确实转成 TAKEN_OVER 时调用它的尽力版本，失败只记日志，不影响已落盘的接替。
  `agent_run.completed` 事件带 `runtime_source=subagent_takeover`、`runtime_reason=taken_over`、`takeover_by`。
- **不改**：已关闭来源被接替（只记 superseded）运行账不动；仍在运行的来源被接管时不在这里停执行轮（归取消入口，runner 结果会被准入拒绝），
  这条运行要等取消入口处理，记为待定（2026-10-01 C12d 真实链路核对：runner 心跳会自己停下，见上方 C12d 条目）；避开了 1 号会话正在改的 `takeover/record.py` 与 Codex 在重构的 `conversation/runtime.py`。
- **验证**：`test_subagent_takeover_runtime_closeout.py` 6 项；7 个变异全部被杀。见 TESTS.md 同名节。

## 工具参数无效改为有界纠正：长任务不再因一次坏参数整轮失败（2026-09-30，已上线 step17a（main de222698b，2026-10-02））

- **现象**（真实）：2 号 deepseek-v4-flash 工作会话做开发任务，写了一半测试时一次工具参数不是合法 JSON，474 秒的工作整轮以
  `MODEL_TOOL_ARGUMENTS_INVALID` 失败。此前设计是“参数无效就停止本轮”（为了让用户看到失败而不是停在半句话上）。
- **做法**：参照官方 Codex，把参数无效作为宿主纠正回给模型、同一轮续跑；连续 3 次仍无效才按原错误结束，形成可执行调用后清零。
  整组仍零执行、不修残缺参数、不读正文；断流、过滤等其它错误不变。计数器各加计数改用 `replace`，新字段不会被别的纠正清零。
  详见 [模型不完整响应的终态诊断 · 参数无效改为有界纠正](docs/design/MODEL_TERMINAL_DIAGNOSTICS.md)。
- **验证**：`test_native_truncated_write_recovery.py` 把原“参数无效不恢复”用例改为“有界纠正后保留原错误”（四种坏参数），新增连续计数清零
  用例；工具循环、协议、截断、终态等 855 项回归通过；7/7 变异被杀。
- **配置**：未新增开关。协议修复预算在 09-27 参数精简时已改为代码常量，本处沿用同一做法；它替代的是“整轮失败、用户重发”，不是额外功能。

## 《能力包使用说明》用户文档（2026-09-30，文档，已完成）

- **内容**：把散落在多份设计文档里的能力包能力整理成给用户看的中文大白话说明（每个功能带小例子）：能力包是什么（纯内容包，和 MCP/插件/Skill 的区别）、装/启用/停用/更新/回退/卸载（`/plugins` 各动作，安装默认停用、回退=显式指定旧版本包、卸载保留任务产物）、按任务发现与使用（推荐开关默认开、一次选择默认关、新任务才用新版本）、版本固定与派子代理授权（`allowed_skills=["capability:<包ID>"]`）、自制包（`scripts/build_capability_package.py` 用法与声明字段）、安全边界（只按声明读取、改动过的包拒绝、不执行包内代码、不含密钥）、常见疑问。
- **位置**：`docs/guides/CAPABILITY_PACK_GUIDE.md`（与 MODEL_GUIDE.md 同目录、同风格）；README.md「能力包（开发候选）」与 docs/README.md「入口」已加链接。
- **明确边界（2026-10-02 更新）**：TUI 与 IM 的 `/plugins` 和 `/plugins@<插件ID>` 已随 step17a 上线（main de222698b，2026-10-02），共用插件服务；管理动作仅管理员，含可执行程序的启用先预览再确认。真实 IM 收发仍待沙箱外验收。
- **依据**：docs/design/CAPABILITY_PACKS.md、PLUGIN_PACKAGES.md、agent/command_catalog.py（/plugins 动作）、scripts/build_capability_package.py、agent_py_agent/config/capability_config.yaml（推荐/选择开关与默认值）；验收过程记录（docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md）只用于确认「哪些已真实验证、哪些还没有」，未写入用户说明。
- **验证**：纯文档改动，未改代码；`scripts/check_doc_sync.py` 与 `git diff --check` 通过（详见 TESTS.md 同名节）。

## list_agents 显示子代理实际使用的模型与智能程度（2026-09-30，已上线 step17a（main de222698b，2026-10-02））

- **背景**：功能验收发现——派出去的子代理实际用了哪个模型、哪个智能程度，my-agent 的 list_agents 看不到（节点只有 goal_digest/status 等，无 model/effort）。宿主核实子代理线程 model_profile_id=536c11f9（deepseek-v4.1-flash）、reasoning_effort=low，但工具回执不展示。
- **做法**：权威来源是已物化子代理线程的 `ConversationThread.model_profile_id` / `reasoning_effort`（创建时从任务属性写入）；线程未物化或读取失败时回退 kernel 快照里冻结的 `host_model_profile.v1` / `host_reasoning_effort.v1`。节点新增 `model`（“名称（编号）”，default→“继承会话默认”，失败→“未知”）与 `reasoning_effort`（档位或“默认”）；任何读取失败显示“未知”且不让 list_agents 失败；输出绝不含密钥、地址或请求头。
- **改动**：`subagents/kernel.py`（SubagentKernelRun 冻结值回退源）、`agent_core/agent_tree/node_rendering.py`（投影与名称解析）、`agent_core/agent_tree/model_view.py`（白名单）；终态回执（completion_message）是纯文本、没有结构化字段区，故只做 list_agents，未改终态回执。
- **验证**：`test_agent_tree_model_effort.py` 5 项；相邻回归 test_agent_tree_model_view / three_layer_status / list_agents_scope_resolution / test_subagent_kernel / test_reasoning_effort 全部通过（详见 TESTS.md）。

## 持续 Goal 自动续跑连续无进展熔断（2026-09-30，已实现并通过门禁）

- **问题**：Goal 仍为 active 且没有任何结构化变化时，安全边界持续安排新的空模型片，持续消耗用量。
- **做法**：只看本片工具调用数、成功的状态变更工具数、wake 时保存的 Goal revision/状态与任务状态快照；不解析对话正文。
  连续次数写入 Goal metadata 并按 wake ID 去重；达到 `goal_continuation_idle_limit` 后沿现有 `paused` 状态停止续跑，记录
  `GOAL_CONTINUATION_NO_PROGRESS`，用户新消息或 `/goal resume` 清空计数。提示复用待送达 host notice，TUI/IM 按现有最终消息路径呈现。
- **配置**：`goal_continuation_idle_limit` 默认 3，0 表示不限；随包 YAML 注释同时成为参数中心说明。
- **验证**：`test_thread_goal_pauses_after_three_empty_continuation_slices` 复现修复前仍 active；定向续跑/恢复/fuse/参数登记/host notice
  测试通过。Ruff、文档同步、strict code-size（hard=0）与 `git diff --check` 均通过。
- **补充观察**：更广的 Gateway 控制测试中，PTY 资源生命周期参数化用例有 3 个失败（观察到的子进程退出码 71，或缺少确认终止回执）；
  单独运行目标相关的 `test_first_work_tool_does_not_resume_explicitly_paused_goal` 通过。该失败原因未确定，不计为熔断覆盖通过。
- **待复核**：这是隔离替身环境的自动化验证；真实 MiniMax/IM 部署链路与通知的真实接收尚未验证。

## 智能程度（/effort）逐模型真实审计（2026-09-30，分支 `claude/38-effort-receipt`，38 执行）

- **范围**：生产 local/main 模型目录里每个对话模型，产品后端真实请求，隔离目录副本（600、令牌不刷新、用完删）。详表见
  [智能程度设计](docs/design/REASONING_EFFORT.md) 第 2 节“2026-09-30 复测”。
- **结论**：没有任何字段被服务商拒绝。能真正调档的：ChatGPT 订阅（须档案声明 `reasoning_levels`，否则最高档只发 high），
  DeepSeek 官方（v4-pro 明显，v4-flash 在这道题上差距小）。只能开 / 关：DeepSeek Anthropic 兼容、opencode v4.1-flash（关思考生效）、
  MiniMax M3（用 budget）。不支持：opencode v4-flash、MiniMax M2.7。本地 qwen 与 step7 relay 不可达。
- **已改**：`/effort` 回执与 Responses 实际发送同一换算（`79b398484`）。已知表不改：同一中转 / 服务商下模型表现不同，按档案声明。
- **可经产品入口写档案的后续项（待定）**：
  - ChatGPT 订阅五个档案补 `reasoning_levels`（从订阅目录重新勾选即可刷新）；
  - opencode `deepseek-v4.1-flash` 声明 `reasoning_control: effort`（主要为了关思考生效）；
  - MiniMax `M3` 声明 `reasoning_control: budget`（只开 / 关）；
  - opencode `deepseek-v4-flash`、MiniMax `M2.7` 保持不声明（none）。

## WebSocket 断开加固：不再主动发心跳、断开带诊断，Compact 摘要遇瞬时错误原地重发（2026-09-30，已上线 step17a（main de222698b，2026-10-02））

- **来源**：38 的智能程度审计里，订阅 WebSocket 49 次请求有 2 次在第 23–24 秒"回复完成前断开"（握手已成功），都紧跟客户端
  第 20 秒的心跳 ping；错误里没有关闭码，也分不清断在哪个阶段。另查到 Compact 摘要走辅助调用，没有任何瞬时错误重试。
- **做法**：① 仿照官方 Codex 不主动发 ping（服务端 ping 照常自动回应），死连接靠首事件/空闲超时收口；② 断开错误文本附阶段、
  连接后秒数、关闭方与关闭码/原因；③ Compact 摘要（整段、分段、看图）请求复用主回合同一退避器原地重发瞬时错误，分段只重发出错的
  那一段。没有新增配置，退避间隔沿用 `provider_transient_auto_resume_delays_seconds`。
- **验证**：`test_responses_websocket.py`、`test_compact_request_budget.py` 新增用例；73 个 Compact 相关文件及错误/重试相关测试
  1558 项通过；7 个变异全部被抓住。详见 [订阅接口改走 WebSocket](docs/design/MODEL_OAUTH.md) 与
  [摘要请求的瞬时错误重发](docs/design/CONVERSATION_CONTEXT_DESIGN.md#摘要请求的瞬时错误重发2026-09-30)。
- **待观察**：去掉客户端 ping 后的断开率要靠上线后的真实请求统计确认；如果仍有断开，下一步参考 Codex 的会话级回退（重试用完后本会话改走 HTTP）。

## GPT 长任务回合内压缩失败：Responses 思考块被当成未知内容（2026-09-30，已上线 step17a（main de222698b，2026-10-02））

- **现象**（真实）：主会话切到 gpt-6-luna 后派一个开发任务，开头的会话压缩正常（47.3 万→7.5 万），做了 10 轮工具、1137 秒后整轮
  失败，`error_code=COMPACT_REQUEST_NON_TEXT`，没有任何产出。
- **根因**：Responses 模型的加密思考在 canonical 历史里是 `responses_reasoning` 块；“能否按文字计量/能否摘要”的判定
  （`backends/request_content.py`）只认 `thinking`/`redacted_thinking`，把它算成 unknown。于是回合内的工具 IR 压缩在第一道门
  `_prepare_native_compact_plan` 就因“容量未知”直接不做，上下文一路涨到窗口上限；被迫恢复时 `compact_request_recovery.select`
  用同一判定拒绝，报 `COMPACT_REQUEST_NON_TEXT`。所有 GPT（Responses）会话的长回合都会这样失败，与模型无关。
- **做法**：同一后端时把 `responses_reasoning` 与 `thinking` 同等看待（结构完整才算，判定复用发送回放的 `reasoning_item`）；跨模型仍
  不可移植。没有新增配置，也不改其它协议的行为。见 [Compact 媒体策略 · 结构化事实与判定](docs/design/COMPACT_MEDIA_POLICY.md)。
- **验证**：`test_request_content_capacity.py` 新增 5 项，`test_native_tool_ir_compact_and_orphan_sweep.py` 新增回合内压缩端到端用例
  （修复前失败、修复后通过）；相关 1274 项回归通过；6 个变异全部被抓住。上线后用主会话原任务重跑做真实验收。
- **已评估并实现**（38，分支 `claude/38-compact-segment-strip-ciphertext`，基于 step16t `10041de02`）：分段摘要来源剥掉思考密文。
  - 量级（生产结构化计量，不读正文）：目前只有主会话有 GPT 思考块，17 块共 60,712 字符密文，产品估算约 2 万 token，平均每块约 3.6K 字符、
    约 1,190 token；可读摘要（summary_text）全是空的。长 GPT 回合每个助手轮都带一块，100 轮就是十几万 token 的 base64 进摘要文字，
    摘要模型读不懂，只多出分段次数和费用。DeepSeek 的 thinking 块没有签名，也没有 redacted_thinking，本次不涉及。
  - 做法：`compact_message_source.summary_source_message` 只把 `responses_reasoning` 块的 `item.encrypted_content` 换成固定占位，保留块位置、
    model、id 与 summary_text；`_summarize_segments` 的两种来源工厂（列表与可重放来源 `CompactMessageSource.projected`）都经它。
    整请求按原协议发送的路径照旧带密文（同后端能用它续推理）。
  - 校验不变：两遍读取都走同一个确定投影，`CompactTextSource` 的两遍摘要一致与覆盖完整都按投影后的来源算；可读内容在两遍之间变化
    仍报 `COMPACT_SOURCE_CHANGED`，只有密文不同则视为同一来源（密文不进摘要）。
  - 验证：`test_compact_message_source.py` 新增 4 项（两种来源都剥密文且保留摘要与 id、原消息不改、整请求仍带密文、投影来源早退关闭上游、
    可读内容变化仍被发现）；6 个变异全部被杀。见 [会话上下文设计](docs/design/CONVERSATION_CONTEXT_DESIGN.md) 同名节。

## Responses 请求带会话级提示缓存键 prompt_cache_key（2026-09-30，分支 `worker/ds1-responses-cache-key`，基于 main `bf4f740c3`，已上线 step17a（main de222698b，2026-10-02））

- **用户要求**（集成负责人 3a）：OpenAI 官方 Codex 在每个 Responses 请求体里带 `prompt_cache_key`（值是会话编号），让服务商把同一会话路由到同一份提示缓存，命中更高、更快、更省额度；我们的 Responses 后端 `_generate` 没有带。
- **做法**：`provider_headers.py` 新增公开只读函数 `current_provider_session()`，返回当前线程绑定的稳定会话编号（owner+thread 的 sha256，来自 `provider_session_scope` / `provider_runtime_scope`），未绑定时返回空串；其它模块不直接读私有 `_SESSION`。
  `responses.py` 的 `_generate` 在 `validate_responses_input` 之后、发送之前写入 `payload["prompt_cache_key"] = session`（仅当绑定会话时），API Key 与 ChatGPT 订阅（`auth mode=chatgpt`）两条路径共用同一组包，都生效。绝不生成随机键，避免破坏缓存。
- **不改**：openai_chat、anthropic 后端行为不变；`session_header` 的既有逻辑不动。
- **验证**：新增 `test_responses_cache_key.py` 六条用例（scope 内带键、scope 外不带、同线程两次键相同、不同线程键不同、键不含 api_key/token 凭据、订阅模式带键且去 max_output_tokens）；连同 `test_responses_reasoning.py`、`test_responses_websocket.py` 共 27 passed。

## 《模型管理使用说明》用户文档（2026-09-30，文档，已完成）

- **内容**：把散落在多份设计文档里的模型能力整理成给用户看的中文大白话说明（每个功能带小例子）：`/model` 五项菜单新增（填地址和密钥后勾选、ChatGPT 订阅浏览器登录/设备码登录后勾选）、切换当前会话与设置新会话默认（含 IM 的 `/model`、`/model <编号>`、`/model default <编号>`）、`/effort` 智能程度档位及其在不同模型上的实际生效方式（effort/budget/none/auto、Responses 档位对应与 max→xhigh→high 回退）、删除模型（TUI 勾选删除与对话式删除）、管理员共享模型与指定其他用户初始模型、派子代理时选模型与 effort 的权限规则（管理员不限、普通用户只能选自己添加的模型）、`manage_models` 对话式管理。
- **位置**：`docs/guides/MODEL_GUIDE.md`（docs 下无现成使用说明目录，新建 `guides/`）；README.md「模型接入」与 docs/README.md「入口」已加链接。
- **依据**：`agent/command_catalog.py`（`/model`、`/effort` 声明与 IM 可用性）、`agent/capability/model_profile_tool.py`（`manage_models` 动作）、`agent/settings/model_profiles.py`（`resolve_child_model_profile` 权限、初始模型解析）、`agent/backends/reasoning_control.py`（档位换算），以及 docs/design 下 TUI_MODEL_PROFILES / MODEL_OAUTH / REASONING_EFFORT / SHARED_MODEL_CATALOG / SESSION_MODEL_SELECTION。
- **验证**：纯文档改动，未改代码；`scripts/check_doc_sync.py` 与 `git diff --check` 通过（详见 TESTS.md 同名节）。

## 上下文数字第二个来源：owner 级按分词身份的校准比值缓存（2026-09-30，分支 `claude/38-context-calibration-carry`，基于 main `bf4f740c3`，已上线 step17a（main de222698b，2026-10-02））

- **现象**（3a 用同一只读脚本取数）：09-30 11:07 新开的两个会话第一次调用都显示 44,852，实际 35,806 / 35,807，偏高 25.3%；
  第二次调用只差 +1.1% / +0.7%。
- **根因**：供应商校准只存在会话线程上（`provider_context_observation`），要求同线程、同表面指纹、同压缩代次。新会话、刚换过模型的线程、
  每次压缩提交之后、同进程里稳定表面变了（工具清单、技能卡、人格文件）的第一次调用都只能用原始估算。重启这一种已由 `bf4f740c3` 修掉。
- **修法**：新增 owner 级派生缓存 `O/data/context/calibration.json`（规范路径 `home_paths.owner_context_calibration_json`，
  唯一读写 `agent_core/model/context_calibration_carry.py`）。
  - 键是分词身份摘要（后端名、模型名、native/text 协议、不含凭据的连接身份），值只有“本地估算 / 供应商实际 / 观测时间”三个数；最多 32 条。
  - 每次成功记录线程观测时顺手更新；本地估算低于 4096 的小请求不更新（固定开销会把比值带偏）。
  - 取观测的唯一入口 `_provider_context_observation`：本轮与线程的精确观测优先，都对不上时才用缓存，作用域 `owner_ratio`，
    只按比例折算、不低于 50%，不做跨会话的增量口径。
  - 预检、状态条快照与压缩候选门（`frozen_compact_request_calibration` → `compact_calibration`）都经同一入口，用同一个数。
  - 稳定表面指纹的载荷键与取值不变（与 main 逐字相同的摘要），已存的线程观测继续有效。
- **范围变化**：同进程稳定表面变化那一次（上一版台账记的“可选做法 B”）也随之按比值折算，不再显示原始估算。
- **不变**：换到从没用过的模型、换地址或鉴权方式，分词身份不同，仍用原始估算（如实）。
- **开关**（3a 裁定按 AGENTS.md 必须加）：`memory_context_calibration_carry_enabled`，默认开；关掉时既不读也不写缓存文件，回到只用本轮/线程观测。
- **验证**：`test_context_calibration_carry.py` 8 个合同用例；10 个变异全部被杀；详见 TESTS.md 同名节与
  [会话上下文设计](docs/design/CONVERSATION_CONTEXT_DESIGN.md) 同日一节。

## GPT 长回复断线根因与 WebSocket 传输、Responses 智能程度、子代理选模权限、删除模型入口（2026-09-30，已上线 step17a（main de222698b，2026-10-02））

- **用户要求**（长任务 goal 第 1、2 项）：找到断线原因并修好，用 gpt-6-luna 实测，"不能固定模型"；检修模型配置和 effort，保证能调的
  都真的可用；派子代理可选模型和智能程度（管理员不限，普通用户只能用自己添加的模型）；补删除模型入口，这些操作 my-agent 自己也能做。
- **断线根因**：ChatGPT 订阅接口的普通 SSE 长输出会在服务端中途停止发送、约 60 秒后被关（4/4 复现），同代理下其它服务商正常；
  服务商目录声明这些模型 `prefer_websockets`，官方命令行默认也走 WebSocket。**做法**：订阅登录的 Responses 改走 WebSocket
  （`backends/responses_websocket.py`），事件交原解析；真实复验长输出 8374 字、长输入摘要 10562 字完整。依赖 `websockets` 显式声明
  （原随 lark-oapi 安装；自写 RFC 6455 客户端风险更大，不采用）。详见 [模型账号登录 · 订阅接口改走 WebSocket](docs/design/MODEL_OAUTH.md)。
- **Responses 智能程度**：此前 Responses 协议一律"不支持调节"，GPT 的 /effort 等于没生效。现在按 `reasoning.effort` 发送，
  档位按模型档案新字段 `reasoning_levels`（服务商声明，订阅模型添加时从目录自动带上；配置 `model_reasoning_levels`）对应；
  未声明只发通用 low/medium/high。真实核对 gpt-6-luna：low 推理 token 0、high 24、max 59。见 [智能程度](docs/design/REASONING_EFFORT.md) 第 3 节。
  已添加的同名模型再次添加时只刷新思考档位，my-agent 也能用 `add_models` 刷新。
- **子代理选模权限**：普通用户点名子代理模型只认自己添加的模型（共享引用和按名匹配共享都拒绝，提示省略 model 继承当前会话）；
  管理员不变。`effort` 原本就能按批次/逐项指定，现在对 GPT 子代理也真正生效。
- **my-agent 自己可操作**：`manage_models` 新增 `add_models`、`set_shared`、`set_initial`，`discover` 可带未保存连接。
- **删除模型**：「管理已有模型」最后一行"删除模型（勾选一个或多个）"，一次确认、逐个删除、失败说明原因。
- **验证**：`test_responses_websocket.py`、`test_responses_reasoning.py`、`test_model_profile_tool.py`、`test_tui_manage_models.py` 与更新后的
  共享/决策子代理用例；真实 gpt-6-luna 实验记录在 3a scratchpad `diag-1031/`（e1–e4 SSE 断线、c1 对照、w1–w2 与 f1–f2 WebSocket）。

## TUI 上下文数字忽高忽低：持久校准指纹改成跨进程稳定（2026-09-30，分支 `claude/38-context-usage-flicker`，基于 `c80c8b5c2`，已上线 step17a（main de222698b，2026-10-02））

- **现象**：用户长期会话（local/main `thread-de866e7291f846db`）的状态条 Context 一会儿高一会儿低。
- **数据**（只读结构化字段，证据 `~/.my-agent/decision-evidence/context-number-flicker-20260930/`）：
  - 同一请求内的后续调用 52 次，显示值与供应商实际输入的中位偏差 +0.9%；
  - 每个请求的第一次调用 8 次里有 4 次偏高 25–29%（例如显示 462,551、实际 358,330），下一次调用又落回校准值；
  - 这 4 次都紧跟一次 Gateway 部署重启（01:24、03:01–05:22、08:19）或一次换模型档案（07:19）；同进程内的请求首调都准。
  - 只调一次模型的请求结束后，空闲时状态条一直停在这个偏高值，直到下一回合第二次调用。
- **根因**：线程上保存的供应商校准观测（`provider_context_observation`）按“稳定请求表面指纹”取用，指纹里的连接部分用了
  `decision_policy.connection_revision`。那是按进程随机盐的 HMAC，本意只做进程内比较；持久化以后，每次重启都对不上，
  第一次调用只能显示本地原始估算（这个线程上偏高约 28%）。
- **修法**：指纹改用跨进程稳定、不含凭据的分词身份：模型档案、后端与配置里的地址、鉴权方式（`auth_ref` 只取 `mode`）、
  模型名、协议，加上原有的系统提示、稳定提示和工具。密钥、请求头、会话头不影响分词，不再进指纹；
  密钥轮换不再清掉校准（原用例里“换密钥要回到原始估算”的断言按此改掉）。预检、压缩候选门和状态条共用这一个指纹，口径仍然一致。
  旧观测的指纹对不上新算法，上线后第一次调用照旧显示一次原始估算，之后重启不再丢。
- **不改**：换模型（新分词，没有观测时用原始估算是如实的）；换窗口导致的百分比跳变（例如切到 272K 窗口的模型）。
- **残留（待定）**：同一进程内稳定表面变了（工具清单、技能卡、人格文件等），仍会显示一次原始估算；本线程数据里 60 次调用只有 1 次。
  可选做法：同一分词身份下按上次比例折算（下限 50%，压缩候选门同步）。它会让这种情况下的压缩判定不再偏保守，建议先看上线后的数据再定。
- **验证**：`test_runtime_context_pressure.py` 新增重启与轮换两个用例；8 个变异全部被杀；详见 TESTS.md 同名节与
  [会话上下文设计](docs/design/CONVERSATION_CONTEXT_DESIGN.md) 同日一节。

## ChatGPT 订阅模型报"工具能力检查未通过"（2026-09-30，热修，已实现）

- **事实**：用户在 step16q 勾选 GPT 模型后一发消息就报 `TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE`。隔离复现：模型其实调用了探针工具，
  但订阅接口的 `response.completed.output` 是 `[]`，条目只在 `response.output_item.done` 里；解析只读终态 output，所以工具和正文都丢了。
- **做法**：终态 output 为空时改用流里逐条的完整条目（协议层通用处理，不按地址分支）；终态有 output 仍以它为准。
  详见 [模型账号登录 · 订阅接口的流式输出](docs/design/MODEL_OAUTH.md)。
- **验证**：`test_responses_backend.py` 三个新用例（终态无 output 取流内条目、终态有 output 不被覆盖、流式能力探针通过）；
  真实订阅账号 gpt-6.1-sol / gpt-5.6-sol 能力检查通过、短问候有正文（隔离目录副本，不刷新令牌）。

## /model 菜单改成五项、其他用户的初始模型、补全高亮（2026-09-30，分支 `claude/3a-chatgpt-browser-login`，已上线 step17a（main de222698b，2026-10-02））

- **用户反馈**：菜单九项、每项下面又一堆，容易绕晕；OpenCode 这种要加头的服务商在「新增模型」里加就行；希望管理员能给其他
  用户指定初始模型；`/model` 打全后补全高亮会消失。用户确认的方案是顶层五项。
- **五项**：新增模型（OpenAI Chat / Anthropic / OpenAI Responses / 登录账号 / Jev 决策，填地址和密钥 → 拉列表 → 勾选一次保存，
  请求头与会话头在「高级」，OpenCode Go 一键模板）、选择模型（对话 / 决策）、管理已有模型（连接和账号一个列表）、连接测试、
  默认模型与共享（我的默认 / 共享给其他用户 / 其他用户的初始模型）。模型表单只留名称、上下文、启用，其余进「高级」。
  详见 [/model 设计 · 菜单结构](docs/design/TUI_MODEL_PROFILES.md)。
- **新操作**：`add_models`（按连接或已有服务商一次加多个模型，锁内整批落盘，四项连接字段相同就复用服务商，同名同接口跳过）、
  `discover` 可带未保存的 `connection`（不落盘，报错按这个连接的密钥脱敏）、`set_initial`。订阅账号勾选也改走 `add_models`。
- **其他用户的初始模型**：共享目录里可选的 `initial_profile`；普通用户选择引用为 `default` 时解析成它，管理员自己的 `default`
  仍是部署配置，用户自己选过的不覆盖；设置时同次开放共享，撤销共享同次清除，原模型删除或目录损坏明确报错、不换模型。
  列表的「默认」行带结构化 `default_source=admin_initial`，TUI 与 IM 都标出来。详见 [共享模型目录](docs/design/SHARED_MODEL_CATALOG.md)。
- **补全高亮**：prompt_toolkit 会丢掉"唯一且接受后文本不变"的候选，完整命令名的候选文本改为自带结尾空格（接受后结果不变）。
- **取舍**：连接测试不再列「默认」行（部署配置没有已保存连接可测，原来选了也只会报错）；决策设置里去掉重复的「管理/新增服务商」，
  保留「主动测试决策连接」。
- **验证**：见 TESTS.md「`/model` 五项菜单」与「斜杠补全高亮」条目；29 个变异全部被杀；真实 OpenCode Go 目录读取通过
  （30 个模型，不含上下文，结果不带密钥、不落盘）。上线后待用户真实 TUI 验收：新增 OpenCode/DeepSeek 连接并勾选、ChatGPT 账号勾选、
  管理员指定初始模型后飞书普通用户直接可用。

## /model 弹窗焦点守卫：鼠标点到弹窗外不再卡死 Tab/Esc（2026-09-30，热修，已实现）

- **事实**：step16o 上线后用户在「登录认证 → 选择模型」的勾选框里按 Tab 跳不到「添加」、Esc 也关不掉，模型一个没存上。
  复现：菜单期间 TUI 自己的按键整体停用（`_my_agent_model_menu_active`），Tab/Esc 只靠弹窗自己的绑定；TUI 默认开鼠标捕获，
  鼠标点到弹窗外（如聊天输入框）会把焦点移出弹窗，两边都收不到按键。所有 `/model` 弹窗都有这个问题。
- **做法**：`tui_model_menu._dialog` 挂 `after_render` 守卫，每次绘制后若最上层仍是本弹窗而焦点在外，就拉回弹窗；
  等待提示浮层在最上层时不动它。弹窗结束时摘掉守卫并照旧恢复原焦点。
- **验证**：`test_tui_pick_models_keys.py`（勾选添加后 Esc 关菜单、聊天框 Tab 可用、长表单 Tab/Esc、焦点被点到外面后拉回并能
  Tab 到「添加」保存、回到主菜单再点外面 Esc 仍能关），去掉守卫后该用例失败。

## ChatGPT 订阅浏览器登录：像官方命令行一样直接跳转（2026-09-30，分支 `claude/3a-chatgpt-browser-login`，step16n 上线，真实账号登录已通过）

- **问题**：TUI 的订阅登录只有设备码方式，网址和验证码显示在只读框里，用户点不开也复制不了，无法完成登录。
- **做法**：照官方命令行（Codex CLI）的授权码 + PKCE(S256) 流程。TUI 在登录期间于 `127.0.0.1:1455`（占用则 `1457`，
  两个都是授权服务器登记过的回调端口）临时监听，自动用默认浏览器打开官方授权页；用户在网页登录并同意后，浏览器跳回本机，
  TUI 只把授权码与 state 交给 Gateway（新操作 `auth_browser_start` / `auth_browser_complete`），由 Gateway 用私存的校验码兑换令牌，
  沿用原 pending 槽位、`_commit` 的配置绑定 + 请求编号 CAS、令牌字段与刷新，不新增第二份认证状态。
- **边界**：只支持 ChatGPT 订阅服务商；回调地址逐字比对登记值；state 常量时间比较，不符不清等待状态（伪造回调打断不了真实登录）；
  等待 10 分钟过期；授权码只兑换一次、不重试；授权地址不带官方命令行的来源标识；本机监听不写访问日志（请求行含授权码）。
  SSH 会话、Linux 无图形界面或两个端口都被占时退回设备码（设备码页也改为自动打开确认页）。IM 没有登录入口，不受影响。
- **验证**：`test_model_oauth_browser.py`、`test_tui_browser_login.py` 与原认证测试；15 个变异全部被杀。09-30 实测：授权页对非浏览器
  客户端是 Cloudflare 浏览器挑战（`cf-mitigated: challenge`），无法用脚本预验参数；换令牌接口可达（无效码回 401）。
- **真实验收**：09-30 用户在 step16n 的 TUI 里用真实账号浏览器登录成功（令牌与账号编号已保存）。
- **登录后勾选模型（同日跟进）**：用户要求像 OpenClaw 那样登录回来直接勾选模型、可多选，嫌手填表单选项太多。订阅账号的
  discover 改读订阅接口自己的 `/models` 目录（必须带 `client_version`，声明 `1.0.0` 只决定列出哪些模型，实测 1.0.0 列全部 9 个），
  只列可见且容量合法的条目；TUI 登录成功（按结构化 connected）后直接弹出勾选框，只列未添加的，勾中的逐个 `save_model`
  （接口 openai_responses、容量用目录值、其它默认），不预选、不自动切换；账号菜单「选择模型」同一勾选框。
  测试 `test_tui_subscription_models.py` 与目录用例，12 个变异全部被杀。勾选添加与短调用待下一版上线后用户验收。详见
  [模型账号登录](docs/design/MODEL_OAUTH.md)。

## 唤醒毒丸第 3 步 C6：优雅停机标记、死进程回合收尾、结案顺序收窄（2026-09-30，step16m，3a 接手，已实现）

- **优雅停机**：后台 supervisor 停机时，先给本进程在途的唤醒尝试打停机标记，再关执行池。写失败逐条吞掉，停机不会因此失败。部署重启后，在途唤醒记不计数的 `attempt:gateway_stopped`，不再每次部署都记一次 `attempt:abandoned`。
- **死进程回合收尾**：进程在一片执行中途死掉时，下一次 preflight（或与它竞态的 begin）从尝试账的在途记录读出那一片的回合号，按后台片异常结束的同一规则收尾：
  - 那一片认领过、还没消费的会话消息退回，下一回合照常投递；
  - steer 拒绝，已提交的不动；
  - 派活正文只在任务没被取消时退回；
  - 这次释放不计次，反复死亡由毒丸按 `attempt:abandoned` 计数结案兜底；
  - 旧账没有回合号时不收尾。
- **结案顺序**：写结案记录 → 删 pending → 移坏账 → 删尝试账。崩溃最多留下孤儿坏账，不会出现"pending 还在、账已不在"而从零重新计数（9a 复审建议）。
- 详见 [docs/design/WAKE_POISON_PILL.md](docs/design/WAKE_POISON_PILL.md) 第 3 步清单与"合并前要补的点"第 2 条。

## 出站协议合同：坏历史不再让请求永远 400（2026-09-30，分支 `claude/3a-wire-contract`，已实现）

- **问题**：历史会原样带进之后每次请求，一条服务端不接受的消息就让线程所有请求永远 400（09-30 热修只堵了 Chat 的一种形状）。
- **做法**：新增 `backends/wire_contract.py`，三个后端组包入口（发送与只读预览同一路径）先修整规范原生历史的副本——空白正文块、
  无文字无签名的思考块、删空的消息不发；工具结果移到紧跟调用的 user 消息开头，缺的补结构化"结果未知"回执，孤儿与重复结果删除——
  再按协议校验最终请求体，违规抛 `PROVIDER_REQUEST_SHAPE_INVALID`（请求拒绝子类，已登记错误合同）且不发送。
  Responses 另去掉助手轮末尾没有后继的 reasoning 项。只读结构化字段，不按正文分类，不按模型名或地址猜供应商。
- **取舍**：只有思考的 assistant 仍按协议区分——Chat、Responses 不发，Messages 照原设计回放（MiniMax 实测接受，省去续跑重做推理）。
  连续同 role 消息不合并、无签名思考不删、后台车道冷却不改（反复失败由毒丸 step16m 收口），理由与边界见
  [docs/design/PROVIDER_WIRE_CONTRACT.md](docs/design/PROVIDER_WIRE_CONTRACT.md)。
- **证据**：MiniMax-M2.7/M3 官网实测，孤儿结果两者都 400、调用与结果之间夹 user 消息 M3 400；产品组包对 6 段坏历史生成的
  24 个请求，修整前 7 个 400（M2.7 2 个，M3 5 个），修整后全部 200（`~/.my-agent/decision-evidence/wire-contract-2026-09-30/`）。
- **待定（9a 复审建议，不阻断）**：主链路上还有第二套出站孤儿清扫（`message_adapter.strip_orphaned_tool_blocks`，由
  `tool_ir_history.project_native_provider_messages` 与 `loop_support` 调用），它按全局 id 集合配对且 `discard("")`。空 id 的真实结果
  在上游就被当孤儿删掉，出口修整只能再补一个"结果未知"。现有服务商都会给调用 id，缺 id 或重复 id 的调用在 `tool_protocol_adapter` 当场判为协议违规，不执行，也不会回放进之后的请求，线上没有影响。2026-09-30 扫描生产全部 849 个会话（23,678 次工具调用）：空 id 调用与结果都是 0，两道清扫丢掉的真实结果都是 0（`history-scan-20260930.txt`）。
  - 可选做法一：让旧清扫改成与出站合同同口径（不丢空 id、按次数配对）；
  - 可选做法二：按"一个概念一个权威位置"，工具循环不再做这层清扫，只保留 wire_contract。压缩摘要的流式清扫是为了省内存，要单独评估。
- **复审跟进（9a，同日，进 step16l）**：
  - 修整改为按出现次数配对，空 id 和同条消息内的重复 id 都算调用。原来空 id 的真实结果会被删掉，Chat 还会在本地永久拦截这个线程，相对改动前是回归。
  - 运行错误报告单列形状错误，视觉探针不把它缓存成"不支持图片"。
  - Chat 动态尾部不再把带图消息的内容数组转成字符串（原有 bug）。
  - P3（调用与结果之间夹 assistant 时，真实结果被当孤儿删掉）和 P5（空白分隔块删掉后正文粘连）记为已知边界，不改。
- **DeepSeek 实测（同日，用户放开官方 DeepSeek 与 opencode Go 测试）**：DeepSeek 官方两种接口都严格执行配对、相邻和
  "结果放在同一条消息里"。同一组坏历史用 `deepseek-v4-flash` 发出：修整前 Anthropic 5 段、Chat 4 段 400，修整后全部 200。
  opencode Go 通道前后都接受。
- **Responses 实测（同日）**：opencode Go 的 `/responses` 用同一把 key 调 `gpt-5.6-luna`，产品 Responses 后端真实发送 7 段坏历史：
  修整前 2 段 400（孤儿结果、调用缺结果），修整后全部 200。ChatGPT 订阅登录那条路仍未真实验收（见 MODEL_OAUTH）。

## 一条空 assistant 让线程所有请求 400：Chat Completions 回放不再发出无正文无工具调用的消息（2026-09-30，热修，生产事故记录）

- **事实**：09-30 00:49 my-agent-4 用配置工具去掉了压缩触发线上限并重启 Gateway；重启后第一个后台回合（`gateway_restart_completed`
  唤醒）模型只返回思考，转换成 Chat Completions 后是一条既无正文也无工具调用的 assistant，DeepSeek 400。它留在历史里，
  之后用户的每条消息和唤醒每 30 秒一次的重试都 400，每次失败再追加一条空记录。
- **处置**：集成方用产品 `wakes.mark_handled` 结案了那条唤醒，止住后台重试；热修改 `openai_chat._openai_assistant_messages`，
  不回放这类消息（历史本身不改写），线程恢复可用。
- **取舍**（集成方代用户裁定）：原设计（09-19「仅思考响应与续跑历史」）让续跑请求带上仅思考轮的 `reasoning_content`；
  这个形状不符合 Chat Completions 规范，DeepSeek 官网实拒。改为仅思考轮只留在原生历史、不进 Chat Completions 请求，
  续跑时模型重新思考，由既有两次空正文预算兜底；Messages 协议不变。原用例的对应断言随之改为新合同。
- **防再犯**：同类「一条坏历史让请求永远被拒」的重试循环，由毒丸第 3 步接线（同因 5 次结案）兜底，在 step16m；
  Anthropic 协议的只有思考的 assistant：MiniMax-M2.7/M3 实测接受，保持回放。整类问题的出口修整与校验见上一条「出站协议合同」。
- **未改**：压缩触发线上限被模型重置的事实保留（配置改动账本 `settings-changes.jsonl` 有记录），是否恢复由用户决定。

## 旧版任务状态文件：355 棵 `TASK_STATE_INVALID` 保留原样（2026-09-29，分支 `claude/9a-legacy-task-state-ledger`，基于 step16l `5ec2db2e0`，只读诊断，待定、未落地）

- **来源**：local/main 的生产首跑积压（09-29 21:39:50）隔离了 355 棵子树，错误码全是 `MEMORY_RETENTION_TASK_STATE_INVALID`。
  - 它们是旧版 `O/tasks/<日期>/<任务>/work/state.json`（文件修改时间 05-31～06-05），文件能解析、有 `task_id`，但全部没有 `status`。
  - 原因是那时这个文件只记「运行身份 + 预留」，键集合分四组：257、67、28、3 个。
  - 现行写入方建文件时就写 status，这批不会再增长。
- **为什么没有终态**：
  - runtime.db 里没有这些 task_id；
  - 任务自己的 timeline 里，351 棵没有任何状态事件；
  - global_index 是投影，不能当权威。
- **裁定**（3a，09-29）：
  - 数据保留原样。retention 本来就整棵保护状态未知的任务，不会误删。
  - 不自动迁移。状态不隐式兼容，也不拿投影或 timeline 去推终态。
  - 不在 retention 里为这批目录开特例，不按目录日期、文件名或键集合放行。
  - 要清理，只能做一次性显式迁移：用管理员命令先 plan 再 apply，每棵写一条结构化迁移记录。做不做由用户拍板。
- **相关**：维护状态把路径级错误记成 `policy_unavailable` 的问题 9b 在修，和这批数据无关。
- 详见 [STORAGE_RETENTION.md 第 9 节](docs/design/STORAGE_RETENTION.md)；证据在 `~/.my-agent/decision-evidence/task-state-invalid-20260929/`。

## 插件子进程测试会往源码树写 __pycache__（2026-09-29，分支 `claude/9a-magicmock-guard`，待定）

- **来源**：约 85 条插件测试（插件调用、停用、启用、发布、卸载、沙箱、MCP 传输、任意语言插件等）会起真实的插件宿主或插件环境准备子进程。
  子进程从源码树导入 `plugin_entry`、`plugin_installation` 等产品模块，每条测试写出 8 到 18 个 `__pycache__`。
  这些子进程由产品代码起，环境由产品的安全清理构造：
  - `tooling/mcp_client.build_safe_env` 只放行 PATH/HOME 等白名单和 `XDG_*`；
  - `plugin_environment_process.preparation_environment` 有意剥掉全部 `PYTHON*`/`PIP_`/`LD_*` 前缀，防止注入。
  所以父进程的 `PYTHONDONTWRITEBYTECODE` 传不进去，conftest 或测试辅助函数也改不到。
  - 定位方法：scratchpad 探针插件串行跑，每条测试前后看产品目录的哨兵 `__pycache__`。
  - 同一轮定位还找出 `test_compileall_succeeds` 一次写 97 个目录，已在同分支 `e833b7148` 修掉。
- **影响**：git 检出里被 `.gitignore` 挡住，看不见也不会提交。只有导出目录（没有 `.git`）里，
  如果运行产物检查 `test_runtime_artifacts_are_not_present_in_tracked_files` 排在这些插件测试之后，才会被 `__pycache__` 绊倒。
- **可选方案 A（未采用）**：`build_safe_env` 白名单加 `PYTHONDONTWRITEBYTECODE`，`preparation_environment` 对这个精确名字豁免剥离。
  这个变量不是机密，也注入不了代码；父进程没设就不传，生产行为不变。
  不采用的理由：为测试卫生去改两处安全清理，收益只是导出目录里的测试顺序问题，不值得。
- **已知负载偶发**：12 片全量并发下出现过 3 条时序断言失败，单独连跑都能通过；step16k 的两次全量里没出现过：
  - `test_tools/test_shell_background.py` 的两条后台立即成功/失败用例（断言 `'started' == 'exited'`）；
  - `test_plugin_invocation.py::test_duplicate_and_disable_during_approval_cannot_execute_old_or_new_activation`。
  以后在全量里单独出现这 3 条，先单跑确认，不当作回归。
- **状态**：待定，不改产品；导出目录流水线如果要彻底干净，可以把运行产物检查排在最前，或者跑前清掉 `__pycache__`。

## 模型每周额度用完：判定、用量与后台轮询成本（2026-09-29，生产事故记录；前三项在做，后两项未落地）

- **事实**：
  - 09-29 04:35，local/main 主模型所走套餐的每周额度用完。服务商 429 错误体带结构化字段 `metadata.limitName="weekly"`。
  - 现有判定 `_provider_error_indicates_quota_exhausted` 只认已知码表，这个错误体不命中，落到瞬时类 `ProviderUsageLimitError`：回合内重试约 19 分钟，后台再按供应退避反复重试。
  - 用量（model_usage 账本 7 天）：四个长期后台线程输入 35.3 亿 token，缓存命中 96–98%；09-28 一天占整周输入的 59%。
  - 压缩一直没触发：模型档案窗口写成 100 万，自动压缩要到 90% 才触发，线程在 30–80 万之间跑了好几个小时。
- **方向与状态**：
  1. 额度用完改按结构化事实判定（限额窗口字段、Retry-After），未知值保持现有的瞬时默认。dsh-9a 在做。
  2. 压缩触发加绝对 token 上限 `memory_compact_auto_trigger_max_tokens`，0 表示不封顶，默认行为不变。dsh-be 在做，生产拟设约 30 万。
  3. 环境级故障按车道暂停（毒丸第 3 步 C5），模型指纹变化或到探测时刻再放行。dsh-9b 在做；额度用完要不要并入同一套暂停机制，等 C5 的核对结论。
  4. 未落地：定时 job 加结构化前置条件「输入没变就不调模型」。例如按被监视路径的大小、mtime 或摘要判断，没变就跳过本次模型调用，不再每 5 分钟带整段上下文轮询一次。这要动调度器合同，需单独设计，默认关闭。现有机制的调查见下一节。
  5. 未落地：同一 owner 的多个后台线程同时撞 429 时整体冻结，按服务商给的重置时刻或探测退避统一恢复，而不是每个 run 各自重试。
- **运维**：
  - 04:22 用产品 `SchedulerRepository.pause_job` 暂停了 4 个看板监控 job，恢复用 `resume_job`。
  - 恢复顺序：压缩上限上线 → 手动压缩 4 个线程 → 错开唤醒、逐个恢复。
  - 要不要换模型、升级套餐，由用户决定。

## 定时任务「输入没变就跳过模型调用」：现有机制调查（2026-09-29，只读调查，待定、未落地）

- **来源**：上一节第 4 项。4 个看板监控 job 每 5 分钟唤醒一次，每次都带整段上下文调模型。
  7 天 model_usage 账本：4 个长期后台线程输入 35.3 亿 token，缓存命中 96–98%，09-28 一天占整周的 59%。
  账本不区分定时唤醒、Goal 续跑、子代理这几种来源，所以单轮定时执行的成本没算。
- **现状**（step16k `d77b3ea4a`）：
  - `schedule` 工具和 `SchedulerJobCreateRequest` 的字段只有这些：名称、prompt、`schedule_kind`（at/every/cron 及其参数）、`misfire_grace_seconds`、`skill_ids`。
    没有前置条件字段，也没有「被监视的输入」字段。
  - 调度器、进度策略和唤醒发布都不做变化检测。那里出现的指纹只是账本文件的完整性校验。
  - 可插入点：`SchedulerService.enqueue_ready_runs` 在发唤醒之前已经有一条结构化分支：`_skill_reference_error` 不通过时，
    `_fail_without_execution` 直接领取并结算这一轮，不发唤醒，也不调模型。前置条件的判定可以放在同一个位置。
  - 结算状态现成：定时 run 的终态里已有 `skipped`。错过补执行窗口时，`_terminal_misfire_run` 记的就是 `skipped` 加 `SCHEDULER_MISFIRE_GRACE_EXPIRED`。
    「输入没变」的跳过可以复用这个状态，另配一个自己的结构化码。
  - 可见面：用户看定时历史只能让模型调 `schedule` 工具的 history 或 status 动作；TUI 和飞书都没有单独的定时历史命令。
    宿主提示（`queue_host_notice`）目前只在定时 run 结算成受阻时用。
- **设计约束**（3a 09-29 给的）：
  - 通用，不按看板这类具体任务写。
  - 只看结构化前置条件，比如被监视路径的大小、mtime 或内容摘要；不解析 prompt。
  - 按 job 显式开启，默认关闭。
  - 每次跳过都留结构化记录，TUI 和飞书都能看到。
- **状态**：待定、未落地。09-29 用户决定模型不再续费、4 个 my-agent 不恢复，这里不出实现方案。以后恢复时，按上面的插入点和约束单独设计。

## 压缩触发线的绝对上限 `memory_compact_auto_trigger_max_tokens`（2026-09-29，分支 `claude/be-compact-trigger-cap`，基于 `89af6b07a`，38 已复审，已拣进 step16k `7f4ccf09a`；跟进在分支 `claude/be-compact-cap-followup`）

- **来源**：
  - 03:44 起生产 4 个 my-agent 撞 deepseek-v4.1-flash 的 429。38 查清压缩本身正常，问题在阈值。
  - 模型档案把窗口填成 1,000,000，触发线默认 90% 就是 90 万。长期后台线程要在 30 万–80 万之间跑几个小时才压，四个线程并发顶住了服务商的每分钟 token 上限。
  - `memory_compact_auto_trigger_percent` 钳在 50–100，1M 窗口下最低只到 50 万。
  - 证据：`~/.my-agent/decision-evidence/compact-429-20260929/`。
- **做法（38 的方案 A）**：
  - 新配置 `memory_compact_auto_trigger_max_tokens`，0（默认）表示不封顶，现有行为不变。（2026-10-01 起默认改为 300000，见顶部同名条目。）
  - 大于 0 时，`runtime_compact_policy` 把触发线取成 min(窗口 × 百分比, 上限)，近期尾部与 recovery 都从封顶后的触发线推出。
  - 上限严格小于窗口 × 百分比才算封顶（`trigger_capped`）。正好相等时触发线与不封顶一样，finalization 仍按百分比判定，`/context` 也不写封顶（38 复审的 should-fix）。
  - 前台请求前预检、工具循环、活动回合、会话压缩和后台定时回合都读这一处。
  - finalization 的旧归档周期原来只按百分比判断（钳在 50–100），现在经 `trigger_tokens` 拿到同一条线，只在封顶时传；不封顶时原样。
  - `/context` 封顶时写“N tokens 触发（绝对上限封顶，比 90% 更早）”，免得出现“90%（30 万）”这种自相矛盾的显示。
  - 前端配置目录：生成器新增按键名的 `rangeMap`，这个键的范围是 0–10,000,000。按名字猜会给 0–200,000，填不进 25 万。
- **取舍**：
  - 封顶后 recovery 按触发线 × recovery% ÷ 触发% 等比推导，保持“恢复目标与触发线之比”不变（38 复审的 should-fix）。
    1M 窗口、上限 30 万、60%/90% 时是 20 万；仍不超过“触发线 − 近期尾部”。整数先乘后除，浮点比值在部分组合下会少 1。
    不封顶时仍是原公式 min(窗口 × recovery%, 触发线 − 近期尾部)，数值不变。
  - recovery 只是候选“可直接提交”的判定线；压缩后的实际大小由摘要加近期尾部决定，尾部 ≤ 2 万、最多 4 轮（`compact_partitions` 按 `recent_tail_tokens` 截）。
  - 已加进 user_config 的自助修改白名单（`TUNABLE_KEYS`），飞书、TUI 里经 user_config 也能改。只拒非整数和负数，接受的值原样生效；
    回执与两个百分比项一样写“保存后需要重启 Gateway 才生效”。
- **生产值**：3a 定 300,000，部署前写进 desktop.yaml；代码提交不改生产配置。

## 普通消息迟到送达：信封带提交时间与提升年龄门（2026-09-29，分支 `claude/38-stale-delivery-docs`，基于 `89af6b07a`，**待定**，只记方案不实现）

- **背景**：2026-09-27 一条发给已结束定时 run 的插话回执因指纹不兼容卡在 terminal_unknown，修好后会被对账提升成普通请求、两天后当作当前指令送达（集成者部署前挪走）。
  第二次核查（2026-09-29）发现 local/main 32 条 queued 回执并非"还在等"：queued 是终态"已提升为普通请求"，它们对应的请求早已 done/failed，
  按"提交 → 请求结束"算的真实送达延迟：<1 分钟 22 条、1–10 分钟 5 条、10–60 分钟 4 条、1–6 小时 1 条（中位数 20 秒、最大 1 小时）；
  当前 pending/active_pending/terminal_unknown 均为 0。分析：`~/.my-agent/decision-evidence/compact-429-20260929/stale-delivery/stale-delivery-analysis.md`。
- **已做**：`docs/modules/gateway/04-structure.md` 写明 input_receipts 五种状态的真实含义与延迟的正确算法（queued 不是待送达）。
- **待定 1（无开关，改模型请求内容）**：`queue_gateway_input_locked` 物化时给请求载荷加结构化 `promoted_at`、`delivery_delay_seconds`
  （= promoted_at − `prepared_request.submitted_at`，旧回执无 submitted_at 记 null），执行时渲染进 runtime facts 紧挨 `current_local_time`，由模型自行判断是否过时；展示是软约束。
- **待定 2（必须有开关，改送达行为）**：新配置 `gateway_input_promotion_max_age_seconds`（0 = 不限，默认 0，行为不变；生产建议 21600）。超龄时不物化，
  回执写新结构化状态 `stale_held`（held_at、age_seconds），对账 summary 计数、/status 可见，并写目标会话的 pending_host_notices；放行/丢弃走结构化控制
  （如 `/inputs release|discard <request_id>`，经 control_operation_service 幂等回执），永不自动放行；无 submitted_at 的旧回执照常提升。
- **裁定（集成者）**：真正迟到送达只发生过一次且已处理，两项都改变模型请求内容或送达行为，先不做；再出现第二次迟到送达时按本条排期。
  机器判定只准用结构化时间字段（`submitted_at`/`promoted_at`），不看正文；状态改名要走显式迁移记录。

## 429 按供应商声明的限额窗口区分额度用完与临时限流（2026-09-29，分支 `claude/9a-quota-window`，基于 `89af6b07a`，be 已复审，已拣进 step16k `e1ab765c5`）

- **背景**：09-29 04:03 起 local/main 主模型套餐每周额度用完。供应商 429 错误体只有
  `error.type=GoUsageLimitError` 和 `metadata.limitName=weekly`，不命中硬额度错误码表，于是被判成瞬时的
  `ProviderUsageLimitError`：同一后台线程两次失败相隔约 21 分钟（传输层重试加回合级自动续跑），之后还按供应退避反复重试；
  前台用户要等十几分钟才看到失败，看不到「额度用完」的提示。
- **规则**：`_provider_error_indicates_quota_exhausted` 先看结构化错误码，再看供应商声明的限额窗口：
  取限额键（`limitName` / `limit_window` / `window` 等，比较时归一化为小写字母数字，递归到 `metadata`）上的字符串值换算成秒，
  超过 1 小时就判为 `ProviderQuotaExhaustedError`。判定之后传输层不再原地重试，回合级自动续跑直接上抛，后台走额度分路。
- **开放世界**：以下情况都保持原来的瞬时默认，不按 message 文案判断，也不按供应商或模型名分支：
  - 窗口在一小时及以内；
  - 写法认不出；
  - 同时声明多个窗口、分不清撞的是哪一个（按最短的算）；
  - 时间词出现在非限额键上。
- **阈值取 1 小时**：回合级自动续跑默认合计约 6 分钟（10/25/45/100/180 秒），每次模型调用传输层还会再试 3 次；
  后台供应退避 30 秒起翻倍、封顶 15 分钟。一小时以内的窗口靠供应退避能等到重置；更长的窗口原地重试只会白白消耗请求。
- **未采用 Retry-After 规则**：响应头不落盘，只有设置了 `MY_AGENT_PROVIDER_DUMP` 才写诊断文件，这次真实 429 有没有 Retry-After 查不到；
  传输层原本就把 Retry-After 封顶在 30 秒。等拿到真实响应头证据再决定是否加「Retry-After 超上限即额度用完」。
- 测试见 [TESTS.md](TESTS.md#429-按限额窗口区分额度用完与临时限流2026-09-29分支-claude9a-quota-window基于-89af6b07a)。

## 定时任务撞模型额度用完：按失败结算，不再每 30 秒重跑（2026-09-29，分支 `claude/be-quota-settle`，基于 step16k `b929d9f02`，9a 已复审，已拣进 step16k）

- **来源**：复审 9a 的限额窗口改判时，用真实 `scheduler.tick` 加可控时钟探出。证据在 `~/.my-agent/decision-evidence/review-9a-quota-window/`。
  - 额度通知报告（`_provider_quota_fallback_report`）没有 `task_status`。`_finish_scheduler_wake_claim` 查不到终态，按「释放租约 + 30 秒后重试」处理，唤醒不确认。
  - 结果：每 30 秒重跑一次模型，再发一次通知（幂等键相同，飞书按 uuid 在 1 小时内去重），会话里每 30 秒多一条定时提示和一条额度通知。
  - 基线上 `insufficient_quota` 早就这样。9a 改判以后，每周额度这种真实场景也会走进来。
- **做法**：
  - 额度通知报告的 `reason` 固定为 `_QUOTA_NOTICE_REPORT_REASON`。通知已送达（`wake_handled`）时，`_finish_scheduler_wake_claim` 交给 `_finish_quota_scheduler_run`：
    run 记 failed，失败码 `PROVIDER_QUOTA_EXHAUSTED`（ERROR_CONTRACTS 已有登记），再把报告交回 `_complete_wake_report`，在同一拍确认唤醒。
  - 不自动暂停 job，下一个到期照常派发：每次到期最多一次模型调用、一条通知。
  - 车道暂停管不到这里（9a 复审更正）：定时任务走唤醒路径，额度错误在 `_run_wake_signal` 里就转成了额度通知，车道收到的是成功，不会暂停。
    额度一直不恢复时，每个到期点都会发一条通知。目前生产没有启用中的定时任务，影响为零。
  - **待定、未落地**：额度通知按线程限频，或连续撞额度时自动暂停 job。
  - 通知没送达的分支语义不变：释放租约，30 秒后经 `_quota_fallback_wakes` 只重投通知，不调用模型；送达后按上面结算。
  - 会话任务本身的状态不改，与非额度失败的结算一致。普通定时任务没有 Goal 授权，唤醒对账（`_reconcile_wake_queue`）不会给它补续跑唤醒。
- 测试见 [TESTS.md](TESTS.md#定时任务撞额度用完的结算2026-09-29分支-claudebe-quota-settle基于-b929d9f02)。

## 压缩调用撞模型额度用完：后台按额度用完处理（2026-09-29，分支 `claude/be-quota-settle`，基于 step16k `b929d9f02`，9a 已复审；共用判定在分支 `claude/be-quota-predicate`）

- **来源**：同一次复审。压缩摘要和业务调用走同一个后端、同一份额度。压缩撞到 429 额度用完时，异常被包成 `ConversationCompactError`，
  错误码 `COMPACT_PROVIDER_QUOTA_EXHAUSTED`。`is_provider_quota_exhausted_error` 只做 isinstance 判断，于是走非额度分路：
  Goal 记 blocked、不发额度通知、定时 run 记 `CONVERSATIONCOMPACTERROR`。超过压缩触发线的大线程撞额度时会先走这条。
- **做法**：
  - `compact_guard.compact_error_is_provider_quota` 只认结构化事实：`ConversationCompactError` 的码等于 `COMPACT_PROVIDER_QUOTA_EXHAUSTED`，
    或它的 `__cause__` 链上有 `ProviderQuotaExhaustedError`；不看文案，其它异常类型一律不认。
  - 共用判定（9a 复审补充）：车道环境暂停（`cli/gateway_lane_retry._next_failure`）、持久策略失败账（`background_claim._counts_as_policy_failure`）
    和毒丸不计数（`wake_poison._is_transient`）原来也各用 isinstance 判额度。大线程上的持久策略撞每周额度时，三拍都抛压缩包装错误，
    结果是第 3 拍策略退休、车道按普通 30 秒冷却、毒丸计数。现在这三处和唤醒入口 `_run_wake_signal` 都只读会话层的唯一判定
    `compact_guard.is_provider_quota_failure`：直接的额度错误，或 `compact_error_is_provider_quota` 认得的压缩包装。
  - 判定放在会话层，因为后端层（`backends.errors`）不能反向依赖会话层。`is_provider_quota_exhausted_error` 仍是 isinstance，
    传输层、回合级续跑、供应退避和错误分类器这些后端内部调用方不受影响。
  - 子代理 failure_type（`agent_core/subagent_mixin._subagent_run_failure_type`）也改读它（3a 裁定）：子代理压缩时撞额度记成
    `PROVIDER_QUOTA_EXHAUSTED`，不再是 `RUNNER_ERROR`。`contracts/provider_error_classifier` 那处只影响标签，行为上等价，不改。
  - `COMPACT_PROVIDER_QUOTA_EXHAUSTED` 登记进 ERROR_CONTRACTS：不可重试，建议切换后端。
- **既有问题，待定，未落地**：`_provider_error_indicates_quota_exhausted` 在结构化错误码和限额窗口之后，还对整段错误正文做子串匹配。
  硬额度码出现在任意位置、数字 `2056`、`token_plan` 加上「用量上限」「购买积分」「upgrade」「exhausted」这几个词，都会判成额度用完。
  这些都会读到 message 文案，和「不看文案」的铁律冲突。这段兜底在仓库初始化（`0b6252590`）时就在，9a 的改判没有碰。
  要不要收掉，等拿到这些供应商的真实结构化错误体再定。
- 测试见 [TESTS.md](TESTS.md#压缩调用撞额度按额度用完处理2026-09-29分支-claudebe-quota-settle基于-b929d9f02)。

## 唤醒认领的毒丸处理（2026-09-28，分支 `claude/75-wake-poison-design`，基于 `f7a4cc909`，方案已审，分步实现中）

- **第 3 步 C1 已落地（2026-09-29，分支 `claude/38-wake-poison-c1`，基于 9b 的 C5 `cb08b80dc`）**：纯函数与存储层补充，不接线。接线层（75 的 C2–C4）依赖的接口：`WakeAttemptFacts`/`verdict_for_attempt(facts)`、`ledger_corrupt_decision()`、`attempts.has_ledger/preflight/mark_stopping/discard`、`quarantine` 返回 `WakeQuarantineResult(settled, source_unreadable, ledger_preserved_at)`、`replay` 对读不出来源返回 WAKE_REPLAY_SOURCE_UNREADABLE。C6（停机标记）待 C3 的在途登记接口。留档目录 quarantine/unreadable、quarantine/ledger 不随线程删除（无法归属线程），满 14 天归档后由运维清理 archive/（清理规则见 WAKE_POISON_PILL.md 第 8 节：归档记录须连同同键去重回执一起删）；archive/unreadable、archive/ledger 没有保留上限（待定）。
- **第 3 步 C2/C3 已落地（2026-09-29，分支 `claude/75-wake-poison-wiring`，进 step16m）**：C2 给 `run_claimed` 加领取观察者；C3 新模块
  `wake_attempt_tracking.py` 在唤醒车道批次消费处记账、结案，跳过阶段与就绪扫描读持久退避（细节见 WAKE_POISON_PILL 第 10 节第 3 点）。
  - 在途记录持久化回合号（`WakeAttemptStart.turn_id` 写进 `in_flight`，`InflightWakeAttempt.turn_id`）：3a 裁定进程中途被杀时
    预留卡住的问题由 C6 收口，C6 的 preflight 识别出死进程的在途尝试后，对这个回合号走 `reject_pending(turn_id, reject_reserved=True)`。
  - 9a 的两条提醒：`discard` 改为在同一把账锁里删（停机线程的 `mark_stopping` 会并发写同一份账，不持锁时它的原子写可能在删之后
    把账写回来）；C3 不调 `mark_stopping`，它的 `OSError` 由 C6 在停机路径里隔离。
  - 两层计数在真实链路上的先后（C3，按 3a 裁定 (a) 两层同一判据）：程序错误这类会让回合崩溃的失败每次两层各加一，第 5 次毒丸先
    结案唤醒，消息刚释放第 5 次、仍 pending，前台回合可跨回合认领消费；超时、连接、环境故障两层都不计次，只退避与释放，不丢消息。
    回执层自己的上限在唤醒车道上走不到（毒丸先收口），由前台回合反复崩溃触发（C4 的前台窗口与存储层用例覆盖）。
- **第 3 步 C4 已落地（2026-09-29，同分支，进 step16m）**：新模块 `wake_domain_closeout.py` 是领域收尾与"领域已是终态"判定的
  唯一位置（第 4 步 `/wakes` 的重放判定与拒绝提示导入只读的 `wake_domain_status`，删掉本地副本；`wake_domain_terminal` 就是它是否非空；
  reason 按去空白、小写比较，统一以这里为准，9a 复审 be 第 4 步时提的）。
  - 派活唤醒被结案：任务收成 failed，`failure_code` 为 `SESSION_TASK_WAKE_QUARANTINED`（登记在 ERROR_CONTRACTS），沿
    `report_task_result` 回报发送方（metadata 带 `session_task_failure_code`）。`SessionTask.failure_code` 为可选字段，读旧记录按空串，
    旧版读新记录忽略多出的键；SessionTask 不进任何持久化摘要（已核查）。
  - **裁定 b 由做法 2 取代（2026-09-29，3a）**：会话消息唤醒被结案时回执不动，只留宿主提示。理由：毒丸隔离的是唤醒这条执行路径，
    不代表消息本身有毒；方案 A 与后台收尾之后，被隔离唤醒的消息是 pending（已释放或还没认领），仍能交给目标的下一次回合；
    判断消息本身有没有毒的权威是回执层上限（`SESSION_MESSAGE_RELEASE_LIMIT_REACHED`）。提示的 `details` 带 `message_dedupe_key` 与
    回执当前状态，注明仍待投递（`HostNotice.details` 为可选结构化字段，空时不写出，旧提示形状不变；线程记录读回时保留它）。
  - 派活正文已放弃（回执 rejected 带 `SESSION_MESSAGE_RELEASE_LIMIT_REACHED`）：领取后准入 `session_task_body_abandoned`，
    `run_claimed` 先经新依赖 `close_out_source` 把任务收成 failed（`failure_code` 为同一个码）并回报，再 `retire_source` 结案唤醒；
    判据 `_receipt_abandoned` 与会话消息的 `session_message_abandoned` 共用。
  - 宿主提示：来源 `wake_poison`、code 为原因码，`queue_host_notice(replace_same_code=True)` 让同一会话同一原因码只留最新一条；
    结案与长时间不计数提醒都留提示。信封读不出时用选批时冻结的副本收尾（3a 裁定 d）。
  - 领域写失败只打 `[background-wake-poison]` 日志，不影响已落盘的结案。
  - 第 4 步（be）：`/wakes`、`/status` 计数、归档、`wake_replayed`。
- **第 4 步运维面已落地（2026-09-29，分支 `claude/be-wake-ops`；9a 复审的两条必须改已跟进，2026-09-30 随 step16m 上线）**：
  - 管理员命令 `/wakes`（列表）与 `/wakes replay <ID> [confirm]`（预览、确认重放），TUI 与飞书共用 Gateway 控制入口；
    `/status` 与 `gateway_status` 显示已结案条数；重放成功打 `wake_replayed` 事件。日志与宿主提示随第 3 步结案发（75 的 C3/C4）。
  - 14 天归档：顶层结案记录连同坏账留档、读不出的信封移到 `quarantine/archive/`，只移不删；归档是发布层的只读安装位置，
    发布语义不变（同键再发布仍返回原结案信号），重放对已归档的记录拒绝（`WAKE_REPLAY_ARCHIVED`）。
  - 领域终态判定已改为导入 C4 的 `wake_domain_status`，本地副本已删（`44b85c417`）。详见 `docs/design/WAKE_POISON_PILL.md` 第 7、8 节。
- **背景**：`204f4ddf9` 和 `7b83c8730` 都让同一条唤醒约每 30 秒被领取一次、失败或取消后永远重试。
  唤醒只有 pending/handled 两种状态，所有重试节流都在进程内，重启即清零，session_task 等 reason 没有任何上限。
- **方向**：
  - 按唤醒记一份持久尝试账，只数结构化原因（admission 码、异常类型或 `error_code`）；
  - 同因连续 5 次即结案为 `failed_permanently`，退避依次为 30、60、120、240 秒；
  - 瞬时类只按类型排除，未知码默认计数；
  - 结案时同步结掉关联 observation，发布层同键不复活，会话任务转 failed 并回报发送方；
  - 运维可见：结构化日志、宿主提示、`/wakes` 列表与人工重放。
- 与回合内兜底（待处理输入作废 8 次、同一失败 15 次熔断）分层：那两条管一个回合内的调用次数，本方案管同一条唤醒被领取几次。
- 3a 裁定：保留总上限 12；领域收尾覆盖 session_task 与 session_message；只重投路径封顶 15 分钟、满 24 小时结案；
  `SkillSnapshotError` 整族补结构化 `error_code`。第 1、2 步先做，接线等 my-agent-3 的取消修复合入。
- 2026-09-29 复审补充：401/403/407 与配置错误基类属于环境级故障、不计数；批次失败后逐条隔离、只对单条失败计数；
  连续不计数满 24 小时发 `wake_uncounted_stalled` 并每 24 小时提醒，不自动结案。第 3 步接线要额外处理额度回退报告、
  优雅停机的在途尝试和读不出的信封。
- 2026-09-29 二次复审跟进（已实现）：时间只往前走（时钟回拨账仍可读）；三类失败判定必须带原因码；写账前按读回口径校验；
  环境级补 402、404；批次失败也推进连续不计数段。文档更正：环境级故障目前只是不计数、仍由车道冷却重试，按车道暂停列为
  第 3 步第 4 点；另记三条接线约定（坏账按 `attempt:ledger_corrupt` 结案、`record()` 放 finally、宿主提示按会话与原因合并）。
  详见 [WAKE_POISON_PILL.md](docs/design/WAKE_POISON_PILL.md)。
- 2026-09-29 第 3 步 C5（分支 `claude/9b-lane-env-pause`，基于 `89af6b07a`，四个提交均经 9a 复审通过，已进 step16k）：环境级故障判定收成
  `backends/errors.is_provider_environment_fault` 一个权威，毒丸不计数与 Gateway 车道共用；车道对这类故障按车道暂停，会话模型指纹
  （`thread_model_fingerprint`，只读）变了立即放行，否则 60→900 秒探测一次，成功清除；缺模型仍先走等配置分支；状态只在进程内。
  核对结论：`ProviderQuotaExhaustedError` 不会被供应退避和车道冷却重复处理——供应退避只吸收瞬时类；唤醒路径就地转额度通知、
  不抛到车道；观察和策略路径抛到车道按 30 秒普通冷却（策略路径另记持久失败账，3 次退休）。9a 把每周额度 429 改判为额度耗尽后，
  观察路径会从供应退避的 30→900 秒变成车道 30 秒固定重试；**3a 裁定**：额度用完在车道这层显式并入环境暂停（共享判定不变），
  与 9a 的改判同批（step16k）上线。同一原则下持久策略失败账也不记环境级故障与额度用完、不因此退休（改前连续 3 次 401 会退休）。
  9a 复审通过初版，建议 1 已补：取指纹出错按未知处理、只等探测时刻，不把暂停冲成普通冷却。
  真实验收（MiniMax-M2.7，隔离 home、8436）通过：真实 401 → 暂停 60 秒，探测仍 401 → 翻倍到 120 秒；写回正确密钥 1.8 秒后按指纹变化放行，
  随后真实调用成功；两次 401 期间策略没有记失败账（证据 `~/.my-agent/decision-evidence/c5-lane-env-pause-2d6e06626/`）。
  **待定**：成功那一轮的用量事件同时带 1 次「未完成」估算（25985）。它不是上一次 401 探测的估算记到了同一请求上：两次 401 的估算
  14110、20047 已分别出现在前两条事件里，按累计快照增量算，第三条多出来的是成功那一轮自己一条发过 HTTP 尝试、但没完成的调用记录
  （最可能是首包超时或连接中断后重放）。验收 home 按规矩已删，无法再取账本确认；下次真实验收删除 home 前，先读 model call ledger
  各记录的 status、error_code 和 provider_attempt_count。顺带观察（只记，未评估影响）：后台策略运行没有调度 run id 时，
  turn_id 退回用 task_id（`runtime._run_agent`），同一任务的多次策略运行共用一个 turn 身份。

## 持久化指纹必须分版本、兼容旧数据（2026-09-29，分支 `claude/38-guidance-receipt-digest-compat`，基于 `bac2f176d`，已上线 step17a（main de222698b，2026-10-02））

- 已落地：插话幂等回执指纹分 v1/v2，旧回执两种任一匹配、新回执记版本严格校验；对账把"带错误的终态未知"计入 summary 并抛给循环守卫，
  状态与错误没变不重写。规矩：改持久化指纹口径 = 加版本 + 保留旧计算 + 钉固定样本，不得原地改。
- 盘点（只列清单，未修）：a) `input_delivery_service._prepared_payload_digest`/`client_input_digest` 自 09-23 起口径未改；b) `store_usage._model_usage_snapshot_digest`
  611a2923a 未改口径；c) `store_messages.append_once` 去重键 0bb09a76b 只改读取实现；d) 唤醒去重 a127be812（09-23）把发布逻辑挪进 store_wake_publication，
  去重路径命名未改；e) `session_tasks` 09-28 新增正文反向索引 `session_task_body_index.v1`，此前创建的任务没有该索引（`load_by_body_dedupe_key` 返回 None），
  属"新索引无回填"，重放旧任务正文时会走新建；f) 插件激活摘要 09-25 已按非 None 字段稳定化；g) `capability_selection_state` 摘要 09-26 新增，无旧数据。
  除 e) 外未发现其它"改口径未兼容"的持久化指纹。
- 裁定（集成者，2026-09-29）：**09-28 之前创建的会话任务没有正文反向索引，重放时不去重；决定不回填**，原因是存量极少且会话互通一直在禁令下。
  另：生产上那条 09-27 发给已结束定时 run 的旧插话回执，不让它按收敛语义迟到送达（两天前的指令会当普通消息打进目标会话），
  由集成者在 step16i 部署前把该回执挪到备份目录（`claude-tools/stale-steer-20260929/`，附说明、可挪回，不改内容不读正文）；
  收敛语义本身对新近插话保持不变，代码不改。

## 前台命令已退出、只是清理未确认时如实返回执行结果（2026-09-29，`claude/75-scheduler-waiting-deadlock` 的 `84e8db619` + `067d2dd3e`，单独集成）

- 来源：2026-09-28 定时任务停摆的触发点是一次前台同步 `run_command`：命令已经跑完、退出码已知，只是后代进程清理的核对没确认（高负载下清理确认超时），工具却报 `TOOL_OPERATION_OUTCOME_UNKNOWN` / `effect_outcome=unknown`，整轮被叫停。
- 已实现（`tooling/shell.py::_run_shell_process_text`）：命令已退出、退出码已知时如实返回——0 为成功，非零为 `COMMAND_FAILED`、`effect_outcome=failed`。工具循环只在 `effect_outcome=unknown` 时叫停，所以不再叫停整轮；工具操作账按真实结果结算（succeeded/failed），不留 `unknown_reason`。
- 清理未确认作为结构化告警附上：`process.cleanup_confirmed=false`（runtime_facts 白名单字段，模型可见）、`termination` 回执（含 `unresolved_pids`），正文提示“[进程清理未确认]……可能仍在运行，不要为此重跑”。清理已确认时不加任何字段。
- 正文按 `termination.method` 分两种说法（`067d2dd3e`，9a 复审跟进）：`identity_unavailable`、`identity_changed` 时终止入口一个信号都没发，写“[进程清理未尝试] 无法核对进程身份，没有尝试结束它启动的进程”，并列出最多 8 个相关进程号 / 进程组（更多的写“等”，完整列表在回执里）；发过信号但没确认退出的，保持“[进程清理未确认]”。
- 真正结果未知的路径不变：超时后终止未确认、没有终止回执的超时、后台会话状态拿不到。
- 代价：未确认的后代可能仍在运行、继续产生副作用；告警写明这一点，不自动重跑。
- 同分支的定时 waiting 死锁根因修复（`0b04e6fbe` 及其复审跟进）仍在复审，之后单独集成。

## 只读动作的权威读失败以后可换一个可重试码（2026-09-29，待定，尚未落地）

- **背景**：`process_session` 的 `list/status/wait/network_status` 在读不出后台进程权威时，现在记 `effect_outcome=not_started`
  （2026-09-29 修复，见 `TESTS.md` 同题条目），不再让主代理把这一轮收口。但错误码仍是
  `TOOL_OPERATION_OUTCOME_UNKNOWN`，而它的合同写的是「**不可重试**、需人工核对」。
- **问题**：只读动作没有任何副作用，权威读失败其实**可以无害重试**；沿用一个「不可重试」的码，
  语义上偏严 —— 模型和操作账都会把它当成需要人工介入的情形。
- **待定方向（未落地，需单独设计）**：给只读动作的权威读失败换一个**可重试**的原因码
  （例如 `PROCESS_SESSION_AUTHORITY_UNREADABLE_RETRYABLE` 之类），或在 `ErrorContract` 上把
  「只读场景」标成可重试；同时决定它是否仍要写进 `unknown_reason`、是否影响 `required_actions` 的阻塞判定。
- **不做什么**：本次不动错误码，只记这一条；换码会牵动 `ERROR_CONTRACTS`、操作账口径和既有测试，
  属于新的控制语义。

## 后台进程与终端会话结果未知时带出具体原因码（2026-09-29，分支 `claude/75-process-unknown-codes`，基于 `64f7ee64e`，已上线 step17a（main de222698b，2026-10-02））

- 来源：定时任务停摆排查时，工具操作账的 `unknown_reason` 只剩 `effect_outcome_unknown:TOOL_OPERATION_OUTCOME_UNKNOWN`。`claude/75-scheduler-waiting-deadlock`（`0b04e6fbe`）已补 `run_command` 后台三处，这里补剩下三处。
- 已实现：错误码与 `effect_outcome=unknown` 不变（仍不自动重做），只加 `reported_error_code`：`process_session` 权威读不出 → `PROCESS_SESSION_AUTHORITY_UNREADABLE`，清理结果未定（`ProcessSessionCleanupError`）→ `PROCESS_SESSION_CLEANUP_UNCONFIRMED`，停止后进程树退出未确认 → `PROCESS_STOP_UNCONFIRMED`；`terminal_session` 关闭未确认 → `PTY_CLOSE_UNCONFIRMED`。四个码登记进 `ERROR_CONTRACTS`（不可自动重试、人工核对），工具操作账的 `unknown_reason` 随之写成 `effect_outcome_unknown:<具体码>`。
- 边界：只让原因可归因，不改变结果未知的判定条件和恢复路径。

## Gateway 派发线程存活成为结构化事实（2026-09-28，分支 `claude/38-gateway-dispatcher-resilience`，基于 `c101d325a`，已上线 step17a（main de222698b，2026-10-02））

- 现状：派发线程的起止、每次 tick 起止、逃逸异常与"错误打印本身失败"记在进程内账本 `gateway_parts/loop_health.py`，
  心跳与 `/status` 扁平并入 `dispatcher_alive`/`dispatcher_state`/`last_dispatch_tick_at` 等字段；所有后台循环共用的错误打印入口不再抛异常；
  扫描门在扫描前取样 mtime。详见 `docs/modules/gateway/02-progress.md` 同日条目。
- 长期方向（未落地）：外部判活（supervisor、`gateway status`、TUI 状态栏）目前只看进程与心跳新鲜度，下一步可把 `dispatcher_alive=False`
  或"有 pending 而 `last_dispatch_tick_at` 陈旧"纳入就绪/告警判据，并考虑派发线程死亡时的自动重启或安全重启触发。两项都是新的控制语义，需单独设计。
- 已落地（2026-09-29，分支 `claude/38-loop-backoff-everywhere`）：`loop_health` 改按 loop id 计数（`loop_error_counts`/`last_loop_errors`），
  `dispatch_tick_errors` 只投影派发循环；维护/调度器到期/孤儿恢复/心跳循环全部接入退避（心跳只限流不退避），出错等待不快于循环自身间隔；
  `runtime_error_report` 对包装异常只沿显式 `__cause__` 补 `cause_type`/`cause_category`（不改 category）；后台修复段失败只推迟各自到期，
  派发轮询不动；`cancel._explicit_target_is_absent` 只认 FileNotFoundError。仍未接：请求租约心跳、supervisor 进程（各有自己的语义）。
- 同批（9a 复审跟进）：派发循环与后台主循环共用 `LoopErrorBackoff`（连续出错 0.2s 翻倍封顶 30s、同种错误打印限流），堵住"磁盘刚腾出又被错误日志写满"这一段链。
- 同批：飞书/QQ 适配器进程的状态写入失败不再杀进程（记账、下一轮重试、原子写），异常退出固定收尾（停适配器 → 删 pid → 状态 failed）；
  `/status` 的 `adapter_alive` 只按 `adapter.pid` 进程存活判定。未落地：supervisor 对"适配器进程死了"的自动拉起仍只看 pid+状态文件，可复用同一事实源。

## Jev 前台超时原因与 observe 不挡主链路（2026-09-28，分支 `claude/9a-jev-observe-async`，已合入 main `2cf214bd5`，开关默认关）

- **超时原因**（只读分析；数据是 outcomes.jsonl 593 条，model_usage 按 run_id 一对一关联 358 条，不读正文）：
  - 主因是外部链路按时段变慢，不是请求大小。09-28 10:00（PDT）之前前台超时 22.2%，之后 1.0%，这期间前台设置没改；前后台在同样三个时段一起变差。
  - recall 输入 8.5k–10.5k token，和耗时的相关系数 r=0.08；瘦身上线后 token 少了 5%，耗时中位数不变。
  - 每次调用都经本机代理新建 TLS 连接到 Jev（09-26 实测握手 0.8–3.6 秒）。各阶段耗时没有落盘，分不清慢在代理还是服务端；阶段耗时落盘由 dsh-be 另做。
  - pre_recall 的超时是结构性的：它和 recall 共用前台阶段，只能拿到剩下的预算。
  - observe 模式下前台同步等待，结果却不采用：用户轮次合计多等中位 4.0 秒、P90 8.0 秒。前台 recall 调用约 93% 来自后台唤醒轮。
- **语义（已实现；开关 `observe_nonblocking_enabled`，默认关）**：
  - 只转普通 thread 范围、点位有效模式为 observe 的调用；apply、实验与用户后台点位不变。
  - `decide` 当场返回 `deferred` 占位（不可采用，也不写结果行）。调用交给进程内唯一的后台 worker 串行执行，排队上限 8（数据依据见常量注释）；队列满时记 `skipped/observe_nonblocking_busy` 结果行，外加一个 reach 内部码。
  - 后台预算用 `background_timeout_seconds`，不占前台阶段预算。
  - worker 执行前按阶段身份装入 runner 上下文，send 返回后立即恢复，写行、结算、回调都在恢复之后运行。任务属性按快照拷贝。后台线程里的身份复核因此恒为真，真正起作用的是设置版本、连接、在途撤销标记和回合取消令牌。
  - 用量走独立范围 `decision-observe:*`（source=`decision_observe`），不进任何 run 的累计容器，既不重复计也不缺报；结果行标 `blocking=false`。
  - 用户停止：请求发出前停止的就不再发送（记 `stale/turn_cancelled`）；已发出的让它自然结束，照常记账。设置撤销和宿主关闭沿用原取消语义，排队中的调用直接作废、不发送。Gateway 停机时，取消之后最多等 2 秒，让后台执行器写完结果行和用量；等待与取消分开隔离，等待出错不影响取消。
  - 有意的边界：后台调用超时以后，worker 里迟到的供应商事实只留在进程内账本，不再补进 model_usage。
  - 选模型：请求记录里的观察标记先记 `deferred`；后台完成时回合如仍在进行，就补记建议编号；回合已结束则不回写，结果只留在结果日志里。
  - 同步调用和后台调用使用不同的有界资源键，后台 observe 在途时，同会话的 apply 调用不会被拒。
- **开关关闭时**：决策的逻辑与时序不变，但结果日志每行会多一个附加字段 `blocking`（值为 true），所以不是“逐字不变”。
- **与 B 第 0 步合并**：后台 observe 行同时带 transport 与 blocking=false，估算输入进独立用量范围；忙码与发出前取消都不算 Jev 失败，也不进 not_sent。
- **复审跟进**（dsh-ae，同分支后续提交）：补了“恢复身份”“补记核对 claim”两处存活变异体的测试；身份快照改为真拷贝；停机时加有界等待；注释改成实际起作用的复核。
- **默认关的理由**：不打乱 09-29 的同钟点复测。由集成方经 `decision_patch`（owner 级）打开，或在 TUI 决策菜单里打开“观察不挡回复”。
- 细节见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#observe-不挡主链路2026-09-28默认关)和 TESTS 同日条目。
- **收尾（2026-09-29）**：复测期间开关一直关着，没有 `blocking=false` 的生产样本；4 个 my-agent 不再恢复，前台流量的主要来源没了。
  最终建议保持关闭，默认值不改。以后要打开，先在隔离 home 验收，步骤见
  [真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 18 节「Jev 观察线收尾复测」。

## `/endtask`：管理员结束卡在等待中的定时会话任务（2026-09-29，分支 `claude/be-end-session-task`；已随 step16g（`bac2f176d`，2026-09-29 02:11 PDT）部署，生产只读验收通过）

**来源**：2026-09-28 生产事故。两个会话由每 5 分钟一次的定时任务驱动，各有一次定时运行里的 `run_command` 结果未知：
- 工作片以 `runtime_status=unfinished`（`TOOL_OPERATION_OUTCOME_UNKNOWN`）停下，会话任务仍是 active；
- `_finish_scheduler_wake_claim` 看到任务 active，就把定时运行停在 waiting 等后续事件；没有子工作也没有后续事件，
  `reconcile_waiting_run` 只在任务到终态时结算，于是永远 waiting；
- `_job_has_active_run` 让同一 job 的到期派发一直跳过，会话从此不再被唤醒。
当晚没有正式入口，只能经 3a 批准、用产品 API 手动把两条会话任务推到 cancelled（记录在 `~/.my-agent/releases/claude-tools/stuck-srun-20260928/`）。

**做法**（已实现）：新增会话控制 `/endtask`，TUI 与飞书共用 Gateway 控制入口，仅本机管理员（含已绑定管理员的 IM 身份）可用。
- `/endtask` 列出等待中的定时执行及能否结束；`/endtask <任务ID>` 只读预览；`/endtask <任务ID> confirm` 才写。
- 放行条件全是结构化事实：定时账本里这条运行是 waiting；会话任务链接是 active；运行库里这个任务整棵执行树（含子代理）
  没有未结束的 attempt（终态判定只用 `runtime_db.operations.attempt_status_is_terminal`）。运行库读不到按“无法确认”拒绝。
- 确认只写两处：会话任务按 `expected_status=active` 的 CAS 改为 `cancelled`；再对同一任务调 `reconcile_waiting_run`。
  结算没成时，定时层每轮入队前的批量对账会再收口。不改运行库、不重做未确认的工具操作。

**部署与验收**（状态：已随 step16g（`bac2f176d`，2026-09-29 02:11 PDT）部署，生产只读验收通过）：
- 验收只读、不带 confirm。
  - 调用路径与 TUI 相同：经生产 Gateway 控制入口，以本机管理员身份（`local-agent` / `chat`）提交；回环来源免令牌，不读配置。
- 两步结果：
  - `/endtask` 列表：ok，空列表（生产上没有等待中的定时执行）。
  - `/endtask <不存在的任务ID>` 预览：拒绝码 `END_TASK_NOT_WAITING_RUN`。这说明管理员通路、预览路径和拒绝码在真实 Gateway 上生效。
- “不停后台命令”这句提示的验收：
  - 这句只出现在有效候选的预览和确认结果里。
  - 生产上没有候选，所以改用替代证据：生产 runtime 中 4 个 `/endtask` 文件与 `bac2f176d` 逐字节一致；`test_end_task_control.py` 对预览和确认结果都断言了这句提示。
- 不为验收人为制造等待中的定时执行。
- 证据目录：`~/.my-agent/decision-evidence/endtask-acceptance/`，含脚本 `endtask_acceptance.py` 与结果 `result-step16g-20260929.jsonl`。
- 第一次真正执行 `confirm`：只在生产上确实出现卡住的等待中定时执行时进行，由管理员操作：
  1. 先发 `/endtask` 看列表；
  2. 再发 `/endtask <任务ID>` 看预览；
  3. 按预览末尾的提示发 `/endtask <任务ID> confirm`。

**边界与未做**：
- 这是事后收口入口，不是根因修复。根因修复另排：定时运行只有在确有后续工作（子代理、后台进程、已登记的续跑事件）时
  才进 waiting；工作片以“结果未知”停下且没有后续工作时，任务应进入结构化的阻塞终态、定时运行记失败，让 job 继续。
- 收口后运行库里这次执行的 AgentRun / TaskRun 仍是未关闭状态（attempt 已结束），不影响后续派发；唤醒发现只在代理树终结后才补关。
- 结束任务不停止它启动的受管后台命令，命令结束后的完成通知会落到已取消的任务上；预览和确认结果固定写明这一点（9a 复审）。
  列表和预览附后续工作事实码（已补，2026-09-29，分支 `claude/be-endtask-follow-up-facts`，基于 `e850ceb04`）：直接调 owner 的
  `scheduler_service.follow_up`（与定时执行收口同一个 `SchedulerFollowUpPolicy`），按 `FollowUpQuery(会话, 任务)` 取
  `FollowUpFacts(present, unreadable)`，只显示事实码，读不出的写“项目:错误码”，没有写“无”，判定未注入写“判定不可用”。
  `GRACE_BOUND_FACTS` 里的码（直接引用 `task_follow_up` 的常量）后面加“（宽限期内才算）”，与自动收口过宽限期不再计入它们一致（3a 定）。
- 从核对 attempt 到做 CAS 之间，可能有唤醒新起一个 attempt（9a 记录，不挡合并）；确认前后都只认结构化事实，CAS 仍只在 active 时改。
- 设计细节见 [CLI 参考](CLI_REFERENCE.md) 的 `/endtask` 说明。

## 定时执行没做完时只凭后续工作事实决定 waiting（2026-09-29，分支 `claude/75-scheduler-waiting-deadlock`，基于 `c101d325a`，已上线 step17a（main de222698b，2026-10-02））

来源：my-agent-2/4 的 300 秒定时任务永久停摆。一次前台 `run_command` 回报 `TOOL_OPERATION_OUTCOME_UNKNOWN`，回合以 unfinished 结束、会话任务仍是 active；`_finish_scheduler_wake_claim` 无条件 `park_waiting`，而 waiting 只在任务终态时对账、`_job_has_active_run` 又让同一 job 有 run 就不派发，于是死锁。两条操作的 `unknown_reason` 都只剩通用码、受管账本的 `result` 为空，排障也无从下手。

已上线 step17a（main de222698b，2026-10-02）：
- 进 waiting 的前提是结构化后续工作事实：`conversation/task_follow_up.task_follow_up_facts` 只读活跃 Goal、指向该任务的待处理 guidance、未终态子代理、指向该任务的待处理唤醒（排除定时触发本身和正在处理的唤醒）、未发完成通知的受管后台命令、启用的进度策略。某一项读取失败只记这一项（`FollowUpFacts.unreadable`：项目名 + 结构化错误码），按仍有后续工作处理（fail closed）。
- 读不出来不能变成永远等（be 复审 M1）：
  - 唤醒、进度策略、后台命令三项读的是 owner 全量数据：读到的记录里已有本任务的匹配就直接算存在，同类坏记录不能遮住它（be 复审 A 的 P2）；没有匹配时，读错误先按记录归属限定：坏记录能解析出属于别的任务（唤醒的 `root_task_id`、策略的 `task_id`、后台命令没有完成通知目标——缺键或盘上的规范空形状 `{}`（be 复审 A 的 P1）——或指向别的任务/会话存储）就不计入，解析不出归属的仍计入；
  - 读不出时按 run 节流（600 秒一条）打结构化告警 `scheduler_follow_up_unreadable`，写明项目和错误码；
  - 只剩读不出、停满宽限期的 6 倍时，按 `SCHEDULED_TASK_FOLLOW_UP_UNREADABLE`（登记在 `ERROR_CONTRACTS`）结算为受阻并排宿主提示；确认存在的后续工作仍然优先。
- 收口 CAS 失败、只释放租约时，和其它释放分支一样退避 30 秒（be 复审 M2），避免任务写入持续失败时每拍重新领取、再跑一整片模型。
- be 复审 S1–S5（复审修复 B）：
  - 新事实 `unsettled_subagent_completion`：本任务血缘里、直属会话、会发完成唤醒的已终态子代理，只要收口 WAL 还没交付、有去重键前缀 `subagent-finished:<run_id>:` 的待处理唤醒，或刚终态（`ended_at` 在宽限期内，盖住“终态先落盘、WAL 与唤醒后写”的间隙），就算后续工作。这也修掉了收口时子代理刚结束、唤醒还没发就把父任务误标受阻的问题（S2）。
  - 宽限期不再写死：`max(600, 5 × orphan_supervision_interval_seconds)`（`waiting_grace_seconds`），由组合根注入 `SchedulerFollowUpPolicy`；同一个值也是“刚终态”窗口，读不出的限期是它的 6 倍（门槛和 6 倍用同一个推导值）。没有新增配置项。
  - 读不出的计时从“首次观察到只剩读不出”算起（按 run 记在进程内，出现会自己推进的后续工作或读全时清掉，重启归零），不从 `waiting_since` 算：扫描时一条唤醒被并发处理移走会产生瞬时读错误，不能让已经合法等了很久的 run 立刻被结算。宽限期内事实不清零这个计时。
  - 子代理两项改为按 id 精确读血缘记录（`list_runs_by_ids_report`）：血缘里有、记录缺失或损坏的记为读不出（受限期和告警约束），不再被当成“还有子代理在跑”；直属与否复用完成通知器的同一判据。
  - 每项事实靠什么机制消失写在 `task_follow_up` 模块头：子代理、唤醒、后台命令、子代理完成都有机制推进；活跃 Goal、待处理 guidance、启用的进度策略没有机制保证会消失（`GRACE_BOUND_FACTS`），过了宽限期不再计入（S4）。健康的 Goal 在两片之间总有一条待处理的续跑唤醒，那一项才是会自己推进的事实；续跑是先 `mark_handled` 上一条、再发下一条，对账可能恰好落在这个空隙里，所以按“没有后续工作”结算要两次确认（见下一条）。
  - 进度策略归入 grace-bound（3a 裁定选 (a)）：进度唤醒触发对账后策略仍启用，只会继续等、不能让这一片收尾；把 `next_due_at` 算作推进会在策略间隔小于宽限期时永远等下去。定时任务不能只靠进度策略续命，被结算后 job 的下一周期照常派发，工作不丢；宿主提示也写明这一点。
- be 复审 B 的跟进（M-B1 与三条应改）：
  - 按 `SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP` 结算要两次确认：按 run 记首次观察到“没有会自己推进的后续工作、也没有读不出”的时间（进程内 `_absent_since`），至少隔一个 60 秒判定周期再次看到才结算；中途出现会自己推进的事实或读不出就清掉，重启归零。盖住 Goal 续跑空隙、后台命令完成通知等发布间隙。
  - 唤醒归属：去重键以 `subagent-finished:<本任务血缘子代理 id>:` 开头的坏唤醒按“可能属于本任务”处理（它的 `root_task_id` 是子代理树的根）。
  - `has_persisted_subagent_parent` 改为 `runner_completion_wake` 的公开函数，`task_follow_up` 不再引用私有函数。
  - 存量 waiting 的后续工作判定每个 run 最多 60 秒算一次（S3）。
  - 用真实子代理记录和真实血缘（`attrs.conversation_task_id` = srun）覆盖“子代理在跑 → waiting → 子代理结束 → 结算”（S5）。
- 分工：自动收口只处理“确认没有后续工作”（以及长期读不出）；`/endtask` 是管理员的人工出口，只要求执行树里没有正在跑的执行，不看后续工作事实。两边对同一任务都做 `expected_status=active` 的 CAS，输的一方不写任何东西。
- 没有后续工作时（`scheduler/active_run_closeout.close_active_run`）：会话任务 CAS 成 `blocked`（期望原状态 active），排一条来源为 `scheduler:<job_id>` 的宿主提示，这次定时执行记 failed。结算码按 `runtime_reason` 选：工具结果无法确认 → `SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN`（提示写明需人工确认是否重做），其它 → `SCHEDULED_TASK_UNFINISHED`。job 的周期不动，下一周期照常派发。CAS 没成功只释放租约，交给原路径按新状态收口。
- `blocked` 进入任务终态映射（→ failed，码 `SCHEDULED_TASK_BLOCKED`），也顺带修好了报告 `task_status=blocked` 时每 30 秒重试一次、永远结不了的情况。
- 存量 waiting 的出口（`settle_stale_waiting`，由 `reconcile_waiting_run` 调用）：对账时任务仍 active、`waiting_since` 已满 600 秒宽限、且没有任何后续工作，就同样标受阻、排提示，按 `SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP` 结算。宽限只为盖住子代理刚结束、生命周期唤醒尚未发布的间隙。
- 原始结论不丢：`run_command` 后台三处原先只报通用码的分支补上具体 `reported_error_code`（`BACKGROUND_SESSION_ATTACH_UNCONFIRMED`、`BACKGROUND_SESSION_STATUS_UNCONFIRMED`、`BACKGROUND_LAUNCH_CLEANUP_UNCONFIRMED`，登记进 `ERROR_CONTRACTS`，均不可自动重试、需人工核对）；受管账本 UNKNOWN 分支写入提供方回报的 `result` 与 `error_code`；`_unknown_claim_result` 取上一次原始结论时顺序反了（上一次已是未知码时直接用记录码，把原结论盖掉），改为取第一个不是通用未知码的值。
- 报告链路：`AgentRunResult.runtime_status/runtime_reason` 经 `BackgroundExecutionResult`、`_BackgroundSlicePlan` 带到 `BackgroundMainAgentReport`，收口只读这两个结构化值。

边界与未做：
- 前台 `run_command` 命令已退出、退出码已知、只是清理未确认的分支：已由同分支的 `84e8db619` + `067d2dd3e` 单独先行集成（随 step16g 上线），如实返回执行结果并附结构化告警，见本台账「前台命令已退出、只是清理未确认时如实返回执行结果」一节。
- `process_sessions.py`、`pty_sessions.py` 里另外几处只报通用未知码的分支：已由 `47bd37d20` 补上具体原因码（随 step16f/step16g 上线），见本台账「后台进程与终端会话结果未知时带出具体原因码」一节。
- 受阻任务由用户处理；本轮没有新增自动重做或自动恢复。

## 墙钟超时后旧请求仍在途，重试可能向供应商重复发送（2026-09-28，来源：`27283cb76` 复审，基于 `6c2fad4da`，暂不排期）

**现象**：模型调用的发出前登记（子代理业务标记、发送前钩子、插话提交）已经完成、HTTP 请求正在发出或等待响应时，
如果触发墙钟超时，会出现下面的链条：

- 保护线程只抛出 `ProviderTimeoutError(stage="wall_clock")`（`agent_core/tool_model_generation.py:1088-1092`），
  不中断工作线程，旧请求继续发往供应商，最长可以跑到后端请求超时 `max(原 request_timeout, 墙钟期限)`（`:1142-1146`）；
- 失败处理按原调用编号把插话退回预留（`_generate_with_wall_timeout`，`:1015-1027`）；
- 门槛5 的超时重试新建调用状态，带着同一批插话再提交一次、再发一次（`_retry_once_after_timeout`，`:454-498`）。

结果是供应商可能收到两个请求：多花一份费用，同一批插话也可能被处理两次。账本按“首个终态获胜”，旧请求迟到完成时的
真实用量不会入账（`contracts/model_call_ledger.py:624`），费用因此少记。用户停止不在此列：那条分支会中断工作线程、
收回连接（`:1083-1086`），而且不重试。

**现状**：`27283cb76`（待合并）只处理了“放弃之后迟到提交插话”：调用被放弃后，工作线程不再登记、不再提交、不再发送。
登记已经完成之后的这个窗口还没有处理。

**方向（待定，可单选或组合）**：

1. 墙钟放弃时也中断在途 HTTP：复用用户停止已有的 `_interrupt_generation_worker`，也就是有界调用的中断句柄，让旧连接
   在重试之前关闭。这只能缩小窗口，请求如果已经完整送达，供应商仍可能照常处理。
2. 重试之前确认旧请求已经终止：等工作线程结束，或者中断后有界等待，再发重试。代价是重试变慢。
3. 接受现状并如实记账：插话批次和回复仍只采纳一次（至多一次），不保证供应商只收到一次请求；两次请求在用量上都如实记账。
   `ef89ba3c2`（待合并）会把超时调用的发送前估算输入记进 `estimated.unfinished_*`，但迟到完成的真实用量仍需另行补记。

**触发条件与量化（2026-09-29，只读）**：

- 这个问题只在关闭流式时才会发生。墙钟守卫只在传输层不负责流式超时时才抛 `wall_clock`
  （`_wait_for_generation_result` 的条件是 `not transport_owns_timeout`），而流式 HTTP 后端的 `stream_timeout_is_idle` 等于 `stream_enabled`。
- 生产的 `desktop.yaml` 和全部模型档案都没有关闭流式。流式调用的超时由传输层看门狗判定，阶段只有 `first_event` / `stream_idle`，
  看门狗先关连接再抛错；回合级自动续跑至少等 10 秒才重发，不会出现旧请求在途时并发重发。
- local/main 近 7 天主调用超时 41 次，全部出现在 19 万到 79 万 token 的上下文里。
- 重发输入的上界约 1,939 万 token（09-27：151 万，09-28：984 万，09-29：805 万），约为 09-28 主调用输入 22.48 亿的 0.44%，
  而且这部分大多按缓存命中计费。
- 09-28 用量暴涨来自调用次数（约 5.5 倍）乘以平均 38.6 万的上下文，不是超时重发。
- 超时阶段目前没有落盘，「主调用超时都是首包或流空闲」来自配置和代码的推断。
- 证据：`~/.my-agent/decision-evidence/wallclock-retry-quantification-20260929/`（README、脚本、输出）。

**状态**：暂不排期（关闭流式时再排，方向按选项 1+2+3）。排期时先用确定性钩子复现（可参照 `27283cb76` 的测试写法），
并让账本能区分“旧请求迟到完成”和“重试请求”。在 model_usage 汇总里加按阶段的超时计数记为可选项，这次不做。

## 【待用户决策】owner 墙模式下凭据文件（`.env` 等）不被拦（2026-09-28，分支 `my-agent/self-dev`，只记录、未改行为）

相近文件名建议那轮做安全复审时发现一个**策略缺口**，不是那个模块的 bug，本模块不擅自改策略，只登记待用户决定。

- **事实（已实测）**：`path_access_policy.check_path_access()` 对 `.env` 这类凭据文件名的拒绝，只作用于
  **未受 owner 墙约束的普通模式**（`_credential_file_decision`，注释里写明它是 legacy/unscoped normal-mode guard）。
  - 普通模式：`.env` → `allowed=False`，`code=PATH_CREDENTIAL_FILE_BLOCKED`；
  - **owner 墙模式：`.env` → `allowed=True`**，即会被正常读取、也会出现在相近文件名建议里。
- **为什么值得决策**：owner 墙正是**远程多用户**场景下用的模式。此时 `.env` 正文会进入模型上下文、
  随请求发给模型服务商。本机单人使用问题不大，多用户部署时可能不是想要的默认。
- **可选方向（未评估优劣，等用户定）**：
  1. 保持现状，只在文档里说明 owner 墙下凭据文件仍可读；
  2. 对凭据文件名统一加一层保护（例如需要显式确认，或读取回执里对正文脱敏）；
  3. 把凭据拒绝提升为与墙无关的通用策略。
- **相关测试**：`test_filesystem_near_name.py::test_credential_file_blocked_in_normal_mode_but_allowed_under_owner_wall`
  已把**两种模式的现行行为**如实钉住，将来无论怎么决策，行为一变就会红。

## 产品持久数据保留策略盘点（2026-09-28，分支 `claude/9b-storage-retention-audit`，基于 `54880f8e9`；2026-09-29 状态：部分落地）

下午数据盘写满后做的只读盘点。生产 home 里产品自己写的数据约 20 GB（发布备份 24 GB 另行处理），其中管理员 home 约 17.1 GB。按大小排前几位的是：旧版任务工作区 `O/tasks` 8.30 GB（有保留键，但 365 天且只认已完成的目录，基本回收不到，最大的是用户任务里克隆的项目），会话本地库 `local_store` 1.92 GB，每轮上下文快照 1.90 GB，子代理目录 `O/agents` 1.30 GB（13.3 万个条目，几乎都超过 90 天），`global_index` 四个只追加索引 0.93 GB，另有两处旧布局遗留 0.87 GB 和 0.80 GB。

现有的 `MemoryRetentionService` 只在 Gateway 运行时触发，扫描写死在旧版 `O/tasks/`，新的两个运行工作区根 `O/runs/`、`O/audits/` 都不在其内（根由 `conversation/workspace_paths.py` 决定，扫描应从这里推导；2026-09-28 裁定两个根都纳入 `completed_task_days`，工具输出暂不扩）；工具输出、Gateway 请求记录、`runtime.db`、快照和子代理目录都没有任何清理。`audit_days` 扫的 `O/audit/` 是记忆归档原始事件，真正的审计日志 `O/logs/audit/audit.jsonl` 和 `O/audit_log.jsonl` 也没有任何清理（缺口 7）。方向：

- 一张存储登记表作为唯一保留权威；
- 只追加的日志和索引按段轮转或整理压缩；
- 快照和记录在终态后按天数保留，并保护仍被结构化引用的项；
- SQLite 库做行级保留和定期 VACUUM；
- 旧布局遗留一次性迁移处理；
- 用户交付物不自动删，改为提供占用视图和提示；
- 维护不再依赖 Gateway 运行；
- 磁盘压力时兜底维护；
- 测试数据与生产 home 分开。

**落地状态（2026-09-29 核对，代码按 main `89af6b07a`，生产按 step16i 首跑）**：
- 已落地（R4，step16h/16i）：
  - Gateway 维护线程按 owner 到期跑维护：保留 apply → 文本向量缓存回收 → global_index 压缩，结果写 `O/data/maintenance.json`，
    每次 apply 在 `O/audit_log.jsonl` 记 `owner_retention_applied`；
  - `completed_task_days` 覆盖 `O/runs`、`O/tasks`、`O/audits` 三个根（按规范深度认任务根）；
  - 路径级错误只剔除重叠动作，不再整次拒绝；
  - `audit-log --cleanup` 指到真实审计日志；扫错目标的 `audit` 类别已停用；
  - global_index 四个只追加索引按门槛重写压缩，生产首次压缩把三份 355/282/284 MB 的索引压到 41/56/55 MB，读取侧 0 差异。
- 部分落地、剩余待定：
  - 工具输出与子代理 scratch 仍只扫 `O/tasks`；
  - 审计日志只能手动清，`O/audit_log.jsonl` 与 LocalStore 副本没人清；
  - 快照、runtime_facts、Gateway 记录、`O/agents` 仍无保留规则；
  - `completed_task_days` 会连交付物一起移入回收站，与「交付物不自动删」的方向不一致。
- 未做（待定）：
  - 维护仍只由 Gateway 触发；
  - `compact` 按 mtime 删、无引用保护；
  - 移走线程的孤儿文件；
  - 测试 owner 移除入口；
  - 存储登记表；
  - 其余只追加日志的轮转；
  - SQLite 行级保留与 VACUUM；
  - 旧布局迁移；
  - 占用视图；
  - 磁盘压力触发维护。
- 已知残留：
  - 已修（基于 step16l、待合并）：只要有路径级错误，维护状态就记 `policy_unavailable`、把「其实执行了」掩盖掉。现新增 `apply_outcome` /
    `isolated_error_count` / `last_applied_at`，审计事件加隔离计数；Gateway 摘要 `failed` 只算整次被拒与执行期失败，另加
    `refused` / `isolated`（摘要只打印）；持久化旧字段含义不变。
    /status、TUI 展示维护状况待定。
  - 待定：解析不了（坏 JSON）的任务 state.json 会被 completed_task 与 tool_output 两个扫描器各报一次，错误条数不等于子树数；
    生产那 355 条是能解析、只缺 status 的旧格式，只由 completed_task 报一次，路径不重复；
  - 待定：不活跃 owner 不会自愈迁移，因为 v1→v2 策略迁移只挂在 Curator 上。`93b8c3ffffb8` 已按裁定一次性迁移，
    `ebd40e6fc3ec`（像真实用户）交用户决定；
  - 两处扫描注释与实际扫描的根不一致；
  - 手动审计清理替换文件时不持追加锁。
- 生产首跑积压（09-29 21:39:50）：
  - local/main `applied=true`，11742 个动作全部执行、失败 0，与 plan 逐类对上：tmp 删 3581、subagent_scratch 进回收站 8090、tool_output 71
    （其中 46 条执行时已不在）；
  - 355 棵 `TASK_STATE_INVALID` 子树被隔离未动（待定；诊断和裁定见本台账「旧版任务状态文件：355 棵 `TASK_STATE_INVALID` 保留原样」一节）；
  - 两个 owner 的 `retention.json` 无效，每天整次拒绝执行（待定）。
- 逐条出处见 STORAGE_RETENTION 第 8 节。

详见 [STORAGE_RETENTION.md](docs/design/STORAGE_RETENTION.md)。

## 决策统计口径：讲清“缺报”，没发出去的单列（2026-09-28，分支 `claude/be-jev-transport-timing`，在 `b792340e0` 之上，已合入 main `57a26743e`）

- **起因**：
  - 用户看到“决策模型成功 140 多、失败 63，还有一些缺报”。超时调用在用量里是 0 token，被一并算作缺报。
  - dsh-9a 分析发现：pre_recall 和 recall 共用阶段预算，pre_recall 常在 `budget_exhausted` 时 1–4 毫秒就返回，根本没发请求，却被算成 Jev 超时。
- **口径**（只改展示与汇总，不改记账，也不改预算逻辑）：
  - TUI 决策段分开标注 token 来源：
    - 已报：供应商回报，外推到成功调用；
    - 估算（未完成）：发出去之后超时或失败的调用，取本地估算。
  - 次数互不重叠，可以直接相加：成功、失败、未发出。真的一点数据都没有的，只在所属次数后面加说明，不另起一个“缺报”数，
    例如“成功 2（其中 2 次未回报用量）”“失败 3（其中 3 次分不清是否发出）”（ae 复审建议）。
  - 失败只算发出去之后的；一次 HTTP 尝试都没有的，单列“未发出”，不计入失败。
  - 非决策调用（主模型、辅助调用）同日统一为同一口径，显示为“LLM 估算 N token（未完成） · 未发出 M · 缺报 K”，
    数字是全部用途减去决策分区。成功但没回报用量的已按本地估算计入会话累计，不算缺报；`unreported_calls` 已删除。
    统一前先核实过非决策调用确实记了两样东西：发送前估算（主模型取可见上下文快照，辅助调用取请求材料），以及 HTTP 尝试
    （两处观察者都写入账本）。没有补记任何账。
  - `audit_records`：
    - 用量行新增 `jev_failures`、`not_sent_calls`、`failures_send_unknown`；
    - 各点位结果里，没发出去的失败类结果单列到 `not_sent`，不计入超时率和失败率。
    - 两种“未发出”范围不同：`points.not_sent` 还包括没建调用记录的冷却、`budget_exhausted` 等，通常比 `not_sent_calls` 大，
      工具说明已写明不要对比或相加。
  - 口径只有一个权威位置：`model_metrics.unfinished_usage_facts` / `split_unsent_failures`。汇总只累加原始次数，展示时推导。
- **飞书**：飞书没有统计行。决策统计由 my-agent 调 `audit_records` 回答，与 TUI 同一口径，工具说明已同步。
- **已知边界**：
  - 旧账里的失败分不清当时是否发出，整体仍算失败：决策段在失败次数后加说明，LLM 段显示为“缺报”。
    旧显示快照缺 `decision_unknown_failures` 键时同样按这条规则处理，不能补 0 后显示成“未发出”
    （9b 复审发现，修复 `d802380d8`）；旧快照没有全部用途的失败构成，LLM 段留空，下一次模型边界重算后恢复。
  - 迟到的尝试事件记进账本或用量快照之前，这次调用会暂时显示成“未发出”，补记之后才转成失败并带上估算。
- 细节见[审计设计](docs/design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md)第 4 节，以及“没发出去与发出去后失败分开”一节。

## Jev 决策调用的链路分段计时（B 第 0 步）（2026-09-28，分支 `claude/be-jev-transport-timing`，已合入 main `611a2923a`）

- **起因**：dsh-9a 查明前台超时主要来自连到 Jev 的链路在某些时段变慢。
  - 每次调用都经本机代理重新建 TCP、CONNECT 和 TLS，urllib 不复用连接；09-26 实测握手 0.8–3.6 秒。
  - `timeout_stage` 只在内存里，各阶段耗时没落盘，分不清慢在代理还是 Jev 服务端。
  - 超时调用在 model_usage 里 token 为 0，这就是用户看到的“缺报”。
- **已实现（只量不改发送）**：
  - 传输层新增 `backends/transport_timing.py`。只有观察者显式开启 `transport_timing` 时，每次 HTTP 尝试才分段计毫秒：
    connect → proxy_connect → tls_handshake → request_send → first_byte → body_read，每段结束发一次 progress 事件。
    目前只有决策调用开启；未开启的调用事件与原先完全相同。
  - 计时只包裹标准库连接已有的步骤；发送字节、`max_retries=0`、发送许可、期限都不变，不复用连接。
  - 账本：尝试条目带 `transport` 快照；progress 只换计时，不改状态、活动时间和事件；超时记 `timeout_transport_phase`。
  - 落盘：决策结果日志里建了调用记录的行带 `transport`（`attempts` 为空表示请求没发出），内容是终态、超时阶段、超时时所处的传输阶段、本地估算输入、各尝试分段毫秒。
  - model_usage：超时或失败、且已发起 HTTP 尝试的调用，本地估算输入记进 `usage_breakdown.estimated.unfinished_*`，
    与 provider 桶分开。`audit_records` 的 decision 主题同步带出这两项。
- **不做**：keep-alive 和连接复用，等拿到数据再定（B 后续步骤）；TUI 统计行口径不变。
- **已知边界**：caller 超时后 worker 可能仍在传输，结果日志记的是超时那一刻的阶段；之后的迟到事实只在内存账本里。
- 细节见 `TESTS.md` 同日条目与[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)“链路分段计时”一节。
- **收尾数据（2026-09-29，140 次各段齐全的成功调用）**：
  - 建连（经代理的 TLS，含代理拨上游的时间）约占网络耗时 50%，首字节约 49%。
  - 11 次超时里 8 次卡在 tls_handshake；按超出均值归因，全部在建连。
  - 保活估算：60 秒可复用 29%，每次约省 339 ms，挡不住建连超时；300 秒可复用 55%，每次约省 647 ms，最多避免 10 次（上限）。
  - B 第 1 步（连接复用）暂不排期，记为后续设计项：现在 Jev 调用只剩后台 curator，而且要先确认代理和服务端的空闲连接上限。
  - 详见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 18 节。

## capability 配置缺文件就用 dataclass 默认值（2026-09-28，分支 `claude/9a-capcfg-missing-defaults`，基于 `025573d5e`，已上线 step17a（main de222698b，2026-10-02））

- **问题**：
  - 生产 Gateway 的 agent 根目录是 owner home，那里通常没有 `config/capability_config.yaml`。
  - `capability_config_for_agent` 缺文件时返回 None，调用点各用自己的兜底值：派活链深和每对每小时限额都兜底为 0（不限制），而默认值是 4 和 60，防循环守卫与限流在生产上失效。
  - 另有三处读错了对象：
    - `send_session_message` 读 `agent.capability_config`，这个属性不存在，文件存在也不生效；
    - 决策设置的 `decision_defaults` 退到 `capability_router.config`，那是 AgentConfig；
    - `/settings` 告警从 AgentConfig 上找 capability 配置。
- **规则**：
  - 统一入口在文件不存在时返回 `CapabilityConfig()` 默认实例，且不缓存，事后建文件下一次调用即生效。
  - 读取失败或格式错误仍返回 None；限流和防循环守卫的调用点另写 `... or CapabilityConfig()`，损坏时也落到默认值，不落到“不限制”。
  - 运行时读 capability 配置一律经 `capability_config_for_agent`，不从 `agent.config`、`capability_router.config` 或 agent 的其它属性上找。router 在 `limit=None` 时读 `self.config.capability_candidate_limit` 的写法只加了注释，调用方必须显式传 limit。
- **行为变化**：
  - 提示词内容和工具列表不变：推荐开、选包关、决策与会话开关，缺文件时原本就等于默认值。
  - 只有两道守卫真正生效：派活链深 4、每对会话每小时 60 条。
- **兜底值清理**（`claude/9a-capcfg-fallback-cleanup`）：
  - 已清 6 处：选包判定、子代理包入口开关、流式活动投影、活动提醒阈值、失败自动拆分、看板巡检阈值。
    各处改为 `capability_config_for_agent(...) or CapabilityConfig()` 后直接读字段，唯一权威是 dataclass 默认值。
  - 会话互通三个工具文件（`create_session_task`、`send_session_message`、`list_owner_sessions`）已随会话修复合入改为
    `capability_config_for_agent(...) or CapabilityConfig()` 直接读字段，不再自带兜底值，唯一权威是 dataclass 默认值。

## user_config 的 decision_patch 通道：多带字段时回执写明是哪个（2026-09-28，分支 `claude/be-decision-patch-fix`，基于 `fc494da3f`，已合入 main `a6b3580cd`）

- **起因**：生产上 my-agent 调 Jev 等待时间，`decision_patch` 连续被拒三次，回执都是 `TOOL_INVALID_ARGUMENTS`「决策设置请求包含未知字段」。
- **根因（已复现）**：
  - 合法请求本身一直能用：经真实工具执行器、只改 `background_timeout_seconds` 的 patch 落盘成功。
  - `user_config` 各动作共用一份扁平 schema，`reason`、`fields`、`profile_id`、顶层 `timeout_seconds` 等别的动作的字段都能过 schema。
  - 工具把这些字段原样转给设置服务，服务按操作严格拒收，但报错不说是哪个字段。模型只会改 `changes`，所以无论怎么改都失败。
- **已实现**：
  - 不放宽校验：多带字段仍整笔拒绝、不写入，也不替模型删字段。
  - 设置服务改抛结构化的 `DecisionSettingsUnknownFields`（仍是 `ModelProfileError`，原捕获点不变），带未知字段和本操作接受的字段。
  - 工具回执与 `handler_details` 写明 `unknown_fields`、`allowed_fields`；工具说明写清 patch/reset 各接受哪些字段。
- **已确认**：生产那三次都多带了 `reason`（来源：调用方记录，三次同因）。三次的顶层键都是 action、changes、expected_revision、reason、scope。
  这来自 my-agent-1 作为调用方的调用记录，不是读工具账得到的：audit 只记工具名和状态，不记参数。本修复对任何多带字段都给出同样的结构化回执。
- **已落地（2026-10-01，分支 `worker/ds1-decision-reason`）**：decision_patch / decision_reset 正式接受可选 `reason`（字符串，截断 200 字，
  回执与账本记录标明截断）。触发条件已核实：隔离环境用真实 deepseek-v4-flash 做 4 条自然语言请求，decision_patch 首次调用 3 次里错 2 次，
  都是多带 `reason` 被拒（unknown_fields=reason），第二次才改对；gpt-6-luna 3 次全对；9-28 生产那 3 次错误也全是多带 `reason`。
  reason 只记录与展示（模型档案旁的修改账本，/settings history 一类入口可显示），不参与任何机器判断。**按动作拆分 schema 不做**：
  会改模型可见的工具面，部分 provider 对 oneOf 的支持不稳定，当前风险大于收益，且接受 reason 后模型不再需要删字段重试。
- **候选（未排期，集成者 2026-09-28 定本轮不做）**：按动作拆分 `user_config` 的 schema，或给决策动作单开一个工具。
  - 不做的原因：会改模型可见的工具面，部分 provider 对 `oneOf` 的支持也不稳定，当前风险大于收益（reason 已改为正式接受，见上条）。
  - 触发条件：回执指名之后，模型仍经常在第一次调用就传错字段时再评估。判断依据须是结构化事实（例如工具结果
    `handler_details.decision_request_fields`），统计 `decision_patch` 首次调用因 `unknown_fields` 被拒的比例，不按对话文本判断。
    audit 目前只记工具名和状态、不记参数，真要统计时先确认有可用的结构化记录。
- 细节见 `TESTS.md` 同日条目。

## Jev 后台点位改用独立期限（2026-09-28，分支 `claude/be-jev-bg-deadline`，基于 `60f6f485a`，已合入 main `b52913b29`）

- **起因**：my-agent-1 实测近 48 小时：后台点位 95 次，成功 49%、超时 39%；前台点位 181 次，成功 74%、超时 22%。同一后台阶段里各点位共用阶段倒计时，排在后面的 `curator_relation` 只拿到残值，19 次超时的中位耗时只有 1930ms。
- **语义（已实现）**：
  - 普通后台（`owner_background`）阶段：每个点位从自己的开始时刻起算完整点位预算（点位 `timeout_seconds`，缺省继承 `background_timeout_seconds`），不再受阶段倒计时残值约束。
  - 调用方绝对期限（建阶段时给的，如 Curator 租约派生期限，以及单次调用给的）更小时，仍取更小的。
  - 采用前复核（`decision_outcome_is_current`）同口径：普通后台建议只看自带的点位期限。
  - 前台（`thread`）阶段和实验阶段保留阶段总上限，行为不变。
  - 设置视图里后台点位的 `max_request_seconds`、`limiting_field` 同步为点位自己的预算。
- **取舍**：一个 Curator 批次的决策总等待从“阶段预算”变为“各点位预算之和”，仍受租约派生期限约束。后台等待值（如调到 15 秒）由 my-agent-1 部署后经 `decision_patch` 调整，本次不改任何设置。
- 细节见 `TESTS.md` 同日条目。
- **5→15 秒复测收尾（2026-09-29，只读）**：净效果判不了。
  - 慢时段：改后后台只有 1 个样本。
  - 正常时段：排除样本不足块后 +10.7%，区间跨 0。my-agent-1 脚本给出的 −30.6% 是假象：06:51–11:37 的链路中断时段、
    以及其它没有前台对照的样本不足块，都被算进了正常时段。
  - 直接度量：改后 curator 已发出 94 次，其中耗时超过 5 秒仍成功的 2 次（2.1%）。
  - 复测口径和被审脚本的改法见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 18 节。

## 已结束子代理被接替：终态不改写，只追加接替关系（2026-09-28，分支 `claude/ae-done-takeover-fix`，基于 `53477806d`，已上线 step17a（main de222698b，2026-10-02））

来源：G03 脚本模型端到端验证（9f88e4905）发现，接替一个已 DONE 的子代理时，create_subagents 回执报 `recorded`、也写了 TAKEOVER.md，但持久化边界 `_restore_newer_closed_state` 把 TAKEN_OVER 静默还原成 DONE，takeover_by 为空，回执与权威状态不符。语义由集成方定：
- 终态不改写：已关闭来源（DONE/ABANDONED/CANCELLED）被接替时状态保持，只追加 `SubAgentTask.superseded_by` 与一条 TakeoverRecord；这两项列入已关闭记录的单调追加白名单（`_closed_record_appends`），同状态旧快照也冲不掉。未关闭来源（BLOCKED 等）照旧转 TAKEN_OVER。
- 回执必须反映真实落盘：`recorded` 带 `disposition`（superseded／taken_over）。落账入口 `takeover/record.py` 保存后重读核对，未落盘抛 `TakeoverNotPersistedError`、不写 TAKEOVER.md；回执层再按重读结果给 `recorded` 或 `not_persisted`，后者让创建闸按 `SUBAGENT_REPLACEMENT_RECORD_FAILED` 取消未启动的新 child。
- 防重复接替：接替预检、回执、接管 run 幂等查找统一读 `models.task_replacement_successor`（takeover_by／superseded_by），第二次接替同一来源得 `SUBAGENT_REPLACEMENT_INVALID`／`source_already_taken_over` 并带 disposition。
- 父级视图：kernel 节点新增 `replaced_by`（接替者与处置），经代理树节点进入 list_agents 模型视图与大树预览；HISTORY_INCOMPLETE 的唯一出口（为已有 run 写 `replacement_for_run_ids`）接替 DONE run 保持可用。

未做：
- TUI 子代理名册已标出“已被 X 接替”（2026-09-28，分支 `claude/ae-tui-superseded-marker` 第 2 个提交）：Gateway 名册行摊平 kernel `replaced_by_view` 为 `replaced_by_run_id`／`replaced_by_disposition` 两个标量，经两道 TUI 白名单后在状态标签后标注。进入子代理页后，头部同样标出“已被 X 接替”（第 3 个提交），数据走导航行白名单、导航快照、渲染上下文和渲染缓存键。CLI `subagents board` 按集成方决定不做，因为用户基本不用 CLI；
- 事件日志已补（2026-09-28，分支 `claude/ae-tui-superseded-marker`）：按集成方口径不新增事件类型，`subagent_run_saved` 的 payload 在有值时带上 takeover_by／superseded_by，供时间线、审计投影读取；权威仍是任务记录里的 takeover_records；
- 真实模型下的接替行为未验。

脚本模型端到端复核（BR/HI）已通过，见 CAPABILITY_PACK_ACCEPTANCE 的 G03 节。模块细节见 [子代理结构](docs/modules/subagent/04-structure.md#接替关系的唯一落账入口2026-09-28)。

## 主代理同一失败调用的回合硬上限（2026-09-28，分支 `claude/75-repeat-failure-halt`，已上线 step17a（main de222698b，2026-10-02））

来源：G05 补测中，脚本模型在一次 Goal 续跑回合里重复同一个失败的 `skill_search get`，共 393 个模型轮，宿主没有硬停。根因是既有机制只给提示：同类失败每满 `repeated_failure_halt_threshold`（15）次就追加一次强返工提示，并清掉失败段；清段的同时也清掉了 action guardrail 的同参计数，于是 3N（30 次）同参拒绝永远到不了。

已上线 step17a（main de222698b，2026-10-02）：
- 判定只看结构化事实：主代理（非 `task_local`）同一回合里，工具名、规范化参数摘要（`ToolCall.args_hash`）、归一化 `error_code` 三者都相同，并且连续失败。中间出现任一成功、换参数、换工具或换错误码，都从头计数；同一批里后到的成功会撤销已经写下的收口。
- 阈值复用 `repeated_failure_halt_threshold`（默认 15，≤0 关闭），不新增参数。主代理路径通过 `task_attributes → runtime_guard_policy → runtime_guard_config.yaml` 读取，单测和 fake LLM 都验证了默认 15 生效。
- 命中后只结束当前回合：`runtime_status=unfinished`，`runtime_reason=REPEATED_IDENTICAL_TOOL_FAILURE`，`runtime_source=tool_loop`，`turn_end_reason` 按原协议推为 `interrupted`。任务不收成完成，Goal 状态不变。宿主按结构化字段直接写收口文字（含工具名、错误码、次数），不再追加一次收口模型调用，所以 TUI、IM 和历史回放都能看到原因。
- Goal 不会原样重跑：这个原因不在 `CONTINUABLE_REASONS` 里，前台 finalization 的 `_schedule_typed_unfinished_continuation` 和后台 `goal_continuation_allowed` 都不会自动开下一轮。Goal 保持 active，用户发新消息后才继续；新回合的连续段从空开始。
- 既有的“连续 2 次失败”软提示、同类失败强返工提示和 action guardrail 提示保留；这个硬上限是最后一道闸。

子代理版本（2026-09-28，分支 `claude/75-repeat-failure-halt-subagent`，基于主代理两个提交，已上线 step17a（main de222698b，2026-10-02））：
- 同一 `identical_failure` 模块也对 `task_local` 子代理生效，阈值同样是 `repeated_failure_halt_threshold`。命中后沿授权阶段收口的同一路径结束本轮：`blocked`、`turn_end_reason=blocked`、`runtime_reason=REPEATED_IDENTICAL_TOOL_FAILURE`，宿主自写收口文字，不调用模型；runner 收成 BLOCKED，不会被立即重派。
- 父级可见：finalize 按工具循环的同一口径，从本 run 归档复算末尾的同调用失败段，写入原有 `tool_failure_halt` 事实（schema `subagent-tool-failure-halt.v1`，含原因码、工具、错误码、阶段、次数、参数名，不带参数值）。完成信封、wake metadata、前台活动回合事件和直属孩子行都经同一合同投影保留这份事实；唤醒摘要按 `reason_code` 区分两种说法。原因码与授权阶段收口码一起唯一定义在 `subagents/tool_failure_ledger.py`。
- 优先级：同一次调用先判授权阶段收口。两者同时满足时按授权阶段收口，因为授权不会因重试改变，需要父级调整授权。已经写下的收口不会被另一种原因改写；同批后到的成功各自撤销对应收口。

边界与未做：
- `turn_end_reason` 保持六值协议，没有新增值；工具名和错误码在收口正文和逐调用归档里，没有给 Gateway 结果新增字段。
- 真实模型下的效果没有验证。

## 会话间消息与派活（2026-09-28，分支 `my-agent/self-dev-3`，第一期已实现并已上线）

解决问题：同一 owner 下多会话之间不能传消息、不能派活，管理员只能在一个会话里干完所有事。
第一期只开给管理员（`owner_kind=main`）：同 owner 会话之间可互发消息、可派任务；普通用户两种能力都关闭；
跨 owner 一律拒绝并返回结构化错误码。复用既有底座、不另起第二套状态——message 正文落 `GuidanceStore`
（`target_type="thread"`），唤醒复用 `WakeStore`，回合触发复用 `TurnTrigger` 模式新增一种 kind，
仅 task 的状态与结果新增 `SessionTaskStore`。配置放 `capability_config.yaml`
（`session_messaging_admin_enabled` 默认开、`session_messaging_user_enabled` 默认关、
`session_task_max_chain_depth`、`session_pair_hourly_limit`）。防循环两道守卫：派活链深度、每对会话每小时上限
（0 表示不限制；**消息、派活、任务回报与取消通知都计入配额**，模型发起的发送在投递前判断、
超限返回 `SESSION_TASK_RATE_LIMIT` 且不占配额，宿主自动回报不被拒但占配额）。飞书入口第一期不做，记为缺口。
完整方案、权限矩阵、落地坐标、实现落点与测试计划见
[SESSION_MESSAGING.md](docs/design/SESSION_MESSAGING.md)。状态：**第一期已实现并已上线**
（模型工具、TUI 命令、权威存储、防循环守卫、结果回报与取消都已交付）。

**第一期遗留缺口已补上（2026-09-28，分支 `claude/75-list-owner-sessions`，已上线 step17a（main de222698b，2026-10-02））**：
模型工具 `list_owner_sessions` 只给管理员、跟随两个管理员开关，列出本 owner 会话的结构化 id、状态、最近活动、
渠道、是否当前会话和可用发送类型，不含正文；别的 owner 的会话不列出也不计数。细节见
[SESSION_MESSAGING.md](docs/design/SESSION_MESSAGING.md) 实现落点一节。

**已修：同一对会话只能送达一条消息**（2026-09-29 复审发现；修复在分支 `claude/75-session-message-dedupe`，待 ae 复审；会话互通禁令维持到修好、部署、验收通过）：
- **原因**：`send_session_message` 的去重键是 `session_message:{发送方}->{目标}`，只区分会话对；`append_once` 对同一个键的处理是同文返回旧记录、异文抛 DataCorruptionError。
- **后果**：
  - 第二条不同内容报结果未知，发送方这一轮收口；
  - 与旧消息同文的一条，返回旧 guidance_id，状态写死为 pending，实际送不到。
- **修法方向**：
  - 去重键按单条消息区分，同一次工具调用重试仍去重；
  - 键写进唤醒 metadata，已消费判据按这个键查回执，不再从会话对拼；
  - 工具返回回执的真实状态。
- **门禁**：`test_repeated_messages_between_the_same_pair_are_all_delivered` 先按 strict xfail 标出，修好后转正。
- **落地**：
  - 键由 `session_messaging.session_message_dedupe_key(发送方, 目标, 这次发送的身份)` 唯一生成：模型工具用执行器注入的
    `__operation_id`（同一次调用重试不变），TUI `/tell` 每次命令新生成；执行器没注入 `__operation_id` 时不入队，不退回旧键；
  - 唤醒 metadata 的 `message_dedupe_key`（常量 `SESSION_MESSAGE_KEY_FIELD`）记这条消息的键，唤醒的 `dedupe_key` 也用它，
    同一次调用重试不多发唤醒；已消费判据只按这个键查回执，唤醒里没有键就照常开回合；
  - 工具结果的 `status` 取回执真实状态；门禁那一窗已转正。

**已定并实现：目标回合在消费前被停止，会话消息怎样重新投递**（2026-09-29 真实链路复现；3a 裁定方案 A，dsh-75 在 `claude/75-session-message-dedupe` 实现，待 ae 复审）：
- **现象**：B 忙时收到消息，消息在安全点注入；带着它的那次模型调用返回前 B 被 `/stop`。
  - 回执此时是 `submitted`：模型还没对这次调用作出返回。调用失败后，回执先退回 `reserved`；
  - 前台收尾 `terminalize_gateway_request_file` 的 `reject_pending(reject_reserved=True)` 再把它改成 `rejected`。
  - 插话（Gateway 输入回执）被拒后，对账会把它重新排成普通请求；会话消息没有这一步。
- **当前 main（2026-09-29 75 实测更正）**：回执永久停在 `rejected`，之后没有任何回合认领它；模型还看得到这条消息，只是因为
  被停回合在安全点注入、已写进会话历史的那段输入没有助手回复，成了下一回合请求里"未答复的尾巴"（唤醒回合本回合的输入只有
  "Wake reason: session_message"，并没有认领这条消息）。这靠历史形状碰巧可见：历史被 Compact 或截断、或中间先跑了别的回合，
  它就不再是本回合输入，模型也不知道这是一条待处理的新消息。门禁按"最后一条助手回复之后的非助手消息"取 notes，所以通过。
- **取消线（`my-agent/self-dev-3-cancel-final`，现由 `claude/75-cancel-line-finish` 收尾）的两处问题**：
  - 已消费判据把 `rejected`（以及可逆的 `submitted`）算成已消费，于是跳过这条唤醒，B 空闲时消息送不到；
  - 跳过时只把认领记成 cancelled，没有结案唤醒，每轮 tick 都会再认领一次。
- **裁定方向**（3a，2026-09-29）：
  - 已消费只认 `consumed`；
  - 跳过时复用 `retire_source` 结案唤醒；
  - rejected 会话消息的重新投递方式（重新排队，或终结时对会话消息 release 而不是 reject）在去重改造里定。
- **方案 A（3a 裁定，已实现）**：回合没消费就结束（/stop、报错、崩溃）时，唯一收尾规则
  `store_guidance_recovery.settle_unconsumed_receipt`（`reject_pending` 的所有调用方共用）：
  - 会话消息的 pending/已预留回执释放回 pending，回执级 `migration.released_turn_ids` 记下这一回合，删这一回合的索引；
    下一回合按跨回合例外认领，认领时改绑 `expected_turn_id` 并补索引（`store_guidance._binds_on_claim`），
    注入路径随后用回执里的现行条目（`guidance._entry_as_claimed`），提交与确认才对得上回合；
  - `claim_for_turn` 与 `available_for_turn` 共用同一条"已被释放"判据（`_released_from`）；
  - 释放满 `SESSION_MESSAGE_RELEASE_LIMIT` 次（取毒丸同因上限 5）后再没消费就结束，回执转 rejected，`migration.rejection_code`
    记 `SESSION_MESSAGE_RELEASE_LIMIT_REACHED`（登记在 ERROR_CONTRACTS）；领取后准入把它判为来源已处理完
    （`session_message_abandoned`），经 `retire_source` 结案唤醒，不开空回合；
  - 已提交（submitted，结果未知）不释放；插话照旧 rejected（Gateway 输入对账重新排成请求）；派活正文不释放
    （任务被取消后释放，会让没有归属任务号的前台回合认领到已取消任务的正文）。
  - 与毒丸第 3 步结案不冲突：毒丸结案不改会话消息回执（C4 做法 2，见下），消息的去留只由回执层上限决定；
    上限原因码记在回执 migration，毒丸原因码记在结案记录，各守一处。
  - 代价（3a 暂时接受）：被停回合写进历史的那段输入还在，下一回合同一个请求里模型会看到两次（尾巴一次 + 正式认领一次）。
  - **后台片收尾（ae 复审发现，已实现）**：后台片（定时 run、派活、会话消息唤醒这三种自带精确回合号的片）没有正常结束
    （模型调用失败、取消、中断）时，`runtime._settle_unconsumed_background_turn_input` 对本片回合号走同一条
    `reject_pending(turn_id, reject_reserved=True)`，前台后台共用一套收尾；Compact 公平让出不收尾（同一回合换片续跑，
    预留已退回 pending 等续跑认领）。此前消息唤醒回合认领后失败，消息永远卡在 reserved。
  - **释放按次计数**：回执级 `migration.release_count` 计次，上限按它判；`released_turn_ids` 只做跨回合认领授权。
    同一条唤醒重跑用同一个回合号，按回合去重时反复失败永远到不了上限。
    - **只计会让回合崩溃的失败（3a 2026-09-29 裁定 (a)，已实现）**：回执照常释放，但 `release_count` 只在唤醒毒丸会计数的
      失败时加一，判据复用 `wake_poison.verdict_for_error`（`store_guidance_recovery.failure_counts_toward_release_limit`，两层
      一个权威）。瞬时（超时、429、连接）与环境级（401/402/403/404/407、配置、额度用完）只释放不计次，交给供应退避、车道暂停和
      毒丸的 24 小时提醒；用户 /stop 与取消是中断，不计次；没有异常（正常结束、宿主收尾、恢复）不计次。计次的失败在已计满后
      再发生才转 rejected，不计次的结束永远不会让消息被拒。前台终态（`request_execution._handle_gateway_request` 的收尾）与后台片
      收尾把回合抛出的异常原样交给 `reject_pending(failure=...)`，同一判据。
    - 更正（2026-09-29，下一提交核实）：123f6f3b4 说「同一回合重新认领被释放的消息时不补回合索引，第二次失败时收尾找不到它」，
      不成立。释放时删掉的回合索引，下一次经 `GuidanceStore.receipt()`/`available_for_turn` 读回执时由 `repair_projections`
      按回执里仍然绑定的回合补回；把 `_binds_on_claim` 换回 fc31db60d 的规则后全部用例照样通过。那处改动已在下一提交还原，
      当时「同一回合重新认领不补索引」那个变异被抓住，是因为它同时破坏了跨回合改绑。
  - **派活正文在后台片非取消的失败后退回（3a 裁定，已实现）**：`reject_pending(..., release_task_body=True)` 时派活正文退回 pending、
    按同一个 `release_count` 计次并沿用上限；不记 `released_turn_ids`、不授权跨回合，只有同一个回合号（即同一个 `session_task_id`）
    的重跑能认领，归属任务号仍按 `_body_belongs_to_task` 核对。只有后台收尾会传这个参数，而且按结构化任务状态判定：
    `_session_task_turn_was_cancelled` 为真（任务已取消）时照旧 rejected。前台终态保持默认，派活正文照旧 rejected。
    - **取消路径的撤回（ae 复审 388194636 的 M1，已实现）**：先失败、后取消时，正文已退回 pending，取消只停回合、不碰正文，重试的
      派活片会把已取消任务的正文当本回合输入再注入。`session_task_control._apply_cancel` 在推进到 cancelled 之后，对已绑定回合的
      任务再调一次 `_withdraw_queued_body`，只把 pending 改成 rejected（reserved/submitted/consumed 不动）。三种先后都收得住：
      先退回后取消（这里撤）；取消先于收尾（收尾读到 cancelled 不退回）；收尾夹在停止与推进之间（推进后这里撤，L1）。
    - L2（记账，不改）：`_session_task_turn_was_cancelled` 读任务账出错时返回 False，对「是否退回」来说会按"没取消"退回正文；
      方向是改成三态（取消/没取消/读不出），读不出时按不退回处理。需要读账失败与取消同时发生才会触发。判据统一按任务号读之后
      （2026-09-30），只看这一条任务自己的记录，别的任务文件坏了不再让判定失效，影响面缩小。
    - 派活正文沿用 `SESSION_MESSAGE_RELEASE_LIMIT_REACHED` 这个码名（3a 裁定不改名）：它的含义是"宿主投递释放满上限"，会话消息
      与派活正文共用。
    - **已取消任务的唤醒在开跑前结案（ae 复审 388194636 的 M2，已实现；判据按 R-a 改为按任务号读）**：
      `runtime._retire_cancelled_task_wake` 在解析交付路线、领取后台租约之前，只对派活唤醒按唤醒信封自带的 `session_task_id`
      直接 `session_tasks.load` 读任务状态，`cancelled` 就释放定时租约、结案唤醒；原来要跑完一片才在交付前丢答复，真实模型下
      已取消任务的工具调用已经做完。交付前判定与 run_once 之后的二次检查保留，兜住开跑后才到的取消。
      - **R-a（ae 复审 M2 时发现的既有缺口，已实现）**：M2 最初沿用 `_session_task_turn_was_cancelled`，它按"任务绑在这一片的回合号上"
        反查。排队中就取消（正文已撤回、从未绑定回合）和绑在别的回合上的任务都对不上，派活唤醒照样开一个没有正文的空派活回合，
        把答复交付到目标会话（step16k 上同样复现）。按任务号读覆盖未绑定、绑在自己的派活回合、绑在别处三种情形，也不用扫全部
        会话任务。任务不存在或记录读不出时照常执行，由交付前判定兜底。
      - 只查派活唤醒的前提（3a 认可，ae 核对范围）：定时 run 和会话消息唤醒跑的是自己的回合（回合号是 `scheduler_run_id` /
        `wake_signal_id`），即使这一片曾经认领过某条派活正文、任务因此绑到了它的回合号，这一片也不是那条任务的派活回合，不能因
        那条任务被取消而不开跑；它们照旧由交付前判定兜底。R-b 之后这些回合认领不到派活正文，这种情形只剩之前已绑定的旧数据。
      - **窄窗口（3a 裁定做，2026-09-30 已实现，进 step16m）**：开跑前判定放行之后、第一次认领正文之前到的取消（窗口是开跑到第一次
        模型调用之间的请求组装，线程大时含压缩）。任务还没绑定，取消走"撤队列"，派活回合在安全点认领不到正文，跑成空回合；原来交付前
        判定按绑定反查也对不上，答复照样交付（ae 探针 ra-residual-window 在 R-a 之上复现）。现在 `_session_task_turn_was_cancelled`
        是派活回合唯一的取消判据：按派活唤醒信封自带的 `session_task_id` 直接 `session_tasks.load` 读，不是派活回合返回 False；
        开跑前（`_retire_cancelled_task_wake` 直接调它）、交付前、后台收尾、跑完一片后的唤醒结案都用它。**已知代价**：窗口里那一次
        没有正文的模型调用仍会发生，答复在交付前丢弃、唤醒结案；门禁窗
        `test_task_cancelled_after_its_turn_starts_but_before_claiming_the_body_delivers_nothing` 钉住这个结果。
    - **派活正文只许它自己的派活回合认领（R-b，ae 复审 M2 时发现的既有缺口，已实现）**：`store_guidance.claim_for_turn` 与
      `available_for_turn` 原来只在 `owning_task_id` 非空时核对正文归属，前台、会话消息唤醒、定时回合（`owning_task_id` 为空）
      能认领任何还没绑定的派活正文。目标正忙时，前台回合在安全点领走正文，任务绑到前台请求上：前台回合不按派活收口，任务一直
      accepted、发送方收不到回报；派活唤醒随后还会再开一个没有正文的空派活回合（step16k 上同样复现）。现在派活正文
      （`origin_kind=session_task`、带 `session_task_id`、不带 `SESSION_TASK_STATUS_FIELD`）只许 `owning_task_id` 等于它任务号的
      回合认领，空的也不行；认领和"有没有待处理输入"两处判据一致，不会一边说有输入一边认领不到而空转。目标正忙时任务留在队列，
      前台结束后由派活唤醒开派活回合、收口、回报。派活回报带 `SESSION_TASK_STATUS_FIELD`（常量收在 `session_messaging`，
      回报写入与认领判定共用），不受影响。
      - 回报的释放语义（待定，放到 C4 或之后，3a 裁定记账）：派活回报和正文共用 `origin_kind=session_task`，后台片非取消的失败时，
        认领到的回报和正文一样退回 pending、只许同一回合号重跑认领（388194636 之后；以前是 rejected，送不到）。回报本质是发给
        发送方的消息，更合适的语义是按会话消息处理：释放时授权跨回合、跨回合认领时在 `_binds_on_claim` 改绑，这样唤醒被结案或
        前台回合失败时，发送方的下一回合还能收到。
    - 满上限转 rejected 后派活唤醒仍会没有正文地重跑：**由 C4 收口，已实现**（3a 裁定）。领取后准入对派活正文已放弃（rejected 带
      `SESSION_MESSAGE_RELEASE_LIMIT_REACHED`）判为来源已放弃，走 `wake_domain_closeout.py` 同一个领域收尾：任务收成 failed 带码、
      回报发送方、结案唤醒；判据写法与会话消息的 `session_message_abandoned` 同一套。
  - **收尾只在调用抛异常时做**（ae 复审确认）：认领只发生在紧接着那次模型调用的请求组装里，认领和调用之间的退出都会抛异常；
    模型返回后才抛错时回执已是 consumed。不在正常结束时收尾：派活回合会跨好几片共用同一个回合号，正常结束一片就收尾会把
    发给进行中任务的插话提前拒掉。
  - **与毒丸第 3 步的两层关系**：
    - 回执层（本条）：每条会话消息自己计数，只计会让回合崩溃的失败（与毒丸同一判据），计数和原因码记在回执 migration；
    - 唤醒层（毒丸，`WAKE_POISON_PILL.md`）：按唤醒的尝试账分原因计数，计数类同因满 5 次隔离唤醒，瞬时/环境类不计数、只退避并在
      停滞时告警，记录在尝试账和结案记录；
    - 两层独立计数、各守一处，同一判据（裁定 (a)）。会让回合崩溃的失败由毒丸先收口唤醒：同一条消息唤醒每次这类失败两层各加一，
      第 5 次失败时毒丸隔离唤醒；这时回执层才释放第 5 次、回执仍 pending，C4 的领域收尾不改它（做法 2，取代原裁定 b），消息留给
      目标的下一次回合，前台回合也崩溃时（第 6 次）回执层按自己的上限转 rejected 带码。step16l（毒丸没接线）只有回执层。
    - 两层同一原则：计数只看会让回合崩溃的失败，瞬时和环境故障不丢消息（原先"连续 6 次瞬时失败后消息被放弃"的代价已由
      裁定 (a) 消除）。回执层另外兜住前台回合反复崩溃的情况（那不经过唤醒车道）。
  - **进程在后台片认领后、结束前被杀（3a 裁定并入毒丸 C6）**：没有异常路径，预留仍卡住（后台没有前台那种请求租约失效后的
    `release_reserved`）。由 C6 收口：preflight 识别出上次在途属于已死进程时，对那次尝试的回合号走同一条
    `reject_pending(turn_id, reject_reserved=True)`（派活正文按任务状态决定是否退回）。C3 在尝试账的在途记录里持久化回合号
    （`WakeAttemptStart.turn_id` 写进 `in_flight`，与 `_run_params` 注入时的回合号同源），C6 直接读它。
- **条件 1 核查（残留去重只能有一套机制）**：现有机制是注入的 UserTurn 带 `input_ids`，`release_reserved_turn_input_after_attempt`
  释放后由 `exclude_active_turn_user_input_ids` 从续跑携带里排除——只覆盖同一次运行内的 Compact/溢出续跑。插话被停后
  重新排成请求、会话消息释放后重新认领，都不会把被停回合写进历史的那段输入排除掉。所以记成通用后续项，这次不做：
  **"被停回合写进历史的输入，重新投递时如何避免重复出现"**（插话、会话消息共用，将来沿 `input_ids` 这一套做，不另写）。
- **顺带核实的现有问题（通用后续项，未排期）**：插话被 `release_unclaimed` 释放后跨回合认领，认领时不改绑，
  存储层的提交（`guidance submission reservation mismatch`）与确认（`guidance provider ack turn mismatch`）都会报错
  （2026-09-29 存储层探针实测；生产上插话的 target 是旧回合的 task 邮箱，能否走到跨回合认领未核实）。会话消息已按上面的
  改绑规则处理；插话若也要跨回合认领，应复用同一条改绑规则。
- **门禁**：
  - `test_busy_target_stopped_before_consuming_the_message_still_gets_it`：main 上通过，取消线上失败，作为取消线的验收门；
  - `test_busy_target_consumes_message_without_an_extra_empty_turn` 补断言「B 没有待处理唤醒」。

**待定，非阻断：交付前的取消判定每片全量扫会话任务**（2026-09-29，ae 复审取消线时提出，随 step16k 上线的是现状；新想法只记账，暂不改）：
- **现状**：
  - `runtime._session_task_turn_was_cancelled` 用 `session_tasks.list_report(limit=0)` 全量读取会话任务，找 `conversation_request_id` 等于本片回合号的那条，看它是否已 `cancelled`。
  - 两处调用：
    - 交付前（`BackgroundMainAgentRuntime`）：每个带回合号的后台片都会调，包括定时任务 run、派活片和消息唤醒片；
    - `_execute_wake_signal`：本片报告为 None 时再调一次。
  - `list_report` 不论 limit 多少，都会先 glob 并解析目录下全部任务文件，limit 只截取返回值。所以每次调用是 O(任务数)，并且会话任务目前没有 retention。
  - 控制面 `control_service._bound_background_turn_is_running` 同样用 `limit=0`：原来默认只截取最新 100 条，任务超过 100 条时，较早创建、仍在跑的那条会被截掉。改成全量不增加 I/O。
- **为什么语义上需要扫描**：会话任务的正文由目标会话在安全点认领时绑定到当前回合，这个回合可能是派活片，也可能是定时任务 run、消息唤醒片或前台请求。所以判定「本片对应的任务是否已取消」时，不能只按唤醒信封里的 `session_task_id` 读一条，必须按 `conversation_request_id` 反查。
- **规模**：09-29 生产 owner 的会话任务文件数为 0（会话互通在禁令下），当前可以忽略；解除禁令后按使用量线性增长。
- **方向**（三选一或组合）：
  - 有 `session_task_id` 的片先 `load(task_id)`，命中且绑定一致就直接判定；其它回合类型再扫；
  - 建 `conversation_request_id → task_id` 索引，绑定回合时同步写；
  - 给会话任务加 retention，终态后按期归档。
- **状态**：待定，非阻断；解除禁令前后择机排期。
- **已解决（2026-09-30，随窄窗口提交，进 step16m）**：R-b 之后派活正文只由它自己的派活回合认领，任务只会绑到派活回合（回合号 =
  任务号），"必须按绑定反查"的前提不再成立。`_session_task_turn_was_cancelled` 改为按派活唤醒信封自带的 `session_task_id` 直接
  `load` 一条，O(1)；不是派活回合（定时 run、会话消息唤醒）直接返回 False，不再扫描。控制面 `_bound_background_turn_is_running`
  的全量读取不在这次范围里，照旧。

## list_agents 显式 run_id 的范围裁决（2026-09-28，分支 `my-agent/self-dev-4`，已上线 step17a（main de222698b，2026-10-02））

显式传入超出当前 owner 可见范围的 run_id 时，`list_agents` 原先只返回 `nodes=[]`/`root_id=""`，并把请求的 id 回显成 `effective` 范围，调用方无法区分"这个 id 不存在"和"它不属于你的可见范围"。规则：显式 run_id 在整棵可见树里没有匹配行（且不是 main run、当前没有子 runner 身份）时，查询折成 `root_tree`，`effective` 不保留该 id，并在既有 `ScopeResolution` 上追加唯一裁决码 `requested_run_id_not_in_visible_scope`；`scope_warnings` 恒为列表（无告警时空列表），模型视图转发该顶层字段。两种原因共用同一分支、同一个码和同一响应形状，因此答复不泄露目标是否存在；合法查询行为不变。同类静默问题（`task_progress` 显式 run_id 静默换账本、`cancel_subagents` 解析空列表不说明原因）按同一码语义收口，已转由 my-agent-2 处理。验证见 TESTS。

## 能力配置的实际生效位置与随包模板一致性（2026-09-28，分支 `claude/9a-lark-and-capcfg`，基于 `3d76ac687`，已上线 step17a（main de222698b，2026-10-02））

- **生效位置**：
  - Gateway 和本地会话按 agent 根目录找配置：先找 `agent_py_agent/config/capability_config.yaml`，再找 `config/capability_config.yaml`。
  - agent 根目录是显式工作区；没有指定时就是所配 owner 的 home，local/main 即 `<MY_AGENT_HOME>/owners/local/main/config/capability_config.yaml`。
  - Gateway 读到的这一份被它服务的所有 owner 共用；找不到文件时按 `CapabilityConfig` 默认值运行。
  - daemon、gateway scenario 和 subagents 系列 CLI 子命令默认直接读随包文件，可用 `--capability-config` 覆盖。
  - G07 验收时，开关文件放在 Gateway 配置目录下没有生效，原因就在这里。
- **随包模板**：
  - `agent_py_agent/config/capability_config.yaml` 是默认模板。测试锁定：模板里每个键都被 `CapabilityConfig` 认识，值等于默认值，加载无告警。
  - 本次补上 5 个在用、但模板里缺失的键：2 个下发上限，3 个子代理巡检阈值。
  - 移除不生效的 `enable_capability_routing`：能力申请链路本来就不受它控制。
- **删除死字段**（同分支第三个提交）：
  - 删掉 12 个运行时没有效果的字段：10 个没有读取方；`enable_capability_routing` 不控制能力申请链路；`subagent_min_evidence_for_done` 只被透传进巡检快照，从未使用。
  - 用户配置里残留这些键只告警、照常加载。
  - 测试进一步锁定：dataclass 的每个字段都必须写进模板。不用的字段直接删，不在模板外保留。
- **不变**：读取逻辑和路径解析都不变；Gateway 找不到文件时的行为也不变。
## 前端 import 链恢复（2026-09-28，分支 `claude/9a-frontend-runtimeconfig`，基于 `3e23d2da8`，已上线 step17a（main de222698b，2026-10-02））

`frontend/src/data/runtimeConfig.ts`、`mockConfig.ts` 在 2026-08-15 建独立仓库（`0b6252590`）时被误删、引用方仍在用，按原结构补回最小版本：runtimeConfig 的类型改由 `frontend-runtime-config.json` 推导、不再手写字段清单，mockConfig 只从生成的配置目录派生；设置页表单项清理已在 main（`3af7c94df`），JSON 与 store 里对应已删后端键的旧字段等能跑 tsc 类型检查时再清（见 ROADMAP）。验证方式见 TESTS。

## 能力包最终固定测量与恢复稳定性（2026-09-28，执行完成、验收未全过）

十二步骤收口审计保持原范围：固定 27 次业务 16/27、原资源执行 12/18，三次完整错误回执后的自建程序属于模型恢复质量限制，未确认新宿主缺陷；不实施未验证的冲突字段候选。七组未覆盖或部分覆盖范围完整列入验收记录，不以最新四项摘要掩盖旧缺项，不因本轮不运行而豁免。C22 的 65536 只证明机制；历史 77 个大窗口检查点中 43 个压缩前估算达到 128K，17 个手动、60 个触发未知，缺当前包 pins 与后继原资源执行。Mac 两版记录已核、Linux 两版未部署，容器源码测试不代替发布；Goal active。建议下一步由 Claude 集成本文档补记，只读可并行，本轮不新增真实模型或 owner 用例；详见[收口审计与完整缺项](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#收口审计与七组未覆盖范围2026-09-28)。

## TaskRun 收口允许“静止但未终态”的子 run（2026-09-28，分支 `claude/38-taskrun-settle`，基于 `d0318486e`，已上线 step17a（main de222698b，2026-10-02））

**问题**（dsh-be 分析，集成方认可）：子代理以 BLOCKED / unfinished 等非终态 runtime_status 结束时，设计上只关闭 attempt、删掉执行锁，`agent_runs.status` 停在 created（展示面读 canonical，不受影响）。`settle_task_run_if_agent_tree_terminal` 要求树上所有 AgentRun 终态，父 TaskRun 因此永远 `closed_at=0`、不写 `task_run.closed`；两个调用方（`runtime_mixin._settle_terminal_conversation_task_run`、`owner_wake_discovery._reconcile_terminal_conversation_task_runs`）忽略返回值，`open_task_runs` 只增不减，发现扫描每轮对这些行白跑一次。

**规则**（只改这一处判定，`runtime_db/repository._quiescent_nonterminal_agent_runs`）：非根 run 满足任一即算已静止——状态终态；或 current attempt 已结算（`_QUIESCENT_ATTEMPT_STATUSES` = attempt 终态集去掉 unknown，即 done/failed/cancelled/recovered）且执行锁 `attempt-exec:{agent_run_id}` 已不在。unknown 是“结果不明”，必须等显式恢复或结算，锁没了也不算（集成方定：否则 `task_run.closed` 会盖过没有定论的子结果，违背“状态别名不隐式兼容”）；recovered 已由恢复协议结算过，可以算。根 run 仍必须终态（TaskRun 状态取根）。没有任何 attempt 的 run 证明不了静止，保持开放。只读 runtime 自己的表，不读 canonical。`task_run.closed` payload 新增 `quiescent_agent_run_count` / `quiescent_agent_run_ids` 作为结构化证据。判定可逆：静止子 run 之后 `create_attempt` 仍经 `_reopen_task_run_for_attempt_conn` 写 `task_run.reopened`。

**顺手修正**：`_activate_pending_attempt_conn` 的 `agent_run.started` 事件原来写 attempt 的 `running`，与 `agent_runs` 列不一致；现按各自的列写（attempt 事件 pending→running，run 事件 →created）。产品侧没有消费方读这个字段。

回归与变异见 `TESTS.md` 同日一节；结构说明见 [Gateway 结构](docs/modules/gateway/04-structure.md)“ConversationTaskLink 与 TaskRun 收口”。

## C23宿主续接与资料消费分层（2026-09-28，机制分项已验）

固定330b原生三助手已证明一次原派工、结构化宿主唤醒、同代子包pins、父真实保存及终态收口。根Task自动选择为空，实际子授权来自父创建参数；不能把两种来源混记。脚本归档页完整与模型实际视图完整也分别判断：不完整preview已有原归档恢复指引，未恢复即不能报全文采用。交接错误不升级为新的领域硬门，尚未确认新宿主缺陷。正式315仅产品常量同值改名、作者全仓及静态gate已核；运行与发布版本分别记账。下一步固定最终运行字节和原27次矩阵，只读复核可并行，产品／发布仍归Claude。见[证据与边界](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c23三助手后台续接与交接2026-09-28)。

## 生命周期事件来源与携带记录（2026-09-28，固定修订复核）

固定 A 草稿 `51361723d` 使用结构化 TurnTrigger 区分生命周期事件与用户轮；合法 Goal 的持久 task ID 仍需和真实历史 request 引用区分。旧目标解析明确允许该 task ID 没有 user 消息，新事件不能仅因 ID 非空就声称来源在历史中。修订应复用已验证 Goal/Task 来源，保持原 wake 与工具账过滤身份，不新增账本或按正文猜角色。两线审阅、224 项组件和隔离解析证据见[验收边界](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#后台唤醒-a-草稿来源角色审阅2026-09-28未正式交付)；产品仍归原作者，root 负责后续固定组合与私有验收。

后续固定 A `c4972cbd0` 已按已验证 Goal 来源修正该问题，独立 18 项组件通过。B `9882db061` 已将可读记录与来源不完整事实分开，其参数真值与创建语义不一致问题也已在 `24f8accb9` 通过复用 item 合并及统一 ID 归一化修正。同一参数沿现有创建口径得出唯一有效值，来源资格仍由原接替预检判断。独立窄审及六文件 272 项组件通过，整组最终门禁和原生验收待完成；产品仍归原作者，见[固定修订复核](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#后台唤醒修订复核2026-09-28组合待完成)。

## C22 连续压缩与交接责任分层（2026-09-28，机制分项已验）

解决问题：核实固定校准修复后，跨两代自动压缩仍能取得同代原资源并真实执行。C22 在既有 65536 profile 同请求提交四代，第二代后的 get、完整 source_ref 复制、原程序执行及 A/B pins、输入保持均已验；第四代后只有最终答复，默认窗口长任务未覆盖。业务交接的能力说明、字段映射和错误归因未过，保持失败原件，不据此增加专项合同或第二模型循环。C21 的 source_ref 与 closeout 只读审计也未确认新宿主缺陷，详见[验收分层](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c22固定校准后的连续压缩与原资源执行2026-09-28)。

Goal 工具已核为 active，最终 0/27。下一步按原 conversation_request_id 核 Claude 唤醒补片后的委派事实及父级交付；产品保持原作者唯一写入，root 负责私有验证和证据，独立只读审阅可并行。以下旧状态保留原时点。

后续唤醒 B 预审：固定双索引 reader 在 owner 读取异常时被外层 catch 整体变为空 carry；离线合成复核确认可读 task 事实也丢失。真实空账与读取失败必须按原结构化异常链区分，不能因未知历史默认为尚无副作用；本线只提证据，修复仍归 Claude。C 正常工具目录未见确定缺陷，A 未固定、C23 未提交，详见[预审范围](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#后台唤醒前置补片审阅2026-09-28整组未交付)。

## 固定校准组合与 C21 真实采用（2026-09-28，分项已验，整体未过）

解决问题：把容量口径修复、真实自动恢复、能力包身份以及业务交付分别核实。固定 `8ef68c5fd` 的独立 135 项和私有安装已通过；C21 工具上下文溢出触发第 1 代 102708→39581，B pin 保持，之后继续 19 次工具调用。连续两代和压缩后包 get 未覆盖；B 原脚本被改写、输入损坏及交接映射失败，不能按模型自建 11/11 测试改判业务。没有新增产品合同或领域硬门，详见[本轮范围](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c21固定8ef的离线交接开发验证2026-09-28)。

后续委派验收增加父级生命周期唤醒无重复派工的结构化观察，须等 Claude 原作者的固定补片；按原请求对应的 delegation 和 TOOL_ONE_SHOT 事实，不依据模型说“已派过”。本线不修改 conversation/runtime.py，设计实施与发布保持原作者唯一写入。旧“校准待交付”“C21 未运行”段落保留原时点；当前 Goal 工具 blocked，协作继续，最终 0/27。

## Compact 候选接受门与预检同一校准口径（2026-09-28，分支 `claude/38-compact-calibration`，基于 `59fdcabbf`，已上线 step17a（main de222698b，2026-10-02））

**根因（压缩异常②）**：真机单回合多次 `read_file` 后，预检按供应商观测校准过的可见上下文越过触发线（本地估算比供应商实际高约 43%：242,207 对 169,217），恢复压缩却按未校准的本地投影量候选，候选被 `COMPACT_CANDIDATE_TOO_LARGE` 拒掉；压缩开始时 `_gateway_compact_progress_callback` 又把未校准的“压缩前”写成线程 `model_context_usage` 快照。两条链说的不是同一种数。

**规则**：
- 唯一校准口径在 `conversation/compact_calibration.py`（纯函数，不读宿主、不写状态）：请求不小于观测请求时与预检完全一致（本轮作用域 `provider + 追加增量`，耐久作用域取比例折算与增量口径的较大者）；请求比观测请求小时按观测比例折算、下限 50%（预检的本轮口径对压小后的请求原样返回，候选门不能直接复用它，Codex 审阅第 1 点）。没有观测就是原始值，不猜系数。
- 观测在宿主边界冻结一次：`PreparedCompactRecovery.select` 冻结完整请求后按同一表面指纹（`context_pressure.request_projection_surface_fingerprint`，与预检同源）取本轮/耐久观测（`frozen_compact_request_calibration`：同 fingerprint，耐久观测须同代次），冻结成 `CompactRequestCalibration` 交给自动判定、压缩前计量和两条接受门；纯投影不做水合（Codex 第 3 点）。表面不符、代次不符、缺观测都保持原始口径。
- 两条接受门都用折算值与输入上限比较：transcript 的 `_measure_candidate`/`_RejectedCandidates`（候选、压缩前、失败诊断的固定开销都折算）和活动回合的 `_project_active_turn_request`/`_apply_request_calibration`；`ConversationCompactProjection.projected_tokens` 仍是原始纯投影。恰好等于上限仍拒绝，输出预留照旧生效。
- **旧观测失效语义的显式改动**（Codex 第 2 点）：提交恢复候选后不再一律清掉本轮观测。有冻结校准时，`rebase_provider_context_observation` 把已接受候选（原始投影 → 折算值）改写成本轮基线（同 fingerprint，带 `basis=compact_candidate`、`derived_from_generation` 结构化标记，只存内存、不持久化），同轮下一次真实预检按 `折算值 + 追加增量` 算，与接受基准一致，不会刚接受就按原始口径再压一次；没有校准的提交仍 `invalidate_provider_context_observation`。线程上的耐久观测仍由提交 CAS 清掉，新进程在下一次真实响应前按原始口径。
- 线程快照：压缩开始事件的 `before_tokens` 就是折算后的压缩前大小，失败路径留下的 `model_context_usage` 不再是原始估算；无观测时两者本来就相同。
- `conversation_compaction_progress` 每个事件（含 started）带结构化 `trigger_source`：`preflight`（发送前按可见上下文或完整请求判定越线，含各宿主首次准备的自动阈值预检）、`provider_error`（供应商已报溢出）、`tool_context_overflow`（工具上下文窗口裁剪器登记溢出，`preflight_context_pressure_response` 的 `runtime_source` 随之从 `preflight` 改为独立值，`finalization_compact_auto` 词表同步）。三宿主都从溢出结果的 `runtime_source` 透传（Gateway 经 `RunParams.compact_trigger_source`）；白名单在 `compact_progress.COMPACT_TRIGGER_SOURCES`，手动或旧事件没有该字段。
- 历史数据注意：2026-09-26 `_full_request_tokens` 修复之前写入的 v3 checkpoint，其 `projected_tokens_before` 不可信（解绑历史后只量到系统提示与工具，例如 35,915 对实际约 31.3 万），不能拿它证明“压缩前更小/更大”；真实大小以同期 Gateway 请求记录的供应商 usage 或 `provider_context_observation` 为准。

**不在范围**：after ≥ before 不告警（集成方决定）；派生基线不持久化到线程；`/compact` 手动路径与阈值以外的旧事件不带 `trigger_source`。回归见 `test_compact_calibrated_candidate_gate.py`（含隔离 home 假 LLM 两回合复现），详见 [会话上下文设计](docs/design/CONVERSATION_CONTEXT_DESIGN.md#压缩候选与预检同一校准口径2026-09-28分支-claude38-compact-calibration)。

## TUI插话：失败调用退回重提交、后台目标按任务终态收尾（2026-09-28，分支`claude/be-steer-loss`，已上线 step17a（main de222698b，2026-10-02））

**根因**：生产结构化记录显示，09-27 22:54 一条插话随模型调用提交，这次调用随即 `ProviderTransientError`。旧规则里，两层重试都因为“有未确认插话”不重发，attempt 失败；新 attempt 又不重发“提交不明”的插话。入口回执因此永远停在 `active_pending`，TUI 已轮询上千次。定时任务目标 `srun_*` 没有 Gateway 请求文件，回合生命周期永远判成未知，所以回合结束后插话也从不收尾。

**规则调整**：原来的规则是“网络一开始，插话最多发一次”。现在只对“结果不明”（在途，或进程崩溃）的提交保留这条。调用明确失败时（抛异常，回复未被采纳），插话按本次调用编号退回本 attempt 的预留，同一 attempt 的重试用新调用编号重新提交。理由：后端不存服务端会话（`store: False`），插话只在确认时写进历史，失败调用在模型的持久上下文里不留痕迹。重试守卫只看在途提交。

**后台目标收尾**：没有请求文件的目标，按会话任务链接的规范终态判断。

**不在范围**：attempt 失败时释放预留。因为失败回合的原生历史会原样回放，同时释放会让模型看到两次；真要做，得和压缩续跑一样，把已释放的插话从部分历史里剔除。本次不做，重试全部失败时，仍由回合终态把插话拒绝并排到下一轮。进程崩溃留下的提交不明插话仍不自动重发；入口回执收成 `terminal_unknown` 后，TUI 停止对账并显示“未获模型确认”终态（分支 `claude/be-steer-terminal`）。

结构见 [Gateway 结构](docs/modules/gateway/04-structure.md)，回归见 `test_steer_delivery_recovery.py`。

**被放弃调用的迟到线程（2026-09-29，分支 `claude/38-abandoned-call-steer-race`，已上线 step17a（main de222698b，2026-10-02））**：墙钟超时或用户停止让 guard 放弃一次物理调用后，它的工作线程可能才走到发出前登记，把插话提交到已作废的调用编号上，重试提交随即撞上账本的 `guidance submission was not reserved`。规则：每次物理调用带一份 liveness；guard 放弃时置位，工作线程在同一把锁内复核“仍是当前调用”后才做发出前登记（子代理业务标记、发送前钩子、插话提交）并发请求；已放弃则三步都不做、不发请求，只在模型调用账本记 `submission_skipped_after_abandon` 事件并以 `ModelCallAbandonedError` 结束该线程。放弃后的退回与重试判据不变；state 必须带 liveness（测试替身也要带），没有缺字段兜底；被放弃的调用不记成失败的 LLM 调用；放弃标记会等一次有界的进行中登记做完，这点延迟是正确性所需。回归见 `test_abandoned_call_steer_race.py`。

## Compact 校准与计量收口（2026-09-28，方案审阅，待原作者实现验收）

## Compact 校准与计量收口（2026-09-28，计量已定向验证，校准待交付）

解决问题：候选接受与下一次实际发送的容量判据须保持一致，失败诊断不能把未知写成零或漏掉仍会发送的材料。Claude 将计量补片与接受口径修复依次集成后交固定版本；本线只读复现现有边界，不修改 Compact 产品。校准观察应在宿主边界冻结，纯投影不引入状态水合；旧观察在 Compact 提交后失效的生命周期，以及缺观察/不同请求表面时的原始估算，必须与新接受门一起验证。详见[证据和待验范围](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#compact-计量与校准待修复边界2026-09-28)。

固定计量提交 `b6dafe22a` 的隔离源码副本已独立通过 6 文件 85 项；只计量入口与实际保留 IR 口径的两处遗漏已在定向范围关闭，校准及固定组合仍待验。历史导出 v2 只确认主 owner 的 17 次手动与 60 次触发来源未知；原 `forced` 字段不作自动触发证明，见[补证边界](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#历史-compact-证据复核2026-09-28)。

## 长任务采用真实开发负载（2026-09-28，验证已准备，未运行）

C21 用小型明确输入驱动离线交接 CLI 的实际开发，复用 A/B 原检查资源；编号表、摘要表及 CLI 都只是测试工作区业务数据/产物，不扩展宿主合同或包格式。持续压缩只能由原 131072 会话阈值自然触发，不能以填充材料、手动压缩或追加提示制造通过；业务完成和跨代包身份/资源执行分别判断。固定计量修复仍由 Claude 交付，本线未写 Compact 产品。详见[验证准备](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c21离线交接工具开发验证准备2026-09-28未提交)。

## C19委派与完成边界核定（2026-09-27，核定完成，无新增运行规则）

原始派工省略第三子的包授权，实际子提示和失败回执已保留原申请入口；没有确认宿主丢授权或申请工具被隐藏。三个结构化子交付文件存在，缺失的是父最终文字额外声称的文件。沿既有通用合同保留模型执行/报告失败，不自动从角色、goal或最终正文生成授权及交付清单，不恢复主会话推断式完成门。既有收尾合同8项通过；无新产品修改、配置或模型循环。真实完整交付仍未过，Compact由Claude继续独占，详见[责任核定](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c19责任核定2026-09-27未确认新的宿主缺陷)。

## 未声明包成员沿原参数纠错恢复（2026-09-27，本地组件通过，真实待验）

C18把猜错成员路径误归快照失效，原恢复因此要求重新授权。现仅在原受限包与预期代次核验之后，以同一`package.resolve`判定声明成员；未命中沿既有`TOOL_INVALID_ARGUMENTS/repair_tool_arguments`返回失败及同代入口`next_read`。建议须由模型再次显式调用，不自动读取、pin、授权或新增循环。入口文件名来自声明，未知包、旧代及真实成员读取失败仍走原错误；取消和中断继续传播，失败get的原任务晋升策略保持。6文件119项、独立窄审及本地严格gate通过，原生C18失败保留；详见[读取合同](docs/design/CAPABILITY_PACKS.md#发现作用域和按需读取)和[本轮验证](TESTS.md#c18包成员参数纠错2026-09-27)。

## 显式包申请沿原直属父级裁决（2026-09-27，已上线 step17a，main de222698b，2026-10-02）

自动语义路由会把合法包申请提前记GAP，混合工具申请还会局部授予后结清整条请求。现沿既有包命名域和PARENT_RESOLUTION_REQUIRED复用父级裁决链：owner路径判断保持在先，含capability:引用的申请整条保留OPEN；前缀不证明可授予，原resolve仍从直属父级当前快照解析完整七字段引用。未知包批准失败后仍可拒绝收口，不自动补裸包名前缀，不增加状态账或首请求资格。5例旧4红1绿、修后全绿，7文件111项通过；真实采用待验，见[当前验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c17显式包申请回到原父级裁决2026-09-27)。

## 子代理资料引用保持软线索（2026-09-27，已上线 step17a，main de222698b，2026-10-02）

G05首次提示准备被长input_refs的路径查询异常中断。修复复用原read_refs/unresolved合同：每个候选exists/resolve的OSError仅影响该候选，InterruptedError继续传播，其它根和引用正常查找；不截短引用、不从说明猜路径、不增加授权或状态。参考仅核本地Hermes逐引用异常隔离，未引入其解析器。65项定向及独立窄审通过，原生失败和包申请自动路由缺口分别保留，见[当前验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c17并发资料交接与输入引用修复2026-09-27)。

## 工具说明按调用方实际路径范围给出（2026-09-27，分支 `claude/9b-read-file-scope`，已上线 step17a（main de222698b，2026-10-02））

read_file / list_files 原说明“可直接读任意绝对路径……无需额外授权”对有 owner 墙的调用方不成立（所有子代理、远程/IM owner、非 Full Access 的本机主代理），87cf12677 现场的子代理据此反复重试墙外路径。现在说明末句按本 run 的读取范围三选一：有墙时写明只能读自己的数据目录、shared 公共区和本任务明确授权的外部工作目录，其它路径在授权阶段被拒并返回 PATH_OWNER_SCOPE_BLOCKED 等 PATH_*_BLOCKED 错误码；无墙 full 保持原句；无墙 normal 在原句后补危险目录与凭据文件的例外。三句与 `PathAccessPolicy.check` 一一对应，由测试钉住。

有效 owner 墙只来自 `path_access_policy.effective_owner_scope_root`（子代理/控制面的墙 > agent 当前工具视图的墙 > 写边界已冻结的值 > 执行注册表的墙）：写边界合并、执行门、快照冻结都调用它，旧的私有 `_effective_owner_scope` 已删；子代理读取预检和包入口读取判定也改用它。说明在 `loop_support._tool_snapshots_for_run` 冻结快照时选定——Full Access 父代理的 task_local 子代理与父代理共用一张没有墙的注册表，墙是按 run 施加的，所以不能在工具构造时定；子代理 compact 缓存面走同一冻结点。`schema_hash` 只含 input_schema、`snapshot_hash` 只含名字/schema_hash/可用性/可见性，都不含顶层说明，二者不变。

**不加开关的归类理由**：这是更正一句对有墙调用方不成立的说明，文本由执行门同一个结构化事实决定，属于 AGENTS.md 里“纯 bug fix、文案修正”一类，不带来新行为、联网、写盘或后台任务；再留一条发错说明的旧路径违背“确定不用的旧路径要删”。

## 能力包资料纠错沿显式版本发布（2026-09-27，B0.1.4私有更新已验）

已确认B原方法把镜头自身ID与所属场次外键写得含混，现只澄清场次和镜头对象各自映射、每个目标镜头只引用目标场次，并沿既有object_mappings表达一对多/多对一。以B0.1.4候选记录两份内容资源的改变，原检查器、schema、模板、示例与许可字节保持；不加入转换器或专项宿主逻辑。56项已有组件及可重复构建通过，11资源与源码一致；随后同一私有Gateway的原生TUI完成热更新，281旧文件、127旧任务版本记录、6配置及其余两包保持。新版本模型采用尚未验证；G04其他字段/限制错误和65k失败继续保留，不能把资料纠错称为全链质量修复。详见[版本来源](examples/capability-packages/drama-workflow-b/PROVENANCE.md#本包-014-修订)和[验证记录](TESTS.md#b014字段映射资料纠正2026-09-27)。

## 通用能力包组合发布与剩余验收（2026-09-27，已发布，真实验收继续）

固定`51dac1b81`已包含能力包与主线补片，Claude完成双机step13w部署；本线核对12分片23639项通过、0失败及静态严格gate，并核对发布日志与本机发行清单，未另做远端在线探测。私有环境也已精确切换51dac，1430个包成员与固定源码及安装一致。第二代自动Compact容量、压缩后原资源执行、普通任务开销边界及27次冻结非安全验收仍未收口。代码发布不等于完整功能收口，各候选旧失败和领域质量差异继续保留，Goal保持active。

私有环境停稳备份后仅移除4个已删除且未设值的配置键，其余13291个停止态文件保持。新原生G02自动第1代77724→47484提交后取得同代6份资源，第2代候选51342超过输入上限49152，需求整体失败、未执行原checker；摘要单独计量1091、工具保留记录0，不足以证明固定开销或重复投影的具体成因，Claude已确认此证据边界。普通G03空选与无私有包采用通过，一次辅助选择1132 tokens；答复漏两位负责人，业务质量失败。目录独占token未采集，最终27次仍未启动。已删键告警及Compact分项计量由Claude另线实施；本线不临时吸收未验证补片。

默认262144窗口G04已终态：包资源读取、source_ref同字节复制原程序及真实执行成立，输入/pins/配置保持；原检查发现1条错误和8条警告被如实表达。保存JSON仅语义相同，交接说明存在字段误述与限制遗漏，整体不记全通过；部分映射歧义也存在于B原方法资料，不能全部归因宿主。该轮generation0，不能补算压缩后执行。继续分开核对通用机制、包来源资料和模型输出质量，不把领域文案错误扩成专项运行时门；当前优先连续压缩及最终验收。见[唯一Goal](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)及[验收明细](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c17固定发布与原生开发验收2026-09-27)。

## 能力包全仓回归接缝（2026-09-27，本地定向通过，组合待验）

固定62c的全仓回归暴露配置占位对象、后台TaskLink及错误码登记接缝。缓存读取沿唯一入口只接受`CapabilityConfig`，无效对象仍按原路径读盘；一次选包模型失败只警告并继续原业务，不重放已领取调用。新定时运行在原claim后、后台快照前建立原TaskStore链接；旧绑定/pins/marker保留，缺失旧任务不重建。没有新增安装账、配置源或模型循环。细节与边界见[能力包合同](docs/design/CAPABILITY_PACKS.md#主任务子代理和长任务)，回归结果见[测试记录](TESTS.md#c16全仓回归修复2026-09-27验证中)。

补齐真实任务绑定后，三项既有测试暴露完成任务的冻结回复被普通过期唤醒过滤吞掉。沿原R279交付合同修复：合法冻结信封保留在原pending队列，执行入口准确回读后优先重投，能力预扫后的第二次回读仍保留；任务完成不等于回复送达。取消抑制、原回执去重及无缓存终态不得重跑业务保持；最终同版14文件375项通过，不新增投递状态或模型循环。

持久定时wake也须交回原SchedulerService领取与结算，不能只消费wake而遗留queued run。服务准确回读同run/job/thread/task的pending后，合法冻结回复只保留原claim用于交付；无有效冻结仍按原任务终态映射收口。原映射由服务统一提供给等待对账及交付收口；无法确认pending或任务状态时不冒充完成。准入结算必须确认原claim的CAS实际成功，返回None则保留pending；两条接手反例已由失败转为通过。该补片与Claude的主线修改已明确互斥，定向验证包含真实repository/enqueue/tick/finish、替身模型和投递，不冒充真实Gateway或正式发布通过。

## 通用能力包为主目标，领域仅作样例（2026-09-27，用户确认，执行中）

用户再次明确短剧或其他领域只是举例，真正交付的是能力包功能。后续优先推进通用生命周期、隔离发现、资源消费、任务版本一致性与迁移维护；不继续把专项人物/剧情能力迁移当主线。真实迁移与可使用仍要验证，样例暴露的通用机制问题继续修；纯领域内容质量和未迁功能单列，旧失败不改判。具体执行和三层证据口径见[唯一Goal](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。

当前发布资料已按源码澄清：推荐默认开、一次选包默认关；宿主配置有Agent缓存，新TUI不等于重载配置或新任务。更新/重新启用产生新激活，原Goal恢复不迁移旧pin；新任务重新选择的步骤见[迁移手册6.1](docs/design/CAPABILITY_MIGRATION.md#61-可选的一次选包与入口准备)。这是既有行为的操作说明，本轮不新增配置入口、热加载或状态账。G03普通待办对照已通过空选与忠实交付，G02缺陷及未覆盖项继续保留。

Claude的f824通用Compact修复已窄组合为8f53：同四元归档原写输出位置证明回执引用归属，Carried来源用同一判据复核，摘要保持原模型可见IR。本线31文件529项和严格gate通过；作者注释补片14e499的AST不变，最终9d5786952已精确安装。C16原生自动第1代提交及同代7份资源get通过，第2代完整恢复候选超出65k配置预算，尚未到原脚本执行。只读定界未确认新实现缺陷，第二代候选分项未持久，确切超额原因仍不可判；当前交接通用功能及发布缺项，不改变窗口、样包或提示词把旧失败补算通过。详见[第十六候选](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c16原生自动提交与资源续读2026-09-27部分通过)。

独立功能审计与第十四候选精确切换已完成，普通G01原资源复制执行通过、最终文案计数错误单列。随后G02在原产品上经独立65k profile触发自动恢复，refs来源分区冲突使恢复失败，原包版本绑定保持。此项属于通用Compact机制缺陷，Claude已确认承接原分区/投影/恢复来源修复；先沿真实构建链复现，不能清空refs、把预览当全文或扩大样包专项逻辑。自动提交和提交后资源消费仍未覆盖；本线未新增产品合同或修改产品实现，详见[当前验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第十五候选受控自动-compact2026-09-27通用机制失败)。

## A0.3.0普通原生复验：资源执行与内容自检（2026-09-27，失败证据保留）

原938宿主热更新A0.3.0后，一次普通A02已结束。canonical人物方法和workflow成功返回，模型有v3人物字段；原checker两页内层body拼接摘要与包一致，但实际文件是外层JSON信封，执行失败后改用简化检查，最终来源关系及内容仍有矛盾。此轮未重构完整model-facing文本，不把get成功说成全文实际入模，也不把尚未成功执行的字面诊断判为算法失效。独立语义复核同判业务未过，旧产物与配置保持。

本轮只记录失败与后续顺序，不新增宿主硬门、专项状态或第二检查器。先按既有组合验收和回滚约定将b496通用引用修复落实到私有宿主，再验原样资源执行及作者内容自检；不能从未安装修复的938结果推断b496无效，结构通过也不能代替语义通过。详见[原生验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#a030原生开发复验2026-09-27)，SHT-26与持物因果迁移缺项、保留集0/36保持。

## A0.3.0：成稿人物依据与字面诊断（2026-09-27，本地组件通过）

包内交付/报告显式升级v3，新增必填代称、显式退出和整镜画外声明。先从成稿回填，再由同一单文件检查器报告字面未覆盖或歧义，保持warning，不判断可见性真假。只迁SHT-22人物子集；SHT-26短引文、持物因果和完整媒体仍未迁入。预算按名称总字符数与镜头正文乘积估算，未检查计数为null，完整扫描后的输出裁剪另计。具体合同见[人物方法](examples/capability-packages/drama-text-a/methods/visible-characters.md)。模板、原脚本、资源声明与公开合成用例同步完成，141项A专用定向及15资源同字节构建通过；独立末审及本地严格gate已通过（线上CI未作为验收来源）。随后原生失败单列于上一节，不改写组件结果，保留集0/36保持。

## 第十二候选：工具结果的原逻辑读入口（2026-09-27，本地定向通过）

同一回执此前既展示本次归档物理路径，又提供可照抄的逻辑读参数。现只在reducer直接展示分支的临时副本中省略本次typed自归档refs及对应ref内容块，继续使用原scoped锚点；不改canonical结果、归档、reader或外置摘要选择。独立审阅指出总入口过滤会改变JSON摘要选择，普通相邻回归先失败后收窄；最终25项通过，真实模型采用仍待验。详细边界与成熟参考见[模型投影合同](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#本次自归档引用的模型投影2026-09-27)。

A0.2.1/0.2.2已反证“方法完整到达、回读成稿即可保证语义正确”。固定上游的下一项具体迁移选择是从成稿回填可见人物依据，再提供具名覆盖告警；实际实施范围随后收窄为SHT-22人物子集，SHT-26短引文仍未迁入；包内v3合同见上述A0.3.0，不扩宿主硬门。来源及非覆盖范围见[迁移差异](docs/design/CAPABILITY_SOURCE_COVERAGE.md#44-成稿可见人物依据的下一迁移切片待设计实施)。旧语义失败、保留集0/36保持。

## A0.2.2：普通创作的作者修订（2026-09-27，已原生复验，语义未通过）

A0.2.1原生任务已完整读取三份分表，却仍漏检接续和删改说明。固定上游普通创作入口只在用户点名时进入正式审稿，write/storyboard owner则在交稿前从成稿反查、直接修订原文档；本线未发现一个漏迁的自动执行器。候选将入口/workflow的末端工作明确为当前候选回读、逐项来源/状态对照、作者修订同一作品、最终原检查与真实汇报；正式审稿仍只交问题及保持项。
反证同时保留：0.2.1已经要求逐镜检查、作者修订及复读，公开轨迹没有证明模型发现问题后因审者职责拒绝修改。因此这是责任与顺序更清楚的待验改良，不是已证实根因修复。本片只调整已有A包流程与版本，不新增宿主门、模型循环或业务必交报告；三份分表和检查器保持。来源与边界见[来源清单4.3](docs/design/CAPABILITY_SOURCE_COVERAGE.md#43-a022普通创作的作者修订候选)；不会把结构通过或方法读取直接算业务通过。

本候选已完成构建/严格gate/热更新及一次原生复验：workflow与最终稿完整送达，三分表未get；模型自行修引用、回读最终稿后仍未修正说明、镜头和人物清单矛盾，语义失败。该观察不支持宣布澄清假设奏效，不能归因为workflow截断。后续先核方法实际采用和回读后漏检的边界，保留旧失败，不继续堆叠同义提醒；见[原生验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#a022原生开发复验2026-09-27)。

## 第十候选组合与操作回执事实（2026-09-27，修复已复核）

固定791f5d14b新增决策跳过记录和聊天skills入口。合并只需保留双方布尔字段和文档历史，原包选择仍归capability_config；4种实际YAML组合和30文件929项已通过，源码无漂移。
独立发现：skills聊天的普通异常统一宣称原记录未改动，而原remove/revert先修改文件/registry再append_event；后一步I/O失败不能抹去前一步事实。原作者c71按结构化operation区分读/写异常回执，写失败只称结果未完整确认并建议先查状态；本线吸收后343项及独立窄复核通过，原P2关闭。不新增事务、重放或第二账本；一处“可预期失败都发生在改动之前”的注释泛化另交原作者维护。

A0.2.1方法独立热更新后，原A02新会话已实际读取三份审阅方法并执行同字节原checker；格式修复后结构通过，来源标签与接续自检仍有失真。失败层归包方法执行/语义审阅，不能直接据模型误判增加故事专用宿主门；后续先核固定上游工作流与实际执行差距。此开发复验仍运行938，与新组合源码验收分开。

## A0.2.1 审阅方法与普通收尾归因（2026-09-27，本地候选／真实待验）

L01 持久化原生上下文仍含 42 镜525秒/目标600秒，模型随后公开承认差距却未修订交付 JSON，最终误报48镜600秒；操作核验仍为 partial。独立窄读现有事实链及固定 Codex/Free-Code 普通出口，未确认能解释本例的宿主丢失事实缺陷，因此不新增第二循环或领域完成门。
按固定上游当前 Markdown 审阅主线，A0.2.1 补来源成对证据、镜头接续、并行交付事实三份方法及反馈模板；14 资源只在包内按需读，入口2546字符，小于原2915。原checker、v2格式和合成资料不改，方法采用与语义质量仍待真实验证。65项定向及可重复构建通过。
另将[普通收尾合同](docs/modules/delivery/01-closeout.md)校正为当前实际范围：partial保留历史失败，明确artifact_integrity blocker最多一次有界续轮；只是文档校正，没有新增运行语义。来源和边界见[覆盖清单4.2](docs/design/CAPABILITY_SOURCE_COVERAGE.md#42-a021把审阅清单迁为实际工作单元本地候选)。候选提交后组合Claude固定main `791f5d14b`再验证，不沿用938的通过数。

## 第九候选真实反馈与后续归因（2026-09-27，分项已验／质量未收口）

固定`938d04aae`已私有精确安装；五席原需求复验保留一项业务通过、四项失败。四孩子首次原生消息均含同代包入口，没有额外选择调用；JSON_INVALID回执后孩子自行修复到JSON_VALID。
B交接在AST一致的手抄副本上实际发现旧摘要并修复，原包字节身份链未验；入口提供、私有方法采用、原脚本执行和业务质量继续分开记。
下一片先只读比较原包审阅方法、成熟参考与既有closeout事实装配，定位未声明新增、跨文件关系和最终数量误报；这是待审计方向，尚未新增运行合同或第二验收循环，不把模型质量改成宿主专项硬门。详见[复验矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第九候选普通业务复验2026-09-27)。

## 子代理包入口准备（2026-09-27，本地严格门及真实首次入口通过）

解决问题：长任务孩子拿到授权及名卡，却没有取得包入口。复用原首请求标记、创建链及RuntimeFacts；
已有Jev资格保持，新增资格只来自明确授权与同代引用，旧无标记线程不回填，也不新增选包请求。
原25文件570项与两项因果变异已验；显式选模后入口资格错误依赖pending advice的缺口已修并独立复核，原marker资格与Jev采用资格分开。claim提取早返回helper后，三片新指纹下35文件977项再次通过，严格尺寸通过；第九候选真实四孩子首请求入口已验，私有方法主动读取未触发。详见[能力包合同](docs/design/CAPABILITY_PACKS.md#主任务子代理和长任务)。

固定main `72cbb0411`的历史原文读取在合入前发现跨owner路径越界，合成全工具链已复现。Claude在固定`558eb65df`复用原路径段校验与owner线程登记修复，两份直接回归本线独立36项通过；本线组合后48文件1301项及六项本地严格gate通过，真实待验。线程ID不得成为任意文件路径，也不另建owner授权状态。

## 文件修改语法反馈（2026-09-27，已实施／异常隔离已定向复验）

解决问题：原生写入成功但JSON语法损坏时模型没有客观反馈。三文件入口在原子候选上做有界纯解析，
成功发布后沿同一工具回执返回观察；默认关闭，启用也不拒绝分块写入、不新增任务完成门。
字节、权限、配额、来源和重放仍归原链，详见[文件语法反馈](docs/design/FILE_SYNTAX_DIAGNOSTICS.md)。
215项分片及39项独立窄复核通过；收集诊断普通异常时整份本次反馈降为未检查，真实修改和取消控制保持。

## Compact 摘要生成事实（2026-09-26，设计交接／待实施）

解决问题：原响应形状告警缺请求关联，分段机械与模型内容合成后无法准确说明最终候选来源。
沿原辅助调用、候选和checkpoint传递结构化来源，区分单次、分段模型/混合/机械及未知；不解析摘要正文。
首片覆盖transcript与共用producer，live-tool持久来源尚未接通；旧v3缺字段保持未知，原hash/CAS/覆盖/计费不变。
不新增模型调用、重试、诊断账本或权限；真实验证仍经原生TUI。Compact/Jev由集成方统一实施，本线停止写入并交接设计。详见[模块设计](docs/design/COMPACT_GENERATION_FACTS.md)。

## 制作交接机械验证（2026-09-27，组件及路径缓存修复已验）

B0.1.3明确升级handoff.v2，以显式阶段、文件绑定和JSON Pointer核对真实字节摘要及对象关系。
只读命令行明确输入，不跟随清单路径自行打开文件，不判故事语义；原project-only报告交接未检查。
原检查器由模型沿现有工具链执行，宿主不增加领域门；版本与使用说明见[迁移流程](docs/design/CAPABILITY_MIGRATION.md)。
四文件183项定向通过，独立复核的路径缓存误合并已修：声明比较和实际快照身份分开，缺目录及中间链接按真实绑定检查。
完整旧project检查与新handoff检查分别报告；新版本尚未安装做真实TUI验收。

## 能力样包来源声明与制作交接（2026-09-26，实施中）

已确认真实采用不等于交付正确：A/B 原检查器通过的任务仍有来源新增和道具语义矛盾；长任务另有原检查器未执行、父派工未给包授权。
改进进入包自身版本：A0.2.0 已以 `drama_text_delivery.v2` 显式保存每镜来源、改编新增和未知，原脚本仅校验声明关系；B0.1.2 已补完整字段模板和交接清单。组合69文件1661 passed、9 skipped，私有原生升级通过；新六席B01/C02冻结范围通过，A02/X01/E01/L01质量失败。最新L01四孩子已显式授权且同代引用正确，仍有两份JSON语法错误和原检查器未执行，不能沿用旧轮省略授权的归因。
旧 ZIP 与失败结果保留，不暗改旧格式通过含义；不添加宿主领域门、自动补授权或第二执行器。结构正确仍不证明来源语义或真实媒体正确。
[固定上游能力清单](docs/design/CAPABILITY_SOURCE_COVERAGE.md)列出A的11个创作入口及维护入口、B的5个入口，区分索引、深读、迁移与验收；细节见[迁移合同](docs/design/CAPABILITY_MIGRATION.md#样包来源声明与制作交接2026-09-26历史候选记录)。

## 一次能力包选择（2026-09-26，本地通过／六席采用已验，质量未全过）

解决问题：只有摘要推荐不能保证模型实际取得领域方法。默认关闭的一次主任务选择复用原 structured 后端、辅助账、TaskLink CAS、ActionPolicy、包reader和pins。
一次标记使用当前后端公开字段摘要，不猜可能已提前切换的thread profile；只存选择数量/摘要，准确refs仍归原任务引用。
入口通过预算验收后才pin；31文件901项及严格gate通过，六席已验证领域入口/方法与普通空选；详见[能力包合同](docs/design/CAPABILITY_PACKS.md#一次能力选择已实现真实分项已验)。
启用且有授权候选时，纯问答也保留普通TaskLink供一次标记和任务列表使用，不创建Goal；默认关闭沿原晋升。
第七候选短剧质量仍未过。A0.1.2/B0.1.1已在包内补镜头/目标时长与交接说明，142项及独立审阅通过，同一Gateway热更新和新版六席复验完成；A/B原脚本使用已触发，但长任务未调用原脚本，语义与报告仍有失败。宿主不新增领域硬门，也不把脚本exit0等同内容审阅通过。
待评审、未修改：递归派工的空授权语义存在层级差异。主代理缺省为空，批量item缺省可继承顶层而显式空覆盖；子派孙的现实现对省略/空列表都继承父范围。不能宣称所有层级空列表均禁用，也不能借模型省参自动授权全部包。后续统一口径须覆盖明确空、缺省、批量覆盖、子集及越界；本轮保持原权限合同。
未实施：宿主如需标记“原包检查通过”，声明须归版本化manifest、脚本摘要复用files，再把执行身份接入原Shell/工具归档的typed事实；完整argv、回放、取消、Compact及跨平台合同须一起评审。包复制摘要或任意Shell成功不能代替实际执行绑定。本Goal范围已与集成方确认仅包层检查，不新增第二执行器、安装账或验证库；真实证据称“包内检查已运行（模型执行，宿主未绑定）”。

## 能力包内化（2026-09-25，首片实现／真实验收中）

解决问题：外部 Agent 的整套能力可独立发现、迁移、升级和卸载，包内方法不膨胀为全局 Skill。
复用原插件安装／授权／执行和逐轮快照；v7 纯内容包不创建 MCP 或 Python 环境。
A/B/C 先独立有来源和验收，再按任务组合；不将复制资料或转发原 Agent 当成100%内化。
详见[模块合同](docs/design/CAPABILITY_PACKS.md)、[执行 Goal](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)
与[来源及验收矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md)。
[迁移操作流程](docs/design/CAPABILITY_MIGRATION.md)使用普通候选源码和原管理命令，不增设自动学习或发布账。
原生替身复制、Compact 同代续读／换代拒绝已有组件证据；六席真实 TUI 已开始，三包安装启用和核心读写通过。
首轮业务未读取能力包，自然召回仍未通过；排查可见候选和选择链，不把模型自行回答当作能力迁移证据。尚未发布。
实际提示词已确认包目录可见；现补包级采用/续读软指引，以及原归档后完整来源引用投影。
大脚本替身先复现缺引用，再在0/200/1800字符预览下逐字节复制通过；第二候选仍未自然采用包，额外真实对照证实引用已可见但模型改用Shell复制，规范物化链失败。
已排除重启丢包与合成提示缺失；进一步审计原生传输和get选择参数。仅修改软指引未证明解决召回，不能加任务专用路由或用业务自评分覆盖失败。
安装版十二组零网络序列化检查未复现原生传输丢提示；实际动态推荐仅含工具，现接入原包摘要软召回，并提供能力配置开关。
读取接口同时补准确 `next_read` 和仍为失败的选择器纠错，读取建议携带内容及激活代次。均不新增循环、正文预载或权限，真实效果待复验。
真实管理已覆盖启停、坏包拒绝、升级、指定旧包回退、卸载和重装，Gateway无需重启；旧任务跨代续读及资源规范使用仍须另验。

2026-09-26 第三候选已真实读取 A/B/C 入口，长任务主代理读 A/B 并给四孩子固定同代范围；
子代理未实际读取方法，长任务 JSON 和时长核对失败，不能把入口采用当作完整能力通过。
第四候选正在修正通用资源地址表达：成功 scoped search/get 明示已声明成员的包命名空间，
附同代限页搜索参数；大结果沿原归档保留完整导航及整卡片预览，不推断正文业务路径。
A 样包独立修订为0.1.1，补齐镜头时长及原始输入字节摘要要求，原检查器不降级；
第四候选503项和本地严格gate通过；实际原样复制原脚本和执行成立，但A/C/L业务质量及B写入仍失败。
同字节重启用已验证旧任务原引用拒读、新任务读取新代；单纯停用臂未触发再次get。
四个孩子留存的实际原生输入含同代入口和可用工具；静态规则展示有差异，尚无证据确定其导致未采用。
不加强制调用或重复循环，主/子展示一致性若后续处理必须保持原scope和预算。详见模块合同和验收矩阵。

第四候选真实原生输入发现前轮L01记忆；新TUI不是无记忆对照。后续使用专用owner原memory-policy总闸关闭，
保留历史、包和权限，以实际Related Memory及used_memories联合验零，不改变用户日用记忆或增设测试旁路。
通用参数合同已本地修正：search显式含resource_path在读取快照前报TOOL_INVALID_ARGUMENTS，
不静默丢条件、不自动get、不泄漏成员；10反例红转绿、范围文件24项通过，尚未进入实际候选。

原生写恢复分类已本地收窄为后端确认的长度终态；坏JSON/EOF/过滤保留各自typed错误，零执行且不额外纠偏。
原guard保留停止原因与用量，删除无人使用的旧连续计数。四文件74项通过，实际候选尚未更新；
历史B01原因仍未知。详见[模型终态合同](docs/design/MODEL_TERMINAL_DIAGNOSTICS.md#原生写恢复的原因分类2026-09-26本地修正)。

资源引用声明已本地补齐：运行时要求完整9字段，模型工具schema原来只有宽泛object，真实A02因此漏name。
以package_resources唯一声明派生字段集合和required，沿原core/registry注入filesystem工具，工具层不反向导入能力包模块；
原通用参数门负责缺字段/类型错误，权限、代次、摘要及完整引用核对保持。不放宽name或增加提示词/错误旁路。
原19项反例13失败，修正后含零包启用边界的20项全绿，10个相关文件147项通过；尚未安装或代替实际复验。

2026-09-26更新：上述三片已合为候选5，统一534项及严格gate通过并精确安装。实际B01原字节复制／执行与业务通过，
两级孙代理同代读取和新客户端续原Goal身份有独立证据；质量缺项与未触发方法续用仍保留。
12个保留例只有首轮：9领域例4例get，稳定自然采用未通过。实际包名卡展示和动态推荐属于软召回，
现有决策include只选择展示，不自动读入口；零包读取答复在现合同中允许，不应误判成宿主丢pin。
后续“原首主请求前一次结构化选择→宿主有界读入口”已与协作方对齐，待实现；默认关闭，原TaskLink可选typed标记防止恢复重复，
不能用任务关键词硬编码、全量私有Skill展开、重复必调或保留集调提示来替代设计。

Compact补充片已获协作方归属确认，本地537项及独立审阅通过：原缓存前缀及工具schema保留，摘要请求沿既有ToolChoice.none禁止工具，
失败记录仅用结构化响应形状和原因，不写正文或参数；候选6实际同会话再次Compact已生成自然摘要，旧REOPEN02机械回退原因保持未知。
通用文件格式检查若后续做，只考虑复用原解析器的可选warning；当前未实施，不增加某个索引文件的专项合同。

决策分支已在本地吸收 main `66a598cf3`，尚未合入 main，也未部署：
- Compact 工具来源统一为 v3 checkpoint 加 run/attempt/turn/call 四元身份，主线三元 `tooling/call_ref.py` 已删除。
- 未知来源保持可见，并返回结构化的 uncertain 结果。
- 模型轮通过 `ModelTurnRequest`/`ToolLoopRunResult` 显式交回实际采用的参数。

兼容后果：`66a598cf3` 写出的 v2 行按 legacy 读取。合并版部署前做过运行中压缩的旧调用会重新进入模型上下文，只多占上下文，不丢失、不误隐藏。

回滚边界：旧版运行时读不了 v3，回滚必须把运行时和数据成对核对并保留新账。详见[依赖拆分](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#两线合并后的来源身份与模型轮结果决策分支吸收-main2026-09-23)。

- **子代理工具失败对父级可见、授权阶段反复失败即停、创建前做可见性预检**（2026-09-27，分支 `claude/subagent-observability`，本地回归与变异通过，未部署，见 TESTS）：
  - **起因**：Full Access 管理员父代理派 4 个只读子代理读 owner home 外的 git 工作树；子代理没有 Full Access（有意设计，不改），
    读取全在授权阶段 `PATH_OWNER_SCOPE_BLOCKED`，各卡约 20 分钟；父代理的 `list_agents` 只见“最近成功调用工具”，看不出被拦。
  - **查明的根因**：重复失败机制对子代理同样生效，但默认只返工提示、每 15 次清段；显式硬门默认关，即使打开也收成 unfinished→`PENDING`，
    随后被孤儿恢复立即重派、计数清零，父级收不到任何事件（`PENDING` 不在唤醒状态集）。
  - **做法（已落地）**：① `recent_tool_failure` 与如实的 `last_progress_summary`，从 owner 权威 `runtime_events` 现算，不新增状态；
    ② `task_local` 子代理同一错误码授权阶段连续失败达 `repeated_failure_halt_threshold`（复用原配置，默认 15，≤0 关闭）即 blocked 收口，
    runner 落 `BLOCKED`，`tool_failure_halt`（原因码/工具/错误码/次数/参数名）经原生命周期事件交直属父级；
    ③ `create_subagents` 对显式输入路径用子代理运行时同一判定链预检，看不到整批 `not_started` 拒绝（`SUBAGENT_INPUT_PATH_NOT_VISIBLE`）。
  - **拒绝而非警告**：沿用创建期结构化门的现有约定（越界 `output_files`、无效接管、已关闭 covers 都整批 `not_started` 拒绝，未知 covers 才警告）；
    “子代理读不到显式输入”是权限墙给出的客观事实，属于铁律允许的硬门；创建即自动启动，只给警告会让子代理照样空跑一轮。goal 正文路径不参与。
  - **边界**：执行阶段的同类失败（如父项目目录内的路径被 handler 以路径笔误提示 `TOOL_INVALID_ARGUMENTS` 拒绝）不触发即停；
    交替插入成功调用的循环不算“连续”，仍只受原返工提示和轮数上限约束。
  - 详见 [子代理结构](docs/modules/subagent/04-structure.md#子代理可观测与授权失败即停工具失败账本的两种投影) 与 [运行手册](docs/modules/subagent/SUBAGENT_RUNBOOK.md#状态与通知)。
- **技能提案与自动总结 Skill 可在 TUI 和 IM 里处理：`/skills`**（2026-09-27，分支 `claude/skill-proposals-tui-im`，本地回归与变异通过，见 TESTS）：
  - **起因**：用户说“命令行几乎不会用，所以所有都能 TUI 和 IM 来”。这两类操作原来只有 `my-agent skills …` 命令行入口，
    自学习审核顺序点 skill_proposal_review 因此在 TUI/IM 里永远触发不了。
  - **做法**：新增聊天控制 `/skills`（查看/确认/拒绝提案，查看/回滚/删除自动总结 Skill），经 Gateway 控制通道执行，TUI 与 IM 共用；
    确认、拒绝保留版本号复核；TUI 本地模式明确拒绝（未列出的控制会落进停止分支），文本还原带上参数。
  - **原则**（长期）：面向用户的能力都要有 TUI 与 IM 入口，只有命令行入口的视为缺口；本机维护命令可以只留命令行。
  - 详见 [自动总结 Skill 的用户入口](docs/design/SKILL_AUTO_SUMMARY.md#10-用户入口)。
- **决策审计说清“每个点位最近为什么没触发”**（2026-09-27，分支 `claude/9b-decision-miss-reasons`，本地回归与变异通过，见 TESTS）：
  - **起因**：审计里某个点位调用 0 次时，用户只看到“0 次”，分不清是没打开、没到触发点，还是每次都被条件挡下。
  - **做法**：新增唯一来源 `conversation/decision_reach_counts.py`。9 个点位每次到达触发检查都记一次结果：没调用记宿主原因码，
    真正交给决策模型前记 `called`。计数只在进程内累加，每个 owner 最多每 60 秒合并写一次
    `<owner_home>/data/decision/reach_counts.json`（owner 规范路径 `owner_decision_reach_counts_json`，Gateway 按用户作用域重设），
    保留 7 天，不逐次写盘。原因码都配了给不懂技术的用户看的大白话。开关复用 `decision_skip_records_enabled`
    （说明改为“决策点诊断记录”），关闭时跳过行与计数都不写。触发行为不变。
  - **展示**：`audit_records topic=decision` 每个 owner 附 `point_diagnostics`（是否开启、检查几次、调用几次、没调用的原因分布），
    飞书等 IM 里由模型直接用大白话解释；TUI 决策菜单“逐接入点设置”每行末尾显示“近24小时检查N次、调用M次，最多是因为：……”。
  - **边界**：model_selection、subagent_model、skill_tool 还没接计数，显示“未统计”而不是 0 次；距上次合并不到 60 秒、之后又没有
    新到达的计数只在本进程可见，Gateway 正常停止时补写，异常退出仍会丢；意外异常不计入。
  - **后续已做**（2026-09-27，分支 `claude/9b-owner-path-scope`）：`home_paths_with_owner` 按用户重设 `owner_memory_policy_json`
    （Gateway 里其它用户原先读的是本机主用户的记忆策略文件），并按字段全集守卫所有 owner_* 路径都在该用户 home 内；
    Gateway 正常停止收尾补写到达计数；数量类大白话改为从 `conversation/decision_point_limits.py` 现算。
  - **三点位计数**（口径已由集成方确认，分支 `claude/9b-three-point-reach`，每个点位单独提交）：model_selection 已接
    （关闭只在内存计数、保持零 I/O；诊断带适用范围说明 `note`）；skill_tool 已接（恢复与已评估不算，实验路径算同一次到达）；
    subagent_model 已接（拆开原来吞掉一切的 `except Exception: return None`，意外异常仍放弃但不计入）。12 个点位全部接入。
  - 详见 [决策审计与管控](docs/design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#每个点位最近为什么没触发2026-09-27)。
- **决策点“触发了但被挡下”也留审计记录**（2026-09-27，分支 `claude/decision-skip-records`，本地回归与变异通过，见 TESTS）：
  - **起因**：my-agent 在真实 TUI 里测 Jev 点位，planning 等没有任何记录，就写出“宿主未接线”的开发需求。实际都已接线且开启；
    planning 那一轮由 3186 字粘贴开启（后两条短消息是中途插话），原话超过 1024 字时按设计整点跳过，却什么都不写。
  - **做法**：结果日志新增 `skipped` 行，只在已到触发点、该点已开启、却被条件挡下时写，原因码 `request_too_long` / `privacy_url`，
    不含正文；`decision_skip_records_enabled` 默认开启。未开启的点、阶段出错不记。
  - **已定（2026-09-27，用户：参数默认就要合理，99% 的人不会手动调，把用户当什么都不懂来设计）**：超长原话不再整点跳过，
    改为首尾节选（开头约 2/3、结尾约 1/3，中间写明省略字数）并在 state 里如实标注 `current_request_completeness`，决策照常进行，
    信息不足时由决策模型选 need_data；`request_too_long` 不再出现。预算 `decision_request_max_chars` 默认 2000，保留给极少数想调的人。
  - 详见 [决策审计与管控](docs/design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#决策点触发了但被挡下也留记录2026-09-27集成方)。
- **压缩后可按编号查回原话，原话备份改为按 token 随窗口放大**（2026-09-26，分支 `claude/curator-budget`，本地回归与隔离真机验收通过，见 TESTS）：
  - **起因**：用户问“10 段各 1 万 token 的需求压缩后怎么办”。旧备份固定 6000 字、每条截开头 1200 字，只放得下约 4 段的开头；原始记录在磁盘但模型查不回。对照 Codex、Claude Code、Hermes、OpenClaw、Gemini CLI、opencode：没有一家把全部长需求原样留在上下文，成熟做法是“摘要记住有什么 + 需要细节时查回原文”。
  - **查回原话**：复用 `session_search`，新增 `message_id` 分段读原文（只读 canonical 消息文件）和 `current_thread=true` 当前会话检索、按页浏览；会话身份只取宿主可信上下文；翻看模式每条正文限 2000 字。显式 `thread_id` 必须通过路径段校验并属于本 owner 线程登记（2026-09-27 修复 Codex 复现的跨 owner 路径越界，存储层同时加校验，见 TESTS）。
  - **原话备份**：预算 min(`compact_landmark_max_tokens`=20000, 窗口 10%) token；每条带编号；短要求先放、最新优先，放不下的最新一条保留头尾；省略的编号列出并可选附回查说明（`compact_recall_hint_enabled`）；两遍处理不让原文整份驻留。
  - **摘要指令**：要求单列“User requirements”和“Pending user requests”。
  - **容量保护**：候选超出恢复目标就按超出量缩小备份并重算一次（备份已最小、或缩到最小仍超上限时不算），备份只占目标以内的空间，不会让压缩失败或多触发一次摘要请求。最初只在收缩能改变判定时才缩，小窗口里候选会贴着上限提交，CI 随临时路径长度确定性失败，当天改为现规则（见 TESTS）。
  - 详见 [会话上下文设计](docs/design/CONVERSATION_CONTEXT_DESIGN.md#压缩后查回原话与原话备份2026-09-26集成方分支-claudecurator-budget)。
- **Compact 分段请求改为片段在前、累计摘要与规则在后**（2026-09-26，分支 `claude/curator-budget`，隔离真机对比通过，见 TESTS）：
  - **问题**：用户问“1M 模型用到很大后换 200K 级模型还能不能压到能用”。隔离真机：DeepSeek 官方 1M 累积约 31.3 万 token 后切 MiniMax-M2.7 262K，压缩能自动完成、压后主请求约 2.9 万 token，但分段摘要退化成片段目录：两段只输出 344 token，上一段摘要整体丢失，第 1 份资料的规则丢了，其余规则只靠原文地标保住。
  - **原因**：分段请求把压缩指令排在最前，片段消息里又是“合并要求→此前摘要→十几万 token 数据”，模型读到结尾只剩数据。
  - **修复**：分段请求改为一条文本消息，顺序为 源片段 → 结束标记 → 此前摘要 → 摘要规则 → 分段合并要求 → 纠正提示；合并要求要求逐条保留此前摘要的有效要点。做法与 Hermes 一致（指令放最后）。预算、纠正、机械摘录、覆盖核对不变。
  - **复测**：同一场景 5 条规则全部进入模型摘要，两段输出 1,052 token，不再白写 17 万 token 缓存。
  - **遗留问题的原因（同日查明）**：
    - checkpoint 的 `projected_tokens_before` 偏小（35,915，实际约 31.3 万）：12.4-2b（`911d0d14d`）在决定摘要后先解绑旧请求历史，再量“压缩前”，只量到系统提示和工具。已改为解绑前按同口径量一次（`compact_request_recovery._full_request_tokens`），transcript 与活动回合两条路径共用；该字段不影响是否压缩，但会进入进度事件：Gateway 在压缩开始时把它存成本会话的上下文用量快照并推给 TUI 状态条（`request_context._gateway_compact_progress_callback`），所以修复前强制压缩期间状态条会显示偏小的数字（Codex 复核指出）。自动路径因此比原来多一次完整投影，只发生在已决定摘要时，相对摘要请求可忽略。
    - 最早的用户原话被挤出地标：6000 字固定上限（窗口不小于 72K 时）扣掉标题后剩 5,706 字，长消息每条 1,209 字，按最新优先只放得下 4 条。有意的固定成本设计，语义摘要是主载体，暂不改。
    - 更正：此前记的“宿主对中文估算偏低”是误读。那组 73,841 取自 `current_context_token_estimate`，它等于渲染后的提示词估算，原生协议下不含历史；估算器本身偏保守（63,562 对实测 57,650）。富 TUI 状态条用的是含历史、经供应商校准的快照；只有纯文本模式和 `gateway ask`/`gateway result` 的状态行显示这个不含历史的字段，未改。详见 [会话上下文设计](docs/design/CONVERSATION_CONTEXT_DESIGN.md#分段摘要请求的排列顺序2026-09-26集成方分支-claudecurator-budget)。
- **Compact 摘要请求改为禁止调用工具，模型违规时改走分段链重写**（2026-09-26，已合入 main `f230ab077` 并双机部署 `step12v-dff317e5`，隔离真实验收通过，见 TESTS；吸收 Codex `d4dd6c094` 的产品与测试部分，并补违规兜底）：
  - **问题**：main 的摘要请求为复用缓存，带着主会话工具，却没设 `tool_choice`，宿主补成 `auto`，模型可以直接调工具。只要回复里有工具调用，摘要就被当作空，退成机械摘要：旧摘要、操作证据加原文片段，不是真正的总结。
    - Codex 私有验收里的真机样本：MiniMax-M2.7 经 Anthropic 兼容协议，带工具加 `none` 仍返回 tool_use。
  - **修复**：
    - **不让模型选工具**：摘要请求保留工具定义以复用缓存前缀，选择设为 `none`；`tools_for_choice` 在 `none` 时不再清空工具，产品里只有 Compact 用到 `none`。
    - **违规兜底**：单次摘要仍回工具调用时，改走原分段链重写（文本化来源、空工具、`none`，沿用原纠正与确定性摘录）。分段链只在非文本来源、严格来源不接受降级这两种原因下失败时，才交回原回复由上层机械回退；来源变化等其它错误照常上抛（Codex 复核后收窄）。
    - **诊断**：无正文的响应形状日志加上 `request_id`、`thread_id`、`purpose` 和 `logged_at`。
  - **未实施**：摘要生成来源写进 checkpoint，见 [Compact 摘要生成事实](docs/design/COMPACT_GENERATION_FACTS.md)；传输层记录实际发送的 `tool_choice` 与工具数，Codex 已指出接缝位置。
  - **顺带修复**：`test_gateway_compact_recovery*.py` 的 12 个用例自 `7a15c9c91`（/effort）起在 main 上失败，因为 `_payload` 改收请求面对象；测试已同步。详见 [会话上下文设计](docs/design/CONVERSATION_CONTEXT_DESIGN.md)。

- **Jev 其它接入点"开了没效果"：真实原因与修复**（2026-09-26，已合入 main `756d4b9bb` 并双机部署，隔离真实验收通过，见 TESTS）。用户在 TUI 里发现，TUI 里的模型凭 `audit_records` 断言"只接了选模型，其余点位没接线"。
  - **事实**：
    - **点位都已接好**：12 个点位都已接入代码。owner 设置是总开关开、11 个点位观察模式、超时 5 秒。观察模式按设计只记录、不生效，想生效要切到 apply。
    - **Jev 慢**：本机经代理访问 `api.typesafe.ai`，TLS 握手 0.8–3.6 秒，单次决策约 2.5–5 秒；同样走代理的 `api.deepseek.com` 握手只要 0.02–0.03 秒。每次调用都新建连接。超时设 2 秒时几乎全部超时，设 5 秒后仍偶发超时。
    - **共享冷却**：任一点位超时，整条连接都会冷却，连续失败翻倍到 300 秒。选模型每轮最先调用又常超时，其它点位几乎拿不到调用机会，而冷却跳过不留任何记录。
    - **审计看不全**：`audit_records` 只读请求记录里的选模型与能力展示观察，其它点位即使调用了也看不到。
    - **观察模式拖慢回复**：选模型的观察同步等待 Jev，每轮多等 3–5 秒（请求记录里 `conversation_prep_ms` 约 5 秒），结果却不采用。
  - **修复**：
    - **决策结果日志**：`decide()` 的每个返回按点位写一行到 `<owner_home>/data/decision/outcomes.jsonl`，这是 owner 规范路径 `owner_decision_outcomes_jsonl`。只记结构化字段，无正文，有上限。`audit_records` 的 decision 主题新增按点位的次数与最近几条；`scope=current_thread` 时只统计当前会话，不混入其它会话和后台点位（Codex 复核发现后修正）。
    - **冷却分级**：超时只冷却本点位（`point_backoff`）；连接被拒、5xx、DNS、额度和配置错误仍冷却整条连接（`connection_backoff`）。成功时两类冷却一起清零。
    - 详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)。
  - **未改，属建议**：
    1. 在代理软件里给 `api.typesafe.ai` 换一条更快的线路。这由用户操作，见效最快。
    2. 让决策请求复用连接。要改传输层，需要单独做。
    3. 观察模式不挡在回复前面，或选模型只在情况变化时才问 Jev。要改请求流程，后者还需用户决定。
    4. 想让某个点位真正生效，把它切到 apply。

- **验证账不收"检查没真正执行"的命令**（2026-09-26，已合入 main `76cb23c6c` 并双机部署 `step12r-d79ae184`；Codex 反例驱动）：
  - 命令里出现换行或回车就整条不算证据。shell 把换行当命令分隔符，返回码只属于最后一条命令。
  - 规范验证命令带上已知的"不执行检查"参数也不算证据，例如帮助/版本、只收集、只编译、演练，以及让失败被吞掉的 `make -i`。参数表按规范命令分开。
  - 这些规则只减少证据、不读输出；不认识的参数仍按原规则记。
  - 未覆盖：配置通道（`PYTEST_ADDOPTS`、`MAKEFLAGS`、ini addopts），以及 cargo/go 的名称筛选仍记 full。详见 [verification 进度](docs/modules/verification/02-progress.md)。
- **智能程度（推理强度）：主会话 `/effort` 与子代理 `effort`**（2026-09-26，已合入 main `7a15c9c91` 并双机部署 `step12k-e5b2f8bc`，隔离真实验收通过；详见[智能程度设计](docs/design/REASONING_EFFORT.md)）：
  - **用户要求**：能设置模型的智能程度，包括给子代理单独设置，并实测。原 `/effort` 只是空壳，请求体从不发推理参数。
  - **实测结论**：DeepSeek 官方 OpenAI 兼容接口的 `reasoning_effort` 与思考开关都生效（low 推理 token 约减半）；其 Anthropic 兼容接口只有开关生效；OpenCode 中转与 MiniMax M2.7 都不生效；MiniMax M3 默认不思考、显式开启才思考。“被接受”不等于“生效”。
  - **做法**：档位 auto/off/low/medium/high/max 是会话线程属性（`/effort` 写主会话，`create_subagents.effort` 写子线程，省略继承父级实际档位，全局默认 `model_reasoning_effort`）；模型档案新增 `reasoning_control`（auto/effort/budget/none），auto 只对实测确认的 DeepSeek 官方接口给默认，其余 none，可在 `/model` 显式声明。真实请求与两处自动选模投影共用 `request_reasoning_options`，强制工具选择的关思考优先。
  - **边界**：不按模型名或正文判断能力；不支持的模型如实回执“不改变请求”；TUI 底栏暂不显示档位；Responses 协议暂不换算。
  - **真实验收**：DeepSeek 两种接口、声明为 `budget` 的 MiniMax M3、MiniMax M2.7，以及一次创建 low/max/继承三个子代理，全部符合预期（详见 TESTS.md 顶部）。生产默认模型是 OpenCode 中转，按实测不支持调节，`/effort` 会如实提示；要生效需切到 DeepSeek 官方接口，或给支持的模型显式声明控制方式。
  - **自动检测是否支持调节**（2026-09-27，分支 `claude/be-effort-probe`，已合入 main，随 step13t 部署；真实服务商验收待做）：my-agent 曾为确认 opencode.ai 是否支持推理强度，
    3 次把 API key 写进 `web_fetch` 请求头。现在由宿主用已保存的凭据检测：`/effort` 设成 auto 以外的档位、档案未声明且解析为不支持时
    自动检测一次（开关 `reasoning_control_auto_probe`，默认开），`/effort probe` 手动检测；同一道短题按低 / 最高 / 不带字段各发 3 次，
    只比较 usage 里的 token（优先推理 token），“最高”组中位数至少为“低”组 1.5 倍、多 200 且两组不重叠才算支持（按 09-26 实测回放标定）。
    确认支持时经参数中心写 `reasoning_control: effort`（私有档案下一轮生效、`/effort revert` 撤销；部署默认模型仅管理员写全局配置；
    共享模型只记录），不支持如实记录。凭据不进入模型上下文。未落地：独立短题的真实验收。详见第 8 节。
    结论提示：检测结束后作为宿主提示在同一会话下一条回复顶部显示一次（分支 `claude/be-host-notices`，待集成，见下一条）。
- **宿主提示（host notices）**（2026-09-27，分支 `claude/be-host-notices`，基于 `eb7c639a1`，待集成；详见[宿主提示设计](docs/design/HOST_NOTICES.md)）：
  - **要求**：宿主要告诉用户的一行字（如检测结论）附在同一会话下一条回复顶部：模型看不到、飞书排在正文前、TUI 灰色系统行、只显示一次。
  - **查证**：assistant_commentary 是模型内容；turn-end 是结束原因专用协议且排在回复后、飞书不用；`/progress` 最多投一次、没有送达确认；
    只有最终回复可靠但缺字段。所以新增通用 `host_notices`。
  - **做法**：待送达的提示存在线程 `pending_host_notices`（同来源替换、最多 5 条，`MODEL_HIDDEN_THREAD_FIELDS` 让上下文包剔除）；
    用户消息落账后发布 `host_notice` 流事件，正常回复提交时按编号取走（提交即已读，集成者定的 B 口径），进最终元数据与
    `channel_delivery.host_notices`；飞书适配层渲染在正文前，TUI 与同会话窗口画灰色系统行，历史回放由元数据或快照重建。
  - **边界**：只附在前台回复上，后台回合不附；停止或失败不取走；没有“必须确认送达”的保证，将来需要时在同一字段上加确认。
- **Shell 读边界与回执表述不一致（macOS）**（2026-09-26 登记；回执已修复；读边界第一步已上线 step12q，上层目录热修复 step12s；第二步已实施，开关默认关闭；Codex 能力内化验收 TUI-CAP06 中前台 `run_command` 发现，证据留在其私有线；归宿主执行/沙箱这条线）：
  - **事实**：owner 隔离模式下，macOS Seatbelt 规则（`agent_py_agent/agent/attempt/sandbox.py` 的 `_macos_profile`）是 `allow default` 加 `deny file-write*` 再放开写根，只限制写、不限制读，命令能读到 owner 工作区外的宿主路径；`read_file` 的 owner 读墙更严。Shell 回执（`agent_py_agent/agent/tooling/shell.py` 的 `[sandbox_scope]` 文本与 `sandbox.external_host_paths_hidden`）在 owner 模式下一律写 `external_host_paths_hidden=true`，这只在 Linux 挂载隔离下成立，macOS 上与实际不符。
  - **影响**：模型会以为宿主路径“看不到”，实际能读到；两条读路径的边界也不一致。不涉及越权写入，但 macOS 规则里没有任何读拒绝：owner 隔离的 Shell 能读到网关账号可读的一切，包括其它 owner 的数据和含密钥的配置目录。这是安全缺口，不只是表述问题。
  - **回执（已修复）**：`attempt/sandbox.sandbox_hides_host_paths()` 按平台给出只读隔离事实（Linux 为 true，macOS 为 false），Shell 回执文本与 `result_envelope.sandbox.external_host_paths_hidden` 同源；macOS 版如实说明只限制写入，并写明工作区外的路径不在任务授权内、不要读取或依赖。
  - **读边界第一步（已上线 step12q）**：owner 隔离形态下，Seatbelt 先拒绝读取 my-agent 家目录，再放行本 owner 家目录、attempt view、staging、授权读根和写根；其它 owner、config（含密钥）、发布记录读不到，与 Linux 挂载视图对齐。本机实测 Seatbelt 是“后写覆盖先写”，所以拒绝必须写在放行之前。拒读根由 `ToolRegistryParams.host_private_roots` → `ShellToolOptions` → `_sandbox_exec(private_roots=)` → `AttemptSandboxSpec.private_read_roots` 显式传递，前台、后台、终端会话三条路径都覆盖；Full Access 不生效。影响：本机默认管理员也在 owner 隔离形态（访问模式 workspace-write），它的 Shell 以后同样读不到 `~/.my-agent` 里自己家目录以外的部分，要读需切 Full Access；项目目录不受影响。
  - **第一步的回归与热修复**（2026-09-26）：拒读根用 subpath，连根目录本身一起拒绝，根与本 owner 目录之间的各级目录因此拿不到元数据。owner 工作区在 `~/.my-agent` 之下时，git、node 的 realpath 和 `python3 -m venv` 逐级 lstat 会遇到 EPERM 并退出。现在对这些上层目录按 literal 放行 `file-read-metadata`：能 stat 目录本身，仍列不出内容，同级目录读不到。详见 [TESTS](TESTS.md)。
  - **读边界第二步（已实施，开关 `shell_sandbox_hide_user_home` 默认关闭；生产未开启）**：用户家目录里的敏感目录（如 `~/.ssh`、其它项目）在 macOS 上原本仍可读。
    - **做法**：开关打开时，`user_space/owner_access.owner_hidden_host_roots` 只对非本机管理员的 owner（`is_local_admin_owner` 为假），并且只在平台沙箱本身不隐藏宿主路径时（macOS），在拒读根里追加用户家目录。开关读全局配置，owner 无法自行覆盖；它也列入 `BOUNDARY_KEYS`，模型不能改。
    - **HOME**：`tooling/shell._redirected_home` 判断宿主 HOME 是否落在拒读根里，是则把前台、后台、终端三条路径的子进程 HOME 改指到 owner home；Shell 回执据同一判断说明家目录读不到。上层目录的元数据放行（见热修复）同样覆盖家目录这一级，git 逐级 lstat 不受影响。
    - **实测**（本机真实 Seatbelt，拒读家目录且 HOME 改指向）：git init/status/commit、python3、node、npm、uv、pip 下载都正常；`~/.ssh` 和其它项目读不到。
    - **已核实的约束**（设计依据）：
      - 本机常用工具链（python3、node、npm、uv 在 `/opt/homebrew`，git 在 `/usr/bin`）不在家目录，拒绝家目录不会弄坏它们。
      - owner 隔离的 Shell 没有改 `HOME`（`tooling/shell._subprocess_text_env` 只改 TMPDIR、缓存目录和 PYTHONUSERBASE，并擦洗凭据）。Linux 上家目录没挂载，git 读 `~/.gitconfig` 得到 ENOENT 会跳过；macOS 若拒读家目录会得到 EPERM，git 的 `access_or_die` 只容忍 ENOENT/EACCES，会直接退出。所以只加读拒绝会弄坏这些 owner 的 git。
      - 对本机管理员拒读家目录会弄坏它自己的 SSH/HTTPS 凭据（git 推送等），而真正的风险来自其它 IM 用户。
    - **边界**：打开后，这些 owner 用不了你本人的 git/SSH 配置和凭据（OpenSSH 按账户数据库而不是 HOME 找 `~/.ssh`，所以读不到，这正是目的）；装在家目录下的工具（如 `~/.local/bin`）也跑不了；环境变量里指向家目录的路径（如自定义的 `XDG_CONFIG_HOME`）读不到。本机管理员与 Full Access 不受影响，Linux 不变。
    - **为何生产未开启**：目前唯一的非管理员 owner 是用户本人的飞书身份，开启后其飞书里的 Shell 也会读不到家目录、用不了本人凭据。有其它 IM 用户接入时再开。
- **记忆 Curator 生产持续失败：输入预算与结构化输出**（2026-09-26 登记；输入预算与失败诊断已修复，main `e47f60d0b`、`22b052fdb`，双机部署 `step12m-cdda962b`，生产已恢复；结构化输出方式已修复，见事实 2）：
  - **事实 1：输入预算（已修复）**。生产 owner 的 Curator 运行记录里，9/25 的 180 次和 9/26（UTC）至今的 51 次全部是 `CURATOR_INPUT_BUDGET_EXCEEDED`，触发原因都是 `session_close`，`cursor_before` 始终同一个、每次处理 0 条；最后一次成功在 2026-09-24T15:28Z，此前成功批次的提示已贴着 40000 上限（39399、39653）。测试 owner `tui-matrix/p1-r141` 同样卡住（9/25 失败 198 次）。根因已用测试复现：收集阶段按条目估算，只给模板留固定 7000 字符（模板实测约 4981）；消息收满预算后，审计仍按保底至少收一条（`curator_inputs.py` 的 `remaining_chars = max(1_000, …)`，且第一条不受上限约束，带 1000 字预览的一条约 1500 字），再加上身份清单里的消息和审计编号，最终提示就超过预算。提取前检查（`curator_backend.py`）直接报错、不缩批，游标不前进，下一轮重建同一批。只有消息时余量够装 80 个编号，所以要有审计事件才触发。
  - **修复**：`fit_batch_to_input_budget` 在可选决策标注之前按最终提示实测长度截尾（先消息后审计，各至少留一条），被截的尾部留在原游标之后、下一轮重放，零丢失，与超时缩批同一游标契约。缩批时运行结果带 `memory_curator_input_fitted:messages=a->b,audit=c->d`；成功运行的 warning 不进运行账（与原超时缩批相同），所以另按尝试形状的日志约定写一行无正文摘要到网关日志。保底后仍超出（预算小于模板）保持原失败码。
  - **生产验证**：9/24 15:39Z 到 9/26 07:17Z 共 307 次 `CURATOR_INPUT_BUDGET_EXCEEDED`，全部是同一个 `cursor_before`；00:23 PDT 切到 `step12l-5145e6cc` 后第一次运行从同一游标开始，成功处理 44 条消息和 1 条审计，生成 2 个候选和 3 条日记事件，游标前进；00:30 第二次运行又处理 24 条。测试 owner `tui-matrix/p1-r141` 也不再卡在超预算，改为 `CURATOR_MODEL_FAILED`（未配模型）。
  - **失败诊断残留（已修复）**：提取在预算早退前没有清空本线程上一调用者的尝试形状，`finally` 里 `reset(token)` 恢复的也是上一调用者的形状；生产 9/25 有 41 条本机失败记录带着飞书 owner（该 owner 未配模型，当天 50 次 `CURATOR_MODEL_FAILED`）的 `ModelNotConfiguredError` 形状。现在提取开头先清空。
  - **事实 2：结构化输出（已修复，分支 `claude/curator-budget`）**。Curator 与自动总结 Skill 的结构化调用原来固定发 `response_format: json_schema`，DeepSeek 官方 OpenAI 兼容接口直接 400（“This response_format type is unavailable now”）。Curator 只用 owner 默认模型（`selected_model_config`），不跟随会话 `/model`，所以只有把默认模型设成 DeepSeek 官方时才受影响。9/24 前半天的 103 次 `CURATOR_SCHEMA_INVALID` 经诊断是模型调用本身抛 `ValueError`（当时中转要求会话编号），已由 `f06d780de` 修复，不是 schema 问题。修复：模型档案新增可选 `structured_output`（auto/native/json_object），与思考控制同一套做法——`backends/structured_output_mode.py` 只对实测确认的 DeepSeek 官方 OpenAI 兼容接口默认 json_object，其余 native（逐字节不变），可在 `/model` 编辑或 `manage_models` 显式声明；json_object 发 JSON 对象模式并把 schema 写进提示，结果仍由调用方严格解析。实测（2026-09-26）：同一档案强制 native 立即 400；auto 解析为 json_object，服务商接受，结果通过 Curator 严格解析，身份清单 3/3 覆盖。
  - **没配模型的 owner（已修复，分支 `claude/curator-budget`）**：飞书 owner 与测试 owner `tui-matrix/p1-r141` 未选模型，约每 7 分钟重建一次 owner 实例、整批收集后以 `ModelNotConfiguredError` 失败，还原地重试一次（各 28 次/3 小时，不调模型、不耗 token）。现在：永久配置错误（`ProviderConfigurationError` 一族，含未配模型、4xx 拒绝）按其基类合同不再原地重试；未配模型记独立失败码 `CURATOR_MODEL_NOT_CONFIGURED`；发现层与待处理原因路径共用 `curator_failure_retry_seconds`，这个失败码退避一小时，其它失败仍是 5 分钟。IM owner 空闲回收后重建会读到新模型配置，所以配好模型后最迟一小时恢复。
  - **事实 3：恢复后的零星失败（诊断已补，分支 `claude/ae-curator-diagnostics`；截断处理待观察）**。主 owner 自 9/26 恢复后成功率约九成，失败有两类，每次都在下一轮从同一游标重做并成功，没有丢批次。
    - 8 次 `CURATOR_SCHEMA_INVALID`，诊断都是 `curator response is not strict JSON`，调用形状 outcome=ok：模型回了，宿主严格解析失败。json_object 模式下不大会带代码块或说明文字，剩下截断、空内容、中途格式坏三种可能。当时的运行账分不清是哪一种：后端已把截断标成 `truncated` / `stop_reason`，但 Curator 解析前不看这两项，也不记响应长度。
    - 3 次 `CURATOR_COMMIT_FAILED`，诊断只剩 `curator batch commit failed`，被包住的 OSError 连同 errno 都丢了。其中 9/28 15:07 那次，系统在失败前 43 秒报 `VQ_VERYLOWDISK`，之后 step15t 的网关日志里也有 `Errno 28`，判为磁盘满；另外两次（9/27 00:48、9/28 07:58）附近没有低空间事件，原因无从查证。
    - **已实施**：解析失败包成 `CuratorResponseParseError`，它仍是 ValueError，失败码和不重试语义都不变，只是附带响应形状。`_failure_diagnostic` 沿显式 `__cause__` 链记根因类名，另记 OSError 的 errno 和 JSONDecodeError 的出错位置。整条诊断超过 300 字符时退回只含类名的形状。运行账键集不变。
    - **待观察**：诊断上线后，下次再出现 "not strict JSON" 时先看 `truncated`、`stop_reason`，以及 `cause_pos` 和 `response_chars` 的关系。如果是截断，再考虑把截断当成模型调用失败，走现有缩批路径重试，用更小的批次换更短的输出。如果是空内容，考虑同输入重试一次。数据确认之前不改重试语义。

- **自学习 S3：完成任务后自动总结 Skill，不要用户逐条审批**（2026-09-26，已合入 main 并双机部署，隔离真实验收通过：真实模型新建并更新了一个 Skill，验收中修了宿主会话绑定和“不记绕过拦截做法”两处；详见[自动总结 Skill 设计](docs/design/SKILL_AUTO_SUMMARY.md)与 TESTS 顶部）：
  - **用户决定**：要有“完成任务后自动总结 Skill”的能力，且“别让用户审批，这个用户没时间审批”。所以用确定性的自动闸门代替人工确认，`AGENTS.md` 自学习约束同步改写；`enable_self_learning` 仍默认关闭。
  - **主链**：主代理任务按结构化判据完成（`conversation_task_completed`、非 task_local、非后台回合、`tool_rounds ≥ self_learning_min_tool_rounds`）时，收口写一条有界、脱敏请求到 `<owner_home>/data/skill_learning/requests/`。Gateway 后台记忆整理车道逐条处理：非阻塞运行锁、前台同端点让路、每日上限，复用 Curator 的 backend 做无工具结构化调用，输出 `create/update/skip`，经闸门后发布到 `<owner_home>/skills/learned/<name>/SKILL.md`，下一轮快照可见。
  - **闸门与所有权**：输出合同、与任何来源的 Skill 重名、删过的名字、数量上限、只更新本轮 `skill_search get` 读过且登记表 hash 与磁盘一致（用户没改过）的自学 Skill、发布前脱敏、frontmatter 往返、`agent_generated` guard。`registry.json` 是自学归属唯一权威，`ledger.jsonl` 只记结构化字段，版本全文可回滚；`my-agent skills learned list/show/revert/remove`，删过的名字不再自动新建。
  - **S1 调整**：自学习开启时，子代理 lesson 提案立即以 `confirmed_by=auto` 走原确认链安装，复核失败的保持待确认；S2 只对遗留待确认提案生效。
  - **参考**：Hermes 与 OpenClaw 上游的后台 review、所有权、使用回执与账本回滚做法；不照搬配额式“每次都要学”、不扫描直接写和默认关扫描。
  - **未做**：支持文件、后台/Goal 回合学习、使用统计、合并与退役。
  - **已知限制：自然采用**（2026-09-26 真实验收观察，待跟进）：第 4 轮任务与已发布的 `owner:csv-merge-cli` 高度相关，模型却没调 `skill_search` 直接做完；第 5 轮用户提示“先找一下之前的做法”后才读取并触发更新。Codex 用 `/show-prompt` 取证：Skill/包摘要确实进了最终提示词，采用规则也在，属通用 Skill 采用问题，展示与采用规则归 Skill 路由线；自学习侧只观察，不为 learned Skill 加专项提示。先看线上账本里 `used_skill_ids` 命中率，再决定是否需要通用的采用回执或提示调整。

- **决策开关、超时自调、统一审计与管理员管控（用户 6 项要求）**（2026-09-25，已实施：本地分支 `claude/decision-audit-controls`，基于 main `07fa00fb3`，已合入 main `2093631e5`）：① `/model` 决策设置每个点改为“开启 + 观察模式”两个勾选，存储值仍是 off/observe/apply；② my-agent 经 `user_config decision_patch` 自调等待时间受 `capability_config` 的 `decision_agent_timeout_min/max_seconds`（默认 1—30 秒，0 不限）约束，越界拒绝不夹取；③ 评估“每条消息都问一次选模型”：observe 下同步等待且约六成超时、输入以摘要锚点段为主，按现状不划算；已只删材料精简（摘要去原文锚点段并限 1500 字、当前消息限 4000 字、截断如实标注、候选公共声明只写一次，合成输入约减 63%），“只在结构性变化时问”需用户拍板，未做；④ 统计行改为“决策 ≈N token · 成功 X · 失败 Y”；⑤ 新增统一只读审计工具 `audit_records`（topic 枚举，首个 decision；本人范围，跨用户需管理员明确许可；只读设置/用量账本/请求记录观察，不 grep 日志或正文）；⑥ 新增管理员专用 `admin_controls`（每次本人确认；各用户 Jev/审计开关与本人跨用户审计许可存目标 owner 的 `tool_policy.json.admin_controls`，失败关闭），Jev 禁用硬拦在唯一调用入口 `invoke_decision_model_call`。新错误码在 `error_taxonomy.py` 末尾独立块。详见[决策开关、超时自调与审计](docs/design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md)。

决策设置的宿主非阻塞读取改为无锁读取已提交版本（已合入 main `d69f30cf3` 并部署双机）：原先读取也拿排它锁，同一 owner 的并发决策互相挤成 `settings_busy` 静默回退；两份设置文件都是原子替换写、单次写事务只改一个文件，旧建议仍由调用前后版本复核与在途取消挡住。同一分支用本机 HTTP 故障矩阵钉住断网/DNS/TLS/额度/计费/5xx/慢响应的冷却与恢复。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#42-已实现的可选服务边界)。

- **宿主关闭时主动取消在途决策，及 Curator 与插件点并发组合（P4-F）**（2026-09-25，已实施：分支 `claude/decision-shutdown-cancel`，已合入 main（`1bfe0683f` 标记 shutdown settlement 与 interrupt 修复已落地）；主线 owner 已同意在 `cli/gateway_process.py` 加一行）：
  - **关闭取消**：`decision_policy.cancel_active_decisions_for_shutdown()` 复用设置撤销那张进程内在途索引。等待中的调用立即回原方案（`stale/host_shutdown`），不冒充用户停止、不进冷却，调用账记 `DECISION_CANCELLED`；关闭后不再登记新决策。Gateway 收尾在置位停止事件后调用它，出错只记异常类型。
  - **并发组合**：后台 `curator` 慢响应不拖住前台 `skill_tool`；线程变更只撤销前台，owner 级改 `curator` 只提前撤销后台。
  - **两处已知取舍**（不改，登记在此）：采用前复核按整份策略版本判断，所以 owner 级任何设置改动会让同 owner 其他点的在途建议返回后作废为 `policy_changed`；冷却原先按连接共享，2026-09-26 已改为超时只冷却本点位（`point_backoff`），只有连接错误才冷却整条连接（见本台账 Jev 条目）。两者都只会少一条建议，不会采用过期建议或卡住。
  - 停止时仍未结束的模型调用已由主线 owner 结清为结构化"被中断"（`db4d46398`，已部署）：排空窗口之后，Gateway 进程账本里的在途调用记为 failed / `MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`，用量保持未报告、不补零；runner worker 的账本另行跟进（2026-10-02 J17 已做：进程内 runner worker 与 owner 池 agent 一并结清，见台账 J17 节）。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)第 4.2 节。
- **决策实验自动晋升后没有主动提示**（2026-09-25，F1 正向晋升真实样本发现；已实施（2026-10-01），本地待集成）：新 `applied` 回执冻结唯一 `promotion_id` 与规则摘要，沿原宿主提示通道在 TUI 当轮灰行及同会话飞书最终回复前提示点位、off→apply、样本数/门槛及真实 `/model` 恢复继承路径。原请求回执阻止重放，`notice_id` 复用唯一编号，消费后重启不补投；不解析自然语言。隔离测试及变异已验证，真实客户端收信未验证，见顶部 J6 与 TESTS 同名节。
- **自学习的 lesson 来源在当前产品里是死路**（2026-09-25，真实验收发现；已由 `record_lesson` 修复并做端到端真实验收，已合入 main `52e0190e1`）：
  - 子代理提示要求"像普通协作者一样回复、不输出状态 JSON"，`output.json` 由宿主生成，没有结构化通道填 `lessons`。所以真实子代理即使在回复里写了经验，也不会产生 `subagent_lesson` 候选，S1 提案与 S2 排序都无法触发。
  - 宿主不能从自然语言回复抽取经验。修复为仿照 `record_finding` 的独立结构化工具 `record_lesson`，见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 15 节。
- **自学习 lesson 的结构化来源 `record_lesson`（第 15 项 P5-C，S1/S2 的上游）**（2026-09-25，已实施并真实验收，已合入 main `52e0190e1`；端到端真实验收见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)「15 自学习 S1/S2 端到端真实验收」一节）：
  - **工具**：子代理专属，参数只有 `title`/`when_to_use`/`procedure`/`applies_to` 四个必填字段（上限 120/500/1000/200 字，Schema 禁止额外字段）。run/attempt 取当前 runner 上下文，task 取该 run 的任务记录，模型不能自报身份。写本 run 工作区的 `lessons.jsonl`：与 `findings.jsonl` 同目录，路径登记在 `AgentRunWorkspacePaths.lessons_jsonl` 与 `SubAgentTask.agent_run_lessons_jsonl`。
  - **账本规则**：沿用 record_finding 的账本口径，锁内先读再判再写，有坏行就拒绝追加；另加 O_NOFOLLOW，防止借符号链接写到工作区外。id 取四字段内容 hash，同 run 相同参数只记一次，返回 `already_recorded`。每 run 最多 5 条、16 KiB，超限返回结构化拒绝（`LESSON_LIMIT_REACHED`/`LESSON_LEDGER_BYTES_EXCEEDED`，`effect_outcome=not_started`），不静默丢弃。拒绝码借用已登记的 `TOOL_GUARDRAIL_DENIED`/`TOOL_INVALID_ARGUMENTS`，因为错误分类表不归本线维护。
  - **暴露**：与 `capability_request` 同一路径。注册表默认隐藏，主线程工具面、tool_search、list_tools 都看不到；直属子代理的 coding/read_only 预设、角色默认工具和层级调度缺省候选都带上它。主线程没有 child run，调用返回 `TOOL_UNAVAILABLE`。要关闭，在 owner 工具策略 `disabled_tools` 里加 `record_lesson`，子代理的 allowed_tools 和提示行会随之去掉；不另加配置项。
  - **结果收口**：`runner_result_service` 在提取阶段读回账本，逐行复核版本、字段、id 与 run 归属，最多采用 5 条。之后与结构化输出的 `lessons` 去重合并（结构化在前），写进 `output.json` 和 runner result 的 `lessons`/`lesson_count`。自然回复没有结构化输出时，账本经验也会记成 `subagent_lesson` 候选：正文用固定四行模板，`applies_when` 取 `when_to_use`，证据引用 `lessons.jsonl#<lesson_id>` 并带 task/run/attempt。
  - **下游**：`enable_self_learning` 开启时，S1 照原链为每条经验生成一个待确认提案。重放不重复：候选靠 observation_id，提案靠 O_EXCL。候选记录失败只写工作日志，不阻断结果交付。
  - **提示**：Runner Contract 只在授权含 `record_lesson` 时多一条可选软引导，不恢复任何状态 JSON 要求；宿主从不解析回复正文。
  - **留给后续**：主线程的经验记录；被取消 run 账本的收取（账本保留，但取消路径不经结果收口）；S1 草稿 `when_to_use` 仍写"来源任务目标："，而账本候选的场景其实是 `when_to_use`。证据见 [TESTS](TESTS.md) 顶部本节。
- **插件层"观察候选"结构已实施（main `c577ed185`，已部署 `runtime-step11d-16b108bd`；manifest v5、代理结果路径、runtime_events 新鲜度、browser-lite）；决策线 `action_candidate` 点已接入并真实验收（2026-09-25，本机隔离 owner 上用 browser-lite 做 off/observe/apply/过期四档，随分支 `claude/decision-action-candidate` 合入，基于 main `6a50d84aa`）**（原稿 2026-09-25，第 15 项剩余点，依据[动作候选审计](docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md)；完整设计稿见[插件观察候选结构](docs/design/PLUGIN_OBSERVATION_CANDIDATES.md)；决策侧见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-动作候选-action_candidate)）：
  - **设计稿要点**：manifest v5 在只读工具上声明 `observation`、在动作工具上声明 `observation_ref`；插件在 `structuredContent.my_agent_observation` 给出目标、代次与有限候选；宿主整份校验后铸 `observation_id`/`candidate_id`，写进该次调用原归档的 `tool_result_envelope.observation`（唯一权威），并在模型可见投影里改写为带 candidate_id、隐去插件 key 的有界候选；新鲜度按 `tool_operations` 调用序，run/task 内的后台续跑共享观察；动作执行前宿主按调用序查新鲜度、经 `_meta` 附代次与 key，插件再按页面代次复核；决策点 `action_candidate` 只选一个别名并追加软提示。
  - **现状**：插件线已合入 browser-lite 与 desktop-lite。browser-lite 的 `read` 会返回有限元素清单（标签、文字、name/id、是否可见），`click`/`fill` 按唯一匹配的选择器执行；但宿主没有经过验证的 `observation_id`/`candidate_id`，也没有观察内容哈希与代次。
  - **原则**：不能为 browser-lite 写专项解析，这会违反禁止专项合同的铁律；也不能用截图坐标、自由文本或工具名推荐冒充动作候选。
  - **方向**：
    - 在插件 SDK 的工具结果合同里增加可选的通用观察候选字段（插件声明哪个只读工具会产出候选），宿主校验形状后生成本地 `observation_id`（绑定 run/task/调用、结果哈希与代次）和有限的 `candidate_id`。
    - 决策点只从这些 ID 里选一个、给软提示；真正执行仍由原工具按原审批执行。
    - 执行前按页面或窗口代次复核候选是否仍然有效，失效即丢弃。
    - 这需要主线 owner（插件线）先确认 SDK 字段，再由决策线接入点。
  - **真实验收的发现**（2026-09-25）：
    - 已修复：只读工具的归档没有 `runtime_gate`，`persist_tool_runtime_ledger` 原先因此提前返回，`tool_completed` 事件一条不写，观察新鲜度恒为 False。插件线已在 main `6a50d84aa` 改为完成事件总写，决策侧集成测试也改走这个真实写入口。
    - 插件线已处理（2026-09-24 深夜，详见[插件观察候选结构](docs/design/PLUGIN_OBSERVATION_CANDIDATES.md)第 6 节）：三处宿主行为都是既有设计，只改了 browser-lite 工具描述与 README（写路径不写 `file://`、相对路径按会话工作区根、http(s) 需插件与宿主两道门）。显式 `file://` 的正确入口是将来 manifest 通用的 `local_file_url_parameters` 声明，不为单个插件放松 URL 门。
    - 主线评估（2026-09-24 深夜，已核对源码，未实施，待用户拍板默认值）：模型经后台进程起的 `python3 -m http.server` 默认监听所有网卡，Gateway 停止后仍在运行。事实：(a) 受管后台进程按设计脱离 Gateway 进程组、跨工作片存活，只有显式 `/stop` 按 owner/task/run 身份回收；`gateway stop` 不触碰任何 process session，存活是设计行为不是泄漏；(b) 监听地址事实只在 `background_process` 的 `status` 动作按端口查询时由 `process_network_status` 观测，启动回执不带监听范围，模型和用户在启动时看不到"绑了所有网卡"；(c) 该模块非目标已写明不按命令正文判断进程状态，所以不能按 `http.server` 文本拦。可选方案：① 启动握手完成后一次观测监听并把 `network.reachability` 写进启动回执与 runtime 事件（只记事实）；② owner 配置 `background_process_listen_scope`（`loopback_only`/`any`），按 socket 事实在启动后结构化停止越界会话并返回稳定错误码；③ Gateway 停机把仍存活的后台会话（数量、监听范围）写进停机事件与 `my-agent status`。**③ 已实现**（2026-09-24 深夜：事件 `gateway_background_sessions_surviving`、state `surviving_background_sessions`、status `background_sessions_after_stop`，见[受管后台进程会话](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md)）；① 因启动瞬间服务往往还没绑定端口而不可靠，不做；**② 已按用户决定实现**（2026-09-25）：默认 `loopback`，模型要开放局域网必须在 `run_command` 里结构化声明 `background_listen_scope=lan`，首次由用户在审批面板确认并可选"本用户长期允许"（新增通用机制 owner operation grants：`ApprovalPolicy.owner_grant_parameters` → binding.grant_key → decision `approved_owner` → owner `tool_policy.json.operation_grants`，自主模式不放行未授权的这类调用）；host 每 2 秒按真实 socket 表核对，越界即回收并留证据（配置 `background_process_listen_scope_enforce`）。见[受管后台进程会话](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md)「监听范围」。
- **自学习 S2：待确认 Skill 提案的审核顺序 `skill_proposal_review`（第 15 项 P5-C）**（2026-09-24，已合入 main `1132fd9d0`；2026-09-25 随 `record_lesson` 做了真实 Jev 验收，端到端真实验收见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)「15 自学习 S1/S2 端到端真实验收」一节）：
  - **接线与开关**：新增独立 `owner_background` 接入点，默认 off，只在 `my-agent skills proposals list` 运行。AgentConfig/YAML 三字段 `decision_skill_proposal_review_mode/_timeout_seconds/_profile_id` 与原设置服务、TUI 菜单共用，只允许用户长期（owner）设置。配置归 AgentConfig：此点只排展示、不授予 Skill/工具权限，和 `enable_self_learning` 同属主配置，CLI 也只加载主配置。审计里暂称 `self_learning`，改名以表明只管审核顺序。
  - **触发与材料**：待确认提案 2—30 条且本点 observe/apply、总开关开启时才请求；0—1 条或关闭时零请求、输出逐字节不变。不要求 `enable_self_learning`（它只管生成）。外发只有 `proposal_i` 别名、创建顺序、来源计数，以及经 `external_data/default` 投影的 description、when_to_use 和 240 字经验摘录；提案/候选/任务/运行编号、路径与目标 Skill 名只进本地版本摘要，草稿 hash 不符则不发。
  - **采用**：每条一道 choice（`review_first/normal/review_later/possible_duplicate`，非选择 `not_needed/need_data/abstain`），缺题、多答、逐题错误或任一非选择整体保留原序。采用前核对候选版本、配置与期限，并重读待确认提案比对编号、版本、状态和草稿 hash。稳定排序 review_first→normal→review_later/可能重复（同组），只动待确认提案的位置，附宿主固定标签；`--json` 增加 `review_order` 块。
  - **边界**：observe 只记账不改输出；任何失败、冷却或变化都保留原输出，取消与中断上抛。从不确认、拒绝或写提案/Skill，没有模型可调用入口。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-自学习-s2待确认-skill-提案的审核顺序-skill_proposal_review)。
- **决策实验授权入口、经验输入上界与发送硬门**（2026-09-24，已合入 main `dfa8e498b`；2026-09-25 两次真实授权发送的结算额都等于供应商计费，比例 0.43；结算快照尚未持久化，列入 E2）：用户于 2026-09-24 批准接受**经验（非供应商保证）**的实验输入上界，并要求明确标注为经验值。三部分：① `/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>` 与 `/audit … prepare` 同一任务命令机制，参数冻结进排队请求、模型只见任务正文；主轮发布 run/attempt 后、首个模型调用前在同一精确回合锁内写 `experiment_grant` 回执并调用 E1 授权原语，重放不再授权、Compact 再入与重启分别以身份/账本代次失效；信封升级 v2，必带 `input_bound_policy="empirical:jev_wire_bytes.v1"`，缺者永不发送。② 经验上界 `C = ceil(B/2) + 256×Q + 1024`（B 为最终 wire 字节），只在 skill_tool、Q≤64、state≤4096 字节、C≤57,600 内使用，越界不预留不发送；原账只接受带 `kind=empirical` 标签的上界对象，估算/上界/实际分开记。③ 传输层在最终字节生成后、任何 DNS/连接/遥测前调用单次发送许可，复核绑定、撤销/期限、设置/身份/账本代次/口径与连接代次后在原账锁内消费；拒绝不重试、不触发连接退避。结算：成功按实际扣减，发送前拒绝与未知结果都不退款并关闭预算，无许可的 HTTP 记 `gate_bypassed`。`experiment_enabled` 默认仍关闭，首次真实授权发送尚未进行。合同见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-e1-有界自测授权入口经验输入上界与发送硬门2026-09-24本地实施待审)，交接见 [E1 交接](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第二片experiment-授权入口经验输入上界与发送硬门2026-09-24)。
- **决策实验对照记录、证据评估与授权内自动晋升（P5-E2/F1）**（2026-09-25，已合入 main `84d873c9b`；拒绝与正向晋升两条路径都已真实验收，见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 17 项两节）：① E2：实验调用经原账结算后，`settle_input_budget` 返回的结算视图（快照＋结算码/调用编号/原估算/声明上界）经实验调用对象带回，挂到 `DecisionOutcome.experiment`；只观察路径据此生成 `decision_experiment_record.v1`（身份 refs、授权/设置/策略/连接版本、基线=点关闭时实际展示的工具名集合、候选=Jev 回答按 apply 同一规则投影的短名单/延迟名单、结算视图），经能力观察出口拆出写进同一请求记录的 `experiment_records`（按原调用编号去重、最多 8 条、盖执行代次，回合关闭/停止时不写）；回合正常收尾才按结构化工具账补写实际调用工具名，非 completed 或工具账不完整记 known=false。没有 token 表、旁路恢复文件或第二本账，普通请求零 I/O。② F1a：只读评估器只读这些条目（按 owner/thread 核对）；可比较样本≥3、窗口（最近 8）内全部 charged、每个可比较样本短名单召回=1.0（快照外工具不计分母）且延迟数>0 时才提出 `points.skill_tool.mode off→apply`，否则 keep_observing 与原因码；阈值是审计规则常量，不设配置。跨请求证据只沿授权回执 v2 的 `previous_request_id` 回读原请求记录（至多 16 条），`user_config decision_read` 在已有授权信封时附只读 `experiment_evaluation`。③ F1b：`/experiment apply skill_tool …` 的信封 operations 为 observe+apply，实验调用仍只观察；回合收尾在 apply 授权内于精确回合锁复读设置，核对授权仍为本请求、active、未到期、revision 与授权时一致、点仍 off，再经原设置 patch 的完整 CAS 写 thread 覆盖；冲突或任何用户后改都跳过不覆盖。回执权威选本请求记录的 `experiment_records.promotion`（与证据同处；信封是纯授权且会被下一次授权整份替换）：先写 promoting 再改设置，已有回执即不再试，崩溃遗留 promoting 表示不确定、不重试不恢复。到期/撤销不回滚已晋升设置，reset 恢复继承；Jev 回答与模型工具都没有授权或晋升路径。合同见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-e2f1-对照记录证据评估与授权内自动晋升2026-09-25本地实施待审)，交接见 [E1 交接第三片](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第三片e2-对照记录f1-证据评估与授权内自动晋升2026-09-25)。
- **交付复核焦点真实样本暴露的四个缺口**（2026-09-25；第 2—4 项当时在分支 `claude/verification-exit-scope-chains` 实施、待审；第 1 项的写入触发已由 J10 于 2026-10-01 实施、待集成，见下一条；样本见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 15 节）：
  1. **触发时机**：原来只在新的验证事件上触发，漏掉"最后一次验证之后又改文件、未复核就交付"。J10 已在成功写入让已有焦点变 stale 时评估一次，仅在多个 stale 焦点间软选复核顺序；不实现回合收尾触发，不增加强制续跑或完成门。
  2. **`&&` 串联的验证命令**：返回码 0 其实证明每条都通过，可以逐条记为 passed；非 0 无法归属，仍不记。
  3. **返回码 126/127**：命令没有执行（找不到命令或不可执行），应记为"未运行"而不是测试失败。
  4. **范围判断**：`pytest tests/` 被记成 targeted。已按主线 owner 的决定改为按参数形状判定：目录或不给路径为 full，文件、`::node` 或筛选开关为 targeted。
- **已实施（2026-10-01）：交付复核焦点在"改后未复核"时触发**（设计于 2026-09-25；J10 分支 `worker/sol2-j10-delivery-stale`，2026-10-02 本地复核，待集成与 be 真实 Jev 复测）：
  - **问题**：旧版只在新的验证事件上触发，漏掉"最后一次验证之后又改文件、然后直接交付"；本片补成功写入的原展示钩子。
  - **触发事实来源**：写入工具的结构化 `verification_state`，即其中带 `status=stale` 与 `last_verification_id` 的行。它由 `record_tool_verification` 在成功写入后产生，与现有焦点来自同一份归档信封；不读正文，也不看模型"我已经测过"之类的说法。触发条件：当前写入记录让某个已有焦点变成 stale，且本轮焦点满足现有 2—12 个的门槛。只有一个 stale 焦点时，不需要 Jev 挑选。
  - **与完成门的关系**：宿主没有、也不增加机器完成门。提示仍只追加在当前工具结果之后，可忽略；不强制续跑，不改最终回复，不写 Todo 或收口状态。模型可见的运行事实本来就带 stale 状态，本增强只负责在多个 stale 焦点里挑先复核哪个。
  - **边界**：每条写入记录至多一次请求，并与 run_command 触发点共用"每条记录一次"的约束；子代理、重复失败或未知副作用收口时不触发；设置、来源或期限变化时丢弃建议，与首片一致。
  - **落实**：当前 canonical `handler_details` 与归档信封的状态必须配对，stale 引用须匹配较早的同 root 焦点；只有多个 stale 才请求且只提供 stale 候选。去重只存本轮参数集合，不参与工具幂等、归档或恢复状态；验证与变异见本台账顶部及 TESTS。
- **验证账只认一次返回码能证明的单条命令，并放行开头的 cd 前缀**（2026-09-25，已实施：分支 `claude/verification-command-shapes`，已合入 main `e7178a22c`）：真实 TUI 里模型最常写 `cd <项目> && python3 -m pytest …`，原分类把它当链式命令整体拒绝，验证账漏记真实测试，交付复核焦点与交付前核对都看不到；单个管道又没被拆段，`pytest | head` 会按 `head` 的返回码记成 passed。现改为：未加引号的 `|`、`|&`、`&` 一律不算证据；只放行开头一个 `cd <可进入的现有目录> &&` 并以其为 cwd；其余链式写法仍拒绝。详见 [verification 进度](docs/modules/verification/02-progress.md)。
- **TUI 决策菜单的接入点清单改为取 schema 登记**（2026-09-25，已实施：分支 `claude/decision-tui-points`，已合入 main `171caa21c`）：菜单原先自带一份接入点清单，漏了 `pre_recall`，界面无法设召回前补充查询，且已有该点覆盖时"恢复继承"列表会抛 KeyError。现在清单直接取 `decision_settings_schema.POINTS`（唯一权威），本地只保留中文显示名，缺显示名时显示原键。今后新增接入点只需在 schema 登记，菜单自动出现；各分支若新增接入点，只需补显示名。
- **交付复核焦点 `delivery_quality`（第 15 项 P5-C 质量提示首片）**（2026-09-24，已实施：本地分支 `claude/decision-delivery-quality`，已合入 main，P1-P5 goal 已随 `313f5dd23` 关闭）：`run_command` 刚产生新验证事件、本轮同 run/task 有 2—12 个验证焦点（每个 project/kind/scope 只留最新一条）且至少一个 failed 或其后有修改时，可选地请 Jev 选一个交付前最值得先复核的焦点，宿主只把该焦点的编号/kind/scope/status/其后修改渲染成一句追加提示（≤512 字符），text/native 共用。外发材料只有脱敏当前请求和焦点别名事实，不含路径、命令或输出；默认 off，observe 只记账不追加，任何非成功、选中本次事件或来源/配置变化都保留原展示；ToolResult、归档、验证账、Goal、Todo 与收口不变，取消照常上抛。接线沿外部材料首片：`_record_tool_call` 的原展示接缝改为 `_optional_result_hints` 依次调用两个按工具名互斥的点。未做真实 Jev/TUI 验收。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-质量提示首片交付复核焦点-delivery_quality)。
- **自学习 S1：子代理 lesson 生成待用户确认的 Skill 提案**（2026-09-24，已合入 main `e9ead5ae3`；2026-09-25 随 `record_lesson` 做了端到端真实验收；2026-09-26 起自学习开启时新提案改为以 `confirmed_by=auto` 自动确认，见顶部“自学习 S3”条目，下文“只能由用户确认”描述的是当时语义）：`enable_self_learning`（默认 false，YAML、AgentConfig 与布尔规范化同步）开启时，组合根给子代理 manager 接上 `capability/skill_proposals.py`；runner 结果记录 lesson Candidate 之后，只把 `subagent_lesson`、同时带 task/run 来源、状态有效且未脱敏的候选按固定模板（不调模型）渲染成提案，O_EXCL 幂等写入 owner 路径解析器登记的 `<owner_home>/data/skill_proposals/<proposal_id>.json`（`proposal_id` 为 sha256(candidate_id + content_hash) 前 24 位；目标 `lesson-<正文 hash 前 12 位>`、`before=absent`；提案写明来源任务/运行、触发原因、拟保存内容和适用场景），生成失败只写工作日志、不影响结果交付。正式 Skill 只能由用户 `my-agent skills proposals confirm <id> --expected-revision N` 写入：owner 锁内复核版本与待确认状态、草稿 hash、来源 Candidate（存在、未脱敏、hash 未变、未被拒绝/替代/过期/阻塞）、目标不存在，再在临时目录经 `parse_skill_file(require_frontmatter=True)`、`scan_skill(source="agent_generated")` 与 `install_decision`（不 force，caution/dangerous 均拒）后 `os.replace` 到 `<owner_home>/skills/<name>/` 并标 committed（revision+1）；任何失败不写目标、提案保持待确认并返回结构化错误码，写回执失败会删掉刚装的目标。目录刻意不叫 `learning_drafts`：Curator 每次持 lease 前的 Memory 迁移会递归迁走并删除该名字的目录。没有任何模型可调用的确认工具；开关只控制自动生成，已有提案仍可在 CLI 查看/确认/拒绝。S2（Jev 对待确认提案的审核排序）见本台账顶部“自学习 S2”条目；两者的端到端真实验收见 `record_lesson` 条目。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)自学习段与 [TESTS](TESTS.md)。
- **召回前补充查询与关系提示的真实收益，以及随之发现的四个缺口**（2026-09-25，真实验收已完成：main `ab23a2666` 在测试机隔离目录，见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#14-p5-a-召回前补充查询语义召回下的真实收益2026-09-25main-ab23a2666)；缺口 3 仍只记录方向，缺口 1、2、4 已实施，见各项）：
  - P5-A：在语义召回下，3 条已知漏召回样本中 2 条稳定补回（4/4 次），答复从"查不到"变成准确事实。
  - P5-B：关系提示让真实 M2.7 提取把"换车"稳定归为 `long_term_fact replace` 并指向原条目（off 两次都是 `user_profile`）；其余两类无稳定差异。
  - 两点都默认关闭。发现的缺口如下：
    1. **补充片段选择缺客观材料**：第 3 条样本 Jev 两批都选了主题已被基线覆盖的片段。方向：宿主把"各片段能否新增记录"作为结构化事实交给 Jev，或在有空槽时按阈值确定性补位。先评估额外嵌入开销和弱相关事实混入的风险。**已实施前一种（可选，默认关）**（2026-10-02，J8，分支 `claude/be-jev-snippet-facts`）：`points.pre_recall.fragment_material=with_new_facts`，见本台账顶部同名条目。
    2. **主模型没有长期事实检索工具**：工具面只有 `remember`、`session_search`、`search_text` 等，正式召回漏掉的事实对主模型不可达，off 轮它自查也查不到。是否补一个只读、按 owner/scope 约束的检索工具需要单独设计，与自动召回的权威边界一并考虑。**已实施（可选，默认关）**（2026-10-02，J9，分支 `claude/ae-j9-memory-tool`）：只读工具 `memory_search`，开关 `enable_memory_search_tool`，见本台账顶部同名条目。
    3. **语义检索每次对全部事实重新嵌入**：`_search_scoped` 对 active 列表整体调嵌入端，补充查询使嵌入量翻倍。事实多时需要按正文哈希缓存向量；缓存只能是派生索引，正文仍以 JSONL 为准。
    4. **关系对按顺序截取前 32 对**：`decision_curator_relation` 取消息×正式条目笛卡尔积的前 32 对，不按相关度，后面的消息比不到。**已实施**（2026-09-28，分支 `my-agent/self-dev-4`，基于 `54880f8e9`）：改用标准库 BM25 词面相似度给每对打分，按分数从高到低取前 32 对，同分保持原枚举顺序；不引入嵌入调用、不增加网络请求和费用。覆盖范围仍如实声明为"仅展示的对"，并在 `state.coverage` 里新增 `total_pair_count`（全批笛卡尔积对数）、`selection`（挑选规则标识）与 `selection_limit`（上限），下游可据此看出未比过全部。缺口 3（语义检索重复嵌入）仍待办，挑选只用词面、不新增嵌入。
  - 同一实验还发现一个与决策无关的 Memory 问题：新 owner 首次整理时，v2 迁移把 memory.md/HOT 模板的标题行生成两条 `migrated_legacy` 待审候选。已告知主线 owner，归 Memory 模块处理。
- **召回前补充查询的证据与收益前提**（2026-09-24，证据已合入 main `ab23a2666`；语义召回真实实验见下一条）：只用词面检索时补充查询按构造补不回任何事实（片段词一定在整句里、BM25 已返回全部正分文档），真实收益只可能出现在语义召回下；上下文包 `memory_refs` 新增 `recalled_refs` 与 `recall_findings`，只写文件、不进提示。详见[召回前审计](docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md)。
- **主会话选模候选补用户授权的用途说明**（2026-09-24，用途标签已合入 main `4ec0e11f3`；采用模式问题说明已随 P5-D 交付（`d9a34fc76` 关闭 item 16）；带标签的第五个真实样本仍选当前模型，修正问题说明后的第六个样本真实跨模型采用成功并答对；实现口径见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)第 4 节 Stage C 段。字段只登记在 `validate_model` 与快捷新增白名单，不进 `resolved_model`：其结果会整体覆盖 AgentConfig，而运行时没有标签消费者；主线 owner 已同意字段形状，`input_modalities` 将按同一格式并列）：四个真实样本里 Jev 都没有建议换模型，其中一个任务明显超出当前模型的声明窗口。原因在于候选材料只有模型名、后端和声明窗口，指令又明确要求"候选声明不是实际能力证明"，Jev 缺少比较语义匹配的依据；而容量本就由宿主判断。方向：模型档案增加由用户显式填写、结构化的用途标签（例如长文档、视觉、推理、低成本），只作为 Jev 的语义材料，不作为容量或授权的证明，也不由模型自述或名称推断。这一项与媒体屏障的"视觉能力事实"都会动 model_profiles schema，须与其实施者（重构线）统一字段后再落。证据见[主会话真实交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。
- **能力推荐的结论与展示结果写进请求记录**（2026-09-24，已实施：本地分支 `claude/decision-capability-observation`，已合入 main（P4-G 真实验收随 `7143ef631` 关闭 item 13）；主线 owner 已同意位置与形状，实现见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)能力推荐段）：在第 13 项的多 owner 真实样本中发现，`recommend_capabilities` 算出的 `finding`（决策状态，例如 `skill_tool_decision:apply:cooldown`）以及展示短名单、延迟名单只存在于运行内存里。已落盘的只有调用层终态（线程 model_usage 的 decision 分项：failed/timed_out/finished 与输入用量）；没有发出调用的结果（冷却、关闭、过期）和采用结论都不在请求记录、线程指标或归档上下文快照中。真实样本因此无法核对结论是否被采用、为什么保留原样（挂起样本里只能从系统提示摘要的变化间接看到采用痕迹）。方向：把这组结构化事实（状态码、候选版本、短名单与延迟名单的名称或摘要）写进 Gateway 请求记录，位置和口径与主会话选模的 `model_selection_observation` 相同；只用于观察和展示，不参与路由或采用判定。证据见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#13-多-owner-与断网真实样本2026-09-24main-4163e66c0)。
- **子代理自动选模提交阶段记录结构化原因码**（2026-09-24，已实施：本地分支 `claude/decision-child-commit-reasons`，待审）：提交阶段此前把目录锁占用、目录代次变化、父线程锁占用或缺失、设置/task/权限变化、期限和 child 线程冲突都记成 `selection_changed`，第四轮真实样本因此无法归因。现每个失败点各有原因码，写进 child thread 建议的 `retained` 记录；只改观察口径，采用/保留的判定与锁序不变。原因码清单见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)第 4 节子代理选择段。
- **决策连接连续失败时逐步加长冷却**（2026-09-24，已合入 main `3933b1db1` 并随 step10k 部署双机；测试机真实复测通过，见[退避复测](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#13-冷却退避的真实复测2026-09-24main-3933b1db1)）：第 13 项挂起样本中，超时和瞬时失败的冷却固定 30 秒，只能挡住窗口内的轮次；供应商持续挂起时，冷却一过的第一轮又要付一次完整期限（样本中多等约 6–7 秒）。实现：同一连接键（owner、profile、连接修订）冷却过期后的重试再失败时冷却翻倍（30→60→120→240 秒，封顶 300 秒，与额度一致）；冷却期内才返回的并发失败算同一次故障、不加级；连接返回过响应、连接测试成功或显式重试即复位；配置错误仍等配置修订，额度仍 300 秒。只改 `decision_policy` 的进程内冷却表，不新增持久状态或配置；合同见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#42-已实现的可选服务边界)。证据见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#13-决策端挂起的真实样本2026-09-24main-4163e66c0)。

12.4 第二片 2a 已由主线 owner 审阅合入 main `931f83739`，已随 `d69f30cf3` 同版部署双机：
- `ConversationHistorySeed` 的具体历史与只读来源严格二选一。来源只冻结同次地址视图、宿主原单行规则和投影时刻，只在原 native/text 准备边界解析（文本协议每轮一次，原生在建立循环和运行内重建时各一次），多次解析结果相同；capture 和纯投影合同不变。
- 代价：canonical 文件在整个运行期间都不能改写（追加合法），改写、截短、替换或删除时下一次解析明确失败；每次解析按地址重读，4.2M 字符下文本约 34ms、原生约 65ms。
- 4.2M 字符来源下，种子准备后驻留从约 8.5MB 降到 32–54KB；运行结束后的驻留减少 8.4–9.4MB；Gateway 首次发送前峰值从 29.8MB 降到 21.3MB。
- 摘要期峰值约 10–11MB 不变，属于 2b（旧请求释放）。

后台上下文预算只估算将渲染的节（已合入 main `5dcdfd463`）：渲染开关作为结构化 `rendered_keys` 传给预算；有历史种子时不渲染的最近消息不再挤占总预算，也不再保留正文。后台回合各阶段约少 1.34MB，可见运行事实不再被过度截断。详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#后台上下文预算只估算渲染节有种子时不保留最近消息2026-09-24本地)。

决策线能力推荐按插件分组出题（已合入 main `a2e26178b`）：只按结构化 `ToolModelHints.provider_id` 把同一插件的工具合成一题；选中展开全部成员，未选中整体延迟；内置工具与 Skill 仍逐项。内置插件 21 个工具下题数 21→8、题目大小 −36%。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)。

12.4 第二片 2b 已由主线 owner 审阅合入 main `911d0d14d`，已随 `d69f30cf3` 同版部署双机：恢复宿主决定摘要后解绑旧请求的完整原生历史（原参数与 frozen，只解绑这一对象，不原地清空）。4.2M 字符下摘要期峰值从 10.2–11.7MB 降到 1.7–3.3MB。建循环时的首次物化峰值仍在；实测每次请求只读一次完整历史，改为按需物化只会挪动峰值、不降峰，所以不做（方案 B 结论，主线 owner 已同意）。Gateway 的 21.3MB 峰值另有来源：索引、近期产物和追加去重三处按行数而不按字节整块读取消息文件，行大时等于整份文件；方案是字节有界的倒读流式行迭代器（已实施：本地分支 `claude/decision-gateway-message-reads`，主线 owner 有条件同意，待审；Gateway 准备期峰值 21.3→0.8MB，全程 22.7→10.0MB，与 child、后台持平），见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#gateway-213mb-峰值来自整块读取消息文件2026-09-24已实施待审)。详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#摘要期释放旧请求历史2b2026-09-24本地)。

恢复候选提交后的同请求重试（已合入 main `c9794b9ca`，已随 `d69f30cf3` 部署双机）：候选发送瞬断后，重试原样复用已提交的（候选参数，prompt），不再在压缩前的原参数上重建。记录按原参数对象身份命中，换参数即清除。`_model_turn_or_retry` 的空响应修复和插话取代两个重跑分支也改为先换成候选参数，不再在原参数上重建或注入。详见[依赖拆分](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#恢复候选提交后的同请求重试决策分支2026-09-24本地)。
候选参数与原参数共享同一个 `tool_context` 列表（`replace_recovery_history` 的既有行为）；2b 已把它写成显式合同并加断言测试，行为不变。

详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#todo124-宿主历史来源与请求生命周期2026-09-23实施中)和[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#宿主历史种子只读来源2a2026-09-23本地)。

三宿主来源视图仍在seed准备时重新物化：相同约4.2M字符输入的准备峰值约8.6–9.0MB，第二片需延后物化并处理旧请求持有者。child无正文展示已移除提前全读；范围、纯投影和完整发送边界保持，详见[宿主基线](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#宿主历史种子生命周期基线2026-09-23第二片进行中)。

12.4选中正文生命周期首片已本地实现：在原canonical消息文件上复用固定尾界和行hash，只保存临时地址视图，贯穿来源、分区与摘要读取；不新增持久索引、摘要权威或截断策略。宿主完整请求的冻结与释放另作伴随片，纯请求投影仍禁止读文件。真实文件到摘要/CAS内存红绿及独立审查已完成，不能视为三宿主全链内存验收，见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#选中正文生命周期方案2026-09-23首片本地验收)。

原生历史只在canonical读取边界做必要深拷贝，后续只读投影接收该独占副本；匿名重复输出各自隔离。token估算复用主线有界小JSON直接编码/大JSON流式选择，异常顺序和数值保持；本地已实现，无持久状态或新配置。第12.4全链有界仍待完成，见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4检查点读取使用同次描述符内的临时ID→行地址/hash，原文件及thread head仍唯一权威；全部行先解码检查，已提交链逐条验封后只保留适用摘要及覆盖元数据。无持久索引、缓存或新开关，已本地实现，整体有界来源仍未完成。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.6本地组合验收已完成，原目录/线程CAS/容量门和校准账仍唯一；扫描修复保持原Unicode空白与损坏分类，不增加配置或持久索引。12.4全链有界与12.7真实缓存未完成，见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

Compact通用底座按函数级闭包向主线移植，自动选模、Jev、菜单及decision配置不是必要依赖。扫描/估算/摘要窗口候选已在主线临时副本验证；scoped checkpoint、摘要基础链、宿主同源恢复与容量门须成套审查，完整保留投影最后接线。当前为交接候选、未合并，详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4摘要字符来源采用只读两遍编码：总字符/hash与顺序当前窗口，不落临时文件或新增索引。共享tokens沿原估算语义流式累计；分段修复提示统一预留并在发送前复验。已实现并通过316项联合，全链来源及覆盖尚未有界；见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4保留历史完整投影已本地实现：显式Compact来源及候选不再套普通字符窗口，三个宿主共用原容量门；超量/未知拒绝而不删原文放行。普通展示规则保留，详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4固定来源范围筛选已本地实现、待主线接口集成：两遍同EOF原字节校验，先解析完整锚点再保留范围内未覆盖正文，后台operational和native共用谓词；ID位置与未压正文仍常驻，完整有界Compact尚未完成。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

超大canonical历史有界化已完成依赖审计，原消息固定尾界/字节页和幂等流式扫描底座已本地153项通过：须同时约束原消息页、scope筛选、checkpoint覆盖链与幂等扫描，不能仅改limit或截断来源。后续沿原游标与CAS设计连续范围证明，详情见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

决策模型媒体整合本地1419项联合与严格gate通过：自动选模/Compact共用原内容完整性检查，未知模态保留原模型和原始历史；强制恢复不凭附件引用取得容量或摘要覆盖。transcript只覆盖安全文字前缀，完整媒体后缀保留。媒体原提交的M3证据与当前集成版分开，12.4仍开放，详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4 集成版媒体真实验收已通过（main `5dcdfd463`，.9 官方 M3，2026-09-24）：图片原样进入请求；手动压缩只摘要图片之前的文字前缀，图片轮原样保留；压缩后模型仍能依据原图作答。见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#124-媒体与-compact-集成版真实验收2026-09-24main-5dcdfd463)。

媒体会话越过压缩点的修复（已合入 main `d69f30cf3` 并部署双机；测试机真实验收已通过）：
- 问题：未压历史带图时，估算一越过自动压缩点，请求就整轮失败，即使离窗口还远。原因是 preflight 仍按压缩点报溢出，而自动和轮内压缩遇未知模态会跳过、强制恢复又拒绝。
- 修复：请求的文字容量不可知时，preflight 只守窗口硬上限；越过窗口时，强制恢复报结构化码 `COMPACT_REQUEST_NON_TEXT`，不再与内部投影失配共用 `COMPACT_REQUEST_PROJECTION_UNKNOWN`，客户端文案据此说明是图片等非文本内容所致。`save=false` 与纯文字会话行为不变。
- 详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#媒体会话越过压缩点2026-09-24本地修复)。

- **媒体屏障已定方向**（2026-09-24，用户拍板，决策线已评审并入；片 A 归档引用主链已本地实现，片 B/C 待做）：A（旧媒体降级为可重新附上的归档引用、只摘要文字）是主链与默认；B（含图轮次随文字进摘要）只在模型视觉能力事实为 supported 时启用，事实只能来自档案字段 `input_modalities` 或宿主的结构化纯色图探针，不来自模型自述；B 失败不在同一请求内退回 A，只由线程上持久化的失败码在下一次压缩切换。三处宿主门把"能否摘要"与"能否计量"分开判定。详见[媒体压缩策略](docs/design/COMPACT_MEDIA_POLICY.md)。

长对话验收方法已按用户要求调整：用 my-agent 自主完成真实 GitHub 项目跨语言实现产生自然历史；合成大文件仅保留存储边界定位用途。官网 M2.7 的 fd→Python 原生 TUI 验收待完成，方法见 [TESTS](TESTS.md#真实开发长任务验收方法)。

TUI 原生媒体输入已实现、专用测试机官网 M3 验收通过：内容寻址原件、owner 校验、草稿 refs、发送边界编码与历史媒体预算复用原主链。旧纯文本记录无需迁移。详细合同见 [TUI 图片视频](docs/design/TUI_INPUT_MEDIA.md)；候选未默认部署。

- **插件声明式面板与只读订阅（第 9 步，实施中）**：包描述 v2 可选声明至多 2 个面板（text/table/status），只订阅核心公开主题 activity/run_state；无面板的包仍按 v1 序列化，已安装包描述字节不变。Gateway 进程内唯一展示服务只读已有活动投影，经固定激活代次的插件连接调用只读 `my-agent/display.render`，每个激活 1 条连接、1 个在途请求、输入只保留最新，结果按类型校验截断；渲染前后复核代次，停用/换代立即丢弃并关闭连接，空闲 120 秒关闭。TUI 只渲染核心校验结果，插件代码不进 TUI。详见 [插件展示](docs/design/PLUGIN_DISPLAY.md)。

- **委派与交付核对软引导（2026-09-23，已采用，实现中）**：第8步新版 TUI229/233 中，部分孩子没有用工具计算，而是心算出合计，写出错误数字却标注"通过"；父级也没有回核，最终报告失实。同一需求由主代理用工具实际计算时（TUI228）完全正确。参照 Codex 的 `spawn_agent`/worker 合同（默认不委派、委派要收窄到具体产出、孩子交回后审阅再整合），只调整软引导文字，不加完成门、不解析回复文字、不按 CSV 或提示词加专项分支：①父级：用户或项目说明没有要求委派时，优先自己完成，只把边界清楚、能并行的部分交出（能力和默认行为不变）；派工时写清要交回的具体产出和核对方式；收到结果后，先用工具对照原始资料抽查关键数字或改动，再整合，不直接转述下级的"通过"。②孩子：最终回复中的数字、统计和核对结论必须来自本轮实际执行的工具输出，并说明出处；没有执行过的检查明确标为"未核对"。没有采用结构化工具用量统计（Codex 也没有），软引导不足时再评估。复测（7c467a4d3）：TUI238 与 233 同题，24 个孩子都用工具计算，全部产物正确（上一轮 10/24 出错）；TUI239 与 229 同题，孩子和父级回核都正确，但父级自己新算的状态小计仍靠心算出错，并编造理由解释矛盾。所以第③条放进所有代理共用的 `prompts/default.md` 证据段：数字来自工具输出；表内不一致时回到工具重算，不编造解释。"没有要求时优先自己做"的折中引导没有阻止自发委派（238 仍派了 24 个孩子），目前只作观察，不升级为默认禁止。

- **第8步耐久进程事实恢复本地修复**：投影下移 tooling，大小输出索引保存同一有界 process；旧行不制造确认。真实索引恢复已复现缺口并修复；末审补齐未知尾部 ID 的候选内容地址，组合检查已过，待发布／原生 TUI；见[依赖拆分](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#耐久索引恢复补齐第8步本地候选)。

- **第8步Compact逐调用来源已本地集成、待组合验收**：已确认裸call_id跨请求过滤会误隐藏新工具结果；沿现有live-tool账增加精确来源引用，保留既有ID／无refs旧编号结果／提交者语义／CAS，新候选将精确来源纳入内容地址，存量不确定来源保留并显式标记。工具并发段窄依赖独立并行，详见[职责拆分](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#发布前缺口compact逐调用来源已确认修复中)。尚未发布验收。决策分支合并版已改用 v3 四元身份，取代这里的三元引用，见[合并节](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#两线合并后的来源身份与模型轮结果决策分支吸收-main2026-09-23)。

第8步 Compact 边界在本地开发：候选只借原三个列表和估算回调；checkpoint/CAS 成功后投影失败不得回滚内存历史。C 顺序来源保持原持久格式与降级语义，不隐式引入 v3/scope 迁移；旧跨 request 调用编号过滤风险仍待处理。决策分支合并版改为显式采用 v3 与四元身份，这个风险随之关闭，兼容和回滚边界见合并节。详见[模型与工具循环](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#第8步-compact-候选与提交边界)。

第8步模型／工具循环已本地分离采纳与请求周期：只绑定原操作，不接完整Agent／params，不移动pre-I/O提交；有界消息扫描和等值token估算作为独立底座移植，未启用scope摘要链或第二执行器。18文件456 passed／24既有xfail，真实TUI未验，详见[职责和移植边界](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md)。

第7.10修复随7280c5b3e发布main，同一wheel（SHA256前缀c3f1242f）双机各1,274文件一致，默认入口与唯一Gateway同版。六文件304项、相邻三文件61项及严格gate通过。新版原生TUI225—227均仅派出回执加一条最终回复，任务／attempt结束、无锁、准确宿主退出；227实际命中旧attempt已结算而宿主未退出期间的grant，随后同run第三attempt执行，且能力事件completed最终回复公开。7.9／7.10本轮框架验收收口；7.10原瞬时快照交错由确定性红转绿用例证明，不把本轮无重复扩大为该交错必然命中。225最终摘要被孩子错误覆盖、226缺汇总合计、227检查程序及能力工具选择问题保留为业务失败／限制。线上CI无运行记录，未作为验收来源。

待处理唤醒快照时效（7.10 已发布并完成当前框架复验）：TUI220显示恢复／能力扫描可在候选选择之后改变原通知状态；执行决策必须以原队列当前pending／handled事实为准，不能把先前快照的历史BLOCKED再次当新事件。保持原通知身份、持久历史与当前run/attempt唯一事实源，未处理的新DONE仍按既有交付语义执行；不能依据回复文字去重，也不能统一丢弃合法的后续回复。

授权与旧工作片结束的接续（7.9 修复已部署，真实复验中）：TUI217中grant遇到fresh runner时不启动重复执行是正确约束；但旧attempt结束后必须按同一run的当前授权和控制事实决定是否续接。不能把“申请已处理／wake已消费”当作“续接已完成”，也不能依据模型文字或长期PENDING猜活执行。沿既有执行权和调度事实修复：通知和接续读取同run当前canonical状态，原结果仍作历史；唤醒记录以原mutate窄写，避免旧全快照覆盖结束记录。不新增状态副本、定时器或绕过生命周期门的例外；来源修复已集成为911a53245，原历史保持、旧入口及无用result参数已删除；定向合同通过，新版真实验收待完成，见TESTS。

完成交付与唤醒原因（7.8 已集成，待原生验收）：TUI212 暴露能力请求事件唤醒后，原生工作片已完成却被旧唤醒原因抑制公开 final。交付裁决应读取本轮结构化完成与投递事实，唤醒原因不能替代本轮结果；BLOCKED相关运行账沿原合同保留，不从模型正文判状态，也不强制结算。详见 TESTS 和 Goal 7.8；最小修复fc17def5f已集成，未完成能力事件、取消／中断、空载荷与重放规则保持，待同版部署与原生验收。

工具清理事实的模型可见性（第8步本地修复，待真实验收）：TUI213缺失的process清理回执已沿统一runtime_facts投影进入当前及恢复上下文；同一有界结构保留未知、退出码和数量，不改原结果／执行器／持久schema，不从正文推断成功。源码和组件验证边界见[工具执行事实投影](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#第8步工具执行事实投影)，真实TUI待组合包。

前台自然退出资源边界（第 7 步验收新增缺口，本地候选已集成）：TUI204 暴露命令普通返回 0／1 后嵌套后代仍存活，后续 `/stop` 不可见。前台完成须在启动归属仍可核验时收回所属后代，原命令退出码、业务副作用与清理确认分别记录；显式后台能力独立，不凭已消失组长的 PID 猜归属。详见 [长任务合同](docs/design/LONG_RUNNING_EXECUTION.md) 和 Goal 7.7；源码9330ee385已通过215项定向并同包部署双机，默认双机路径原生复验已核对，未覆盖的路径单列TESTS，不外推。

第 7 步父终态通知已在本地集成，将实际逻辑归入 `RunnerCompletionNotifier`：只持任务关联、WakeStore、
读取父任务和保存错误四项依赖，完成／受控取消共用原投递路径；结果服务和外部停止端负责装配。
保留 exact attempt、直属父级、文件模式及内部监督者信号语义；阶段提醒和能力申请入口不扩改。
已与恢复扫描接口片组合，原 sweep 和恢复测试均绑定同一通知器；旧接口调用删除；组合包 a067baddd 已双机部署，实际验收进行中，详见 STATUS。
见 [子代理迁移边界](docs/design/SUBAGENT_PARALLEL_EXECUTION.md)。

第 7 步结果提交依赖已在本地候选收窄：初次提交只接原 RuntimeDB、canonical task、结构化结果、
保存回调和绑定本轮身份的交付回调；WAL 原语只接 save，运行结算与诊断只接原 RuntimeDB。
结果服务装配 trace→父通知，仍在 WAL→运行账之后执行；不新增状态副本或兼容转发。
恢复扫描已集成为显式 repo/load/save/list/notify，原 sweep 绑定窄父通知器；恢复模块不再持有完整 manager。
本片不新增扫描器或业务重跑入口，不声称第 7 步完成；新版部署和原生验收分列 STATUS。
见 [收口依赖边界](docs/design/closeout_state_machine.md)。

第 7 步既有 wake／去重回执半写缺口已本地修复，尚未发布：沿原 dedupe 记录先冻结完整观察／信号，
按固定 ID 补齐原文件后确认交付；通用 pending 合并与完成通知保留 handled 显式区分，不截断同键 Goal 后续轮。
查询纯读，v1 只在写入口显式迁移；坏账报错、无 key 不承诺重试幂等、有 key 仍依赖调用方重试，未新增后台扫描。
设计、参考和验收边界见[发布恢复合同](docs/design/closeout_state_machine.md#唤醒配对发布的半写恢复第-7-步本地实现)。

第 7 步依赖收窄（本地候选）：runner 结果准入只接收 canonical task、结果参数和原 RuntimeDB，
不接收完整 manager；文件模式仍显式传 None，诊断仍写原账。已部署结果链与此候选分开验收，
边界见[子代理迁移设计](docs/design/SUBAGENT_PARALLEL_EXECUTION.md#第-7-步结果链迁移边界进行中)。
TUI 绘制合并已改为 20 Hz、周期动画 4 Hz，事件与模型执行不变。同一 fd 真任务的并行原生 TUI 对照 CPU 16.02%→10.23%，候选翻页中位 38 ms；长任务仍在验收，见 TESTS。
稳定历史缓存补充已实现并经等历史真 TUI 验证：发布快照共享、稳定/活动版本键分开、静态前缀按预算复用，安全行组仍在构造时净化。fd 自然开发约 110 分钟/Compact 3 已终态，首次详细展开约 1.2 秒仍是边界；见 [资源寿命](docs/design/TUI_RESOURCE_LIFETIME.md) 与 TESTS，不外推任意历史/时长。

TUI 绘制合并保持 20 Hz、周期动画 4 Hz，事件与模型执行不变。此前同一 fd 真任务的并行原生 TUI 对照 CPU 16.02%→10.23%，最终缓存片另做等历史对照，见 TESTS。

TUI 观察超时与业务终态分离已实现：截止点先查 canonical terminal，原页存活时按同一请求/游标退避续等，退出仅释放观察；plain 有限等待保留。官网 M2.7 原生 TUI 已通过真实短等待窗口和暂停客户端后接收终态，见 [资源寿命](docs/design/TUI_RESOURCE_LIFETIME.md)。

长对话验收方法已按用户要求调整：用 my-agent 自主完成真实 GitHub 项目跨语言实现产生自然历史；合成大文件仅保留存储边界定位用途。官网 M2.7 的 fd→Python 已自然结束，框架与项目内容分开记录，见 [TESTS](TESTS.md#真实开发长任务验收方法)。

TUI 原生媒体输入已实现、专用测试机官网 M3 验收通过：内容寻址原件、owner 校验、草稿 refs、发送边界编码与历史媒体预算复用原主链。旧纯文本记录无需迁移。详细合同见 [TUI 图片视频](docs/design/TUI_INPUT_MEDIA.md)；候选未默认部署。

第 5 步当前状态：使用卡按现有插件包声明与设置 schema 即时投影，详情和成功启用共用格式；列表、动作帮助及补全仍读同一目录，不新增卡片缓存、权限或执行链。本地实现与 300 项相关回归已完成，发布及原生 TUI 仍待验；详见 [插件生命周期](docs/design/PLUGIN_LIFECYCLE.md#从安装完成到真正可用) 和 [唯一 TODO](docs/tasks/REFACTOR_PLUGIN_GOAL.md#当前-todo唯一执行清单)。

重复启用的空资源声明已本地修复：无新环境计划时不声明候选资源，使原 unchanged／缺失拒绝路径真正可达；不更改激活状态机。见 [激活权威](docs/design/PLUGIN_ACTIVATION.md)，待发布真实复验。

显式插件连接收尾已本地移入原 HostCommand 执行区间，释放调用结束后才登记 executor 退出与运行终态；未启动拒绝同样延后，重送只读。详见 [宿主操作与结果](docs/design/HOST_COMMAND_EXECUTION.md#操作与结果)。状态：相关验证中，未发布，不等于所有清理均成功。

MCP 完整失败回执结算已本地修复、待发布验收：合法 `isError=true` 沿原操作账记失败，保留正文、释放逻辑锁；不表示零副作用，不重写历史 UNKNOWN。传输未知仍保留，详见 [连接与结果边界](docs/design/MCP_TRANSPORT_LIFECYCLE.md#完整工具失败与未知结果本地修复待发布验收)。

通用启动观察竞态修复已发布部署，383 项相关回归通过，新 TUI158／161 已实际启用复验：观察 host 退出后复读同一 session 的权威终态，保持原交接事务、身份、取消及退出码裁决。详见[托管进程合同](docs/design/MANAGED_PROCESS_STDIO.md#生命周期与通道)。

发布前边界修正已本地实现：进程内取消令牌从 tooling 迁入 common，保留唯一类型与上下文，不新增兼容入口或持久状态。详见[宿主命令边界](docs/design/HOST_COMMAND_EXECUTION.md#解决问题)，完整发布验收仍待通过。

十步重构的具体 TODO 已落地于 [原 Goal 台账](docs/tasks/REFACTOR_PLUGIN_GOAL.md#当前-todo唯一执行清单)。仅细化交付顺序与汇报，不改变架构范围；当前第 4 步的本地实现、发包部署与真实 TUI 验收分开标记。

第 9 步[插件面板](docs/design/PLUGIN_DISPLAY.md)已发布并经本机真实 TUI 验收；已知缺口：未调用工具的纯模型回合不进入宿主活动投影，
面板与其他窗口显示空闲，是否让未晋升前台回合进入投影待单独设计。

第 10 步已实施（本地）：[插件宿主只读 API](docs/design/PLUGIN_HOST_API.md)（包描述 v4 `host_api=["read"]`），让网页控制台、桌面窗口等界面型插件读取公开运行状态；写入与控制类权限未开放。

第 10 步已实施（本地）：随包 Skill（包描述 v3，来源 `plugin:<ID>`，优先级低于工作区 > 用户 > 共享 > 内置），详见 [可装卸插件方案](docs/design/PLUGIN_LIFECYCLE.md)。

第 10 步审批前复核已实现（2026-09-24 用户批准，分支 `claude/tool-precheck`，代理工具 opt-in 复核、两码登记、停用链改报激活失效，待真实复验）：[工具调用审批前的有效性复核](docs/design/TOOL_CALL_PRECHECK.md)——同一回合内插件被停用后仍先弹审批、批准后才失败；
设计为统一权限门在 `ask` 之后、审批事件之前，以及审批通过后 claim 之前，用处理器自己的 `availability()` 复核，失效按 `TOOL_UNAVAILABLE` 拦下。
改动统一权限门，须用户确认并经决策线评审后实现。插件数据清理命令、SDK 的"一致才替换"写入原语仍在待设计列表。

第 10 步新增[插件逐次工作区写入上下文](docs/design/PLUGIN_WORKSPACE_WRITE.md)，状态为本地已实施、真实 TUI 未验：
只对协商扩展且声明写效果的工具下发冻结写入范围，裁决与内置写工具一致且只可能更严，由逐项比对测试强制。

第 4 步新增[插件逐次工作区读取上下文](docs/design/PLUGIN_WORKSPACE_CONTEXT.md)，状态为开发中：
仅对固定连接明确支持扩展的自有插件传递冻结 cwd 与原读取权限，不改参数、共享进程或安装配置。
通用运输已在本地接通；唯一源码构建期投影的轻量 SDK 与 workspace-peek 已有实际标准构建和独立 MCP 组件验证。
真实多 TUI 装卸未验，不进入第 5 步。
SDK 构建开始实施：原字节投影读取合同、路径策略及通用 no-follow I/O；分页读取持有原文件描述符，
不另写链接校验旁路。workspace-peek 的命令、工具、设置从包内一份声明生成，版本和 wheel 摘要来自标准构建结果。

显式业务命令本地执行链已接通：原 HostCommand v2 分别冻结工具参数摘要和宿主选择摘要，v1 身份索引不变；
等待审批仍在原 executor 区间，经现有批准 binding 恢复同一调用，不新增审批服务或执行器。
协议与恢复边界见 [宿主命令](docs/design/HOST_COMMAND_EXECUTION.md#请求与运行)，尚未完成端到端验收。

**可选决策模型：P1—P5 完整实施已授权，P1 与 Curator 已本地验收，子代理/召回已本地验收，原设置入口、原生测试与 agent 代操作已本地联合验收，10 能力减量已本地验收，12窗口/缓存进行中。**

第 12 项本地计量已复用原出站配对清扫、guidance 与 ToolChoice，纯投影计量不读取宿主或校准；旧观测 v2 明确失效。三宿主同 turn 展示接续、失效清除与后台原执行身份回传已本地验收；不新增持久展示状态。完整恢复请求与 Compact 候选接受边界仍未闭合，细节见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
12.5的已知输出预留已接原transcript触发/候选接受门，本地72项通过；完整计量仍依赖12.4的原恢复准备一次化，不得多次召回/重放摘要副作用或把上一失败轮IR直接冒充恢复输入。
后台准备已分离prepare/render，历史范围及摘要投影从同次成功读取冻结；这只是完整恢复前置，不改变持久Compact的作用域权威。detached任务与窄审计的全局摘要/工具隐藏关系须先收口，不以省略历史后得到的小容量作为成功。
审查已用原store/checkpoint复现三类恢复材料丢失。唯一checkpoint链的v3 scope/base/精确覆盖与局部CAS保留全线程投影已实现底座；旧v1/v2显式读取，旧工具身份不全不命中精确覆盖。后台作用域选择和同一视图的摘要注入/隐藏已本地接线；原transcript与活动归档提交使用同scope/base，后台完整请求与Gateway/child同视图准备已本地接线，初次/手动与混合超大来源仍待实施，详见[容量审计末节](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4的Gateway overflow接缝已本地实施：原render/select冻结完整恢复请求，候选按结构化位置更新历史/证据，原CAS成功后继续同次生成。原来源defer只跳过压缩，保留repair/索引；无来源的活动回合压缩仍先于恢复准备。摘要失败不进入普通业务重试，token取消与边界事件沿原合同；child overflow现已接通并共用core恢复器与请求捕获，Gateway只保留宿主投影；后台transcript及carried活动归档已接公共完整恢复入口，初次/手动仍待接通，详见容量审计末节。
P5-B 首片已本地接入独立 `curator_relation`：只比较本批完整消息与有真实版本、完整短正文的正式 long-term 条目，提示可能重复/更新/冲突；原提取、候选、验证和晋升仍唯一，缺版本/截断/变更保留原流程。默认关闭、owner 后台设置、同一阶段期限；隔离真实 Jev 的12对短样本符合预设、超时保留原输入，后续提取仅本地替身，更广质量和真实晋升未验，详见[交接](docs/tasks/DECISION_MODEL_P5B_HANDOFF.md)。
P5-A 召回前省略普通会话记忆仍缺可保证用户显式查历史不被跳过的可信结构化意图；普通用户回合继续原召回。默认关闭的 `pre_recall` 首片已接正式上下文：原完整查询、HOT、lesson 和既选长期事实先保留，Jev 只从至多四个有界片段中选一次补充查询；仅用原 scope、剩余 top_k/字符预算追加经正式源确认的事实，未注入候选不记访问，P3 排序与本点共用阶段期限。离线136项通过，其中受控 JSONL 漏召回样本可只补确认缺失事实；隔离 Jev 两轮均建议查询，但词面基线已覆盖两条事实，应用无新增并标注 `no_addition`，真实质量收益未证，默认维持关闭，见[审计与实施记录](docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md)。P5-C 的已归档 web_fetch 多页阅读顺序首片默认关闭，只向原 text/native 展示追加有界页序；隔离真实 Jev 首轮 not_needed 保留原展示、第二样本自动追加2→3→1，原结果/refs/归档/账本不变。本地页面源不证明互联网检索总体质量，其余接入点未验，见[交接](docs/tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md)。
Jev官方64k整包/32k单题容量与声明的更小窗口现做发送前估算筛查，原 JSON 256 KiB 仍是资源硬帽。全仓回归曾发现把 UTF-8 字节数当 token 上界误拒 78 KB 合法批量候选并使 HTTP 零请求，现改复用原 `estimate_tokens` 且留一成余量；估算不冒充供应商精确 tokenizer 或硬容量保证，供应商超窗错误沿原业务回退。完整子代理模型容量、Compact面和真实缓存仍待核对。
召回后排序已移除旧的固定64题拦截；原记录完整性不变，过量请求由共用协议资源帽与Jev窗口门拒绝，失败沿原顺序。Curator每批最多标注32个来源是本地延迟保护，不是供应商题数限制。
子代理执行模型候选现能在同一决策设置服务按 profile ID 缩小；空列表沿原授权目录。当前用户指定的隔离验收范围是官方 MiniMax-M2.7、官方 MiniMax-M3 和 OpenCode DeepSeek-V4-Flash，不按名称猜来源；实际改选执行仍待验。
用户明确：主代理收到派工需求后，子代理模型建议、首轮请求能力核验、采用或回退及继续运行都由宿主自动完成，不设置逐子代理用户选择、确认或补资料步骤。显式模型设置只提供已有的结构化约束与竞态优先级；创建时的 pending 建议尚无执行效力，不可算作实际切换验收。
子代理首次采用所需的模型连接来源现有原私有目录 v5、共享发布 v2 的随机持久代次；所有原保存、凭据/OAuth 刷新及共享撤销均使旧快照失效，已启用准备才初始化旧目录，普通读取不写。原锁 guard 可覆盖最终 child thread CAS，缺代次或非管理员旧 shared 自动保留原模型；当前仅目录合同通过142项，首次发送消费与真实异模仍待验，见[交接](docs/tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md)。
P5-D 主会话自动选模只读审计及原线程选择版本首片已完成：同值显式选择也前进单调版本，旧线程三字段全缺归一未知、不伪造历史；Gateway 仍先于准确会话车道冻结模型，真实首请求容量和跨 provider 历史可回放事实尚缺。须在获车道后、首次模型相关准备前自动选择一次，未知保留原合法模型，不要求逐片用户确认。版本首片不等于已自动采用，详见[审计](docs/tasks/DECISION_MODEL_MAIN_MODEL_AUDIT.md)与[交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md)。
P5-D 后续 Stage C 已在准确 Gateway 车道内用原完整请求准备、同源 provider payload、候选目录代次和原线程 CAS 实现首请求自动采用；发送前明确拒绝才沿原模型一次，已提交或结果未知不跨模型重发，未知模态/容量保留原模型。容量仅是 UTF-8 字节加协议余量的工程估计，不是供应商精确 token 保证；本地 fake HTTP 联合302项、最新本片34项通过，真实主会话跨供应商仍待验，见[采用交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md)。
P4-B 普通 user owner 的原 `user_config` 现仅展示/执行 decision-only 动作，主 Gateway 回合从宿主当前 RunParams 取可信 thread，子代理只能用自己 runner 身份；原设置回执保持 `revision` 出现在有界模型预览内，CAS 不放宽。隔离真实中文第二轮由模型自主 `decision_read(thread)`、`decision_patch(thread,14/0)`，原事务回执与线程文件显示 14/1 和预期三项生效，模型准确回复；没有第三次独立 read。首轮失败、底层修复、私有恢复及单 Gateway 证据见[交接](docs/tasks/DECISION_MODEL_NATURAL_CONFIG_FIX_HANDOFF.md)。
隔离主会话 `apply` 三条普通中文新会话已验短期限、`need_data`、选择当前 M2.7 三种真实保留：5 次官方 Jev HTTP（1 次超时输入未知）、4 次官方 M2.7 HTTP 200，三轮都完成且无异模业务请求。调整期限只作用私有测试设置，最终原 CAS 恢复为 off；**真实跨 provider 自动采用仍未出现**，不能用 fake 阳性代替，见[真实交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。
P5-E/F/G/H 的只读审计已完成：原 Goal 事后 token 口径不是决策 input-only 的请求前预算，ModelCallLedger/验证证据也不能自行授予实验权或证明业务收益。首片在原设置事务加入内部 `restore`：同一层 set/unset、完整 owner/thread CAS、一次版本前进和读回；后改冲突保留用户值。实验授权、请求前限额、对照质量及自动应用尚未实现，详见[审计](docs/tasks/DECISION_MODEL_SELF_EXPERIMENT_AUDIT.md)。
E1 原语已把默认关闭的实验开关、有界 thread 授权信封与撤销放入原决策设置 v2，并在原 ModelCallLedger 同锁内串行预留请求数/声明的完整输入量；本地组合 356 项通过。当前没有宿主用户授权入口及原操作提交证据，声明输入量也不是可靠 token 上界，发送前硬门尚未接通，因此实验联网依旧失败关闭，不能称完整 E1；后续 E2/F/G/H 未实现。见[交接](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md)。
E1 后续只读核验发现官方示例的 `usage.input_tokens` 高于该请求 JSON 字节数，公开 64k/32k 上下文说明也没有明确绑定完整服务端包装后的实际输入账。因此不能把 UTF-8 大小、原估算器或自填整数当硬预算证明；优先取得供应商固定 endpoint/模型/计数口径的完整输入上限，再于原实际发送接缝绑定最终 wire、授权回执、业务身份与账本预留。原观察器吞异常，不能承担硬门；在此之前实验继续关闭，详见同一 E1 交接。
P5-C 自学习点的[只读审计](docs/tasks/DECISION_MODEL_SELF_LEARNING_AUDIT.md)确认：现行 runner lesson 进入 owner CandidateService，旧 learning_drafts 只有迁移读；没有受用户确认约束的 Skill 提案/正式写入链。Jev 可在来源明确时观察候选，不能凭评分写 `SKILL.md`；apply 须先建唯一提案与确认入口。旧 README/指南的 `enable_self_learning` 和 `my-agent learn` 已实现说法已按当前代码校正，AGENTS.md 的确认规则仍为后续实现约束。其后 S1 提案/确认链已在本地分支实施待审，见本文顶部“自学习 S1”条目。
P5-C 规划点的[审计](docs/tasks/DECISION_MODEL_PLANNING_AUDIT.md)区分现有 Todo、workflow plan、Goal 与 create_subagents 的权威。默认关闭的 `planning` 首片已接当前主代理原 `task_progress(read)`：只对2–24个 exact open Todo ID 提示一个优先评估项，版本/本轮问题失效不采用；原账本、Goal、派工均不变。离线133项通过；隔离 Jev 先遇4秒真实超时并保留原回执，8秒对照成功建议精确ID且只追加软提示。Gateway TUI 质量和多次读取延迟待验，见[交接](docs/tasks/DECISION_MODEL_PLANNING_HANDOFF.md)。
P5-C 交付质量点的[只读审计](docs/tasks/DECISION_MODEL_DELIVERY_QUALITY_AUDIT.md)限定 Jev 只能从本轮原 verification 或已确认 ready artifact 的精确引用中建议一个复核焦点，供主模型参考；不得评分设硬门、造产物、改 Goal/child/最终回复状态。原归档后的 text/native 共用展示接缝可复用，候选 ready 来源与独立设置尚未实施。
P5-C 动作候选的[只读审计](docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md)发现本机 Browser ref 模块尚无生产工具引用，Computer Use 经原 MCP/审批链但 OCR 内容没有宿主验证的 observation/candidate ID 与代次；Jev 暂不能直接选坐标、命令或输入。先让现有工具来源产生可绑定、可失效的结构化候选，再做归档后软提示，不重复 `skill_tool` 工具路由。
早期全仓集成 gate 发现本线新增 13 处跨层导入：Gateway 模型采用/观察直接调用 `agent_core`，Compact 重建从 Gateway 反向读 core 运行类型，`user_config` 从 Tooling 反向读 runner context。现已把同一请求选择及可信运行上下文移至 `agent/` 共用层、跨 Gateway/core 的采用和 Compact 编排移至应用层，删除旧模块，没有扩宽 import 白名单或新增只转发 facade；守卫 **0 findings**，相关 10 文件 **268 passed、4 xfailed**。完整严格 gate 仍未通过，见[测试总览](TESTS.md)。

最新用量合同：决策用量沿原 LLM 用量行增加输入 token，输出位置预留且当前留白；原 usage、请求状态、期限与资源预算保留。决策请求不进入 USD 价格估算或 owner/run 成本累计，本地决策模型沿用同一合同；普通生成模型原有成本机制不在本设计变更范围。
用户要求可见进度：完整 Goal 顶部维护 18 项验收 TODO，组件与可用产品分开；先打通可用链路，再扩接业务，不用测试数量代替整体完成度。
开发调度按任务难度选模型：普通实现、测试和文档可用 Astra high 或 GPT-6 Sol high；较难的独立开发可用 GPT-6 Sol xhigh，复杂跨模块状态、并发与模型切换审查用 Astra max（最高档，不使用 ultra）。这是开发代理的算力分配，不进入 my-agent 子代理逐次模型选择的人机流程。
已本地接入工作片展示投影，减少Skill名卡/工具目录及明确可选类别schema；原权限/快照身份保持，被收起项由原搜索找回，本地HTTP及原运行接线组合已验。
metadata作为对照，progressive才覆盖额外direct插件schema减量；默认总开关关闭，完整细节见模块计划。
策略/开放类别列表复用原设置、CAS和菜单；真实Jev否定跨题选择槽，已改每候选独立适用性题，候选说明只发一次，超限沿原输入。
真实API概率会按百分位舍入，wire按量化边界检查并保留原值；原失败样本replay及实际复测见真实验收记录。隔离 Gateway TUI 已跑通普通回合与保留原模型的子代理批次，完整窗口和跨模型切换未通过。
缺目标/合同/步骤/环境保留原输入，明确无需才可空短名单；采用前复核原Skill范围和固定插件代次，不自动补资料或扩大授权。
设置范围现以同一登记表约束投影与运行：Curator 仅 owner 后台，旧线程后台覆盖仅可清理。原菜单和 agent 共用显式探测，读取/保存不发模型请求。

原模型目录当前私有 v5、共享发布 v2，会话当前 v10，显式迁移旧记录并保存原 owner/thread 决策覆盖；不新增配置文件。
共用设置服务与原 user_config 的读取/修改/恢复继承已本地接线，双层版本 CAS、时间校验、失效服务仍可关闭。
原账本增加互斥用途分区、逐字段真实用量、单调终态及 worker 精确保留；原用量增量及同一行决策输入已接线。
原生 Jev、严格 HTTP、有界调用与取消/准入已接入实际决策服务；配置/本地 HTTP/原账本/活动显示联合通过。
阶段预算、连接冷却、在途关闭及迟到建议拒绝已本地验证；Curator 已接通，原设置菜单/原运输/本地原生HTTP已组合接通；实际安装版 TUI 尚未验收。
短决策只刷新当前显示，不等待会话写锁；原持久用量和持久显示继续由原调用边界/收尾负责。
这些组件不代表当前安装版 Jev 已可用；当前已验/待验边界见完整 Goal。
首个业务消费者 Curator 已本地接通：用户后台显式绑定原 run，不借历史会话；临时标注不删材料、不写游标。
缺数据/无需/无匹配/弃权逐题区分；可调决策时间受原 lease 剩余头寸约束。子代理选择与召回重排已本地接通；原权限/幂等/记忆预算保持权威，完整窗口与真实模型验证未完成。

按最新讨论，Jev 作为原模型体系的 decision 用途接入，复用 owner 配置、预算、取消、账本和原业务入口；
不以独立 MCP 判断工具作为主要产品方案。默认关闭，逐点观察/应用；建议前台单次 2 秒、阶段累计 4 秒、
后台标注 4 秒，默认零重试，报错或到期保留基础方案，迟到结果无提交权。主模型与权限合同不变。
2/4 秒只是可调默认值，支持按接入点覆盖；用户设置与 my-agent 按明确指令代操作首版即共用配置服务，
读取有效值与来源、按版本修改并读回，不依赖 Jev 在线。配置热生效于后续请求，关闭阻止旧建议应用，不改原业务结果。
首批是子代理模型选择与原 Curator 前置标注，随后扩展召回和能力推荐；完整窗口/缓存、缺数据和并行边界见
[决策模型接入计划](docs/design/DECISION_MODEL_INTEGRATION.md)。完整清单见[执行 Goal](docs/tasks/DECISION_MODEL_GOAL.md)，
另一任务已确认 P1-A 范围无重叠；本线独立实施，P5 纳入总完成条件，不改变对方 Goal。

卸载已本地接通，完整真实验收待显式业务调用接线后进行；设计见 [卸载边界](docs/design/PLUGIN_ACTIVATION.md#卸载与重新安装边界本地实现未发布验收)。
显式调用复用原 HostCommand/ToolExecutor/MCP 与审批请求链，命令运输已携带自己的审批生命周期；
显式 slash 不构成自动批准，旧激活的会话批准不能复用到新代，见 [业务接线](docs/design/PLUGIN_ACTIVATION.md#显式业务调用的待接线边界)。

第 4 步本地源码已接通安装、配置、实际启用、普通工具组合、停用释放、重新启用及卸载。
卸载使用释放返回的完整记录做原锁 CAS；原成功结果持久化并严格读回后，才消费退出证明和回收无人引用的旧包。
原准备仍在执行时保留安装与环境；同包重装保留其包，旧请求不控制新安装，UNKNOWN 不因后来清理成功被改写。
公开目录 v3 派生原提交安装引用，避免同包卸载重装后旧目录再次有效；安装表和原操作历史不新增权威副本。
业务调用和 TUI/Gateway 交互审批运输已本地接通；同机连接沿原 GatewayPaths 和服务端规范 owner 固定审批地址。
原 HTTP 请求线程执行命令，消息流只运输审批和原结果；退出只取消自己的等待，不新增执行或审批账本。
完整多 TUI 装卸仍待完成；运输及恢复边界见 [宿主命令](docs/design/HOST_COMMAND_EXECUTION.md#请求与运行)。
本片未推送部署、未新增实际 TUI，不进入第 5 步。
详细顺序、原 executor 校验、v4 保留及消费合同见 [激活释放](docs/design/PLUGIN_ACTIVATION.md#管理停用的组合边界)。

以下前片约定中的待实施状态以本段为准。

第 4 步实际 enable 与新运行工具组合已有本地实现：沿原操作准备环境，完整验证 MCP 目录，确认候选退出后提交 active。
Registry 只从 core 注入的可信 owner 读取已启用代次，构造不启动服务；共享连接缓存同代代理，各权限视图分别投影。
私有设置仅交给子进程，插件 effect 自述不降低审批；永久关闭与迟到客户端登记双向复查，避免关闭后启动。
原 termination 新增可选 cleanup 保存完整 session 证明，单调保留且不改原命令退出事实；UNKNOWN 不以 PID 消失消除。
完整装卸和实际多 TUI 仍待完成，详见 [启用组合](docs/design/PLUGIN_ACTIVATION.md#启用与新运行的贡献组合)。

第 4 步本地管理停用已接线：安装表先撤销，原准备 attempt 关闭后按完整执行归属冻结，共享连接按原激活冻结。
两类资源均锁外使用原清理器；终态准备命令也核对 host，坏原操作或未知清理不能报告完成。
退出证据仍保留在原资源账，激活保留 revoked；后续 release 须先核验资源再 CAS，原操作结果持久化后才消费退出证据。
不能先删证据再清激活；删除环境还须确认原准备执行器退出，不能只看 cancelled。详见 [激活合同](docs/design/PLUGIN_ACTIVATION.md#管理停用的组合边界)。

第 4 步当前内部实现：固定 PluginActivationRef 沿唯一安装表跨进程复查，插件 MCP 接原托管管道与资源回执。
发送的原资源短锁在连接准入锁前，等待可取消；不持锁等服务端。未发送拒绝和可能已发送的失败分开进入原操作账。
旧代理固定连接，未知清理/启动禁止替代；完整管理装卸与实际 TUI 仍待完成，详见 [连接合同](docs/design/MCP_TRANSPORT_LIFECYCLE.md)。
以下早期切片保留当时设计边界，当前组合进度以本段与执行 Goal 为准。

第 4 步原 host 的[标准字节管道](docs/design/MANAGED_PROCESS_STDIO.md)与 v3 激活归属已有本地实现：
端点直接继承，launcher 退出收回原资源；共享连接不借用业务任务身份，旧 v2 原版本恢复，完整 MCP 准入仍待接线。

第 4 步唯一安装表的激活 CAS 已本地实现：准备、发布、撤销同源，旧快照不得改绑新代；资源退出证明仍沿原进程账。
状态、显式 v3 迁移、撤销与配额边界见 [激活权威](docs/design/PLUGIN_ACTIVATION.md)，完整装卸尚未开放。

本地开发：MCP 已分离客户端、固定连接和协议响应箱；永久关闭先撤销，临时断连只清理原连接，未知清理阻止替代进程。
目录发布复查同一连接，权限视图不再复活已关闭客户端；跨进程插件激活/撤销仍待接线，见 [连接合同](docs/design/MCP_TRANSPORT_LIFECYCLE.md)。
本地修复：模型工作片权限视图已迁移到原共享 owner_access；删除 helper 后的遗漏调用方已修正，不增加权限别名或兼容层。

本地实施中：插件环境准备复用原 operation 的完整 logical 资源声明和原 ProcessSessionStore；不新增进程账或操作 checkpoint。
固定计划及原 claim 先于写入；准备进程绑定原 owner/thread/task/run/attempt、宿主寿命及截止时间，普通后台仍按旧默认独立运行。
完整激活及 MCP 撤销仍待实施，见 [插件环境合同](docs/design/PLUGIN_ENVIRONMENTS.md)。

本台账保留当前决策、设计入口和未落地边界。逐次排障流水不作为产品规范；实际实现以代码、配置和结构化协议为准。

## 已采用的原则

第 4 步[配置接线](docs/design/PLUGIN_PACKAGES.md#配置接线第-4-步本地开发中)已进入本地实现：
明确 `/plugins configure <插件> --file <JSON>` 沿原管理员、路径权限、宿主请求和工具账执行，值只写 owner 私有安装表。
安装表 v2 在同一次提交保存配置、版本与回执；原 v1 显式读取并在首次修改时记录源摘要，查询不迁移写入。
目录 v2 加安装版本使配置变更也能拒绝旧请求，客户端和宿主须同版；原未知结果不重跑。启用、发布与撤销仍待完成。

第 4 步[本地插件包](docs/design/PLUGIN_PACKAGES.md)正在开发：静态校验与默认停用安装事实已有本地源码，包不可携带 owner/启用/激活身份。
显式管理请求的[宿主执行身份](docs/design/HOST_COMMAND_EXECUTION.md)已有本地接线：原 RuntimeDB 绑定独立运行，原工具执行器执行；HTTP/direct 复用原管理员权限，重送不重复执行，查询只读原结果。
本地取消记录已保留原领取元数据；执行权关闭与结果可读性分开，损坏原文不覆盖，严格查询明确拒绝。该开发修复不代表新一轮真实 TUI 资源停止验收。
唯一安装表沿 canonical owner data，版本与最后提交回执原子保存；原请求重试不覆盖新状态，清理异常不抹掉提交事实。
公共目录锁复用原后台系统锁，保留锁名/顺序并拒绝链接；原配额准入先于插件锁，没有第二套操作历史。
本地安装只保存停用包；开关关闭后仍能查询原请求，超时保持未知。独立环境、激活及撤销尚未完成，完整装卸未发布验收。
独立环境按[环境合同](docs/design/PLUGIN_ENVIRONMENTS.md)已有内部准备实现：固定地址标准 venv、本地 wheel 闭包、文件覆盖检查和安装后宿主读回；原配额非阻塞准入，进程沿原取消与退出回执。未接启用命令，原安装表独占发布权限。

第 3 步参数合同源码已接入：公共目录的动作声明驱动解析、帮助与补全，Windows 反斜杠保留，
完整引号错误不降级，部分输入和候选使用同一绑定规则。CLI/HTTP 的管理帮助只读可用，装卸和业务仍未开放；
owner 目录与提交版本已由后续宿主片接线，真实激活代次和执行权限留生命周期实现。设计与参考边界见 [参数合同](docs/design/PLUGIN_LIFECYCLE.md#第-3-步参数声明与解析)。
参数与补全修复已发布同版双机；TUI 146 复验 Tab 后正常 Enter，两端各 13 类命令检查通过，143 原失败保留。
150 已验活动请求的静态分流与普通正文保留；不代表动态插件目录或整个第 3 步完成。
已发布同版双机的[宿主目录与提交绑定](docs/design/PLUGIN_LIFECYCLE.md#第-3-步宿主目录与提交绑定)：
沿原可信 owner 生成声明快照，客户端只持展示缓存和首次选择的 revision，过期提交不自动重放。TUI 151—153 所测框架入口通过，第 3 步本轮范围收口；实际贡献为空，安装与业务执行未实现，不能用本片代替真实插件换代验收。

模型归因新增四组对照：my-agent 分别使用官方 M2.7 与 OpenCode Flash，Codex、Free-Code 使用同一官方 M2.7。
状态：用户已明确选择框架功能与模型交付质量分开记录，Goal 恢复 active；第 1 步框架基线收口，第 2 步进度纯计算、供应状态与唤醒/Goal 路由已分离并部署，相关实际链路已验；租约/恢复与稳定资源换代修复也已同版部署，相关实际验收和资源收尾完成，第 2 步本轮框架范围收口，第 3 步公共声明与命名空间首片已发布并同包部署；实际 TUI 137 暴露的资源停止缺口已修复并由新版 TUI 138—142 分项复验；参数与宿主目录片也已发布部署和复验，第 3 步本轮框架范围收口，第 4 步静态包与安装事实已进入本地开发，装卸链仍待完成。命令公共声明、文件队列守门和旧控制分派的扩展边界已记入[首片接线核对](docs/design/PLUGIN_LIFECYCLE.md#第-3-步首片接线与实现边界)；宿主目录已接通，真实安装/激活贡献与装卸仍待做。my-agent 两模型短题通过，Flash 核心长题通过但报告有保留项，M2.7 长题跨框架失败保留。

任务资源停止边界已实现、发布并完成本轮分项实际验收：显式停止覆盖主代理和原子树启动的后台/PTY，暂停 Goal 与回合中断仍各守语义。
先沿原 task/run/attempt 关闭执行和派工权限，再冻结原清单，锁外只清理固定实例；停止不能追随后来恢复的新任务。
访问范围、完成通知与执行归属分开；v1 不猜迁移，v2 以同一 Store 的目录锁、CAS 与 redo 持久化启动预留、host/child 绑定及交接。
已交接独立资源不受旧回合 token 控制。终态业务历史与 UNKNOWN 锁保留，独立 runner 用原心跳转交精确取消，宿主只读核对退出。
子代理创建、换轮、放弃、授权恢复及取消共用原 creation guard；managed 运输准确 pending ID，文件模式一次性消费原非空 launch/attempt。
旧快照不回滚新启动；再次停止撤销终态上的新预留。插话沿排序 turn 锁修复原批次、复读 pending 后准确预留，网络重试只接续本条消息。
direct/local 与未晋升请求使用正式绑定及原锁；读取失败、错代次或已晋升不能降级猜测清理。
参考本地 Codex `578c1b2` 的独立控制和关闭入口/固定集合做法；不创建第二套状态，不按共享宿主 PID 强杀。
设计细节见[任务资源停止](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md#任务资源停止修复)及[整任务控制合同](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md#整任务控制接线)。
TUI 138—142 的框架证据、模型脚本失败及真实覆盖限制见 [TESTS](TESTS.md#整任务停止发布与实际-tui-138142)；137 失败原样保留，后续宿主目录验收另见本页首段。

Working 图标间歇消失：状态为**用户明确延期、尚未复现**。当前只记反馈，不修改活动计数或动画策略；
在本轮十步目标完成后与用户一起采集真实状态时间线，再区分正常展示切换与活动事件丢失，不作为当前目标的验收阻塞。
推进依据是本步实际框架证据；模型已核实的程序、计算和报告失败单列，不改原整轮结果。框架缺陷、必测覆盖缺失及未明归因不豁免。
同题、同初始输入和独立会话的结果与原生上下文共同定位根因，不以模型口头成功或通过组数代替证据。
具体控制变量、协议适配边界及当前覆盖见 [模型与框架对照](TESTS.md#模型与框架对照)。

runner 失败分类已修：复用 `turn_end.py` 的唯一映射，正常让出保留 `PENDING / interrupted / ok=False`，
但不补 `runner_error` 或写入最近错误；正常完成同样清当前旧错误。显式失败、未知原因、状态冲突和授权阻塞保持原判据，
不改调度、尝试历史或持久格式。状态合同与验收边界见 [轮结束设计](docs/design/closeout_state_machine.md)。

线程、消息、任务关联与 Audit 的显式组合已实现并验证主要运行路径；Store 的领域继承链现已全部移除。
`threads` 唯一持有新会话模型解析器；消息依赖线程校验/原子更新，任务终态显式关闭进度策略；
Audit 共用同一任务账本、命名锁与任务锁，移除原转发包装。定向 2,206 项通过，尺寸基线未扩大。
插话事务分层已实现：回执格式与校验独立，账本读写及索引修复共用同一 storage，提交批次、确认批次和恢复各自持有明确依赖。
组装入口已删除最后一层继承；回合锁、回执锁、批次先提交后修复的次序及原持久迁移保持。
新版真实 TUI 已验完整运行中插话和并行长命令。用户明确三类控制后，修正此前将 Goal 暂停视为停止资源的错误预期：
Goal pause 只关闭目标自动续跑；interrupt 只停止当前模型/工具执行链；明确停止任务资源才关闭归属进程、终端和子代理。
代码已移除 Goal 暂停/清除的中断回调；有无 Goal 或 Goal paused 都不能让 interrupt 落入任务停止，子代理自然完成也不伪标中断。
已实现并通过真实控制验证：PTY 执行归属与访问权限分别保存；明确任务停止按精确 owner/thread/root task 或 run/attempt 收口。
启动预留在停止时标记，句柄冻结后异步终止，旧停止不重新扫描恢复轮；进程树退出需实际核对。
不按整段会话、命令文字或文件路径批量杀进程；参考和语义边界见 [长任务执行](docs/design/LONG_RUNNING_EXECUTION.md#pty-的执行归属与任务停止)。
后台续跑默认工具目录现复用统一命令/会话工具组，修复恢复时遗漏交互终端的缺口；显式配置及 owner/task 限制仍有效。
该目录修复与三类控制分开验收，不改变暂停/中断/资源停止语义；267 项定向及真实工具准入、子代理续采和主代理 PTY 保留已验。
后台启动仍经审批门，主代理恢复轮使用文件查询；旧句柄的后台读取与报告质量不因目录修复而自动算通过。
后台主代理审批桥已实现并通过本机真实 TUI：复用原主任务审批目录、完整工具请求和 TUI FIFO，不新建权限或持久状态源。
主代理额外绑定当前 claim；换轮、取消、坏账与无交互接收方均关闭式失败，Goal paused 不撤销当前回合的审批。
只为当前会话选中的主任务开放审批写回，其余子代理查看、插话和停止的授权边界保持。
428 项定向通过；暂停 Goal 后原回合仍可审批，中断后新 claim 读取同一 PTY 并审批输入，程序只启动一次且正常退出。
拒绝样本的 handler 未执行，未产生替代调用。合同和边界见 [后台工具审批桥](docs/design/SUBAGENT_TOOL_APPROVAL_BRIDGE.md)。
安装版长任务发现父会话任务关联被误当成子代理执行身份，审批在发布前失效；已在原归属读取处修正并同版部署双机。
实际 TUI 已验子代理/孙代理批准后继续原调用，以及拒绝后 handler 未执行；数据与报告质量另记，不把审批通过当作整体验收通过。
canonical child 记录优先于会话任务投影，仅不存在时读取 main claim；坏账不回退，主子孙仍复用同一审批账本。

存储组合首片已实现并验证主要运行路径：模型用量通过 `model_usage` 领域对象访问，只接收原账本目录、线程读取和原子更新能力；
Goal 时钟由 `goal_clock` 共享对象直接持有操作与状态，不再沿继承链暴露计时方法或复制锁/字典引用。
JSONL 读取与错误报告下沉无 Store 依赖的 IO 模块，调用方同步迁移；文件格式、锁路径、CAS 与提交顺序保持。
详见 [第四批重构](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第四批存储能力由深继承转为明确组合)。

存储公共上下文与 claim 迁移已实现并验证主要运行路径：目录与路径统一由 `store.storage` 持有，扫描投影归其 `indexes` 对象；
基础类中的 `wake_delivery_receipt` 回归唤醒领域。各调用方同步改用显式上下文，不保留旧根目录字段或路径方法转发。
执行租约经 `store.claims` 领取、续租和提交终态；目录初始化、路径、索引失效、TTL、恢复归属及原子锁语义保持。
旧基础类和 claim 继承类删除，归档维持原策略后租约的调用顺序，不新增长期任务。
本片定向 1,065 项通过、4 项既有 xfail，双 TUI 的暂停静止、压缩重连、真实续采、并行插话及原生投递去重已验。

Goal、观察、唤醒与进度策略已改为显式组合，并完成本片主要运行路径验收。唤醒接收观察领域的确认能力，
保留先发布 wake 再追加观察的顺序；Goal 接收共享时钟、线程校验和任务读取能力，不反向导入 Store。
跨领域旧账维护留在组装入口，依次调用策略归档和 claim 归档；通用 JSON 对象读取下沉既有 IO 模块。
原 208 个函数/方法及 32 个生产调用文件核对，无非预期逻辑变化；1,236 项定向通过、28 项既有 xfail，尺寸基线不变。
新版双 TUI 验证暂停/压缩/重连续采和并行批量中文数据，报告取消原因错误与临时脚本选错目录的样本保留。

Gateway 请求职责拆分已实现并验证主要运行路径：上下文读取与准备、模型输入渲染、历史提交/补交、请求身份与执行车道分别归位，
请求执行器保留编排和结果映射。前后台完整历史行选择下沉 conversation 领域，删除后台对 Gateway 执行器的反向导入。
不新建存储副本或状态机，不改变 claim、Compact、追加/补交及终态顺序；无生产调用的历史入口直接删除。
详见 [第三批重构](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第三批收窄-gateway-请求适配)。

子代理跨进程停止已修、定向及本机真实 TUI 已验：dispatch 进程退出后，新会话命令仍写入的缺陷已复现并修复。
历史发布版的子代理取消和失联回收共用工具层进程树终止原语，保留完整性、出生标识及未确认 PID 回执；
现行发布版已将明确取消改为固定工具资源清理和 worker 内协作中断，上述整树停止合同优先。
仍由调用方先证明宿主独占，共享宿主和 Gateway 不允许按 PID 终止。已删除仅杀进程组的重复实现。
无句柄的 POSIX 退出核对补充内核僵尸状态，不回收他人 Popen；无法核对时仍返回未确认。
OS 强制终止不能保证子代理补写原生历史，已有取消账和 attempt fence 仍为恢复依据，不能伪造缺失工具结果。
详见 [重构设计](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第三批收窄-gateway-请求适配)。

仅思考响应保留已实现、真实复验中：有正文、工具或有效 typed 思考都表示响应有内容；
思考不是公开答复，不因无正文在适配器内偷偷重采样。既有零工具有界续跑先追加独立原生轮，
不按未增加的工具轮号合并；未执行工具块不回放为调用，签名/密文和用量保留。
不新增模型完成判官或长思考时限，慢模型单次输出自身复读仍单独排查。
详见 [响应历史](docs/design/LONG_RUNNING_EXECUTION.md#仅思考响应与续跑历史)。

工具正文二次裁剪已修、定向及真实出站已验：归档日志预览不再代替已分页的模型结果，
完整尾部和继续读取参数共同保留；大输出外置、脱敏和权限合同不变，不改旧历史/缓存前缀。
详见 [工具结果投影](docs/design/tool-runtime-unification.md#重复工具观测与恢复)。

Shell 人工长度门已删除、真实长命令已验：合法脚本不再因 2,000 字符上限被前置拒绝；
Schema/handler 同步，原安全及进程预算不放宽，不自动拆脚本或扩大权限。
两项修复不等于慢模型复读已解决：新会话仍重复读取，相同失败输入关闭服务缓存也未换路。
不据此关闭产品缓存。用户开启思考后，原生思考已收发保留，真实新任务仍重复读取未交付；
不能把“开启思考”当成已经解决。服务端采样单变量对照单独留证，不修改日常连接或静默切模型。

渠道故障软提示误分类已修、定向已验：只统计 canonical 错误分类中的网络可重试/工具不可用，
按 tool/call_id 消费最新回执。测试/编译非零、参数、状态、权限、取消和未知失败不再触发换渠道提示。
原错误、工具执行、审批和恢复合同保持不变；真实正常 TUI 66 组工具后自然结束，
没有误加渠道故障提示，8 项测试及原始/现有数据独立复算通过；不据此关闭慢模型复读项。

后台缺模型等待已实现、真实 TUI 已验：本地 `ModelNotConfiguredError/ModelProfileError` 等待该会话配置恢复，
不因 30 秒届满反复执行。普通错误仍按原配置冷却；网络限流与远端请求拒绝不误当本地未配置。
进程内退避有界且线程安全，不消费持久唤醒、不改 Goal、不选择默认模型；模型选择仍以 canonical thread 为准。
详见 [长时间运行](docs/design/LONG_RUNNING_EXECUTION.md#后台异常退避)。
真实验收补齐 Goal 的旧错误收口分支；缺配置不消费 wake 或终结目标，选好模型后原任务自动继续并完成。

TUI 持久请求回执优先于 Gateway 瞬时存活检查：已经取得规范 request ID 的消息沿原终态等待，
不能因重启间隙 PID 不可见而宣告未执行。未提交请求保留服务检查，超时/取消不变、不自动重发。

进度跨轮补充已实现、原会话 TUI 已验：`task_progress` 显式 `run_id` 可以指向同 owner/thread 的已存在计划，
默认仍写本轮；不以最近读取、正文或同名 item 猜目标，不选择工作区或改变旧任务生命周期。
子代理仅写自身，独立后台工作不混入；接口和运行权限由同一解析器校验。
已完成项的 notes 属于可维护备注，后续验证可直接更新；状态/结果的显式更正规则保留，证据仍追加去重。
本地队列展示补修已实现、原会话 TUI 已验：CLI source 与 owner provider 分工明确，owner/鉴权不变，
本地前台消息按私有通道保存和重放。HTTP/IM 未因 rich transcript 开关扩大路径可见性。

已实现、真实串行对照已取证：三种模型接口统一遵守显式温度开关；未启用时使用提供方默认，
不再由 Chat/Messages 隐式注入低温。模型级显式值、工作片冻结与摘要调用的显式覆盖保留。
采样差异是已确认的请求问题，不能据此断言所有复读都由温度造成；不增加循环硬停或历史改写。
见 [模型采样](docs/design/TUI_MODEL_PROFILES.md#采样参数)。

执行事实投影瘦身已实现、定向及真实出站已验：原生工具回执已带完整顺序，下一轮只追加最近完成工具批次的权威状态，
不再每轮重抄近期成功/失败明细和聚合账本。旧历史、原始账本与已有缓存前缀不改；最终操作核验仍保留全轮汇总。
批次由宿主 turn_id/tool_round 标定，不从模型文字推断；不新增质量验收或循环中止门。
代理树轮询的稳定进展补修已实现、定向已验：复用已有成功软观察，计时和心跳变化不当成真实进展，
不追加新的等待器、杀进程策略或完成门；完整状态、权限与生命周期仍保持原样。
子代理查询自己的子树时按规范范围裁决排除查询者自身；显式查询其它代理不排除目标节点。
进度部分更新补修已实现、真实续写已验：Todo 与覆盖目标未传状态时保留原值，新建才默认 pending。
更正开关不是隐式重置状态的授权；显式状态、完成事实保护和参数校验不变，不猜测或重写旧账。
Todo 项的原生 Schema 同步声明 ID 必填，消除生成与执行合同不一致；不以错误重试代替必要字段约束。
验证软提示不再要求逐动作重新检查：相同版本、输入和观察点复用有效证据，信息足够后推进实现，
针对改动验证并如实汇报；权限硬门不变。此项修正冲突引导，不宣称单靠提示可治愈模型循环。

已实现、定向及真实递归协作已验：子代理创建和续派按其 canonical thread 模型引用构造后端，
删除从共享调度宿主隐式捕获连接的路径；显式测试/嵌入注入按线程隔离，并行工具仅传本线程快照。
见 [会话模型](docs/design/SESSION_MODEL_SELECTION.md)。不增加部署默认模型或失败后换模型兜底。

已实现、定向与真实 TUI 复验中：停止不投递回复，但保留中断前原生工具历史；异常先交同宿主 canonical
出口再抛回原错误。前台、子代理及 CLI 共用运行回调；后台工作片也先保存 native，再按原规则投递公开 final，
两者用独立宿主回合编号关联，冻结重投不改编号。不建影子会话；缺结果只标未知，不伪称压缩或成功。
边界见 [上下文](docs/design/CONVERSATION_CONTEXT_DESIGN.md)，历史缺失与慢模型自身复读分别验收。

已实现、定向及正常 TUI 主链已验：受管命令可取消长等及原 wake 队列终态通知；自然收尾后的欠报通知
必须在队列和执行准入共同核验，回执未写成功暂不消费。请求摘要只诊断客户端前缀，不推断服务端缓存、
不改历史。旧子树阶段快照不再拥有完成否决权，实际子树和未读邮箱保持权威。
慢模型长任务仍复读，不能以健康流或高缓存宣称任务完成。配置和边界见 [长时间运行](docs/design/LONG_RUNNING_EXECUTION.md)。

已实现、定向回放与新包 TUI 主链通过，长循环采纳效果另验：后台进程将状态、日志增长与耗时分开，稳定进展摘要只进入
成功重复软观察；原始结果、权限与终态不变，不因沉默强杀。见 [工具恢复](docs/design/tool-runtime-unification.md)。

已实现、定向及真实 TUI 已验：客户端不活跃等待使用单调时钟，绝对 deadline 仅在边界转换一次；
系统校时不能伪造请求超时，客户端脱离不等于后台任务失败，不自动重跑。见 [TUI 恢复](docs/design/TUI_DESIGN.md)。

已实现、真实慢模型复验中：成功重复计数采用宿主完整结果摘要和固定间隔软提醒，不按 Shell 文本猜只读、
不增加任务完成硬门。同端点前台工作期间延后普通记忆策展，保留 pending/游标；单次后台预算传入 HTTP，
超时取消旧连接后再决定缩批。详见 [工具恢复](docs/design/tool-runtime-unification.md) 与
[记忆结构](docs/modules/memory/04-structure.md)，外部应用占用与服务端缓存不由本 Gateway 保证。

已实现、真实账号待授权验收：`/model` 认证复用 owner provider 唯一存储；支持订阅设备码登录和显式填写的
RFC 8628 参数，不导入其他应用凭据、不默认选模型。登录取消/退出/配置变更由代次及请求 CAS 约束，
刷新跨进程串行；OAuth 不发布给其他用户。协议、边界及验证见 [模型账号登录](docs/design/MODEL_OAUTH.md)。

长思考折叠提示展示已接收的原文行数与字符数，排版行与原文行分开；不把计数当作
剩余工作进度，不为隐藏文本全量排版。定向回归与新包真实 TUI 连续增量均通过，
字符数超过预览上限后仍刷新，见 [完整原文](docs/design/TUI_COMPLETE_DETAIL.md)。

状态查询与界面共用同一授权快照，但模型只接收精确身份、状态、原因和真实产物读取顺序。
恢复摘要、检查点和内部目录不作为模型结果引用；超长结果复用原归档与逻辑引用，并明确省略范围。
状态查询的协作说明复用派工纪律，不再另外要求父级先结束回合。完成后无活动目录的查询绑定
本会话；主请求不在子代理表时仍沿精确 parent_id 查子树。定向与双真实 TUI 文件读取已验证。

派工快照与执行建议分开：状态面只陈述各 run 的事实，不根据“仍有孩子运行”推导父级必须等待。
根与递归协调者共享同一分工说明；当前角色只加载自己的冻结行为与可选角色索引，不注入其它角色全文。
清除协调者全部外包、结束后也不得接手等旧说明；保留用户限制、活动写集分工与真实依赖等待。
本项已完成定向与三路真实 TUI 验收：独立工作和依赖等待可正常完成；同会话追加有真实孙级交接。
分工质量仍开放：模型仍会转交本应留给自己的入口、反复查询状态，不能把链路完成等同于效率达标。

产物交接补修已实现并通过真实新包主链验收：自然回复从本 run 工具账本交回真实文件，声明与工具使用同一 cwd；
删除内部 output 重定位与隐式搬运。补丁新增精确文件引用和删除墓碑，沿原工具 registry 交接。
实际分层基线已完成，分工选择不冒充运行时故障。详见
[父子并行与交接](docs/design/SUBAGENT_PARALLEL_EXECUTION.md)。

| 决策 | 当前约束 | 设计入口 |
|---|---|---|
| 用户家目录是默认工作区 | 不以启动终端目录改变默认归属；家目录内整理依靠提示约定，权限是硬边界 | [目录规范](docs/architecture/MY_AGENT_HOME_LAYOUT.md) |
| 单一身份与事实源 | owner/thread/task/run/attempt 显式传递；索引仅用于查找展示 | [架构](docs/design/ARCHITECTURE_GUIDE.md) |
| 单 Gateway、多客户端 | 客户端关闭与后台任务生命周期分开；恢复不能重复执行 | [Gateway](docs/design/GATEWAY_DESIGN.md) |
| 会话独立模型配置 | 会话选择不覆盖其他会话；用户默认只影响按规则继承的新会话 | [模型选择](docs/design/SESSION_MODEL_SELECTION.md) |
| 软件不预选模型 | 模型/协议/地址默认留空；未配置只开放设置和历史，用户已保存选择不变 | [模型配置](docs/design/TUI_MODEL_PROFILES.md) |
| 模型统计与上下文分离 | 轮次取调用账，缓存取服务商用量，累计按当前代理会话；展示字段不回灌模型输入 | [统计口径](docs/design/TUI_DESIGN.md#模型统计口径) |
| 单代理单持续目标 | 每个代理至多一个未结束 Goal，主子分别归属；active Goal 在安全边界续跑，不依赖工具数量或 Todo；旧冲突仅显式迁移，不自动激活 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 目标计时只有一份基线 | 同一 Gateway 的同源会话存储共享单调时钟；模型轮次、后台工作片及控制查询不重复累计同一秒；旧多记耗时不从日志推测回写 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| Goal 终态不冒充执行终态 | 完成目标只停止该目标的续跑；当前回合、消息和子树完成后由统一 finalization 关闭任务，不在目标工具中提前关父任务 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 名称不决定执行通道 | Goal 名称不创建额外执行器；已有未结束目标时新名字返回冲突。后台数量不授予前台执行权，父级取精确任务身份 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 目标编号不证明仍可续跑 | 轮限交接重读当前 Goal 状态与任务绑定，旧编号、已完成或暂停记录不产生自动续跑承诺；不新增完成质量判断 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 主子代理共享会话能力 | 历史、插话、停止、压缩、终态以相同底层协议处理 | [子代理](docs/modules/subagent/04-structure.md) |
| 逐项交付与阶段诊断 | 创建不强制等待、成功不等全树；原执行车道接收结果，长静默只诊断、不强杀或自动替代 | [并行执行](docs/design/SUBAGENT_PARALLEL_EXECUTION.md) |
| 终态与通知可恢复 | 先持久化结论，再提交账本，最后按精确 attempt 去重通知 | [收口](docs/design/closeout_state_machine.md) |
| 执行器退出是独立事实 | 进入/退出登记属于 exact attempt；没有 session 可核对 OS 身份，未知副作用保留封存 | [收口](docs/design/closeout_state_machine.md) |
| 恢复游标不等于消费 | 未消费事件分页轮转；成功写入 canonical WAL 后才记回执，失败项可重试 | [收口](docs/design/closeout_state_machine.md) |
| 完整历史与展示窗口分离 | 完整未压缩历史来自 canonical 消息，分页不能裁掉模型记忆 | [上下文](docs/design/CONVERSATION_CONTEXT_DESIGN.md) |
| 权限硬、任务组织软 | 自然语言不决定运行状态、越权、任务归属或验收结果 | [开发规则](docs/development/DEVELOPMENT_RULES.md) |
| 分层验证 | 确定性合同/替身/回放用于开发反馈，真实 TUI 用于最终验收 | [测试分层](docs/design/main-agent-contract-testing.md) |

## 代码体检与后续拆分计划

下一轮已将插件方案并入“拆清依赖”计划，按以下顺序推进：明确依赖与核心边界 → 后台调度拆分 →
公共命令声明/解析 → 最小插件装卸 → TUI 发现/使用 → 并发长任务与故障验收 → 子代理生命周期/交接 →
模型工具循环 → 剩余 TUI → 约 10 个简易插件与组合验收。纯结构批保持行为等价，新能力单独验收。
按用户新范围移除本轮 Audit/摄取重构，现有功能保留；第 9 步补声明式展示和可撤销只读订阅，第 10 步用自有插件检验能力组合。
具体切片、理由与完成标准见 [下一轮结构整理顺序](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#下一轮结构整理顺序待实施)。
用户已授权先将当前基线推送 main、统一部署本机及确认的测试机，再创建十步执行 Goal；详细矩阵见 [执行记录](docs/tasks/REFACTOR_PLUGIN_GOAL.md)。
第 1 步已记录调度、租约、子代理、工具循环、命令和扩展注册的[依赖与副作用清单](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第-1-步依赖与副作用清单)，框架基线按执行 Goal 的实际证据收口。
第 2 步首片将无进展计数和确定性失败退避移至 `conversation/background_progress_policy.py`；只输入标量事实，运行后读取、持久记账、租约与供应退避状态保留原位。旧函数删除，不保留转发层；新切片验收独立记录。
供应退避次片已独立为 `background_supply_backoff.py` 并同版部署，TUI 114—119 已验；状态、执行守卫和事件日志同属一个模块，runtime 构造时读取配置并持有三类消费共用的唯一实例，详见[迁移边界](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第-2-步次片进程内供应退避)。
第 2 步路由切片已在源码分为 `background_goal.py` 与 `background_routing.py`：前者只接收原 Goal/任务/时钟领域和精确查询、发布能力，后者只接收三个只读回调；删除混合职责的 GoalMixin，观察执行保留为原调度器内的独立编排函数，能力预扫归 Wake。353 项相关回归及独立审阅通过，同包部署双机后的 TUI 120—123 已验长 Goal 暂停续采/Compact、并行普通任务、三子代理和定时投递；交付失败及未覆盖分路见 TESTS，具体结构边界见[路由切片](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第-2-步路由切片目标续跑与投递地址)。
租约/恢复片已同版部署：执行 claim 编排迁入 `background_claim.py`，权威恢复查询与日志指纹归 `background_recovery.py`；共享心跳和三类租约各守原边界，324 项定向回归与独立复核通过，真实续租、排队、有序恢复及控制后的原资源复用已验。领取后竞态、各时钟、异常顺序及引用范围见[本片合同](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第-2-步租约与恢复切片)。
恢复入口的现有边界也须保留：客户端 `resume` 只重连会话，`/goal resume` 只恢复目标续跑；它们不能裁决通用 UNKNOWN。当前只有原请求、精确执行身份、旧进程已死及工具副作用可确定时的 recorded-active-turn 自动恢复链；通用 UNKNOWN 尚无 TUI 人工裁决入口，不通过私调 API 或修改数据库冒充验收。活动 Gateway 恢复只在排除其它工作后有序停启应用，不改变系统网络；不能把空闲部署当成活动恢复。
新版真实 TUI 暴露换代清理的既有缺陷：已确认 STABLE 资源被旧轮清理误置 DIRTY。本地修复只保留 STABLE 与原 DIRTY 记录，未确认资源仍保守标脏，不按整个旧 attempt 已结束而跳过清理；正常续轮、活动接管和升级调和共用原事务。旧数据库中已存在的 DIRTY 不自动修复。三项旧实现失败的合同已固定，186 项相关回归及严格 gate 通过，修复已发布并同包部署。TUI 130 已验零工具副作用的原请求有序恢复；133 已验取消后续代保留原 STABLE 全字段、继续使用原累计值并自行退出原解释器，完整覆盖边界见 TESTS。通用 UNKNOWN 不由目标恢复命令裁决。
插件接线已补当前源码核对：工具快照已有 handler 绑定，但冻结可用性不承担热撤销；权限视图共享 MCP 连接，Skill 快照只校验原文件。
后续沿原执行链补激活代次、准入与资源登记的原子边界，覆盖审批/锁等待、重试及重连后的停用；仍待实施，见[接线约束](docs/design/PLUGIN_LIFECYCLE.md#第-1-步接线核对与迁移约束)。
真实长任务发现拒绝记忆在子代理 Goal 自动续轮重建参数时丢失，同一参数再次弹出审批；已发布部署，实际 TUI 70 已验证同参续轮拒绝。
同一执行链的拒绝列表由宿主持有并跨子代理自动续轮/前后台 Compact 传递，仅复用已有工具名和参数哈希判据；不扩大批准、不解析自然语言或添加报告质量裁决。
确切作用域和新调用边界见 [Goal 生命周期](docs/design/THREAD_GOAL_LIFECYCLE.md)，不宣称跨进程重启持久拒绝已经实现。
验证清理已落地：删除无生产调用的旧 verifier integrity 合同及仅检查自造字典的测试，开发矩阵改指实际 verification 运行入口和存储测试；不新增报告评分或隐藏返工轮。
诊断入口清理（已发布部署）：移除已停用结果块协议的 `structured-repair` 场景、专用模拟后端及调用清单，
普通 runner 的结束仍读取宿主 `turn_end`。保留独立的 runner 重试场景及其已有失败记录；不恢复 JSON 修复回合，也不把这项删除算作长任务验收通过。
TUI 70 后续嵌套 Shell 后台执行及前台超时残留进程已发布部署、对应真实 TUI 复现已验：后台语法集中到 shell_syntax.py，管道结束前不回收组长，终止快照纳入出生标识仍相同的独立组成员。
已复现旧实现失败，并核对 TERM 忽略、另一进程组隔离及 PID 复用边界；不按命令文字或个人路径批量停止，也不把静态语法分析当成任意程序的安全证明。
Shell 输出行数修复已发布并同版部署、双 TUI 事实复验通过：末尾 LF 不产生额外空行；正文、截断说明和展示记录共用一个纯计数入口，不改变采集或执行。
TUI 71 已读到源文件第 51 行仍在报告中写成含表头 50 行，属独立模型表述失败；不能以本次计数修复宣布质量问题解决。
前版 TUI 85 再次确认自然子代理终态与独立后台进程终态须分开读取：A/C 孩子结束后程序继续运行，A 最终因生成程序异常退出。
父级有原会话范围内的进程查询能力但没有调用，错误报告不支持添加第二套状态或最终质量裁决；该轮模型交付失败保留，原架构合同不变。
继续核对原生上下文发现投影缺口（已发布部署，实际后台消费已验）：完成事件已有 `service_window_incomplete` 和冻结的剩余秒数，
但活动回合事件及后台完成清单未保留它们，当前唤醒切换后便不再可见。沿既有中性完成合同统一筛选、传递这对字段；
不重新计时，不改变 DONE、Goal 或重派策略，也不把声明窗口当作进程运行时长或质量验收。测试与报告见执行 Goal。

“装备”采用可选插件包：稳定核心保留完整能力，Python 工具优先独立进程，经现有工具/MCP 与权限链执行。
停用不加载代码、不起后台工作、不占模型上下文；卸载撤销命令/工具/重连并有界回收专属资源，保留用户产物与真实操作历史。
安装与启用分离，动态 slash 命令、版本快照、原子发布与失败回退均待实施，不能把已有启动插件当成热卸载完成。
详细生命周期、DeepSeek Harness/OpenClaw 参考、先行切片与真实 TUI 矩阵见 [可装卸插件方案](docs/design/PLUGIN_LIFECYCLE.md)。
方案中的日志、表格、文档、网页、桌面适配、数据库、测试机和代码检查仅为候选功能示例；不代表已实现，也不将业务特判加入通用核心。
已补 GitHub 社区 15 个项目的 README/包声明调查：插件管理、流程可视化、浏览器/桌面执行、复核与故障恢复等；
阅读证据和取舍见插件方案的社区调查小节，未安装实测，不将社区多实例方案或模型评分门直接搬入本项目。
插件工作聚焦自有 Python 体系：安装后按贡献提供中文调用、slash、Skill 或设置入口，并给出使用卡；不是接入 DSH 插件。
发现目录、分发与撤销必须来自同一有效贡献集；完整交互流程和现有静态 TUI 的差距已写入插件方案，状态仍为待实施。
命令主入口采用 `/plugins <管理动作>` 与 `/plugins@<插件ID> [动作] [参数]`：前者列出/帮助/配置/启停/装卸，后者使用插件。
参数、帮助和补全由声明生成，第一版不分散顶层插件命令；停用仍可查静态帮助，业务调用不可用，不误转聊天或 Shell。
首批用本地包和只读 Python 示例验证完整装卸链，依赖隔离与撤销一并实现；在线安装、更新/回退另排小批。
后台拆分所测框架范围已收口，现推进公共命令首片；详细步骤与完成标准统一在上述合并计划维护。
已按公开 GitHub 关注度筛候选并随机抽取 10 个功能参考，范围与证据见 [插件样本验收计划](docs/design/PLUGIN_SAMPLE_ACCEPTANCE.md)。
只读文件预览、上下文查看、工作台、活动条、状态宠物、图表、设计文件、OCR、文件快照和浏览器操作均为待实现简易版；不复制第三方运行时或素材。
常规模型调用用官方 MiniMax-M2.7，视觉改用官方 MiniMax-M3，复用同一私有密钥引用；样本不新增第二套任务/历史/记忆状态，不按展示文案决策。

本次独立重构 Goal 的四批实现与本机验收已完成；auth 与 Jev 不属于完成条件。Gateway 分离流式出口与请求编排，
前后台 Compact 携带共用纯计算，mailbox 释放保留原副作用入口；设计和验证状态见可维护性评估。
流式切片已通过定向、事件回放及真实子代理/插话/审批缓存；历史取消后进程继续写入的样本保留。
当前 Goal 暂停不停止资源，资源停止另经精确归属核对；不把子代理取消标签当作进程退出事实，强制终止的 native 收口缺口仍保留。
请求上下文、绑定、历史与输入渲染继续拆分后，666 项定向与真实暂停/压缩/重连、程序续采及并行插话已验；
报告数值遗漏与心算错误按质量失败留证。存储的用量与共享 Goal 时钟已迁为显式对象，629 项定向通过，
新版双 TUI 已验暂停计时、压缩重连、真实续采、并行插话及主子费用归属；报告一处汇总与明细矛盾单独留证。
其余领域已共享原路径、锁与 CAS 完成迁移，没有增加转发 facade。
执行/调度、历史提交/投递、Gateway 适配及存储组合四批均保持权限、身份、取消、恢复和事务合同，
真实验收只走官方 MiniMax-M2.7 的多路 TUI。
执行拆分已落地：同片模型运行、Compact 重试和原生历史保存独立，使用明确的参数准备接口与具名结果；
技术续跑判据归入已有 `turn_end.py`。定向与真实 TUI 的暂停恢复、压缩重连、进程停止续做已验；
子任务 UTC 错误、定时采样不满足间隔及清理参数错误保留，不能把全部任务质量记为通过。
历史提交与投递已拆分，保持外发、canonical 提交和冻结重投的原顺序；调度准入及原存储事务边界保持。
提交/投递批次已落地：投递模块以显式能力和请求字段独立加载，实时任务状态仍在原抑制位置读取；
迁移调用方并删除旧私有转发入口，历史 v1 冻结载荷继续按已有持久数据合同读取。
全仓检查补修（已落地）：同 task 新请求会复用 canonical run，归档必须在权威绑定之后创建；
request 保持消息身份，归档、工具与收尾共用绑定后的 run。归档准备失败要关闭本次新 attempt，不能遗留执行权。
同时收窄已有宽泛参数接口、登记历史读取/任务绑定错误码，并按当前配置与身份合同修复旧测试夹具。
全仓复查 17,070 passed、0 failed，既有 skip/xfail 保留；新版 TUI 的 canonical 身份和返回已验，
模型改用文件工具补记录、时间间隔失真等质量失败保持开放，不能把程序续做宣称通过。

2026-09-19 热点源码评估已完成，用户已授权分批重构，后端协议拆分已通过本地定向验收；Jev 接入仍待实施。先拆后端协议，
再分离会话执行/调度和 Gateway 适配，最后整理深层存储继承；保留唯一状态源与事务边界。
Computer Use 已安装既有可选执行器并完成本机真实 TUI 英文、中文输入与读回；空白控件定位仍有 OCR 局限。
Jev 当前接入方向见[决策模型计划](docs/design/DECISION_MODEL_INTEGRATION.md)：作为可选模型用途逐点接入，仍不担任生成主模型、授权或完成判官。
本轮真实验收仅通过实际 TUI、官方 MiniMax-M2.7；本机桌面与登录认证分别留证，私有材料不进入仓库。
公共 `base` 已收窄到合同与本地后端，HTTP/Chat/Messages/工厂各有唯一实现；旧实现和错误注释已移除。
Messages 长参数入口改为冻结请求对象，后台纯工具策略从会话运行时独立，未放宽 code-size 基线。
后台上下文与历史种子已分别迁入 `background_context.py`、`background_history_seed.py`；
保留任务范围、原生工具记录、Compact 代次及读取错误合同。上下文内既有进度对账可能写任务账，
保持其调用顺序；不迁移锁、存储、唤醒消费或投递事务，删除被后定义覆盖的策略快照重复函数。
并行真实 TUI 已覆盖长任务、取消续做、客户端重连、PTY、后台等待和 Goal 暂停恢复；详细通过边界见验收矩阵。
真实测试发现并修复后台状态地址随 Full Access 漂移、PTY 旧方法调用及桌面输入丢字。
后台状态按宿主 canonical owner home 存取，权限墙独立；桌面输入复用既有 Quartz/PyAutoGUI 并要求后置读回。
auth 表单取消和参数拒绝已验，官方设备码在两处环境被 HTTP 530/403 拒绝，真实账号确认与刷新退出仍待验。
增量真实 TUI 发现首次 `/goal` 未保存客户端工作目录。已修并复验：控制请求携带结构化 workspace，
复用普通消息的目录权限校验，并签入持久回执摘要；只初始化尚无目录的线程，不用控制命令重定向既有任务。
旧回执按显式摘要版本读取，旧版本不得携带未签名的 workspace；不从目标文字提取路径。
全新真实 TUI 的实际目录与线程记录一致；启动后替换空目录为外部符号链接时，Gateway 在创建 Goal 前拒绝。
源码统计、参考阅读、适用边界及分批方案见 [可维护性与 Jev 评估](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md)。

已落地第一步：后台重试策略独立为 `cli/gateway_lane_retry.py`，调度器只负责编排，
会话模型解析只负责编号与 owner 验证，后端工厂与恢复准入共用缺配置判据。
删除 worker/planner 中分散的退避字典兼容初始化与重复判断，避免只加转发壳。

后续热点可继续评估 `conversation/runtime.py` 的恢复及 `cli/gateway_loops.py` 的维护车道；
本次 Gateway 请求职责已按上述边界拆分，存储深继承已按直接调用方和显式领域依赖改为组合。
不在修行为的同一批进行大文件移动，不放宽尺寸基线，不把通过尺寸检查说成可读性已经理想。
每批保留 focused 回归、文档同步和真实 TUI 验收；模型请求、权限、持久状态是不可意外改变的边界。

## 当前待落地或待复验

- 已实现（2026-09-27，Codex 全仓复现）：进程会话停止与 host 自行退出的并发收敛。
  - 问题：host 读到停止意图后会自己清完 child、写回已确认回执并退出；调用方在锁外采快照时若 host 刚好退出，只能拿到未确认的 identity_changed，整次停止被误报成 unknown。
  - 规则：记录已是 host 写的终态且顶层回执已确认、两级实例都按出生身份证明消失，这两条同时成立时，才把该回执改写为已确认的 `host_exited_during_stop`。
  - 不变的部分：身份核对不放宽，其它未确认回执照旧。详见 `docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md`。

- 已实现（2026-09-27，Codex G2 复验请求）：Compact 候选过大诊断。G2 第二代自动 Compact 报 `COMPACT_CANDIDATE_TOO_LARGE`
  时，failed 进度只有未计量的 after_tokens=0，无法判断是摘要过长还是固定开销太大。现在会话 transcript 和活动回合两条压缩链
  被输入上限拒掉候选时，错误带 `CompactCapacityFacts`：最小候选的完整下一请求 token、输入上限、摘要估算 token、保留条数、
  试过的候选数。这组计量经 `conversation_compaction_progress.v1` 的可选白名单字段外发，TUI 失败行也会显示。
  不改线程记录 schema、熔断和重试，也不参与候选选择。本轮工具 IR 压缩链不会抛这个错误，所以没有接入。
  - 已实现（2026-09-28，集成者转 Codex 复现）：固定开销与保留 IR 的口径修正。真实宿主里固定开销那一版空摘要撞上候选替换
    入口的非空摘要合同，`fixed_tokens` 恒为 0；保留 IR 又按来源保留区计，把候选会整体替换的旧摘要/旧交接算了进去。
    现在两个恢复入口都有“只计量、不提交”入口（`ConversationCompactView.measure_only`、`fixed_request_projector`），
    只有它允许空摘要，普通候选的非空摘要合同不变；固定开销只在真正抛出失败时量一次，测不出就缺失，不写 0；
    保留 IR 按候选实际发送的原生 IR 计，只去掉 `applied_compact` / `carried_tool_handoff` 两种载体。
    细节与测试见 `TESTS.md` 同日条目和 `docs/modules/gateway/04-structure.md`。

- 已实现（2026-09-28，T3 验收发现）：凭据类字符串配置类型写错时告警并回落默认值，告警一律不回显原值。
  - 名单由配置类声明推出，不另写。
  - 之前列表会原样进入运行配置，或被静默变成空串，`/settings` 里看不到任何告警。
  - 细节见 `docs/modules/gateway/02-progress.md` 同日条目。
- 已实现（2026-09-28，T3 验收观察 2，集成者批准按 B → C → A 各一个提交）：子代理生命周期唤醒片续接原用户回合。
  - B 已实现：唤醒片按精确 `conversation_request_id` 同时读 owner 根索引（owner 取自任务 `run_workspace.json`）和任务
    work 索引，流式过滤，不全量加载。前台轮记录带 `carried_runtime_only`，只进去重、已执行工具和工具轮数，不进本片
    工具账与模型可见交接；新增工具轮额度不变，`background_max_tool_rounds=0` 回落全局正数上限时也一样（见下方预算条）。
    细节见 `TESTS.md` 同日条目。
  - C 已实现：后台整合档与 goal 子代理两档从 `DIRECT_CHILD_CONTROL_TOOLS` 派生，原顺序不变、末尾补上 `list_agents`，
    有锁测；owner/task 策略、显式配置与退休过滤仍只做减法，其它后台档不变。细节见 `TESTS.md` 同日条目。
  - A 已实现：唤醒片记为宿主事件，不再写第二条用户任务。回合触发类型 `TurnTrigger` 只由后台运行时按唤醒信封构造；
    原生 IR 以 `RuntimeFactsTurn(source=host.lifecycle_wake)` 开头，canonical 与文本协议用 `# Host Event` 加固定首句；
    推荐节用固定短名单（`list_agents` 第一）；唤醒事实从唤醒信封字段确定性投影；原任务不在历史里（已核实的 Goal 任务来源，
    或唤醒没有请求编号）时附有界原任务、首句不声称它在历史里，`origin_request_ids` 只放真实历史请求；
    唤醒片保存到会话历史时去掉本片的后台上下文注入。宿主决策只读 `turn_trigger` 和 `wake_signal`，旧数据不迁移。
    细节见 `TESTS.md` 同日条目。
  - B 读错分支修补已实现（Codex 审查）：两处索引各自读取，一处读不到不跳过另一处；读不到时本片 task_attributes 写结构化
    不完整事实（来源与错误类型），压缩重试沿用首次结论；不完整时一次性编排 fail-closed，只认 `replacement_for_run_ids`
    这一条结构化出口（“父级结构化确认”留作后续），是否有效复用创建边界口径（item 覆盖顶层、去空白后非空）。
    细节见 `TESTS.md` 同日条目。
  - 预算已实现（集成者定语义）：`background_max_tool_rounds` 写 0 回落全局 `max_tool_rounds` 时，唤醒片同样按
    “基线 + 携带条数”起算。语义：正数与 0 两支都是“每片新增额度”，正数 N 时新增 N，写 0 且全局为正数 M 时
    新增额度就是 M（片上限 = 携带条数 + M）；全局也是 0（不限制）或留空（5000）时不设片上限，行为不变。只改 `_apply_internal_background_tool_budget`，
    三种情形（另加全局非法值）有测试锁定。细节见 `TESTS.md` 同日条目。
  - 后续设计项（未排期，集成者 2026-09-28 定本轮不做）：carry 不完整时一次性编排的“父级结构化确认”出口。现在只认
    `replacement_for_run_ids`；要加第二条出口，只能是结构化字段（控制事件或工具参数），不能用自然语言确认放行。
- 进行中（2026-09-27，用户要求）：参数中心。用户指出参数和常数散落、同名不同义、改一个要猜含义，并希望 my-agent 能自助
  修改更多设置。方向：每个可调参数一个权威定义（默认值、范围、说明、安全等级、生效时机、读取方），YAML 与 AgentConfig 由测试
  核对；my-agent 可改全部非安全参数，权限、路径、凭据、宿主控制面继续结构性拒绝；每次修改记账、可回滚；TUI 与 IM 用 `/settings`。
  阶段 0 已实现：输出上限统一 65536，构造后端时按已知窗口夹取（≤ 窗口 ÷ 4，公式唯一在 `output_cap_for_window`）；删除无读取方的 `vision_*` 配置；
  验证命令被管道等掩盖返回码时给“未计入”事实。阶段 1 首轮盘点已做：再删 5 个无读取方的配置，新增“每个配置字段都必须有读取方”
  的扫描测试；699 个散落常数与同名不同义参数已列出迁移批次。阶段 2 已实现：登记表（286 项模型可改）、唯一写入口（按类型写入、
  正式加载回读、修改记录与回滚）、`user_config` 工具 search/reset/history/revert、聊天 `/settings`（仅管理员）；修正工具只看从未设置的
  MY_AGENT_CONFIG 导致“找不到用户配置”。阶段 2b 已实现：写权限沿用 F4 Full Access（按 owner、仅管理员，早已存在），只新增
  `self_dev_worktree`（边界项）——管理员 Full Access 时提示词写明正在运行的代码位置与开发工作树，改完提交到该分支由集成者合并部署。
  参数减量第 3 批（internal 字段降级为读取点旁常量）：A 组 24 项命令/展示默认值（分支 `claude/38-internal-constants`）、
  E 组 14 项后台上下文预算/终态工具折叠/工具目录与详情上限/合同状态扫描预算（分支 `claude/9a-batch3-e`）已完成，
  加载器元数据（config_* 四项与 memory_config_warnings）已从 /settings 列表、计数与搜索隐藏；B 组 19 项 gateway/后台节奏与 daemon/dispatch 默认值、D 组 10 项动态超时探针/anthropic_version/媒体预留/微压缩/协议修复次数
  与集成者追加的 `tool_write_inline_max_chars`（分支 `claude/38-internal-constants-bd`）已完成，C 组已合入 main（第 1–3 批与杂项批、`3bba03e22`、`6dc18342e` 均已上线），见参数中心 §6。
  参数减量第 1 批已在分支 `claude/38-delete-dead-config` 完成（2026-09-27，已合入 main `8f73a512c`，双机 step13s）：按逐项分类删除 43 个没有产品读取方的配置项，
  连同只被测试引用的 `agent_core/watchdog.py`、`concurrency/task_lock.py`、`external_knowledge/` 模块；`tool_protocol` 改为代码常量 native，
  不再放行 text；子代理任务记录 `memory_scope` 升 v2。不改任何生效值；旧用户配置里残留的键只告警。细节见参数中心 §6。
  待做：阶段 3 分批迁移常数。运行中安装写保护已实现：边界开关 `protect_running_runtime`（默认开）让正在运行的安装目录对所有工具只读，
  Full Access 也不例外（独立边界键 `runtime_install_roots`，见 gateway 结构文档 Full Access 一节）。
  参数减量第 2 批（分支 `claude/9a-merge-config`，已合入 main `8f73a512c`，双机 step13s）：11 组重复参数各合并成一个旋钮（并发、租约、工具并行、单代理预算、
  artifact 读取预算、归档级别、规则路由、恢复上下文、上下文窗口、温度、runner 重跑次数），守卫文件里被遮蔽的副本删除，
  `max_parallel_tool_calls` 大于 8 与 0 真正生效，runner 重跑次数只剩 AgentConfig 一个家；默认行为不变。
  决策点位参数收口（分支 `claude/decision-point-fields`，2026-09-27）：12 个点位在三份配置里各有的 `timeout_seconds`/`profile_id`
  （24 个）删除，点位期限与模型只在用户长期设置、会话设置里按点位覆盖，默认继承通用值；详见 [决策模型接入](docs/design/DECISION_MODEL_INTEGRATION.md)。
  常用层级（分支 `claude/9a-settings-common-view`，已合入 main，随 step13t 部署）：`/settings` 默认只列 21 个常用参数并提示 `/settings all` 看全部，
  `user_config` 搜索结果标出常用；名单在 `parameter_registry.COMMON_KEYS`，是封闭的产品决策。
  回显值文字统一（分支 `claude/9a-mask-value-display`）：`mask_value` 一处根修，user_config 查看/搜索/改参回执、`/settings`、`config get`
  的布尔显示 true/false、数字照实（原来 False、0 给模型的是空串），`/settings` 的特判删除；`test_value_display_parity` 钉住各出口一致。
  嵌入服务合并（分支 `claude/9a-embedding-merge`）：`tool_embedding_*` 删除、`memory_embedding_*` 改名为 `embedding_*`，记忆语义召回与
  工具语义检索共用一个嵌入服务，两个开关各管各的；无产品调用方的 `build_embedder(dict)` 删除，`LocalHashingEmbedder` 移到测试 helper。
  未落地方向：`/model` 目录的 embedding 用途改为引用档案（复用服务商凭据，取代 4 个平铺键）；向量库记录生成模型、不匹配视为不存在并
  提供重新嵌入（方案待写，重点是没有模型身份的旧 `memory_vectors.json` 怎么处理）。
  详见 [参数中心](docs/design/PARAMETER_CENTER.md)。
- 已实现并合入 main、待真实飞书验收（2026-09-26，用户决定）：IM 管理员身份与聊天内工具审批。
  以前管理员只有本机 local/main，飞书用户永远是自己的 owner，IM 客户端也无法确认工具，需要确认的操作一律被拒。
  现在管理员先在本机用 `my-agent admin-password set` 设置密码（scrypt，0600），再在飞书与机器人的一对一私聊里发送
  `/admin <密码>`，把 `(channel, user_id)` 精确绑定为管理员，之后该私聊的请求和控制作用域都解析为 local/main。
  同一渠道身份 10 分钟内错 5 次锁 10 分钟，拒绝文案不区分原因。对这些私聊，由 Gateway 服务端开启交互审批：
  待确认工具经 `/progress` 提示，`/approve <密码>` 只批准本会话唯一待决的一次，`/deny` 拒绝，都写原 permission bridge
  的精确决定文件。密码不进回执（落盘前脱敏为 `/admin ******`）、会话记录、日志、适配器持久队列和 TUI 历史。
  开关 `admin_channel_identity_enabled`。后台续跑与子代理的审批在 IM 里仍无人接收；飞书服务端保留原消息，
  需要用户撤回。详见 [IM 管理员身份](docs/design/ADMIN_CHANNEL_IDENTITY.md)。
- 第一期已实现（2026-09-26，用户批准）：Gateway 安全重启与代理自助重启。管理员主代理的 `restart_gateway` 工具与管理员 `/restart`
  只写结构化重启请求；服务循环两段排空（先等本进程在跑回合、再经工具关口等执行中副作用工具，超时取消并通知发起方），换新进程接班
  （托管时退出码 75），接班进程立即续跑旧回合并通知发起方“重启已完成、不要再次重启”；冷却 30 秒、同会话 10 分钟最多 3 次。
  第二批：终端 `gateway restart` 默认安全重启（`--force` 先停后起）、排空期待处理请求写等待事实、TUI 页脚“正在安全重启”提示。
  第三批：续跑代次执行前 Gateway 写一次结构化 `turn_resumed{cause}`，TUI 据此本地作废旧确认框、冻结旧回复与思考、中断旧工具卡并提示已续跑，
  续跑代次工具卡块号带代次；没有采用“排空收尾时给在途确认写作废”的原设想。未做：部署工具改走安全重启、重启窗口控制面、IM 文字反馈。
  以下为设计阶段记录。
- 设计阶段（2026-09-25）：Gateway 安全重启与代理自助重启。用户要求代理能自己重启 Gateway，且所有 TUI 和 IM 不出事。
  现状：停止只给内部循环各 2 秒，不等在途回合；重启那一刻有执行中的工具，那个会话就停在 unknown，代理自己发起的重启必然如此。
  方案：统一的结构化重启请求；排空时停领新请求、在工具协调器前挡住新的有副作用工具、等执行中的工具归零；脱离的接班进程确认旧进程消失后
  以新进程号启动；沿用现有启动恢复续跑，并给续跑回合注入“已重启”的结构化事实；管理员专用 `restart_gateway` 工具只写请求、立即返回；
  排空超时默认取消重启、不强杀。核对 TUI 与 IM 时另发现：续跑回合重新排队约 10 秒且排在新消息后，同会话新消息可能与它互等；
  重启窗口里 `/stop`、`/btw` 找不到续跑回合；授权确认框续跑后可能被吞。这些列为同一方案的必修项。
  2026-09-26 对照 Hermes 与 OpenClaw 上游：两家都先等回合结束再换新进程并自动续跑，续跑说明禁止再执行重启；Hermes 另有重启前后通知、
  连续重启挂起会话，OpenClaw 另有冷却与合并。据此把排空改成“先等回合结束、再只等执行中工具”两段。权限上 Hermes 要审批，
  OpenClaw 7 月起已把代理的重启动作移除、改为必须人批准。2026-09-26 澄清审批语义：对话里说“重启”只是请求，批准只来自
  TUI 确认框、事先选定的自主/完全访问模式或管理员 `/restart` 命令；飞书里当管理员需先登记渠道身份并补卡片或确认码。
  详见 [Gateway 安全重启](docs/design/GATEWAY_SAFE_RESTART.md)。
- 已实现（2026-09-25，用户批准）：结果未确认的执行轮有了会话内显式出口 `/recover`。此前唯一人工出口 `recover_attempt_unknown`
  没有任何 CLI/TUI/IM 入口，执行者中途死亡后该会话的 active 工作任务每条新消息都在 `create_attempt` 的 unknown 闸被拒，会话永久卡死。
  现在 `/recover` 只读列出当前 thread 工作任务根执行轮里未确认的工具操作；用户核对外部事实后用 `recorded|confirmed_noop|abandoned`
  之一显式解除阻塞，下一条消息接着原任务新开一轮，旧操作不重放。unknown 闸改抛 `RUN_RECOVERY_REQUIRED`，客户端文案指向 `/recover`。
  范围只覆盖 thread 的工作任务；持续目标（Goal）任务的 unknown 执行轮仍无入口，待有真实样本再扩展。细节见 `docs/modules/gateway/04-structure.md`。
- 已实现（2026-09-25，用户确认两项都做）：模型在回合里执行 `my-agent gateway restart`，重启的正是承载自己这一轮的 Gateway，
  命令被自己的重启切断，回合停在 unknown。触发背景：用户让它排查飞书“模型调用失败”，它此前用 `manage_models` 的 `set_default`
  只改了本机主 owner 的默认模型，飞书用户是另一个 owner，仍是 `MODEL_NOT_CONFIGURED`；它随后推测是 Gateway 缓存了旧配置，于是重启。
  - 托管自停闸：Gateway 服务进程启动时把自己的进程号写进环境变量 `MY_AGENT_HOSTING_GATEWAY_PID`，工具、后台命令和 runner 子进程继承；
    `gateway stop`/`restart`/`start --force` 发现要停的正是这个进程号就拒绝并以 2 退出，不写停止请求。不解析命令文本。
    已知边界：直接 kill 进程号或调用 HTTP `POST /stop` 不经过它；从托管进程里启动的终端继承标记，人工在里面重启时需先清掉该变量。
    没有实现“本轮结束后自动重启”：拒绝文案让用户在对话结束后自己重启，模型配置修改本就不需要重启。
  - IM 选模型：`/model` 成为聊天文字控制（IM 与 TUI 带编号形式；TUI 单独 `/model` 仍开菜单），`/model <编号>` 选当前会话，
    `/model default <编号>` 设新会话默认，只列自己的、部署的和管理员共享的模型，不在聊天里新增或收发密钥。
    `MODEL_NOT_CONFIGURED` 文案改为引导发送 `/model`。仍未做：管理员替其他用户预设模型；`manage_models` 回执不写作用 owner。
- 已实现（2026-09-25，用户确认后落地；第二版按用户“不要关了再开”改为原地切换）：客户端随 Gateway 升级。TUI 空闲时在 UI 事件循环线程原地 `execv`，不退出全屏、同一会话、首帧即原对话，跨 exec 只传会话编号与原始 termios（`MY_AGENT_TUI_HANDOFF`）；实现 `cli/chat_parts/tui_upgrade_follow.py` + Gateway `runtime_prefix`，开关 `tui_follow_gateway_upgrade`。发布工具（仓库外）只在适配器实际加载的模块有变化时才重启它。最初设计：现状：TUI 是独立进程，Gateway 重启后会自动重连并补回历史，
  但客户端代码停在启动时的版本（旧 `/help`、旧插件命令目录），没有版本不一致检测；IM 适配器也是独立守护进程，部署工具只切 Gateway 不重启它。
  方案：(1) 部署工具切完 Gateway 后检测并重启同机适配器守护进程，纳入同版核对；(2) TUI 在重连时比对 Gateway 上报的版本事实（wheel 哈希/source_sha），
  不一致且当前无进行中的回合、输入框为空时 `os.execv` 重启自身（同参数、同会话，历史在服务端），否则只在状态行提示“Gateway 已升级，空闲时将自动重启”；
  版本事实来自结构化状态接口，不解析文案。待用户确认优先级后实现。
- 教训并已修（2026-09-25，对照线在 Homebrew Python 3.14 上发现）：随包 Skill 目录 `plugin_skill_dir` 曾用宿主默认 sysconfig scheme 推算激活环境的
  purelib；framework/user 类 scheme 会忽略传入的 base，把目录算到宿主 site-packages，Python 插件的随包 Skill 在非 venv 的 Homebrew 宿主上整体失踪。
  现在显式用 venv 布局 scheme（3.10 用 posix_prefix/nt 等价），回归 `test_plugin_skills.py::test_plugin_skill_dir_ignores_host_default_scheme`。

- 已决定并实现（2026-09-25，用户：“/model 我的 agent 不能直接改……需要给他自己有这个权限”）：新增 `manage_models` 工具，主会话代理可直接
  list/add/save_provider/save_model/select/set_default/delete_model/delete_provider/probe/discover，复用 `execute_model_profile_operation`
  唯一写入口与文件锁；effect 按 action 结构化解析（删服务商 dangerous 走审批门，其余 mutating 按 owner 审批模式），子代理不可用，
  `select` 只读结构化 `conversation_thread_id`。密钥经一次模型上下文与工具参数，回执只含 `has_key`、归档按字段名脱敏。
  开关 `enable_model_profile_tool` 默认开。详见 [/model 设计](docs/design/TUI_MODEL_PROFILES.md)。待真实 TUI 复验：口头加模型→select→真实回复。

- 已实现、待真实停机复验：Gateway 停止排空窗口后，仍在途的模型调用由唯一账本批量记为 failed/`MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`
  （用量按缺报，不补零），并写 `gateway_model_calls_interrupted` 结构化事件；只覆盖 Gateway 进程 agent 自己的账本，
  子代理 runner worker 各自持有的账本尚未纳入（2026-10-02 J17 已纳入进程内 runner worker 与 owner 池 agent，见台账 J17 节）。下一片是启动时对遗留 `running` attempt 的结构化对账（非正常退出的补救路径），
  与本项一起构成"停机结清 + 启动对账"两段自愈，见 [Gateway 结构](docs/modules/gateway/04-structure.md)。

- 已决定并实现（2026-09-24 晚，用户第 5 项"小问题 my-agent 自己搞定"）：无进程身份的悬挂运行轮**不自动判死**——同一 owner 权威库会被多个运行版本写入，"没有身份"不是死亡证明；产品改为在 Gateway 启动时把它们列进状态与事件，并提供显式结构化命令 `runtime-stale-attempts --settle` 按阈值结清为 unknown（记结清来源）。自愈的边界是"看得见 + 一条命令"，不是猜。启动对账仍只对能证实进程死亡的行自动生效。
- 教训并已修（2026-09-25 凌晨，真实 owner 组合验收发现）：持久摘要必须对声明类的字段演进稳定。manifest v5 加可选字段后 `plugin_catalog_digest` 哈希 `asdict(tool)` 导致所有既有激活记录校验失败、安装表整体不可读（真实 owner 上插件能力从 step11d 起静默消失）。现在摘要只哈希有值字段；规则写进函数注释与 `test_plugin_catalog_digest_stability.py`：给 `PluginToolDeclaration` 等被持久摘要引用的声明加字段时默认 None/空，先跑稳定性测试。`activation_sha256`、`installation_ref` 由结构化回执字段派生，不含声明 dataclass 全量。
- 已决定并本地实现（2026-09-25，用户四项决定：插件可用系统解释器；非 Python 插件怎么查工作区路径由开发方选型实测；带可执行文件或外部解释器的包须用户本人确认；OS 沙箱可以试）：包描述 v6 用结构化 `entry`（`executable` 随包可执行文件 / `interpreter` 系统解释器加随包脚本）、`files`、`platforms` 取代 Python 模块入口与 wheel，只替换包描述、环境准备、启动命令三处，安装表/激活/托管进程/目录核对/撤销/执行器/Skill/面板全部复用，没有按语言分支。启用分两步：先给确认回执（程序、解释器路径与摘要、随包文件、本机平台），再凭事实摘要生成的确认码启用；解释器按真实路径与摘要固定在环境 `runtime.json`，每次启动复核，被换即拒绝。第 2 项结论：各语言移植宿主读取检查，跑通 `plugins/sdk/conformance/workspace_read_check.json`（Node 样例全过，8 种典型错误写法均被抓出）；写入上下文暂无跨语言用例。第二阶段 OS 沙箱试点（复用随包 bubblewrap / macOS sandbox-exec，默认关）未开始，接入启动链前与插件线对齐。见[任意语言插件](docs/design/PLUGIN_ANY_LANGUAGE.md)。
- 已决定并本地实现（2026-09-25，用户第 4 项"OS 沙箱可以试"）：插件进程 OS 沙箱试点，开关 `plugin_process_sandbox` 默认关。打开后所有插件进程经唯一的 `AttemptExecutionSandbox` 网关启动（Linux bwrap 新增"整根只读"形态、macOS Seatbelt 非 full 形态）：读范围不变、只写插件自己的数据目录、网络不变（不是网络边界）；沙箱不可用时启用与显式调用都结构化拒绝（`sandbox_unavailable`），不退回无沙箱。已知限制：写工作区的插件在沙箱里写入会失败，读范围未收窄（多用户读收窄留待后续）。见[插件进程 OS 沙箱](docs/design/PLUGIN_PROCESS_SANDBOX.md)。
- 已决定并实现（2026-09-24 深夜，用户第 5 项"单回合超窗渐进压缩"）：单个活动回合多条工具结果在下一次预检前全部内联，会把上下文冲过窗口再撞 `COMPACT_CANDIDATE_TOO_LARGE`（真实样本 129%）。修法不按工具名、不改压缩器：归档入口用 preflight 同口径余量判断，本条输出估算 token 不小于距压缩点的剩余余量就立刻外置（`read_file` 分页也外置，模型只看预览+恢复锚点）并登记 `tool_context_window_overflow(reason=tool_result_headroom)`，下一次预检必走统一 Compact；开关 `tool_output_externalize_on_low_headroom` 默认开。见[验证模块进展](docs/modules/verification/02-progress.md)。
- 想法、未落地：模型档案窗口目前靠人工实测（2026-09-24 用产品后端探到 MiniMax-M2.7=262,144、M3=1,048,576，
  官方文档分别写 204,800/1,000,000，口径都是输入+输出合计）。后续可把供应商 400 "context window exceeds limit"
  的结构化事实回灌成档案窗口的自动校准候选：只提示、需用户确认，不静默改档案，也不从错误文案猜数字。

- 已实现、真实增量已验：模型累计容器生成唯一 `usage_scope_id`；来源切换不更换，进程重启或有界容器重建才换代。
  正常、错误和取消复用唯一结算入口，异常不会凭估算补费用。历史无代次的旧账维持原口径，不自动重写。
  认证已实现、真实账号待授权：沿 owner 私有 provider 管理登录凭据，会话仍只保存模型引用；订阅认证不冒充通用 API Key。
  仅接入已核对的服务商授权流程，不读取其他应用私有凭据或静默更改用户日常模型。

- 已实现、真实工具复验通过：普通插话继续已有任务时，主执行绑定必须同时回填真实 run 与 attempt；
  `request_id` 继续标识当前用户消息，不可被持久 run 覆盖。宿主发布仍携带原消息与真实执行绑定，
  Compact 换代不得混用两者。工具权威门保持精确 run/task/attempt 验证，不新增身份别名或绕过。
  原失败会话恢复后整合通过 14 项测试；另一会话等待两个孩子时插话，真实命令执行成功且孩子继续运行。

- 已修、验收中：真实本地模型任务出现同参数、同结果的成功 Shell 循环；重复门自身的未执行拒绝
  不再重置观测，也不追加到有界诊断窗口挤掉原结果。完整拒绝仍进入工具回执、模型历史及审计。
  执行前只查相同调用的真实结果；无关工具失败不清空该哈希。拒绝正文携带原门的计数和换路说明。
  不新增任务完成门或慢流时限，原阈值、零值不限制、不同结果及真实写入的进展边界不变。
  文本工具解码失败提示能力发现途径，不自动转图或调用额外模型；图片误用只是现场前置事件，
  不能据此断言所有模型复读都已修复。见 [工具协议](docs/design/tool-runtime-unification.md#重复工具观测与恢复)。

- 已实现、主代理真实 TUI 已验：Esc 只中断当前执行轮，有 active Goal 时沿原去重 wake 安全续接；
  `/stop`、`/goal pause` 才明确暂停，普通聊天和目标正文编辑不自动恢复暂停状态。
  同一 Goal 的前台、后台续接共用任务与执行代次，旧取消记录不得覆盖新执行。
  用户补充和纠偏保留在 canonical 会话与压缩摘要中，不强制逐条改写 Goal；
  后台插话须有精确 running claim，并共用原输入回执；合法恢复在换代事务内同步重开 TaskRun，历史关闭事件不删。
  后台模型参数不得另造输入回合编号；先后完成的历史 Goal 不构成未结束目标冲突，恢复不无故改绑。
  新鲜多子代理验收发现生命周期信封中的 Goal task 编号被误当普通 user request 查找；补修先核对本线程
  Goal 归属，再排除该持久输入编号。普通消息编号仍逐项校验，缺失/跨线程/损坏不回退到旧目标；
  原现场与同会话第二个多子代理目标真实复验已过。

- 已修、真实增量验收通过：用量 source 只说明入口来源；成功/异常/取消复用结算，累计容器代次参与去重，
  来源交接不重计，进程重启/容器淘汰重建另计。真实中断后追加消息累计与持久账一致；旧账没有自动迁移。
  本地慢模型工具语法编译、流不完整和单槽排队是不同兼容问题；不通过无界重试、猜补 JSON 或取消权限门解决。
  是否调整目标由模型结合上下文决定，状态变化仍需结构化控制。详见 [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md)。

- 慢模型增加显式每模型排队预算（默认 0，真实组合验收中）：只加到首事件预填充预算，不改流静默、取消或重试。
  单处理槽位排队不能靠模型输入速率推断，需用户明确配置。另有长思考耗尽 16,314 输出 token
  后无正文，正确保留 `MODEL_RESPONSE_TRUNCATED / unfinished`，不能靠延长时间解决。
  下一步需分别验证提供方容量/排队预算和推理输出预算；不能按本地地址硬编码协议或无界重试。

- 已实现，主链已验收、复杂组合待补：父子孙继续独立工作，成功通知只保留有界合批窗口，不等待整树终态。
  模型自然让出才进入直属等待，任一新结果可解除等待；复用 canonical 事件与原执行权。
  停滞诊断区分慢首 token、流活动、长工具与已退出执行器，不引入静默超时强杀或自动替代。
  四路真实 TUI 已验证快结果接入、父级独立工作和低阈值提醒后继续完成；后续已完成真实孙级交接，重复实现仍需改进。
  设计和验收边界见 [并行执行](docs/design/SUBAGENT_PARALLEL_EXECUTION.md)。

- 已落地并完成真实 TUI 主流程验证：每个代理会话至多一个未结束 Goal；主代理与每个子代理独立保存。新增第二个名字不再启动另一个执行器。
  派工可只给 prompt，也可显式附持续目标；Todo 按代理复用已有账本、完全可选，不参与完成门。
  TUI 通过方向键和 Enter 打开目标草稿，明确保存才生效，退出丢弃；保存使用内容版本比较，计费刷新不制造编辑冲突。
  修改内容不隐式恢复暂停目标，不改变 run/task、历史和权限。主子目标保存、放弃、停止以及父子消息隔离已实测；孙级、IM 和旧数据迁移仍待专项验证。详见 [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md)。

- 已实现并真实验收通过（2026-09-24，main `a1fea9e54`，本机 workspace-peek 0.1.1→0.1.2→enable 成功）：`/plugins update <插件> <新包路径>` 首片——同 ID 新包替换已停用安装，install+可选 configure 两步回执保留兼容配置，不新增持久动作；不做双版本准备切换与 rollback（需安装记录持有候选版本字段，另开一片）。见 [插件方案](docs/design/PLUGIN_LIFECYCLE.md#管理与使用分开的命令语法待实施)。
- 已实现并真实验收通过（2026-09-24 晚，片 C，main `94a2d0b4d`，双机 runtime-step11c；M2.7 阈值自动压缩 checkpoint `vision_summary/declared/summarized=1`）：随图摘要改为“先看图后总结”两步——含图回合按摘要预算打包成若干看图小请求（`compact_vision_digest_max_requests`），
  要点文字进普通文字摘要请求，图块统一投影为归档引用；准入按最大的一次小请求判断，因此阈值自动压缩与手动 /compact 走同一条路；
  部分成功记 `vision_digest_partial` 与双计数，一次都没成功同次回落 A。见 [媒体压缩策略](docs/design/COMPACT_MEDIA_POLICY.md#片-c先看图后总结2026-09-24用户决定第-2-项随图摘要必须在自动压缩里生效)。
- 已实现并真实验收通过（2026-09-24，main `8c6d29c5f`，双机 runtime-step10r；M3 手动 /compact 走随图摘要，阈值压缩按预算门回落并记原因——片 C 已解决这一回落）：媒体压缩策略片 B。auto 下由结构化事实选随图摘要：档案 `input_modalities` 声明，
  或宿主一次 8×8 纯色图探针（进程级缓存、只认结构化工具回答）；强制恢复、同代次 B 曾失败、视频、字节/摘要预算不满足都落归档引用并在 checkpoint
  记 reason。B 单请求失败统一 typed `COMPACT_VISION_SUMMARY_FAILED`，只写线程代次标记不进熔断。见 [媒体压缩策略](docs/design/COMPACT_MEDIA_POLICY.md#片-b-实现记录与偏差2026-09-24按代码事实调整不改原则)。
- 已修并真实复验通过（2026-09-24，main `4ec0e11f3`，双机 runtime-step10o；本机两段式派工/唤醒任务与测试机停用重启用均通过，见 TESTS）：后台唤醒续跑的工具目录不再是封闭名单。默认 profile 决策带 `extension_tools=inherit`，
  运行构造方按注册表代理类型事实并入当前已启用插件/MCP 工具；显式配置或任务白名单标 `none`。停用撤销与禁用表仍在注册表/快照
  fail-closed。同批：停止重试可按 PID 出生标识结清实例已消失的旧 unknown 进程记录，插件停用不再卡在 `activation_unsettled`。
  见 [后台工具策略边界](docs/modules/gateway/04-structure.md#后台工具策略边界) 与 [受管后台进程](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md#重试结清旧未知记录)。
- 执行器退出与积压分页修复已落地并通过定向回归，真实 TUI 故障组合待验；不按静默时长判死。
- 长会话的模型输入、展示估算、累计用量和缓存费用统一核算，并保留压缩前后的可恢复历史。
- 旧会话目标冲突的显式迁移、小时级慢模型并发操作、IM 环境能力仍需专项验证。
- Goal 提前关闭任务及命名后插话身份断裂已通过真实 TUI；多目标去重分工和重复正文仍未解决，模型自行填写 Goal 时限仍是开放边界，
  尚未把模型的时间参数改为必须由用户控制面授权的方案，不通过解析用户正文判断。
- 详细现象和优先级以 [STATUS](STATUS.md) 与 [ROADMAP](docs/ROADMAP.md) 为准。

## TUI 阅读位置与插话时序

本轮 TUI 原地阅读与插话时序修复已实现，验收按 [交接记录](docs/tasks/TUI_READING_HANDOFF.md) 的精确版本与范围核对。普通/详细/原文共用阅读锚点，
内部有界分页连续滚动；插话提交边界、显示检查点与跨片历史排序共用精确输入身份。
Goal scope 同时公开宿主续跑机制事实，不把 active 或单轮 final 当作长期运行证明。
边界和验收见 [完整原文](docs/design/TUI_COMPLETE_DETAIL.md) 与 [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md)。

## 发布资料约定

产品统一命名为 my-agent。文档只保留使用、部署、功能和开发资料；示例使用保留域名或虚构用户。真实凭据、个人数据、临时评估产物、机器现场配置不进仓库。

运行必需的服务商名称、模型 ID、HTTP header、兼容文件名、依赖名、正式仓库地址保留。第三方许可和版权说明见 LICENSE、NOTICE 及对应 vendor 目录，发布包必须携带。

决策模型12.4混合来源恢复本地切片已验：在原transcript候选中合并活动工具精确分区和完整模型可见材料，v3双覆盖、单writer/CAS及原完整容量门；不增加持久状态。细节见[容量审计末节](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)，738项定向与本片严格gate通过；初次/手动、真实IR组合和供应商验收未完成。

决策模型12.4首次准备切片进行中：完整请求压缩放在 child/主代理候选选模及发送前拒绝回退基线之前；无压缩也只消费同次冻结输入，不重复准备。手动 Compact 没有下一轮业务输入，只证明当前会话历史估算与原覆盖/CAS，下一轮独立验证完整容量，不能把手动成功展示为未来请求容量通过。

决策模型12.4真实IR来源切片进行中：同一临时工具来源扩展保存完整原AssistantTurn/ToolResult分区及保留IR，覆盖仍取原ToolCall四元ref，不以archive preview代替真实模型可见正文。未知/不完整/媒体组保留，关联archive也不获得覆盖；同ref原IR优先归档投影。机械回退须保留全部所选来源，分段退化无法证明完整时拒绝提交，仍用原writer/CAS及完整容量门。

决策模型12.4外层overflow原生IR接续进行中：同一宿主回合只在context_overflow返回临时冻结的typed IR/工具上下文/已转发指引，沿原RunParams回入；新请求、跨任务、跨scope/view不得沿用。主代理attempt由原DB轮换，旧ToolCall的原四元身份保持，carrier不授予执行权；权限/工具快照/provider历史前缀仍重新准备。插话UserTurn增加仅内部input_ids，与原mailbox packet同源，释放只按ID剔除，禁止正文匹配。保留原用户IR和媒体引用，不重复初始化用户轮。仍只有原archive恢复执行预算、原Compact writer/CAS提交。

外层原生IR接续补充（已本地实现，验收中）：宿主冻结的typed AppliedCompactContext是摘要线程来源；taskless后台缺task属性时不补写以免误升任务，已有当前线程声明仍按child优先核对。request_id同逻辑回合稳定；强制恢复必须有消息或完整工具来源，carry本身不证明可压。详细边界见容量审计末节。

# 客户端资源寿命与低配置并发（2026-09-23，候选本片验收通过）

解决长历史逐帧处理、旧测试客户端驻留和状态入口拥塞；区分 IM 身份、持久排队、
执行槽与 HTTP 连接，保持既有任务权威。资源合同与验收边界见
[资源寿命](docs/design/TUI_RESOURCE_LIFETIME.md)。已在独立测试机分层验收，最终统计见 TESTS；未发布默认环境，不将有限采样外推为无限耐久承诺。
