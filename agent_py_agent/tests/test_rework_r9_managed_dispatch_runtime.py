# LLM: 真实SimpleAgent、会话库、后台调度器与W冻结启动；只把供应商后端换成离线记录器，记录实际模型档案与凭据。
# 模块用途: 验证受管派单在Count0/CountN的唯一规范执行、受管池容量与失败、同owner模型选择和拒绝边界。
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import time
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from agent_py_agent.agent.common.managed_owner_generation import (
    current_managed_owner_generation,
    require_agent_managed_owner_generation,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import managed_owner_runtime
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.managed_owner_runtime import (
    ManagedOwnerPool,
    managed_owner_pool_for_agent,
    prepare_managed_owner_runtime,
)
from agent_py_agent.agent.owner_scoped_pool import (
    OwnerScopedAgentPool,
    PoolCapacityExceededError,
    _pool_total_live_locked,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_dispatch_recovery import (
    canonical_conversation_root,
    open_legacy_conversation_store,
)
from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
from agent_py_agent.tests.test_rework_r9_managed_dispatch_recovery import _legacy_dispatch

ALICE = OwnerIdentity.local_user("alice")


class _RecordingBackend(BaseBackend):
    name = "recording"

    def __init__(self, calls, config=None):
        self.calls, self.config = calls, config

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(provider="recording", endpoint="local://recording", model="", stream=False,
                                      native_supported=True, evidence="test_backend_declares_native_tools",
                                      observed_at=_utc_now_iso())

    # LLM: 原生工具协议把会话消息放在结构化messages里；只记录实际模型档案、凭据与消息，不发网络。
    # 函数用途: 记录一次模型调用并返回固定完成文本。
    def generate(self, prompt, on_chunk=None, **kwargs):
        self.calls.append({"model": getattr(self.config, "model_name", ""), "api_key": getattr(self.config, "api_key", ""),
                           "messages": repr(kwargs.get("messages"))})
        return ModelResponse(text="managed work finished", backend=self.name)


@pytest.fixture
def calls(monkeypatch):
    from agent_py_agent.agent.settings import model_scope

    recorded = []
    monkeypatch.setattr(model_scope, "_profile_backend", lambda agent, config: _RecordingBackend(recorded, config))
    return recorded


@pytest.fixture
def host(tmp_path):
    root = (tmp_path / "host").resolve()
    lifecycle = LocalUserLifecycle(LocalUserStore(root))
    lifecycle.create("alice", idempotency_key="managed_alice_0001")
    lifecycle.create("bob", idempotency_key="managed_bob_00001")
    config = AgentConfig(enable_tools=False, memory_path="memory.jsonl", my_agent_home=str(root), model_backend="echo",
                         api_key="deployment-secret", access_mode="full-access", path_access_mode="full",
                         additional_write_roots=[str(tmp_path)], gateway_per_user_owner_scoping=True,
                         owner_agent_idle_seconds=3600)
    base = SimpleAgent(config, tmp_path / "workspace", defer_mcp_connect=True)
    return SimpleNamespace(root=root, base=base, lifecycle=lifecycle, tmp=tmp_path)


# LLM: 档案由受管owner自己的模型目录保存；凭据为测试假值，不发网络。
# 函数用途: 为受管Agent新增一个私有模型档案并返回其编号。
def _add_profile(agent, name):
    profile_id = str(uuid4())
    execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "model_backend": "anthropic_compatible", "model_name": name, "api_base": "https://example.test/anthropic",
        "api_key": name + "-secret", "model_context_window_tokens": 96000}})
    return profile_id


def _supervisor(host):
    from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor
    from agent_py_agent.cli.models import GatewayRunContext

    return _BackgroundMainSupervisor(GatewayRunContext(agent=host.base, paths=SimpleNamespace(),
                                                       config_path=host.tmp / "config.yaml"))


