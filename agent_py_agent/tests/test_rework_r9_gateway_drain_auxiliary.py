"""R9-R1 P1-R9-16：原审批桥正常消费后遗留的空锁设施不再阻断停止库存与排空快照；未结/活跃/危险布局仍阻断。

审批两端都是原实现：服务端 StreamApproval / serve_command_stream 等待并消费决定，客户端沿原
gateway_chunk_path / command_approval_path 调 write_gateway_permission_decision 回写。HTTP 输出流是合成
对象；不启动 Gateway 服务、模型或 native 进程，停止事实由原 Count0 停止 writer 在临时 home 写入。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from argparse import Namespace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from agent_py_agent.agent.gateway_parts import standalone_inventory as inventory
from agent_py_agent.agent.gateway_parts.command_stream import serve_command_stream
from agent_py_agent.agent.gateway_parts.command_stream_protocol import command_approval_path
from agent_py_agent.agent.gateway_parts.multiprocess_drain_sources import (
    durable_drain_snapshot,
    durable_items,
    same_durable_snapshot,
)
from agent_py_agent.agent.gateway_parts.multiprocess_stop_scope import CoordinatorStopScope
from agent_py_agent.agent.gateway_parts.paths import (
    claimed_request_chunk_path,
    gateway_chunk_path,
    gateway_paths_from_root,
)
from agent_py_agent.agent.gateway_parts.permission_bridge import (
    gateway_permission_decision_path,
    write_gateway_permission_decision,
)
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_migration_errors import (
    StandaloneMigrationRejected,
)
from agent_py_agent.agent.gateway_parts.standalone_runtime_inventory import responsibility_digest
from agent_py_agent.agent.gateway_parts.standalone_stop import record_standalone_stop
from agent_py_agent.agent.gateway_parts.stream_approval import (
    StreamApproval,
    StreamApprovalRequestOptions,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.cli import common, gateway_process
from agent_py_agent.cli.gateway_coordinated_run import _cold_gateway_paths

EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
COMMAND_OWNER = OwnerIdentity("local", "main", "main")


def _request(request_id: str, permission_id: str) -> ToolApprovalRequest:
    return ToolApprovalRequest(
        permission_id=permission_id, request_id=request_id, tool_name="synthetic", round=1, call_index=0,
        title="合成审批", description="仅审批桥合同",
        binding={"tool_name": "synthetic", "run_id": "run-r9", "operation_id": "operation-r9",
                 "idempotency_key": "idempotency-r9", "args_hash": "a" * 64},
        options=({"id": "yes", "decision": "approved", "label": "同意"},
                 {"id": "no", "decision": "denied", "label": "拒绝"}),
    )


def _approve(chunk_path, permission: dict) -> None:
    shown = ToolApprovalRequest.from_mapping(permission)
    write_gateway_permission_decision(chunk_path, shown, ToolApprovalDecision(shown.permission_id, "approved"))


# 前台请求：原 StreamApproval 在 claimed chunk 旁等待，客户端沿 gateway_chunk_path 回写，原桥消费。
def _complete_foreground(paths, owner=None, request_id="gwreq-r9-foreground"):
    request = _request(request_id, "permission-foreground")
    chunk = claimed_request_chunk_path(paths.processing / f"{request_id}.json", request_id)
    requested, events, outcome = threading.Event(), [], {}

    def publish(event):
        events.append(event)
        if event["kind"] == "permission_requested":
            requested.set()

    approval = StreamApproval(chunk, publish, lambda: None)
    waiter = threading.Thread(target=lambda: outcome.update(
        approval.request(request.to_dict(), options=StreamApprovalRequestOptions(True))))
    waiter.start()
    try:
        assert requested.wait(5)
        _approve(gateway_chunk_path(paths, request_id), events[-1]["permission"])
    finally:
        waiter.join(10)
    assert not waiter.is_alive() and outcome["decision"] == "approved"
    return gateway_permission_decision_path(chunk, request)


class _CommandWire:
    """合成 HTTP 输出：只记录原命令流帧并在审批请求出现时通知客户端。"""

    def __init__(self):
        self.frames, self.requested = [], threading.Event()

    def write(self, raw):
        frame = json.loads(raw)
        self.frames.append(frame)
        if frame["kind"] == "permission_requested":
            self.requested.set()
        return len(raw)

    def flush(self):
        return None


# 宿主命令：原 serve_command_stream 在 HTTP 线程等待，TUI 沿 command_approval_path 回写，原桥消费。
def _complete_host_command(paths, owner=COMMAND_OWNER, request_id="command-r9"):
    wire = _CommandWire()
    handler = SimpleNamespace(wfile=wire, connection=SimpleNamespace(settimeout=lambda _seconds: None),
                              close_connection=False, send_response=lambda _code: None,
                              send_header=lambda *_args: None, end_headers=lambda: None)
    request = _request(request_id, "permission-command")

    def execute(request_permission, token):
        return {"decision": request_permission(request.to_dict(), cancellation_token=token)["decision"]}

    server = threading.Thread(target=serve_command_stream, args=(handler, paths, owner, request_id, execute))
    server.start()
    try:
        assert wire.requested.wait(5)
        permission = next(f for f in wire.frames if f["kind"] == "permission_requested")["payload"]["permission"]
        _approve(command_approval_path(paths, owner, request_id), permission)
    finally:
        server.join(10)
    assert not server.is_alive()
    assert [f["payload"] for f in wire.frames if f["kind"] == "result"] == [{"decision": "approved"}]
    return gateway_permission_decision_path(command_approval_path(paths, owner, request_id), request)


COMPLETIONS = {"foreground": _complete_foreground, "host-command": _complete_host_command}


def _settled_lock(decision):
    lock = decision.with_name(decision.name + ".lock")
    # 原桥消费后只删除决定本身；锁仍在，故两层哈希目录不能被 rmdir。
    assert not decision.exists() and lock.is_file() and lock.read_bytes() == b""
    assert sorted(p.name for p in decision.parent.iterdir()) == [lock.name]
    return lock


# 原 Count0 正常停止 writer：完成审批后才记录停止事实，与真实先跑后停顺序一致。
def _stop_after(tmp_path, monkeypatch, complete):
    home = tmp_path / "home"
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(f'model_backend: echo\nmy_agent_home: "{home}"\ngateway_worker_process_count: 0\n')
    args = Namespace(config=config_path, workspace_root="")
    config = common.agent_configuration(args)[0]
    paths = _cold_gateway_paths(args)
    owner = resolve_owner_home(home)
    owner.home_dir.mkdir(parents=True)
    RuntimeRepository(runtime_db_path(owner.home_dir))
    paths.processing.mkdir(parents=True, exist_ok=True)
    decision = complete(paths, owner.identity)
    identity = {"host_id": "fixture-host", "pid": os.getpid(), "start_time": "fixture-birth"}
    context = SimpleNamespace(paths=paths, config=config, config_path=config_path, process_identity=identity,
                              process_started_at=1.0, coordinator_worker_count=0, control_plane=None)
    cleanup = {"drain_complete": True, "alive_threads": [], "interrupted_model_calls": 0,
               "host_shutdown_executors": 0, "surviving_background_sessions": []}
    monkeypatch.setattr(gateway_process, "log_gateway_event", lambda *_: None)
    with standalone_lifecycle_guard(paths.root) as held:
        gateway_process._record_gateway_service_termination(
            paths, None, os.getpid(),
            gateway_process._GatewayServiceTermination("stopped", "planned_stop", "fixture", "", 0), context=context)
        fact = record_standalone_stop(context, held, cleanup)
    return SimpleNamespace(home=home, config=config, paths=paths, fact=fact, decision=decision)


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / "gateway"
    root.mkdir(mode=0o700)
    value = gateway_paths_from_root(root)
    value.processing.mkdir(parents=True, mode=0o700)
    return value


def _sources(paths):
    namespaces, files = {}, {}
    inventory._gateway_sources(paths, namespaces, files)
    return namespaces, files


@pytest.mark.parametrize("kind", sorted(COMPLETIONS))
def test_normal_stop_after_completed_approval_records_stable_settled_lock(tmp_path, monkeypatch, kind):
    case = _stop_after(tmp_path, monkeypatch, COMPLETIONS[kind])
    lock = _settled_lock(case.decision)
    stop = case.fact["standalone_stop"]
    assert stop["blockers"] == [] and stop["sources"] is not None
    ref = "gateway/" + "/".join(lock.relative_to(case.paths.root).parts)
    current = inventory.standalone_inventory(case.home, case.config, case.paths)
    # 已结空锁带原身份目录链进入摘要；后继迁移重算与停止事实一致，锁本身不被删除或改写。
    assert current["files"][ref] == EMPTY_DIGEST
    assert all("gateway/" + "/".join(parent.relative_to(case.paths.root).parts) in current["namespaces"]
               for parent in (lock.parent, lock.parent.parent))
    assert inventory.standalone_inventory(case.home, case.config, case.paths) == current
    assert responsibility_digest(current) == stop["sources"]["digest"]
    assert lock.is_file() and lock.read_bytes() == b""


def test_drain_snapshot_classifies_completed_approval_facilities_and_keeps_requests(paths):
    locks = [_settled_lock(complete(paths)) for complete in COMPLETIONS.values()]
    scope = CoordinatorStopScope(2, "c-r9")
    snapshot = durable_drain_snapshot(paths, 2)
    # 原审批/命令设施只核身份，不变成 processing 责任；同一停止观察可重复确认。
    assert snapshot[0][1][1] == ()
    assert durable_items(scope, snapshot) == ()
    assert same_durable_snapshot(snapshot, durable_drain_snapshot(paths, 2))
    # 真实 processing 请求仍是排空责任，设施分类不吞业务项。
    (paths.processing / "gwreq-r9-live.json").write_text(json.dumps({"id": "gwreq-r9-live"}))
    items = durable_items(scope, durable_drain_snapshot(paths, 2))
    assert [(item.item_ref, item.state) for item in items] == [("requests/processing/gwreq-r9-live.json", "processing")]
    assert all(lock.is_file() and lock.read_bytes() == b"" for lock in locks)


def _pending_decision(paths, kind):
    if kind == "foreground":
        request, chunk = _request("gwreq-r9-pending", "permission-pending"), gateway_chunk_path(paths, "gwreq-r9-pending")
    else:
        request = _request("command-r9-pending", "permission-pending")
        chunk = command_approval_path(paths, COMMAND_OWNER, "command-r9-pending")
    write_gateway_permission_decision(chunk, request, ToolApprovalDecision(request.permission_id, "approved"))
    return gateway_permission_decision_path(chunk, request)


@pytest.mark.parametrize("kind", sorted(COMPLETIONS))
def test_pending_decision_and_unknown_entries_keep_blocking(paths, kind):
    decision = _pending_decision(paths, kind)
    with pytest.raises(StandaloneMigrationRejected) as pending:
        _sources(paths)
    assert pending.value.reason == "processing_auxiliary_unsettled"
    assert pending.value.ref == "/".join(decision.relative_to(paths.root).parts)
    decision.unlink()
    lock = decision.with_name(decision.name + ".lock")
    stage = decision.with_name(f".{decision.name}.{'0' * 32}.tmp")
    stage.write_bytes(b"")
    with pytest.raises(StandaloneMigrationRejected, match="processing_auxiliary_unsettled"):
        _sources(paths)
    stage.unlink()
    assert _sources(paths)[1]["gateway/" + "/".join(lock.relative_to(paths.root).parts)] == EMPTY_DIGEST
    # 宿主命令目录从不写 approval.chunks.jsonl；前台决定桶不接受非哈希名称。
    unknown = (command_approval_path(paths, COMMAND_OWNER, "command-r9-pending") if kind == "host-command"
               else decision.parent.parent / "not-a-request-bucket")
    unknown.write_bytes(b"")
    with pytest.raises(StandaloneMigrationRejected) as unexpected:
        _sources(paths)
    assert (unexpected.value.reason, unexpected.value.ref) == (
        "processing_auxiliary_unsettled", "/".join(unknown.relative_to(paths.root).parts))


@pytest.mark.parametrize("kind", sorted(COMPLETIONS))
def test_live_lock_holder_and_nonempty_lock_keep_blocking(paths, kind):
    decision = COMPLETIONS[kind](paths)
    lock = _settled_lock(decision)
    # 原锁原语持有即活跃 writer/consumer；探测不等待也不改动锁。
    with locked_json_path(decision):
        with pytest.raises(StandaloneMigrationRejected) as held:
            _sources(paths)
    assert (held.value.reason, held.value.ref) == (
        "processing_approval_lock_unsettled", "/".join(lock.relative_to(paths.root).parts))
    before = os.stat(lock)
    assert _sources(paths)[1]
    lock.write_bytes(b"x")
    with pytest.raises(StandaloneMigrationRejected, match="processing_approval_lock_unsettled"):
        _sources(paths)
    assert (os.stat(lock).st_dev, os.stat(lock).st_ino) == (before.st_dev, before.st_ino)


@pytest.mark.parametrize("kind", sorted(COMPLETIONS))
@pytest.mark.parametrize("unsafe", ["symlink-lock", "hardlink-lock", "symlink-bucket"])
def test_unsafe_links_keep_blocking(paths, tmp_path, kind, unsafe):
    lock = _settled_lock(COMPLETIONS[kind](paths))
    outside = tmp_path / "outside"
    outside.mkdir()
    if unsafe == "symlink-lock":
        lock.unlink()
        (outside / "target.lock").write_bytes(b"")
        lock.symlink_to(outside / "target.lock")
    elif unsafe == "hardlink-lock":
        os.link(lock, outside / "alias.lock")
    else:
        bucket = lock.parent
        bucket.rename(outside / "bucket")
        bucket.symlink_to(outside / "bucket", target_is_directory=True)
    with pytest.raises(OSError):
        _sources(paths)
