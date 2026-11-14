"""Consolidated test shard module: test_consolidated_c7_g2.py.

Sources:
- agent_py_agent/tests/test_rework_r3_hint_init_recovery.py
- agent_py_agent/tests/test_rework_r3_local_pagination.py
- agent_py_agent/tests/test_rework_r3_plugin_sources.py
- agent_py_agent/tests/test_rework_r4_network_contract.py
- agent_py_agent/tests/test_rework_r5_portable_runtime.py
- agent_py_agent/tests/test_rework_r9_adapter_delivery_lifetime.py
- agent_py_agent/tests/test_rework_r9_audit_redelivery_io.py

[test_rework_r3_hint_init_recovery.py] 提示索引失败不能阻断原后台循环；失败构造和退出均不残留提示资源。
[test_rework_r3_local_pagination.py] 同一真实Agent的本地业务入口复用只读服务，但不复用调用者权限。
[test_rework_r3_plugin_sources.py] R2 / GPT-P1-03：真实配置的外部 file: 宿主来源进入全部插件启动路径的最终隐藏规格。
[test_rework_r4_network_contract.py] R4 contracts through disposable authenticated loopback listeners (not LAN proof).
[test_rework_r5_portable_runtime.py] R5：验证编译前拒绝、真实安装器启动及 portable 依赖准备的 ABI 分流。
[test_rework_r9_adapter_delivery_lifetime.py] R9-R1 P1-R9-19：唯一 adapter 投递线程遇普通队列 OSError 后沿原可中断等待继续推进。
"""

from __future__ import annotations

import base64
import errno
import gc
import importlib.util
import json
import os
import secrets
import shlex
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from agent.common.audit_activation import audit_source_worker_key
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion.watch_state import persist_state

from agent_py_agent.agent import owner_wake_discovery, plugin_runtime, plugin_sandbox
from agent_py_agent.agent.adapter import ingress
from agent_py_agent.agent.adapter.delivery import (
    GatewayClaimLeaseConfig,
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
)
from agent_py_agent.agent.adapter.ingress import (
    GatewayAdapterDeliveryWorker,
    GatewayIngressAdvance,
    GatewayIngressStore,
    build_gateway_ingress_record,
)
from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, SandboxUnavailableError
from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.auth.models import Role
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import plugin_panels_http
from agent_py_agent.agent.gateway_parts.admin_control_service import pending_tool_approvals
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _scope_request_payload,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.permission_bridge import gateway_permission_decision_path
from agent_py_agent.agent.gateway_parts.queue_service import archive_request
from agent_py_agent.agent.gateway_parts.stream_writer import (
    BufferedChunkStreamWriter,
    write_chunk_event,
)
from agent_py_agent.agent.path_access_policy import configured_host_source_paths
from agent_py_agent.agent.plugin_display.service import plugin_display_client
from agent_py_agent.agent.plugin_enable_tool import PluginEnablePolicy
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.plugin_runtime import PluginMCPClient
from agent_py_agent.agent.runtime_db import input_lifecycle
from agent_py_agent.agent.scheduler import due_index
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.tooling import sandbox as bwrap_compiler
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPStdioClient
from agent_py_agent.agent.tooling.plugin_registration import synchronize_plugin_clients
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.agent.tooling.registry import ToolRegistryParams
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    NetworkGatewayObserver,
)
from agent_py_agent.cli.chat_parts.tui_runtime import (
    TuiRuntime,
    TuiTurnEventAdapter,
    TuiTurnSummary,
)
from agent_py_agent.tests.plugin_enable_fixtures import confirm_legacy_enable
from agent_py_agent.tests.test_mega_background_lifecycle_controls import ready_index
from agent_py_agent.tests.test_plugin_any_language import (
    PLUGIN_ID,
    _declaration,
    _enable,
    _package,
)
from agent_py_agent.tests.test_plugin_manifest_v8 import _bundle as v8_bundle
from agent_py_agent.tests.test_plugin_manifest_v8 import _v8
from agent_py_agent.tests.test_watch_audit_guarantee import (
    _ack_ids,
    _audit_state,
    _FakeSource,
    _verdict,
)
from agent_py_agent.tests.test_watch_audit_guarantee import (
    owner_home as _owner_home,
)
from scripts.private_release import build

# =============================================================================
# Source: agent_py_agent/tests/test_rework_r3_hint_init_recovery.py
# =============================================================================

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """只替换领域控制器，保留真实组合根、线程启动、tick/backoff、索引及订阅。"""
    home = SimpleNamespace(root=tmp_path, global_index_dir=tmp_path / "global_index",
        owners_dir=tmp_path / "owners", owner_home_dir=tmp_path / "owners/local/main")
    context = SimpleNamespace(agent=SimpleNamespace(home_paths=home,
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=0.01),
        scheduler_repository=SimpleNamespace(due_index=due_index.SchedulerDueIndex(
            home.global_index_dir / "scheduler_due.sqlite3")), _owner_wake_hints=None))
    state = SimpleNamespace(context=context, stop=threading.Event(), controllers={}, supervisors=[],
        hints=[], timers=[], errors=[], escaped=[])
    original_init = loops._OwnerWakeHints.__init__
    original_timer = loops.TimerService
    original_report = loops._print_gateway_loop_error

    def hint_init(self, current):
        state.hints.append(self)
        original_init(self, current)

    def timer(**kwargs):
        instance = original_timer(**kwargs)
        state.timers.append(instance)
        return instance

    def report(site, worker, exc):
        state.errors.append((site, worker, exc))
        original_report(site, worker, exc)

    class Controller:
        interval = 0.01

        def __init__(self, kind):
            assert kind not in state.controllers
            self.kind, self.ticks = kind, 0
            self.started, self.finished = threading.Event(), threading.Event()
            self._priority, self._base_home = {}, home
            state.controllers[kind] = self

        def tick(self):
            self.ticks += 1
            self.started.set()

        def run(self, stop):
            self.thread = threading.current_thread()
            try:
                loops._run_hinted_controller(self, stop,
                    loops._LoopSite(self.kind, self.kind, self.kind, self.interval))
            finally:
                self.finished.set()

    class Supervisor:
        poll_interval = 0.001

        def __init__(self, current):
            assert current is context
            self.ticks, self.closed = 0, 0
            state.supervisors.append(self)

        def tick(self):
            assert set(state.controllers) == {"reconcile", "maintenance", "scheduler_due"}
            assert all(controller.started.wait(1) for controller in state.controllers.values())
            self.ticks += 1
            if self.ticks == 3:
                state.stop.set()
            return False

        def shutdown(self):
            self.closed += 1

    monkeypatch.setattr(loops._OwnerWakeHints, "__init__", hint_init)
    monkeypatch.setattr(loops, "TimerService", timer)
    monkeypatch.setattr(loops, "_print_gateway_loop_error", report)
    monkeypatch.setattr(loops, "_BackgroundMainSupervisor", Supervisor)
    monkeypatch.setattr(loops, "_GatewayOrphanReconciler", lambda current: Controller("reconcile"))
    monkeypatch.setattr(loops, "_GatewayOwnerMaintenanceController", lambda current: Controller("maintenance"))
    monkeypatch.setattr(loops, "_GatewaySchedulerDueController", lambda current: Controller("scheduler_due"))
    before_wake = dict(input_lifecycle._WAKE_LISTENERS)
    due_key = str(context.agent.scheduler_repository.due_index.path)
    before_due = dict(due_index._DUE_LISTENERS.get(due_key, {}))
    binding_key = str(home.owners_dir.resolve())
    before_binding = owner_wake_discovery._ACTIVITY_BINDINGS.get(binding_key)
    yield state
    state.stop.set()
    for hint in state.hints:
        for remove in getattr(hint, "_removers", ()):
            remove()
        discovery = getattr(hint, "discovery", None)
        if discovery is not None:
            discovery.close()
        for event in getattr(hint, "events", {}).values():
            event.set()
    for timer_instance in state.timers:
        timer_instance.stop(1)
    for controller in state.controllers.values():
        if hasattr(controller, "thread"):
            controller.thread.join(1)
    assert dict(input_lifecycle._WAKE_LISTENERS) == before_wake
    assert dict(due_index._DUE_LISTENERS.get(due_key, {})) == before_due
    assert owner_wake_discovery._ACTIVITY_BINDINGS.get(binding_key) is before_binding


def run_background(state):
    def run():
        try:
            loops._gateway_background_main_loop(state.context, state.stop)
        except Exception as exc:
            state.escaped.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(3)
    try:
        assert not thread.is_alive(), "初始化故障不能让循环卡在重试中"
        assert not state.escaped, state.escaped
        assert state.stop.is_set()
        assert len(state.supervisors) == 1
        assert state.supervisors[0].ticks == 3
        assert state.supervisors[0].closed == 1
        assert set(state.controllers) == {"reconcile", "maintenance", "scheduler_due"}
        for controller in state.controllers.values():
            assert controller.ticks >= 1
            assert controller.finished.is_set()
            assert not controller.thread.is_alive()
        assert all(not timer.snapshot()["running"] for timer in state.timers)
    finally:
        state.stop.set()
        for hint in state.hints:
            for event in getattr(hint, "events", {}).values():
                event.set()
        thread.join(1)


@pytest.mark.parametrize("failure", ["once", "persistent", "corrupt"])
def test_activity_index_failure_starts_all_loops_hint_free(runtime, monkeypatch, failure):
    attempts = []
    original_connect = loops.OwnerActivityIndex._connect
    error = OSError("simulated owner activity index I/O error")
    if failure == "corrupt":
        path = runtime.context.agent.home_paths.global_index_dir / "owner_activity.sqlite3"
        path.write_bytes(b"not a sqlite database")

    def connect(index):
        attempts.append(index.path)
        if failure == "persistent" or failure == "once" and len(attempts) == 1:
            raise error
        return original_connect(index)

    monkeypatch.setattr(loops.OwnerActivityIndex, "_connect", connect)
    run_background(runtime)
    assert len(attempts) == 1, "降级不应重试构造或阻塞核心循环"
    assert len(runtime.hints) == 1 and len(runtime.timers) == 1
    assert len(runtime.errors) == 1
    assert runtime.errors[0][:2] == ("gateway_owner_wake_hints.initialize", "background-main")
    if failure != "corrupt":
        assert runtime.errors[0][2] is error
    assert runtime.context.agent._owner_wake_hints is None
    assert getattr(runtime.supervisors[0], "_wake_hints", None) is None
    assert all(getattr(controller, "_wake_hints", None) is None for controller in runtime.controllers.values())
    assert runtime.timers[0]._thread is None


@pytest.mark.parametrize("failure", ["wake_listener", "discovery"])
def test_partial_construction_removes_registered_listeners(runtime, monkeypatch, failure):
    handles, removed = [], []
    original_due = due_index.SchedulerDueIndex.add_due_listener
    original_wake = input_lifecycle.add_wake_listener
    error = OSError("simulated late hint initialization error")

    def track(remove):
        def cleanup():
            removed.append(remove)
            remove()
        handles.append(remove)
        return cleanup

    def add_due(index, listener):
        return track(original_due(index, listener))

    def add_wake(listener):
        if failure == "wake_listener":
            raise error
        return track(original_wake(listener))

    def fail_discovery(*args, **kwargs):
        raise error

    monkeypatch.setattr(due_index.SchedulerDueIndex, "add_due_listener", add_due)
    monkeypatch.setattr(input_lifecycle, "add_wake_listener", add_wake)
    monkeypatch.setattr(loops, "bind_owner_activity_index", fail_discovery)
    hint = object.__new__(loops._OwnerWakeHints)
    try:
        with pytest.raises(OSError) as caught:
            hint.__init__(runtime.context)
        assert caught.value is error
        assert removed == handles and handles
        assert hint._removers == []
        assert runtime.context.agent._owner_wake_hints is None
        assert runtime.timers[0]._thread is None
    finally:
        for remove in handles:
            remove()


@pytest.mark.parametrize("failure", ["bind_controller", "start"])
def test_activation_failure_closes_built_hints_before_fallback(runtime, monkeypatch, failure):
    original = getattr(loops._OwnerWakeHints, failure)
    original_close = loops._OwnerWakeHints.close
    closed = []
    error = OSError("simulated hint activation error")

    def fail(self, *args):
        original(self, *args)
        raise error

    def close(self):
        closed.append(self)
        original_close(self)

    monkeypatch.setattr(loops._OwnerWakeHints, failure, fail)
    monkeypatch.setattr(loops._OwnerWakeHints, "close", close)
    run_background(runtime)
    assert closed == runtime.hints and len(closed) == 1
    assert runtime.errors == [("gateway_owner_wake_hints.initialize", "background-main", error)]
    assert runtime.context.agent._owner_wake_hints is None
    assert getattr(runtime.supervisors[0], "_wake_hints", None) is None
    assert all(getattr(controller, "_wake_hints", None) is None for controller in runtime.controllers.values())


