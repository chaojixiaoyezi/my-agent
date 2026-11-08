"""R6O-W1：全量维护拍续用上拍分页续点；无 work/ 的无状态任务根不再使派生记录清理永久失败。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.local_storage import records
from agent_py_agent.agent.user_space import owner_maintenance as om


def test_partial_store_cursor_only_resumes_partial_pages():
    cursor = {"after": "workspace/runtime/a/local_store/local.db", "pending": []}
    assert om._partial_store_cursor({"k": {"partial": True, "cursor": cursor}}, "k") is cursor
    for value in ({"partial": False, "cursor": cursor}, {"partial": True}, {"partial": True, "cursor": "x"}, None):
        assert om._partial_store_cursor({"k": value}, "k") is None


@pytest.mark.parametrize("transition", [False, True])
def test_full_pass_resumes_both_paged_store_cursors(tmp_path, monkeypatch, transition):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
        home_paths_with_owner,
    )

    base = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(base.root, OwnerIdentity.local_main())
    home = home_paths_with_owner(base, owner)
    monkeypatch.setattr(om, "_retention_deletion_safe", lambda retention: True)
    seen = {}

    def derived_page(owner_home, *, now, commit_transition=None, cursor=None):
        seen["derived"] = cursor
        return {"executed": True, "partial": False}

    def events_page(paths, *, now, commit_transition=None, cursor=None):
        seen["events"] = cursor
        return {"executed": True, "partial": False}

    monkeypatch.setattr(om, "_prune_local_store_derived_records", derived_page)
    monkeypatch.setattr(om, "_maintain_local_store_events", events_page)
    # 多进程协调者沿 commit_transition 走 owner_maintenance_transition 的全量拍，续点规则相同。
    mode = {"include_global_indexes": False, "commit_transition": lambda execute: execute()} if transition else {}
    first = om.run_owner_retention_if_due(home, now=1_000_000.0, **mode)
    assert first.ran
    state_path = Path(home.owner_home_dir) / "data" / "maintenance.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    derived = {"after": "workspace/runtime/a/local_store/local.db", "pending": []}
    events = {"after": "workspace/runtime/b/local_store/local.db", "pending": []}
    state.update(maintenance_partial=True,
                 derived_records_pruned={"executed": True, "partial": True, "cursor": derived},
                 local_store_events_maintenance={"executed": True, "partial": True, "cursor": events})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    seen.clear()
    assert om.run_owner_retention_if_due(home, now=1_000_000.0 + 10 * 86400, **mode).ran
    assert seen == {"derived": derived, "events": events}


def _pins(root):
    return SimpleNamespace(ids=set(), paths={root})


def test_stateless_canonical_task_root_contributes_no_ids(tmp_path):
    owner = tmp_path.resolve()
    root = owner / "tasks" / "2026-11-07" / "red-alert-web"
    (root / "src").mkdir(parents=True)
    assert records._derived_owner_pin_ids(SimpleNamespace(owner_home_dir=owner), _pins(root)) == set()


def test_task_root_with_work_but_missing_state_still_refuses(tmp_path):
    owner = tmp_path.resolve()
    root = owner / "tasks" / "2026-11-07" / "half-written"
    (root / "work").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        records._derived_owner_pin_ids(SimpleNamespace(owner_home_dir=owner), _pins(root))
