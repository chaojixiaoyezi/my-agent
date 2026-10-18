# LLM: 复用原producer闭环fixture；仅注入普通文件提交异常，不运行服务/native/P6探针，所有真实格式和来源检查保持。
# 模块用途: 覆盖一次封存raw CAS、写后未知、混代窗口与旧schema合同，运行时用例支持包内.pyc。
import json

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_sources import require_role_coverage
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    closing as closing,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    context as context,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    coordinator_handles as coordinator_handles,
)
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


@pytest.mark.parametrize('failure', ['before_generation', 'after_generation', 'before_marker'])
def test_original_publication_failure_never_returns_sealed_or_opens_old_marker(closing, monkeypatch, failure):
    original = history.write_bytes_atomic_beneath

    def interrupted(root, parts, content):
        generation = parts[:2] == ('workers', 'generations')
        if (generation and failure == 'before_generation') or (not generation and failure == 'before_marker'):
            raise OSError('controlled_publication_not_confirmed')
        result = original(root, parts, content)
        if generation and failure == 'after_generation':
            raise OSError('controlled_generation_written_without_readback')
        return result

    monkeypatch.setattr(history, 'write_bytes_atomic_beneath', interrupted)
    with pytest.raises(OSError):
        closing.seal_generation()
    assert closing._closed_generation_ref is None
    state = json.loads((closing.control.paths.root / 'gateway_state.json').read_bytes())
    assert state.get('generation_closure_ref') is None and state.get('drain_complete') is not True
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    with pytest.raises(ValueError):
        require_role_coverage(closing.control.paths, 'next-c', count=closing.scope.count)


def test_format_only_publisher_cannot_create_or_replace_a_closed_generation(closing):
    closing.seal_generation()
    root, generation = closing.control.paths.root, closing.scope.c_run_id
    record, raw = history.read_generation_metadata(root, generation)
    with pytest.raises(ValueError, match='generation_original_closing_required'):
        history._publish_generation_once(root, record)
    history._publish_generation_once(root, {'schema_version': 'multiprocess_c_generation.v1',
                                    'c_run_id': generation, 'worker_count': closing.scope.count})
    assert history.read_generation_metadata(root, generation) == (record, raw)


def test_successor_keeps_original_closed_sources_after_current_c_marker_is_published(closing, monkeypatch):
    closing.seal_generation()
    paths = closing.control.paths
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    captured = require_role_coverage(paths, 'next-c', count=closing.scope.count)
    import os
    write_json_file_atomic(paths.state, {'status': 'starting', 'pid': os.getpid(), 'c_run_id': 'next-c'})
    assert require_role_coverage(paths, 'next-c', count=closing.scope.count, captured=captured) == captured
    old = paths.root.joinpath(*history.generation_metadata_parts(closing.scope.c_run_id))
    old.write_bytes(old.read_bytes() + b' ')
    with pytest.raises(ValueError):
        require_role_coverage(paths, 'next-c', count=closing.scope.count, captured=captured)


def test_legacy_count_golden_and_v2_initial_record_are_distinct():
    value = {'schema_version': 'multiprocess_c_generation.v1', 'c_run_id': 'old-c', 'worker_count': 1}
    assert serialize_record(value) == b'{"c_run_id":"old-c","schema_version":"multiprocess_c_generation.v1","worker_count":1}'
    candidate = {**value, 'schema_version': 'multiprocess_c_generation.v2', 'closure': None}
    assert serialize_record(candidate) != serialize_record(value)


@pytest.mark.parametrize('late', ['registry', 'worker', 'source', 'observer', 'lease'])
def test_cas_last_gate_rechecks_original_sources_after_token_capture(closing, monkeypatch, late):
    original = history.seal_generation

    def changed(runtime, token):
        if late == 'registry':
            path = runtime.control.paths.root / 'workers/registry.json'
            path.write_bytes(path.read_bytes() + b' ')
        elif late == 'worker':
            path = runtime.control.paths.root / 'workers/w0.json'
            path.write_bytes(path.read_bytes() + b' ')
        elif late == 'source':
            write_json_file_atomic(runtime.control.paths.root / 'admission/reservations/late.json',
                                   {'route': {'c_run_id': runtime.scope.c_run_id}})
        elif late == 'observer':
            runtime._workers._created_origins[0][2]._unknown = True
        else:
            runtime._lease_origin._closing = True
        return original(runtime, token)

    monkeypatch.setattr(history, 'seal_generation', changed)
    expected = history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id)
    with pytest.raises((ValueError, RuntimeError)):
        closing.seal_generation()
    assert history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id) == expected
    if late == 'lease':
        closing._lease_origin._closing = False