def test_healthy_hints_start_once_and_shutdown_removes_resources(runtime, monkeypatch):
    starts, closes = [], []
    original_start, original_close = loops._OwnerWakeHints.start, loops._OwnerWakeHints.close

    def start(self):
        starts.append(self)
        original_start(self)

    def close(self):
        closes.append(self)
        original_close(self)

    monkeypatch.setattr(loops._OwnerWakeHints, "start", start)
    monkeypatch.setattr(loops._OwnerWakeHints, "close", close)
    run_background(runtime)
    assert starts == closes == runtime.hints and len(starts) == 1
    assert runtime.errors == []
    assert runtime.hints[0]._closed_event.is_set()
    assert getattr(runtime.context.agent, "_owner_wake_hints", None) is None


# =============================================================================
# Source: agent_py_agent/tests/test_rework_r3_local_pagination.py
# =============================================================================

# LLM: 使用真实注册、canonical线程和任务；不注入观察reader或替换cursor验证。
# 函数用途: 为两条本地入口提供隔离的live Agent与101条规范任务。
@pytest.fixture
def live_agent(tmp_path, monkeypatch):
    return _live_agent(tmp_path, monkeypatch, ready=True)


def _live_agent(tmp_path, monkeypatch, *, ready):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True,
        business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path / 'workspace')
    store = agent.conversation_store
    threads = [store.threads.create(owner_id=agent.home_paths.owner_id,
        owner_home=str(agent.home_paths.owner_home_dir)) for _ in range(2)]
    tasks = [store.session_tasks.create(SessionTaskDraft(threads[0].thread_id,
        threads[1].thread_id, f'canonical task {number}')) for number in range(101)]
    assert len({task.task_id for task in tasks}) == 101
    # 此组核稳定快照的服务寿命；先让原有界冷回填追平，不能把跨批revision变化当缓存失效。
    if ready:
        ready_index(store)
    return agent, tasks


@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_cold_index_reports_partial_then_requires_fresh_snapshot(tmp_path, monkeypatch, entrypoint):
    agent, tasks = _live_agent(tmp_path, monkeypatch, ready=False)
    first = call_tasks(agent, entrypoint)
    assert first['error'] is None and first['data']['coverage'] == 'partial', first
    if first['data']['next_cursor']:
        assert_rejected(call_tasks(agent, entrypoint, cursor=first['data']['next_cursor']))
    # 回填不固定旧cursor；从真实入口请求新快照，不能把首次partial写成完整结果。
    ready_index(agent.conversation_store)
    result = call_tasks(agent, entrypoint, limit=100)
    assert result['error'] is None and result['data']['coverage'] == 'partial', result
    assert len(result['data']['items']) == 100 and result['data']['next_cursor']
    last = call_tasks(agent, entrypoint, cursor=result['data']['next_cursor'], limit=100)
    identifiers = [row['task_id'] for row in result['data']['items'] + last['data']['items']]
    assert last['error'] is None and last['data']['next_cursor'] is None
    assert last['data']['coverage'] == 'complete'
    assert len(identifiers) == len(set(identifiers)) == 101
    assert set(identifiers) == {task.task_id for task in tasks}


# LLM: 直接调用生产命令入口或原Registry中已注册工具；不手工持有服务来绕过寿命缺陷。
# 函数用途: 从实际入口发出同一冻结tasks.list请求并保留真实业务错误。
def call_tasks(agent, entrypoint, **params):
    request = {'action': 'tasks.list', 'request_id': 'local-pagination', 'params': params}
    if entrypoint == 'command':
        return api.run_business_command(agent, 'space', 'call ' + json.dumps(request))
    outcome = agent.tools.tools['business_tasks_list'].execute(request)
    result = json.loads(outcome.output)
    assert outcome.ok is (result['error'] is None), outcome
    if not outcome.ok and result.get('effect_outcome') == 'none':
        assert outcome.effect_outcome == 'not_started', outcome
    return result


# LLM: 每页均从实际入口重新进入，不接受静默从第一页重启或重复ID。
# 函数用途: 证明两个本地入口可完整遍历原101条任务的50/50/1三页。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_actual_local_entrypoints_traverse_101_canonical_tasks(live_agent, entrypoint):
    agent, tasks = live_agent
    cursor, pages = None, []
    for _ in range(3):
        result = call_tasks(agent, entrypoint, **({'cursor': cursor} if cursor else {}))
        assert result['error'] is None, result
        pages.append(result['data']['items'])
        cursor = result['data']['next_cursor']
        assert (cursor is not None) is (len(pages) < 3)
    identifiers = [row['task_id'] for page in pages for row in page]
    assert [len(page) for page in pages] == [50, 50, 1]
    assert len(identifiers) == len(set(identifiers)) == 101
    assert set(identifiers) == {task.task_id for task in tasks}
    assert result['data']['coverage'] == 'complete'
    print(f'{entrypoint}: page_sizes={[len(page) for page in pages]}, unique_ids={len(set(identifiers))}')


# LLM: 拒绝必须是原业务错误且不披露数据，不接受重启第一页冒充游标恢复。
# 函数用途: 统一检查失效游标及当前权限拒绝的真实返回。
def assert_rejected(result, code='CURSOR_EXPIRED', *, effect='not_started'):
    assert result['error']['code'] == code, result
    assert not result.get('data'), result
    assert result['effect_outcome'] == effect, result


# LLM: 只篡改真实生产签名的字节，不用测试签名器替代原HMAC。
# 函数用途: 证明两个入口仍拒绝伪造游标。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_tampered_cursor_is_rejected(live_agent, entrypoint):
    agent, _ = live_agent
    first = call_tasks(agent, entrypoint)
    cursor = first['data']['next_cursor']
    value = bytearray(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
    value[-1] ^= 1
    tampered = base64.urlsafe_b64encode(value).decode().rstrip('=')
    assert_rejected(call_tasks(agent, entrypoint, cursor=tampered))
    assert call_tasks(agent, entrypoint, cursor=cursor)['error'] is None


# LLM: 只推进观察层时钟；原cursor签发、过期判断及生产入口保持不变。
# 函数用途: 证明一小时过期规则仍在两个入口生效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_expired_cursor_is_rejected(live_agent, entrypoint, monkeypatch):
    from agent_py_agent.agent.conversation import business_observability as observation

    agent, _ = live_agent
    now = [1000.0]
    monkeypatch.setattr(observation, 'time', SimpleNamespace(time=lambda: now[0]))
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    now[0] += 3601
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor))


# LLM: 所有任务均queued，两过滤器选择相同记录仍须因scope不同而拒绝；limit不属于scope。
# 函数用途: 保持过滤器绑定，同时证明合法的分页大小变化没有被额外禁止。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_wrong_filter_rejected_but_limit_change_allowed(live_agent, entrypoint):
    agent, _ = live_agent
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor, filter={'include_completed': False}))
    result = call_tasks(agent, entrypoint, cursor=cursor, limit=1)
    assert result['error'] is None, result
    assert len(result['data']['items']) == 1


# LLM: 相等但不同的配置对象、原库包装对象和只读源组件都不能沿用旧服务签名寿命。
# 函数用途: 分别覆盖配置身份、Store身份、storage身份及canonical reader身份失效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
@pytest.mark.parametrize('change', ['config', 'store', 'storage', 'task_source', 'source_root'])
def test_configuration_and_source_identity_changes_invalidate(live_agent, entrypoint, change):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskStore
    from agent_py_agent.agent.conversation.store_layout import ConversationStorage

    agent, _ = live_agent
    first = call_tasks(agent, entrypoint)
    _, before = api.local_business_context(agent)
    store = agent.conversation_store
    if change == 'config':
        agent.config = replace(agent.config)
    elif change == 'store':
        agent.conversation_store = ConversationStore(store.storage.root, initialize=False)
    elif change == 'storage':
        store.storage = ConversationStorage(store.storage.root, initialize=False)
    elif change == 'task_source':
        store.session_tasks = SessionTaskStore(store.storage)
    else:
        store.storage.root = store.storage.root / 'changed-source'
    assert_rejected(call_tasks(agent, entrypoint, cursor=first['data']['next_cursor']))
    _, after = api.local_business_context(agent)
    assert after is not before
    fresh = call_tasks(agent, entrypoint)
    assert fresh['error'] is None, fresh
    assert fresh['data']['items'] == first['data']['items']


# LLM: 停用时必须拒绝当前请求；重新启用后新服务不能接受停用前的旧游标。
# 函数用途: 保持实时功能开关并验证同配置对象的enabled变化也会失效缓存。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_live_enabled_state_invalidates_without_restarting_host(live_agent, entrypoint):
    agent, _ = live_agent
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    agent.config.business_spaces_enabled = False
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor), 'HOST_NOT_READY')
    agent.config.business_spaces_enabled = True
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor))
    assert len(call_tasks(agent, entrypoint)['data']['items']) == 50


# LLM: 观察投影身份由宿主注入且仍使用原签名器；缓存不私自替换端口或给新端口续签旧页。
# 函数用途: 验证显式注入投影的保留、复用及替换失效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_injected_projection_is_preserved_and_replacement_invalidates(live_agent, entrypoint):
    from agent_py_agent.agent.conversation.business_observability import BusinessObservationService

    agent, _ = live_agent
    projections = [BusinessObservationService(agent.home_paths.owner_home_dir,
        owner_id=agent.home_paths.owner_id, store=agent.conversation_store) for _ in range(2)]
    agent.business_projection = projections[0]
    first = call_tasks(agent, entrypoint)
    _, services = api.local_business_context(agent)
    assert services.observation is projections[0]
    assert call_tasks(agent, entrypoint, cursor=first['data']['next_cursor'])['error'] is None
    agent.business_projection = projections[1]
    assert_rejected(call_tasks(agent, entrypoint, cursor=first['data']['next_cursor']))
    assert api.local_business_context(agent)[1].observation is projections[1]


# LLM: 签名有效不代表源数据未变；不缓存任务列表或弱化原revision检查。
# 函数用途: 证明canonical任务变化仍通过原观察层使旧页失效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_canonical_task_revision_change_invalidates_without_replacing_services(live_agent, entrypoint):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    agent, tasks = live_agent
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    _, services = api.local_business_context(agent)
    changed = agent.conversation_store.session_tasks.advance(tasks[0].task_id,
        SessionTaskUpdate('failed', failure_code='HOST_NOT_READY'))
    assert changed.status == 'failed'
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor))
    assert api.local_business_context(agent)[1] is services


# LLM: 身份仅来自原HostChannelActor专有参数与canonical通道绑定，不改授权函数。
# 函数用途: 为当前模型调用建立可被真实actor解析器复核的会话身份。
def set_current_actor(agent, thread_id, request_id):
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.conversation.models import ChannelBinding

    binding = ChannelBinding('feishu', 'pagination-chat', 'pagination-person',
        canonical_user_id='pagination-person', thread_id=thread_id)
    agent.conversation_store.threads.update_atomic(thread_id,
        lambda thread: replace(thread, channel_bindings=[binding]))
    actor = HostChannelActor(agent.home_paths.owner_id, thread_id, request_id,
        'feishu', 'pagination-chat', 'pagination-person')
    agent._current_run_params = SimpleNamespace(host_channel_actor=actor, task_id='',
        task_attributes={'conversation_thread_id': thread_id})
    return actor


# LLM: 切换真实owner解析结果后旧游标不能披露原owner任务；测试不用payload字段授予身份。
# 函数用途: 覆盖两入口的foreign-owner游标拒绝及新owner的canonical任务隔离。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_foreign_owner_cursor_is_rejected(live_agent, entrypoint):
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    agent, tasks = live_agent
    cursor = call_tasks(agent, entrypoint)['data']['next_cursor']
    home = resolve_owner_home(agent.home_paths.root, OwnerIdentity.local_user('pagination-bob'))
    agent.home_paths = home_paths_with_owner(agent.home_paths, home)
    store = agent.conversation_store
    source, target = [store.threads.create(owner_id=home.owner_id,
        owner_home=str(home.home_dir)) for _ in range(2)]
    own_task = store.session_tasks.create(SessionTaskDraft(source.thread_id, target.thread_id, 'bob task'))
    set_current_actor(agent, source.thread_id, 'bob-request')
    assert_rejected(call_tasks(agent, entrypoint, cursor=cursor))
    fresh = call_tasks(agent, entrypoint)
    assert fresh['error'] is None, fresh
    assert [row['task_id'] for row in fresh['data']['items']] == [own_task.task_id]
    assert not ({task.task_id for task in tasks} & {row['task_id'] for row in fresh['data']['items']})


