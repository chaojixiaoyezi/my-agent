"""版本记录与当前实例ready纯合同；原实际子进程出生节点保留在固定来源，另待授权验收。"""
import copy
import json
import os
import selectors
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import (
    WorkerReadyError,
    registered_worker_heartbeat,
    require_current_worker,
)
from agent_py_agent.agent.gateway_parts.multiprocess_executor_identity import process_birth
from agent_py_agent.agent.gateway_parts.multiprocess_records import (
    MultiprocessRecordError,
    deserialize_record,
    serialize_record,
    validate_record,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition


def identity(pid=102, birth="101:000001"):
    return {"schema_version": "multiprocess_process_identity.v1", "host_id": "host-one", "pid": pid, "start_time": birth}


def registry():
    return {"schema_version": "multiprocess_worker_registry.v1", "c_run_id": "c-one", "worker_count": 2,
            "process_identity": identity(101), "http_port": 43123,
            "workers": [{"worker": 0, "instance_id": "w0-one", "process_identity": identity(),
                         "executor_ledger_ref": "workers/executors/w0"}]}


def heartbeat():
    return {"schema_version": "multiprocess_worker_heartbeat.v1", "worker": 0, "of": 2,
            "pid": 102, "process_identity": identity(), "c_run_id": "c-one", "instance_id": "w0-one",
            "ready": True, "loop_health": "healthy", "inflight": 0, "updated_at": 100.0,
            "readiness": {"ready": True, "gateway_ports": [43123]}}


@pytest.mark.parametrize("factory", [registry, heartbeat])
def test_new_wire_records_round_trip_without_changing_input(factory):
    source = factory()
    before = copy.deepcopy(source)
    try:
        actual = deserialize_record(serialize_record(source))
    except MultiprocessRecordError as error:
        actual = error.to_payload()
    assert actual == source
    assert source == before


def test_current_instance_is_accepted_and_result_is_independent():
    record = registry()
    actual = require_current_worker(record, heartbeat(), WorkerPartition(0, 2, "c-one"), 105.0)
    assert actual == record["workers"][0]
    actual["process_identity"]["pid"] = 999
    assert record["workers"][0]["process_identity"]["pid"] == 102


@pytest.mark.parametrize("field,value,reason", [
    ("instance_id", "w0-old", "worker_instance_mismatch"),
    ("c_run_id", "c-old", "worker_generation_mismatch"),
    ("worker", 1, "worker_not_registered"),
    ("of", 3, "worker_count_mismatch"),
    ("updated_at", 89.0, "worker_unhealthy"),
    ("updated_at", 110.0, "worker_unhealthy"),
    ("ready", False, "worker_unhealthy"),
    ("loop_health", "not_ready", "worker_unhealthy"),
])
def test_late_or_unhealthy_heartbeat_is_rejected(field, value, reason):
    candidate = heartbeat()
    candidate[field] = value
    with pytest.raises(WorkerReadyError) as error:
        require_current_worker(registry(), candidate, WorkerPartition(0, 2, "c-one"), 105.0)
    assert error.value.reason == reason


@pytest.mark.parametrize("field,value", [("pid", 103), ("start_time", "102:000001"), ("host_id", "host-two")])
def test_same_slot_and_instance_label_do_not_hide_different_process(field, value):
    candidate = heartbeat()
    candidate["process_identity"][field] = value
    if field == "pid":
        candidate["pid"] = value
    with pytest.raises(WorkerReadyError, match="worker_process_mismatch"):
        require_current_worker(registry(), candidate, WorkerPartition(0, 2, "c-one"), 105.0)


def test_registry_itself_must_belong_to_current_coordinator():
    with pytest.raises(WorkerReadyError, match="registry_generation_mismatch"):
        require_current_worker(registry(), heartbeat(), WorkerPartition(0, 2, "c-new"), 105.0)


@pytest.mark.parametrize("readiness", [{"ready": True, "gateway_ports": []},
                                      {"ready": True, "gateway_ports": [43124]},
                                      {"ready": True, "gateway_ports": [True]}, {"ready": False}])
def test_ready_requires_actual_coordinator_port_readback(readiness):
    candidate = heartbeat()
    candidate["readiness"] = readiness
    with pytest.raises(WorkerReadyError):
        require_current_worker(registry(), candidate, WorkerPartition(0, 2, "c-one"), 105.0)


# LLM: 原六类坏身份逐项保留，提前返回使每条夹具仅有一层判断。
# 函数用途: 构造身份含混的登记，测试仍通过原 P0 入口验证拒绝。
def ambiguous_registry(change):
    record = registry()
    if change == "duplicate_worker":
        record["workers"].append(copy.deepcopy(record["workers"][0]))
        return record
    if change == "duplicate_instance":
        extra = copy.deepcopy(record["workers"][0])
        extra.update(worker=1, executor_ledger_ref="workers/executors/w1")
        record["workers"].append(extra)
        return record
    if change == "bad_ledger":
        record["workers"][0]["executor_ledger_ref"] = "../other"
        return record
    if change == "pid_only":
        record["workers"][0]["process_identity"]["start_time"] = None
        return record
    if change == "port_bool":
        record["http_port"] = True
        return record
    record["argv"] = ["not-allowed"]
    return record


@pytest.mark.parametrize("change", ["duplicate_worker", "duplicate_instance", "bad_ledger", "pid_only", "port_bool", "unknown"])
def test_registry_rejects_ambiguous_or_incomplete_identity(change):
    record = ambiguous_registry(change)
    with pytest.raises(MultiprocessRecordError):
        validate_record(record)


@pytest.mark.parametrize("field,value", [("worker", True), ("of", True), ("pid", True), ("inflight", -1),
                                         ("updated_at", float("nan")), ("ready", 1)])
def test_malformed_heartbeat_fails_as_structured_error(field, value):
    candidate = heartbeat()
    candidate[field] = value
    with pytest.raises(WorkerReadyError) as error:
        require_current_worker(registry(), candidate, WorkerPartition(0, 2, "c-one"), 105.0)
    assert error.value.error_code == "WORKER_READY_REJECTED"
