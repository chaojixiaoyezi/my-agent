"""Wave 3 consolidated tests (test_consolidated_c10_g2.py).

Merged sources:
- test_r9_model_result_consumer_lifetime.py: 结果consumer退出与worker发表精确交错，不靠GC丢原异常或假签物理退出。
- test_r9_tui_stream_bootstrap_wire.py: 真实瘦客户端建档、模型菜单与状态只读往返，仅隔离echo执行，无外部模型调用。
- test_rework_r1_actor_handoff.py: 真实队列交接只释放已完成的精确 actor 身份，不把让出执行当成终态。
- test_rework_r3_plugin_execution_scope.py: R3-R2: refuse unsupported plugin execution, including durable legacy input.
- test_rework_r4_background_retirement.py: R4-02：原业务入口/调度器/输入账本的取消及退休回归，只控制外部模型。
- test_rework_r4_model_worker_nonstart.py: R4-R1：真实 CPython 启动拒绝不能遗留不存在 worker 的保留权。
- test_rework_r5_delivery_claim_shutdown.py: R5-R1：一次最终落账 EIO 只留下原 wake 重投责任，不遗留执行片。
"""

import errno
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.agent_core.runtime.guidance import (
    has_pending_request_guidance,
    inject_pending_guidance,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
from agent_py_agent.agent.concurrency import interrupt
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFailureParams,
    ModelCallLedger,
    model_call_drain_snapshot,
)
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentScheduler,
    background_claim,
    wake_attempt_tracking,
)
from agent_py_agent.agent.conversation import (
    background_shutdown as shutdown,
)
from agent_py_agent.agent.conversation import (
    conversation_actor as actor,
)
from agent_py_agent.agent.conversation.background_delivery import cached_owner_delivery
from agent_py_agent.agent.conversation.background_shutdown import BackgroundShutdown
from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.models import ChannelBinding
from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime
from agent_py_agent.agent.conversation.session_task_dispatch import (
    SessionTaskDispatchPlan,
    dispatch_session_task,
)
from agent_py_agent.agent.conversation.session_task_report import (
    report_task_result,
    result_report_key,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.wake_poison import WAKE_CLAIM_YIELDED
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
from agent_py_agent.agent.gateway_parts import (
    control_service as control,
)
from agent_py_agent.agent.gateway_parts import (
    recovery,
)
from agent_py_agent.agent.gateway_parts import (
    request_worker as worker,
)
from agent_py_agent.agent.gateway_parts.io import read_json_file, update_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import (
    GatewayArchiveDisposition,
    GatewayArchiveResult,
    ensure_gateway_folders,
)
from agent_py_agent.agent.gateway_parts.request_binding import GatewayActiveTurnTransition
from agent_py_agent.agent.gateway_parts.request_client import GatewayAskParams, submit_gateway_ask
from agent_py_agent.agent.plugin_host import composition
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space import owner_epoch
from agent_py_agent.tests import test_s2w2_tui_network_mode as network_tests
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_background_owner_delivery_commit import (
    _ScriptedBackend,
    _ScriptedDelivery,
)
from agent_py_agent.tests.test_bounded_call_error_origin import held_call
from agent_py_agent.tests.test_business_v12_f3_agents import (
    _service,
)
from agent_py_agent.tests.test_business_v12_f3_agents import (
    business_host as business_host,
)
from agent_py_agent.tests.test_mega_background_lifecycle_controls import (
    edit_task,
    eventually_retired,
    native_cancellation_case,
    ready_index,
)
from agent_py_agent.tests.test_mega_business_plugin_compatibility import scoped_authorized
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_call,
)
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)
from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
from agent_py_agent.tests.test_rework_r3_model_worker_cancellation import (
    _guard_request,
    _guard_state,
    _retentions,
    _started_call,
)

# ==========================================================================
# Source: agent_py_agent/tests/test_r9_model_result_consumer_lifetime.py
# 结果consumer退出与worker发表精确交错，不靠GC丢原异常或假签物理退出。
# ==========================================================================

@pytest.mark.parametrize('publication', ['after_exit', 'before_exit'])
def test_abandoned_result_releases_exception_request_without_gc(tmp_path, publication):
    code = r'''
import gc,threading,weakref,sys
from types import SimpleNamespace
from agent_py_agent.agent.agent_core import tool_model_generation as generation
class Payload:
    def __init__(self):self.body=bytearray(512*1024)
class Token:
    def release(self):released.set()
class Ledger:
    def retain_call(self,*args):return Token()
release=threading.Event();published=threading.Event();released=threading.Event();workers=[];queues=[]
publication=sys.argv[1]
def provider(request,*args):
    assert release.wait(3)
    raise RuntimeError('synthetic late failure')
def wait(request,state,results,worker,*args,**kwargs):
    workers.append(worker);queues.append(results)
    assert not released.is_set(), 'caller不能代worker释放原保留权'
    if publication=='before_exit':
        release.set()
        assert published.wait(3)
    return None
# queue.put返回后才证明发表完毕；不靠sleep或轮询队列等待。
class ObservedQueue(generation._GenerationResultQueue):
    def put(self,*args,**kwargs):
        super().put(*args,**kwargs)
        published.set()
generation._GenerationResultQueue=ObservedQueue
generation._effective_model_request_timeout_seconds=lambda *args,**kwargs:1
generation._generate_backend_response=provider
generation._wait_for_generation_result=wait
payload=Payload();reference=weakref.ref(payload)
request=SimpleNamespace(agent=SimpleNamespace(),prompt='',payload=payload)
state=SimpleNamespace(ledger=Ledger(),call_id='synthetic')
gc.disable()
generation._generate_through_wall_guard(request,state)
del payload,request,state
release.set();workers[0].join(3)
assert not workers[0].is_alive() and released.is_set()
assert queues[0].empty(), 'consumer退出前已入队或之后迟到的异常均不可滞留'
workers.clear();queues.clear()
assert reference() is None, 'worker退出后仍由无人接收的异常环保留请求'
'''
    home = tmp_path / 'home'
    home.mkdir()
    completed = subprocess.run([sys.executable, '-c', code, publication], capture_output=True, text=True,
        timeout=15, env={**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(home)})
    assert completed.returncode == 0, completed.stderr


def test_consumed_provider_failure_preserves_original_exception_and_traceback(monkeypatch):
    released = threading.Event()
    workers = []
    error = ValueError('synthetic provider error')
    class Token:
        def release(self):
            released.set()
    class Ledger:
        def retain_call(self, call_id):
            return Token()
    def provider(*args):
        workers.append(threading.current_thread())
        raise error
    monkeypatch.setattr(generation, '_effective_model_request_timeout_seconds', lambda *args, **kwargs: 2)
    monkeypatch.setattr(generation, '_generate_backend_response', provider)
    request = SimpleNamespace(agent=SimpleNamespace(backend=None), prompt='')
    state = SimpleNamespace(ledger=Ledger(), call_id='synthetic')
    with pytest.raises(ValueError) as caught:
        generation._generate_through_wall_guard(request, state)
    assert caught.value is error
    frames, trace = [], caught.value.__traceback__
    while trace is not None:
        frames.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert 'provider' in frames and '_target' in frames
    workers[0].join(3)
    assert not workers[0].is_alive() and released.is_set()



# ==========================================================================
# Source: agent_py_agent/tests/test_r9_tui_stream_bootstrap_wire.py
# 真实瘦客户端建档、模型菜单与状态只读往返，仅隔离echo执行，无外部模型调用。
# ==========================================================================

startup_gateway = network_tests.startup_gateway
_startup_args = network_tests._startup_args


@pytest.mark.parametrize('first', ['models', 'control_model', 'permissions'])
def test_tui_first_metadata_thread_is_stream(startup_gateway, first, monkeypatch):
    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )
    from agent_py_agent.cli.chat_parts.tui_model_menu import _request_data_sync
    class Provider(EchoBackend):
        def generate(self, *args, **kwargs):
            return ModelResponse(text='synthetic complete reply', backend='echo')
    startup_gateway.host.backend = Provider()
    from agent_py_agent.cli.parser import build_parser
    workspace = startup_gateway.client_home.parent / 'new-project'
    workspace.mkdir(parents=True)
    arguments = build_parser().parse_args(['--config', str(startup_gateway.config),
        'chat', '--gateway', '--dir', str(workspace)])
    client = make_gateway_chat_client(arguments)
    session = 'fresh-' + first
    execution = ChatControlExecution(client, True, ChatControlState(False, 0, '', 0, session), tui_streaming=True)
    if first == 'control_model':
        assert execute_chat_control(execution, parse_conversation_control('/model')).ok
    elif first == 'permissions':
        assert client.request_permissions(session_id=session, operation='get')['ok']
    result = _request_data_sync(client, session, 'list', {})
    assert result['ok'], result
    thread_id = result.get('thread_id') or result.get('model_execution', {}).get('thread_id')
    store = startup_gateway.host.conversation_store
    thread = store.threads.load(thread_id) if thread_id else store.threads.resolve(channel='chat', channel_conversation_id=session, channel_user_id='local-agent')
    assert thread is not None, result
    assert thread.verbose_level == 'stream', thread.to_dict()
    assert thread.metadata.get('verbose_level_source') == 'tui_default'
    status = execute_chat_control(execution, parse_conversation_control('/status'))
    assert status.ok
    assert 'stream' in status.message, status.message
    from agent_py_agent.agent.gateway_parts import request_execution
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_requests
    from agent_py_agent.cli.chat_parts.gateway_client import ChatRequestContent, submit_chat_request
    levels = []
    original_level = request_execution._set_gateway_verbose_level
    def observe_level(on_chunk, level, *args, **kwargs):
        levels.append(level)
        return original_level(on_chunk, level, *args, **kwargs)
    monkeypatch.setattr(request_execution, '_set_gateway_verbose_level', observe_level)
    request_id, _chunks, _response = submit_chat_request(gateway_paths(client),
        ChatRequestContent('synthetic echo request', [], [], False, False, False,
                           chat_session_id=session, tui_streaming=True), client)
    assert _process_gateway_requests(startup_gateway.host, startup_gateway.paths, worker_id='wire-test') == 1
    from agent_py_agent.cli.chat_parts.gateway_client import network_gateway_observer
    observer = network_gateway_observer(client)
    code, response = observer.request_json('GET', '/result/' + request_id, total_deadline_seconds=3)
    assert code == 200 and response.get('ok'), (response.get('error_code'), response.get('error'), response.get('response'))
    assert levels == ['stream'], '真实请求应将同代stream交给原过程输出writer'
    current = store.threads.load(thread.thread_id)
    assert current.verbose_level == 'stream' and current.metadata['verbose_level_source'] == 'tui_default'



def test_control_receipt_freezes_display_capability_and_preserves_legacy_replay(startup_gateway):
    import json
    from dataclasses import replace

    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        GatewayControlOperationReceipt,
        _receipt_scope,
        control_input_matches_receipt,
        gateway_control_operation_identity,
        gateway_control_operation_path,
    )
    from agent_py_agent.agent.runtime_errors import DataCorruptionError
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )

    client = make_gateway_chat_client(_startup_args(startup_gateway))
    execution = ChatControlExecution(client, True, ChatControlState(False, 0, '', 0, 'receipt'), tui_streaming=True)
    command = parse_conversation_control('/status')
    first = execute_chat_control(execution, command, message_id='tui-display')
    assert first.ok and first.operation_id
    path = gateway_control_operation_path(startup_gateway.paths, first.operation_id)
    payload = json.loads(path.read_text())
    receipt = GatewayControlOperationReceipt.from_dict(payload)
    assert receipt.client_tui_streaming and _receipt_scope(receipt).metadata['client_capabilities'] == {'tui_streaming': True}
    assert execute_chat_control(execution, command, message_id='tui-display').operation_id == first.operation_id
    for replacement in (False, None, 1, 'true', {}):
        with pytest.raises(DataCorruptionError):
            GatewayControlOperationReceipt.from_dict({**payload, 'client_tui_streaming': replacement})
    changed = dict(payload)
    changed.pop('client_tui_streaming')
    with pytest.raises(DataCorruptionError):
        GatewayControlOperationReceipt.from_dict(changed)
    old_execution = replace(execution, tui_streaming=False)
    legacy = execute_chat_control(old_execution, command, message_id='legacy-display')
    assert legacy.ok
    old_payload = json.loads(gateway_control_operation_path(startup_gateway.paths, legacy.operation_id).read_text())
    assert 'client_tui_streaming' not in old_payload
    old_receipt = GatewayControlOperationReceipt.from_dict(old_payload)
    assert old_receipt.to_dict() == old_payload and not old_receipt.client_tui_streaming
    assert 'client_capabilities' not in _receipt_scope(old_receipt).metadata
    assert execute_chat_control(old_execution, command, message_id='legacy-display').operation_id == legacy.operation_id
    replay = execute_chat_control(execution, command, message_id='legacy-display')
    assert replay.ok and replay.operation_id == legacy.operation_id
    assert json.loads(gateway_control_operation_path(startup_gateway.paths, legacy.operation_id).read_text()) == old_payload
    assert not execute_chat_control(execution, parse_conversation_control('/model'), message_id='legacy-display').ok
    original_scope = _receipt_scope(old_receipt)
    retry_scope = replace(original_scope, metadata={**original_scope.metadata, 'client_capabilities': {'tui_streaming': True}})
    for key, value in (('expected_turn_id', 'different-turn'), ('target_control_message_id', 'different-target'),
                       ('expected_execution', {'thread_id': 'different-thread'})):
        changed_scope = replace(retry_scope, metadata={**retry_scope.metadata, key: value})
        _, changed_digest = gateway_control_operation_identity(changed_scope, '/status')
        assert not control_input_matches_receipt(old_receipt, changed_scope, '/status', changed_digest)
    changed_scope = replace(retry_scope, workspace={'cwd': '/different/workspace', 'roots': []})
    _, changed_digest = gateway_control_operation_identity(changed_scope, '/status')
    assert not control_input_matches_receipt(old_receipt, changed_scope, '/status', changed_digest)



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r1_actor_handoff.py
# 真实队列交接只释放已完成的精确 actor 身份，不把让出执行当成终态。
# ==========================================================================

