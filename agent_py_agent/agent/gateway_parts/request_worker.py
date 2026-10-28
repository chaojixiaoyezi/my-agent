# LLM: 原审批债务及其真实outer异常链只同对象运输和严格核收；finish在原native活except内执行，普通同码不升级，物理未知优先，不以清理完成签发终态或释放资源。
# LLM: 本模块负责 Gateway 请求入队、claim 和 worker 调度；文件 payload 的会话身份与客户端能力必须保持结构化且可审计。
#   pending缓存仅复用原字节确认过的读取副本，不授claim/更新权；terminal等待按单调钟预算退避，不缓存时间决策。
# 原runner Thread只凭native异常/Context原锚点观察同claim未知，无Call id；失活后只保留既存诊断，不新增终态或释放。
#   owner 解析另认已用管理员密码绑定的 IM 私聊，判据只有结构化渠道身份与绑定表的精确匹配。
#   SQLite复用仅在已解析owner的请求范围内启用，结束时回收，不改变独立事务和终态提交。
#   正count派发只消费原宿主owner和原P3准入/claim来源，后台pending仅重排同claim且保P3；Count0原公平链保持。
#   Call/心跳停止未知只在活原claim记录既有unknown；退出后保留原P3，不从marker/晚到异常签发新来源或终态。
# 模块用途: 接收 Gateway 请求、写入队列并驱动后台 worker 可靠处理。

from __future__ import annotations

"""Request execution and handling for gateway.

Only normalized ordinary prompts and typed task-command payloads may enter the
file queue. Any slash command that bypasses HTTP/CLI dispatch fails closed before
the worker creates a model turn.
"""

import copy
import json
import os
import stat
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..concurrency.interrupt import register_interruptible
from ..conversation.control_commands import (
    conversation_request_interrupt_name,
)
from ..io.sqlite_scope import sqlite_connection_scope
from ..observability.concurrency_metrics import (
    gateway_admission_blocked_set,
    gateway_inflight,
    gateway_worker_busy,
    record_gateway_queue_wait,
)
from ..runtime_errors import DataCorruptionError
from .io import (
    append_gateway_history_once,
    gateway_response_path,
    read_json_file,
    read_json_file_report,
    update_json_file_atomic,
    write_json_file_atomic,
)
from .paths import GatewayPaths, gateway_chunk_path
from .queue_service import (
    GatewayClaim,
    archive_request,
    claim_request,
    ensure_gateway_folders,
)
from .recovery import (
    GATEWAY_SAFE_RESTART_CAUSE,
    _gateway_request_attempts,
    gateway_terminal_projection_folder,
)
from .request_binding import gateway_turn_transition
from .request_client import GatewayAskParams, submit_gateway_ask
from .request_errors import (
    gateway_owner_scope_error_response,
    gateway_request_identity_error_response,
    gateway_request_load_error_response,
    gateway_request_processing_state_error_response,
    gateway_unhandled_worker_error_response,
)
from .request_execution import _handle_gateway_request
from .request_owner_binding import REQUEST_OWNER_KEY
from .response_renderer import read_gateway_terminal_response_file

if TYPE_CHECKING:
    from ...core import SimpleAgent


_DEFAULT_READ_JSON_FILE_REPORT = read_json_file_report
_DEFAULT_PATH_READ_BYTES = Path.read_bytes
_PENDING_SCAN_CACHE: OrderedDict[tuple[str, int, int, int, int, int, bytes], dict[str, Any]] = OrderedDict()
_PENDING_SCAN_CACHE_LOCK = threading.Lock()
# 同时限制条目和保留的原JSON字节，避免多个队列或文件版本使缓存无界增长。
_PENDING_SCAN_CACHE_MAX_ENTRIES = 1024
# pending队列扫描缓存允许占用的最大总字节数
_PENDING_SCAN_CACHE_MAX_BYTES = 4 * 1024 * 1024
# 单个pending请求文件允许进入扫描缓存的最大字节数
_PENDING_SCAN_CACHE_MAX_FILE_BYTES = 65536
_WAIT_POLL_STEPS_SECONDS: tuple[float, ...] = (0.02, 0.05, 0.1, 0.2)


@dataclass(frozen=True)
class _ClaimedGatewayRequestContext:
    agent: SimpleAgent
    processing_path: Path
    request_payload: dict
    request_id: str
    worker_id: str


@dataclass(frozen=True)
class _PendingGatewayRequest:
    path: Path
    payload: dict


# LLM: mtime 在扫描**之前**取样（should_scan 里），record_scan 记的就是这份扫描前的值。请求先写 .tmp 再 rename，
#   rename 若落在 glob 之后、record_scan 之前，目录 mtime 已晚于扫描前取样，下一轮 should_scan 一比对就重扫；
#   扫描后取样会把这次 rename 记成"已见过"，一轮超过 2 秒粗粒度保护窗口时该文件被永久跳过（2026-09-28 生产事故的
#   第二个隐患）。扫描前后取样不一致也强制下一轮重扫。改动须同步 test_gateway_dispatcher_resilience.py 与
#   test_gateway_two_tier_admission.py。
# 类用途: inbox 空闲扫描门，目录没变且上轮为空时跳过 glob，省掉空闲时的目录遍历。
class GatewayInboxScanGate:
    """inbox 空闲扫描门：目录 mtime 没变且上轮扫描为空时跳过 glob。

    粗粒度文件系统（mtime 秒级）保护：mtime 距今不足 2 秒时不跳过，
    避免同一秒内新写入的请求被漏掉。上轮存在 deferred（not_before_at）
    请求时也不跳过，保证延迟请求到点被处理。"""

    _COARSE_MTIME_GUARD_SECONDS = 2.0

    # LLM: 三个字段的唯一定义处：_last_mtime_ns 是"已见过"基线，_scan_started_mtime_ns 是本轮扫描前样本，
    #   _scan_required 初始为 True 保证新门第一轮必扫。每个派发者一份，不跨线程共享。
    # 函数用途: 建一个新门，第一轮一定扫描。
    def __init__(self) -> None:
        self._last_mtime_ns: int | None = None
        self._scan_started_mtime_ns: int | None = None
        self._scan_required = True

    # LLM: 每次调用都先取样并保存（哪怕这轮不扫），record_scan 只认这份样本；返回 True 的三种情况按顺序：上轮要求重扫、
    #   目录不可读、mtime 变了；都不满足才看粗粒度保护窗口。取样不能挪到 glob 之后。
    # 函数用途: 判断这一轮要不要 glob；总是先把当前 mtime 记为"本轮扫描前的样本"。
    def should_scan(self, inbox: Path) -> bool:
        mtime_ns = self._inbox_mtime_ns(inbox)
        self._scan_started_mtime_ns = mtime_ns
        if self._scan_required or mtime_ns is None or mtime_ns != self._last_mtime_ns:
            return True
        return (time.time() - mtime_ns / 1e9) < self._COARSE_MTIME_GUARD_SECONDS

    # LLM: 基线是 should_scan 存的扫描前样本，不是现在的 mtime；这里再 stat 一次只为判"扫描期间变过"。没取过样
    #   （None）按需要重扫处理，宁多扫不漏扫。processed/deferred_present 语义不变：有活或有延迟请求下轮必扫。
    # 函数用途: 记录本轮结果；以扫描前样本为"已见过"的基线，扫描期间目录又变了就强制下一轮重扫。
    def record_scan(self, inbox: Path, *, processed: int, deferred_present: bool) -> None:
        self._last_mtime_ns = self._scan_started_mtime_ns
        changed_during_scan = self._inbox_mtime_ns(inbox) != self._last_mtime_ns
        self._scan_required = bool(
            processed or deferred_present or changed_during_scan or self._last_mtime_ns is None
        )

    @staticmethod
    def _inbox_mtime_ns(inbox: Path) -> int | None:
        try:
            return inbox.stat().st_mtime_ns
        except OSError:
            return None


def _decrement_admission_counter(counters: dict[str, int], key: str) -> None:
    remaining = counters.get(key, 0) - 1
    if remaining > 0:
        counters[key] = remaining
    else:
        counters.pop(key, None)


# LLM: Admission precedes claim; distinct-user capacity is backpressure, not execution or owner-routing authority.
# 类用途: 在会话、用户、全局及活跃用户容量上做非阻塞准入记账。
class GatewayAdmission:
    """三层在飞记账:同会话顺序执行 + 每用户小坑 + 全局大坑。

    admission 在认领(claim)之前判:超限的请求留在 pending 排队(文件队列天然
    背压,不拒不崩),坑一空下轮派发扫描立即补位。单用户小坑防独吞饿死别人,
    全局大坑是总天花板。同一会话只放行一条,保证后一轮一定能看见前一轮已落库
    的 user/assistant 历史；同一用户的不同会话仍可并行。计数只在本进程网关
    派发路径动,claim 的原子性保证不会双记。"""

    # LLM: Zero preserves existing callers; dispatch can supply the configured owner-pool limit per scan.
    # 函数用途: 初始化线程安全的在途计数及可选owner容量。
    def __init__(self, owner_limit: int = 0) -> None:
        self._lock = threading.Lock()
        self._owner_limit = max(0, int(owner_limit))
        self._per_user: dict[str, int] = {}
        self._per_conversation: dict[str, int] = {}
        self._total = 0

    def try_acquire(
        self,
        user_key: str,
        user_limit: int,
        global_limit: int,
        *,
        conversation_key: str = "",
    ) -> bool:
        return self.try_acquire_report(
            user_key,
            user_limit,
            global_limit,
            conversation_key=conversation_key,
        ) == ""

    # LLM: 准入失败必须给出**结构化原因**(哪一层限流),排队等待信号与诊断都只认这个原因码,
    # 不从文案或调用顺序推断。返回空串=已获取。
    # 函数用途: 尝试获取准入并返回容量或会话忙原因，不在池内等待。
    def try_acquire_report(
        self,
        user_key: str,
        user_limit: int,
        global_limit: int,
        *,
        conversation_key: str = "",
        owner_limit: int | None = None,
    ) -> str:
        with self._lock:
            if self._total >= max(1, global_limit):
                return "global_inflight"
            if self._per_user.get(user_key, 0) >= max(1, user_limit):
                return "user_inflight"
            capacity = self._owner_limit if owner_limit is None else owner_limit
            if capacity > 0 and not self._per_user.get(user_key, 0) and len(self._per_user) >= capacity:
                return "owner_capacity"
            if conversation_key and self._per_conversation.get(conversation_key, 0) >= 1:
                return "conversation_busy"
            self._per_user[user_key] = self._per_user.get(user_key, 0) + 1
            if conversation_key:
                self._per_conversation[conversation_key] = (
                    self._per_conversation.get(conversation_key, 0) + 1
                )
            self._total += 1
        gateway_inflight(1)
        return ""

    def release(self, user_key: str, *, conversation_key: str = "") -> None:
        with self._lock:
            if self._per_user.get(user_key, 0) <= 0:
                return
            if conversation_key and self._per_conversation.get(conversation_key, 0) <= 0:
                return
            _decrement_admission_counter(self._per_user, user_key)
            if conversation_key:
                _decrement_admission_counter(self._per_conversation, conversation_key)
            self._total = max(0, self._total - 1)
        gateway_inflight(-1)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total": self._total,
                "per_user": dict(self._per_user),
                "per_conversation": dict(self._per_conversation),
            }


