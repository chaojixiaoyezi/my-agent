# LLM: Gateway 请求绑定只投影已由宿主确认的 thread/task/run/attempt；写前绑定、T 锁和 claim 释放顺序不可更改。
# 模块用途: 将精确请求接到共用执行车道和运行身份，保留原 JSON 原子更新与停止裁决，不复制生命周期。
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..concurrency.interrupt import is_interrupted
from ..conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_WORK_KIND_ATTR,
    CONVERSATION_WORK_NAME_ATTR,
)
from ..conversation.control_commands import (
    conversation_task_attributes,
)
from ..conversation.run_claim import (
    ConversationRunLaneRequest,
    claim_heartbeat_interval_seconds,
    conversation_run_lane,
)
from .io import (
    gateway_turn_transition,
    read_json_file_report,
    update_json_file_atomic,
)
from .paths import (
    gateway_paths_from_root,
)
from .recovery import _ACTIVE_TURN_RECOVERY_SCHEMA
from .request_errors import (
    ConversationPersistenceError,
)

if TYPE_CHECKING:
    from . import request_context

_GATEWAY_FOREGROUND_CLAIM_REASON = "gateway_foreground_turn"


# LLM: This writer is the only bridge from a promoted conversation task back to the exact
# claimed Gateway request. Keep the durable request file and the live request object identical,
# otherwise an inline Compact retry loses active-turn authority and leaves wake policies alive.
#   RuntimeDB 身份另由 bind_runtime_authority 原样投影，展示任务不能覆盖执行绑定。
# 类用途: 保存本轮展示任务与实际执行绑定，分别供会话展示和精确恢复使用，互不反推。
@dataclass(frozen=True)
class GatewayTaskBindingWriter:
    """Publish live request -> durable task lineage for /status, /btw and /stop."""

    request_path: Path
    request_id: str
    request: dict | None = None
    execution_attempt_id: str = ""

    # LLM: Publish the exact request/thread lane before acquiring its recoverable claim. This
    # write-ahead binding lets the sole terminalizer release it even after a crash before execution.
    # 函数用途: 先保存请求的执行车道归属，避免进程中断后留下无法释放的租约。
    def bind_conversation_claim(self, thread_id: str) -> bool:
        if not self.request_id or not thread_id:
            raise ValueError("执行车道必须绑定 request_id 与 thread_id")
        binding = {
            "schema_version": "gateway_conversation_claim.v1",
            "request_id": self.request_id,
            "thread_id": thread_id,
            "task_id": f"gateway:{self.request_id}",
        }

        # LLM: Preserve queue fields under T plus JSON lock; an existing binding cannot change thread.
        # 函数用途: 持久保存唯一请求归属，恢复不能改绑其他会话，失败就不领取执行权。
        def persist() -> bool:
            # LLM: The existing binding is write-ahead authority, not an overwriteable projection.
            # 函数用途: 在原子更新中校验旧绑定，防止恢复失败时丢掉待清理的原始车道。
            def update(current: dict) -> dict:
                previous = current.get("conversation_claim")
                if previous is not None and previous != binding:
                    raise ConversationPersistenceError("恢复执行车道与原请求绑定不一致")
                return {**current, "conversation_claim": binding}

            update_json_file_atomic(
                self.request_path, update, require_existing=True,
            )
            if isinstance(self.request, dict):
                self.request["conversation_claim"] = dict(binding)
            return True

        return GatewayActiveTurnTransition(
            self.request_path, self.request_id, self.execution_attempt_id,
        )("bind_conversation_claim", persist)

    # LLM: Only core's actual DB binding calls this method, before any model/tool side effect.
    # Preserve unrelated queue fields under the exact transport-attempt transition; failure must
    # prevent execution. RuntimeDB revalidates this projection on recovery, it grants no new scope.
    # 函数用途: 将真实 task/run/attempt 原样落到本轮请求，避免旧项目展示编号误导重启恢复。
    def bind_runtime_authority(self, binding: dict[str, str]) -> bool:
        keys = ("task_id", "run_id", "agent_run_id", "attempt_id")
        if binding.get("invocation_run_id") != self.request_id or not all(
            isinstance(binding.get(k), str) and binding[k].strip() for k in keys
        ):
            return False
        payload = {
            "schema_version": "gateway_runtime_authority.v1",
            "request_id": self.request_id,
            "gateway_execution_attempt_id": self.execution_attempt_id,
            **{key: binding[key] for key in keys},
        }

        # LLM: T is held by the caller; the JSON lock preserves concurrent heartbeat fields.
        # 函数用途: 在当前回合锁内只更新执行绑定，不覆盖租约、停止标记或展示任务。
        def persist() -> bool:
            update_json_file_atomic(
                self.request_path,
                lambda current: {**current, "runtime_authority": payload},
                require_existing=True,
            )
            if isinstance(self.request, dict):
                self.request["runtime_authority"] = dict(payload)
            return True

        return GatewayActiveTurnTransition(
            self.request_path, self.request_id, self.execution_attempt_id,
        )("bind_runtime_authority", persist)

    # LLM: 任务晋升只能回写同一请求；原子持久写成功后再更新共享内存对象，失败不伪造绑定。
    # 函数用途: 接收运行时确认的任务链接，让本轮 Compact 重试和重启恢复使用相同归属。
    def __call__(self, link: object) -> bool:
        selected_thread_id = str(getattr(link, "thread_id", "") or "")
        selected_task_id = str(getattr(link, "task_id", "") or "")
        selected_task_path = str(getattr(link, "task_path", "") or "")
        if isinstance(self.request, dict):
            explicit_request_id = str(
                self.request.get("id") or self.request.get("request_id") or ""
            ).strip()
            if explicit_request_id and explicit_request_id != self.request_id:
                return False
        updated = persist_gateway_request_task_binding(
            self.request_path,
            self.request_id,
            thread_id=selected_thread_id,
            task_id=selected_task_id,
            task_path=selected_task_path,
        )
        if updated and isinstance(self.request, dict):
            self.request["conversation_runtime"] = {
                "request_id": self.request_id,
                "thread_id": selected_thread_id,
                "task_id": selected_task_id,
                "task_path": selected_task_path,
            }
        return updated


