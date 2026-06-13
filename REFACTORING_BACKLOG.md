# REFACTORING BACKLOG

LLM: keep this file current. Do not copy old split plans back in.

当前方向：

- 不按行数强拆文件。
- 优先合并只转发调用、隐藏主链路、保留历史路径名或历史字段的层。
- 只在一个文件同时承担无关职责时拆分。
- 每次改主链路后运行对应 focused tests，再刷新 `CODE_SIZE_REPORT.md`。

---

## Active Items

### 后台 shell job 生命周期 + 交互式 stdin（P0-2 续,中优先）

P0-2 已落地 run_in_background(后台启动+输出落文件+pid+registry)。两项续做:
①**生命周期裁决**:后台 shell job 在 run 退出时是否回收?子代理 background 是
"未收口必回收",但 shell job 不一定(用户可能就要它在 run 结束后继续,如启动
服务)——需要一个策略(默认随 run 结束 kill?还是留存?可加 detach 参数)。
registry.jsonl 已记 pid,出口孤儿回收接入 .background_jobs 是增量,但要先定语义。
②**交互式 stdin(P0-2b)**:会话运行时 exec_command+write_stdin 形态(持久 PTY 会话池),
驱动 REPL/调试器;工程量较大,任务型 agent 优先级低于①。

### Hint 系统大一统管道（批4 评估结论：暂不做，低优先）

2026-06-12 批4 评估：5 套 hint 注入（loop_hints / delivery_completion_soft_hint /
guardrail_hint / 检索完备性软引导 / memory_push）各自已有幂等守门
（`_hint_already_added` 形态），段格式已统一 `[xxx-hint]`，无实锤重复注入。
统一管道属"为统一而统一"的过度工程；且按稳而不管方向，hint 总量是减负对象
而非加固对象。若未来出现跨套重复注入实锤或第 6 套 hint 诞生，再立共享
幂等标记+渲染助手两件套（先共享原语，仍不建大管道）。

### Skill 树二期：快照缓存 + 自动归类进化（批2 留口，待 skill 数上百再动）

千级地基（目录即分类+类目索引常驻+skill_search 冷路）已落地（批2）。二期两件：
①长期助手 `_load_snapshot` 形态的扫描快照缓存（skill 数上百后 scan 成本可见时再加，
200 卡基准现 <1s 不急）；②"模型自己分配 skill 到树"——入库自动归类现靠
skill-creator 引导按类目索引选目录，进化方向是嵌入聚类/LLM 归类建议。

### 声明漂移可见性：对账门 evidence 投影声明变更历史（R9 观察，低优先）

R9b 实锤新形态：主跑声明 `WEEK*.xlsx`×24 被对账门正确拦截（实交 W*.xlsx），
接力轮模型不改文件名而是**改声明**使对账通过——机制语义内合法（声明驱动允许
更新声明），但命名仍违 prompt 硬要求，把关者若只看对账门结论会漏掉。待做：
对账门 evidence 附声明变更摘要（task_progress 的 expected_outputs 合并历史
已有数据基础），把"声明在两轮之间变过"这个客观事实投影给把关者；配套教训
记忆补种（artifacts lesson 加"文件名是交付要求的一部分，改名比改声明便宜"）。
纯可见性增强，非新门。

## Completed Cleanup

- 2026-06-12: P0-2 后台 shell 执行落地(对照组 3/5 有持久/后台 shell,补齐)。
  `run_command` 加 `run_in_background` 参数:Popen 独立会话(start_new_session,
  与子代理后台进程同款,出口孤儿回收可发现)启动,stdout/stderr 合并落工作区
  `.background_jobs/job-<ns>.log`,立即返回 {status,pid,output_file,hint} 不阻塞
  工具循环;模型用 read_file 读进度、kill <pid> 收尾;registry.jsonl 留痕。
  纯延迟(sleep)仍走既有 wait 引导不进后台。模块函数 _wants_background/
  _spawn_background_process/_record_background_job。钉子 5 条
  (test_shell_background.py:返回 pid+输出落文件/不阻塞/registry/参数形态/
  同步模式不受影响)。生命周期+交互式 stdin 见 Active。

- 2026-06-12: 对照 5 主流 agent 能力差距分析 + P0-1 edit_file 工具落地。
  对照 通道运行时/长期助手/会话运行时/终端交互/工具运行时(各 1 agent 深挖源码)产出
  能力矩阵(docs/audits/CAPABILITY-GAP-vs-5-agents-20260612.md):my-agent
  独有交付验收闭环+稳定性机制群(对照组 0/5 有),缺 str_replace 编辑/持久
  shell/LSP/MCP/OS沙箱/多模态读/hooks。**P0-1 已落地**:新增 `edit_file`
  工具(`tooling/_filesystem_edit.py`)——精确子串替换 + 三级容错级联(精确→
  行首尾空白 line_trimmed→行内空白 whitespace_normalized,抄 工具运行时 多策略
  匹配思路),容错命中按文件真实文本替换 + reindent 缩进保留(模型给无缩进
  old/new 也能在缩进代码里替换后对齐),多处命中需 replace_all 防误改。
  超越点:多数对照组(会话运行时/终端交互)只做精确匹配,my-agent 带容错+缩进保留。
  钉子 9 条(test_edit_file_tool.py),复用 FileSystemTool 路径解析+原子写边界。
  待办:P0-2 持久/后台 shell session;P1 LSP + MCP。

