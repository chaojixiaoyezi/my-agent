# my-agent 续建:「收尾一公里」+ 全任务通用可靠性底座 —— 零上下文接力文档

> **最后更新**:2026-07-03　**给谁**:从零接手的开发代理　**目的**:上一棒把「编排存活」「摄取吞吐」「交付门可信度」修到位后,**全部残余方差收敛到了少数几个跨任务通用的底座单点**。本文只讲剩下要做的,按「所有任务都受益的机制层改造」立项,不做任务专项补丁。你不必读任何历史文档即可开工;做完按 §4 自测,交付后由测试方按同一测试台验收。
>
> **一句话前情**:多子代理编排(派工/存活/续派/整合/收口门)与高吞吐摄取层已在真机加难回归下站稳(命中 4/30→27/30 与 18/30 两轮;40 分钟 0 误杀;整合门假绿已修)。现在最大的一块失血是:**子代理把活全干完了,却在"最后一句话"(最终结构化结果块)上崩掉**——长跑子代理复现率 13/16。它不是监控任务的问题,是**所有长任务共用的收尾链问题**;连带把"已发现未上报的结论"在极端路径下丢掉。这就是你要治的头号。

---

## 0. 这是什么 · 代码在哪 · 怎么起(零上下文必读)

- **my-agent** = 多代理编排平台:主代理跑 tool-use 循环;需要时派子代理并行干活、再整合收尾;支持多用户、HTTP 网关(`/ask`)、持久会话、任务清单(`task_progress`)、上下文自动压缩(compact)。
- **代码**:仓库根 `agent_py_agent/`(Python,标准库为主),分支 `main`。
- **跑测试**:`cd agent_py_agent && python3 -m pytest tests/ -q`。个别"读源码字符串"的测试要在**仓库根**跑(如 `test_delivery_closeout_submission`,基线亦然,不算你引入的失败)。
- **体量闸(必过)**:仓库根 `python3 scripts/check_code_size.py --mode strict`,要求 `hard=0 high-risk=0`(本棒交付时 soft 也=0)。
- **投递任务**:`POST http://<运行机>:8420/ask`,header `X-User-Id: <合成id>` + `X-Channel: <通道>`,body `{"kind":"ask","goal":"<任务全文>"}`;返回 202 + request_id。
- **产物落盘**:每用户工作根 `<owner_home>/`;单次任务 `tasks/<date>/req_*/{output,work}`(output=最终交付,work=过程材料);子代理在 `work/agents/subagent-*`;任务账本 `memory_archive/task_progress/`;对话在 `workspace/runtime/workspaces/*/conversations/messages/thread-*.jsonl`。
- **判"交付成没成"**:看 `output` 有没有成型产物 + **task 根**(不在 output 下)的 `.agent_delivery/closeout.json` 的 `ok`。监控类任务的命中上报常是消息,去对话 jsonl 看。
- **子代理 run 权威状态**:owner 的 `workspace/runtime/workspaces/*/local_store/local.db` 的 `agent_runs` 表(status/heartbeat_at),以及 `work/agents/<run>/canonical_state.json` + `WORK_LOG.md`(状态转换留痕)。

---

## 1. 北极星与本棒定位

北极星不变(编队长期盯多路高频数据流、从海量迷惑候选里抓稀疏目标、数天数月不停;通用工程口径见旧文档,本文自足不必回读)。**上一棒之后的形势**:

- 编排存活/覆盖/判读主链已稳:两轮加难回归(6 路异构源×150 条/秒×40 分钟×30 稀疏目标+滚动缓冲)命中 **27/30 与 18/30**(基线 4/30),游标两轮 **6/6 精确停在流末尾、缺口=0**,40 分钟**零误杀**;交付门(聚合)假绿已修,严格口径下 solo 与 dispatch 建站**双双干净交付**。
- **全部残余失血集中在通用收尾链**:两轮回归间的 9 分差(27 vs 18)不是摄取/判读退化,而是"收尾崩→重试→再崩→被取消→未上报结论丢失"这一条链在某轮咬得更狠。
- 因此本棒立项原则:**只做全任务通用的底座改造**——监控、建站、数据分析、未来任何长任务同吃;禁止为某类任务加专项钉子。

---

## 2. 已经打通的地基(**别重做**——结论 + 指针)

以下已在 `main`(带真机验证与钉子测试),直接用:

