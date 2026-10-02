# LLM: Gateway 后台循环共用的"连续出错退避 + 打印限流"小工具：派发循环（_guarded_dispatch_tick）与后台主循环
#   （_supervisor_tick_survives）各持一份实例。语义固定：连续第 k 次失败后等 min(base·2^(k-1), cap) 秒，成功一次清零；
#   同一种错误（context + 异常类型）只打第一次，之后每第 print_every 次打一次，成功后重新计。只算节奏，不记账、不打印、
#   不落盘——次数仍由 loop_health 记，打印仍由 _print_gateway_loop_error 做。改动须同步 test_gateway_dispatcher_resilience.py。
# 模块用途: 防止 tick 持续出错时每 0.2 秒打一条错误日志把磁盘再写满（2026-09-28 事故链的后半段）。
from __future__ import annotations

# 连续出错的退避：0.2 秒起步翻倍，封顶 30 秒；打印限流：同种错误第 1 次打，之后每 10 次打 1 次。
LOOP_ERROR_BACKOFF_BASE_SECONDS = 0.2
# 网关主循环连续出错的退避封顶 30 秒
LOOP_ERROR_BACKOFF_CAP_SECONDS = 30.0
# 同种错误第 1 次打印，之后每 10 次打 1 次，限流刷屏
LOOP_ERROR_PRINT_EVERY_COUNT = 10


# LLM: 单线程使用（每个循环线程自己一份），不加锁；delay() 只读当前连续次数，record_failure/record_success 改状态。
#   错误种类的键由调用方给（context + 异常类型名），本类不看异常正文。
# 类用途: 记住一个循环最近连续失败了几次、每种错误已出现几次，据此给出下一拍等多久、这次要不要打印。
class LoopErrorBackoff:
    # LLM: 三个参数都做了下限保护（base≥0、cap≥base、print_every≥1），调用方传坏值也不会出现负等待或除零。
    # 函数用途: 建一份空状态；参数只在测试里改，生产用模块常量。
    def __init__(
        self,
        *,
        base_seconds: float = LOOP_ERROR_BACKOFF_BASE_SECONDS,
        cap_seconds: float = LOOP_ERROR_BACKOFF_CAP_SECONDS,
        print_every: int = LOOP_ERROR_PRINT_EVERY_COUNT,
    ) -> None:
        self._base = max(0.0, float(base_seconds))
        self._cap = max(self._base, float(cap_seconds))
        self._print_every = max(1, int(print_every))
        self._consecutive = 0
        self._seen: dict[str, int] = {}

    # LLM: 返回值就是"这次要不要打印"的裁决；调用方不得自己再数。连续次数无上界，delay 用 cap 封顶。
    # 函数用途: 记一次失败，返回这种错误这次该不该打印。
    def record_failure(self, kind: str) -> bool:
        self._consecutive += 1
        count = self._seen.get(kind, 0) + 1
        self._seen[kind] = count
        return count == 1 or count % self._print_every == 0

    # LLM: 成功既清连续计数也清"已见错误"表：下一段故障的第一条一定会打出来。
    # 函数用途: 记一次成功，退避与限流都归零。
    def record_success(self) -> None:
        self._consecutive = 0
        self._seen.clear()

    # LLM: 纯函数式读取，不改状态；第 k 次连续失败对应 base·2^(k-1)，用 cap 封顶，调用方把它直接交给 stop_event.wait。
    # 函数用途: 当前该等多久；没有连续失败时是 0。
    def delay(self) -> float:
        if self._consecutive <= 0:
            return 0.0
        return min(self._base * (2 ** (self._consecutive - 1)), self._cap)

    # LLM: 只读投影，不是账本；真实错误次数以 loop_health 为准，这里只反映当前退避档位。
    # 函数用途: 当前连续失败次数（测试与状态展示用）。
    @property
    def consecutive_failures(self) -> int:
        return self._consecutive
