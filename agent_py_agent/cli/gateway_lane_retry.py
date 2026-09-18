# LLM: 这里只保存有界、进程内的 owner/thread 失败退避；持久唤醒、Goal 与执行权仍由原会话存储管理。
# 模块用途: 分清等待模型配置与普通错误冷却，避免后台重复抢工位；不新增调度器、模型调用或落盘状态。

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..agent.settings.thread_model_selection import is_model_configuration_unavailable


# LLM: 不保存异常正文、配置或密钥；模型依赖不靠计时解除，普通错误仍按单调时钟冷却。
# 类用途: 描述一条失败后台车道下一次何时、满足什么条件可以再尝试。
@dataclass(frozen=True)
class _LaneFailure:
    retry_at: float
    waiting_for_model: bool


# LLM: worker 写入和 planner 读取共用短锁；配置读取在锁外，不能堵住其它用户或正在慢流的模型。
# 类用途: 管理后台失败冷却，成功清除、owner 淘汰清除；重启后从原持久队列重新判断。
class BackgroundLaneRetry:
    # LLM: 最多保留 1024 条临时失败记录，容量不影响持久任务及其恢复资格。
    # 函数用途: 初始化单个 Gateway supervisor 的退避记录及并发保护。
    def __init__(self) -> None:
        self._failures: dict[tuple[str, str], _LaneFailure] = {}
        self._lock = threading.Lock()

    # LLM: 只按异常类型分类；远端 4xx/临时断线不等于本地没有模型，保持原普通冷却语义。
    # 函数用途: 失败后记住这条车道需要等配置，还是等一段冷却时间。
    def failed(self, owner: str, thread_id: str, exc: Exception, *, delay: float) -> None:
        waiting = is_model_configuration_unavailable(exc)
        row = _LaneFailure(0.0 if waiting else time.monotonic() + max(0.0, delay), waiting)
        with self._lock:
            key = (owner, thread_id)
            if key not in self._failures and len(self._failures) >= 1024:
                self._failures.pop(next(iter(self._failures)))
            self._failures[key] = row

    # LLM: model_ready 必须从当前会话的 canonical 模型引用读取，不构造后端、不请求网络、不切换默认模型。
    # 函数用途: 冷却期跳过；缺模型时等待用户配置好该会话，随后立即恢复原工作。
    def ready(self, owner: str, thread_id: str, *, model_ready: Callable[[], bool]) -> bool:
        with self._lock:
            row = self._failures.get((owner, thread_id))
        if row is None:
            return True
        if time.monotonic() < row.retry_at:
            return False
        return not row.waiting_for_model or model_ready()

    # LLM: 成功只清除精确车道，不影响其它线程的冷却或持久唤醒。
    # 函数用途: 后台工作片成功返回后去掉旧失败标记。
    def succeeded(self, owner: str, thread_id: str) -> None:
        with self._lock:
            self._failures.pop((owner, thread_id), None)

    # LLM: 只补偿当前 owner 的候选数量，避免被跳过的旧会话遮住健康会话。
    # 函数用途: 给调度规划提供冷却条目数，不泄露错误或配置内容。
    def count(self, owner: str) -> int:
        with self._lock:
            return sum(key[0] == owner for key in self._failures)

    # LLM: 只丢弃已离开本 supervisor 的 owner 临时投影；不能改原事件和任务。
    # 函数用途: owner 缓存淘汰时一并回收退避记录。
    def retain_owners(self, owners: Iterable[str]) -> None:
        retained = set(owners)
        with self._lock:
            self._failures = {key: row for key, row in self._failures.items() if key[0] in retained}