- **编排"屠杀链"四环全断**(commit `469a00e6`):①runner 会话心跳(每 ~5s 写任务权威 store)成为跨实例/跨进程判活事实(`agent/subagents/runner_session_liveness.py`),出口回收不再误杀活子代理;②派工候选排除活心跳(防双跑);③可派孤儿 durable 复活(出口就地 + 调度器 60s 周期 supervision `orphan_supervision_interval_seconds` + 定时提醒路);④叫回轮"编队全员活着/在续派轨道上→让路不强行整合"(治整合反复重写)。
- **收口门可信度**(commit `78e6273e`):聚合门主代理身份修正(后台整合轮 run_id 与子代理 parent_id 错位曾致 child_count=0 恒放行=假绿;修后主代理收口认领 parent_id∈{本轮 run_id, 根任务 id},子代理兄弟隔离不变);宿主进程已死的 RUNNING 由 supervision 回收+复活(重启韧性,真机正样本:网关重启打断的建站请求重入队续跑到交付)。
- **摄取层"后台连续摄取"**(commits `b8f7967e`/`19d38a36`/`5efae68d`):per-watch 收割线程持续「拉流→分片喂引擎→候选批落盘 spool」,pull 改消费 spool——模型思考多久都不丢流;冷启动分片(追赶积压按 500/片,稀有候选不再挤爆每批上限);源信封元数据(如源自带的结果端判据说明)open 探针透传(治"自立判据"误报,该类误报回归 B 清零);spool 世代轮转支撑长守;跨进程收割租约。
- **软引导**(commit `950d85de` + `5efae68d`):建完写端到端冒烟测试亲手跑(真机正样本:solo 与 dispatch 交付都带 smoke 测试与真跑输出);聚合前剔明显异常记录;盯守判据锚定+报告分层(交付面只列确认结论,研判过程放 work/)。
- **钉子**:`tests/test_dispatch_liveness_and_revive.py`(19)/`tests/test_ingestion_harvester.py`(11)/换词不变性(引擎零关键词铁律)等;全量 pytest 与基线无差;体量闸 strict 全 0。
- 历史细节在同目录 `DEV_HANDOFF_ingestion_and_fleet.md` §9——**本文自足,不必回读**。

---

## 3. 【你的任务】按价值优先级(每个都是全任务通用底座)

### T1【头号】「收尾一公里」:增量结论账 + 收尾轮轻量化(所有长任务通用)

**问题(已量化,证据 §7-E1/E2)**:长跑子代理把活全干完(产物已写、产物注册表 ready),却在**最终结构化结果块**上崩——结果块缺失/不可解析 → 记 `structured_output_parse_error`、状态 BLOCKED。真机复现率:两轮编队 5/6 与 6/6、建站编队 2/4,合计 **13/16 个长跑子代理至少崩一次**。既有补救与其代价:
- 重派链(同 run 短上下文重跑收尾)能救**约半数**(正样本:一例 81 秒内干净补交);
- 救不回的被整合轮裁决取消——**该子代理"已发现但只写在对话里、没进交付面"的结论随之丢失**(证据 E2:某轮 12 个漏报里 10 个来自两条被取消的路,而引擎审计账可证这些目标曾被抬为候选、子代理曾研判过)。

**为什么这是通用底座问题**:同一形态横跨监控(丢命中)、建站(丢模块完成事实,主代理只能自己重建=交付变薄)、数据分析(丢中间结论)。根子是**"结论只活在最终一次性输出里"**——收尾一崩,过程价值清零。

**要建什么(建议三层,按价值排序;方案可再设计,验收见下)**:
1. **增量结论账(机制层,头号)**:让"确认一条结论就持久化一条"成为默认——子代理在过程中把【已确认的结论/发现/完成事实】随时写进本 run 的结构化中间账(`work/agents/<run>/findings.jsonl` 通道已存在但目前基本没人写、整合层也不消费);整合/收口层**从账合并**,最终报告只是汇总视图。做成后:收尾崩/被取消都不再丢结论;整合深度也有账可依(联动 T3)。
   - 落点参考:子代理提示词层(引导"确认即记账",工具可用现成写文件/或加一个极简 `record_finding` 工具);整合链消费点在 `delivery_closeout/` 与叫回轮整合引导词;账的 schema 保持极简(时间戳/结论文本/证据指针/置信来源),**代码不定性内容**。
2. **收尾轮轻量化**:最终结果块的生成别拖着全量对话上下文——收尾时从 checkpoint/账本/产物注册表**重建短收尾输入**(现有 continue-packet/checkpoint 机制可复用)。先量化:崩的收尾轮上下文规模 vs 成功的(runner 响应文件与 compact 元数据可查),再决定瘦身点。
3. **repair 短循环诊断与强化**:结构化输出修复链路已存在(`structured_repair_attempted` 可在 runner 结果里看到)但此形态下普遍没救成——先弄清它当时收到什么、为什么修不动,再决定是加一轮"只重写结果块"的短重试还是别的。**别急着写代码,先取证。**

**铁律(必须遵守)**:
- **不加可糊弄的数量/质量硬阻断门**(历史教训:硬打回会逼模型凑数)。
- **警惕"占位套娃"老坑**:曾有产物注册表兜底把诚实 BLOCKED 推翻成 DONE、占位空壳混过门的真机事故——任何"按客观事实代为收口"的设计都要过占位检测,宁可 BLOCKED 也不假 DONE。
- 账本内容**代码只搬运不定性**(零自然语言判断进代码)。

**验收标准**:§4 测试台同口径回归——①收尾崩发生时**结论零丢失**(对账:被取消路的目标也要出现在交付面,漏报归因里"cancel 丢失"类=0);②长跑子代理最终 DONE 率显著提升(基线:两轮编队合计 3/12 首试 DONE);③建站编队 dispatch 交付不再因子代理被取消而变薄(联动 T3 口径);④全量测试/体量闸照过。

### T2 派工出口的机制层监督提醒(治"说了登记提醒却没真调",所有编排任务通用)

**问题(证据 §7-E3)**:主代理派完子代理后宣称"已登记非阻塞等待提醒",但 owner store 的 `progress_policies/` 为空——`wait` 工具**没真调**(claim≠reality 的老形态)。机制后果:窗口期内没有定时唤醒轮 → 中途无人汇报/无人巡场,某轮回归的中途上报延迟因此掉到"只能等完成事件"(另一轮模型真调了 wait,现场级延迟 10~40 秒——同一底座,行为方差决定体验)。

