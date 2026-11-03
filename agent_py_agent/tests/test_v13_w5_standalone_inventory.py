"""#52：临时假树核真实目录、文件锁及正常 stop→迁移，不启动服务。"""
from __future__ import annotations

import builtins
import hashlib
import json
import os
import stat
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.gateway_parts import standalone_inventory as inventory
from agent_py_agent.agent.gateway_parts import standalone_stop as stop
from agent_py_agent.agent.gateway_parts.command_stream_protocol import command_approval_path
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.permission_bridge import write_gateway_permission_decision
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_migration_errors import (
    StandaloneMigrationRejected,
)
from agent_py_agent.agent.gateway_parts.standalone_stop import record_standalone_stop
from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_gateway_standalone_migration import _prepare, stopped_home


@pytest.fixture
def paths(tmp_path):
    value = gateway_paths_from_root(tmp_path / "gateway")
    value.processing.mkdir(parents=True)
    return value


def _sources(paths):
    namespaces, files = {}, {}
    report = inventory._gateway_sources(paths, namespaces, files)
    return namespaces, files, report


def _local_tree(tmp_path):
    home = tmp_path / "home"
    resolve_owner_home(home).home_dir.mkdir(parents=True)
    for uid in ("qimiaokeju11..88", "real-test-a", "scale-proof"):
        owner = resolve_owner_home(home, OwnerIdentity.local_user(uid))
        owner.home_dir.mkdir()
        canonical = process_session_store_root(owner.home_dir, owner.home_dir)
        canonical.mkdir(parents=True)
        (canonical.parent / uid).mkdir()
    return home


def test_local_users_and_native_namespaces_are_bound(tmp_path):
    home = _local_tree(tmp_path)
    namespaces = {}
    owners = inventory._owners(home, namespaces)
    assert [owner.identity for owner in owners] == [OwnerIdentity.local_main()] + [
        OwnerIdentity.local_user(uid) for uid in ("qimiaokeju11..88", "real-test-a", "scale-proof")]
    inventory._native_namespaces(home, owners, namespaces)
    expected = {root.name for owner in owners[1:] for root in (
        process_session_store_root(owner.home_dir, owner.home_dir),
        process_session_store_root(owner.home_dir, owner.home_dir).parent / owner.identity.owner_id)}
    assert set(namespaces["owners/local/.my-agent-runtime/process_sessions"][1]) == expected


def test_exact_regular_ds_store_ignored_without_opening_nondirectories(tmp_path, monkeypatch):
    home = _local_tree(tmp_path)
    provider = home / "owners/providers/example/users/user-a"
    provider.mkdir(parents=True)
    for parts in ("owners", "owners/local", "owners/providers", "owners/providers/example",
                  "owners/providers/example/users"):
        (home / parts / ".DS_Store").write_bytes(b"finder metadata")
    original = inventory.open_directory_beneath

    def checked(root, parts, **kwargs):
        target = root.joinpath(*parts)
        if target.exists():
            assert stat.S_ISDIR(target.lstat().st_mode), parts
        return original(root, parts, **kwargs)

    monkeypatch.setattr(inventory, "open_directory_beneath", checked)
    namespaces = {}
    assert len(inventory._owners(home, namespaces)) == 5
    assert all(".DS_Store" not in value[1] for value in namespaces.values())


@pytest.mark.parametrize("place", ["owners", "owners/local", "owners/providers"])
@pytest.mark.parametrize("unsafe", ["symlink", "other-file", "invalid-uid", "regular-uid"])
def test_unknown_or_link_owner_entries_never_open_as_directory(tmp_path, monkeypatch, place, unsafe):
    home = _local_tree(tmp_path)
    parent = home / place
    parent.mkdir(exist_ok=True)
    if unsafe == "symlink":
        (parent / ".DS_Store").symlink_to(home / "owners/local/main", target_is_directory=True)
    elif unsafe == "invalid-uid":
        (parent / "-bad-").mkdir()
    else:
        (parent / (".DS_Store.extra" if unsafe == "other-file" else "looks-valid")).write_bytes(b"")
    original = inventory.open_directory_beneath

    def checked(root, parts, **kwargs):
        target = root.joinpath(*parts)
        if target.exists():
            assert stat.S_ISDIR(target.lstat().st_mode), parts
        return original(root, parts, **kwargs)

    monkeypatch.setattr(inventory, "open_directory_beneath", checked)
    with pytest.raises((StandaloneMigrationRejected, OSError, ValueError)):
        inventory._owners(home, {})


