# my-agent 四个不足(全量真机复验后,证据+如何测试,零上下文)

> **【回填 2026-07-06·下一棒已修+真机复测通过】**:四条不足全部机制层修复,commit `26422c7e`(单测 24 新增+相关套件全绿+code-size strict 0+架构护栏 10/10+DOC_SYNC_PASS);已部署测试机(scp 12 文件+清 __pycache__+systemctl restart,结构探针确认新代码加载)。**真机复测(repro_prompt×2 新用户+校准监控 90 分钟,三任务并发)全部通过**:u-fx1/u-fx2 双双 **24/24 收口**(老基线停 8/24;u-fx2 五轮续派 54K 行全 credit,对照 u-fc2 的 18K 行只认 4/24);假需求两用户一致收敛"done+可查证据"零 pending;监控召回 66-67%→**76.3%**、误报 0/518、audit 涓流 110(战役 0)、drain 净增 +148。各节末尾有【已修】块;原始件与逐条验收见 `docs/tasks/evidence/campaign_shortcomings_retest/`(summary.txt/coverage_raw×2/observe_timeseries/fm4_funnel)。遗留(下一棒"月级值守+回扫追认"):冷启动漏与残余积压的硬清账、无窗长守退休保护、账本轮转、判据漂移自检。

> **给谁**:接手来修的开发代理　**日期**:2026-07-06　**环境**:测试机常驻网关 8420、MiniMax-M2.7、飞书多用户并行
> **背景**:上一棒修了对账如实度(打勾保 title = 瑕疵A,已验证修好;树归并对账 = 已验证 OK)。测试方随后跑了一轮全量(代码建站×2 + 深度分析×2 + 监控 90 分×2),**又暴露/确认 4 个不足**,本文逐条给现象/证据/日志/如何测试/修法方向。
> **总方针**:加底座、不加限制;通用、不做专项;守铁律(代码只用结构信号:计数/状态/id/产物存在;禁自然语言/关键词匹配);别破已验证 OK 的(打勾保 title、树归并对账、covers 对干净派工有效、A2 主功能种清单、反压、节奏兜底、数据全扫、盯守召回、P2 误报免疫、P3 占位闸)。
> **诚实说明(关于证据)**:测试方清理测试机前**只完整落盘了监控证据**(见 `docs/tasks/evidence/campaign_shortcomings/`);代码/派工两条的原始 coverage 日志已随清理删除,本文给**实测数字 + 复现法**,并复用上一棒已落盘的 `docs/tasks/evidence/bigbuild_reconcile/coverage_retest.txt`(838 行,含派工耐力那条的原始证据)。下一棒复现后请把新原始日志落盘。

---

## 不足 1 · 假需求处理不稳(瑕疵B 没真治)

**现象**:A2 按顿号字面种清单时,会把 prompt 结尾的约束指令("别用占位、别省略、别留 TODO、别写")种成假需求 req-21..24。上一棒的修法是"让模型看到 title 后把这类标 skipped"。但真机三种结果**不一致**:
- 开发方自测(u-fixtest1):标成了 **skipped**(符合预期)。
- 测试方 u-fc1:标成了 **done**(模型把"别用占位"当质量约束"满足了"→ done)。
- 测试方 u-fc2:全留 **pending**(没处理)。

**证据/日志**:
- 已落盘:`docs/tasks/evidence/bigbuild_reconcile/coverage_retest.txt`——搜 `别省略|别留|别写|别用占位`,开发方那轮是 skipped。
- 测试方数字(原始日志已清):u-fc1 四假需求 `status=done`;u-fc2 四假需求 `status=pending`;两者 `title=id 的项 0 个`(瑕疵A 已修,别混淆)。

**影响**:清单能不能"真做全"依赖模型自觉判断非功能项——时而 skipped/done/pending,**不稳定**。

**修法方向(权衡)**:根子还是 A2 在**种**这一步就把指令碎片种成了需求。①种清单时结构化只种"主功能枚举段"、丢弃末尾约束/指令段(段界靠结构标点如冒号+段落,不判词义);或 ②让完整度判据结构化认"不可覆盖项"(无 covers 通道、纯字面碎片)不阻塞收口。别写关键词表。