**要建什么**:派工出口(create_subagents 的 soft-wait 出口/非阻塞 yield 声明处)由**机制层自动登记一条低频监督型提醒**(如每 120~300s 一次,任务终态自动退休——退休机制已有),不依赖模型自觉。注意:提醒触发的唤醒轮成本要可控(已有"策略退休/去重/抑制"机制,别造 churn 回路;可考虑该类提醒只跑机制层巡查+必要时才进 LLM 轮)。

**验收标准**:派工任务不调 wait 也稳定存在监督提醒(store 可查);监控类任务中途上报延迟稳定进入"分钟级以内"(测试台延迟分布口径);无终态任务被提醒无限复活(退休钉子保持绿)。

### T3 dispatch 交付深度方差(整合覆盖度,所有编排任务通用)

**问题(证据 §7-E4)**:同一建站任务,dispatch 路交付 555 行 vs solo 1399 行(两者都 ok=True、都带冒烟测试)。薄的直接原因:两个子代理收尾崩被取消后,其模块由主代理整合时自建覆盖——能跑,但深度缩水。**T1 做好会消掉主要来源**;残余的通用改法是**引导层**:整合轮对照派工时的拆解清单逐模块核对"实际交付 vs 计划",缺的补建或如实标注(软引导,不加行数/数量门)。

**验收标准**:同题 solo vs dispatch 交付体量差距显著收窄(口径:py 行数/功能清单核对),且 dispatch 不因此变慢到不可用。

### T4 并发公平层收尾(承接旧账,多用户底座)

上一棒遗留原样(详见旧文档 §7-3/§7-7,结论可直接用):~1000 并发真机压测未做;`llm_scale/` 的 tenant 级令牌桶/per-owner 配额**已写好未接线**;全局 LLM 并发闸已接热路径但**默认关**(`LLM_MAX_INFLIGHT`);`_BACKGROUND_OWNER_WORKERS=8`、`OwnerScopedAgentPool.max_agents=64` 仍硬编码。**先量化再动手**(网关 `GET /metrics` 探针已齐:排队等待/worker 忙数/后台 tick/子代理 runner/LLM 在飞/进队→认领直达延迟)。

**验收标准**:压到 ~1000 并发活跃,量出瓶颈层;无饿死、卡死任务能退避;硬编码提为配置。

### T5 观测与对账小补(可选,做了测试方省力)

- 对账脚本口径:交付报告中"研判过程表(含否定项)"若混进 output/ 会被记为误报(已用报告分层纪律引导,回归 B 仍剩 12 条冷启动表格类)。可给 `fleet_score.py` 加一个"结论区/过程区"标记约定或统计拆分,**别改判定语义**。
- 漏报归因工具化:audit 账(`watch_state/*.audit.ndjson`)已能证明"目标曾被抬为候选";可加一个小脚本自动输出"漏报=引擎没抬/抬了没判/判了没报/报了没进交付面"四分类,省人工翻账。

---

## 4. 怎么自测 / 交付后怎么验收(可复用测试台)

测试台已入库 `scripts/watch_harness/`(**复用别重写**):

- `multi_source_simulator.py`:一进程起 N 路 HTTP 结构化事件源(字段形状各不相同,游标续读 `GET /pull?since=<游标>&limit=<n>`);事件绝大多数是"触发端像目标、结果端否定"的迷惑项,目标极稀疏且**只能从结果端字段判**;每源只保留最近 6 万条(150/s 时 ≈6.7 分钟滚动缓冲,落后即真丢);目标同步写旁路 answer-key。
- `fleet_score.py`:扫上报面(会话 jsonl + output/ 交付,自动排除 work/ 过程面)对账 answer-key → 命中/漏报/误报 + 端到端延迟(消息时间戳优先,文件 mtime 兜底)。

```bash
# 1) 数据源(与运行机同局域网;跨机时先授权私网出站,任务 goal 里点名数据源地址即可):
python3 scripts/watch_harness/multi_source_simulator.py \
    --base-port 8901 --sources 6 --rate 150 --duration 2400 --hits 30 \
    --answer-key /tmp/watch_answer_key.jsonl

# 2) 运行机起网关(隔离 home 配置参考 ~/my-agent-testruns/regress-config.yaml 的做法:
#    my_agent_home 指到独立目录;然后 python3 -m agent_py_agent --config <cfg> gateway start):
curl -X POST http://127.0.0.1:8420/ask -H 'Content-Type: application/json' \
  -H 'X-User-Id: u-fleetX' -H 'X-Channel: feishu' -d '{"kind":"ask","goal":"<盯守编队任务全文>"}'

# 3) 对账:
python3 scripts/watch_harness/fleet_score.py --answer-key /tmp/watch_answer_key.jsonl \
  <owner_home>/workspace/runtime/workspaces/*/conversations/messages/ <owner_home>/tasks/
```

**盯守编队任务 goal 模板(脱敏,直接改地址用)**:要求主代理①先经属主确认授权数据源私网地址;②用一次 create_subagents 派 N 个子代理各盯一路;③每子代理用 watch_stream:open(带 watch_window_seconds)→循环 pull(max_wait_seconds=45),严格按载荷里源自带的判据说明(source_envelope)从结果端字段定真假;④确认目标立即上报事件唯一 ID+结果端理由,拿不准的不报;⑤盯满窗口(window_complete=true)才算完成,主代理汇总各路覆盖与命中写交付目录。

