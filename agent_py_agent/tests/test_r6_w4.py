"""隔离 owner 的真实追加、迁移、故障恢复与 no-follow 回归，不启动模型或 live 服务。"""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.observability import task_call_inspector as inspector_module
from agent_py_agent.agent.observability import task_rounds as storage
from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger, _encode
from agent_py_agent.tests.test_telemetry_ledger_and_inspector import (
    _end_model_call,
    _model_request,
    call,
    call_runtime,
)


@pytest.fixture
def task(tmp_path):
    inspector = inspector_module.TaskCallInspector(tmp_path, owner_id="one")
    inspector.upsert_task_call(dict(task_id="t", started_at=1))
    return inspector


def _row(identity):
    return dict(round_id=identity, run_id="run", prompt_tokens=1, completion_tokens=2)


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_append_io_is_independent_of_round_history(task, monkeypatch, count):
    task.upsert_task_call(dict(task_id="t", rounds=[_row(str(i)) for i in range(count)]))
    counters = dict(read=0, written=0)
    read, pread, write = os.read, os.pread, os.write

    def counted_read(*args):
        data = read(*args)
        counters["read"] += len(data)
        return data

    def counted_pread(*args):
        data = pread(*args)
        counters["read"] += len(data)
        return data

    def counted_write(fd, data):
        size = write(fd, data)
        counters["written"] += size
        return size

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", counted_read)
        patch.setattr(os, "pread", counted_pread)
        patch.setattr(os, "write", counted_write)
        patch.setattr(storage, "read_task_rounds", lambda *a: pytest.fail("append hydrated history"))
        patch.setattr(storage, "OwnerReadIndex", lambda *a: pytest.fail("append opened read projection"))
        assert task.append_round("t", _row("last"), include_rounds=False) is None
    assert counters["read"] < 4096 and counters["written"] < 4096, counters
    metadata = json.loads((task._home / "data/telemetry/task_calls/t.json").read_text())
    assert metadata["rounds"] == [] and metadata["token_usage"]["total_tokens"] == (count + 1) * 3
    detail = task.get_task_call_detail("t")
    assert len(detail["rounds"]) == count + 1 and detail["rounds_coverage"]["status"] == "complete"


def test_full_detail_follows_real_prefix_verification_progress(task, monkeypatch):
    from agent_py_agent.agent.local_storage import owner_read_index as index_module

    task.upsert_task_call(dict(task_id="t", rounds=[_row(str(i)) for i in range(100)]))
    task.append_round("t", _row("delta"), include_rounds=False)
    original = index_module._Budget

    def small_budget(*args, **kwargs):
        return original(max_bytes=1024)

    monkeypatch.setattr(index_module, "_Budget", small_budget)
    detail = task.get_task_call_detail("t")
    assert detail["rounds_coverage"]["status"] == "complete", detail["rounds_coverage"]
    assert len(detail["rounds"]) == 101


def test_metadata_failure_is_partial_and_replay_is_exact(task, monkeypatch):
    task.append_round("t", _row("seed"), include_rounds=False)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("single metadata commit interruption")
        patch.setattr(storage, "_write_metadata", fail)
        with pytest.raises(OSError):
            task.append_round("t", _row("pending"), include_rounds=False)
    detail = task.get_task_call_detail("t")
    assert detail["rounds_coverage"]["status"] == "partial" and len(detail["rounds"]) == 2
    assert detail["token_usage"]["total_tokens"] == 6
    task.append_round("t", _row("pending"), include_rounds=False)
    detail = task.get_task_call_detail("t")
    assert len(detail["rounds"]) == 2 and detail["rounds_coverage"]["status"] == "complete"
    assert detail["token_usage"]["total_tokens"] == 6


def test_first_metadata_commit_failure_is_visible_and_retryable(tmp_path, monkeypatch):
    task = inspector_module.TaskCallInspector(tmp_path, owner_id="one")
    payload = dict(task_id="t", rounds=[_row("old")])
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("single first metadata interruption")
        patch.setattr(inspector_module, "write_text_atomic_beneath", fail)
        with pytest.raises(OSError):
            task.upsert_task_call(payload)
    detail = task.get_task_call_detail("t")
    assert detail["rounds_coverage"]["reason"] == "task_metadata_missing" and detail["status"] is None
    task.upsert_task_call(payload)
    assert [row["round_id"] for row in storage.read_task_rounds(tmp_path, "t")] == ["old"]
    assert task.get_task_call_detail("t")["rounds_coverage"]["status"] == "complete"


