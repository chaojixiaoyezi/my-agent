"""恢复集合读取只回读当前 id，不得淘汰共享缓存里仍存在的历史 run。"""
from agent_py_agent.tests.test_subagent_listruns_cache import _selected_manager, _selected_task_file


def _indexed_manager(tmp_path, total=20, running=2):
    manager = _selected_manager(tmp_path)
    index = manager.persistence.run_index
    with index.connect() as db:
        for number in range(total):
            run_id = f"run-{number}"
            _selected_task_file(manager, run_id, "RUNNING" if number < running else "DONE")
            index.upsert(manager.load(run_id), connection=db)
    return manager


def _count_loads(monkeypatch, service):
    loads, original = [], service.load

    def observed(run_id):
        loads.append(run_id)
        return original(run_id)

    monkeypatch.setattr(service, "load", observed)
    return loads


def test_recovery_read_keeps_history_cache_warm(tmp_path, monkeypatch):
    manager = _indexed_manager(tmp_path)
    service = manager.persistence
    assert not manager.list_runs_report().load_errors
    warm = set(service._run_cache)
    assert len(warm) == 20
    report = manager.list_recovery_runs_report()
    assert not report.load_errors and {run.id for run in report.runs} == {"run-0", "run-1"}
    assert set(service._run_cache) == warm
    loads = _count_loads(monkeypatch, service)
    assert not manager.list_runs_report().load_errors
    assert loads == [], "恢复读之后全量读应命中缓存，不能重新解析历史"


def test_recovery_index_error_does_not_flush_cache(tmp_path, monkeypatch):
    manager = _indexed_manager(tmp_path)
    service = manager.persistence
    assert not manager.list_runs_report().load_errors
    warm = set(service._run_cache)

    def unavailable(**_kwargs):
        raise OSError("index unavailable")

    monkeypatch.setattr(service.run_index, "recovery_ids", unavailable)
    report = manager.list_recovery_runs_report()
    assert report.load_errors and not report.runs
    assert set(service._run_cache) == warm


def test_deleted_run_is_still_evicted_by_full_listing(tmp_path):
    manager = _indexed_manager(tmp_path, total=4, running=1)
    service = manager.persistence
    assert not manager.list_runs_report().load_errors
    (manager.workspace / "run-3" / "task.json").unlink()
    manager.list_recovery_runs_report()
    assert not manager.list_runs_report().load_errors
    assert "run-3" not in service._run_cache
