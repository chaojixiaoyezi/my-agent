"""验证路径级保留错误只隔离对应路径，未知错误仍关闭删除闸门。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store.retention import _POLICY_LEVEL_ERROR_CODES
from agent_py_agent.agent.memory_store.retention_models import (
    MemoryRetentionError,
    MemoryRetentionReport,
)
from agent_py_agent.agent.user_space import owner_maintenance as maintenance
from agent_py_agent.agent.user_space import owner_maintenance_transition, storage_lanes
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


@pytest.fixture
def path_error(tmp_path):
    return MemoryRetentionError(
        "MEMORY_RETENTION_SECURITY_AUDIT_UNREADABLE", str(tmp_path / "a.jsonl"), "x"
    )


@pytest.mark.parametrize("serialized", [False, True])
def test_path_scoped_errors_allow_deletion(path_error, serialized):
    error = json.loads(json.dumps(path_error.to_dict())) if serialized else path_error
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[error])

    assert maintenance._retention_deletion_safe(retention) is True


@pytest.mark.parametrize("code", sorted(_POLICY_LEVEL_ERROR_CODES))
@pytest.mark.parametrize("serialized", [False, True])
def test_policy_errors_refuse_deletion(path_error, code, serialized):
    error = MemoryRetentionError(code, path_error.path, "x")
    error = error.to_dict() if serialized else error
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[error])

    assert maintenance._retention_deletion_safe(retention) is False


@pytest.mark.parametrize("serialized", [False, True])
def test_reference_source_error_with_path_refuses_deletion(path_error, serialized):
    error = MemoryRetentionError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE", path_error.path, "x")
    error = error.to_dict() if serialized else error
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[error])

    assert maintenance._retention_deletion_safe(retention) is False


@pytest.mark.parametrize(
    "error",
    [
        "bad",
        None,
        {},
        {"error_code": "", "path": "a.jsonl"},
        {"error_code": 1, "path": "a.jsonl"},
        SimpleNamespace(error_code="", path="a.jsonl"),
        SimpleNamespace(path="a.jsonl"),
    ],
)
def test_unstructured_errors_refuse_deletion(error):
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[error])

    assert maintenance._retention_deletion_safe(retention) is False


@pytest.mark.parametrize("path", [None, ""])
@pytest.mark.parametrize("serialized", [False, True])
def test_missing_error_path_refuses_deletion(path_error, path, serialized):
    error = {"error_code": path_error.error_code}
    if path is not None:
        error["path"] = path
    error = error if serialized else SimpleNamespace(**error)

    assert maintenance._retention_errors_path_scoped([error]) is False


@pytest.mark.parametrize("applied, legal_hold", [(False, False), (True, True), (False, True)])
def test_deletion_requires_applied_without_legal_hold(path_error, applied, legal_hold):
    retention = SimpleNamespace(applied=applied, legal_hold=legal_hold, load_errors=[path_error])

    assert maintenance._retention_deletion_safe(retention) is False


@pytest.mark.parametrize("errors", [None, [], ()])
def test_empty_errors_keep_deletion_available(errors):
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=errors)

    assert maintenance._retention_deletion_safe(retention) is True


def test_one_unsafe_error_closes_the_whole_deletion_gate(path_error):
    assert maintenance._retention_errors_path_scoped([path_error, "bad"]) is False
    assert maintenance._retention_errors_path_scoped(["bad", path_error]) is False


@pytest.mark.parametrize("coordinator", [False, True])
@pytest.mark.parametrize("error_form", ["object", "dict", "unstructured", "policy"])
def test_storage_lanes_only_defer_unsafe_retention(tmp_path, monkeypatch, path_error, coordinator, error_form):
    home = ensure_my_agent_home(tmp_path / "home")
    error = {
        "object": path_error,
        "dict": path_error.to_dict(),
        "unstructured": "bad",
        "policy": MemoryRetentionError(next(iter(_POLICY_LEVEL_ERROR_CODES)), path_error.path, "x"),
    }[error_form]
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[error])
    called = []

    # 只替换领域叶子，真实维护调度器、回执校验和配额对账仍走原入口。
    def lane(request):
        called.append(request)
        return {"cursor": {}, "complete": True, "items": 0, "bytes": 0}

    monkeypatch.setattr(storage_lanes, "resolve_storage_lane", lambda kind: lane)
    transition = (lambda operation: operation()) if coordinator else None
    result = maintenance.run_owner_storage_maintenance_slice(
        home, {}, retention, now=2_000_000, coordinator=coordinator,
        state_locked=not coordinator, commit_transition=transition,
        quota_max_entries=1, quota_max_ms=5000,
    )

    safe = error_form in {"object", "dict"}
    assert {request.kind for request in called} == (set(storage_lanes.LANE_BINDINGS) if safe else set())
    assert all(request.coordinator is coordinator and request.commit is transition for request in called)
    if coordinator:
        assert result["deferred_reason"] == "coordinator_adapter_protocol_required"
        assert ("coordinator_lanes" in result) is safe
    else:
        assert (result.get("deferred_reason") == "retention_not_safe") is not safe
    assert result["deletion_enabled"] is False


@pytest.mark.parametrize("transition", [False, True])
@pytest.mark.parametrize("safe", [False, True])
def test_owner_entrypoints_share_deletion_gate_and_preserve_error_records(
    tmp_path, monkeypatch, path_error, transition, safe
):
    home = ensure_my_agent_home(tmp_path / "home")
    retention = (
        MemoryRetentionReport(applied=True, actions=(), errors=(path_error,), isolated_errors=(path_error,))
        if safe else SimpleNamespace(applied=True, legal_hold=False, actions=(), isolated_errors=(), load_errors=["bad"])
    )
    entry_module = owner_maintenance_transition if transition else maintenance
    monkeypatch.setattr(entry_module, "apply_owner_retention", lambda *args, **kwargs: retention)
    pruned = []

    def prune(owner_home, *, now, commit_transition=None):
        def operation():
            pruned.append(owner_home)
            return {"executed": True}
        return operation() if commit_transition is None else commit_transition(operation)

    monkeypatch.setattr(maintenance, "_prune_local_store_derived_records", prune)
    options = {"commit_transition": lambda operation: operation()} if transition else {}
    result = maintenance.run_owner_retention_if_due(
        home, now=2_000_000, include_global_indexes=False, quota_max_ms=5000, **options
    )
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))

    assert pruned == ([home.owner_home_dir] if safe else [])
    assert marker["derived_records_pruned"]["executed"] is safe
    assert result.status == marker["status"] == "policy_unavailable"
    assert result.apply_outcome == marker["apply_outcome"] == "applied"
    assert marker["last_success_at"] == 0.0
    assert marker["load_errors"] == ([path_error.to_dict()] if safe else ["bad"])
    if not transition:
        assert marker["workflow_maintenance"]["reason"] == (
            "no_workflow_index" if safe else "retention_not_safe"
        )