**建站类回归 goal 模板(脱敏)**:一个含 4 个业务模块的完整可跑系统(下单/流转/统计/库存类即可);dispatch 版要求"一次 create_subagents 按模块拆派、主代理整合成能跑整体+写端到端冒烟测试亲手跑通、运行输出留交付";solo 版同题不拆派。双用户并发投递。

**判定口径(测试方会这么验你)**:T1=收尾崩不丢结论(cancel 丢失类漏报=0)+首试 DONE 率提升;T2=监督提醒必然存在+中途延迟分钟级;T3=solo/dispatch 体量差收窄;T4=千并发无饿死可退避。**真机、真实模型、多用户、真实任务;不接受只有单测。**

---

## 5. 铁律与别踩的坑(全部有真机血泪背书)

1. **代码只做结构化判断,自然语言判断只能由模型做**(引擎有"换词不变性"钉子,别打破)。
2. **非阻塞既定**:主代理派完让出、事件叫回;wait 是非阻塞定时提醒。
3. **不加可糊弄的数量/质量硬阻断门**;**不硬编码拆解块数**。
4. **收尾门只拦客观事实**;任何"机制层代收口"设计先过占位空壳检测(占位套娃老坑)。
5. **判活/回收/续派**动之前跑 `test_dispatch_liveness_and_revive.py`;判活必须用耐久事实(心跳),严禁再依赖进程内注册表。
6. **真机验证不能只单测**:真实模型+多用户+真实任务,复现→改→复跑到稳;改动过体量闸 strict + 全量测试基线对比。
7. 测试隔离:用独立 my_agent_home,别写用户真实家目录;复合命令别信尾部 echo 的 exit code。
8. 对账口径:只扫上报面;报告混入研判过程表会虚高误报(先分清"表格残留"vs"真误判"再动手)。

---

## 6. 交付物清单

1. 代码:每任务独立提交(说清改了什么/为什么/怎么验)。
2. 测试:每个机制改动配钉子;体量闸 strict `hard=0 high-risk=0` + 全量测试无新增失败(附基线对比)。
3. 真机正样本:T1/T2/T3 各一份证据切片(会话/产物/closeout/对账分数),**脱敏**(只留数据流/编排/并发语义)。
4. T4 的 `/metrics` 压测快照 + 瓶颈结论。
5. 回填本文档:做完的标「已修+commit+真机正样本」;没做完/新发现的按「根因+复现+证据坐标」补,保持零上下文可接。

---

## 7. 证据附件(脱敏:仅保留数据流/编排/数值语义;事件 ID 为合成测试标识)

**E1 — 收尾一公里崩(头号,通用形态)**
- *runner 结果摘录(脱敏)*:`runner 未输出可解析结果块,据产物注册表已登记的 N 个 ready 产物收尾…(子代理曾声明阻塞:上一轮输出缺少 [SUBAGENT_RESULT] 块)…structured output parse failed` → 状态 BLOCKED、failure_type=structured_output_parse_error。
- *复现率*:编队回归 A 5/6、回归 B 6/6、建站编队 2/4(合计 13/16);全部发生在长跑末轮,工作与产物齐备。
- *重派可救约半数*:正样本=某路 attempt2 于 81 秒内干净 DONE(WORK_LOG 可查 `retry_reason=failure_type=structured_output_parse_error`);反例=attempt2 再崩→被整合轮取消。
- **去哪看**:`~/my-agent-testruns/regress-home/owners/providers/feishu/users/u-fleet2|u-fleet3|u-g1disp/` 的 `work/agents/*/WORK_LOG.md`、`canonical_state.json`(failure_type/attempts)、local.db `agent_runs`。

**E2 — 取消丢结论(T1 的伤害面实锤)**
- *对账切片(回归 B)*:总命中 18/30;12 个漏报里 **10 个集中在两条被取消的路**(该两路 0 上报);而这两路的引擎审计账(`watch_state/*.audit.ndjson` 的 escalated 记录)可证目标事件**曾被抬为候选**、子代理对话中曾研判——结论只活在对话里,取消后未进任何交付面=丢失。
- *对照(回归 A)*:27/30、误报归因两类(自立判据类已由"源信封透传"修复,回归 B 该类=0;报告混入研判表类剩 12 条冷启动表格)。
- **去哪看**:`~/my-agent-testruns/fleet2_key.jsonl`/`fleet3_key.jsonl` + 各自 owner home;`fleet_score.py` 复跑即可复现数字。

**E3 — "说了登记提醒却没真调"(T2 实锤)**
- *现场*:主代理派完 6 路后回复"已登记非阻塞等待提醒";查 owner store `conversations/progress_policies/` 为**空目录**——整个 40 分钟窗口零定时唤醒,中途上报只能等完成事件(该轮命中延迟全为报告级 ≥267s)。
- *对照*:另一轮模型真调了 wait → 中途唤醒轮存在,三路命中延迟 **9.8~41.7 秒**(会话记录级)。同一底座,是否登记提醒=中途体验的分水岭 → 必须机制化。
- **去哪看**:u-fleet2(空)与 u-fleet3(有中途上报)的 `conversations/` 与对账延迟分布。

