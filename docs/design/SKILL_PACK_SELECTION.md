# skill 与能力包：挑选、会话沿用和"不比原版差"的验收

状态：用户 10-07 已确认，实施中（3a 统筹、处理疑难和真机测试；my-agent 会话 w1 做第 1–2 步，w2 做第 3 步的测量工具，
模型 gpt-6.1-sol）。MiniMax 用量用户不设限。代码基线 main `a5ea6778a`。台账入口：`DESIGN_LEDGER.md` learnpack 一节
"挑包和挑 skill 是同一条路"条。相关设计：[CAPABILITY_PACKS.md](CAPABILITY_PACKS.md)（包协议、一次能力选择）、
[LEARN_TO_PACK.md](LEARN_TO_PACK.md)（学外部 agent 做成包）。

## 0. 达标线（用户 10-07 定："能和原版最差能一致就行"）

- 对比对象：10-05 对照评测里的原版（Claude Code＋原版技能，模型 MiniMax-M2.7），每道题两遍，交付在
  `~/.my-agent/releases/claude-tools/bench-2026-10-05/runs/orig/`。
- 每道题：她两遍里差的那遍，盲评得分不低于原版两遍里差的那遍。
- 每个方向（短剧、小说）：她 6 遍的判据总分不低于原版 6 遍的总分。
- 每遍都要交齐文件。花费（token）不算进达标线，照实记录（10-07 实测她是原版的 2–4 倍）。

## 1. 现状（核对代码）

- 平时的挑法：每轮提示里有一份目录，列出可用 skill 和已启用能力包，每项一行（名字、一句简介、编号），占上下文的 2%，
  有 skill 时包最多占一半（`capability/router.py` `render_skill_metadata_index`）。她自己用 `skill_search(action=get)` 打开。
  装、停、删下一轮就生效。和 skill 的区别只在打开以后：宿主把包的版本锁在这件活上。
- 关键词提醒：用户原话里整词出现包名或关键词时，多一段"本轮能力包候选"（`render_package_recommendations`）。
- 开工前选包（`enable_capability_package_selection`，默认关，生产也关）：新任务第一次业务请求前，用主模型单独问一次
  （输入不超过 3000 token，只看用户这一句，不看历史），选中后宿主读入口（不超过 3000 token）放进本轮。
  同一件活不重复，但普通聊天基本每条新消息都算一件新活；纯问答也会留一条任务记录。第 1 步落地前改这个开关要重启 Gateway：
  能力配置读一次就缓存在 agent 上，当时只有两个"自动装"开关每次现读。w1 本地实现已把选包开关接到同一现读入口，
  管理员 `/settings` 如实提示"马上生效"，预算仍保持缓存；待 3a 复核和部署，不代表生产已切换（落点见第 3 节）。
- 压缩：会话压缩不保留读过的 skill／包正文，压缩后只剩摘要里提一句。
- 实测：
  - 原版两遍都没调用自己的短剧技能。
  - 能力包第一轮验收，3 个该用包的用例零读取；包段补了"对口先读入口"的规则后，第二轮她才自己用上。
  - 她做的小说包关键词全是英文，两遍小说题都没用上。题面点名用包的那一遍，分数也没更高（只有 1 个样本）。
  - [CAPABILITY_PACKS.md](CAPABILITY_PACKS.md) 一次能力选择一节的结论：继续堆软提示，不能变成稳定的方法加载。
- 对照项目：
  - Claude Code（本机对照副本 free-code `c39d4a1`）：会话状态里记下调用过的 skill。压缩后用 `invoked_skills` 附件
    带回正文，最近优先，每个最多 5000 token、总共最多 25000 token，并写明"本会话调用过这些，继续照做"。
    每轮用小模型预取相关 skill 还在实验开关后面；代码注释说，旧的每轮阻塞查找在线上 97% 什么都没找到。
  - Hermes（本机副本 `2182de55b`）：同样是目录加 `skill_view` 按需读。目录说法是"沾边就必须先读"。
    目录按文件修改时间和大小校验缓存，不用重启。

## 2. 顺序

| 步 | 做什么 | 解决什么 | 估时 |
| --- | --- | --- | --- |
| 1 | 选包开关改成每次现读 | 改开关要重启 Gateway | 约 1 小时 |
| 2 | 会话沿用（skill 和包一起） | 断断续续写要提醒；压缩后方法丢了 | 约半天 |
| 3 | 第一次挑中：三组对比测试，按数据定方案 | 新会话、换语言时第一次挑不中 | 约半天（多数在等模型） |
| 4 | 部署，生产验收"不比原版差" | 证明达标线 | 约半天到 1 天（多数在等模型） |

