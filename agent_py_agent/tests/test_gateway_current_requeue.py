"""当前原执行者让出工作：真实C/W claim、P3、P5和文件CAS，不启动服务/模型。"""
from __future__ import annotations

import json
import time
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch import WorkerDispatchError
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.recovery import requeue_current_processing_request
from agent_py_agent.agent.gateway_parts.request_binding import GatewayActiveTurnTransition
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.support.multiprocess_agent_cases import execution_case


# LLM: 复用已冻结真实W原claim，只有业务Agent为轻量对象；不伪造许可，原端口仍核活源。
# 函数用途: 准备当前让出工作使用的原执行上下文与持久P3。
@pytest.fixture
def current(tmp_path, monkeypatch):
    binding, request = execution_case(tmp_path, monkeypatch)
    root = binding.snapshot.bootstrap.launch.owner_root
    owner = OwnerIdentity.provider_user('synthetic', 'alice')
    agent = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root, owner)))
    paths = gateway_paths_from_root(binding.snapshot.bootstrap.launch.gateway_root)
    return binding, request, agent, paths, None


def test_current_yield_preserves_original_p3_and_identity(current):
    binding, original, agent, paths, transition = current
    p3 = reservation_file(paths, binding.claim.request_id).read_bytes()
    when = time.time() + 60
    with worker_execution_scope(binding, agent):
        transition = GatewayActiveTurnTransition(binding.claim.path, binding.claim.request_id, binding.claim.execution_attempt_id)
        assert requeue_current_processing_request(paths, binding.claim, transition, not_before_at=when) is True
    pending = json.loads((paths.inbox / binding.claim.path.name).read_bytes())
    assert not binding.claim.path.exists()
    assert pending['id'] == original['id']
    assert pending['route'] == original['route']
    assert pending['request_fingerprint'] == original['request_fingerprint']
    assert pending['status'] == 'pending' and pending['not_before_at'] == when
    assert 'execution_attempt_id' not in pending and 'lease_owner' not in pending
    assert reservation_file(paths, binding.claim.request_id).read_bytes() == p3
    assert not (paths.terminal / binding.claim.path.name).exists()


@pytest.mark.parametrize('drift', ['attempt', 'epoch', 'owner', 'pending', 'terminal'])
def test_current_yield_drift_never_changes_original_source(current, drift):
    binding, original, agent, paths, transition = current
    if drift == 'attempt':
        original['execution_attempt_id'] = 'later-attempt'
    elif drift == 'epoch':
        original['lease_epoch'] += 1
    elif drift == 'owner':
        original['lease_owner'] = 'later-lease'
    else:
        target = paths.inbox if drift == 'pending' else paths.terminal
        (target / binding.claim.path.name).write_text('{"id":"another-record"}')
    if drift in {'attempt','epoch','owner'}:
        binding.claim.path.write_text(json.dumps(original))
    before = binding.claim.path.read_bytes()
    p3 = reservation_file(paths, binding.claim.request_id).read_bytes()
    with worker_execution_scope(binding, agent), pytest.raises((DataCorruptionError, WorkerDispatchError, InterruptedError)):
        transition = GatewayActiveTurnTransition(binding.claim.path, binding.claim.request_id, binding.claim.execution_attempt_id)
        requeue_current_processing_request(paths, binding.claim, transition, not_before_at=time.time()+60)
    assert binding.claim.path.read_bytes() == before
    assert reservation_file(paths, binding.claim.request_id).read_bytes() == p3


@pytest.mark.parametrize('when', [False, float('nan'), float('inf'), -1])
def test_current_yield_rejects_invalid_deadline(current, when):
    binding, _, agent, paths, transition = current
    before = binding.claim.path.read_bytes()
    with worker_execution_scope(binding, agent), pytest.raises(DataCorruptionError):
        transition = GatewayActiveTurnTransition(binding.claim.path, binding.claim.request_id, binding.claim.execution_attempt_id)
        requeue_current_processing_request(paths, binding.claim, transition, not_before_at=when)
    assert binding.claim.path.read_bytes() == before


def test_current_yield_wrong_transition_never_acts(current):
    binding, _, agent, paths, transition = current
    before = binding.claim.path.read_bytes()
    with worker_execution_scope(binding, agent), pytest.raises(DataCorruptionError):
        transition = GatewayActiveTurnTransition(binding.claim.path, binding.claim.request_id, binding.claim.execution_attempt_id)
        wrong = replace(transition, execution_attempt_id="not-original")
        requeue_current_processing_request(paths, binding.claim, wrong, not_before_at=time.time()+60)
    assert binding.claim.path.read_bytes() == before