# LLM: 每次发新上下文/actor/回调；旧context仍保留本次actor，不能借后来的合法actor续权。
# 函数用途: 证明缓存只复用服务，并保留当前canonical通道撤权和逐调用身份检查。
def test_model_tool_uses_fresh_actor_and_current_canonical_authority(live_agent, monkeypatch):
    agent, tasks = live_agent
    source = tasks[0].sender_thread_id
    actor_one = set_current_actor(agent, source, 'first-request')
    contexts = []
    original = api.bind_business_context

    # LLM: 只记录真实issuer返回值，不替换认证、领域调用或任何返回数据。
    # 函数用途: 观察两个生产调用分别发行的上下文对象。
    def record_context(*args, **kwargs):
        context = original(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(api, 'bind_business_context', record_context)
    first = call_tasks(agent, 'model_tool')
    _, services = api.local_business_context(agent)
    actor_two = set_current_actor(agent, source, 'second-request')
    second = call_tasks(agent, 'model_tool', cursor=first['data']['next_cursor'])
    assert second['error'] is None, second
    issued_one, issued_two = contexts[0], contexts[-1]
    assert issued_one is not issued_two
    assert issued_one.authorize is not issued_two.authorize
    assert issued_one.actor == actor_one and issued_two.actor == actor_two
    assert api.local_business_context(agent)[1] is services
    assert services.observation_actor(issued_two) is not services.observation_actor(issued_two)
    with pytest.raises(api.HostAPIError) as denied:
        issued_one.check('tasks.list', {})
    assert denied.value.code == 'CAPABILITY_DENIED'
    agent.conversation_store.threads.update_atomic(source,
        lambda thread: replace(thread, channel_bindings=[]))
    assert_rejected(call_tasks(agent, 'model_tool', cursor=second['data']['next_cursor']), 'CHANNEL_ACTOR_SCOPE_MISMATCH')
    with pytest.raises(api.HostAPIError) as denied:
        issued_two.check('tasks.list', {})
    assert denied.value.code == 'CAPABILITY_DENIED'


# LLM: 查询过程中原绑定被撤销时，末核仍须隐藏数据；复用服务不意味着复用首次授权决定。
# 函数用途: 覆盖已进入观察查询后的实时撤权。
def test_model_tool_rechecks_authority_after_observation(live_agent, monkeypatch):
    agent, tasks = live_agent
    source = tasks[0].sender_thread_id
    set_current_actor(agent, source, 'revoked-during-query')
    _, services = api.local_business_context(agent, model=True)
    original = services.observation.query
    calls = []

    # LLM: 先执行原查询，再通过原线程存储撤销绑定；不伪造业务结果或错误。
    # 函数用途: 在生产末核之前制造真实当前权限变化。
    def revoke_after_query(*args, **kwargs):
        calls.append(args[0])
        result = original(*args, **kwargs)
        agent.conversation_store.threads.update_atomic(source,
            lambda thread: replace(thread, channel_bindings=[]))
        return result

    monkeypatch.setattr(services.observation, 'query', revoke_after_query)
    # 原业务边界已经完成只读查询，JSON必须保留none；外层工具合同另核not_started。
    assert_rejected(call_tasks(agent, 'model_tool'), 'CAPABILITY_DENIED', effect='none')
    assert calls == ['tasks.list']


# LLM: 同canonical home的不同拼写不是新owner；规范化也必须与execute和观察actor的归属核验一致。
# 函数用途: 证明同一home的等价路径保留游标，而真正换home会失效。
@pytest.mark.parametrize('entrypoint', ['command', 'model_tool'])
def test_owner_home_uses_canonical_path_and_invalidates_on_change(live_agent, entrypoint, tmp_path):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    agent, _ = live_agent
    first = call_tasks(agent, entrypoint)
    _, services = api.local_business_context(agent)
    home = agent.home_paths.owner_home_dir
    agent.home_paths = replace(agent.home_paths, owner_home_dir=home / '..' / home.name)
    assert api.local_business_context(agent)[1] is services
    assert call_tasks(agent, entrypoint, cursor=first['data']['next_cursor'])['error'] is None
    root = tmp_path / 'another-home'
    agent.home_paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root))
    assert_rejected(call_tasks(agent, entrypoint, cursor=first['data']['next_cursor']))
    assert api.local_business_context(agent)[1] is not services


# LLM: 即便home、owner、config和Store全相同，另一host也不共享签名器或缓存对象。
# 函数用途: 保持独立宿主/CLI生命周期之间没有全局cursor key。
def test_services_and_cursors_do_not_escape_the_live_host(live_agent):
    agent, _ = live_agent
    cursor = call_tasks(agent, 'command')['data']['next_cursor']
    other = SimpleNamespace(home_paths=agent.home_paths, config=agent.config,
        conversation_store=agent.conversation_store)
    first, second = api.local_business_context(agent), api.local_business_context(other)
    assert first[0] is not second[0] and first[1] is not second[1]
    assert first[1].observation is not second[1].observation
    assert_rejected(call_tasks(other, 'command', cursor=cursor))


# LLM: bool为False的显式宿主端口仍是供应对象，不等于缺失，不应静默生成另一服务。
# 类用途: 仅用来验证constructor的None与显式供应值边界。
class FalseyPort(SimpleNamespace):
    # LLM: 本测试值故意为False，不影响其作为供应端口的身份。
    # 函数用途: 暴露不应由or表达式覆盖的合法显式注入。
    def __bool__(self):
        return False


# LLM: 构造器显式参数和宿主注入都保留原对象，不走本地缓存来覆盖供应服务。
# 函数用途: 回归显式spaces/workflow/background/capability/projection供应合同。
def test_supplied_services_are_preserved(live_agent):
    agent, _ = live_agent
    provided = {name: FalseyPort(enabled=True) for name in
        ('spaces', 'workflows', 'background', 'capabilities', 'projection')}
    services = api.BusinessServices(agent, **provided)
    for name, port in provided.items():
        assert getattr(services, name) is port
    agent.business_background = provided['background']
    agent.business_capabilities = provided['capabilities']
    agent.business_projection = provided['projection']
    context, cached = api.local_business_context(agent)
    again, reused = api.local_business_context(agent)
    assert reused is cached and context is not again
    for name in ('background', 'capabilities', 'projection'):
        assert getattr(cached, name) is provided[name]


# LLM: 同一宿主并发首次进入也只发表一个服务实例；锁不保存调用身份或授权结果。
# 函数用途: 覆盖服务缓存的并发发行与每次context唯一性。
def test_concurrent_contexts_share_only_one_service(live_agent):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    agent, _ = live_agent
    agent.config = replace(agent.config)
    barrier = Barrier(4)

    # LLM: 同步启动多个真实本地context发行，不替换服务构造。
    # 函数用途: 同时观察缓存失效后第一次重建的结果。
    def enter(_):
        barrier.wait(timeout=10)
        return api.local_business_context(agent)

    with ThreadPoolExecutor(max_workers=4) as pool:
        issued = list(pool.map(enter, range(4)))
    assert len({id(context) for context, _ in issued}) == 4
    assert len({id(services) for _, services in issued}) == 1


# =============================================================================
# Source: agent_py_agent/tests/test_rework_r3_plugin_sources.py
# =============================================================================

_KINDS = ("legacy-wide", "restricted", "v8")
# 插件程序永不执行：每次启动都在进程边界被替身截住。
_PROGRAM = b"raise RuntimeError('fixture plugin must not execute')\n"
# Linux 单文件遮罩的前两个参数；第三个参数是被遮罩的 namespace 位置。
_LINUX_MASK = ["--ro-bind", bwrap_compiler.HIDDEN_FILE_MASK_SOURCE]


# 类用途: 一次用例的目录布局；HOME、数据根与两份外部来源互不包含，同级业务文件用来证明不隐藏父目录。
@dataclass(frozen=True)
class _Layout:
    tmp: Path
    data_root: Path
    credential: Path
    config_file: Path
    sibling: Path

    @property
    def sources(self) -> tuple[Path, ...]:
        return (self.credential, self.config_file)


# 类用途: 进程边界上观察到的预检/启动规格、编译出的启动命令和构造的插件客户端。
@dataclass
class _Boundary:
    specs: list = field(default_factory=list)
    launches: list = field(default_factory=list)
    clients: list = field(default_factory=list)


# 函数用途: 建立临时 HOME、数据根与外部凭据目录，并确认外部来源同时位于两者之外。
def _layout(tmp_path: Path, monkeypatch) -> _Layout:
    tmp = tmp_path.resolve()
    os_home, data_root = tmp / "os-home", tmp / "home"
    credential = tmp / "external-credentials" / "feishu-app-secret"
    config_file = tmp / "deploy" / "agent.yaml"
    for directory in (os_home, data_root, credential.parent, config_file.parent):
        directory.mkdir(parents=True)
    credential.write_text("fixture-not-a-secret\n")
    sibling = credential.with_name("business-notes.txt")
    sibling.write_text("ordinary business file next to the credential\n")
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("MY_AGENT_HOME", str(data_root))
    for source in (credential, config_file):
        assert not source.is_relative_to(Path.home()) and not source.is_relative_to(data_root)
    return _Layout(tmp, data_root, credential, config_file, sibling)


# 函数用途: 写外部部署配置并经真实加载器读回；三种策略只差沙箱/事件开关，宿主来源相同。
def _config(layout: _Layout, kind: str, **extra):
    settings = {"feishu_app_secret": f"file:{layout.credential}", "plugin_process_sandbox": kind == "legacy-wide",
                "plugin_legacy_sandbox_default": kind != "legacy-wide", "plugin_events_enabled": kind == "v8", **extra}
    layout.config_file.write_text("".join(f"{key}: {json.dumps(value)}\n" for key, value in settings.items()))
    config = load_config(layout.config_file)
    assert {str(source) for source in layout.sources} <= set(config.host_exclusive_path_roots)
    return config


# 函数用途: 经真实 plugin_management_context 组装管理服务，宿主来源只来自上面的真实配置。
def _management(layout: _Layout, config, kind: str) -> PluginManagement:
    owner = resolve_owner_home(layout.data_root)
    threads = ConversationStore(owner.home_dir / "conversations", initialize=False).threads
    home = home_paths_with_owner(home_paths(layout.data_root), owner)
    context = plugin_management_context(owner, home, config, threads, actor_id="tester", channel="chat",
                                        conversation_id="session", is_admin=True)
    assert set(layout.sources) <= set(context.host_exclusive_roots)
    if kind == "v8":
        # 本机管理员资格判定不在本题范围；宿主来源仍完全来自真实配置投影。
        context = replace(context, events_owner_allowed=True)
    return PluginManagement(context)


# 函数用途: 经原管理命令安装老格式 v6 或 v8 包（安装本身不启动插件）；包放在原路径策略授权的会话工作区里。
def _install(service: PluginManagement, kind: str) -> None:
    workspace = service.context.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    if kind == "v8":
        source = workspace / "event-plugin.zip"
        source.write_bytes(v8_bundle(_v8()))
    else:
        source = _package(workspace, _declaration(), {"bin/server.py": _PROGRAM})
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")
    assert result["state"] == "succeeded", result