先做第 1 步，是因为第 2 步的新开关要用同一种现读。每步都要过本地严格 gate、testbox Linux 3.10／3.11／3.12、
线上 PR CI 全绿，再合 main；部署前 main 的线上 CI 也要全绿。

## 3. 第 1 步：开关改完立刻生效

- **原问题**：管理员发 `/settings set enable_capability_package_selection true`，参数中心提示"重启 Gateway 才生效"，
  当前进程照旧按关跑。
- **改后**：改完下一条消息就按新值跑，参数中心如实报"马上生效"。
- **怎么改**：
  - 把两个"自动装"开关用的"每次按文件现读"做法，扩到一份名单：两个自动装开关、选包开关。读不到或文件坏按默认值，不报错。
    第 2 步才新增 `conversation_method_carry_enabled` 并把它加到名单，本步不提前加配置或名单成员。
  - 读选包开关的两处改走现读：`capability/package_selection_scope.py` 和 `capability/subagent_package_entries.py`。
  - 参数中心按这份名单报生效时机（`settings/parameter_registry.py`）。
  - `capability_config.yaml` 头部"改完重启 Gateway 最稳妥"那句改成写明哪几个键马上生效。
- **测试**：
  - 5 个合同用例：参数中心报"马上生效"；不重建 agent 改文件，关→开、开→关都在下一次请求生效；坏文件按关；
    子代理入口同样现读。
  - 2 个变异（改回缓存读）必须被抓到。

- **实际落点（w1，2026-10-07，本地已实现、待 3a 复核）**：
  - `agent_py_agent/agent/capability/self_install_switches.py` 的 `CAPABILITY_FRESH_SWITCH_KEYS` 是马上生效开关的唯一名单，
    保留 `SELF_INSTALL_SWITCH_KEYS` 的两键回执合同。`_read_fresh_capability_snapshot` 共用原文件优先级和正式加载器，
    `read_fresh_capability_switch` 在读不到时按 `CapabilityConfig` 对应键默认值处理，不读写 agent 缓存。
  - `package_selection_scope.package_selection_scope` 只把是否开关改为现读；`PackageSelectionScope.config` 及预算仍取
    `runtime_config_reload.capability_config_for_agent` 的原缓存实例，没有改它的缓存语义。
    `subagent_package_entries.subagent_entries_enabled` 共用现读入口；已有线程、授权、pin 和首请求资格保持原样。
  - `settings/parameter_registry._capability_specs` 按唯一名单给出 `EFFECT_IMMEDIATE`；其它能力参数仍为 `EFFECT_GATEWAY_RESTART`。
    YAML 头部列出三个立即生效键；`frontend/scripts/sync-backend-config.mjs::backendRestartRequirements` 通过 Python
    直接读后端 `parameter_registry()` 的 effect 生成 `restartRequired`，没有在 JS 或目录守卫里重抄名单。
    生成器默认 python3（Windows 为 python），项目解释器可经 `PYTHON` 指定，失败即中止，不猜时机。
  - 五类合同及管理员 `/settings` 保存→原 agent 下一次主/子开关判断，记录在 `test_capability_selection_scope.py`；
    `test_backend_config_catalog.py` 逐字段对后端 effect。测试、基线失败复核和两处有效变异见 `TESTS.md` 的 w1 第 1 步节。
  - 会话沿用、真模型、实际 TUI/飞书、Linux 通道与生产部署不在本步本地验收内，均未验证。

## 4. 第 2 步：会话沿用

- **现在**：
  - 她在第 1 轮打开了小说包，之后每轮能不能接着用，全看她自己。
  - 隔几天回来只说"接着写"：不会有关键词提醒；开工前选包只看这一句，也挑不中。
  - 聊天记录压缩以后，读过的包正文就没了。
- **能力包和 skill 是两码事（用户 10-07 提醒）**：skill 是一份方法文档；能力包是一整套，有入口、多条流程和阶段、模板、
  检查脚本、宿主核验和交付要求，还有版本和任务锁定。会话沿用以能力包为主，记得更细；skill 只做简单版。