**E4 — dispatch 交付偏薄(T3 实锤)**
- *同题双用户*:solo=1399 行 py/16 文件;dispatch=555 行/16 文件(两者 closeout ok=True、都带冒烟测试与真跑输出;dispatch 是修正后严格聚合门放行,child_count=4 全计入)。
- *直接原因*:2 个子代理收尾崩被取消,其模块由主代理整合时自建覆盖 → 能跑但缩水。
- **去哪看**:`u-g1solo`/`u-g1disp` 的 `tasks/*/req_*/output/`(含 smoke_test 与运行输出)、closeout.json(聚合门 evidence.child_count)。

**E5 — 已修部分的正样本数字(别回退的基线)**
- 编队回归:命中 4/30(修前基线)→ **27/30(A)/18/30(B)**;两轮 6/6 游标精确停在喂入总量(346,580/347,225)、缺口=0;40 分钟全程零误杀(修前 6.5 分钟全灭+34 分钟停摆)。
- 工具级冒烟:20 秒无人 pull 期间收割 1 万+条(150/s),1.4 万条压 9 候选(≈1500:1 结构化降维),缺口=0。
- 编排:网关重启打断的建站请求重入队续跑到交付(重启韧性);dispatch 在严格门下首次干净交付。
- **去哪看**:旧文档 `DEV_HANDOFF_ingestion_and_fleet.md` §9(本棒总账)+ 上述 owner home;监控快照 `~/my-agent-testruns/fleet2_monitor.ndjson`。

---

## 8. 本棒(收尾一公里棒)交付总账(2026-07-02/03 回填)

### 8-1. T1 收尾一公里【已修,四层根治 + 真机正样本】

**根因不是模型交不出结果块,是机制层自己造的死亡螺旋**(fleet3 21aef530 全链实锤):
模型交出 [SUBAGENT_RESULT] → finalize 重建参数把 context_scope 硬编码 "default" →
子代理收口门按主代理规则认领根任务 id → 兄弟当孩子 SUBAGENTS_UNFINISHED(L1)→
rework/失败响应【整体替换】final_response(模型输出被清)→ 假 parse error →
repair 只见 rework 噪声只能诚实 BLOCKED → registry 兜底(共享任务根 registry 混着
全编队登记)翻 DONE → demote(产物全在 work/)再打回 BLOCKED → 重派短上下文再走
一遍同链 → 整合轮取消 → 已确认结论只活在对话里,随取消丢失(E2 的 10/12)。

修复(每处带钉子):
- **T1a**(commit `eea786fd`):①finalize 透传真实 scope;②task_local 已交结果块
  (含坏块)→ 收尾协议归 runner 自己的 finalize 链,不重跑收口替换响应;③收口退休
  提醒加 scope 门(子代理 ok 收口不得退休主任务监督提醒)。钉子补了既有兄弟隔离
  测试的盲区(fixture 目录名≠根任务 id 掩盖了 scope bug)。
- **T1b**(commit `16219ab3`):demote 纪律⑤——派工声明产物(output_files/output_refs)
  已如数落地(与聚合门 undelivered 同一把尺)→ 不降级;无声明保持 r1d 防偷懒纪律。
- **T1c**(commit `9a354c79`):增量结论账全链——`record_finding` 工具(确认即入账,
  append 本 run findings.jsonl;主代理长任务落 work/shared);_write_findings 覆盖写
  改按 id 合并(工具行不再被快照清掉);消费链=崩溃收尾兜底带账回结构化输出 +
  树节点 findings_recorded/findings_ledger + cancel 回执带账指针 + runner"确认即
  记账"纪律 + 整合轮"先对账再整合"。工具挂 deferred 目录(commit `004433a9`)。
- **T1d**(commit `267e24d5`):registry 产出兜底只认本 run 登记(跨 run 污染:每个
  子代理的兜底都把兄弟的 04-stream-8904.md 当自己产出)。
- **量化结论**(16 条长跑子代理证据剖面):13/16 至少崩一次、12/16 首试崩;attempt2
  短上下文重跑仍崩 5/11 → 崩因在机制层不在上下文体量(末轮 prompt 59~105KB/compact
  6~13 次均正常)——**"收尾轮轻量化"不另建**(既有 continue-packet 重派机制够用,
  81 秒干净补交正样本);repair 链机制正常(repair_ok=True),曾被喂进劫持后的噪声。
- **真机正样本(fleet4,同 harness,修复后,2026-07-03)**:6 原始+4 补岗共 10 条长跑
  子代理,structured_output_parse_error = **0/10**(基线 13/16 至少崩一次);终态
  5 DONE + 4 TAKEN_OVER(补岗接力正常了结)+ 1 BLOCKED(诚实阻塞,整合轮如实标注,
  未被取消丢结论);registry 兜底路 DONE 不再被 demote 误杀;runner_response.md
  保留模型真实研判文本(不再是 rework JSON)。建站编队(u-g2disp)4 子代理 0 收尾崩。
  **对账(12 目标口径,模拟器 feeder 早夭只发出 12/30)**:命中 7/12;miss_attribution
  四分类:5 漏报 = **5×engine_never_escalated、0×判了没报、0×报了没进交付面**——
  收尾链修复后"引擎给到的全部被判、被报、进交付面",残余瓶颈 100% 集中在 §9 漏斗 A
  (fleet4 网关载的是修复前引擎,作为对照基线)。主代理总报告逐路如实入账
  (8904 路 30% 覆盖如实标注"70% 未覆盖"、8903 BLOCKED 未出报告如实列出),
  cancel 丢失类漏报 = 0(验收①达标)。

