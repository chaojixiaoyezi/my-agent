# my-agent 续建:高吞吐「摄取层」+ 盯守「编队持续性」—— 零上下文接力文档

> **最后更新**:2026-07-03　**给谁**:从零接手的开发代理　**目的**:上一棒把"访问链路 + 编排主链"打通了,但**旗舰能力(长时·高吞吐·多源数据流监控编队)最深的地基还没建**。本文把**剩下要做的**讲清楚,你不必读任何历史文档即可开工;做完按 §4 自测,交付后由测试方按同一测试台验收。
>
> **一句话前情**:一批 agent 编队长期盯多路高频数据流、从海量迷惑候选里实时抓出极少数"目标事件"上报——现在**子代理能拉到数据了,但 LLM 一秒消化不了那么多条,大半目标在"读取地平线"外被漏掉**。这就是你要解决的头号问题。

---

## 0. 这是什么 · 代码在哪 · 怎么起(零上下文必读)

- **my-agent** = 一个多代理编排平台:一个"主代理"跑 tool-use 循环完成任务;需要时派"子代理"并行干活、再自己整合收尾;支持多用户、HTTP 网关(`/ask`)、持久会话、任务清单(`task_progress`)、上下文自动压缩(compact)。
- **代码**:仓库根 `agent_py_agent/`(Python,标准库为主),分支 `main`。
- **跑测试**:`cd agent_py_agent && python3 -m pytest tests/ -q`。少数"读源码字符串"的测试要在**仓库根**跑(用相对路径读源码)。判"是不是我引入的新失败":拿基线 commit `git stash`/`checkout` 前后对比。
- **体量闸(必过)**:仓库根 `python3 scripts/check_code_size.py --mode strict`,要求 `hard=0 high-risk=0`。
- **投递任务**:`POST http://<host>:8420/ask`,header `X-User-Id: <合成id>` + `X-Channel: <通道>`,body `{"kind":"ask","goal":"<任务全文>"}`;返回 202 + request_id。
- **产物落盘**:每用户工作根 `<owner_home>/`;单次任务 `tasks/<date>/req_*/{output,work}`(**output=最终交付、work=过程材料**);子代理在 `work/agents/subagent-*`;任务账本 `memory_archive/task_progress/<run_id>/progress.json`;对话在 `conversations/messages/thread-*.jsonl`。
- **判"交付成没成"**:看 `output`(排 `work/`)有没有成型产物 + `output/.agent_delivery/closeout.json` 的 `ok`。**监控类任务的"命中上报"常是消息(不是文件)**——去对话 jsonl 里看,别只看 output 目录。

---

## 1. 要建成什么(北极星 · 通用工程口径)

**旗舰能力**:一批 agent 编队,长期驻守盯多路高频数据流,靠 LLM 全局判断从海量迷惑候选里实时抓出极少数"目标事件"并即时上报,能一盯数天数月不停。硬指标:

1. **多源异构**:同时 ≥5 路数据源接口,各自持续吐结构化事件;**每路字段格式(schema)各不相同**,要能适配(不能写死一套字段)。
2. **高吞吐**:每路起步 ~100 条/秒,目标演进到万级/秒。**LLM 读不过来是硬约束**(可查:*stream processing / event triage / backpressure / rate limiting / sampling*)。
3. **靠编队**:主代理**不亲自盯**,派子代理各盯一路;主代理**非阻塞统筹**(谁在盯、谁挂了重拉、命中汇总),自己不被任一路拖住(可查:*agent supervision / worker pool / heartbeat & respawn*)。
4. **超长驻守**:每路子代理**数天~数月不间断**,靠**自动上下文压缩 + 无限续跑**撑住,且**不被"防跑飞的固定次数硬顶"误杀**(可查:*long-running agent / context compaction*)。
5. **判据靠 LLM 全局判断**:每条候选要**同时看"触发/输入"端和"结果/响应"端**才能定性——绝大多数候选"看着像目标、其实结果证明没发生";目标事件极稀疏。**真假只能从"结果"端区分**(只看"输入"端必被迷惑)。
6. **实时上报 + 继续驻守**:发现目标**立刻**推送、带理由/证据;**报完继续盯不停**。端到端延迟(事件产生→用户收到)可测、要小。
7. **增量不重不漏**:用**游标/offset 续读**,别用固定尾窗口(会漏)。

---

## 2. 已经打通的地基(**别重做**——只给结论 + 指针)

以下已在 `main`(带真机验证),你**直接用**、不要重建:

- **跨机/内网数据源访问已通**:子代理要访问内网地址(如 `192.168.x.x:<port>` 数据源)时,主代理**派工前先调工具 `authorize_network_host`**(经属主一次确认)把该地址落白名单,子代理的 `web_fetch` 就放行。真机验证过:5 路子代理全部真拉到跨机数据、全程零拦截。相关:`agent/capability/network_authorization_tool.py`、`agent/user_space/network_grants.py`、`agent/agent_core/tool_runtime_ledger.py`。
- **派工/能力/收尾主链已修**:派子代理不再产出空目标、能力批复不再卡死、救不回的子代理能被取消、并行子代理收口不再被"兄弟"误拦(`delivery_closeout/subagent_aggregation.py:_is_own_child`)。
- **单路最小版盯守已修通**:主代理能进"读→等→增量重读→命中即报"的稳定循环(真机 7/8 命中、0 误报)。
- **并发可观测已建**:网关 `GET /metrics`(Prometheus 文本)带并发占用探针(排队等待/worker 忙数/后台 tick/子代理 runner/LLM 在飞),供你压测时量化瓶颈层。
- **完成判定链**(改前必懂):主循环 `agent/agent_core/_tool_loop_service.py`→每轮裁决 `tool_loop/completion.py`→出口合同 `tool_loop/final_exit_contract.py`(`_todo_persistence_decision`="熬劲"机制,把没做完的 task_progress 踹回继续;`_background_nonblocking_yield`=派了子代理就干净退出等叫回)→交付判定 `delivery_closeout/closeout.py`。**设计哲学(别踩)**:团队**刻意不加"可糊弄的数量/质量硬阻断门"**(历史教训:数量类硬打回会逼模型"凑数过门"),收尾门只拦**客观事实**。

> 想看这些是怎么建成的,历史细节在同目录 `DEV_HANDOFF_orchestration_reliability.md`——但**本文自足,你不必读它**。

---

## 3. 【你的任务】按优先级(每个自足 + 带验收标准)

### T1【头号 · 最难 · 最有价值】高吞吐「摄取层」:把数据流压成候选批再喂 LLM

**问题(已量化)**:数据流每路 ~100 条/秒,而 LLM 子代理逐条研判的消费力只有 **~5–25 条/秒**,差 **4–20 倍**。真机实测:20 分钟里有的盯守子代理只读到流的前 42–56 秒;目标事件散布在全程,**大半落在"读取地平线"之外根本没被看到** → 这是监控命中率上不去的**真正瓶颈**(不是判断力问题,是"喝不完")。

**要建什么**:在"原始数据流"和"LLM 研判"之间,新建一层**代码层的预聚合 / 初筛 / 降噪 / 背压**,把每秒上百条压成**每批几条的"候选批"**,LLM 只按批研判。业界成熟形态可查:*stream processing(Flink / Bytewax 单机版)、event triage、pre-filtering、deduplication、sliding-window aggregation、backpressure*。

**铁律(必须遵守,否则白做)**:**这一层的筛选只能用「结构化信号」**——去重键、字段存在/类型、滑动窗口计数、频次阈值、严重度/优先级字段、采样率——**严禁在代码里写任何自然语言判断 / 关键词匹配来定性"是不是目标事件"**。"是不是目标"这个判断**永远留给 LLM**;代码层只负责"把明显重复/低价值的压掉、把值得看的抬上来",做的是**降维不是定性**。(这是本项目一条硬规矩:代码只做结构化判断,自然语言判断只能由模型做。)

**设计要点**:
- **游标续读 + 背压**:按 offset 增量拉,消费跟不上时要有背压/采样策略(丢明确的噪声、保留高优先级),并**如实记录"这段时间覆盖到哪、丢了多少"**(不能假装全看了)。
- **候选批接口**:LLM 子代理拿到的应是"已压缩、带原始事件唯一 ID、带初筛理由"的候选批,而不是原始火力。
- **可配置**:压缩比、窗口、阈值要能调(起步 100/秒,以后万级/秒)。

**验收标准**:用 §4 测试台(每路 100/秒)跑 20 分钟盯守,**命中率显著 > 0**(上一棒是 0/21,卡的就是这一层);且盯守子代理的游标能跟到"接近流的当前末尾"(不再只停在前 1 分钟);漏报的要能从日志看出"是被结构化初筛合理压掉、还是真漏"。**不允许**用自然语言/关键词在代码里定性来"刷"命中。

---

### T2 盯守「编队持续性」:提前收工 / 死岗要补岗续盯

**问题**:真机复现两种掉链子——①盯守子代理**没盯满要求时长就自己 DONE**(采样几分钟就收工);②某路子代理**挂了/终态了,但盯守窗口还没到,主代理没有重新派人接着盯**。现在编排只有"取消"没有"补岗续盯"。