- **改后**：
  - 能力包：这个会话里她打开过的包，程序记下包、版本，以及她读过的包内资料（每个包最近 8 份的路径）。之后每轮目录里，
    在用的包排最前，标"（本会话在用）"，规则多一句："接着做同类的事，继续照在用包的方法和你走到的那一步；不相关的事照常处理。"
    每次压缩后的下一轮，程序把入口按预算重新读进本轮，再附上"压缩前读过的包内资料"清单。清单每份带可以原样重读的参数
    （`next_read`），她能从原来那一步接着做，不用从头再走一遍流程。
  - skill：只记编号。目录里同样标"在用"，压缩后带回正文开头。
  - `/skills using` 列出本会话在用的包和 skill；`/skills using remove <名字>` 让本会话不再沿用它。TUI 和飞书都能用。
- **怎么改**：
  1. 会话线程记录加一栏"本会话在用的方法"（`conversation/models.py`，存法同 `pending_host_notices`，不直接给模型看）。
     - 能力包每条记：包编号、最近读到的版本、最近一次读的时间、读时的压缩代次、已带回的压缩代次，以及最近读过的
       包内资料路径（最多 8 份，入口不算）。
     - skill 每条记：编号、最近一次读的时间、读时的压缩代次、已带回的压缩代次。
     - 总共最多 5 条，按最近使用排序。
  2. 记账点：`skill_search` 读包成功（入口或包内资料，后者记下资料路径）、读 skill 成功时记一笔（`capability/skill_search_tool.py`
     两处成功返回前）。只记主会话：子代理、隔离和控制面的请求不记；读失败不记。
  3. 目录：在用的排最前并标注，预算紧也保证显示，复用"必须显示"那条规则（`required_skill_ids`）。
     改 `capability/router.py` 的渲染，`prompting_parts/builder.py` 把本会话在用名单传进来。
  4. 压缩后带回：线程的 `compact_generation` 比"已带回的代次"大时，下一轮开工前（`_tool_loop_service` 里开工前选包的同一个
     接缝）带回一次，并记下已带回的代次，每代只带一次。总预算沿用 `capability_bundle_max_tokens`（3000），包先 skill 后，
     最近用的优先。
     - 能力包：入口复用开工前选包已有的"读入口、按预算装配、核对版本"那段
       （`package_selection_context.prepare_package_entry_context`）。资料清单按当前装着的版本过滤，新版本里没有的路径不列；
       只列路径和重读参数，不带正文。
     - skill：用快照读正文，截取开头。
     - 读出的内容按"宿主读取的参考资料"放进本轮上下文（`record_runtime_facts_turn_ir`），之后随历史走，到下次压缩再带一次。
  5. 什么时候不再沿用：
     - 包停用、删除，或 skill 被删：以当前装着、启用着的为准，目录里不再标注。
     - 包升级：照样沿用，按新版本读；资料清单按新版本过滤。
     - 你发 `/skills using remove <名字>`。
  6. 新开关 `conversation_method_carry_enabled`，放 `capability_config.yaml`，默认开，按第 1 步现读。
     关掉时，发给模型的提示一个字节都不变。
- **代价**：每轮每个在用方法多一行，几十 token；每次压缩后多一次不超过 3000 token 的带回，随历史缓存；不多调模型。
- **边界**：
  - 只认程序自己的结构化记录，不解析用户的话。
  - 带回的内容是参考资料，不增加任何权限，不执行包里的脚本。
  - 子代理的包授权照旧走 `allowed_skills`。
- **测试**：
  - 约 23 个合同用例：
    - 记账 9 个：读包入口、读包内资料（记下路径）、读 skill 成功各记一笔；资料路径每包最多 8 份；读失败不记；子代理不记；
      隔离和控制面不记；重复读只更新时间；超过 5 个挤掉最旧的。
    - 目录 4 个：在用的排最前并标注；预算紧也显示；包停用或删除后不再标；skill 删除后不再标。
    - 压缩带回 5 个：代次变了下一轮带回，预算内、包先 skill 后、最近优先；资料清单带重读参数，包升级后按新版本过滤；
      同一代只带一次；没压缩不带；包读不到只记提示码、不报错。
    - 命令 3 个：`/skills using` 列出；`remove` 生效；IM 同样可用。
    - 开关关掉时提示字节不变 1 个；旧线程记录没有这一栏也能正常读 1 个。
  - 1 个假模型端到端：Gateway 第 1 轮读包，`/compact` 后第 2 轮上下文里有带回的入口，目录里有"在用"标注。
  - 约 10 个变异，只有 pytest 退出码为 1 才算抓到。

