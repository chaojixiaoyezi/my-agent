from __future__ import annotations

"""Gateway safe restart: request, two-phase drain, handover marker, successor recovery and continuation."""

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    gateway_paths,
    read_json_file,
    recover_gateway_processing_requests,
    submit_gateway_ask,
    write_json_file,
)
from agent_py_agent.agent.gateway_parts import restart_service as service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_worker import _iter_pending_request_paths
from agent_py_agent.agent.gateway_parts.restart_control import execute_restart_control
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli import gateway_restart_handover as handover
from agent_py_agent.cli.gateway_process import (
    _classify_gateway_service_return,
    _record_gateway_service_termination,
)


@pytest.fixture(autouse=True)
def _idle_restart_state():
    service.reset_restart_drain_state()
    yield
    service.reset_restart_drain_state()


def _paths(tmp_path: Path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True, exist_ok=True)
    return paths


def _agent(root: Path, **overrides) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        gateway_workspace="gateway",
        local_store_path="local_store/local.db",
        local_store_files_dir="local_store/files",
        local_store_events_path="local_store/events.jsonl",
        **overrides,
    )
    return SimpleAgent(config, root)


def test_submit_schedules_then_coalesces_additional_requesters(tmp_path):
    paths = _paths(tmp_path)
    first = service.submit_restart_request(
        paths, target_pid=4321, requester={"kind": "agent_tool", "thread_id": "t1"},
        reason="加载新配置", cooldown_seconds=30,
    )
    second = service.submit_restart_request(
        paths, target_pid=4321, requester={"kind": "control_command", "thread_id": "t2"},
        reason="管理员 /restart", cooldown_seconds=30,
    )
    assert first["status"] == "scheduled"
    assert second["status"] == "coalesced"
    request = read_json_file(service.restart_request_path(paths))
    assert request["request_id"] == first["request"]["request_id"]
    assert request["reason"] == "加载新配置"
    assert request["additional_requesters"] == [{"kind": "control_command", "thread_id": "t2"}]
    assert service.pending_restart_request(paths, pid=4321)["request_id"] == request["request_id"]
    assert service.pending_restart_request(paths, pid=9999) is None, "只认目标进程"


def test_cooldown_after_drained_restart_and_zero_disables_it(tmp_path):
    paths = _paths(tmp_path)
    scheduled = service.submit_restart_request(
        paths, target_pid=1, requester={}, reason="r", cooldown_seconds=30,
    )
    service.mark_restart_drained(
        paths, scheduled["request"], old_pid=1, old_process_started_at=0.0, active_turns_at_exit=0,
    )
    refused = service.submit_restart_request(paths, target_pid=2, requester={}, reason="r", cooldown_seconds=30)
    assert refused["status"] == "cooldown"
    assert 0 < refused["retry_after_seconds"] <= 30
    assert not service.restart_request_path(paths).exists()
    allowed = service.submit_restart_request(paths, target_pid=2, requester={}, reason="r", cooldown_seconds=0)
    assert allowed["status"] == "scheduled"


def test_loop_guard_limits_restarts_from_one_thread(tmp_path):
    paths = _paths(tmp_path)
    now = time.time()
    for index in range(service.LOOP_GUARD_LIMIT_COUNT):
        result = service.submit_restart_request(
            paths, target_pid=100 + index, requester={"thread_id": "loop"}, reason="r",
            cooldown_seconds=0, now=now + index,
        )
        assert result["status"] == "scheduled"
        service.restart_request_path(paths).unlink()
    blocked = service.submit_restart_request(
        paths, target_pid=200, requester={"thread_id": "loop"}, reason="r", cooldown_seconds=0, now=now + 10,
    )
    assert blocked == {"status": "loop_guard", "recent_count": service.LOOP_GUARD_LIMIT_COUNT}
    other = service.submit_restart_request(
        paths, target_pid=200, requester={"thread_id": "other"}, reason="r", cooldown_seconds=0, now=now + 10,
    )
    assert other["status"] == "scheduled"
    later = now + service.LOOP_GUARD_WINDOW_SECONDS + 60
    service.restart_request_path(paths).unlink()
    assert service.submit_restart_request(
        paths, target_pid=201, requester={"thread_id": "loop"}, reason="r", cooldown_seconds=0, now=later,
    )["status"] == "scheduled"


