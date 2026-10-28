# LLM: 程序计时独立于模型正文；单调钟算耗时，受阻/未完成不等于成功，未知不能变零，重复收口冻结首次事实。
# 模块用途: 管理终端请求时钟与首次收口事实；纯格式化归会话层，保留既有CLI导入合同。
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..agent.conversation.turn_timing import (
    format_completion_line as format_completion_line,
)
from ..agent.conversation.turn_timing import (
    format_turn_completion_line as format_turn_completion_line,
)
from ..agent.conversation.turn_timing import (
    format_turn_duration as format_turn_duration,
)
from ..agent.conversation.turn_timing import (
    render_turn_timing as render_turn_timing,
)


# LLM: 纯展示事实，不授予业务成功或请求已送达；elapsed_seconds=None 表示不能证明开始时间。
# 类用途: 保存一个逻辑请求的完成时间与耗时来源。
@dataclass(frozen=True)
class TurnCompletion:
    logical_id: str
    status: str
    completed_at: float
    elapsed_seconds: float | None
    source: str


# LLM: 创建时捕获原接受时钟，重试不新建；finish 并发幂等，受阻/未完成也冻结，不能由墙钟回拨改写耗时。
# 类用途: 给一个逻辑输入保留原开始点并冻结首个确认收口状态。
@dataclass
class TurnClock:
    logical_id: str
    started_monotonic: float | None = field(default_factory=lambda: time.monotonic())
    completion: TurnCompletion | None = None
    _lock: object = field(default_factory=threading.Lock, repr=False)

    # LLM: 仅接受五类明确收口状态，受阻/未完成不是成功；未知连接只交展示 notice，不调用 finish。
    # 函数用途: 冻结本次输入的首个确认状态、结束时间与单调耗时。
    def finish(self, status: str) -> TurnCompletion:
        if status not in {"completed", "failed", "interrupted", "blocked", "unfinished"}:
            raise ValueError("不能为未知业务状态生成完成计时")
        with self._lock:
            if self.completion is None:
                elapsed = None if self.started_monotonic is None else max(0.0, time.monotonic() - self.started_monotonic)
                self.completion = TurnCompletion(self.logical_id, status, time.time(), elapsed,
                                                 "local_monotonic" if elapsed is not None else "unknown")
            return self.completion


# LLM: 只为当前 TUI 生命周期管理计时，有界记录避免无限驻留；原 turn 身份和业务回执不改。
# 类用途: 按请求身份保存接受时钟，恢复观察缺少原时钟时明确未知。
class TurnCompletionLedger:
    # LLM: 无 IO 或线程副作用，锁只保护本地显示状态。
    # 函数用途: 初始化单会话的有界计时表。
    def __init__(self):
        self.clocks = {}
        self.lock = threading.Lock()

    # LLM: 首次入队捕获开始，重复观察不能重置；淘汰仅限最旧已完成记录。
    # 函数用途: 在输入接受时登记计时。
    def accept(self, logical_id):
        with self.lock:
            self.clocks.setdefault(logical_id, TurnClock(logical_id))
            if len(self.clocks) > 2048:
                old = next((key for key, clock in self.clocks.items() if clock.completion), None)
                if old is not None:
                    self.clocks.pop(old)

    # LLM: 无原时钟用 None，不把恢复观察开始点冒充输入开始；同身份终态保持首次结果。
    # 函数用途: 返回结构化完成事实及是否首次结算。
    def finish(self, logical_id, status):
        with self.lock:
            clock = self.clocks.setdefault(logical_id, TurnClock(logical_id, started_monotonic=None))
            first = clock.completion is None
            return clock.finish(status), first


# LLM: 只格式化非负有限耗时，短于一分钟保留一位小数；未知和非法值明确未知。
# 函数用途: 用统一中文短格式显示总耗时。
def format_elapsed(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "未知"
    if seconds < 60:
        return f"{seconds:.1f}秒"
    remaining = int(seconds)
    rows = []
    for unit, span in (("天", 86400), ("小时", 3600), ("分", 60), ("秒", 1)):
        value, remaining = divmod(remaining, span)
        if value or rows or unit == "秒":
            rows.append(f"{value}{unit}")
    return "".join(rows)


# LLM: 输入是程序结构化事实，模型正文不参与；五类收口分别标注，未知不冒充成功，本地日期带时区。
# 函数用途: 为所有终端入口渲染保留原状态的一行程序收口摘要。
def render_completion(value: TurnCompletion | dict) -> str:
    data = asdict(value) if isinstance(value, TurnCompletion) else value
    label = {
        "completed": "完成",
        "failed": "失败",
        "interrupted": "已中断",
        "blocked": "受阻",
        "unfinished": "未完成",
    }.get(data.get("status"), "状态未知")
    ended = datetime.fromtimestamp(data["completed_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"{label}于 {ended}  ·  用时 {format_elapsed(data.get('elapsed_seconds'))}"
