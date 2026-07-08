# 交接单 · 监控过滤器根本设计修复(已修完+隔离长跑在跑,待收尾复测证据)

> **给谁**:接手继续的开发代理　**日期**:2026-07-06　**分支**:`fix/watch-delivery-and-output`
> **一句话**:上一棒(我)已把《监控过滤器》根本设计错误修完并提交(commit `3b7ebdda`),本地
> 全绿 + 机制层确定性对照坐实修复;隔离真机长跑正在跑,**你只剩两件事**:①收尾长跑、把
> live 召回数据回填进证据、提交证据;②接着做 monitoring_catch(独立任务,见文末)。

---

## ⚠️ 硬约束(务必守,与原交接一致)

1. **测试机 192.0.2.10(ssh `testbox`)和 192.168.1.9(ssh `openeuler`)上有正在跑的长测试。
   只读欢迎、严禁任何改动**:不 ssh 部署、不重启/复用其网关、不动任何进程/用户/端口/sim/数据。
   openEuler ssh 两坑:①命令加 `-o ServerAliveInterval=10`;②别用 heredoc(`<<EOF` 挂会话),传脚本用 scp。
2. **铁律**:代码判断只用结构化信号(计数/状态/产物/请求响应的结构化形态);**禁自然语言关键词匹配**。
3. **别破已验证 OK 的**:盯守高精度判读、reflux/节奏兜底、A2 种清单、假需求证据闸、solo 续推 —— 保持。
4. **本地绿=必要不充分**:真机由测试方独立复验,两边都过才算数;贴自己隔离环境的真实数据,别自称"真机通过"。

---

## 一、已经做完的(commit `3b7ebdda`,别重做)

**根本错误**:引擎按"某结果端取值在滑动窗口出现频率超阈值%(旧 2%,被改 25%)"就判"常态"、
整批压组丢弃(`_suppressed_spec_hit`/`_outside_normal_common`/`_named_target_common`)——真事一变
频繁就被当"太常见"整批扔。**2%→25% 是换数字不换病**。

**修法(频率触发调查、内容决定去留)——已落地**:
- `ingestion/config.py`:删 `outside_normal_common_value_pct`/`spec_target_common_value_pct` 两个
  去留阈值,换成**只触发告警不决定去留**的 `frequent_hit_investigate_pct`(默认 25,0=只关告警)。
- `ingestion/engine.py`:移除一切"频率超阈值即丢"路径。spec 命中(target/常态之外)**一律抬候选**,
  车道内按取值频次升序(稀有先上、高频沉底,名额有界+溢出入账,判读不被淹)。高频命中类只发
  **结构化调查告警**(`digest.frequent_hits`:类计数事实+示例事件,按取值类折叠防文本尾巴刷屏)。
  常态规则(spec 的 `normal_*`)命中按**规则条目记账**(`engine.rule_hits`,随快照持久化),事件回落
  通用兜底车道(稀有仍可抬)=减负来自"认得它了"、非"懒得看"。`apply_spec` 只在 `ignore_fields`
  变化时才重置签名窗/census(建规则不再付第二个冷启动窗口)。
- `ingestion/source_spec.py`:`SpecMatch` 加 `normal_value`/`normal_contains` 两模式,新增
  `classify()`(五模式全量报告)+ `_normal_entry()`(报命中的规则条目);`match()` 保旧契约。
- `ingestion/watch_payloads.py`:`spec_target_common`→`frequent_hit_investigation` 调查告警(带示例)、
  `content_rules_block()`(status 出规则审计块)、`attach_content_rules_count()`(pull 报本批减负数)。
- `ingestion/watch_tool.py`:status 载荷加 `content_rules`;spool 路合并调查告警+规则减负账。
- `ingestion/harvester.py`:spool 记录换 `frequent_hits`/`normal_rule_hits` 键。
- `ingestion/watch_learn.py`:configure 拒配文案改写——**不再教模型"把高频取值列进 normal"**
  (真事高发时那是把目标标成常态=整类漏死),改为"按内容再研判"两分支;CONFIGURE/SAMPLE 指引同改。
