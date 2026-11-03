"""真实本地模型执行、物理 owner 资源来源与原生 HTTP 用量读口的组合回归。"""
from __future__ import annotations

import gc
import inspect
import json
import os
import subprocess
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_v13_w0_harness import v13_call


class _UsageBackend(EchoBackend):
    """仅替代离线供应商输出；执行、调用账和用量保存均使用生产入口。"""

    # LLM: 后端只在隔离测试构造，不创建执行账或用量事件。
    # 函数用途: 记录实际收到的生成次数。
    def __init__(self):
        self.calls = 0

    # LLM: 仅替代供应商响应；原SimpleAgent负责调用、用量和持久化。
    # 函数用途: 返回带明确token数的离线模型结果。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
                 on_thinking_delta=None, request_options=None):
        self.calls += 1
        return ModelResponse(text='本地模型已完成。', backend=self.name,
            usage={'input_tokens': 37, 'output_tokens': 5})


# LLM: 身份与HTTP夹具的feishu通道一致，所有目录显式落在测试根。
# 函数用途: 创建真实而非手工补标签的owner执行Agent。
def _agent(root, *, owner='business-alice'):
    """显式指定合成 owner，不读取或重绑实际宿主目录。"""
    return SimpleAgent(AgentConfig(model_backend='echo', prompt_files=[], enable_self_learning=False,
        my_agent_home=str(root), my_agent_owner_provider='feishu',
        my_agent_owner_kind='user', my_agent_owner_id=owner,
        business_spaces_enabled=True, memory_path='memory.jsonl'), root / 'workspace')


# LLM: 先核对HTTP与执行者同owner/home/store，再验原生usage事件和两个真实读口。
# 函数用途: 复现并回归P1-05，不注入资源provider或手工写用量。
def test_real_run_usage_event_reaches_default_http_usage_and_costs(tmp_path, monkeypatch):
    """先产出原生调用与 canonical 事件，再经真实 HTTP 验证默认装配。"""
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = _agent(tmp_path / 'home')
    backend = _UsageBackend()
    agent.backend = backend
    # 成本目录沿原background actor门读取；先用真实Store建立本次HTTP的通道会话，不伪造上下文。
    agent.conversation_store.threads.get_or_create({'channel': 'feishu', 'channel_conversation_id': 'default',
        'channel_user_id': 'business-alice', 'canonical_user_id': 'business-alice',
        'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir)})
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        status, before = v13_call(server, 'system.capabilities', {}, user='business-alice')
        assert status == 200 and before['error'] is None, before
        [services] = server._business_services.values()
        assert services.owner_id == agent.home_paths.owner_id
        assert services.owner_home == agent.home_paths.owner_home_dir.resolve()
        assert services.projection.store.storage.root == agent.conversation_store.storage.root
        agent.run('只输出本地完成说明，不调用工具。', source='cli_run', save=False,
            request_id='resource-request', run_id='resource-run', task_id='resource-task')
        assert backend.calls == 1
        threads, errors = agent.conversation_store.threads.list_report(limit=0)
        assert not errors
        events = []
        for thread in threads:
            rows, issues = agent.conversation_store.model_usage.events_report(thread.thread_id)
            assert not issues
            events.extend(rows)
        assert len(events) == 1
        assert events[0].request_id == 'resource-request'
        assert events[0].model_calls['physical_model_attempt_count'] == 1
        assert events[0].model_calls['usage_breakdown']['provider']['input_tokens'] == 37
        status, usage = v13_call(server, 'usage.get', {}, user='business-alice')
        assert status == 200, usage
        assert usage['state'] == 'completed' and usage['error'] is None
        assert usage['data']['resource_usage']['active_model_calls'] == 0
        assert 'decision_usage_only' in usage['data']['unknown']
        status, costs = v13_call(server, 'costs.dashboard', {}, user='business-alice')
        assert status == 200, json.dumps(costs, ensure_ascii=False)
        assert costs['state'] == 'completed' and costs['error'] is None
        assert costs['data']['totals']['actual'] == usage['data']['actual']
        assert costs['data']['totals']['actual']['cost'] is None
    finally:
        server.stop()


# LLM: 环境只在每个用例内替换，子进程继承同一隔离边界。
# 函数用途: 避免测试读取真实用户资料。
@pytest.fixture(autouse=True)
def _isolated_owner_home(tmp_path, monkeypatch):
    """所有真实构造与子进程均只能看见当前用例的隔离目录。"""
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))