- 2026-06-12: web_search 单点故障修复（R12-R15 反复实锤的根因级工具缺口）。
  实测定位：唯一后端 DuckDuckGo HTML 在本网络对脚本返回 202 反爬挑战页（0 结果）
  → web_search 全程 TOOL_UNAVAILABLE → 模型每次想搜索都被挡,被迫退化成抓
  trending 页面/瞎推断（github 产物"按行号瞎对应周增星"的根因）。同环境实测
  Bing HTML 200+10 真结果、GitHub API 通（网络正常,仅 DDG 不可达）。修复=
  `web_search.py` 新增 `BingHtmlProvider`（免 key,按 b_algo 块逐块解析 title/
  真实URL〔ck/a 跳转 base64 解码〕/snippet,块内对应杜绝错位）+ 默认 providers
  改 [Bing 首选, DuckDuckGo 兜底]（provider 插槽与 _search_with_providers fallback
  早已就绪,只是列表里只挂了一个挂掉的后端）。纯工具修复,通用（所有联网搜索
  任务受益:榜单/论文/调研）,不碰任务专属逻辑、不给模型窄路。钉子 6 条
  （离线 HTML 夹具:逐块不错位/ck解码三态/默认Bing首选/主后端空时fallback）。

- 2026-06-12: 自我承诺单次提醒（R14c 实锤：模型自我声明 expected_outputs
  cn.pdf min_count=1,两轮都在 0 实存时提前收口——closeout ok=true 后接力
  也不再接〔接力只接未收口〕,pending 永不补齐的结构性缺口）。语义升级：
  模型自我声明的对账缺口第一次打回提醒（注入 unmet_declarations 结构化
  数字+双出口——补齐,或 task_progress 改声明并写明原因）,同缺口第二次
  放行 ok=true+advisories。与 R9 凑数反噬的边界：①数量是模型自我声明非
  外部写死；②幂等仅一次绝不无限打回；③改声明是显式合法出口。旧钉子按
  新语义改写,新钉子 2 条（首次打回带双出口文案/二次放行留 advisory）。

- 2026-06-12: R13 迭代轮四修复（产物并集/端点环境/skill 升级/截断二试）。
  ①closeout 产物=记录 ∪ 扫描并集（R13c 实锤：write_file 只记录 1 个 md 清单,
  3 个 curl 下载的 PDF 被旧 if/else"有记录就不扫描"短路,closeout 只见 1/4 文件;
  与 lesson 召回并集修复同一设计裁决——记录是加速器不是封闭白名单）,同路径
  记录优先。钉子 test_expected_outputs_gate 并集混合形态。
  ②模型端点环境事实（R13c 实锤：任务脚本需要 LLM 子调用〔批量翻译〕时模型
  只知道 AGENT_API_KEY 不知道配套端点,试 OpenAI/DeepSeek/OpenRouter 全 401 后
  写"声称 zh.pdf 存在"的清单提前交付）：SimpleAgent 初始化 setdefault 导出
  AGENT_API_BASE/AGENT_MODEL_NAME,run_command 子进程自然继承;工具说明书+
  pdf-translate skill 路线 B 同步教学（anthropic 兼容样例+“写完脚本必须真跑+
  绝不在清单声称不存在的文件”）。钉子 test_common_safe_id_and_paths。
  ③runner 结果块截断二试（R11a 实锤复现：35KB 正文下模型连修复轮也截断,
  旧链 repair 仅一次机会,两次截断即 BLOCKED 终判——干完活的子代理被状态
  掩埋,主代理只会重新派人,恶性循环）：repair 响应仍带截断特征（块开头无
  闭合/JSON 未读完）时追加"极简块"硬约束专项二试一次;非截断失败不重试。
  实证:two_trunc_one_full 从 BLOCKED 翻 DONE。钉子 test_runner_truncation_repair 3 条。