@pytest.fixture
def lane(tmp_path, monkeypatch, request):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    ensure_gateway_folders(paths)
    agent = SimpleNamespace(config=SimpleNamespace(
        conversation_actor_workers=getattr(request, "param", 1), owner_epoch_fence_enabled=False,
    ), conversation_store=None)
    scheduler = worker.gateway_conversation_scheduler(paths, agent)
    monkeypatch.setattr(worker, "admission", worker.GatewayAdmission())
    monkeypatch.setattr(worker, "_CLAIM_EPOCHS", {})
    state = SimpleNamespace(paths=paths, agent=agent, scheduler=scheduler, root=tmp_path)
    try:
        yield state
    finally:
        if scheduler is not None:
            scheduler.close(wait=True)


def enqueue(lane):
    return submit_gateway_ask(lane.paths, params=GatewayAskParams("work", chat_session_id="conversation"))


def dispatch(lane):
    submitted = []
    count = worker.dispatch_pending_requests(
        lane.paths, worker.AdmissionLimits(5, 5), lambda *args: submitted.append(args),
    )
    assert count == 1
    claim, user, key = submitted[0]
    return claim, user, key


def drain(lane, key):
    lane.scheduler.post(actor.TurnFinished("owner", key, "barrier")).result(timeout=5)


def bind(lane, claim, *, epoch=7, provisional=False):
    payload = read_json_file(claim.path)
    key = worker.request_conversation_key(payload) or claim.request_id
    binding = (None, epoch, SimpleNamespace(owner_epoch_fence_enabled=False))
    exact = worker._claim_epoch_key(claim.path, claim.request_id, claim.execution_attempt_id)
    other = worker._claim_epoch_key(claim.path, claim.request_id, "later-attempt")
    worker._CLAIM_EPOCHS[exact] = binding
    worker._CLAIM_EPOCHS[other] = binding
    if not provisional:
        assert lane.scheduler.sequencer.decide(actor.Admit(
            worker.request_user_key(payload), key, claim.request_id, claim.execution_attempt_id, epoch,
        )).action == "admit"
    return payload, key, exact, other


def finish(lane, claim, response):
    worker._finish_claimed_gateway_request(
        lane.paths, claim.path, claim.request_id, response,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )


def requeue(lane, claim, kind, monkeypatch):
    if kind == "owner":
        return worker._requeue_owner_capacity(lane.paths, claim.path, read_json_file(claim.path))
    response = {"id": claim.request_id, "status": "failed"}
    if kind == "provider":
        response["provider_transient_resume"] = {"resume_count": 1, "error_code": "PROVIDER_TIMEOUT"}
    else:
        from agent_py_agent.agent.gateway_parts import (
            multiprocess_background_execution as background,
        )
        from agent_py_agent.agent.gateway_parts.recovery import requeue_current_processing_request

        # 只替换后台消费者身份接缝；实际 T/lease CAS、写 pending 和移动仍用生产入口。
        def pending(*_):
            return requeue_current_processing_request(
                lane.paths, claim,
                GatewayActiveTurnTransition(claim.path, claim.request_id, claim.execution_attempt_id),
                not_before_at=worker.time.time() + 30,
            )

        monkeypatch.setattr(background, "requeue_pending_background_request", pending)
        response.update(kind="background", background_result={"outcome": "pending"})
    finish(lane, claim, response)
    return True


def test_dispatch_claim_early_stop_terminal_allows_next_request(lane, monkeypatch):
    rid, _, _ = enqueue(lane)
    claim, user, key = dispatch(lane)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (rid, "", 0)
    lane.scheduler.sequencer.decide(actor.Stop(user, key, rid))
    update_json_file_atomic(claim.path, lambda payload: dict(payload, cancel_requested=True))
    handled = []

    def interrupted(*_, **kwargs):
        handled.append(kwargs["refresh_lease"])
        return {"id": rid, "status": "interrupted", "ok": False}

    monkeypatch.setattr(worker, "_handle_gateway_request", interrupted)
    assert worker._process_claimed_gateway_request_path(lane.agent, lane.paths, claim.path, "worker", claim=claim)
    drain(lane, key)
    assert handled == [False]
    assert read_json_file(lane.paths.terminal / claim.name)["terminal_response"]["status"] == "interrupted"
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert rid not in lane.scheduler.sequencer._box(key).stopped
    worker.admission.release(user, conversation_key=key)
    next_id, _, _ = enqueue(lane)
    following, _, _ = dispatch(lane)
    assert following.request_id == next_id


@pytest.mark.parametrize("provisional", [False, True])
def test_terminal_cleans_only_its_claim_binding(lane, provisional):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, other = bind(lane, claim, provisional=provisional)
    finish(lane, claim, {"id": claim.request_id, "status": "done", "ok": True})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("kind", ["provider", "owner", "background"])
@pytest.mark.parametrize("provisional", [False, True])
def test_successful_requeue_releases_actor_and_can_be_reclaimed(lane, monkeypatch, kind, provisional):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, other = bind(lane, claim, provisional=provisional)
    assert requeue(lane, claim, kind, monkeypatch)
    drain(lane, key)
    assert not claim.path.exists()
    pending = lane.paths.inbox / claim.name
    assert read_json_file(pending)["status"] == "pending"
    assert not (lane.paths.terminal / claim.name).exists()
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS
    worker.admission.release(user, conversation_key=key)
    update_json_file_atomic(pending, lambda payload: dict(payload, not_before_at=0))
    following, _, _ = dispatch(lane)
    assert following.request_id == claim.request_id
    assert following.execution_attempt_id != claim.execution_attempt_id


@pytest.mark.parametrize("target", ["new_attempt", "new_epoch", "other_request", "nonzero_provisional"])
def test_late_terminal_does_not_clear_a_different_active_tuple(lane, target):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, other = bind(lane, claim)
    previous = (claim.request_id, claim.execution_attempt_id, 7)
    targets = {
        "new_attempt": (claim.request_id, "later-attempt", 7),
        "new_epoch": (claim.request_id, claim.execution_attempt_id, 8),
        "other_request": ("other-request", "", 0),
        "nonzero_provisional": (claim.request_id, "", 8),
    }
    current = targets[target]
    assert lane.scheduler.sequencer.decide(actor.Recover(
        user, key, *current, exit_proof=True, previous_attempt=previous,
    )).action == "admit"
    finish(lane, claim, {"id": claim.request_id, "status": "done"})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == current
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("disposition", [
    GatewayArchiveDisposition.STALE_CLAIM, GatewayArchiveDisposition.CONFLICT,
    GatewayArchiveDisposition.RETRYABLE_IO,
])
def test_uncommitted_terminal_preserves_actor_binding_and_stop(lane, monkeypatch, disposition):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    lane.scheduler.sequencer.decide(actor.Stop(user, key, claim.request_id))
    monkeypatch.setattr(worker, "archive_request", lambda *_args, **_kwargs: GatewayArchiveResult(disposition))
    before = claim.path.read_bytes()
    finish(lane, claim, {"id": claim.request_id, "status": "done"})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert claim.request_id in lane.scheduler.sequencer._box(key).stopped


@pytest.mark.parametrize("kind", ["provider", "owner", "background"])
def test_failed_requeue_keeps_its_actor_and_binding(lane, monkeypatch, kind):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    original = Path.replace

    def fail_move(path, target):
        if path == claim.path:
            raise OSError("injected queue move failure")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", fail_move)
    monkeypatch.setattr(worker, "archive_request", lambda *_args, **_kwargs:
                        GatewayArchiveResult(GatewayArchiveDisposition.RETRYABLE_IO))
    if kind == "provider":
        requeue(lane, claim, kind, monkeypatch)
    else:
        with pytest.raises(OSError, match="queue move failure"):
            requeue(lane, claim, kind, monkeypatch)
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.exists()
    assert not (lane.paths.terminal / claim.name).exists()


def test_restart_resume_retains_original_responsibility(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    before = claim.path.read_bytes()
    finish(lane, claim, {"id": claim.request_id, "restart_resume": {"cause": "shutdown"}})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before


def test_changed_claim_rejects_old_completion_without_clearing_active(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    update_json_file_atomic(claim.path, lambda payload: dict(
        payload, execution_attempt_id="replacement", lease_epoch=claim.lease_epoch + 1,
    ))
    before = claim.path.read_bytes()
    finish(lane, claim, {"id": claim.request_id, "status": "done"})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert not (lane.paths.terminal / claim.name).exists()


def test_stale_owner_epoch_keeps_active_and_claim_binding(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    repo = RuntimeRepository(lane.root / "epoch.db")
    old = owner_epoch.activate_owner_epoch(repo)
    _, key, exact, _ = bind(lane, claim, epoch=old)
    worker._CLAIM_EPOCHS[exact] = (repo, old, SimpleNamespace(owner_epoch_fence_enabled=True))
    owner_epoch.activate_owner_epoch(repo)
    before = claim.path.read_bytes()
    finish(lane, claim, {"id": claim.request_id, "status": "done"})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, old)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert not (lane.paths.terminal / claim.name).exists()


def test_recorded_unknown_never_releases_actor_or_claim(lane):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    error = BoundedCallExitUnconfirmedError("original-call", "worker_exit_unknown")
    observation = {
        "schema_version": "gateway_execution_outcome.v1", "status": "unknown",
        "request_id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id,
        "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner,
        "phase": "bounded_call_exit", "operation_entered": None,
        "error_code": error.error_code, "call_id": error.call_id, "reason": error.reason,
    }
    update_json_file_atomic(claim.path, lambda payload: dict(payload, execution_outcome=observation))
    before = claim.path.read_bytes()
    worker.terminalize_unhandled_claimed_gateway_request(
        lane.paths, claim.path, error,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert not (lane.paths.terminal / claim.name).exists()


def test_owner_capacity_process_notifies_only_after_transition_exit(lane, monkeypatch):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim, provisional=True)
    entered = []
    notifications = []
    original_scope = GatewayActiveTurnTransition.scope
    original_post = lane.scheduler.post

    @contextmanager
    def scope(transition, *args):
        with original_scope(transition, *args):
            entered.append(True)
            try:
                yield
            finally:
                entered.pop()

    def post(command, **kwargs):
        assert not entered, "actor notification must not run inside the original transition"
        notifications.append(command)
        return original_post(command, **kwargs)

    def full(*_):
        raise worker.PoolCapacityExceededError("capacity")

    monkeypatch.setattr(worker, "_resolve_request_agent", full)
    monkeypatch.setattr(GatewayActiveTurnTransition, "scope", scope)
    monkeypatch.setattr(lane.scheduler, "post", post)
    assert worker._process_claimed_gateway_request_path(lane.agent, lane.paths, claim.path, "worker", claim=claim)
    drain(lane, key)
    assert any(command.request_id == claim.request_id for command in notifications)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("kind", ["provider", "owner", "background"])
@pytest.mark.parametrize("stop_at", ["before_move", "after_move"])
def test_stop_interleaved_with_requeue_preserves_tombstone(lane, monkeypatch, kind, stop_at):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    ready, release = threading.Event(), threading.Event()
    original = Path.replace

    def move(path, target):
        if path != claim.path:
            return original(path, target)
        if stop_at == "after_move":
            result = original(path, target)
        ready.set()
        assert release.wait(5)
        return result if stop_at == "after_move" else original(path, target)

    monkeypatch.setattr(Path, "replace", move)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(requeue, lane, claim, kind, monkeypatch)
        try:
            assert ready.wait(5)
            assert lane.scheduler.sequencer.decide(actor.Stop(user, key, claim.request_id)).action == "stop"
        finally:
            release.set()
        assert pending.result(timeout=5)
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert lane.scheduler.sequencer.decide(actor.Admit(user, key, claim.request_id, "later", 7)).action == "rejected"
    assert lane.scheduler.sequencer.decide(actor.Admit(user, key, "next-request", "next", 7)).action == "admit"
    assert not (lane.paths.terminal / claim.name).exists()


def test_owner_capacity_changed_claim_cannot_release_reservation(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim, provisional=True)
    update_json_file_atomic(claim.path, lambda current: dict(current, lease_owner="changed"))
    before = claim.path.read_bytes()
    with pytest.raises(DataCorruptionError, match="claim changed"):
        worker._requeue_owner_capacity(lane.paths, claim.path, payload)
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, "", 0)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before


@pytest.mark.parametrize("lane", [0], indirect=True)
@pytest.mark.parametrize("kind", ["provider", "background"])
def test_disabled_actor_requeue_then_terminal_leaves_no_request_bindings(lane, monkeypatch, kind):
    assert lane.scheduler is None
    assert lane.agent.config.owner_epoch_fence_enabled is False
    lane.agent.subagents = SimpleNamespace(runtime_db=RuntimeRepository(lane.root / "disabled-epoch.db"))
    rid, _, _ = enqueue(lane)
    claim, user, key = dispatch(lane)
    binding = worker._stamp_claim_owner_epoch(
        lane.agent, claim.path, read_json_file(claim.path), claim.execution_attempt_id,
    )
    exact = worker._claim_epoch_key(claim.path, rid, claim.execution_attempt_id)
    unrelated = worker._claim_epoch_key(lane.paths.processing / "unrelated.json", "unrelated", "other-attempt")
    worker._CLAIM_EPOCHS[unrelated] = binding
    assert exact in worker._CLAIM_EPOCHS
    assert requeue(lane, claim, kind, monkeypatch)
    assert exact not in worker._CLAIM_EPOCHS
    pending = lane.paths.inbox / claim.name
    assert read_json_file(pending)["status"] == "pending"
    assert not (lane.paths.terminal / claim.name).exists()
    worker.admission.release(user, conversation_key=key)
    update_json_file_atomic(pending, lambda payload: dict(payload, not_before_at=0))
    following, _, _ = dispatch(lane)
    assert following.execution_attempt_id != claim.execution_attempt_id
    worker._stamp_claim_owner_epoch(
        lane.agent, following.path, read_json_file(following.path), following.execution_attempt_id,
    )
    assert worker._claim_epoch_key(following.path, rid, following.execution_attempt_id) in worker._CLAIM_EPOCHS
    finish(lane, following, {"id": rid, "status": "done", "ok": True})
    assert read_json_file(lane.paths.terminal / following.name)["terminal_response"]["status"] == "done"
    assert [entry for entry in worker._CLAIM_EPOCHS if entry[2] == rid] == []
    assert unrelated in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [0], indirect=True)