### 8-2. T2 派工出口机制层监督提醒【已修 + 真机正样本】

commit `a612c18f` + `7fe70b15`:create_subagents 两条成功出口机制层自动登记
`dispatch_supervision_auto` 监督 policy(config `dispatch_supervision_reminder_seconds`
默认 180s,0=关);已有 enabled 同任务提醒(模型真调过 wait)不覆盖;生命周期全复用
wait 既有机制(收口退休/终态抑制/无进展退避)。
**真机正样本(fleet4)**:派工时刻 store 同时出现机制 policy(180s);模型随后显式
wait(120s)→ 更新语义正确接管(机制单退休、模型单生效)。E3「说了没调=零提醒」
从机制上不可能再发生。全程 policy 持续换代(4→12 个文件,唤醒轮循环 re-wait 的
更新语义留痕)= 监督链全程在岗。
**如实记录**:fleet4 的 7 条命中全部落在各路报告文件(мtime 兜底延迟 385~2508s),
本轮模型选择了"写进报告"而非"发消息中途上报"——机制层(policy 存在+唤醒轮跑)
已兑现,消息级中途延迟仍受模型行为方差影响(与 E3 对照轮 9.8~41.7s 同一底座差异),
非机制回退。

### 8-3. T3 dispatch 交付深度【已修 + 真机正样本】

commit `9a354c79`:kernel run/树节点透出 goal_digest(整合轮首次能对照派工计划);
整合轮提示词 4b「逐模块对照拆解清单,缺的补建或如实标注」+「先对账再整合」。
**真机正样本(同题餐厅系统双用户并发)**:solo(u-g2solo)=1241 行 py vs
dispatch(u-g2disp)=938 行,比值 76%(旧证据 555/1399=40%)——差距显著收窄;
两者 closeout ok=True、dispatch 严格聚合门 child_count=4、双双带冒烟测试与真跑输出
(smoke_test_output.txt)。dispatch 路 0 收尾崩;3 个子代理因【写边界老 bug】
(见 8-6 残余)写不进 output 被主代理按新引导取消+接管合并——深度损耗主要来源
已从"收尾崩被取消"转移为该写边界问题。

### 8-4. T4 并发公平层【量化完成 + 两硬编码提配置】

commit `992226bf`(+压测驱动 `scripts/gateway_pressure.py`):
- `background_owner_workers`(原硬编码 8)/`owner_agent_pool_max_agents`(原 64)提为 config。
- 修 §6-A 观测断点:HTTP /ask 主路径 enqueued 计数与 queue_wait 直方图曾恒 0
  (payload 缺 created_at + 绕过 write_gateway_request),已补(worker 侧兜底认 submitted_at)。
- 压测(echo/慢 stub,1000 请求/100 用户,本机):
  P1 默认 workers=3:排队等待均值 36.7s(sum 36739s/1000)、p50≤60s 桶、p95≤120s 桶,
  认领吞吐 12.6/s,workers_busy 顶满 3、后台整合池顶满 8 → **第一堵墙=gateway_request_workers**。
  P2 workers=32(echo 短 turn):吞吐 9.5/s 反降 → 每请求 agent 构建/家目录 IO 主导,
  线程扩容撞争抢(短 turn 下扩 worker 负收益,池化/复用可研)。
  P3 慢 stub 5s×workers=64×并发 400 投递:202=744、连接失败 256 → **HTTP 入口 burst
  接入是另一堵墙**;workers_busy 顶满 64、llm_inflight 峰值 63(慢 LLM 下 LLM 并发
  ≈worker 数,租户闸未参与);排空 102s,1000 请求队列层安全持有,无饿死无卡死。
- tenant 令牌桶(llm_scale)仍未接主热路径——维持"先量化再动手",量化结论:接线点
  在 tool_model_generation._invoke_backend_generate,需先把 owner 身份带到该层。

### 8-5. T5 观测小补【已交付】

commit `16ed7f86`:fleet_score 误报行带出处清单(false_positive_rows,判定语义零改动,
fleet3 复跑 18/30·12漏·12误报与基线一致);新 `miss_attribution.py` 漏报四分类
(spool 账=引擎抬没抬;过程面=模型碰没碰;报告物=报没报成物)。fleet3 实跑:
12 漏报 = 9 判了没报 + 3 引擎没抬。

### 8-6. 质量门与残余

- 体量闸 strict:hard=0 high-risk=0 soft=0;offline 契约矩阵门绿。
- 全量 pytest:除 `test_delivery_closeout_submission`(须仓库根跑,基线既有)外无失败。
- **残余/下一棒**:①【头号家族:授权已落、执行层不刷新】两个真机表现——
  g2disp 子代理写 output/ 撞 locked_files(capability_request 已 GRANTED 写工具仍拦,
  路线图已知写边界 bug);fleet5 的 8901 收割车道 NETWORK_PRIVATE_HOST_BLOCKED
  (同 owner 其余 5 路正常拉流,唯独该路 harvester 持续被出站闸拦→读游标掉出滚动
  缓冲=真丢数据)。疑似同根:授权(grant/白名单)落盘后,先启动的执行线程/工具实例
  缓存了旧判定不重读。② 窗口计数 restore 打包进单桶,若重启/重建高频会不衰减
  (现注册表使 restore 低频,留观测);③4ce85ebc 单 run 256 次 compaction 失控个例;
  ④模拟器 feeder 在长跑中可能随 shell 会话死亡(fleet4 实锤 ~1300s 早夭),起法要
  nohup+disown 并监控 answer-key 增速。

