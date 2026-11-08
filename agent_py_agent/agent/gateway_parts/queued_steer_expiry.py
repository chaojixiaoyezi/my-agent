# LLM: 只按expected_turn_id与校验过的canonical终态收口；pending不生成执行身份，不新增P3，MP调用者必须确持原T与租约末门。
# 模块用途: 让已失去目标回合的排队插话直接过期，不误开下一轮。
from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

from ..runtime_errors import DataCorruptionError
from .io import gateway_request_fingerprint, gateway_turn_transition, read_json_file_report
from .owner_retention import gateway_terminal_exists
from .paths import GatewayPaths
from .response_renderer import read_gateway_terminal_envelope_report


# LLM: 只读且只处理从未执行的输入；目标必须同冻结owner和原会话，旧恢复责任交回原链，不借任意合法终态过期。
# 函数用途: 根据结构化目标终态生成过期响应；普通消息及未结束的目标返回None。
def queued_steer_expiry_response(paths: GatewayPaths, request: dict) -> dict | None:
    if _has_execution_responsibility(request):
        return None
    committed = _committed_expiry(paths, request)
    if committed is not None:
        return committed["terminal_response"]
    metadata = request.get("metadata")
    target = metadata.get("expected_turn_id") if isinstance(metadata, dict) else None
    if target is None or target == "":
        return None
    if (not isinstance(target, str) or not target.strip() or target in {".", ".."}
            or any(char in target for char in ("/", "\\", "\x00"))):
        raise DataCorruptionError("invalid_expected_turn_id")
    target = target.strip()
    report = read_gateway_terminal_envelope_report(paths.terminal / f"{target}.json", request_id=target, include_sealed=True)
    if report.load_error is not None:
        raise DataCorruptionError("target_terminal_unreadable")
    if not report.payload:
        return None
    _require_same_scope(request, report.payload)
    response = report.payload["terminal_response"]
    status = response.get("status")
    if (report.payload.get("turn_phase") != "closed" or report.payload.get("status") != status
            or status not in {"done", "failed", "cancelled", "stopped", "interrupted"}):
        raise DataCorruptionError("target_terminal_state_conflict")
    if status in {"done", "failed"}:
        # 目标自行收口不是用户放弃：入口idle/closing、finish未见输入与拒收重排都已承诺下一轮，只有停止族过期。
        return None
    return {"id": request["id"], "ok": False, "status": "cancelled", "response": "",
            "error_code": "EXPIRED_TARGET_TURN", "expected_turn_id": target, "ended_at": time.time()}


# LLM: 供Count0在原准入锁内只读复核，结束目标先于容量拒绝；读取不确定保留pending，不伪造过期。
# 函数用途: 返回可供原准入报告消费的结构化预检结果。
def queued_steer_pre_acquire(paths: GatewayPaths, request: dict) -> str:
    try:
        return "expired_target_turn" if queued_steer_expiry_response(paths, request) else ""
    except (OSError, ValueError, DataCorruptionError):
        return "target_turn_unavailable"


# LLM: 调用方持当前pending的T（MP另持原runtime commit门）；精确重读，不搬到processing、不造attempt或手拼canonical格式。
# 复用recovery的payload/commit叶，canonical先写成功再移除pending；提交失败保留原pending以便下次重试。
# 函数用途: 直接封存一条尚未执行的过期插话，返回唯一终态响应；扫描事实变更时不写。
def expire_queued_steer_locked(paths: GatewayPaths, path: Path, expected: dict, *, retain_inbox: bool = False) -> dict | None:
    if path.parent != paths.inbox or str(expected.get("id") or "") != path.stem:
        raise DataCorruptionError("queued_steer_identity_conflict")
    report = read_json_file_report(path)
    if report.load_error is not None:
        raise DataCorruptionError("queued_steer_unreadable")
    current = report.payload
    if not current or current != expected or (paths.processing / path.name).exists():
        return None
    _require_never_started(current)
    from .input_delivery_service import require_gateway_prepared_identity

    require_gateway_prepared_identity(paths, current)
    response = queued_steer_expiry_response(paths, current)
    if response is None:
        return None
    from .recovery import _commit_gateway_terminal_request, _terminal_gateway_request_payload

    terminal = read_gateway_terminal_envelope_report(paths.terminal / path.name, request_id=path.stem)
    if terminal.load_error is not None:
        raise DataCorruptionError("queued_steer_terminal_conflict")
    if terminal.payload:
        previous = terminal.payload["terminal_response"]
        if previous.get("error_code") != "EXPIRED_TARGET_TURN" or previous.get("expected_turn_id") != response["expected_turn_id"]:
            raise DataCorruptionError("queued_steer_terminal_conflict")
        response = previous
    payload = _terminal_gateway_request_payload(path.stem, current, response)
    _commit_gateway_terminal_request(paths, paths.terminal / path.name if retain_inbox else path,
                                     paths.failed, path.stem, payload)
    return response