# LLM: 只通过公开工厂按物理owner取源，不用私有gauge构造成功数据。
# 函数用途: 为实际Agent取得可调用的只读来源。
def _provider(agent):
    from agent_py_agent.agent.observability import concurrency_metrics as counters

    factory = getattr(counters, 'owner_model_activity_provider', None)
    assert callable(factory), '需要公开的物理 owner 活动来源，而不是私有全局 gauge'
    provider = factory(agent.home_paths.owner_home_dir, agent.home_paths.owner_id)
    assert callable(provider), '实际 Agent 初始化后应有本地执行来源'
    return provider


# LLM: 不发模型请求也须有可信idle0，别名归一且调用方不能篡改快照。
# 函数用途: 验证初始化、物理路径和不可变合同。
def test_initialized_source_is_idle_immutable_and_canonical(tmp_path):
    agent = _agent(tmp_path / 'home')
    provider = _provider(agent)
    snapshot = provider()
    assert snapshot['active_model_calls'] == 0
    assert snapshot['owner_id'] == agent.home_paths.owner_id
    assert snapshot['owner_home'] == str(agent.home_paths.owner_home_dir.resolve())
    assert snapshot['process_id'] == os.getpid()
    with pytest.raises(TypeError):
        snapshot['active_model_calls'] = 4
    alias = tmp_path / 'owner-alias'
    alias.symlink_to(agent.home_paths.owner_home_dir, target_is_directory=True)
    from agent_py_agent.agent.observability.concurrency_metrics import owner_model_activity_provider
    assert owner_model_activity_provider(alias, agent.home_paths.owner_id)() == snapshot


# LLM: 使用四个真实构造覆盖同根同owner聚合、不同owner及相同ID不同根隔离。
# 函数用途: 验证重入、异常退出和准确聚合边界。
def test_multiple_real_agents_share_only_the_same_physical_owner(tmp_path):
    from agent_py_agent.agent.observability import concurrency_metrics as counters

    first = _agent(tmp_path / 'home')
    second = _agent(tmp_path / 'home')
    other = _agent(tmp_path / 'home', owner='business-bob')
    elsewhere = _agent(tmp_path / 'other-home')
    a, b, foreign, remote = map(_provider, (first, second, other, elsewhere))
    scope = getattr(counters, 'owner_model_activity_scope', None)
    assert callable(scope)
    with scope(first):
        assert a()['active_model_calls'] == b()['active_model_calls'] == 1
        assert foreign()['active_model_calls'] == remote()['active_model_calls'] == 0
        with scope(second), scope(first), scope(elsewhere):
            assert a()['active_model_calls'] == b()['active_model_calls'] == 3
            assert remote()['active_model_calls'] == 1
        assert a()['active_model_calls'] == 1
    assert a()['active_model_calls'] == b()['active_model_calls'] == 0
    with pytest.raises(RuntimeError, match='synthetic failure'), scope(first):
        assert a()['active_model_calls'] == 1
        raise RuntimeError('synthetic failure')
    assert a()['active_model_calls'] == 0


