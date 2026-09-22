"""同一进程账的任务/激活隔离与旧协议恢复，不启动业务进程。"""

from __future__ import annotations

import json
from dataclasses import asdict, replace

import pytest

from agent_py_agent.agent.tooling import process_registry as registry_module
from agent_py_agent.agent.tooling import process_session_cleanup as cleanup_module
from agent_py_agent.agent.tooling import process_session_commit as commit
from agent_py_agent.agent.tooling.process_registry import ProcessRegistry
from agent_py_agent.agent.tooling.process_scope import (
    ProcessAccessScope,
    ProcessActivationScope,
    ProcessExecutionScope,
)
from agent_py_agent.agent.tooling.process_session_commit import ProcessSessionCommitPendingError
from agent_py_agent.agent.tooling.process_session_records import (
    TASK_PROCESS_SESSION_SCHEMA,
    validate_process_record,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
from agent_py_agent.tests.test_process_session_store import SCOPE, _reservation, _running

ACTIVATION = ProcessActivationScope("owner-a", SCOPE.owner_home, "workspace-peek", "a" * 64)


# LLM: 构造纯存储夹具，PID 不得用于系统信号；共享激活归属必须清空全部业务身份。
# 函数用途: 建立独立插件代次的严格 v3 记录，验证筛选和持久更新不会串到任务。
def shared_record(session_id="bg-plugin", *, scope=ACTIVATION, status="starting"):
    base = _reservation if status == "starting" else _running
    result = base(session_id, activation_scope=asdict(scope),
                  access_scope=asdict(ProcessAccessScope(scope.owner_id, "", scope.owner_home)),
                  execution_scope=asdict(ProcessExecutionScope(owner_home=scope.owner_home)))
    if status != "starting":
        result.update(status=status, finished_at=3.0 if status in {"exited", "killed"} else None)
    return result


# LLM: 显式使用旧 v2 原字段，不把新字段写进旧协议；调用方只能保留原版本更新。
# 函数用途: 为跨版本恢复测试构造旧任务预留。
def previous_record(session_id="bg-previous"):
    value = _reservation(session_id, schema=TASK_PROCESS_SESSION_SCHEMA)
    value.pop("activation_scope")
    value.pop("retain_until_consumed")
    return value


def test_task_stop_and_activation_stop_select_disjoint_scopes_and_generations(tmp_path):
    store = ProcessSessionStore(tmp_path)
    variants = [ACTIVATION, replace(ACTIVATION, activation_id="b" * 64),
                replace(ACTIVATION, plugin_id="another"), replace(ACTIVATION, owner_id="other-owner")]
    for index, scope in enumerate(variants):
        store.write(shared_record(f"bg-plugin-{index}", scope=scope))
    store.write(_reservation())
    store.write(previous_record())
    receipt = store.request_stop(SCOPE)
    assert {r["session_id"] for r in receipt.records} == {"bg-a", "bg-previous"}
    selected = store.request_stop_activation(ACTIVATION)
    assert [r["session_id"] for r in selected.records] == ["bg-plugin-0"]
    for index in range(1, 4):
        assert not store.load(f"bg-plugin-{index}").record["stop_requested"]


@pytest.mark.parametrize("field,value", [
    ("owner_id", ""), ("owner_home", "relative"), ("plugin_id", "../other"),
    ("activation_id", ""), ("activation_id", "b" * 63), ("activation_id", True),
])
def test_scope_requires_full_exact_identity(field, value):
    with pytest.raises(ValueError):
        replace(ACTIVATION, **{field: value})


@pytest.mark.parametrize("section,field,value", [
    ("access_scope", "conversation_id", "thread-a"),
    ("access_scope", "owner_id", "wrong-owner"),
    ("access_scope", "owner_home", "/owners/wrong"),
    ("execution_scope", "thread_id", "thread-a"),
    ("execution_scope", "root_task_id", "task-a"),
    ("execution_scope", "run_id", "run-a"),
    ("execution_scope", "attempt_id", "attempt-a"),
    ("activation_scope", "extra", "untrusted"),
])
def test_shared_record_refuses_business_and_conflicting_identity(section, field, value):
    row = shared_record()
    row[section][field] = value
    with pytest.raises(ValueError):
        validate_process_record(row)


def test_new_schema_requires_scope_and_old_schema_cannot_smuggle_it():
    value = _reservation()
    value.pop("activation_scope")
    value.pop("retain_until_consumed")
    with pytest.raises(ValueError, match="scope required"):
        validate_process_record(value)
    value["schema"] = TASK_PROCESS_SESSION_SCHEMA
    assert "activation_scope" not in validate_process_record(value)
    value["activation_scope"] = None
    with pytest.raises(ValueError, match="v2 managed process"):
        validate_process_record(value)


def test_existing_session_cannot_change_activation_or_return_to_task(tmp_path):
    store = ProcessSessionStore(tmp_path)
    current = store.write(shared_record())
    for changes in ({"activation_scope": asdict(replace(ACTIVATION, activation_id="b" * 64))},
                    {"activation_scope": None}):
        with pytest.raises(ValueError, match="authority conflict"):
            store.write({**current, **changes})
    before = store.record_path(current["session_id"]).read_bytes()
    queried = store.load(current["session_id"]).record
    queried["activation_scope"]["activation_id"] = "c" * 64
    assert store.record_path(current["session_id"]).read_bytes() == before


def test_prune_retains_activation_terminal_evidence_and_stop_can_revisit_it(tmp_path):
    store = ProcessSessionStore(tmp_path)
    store.write(shared_record(status="exited"))
    store.write(_reservation(status="not_started", finished_at=3))
    store.prune_finished(0)
    assert not store.load("bg-a").record
    retained = store.load("bg-plugin").record
    assert retained["status"] == "exited"
    frozen = store.request_stop_activation(ACTIVATION).records
    assert len(frozen) == 1 and frozen[0]["stop_requested"] and frozen[0]["status"] == "exited"


def test_old_v2_redo_recovers_without_schema_migration(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path)
    original = commit.write_json_file_atomic_unlocked

    def fail_install(path, value):
        if path.name == "bg-previous.json":
            raise OSError("interrupted after old redo commit")
        return original(path, value)

    monkeypatch.setattr(commit, "write_json_file_atomic_unlocked", fail_install)
    with pytest.raises(ProcessSessionCommitPendingError):
        store.write(previous_record())
    monkeypatch.setattr(commit, "write_json_file_atomic_unlocked", original)
    record = store.load("bg-previous").record
    assert record["schema"] == TASK_PROCESS_SESSION_SCHEMA and "activation_scope" not in record
    before = store.record_path(record["session_id"]).read_bytes()
    assert store.load(record["session_id"]).record == record
    assert store.record_path(record["session_id"]).read_bytes() == before
    updated = store.write({**record, "stop_requested": True})
    assert updated["schema"] == TASK_PROCESS_SESSION_SCHEMA and updated["revision"] == record["revision"] + 1
    changed = {**updated, "schema": _reservation()["schema"], "activation_scope": None, "retain_until_consumed": False}
    with pytest.raises(ValueError, match="authority conflict"):
        store.write(changed)


@pytest.mark.parametrize("previous", [False, True])
def test_both_managed_versions_keep_unknown_host_loss_and_exact_cleanup_path(tmp_path, monkeypatch, previous):
    store = ProcessSessionStore(tmp_path)
    row = store.write(previous_record() if previous else _reservation())
    registry = ProcessRegistry()
    monkeypatch.setattr(registry_module, "_process_instance_terminated", lambda *_: True)
    state = registry.status(row["session_id"], store_root=store.root)
    assert state["status"] == "unknown" and state["handoff_confirmed"] is False
    calls = []

    def cleanup(_store, selected, **_kwargs):
        calls.append(selected)
        return cleanup_module.ProcessSessionCleanup(selected, False)

    monkeypatch.setattr(cleanup_module, "stop_process_session", cleanup)
    monkeypatch.setattr(registry, "_kill_legacy", lambda *_: pytest.fail("must not use v1 cleanup"))
    outcome = registry.kill(row["session_id"], store_root=store.root)
    assert len(calls) == 1 and calls[0]["schema"] == row["schema"]
    assert outcome["status"] == "unknown" and outcome["termination"]["confirmed"] is False


@pytest.mark.parametrize("action", ["status", "wait", "network_status", "stop"])
def test_model_process_tool_cannot_access_shared_connection_even_with_exact_handle(tmp_path, action):
    scope = replace(ACTIVATION, owner_home=str(tmp_path))
    store = ProcessSessionStore(process_session_store_root(tmp_path, tmp_path))
    row = store.write(shared_record(scope=scope))
    tool = ProcessSessionTool(tmp_path, tmp_path)
    result = tool.execute({"action": action, "session_id": row["session_id"],
                           "__run_scope": {"owner_id": scope.owner_id, "owner_home": scope.owner_home,
                                           "session_id": "thread-a"}})
    assert result.error_code == "PROCESS_NOT_FOUND"
    assert not store.load(row["session_id"]).record["stop_requested"]
    registry = ProcessRegistry()
    assert registry.list(ProcessAccessScope(scope.owner_id, "", scope.owner_home), store.root) == []


def test_bad_record_prevents_partial_activation_stop(tmp_path):
    store = ProcessSessionStore(tmp_path)
    record = store.write(shared_record())
    store.record_path("bg-damaged").write_text(json.dumps({"schema": "unknown"}))
    with pytest.raises(ValueError, match="complete stop selection"):
        store.request_stop_activation(ACTIVATION)
    assert store.load(record["session_id"]).record == record


def test_missing_scope_query_does_not_create_directory(tmp_path):
    store = ProcessSessionStore(tmp_path / "absent")
    assert not store.request_stop_activation(ACTIVATION).records
    assert not store.root.exists()
