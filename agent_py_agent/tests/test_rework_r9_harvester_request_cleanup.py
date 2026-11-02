# LLM: 真实 P3/P5 worker claim、原 background_request_scope、原租约/WAL/提交与 _finish；只替换来源 HTTP 抓取、
#   父 Audit 任务观察与 Audit 岗位通知这三处外部协作者，每个场景至多一次普通模拟 I/O 错误或一次 typed 权限变化。
# 模块用途: 回归 P1-R9-15——收割提交后通知失败仍在 finally 内执行原租约收尾，typed 未知/过期/冲突不删租约、不新领租约。
from __future__ import annotations

import errno
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.core import configure_canonical_background_contracts
from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    gateway_request_fingerprint,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    freeze_source,
    read_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import bind_claimed_request
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_claim import (
    ClaimCandidate,
    freeze_claim_snapshot,
)
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.multiprocess_route import (
    RouteStamp,
    WorkerPartition,
    freeze_request_route,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.agent.ingestion import harvest_execution as execution
from agent_py_agent.agent.ingestion import harvest_parent, source_worker
from agent_py_agent.agent.ingestion import harvester as hv
from agent_py_agent.agent.ingestion import watch_state as ws
from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.support.multiprocess_dispatch_cases import reservation_for

_ENVELOPE = {
    "mode": "cursor", "record_boundary": "array_item", "record_list_key": "items",
    "cursor_field": "next_cursor", "cursor_semantics": "next_position",
    "request": {"method": "GET", "cursor_binding": {"location": "query", "name": "since", "initial": 0},
                "page_size_binding": {"location": "query", "name": "limit"}},
    "valid": True,
}


# LLM: 与原 stamped_request 同一字段与指纹顺序，只在指纹前放入原 producer 冻结的 harvester 来源引用。
# 函数用途: 构造携带精确 background_source 的已盖戳合成请求。
def _stamped_harvester_request(owner, ref):
    request = {"id": "harvest-request", "kind": "ask", "prompt": "合成收割请求", "status": "pending",
               "user_id": "synthetic-user", "metadata": {"provider": "synthetic", "channel": "synthetic",
               "user_id": "synthetic-user", "chat_id": "chat-a", "background_source": ref},
               "conversation": {"channel": "synthetic", "canonical_user_id": "synthetic-user",
                                "channel_conversation_id": "chat-a"}}
    request[REQUEST_OWNER_KEY] = asdict(owner)
    request["metadata"]["gateway_admin_binding_version"] = "binding-v1"
    request["request_fingerprint"] = gateway_request_fingerprint(request, request["id"])
    request["request_fingerprint_schema"] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    return freeze_request_route(request, owner, RouteStamp(2, "c-p5", 100.0, "binding-v1"))


# LLM: 真实 owner home、真实保证档 watch 快照与原 freeze_source 引用；请求经 P3 预留、P5 快照与原 claim 入口认领。
# 函数用途: 准备一拍原请求收割所需的 claim、来源引用、agent 与抓取计数。
@pytest.fixture
def harvest_case(tmp_path, monkeypatch):
    configure_canonical_background_contracts()
    monkeypatch.setattr(execution, "registry", ws.WatchRegistry())
    owner = OwnerIdentity.provider_user("synthetic", "alice")
    root = tmp_path / "home"
    agent = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root, owner)))
    owner_home = agent.home_paths.owner_home_dir
    state = ws.new_state(owner_home, "http://src.example/pull", {"full_read_per_pull": 4})
    state.source_envelope = dict(_ENVELOPE)
    state.audit_guarantee = True
    state.owner_id = agent.home_paths.owner_id
    ws.persist_state(state)
    ref = freeze_source(root, owner, owner_home / "watch_state" / f"{state.watch_id}.json", "harvester")
    request = _stamped_harvester_request(owner, ref)
    launch = mode.WorkerLaunch(WorkerPartition(request["route"]["worker"], 2, "c-p5"), tmp_path / "gateway", root, 101)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    paths = gateway_paths_from_root(launch.gateway_root)
    ensure_gateway_folders(paths)
    path = paths.inbox / f"{request['id']}.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    snapshot = freeze_claim_snapshot(freeze_worker_bootstrap(launch), ClaimCandidate(path, request, reservation_for(request)))
    reservation = reservation_file(paths, request["id"])
    reservation.parent.mkdir(parents=True, exist_ok=True)
    reservation.write_bytes(snapshot.reservation_bytes)
    claim = claim_request(paths, path, worker_snapshot=snapshot)
    assert claim is not None
    current = json.loads(claim.path.read_text(encoding="utf-8"))
    binding = bind_claimed_request(snapshot, claim, current)
    fetches = []

    def fetch(_tool, request_obj):
        fetches.append(request_obj)
        return True, {"items": [{"seq": n, "kind": "beat", "note": f"n{n}"} for n in range(3)],
                      "next_cursor": 3, "has_more": False}, ""

    monkeypatch.setattr(WatchStreamTool, "_fetch_json", fetch)
    monkeypatch.setattr(harvest_parent, "observe_harvest_parent", lambda *_args: None)
    monkeypatch.setattr(execution, "_parent_active", lambda *_args: True)
    context = {"agent": agent, "request": current, "request_path": claim.path, "request_id": claim.request_id}
    return SimpleNamespace(agent=agent, binding=binding, context=context, ref=ref, root=root,
                           state=state, fetches=fetches)