**如何测试**:发 `evidence/bigbuild_reconcile/repro_prompt.txt`(结尾含"别用占位…")给 2 用户 → 收口后读父 run coverage → 假需求项应**稳定地不阻塞"做全"**(种子层就不种,或统一 skipped),不再时 done 时 pending。

**【已修 · commit 26422c7e】**:种子层纯结构无法零误杀地区分约束段(上一棒已论证同构;"只种冒号前导段"会误杀真枚举——真实 prompt 里主功能串被括号腰斩后前导是`)`,黑名单破折号也会误杀"——A、B、C"式真枚举),故落点在**裁决出口的结构收敛**:
- **需求项 done 证据闸**(`task_progress.requirement_done_without_evidence` + 工具入口拒写):自动种的需求项(`coverage_kind=requirement_item`/`source_ref=auto:requirement*`)标 done 必须带 evidence(本次 update 或账本现存任一非空),否则整次 update 拒绝并教两条出口——真功能补证据标 done;非功能碎片标 skipped+reason(skipped 无需证据)。"别用占位"类约束**附不出产物证据** → 分布结构性收敛向 skipped;u-fc1 的"裸 done"形态从此写不进账。纯结构信号(标记/状态/证据非空),模型自立项与 items 不受影响,系统对账路(reconcile 直写)不经此闸。
- **话术统一**:种子 note/coverage 返工门指令/quality_hints 软提醒三处对齐"非功能碎片一律 skipped、别标 done"——返工门原文"标 done/skipped 并写明原因"混着教,正是裸 done 的话术根源之一。
- pending 形态由不足 3 的续推修复连带治(它是任务停摆的伴生,不是独立裁决问题)。
单测:`test_task_progress_coverage.py::TestRequirementDoneEvidenceGate`(4 例:检出/证据或 skipped 放行/现存证据算数+自立项不碰/工具整拒+两出口回归)。
**真机复测 ✅**:两用户 req-21..24 终态一致=done+可查证据、零 pending、零裸 done(u-fx2 绑进派工 covers 连带打勾;u-fx1 模型自勾被闸要求附证,给出验证性陈述"全部 103 个 Python 文件均为完整实现,无占位符"——真去验证了约束)。三态摇摆收敛为单一形态,两任务均到 24/24 收口;收敛点是"done+证据"而非预期的"统一 skipped",如实记。

---

## 不足 2 · 对账"少认"(covers 绑定稀疏,写一大片只认几项)

**现象**:大工程模型递归乱派几百个子代理,只给**少数**子代理绑 covers。树归并对账(已验证 OK)把绑了的归并回清单,但**没绑 covers 的大量已建模块归并不到** → 清单显示的 done 数**远少于实际做的**。

**证据/日志(实测数字,原始已清)**:
- u-fc2:真实代码 **18,270 行**(明显建了一大批模块、成品目录 `teamforge`),coverage 却只 **4/24 done**、20 项 open。
- u-fc1:真实代码 8,965 行,coverage 11/24。
- 对比·上一棒已落盘证据 `coverage_retest.txt`:u-fixtest2 也是这形态(8/24,单子代理落地)。
- **关键对比**:打勾的都是真的(有真子代理+产物、无假 credit);问题是**反向少认**——建了却没绑 covers 的项如实显示 open,但其实做了。

**影响**:账本"不撒谎但不完整"——从上轮"全不认(0/24)"改善成了"认得不全"。离"对账如实完美"还差这口:清单该反映真实完成度,现在偏低。

**修法方向**:covers 别只靠模型临场绑。①派工时结构化【预分】清单 open 项给正在派的子代理(种清单即关联,不靠模型想);或 ②树归并再进一步:子代理产物路径/声明的 scope 与 open 清单项结构化对上就 credit(不只认显式 covers)。守只增不减/无假 credit/solo 不动。