# LLM: 失效绑定/PID错配必须未知；新解释器无父进程执行来源，不能借用idle0。
# 函数用途: 验证缺源、改绑和跨进程的失败关闭。
def test_missing_stale_and_different_process_sources_stay_unavailable(tmp_path, monkeypatch):
    from agent_py_agent.agent.observability import concurrency_metrics as counters

    agent = _agent(tmp_path / 'home')
    provider = _provider(agent)
    factory = counters.owner_model_activity_provider
    assert factory(tmp_path / 'missing', agent.home_paths.owner_id) is None
    assert factory(agent.home_paths.owner_home_dir, 'local/user/unrelated') is None
    original_paths = agent.home_paths
    agent.home_paths = replace(original_paths, owner_id='local/user/changed')
    assert provider() is None
    agent.home_paths = original_paths
    assert provider()['active_model_calls'] == 0
    with monkeypatch.context() as patch:
        original_pid = os.getpid()
        patch.setattr(counters.os, 'getpid', lambda: original_pid + 1)
        assert provider() is None
        assert factory(original_paths.owner_home_dir, original_paths.owner_id) is None
    # 新进程没有该 owner 的执行对象；不能把父进程的 idle 值冒充子进程事实。
    code = ('import json,sys; from agent_py_agent.agent.observability.concurrency_metrics '
            'import owner_model_activity_provider; '
            'print(json.dumps(owner_model_activity_provider(sys.argv[1],sys.argv[2]) is None))')
    child = subprocess.run([sys.executable, '-c', code, str(original_paths.owner_home_dir),
        original_paths.owner_id], cwd=Path(__file__).resolve().parents[2],
        text=True, capture_output=True, timeout=15, check=True)
    assert json.loads(child.stdout) is True


# LLM: 观察provider可长寿，但不能强引用执行者或让已回收来源保持就绪。
# 函数用途: 验证真实Agent回收后的旧provider失效。
def test_provider_does_not_keep_an_execution_agent_alive(tmp_path):
    agent = _agent(tmp_path / 'home')
    provider = _provider(agent)
    reference = weakref.ref(agent)
    del agent
    gc.collect()
    assert reference() is None
    assert provider() is None


# LLM: 所有旧成员已改绑时，新的实际执行者必须创建新来源；缓存的旧provider不能借它复活。
# 函数用途: 验证同一物理namespace重新初始化后的失效代次隔离。
def test_new_execution_source_does_not_revive_a_stale_provider(tmp_path):
    stale = _agent(tmp_path / 'home')
    old_provider = _provider(stale)
    old_paths = stale.home_paths
    stale.home_paths = replace(old_paths, owner_id='local/user/changed')
    assert old_provider() is None
    current = _agent(tmp_path / 'home')
    assert _provider(current)()['active_model_calls'] == 0
    assert old_provider() is None


