"""历史通道按真实登记与维护入口调度，领域模块保持惰性加载。"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space import home_retention, owner_maintenance, storage_lanes
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

_HISTORY_LANES = ("runtime_history", "gateway_history", "gateway_input_refs")
_ORIGINAL_EVICTION_ORDER = (
    "display_archives", "conversation_messages", "compact_checkpoints", "local_events",
    "conversation_ledgers", "tool_outputs", "subagents", "asset_auxiliary",
    "session_task_payloads", "process_sessions",
)
_RUNTIME_COUNTERS = ("slimmed", "deleted_events", "deleted_llm_records")


@pytest.mark.parametrize(("kind", "module", "name"), [
    ("runtime_history", "runtime_db.history_retention", "maintain_runtime_history_lane"),
    ("gateway_history", "gateway_parts.gateway_history_index", "maintain_gateway_history_lane"),
    ("gateway_input_refs", "gateway_parts.input_turn_retention", "maintain_input_turn_refs_lane"),
])
def test_history_lanes_resolve_existing_domain_entrypoints(kind, module, name):
    lane = storage_lanes.resolve_storage_lane(kind)
    assert callable(lane)
    assert lane.__module__ == "agent_py_agent.agent." + module
    assert lane.__name__ == name


def test_storage_lane_import_does_not_load_history_domains():
    script = """
import sys
from agent_py_agent.agent.user_space import storage_lanes
assert 'runtime_history' in storage_lanes.LANE_BINDINGS
assert 'gateway_history' in storage_lanes.LANE_BINDINGS
assert 'agent_py_agent.agent.runtime_db.history_retention' not in sys.modules
assert 'agent_py_agent.agent.gateway_parts.gateway_history_index' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_history_lanes_append_without_reordering_existing_eviction_lanes():
    actual_order = storage_lanes.LANE_EVICTION_ORDER
    assert actual_order == (*_ORIGINAL_EVICTION_ORDER, *_HISTORY_LANES, "usage_sources")


def test_history_registration_reduces_unbound_kinds_through_real_dispatch(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    registry = {kind.kind: kind for kind in home_retention.storage_kind_registry()}
    assert tuple(registry)[-len(_HISTORY_LANES)-1:-1] == _HISTORY_LANES
    assert tuple(registry)[-1] == "usage_sources"
    for name in ("runtime_events", "gateway_logs", *_HISTORY_LANES):
        assert "policy-delete" in registry[name].actions
    assert registry["runtime_events"].policy_ref == "audit_days / tool_output_days_after_terminal after terminal"

    def adapter(kind):
        lane = storage_lanes.resolve_storage_lane(kind)
        assert callable(lane)

        def step(cursor, budget):
            request = storage_lanes.StorageLaneRequest(kind, home, 200 * 86400, cursor,
                storage_lanes.StorageLaneBudget(budget["items"], budget["bytes"], budget["deadline"]),
                False, False, None, None)
            return storage_lanes.validate_lane_receipt(lane(request))
        return step

    adapters = {kind: adapter(kind) for kind in storage_lanes.LANE_BINDINGS}
    previous = owner_maintenance.run_storage_maintenance_slice({},
        {kind: lane for kind, lane in adapters.items() if kind not in _HISTORY_LANES}, max_seconds=0)
    current = owner_maintenance.run_storage_maintenance_slice({}, adapters, max_seconds=0)
    assert not previous["errors"] and not current["errors"]
    assert set(previous["unbound_kinds"]) - set(current["unbound_kinds"]) == set(_HISTORY_LANES)
    assert len(current["unbound_kinds"]) == len(previous["unbound_kinds"]) - len(_HISTORY_LANES)


@pytest.mark.parametrize("value", [0, 3])
def test_runtime_history_receipt_counters_keep_valid_nonnegative_integers(value):
    receipt = {"cursor": {}, "complete": True, "items": 3, "bytes": 0,
               **dict.fromkeys(_RUNTIME_COUNTERS, value)}
    checked = storage_lanes.validate_lane_receipt(receipt)
    assert checked == receipt and checked is not receipt


@pytest.mark.parametrize("field", _RUNTIME_COUNTERS)
@pytest.mark.parametrize("value", [-1, True, False, 1.5, "1", None])
def test_runtime_history_receipt_counters_reject_invalid_types(field, value):
    with pytest.raises(ValueError, match="^invalid_storage_lane_receipt$"):
        storage_lanes.validate_lane_receipt({"cursor": {}, "complete": True, "items": 0, "bytes": 0, field: value})


def test_runtime_history_receipt_still_rejects_unknown_fields():
    with pytest.raises(ValueError, match="^invalid_storage_lane_receipt$"):
        storage_lanes.validate_lane_receipt({"cursor": {}, "complete": True, "items": 0, "bytes": 0,
                                             "slimmed": 0, "unregistered_counter": 0})


def test_owner_maintenance_publishes_both_real_history_lane_receipts(tmp_path):
    from agent_py_agent.agent.gateway_parts.gateway_history_index import GatewayHistoryIndex
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
    from agent_py_agent.tests.test_cat3_runtime_history_retention import _event, _llm, _run, _tool

    home = ensure_my_agent_home(tmp_path / "home")
    repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
    with closing(sqlite3.connect(repo.db_path)) as conn:
        _run(conn)
        _tool(conn)
        _event(conn, "expired-event")
        _llm(conn, "expired-call", owner=home.owner_id)
        conn.commit()
    root = runtime_paths_for_agent(None, home.owner_home_dir, home)["gateway_workspace"]
    index = GatewayHistoryIndex(gateway_paths_from_root(root))
    with index.connection():
        pass

    # 只提供已完成的保留报告；调度、领域策略、数据库写入和回执校验均用真实实现。
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[])
    previous = {"storage_maintenance": {"continuation_pending": True, "pending_kinds": list(_HISTORY_LANES)}}
    state = owner_maintenance.run_owner_storage_maintenance_slice(home, previous, retention,
        now=200 * 86400, quota_max_ms=1000, quota_max_entries=4096)
    assert not state["errors"]
    assert set(state["lane_reports"]) == set(_HISTORY_LANES)
    assert not set(_HISTORY_LANES).intersection(state["lane_fallback_kinds"])
    for kind in _HISTORY_LANES:
        receipt = {"cursor": state["cursors"][kind], **state["lane_reports"][kind]}
        assert storage_lanes.validate_lane_receipt(receipt) == receipt
        assert {"cursor", "complete", "items", "bytes"} <= receipt.keys()
        assert receipt["complete"]
    runtime = state["lane_reports"]["runtime_history"]
    assert runtime["slimmed"] == runtime["deleted_events"] == runtime["deleted_llm_records"] == 1
    assert runtime["bytes"] > 0