# 函数用途: 固定 Darwin 平台，记录全部预检与启动规格、真实编译的启动命令和构造出的插件客户端。
def _launch_boundary(patch) -> _Boundary:
    boundary = _Boundary()
    patch.setattr(plugin_sandbox.platform, "system", lambda: "Darwin")
    patch.setattr(AttemptExecutionSandbox, "require_ready", lambda self: boundary.specs.append(self.spec))

    # 真实包装函数；只补上永不执行的 sandbox-exec 路径，任何车道都编译同一份 Seatbelt 规则。
    def launch(argv, spec):
        command = plugin_sandbox.sandboxed_plugin_argv(argv, replace(spec, macos_sandbox_exec="/usr/bin/sandbox-exec"))
        boundary.launches.append(command)
        return command

    patch.setattr(plugin_runtime, "sandboxed_plugin_argv", launch)
    initialize = PluginMCPClient.__init__

    def observe(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        boundary.clients.append(self)

    patch.setattr(PluginMCPClient, "__init__", observe)
    return boundary


# 函数用途: 固定 Linux 平台与替身 bwrap 路径，记录真实包装函数编译出的每条插件启动命令；bwrap 本身从不执行。
def _linux_launch_boundary(patch) -> _Boundary:
    boundary = _Boundary()
    patch.setattr(plugin_sandbox.platform, "system", lambda: "Linux")
    patch.setattr(AttemptExecutionSandbox, "require_ready", lambda self: boundary.specs.append(self.spec))
    patch.setattr(bwrap_compiler, "_PROC_MOUNT_KIND", "dir")

    def launch(argv, spec):
        command = plugin_sandbox.sandboxed_plugin_argv(argv, replace(spec, bwrap_path="/fixture/bwrap"))
        boundary.launches.append(command)
        return command

    patch.setattr(plugin_runtime, "sandboxed_plugin_argv", launch)
    return boundary


# 函数用途: 断言 Linux 启动命令以精确单文件遮罩隐藏外部来源：遮罩块紧贴插件目录覆盖之前，同级业务文件与插件 argv 不含来源。
def _assert_linux_masks_before_overlays(command: list[str], layout: _Layout) -> None:
    end = command.index("--chdir")
    overlays = 0
    while end >= 4 and command[end - 4] == "--tmpfs" and command[end - 2:end] == ["--remount-ro", command[end - 3]]:
        end, overlays = end - 4, overlays + 1
    assert overlays, f"插件目录覆盖必须紧跟单文件遮罩: {command}"
    masked = []
    while end >= 3 and command[end - 3:end - 1] == _LINUX_MASK:
        masked.append(command[end - 1])
        end -= 3
    # 遮罩只出现在这一块：其前是全部业务与保护挂载，其后只有插件目录覆盖和 --chdir。
    assert sum(command[index:index + 2] == _LINUX_MASK for index in range(len(command) - 1)) == len(masked)
    assert {str(source) for source in layout.sources} <= set(masked), command
    assert str(layout.sibling) not in command
    plugin_argv = command[command.index("--") + 1:]
    assert not any(str(source) in part for part in plugin_argv for source in layout.sources)


# 函数用途: 候选连接只替换进程收发；客户端构造（含规格）与原启用链照常执行。
def _no_candidate_process(patch) -> None:
    patch.setattr(PluginMCPClient, "start", lambda _self: object())
    patch.setattr(PluginMCPClient, "discover_tools", lambda _self, _transport: ())
    patch.setattr(PluginMCPClient, "stop", lambda _self: SimpleNamespace(
        confirmed=True, record={"session_id": "fixture-candidate"}, terminations=()))


# 函数用途: 按真实两步确认协议启用插件，返回预览与成功回执。
def _enable_plugin(service: PluginManagement, kind: str) -> tuple[dict, dict]:
    preview = _enable(service, "enable")
    assert preview["details"]["reason"] == "confirmation_required", preview
    if kind == "v8":
        enabled = _enable(service, "confirm", preview["details"]["confirmation"]["confirm_code"])
    else:
        enabled = confirm_legacy_enable(service, preview)
    assert enabled["state"] == "succeeded", enabled
    return preview, enabled


# 函数用途: 安装并启用插件（候选不起进程），供运行期启动路径的用例使用。
def _enabled_service(layout: _Layout, config, kind: str, monkeypatch) -> PluginManagement:
    service = _management(layout, config, kind)
    _install(service, kind)
    with monkeypatch.context() as patch:
        _launch_boundary(patch)
        _no_candidate_process(patch)
        _enable_plugin(service, kind)
    return service


# 函数用途: 用原 MCP 启动/重连链但替换传输，记录每次实际启动使用的完整命令。
def _fake_transport(patch) -> list[list[str]]:
    launched: list[list[str]] = []

    def start_transport(client):
        closed = threading.Event()
        transport = SimpleNamespace(inbox=SimpleNamespace(closed=closed), ready=True,
                                    is_running=lambda: not closed.is_set(), revoke=closed.set,
                                    terminate=lambda **_: ProcessTerminationReceipt("fixture-transport", True, 0, 1))
        client._transport, client._starting = transport, False
        launched.append([client.config.command, *client.config.args])
        return transport

    patch.setattr(MCPStdioClient, "_start_transport", start_transport)
    return launched


# 函数用途: 规格隐藏集合（真实路径），用于集合比较。
def _hidden(spec) -> frozenset[Path]:
    return frozenset(Path(path).resolve() for path in spec.hidden_paths)


# 函数用途: 断言外部来源按精确文件留在隐藏集合里，既不丢失，也不扩成父目录或同级业务文件。
def _assert_exact_sources(spec, layout: _Layout) -> None:
    hidden = _hidden(spec)
    for source in layout.sources:
        assert source in hidden, (source, sorted(hidden))
        assert not any(path in source.parents for path in hidden), "宿主来源不能扩成父目录"
    assert not any(path == layout.sibling or path in layout.sibling.parents for path in hidden)


# 函数用途: 断言编译出的 Seatbelt 规则精确拒绝来源文件，插件自身 argv 不含来源路径。
def _assert_compiled_exactly(command: list[str], layout: _Layout) -> None:
    profile = command[command.index("-p") + 1]
    for source in layout.sources:
        literal = json.dumps(str(source), ensure_ascii=False)
        assert f"(deny file-read* file-write* (subpath {literal}))" in profile
        assert f"(deny process-exec (subpath {literal}))" in profile
        assert f"(subpath {json.dumps(str(source.parent), ensure_ascii=False)})" not in profile
    assert str(layout.sibling) not in profile
    plugin_argv = command[command.index("--") + 1:]
    assert not any(str(source) in part for part in plugin_argv for source in layout.sources)


# 函数用途: 断言来源只进隐藏规格：客户端冻结了它们，但插件原始 argv 与 env 里都没有。
def _assert_client_sources(client: PluginMCPClient, layout: _Layout) -> None:
    assert set(layout.sources) <= set(client._host_exclusive_roots)
    _assert_exact_sources(client._source_sandbox_spec, layout)
    exposed = [*client._source_raw_argv, *client.config.env.values()]
    assert not any(str(source) in str(value) for value in exposed for source in layout.sources)


# 函数用途: 断言公开回执不含宿主来源路径。
def _assert_payload_clean(payload: dict, layout: _Layout) -> None:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    assert not any(str(source) in text for source in layout.sources)


# 函数用途: 经真实 start → 断开 → reconnect 两次实际启动，确认两次都按冻结来源编译且规格不变。
def _assert_reconnect_keeps_sources(client: PluginMCPClient, launched: list, layout: _Layout) -> None:
    first = client.start()
    first.revoke()
    second = client.reconnect()
    assert second is not first and len(launched) == 2
    _assert_exact_sources(client._source_sandbox_spec, layout)
    for command in launched:
        _assert_compiled_exactly(command, layout)


@pytest.mark.parametrize("kind", _KINDS)
def test_management_preflight_and_candidate_hide_external_file_sources(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    service = _management(layout, _config(layout, kind), kind)
    _install(service, kind)
    with monkeypatch.context() as patch:
        boundary = _launch_boundary(patch)
        _no_candidate_process(patch)
        preview, enabled = _enable_plugin(service, kind)
    (candidate,) = boundary.clients
    _assert_client_sources(candidate, layout)
    (launch,) = boundary.launches
    _assert_compiled_exactly(launch, layout)
    # 预览与确认两次启用预检、客户端内部就绪门与实际启动规格隐藏同一集合，预检不是另一套推导。
    assert len(boundary.specs) >= 3
    assert {_hidden(spec) for spec in boundary.specs} == {_hidden(candidate._source_sandbox_spec)}
    for payload in (preview, enabled):
        _assert_payload_clean(payload, layout)


@pytest.mark.parametrize("kind", _KINDS)
def test_business_invocation_client_hides_external_file_sources(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    service = _enabled_service(layout, _config(layout, kind), kind, monkeypatch)
    target = service.context.workspace / "input.txt"
    target.write_text("business input\n")

    # 业务连接只在实际起进程处截住；客户端构造与业务预检照常执行。
    def no_process(_self):
        raise MCPError("fixture business plugin process is not started", code="MCP_SERVER_START_FAILED")

    with monkeypatch.context() as patch:
        boundary = _launch_boundary(patch)
        patch.setattr(PluginMCPClient, "start", no_process)
        result = service.command(f'/plugins@{PLUGIN_ID} read "{target}"', revision=service.catalog().revision,
                                 request_id="business")
    assert result["state"] != "succeeded", result
    (client,) = boundary.clients
    _assert_client_sources(client, layout)
    _assert_compiled_exactly(boundary.launches[-1], layout)
    assert {_hidden(spec) for spec in boundary.specs} == {_hidden(client._source_sandbox_spec)}
    _assert_payload_clean(result, layout)


@pytest.mark.parametrize("kind", _KINDS)
def test_registry_adapter_and_sync_keep_external_file_sources_across_reconnect(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent import core

    layout = _layout(tmp_path, monkeypatch)
    config = _config(layout, kind, model_backend="echo", my_agent_home=str(layout.data_root))
    _enabled_service(layout, config, kind, monkeypatch)
    # core 的字段敏感适配器只在 ToolRegistryParams 声明该字段时转运来源（core.py 本题只读）。
    assert "host_exclusive_paths" in ToolRegistryParams.__dataclass_fields__
    adapted = core._tool_registry_host_exclusive_kwargs(home_paths(layout.data_root), config)
    assert set(layout.sources) <= set(adapted["host_exclusive_paths"])
    agent = core.SimpleAgent(config, layout.tmp)
    try:
        params = agent.tools._construction_params
        assert set(layout.sources) <= set(params.host_exclusive_paths)
        assert "host_exclusive_paths" not in repr(params)
        with monkeypatch.context() as patch:
            _launch_boundary(patch)
            launched = _fake_transport(patch)
            synchronize_plugin_clients(agent.tools)
            (client,) = [item for item in agent.tools._mcp_clients if isinstance(item, PluginMCPClient)]
            _assert_client_sources(client, layout)
            _assert_reconnect_keeps_sources(client, launched, layout)
    finally:
        agent.tools.close_mcp_clients()


@pytest.mark.parametrize("kind", _KINDS)
def test_gateway_shared_pool_factory_keeps_external_file_sources_across_reconnect(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    config = _config(layout, kind)
    service = _enabled_service(layout, config, kind, monkeypatch)
    entry = service.installations.snapshot()[0]
    server = SimpleNamespace(agent=SimpleNamespace(config=config), plugin_channel_pool=None,
                             plugin_display=None, plugin_event_hub=None)
    client = None
    try:
        pool = plugin_panels_http.plugin_channel_pool(server)
        # 面板服务与事件中心取同一个池，所以这一个 factory 冻结的来源覆盖两条启动路径。
        assert plugin_panels_http.plugin_display_service(server)._pool is pool
        plugin_panels_http.plugin_event_hub(server)
        assert server.plugin_channel_pool is pool
        assert set(layout.sources) <= set(pool.client_factory.keywords["host_exclusive_roots"])
        with monkeypatch.context() as patch:
            _launch_boundary(patch)
            launched = _fake_transport(patch)
            client = pool.client_factory(service.context.owner, entry)
            _assert_client_sources(client, layout)
            _assert_reconnect_keeps_sources(client, launched, layout)
    finally:
        if client is not None:
            client.stop()
        plugin_panels_http.close_plugin_channel(server)


def test_linux_preflight_accepts_file_sources_and_launch_wrapper_masks_them(tmp_path, monkeypatch):
    layout = _layout(tmp_path, monkeypatch)
    roots = configured_host_source_paths(_config(layout, "legacy-wide"))
    owner_home = resolve_owner_home(layout.data_root).home_dir
    owner_home.mkdir(parents=True)
    # 已存在的宿主独占目录仍由插件包装在单文件遮罩之后再盖一次 tmpfs。
    (owner_home / "config").mkdir()
    monkeypatch.setattr(AttemptExecutionSandbox, "require_ready", lambda _self: None)
    monkeypatch.setattr(plugin_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bwrap_compiler, "_PROC_MOUNT_KIND", "dir")
    assert plugin_sandbox.plugin_sandbox_problem(True, owner_home) == ""
    assert plugin_sandbox.plugin_sandbox_problem(True, owner_home, host_exclusive_roots=roots) == ""
    spec = plugin_sandbox.plugin_sandbox_spec(cwd=owner_home, data_dir=owner_home, owner_home=owner_home,
                                              host_exclusive_roots=roots)
    # 来源既不丢弃也不扩成父目录：规格仍按精确文件保留，启动命令逐个遮罩。
    _assert_exact_sources(spec, layout)
    command = plugin_sandbox.sandboxed_plugin_argv(["/usr/bin/true"], replace(spec, bwrap_path="/usr/bin/bwrap"))
    _assert_linux_masks_before_overlays(command, layout)


@pytest.mark.parametrize("kind", _KINDS)
def test_linux_file_sources_let_enable_reach_confirmation(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    service = _management(layout, _config(layout, kind), kind)
    _install(service, kind)
    monkeypatch.setattr(AttemptExecutionSandbox, "require_ready", lambda _self: None)
    monkeypatch.setattr(plugin_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(PluginMCPClient, "__init__", lambda *_args, **_kwargs: pytest.fail("确认前不能构造候选客户端"))
    preview = _enable(service, "enable-linux")
    # 外部单文件来源不再让 Linux 预检拒绝；启用照常进入两步确认，确认前不写激活、不备环境，回执不含来源路径。
    assert preview["error_code"] == "PLUGIN_CONFIRMATION_REQUIRED", preview
    assert preview["details"]["reason"] == "confirmation_required", preview
    assert service.installations.snapshot()[0].activation is None
    environments = service.context.owner.plugins_dir / "environments"
    assert not environments.exists() or not any(environments.iterdir())
    _assert_payload_clean(preview, layout)


@pytest.mark.parametrize("kind", _KINDS)
def test_linux_business_and_shared_pool_launch_mask_file_sources(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    config = _config(layout, kind)
    service = _enabled_service(layout, config, kind, monkeypatch)
    entry = service.installations.snapshot()[0]
    (service.context.owner.home_dir / "config").mkdir(exist_ok=True)
    target = service.context.workspace / "input.txt"
    target.write_text("business input\n")

    # 业务连接只在实际起进程处截住；预检、客户端构造与 bwrap 参数编译照常执行。
    def no_process(_self):
        raise MCPError("fixture business plugin process is not started", code="MCP_SERVER_START_FAILED")

    with monkeypatch.context() as patch:
        boundary = _linux_launch_boundary(patch)
        patch.setattr(PluginMCPClient, "start", no_process)
        result = service.command(f'/plugins@{PLUGIN_ID} read "{target}"', revision=service.catalog().revision,
                                 request_id="business-linux")
        assert result["state"] != "succeeded", result
        assert "host_exclusive_file_isolation_unavailable" not in json.dumps(result, default=str), result
        (business,) = boundary.launches
        _assert_linux_masks_before_overlays(business, layout)
        _assert_payload_clean(result, layout)
        shared = plugin_display_client(service.context.owner, entry, process_sandbox=kind == "legacy-wide",
                                       host_exclusive_roots=configured_host_source_paths(config))
        assert [shared.config.command, *shared.config.args] == boundary.launches[-1]
        _assert_linux_masks_before_overlays(boundary.launches[-1], layout)


def test_host_sources_stay_out_of_policy_and_registry_reprs(tmp_path):
    source = tmp_path / "external-credentials" / "feishu-app-secret"
    policy = PluginEnablePolicy(host_exclusive_roots=(source,))
    params = ToolRegistryParams(tmp_path, 1, 1, 1, 1, 1, 1, 1, False, host_exclusive_paths=(source,))
    assert policy.host_exclusive_roots == (source,) and params.host_exclusive_paths == (source,)
    assert str(source) not in repr(policy) and str(source) not in repr(params)


# =============================================================================
# Source: agent_py_agent/tests/test_rework_r4_network_contract.py
# =============================================================================

# LLM: Each test owns an authenticated real listener, disposable queue and random in-memory token; no existing daemon or G1 client credential is used.
# 函数用途: 准备真实认证服务与普通私有通道用户，测试结束关闭原监听器。
@pytest.fixture
def gateway(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
        gateway_per_user_owner_scoping=True, gateway_native_stream_max=2), tmp_path / "workspace")
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses, paths.terminal):
        folder.mkdir(parents=True, exist_ok=True)
    middleware = AuthMiddleware(AuthManager(), secrets.token_urlsafe(24))
    assert middleware.auth_manager.authenticate("tui", "alice").role is Role.USER
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent, auth_middleware=middleware))
    server.start()
    state = SimpleNamespace(agent=agent, paths=paths, server=server, middleware=middleware,
        url=f"http://127.0.0.1:{server.server.server_address[1]}")
    try:
        yield state
    finally:
        server.stop()


# LLM: Explicit providers isolate the other regressions from the configured-token bug; identity still comes solely from the original projector.
# 函数用途: 创建真实网络客户端，按需使用显式远程头并绑定原 GET/POST 合同。
def client(gateway, *, user="alice", channel="tui", provider=True):
    agent = SimpleNamespace(config=SimpleNamespace(gateway_auth_token=gateway.middleware.auth_token,
        gateway_request_timeout=5), gateway_request_identity=lambda: {"user_id": user, "channel": channel})
    if provider:
        agent.gateway_network_headers = lambda: {AuthMiddleware.HEADER_TOKEN: gateway.middleware.auth_token}
    observer = NetworkGatewayObserver(agent, gateway.url)
    observer.bind_client_transport()
    return observer


# LLM: The authoritative hot request freezes exact user/channel/conversation and the current lease before any writer event.
# 函数用途: 在测试队列建立活动请求，供原 ACL、pending 审批和归档逻辑共同读取。
def hot_request(gateway, *, request_id="r", user="alice", channel="tui", session="session-a"):
    scope = GatewayControlScope(user, channel, session)
    payload = {**_scope_request_payload(scope), "id": request_id, "request_id": request_id,
        "execution_attempt_id": "attempt-a", "lease_started_at": time.time(), "status": "processing"}
    path = gateway.paths.processing / f"{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return SimpleNamespace(scope=scope, payload=payload, path=path,
        chunk=gateway_chunk_path(gateway.paths, request_id), request_id=request_id)


# LLM: Only the real canonical archive commits completion; the response file alone is never a terminal fact.
# 函数用途: 提交真实终态，使网络结果路由按原权限投影返回。
def finish(gateway, record):
    result = {**record.payload, "ok": True, "response": "finished"}
    archived = archive_request(gateway.paths, record.path, gateway.paths.done, record.request_id,
        terminal_response=result)
    assert archived.disposition.value in {"committed", "already_committed"}, archived


# LLM: A poll request's paths provide identifiers only; the network observer must get all payloads through HTTP.
# 函数用途: 记录网络事件和计数，不以客户端文件读取替代服务端路由。
def poll_request(gateway, record, consume):
    return GatewayChunkPollRequest(record.chunk, gateway.paths.terminal / f"{record.request_id}.json",
        time.time() + 5, lambda _: None, [0], [0], on_event=consume)


def test_configured_token_authenticates_real_listener(gateway):
    record = hot_request(gateway)
    finish(gateway, record)
    observer = client(gateway, provider=False)
    status, body = observer.request_json("GET", "/result/r")
    assert status == 200
    assert body["response"] == "finished"
    assert "Authorization" not in observer._headers()
    assert observer._headers()[AuthMiddleware.HEADER_TOKEN] == gateway.middleware.auth_token


def test_provider_cannot_replace_original_identity(gateway):
    observer = client(gateway)
    observer.agent.gateway_network_headers = lambda: {AuthMiddleware.HEADER_TOKEN: gateway.middleware.auth_token,
        "x-user-id": "admin", "x-channel": "chat", "Authorization": "custom-provider-value"}
    headers = observer._headers()
    assert headers["X-User-Id"] == "alice"
    assert headers["X-Channel"] == "tui"
    assert headers["Authorization"] == "custom-provider-value"


def test_control_receipt_round_trip_keeps_exact_conversation(gateway):
    observer = client(gateway)
    status, accepted = observer.agent.post_gateway_json("/control", {"command": "/status",
        "conversation_id": "session-a", "metadata": {"message_id": "receipt-r4"}}, timeout=5)
    assert status == 200, accepted
    operation_id = accepted["operation_id"]
    path = f"/control-status/{operation_id}"
    status, receipt = observer.agent.get_gateway_json(path, timeout=5, conversation_id="session-a")
    assert status == 200, receipt
    assert receipt["operation_id"] == operation_id
    assert observer.agent.get_gateway_json(path, timeout=5, conversation_id="session-b")[0] in {403, 404}
    assert client(gateway, user="bob").agent.get_gateway_json(path, timeout=5,
        conversation_id="session-a")[0] in {403, 404}


def test_terminal_http_drain_emits_all_450_real_capped_progress_rows(gateway):
    record = hot_request(gateway)
    for number in range(450):
        write_chunk_event(record.chunk, {"kind": "assistant_commentary", "text": f"row-{number}"})
    finish(gateway, record)
    observer = client(gateway)
    status, page = observer.request_json("GET", "/progress/r?since=0")
    assert status == 200
    assert len(page["events"]) == 200 and page["next"] == 200
    observer._http_only = True
    emitted = []
    request = poll_request(gateway, record, lambda event: emitted.append(event) or True)
    assert observer(request)["response"] == "finished"
    assert [event["text"] for event in emitted] == [f"row-{number}" for number in range(450)]
    assert observer.line_cursor == 450
    assert request.chunks_printed_ref == [450]


def test_tail_appended_and_terminal_committed_between_reads_is_emitted(gateway, monkeypatch):
    record = hot_request(gateway)
    write_chunk_event(record.chunk, {"kind": "assistant_commentary", "text": "before"})
    observer = client(gateway)
    observer._http_only = True
    actual = observer.request_json
    pages = []

    def append_after_first_page(method, path, payload=None, **kwargs):
        response = actual(method, path, payload, **kwargs)
        if path.startswith("/progress/"):
            pages.append(response[1])
            if len(pages) == 1:
                write_chunk_event(record.chunk, {"kind": "assistant_commentary", "text": "late tail"})
                finish(gateway, record)
        return response

    monkeypatch.setattr(observer, "request_json", append_after_first_page)
    emitted = []
    result = observer(poll_request(gateway, record, lambda event: emitted.append(event) or True))
    assert result["response"] == "finished"
    assert [event["text"] for event in emitted] == ["before", "late tail"]
    assert observer.line_cursor == 2
    assert pages[-1]["next"] == 2 and not pages[-1]["events"]


# LLM: Build a canonical request from the same ToolCall contract as the executor; labels and parameters cannot select authorization.
# 函数用途: 生成带完整 binding 与有限选项的真实前台审批 DTO。
def approval_request(record, *, call_id="call-r4", call_index=1):
    return build_tool_approval_request(ToolCall(call_id=call_id, tool_name="controlled_exec",
        arguments={"command": ["python3", "-V"]}, source_protocol="native", schema_hash="sha256:r4",
        run_id="run-r4", turn_id=record.request_id, attempt_id="attempt-a"), request_id=record.request_id,
        round_number=1, call_index=call_index, description="Inspect /private/tmp/r4-example.txt",
        grant_key="controlled_exec:read")


# LLM: Wait for the production pending reader, not a guessed sleep; cancellation and join in finally keep failing tests from leaving a live waiter.
# 函数用途: 启动原 writer 等待链并证明它仍持有当前 lease 的待决审批。
@contextmanager
def waiting_approval(gateway, *, channel="tui"):
    record = hot_request(gateway, channel=channel)
    request = approval_request(record)
    writer = BufferedChunkStreamWriter(record.chunk, interactive_approvals=True, rich_transcript=True,
        delivery_channel=channel)
    cancelled, finished = threading.Event(), threading.Event()
    result, errors = {}, []
    cancellation = SimpleNamespace(is_cancelled=cancelled.is_set)

    def wait():
        try:
            result.update(writer.request_permission(request.to_dict(), cancellation_token=cancellation))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        until = time.monotonic() + 5
        while time.monotonic() < until and not pending_tool_approvals(gateway.agent, gateway.paths, record.scope):
            assert not errors, errors
            assert not finished.is_set(), result
            finished.wait(0.01)
        pending = pending_tool_approvals(gateway.agent, gateway.paths, record.scope)
        assert len(pending) == 1
        assert pending[0].request == request
        yield SimpleNamespace(record=record, request=request, writer=writer, result=result,
            finished=finished, thread=thread, cancelled=cancelled)
    finally:
        cancelled.set()
        thread.join(5)
        writer.close()
        assert not thread.is_alive()
        assert not errors, errors


# LLM: The HTTP body contains the exact verified request/decision and binding run id; only the original waiter may consume the decision.
# 函数用途: 使用已有网络审批路由发送一份精确决定。
def approval_body(waiting, *, decision="approved"):
    return {"conversation_id": "session-a", "run_id": waiting.request.binding["run_id"],
        "request": waiting.request.to_dict(), "decision": ToolApprovalDecision(
            waiting.request.permission_id, decision, "read-only verification").to_dict()}


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize("decision", ["approved", "denied", "approved_session", "approved_owner"])
def test_real_writer_network_tui_http_decision_releases_original_waiter(gateway, transport, decision):
    with waiting_approval(gateway) as waiting:
        observer = client(gateway)
        observer._http_only = transport == "http"
        runtime = TuiRuntime("session-a")
        adapter = TuiTurnEventAdapter(runtime, "r")
        adapter.configure_gateway_permission_sink(partial(observer.permission_sink, conversation_id="session-a"))
        emitted, resolved, routes = [], [], []
        actual = observer.request_json

        def track(method, path, payload=None, **kwargs):
            routes.append((method, urlsplit(path).path, payload))
            return actual(method, path, payload, **kwargs)

        observer.request_json = track

        def consume(event):
            emitted.append(event)
            consumed = adapter.on_gateway_event(event)
            if event.get("kind") == "permission_requested":
                overlay = runtime.store.snapshot().permission
                if overlay is not None:
                    resolved.append(adapter.resolve_permission(overlay.permission_id, decision,
                        feedback="read-only verification"))
                    if resolved[-1] and waiting.finished.wait(3):
                        finish(gateway, waiting.record)
            return consumed

        result = observer(poll_request(gateway, waiting.record, consume))
        permissions = [event for event in emitted if event.get("kind") == "permission_requested"]
        assert permissions and permissions[0].get("permission") == waiting.request.to_dict()
        assert resolved == [True]
        assert waiting.finished.wait(3)
        assert waiting.result["decision"] == decision
        assert waiting.result["feedback"] == "read-only verification"
        assert result["ok"] is True
        assert runtime.store.snapshot().permission is None
        writes = [(path, payload) for method, path, payload in routes if method == "POST"]
        assert [path for path, _ in writes] == ["/client/agent-permission"]
        assert writes[0][1]["run_id"] == waiting.request.binding["run_id"]
        assert writes[0][1]["request"] == waiting.request.to_dict()


@pytest.mark.parametrize("wrong", ["owner", "channel", "session", "run", "binding", "permission_id",
    "decision_id", "description", "options", "deadline", "expired", "old_lease"])
def test_wrong_approval_identity_or_lifetime_keeps_original_waiter_pending(gateway, monkeypatch, wrong):
    with waiting_approval(gateway) as waiting:
        observer = client(gateway, user="bob" if wrong == "owner" else "alice",
            channel="gateway-cli" if wrong == "channel" else "tui")
        body = deepcopy(approval_body(waiting))
        if wrong == "session":
            body["conversation_id"] = "session-b"
        elif wrong == "run":
            body["run_id"] = "another-run"
        elif wrong == "binding":
            body["request"]["binding"]["args_hash"] = "another-arguments-hash"
        elif wrong == "permission_id":
            body["request"]["permission_id"] = "another-permission"
        elif wrong == "decision_id":
            body["decision"]["permission_id"] = "another-permission"
        elif wrong == "description":
            body["request"]["description"] = "different displayed operation"
        elif wrong == "options":
            body["request"]["options"][0]["label"] = "different label"
        elif wrong == "deadline":
            body["request"]["expires_at"] = time.time() + 3600
        elif wrong == "expired":
            from agent_py_agent.agent.gateway_parts import http_handlers
            future = time.time() + 100000
            monkeypatch.setattr(http_handlers, "time", SimpleNamespace(time=lambda: future))
        elif wrong == "old_lease":
            waiting.record.payload["lease_started_at"] = time.time() + 1
            waiting.record.path.write_text(json.dumps(waiting.record.payload), encoding="utf-8")
        status, _ = observer.request_json("POST", "/client/agent-permission", body)
        assert status in {404, 409}
        assert not gateway_permission_decision_path(waiting.record.chunk, waiting.request).exists()
        assert not waiting.finished.is_set()
        assert waiting.result == {}


def test_external_im_keeps_summary_only_even_with_all_user_view(gateway):
    with waiting_approval(gateway, channel="feishu") as waiting:
        for user in ("alice", "admin"):
            status, batch = client(gateway, user=user, channel="feishu").request_json("GET", "/progress/r")
            assert status == 200
            event = batch["events"][0]
            assert set(event) == {"kind", "tool", "summary"}
            assert event["kind"] == "permission_requested"
            assert "/private/tmp/" not in event["summary"]
        assert not waiting.finished.is_set()


def test_invalid_permission_dto_is_not_exposed_to_private_viewer(gateway):
    record = hot_request(gateway)
    write_chunk_event(record.chunk, {"kind": "permission_requested", "permission": {
        "tool_name": "controlled_exec", "description": "invalid incomplete DTO"}})
    status, batch = client(gateway).request_json("GET", "/progress/r")
    assert status == 200
    assert all("permission" not in event for event in batch["events"])


# LLM: Measure only resources owned by this listener after libc resolver initialization; failures still execute teardown and the zero-delta assertion.
# 函数用途: 对成功、断连和畸形输入用例统一核对文件描述符没有净增。
@pytest.fixture
def measured_gateway(tmp_path):
    """原认证监听器完整退出后核对描述符，包含断连与失败用例的 finally。"""
    import socket

    # macOS 的 libc 首次反查会保留进程级 mDNS 连接；先初始化，再测本用例拥有的资源。
    socket.getfqdn("127.0.0.1")
    fd_root = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    if not os.path.isdir(fd_root):
        pytest.skip("descriptor census requires procfs or devfs")
    gc.collect()
    before = len(os.listdir(fd_root))
    fixture = gateway.__wrapped__(tmp_path)
    try:
        yield next(fixture)
    finally:
        fixture.close()
        gc.collect()
        delta = len(os.listdir(fd_root)) - before
        print(f"resource_check fd_delta={delta} teardown_complete=true")
        assert delta == 0


# LLM: One production writer has at most one sequential waiter; finally cancels and joins it even on the pre-fix callback failure.
# 函数用途: 建立同一前台请求的两个审批，测试失败也不遗留等待线程。
@contextmanager
def sequential_approvals(gateway):
    """同一前台 writer 顺序等待两份规范审批，退出必取消并回收原线程。"""
    record = hot_request(gateway)
    requests = (approval_request(record), approval_request(record, call_id="call-r3-b", call_index=2))
    writer = BufferedChunkStreamWriter(record.chunk, interactive_approvals=True, rich_transcript=True,
        delivery_channel="tui")
    cancelled, finished = threading.Event(), threading.Event()
    results, errors = [], []

    def wait():
        try:
            for request in requests:
                results.append(writer.request_permission(request.to_dict(),
                    cancellation_token=SimpleNamespace(is_cancelled=cancelled.is_set)))
                if cancelled.is_set():
                    return
            finish(gateway, record)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        until = time.monotonic() + 5
        while time.monotonic() < until and not pending_tool_approvals(gateway.agent, gateway.paths, record.scope):
            assert not errors, errors
            assert not finished.is_set()
            finished.wait(0.01)
        assert [pending.request for pending in pending_tool_approvals(
            gateway.agent, gateway.paths, record.scope)] == [requests[0]]
        yield SimpleNamespace(record=record, requests=requests, writer=writer, results=results,
            finished=finished, thread=thread)
    finally:
        cancelled.set()
        thread.join(5)
        writer.close()
        assert not thread.is_alive()
        assert not errors, errors


# LLM: The peer resolves A through the actual HTTP sink; only the delivered durable resolution may let the foreground adapter open B.
# 函数用途: 在两种真实网络运输及一次重连下复现并阻止审批弹窗卡死。
@pytest.mark.parametrize(("transport", "disconnect"), [("sse", False), ("http", False), ("sse", True)])
def test_peer_resolution_preserves_next_foreground_approval(measured_gateway, monkeypatch, transport, disconnect):
    import agent_py_agent.agent.common.authenticated_http as network

    gateway = measured_gateway
    with sequential_approvals(gateway) as waiting:
        first, second = waiting.requests
        observer, peer = client(gateway), client(gateway)
        observer._http_only = transport == "http"
        runtime = TuiRuntime("session-a")
        adapter = TuiTurnEventAdapter(runtime, waiting.record.request_id)
        adapter.configure_gateway_permission_sink(partial(observer.permission_sink, conversation_id="session-a"))
        emitted, closed, actionable, approved, errors, pending_counts, streams = [], [], [], [], [], [], []
        aborted = threading.Event()
        actual_open = network.open_authenticated_request

        class DisconnectAfterResolution:
            def __init__(self, raw):
                self.raw, self.target_frame, self.close_next = raw, False, False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.raw.close()

            def readline(self, size):
                if self.close_next:
                    self.raw.close()
                    return b""
                line = self.raw.readline(size)
                if line.startswith(b"data:"):
                    event = json.loads(line[5:]).get("event", {})
                    self.target_frame = event.get("kind") == "permission_resolved" and event.get("permission_id") == first.permission_id
                if line in (b"\n", b"\r\n") and self.target_frame:
                    self.close_next = True
                return line

        def open_request(request, *, timeout):
            raw = actual_open(request, timeout=timeout)
            if urlsplit(request.full_url).path.endswith("/events"):
                streams.append(request.get_header("Last-event-id"))
                if disconnect and len(streams) == 1:
                    return DisconnectAfterResolution(raw)
            return raw

        monkeypatch.setattr(network, "open_authenticated_request", open_request)

        def consume(event):
            emitted.append(event)
            try:
                consumed = adapter.on_gateway_event(event)
                if event.get("kind") == "permission_requested":
                    overlay = runtime.store.snapshot().permission
                    pending_counts.append(len(pending_tool_approvals(gateway.agent, gateway.paths, waiting.record.scope)))
                    if event["permission"]["permission_id"] == first.permission_id:
                        assert overlay is not None and overlay.permission_id == first.permission_id
                        peer.permission_sink(first, ToolApprovalDecision(first.permission_id, "approved"),
                            conversation_id="session-a")
                    else:
                        actionable.append(overlay is not None and overlay.permission_id == second.permission_id)
                        approved.append(adapter.resolve_permission(second.permission_id, "approved"))
                elif event.get("kind") == "permission_resolved" and event.get("permission_id") == first.permission_id:
                    closed.append(runtime.store.snapshot().permission is None)
                return consumed
            except Exception as exc:
                errors.append((type(exc).__name__, str(exc), exc.__cause__))
                aborted.set()
                raise

        request = replace(poll_request(gateway, waiting.record, consume), is_wait_cancelled=aborted.is_set)
        result = observer(request)
        rows = [json.loads(line) for line in waiting.record.chunk.read_text().splitlines()]
        durable = [row for row in rows if row.get("kind") == "permission_resolved"
                   and row.get("permission_id") == first.permission_id]
        resolutions = [event for event in emitted if event.get("kind") == "permission_resolved"
                       and event.get("permission_id") == first.permission_id]
        overlay = runtime.store.snapshot().permission
        trace = {"transport": transport, "disconnect": disconnect, "durable_a": len(durable),
            "delivered_a": len(resolutions), "a_pending": overlay is not None and overlay.permission_id == first.permission_id,
            "b_actionable": actionable, "b_waiter_completed": len(waiting.results) == 2,
            "consumer_errors": [(name, text, cause is None) for name, text, cause in errors]}
        print("permission_trace " + json.dumps(trace, sort_keys=True))
        assert len(durable) == 1
        assert len(resolutions) == 1, trace
        assert closed == [True] and actionable == [True] and approved == [True], trace
        assert not errors, trace
        assert waiting.finished.wait(3) and len(waiting.results) == 2
        assert all(row["decision"] == "approved" for row in waiting.results)
        assert result["ok"] is True and runtime.store.snapshot().permission is None
        assert not pending_tool_approvals(gateway.agent, gateway.paths, waiting.record.scope)
        assert pending_counts and max(pending_counts) <= 1
        assert [(event["kind"], event.get("permission_id", event.get("permission", {}).get("permission_id")))
                for event in emitted if event["kind"] in {"permission_requested", "permission_resolved"}] == [
            ("permission_requested", first.permission_id), ("permission_resolved", first.permission_id),
            ("permission_requested", second.permission_id), ("permission_resolved", second.permission_id)]
        assert observer._http_only is (transport == "http")
        assert len(streams) == (2 if disconnect else 0 if transport == "http" else 1)
        if disconnect:
            assert streams[1] and streams[1].startswith("v1:r:attempt-a:")
        adapter.finalize(TuiTurnSummary(response_text="finished"))
        _headless_followup_after_approvals(runtime, gateway)


# LLM: Derive negative rows from a real writer resolution, preserving DTO authority and both transport boundaries rather than inventing a parallel wire contract.
# 函数用途: 验证坏决议被丢弃、额外字段不下发且后续有效行仍能到达。
@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize(("field", "value"), [
    ("permission_id", ""), ("decision", "unknown"), ("decided_at", [1]),
    ("decided_at", "invalid-time"), ("reason_code", "unknown-reason"),
    pytest.param("decided_at", 10**1000, id="timestamp-overflow")])
def test_resolution_projection_rejects_malformed_durable_dto(measured_gateway, transport, field, value):
    gateway = measured_gateway
    with waiting_approval(gateway) as waiting:
        observer = client(gateway)
        observer._http_only = transport == "http"
        observer.permission_sink(waiting.request, ToolApprovalDecision(waiting.request.permission_id, "approved"),
            conversation_id="session-a")
        assert waiting.finished.wait(3)
        rows = [json.loads(line) for line in waiting.record.chunk.read_text().splitlines()]
        canonical = next(row for row in rows if row.get("kind") == "permission_resolved")
        malformed = {**canonical, field: value}
        # 负例从真实 writer 的规范行改坏一个字段，不另造客户端/服务端 Wire 合同。
        write_chunk_event(waiting.record.chunk, malformed)
        write_chunk_event(waiting.record.chunk, {**canonical, "private_extra": "not-a-dto-field"})
        finish(gateway, waiting.record)
        runtime = TuiRuntime("session-a")
        adapter = TuiTurnEventAdapter(runtime, "r")
        emitted = []

        def consume(event):
            emitted.append(event)
            return adapter.on_gateway_event(event)

        assert observer(poll_request(gateway, waiting.record, consume))["ok"] is True
        resolutions = [event for event in emitted if event.get("kind") == "permission_resolved"]
        expected = {"kind": "permission_resolved", **ToolApprovalDecision.from_mapping(canonical).to_dict()}
        assert resolutions == [expected, expected]
        assert runtime.store.snapshot().permission is None
        assert not pending_tool_approvals(gateway.agent, gateway.paths, waiting.record.scope)
        print(f"resolution_contract_trace transport={transport} malformed_rejected=true canonical_dto_only=true")


# LLM: External viewers, including the admin identity, receive only the original request summary and never canonical resolution fields.
# 函数用途: 核对普通用户和管理员都不能在外部通道获得完整审批决议。
@pytest.mark.parametrize("transport", ["sse", "http"])
def test_external_channels_do_not_disclose_canonical_resolution(measured_gateway, transport):
    gateway = measured_gateway
    with waiting_approval(gateway, channel="feishu") as waiting:
        waiting.cancelled.set()
        assert waiting.finished.wait(3)
        rows = [json.loads(line) for line in waiting.record.chunk.read_text().splitlines()]
        assert any(row.get("kind") == "permission_resolved" for row in rows)
        finish(gateway, waiting.record)
        for user in ("alice", "admin"):
            observer = client(gateway, user=user, channel="feishu")
            observer._http_only = transport == "http"
            emitted = []
            assert observer(poll_request(gateway, waiting.record, lambda event: emitted.append(event) or True))["ok"] is True
            assert [event["kind"] for event in emitted] == ["permission_requested"]
            assert set(emitted[0]) == {"kind", "tool", "summary"}
        print(f"resolution_contract_trace transport={transport} external_resolution_hidden=true")


# LLM: The shared projector reuses the existing DTO normalization; an empty timestamp does not justify a new validation policy in this slice.
# 函数用途: 锁住原 DTO 的空值兼容行为，避免测试自造更严格的合同。
def test_resolution_projection_keeps_existing_dto_empty_timestamp_default():
    from agent_py_agent.agent.gateway_parts.http_handlers import _project_public_progress_rows

    row = {"kind": "permission_resolved", **ToolApprovalDecision("approval-empty-time", "approved").to_dict()}
    row["decided_at"] = []
    expected = ToolApprovalDecision.from_mapping(row).to_dict()
    assert expected["decided_at"] == 0.0
    assert _project_public_progress_rows((json.dumps(row),), channel="tui", next_cursor=7) == (
        [{"kind": "permission_resolved", **expected}], 7)


# LLM: Stream/steer/stop use the original headless TUI control boundary; reset requires a completed real /new receipt and a new network result, not a synthetic reset alone.
# 函数用途: 审批后连续走界面控制、真实新会话回执和下一轮，核对旧弹窗、目标及页脚状态全部释放。
def _headless_followup_after_approvals(runtime, gateway):
    """不以本地出箱证明服务端 steer/stop 或模型执行；/new 则核对真实 HTTP 回执。"""
    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_worker import _reset_worker_refs
    from agent_py_agent.tests.test_tui_control_delivery import _post_turn_params, _request_target

    steers, controls = [], []
    params = _post_turn_params(runtime, steers)
    params.control_operation_reconciler = SimpleNamespace(enqueue_background=controls.append)
    runtime.register_gateway_request("gwreq-turn-1")
    turn = runtime.begin_turn("chat-turn-1", task_progress_generation_id="gwreq-turn-1")
    turn.write_model("正在生成第一段")
    turn.write_model("继续生成下一段")
    assert runtime.store.snapshot().has_active_work
    runtime.publish_execution_target(_request_target())
    assert tui_actions._tui_submit_active_turn_input(params, "补充证据", display_text="补充证据") is True
    assert len(steers) == 1 and steers[0].expected_turn_id == "gwreq-turn-1"
    assert tui_actions._tui_submit_control_operation(params, "/stop") is True
    assert len(controls) == 1 and controls[0].expected_turn_id == "gwreq-turn-1"
    assert controls[0].command_kind == "stop" and runtime.request_interrupt()
    runtime.complete_turn("chat-turn-1", TuiTurnSummary(ok=False, interrupted=True))
    _reset_worker_refs(params)
    observer = client(gateway)
    status, receipt = observer.agent.post_gateway_json("/control", {"command": "/new",
        "conversation_id": runtime.session_id, "metadata": {"message_id": "r3-followup-new"}}, timeout=5)
    assert status == 200 and receipt["control_state"] == "completed" and receipt["ok"] is True, receipt
    assert receipt["kind"] == "new" and receipt["details"]["view_reset"] is True
    status, replay = observer.agent.get_gateway_json("/control-status/" + receipt["operation_id"], timeout=5,
        conversation_id=runtime.session_id)
    assert status == 200 and replay == receipt
    runtime.publish_conversation_reset(receipt["details"]["thread_id"])
    assert runtime.publish_execution_target(_request_target()) is False
    assert tui_actions._tui_submit_active_turn_input(params, "下一轮对话", display_text="下一轮对话") is False
    tui_actions._tui_enqueue_job(params, "下一轮对话")
    assert params.jobs.get_nowait().user == "下一轮对话"
    record = hot_request(gateway, request_id="r3-next-turn")
    finish(gateway, record)
    status, result = observer.request_json("GET", "/result/" + record.request_id)
    assert status == 200 and result["ok"] is True and result["request_id"] == record.request_id
    next_turn = runtime.begin_turn("chat-turn-2")
    next_turn.write_model("新的回答")
    runtime.complete_turn("chat-turn-2", TuiTurnSummary(response_text="下一轮完成"))
    snapshot = runtime.store.snapshot()
    assert snapshot.permission is None and snapshot.pending_steers == () and not snapshot.has_active_work
    assert runtime.execution_target() is None
    assert not runtime._turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings and not runtime._owned_gateway_requests
    print("headless_sequence stream->steer->stop=tui_control_outbox new=real_completed_http_receipt "
          "next_turn=real_http_result_and_tui clean=true")


# =============================================================================
# Source: agent_py_agent/tests/test_rework_r5_portable_runtime.py
# =============================================================================

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/private_release"


# LLM: 只创建临时合成发行副本；解释器委托真实测试Python，原生文件不执行。
# 函数用途: 为前置拒绝与真实安装器字节码启动提供同一个最小构建输入。
@pytest.fixture
def portable_build_case(tmp_path, monkeypatch):
    root, output = tmp_path / "bundle with spaces", tmp_path / "output"
    package = root / f"runtime/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/agent_py_agent"
    for path in (root / "runtime/bin", root / "native/lib", root / "native/lark_cli",
                 package / "vendor/bin", package / "vendor/bubblewrap", package / "config"):
        path.mkdir(parents=True)
    python = root / "runtime/bin/python3"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    (root / "runtime/bin/example").write_text("#!/old/build/python3\nprint('relocate')\n")
    for name in ("bwrap", "PROVENANCE.txt", "feishu-cli-host.linux-x86_64", "lib/libfixture.so", "lark_cli/release.json"):
        (root / "native" / name).write_bytes(b"synthetic native resource")
    helper = bytearray(64)
    helper[:7] = b"\x7fELF\x02\x01\x01"
    helper[18:20] = (62).to_bytes(2, "little")
    (root / "native/my-agent-sandbox-helper.linux-x86_64").write_bytes(helper)
    (package / "__init__.py").write_text("VALUE = 1\n")
    (package / "config/agent_config.yaml").write_text('workspace_root: ""\ngateway_port: 8420\n')
    argv = ["build", "--bundle", str(root), "--helpers", str(SCRIPTS), "--output", str(output),
            "--version", "test.r5", "--base-commit", "fixture", "--source-sha256", "0" * 64]
    monkeypatch.setattr(sys, "argv", argv)
    return SimpleNamespace(root=root, package=package, python=python, output=output, argv=argv)


def _file_snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode)
            for path in root.rglob("*") if path.is_file()}


def test_magic_mismatch_refuses_before_any_bundle_mutation(portable_build_case):
    case = portable_build_case
    # 版本号仍真实一致，唯独改变magic；不能只比较major/minor或目录名。
    magic = bytes([importlib.util.MAGIC_NUMBER[0] ^ 0xFF]) + importlib.util.MAGIC_NUMBER[1:]
    bootstrap = ("import importlib.util, sys; "
                 f"importlib.util.MAGIC_NUMBER = {magic!r}; exec(sys.argv[1])")
    case.python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -c {shlex.quote(bootstrap)} "$3"\n')
    before = _file_snapshot(case.root)
    with pytest.raises(ValueError, match="MAGIC_NUMBER") as error:
        build.main()
    assert shlex.join([str(case.python), *case.argv]) in str(error.value)
    assert _file_snapshot(case.root) == before
    assert not case.output.exists()
    assert (case.package / "__init__.py").is_file()
    assert not list(case.root.rglob("*.pyc"))


@pytest.mark.parametrize("magic", ["", "not-hex", "00"])
def test_invalid_magic_probe_cannot_publish(portable_build_case, magic):
    case = portable_build_case
    version = ".".join(map(str, sys.version_info[:3]))
    case.python.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(version)} {shlex.quote(magic)}\n")
    before = _file_snapshot(case.root)
    with pytest.raises(ValueError):
        build.main()
    assert _file_snapshot(case.root) == before
    assert not case.output.exists()


