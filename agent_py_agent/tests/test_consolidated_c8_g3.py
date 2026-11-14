"""Consolidated tests for shard c8 (test_consolidated_c8_g3.py).

Merged sources: test_rework_r9_managed_dispatch_runtime.py, test_rework_r9_plugin_cold_history.py, test_rework_r9_runner_result_precommit_recovery.py, test_rework_r9_sandbox_tmp_publication.py, test_rework_r9_task_binding_cancel.py, test_rework_r9_terminal_observation_retirement.py, test_rework_r9_web_artifact_owner_root.py.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import random
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
    supervise_stalled_orphans,
)
from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    _publish_sandbox_tmp_outputs,
    attach_run_task_workspace_context,
    finish_run_task_workspace_if_needed,
)
from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from agent_py_agent.agent.common.managed_owner_generation import (
    current_managed_owner_generation,
    require_agent_managed_owner_generation,
)
from agent_py_agent.agent.conversation import (
    compact_checkpoint as checkpoints,
)
from agent_py_agent.agent.conversation import (
    session_tasks as session_tasks_module,
)
from agent_py_agent.agent.conversation.models import (
    THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES,
    ConversationCompactCommit,
)
from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentScheduler
from agent_py_agent.agent.conversation.session_tasks import (
    SessionTaskDraft,
    SessionTaskStore,
    SessionTaskUpdate,
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
from agent_py_agent.agent.plugin_host import history
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import thaw
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.runner_result_commit import (
    prepare_runner_result_commit,
)
from agent_py_agent.agent.subagents.services.runtime_closeout import (
    CLOSEOUT_UNPERSISTED_EVENT,
    pending_closeout,
)
from agent_py_agent.agent.tooling.web import WebFetchTool
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_dispatch_recovery import (
    canonical_conversation_root,
    open_legacy_conversation_store,
)
from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    resolve_owner_home,
)
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_background_main_agent_runtime import (
    _child_settlement_fixture,
    _NaturalCompletionBackend,
)
from agent_py_agent.tests.test_mega_background_lifecycle_controls import (
    eventually_retired,
    ready_index,
)
from agent_py_agent.tests.test_plugin_host_v2_conversations import case as _case
from agent_py_agent.tests.test_plugin_host_v2_requests import (
    _public_call,
    _public_request,
)
from agent_py_agent.tests.test_plugin_host_v2_requests import (
    public_case as _public_case,
)
from agent_py_agent.tests.test_rework_r9_managed_dispatch_recovery import _legacy_dispatch

# ============================================================================
# Source: agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py
# ============================================================================

# LLM: 真实SimpleAgent、会话库、后台调度器与W冻结启动；只把供应商后端换成离线记录器，记录实际模型档案与凭据。
# 模块用途: 验证受管派单在Count0/CountN的唯一规范执行、受管池容量与失败、同owner模型选择和拒绝边界。

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
    for step in range(len(history) + 16):
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

# ============================================================================
# Source: agent_py_agent/tests/test_rework_r9_plugin_cold_history.py
# ============================================================================

# LLM: 只用 pytest 临时 owner、真实 ConversationStore 冷转（产品原 128 行阈值）与真实插件领域入口；
#   不改游标、授权或分页算法，冷段损坏只在临时目录模拟一次。
# 模块用途: 回归 P1-R9-07——插件历史与请求回复读取 canonical 冷段+热尾，冻结快照，坏冷段不当作空成功。

case = _case
public_case = _public_case
# 超过产品原 128 行冷转阈值；每行很短，按行数触发而不是缩小阈值。
ROWS = 140
POLICY = RuntimeCompactPolicy(
    context_window_tokens=1000, trigger_percent=90, trigger_tokens=900,
    recovery_target_percent=60, recovery_target_tokens=600,
    allow_persistent_apply=True, recent_tail_max_turns=4, recent_tail_tokens=100,
    failure_threshold=3, failure_cooldown_seconds=300.0,
)

# LLM: 走真实 Compact 检查点与线程 CAS，冷转由原 writer 在消息锁内完成；不替换阈值或覆盖算法。
# 函数用途: 在已有消息末尾补足行数后提交一次压缩，使旧前缀进入冷段，返回热文件路径与原始完整字节。
def _rotate(store, tid, *, turn_id="turn-one"):
    rows = [store.messages.append({
        "thread_id": tid, "role": "user" if number % 2 == 0 else "assistant",
        "content": f"历史第{number}行", "metadata": {"turn_id": turn_id},
    }) for number in range(ROWS)]
    path = store.storage.message_path(tid)
    original = path.read_bytes()
    every, errors = store.messages.recent_report(tid, limit=0)
    assert not errors
    boundary = store.messages.byte_offset_after(tid, rows[-2].message_id)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=store.storage.resolve_compact_root()),
                            backend=SimpleNamespace(name="test", model_name="test-model"))
    thread = store.threads.require(tid)
    checkpoint_id = checkpoints.write_compact_checkpoint(agent, checkpoints.CompactCheckpointRequest(
        thread=thread, summary="已覆盖旧行", operation_evidence={}, compact_rows=tuple(every[:-1]),
        retained_tail=(every[-1],), source_end_byte_offset=boundary,
        projected_tokens_before=900, projected_tokens_after=300, policy=POLICY, forced=False,
    ))
    store.threads.update_compact_state(tid, commit=ConversationCompactCommit(
        summary="已覆盖旧行", operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=rows[-2].message_id, compacted_through_byte_offset=boundary,
        source_messages=len(every) - 1, source_tool_pairs=0,
    ), expected_generation=thread.compact_generation)
    manifest = json.loads(path.with_suffix(".manifest.json").read_bytes())
    assert manifest["segment_count"] >= 1 and len(path.read_bytes()) < len(original)
    return path, original

# LLM: 只经公开 messages.list 与真实游标续页收集，不读取内部存储。
# 函数用途: 按给定页大小读完整段可见历史，返回全部公开行与页数。
def _all_pages(case, tid, limit):
    items, pages, cursor = [], [], None
    while True:
        payload = {"thread_id": tid, "limit": limit, **({"cursor": cursor} if cursor else {})}
        page = thaw(case.call("messages.list", payload).data)
        pages.append(page)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items, pages

# LLM: 冷转后历史必须从会话开头完整、按序、按可见序号返回；跨页游标冻结同一快照，后来追加不混入旧快照。
# 函数用途: 验证插件历史覆盖冷段+热尾而不是只读物理热文件。
def test_plugin_history_reads_cold_prefix_and_freezes_snapshot(case):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    _rotate(store, tid)
    first = thaw(case.call("messages.list", {"thread_id": tid, "limit": 50}).data)
    store.messages.append({"thread_id": tid, "role": "user", "content": "快照之后的新消息",
                           "metadata": {"turn_id": "turn-two"}})
    items, pages = [*first["items"]], [first]
    cursor = first["next_cursor"]
    while cursor:
        page = thaw(case.call("messages.list", {"thread_id": tid, "limit": 50, "cursor": cursor}).data)
        assert page["snapshot_id"] == first["snapshot_id"]
        items.extend(page["items"])
        pages.append(page)
        cursor = page["next_cursor"]
    assert [row["content"] for row in items] == [f"历史第{number}行" for number in range(ROWS)]
    assert [row["seq"] for row in items] == list(range(1, ROWS + 1))
    assert len(pages) == 3 and pages[-1]["complete"]
    fresh, _ = _all_pages(case, tid, 100)
    assert [row["content"] for row in fresh][-1] == "快照之后的新消息" and len(fresh) == ROWS + 1

# LLM: 缺失冷段与改写冷段都无法证明完整历史：首读 500、续页 409，绝不返回只含热尾的“成功”页。
# 函数用途: 验证损坏冷成员使快照失效而不是变成空或截断成功。
@pytest.mark.parametrize("damage", ["missing", "rewritten"])
def test_damaged_cold_segment_is_not_partial_success(case, damage):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    path, _original = _rotate(store, tid)
    first = thaw(case.call("messages.list", {"thread_id": tid, "limit": 10}).data)
    segment = sorted((path.parent / "archive" / tid).glob("seg-*.jsonl"))[0]
    if damage == "missing":
        segment.unlink()
    else:
        raw = segment.read_bytes()
        segment.write_bytes(raw.replace("历史第1行".encode(), "历史第9行".encode(), 1))
    with pytest.raises(HostAPIError) as fresh:
        case.call("messages.list", {"thread_id": tid, "limit": 10})
    assert (fresh.value.code, fresh.value.status) == ("CORRUPT_HISTORY", 500)
    with pytest.raises(HostAPIError) as resumed:
        case.call("messages.list", {"thread_id": tid, "limit": 10, "cursor": first["next_cursor"]})
    assert (resumed.value.code, resumed.value.status) == ("CURSOR_EXPIRED", 409)

# LLM: 热文件被删而分卷清单仍在属于损坏；只有从未写过消息的线程才是合法空历史。
# 函数用途: 验证缺失热文件不被当成空成功。
def test_missing_hot_file_with_manifest_is_corrupt(case):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    path, _original = _rotate(store, tid)
    path.unlink()
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid})
    assert (error.value.code, error.value.status) == ("CORRUPT_HISTORY", 500)
    empty = case.create("空线程", key="create_empty_0001")["thread_id"]
    page = thaw(case.call("messages.list", {"thread_id": empty}).data)
    assert page["items"] == [] and page["complete"]

# LLM: 冷目录、索引都必须从受信存储根逐段 no-follow 读取；链接回原内容也拒绝，不能借链接把读口带出规范存储。
# 函数用途: 验证冷归档目录被换成符号链接时历史读取失败关闭。
def test_cold_archive_symlink_is_refused(case, tmp_path):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    path, _original = _rotate(store, tid)
    archive = path.parent / "archive" / tid
    moved = tmp_path / "moved-archive"
    archive.rename(moved)
    archive.symlink_to(moved, target_is_directory=True)
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid})
    assert (error.value.code, error.value.status) == ("CORRUPT_HISTORY", 500)

# LLM: 物理 _open 仍是 Bot 调用方依赖的可 fileno/fstat/seek 句柄，只读当前热文件。
# 函数用途: 锁定 Bot 兼容口不被换成虚拟快照。
def test_physical_open_stays_for_bot_callers(case):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    path, _original = _rotate(store, tid)
    with history._open(store, tid) as handle:
        info = os.fstat(handle.fileno())
        assert (info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino)
        handle.seek(0)
        assert handle.read() == path.read_bytes()

# LLM: 请求最终回复位于冷段时仍按 8 KiB UTF-8 片段经真实公开 poll 与回复游标完整读回，不跨请求拼接。
# 函数用途: 验证 request_replies 读取 canonical 冷段+热尾。
def test_request_reply_in_cold_prefix_is_streamed_completely(public_case):
    request_id, tid = _public_request(public_case)
    store = public_case[2]
    text = "回复正文" * 3000
    store.messages.append({"thread_id": tid, "role": "assistant", "content": text,
                           "metadata": {"turn_id": request_id, "phase": "final"}})
    _rotate(store, tid)
    with history._open(store, tid) as handle:
        assert text.encode("utf-8") not in handle.read()
    result = thaw(_public_call(public_case, "requests.poll", {"request_id": request_id}).data)
    pieces = list(result["replies"])
    while result["has_more_replies"]:
        result = thaw(_public_call(public_case, "requests.poll", {
            "request_id": request_id, "reply_cursor": result["reply_cursor"]}).data)
        pieces.extend(result["replies"])
    assert "".join(row["content"] for row in pieces) == text
    assert all(len(row["content"].encode("utf-8")) <= 8192 for row in pieces)
    assert all(row["request_id"] == request_id for row in pieces) and pieces[-1]["complete"]

# LLM: 回复游标冻结的旧快照遇到冷段损坏必须过期，不能返回空回复冒充“暂无更多”。
# 函数用途: 验证 request_replies 的冷成员损坏走失效而非空成功。
def test_request_reply_cursor_expires_on_damaged_cold_segment(public_case):
    request_id, tid = _public_request(public_case)
    store = public_case[2]
    store.messages.append({"thread_id": tid, "role": "assistant", "content": "长回复" * 4000,
                           "metadata": {"turn_id": request_id, "phase": "final"}})
    path, _original = _rotate(store, tid)
    first = thaw(_public_call(public_case, "requests.poll", {"request_id": request_id}).data)
    assert first["replies"] and first["has_more_replies"]
    for segment in (Path(path.parent) / "archive" / tid).glob("seg-*.jsonl"):
        segment.unlink()
    with pytest.raises(HostAPIError) as error:
        _public_call(public_case, "requests.poll", {"request_id": request_id, "reply_cursor": first["reply_cursor"]})
    assert (error.value.code, error.value.status) == ("CURSOR_EXPIRED", 409)
    with pytest.raises(HostAPIError) as fresh:
        _public_call(public_case, "requests.poll", {"request_id": request_id})
    assert (fresh.value.code, fresh.value.status) == ("STORE_UNAVAILABLE", 503)

# ============================================================================
# Source: agent_py_agent/tests/test_rework_r9_runner_result_precommit_recovery.py
# R9-25：子代理终态 canonical 首存与原 exact 收口事实同次落盘；之后的附带副作用失败时，重启后原监督恢复链补结算与父级唤醒。
# ============================================================================

def _agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path / "workspace")

# LLM: 孩子沿原 create_run 建立，并按 orchestration.lifecycle 的原写法把孩子 run 关联到父会话；attempt 由原 lifecycle 准备。
# 函数用途: 建一个直属根会话任务的孩子及其当前 exact attempt。
def _child_case(tmp_path):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": agent.home_paths.owner_id,
        "channel_conversation_id": "session-main", "channel_user_id": "local-agent",
    })
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-root", "goal": "整理资料", "status": "active"})
    child = agent.subagents.create_run(goal="整理资料", thought="", plan=["执行"], parent_id="task-root", root_id="task-root")
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": child.id, "goal": child.goal,
                      "task_path": str(child.task_dir or ""), "status": "active"})
    attempt = agent.subagents.lifecycle.prepare_runner_attempt(child.id).runner_active_attempt_id
    assert attempt
    return agent, thread, child, attempt

def _parent_wakes(agent, thread_id, child_id):
    return [signal for signal in agent.conversation_store.wakes.pending(limit=0)
            if signal.thread_id == thread_id and signal.source_agent_id == child_id]

def _fail_first_runner_work_log(monkeypatch, manager):
    original = manager.actions._append_task_work_log
    failures = []

    def work_log(task, message, *args, **kwargs):
        if str(message).startswith("subagent_runner:") and not failures:
            failures.append(task.id)
            raise OSError("simulated I/O error")
        return original(task, message, *args, **kwargs)

    monkeypatch.setattr(manager.actions, "_append_task_work_log", work_log)
    return failures

def test_work_log_error_leaves_exact_root_handoff_recoverable_after_restart(tmp_path, monkeypatch):
    agent, thread, child, attempt = _child_case(tmp_path)
    failures = _fail_first_runner_work_log(monkeypatch, agent.subagents)
    params = RecordRunnerResultParams(run_id=child.id, dry_run=False, ok=True, message="资料已整理",
                                      attempt_id=attempt, status="DONE")
    with pytest.raises(OSError, match="simulated I/O error"):
        agent.subagents.runner_result.record_runner_result(params)
    assert failures == [child.id]
    # 首个终态保存已同时带上原 exact 收口事实；权威 run 尚未结算、父级尚未唤醒。
    saved = agent.subagents.load(child.id)
    assert saved.status == "DONE" and not saved.runner_active_attempt_id
    fact = pending_closeout(saved)
    assert fact is not None and (fact["attempt_id"], fact["delivery"]) == (attempt, "pending")
    assert fact["target_status"] == "DONE"
    repo = agent.subagents.runtime_db
    agent_run_id = repo.agent_run_for_run_id(child.id)["agent_run_id"]
    assert repo.agent_run_for_run_id(child.id)["status"] != "done"
    assert _parent_wakes(agent, thread.thread_id, child.id) == []

    # 重启：新的宿主实例只经原监督 sweep 推进；不得重跑业务或调模型。
    monkeypatch.setattr(SimpleAgent, "run_subagent",
                        lambda *_a, **_k: pytest.fail("closeout recovery must not rerun child business"))
    monkeypatch.setattr(SimpleAgent, "run", lambda *_a, **_k: pytest.fail("closeout recovery must not call the model"))
    restarted = _agent(tmp_path)
    summary = supervise_stalled_orphans(restarted)
    assert summary["runtime_closeouts_recovered"] == 1, summary
    (wake,) = _parent_wakes(restarted, thread.thread_id, child.id)
    assert wake.reason == "subagent_runner_finished" and wake.root_task_id == "task-root"
    assert wake.dedupe_key == f"subagent-finished:{child.id}:DONE:{attempt}"
    recovered = restarted.subagents.load(child.id)
    assert recovered.status == "DONE" and pending_closeout(recovered) is None
    repo = restarted.subagents.runtime_db
    assert repo.agent_run_for_run_id(child.id)["status"] == "done"
    assert [row["attempt_id"] for row in repo.attempts_for_run(agent_run_id)] == [attempt]

    # 重复恢复幂等：不再唤醒、不再结算、不新建 attempt。
    for _ in range(2):
        again = supervise_stalled_orphans(restarted)
        assert again["runtime_closeouts_recovered"] == 0 and again["runtime_closeouts_pending"] == 0, again
        assert [item.wake_signal_id for item in _parent_wakes(restarted, thread.thread_id, child.id)] == [wake.wake_signal_id]
    assert [row["attempt_id"] for row in repo.attempts_for_run(agent_run_id)] == [attempt]

def test_unpersisted_first_save_stops_after_original_fallback_event(tmp_path, monkeypatch):
    agent, thread, child, attempt = _child_case(tmp_path)
    manager = agent.subagents
    original_save = manager.save
    blocked = []

    def save(task):
        if task.id == child.id and pending_closeout(task) is not None and not blocked:
            raise OSError("simulated I/O error")
        return original_save(task)

    monkeypatch.setattr(manager, "save", save)
    logged = []
    monkeypatch.setattr(manager.actions, "_append_task_work_log", lambda task, message, *a, **k: logged.append(message))
    result = manager.runner_result.record_runner_result(RecordRunnerResultParams(
        run_id=child.id, dry_run=False, ok=True, message="资料已整理", attempt_id=attempt, status="DONE"))
    assert result.status == "DONE"
    # 两次保存都失败：只留原兜底事件，不跑附带副作用、不结算、不通知。
    assert logged == []
    assert _parent_wakes(agent, thread.thread_id, child.id) == []
    repo = manager.runtime_db
    agent_run_id = repo.agent_run_for_run_id(child.id)["agent_run_id"]
    events = [row["event_type"] for row in repo.events_for_attempt(attempt, limit=100)]
    assert events.count(CLOSEOUT_UNPERSISTED_EVENT) == 1, events
    assert repo.agent_run_for_run_id(child.id)["status"] != "done"
    # 存储恢复后原恢复链先还原收口事实，再按原顺序结算并唤醒一次父级。
    blocked.append(True)
    restarted = _agent(tmp_path)
    for _ in range(3):
        supervise_stalled_orphans(restarted)
        if _parent_wakes(restarted, thread.thread_id, child.id):
            break
    (wake,) = _parent_wakes(restarted, thread.thread_id, child.id)
    assert wake.dedupe_key == f"subagent-finished:{child.id}:DONE:{attempt}"
    assert restarted.subagents.runtime_db.agent_run_for_run_id(child.id)["status"] == "done"
    assert [row["attempt_id"] for row in restarted.subagents.runtime_db.attempts_for_run(agent_run_id)] == [attempt]

def test_not_required_resumable_result_keeps_canonical_save_without_closeout(tmp_path):
    agent, _thread, child, attempt = _child_case(tmp_path)
    manager = agent.subagents
    result = manager.runner_result.record_runner_result(RecordRunnerResultParams(
        run_id=child.id, dry_run=False, ok=False, message="等待授权后继续", attempt_id=attempt, status="BLOCKED",
        turn_end_reason="blocked", failure_type="capability_request"))
    assert result.status == "BLOCKED"
    saved = manager.load(child.id)
    assert saved.status == "BLOCKED"
    assert pending_closeout(saved) is None
    assert manager.runtime_db.agent_run_for_run_id(child.id)["status"] not in {"done", "failed"}

@pytest.mark.parametrize("case", ["terminal", "resumable", "unpersisted"])
def test_prepare_runner_result_commit_states(case):
    status = "PENDING" if case == "resumable" else "DONE"
    task = SubAgentTask(id="child", goal="g", thought="t", plan=[], status=status)
    params = RecordRunnerResultParams(run_id=task.id, dry_run=False, ok=True, message="", attempt_id="attempt-1",
                                      status=status)
    saved, events = [], []

    def save_task(current):
        if case == "unpersisted":
            raise OSError("simulated I/O error")
        saved.append((current.status, dict(pending_closeout(current) or {})))

    repo = SimpleNamespace(agent_run_for_run_id=lambda _run_id: {"agent_run_id": "agent-1"},
                           append_event=lambda **kwargs: events.append(kwargs["event_type"]))
    state = prepare_runner_result_commit(repo, task, params, SimpleNamespace(status=status, message=""),
                                         save_task=save_task)
    if case == "terminal":
        assert state == "persisted"
        assert len(saved) == 1 and saved[0][0] == "DONE"
        assert (saved[0][1]["attempt_id"], saved[0][1]["delivery"]) == ("attempt-1", "pending")
        assert events == []
    elif case == "resumable":
        assert state == "not_required" and saved == [] and events == []
    else:
        assert state == "unpersisted" and saved == []
        assert events == [CLOSEOUT_UNPERSISTED_EVENT]

# ============================================================================
# Source: agent_py_agent/tests/test_rework_r9_sandbox_tmp_publication.py
# ============================================================================

# LLM: 只用 pytest 临时 home 里的合成 owner 目录，经原 attach_run_task_workspace_context 建任务、
#   原 finish_run_task_workspace_if_needed 收口；沙箱 /tmp 里的链接、替换与目标链接都由测试在临时目录直接摆好，
#   受控替换只包住 os.stat/os.open 在指定文件名上先做一次替换再委托原调用，不替换发布实现。
# 模块用途: 回归 P1-R9-27——收口发布沙箱 /tmp 产物只经锚定 no-follow 描述符，链接/非普通/多链接与替换一律不发布，计数如实。

OUTSIDE_BYTES = b"outside-owner synthetic bytes\x00\x01"
_OK = SimpleNamespace(runtime_status="ok", runtime_reason="", runtime_source="")

# LLM: 合成普通 owner 与另一 owner 的私有文件；任务目录由原 attach 组件建立。
# 函数用途: 返回 agent、已绑定参数、任务根与另一 owner 的合成源文件。
def _task(tmp_path: Path, name: str = "publish"):
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", prompt_files=[], my_agent_home=str(tmp_path / "home"),
        my_agent_owner_provider="feishu", my_agent_owner_kind="user", my_agent_owner_id="ou_synthetic",
    ), tmp_path / "workspace")
    params = RunParams(save=True, source="cli_run", request_id=f"request-{name}", run_id=f"run-{name}",
                       task_id=f"task-{name}")
    bound = attach_run_task_workspace_context(agent, params, "合成沙箱产物发布任务")
    root = Path(bound.task_attributes["run_workspace"]["task_root"])
    other = resolve_owner_home(tmp_path / "home", OwnerIdentity.provider_user("feishu", "ou_other")).home_dir
    other.mkdir(parents=True, exist_ok=True)
    outside_file = other / "private.bin"
    outside_file.write_bytes(OUTSIDE_BYTES)
    return SimpleNamespace(agent=agent, bound=bound, root=root, outside_file=outside_file)

# 函数用途: 经原 finish 组件收口，返回结果根。
def _finish(case) -> str:
    return finish_run_task_workspace_if_needed(case.agent, case.bound, _OK)

# 函数用途: 读取任务 timeline 里的沙箱发布事件。
def _published_events(root: Path) -> list[dict]:
    timeline = root / "work" / "timeline.jsonl"
    if not timeline.exists():
        return []
    rows = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("event_type") == "sandbox_tmp_published"]

# 函数用途: 列出交付目录 .sandbox-tmp 下的全部普通文件相对路径。
def _delivered(root: Path) -> list[str]:
    base = root / "output" / ".sandbox-tmp"
    return sorted(str(path.relative_to(base)) for path in base.rglob("*") if path.is_file()) if base.is_dir() else []

# 函数用途: 确认任何交付文件都不含另一 owner 的合成字节。
def _no_outside_bytes_delivered(root: Path) -> bool:
    base = root / "output"
    return all(OUTSIDE_BYTES not in path.read_bytes() for path in base.rglob("*") if path.is_file() and not path.is_symlink())

# LLM: 正常嵌套文件、可执行位与 24 MiB 二进制按字节发布；大文件读取每次不超过 1 MiB，timeline 计数等于真实发布数。
# 函数用途: 验证常规嵌套、生产规模二进制与有界分块读取。
def test_nested_and_large_binary_outputs_publish_exactly(tmp_path, monkeypatch):
    case = _task(tmp_path)
    source = case.root / "work" / ".sandbox-tmp"
    big = random.Random(27).randbytes(24 * 1024 * 1024)
    (source / "proj" / "pkg").mkdir(parents=True)
    (source / "proj" / "pkg" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (source / "proj" / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (source / "proj" / "run.sh").chmod(0o755)
    (source / "big.bin").write_bytes(big)
    reads = []
    original_read = os.read

    def tracking_read(fd, length):
        if os.fstat(fd).st_size == len(big):
            reads.append(length)
        return original_read(fd, length)

    monkeypatch.setattr(os, "read", tracking_read)
    assert _finish(case) == str(case.root)
    monkeypatch.undo()

    target = case.root / "output" / ".sandbox-tmp"
    assert _delivered(case.root) == ["big.bin", "proj/pkg/main.py", "proj/run.sh"]
    assert (target / "big.bin").read_bytes() == big
    assert reads and max(reads) <= 1024 * 1024
    assert stat.S_IMODE((target / "proj" / "run.sh").stat().st_mode) == 0o755
    assert [event["count"] for event in _published_events(case.root)] == [3]
    assert not any(name.startswith(".") for name in os.listdir(target))

# LLM: 叶子链接、目录链接、多链接与 FIFO 都指向或代表不可发布的源；只有普通单链接文件被发布，计数只算它。
# 函数用途: 验证源链接、非普通与多链接条目不发布，另一 owner 字节不出现在交付目录。
def test_source_links_nonregular_and_multilink_entries_are_skipped(tmp_path):
    case = _task(tmp_path)
    source = case.root / "work" / ".sandbox-tmp"
    source.mkdir(parents=True)
    (source / "ok.txt").write_text("ok", encoding="utf-8")
    (source / "linked.txt").symlink_to(case.outside_file)
    (source / "linked-dir").symlink_to(case.outside_file.parent, target_is_directory=True)
    os.link(case.outside_file, source / "hard.bin")
    os.mkfifo(source / "pipe")

    _finish(case)

    assert _delivered(case.root) == ["ok.txt"]
    assert [event["count"] for event in _published_events(case.root)] == [1]
    assert _no_outside_bytes_delivered(case.root)

# LLM: 主位置任一祖先是链接属于不安全而非不存在：不得回落任务根旧位置，也不发布链接目标里的另一 owner 字节。
#   work 本身是链接时原 finish 资格检查先拒绝；直接调用两参发布函数同样不发布。
# 函数用途: 验证链接祖先的整体拒绝与不回落。
@pytest.mark.parametrize("linked", ["sandbox-tmp", "work"])
def test_linked_ancestor_is_refused_without_fallback(tmp_path, linked):
    case = _task(tmp_path)
    outside = case.outside_file.parent / "linked-tmp"
    (outside / ".sandbox-tmp").mkdir(parents=True)
    (outside / "outside.bin").write_bytes(OUTSIDE_BYTES)
    (outside / ".sandbox-tmp" / "outside.bin").write_bytes(OUTSIDE_BYTES)
    (case.root / ".sandbox-tmp").mkdir()
    (case.root / ".sandbox-tmp" / "fallback.txt").write_text("旧位置", encoding="utf-8")
    if linked == "sandbox-tmp":
        (case.root / "work" / ".sandbox-tmp").symlink_to(outside, target_is_directory=True)
        assert _finish(case) == str(case.root)
    else:
        work = case.root / "work"
        work.rename(case.root / "work-real")
        work.symlink_to(outside, target_is_directory=True)
        assert _finish(case) == ""
        assert _publish_sandbox_tmp_outputs(case.root, "run-direct") == []

    assert _delivered(case.root) == []
    assert _published_events(case.root) == [] or linked == "work"
    assert _no_outside_bytes_delivered(case.root)

# LLM: 主位置真实不存在才使用任务根旧位置；主位置是普通文件（不安全）时不回落。
# 函数用途: 验证兼容回落布局的准入条件与 timeline 来源。
@pytest.mark.parametrize("primary", ["absent", "regular-file"])
def test_fallback_layout_only_when_primary_is_absent(tmp_path, primary):
    case = _task(tmp_path)
    (case.root / ".sandbox-tmp" / "notes").mkdir(parents=True)
    (case.root / ".sandbox-tmp" / "notes" / "a.txt").write_text("hi", encoding="utf-8")
    if primary == "regular-file":
        (case.root / "work" / ".sandbox-tmp").write_text("not a directory", encoding="utf-8")

    _finish(case)

    if primary == "absent":
        assert _delivered(case.root) == ["notes/a.txt"]
        events = _published_events(case.root)
        assert [(event["count"], event["source"]) for event in events] == [(1, str(case.root / ".sandbox-tmp"))]
    else:
        assert _delivered(case.root) == [] and _published_events(case.root) == []

# LLM: 目标基目录链接、嵌套目录链接与叶子链接都不得被跟随：链接外文件逐字节不变，其余正常产物照常发布。
# 函数用途: 验证目标链接不被跟随且计数只含真正发布的文件。
@pytest.mark.parametrize("linked", ["base", "nested-dir", "leaf"])
def test_destination_links_are_not_followed(tmp_path, linked):
    case = _task(tmp_path)
    source = case.root / "work" / ".sandbox-tmp"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "a.txt").write_text("nested", encoding="utf-8")
    (source / "top.txt").write_text("top", encoding="utf-8")
    outside = case.outside_file.parent / "dest-outside"
    outside.mkdir()
    (outside / "top.txt").write_bytes(OUTSIDE_BYTES)
    target = case.root / "output" / ".sandbox-tmp"
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = {"base": [], "nested-dir": ["top.txt"], "leaf": ["nested/a.txt"]}[linked]
    if linked == "base":
        target.symlink_to(outside, target_is_directory=True)
    else:
        target.mkdir()
        link = target / ("nested" if linked == "nested-dir" else "top.txt")
        link.symlink_to(outside if linked == "nested-dir" else outside / "top.txt",
                        target_is_directory=linked == "nested-dir")

    _finish(case)

    assert sorted(os.listdir(outside)) == ["top.txt"] and (outside / "top.txt").read_bytes() == OUTSIDE_BYTES
    published = [str(path.relative_to(target)) for path in sorted(target.rglob("*"))
                 if not path.is_symlink() and path.is_file()] if linked != "base" else []
    assert published == expected
    assert [event["count"] for event in _published_events(case.root)] == ([len(expected)] if expected else [])

# LLM: 发现之后、打开之前被替换（链接或另一普通文件）的源一律不发布；包装只在 replaced.txt 的指定调用点先替换一次再委托原调用。
# 函数用途: 验证发现与打开之间的受控替换不会发布替换后的内容。
@pytest.mark.parametrize(("hook", "replacement"), [
    ("second-stat", "regular"), ("open", "regular"), ("open", "symlink"),
])
def test_replacement_between_discovery_and_open_is_not_published(tmp_path, monkeypatch, hook, replacement):
    case = _task(tmp_path)
    source = case.root / "work" / ".sandbox-tmp"
    source.mkdir(parents=True)
    replaced = source / "replaced.txt"
    replaced.write_text("原始内容", encoding="utf-8")
    (source / "z-ok.txt").write_text("ok", encoding="utf-8")
    seen = {"stat": 0, "swapped": False}

    def swap() -> None:
        staged = source / ".staged"
        if replacement == "symlink":
            staged.symlink_to(case.outside_file)
        else:
            staged.write_bytes(OUTSIDE_BYTES)
        os.replace(staged, replaced)
        seen["swapped"] = True

    original_stat, original_open = os.stat, os.open

    def hooked_stat(path, *args, **kwargs):
        if path == "replaced.txt" and kwargs.get("dir_fd") is not None and not seen["swapped"]:
            seen["stat"] += 1
            if hook == "second-stat" and seen["stat"] == 2:
                swap()
        return original_stat(path, *args, **kwargs)

    def hooked_open(path, flags, *args, **kwargs):
        if hook == "open" and path == "replaced.txt" and not flags & os.O_CREAT and not seen["swapped"]:
            swap()
        return original_open(path, flags, *args, **kwargs)

    # 原能力探测按函数对象查 supports_dir_fd；包装函数委托原调用，需登记为同等能力，否则发布按无能力平台整体不发布。
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, hooked_stat, hooked_open})
    monkeypatch.setattr(os, "stat", hooked_stat)
    monkeypatch.setattr(os, "open", hooked_open)
    _finish(case)
    monkeypatch.undo()

    assert seen["swapped"]
    assert _delivered(case.root) == ["z-ok.txt"]
    assert [event["count"] for event in _published_events(case.root)] == [1]
    assert _no_outside_bytes_delivered(case.root)

# ============================================================================
# Source: agent_py_agent/tests/test_rework_r9_task_binding_cancel.py
# R9 P1-R9-03：取消在正文入队前或绑定写入前提交时，绑定不得复活任务、不得再唤醒模型。
# ============================================================================

@dataclass
class _Case:
    agent: object
    backend: object
    scheduler: object
    call: object
    space_id: str
    view: dict

def _native_business(tmp_path) -> _Case:
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        bind_business_context,
    )
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "native-home"),
        business_spaces_enabled=True, enable_tools=True, prompt_files=[]), tmp_path / "workspace")
    store, owner = agent.conversation_store, str(agent.home_paths.owner_id)
    source = store.threads.create(owner_id=owner, canonical_user_id="local-agent")
    actor = HostChannelActor(owner, source.thread_id, "binding-cancel", "local", "local", "local-agent")
    context = bind_business_context(agent, authorize=lambda action, facts: True, actor=actor)
    services = BusinessServices(agent)
    space = services.spaces.create({"name": "绑定取消空间"}, actor=services.actor(context),
                                   idempotency_key="binding-cancel-space")

    def call(action, params, **options):
        return services.background.execute(action, params, context=context, actor=actor, **options)

    view = call("background_agents.create", {"space_id": space["space_id"], "name": "原执行者"},
                idempotency_key="binding-cancel-worker")["agent"]

    class CountingBackend:
        name = "binding-cancel-counting-model"

        def __init__(self):
            self.calls = 0

        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name, endpoint="local://test", model="", stream=False,
                native_supported=True, evidence="contract_test", observed_at=_utc_now_iso())

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            return ModelResponse(text="original worker reply", backend=self.name)

    agent.backend = backend = CountingBackend()
    scheduler = BackgroundMainAgentScheduler({"runtime": BackgroundMainAgentRuntime(agent=agent, store=store), "store": store})
    return _Case(agent, backend, scheduler, call, space["space_id"], view)

def _assign(case, key):
    return case.call("tasks.assign", {"space_id": case.space_id, "agent_id": case.view["agent_id"],
                                      "instruction": "读取原资料后汇报"}, idempotency_key=key)["task_id"]

def _cancel_concurrently(case, task_id):
    # 另一个调用方线程完成整次原取消后才放行派发线程：取消恰好在此刻提交。
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(case.call, "tasks.cancel", {"task_id": task_id, "include_descendants": True}).result(timeout=30)

def _thread_wakes(store, thread_id):
    return [wake for wake in store.wakes.pending(limit=0) if wake.thread_id == thread_id]

def _tick(case, rounds=3):
    for _ in range(rounds):
        case.scheduler.tick()

def _retire_and_drain(case):
    store = case.agent.conversation_store
    ready_index(store)
    retiring = case.call("background_agents.retire", {"agent_id": case.view["agent_id"], "disposition": "drain"},
                         expected_revision=case.view["revision"], idempotency_key="binding-cancel-retire")
    assert retiring["agent"]["identity_state"] in {"retiring", "retired"}
    return eventually_retired(case.agent, case.view["agent_id"])

def test_uncancelled_dispatch_still_wakes_and_reaches_model(tmp_path):
    # 对照：同一真实链路未取消时照常唤醒并执行，下面"零调用"断言不是空转。
    case = _native_business(tmp_path)
    store = case.agent.conversation_store
    task_id = _assign(case, "control-dispatch")
    task = store.session_tasks.require(task_id)
    assert task.status == "queued" and task.body_guidance_id
    assert _thread_wakes(store, task.target_thread_id)
    _tick(case, rounds=1)
    assert case.backend.calls >= 1

def test_cancel_before_body_enqueue_publishes_no_wake_and_settles_late_body(tmp_path, monkeypatch):
    case = _native_business(tmp_path)
    store = case.agent.conversation_store
    append_once, fired = store.guidance.append_once, []

    def cancel_then_append(request, **kwargs):
        metadata = request.get("metadata") or {}
        if metadata.get("origin_kind") == "session_task" and not fired:
            fired.append(_cancel_concurrently(case, metadata["session_task_id"]))
        return append_once(request, **kwargs)

    monkeypatch.setattr(store.guidance, "append_once", cancel_then_append)
    task_id = _assign(case, "cancel-before-enqueue")
    # 取消提交时尚无正文：原取消按"从未发表"完成；派发随后入队的迟到正文不能被遗留或唤醒。
    assert fired == [{"operation_id": task_id, "state": "completed", "outstanding_refs": []}]
    task = store.session_tasks.require(task_id)
    assert task.status == "cancelled" and task.body_guidance_id
    assert task.metadata["cancellation_control"]["outstanding_refs"] == []
    receipt = store.guidance.receipt(task.body_dedupe_key)
    assert receipt.status == "rejected" and receipt.entry.guidance_id == task.body_guidance_id
    assert not _thread_wakes(store, task.target_thread_id)
    assert not store.guidance.pending_report("thread", task.target_thread_id, limit=0)[0]
    _tick(case)
    assert case.backend.calls == 0
    assert store.session_tasks.require(task_id).status == "cancelled"
    assert _retire_and_drain(case).metadata["background_agent"]["identity_state"] == "retired"
    assert case.backend.calls == 0

def test_cancel_committed_during_binding_stays_cancelled_and_never_reaches_model(tmp_path, monkeypatch):
    case = _native_business(tmp_path)
    store = case.agent.conversation_store
    dispatcher, state = threading.get_ident(), {"binding": "", "fired": []}
    bind_body = store.session_tasks.bind_body

    def binding(task_id, body_guidance_id):
        state["binding"] = task_id
        try:
            return bind_body(task_id, body_guidance_id)
        finally:
            state["binding"] = ""

    def cancel_before_bind_write(original):
        # 绑定已开始、其写入尚未发生时提交原取消；旧实现在锁外已读过queued快照。
        def wrapper(path, *args, **kwargs):
            task_id = state["binding"]
            if (task_id and not state["fired"] and threading.get_ident() == dispatcher
                    and path == store.session_tasks._path(task_id)):
                state["fired"].append(_cancel_concurrently(case, task_id))
            return original(path, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(store.session_tasks, "bind_body", binding)
    for name in ("update_json_file_atomic", "write_json_file_atomic"):
        monkeypatch.setattr(session_tasks_module, name, cancel_before_bind_write(getattr(session_tasks_module, name)))
    task_id = _assign(case, "cancel-during-binding")
    # 正文已入队但尚未绑定：原取消不能确认这条正文，保留本任务责任。
    assert state["fired"] == [{"operation_id": task_id, "state": "pending", "outstanding_refs": [task_id]}]
    task = store.session_tasks.require(task_id)
    assert task.status == "cancelled", "binding resurrected a committed cancellation"
    control = task.metadata["cancellation_control"]
    assert control["outstanding_refs"] == [task_id]
    assert task.body_guidance_id
    receipt = store.guidance.receipt(task.body_dedupe_key)
    assert receipt.status == "rejected" and receipt.entry.guidance_id == task.body_guidance_id
    assert not _thread_wakes(store, task.target_thread_id)
    _tick(case)
    assert case.backend.calls == 0
    # 原取消重试收掉已结清正文的同一责任，不复制第二份。
    assert case.call("tasks.cancel", {"task_id": task_id, "include_descendants": True}) == {
        "operation_id": task_id, "state": "completed", "outstanding_refs": []}
    assert store.session_tasks.require(task_id).status == "cancelled"
    assert _retire_and_drain(case).metadata["background_agent"]["identity_state"] == "retired"
    assert case.backend.calls == 0

def test_unsettled_late_body_keeps_original_cancellation_responsibility(tmp_path, monkeypatch):
    case = _native_business(tmp_path)
    store = case.agent.conversation_store
    append_once, mark_status = store.guidance.append_once, store.guidance.mark_status
    fired, failures = [], []

    def cancel_then_append(request, **kwargs):
        metadata = request.get("metadata") or {}
        if metadata.get("origin_kind") == "session_task" and not fired:
            fired.append(_cancel_concurrently(case, metadata["session_task_id"]))
            failures.append("armed")
        return append_once(request, **kwargs)

    def failing_mark_status(key, status):
        if failures == ["armed"]:
            failures.append("raised")
            raise OSError("simulated receipt write error")
        return mark_status(key, status)

    monkeypatch.setattr(store.guidance, "append_once", cancel_then_append)
    monkeypatch.setattr(store.guidance, "mark_status", failing_mark_status)
    task_id = _assign(case, "late-body-unsettled")
    assert failures == ["armed", "raised"]
    task = store.session_tasks.require(task_id)
    assert task.status == "cancelled"
    assert task.metadata["cancellation_control"]["outstanding_refs"] == [task_id]
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    assert not _thread_wakes(store, task.target_thread_id)
    _tick(case)
    assert case.backend.calls == 0
    assert case.call("tasks.cancel", {"task_id": task_id, "include_descendants": True}) == {
        "operation_id": task_id, "state": "completed", "outstanding_refs": []}
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert _retire_and_drain(case).metadata["background_agent"]["identity_state"] == "retired"
    assert case.backend.calls == 0

def test_bind_body_only_fills_empty_reference_and_preserves_latest_record(tmp_path):
    from agent_py_agent.agent.conversation import ConversationStore

    store = ConversationStore(tmp_path / "conversations")
    sender = store.threads.create(owner_id="owner", canonical_user_id="u")
    target = store.threads.create(owner_id="owner", canonical_user_id="u")
    tasks: SessionTaskStore = store.session_tasks
    task = tasks.create(SessionTaskDraft(sender.thread_id, target.thread_id, "goal", body_dedupe_key="body:k"))
    tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))
    controlled = tasks.record_cancellation_control(task.task_id, outstanding_refs=["exact-ref"])
    bound = tasks.bind_body(task.task_id, "guidance-1")
    assert bound.status == "cancelled" and bound.body_guidance_id == "guidance-1"
    assert bound.metadata == controlled.metadata
    assert tasks.bind_body(task.task_id, "guidance-1") == bound
    assert tasks.bind_body(task.task_id, "guidance-other") == bound
    assert tasks.bind_body(task.task_id, "") == bound
    assert tasks.bind_body("stask-missing", "guidance-1") is None
    with pytest.raises(ValueError):
        tasks.advance(task.task_id, SessionTaskUpdate(status="queued"))

# ============================================================================
# Source: agent_py_agent/tests/test_rework_r9_terminal_observation_retirement.py
# R9-23：终态任务的观察兜底结案沿原观察账 mark_handled(ids, now=...)，只结确切观察，之后到期策略照常处理且不调模型。
# ============================================================================

# 全部不可复活的 TaskLink 终态，外加历史大写写法（领取准入按大小写无关判定终态）。
_TERMINAL_STATUSES = sorted(THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES) + ["DONE"]

def _handled(store, thread_id):
    events, errors = store.observations.recent_report(thread_id, limit=0)
    assert not errors
    return {event.observation_id: event.handled_at for event in events if event.handled_at > 0}

# LLM: 观察与唤醒沿原账本 append/raise_signal 互相链接；另一条同根非主代理观察不进入批次，用来证明只结确切 ID。
# 函数用途: 建一个根任务已终态、带待处理完成唤醒、链接观察和到期进度策略的真实后台场景。
def _terminal_case(tmp_path, terminal=None):
    agent, store, thread, runtime, first, _second = _child_settlement_fixture(tmp_path)
    backend = _NaturalCompletionBackend()
    agent.backend = backend
    linked = store.observations.append({
        "thread_id": thread.thread_id, "event_type": "subagent_runner_finished", "root_task_id": "task-root",
        "source_agent_id": first.id, "requires_main_agent": True, "requires_llm_report": True,
        "metadata": {"task_id": first.id, "status": "DONE"}, "now": 20.0,
    })
    bystander = store.observations.append({
        "thread_id": thread.thread_id, "event_type": "subagent_progress", "root_task_id": "task-root",
        "source_agent_id": first.id, "metadata": {"task_id": first.id}, "now": 21.0,
    })
    wake = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "reason": "subagent_runner_finished", "root_task_id": "task-root",
        "source_agent_id": first.id, "observation": linked,
        "metadata": {"task_id": first.id, "status": "DONE"}, "now": 20.0,
    })
    if terminal is not None:
        store.tasks.update_status({"task_id": "task-root", "status": terminal, "now": 30.0})
    policy = store.progress.create({
        "thread_id": thread.thread_id, "task_id": "task-root", "interval_seconds": 60,
        "route_channel": "internal", "route_target": "thread-1", "now": 12.0,
    })
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    return store, thread, backend, scheduler, (linked, bystander, wake, policy)

@pytest.mark.parametrize("terminal", _TERMINAL_STATUSES)
def test_ledger_error_after_wake_removal_retires_exact_observation_on_retry(tmp_path, monkeypatch, terminal):
    store, thread, backend, scheduler, (linked, bystander, wake, policy) = _terminal_case(tmp_path, terminal)
    index = store.observations._index
    original = index._append
    failures = []

    def ledger_write(pairs):
        if not failures:
            failures.append(index.journal_path)
            raise OSError("simulated I/O error")
        return original(pairs)

    monkeypatch.setattr(index, "_append", ledger_write)
    # 唤醒先写 handled 回执并移出 pending，随后观察处理账写失败；失败照常上抛，不吞成成功。
    with pytest.raises(OSError, match="simulated I/O error"):
        scheduler.tick(now=100.0)
    assert len(failures) == 1
    assert store.wakes.pending_one(wake.wake_signal_id) is None
    assert _handled(store, thread.thread_id) == {}
    assert store.progress.load(policy.policy_id).enabled is True

    # 普通下一拍：观察兜底车道只结这条确切观察，再走到到期策略并按终态退休，全程不调模型。
    assert scheduler.tick(now=200.0) == []
    handled = _handled(store, thread.thread_id)
    assert set(handled) == {linked.observation_id}
    assert bystander.observation_id not in handled
    assert [row["reason"] for row in scheduler.last_progress_policy_suppressed] == ["terminal_task_link"]
    assert store.progress.load(policy.policy_id).enabled is False
    assert store.tasks.load("task-root").status == terminal
    assert backend.prompts == []

    # 重复拍幂等：不再重领、不改处理时间、不重开执行。
    for current in (300.0, 400.0):
        assert scheduler.tick(now=current) == []
        assert _handled(store, thread.thread_id) == handled
        assert store.progress.load(policy.policy_id).enabled is False
        assert store.tasks.load("task-root").status == terminal
    assert backend.prompts == []
    assert len(failures) == 1

@pytest.mark.parametrize("case", ["active_root", "foreign_root", "foreign_thread"])
def test_terminal_source_cleanup_never_retires_inexact_observation(tmp_path, case):
    from agent_py_agent.agent.conversation.runtime import _retire_terminal_background_source

    terminal = None if case == "active_root" else "completed"
    store, thread, _backend, _scheduler, (linked, _bystander, _wake, _policy) = _terminal_case(tmp_path, terminal)
    assert store.tasks.load("task-root").status == ("active" if terminal is None else terminal)
    kwargs = {"thread_id": thread.thread_id, "task_id": "task-root", "wake_signal": {
        "kind": "observation_fallback", "root_task_id": "task-root", "observation_ids": [linked.observation_id]}}
    if case == "foreign_root":
        kwargs["wake_signal"] = {**kwargs["wake_signal"], "root_task_id": "task-other"}
    if case == "foreign_thread":
        kwargs["thread_id"] = "thread-foreign"
    _retire_terminal_background_source(store, kwargs)
    assert _handled(store, thread.thread_id) == {}

# ============================================================================
# Source: agent_py_agent/tests/test_rework_r9_web_artifact_owner_root.py
# ============================================================================

# LLM: 只用 pytest 临时 home、两个合成普通 owner 的原 SimpleAgent/Registry/Executor 与本机随机端口服务；
#   私网授权、网络安全门与 owner 路径墙全部走原实现，不替换解析、抓取、产物保存或读文件逻辑。
# 模块用途: 回归 P1-R9-26——owner 注册表把网页产物落到各自 owner 根的 artifacts/web，独立构造仍走原默认目录。

# 正文远超 256 字符的最小预览；二进制含 NUL 与全部字节值，按字节和摘要核对。
LONG_TEXT = "".join(f"第{n:03d}行 合成网页正文\n" for n in range(120))
BINARY = bytes(range(256)) * 300 + b"\x00\xff tail"
_ROUTES = {
    "/text": (LONG_TEXT.encode("utf-8"), "text/plain; charset=utf-8"),
    "/blob": (BINARY, "application/octet-stream"),
}
# 私网授权只经调用写边界进入原调用副本，与生产逐调用授权同一路径。
_GRANT = {"allowed_private_hosts": ["127.0.0.1"], "allow_private_resolution": True}

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        body, content_type = _ROUTES.get(self.path, (b"missing", "text/plain"))
        self.send_response(200 if self.path in _ROUTES else 404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

# 函数用途: 在本机随机端口起一个只服务固定合成内容的 HTTP 服务。
@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    thread.join(2)
    srv.server_close()

# 函数用途: 构造一个普通 owner 的原 Agent，工具表由原 core 装配。
def _owner(tmp_path: Path, user_id: str) -> SimpleAgent:
    return SimpleAgent(AgentConfig(
        model_backend="echo", enable_plugins=False, my_agent_home=str(tmp_path / "home"),
        my_agent_owner_provider="feishu", my_agent_owner_kind="user", my_agent_owner_id=user_id,
        access_mode="workspace-write",
    ), tmp_path / f"workspace-{user_id}")

# 函数用途: 返回该 owner 的规范网页产物目录。
def _web_root(agent: SimpleAgent) -> Path:
    return Path(agent.home_paths.owner_home_dir) / "artifacts" / "web"

# 函数用途: 经原 ToolCall/Registry/Executor 入口执行一次工具调用。
def _call(agent, tool_name, arguments, call_id, *, registry=None, boundary=None):
    return execute_registry_test_call(registry or agent.tools, tool_name, arguments, write_boundary=boundary,
                                      register_with=agent, call_id=call_id)

# LLM: 两个普通 owner 抓同一页：各自产物落在自己的 owner 根，预览只有 256 字符而产物保留全文，
#   本人经原 read_file 读回全文，另一 owner 读同一路径被 owner 墙拒绝。
# 函数用途: 验证 owner 归属、预览之外的全文与跨 owner 拒绝。
def test_two_owners_keep_full_text_under_their_own_root(tmp_path, server):
    alice, bob = _owner(tmp_path, "ou_alice"), _owner(tmp_path, "ou_bob")
    pages = {}
    for name, agent in (("alice", alice), ("bob", bob)):
        result = _call(agent, "web_fetch", {"urls": [f"{server}/text"], "max_chars": 256}, f"{name}-text",
                       boundary=_GRANT)
        assert result.ok, result.output
        pages[name] = result.metadata["handler_details"]["pages"][0]

    alice_ref, bob_ref = Path(pages["alice"]["artifact_ref"]), Path(pages["bob"]["artifact_ref"])
    assert alice_ref.parent == _web_root(alice) and bob_ref.parent == _web_root(bob)
    assert len(pages["alice"]["preview"]) == 256 < len(LONG_TEXT)
    assert alice_ref.read_text(encoding="utf-8") == bob_ref.read_text(encoding="utf-8") == LONG_TEXT
    assert pages["alice"]["content_hash"] == hashlib.sha256(LONG_TEXT.encode("utf-8")).hexdigest()

    read_back = _call(alice, "read_file", {"path": str(alice_ref)}, "alice-read")
    assert read_back.ok, read_back.output
    assert LONG_TEXT.splitlines()[-1] in read_back.output
    denied = _call(bob, "read_file", {"path": str(alice_ref)}, "bob-read")
    assert (denied.ok, denied.error_code) == (False, "PATH_CROSS_OWNER_BLOCKED")

# LLM: 访问策略副本重建同一注册参数，调用副本浅拷贝原工具；两层副本都必须保持同一 owner 根，
#   非文本响应按原字节落盘，摘要与长度如实。
# 函数用途: 验证访问策略副本与调用副本保持根，以及非文本字节与摘要。
def test_nontext_bytes_and_hash_survive_access_policy_copy(tmp_path, server):
    alice = _owner(tmp_path, "ou_alice")
    owner_root = str(alice.home_paths.owner_home_dir)
    view = alice.tools.with_access_policy(access_mode="restricted", path_access_mode=alice.tools.path_access_mode,
                                          owner_scope_root=owner_root)
    assert view is not alice.tools
    assert view.tools["web_fetch"].artifact_root == alice.tools.tools["web_fetch"].artifact_root == _web_root(alice)

    result = _call(alice, "web_fetch", {"url": f"{server}/blob"}, "alice-blob", registry=view, boundary=_GRANT)

    assert result.ok, result.output
    artifact = result.metadata["handler_details"]["artifact"]
    saved = Path(artifact["path"])
    assert saved.parent == _web_root(alice)
    assert saved.read_bytes() == BINARY
    assert (artifact["sha256"], artifact["bytes"]) == (hashlib.sha256(BINARY).hexdigest(), len(BINARY))

# LLM: 进程级环境覆盖只影响独立构造；owner 注册表不回落到它。未授权私网的调用仍被原网络门拒绝且不落产物。
# 函数用途: 验证独立构造兼容、owner 不回落环境覆盖与网络安全门保持。
def test_standalone_default_kept_and_owner_ignores_process_override(tmp_path, server, monkeypatch):
    override = tmp_path / "process-wide-web"
    monkeypatch.setenv("MY_AGENT_WEB_ARTIFACT_ROOT", str(override))
    assert WebFetchTool(max_chars=1000, timeout=5).artifact_root == override
    alice = _owner(tmp_path, "ou_alice")

    blocked = _call(alice, "web_fetch", {"url": f"{server}/blob"}, "alice-no-grant")
    assert not blocked.ok and blocked.error_code
    assert not _web_root(alice).exists()

    granted = _call(alice, "web_fetch", {"url": f"{server}/blob"}, "alice-grant", boundary=_GRANT)
    assert granted.ok, granted.output
    assert Path(granted.metadata["handler_details"]["artifact"]["path"]).parent == _web_root(alice)
    assert not override.exists()