# LLM: 只补从未执行的同名输入与展示；不冒充processing源，不跨目标T，不把None Store当输入空账。
# 函数用途: 在当前真实C末门内补投影，全部成功之后才退休原inbox。
def project_expired_steer(paths: GatewayPaths, request_id: str, *, admission_gate: object = None) -> bool:
    from . import input_delivery_service as inputs
    from .recovery import _commit_gateway_terminal_request, _project_recovered_terminal_response

    controller = getattr(admission_gate, "_runtime_takeover", None)
    lease = getattr(controller, "_lease", None)
    with _projection_scope(paths, request_id, admission_gate, controller, lease):
        path = paths.inbox / (request_id + ".json")
        current = read_json_file_report(path)
        if current.load_error is not None:
            raise DataCorruptionError("queued_steer_unreadable")
        terminal = read_gateway_terminal_envelope_report(paths.terminal / path.name, request_id=request_id)
        if terminal.load_error is not None or not terminal.payload:
            raise DataCorruptionError("queued_steer_terminal_unavailable")
        payload = current.payload or terminal.payload
        canonical = _committed_expiry(paths, payload)
        if canonical is None or (paths.processing / path.name).exists():
            raise DataCorruptionError("queued_steer_terminal_conflict")
        _require_never_started(payload)
        inputs.require_gateway_prepared_identity(paths, payload)
        inputs.require_gateway_prepared_identity(paths, canonical)
        if admission_gate is not None:
            _validate_expiry_reservation(admission_gate, payload)
        _commit_gateway_terminal_request(paths, paths.terminal / path.name, paths.failed, request_id, canonical)
        summary = {"errors": 0}
        related = inputs._turn_input_request_ids(paths, request_id, summary)
        receipt = inputs.read_gateway_input_receipt(paths, request_id)
        settled = not related and not summary["errors"]
        if receipt is not None:
            if receipt.state == "queued" and not receipt.target_turn_id:
                # 从未执行的运输不能证明消息消费；复用原terminal_unknown，不新增输入状态或伪造consumed。
                inputs._write_terminal_unknown(paths, receipt, None)
            elif receipt.state not in {"consumed", "terminal_unknown"} or receipt.reconcile_error:
                settled = False
        _project_recovered_terminal_response(paths, request_id, settled)
        if settled and current.payload:
            path.unlink()
        return settled


# LLM: 指纹不含顶层宿主owner/owner_id/route，须额外精确核对；canonical补交不依赖旧目标重查。
# 函数用途: 识别当前pending是否已经以过期原因封存。
def _committed_expiry(paths, request):
    request_id = str(request.get("id") or "")
    report = read_gateway_terminal_envelope_report(paths.terminal / (request_id + ".json"), request_id=request_id)
    if report.load_error is not None:
        raise DataCorruptionError("queued_steer_terminal_unreadable")
    if not report.payload:
        return None
    canonical = report.payload
    response = canonical["terminal_response"]
    metadata = request.get("metadata")
    if (canonical.get("turn_phase") != "closed" or canonical.get("status") != "cancelled"
            or response.get("status") != "cancelled" or response.get("error_code") != "EXPIRED_TARGET_TURN"
            or not isinstance(metadata, dict) or not metadata.get("expected_turn_id")
            or response.get("expected_turn_id") != metadata["expected_turn_id"]
            or gateway_request_fingerprint(canonical, request_id) != gateway_request_fingerprint(request, request_id)):
        raise DataCorruptionError("queued_steer_terminal_conflict")
    _require_same_scope(request, canonical)
    if canonical.get("route") != request.get("route"):
        raise DataCorruptionError("queued_steer_terminal_route_conflict")
    return canonical


# LLM: inbox不是未执行证明；恢复识别复用原现代/legacy合同，存在原claim/attempt/恢复责任时保留原链。
# 函数用途: 识别应交回原执行恢复链的请求。
def _has_execution_responsibility(payload):
    from .request_binding import gateway_request_is_active_turn_recovery, gateway_runtime_authority

    return bool(any(payload.get(key) for key in ("execution_attempt_id", "lease_epoch", "conversation_claim",
            "queue_transport_source", "active_turn_recovery", "runtime_authority"))
            or gateway_request_is_active_turn_recovery(payload, str(payload.get("id") or ""))
            or gateway_runtime_authority(payload, str(payload.get("id") or "")))