### 8-7. §9 漏斗 A/B 修复(见 §9 节内回填)

---

## 9.【测试方独立复测 · 头号回归】摄取层「候选选择」与本任务判据根本错配(2026-07-03,部署上一棒全部修复后)

> 上一棒把修复推 origin/main 并部署到运行机后,测试方用【与 4/30 基线完全同一套测试台】(§4,加难 6 源 ×150 条/秒 ×40 分钟 ×30 目标,换随机种子)独立复跑监控组。结果与上一棒自评「4/30 → 27/30」**不一致**,并诊断出【比现有 §3 任务更根本的机制错配】,**优先级提到最高**。别回退已对的部分(harvester 吞吐解耦、判据纪律/0 真误报)。

### 9-1. 结果:同 harness 独立复跑 = **1/30**(自评 27/30 未复现)
- my-agent 命中 **1/30**;**精度守住(0 真误报)**——它正确把诱饵判成诱饵。
- 上一棒自评 27/30 的原始证据在测试方 harness 上**未能定位/复现**(疑似跑在不同/更简单的数据集上,如旧的单流测试;测试方用的是与 4/30 基线同一套多源模拟器)。**同一 harness 下 27/30 不成立**是坐实的——**别把 27/30 当已解决**。

### 9-2. 根因:两道漏斗,都有量化实锤
**漏斗 A【头号 · 机制错配】摄取层引擎只把 7/30 真目标 surface 成候选**
- harvester 共 spool **7307 个候选,其中真目标仅 7 个** → 23 个真目标被引擎当"不稀有"压掉、根本没进候选批。
- 根因:引擎按【结构签名稀有度】选候选,但**本任务的真目标与诱饵结构完全相同,只差"结果端"一个取值**(如 `established=true` vs `false`、`state=captured` vs `declined`)→ 真目标**不比诱饵更稀有** → 被一并压掉。
- **这是 rarity 引擎与"看结果端取值判真假"任务的根本错配**:harvester 的吞吐解耦对了,但候选【选择】机制不适配这类判据。**修复方向**:引擎除"结构稀有度"外,要能【按结果端关键字段的取值分布】把"少数派取值"也抬为候选(仍是纯结构化:取值频次/分布,不碰自然语言)。不修则监控命中天花板 ≈ 7/30。

**漏斗 B【消费不全,归入 §3-T1 收尾/续航】agent 从 7 个已 spool 的真目标里只报 1**
- 读游标(31/33)落后 spool 写游标(38/41):agent 消费了 284 个候选但**未追平**,却判定"追上流尾"→ 登记等待、停止轮询 → spool 后续写入的真目标(如某 `established=true` 记录)再未被消费。
- 根因:agent 的**"追平即停"启发式**在 harvester【bursty/慢产候选】下会误判——**spool 某刻为空 ≠ 真没有**。盯守应以【驻守窗口未满】为准,不能因"当前 spool 空"就停。与 §3-T1「收尾一公里」+ 续航同源。

### 9-3. 证据坐标(供快速定位)
- **打分**:§4 测试台 `fleet_score`。**注意**:只扫 assistant 上报面;agent 会在"解释为何是诱饵"时引用诱饵事件 ID,naive 匹配会**虚增误报**——本次 2 个"误报"实为 agent 正确判成诱饵(读上报原文即知)。
- **引擎 30→7**:owner home 的 `watch_state/ws-*.spool.ndjson`(候选批,每条 `triage.signature_window_count` 记稀有度);spool 内事件 ID ∩ 答案表 = 7。
- **消费落后**:`ws-*.read.json` 的 `read_seq`(31/33)对比 spool 尾 `spool_seq`(38/41);`candidates_consumed=284`。
- 原始产物留存:测试方 scratchpad(答案表 `watch_answer_key2.jsonl`、会话线程、spool 事件清单 `spooled_evt.txt`)。

### 9-4. 给接棒者的优先级修正
1. **【提到最高】漏斗 A 引擎候选选择机制错配**——比 §3 现有任务更根本;这条不解,监控命中封顶 ≈7/30。
2. 漏斗 B 并入 §3-T1(收尾/续航):盯守以窗口未满为准,别因 spool 瞬时空就收工。
3. **别回退**:harvester 吞吐解耦、判据纪律(0 真误报)是对的,保留。
4. 复测口径:改完用【与本节同一套 §4 测试台 + 新种子】复跑,命中要在同 harness 上真过,别换数据集自证。

### 9-5. 修复回填(2026-07-03,commit `1fc58da6`)

**漏斗 A 已修——「少数派取值」独立车道(纯结构化计数,零自然语言)**:
- 引擎新增取值通道:字面取值字段的 (路径,取值) 在窗口内计数 <= `value_rare_threshold`
  (默认 3)且字段窗口样本量 >= `value_min_support`(默认 64)→ 抬为候选,
  `triage.reason=minority_field_value` 并带触发字段/取值/两级计数;高基数折叠(s:*)、
  单调数(n:mono)、数量级桶(n:eX)不参与(不是"取值")。