- **单测**:`test_ingestion_engine.py`/`test_ingestion_watch_learn.py`/`test_ingestion_harvester.py`
  免疫系列全改写为调查系列(高频照抬+告警带示例/稀有不回归/旋钮 0 只关告警/冷窗口不警/规则记账+
  快照往返/apply_spec 保窗;端到端"盲配反→告警→建规则→规则记账"调查环)。

**本地验收(全绿,我亲跑)**:
- 摄取全套 + `-m "not slow and not e2e"` 全量快速套件:**7884 passed, 29 skipped**(用
  `PYTHONPATH=agent_py_agent python3 -m pytest -m "not slow and not e2e"` 从仓库根跑;裸 pytest 会因
  个别测试用相对路径读文件而收集报错,那是环境不是回归)。
- code-size strict **0 findings**、DOC_SYNC **PASS**、架构护栏 + offline_contract_matrix gate 绿。
  (`CODE_SIZE_REPORT.md` 有改动=引擎类缩容后重新生成,一并提交即可。)
- **离线召回台 `scripts/offline_recall_bench.py` 新旧逐位一致**(除时间戳),未破 B 召回三件套。

**机制层确定性铁证(已落 `docs/tasks/evidence/monitoring_filter_iso/`)**:
`funnelA_replay.py` 同一确定性流喂旧(d58cb875)/新引擎,`funnelA_contrast.txt` 原始输出:
- **dense35(35% 密度,过旧 25% 线)**:旧代码 1721 真事只抬 **29**、1692 个(98.3%)被免疫整批扔;
  新代码 **1721/1721 全抬(100%)**、0 丢,只发告警+记账。← 这就是根本 bug 的复现与修复。
- 稀有车道旧新逐位一致(rare1500 29/29),**零回归**。

---

## 二、你要做的第 1 件:收尾隔离长跑 + 回填证据 + 提交(半小时内可完)

**隔离环境现在正在跑**(全程只本机、与 1.9/1.10 无关):
- `MY_AGENT_HOME=/Users/example/my_agent/iso_mf_retest/home`、网关 `127.0.0.1:8663`、
  sim 端口 `9561~9565`、用户 `mf-d1`(密集 18%+35%)/`mf-r1`(稀有 1/1500+1/1000)/`mf-x1`(漂移减负)。
- 起法记录:sim=`iso_mf_retest/sim/mf_sim.py`(后台)、任务=`send_tasks.py`、观测=`mf_observe.py`
  (每 3min 落 `logs/observe.log`)、精确召回=`mf_recall.py --window <分钟>`。
- 网关是本机 editable 安装,`python3 -m agent_py_agent --config iso_mf_retest/iso_config.yaml gateway
  {start,stop,status}` 即可;跑的就是 my-agent-main 的新代码(已确认 import 解析到本仓库)。
- t+3min 实况:mf-d1 已 31 findings、dense35 过线目标照抬照报(esc/frequent 都涨)、内容规则
  `normal=false/normal` 在减负——**机制在真模型下如预期**。
- **t+15 window 快照(`mf_recall.py --window 15`)**:precision **99.4%**、稀有两路 **100%**
  (rare1500 5/5、rare1000 1/1)、drift 2/3;dense18 38.9%、dense35 27.9%。
  **⚠️ dense 百分比偏低是窗口口径的"追流尾滞后",不是回归**:分母是"窗口内已产生的全部目标",
  而模型逐条判读+record_finding 落后于流尾几分钟(findings 仍在快速爬:t+3→t+6 mf-d1 从 31→128)。
  判修复对不对**看 esc_spec/frequent_hits 在涨且 precision≈99%(过线目标在照抬照报、零整批丢弃)**,
  别只看某一刻的召回分子;跑满窗口 + 清完积压后再算终态召回才是可比数(见收尾步骤 1)。
  对照旧代码 funnelA:dense35 引擎层只放 1.7%,模型根本拿不到——这才是被修掉的病。

