# LLM: 这里只保存有界、进程内的 owner/thread 失败退避；持久唤醒、Goal 与执行权仍由原会话存储管理。
#   三类失败：等模型配置（不靠计时，模型配好即放行）；环境暂停（backends.errors.is_provider_environment_fault 判定的环境级故障，
#   加上只在车道这层显式并入的额度用完，额度用完按会话层唯一判定 is_provider_quota_failure，含压缩调用撞额度的包装；
#   会话模型指纹变了立即放行，否则到探测时刻放行一次真实尝试，间隔 60 秒起翻倍封顶 900 秒）；
#   其余按调用方给的时长冷却。
#   分类只看异常类型和结构化状态码；状态不落盘，重启即清。环境暂停与恢复各打一行 [gateway-lane-retry]。
# 模块用途: 分清等待模型配置、等待环境恢复与普通错误冷却，避免后台对坏掉的环境反复发请求；不新增调度器、模型调用或落盘状态。

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from ..agent.backends.errors import (
    is_provider_environment_fault,
    provider_error_http_status,
)
from ..agent.conversation.compact_guard import is_provider_quota_failure
from ..agent.gateway_parts.loop_health import loop_health
from ..agent.settings.thread_model_selection import is_model_configuration_unavailable

# 环境级故障暂停后的探测间隔：首次 60 秒，每次探测再失败翻倍，封顶 900 秒。安全兜底用内部常量，不进配置。
LANE_ENVIRONMENT_PROBE_BASE_SECONDS = 60.0
# 环境级故障探测间隔封顶 900 秒：翻倍退避到上限后不再增长
LANE_ENVIRONMENT_PROBE_MAX_SECONDS = 900.0


# LLM: 不保存异常正文、配置或密钥；模型依赖不靠计时解除，普通错误仍按单调时钟冷却。
#   环境暂停时 retry_at 是下一次探测时刻，probe_interval 是本轮间隔（探测再失败据此翻倍），fingerprint 是暂停后
#   第一次就绪检查读到的模型指纹（None 表示还没读到）；指纹是进程盐 HMAC，不可反查凭据。
# 类用途: 描述一条失败后台车道下一次何时、满足什么条件可以再尝试。
@dataclass(frozen=True)
class _LaneFailure:
    retry_at: float
    waiting_for_model: bool
    waiting_for_environment: bool = False
    probe_interval: float = 0.0
    fingerprint: str | None = None


# LLM: worker 写入和 planner 读取共用短锁；配置与指纹读取在锁外，不能堵住其它用户或正在慢流的模型。
# 类用途: 管理后台失败冷却与环境暂停，成功清除、owner 淘汰清除；重启后从原持久队列重新判断。
class BackgroundLaneRetry:
    # LLM: 最多保留 1024 条临时失败记录，容量不影响持久任务及其恢复资格。
    # 函数用途: 初始化单个 Gateway supervisor 的退避记录及并发保护。
    def __init__(self) -> None:
        self._failures: dict[tuple[str, str], _LaneFailure] = {}
        self._lock = threading.Lock()

    # LLM: 分类顺序固定：先判等模型配置（ModelNotConfiguredError 也命中环境级判定，必须先走这条），再判环境级故障或额度用完，
    #   其余按 delay 冷却；远端 400/413 这类请求问题、临时断线都不是环境故障，保持普通冷却。环境暂停打一行日志。
    # 函数用途: 失败后记住这条车道要等配置、等环境恢复，还是等一段冷却时间。
    def failed(self, owner: str, thread_id: str, exc: Exception, *, delay: float) -> None:
        key = (owner, thread_id)
        with self._lock:
            row = _next_failure(self._failures.get(key), exc, delay)
            if key not in self._failures and len(self._failures) >= 1024:
                self._failures.pop(next(iter(self._failures)))
            self._failures[key] = row
        if row.waiting_for_environment:
            _log_lane_event("lane_environment_paused", key, {
                "probe_in_seconds": row.probe_interval,
                "error_type": type(exc).__name__,
                "http_status": provider_error_http_status(exc),
            })

    # LLM: model_ready 与 fingerprint 必须从当前会话的 canonical 模型引用只读计算，不构造后端、不请求网络、不切换默认模型，
    #   两者都在锁外调用；fingerprint 为 None 或读取出错时环境暂停只按探测时刻放行（Gateway 规划必须传入）。
    # 函数用途: 冷却期跳过；缺模型时等用户配置好该会话；环境故障时等模型指纹变化或探测时刻。
    def ready(
        self,
        owner: str,
        thread_id: str,
        *,
        model_ready: Callable[[], bool],
        fingerprint: Callable[[], str] | None = None,
    ) -> bool:
        with self._lock:
            row = self._failures.get((owner, thread_id))
        if row is None:
            return True
        if row.waiting_for_environment:
            return self._environment_ready((owner, thread_id), row, fingerprint)
        if time.monotonic() < row.retry_at:
            return False
        return not row.waiting_for_model or model_ready()

    # LLM: 指纹在锁外读取，读不到按未知处理（见 _read_fingerprint）；锁内只在记录仍是同一条时推进：第一次检查记下基线；
    #   指纹变了删除暂停、打恢复日志并放行（之后再失败从 60 秒重新计）；到探测时刻放行但保留记录，探测再失败由 failed
    #   翻倍，成功由 succeeded 清除。记录期间被别处改掉时本轮不放行，交给下一次规划重新判断。
    # 函数用途: 判断一条因环境故障暂停的车道现在能不能放行。
    def _environment_ready(
        self,
        key: tuple[str, str],
        row: _LaneFailure,
        fingerprint: Callable[[], str] | None,
    ) -> bool:
        current = _read_fingerprint(fingerprint)
        with self._lock:
            if self._failures.get(key) is not row:
                return False
            changed = current is not None and row.fingerprint is not None and current != row.fingerprint
            if changed:
                self._failures.pop(key)
            if current is not None and row.fingerprint is None:
                self._failures[key] = replace(row, fingerprint=current)
        if changed:
            _log_lane_event("lane_environment_resumed", key, {"reason": "model_fingerprint_changed"})
            return True
        return time.monotonic() >= row.retry_at

    # LLM: 成功只清除精确车道，不影响其它线程的冷却或持久唤醒；清掉的是环境暂停时（只可能是探测那次成功）打一行恢复日志。
    # 函数用途: 后台工作片成功返回后去掉旧失败标记。
    def succeeded(self, owner: str, thread_id: str) -> None:
        with self._lock:
            row = self._failures.pop((owner, thread_id), None)
        if row is not None and row.waiting_for_environment:
            _log_lane_event("lane_environment_resumed", (owner, thread_id), {"reason": "probe_succeeded"})

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


