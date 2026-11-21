from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.background import dispatch, log_retention
from agent_py_agent.agent.conversation.runtime_reconciliation import (
    canonical_gateway_terminal_reason,
)
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.io import GatewayJsonReadReport, fcntl
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
    generation_metadata_parts,
)
from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.constants_catalog import load_catalog
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

DAY = 86400
NOW = 20 * DAY


def _receipt(paths, name, *, state="completed", updated_at=DAY):
    owner = OwnerIdentity("local", "user", "main")
    scope = GatewayControlScope("main", "local", "chat", {"message_id": name}, resolved_owner=owner)
    operation, digest = ops.gateway_control_operation_identity(scope, "/stop")
    receipt = ops._new_control_operation_receipt(scope, "stop", "/stop", operation, digest)
    receipt = replace(receipt, state=state, updated_at=updated_at,
                      result={"kind": "stop", "ok": True, "message": "done"} if state == "completed" else {})
    path = ops.gateway_control_operation_path(paths, operation)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_dict()), encoding="utf-8")
    return path


@pytest.mark.skipif(fcntl is None, reason="需要原文件事务和目录句柄")
def test_old_terminal_receipt_is_retired(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    old = _receipt(paths, "old")
    recent = _receipt(paths, "recent", updated_at=NOW - 7 * DAY)
    pending = _receipt(paths, "pending", state="executing")
    result = ops.prune_terminal_control_operations(paths, now=NOW, retention_days=7, max_ms=500)
    assert result == dict(examined=3, removed=1, kept_nonterminal=1, kept_referenced=0, budget_exhausted=False)
    assert not old.exists() and recent.exists() and pending.exists()


@pytest.mark.skipif(fcntl is None, reason="需要原文件事务和目录句柄")
def test_referenced_receipt_is_retained(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    old = _receipt(paths, "referenced")
    registry = paths.root / "workers" / "registry.json"
    registry.parent.mkdir()
    registry.write_text("{}", encoding="utf-8")
    result = ops.prune_terminal_control_operations(paths, now=NOW, retention_days=7, max_ms=500)
    assert old.exists() and result["kept_referenced"] == 1


def test_dispatch_log_uses_utc_launch_date(tmp_path):
    start = datetime(2026, 11, 19, 23, 59, tzinfo=timezone.utc)
    launch_id = f"subagent-start-{int(start.timestamp()) * 1_000_000_000}"
    agent = SimpleNamespace(subagents=SimpleNamespace(workspace=tmp_path))
    assert dispatch._background_log_path(agent, launch_id) == str(
        tmp_path / "background_dispatch" / "2026-11-19" / f"{launch_id}.log"
    )


@pytest.mark.skipif(fcntl is None, reason="需要原文件事务和目录句柄")
def test_closed_generation_keeps_only_its_receipt_refs(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    protected = _receipt(paths, "protected")
    unrelated = _receipt(paths, "unrelated")
    identity = {"schema_version": "multiprocess_process_identity.v1", "host_id": "fixture",
                "pid": 999999990, "start_time": "fixture-birth"}
    registry = {"schema_version": "multiprocess_worker_registry.v1", "c_run_id": "old-c",
                "worker_count": 1, "process_identity": identity, "http_port": 1, "workers": [
                    {"worker": 0, "instance_id": "old-instance",
                     "process_identity": {**identity, "pid": 999999991},
                     "executor_ledger_ref": "workers/executors/w0"}]}
    closure = {"registry": registry, "registry_sha256": hashlib.sha256(serialize_record(registry)).hexdigest(),
               "role_markers": {"gateway_state.json": "a" * 64, "workers/w0.json": "b" * 64},
               "source_files": {f"control_operations/{protected.name}": hashlib.sha256(protected.read_bytes()).hexdigest()},
               "tree_evidence": {}}
    metadata = paths.root.joinpath(*generation_metadata_parts("old-c"))
    metadata.parent.mkdir(parents=True)
    (metadata.parent.parent / "registry.json").write_bytes(serialize_record(registry))
    metadata.write_bytes(serialize_record({"schema_version": "multiprocess_c_generation.v2",
                                           "c_run_id": "old-c", "worker_count": 1, "closure": closure}))
    result = ops.prune_terminal_control_operations(paths, now=NOW, retention_days=7, max_ms=500)
    assert result["kept_referenced"] == result["removed"] == 1
    assert protected.exists() and not unrelated.exists()


@pytest.mark.skipif(fcntl is None, reason="需要原协调者文件租约")
def test_inflight_coordinator_blocks_retirement(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    old = _receipt(paths, "inflight")
    with coordinator_guard(paths.root, 1):
        result = ops.prune_terminal_control_operations(paths, now=NOW, retention_days=7)
    assert old.is_file() and result["removed"] == 0


@pytest.mark.skipif(fcntl is None, reason="需要原文件事务和目录句柄")
def test_entry_budget_resumes_and_preserves_sidecars(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    entries = [_receipt(paths, "pending", state="prepared")]
    entries.extend(_receipt(paths, f"expired-{idx}") for idx in range(3))
    for path in entries[1:]:
        path.with_suffix(".json.lock").touch(mode=0o600)
    removed = 0
    for _ in range(15):
        result = ops.prune_terminal_control_operations(paths, now=NOW, retention_days=7, max_entries=1, max_ms=500)
        assert result["examined"] <= 1
        removed += result["removed"]
        if not result["budget_exhausted"]:
            break
    assert removed == 3 and entries[0].exists()
    assert all(path.with_suffix(".json.lock").exists() for path in entries[1:])


def test_zero_retention_preserves_receipts_without_lock_io(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    old = _receipt(paths, "keep-forever")
    result = ops.prune_terminal_control_operations(paths, now=NOW, retention_days=0)
    assert result["examined"] == result["removed"] == 0
    assert old.exists() and not list(paths.root.glob("*.lock"))


@pytest.mark.skipif(fcntl is None, reason="需要原文件事务和目录句柄")
def test_time_budget_rechecks_before_unlink(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path.resolve())
    old = _receipt(paths, "expiry")
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(ops, "time", SimpleNamespace(monotonic=lambda: clock.value))
    original_read = ops._control_retention_raw

    def read(*args):
        payload = original_read(*args)
        clock.value = 1.0
        return payload

    monkeypatch.setattr(ops, "_control_retention_raw", read)
    result = ops.prune_terminal_control_operations(paths, now=NOW, retention_days=7, max_ms=25)
    assert result["budget_exhausted"] and result["examined"] == 1 and result["removed"] == 0
    assert old.exists()


@pytest.mark.skipif(fcntl is None, reason="需要原日志句柄租约")
def test_mixed_flat_and_day_shards_retire_in_bounded_slices(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    owner.mkdir()
    repo = RuntimeRepository(owner / "runtime.db")
    with closing(sqlite3.connect(repo.db_path)) as conn:
        conn.execute("INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES ('t','task','done',1,1)")
        conn.execute("INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,created_at,updated_at) VALUES ('a','t','run','done',1,1)")
        conn.execute("INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) VALUES ('attempt','a',1,'done',1,2)")
        conn.commit()
    folder = tmp_path / "background_dispatch"
    for item in ("2026-10-02", "", "2026-10-01"):
        shard = folder / item
        shard.mkdir(parents=True, exist_ok=True)
        log = shard / "launch.log"
        log.write_bytes(b"stdout")
        info = log.stat()
        log_retention.record_dispatch_log_exit(log, returncode=0, run_ids=["run"], owner_home=owner,
                                               attempt_ids={"run": "attempt"}, expected_identity=(info.st_dev, info.st_ino))
    retired = []
    original = log_retention._retire_exit_marker

    def observe(path, *args):
        if original(path, *args):
            retired.append(path)
            return True
        return False

    monkeypatch.setattr(log_retention, "_retire_exit_marker", observe)
    for _ in range(20):
        result = log_retention.retire_dispatch_logs_slice(folder, now=time.time() + 31 * DAY, max_entries=2, max_ms=500)
        assert result["scanned"] <= 2
        if result["complete"]:
            break
    assert retired == [folder, folder / "2026-10-01", folder / "2026-10-02"]
    assert not list(folder.iterdir())



def _settled_owner(root):
    owner = root / "owner"
    owner.mkdir()
    repo = RuntimeRepository(owner / "runtime.db")
    with closing(sqlite3.connect(repo.db_path)) as conn:
        conn.execute("INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES ('t','task','done',1,1)")
        conn.execute("INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,created_at,updated_at) VALUES ('a','t','run','done',1,1)")
        conn.execute("INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) VALUES ('attempt','a',1,'done',1,2)")
        conn.commit()
    return owner


def _rotating_roots(tmp_path, count=33, days=129):
    owner = _settled_owner(tmp_path)
    roots, subjects = [], []
    for index in range(count):
        folder = tmp_path / f"workspace-{index}" / "background_dispatch"
        names = [time.strftime("%Y-%m-%d", time.gmtime((20000 + day) * DAY)) for day in range(days)]
        for name in names:
            (folder / name).mkdir(parents=True)
        log = folder / names[-1] / "launch.log"
        log.write_bytes(b"stdout")
        info = log.stat()
        log_retention.record_dispatch_log_exit(log, returncode=0, run_ids=["run"], owner_home=owner,
                                               attempt_ids={"run": "attempt"}, expected_identity=(info.st_dev, info.st_ino))
        roots.append(folder)
        subjects.append(log)
    return roots, subjects


@pytest.mark.skipif(fcntl is None, reason="需要原日志句柄租约")
def test_rotating_workspaces_resume_scan_position_beyond_cache(tmp_path):
    # 续点只在目录内进度记录中，进程内无名单缓存：33个轮转工作区与进程重启同一路径。
    # 墙钟放宽，每片只受 128 条目预算约束：每个工作区恰需三片（129 个日名两页，再逐日换日与退役），
    # 结果与机器快慢无关；慢机墙钟截断由 slow_host 用例以受控时钟覆盖。
    roots, subjects = _rotating_roots(tmp_path)
    later = time.time() + 31 * DAY
    deleted = completed = 0
    for _round in range(3):
        for folder in roots:
            result = log_retention.retire_dispatch_logs_slice(folder, now=later, max_ms=60_000)
            assert result["scanned"] <= 128
            deleted += result["deleted"]
            completed += result["complete"]
    assert deleted == len(roots) and completed >= len(roots)
    assert not any(log.exists() for log in subjects)
    for folder in roots:
        # 完整一轮后旧空日目录与进度文件都已退役，下一轮从头开始。
        assert not list(folder.iterdir())


@pytest.mark.skipif(fcntl is None, reason="需要原日志句柄租约")
def test_resumed_cursor_skips_processed_markers_and_resets_on_identity_change(tmp_path, monkeypatch):
    owner = _settled_owner(tmp_path)
    folder = tmp_path / "background_dispatch"
    shard = folder / "2026-10-01"
    _expired_markers(owner, shard, 4, "resume")
    seen = []
    original = log_retention._retire_exit_marker

    def observe(path, descriptor, marker, *args):
        seen.append(marker)
        return original(path, descriptor, marker, *args)

    monkeypatch.setattr(log_retention, "_retire_exit_marker", observe)
    # 回执未过 30 天窗口：游标推进但不退役；每片从落盘续点继续，同一轮内每个回执只处理一次。
    for _ in range(16):
        result = log_retention.retire_dispatch_logs_slice(folder, max_entries=2)
        if result["complete"]:
            break
        progress = json.loads((folder / log_retention._PROGRESS_NAME).read_text())
        assert progress["schema"] == log_retention._PROGRESS_SCHEMA and progress["phase"] in ("flat", "days")
    assert result["complete"] and len(seen) == 4
    assert sorted(seen) == [f"resume-{index:03d}.exit.json" for index in range(4)]
    seen.clear()
    log_retention.retire_dispatch_logs_slice(folder, max_entries=2)
    # 进度与目录身份不符时（如目录被替换）从头扫，而不是沿旧游标跳过回执。
    progress = json.loads((folder / log_retention._PROGRESS_NAME).read_text())
    progress["identity"] = [0, 0]
    (folder / log_retention._PROGRESS_NAME).write_text(json.dumps(progress))
    result = log_retention.retire_dispatch_logs_slice(folder, now=time.time() + 31 * DAY, max_entries=16)
    assert result["complete"] and result["deleted"] == 4
    assert sorted(seen) == [f"resume-{index:03d}.exit.json" for index in range(4)]
    assert not folder.joinpath(log_retention._PROGRESS_NAME).exists() and not shard.exists()


def _expired_markers(owner, directory, count, prefix):
    directory.mkdir(parents=True, exist_ok=True)
    logs = []
    for index in range(count):
        log = directory / f"{prefix}-{index:03d}.log"
        log.write_bytes(b"stdout")
        info = log.stat()
        log_retention.record_dispatch_log_exit(log, returncode=0, run_ids=["run"], owner_home=owner,
                                               attempt_ids={"run": "attempt"}, expected_identity=(info.st_dev, info.st_ino))
        logs.append(log)
    return logs


class _SlowHostClock:
    """每次读单调时钟都比上次晚30毫秒：任何一次目录枚举都耗尽整片墙钟预算（超宽目录/慢主机的确定性模型）。"""

    def __init__(self):
        self.value = time.monotonic()

    def monotonic(self):
        self.value += 0.03
        return self.value


@pytest.mark.skipif(fcntl is None, reason="需要原日志句柄租约")
def test_wide_flat_and_day_directories_retire_across_rotating_workspaces_on_a_slow_host(tmp_path, monkeypatch):
    owner = _settled_owner(tmp_path)
    loaded = AgentConfig()
    budget = {"max_entries": loaded.retention_scan_slice_entries, "max_ms": loaded.retention_scan_slice_ms}
    wide = tmp_path / "workspace-wide" / "background_dispatch"
    # 旧平铺层与一个旧日目录的目录项都多于一片能列出的条目数（每个回执一对 .log/.exit.json）。
    count = budget["max_entries"] // 2 + 1
    subjects = _expired_markers(owner, wide, count, "flat") + _expired_markers(owner, wide / "2026-01-01", count, "day")
    roots = [wide]
    for index in range(32):
        folder = tmp_path / f"workspace-{index}" / "background_dispatch"
        subjects += _expired_markers(owner, folder / "2026-01-02", 1, "small")
        roots.append(folder)
    clock = _SlowHostClock()
    monkeypatch.setattr(log_retention, "time", SimpleNamespace(
        monotonic=clock.monotonic, time=time.time, strftime=time.strftime, gmtime=time.gmtime))
    later = time.time() + 31 * DAY
    progress = wide / log_retention._PROGRESS_NAME
    for _round in range(4 * count + 16):
        for folder in roots:
            before = progress.read_bytes() if folder == wide and progress.exists() else None
            result = log_retention.retire_dispatch_logs_slice(folder, now=later, **budget)
            assert result["scanned"] <= budget["max_entries"]
            if folder == wide and not result["complete"]:
                # 即使本片没有退役任何回执，枚举续点也已推进并落盘。
                after = progress.read_bytes() if progress.exists() else None
                assert after is not None and after != before
        if not any(log.exists() for log in subjects):
            break
    assert not any(log.exists() or log.with_suffix(".exit.json").exists() for log in subjects)
    for _ in range(4):
        if log_retention.retire_dispatch_logs_slice(wide, now=later, **budget)["complete"]:
            break
    assert not (wide / "2026-01-01").exists() and not progress.exists()


def test_both_dispatch_callers_pass_the_loaded_retention_budget(tmp_path, monkeypatch):
    loaded = AgentConfig(retention_scan_slice_entries=7, retention_scan_slice_ms=11)
    agent = SimpleNamespace(root=tmp_path / "owner", config=loaded, subagents=SimpleNamespace(workspace=tmp_path),
                            _background_subagent_dispatches={})
    request = dispatch._BackgroundDispatchRequest(agent, ["run"], "budget-launch", None, None,
                                                  SimpleNamespace(expected_attempt_ids={"run": "attempt"}))
    calls = []

    class _StopBeforeSpawn(Exception):
        pass

    def capture(folder, **options):
        calls.append(options)
        if len(calls) == 1:
            raise _StopBeforeSpawn
        return {"scanned": 0, "deleted": 0, "complete": True}

    monkeypatch.setattr(log_retention, "retire_dispatch_logs_slice", capture)
    monkeypatch.setattr(log_retention, "record_dispatch_log_exit", lambda *_args, **_kwargs: None)
    with pytest.raises(_StopBeforeSpawn):
        with dispatch._BackgroundProcessWaitOwner(agent, request) as owner:
            dispatch._spawn_background_dispatch_process(agent, request, wait_owner=owner)
    owner.thread.join(timeout=5)
    process = SimpleNamespace(wait=lambda: 0, returncode=0, _my_agent_background_log_identity=(1, 2),
                              _my_agent_background_log_path=str(tmp_path / "background_dispatch" / "day" / "x.log"),
                              _my_agent_bounded_capture_expected=False)
    dispatch._reap_then_settle_unactivated_exit(agent, request, process, (("run", "attempt"),))
    assert calls == [{"max_entries": 7, "max_ms": 11}] * 2

def test_deployment_retention_key(tmp_path):
    key = "gateway_control_operation_retention_days"
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(f"{key}: 3\n", encoding="utf-8")
    assert getattr(load_config(config_path), key) == 3
    config_field = next(item for item in fields(AgentConfig) if item.name == key)
    assert config_field.default == 7 and config_field.metadata["bounds"] == (0, None)
    assert AgentConfig(**{key: 0}).gateway_control_operation_retention_days == 0
    for invalid in (-1, True):
        with pytest.raises(ValueError, match=key):
            AgentConfig(**{key: invalid})
    spec = parameter_registry()[key]
    assert spec.tier == "deployment" and not spec.writable and not spec.user_editable
    assert any(entry["name"] == "_GATEWAY_CONTROL_RECEIPT_RETENTION_DAYS" and entry["value"] == 7
               for entry in load_catalog())


@dataclass(frozen=True, slots=True)
class _PlainReport:
    payload: dict
    load_error: dict | None = None
    lock_busy: bool = False


def test_slotted_report_matches_plain_and_real_consumer(tmp_path):
    assert "__dict__" not in GatewayJsonReadReport.__dict__
    for values in (({},), ({"status": "done"}, None), ({}, {"code": "busy"}, True)):
        assert asdict(GatewayJsonReadReport(*values)) == asdict(_PlainReport(*values))
    path = tmp_path / "root-1.json"
    path.write_text(json.dumps({"schema_version": "gateway_terminal_request.v1", "id": "root-1", "status": "done"}))
    assert canonical_gateway_terminal_reason(SimpleNamespace(terminal=tmp_path), "root-1") == "parent_turn_ended"


def g1_case_gateway_json_read_report():
    payload = {"status": "done"}
    return lambda: GatewayJsonReadReport(payload)