@pytest.mark.parametrize("outcome", ["unknown", "uncommitted", "restart_resume"])
def test_disabled_actor_unfinished_exits_keep_claim_binding(lane, monkeypatch, outcome):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

    assert lane.scheduler is None
    assert lane.agent.config.owner_epoch_fence_enabled is False
    lane.agent.subagents = SimpleNamespace(runtime_db=RuntimeRepository(lane.root / "unfinished-epoch.db"))
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    worker._stamp_claim_owner_epoch(lane.agent, claim.path, read_json_file(claim.path), claim.execution_attempt_id)
    exact = worker._claim_epoch_key(claim.path, claim.request_id, claim.execution_attempt_id)
    before = claim.path.read_bytes()
    if outcome == "unknown":
        worker.terminalize_unhandled_claimed_gateway_request(
            lane.paths, claim.path, BoundedCallExitUnconfirmedError("unconfirmed-call", "worker_exit_unknown"),
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
        )
    else:
        response = {"id": claim.request_id, "status": "done"}
        if outcome == "restart_resume":
            response["restart_resume"] = {"cause": "shutdown"}
        else:
            monkeypatch.setattr(worker, "archive_request", lambda *_args, **_kwargs:
                                GatewayArchiveResult(GatewayArchiveDisposition.RETRYABLE_IO))
        finish(lane, claim, response)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert not (lane.paths.terminal / claim.name).exists()


def _resource_count():
    if os.name == "nt":
        import ctypes
        count = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    root = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    return len(os.listdir(root))


@contextmanager
def _no_resource_growth():
    before = _resource_count()
    try:
        yield
    finally:
        assert _resource_count() - before == 0, "request operation leaked descriptors/handles"


def _control_scope(payload):
    conversation = payload["conversation"]
    return control.GatewayControlScope(
        payload.get("user_id") or conversation["channel_user_id"],
        conversation["channel"], conversation["channel_conversation_id"],
    )


def _stop_entry(lane, record, scope, entry):
    if entry == "mark":
        return control._mark_request_stopping(record, scope)[0]
    return control._stop_live_window_request(lane.agent, lane.paths, record, scope, interrupt_only=True).ok


@pytest.mark.parametrize("lane", [4, 0], indirect=True)
@pytest.mark.parametrize("sealed", [False, True])
def test_projection_recovery_hands_off_exact_actor_after_one_io_error(lane, monkeypatch, sealed):
    enqueue(lane)
    claim, user, key = dispatch(lane)
    if lane.scheduler is not None:
        _, key, exact, other = bind(lane, claim)
    else:
        exact = worker._claim_epoch_key(claim.path, claim.request_id, claim.execution_attempt_id)
        other = worker._claim_epoch_key(claim.path, claim.request_id, "later-attempt")
        binding = (None, 7, lane.agent.config)
        worker._CLAIM_EPOCHS.update({exact: binding, other: binding})
    original_write = recovery.write_json_file_atomic
    failures = []

    def write(path, payload, **kwargs):
        if Path(path) == lane.paths.done / claim.name and not failures:
            failures.append(True)
            raise OSError("simulated done projection I/O error")
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(recovery, "write_json_file_atomic", write)
    response = {"id": claim.request_id, "status": "done", "ok": True, "output": "original answer"}
    with _no_resource_growth():
        before_failure_resources = _resource_count()
        if sealed:
            original_write(claim.path, recovery._terminal_gateway_request_payload(
                claim.request_id, read_json_file(claim.path), response,
            ))
            first = recovery.recover_gateway_processing_requests_report(lane.paths, agent=None)
            assert first.load_errors
        else:
            finish(lane, claim, response)
        assert _resource_count() == before_failure_resources
        assert failures == [True]
        assert exact in worker._CLAIM_EPOCHS
        canonical = lane.paths.terminal / claim.name
        before = canonical.read_bytes()
        repairs = [recovery.repair_gateway_terminal_projections(lane.paths, agent=None) for _ in range(2)]
        assert repairs == [(1, []), (0, [])]
        assert canonical.read_bytes() == before
        assert read_json_file(lane.paths.responses / claim.name) == response
        if lane.scheduler is not None:
            drain(lane, key)
            assert lane.scheduler.sequencer.snapshot(key)["active"] is None
        assert exact not in worker._CLAIM_EPOCHS
        assert other in worker._CLAIM_EPOCHS
        worker.admission.release(user, conversation_key=key)
        next_id, _, _ = enqueue(lane)
        following, _, _ = dispatch(lane)
        assert following.request_id == next_id


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("entry", ["mark", "live_window"])
def test_stale_stop_after_owner_requeue_does_not_block_readmission(lane, monkeypatch, entry):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    scope = _control_scope(payload)
    record = control._GatewayRequestRecord(claim.path, payload)
    selected, resume = threading.Event(), threading.Event()
    original_decision = control._control_command_decision

    def decide(paths, command_scope, kind, turn_id, **kwargs):
        if kind == "stop":
            selected.set()
            assert resume.wait(5)
        return original_decision(paths, command_scope, kind, turn_id, **kwargs)

    monkeypatch.setattr(control, "_control_command_decision", decide)
    with _no_resource_growth(), ThreadPoolExecutor(max_workers=1) as pool:
        pending_stop = pool.submit(_stop_entry, lane, record, scope, entry)
        try:
            assert selected.wait(5)
            assert worker._requeue_owner_capacity(lane.paths, claim.path, payload)
            drain(lane, key)
            assert exact not in worker._CLAIM_EPOCHS
            worker.admission.release(user, conversation_key=key)
            pending_path = lane.paths.inbox / claim.name
            before = pending_path.read_bytes()
        finally:
            resume.set()
        assert pending_stop.result(timeout=5) is False
        assert pending_path.read_bytes() == before
        assert not read_json_file(pending_path).get("cancel_requested")
        assert not (lane.paths.terminal / claim.name).exists()
        stopped = claim.request_id in lane.scheduler.sequencer._box(key).stopped
        submitted = []
        counts = [worker.dispatch_pending_requests(
            lane.paths, worker.AdmissionLimits(5, 5), lambda *args: submitted.append(args),
        ) for _ in range(3)]
        assert (stopped, counts) == (False, [1, 0, 0])
        assert len(submitted) == 1
        assert submitted[0][0].request_id == claim.request_id
        assert submitted[0][0].execution_attempt_id != claim.execution_attempt_id


@pytest.mark.parametrize("lane", [4], indirect=True)
def test_fresh_stop_binding_read_has_no_stop_side_effect(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    before = claim.path.read_bytes()
    active = lane.scheduler.sequencer.snapshot(key)["active"]
    with _no_resource_growth():
        assert control._fresh_stop_runtime_binding(
            control._GatewayRequestRecord(claim.path, payload), _control_scope(payload),
        ) == {}
    assert claim.path.read_bytes() == before
    assert lane.scheduler.sequencer.snapshot(key)["active"] == active
    assert lane.scheduler.sequencer.snapshot(key)["state"] == "running"
    assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
    assert exact in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("entry", ["mark", "live_window"])
def test_accepted_stop_publishes_only_after_durable_cancel(lane, monkeypatch, entry):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    original_write = control.update_json_file_atomic
    observed = []

    def write(path, updater, **kwargs):
        assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
        result = original_write(path, updater, **kwargs)
        observed.append(read_json_file(path)["cancel_requested"])
        assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
        return result

    monkeypatch.setattr(control, "update_json_file_atomic", write)
    with _no_resource_growth():
        assert _stop_entry(lane, control._GatewayRequestRecord(claim.path, payload), _control_scope(payload), entry)
    assert observed == [True]
    assert read_json_file(claim.path)["cancel_requested"] is True
    assert claim.request_id in lane.scheduler.sequencer._box(key).stopped
    assert lane.scheduler.sequencer.decide(actor.Admit(user, key, claim.request_id, "later", 7)).action == "rejected"
    assert exact in worker._CLAIM_EPOCHS
    with _no_resource_growth():
        finish(lane, claim, {"id": claim.request_id, "status": "interrupted", "ok": False})
        drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
    assert read_json_file(lane.paths.terminal / claim.name)["terminal_response"]["status"] == "interrupted"


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("outcome", ["requeued", "failed"])
@pytest.mark.parametrize("stopped", [False, True])
def test_verified_stale_processing_recovery_handoff(lane, outcome, stopped):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, other = bind(lane, claim)
    if stopped:
        lane.scheduler.sequencer.decide(actor.Stop(user, key, claim.request_id))
    context = recovery._RecoveryContext(
        worker.time.time() + 1000, 2 if outcome == "requeued" else 1, 1, False, None, 1,
    )
    with _no_resource_growth():
        report = recovery.recover_gateway_processing_requests_report(lane.paths, params=context)
        assert report.load_errors == []
        assert report.summary[outcome] == 1
        drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS
    assert not claim.path.exists()
    if outcome == "requeued":
        assert read_json_file(lane.paths.inbox / claim.name)["status"] == "pending"
        assert (claim.request_id in lane.scheduler.sequencer._box(key).stopped) is stopped
        assert not (lane.paths.terminal / claim.name).exists()
    else:
        assert read_json_file(lane.paths.terminal / claim.name)["terminal_response"]["status"] == "failed"
        assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped


@pytest.mark.parametrize("lane", [4], indirect=True)
def test_unproven_recovery_source_retains_actor_and_claim(lane):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_processing_recovery import (
        recover_stale_processing,
    )

    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    before = claim.path.read_bytes()
    context = recovery._RecoveryContext(worker.time.time(), 2, 1, False, lane.agent, 1, admission_gate=object())
    with _no_resource_growth(), pytest.raises(RequestExecutionOutcomeUncertainError):
        recover_stale_processing(lane.paths, claim.path, payload, context, recovery._settle_stale_processing)
    assert claim.path.read_bytes() == before
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("target", ["new_attempt", "new_epoch", "other_request"])
def test_terminal_recovery_keeps_a_newer_active_tuple(lane, target):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    payload, key, exact, other = bind(lane, claim)
    recovery.write_json_file_atomic(claim.path, recovery._terminal_gateway_request_payload(
        claim.request_id, payload, {"id": claim.request_id, "status": "done", "ok": True},
    ))
    targets = {
        "new_attempt": (claim.request_id, "later-attempt", 7),
        "new_epoch": (claim.request_id, claim.execution_attempt_id, 8),
        "other_request": ("other-request", "other-attempt", 7),
    }
    current = targets[target]
    assert lane.scheduler.sequencer.decide(actor.Recover(
        user, key, *current, exit_proof=True, previous_attempt=(claim.request_id, claim.execution_attempt_id, 7),
    )).action == "admit"
    with _no_resource_growth():
        report = recovery.recover_gateway_processing_requests_report(lane.paths, agent=None)
        assert report.load_errors == []
        assert report.summary["archived"] == 1
        drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == current
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("entry", ["mark", "live_window"])
@pytest.mark.parametrize("rejection", ["io", "new_attempt", "wrong_scope", "terminal"])
def test_rejected_stop_leaves_no_uncommitted_tombstone(lane, monkeypatch, entry, rejection):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    record, scope = control._GatewayRequestRecord(claim.path, payload), _control_scope(payload)
    if rejection == "io":
        def unavailable(*_args, **_kwargs):
            raise OSError("simulated cancellation write I/O error")
        monkeypatch.setattr(control, "update_json_file_atomic", unavailable)
    elif rejection == "new_attempt":
        update_json_file_atomic(claim.path, lambda current: dict(
            current, execution_attempt_id="later-attempt", lease_epoch=claim.lease_epoch + 1,
        ))
        assert lane.scheduler.sequencer.decide(actor.Recover(
            user, key, claim.request_id, "later-attempt", 8, exit_proof=True,
            previous_attempt=(claim.request_id, claim.execution_attempt_id, 7),
        )).action == "admit"
    elif rejection == "wrong_scope":
        update_json_file_atomic(claim.path, lambda current: dict(current, user_id="another-owner"))
    else:
        finish(lane, claim, {"id": claim.request_id, "status": "done", "ok": True})
        drain(lane, key)
    selected_path = lane.paths.terminal / claim.name if rejection == "terminal" else claim.path
    before = selected_path.read_bytes()
    active = lane.scheduler.sequencer.snapshot(key)["active"]
    with _no_resource_growth():
        assert _stop_entry(lane, record, scope, entry) is False
    assert selected_path.read_bytes() == before
    assert not read_json_file(selected_path).get("cancel_requested")
    assert lane.scheduler.sequencer.snapshot(key)["active"] == active
    assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
    assert (exact in worker._CLAIM_EPOCHS) is (rejection != "terminal")


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("entry", ["mark", "live_window"])
def test_committed_stop_contention_keeps_durable_cancel_authoritative(lane, monkeypatch, entry):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    held, release = threading.Event(), threading.Event()
    original_write = control.update_json_file_atomic
    original_decision = control._control_command_decision
    memory_results = []

    def decide(paths, scope, kind, request_id, **kwargs):
        result = original_decision(paths, scope, kind, request_id, **kwargs)
        if kind == "stop_committed":
            memory_results.append(result[2].action)
        return result

    def hold():
        with lane.scheduler.sequencer._lock:
            held.set()
            assert release.wait(5)

    with _no_resource_growth(), ThreadPoolExecutor(max_workers=1) as pool:
        waiting = []

        def write(path, updater, **kwargs):
            result = original_write(path, updater, **kwargs)
            waiting.append(pool.submit(hold))
            assert held.wait(5)
            return result

        monkeypatch.setattr(control, "update_json_file_atomic", write)
        monkeypatch.setattr(control, "_control_command_decision", decide)
        try:
            assert _stop_entry(lane, control._GatewayRequestRecord(claim.path, payload), _control_scope(payload), entry)
            assert read_json_file(claim.path)["cancel_requested"] is True
        finally:
            release.set()
        for future in waiting:
            future.result(timeout=5)
    assert memory_results == ["retry"]
    assert exact in worker._CLAIM_EPOCHS
    assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
    handled = []

    def interrupted(*_args, **kwargs):
        handled.append(kwargs["refresh_lease"])
        return {"id": claim.request_id, "status": "interrupted", "ok": False}

    monkeypatch.setattr(worker, "_handle_gateway_request", interrupted)
    with _no_resource_growth():
        assert worker._process_claimed_gateway_request_path(
            lane.agent, lane.paths, claim.path, "worker", claim=claim,
        )
        drain(lane, key)
    assert handled == [False]
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert read_json_file(lane.paths.terminal / claim.name)["terminal_response"]["status"] == "interrupted"


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("change", ["new_attempt", "new_epoch", "move_error"])
def test_unsuccessful_recovery_preserves_old_responsibility(lane, monkeypatch, capsys, change):
    from agent_py_agent.agent.gateway_parts.multiprocess_processing_recovery import (
        recover_stale_processing,
    )

    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, other = bind(lane, claim)
    lane.scheduler.sequencer.decide(actor.Stop(worker.request_user_key(payload), key, claim.request_id))
    context = recovery._RecoveryContext(worker.time.time() + 1000, 2, 1, False, None, 1)
    errors = []
    if change == "new_attempt":
        update_json_file_atomic(claim.path, lambda current: dict(current, execution_attempt_id="replacement"))
    elif change == "new_epoch":
        update_json_file_atomic(claim.path, lambda current: dict(current, lease_epoch=claim.lease_epoch + 1))
    else:
        original_replace = Path.replace

        def move(path, target):
            if path == claim.path:
                error = OSError("simulated recovery move I/O error")
                errors.append(error)
                raise error
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", move)
    with _no_resource_growth():
        assert recover_stale_processing(
            lane.paths, claim.path, payload, context, recovery._settle_stale_processing,
        ) == ""
    if change == "move_error":
        assert len(errors) == 1 and errors[0].__cause__ is None
        stderr = capsys.readouterr().err
        assert '"operation": "requeue_gateway_request"' in stderr
        assert '"category": "io"' in stderr
        assert str(errors[0]) in stderr
    assert claim.path.exists()
    assert not (lane.paths.terminal / claim.name).exists()
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert claim.request_id in lane.scheduler.sequencer._box(key).stopped
    assert exact in worker._CLAIM_EPOCHS and other in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("outcome", ["archived", "failed", "requeued"])
def test_recovery_notifies_actor_after_original_transition_exit(lane, monkeypatch, outcome):
    from agent_py_agent.agent.gateway_parts import multiprocess_processing_recovery as metadata

    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    if outcome == "archived":
        recovery.write_json_file_atomic(claim.path, recovery._terminal_gateway_request_payload(
            claim.request_id, payload, {"id": claim.request_id, "status": "done", "ok": True},
        ))
    entered, notifications, trace = [], [], []
    original_scope = metadata.gateway_turn_transition
    original_post = lane.scheduler.post

    @contextmanager
    def scope(*args, **kwargs):
        acquired = False
        try:
            with original_scope(*args, **kwargs):
                acquired = True
                entered.append(True)
                trace.append("enter")
                yield
        finally:
            if acquired:
                entered.pop()
                trace.append("exit")

    def post(command, **kwargs):
        assert not entered, "recovery must release T before notifying the actor"
        if command.request_id == claim.request_id:
            assert trace and trace[-1] == "exit", "the real recovery transition must have been observed"
            trace.append("notify")
            notifications.append(command)
        return original_post(command, **kwargs)

    monkeypatch.setattr(metadata, "gateway_turn_transition", scope)
    monkeypatch.setattr(lane.scheduler, "post", post)
    context = recovery._RecoveryContext(
        worker.time.time() + 1000, 2 if outcome == "requeued" else 1, 1, False, None, 1,
    )
    with _no_resource_growth():
        report = recovery.recover_gateway_processing_requests_report(lane.paths, params=context)
        assert report.load_errors == []
        assert report.summary[outcome] == 1
        drain(lane, key)
    assert len(notifications) == 1
    assert trace.count("enter") == trace.count("exit") > 0
    assert trace[trace.index("notify") - 1] == "exit"
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r3_plugin_execution_scope.py
# R3-R2: refuse unsupported plugin execution, including durable legacy input.
# ==========================================================================

DENIED_ACTIONS = {
    'background_agents.messages.send', 'tasks.submit', 'tasks.assign', 'tasks.follow_up',
    'workflows.run', 'workflow_runs.resume', 'subagents.messages.send',
    'background_agents.resume', 'background_agents.restore',
}
PAYLOADS = {
    'background_agents.messages.send': {'agent_id': 'unstarted-agent', 'text': 'work', 'input_kind': 'guidance'},
    'tasks.submit': {'space_id': 'unstarted-space', 'instruction': 'work'},
    'tasks.assign': {'agent_id': 'unstarted-agent', 'space_id': 'unstarted-space', 'instruction': 'work'},
    'tasks.follow_up': {'task_id': 'unstarted-task', 'instruction': 'work'},
    'workflows.run': {'workflow_ref': 'unstarted-workflow'},
    'workflow_runs.resume': {'workflow_run_id': 'unstarted-workflow-run'},
    'subagents.messages.send': {'run_id': 'unstarted-run', 'text': 'work'},
    'background_agents.resume': {'agent_id': 'unstarted-agent'},
    'background_agents.restore': {'agent_id': 'unstarted-agent'},
}


@pytest.mark.parametrize('action', sorted(DENIED_ACTIONS))
def test_plugin_execution_is_unready_http_direct_and_live_context(production_host, action):
    server, _, owner, *_ = production_host
    server.agent = SimpleNamespace(config=AgentConfig(business_spaces_enabled=True))
    backend = composition.business_port(server.plugin_host.composition.ports)
    if 'execute' in authorized_caps(action):
        production_call(production_host, 'users.local.create', {'user_id': 'alice'}, key='r2-create-alice-key', status=201)
    authorized = scoped_authorized(production_host, 'business.' + action,
        {**PAYLOADS[action], **({'expected_revision': 1} if action in {
            'background_agents.resume', 'background_agents.restore', 'workflow_runs.resume'} else {})},
        user='alice' if 'execute' in authorized_caps(action) else None, key='r2-refused-' + action.replace('.', '-'))
    before = _owner_tree(owner.root)
    assert backend.ready('business.' + action) is False
    with pytest.raises(HostAPIError) as direct:
        backend.call(authorized)
    assert (direct.value.code, direct.value.status, direct.value.outcome) == ('HOST_NOT_READY', 503, 'not_started')
    context, services = backend.context(authorized)
    assert not DENIED_ACTIONS.intersection(services.supported(context))
    with pytest.raises(HostAPIError) as projected:
        context.check(action, {'owner_id': context.owner_id})
    assert projected.value.code == 'HOST_NOT_READY'
    status, result = http_request(server, production_host[1], {
        'schema': 'plugin_host_call.v2', 'action': authorized.call.action,
        'payload': dict(authorized.call.payload), 'idempotency_key': 'r2-http-' + action.replace('.', '-'),
        **({'as_user': {'user_id': 'alice'}} if 'execute' in authorized_caps(action) else {})})
    assert status == 503 and result['error']['code'] == 'HOST_NOT_READY', result
    assert result['error']['outcome'] == 'not_started'
    assert _owner_tree(owner.root) == before


# LLM: 691c5345d 的代次锁侧车在首次读取既有库时建立，属于锁文件而非业务状态写入。
# 函数用途: 列出 owner 树中除代次锁侧车外的全部路径。
def _owner_tree(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob('*') if not path.name.endswith('.generation.lock'))


def authorized_caps(action):
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
    return business_action_spec(action).capabilities


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True,
        business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path / 'workspace')
    store, owner = agent.conversation_store, agent.home_paths.owner_id
    source = store.threads.create(owner_id=owner, owner_home=str(agent.home_paths.owner_home_dir))
    source = replace(source,
        metadata={'plugin_host_v2': {'installation_ref': 'original-installation', 'revision': 1, 'workspace_ref': 'default'}},
        channel_bindings=(ChannelBinding('plugin', 'original-installation', owner, owner, source.thread_id),))
    store.threads.write(source)
    target = store.threads.create(owner_id=owner, owner_home=str(agent.home_paths.owner_home_dir))
    calls = []
    native_run = agent.run

    def forbidden(*args, **kwargs):
        calls.append('model-admission')
        raise AssertionError('unsupported plugin work reached model admission')

    monkeypatch.setattr(agent, 'run', forbidden)
    return SimpleNamespace(agent=agent, store=store, owner=owner, source=source, target=target, calls=calls, native_run=native_run)