# LLM: 调用方持锁；只按异常类型和结构化状态码分类，顺序见 BackgroundLaneRetry.failed。额度用完（3a 2026-09-29 裁定）在这里
#   显式并入环境暂停：重试同一额度不会恢复，换模型或额度重置后由指纹或探测放行；is_provider_environment_fault 本身不含额度
#   （毒丸也用它，额度在毒丸那边按瞬时类不计数）。额度用完只读会话层唯一判定 compact_guard.is_provider_quota_failure，
#   大线程压缩调用撞额度的包装也算。持久策略失败账排除同一组（background_claim._counts_as_policy_failure）。
#   只有紧接在上一轮环境暂停之后才翻倍（探测失败），否则从 60 秒起；不读异常正文。
# 函数用途: 根据上一条记录和本次异常算出这条车道新的失败记录。
def _next_failure(previous: _LaneFailure | None, exc: Exception, delay: float) -> _LaneFailure:
    now = time.monotonic()
    if is_model_configuration_unavailable(exc):
        return _LaneFailure(0.0, True)
    if not (is_provider_environment_fault(exc) or is_provider_quota_failure(exc)):
        return _LaneFailure(now + max(0.0, delay), False)
    interval = LANE_ENVIRONMENT_PROBE_BASE_SECONDS
    if previous is not None and previous.waiting_for_environment:
        interval = min(previous.probe_interval * 2, LANE_ENVIRONMENT_PROBE_MAX_SECONDS)
    return _LaneFailure(now + interval, False, waiting_for_environment=True, probe_interval=interval)


# LLM: 指纹只是"提前放行"的信号：没有读取入口或读取抛任何 Exception（OSError、DataCorruptionError、ModelProfileError 等）都当
#   未知，只按探测时刻放行，暂停和翻倍保持（9a 复审）；否则异常会经 Gateway 记成新失败，把环境暂停冲成普通冷却或等模型配置、
#   翻倍从头算。真实问题会在探测那次真实尝试里按原异常暴露。BaseException 不吞。
# 函数用途: 读取会话模型指纹，读不到时返回 None。
def _read_fingerprint(fingerprint: Callable[[], str] | None) -> str | None:
    if fingerprint is None:
        return None
    try:
        return fingerprint()
    except Exception:
        return None


# LLM: 与供应退避日志同一形态（前缀 + 排序 JSON + flush）；只含 owner 标签、thread_id、原因、间隔、异常类名和状态码，
#   不含异常正文或配置。打印失败（典型是磁盘写满）只记 loop_health，不能改变车道状态或让调用方的循环线程退出。
# 函数用途: 打一行车道环境暂停或恢复日志。
def _log_lane_event(event: str, key: tuple[str, str], facts: dict[str, object]) -> None:
    try:
        body = json.dumps({"event": event, "owner": key[0], "thread_id": key[1], **facts}, ensure_ascii=False, sort_keys=True)
        print(f"[gateway-lane-retry] {body}", flush=True)
    except Exception as exc:
        loop_health.note_print_failure("gateway_lane_retry", exc)
