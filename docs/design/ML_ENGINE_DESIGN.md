# my-agent 内置 ML 推理/决策引擎 — 设计方案(终版)

> 状态:**设计稿,待审,未动手**
> 定位:ML 引擎 = **推理/决策引擎**(`输入特征→模型推理→输出预测/决策`),**不是检索/相似匹配**(此前理解已纠正)。
> 场景:多子代理监测多个 API,每 API **1–10G/小时**日志 → 异常分诊 + 智能协调派工 + 经验复用
> 输入参考:claw `LocalMLEngine` + 业界 2026 agentic SOC(反馈闭环 / 监督·无监督·关联三路 / 解释层 / 联邦学习·迁移学习 / UEBA / autonomous detection engineering)+ 你的诉求(outcome 维度、agent 驯化评级、跨用户经验复用不重训、行为异常、agent 现编检测)
> 守约:只动**通用底座**、`ml_engine_enabled` 默认关、不破现有规则路径、复用 `log_ops`/`dispatch`/`collaboration`、gate 绿

---

## 0. 一句话

对每小时 **1–10G、多 API** 的日志,ML 引擎做"**降维 → 三路评级 → 协调派工**"的扛量漏斗;评级**会被 agent 驯化**(outcome 标注回流,不死板);经验**跨子代理/用户/时间复用**(脱敏领域包,不重训);并覆盖**预设规则看不见的两类情况**——**行为异常**(UEBA:一个IP扫多个IP、流量突增)和**未知情况**(agent 与人**现编检测**并部署)。

---

## 1. 目标 / 非目标

**目标**
- 1–10G/h/API × 多 API 下,**不喂原始日志给 LLM、不逐行推理**——分层漏斗压量级。
- 产出**可解释**异常评分/分级/路由 + "派几个子代理、派给谁"的协调计划。
- 评级**可被 agent 复核结论(尤其 outcome)驯化**,不是固定公式。
- 经验**脱敏沉淀、跨用户迁移**,新任务不重训。
- 覆盖**关系型/行为型异常**(扇出扫描、流量突增)。
- 支持 agent **与人协作现编新检测**并部署持续监控(应对预设外情况)。
- 预留口子:确定性 scorer 可无缝换训练模型(输出契约不变)。

**非目标**
- 不依赖外部 ML 服务(纯内置)。
- 不破坏 log_ops"一条不丢"——ML 只改排序/优先级/路由,绝不删原始。
- 第一版不追训练模型/任意脚本注入,先做确定性 first-pass + 声明式规则(可解释、可测、可上线)。
- 不破现有规则路径(`ml_engine_enabled` 默认关)。

---

## 2. 现状盘点:有头有尾,缺中段

| 层 | 现状 | 评价 |
|---|---|---|
| ①采集 | `log_ops` daemon:确定性、扛量、不丢、续采、对账 | ✅ 复用 |
| ②降维 | 无(candidate 逐条进队列,不聚类) | ❌ 缺 |
| ③ML 引擎 | 只有 `triage._anomaly_severity` 规则阈值 + `baseline` 统计异常 | ❌ 缺(核心) |
| ④智能协调 | 靠 `dispatch_default_max_runners`(默认1)+ 主代理 LLM 手动 | ❌ 缺 |
| ⑤LLM 研判 | 6 工具 + collaboration | ✅ 复用 |
| ⑥经验复用 | 无(子代理 DONE 经验丢、跨用户各学各的) | ❌ 缺 |
| ⑦行为异常(UEBA) | 扇出扫描**基本无**(`BurstTracker` 只数单IP频率不数distinct目标);流量突增**半套**(无 per-entity 时序基线/EWMA/落盘) | ❌ 缺 |
| ⑧agent 现编检测 | **半能**:`log_profile_set` 能注入正则规则+持久化;但缺超正则表达力/验证/回流/协作结构 | ⚠️ 半 |

**关键发现**:`triage` 产出的 **candidate** = 天然 ML 输入单元(对应 claw `SignalEvent`)。

**本文两条线**:
- **三大支柱(引擎怎么运转)**:① 扛量(§3)② 会学(§4)③ 复用(§5)
- **两个覆盖面(检测什么)**:① 行为异常 UEBA(§6)② 未知→agent 现编(§7)

---

## 3. 支柱一 · 扛量漏斗

### 3.1 五层(1–10G/h)

