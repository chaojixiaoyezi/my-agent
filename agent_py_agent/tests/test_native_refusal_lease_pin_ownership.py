# LLM: 独立评审只调用原pin producer；不改候选或清真实账，原pin在finally由原API结清。
# 模块用途: 验证同租约其它原来源的同owner责任不会被首拒资格漏读。
import os

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_admission as ma
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    pin_coordinator_commit,
    retire_coordinator_commit,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.gateway_parts.multiprocess_worker_commit import WorkerCommitRejected
from agent_py_agent.tests.test_native_first_materialize_refusal import case as case
from agent_py_agent.tests.test_native_first_materialize_refusal import context as context


def test_same_owner_original_lease_pin_outside_worker_issuer_blocks_first_terminal(case):
    c = case
    pin = pin_coordinator_commit(c.issuer._lease, ('private_interaction', owner_digest(c.owner), 'review-original-pin'))
    assert pin in c.issuer._lease._commit_pins and pin not in c.issuer._pins
    try:
        with pytest.raises(WorkerCommitRejected):
            c.operation.apply(os.getpid(), lambda pin: pytest.fail('unexpected pin'), rejection_origin=object())
        selected = c.binding.claim.request_id
        assert not (c.control.paths.terminal / (selected + '.json')).exists(), 'same-owner original lease responsibility was not considered before FAILED'
        assert c.binding.claim.path.exists()
        assert ma._read_reservation(ma.reservation_file(c.control.paths, selected))['state'] == 'occupied'
        assert pin in c.issuer._lease._commit_pins
    finally:
        retire_coordinator_commit(c.issuer._lease, pin)