**如何测试**:发 `repro_prompt.txt` 给 2 用户大工程 → 收口读 coverage → **done 数应接近实际建的模块数**(不再写 18K 只认 4/24);打勾仍要有真产物证据(别为凑数假 credit)。

**【已修 · commit 26422c7e】**:走方向②的结构化落地——把"模型声明 covers"的机会从【派工时一锤子】扩成【全生命周期双通道】,零假 credit:
- **树归并第三道判据【后代账本自声明】**(`dispatch_coverage_reconcile._credit_descendant_ledger_claims`):DONE 非占位后代在**自己 task_progress 账本**里把与主清单同 id 的 coverage target/item 标 done 且 evidence 非空 → 收口按 id 等值归并回主清单(`source_ref=auto:descendant-ledger-reconcile`,evidence 带 `descendant-ledger:{run_id}`+后代证据)。语义判断仍由模型(子代理做完自己知道做了哪项),代码只对账 id——与 covers 绑定同信任级;skipped/无证据/RUNNING/占位后代一律不 credit;主账本仍单写者;solo 不动;只增不减。
- **子代理现场注入**(`requirement_coverage_seed.run_params_with_parent_coverage_context`,挂 runtime_mixin 种子后):树深处 run(params.task_id≠run_id,含子/孙代理与后台唤醒轮)开场注入主清单 open 项(id+标题,上限 24)+自声明指引(完成→自己账本同 id 标 done 附证据;续派→item 带 covers)。大工程乱派时子代理**知道清单有哪些项、完成后有处声明**——这就是"种清单即关联"的可落地形态(硬预分配无法零假 credit:代码不知道哪个子代理真做了哪项)。
单测:`test_dispatch_coverage_reconcile.py` 新增 7 例(功能名项自声明 credit/无证据不认/skipped 不认/items 记法也认/RUNNING 不认/孙代理声明归并/主项有未闭 checks 不动)+`test_requirement_coverage_seed.py` 新增 4 例(注入形态/唤醒轮也见/浅层与干净账不注/上限 24)。既有 39 例(covers/路径段/树归并/兄弟隔离/防环)全绿零回归。
**真机复测 ✅**:u-fx2 写 **54,324 行/332 源文件,coverage 24/24 全 credit**(对照老基线 u-fc2:18,270 行只认 4/24)——主清单注入+回执提醒让模型五轮派工全部精确绑 covers(6/12/11/5/1 项逐轮点名),零假 credit、零 title 丢失。第三道判据真机未走到(covers 全绑不需要兜底),7 单测护住待 covers 缺席形态自然触发;1 例绑错 id(covers=['frontend'])不生效不阻塞,由唤醒轮自做兜住。

---

## 不足 3 · 派工"叫回后不续"(大工程冲不到底的深层耐力残留)

**现象**(开发方自己也挖到并诚实标注,测试方确认):父代理宣称"派 N 个子代理"实际常只落地 1 个(create_subagents 驾驭方差);某个子代理完成后发出 subagent-finished 唤醒信号,被下一个唤醒轮消费——**但那轮只 inspect_agent_tree "看一眼"就本轮收口,既不续派、也不再登记新唤醒** → 唤醒队列走空、任务如实停在半截(如 8/24),再没有未来唤醒把它推下去。

**证据/日志**:已落盘 `docs/tasks/evidence/bigbuild_reconcile/coverage_retest.txt`(u-fixtest2 停在 8/24)+ 上一棒文档 `DEV_HANDOFF_bigbuild_forcing.md` 尾部【遗留观察】节有完整时刻链描述。

**影响**:这是"大工程冲不到底"的**根**——不是清单没打勾(那已修),是**打完勾没人推着继续做剩下的**。与不足 2 叠加:既少认、又不续。

**修法方向**:唤醒轮消费 subagent-finished 信号后,**若账本还有 open 的 coverage/items → 结构化续推(再派剩余项)或再登记一个唤醒**,而不是"观察完就收口"。判据用结构信号(open 项计数>0 且窗口未到期)。这正是"派完撒手→叫回→叫回后要接着干"链条的最后一环。