def _decision(paths, index=0, *, state="completed", decision="approved"):
    request_id, permission_id = f"gwreq-history-{index}", f"permission-{index}"
    request_hash = hashlib.sha256(request_id.encode()).hexdigest()[:24]
    permission_hash = hashlib.sha256(permission_id.encode()).hexdigest()[:24]
    path = paths.processing / ".approvals" / request_hash / (permission_hash + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**ToolApprovalDecision(permission_id, decision).to_dict(), "request_id": request_id,
           "binding": {"tool_name": "synthetic", "run_id": "run-history", "operation_id": "op-history",
                       "idempotency_key": "key-history", "args_hash": "a" * 64}}
    if state is not None:
        row["state"] = state
    path.write_text(json.dumps(row))
    return path


def test_31_structurally_settled_approvals_remain_in_place(paths):
    decisions = [_decision(paths, i, decision=("approved", "denied", "cancelled")[i % 3]) for i in range(31)]
    before = {path: path.read_bytes() for path in decisions}
    namespaces, files, report = _sources(paths)
    assert len(files) == 31 and namespaces
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert _sources(paths) == (namespaces, files, report)


@pytest.mark.parametrize("damage", ["pending", "unknown", "no-decision", "text-only", "bad-binding", "wrong-id"])
def test_unconsumed_or_invalid_approvals_keep_blocking(paths, damage):
    target = _decision(paths, state=None if damage == "pending" else "completed")
    row = json.loads(target.read_text())
    if damage == "unknown":
        row["decision"] = "mystery"
    elif damage == "no-decision":
        del row["decision"]
    elif damage == "text-only":
        row.pop("state")
        row["feedback"] = "completed consumed 已完成"
    elif damage == "bad-binding":
        row["binding"] = {}
    elif damage == "wrong-id":
        row["permission_id"] = "other"
    target.write_text(json.dumps(row))
    with pytest.raises(StandaloneMigrationRejected, match="processing_auxiliary_unsettled"):
        _sources(paths)


def _orphan(paths, *, old=True):
    path = paths.processing / "gwreq-orphan.json.lock"
    path.write_bytes(b"")
    if old:
        os.utime(path, (time.time() - 90000,) * 2)
    return path


def test_orphan_lock_report_is_readonly_and_age_bounded(paths):
    lock = _orphan(paths)
    report = _sources(paths)[2]
    assert report["cleanable_locks"] == ["requests/processing/" + lock.name]
    assert lock.exists()
    os.utime(lock, None)
    assert _sources(paths)[2]["cleanable_locks"] == []
    os.utime(lock, (time.time() - 90000,) * 2)
    lock.with_suffix("").write_text("{}")
    with pytest.raises(StandaloneMigrationRejected, match="processing_request_present"):
        _sources(paths)