def test_drain_waits_for_turns_then_executing_tools_and_keeps_gate_closed():
    turns = {"count": 1}
    release_tool = threading.Event()
    phases: list[str] = []

    def tool_worker():
        with restart_gate.tool_execution_admission():
            release_tool.wait(5)

    tool_thread = threading.Thread(target=tool_worker)
    tool_thread.start()
    time.sleep(0.1)

    def finish_turn_then_tool():
        time.sleep(0.4)
        assert service.restart_draining() is True
        turns["count"] = 0
        time.sleep(0.4)
        release_tool.set()

    threading.Thread(target=finish_turn_then_tool).start()
    result = service.run_restart_drain(
        {"request_id": "gwrestart-1"},
        active_turns=lambda: turns["count"],
        turn_wait_seconds=10,
        drain_timeout_seconds=10,
        on_progress=lambda facts: phases.append(str(facts["phase"])),
    )
    tool_thread.join(timeout=5)
    assert result == {"ok": True, "request_id": "gwrestart-1", "active_turns_at_exit": 0}
    assert phases == ["turn_wait", "tool_drain"]
    assert service.restart_phase()["phase"] == "exiting"
    assert restart_gate.tool_gate_closed() is True, "成功排空后关口保持关闭直到进程退出"


def test_turn_wait_timeout_moves_on_and_drain_timeout_cancels_without_killing():
    release_tool = threading.Event()

    def tool_worker():
        with restart_gate.tool_execution_admission():
            release_tool.wait(5)

    tool_thread = threading.Thread(target=tool_worker)
    tool_thread.start()
    time.sleep(0.1)
    try:
        started = time.monotonic()
        result = service.run_restart_drain(
            {"request_id": "gwrestart-2"},
            active_turns=lambda: 3,
            turn_wait_seconds=0.3,
            drain_timeout_seconds=0.3,
        )
        assert time.monotonic() - started < 3
    finally:
        release_tool.set()
        tool_thread.join(timeout=5)
    assert result == {"ok": False, "reason": "drain_timeout", "executing": 1, "request_id": "gwrestart-2"}
    assert service.restart_draining() is False
    assert restart_gate.tool_gate_closed() is False, "取消后恢复正常服务"


def test_restart_marker_is_consumed_once_only_after_old_process_exit(tmp_path):
    paths = _paths(tmp_path)
    request = service.submit_restart_request(
        paths, target_pid=77, requester={}, reason="r", cooldown_seconds=0,
    )["request"]
    service.mark_restart_drained(paths, request, old_pid=77, old_process_started_at=1.0, active_turns_at_exit=2)
    assert not service.restart_request_path(paths).exists()
    assert service.consume_restart_marker(paths, old_pid_alive=lambda pid: True) is None
    assert service.restart_completed_path(paths).exists(), "旧进程还活着时不消费"
    marker = service.consume_restart_marker(paths, old_pid_alive=lambda pid: False)
    assert marker["request_id"] == request["request_id"]
    assert marker["active_turns_at_exit"] == 2
    assert not service.restart_completed_path(paths).exists()
    assert service.consume_restart_marker(paths, old_pid_alive=lambda pid: False) is None


def test_stale_restart_marker_is_dropped(tmp_path):
    paths = _paths(tmp_path)
    request = service.submit_restart_request(paths, target_pid=5, requester={}, reason="r", cooldown_seconds=0)
    service.mark_restart_drained(paths, request["request"], old_pid=5, old_process_started_at=0.0, active_turns_at_exit=0)
    later = time.time() + service.MARKER_MAX_AGE_SECONDS + 5
    assert service.consume_restart_marker(paths, old_pid_alive=lambda pid: False, now=later) is None
    assert not service.restart_completed_path(paths).exists()


def test_continuation_wakes_requester_thread_and_skips_foreign_store(tmp_path):
    home = tmp_path / "home"
    store_root = home / "owners" / "local" / "conversations"
    store = ConversationStore(store_root)
    thread = store.threads.get_or_create({
        "canonical_user_id": "admin", "channel": "tui",
        "channel_conversation_id": "restart-case", "channel_user_id": "admin",
    })
    marker = {
        "request_id": "gwrestart-9", "old_pid": 11, "drained_at": time.time(),
        "requester": {"thread_id": thread.thread_id, "conversation_store_root": str(store_root)},
        "additional_requesters": [
            {"thread_id": "x", "conversation_store_root": str(tmp_path / "elsewhere")},
            {"kind": "control_command", "thread_id": "no-store"},
        ],
    }
    result = service.append_restart_continuations(marker, home_root=home)
    assert result == {"written": 1, "skipped": [{"thread_id": "x", "reason": "store_root_outside_home"}]}
    pending = ConversationStore(store_root, initialize=False).wakes.pending()
    assert [signal.reason for signal in pending] == [service.CONTINUATION_EVENT_TYPE]
    again = service.append_restart_continuations(marker, home_root=home)
    assert again["written"] == 1
    assert len(ConversationStore(store_root, initialize=False).wakes.pending()) == 1, "按请求编号去重"


