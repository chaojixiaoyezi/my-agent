# LLM: 每对会话（发送 thread, 接收 thread）每小时消息条数的结构化计数。只按两个 thread id 分桶与时间窗口
#   计数，绝不解析消息正文、任务正文或模型输出；窗口是固定长度的小时桶，跨窗口自动归零。
#   调用方分两类：模型发起的发送/派发先 count 再按配置上限决定是否拒绝；宿主自动回报只 record 计入配额，
#   不被拒绝（回报不是模型请求，拒绝只会丢信息，但必须占用配额，避免回报绕过限额）。
# 模块用途: 为会话间消息与派活的每对会话限额提供可单测、可核对的持久计数。
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..gateway_parts.io import read_json_file_report, update_json_file_atomic
from .store_io import safe_file_stem

# 窗口长度（秒）：一小时，与配置键 session_pair_hourly_limit 的语义一致。
WINDOW_SECONDS = 3600.0


# LLM: 冻结值对象；只承载两个 thread id 与窗口起点，不做 IO、不判断上限。
# 类用途: 描述一个会话对在某个时间窗口里的计数。
@dataclass(frozen=True)
class PairWindow:
    sender_thread_id: str
    target_thread_id: str
    window_start: float
    count: int


# LLM: 只接收 storage；不读配置、不发请求、不持有会话对象。上限由调用方按配置传入。
# 类用途: 记录与读取每对会话在当前小时窗口内的消息条数。
class SessionPairRateLimiter:
    # LLM: 计数文件放在 canonical storage 根下，与其它会话领域共用同一持久位置。
    # 函数用途: 绑定 canonical 存储目录。
    def __init__(self, storage: object) -> None:
        self.storage = storage

    # LLM: 只读计数，不写入；窗口已过期按 0 返回。纯查询，供调用方在真正写入前判断。
    # 函数用途: 返回这一对会话在当前窗口内已记录的条数。
    def count(self, sender_thread_id: str, target_thread_id: str, *, now: float | None = None) -> int:
        window = self._load(sender_thread_id, target_thread_id, now)
        return window.count if window is not None else 0

    # LLM: 先读窗口再原子 +1：跨窗口时把计数重置为 1，窗口内累加。读坏计数文件按新窗口处理，
    #   不让坏文件永久卡住这一对会话（计数是防滥用节流，不是权威业务账本）。
    # 函数用途: 记一条消息并返回记录后的窗口内条数。
    def record(self, sender_thread_id: str, target_thread_id: str, *, now: float | None = None) -> int:
        current = time.time() if now is None else float(now)
        start = _window_start(current)
        path = self._path(sender_thread_id, target_thread_id)
        if path is None:
            return 0

        # LLM: 锁内重读再累加，避免两个并发写入方互相覆盖计数。
        # 函数用途: 在原子更新内把窗口计数加一。
        def updater(data: dict[str, object]) -> dict[str, object]:
            previous = _count_of(data, start)
            return {"schema_version": "session_pair_rate.v1", "window_start": start, "count": previous + 1}

        updated = update_json_file_atomic(path, updater)
        return int(updated.get("count") or 0)

    # LLM: 超限判定只按结构化计数与调用方传入的上限；limit<=0 表示不限制（与配置语义一致）。
    # 函数用途: 判断这一对会话是否已经用满本窗口配额。
    def over_limit(
        self, sender_thread_id: str, target_thread_id: str, limit: int, *, now: float | None = None
    ) -> bool:
        if int(limit or 0) <= 0:
            return False
        return self.count(sender_thread_id, target_thread_id, now=now) >= int(limit)

    # LLM: 目录不存在时按"没有计数"处理；读坏或字段异常同样按新窗口，不把读坏当超限。
    # 函数用途: 读取这一对会话在当前窗口内的计数，跨窗口或缺失时返回 None。
    def _load(self, sender_thread_id: str, target_thread_id: str, now: float | None) -> PairWindow | None:
        path = self._path(sender_thread_id, target_thread_id)
        if path is None or not path.exists():
            return None
        report = read_json_file_report(path, context="conversation.session_pair_rate.read")
        if report.load_error is not None or not report.payload:
            return None
        start = _window_start(time.time() if now is None else float(now))
        if float(report.payload.get("window_start") or 0.0) != start:
            return None
        return PairWindow(
            sender_thread_id=str(sender_thread_id or ""),
            target_thread_id=str(target_thread_id or ""),
            window_start=start,
            count=int(report.payload.get("count") or 0),
        )

    # LLM: 两个 thread id 都必须非空才能分桶；空值返回 None，调用方按"不计数"处理而不是编造一个桶。
    # 函数用途: 返回这一对会话的计数文件路径。
    def _path(self, sender_thread_id: str, target_thread_id: str) -> Path | None:
        sender = str(sender_thread_id or "").strip()
        target = str(target_thread_id or "").strip()
        if not sender or not target:
            return None
        root = Path(self.storage.session_pair_rate_dir)
        return root / f"{safe_file_stem(sender)}.{safe_file_stem(target)}.json"


# LLM: 窗口起点是时间戳向下取整到固定长度，只由当前时间决定，不读正文也不看历史。
# 函数用途: 返回给定时刻所属小时窗口的起点。
def _window_start(now: float) -> float:
    return float(int(now // WINDOW_SECONDS) * WINDOW_SECONDS)


# LLM: 计数文件里的 window_start 与当前窗口一致时沿用旧值，否则归零重数。
# 函数用途: 从计数文件内容里取出当前窗口的有效计数。
def _count_of(data: dict[str, object], window_start: float) -> int:
    if float(data.get("window_start") or 0.0) != window_start:
        return 0
    return int(data.get("count") or 0)


# LLM: 计数是节流不是权威账本，所以 store 上没有这个组件时按"不限制/不计数"处理，绝不因为缺组件而拒绝业务。
# 函数用途: 取当前会话存储上的每对会话计数器；没有该组件时返回 None。
def pair_rate_limiter(store: object) -> SessionPairRateLimiter | None:
    limiter = getattr(store, "session_pair_rate", None)
    return limiter if isinstance(limiter, SessionPairRateLimiter) else None


# LLM: 判定"这一对会话是否已经用满本小时配额"。limit<=0 或没有计数器时一律放行（0 = 不限制）。
#   只读计数，不写入；调用方在真正投递前调用，避免被拒的消息占用配额。
# 函数用途: 判断一次新的会话间消息是否超出每对会话限额。
def pair_limit_reached(
    store: object, sender_thread_id: str, target_thread_id: str, limit: int
) -> bool:
    limiter = pair_rate_limiter(store)
    if limiter is None:
        return False
    return limiter.over_limit(sender_thread_id, target_thread_id, limit)


# LLM: 真正投递成功（或幂等重放）后记一条。回报消息也走这里计入配额，但调用方不因超限拒绝回报——
#   回报是宿主发出的结构化结果，丢掉它只会让发送方永远看不到任务结束。
# 函数用途: 记一条会话间消息到这一对会话的当前小时计数。
def record_pair_message(store: object, sender_thread_id: str, target_thread_id: str) -> int:
    limiter = pair_rate_limiter(store)
    if limiter is None:
        return 0
    try:
        return limiter.record(sender_thread_id, target_thread_id)
    except (OSError, ValueError, TypeError):
        # 计数写失败不影响已经投递成功的消息；节流是附加能力。
        return 0


__all__ = [
    "PairWindow",
    "SessionPairRateLimiter",
    "WINDOW_SECONDS",
    "pair_limit_reached",
    "pair_rate_limiter",
    "record_pair_message",
]