def queue_task(case):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    outcome = dispatch_session_task(case.agent,
        SessionTaskDispatchPlan(case.source.thread_id, case.target.thread_id, 'must not execute'),
        case.target, CapabilityConfig(), business_context_intent=BusinessContextIntent(case.owner, 'space-one', None, None),
        business_context_factory=lambda tid: BusinessExecutionContext(case.owner, 'space-one', None, tid,
            0, None, (), (), 'business-context.v1'))
    assert outcome.ok, outcome.output
    task = case.store.session_tasks.require(json.loads(outcome.output)['task_id'])
    signal = next(w for w in case.store.wakes.pending(limit=0) if w.metadata.get('session_task_id') == task.task_id)
    return task, signal


def run_wake(case, signal):
    return BackgroundMainAgentRuntime(agent=case.agent, store=case.store).run_once({
        'thread_id': signal.thread_id, 'task_id': signal.root_task_id, 'reason': signal.reason,
        'wake_signal': signal.to_dict()})


def test_queued_plugin_task_closes_original_body_and_result_without_model(legacy):
    task, signal = queue_task(legacy)
    report = run_wake(legacy, signal)
    current = legacy.store.session_tasks.require(task.task_id)
    assert current.status == 'failed' and current.failure_code == 'HOST_NOT_READY'
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    assert legacy.store.guidance.receipt(result_report_key(current)) is not None
    assert report.runtime_status == 'failed' and report.runtime_reason == 'HOST_NOT_READY'
    assert report.wake_handled and report.tool_call_count == 0 and legacy.calls == []


@pytest.mark.parametrize('target_type', ['thread', 'agent_run'])
def test_pending_plugin_instruction_is_rejected_before_claim_and_probe(legacy, target_type):
    target_id = legacy.target.thread_id if target_type == 'thread' else 'child-run'
    entry = legacy.store.guidance.append_once({'target_type': target_type, 'target_id': target_id,
        'message': 'plugin instruction', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.source.thread_id}},
        dedupe_key='plugin-instruction')
    params = SimpleNamespace(request_id='current-turn', run_id='child-run' if target_type == 'agent_run' else '', task_id='',
        context_scope='task_local' if target_type == 'agent_run' else 'default',
        task_attributes={'conversation_thread_id': legacy.target.thread_id, 'agent_thread_id': legacy.target.thread_id},
        tool_context=[], live_archive_state={})
    assert has_pending_request_guidance(legacy.agent, params) is False
    assert inject_pending_guidance(legacy.agent, params) is False
    assert legacy.store.guidance.receipt(entry.metadata['dedupe_key']).status == 'rejected'
    assert params.tool_context == [] and not getattr(params, 'active_turn_user_inputs', None)


def test_host_generated_terminal_result_from_plugin_thread_is_preserved(legacy):
    task, _signal = queue_task(legacy)
    # Reverse a separate canonical task so the report's source is a plugin thread.
    native = legacy.store.session_tasks.create(__import__(
        'agent_py_agent.agent.conversation.session_tasks', fromlist=['SessionTaskDraft']).SessionTaskDraft(
        legacy.target.thread_id, legacy.source.thread_id, 'previous native work'))
    legacy.store.session_tasks.advance(native.task_id, SessionTaskUpdate(status='accepted'))
    done = legacy.store.session_tasks.advance(native.task_id, SessionTaskUpdate(status='done', summary='historical result'))
    assert report_task_result(legacy.agent, done)
    params = SimpleNamespace(request_id='result-turn', task_id='', task_attributes={'conversation_thread_id': legacy.target.thread_id},
        tool_context=[], live_archive_state={}, tool_protocol_snapshot=make_test_protocol_snapshot())
    assert has_pending_request_guidance(legacy.agent, params)
    assert inject_pending_guidance(legacy.agent, params)
    assert legacy.store.guidance.receipt(result_report_key(done)).status == 'reserved'
    assert legacy.store.session_tasks.require(task.task_id).status == 'queued'


@pytest.mark.parametrize('failure', ['reject', 'advance', 'report'])
def test_interrupted_task_closeout_retries_only_bookkeeping(legacy, monkeypatch, failure):
    from agent_py_agent.agent.conversation import session_task_report
    task, signal = queue_task(legacy)
    if failure == 'report':
        service, method = session_task_report, 'report_task_result'
    elif failure == 'advance':
        service, method = legacy.store.session_tasks, 'advance'
    else:
        service, method = legacy.store.guidance, 'mark_status'
    original = getattr(service, method)
    remaining = [True]

    def interrupt(*args, **kwargs):
        if remaining[0]:
            remaining[0] = False
            if failure == 'report':
                assert original(*args, **kwargs)  # simulate a lost acknowledgement after durable publish
                return False
            raise OSError('injected closeout interruption')
        return original(*args, **kwargs)

    monkeypatch.setattr(service, method, interrupt)
    first = run_wake(legacy, signal)
    assert not first.wake_handled and legacy.calls == []
    second = run_wake(legacy, signal)
    assert second.wake_handled and legacy.calls == []
    current = legacy.store.session_tasks.require(task.task_id)
    assert current.status == 'failed' and current.failure_code == 'HOST_NOT_READY'
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    result = legacy.store.guidance.receipt(result_report_key(current))
    assert result is not None
    assert run_wake(legacy, signal).wake_handled
    assert legacy.store.guidance.receipt(result_report_key(current)).entry.guidance_id == result.entry.guidance_id


def test_only_exact_plugin_task_body_is_rejected(legacy):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    task, signal = queue_task(legacy)
    other = legacy.store.guidance.append_once({'target_type': 'thread', 'target_id': legacy.target.thread_id,
        'message': 'unrelated native input', 'sender': 'host'}, dedupe_key='unrelated-body')
    different = legacy.store.session_tasks.create(SessionTaskDraft(legacy.source.thread_id, legacy.target.thread_id, 'other'))
    assert run_wake(legacy, signal).wake_handled
    assert legacy.store.guidance.receipt(other.metadata['dedupe_key']).status == 'pending'
    assert legacy.store.session_tasks.require(different.task_id).status == 'queued'
    assert legacy.store.session_tasks.require(task.task_id).status == 'failed'