def test_planned_restart_resumes_turns_immediately_and_first(tmp_path):
    agent = _agent(tmp_path, gateway_processing_timeout_seconds=1)
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    fresh_id, fresh_path, _ = submit_gateway_ask(paths, params=GatewayAskParams(prompt="重启期间的新消息", save=False))
    old = paths.processing / "gwreq-inflight.json"
    write_json_file(old, {
        "id": "gwreq-inflight", "kind": "ask", "prompt": "重启前在跑", "attempts": 1,
        "status": "processing", "turn_phase": "open", "created_at": time.time() + 5,
        "lease_started_at": time.time() - 10,
    })
    recovered = recover_gateway_processing_requests(
        paths, startup=True, max_attempts=2, timeout_seconds=1, agent=agent, planned_restart=True,
    )
    assert recovered["requeued"] == 1
    payload = read_json_file(paths.inbox / old.name)
    assert float(payload.get("not_before_at") or 0) <= time.time(), "计划内重启不加 10 秒等待"
    assert payload["active_turn_recovery"]["cause"] == "gateway_safe_restart"
    ordered = [path.name for path in _iter_pending_request_paths(paths)]
    assert ordered[0] == old.name, "续跑回合排在重启期间的新消息前面"
    assert fresh_path.name in ordered


def test_classification_and_state_for_planned_restart(tmp_path):
    paths = _paths(tmp_path)
    context = SimpleNamespace(paths=paths, process_identity={"pid": os.getpid()}, process_started_at=time.time())
    termination = _classify_gateway_service_return(
        context, {"summary": "safe restart drained", "restart": {"request_id": "gwrestart-3", "reason": "升级"}},
    )
    assert (termination.status, termination.kind, termination.exit_code) == ("stopped", "planned_restart", 0)
    events: list[tuple[str, dict]] = []
    agent = SimpleNamespace()
    import agent_py_agent.cli.gateway_process as gateway_process

    original = gateway_process.log_gateway_event
    gateway_process.log_gateway_event = lambda _agent, event, payload: events.append((event, payload))
    try:
        _record_gateway_service_termination(paths, agent, 123, termination)
    finally:
        gateway_process.log_gateway_event = original
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    assert state["status"] == "stopped" and state["termination_kind"] == "planned_restart"
    assert "error_code" not in state
    assert events[0][0] == "gateway_run_stopped"