**要建什么**:主代理(非阻塞统筹)对盯守类编队要能**监督 + 补岗**:发现某路子代理终态但**盯守时长/覆盖未达标**→**重新派一个接着盯**(从上次游标续),直到用户要求的驻守窗口真正走完。可查:*supervisor / heartbeat / respawn / worker pool 的 self-healing*。

**验收标准**:测试台跑满 20 分钟,**全程每一路都持续有人在盯**(不出现"某路盯了 5 分钟就没人了")。会话/日志能看到"某路子代理终态→主代理补派续盯"的动作。

---

### T3 上一棒 3 个机制修复的「真机复验」(代码已入库,缺真机跑通)

上一棒修了 3 个机制 bug,只过了单测/本机集成,**缺真实模型 + 多用户 + 真实任务的真机回归**。请在真机复跑并确认守住:
1. **收口盲区**:成品被写到"交付区外"(如用户根目录)时,收尾要能发现并打回一次让它搬进交付目录(别再"交付区空→静默不交付"让用户啥也收不到)。复验:让主代理自己一条龙建一个中等系统,确认成品最终落在 `output/`、`closeout.ok=True`。
2. **账本跨唤醒轮延续**:派工立的账 + 唤醒轮的账要对得上(别出现"同任务 3 本账、主账 6 项永远挂 open")。复验:派子代理建站,跑完看 `memory_archive/task_progress/` 账本无悬挂 open 项。
3. **并行子代理收口不被"兄弟"误拦**:多子代理并行时,各自收口不该被别的子代理未完成拖住。复验:一次派 3+ 子代理并行,确认先做完的能正常收口、不被兄弟拦成 BLOCKED/CANCELLED。

**验收标准**:上述 3 条各出一个真机正样本(会话 + 产物 + closeout),失败的按"根因 + 复现"补进本文档。

---

### T4 并发「公平层」:~1000 用户同时活跃时别"自己饿死自己"

**背景**:当前形态下,多摊重活同时跑时,共享的有界调度 + 同一条模型调用管道会**自己把自己挤垮**(个别卡死/长跑任务还会周期性醒来空转占资源)。探针和 `/metrics` 已建(见 §2),**先量化再动手**。要你解决(自己调研业界做法、设计、真机验证):
- **先量化**:~1000 并发活跃时真正的瓶颈在哪层(调度槽位 / 网关请求处理 / 模型调用并发 / 磁盘 IO / 进程内锁)?每用户会**扇出**成多少个要调模型的单元(主代理 + 各子代理 + 各唤醒轮)?峰值放大到多少?——用 `/metrics` 量,别拍脑袋。
- **公平与隔离**:怎么做**每租户资源保底/配额 + 公平调度**,保证没有哪个用户被饿死?**卡死/无进展的任务怎么自动退避(backpressure)让出资源**?
- **长跑摊薄**:很多用户都挂长跑盯守时,每个都常驻占一个模型 agent 显然不现实——长跑盯守的资源占用怎么摊薄(什么时候才真花一次 LLM,什么时候只是廉价待着)?
- **模型调用层**:大量调用压到同一条模型 API,限流/排队/优先级/多 key fan-out/缓存怎么设计,不让"某一摊爆发"堵死别人;成本怎么随并发可控增长?

可查:*fair scheduling / weighted fair queuing / per-tenant quota / admission control / token-bucket rate limiting / backpressure / circuit breaker*。

**验收标准**:真机压到 ~1000 并发活跃(用 §4 测试台思路批量造负载),**量出瓶颈层 + 证明"无饿死 + 卡死任务能退避 + 公平"**。**先量化再改**(没量出瓶颈就改=瞎改)。

---

### T5 超长驻守「真机长跑」+ 子代理判读纪律复验

- **超长长跑**:盯守子代理数小时级不间断真机跑,确认**自动 compact + 无限续跑**撑得住、不被"防跑飞的固定次数硬顶"误杀(长跑盯守 run 应有"守望模式"活性判据代替固定次数顶)。
- **判读纪律复验**:上一棒给子代理加了提示词——"脚本只算初筛,每条候选须自判;脚本报 0 要抽样核实字段/类型/嵌套没漏再采信;盯满时长才算完成"。请真机确认子代理**不再"信脚本报 0 就收工"**、不再把"读了前几分钟"当"盯完了"。

**验收标准**:一次数小时级盯守真机跑,子代理靠 compact 续到底、覆盖如实、命中随 T1 摄取层就位而显著提升。

---

### T6 量化工具小补(收尾项)

- **打分器口径**:测试台的对账脚本要能清楚区分"目标命中上报 vs 迷惑项误报",并给出端到端延迟分布(样例口径见 §4)。
- **排队埋点**:并发探针补一个"进队→被认领"的直达延迟埋点,让"排队饿死 vs 认领后卡首轮"一眼可分。