### 第 2 步实际落点（w1，2026-10-08，本地实现，待 3a 复核）

- 功能/测试提交 `673c0914d1f78b993edfa1c1d546bc39d3646c52`；242 项无变异最终复验、十处有效变异和基线失败边界见 TESTS 首节。
- 唯一负责模块 `capability/method_carry.py`：`record_method_read` 在主会话成功 get 后经原 `threads.update_atomic` 合并隐藏账本；
  包记录当前版本、读代次和最近 8 份非入口资料路径，Skill 仅保存身份/时间/代次；最多 5 个方法，按最近使用淘汰。
  `conversation/models.ConversationThread.conversation_methods` 空时不输出 JSON 键，加入 `MODEL_HIDDEN_THREAD_FIELDS`；原摘要和 fingerprint 不参与更新。
  `store_threads` 锁内扩展字段合并以 dataclass 显式字段为权威，避免移除最后一项后旧 extra_fields 复活登记，锁/CAS 不变。
- 缓存两条分别保证：`first_used_at` 重读不刷新，`prompt_method_ids` 只按首次使用排序且不展示时间；
  `runtime_mixin.current_prompt_scope` 调 `freeze_conversation_methods`，名单落本 run 参数，不挂线程全局缓存；
  `_runtime_params` / `loop_support` / `loop_models` 透传冻结值，builder 及纯请求投影只读此值，中途 get 只改下一个 run 的账本。
  无合格线程的通用 prompt scope 不写新增字段，保留原嵌套/并发失败恢复；原用例已参与最终复验。
- `router.render_skill_metadata_index` / `_render_package_metadata` 统一调用 `using_method_cards` 标注并复用 required 必显；
  在用包规则只在当前授权目录确实含在用包时追加。无在用项或关闭路径锁定旧目录字节；停用/删除/旧 pin 失效不补造卡片。
- `_tool_loop_service.next_tool_loop_model_response` 在选包准备后、首业务 build/capture 前调用 `prepare_conversation_method_carry`。
  `package_selection_scope.package_read_scope` 抽出原“工具与快照可用”检查，带回不依赖选包开关；`MethodCarryAuthority` 核取消、原 run/attempt 执行权。
  包入口沿 `prepare_package_entry_context` / 原 ActionPolicy / reader / task pins，资料只列当前清单存在的路径与完整 `next_read`；Skill 按完整文本预算截正文开头。
- 预算取原缓存 `capability_bundle_max_tokens`，公共头、JSON 转义、资料参数和分隔符全部计入；包先 Skill 后、同类最近使用优先。
  `_commit_method_carry` 在原线程锁内复核同代和登记实例，实际 `record_runtime_facts_turn_ir(source="conversation_method_carry")` 成功才推进代次。
  同代成功只投递一次；同 run 失败不热重试，失败不消费持久代次，下一合法 run 可再试；取消沿原异常传播。
- `/skills using` / `remove` 的解析、命令目录与 `skill_control_service._execute_using_control` 共用 TUI/IM 控制入口，按认证 scope 的通道绑定找线程；
  不采用 metadata 自称线程、不构造 Agent、不改安装/授权/pins。开关为唯一现读名单第四项，管理员边界、YAML/dataclass、前端目录同源。
- 三轮假模型 Gateway 联验使用真实 get、原 `write_compact_checkpoint` 和同 Store 的 Compact/CAS 提交；第 2 轮有入口、资料重读参数和目录标注，第 3 轮同代不重复。
  只替换模型传输，不代表真实客户端、MiniMax 采用或生产验收。测试、变异、门禁及失败基线对照详见 TESTS.md 第 2 步节。

## 5. 第 3 步：第一次挑中（三组对比，按数据定）