# LLM: Store与只读RuntimeRepository均指向同一实际owner，不手工造资源行。
# 函数用途: 按公共签名装配需要验证的默认reader。
def _readers(agent, *, resource_provider=None):
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import (
        default_observation_readers,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path

    assert 'resource_provider' in inspect.signature(default_observation_readers).parameters
    runtime = RuntimeRepository(runtime_db_path(agent.home_paths.owner_home_dir), read_only=True)
    return default_observation_readers(agent.home_paths.owner_home_dir, agent.home_paths.owner_id,
        agent.conversation_store, runtime, resource_provider=resource_provider)


# LLM: 默认发现与显式注入都必须独立于私有gauge，并跟随实际scope变化。
# 函数用途: 验证新来源可直接供原reader消费。
def test_default_reader_accepts_matching_public_source_without_private_gauge(tmp_path, monkeypatch):
    from agent_py_agent.agent.observability import concurrency_metrics as counters

    agent = _agent(tmp_path / 'home')
    provider = _provider(agent)
    monkeypatch.setattr(counters, '_METRICS', None)
    for readers in (_readers(agent), _readers(agent, resource_provider=provider)):
        assert 'resource_usage' in readers
        row = readers['resource_usage']({}).records[0]
        assert row.owner_id == agent.home_paths.owner_id
        assert row.value['active_model_calls'] == 0
        with counters.owner_model_activity_scope(agent):
            assert readers['resource_usage']({}).records[0].value['active_model_calls'] == 1


# LLM: 显式无效来源不得借已有owner标签成功；bool、小数和无穷不是合法物理计数。
# 函数用途: 验证provider身份、进程、路径和整数白名单。
@pytest.mark.parametrize('change', [
    {'owner_id': 'local/user/another'}, {'owner_home': '/unrelated/synthetic-root'},
    {'process_id': -1}, {'active_model_calls': -1}, {'active_model_calls': True},
    {'active_model_calls': 0.5}, {'active_model_calls': float('inf')},
])
def test_injected_source_is_validated_and_never_falls_back_to_a_global_meter(tmp_path, change):
    from agent_py_agent.agent.observability import concurrency_metrics as counters

    agent = _agent(tmp_path / 'home')
    snapshot = dict(_provider(agent)())
    snapshot.update(change)
    meter = counters._metrics().llm_inflight
    labels = {'owner_id': agent.home_paths.owner_id}
    meter.set(19, labels=labels)
    try:
        readers = _readers(agent, resource_provider=lambda: MappingProxyType(snapshot))
        assert 'resource_usage' not in readers and 'usage' not in readers
    finally:
        with meter._lock:
            meter._vals.pop(tuple(sorted(labels.items())), None)


# LLM: 真实外部owner、无结果、异常和不可调用对象都保持不可用。
# 函数用途: 防止来源失败被改写为空闲0。
def test_foreign_provider_and_source_failure_do_not_become_idle_zero(tmp_path):
    alice = _agent(tmp_path / 'home')
    bob = _agent(tmp_path / 'home', owner='business-bob')
    assert 'resource_usage' not in _readers(alice, resource_provider=_provider(bob))

    # LLM: 明确返回未知，不提供任何资源事实。
    # 函数用途: 模拟已存在但失效的provider。
    def unavailable():
        return None

    # LLM: 读源故障不能触发其它owner或旧全局标签兜底。
    # 函数用途: 模拟provider读取失败。
    def failed():
        raise OSError('synthetic source failure')

    for provider in (unavailable, failed, SimpleNamespace(active_model_calls=0)):
        assert 'resource_usage' not in _readers(alice, resource_provider=provider)


class _BlockingUsageBackend(_UsageBackend):
    # LLM: 事件仅控制离线供应商阻塞，不修改模型账或占用计数。
    # 函数用途: 准备可释放的真实生成调用。
    def __init__(self, *, failure=False):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()
        self.failure = failure

    # LLM: 进入后由测试查询实际owner来源，退出仍交生产finally处理。
    # 函数用途: 保持一次物理生成在途，并可用明确异常结束。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
                 on_thinking_delta=None, request_options=None):
        self.entered.set()
        assert self.release.wait(10), '测试未能释放离线 worker'
        if self.failure:
            raise RuntimeError('synthetic provider failure')
        return super().generate(prompt, on_chunk, tools, tool_choice, messages,
            on_thinking_delta, request_options)


# LLM: 捕获工作线程的原异常供主测试断言，不把线程结束当成成功。
# 函数用途: 在finally释放测试线程后核对完整结果。
def _capture(target, errors):
    try:
        target()
    except BaseException as error:
        errors.append(error)