admission = GatewayAdmission()


def request_user_key(payload: dict) -> str:
    """限流记账用的用户键:顶层 user_id → conversation.canonical_user_id → anonymous。
    与 /ask 写入路径(http_handlers._build_ask_request)的字段对齐。"""
    user_id = str((payload or {}).get("user_id") or "").strip()
    if user_id:
        return user_id
    conversation = (payload or {}).get("conversation")
    if isinstance(conversation, dict):
        canonical = str(conversation.get("canonical_user_id") or "").strip()
        if canonical:
            return canonical
    return "anonymous"


# LLM: 顺序键与准入会话投影共用原三元组，保持既有分隔符和strip/回退字节，不读线程或物化owner。
# 函数用途: 返回原会话顺序键，无外部会话时保持空键。
def request_conversation_key(payload: dict) -> str:
    return "\x1f".join(request_conversation_parts(payload))


# LLM: 仅从当前结构化会话提取原顺序键边界；准入不得拆已拼接的字符串，也不得加载远端线程补身份。
# 函数用途: 让顺序调度与新准入摘要复用同一套用户、渠道和外部会话字段。
def request_conversation_parts(payload: dict) -> tuple[str, ...]:
    conversation = (payload or {}).get("conversation")
    if not isinstance(conversation, dict):
        return ()
    channel_conversation_id = str(conversation.get("channel_conversation_id") or "").strip()
    if not channel_conversation_id:
        return ()
    channel = str(conversation.get("channel") or "gateway").strip() or "gateway"
    canonical_user_id = str(conversation.get("canonical_user_id") or "").strip()
    channel_user_id = str(conversation.get("channel_user_id") or "").strip()
    user_key = canonical_user_id or channel_user_id or request_user_key(payload)
    return user_key, channel, channel_conversation_id


# LLM: 合法排队的结构化信号。请求因为准入限流(或恢复退避)留在 inbox 时,worker 每次扫描都
# 会在**请求文件本身**写一次 admission_wait_at,证明"这个队列条目此刻仍在合法等待准入"。
# 它只写等待事实:不写 status/lease_heartbeat_at/lease_started_at/模型字段,因此不会被任何
# 消费方误读成"已经认领/模型在推进"。
#
# 这里**没有**累计等待上限:健康网关只要还在证明"仍在合法等待",等待就不该因为总时长被判失败
# (慢模型/长回合占用车道是正常工作状态)。收口只来自权威事实:网关停写→客户端按自己的空闲窗口
# 收口;取消→队列条目消失→同样收口;权威终态→读到终态记录;崩溃恢复→新网关继续写,真死则停写。
# 写入只做节流(见 _ADMISSION_WAIT_REFRESH_SECONDS),不做"到点停止续期"——那会把前台报失败与
# 后台照常执行同时留在系统里。
_ADMISSION_WAIT_REFRESH_SECONDS = 30.0
_ADMISSION_WAIT_FIELDS = ("admission_wait_at", "admission_wait_reason", "admission_wait_count")


# LLM: Existing public config supplies the owner cap; zero in manually constructed limits preserves legacy components.
# 类用途: 保存当前扫描使用的准入上限，不新增用户配置键。
@dataclass(frozen=True)
class AdmissionLimits:
    user_inflight: int
    global_inflight: int
    owner_inflight: int = 0

    # LLM: Reuse owner_agent_pool_max_agents; invalid/non-positive values follow the pool's existing default 64.
    # 函数用途: 从现有配置读取用户、全局及owner池容量。
    @classmethod
    def from_config(cls, config: object) -> AdmissionLimits:
        return cls(
            user_inflight=_positive_int(getattr(config, "gateway_user_inflight_limit", 8), 8),
            global_inflight=_positive_int(
                getattr(config, "gateway_global_inflight_limit", 500), 500
            ),
            owner_inflight=_positive_int(getattr(config, "owner_agent_pool_max_agents", 64), 64),
        )


def _positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


# LLM: hold_reason 非空时（Gateway 安全重启排空）整轮只给每个待处理请求记一次结构化等待事实、不认领，
# 请求留给接班进程；等待事实让客户端按活动续期，不把长时间排空误判为超时。改动须同步 test_gateway_safe_restart.py。
# 函数用途: 按两层限流认领待处理请求并提交执行；排空时只记录等待。
def dispatch_pending_requests(
    paths: GatewayPaths,
    limits: AdmissionLimits,
    submit: Callable[[GatewayClaim, str, str], None],
    scan_gate: GatewayInboxScanGate | None = None,
    hold_reason: str = "",
    admission_gate: object = None,
) -> int:
    """两层限流的派发扫描:按 (recovery, created_at) 顺序认领在限内的请求并交给 submit;
    超限的留在 pending(排队),被限流数入探针。返回本轮认领数。

    公平性:单用户小坑满后扫描继续走到后面用户的请求——先到先服务但不许独吞;
    坑释放后下一轮扫描按同一顺序补位,任何用户至多再等一个空位周期,不会饿死。
    内部多进程模式由 admission_gate 接管准入(持久预留+盖章);默认 None 时进程内两层限流一字不变。"""
    ensure_gateway_folders(paths)
    if hold_reason:
        for request in _iter_pending_requests(paths):
            _record_admission_wait(request, reason=hold_reason, paths=paths if admission_gate is not None else None, admission_gate=admission_gate)
        return 0
    if scan_gate is not None and not scan_gate.should_scan(paths.inbox):
        return 0
    claimed = 0
    blocked = 0
    deferred_present = False
    for request in _iter_pending_requests(paths):
        if _request_deferred_until_later(request):
            deferred_present = True
            # 恢复退避也是合法排队:同样发结构化等待信号(原因码区分),客户端不按超时误判。
            _record_admission_wait(request, reason="retry_backoff", paths=paths if admission_gate is not None else None, admission_gate=admission_gate)
            continue
        outcome = _admit_and_submit(paths, request, (limits, submit, admission_gate))
        claimed += 1 if outcome == "claimed" else 0
        blocked += 1 if outcome == "blocked" else 0
    if scan_gate is not None:
        # 被限流的请求在 inbox 不产生新 mtime:blocked>0 时视同"还有活",强制下轮重扫补位。
        scan_gate.record_scan(
            paths.inbox, processed=claimed, deferred_present=deferred_present or blocked > 0
        )
    gateway_admission_blocked_set(blocked)
    return claimed



# LLM: 按原持久gate分流；Count0在claim前增加配置的活跃用户容量，C/W仍不借内存slot或submit失败清P3。
# 函数用途: 让原派发算法消费对应的唯一准入门，不新增排队状态。
def _admit_and_submit(
    paths: GatewayPaths,
    request: _PendingGatewayRequest,
    lane: tuple[AdmissionLimits, Callable[[GatewayClaim, str, str], None], object],
) -> str:
    gate = lane[2] if len(lane) > 2 else None
    if gate is not None:
        return _admit_and_submit_durable(paths, request, lane)
    limits, submit = lane[0], lane[1]
    user_key = request_user_key(request.payload)
    conversation_key = request_conversation_key(request.payload)
    blocked_reason = admission.try_acquire_report(
        user_key,
        limits.user_inflight,
        limits.global_inflight,
        conversation_key=conversation_key,
        owner_limit=limits.owner_inflight or None,
    )
    if blocked_reason:
        # 合法排队:把"仍在等准入"这一事实写成结构化字段,客户端据此续期而不是误判超时。
        _record_admission_wait(request, reason=blocked_reason)
        return "blocked"
    claim = claim_request(paths, request.path)
    if claim is None:
        admission.release(user_key, conversation_key=conversation_key)  # 别的扫描抢先认领了:坑退回
        return "raced"
    try:
        submit(claim, user_key, conversation_key)
    except Exception:
        # 提交失败不能漏坑;请求留在 processing,由恢复扫描收尸。
        admission.release(user_key, conversation_key=conversation_key)
        raise
    return "claimed"



# LLM: 内存快照只可省略同原因节流写；真正写入仍沿原T→JSON重读/CAS，不能用扫描正文覆盖当前身份。
# 只按节流窗口决定是否再写一次,不按累计等待时长停止:停止续期等价于"前台报失败、后台照常执行",
# 那是隐式语义。权威收口由取消/终态/真实失活负责。
# 函数用途: 在被准入限流的排队请求上记录一次结构化"合法等待"信号(只做节流,不做累计上限)。
def _record_admission_wait(request: _PendingGatewayRequest, *, reason: str, error_code: str = "", paths: GatewayPaths | None = None, admission_gate=None) -> None:
    fields = {
        "admission_wait_at": time.time(),
        "admission_wait_reason": str(reason or ""),
    }
    if paths is not None:
        fields["admission_wait_error_code"] = str(error_code or "")
    if isinstance(request.payload, dict) and request.payload and not _current_admission_wait_fields(request.payload, request.path, fields):
        return
    _write_admission_wait_field(request.path, fields, expected_payload=request.payload, paths=paths, admission_gate=admission_gate)