@pytest.mark.parametrize("complete", [False, True])
def test_one_torn_append_recovers_without_duplicate(task, complete):
    task.append_round("t", _row("seed"), include_rounds=False)
    path = task._home.joinpath(*storage._parts("t"))
    generation, _ = storage._snapshot(task._home, "t")
    tail = (_encode(storage._frame(generation, 2, _row("pending"))).encode().rstrip(b"\n")
            if complete else b'{"row":"cut\xe4')
    with path.open("ab") as stream:
        stream.write(tail)
    task.append_round("t", _row("pending"), include_rounds=False)
    rows = storage.read_task_rounds(task._home, "t")
    assert [row["round_id"] for row in rows] == ["seed", "pending"]
    sidecars = list(path.parent.glob(path.name + ".torn-*"))
    assert len(sidecars) == (0 if complete else 1)
    if sidecars:
        assert sidecars[0].read_bytes() == tail and sidecars[0].stat().st_mode & 0o777 == 0o600


def test_interrupted_legacy_migration_reuses_generation(task, monkeypatch):
    legacy = inspector_module.TaskCallRecord("t", owner_id="one", rounds=[_row("old")]).to_dict()
    metadata = task._home / "data/telemetry/task_calls/t.json"
    metadata.write_text(_encode(legacy))
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("single migration marker interruption")
        patch.setattr(inspector_module, "write_text_atomic_beneath", fail)
        with pytest.raises(OSError):
            task.append_round("t", _row("new"), include_rounds=False)
    generation, before = storage._snapshot(task._home, "t")
    assert task.get_task_call_detail("t")["rounds_coverage"]["status"] == "partial"
    task.append_round("t", _row("new"), include_rounds=False)
    for _ in range(2):
        task.upsert_task_call(dict(task_id="t", title="metadata only"))
    assert storage._snapshot(task._home, "t")[0] == generation
    assert storage._snapshot(task._home, "t")[1][:2] == before[:2]
    assert [row["round_id"] for row in storage.read_task_rounds(task._home, "t")] == ["old", "new"]


def test_public_roundtrip_redaction_scope_and_terminal_state(task):
    storage.append_task_round(task._home, "t", {**_row("direct"), "note": "password=dummy-private"})
    result = task.append_round("t", _row("default"))
    assert [row["round_id"] for row in result["rounds"]] == ["direct", "default"]
    assert len(result["runs"]) == 1
    task.complete_task_call("t", "cancelled", ended_at=2)
    task.append_round("t", _row("after"), include_rounds=False)
    assert task.get_task_call_detail("t")["status"] == "cancelled"
    assert "dummy-private" not in task._home.joinpath(*storage._parts("t")).read_text()
    with pytest.raises(ValueError, match="owner mismatch"):
        inspector_module.TaskCallInspector(task._home, owner_id="other").append_round("t", _row("wrong"))


def test_round_source_symlink_is_rejected(task, tmp_path):
    task.append_round("t", _row("seed"), include_rounds=False)
    path = task._home.joinpath(*storage._parts("t"))
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(OSError):
        task.append_round("t", _row("rejected"), include_rounds=False)
    with pytest.raises(OSError):
        storage.read_task_rounds(task._home, "t")
    assert outside.read_text() == "unchanged"


def test_public_append_reuses_owner_lock_for_concurrent_writers(task):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda identity: storage.append_task_round(task._home, "t", _row(str(identity))), range(12)))
    detail = task.get_task_call_detail("t")
    assert len({row["round_id"] for row in detail["rounds"]}) == 12
    assert detail["token_usage"]["total_tokens"] == 36


