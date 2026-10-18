# LLM: 原临时C/control/Root、prepared receipt与P3均由产品创建；不建Agent、W、业务执行池或原生进程。
# 模块用途: 先证明cold loop未盖章的真实缺口，再核只读来源、纯准入、公平、配额和规范终态释放。
import copy
import threading
from contextlib import contextmanager
from importlib import import_module

import pytest

from agent_py_agent.agent.common.request_execution_ports import RequestExecutionContextUnavailable
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    coordinator_admission_gate,
    reservation_file,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    enqueue_source,
    freeze_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_curator_accounting import (
    CURATOR_DAILY_QUOTA_DEFAULT_COUNT,
    quota_date,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    resolve_owner_home,
)
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_gateway_prepared_route_immutability import _fingerprint, _request
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 不构造或伪装Agent；actual控制面和Root只供元数据入口消费，同一原租约仍是唯一准入来源。
# 函数用途: 从当前真实临时控制面生成cold CLI上下文。
def cold_context(control):
    return GatewayRunContext(None, control.paths, control.agent_root / 'config.yaml',
        coordinator_worker_count=control.worker_count, control_plane=control,
        drain_runtime=GatewayDrainRuntime(control))


# LLM: 通过原首次输入及队列入口生成原receipt/摘要/owner，动作在测试临时C内，不重签手改route。
# 函数用途: 准备同owner或跨owner排队请求，保留原actor及媒体字段用于精确对照。
def queue_request(control, identifier='cold-first', user='cold-alice', *, local=False):
    payload = _request()
    payload.update(id=identifier, request_id=identifier, user_id='local-agent' if local else user)
    payload['metadata'].update(channel='chat' if local else 'feishu', user_id=payload['user_id'],
        channel_message_id=identifier, client_input_digest='digest-' + identifier)
    payload['conversation']['id'] = 'thread-' + payload['user_id']
    _fingerprint(payload)
    with inputs.gateway_input_transition(control.paths, identifier):
        receipt, _ = inputs.load_or_prepare_gateway_input_locked(control.paths, request_id=identifier,
            client_input_digest='digest-' + identifier, client_message_id=identifier,
            guidance_dedupe_key='guidance-' + identifier, prepared_request=payload)
        receipt, _ = inputs.queue_gateway_input_locked(control.paths, receipt)
    return receipt


# LLM: 延迟导入使未实现的产品接口成为逐case红测，不把整份测试变成collection错误。
# 函数用途: 构造提案中的原cold元数据泵。
def pump(control):
    module = import_module('agent_py_agent.cli.gateway_coordinator_request_pump')
    return module.CoordinatorMetadataRequestPump(cold_context(control), control.paths)


def test_actual_cold_loop_stamps_original_w_request_without_agent_or_submit(context, monkeypatch):
    receipt = queue_request(context)
    stop = threading.Event()
    finished = loops.loop_health.mark_tick_finished
    calls = []

    def finish_one_tick(count):
        finished(count)
        stop.set()

    def no_business_dispatcher(*args):
        calls.append(args)
        raise AssertionError('cold metadata cannot construct business dispatcher')

    monkeypatch.setattr(loops.loop_health, 'mark_tick_finished', finish_one_tick)
    monkeypatch.setattr(loops, 'GATEWAY_REQUEST_POLL_INTERVAL_SECONDS', 0.001)
    monkeypatch.setattr(loops, '_RequestDispatcher', no_business_dispatcher)
    loops._gateway_request_loop(cold_context(context), context.paths, stop)
    stored = read_json_file(context.paths.inbox / (receipt.request_id + '.json'))
    assert type(stored.get('route', {}).get('worker')) is int
    assert read_json_file(reservation_file(context.paths, receipt.request_id))['state'] == 'occupied'
    assert stored['metadata'] == receipt.prepared_request['metadata']
    assert stored['input_media'] == receipt.prepared_request['input_media']
    assert stored['request_fingerprint'] == receipt.prepared_request['request_fingerprint']
    assert calls == [] and not list(context.paths.processing.glob('*.json'))
    owner = resolve_owner_home(context.home_root, OwnerIdentity.provider_user('feishu', 'cold-alice'))
    assert not owner.home_dir.exists()


