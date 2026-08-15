"""摄取层可调参数(压缩比/窗口/阈值都能调:起步 100 条/秒,演进到万级/秒只动参数)。"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class IngestTuning:
    # 滑动窗口:签名频次统计的时间窗与桶粒度(秒)。
    window_seconds: int = 300
    bucket_seconds: int = 30
    # 字段画像:每个字段路径最多记多少个不同取值,超过即视为高基数(粘性,不回退)。
    low_cardinality_limit: int = 24
    # 稀有度阈值:签名在窗口内计数 <= 该值的事件被抬为候选。
    rare_threshold: int = 3
    # 少数派取值通道(测试方独立复测实锤:真目标与诱饵结构相同、只差结果端一个取值,
    # 签名稀有度天花板 7/30):字面取值字段的 (路径,取值) 在窗口内计数 <= 该值,且该
    # 字段窗口样本量 >= value_min_support,也抬为候选。0=关闭该通道。
    value_rare_threshold: int = 3
    value_min_support: int = 64
    # 取值车道独立候选名额(与稀有形状车道互不挤占——单一名额池会让成群的稀有形状
    # 诱饵按序号平手挤掉真目标)。
    value_max_candidates_per_pull: int = 8
    # 首记号子车道(文本结果端)宽抬三参(测试方真机实锤:文本源召回 ~53%,离线复现坐实
    # 漏因全在闸——目标共享同一结论词时窗口内累计超绝对阈值,第 4 个起全漏):
    # 相对稀有闸:首记号窗口计数 <= max(value_rare_threshold, 窗口字段样本量 * pct / 100)
    # 才抬——"稀不稀"看相对占比而非绝对次数,稀疏结论词不再被自身重复卡死。0=只用绝对阈值。
    head_value_rare_pct: int = 2
    # 首记号候选独立名额(不与字面少数派挤同一池:文本源目标天生只从这条路上来)。
    head_value_max_candidates_per_pull: int = 8
    # 首记号画像的基数上限(独立于 low_cardinality_limit:首记号=结论词集合,天然比
    # 一般字段宽;沿用 24 会因偶发杂词把该字段此路永久关闸)。
    head_low_cardinality_limit: int = 48
    # spec 命中的调查触发线(铁则:量大 ≠ 可以不研判;频率只触发调查、内容决定去留,
    # 去留判据永远来自内容判断,不来自计数阈值)。spec 命中的取值类在窗口内占字段样本量
    # 比例超过该值(计数 > max(value_rare_threshold, 样本量*pct/100),且样本量 >=
    # value_min_support)时,命中【照常按车道名额抬升、一条不丢】(车道内按取值频次升序,
    # 稀有命中永远先上、高频命中沉底,判读负载有界),只随批发结构化调查告警(类计数事实
    # + 示例事件),由模型按内容(请求端+结果端)研判这一类:判为噪声(判据配反/常态漏列)
    # → configure 把该类列进 normal_*(=建内容过滤规则,命中逐条记账可审计);判为真事 →
    # 照常逐条上报(哪怕它频繁),量大可提 spec.max_per_pull。历史教训(g8 复验实锤):
    # 旧版在这条线上"整批压组免疫"——2% 阈值把 18% 密度的真目标整车道当"常态"吞进被压组
    # (seen=8470 全量、escalated=6,召回平 ~7%),改 25% 只挪线不换判据(真事过线照样被吞)。
    # 一切"频率超阈值即丢"的去留判据已整体移除;本阈值只决定"何时提醒去看",0=不提醒。
    frequent_hit_investigate_pct: int = 25
    # per-源判据 spec 车道独立名额(学出来的精准判据,绝不能被通用车道诱饵挤掉;
    # spec.max_per_pull>0 时以 spec 为准)。
    spec_max_candidates_per_pull: int = 8
    # 每次 pull 返回给模型的候选上限;超出的进 overflow 账目(带坐标,不静默丢)。
    max_candidates_per_pull: int = 8
    # 正常量直通(需求:1000-100000/天的正常量够模型逐条认真读,不许囫囵)——本批事件数
    # 不超过直通预算(min(本值, 判读余量 judge_headroom))时,整批全量抬给模型分析:
    # 常见形状不压组、名额不裁剪(内容过滤规则 normal_* 命中仍照记账回落=教过的常态减负)。
    # 量涨到预算外(洪水/冷启动追赶)或判读积压把余量吃光 → 自动回落降维分诊,反压天然
    # 衔接;判完积压余量回升又自动恢复直通。纯计数切换,零速率估算。0=关闭直通(旧行为)。
    full_read_per_pull: int = 48
    # 批摘要里列出的被压组上限(按窗口计数降序)。
    max_suppressed_groups_listed: int = 24
    # 游标拉取:单页 limit 与单次 pull 处理事件总数上限(背压:超出留给下次,如实报滞后)。
    page_limit: int = 400
    max_events_per_pull: int = 20000
    # pull 长轮询:块内每次重拉源的间隔与等待上限(秒)。
    poll_interval_seconds: float = 1.5
    max_wait_cap_seconds: int = 55
    # poll 模式(mode=poll 的快照接口)专用查询节拍:每隔这么多秒查一次接口,把整份响应
    # 包装成一条事件交研判(游标源/文件源不受此参数影响)。
    poll_query_seconds: int = 60
    # 后台连续摄取(harvester):1=开(模型研判期间照样拉流,源端滚动缓冲不淘汰漏),0=关
    # (回到 pull 块内拉流的旧行为)。
    background_harvest: int = 1
    # 无窗长守时,多久没人 pull 消费就自停收割线程(0=不自停;有窗时按窗口+余量自停)。
    harvester_idle_stop_seconds: int = 1800
    # 收割喂引擎的分片大小:冷启动/断点追赶会一次 drain 上万条积压,若整批一次 process,
    # 稀有候选会挤爆"每批候选上限"进 overflow(真机实锤:12421 条一批,91 达标挤 8 位,
    # 真命中落 overflow 模型看不见)。按片喂,候选位随积压量线性扩,稳态(每拍几百条)不受影响。
    harvest_chunk_events: int = 500
    # 抬取背压(P1 头号:别让缓冲区单向只进不出无限堆;真机实锤:passthrough 五源各把
    # 整条流 ~5000 条倒进 spool,消费追不上→真事被埋报不出/过载 rubber-stamp)。spool 里
    # 【未读】候选积压达到 factor×judge_quota 时,收割线程本拍不再 drain(游标不动、只续
    # 租约心跳),等消费者判读推进读游标、积压回落再续抬——把抬取速度钳到判读速度。
    # append-only 游标源=延迟无损(游标续读);滚动缓冲源=如实记覆盖缺口(gap)。纯计数
    # 自适应任意模型判读速度,零速率估算、零关键词。0=关闭背压(回到无界堆积的旧行为)。
    # 只对内容型全量直通(passthrough/冷启动且直通开着,候选≈事件易洪泛)生效;结构化
    # 判据源候选天生稀疏、不洪泛,不背压。8×judge_quota≈8 个 pull 的前瞻缓冲,够平滑
    # "模型思考间隙照拉"又能把 passthrough 五源各 5000 条的整流洪泛钳住。
    spool_backpressure_factor: int = 8
    # Owner-facing capacity health uses these mechanical thresholds only. They
    # do not classify event content or change verdicts. A user may override
    # them per prepared source when a slower/faster operational SLO is needed.
    capacity_alert_backlog_records: int = 1000
    capacity_alert_oldest_seconds: int = 900
    capacity_alert_cooldown_seconds: int = 900
    # ── 摄取召回三件套(B2 抽检 / B3 反馈学习 / B4 倾斜;真机实锤:判 0 误报但筛只把
    # 26% 喂给模型、一个源 0/177 全瞎——很多真目标语义上真、结构上和常态一样)──
    # 抽检车道(常态流分层抽样喂模型,撞结构筛盲区):每次 process 的名额上限;0=关。
    audit_sample_per_pull: int = 2
    # 盲区证据成立(抽检确认过/反馈车道在抬/筛长期零抬升)时的倾斜名额上限。
    audit_tilt_per_pull: int = 6
    # 抽检允额(每分钟,独立滑窗):base 档与倾斜档——有界=负载固定,不挤占判力。
    audit_sample_per_minute: int = 6
    audit_tilt_per_minute: int = 24
    # 抽检涓流保底(每分钟,与抽检同一滑窗共账):判读反压(judge_headroom)钳位时仍保留的
    # 最小抽检额度;0=关(反压可把抽检钳到 0)。真机实锤:90 分钟盯守 backlog 恒 >8 →
    # headroom 恒 0 → 抽检全程 0 条 → 反馈飞轮的盲区发现断粮,funnel A 天花板没人抬。
    # 默认 1/min:90 分钟最多 90 条额外判读(相对真机 ~1700 消费量约 5%),不淹判力。
    audit_floor_per_minute: int = 1
    # 反馈车道(模型确认真目标的结构特征回灌,同特征事件自动抬升)独立名额;0=关。
    feedback_max_candidates_per_pull: int = 8
    # 每特征每窗口的抬升上限(洪泛闸:特征若配到常态取值,最多污染这么多判力)。
    feedback_feature_window_cap: int = 24
    # 特征自动退休线:实抬 >= 此数仍只有注册那一次确认且已过 stale 窗 → 停抬(新确认复活)。
    feedback_retire_min_lifted: int = 64
    # 判读并发不在摄取配置中规定。持久队列提供一份权威交付游标和逐条签收账；
    # Agent 可按目标、负载和可用执行槽自主决定自己消费、委派或复核，摄取层不固定工数与角色。
    # ── /audit 保证档(逐条保证判读,见 watch_state.audit_guarantee;与抽检车道 audit_*
    # 无关)。保证档不按判读积压背压抬取(源端滚动缓冲淘汰=真丢,宁可 spool 涨),唯一
    # 停抬边界是磁盘水位:spool 文件超上限或磁盘剩余不足即停抬(积压留在源端,告警),
    # 绝不丢已入队的。0=不设该边界。──
    guarantee_spool_max_mb: int = 2048
    guarantee_min_disk_free_mb: int = 1024
    # 单次 Audit 模型交付的机械输入上限。它只限制完整记录组成的批次体积，既不决定
    # 记录真假，也不截断单条记录。默认值来自正式 MiniMax-M2.7 的 30K/45K/60K
    # 基线：30K/45K 能完整稳定返回，60K 在要求大输出时会触及单轮输出上限；
    # 因此默认取已完整通过的 45K。不同部署可按实测能力调整。
    guarantee_batch_max_tokens: int = 45000


_INT_FIELDS = {
    "window_seconds": (30, 86400),
    "bucket_seconds": (5, 3600),
    "low_cardinality_limit": (4, 256),
    "rare_threshold": (1, 100),
    "value_rare_threshold": (0, 100),
    "value_min_support": (8, 100000),
    "value_max_candidates_per_pull": (0, 50),
    "head_value_rare_pct": (0, 50),
    "head_value_max_candidates_per_pull": (0, 50),
    "head_low_cardinality_limit": (4, 256),
    "frequent_hit_investigate_pct": (0, 90),
    "spec_max_candidates_per_pull": (1, 50),
    "max_candidates_per_pull": (1, 50),
    "full_read_per_pull": (0, 500),
    "max_suppressed_groups_listed": (4, 100),
    # 一条本身仍是合法页：HTTP 整页超过传输上限时，游标拉取器会按完整记录数
    # 减半并可降到 1；limit=1 仍过大才明确失败，绝不截断记录。
    "page_limit": (1, 500),
    "max_events_per_pull": (100, 200000),
    "max_wait_cap_seconds": (0, 55),
    "poll_query_seconds": (5, 86400),
    "background_harvest": (0, 1),
    "harvester_idle_stop_seconds": (0, 86400),
    "harvest_chunk_events": (50, 20000),
    "spool_backpressure_factor": (0, 64),
    "capacity_alert_backlog_records": (1, 100000000),
    "capacity_alert_oldest_seconds": (1, 31536000),
    "capacity_alert_cooldown_seconds": (30, 86400),
    "audit_sample_per_pull": (0, 16),
    "audit_tilt_per_pull": (0, 32),
    "audit_sample_per_minute": (0, 600),
    "audit_tilt_per_minute": (0, 1200),
    "audit_floor_per_minute": (0, 60),
    "feedback_max_candidates_per_pull": (0, 50),
    "feedback_feature_window_cap": (0, 1000),
    "feedback_retire_min_lifted": (8, 100000),
    "guarantee_spool_max_mb": (0, 1048576),
    "guarantee_min_disk_free_mb": (0, 1048576),
    "guarantee_batch_max_tokens": (1000, 200000),
}


def tuning_from_params(params: dict[str, object]) -> IngestTuning:
    """从工具参数取覆盖值,越界钳到边界;没给的用默认。"""
    values: dict[str, object] = {}
    for field in fields(IngestTuning):
        bounds = _INT_FIELDS.get(field.name)
        if bounds is None:
            continue
        parsed = _clamped_int(params.get(field.name), bounds[0], bounds[1])
        if parsed is not None:
            values[field.name] = parsed
    return IngestTuning(**values)  # type: ignore[arg-type]


def _clamped_int(value: object, low: int, high: int) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(low, min(high, parsed))


__all__ = ["IngestTuning", "tuning_from_params"]
