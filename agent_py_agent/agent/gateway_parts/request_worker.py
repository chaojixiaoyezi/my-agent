
from __future__ import annotations

"""Request execution and handling for gateway."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..concurrency.interrupt import register_interruptible
from ..conversation.control_commands import conversation_request_interrupt_name
from ..observability.concurrency_metrics import (
    gateway_admission_blocked_set,
    gateway_inflight,
    gateway_worker_busy,
    record_gateway_queue_wait,
)
from .audit_service import audit_request_queued
from .io import (
    append_gateway_history,
    gateway_response_path,
    new_gateway_request_id,
    read_json_file,
    read_json_file_report,
    update_json_file_atomic,
    write_json_file,
)
from .paths import GatewayPaths, gateway_chunk_path
from .queue_service import (
    archive_request,
    claim_request,
    ensure_gateway_folders,
    materialize_missing_archive,
)
from .recovery import _gateway_request_attempts
from .request_errors import (
    gateway_owner_scope_error_response,
    gateway_request_load_error_response,
    gateway_request_processing_state_error_response,
    gateway_unhandled_worker_error_response,
)
from .request_execution import _handle_gateway_request
from .response_renderer import read_gateway_response_file

if TYPE_CHECKING:
    from ...core import SimpleAgent


@dataclass
class GatewayAskParams:
    prompt: str
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool = True
    include_prompt: bool = False
    resume_context: bool | None = None
    chat_session_id: str = ""
    channel_user_id: str = "local-cli"
    canonical_user_id: str = "local-agent"
    agent: SimpleAgent | None = field(default=None, repr=False)


_DEFAULT_GATEWAY_CLI_SESSION_ID = "default"


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
        with self._lock:
            if self._total >= max(1, global_limit):
                return False
            if self._per_user.get(user_key, 0) >= max(1, user_limit):
                return False
            if conversation_key and self._per_conversation.get(conversation_key, 0) >= 1:
                return False
            self._per_user[user_key] = self._per_user.get(user_key, 0) + 1
            if conversation_key:
                self._per_conversation[conversation_key] = (
                    self._per_conversation.get(conversation_key, 0) + 1
                )
            self._total += 1
        gateway_inflight(1)
        return True

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


@dataclass(frozen=True)
class AdmissionLimits:
    user_inflight: int
    global_inflight: int

    @classmethod
    def from_config(cls, config: object) -> AdmissionLimits:
        return cls(
            user_inflight=_positive_int(getattr(config, "gateway_user_inflight_limit", 8), 8),
            global_inflight=_positive_int(getattr(config, "gateway_global_inflight_limit", 500), 500),
        )


def _positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def dispatch_pending_requests(
    paths: GatewayPaths,
    limits: AdmissionLimits,
    submit: Callable[[Path, str, str], None],
    scan_gate: GatewayInboxScanGate | None = None,
) -> int:
    """两层限流的派发扫描:按 (recovery, created_at) 顺序认领在限内的请求并交给 submit;
    超限的留在 pending(排队),被限流数入探针。返回本轮认领数。

    公平性:单用户小坑满后扫描继续走到后面用户的请求——先到先服务但不许独吞;
    坑释放后下一轮扫描按同一顺序补位,任何用户至多再等一个空位周期,不会饿死。"""
    ensure_gateway_folders(paths)
    if scan_gate is not None and not scan_gate.should_scan(paths.inbox):
        return 0
    claimed = 0
    blocked = 0
    deferred_present = False
    for request in _iter_pending_requests(paths):
        if _request_deferred_until_later(request):
            deferred_present = True
            continue
        outcome = _admit_and_submit(paths, request, (limits, submit))
        claimed += 1 if outcome == "claimed" else 0
        blocked += 1 if outcome == "blocked" else 0
    if scan_gate is not None:
        # 被限流的请求在 inbox 不产生新 mtime:blocked>0 时视同"还有活",强制下轮重扫补位。
        scan_gate.record_scan(paths.inbox, processed=claimed, deferred_present=deferred_present or blocked > 0)
    gateway_admission_blocked_set(blocked)
    return claimed


def _admit_and_submit(
    paths: GatewayPaths,
    request: _PendingGatewayRequest,
    lane: tuple[AdmissionLimits, Callable[[Path, str, str], None]],
) -> str:
    limits, submit = lane
    user_key = request_user_key(request.payload)
    conversation_key = request_conversation_key(request.payload)
    if not admission.try_acquire(
        user_key,
        limits.user_inflight,
        limits.global_inflight,
        conversation_key=conversation_key,
    ):
        return "blocked"
    processing_path = claim_request(paths, request.path)
    if processing_path is None:
        admission.release(user_key, conversation_key=conversation_key)  # 别的扫描抢先认领了:坑退回
        return "raced"
    try:
        submit(processing_path, user_key, conversation_key)
    except Exception:
        # 提交失败不能漏坑;请求留在 processing,由恢复扫描收尸。
        admission.release(user_key, conversation_key=conversation_key)
        raise
    return "claimed"


def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    params: GatewayAskParams,
) -> tuple[str, Path, Path]:
    from .io import write_gateway_request

    # Request IDs join queue files, responses, chunk streams, and audits across clients.
    request_id = new_gateway_request_id()

    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": params.prompt,
        "inject": params.inject or [],
        "prompt_files": params.prompt_files or [],
        "save": params.save,
        "include_prompt": params.include_prompt,
        "created_at": time.time(),
        "client_pid": 0,
        "status": "pending",
        "priority": "interactive",
        "source": "cli_chat" if params.chat_session_id else "cli_gateway",
        "attempts": 0,
    }
    if params.resume_context is not None:
        payload["resume_context"] = bool(params.resume_context)
    payload["conversation"] = _gateway_conversation_payload(params)
    request_path = write_gateway_request(paths, payload)
    response_path = gateway_response_path(paths, request_id)
    if params.agent is not None:
        audit_request_queued(
            params.agent,
            {**payload, "status": "queued", "ok": False},
            request_path,
            response_path,
        )
    return request_id, request_path, response_path


def _gateway_conversation_payload(params: GatewayAskParams) -> dict:
    session_id = str(params.chat_session_id or _DEFAULT_GATEWAY_CLI_SESSION_ID)
    return {
        "channel": "chat" if params.chat_session_id else "gateway-cli",
        "channel_conversation_id": session_id,
        "channel_user_id": str(params.channel_user_id or "local-cli"),
        "canonical_user_id": str(params.canonical_user_id or "local-agent"),
    }


def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    path = gateway_response_path(paths, request_id)
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        payload = read_gateway_response_file(path, request_id=request_id, context="gateway.worker.response.read")
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
    payload_report = read_json_file_report(request_path, context="gateway.worker.pending_priority.read")
    return _pending_request_entry_sort_key(_PendingGatewayRequest(request_path, payload_report.payload or {}))


def _pending_request_entry_sort_key(request: _PendingGatewayRequest) -> tuple[int, float, str]:
    payload = request.payload or {}
    priority = str(payload.get("priority") or "").strip().lower()
    is_recovery = priority == "recovery" or bool(payload.get("requeued_at"))
    created_at = _request_created_at(payload, request.path)
    return (1 if is_recovery else 0, created_at, request.path.name)


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


class OwnerScopeUnavailableError(RuntimeError):
    """远程用户请求无法建立隔离 owner 时 fail-closed，禁止回退到共享 main agent。"""

    error_code = "OWNER_SCOPE_UNAVAILABLE"


def _resolve_request_agent(agent, request_payload: dict):
    """按请求 owner 取作用域 agent(多用户飞书 per-用户隔离)。

    本机/单 owner 请求没有远程 channel 身份时走基础 agent。开启后:飞书用户 A/B 各跑在自己
    owner 作用域的 agent 上,home/记忆/数据/成本/审计天然隔离；远程身份缺失或 owner agent
    建立失败必须 fail-closed，绝不回退到共享 main agent 串户。
    """
    if not bool(getattr(getattr(agent, "config", None), "gateway_per_user_owner_scoping", False)):
        return agent
    owner = _owner_from_request(agent, request_payload)
    if owner is None:
        if _is_remote_channel_request(request_payload):
            raise OwnerScopeUnavailableError("远程通道请求缺少可信 user_id/channel，已拒绝共享 owner 回退")
        return agent
    _record_active_owner(agent, owner)  # 登记进共享活跃表,让后台主代理循环能逐 owner tick 叫回
    try:
        return _owner_pool(agent).get(owner)
    except Exception as exc:
        raise OwnerScopeUnavailableError(
            f"无法建立 owner 隔离 agent(provider={owner.provider}, kind={owner.owner_kind})"
        ) from exc


def _is_remote_channel_request(request_payload: dict) -> bool:
    metadata = request_payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    channel = str(metadata.get("channel") or "").strip().lower()
    if not channel:
        return False
    return channel not in {"local", "cli", "chat", "gateway-cli", "http"}


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
    chat_type/chat_id，不从 conversation_id 或自然语言猜测。
    """
    meta = request_payload.get("metadata") if isinstance(request_payload.get("metadata"), dict) else {}
    user_id = str(request_payload.get("user_id") or meta.get("user_id") or "").strip()
    channel = str(meta.get("channel") or "").strip()
    if not user_id or user_id == "anonymous" or not channel:
        return None
    from ..user_space.owner_resolver import OwnerIdentity

    chat_type = str(meta.get("channel_chat_type") or "").strip().lower()
    chat_id = str(meta.get("channel_chat_id") or "").strip()
    if chat_type not in {"", "p2p", "private"} and chat_id:
        return OwnerIdentity.provider_group(channel, chat_id)
    return OwnerIdentity.provider_user(channel, user_id)


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
    processing_path = claim_request(paths, request_path)
    if processing_path is None:
        return False
    # §6-A 量化探针:worker 忙数 gauge(上限=gateway_request_workers)。贴着这个上限跑
    # =顶层槽位饱和,第 4 个并发用户只能在 pending 里排队。
    gateway_worker_busy(1)
    try:
        return _process_claimed_gateway_request_path(agent, paths, processing_path, worker_id)
    finally:
        gateway_worker_busy(-1)