- **现在**：第一次能不能挑中，看她自己有没有注意到目录那一行。关键词提醒只认同语言的整词；开工前选包关着。
- **测什么**：新会话第一句话，她能不能打开对口的包；无关的话会不会乱开包。
- **怎么测**：
  - 环境：隔离的 my-agent 家目录，不碰生产。装好、启用同样的 10 个包：8 个对照评测的样包（短剧、小说、数据分析、
    学术写作，各有中英两版）加她自己做的 2 个。模型用 MiniMax-M2.7 官网，提示用当时 main 的代码拼。
  - 句子：语义校准用过的 39 句。中文 15、英文 12、法语 12；25 句对口（每句对口 2–3 个包），14 句无关。
    来自 `~/.my-agent/decision-evidence/learnpack-prod-c775bd4b4/semantic-calibration/`。
  - 三组：
    - A：现在的目录说法，她自己挑。
    - B：目录说法加硬，照 Hermes：沾边就先打开入口看一眼。
    - C：打开开工前选包。
  - 每句每组各跑 1 次，都是新会话的第一句话。
    - A、B 只发第一次请求，看她第一步有没有调 `skill_search(action=get)` 打开对口的包，看工具账本，不执行后续工具。
    - C 看选包结果。
  - 记两个数，分语言列：
    - 对口率：25 句对口的里，打开了对口包的比例。
    - 误开率：14 句无关的里，打开了任何包的比例。
- **判定规则**（跑之前定死）：
  - B 对口率 ≥ 80% 且误开率 ≤ 10%：采用 B，选包开关不开，不多花钱。
  - 否则，C 对口率 ≥ 90% 且误开率 ≤ 5%：打开 C。代价是每条新消息多一次约 3000 token 的调用，多等几秒，
    任务列表里每条消息多一条记录。
  - 都不够：用 A、B 里好的那个，C 不开，把数据给用户看再定。
- **用量**：A、B 每次约 4 万输入，39 句两组约 310 万；C 每次约 3000，约 12 万。合计约 320 万输入 token，大部分命中缓存。

## 6. 第 4 步：部署和生产验收

- **部署**：本地严格 gate → testbox 三个 Python 版本 → 开 PR 看线上 CI 全绿 → 合 main 推送，main 线上 CI 全绿 →
  部署 step17x → 在 TUI 和飞书各发一次 `/skills using`，确认能用。
- **测试者只旁观**：每遍只发一次题面，看她自己的日志、产物和账本，不替她改文件、补产物。
- **验收 1，对原版（达标线）**：
  - 短剧 3 题、小说 3 题，每题她跑 2 遍，共 12 遍。都是新会话，题面和原版一字不差，不点名包。
  - 和原版同题的 12 遍放在一起盲评，评审不知道哪份是谁的。按第 0 节判定。
  - 同时记机制事实：每遍有没有用上包（工具账本）。目标每个方向至少 5／6，只记录，不算进达标线。
- **验收 2，断续写**：
  - 小说一条会话：
    1. "按这个大纲开始写，先写第 1–2 章"（不点名包）
    2. "接着写第 3 章"
    3. 手动 `/compact`
    4. "接着写第 4 章"
    5. 用英文说 "Continue with chapter 5"
  - 短剧一条会话：分集续写，步骤同上。
  - 看三件事：
    - 每句都用上了包（工具账本或带回记录）。
    - 压缩后带回正好发生一次（线程上的已带回代次）。
    - 后面几章的盲评得分不低于第 1–2 章。
- **验收 3，跨语言**：短剧第 1 题翻成英文、法文各跑 1 遍。对口包要用上，得分不低于原版这题差的那遍。
- **验收 4，不乱用**：在一条正在用小说包的会话里问 3 个不相关、不联网的问题。她照常回答，不硬套小说方法，由盲评看是否跑题。
- **没达标时**：
  - 逐条判据找原因，按通用层修，不为某道题加专项分支。
  - 修完重跑没过的那道题，2 遍。
  - 结论如实写进本文和台账。
- **用量**：

  | 验收 | 输入 token |
  | --- | --- |
  | 验收 1（短剧每遍约 20 万，小说每遍约 60 万） | 约 500 万 |
  | 验收 2 | 约 250 万 |
  | 验收 3 | 约 40 万 |
  | 验收 4 | 约 15 万 |

  合计约 800 万，九成左右命中缓存。评审用 Claude 子代理，不占 MiniMax。

## 7. 总用量与不做的事

- MiniMax 合计约 1100–1200 万输入 token，九成左右命中缓存，约是 10-07 学做包生产测试（2150 万）的一半。
  跑之前按当时账本再估一遍；遇到额度报错就停。
- 不做：
  - 向量通道：embo-01 跨语言校准不行。
  - 每轮多一次模型调用的预取。
  - 把能力包拆成很多个全局 skill。
  - 改她已经做好的包。
