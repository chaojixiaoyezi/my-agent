# LLM: Exercise real registry, worker and pool lifecycle boundaries with isolated temporary owners; no production services or credentials.
# 模块用途: 覆盖双用户及父子并发隔离、MCP借用、池重开和准入背压。
from __future__ import annotations

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner import worker as runner
from agent_py_agent.agent.gateway_parts import request_worker as requests
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


# LLM: Only the external MCP client is a controlled double; ownership, counters, locks and close paths are real.
# 类用途: 记录连接是否被提前关闭和实际关闭次数。
class Client:
    # LLM: Each test receives a fresh lifecycle, with no subprocess or shared state.
    # 函数用途: 初始化可观察的独立连接。
    def __init__(self):
        self.stops = 0

    # LLM: Keep stop observable and idempotent like a closed transport.
    # 函数用途: 记录首次关闭。
    def stop(self):
        if not self.stops:
            self.stops += 1

    # LLM: No external process is represented by this double.
    # 函数用途: 返回连接关闭状态。
    def is_closed(self):
        return bool(self.stops)

    # LLM: Running is the inverse of the observed close state.
    # 函数用途: 返回连接存活状态。
    def is_running(self):
        return not self.stops


# LLM: Use the real deferred registry constructor and explicit stable owner IDs, not a copied parent tool dictionary.
# 函数用途: 创建不会启动外部服务的测试工具表。
def registry(tmp_path, owner="test/user/a", **options):
    return ToolRegistry(ToolRegistryParams(
        workspace_root=tmp_path, max_chars=1000, max_entries=20, max_matches=10,
        web_max_chars=1000, http_timeout=10, catalog_limit=10, retrieval_limit=3,
        vector_search_enabled=False, defer_mcp_connect=True,
        operation_owner_id=owner, **options,
    ))


# LLM: Missing new API is an explicit contract failure before any attempt to invoke it.
# 函数用途: 在真实注册表上接入借用并断言成功。
def borrow(child, parent):
    share = getattr(child, "share_same_owner_mcp_state_from", None)
    assert callable(share), "same-owner MCP borrowing API is missing"
    assert share(parent) is True


# LLM: The policy view must release its own lease, never close the parent's underlying MCP connection.
# 函数用途: 验证浅拷贝权限视图不再误关父连接。
def test_policy_view_close_keeps_parent_mcp_alive(tmp_path):
    parent = registry(tmp_path)
    client = Client()
    parent._mcp_clients.append(client)
    parent._mcp_connected_state[0] = True
    view = parent.with_access_policy(access_mode="full-access", path_access_mode="full-access", owner_scope_root="")
    try:
        view.close_mcp_clients()
        assert not parent._mcp_closed.is_set()
        assert client.is_running() and parent._mcp_clients == [client]
        assert isinstance(view._mcp_clients, list) and view._mcp_clients == []
    finally:
        parent.close_mcp_clients()


# LLM: Worker cleanup must route borrowed registries through their ref-counted detach path, including repeated cleanup.
# 函数用途: 验证子代理退出只释放借用，不误关父连接。
def test_worker_cleanup_releases_borrow_once(tmp_path):
    parent, child = registry(tmp_path), registry(tmp_path)
    client = Client()
    parent._mcp_clients.append(client)
    parent._mcp_connected_state[0] = True
    borrow(child, parent)
    params = SimpleNamespace(request_transport=SimpleNamespace(scope=SimpleNamespace(execution=SimpleNamespace(agent=SimpleNamespace(tools=parent)))))
    try:
        assert parent._mcp_borrow_count == [1]
        runner._close_worker_mcp_clients(SimpleNamespace(tools=child), params)
        child.close_mcp_clients()
        assert parent._mcp_borrow_count == [0]
        assert client.is_running() and not parent._mcp_closed.is_set()
    finally:
        parent.close_mcp_clients()


# LLM: A repeated adoption must reject before trying to acquire the same non-reentrant prepare lock twice.
# 函数用途: 验证重复借用不会死锁或重复增加引用计数。
def test_repeated_borrow_is_nonblocking(tmp_path):
    parent, child = registry(tmp_path), registry(tmp_path)
    borrow(child, parent)
    result = []
    thread = threading.Thread(target=lambda: result.append(child.share_same_owner_mcp_state_from(parent)), daemon=True)
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive(), "repeated borrowing deadlocked on shared prepare lock"
    assert result == [False] and parent._mcp_borrow_count == [1]
    child.close_mcp_clients()
    parent.close_mcp_clients()