def test_same_interpreter_run_reaches_real_installer_help(portable_build_case, tmp_path):
    case = portable_build_case
    assert build.main() == 0
    assert not (case.package / "__init__.py").exists()
    assert (case.package / "__init__.pyc").read_bytes()[:4] == importlib.util.MAGIC_NUMBER
    assert (case.root / "installer.pyc").read_bytes()[:4] == importlib.util.MAGIC_NUMBER
    # 只替代外层架构观察；实际解包、哈希校验、解释器和原installer.pyc均真实执行。
    # 这是本机启动回归，不声称合成原生文件或Linux x86_64运行时已获验收。
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text("#!/bin/sh\nprintf 'x86_64\\n'\n")
    uname.chmod(0o755)
    result = subprocess.run(
        ["bash", str(case.output / "my-agent-test.r5-linux-x86_64.run"), "--help"],
        env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "my-agent 临时离线安装 / 更新" in result.stdout
    assert "--prefix" in result.stdout and "--verify-only" in result.stdout


# LLM: 执行未改写的prepare_runtime.sh；只替代pip和解释器环境，绝不联网安装依赖。
# 函数用途: 在同一个shell入口观察版本、GIL、精确依赖及失败传播。
@pytest.fixture
def prepare_case(tmp_path):
    root = tmp_path / "runtime build"
    for name in ("bin", "lib", "include", "share"):
        (root / name).mkdir(parents=True)
    driver = tmp_path / "interpreter.py"
    driver.write_text('''import builtins, importlib.metadata, json, os, sys, sysconfig, types
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["R5_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:2] == ["-m", "pip"]:
    binary = any(arg.startswith(("psycopg-binary==", "psycopg[binary]==")) for arg in args)
    if binary and os.environ.get("R5_PIP_FAIL"):
        raise SystemExit(17)
    raise SystemExit(0)
assert args[0] == "-c", args
sys.version_info = (*json.loads(os.environ["R5_VERSION"]), 4, "final", 0)
original_config = sysconfig.get_config_var
sysconfig.get_config_var = lambda name: int(os.environ["R5_GIL"]) if name == "Py_GIL_DISABLED" else original_config(name)
original_version = importlib.metadata.version
def version(name):
    if name != "psycopg":
        return original_version(name)
    if os.environ.get("R5_METADATA_FAIL"):
        raise importlib.metadata.PackageNotFoundError(name)
    return os.environ["R5_RESOLVED"]
importlib.metadata.version = version
for name in ("agent_py_agent", "psycopg", "psycopg_binary"):
    module = types.ModuleType(name)
    module.__version__ = os.environ["R5_RESOLVED"]
    module.pq = types.SimpleNamespace(__impl__="binary")
    sys.modules[name] = module
original_import = builtins.__import__
def imported(name, *args, **kwargs):
    if name.startswith("psycopg") and os.environ.get("R5_IMPORT_FAIL"):
        raise ImportError("synthetic missing libpq/binary")
    return original_import(name, *args, **kwargs)
builtins.__import__ = imported
exec(args[1])
''')
    python = root / "bin/python3"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(driver))} "$@"\n')
    python.chmod(0o755)
    log = tmp_path / "calls.jsonl"

    def run(version=(3, 14), gil=0, resolved="3.2.9", **flags):
        result = subprocess.run(
            ["bash", str(SCRIPTS / "prepare_runtime.sh"), str(root), str(tmp_path / "source")],
            env={**os.environ, "R5_LOG": str(log), "R5_VERSION": json.dumps(version),
                 "R5_GIL": str(gil), "R5_RESOLVED": resolved, **flags},
            capture_output=True, text=True, timeout=30,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    return run


@pytest.mark.parametrize("resolved", ["3.2.9", "3.3.1"])
def test_normal_314_installs_resolved_binary_wheel_then_imports(prepare_case, resolved):
    result, calls = prepare_case(resolved=resolved)
    assert result.returncode == 0, result.stderr
    installs = [call for call in calls if call[:3] == ["-m", "pip", "install"]]
    assert len(installs) == 2
    assert installs[1][-1] in {f"psycopg-binary=={resolved}", f"psycopg[binary]=={resolved}"}
    assert "--only-binary=:all:" in installs[1]
    imports = [call for call in calls if call[0] == "-c" and "import psycopg" in call[1]]
    assert imports
    assert calls.index(imports[0]) > calls.index(installs[1])


@pytest.mark.parametrize("version,gil", [((3, 14), 1), ((3, 11), 0), ((3, 12), 0), ((3, 15), 0)])
def test_free_threaded_and_legacy_prepare_do_not_add_cp314_binary(prepare_case, version, gil):
    result, calls = prepare_case(version=version, gil=gil)
    assert result.returncode == 0, result.stderr
    installs = [call for call in calls if call[:3] == ["-m", "pip", "install"]]
    assert len(installs) == 1
    assert installs[0][-1].endswith(".whl[scale,secrets]")
    assert all("psycopg-binary==" not in arg and "psycopg[binary]==" not in arg for call in calls for arg in call)


@pytest.mark.parametrize("flag,returncode", [("R5_PIP_FAIL", 17), ("R5_IMPORT_FAIL", 1), ("R5_METADATA_FAIL", 1)])
def test_normal_314_prepare_fails_closed_on_dependency_error(prepare_case, flag, returncode):
    result, calls = prepare_case(**{flag: "1"})
    assert result.returncode == returncode, (result.stdout, result.stderr)
    assert not any("import agent_py_agent, ssl, sqlite3" in arg for call in calls for arg in call)


# =============================================================================
# Source: agent_py_agent/tests/test_rework_r9_adapter_delivery_lifetime.py
# =============================================================================

def _record():
    return build_gateway_ingress_record(channel="feishu", user_id="ou-r9", conversation_id="oc-r9",
                                        provider_message_id="om-r9-1", content="hello", metadata={}, timestamp=1.0)


class _Callbacks:
    def __init__(self):
        self.submits = []
        self.handoffs = []

    def prepare(self, record):
        return {"kind": "ask", "prompt": record.content, "metadata": {}}

    def submit(self, record):
        self.submits.append((record.claim_epoch, time.time()))
        return {"request_id": f"req-r9-{len(self.submits)}", "status": "queued"}

    def advance(self, _record):
        return GatewayIngressAdvance("placeholder_ready", "typing")

    def handoff(self, record):
        self.handoffs.append(record.ingress_id)


def _worker(tmp_path, callbacks, *, ttl=None, poll_interval=0.02):
    reply = GatewayReplyDeliveryWorker(GatewayReplyDeliveryStore(tmp_path / "reply"),
                                       poll_response=lambda _pending: None,
                                       deliver_response=lambda _pending, _text: True)
    return GatewayAdapterDeliveryWorker(
        GatewayIngressStore(tmp_path / "delivery"), reply_worker=reply, prepare_payload=callbacks.prepare,
        submit_payload=callbacks.submit, advance_submission=callbacks.advance, handoff_reply=callbacks.handoff,
        poll_interval=poll_interval, lease=None if ttl is None else GatewayClaimLeaseConfig(ttl_seconds=ttl))


def _fail_write_once(monkeypatch, predicate):
    real, failures = ingress._write_private_json, []

    def write(path, payload):
        if not failures and predicate(payload):
            failures.append(dict(payload))
            raise OSError("simulated ingress queue write failure")
        return real(path, payload)

    monkeypatch.setattr(ingress, "_write_private_json", write)
    return failures


def _eventually(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _completed(worker):
    records = worker.store.records()
    return len(records) == 1 and records[0].state == "completed"


def test_claim_publication_failure_keeps_the_sole_poller_progressing(tmp_path, monkeypatch):
    callbacks = _Callbacks()
    worker = _worker(tmp_path, callbacks)
    failures = _fail_write_once(monkeypatch, lambda row: row.get("state") == "prepared" and row.get("claim_owner"))
    worker.enqueue(_record())
    worker.start()
    thread = worker._thread
    try:
        assert _eventually(lambda: _completed(worker)), "sole delivery thread stopped after a claim write failure"
        assert worker._thread is thread and thread.is_alive()
    finally:
        worker.stop()
    assert not thread.is_alive()
    # 失败的 claim 从未发布，外部效果只执行一次；后续由同一线程按原阶段与 epoch 推进到 completed。
    assert len(failures) == 1 and failures[0]["claim_epoch"] == 1
    assert len(callbacks.submits) == 1 and callbacks.handoffs == [_record().ingress_id]
    record, = worker.store.records()
    assert record.claim_owner == "" and record.claim_epoch > failures[0]["claim_epoch"]


def test_post_submit_publication_failure_keeps_original_lease_before_replay(tmp_path, monkeypatch):
    callbacks = _Callbacks()
    worker = _worker(tmp_path, callbacks, ttl=1.0)
    failures = _fail_write_once(monkeypatch, lambda row: row.get("state") == "submitted")
    worker.enqueue(_record())
    worker.start()
    thread = worker._thread
    try:
        assert _eventually(lambda: _completed(worker)), "sole delivery thread stopped after a commit write failure"
        assert worker._thread is thread and thread.is_alive()
    finally:
        worker.stop()
    assert len(failures) == 1
    # 提交结果未知：原租约未过期前不重投；过期后同一线程以更大 epoch 精确重放原阶段。
    (first_epoch, first_at), (replay_epoch, replay_at) = callbacks.submits
    assert replay_epoch > first_epoch and replay_at - first_at >= 0.9
    record, = worker.store.records()
    assert record.claim_owner == "" and record.submission["request_id"] == "req-r9-2"


def test_stop_interrupts_bounded_wait_and_base_exception_still_ends_loop(tmp_path, monkeypatch):
    callbacks = _Callbacks()
    worker = _worker(tmp_path, callbacks, poll_interval=30.0)
    worker.start()
    thread = worker._thread
    started = time.monotonic()
    worker.stop(timeout=5.0)
    assert not thread.is_alive() and time.monotonic() - started < 5.0

    class _Halt(BaseException):
        pass

    halted = _worker(tmp_path / "halt", callbacks)
    calls = []

    def halt():
        calls.append(threading.current_thread().name)
        raise _Halt()

    monkeypatch.setattr(halted, "run_once", halt)
    with pytest.raises(_Halt):
        halted._run()
    assert len(calls) == 1


# =============================================================================
# Source: agent_py_agent/tests/test_rework_r9_audit_redelivery_io.py
# LLM: 只用 pytest 临时 owner、真实收割管线与真实 pull/submit_verdicts；每个场景只出现一次普通模拟 I/O 错误，
#   不改游标、欠账、令牌或租约算法。
# 模块用途: 回归 P1-R9-14——保证档重投递读失败保留不确定：不按缺口签收、在途身份不动、交原公开可重试结果。
# =============================================================================

owner_home = _owner_home


# LLM: 包住原区间读者，第一次调用抛普通 EIO，之后原样委托；故障点就是重投递读取的真实入口。
# 函数用途: 让在途区间重读失败一次并返回计数。
def _fail_range_read_once(monkeypatch) -> dict[str, int]:
    original = hv._scan_spool_range
    calls = {"failed": 0}

    def flaky(state, inflight):
        if not calls["failed"]:
            calls["failed"] += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return original(state, inflight)

    monkeypatch.setattr(hv, "_scan_spool_range", flaky)
    return calls


# 函数用途: 读取盘上读游标 sidecar 原始字节，用于逐字节比较“未写账”。
def _cursor_bytes(state) -> bytes:
    return hv._read_sidecar_path(state).read_bytes()


# LLM: 一次读失败返回原公开可重试形状且游标逐字节不变；无故障重试拿到同一批同一令牌，部分签收后只重投剩余，
#   全程没有缺口计数。
# 函数用途: 验证读失败不变成空签收批，并覆盖无故障重试与部分签收。
def test_redelivery_read_error_is_retryable_and_keeps_inflight_identity(owner_home, monkeypatch):
    state = _audit_state(owner_home, _FakeSource(), 4, chunk=4)
    first, _ = hv.read_spool_records(state, max_candidates=4, consumer="judge-a")
    ids = _ack_ids(first)
    assert len(ids) == 4
    before = _cursor_bytes(state)
    calls = _fail_range_read_once(monkeypatch)

    failed, backlog = hv.read_spool_records(state, max_candidates=4, consumer="judge-b")

    assert calls["failed"] == 1 and failed == []
    assert backlog["authorization_error"]["error_code"] == "AUDIT_SPOOL_READ_UNAVAILABLE"
    assert _cursor_bytes(state) == before
    cursor = hv.read_spool_cursor(state)
    assert cursor["inflight"]["pending_acks"] == ids and "redelivery_gap_candidates" not in cursor
    assert hv.acked_candidates(cursor) == 0

    retried, backlog = hv.read_spool_records(state, max_candidates=4, consumer="judge-b")
    assert _ack_ids(retried) == ids and "authorization_error" not in backlog
    assert backlog["pending_verdicts"] == 4

    partial = hv.submit_verdicts(state, consumer="judge-b", verdicts=[_verdict(ack_id) for ack_id in ids[:2]])
    assert partial["ok"] and partial["acked_now"] == 2 and partial["pending_remaining"] == 2
    rest, backlog = hv.read_spool_records(state, max_candidates=4, consumer="judge-b")
    assert _ack_ids(rest) == ids[2:] and backlog["pending_verdicts_total"] == 2
    done = hv.submit_verdicts(state, consumer="judge-b", verdicts=[_verdict(ack_id) for ack_id in ids[2:]])
    assert done["ok"] and done["pending_remaining"] == 0
    cursor = hv.read_spool_cursor(state)
    assert hv.acked_candidates(cursor) == 4 and "redelivery_gap_candidates" not in cursor


# LLM: 部分签收后读游标 sidecar 一次读失败：不能当全新游标从头重领、清零签收计数，只交可重试结果；
#   之后无故障拉取只重投未签收的两条。
# 函数用途: 验证游标读失败保持在途欠账与签收计数。
def test_cursor_read_error_does_not_reclaim_from_start(owner_home, monkeypatch):
    state = _audit_state(owner_home, _FakeSource(), 4, chunk=4)
    first, _ = hv.read_spool_records(state, max_candidates=4, consumer="judge-a")
    ids = _ack_ids(first)
    hv.submit_verdicts(state, consumer="judge-a", verdicts=[_verdict(ack_id) for ack_id in ids[:2]])
    sidecar = hv._read_sidecar_path(state)
    before = sidecar.read_bytes()
    original = Path.read_text
    calls = {"failed": 0}

    def flaky_read_text(self, *args, **kwargs):
        if self == sidecar and not calls["failed"]:
            calls["failed"] += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    failed, backlog = hv.read_spool_records(state, max_candidates=4, consumer="judge-a")

    assert calls["failed"] == 1 and failed == []
    assert backlog["authorization_error"]["error_code"] == "AUDIT_SPOOL_CURSOR_UNAVAILABLE"
    assert sidecar.read_bytes() == before
    retried, _ = hv.read_spool_records(state, max_candidates=4, consumer="judge-a")
    assert _ack_ids(retried) == ids[2:]
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 2


# LLM: 读失败期间与之后，过期来源尝试既不能领取也不能签收；在途令牌和欠账只属于当前租约，当前租约重试拿同一批。
# 函数用途: 验证过期租约拒绝与读失败后的身份保持。
def test_stale_lease_refused_while_current_lease_retries_same_ids(owner_home, monkeypatch):
    state = _audit_state(owner_home, _FakeSource(), 2, chunk=2)
    state.audit_root_task_id = "audit-r9-io"
    state.source_id = "source-r9-io"
    persist_state(state)
    lease = {
        "schema_version": "audit-source-worker-lease.v1",
        "audit_id": state.audit_root_task_id,
        "source_id": state.source_id,
        "watch_id": state.watch_id,
        "worker_key": audit_source_worker_key(state.audit_root_task_id, state.watch_id),
        "run_id": "run-current",
        "attempt_id": "attempt-current",
        "epoch": 2,
        "expires_at": 9_999_999_999.0,
    }
    lease_path = ws.state_dir(owner_home) / f"{state.watch_id}.worker-lease.json"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    current = {**lease, "lease_epoch": 2}
    stale = {**current, "run_id": "run-stale", "attempt_id": "attempt-stale", "lease_epoch": 1}
    first, _ = hv.read_spool_records(state, max_candidates=2, consumer="run-current", source_authority=current)
    ids = _ack_ids(first)
    before = _cursor_bytes(state)
    _fail_range_read_once(monkeypatch)

    failed, backlog = hv.read_spool_records(state, max_candidates=2, consumer="run-current",
                                            source_authority=current)
    assert failed == [] and backlog["authorization_error"]["error_code"] == "AUDIT_SPOOL_READ_UNAVAILABLE"
    refused, backlog = hv.read_spool_records(state, max_candidates=2, consumer="run-stale", source_authority=stale)
    assert refused == [] and backlog["authorization_error"]["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"
    assert _cursor_bytes(state) == before

    retried, backlog = hv.read_spool_records(state, max_candidates=2, consumer="run-current",
                                             source_authority=current)
    assert _ack_ids(retried) == ids and "authorization_error" not in backlog
    stale_settle = hv.submit_verdicts(state, consumer="run-stale", verdicts=[_verdict(ack_id) for ack_id in ids],
                                      source_authority=stale)
    assert stale_settle["ok"] is False and stale_settle["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"
    settled = hv.submit_verdicts(state, consumer="run-current", verdicts=[_verdict(ack_id) for ack_id in ids],
                                 source_authority=current)
    assert settled["ok"] and settled["acked_now"] == 2
    assert "redelivery_gap_candidates" not in hv.read_spool_cursor(state)