```
多 API 日志(每 1–10G/h)
 ▼ ① 采集【复用 log_ops daemon】流式落档·不丢·对账;triage 产 candidate
 ▼ ② 降维【新建 reducer】按 cluster_id=(domain:fingerprint)聚类+窗口聚合;按 domain 分 shard(10K/256MB),压缩 10–1000×
 ▼ ③ 三路评级【新建 engine】SignalCluster→特征→三路融合分→band→route(§4)
 ▼ ④ 协调派工【新建 coordination 接 dispatch】按分+负载决定派几个/派给谁
 ▼ ⑤ LLM 研判【复用 6 工具 + collaboration】只 critical/high 升级,token 可控
```

### 3.2 扛量关卡

采集(流式·熔断限速,扛 G 级)→ 分片(domain 分 shard,prefilter 100K条/1GB)→ 降维(聚类去重+窗口聚合,**10–1000×**)→ 推理(只对 N 簇,截断 `max_assessments=200`)→ 派工(`max_temporary_workers=16` 低扇出)→ LLM(只 critical/high)。**每关早截断,海量原始永不到 LLM。**

### 3.3 召回保障(回应"原始到不了 LLM 会不会漏")

①数据不丢(被升级的簇 agent 顺 `evidence_refs` 回查**原始行**)②无监督/UEBA 高分**强制升级**(抓 0-day/行为异常)③LOW 采样抽查 ④不确定即升级(`uncertainty` 高)⑤可调旋钮(阈值/采样率/权重)。

---

## 4. 支柱二 · 会学(自适应闭环)

### 4.1 评级 = 预测 outcome 风险(不死算 severity)

- **标签**:`SUCCESS / ATTEMPT / FAILURE / OTHER`——目标是"**会不会成功**"(成功>企图>失败>其他)。
- **特征**:告警类型(10类)、severity、confidence、count、突发率、duration、统计异常、**实体行为特征(§6)**。
- agent 复核打 outcome 标签 → 引擎学"哪些特征组合→会成功"。

### 4.2 三路融合评级

| 路 | 抓什么 | 落地 |
|---|---|---|
| 监督路 | 已知模式、"像以前成功过的" | M1 确定性加权;M3 训练分类器(吃 outcome 标注) |
| 无监督路 | **未知/0-day + 行为异常** | 复用 `baseline` + **UEBA 特征族(§6)** |
| 关联路 | 攻击链(扫描→爆破→提权) | 复用 `log_correlate`/`log_lead`;M3 图关联 |

```python
supervised_risk = severity·0.40 + count(log1p)·0.20 + burst·0.20 + confidence·0.10 + statistical_anomaly·0.10
fused_score = w_sup*supervised_risk + w_uns*unsupervised_anomaly + w_cor*correlation_boost
# 融合初值 0.5/0.3/0.2,随 outcome 标注自适应(带护栏,见风险章)
```
**Band**:CRITICAL≥0.78(或 sev≥5 且 count≥20)·HIGH≥0.58(或 sev≥3)·MEDIUM≥0.30·LOW else
**Route**:CRITICAL→`MAIN_AGENT_ESCALATION`·HIGH→`CHILD_DOMAIN_REVIEW`·else→`TEMP_GRANDCHILD_EVIDENCE_SCAN`

### 4.3 解释层(业界:别只给裸分数)

每评级带 `narrative` + `supporting_context` + `uncertainty`(不确定度)+ 三路分解 + `evidence_refs`。`uncertainty` 高时主动说"我不确定"促 agent 复核,避免系统性过度/不足信任。

### 4.4 co-teaming 闭环

ML 给建议 → agent 复核 critical(覆盖评级+打 outcome+补上下文)→ 标注回流 → 引擎自适应(M1 调权重 / M3 增量训练)。**M1 就通闭环**——这是"我能不能影响评级"的答案:M1 就能。

---

## 5. 支柱三 · 经验复用(跨子代理/用户/时间不重训)

> 场景:这月用户A 子代理监控 A类设备日志,下月用户B、下下月用户C,**都是普通用户**。= 业界 **MSSP** 难题。

### 5.1 钥匙:分离"数据"与"经验"

- **原始日志**=隐私 → **per-user 严格隔离** · **学到的模式知识**(特征→成功攻击、权重、指纹模板)=脱敏可泛化 → **可跨用户复用**
- 业界:共享检测模型靠"**不可追溯回原始数据的通用特征**"(USPTO);联邦学习"只共享模型更新不共享原始数据"。

### 5.2 三层