**如何测试**:发 `repro_prompt.txt` 大工程 → 观察:子代理完成的唤醒轮后,**还有 open 项时应继续派/登记唤醒**(不再看一眼就收口);最终 coverage 逐步逼近全做完,而不是停在 8/24。

**【已修 · commit 26422c7e】**:机制级死因坐实后三保底+一引导(判据全=主账本 `coverage.counts.targets_incomplete`,纯结构):
- **死因**:唤醒轮 inspect 后走 closeout `ok=True`(coverage 是软门拦不住)→ `retire_task_progress_policies_on_closeout` 把派工监督提醒**全体退休** → 唤醒链走空,任务停在半截。外加唤醒轮工具集**没有 create_subagents**(prompt 铁律原文"这轮没有派子代理的工具")——想续派也派不动。
- **退休守卫**(progress_policy_retirement):清单未对完账(open>0)时 ok=True 收口**不退休**循环提醒(与既有"盯守窗口未走完不退 watch policy"守卫同构);清单全闭后照常退。
- **工具集条件开路**(conversation/runtime):唤醒轮/定时轮在 open>0 时给含 create_subagents 的续推变体 profile(`subagent_integration_continue`/`scheduled_progress_continue`);open=0 或无清单时原集合一字不动——"整合轮派读取孙代理绕圈"的原防护只在没活可派时才该生效,不回归。
- **唤醒链补登**(`_ensure_open_coverage_wake_chain`):消费完 subagent-finished 后 open>0 且任务名下无 enabled policy → 机制层补登一条(kind 同派工监督;无进展退避 2^streak 封顶、清单全闭收口自动退休,生命周期全复用既有)。
- **prompt 续推分支**:整合轮加"全部终态但清单还有 open 项→先续推别收口(续派 covers 带清单项 id / 项少自己做)";定时轮加同款 2b 步;铁律段改为按账本状态描述工具配发。
"窗口未到期"的普适替代=policy 自带的无进展退避(2^streak 封顶 8×,永不停机),避免给无窗任务发明假窗口。
单测:`test_wait_tool_self_wake.py` 新增 6 例(lifecycle/scheduled 开路+清单干净不开路防绕圈回归+退休守卫开/关+补登含幂等与清账不登)。
**真机复测 ✅(根治实锤)**:u-fx2 复刻老停摆场景(初派仅 1 子代理)——完成后系统**连续四次续派**(agents 1→5),coverage 0→6→18→23→**24/24 收口**(老基线 u-fixtest2 停 8/24 再无未来轮);u-fx1 走另一分支:唤醒轮**自己动手**写后端跑测试一举清 9 项,15→24/24。两用户全程 enabled policy 不走空,无一次"看一眼就收口"。

---

## 不足 4 · 监控召回 ~70%(漏约四分之一到三分之一)

**现象**:90 分钟盯守窗口内真目标(真命中)总数 ≈ **685-753 个**(答案 key 窗口内新增;753 含窗口后继续 emit 的略虚高),两个用户各只报出 ~500 个 = **召回 ~67-74%**,各漏 ~180-250 个。

**证据/日志(已完整落盘)**:
- `docs/tasks/evidence/campaign_shortcomings/monitoring_recall_summary.txt`——u-fm1 引擎抬中 597、报出 506;u-fm2 抬中 559、报出 496;误报 5 / 0。
- `monitoring_timeseries.log`——32 段时间序列(funnelB 稳步爬到 506/496、interval 全程 120、bgclaim 活、90 分不冻)。
- `u-fm1_fd.txt`/`u-fm2_fd.txt`——两用户报出的真目标 EVT id 原件。

**漏在哪(两段)**:①引擎没抬上来(685→597/559,funnel A 结构预筛天花板漏 ~90-130 个);②抬了没报(597→506、559→496,漏 ~60-90 个,多半窗口末尾边界 + 判读)。**误报很低(5/0),是好消息(P2 免疫可能在帮忙)。**

