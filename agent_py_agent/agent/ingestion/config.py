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
    # outside_normal 洪泛免疫(真机实锤:模型学常态清单漏列一个高频取值 → "常态之外"
    # 把该取值整条车道刷满,82+ 误报还把稀疏真目标淹没在重判批里):常态之外命中的取值
    # 若在窗口内高频出现(计数 > max(value_rare_threshold, 字段样本量*pct/100)),
    # 不抬 spec 候选、按常规形状进被压组账目(有示例可抽查,不静默丢)。高频=常态的
    # 结构化定义,与 configure 拒高频 target 同一原理。target 点名命中不受影响。0=关。
    outside_normal_common_value_pct: int = 2
    # 点名 target 配反免疫(P2 真机实锤:模型 sample→configure 把常态高频取值配成 target,
    # 两道 configure 闸在"没 sample+窗口没热"的双盲区放行 → 引擎照判据整批抬常态、模型照报,
    # 2h 26 误报;上报侧无任何结构兜底):target_value/target_contains 命中的【具体取值】若在
    # 窗口内占字段样本量比例 >= 该值(且样本量 >= value_min_support),不抬 spec 候选、按常态
    # 压组 + 结构化告警(digest.spec_target_common,payload 提示重 sample+configure)。
    # 阈值故意远高于 configure 闸的 2%(护栏:离线台真目标密度 5-8% 绝不能碰;真目标事故
    # 尖峰突破 25% 才暂压、回落自愈)——"高频=常态"同一结构化定义,只是执行点在运行时。0=关。
    spec_target_common_value_pct: int = 25
    # per-源判据 spec 车道独立名额(学出来的精准判据,绝不能被通用车道诱饵挤掉;
    # spec.max_per_pull>0 时以 spec 为准)。
    spec_max_candidates_per_pull: int = 8
    # 每次 pull 返回给模型的候选上限;超出的进 overflow 账目(带坐标,不静默丢)。
    max_candidates_per_pull: int = 8
    # 批摘要里列出的被压组上限(按窗口计数降序)。
    max_suppressed_groups_listed: int = 24
    # 游标拉取:单页 limit 与单次 pull 处理事件总数上限(背压:超出留给下次,如实报滞后)。
    page_limit: int = 400
    max_events_per_pull: int = 20000
    # pull 长轮询:块内每次重拉源的间隔与等待上限(秒)。
    poll_interval_seconds: float = 1.5
    max_wait_cap_seconds: int = 55
    # 后台连续摄取(harvester):1=开(模型研判期间照样拉流,源端滚动缓冲不淘汰漏),0=关
    # (回到 pull 块内拉流的旧行为)。
    background_harvest: int = 1
    # 无窗长守时,多久没人 pull 消费就自停收割线程(0=不自停;有窗时按窗口+余量自停)。
    harvester_idle_stop_seconds: int = 1800
    # 收割喂引擎的分片大小:冷启动/断点追赶会一次 drain 上万条积压,若整批一次 process,
    # 稀有候选会挤爆"每批候选上限"进 overflow(真机实锤:12421 条一批,91 达标挤 8 位,
    # 真命中落 overflow 模型看不见)。按片喂,候选位随积压量线性扩,稳态(每拍几百条)不受影响。
    harvest_chunk_events: int = 500
    # ── 摄取召回三件套(B2 抽检 / B3 反馈学习 / B4 倾斜;真机实锤:判 0 误报但筛只把
    # 26% 喂给模型、一个源 0/177 全瞎——很多真目标语义上真、结构上和常态一样)──
    # 抽检车道(常态流分层抽样喂模型,撞结构筛盲区):每次 process 的名额上限;0=关。
    audit_sample_per_pull: int = 2
    # 盲区证据成立(抽检确认过/反馈车道在抬/筛长期零抬升)时的倾斜名额上限。
    audit_tilt_per_pull: int = 6
    # 抽检允额(每分钟,独立滑窗):base 档与倾斜档——有界=负载固定,不挤占判力。
    audit_sample_per_minute: int = 6
    audit_tilt_per_minute: int = 24
    # 反馈车道(模型确认真目标的结构特征回灌,同特征事件自动抬升)独立名额;0=关。
    feedback_max_candidates_per_pull: int = 8
    # 每特征每窗口的抬升上限(洪泛闸:特征若配到常态取值,最多污染这么多判力)。
    feedback_feature_window_cap: int = 24
    # 特征自动退休线:实抬 >= 此数仍只有注册那一次确认且已过 stale 窗 → 停抬(新确认复活)。
    feedback_retire_min_lifted: int = 64


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
    "outside_normal_common_value_pct": (0, 50),
    "spec_target_common_value_pct": (0, 90),
    "spec_max_candidates_per_pull": (1, 50),
    "max_candidates_per_pull": (1, 50),
    "max_suppressed_groups_listed": (4, 100),
    "page_limit": (10, 500),
    "max_events_per_pull": (100, 200000),
    "max_wait_cap_seconds": (0, 55),
    "background_harvest": (0, 1),
    "harvester_idle_stop_seconds": (0, 86400),
    "harvest_chunk_events": (50, 20000),
    "audit_sample_per_pull": (0, 16),
    "audit_tilt_per_pull": (0, 32),
    "audit_sample_per_minute": (0, 600),
    "audit_tilt_per_minute": (0, 1200),
    "feedback_max_candidates_per_pull": (0, 50),
    "feedback_feature_window_cap": (0, 1000),
    "feedback_retire_min_lifted": (8, 100000),
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