def _drive(supervisor, rounds=3):
    for _ in range(rounds):
        supervisor.tick()
        cf.wait(list(supervisor._inflight.values()), timeout=30)
    supervisor.tick()


# LLM: 原调度器对空闲owner有封顶指数退避（真实时钟）；只按原节拍继续推进，不改调度器内部状态。
# 函数用途: 推进后台节拍直至条件成立或超时。
def _drive_until(supervisor, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        _drive(supervisor, rounds=1)
        time.sleep(0.1)
    return predicate()


def _assistant_texts(store, thread_id):
    return [message.content for message in store.messages.recent(thread_id) if message.role == "assistant" and message.content]


def test_count0_cold_and_warm_dispatch_execute_once_on_canonical_store(host, calls):
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool

    managed = managed_owner_pool_for_agent(host.base)
    with managed.acquire_pinned(ALICE) as agent:
        execute_model_profile_operation(agent, "set_default", {"profile_id": _add_profile(agent, "owner-model")})
    assert managed.release_idle(0) and managed.gauges()["cached"] == 0
    service = ManagedUserGovernanceService(host.root)
    first = service.dispatch_task("alice", "Summarize the first notes", title="First")
    assert first["status"] == "queued"
    supervisor = _supervisor(host)
    _drive(supervisor)
    home = LocalUserStore(host.root).owner_home("alice")
    [agent] = managed.hard_agents()
    store = agent.conversation_store
    assert store.storage.root == canonical_conversation_root(home) and id(agent) in supervisor._owner_schedulers
    assert [(call["model"], call["api_key"]) for call in calls] == [("owner-model", "owner-model-secret")]
    assert "Summarize the first notes" in calls[0]["messages"]
    assert store.tasks.load(first["task_id"]).status == "completed" and store.wakes.pending() == []
    assert _assistant_texts(store, first["thread_id"]) == ["managed work finished"]
    legacy = open_legacy_conversation_store(home)
    assert legacy is None or legacy.threads.load(first["thread_id"]) is None

    second = service.dispatch_task("alice", "Summarize the second notes", title="Second")
    assert _drive_until(supervisor, lambda: len(calls) >= 2 and store.tasks.load(second["task_id"]).status == "completed")
    assert managed.hard_agents() == [agent] and managed.gauges()["cached"] == 1
    assert len(calls) == 2 and "Summarize the second notes" in calls[1]["messages"]
    assert store.tasks.load(second["task_id"]).status == "completed"
    _drive(supervisor)
    assert len(calls) == 2
    with pytest.raises(HostAPIError) as caught:
        _owner_pool(host.base).get(ALICE)
    assert caught.value.code == "HOST_NOT_READY"


def test_managed_runtime_is_restricted_and_keeps_same_owner_model_selection(host, calls):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    home = LocalUserStore(host.root).owner_home("alice")
    agent = prepare_managed_owner_runtime(host.root, ALICE, host_options=host.base.config)
    config = agent.config
    assert (config.api_key, config.access_mode, config.path_access_mode) == ("", "workspace-write", "normal")
    assert config.model_backend != "echo" and config.mcp_servers == {} and config.additional_write_roots == []
    assert config.owner_agent_idle_seconds == 3600 and config.my_agent_owner_id == "alice"
    assert agent.home_paths.owner_home_dir == home.home_dir and agent.root == home.home_dir
    assert agent.conversation_store.storage.root == canonical_conversation_root(home)
    generation = agent._managed_owner_generation
    execute_model_profile_operation(agent, "set_default", {"profile_id": _add_profile(agent, "owner-default")})
    chosen = _add_profile(agent, "owner-thread")
    service = ManagedUserGovernanceService(host.root)
    selected = service.dispatch_task("alice", "Use the thread model", title="Selected")
    plain = service.dispatch_task("alice", "Use the default model", title="Plain")
    execute_model_profile_operation(agent, "select", {"profile_id": chosen}, thread_id=selected["thread_id"])
    assert require_agent_managed_owner_generation(agent, "after_selection") is generation
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store, channels=agent.delivery_service)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": agent.conversation_store})
    scheduler.tick_thread(selected["thread_id"])
    scheduler.tick_thread(plain["thread_id"])
    assert [(call["model"], call["api_key"]) for call in calls] == [
        ("owner-thread", "owner-thread-secret"), ("owner-default", "owner-default-secret")]
    assert all("deployment-secret" not in call["messages"] for call in calls)
    assert agent.conversation_store.threads.require(selected["thread_id"]).model_profile_id == chosen


