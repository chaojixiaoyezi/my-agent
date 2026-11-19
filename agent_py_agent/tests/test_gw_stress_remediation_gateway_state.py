# LLM: GW-18 回归：停机保留大量模型调用后，Gateway 自己写下的 gateway_state.json 必须能被下一次启动的
#   全部读者读回；裁决仍按完整行，state 只存每原因 count + 规范化摘要（无逐条明细），完整证明留在重启
#   完成 marker；超读者上限给出 retirement_source_oversized 与实际字节数。
# 模块用途: 覆盖写者投影/上限、原 16 KiB 读者放宽、旧版超大状态自愈、交接读回核对与 oversized 原因；
#   R1（F01/C1）另覆盖 16 KiB 新写预算、starting/接班/取消通知/完成回执/排空写口的投影，以及拒写时原字节保留、缺失不新建。
from __future__ import annotations

import json
import os
import random
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.nofollow_fs import ManagedFileReadLimitExceeded, read_bytes_beneath
from agent_py_agent.agent.gateway_parts import multiprocess_generation_retirement as retirement
from agent_py_agent.agent.gateway_parts import restart_service as service
from agent_py_agent.agent.gateway_parts.gateway_state_bounds import (
    GATEWAY_STATE_MAX_BYTES,
    GATEWAY_STATE_WRITE_MAX_BYTES,
    GatewayStateOversized,
    fit_gateway_state,
    fit_or_refuse,
    gateway_state_bytes,
    project_gateway_state_lists,
    project_refs,
    ref_count,
    refs_digest,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_sources import _role_snapshot
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_sources import (
    StandaloneSourceUnproven,
    require_standalone_sources,
)
from agent_py_agent.cli import gateway_process

# 旧读者的 16 KiB 门槛：MAX2 实测 94 条保留调用即越过它；紧凑投影必须远低于它。
_LEGACY_READER_BYTES = 16384


def _paths(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    return paths


def _retained(count: int) -> list[dict]:
    return [{"reason": "model_call_retained", "ref": f"model-call:{index:032x}"} for index in range(count)]


def _cleanup(count: int) -> dict:
    return {
        "status": "cleanup", "pid": os.getpid(), "drain_complete": False,
        "alive_threads": [],
        "background_blockers": _retained(count)
        + [{"reason": "bounded_call_retained", "ref": f"bounded:{i}", "purpose": "model"} for i in range(count // 4)],
        "unresolved_operation_refs": [{"reason": "foreground_turn_unsettled", "ref": "gateway_admission", "count": 143}]
        + [{"tool": "run_shell", "request_id": f"req-{i}", "admitted_at": 1.0} for i in range(count)],
        "interrupted_model_calls": count,
    }


def _assert_no_detail_rows(rows):
    for row in rows:
        assert set(row) <= {"reason", "count", "digest", "purpose", "ref"} and row["count"] >= 1
        # 投影行带规范化摘要；上游汇总行（reason/ref/purpose/count）本身无明细，原样保留。
        assert "digest" not in row or row["digest"].startswith("sha256:")


def test_project_refs_keeps_counts_and_digest_without_detail_rows():
    rows = _retained(200) + [{"reason": "foreground_turn_unsettled", "ref": "gateway_admission", "count": 143}]
    projected = project_refs(rows)
    _assert_no_detail_rows(projected)
    by_reason = {row["reason"]: row for row in projected}
    assert by_reason["model_call_retained"]["count"] == 200 and "ref" not in by_reason["model_call_retained"]
    # 上游已汇总的前台 count 行原样保留（不是逐条样本），旧读者与回读断言不变。
    assert by_reason["foreground_turn_unsettled"] == rows[-1]
    assert project_refs(rows[-1:] * 2) == rows[-1:] * 2  # 全是紧凑行：原样（幂等）
    mixed = [rows[-1], {"reason": "foreground_turn_unsettled", "ref": "turn-x"}]
    assert project_refs(mixed) == [{"reason": "foreground_turn_unsettled", "count": 144, "digest": refs_digest(mixed)}]
    assert by_reason["model_call_retained"]["digest"] == refs_digest(list(reversed(rows[:200])))
    assert ref_count(projected) == ref_count(rows) == 343
    assert project_refs(projected) == projected
    assert project_refs([]) == []
    assert project_refs(["heartbeat", "request"]) == [
        {"reason": "heartbeat", "count": 1, "digest": refs_digest(["heartbeat"])},
        {"reason": "request", "count": 1, "digest": refs_digest(["request"])}]


def test_projection_reaches_nested_results_without_mutating_input():
    cleanup = _cleanup(200)
    payload = {"cleanup_result": {**cleanup, "cleanup_result": dict(cleanup)}}
    projected = project_gateway_state_lists(payload)
    assert len(payload["cleanup_result"]["background_blockers"]) == 250
    inner = projected["cleanup_result"]["cleanup_result"]
    assert ref_count(inner["background_blockers"]) == 250
    assert ref_count(inner["unresolved_operation_refs"]) == 143 + 200
    _assert_no_detail_rows(inner["background_blockers"])
    assert inner["alive_threads"] == []


@pytest.mark.parametrize("seed", range(12))
def test_every_fitted_state_stays_compact(seed):
    rng = random.Random(seed)
    count = rng.choice([0, 1, 8, 9, 94, 133, 500, 4000])
    state = {"status": "failed", "cleanup_result": _cleanup(count), "unresolved_operation_refs": _retained(count),
             "padding": "x" * rng.randrange(0, 4096)}
    fitted = fit_gateway_state(state)
    assert fitted is not None
    assert gateway_state_bytes(fitted) <= _LEGACY_READER_BYTES <= GATEWAY_STATE_MAX_BYTES
    assert ref_count(fitted["cleanup_result"]["background_blockers"]) == count + count // 4


def test_stop_with_200_retained_calls_then_restart_twice(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    events = []
    monkeypatch.setattr(gateway_process, "log_gateway_event", lambda _agent, name, payload: events.append((name, payload)))
    write_json_file_atomic(paths.state, {"status": "running", "pid": os.getpid()})
    for _ in range(2):
        # 停机：200 条保留的模型调用 + 200 个执行中工具 + 143 个未结前台回合。
        gateway_process._record_gateway_drain_failure(paths, None, os.getpid(), _cleanup(200))
        assert paths.state.stat().st_size < _LEGACY_READER_BYTES
        state = json.loads(paths.state.read_text())
        assert state["error_code"] == "GATEWAY_DRAIN_INCOMPLETE"
        assert ref_count(state["cleanup_result"]["background_blockers"]) == 250
        _assert_no_detail_rows(state["cleanup_result"]["background_blockers"])
        # 完整明细仍在事件里。
        assert len(events[-1][1]["cleanup_result"]["background_blockers"]) == 250
        # 重启：单进程来源门与代次退役（原 16 KiB 读者）都能读回本代状态。
        with standalone_lifecycle_guard(paths.root) as lifecycle:
            require_standalone_sources(paths, lifecycle)
            assert retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle) is None
        assert set(_role_snapshot(paths.root)) == {"gateway_state.json"}


def test_legacy_oversized_state_is_readable_and_healed_by_next_commit(tmp_path):
    paths = _paths(tmp_path)
    write_json_file_atomic(paths.state, {"status": "failed", "pid": 999_999, "cleanup_result": _cleanup(133)})
    assert _LEGACY_READER_BYTES < paths.state.stat().st_size < GATEWAY_STATE_MAX_BYTES
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        require_standalone_sources(paths, lifecycle)
        assert retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle) is None
    assert gateway_process._commit_gateway_state(paths, {"status": "starting", "pid": os.getpid()})
    assert paths.state.stat().st_size < _LEGACY_READER_BYTES


def test_state_above_shared_limit_reports_oversized_with_size(tmp_path, capsys):
    paths = _paths(tmp_path)
    write_json_file_atomic(paths.state, {"status": "stopped", "blob": "x" * (GATEWAY_STATE_MAX_BYTES + 10)})
    size = paths.state.stat().st_size
    with pytest.raises(ManagedFileReadLimitExceeded) as raised:
        read_bytes_beneath(paths.root, ("gateway_state.json",), max_bytes=GATEWAY_STATE_MAX_BYTES)
    assert str(raised.value) == "managed file exceeds read limit"
    assert isinstance(raised.value, ValueError) and raised.value.observed_bytes == size
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        with pytest.raises(StandaloneSourceUnproven, match="retirement_source_oversized"):
            retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        event = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert event["reason"] == "retirement_source_oversized"
        assert event["size_bytes"] == size and event["max_bytes"] == GATEWAY_STATE_MAX_BYTES
        with pytest.raises(StandaloneSourceUnproven, match=f"retirement_source_oversized：{size} 字节"):
            require_standalone_sources(paths, lifecycle)


def test_writer_refuses_payload_that_cannot_fit_and_keeps_readable_file(tmp_path, capsys):
    paths = _paths(tmp_path)
    write_json_file_atomic(paths.state, {"status": "running", "pid": os.getpid()})
    before = paths.state.read_bytes()
    assert not gateway_process._commit_gateway_state(paths, {"blob": "x" * GATEWAY_STATE_MAX_BYTES})
    assert paths.state.read_bytes() == before
    assert "gateway_state_oversized" in capsys.readouterr().err


@pytest.mark.parametrize("mode,allowed", [("fast", True), ("graceful", False)])
def test_restart_handover_projects_state_and_keeps_full_marker_proof(tmp_path, monkeypatch, mode, allowed):
    from agent_py_agent.cli import gateway_restart_handover as handover

    service.reset_restart_drain_state()
    paths = _paths(tmp_path)
    identity = {"pid": os.getpid(), "instance_id": "original"}
    context = SimpleNamespace(paths=paths, process_identity=identity, agent=SimpleNamespace())
    write_json_file_atomic(paths.state, {"pid": os.getpid(), "process_identity": identity, "started_at": 1.0})
    marker = service.mark_restart_drained(paths, {"request_id": "original", "restart_mode": mode},
        old_pid=os.getpid(), old_process_started_at=1.0, active_turns_at_exit=1,
        old_process_identity=identity, cleanup_result={"status": "pending"})
    monkeypatch.setattr(handover, "log_gateway_event", lambda *args: None)
    monkeypatch.setattr(handover, "append_restart_cancellation", lambda *args, **kwargs: {"notification_results": []})
    foreground = {"reason": "foreground_turn_unsettled", "ref": "gateway_admission", "count": 1}
    blockers = _retained(200)
    result = handover.finalize_restart_cleanup(context, marker, {
        "drain_complete": False, "alive_threads": [], "background_blockers": blockers,
        "unresolved_operation_refs": [foreground],
    })
    # 裁决按完整行：fast 下模型调用可续跑交接，graceful 下仍是未结清。
    assert result["handover_allowed"] is allowed
    full_refs = result["continuation_refs"] if allowed else result["unresolved_operation_refs"]
    assert all(row in full_refs for row in blockers)
    state = json.loads(paths.state.read_text())
    assert state["cleanup_result"] == project_gateway_state_lists(result)
    assert paths.state.stat().st_size < _LEGACY_READER_BYTES
    projected = state["cleanup_result"]["continuation_refs" if allowed else "unresolved_operation_refs"]
    _assert_no_detail_rows(projected)
    retained = next(row for row in projected if row["reason"] == "model_call_retained")
    assert retained["count"] == 200 and retained["digest"] == refs_digest(blockers)
    successor = service.consume_restart_marker(paths, old_pid_alive=lambda pid: False)
    if allowed:
        # marker 保留完整证明，摘要可按同一规则对账。
        assert successor["cleanup_result"] == result
        assert refs_digest([row for row in successor["cleanup_result"]["continuation_refs"]
                            if row.get("reason") == "model_call_retained"]) == retained["digest"]
    else:
        assert successor is None
    service.reset_restart_drain_state()


# ---- R1（F01/C1）：16 KiB 新写预算覆盖所有新状态写口，拒写不改字节、不新建文件 ----

def _notification_rows(count: int) -> list[dict]:
    return [{"requester_index": index, "request_id": "original", "event_type": "gateway_restart_cancelled",
             "thread_id": f"thread-{index:04d}", "status": "written", "reason": ""} for index in range(count)]


def _state_of_size(size: int) -> dict:
    state = {"status": "running", "pid": os.getpid(), "blob": ""}
    base = gateway_state_bytes(state)
    wide = gateway_state_bytes({**state, "blob": "汉"}) - base
    room = size - base
    state["blob"] = "汉" * (room // wide) + "a" * (room % wide)
    assert gateway_state_bytes(state) == size
    return state


def _drained_marker(paths, identity, mode, *, turns=1):
    return service.mark_restart_drained(paths, {"request_id": "original", "restart_mode": mode},
        old_pid=os.getpid(), old_process_started_at=1.0, active_turns_at_exit=turns,
        old_process_identity=identity, cleanup_result={"status": "pending"})


def test_singleton_detail_ref_is_dropped_but_shared_label_is_kept():
    single = [{"reason": "model_call_retained", "ref": "model-call:" + "0" * 32}]
    assert project_refs(single) == [{"reason": "model_call_retained", "count": 1, "digest": refs_digest(single)}]
    shared = [{"reason": "foreground_turn_unsettled", "ref": "gateway_admission"}] * 2
    assert project_refs(shared) == [{"reason": "foreground_turn_unsettled", "count": 2,
                                     "digest": refs_digest(shared), "ref": "gateway_admission"}]


def test_write_budget_is_exact_utf8_bytes(tmp_path):
    paths = _paths(tmp_path)
    assert GATEWAY_STATE_WRITE_MAX_BYTES == _LEGACY_READER_BYTES < GATEWAY_STATE_MAX_BYTES
    at_limit = _state_of_size(GATEWAY_STATE_WRITE_MAX_BYTES)
    assert fit_gateway_state(at_limit) == at_limit
    assert fit_gateway_state(_state_of_size(GATEWAY_STATE_WRITE_MAX_BYTES + 1)) is None
    with pytest.raises(GatewayStateOversized):
        fit_or_refuse(_state_of_size(GATEWAY_STATE_WRITE_MAX_BYTES + 1))
    assert gateway_process._commit_gateway_state(paths, at_limit, replace=True)
    assert paths.state.stat().st_size == GATEWAY_STATE_WRITE_MAX_BYTES


@pytest.mark.parametrize("existing", ["missing", "compact"])
def test_refused_commit_never_creates_or_rewrites_state(tmp_path, capsys, existing):
    paths = _paths(tmp_path)
    if existing == "compact":
        # 非规范空白（紧凑 JSON）：旧实现 return current 会按规范格式重写这些字节。
        paths.state.write_text(json.dumps({"status": "running", "pid": os.getpid()}), encoding="utf-8")
        os.chmod(paths.state, 0o600)
    before = paths.state.read_bytes() if paths.state.exists() else None
    assert not gateway_process._commit_gateway_state(paths, {"blob": "x" * GATEWAY_STATE_WRITE_MAX_BYTES})
    assert "gateway_state_oversized" in capsys.readouterr().err
    if existing == "compact":
        assert not gateway_process._commit_gateway_state(paths, {"status": "stopped"}, pid=os.getpid() + 1)
    assert (paths.state.read_bytes() if paths.state.exists() else None) == before


def test_successor_starting_state_projects_marker_cleanup(tmp_path, monkeypatch):
    from agent_py_agent.cli import gateway_restart_handover as handover

    service.reset_restart_drain_state()
    paths = _paths(tmp_path)
    identity = {"pid": os.getpid(), "instance_id": "original"}
    context = SimpleNamespace(paths=paths, process_identity=identity, agent=SimpleNamespace())
    write_json_file_atomic(paths.state, {"pid": os.getpid(), "process_identity": identity, "started_at": 1.0})
    monkeypatch.setattr(handover, "log_gateway_event", lambda *args: None)
    result = handover.finalize_restart_cleanup(context, _drained_marker(paths, identity, "fast"), {
        "drain_complete": False, "alive_threads": [], "background_blockers": _retained(200),
        "unresolved_operation_refs": []})
    assert result["handover_allowed"] is True
    successor = service.consume_restart_marker(paths, old_pid_alive=lambda pid: False)
    assert len(successor["cleanup_result"]["continuation_refs"]) == 200  # 完成标记保留完整证明
    # 接班 setup：恢复入口替身，只验证 starting 写口本身。
    monkeypatch.setattr(gateway_process, "recover_gateway_processing_requests",
                        lambda *args, **kwargs: {"requeued": 0, "failed": 0})
    monkeypatch.setattr(gateway_process, "_recover_gateway_stale_attempts",
                        lambda agent: {"count": 0, "error": None, "unidentified": []})
    monkeypatch.setattr(gateway_process, "log_gateway_event", lambda *args: None)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_request_max_attempts=3, gateway_processing_timeout_seconds=60.0),
                            subagents=SimpleNamespace(workspace=tmp_path / "subagents"))
    gateway_process._cmd_gateway_run_setup(agent, paths, restart_marker=successor)
    raw = paths.state.read_bytes()
    assert len(raw) <= GATEWAY_STATE_WRITE_MAX_BYTES and b"model-call:" not in raw
    state = json.loads(raw)
    assert state["status"] == "starting" and state["restart_request_id"] == "original"
    continuation = state["cleanup_result"]["continuation_refs"]
    _assert_no_detail_rows(continuation)
    assert ref_count(continuation) == 200
    service.reset_restart_drain_state()


def test_cancelled_handover_projects_96_notifications_and_keeps_full_marker(tmp_path, monkeypatch):
    from agent_py_agent.cli import gateway_restart_handover as handover

    service.reset_restart_drain_state()
    paths = _paths(tmp_path)
    identity = {"pid": os.getpid(), "instance_id": "original"}
    context = SimpleNamespace(paths=paths, process_identity=identity, agent=SimpleNamespace())
    write_json_file_atomic(paths.state, {"pid": os.getpid(), "process_identity": identity, "started_at": 1.0})
    rows = _notification_rows(96)
    monkeypatch.setattr(handover, "log_gateway_event", lambda *args: None)
    monkeypatch.setattr(handover, "append_restart_cancellation", lambda *args, **kwargs: {"notification_results": rows})
    result = handover.finalize_restart_cleanup(context, _drained_marker(paths, identity, "graceful"), {
        "drain_complete": False, "alive_threads": [], "background_blockers": _retained(200),
        "unresolved_operation_refs": []})
    assert result["handover_allowed"] is False and result["notification_results"] == rows
    raw = paths.state.read_bytes()
    assert len(raw) <= GATEWAY_STATE_WRITE_MAX_BYTES and b"thread-0001" not in raw
    state = json.loads(raw)
    assert state["cleanup_result"]["notification_results"] == [
        {"reason": "unlabeled", "count": 96, "digest": refs_digest(rows)}]
    assert state["restart_drain"]["phase"] == "cleanup_failed"
    marker = json.loads(service.restart_completed_path(paths).read_text(encoding="utf-8"))
    assert marker["cleanup_result"]["notification_results"] == rows
    service.reset_restart_drain_state()


def test_completion_receipt_notifications_are_projected(tmp_path):
    paths = _paths(tmp_path)
    write_json_file_atomic(paths.state, {"status": "running", "pid": os.getpid()})
    rows = _notification_rows(96)
    receipt = {"ready_observation": {"ready": True}, "cleanup_result": _cleanup(200), "notification_results": rows}
    assert gateway_process._commit_gateway_state(paths, receipt, pid=os.getpid())
    assert paths.state.stat().st_size <= GATEWAY_STATE_WRITE_MAX_BYTES
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    assert state["notification_results"] == [{"reason": "unlabeled", "count": 96, "digest": refs_digest(rows)}]
    assert ref_count(state["cleanup_result"]["background_blockers"]) == 250


def test_handover_refuses_oversized_merge_and_missing_state_without_writing(tmp_path, monkeypatch):
    from agent_py_agent.cli import gateway_restart_handover as handover

    paths = _paths(tmp_path)
    identity = {"pid": os.getpid(), "instance_id": "original"}
    context = SimpleNamespace(paths=paths, process_identity=identity, agent=SimpleNamespace())
    monkeypatch.setattr(handover, "log_gateway_event", lambda *args: None)
    monkeypatch.setattr(handover, "append_restart_cancellation", lambda *args, **kwargs: {"notification_results": []})

    def finalize():
        service.reset_restart_drain_state()
        return handover.finalize_restart_cleanup(context, _drained_marker(paths, identity, "fast", turns=0), {
            "drain_complete": True, "alive_threads": [], "background_blockers": [], "unresolved_operation_refs": []})

    # 缺失 state：不属本代，拒写且不新建 {}。
    result = finalize()
    assert result["handover_allowed"] is False and result["reason"] == "gateway_generation_changed"
    assert not paths.state.exists()
    # 合并后超出写预算：按 gateway_state_oversized 拒绝交接，原（非规范空白）字节保持。
    paths.state.write_text(json.dumps({"pid": os.getpid(), "process_identity": identity, "started_at": 1.0,
                                       "padding": "x" * GATEWAY_STATE_WRITE_MAX_BYTES}), encoding="utf-8")
    os.chmod(paths.state, 0o600)
    before = paths.state.read_bytes()
    result = finalize()
    assert result["handover_allowed"] is False and result["reason"] == "gateway_state_oversized"
    assert paths.state.read_bytes() == before
    assert service.consume_restart_marker(paths, old_pid_alive=lambda pid: False) is None
    service.reset_restart_drain_state()


def test_drain_state_is_projected_and_refused_over_budget(tmp_path):
    from agent_py_agent.cli import gateway_restart_handover as handover

    paths = _paths(tmp_path)
    write_json_file_atomic(paths.state, {"status": "running", "pid": os.getpid()})
    handover._publish_drain_state(paths, {"phase": "draining", "unresolved_operation_refs": _retained(500)})
    drain = json.loads(paths.state.read_text(encoding="utf-8"))["restart_drain"]
    assert drain["phase"] == "draining" and ref_count(drain["unresolved_operation_refs"]) == 500
    _assert_no_detail_rows(drain["unresolved_operation_refs"])
    paths.state.write_text(json.dumps({"status": "running", "padding": "x" * GATEWAY_STATE_WRITE_MAX_BYTES}),
                           encoding="utf-8")
    before = paths.state.read_bytes()
    handover._publish_drain_state(paths, {"phase": "draining"})
    assert paths.state.read_bytes() == before