@pytest.mark.parametrize('state', ['pending', 'reserved', 'submitted', 'consumed'])
def test_old_plugin_message_wake_never_runs_and_preserves_uncertain_effects(legacy, state):
    entry = legacy.store.guidance.append_once({'target_type': 'thread', 'target_id': legacy.target.thread_id,
        'message': 'plugin instruction', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.source.thread_id}},
        dedupe_key='legacy-message')
    if state != 'pending':
        assert legacy.store.guidance.claim_for_turn(entry, expected_turn_id='old-turn', attempt_id='old-attempt')
        entry = legacy.store.guidance.receipt('legacy-message').entry
    if state in {'submitted', 'consumed'}:
        legacy.store.guidance.submissions.mark_submitted('old-turn', [entry],
            attempt_id='old-attempt', provider_call_id='original-provider-call')
    if state == 'consumed':
        legacy.store.guidance.acknowledgements.consume_submitted('old-turn', [entry], provider_call_id='original-provider-call')
    signal = legacy.store.wakes.raise_signal({'thread_id': legacy.target.thread_id, 'reason': 'session_message',
        'dedupe_key': 'legacy-message-wake', 'metadata': {'origin_kind': 'session_message',
            'origin_thread_id': legacy.source.thread_id, 'message_dedupe_key': 'legacy-message'}})
    report = run_wake(legacy, signal)
    assert legacy.calls == [] and report.tool_call_count == 0 and report.runtime_reason == 'HOST_NOT_READY'
    assert report.wake_handled is (state in {'pending', 'consumed'})
    assert legacy.store.guidance.receipt('legacy-message').status == ('rejected' if state == 'pending' else state)


@pytest.mark.parametrize('kind', ['metadata-only', 'channel-only', 'wrong-owner', 'wrong-installation'])
def test_malformed_positive_plugin_source_never_falls_back_to_native(legacy, kind):
    from agent_py_agent.agent.conversation.host_actor import (
        HostActorError,
        is_plugin_business_source_thread,
    )
    row = legacy.source
    if kind == 'metadata-only':
        row = replace(row, channel_bindings=())
    elif kind == 'channel-only':
        row = replace(row, metadata={})
    elif kind == 'wrong-owner':
        row = replace(row, owner_id='foreign-owner')
    else:
        row = replace(row, metadata={'plugin_host_v2': {'installation_ref': 'different-installation'}})
    legacy.store.threads.write(row)
    with pytest.raises(HostActorError):
        is_plugin_business_source_thread(legacy.store, row.thread_id, owner_id=legacy.owner)


def test_source_classifier_uses_canonical_facts_not_thread_prefix_or_text(legacy):
    from agent_py_agent.agent.conversation.host_actor import is_plugin_business_source_thread
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic
    fake = create_thread_atomic(legacy.store.threads, ConversationThread(thread_id='thread-plugin-business-fake',
        canonical_user_id=legacy.owner, owner_id=legacy.owner, title='plugin_host_v2 original-installation'))
    assert is_plugin_business_source_thread(legacy.store, fake.thread_id, owner_id=legacy.owner) is False
    assert is_plugin_business_source_thread(legacy.store, legacy.source.thread_id, owner_id=legacy.owner) is True


@pytest.mark.parametrize('field', ['task_run_id', 'conversation_request_id'])
def test_accepted_unknown_or_foreign_execution_is_not_overwritten(legacy, field):
    task, signal = queue_task(legacy)
    current = legacy.store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='accepted', **{field: 'original-live-execution'}))
    before = legacy.store.session_tasks._path(task.task_id).read_bytes()
    report = run_wake(legacy, signal)
    assert not report.wake_handled and legacy.calls == []
    assert legacy.store.session_tasks._path(task.task_id).read_bytes() == before
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'pending'
    assert legacy.store.session_tasks.require(task.task_id) == current


def test_accepted_reserved_body_requires_exact_recovery_claim_ownership(legacy):
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )
    task, signal = queue_task(legacy)
    legacy.store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='accepted', conversation_request_id=task.task_id))
    body = legacy.store.guidance.receipt(task.body_dedupe_key).entry
    assert legacy.store.guidance.claim_for_turn(body, expected_turn_id=task.task_id,
        attempt_id='original-attempt', owning_task_id=task.task_id)
    assert not run_wake(legacy, signal).wake_handled
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'reserved'
    old = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id, 'task_id': task.task_id, 'reason': 'session_task'})
    assert legacy.store.claims.finish({'thread_id': legacy.target.thread_id, 'claim_id': old['claim_id'], 'status': 'failed'})
    recovery = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id, 'reason': 'session_task'})
    with bind_claim_execution(ClaimExecutionBinding(legacy.store.claims, legacy.target.thread_id, recovery['claim_id'], '')):
        report = run_wake(legacy, signal)
    assert report.wake_handled and legacy.calls == []
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    assert legacy.store.session_tasks.require(task.task_id).status == 'failed'


def test_historical_terminal_task_result_and_execution_refs_are_preserved(legacy):
    task, signal = queue_task(legacy)
    legacy.store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='accepted', task_run_id='historical-run'))
    ended = legacy.store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='done', summary='actual historical result', result_refs=('artifact-old',)))
    before = legacy.store.session_tasks._path(task.task_id).read_bytes()
    report = run_wake(legacy, signal)
    assert report.wake_handled and report.task_status == 'done' and legacy.calls == []
    assert legacy.store.session_tasks._path(task.task_id).read_bytes() == before
    assert legacy.store.guidance.receipt(result_report_key(ended)).entry.metadata['session_task_status'] == 'done'


# LLM: 原生回合仍走完整Agent执行层；测试显式供应echo后端，不依赖未配置Home的自动模型回退。
# 函数用途: 确认插件停用边界不会误挡原生用户的后台执行。
def test_native_owner_background_execution_still_reaches_real_agent(legacy, monkeypatch):
    from agent_py_agent.agent.backends import get_backend

    legacy.agent.config.enable_tools = False
    legacy.agent.backend = get_backend('echo', legacy.agent.config)
    calls = []

    def native(*args, **kwargs):
        calls.append('native')
        return legacy.native_run(*args, **kwargs)

    monkeypatch.setattr(legacy.agent, 'run', native)
    report = BackgroundMainAgentRuntime(agent=legacy.agent, store=legacy.store).run_once({
        'thread_id': legacy.target.thread_id, 'reason': 'scheduled_progress_report'})
    assert calls == ['native'] and report.response and report.runtime_reason != 'HOST_NOT_READY'


def test_exact_wake_finishes_only_after_durable_task_result(legacy):
    import time

    from agent_py_agent.agent.conversation.runtime import _complete_wake_report
    task, signal = queue_task(legacy)
    unrelated = legacy.store.wakes.raise_signal({'thread_id': legacy.target.thread_id,
        'reason': 'session_message', 'dedupe_key': 'unrelated-wake'})
    report = run_wake(legacy, signal)
    scheduler = SimpleNamespace(store=legacy.store, _wake_retry_after={}, _quota_fallback_wakes=set(), scheduler_service=None,
        runtime=BackgroundMainAgentRuntime(agent=legacy.agent, store=legacy.store))
    assert _complete_wake_report(scheduler, signal, replace(report, wake_handled=False),
        lifecycle_reason='session_task', now=time.time()) is None
    assert legacy.store.wakes.pending_one(signal.wake_signal_id) is not None
    assert _complete_wake_report(scheduler, signal, report, lifecycle_reason='session_task', now=time.time()) is report
    assert legacy.store.wakes.pending_one(signal.wake_signal_id) is None
    assert legacy.store.wakes.pending_one(unrelated.wake_signal_id) is not None
    assert legacy.store.guidance.receipt(result_report_key(legacy.store.session_tasks.require(task.task_id))) is not None


@pytest.mark.parametrize('revoked', [False, True])
@pytest.mark.parametrize('production_host', [{'max_tool_calls': 1}], indirect=True)
def test_original_http_installation_legacy_dispatch_cannot_execute_even_if_revoked(production_host, revoked):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.tests.test_mega_business_plugin_compatibility import (
        revoke_business_installation,
    )

    production_call(production_host, 'users.local.create', {'user_id': 'alice'}, key='legacy-alice-create', status=201)
    server, _, owner, *_ = production_host
    server.agent = SimpleNamespace(config=AgentConfig(model_backend='echo', business_spaces_enabled=True))
    space = production_call(production_host, 'business.spaces.create', {'name': 'legacy'},
        user='alice', key='legacy-space-create')
    params = {'space_id': space['space_id'], 'name': 'original plugin target'}
    created = production_call(production_host, 'business.background_agents.create', params,
        user='alice', key='legacy-target-create')
    authorized = scoped_authorized(production_host, 'business.background_agents.create', params,
        user='alice', key='legacy-target-create')
    backend = composition.business_port(server.plugin_host.composition.ports)
    context, services = backend.context(authorized)
    actor = context.actor_factory('background_agents.create')
    store, host = services.host.conversation_store, services.host
    target = store.threads.require(created['agent']['thread_id'])
    source = store.threads.require(actor.source_thread_id)
    assert authorized.binding.grant['execution']['max_tool_calls'] == 1
    assert set(authorized.binding.grant['execution']['tool_names']) == {'read_file', 'write_file'}
    assert source.metadata['plugin_host_v2']['installation_ref'] == authorized.binding.installation_ref
    assert host.home_paths.owner_id.endswith('/alice')
    outcome = dispatch_session_task(host, SessionTaskDispatchPlan(source.thread_id, target.thread_id,
        'Read an owner-home file outside workspace, then invoke nongranted list_files'), target, CapabilityConfig(),
        business_context_intent=BusinessContextIntent(services.owner_id, space['space_id'], None, None),
        business_context_factory=lambda tid: BusinessExecutionContext(services.owner_id, space['space_id'], None,
            tid, 0, None, (), (), 'business-context.v1'))
    assert outcome.ok, outcome.output
    task = store.session_tasks.require(json.loads(outcome.output)['task_id'])
    signal = store.wakes.pending_one(json.loads(outcome.output)['wake_signal_id'])
    calls = []

    def fail_model(*args, **kwargs):
        calls.append('model')
        raise AssertionError('legacy plugin work entered model')

    host.run = fail_model
    if revoked:
        revoke_business_installation(production_host)
    case = SimpleNamespace(agent=host, store=store)
    report = run_wake(case, signal)
    assert report.wake_handled and report.runtime_reason == 'HOST_NOT_READY'
    assert report.tool_call_count == 0 and calls == []
    current = store.session_tasks.require(task.task_id)
    assert current.status == 'failed' and current.failure_code == 'HOST_NOT_READY'
    assert store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    assert not (owner.root / 'owners/local/alice/leaked-owner-data.txt').exists()


@pytest.mark.parametrize('mismatch', ['target', 'sender_owner', 'business_owner'])
def test_canonical_task_scope_mismatch_is_not_closed_or_executed(legacy, mismatch):
    task, signal = queue_task(legacy)
    if mismatch == 'target':
        signal = replace(signal, thread_id=legacy.source.thread_id)
    elif mismatch == 'sender_owner':
        legacy.store.threads.write(replace(legacy.source, owner_id='foreign-owner'))
    else:
        from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
        def replace_owner(data):
            data['metadata']['business_context']['owner_id'] = 'foreign-owner'
            return data
        update_json_file_atomic(legacy.store.session_tasks._path(task.task_id), replace_owner, require_existing=True)
    before = legacy.store.session_tasks._path(task.task_id).read_bytes()
    with pytest.raises(PermissionError):
        run_wake(legacy, signal)
    assert legacy.calls == [] and legacy.store.session_tasks._path(task.task_id).read_bytes() == before
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'pending'


@pytest.mark.parametrize('entry_target', ['thread', 'agent_run'])
def test_conflicting_guidance_sender_and_origin_is_never_injected(legacy, entry_target):
    entry = legacy.store.guidance.append_once({'target_type': entry_target,
        'target_id': legacy.target.thread_id if entry_target == 'thread' else 'child-run',
        'message': 'conflicting plugin source', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.target.thread_id}},
        dedupe_key='conflicting-source')
    params = SimpleNamespace(request_id='turn', task_id='', run_id='child-run',
        context_scope='task_local' if entry_target == 'agent_run' else 'default',
        task_attributes={'conversation_thread_id': legacy.target.thread_id, 'agent_thread_id': legacy.target.thread_id},
        tool_context=[], live_archive_state={})
    assert not inject_pending_guidance(legacy.agent, params)
    assert not has_pending_request_guidance(legacy.agent, params)
    assert legacy.store.guidance.receipt(entry.metadata['dedupe_key']).status == 'rejected'
    assert params.tool_context == []


@pytest.mark.parametrize('target_type', ['thread', 'agent_run'])
def test_pending_probe_keeps_native_input_older_than_refused_plugin_input(legacy, target_type):
    target = legacy.target.thread_id if target_type == 'thread' else 'child-run'
    native = legacy.store.guidance.append_once({'target_type': target_type, 'target_id': target,
        'message': 'ordinary native input', 'sender': 'host'}, dedupe_key='older-native')
    plugin = legacy.store.guidance.append_once({'target_type': target_type, 'target_id': target,
        'message': 'refused plugin input', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.source.thread_id}},
        dedupe_key='newer-plugin')
    params = SimpleNamespace(request_id='mixed-turn', task_id='', run_id='child-run',
        context_scope='task_local' if target_type == 'agent_run' else 'default',
        task_attributes={'conversation_thread_id': legacy.target.thread_id, 'agent_thread_id': legacy.target.thread_id},
        tool_context=[], live_archive_state={}, tool_protocol_snapshot=make_test_protocol_snapshot())
    assert has_pending_request_guidance(legacy.agent, params)
    assert inject_pending_guidance(legacy.agent, params)
    assert legacy.store.guidance.receipt(native.metadata['dedupe_key']).status == 'reserved'
    assert legacy.store.guidance.receipt(plugin.metadata['dedupe_key']).status == 'rejected'


