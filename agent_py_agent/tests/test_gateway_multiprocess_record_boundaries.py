"""B1全版本纯闭包与取消负例；所有数据均合成，不取得运行或停止授权。"""
import builtins
import copy
from dataclasses import asdict

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_records as records
from agent_py_agent.agent.gateway_parts.multiprocess_route import (
    RouteStamp,
    freeze_request_route,
    owner_worker,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_multiprocess_record_semantics import (
    domain_sample,
    heartbeat,
    registry,
)
from agent_py_agent.tests.test_gateway_multiprocess_records import (
    admission,
    proof,
    samples,
)
from agent_py_agent.tests.test_gateway_multiprocess_records import (
    isolated_home as isolated_home,
)
from agent_py_agent.tests.test_gateway_multiprocess_session_key import expected_scope


def cancellation():
    owner = OwnerIdentity.provider_user('synthetic', 'alice')
    return {'schema_version': 'multiprocess_cancellation.v1', 'request_id': 'req-1',
            'execution_attempt_id': 'attempt-1', 'lease_epoch': 1, 'lease_owner': 'worker-1',
            'worker_index': owner_worker(owner, 2), 'worker_count': 2, 'c_run_id': 'c-1',
            'owner': [owner.provider, owner.owner_kind, owner.owner_id],
            'route_binding': 'a' * 64, 'cancel_requested_at': 1.0}


def all_versions():
    executor_v2, _ = domain_sample()
    admission_v2 = {**admission(), 'schema_version': 'multiprocess_admission_reservation.v2',
                    'session_scope': expected_scope()}
    proof_v2 = {**proof(), 'schema_version': 'multiprocess_takeover_proof.v2'}
    proof_v2['covered_scope'] = {**proof_v2['covered_scope'], 'role': 'W', 'c_run_id': 'c-1'}
    generation_v2 = {'schema_version': 'multiprocess_c_generation.v2', 'c_run_id': 'c-1',
                     'worker_count': 1, 'closure': None}
    return [*samples(), executor_v2, admission_v2, proof_v2, registry(), heartbeat(), cancellation(), generation_v2]


def test_all_eleven_versions_close_without_runtime_imports_or_files(tmp_path, monkeypatch):
    original_import = builtins.__import__
    forbidden = {'multiprocess_stop', 'multiprocess_dispatch', 'multiprocess_dispatch_binding',
                 'multiprocess_admission', 'multiprocess_bootstrap', 'multiprocess_mode',
                 'multiprocess_coordinator', 'multiprocess_executor_ledger'}

    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        assert name.rsplit('.', 1)[-1] not in forbidden
        assert not forbidden.intersection(fromlist or ())
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, '__import__', pure_import)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob('*'))
    values = all_versions()
    assert {row['schema_version'] for row in values} == set(records.RECORD_V1_FIELDS) | set(records.RECORD_V2_FIELDS)
    for value in values:
        snapshot = copy.deepcopy(value)
        assert records.deserialize_record(records.serialize_record(value)) == snapshot
        assert records.validate_record(value) == snapshot
        assert value == snapshot
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob('*')) == before


def test_new_host_owner_route_is_valid_without_rewriting_legacy_wire_golden():
    from agent_py_agent.agent.gateway_parts.io import (
        GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
        gateway_request_fingerprint,
    )

    owner = OwnerIdentity.provider_user('local', 'synthetic-alice')
    request = {'id': 'new-req-1', 'prompt': '合成新认证请求', REQUEST_OWNER_KEY: asdict(owner),
               'metadata': {'channel': 'local', 'user_id': 'synthetic-alice',
                            'gateway_admin_binding_version': 'binding-1'}}
    request['request_fingerprint'] = gateway_request_fingerprint(request, request['id'])
    request['request_fingerprint_schema'] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    frozen = freeze_request_route(request, owner, RouteStamp(2, 'c-1', 1, 'binding-1'))
    reservation = {**admission(), 'request_id': 'new-req-1',
                   'owner_sha': frozen['route']['owner_sha'], 'route': frozen['route']}
    assert records.deserialize_record(records.serialize_record(reservation)) == reservation
    assert frozen[REQUEST_OWNER_KEY] == request[REQUEST_OWNER_KEY]
    assert 'gateway_route_owner' not in frozen['metadata']
    assert records.record_digest(admission()) == '0c0d95a6d2308b260603c856ed839a10e90defa30a8bcf44c786ae398c4be215'


@pytest.mark.parametrize('value', [True, False, None, -1, float('inf'), float('nan'), [], {}, 10 ** 1000],
                         ids=['true', 'false', 'none', 'negative', 'infinity', 'nan', 'list', 'dict', 'oversized_integer'])
def test_cancel_observation_time_is_structured_rejection(value):
    with pytest.raises(records.MultiprocessRecordError) as error:
        records.validate_record({**cancellation(), 'cancel_requested_at': value})
    assert error.value.reason == 'invalid_stop_time'
    assert error.value.field == 'observed_at'


@pytest.mark.parametrize('field,value', [('lease_epoch', 0), ('lease_epoch', True), ('worker_count', 0),
    ('worker_index', True), ('worker_index', 99), ('route_binding', 'a'), ('owner', ['local', 'main', 'main']),
    ('request_id', '../other'), ('execution_attempt_id', ''), ('lease_owner', '')])
def test_cancellation_preserves_strict_identity_partition_and_shape(field, value):
    with pytest.raises(records.MultiprocessRecordError):
        records.serialize_record({**cancellation(), field: value})


def test_cancel_missing_or_unknown_fields_never_gain_default_authority():
    for field in cancellation():
        row = cancellation()
        del row[field]
        with pytest.raises(records.MultiprocessRecordError):
            records.validate_record(row)
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record({**cancellation(), 'confirmed': True})


@pytest.mark.parametrize('role', ['C', 'W'])
def test_pure_v2_fsm_preserves_full_domain_without_ledger_or_death_claim(role):
    row, registration = domain_sample(role)
    registered = records.advance_executor(row, 'registered', registration)
    released = records.advance_executor(registered, 'released')
    abandoned = records.advance_executor(registered, 'abandoned')
    assert released['execution_domain'] == abandoned['execution_domain'] == registration['execution_domain']
    assert records.deserialize_record(records.serialize_record(released)) == released
    assert row['state'] == 'reserved' and row['execution_domain'] is None


@pytest.mark.parametrize('field', ['init_identity', 'creator_identity'])
def test_v2_domain_cannot_use_another_valid_record_as_process_identity(field):
    row, registration = domain_sample()
    registration['execution_domain'][field] = {
        'schema_version': 'multiprocess_c_generation.v1', 'c_run_id': 'c-1', 'worker_count': 2,
    }
    with pytest.raises(records.MultiprocessRecordError):
        records.advance_executor(row, 'registered', registration)