def test_actual_metadata_control_gate_never_claims_c_or_reserves_without_agent(context):
    receipt = queue_request(context, local=True)
    gate = coordinator_admission_gate(context, context.paths, cold_context(context))
    submitted = []
    claimed = request_worker.dispatch_pending_requests(context.paths, request_worker.AdmissionLimits(8, 8),
        lambda *args: submitted.append(args), admission_gate=gate)
    assert claimed == 0 and submitted == []
    assert (context.paths.inbox / (receipt.request_id + '.json')).is_file()
    assert not reservation_file(context.paths, receipt.request_id).exists()
    assert not list(context.paths.processing.glob('*.json'))


@pytest.mark.parametrize('fault', ['copy', 'paths', 'control', 'runtime', 'closed', 'context', 'gate', 'pid', 'lease'])
def test_cold_pump_rejects_changed_original_source_before_queue_writes(context, fault):
    receipt = queue_request(context)
    current = pump(context)
    before = (context.paths.inbox / (receipt.request_id + '.json')).read_bytes()
    if fault == 'copy':
        current = copy.copy(current)
    elif fault == 'paths':
        current.paths = gateway_paths_from_root(context.paths.root)
    elif fault == 'control':
        current.control = copy.copy(current.control)
    elif fault == 'runtime':
        current.runtime = copy.copy(current.runtime)
    elif fault == 'closed':
        current.runtime.stop_producers()
    elif fault == 'context':
        current.context = copy.copy(current.context)
    elif fault == 'gate':
        current._admission_gate = copy.copy(current._admission_gate)
    elif fault == 'pid':
        current._creator_pid += 1
    else:
        current.runtime._lease_origin = copy.copy(current.runtime._lease_origin)
    with pytest.raises(ValueError):
        current.tick()
    assert (context.paths.inbox / (receipt.request_id + '.json')).read_bytes() == before
    assert not reservation_file(context.paths, receipt.request_id).exists()


def test_cold_pump_original_p2_refusal_keeps_pending_without_new_p3(context):
    receipt = queue_request(context, local=True)
    current = pump(context)
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
    context.controller()._unproven_owners.add(owner_digest(OwnerIdentity.local_main()))
    current.tick()
    payload = read_json_file(context.paths.inbox / (receipt.request_id + '.json'))
    assert payload['admission_wait_reason'] == 'owner_partition_quarantined'
    assert not reservation_file(context.paths, receipt.request_id).exists()


def test_same_original_gate_preserves_owner_fairness_and_canonical_release(context):
    context.config.gateway_user_inflight_limit = 1
    context.config.gateway_global_inflight_limit = 2
    first = queue_request(context, 'first', 'alice')
    second = queue_request(context, 'second', 'alice')
    other = queue_request(context, 'other', 'bob')
    current = pump(context)
    current.tick()
    gate = current._admission_gate
    assert gate.agent is context and gate.occupied_snapshot()['total'] == 2
    assert reservation_file(context.paths, first.request_id).exists()
    assert not reservation_file(context.paths, second.request_id).exists()
    assert reservation_file(context.paths, other.request_id).exists()
    # 仅是原规范terminal输入夹具，不声称Worker实际完成了任何业务。
    request = read_json_file(context.paths.inbox / (first.request_id + '.json'))
    terminal = {**request, 'schema_version': 'gateway_terminal_request.v1', 'turn_phase': 'closed',
                'status': 'cancelled', 'terminal_response': {'id': first.request_id, 'ok': False, 'status': 'cancelled'}}
    write_json_file_atomic(context.paths.terminal / (first.request_id + '.json'), terminal)
    current.tick()
    assert read_json_file(reservation_file(context.paths, first.request_id))['state'] == 'released'
    assert reservation_file(context.paths, second.request_id).exists()
    assert gate.occupied_snapshot()['total'] == 2
    assert current._admission_gate is gate


def test_pending_or_projection_only_never_releases_original_p3(context):
    receipt = queue_request(context)
    current = pump(context)
    current.tick()
    path = reservation_file(context.paths, receipt.request_id)
    before = path.read_bytes()
    write_json_file_atomic(context.paths.done / (receipt.request_id + '.json'), {'id': receipt.request_id, 'ok': True})
    current.tick()
    assert path.read_bytes() == before and current._admission_gate.occupied_snapshot()['total'] == 1