---

## 4. 怎么自测 / 交付后怎么验收(可复用测试台)

测试台已入库 `scripts/watch_harness/`(**别重写,复用**):

- **`multi_source_simulator.py`**:一进程起 N 个 HTTP 数据源(默认 5 源、每源 ~100 条/秒),schema 各不相同、`GET /pull?since=<游标>&limit=<n>` 游标续读;**所有事件带唯一 ID(ID 不泄真假)**;绝大多数候选"触发端像目标、结果端否定"(迷惑),目标事件极稀疏且**只能从结果/响应端字段判**;目标同步写旁路 answer-key。
- **`fleet_score.py`**:扫会话 jsonl / 产物文本里出现的**事件 ID**,对账 answer-key → 命中 / 漏报 / 误报 + 端到端延迟。**注意**:只扫"上报面"(会话/交付),别扫"原始拉取的数据",否则误报虚高。

```bash
# 1) 数据源机(与运行机同局域网;先验运行机能 LAN 直连本机端口):
python3 scripts/watch_harness/multi_source_simulator.py \
    --base-port 8901 --sources 5 --rate 100 --duration 1260 --hits 22 \
    --answer-key /tmp/watch_answer_key.jsonl

# 2) 运行机投任务(要求主代理:先 authorize_network_host 授权 5 个内网源→派编队各盯一路→
#    结果端判目标→命中即上报事件 ID→盯满 20 分钟;数据源是内网地址,见 §2):
curl -X POST http://<host>:8420/ask -H 'Content-Type: application/json' \
  -H 'X-User-Id: <合成id>' -H 'X-Channel: <通道>' \
  -d '{"kind":"ask","goal":"<按上面要求写全>"}'

# 3) 跑完对账:
python3 scripts/watch_harness/fleet_score.py --answer-key /tmp/watch_answer_key.jsonl \
    <owner_home>/.../conversations/messages/ <owner_home>/tasks/
```

**判定口径(测试方会这么验你)**:命中率(抓真数/漏报/误报)、端到端延迟分布、每一路是否全程有人盯(T2)、盯守子代理游标是否跟到接近流末尾(T1)、~1000 并发下有无饿死 + 卡死任务能否退避(T4)。**真机、真实模型、多用户、真实任务**——不接受只有单测。

---

## 5. 铁律与别踩的坑

1. **代码只做结构化判断,自然语言判断只能由模型做**——尤其 T1 摄取层,代码层严禁用关键词/自然语言定性"是不是目标事件",只做结构化降维。
2. **非阻塞是既定原则**:主代理派完子代理要能干净让出、靠事件叫回,别改回"阻塞等子代理";`wait` 类是定时提醒/非阻塞,别改回阻塞。
3. **不硬编码"该拆几块"**:拆解粒度靠引导层软引导,别写死数量类硬门(会逼模型凑数)。
4. **收尾门只拦客观事实**:别加"可糊弄的数量/质量硬阻断门"(见 §2 设计哲学)。
5. **真机验证不能只单测**:每条都要真实模型 + 多用户 + 真实任务复现→改→复跑到稳定;改动过质量门(体量闸 strict `hard=0 high-risk=0` + 全量测试无新增失败,拿基线对比坐实)。
6. **增量续读用游标**,别用固定尾窗口(会漏两轮之间夹缝里的事件)。

---

## 6. 交付物清单(你做完给什么,方便测试方直接验收)

1. **代码**:各任务改动(建议每个任务独立提交,提交信息说清"改了什么/为什么/怎么验")。
2. **测试**:每个机制改动配钉子测试;体量闸 + 全量测试绿(附基线对比)。
3. **真机正样本**:T1/T2/T3/T5 各附一次真机跑的证据切片(会话/产物/closeout/对账分数),**脱敏**(只留数据流/编排/并发语义,别带任何领域敏感词)。
4. **量化数据**:T4 的 `/metrics` 压测快照 + 瓶颈层结论。
5. **回填本文档**:没做完的、或新发现的问题,按"根因 + 复现 + 证据坐标"补进本文档,保持零上下文可接。

> 交付后由测试方按 §4 测试台复跑验收;命中率 / 延迟 / 持续性 / 并发四条线达标即算能力一地基建成。

---

## 7. 本棒交付总账(2026-07-02,T1/T2/T4/T6 落地 + 真机验证 + T3/T5 部分 + findings 回填)

