# LLM: 原pin/bind与临时C真实，物理peer/P4及负向P2隔离为fixture边界；不改实际服务，未知不授信。
# 模块用途: 验完整lease pin来源，unknown foreign-looking不授信，已登记其它W的真实foreign pin原样保留。
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_admission as ma
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    pin_coordinator_commit,
    retire_coordinator_commit,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import (
    WorkerPartition,
    owner_digest,
    owner_worker,
)
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    bind_current_worker_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_worker_commit import (
    WorkerCommitIssuer,
    WorkerCommitRejected,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_native_first_materialize_refusal import case as case
from agent_py_agent.tests.test_native_first_materialize_refusal import context as context
from agent_py_agent.tests.test_native_first_materialize_refusal import reject


# LLM: 原bind/pin/finish真实；OS观察与负向P2隔离为fixture事实，不造另一pin registry或授执行权。
# 函数用途: 准备原C登记的另一W及其合法其它owner，用于归属正负例。
def registered_foreign(case):
    c = case
    owner = next(
        OwnerIdentity.provider_user("synthetic", f"foreign-{index}")
        for index in range(50)
        if owner_worker(OwnerIdentity.provider_user("synthetic", f"foreign-{index}"), 2)
        != c.issuer.launch.partition.index
    )
    partition = WorkerPartition(owner_worker(owner, 2), 2, c.control.c_run_id)
    launch = replace(c.issuer.launch, partition=partition, instance_id="original-other-worker")
    # Hashable locator只在fixture的原OS边界使用，不声明为真实Popen或native通过。
    process = type("OriginalFakeProcess", (), {"pid": os.getpid()})()
    source = WorkerCommitIssuer.__new__(WorkerCommitIssuer)
    source.control, source.launch, source.process = c.control, launch, process
    source._issuer_origin, source._creator_pid = source, os.getpid()
    source._source_origin = (launch, process)
    source._controller, source._lease = c.issuer._controller, c.issuer._lease
    source._pins, source._executor_records = set(), {}
    source._require_current = lambda: c.runtime.require_original()
    source._require_worker = lambda peer: (
        None if peer == process.pid else pytest.fail("wrong original peer")
    )
    bind_current_worker_source(source._controller, source)
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import _scope_keys

    # 原owner launch会刷新unproven投影；显式负向P2隔离须沿原scope codec保持，不能误入正常写者。
    source._controller._quarantined.update(
        _scope_keys(
            {
                "c_run_id": c.control.c_run_id,
                "role": "W",
                "worker_index": c.issuer.launch.partition.index,
                "owner_shas": [owner_digest(c.owner)],
            }
        )
    )
    return source, owner


@pytest.mark.parametrize("scope", ["foreign_looking_worker", "foreign_looking_private"])
def test_original_lease_pin_without_original_issuer_reference_is_unknown_even_with_foreign_labels(
    case, scope
):
    foreign = OwnerIdentity.provider_user("synthetic", "foreign-other-owner")
    worker = case.issuer.launch
    label = (
        (
            case.control.c_run_id,
            "W",
            str(worker.partition.index),
            worker.instance_id,
            owner_digest(foreign),
        )
        if scope == "foreign_looking_worker"
        else ("private_interaction", owner_digest(foreign), "foreign-pin")
    )
    pin = pin_coordinator_commit(case.issuer._lease, label)
    try:
        assert pin not in case.issuer._pins
        with pytest.raises(WorkerCommitRejected):
            case.operation.apply(
                os.getpid(), lambda pin: pytest.fail("unexpected pin"), rejection_origin=object()
            )
        assert case.binding.claim.path.exists()
        assert not (case.control.paths.terminal / "native-first.json").exists()
        assert (
            ma._read_reservation(ma.reservation_file(case.control.paths, "native-first"))["state"]
            == "occupied"
        )
        assert pin in case.issuer._lease._commit_pins
    finally:
        retire_coordinator_commit(case.issuer._lease, pin)


def test_registered_other_worker_foreign_owner_original_pin_is_preserved_while_first_claim_fails(
    case,
):
    source, owner = registered_foreign(case)
    pin = source.begin(owner, peer_pid=source.process.pid, rejection_origin=object())
    try:
        reject(case)
        assert (case.control.paths.terminal / "native-first.json").exists()
        assert (
            ma._read_reservation(ma.reservation_file(case.control.paths, "native-first"))["state"]
            == "released"
        )
        assert pin in source._pins and pin in source._lease._commit_pins
    finally:
        # 即便正例暴露产品错误并意外产生本fixture pin，仍只用原issuer回收自己的确切对象，保失败断言。
        for unexpected in tuple(case.issuer._pins):
            case.issuer.finish(unexpected)
        source.finish(pin)


def test_current_worker_same_owner_original_pin_prevents_first_terminal(case):
    digest = owner_digest(case.owner)
    case.issuer._controller._unproven_owners.remove(digest)
    try:
        pin = case.issuer.begin(case.owner, peer_pid=os.getpid(), rejection_origin=object())
    finally:
        case.issuer._controller._unproven_owners.add(digest)
    try:
        with pytest.raises(WorkerCommitRejected):
            case.operation.apply(
                os.getpid(), lambda pin: pytest.fail("unexpected pin"), rejection_origin=object()
            )
        assert case.binding.claim.path.exists()
        assert not (case.control.paths.terminal / "native-first.json").exists()
        assert pin in case.issuer._pins and pin in case.issuer._lease._commit_pins
    finally:
        case.issuer.finish(pin)


def test_rebound_registered_worker_origin_cannot_authorize_foreign_pin(case):
    source, owner = registered_foreign(case)
    pin = source.begin(owner, peer_pid=source.process.pid, rejection_origin=object())
    index = source.launch.partition.index
    original = source._controller._current_worker_origins[index]
    source._controller._current_worker_origins[index] = (source, source.launch, object())
    try:
        with pytest.raises((WorkerCommitRejected, ValueError)):
            case.operation.apply(
                os.getpid(), lambda pin: pytest.fail("unexpected pin"), rejection_origin=object()
            )
        assert case.binding.claim.path.exists()
        assert not (case.control.paths.terminal / "native-first.json").exists()
        assert pin in source._pins
    finally:
        source._controller._current_worker_origins[index] = original
        source.finish(pin)