@pytest.mark.parametrize('live', [True, None])
def test_history_never_replaces_current_exact_physical_role_observation(closing, monkeypatch, live):
    closing.seal_generation()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: live)
    with pytest.raises(ValueError, match='old_role_lifecycle_unproven'):
        require_role_coverage(closing.control.paths, 'next-c', count=closing.scope.count)


def test_reserved_p1_without_original_domain_cannot_be_sealed_as_empty(closing):
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_digest
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    owner = owner_digest(OwnerIdentity.provider_user('synthetic', 'pending-owner'))
    ledger = ExecutorLedger(closing.control.paths.root,
                            WorkerPartition(int(owner, 16) % closing.scope.count, closing.scope.count, closing.scope.c_run_id))
    ledger.reserve({'schema_version': 'multiprocess_executor_reservation.v1', 'reservation_id': 'unstarted-original-domain',
        'kind': 'shell', 'c_run_id': closing.scope.c_run_id, 'owner_sha': owner, 'request_id': 'request-one',
        'execution_attempt_id': 'attempt-one', 'lease_epoch': 0, 'tool_call_id': 'tool-one', 'state': 'reserved',
        'process_identity': None, 'pgid': None, 'sid': None})
    expected = history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id)
    with pytest.raises(ValueError, match='generation_original_drain_unconfirmed'):
        closing.seal_generation()
    assert history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id) == expected
    assert not closing._closed_generation_ref


def test_copied_seal_token_never_reaches_generation_cas(closing, monkeypatch):
    actual = history.seal_generation

    def copied(runtime, token):
        return actual(runtime, dict(token))

    expected = history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id)
    monkeypatch.setattr(history, 'seal_generation', copied)
    with pytest.raises(ValueError, match='generation_original_seal_required'):
        closing.seal_generation()
    assert history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id) == expected


def test_three_p1_digest_entries_fit_old_size_without_copying_executor_records(closing):
    import copy
    import hashlib

    closing.seal_generation()
    value = copy.deepcopy(history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id)[0])
    # 这里只测纯编码尺寸；这些合成摘要不写P1、不发布或用于consumer授权。
    for index in range(3):
        digest = hashlib.sha256(str(index).encode()).hexdigest()
        value['closure']['source_files'][f'workers/executors/w0/{digest}.json'] = digest
        value['closure']['tree_evidence'][f'workers/takeover_evidence/{digest}.json'] = digest
    encoded = serialize_record(value)
    assert len(encoded) < 16384
    assert history._GENERATION_METADATA_MAX_BYTES == 4 * 1024 * 1024


def test_oversize_seal_cannot_publish_or_fall_back_to_empty(closing, monkeypatch):
    encode = history.serialize_record
    expected = history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id)
    monkeypatch.setattr(history, 'serialize_record', lambda value: encode(value) + b' ' * (4 * 1024 * 1024))
    with pytest.raises(ValueError, match='generation_closure_too_large'):
        closing.seal_generation()
    assert history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id) == expected
    assert closing._closed_generation_ref is None


def test_legacy_reader_keeps_original_sixteen_kib_acceptance_limit(tmp_path):
    value = {'schema_version': 'multiprocess_c_generation.v1', 'c_run_id': 'legacy-c', 'worker_count': 1}
    history._publish_generation_once(tmp_path, value)
    path = tmp_path.joinpath(*history.generation_metadata_parts('legacy-c'))
    path.write_bytes(serialize_record(value) + b' ' * 16384)
    with pytest.raises(ValueError):
        history.read_generation_metadata(tmp_path, 'legacy-c')