> 一句话:**头号问题 T1「读取地平线」已根治**——真机 5 路盯守编队 **命中 7/12、误报 0**(上一棒是 0/21),4/5 路游标全程跟到流末尾。摄取层严守「代码只做结构化降维、定性永远留给 LLM」铁律(有换词不变性钉子坐实)。T2 补岗、T4 并发闸、T6 打分器口径都已落地。剩两处真机新发现的深层坑(编队收口/接管派工)按「根因+复现+证据坐标」回填在 §7-7,是下一棒头号。

### 7-1. T1 高吞吐摄取层(已建成 + 真机达标)—— commit `8b324a14`

- **建了什么**:`agent/ingestion/` 一整层——事件压平(`flatten.py`)→ 字段画像(`field_profile.py`:基数/单调性/数量级分类,高基数粘性收敛)→ 签名(`signature.py`)→ 滑窗计数(`window_counter.py`)→ 稀有度分诊引擎(`engine.py`:窗口内签名计数 ≤ 阈值=候选,两阶段排序保「最稀有先上」)+ 游标拉取器(`puller.py`)+ owner 盘上状态/审计(`watch_state.py`)。工具 `watch_stream`(`watch_tool.py`,open/pull/status/close/list)。
- **接线**:`agent/core.py:_register_orchestration_tools` 注册;进 `CODING_SUBAGENT_TOOLS`(子代理默认可用)+ 唤醒轮 `_BACKGROUND_WORK_TOOLS`;runner 系统提示词 + 定时续跑提示词加「盯高吞吐游标源用 watch_stream」引导(`agent/agent_core/runner/prompts.py`、`agent/conversation/runtime.py`)。
- **铁律守住(有钉子)**:`tests/test_ingestion_harness_replay.py::test_value_renaming_invariance_proves_no_keyword_logic`——把全部字符串值双射改名(语义全毁),分诊决策按事件位置**完全一致**,证明本层零自然语言/关键词定性。
- **pull 是长轮询**:块内代码层持续「拉流→喂引擎」直到出候选或等待额度用完;游标始终追平流末尾,模型轮只在有东西可判时消耗。出站沿用 web_fetch 同款网络安全闸(`allowed_private_hosts` 每次热注入,owner 授权即时生效)。
- **真机正样本(脱敏)**:5 路异构源各 ~100/s、每路盯 480s。`~/my-agent-testruns/evidence/fleet_final_report.md`(交付报告,每命中带**结果端**判据如 `session.established=true, mfa=false`;迷惑项 `established=false` 全 suppressed 不报)+ `fleet_score.json`(命中 7/12 误报 0)。真机游标实测:4/5 路 cursor 到 52k~54k、200+ 次 pull、reached_end=true(全程跟到流末尾,不再停在前 1 分钟)。
  - 5 漏报归因(全可解释,非摄取层失效):2 条在 480s 窗口**之后**才产生(seq 55k/60k ≈ 550-590s);2 条(source D)在 §7-7 的死岗 8904 覆盖缺口里;仅 1 条(source E)是已覆盖路上的模型判读漏。
- **验收对标**:handoff §3「命中率显著 > 0」→ **7/12 ≫ 0** 达标;「游标跟到接近流末尾」→ 4/5 路达标;「漏报能从日志归因」→ 审计账 `watch_state/*.audit.ndjson` 逐 pull 记覆盖区间/候选/溢出/组计数。

### 7-2. T2 编队持续性:补岗续盯(已建 + 真机 respawn 实证 + 加固)—— commit `16c4560d`、`5a682383`

- **建了什么**:`agent/agent_core/orchestration/dispatch/watch_lane_sweep.py`——唤醒轮前扫描:盯守窗口未走完 × 岗上 run 已终态 → 建接管 run(幂等、链深封顶、观察流写 `watch_lane_respawned`)。挂进 `auto_capability_sweep`(子代理生命周期唤醒)+ 定时提醒唤醒路兜底。判据全结构化(窗口时长/时间戳/run status),零 NL。
- **真机实证**:5 路里 lane 8904 的子代理**提前收工**(窗口没到就终态),扫描检测到 → 建接管 run(watch_state `respawn_count=1`、`last_respawn_takeover_run_id` 落定),死岗 cursor 从 7420 被续到 27420。**这就是 §3 T2「某路终态→重新派人接着盯」的机制真机触发。**
- **加固(`5a682383`,真机暴露后修)**:①接管 run 建出后停在 PLANNING——唤醒轮 `_redispatch` 拉 PLANNING 不稳;改用 `create_subagents` 同款 `auto_start_tasks`(durable 后台派工)拉起。②`respawn_count` 被拉流方 `persist_state` 整体覆写抹掉——persist 前取盘上较大值(respawn 单调)。
- **钉子**:`tests/test_watch_lane_sweep.py`(6 用例:DONE 死岗建接管+记账、窗口走完/关闭/在岗/无人岗不动、takeover 链走最新死岗、幂等不重复、主代理 solo 不归管)。

