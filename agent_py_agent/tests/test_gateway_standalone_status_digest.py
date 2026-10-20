import pytest

from agent_py_agent.agent.gateway_parts import standalone_inventory as inventory
from agent_py_agent.agent.gateway_parts import standalone_migration as migration
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.tests.test_gateway_standalone_migration import _prepare, stopped_home


@pytest.mark.parametrize('table', ['agent_runs', 'task_runs'])
def test_unknown_runtime_status_is_not_migration_authority(tmp_path, monkeypatch, table):
    case = stopped_home(tmp_path, monkeypatch)
    with case.repo._runtime_connection() as conn:
        conn.execute('UPDATE '+table+" SET status='unknown'")
        conn.commit()
    inventory.prepare_standalone_read_facilities(case.home)
    before=case.paths.state.read_bytes()
    with coordinator_guard(case.paths.root, 2), pytest.raises(migration.StandaloneMigrationRejected):
        _prepare(case)
    assert case.paths.state.read_bytes()==before


@pytest.mark.parametrize("table", ["agent_runs","task_runs"])
@pytest.mark.parametrize("status", ["","created","done","failed","cancelled"])
def test_original_known_run_states_do_not_become_physical_busy(tmp_path,monkeypatch,table,status):
    from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
    from agent_py_agent.agent.gateway_parts.standalone_stop import record_standalone_stop
    case=stopped_home(tmp_path,monkeypatch)
    case.configure(0)
    with case.repo._runtime_connection() as conn:
        conn.execute("UPDATE "+table+" SET status=?",(status,))
        conn.commit()
    cleanup=__import__("json").loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact=record_standalone_stop(case.context,held,cleanup)
    assert fact["standalone_stop"]["blockers"]==[]
    case.args,case.config=case.configure(2)
    with coordinator_guard(case.paths.root,2):
        controller=_prepare(case)
        assert not controller._all_quarantined


def test_valid_responsibility_row_change_after_stop_cannot_replace_stop_digest(tmp_path,monkeypatch):
    case=stopped_home(tmp_path,monkeypatch)
    with case.repo._runtime_connection() as conn:
        conn.execute("UPDATE task_runs SET status='done'")
        conn.commit()
    inventory.prepare_standalone_read_facilities(case.home)
    before=case.paths.state.read_bytes()
    with coordinator_guard(case.paths.root,2),pytest.raises(migration.StandaloneMigrationRejected,match="stop_responsibility_changed"):
        _prepare(case)
    assert case.paths.state.read_bytes()==before