# LLM: 用原生HTTP证明在途1，冷owner未知；另一实际Agent就绪后只刷新同一缓存投影。
# 函数用途: 验证默认装配的实时owner隔离和缓存身份保持。
def test_live_http_owner_activity_and_cached_projection_never_borrow_base_owner(tmp_path):
    alice = _agent(tmp_path / 'home')
    provider = _provider(alice)
    backend = _BlockingUsageBackend()
    alice.backend = backend
    errors = []
    worker = threading.Thread(target=_capture, args=(lambda: alice.run('本地输出。',
        source='cli_run', save=False, request_id='live-request', run_id='live-run', task_id='live-task'), errors))
    server = GatewayHTTPServer(0, gateway_paths_from_root(alice.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=alice))
    server.start()
    worker.start()
    try:
        assert backend.entered.wait(5)
        assert provider()['active_model_calls'] == 1
        status, current = v13_call(server, 'usage.get', {}, user='business-alice')
        assert status == 200 and current['data']['resource_usage']['active_model_calls'] == 1, current
        status, missing = v13_call(server, 'usage.get', {}, user='business-bob')
        assert status == 503 and missing['error']['code'] == 'HOST_NOT_READY', missing
        bob_services = next(service for service in server._business_services.values()
            if service.owner_id.endswith('/business-bob'))
        projection = bob_services.projection
        bob = _agent(tmp_path / 'home', owner='business-bob')
        assert _provider(bob)()['active_model_calls'] == 0
        status, idle = v13_call(server, 'usage.get', {}, user='business-bob')
        assert status == 200 and idle['data']['resource_usage']['active_model_calls'] == 0, idle
        assert bob_services.projection is projection
        assert provider()['active_model_calls'] == 1
    finally:
        backend.release.set()
        worker.join(10)
        server.stop()
    assert not worker.is_alive() and not errors
    assert provider()['active_model_calls'] == 0


# LLM: 辅助入口实际阻塞生成，成功和异常都由同一物理栈平衡。
# 函数用途: 验证辅助生成的owner活动接线。
@pytest.mark.parametrize('failure', [False, True])
def test_auxiliary_scope_tracks_only_the_actual_generate_worker(tmp_path, failure):
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        AuxiliaryModelCallRequest,
        generate_auxiliary_model_response,
    )
    agent = _agent(tmp_path / 'home')
    provider = _provider(agent)
    backend = _BlockingUsageBackend(failure=failure)
    agent.backend = backend
    request = AuxiliaryModelCallRequest(agent, '本地辅助请求', request_id='aux-request', run_id='aux-run')
    errors = []
    worker = threading.Thread(target=_capture, args=(lambda: generate_auxiliary_model_response(request), errors))
    worker.start()
    try:
        assert backend.entered.wait(5)
        assert provider()['active_model_calls'] == 1
    finally:
        backend.release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert provider()['active_model_calls'] == 0
    if failure:
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    else:
        assert not errors


# LLM: 原有界调用使caller先终态，真实worker仍阻塞；必须保持1直到后者退出。
# 函数用途: 验证取消与超时不提前释放物理占用。
@pytest.mark.parametrize('terminal', ['cancelled', 'timed_out'])
def test_decision_terminal_caller_keeps_still_running_physical_worker_counted(tmp_path, terminal):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallStillRunningError
    from agent_py_agent.agent.concurrency.interrupt import InterruptHandle
    from agent_py_agent.agent.conversation import decision_model_call as decision
    from agent_py_agent.tests.test_decision_model_call import _Backend, _params, _request, _result

    agent = _agent(tmp_path / 'home')
    provider = _provider(agent)
    entered, release = threading.Event(), threading.Event()
    handle, errors = InterruptHandle(), []

    # LLM: 仅替代离线决策供应商，所有准入、有界worker和ledger沿原生产入口。
    # 函数用途: 让caller终态与worker实际退出可独立观察。
    def operation(request, deadline):
        entered.set()
        assert release.wait(10), '测试未能释放决策 worker'
        return _result(request)

    backend = _Backend(operation)
    deadline = time.monotonic() + (1 if terminal == 'timed_out' else 5)
    caller = threading.Thread(target=_capture, args=(lambda: decision.invoke_decision_model_call(
        agent, _params(), _request(), backend, deadline=deadline,
        resource_key=('owner-resource-test', str(tmp_path)), interrupt_handle=handle), errors))
    caller.start()
    try:
        assert entered.wait(5)
        assert provider()['active_model_calls'] == 1
        if terminal == 'cancelled':
            handle.cancel()
        caller.join(3)
        assert not caller.is_alive() and len(errors) == 1
        expected = InterruptedError if terminal == 'cancelled' else BoundedCallStillRunningError
        assert isinstance(errors[0], expected)
        assert backend.threads[0].is_alive()
        record = agent._model_call_ledger.records()[0]
        assert record.status == ('failed' if terminal == 'cancelled' else 'timed_out')
        assert provider()['active_model_calls'] == 1
    finally:
        release.set()
        caller.join(5)
        for worker in backend.threads:
            worker.join(5)
    assert not caller.is_alive() and all(not worker.is_alive() for worker in backend.threads)
    assert provider()['active_model_calls'] == 0