@pytest.fixture
def countn(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders

    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    home = (tmp_path / "home").resolve()
    repo = (tmp_path / "repo").resolve()
    config = AgentConfig(my_agent_home=str(home), model_backend="echo", api_key="deployment-secret", prompt_files=[])
    SimpleAgent(replace(config), repo)
    names = [f"user{index:02d}" for index in range(40)]
    good = next(name for name in names if owner_worker(OwnerIdentity.local_user(name), 2) == 0)
    wrong = next(name for name in names if owner_worker(OwnerIdentity.local_user(name), 2) == 1)
    lifecycle = LocalUserLifecycle(LocalUserStore(home))
    for name in (good, wrong):
        lifecycle.create(name, idempotency_key=f"managed_{name}_0001")
    # 组件夹具显式准备C物化输出（真实CountN由C在认领前物化）；W本身不补建owner目录。
    ensure_owner_home(home, OwnerIdentity.local_user(good))
    managed_home = LocalUserStore(home).owner_home(good)
    dispatch = _legacy_dispatch(managed_home, "Frozen legacy work")
    paths = gateway_paths_from_root(home / "gateway")
    ensure_gateway_folders(paths)
    launch = mode.WorkerLaunch(WorkerPartition(0, 2, "r9-managed"), home / "gateway", home, os.getppid(),
                               workspace_roots=(repo,))
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"])
    assert mode.establish_worker_mode(launch) == {"ok": True}
    return SimpleNamespace(frozen=freeze_worker_bootstrap(launch), config=config, repo=repo, home=home, paths=paths,
                           good=OwnerIdentity.local_user(good), wrong=OwnerIdentity.local_user(wrong),
                           managed_home=managed_home, dispatch=dispatch)


def test_countn_worker_pool_hosts_managed_owner_under_frozen_bootstrap(countn):
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool

    pool = create_worker_pool(countn.frozen, countn.config, countn.repo)
    managed = pool.managed_owner_pool
    assert isinstance(managed, ManagedOwnerPool)
    with pytest.raises(HostAPIError) as caught:
        pool.get(countn.good)
    assert caught.value.code == "HOST_NOT_READY"
    with managed.acquire_pinned(countn.good) as agent:
        assert agent._gateway_worker_partition is countn.frozen.launch.partition
        assert agent.home_paths.owner_home_dir == countn.managed_home.home_dir
        assert agent.conversation_store.storage.root == canonical_conversation_root(countn.managed_home)
        assert agent.root == countn.repo and list(agent.workspace_roots) == [countn.repo]
        assert agent.config.api_key == "" and agent.config.model_backend != "echo"
        with pool._lock:
            assert _pool_total_live_locked(pool) == 1
        assert managed.gauges() == {"cached": 1, "building": 0, "pinned": 1}
    with pytest.raises(WorkerBootstrapError):
        managed.get(countn.wrong)
    assert managed.gauges() == {"cached": 1, "building": 0, "pinned": 0}


def test_countn_claims_run_on_managed_pool_without_retargeting_frozen_legacy_wake(countn, monkeypatch, calls):
    from agent_py_agent.agent.gateway_parts import multiprocess_dispatch_binding as binding_module
    from agent_py_agent.agent.gateway_parts import request_worker
    from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
        execute_background_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import freeze_source
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import ExecutionBinding
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import execute_worker_claim
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
    from agent_py_agent.agent.gateway_parts.queue_service import GatewayClaim

    pool = create_worker_pool(countn.frozen, countn.config, countn.repo)
    managed = pool.managed_owner_pool
    checked, processed = [], []
    monkeypatch.setattr(binding_module, "bind_claimed_request", lambda snapshot, claim, payload: ExecutionBinding(snapshot, claim))
    monkeypatch.setattr(binding_module, "require_execution_binding", lambda binding, payload: countn.good)
    monkeypatch.setattr(binding_module, "require_execution_agent",
                        lambda binding, payload, agent: checked.append(agent) or countn.good)
    monkeypatch.setattr(pool, "get", lambda *_args, **_kwargs: pytest.fail("ordinary pool must not host a managed owner"))

    # LLM: 替身只替代外层回合收口；后台来源仍由真实原消费者在原T门内读取与处理。
    # 函数用途: 在受管租用与代次绑定内执行原后台请求消费者。
    def process(agent, paths, path, worker_id, *, claim):
        assert current_managed_owner_generation() is agent._managed_owner_generation
        assert managed.gauges()["pinned"] == 1
        processed.append(execute_background_request({"agent": agent, "request": json.loads(path.read_text()),
                                                     "request_path": path, "request_id": claim.request_id}))
        if processed[-1]['outcome'] == 'pending':
            from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
                requeue_pending_background_request,
            )

            assert requeue_pending_background_request({'background_result': processed[-1]}, agent)
            pending = countn.paths.inbox / path.name
            assert json.loads(pending.read_text())['metadata']['background_source'] == frozen_ref
        return True

    monkeypatch.setattr(request_worker, "_process_claimed_gateway_request_path", process)

    # LLM: 请求文件是已认领processing回合的原字段子集；attempt与id即T门复核的执行身份。
    # 函数用途: 以一个冻结来源执行一次已认领W请求。
    def run_claim(request_id, ref):
        path = countn.paths.processing / f"{request_id}.json"
        path.write_text(json.dumps({"id": request_id, "kind": "background", "status": "processing", "turn_phase": "open",
                                    "execution_attempt_id": "attempt-1", "lease_epoch": 1, "lease_owner": "worker-0",
                                    "metadata": {"background_source": ref}}))
        claim = GatewayClaim(path, request_id, "attempt-1", 1, "worker-0")
        return execute_worker_claim(SimpleNamespace(bootstrap=countn.frozen), claim, pool)

    legacy = open_legacy_conversation_store(countn.managed_home)
    wake_id = countn.dispatch.legacy_wake.wake_signal_id
    # 超过线程窗口，证明迁移完整分页身份，不能只拷task_ids使旧历史丢失或incomplete。
    for number in range(129):
        legacy.tasks.bind({"thread_id": countn.dispatch.legacy_wake.thread_id,
                           "task_id": f"old-history-{number}", "goal": "已完成历史", "status": "completed"})
    history = tuple(legacy.tasks.iter_task_ids(countn.dispatch.legacy_wake.thread_id))
    assert len(history) == 130
    frozen_ref = freeze_source(countn.home, countn.good, legacy.storage.wake_signal_path(countn.dispatch.legacy_wake), "wake")
    assert run_claim("r9-frozen-legacy", frozen_ref) is True
    assert processed == [{"source_kind": "wake", "source_id": wake_id, "outcome": "pending",
                          "report_count": 0}]
    [agent] = managed.hard_agents()
    assert checked and all(item is agent for item in checked)
    assert managed.gauges()["pinned"] == 0 and current_managed_owner_generation() is None
    canonical = agent.conversation_store
    receipt = agent.home_paths.owner_data_dir / 'managed_dispatch_handoffs' / (countn.dispatch.task_id + '.json')
    original_receipt = receipt.read_bytes()
    for field, invalid in (('phase', 'unknown.v99'), ('owner_id', 'local/other'), ('legacy_store', 'wrong-store'), ('history_copy', {})):
        row = json.loads(original_receipt)
        receipt.write_text(json.dumps({**row, field: invalid}))
        with pytest.raises(ValueError, match='background_source_store_mismatch'):
            run_claim('r9-invalid-' + field, frozen_ref)
        assert calls == [] and canonical.threads.load(countn.dispatch.legacy_wake.thread_id) is None
    receipt.write_bytes(original_receipt)
    for step in range(32):
        if canonical.wakes.pending():
            break
        assert calls == [] and canonical.threads.load(countn.dispatch.legacy_wake.thread_id) is None
        assert run_claim(f"r9-history-{step}", frozen_ref) is True
        assert processed[-1]["outcome"] in {"pending", "source_changed"}
    assert [wake.wake_signal_id for wake in canonical.wakes.pending()] == [wake_id]
    assert legacy.wakes.pending() == [] and calls == []
    assert tuple(canonical.tasks.iter_task_ids(countn.dispatch.legacy_wake.thread_id)) == history

    canonical_ref = freeze_source(countn.home, countn.good, canonical.storage.wake_signal_path(canonical.wakes.pending()[0]), "wake")
    assert canonical_ref["source_id"] == wake_id and canonical_ref["store_ref"] != frozen_ref["store_ref"]
    assert run_claim("r9-canonical", canonical_ref) is True
    assert processed[-1]["outcome"] == "handled" and len(calls) == 1
    assert "Frozen legacy work" in calls[0]["messages"]
    assert canonical.tasks.load(countn.dispatch.task_id).status == "completed" and canonical.wakes.pending() == []
    assert managed.hard_agents() == [agent] and managed.gauges()["pinned"] == 0