| 层 | 内容 | 跨用户? | 复用 |
|---|---|---|---|
| ①领域经验包 | 按"A类设备"领域:指纹模板+特征schema+初始权重+**脱敏**攻击模式 | **共享**(脱敏) | 新建(产品级,领域索引) |
| ②用户私有适配 | 各用户原始数据+私有标注+本地微调 | **严格隔离** | 复用 `OwnerScopedAgentPool` |
| ③子代理运行时 | 加载①+②干活;新经验脱敏回流①、私有沉淀② | 临时 | 复用 dispatch |

**子代理 DONE 时经验已沉淀①②,不丢。**

### 5.3 冷启动(不重训)

子代理认领域=「A类设备」→ 加载①领域包(出厂内置+前人沉淀)→ 叠②用户私有微调 → 干活边学回流。**A→B→C 各吃前人经验,越来越准,无须从零训。**

### 5.4 隐私护栏(普通用户,硬约束)

①只回流脱敏模式绝不回原始日志 ②聚合+差分隐私才入库(防反推)③防投毒(贡献校验+异常剔除)。

### 5.5 贡献策略(默认,可配)

同用户·跨时间=**默认复用**(②层私有,无隐私问题);出厂领域包=**内置可用**;**跨用户共享=默认私有,opt-in 才贡献**(隐私优先)。守住"②隔离不动、①只放脱敏经验",故不破隔离不污染。

---

## 6. 覆盖面① · 实体行为分析(UEBA)——关系型/行为型异常

> 预设规则和指纹聚类**看不到**"一个IP扫多个IP""流量突增"这类**跨实体/时序**异常。补 UEBA 这一维(业界 UEBA/NDR:给每个实体建行为基线,偏离即报,抓规则工具看不见的横向移动/扫描/spike)。与三路评级**正交**,挂在无监督路。

### 6.1 实测缺口

扇出扫描**基本无**(`BurstTracker`/`store.py:79` 只数单IP频率,不数 distinct 目标→慢扫100个目标抓不到);流量突增**半套**(单IP频率有,无 per-entity 时序基线/EWMA/落盘,`baseline.py:185` 只用简单线性);地基问题:`extract_entities`(`baseline.py:42`)解析了实体字段但**没存进候选**(`triage.py:170`)→ 下游拿不到。

### 6.2 补三样(按依赖)

1. **(地基) 结构化实体字段进候选**:src_ip/dst_ip/port/user/action 解析后**存进 candidate**(现在提取了却丢)。A、B 共同地基。
2. **(关系) 按实体聚合 + 基数/扇出**:按 src_ip 聚合,算 **distinct dst 数 / fan-out 度 / 端口基数**——超基线即扫描。
3. **(时序) per-entity 速率基线 + 突变检测**:滑窗速率 + **EWMA/Z-score** + **落盘持久化**(现在内存重启丢)。

### 6.3 接到漏斗

- **②降维**:加"**按实体聚合**"的第二聚类键(entity-keyed),和 fingerprint 聚类**并行** → 一个IP→N目标聚成一个**实体簇**,扇出度一目了然。
- **③无监督路**:升级为 **UEBA 特征族**(实体基数/扇出 + per-entity 时序突变),高分强制升级(召回保障)。
- **特征向量**:`MLFeatureVector` 加 entity 字段 + cardinality/fan-out + rate_deviation。

---

## 7. 覆盖面② · Agentic Detection Authoring——人机协作现编检测(应对未知)

> 预设外的情况靠 agent **与人一起现编检测**并部署。业界 2026 **autonomous detection engineering**(微软 CTI-REALM、Sublime ADÉ、AgentSigma):给 agent 和人类检测工程师**同样的工具**,从情况/情报**生成检测草案→自动验证→部署→持续自我改进**。

### 7.1 现状:已具备一半

✅ **分析**(读样本 `log_source_sample`/基线)→ ✅ **生成正则规则**(`log_profile_set`,`tools_adaptive.py:93`)→ ✅ **部署持久监控**(写盘 `profiles/`,daemon 自动生效,**重启存活**,ReDoS 校验)。"你说情况→agent 读样本→现编规则→挂上监控"**现在就能跑**。

### 7.2 完整闭环五步

```
分析情况 → 生成检测草案 → ★验证(拿历史存档+已知样本回测:误报/漏报)→ 部署 → 效果回流调整
```
**★验证是关键**:agent 编的规则**先验证才上线**,不是直接信(业界共识)。

### 7.3 缺四块(复用已有底座,不另起炉灶)