# LLM: Existing run views are temporary and not all callers explicitly close them; garbage collection must release their lease once.
# 函数用途: 验证临时权限视图回收后不会永久钉住父MCP生命周期。
def test_unused_policy_view_releases_borrow_on_gc(tmp_path):
    import gc
    import weakref

    parent = registry(tmp_path)
    view = parent.with_access_policy(access_mode="full-access", path_access_mode="full-access", owner_scope_root="")
    reference = weakref.ref(view)
    completed = view._mcp_borrow_release_completed
    del view
    gc.collect()
    assert completed.wait(10)
    assert reference() is None
    assert parent._mcp_borrow_count == [0]
    parent.close_mcp_clients()


# LLM: A connection attempt can pass the fast check before a borrowed facade is released; recheck under prepare too.
# 函数用途: 验证排队中的初连不会在借用已释放后新建失管连接。
def test_queued_ensure_cannot_connect_released_borrow(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import registry as module

    parent, child = registry(tmp_path), registry(tmp_path)
    borrow(child, parent)
    waiting, proceed = threading.Event(), threading.Event()
    original = parent._mcp_prepare_lock
    connected, result = [], []
    monkeypatch.setattr(module, "_connect_mcp_servers", lambda *_args: connected.append(True) or [])

    # LLM: Wrap only acquisition timing, keeping the original lock and production connection implementation seam.
    # 类用途: 在初连快检后、取得prepare锁之前安排释放交错。
    class GateLock:
        # LLM: Main-thread release is not delayed.
        # 函数用途: 只阻塞指定初连线程。
        def __enter__(self):
            if threading.current_thread().name == "late-ensure":
                waiting.set()
                assert proceed.wait(10)
            original.acquire()
            return self

        # LLM: Always release the actual original mutex.
        # 函数用途: 退出时归还原锁。
        def __exit__(self, *_args):
            original.release()

    parent._mcp_prepare_lock = child._mcp_prepare_lock = GateLock()
    thread = threading.Thread(name="late-ensure", target=lambda: result.append(child.ensure_mcp_connected()))
    thread.start()
    try:
        assert waiting.wait(10)
        child.close_mcp_clients()
    finally:
        proceed.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert result == [False] and connected == []
    parent.close_mcp_clients()


# LLM: Starting prepare closes the fresh-facade adoption window before capturing a mutex that sharing might otherwise replace.
# 函数用途: 验证正在初连的门面不能在途中改借父连接和锁。
def test_share_rejects_facade_with_queued_initial_ensure(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import registry as module

    parent, child = registry(tmp_path), registry(tmp_path)
    original = child._mcp_prepare_lock
    waiting, proceed = threading.Event(), threading.Event()
    monkeypatch.setattr(module, "_connect_mcp_servers", lambda *_args: [])

    # LLM: Preserve original mutual exclusion, delaying only the first connection's lock acquisition.
    # 类用途: 固定首次prepare已开始但尚未拿到原锁的交错。
    class GateLock:
        # LLM: Do not stall the borrowing caller.
        # 函数用途: 在后台初连拿锁前通知主线程。
        def __enter__(self):
            if threading.current_thread().name == "first-ensure":
                waiting.set()
                assert proceed.wait(10)
            original.acquire()

        # LLM: Release exactly the original mutex.
        # 函数用途: 归还原prepare锁。
        def __exit__(self, *_args):
            original.release()

    child._mcp_prepare_lock = GateLock()
    thread = threading.Thread(name="first-ensure", target=child.ensure_mcp_connected)
    thread.start()
    try:
        assert waiting.wait(10)
        shared = child.share_same_owner_mcp_state_from(parent)
    finally:
        proceed.set()
        thread.join(timeout=10)
        child.close_mcp_clients()
        parent.close_mcp_clients()
    assert not thread.is_alive()
    assert shared is False


# LLM: Exercise finalizer release both with and without an earlier explicit close; another live borrower must retain the parent.
# 函数用途: 验证显式关闭和回收不会重复扣数，最后一份回收可完成延迟关闭。
@pytest.mark.parametrize("explicit", [False, True])
def test_gc_borrow_release_is_once_and_finishes_deferred_close(tmp_path, explicit):
    import gc

    parent, first, last = registry(tmp_path), registry(tmp_path), registry(tmp_path)
    client = Client()
    parent._mcp_clients.append(client)
    parent._mcp_connected_state[0] = True
    borrow(first, parent)
    borrow(last, parent)
    first_done, last_done = first._mcp_borrow_release_completed, last._mcp_borrow_release_completed
    parent.close_mcp_clients()
    if explicit:
        first.close_mcp_clients()
    del first
    gc.collect()
    assert first_done.wait(10)
    assert parent._mcp_borrow_count == [1] and client.stops == 0
    del last
    gc.collect()
    assert last_done.wait(10)
    assert parent._mcp_borrow_count == [0] and client.stops == 1
    assert parent._mcp_closed.is_set()


# LLM: GC can run during allocation inside prepare; its callback must never reacquire prepare or wait for lifecycle.
# 函数用途: 复现独审指出的GC回调同线程自锁，要求回收线程可正常退出。
def test_gc_inside_prepare_does_not_deadlock(tmp_path):
    parent = registry(tmp_path)
    completions = []

    # LLM: Daemon bounds the failure specimen so the original deadlock cannot hang pytest shutdown.
    # 函数用途: 在真实prepare临界区内回收临时视图。
    def collect_view():
        import gc

        view = parent.with_access_policy(access_mode="full-access", path_access_mode="full-access", owner_scope_root="")
        completions.append(view._mcp_borrow_release_completed)
        with parent._mcp_prepare_lock:
            del view
            gc.collect()

    thread = threading.Thread(target=collect_view, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive(), "GC finalizer deadlocked inside prepare"
    assert completions[0].wait(10)
    parent.close_mcp_clients()


# LLM: Owner shutdown cannot interrupt a live same-owner child or view; the final release closes exactly once.
# 函数用途: 验证父关闭延迟到最后一个借用者退出。
def test_parent_close_waits_for_child_and_policy_view(tmp_path):
    parent, child = registry(tmp_path), registry(tmp_path)
    client = Client()
    parent._mcp_clients.append(client)
    borrow(child, parent)
    view = parent.with_access_policy(access_mode="full-access", path_access_mode="full-access", owner_scope_root="")
    try:
        parent.close_mcp_clients()
        assert client.is_running() and not parent._mcp_closed.is_set()
        child.close_mcp_clients()
        assert client.is_running()
        view.close_mcp_clients()
        assert parent._mcp_closed.is_set() and client.stops == 1
        assert parent._mcp_borrow_count == [0]
    finally:
        child.close_mcp_clients()
        view.close_mcp_clients()
        parent.close_mcp_clients()


# LLM: Config equality includes server declarations (including timeouts), credential environment boundaries and process identity.
# 函数用途: 验证不同配置或进程不能借用，拒绝时保持独立延迟连接。
@pytest.mark.parametrize("difference", ["server", "timeout", "credentials", "pid", "closed"])
def test_incompatible_mcp_cannot_be_borrowed(tmp_path, difference):
    parent = registry(tmp_path, mcp_servers={"echo": {"command": "echo", "timeout": 10}})
    child = registry(tmp_path, mcp_servers={"echo": {"command": "echo", "timeout": 10}})
    share = getattr(child, "share_same_owner_mcp_state_from", None)
    assert callable(share), "same-owner MCP borrowing API is missing"
    if difference in {"server", "timeout"}:
        child._construction_params = replace(child._construction_params, mcp_servers={"echo": {"command": "other" if difference == "server" else "echo", "timeout": 20}})
    elif difference == "credentials":
        child._construction_params = replace(child._construction_params, host_credential_env_names=frozenset({"PRIVATE_ENV_NAME"}))
    elif difference == "pid":
        parent._mcp_owner_pid = -1
    else:
        parent.close_mcp_clients()
    assert share(parent) is False
    assert child._mcp_clients is not parent._mcp_clients
    assert child._mcp_connected_state == [False]


# LLM: Cross-owner and missing-owner attempts fail before sharing any mutable client or lock state.
# 函数用途: 验证跨用户共享失败关闭。
@pytest.mark.parametrize("owner", ["test/user/b", ""])
def test_cross_owner_sharing_is_forbidden(tmp_path, owner):
    parent, child = registry(tmp_path), registry(tmp_path)
    child.operation_owner_id = owner
    share = getattr(child, "share_same_owner_mcp_state_from", None)
    assert callable(share), "same-owner MCP borrowing API is missing"
    with pytest.raises(PermissionError, match="Cross-owner"):
        share(parent)


# LLM: Pool ownership remains real; only agent construction is replaced so eviction/re-pin state can be observed without external processes.
# 函数用途: 验证淘汰后重新租用会重置连接状态供下一轮重连。
def test_pool_eviction_then_pin_reopens_registry(tmp_path):
    pool = OwnerScopedAgentPool(AgentConfig(my_agent_home=str(tmp_path / "home")), tmp_path, max_agents=1)
    pool._builder = lambda *_args: SimpleNamespace(tools=registry(tmp_path))
    first = pool.get(OwnerIdentity.provider_user("test", "a"))
    client = Client()
    first.tools._mcp_clients.append(client)
    first.tools._mcp_connected_state[0] = True
    pool.get(OwnerIdentity.provider_user("test", "b"))
    assert first.tools._mcp_closed.is_set()
    with pool.pin(first):
        assert not first.tools._mcp_closed.is_set()
        assert first.tools._mcp_connected_state == [False]
    assert first.tools._mcp_closed.is_set()


# LLM: Public owner_limit is asserted before use; an active user can consume another conversation without consuming a distinct-owner slot.
# 函数用途: 验证新用户受背压而已活跃用户仍可进入。
def test_owner_capacity_is_distinct_user_backpressure():
    assert "owner_limit" in inspect.signature(requests.GatewayAdmission).parameters
    admission = requests.GatewayAdmission(owner_limit=1)
    try:
        assert admission.try_acquire_report("a", 3, 10) == ""
        assert admission.try_acquire_report("b", 3, 10) == "owner_capacity"
        assert admission.try_acquire_report("a", 3, 10) == ""
    finally:
        admission.release("a")
        admission.release("a")


# LLM: Construction must defer MCP on both original and transported paths, preserving the original workspace roots.
# 函数用途: 验证worker构造只传延迟标志，不提前建立连接。
@pytest.mark.parametrize("transported", [False, True])
def test_worker_construction_defers_mcp(tmp_path, transported):
    options = []
    parent = SimpleNamespace(workspace_roots=[tmp_path])
    transport = SimpleNamespace(scope=SimpleNamespace(execution=SimpleNamespace(agent=parent))) if transported else None
    params = SimpleNamespace(root=tmp_path, request_transport=transport)

    # LLM: Observe real constructor arguments without creating a test-only product path.
    # 函数用途: 捕获构造入口交给Agent的延迟选项。
    def construct(config, root, **kwargs):
        options.append(kwargs)
        return object()

    runner._construct_transported_worker(construct, params, AgentConfig())
    assert options[0].get("defer_mcp_connect") is True
    if transported:
        assert options[0]["workspace_roots"] is parent.workspace_roots


# LLM: The same thread-local descriptors are tested with real separate SimpleAgent facades and synchronized overlap, not copied tool instances.
# 函数用途: 验证两个owner及同owner父子在并发线程中各自保有回合身份。
def test_dual_owner_and_child_thread_local_isolation(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.runtime_context import (
        current_subagent_run_id,
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    config = AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo")
    agents = [SimpleAgent(replace(config, my_agent_owner_provider="test", my_agent_owner_kind="user", my_agent_owner_id=owner), tmp_path, defer_mcp_connect=True) for owner in ("a", "a", "b")]
    barrier = threading.Barrier(3)

    # LLM: Assert before and after overlap; always restore runner context, including when a peer fails.
    # 函数用途: 同步写入与读取当前线程绑定的三个回合字段和子代理身份。
    def run(pair):
        index, agent = pair
        marker = f"run-{index}"
        agent._current_user_prompt = marker
        agent._current_run_params = marker
        agent._current_run_task_workspace = marker
        previous = set_current_subagent_context(agent, run_id=marker)
        try:
            barrier.wait(timeout=10)
            assert (agent._current_user_prompt, agent._current_run_params, agent._current_run_task_workspace, current_subagent_run_id(agent)) == (marker,) * 4
        finally:
            restore_current_subagent_context(agent, previous)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(run, enumerate(agents)))
        assert agents[0].tools is not agents[1].tools
        assert agents[0].home_paths.owner_home_dir == agents[1].home_paths.owner_home_dir
        assert agents[0].home_paths.owner_home_dir != agents[2].home_paths.owner_home_dir
    finally:
        for agent in agents:
            agent.tools.close_mcp_clients()


# LLM: Exercise the real pool with an external lifecycle double lacking registry's own mutex, so registry serialization cannot mask a pool race.
# 函数用途: 验证旧淘汰关闭不能在新pin重开之后再次关闭连接。
def test_eviction_close_cannot_overwrite_concurrent_pin(tmp_path):
    pool = OwnerScopedAgentPool(AgentConfig(my_agent_home=str(tmp_path / "home")), tmp_path, max_agents=1)
    pool._builder = lambda *_args: SimpleNamespace(tools=registry(tmp_path))
    agent = pool.get(OwnerIdentity.provider_user("test", "a"))
    closing, allow_close, entered, evicted = (threading.Event() for _ in range(4))
    closed = threading.Event()

    # LLM: Delay actual close so the old get/pin race is deterministic; never hold the pool lock from the test.
    # 函数用途: 在真正关闭连接前留出并发租用窗口。
    def stop():
        closing.set()
        assert allow_close.wait(10)
        closed.set()

    agent.tools = SimpleNamespace(_mcp_closed=closed, close_mcp_clients=stop, reopen_mcp_clients=closed.clear)

    # LLM: Sample after eviction has completed, while the new lease is still active.
    # 函数用途: 核实租用期间MCP已重开而非被旧关闭覆盖。
    def lease():
        with pool.pin(agent):
            entered.set()
            assert evicted.wait(10)
            return agent.tools._mcp_closed.is_set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        removing = executor.submit(pool.get, OwnerIdentity.provider_user("test", "b"))
        assert closing.wait(10)
        pinning = executor.submit(lease)
        entered.wait(0.1)
        allow_close.set()
        removing.result(timeout=10)
        evicted.set()
        assert pinning.result(timeout=10) is False


# LLM: Gauges describe live objects, not persistent tasks; acquiring never waits for capacity while holding another pin.
# 函数用途: 验证租用及退役计数可观察且最后释放后归零。
def test_pool_acquire_and_gauges(tmp_path):
    pool = OwnerScopedAgentPool(AgentConfig(my_agent_home=str(tmp_path / "home")), tmp_path, max_agents=1)
    pool._builder = lambda *_args: SimpleNamespace(tools=registry(tmp_path))
    assert callable(getattr(pool, "acquire", None)), "coordinated pool acquisition is missing"
    with pool.acquire(OwnerIdentity.provider_user("test", "a")) as agent:
        pool.get(OwnerIdentity.provider_user("test", "b"))
        assert pool.hard_count == 1 and pool.soft_count == 0
        assert pool.retired_count == 1 and pool.pinned_count == 1
        assert not agent.tools._mcp_closed.is_set()
    assert pool.retired_count == 0 and pool.pinned_count == 0


# LLM: Observe the actual owner builder/ToolRegistry connection seam; no real MCP server is spawned.
# 函数用途: 验证冷owner激活不连接MCP，首个run准备仍会连接。
def test_cold_owner_only_connects_at_run_boundary(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import registry as module

    connected = []
    monkeypatch.setattr(module, "_connect_mcp_servers", lambda *_args: connected.append(True) or [])
    pool = OwnerScopedAgentPool(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo"), tmp_path)
    agent = pool.get(OwnerIdentity.provider_user("test", "a"))
    try:
        assert connected == []
        agent.tools.prepare_for_run()
        assert connected == [True]
    finally:
        agent.tools.close_mcp_clients()


# LLM: An existing in-memory wait signal may suppress an unchanged write, but writes must still retain canonical locked reread/CAS.
# 函数用途: 验证节流窗口内不重复读取请求文件。
def test_wait_metadata_uses_in_memory_throttle(tmp_path, monkeypatch):
    now = 1000.0
    payload = {"id": "wait", "created_at": 1, "admission_wait_at": now, "admission_wait_reason": "owner_capacity"}
    request = SimpleNamespace(path=tmp_path / "wait.json", payload=payload)
    writes = []
    monkeypatch.setattr(requests.time, "time", lambda: now + 1)
    monkeypatch.setattr(requests, "_write_admission_wait_field", lambda *args, **kwargs: writes.append(args))
    requests._record_admission_wait(request, reason="owner_capacity")
    assert writes == []


# LLM: Follow the real dispatcher and atomic pending-file writer; a full owner lane must leave the next user unclaimed in inbox.
# 函数用途: 验证配置的owner容量真正接入派发并保留等待元数据。
def test_configured_owner_capacity_keeps_request_in_inbox(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    limits = requests.AdmissionLimits.from_config(SimpleNamespace(owner_agent_pool_max_agents=1))
    admission = requests.GatewayAdmission()
    monkeypatch.setattr(requests, "admission", admission)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    requests.ensure_gateway_folders(paths)
    path = paths.inbox / "queued.json"
    payload = {"id": "queued", "user_id": "b", "kind": "ask", "created_at": 1}
    path.write_text(json.dumps(payload))
    claimed = []
    admission.try_acquire_report("a", 8, 500)
    try:
        request = SimpleNamespace(path=path, payload=payload)
        assert requests._admit_and_submit(paths, request, (limits, lambda *args: claimed.append(args), None)) == "blocked"
        assert not claimed and path.exists()
        assert json.loads(path.read_text())["admission_wait_reason"] == "owner_capacity"
    finally:
        admission.release("a")


# LLM: Durable admission consumes canonical reservations, not process-local counters; this gate double isolates the decision seam.
# 函数用途: 验证多进程派发拦截新owner，已在途owner仍进入原持久准入。
@pytest.mark.parametrize("active", [False, True])
def test_durable_admission_uses_canonical_owner_capacity(tmp_path, monkeypatch, active):
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    owner = OwnerIdentity.provider_user("test", "a" if active else "b")
    first = OwnerIdentity.provider_user("test", "a")
    seen, waits = [], []

    # LLM: Original gate retains reservation/stamping authority; observe only whether it was called.
    # 函数用途: 返回W路由，不伪造本地业务执行。
    def admit(*args, **kwargs):
        seen.append(args)
        if args[2].global_inflight <= 1:
            return {"status": "blocked", "reason": "global_inflight"}
        return {"status": "admitted", "request": {"route": {"worker": "W1"}}}

    gate = SimpleNamespace(paths=paths, admit=admit, occupied_snapshot=lambda: {
        "total": 1, "per_owner": {owner_digest(first): 1}, "per_session": {}, "unreadable": 0,
    })
    monkeypatch.setattr(requests, "_durable_request_owner", lambda *_args: owner)
    monkeypatch.setattr(requests, "_record_admission_wait", lambda *args, **kwargs: waits.append(kwargs["reason"]))
    request = SimpleNamespace(path=paths.inbox / "queued.json", payload={"id": "queued", "user_id": "same-user"})
    limits = requests.AdmissionLimits.from_config(SimpleNamespace(owner_agent_pool_max_agents=1))
    result = requests._admit_and_submit_durable(paths, request, (limits, None, gate))
    assert result == ("dispatched" if active else "blocked")
    assert len(seen) == 1
    assert waits == ([] if active else ["owner_capacity"])


# LLM: Real gate rejects generation absence before reservation; full owner lanes must not replace this with capacity waiting.
# 函数用途: 用真实持久准入门证明容量等待不能掩盖原代次拒绝。
def test_durable_capacity_preserves_real_gate_rejection(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        CoordinatorAdmissionScope,
        PersistentAdmission,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    first, owner = OwnerIdentity.provider_user("test", "a"), OwnerIdentity.provider_user("test", "b")
    gate = PersistentAdmission(paths, SimpleNamespace(), CoordinatorAdmissionScope(1, ""))
    monkeypatch.setattr(gate, "occupied_snapshot", lambda: {"total": 1, "per_owner": {owner_digest(first): 1}, "unreadable": 0})
    request = SimpleNamespace(path=paths.inbox / "queued.json", payload={"id": "queued"})
    limits = requests.AdmissionLimits.from_config(SimpleNamespace(owner_agent_pool_max_agents=1))
    original = gate.admit(request.path, (owner, None), limits, snapshot=request.payload)
    assert original["reason"] == "coordinator_generation_unavailable"
    assert requests._durable_admission_decision(paths, request, gate, (owner, None), limits) == original
