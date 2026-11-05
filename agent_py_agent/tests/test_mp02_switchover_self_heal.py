# LLM: 仅在隔离临时home使用真实C锁/准入/终态/会话存储；进程死亡观察是显式替身，不代表线上完整P2或真实用户E2E。
# 模块用途: 钉住MP-02切换遗留请求与控制回执的安全收尾、重试和保守拒绝。
import copy
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.gateway_parts import daemon_metadata, recovery, request_worker
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import multiprocess_processing_recovery as pr
from agent_py_agent.agent.gateway_parts import multiprocess_unroutable_closeout as closeout
from agent_py_agent.agent.gateway_parts.io import (
    read_json_file,
    read_json_file_report,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    reservation_file,
    session_scope_for_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import RouteStamp, freeze_request_route
from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.tests.test_gateway_cold_processing_recovery import cold_case
from agent_py_agent.tests.test_gateway_prepared_route_immutability import _fingerprint
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_rework_v14_admission_io_recovery import ordinary_case
from agent_py_agent.tests.test_v13_w4_conversation_steer_stop import _count0_steer


@pytest.mark.parametrize("variant", ["stale", "missing_stamp"])
@pytest.mark.parametrize("retry_via", ["dispatcher", "projector", "admission"])
def test_committed_retirement_converges_after_one_projection_error(context, monkeypatch, variant, retry_via):
    gate, path, ownership = ordinary_case(context)
    if variant == "stale":
        payload = read_json_file(path)
        payload = freeze_request_route(payload, ownership[0], RouteStamp(gate.count, "retired-c", 1.0,
                                               payload["metadata"]["gateway_admin_binding_version"]))
        write_json_file_atomic(path, payload)
    else:
        path, payload, ownership = _legacy_pending(gate, abv=True)
    original = recovery._project_recovered_terminal_response
    errors = []

    def one_error(*args):
        if not errors:
            errors.append("projection")
            raise OSError("simulated projection I/O error")
        return original(*args)

    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", one_error)
    submit = Mock(side_effect=AssertionError("terminal requests never execute"))
    before = path.read_bytes()
    limits = AdmissionLimits(8, 8)
    assert request_worker.dispatch_pending_requests(gate.paths, limits, submit, admission_gate=gate) == 0
    canonical = gate.paths.terminal / path.name
    saved = canonical.read_bytes()
    assert errors == ["projection"] and path.read_bytes() == before
    assert not (gate.paths.responses / path.name).exists()
    for _ in range(3):
        if retry_via == "dispatcher":
            assert request_worker.dispatch_pending_requests(gate.paths, limits, submit, admission_gate=gate) == 0
        elif retry_via == "projector":
            recovery.repair_gateway_terminal_projections(gate.paths, agent=gate.agent, full_scan=True,
                recovery_context=recovery._RecoveryContext(time.time(), 3, 10, False, gate.agent, None, admission_gate=gate))
        elif path.exists():
            assert gate.admit(path, ownership, limits)["status"] == "expired"
    assert not path.exists(), "committed retirement pending must converge after one successful retry"
    assert canonical.read_bytes() == saved
    response = read_json_file(gate.paths.responses / path.name)
    assert response == read_json_file(canonical)["terminal_response"]
    assert response["error_code"] == "GATEWAY_REQUEST_ROUTE_RETIRED"
    assert not reservation_file(gate.paths, path.stem).exists()
    receipt = inputs.read_gateway_input_receipt(gate.paths, path.stem)
    if receipt is not None:
        assert receipt.state == "terminal_unknown"
    history = [json.loads(line) for line in gate.paths.history.read_text().splitlines()]
    assert sum(row.get("id") == path.stem for row in history) == 1
    submit.assert_not_called()


@pytest.mark.parametrize("settlement_error", [False, True])
@pytest.mark.parametrize("variant", ["single", "multi", "slow", "live", "unknown", "wrong_scope", "bad_detail", "cropped", "missing_proof", "wrong_claim"])
def test_recovery_requeued_pending_closes_after_real_retirement(tmp_path, monkeypatch, settlement_error, variant):
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import coordinator_admission_gate
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_generation_retirement import (
        retire_dead_multiprocess_generations,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    if variant in {"multi", "cropped"}:
        from agent_py_agent.tests import test_gateway_processing_recovery_source as fixtures

        original_register = fixtures.register

        def two_launches(ledger, source):
            result = original_register(ledger, source)
            extra = dict(source, reservation_id="exec-two")
            if variant == "cropped":
                extra["request_id"] = "another-request"
            original_register(ledger, extra, pid=102)
            return result

        # 两次真实登记发生在旧scope冻结之前，不修改已捕获的P1来源。
        monkeypatch.setattr(fixtures, "register", two_launches)
    with cold_case(tmp_path, monkeypatch, with_input=True) as (original, control, gate, context):
        binding = read_json_file(original.claim.path)["conversation_claim"]
        original.store.tasks.bind({"task_id": binding["task_id"], "thread_id": binding["thread_id"], "status": "active"})
        assert original.store.claims.acquire({"thread_id": binding["thread_id"], "task_id": binding["task_id"],
                                              "recover_same_task_only": True})
        report = recovery.recover_gateway_processing_requests_report(gate.paths, params=context)
        assert not report.load_errors and report.summary["requeued"] == 1
        paths, store, config = gate.paths, original.store, control.config
        path = paths.inbox / original.claim.path.name
        pending = read_json_file(path)
        prior = path.read_bytes()
        assert all(key in pending for key in ("conversation_claim", "lease_epoch", "active_turn_recovery", "queue_transport_source"))
        home, root = control.home_root, control.agent_root
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
    # 后继使用原constructor的新DTO，不让旧C捕获的角色租约跟随新代次。
    paths = gateway_paths_from_root(paths.root)
    with coordinator_guard(paths.root, 1):
        assert retire_dead_multiprocess_generations(paths, target_count=1, current_c_run_id="successor-c")
        assert path.read_bytes() == prior
        successor = prepare_coordinator_takeover(paths, 1, "successor-c")
        assert successor.active() and not successor._all_quarantined
        control = GatewayControlPlane(config, home, paths, 1, "successor-c", agent_root=root, workspace_roots=(root,))
        gate = coordinator_admission_gate(control, paths, SimpleNamespace(coordinator_worker_count=1))
        if variant == "slow":
            import threading

            from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2
            from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition

            observe, probes, probe_errors = p2.observe_processes, [], []

            def slow_observation():
                acquired = threading.Event()

                def competing_metadata():
                    try:
                        with gateway_turn_transition(paths, path.stem), gate._runtime_takeover._mutex, gate._runtime_takeover._lease._lifecycle_gate:
                            acquired.set()
                    except BaseException as error:
                        probe_errors.append(error)

                probe = threading.Thread(target=competing_metadata, daemon=True)
                probe.start()
                probes.append(probe)
                assert acquired.wait(3), "platform observation must not hold T/M/L"
                probe.join(3)
                assert not probe.is_alive() and not probe_errors
                return observe()

            monkeypatch.setattr(p2, "observe_processes", slow_observation)
        elif variant in {"live", "unknown"}:
            from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2

            table = {"platform": "darwin", "host_id": "host-one", "rows": {}, "failure": ""}
            if variant == "unknown":
                table["failure"] = "observation_unavailable"
            else:
                table["rows"][101] = {"pid": 101, "ppid": 1, "pgid": 101, "sid": 101, "birth": 10, "state": "R"}
                record = pending["queue_transport_source"]["stopped_execution"]["records"][0]
                assert p2.evaluate_tree(record, table)["status"] == "live"
            monkeypatch.setattr(p2, "observe_processes", lambda: table)
        elif variant not in {"single", "multi", "slow"}:
            changed = copy.deepcopy(pending)
            evidence = changed["queue_transport_source"]["stopped_execution"]
            if variant == "wrong_scope":
                evidence["proof"]["covered_scope"]["worker_index"] = 1
            elif variant == "bad_detail":
                evidence["detail"] = None
            elif variant == "missing_proof":
                del evidence["proof"]
            elif variant == "wrong_claim":
                changed["conversation_claim"]["thread_id"] = "another-thread"
            else:
                evidence["records"] = evidence["records"][:1]
                evidence["proof"]["covered_scope"]["executor_reservation_ids"] = ["exec-one"]
                evidence["detail"]["covered_scope"]["executor_reservation_ids"] = ["exec-one"]
                evidence["detail"]["instances"] = evidence["detail"]["instances"][:1]
                evidence["detail"]["record_digests"].pop("exec-two")
            write_json_file_atomic(path, changed)
        from agent_py_agent.tests.test_rework_v14_admission_io_recovery import fd_count

        before_fd = fd_count()
        guarded = path.read_bytes()
        original_finish = closeout._finish_closeout_claim
        errors = []

        def once(*args):
            if settlement_error and not errors:
                errors.append("settlement")
                raise OSError("simulated settlement I/O error")
            return original_finish(*args)

        monkeypatch.setattr(closeout, "_finish_closeout_claim", once)
        project = closeout.project_unroutable_closeout
        projection_errors = []

        def observed_projection(*args, **kwargs):
            try:
                return project(*args, **kwargs)
            except Exception:
                import traceback

                projection_errors.append(traceback.format_exc())
                raise

        monkeypatch.setattr(closeout, "project_unroutable_closeout", observed_projection)
        submit = Mock(side_effect=AssertionError("recovered retirement must not execute"))
        for _ in range(3):
            if variant not in {"single", "multi", "slow"}:
                exact = path.read_bytes()
                assert gate.try_close_unroutable_pending(path)["status"] == "blocked"
                assert path.read_bytes() == exact
            assert request_worker.dispatch_pending_requests(paths, AdmissionLimits(8, 8), submit, admission_gate=gate) == 0
        if variant not in {"single", "multi", "slow"}:
            # dispatcher按原契约新增等待投影；所有原字段必须逐项相等，不只挑几项证明。
            unchanged, waiting = json.loads(guarded), read_json_file(path)
            assert all(waiting[key] == value for key, value in unchanged.items())
            assert set(waiting) - set(unchanged) <= {"admission_wait_at", "admission_wait_reason", "admission_wait_count",
                                                   "admission_wait_error_code", "admission_wait_since"}
            assert waiting["admission_wait_reason"] == "unroutable_closeout_pending"
            assert not (paths.terminal / path.name).exists()
            assert fd_count() - before_fd == 0
            submit.assert_not_called()
            return
        assert not path.exists(), "\n".join(projection_errors)
        canonical = read_json_file(paths.terminal / path.name)
        assert canonical["terminal_response"]["error_code"] == "GATEWAY_REQUEST_ROUTE_RETIRED"
        for key in ("conversation_claim", "lease_epoch", "active_turn_recovery", "queue_transport_source", "route", "metadata", "request_fingerprint"):
            assert canonical[key] == pending[key]
        binding = pending["conversation_claim"]
        assert store.claims.load(binding["thread_id"])["status"] == "failed"
        assert binding["task_id"] not in store.threads.require(binding["thread_id"]).active_task_ids
        assert not reservation_file(paths, path.stem).exists()
        # 原结算器将明确未消费的关联输入排回原队列，不得伪记consumed或丢掉输入。
        receipt = inputs.read_gateway_input_receipt(paths, "input-original")
        assert receipt.state == "queued" and receipt.target_turn_id == path.stem
        assert receipt.guidance_dedupe_key == "input-key-original" and not receipt.reconcile_error
        queued = read_json_file(paths.inbox / "input-original.json")
        inputs.require_gateway_prepared_identity(paths, queued)
        assert queued["metadata"]["expected_turn_id"] == path.stem
        assert store.guidance.receipt("input-key-original").status == "rejected"
        saved = (paths.terminal / path.name).read_bytes()
        repair_context = recovery._RecoveryContext(time.time(), 3, 10, False, control, None, admission_gate=gate)
        assert recovery.repair_gateway_terminal_projections(paths, agent=control,
            recovery_context=repair_context, full_scan=True)[1] == []
        assert (paths.terminal / path.name).read_bytes() == saved
        assert fd_count() - before_fd == 0
        submit.assert_not_called()


@pytest.mark.parametrize("mismatch", ["unrelated_terminal", "owner_id", "host_request_owner.v1", "route", "request_fingerprint"])
def test_retirement_resumer_protects_unrelated_or_mismatched_terminal(context, mismatch):
    """真实canonical producer建立终态；错误身份不能借退休重试覆写或删除pending。"""
    gate, path, ownership = ordinary_case(context)
    payload = read_json_file(path)
    stale = freeze_request_route(payload, ownership[0], RouteStamp(gate.count, "retired-negative", 1.0,
        payload["metadata"]["gateway_admin_binding_version"]))
    write_json_file_atomic(path, stale)
    response = closeout.unroutable_closeout_response(stale, "stale_generation_route")
    if mismatch == "unrelated_terminal":
        response = {"id": path.stem, "status": "done", "ok": True, "response": "original result", "ended_at": 1.0}
    canonical = recovery._terminal_gateway_request_payload(path.stem, stale, response)
    target = gate.paths.terminal / path.name
    recovery._commit_gateway_terminal_request(gate.paths, target, gate.paths.failed, path.stem, canonical)
    if mismatch != "unrelated_terminal":
        altered = copy.deepcopy(stale)
        if mismatch == "owner_id":
            altered[mismatch] = "providers/feishu/users/not-the-owner"
        elif mismatch == "host_request_owner.v1":
            altered[mismatch]["owner_id"] = "not-the-owner"
        elif mismatch == "route":
            altered[mismatch]["c_run_id"] = "different-retired-generation"
        else:
            altered[mismatch] = "0" * 64
        write_json_file_atomic(path, altered)
    before, pending = target.read_bytes(), path.read_bytes()
    submit = Mock(side_effect=AssertionError("terminal requests cannot execute"))
    for _ in range(2):
        assert request_worker.dispatch_pending_requests(gate.paths, AdmissionLimits(8, 8), submit, admission_gate=gate) == 0
        assert gate.try_close_unroutable_pending(path)["status"] == "blocked"
    assert target.read_bytes() == before and path.read_bytes() == pending
    assert not (gate.paths.responses / path.name).exists()
    assert not reservation_file(gate.paths, path.stem).exists()
    submit.assert_not_called()


# LLM: 创建独立legacy输入而非剥除已有请求标签；沿既有cold helper的冻结owner和实际会话构造原claim。
# 函数用途: 在真实C临时根中添加一个无路由的Count0遗留processing。
def _legacy_processing(original, gate, *, claim=True):
    prior = read_json_file(original.claim.path)
    rid = "legacy-processing"
    payload = {"id": rid, "kind": "ask", "goal": "旧单进程未完成输入", "status": "processing",
               "user_id": prior.get("user_id") or prior.get("metadata", {}).get("user_id", "p2-user"), "owner_id": prior.get("owner_id"),
               "host_request_owner.v1": copy.deepcopy(prior["host_request_owner.v1"]),
               "conversation": copy.deepcopy(prior.get("conversation", {})),
               "metadata": {key: value for key, value in prior.get("metadata", {}).items()
                            if key in {"channel", "provider", "user_id", "chat_id", "thread_id"}},
               "lease_started_at": 1, "lease_heartbeat_at": 1,
               "lease_process_identity": {"synthetic_dead": True},
               "execution_attempt_id": "legacy-attempt", "lease_owner": "legacy-attempt", "lease_epoch": 1}
    if claim:
        binding = dict(prior["conversation_claim"], request_id=rid, task_id="gateway:" + rid)
        payload["conversation_claim"] = binding
        original.store.tasks.bind({"task_id": binding["task_id"], "thread_id": binding["thread_id"], "status": "active"})
        assert original.store.claims.acquire({"thread_id": binding["thread_id"], "task_id": binding["task_id"],
                                              "recover_same_task_only": True})
    _fingerprint(payload)
    path = gate.paths.processing / (rid + ".json")
    write_json_file_atomic(path, payload)
    return path, payload


# LLM: 真实恢复入口保留指纹校验及stale前置，不直接绕过产品分流。
# 函数用途: 恢复单个合成遗留文件，不误恢复helper中另一条对照请求。
def _recover_one(gate, context, path):
    return recovery._recover_one_processing_request(gate.paths, path, context, read_json_file_report(path))


# LLM: 冷C不能创建业务Agent，原claim与活动task由真实ConversationStore收尾；无P3、无重排、无重执行。
# 函数用途: 验证明确死亡的Count0遗留processing可收口并解除原会话占用。
def test_mp_closes_out_legacy_count0_processing_with_dead_lease(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch) as (original, _control, gate, context):
        path, payload = _legacy_processing(original, gate)
        monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
        assert _recover_one(gate, context, path) == "failed"
        terminal = read_json_file(gate.paths.terminal / path.name)
        response = terminal["terminal_response"]
        assert set(response) == {"id", "ok", "status", "response", "error_code", "route_retirement_reason", "ended_at"}
        assert response["error_code"] == "GATEWAY_REQUEST_ROUTE_RETIRED"
        assert response["route_retirement_reason"] == "legacy_count0_request"
        assert terminal["request_fingerprint"] == payload["request_fingerprint"]
        assert terminal.get("route") == payload.get("route") and terminal["metadata"] == payload["metadata"]
        claim = payload["conversation_claim"]
        assert original.store.claims.load(claim["thread_id"])["status"] == "failed"
        assert claim["task_id"] not in original.store.threads.require(claim["thread_id"]).active_task_ids
        assert not reservation_file(gate.paths, path.stem).exists()
        assert not path.exists() and not (gate.paths.inbox / path.name).exists()
        before = (gate.paths.terminal / path.name).read_bytes()
        assert pr.recover_stale_processing(gate.paths, gate.paths.terminal / path.name, terminal, context,
                                           recovery._settle_committed_terminal) == "archived"
        assert (gate.paths.terminal / path.name).read_bytes() == before
        assert read_json_file(gate.paths.responses / path.name) == response


# LLM: 不stale且executor未知，以及即使stale但executor活着，都拒绝；两轮只写一次诊断、不动任何请求字节。
# 函数用途: 防止不确定执行者被拓扑切换误杀。
@pytest.mark.parametrize("live", [True, None])
def test_mp_keeps_legacy_processing_when_lease_live_or_unknown(tmp_path, monkeypatch, caplog, live):
    with cold_case(tmp_path, monkeypatch) as (original, _control, gate, context):
        path, payload = _legacy_processing(original, gate, claim=False)
        monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: live)
        monkeypatch.setattr(recovery, "_processing_request_stale", lambda *_args: live is True)
        before = path.read_bytes()
        for _ in range(2):
            with pytest.raises(pr.RequestExecutionOutcomeUncertainError) as caught:
                pr.recover_stale_processing(gate.paths, path, payload, context, recovery._settle_stale_processing)
            assert caught.value.phase == "processing_recovery.executor_unproven"
        assert path.read_bytes() == before
        assert not (gate.paths.terminal / path.name).exists()
        assert sum("executor_unproven" in record.message for record in caplog.records) == 1


# LLM: legacy请求全新构造，无输入receipt保持原兼容合同；未发布过路由与指纹，不对现有输入改签。
# 函数用途: 为未盖章pending准备同owner、同会话的合成旧请求。
def _legacy_pending(gate, *, fingerprint=False, abv=False):
    payload = _count0_steer("legacy-pending")
    payload["host_request_owner.v1"] = {"provider": "feishu", "owner_kind": "user", "owner_id": "fixture-alice"}
    payload["owner_id"] = "providers/feishu/users/fixture-alice"
    payload["user_id"] = "fixture-alice"
    payload["metadata"] = {"channel": "feishu", "user_id": "fixture-alice"}
    if abv:
        payload["metadata"]["gateway_admin_binding_version"] = "none"
    payload["status"] = "pending"
    if fingerprint:
        _fingerprint(payload)
    path = gate.paths.inbox / (payload["id"] + ".json")
    write_json_file_atomic(path, payload)
    return path, payload, (frozen_request_owner(payload), session_scope_for_request(payload))


# LLM: 从真实ordinary helper取得gate，但遗留输入单独创建，不改变原prepared消息或P3。
# 函数用途: 验证缺指纹或缺版本的pending会终结而非永久request_invalid。
@pytest.mark.parametrize("abv", [False, True])
def test_mp_closes_out_unstamped_pending(context, abv):
    gate, _path, _ownership = ordinary_case(context)
    path, payload, ownership = _legacy_pending(gate, abv=abv)
    result = gate.admit(path, ownership, AdmissionLimits(8, 8))
    assert result["status"] == "expired" and result["closeout"] == "unroutable"
    response = read_json_file(gate.paths.responses / path.name)
    assert response == result["response"] and response["error_code"] == "GATEWAY_REQUEST_ROUTE_RETIRED"
    assert response["route_retirement_reason"] == ("legacy_unstamped_request" if abv else "legacy_count0_request")
    assert not path.exists() and not reservation_file(gate.paths, path.stem).exists()
    terminal = read_json_file(gate.paths.terminal / path.name)
    assert terminal.get("route") == payload.get("route") and terminal["metadata"] == payload["metadata"]


# LLM: W4只移设施后的真实形态：保留完整旧route/prepared/fingerprint，直接构造无P3旧代次输入。
# 函数用途: 旧路由pending收口后保留原不可变事实并结清queued输入。
def test_mp_closes_out_stale_route_pending_without_reservation(context):
    gate, path, ownership = ordinary_case(context)
    payload = read_json_file(path)
    stale = freeze_request_route(payload, ownership[0], RouteStamp(gate.count, "retired-c", 1.0,
                                         payload["metadata"]["gateway_admin_binding_version"]))
    write_json_file_atomic(path, stale)
    result = gate.admit(path, ownership, AdmissionLimits(8, 8))
    assert result["status"] == "expired", result
    terminal = read_json_file(gate.paths.terminal / path.name)
    assert terminal["route"] == stale["route"] and terminal["request_fingerprint"] == stale["request_fingerprint"]
    assert result["response"]["route_retirement_reason"] == "stale_generation_route"
    assert not reservation_file(gate.paths, path.stem).exists() and not path.exists()
    assert inputs.read_gateway_input_receipt(gate.paths, path.stem).state == "terminal_unknown"


# LLM: 原P3不论旧代与否都是权威，predicate不抢恢复权，不放宽既有takeover拒绝。
# 函数用途: 防止有原预留的旧路由被误收口。
def test_mp_stale_route_with_reservation_uses_normal_path(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch) as (original, _control, gate, context):
        payload = read_json_file(original.claim.path)
        assert payload["route"]["c_run_id"] != gate.c_run_id
        assert closeout.unroutable_reason(gate.paths, payload, gate=gate) == ""
        report = recovery.recover_gateway_processing_requests_report(gate.paths, params=context)
        assert not report.load_errors and report.summary["requeued"] == 1
        assert reservation_file(gate.paths, original.claim.request_id).is_file()
        assert not (gate.paths.terminal / original.claim.path.name).exists()


# LLM: 测试只移除临时home的原P3模拟已退休设施，完整请求route/版本/指纹保持；判死叶显式合成。
# 函数用途: 无预留旧代processing直接终结，不进入P2等待或补章。
def test_mp_closes_out_stale_route_processing_without_reservation(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch) as (original, _control, gate, context):
        payload = read_json_file(original.claim.path)
        reservation_file(gate.paths, original.claim.request_id).unlink()
        monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
        monkeypatch.setattr(gate._runtime_takeover, "recover_processing", lambda *_args: pytest.fail("must not enter P2"))
        assert _recover_one(gate, context, original.claim.path) == "failed"
        terminal = read_json_file(gate.paths.terminal / original.claim.path.name)
        assert terminal["route"] == payload["route"] and terminal["request_fingerprint"] == payload["request_fingerprint"]
        assert terminal["terminal_response"]["route_retirement_reason"] == "stale_generation_route"
        assert not reservation_file(gate.paths, original.claim.request_id).exists()


# LLM: 独立无C根构造完整MP印记，不改指纹；真实Count0生命周期锁是唯一新增放行条件。
# 函数用途: 验证同一带印记processing锁外拒绝、锁内按原Count0策略重排。
def test_count0_recovers_mp_stamped_processing_under_lifecycle(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "count0")
    recovery._ensure_recovery_dirs(paths)
    payload = _count0_steer("count0-stamped")
    payload["metadata"] = {"gateway_admin_binding_version": "none"}
    payload.update(status="processing", lease_heartbeat_at=1, attempts=1)
    _fingerprint(payload)
    payload = freeze_request_route(payload, frozen_request_owner(payload), RouteStamp(1, "retired-c", 1.0, "none"))
    path = paths.processing / (payload["id"] + ".json")
    write_json_file_atomic(path, payload)
    context = recovery._RecoveryContext(time.time(), 3, 10, False, None, None)
    assert pr.processing_recovery_required(context, payload) is True
    with pytest.raises(pr.RequestExecutionContextUnavailable):
        pr.recover_stale_processing(paths, path, payload, context, recovery._settle_stale_processing)
    assert not closeout.standalone_lifecycle_held_for(paths.root)
    with standalone_lifecycle_guard(paths.root):
        assert closeout.standalone_lifecycle_held_for(paths.root)
        assert pr.recover_stale_processing(paths, path, payload, context, recovery._settle_stale_processing) == "requeued"
    assert not closeout.standalone_lifecycle_held_for(paths.root)
    pending = read_json_file(paths.inbox / path.name)
    assert pending["route"] == payload["route"] and pending["request_fingerprint"] == payload["request_fingerprint"]
    assert pending["metadata"] == payload["metadata"] and not reservation_file(paths, path.stem).exists()


# LLM: 任意合法已提交答复优先于退休失败；冷C仍需原owner会话来源，不能覆写canonical的任何字节。
# 函数用途: 验证有终态的无路由processing只补交原成功结果。
def test_existing_terminal_never_overwritten(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch) as (original, _control, gate, context):
        path, payload = _legacy_processing(original, gate)
        response = {"id": path.stem, "ok": True, "status": "done", "response": "原已完成答复", "ended_at": 1.0}
        canonical = recovery._terminal_gateway_request_payload(path.stem, payload, response)
        target = gate.paths.terminal / path.name
        recovery._commit_gateway_terminal_request(gate.paths, target, gate.paths.done, path.stem, canonical)
        before = target.read_bytes()
        assert _recover_one(gate, context, path) == "archived"
        assert target.read_bytes() == before and not path.exists()
        assert read_json_file(gate.paths.responses / path.name) == response
        assert not reservation_file(gate.paths, path.stem).exists()


# LLM: 回执由原控制产品入口创建，guard退出仅模拟代次退役，不调用任何控制业务或创建远端Agent。
# 函数用途: 创建一个准备好却未执行的旧代控制回执。
def _prepared_control(paths):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
        bind_coordinator_generation,
        coordinator_guard,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    scope = GatewayControlScope("alice", "feishu", "room", metadata={"message_id": "retired-control"},
                                resolved_owner=OwnerIdentity.provider_user("feishu", "alice"))
    with coordinator_guard(paths.root, 2):
        bind_coordinator_generation(paths.root, 2, "retired-control-c")
        return ops.execute_gateway_control_operation(SimpleNamespace(), paths,
            parse_conversation_control("/route true", reject_unknown_slash=True), scope, command_text="/route true")


# LLM: prepared无executor也能封口，但同代prepared不动；使用原op try锁和真实回执重读。
# 函数用途: 验证旧代prepared控制不会永久挂起或重放。
@pytest.mark.parametrize("count0", [False, True])
def test_control_prepared_receipt_for_retired_generation_sealed(tmp_path, count0):
    from contextlib import nullcontext

    from agent_py_agent.agent.gateway_parts import control_operation_service as ops
    from agent_py_agent.agent.gateway_parts.multiprocess_control_forward import (
        read_remote_control_status,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
        bind_coordinator_generation,
        coordinator_guard,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "control")
    receipt = _prepared_control(paths)
    with nullcontext() if count0 else coordinator_guard(paths.root, 2):
        if not count0:
            bind_coordinator_generation(paths.root, 2, "current-control-c")
        result = read_remote_control_status(paths, receipt)
        assert result.state == "terminal_unknown"
        assert result.error == {"error_code": "CONTROL_EXECUTOR_DEAD", "message":
                               "dispatch generation retired; control was not executed and will not be replayed"}
        assert ops.read_gateway_control_operation(paths, receipt.operation_id) == result
        assert read_remote_control_status(paths, result) == result
        assert result.dispatch == receipt.dispatch


# LLM: 执行者P0身份是显式合成，三态活性叶单独替换；产品回执schema、路由绑定和锁路径保持。
# 函数用途: 验证Count0仅封明确死亡的claimed/executing控制，live/unknown保留。
@pytest.mark.parametrize("state", ["claimed", "executing"])
@pytest.mark.parametrize("live", [False, True, None])
def test_control_count0_dead_executor_sealed(tmp_path, monkeypatch, state, live):
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops
    from agent_py_agent.agent.gateway_parts import multiprocess_control_identity as identity
    from agent_py_agent.agent.gateway_parts.multiprocess_control_forward import (
        read_remote_control_status,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "control")
    receipt = _prepared_control(paths)
    executor = {"schema_version": "multiprocess_process_identity.v1", "host_id": "synthetic-host", "pid": 123,
                "start_time": "synthetic-birth"}
    receipt = replace(receipt, state=state, dispatch=dict(receipt.dispatch, executor=executor, instance_id="old-instance"))
    path = ops.gateway_control_operation_path(paths, receipt.operation_id)
    write_json_file_atomic(path, receipt.to_dict())
    before = path.read_bytes()
    monkeypatch.setattr(identity, "control_executor_live", lambda _executor: live)
    result = read_remote_control_status(paths, receipt)
    assert result.state == ("terminal_unknown" if live is False else state)
    if live is False:
        assert result.error["error_code"] == "CONTROL_EXECUTOR_DEAD"
    else:
        assert path.read_bytes() == before


# LLM: 分类登记必须可查询且提示重发，不把旧请求自动重排当retryable。
# 函数用途: 验证退休错误码具备冻结的中文提示和可重试分类。
def test_error_code_registered():
    result = error_contract("GATEWAY_REQUEST_ROUTE_RETIRED")
    assert result.code == "GATEWAY_REQUEST_ROUTE_RETIRED"
    assert result.category == "state" and result.retryable is True
    assert result.recovery_hint == "网关拓扑切换或旧代次退役，本请求未在当前拓扑执行；请重新发送。"


# LLM: 投影中断保留inbox和已封终态，重试不得被steer-specific校验误拦或改变ended_at。
# 函数用途: 验证pending退休收口可在下一次admit幂等补交。
def test_unroutable_pending_projection_retries_without_rewriting_terminal(context, monkeypatch):
    gate, _path, _ownership = ordinary_case(context)
    path, _payload, ownership = _legacy_pending(gate)
    project = recovery._project_recovered_terminal_response
    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", lambda *_args: (_ for _ in ()).throw(OSError("projection interrupted")))
    result = gate.admit(path, ownership, AdmissionLimits(8, 8))
    assert result["reason"] == "unroutable_projection_pending" and path.exists()
    target = gate.paths.terminal / path.name
    before = target.read_bytes()
    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", project)
    assert gate.admit(path, ownership, AdmissionLimits(8, 8))["status"] == "expired"
    assert not path.exists() and target.read_bytes() == before


# LLM: 坏预留占位而不是缺失，predicate只读不动请求，不夺P3权威。
# 函数用途: 验证不可读reservation不会触发退休收口。
def test_unreadable_reservation_never_means_unroutable(context):
    gate, _path, _ownership = ordinary_case(context)
    path, payload, ownership = _legacy_pending(gate)
    reservation = reservation_file(gate.paths, path.stem)
    reservation.parent.mkdir(parents=True, exist_ok=True)
    reservation.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    assert closeout.unroutable_reason(gate.paths, payload, gate=gate) == ""
    assert gate.admit(path, ownership, AdmissionLimits(8, 8))["reason"] == "reservation_unreadable"
    assert path.read_bytes() == before and not (gate.paths.terminal / path.name).exists()


# LLM: 明确复现owner入口先拒旧代route，再调用新增接入口；真实T/C/lease和canonical投影不替换，投影只能在外层锁退出后发生。
# 函数用途: 验证调度器可在owner校验前独立结清无P3的旧代pending。
def test_try_close_unroutable_pending_precedes_owner_check_and_projects_outside_lock(context, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
    from agent_py_agent.agent.gateway_parts.request_worker import (
        OwnerRouteMismatchError,
        _durable_request_owner,
    )

    gate, path, ownership = ordinary_case(context)
    current = read_json_file(path)
    stale = freeze_request_route(current, ownership[0], RouteStamp(gate.count, "retired-before-owner", 1.0,
                                          current["metadata"]["gateway_admin_binding_version"]))
    write_json_file_atomic(path, stale)
    with pytest.raises(OwnerRouteMismatchError):
        _durable_request_owner(gate, stale)
    transition = admission.gateway_turn_transition
    project = closeout.project_unroutable_closeout
    held, projected = [], []

    # LLM: 只记录原T入口是否仍在作用域，实际flock保持原实现，不以替身授权收口。
    # 函数用途: 为投影时机断言记录新增入口的真实锁区间。
    @contextmanager
    def observe_transition(paths, request_id):
        with transition(paths, request_id):
            held.append(request_id)
            try:
                yield
            finally:
                held.pop()

    # LLM: 在原投影器重新取T之前核外层已释放T/M/L，避免测试因非重入flock死锁而挂起。
    # 函数用途: 验证先退出短提交窗口再完成真实投影。
    def observe_projection(*args, **kwargs):
        assert held == []
        assert not gate._runtime_takeover._mutex._is_owned()
        assert not gate._runtime_takeover._lease._lifecycle_gate._is_owned()
        projected.append(path.stem)
        return project(*args, **kwargs)

    monkeypatch.setattr(admission, "gateway_turn_transition", observe_transition)
    monkeypatch.setattr(closeout, "project_unroutable_closeout", observe_projection)
    decision = gate.try_close_unroutable_pending(path, snapshot=stale)
    assert decision == {"status": "expired", "response": decision["response"], "closeout": "unroutable"}
    assert projected == [path.stem]
    assert decision["response"]["route_retirement_reason"] == "stale_generation_route"
    assert decision["response"]["error_code"] == "GATEWAY_REQUEST_ROUTE_RETIRED"
    terminal = read_json_file(gate.paths.terminal / path.name)
    assert terminal["route"] == stale["route"] and terminal["metadata"] == stale["metadata"]
    assert terminal["request_fingerprint"] == stale["request_fingerprint"]
    assert read_json_file(gate.paths.responses / path.name) == decision["response"]
    assert inputs.read_gateway_input_receipt(gate.paths, path.stem).state == "terminal_unknown"
    assert not path.exists() and not reservation_file(gate.paths, path.stem).exists()


# LLM: legacy输入没有route或完整印记，新入口不依赖caller的owner校验，也不创建执行预留。
# 函数用途: 验证新增入口能收口两类旧单进程pending。
@pytest.mark.parametrize("abv", [False, True])
def test_try_close_unroutable_pending_closes_legacy_input(context, abv):
    gate, _path, _ownership = ordinary_case(context)
    path, payload, _ownership = _legacy_pending(gate, abv=abv)
    decision = gate.try_close_unroutable_pending(path, snapshot=payload)
    assert decision["status"] == "expired" and decision["closeout"] == "unroutable"
    assert decision["response"]["route_retirement_reason"] == ("legacy_unstamped_request" if abv else "legacy_count0_request")
    assert read_json_file(gate.paths.responses / path.name) == decision["response"]
    assert not path.exists() and not reservation_file(gate.paths, path.stem).exists()


# LLM: 快照只是扫描线索；原门内必须重读当前文件，正常请求不能被旧快照误收口或获得新P3。
# 函数用途: 验证非unroutable请求返回None且保留原字节。
def test_try_close_unroutable_pending_rereads_normal_request(context):
    gate, path, ownership = ordinary_case(context)
    current = read_json_file(path)
    stale_snapshot = freeze_request_route(current, ownership[0], RouteStamp(gate.count, "retired-scan", 1.0,
                                                  current["metadata"]["gateway_admin_binding_version"]))
    before = path.read_bytes()
    assert gate.try_close_unroutable_pending(path, snapshot=stale_snapshot) is None
    assert path.read_bytes() == before
    assert not (gate.paths.terminal / path.name).exists() and not reservation_file(gate.paths, path.stem).exists()


# LLM: 有预留或读不出预留都交还原P3链；不能把错误或旧代route当成无权威证据。
# 函数用途: 验证独立入口保留可读及不可读的原预留。
@pytest.mark.parametrize("unreadable", [False, True])
def test_try_close_unroutable_pending_preserves_reservation(context, unreadable):
    gate, path, ownership = ordinary_case(context)
    current = read_json_file(path)
    if unreadable:
        stale = freeze_request_route(current, ownership[0], RouteStamp(gate.count, "retired-with-p3", 1.0,
                                            current["metadata"]["gateway_admin_binding_version"]))
        write_json_file_atomic(path, stale)
        reservation = reservation_file(gate.paths, path.stem)
        reservation.parent.mkdir(parents=True, exist_ok=True)
        reservation.write_text("{broken", encoding="utf-8")
    else:
        assert gate.admit(path, ownership, AdmissionLimits(8, 8))["status"] == "admitted"
        reservation = reservation_file(gate.paths, path.stem)
    before, p3_before = path.read_bytes(), reservation.read_bytes()
    assert gate.try_close_unroutable_pending(path) is None
    assert path.read_bytes() == before and reservation.read_bytes() == p3_before
    assert not (gate.paths.terminal / path.name).exists()


# LLM: 已封canonical在新入口重试时原样复用；投影失败不得返回expired，也不丢原pending补交责任。
# 函数用途: 验证新增入口投影中断后可幂等重试。
def test_try_close_unroutable_pending_retries_projection_without_restamping(context, monkeypatch):
    gate, _path, _ownership = ordinary_case(context)
    path, payload, _ownership = _legacy_pending(gate, fingerprint=True)
    project = recovery._project_recovered_terminal_response

    # LLM: 只替换展示写叶模拟I/O中断，真实canonical提交及原inbox保留逻辑不变。
    # 函数用途: 制造可重试的投影失败。
    def fail_projection(*_args):
        raise OSError("projection interrupted before dispatcher retry")

    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", fail_projection)
    decision = gate.try_close_unroutable_pending(path, snapshot=payload)
    assert decision["status"] == "blocked" and decision["reason"] == "unroutable_projection_pending"
    assert path.exists()
    canonical = gate.paths.terminal / path.name
    before = canonical.read_bytes()
    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", project)
    decision = gate.try_close_unroutable_pending(path, snapshot=payload)
    assert decision["status"] == "expired"
    assert canonical.read_bytes() == before and not path.exists()
    assert read_json_file(gate.paths.responses / path.name) == decision["response"]


# LLM: 原C来源关闭后即使扫描快照仍合法也不得写；保留pending，不假造终态或回落普通执行。
# 函数用途: 验证新增入口仍受真实提交末门约束。
def test_try_close_unroutable_pending_rejects_closed_coordinator(context):
    gate, _path, _ownership = ordinary_case(context)
    path, payload, _ownership = _legacy_pending(gate, fingerprint=True)
    before = path.read_bytes()
    gate._runtime_takeover._lease._closing = True
    decision = gate.try_close_unroutable_pending(path, snapshot=payload)
    assert decision["status"] == "blocked"
    assert path.read_bytes() == before and not (gate.paths.terminal / path.name).exists()
