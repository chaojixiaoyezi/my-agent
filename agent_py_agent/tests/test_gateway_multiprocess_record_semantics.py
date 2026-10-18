"""B1纯结构：保原bootstrap/domain节点断言，不导ready、ledger或启动执行者。"""
import copy

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_records import (
    MultiprocessRecordError,
    advance_executor,
    deserialize_record,
    serialize_record,
    validate_record,
)
from agent_py_agent.tests.test_gateway_multiprocess_records import (
    executor,
    identity,
)
from agent_py_agent.tests.test_gateway_multiprocess_records import (
    isolated_home as isolated_home,
)


def process_identity(pid=102, birth="101:000001"):
    return {"schema_version": "multiprocess_process_identity.v1", "host_id": "host-one", "pid": pid, "start_time": birth}



def registry():
    return {"schema_version": "multiprocess_worker_registry.v1", "c_run_id": "c-one", "worker_count": 2,
            "process_identity": process_identity(101), "http_port": 43123,
            "workers": [{"worker": 0, "instance_id": "w0-one", "process_identity": process_identity(),
                         "executor_ledger_ref": "workers/executors/w0"}]}



def heartbeat():
    return {"schema_version": "multiprocess_worker_heartbeat.v1", "worker": 0, "of": 2,
            "pid": 102, "process_identity": process_identity(), "c_run_id": "c-one", "instance_id": "w0-one",
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
def test_malformed_heartbeat_codec_is_structured_before_ready_admission(field, value):
    candidate = heartbeat()
    candidate[field] = value
    with pytest.raises(MultiprocessRecordError) as error:
        validate_record(candidate)
    assert error.value.error_code == 'MULTIPROCESS_RECORD_INVALID'



def domain_sample(role='W'):
    init = identity()
    creator = {**init, 'pid': 100, 'start_time': 400}
    domain = {'schema_version': 'multiprocess_execution_domain.v1', 'platform': 'linux', 'method': 'pid_namespace',
              'namespace': {'device': 4, 'inode': 42}, 'parent_namespace': {'device': 4, 'inode': 12},
              'init_identity': init, 'creator_identity': creator, 'host_parent_pid': creator['pid'],
              'init_namespace_pid': 1, 'source_role': role, 'source_worker_index': 0}
    row = {**executor(), 'schema_version': 'multiprocess_executor_reservation.v2', 'execution_domain': None}
    return row, {'process_identity': init, 'pgid': init['pid'], 'sid': init['pid'], 'execution_domain': domain}



@pytest.mark.parametrize('field,value', [
    ('namespace', {'device': True, 'inode': 42}), ('init_namespace_pid', True),
    ('init_namespace_pid', 2), ('host_parent_pid', True), ('source_role', 'H'),
    ('source_role', []), ('source_worker_index', True), ('source_worker_index', -1),
])
def test_v2_rejects_nonfacts_or_role_aliases(field, value):
    row, registration = domain_sample()
    registration['execution_domain'][field] = value
    with pytest.raises(MultiprocessRecordError):
        advance_executor(row, 'registered', registration)



@pytest.mark.parametrize('corrupt', ['parent_namespace', 'init_identity', 'creator_identity', 'missing'])
def test_v2_requires_independent_actual_init_and_creator_bindings(corrupt):
    row, registration = domain_sample()
    domain = registration['execution_domain']
    if corrupt == 'parent_namespace':
        domain['parent_namespace'] = copy.deepcopy(domain['namespace'])
    elif corrupt == 'init_identity':
        domain['init_identity'] = {**domain['init_identity'], 'start_time': 999}
    elif corrupt == 'creator_identity':
        domain['creator_identity'] = {**domain['creator_identity'], 'pid': 999}
    else:
        del domain['creator_identity']
    with pytest.raises(MultiprocessRecordError):
        advance_executor(row, 'registered', registration)



def test_v1_bytes_and_live_v1_records_never_gain_namespace_evidence():
    before = executor()
    row, registration = domain_sample()
    with pytest.raises(MultiprocessRecordError):
        advance_executor(before, 'registered', registration)
    v1_registration = {key: value for key, value in registration.items() if key != 'execution_domain'}
    live = advance_executor(before, 'registered', v1_registration)
    assert live['schema_version'] == before['schema_version'] and 'execution_domain' not in live
    assert deserialize_record(serialize_record(live)) == live
    with pytest.raises(MultiprocessRecordError):
        validate_record({**before, 'execution_domain': None})
    with pytest.raises(MultiprocessRecordError):
        validate_record({**row, 'execution_domain': registration['execution_domain']})