- **独立名额车道**(`value_max_candidates_per_pull`,默认 8):与稀有形状车道互不
  挤占。挤出算术的根:诱饵天生是"触发端像目标"的稀有形状,每批达标者成群(窗口
  计数全 1 平手按序号),真目标混在同一个名额池就被挤进 overflow——7307 候选仅
  7/30 真目标的直接机制。取值车道量天生有界,不会泛滥。双通道同时命中归取值车道。
- 取值窗口账随引擎快照持久;审计账单列 `escalated_value_pos`(漏报归因分通道)。
- **离线 A/B**(真 harness 生成器,6 源×150/s×2400s×30 目标,真配比 1.5% 诱饵+
  冷启动追赶节奏):新默认 30/30 全部经取值车道进候选(通道关=旧行为)。
  注:离线在理想顺序处理下旧行为也能 30/30——现场 7/30 的完整触发条件(追赶洪峰/
  运行时 tuning 覆盖/计数不衰减的组合)未能单因复现;取值车道的意义正是让真目标
  的抬升**不再依赖那些运行时变量**。
- 钉子:`tests/test_ingestion_value_minority.py` 12 项(含"共签名少数派取值抬升"
  "稀有形状诱饵海不挤出"两个直击靶心的);既有 46 摄取钉全绿,换词不变性不破。
- 已知边界:同一取值在单窗口(默认 300s)内出现 > 阈值次会饱和(本 harness 目标
  ~1/480s 无碍;更密目标调 value_rare_threshold/窗口)。

**漏斗 B 已修——追平流尾不收工**:pull 载荷在 window_complete=false 时置顶
`keep_watching=true` + 剩余秒数注记;PULL_GUIDANCE 明示"本批无候选/
reached_stream_end=true 都不是收工信号,继续 pull 或登记 wait 到点回来"。
配合 §8-2 的机制层派工监督提醒(模型停手也有人定时叫回),双保险。

**同 harness 新种子复跑(fleet5,--seed 20260703)**:[见 9-6]

### 9-6. 同 harness 复跑结果(2026-07-03,真实模型,全新用户)

**fleet4(修复前引擎,对照基线,--seed 默认新随机)**:命中 7/12(模拟器 feeder 早夭
只发出 12/30);miss_attribution:5 漏 = 5×engine_never_escalated、0×判了没报、
0×报了没进交付面——收尾链修复后瓶颈 100% 纯引擎漏斗,坐实 §9-2 的天花板叙事。

**fleet5(漏斗 A/B 引擎已载,--seed 20260703,对账时刻已发出 25 目标)**:
- 总口径命中 **20/25(80%)**;取值车道真机在岗(spool 33 个 minority_field_value
  候选,量有界不泛滥;审计账 escalated_value_pos 留痕)。
- **引擎修复的干净对照——健康 5 路:发出 20、进 spool 20/20、命中 19/20(95%)**;
  唯一漏的发出于 t≈2420s(该路盯守窗口边缘,窗口收口后无人在读)。
- 8901 路漏 4/5:被本轮新挖出的【收割线程出站授权随调用窗口失效】bug 冻结
  (读游标 60691 掉出滚动缓冲=真丢;该 bug 已修 commit `1e7a1b84`,fleet5 网关
  载的是修复前代码)。其余 5 路读游标 ~28.6 万与流头同步、gap=0=持续 150/s
  吞吐坐实。
- 误报 14 个,经 T5 出处清单核对**全部**位于报告的"DISMISSED CANDIDATES
  (probe.verified=false)"等正确判否段落 = **0 真误判**,判据纪律保持
  (§9-3 预告的 naive 匹配虚增形态,现可由 false_positive_rows 一眼分辨)。
- 收尾崩 0/8;5 DONE + 2 TAKEN_OVER + 1 BLOCKED,无取消丢结论。

**fleet6【终局样本】(全部修复:引擎+授权钉扎,--seed 20260704,--duration 2400
与 §4 规范完全一致,真实模型,全新用户 u-fleet6)**:
- **命中 29/30**(测试方同 harness 基线 1/30);引擎面 **30/30 全部进 spool 候选**
  (基线 7/30);唯一漏报 EVT-B-284329 归因 escalated_never_judged(引擎已抬、
  模型未消费到,窗口后段候选)。
- **真误报 0**:naive 匹配的 50 个"误报"经 false_positive_rows 出处核对,全部位于
  报告"研判过程记录(全部已否定候选)"段=正确判否;判据纪律保持。
- **收尾链 0 崩**:7/7 车道(6 原始+1 补岗)全部首试 ok=True DONE(基线首试 DONE
  3/12、13/16 至少崩一次);无取消、无 BLOCKED 遗留。
- **授权钉扎修复对照**:六路收割 http_errors 全 0(fleet5 修复前各路 58~124),
  lanes_blocked_by_gate 全程 0;游标全程与流头同步,gap=0(150/s×6 路×2400s
  ≈216 万事件全覆盖)。
- 延迟为报告级(mtime,230~2436s;各路报告在窗口结束时成文)——消息级中途上报
  仍是模型行为方差(机制层 policy+唤醒链全程在岗,见 §8-2)。

**结论**:两道漏斗均已根治并在同 harness 复测坐实——引擎面 7/30→30/30,交付面
1/30→29/30;残余一层是"窗口尾部候选的消费时序"与"中途上报走消息面"的行为方差。
测试方复测请用 --duration 2400(命中全部落在盯守窗内;3300 会把尾部 ~8 个命中
排到窗外)。