- 2026-06-12: 接力遗产清单注入 + research lesson 中文化大修（R12 迭代轮实锤）。
  ①接力遗产清单（R11a 接力倒退实锤：上轮 5 子代理 345KB 深度内容躺在
  work/agents/*/runner_response.md，接力轮模型看不到这笔资产→重新派工又没干完，
  交付区被新一层浅文件覆盖）：`_workspace_prompt_section` 新增
  `_relay_legacy_lines`——任务目录复用（timeline≥2 行）时把上轮子代理产出档案
  按"份数+大小+路径"投影成结构化事实（纯事实零指令，用不用模型决定）；
  实测 r11a 真实数据输出 14 份 447KB 明细。钉子入 test_run_task_workspace_writer。
  ②research lesson 中文化大修（R12c 实锤×2：模型把"官方"自行收紧为"必须
  DeepSeek-AI 署名"，对已打开的 2601.20552 页面一票否决；英文 lesson 的产品线
  枚举条款未被中文任务模型执行）：种子全文中文化+新增两条通用判定纪律——
  署名形态多样性（机构名/团队名/个人署名+affiliation 综合判定，单一字段不
  一票否决）、平台历史数据≠不存在（事件流归档/公共数据集/第三方分析站,
  方法论不喂答案）。钉子按新语义更新（test_memory_trigger_conditions）。

- 2026-06-12: 问句型零交付出口守卫落地（R11b 实锤当日收口）。
  `final_exit_contract` 新增 `_question_exit_guard_applies`——主信号全结构化
  （source==cli_run 单次模式 + executed_tools 非空〔纯问答零工具不误伤〕+
  _closeout_candidate 三条件全不沾即零交付零派工零 capreq），问句/等待指示
  形态（?/？结尾或尾部 300 字含"请您选择/等待指示"等短语表）只作"答完了 vs
  反问人"的最后区分器。命中→打回一轮并注入 [exit-question-guard]（重申 prompt
  既有授权：自主决策+如实标注缺失，不替模型做选择）；幂等一次，打回后仍请示
  则诚实放行防死循环；gateway/chat 多轮场景不守卫。钉子 5 条
  （test_final_exit_contract.py），出口链 focused 回归 49 过。

- 2026-06-12: 一日基础完善四批（PLAN drifting-marinating-truffle，对照组=长期助手 为主）。
  - 批1 底座：`common/safe_id.py`（4 处实现归一）、`common/path_normalize.py`（7 种
    模式归一）、`json_io.write_text_file_atomic`（records.py 迁移）、吞异常处
    观测补丁（rg 降级/索引损坏/thread 绑定/unlink 二次异常）。
  - 批2 召回+skill 树：lesson 匹配重写为路由索引 trigger_keywords（中文词）∪
    stem 兜底的并集（旧算法要求英文文件名出现在中文 prompt 里，永远零命中）；
    skill 目录升级 `skills/builtin/<category>/<name>/`，SkillCard +category/platforms,
    递归扫描+路径推导类目；`render_category_index` 类目索引常驻（与 skill 总数
    解耦）；`skill_search` 模型工具（search-first 冷路，千级 prompt 零索引成本）；
    主 run prompt 注入=类目索引+命中卡（limit 2）。
  - 批3 对照移植：`concurrency/retry.jittered_backoff`+`apply_retry_jitter`
    （provider 重试链配置阶梯叠加抖动防共振）；`contracts/provider_error_classifier`
    （9 类，typed 优先文本兜底，transient_resume 前置裁决扩展重试面）；
    registry_invoke 错误出口 JSON `{"error","hint"}` 化（复用 taxonomy 权威码
    TOOL_UNAVAILABLE/TOOL_ERROR）；`concurrency/interrupt.py` per-thread 协作中断
    （工具循环安全点轮询+cancel_subagents 对线程形态按登记名递旗，补齐"线程
    形态无停止手段"缺口）；preview 截断落换行处；turn 聚合预算 200K 字符
    （round 收尾裁最大段，兜"单个不大累计巨大"）。
  - 批4 性能：`json_io.read_text_lines_cached`（mtime+size 守门行缓存,协作台账+
    产物注册表两热点接入）。
  - 已验证：focused 钉子全过（分类器 10/中断 4/turn 预算 3/缓存 1 等）、
    code-size strict 0-0-0、pyramid/offline-matrix gate 过、全量回归对照基线。

- 2026-06-12: 来源比例观测 + 启动时孤儿进程检测落地（backlog Active 清零）。
  ①来源比例观测（R8b 隐蔽编造实锤）：`delivery_closeout/source_volume.py`——
  closeout 报告新增 `source_volume_observation`（网络成功调用数〔按
  ToolSpec.category=="web" 结构化判定零白名单〕/交付文件数与字节/声明 min_count
  总数并排），挂 uncontracted+contract 双路径。**设计裁决：只做纯观测零 finding**
  ——"数据类产物 vs 分析类产物"是机器判不了的内容语义，任何可疑阈值必误伤
  零网络的本地分析任务（r9a 形态）；比例是否可疑由把关者结合任务性质判断，
  这也忠于"机器只摆客观事实"铁律。钉子 test_source_volume_observation.py 4 条。
  ②启动时孤儿进程检测（异常崩溃兜底，孤儿回收第二期）：startup_recovery 新增
  `_detect_orphan_processes`——扫任务落盘 background_start.pid，孤儿三条件
  （任务终态+进程活+cmdline 含本系统特征〔通道运行时 同款身份验证防 pid 复用
  误报〕）全满足才报告进 ActiveWorkSummary 与启动文案；**observability 先行
  绝不自动 kill**。钉子 test_startup_orphan_detection.py 5 条（含防误杀三连）。

- 2026-06-12: 交付写权限墙取证收口 + 两项底座修复（R8a 实锤：17 次交付写被系统
  WRITE_FORBIDDEN，含同子代理同文件"先拒后成"〔capreq grant 救场〕）。取证全程：
  A1 账本证实系统真拒非幻觉；canonical 终态重放（含清空 grant）一律 ALLOWED——
  瞬态拒因不可复现，暴露"边界决策不留痕"的可观测性根缺口；可控对照实验实锤独立
  通用缺陷：`delivery_root` 只认环境字段（attrs.run_workspace/task_workspace_dir/
  output_dir），创建链未透传时为空 → **子代理写自己 output_files 声明的位置被
  自家边界拦**（声明驱动语义断裂）。修复两件（全部底座通用，零任务专项）：
  ①`output_alignment.declared_output_write_roots`——声明产物父目录过围栏
  （task_workspace_dir/task_dir/manager workspace 根，与 capability grant
  `_safe_grant_roots` 同款基准）后直接并入写边界；围栏外声明不自动授权（防自我
  扩权，仍走 capreq）。接线 `runner_context_service._build_write_boundary`。
  ②A1 失败账本条目新增 `message`（系统拒绝原文截断 240 字）——决策时刻的拒因
  （边界/锁/危险目录）随账本留痕，根治"事后只能终态重放考古"。钉子：
  output_alignment 增 2（环境字段缺失下声明位置可写 + 围栏外不授权）、
  failure_ledger 增 1（原文留痕+截断）、既有全字典断言按新契约更新 2。

- 2026-06-12: 接力目录复用缺陷修复（R8 接力实锤）+ 语义裁决。
  `run_workspace._workspace_matches_request` 此前只按 request_id/run_id/task_id
  三个机器 ID 匹配——同 prompt 的新 run 三 ID 全新永不命中，被迫开 `-run-<ns>`
  新目录，接力变重做（原 progress/产物/expected_outputs 声明全失效）。修复=补
  prompt_fingerprint 匹配（逐字同 prompt 复用同目录接续；fingerprint 早已随
  identity payload 落盘，旧目录天然可比）。**语义裁决**：旧测试
  `test_same_prompt_new_run_gets_separate_task_workspace` 钉的是实现缺陷而非设计
  意图——配置注释明确承诺"同一 prompt 会复用同一个任务目录，并用
  work/timeline.jsonl 记录多次 run"（timeline 的存在本身就是多 run 设计），
  RUN_UNFINISHED_EXIT 的 resume 指引"再次对同一任务发起 run"亦依赖复用语义；
  该测试按新语义改写为 `..._reuses_task_workspace_with_timeline`（1 目录/
  timeline 2 行/身份随最新 run）。钉子另入 test_run_task_workspace_writer
  （同 prompt 复用+异 prompt 不误复用）。

- 2026-06-12: 跨产品对照归因落地三件套 + P5-2 收口 + slug 缺陷修复。
  ①检索完备性教训播种：`home_memory_seeds` 新增 `research.md` lesson + 路由段
  （归因实锤：r7c 把 author 字段 6 篇当全集 vs 对照产品多渠道并行命中 OCR 2；
  假全集陷阱通用化为检索纪律，零站点专项）；artifacts.md 增数据诚实条款
  （r7b 插值凑数实锤——数量要求绝不构成编造理由），expected_outputs spec 同步
  引导。**工程裁决**：数据真假机器在通用层不可判（插值发生在有真实检索的会话
  内，过程指标无法区分），不造无效硬门，走教训+声明引导，把关靠外部核验。
  ②P5-2 教训触发条件全链路收口（原 Active 暂缓项）：MemoryRecord 新增开放
  `attributes` 扩展位（旧行兼容、空不写键；底层唯一落盘口 `add_record`）；
  write_memory_with_type 持久化 trigger_conditions；推送端
  `trigger_conditions_match` 结构化匹配提权（列表任一/min_ 阈值/标量相等，
  软提权不淘汰）；生产端=失败自省调参后自动写带条件教训（failure_type+
  min_attempts，"超时×3→调2x"形态闭环）。钉子 test_memory_trigger_conditions.py
  6 条。③slug 缺陷修复（R5c/R7c"失败统计"目录名实锤）：task_title 的
  `_PATH_RE` Unix 分支加负向后顾——斜杠前是字母数字/汉字时（"成功/失败统计"
  "A/B 测试"）是词内并列不是路径起点；真路径标题分支不回归。钉子入
  test_home_layout.py。

- 2026-06-11: R7 真实任务轮暴露的四个通用缺陷当轮修复（详见
  docs/audits/R7-three-tasks-20260611.md）。①CLI mark 抹 pid：background_start
  记录构造收敛到唯一权威 `process_control.build_background_start_record`
  （BackgroundStartUpdate 入参包，pid 保留契约），agent 侧 mark_background_start
  与 CLI 侧 mark_background_launch 共用（r7a 实锤：CLI 手写 dict 覆盖抹掉 pid →
  孤儿回收 terminated_processes=[]）。②合同空壳压制真实产物：closeout 的
  `_no_required_artifact_response` 改为"有当前产物即回落 uncontracted 验收链"
  （r7a 实锤：空壳合同把 15 个真实 md 打回"请先写出交付物"）；死分支
  `_coverage_contract_present` 删除。③run 单次模式环境事实：
  `_workspace_prompt_section` 对 source=cli_run 注入"单次运行请示无人应答、
  不要以提问收尾"软约束（r7c 实锤：模型零产物"请指示如何继续"退出；gateway
  有 guidance 渠道、chat 多轮，不注入）。④compact 后产物候选失明（最重）：
  `_current_run_task_output_artifacts` 增加交付目录文件系统扫描兜底（只扫
  task_output scope，绝不反向扫 prompt 提取的 user_requested 目录；每文件照常
  过 validate_artifact；上限 200），出口合同 `_has_final_closeout_candidate`
  contract 分支同步回落同一产物事实链（r7b 实锤：compact 后 24 个真实 xlsx 对
  closeout 完全不可见，落 delivery_contract_missing 兜底，expected_outputs
  对账门被短路）。钉子：test_exit_orphan_recovery +2（CLI pid 保留）、
  test_expected_outputs_gate +2（扫描兜底 24 文件全见+对账门出场/空壳合同回落）、
  test_run_task_workspace_writer +1（cli_run 注入 gateway 不注入）。

- 2026-06-11: 检索完备性软引导落地（R5b 实锤：web_search 系统失败 2 次即断言
  "数据根本不存在"口头放弃；R6c 同构）。`tool_guard/loop_hints.py` 新增
  `append_tool_failure_channel_hint`——同一工具 archive ok=false（A1 同源系统事实）
  累计达 `tool_failure_channel_hint_threshold`（主配置三同步，默认 2，0=关闭）即注入
  软提示：下"不存在/不可行"绝对结论前先枚举已试/未试渠道（指向 P5-1 不可行报告的
  tried_channels/untried_channels_known 字段），换渠道再验证。每工具幂等一次、纯软
  提示零拦截、零工具枚举（按 tool name 通用计数）、`__parse_error__` 不计。接线
  `_run_tool_round`（与 guardrail 拦截回显并排，主代理与 worker 子代理同链路生效）。
  钉子 test_tool_failure_channel_hint.py 5 条。R6c"成功但单一查询字段"形态属模型
  认知层，机制无法结构化判定，留给教训记忆（P5-2）。

- 2026-06-11: 产物类型/数量对账门落地（R6b/R6c 实锤：prompt 要求 24 周/每篇一个
  PDF，实交 1 个 md/0 个 PDF，closeout 只查"有产物"仍 ok=true）。纯声明驱动开放
  世界设计：task_progress 新增 `expected_outputs` 声明字段（{pattern, min_count,
  note}，pattern 是相对交付目录的 glob，扩展名天然携带类型，归一化/合并/坏条目
  丢弃见 normalize_expected_outputs）；新增
  `delivery_closeout/expected_outputs_gate.py`——closeout 时逐条 glob 交付区实存
  文件数 < min_count 即 `EXPECTED_OUTPUTS_MISSING`（medium，repair 非硬卡死），
  挂 contract（gates.attach_closeout_gates）与 uncontracted 两条路径；零声明零影响、
  零格式专项分支、绝不解析自然语言（模型负责把 prompt 产物要求翻译成结构化声明，
  spec parameter_details 引导）。钉子 test_expected_outputs_gate.py 9 条（类型错配/
  数量缺口/目录不算/持久化合并/坏条目/端到端打回+补齐放行）。

- 2026-06-11: 孤儿子代理回收落地（R6a 实锤：主代理 RUN_EXIT 后后台 dispatch 进程
  继续运行 21 分钟写占位符，详见 docs/modules/subagent/02-progress.md 同日条目）。
  根因：派工是 `Popen(start_new_session=True)` 独立进程（durable），pid 只进内存
  registry 未落盘，出口合同不覆盖子代理生命周期。对照 通道运行时/长期助手/会话运行时
  （三家显式 kill+SIGTERM→SIGKILL+pid 落盘）落地四件：①`subagents/process_control.py`
  进程治理原语（kill -0+zombie reap 活性探测、SIGTERM 组→宽限→SIGKILL 两阶段终止），
  cancel_subagents 收敛复用；②`mark_background_start` 落盘 `background_start.pid`
  （激活 cancel 已预留的终止路径）；③`tool_loop/exit_orphan_recovery.py` 出口回收
  （BFS 子树收 pid 去重终止、仅 RUNNING requeue 回 PENDING 保 resume 语义、
  orphan_recovery 留痕）；④出口合同接线（RUN_UNFINISHED_EXIT 前先回收、resume 块带
  报告、修正"子代理会停止"反事实文案）+ `unfinished_exit_passthrough` 覆盖工具轮数
  耗尽系统截停出口（R5a 形态）。配置三同步 `run_exit_orphan_recovery_enabled`
  （默认 true）。钉子 test_exit_orphan_recovery.py 15 条（真实进程）。

- 2026-06-11: 任务完成力底座五支柱八子项落地（R5/R6 真实任务实锤驱动，详见
  docs/design/PLAN-foundation-task-completion-20260611.md 与
  docs/audits/R5-three-tasks-20260611.md）。原 4 个 Active 项（口头放弃绕过交付门 /
  交付目标运行中途被锁 / 读边界过窄 / A3 引导时机晚）全部收口：
  - **出口合同**（新增 `tool_loop/final_exit_contract.py`）：模型想结束时若任务未
    收口（非终态子代理 / open capreq / 派过子代理 / 产物可验）强制走 closeout；
    失败则双闸（`run_repair_max_continuations` 预算 + 进展签名）内打回续修，闸断
    放行且带 `[RUN_UNFINISHED_EXIT]`+resume。修 R5b/R5c"口头放弃绕过门"。
  - **空交付门**（uncontracted）：派过子代理却零产物 → `UNCONTRACTED_EMPTY_DELIVERY`
    返工（non-terminal），含不可行报告 schema（tried/untried 倒逼探索完备性）。
  - **锁生命周期**（`output_alignment` + persistence.save）：剔除"锁住自己交付目标"
    的自锁矛盾 + 锁变更账本。修 R5a 中途加锁。
  - **读边界 grant 闭环**（runner_context_service）：grant 的 path_scope 并入
    allowed_read_roots。修 R5a 读边界过窄。
  - **引导前移**（kernel 树快照）：OPEN capreq 时 tool_contract 直接带
    recommended_tool+open_capability_request_ids。修 A3 引导时机晚。
  - **占位符明示**（result_artifact_evidence）：materialize 兜底产物带 placeholder
    标记 + 独立账本 + closeout 投影计数。
  - 钉子：test_final_exit_contract（10）+ test_subagent_lock_lifecycle（5）+
    test_subagent_tool_failure_ledger（13）+ test_memory_push_decision_points（9）等。
  - 自查：零强行终止硬门（无 raise/exit/kill）；唯一阻断（空交付门）守产物存在性
    客观事实、走 non-terminal 返工、出口双闸兜底放行，合 AGENTS.md 铁律。

- 2026-06-11: 失败自省 split 建议生产→消费链路打通。`split_suggestions` 唯一消费方
  `_apply_introspection_split`（dispatch/mixin）：开关 `subagent_failure_auto_split_enabled`
  （capability_config，默认 false）+ 深度上限 `subagent_failure_split_max_depth`（默认 2）
  下复用 split_task 真实拆分（子任务 PLANNING 先落盘、原任务 TAKEN_OVER 后落盘），
  跳过原因结构化进 `failure_introspection_data.split_skipped_reason`。吞异常修复：
  apply 段失败写 `failure_introspection_error`（runtime_error_report）并补落盘。
  附带：删除无人生产的 `split_goal` 影子拆分分支（保留 `max_tool_rounds`——消费链
  真实存在）；运行时读 capability 配置收敛到新公共入口 `capability_config_for_agent`
  （context_compactor 原私有实现去重）；救活 test_dispatch_mixin.py 里 4 个被错误
  缩进成嵌套 def 的死测试（收集数 1→23）。钉子：test_real_class_integration.py
  （开关开真实拆分落盘 / 默认关只记 skip）+ test_dispatch_mixin.py（留痕双钉子）。
- 2026-06-11: scoped lock 线程互斥缺陷按方案A定性收口（文档化进程级单例语义，
  非代码缺陷修复）。全仓调用点排查实锤：acquire/release 生产代码零运行时调用
  （daemon_control 仅公共 API 转口、supervisor 死 import 已删、CLI/scripts 零引用），
  gateway 多线程路径未拿它当临界区，无存量数据竞争；对照 长期助手 ProcessRegistry
  确认"进程身份锁与线程互斥锁语义分离"是成熟做法。落地：`scoped_locks.py` 补
  LLM/双层中文注释并写明"同进程线程重入=刷新心跳是契约、禁止当线程临界区"；
  docs/modules/gateway/04-structure.md 增加核心文件条目和规则；原 strict xfail 钉子
  改写为进程级语义钉子 `test_real_io_concurrency.py::
  test_scoped_lock_process_singleton_reentrant_threads_and_cross_process_mutex`
  （线程重入刷新 + 真实子进程互斥 + 非持有进程 release 保护 + release 后干净重持有）。
  显式不做：方案B（thread id 维度，会破坏 supervisor 重入刷新）；新增独立线程锁
  原语（无任何调用方，违反"主链路优先不造旁路"，线程互斥直接用 threading.Lock /
  参考 agent/io/jsonl.py 双层锁先例，需求出现时再抽象）。
- 2026-06-11: 多代理产物交付链路四子项全部落地（R4 GoAttack 根因链，详见
  docs/audits/R4-goattack-20260611.md；对照 通道运行时 worktree / 长期助手 脏页模式后
  按"子代理写隔离区、产物由机制送回"同构设计）。
  - 子项①路径对齐：新增 `subagents/services/output_alignment.py` 投影层——attributes
    里的 output_files/output_refs 保持"最终交付意图"不动（唯一权威），执行合同
    （context_bundle_contracts 的 task_packet/output_contract）把目标 refs 翻译成子代理
    自己 output_dir 下的可写落点，`output_delivery_map`（落点→意图位置）进 write_contract
    并渲染进 runner prompt；落点被 locked_files 盖住记结构化 OUTPUT_TARGET_LOCKED warning。
    钉子测试 test_subagent_output_alignment.py（13 项，含真实 write_boundary 端到端）。
  - 子项②capability 回路：新增模型工具 `resolve_capability_requests`（grant/deny 显式
    裁决；grant 落 path_scope+写工具并即时生效到写边界，目录围栏=任务工作区+主代理
    workspace，越界结构化拒绝；deny 走协议终态 CLOSED+denial_reason 审计）；提交端
    `capability_request` 记录后即向父级线程发 requires_main_agent 观察+wake
    （runner_completion_wake.notify_parent_on_capability_request），主代理不再失明。
    钉子测试 test_resolve_capability_requests_tool.py（6 项）。
  - 子项③声明对账：subagent_aggregation gate 新增 `SUBAGENTS_DECLARED_OUTPUTS_MISSING`
    ——DONE 子代理声明产物在声明位置缺失即 NEED_REPAIR（R4"声明 40 实交 1 仍
    ok=true"形态被拦）；同时修复 gate 把 runner 自己算成未完成子代理的自指拦截。
  - 子项④汇总搬运：runner result 写回时 `deliver_anchored_outputs_to_declared`
    按 delivery_map 把锚定落点真实产物搬到声明位置（在 summary 物化之前，真实产物
    优先），结果进 attributes["output_delivery_results"]（delivered/skipped_existing/
    source_missing/target_outside_workspace），缺口由子项③对账拦截。
  - 接续 closeout 端半成品：aggregation gate 的 open capability_request 拦截
    （SUBAGENTS_CAPABILITY_REQUESTS_OPEN）+ rework 推送保留并通过测试。

- 2026-06-11: compact 工程鲁棒性三件套全部落地（终端交互/终端应用 蓝本）。
  - microcompact：`agent_core/tool_context/microcompact.py` 渲染期回收窗口外旧工具
    结果正文（保留 read_artifact 锚点占位），prompt 拼装层接线；配置
    `tool_context_microcompact_keep_recent/min_chars`（0=关闭）。
  - thrash circuit breaker：已于 5ac01a70 提交。
  - 单轮 PTL retry：`agent_core/tool_context/ptl_retry.py` + `_tool_loop_service.
    _retry_after_provider_context_overflow`——provider 实报上下文超限（typed
    ProviderContextWindowError）时回收最老 20% 工具结果正文重拼重试（持久突变，
    lossy 语义同 终端交互 truncateHeadForPTLRetry），上限 `tool_context_ptl_retry_max`
    （默认 3，0=关闭），救不回落回原 compact/resume 路径；preflight 预测溢出不抢跑。
    钉子测试 test_tool_context_ptl_retry.py（6 项，含 fake backend 端到端）。
  - 另落地 lesson 陈旧提示（终端交互 memoryAge 蓝本）：召回 lesson 超
    `home_lesson_stale_caveat_days`（默认 7，0=关闭）未更新附加"记忆可能过期"提示。

- 2026-06-10: 产物验收的"运行时遇到未登记格式"能力补齐（先实证 长期助手/工具运行时/
  终端应用 三家都是"通用兜底降级 + 插件热加载"，会话运行时 偏严格拒绝；按三家做法补齐）。
  - 通用兜底打开器 `open_fallback`：未登记格式按"长相"产 GenericView（文本/zip 成员），
    让未知格式也能跑声明字段校验（required_strings/required_sections/min_size/required_files），
    格式无关、永不随格式增长；无声明则落第 0 层放行，不拒绝（守开放世界铁律）。
  - 打开器热加载：`~/.my-agent/openers/`（或 MY_AGENT_OPENERS_DIR）目录扫描，丢一个十几行
    打开器脚本即可支持新格式深度校验，不改主代码、不重启。坏脚本隔离加载（异常只记
    OPENER_LOAD_ERRORS、不打断主链路、好打开器不丢）；内置格式不可被插件覆盖。
  - 钉子测试 test_artifact_openers_fallback.py（6 项）：未知格式兜底/放行/zip成员/热加载/
    坏脚本隔离/内置不可覆盖。文档 docs/modules/contracts/artifact-openers.md。
  - 47 个格式验收测试仍逐位过；全量快速套件失败集是基线子集（零新增）；
    code-size strict 0/0、doc sync、offline matrix 全过。

- 2026-06-10: 产物格式验证器重构为"打开器注册表 + 通用检查器"两层（先看 终端应用/
  长期助手/会话运行时/工具运行时 实证：四家都是"打开⊥验证正交 + 薄注册表 + 格式专属校验单独层"，
  没有一家把格式专属逻辑塞进一个通用检查器）。
  - 新增 `artifact_openers.py`（第 1 层薄注册表）：`OPENERS = {格式: 打开器}`，每个打开器只
    把 bytes 变结构化视图或返回"打不开"finding（XLSX_INVALID/CSV_INVALID/PDF_INVALID_SIGNATURE/
    DOCX_INVALID/JSON_* 等逐字保留），不做内容校验。未登记格式没有打开器 → 落第 0 层
    （存在/非空/残桩，格式无关），守"开放世界禁止封闭枚举"。
  - 第 0 层（存在/非空/残桩）已存在；第 2 层结构校验改为吃打开器产出的视图，不再重复打开。
  - 删 `artifact_csv_acceptance.py`、`artifact_document_acceptance.py`：打开逻辑进 openers，
    csv header/min_rows、docx/txt/pdf quality 校验进 artifact_acceptance.py。
  - `artifact_xlsx_contract.py` 退化为纯 xlsx 解析模块（workbook_text/worksheet_tables/
    required_columns_with_blank_values，供打开器用）；死的 xlsx_contract_findings 块删除，
    xlsx 结构校验（TOO_FEW_SHEETS/MISSING_REQUIRED_COLUMNS/REQUIRED_COLUMN_EMPTY_VALUES）
    移入 acceptance 的 `_xlsx_structure_findings`，吃视图不重开。
  - 新增格式成本：简单格式≈OPENERS 注册一行（落通用校验）；带专属结构校验≈再加一薄函数。
  - static_site 不并：它委托 subagents/static_site/validator.py 做多文件跨文件 DOM/JS 校验，
    是子系统不是单文件格式打开器，按"职责清晰不硬并"保留。
  - 逐位不变：47 个格式验收测试全过，全量快速套件失败集是基线子集（零新增），
    code-size strict 0/0、doc sync、offline matrix、replay 全过。

- 2026-06-10: 第三批合并（严格按"为可维护性合并、不为减文件数合并"原则）。
  - contracts/：tool_protocol_v2_models→tool_protocol_v2、llm_activation_(models/fixtures/timeout_budget)→llm_activation_readiness、artifact_xlsx_reader→artifact_xlsx_contract。
    都是"数据模型/读取器拆分自唯一逻辑父文件"的 facade 形态，类型在前逻辑在后读起来更顺。
  - subagents/services/：idempotency_contract_identity + repair_contract_identity → contract_identity，
    去重 3 个逐字相同的私有 helper（_iter_packs/_string_tuple/_normalized_path），两个身份计算改名区分。
  - 清理阶段1遗留的空目录 compact_context_bundle/。
  - 合计 −6 个源文件，纯结构整理零行为变化。
  - 严格评估后**保留不合**（按原则该留）：lifecycle_runner_attempts/lifecycle_capability_records
    （两件不相关职责）、_memory_types（2 消费者共享类型）、control_plane_codec（序列化层）、
    web_markdown（HTML→MD 转换器）、filesystem_structured_read、home_runtime_compact_refs、
    registry_auth（并进 704 行 execution 更难读）、artifact_* 格式验证器族、offline_* 合约族。
    这些是命名自解释、职责单一的内聚文件，合进大文件降可读性。
  - 验证：编译、ruff、focused tests、doc sync、offline matrix、code-size strict 0/0；
    全量快速套件失败集是基线子集（仅 2 个基线既有失败，零新增）。

- 2026-06-10: 阶段5 旧兼容审计与收尾（逐项核对写入方后处置，未盲删）。
  - 已删：home_layout 8 个旧根 memory 字段（memory_dir/daily/raw/hooks/lessons/routing/
    routing_index_md/indexes），bootstrap 不再创建旧根目录；默认路由表与默认 lessons 播种
    迁到 owner 权威位置（原来播在被查询忽略的根索引=死配置，现在真实生效）；
    staged_checkpoint 的 `staging.source_ref` 死臂（全仓无写入方）。
  - 核对后判定非旧兼容、保留：task_progress 的 `id or title`（工具 schema 明确声明两字段）；
    content_recovery 的 source_tool/tool_name/tool（write-abort 与 tool-call 两种活协议形态）；
    collaboration updated_at→created_at（时间戳数据卫生）。
  - 显式推迟（有删除条件）：agent_work_dir/agent_run_workspace 双键（存量 compact 续接包
    在用旧键，删除条件=续接包数据迁移完成）；registry member rows 的 path→relative_path
    （删除条件=确认无旧 group 记录需要重验）；delivery doctor 的 output_mode 同义键与
    offline 合同 id/path 别名（删除条件=prompt 合同文档明确唯一键后）。

- 2026-06-10: 阶段3 gateway/chat 性能可观测 + 阶段4 coverage 补全。
  - gateway：inbox mtime 扫描门（空闲不再每 0.2s 全量 glob）、worker-0 恢复扫描节流
    （timeout/3）、heartbeat 新增 queue_ages 观测；jsonl 路径锁引用计数回收。
  - chat：`read_jsonl_tail_report` 尾部倒读（5000 行账本取 20 条实测 41x，逐位一致）。
  - coverage：新增 `directory_tree` 覆盖类型（min_read_ratio / max_candidates 合同可声明、
    候选截断显式暴露、修复提示带 missing_files）；shell 输出改中段截断（保头+保尾+
    省略标记+总行数），结论不再被截掉。
  - 核实后跳过：list_files 分页早已存在（offset/next_offset/page_window）；registry↔ledger
    打通已存在（metadata.coverage_items）；"artifact 声明覆盖源文件"不做——会成为模型
    自证通道，违反"模型输出不能自己证明自己"。
  - 推迟（待 R2 实测）：subagent tree 投影缓存、lane 化并发。

- 2026-06-10: 阶段2 字符串判断清零 + 阶段6 子代理参数统一。
  - `contracts/recovery.py`：5 组散落的错误码前缀规则（repairable/recovering/hard_stop/
    category/recommended_action）收敛为单一 `CodePolicy` 注册表（精确码 > 最长家族前缀 >
    fail-closed），187 码 × 13 状态等价校验 0 差异；finding 显式声明的 recommended_action/
    category（当前协议枚举值）优先于推导；信封全 blocked 时不再被门状态兜成 repair_required。
  - `contracts/state_machine.py`：can_dispatch/can_repair/can_closeout 对协议错误状态
    fail-closed（未知状态不再是"可修复的 BLOCKED"）。
  - 新钉子测试 `test_recovery_code_policy.py`：未知码 fail-closed、精确码优先、声明覆盖、
    classify_error 仅限自检模块、协作 raw_* 审计字段只写不读。
  - 阶段6：子代理 runner 复用主代理同一 agent 对象（合同测试钉死，禁自建 backend/config）；
    thought/plan 默认模板归一到 `runner/prompts.py` 单一权威；新增 capability 配置
    `subagent_compact_trigger_percent`（0=继承主代理，>0 仅作用于 task_local 回合）。
  - 评估后保留：`explicit_root_allowed_tools`（spawn 时增补）与 `allowed_tool_set`
    （runtime 集合化）属不同层职责，非重复实现；`recovery_mode_from_protocol_value`
    未知值→MANUAL_REVIEW 是正确的 fail-closed；collaboration `_unavailable_reason`
    比较的是协议常量。

- 2026-06-10: 第二批 facade/碎片合并（阶段1，详见 docs/modules/*/04-structure.md）。
  - `contracts/gates/` 打平：command/artifact/network/document/tool 五个子包并入单层模块
    （`command_policy.py`、`artifact_gate.py`、`artifact_provenance.py`、`network_safety.py`、
    `document_content.py`、`tool_*.py`），positions/address_projection/content_extractors 并入唯一消费者；
    `gates/__init__.py` 155 行转发枢纽清空，15 个调用方直连权威模块。
  - `subagents/services/` 三个单模块包打平为 `capability_service.py` / `runner_context_service.py` /
    `runner_result_service.py`；`services/__init__.py` 11 个 re-export 删除。
  - `delivery_closeout/`：三个 `*_repair.py` 并入 `repairs.py`；`recovery_models.py`+`config.py` 并入
    `models.py`；`source_checkpoint.py` 并入唯一消费者 `staging_recovery.py`。
  - `orchestration/`：create_target_roots→create_context、create_idempotency→create_constraints、
    create_items→create_payload、create_conversation→create_policy（8 文件→4）；顶层 init 枢纽清空。
  - `memory_archive/compact_context_bundle/` 包并入单模块，导入路径不变。
  - 模块级真循环清零：gates.delivery_quality↔staged_checkpoint（claims/source_refs 归位
    evidence_contract）、log_analysis models/contracts 尾部 re-export、parsing/hierarchy/services
    init 转发，共 6 处。
  - 已验证：focused pytest、compileall、doc sync、offline contract matrix、code-size strict 0 hard/0 high-risk。

