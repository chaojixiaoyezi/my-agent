"""真实来源类型的导入闭包与原对象同一性；不把导入成功计作执行或 ready。"""
from importlib import import_module
from pathlib import Path

import pytest

import agent_py_agent


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    target = (tmp_path / "explicit-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    assert target != (Path.home() / ".my-agent").resolve()
    yield
    assert not target.exists()


@pytest.mark.parametrize("module_name", [
    "multiprocess_takeover_runtime",
    "multiprocess_coordinator_executor_source",
    "multiprocess_native_coordinator_host",
    "multiprocess_worker_commit",
    "multiprocess_stop",
])
def test_real_coordinator_source_types_import_from_current_tree(module_name):
    module = import_module("agent_py_agent.agent.gateway_parts." + module_name)
    assert Path(module.__file__).resolve().is_relative_to(Path(agent_py_agent.__file__).resolve().parent)


def test_runtime_stop_reuses_unique_schema_functions():
    from agent_py_agent.agent.gateway_parts import multiprocess_stop as runtime
    from agent_py_agent.agent.gateway_parts import multiprocess_stop_schema as schema

    assert runtime.validate_cancellation_record is schema.validate_cancellation_record
    assert runtime.require_stop_time is schema.require_stop_time


def test_request_unknown_error_is_the_same_common_type_not_active_turn_alias():
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts import request_errors

    assert request_errors.RequestExecutionOutcomeUncertainError is RequestExecutionOutcomeUncertainError
    assert request_errors.ActiveTurnOutcomeUncertainError is not RequestExecutionOutcomeUncertainError
    request = {"id": "fixture-request", "execution_attempt_id": "fixture-attempt",
               "lease_epoch": 3, "lease_owner": "fixture-owner"}
    error = RequestExecutionOutcomeUncertainError(request, "fixture.short_operation")
    assert error.operation_entered is False
    assert error.error_code == "ACTIVE_TURN_OUTCOME_UNCERTAIN"
    assert error.request_id == "fixture-request" and error.execution_attempt_id == "fixture-attempt"
    assert error.lease_epoch == 3 and error.lease_owner == "fixture-owner"
