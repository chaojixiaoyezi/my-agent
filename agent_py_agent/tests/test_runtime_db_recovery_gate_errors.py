"""运行库永久恢复故障与暂态代次竞争保留不同错误与重试合同。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.io.sqlite_connection import sqlite_error_details
from agent_py_agent.agent.runtime_db.recovery_guard import (
    RuntimeDatabaseDeferred,
    RuntimeDatabaseRecoveryRequired,
)
from agent_py_agent.agent.tooling import executor
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.tests.test_plan_w4_storage_errors import CountingTool, run_tool


def test_recovery_required_is_specific_nonretryable_preexecution(tmp_path, monkeypatch):
    error = RuntimeDatabaseRecoveryRequired('private-user-path /private/test-secret.db token=secret-value')
    calls, delays = [], []
    class Store:
        def require_authority(self, authority):
            calls.append(authority)
            raise error
    monkeypatch.setattr(executor.time, 'sleep', delays.append)
    tool = CountingTool()
    result = run_tool(tmp_path, Store(), tool)
    assert result.error_code == 'RUNTIME_DB_RECOVERY_REQUIRED'
    assert not result.ok and not result.handler_executed and tool.calls == 0
    assert result.effect_outcome == 'not_started'
    assert len(calls) == 1 and delays == []
    assert 'RuntimeDatabaseRecoveryRequired' in result.output
    assert 'SQLITE_CORRUPT(11)' in result.output
    assert '禁止原样盲重试' in result.output
    assert 'test-secret' not in result.output and 'secret-value' not in result.output
    assert result.metadata['handler_details']['storage_error'] == sqlite_error_details(error)
    contract = error_contract(result.error_code)
    assert not contract.retryable and contract.recommended_action == 'manual_review'


def test_generation_busy_preserves_bounded_same_identity_retry(tmp_path, monkeypatch):
    calls, delays = [], []
    class Store:
        def require_authority(self, authority):
            calls.append(authority)
            raise RuntimeDatabaseDeferred('generation_busy')
    monkeypatch.setattr(executor.time, 'sleep', delays.append)
    tool = CountingTool()
    result = run_tool(tmp_path, Store(), tool)
    assert result.error_code == 'RUNTIME_DB_RECOVERY_DEFERRED'
    assert len(calls) == 3 and all(call is calls[0] for call in calls)
    assert delays == [0.060, 0.120]
    assert result.effect_outcome == 'not_started' and not result.handler_executed and tool.calls == 0
    assert 'generation_busy' in result.output
    assert result.metadata['handler_details']['storage_error']['status'] == 'deferred'
    assert error_contract(result.error_code).retryable


def test_postbody_unknown_is_not_relabelled_as_not_started():
    outcome = ToolHandlerOutcome('probe', False, 'operation uncertain', effect_outcome='unknown',
                                 handler_executed=True, error_code='TOOL_OPERATION_OUTCOME_UNKNOWN')
    decision = SimpleNamespace(resolved_effect='write')
    assert executor._effect_outcome(decision, outcome, None) == 'unknown'
    assert executor._outcome_state(outcome) == 'unknown'


@pytest.mark.parametrize('error_type', [RuntimeDatabaseDeferred, RuntimeDatabaseRecoveryRequired])
def test_recovery_contracts_are_distinct_from_generic_store_unavailable(error_type):
    error = error_type('generation_busy')
    assert error_contract(error.code).code == error.code
