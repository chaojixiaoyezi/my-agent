# LLM: 程序计时独立于模型正文；单调钟算耗时，受阻/未完成不等于成功，未知不能变零，重复收口冻结首次事实。
# 模块用途: 为 run、普通聊天和 TUI 共用结束时间、耗时及区分收口状态的渲染合同。
from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime


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


# LLM: 纯展示接口，独立于旧中文 format_elapsed 合同；不得根据格式化文案推断运行终态。
# 函数用途: 将耗时显示为秒、分钟或小时的简短英文单位。
def format_turn_duration(elapsed_seconds: float) -> str:
    if not math.isfinite(elapsed_seconds):
        return "未知"
    seconds = max(0, int(round(elapsed_seconds)))
    if 0 < elapsed_seconds < 1:
        seconds = 1
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# LLM: 纯格式化给定终态和种子，不能用 Python 随机 hash 破坏跨进程回放。
# 函数用途: 生成 Claude 风格的单行回合耗时。
def format_turn_completion_line(elapsed_seconds: float, *, status: str = "done", seed_key: str = "", verb: str | None = None) -> str:
    duration = format_turn_duration(elapsed_seconds)
    if status in {"cancelled", "interrupted"}:
        return f"✻ Interrupted after {duration}"
    if status in {"failed", "error"}:
        return f"✻ Failed after {duration}"
    if status not in {"done", "succeeded", "completed"}:
        return f"✻ {status} after {duration}"
    verbs = ("Baked", "Brewed", "Churned", "Cogitated", "Cooked", "Crafted", "Crunched", "Sautéed", "Worked")
    index = int.from_bytes(hashlib.sha256(seed_key.encode("utf-8")).digest()[:8], "big") % len(verbs)
    return f"✻ {verb or verbs[index]} for {duration}"


# LLM: 默认墙钟只供实时调用；恢复必须传冻结时刻，不能用本函数给旧历史补造事实。
# 函数用途: 为成功回合统一输出Worked、耗时与带时区的完成日期。
def format_completion_line(elapsed_s: float, completed_at: object = None) -> str:
    if completed_at is None or completed_at == "":
        ended = datetime.now().astimezone()
    elif isinstance(completed_at, datetime):
        ended = completed_at.astimezone()
    elif isinstance(completed_at, str):
        ended = datetime.fromisoformat(completed_at.replace("Z", "+00:00")).astimezone()
    else:
        ended = datetime.fromtimestamp(completed_at).astimezone()
    return f"✻ Worked for {format_turn_duration(elapsed_s)} · 完成于 {ended:%Y-%m-%d %H:%M:%S %Z}"


# LLM: 结构化展示必须有可转换的冻结时标；非法历史返回空串而非补now，纯实时formatter仍支持默认墙钟。
# 函数用途: 在 TUI 消息尾标显示稳定动词、耗时与原完成时刻。
def render_turn_timing(data: dict) -> str:
    ended = data.get("completed_at")
    if type(ended) not in (int, float):
        return ""
    try:
        datetime.fromtimestamp(ended).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    seconds = data.get("elapsed_seconds")
    if data.get("status") == "completed":
        return format_completion_line(seconds if seconds is not None else float("nan"), data["completed_at"])
    line = format_turn_completion_line(seconds if seconds is not None else float("nan"),
        status=str(data.get("status") or "unknown"), seed_key=str(data.get("logical_id") or ""))
    label = {"completed": "完成于", "failed": "失败于", "interrupted": "已中断于",
             "blocked": "受阻于", "unfinished": "未完成于"}.get(data.get("status"), "记录于")
    ended_text = datetime.fromtimestamp(ended).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"{line} · {label} {ended_text}"