| 缺口 | 补法 | 复用 |
|---|---|---|
| **表达力(超正则)** | `log_profile_set` 从正则升级**声明式规则**(时窗/聚合/字段条件) | = §6 UEBA |
| **验证** | 新增"规则回测":拿 archive 历史 + 已知样本测误报/漏报,达标才 promote | 复用 archive |
| **效果回流** | 误报→调阈值/改规则/版本化 | = §4 反馈闭环 |
| **协作结构** | collaboration 加 `RuleDesignCase`(人机定场景/评审/固化) | 扩展 collaboration |

### 7.4 高风险,缓做

agent 写**任意检测脚本/函数**进 log_ops 闭环(daemon 无 hook)——业界也谨慎(需沙箱)。**先用声明式规则覆盖大部分**,不急开任意代码注入。

---

## 8. 核心数据结构(契约即换模型口子)

```python
@dataclass(frozen=True)
class SignalCluster:        # ②降维单元
    cluster_id; domain_id; fingerprint; cluster_kind: str   # "fingerprint" | "entity"
    severity: int; count: int; confidence: float
    time_window: tuple[float, float]; burst_per_min: float
    entities: dict[str, str]                 # ← UEBA:src_ip/dst_ip/port/user...
    distinct_targets: int; fan_out: int      # ← UEBA:扇出/基数
    evidence_refs: tuple[str, ...]; statistical_anomaly: float

@dataclass(frozen=True)
class MLFeatureVector:      # ③推理统一输入契约
    cluster_id; domain_id; fingerprint; alert_type: str
    severity: int; count: int; confidence: float
    duration_seconds: float; count_per_minute: float
    cardinality: int; fan_out: int; rate_deviation: float   # ← UEBA
    statistical_anomaly: float

class SignalOutcome(StrEnum): SUCCESS="success"; ATTEMPT="attempt"; FAILURE="failure"; OTHER="other"

@dataclass(frozen=True)
class MLSignalAssessment:   # ③评级输出(带解释)
    cluster_id; domain_id
    fused_score: float; band: AnomalyBand; route: CoordinationRoute
    supervised_risk: float; unsupervised_anomaly: float; correlation_boost: float
    predicted_outcome: SignalOutcome; outcome_confidence: float; feature: MLFeatureVector
    narrative: str; supporting_context: tuple[str, ...]; uncertainty: float
    evidence_refs: tuple[str, ...]; reasons: tuple[str, ...]; ai_hint: str

@dataclass(frozen=True)
class OutcomeLabel:         # ②agent 回流标注(驯化引擎)
    cluster_id; outcome: SignalOutcome; corrected_band: AnomalyBand | None
    rationale: str; labeled_by: str; labeled_at: float

@dataclass(frozen=True)
class DomainExperiencePack: # ①跨用户脱敏经验包(迁移载体)
    domain_id; version: int; fingerprint_templates: tuple[...]; feature_schema: dict
    fusion_weights: dict; band_thresholds: dict; known_patterns: tuple[...]  # 脱敏·不可追溯

@dataclass(frozen=True)
class DetectionRule:        # §7 声明式规则(超正则)
    rule_id; name; source_id
    match: dict              # pattern / 字段条件
    window_seconds: int | None; aggregate_by: str | None    # 时窗 / 按实体聚合
    threshold: float | None  # 如 distinct_dst > N
    severity: str; version: int; status: str  # draft|validated|production

@dataclass(frozen=True)
class RuleValidationResult: # §7 规则回测
    rule_id; tested_lines: int; true_positive: int; false_positive: int
    precision: float; recall_hint: float; passed: bool

@dataclass(frozen=True)
class MLAnalysisResult:     # 总输出契约
    total_candidates: int; shard_count: int; total_cluster_count: int
    assessment_limit: int; omitted_assessment_count: int
    assessments: tuple[MLSignalAssessment, ...]; coordination_plan: MLCoordinationPlan
```

**契约即换模型口子**:`MLFeatureVector`(输入)+`MLSignalAssessment`(输出)就是换训练模型的接口——M3 `TrainingMLEngine` 只重写 `feature→supervised_risk`,上下游全不动(claw 就这么留的)。

---

## 9. 与 my-agent 接线