# LLM: 观察actor按原合同每次新签发；刷新不得替换显式服务或令此前actor失权。
# 函数用途: 验证显式投影和已签发actor绑定保持。
def test_composition_preserves_explicit_projection_and_existing_actor(tmp_path):
    from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        bind_business_context,
    )

    agent = _agent(tmp_path / 'home')
    explicit = BusinessObservationService(agent.home_paths.owner_home_dir,
        owner_id=agent.home_paths.owner_id, readers={})
    services = BusinessServices(agent, projection=explicit)
    context = bind_business_context(agent, authorize=lambda action, facts: True)
    actor = services.observation_actor(context)
    services.refresh_observation_readers()
    assert services.projection is explicit and explicit.readers == {}
    assert actor.service is explicit
    explicit.authorize_call('usage.get', {}, actor=actor)
    assert services.observation_actor(context).service is explicit


# LLM: 用可控的原准入门检查三条实际入口，等待准入时不得计作物理在途。
# 函数用途: 防止owner scope被移到准入前或caller包装层。
@pytest.mark.parametrize('path', ['main', 'auxiliary', 'decision'])
def test_waiting_for_admission_is_not_counted_as_provider_activity(tmp_path, monkeypatch, path):
    from agent_py_agent.agent.conversation import decision_model_call as decision
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        AuxiliaryModelCallRequest,
        generate_auxiliary_model_response,
    )
    from agent_py_agent.agent.llm_scale import hot_path
    from agent_py_agent.tests.test_decision_model_call import _Backend, _params, _request, _result

    agent = _agent(tmp_path / 'home')
    provider = _provider(agent)
    admission_entered, allow_admission = threading.Event(), threading.Event()
    backend, errors = _BlockingUsageBackend(), []
    agent.backend = backend

    # LLM: 只替代准入等待，不直接操作owner计数，scope仍由生产worker进入。
    # 函数用途: 把准入前和供应商执行中的观察点拆开。
    @contextmanager
    def admission(*, optional=False):
        admission_entered.set()
        assert allow_admission.wait(10), '测试未能释放准入等待'
        yield

    monkeypatch.setattr(hot_path, 'global_llm_admission_slot', admission)
    monkeypatch.setattr(decision, 'global_llm_admission_slot', admission)
    if path == 'main':
        target = partial(agent.run, '本地准入检查。', source='cli_run', save=False,
            request_id='admission-request', run_id='admission-run', task_id='admission-task')
    elif path == 'auxiliary':
        request = AuxiliaryModelCallRequest(agent, '本地准入检查', request_id='admission-request', run_id='admission-run')
        target = partial(generate_auxiliary_model_response, request)
    else:
        # LLM: 决策的等待发生在真实有界worker内，生成结果由原ledger收口。
        # 函数用途: 提供可观察进入和退出的离线决策调用。
        def operation(request, deadline):
            backend.entered.set()
            assert backend.release.wait(10), '测试未能释放决策生成'
            return _result(request)

        decision_backend = _Backend(operation)
        target = partial(decision.invoke_decision_model_call, agent, _params(), _request(), decision_backend,
            deadline=time.monotonic() + 5, resource_key=('admission-owner', str(tmp_path)))
    worker = threading.Thread(target=_capture, args=(target, errors))
    worker.start()
    try:
        assert admission_entered.wait(5)
        assert not backend.entered.is_set()
        assert provider()['active_model_calls'] == 0
        allow_admission.set()
        assert backend.entered.wait(5)
        assert provider()['active_model_calls'] == 1
    finally:
        allow_admission.set()
        backend.release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    assert provider()['active_model_calls'] == 0
