# LLM: FG3 uses real temporary files, lifecycle locks, admission/closeout and TUI reducers.
# Only OS liveness observations and the periodic recheck launcher are controlled inputs;
# no production retire/closeout/guard/renderer is replaced in the positive scenarios.
# 函数用途: 回放固定种子的多代崩溃事件，并检查连续 headless 会话不会继承旧任务。
"""Deterministic semantic traces, not a claim of physical C/W or terminal E2E.

API mapping for main: GatewayRuntimePaths -> gateway_paths; expected_worker_count
-> target_count; reconciliation -> PersistentAdmission.try_close_unroutable_pending.
The trace records explicit business outcomes, not wall-clock timestamps, UUIDs,
absolute paths or OS-assigned PIDs. It does not normalize away failures.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace

import pytest

from agent_py_agent.agent.common.runtime_gate_registry import (
    SubagentCreationGuardBusy,
    current_thread_holds_runtime_gate,
)
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.local_run_control import LocalRunControl
from agent_py_agent.agent.core import SimpleAgent, configure_canonical_background_contracts
from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_generation_retirement as retirement
from agent_py_agent.agent.gateway_parts import multiprocess_route as routes
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as takeover
from agent_py_agent.agent.gateway_parts import multiprocess_unroutable_closeout as closeout
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    gateway_request_fingerprint,
    read_json_file,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    CoordinatorAdmissionScope,
    PersistentAdmission,
    reservation_file,
    session_scope_for_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_publish import (
    publish_worker_registry,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    CoordinatorUnavailable,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
    freeze_coordinator_claim,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    CoordinatorExecution,
    coordinator_execution_scope,
    current_coordinator_execution,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
    _publish_generation_once,
    read_generation_metadata,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import RouteStamp, freeze_request_route
from agent_py_agent.agent.gateway_parts.paths import gateway_paths, gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_transactions import request_transaction_ports_scope
from agent_py_agent.agent.gateway_parts.request_worker import (
    AdmissionLimits,
    _finish_claimed_gateway_request,
)
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_sources import require_standalone_sources
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.coordination import subagent_creation_guard
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_parts.control_runtime import (
    ChatControlExecution,
    ChatControlState,
    execute_chat_control,
)
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, render_tui_snapshot
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary


# LLM: The agent owns only this invocation's temporary home; no model/network is run.
# 函数用途: 构造独立的真实会话和运行账本。
def _agent(root):
    configure_canonical_background_contracts()
    return SimpleAgent(AgentConfig(my_agent_home=str(root / 'home'), model_backend='echo',
                                  gateway_per_user_owner_scoping=False), root / 'workspace')


# LLM: This is input preparation, not an authentication test or a route repair.
# 函数用途: 为模拟事件创建尚未发表过的、带完整指纹的本机请求。
def _request(request_id, *, owner=None):
    identity = owner or OwnerIdentity.local_main()
    owner = asdict(identity)
    actor = 'local-agent' if identity == OwnerIdentity.local_main() else identity.owner_id
    channel = 'chat' if identity == OwnerIdentity.local_main() else identity.provider
    payload = {'id': request_id, 'kind': 'ask', 'goal': 'seeded headless input',
               'status': 'pending', 'created_at': 1.0, 'user_id': actor,
               'host_request_owner.v1': owner,
               'metadata': {'channel': channel, 'user_id': actor,
                            'host_request_owner.v1': owner, 'gateway_admin_binding_version': 'none'},
               'conversation': {'channel': channel, 'channel_conversation_id': 'simulation'}}
    payload['request_fingerprint_schema'] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    payload['request_fingerprint'] = gateway_request_fingerprint(payload, request_id)
    return payload


# LLM: Real lease/controller/control-plane registration; no synthetic M/L owner or fake execution.
# 函数用途: 在准确根和分区数的真实协调者锁内建立准入门。
@contextmanager
def _gate(agent, paths, count, generation, *, retired=None):
    # A prior C's DTO deliberately cannot be rebound to a successor lease.
    # Reconstruct the same-root DTO just as a fresh coordinator process does.
    paths = gateway_paths_from_root(paths.root)
    with coordinator_guard(paths.root, count):
        if retired is not None:
            manifest = retirement.retire_dead_multiprocess_generations(
                paths, target_count=count, current_c_run_id=generation)
            assert manifest is not None, 'dead_generation_not_retired'
            retired.append(manifest)
        controller = takeover.prepare_coordinator_takeover(paths, count, generation)
        gate = PersistentAdmission(paths, agent, CoordinatorAdmissionScope(count, generation))
        gate._runtime_takeover, gate._runtime_takeover_required = controller, True
        control = GatewayControlPlane(agent.config, agent.home_paths.root, paths, count, generation,
                                      agent_root=agent.root)
        GatewayDrainRuntime(control).retain_admission(gate)
        yield gate


# LLM: Event handshakes force contention; timeout is a failure bound, never a trace value.
# 函数用途: 让另一个线程真实持有创建文件锁，并保证异常时也释放、收线程。
@contextmanager
def _contended_creation_guard(workspace):
    ready, release = threading.Event(), threading.Event()
    failures = []

    # LLM: Only this helper thread owns CG; it never waits for the caller's M/L.
    # 函数用途: 在明确释放事件前占住真实文件锁。
    def hold():
        try:
            with subagent_creation_guard(workspace):
                ready.set()
                assert release.wait(5), 'creation_guard_release_timeout'
        except BaseException as error:
            failures.append(error)
            ready.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert ready.wait(5), 'creation_guard_acquisition_timeout'
        assert not failures, failures
        yield
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive(), 'creation_guard_thread_leaked'
        assert not failures, failures


# LLM: Admit/claim a real C request, scope its original execution, contend real CG under M/L,
# then terminalize the same claim. Nothing here grants permission from a fabricated claim.
# 函数用途: 验证真实执行上下文、非阻塞锁竞争、重入及退出后无残留。
def _exercise_execution(gate, request_id, rng):
    payload = _request(request_id)
    path = gate.paths.inbox / f'{request_id}.json'
    write_json_file_atomic(path, payload)
    # A complete new prepared input is not a legacy request; admission must publish
    # its original P3 before its route, never accept a forged route without reservation.
    before = path.read_bytes()
    assert gate.try_close_unroutable_pending(path) is None, 'routable_request_retired'
    assert path.read_bytes() == before
    ownership = (OwnerIdentity.local_main(), session_scope_for_request(payload))
    admitted = gate.admit(path, ownership, AdmissionLimits(8, 50))
    assert admitted['status'] == 'admitted', admitted
    stamped = read_json_file(path)
    assert stamped['route']['c_run_id'] == gate.c_run_id and stamped['route']['worker'] == 'C'
    assert stamped['request_fingerprint'] == payload['request_fingerprint']
    before = path.read_bytes()
    assert gate.try_close_unroutable_pending(path) is None, 'reserved_request_retired'
    assert path.read_bytes() == before
    claim = claim_request(gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, admitted))
    assert claim is not None
    assert current_coordinator_execution() is None
    lock_name = rng.choice(('mutex', 'lease'))
    controller = gate._runtime_takeover
    runtime_lock = controller._mutex if lock_name == 'mutex' else controller._lease._lifecycle_gate
    with coordinator_execution_scope(gate, claim, gate.agent), request_transaction_ports_scope(gate.agent):
        execution = current_coordinator_execution()
        assert type(execution) is CoordinatorExecution and execution.claim is claim
        execution.lifetime.require_original(execution)
        with _contended_creation_guard(gate.agent.root):
            with runtime_lock:
                assert current_thread_holds_runtime_gate(), 'runtime_gate_not_registered'
                with pytest.raises(SubagentCreationGuardBusy):
                    with subagent_creation_guard(gate.agent.root):
                        pytest.fail('contended_creation_guard_entered')
        # Acquire after release and reenter while M is held: same-thread recursion is legal.
        with subagent_creation_guard(gate.agent.root), controller._mutex:
            with subagent_creation_guard(gate.agent.root):
                assert current_coordinator_execution() is execution
        _finish_claimed_gateway_request(gate.paths, claim.path, claim.request_id,
            {'id': request_id, 'status': 'done', 'ok': True, 'response': 'simulation completed'},
            conversation_store=gate.agent.conversation_store, execution_agent=gate.agent,
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
    assert current_coordinator_execution() is None
    assert not current_thread_holds_runtime_gate(), 'runtime_gate_leaked'
    with pytest.raises(ValueError):
        execution.lifetime.require_original(execution)
    assert read_json_file(gate.paths.terminal / path.name)['status'] == 'done'
    return {'request_id': request_id, 'runtime_lock': lock_name, 'contention': 'busy',
            'reentrant': True, 'execution_closed': True, 'terminal': 'done'}


# LLM: All role records go through the production publishers/codecs; synthetic identities
# are only inputs to the injected OS observer, never presented as real host processes.
# 函数用途: 创建带完整 C/W 记录、未封存元数据和可切换存活状态的代次。
def _spawn_generation(paths, generation, count, live, episode, liveness):
    base = 900000000 + episode * 100
    identity = {'schema_version': 'multiprocess_process_identity.v1',
                'host_id': 'fg3-simulation-host', 'pid': base, 'start_time': 1}
    liveness[base] = False
    workers = []
    for index in range(count):
        worker_identity = {**identity, 'pid': base + index + 1}
        liveness[worker_identity['pid']] = live
        workers.append({'worker': index, 'instance_id': f'instance-{episode}-{index}',
                        'process_identity': worker_identity,
                        'executor_ledger_ref': f'workers/executors/w{index}'})
    publish_worker_registry(paths.root, {'schema_version': 'multiprocess_worker_registry.v1',
        'c_run_id': generation, 'worker_count': count, 'process_identity': identity,
        'http_port': 43123, 'workers': workers}, None)
    _publish_generation_once(paths.root, {'schema_version': 'multiprocess_c_generation.v2',
        'c_run_id': generation, 'worker_count': count, 'closure': None})
    write_json_file_atomic(paths.state, {'c_run_id': generation, 'worker_count': count,
                                       'pid': base, 'status': 'running'})
    for row in workers:
        write_json_file_atomic(paths.root / f"workers/w{row['worker']}.json", {
            'schema_version': 'multiprocess_worker_heartbeat.v1', 'worker': row['worker'], 'of': count,
            'pid': row['process_identity']['pid'], 'process_identity': row['process_identity'],
            'c_run_id': generation, 'instance_id': row['instance_id'], 'ready': True,
            'loop_health': 'running', 'inflight': 0, 'updated_at': 1.0,
            'readiness': {'ready': True, 'reason': 'ready'}})


# LLM: Enumerating pending is a test driver; every decision/projection is still the real hook.
# It asserts canonical bytes/response facts, not merely a drained directory.
# 函数用途: 逐条收口旧输入，并核对原路由、指纹和实际终态响应未被重建。
def _reconcile(gate, originals):
    outcomes = []
    for path in sorted(gate.paths.inbox.glob('*.json')):
        original = originals[path.stem]
        assert closeout.unroutable_reason(gate.paths, read_json_file(path), gate=gate) == 'stale_generation_route'
        decision = gate.try_close_unroutable_pending(path)
        assert decision is not None and decision['status'] == 'expired', 'pending_not_closed'
        response = read_json_file(gate.paths.responses / path.name)
        terminal = read_json_file(gate.paths.terminal / path.name)
        assert response == decision['response'] == terminal['terminal_response']
        assert response['error_code'] == 'GATEWAY_REQUEST_ROUTE_RETIRED'
        assert response['route_retirement_reason'] == 'stale_generation_route'
        assert terminal['route'] == original['route'], 'route_was_rewritten'
        assert terminal['request_fingerprint'] == original['request_fingerprint']
        assert terminal['metadata'] == original['metadata']
        assert not path.exists() and not reservation_file(gate.paths, path.stem).exists()
        outcomes.append({'request_id': path.stem, 'status': decision['status'],
                         'error_code': response['error_code'],
                         'reason': response['route_retirement_reason']})
    return outcomes


# LLM: Count0 uses the original lifecycle lock and original source gate, not an empty-dir proxy.
# 函数用途: 清除已退出协调者的设施，并实际证明下一次启动的来源检查可通过。
def _standalone_cleanup(paths):
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        require_standalone_sources(paths, lifecycle)
        assert retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle) is None
    return [] if manifest is None else manifest['generations']


# LLM: Exactly 25 semantic events; every five-step episode guarantees spawn/pending/CG/
# crash/retire+reconcile coverage while seeded choices vary count, targets, IDs and lock.
# Only external liveness and periodic background scheduling are controlled; real producers,
# lease/controller registration, retire/closeout and filesystem locks execute unchanged.
# 函数用途: 在独立临时根回放五代崩溃，返回完整结构化轨迹和其规范 JSON 的 SHA-256。
def run_gateway_simulation(tmp_root, seed):
    rng, trace, liveness, originals = random.Random(seed), [], {}, {}
    agent = _agent(tmp_root)
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    real_liveness = daemon_metadata.process_identity_is_live

    # LLM: Answer only for this simulation's synthetic identities; retain real probing elsewhere.
    # 函数用途: 将随机事件的 C/W 存活输入传给真实退役判定器。
    def observed_liveness(identity):
        if isinstance(identity, dict) and identity.get('host_id') == 'fg3-simulation-host':
            return liveness[identity['pid']]
        return real_liveness(identity)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_metadata, 'process_identity_is_live', observed_liveness)
        # Background polling is not a seeded event and must not race this deterministic driver.
        patch.setattr(takeover, 'start_takeover_rechecks', lambda _controller: None)
        for episode in range(5):
            cleanup = _standalone_cleanup(paths)
            generation = f'generation-{episode}'
            count = rng.choice((1, 2, 4))
            live = episode == 0 or (episode > 1 and bool(rng.getrandbits(1)))
            # A cold indexed C owns execution. Synthetic old role files are introduced only
            # after it exits: importing them into a running C correctly quarantines its owner.
            with _gate(agent, paths, count, f'execution-generation-{episode}') as gate:
                trace.append({'event': 'execution_and_contention',
                              **_exercise_execution(gate, f'execution-{episode}', rng)})
            cleanup += _standalone_cleanup(paths)
            _spawn_generation(paths, generation, count, live, episode, liveness)
            with coordinator_guard(paths.root, count):
                refusal = None
                if live:
                    before = {str(p.relative_to(paths.root)): p.read_bytes()
                              for p in (paths.root / 'workers').rglob('*') if p.is_file()}
                    with pytest.raises(CoordinatorUnavailable, match='retirement_worker_alive'):
                        retirement.retire_dead_multiprocess_generations(
                            paths, target_count=count, current_c_run_id='not-this-generation')
                    after = {str(p.relative_to(paths.root)): p.read_bytes()
                             for p in (paths.root / 'workers').rglob('*') if p.is_file()}
                    assert before == after, 'live_generation_was_moved'
                    refusal = 'retirement_worker_alive'
                trace.append({'event': 'spawn', 'generation': generation, 'worker_count': count,
                              'workers_live': live, 'refusal': refusal, 'prior_cleanup': cleanup})
                requests = []
                for fault in ('stale_generation', 'out_of_range_worker'):
                    request_id = f'pending-{episode}-{fault}-{rng.randrange(100000)}'
                    stamp = RouteStamp(count, f'stale-{episode}' if fault == 'stale_generation'
                                       else generation, 1.0, 'none')
                    payload = freeze_request_route(_request(request_id), OwnerIdentity.local_main(), stamp)
                    if fault == 'out_of_range_worker':
                        # An intentionally faulty initial input, not a repair of published route bytes.
                        payload['route']['worker'] = count + rng.randrange(4)
                        assert payload['route']['worker'] >= count
                    path = paths.inbox / f'{request_id}.json'
                    write_json_file_atomic(path, payload)
                    originals[request_id] = payload
                    requests.append({'request_id': request_id, 'fault': fault,
                                     'c_run_id': payload['route']['c_run_id'],
                                     'worker': payload['route']['worker']})
                trace.append({'event': 'pending', 'requests': requests})
                for pid in liveness:
                    liveness[pid] = False
                metadata, _ = read_generation_metadata(paths.root, generation)
                assert metadata['closure'] is None, 'unclean_exit_forged_closure'
                trace.append({'event': 'unclean_exit', 'generation': generation,
                              'closure': metadata['closure'], 'all_workers_dead': not any(liveness.values())})
            target = rng.choice((1, 2, 4))
            retired = []
            with _gate(agent, paths, target, f'recovery-{episode}', retired=retired) as successor:
                outcomes = _reconcile(successor, originals)
                assert len(outcomes) == 2, 'not_all_seeded_requests_reconciled'
                assert not list(paths.inbox.glob('*.json')), 'pending_requests_stuck'
                archived = list((paths.root / 'multiprocess_retired').glob('*/workers/generations/*.json'))
                old_metadata = [read_json_file(p) for p in archived
                                if read_json_file(p).get('c_run_id') == generation]
                assert len(old_metadata) == 1 and old_metadata[0]['closure'] is None
                trace.append({'event': 'retire_and_reconcile', 'target_count': target,
                              'generations': retired[0]['generations'], 'requests': outcomes})
        # A final positive-count sweep followed by Count0's real startup source gate.
        with _gate(agent, paths, 4, 'final-sweep') as gate:
            # Recovery already retired the last crashed generation. An empty final
            # sweep is the expected idempotent result, not another synthetic death.
            assert retirement.retire_dead_multiprocess_generations(
                gate.paths, target_count=4, current_c_run_id=gate.c_run_id) is None
            assert gate._runtime_takeover.active() and not gate._runtime_takeover._all_quarantined
            assert _reconcile(gate, originals) == []
            assert not any(closeout.unroutable_reason(paths, read_json_file(p), gate=gate)
                           for p in paths.inbox.glob('*.json'))
        _standalone_cleanup(paths)
        unretired = list((paths.root / 'workers/generations').glob('*.json'))
        pending = list(paths.inbox.glob('*.json'))
        assert not unretired, 'dead_unretired_generation_blocks_startup'
        assert not pending, 'pending_requests_stuck'
        trace[-1]['end_of_run'] = {'unretired_generations': len(unretired),
                                  'pending_requests': len(pending), 'startup_sources': 'admissible'}
    assert len(trace) == 25
    encoded = json.dumps(trace, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return trace, hashlib.sha256(encoded).hexdigest()


# LLM: Five explicit seeds each execute two independent real filesystem worlds; compare full
# trace equality AND byte encoding/digests, including every step and the liveness proof.
# 函数用途: 锁定逐步、逐位回放一致性，而不是只比较一个预置摘要。
@pytest.mark.parametrize('seed', [101, 202, 303, 404, 505])
def test_deterministic_gateway_chaos_replay(tmp_path, seed, record_property):
    trace1, digest1 = run_gateway_simulation(tmp_path / 'first', seed)
    trace2, digest2 = run_gateway_simulation(tmp_path / 'replay', seed)
    assert trace1 == trace2, f'seed={seed}: replay trace diverged'
    assert digest1 == digest2, f'seed={seed}: replay digest diverged'
    assert len(digest1) == 64
    assert trace1[-1]['end_of_run'] == {
        'unretired_generations': 0, 'pending_requests': 0, 'startup_sources': 'admissible'}
    record_property('seed', seed)
    record_property('sha256', digest1)


# LLM: Assert raw renderer output; main's full-width colon is intentional typography, not
# a replacement renderer. A positive-count badge must be the literal trailing fragment.
# 函数用途: 检查零计数隐藏、正计数尾置，并拒绝旧模式或路由提示。
def _assert_footer(frame, total, running):
    footer = fragments_text(frame.footer).rstrip()
    assert '/route' not in footer, 'forbidden_footer_route'
    assert not footer.lstrip().startswith(('简洁', '详细')), 'forbidden_footer_mode'
    if total == 0:
        assert '后台Agent' not in footer, 'zero_agent_badge_visible'
    else:
        assert footer.endswith(f'后台Agent：运行 {running} / 总 {total}'), 'agent_badge_not_trailing'


# LLM: The checks consume the same continuous TuiRuntime/StateStore at every checkpoint,
# and reread the actual thread index. No hand-built TuiViewSnapshot or task-id oracle.
# 函数用途: 对真实当前视图和持久会话同时验证任务隔离及多种 footer 状态。
def _headless_checkpoint(runtime, store, thread_id, *, retired_ids=()):
    thread = store.threads.require(thread_id)
    assert not set(retired_ids).intersection((*thread.task_ids, *thread.active_task_ids)), 'old_task_leaked'
    snapshot = runtime.store.snapshot()
    if retired_ids:
        visible = repr((snapshot.stable_blocks, snapshot.active_blocks,
                        snapshot.pending_steers, snapshot.queued_inputs, snapshot.status))
        assert not any(identifier in visible for identifier in retired_ids), 'old_ui_reference_leaked'
    for detailed in (False, True):
        for route in (False, True):
            for total, running in ((0, 0), (3, 0), (3, 2)):
                context = TuiRenderContext(width=160, detailed_transcript=detailed, route_enabled=route,
                    background_agent_count=total, background_agent_running_count=running)
                _assert_footer(render_tui_snapshot(snapshot, context), total, running)


# LLM: Local controls, guidance, original RuntimeDB authority, task links, thread succession
# and reset reducer are real. Streaming/worker terminal notifications are headless inputs;
# there is no provider call, physical process cleanup, real keyboard or Gateway HTTP claim.
# 函数用途: 在同一客户端连续经历流式回答、steer、stop、new及第二轮完成，检查旧任务不串轮。
def test_headless_bot_steer_stop_new_and_footer_invariants(tmp_path):
    from agent_py_agent.agent.runtime_db.managed_operation_store import (
        AuthorityContextMissing,
        ManagedOperationStore,
        ToolOperationAuthorityRequest,
    )

    agent = _agent(tmp_path)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    session_id, request_one, task_one = 'fg3-headless-session', 'fg3-turn-one', 'fg3-task-one'
    background_task, task_two = 'fg3-background-one', 'fg3-task-two'
    old = store.threads.get_or_create({'canonical_user_id': 'local-agent', 'channel': 'chat',
        'channel_user_id': 'local-agent', 'channel_conversation_id': session_id,
        'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir),
        'cwd': str(agent.root)})
    for task_id in (task_one, background_task):
        store.tasks.bind({'task_id': task_id, 'thread_id': old.thread_id, 'status': 'active', 'goal': task_id})
    record = repo.record_run_creation(owner_id=agent.home_paths.owner_id, conversation_task_id=task_one,
                                     run_id='fg3-run-one', thread_id=old.thread_id, role='main')
    control = LocalRunControl(request_one)
    binding = {'invocation_run_id': request_one, 'task_id': task_one, 'run_id': 'fg3-run-one',
               'agent_run_id': record['agent_run_id'], 'attempt_id': record['attempt_id']}
    assert control.bind_runtime_authority(binding)
    authority = ToolOperationAuthorityRequest(agent.home_paths.owner_id, 'fg3-run-one', task_one,
                                              'fg3-operation', 'run_command', record['attempt_id'])
    operations = ManagedOperationStore(repo)
    operations.require_authority(authority)
    runtime = TuiRuntime(session_id)
    history, outputs = [('old user', 'old assistant')], ['old output']
    runtime.bind_conversation_history(history, threading.Lock(), outputs)
    runtime.enqueue_prompt(request_one, '第一轮：核对事实', queued=False)
    first = runtime.begin_turn(request_one, task_progress_generation_id=task_one)
    _headless_checkpoint(runtime, store, old.thread_id)
    for chunk in ('第一段流式回答。', '第二段继续。'):
        assert first.write_model(chunk, public_stream=True)
        assert chunk in ''.join(block.text for block in runtime.store.snapshot().active_blocks)
        _headless_checkpoint(runtime, store, old.thread_id)
    active = ChatControlExecution(agent, False,
        ChatControlState(True, 0, '第一轮', 0, session_id, request_one, control))
    steer = execute_chat_control(active, parse_conversation_control('/btw 补充：先保留证据'))
    assert steer.ok and steer.request_id == request_one
    runtime.enqueue_active_turn_input('fg3-steer-one', '补充：先保留证据')
    assert len(runtime.store.snapshot().pending_steers) == 1
    _headless_checkpoint(runtime, store, old.thread_id)
    busy = execute_chat_control(active, parse_conversation_control('/new'))
    assert not busy.ok and busy.error_code == 'NEW_SESSION_BUSY'
    assert store.threads.require(old.thread_id).status == 'active'
    _headless_checkpoint(runtime, store, old.thread_id)
    assert runtime.request_interrupt()
    stopped = execute_chat_control(active, parse_conversation_control('/stop'))
    assert stopped.ok and stopped.delivery_status == 'accepted', stopped
    with pytest.raises(AuthorityContextMissing):
        operations.require_authority(authority)
    with pytest.raises(InterruptedError):
        control.bind_runtime_authority(binding)
    _headless_checkpoint(runtime, store, old.thread_id)
    # Simulate the worker's terminal notification, not a second implementation of /stop.
    runtime.complete_turn(request_one, TuiTurnSummary(ok=False, interrupted=True))
    control.finish()
    store.tasks.update_status({'task_id': task_one, 'status': 'interrupted'})
    _headless_checkpoint(runtime, store, old.thread_id)
    idle = ChatControlExecution(agent, False, ChatControlState(False, 0, '', 0, session_id))
    created = execute_chat_control(idle, parse_conversation_control('/new 第二轮'))
    assert created.ok and created.details['view_reset'] is True, created
    assert created.details['predecessor_thread_id'] == old.thread_id
    new_id = created.details['thread_id']
    assert new_id != old.thread_id
    runtime.publish_conversation_reset(new_id)
    assert history == [] and outputs == []
    assert runtime.conversation_reset_generation == 1
    assert runtime.store.snapshot().pending_steers == ()
    assert runtime.store.snapshot().queued_inputs == ()
    assert not store.threads.require(new_id).task_ids
    assert not store.messages.recent(new_id)
    retired_ids = (task_one, background_task, request_one, 'fg3-steer-one')
    _headless_checkpoint(runtime, store, new_id, retired_ids=retired_ids)
    archived = store.threads.require(old.thread_id)
    assert archived.status == 'archived'
    assert set(archived.task_ids) == {task_one, background_task}
    assert background_task in archived.active_task_ids, 'new_stopped_independent_background_task'
    assert store.tasks.load(background_task).status == 'active'
    resolved = store.threads.resolve(channel='chat', channel_conversation_id=session_id, channel_user_id='local-agent')
    assert resolved.thread_id == new_id
    # Delayed old streams cannot repaint after the conversation boundary.
    before_late = runtime.store.snapshot()
    first.write_model('LATE OLD TURN', public_stream=True)
    assert runtime.store.snapshot() == before_late, 'late_stream_changed_new_conversation'
    _headless_checkpoint(runtime, store, new_id, retired_ids=retired_ids)
    store.tasks.bind({'task_id': task_two, 'thread_id': new_id, 'status': 'active', 'goal': 'second turn'})
    runtime.enqueue_prompt('fg3-turn-two', '第二轮：重新开始', queued=False)
    second = runtime.begin_turn('fg3-turn-two', task_progress_generation_id=task_two)
    assert second is not first
    _headless_checkpoint(runtime, store, new_id, retired_ids=retired_ids)
    assert second.write_model('第二轮独立回答。', public_stream=True)
    _headless_checkpoint(runtime, store, new_id, retired_ids=retired_ids)
    runtime.complete_turn('fg3-turn-two', TuiTurnSummary(response_text='第二轮独立回答。', ok=True))
    store.tasks.update_status({'task_id': task_two, 'status': 'done'})
    _headless_checkpoint(runtime, store, new_id, retired_ids=retired_ids)
    latest = store.threads.require(new_id)
    assert latest.task_ids == (task_two,) and latest.active_task_ids == ()
    assert runtime.store.snapshot().active_blocks == ()
    assert runtime._turns == {} and runtime._retired_turns == {} and runtime._completing_turns == {}


# LLM: Deliberately break one production boundary only in isolated negative controls;
# the same positive simulation must detect each omission rather than merely hash its own plan.
# 函数用途: 证明遗漏真实退役或请求收口时，回放断言确实报错。
@pytest.mark.parametrize('fault', ['retirement', 'closeout'])
def test_simulation_falsifies_missing_recovery(tmp_path, monkeypatch, fault):
    if fault == 'retirement':
        original = retirement.retire_dead_multiprocess_generations

        # LLM: Suppress precisely the first recovery pass; all earlier real guards still execute.
        # 函数用途: 注入一个未退役旧代的反例。
        def omit_retirement(paths, **kwargs):
            if kwargs.get('current_c_run_id') == 'recovery-0':
                return None
            return original(paths, **kwargs)

        monkeypatch.setattr(retirement, 'retire_dead_multiprocess_generations', omit_retirement)
        reason = 'dead_generation_not_retired'
    else:
        original = PersistentAdmission.try_close_unroutable_pending

        # LLM: Keep the routable negative control real; omit only seeded stale pending closeouts.
        # 函数用途: 注入一个输入永远留在 pending 的反例。
        def omit_closeout(self, path, **kwargs):
            if path.stem.startswith('pending-'):
                return None
            return original(self, path, **kwargs)

        monkeypatch.setattr(PersistentAdmission, 'try_close_unroutable_pending', omit_closeout)
        reason = 'pending_not_closed'
    with pytest.raises(AssertionError, match=reason):
        run_gateway_simulation(tmp_path, 101)


# LLM: Inject corrupt footer fragments into a real frame and prove each invariant is live.
# 函数用途: 反证旧路由提示、模式前缀、零计数徽标和非尾置徽标不能假绿。
@pytest.mark.parametrize('text,total,running,reason', [
    ('/route', 0, 0, 'forbidden_footer_route'),
    ('简洁 · shortcuts', 0, 0, 'forbidden_footer_mode'),
    ('详细 · shortcuts', 0, 0, 'forbidden_footer_mode'),
    ('后台Agent：运行 0 / 总 0', 0, 0, 'zero_agent_badge_visible'),
    ('后台Agent：运行 2 / 总 3 · trailing junk', 3, 2, 'agent_badge_not_trailing'),
])
def test_footer_invariants_falsify_bad_frames(text, total, running, reason):
    frame = render_tui_snapshot(TuiRuntime('footer-counterexample').store.snapshot(), TuiRenderContext(width=160))
    with pytest.raises(AssertionError, match=reason):
        _assert_footer(replace(frame, footer=(('', text),)), total, running)


# LLM: Isolate a current-generation W range fault from old-generation retirement. Main's
# policy is fail-closed while current (not an invented expiry reason), then close after rollover.
# The valid W control and direct production validator prevent another refusal masking range acceptance.
# 函数用途: 在未换代时单独验证越界拒绝及零写入，再验证换代后原坏路由可收口。
def _current_worker_range_scenario(root, count):
    agent = _agent(root)
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    owner = OwnerIdentity.provider_user('synthetic', 'range-user')
    request_id = 'current-worker-range'
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(takeover, 'start_takeover_rechecks', lambda _controller: None)
        with _gate(agent, paths, count, 'range-current') as gate:
            valid = freeze_request_route(_request(request_id, owner=owner), owner,
                                        RouteStamp(count, gate.c_run_id, 1.0, 'none'))
            assert routes.validate_published_route(valid, count, gate.c_run_id)['ok'] is True
            faulty = {**valid, 'route': {**valid['route'], 'worker': count}}
            assert faulty['route']['c_run_id'] == gate.c_run_id
            assert faulty['route']['worker'] >= count
            checked = routes.validate_published_route(faulty, count, gate.c_run_id)
            assert checked['ok'] is False, 'current_worker_range_accepted'
            assert checked['reason'] == 'incomplete_record'
            path = gate.paths.inbox / f'{request_id}.json'
            write_json_file_atomic(path, faulty)
            before = path.read_bytes()
            assert closeout.unroutable_reason(gate.paths, faulty, gate=gate) == ''
            assert gate.try_close_unroutable_pending(path) is None
            decision = gate.admit(path, (owner, session_scope_for_request(faulty)), AdmissionLimits(8, 50))
            assert decision == {'status': 'blocked', 'reason': 'reservation_missing'}, decision
            assert path.read_bytes() == before
            assert not reservation_file(gate.paths, request_id).exists()
            assert not (gate.paths.terminal / path.name).exists()
            assert not (gate.paths.responses / path.name).exists()
        _standalone_cleanup(paths)
        with _gate(agent, paths, count, 'range-next') as successor:
            result = _reconcile(successor, {request_id: faulty})
            assert result == [{'request_id': request_id, 'status': 'expired',
                               'error_code': 'GATEWAY_REQUEST_ROUTE_RETIRED',
                               'reason': 'stale_generation_route'}]
            assert not list(paths.inbox.glob('*.json'))


# LLM: Every supported worker count gets a same-generation-only numeric W fault, before any rollover.
# 函数用途: 防止旧代失配遮蔽当前代的越界路由拒绝。
@pytest.mark.parametrize('count', [1, 2, 4])
def test_current_generation_worker_range_is_rejected_before_rollover(tmp_path, count):
    _current_worker_range_scenario(tmp_path, count)


# LLM: Omit only the current W-range validation outcome; the old-generation closeout code stays real.
# 函数用途: 证明新越界观察点不能依赖之后的旧代失配继续假绿。
def test_current_worker_range_falsifies_validator_bypass(tmp_path, monkeypatch):
    original = routes.validate_published_route

    # LLM: The valid control and any different generation still call the original validator.
    # 函数用途: 只给当前代越界样本注入错误放行。
    def accept_out_of_range(payload, count, generation):
        route = payload.get('route', {})
        worker = route.get('worker')
        if (route.get('c_run_id') == generation and type(worker) is int and worker >= count):
            return {'ok': True}
        return original(payload, count, generation)

    monkeypatch.setattr(routes, 'validate_published_route', accept_out_of_range)
    with pytest.raises(AssertionError, match='current_worker_range_accepted'):
        _current_worker_range_scenario(tmp_path, 2)