| 动作 | 内容 |
|---|---|
| **复用** | `log_ops`(采集/triage/`baseline`/`log_correlate`/archive/`log_profile_set`)、`dispatch`、`collaboration`、`OwnerScopedAgentPool`(②隔离)、`enable_self_learning` |
| **新建** | `agent_py_agent/agent/ml_engine/`:`models`/`reducer`(②降维+实体键)/`engine`(③三路)/`coordination`(④派工)/`feedback`(标注+自适应)/`explain`/`experience`(①领域包+脱敏)/`ueba`(实体聚合+基数+时序)/`authoring`(声明式规则+回测) |
| **接口** | `log_ml_analyze`/`log_ml_label`(回流标注)/`log_ml_calibrate`(per-API)/`log_rule_author`(现编规则)/`log_rule_validate`(回测)/`log_rule_promote`(验证后上线) |
| **配置** | `ml_engine_enabled`(默认关)/`ml_max_assessments=200`/`ml_max_workers=16`/`ml_fusion_weights`/`ml_band_thresholds`/`ml_sampling_rate`/`ueba_enabled`/`ml_experience_share`(默认 opt-in) |

---

## 10. 分阶段里程碑

| 阶段 | 交付 | 验证 |
|---|---|---|
| **M1:可学的漏斗 + UEBA 地基** | `models`+`reducer`(双键聚类)+`engine`(确定性三路)+`explain`+`feedback`(②私有标注回流)+`log_ml_analyze`/`log_ml_label`;**修 UEBA 地基**(实体字段进候选);**声明式规则雏形**(`log_rule_author` 基础) | 单测(评分/分级/防爆/压缩比/实体字段透传/标注调权重/私有隔离)+ 真机合成 1G 日志 |
| **M2:协同 + UEBA + 经验包 + 现编闭环** | `coordination` 接 dispatch;UEBA(实体聚合/扇出/时序突变);①领域经验包(出厂+冷启动);规则**回测+promote**+`RuleDesignCase` | 真机:多 API 派工 vs 默认 single_worker;扇出扫描/流量突增检出;B 吃 A 沉淀冷启动;agent 现编规则回测后上线 |
| **M3:模型 + 关联 + 联邦 + 自适应规则** | 监督路换训练模型(影子A/B);关联路图关联;联邦聚合护栏;规则误报→自动优化 | 影子对比确定性 baseline |

每阶段独立 gate 绿 + 真机验证后再进下一阶段(守"别一轮仓促改架构"教训)。

---

## 11. 关键决策(请拍板)

1. **三大支柱 + 两覆盖面**框架对不对?
2. **outcome 作评级目标**(预测成功率而非死算 severity)——认同?
3. **经验复用三层**(数据隔离 + 脱敏经验共享)——认同?跨用户**默认 opt-in** 还是默认贡献?
4. **UEBA 挂无监督路**(实体聚合键 + 基数/扇出 + 时序突变)——认同?
5. **现编检测走声明式规则 + 强制回测**(不急开任意脚本注入)——认同?
6. **从 M1 动手**(可学的漏斗 + UEBA 地基 + 私有经验沉淀)还是再调?

---

## 12. 风险与权衡

- **评分权重需调**:claw 权重是告警语境,多 API 按真实数据回归;M1 用可解释默认值 + config。
- **聚类指纹质量**:压缩比/召回取决于 fingerprint(太粗漏真异常,太细压不动)。
- **UEBA 基数内存**:distinct 集合大规模耗内存→用 HyperLogLog 近似基数 + 落盘窗口。
- **冷启动**:M1 标注少→靠无监督路+UEBA 兜底,攒够再加重监督权重。
- **标注质量**:agent 打的 outcome 不准会带歪→`rationale`+审计+采样校验。
- **权重漂移**:自适应调权重要护栏(范围约束+影子对比)。
- **现编规则误报/ReDoS**:强制回测达标才上线 + 正则安全校验;任意脚本注入缓做(需沙箱)。
- **脱敏 vs 复用价值 / 投毒**:聚合+差分隐私是旋钮;跨用户①层带贡献校验。
- **不破现有**:`ml_engine_enabled`/`ueba_enabled` 默认关,老路径不动。

---

## 13. 一页结论

my-agent 已有**采集**和**研判**两头,缺中间的 **ML 推理/决策引擎**。本设计填中段:**三大支柱**(扛量漏斗 + outcome 标注驯化的会学 + 脱敏领域包的复用)+ **两覆盖面**(UEBA 抓行为异常 + agent 现编检测应对未知)。一整条覆盖"**预设规则 + 行为异常 + 未知现编 + 自我改进 + 跨用户复用**",对齐业界 2026 agentic SOC。建议 M1→M2→M3,每阶段 gate + 真机验证,M1 先把"可学的漏斗 + UEBA 地基 + 私有经验沉淀"跑通。
