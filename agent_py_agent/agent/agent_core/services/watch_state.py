# LLM: Watch state DTOs shared by watch_service without pulling in dispatch side effects.
# 模块用途: 保存 watch 循环的轻量状态和单轮结果，避免主服务文件继续膨胀。

from __future__ import annotations

from dataclasses import dataclass, field

from ..dispatch_no_progress import DispatchNoProgressTracker


# LLM: WatchCycleResult separates heartbeat ticks from durable audit records.
# 类用途: 返回单轮 watch 的记录、是否落审计、是否有真实进展和是否要退出循环。
@dataclass(frozen=True)
class WatchCycleResult:
    record: object | None
    store_record: bool
    had_progress: bool
    stop_watch: bool = False


# LLM: WatchLoopState keeps mutable watch-loop facts out of parameter bundles.
# 类用途: 保存上一轮是否有真实进展、是否已经写过 idle 记录，以及 no-progress 追踪器。
@dataclass
class WatchLoopState:
    last_dispatch_had_changes: bool = False
    idle_record_written: bool = False
    no_progress_tracker: DispatchNoProgressTracker = field(default_factory=DispatchNoProgressTracker)