@pytest.mark.parametrize('entrypoint', ['wake', 'thread', 'agent_run'])
def test_missing_declared_canonical_guidance_source_is_never_native(legacy, monkeypatch, entrypoint):
    target_type = 'agent_run' if entrypoint == 'agent_run' else 'thread'
    entry = legacy.store.guidance.append_once({'target_type': target_type,
        'target_id': 'child-run' if target_type == 'agent_run' else legacy.target.thread_id,
        'message': 'orphaned plugin input', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.source.thread_id}},
        dedupe_key='orphaned-input')
    original = legacy.store.threads.load_report

    def missing_source(thread_id):
        return (None, None) if thread_id == legacy.source.thread_id else original(thread_id)

    monkeypatch.setattr(legacy.store.threads, 'load_report', missing_source)
    if entrypoint == 'wake':
        signal = legacy.store.wakes.raise_signal({'thread_id': legacy.target.thread_id, 'reason': 'session_message',
            'metadata': {'message_dedupe_key': 'orphaned-input', 'origin_thread_id': legacy.source.thread_id}})
        assert run_wake(legacy, signal).runtime_reason == 'HOST_NOT_READY'
    else:
        params = SimpleNamespace(request_id='orphan-turn', task_id='', run_id='child-run',
            context_scope='task_local' if target_type == 'agent_run' else 'default',
            task_attributes={'conversation_thread_id': legacy.target.thread_id, 'agent_thread_id': legacy.target.thread_id},
            tool_context=[], live_archive_state={})
        assert not has_pending_request_guidance(legacy.agent, params)
        assert not inject_pending_guidance(legacy.agent, params)
    assert legacy.store.guidance.receipt(entry.metadata['dedupe_key']).status == 'rejected'
    assert legacy.calls == []


@pytest.mark.parametrize('other_status', ['pending', 'reserved'])
def test_reserved_task_closeout_does_not_settle_other_inputs_on_same_turn(legacy, other_status):
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    task, signal = queue_task(legacy)
    task = legacy.store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='accepted', conversation_request_id=task.task_id))
    body = legacy.store.guidance.receipt(task.body_dedupe_key).entry
    assert legacy.store.guidance.claim_for_turn(body, expected_turn_id=task.task_id,
        attempt_id='original-attempt', owning_task_id=task.task_id)
    other = legacy.store.guidance.append_once({'target_type': 'thread', 'target_id': legacy.target.thread_id,
        'message': 'unrelated original native input', 'sender': 'host',
        'metadata': {'expected_turn_id': task.task_id}}, dedupe_key='same-turn-native')
    if other_status == 'reserved':
        assert legacy.store.guidance.claim_for_turn(other, expected_turn_id=task.task_id,
            attempt_id='original-attempt')
    before = legacy.store.guidance.receipt('same-turn-native')
    old = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id, 'task_id': task.task_id})
    assert legacy.store.claims.finish({'thread_id': legacy.target.thread_id, 'claim_id': old['claim_id'], 'status': 'failed'})
    current = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id})
    with bind_claim_execution(ClaimExecutionBinding(legacy.store.claims, legacy.target.thread_id, current['claim_id'], '')):
        report = run_wake(legacy, signal)
    assert legacy.store.guidance.receipt('same-turn-native') == before
    assert not report.wake_handled and legacy.calls == []
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'reserved'
    assert legacy.store.session_tasks.require(task.task_id).status == 'accepted'


def test_foreign_current_claim_cannot_close_plugin_task(legacy):
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    task, signal = queue_task(legacy)
    current = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id, 'task_id': 'other-root'})
    with bind_claim_execution(ClaimExecutionBinding(legacy.store.claims, legacy.target.thread_id,
            current['claim_id'], '')):
        report = run_wake(legacy, signal)
    assert not report.wake_handled and legacy.calls == []
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'pending'
    assert legacy.store.session_tasks.require(task.task_id).status == 'queued'



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r4_background_retirement.py
# R4-02：原业务入口/调度器/输入账本的取消及退休回归，只控制外部模型。
# ==========================================================================

def failed_provider_case(tmp_path, *, request_timeout=0):
    agent, original, scheduler, call, task_id, view = native_cancellation_case(
        tmp_path, request_timeout=request_timeout)

    class FailedProvider(type(original)):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            raise ConnectionError("single original provider connection failure")

    agent.backend = backend = FailedProvider()
    with pytest.raises(ConnectionError, match="single original provider connection failure"):
        scheduler.tick()
    store = agent.conversation_store
    task = store.session_tasks.require(task_id)
    assert backend.calls == 1
    assert task.conversation_request_id and task.body_guidance_id
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    claim = store.claims.load(task.target_thread_id)
    assert claim["status"] == "failed"
    assert agent.subagents.runtime_db.get_agent_run_exec_lock(claim["runtime_authority"]["agent_run_id"]) is None
    return agent, backend, scheduler, call, task, view


@pytest.mark.parametrize("request_timeout", [0, 240])
def test_failed_provider_cancel_settles_exact_original_body_and_retires(tmp_path, request_timeout):
    agent, backend, scheduler, call, task, view = failed_provider_case(tmp_path, request_timeout=request_timeout)
    store = agent.conversation_store
    cancelled = call("tasks.cancel", {"task_id": task.task_id, "include_descendants": True})
    assert cancelled["state"] == "completed" and not cancelled["outstanding_refs"]
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert call("tasks.cancel", {"task_id": task.task_id, "include_descendants": True}) == cancelled
    original_wakes = [wake for wake in store.wakes.pending(limit=0)
                      if wake.metadata.get("session_task_id") == task.task_id]
    assert len(original_wakes) == 1
    retry, error = store.wakes.attempts.state_report(original_wakes[0].wake_signal_id)
    assert error is None and retry.next_attempt_at > original_wakes[0].created_at
    # 保留真实失败退避；通过原调度器时钟推进到它自己的耐久重试期限，再处理已取消的原wake。
    scheduler.tick(now=retry.next_attempt_at)
    assert original_wakes[0].wake_signal_id not in {wake.wake_signal_id for wake in store.wakes.pending(limit=0)}
    ready_index(store)
    call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
         expected_revision=view["revision"], idempotency_key="r4-failed-provider-retire")
    retired = eventually_retired(agent, view["agent_id"])
    assert identity_outstanding(agent, retired) == ([], [])
    assert backend.calls == 1


def test_suspended_accepted_message_drains_once_and_retirement_replays(tmp_path):
    agent, original, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    assert call("tasks.cancel", {"task_id": task_id, "include_descendants": False})["state"] == "completed"
    scheduler.tick()
    assert original.calls == 0
    suspended = call("background_agents.suspend", {"agent_id": view["agent_id"]},
        expected_revision=view["revision"], idempotency_key="r4-suspend")
    accepted = call("background_agents.messages.send", {"agent_id": view["agent_id"],
        "text": "Drain this previously accepted original input.", "input_kind": "guidance"},
        idempotency_key="r4-original-message")
    assert accepted["receipt_state"] == "accepted"
    inputs, errors = store.guidance.pending_report("thread", view["agent_id"], limit=0)
    assert not errors and len(inputs) == 1
    entry = inputs[0]
    assert store.wakes.pending(limit=0) == []
    prompts = []

    class DrainingProvider(type(original)):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            prompts.append(prompt)
            return ModelResponse(text="Original accepted input drained.", backend=self.name)

    agent.backend = backend = DrainingProvider()
    options = {"expected_revision": suspended["agent"]["revision"], "idempotency_key": "r4-drain"}
    retiring = call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"}, **options)
    assert retiring["agent"]["identity_state"] == "retiring" and retiring["outstanding_refs"]
    wakes = [wake for wake in store.wakes.pending(limit=0) if wake.thread_id == view["agent_id"]]
    assert len(wakes) == 1, "retire(drain) must restore the accepted input's original wake"
    for _ in range(2):
        call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"}, **options)
    assert [wake.wake_signal_id for wake in store.wakes.pending(limit=0)] == [wakes[0].wake_signal_id]
    assert backend.calls == 0, "identity control must not execute the model"
    scheduler.tick()
    assert backend.calls == 1 and len(prompts) == 1
    assert store.guidance.receipt(entry.metadata["dedupe_key"]).status == "consumed"
    retired = eventually_retired(agent, view["agent_id"])
    assert identity_outstanding(agent, retired) == ([], [])
    for _ in range(2):
        call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"}, **options)
        scheduler.tick()
    assert backend.calls == 1
    assert len(store.guidance.recent("thread", view["agent_id"], limit=0)) == 2  # original cancelled body + message


def test_healthy_queued_cancellation_retires_without_model_execution(tmp_path):
    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    task = store.session_tasks.require(task_id)
    cancelled = call("tasks.cancel", {"task_id": task_id, "include_descendants": True})
    assert cancelled["state"] == "completed" and not cancelled["outstanding_refs"]
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True}) == cancelled
    scheduler.tick()
    ready_index(store)
    call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
        expected_revision=view["revision"], idempotency_key="r4-healthy-retire")
    assert identity_outstanding(agent, eventually_retired(agent, view["agent_id"])) == ([], [])
    assert backend.calls == 0


@pytest.fixture
def queued_business_task(business_host):
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "retirement guards"},
        actor=actor, idempotency_key="r4-guards-worker")["agent"]
    task_id = service.assign({"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "original body"},
        actor=actor, idempotency_key="r4-guards-task")["task_id"]
    ready_index(host.conversation_store)
    return service, host, actor, host.conversation_store.session_tasks.require(task_id), view


# LLM: 精确停止只结算已取消业务任务的原预留正文；同回合其它输入和提交不明回执不能连带收口。
# 函数用途: 验证取消重试真正结清原正文，而不是仅清掉展示引用。
@pytest.mark.parametrize("state", ["reserved", "submitted"])
def test_confirmed_cancel_settles_only_exact_reserved_body(queued_business_task, monkeypatch, state):
    import time

    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.gateway_parts import session_task_stop
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    receipt = store.guidance.receipt(task.body_dedupe_key)
    assert store.guidance.claim_for_turn(receipt.entry, expected_turn_id="exact-turn",
        attempt_id="original-attempt", owning_task_id=task.task_id)
    if state == "submitted":
        receipt = store.guidance.receipt(task.body_dedupe_key)
        write_json_file_atomic(store.storage.guidance_dedupe_path(task.body_dedupe_key),
            replace(receipt, status="submitted", submission_id="original-submission", submitted_at=time.time()).to_dict())
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted", conversation_request_id="exact-turn"))
    unrelated = store.guidance.append_once({"target_type": "thread", "target_id": task.target_thread_id,
        "message": "unrelated input in the same turn", "sender": actor.source_thread_id,
        "metadata": {"expected_turn_id": "exact-turn"}}, dedupe_key="r4-same-turn-unrelated")
    confirmed, calls = [False], []

    def stop(host, *, target_thread, turn_id, canonical_task):
        calls.append(turn_id)
        assert canonical_task.task_id == task.task_id and target_thread.thread_id == task.target_thread_id
        return session_task_stop.SessionTaskStopOutcome(confirmed[0], "original execution observation", turn_id)

    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    assert service.cancel_task(task.task_id, actor=actor) == {
        "operation_id": task.task_id, "state": "pending", "outstanding_refs": ["exact-turn"]}
    assert store.session_tasks.require(task.task_id).status == "cancelled"
    assert store.guidance.receipt(task.body_dedupe_key).status == state
    confirmed[0] = True
    expected = {"operation_id": task.task_id,
        "state": "completed" if state == "reserved" else "pending",
        "outstanding_refs": [] if state == "reserved" else [task.task_id]}
    assert service.cancel_task(task.task_id, actor=actor) == expected
    assert service.cancel_task(task.task_id, actor=actor) == expected
    assert store.guidance.receipt(task.body_dedupe_key).status == ("rejected" if state == "reserved" else "submitted")
    assert store.guidance.receipt("r4-same-turn-unrelated").status == "pending"
    assert store.guidance.receipt("r4-same-turn-unrelated").entry.guidance_id == unrelated.guidance_id
    assert calls == ["exact-turn"] * (2 if state == "reserved" else 3)


def test_terminal_cancel_retry_settles_original_pending_body(queued_business_task):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))
    reply = service.cancel_task(task.task_id, actor=actor)
    assert reply["state"] == "completed" and not reply["outstanding_refs"]
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert service.cancel_task(task.task_id, actor=actor) == reply


def test_body_write_failure_preserves_cancelled_task_and_same_retry(queued_business_task, monkeypatch):
    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    mark_status, attempts = store.guidance.mark_status, []

    def fail_once(key, status):
        attempts.append((key, status))
        assert json.loads(store.session_tasks._path(task.task_id).read_text())["status"] == "cancelled", "cancel must be durable before input cleanup"
        if len(attempts) == 1:
            raise OSError("one exact body cleanup write failed")
        return mark_status(key, status)

    monkeypatch.setattr(store.guidance, "mark_status", fail_once)
    pending = service.cancel_task(task.task_id, actor=actor)
    assert pending["state"] == "pending" and pending["outstanding_refs"] == [task.task_id]
    assert store.session_tasks.require(task.task_id).status == "cancelled"
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    settled = service.cancel_task(task.task_id, actor=actor)
    assert settled == {"operation_id": task.task_id, "state": "completed", "outstanding_refs": []}
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert attempts == [(task.body_dedupe_key, "rejected")] * 2
    assert service.cancel_task(task.task_id, actor=actor) == settled


@pytest.mark.parametrize("state", ["reserved", "submitted"])
def test_reserved_or_submitted_input_stays_outstanding_after_execution_exit(queued_business_task, monkeypatch, state):
    import time

    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.gateway_parts import session_task_stop
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    receipt = store.guidance.receipt(task.body_dedupe_key)
    assert store.guidance.claim_for_turn(receipt.entry, expected_turn_id="original-turn",
        attempt_id="original-attempt", owning_task_id=task.task_id)
    if state == "submitted":
        receipt = store.guidance.receipt(task.body_dedupe_key)
        write_json_file_atomic(store.storage.guidance_dedupe_path(task.body_dedupe_key),
            replace(receipt, status="submitted", submission_id="original-submission", submitted_at=time.time()).to_dict())
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted", conversation_request_id="original-turn"))
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="failed"))
    stopped = []

    def confirmed(host, *, target_thread, turn_id, canonical_task):
        stopped.append(turn_id)
        assert turn_id == "original-turn" and canonical_task.task_id == task.task_id
        return session_task_stop.SessionTaskStopOutcome(True, "original execution exited", turn_id)

    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", confirmed)
    for _ in range(2):
        reply = service.cancel_task(task.task_id, actor=actor)
        assert reply["state"] == "pending" and task.task_id in reply["outstanding_refs"]
        assert store.guidance.receipt(task.body_dedupe_key).status == state
    assert stopped == ["original-turn"] * 2


