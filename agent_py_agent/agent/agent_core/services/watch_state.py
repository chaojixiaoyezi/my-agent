
from __future__ import annotations

from dataclasses import dataclass, field

from ..orchestration.dispatch.no_progress import DispatchNoProgressTracker


@dataclass(frozen=True)
class WatchCycleResult:
    record: object | None
    store_record: bool
    had_progress: bool
    stop_watch: bool = False


@dataclass
class WatchLoopState:
    last_dispatch_had_changes: bool = False
    idle_record_written: bool = False
    no_progress_tracker: DispatchNoProgressTracker = field(default_factory=DispatchNoProgressTracker)