**影响**:能抓大头(~70%)、误报低,但漏了约 1/4-1/3,对"别漏"的盯守场景还不够。

**修法方向**:这是老的 funnel A 召回天花板(audit 抽检+反馈学习本就是治它的,可能抬得还不够)。可查:抽检率是否被判读吞吐反压压得过狠、反馈学习飞轮是否够快抬同类。**注意别破已验证 OK 的低误报**(反压/免疫别动坏)。属较难的已知课题,优先级可低于 1-3。

**【已修 · commit 26422c7e】**:真机 timeseries 复盘坐实两处,窄修不碰误报路径:
- **抽检被反压钳死坐实**:`monitoring_timeseries.log` 全程 backlog 60-260 恒 >8 → `judge_headroom=max(0,8-backlog)` **恒 0** → audit 抽检整窗 0 条 → 反馈飞轮的盲区发现通道(治 funnel A 天花板的机制)整场没转。修 = **audit 涓流保底** `audit_floor_per_minute`(默认 1,与抽检分钟允额同滑窗共账、不放大总量;=0 恢复完全钳死):钳位下每分钟仍抬 1 条抽检,90 分钟 ≤90 条 ≈ 真机判读量 5%,不淹判力;抽检样本仍经模型逐条重判才报,误报路径零改动。
- **窗口末尾弃判坐实**:t+90/93min 时 u-fm1/u-fm2 仍剩 backlog 200/97 条已抬候选,窗口一到模型收工=整批弃判(按真目标密度折 ~10-20 个直接漏报)。修 = **窗口到期清账信号**:`window_complete=true 且 spool_backlog>0` → 载荷置顶 `drain_before_close_note`("判完这批积压再收工");顺手补齐 **spool 消费路(真机主路)一直缺失的 keep_watching note 挂载**(此前续蹲/清账信号只在 inline 路生效,`_render_spool_pull` 注释声称同契约实则没调)。
- 更深的 funnel A 结构预筛天花板(壓组漏"语义真、结构=常态"的目标)不动——靠涓流让飞轮转起来自然抬,不加抬升冒误报险。
单测:`test_watch_judge_backpressure.py`(涓流分钟额度往复+既有反压测试显式关 floor 保原语义)+`test_ingestion_value_minority.py`(清账信号触发/清零不触发/无窗不触发)。
**真机复测 ✅**:校准密度盯守 90 分钟(与两大工程并发)——召回 66-67%→**76.3%**(499/654),误报 **0/518**(红线保持);audit 涓流 **110 条**(战役 0,throttled 6752 如实记账);spec 机械层捕捉 650/654=**99.4%**(残 4 条=冷启动判据学成前流过);drain 窗口后净增 **+148 条**(未弃账),残余 155 条判读停=指引有效非硬保证。冷启动漏+残余积压 → 下一棒回扫追认(全量 WAL+判据更新回扫+收口末扫)。详 evidence/campaign_shortcomings_retest/fm4_funnel.txt。

---

## 优先级 & 复测总纲

**优先级**:不足 2 + 3(大工程"少认+不续",最影响"真做全",一对孪生根)→ 不足 1(假需求稳定化)→ 不足 4(监控召回,已知硬课题,优先级低)。

**复测总纲**(每条上方有专门【如何测试】):部署修复到测试机常驻网关(8420)→ 飞书 POST /ask 带 X-User-Id 发对应任务(大工程用 `evidence/bigbuild_reconcile/repro_prompt.txt`;监控发 5 源盯守任务)→ 收口(进程连续空闲≥5 分才算真完,pgrep 空闲会误报)→ 读 coverage/findings 对照验收 → **把新原始日志落盘 `docs/tasks/evidence/`**。

## 约束(逐条守)
加底座不加限制 / 通用非专项 / 守铁律(结构信号,禁 NL) / 别破已验证 OK 的(尤其打勾保 title、树归并对账、covers 分析路、A2 主功能、反压、节奏、数据、召回、P2 免疫)/ 过质量门 / 真机复测 + 原始日志落盘 / 做完在本文档回填。