def _process_claimed_gateway_request_path(
    agent: SimpleAgent,
    paths: GatewayPaths,
    processing_path: Path,
    worker_id: str,
) -> bool:
    request_report = read_json_file_report(processing_path, context="gateway.worker.request.read")
    if request_report.load_error is not None:
        request_id = processing_path.stem
        if gateway_response_path(paths, request_id).exists():
            archive_request(processing_path, paths.done, request_id)
            return True
        response = gateway_request_load_error_response(processing_path, request_report.load_error)
        _finish_claimed_gateway_request(paths, processing_path, request_id, response)
        return True
    request_payload = request_report.payload
    request_id = str(request_payload.get("id") or processing_path.stem)
    request_payload.setdefault("id", request_id)
    if gateway_response_path(paths, request_id).exists():
        archive_request(processing_path, paths.done, request_id)
        return True
    if bool(request_payload.get("cancel_requested")):
        with register_interruptible(conversation_request_interrupt_name(request_id)):
            response = _handle_gateway_request(
                agent,
                processing_path,
                refresh_lease=False,
                worker_id=worker_id,
            )
        _finish_claimed_gateway_request(paths, processing_path, request_id, response)
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
        _finish_claimed_gateway_request(paths, processing_path, request_id, response)
        return True
    with register_interruptible(conversation_request_interrupt_name(request_id)):
        response = _process_claimed_gateway_request(
            _ClaimedGatewayRequestContext(
                request_agent,  # 多用户飞书:只在请求 owner 作用域的 agent 上跑；失败已在上方终态拒绝
                processing_path, request_payload, request_id, worker_id,
            )
        )
    _finish_claimed_gateway_request(paths, processing_path, request_id, response)
    return True