def test_current_yield_expired_context_cannot_move_source(current):
    binding, _, agent, paths, transition = current
    before = binding.claim.path.read_bytes()
    with worker_execution_scope(binding, agent):
        transition = GatewayActiveTurnTransition(binding.claim.path, binding.claim.request_id, binding.claim.execution_attempt_id)
        copied = copy_context()
    with pytest.raises(WorkerDispatchError):
        copied.run(requeue_current_processing_request, paths, binding.claim, transition, not_before_at=time.time()+60)
    assert binding.claim.path.read_bytes() == before


def test_real_count0_frozen_owner_request_still_recovers(tmp_path):
    from agent_py_agent.agent.gateway_parts import (
        GatewayAskParams,
        gateway_paths,
        submit_gateway_ask,
    )
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.http_handlers import _prepare_http_gateway_input
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.recovery import (
        _RecoveryContext,
        recover_gateway_processing_requests_report,
    )
    from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
    from agent_py_agent.tests.test_local_store_gateway_recovery import (
        _ensure_gateway_dirs,
        _make_recovery_agent,
    )

    agent = _make_recovery_agent(tmp_path / 'project')
    agent.config.gateway_per_user_owner_scoping = False
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    rid, request_path, _ = submit_gateway_ask(paths, params=GatewayAskParams(prompt='合成恢复合同',save=False,agent=agent))
    payload = read_json_file(request_path)
    scope = GatewayControlScope('local-user', 'gateway-cli', 'default', resolved_owner=OwnerIdentity.local_main())
    prepared = _prepare_http_gateway_input(SimpleNamespace(agent=agent, paths=paths), scope, payload)
    assert REQUEST_OWNER_KEY in prepared
    assert 'route' not in prepared and 'gateway_admin_binding_version' not in prepared.get('metadata', {})
    write_json_file_atomic(request_path, prepared)
    claim = claim_request(paths, request_path)
    assert claim is not None
    before = claim.path.read_bytes()
    context = _RecoveryContext(time.time() + 100, 3, 1, False, agent, None)
    report = recover_gateway_processing_requests_report(paths, params=context)
    print({'summary':report.summary, 'load_errors':report.load_errors, 'processing_retained':claim.path.exists(), 'unchanged':claim.path.exists() and claim.path.read_bytes()==before})
    assert report.summary['requeued'] == 1, report.load_errors
    assert not report.load_errors
    pending=read_json_file(paths.inbox / claim.path.name)
    assert pending[REQUEST_OWNER_KEY] == prepared[REQUEST_OWNER_KEY]
    assert not claim.path.exists()


@pytest.mark.parametrize('source_state', ['present', 'missing', 'empty', 'null'])
def test_count0_repeated_recovery_preserves_immutable_source(tmp_path, source_state):
    from agent_py_agent.agent.gateway_parts import (
        GatewayAskParams,
        gateway_paths,
        multiprocess_mode,
        submit_gateway_ask,
    )
    from agent_py_agent.agent.gateway_parts.io import (
        gateway_request_fingerprint,
        read_json_file,
        write_json_file_atomic,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.recovery import (
        _RecoveryContext,
        recover_gateway_processing_requests_report,
    )
    from agent_py_agent.tests.test_local_store_gateway_recovery import (
        _ensure_gateway_dirs,
        _make_recovery_agent,
    )

    agent = _make_recovery_agent(tmp_path / 'count0')
    agent.config.gateway_per_user_owner_scoping = False
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    rid, pending, _ = submit_gateway_ask(paths, params=GatewayAskParams(prompt='原输入来源恢复', save=False, agent=agent))
    original = read_json_file(pending)
    if source_state == 'missing':
        original.pop('source', None)
    else:
        original['source'] = {'present': 'synthetic-origin', 'empty': '', 'null': None}[source_state]
    original['request_fingerprint'] = gateway_request_fingerprint(original, rid)
    write_json_file_atomic(pending, original)
    expected = ('source' in original, original.get('source'), original['request_fingerprint'])
    for index in range(2):
        claim = claim_request(paths, pending)
        assert claim is not None
        report = recover_gateway_processing_requests_report(paths, params=_RecoveryContext(
            time.time()+100, 3, 1, False, agent, None,
        ))
        assert report.summary['requeued'] == 1 and not report.load_errors, (index, report)
        actual = read_json_file(pending)
        assert ('source' in actual, actual.get('source'), actual['request_fingerprint']) == expected