def test_handover_spawns_successor_with_after_pid(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(handover, "service_manager_restarts_gateway", lambda: False)
    monkeypatch.setattr(handover, "log_gateway_event", lambda _agent, event, _payload: events.append(event))
    spawned: list[list[str]] = []
    written: list[tuple[int, list[str]]] = []
    code = handover.hand_over_to_successor(
        SimpleNamespace(agent=object()), pid=321, command=["python", "-m", "agent_py_agent", "gateway", "run"],
        spawn=lambda command: spawned.append(command) or SimpleNamespace(pid=654),
        write_start_files=lambda pid, command: written.append((pid, command)),
    )
    assert code == 0
    assert spawned == [["python", "-m", "agent_py_agent", "gateway", "run", "--after-pid", "321"]]
    assert written == [(654, spawned[0])]
    assert events == ["gateway_restart_handover"]


def test_handover_under_service_manager_exits_75_without_spawning(monkeypatch):
    monkeypatch.setattr(handover, "service_manager_restarts_gateway", lambda: True)
    monkeypatch.setattr(handover, "log_gateway_event", lambda *_args: None)

    def refuse(_command):
        raise AssertionError("托管时不能自己拉起接班进程")

    code = handover.hand_over_to_successor(
        SimpleNamespace(agent=object()), pid=1, command=["x"], spawn=refuse, write_start_files=refuse,
    )
    assert code == 75


def test_service_manager_detection_uses_launchd_label(monkeypatch):
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.my-agent.gateway")
    assert handover.service_manager_restarts_gateway() is True
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    assert handover.service_manager_restarts_gateway() is (
        f"/{handover.get_service_name()}.service" in _self_cgroup()
    )


def _self_cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""


def test_service_loop_drain_returns_restart_report(tmp_path, monkeypatch):
    agent = _agent(tmp_path, gateway_restart_turn_wait_seconds=1, gateway_restart_drain_timeout_seconds=1)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    events: list[str] = []
    monkeypatch.setattr(handover, "log_gateway_event", lambda _agent, event, _payload: events.append(event))
    context = SimpleNamespace(paths=paths, agent=agent, process_started_at=123.0)
    identity = {"pid": os.getpid()}
    assert handover.drain_for_requested_restart(context, identity) is None, "没有请求时继续服务"
    service.submit_restart_request(paths, target_pid=os.getpid(), requester={}, reason="r", cooldown_seconds=0)
    report = handover.drain_for_requested_restart(context, identity)
    assert report["summary"] == "safe restart drained"
    assert report["restart"]["old_pid"] == os.getpid()
    assert service.restart_completed_path(paths).exists()
    assert not service.restart_request_path(paths).exists()
    assert events[0] == "gateway_restart_requested" and events[-1] == "gateway_restart_drained"
    assert read_json_file(paths.state)["restart_drain"]["phase"] == "tool_drain"


def test_restart_control_requires_admin_and_schedules_for_this_gateway(tmp_path):
    paths = _paths(tmp_path)
    config = SimpleNamespace(gateway_restart_cooldown_seconds=30, gateway_restart_turn_wait_seconds=300)
    command = ConversationControlCommand("restart", value="升级通道", operation="apply")
    user = SimpleNamespace(home_paths=SimpleNamespace(owner_provider="feishu", owner_kind="user", owner_id="u1"), config=config)
    refused = execute_restart_control(user, None, command, paths=paths)
    assert (refused.ok, refused.error_code) == (False, "GATEWAY_RESTART_ADMIN_ONLY")
    assert not service.restart_request_path(paths).exists()
    admin = SimpleNamespace(home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"), config=config)
    accepted = execute_restart_control(admin, SimpleNamespace(thread_id="th-1"), command, paths=paths)
    assert accepted.ok is True and "已安排安全重启" in accepted.message
    request = read_json_file(service.restart_request_path(paths))
    assert request["target_pid"] == os.getpid()
    assert request["reason"] == "升级通道"
    assert request["requester"]["kind"] == "control_command"
    assert execute_restart_control(admin, None, command, paths=paths).message.startswith("已合并")


# LLM: 显式关闭强制超时策略，继续验证原取消、开门与发起会话通知合同。
# 函数用途: 验证非强制重启超时后仍正常服务并通知取消。
def test_drain_timeout_cancels_restart_and_notifies_requester(tmp_path, monkeypatch):
    agent = _agent(tmp_path, gateway_restart_turn_wait_seconds=0, gateway_restart_drain_timeout_seconds=1,
                   gateway_restart_force_on_drain_timeout=False)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(handover, "log_gateway_event", lambda *_args: None)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "admin", "channel": "tui",
        "channel_conversation_id": "cancel-case", "channel_user_id": "admin",
    })
    service.submit_restart_request(
        paths, target_pid=os.getpid(), reason="r", cooldown_seconds=0,
        requester={"thread_id": thread.thread_id, "conversation_store_root": str(store.storage.root)},
    )
    release = threading.Event()

    def long_tool():
        with restart_gate.tool_execution_admission():
            release.wait(10)

    worker = threading.Thread(target=long_tool)
    worker.start()
    time.sleep(0.1)
    try:
        report = handover.drain_for_requested_restart(
            SimpleNamespace(paths=paths, agent=agent, process_started_at=1.0), {"pid": os.getpid()},
        )
    finally:
        release.set()
        worker.join(timeout=5)
    assert report is None, "取消后继续服务"
    assert not service.restart_request_path(paths).exists()
    assert not service.restart_completed_path(paths).exists()
    assert read_json_file(paths.state)["restart_drain"]["phase"] == "cancelled"
    assert restart_gate.tool_gate_closed() is False
    reasons = [signal.reason for signal in store.wakes.pending()]
    assert reasons == [service.CANCELLED_EVENT_TYPE]