@pytest.mark.parametrize("failure", ["constructor", "ownership", "shared_capacity"])
def test_managed_pool_build_failures_leave_no_entry_or_slot(host, monkeypatch, failure):
    import agent_py_agent.agent.core as core

    ordinary = OwnerScopedAgentPool(host.base.config, host.tmp / "workspace", max_agents=1)
    managed = ManagedOwnerPool(host.root, host_options=host.base.config, shared_pool=ordinary, max_agents=2)
    closed = []
    with ExitStack() as holder:
        if failure == "constructor":
            def refuse_constructor(*_args, **_kwargs):
                raise RuntimeError("simulated constructor failure")

            monkeypatch.setattr(core, "SimpleAgent", refuse_constructor)
        elif failure == "ownership":
            def drifted(*_args):
                raise HostAPIError("HOST_NOT_READY", 503)

            monkeypatch.setattr(managed_owner_runtime, "_require_managed_agent", drifted)
            monkeypatch.setattr(managed_owner_runtime, "_close_agent_mcp_clients", closed.append)
        else:
            holder.enter_context(ordinary.pin(object()))
        with pytest.raises(HostAPIError) as caught:
            with managed.acquire_pinned(ALICE):
                pytest.fail("a failed build must not yield an agent")
        assert (caught.value.code, caught.value.outcome) == ("HOST_NOT_READY", "not_started")
        assert managed.gauges() == {"cached": 0, "building": 0, "pinned": 0}
        with ordinary._lock:
            assert _pool_total_live_locked(ordinary) == (1 if failure == "shared_capacity" else 0)
        assert len(closed) == (1 if failure == "ownership" else 0)
        monkeypatch.undo()
    with managed.acquire_pinned(ALICE) as agent:
        assert agent._managed_owner_generation is not None
        with ordinary._lock:
            assert _pool_total_live_locked(ordinary) == 1