# LLM: 只在原 worker 执行域与原后台请求域内调用产品入口；返回值或异常原样交给断言。
# 函数用途: 以原运输作用域执行一拍收割。
def _run(case):
    with worker_execution_scope(case.binding, case.agent), bridge.background_request_scope(case.context):
        return execution.execute_harvester_source(case.agent, case.ref, read_source(case.root, case.ref))


# 函数用途: 返回该 watch 的原租约文件路径。
def _lease(case) -> Path:
    return hv._lease_path(case.state)


# 函数用途: 读取盘上已提交的 watch 快照。
def _committed(case):
    return ws.load_state(case.state.owner_home, case.state.watch_id)


# LLM: 通知一次普通失败：已提交游标/spool 保持，原租约仍按 finally 收尾释放，对外异常就是通知失败本身。
# 函数用途: 验证通知失败不遗留租约、不回滚提交、不被收尾吞掉。
def test_notification_failure_still_releases_original_lease(harvest_case, monkeypatch):
    def failing_wake(_agent, _state):
        assert _lease(harvest_case).exists()
        raise OSError(errno.EIO, "simulated I/O error")

    monkeypatch.setattr(source_worker, "wake_audit_source_worker", failing_wake)
    with pytest.raises(OSError, match="simulated I/O error"):
        _run(harvest_case)

    assert not _lease(harvest_case).exists()
    committed = _committed(harvest_case)
    assert committed.cursor == 3 and committed.spool_seq >= 1
    assert hv._read_harvest_transaction(committed) is None
    assert len(harvest_case.fetches) == 1


# LLM: 收尾删除租约遇一次普通 I/O 错误：沿同一原计划/作用域有界重试后释放，本拍结果照常 handled。
# 函数用途: 验证普通清理错误的有界重试。
def test_ordinary_cleanup_error_is_retried_with_same_plan(harvest_case, monkeypatch):
    monkeypatch.setattr(source_worker, "wake_audit_source_worker", lambda *_args: True)
    lease_path = _lease(harvest_case)
    original = Path.unlink
    calls = {"failed": 0}

    def flaky_unlink(self, *args, **kwargs):
        if self == lease_path and not calls["failed"]:
            calls["failed"] += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    result = _run(harvest_case)

    assert calls["failed"] == 1
    assert result == {"outcome": "handled", "status": "ok", "cursor": 3}
    assert not lease_path.exists() and _committed(harvest_case).cursor == 3


# LLM: 通知期间租约被接手者换代（冲突）：收尾核到非原租约即 typed 未知，不删接手者租约、不新领租约。
# 函数用途: 验证租约冲突不授权删除。
def test_lease_conflict_during_notification_is_typed_and_keeps_successor(harvest_case, monkeypatch):
    lease_path = _lease(harvest_case)
    successor = {}

    def takeover(_agent, _state):
        current = json.loads(lease_path.read_text(encoding="utf-8"))
        successor.update(current, lease_id="successor-lease")
        lease_path.write_text(json.dumps(successor), encoding="utf-8")
        return True

    monkeypatch.setattr(source_worker, "wake_audit_source_worker", takeover)
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        _run(harvest_case)

    assert json.loads(lease_path.read_text(encoding="utf-8")) == successor
    assert _committed(harvest_case).cursor == 3


# LLM: 通知期间原执行窗口过期（原 lifetime 失活）：收尾短门报 typed 未知，原租约记录保留给确实死者接手，不删除。
# 函数用途: 验证执行权过期不授权删除租约。
def test_expired_scope_during_notification_keeps_original_lease(harvest_case, monkeypatch):
    lease_path = _lease(harvest_case)
    held = {}

    def expire(_agent, _state):
        held["lease"] = lease_path.read_bytes()
        bridge.current_background_request().lifetime.active = False
        return True

    monkeypatch.setattr(source_worker, "wake_audit_source_worker", expire)
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        _run(harvest_case)

    assert lease_path.read_bytes() == held["lease"]
    assert _committed(harvest_case).cursor == 3