# LLM: This callback is the Gateway adaptation of 会话运行时's active_turn mutex. It validates the
# immutable execution attempt under T before runtime may reserve input or cross the provider edge.
# 类用途: 将补充消息认领和模型提交与同一精确 Gateway 回合的结束、停止串行化。
@dataclass(frozen=True)
class GatewayActiveTurnTransition:
    request_path: Path
    request_id: str
    execution_attempt_id: str

    # LLM: Caller supplies only an in-memory mailbox operation. This method owns T, re-reads the
    # exact hot request, and applies phase-specific admission: reserve/submit require open, while
    # provider ACK/explicit rejection cleanup may finish the same attempt after stop marked closing.
    # 函数用途: 按补充消息阶段校验精确执行代次，并与停止、终态收口串行。
    def __call__(self, phase: str, operation):
        phase_name = str(phase or "").strip().lower()
        allow_closing = phase_name in {"acknowledge", "restore", "release"}
        root = self.request_path.parent.parent.parent
        paths = gateway_paths_from_root(root)
        with gateway_turn_transition(paths, self.request_id):
            report = read_json_file_report(
                self.request_path,
                context="gateway.active_turn_transition.read",
            )
            payload = report.payload
            turn_phase = str(payload.get("turn_phase") or "open").strip().lower()
            if (
                report.load_error is not None
                or not payload
                or str(payload.get("id") or self.request_path.stem) != self.request_id
                or str(payload.get("status") or "") != "processing"
                or turn_phase not in ({"open", "closing"} if allow_closing else {"open"})
                or (payload.get("cancel_requested") is True and not allow_closing)
                or str(payload.get("execution_attempt_id") or "") != self.execution_attempt_id
                or (paths.terminal / f"{self.request_id}.json").exists()
            ):
                raise InterruptedError("Gateway active turn closed before provider admission")
            return operation()


# LLM: Transport owns this persisted projection, RuntimeDB owns the identity. Validate request
# and transport-attempt provenance before passing any field to recovery or a resumed RunParams.
# 函数用途: 读取已绑定的执行身份，拒绝错请求、错代次或残缺凭据，不解析模型正文。
def gateway_runtime_authority(request: dict, request_id: str) -> dict:
    binding = request.get("runtime_authority")
    if binding is None:
        return {}
    marker = request.get("active_turn_recovery")
    marker = marker if isinstance(marker, dict) else {}
    attempts = {str(request.get("execution_attempt_id") or "")}
    if (marker.get("schema_version") == _ACTIVE_TURN_RECOVERY_SCHEMA
            and marker.get("request_id") == request_id):
        attempts.add(str(marker.get("dead_execution_attempt_id") or ""))
    valid = (
        isinstance(binding, dict)
        and binding.get("schema_version") == "gateway_runtime_authority.v1"
        and binding.get("request_id") == request_id
        and isinstance(binding.get("gateway_execution_attempt_id"), str)
        and binding.get("gateway_execution_attempt_id") in attempts - {""}
        and all(isinstance(binding.get(k), str) and binding[k].strip()
                for k in ("task_id", "run_id", "agent_run_id", "attempt_id"))
    )
    if not valid:
        raise ConversationPersistenceError("本轮执行绑定的请求或代次不匹配，已停止恢复")
    return binding