def test_missing_context_error_propagates_same_object_and_keeps_original_p3(context, monkeypatch):
    receipt = queue_request(context)
    current = pump(context)
    current.tick()
    path = reservation_file(context.paths, receipt.request_id)
    before = path.read_bytes()
    original = current._admission_gate.release_settled
    error = RequestExecutionContextUnavailable('synthetic_original_source_unavailable')

    def unavailable():
        original()
        raise error

    monkeypatch.setattr(current._admission_gate, 'release_settled', unavailable)
    with pytest.raises(RequestExecutionContextUnavailable) as caught:
        current.tick()
    assert caught.value is error and path.read_bytes() == before
    assert not list(context.paths.terminal.glob('*.json'))


def test_cold_pump_repairs_queued_cursor_without_unsettled_input_reconciliation(context, monkeypatch):
    receipt = queue_request(context)
    receipt_path = inputs.gateway_input_receipt_path(context.paths, receipt.request_id)
    before = receipt_path.read_bytes()
    current = pump(context)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError('cold metadata has no owner business reconciliation source')

    monkeypatch.setattr(loops, 'reconcile_gateway_input_receipts', forbidden)
    monkeypatch.setattr(inputs, 'reconcile_gateway_input_receipts', forbidden)
    current.tick()
    current.shutdown()
    assert calls == []
    cursor = context.paths.root / 'input_receipts' / 'state' / 'reconcile_cursor.json'
    assert read_json_file(cursor)['last_name'] == receipt.request_id + '.json'
    assert receipt_path.read_bytes() == before
    assert (context.paths.root / 'projection_state' / 'terminal_cursor.json').is_file()


# LLM: 所有owner/store/state都在持C前由原Count0入口准备，之后只消费真实ControlPlane和ref，不伪造Agent字段。
# 函数用途: 为cold配额测试生成已有消息的两名owner和真实策展源，可选原pending/lease豁免。
@contextmanager
def curator_control(tmp_path, monkeypatch, *, exemption='none'):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    config = AgentConfig(my_agent_home=str(home))
    refs = []
    for name in ('alice', 'bob'):
        owner = OwnerIdentity.provider_user('feishu', name)
        resolved = ensure_owner_home(home, owner)
        store = ConversationStore(resolved.home_dir / 'conversations')
        thread = store.threads.get_or_create({'canonical_user_id': name, 'channel': 'internal',
                                            'channel_conversation_id': 'curator-' + name})
        store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '隔离测试消息'})
        state = MemoryCuratorStateStore(resolved.home_dir / 'memory' / 'curator' / 'state.json')
        if exemption in {'pending', 'lease'}:
            state.request('admin')
        if exemption == 'lease':
            assert state.acquire(reason='admin', config_revision='test', lease_seconds=60) is not None
        refs.append(freeze_source(home, owner, state.path, 'curator'))
    if exemption == 'none':
        # 原legacy日累计输入；迁移须保留，不伪称测试真的跑过这些模型调用。
        write_json_file_atomic(home / 'global_index' / 'curator_daily_quota.json',
            {'date': quota_date(), 'count': CURATOR_DAILY_QUOTA_DEFAULT_COUNT - 1})
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, 'cold-curator')
        assert controller.active()
        control = GatewayControlPlane(config, home, paths, 2, 'cold-curator', agent_root=tmp_path,
                                      workspace_roots=(tmp_path,))
        yield control, refs


@pytest.mark.parametrize('exemption', ['none', 'pending', 'lease'])
def test_actual_control_gate_reads_same_canonical_curator_quota_root(tmp_path, monkeypatch, exemption):
    with curator_control(tmp_path, monkeypatch, exemption=exemption) as (control, refs):
        paths = [enqueue_source(control.paths, control.home_root, ref)[0] for ref in refs]
        run = cold_context(control)
        gate = coordinator_admission_gate(control, control.paths, run)
        run.drain_runtime.retain_admission(gate)
        calls = []
        request_worker.dispatch_pending_requests(control.paths, request_worker.AdmissionLimits(8, 8),
            lambda *args: calls.append(args), admission_gate=gate)
        assert calls == [] and not list(control.paths.processing.glob('*.json'))
        rows = [read_json_file(path) for path in paths]
        expected = 1 if exemption == 'none' else 2
        assert sum('route' in row for row in rows) == expected
        assert gate.occupied_snapshot()['total'] == expected
        quota = control.home_root / 'global_index' / 'curator_daily_quota.json'
        assert quota.exists() is (exemption == 'none')
        if exemption == 'none':
            assert read_json_file(quota)['count'] == CURATOR_DAILY_QUOTA_DEFAULT_COUNT - 1
            assert any(row.get('admission_wait_reason') == 'curator_daily_quota_exhausted' for row in rows)