### 7-3. T4 并发公平层:先量化 + 层4 全局闸接线(默认关)—— commit `07f1563e`

- **量化(先量化再动手,§3 明令)**:架构勘查 + 真机 `/metrics` 实测。1 个 5 路盯守用户扇出 **llm_inflight=5 / subagent_runners=4 / bg_ticks=2**(证据 `~/my-agent-testruns/evidence/metrics_fanout.txt` 峰值切片)。**最先饱和 3 层**:
  1. **网关 worker 池**:`gateway_request_workers=3` **共享单池、非 per-owner**(`cli/gateway_loops.py`)——3 槽服务 1000 用户,3 个长任务用户就把第 4~1000 个卡进 pending。
  2. **真实 LLM 调用层**:`tool_model_generation.py:_invoke_backend_generate` **零全局并发闸**——子代理每层 ×8、总 50,直冲 provider RPM/TPM → 429。唯一测得到却拦不住的层。
  3. **后台 tick 池**:`_BACKGROUND_OWNER_WORKERS=8` **硬编码共享**(`cli/gateway_loops.py`)——1000 活跃 owner 挤 8 worker。
  - **关键发现**:`agent/llm_scale/`(admission/令牌桶/per-租户预算/全局并发信号量/lane 公平/背压/断路器)**功能齐全但没接线**——公平层大部分是「把已写好的闸接到真实 LLM 路径」,不是从零写。
- **动手(层4,最高杠杆最低风险)**:`agent/llm_scale/hot_path.py` 把已测的 `ConcurrencyLimiter` 接进热路径,给「全局在飞 LLM 调用数」硬顶。**默认关**:env `LLM_MAX_INFLIGHT` 未设=`nullcontext` 零行为变化;设了才封顶,槽满等 `LLM_ADMISSION_WAIT_SECONDS` 抛 `ProviderTransientError`→tool 循环退避重试=背压(不闷等不崩)。钉子 `tests/test_llm_hot_path_admission.py` + 真机 driver 证默认零变化。
- **未做(§7-7 下一棒)**:tenant 级令牌桶/per-owner 配额接线、`_BACKGROUND_OWNER_WORKERS` 提 config、网关出队从纯 FIFO 换 per-owner 加权公平、**~1000 并发真压测**(本棒只到「量化 + 层4 闸」,没到全链 1000 压)。

### 7-4. T6 量化工具小补(已建成)—— commit `828b3261`、`65e664c6`

- **打分器口径**:`fleet_score.py` 只扫**上报面**(`output/` 交付 + 会话消息),排除 `work/` 过程材料——摄取层把初筛候选批(带原始事件 ID)落进 `work/blobs/tool_outputs`,旧脚本递归扫 .txt 把候选原料当「上报」→真机实测两路盯守就把**误报从 1 虚高到 89**;修复后 89→1(剩 1 是任务 prompt 里的示例占位 `EVT-X`)。钉子 `scripts/watch_harness/test_fleet_score_surface.py`(5 用例)。
- **排队直达埋点**:并发探针加 `agent_gateway_requests_enqueued_total`/`_claimed_total`——enqueued 涨而 claimed 不跟=worker 槽饿死(排队);claimed 跟上却无产出=认领后卡首轮。这就是 §6「排队饿死 vs 认领后卡首轮一眼可分」。埋点 `gateway_parts/io.py`(进队)+ `gateway_parts/queue_service.py`(认领)。

### 7-5. T3 上一棒 3 修复真机复验(部分:2 稳 / 1 受 §7-7 阻塞未完整取证)

- **③并行子代理收口不被兄弟误拦**:真机 5 子代理并行盯守,各自独立 pull/研判,先做完的没被兄弟拖成 BLOCKED——**稳**(子代理账本 5/5 全 `open=0`)。
- **②账本跨唤醒轮延续**:主任务账本 13 项、**仅 1 项 open**(那 1 项是 `integrate`「等所有子代理 window_complete 后整合」),子代理账本全 `open=0`——比上一棒描述的「同任务 3 本账、主账 6 项永远挂 open」分裂**已好一个量级**;那 1 项 open 是被 §7-7 的编队收口坑挂住,不是账本分裂本身。
- **①收口盲区**:成品 `final_report.md` **确实落在 `output/`**(不是交付区外),这条盲区没复现;但 `closeout.ok=false`——不是「成品写错地方」,而是 §7-7 的编队收口没跑完(integrate 步等不到子代理终态)。**需下一棒在编队收口修好后补一次干净 closeout 正样本。**

### 7-6. T5 判读纪律(已真机验证)+ 超长长跑(部分)