# LLM: Recovery authority is typed metadata written by the reconciler. The narrow legacy branch
# accepts the prior structured priority+timestamp pair so an in-flight request survives upgrade;
# arbitrary last_error text or user prompt content can never enable active-turn replay.
# 函数用途: 判断请求是否确为 Gateway 重排的同一回合，并兼容上一版已经排队的恢复请求。
def gateway_request_is_active_turn_recovery(request: object, request_id: str) -> bool:
    row = request if isinstance(request, dict) else {}
    marker = row.get("active_turn_recovery")
    if isinstance(marker, dict):
        return bool(
            str(marker.get("schema_version") or "").strip() == _ACTIVE_TURN_RECOVERY_SCHEMA
            and str(marker.get("request_id") or "").strip() == str(request_id or "").strip()
        )
    try:
        requeued_at = float(row.get("requeued_at") or 0)
    except (TypeError, ValueError):
        requeued_at = 0
    return str(row.get("priority") or "").strip().lower() == "recovery" and requeued_at > 0


# LLM: Write-ahead request binding precedes pinned claim acquisition; terminalization owns crash
# cleanup. Background child wake may take this lane only after the foreground yields or finishes.
# 函数用途: 将原请求绑定到共用车道，重启后由原请求接续，不让后台先重复执行同一任务。
def gateway_conversation_execution_lane(context: request_context.GatewayAskRunContext, thread_id: str):
    """Serialize one thread's foreground and background model turns.

    会话运行时 reserves one ``active_turn`` before automatic idle work.  The
    conversation claim file adapts that invariant to my-agent's durable runtime.
    Foreground Gateway turns must own the same lane; otherwise a scheduled
    background wake can run the task concurrently and deliver a false completion
    while the foreground request is still changing files.
    """
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return nullcontext()
    agent = context.agent
    request_id = context.request_id
    GatewayTaskBindingWriter(
        context.request_path, request_id, context.request,
        str(context.request.get("execution_attempt_id") or "").strip() or request_id,
    ).bind_conversation_claim(thread_id)
    store = agent.conversation_store
    config = getattr(agent, "config", None)
    ttl_seconds = max(1, int(getattr(config, "background_claim_ttl_seconds", 90) or 90))
    interval_seconds = claim_heartbeat_interval_seconds(
        ttl_seconds=ttl_seconds,
        configured_interval_seconds=getattr(
            config,
            "background_claim_heartbeat_interval_seconds",
            0,
        ),
    )
    return conversation_run_lane(
        ConversationRunLaneRequest(
            store=store,
            thread_id=thread_id,
            # This is an execution owner, not durable workspace identity.  Keeping
            # the ids distinct lets admission reject only a true second executor
            # while this foreground turn owns the shared lane.
            claim_task_id=f"gateway:{request_id}",
            reason=_GATEWAY_FOREGROUND_CLAIM_REASON,
            lease_seconds=ttl_seconds,
            heartbeat_interval_seconds=interval_seconds,
            interrupt_check=is_interrupted,
            runtime_facts={"execution_source": "gateway", "request_id": request_id},
            recover_same_task_only=True,
            acquire_transition=GatewayActiveTurnTransition(
                context.request_path, request_id,
                str(context.request.get("execution_attempt_id") or "").strip() or request_id,
            ),
        )
    )


# LLM: 只在仍存在且 request_id 一致的队列文件上原子更新 conversation_runtime，不覆盖租约或终态字段。
# 函数用途: 持久保存当前请求与任务目录的确切对应，失败由任务绑定调用方处理。
def persist_gateway_request_task_binding(
    request_path: Path,
    request_id: str,
    *,
    thread_id: str,
    task_id: str,
    task_path: str,
) -> bool:
    """Atomically bind one claimed request to the durable task it is executing."""
    expected_id = str(request_id or "").strip()
    selected_task_id = str(task_id or "").strip()
    selected_thread_id = str(thread_id or "").strip()
    if not expected_id or not selected_task_id or not selected_thread_id:
        return False
    updated = False

    # LLM: 在原 JSON 锁内检查记录身份后更新单一绑定；updated 只报告本次写入是否适用。
    # 函数用途: 保留同时写入的队列字段，不把其它请求记录误绑到当前任务。
    def updater(current: dict) -> dict:
        nonlocal updated
        current_id = str(current.get("id") or current.get("request_id") or request_path.stem)
        if current_id != expected_id:
            return current
        current["conversation_runtime"] = {
            "request_id": expected_id,
            "thread_id": selected_thread_id,
            "task_id": selected_task_id,
            "task_path": str(task_path or ""),
        }
        updated = True
        return current

    try:
        update_json_file_atomic(request_path, updater, require_existing=True)
    except (OSError, FileNotFoundError, TypeError):
        return False
    return updated