def test_draining_dispatch_only_records_wait_facts_and_claims_nothing(tmp_path):
    from agent_py_agent.agent.gateway_parts.request_worker import (
        AdmissionLimits,
        dispatch_pending_requests,
    )

    agent = _agent(tmp_path)
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    _rid, pending_path, _ = submit_gateway_ask(paths, params=GatewayAskParams(prompt="排空期间的新消息", save=False))
    submitted: list[object] = []
    claimed = dispatch_pending_requests(
        paths, AdmissionLimits(user_inflight=8, global_inflight=8), lambda *args: submitted.append(args),
        hold_reason=service.RESTART_DRAIN_HOLD_REASON,
    )
    assert claimed == 0 and submitted == []
    payload = read_json_file(pending_path)
    assert payload["admission_wait_reason"] == "gateway_restart_draining"
    assert payload.get("status") != "processing" and not list(paths.processing.glob("*.json"))


def _cli_agent(**overrides):
    values = {"gateway_restart_cooldown_seconds": 30, "gateway_restart_turn_wait_seconds": 1,
              "gateway_restart_drain_timeout_seconds": 1, **overrides}
    return SimpleNamespace(config=SimpleNamespace(**values))


def test_cli_restart_falls_back_when_gateway_not_running(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(handover, "get_running_pid", lambda _path: None)
    assert handover.safe_restart_from_cli(_cli_agent(), paths) is None
    assert not service.restart_request_path(paths).exists()


def test_cli_restart_is_refused_inside_its_own_gateway(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(handover, "get_running_pid", lambda _path: 4242)
    monkeypatch.setattr(handover, "is_pid_alive", lambda _pid: True)
    monkeypatch.setenv(service.HOSTING_GATEWAY_PID_ENV, "4242")
    assert handover.safe_restart_from_cli(_cli_agent(), paths) == 2
    assert not service.restart_request_path(paths).exists()


def test_cli_restart_waits_for_new_running_pid(tmp_path, monkeypatch, capsys):
    paths = _paths(tmp_path)
    monkeypatch.delenv(service.HOSTING_GATEWAY_PID_ENV, raising=False)
    current = {"pid": 4242}
    monkeypatch.setattr(handover, "get_running_pid", lambda _path: current["pid"])
    monkeypatch.setattr(handover, "is_pid_alive", lambda _pid: True)

    def successor():
        time.sleep(0.4)
        assert read_json_file(service.restart_request_path(paths))["requester"] == {"kind": "cli"}
        current["pid"] = 5353
        paths.state.write_text(json.dumps({"status": "running", "pid": 5353}), encoding="utf-8")

    threading.Thread(target=successor).start()
    assert handover.safe_restart_from_cli(_cli_agent(), paths) == 0
    assert "4242 → 5353" in capsys.readouterr().out


def test_cli_restart_reports_cancellation_and_cooldown(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.delenv(service.HOSTING_GATEWAY_PID_ENV, raising=False)
    monkeypatch.setattr(handover, "get_running_pid", lambda _path: 4242)
    monkeypatch.setattr(handover, "is_pid_alive", lambda _pid: True)

    def cancel():
        time.sleep(0.4)
        request_id = read_json_file(service.restart_request_path(paths))["request_id"]
        paths.state.write_text(json.dumps({"status": "running", "pid": 4242, "restart_drain": {
            "request_id": request_id, "phase": "cancelled", "reason": "drain_timeout"}}), encoding="utf-8")

    threading.Thread(target=cancel).start()
    assert handover.safe_restart_from_cli(_cli_agent(), paths) == 2
    request = read_json_file(service.restart_request_path(paths))
    service.mark_restart_drained(paths, request, old_pid=4242, old_process_started_at=0.0, active_turns_at_exit=0)
    assert handover.safe_restart_from_cli(_cli_agent(), paths) == 2, "冷却中直接拒绝"


@pytest.mark.parametrize("case", ["resumed", "fresh", "cancelled"])
def test_resumed_claim_writes_one_turn_resumed_boundary_before_new_output(tmp_path, monkeypatch, case):
    from agent_py_agent.agent.gateway_parts import request_execution

    agent = _agent(tmp_path, gateway_processing_timeout_seconds=1)
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    request_id = "gwreq-resume-boundary"
    claimed = paths.processing / f"{request_id}.json"
    write_json_file(claimed, {
        "id": request_id, "kind": "ask", "prompt": "重启前在跑", "attempts": 1,
        "status": "processing" if case != "fresh" else "pending", "turn_phase": "open",
        "lease_started_at": time.time() - 10,
    })
    chunk_path = paths.processing / f"{request_id}.chunks.jsonl"
    stale_row = {"t": 1.0, "kind": "tool_progress",
                 "progress": {"round": 2, "call_index": 0, "tool": "run_command", "phase": "started"}}
    chunk_path.write_text(json.dumps(stale_row) + "\n", encoding="utf-8")
    if case != "fresh":
        recovered = recover_gateway_processing_requests(
            paths, startup=True, max_attempts=2, timeout_seconds=1, agent=agent, planned_restart=True,
        )
        assert recovered["requeued"] == 1
        # 接班进程按原请求号重新认领；恢复写下的结构化续跑标记随请求文件保留，chunk 文件原地不动。
        (paths.inbox / claimed.name).replace(claimed)
        assert read_json_file(claimed)["active_turn_recovery"]["cause"] == "gateway_safe_restart"
    if case == "cancelled":
        write_json_file(claimed, {**read_json_file(claimed), "cancel_requested": True})
    executed: list[str] = []

    def run_new_generation(context, on_chunk):
        executed.append(context["request_id"])
        on_chunk.write_progress({"round": 2, "call_index": 0, "tool": "run_command", "phase": "started"}, "")

    monkeypatch.setattr(request_execution, "_execute_gateway_request_body", run_new_generation)
    request_execution._handle_gateway_request(agent, claimed)

    rows = [json.loads(line) for line in chunk_path.read_text(encoding="utf-8").splitlines()]
    kinds = [row["kind"] for row in rows]
    if case == "resumed":
        assert executed == [request_id]
        assert kinds == ["tool_progress", "turn_resumed", "tool_progress"], "边界只写一次，且先于续跑代次的任何输出"
        assert rows[1]["cause"] == "gateway_safe_restart"
        assert set(rows[1]) == {"t", "kind", "cause"}
    elif case == "fresh":
        assert executed == [request_id]
        assert kinds == ["tool_progress", "tool_progress"], "普通请求没有续跑标记，不写边界"
    else:
        assert executed == []
        assert kinds == ["tool_progress"], "认领时已被停止的续跑请求不执行，也不写边界"


def test_update_stop_uses_original_drain_but_does_not_launch_successor(tmp_path):
    paths=_paths(tmp_path)
    first=service.submit_restart_request(paths,target_pid=123,requester={'kind':'cli'},reason='synthetic update',cooldown_seconds=0,operation='stop_for_update',now=1000)
    assert first['status']=='scheduled'
    conflict=service.submit_restart_request(paths,target_pid=123,requester={'kind':'cli'},reason='restart',cooldown_seconds=0,now=1001)
    assert conflict['status']=='conflict'
    assert service.pending_restart_request(paths,pid=123)['operation']=='stop_for_update'
    context=SimpleNamespace(paths=paths)
    report={'summary':'drained','restart':{'operation':'stop_for_update','request_id':'req-test'}}
    termination=_classify_gateway_service_return(context,report)
    assert termination.kind=='planned_update_stop' and termination.exit_code==0


# LLM: 活跃计数只读进程内finished，不调用会检查持久claim/worker的blockers，也不改变停止状态。
# 函数用途: 验证未完成后台片计数且已结束但未核清的片不重复计入回合。
def test_incident_background_active_slice_count_is_memory_only(monkeypatch):
    from agent_py_agent.agent.conversation.background_shutdown import (
        BackgroundShutdown,
        BackgroundSlice,
    )

    shutdown = BackgroundShutdown()
    finished = BackgroundSlice(shutdown, None)
    finished.finished = True
    shutdown.slices.add(finished)
    # LLM: 禁止计数器借blockers查库或核对worker，发现调用就明确失败。
    # 函数用途: 守住纯内存计数的边界。
    def forbid_blockers(_self):
        raise AssertionError('active count accessed blockers')
    count = getattr(shutdown, 'active_slice_count', lambda: -1)
    monkeypatch.setattr(BackgroundSlice, 'blockers', forbid_blockers)
    assert count() == 0
    live = BackgroundSlice(shutdown, None)
    shutdown.slices.add(live)
    assert count() == 1 and not shutdown.stopping
    live.finished = True
    assert count() == 0 and shutdown.slices == {live, finished}


# LLM: 用真实后台observe、restart工具、工具门和本机子进程追加日志；不启动或重启真实Gateway。
# 函数用途: 验证后台发起重启后的同片写日志可完成，前后台均归零后才关闭工具门。
@pytest.mark.parametrize('foreground', [0, 2])
def test_incident_background_slice_finishes_log_before_tool_drain(tmp_path, monkeypatch, foreground):
    import subprocess
    import sys

    from agent_py_agent.agent.conversation.background_shutdown import BackgroundShutdown
    from agent_py_agent.agent.tooling.gateway_restart_tool import RestartGatewayTool

    agent = _agent(tmp_path, gateway_restart_turn_wait_seconds=2, gateway_restart_drain_timeout_seconds=1)
    paths, shutdown = gateway_paths(agent), BackgroundShutdown()
    context = SimpleNamespace(paths=paths, agent=agent, process_started_at=1, background_shutdown=shutdown)
    ready, turn_wait, logged = threading.Event(), threading.Event(), threading.Event()
    errors, phases, turns = [], [], {'count': foreground}
    log = tmp_path / 'scheduled-final.log'
    monkeypatch.setenv(service.HOSTING_GATEWAY_PID_ENV, str(os.getpid()))
    monkeypatch.setattr(handover.admission, 'snapshot', lambda: {'total': turns['count']})
    # LLM: 仅冻结进程切换前的真实阶段观察，等待回合时不得已关闭工具门。
    # 函数用途: 把turn_wait交给后台片继续，并核对下一阶段已完成日志。
    def progress(_agent, event, facts):
        if event != 'gateway_restart_draining':
            return
        phases.append(facts['phase'])
        if facts['phase'] == 'turn_wait':
            assert facts['active_turns'] == foreground + 1
            assert not restart_gate.tool_gate_closed()
            turn_wait.set()
        else:
            assert logged.is_set() and shutdown.active_slice_count() == 0
    # LLM: 隔离后台片先安排重启，再经真实工具门写日志；异常交回主测试，不留停车线程。
    # 函数用途: 模拟isolated定时回合顺序完成重启请求和最后的run_command副作用。
    def scheduled_slice():
        try:
            with shutdown.observe(None):
                assert RestartGatewayTool(agent).execute({'reason': '定时维护'}).ok
                ready.set()
                assert turn_wait.wait(3)
                with restart_gate.tool_execution_admission() as admitted:
                    assert admitted
                    subprocess.run([sys.executable, '-c', 'import sys;open(sys.argv[1],"a").write("done\\n")', str(log)],
                                   check=True, timeout=2)
                logged.set()
                turns['count'] = 0
        except BaseException as exc:
            errors.append(exc)
            ready.set()
    monkeypatch.setattr(handover, 'log_gateway_event', progress)
    worker = threading.Thread(target=scheduled_slice)
    worker.start()
    try:
        assert ready.wait(3) and not errors
        report = handover.drain_for_requested_restart(context, {'pid': os.getpid()})
        assert report['restart']['active_turns_at_exit'] == 0
        assert log.read_text() == 'done\n' and not errors
        assert phases == ['turn_wait', 'tool_drain']
    finally:
        service.reset_restart_drain_state()
        turn_wait.set()
        worker.join(5)
    assert not worker.is_alive() and not errors


# LLM: 长工具仍真实持有准入；restart按配置强制并保留计数，stop_for_update无论开关都必须取消。
# 函数用途: 验证强制超时标记/事件透传、默认策略及离线更新的非强制边界。
@pytest.mark.parametrize('operation,force,expected', [
    ('restart', True, True), ('restart', None, True), ('restart', False, False), ('stop_for_update', True, False),
])
def test_incident_handover_force_timeout_metadata(tmp_path, monkeypatch, operation, force, expected):
    paths = _paths(tmp_path)
    config = SimpleNamespace(gateway_restart_turn_wait_seconds=0, gateway_restart_drain_timeout_seconds=.01)
    if force is not None:
        config.gateway_restart_force_on_drain_timeout = force
    agent = SimpleNamespace(config=config)
    context = SimpleNamespace(paths=paths, agent=agent, process_started_at=1)
    service.submit_restart_request(paths, target_pid=os.getpid(), requester={}, reason='r',
                                   cooldown_seconds=0, operation=operation)
    events, entered, release = [], threading.Event(), threading.Event()
    monkeypatch.setattr(handover, 'log_gateway_event', lambda _a, event, facts: events.append((event, facts)))
    # LLM: 在真实工具准入中停住线程，直到主测试读完超时结果才退出，不执行外部业务。
    # 函数用途: 确保超时当刻确实还有一个工具未退。
    def long_tool():
        with restart_gate.tool_execution_admission():
            entered.set()
            release.wait(5)
    worker = threading.Thread(target=long_tool)
    worker.start()
    try:
        assert entered.wait(2)
        result = handover.drain_for_requested_restart(context, {'pid': os.getpid()})
        assert restart_gate.executing_tool_count() == 1
        if expected:
            marker = read_json_file(service.restart_completed_path(paths))
            assert result['restart'] == marker
            assert marker['forced_after_drain_timeout'] is True and marker['executing_tools_at_exit'] == 1
            assert restart_gate.tool_gate_closed() and service.restart_phase()['phase'] == 'exiting'
            assert events[-1][0] == 'gateway_restart_drained'
            assert events[-1][1]['forced_after_drain_timeout'] is True
            assert events[-1][1]['executing_tools_at_exit'] == 1
        else:
            assert result is None and not service.restart_completed_path(paths).exists()
            assert not restart_gate.tool_gate_closed()
            assert events[-1][1]['reason'] == 'drain_timeout'
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


# LLM: 停止可在回合等待、关闭门、进度回调或等待返回时到达，均应优先撤销并恢复一致状态。
# 函数用途: 验证tool_drain前后及两种等待结果的停止竞态。
@pytest.mark.parametrize('stop_at', ['turn_wait', 'close', 'tool_drain', 'wait_success', 'wait_timeout'])
@pytest.mark.parametrize('force', [False, True])
def test_incident_stop_requested_at_drain_boundaries(monkeypatch, stop_at, force):
    stopping, waited = {'value': False}, []
    original_close = service.close_tool_gate
    # LLM: 保留真实关门动作，只注入关门时到达的停止事实。
    # 函数用途: 捕获关闭门与第二次停止检查之间的竞态。
    def close():
        original_close()
        stopping['value'] = stop_at == 'close'
    # LLM: 只通过原进度回调注入停止，不替换排空状态机。
    # 函数用途: 覆盖第一段和第二段开始处的停止信号。
    def progress(facts):
        if facts['phase'] == stop_at:
            stopping['value'] = True
    # LLM: 模拟等待成功/超时的同刻出现停止，不能让成功或强制分支覆盖停止。
    # 函数用途: 固定复现等待返回后的停止优先级。
    def wait(_timeout):
        waited.append(True)
        stopping['value'] = True
        return stop_at != 'wait_timeout'
    monkeypatch.setattr(service, 'close_tool_gate', close)
    monkeypatch.setattr(service, 'wait_until_no_executing_tools', wait)
    result = service.run_restart_drain({'request_id': 'stop-case'}, active_turns=lambda: 0,
                                      turn_wait_seconds=0, drain_timeout_seconds=1,
                                      force_on_drain_timeout=force,
                                      stop_requested=lambda: stopping['value'], on_progress=progress)
    assert result == {'ok': False, 'reason': 'stop_requested', 'request_id': 'stop-case'}
    assert service.restart_phase()['phase'] == 'idle' and not restart_gate.tool_gate_closed()
    assert bool(waited) == stop_at.startswith('wait_')


# LLM: 排空返回成功与handover复核之间到达停止也必须开门复位，不能留下exiting但取消标记。
# 函数用途: 验证完成排空后抢先到达的停止请求仍优先于重启。
def test_incident_stop_arriving_after_drain_reopens_gate(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    context = SimpleNamespace(paths=paths, agent=_cli_agent(), process_started_at=1)
    service.submit_restart_request(paths, target_pid=os.getpid(), requester={}, reason='r', cooldown_seconds=0)
    monkeypatch.setattr(handover, 'log_gateway_event', lambda *_args: None)
    monkeypatch.setattr(handover, 'stop_request_targets_process', lambda *_args, **_kwargs: True)
    # LLM: 执行真实排空，随后注入原停止文件；不伪造成功或替换工具门。
    # 函数用途: 固定重现两层停止复核之间的竞态。
    def drain(*args, **kwargs):
        result = service.run_restart_drain(*args, **kwargs)
        paths.stop_request.write_text('{}')
        return result
    monkeypatch.setattr(handover, 'run_restart_drain', drain)
    assert handover.drain_for_requested_restart(context, {'pid': os.getpid()}) is None
    assert service.restart_phase()['phase'] == 'idle' and not restart_gate.tool_gate_closed()
    assert not service.restart_completed_path(paths).exists()