**收尾步骤**:
1. 长跑到 70min 窗口走完(或你判断够了),`python3 mf_recall.py --window 70` 出各车道精确召回;
   把输出存 `evidence/monitoring_filter_iso/recall_final.txt`,各 watch 的 spec+totals 存
   `live_totals.txt`(observe.log 已有时间线;新键 `spec_frequent_hits`/`spec_normal_rule_hits`、
   旧免疫键应彻底消失)。
2. 用真实数据回填 `evidence/monitoring_filter_iso/README.md` 第二三节的占位(§一 funnelA 已写实)。
3. 验四条验收:①dense18/35 高密真事召回高(不再整批扔)②rare 两路不回归 ③内容规则减负可核算
   (status `content_rules`/`spec_normal_rule_hits`>0 且拦了多少可查、零静默)④代码零"按频丢弃"。
4. `git add docs/tasks/evidence/monitoring_filter_iso CODE_SIZE_REPORT.md && git commit`;更新本交接单
   状态或写定稿说明。**跑完 `python3 -m agent_py_agent --config .../iso_config.yaml gateway stop` 收摊,
   kill mf_sim/mf_observe 后台进程**(它们只在本机,与测试机无关)。

> 若嫌长跑久:机制层 funnelA 对照已确定性证明引擎修复(dense35 1.7%→100%),live 长跑只为佐证
> "真模型端到端也照抬照报、减负可核算"。可只取一段(如 25~40min)贴数据,注明窗口口径即可。

---

## 三、你要做的第 2 件:monitoring_catch(独立任务,做完上面再开)

`docs/tasks/DEV_HANDOFF_monitoring_catch.md`(读整篇)。一句话:1.9 上"真实难判场景几乎抓不到真事",
要你**只读上 1.9(`ssh openeuler`,加 `-o ServerAliveInterval=10`,禁 heredoc)确认+挖深**病因,现成
诊断脚本 `/root/diag.py`、`/root/diag2.py`。**三个坑**:①测量假象(别追"瞎报 bug",那是漏不是瞎报)
②别看一眼就喊"测试不公平"(真事能靠请求+响应对出来)③病因别拍脑袋,要对历史+查 learn 状态,
钉死是"模型判不动"还是"代码回归"还是"learn 没生效"。钉死后在本套代码上接着修(大概率与《过滤器》同源:
按内容学"请求意图↔响应命中"关联规律、逐类研判,别冷批扫拍"全合规")。修完照旧:本地绿+隔离真机自测。

> 我 20:xx 只读探过一眼 1.9:`recall.py u-sm1` = 真事 79、报对 1、召回 1%、误报 15、漏 78——
> 病象与文档一致,但**没深挖、没钉病因**,留给你按文档三坑走。别把那 15 个"误报"当 bug 追(是漏不是瞎报)。

---

## 附:关键文件地图
- 修复代码:`agent_py_agent/agent/ingestion/{config,engine,source_spec,watch_payloads,watch_tool,harvester,watch_learn}.py`
- 单测:`agent_py_agent/tests/test_ingestion_{engine,watch_learn,harvester}.py`
- 机制证据:`docs/tasks/evidence/monitoring_filter_iso/`(funnelA_replay.py / funnelA_contrast.txt / README.md / mf_sim.py / mf_recall.py)
- 原始需求:`docs/tasks/DEV_HANDOFF_monitoring_filter.md` + `evidence/monitoring_filter/mechanism_and_data.md`
- 下一任务:`docs/tasks/DEV_HANDOFF_monitoring_catch.md`
- 隔离环境:`/Users/example/my_agent/iso_mf_retest/`(home/sim/logs/iso_config.yaml)