def test_managed_and_ordinary_instances_share_one_capacity(host):
    ordinary = OwnerScopedAgentPool(host.base.config, host.tmp / "workspace", max_agents=2)
    managed = ManagedOwnerPool(host.root, host_options=host.base.config, shared_pool=ordinary, max_agents=4)
    bob = OwnerIdentity.local_user("bob")
    with ExitStack() as stack:
        stack.enter_context(ordinary.pin(object()))
        managed.get(ALICE)
        with ordinary._lock:
            assert _pool_total_live_locked(ordinary) == 2
        with managed.acquire_pinned(bob) as bob_agent:
            assert managed.hard_agents() == [bob_agent]
            with ordinary._lock:
                assert _pool_total_live_locked(ordinary) == 2
            with pytest.raises(HostAPIError) as caught:
                managed.get(ALICE)
            assert caught.value.code == "HOST_NOT_READY"
            with pytest.raises(PoolCapacityExceededError):
                stack.enter_context(ordinary.pin(object()))
            assert managed.gauges() == {"cached": 1, "building": 0, "pinned": 1}
    assert managed.gauges()["pinned"] == 0


def test_wrong_owner_and_stale_references_are_denied(host):
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool

    managed = ManagedOwnerPool(host.root, host_options=host.base.config, max_agents=4)
    for owner in (OwnerIdentity.local_main(), OwnerIdentity.local_user("nobody"), OwnerIdentity.provider_user("feishu", "alice")):
        with pytest.raises(HostAPIError) as caught:
            managed.get(owner)
        assert caught.value.code == "TARGET_DENIED"
        with pytest.raises(HostAPIError):
            prepare_managed_owner_runtime(host.root, owner, host_options={})
    assert managed.gauges()["cached"] == 0
    stale = managed.get(ALICE)
    with pytest.raises(HostAPIError):
        with managed.pin(host.base):
            pytest.fail("a foreign agent must not borrow a managed lease")
    host.lifecycle.disable("alice", idempotency_key="disable_alice_0001")
    with pytest.raises(HostAPIError) as caught:
        managed.get(ALICE)
    assert caught.value.code == "USER_DISABLED" and managed.gauges()["cached"] == 0
    host.lifecycle.enable("alice", idempotency_key="enable_alice_00001")
    fresh = managed.get(ALICE)
    assert fresh is not stale and managed.hard_agents() == [fresh]
    with pytest.raises(HostAPIError) as caught:
        with managed.pin(stale):
            pytest.fail("a retired generation must not be pinned again")
    assert caught.value.code == "HOST_NOT_READY"
    with pytest.raises(HostAPIError) as caught:
        require_agent_managed_owner_generation(stale, "stale_reference")
    assert caught.value.code == "USER_REVISION_CHANGED"
    assert require_agent_managed_owner_generation(fresh, "fresh_reference") is fresh._managed_owner_generation
    with pytest.raises(HostAPIError) as caught:
        _owner_pool(host.base).get(ALICE)
    assert caught.value.code == "HOST_NOT_READY"


def test_business_control_issuer_pins_managed_owner_for_one_call(host):
    from agent_py_agent.agent.gateway_parts.business_api import owner_complete_host

    server = SimpleNamespace(agent=host.base)
    managed = managed_owner_pool_for_agent(host.base)
    with ExitStack() as stack:
        agent = owner_complete_host(server, ALICE, stack)
        assert agent._managed_owner_generation is not None and managed.gauges()["pinned"] == 1
        assert agent.home_paths.owner_home_dir == LocalUserStore(host.root).owner_home("alice").home_dir
    assert managed.gauges()["pinned"] == 0
    assert owner_complete_host(server, OwnerIdentity.local_main(), ExitStack()) is host.base
    host.lifecycle.disable("alice", idempotency_key="disable_alice_0002")
    with ExitStack() as stack, pytest.raises(HostAPIError) as caught:
        owner_complete_host(server, ALICE, stack)
    assert (caught.value.code, caught.value.outcome) == ("USER_DISABLED", "not_started")
    assert managed.gauges() == {"cached": 0, "building": 0, "pinned": 0}
