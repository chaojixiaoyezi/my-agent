from __future__ import annotations

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.services import runtime_closeout
from agent_py_agent.tests.test_dispatch_liveness_and_revive import _closeout_fixture


def _event(repo, index, kind="closeout_unpersisted"):
    return repo.append_event(event_type=kind, attempt_id=f"attempt-{index}", agent_run_id=f"run-{index}",
                             payload={"run_id": f"child-{index}", "attempt_id": f"attempt-{index}"})


def _page(repo):
    return repo.pending_events_page("closeout_unpersisted", consumed_event_type="closeout_restored",
                                    consumer="test", limit=50)


def test_pagination_survives_restart_and_failed_front_page(tmp_path):
    path = tmp_path / "runtime.db"
    repo = RuntimeRepository(path)
    for index in range(121):
        _event(repo, index)
    pages = []
    for _ in range(3):
        repo = RuntimeRepository(path)
        pages.append(_page(repo))
    assert [len(page) for page in pages] == [50, 50, 21]
    assert len({row["attempt_id"] for page in pages for row in page}) == 121
    # 没有写成功回执，下一圈必须能重试最老的一页；游标不是消费事实。
    assert _page(repo)[0]["attempt_id"] == "attempt-0"


def test_consumption_has_no_recent_window_and_duplicates_share_identity(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    for index in range(151):
        _event(repo, index)
        _event(repo, index, "closeout_restored")
    _event(repo, 152)
    _event(repo, 152)
    page = _page(repo)
    assert [row["attempt_id"] for row in page] == ["attempt-152"]
    _event(repo, 152, "closeout_restored")
    assert _page(repo) == []


def test_restore_reaches_real_fact_behind_fifty_unreadable_events(tmp_path):
    manager, store, task, attempt, run, params, _ = _closeout_fixture(tmp_path, owner="test/paging")
    repo = manager.runtime_db
    # 页面前部不可恢复的坏记录不能吞掉第 51 个正常事实。
    for index in range(50):
        _event(repo, index)
    original = runtime_closeout._store_closeout
    runtime_closeout._store_closeout = lambda *a: False
    try:
        manager.runner_result.record_runner_result(params)
    finally:
        runtime_closeout._store_closeout = original
    assert runtime_closeout.recover_pending_closeouts(manager)["runtime_closeouts_restored"] == 0
    assert runtime_closeout.recover_pending_closeouts(manager)["runtime_closeouts_restored"] == 1
    assert manager.runtime_db.agent_run_for_run_id(task.id)["status"] == "failed"
    for _ in range(3):
        assert runtime_closeout.recover_pending_closeouts(manager)["runtime_closeouts_restored"] == 0