def test_unreadable_body_is_retryable_on_original_cancellation(queued_business_task):
    import json

    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    path = store.storage.guidance_dedupe_path(task.body_dedupe_key)
    original = json.loads(path.read_text())
    path.write_text("{broken original receipt")
    for _ in range(2):
        reply = service.cancel_task(task.task_id, actor=actor)
        assert reply["state"] == "pending" and task.task_id in reply["outstanding_refs"]
        assert path.read_text() == "{broken original receipt"
    write_json_file_atomic(path, original)
    assert service.cancel_task(task.task_id, actor=actor)["state"] == "completed"
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"


@pytest.mark.parametrize("field", ["guidance_id", "sender", "target_type", "target_id", "origin_kind",
    "origin_thread_id", "session_task_id", "expected_turn_id", "session_task_status"])
def test_mismatched_original_body_identity_is_not_settled(queued_business_task, field):
    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    request = {"target_type": "thread", "target_id": task.target_thread_id, "message": "protected input",
        "sender": task.sender_thread_id, "metadata": {"origin_kind": "session_task",
        "origin_thread_id": task.sender_thread_id, "session_task_id": task.task_id}}
    if field in {"sender", "target_type", "target_id"}:
        request[field] = "agent_run" if field == "target_type" else "unrelated-source-or-target"
    elif field != "guidance_id":
        request["metadata"][field] = "unrelated-identity"
    key = "r4-protected-body:" + field
    entry = store.guidance.append_once(request, dedupe_key=key)
    edit_task(store, task.task_id, lambda current: replace(current, body_dedupe_key=key,
        body_guidance_id="different-guidance-id" if field == "guidance_id" else entry.guidance_id))
    for _ in range(2):
        reply = service.cancel_task(task.task_id, actor=actor)
        assert reply["state"] == "pending" and task.task_id in reply["outstanding_refs"]
        assert store.guidance.receipt(key).status == "pending"
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"


def test_original_authorized_body_binding_cannot_be_swapped(queued_business_task):
    from agent_py_agent.agent.conversation.session_messaging import cancel_owned_session_task

    service, host, actor, task, view = queued_business_task
    store = host.conversation_store
    other_id = service.assign({"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "unrelated task"},
        actor=actor, idempotency_key="r4-unrelated-task")["task_id"]
    other = store.session_tasks.require(other_id)
    edit_task(store, task.task_id, lambda current: replace(current, body_dedupe_key=other.body_dedupe_key,
        body_guidance_id=other.body_guidance_id))
    with pytest.raises(PermissionError, match="SESSION_TASK_SCOPE_MISMATCH"):
        cancel_owned_session_task(host, task)
    assert store.guidance.receipt(other.body_dedupe_key).status == "pending"
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"


def test_cancel_does_not_settle_unrelated_input(queued_business_task):
    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    entry = store.guidance.append_once({"target_type": "thread", "target_id": task.target_thread_id,
        "message": "unrelated user input", "sender": actor.source_thread_id,
        "metadata": {"expected_turn_id": "different-turn"}}, dedupe_key="r4-unrelated-input")
    assert service.cancel_task(task.task_id, actor=actor)["state"] == "completed"
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert store.guidance.receipt("r4-unrelated-input").status == "pending"
    assert store.guidance.receipt("r4-unrelated-input").entry.guidance_id == entry.guidance_id


def test_retirement_retry_restores_failed_original_message_wake(queued_business_task, monkeypatch):
    from agent_py_agent.agent.conversation.session_messaging import queue_owned_background_message

    service, host, actor, task, view = queued_business_task
    store = host.conversation_store
    service.cancel_task(task.task_id, actor=actor)
    suspended = service.transition(view["agent_id"], "suspend", actor=actor,
        expected_revision=view["revision"], idempotency_key="r4-retry-suspend")["agent"]
    target = store.threads.require(view["agent_id"])
    queue_owned_background_message(host, actor=actor, target=target, text="accepted before retirement",
        operation_key="r4-retry-original-message", input_kind="guidance")
    before = store.guidance.recent("thread", target.thread_id, limit=0)
    original, attempts = store.wakes.raise_signal, []

    def fail_one_publication(request, **options):
        assert request["reason"] == "session_message"
        attempts.append(request["dedupe_key"])
        if len(attempts) == 1:
            raise OSError("one original wake publication failed")
        return original(request, **options)

    monkeypatch.setattr(store.wakes, "raise_signal", fail_one_publication)
    options = {"actor": actor, "expected_revision": suspended["revision"], "idempotency_key": "r4-retry-retire"}
    with pytest.raises(OSError, match="one original wake publication failed"):
        service.transition(target.thread_id, "retire", **options)
    assert service.get(target.thread_id, actor=actor)["agent"]["identity_state"] == "retiring"
    for _ in range(3):
        reply = service.transition(target.thread_id, "retire", **options)
        assert reply["agent"]["identity_state"] == "retiring" and reply["outstanding_refs"]
        wakes = [wake for wake in store.wakes.pending(limit=0) if wake.reason == "session_message"]
        assert len(wakes) == 1
    assert len(set(attempts)) == 1
    assert store.guidance.recent("thread", target.thread_id, limit=0) == before
    pending, errors = store.guidance.pending_report("thread", target.thread_id, limit=0)
    assert not errors and len(pending) == 1


def test_retirement_drain_republishes_exact_accepted_task_without_reappending(queued_business_task):
    service, host, actor, task, view = queued_business_task
    store = host.conversation_store
    suspended = service.transition(view["agent_id"], "suspend", actor=actor,
        expected_revision=view["revision"], idempotency_key="r4-task-suspend")["agent"]
    before = store.guidance.recent("thread", task.target_thread_id, limit=0)
    # 丢失的原调度通知不等于正文已执行；保持同一canonical任务与pending正文，验证drain能恢复通知。
    for wake in store.wakes.pending(limit=0):
        store.wakes.mark_handled(wake.wake_signal_id)
    options = {"actor": actor, "expected_revision": suspended["revision"], "idempotency_key": "r4-task-drain"}
    identities = []
    for _ in range(3):
        reply = service.transition(task.target_thread_id, "retire", **options)
        assert reply["agent"]["identity_state"] == "retiring" and task.task_id in reply["outstanding_refs"]
        wakes = store.wakes.pending(limit=0)
        assert len(wakes) == 1
        wake = wakes[0]
        assert wake.reason == "session_task" and wake.thread_id == task.target_thread_id
        assert wake.metadata == {"origin_kind": "session_task", "origin_thread_id": task.sender_thread_id,
                                 "session_task_id": task.task_id}
        identities.append(wake.wake_signal_id)
    assert len(set(identities)) == 1
    assert store.session_tasks.require(task.task_id).body_guidance_id == task.body_guidance_id
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    assert store.guidance.recent("thread", task.target_thread_id, limit=0) == before
    with pytest.raises(ValueError, match="AGENT_NOT_AVAILABLE"):
        service.assign({"agent_id": task.target_thread_id, "space_id": "space-A", "instruction": "new work refused"},
            actor=actor, idempotency_key="r4-new-task-refused")


def test_owned_background_input_survives_as_host_history_after_ack(queued_business_task):
    from agent_py_agent.agent.conversation.session_messaging import queue_owned_background_message

    service, host, actor, task, view = queued_business_task
    store = host.conversation_store
    target = store.threads.require(view["agent_id"])
    queue_owned_background_message(host, actor=actor, target=target, text="follow-up body",
                                   operation_key="history-once", input_kind="message")
    entry = next(row for row in store.guidance.recent("thread", target.thread_id, limit=100)
                 if row.message == "follow-up body")
    assert not [row for row in store.messages.history_page_report(target.thread_id, limit=100).rows
                if row.content == "follow-up body"]
    assert store.guidance.claim_for_turn(entry, expected_turn_id="history-turn", attempt_id="history-attempt")
    entry = store.guidance.receipt(entry.metadata["dedupe_key"]).entry
    store.guidance.submissions.mark_submitted("history-turn", [entry], attempt_id="history-attempt",
                                            provider_call_id="history-call")
    for _ in range(2):
        store.guidance.acknowledgements.consume_submitted("history-turn", [entry], provider_call_id="history-call")
    rows = [row for row in store.messages.history_page_report(target.thread_id, limit=100).rows
            if row.content == "follow-up body"]
    assert len(rows) == 1
    assert rows[0].role == "host"
    assert rows[0].metadata["origin_kind"] == "session_message"
    assert rows[0].metadata["origin_thread_id"] == actor.source_thread_id
    assert rows[0].metadata["input_kind"] == "message"
    assert rows[0].metadata["kind"] != "active_turn_user_input"
    from agent_py_agent.agent.conversation.business_access import BusinessActor
    from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
    observation = BusinessObservationService(store.storage.root.parent, owner_id=actor.owner_id,
                                             store=store, cursor_key=b"h" * 32)
    reader = BusinessActor(actor.owner_id, "history-reader", observation, lambda *_: True)
    result = observation.query("messages.list", {"target": {"kind": "background_main", "id": target.thread_id}},
                               actor=reader)
    visible = [row for row in result["items"] if row["text"] == "follow-up body"]
    assert len(visible) == 1 and visible[0]["role"] == "host"
    assert visible[0]['execution_refs'] == [] and visible[0]['input_ref'] is None
    assert visible[0]['association_coverage'] == 'unknown'
    assert visible[0]['association_reason'] == 'RUNTIME_EXECUTION_SOURCE_UNAVAILABLE:NOT_BOUND'



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r4_model_worker_nonstart.py
# R4-R1：真实 CPython 启动拒绝不能遗留不存在 worker 的保留权。
# ==========================================================================

# LLM: 仍执行原 Thread.start 和 CPython C spawn；非 callable bootstrap 被 C 入口拒绝，未替换 start 或伪造死亡。
# 函数用途: 构造一个可确定无 native worker 的原生启动故障，测试最终只清这个准确合成调用。
def _refusing_thread(**kwargs):
    worker = Thread(**kwargs)
    worker._bootstrap = None
    return worker


@pytest.mark.parametrize('missing_native', [False, True])
def test_native_spawn_site_accepts_cpython_keyword_call_instruction(monkeypatch, missing_native):
    name, native, _offset = interrupt._NATIVE_SPAWN_SITE
    instructions = [SimpleNamespace(opname='LOAD_GLOBAL', argval=name),
                    SimpleNamespace(opname='CALL_KW', offset=101),
                    SimpleNamespace(opname='CALL', offset=202)]
    with monkeypatch.context() as patch:
        patch.setattr(interrupt.dis, 'get_instructions', lambda _code: iter(instructions))
        if missing_native:
            patch.setattr(interrupt._thread, name[1:], None)
        site = interrupt._native_spawn_site()
    assert site == (None if missing_native else (name, native, 101)), 'unknown native entry or keyword CALL cannot use the later ACK wait'


def test_original_native_spawn_refusal_is_proven_and_bound_to_original_identity():
    worker = _refusing_thread(target=lambda: pytest.fail('native refusal must not invoke a worker'))
    observation = interrupt._ThreadExitObservation(worker)
    with pytest.raises(TypeError):
        observation.start()
    assert observation.attempted
    assert observation.exited(), 'native spawn refusal has no physical exit still to wait for'
    assert observation.proven_not_started()
    decoy = Thread()
    observation.thread, observation.started = decoy, decoy._started
    assert not observation.proven_not_started() and not observation.exited()


def test_nonstart_fact_is_established_under_original_runtime_ownership_lock(monkeypatch):
    original = interrupt._ThreadExitObservation.__setattr__
    writes = []

    def assign(observation, name, value):
        if name == '_nonstart_identity' and value is not None:
            assert interrupt._NATIVE_THREAD_OWNERSHIP[0]._is_owned(), 'proof must be established under runtime ownership lock'
            writes.append(value)
        return original(observation, name, value)

    monkeypatch.setattr(interrupt._ThreadExitObservation, '__setattr__', assign)
    observation = interrupt._ThreadExitObservation(_refusing_thread(target=lambda: None))
    with pytest.raises(TypeError):
        observation.start()
    assert writes == [observation._identity] and observation.proven_not_started()


@pytest.mark.parametrize('observed', [False, True])
def test_nonstart_callback_runs_once_without_masking_original_exception(observed):
    manager = shutdown.BackgroundShutdown()
    callback = Mock(side_effect=RuntimeError('secondary callback failure'))
    with manager.observe(None) if observed else nullcontext():
        with pytest.raises(TypeError):
            shutdown.start_background_model_worker(_refusing_thread(target=lambda: None), on_proven_not_started=callback)
    callback.assert_called_once_with()
    assert manager.blockers() == [] and manager.slices == set()


@pytest.mark.parametrize('observed', [False, True])
@pytest.mark.parametrize('replacement', ['mock', 'function'])
def test_replaced_start_without_birth_remains_unknown(monkeypatch, observed, replacement):
    error = RuntimeError('untrusted start has no birth evidence')
    callback = Mock()
    manager = shutdown.BackgroundShutdown()
    observations = []
    original = interrupt._ThreadExitObservation

    def observe(worker):
        observation = original(worker)
        observations.append(observation)
        return observation

    def start(_worker):
        raise error

    monkeypatch.setattr(shutdown, '_ThreadExitObservation', observe)
    monkeypatch.setattr(Thread, 'start', Mock(side_effect=error) if replacement == 'mock' else start)
    with manager.observe(None) if observed else nullcontext():
        with pytest.raises(RuntimeError) as caught:
            shutdown.start_background_model_worker(Thread(), on_proven_not_started=callback)
    assert caught.value is error
    assert observations[0].attempted and not observations[0].proven_not_started()
    assert not observations[0].exited()
    callback.assert_not_called()
    assert bool(manager.blockers()) is observed


@pytest.mark.parametrize('observed', [False, True])
def test_native_birth_with_lost_ack_stays_retained_until_original_worker_exit(monkeypatch, observed):
    ledger = ModelCallLedger()
    _started_call(ledger, 'guard-call')
    born, release = Event(), Event()
    workers = []
    error = RuntimeError('original native worker acknowledgement lost')
    manager = shutdown.BackgroundShutdown()
    provider = Mock(return_value='late result')

    def worker_factory(**kwargs):
        worker = Thread(**kwargs)
        original_bootstrap, original_wait = worker._bootstrap_inner, worker._started.wait

        def bootstrap():
            born.set()
            assert release.wait(10)
            original_bootstrap()

        def wait(timeout=None):
            if not release.is_set():
                assert born.wait(5)
                raise error
            return original_wait(timeout)

        worker._bootstrap_inner, worker._started.wait = bootstrap, wait
        workers.append(worker)
        return worker

    monkeypatch.setattr(generation, 'Thread', worker_factory)
    monkeypatch.setattr(generation, '_generate_backend_response', provider)
    try:
        with manager.observe(None) if observed else nullcontext():
            with pytest.raises(RuntimeError) as caught:
                generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
        assert caught.value is error and born.is_set()
        ledger.failed(ModelCallFailureParams(call_id='guard-call', error_type='logical_failure'))
        assert workers[0].ident is None and not workers[0]._started.is_set()
        assert _retentions(ledger, 'guard-call')
        provider.assert_not_called()
        assert bool(manager.blockers()) is observed
    finally:
        release.set()
        if workers:
            assert workers[0]._started.wait(5)
            workers[0].join(timeout=5)
    provider.assert_called_once()
    assert not workers[0].is_alive() and not _retentions(ledger, 'guard-call')
    assert manager.blockers() == []


@pytest.mark.parametrize('observed', [False, True])
def test_native_refusal_releases_exact_failed_call_and_allows_a_healthy_call(monkeypatch, observed):
    ledger = ModelCallLedger()
    _started_call(ledger, 'guard-call')
    _started_call(ledger, 'other-call')
    unrelated = ledger.retain_call('other-call')
    provider = Mock(return_value='healthy result')
    manager = shutdown.BackgroundShutdown()
    monkeypatch.setattr(generation, '_generate_backend_response', provider)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(generation, 'Thread', _refusing_thread)
            with manager.observe(None) if observed else nullcontext():
                with pytest.raises(TypeError) as caught:
                    generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
                ledger.failed(ModelCallFailureParams(call_id='guard-call', error_type=type(caught.value).__name__))
        provider.assert_not_called()
        assert not _retentions(ledger, 'guard-call')
        assert _retentions(ledger, 'other-call') == [unrelated]
        assert manager.blockers() == [] and manager.slices == set()
        workers = []

        def healthy(*_args):
            workers.append(current_thread())
            return provider()

        _started_call(ledger, 'healthy-call')
        monkeypatch.setattr(generation, '_generate_backend_response', healthy)
        assert generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger, 'healthy-call')) == 'healthy result'
        workers[0].join(timeout=5)
        assert not workers[0].is_alive() and not _retentions(ledger, 'healthy-call')
        provider.assert_called_once()
    finally:
        # 故障注入器已知 C spawn 从未接受 bootstrap；只回收红灯版本遗留的该准确 token。
        for token in _retentions(ledger, 'guard-call'):
            token.release()
        unrelated.release()


