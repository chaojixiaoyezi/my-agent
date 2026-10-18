"""适配器原对象替换须由原source报typed unknown，不能在读取数据前变普通AttributeError/TypeError释放原P3。"""

from dataclasses import replace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.gateway_parts import multiprocess_active_input
from agent_py_agent.tests.test_gateway_active_input_worker import real_worker_case
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import worker as worker


@pytest.mark.parametrize(
    "fault", ["execution", "source", "claim", "paths", "missing_paths", "missing_claim"]
)
def test_original_adapter_reference_drift_is_typed_before_any_observation(
    worker, monkeypatch, fault
):
    with real_worker_case(worker) as (source, params, agent, claim, _paths, _receipt, p3):
        before = p3.read_bytes()
        if fault == "execution":
            source.execution = None
        elif fault == "source":
            source.source = object()
        elif fault == "claim":
            source.claim = replace(claim)
        elif fault == "paths":
            source.paths.root = source.paths.root / "foreign"
        elif fault == "missing_claim":
            source.claim = None
        else:
            source.paths = None

        def forbidden(*_args, **_kwargs):
            pytest.fail("drift reached input observation")

        monkeypatch.setattr(multiprocess_active_input, "target_record", forbidden)
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            source.probe(agent, params)
        error = caught.value
        assert (
            error.request_id == claim.request_id
            and error.execution_attempt_id == claim.execution_attempt_id
        )
        assert error.lease_epoch == claim.lease_epoch and error.operation_entered is False
        assert p3.read_bytes() == before and claim.path.exists()