# LLM: 命名工作归属只读取已验证 system_task 与 durable Audit 绑定，供前台历史/输入共用，不解析普通文本。
# 函数用途: 取得本轮需要记入消息的精确工作范围，防止同一会话的其它命名工作混入准备轮。
def gateway_message_work_scope(request: object) -> dict[str, object]:
    """Return the exact typed named-work attribution for one Gateway turn.

    The slash parser and durable Audit reservation own these facts.  Message
    history never derives them from prose, so interleaved named Audits can
    share one transcript without treating sibling requirements as authority.
    """

    row = request if isinstance(request, dict) else {}
    attrs = conversation_task_attributes(row.get("system_task"))
    work_kind = str(attrs.get(CONVERSATION_WORK_KIND_ATTR) or "").strip().lower()
    work_name = str(attrs.get(CONVERSATION_WORK_NAME_ATTR) or "").strip()
    if work_kind != "audit" or not work_name:
        return {}
    scope = row.get("conversation_audit_scope")
    scope = scope if isinstance(scope, dict) else {}
    runtime = row.get("conversation_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    task_id = str(scope.get("audit_id") or runtime.get("task_id") or "").strip()
    selected: dict[str, object] = {
        CONVERSATION_WORK_KIND_ATTR: work_kind,
        CONVERSATION_WORK_NAME_ATTR: work_name,
    }
    if task_id:
        selected["conversation_task_id"] = task_id
    if attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True:
        selected[CONVERSATION_AUDIT_PREPARE_ATTR] = True
    return selected


# LLM: Inline Compact/provider retries may rebuild RunParams, but only the exact request that
# durably published this thread/task binding may regain active-turn authority. A historical task id
# alone is insufficient because a later user turn or background wake can observe the same cwd.
# 函数用途: 判断当前请求是否仍是该会话任务的原执行回合，避免续跑丢权或新请求冒充旧回合。
def gateway_request_owns_active_task_turn(
    attrs: object,
    request: object,
    request_id: str,
) -> bool:
    selected = attrs if isinstance(attrs, dict) else {}
    row = request if isinstance(request, dict) else {}
    runtime = row.get("conversation_runtime")
    if not isinstance(runtime, dict):
        return False
    exact_request_id = str(request_id or "").strip()
    payload_request_id = str(row.get("id") or row.get("request_id") or "").strip()
    binding_request_id = str(runtime.get("request_id") or "").strip()
    if not exact_request_id:
        return False
    if payload_request_id and payload_request_id != exact_request_id:
        return False
    if binding_request_id and binding_request_id != exact_request_id:
        return False
    thread_id = str(selected.get("conversation_thread_id") or "").strip()
    task_id = str(selected.get("conversation_task_id") or "").strip()
    return bool(
        thread_id
        and task_id
        and str(runtime.get("thread_id") or "").strip() == thread_id
        and str(runtime.get("task_id") or "").strip() == task_id
    )


# LLM: A request-level task binding is written by the Gateway after promotion and survives retry
# or restart. Client prose and the thread's historical sticky pointer cannot manufacture it.
# 函数用途: 校验请求自身已经持久绑定的精确 thread/task 身份，供同一执行轮恢复目录。
def gateway_bound_request_task_id(
    request: object,
    *,
    request_id: str,
    thread_id: str,
) -> str:
    row = request if isinstance(request, dict) else {}
    runtime = row.get("conversation_runtime")
    if not isinstance(runtime, dict):
        return ""
    expected_request_id = str(request_id or "").strip()
    payload_request_id = str(row.get("id") or row.get("request_id") or "").strip()
    bound_request_id = str(runtime.get("request_id") or "").strip()
    if payload_request_id and expected_request_id and payload_request_id != expected_request_id:
        return ""
    if bound_request_id and expected_request_id and bound_request_id != expected_request_id:
        return ""
    if str(runtime.get("thread_id") or "").strip() != str(thread_id or "").strip():
        return ""
    return str(runtime.get("task_id") or "").strip()


# LLM: 精确请求的持久停止标记优先于模型或工具完成；恢复也须重读，不能从回复正文推断。
# 函数用途: 读取当前 processing 记录，判断这轮请求是否已被用户要求停止。
def gateway_cancel_requested(request_path: Path, request_id: str) -> bool:
    report = read_json_file_report(request_path, context="gateway.control.cancel.read")
    if report.load_error is not None or not report.payload:
        return False
    current_id = str(report.payload.get("id") or request_path.stem)
    return current_id == request_id and bool(report.payload.get("cancel_requested"))