- **判读纪律**:真机 5 路 **误报 0** + 交付报告每命中带**结果端**判据、迷惑项(结果端否定)全不报——子代理**没有信脚本报 0 就收工**、没有把候选原料当命中乱报,逐条按判据自判。**这条 §3/§5 判读纪律真机达标。**
- **多分钟持续盯守**:4/5 路盯满 480s、cursor 跟到流末尾——多分钟级持续消费达标。
- **未做**:数小时级 compact 续航真机没跑(会话时长约束);且遇到 §7-7 的编队收口坑,长跑的「盯满→干净收口」闭环没走通。

### 7-7. 【下一棒头号】真机新发现的两处深层坑(根因 + 复现 + 证据坐标)

> 这两处都**不是**本棒 T1-T6 的机制没建对,而是**长跑盯守子代理的 runner 生命周期**层的既有短板,被真机 5 路编队压出来。摄取/判读/命中本身全对(7/12、0 误报),卡的是「盯满之后怎么干净收口」。

**坑 A(头号):长跑盯守子代理 480s 占住 runner → 被孤儿回收成 PENDING → 无唤醒源续派 → 编队收口卡死。**
- **根因**:watch 子代理按提示词「循环 pull(max_wait=45s)」同步占住 runner 线程整整 480s;后台派工的孤儿回收/租约把长跑 runner 标 PENDING;而 PENDING 既不发子代理完成 wake、盯守路也没登记 wait policy → **没有任何唤醒源触发续派**,5 路子代理终态全 PENDING 卡住,主代理 `integrate` 步永远等不到全部 window_complete → `submit_for_acceptance` 不触发、`closeout.ok=false`。
- **复现**:§4 测试台 5 源 × 480s 窗口;跑完看 `agent_runs` 全 PENDING、主账 `integrate` 项永远 open、`output/.agent_delivery/closeout.json` ok=false(空壳)。证据坐标:`~/my-agent-testruns/ingest-home/owners/providers/feishu/users/u-fleet/`(`agent_runs` 表、`memory_archive/task_progress/req_*/progress.json`)。
- **修法(建议,与非阻塞原则一致)**:watch 子代理**别同步 loop-pull 占 runner 480s**——改成「pull 几批→判读→窗口没到就登记 wait 并 yield(结束本轮 runner)→定时 wake 从**持久化游标**续 pull」。游标/引擎状态已跨轮持久(`watch_state.py`),resume-from-cursor 现成;wait policy 的定时 wake 比孤儿续派可靠。这需要改 `watch_stream`/runner 盯守引导词 + 验证 wait-wake 对 watch 子代理稳定,属**行为层改造 + 一次真机长跑复验**,本棒未做(避免又一次未充分验证的热路径改动)。

**坑 B:接管 run 建出后不被唤醒轮 `_redispatch` 可靠拉起(本棒已缓解未根治)。**
- **根因**:`create_takeover_run` 建 PLANNING 接管 run 后,唤醒轮上下文的 `dispatch_subagents(start_runners=True)` 拉 PLANNING 不稳(真机停在 PLANNING;`b45d7bbd` 全程没跑)。
- **本棒缓解**:`5a682383` 让补岗改走 `auto_start_tasks`(create_subagents 同款 durable 派工),真机死岗 8904 因此被主代理背景线程接上续到 cursor 27420。但**根因(唤醒轮 redispatch 对 PLANNING/PENDING 孤儿不稳)与坑 A 同源**,建议一并在 runner 生命周期层治：让 PENDING/PLANNING 孤儿有稳定的续派唤醒源(不是只靠 lifecycle wake)。
- **证据坐标**:同 u-fleet home;`agent_runs` 里 `b45d7bbd` 长期 PLANNING;`watch_lane_sweep.py` 的 `_auto_start_takeovers` 是本棒缓解点。

**次要**:T4 层4 闸默认关、只单实例信号量;多副本要 Redis/分布式信号量(`llm_scale/concurrency.py` docstring 已注明)。`_BACKGROUND_OWNER_WORKERS`/`OwnerScopedAgentPool.max_agents=64` 仍硬编码待提 config。

### 7-8. 质量门 & 测试

- **体量闸**:仓库根 `python3 scripts/check_code_size.py --mode strict` → `hard=0 high-risk=0 soft=0`(本棒新增代码全过;`soft=0` 连契约矩阵门 `test_offline_contract_matrix_gate` 一起过)。
- **新增钉子**:`test_ingestion_engine.py`、`test_ingestion_harness_replay.py`、`test_ingestion_watch_tool.py`、`test_watch_lane_sweep.py`、`test_llm_hot_path_admission.py`、`scripts/watch_harness/test_fleet_score_surface.py`、`test_concurrency_metrics.py`(加 enqueued/claimed 用例)。
- **基线对比**:全量 `pytest tests/`(在 `agent_py_agent/` 跑)——唯一「失败」是 `test_delivery_closeout_submission::test_tool_loop_service_does_not_replay_existing_failed_closeout_context`,它读源码相对路径**必须在仓库根跑**(基线 commit `5966ed6e` 同样如此,非本棒回归);在仓库根单独跑该文件 → 绿。

