# LLM: 仅原first materialize/pre-pin事件可达；原T→controller→lease固定writer，无owner清算不借隔离发业务权。同步wire、P3及终态回归。
# 模块用途: 把可证未开始的原请求交原FAILED/P3/展示链；输入结算明确保欠账，不造Agent、恢复source、成功或重放。
from . import multiprocess_admission as ma
from .multiprocess_native_refusal_source import (
    claim_bytes,
    original_terminal_gate,
    read_claim_payload,
    require_captured_claim,
)
from .multiprocess_worker_commit import WorkerCommitNotStarted, WorkerCommitRejected


# LLM: caller持原T；只认本次begin producer事件及原operation来源，严格不合格时不调用任何terminal/release writer。
# 函数用途: 在原同步首拒处提交本请求失败并原样尝试P3释放，返回原固定后投影所需引用。
def terminalize_first_refusal(operation, peer_pid, rejection_origin, error):
    if not getattr(operation, "_first_materialize_origin", False):
        return None
    issuer, owner, transition, phase, _callback = operation._sources
    if (
        phase != "owner_materialize"
        or type(error) is not WorkerCommitNotStarted
        or not error.matches_request(issuer, peer_pid, rejection_origin)
    ):
        return None
    gate = original_terminal_gate(issuer)
    issuer._require_worker(peer_pid)
    with ma._runtime_commit_scope(gate) as rejection:
        if rejection is not None:
            raise WorkerCommitRejected("first_native_terminal_unavailable")
        payload = require_captured_claim(operation, issuer, owner, transition)
        paths, selected = gate.paths, transition.request_id
        from ..common.nofollow_fs import read_bytes_beneath

        if (
            read_bytes_beneath(
                paths.root, ("requests", "terminal", selected + ".json"), require_dir_fd=True
            )
            is not None
        ):
            raise WorkerCommitRejected("first_native_terminal_conflict")
        from .recovery import _terminalize_gateway_request_locked
        from .request_errors import gateway_unhandled_worker_error_response

        response = gateway_unhandled_worker_error_response(
            transition.request_path, payload, error, request_id=selected
        )
        expected = {
            "expected_execution_attempt_id": payload["execution_attempt_id"],
            "expected_lease_epoch": payload["lease_epoch"],
            "expected_lease_heartbeat_at": payload["lease_heartbeat_at"],
            "expected_request_status": "processing",
            "expected_turn_phase": "open",
            "expected_cancel_requested": False,
        }
        # None只表示此首片不持有Store；原all([])绝不变成“输入已结算”证明。
        _terminalize_gateway_request_locked(
            paths, transition.request_path, paths.failed, selected, None, response, expected
        )
        canonical = read_claim_payload(paths, paths.terminal / (selected + ".json"))
        require_refusal_terminal(payload, canonical)
        released = ma._release_reservation_locked(
            gate, ma.reservation_file(paths, selected), selected
        )
        # 释放写失败保原occupied，原terminal-based消费者可补释放，不重做物化或撤销canonical。
        error._terminal_release_observation = released
        return issuer, gate, selected, canonical


# LLM: canonical是唯一终态；完整claim/route/指纹必须与原首像一致，固定error仅在typed首拒writer之后消费，不解析自然语言。
# 函数用途: 核同请求失败身份，拒绝借其它attempt或伪终态收槽。
def require_refusal_terminal(original, canonical):
    from .io import validated_gateway_request_fingerprint

    response = canonical.get("terminal_response")
    fixed = (
        "id",
        "execution_attempt_id",
        "lease_epoch",
        "lease_owner",
        "lease_process_identity",
        "lease_started_at",
        "lease_heartbeat_at",
        "attempts",
        "route",
        "request_fingerprint",
        "request_fingerprint_schema",
    )
    if (
        any(
            type(canonical.get(key)) is not type(original.get(key))
            or claim_bytes(canonical[key]) != claim_bytes(original[key])
            for key in fixed
        )
        or canonical.get("schema_version") != "gateway_terminal_request.v1"
        or canonical.get("status") != "failed"
        or canonical.get("turn_phase") != "closed"
        or canonical.get("ok") is not False
        or canonical.get("error_code") != "WORKER_COMMIT_REJECTED"
        or type(response) is not dict
        or response.get("id") != original["id"]
        or response.get("status") != "failed"
        or response.get("ok") is not False
        or response.get("error_code") != "WORKER_COMMIT_REJECTED"
    ):
        raise WorkerCommitRejected("first_native_terminal_mismatch")
    validated_gateway_request_fingerprint(canonical, original["id"])


# LLM: 此函数仅在原request T已退出后调用；另取原metadata T→controller→lease，拒撤权，原projector固定写response/history/marker，明确pending。
# 函数用途: 完成可读失败响应，同时保留原输入结算责任；不调用None输入结算，不发表P6。
def project_first_refusal(outcome):
    issuer, gate, selected, canonical = outcome
    from .io import gateway_turn_transition
    from .recovery import _project_recovered_terminal_response

    with gateway_turn_transition(gate.paths, selected):
        original_terminal_gate(issuer)
        with ma._runtime_commit_scope(gate) as rejection:
            if rejection is not None:
                raise WorkerCommitRejected("first_native_projection_unavailable")
            current = read_claim_payload(gate.paths, gate.paths.terminal / (selected + ".json"))
            if current != canonical:
                raise WorkerCommitRejected("first_native_terminal_changed")
            _project_recovered_terminal_response(gate.paths, selected, settlement_ok=False)
