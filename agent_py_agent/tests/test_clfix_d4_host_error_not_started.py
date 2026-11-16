"""D-4: a handler's HostAPIError(outcome="not_started") keeps the not_started effect instead of unknown."""
from __future__ import annotations

import pytest

from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.tooling.models import BaseTool
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


class _RaisingTool(BaseTool):
    model_spec = make_test_model_spec("host_call", description="Raises a host interface error.")
    runtime_policy = make_test_runtime_policy("mutating")

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def execute(self, params):
        raise self.error


@pytest.mark.parametrize(("error", "expected"), [
    (HostAPIError("BUSINESS_SPACE_NOT_FOUND", 404, "not_started"), "not_started"),
    (HostAPIError("HOST_UNAVAILABLE", 503, "unknown"), ""),
    (RuntimeError("boom"), ""),
])
def test_handler_exception_effect_outcome(tmp_path, error, expected):
    execution = execute_canonical_test_call(
        tmp_path, tools={"host_call": _RaisingTool(error)}, tool_name="host_call", arguments={},
    )
    outcome = execution.result if hasattr(execution, "result") else execution
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_RUNTIME_ERROR"
    assert (outcome.effect_outcome or "") in ({expected} if expected else {"", "unknown"})