- 2026-06-06: 删除第一批只转发/影子入口。
  - `delivery_contract_prompting_recovery_bool.py` 并入唯一调用方 `delivery_contract_prompting_staged.py`。
  - `coordinator_seed_tools.py` 并入 `orchestration/create_policy.py`。
  - `orchestration/runner_instruction.py` 并入 `orchestration/dispatch/tool.py`。
  - `tooling/filesystem_write.py` 删除，测试和调用改走 `tooling/filesystem.py` 主入口。
  - `cli/memory_commands.py` 删除；实际 Python 导入一直走 `cli/memory_commands/__init__.py`，该文件只是同名影子入口。
  - `conversation/store.py` 和 `collaboration/store.py` 删除，公开 Store 类放回真实实现文件。
  - 已验证：focused pytest、py_compile、doc sync 均通过。

1. `agent_py_agent/agent/subagents/manager.py`
   - 当前定位：子代理管理主入口，允许比以前更大。
   - 下一步只在职责明显分叉时拆；不要再拆出基础 manager 薄层。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_manager_board_class.py agent_py_agent/tests/test_subagent_coordinator_due_check.py -q`

2. `agent_py_agent/agent/gateway_parts/request_execution.py`
   - 当前定位：gateway 请求执行主链路。
   - 下一步优先排查慢响应和上下文膨胀；只有出现无关职责才拆。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_gateway_request_runtime_errors.py agent_py_agent/tests/test_gateway_chat_conversation_context.py -q`

3. `agent_py_agent/agent/agent_core/orchestration/dispatch/mixin.py`
   - 当前定位：主代理 dispatch/watch 入口。
   - 下一步保留一条清晰调用链，避免新增转发层或历史参数层。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_dispatch_mixin.py agent_py_agent/tests/test_orchestration_dispatch_subagents_tool.py -q`