# LLM: 提交时复核和预检同源；只有从未执行的输入可走过期adapter。
# 函数用途: 禁止排队收口覆盖原执行恢复责任。
def _require_never_started(payload):
    if _has_execution_responsibility(payload):
        raise DataCorruptionError("queued_steer_has_execution_responsibility")


# LLM: 复用宿主冻结owner及原会话提取口，不从正文重推归属；缺身份和跨owner/会话均失败关闭。
# 函数用途: 防止用别人的终态过期本请求，或拿异主canonical删除当前inbox。
def _require_same_scope(request, other):
    from .request_owner_binding import frozen_request_owner
    from .request_worker import request_conversation_parts

    owner = frozen_request_owner(request)
    conversation = request_conversation_parts(request)
    if (owner is None or owner != frozen_request_owner(other)
            or not isinstance(request.get("owner_id"), str) or not request["owner_id"]
            or request["owner_id"] != other.get("owner_id")
            or not conversation or conversation != request_conversation_parts(other)):
        raise DataCorruptionError("queued_steer_scope_conflict")


# LLM: 特殊过期收口仅支持真实C；纯记录兼容合同必须在canonical之前拒绝，不能靠事后投影失败挂账。
# 函数用途: 取得这次过期提交必须具备的真实controller与lease。
def _expiry_coordinator_source(gate):
    from .multiprocess_admission import PersistentAdmission
    from .multiprocess_takeover_runtime import CoordinatorTakeover

    controller = getattr(gate, "_runtime_takeover", None)
    lease = getattr(controller, "_lease", None)
    if type(gate) is not PersistentAdmission or type(controller) is not CoordinatorTakeover or lease is None:
        raise BlockingIOError("queued_steer_coordinator_source_unavailable")
    return controller, lease


# LLM: 每个同步写叶持实际T→controller→lease；捕获对象只在本调用有效，替换controller/lease或closing均拒绝。
# 函数用途: 为从未执行的过期输入打开当前C元数据末门，Count0仍用原T。
@contextmanager
def _projection_scope(paths, request_id, gate, controller, lease):
    with gateway_turn_transition(paths, request_id):
        if gate is None:
            yield
            return
        from .multiprocess_admission import _runtime_commit_scope

        current_controller, current_lease = _expiry_coordinator_source(gate)
        if gate.paths != paths or current_controller is not controller or current_lease is not lease:
            raise BlockingIOError("queued_steer_coordinator_source_unavailable")
        with _runtime_commit_scope(gate) as rejection:
            if rejection:
                raise BlockingIOError("queued_steer_coordinator_inactive")
            yield


# LLM: 已提交终态不重占P3；旧代/已released原预留可补展示，但其owner、session与原route仍须完全匹配。
# 函数用途: 核对过期请求原预留，不为补交造路由或执行身份。
def _validate_expiry_reservation(gate, payload):
    from .multiprocess_admission import (
        _UNREADABLE,
        _legacy_scope_matches,
        _read_reservation,
        reservation_file,
        session_scope_for_request,
    )
    from .multiprocess_route import owner_digest, validate_published_route
    from .request_owner_binding import frozen_request_owner

    owner = frozen_request_owner(payload)
    if owner is None:
        raise DataCorruptionError("queued_steer_owner_unavailable")
    record = _read_reservation(reservation_file(gate.paths, payload["id"]))
    route = payload.get("route")
    if route is None and record is None:
        return
    if (record is _UNREADABLE or not isinstance(record, dict) or not isinstance(route, dict)
            or not validate_published_route(payload, gate.count, route.get("c_run_id")).get("ok")
            or record.get("route") != route or record.get("owner_sha") != owner_digest(owner)
            or _legacy_scope_matches(payload, record, session_scope_for_request(payload)) is None):
        raise DataCorruptionError("queued_steer_reservation_conflict")


# LLM: 早于ordinary准入检查；只补已提交canonical，原提交器RuntimeError冲突同样保留inbox及冲突投影，不中断扫描。
# 函数用途: 在下一次派发扫描中偿还过期请求的崩溃补交责任。
def resume_expired_steer(paths: GatewayPaths, path: Path, *, admission_gate: object = None) -> str | None:
    try:
        # 终态未知交原 blocked 分支，不退回 ordinary 准入重新派发。
        if not gateway_terminal_exists(paths, path.name):
            return None
        current = read_json_file_report(path)
        if current.load_error is not None or not current.payload:
            return "blocked"
        if _committed_expiry(paths, current.payload) is None:
            return None
        settled = project_expired_steer(paths, path.stem, admission_gate=admission_gate)
        if admission_gate is not None:
            admission_gate.release_if_terminal(path.stem)
        return "expired" if settled else "blocked"
    except (OSError, ValueError, RuntimeError, DataCorruptionError):
        return "blocked"
