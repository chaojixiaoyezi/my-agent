# LLM: 本模块负责 Gateway 请求入队、claim 和 worker 调度；文件 payload 的会话身份与客户端能力必须保持结构化且可审计。
#   owner 解析另认已用管理员密码绑定的 IM 私聊，判据只有结构化渠道身份与绑定表的精确匹配。
# 模块用途: 接收 Gateway 请求、写入队列并驱动后台 worker 可靠处理。

from __future__ import annotations

"""Request execution and handling for gateway.

Only normalized ordinary prompts and typed task-command payloads may enter the
file queue. Any slash command that bypasses HTTP/CLI dispatch fails closed before
the worker creates a model turn.
"""

import threading
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..concurrency.interrupt import register_interruptible
from ..conversation.control_commands import (
    conversation_request_interrupt_name,
)
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
from .request_client import GatewayAskParams, submit_gateway_ask
from .request_errors import (
    gateway_owner_scope_error_response,
    gateway_request_identity_error_response,
    gateway_request_load_error_response,
    gateway_request_processing_state_error_response,
    gateway_unhandled_worker_error_response,
)
from .request_execution import _handle_gateway_request
from .response_renderer import read_gateway_terminal_response_file

if TYPE_CHECKING:
    from ...core import SimpleAgent


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


class GatewayInboxScanGate:
    """inbox 空闲扫描门：目录 mtime 没变且上轮扫描为空时跳过 glob。

    粗粒度文件系统（mtime 秒级）保护：mtime 距今不足 2 秒时不跳过，
    避免同一秒内新写入的请求被漏掉。上轮存在 deferred（not_before_at）
    请求时也不跳过，保证延迟请求到点被处理。"""

    _COARSE_MTIME_GUARD_SECONDS = 2.0

    def __init__(self) -> None:
        self._last_mtime_ns: int | None = None
        self._scan_required = True

    def should_scan(self, inbox: Path) -> bool:
        if self._scan_required:
            return True
        mtime_ns = self._inbox_mtime_ns(inbox)
        if mtime_ns is None or mtime_ns != self._last_mtime_ns:
            return True
        return (time.time() - mtime_ns / 1e9) < self._COARSE_MTIME_GUARD_SECONDS

    def record_scan(self, inbox: Path, *, processed: int, deferred_present: bool) -> None:
        self._last_mtime_ns = self._inbox_mtime_ns(inbox)
        self._scan_required = bool(processed or deferred_present or self._last_mtime_ns is None)

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