def test_normal_stop_cleans_only_unlocked_stale_orphan_then_migrates(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    case.configure(0)
    case.paths.processing.mkdir(parents=True, exist_ok=True)
    stale = _orphan(case.paths)
    fresh = case.paths.processing / "gwreq-fresh.json.lock"
    fresh.write_bytes(b"")
    # 普通预检不核 launcher 也不能删除；只有实际正常 stop 持锁上下文可清理。
    inventory.standalone_inventory(case.home, case.context.config, case.paths, check_launcher=False)
    assert stale.exists()
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    assert not stale.exists() and fresh.exists()
    case.args, case.config = case.configure(2)
    with coordinator_guard(case.paths.root, 2):
        assert not _prepare(case)._all_quarantined


def test_occupied_stale_lock_not_deleted_on_stop(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    case.configure(0)
    case.paths.processing.mkdir(parents=True, exist_ok=True)
    lock = _orphan(case.paths)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with locked_json_path(lock.with_suffix("")):
        with standalone_lifecycle_guard(case.paths.root) as held:
            fact = record_standalone_stop(case.context, held, cleanup)
        assert lock.exists()
        assert fact["standalone_stop"]["blockers"]


def test_missing_fcntl_still_fails_closed(paths, monkeypatch):
    target = _decision(paths)
    target.unlink()
    lock = target.with_name(target.name + ".lock")
    lock.write_bytes(b"")
    original = builtins.__import__

    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("synthetic platform")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    with pytest.raises(StandaloneMigrationRejected, match="processing_approval_lock_unsettled"):
        _sources(paths)
    assert lock.exists()


def _legacy(paths, index=0, **overrides):
    operation_id = "gwctl-msg-" + f"{index:032x}"
    path = paths.root / "control_operations" / (operation_id + ".json")
    path.parent.mkdir(exist_ok=True)
    row = {"schema_version": "gateway_control_operation.v1", "operation_id": operation_id,
           "state": "completed", "result": {"ok": True}, **overrides}
    path.write_text(json.dumps(row))
    return path


def test_legacy_completed_receipts_are_versioned_in_place_with_archive_tolerated(paths):
    first, second = _legacy(paths), _legacy(paths, 1)
    before = {path: path.read_bytes() for path in (first, second)}
    archive = paths.root / "archive" / "old"
    archive.mkdir(parents=True)
    (archive / "anything.json").write_text("not active")
    report = _sources(paths)[2]
    assert len(report["legacy_completed"]) == 2
    assert all(row["schema_version"] == "gateway_control_operation.v1" for row in report["legacy_completed"])
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert sorted(p.name for p in archive.iterdir()) == ["anything.json"]


@pytest.mark.parametrize("row", [{}, [], {"state": "completed"}, {"state": "executing"},
                                {"state": "terminal_unknown"}, {"schema_version": "future.v99"},
                                {"dispatch": {}}, {"result": "completed"}, {"operation_id": "different"}])
def test_unknown_or_inflight_legacy_control_still_blocks(paths, row):
    target = _legacy(paths, **row) if isinstance(row, dict) and row else _legacy(paths)
    if not row or isinstance(row, list) or row == {"state": "completed"}:
        target.write_text(json.dumps(row))
    with pytest.raises((StandaloneMigrationRejected, ValueError), match="control_operation_unsettled"):
        _sources(paths)


def test_local_owner_full_inventory_uses_existing_runtime_only(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    user = resolve_owner_home(case.home, OwnerIdentity.local_user("real-test-a"))
    user.home_dir.mkdir()
    RuntimeRepository(runtime_db_path(user.home_dir))
    process_session_store_root(user.home_dir, user.home_dir).mkdir(parents=True, exist_ok=True)
    inventory.prepare_standalone_read_facilities(case.home)
    snapshot = inventory.standalone_inventory(case.home, case.config, case.paths)
    assert {row["owner_id"] for row in snapshot["owners"]} == {"main", "real-test-a"}
    assert user.owner_id in snapshot["databases"]


def test_literal_uid_native_namespace_is_inspected_not_just_allowed(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    user = resolve_owner_home(case.home, OwnerIdentity.local_user("real-test-a"))
    user.home_dir.mkdir()
    RuntimeRepository(runtime_db_path(user.home_dir))
    literal = process_session_store_root(user.home_dir, user.home_dir).parent / "real-test-a"
    literal.mkdir()
    inventory.prepare_standalone_read_facilities(case.home)
    inventory.standalone_inventory(case.home, case.config, case.paths)
    (literal / ".process-sessions.redo.json").write_text("{}")
    with pytest.raises(StandaloneMigrationRejected, match="native_redo_requires_recovery"):
        inventory.standalone_inventory(case.home, case.config, case.paths)


@pytest.mark.parametrize("damage", ["nonempty", "symlink", "json-present", "young"])
def test_stop_cleanup_does_not_delete_unproven_orphans(tmp_path, monkeypatch, damage):
    case = stopped_home(tmp_path, monkeypatch)
    case.configure(0)
    case.paths.processing.mkdir(parents=True, exist_ok=True)
    lock = _orphan(case.paths)
    if damage == "nonempty":
        lock.write_bytes(b"unsettled")
        os.utime(lock, (time.time() - 90000,) * 2)
    elif damage == "symlink":
        outside = case.home / "not-a-lock"
        outside.write_bytes(b"")
        lock.unlink()
        lock.symlink_to(outside)
    elif damage == "json-present":
        lock.with_suffix("").write_text("{}")
    else:
        os.utime(lock, None)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert lock.exists()
    if damage != "young":
        assert fact["standalone_stop"]["blockers"]


def test_data_file_appearing_before_unlink_prevents_cleanup(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    case.configure(0)
    case.paths.processing.mkdir(parents=True, exist_ok=True)
    lock = _orphan(case.paths)
    original = inventory._missing_leaf
    calls = []

    def published(parent, name):
        calls.append(name)
        if len(calls) == 2:
            lock.with_suffix("").write_text("{}")
        return original(parent, name)

    monkeypatch.setattr(inventory, "_missing_leaf", published)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert lock.exists() and lock.with_suffix("").exists()
    assert fact["standalone_stop"]["blockers"]


def test_corrupt_current_receipt_cannot_use_legacy_completed_bypass(paths):
    target = _legacy(paths, schema_version="gateway_control_operation.v2")
    with pytest.raises(StandaloneMigrationRejected, match="control_operation_unsettled"):
        _sources(paths)
    assert target.exists()


def test_later_bad_receipt_is_not_swallowed_after_legacy_completion(paths):
    _legacy(paths)
    bad = _legacy(paths, 1, state="executing")
    with pytest.raises(StandaloneMigrationRejected) as rejected:
        _sources(paths)
    assert rejected.value.reason == "control_operation_unsettled"
    assert rejected.value.ref == "control_operations/" + bad.name


def test_missing_fcntl_on_orphan_never_deletes(paths, monkeypatch):
    lock = _orphan(paths)
    original = builtins.__import__

    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("synthetic platform")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    with pytest.raises(StandaloneMigrationRejected, match="processing_approval_lock_unsettled"):
        _sources(paths)
    assert lock.exists()


# LLM: 只在tmp使用真实审批writer和RuntimeDB领取/结算，不给决定补写state/status。
# 函数用途: 建立可精确核run/op/identity的历史决定，或保留相同协议的在途决定。
def _writer_history(case, index=0, *, complete=True, kind="foreground"):
    run_id, operation_id = f"run-writer-{index}", f"operation-writer-{index}"
    ids = case.repo.record_run_creation(owner_id=case.owner.owner_id, run_id=run_id)
    call = SimpleNamespace(call_id=f"call-{index}", tool_name="synthetic", run_id=run_id,
                           operation_id=operation_id, idempotency_key=f"key-{index}",
                           args_hash="a" * 64, turn_id=f"turn-{index}")
    store = ManagedOperationStore(case.repo)
    claimed = store.claim_tool_operation(ToolOperationClaimRequest(
        owner_id=case.owner.owner_id, run_id=run_id, task_id=ids["task_id"],
        operation_id=operation_id, tool=call.tool_name, args_hash=call.args_hash,
        idempotency_key=call.idempotency_key, idempotency_scope="operation", idempotency_namespace="test",
        holder=new_tool_operation_holder(), lease_expires_at=time.time() + 300, attempt_id=ids["attempt_id"]))
    assert claimed.action == "execute"
    request = build_tool_approval_request(call, request_id=f"gwreq-writer-{index}", round_number=1,
                                         call_index=0, description="临时历史审批")
    chunk = (command_approval_path(case.paths, case.owner.identity, request.request_id)
             if kind == "host-command" else case.paths.processing / "unused.chunks.jsonl")
    target = write_gateway_permission_decision(chunk, request, ToolApprovalDecision(request.permission_id, "approved"))
    assert not {"state", "status"}.intersection(json.loads(target.read_text()))
    if complete:
        store.finish_tool_operation(ToolOperationCompletionRequest(
            owner_id=case.owner.owner_id, run_id=run_id, operation_id=operation_id,
            holder_id=claimed.record.holder_id, generation=claimed.record.generation,
            status="succeeded", result={"ok": True}))
        assert case.repo.settle_agent_run(agent_run_id=ids["agent_run_id"], attempt_id=ids["attempt_id"],
                                          status="done")["settled"]
        assert case.repo.settle_task_run_if_agent_tree_terminal(
            task_run_id=ids["task_run_id"], task_id=ids["task_id"])["settled"]
    return target, ids, operation_id


# LLM: 31条只是合成历史批量，不代表生产31条已取样；停止和迁移沿原入口。
# 函数用途: 验证真实writer无终态字段的决定被规范结算证据放行且原地保留。
def test_31_real_writer_settled_approvals_stop_then_migrate(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    targets = [_writer_history(case, i, kind="host-command" if i % 2 else "foreground")[0] for i in range(31)]
    before = {target: target.read_bytes() for target in targets}
    case.configure(0)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    assert all(target.read_bytes() == raw for target, raw in before.items())
    case.args, case.config = case.configure(2)
    with coordinator_guard(case.paths.root, 2):
        assert not _prepare(case)._all_quarantined


# LLM: 同一真实writer，只有权威结算/身份受损；不按文案、年龄或空目录放行。
# 函数用途: 覆盖未消费、缺证据、错误binding和未知/在途/坏库的失败关闭。
@pytest.mark.parametrize("damage", ["pending", "missing-op", "missing-db", "corrupt-db",
    "binding-run_id", "binding-operation_id", "binding-tool_name", "binding-idempotency_key", "binding-args_hash",
    "missing-binding", "unknown", "no-settled", "no-ended", "run-created", "attempt-pending", "no-closed",
    "owner", "generation", "broken-payload", "missing-payload-key", "wrong-command-owner", "explicit-pending"])
def test_real_writer_unproven_history_still_blocks(tmp_path, monkeypatch, damage):
    case = stopped_home(tmp_path, monkeypatch)
    target, ids, op = _writer_history(case, complete=damage != "pending", kind="host-command"
                                     if damage == "wrong-command-owner" else "foreground")
    inventory.prepare_standalone_read_facilities(case.home)
    if damage != "pending":
        assert inventory.standalone_inventory(case.home, case.config, case.paths)
    if damage.startswith("binding-") or damage in {"missing-binding", "explicit-pending"}:
        row = json.loads(target.read_text())
        if damage == "missing-binding":
            del row["binding"]
        elif damage == "explicit-pending":
            row["state"] = "pending"
        else:
            row["binding"][damage.removeprefix("binding-")] = "wrong"
        target.write_text(json.dumps(row))
    elif damage == "missing-db":
        runtime_db_path(case.owner.home_dir).unlink()
    elif damage == "corrupt-db":
        runtime_db_path(case.owner.home_dir).write_bytes(b"not sqlite")
    elif damage == "wrong-command-owner":
        bucket = target.parents[3]
        wrong = bucket.with_name("0" * 64) / target.relative_to(bucket)
        wrong.parent.mkdir(parents=True)
        target.rename(wrong)
    elif damage != "pending":
        with case.repo._runtime_connection() as conn:
            if damage == "missing-op":
                conn.execute("DELETE FROM tool_operations WHERE operation_id=?", (op,))
            elif damage in {"unknown", "no-settled", "generation", "broken-payload", "missing-payload-key"}:
                if damage == "missing-payload-key":
                    payload = json.loads(conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?", (op,)).fetchone()[0])
                    del payload["idempotency_key"]
                    conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?", (json.dumps(payload), op))
                else:
                    column, value = {"unknown": ("status", "UNKNOWN"), "no-settled": ("settled_at", 0),
                        "generation": ("attempt_generation", 99), "broken-payload": ("outcome_json", "{")}[damage]
                    conn.execute(f"UPDATE tool_operations SET {column}=? WHERE operation_id=?", (value, op))
            elif damage in {"no-ended", "attempt-pending"}:
                column, value = ("ended_at", 0) if damage == "no-ended" else ("status", "pending")
                conn.execute(f"UPDATE agent_attempts SET {column}=? WHERE attempt_id=?", (value, ids["attempt_id"]))
            elif damage == "run-created":
                conn.execute("UPDATE agent_runs SET status='created' WHERE agent_run_id=?", (ids["agent_run_id"],))
            elif damage == "no-closed":
                conn.execute("UPDATE task_runs SET closed_at=0 WHERE task_run_id=?", (ids["task_run_id"],))
            elif damage == "owner":
                conn.execute("UPDATE tasks SET owner_id='other' WHERE task_id=?", (ids["task_id"],))
            conn.commit()
        # 写连接关闭可能回收WAL设施；由原准备口恢复，避免反例只测到sidecar缺失。
        inventory.prepare_standalone_read_facilities(case.home)
    with pytest.raises((StandaloneMigrationRejected, OSError, ValueError)) as rejected:
        inventory.standalone_inventory(case.home, case.config, case.paths)
    if damage not in {"corrupt-db", "broken-payload"}:
        assert rejected.value.reason == "processing_auxiliary_unsettled"


# LLM: 原runtime digest未含outcome_json/closed_at；新增读取事实必须进入原stop摘要比较。
# 函数用途: 保持合法终态，仅改变新读取责任，验证后继拒绝旧停止回执。
@pytest.mark.parametrize("change", ["payload", "closed-at"])
def test_real_writer_new_evidence_is_in_stable_stop_digest(tmp_path, monkeypatch, change):
    case = stopped_home(tmp_path, monkeypatch)
    _target, ids, op = _writer_history(case)
    case.configure(0)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    before = inventory.standalone_inventory(case.home, case.config, case.paths)
    with case.repo._runtime_connection() as conn:
        if change == "payload":
            row = json.loads(conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?", (op,)).fetchone()[0])
            row["history_revision"] = 2
            conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?", (json.dumps(row), op))
        else:
            conn.execute("UPDATE task_runs SET closed_at=closed_at+1 WHERE task_run_id=?", (ids["task_run_id"],))
        conn.commit()
    inventory.prepare_standalone_read_facilities(case.home)
    assert inventory.standalone_inventory(case.home, case.config, case.paths) != before
    case.args, case.config = case.configure(2)
    with coordinator_guard(case.paths.root, 2), pytest.raises(StandaloneMigrationRejected):
        _prepare(case)


# LLM: 前后完整文件字节核对，仓储仍是真实实现，只观察连接参数，不替换读结果。
# 函数用途: 验证新证据读取仅使用existing/readonly，不改审批、DB或已有文件。
def test_real_writer_evidence_inventory_is_readonly(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    _writer_history(case)
    inventory.prepare_standalone_read_facilities(case.home)
    original, calls = inventory.RuntimeRepository, []

    # LLM: 仅记录构造参数并调用原类，任何读写模式漂移都直接失败。
    # 函数用途: 确认审批库存连接不初始化或迁移现存库。
    def observed(path, **kwargs):
        assert kwargs == {"read_only": True, "existing_only": True}
        calls.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(inventory, "RuntimeRepository", observed)
    before = {str(path.relative_to(case.home)): path.read_bytes()
              for path in case.home.rglob("*") if path.is_file()}
    assert inventory.standalone_inventory(case.home, case.config, case.paths)
    assert calls == [runtime_db_path(case.owner.home_dir)]
    after = {str(path.relative_to(case.home)): path.read_bytes()
             for path in case.home.rglob("*") if path.is_file()}
    assert after == before


# LLM: 在原stop双读的准确中间观察点修改新证据；不是按mtime猜测变化。
# 函数用途: 新读取责任在首末采样间漂移时必须拒绝签发无blocker停止回执。
def test_real_writer_evidence_change_between_stop_samples_is_rejected(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    _target, _ids, op = _writer_history(case)
    case.configure(0)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]

    # LLM: 真实SQL提交后由原准备口恢复读设施，只改变责任不伪造停止结果。
    # 函数用途: 在两次库存之间确定性发表另一合法终态payload。
    def between_samples():
        with case.repo._runtime_connection() as conn:
            row = json.loads(conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?", (op,)).fetchone()[0])
            row["history_revision"] = 2
            conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?", (json.dumps(row), op))
            conn.commit()
        inventory.prepare_standalone_read_facilities(case.home)
        return ()

    monkeypatch.setattr(stop, "model_call_drain_snapshot", between_samples)
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["sources"] is None
    assert fact["standalone_stop"]["blockers"] == [{"reason": "stop_sources_changed", "ref": ""}]


@pytest.mark.parametrize("current_state", ["settled", "running", "missing-ended", "bad-generation"])
# LLM: 真API换代保留旧终态操作；历史身份与当前执行权不能混为一谈。
# 函数用途: 合法后继已结算可迁移，在途或当前身份缺证据仍拒绝。
def test_settled_writer_history_survives_later_attempt(tmp_path, monkeypatch, current_state):
    case = stopped_home(tmp_path, monkeypatch)
    target, ids, _operation_id = _writer_history(case)
    before = target.read_bytes()
    current = case.repo.create_attempt(ids["agent_run_id"])
    assert current["attempt_id"] != ids["attempt_id"]
    if current_state != "running":
        assert case.repo.settle_agent_run(agent_run_id=ids["agent_run_id"], attempt_id=current["attempt_id"],
                                          status="done")["settled"]
        assert case.repo.settle_task_run_if_agent_tree_terminal(
            task_run_id=ids["task_run_id"], task_id=ids["task_id"])["settled"]
        if current_state != "settled":
            with case.repo._runtime_connection() as conn:
                if current_state == "missing-ended":
                    conn.execute("UPDATE agent_attempts SET ended_at=0 WHERE attempt_id=?", (current["attempt_id"],))
                else:
                    conn.execute("UPDATE agent_attempts SET attempt_generation=99 WHERE attempt_id=?",
                                 (current["attempt_id"],))
                conn.commit()
    inventory.prepare_standalone_read_facilities(case.home)
    if current_state != "settled":
        with pytest.raises(StandaloneMigrationRejected, match="processing_auxiliary_unsettled"):
            inventory.standalone_inventory(case.home, case.config, case.paths)
        return
    assert inventory.standalone_inventory(case.home, case.config, case.paths)
    case.configure(0)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    assert target.read_bytes() == before
    case.args, case.config = case.configure(2)
    with coordinator_guard(case.paths.root, 2):
        assert not _prepare(case)._all_quarantined
