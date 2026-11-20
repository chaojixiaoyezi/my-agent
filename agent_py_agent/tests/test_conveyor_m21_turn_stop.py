"""Gateway HTTP 回合停止与原请求账本的往返合同。"""
from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.fast_dataclass import check_slots_compat
from agent_py_agent.agent.concurrency.interrupt import register_interruptible, wait_interruptibly
from agent_py_agent.agent.conversation.claim_execution_binding import (
    ClaimExecutionBinding,
    bind_claim_execution,
    publish_claim_runtime_authority,
)
from agent_py_agent.agent.conversation.control_commands import session_task_interrupt_name
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
from agent_py_agent.agent.gateway_parts.http_lanes import classify_peeked_request, control_sublane
from agent_py_agent.agent.gateway_parts.http_routes import GatewayHTTPRoute
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.background_process_launch import start_background_process
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_cleanup import stop_process_session
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.tests._managed_process_harness import managed_request


@pytest.fixture
def gateway(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield agent, paths, server
    finally:
        server.stop()


def _post(gateway, route, body, *, user="u-1", authenticated=True):
    agent, _, server = gateway
    headers = {"Content-Type": "application/json", "X-User-Id": user, "X-Channel": "feishu"}
    if authenticated:
        headers.update(GatewayClientCredentials(agent.home_paths.root).headers())
    data = json.dumps({"conversation_id": "c-1", **body}).encode() if isinstance(body, dict) else body
    connection = HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=10)
    try:
        connection.request("POST", route, data, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _thread(gateway, *, user="u-1", conversation="c-1"):
    agent, _, _ = gateway
    return agent.conversation_store.threads.get_or_create({
        "channel": "feishu", "channel_conversation_id": conversation, "channel_user_id": user,
        "canonical_user_id": user, "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir),
    })


def _queued(gateway, thread, request_id, *, user="u-1", conversation="c-1"):
    _, paths, _ = gateway
    path = paths.inbox / f"{request_id}.json"
    write_json_file_atomic(path, {
        "id": request_id, "kind": "ask", "status": "pending", "goal": "queued turn",
        "user_id": user, "conversation_thread_id": thread.thread_id,
        "conversation": {"channel": "feishu", "channel_conversation_id": conversation,
                         "channel_user_id": user, "canonical_user_id": user},
    })
    return path


def _control(gateway, command, message_id, **body):
    return _post(gateway, "/control", {"command": command, "metadata": {"message_id": message_id}, **body})


def test_plain_stop_settles_queued_request_without_claiming(gateway):
    thread = _thread(gateway)
    path = _queued(gateway, thread, "queued-current")
    code, result = _control(gateway, "/stop", "stop-queued")
    assert code == 200 and result["ok"], result
    _, paths, _ = gateway
    assert claim_request(paths, path) is None
    terminal = read_json_file(paths.terminal / path.name)
    assert terminal["terminal_response"]["status"] == "cancelled"
    assert not (paths.processing / path.name).exists()


def test_new_turn_stop_route_selects_the_stop_lane():
    prefix = b"POST /client/turn-stop HTTP/1.1\r\n"
    assert classify_peeked_request(prefix) == "control"
    assert control_sublane(prefix) == "stop"
    assert control_sublane(b"POST /control HTTP/1.1\r\n") == "control"


def test_new_thread_replays_the_original_target_and_conflicts_are_inert(gateway):
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        GatewayControlOperationReceipt,
        read_gateway_control_operation,
    )
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    earlier = _thread(gateway)
    body = {"message_id": "one-stop"}
    original = _queued(gateway, earlier, "queued-earlier")
    code, first = _post(gateway, "/client/turn-stop", body)
    assert code == 200 and first["queued_cancelled"] == 1, first
    stored = read_gateway_control_operation(gateway[1], first["operation_id"])
    assert stored.input_digest_version == 6
    tampered = {**stored.to_dict(), "target_thread_ids": ["another-thread"]}
    with pytest.raises(DataCorruptionError):
        GatewayControlOperationReceipt.from_dict(tampered)
    code, successor = _control(gateway, "/new", "new-thread")
    assert code == 200 and successor["ok"], successor
    current = _thread(gateway)
    assert earlier.thread_id != current.thread_id
    later = _queued(gateway, current, "queued-later")
    code, replay = _post(gateway, "/client/turn-stop", body)
    assert code == 200 and replay == first
    assert replay["target_thread_ids"] == [earlier.thread_id]
    for change in ({"thread_id": current.thread_id}, {"scope": "session"}):
        code, conflict = _post(gateway, "/client/turn-stop", {**body, **change})
        assert code == 409 and conflict["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert not read_json_file(later).get("cancel_requested")
    assert read_json_file(original)["cancel_requested"]


def test_plain_stop_and_session_stop_select_only_the_requested_lineage(gateway):
    earlier = _thread(gateway)
    old = _queued(gateway, earlier, "old-turn")
    code, successor = _control(gateway, "/new", "successor")
    assert code == 200 and successor["ok"], successor
    current = _thread(gateway)
    same = _queued(gateway, current, "current-turn")
    other = _queued(gateway, _thread(gateway, conversation="other"), "unrelated-turn", conversation="other")
    code, result = _post(gateway, "/client/turn-stop", {"message_id": "current-stop"})
    assert code == 200 and result["target_thread_ids"] == [current.thread_id]
    assert read_json_file(same)["cancel_requested"]
    assert not read_json_file(old).get("cancel_requested")
    code, result = _post(gateway, "/client/turn-stop", {"message_id": "session-stop", "scope": "session"})
    assert code == 200 and result["target_thread_ids"] == sorted([earlier.thread_id, current.thread_id])
    assert read_json_file(old)["cancel_requested"]
    assert not read_json_file(other).get("cancel_requested")
    earlier_only = _queued(gateway, earlier, "previous-thread-only")
    current_only = _queued(gateway, current, "current-thread-only")
    code, targeted = _post(gateway, "/client/turn-stop",
                           {"message_id": "prior-only", "thread_id": earlier.thread_id})
    assert code == 200 and targeted["target_thread_ids"] == [earlier.thread_id] and targeted["queued_cancelled"] == 1
    assert read_json_file(earlier_only)["cancel_requested"]
    assert not read_json_file(current_only).get("cancel_requested")
    assert not gateway[1].stop_request.exists()


def test_route_checks_credentials_before_parsing_and_rejects_foreign_thread(gateway):
    earlier = _thread(gateway)
    path = _queued(gateway, earlier, "private-turn")
    code, _ = _post(gateway, "/client/turn-stop", b"not JSON", authenticated=False)
    assert code in {401, 403}
    _thread(gateway, user="u-2")
    code, result = _post(gateway, "/client/turn-stop",
                         {"message_id": "foreign-stop", "thread_id": earlier.thread_id}, user="u-2")
    assert code in {403, 404} or (code == 200 and not result["ok"])
    assert not read_json_file(path).get("cancel_requested")


def test_status_always_projects_background_roots(gateway):
    _thread(gateway)
    code, result = _control(gateway, "/status", "status-roots")
    assert code == 200 and result["task_status"]["background_roots"] == []


def test_503_before_admission_retries_the_same_message_once(gateway, monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service

    thread = _thread(gateway)
    queued = _queued(gateway, thread, "queued-retried")
    server = gateway[2].server
    slots = server._lanes._lane_slots["stop"]
    held = 0
    while slots.acquire(blocking=False):
        held += 1
    assert held
    calls = []
    original = control_service._stop_queued_turns

    def observed(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(control_service, "_stop_queued_turns", observed)
    body = {"message_id": "retry-stop"}
    try:
        assert _post(gateway, "/client/turn-stop", body)[0] == 503
        assert not calls and not read_json_file(queued).get("cancel_requested")
    finally:
        for _ in range(held):
            slots.release()
    code, first = _post(gateway, "/client/turn-stop", body)
    assert code == 200 and first["queued_cancelled"] == 1
    assert _post(gateway, "/client/turn-stop", body) == (200, first)
    assert calls == [True]


def test_claim_winning_the_race_gets_a_durable_stop_fence(gateway, monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service

    path = _queued(gateway, _thread(gateway), "about-to-claim")
    _, paths, _ = gateway
    picked, release = threading.Event(), threading.Event()
    original = control_service._matching_requests

    def paused(folder, scope):
        rows = original(folder, scope)
        if folder == paths.inbox:
            picked.set()
            assert release.wait(5)
        return rows

    monkeypatch.setattr(control_service, "_matching_requests", paused)
    result = []
    stopping = threading.Thread(target=lambda: result.append(_post(
        gateway, "/client/turn-stop", {"message_id": "claim-race"},
    )))
    stopping.start()
    try:
        assert picked.wait(5)
        claim = claim_request(paths, path)
        assert claim is not None
    finally:
        release.set()
        stopping.join(8)
    assert not stopping.is_alive()
    assert result[0][0] == 200 and result[0][1]["queued_cancelled"] == 1
    processing = read_json_file(claim.path)
    assert processing["cancel_requested"] is True
    assert processing["execution_attempt_id"] == claim.execution_attempt_id
    assert processing["turn_phase"] == "closing"


def _dispatch(gateway, earlier, *, turn="m21-bound-dispatch"):
    agent, _, _ = gateway
    store = agent.conversation_store
    sender = _thread(gateway, conversation="sender")
    task = store.session_tasks.create(SessionTaskDraft(sender.thread_id, earlier.thread_id, "后台派活"))
    store.session_tasks.bind_turn(task.task_id, turn_id=turn)
    repo = agent.subagents.runtime_db
    run = repo.record_run_creation(owner_id=agent.home_paths.owner_id, conversation_task_id=task.task_id,
                                   thread_id=earlier.thread_id, run_id=turn, role="main", attempt_status="pending")
    attempt = repo.create_attempt(run["agent_run_id"], reuse_pending=True)
    claim = store.claims.acquire({"thread_id": earlier.thread_id, "task_id": task.task_id, "reason": "session_task"})
    binding = ClaimExecutionBinding(store.claims, earlier.thread_id, claim["claim_id"], earlier.thread_id)
    with bind_claim_execution(binding):
        publish_claim_runtime_authority({"invocation_run_id": turn, "task_id": task.task_id, "run_id": turn,
            "agent_run_id": run["agent_run_id"], "attempt_id": attempt["attempt_id"]})
    return turn


def test_new_notice_and_session_stop_reach_dispatch_and_owned_process(gateway, tmp_path):
    agent, paths, _ = gateway
    earlier = _thread(gateway)
    turn = _dispatch(gateway, earlier)
    owner = Path(agent.home_paths.owner_home_dir).resolve()
    request = managed_request(
        tmp_path / "managed", access_scope=ProcessAccessScope(agent.home_paths.owner_id, earlier.thread_id, str(owner)),
        execution_scope=ProcessExecutionScope(str(owner), earlier.thread_id, "settled-root", "managed-run", "managed-attempt"),
        store_root=process_session_store_root(owner, owner),
    )
    hosted = start_background_process(request, startup_timeout_seconds=8)
    ready, done, release = threading.Event(), threading.Event(), threading.Event()

    def dispatch_runner():
        with register_interruptible(session_task_interrupt_name(turn)):
            ready.set()
            try:
                while not release.is_set():
                    wait_interruptibly(0.05)
            except InterruptedError:
                done.set()

    worker = threading.Thread(target=dispatch_runner)
    worker.start()
    try:
        assert ready.wait(5)
        code, created = _control(gateway, "/new", "with-live-background")
        assert code == 200 and created["ok"]
        assert _control(gateway, "/new", "with-live-background") == (200, created)
        store = agent.conversation_store
        current = store.threads.require(created["details"]["thread_id"])
        notices = [notice for notice in current.pending_host_notices if notice["code"] == "BACKGROUND_ROOTS_CONTINUE"]
        assert len(notices) == 1 and notices[0]["details"]["count"] == "2"
        assert _control(gateway, "/stop", "plain-new-thread")[0] == 200
        assert not done.is_set() and hosted.process.poll() is None
        code, report = _control(gateway, "/status", "show-old-background")
        assert code == 200
        roots = report["task_status"]["background_roots"]
        assert {(row["thread_id"], row["kind"]) for row in roots} == {
            (earlier.thread_id, "dispatch"), (earlier.thread_id, "process"),
        }
        started = time.monotonic()
        code, stopped = _post(gateway, "/client/turn-stop", {"message_id": "all-roots", "scope": "session"})
        assert code == 200 and stopped["roots_stopped"] == 1 and stopped["processes_stopped"] == 1, stopped
        assert done.wait(max(0, 10 - (time.monotonic() - started)))
        hosted.process.wait(timeout=max(0, 10 - (time.monotonic() - started)))
        assert time.monotonic() - started < 10
        assert not paths.stop_request.exists()
    finally:
        release.set()
        worker.join(5)
        try:
            if hosted.process.poll() is None:
                stop_process_session(ProcessSessionStore(request.store_root), hosted.record, host_process=hosted.process)
        finally:
            hosted.process.wait(timeout=5)


class _InterruptibleRoot:
    """在后台登记一个可中断执行体，模拟某线程上仍在跑的派发根。"""

    def __init__(self, turn):
        self.ready, self.done, self.release = threading.Event(), threading.Event(), threading.Event()
        self.worker = threading.Thread(target=self._run, args=(turn,))

    def _run(self, turn):
        with register_interruptible(session_task_interrupt_name(turn)):
            self.ready.set()
            try:
                while not self.release.is_set():
                    wait_interruptibly(0.05)
            except InterruptedError:
                self.done.set()

    def __enter__(self):
        self.worker.start()
        assert self.ready.wait(5)
        return self

    def __exit__(self, *_exc):
        self.release.set()
        self.worker.join(5)


def test_deleted_predecessor_ends_lineage_without_disabling_session_stop(gateway):
    from agent_py_agent.agent.plugin_host.thread_settings_v2 import ThreadSettingsV2Handlers

    agent, paths, _ = gateway
    store = agent.conversation_store
    first = _thread(gateway)
    code, created = _control(gateway, "/new", "lineage-second")
    assert code == 200 and created["ok"], created
    middle = _thread(gateway)
    middle_turn = _dispatch(gateway, middle, turn="m21-middle-dispatch")
    with _InterruptibleRoot(middle_turn) as middle_root:
        code, created = _control(gateway, "/new", "lineage-third")
        assert code == 200 and created["ok"], created
        current = _thread(gateway)
        assert len({first.thread_id, middle.thread_id, current.thread_id}) == 3
        current_turn = _dispatch(gateway, current, turn="m21-current-dispatch")
        with _InterruptibleRoot(current_turn) as current_root:
            # 通过受支持的 threads.delete 入口删掉最早的空闲前任；后继仍保留指向它的 predecessor_thread_id。
            context = SimpleNamespace(owner_home=SimpleNamespace(
                owner_id=agent.home_paths.owner_id, home_dir=agent.home_paths.owner_home_dir))
            deleted = ThreadSettingsV2Handlers(store, context, home_paths=agent.home_paths).delete_thread(
                {"thread_id": first.thread_id})
            assert deleted["deleted"] and store.threads.load(first.thread_id) is None
            assert store.threads.require(middle.thread_id).metadata["predecessor_thread_id"] == first.thread_id

            code, targeted = _post(gateway, "/client/turn-stop",
                                   {"message_id": "current-only", "thread_id": current.thread_id})
            assert code == 200 and targeted["ok"], targeted
            assert targeted["target_thread_ids"] == [current.thread_id] and targeted["roots_stopped"] == 1
            assert current_root.done.wait(5) and not middle_root.done.is_set()

            code, report = _control(gateway, "/status", "lineage-status")
            assert code == 200, report
            roots = report["task_status"]["background_roots"]
            assert [(row["thread_id"], row["kind"], row["lineage_complete"]) for row in roots] == [
                (middle.thread_id, "dispatch", False),
            ]

            code, stopped = _post(gateway, "/client/turn-stop", {"message_id": "lineage-all", "scope": "session"})
            assert code == 200 and stopped["ok"], stopped
            assert stopped["target_thread_ids"] == sorted([middle.thread_id, current.thread_id])
            assert stopped["roots_stopped"] == 1, stopped
            assert middle_root.done.wait(5)
    assert not paths.stop_request.exists()


def _lineage_record(thread_id, *, predecessor="", successor="", owner="owner-a"):
    binding = ["feishu", "u-1", "c-1"]
    metadata = {}
    if predecessor:
        metadata.update(predecessor_thread_id=predecessor, predecessor_binding=binding)
    if successor:
        metadata["succession_by_binding"] = {"\x1f".join(binding): successor}
    return SimpleNamespace(thread_id=thread_id, metadata=metadata, owner_id=owner, owner_home="/owner-home")


def _lineage_store(current, records, *, binding_error=None):
    threads = SimpleNamespace(
        resolve_report=lambda **_kwargs: (current, binding_error),
        load_report=lambda thread_id: records.get(thread_id, (None, None)),
    )
    return SimpleNamespace(threads=threads)


_LINEAGE_SCOPE = SimpleNamespace(channel="feishu", user_id="u-1", conversation_id="c-1")


def test_session_lineage_boundary_and_refusal_cases():
    from agent_py_agent.agent.gateway_parts.control_service import (
        TurnStopTargetUnavailable,
        _session_stop_threads,
    )

    current = _lineage_record("t-current", predecessor="t-prior")
    prior = _lineage_record("t-prior", successor="t-current")
    walk = _session_stop_threads(_lineage_store(current, {"t-prior": (prior, None)}), _LINEAGE_SCOPE)
    assert ([thread.thread_id for thread in walk[0]], walk[1]) == (["t-current", "t-prior"], True)
    # 干净缺失：前任文件已被删除，读无错误 -> 已核验链的边界。
    walk = _session_stop_threads(_lineage_store(current, {}), _LINEAGE_SCOPE)
    assert ([thread.thread_id for thread in walk[0]], walk[1]) == (["t-current"], False)
    assert _session_stop_threads(_lineage_store(None, {}), _LINEAGE_SCOPE) == ((), True)

    cyclic = _lineage_record("t-prior", predecessor="t-current", successor="t-current")
    cross_owner = _lineage_record("t-prior", successor="t-current", owner="owner-b")
    mismatched_record = _lineage_record("t-prior", successor="t-elsewhere")
    refusals = {
        "unreadable": _lineage_store(current, {"t-prior": (None, {"code": "simulated_read_error"})}),
        "cyclic": _lineage_store(current, {"t-prior": (cyclic, None)}),
        "cross_owner": _lineage_store(current, {"t-prior": (cross_owner, None)}),
        "mismatch": _lineage_store(current, {"t-prior": (mismatched_record, None)}),
        "binding_unreadable": _lineage_store(current, {}, binding_error={"code": "simulated_read_error"}),
    }
    for case, store in refusals.items():
        with pytest.raises(TurnStopTargetUnavailable):
            _session_stop_threads(store, _LINEAGE_SCOPE)
            pytest.fail(f"{case} lineage must stay a refusal")


def test_http_route_slots_preserve_dataclass_behavior():
    @dataclass(frozen=True)
    class PlainRoute:
        template: str
        handler_name: str
        access_tier: str
        handler_module: str = ""

    fields = ("/client/turn-stop", "handle_client_turn_stop", "credential")
    original, converted = PlainRoute(*fields), GatewayHTTPRoute(*fields)
    assert asdict(original) == asdict(converted)
    assert hash(original) == hash(converted)
    assert asdict(replace(original, access_tier="admin")) == asdict(replace(converted, access_tier="admin"))
    assert check_slots_compat(GatewayHTTPRoute) == []


def test_ready_coordinator_delivers_frozen_session_stop_to_the_original_worker(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.io import write_gateway_request
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        CoordinatorAdmissionScope,
        PersistentAdmission,
        release_reservation_if_terminal,
        session_scope_for_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        bind_admission_takeover,
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.tests.test_coordinator_control_forward import ForwardCase, consume, worker
    from agent_py_agent.tests.test_gateway_c_http_wait import request

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config = AgentConfig(my_agent_home=str(home), model_backend="echo",
                         gateway_per_user_owner_scoping=True, enable_plugins=False, enable_subagents=True)
    worker_config = deepcopy(config)
    base = SimpleAgent(config, workspace)
    owner = OwnerIdentity.provider_user("feishu", "alice")
    seeded = SimpleAgent(_config_with_owner(config, owner), workspace)
    thread = _thread((seeded, None, None), user="alice", conversation="control-session")
    paths = gateway_paths_from_root(tmp_path / "gateway")
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "m21-turn-stop-C")
        control = GatewayControlPlane(config, home, paths, 2, controller.c_run_id,
                                      agent_root=workspace, workspace_roots=(workspace,))
        assert control.start_business(lambda: base) is base
        base._gateway_control_plane = control
        runtime = GatewayDrainRuntime(control)
        server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(
            agent=base, control_plane=control, drain_runtime=runtime,
        ))
        server.start()
        try:
            pending = control.prepare_input({
                "id": "m21-worker-queued", "kind": "ask", "goal": "waiting in the original queue",
                "user_id": "alice", "metadata": {"channel": "feishu", "user_id": "alice"},
                "conversation_thread_id": thread.thread_id,
                "conversation": {"channel": "feishu", "channel_user_id": "alice",
                                 "channel_conversation_id": "control-session", "canonical_user_id": "alice"},
            })
            path = write_gateway_request(paths, pending)
            gate = PersistentAdmission(paths, base, CoordinatorAdmissionScope(2, controller.c_run_id))
            bind_admission_takeover(gate, controller)
            runtime.retain_admission(gate)
            decision = gate.admit(path, (owner, session_scope_for_request(pending)), AdmissionLimits(8, 8))
            assert decision["status"] == "admitted", decision
            code, denied = request(server, "POST", "/client/turn-stop", {
                "message_id": "other-owner-stop", "conversation_id": "control-session", "thread_id": thread.thread_id,
            }, headers={"X-User-Id": "bob"})
            assert code == 403 and denied["error_code"] == "EXECUTION_TARGET_CHANGED"
            assert not read_json_file(path).get("cancel_requested")
            body = {"message_id": "worker-stop-operation", "scope": "session", "conversation_id": "control-session"}
            code, prepared = request(server, "POST", "/client/turn-stop", body)
            assert code == 202 and prepared["target_thread_ids"] == [thread.thread_id], prepared
            case = ForwardCase(base, server, controller, owner, seeded, thread, {}, worker_config, [])
            with worker(case, monkeypatch) as (_, pump):
                completed = consume(case, pump, prepared["operation_id"])
                assert completed.state == "completed" and completed.result["details"]["queued_cancelled"] == 1, completed.to_dict()
            code, result = request(server, "POST", "/client/turn-stop", body)
            assert code == 200 and result["queued_cancelled"] == 1
            assert not path.exists() and not (paths.processing / path.name).exists()
            terminal = read_json_file(paths.terminal / path.name)
            assert terminal["terminal_response"]["status"] == "cancelled"
            released = release_reservation_if_terminal(gate, path.stem)
            assert released["released"] is True, released
            assert not paths.stop_request.exists()
        finally:
            if control.controller() is not None and control.controller().active():
                runtime.stop_producers()
            server.stop()
