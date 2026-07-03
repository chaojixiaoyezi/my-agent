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


_INT_FIELDS = {
    "window_seconds": (30, 86400),
    "bucket_seconds": (5, 3600),
    "low_cardinality_limit": (4, 256),
    "rare_threshold": (1, 100),
    "max_candidates_per_pull": (1, 50),
    "max_suppressed_groups_listed": (4, 100),
    "page_limit": (10, 500),
    "max_events_per_pull": (100, 200000),
    "max_wait_cap_seconds": (0, 55),
    "background_harvest": (0, 1),
    "harvester_idle_stop_seconds": (0, 86400),
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