---

## 8. 测试方独立加难真机回归(2026-07-03,部署本棒全部提交到 origin/main 后)

> 由测试方以「普通不懂技术用户」口吻投递,难度较上一棒【加大】,一组组顺序跑(不并发堆)。每组 my-agent 2 用户(solo 主代理自写 / dispatch 派子代理)+ 外部对照工具。本节是**独立验证 + 给下一棒的靶子**,与 §7 的自评互为印证。部署冒烟:摄取层 12 文件真机 import OK、双服务 active、0 错误。

### 8-1. 监控组(能力一,加难=6 源异构 / 每源 150 条每秒 / 40 分钟 / 30 稀疏目标 / 缓冲约 6 分钟淘汰)
- **结果**:my-agent 单用户 **命中 4/30、误报 0**,~34 分钟后停;外部对照工具命中 2/30、0 误报(其定时续跑在无常驻运行时的一次性模式下未触发,受测试台限制)。
- **判断**:①两边**判据都对、0 误报**(结果端判真都判对,不被海量诱饵骗);②**头号未解 = 高吞吐 + 滚动缓冲下跟不上**:合计 900 条/秒、落后即永久丢,摄取层把 my-agent 从 0 提到 **4(有量化提升!)**但离「脚本级吞吐 + LLM 级判断」的合体还差一截 = **§7-1 摄取层方向对,但加难暴露出「深度 + 覆盖 + 续航」仍是硬地基**(呼应本文档头号);③续航仍痛(~34 分钟停)。
- **证据坐标**:`scripts/watch_harness/multi_source_simulator.py --sources 6 --rate 150 --duration 2400 --hits 30`;`fleet_score.py` 对账(注意只扫上报面、剔工具拉取原始数据,否则误报虚高);答案表 watch_answer_key.jsonl。

### 8-2. 代码组 G1(能力二,加难=完整点餐 + 后厨 + 统计 + 库存)
- **结果**:solo ~2000 行 **closeout ok=True**(规范包结构,与对照工具 1763 行同量级);**dispatch closeout ok=False**(派 4 子代理 3 个 CANCELLED → 整合判失败);对照工具干净交付 + 端到端测试。
- **判断**:①**✅ 2 并发建站用户无饿死**(上一轮「并发 solo=0 产出」没复现,明显改善);②**⚠️ dispatch 派子代理路仍不稳(头号)**——多子代理 CANCELLED + 整合判失败,产出/深度不如 solo(**solo 稳、dispatch 飘**,呼应上一版 P2/P3);③收尾「自证能跑」纪律弱(对照工具写了端到端测试,my-agent 建好但少验证那步)。

### 8-3. 数据处理组 G4(加难=300 万行 129MB 大文件,找月度/畅销/异常)
- **结果**:my-agent solo(206 行详报)+ dispatch(56 行薄报)均 **ok=True**,都写脚本流式处理大文件;对照工具 90 行简洁报告。
- **判断**:①**✅ 能处理大文件**(都没傻到让模型逐行读,写脚本流式扫);②**⚠️ 数据分析严谨度弱(头号)**:my-agent solo 犯「未先剔脏数据再聚合」的方法错 → 汇总总额虚高、排名被异常值扭曲;对照工具「统计口径已剔除问题记录」→ 结果准(总额误差 0.3%)。是「能跑出报告」vs「分析方法对」的差距;③dispatch 又是薄(56 vs 206),同 G1。

### 8-4. 给下一棒的靶子(独立回归汇总)
1. **能力一高吞吐深度 + 续航(头号)**:摄取层再往「结构化高速初筛 + LLM 批判断」深化;守望续航要真撑住数小时(§7-6 只到「部分」)。
2. **dispatch 派子代理路稳定性(头号)**:多子代理 CANCELLED + 整合方差,跨代码/数据两类任务复现(§7-7 两处深坑相关)。solo 稳、dispatch 飘是当前最一致的短板。
3. **收尾「自证能跑」+ 数据分析严谨度**:引导「建完写个测试跑一遍」「聚合前先剔脏数据」。
4. **未覆盖(如实标注)**:G2/G3(另两个代码任务)、G5(访问记录数据任务)本轮时间不够未跑;测试台已备好(prompt + 生成器),下一棒可直接复用扩测。