@pytest.mark.parametrize("change", [dict(prompt_tokens=-1), dict(duration_ms=float("nan")), dict(prompt_tokens=2**63)])
def test_invalid_round_is_rejected_before_metadata_or_migration(task, change):
    metadata = task._home / "data/telemetry/task_calls/t.json"
    before = metadata.read_bytes()
    for task_id in ("t", "new"):
        with pytest.raises(ValueError):
            task.append_round(task_id, change, create_if_missing=True, include_rounds=False)
    assert metadata.read_bytes() == before
    assert not task._home.joinpath(*storage._parts("t")).exists()
    assert not (metadata.parent / "new.json").exists()


def test_model_callback_alone_disables_round_hydration(tmp_path, monkeypatch):
    request, _ = _model_request(tmp_path)
    options = []
    original = inspector_module.TaskCallInspector.append_round

    def observed(self, *args, **kwargs):
        options.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(inspector_module.TaskCallInspector, "append_round", observed)
    ledger, call_id, _ = call_runtime.start_model_call_record(
        request, context_snapshot=SimpleNamespace(current_tokens=17, to_public_dict=lambda: {}),
    )
    _end_model_call(ledger, call_id, "finished")
    assert options == [dict(create_if_missing=True, include_rounds=False)]
    assert not getattr(request.agent.config, "llm_call_records_enabled", False)
    assert len(inspector_module.TaskCallInspector(tmp_path, owner_id="one").get_task_call_detail("model-task")["rounds"]) == 1


def test_first_background_round_never_rebuilds_historical_task_index(task, monkeypatch):
    (task._home / "data/telemetry/task_calls_index.jsonl").unlink()

    def forbid_scan(*args, **kwargs):
        pytest.fail("background creation traversed task history")

    with monkeypatch.context() as patch:
        patch.setattr(inspector_module, "list_names_beneath", forbid_scan)
        patch.setattr(inspector_module.TaskCallInspector, "_write_index", forbid_scan)
        assert task.append_round("new", _row("new"), create_if_missing=True, include_rounds=False) is None
    assert len(task.get_task_call_detail("new")["rounds"]) == 1


def test_short_recovery_read_never_truncates_committed_round(task, monkeypatch):
    task.append_round("t", _row("seed"), include_rounds=False)
    path = task._home.joinpath(*storage._parts("t"))
    generation, _ = storage._snapshot(task._home, "t")
    with path.open("ab") as stream:
        stream.write(_encode(storage._frame(generation, 2, _row("pending"))).encode())
    before, pread = path.read_bytes(), os.pread

    def short_read(fd, count, offset):
        data = pread(fd, count, offset)
        return data[:-1] if offset else data

    with monkeypatch.context() as patch:
        patch.setattr(os, "pread", short_read)
        with pytest.raises(OSError, match="short task round recovery read"):
            task.append_round("t", _row("pending"), include_rounds=False)
    assert path.read_bytes() == before and list(path.parent.glob(path.name + ".torn-*")) == []


def test_preexisting_owner_index_gets_only_missing_domain(task):
    ledger = ToolTelemetryLedger(task._home)
    ledger.record_tool_call(call())
    assert len(ledger.query_tool_calls()) == 1
    with sqlite3.connect(task._home / "cache/owner_read_index.sqlite3") as connection:
        connection.execute("DROP TABLE ori_task_rounds")  # 临时 fixture 还原升级前投影布局。
    task.append_round("t", _row("first"), include_rounds=False)
    assert len(task.get_task_call_detail("t")["rounds"]) == 1
    assert len(ledger.query_tool_calls()) == 1


@pytest.mark.parametrize("quota_bytes", [100 * 1024**2, 1024**3, 100 * 1024**3])
def test_existing_runtime_quota_policy_covers_new_sources(task, quota_bytes):
    from agent_py_agent.agent.user_space import owner_quota as quota

    enforcer = quota.OwnerQuotaEnforcer(task._home, max_bytes=quota_bytes)
    payload = task._home / "payload"
    with enforcer.reserve([quota.OwnerQuotaChange(payload, 1)]):
        payload.write_bytes(b"x")
    task.append_round("t", _row("first"), include_rounds=False)
    assert len(task.get_task_call_detail("t")["rounds"]) == 1
    # 原策略将 telemetry 子树和 owner_read_index 缓存列为宿主运行时；配额范围不随本修复改变。
    assert quota.owner_logical_usage_bytes(task._home, exact=True) == 1