def _process_claimed_gateway_request(context: _ClaimedGatewayRequestContext) -> dict:
    from ..runtime_errors import runtime_error_report
    from .logging import _report_gateway_side_effect_error

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
        created_at = float(request_payload.get("created_at") or request_payload.get("submitted_at") or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0
    if created_at > 0:
        record_gateway_queue_wait(lease_now - created_at)
    request_payload.update(
        {
            "status": "processing",
            "attempts": _gateway_request_attempts(request_payload) + 1,
            "lease_owner": worker_id,
            "lease_started_at": lease_now,
            "lease_heartbeat_at": lease_now,
            "updated_at": lease_now,
        }
    )


def _finish_claimed_gateway_request(
    paths: GatewayPaths,
    processing_path: Path,
    request_id: str,
    response: dict,
) -> None:
    target_folder = paths.done if response.get("ok") else paths.failed
    _attach_archived_chunk_stream(paths, request_id, target_folder, response)
    response_path = gateway_response_path(paths, str(response.get("id", processing_path.stem)))
    final_request_load_error = _write_final_request_archive_payload(processing_path, response)
    if final_request_load_error is not None:
        response["final_request_load_error"] = final_request_load_error
    if not response_path.exists():
        write_json_file(response_path, response)
    append_gateway_history(paths, response)
    archived = archive_request(processing_path, target_folder, request_id)
    if not archived and not processing_path.exists():
        materialize_missing_archive(target_folder, request_id, response)


def terminalize_unhandled_claimed_gateway_request(
    paths: GatewayPaths,
    processing_path: Path,
    error: BaseException,
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
    )


def _attach_archived_chunk_stream(paths: GatewayPaths, request_id: str, target_folder: Path, response: dict) -> None:
    archived_path, archive_error = _archive_gateway_chunk_stream(paths, request_id, target_folder)
    if archived_path is not None:
        response["chunk_stream_path"] = str(archived_path)
    if archive_error is not None:
        response["chunk_stream_archive_error"] = archive_error


def _archive_gateway_chunk_stream(paths: GatewayPaths, request_id: str, target_folder: Path) -> tuple[Path | None, dict | None]:
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


def _write_final_request_archive_payload(processing_path: Path, response: dict) -> dict | None:
    report = read_json_file_report(processing_path, context="gateway.worker.final_request.read")
    if report.load_error is not None:
        return report.load_error
    request_payload = report.payload
    if not request_payload:
        return None
    terminal_status = str(response.get("status") or ("done" if response.get("ok") else "failed"))
    request_payload.update(
        {
            # The response is the authoritative terminal outcome.  Keeping the
            # lease's earlier ``processing`` value after moving this file into a
            # terminal archive makes /status and recovery disagree with /result.
            "status": terminal_status,
            "attempts": response.get("attempts", request_payload.get("attempts", 0)),
            "lease_owner": response.get("lease_owner", request_payload.get("lease_owner", "")),
            "lease_started_at": response.get("lease_started_at", request_payload.get("lease_started_at", 0)),
            "lease_heartbeat_at": response.get("lease_heartbeat_at", request_payload.get("lease_heartbeat_at", 0)),
            "completed_at": response.get("ended_at", time.time()),
        }
    )
    write_json_file(processing_path, request_payload)
    return None