class GatewayAdmission:
    """三层在飞记账:同会话顺序执行 + 每用户小坑 + 全局大坑。

    admission 在认领(claim)之前判:超限的请求留在 pending 排队(文件队列天然
    背压,不拒不崩),坑一空下轮派发扫描立即补位。单用户小坑防独吞饿死别人,
    全局大坑是总天花板。同一会话只放行一条,保证后一轮一定能看见前一轮已落库
    的 user/assistant 历史；同一用户的不同会话仍可并行。计数只在本进程网关
    派发路径动,claim 的原子性保证不会双记。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
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
    # 函数用途: 尝试获取准入并返回原因码(global_inflight/user_inflight/conversation_busy)。
    def try_acquire_report(
        self,
        user_key: str,
        user_limit: int,
        global_limit: int,
        *,
        conversation_key: str = "",
    ) -> str:
        with self._lock:
            if self._total >= max(1, global_limit):
                return "global_inflight"
            if self._per_user.get(user_key, 0) >= max(1, user_limit):
                return "user_inflight"
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


def request_conversation_key(payload: dict) -> str:
    """同一用户在同一渠道会话中的顺序键；没有结构化会话时不额外串行。"""
    conversation = (payload or {}).get("conversation")
    if not isinstance(conversation, dict):
        return ""
    channel_conversation_id = str(conversation.get("channel_conversation_id") or "").strip()
    if not channel_conversation_id:
        return ""
    channel = str(conversation.get("channel") or "gateway").strip() or "gateway"
    canonical_user_id = str(conversation.get("canonical_user_id") or "").strip()
    channel_user_id = str(conversation.get("channel_user_id") or "").strip()
    user_key = canonical_user_id or channel_user_id or request_user_key(payload)
    return "\x1f".join((user_key, channel, channel_conversation_id))


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


@dataclass(frozen=True)
class AdmissionLimits:
    user_inflight: int
    global_inflight: int

    @classmethod
    def from_config(cls, config: object) -> AdmissionLimits:
        return cls(
            user_inflight=_positive_int(getattr(config, "gateway_user_inflight_limit", 8), 8),
            global_inflight=_positive_int(
                getattr(config, "gateway_global_inflight_limit", 500), 500
            ),
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
) -> int:
    """两层限流的派发扫描:按 (recovery, created_at) 顺序认领在限内的请求并交给 submit;
    超限的留在 pending(排队),被限流数入探针。返回本轮认领数。

    公平性:单用户小坑满后扫描继续走到后面用户的请求——先到先服务但不许独吞;
    坑释放后下一轮扫描按同一顺序补位,任何用户至多再等一个空位周期,不会饿死。"""
    ensure_gateway_folders(paths)
    if hold_reason:
        for request in _iter_pending_requests(paths):
            _record_admission_wait(request, reason=hold_reason)
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
            _record_admission_wait(request, reason="retry_backoff")
            continue
        outcome = _admit_and_submit(paths, request, (limits, submit))
        claimed += 1 if outcome == "claimed" else 0
        blocked += 1 if outcome == "blocked" else 0
    if scan_gate is not None:
        # 被限流的请求在 inbox 不产生新 mtime:blocked>0 时视同"还有活",强制下轮重扫补位。
        scan_gate.record_scan(
            paths.inbox, processed=claimed, deferred_present=deferred_present or blocked > 0
        )
    gateway_admission_blocked_set(blocked)
    return claimed


def _admit_and_submit(
    paths: GatewayPaths,
    request: _PendingGatewayRequest,
    lane: tuple[AdmissionLimits, Callable[[GatewayClaim, str, str], None]],
) -> str:
    limits, submit = lane
    user_key = request_user_key(request.payload)
    conversation_key = request_conversation_key(request.payload)
    blocked_reason = admission.try_acquire_report(
        user_key,
        limits.user_inflight,
        limits.global_inflight,
        conversation_key=conversation_key,
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


# LLM: 只改"等待事实"字段,绝不碰 status/lease/模型进展字段;写入前先做节流判断,读-改-写
# 走既有原子更新原语(与其它请求文件写者同一把文件锁语义),因此并发扫描不会互相覆盖。
# 只按节流窗口决定是否再写一次,不按累计等待时长停止:停止续期等价于"前台报失败、后台照常执行",
# 那是隐式语义。权威收口由取消/终态/真实失活负责。
# 函数用途: 在被准入限流的排队请求上记录一次结构化"合法等待"信号(只做节流,不做累计上限)。
def _record_admission_wait(request: _PendingGatewayRequest, *, reason: str) -> None:
    now = time.time()
    payload = request.payload or {}
    waited_since = _admission_wait_since(payload, request.path)
    try:
        last_signal = float(payload.get("admission_wait_at") or 0.0)
    except (TypeError, ValueError):
        last_signal = 0.0
    if last_signal and now - last_signal < _ADMISSION_WAIT_REFRESH_SECONDS:
        return
    try:
        count = max(0, int(payload.get("admission_wait_count") or 0))
    except (TypeError, ValueError):
        count = 0
    fields: dict[str, object] = {
        "admission_wait_at": now,
        "admission_wait_reason": str(reason or ""),
        "admission_wait_count": count + 1,
        "admission_wait_since": waited_since or now,
    }
    if waited_since and not _has_queue_order_timestamp(payload):
        # 老请求/旁路生产者可能没有任何进队时间戳,此时排序回退用文件 mtime;我们改写文件会把
        # 它一直往后挤(等于自己制造饥饿)。第一次写等待信号时把"改写前的 mtime"钉成 created_at,
        # 顺序从此稳定,不再随等待信号漂移。
        fields["created_at"] = waited_since
    _write_admission_wait_field(request.path, fields)


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


# 函数用途: 用原子读-改-写把等待字段写进队列请求文件;失败只放弃本次信号,不影响扫描。
def _write_admission_wait_field(request_path: Path, fields: dict[str, object]) -> bool:
    def apply(current: dict) -> dict:
        updated = dict(current or {})
        updated.update(fields)
        return updated

    try:
        update_json_file_atomic(request_path, apply, require_existing=True)
        return True
    except (OSError, ValueError):
        return False


# LLM: Synchronous worker callers wait on the immutable canonical terminal record. A response
# projection may be repaired later but can never end the wait by itself.
# 函数用途: 等待指定 Gateway 请求的唯一终态归档并返回其完整答复。
def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    path = paths.terminal / f"{request_id}.json"
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        payload = read_gateway_terminal_response_file(
            path,
            request_id=request_id,
            context="gateway.worker.terminal.read",
        )
        if payload:
            return payload
        time.sleep(0.2)
    return {}


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


def _iter_pending_requests(paths: GatewayPaths) -> list[_PendingGatewayRequest]:
    entries = [
        _PendingGatewayRequest(
            path=request_path,
            payload=read_json_file_report(
                request_path,
                context="gateway.worker.pending_scan.read",
            ).payload,
        )
        for request_path in paths.inbox.glob("*.json")
    ]
    return sorted(entries, key=_pending_request_entry_sort_key)


def _pending_request_sort_key(request_path: Path) -> tuple[int, float, str]:
    payload_report = read_json_file_report(
        request_path, context="gateway.worker.pending_priority.read"
    )
    return _pending_request_entry_sort_key(
        _PendingGatewayRequest(request_path, payload_report.payload or {})
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


# LLM: Ordinary Gateway requests resolve owner once from structured ingress facts. Durable
# operation receipts use the paired exact-owner helper so later config cannot reroute recovery.
# 函数用途: 为普通请求解析并取得当前实际 owner-scoped Agent。
def _resolve_request_agent(agent, request_payload: dict):
    """按请求 owner 取作用域 agent(多用户飞书 per-用户隔离)。

    本机/单 owner 请求没有远程 channel 身份时走基础 agent。开启后:飞书用户 A/B 各跑在自己
    owner 作用域的 agent 上,home/记忆/数据/成本/审计天然隔离；远程身份缺失或 owner agent
    建立失败必须 fail-closed，绝不回退到共享 main agent 串户。
    """
    owner = _resolve_request_owner_identity(agent, request_payload)
    return _resolve_request_agent_for_owner(agent, owner)


# LLM: This resolver freezes the actual owner chosen at ingress. Durable receipts must persist
# the returned identity and must not rerun channel routing after configuration changes.
# 已绑定管理员的 IM 私聊（admin_channel_identity_for_request）先于逐用户路由解析为 base owner；控制作用域经同一函数，结果一致。
# 函数用途: 按当前可信请求事实解析本次实际使用的唯一 owner 身份，但不创建作用域 Agent。
def _resolve_request_owner_identity(agent, request_payload: dict):
    from ..user_space.owner_resolver import owner_identity_from_config

    base_owner = owner_identity_from_config(getattr(agent, "config", None))
    if not bool(getattr(getattr(agent, "config", None), "gateway_per_user_owner_scoping", False)):
        return base_owner
    if admin_channel_identity_for_request(agent, request_payload) is not None:
        # 已用管理员密码绑定的 IM 私聊按本机主用户运行；精确匹配只在 base owner 为 local/main 时成立。
        return base_owner
    owner = _owner_from_request(agent, request_payload)
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


# LLM: 开关读显式配置，基础 owner 必须正是 local/main；否则“绑定到管理员”没有可信目标，整体停用。
# 函数用途: 判断本 Gateway 是否允许 IM 私聊通过管理员密码成为本机管理员。
def admin_channel_identity_enabled(agent) -> bool:
    from ..user_space.owner_resolver import OwnerIdentity, owner_identity_from_config

    config = getattr(agent, "config", None)
    if not bool(getattr(config, "admin_channel_identity_enabled", False)):
        return False
    return owner_identity_from_config(config) == OwnerIdentity.local_main()


# LLM: 授权入口：私聊身份与 home/config 绑定文件做 (channel, user_id) 精确匹配，绑定文件损坏按未绑定处理（fail-closed）。
#   只读文件，不创建 Agent；owner 解析、控制作用域和 Gateway 审批开关都必须经这里，不能各自另判。
# 函数用途: 返回本请求命中的管理员 IM 身份绑定；不是已绑定的管理员私聊时返回 None。
def admin_channel_identity_for_request(agent, request_payload: dict):
    identity = private_channel_identity(request_payload) if isinstance(request_payload, dict) else None
    if identity is None or not admin_channel_identity_enabled(agent):
        return None
    home_root = getattr(getattr(agent, "home_paths", None), "root", None)
    if not home_root:
        return None
    from ..user_space.admin_channel_identity import find_admin_channel_identity

    return find_admin_channel_identity(home_root, *identity)


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
    # §6-A 量化探针:worker 忙数 gauge(上限=gateway_request_workers)。贴着这个上限跑
    # =顶层槽位饱和,第 4 个并发用户只能在 pending 里排队。
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


# LLM: 请求从 owner 解析到结果落账一直租用同一实例；退出只释放缓存租用，不改变持久任务恢复合同。
# 函数用途: 处理一个已认领请求，在所有结束路径保留 owner 隔离并释放运行资源。
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
    if bool(request_payload.get("cancel_requested")):
        with register_interruptible(conversation_request_interrupt_name(request_id)):
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
    pool = getattr(agent, "_owner_pool", None)
    lease = pool.pin(request_agent) if pool is not None and request_agent is not agent else nullcontext()
    with lease, register_interruptible(conversation_request_interrupt_name(request_id)):
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
        _finish_claimed_gateway_request(
            paths,
            processing_path,
            request_id,
            response,
            conversation_store=request_agent.conversation_store,
            expected_execution_attempt_id=expected_attempt_id,
            expected_lease_epoch=expected_lease_epoch,
        )
    return True


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


def _finish_claimed_gateway_request(
    paths: GatewayPaths,
    processing_path: Path,
    request_id: str,
    response: dict,
    *,
    conversation_store: object | None = None,
    expected_execution_attempt_id: str = "",
    expected_lease_epoch: int = 0,
) -> None:
    response_id = str(response.get("id") or "").strip()
    if response_id != request_id:
        raise DataCorruptionError(
            "gateway terminal response identity conflicts with the claimed request"
        )
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
    path must therefore create one terminal response and move the claim out of
    ``processing``; otherwise a Python/runtime initialization error becomes a
    false hour-long running task.
    """

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
