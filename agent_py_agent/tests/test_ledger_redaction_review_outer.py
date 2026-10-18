# ruff: noqa: F811
"""只测普通临时 SQLite 与受控真实异常对象，不运行原生平台/模型探针。"""
import pytest

from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable
from agent_py_agent.tests.test_ledger_redaction_coordinator import SECRET, rows, store_with_source


@pytest.mark.parametrize('error_type', [LocalControlCoordinatorUnavailable, BoundedCallExitUnconfirmedError])
def test_sql_writes_then_actual_outer_keeps_object_and_rolls_back(tmp_path, monkeypatch, error_type):
    store, door = store_with_source(tmp_path)
    prior = [rows(store, t) for t in ['runtime_gate_ledger','tool_operations']]
    error = (error_type('controlled-call', 'controlled_exit') if error_type is BoundedCallExitUnconfirmedError else error_type('controlled original outer'))
    cause = OSError('controlled cause')
    context = ValueError('controlled context')
    error.__cause__, error.__context__ = cause, context
    transaction = store._coordinator_transaction
    def execute_then_fail(operation, **kwargs):
        def fail(conn):
            assert door.inside
            operation(conn)
            raise error
        return transaction(fail, **kwargs)
    monkeypatch.setattr(store, '_coordinator_transaction', execute_then_fail)
    with global_write_commit_scope(door), pytest.raises(error_type) as caught:
        store.redact_tool_ledger_content(tool='remember', contents={SECRET})
    assert caught.value is error
    assert error.__cause__ is cause and error.__context__ is context
    assert [rows(store,t) for t in ['runtime_gate_ledger','tool_operations']] == prior