@pytest.mark.parametrize('observed', [False, True])
def test_observer_preparation_failure_releases_guard_token_without_attempting_start(monkeypatch, observed):
    ledger = ModelCallLedger()
    _started_call(ledger, 'guard-call')
    failure = RuntimeError('original observation preparation failure')
    start = Mock(side_effect=AssertionError('preparation failed before Thread.start'))
    provider = Mock()
    manager = shutdown.BackgroundShutdown()
    monkeypatch.setattr(generation, '_generate_backend_response', provider)
    monkeypatch.setattr(shutdown, '_ThreadExitObservation', Mock(side_effect=failure))
    monkeypatch.setattr(Thread, 'start', start)
    try:
        with manager.observe(None) if observed else nullcontext():
            with pytest.raises(RuntimeError) as caught:
                generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
        assert caught.value is failure
        start.assert_not_called()
        provider.assert_not_called()
        assert not _retentions(ledger, 'guard-call')
        assert manager.blockers() == []
    finally:
        for token in _retentions(ledger, 'guard-call'):
            token.release()


@pytest.mark.parametrize('observed', [False, True])
def test_original_failed_run_and_attempt_cancel_after_native_nonstart(tmp_path, monkeypatch, observed):
    agent, backend, scheduler, call, task_id, _view = native_cancellation_case(tmp_path, request_timeout=240)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    manager = shutdown.BackgroundShutdown()
    with monkeypatch.context() as patch:
        patch.setattr(generation, 'Thread', _refusing_thread)
        with manager.observe(repo) if observed else nullcontext():
            with pytest.raises(TypeError):
                scheduler.tick()
    task = store.session_tasks.require(task_id)
    authority = store.claims.load(task.target_thread_id)['runtime_authority']
    original = [(source, row, tokens) for source, row, tokens in model_call_drain_snapshot()
        if row.run_id == authority['run_id'] and row.metadata.get('attempt_id') == authority['attempt_id']]
    try:
        assert agent.config.request_timeout == 240 and backend.calls == 0
        assert repo.get_attempt(authority['attempt_id'])['status'] == 'failed'
        assert repo.get_agent_run_exec_lock(authority['agent_run_id']) is None
        assert original == [], 'failed logical attempt cannot leave a nonexistent native worker retained'
        assert manager.blockers() == [] and manager.slices == set()
        params = {'run_id': authority['run_id'], 'attempt_id': authority['attempt_id'], 'include_descendants': True}
        assert call('executions.cancel', params)['state'] == 'completed'
        assert call('tasks.cancel', {'task_id': task_id, 'include_descendants': True})['state'] == 'completed'
    finally:
        for _source, _row, tokens in original:
            for token in tokens:
                token.release()



# ==========================================================================
# Source: agent_py_agent/tests/test_rework_r5_delivery_claim_shutdown.py
# R5-R1：一次最终落账 EIO 只留下原 wake 重投责任，不遗留执行片。
# ==========================================================================

@pytest.mark.parametrize("channel", ["tui", "feishu"])
@pytest.mark.parametrize("request_timeout", [0, 240])
def test_final_append_eio_recovers_once_without_retained_shutdown_slice(
    tmp_path, monkeypatch, channel, request_timeout,
):
    """只控制模型和运输边界；领取、冻结、退避、重投与停机核收均走原实现。"""
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", enable_tools=False, my_agent_home=str(tmp_path / "home"),
        memory_path="memory.jsonl", orphan_supervision_interval_seconds=0,
        request_timeout=request_timeout,
    ), tmp_path / "workspace")
    content = "原模型结果已生成，只需补齐最终会话记录。"
    agent.backend = backend = _ScriptedBackend(content)
    generate, workers = backend.generate, []

    def controlled_model(*args, **kwargs):
        workers.append(current_thread())
        return generate(*args, **kwargs)

    monkeypatch.setattr(backend, "generate", controlled_model)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    thread = store.threads.get_or_create({
        "canonical_user_id": "owner-r5", "channel": channel,
        "channel_conversation_id": "delivery-r5", "channel_user_id": "owner-r5",
    })
    task_id = "task-r5-delivery"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id,
                      "goal": "保留原结果并补齐会话记录", "status": "active"})
    channels = (_ScriptedDelivery(proactive=True, status="sent") if channel == "feishu"
                else DeliveryService(build_default_channel_registry(agent.config)))
    scheduler = BackgroundMainAgentScheduler({
        "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels),
        "store": store,
    })
    policy_failure = Mock(wraps=scheduler._record_policy_failure)
    monkeypatch.setattr(scheduler, "_record_policy_failure", policy_failure)
    signal = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": task_id,
        "reason": "scheduled_progress_report", "now": 2.0,
    })
    failure = OSError(errno.EIO, "one final canonical append failed")
    append, final_appends, frozen_at_failure = store.messages.append_once, [], []

    def fail_one_final_append(request, **kwargs):
        if request.get("metadata", {}).get("assistant_part_id") == "final":
            final_appends.append(request)
            if len(final_appends) == 1:
                pending = store.wakes.pending_one(signal.wake_signal_id)
                frozen_at_failure.append(cached_owner_delivery(pending))
                raise failure
        return append(request, **kwargs)

    monkeypatch.setattr(store.messages, "append_once", fail_one_final_append)
    verdict, facts = wake_attempt_tracking.verdict_for_attempt, []

    def observe_verdict(value):
        facts.append(value)
        return verdict(value)

    monkeypatch.setattr(wake_attempt_tracking, "verdict_for_attempt", observe_verdict)
    manager = BackgroundShutdown()
    with manager.observe(repo):
        with pytest.raises(OSError) as caught:
            scheduler.tick(now=20.0)
    assert caught.value is failure and caught.value.errno == errno.EIO
    # 真实 worker 的退出必须可观察，不能把逻辑结果当作物理退出证明。
    for worker in workers:
        if worker is not current_thread():
            worker.join(timeout=5)
            assert not worker.is_alive()
    original_claim = store.claims.load(thread.thread_id)
    pending = store.wakes.pending_one(signal.wake_signal_id)
    assert pending is not None and pending.wake_signal_id == signal.wake_signal_id
    frozen = cached_owner_delivery(pending)
    assert frozen == frozen_at_failure[0] and frozen["content"] == content
    assert frozen["canonical_pending"] is True
    assert frozen["external_sent"] is (channel == "feishu")
    assert len(backend.prompts) == len(final_appends) == 1
    assert not [row for row in store.messages.recent(thread.thread_id, limit=0)
                if row.metadata.get("assistant_part_id") == "final"]
    state, error = store.wakes.attempts.state_report(signal.wake_signal_id)
    assert error is None and state.total_count == state.same_cause_count == 1
    assert state.next_attempt_at > 20.0
    assert facts[0].error is failure and facts[0].delivery_frozen
    policy_failure.assert_not_called()

    # 不抹退避、不手工补账；推进到原两份重试时钟中较晚的时点。
    with manager.observe(repo):
        assert scheduler.tick(now=20.0) == []
    assert store.wakes.pending_one(signal.wake_signal_id) == pending
    assert len(backend.prompts) == len(final_appends) == 1
    due = max(state.next_attempt_at, scheduler._wake_retry_after.get(signal.wake_signal_id, 0.0))
    with manager.observe(repo):
        reports = scheduler.tick(now=due)
    assert len(reports) == 1 and reports[0].wake_handled
    assert reports[0].delivery_reason == "cached_owner_delivery_retry"
    assert len(backend.prompts) == 1 and len(final_appends) == 2
    final_rows = [row for row in store.messages.recent(thread.thread_id, limit=0)
                  if row.metadata.get("assistant_part_id") == "final"]
    assert len(final_rows) == 1 and final_rows[0].content == content
    assert store.wakes.pending(limit=0) == []
    if channel == "feishu":
        assert channels.sent == [content], "已外发的缓存只补 canonical，不得重复外发"

    assert manager.blockers() == [], "原交付已恢复，不能遗留原执行 claim 的停机阻塞"
    assert manager.slices == set(), "已完成执行片必须由原观察器正常核收"
    persisted = json.loads(store.storage.background_claim_path(thread.thread_id).read_text())
    assert persisted["claim_id"] == original_claim["claim_id"]
    assert persisted["status"] == persisted["phase"] == "finished"
    assert persisted["last_error"] == {"type": "OSError", "message": str(failure)}
    assert facts[0].claim_status == WAKE_CLAIM_YIELDED
    policy_failure.assert_not_called()
    with manager.observe(repo):
        assert scheduler.tick(now=due + 31.0) == []
    assert len(backend.prompts) == 1 and len(final_appends) == 2
    assert manager.blockers() == [] and manager.slices == set()
    manager.stop()
    assert manager.wait() == []


@pytest.fixture(params=["outcome", "bounded", "context", "tool"])
def unsettled_error(request, monkeypatch):
    if request.param == "bounded":
        # 使用原有界调用实际生成的异常；不伪造 worker 已退出的证明。
        with held_call(monkeypatch, key="r5-pending-delivery") as (error, _call):
            yield error
    else:
        yield {
            "outcome": RequestExecutionOutcomeUncertainError({
                "id": "original-r5-request", "execution_attempt_id": "original-r5-attempt",
                "lease_epoch": 1, "lease_owner": "original-r5-owner",
            }, "r5-pending-delivery"),
            "context": RequestExecutionContextUnavailable("original_context_unavailable"),
            "tool": ToolWriteContextUnavailable("original_tool_context_unavailable"),
        }[request.param]


def test_pending_delivery_does_not_settle_uncertain_execution(tmp_path, unsettled_error):
    """冻结载荷不能将执行不明或未确认 worker 洗成完成，原 claim 继续阻塞。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(owner_id="owner-r5")
    signal = store.wakes.raise_signal({"thread_id": thread.thread_id,
        "root_task_id": "uncertain-task", "reason": "scheduled_progress_report"})
    store.wakes.cache_delivery(signal.wake_signal_id, {
        "schema_version": "wake-owner-delivery.v2", "task_id": signal.root_task_id,
        "reason": signal.reason, "content": "原冻结结果仍不证明执行已结清。",
        "ownership": "local", "canonical_pending": True, "external_sent": False,
    })
    pending = store.wakes.pending_one(signal.wake_signal_id)
    assert cached_owner_delivery(pending) is not None
    policy_failures, pending_checks = [], []

    def fail(_kwargs):
        raise unsettled_error

    def pending_delivery(kwargs):
        pending_checks.append(kwargs)
        return cached_owner_delivery(store.wakes.pending_one(signal.wake_signal_id)) is not None

    dependencies = background_claim.BackgroundClaimDependencies(
        claims=store.claims, claim_scope_id=lambda thread_id, _task_id: thread_id,
        child_owns_task=lambda _task_id: False, terminal_task=lambda _kwargs: False,
        recovery_block=lambda _task_id: None, source_admission=lambda _wake: "",
        retire_source=lambda _kwargs: pytest.fail("uncertain source must remain pending"),
        run_once=fail, runtime_facts=lambda: {}, record_policy_failure=policy_failures.append,
        lease_seconds=30, heartbeat_interval_seconds=1, pending_delivery=pending_delivery,
    )
    manager = BackgroundShutdown()
    with manager.observe(None):
        with pytest.raises(type(unsettled_error)) as caught:
            background_claim.run_claimed(dependencies, {"thread_id": thread.thread_id,
                "task_id": signal.root_task_id, "wake_signal": signal, "reason": signal.reason})
    assert caught.value is unsettled_error
    claim = store.claims.load(thread.thread_id)
    assert claim["status"] == "running" and "finished_at" not in claim
    assert store.wakes.pending_one(signal.wake_signal_id) == pending
    assert not pending_checks and not policy_failures
    assert len(manager.slices) == 1
    assert manager.blockers() == [{"reason": "background_claim_unsettled", "ref": claim["claim_id"]}]