# 函数用途: 判断请求是否已经带有进队顺序时间戳(created_at/submitted_at/requeued_at)。
def _has_queue_order_timestamp(payload: dict) -> bool:
    for key in ("created_at", "submitted_at", "requeued_at"):
        try:
            if float(payload.get(key) or 0.0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


# 函数用途: 等待起点=首次记录的时刻,缺失时回退请求进队时刻(created_at/submitted_at)。
def _admission_wait_since(payload: dict, request_path: Path) -> float:
    for key in ("admission_wait_since", "created_at", "submitted_at"):
        try:
            value = float(payload.get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    try:
        return request_path.stat().st_mtime
    except OSError:
        return 0.0


# LLM: 内部C显式传冻结paths及原gate，原T→controller→lease在JSON之前；退出C不再写等待。默认0仍只原JSON，已认领/移动/取消/终态不能复活。
# 函数用途: 锁内复读当前排队记录再写等待，失败仅放弃信号，不产生执行许可。
def _write_admission_wait_field(request_path: Path, fields: dict[str, object], *, expected_payload: dict | None = None, paths: GatewayPaths | None = None, admission_gate=None) -> bool:
    identity_keys = ("id", "request_id", "kind", "user_id", REQUEST_OWNER_KEY, "owner_id", "metadata", "route", "execution_attempt_id", "lease", "cancel_requested")
    # LLM: 原JSON锁内再次核完整归属和claim未动；只增等候投影，不能借旧扫描覆盖新身份或复活取消。
    # 函数用途: 从当前同一排队记录计算节流后等待事实。
    def apply(current: dict) -> dict:
        if not current or current.get("cancel_requested") or current.get("execution_attempt_id"):
            raise ValueError("queue request is unreadable or cancelled")
        if expected_payload is not None and any(current.get(key) != expected_payload.get(key) for key in identity_keys):
            raise ValueError("queue request identity or claim changed")
        signal = {key: fields[key] for key in ("admission_wait_at", "admission_wait_reason", "admission_wait_error_code") if key in fields}
        return {**current, **_current_admission_wait_fields(current, request_path, signal)}

    try:
        if paths is None:
            update_json_file_atomic(request_path, apply, require_existing=True)
            return True
        if request_path.parent != paths.inbox:
            return False
        with gateway_turn_transition(paths, request_path.stem):
            if any((folder / request_path.name).exists() for folder in (paths.processing, paths.terminal, paths.done, paths.failed)):
                return False
            from .multiprocess_admission import _runtime_commit_scope

            if admission_gate is None or admission_gate.paths is not paths:
                return False
            with _runtime_commit_scope(admission_gate) as rejection:
                if rejection is not None:
                    return False
                update_json_file_atomic(request_path, apply, require_existing=True)
        return True
    except (OSError, ValueError):
        return False



# LLM: 一次等待只选一次时钟，优先monotonic；旧测试的SimpleNamespace缺该属性或为None时保持time回退。
# 函数用途: 返回本次终态等待使用的时钟函数，避免循环中混用时钟。
def _wait_clock() -> Callable[[], float]:
    return getattr(time, "monotonic", None) or time.time


# LLM: 只读canonical终态，projection不能结束等待；首次先读、每轮睡眠截到剩余预算，步长封顶200ms。
# 函数用途: 按20/50/100/200ms退避等待唯一终态，读到即返回，预算耗尽返回空。
def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    path = paths.terminal / f"{request_id}.json"
    clock = _wait_clock()
    deadline = clock() + max(0.0, float(timeout))
    step = 0
    while True:
        payload = read_gateway_terminal_response_file(
            path,
            request_id=request_id,
            context="gateway.worker.terminal.read",
        )
        if payload:
            return payload
        remaining = deadline - clock()
        if remaining <= 0.0:
            return {}
        delay = _WAIT_POLL_STEPS_SECONDS[min(step, len(_WAIT_POLL_STEPS_SECONDS) - 1)]
        step += 1
        time.sleep(min(delay, remaining))


def _process_gateway_requests(
    agent: SimpleAgent,
    paths: GatewayPaths,
    *,
    worker_id: str = "gw-worker",
    scan_gate: GatewayInboxScanGate | None = None,
) -> int:
    ensure_gateway_folders(paths)
    if scan_gate is not None and not scan_gate.should_scan(paths.inbox):
        return 0
    processed = 0
    deferred_present = False
    for request in _iter_pending_requests(paths):
        if _request_deferred_until_later(request):
            deferred_present = True
            continue
        if _process_gateway_request_path(agent, paths, request.path, worker_id):
            processed += 1
    if scan_gate is not None:
        scan_gate.record_scan(paths.inbox, processed=processed, deferred_present=deferred_present)
    return processed


def _iter_pending_request_paths(paths: GatewayPaths) -> list[Path]:
    return [entry.path for entry in _iter_pending_requests(paths)]


# LLM: 仅只读lstat检查伴随.lock是否为常规文件（或不存在），不创建文件、不阻塞FIFO、不跟随符号链接。
# 函数用途: 校验请求伴随锁文件未被替换为符号链接或管道。
def _lock_sidecar_is_safe(request_path: Path) -> bool:
    lock_path = request_path.with_name(f"{request_path.name}.lock")
    try:
        return stat.S_ISREG(os.lstat(lock_path).st_mode)
    except FileNotFoundError:
        return True
    except OSError:
        return False


# LLM: 单FD O_NONBLOCK|O_NOFOLLOW读取原字节与fstat并核对伴随.lock常规文件身份，不阻塞FIFO或绕过锁路径拒绝。
# 函数用途: 对不超过64KiB的常规请求文件取代次与精确字节，其他情况交原reader处理。
def _pending_scan_cache_key(request_path: Path) -> tuple[str, int, int, int, int, int, bytes] | None:
    if not _lock_sidecar_is_safe(request_path):
        return None
    fd = -1
    try:
        if Path.read_bytes is not _DEFAULT_PATH_READ_BYTES:
            st = request_path.lstat()
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= _PENDING_SCAN_CACHE_MAX_FILE_BYTES:
                return None
            raw = request_path.read_bytes()
        else:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            fd = os.open(request_path, flags)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= _PENDING_SCAN_CACHE_MAX_FILE_BYTES:
                return None
            chunks: list[bytes] = []
            remaining = _PENDING_SCAN_CACHE_MAX_FILE_BYTES + 1
            while remaining > 0 and (chunk := os.read(fd, min(65536, remaining))):
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(raw) != st.st_size:
        return None
    return (str(request_path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, raw)


# LLM: 原reader后确认字节与JSON值/类型；拒绝True==1和旧快照。确认的ValueError（含整数位限）/递归失败只弃缓存，深拷贝在锁外。
# 函数用途: 尽力保存可信读取的独立副本，按LRU双预算淘汰，确认失败不影响原读取结果。
def _cache_verified_pending_payload(request_path: Path, payload: dict[str, Any]) -> None:
    key = _pending_scan_cache_key(request_path)
    if key is None:
        return
    try:
        verified = json.loads(key[-1].decode("utf-8"))
        if verified != payload or json.dumps(verified, sort_keys=True) != json.dumps(payload, sort_keys=True):
            return
        saved = copy.deepcopy(payload)
    except (ValueError, RecursionError):
        return
    with _PENDING_SCAN_CACHE_LOCK:
        _PENDING_SCAN_CACHE[key] = saved
        _PENDING_SCAN_CACHE.move_to_end(key)
        cached_bytes = sum(len(entry[-1]) for entry in _PENDING_SCAN_CACHE)
        while _PENDING_SCAN_CACHE and (len(_PENDING_SCAN_CACHE) > _PENDING_SCAN_CACHE_MAX_ENTRIES
                                      or cached_bytes > _PENDING_SCAN_CACHE_MAX_BYTES):
            oldest, _payload = _PENDING_SCAN_CACHE.popitem(last=False)
            cached_bytes -= len(oldest[-1])


# LLM: 被替换的reader完整旁路；热命中在锁外深拷贝且再次核对伴随.lock安全性，返回副本永不暴露缓存嵌套对象。
# 函数用途: 加速扫描/排序的payload读取，未命中或探针失败仍沿原文件锁和错误报告读取。
def _read_pending_request_payload(request_path: Path, *, context: str) -> dict[str, Any]:
    if read_json_file_report is not _DEFAULT_READ_JSON_FILE_REPORT:
        return read_json_file_report(request_path, context=context).payload
    key = _pending_scan_cache_key(request_path)
    if key is not None:
        with _PENDING_SCAN_CACHE_LOCK:
            cached = _PENDING_SCAN_CACHE.get(key)
            if cached is not None:
                _PENDING_SCAN_CACHE.move_to_end(key)
        if cached is not None:
            if _lock_sidecar_is_safe(request_path):
                try:
                    return copy.deepcopy(cached)
                except RecursionError:
                    pass
            with _PENDING_SCAN_CACHE_LOCK:
                _PENDING_SCAN_CACHE.pop(key, None)
    report = read_json_file_report(request_path, context=context)
    if report.load_error is None and not report.lock_busy and isinstance(report.payload, dict) and report.payload:
        _cache_verified_pending_payload(request_path, report.payload)
    return report.payload


# LLM: 只清本inbox路径分量内未见项，不按字符串前缀误伤相邻队列；锁内删除最多1024条，不做文件IO。
# 函数用途: 每轮扫描后移除已消失/认领/取消的缓存记录，不串工作区。
def _prune_pending_scan_cache(inbox: Path, seen_paths: set[str]) -> None:
    with _PENDING_SCAN_CACHE_LOCK:
        for key in list(_PENDING_SCAN_CACHE):
            if key[0] not in seen_paths and Path(key[0]).is_relative_to(inbox):
                del _PENDING_SCAN_CACHE[key]


# LLM: 缓存只加速静态payload读取，保留原排序与逐拍延迟判定；扫描完成清已消失项，claim和等待更新仍各走原锁内复读。
# 函数用途: 返回隔离的待处理请求快照，并清本队列不再存在的缓存。
def _iter_pending_requests(paths: GatewayPaths) -> list[_PendingGatewayRequest]:
    entries = []
    seen_paths = set()
    for request_path in paths.inbox.glob("*.json"):
        seen_paths.add(str(request_path))
        payload = _read_pending_request_payload(request_path, context="gateway.worker.pending_scan.read")
        entries.append(_PendingGatewayRequest(request_path, payload))
    _prune_pending_scan_cache(paths.inbox, seen_paths)
    return sorted(entries, key=_pending_request_entry_sort_key)


# LLM: 单路径排序与批量扫描共用同一精确字节缓存，原priority context、排序字段与空payload回退保持不变。
# 函数用途: 读取当前请求并计算原排队顺序，不缓存排序或时间决策。
def _pending_request_sort_key(request_path: Path) -> tuple[int, float, str]:
    payload = _read_pending_request_payload(
        request_path, context="gateway.worker.pending_priority.read"
    )
    return _pending_request_entry_sort_key(
        _PendingGatewayRequest(request_path, payload or {})
    )


# LLM: 排序只读结构化字段：安全重启接班的续跑回合最先（它们在重启前已在跑，且同会话后来的消息必须排在它后面），
# 其次新请求，最后普通崩溃恢复（防止毒请求挡住新工作）。改动须同步 test_gateway_safe_restart.py。
# 函数用途: 给待派发请求排序，决定下一轮先认领谁。
def _pending_request_entry_sort_key(request: _PendingGatewayRequest) -> tuple[int, float, str]:
    payload = request.payload or {}
    priority = str(payload.get("priority") or "").strip().lower()
    is_recovery = priority == "recovery" or bool(payload.get("requeued_at"))
    marker = payload.get("active_turn_recovery")
    cause = str(marker.get("cause") or "") if isinstance(marker, dict) else ""
    rank = 0 if is_recovery and cause == GATEWAY_SAFE_RESTART_CAUSE else (2 if is_recovery else 1)
    created_at = _request_created_at(payload, request.path)
    return (rank, created_at, request.path.name)


def _request_created_at(payload: dict, request_path: Path) -> float:
    for key in ("created_at", "submitted_at", "requeued_at"):
        try:
            value = float(payload.get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value:
            return value
    try:
        return request_path.stat().st_mtime
    except OSError:
        return 0.0


def _request_deferred_until_later(request: _PendingGatewayRequest) -> bool:
    payload = request.payload
    if not payload:
        return False
    try:
        not_before = float(payload.get("not_before_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(not_before and time.time() < not_before)


_OWNER_POOL_LOCK = threading.Lock()
_BASE_OWNER_CHANNELS = frozenset({"local", "cli", "chat", "gateway-cli", "http"})
_LOCAL_MAIN_USER_IDS = frozenset({"local-agent"})
# IM 管理员身份只接受 adapter 明确标成一对一私聊的结构化 chat_type；缺失或群聊一律不匹配。
_ADMIN_CHANNEL_CHAT_TYPES = frozenset({"p2p", "private"})


class OwnerScopeUnavailableError(RuntimeError):
    """远程用户请求无法建立隔离 owner 时 fail-closed，禁止回退到共享 main agent。"""

    error_code = "OWNER_SCOPE_UNAVAILABLE"


# LLM: 把同一次已读权威队列记录传给拒绝门，避免门内重读或另推队列根。
# 类用途: 保存一次路由拒绝所需的调用上下文。
@dataclass(frozen=True)
class _WorkerRouteCheck:
    agent: object
    paths: GatewayPaths
    path: Path
    payload: dict



# LLM: C/W上下文均由原执行组合根提供，默认单进程为空；拒绝后不得解析新owner或降级main。
# 函数用途: 在创建owner Agent前核验冻结归属，单进程保持原有解析。
def _frozen_worker_owner(agent, request_payload: dict):
    from ..user_space.owner_resolver import OwnerIdentity
    from .multiprocess_mode import current_worker_partition
    from .multiprocess_route import validate_worker_route

    partition = getattr(agent, "_gateway_worker_partition", None) or current_worker_partition()
    if partition is None:
        from .multiprocess_coordinator_execution import (
            check_coordinator_transition,
            current_coordinator_execution,
        )
        from .multiprocess_dispatch import WorkerDispatchError

        try:
            return check_coordinator_transition(current_coordinator_execution(), request_payload)
        except WorkerDispatchError as error:
            raise OwnerRouteMismatchError(error.reason) from error
    result = validate_worker_route(request_payload, partition)
    if not result["ok"]:
        raise OwnerRouteMismatchError(result["reason"])
    return OwnerIdentity(*result["owner"])



# LLM: C/W只能消费原execution和冻结路由，拒绝后不能热解析owner；普通Count0仍使用main唯一hostowner与原owner池。
# 函数用途: 为普通请求解析并取得当前实际 owner-scoped Agent。
def _resolve_request_agent(agent, request_payload: dict):
    """按请求 owner 取作用域 agent(多用户飞书 per-用户隔离)。

    本机/单 owner 请求没有远程 channel 身份时走基础 agent。开启后:飞书用户 A/B 各跑在自己
    owner 作用域的 agent 上,home/记忆/数据/成本/审计天然隔离；远程身份缺失或 owner agent
    建立失败必须 fail-closed，绝不回退到共享 main agent 串户。
    """
    from .multiprocess_execution import check_worker_transition, current_worker_execution

    execution = current_worker_execution()
    if execution is not None:
        check_worker_transition(execution, request_payload)
        return execution.agent
    owner = _frozen_worker_owner(agent, request_payload) or _resolve_request_owner_identity(agent, request_payload)
    return _resolve_request_agent_for_owner(agent, owner)


# LLM: Agent与无业务Agent的C共用下方唯一解析实现；已落盘host owner先于热配置和绑定查询，不能因重放换归属。
# 函数用途: 按当前可信请求事实解析本次实际使用的唯一 owner 身份，但不创建作用域 Agent。
def _resolve_request_owner_identity(agent, request_payload: dict):
    return _resolve_request_owner_from_config(
        getattr(agent, "config", None), request_payload,
        getattr(getattr(agent, "home_paths", None), "root", None),
    )


# LLM: 首次认证入口与原C控制面共用当前main规则；冻结owner优先，C可传同次原安全读快照，默认查询不新增读取。
# 函数用途: 用可信配置和数据根解析owner，不创建业务Agent、用户目录或会话。
def _resolve_request_owner_from_config(config, request_payload: dict, home_root=None, *, binding_snapshot=None):
    from ..user_space.owner_resolver import owner_identity_from_config
    from .request_owner_binding import frozen_request_owner

    fixed = frozen_request_owner(request_payload)
    if fixed is not None:
        return fixed

    base_owner = owner_identity_from_config(config)
    if not bool(getattr(config, "gateway_per_user_owner_scoping", False)):
        return base_owner
    if _admin_channel_identity_from_config(config, home_root, request_payload, binding_snapshot=binding_snapshot) is not None:
        # 已用管理员密码绑定的 IM 私聊按本机主用户运行；精确匹配只在 base owner 为 local/main 时成立。
        return base_owner
    owner = _owner_from_request(None, request_payload)
    if owner is None:
        if _is_remote_channel_request(request_payload):
            raise OwnerScopeUnavailableError(
                "远程通道请求缺少可信 user_id/channel，已拒绝共享 owner 回退"
            )
        return base_owner
    return owner


# LLM: A persisted owner identity is machine authority. This path intentionally ignores later
# owner-routing config changes and resolves exactly that owner or fails closed.
# 函数用途: 根据已经持久化的 owner 身份取得唯一作用域 Agent，供控制回执恢复和对账。
def _resolve_request_agent_for_owner(agent, owner):
    from ..user_space.owner_resolver import owner_identity_from_config

    base_owner = owner_identity_from_config(getattr(agent, "config", None))
    if owner == base_owner:
        return agent
    _record_active_owner(agent, owner)  # 登记进共享活跃表,让后台主代理循环能逐 owner tick 叫回
    try:
        return _owner_pool(agent).get(owner)
    except Exception as exc:
        raise OwnerScopeUnavailableError(
            f"无法建立 owner 隔离 agent(provider={owner.provider}, kind={owner.owner_kind})"
        ) from exc


# LLM: Passive projections may reuse an already resident owner Agent, but must
# not create one or record a hard active-owner fact. Mutation and model work must
# continue through _resolve_request_agent_for_owner so security state is loaded.
# 函数用途: 给空闲状态查询复用已加载的用户 Agent；冷用户直接返回空，不触发完整初始化。
def _resolve_loaded_request_agent_for_owner(agent, owner):
    from ..user_space.owner_resolver import owner_identity_from_config

    base_owner = owner_identity_from_config(getattr(agent, "config", None))
    if owner == base_owner:
        return agent
    pool = getattr(agent, "_owner_pool", None)
    if pool is None:
        return None
    return pool.peek(owner)


def _is_remote_channel_request(request_payload: dict) -> bool:
    metadata = request_payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    channel = str(metadata.get("channel") or "").strip().lower()
    if not channel:
        return False
    return channel not in _BASE_OWNER_CHANNELS


def _record_active_owner(agent, owner) -> None:
    """把解析出的 scoped owner 记进共享活跃登记表(best-effort,永不因登记失败影响请求处理)。

    登记表由网关后台循环启动时挂到共享 context.agent 上并同步到各 worker agent(见 gateway_loops);
    没挂(单元测试/未接线)则静默跳过。"""
    registry = getattr(agent, "_active_owner_registry", None)
    if registry is None:
        return
    try:
        registry.record(owner)
    except Exception:
        pass


def _owner_from_request(agent, request_payload: dict):
    """从结构化通道身份构造 per-user 或 per-group owner；匿名/缺字段返回 None。

    p2p/private 使用发件 user_id；群聊使用通道提供的 chat_id。决策只认 adapter 传来的结构化
    chat_type/chat_id，不从 conversation_id 或自然语言猜测。本机 thin TUI 也可以显式携带
    ``local/user/<id>``；只有固定的 ``local-agent`` 才代表基础 ``local/main``。
    """
    meta = (
        request_payload.get("metadata") if isinstance(request_payload.get("metadata"), dict) else {}
    )
    user_id = str(request_payload.get("user_id") or meta.get("user_id") or "").strip()
    channel = str(meta.get("channel") or "").strip()
    if not user_id or user_id == "anonymous" or not channel:
        return None
    normalized_channel = channel.casefold()
    # chat/cli/http 仍是基础 owner 的不同内部入口。local 则同时承担两种结构化身份：
    # local-agent 是基础 local/main；local + 其他显式 user_id 是本机多用户 thin TUI。
    # 两者不能只按 channel 合并，否则客户端虽已投影 owner，服务端仍会把记忆、任务、
    # 会话和子代理全部写回 local/main，形成真实串户。
    if normalized_channel in _BASE_OWNER_CHANNELS and (
        normalized_channel != "local" or user_id.casefold() in _LOCAL_MAIN_USER_IDS
    ):
        return None
    from ..user_space.owner_resolver import OwnerIdentity

    chat_type = str(meta.get("channel_chat_type") or "").strip().lower()
    chat_id = str(meta.get("channel_chat_id") or "").strip()
    if chat_type not in {"", "p2p", "private"} and chat_id:
        return OwnerIdentity.provider_group(normalized_channel, chat_id)
    return OwnerIdentity.provider_user(normalized_channel, user_id)


# LLM: 只读 adapter 经认证 /ask 带来的结构化 user_id、metadata.channel、metadata.channel_chat_type；本机基础通道、
#   匿名、缺 chat_type 或群聊都返回 None。不看昵称、会话名或正文。
# 函数用途: 取出一条请求的 IM 私聊身份 (channel, user_id)，供管理员绑定匹配与 /admin 作用域检查共用。
def private_channel_identity(request_payload: dict) -> tuple[str, str] | None:
    meta = request_payload.get("metadata") if isinstance(request_payload.get("metadata"), dict) else {}
    user_id = str(request_payload.get("user_id") or meta.get("user_id") or "").strip()
    channel = str(meta.get("channel") or "").strip().casefold()
    chat_type = str(meta.get("channel_chat_type") or "").strip().lower()
    if not user_id or user_id == "anonymous" or not channel or channel in _BASE_OWNER_CHANNELS:
        return None
    if chat_type not in _ADMIN_CHANNEL_CHAT_TYPES:
        return None
    return channel, user_id


# LLM: 仅适配Agent的真实配置到同一管理员目标门，不创建Agent或读取绑定文件。
# 函数用途: 判断本 Gateway 是否允许 IM 私聊通过管理员密码成为本机管理员。
def admin_channel_identity_enabled(agent) -> bool:
    return _admin_channel_identity_enabled_for_config(getattr(agent, "config", None))


# LLM: 开关读显式配置，基础owner必须正是local/main；C与Agent不能各自推导另一管理员目标。
# 函数用途: 在不物化Agent时复用原管理员私聊启用条件。
def _admin_channel_identity_enabled_for_config(config) -> bool:
    from ..user_space.owner_resolver import OwnerIdentity, owner_identity_from_config

    if not bool(getattr(config, "admin_channel_identity_enabled", False)):
        return False
    return owner_identity_from_config(config) == OwnerIdentity.local_main()


# LLM: Agent查询与C查询共用唯一安全绑定读；此适配只传真实配置/根，不从请求或模型声明构造宿主。
# 函数用途: 返回本请求命中的管理员 IM 身份绑定；不是已绑定的管理员私聊时返回 None。
def admin_channel_identity_for_request(agent, request_payload: dict):
    return _admin_channel_identity_from_config(
        getattr(agent, "config", None), getattr(getattr(agent, "home_paths", None), "root", None), request_payload,
    )


# LLM: 只认原私聊字段及当前local/main管理员目标；C快照须是原typed只读载体，默认沿原find读取，不以角色、群或版本补权限。
# 函数用途: 供C和原Agent同源匹配管理员私聊，不创建业务目录，沿原安全读取次数与权限门。
def _admin_channel_identity_from_config(config, home_root, request_payload: dict, *, binding_snapshot=None):
    identity = private_channel_identity(request_payload) if isinstance(request_payload, dict) else None
    if identity is None or not _admin_channel_identity_enabled_for_config(config):
        return None
    if not home_root:
        return None
    from ..user_space.admin_channel_identity import (
        AdminChannelIdentitySnapshot,
        find_admin_channel_identity,
    )

    if binding_snapshot is not None:
        if type(binding_snapshot) is not AdminChannelIdentitySnapshot:
            raise ValueError("admin_binding_snapshot_required")
        return next((item for item in binding_snapshot.identities if (item.channel, item.user_id) == identity), None)

    return find_admin_channel_identity(home_root, *identity)


_ADMIN_BINDING_HINT = (
    "如果你是这台电脑的 my-agent 管理员：在与机器人的一对一私聊里发送 /admin <管理员密码>，"
    "验证后本私聊会使用管理员的模型和设置；发完请撤回那条含密码的消息。"
)


# LLM: 判据全是结构化事实：IM 私聊身份、配置开关 admin_channel_identity_enabled、全局数据根里的管理员密码文件、绑定表；不看正文。
#   调用方可能是按用户隔离的 agent（owner 池把它的 owner 字段改成了该用户），所以这里不做“基础 owner 是 local/main”的判断：
#   密码只能由 local/main 的 CLI 设置，未设密码就不提示。/admin 本就在公开命令帮助里；已绑定、群聊、未设密码或开关关闭都返回空串。
# 函数用途: 给还没绑定管理员的 IM 私聊报错补一句 /admin 指引，让管理员不用排查就知道下一步。
def admin_binding_hint_for_request(agent, request_payload: dict) -> str:
    identity = private_channel_identity(request_payload) if isinstance(request_payload, dict) else None
    if identity is None or not bool(getattr(getattr(agent, "config", None), "admin_channel_identity_enabled", False)):
        return ""
    home_root = getattr(getattr(agent, "home_paths", None), "root", None)
    if not home_root:
        return ""
    from ..user_space.admin_channel_identity import find_admin_channel_identity
    from ..user_space.admin_password import admin_password_status

    if find_admin_channel_identity(home_root, *identity) is not None:
        return ""
    return _ADMIN_BINDING_HINT if admin_password_status(home_root).get("configured") else ""


def _config_without_runtime_paths(agent):
    """克隆基础 config 但把已解析的 owner 运行时路径字段(local_store_path/memory_path 等)清空。

    基础 agent 的 __init__ 把这些字段写成了基础 owner(main)的路径,且 resolve_runtime_paths 把"已有值"
    当 override 不再重解析。若池直接克隆这份已固化的 config,作用域 agent 会沿用 main 的 local_store/记忆
    → 不隔离。清空它们,作用域 agent 才会按各自 owner 的 home 重新解析(真隔离)。my_agent_home 是输入
    不在解析字段里,不动。
    """
    import dataclasses

    fields = getattr(getattr(agent, "runtime_path_resolution", None), "paths", {}) or {}
    resets = {name: "" for name in fields if hasattr(agent.config, name)}
    return dataclasses.replace(agent.config, **resets)


def _owner_pool(agent):
    """懒建并缓存挂在基础 agent 上的 owner 池(线程安全双检;多 worker 首次并发只建一个)。"""
    pool = getattr(agent, "_owner_pool", None)
    if pool is not None:
        return pool
    with _OWNER_POOL_LOCK:
        pool = getattr(agent, "_owner_pool", None)
        if pool is None:
            from ..owner_scoped_pool import OwnerScopedAgentPool

            pool = OwnerScopedAgentPool(
                _config_without_runtime_paths(agent),
                agent.root,
                workspace_roots=getattr(agent, "workspace_roots", None),
                # Adapter daemon 是基础 Gateway 的进程级事实；scoped owner 只继承其只读健康源，
                # 自己的 registry、凭据配置和 conversation binding 仍在独立 agent 内。
                channel_runtime_health_provider=getattr(
                    agent,
                    "_channel_runtime_health_provider",
                    None,
                ),
            )
            agent._owner_pool = pool
        return pool


def _process_gateway_request_path(
    agent: SimpleAgent,
    paths: GatewayPaths,
    request_path: Path,
    worker_id: str,
) -> bool:
    claim = claim_request(paths, request_path)
    if claim is None:
        return False
    # §6-A 量化探针:worker 忙数 gauge(上限=gateway_global_inflight_limit 两层限流)。贴着这个上限跑
    # =顶层槽位饱和,超出的并发用户只能在 pending 里排队。
    gateway_worker_busy(1)
    try:
        return _process_claimed_gateway_request_path(
            agent,
            paths,
            claim.path,
            worker_id,
            claim=claim,
        )
    finally:
        gateway_worker_busy(-1)


# LLM: 原C/W冻结路由在取消/owner物化之前核；实际handler与结果落账复用同一原事务端口，SQLite/owner租用保持main区间，退出不释放P3。
# LLM: 同词法pending槽涵盖handler后收尾；已有cause保持，finish或读回失败不签接收，原Future继续持同outer债务。
# 函数用途: 处理准确已认领请求和原终态投影，短写只消费同源执行门，异常保留原claim与清理责任。
def _process_claimed_gateway_request_path(
    agent: SimpleAgent,
    paths: GatewayPaths,
    processing_path: Path,
    worker_id: str,
    *,
    claim: GatewayClaim | None = None,
) -> bool:
    request_report = read_json_file_report(processing_path, context="gateway.worker.request.read")
    if request_report.load_error is not None:
        request_id = processing_path.stem
        response = gateway_request_load_error_response(processing_path, request_report.load_error)
        _finish_claimed_gateway_request(paths, processing_path, request_id, response)
        return True
    request_payload = request_report.payload
    request_id = str(request_payload.get("id") or processing_path.stem)
    request_payload.setdefault("id", request_id)
    expected_attempt_id = (
        claim.execution_attempt_id
        if claim is not None
        else str(request_payload.get("execution_attempt_id") or "").strip()
    )
    expected_lease_epoch = (
        claim.lease_epoch if claim is not None else _request_lease_epoch(request_payload)
    )
    if claim is not None and (
        request_id != claim.request_id
        or str(request_payload.get("execution_attempt_id") or "").strip()
        != claim.execution_attempt_id
        or _request_lease_epoch(request_payload) != claim.lease_epoch
    ):
        return False
    identity_error = request_payload.get("request_identity_error")
    if isinstance(identity_error, dict):
        response = gateway_request_identity_error_response(
            processing_path,
            request_payload,
            identity_error,
            request_id=request_id,
        )
        _finish_claimed_gateway_request(
            paths,
            processing_path,
            request_id,
            response,
            expected_execution_attempt_id=expected_attempt_id,
            expected_lease_epoch=expected_lease_epoch,
        )
        return True
    if _reject_worker_route(_WorkerRouteCheck(agent, paths, processing_path, request_payload)):
        return True
    from .multiprocess_execution import check_worker_transition, current_worker_execution
    from .request_transactions import request_transaction_ports_scope

    check_worker_transition(current_worker_execution(), request_payload)
    if bool(request_payload.get("cancel_requested")):
        with (register_interruptible(conversation_request_interrupt_name(request_id)),
              request_transaction_ports_scope(agent)):
            response = _handle_gateway_request(
                agent,
                processing_path,
                refresh_lease=False,
                worker_id=worker_id,
            )
        _finish_claimed_gateway_request(
            paths,
            processing_path,
            request_id,
            response,
            conversation_store=_conversation_store_for_request(agent, request_payload),
            expected_execution_attempt_id=expected_attempt_id,
            expected_lease_epoch=expected_lease_epoch,
        )
        return True
    from .request_binding import record_gateway_request_stage

    timing = record_gateway_request_stage(processing_path, request_payload, "dispatched_at",
        at=float(request_payload.get("lease_started_at") or time.time()))
    try:
        request_agent = _resolve_request_agent(agent, request_payload)
    except OwnerScopeUnavailableError as exc:
        response = gateway_owner_scope_error_response(
            processing_path,
            request_payload,
            exc,
            request_id=request_id,
        )
        _finish_claimed_gateway_request(
            paths,
            processing_path,
            request_id,
            response,
            expected_execution_attempt_id=expected_attempt_id,
            expected_lease_epoch=expected_lease_epoch,
        )
        return True
    timing.update(record_gateway_request_stage(processing_path, request_payload, "owner_agent_ready_at"))
    pool = getattr(agent, "_owner_pool", None)
    lease = pool.pin(request_agent) if pool is not None and request_agent is not agent else nullcontext()
    from ..conversation.agent_tool_approval import (
        ApprovalRetirementUnconfirmedError,
        accept_approval_retirement_terminal,
        approval_retirement_request_observation,
    )
    from ..runtime_db.repository import request_stage_timing_scope
    from .io import gateway_request_fingerprint

    with (request_stage_timing_scope(timing), lease, register_interruptible(conversation_request_interrupt_name(request_id)),
          request_transaction_ports_scope(request_agent),
          sqlite_connection_scope(bool(getattr(agent.config, "sqlite_connection_reuse", False))),
          approval_retirement_request_observation(request_agent, processing_path, request_payload,
              gateway_request_fingerprint(request_payload, request_id), receiver=True) as retirement_observation):
        try:
            try:
                response = _process_claimed_gateway_request(
                    _ClaimedGatewayRequestContext(
                        request_agent,  # 多用户飞书:只在请求 owner 作用域的 agent 上跑；失败已在上方终态拒绝
                        processing_path,
                        request_payload,
                        request_id,
                        str(
                            (claim.lease_owner if claim is not None else "")
                            or request_payload.get("lease_owner")
                            or worker_id
                        ),
                    )
                )
            except Exception as error:
                _raise_with_pending_approval_retirement(error, retirement_observation)
            try:
                if retirement_observation.pending_error is not None:
                    raise retirement_observation.pending_error
            except ApprovalRetirementUnconfirmedError:
                _finish_claimed_gateway_request(
                    paths,
                    processing_path,
                    request_id,
                    response,
                    conversation_store=request_agent.conversation_store,
                    execution_agent=request_agent,
                    expected_execution_attempt_id=expected_attempt_id,
                    expected_lease_epoch=expected_lease_epoch,
                )
                accept_approval_retirement_terminal(retirement_observation, response, request_agent.conversation_store)
                # finish与严格读回全部成功才可核收；新的outer仍携原native，交同一Future，不重放旧业务。
                raise
            else:
                _finish_claimed_gateway_request(
                    paths,
                    processing_path,
                    request_id,
                    response,
                    conversation_store=request_agent.conversation_store,
                    execution_agent=request_agent,
                    expected_execution_attempt_id=expected_attempt_id,
                    expected_lease_epoch=expected_lease_epoch,
                )
        except Exception as error:
            from .request_errors import gateway_request_execution_unknown

            expected = (request_id, expected_attempt_id, expected_lease_epoch)
            if gateway_request_execution_unknown(error, request_payload, expected):
                try:
                    _record_execution_outcome_uncertain(paths, processing_path, error, expected)
                except Exception as observation_error:
                    from .logging import _report_gateway_side_effect_error

                    _report_gateway_side_effect_error("observe_execution_unknown", request_id, observation_error)
            raise
    return True


# LLM: 仅原仍current的词法观察可将已有native债务保到同outer；无写入/签收，不改cause、运行旧Context或授执行权。
# 函数用途: 保住handler接收后收尾失败的异常链，物理未知及已有链仍为原实际对象。
def _raise_with_pending_approval_retirement(error, observation):
    from ..conversation.agent_tool_approval import (
        ApprovalRetirementUnconfirmedError,
        approval_retirement_obligation,
        find_approval_retirement_error,
    )

    pending = observation.pending_error
    if (not observation.current() or approval_retirement_obligation(pending) is None
            or find_approval_retirement_error(error) is not None):
        raise error
    try:
        raise pending
    except ApprovalRetirementUnconfirmedError:
        # 让Python在原native活except里记录context；显式from会覆盖outer已经有的cause。
        raise error  # noqa: B904 - 保留同outer既有cause和自然context。


# LLM: 在取消/owner 解析之前调用；失败仅经现有规范终态归档写网关文件，不传 conversation_store。
# 函数用途: 把错配请求终结为结构化失败，杜绝取消分支绕过路由门。
def _reject_worker_route(check: _WorkerRouteCheck) -> bool:
    request = check.payload
    try:
        _frozen_worker_owner(check.agent, request)
    except OwnerRouteMismatchError as exc:
        response = gateway_owner_scope_error_response(check.path, request, exc, request_id=check.path.stem)
        _finish_claimed_gateway_request(
            check.paths, check.path, check.path.stem, response,
            expected_execution_attempt_id=str(request.get("execution_attempt_id") or ""),
            expected_lease_epoch=_request_lease_epoch(request),
        )
        return True
    return False



def _process_claimed_gateway_request(context: _ClaimedGatewayRequestContext) -> dict:
    from ..runtime_errors import runtime_error_report
    from .logging import _report_gateway_side_effect_error

    if (
        str(context.request_payload.get("status") or "") == "processing"
        and str(context.request_payload.get("execution_attempt_id") or "").strip()
        and _request_lease_epoch(context.request_payload) > 0
    ):
        return _handle_gateway_request(
            context.agent,
            context.processing_path,
            refresh_lease=True,
            worker_id=context.worker_id,
        )

    def mark_processing(current: dict) -> dict:
        payload = current or dict(context.request_payload)
        _mark_request_processing(payload, context.worker_id)
        return payload

    try:
        updated = update_json_file_atomic(
            context.processing_path,
            mark_processing,
            require_existing=True,
        )
        context.request_payload.clear()
        context.request_payload.update(updated)
    except OSError as exc:
        _report_gateway_side_effect_error("prepare_gateway_request_lease", context.request_id, exc)
        return gateway_request_processing_state_error_response(
            context.processing_path,
            runtime_error_report(exc, context="gateway.worker.processing_state.write"),
            request_id=context.request_id,
        )
    return _handle_gateway_request(
        context.agent,
        context.processing_path,
        refresh_lease=True,
        worker_id=context.worker_id,
    )


def _mark_request_processing(request_payload: dict, worker_id: str) -> None:
    lease_now = time.time()
    # §6-A 量化探针:进队(created_at)→被认领的等待直方图。分位一拉高=worker 槽位饿死
    # (排队),而不是认领后卡首轮——正是"solo 用户 20 分钟 0 产出"要区分的两种死法。
    # created_at 缺失(老请求/旁路生产者)兜底 submitted_at,两字段语义同为进队时刻。
    try:
        created_at = float(
            request_payload.get("created_at") or request_payload.get("submitted_at") or 0.0
        )
    except (TypeError, ValueError):
        created_at = 0.0
    if created_at > 0:
        record_gateway_queue_wait(lease_now - created_at)
    try:
        previous_epoch = max(0, int(request_payload.get("lease_epoch") or 0))
    except (TypeError, ValueError):
        previous_epoch = 0
    request_payload.update(
        {
            "status": "processing",
            "attempts": _gateway_request_attempts(request_payload) + 1,
            "lease_owner": worker_id,
            "lease_started_at": lease_now,
            "lease_heartbeat_at": lease_now,
            "lease_epoch": previous_epoch + 1,
            "execution_attempt_id": f"gateway-attempt-{uuid.uuid4().hex}",
            "updated_at": lease_now,
        }
    )


# LLM: Persisted lease epochs may come from interrupted recovery and are never inferred from text.
# 函数用途: 安全读取请求的非负执行代次，坏值按零处理。
def _request_lease_epoch(request_payload: dict) -> int:
    try:
        return max(0, int(request_payload.get("lease_epoch") or 0))
    except (TypeError, ValueError):
        return 0


# LLM: 供应商故障重排的统一出口：从响应标记读次数与错误码，调 recovery 写回队列；重排失败返回 False，
#   由调用方照常收失败终态（不让请求卡死）。身份取响应里的请求编号，与 processing 文件同源。
# 函数用途: 把带 provider_transient_resume 标记的请求重排回队列。
def _requeue_provider_transient_resume(paths, processing_path, response, conversation_store) -> bool:
    from .recovery import ProviderTransientResume, requeue_provider_transient_processing

    marker = response["provider_transient_resume"]
    return requeue_provider_transient_processing(
        paths,
        processing_path,
        str(response.get("id") or ""),
        ProviderTransientResume(
            resume_count=int(marker.get("resume_count") or 0),
            error_code=str(marker.get("error_code") or ""),
            conversation_store=conversation_store,
        ),
    )


# LLM: 重排失败退回正常归档前收口本回合仍未消费的插话：request_execution 拿到续跑标记时跳过了收口
#   （留给续跑回合认领），但重排没成功、没有续跑回合。正常归档时 terminalize 也会收口，这里显式先收，
#   覆盖归档失败（读不到请求文件、改名失败等）的形态；收口幂等（归档侧第二次为空转）。
#   身份只有 request_id（与本回合同源）；收口失败留给 recovery 兜底，不反噬终态归档。
# 函数用途: 在供应商故障重排失败退回时，按正常回合语义拒绝本回合仍挂起的补充消息。
def _settle_fallback_turn_guidance(conversation_store: object | None, request_id: str) -> None:
    recovery_api = getattr(getattr(conversation_store, "guidance", None), "recovery", None)
    reject = getattr(recovery_api, "reject_pending", None)
    if not callable(reject) or not str(request_id or "").strip():
        return
    try:
        reject(request_id, reject_reserved=True)
    except Exception:  # noqa: BLE001 收口失败不阻断终态归档，recovery 会再次处理
        pass


# LLM: 只有原后台消费者明确pending才经当前同Agent/claim短门重排，P3保留；其它响应沿main终态/重启/供应商续跑，不引入业务continuation旁路。
# 函数用途: 将已认领请求的真实结果归档，后台尚未消费的同一来源让出执行槽后原请求稍后重领。
def _finish_claimed_gateway_request(
    paths: GatewayPaths,
    processing_path: Path,
    request_id: str,
    response: dict,
    *,
    conversation_store: object | None = None,
    execution_agent: SimpleAgent | None = None,
    expected_execution_attempt_id: str = "",
    expected_lease_epoch: int = 0,
) -> None:
    response_id = str(response.get("id") or "").strip()
    if response_id != request_id:
        raise DataCorruptionError(
            "gateway terminal response identity conflicts with the claimed request"
        )
    if response.get("kind") == "background":
        from .multiprocess_background_execution import requeue_pending_background_request

        if requeue_pending_background_request(response, execution_agent):
            return
    if isinstance(response.get("provider_transient_resume"), dict):
        if _requeue_provider_transient_resume(paths, processing_path, response, conversation_store):
            # 供应商临时故障自动续跑：回合不写终态，请求重排回队列（退避与累计次数在 recovery 里写）。
            return
        # 重排失败退回正常归档：标记只服务重排判定，先清掉，不让它落进终态响应与 responses 投影。
        response.pop("provider_transient_resume", None)
        # 退回路径按正常结束的回合收口插话（#7：回合结束、插话还挂着会占住会话通道）；terminalize
        # 正常归档时也会收口，这里先收覆盖归档失败（读不到请求文件等）的形态。
        _settle_fallback_turn_guidance(conversation_store, request_id)
    if isinstance(response.get("restart_resume"), dict):
        # I4：宿主停机准入拒绝的回合不写终态，请求留在 processing，重启后由 recovery 按死进程重排续跑（见
        # request_execution._host_shutdown_resume_marker）。
        return
    target_folder = gateway_terminal_projection_folder(paths, response)
    response_path = gateway_response_path(paths, request_id)
    archive_result = archive_request(
        paths,
        processing_path,
        target_folder,
        request_id,
        conversation_store=conversation_store,
        terminal_response=response,
        expected_execution_attempt_id=expected_execution_attempt_id,
        expected_lease_epoch=expected_lease_epoch,
    )
    if not archive_result.terminal_committed:
        return
    from .recovery import (
        mark_gateway_terminal_projection_complete,
        repair_gateway_chunk_projection,
    )

    try:
        repair_gateway_chunk_projection(paths, request_id, response)
    except Exception as exc:
        from .logging import _report_gateway_side_effect_error

        _report_gateway_side_effect_error("archive_gateway_chunk_stream", request_id, exc)
    # responses 是 canonical terminal 的可修复投影；合法但陈旧的独立 JSON 也必须覆盖，
    # 不能因“文件存在”反过来成为终态权威。
    write_json_file_atomic(response_path, response)
    append_gateway_history_once(paths, response)
    try:
        mark_gateway_terminal_projection_complete(paths, request_id, response)
    except Exception as exc:
        # 完成标记只是可重建加速索引；主请求已经由 canonical terminal 封口，不能因
        # 标记写失败反过来把成功回合改成失败。后台 projector 会继续补齐。
        from .logging import _report_gateway_side_effect_error

        _report_gateway_side_effect_error("archive_gateway_terminal_marker", request_id, exc)


# LLM: 普通异常沿main终态；精确typed/Call/心跳/runner出处保processing/P3，退出后仅既存观察或保守未结，不授重放。
# LLM: 原native及真实outer链未结不得普通FAILED/finish；查链仅保原Future债务，持久UNKNOWN仍须实际producer另核。
# 函数用途: 只结清普通初始化失败，实际未结由原Future和准确canonical事实承担。
def terminalize_unhandled_claimed_gateway_request(
    paths: GatewayPaths,
    processing_path: Path,
    error: BaseException,
    *,
    agent: SimpleAgent | None = None,
    expected_execution_attempt_id: str = "",
    expected_lease_epoch: int = 0,
) -> None:
    """Fail closed when a claimed request raises before its normal handler.

    The dispatcher has already removed the request from the inbox.  Every exit
    ordinary path creates one terminal response and moves the claim out of
    ``processing``; exact typed unknown retains the original claim and P3.
    Otherwise a Python/runtime initialization error becomes a
    false hour-long running task.
    """

    from ..conversation.agent_tool_approval import find_approval_retirement_error

    if find_approval_retirement_error(error) is not None:
        return
    report = read_json_file_report(
        processing_path,
        context="gateway.worker.unhandled_request.read",
    )
    if report.load_error is not None:
        response = gateway_request_load_error_response(processing_path, report.load_error)
        request_id = processing_path.stem
    else:
        request = report.payload
        request_id = str(request.get("id") or processing_path.stem)
        from .request_errors import gateway_request_execution_unknown

        expected = (processing_path.stem, expected_execution_attempt_id, expected_lease_epoch)
        if gateway_request_execution_unknown(error, request, expected):
            _record_execution_outcome_uncertain(paths, processing_path, error, expected)
            return
        from .request_errors import (
            recorded_bounded_request_unknown,
            recorded_heartbeat_request_unknown,
            recorded_runner_request_unknown,
            request_resource_exit_unconfirmed,
        )

        if (recorded_bounded_request_unknown(error, request, expected)
                or recorded_heartbeat_request_unknown(error, request, expected)
                or recorded_runner_request_unknown(error, request, expected)):
            return
        if request_resource_exit_unconfirmed(error):
            from .logging import _report_gateway_side_effect_error

            # 来源已失效或未能原位观察时仅保守未结；不从旧Call/心跳或相同错误码补事件、终态或释放许可。
            _report_gateway_side_effect_error("unattributed_resource_execution_unknown", request_id, error)
            return
        response = gateway_unhandled_worker_error_response(
            processing_path,
            request,
            error,
            request_id=request_id,
        )
    _finish_claimed_gateway_request(
        paths,
        processing_path,
        request_id,
        response,
        conversation_store=(
            _conversation_store_for_request(agent, request)
            if agent is not None and report.load_error is None
            else None
        ),
        expected_execution_attempt_id=expected_execution_attempt_id,
        expected_lease_epoch=expected_lease_epoch,
    )


# LLM: 原T内再核typed/原Call、心跳或common唯一runner出处与活execution/claim；保持status/route/P3/lease，资源entered=null，不伪造False或重放许可。
# LLM: 原审批观察只在原handler词法catch活时写同claim未知，Count0仅原T，C/W还保原source末门；纯退休不覆盖物理未知。
# 函数用途: 保存原canonical未结观察供Future只读核收，不签终态、重放或释放许可。
def _record_execution_outcome_uncertain(paths, path, error, expected, *, execution_agent=None):
    from ..conversation.agent_tool_approval import (
        approval_retirement_diagnostic,
        approval_retirement_obligation,
        approval_retirement_request_matches,
    )
    from .io import gateway_request_fingerprint
    from .request_errors import gateway_request_execution_unknown, request_resource_exit_unconfirmed

    # LLM: JSON最新态再匹配原claim，common与真实资源分别保未进入/退出未知；runner只读common原锚点，来源或生命周期失效不写。
    # 函数用途: 为仍未结的这一轮附加结构化观察。
    def observe(current):
        actual = gateway_request_execution_unknown(error, current, expected)
        retirement = approval_retirement_request_matches(error, path, current,
            gateway_request_fingerprint(current, path.stem), agent=execution_agent)
        # 原同对象若也带物理退出未知，必须优先保原观察，不能用纯退休覆盖设施责任。
        if not actual and (not retirement or request_resource_exit_unconfirmed(error)):
            return current
        from .request_errors import request_execution_outcome_uncertain

        observation = {"schema_version": "gateway_execution_outcome.v1", "status": "unknown",
                       "request_id": current["id"], "execution_attempt_id": current["execution_attempt_id"],
                       "lease_epoch": current["lease_epoch"], "lease_owner": current["lease_owner"]}
        if not actual:
            observation.update(error_code=error.error_code, phase="approval_retirement", operation_entered=None)
        elif request_execution_outcome_uncertain(error, current, expected):
            observation.update(error_code=error.error_code, phase=error.phase, operation_entered=False)
        else:
            from .request_errors import heartbeat_request_execution_outcome_uncertain

            if heartbeat_request_execution_outcome_uncertain(error, current, expected):
                from ..memory_store.curator_lease import lease_heartbeat_error_origin

                observation.update(error_code="TASK_RESOURCE_STOP_UNCONFIRMED", phase="curator_lease_heartbeat_stop",
                                   operation_entered=None, lease_id=lease_heartbeat_error_origin(error)[1])
            else:
                from .request_errors import runner_request_execution_outcome_uncertain

                if runner_request_execution_outcome_uncertain(error, current, expected):
                    from ..common.runner_thread_origin import runner_thread_error_origin

                    origin = runner_thread_error_origin(error)
                    observation.update(error_code="TASK_RESOURCE_STOP_UNCONFIRMED", phase=origin[1], operation_entered=None,
                                       runner_run_id=origin[2], runner_attempt_id=origin[3])
                else:
                    observation.update(error_code=error.error_code, phase="bounded_call_exit", operation_entered=None,
                                       call_id=error.call_id, reason=error.reason)
        diagnostic = approval_retirement_diagnostic(error)
        previous = current.get("execution_outcome")
        if retirement or (diagnostic is not None and type(previous) is dict
                and previous.get("schema_version") == "gateway_execution_outcome.v1"
                and previous.get("approval_retirement") == diagnostic):
            # 外层仍可原位重记同设施unknown；保同native退休引用，不覆盖物理phase或另签来源。
            observation["approval_retirement"] = diagnostic
        return {**current, "execution_outcome": observation}

    if approval_retirement_obligation(error) is not None:
        from ..common.request_execution_ports import gateway_short_operation
        from .multiprocess_execution import current_worker_execution

        if current_worker_execution() is not None:
            # 实际C/W仍沿原source的T/P3/P5/生命周期末门，不把只读观察当写许可。
            gateway_short_operation("approval_retirement_unknown", lambda: update_json_file_atomic(path, observe, require_existing=True))
            return
    with gateway_turn_transition(paths, path.stem):
        update_json_file_atomic(path, observe, require_existing=True)



# LLM: Terminalization may need the owner-scoped ConversationStore, but owner resolution failure
# must never fall back to the bootstrap agent and cross an isolation boundary.
# 函数用途: 尽力取得请求所属的会话存储，失败时返回空供收口继续归档。
def _conversation_store_for_request(agent: SimpleAgent | None, payload: dict) -> object | None:
    if agent is None:
        return None
    try:
        return _resolve_request_agent(agent, payload).conversation_store
    except Exception:
        return None


def _attach_archived_chunk_stream(
    paths: GatewayPaths, request_id: str, target_folder: Path, response: dict
) -> None:
    archived_path, archive_error = _archive_gateway_chunk_stream(paths, request_id, target_folder)
    if archived_path is not None:
        response["chunk_stream_path"] = str(archived_path)
    if archive_error is not None:
        response["chunk_stream_archive_error"] = archive_error


def _archive_gateway_chunk_stream(
    paths: GatewayPaths, request_id: str, target_folder: Path
) -> tuple[Path | None, dict | None]:
    chunk_path = gateway_chunk_path(paths, request_id)
    if not chunk_path.exists():
        return None, None
    target_folder.mkdir(parents=True, exist_ok=True)
    target = target_folder / chunk_path.name
    try:
        chunk_path.replace(target)
    except OSError as exc:
        from ..runtime_errors import runtime_error_report

        report = runtime_error_report(exc, context="gateway.worker.chunk_stream.archive")
        report["path"] = str(chunk_path)
        report["target_path"] = str(target)
        return None, report
    return target, None


# LLM: 继承现有 owner 拒绝链，沿用规范失败归档，不建立另一个恢复状态机。
# 类用途: 标识进程 worker 路由错配，不能按共享主用户继续执行。
class OwnerRouteMismatchError(OwnerScopeUnavailableError):
    error_code = "OWNER_ROUTE_MISMATCH"



# LLM: 原gate始终核身份/输入/租约；满owner时仅收紧该次总量下界，原限制裁决前无新P3，最终只翻译容量原因。
# 函数用途: 在多进程原准入之前为新owner背压，旧owner及旧路由仍由原gate裁决。
def _durable_admission_decision(paths, request, gate, ownership, limits):
    capacity = getattr(limits, "owner_inflight", 0)
    if capacity <= 0:
        return gate.admit(request.path, ownership, limits, snapshot=request.payload)
    from .io import locked_file_transition
    from .multiprocess_route import owner_digest

    with locked_file_transition(paths.root / "admission" / "owner_capacity.transition"):
        occupied = gate.occupied_snapshot()
        if (not request.payload.get("route") and not occupied["unreadable"]
                and occupied["total"] < limits.global_inflight
                and not occupied["per_owner"].get(owner_digest(ownership[0]), 0)
                and len(occupied["per_owner"]) >= capacity):
            narrowed = AdmissionLimits(limits.user_inflight, min(limits.global_inflight, capacity), capacity)
            decision = gate.admit(request.path, ownership, narrowed, snapshot=request.payload)
            if decision.get("status") == "blocked" and decision.get("reason") == "global_inflight":
                return {**decision, "reason": "owner_capacity"}
            return decision
        return gate.admit(request.path, ownership, limits, snapshot=request.payload)


# LLM: C 只准入盖章；W 路由保留 pending 给冻结分区认领，只有 C 路由进入本地 claim/submit。
#   预留只由规范终态释放：claim 失败/回退、submit 失败都不动预留（A3 屏障点），
#   进程内字典计数在 count>0 时完全不参与（C 本地与各 worker 共用一个持久全局上限）。
# 函数用途: 为请求完成持久派发，返回 dispatched/claimed/blocked/raced，不抢 worker 的任务。
def _admit_and_submit_durable(
    paths: GatewayPaths,
    request: _PendingGatewayRequest,
    lane: tuple[AdmissionLimits, Callable[[GatewayClaim, str, str], None], object],
) -> str:
    from .multiprocess_admission import session_scope_for_request

    limits, submit, gate = lane
    user_key = request_user_key(request.payload)
    conversation_key = request_conversation_key(request.payload)
    try:
        owner = _durable_request_owner(gate, request.payload)
    except OwnerRouteMismatchError as error:
        _record_admission_wait(request, reason=str(error), error_code=error.error_code, paths=paths, admission_gate=gate)
        return "blocked"
    except OwnerScopeUnavailableError:
        _record_admission_wait(request, reason="owner_scope_unavailable", paths=paths, admission_gate=gate)
        return "blocked"
    # mc4-1706：锁外扫描到的请求正文作为快照交给准入，锁内会用同一投影核对归属输入未漂移。
    decision = _durable_admission_decision(
        paths, request, gate,
        (owner, session_scope_for_request(request.payload)),
        limits,
    )
    status = str(decision.get("status") or "")
    if status == "blocked":
        _record_admission_wait(request, reason=str(decision.get("reason") or "admission_blocked"), error_code=str(decision.get("error_code") or ""), paths=paths, admission_gate=gate)
        return "blocked"
    if status != "admitted":
        return "raced"
    if decision["request"]["route"]["worker"] != "C":
        return "dispatched"
    from .multiprocess_coordinator_claim import freeze_coordinator_claim
    from .multiprocess_dispatch import WorkerDispatchError

    try:
        claim = claim_request(paths, request.path, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
    except WorkerDispatchError as error:
        _record_admission_wait(request, reason=error.reason, error_code=error.error_code, paths=paths, admission_gate=gate)
        return "blocked"
    if claim is None:
        return "raced"
    submit(claim, user_key, conversation_key)
    gate.observe("claim_moved", {"request_id": request.path.stem})
    return "claimed"



# LLM: C只恢复main原冻结宿主owner，不按当前热配置重派；background须同owner canonical ref，typed源换代仅保原运输结清，不能消费新源。
# 函数用途: 为C准入恢复唯一owner，禁止把后台源声明或热配置当已盖章权限。
def _durable_request_owner(gate, payload):
    from ..user_space.owner_resolver import OwnerIdentity
    from .multiprocess_route import validate_published_route
    from .request_owner_binding import frozen_request_owner

    if "route" in payload:
        checked = validate_published_route(payload, gate.count, gate.c_run_id)
        if not checked["ok"]:
            raise OwnerRouteMismatchError(checked["reason"])
        return OwnerIdentity(*checked["owner"])
    try:
        owner = frozen_request_owner(payload)
        if owner is None:
            raise OwnerRouteMismatchError("request_owner_required")
        if payload.get("kind") == "background":
            from .control_plane import GatewayControlPlane
            from .multiprocess_background_request import (
                BackgroundSourceChanged,
                read_source,
                source_owner,
                validate_source_ref,
            )

            ref = validate_source_ref(payload.get("metadata", {}).get("background_source"))
            if source_owner(ref["owner"]) != owner:
                raise OwnerRouteMismatchError("background_source_owner_mismatch")
            root = gate.agent.home_root if type(gate.agent) is GatewayControlPlane else gate.agent.home_paths.root
            try:
                read_source(root, ref)
            except BackgroundSourceChanged:
                pass  # 原运输仍走同一claim/终态，业务入口会再次核此ref，不消费新源。
        return owner
    except (OSError, ValueError, TypeError, AttributeError, DataCorruptionError) as error:
        raise OwnerRouteMismatchError("request_owner_unavailable") from error


# LLM: 只在原T→JSON锁内按当前正文计算等待计数；原因/码变化立即刷新，同对才节流。
# 函数用途: 从现存队列记录算本次等待字段，不借旧扫描覆盖新计数或进队时间。
def _current_admission_wait_fields(payload: dict, path: Path, fields: dict) -> dict:
    now = float(fields["admission_wait_at"])
    waited_since = _admission_wait_since(payload, path)
    try:
        last_signal = float(payload.get("admission_wait_at") or 0.0)
    except (TypeError, ValueError):
        last_signal = 0.0
    same = all(str(payload.get(key) or "") == str(fields.get(key) or "")
               for key in ("admission_wait_reason", "admission_wait_error_code"))
    if same and last_signal and now - last_signal < _ADMISSION_WAIT_REFRESH_SECONDS:
        return {}
    try:
        count = max(0, int(payload.get("admission_wait_count") or 0))
    except (TypeError, ValueError):
        count = 0
    updated = {**fields,
        "admission_wait_count": count + 1,
        "admission_wait_since": waited_since or now,
    }
    if waited_since and not _has_queue_order_timestamp(payload):
        # 老请求/旁路生产者可能没有任何进队时间戳,此时排序回退用文件 mtime;我们改写文件会把
        # 它一直往后挤(等于自己制造饥饿)。第一次写等待信号时把"改写前的 mtime"钉成 created_at,
        # 顺序从此稳定,不再随等待信号漂移。
        updated["created_at"] = waited_since
    return updated
