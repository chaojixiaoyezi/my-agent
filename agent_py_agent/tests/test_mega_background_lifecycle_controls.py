"""R2：真实规范任务、列表投影、guidance 和 wake 的跨页生命周期回归。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.background_team_lifecycle import (
    reconcile_background_identity,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.tests.test_business_v12_f3_agents import (
    _service,
)
from agent_py_agent.tests.test_business_v12_f3_agents import (
    business_host as business_host,
)


def test_thread_creation_metadata_hook_runs_once_per_new_row(business_host):
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService

    class RecordingService(BackgroundAgentService):
        def _thread_creation_metadata(self, *, actor, metadata: dict) -> dict:
            assert actor is original_actor
            calls.append(dict(metadata))
            return {**metadata, "installation_provenance": "original-installation"}

    host, original_actor = business_host
    service, calls = RecordingService(host), []
    request = {"space_id": "space-A", "name": "new worker"}
    first = service.create(request, actor=original_actor, idempotency_key="hook-worker")
    assert len(calls) == 1
    assert service.create(request, actor=original_actor, idempotency_key="hook-worker") == first
    controller = service.ensure_controller(actor=original_actor)
    assert controller["created"] is True and len(calls) == 2
    for identifier in (first["agent"]["agent_id"], controller["thread_id"]):
        assert host.conversation_store.threads.require(identifier).metadata["installation_provenance"] == "original-installation"
    before = host.conversation_store.threads.update_atomic(controller["thread_id"],
        lambda thread: replace(thread, status="archived"))
    assert service.ensure_controller(actor=original_actor) == {**controller, "created": False}
    assert host.conversation_store.threads.require(controller["thread_id"]) == before
    assert len(calls) == 2


def test_thread_creation_hook_does_not_retag_native_or_existing_installation(business_host):
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService

    service, host, actor = _service(business_host)
    request = {"space_id": "space-A", "name": "native"}
    created = service.create(request, actor=actor, idempotency_key="native-hook")
    controller = service.ensure_controller(actor=actor)
    worker_thread = host.conversation_store.threads.require(created["agent"]["agent_id"])
    assert set(worker_thread.metadata) == {"background_agent", "background_purpose", "background_creation"}
    assert host.conversation_store.threads.require(controller["thread_id"]).metadata == {"business_controller": True}

    class ExistingService(BackgroundAgentService):
        def _thread_creation_metadata(self, *, actor, metadata: dict) -> dict:
            pytest.fail("existing canonical row must not be retagged")

    service = ExistingService(host)
    assert service.create(request, actor=actor, idempotency_key="native-hook") == created
    assert service.ensure_controller(actor=actor)["created"] is False
    before = host.conversation_store.threads.update_atomic(controller["thread_id"],
        lambda thread: replace(thread, status="archived", metadata={**thread.metadata, "installation_provenance": "other-installation"}))
    assert service.ensure_controller(actor=actor)["created"] is False
    assert host.conversation_store.threads.require(controller["thread_id"]) == before
    with pytest.raises(PermissionError, match="OWNER_MISMATCH"):
        service.ensure_controller(actor=replace(actor, owner_id="foreign"))


def test_native_background_dispatch_preserves_typed_unknown_outcome(business_host, monkeypatch):
    from agent_py_agent.agent.conversation import background_agents
    from agent_py_agent.agent.conversation.session_task_dispatch import SessionTaskDispatchError
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    service, host, actor = _service(business_host)
    controller = service.ensure_controller(actor=actor)
    outcome = ToolHandlerOutcome("create_session_task", False, "unknown", error_code="TOOL_EXECUTION_FAILED",
        effect_outcome="unknown", result_envelope={"session_task_dispatch": {
            "sender_thread_id": actor.source_thread_id, "target_thread_id": controller["thread_id"], "dedupe_key": "original"}})
    monkeypatch.setattr(background_agents, "dispatch_session_task", lambda *args, **kwargs: outcome)
    with pytest.raises(SessionTaskDispatchError) as raised:
        service.submit({"space_id": "space-A", "instruction": "unknown"}, actor=actor, idempotency_key="typed-outcome")
    assert raised.value.outcome is outcome
    assert raised.value.error_code == outcome.error_code and raised.value.effect_outcome == "unknown"
    assert host.conversation_store.session_tasks.list_report(limit=0) == ([], [])


@pytest.mark.parametrize("published_receipt_missing", [False, True])
def test_cancel_original_task_after_create_ack_loss_without_published_input(business_host, monkeypatch, published_receipt_missing):
    from agent_py_agent.agent.conversation.session_task_dispatch import SessionTaskDispatchError

    service, host, actor = _service(business_host)
    store = host.conversation_store
    create, created = store.session_tasks.create, []

    def lose_ack_once(*args, **kwargs):
        task = create(*args, **kwargs)
        if not created:
            created.append(task.task_id)
            raise OSError("original create committed; ACK lost")
        return task

    monkeypatch.setattr(store.session_tasks, "create", lose_ack_once)
    with pytest.raises(SessionTaskDispatchError) as error:
        service.submit({"space_id": "space-A", "instruction": "unpublished input"},
            actor=actor, idempotency_key="create-ack-loss")
    assert error.value.effect_outcome == "unknown"
    assert len(created) == 1
    task = store.session_tasks.require(created[0])
    assert task.metadata["business_context"]["task_id"] == task.task_id
    assert not task.body_guidance_id and not task.task_run_id and not task.conversation_request_id
    assert store.guidance.receipt(task.body_dedupe_key) is None and not store.wakes.pending(limit=0)
    if published_receipt_missing:
        # 发布身份仍在而原回执丢失不能等同于从未发表；保留原任务责任，不补造输入。
        edit_task(store, task.task_id, lambda current: replace(current, body_guidance_id="original-published-input"))
    ready_index(store)
    result = service.cancel_task(task.task_id, actor=actor, include_descendants=True)
    assert result == {"operation_id": task.task_id, "state": "pending" if published_receipt_missing else "completed",
                      "outstanding_refs": [task.task_id] if published_receipt_missing else []}
    assert store.session_tasks.require(task.task_id).status == "cancelled"
    assert service.cancel_task(task.task_id, actor=actor, include_descendants=True) == result
    assert store.session_tasks.list_report(limit=0)[0] == [store.session_tasks.require(task.task_id)]
    assert store.guidance.receipt(task.body_dedupe_key) is None and not store.wakes.pending(limit=0)


def native_cancellation_case(tmp_path, *, controller=False, request_timeout=0):
    """真实业务入口、调度器、claim、SQLite 和无害文件工具；仅外部模型受控。"""
    from threading import Event

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
        business_spaces_enabled=True, enable_tools=True, request_timeout=request_timeout, prompt_files=[]), tmp_path / "workspace")
    store, owner = agent.conversation_store, str(agent.home_paths.owner_id)
    # 与本地真实入口一致保留原通道绑定，后台工具才能核验派发来源。
    source = store.threads.get_or_create({
        "owner_id": owner, "owner_home": str(agent.home_paths.owner_home_dir),
        "canonical_user_id": "local-agent", "channel": "local",
        "channel_conversation_id": "local", "channel_user_id": "local-agent",
    })
    actor = HostChannelActor(owner, source.thread_id, "native-cancel", "local", "local", "local-agent")
    context = bind_business_context(agent, authorize=lambda action, facts: True, actor=actor)
    services = BusinessServices(agent)
    space = services.spaces.create({"name": "停止核验空间"}, actor=services.actor(context), idempotency_key="native-cancel-space")
    sid = space["space_id"]

    def call(action, params, **options):
        return services.background.execute(action, params, context=context, actor=actor, **options)

    view = None if controller else call("background_agents.create", {"space_id": sid, "name": "原执行者"},
        idempotency_key="native-cancel-worker")["agent"]
    action = "tasks.submit" if controller else "tasks.assign"
    params = {"space_id": sid, "instruction": "先读取原资料，再等待取消观察。"}
    if view:
        params["agent_id"] = view["agent_id"]
    task_id = call(action, params, idempotency_key="native-cancel-task")["task_id"]

    class Backend:
        name = "native-cancel-controlled-model"

        def __init__(self):
            self.calls = 0
            self.waiting, self.release, self.returned = Event(), Event(), Event()
            self.waiting_worker = None

        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name, endpoint="local://test", model="", stream=False,
                native_supported=True, evidence="contract_test", observed_at=_utc_now_iso())

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
                    "id": "before-cancel", "name": "write_file", "input": {
                        "path": "before-cancel-result.txt", "content": "harmless original result"}}])
            if self.calls > 2:
                return ModelResponse(text="unexpected continuation after original model wait", backend=self.name)
            from threading import current_thread
            self.waiting_worker = current_thread()
            self.waiting.set()
            assert self.release.wait(15), "test did not release original model"
            self.returned.set()
            return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
                "id": "after-cancel", "name": "write_file", "input": {
                    "path": "after-cancel-result.txt", "content": "must never be written"}}])

    agent.backend = backend = Backend()
    scheduler = BackgroundMainAgentScheduler({"runtime": BackgroundMainAgentRuntime(agent=agent, store=store), "store": store})
    return agent, backend, scheduler, call, task_id, view


def native_task_work_file(agent, task_id, name):
    """按原任务出生时间与已配置模板核对工作文件，不假定写在 owner 根。"""
    from datetime import datetime, timezone

    from agent_py_agent.agent.user_space.home_layout import task_workspace_path

    task = agent.conversation_store.session_tasks.require(task_id)
    root = task_workspace_path(agent.home_paths.owner_home_dir,
        agent.config.workspace_task_path_template,
        date=datetime.fromtimestamp(task.created_at, timezone.utc).date().isoformat(),
        task_name=task.task_id)
    return root / "work" / name


@pytest.mark.parametrize("controller,execution_only", [(False, False), (True, False), (False, True), (True, True)])
def test_native_cancel_stops_original_authority_and_converges_after_real_exit(tmp_path, controller, execution_only):
    from concurrent.futures import ThreadPoolExecutor

    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path, controller=controller)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(scheduler.tick)
    try:
        assert backend.waiting.wait(15), "native Scheduler did not reach second model call"
        original_result = native_task_work_file(agent, task_id, "before-cancel-result.txt")
        forbidden_result = native_task_work_file(agent, task_id, "after-cancel-result.txt")
        assert original_result.read_text() == "harmless original result"
        assert not forbidden_result.exists()
        task = store.session_tasks.require(task_id)
        claim = store.claims.load(task.target_thread_id)
        authority = claim["runtime_authority"]
        assert task.task_run_id and task.conversation_request_id
        assert claim["status"] == "running" and not future.done()
        unchanged = store.session_tasks.require(task_id).to_dict()
        original_run = dict(repo.get_agent_run(authority["agent_run_id"]))
        original_attempt = dict(repo.get_attempt(authority["attempt_id"]))
        with pytest.raises(ValueError):
            call("executions.cancel", {"run_id": authority["run_id"], "attempt_id": "not-original-attempt",
                                       "include_descendants": True})
        assert store.session_tasks.require(task_id).to_dict() == unchanged
        assert dict(repo.get_agent_run(authority["agent_run_id"])) == original_run
        assert dict(repo.get_attempt(authority["attempt_id"])) == original_attempt
        assert store.claims.load(task.target_thread_id)["runtime_authority"] == authority
        assert not future.done() and not backend.returned.is_set()
        if execution_only:
            action = "executions.cancel"
            params = {"run_id": authority["run_id"], "attempt_id": authority["attempt_id"], "include_descendants": True}
        else:
            action, params = "tasks.cancel", {"task_id": task_id, "include_descendants": True}
        before = store.session_tasks.require(task_id)
        pending = call(action, params)
        assert pending["state"] == "pending"
        assert repo.get_agent_run(authority["agent_run_id"])["status"] == "cancelled"
        held = store.session_tasks.require(task_id).metadata["cancellation_control"]
        assert held["outstanding_refs"] and held["execution_ref"]["observed_attempt_id"] == authority["attempt_id"]
        assert store.session_tasks.require(task_id).task_run_id == before.task_run_id
        # 原worker仍在真实模型内，逻辑cancelled不能伪报物理停止。
        assert not future.done() and not backend.returned.is_set()
        if view and not execution_only:
            ready_index(store)
            retiring = call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
                expected_revision=view["revision"], idempotency_key="native-retire")
            assert retiring["agent"]["identity_state"] == "retiring" and retiring["outstanding_refs"]
        backend.release.set()
        future.result(timeout=15)
        assert backend.returned.is_set() and backend.calls == 2
        assert repo.get_agent_run_exec_lock(authority["agent_run_id"]) is None
        if view and not execution_only:
            retired_thread = eventually_retired(agent, view["agent_id"])
            assert retired_thread.metadata["background_agent"]["identity_state"] == "retired"
        done = call(action, params)
        assert done["state"] == "completed", done
        assert not store.session_tasks.require(task_id).metadata["cancellation_control"]["outstanding_refs"]
        assert call(action, params) == done
        assert original_result.read_text() == "harmless original result" and not forbidden_result.exists()
        if execution_only:
            cancelled_task = call("tasks.cancel", {"task_id": task_id, "include_descendants": True})
            assert cancelled_task["state"] == "completed"
            assert store.tasks.load(authority["task_id"]).status == "cancelled"
        if view and not execution_only:
            restored = call("background_agents.restore", {"agent_id": view["agent_id"]},
                expected_revision=retired_thread.metadata["background_agent"]["identity_revision"], idempotency_key="native-restore")
            assert restored["agent"]["identity_state"] == "available"
    finally:
        backend.release.set()
        pool.shutdown(wait=True)


def test_original_cancellation_converges_after_successor_reuses_claim_slot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.backends import ModelResponse

    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick)
        try:
            assert backend.waiting.wait(15)
            original = store.session_tasks.require(task_id)
            claim = store.claims.load(original.target_thread_id)
            assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True})["state"] == "pending"
        finally:
            backend.release.set()
            future.result(timeout=15)

    class SuccessorBackend(type(backend)):
        def generate(self, prompt, on_chunk=None, **kwargs):
            return ModelResponse(text="successor preserved result", backend=self.name)

    agent.backend = SuccessorBackend()
    successor_id = call("tasks.assign", {"space_id": original.metadata["business_context"]["space_id"],
        "agent_id": view["agent_id"], "instruction": "independent successor"},
        idempotency_key="successor-after-cancel")["task_id"]
    scheduler.tick()
    successor = store.session_tasks.require(successor_id)
    assert successor.status == "done"
    replacement = store.claims.load(original.target_thread_id)
    assert replacement["claim_id"] != claim["claim_id"]
    assert replacement["previous_claim"]["claim_id"] == claim["claim_id"]
    assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True})["state"] == "completed"
    assert store.session_tasks.require(successor_id) == successor
    assert store.claims.load(original.target_thread_id) == replacement


def test_execution_cancel_expands_scope_and_waits_for_original_child_registration(tmp_path):
    # 子worker用真实命名注册与原创建/SQLite链，控制其退出时序；不替换停止实现或伪造清理回执。
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Thread

    from agent_py_agent.agent.concurrency.interrupt import register_interruptible
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent, backend, scheduler, call, task_id, _view = native_cancellation_case(tmp_path)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    ready, release = Event(), Event()
    child_worker = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick)
        try:
            assert backend.waiting.wait(15)
            task = store.session_tasks.require(task_id)
            binding = store.claims.load(task.target_thread_id)["runtime_authority"]
            ref = repo.parent_execution_ref(owner_id=str(agent.home_paths.owner_id),
                run_id=binding["run_id"], attempt_id=binding["attempt_id"])
            child = agent.subagents.create_run(params=CreateRunParams(goal="原子执行退出观察", thought="合同回归", plan=[],
                owner=str(agent.home_paths.owner_id), host_parent_execution=ref,
                attributes={"conversation_task_id": binding["task_id"], "conversation_thread_id": task.target_thread_id}))
            row = repo.agent_run_for_run_id(child.id)
            attempt = repo.create_attempt(row["agent_run_id"], reuse_pending=True)
            child.runner_active_attempt_id = attempt["attempt_id"]
            agent.subagents.save(child)

            def worker():
                with register_interruptible(f'subagent-runner-attempt:{child.id}:{attempt["attempt_id"]}'):
                    ready.set()
                    assert release.wait(15)

            child_worker = Thread(target=worker)
            child_worker.start()
            assert ready.wait(15)
            params = {"run_id": ref.run_id, "attempt_id": ref.observed_attempt_id, "include_descendants": False}
            assert call("executions.cancel", params)["state"] == "pending"
            assert repo.get_attempt(attempt["attempt_id"])["status"] == "running"
            backend.release.set()
            future.result(timeout=15)
            assert call("executions.cancel", params)["state"] == "completed"
            assert child_worker.is_alive() and repo.get_attempt(attempt["attempt_id"])["status"] == "running"
            expanded = {**params, "include_descendants": True}
            assert call("executions.cancel", expanded)["state"] == "pending"
            assert child_worker.is_alive() and repo.get_attempt(attempt["attempt_id"])["status"] == "cancelled"
            assert call("executions.cancel", expanded)["state"] == "pending"
            before = store.session_tasks.require(task_id)
            with pytest.raises(ValueError, match="SESSION_TASK_RESOURCE_CONFLICT"):
                store.session_tasks.record_cancellation_control(task_id, outstanding_refs=[], execution_ref=ref,
                    confirmed_refs=(ref.run_id, task_id, task.conversation_request_id), expected_resource_refs=[])
            assert store.session_tasks.require(task_id) == before
            release.set()
            child_worker.join(timeout=15)
            assert not child_worker.is_alive()
            assert call("executions.cancel", expanded)["state"] == "completed"
        finally:
            backend.release.set()
            release.set()
            if child_worker is not None:
                child_worker.join(timeout=15)
            future.result(timeout=15)


def test_retirement_observes_original_pty_exit_without_second_stop(tmp_path):
    # 标准库真实PTY进程注入唯一原注册表；不替换停止/清理，也不外推terminal_session工具沙箱验收。
    import os
    import subprocess
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.tooling.process_scope import ProcessExecutionScope
    from agent_py_agent.agent.tooling.pty_process_lifecycle import PtyOwnedProcess
    from agent_py_agent.agent.tooling.pty_sessions import (
        PtyAccessScope,
        PtySession,
        pty_session_registry,
    )

    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(scheduler.tick)
    session, process, master = None, None, -1
    try:
        assert backend.waiting.wait(15)
        task = store.session_tasks.require(task_id)
        authority = store.claims.load(task.target_thread_id)["runtime_authority"]
        master, slave = os.openpty()
        try:
            process = PtyOwnedProcess(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True))
        finally:
            os.close(slave)
        session = PtySession("pty-native-cancel", "isolated test wait", process, master, time.time(), time.time(),
            PtyAccessScope(str(agent.home_paths.owner_home_dir), task.target_thread_id, (), (), ()),
            execution_scope=ProcessExecutionScope(str(agent.home_paths.owner_home_dir), task.target_thread_id,
                authority["task_id"], authority["run_id"], authority["attempt_id"]))
        with pty_session_registry._lock:
            pty_session_registry._sessions[session.session_id] = session
        with session.close_lock:
            assert process.poll() is None
            assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True})["state"] == "pending"
            assert any(ref["id"] == session.session_id for ref in
                       store.session_tasks.require(task_id).metadata["cancellation_control"]["resource_refs"])
            ready_index(store)
            retiring = call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
                expected_revision=view["revision"], idempotency_key="retire-pty")
            assert retiring["agent"]["identity_state"] == "retiring"
            backend.release.set()
            future.result(timeout=15)
            assert reconcile_background_identity(agent, view["agent_id"]).metadata["background_agent"]["identity_state"] == "retiring"
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail(f"original PTY exit unconfirmed: birth={process.birth_token!r}, receipt={session.termination!r}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (session.termination is None or not session.closed):
            time.sleep(.01)
        assert session.closed and session.termination.confirmed, session.termination
        from agent_py_agent.agent.gateway_parts.session_task_stop import _business_resources_exited
        assert _business_resources_exited(agent, agent.subagents.runtime_db, store.session_tasks.require(task_id)), (
            store.session_tasks.require(task_id).metadata["cancellation_control"], session.termination)
        retired = eventually_retired(agent, view["agent_id"])
        assert retired.metadata["background_agent"]["identity_state"] == "retired"
        assert session.closed and session.termination.confirmed
        assert not store.session_tasks.require(task_id).metadata["cancellation_control"]["outstanding_refs"]
    finally:
        backend.release.set()
        pool.shutdown(wait=True)
        if session is not None:
            pty_session_registry.close(session.session_id)
            if session.process.poll() is None:
                # 仅测试teardown的自有单进程，不把此旁路动作算进上面的业务清理验收。
                session.process.raw.terminate()
            session.process.raw.wait(timeout=15)
        elif process is not None:
            process.raw.terminate()
            process.raw.wait(timeout=15)
            os.close(master)


def test_native_wall_guard_keeps_unobserved_physical_worker_exit_pending(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.contracts.model_call_ledger import model_call_drain_snapshot
    from agent_py_agent.agent.settings import AgentConfig

    agent, backend, scheduler, call, task_id, _view = native_cancellation_case(tmp_path, request_timeout=AgentConfig().request_timeout)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick)
        try:
            assert backend.waiting.wait(15)
            assert backend.waiting_worker.is_alive()
            pending = call("tasks.cancel", {"task_id": task_id, "include_descendants": True})
            assert pending["state"] == "pending"
            future.result(timeout=15)
            assert backend.waiting_worker.is_alive() and not backend.returned.is_set()
            target = agent.conversation_store.session_tasks.require(task_id).target_thread_id
            authority = agent.conversation_store.claims.load(target)["runtime_authority"]
            assert any(tokens and record.run_id == authority["run_id"] and record.metadata.get("attempt_id") == authority["attempt_id"]
                       for _ledger, record, tokens in model_call_drain_snapshot())
            # claim/core已经finally，原模型worker仍活着；逻辑终态不能抹掉准确调用的保留权。
            again = call("tasks.cancel", {"task_id": task_id, "include_descendants": True})
            assert again["state"] == "pending" and again["outstanding_refs"]
            assert agent.conversation_store.session_tasks.require(task_id).metadata["cancellation_control"]["outstanding_refs"]
        finally:
            backend.release.set()
            if backend.waiting_worker is not None:
                backend.waiting_worker.join(timeout=15)
                assert not backend.waiting_worker.is_alive()
            future.result(timeout=15)
    assert not (native_task_work_file(agent, task_id, "after-cancel-result.txt")).exists()
    assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True})["state"] == "completed"


def test_native_cancellation_leaves_unrelated_active_execution_untouched(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.backends import ModelResponse

    agent, original_backend, scheduler, call, task_id, _view = native_cancellation_case(tmp_path)
    store, repo = agent.conversation_store, agent.subagents.runtime_db

    class UnrelatedBackend(type(original_backend)):
        def __init__(self):
            super().__init__()
            self.finished = Event()

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
                    "id": "unrelated-active-write", "name": "write_file", "input": {
                        "path": "unrelated-active-result.txt", "content": "unrelated successful tool"}}])
            self.waiting.set()
            assert self.release.wait(15), "unrelated execution not released"
            self.finished.set()
            return ModelResponse(text="unrelated original result", backend=self.name)

    unrelated_backend = UnrelatedBackend()
    with ThreadPoolExecutor(max_workers=2) as pool:
        original_future = pool.submit(scheduler.tick)
        try:
            assert original_backend.waiting.wait(15)
            task = store.session_tasks.require(task_id)
            sid = task.metadata["business_context"]["space_id"]
            view = call("background_agents.create", {"space_id": sid, "name": "unrelated active worker"},
                        idempotency_key="unrelated-active-worker")["agent"]
            unrelated_id = call("tasks.assign", {"space_id": sid, "agent_id": view["agent_id"],
                "instruction": "produce unrelated original result"}, idempotency_key="unrelated-active-task")["task_id"]
            agent.backend = unrelated_backend
            unrelated_future = pool.submit(scheduler.tick)
            assert unrelated_backend.waiting.wait(15)
            assert (native_task_work_file(agent, unrelated_id, "unrelated-active-result.txt")).read_text() == "unrelated successful tool"
            unrelated_task = store.session_tasks.require(unrelated_id)
            unrelated_claim = store.claims.load(unrelated_task.target_thread_id)
            unrelated_authority = unrelated_claim["runtime_authority"]
            assert unrelated_task.status == "accepted"
            snapshot = unrelated_task.to_dict()
            unrelated_run = dict(repo.get_agent_run(unrelated_authority["agent_run_id"]))
            unrelated_attempt = dict(repo.get_attempt(unrelated_authority["attempt_id"]))
            pending = call("tasks.cancel", {"task_id": task_id, "include_descendants": True})
            assert pending["state"] == "pending"
            assert store.session_tasks.require(unrelated_id).to_dict() == snapshot
            assert dict(repo.get_agent_run(unrelated_authority["agent_run_id"])) == unrelated_run
            assert dict(repo.get_attempt(unrelated_authority["attempt_id"])) == unrelated_attempt
            assert store.claims.load(unrelated_task.target_thread_id)["runtime_authority"] == unrelated_authority
            assert not unrelated_future.done() and not unrelated_backend.finished.is_set()
            original_backend.release.set()
            original_future.result(timeout=15)
            assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True})["state"] == "completed"
            assert not unrelated_future.done() and not unrelated_backend.finished.is_set()
            assert store.session_tasks.require(unrelated_id).to_dict() == snapshot
            unrelated_backend.release.set()
            unrelated_future.result(timeout=15)
            assert unrelated_backend.finished.is_set()
            assert store.session_tasks.require(unrelated_id).status == "done"
            assert repo.get_agent_run(unrelated_authority["agent_run_id"])["status"] == "done"
            assert not (native_task_work_file(agent, task_id, "after-cancel-result.txt")).exists()
        finally:
            original_backend.release.set()
            unrelated_backend.release.set()


def worker(business_host):
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="worker")["agent"]
    return service, host, actor, view


def terminal_history(store, sender, target, count):
    for _ in range(count):
        task = store.session_tasks.create(SessionTaskDraft(sender, target, "retained history"))
        store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted"))
        store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="done"))


def ready_index(store):
    # 只让原有有界回填追平；不替换分页、索引或任务状态。
    for _ in range(20):
        page = store.session_tasks.list_page(limit=100)
        if page["coverage"] == "complete" and not page["load_errors"]:
            return
    pytest.fail("canonical index did not reach complete coverage")


def retire(service, actor, view, **options):
    return service.transition(view["agent_id"], "retire", actor=actor,
        expected_revision=view["revision"], idempotency_key="retire", **options)


def chain(service, actor):
    root = service.submit({"space_id": "space-A", "instruction": "root"}, actor=actor, idempotency_key="root")["task_id"]
    child = service.follow_up({"task_id": root, "instruction": "child"}, actor=actor, idempotency_key="child")["new_task_id"]
    leaf = service.follow_up({"task_id": child, "instruction": "leaf"}, actor=actor, idempotency_key="leaf")["new_task_id"]
    return root, child, leaf


@pytest.mark.parametrize("count", [100, 101, 205])
def test_terminal_history_retires_and_restores_same_identity(business_host, count):
    service, host, actor, view = worker(business_host)
    terminal_history(host.conversation_store, actor.source_thread_id, view["agent_id"], count)
    ready_index(host.conversation_store)
    result = retire(service, actor, view)
    assert result["agent"]["identity_state"] == "retired"
    assert result["outstanding_refs"] == []
    restored = service.transition(view["agent_id"], "restore", actor=actor,
        expected_revision=result["agent"]["revision"], idempotency_key="restore")
    assert restored["agent"]["identity_state"] == "available"
    assert restored["agent"]["agent_id"] == view["agent_id"]


# LLM: 原三层提交、guidance及wake均用规范存储；核验真实运行时取消门，不把返回completed当作后代已撤销。
# 函数用途: 验证完整后代取消和单任务取消的实际可运行范围，以及重复调用幂等。
@pytest.mark.parametrize("descendants", [False, True])
def test_three_level_cancellation_uses_complete_original_chain(business_host, descendants):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _session_task_turn_was_cancelled,
    )

    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    ready_index(store)
    result = service.cancel_task(ids[0], actor=actor, include_descendants=descendants)
    expected = ["cancelled"] * 3 if descendants else ["cancelled", "queued", "queued"]
    assert [store.session_tasks.require(task_id).status for task_id in ids] == expected
    assert result == {"operation_id": ids[0], "state": "completed", "outstanding_refs": []}
    for task_id, status in zip(ids, expected):
        task = store.session_tasks.require(task_id)
        assert store.guidance.receipt(task.body_dedupe_key).status == ("rejected" if status == "cancelled" else "pending")
        wake = next(item for item in store.wakes.pending() if item.metadata.get("session_task_id") == task_id)
        request = BackgroundRunRequest(thread_id=task.target_thread_id, task_id=task.conversation_request_id,
            reason=wake.reason, wake_signal=wake.to_dict())
        assert _session_task_turn_was_cancelled(host, request) is (status == "cancelled")
    assert service.cancel_task(ids[0], actor=actor, include_descendants=descendants) == result


def edit_task(store, task_id, transform):
    from agent_py_agent.agent.conversation.session_tasks import SessionTask
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    update_json_file_atomic(store.session_tasks._path(task_id),
        lambda raw: transform(SessionTask.from_dict(raw)).to_dict(), require_existing=True)
    store.session_tasks._list_index().publish(store.session_tasks._path(task_id))


def settle(store, task_id, status="done"):
    store.session_tasks.advance(task_id, SessionTaskUpdate(status="accepted"))
    return store.session_tasks.advance(task_id, SessionTaskUpdate(status=status,
        result_refs=("original-result",), summary="original-summary"))


def eventually_retired(host, identifier):
    for _ in range(10):
        thread = reconcile_background_identity(host, identifier)
        if thread.metadata["background_agent"]["identity_state"] == "retired":
            return thread
    pytest.fail("fresh complete traversal did not converge")


@pytest.mark.parametrize("obligation", ["unfinished", "cancellation", "learning"])
def test_later_page_obligation_blocks_then_resolves(business_host, obligation):
    service, host, actor, view = worker(business_host)
    store = host.conversation_store
    old = store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, view["agent_id"], "old obligation"))
    if obligation != "unfinished":
        settle(store, old.task_id)
        metadata = ({"cancellation_control": {"outstanding_refs": ["exact-stop"]}}
            if obligation == "cancellation" else {"task_learning_enqueue": {"status": "pending"},
                "trusted_task_completion": {"completion_ref": "exact-completion"}})
        edit_task(store, old.task_id, lambda task: replace(task, metadata=metadata))
    terminal_history(store, actor.source_thread_id, view["agent_id"], 101)
    ready_index(store)
    result = retire(service, actor, view)
    assert result["agent"]["identity_state"] == "retiring"
    expected = {"unfinished": old.task_id, "cancellation": "exact-stop", "learning": "exact-completion"}[obligation]
    assert expected in result["outstanding_refs"]
    if obligation == "unfinished":
        store.session_tasks.advance(old.task_id, SessionTaskUpdate(status="cancelled"))
    else:
        edit_task(store, old.task_id, lambda task: replace(task, metadata={}))
    eventually_retired(host, view["agent_id"])


def test_available_reuse_never_scans_history(business_host, monkeypatch):
    _, host, _, view = worker(business_host)
    def forbidden(**kwargs):
        pytest.fail("ordinary reusable completion performed a history scan")
    monkeypatch.setattr(host.conversation_store.session_tasks, "list_page", forbidden)
    for _ in range(3):
        result = reconcile_background_identity(host, view["agent_id"])
        assert result.metadata["background_agent"]["identity_state"] == "available"
        assert result.metadata["background_agent"]["identity_revision"] == view["revision"]


def test_real_cursor_expiry_does_not_merge_snapshots(business_host, monkeypatch):
    from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
    _, host, actor, view = worker(business_host)
    store = host.conversation_store
    terminal_history(store, actor.source_thread_id, view["agent_id"], 101)
    ready_index(store)
    original = store.session_tasks.list_page
    seen = []
    injected = []
    def concurrent_write(**kwargs):
        page = original(**kwargs)
        seen.append((kwargs, page["coverage"]))
        if page["next_cursor"] and not injected:
            injected.append(store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, view["agent_id"], "new obligation")))
        return page
    monkeypatch.setattr(store.session_tasks, "list_page", concurrent_write)
    refs, _ = identity_outstanding(host, store.threads.require(view["agent_id"]))
    assert "session_tasks:coverage_unknown" in refs
    assert any(options.get("cursor") and coverage == "expired" for options, coverage in seen)
    assert all(options["limit"] <= 100 for options, _ in seen)
    refs, _ = identity_outstanding(host, store.threads.require(view["agent_id"]))
    assert injected[0].task_id in refs
    store.session_tasks.advance(injected[0].task_id, SessionTaskUpdate(status="cancelled"))
    ready_index(store)
    assert identity_outstanding(host, store.threads.require(view["agent_id"])) == ([], [])


def test_partial_and_unreadable_history_remain_unresolved(business_host):
    from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
    service, host, actor, view = worker(business_host)
    store = host.conversation_store
    terminal_history(store, actor.source_thread_id, view["agent_id"], 205)
    refs, _ = identity_outstanding(host, store.threads.require(view["agent_id"]))
    assert "session_tasks:coverage_unknown" in refs
    ready_index(store)
    broken = store.session_tasks._dir() / "broken.json"
    broken.write_text("not-json")
    ready_index_with_error = store.session_tasks.list_page(limit=100)
    assert ready_index_with_error["coverage"] in {"partial", "complete"}
    result = retire(service, actor, view)
    assert result["agent"]["identity_state"] == "retiring"
    assert "session_tasks:coverage_unknown" in result["outstanding_refs"]


@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
def test_terminal_root_and_intermediate_preserve_results_but_visit_leaf(business_host, terminal):
    service, host, actor = _service(business_host)
    root, child, leaf = chain(service, actor)
    store = host.conversation_store
    if terminal == "cancelled":
        store.session_tasks.advance(root, SessionTaskUpdate(status="cancelled"))
    else:
        settle(store, root, terminal)
    settle(store, child)
    before = {task_id: store.session_tasks.require(task_id).to_dict() for task_id in (root, child)}
    ready_index(store)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result["state"] == "completed" and result["operation_id"] == root
    assert store.session_tasks.require(leaf).status == "cancelled"
    for task_id in (root, child):
        after = store.session_tasks.require(task_id).to_dict()
        after["metadata"].pop("cancellation_control", None)
        assert after == before[task_id]
    assert service.cancel_task(root, actor=actor, include_descendants=True) == result


def test_descendant_after_page_one_and_unrelated_history(business_host):
    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    target = store.session_tasks.require(ids[0]).target_thread_id
    terminal_history(store, actor.source_thread_id, target, 105)
    unrelated = service.submit({"space_id": "space-A", "instruction": "unrelated"}, actor=actor, idempotency_key="unrelated")["task_id"]
    ready_index(store)
    original = store.session_tasks.require(unrelated).to_dict()
    result = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert result["state"] == "completed"
    assert [store.session_tasks.require(task_id).status for task_id in ids] == ["cancelled"] * 3
    assert store.session_tasks.require(unrelated).to_dict() == original


def test_running_descendant_retains_exact_stop_until_retry_confirms(business_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import session_task_stop
    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    leaf = store.session_tasks.require(ids[-1])
    receipt = store.guidance.receipt(leaf.body_dedupe_key)
    assert store.guidance.claim_for_turn(receipt.entry, expected_turn_id="exact-turn", attempt_id="attempt", owning_task_id=leaf.task_id)
    store.session_tasks.advance(leaf.task_id, SessionTaskUpdate(status="accepted", conversation_request_id="exact-turn"))
    confirmed, calls = [False], []
    def stop(host, *, target_thread, turn_id, canonical_task=None):
        calls.append((target_thread.thread_id, turn_id))
        return session_task_stop.SessionTaskStopOutcome(confirmed[0], "external stop observation", turn_id)
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    ready_index(store)
    pending = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert pending == {"operation_id": ids[0], "state": "pending", "outstanding_refs": ["exact-turn"]}
    assert store.session_tasks.require(ids[0]).metadata["cancellation_control"]["outstanding_refs"] == ["exact-turn"]
    confirmed[0] = True
    done = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert done == {"operation_id": ids[0], "state": "completed", "outstanding_refs": []}
    assert calls == [(leaf.target_thread_id, "exact-turn")] * 2
    assert service.cancel_task(ids[0], actor=actor, include_descendants=True) == done
    assert len(calls) == 2


def test_partial_descendant_traversal_retains_control_and_converges(business_host):
    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    target = store.session_tasks.require(ids[0]).target_thread_id
    terminal_history(store, actor.source_thread_id, target, 300)
    first = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert first["state"] == "pending" and "descendants:coverage_unknown" in first["outstanding_refs"]
    assert [store.session_tasks.require(task_id).status for task_id in ids] == ["queued"] * 3
    for _ in range(12):
        result = service.cancel_task(ids[0], actor=actor, include_descendants=True)
        if result["state"] == "completed":
            break
    assert result["state"] == "completed" and result["operation_id"] == ids[0]
    assert [store.session_tasks.require(task_id).status for task_id in ids] == ["cancelled"] * 3


def test_completed_running_root_is_not_stopped_again(business_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import session_task_stop
    service, host, actor = _service(business_host)
    root = service.submit({"space_id": "space-A", "instruction": "root"}, actor=actor, idempotency_key="root")["task_id"]
    store = host.conversation_store
    store.session_tasks.advance(root, SessionTaskUpdate(status="accepted", conversation_request_id="original-turn"))
    calls = []
    def stop(host, *, target_thread, turn_id, canonical_task=None):
        calls.append(turn_id)
        return session_task_stop.SessionTaskStopOutcome(True, "confirmed", turn_id)
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    ready_index(store)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result["state"] == "completed"
    assert service.cancel_task(root, actor=actor, include_descendants=True) == result
    assert calls == ["original-turn"]


@pytest.mark.parametrize("action", ["resume", "cancel_tasks"])
def test_lifecycle_action_reaches_queued_task_after_page_one(business_host, action):
    service, host, actor, view = worker(business_host)
    store = host.conversation_store
    queued = service.assign({"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "old queued"},
        actor=actor, idempotency_key="assign")["task_id"]
    for wake in store.wakes.pending(limit=0):
        store.wakes.mark_handled(wake.wake_signal_id)
    terminal_history(store, actor.source_thread_id, view["agent_id"], 101)
    ready_index(store)
    if action == "resume":
        suspended = service.transition(view["agent_id"], "suspend", actor=actor,
            expected_revision=view["revision"], idempotency_key="suspend")
        service.transition(view["agent_id"], "resume", actor=actor,
            expected_revision=suspended["agent"]["revision"], idempotency_key="resume")
        assert any(w.metadata.get("session_task_id") == queued for w in store.wakes.pending(limit=0))
        assert store.session_tasks.require(queued).status == "queued"
    else:
        result = retire(service, actor, view, disposition="cancel_tasks")
        assert store.session_tasks.require(queued).status == "cancelled"
        assert store.guidance.receipt(store.session_tasks.require(queued).body_dedupe_key).status == "rejected"
        assert result["agent"]["identity_state"] == "retired"


def test_cancellation_expired_snapshot_changes_no_task_states_and_fresh_retry_converges(business_host, monkeypatch):
    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    terminal_history(store, actor.source_thread_id, store.session_tasks.require(ids[0]).target_thread_id, 101)
    ready_index(store)
    original, observations, changed = store.session_tasks.list_page, [], []
    def concurrent_change(**kwargs):
        page = original(**kwargs)
        observations.append(page["coverage"])
        if page["next_cursor"] and not changed:
            changed.append(True)
            store.session_tasks.advance(page["items"][0].task_id, SessionTaskUpdate(status="done"))
        return page
    monkeypatch.setattr(store.session_tasks, "list_page", concurrent_change)
    result = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert result["state"] == "pending" and "descendants:coverage_unknown" in result["outstanding_refs"]
    assert "expired" in observations
    assert [store.session_tasks.require(identifier).status for identifier in ids] == ["queued"] * 3
    for _ in range(10):
        result = service.cancel_task(ids[0], actor=actor, include_descendants=True)
        if result["state"] == "completed":
            break
    assert result["state"] == "completed"
    assert [store.session_tasks.require(identifier).status for identifier in ids] == ["cancelled"] * 3


@pytest.mark.parametrize("conflict", ["owner", "space"])
def test_every_descendant_is_scope_checked_before_any_cancellation(business_host, conflict):
    service, host, actor = _service(business_host)
    root, child, leaf = chain(service, actor)
    store = host.conversation_store
    if conflict == "owner":
        foreign = store.threads.create(owner_id="foreign", canonical_user_id="foreign")
        edit_task(store, leaf, lambda task: replace(task, target_thread_id=foreign.thread_id))
    else:
        edit_task(store, leaf, lambda task: replace(task, metadata={**task.metadata,
            "business_context": {**task.metadata["business_context"], "space_id": "space-B"}}))
    ready_index(store)
    with pytest.raises(PermissionError, match="OWNER_MISMATCH|SPACE_MISMATCH"):
        service.cancel_task(root, actor=actor, include_descendants=True)
    assert [store.session_tasks.require(identifier).status for identifier in (root, child, leaf)] == ["queued"] * 3


def test_cycle_protection_visits_each_task_once(business_host):
    service, host, actor = _service(business_host)
    root, child, leaf = chain(service, actor)
    store = host.conversation_store
    edit_task(store, root, lambda task: replace(task, origin_task_id=leaf))
    ready_index(store)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result["state"] == "completed"
    assert [store.session_tasks.require(identifier).status for identifier in (root, child, leaf)] == ["cancelled"] * 3


def test_reference_overflow_is_explicit_and_all_stops_can_later_complete(business_host, monkeypatch):
    from agent_py_agent.agent.conversation.business_contracts import BusinessContextIntent
    from agent_py_agent.agent.gateway_parts import session_task_stop
    service, host, actor = _service(business_host)
    root = service.submit({"space_id": "space-A", "instruction": "root"}, actor=actor, idempotency_key="root")["task_id"]
    store = host.conversation_store
    task, context = service._owned_task(root, actor)
    children = []
    for i in range(101):
        child = store.session_tasks.create(SessionTaskDraft(task.sender_thread_id, task.target_thread_id,
            "running child", origin_task_id=root), business_context_intent=BusinessContextIntent(actor.owner_id, "space-A", None, None),
            business_context_factory=lambda identifier: replace(context, task_id=identifier))
        store.session_tasks.advance(child.task_id, SessionTaskUpdate(status="accepted", conversation_request_id=f"turn-{i}"))
        children.append(child.task_id)
    confirmed, calls = [False], []
    def stop(host, *, target_thread, turn_id, canonical_task=None):
        calls.append(turn_id)
        return session_task_stop.SessionTaskStopOutcome(confirmed[0], "external observation", turn_id)
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    ready_index(store)
    pending = service.cancel_task(root, actor=actor, include_descendants=True)
    assert pending["state"] == "pending" and len(pending["outstanding_refs"]) <= 100
    assert "descendants:unresolved_scope" in pending["outstanding_refs"]
    assert all(store.session_tasks.require(identifier).metadata["cancellation_control"]["outstanding_refs"] for identifier in children)
    confirmed[0] = True
    for _ in range(10):
        result = service.cancel_task(root, actor=actor, include_descendants=True)
        if result["state"] == "completed":
            break
    assert result == {"operation_id": root, "state": "completed", "outstanding_refs": []}
    assert set(calls) == {f"turn-{i}" for i in range(101)} and len(calls) == 202


def test_existing_descendant_execution_cancellation_is_not_erased(business_host, tmp_path):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    service, host, actor = _service(business_host)
    root, _, leaf = chain(service, actor)
    store = host.conversation_store
    task, context = service._owned_task(leaf, actor)
    repo = RuntimeRepository(tmp_path / "execution.db")
    run = repo.record_run_creation(owner_id=actor.owner_id, conversation_task_id="runtime-root",
        run_id="still-unconfirmed", thread_id=task.target_thread_id, attempt_status="running",
        task_run_metadata={"business_context": context.to_dict(), "session_task_ref": leaf})
    store.session_tasks.bind_execution(leaf, task_run_id=run["task_run_id"], target_thread_id=task.target_thread_id)
    ref = repo.parent_execution_ref(owner_id=actor.owner_id, run_id="still-unconfirmed", attempt_id=run["attempt_id"])
    store.session_tasks.record_execution_cancellation_control(leaf, execution_ref=ref, outstanding_refs=["still-unconfirmed"])
    ready_index(store)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result["state"] == "pending" and "still-unconfirmed" in result["outstanding_refs"]
    current = store.session_tasks.require(leaf)
    assert current.metadata["cancellation_control"]["outstanding_refs"] == ["still-unconfirmed"]
    assert current.metadata["cancellation_control"]["execution_ref"] == ref.to_dict()
    # 普通会话控制没有原执行退出证明，不能借空列表抹去独立执行责任；真实确认收口由上面的原生调度回归覆盖。
    store.session_tasks.record_cancellation_control(leaf, outstanding_refs=[])
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result == {"operation_id": root, "state": "pending", "outstanding_refs": ["still-unconfirmed"]}
    assert store.session_tasks.require(leaf).metadata["cancellation_control"]["execution_ref"] == ref.to_dict()


def test_terminal_root_atomic_updater_revalidates_business_binding(business_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import io
    service, host, actor = _service(business_host)
    root, _, _ = chain(service, actor)
    store = host.conversation_store
    settle(store, root)
    ready_index(store)
    original, changed = io.update_json_file_atomic, []
    def change_binding(path, updater, **options):
        if path == store.session_tasks._path(root) and not changed:
            changed.append(True)
            original(path, lambda raw: {**raw, "metadata": {**raw["metadata"], "business_context": {
                **raw["metadata"]["business_context"], "space_id": "space-B"}}}, require_existing=True)
        return original(path, updater, **options)
    monkeypatch.setattr(io, "update_json_file_atomic", change_binding)
    with pytest.raises(PermissionError, match="SESSION_TASK_SCOPE_MISMATCH"):
        service.cancel_task(root, actor=actor, include_descendants=True)
    current = store.session_tasks.require(root)
    assert current.status == "done" and current.result_refs == ("original-result",)
    assert current.metadata["business_context"]["space_id"] == "space-B"


@pytest.mark.parametrize("terminal", ["done", "failed"])
def test_terminal_ancestor_keeps_its_unconfirmed_turn_until_stop_confirmation(business_host, monkeypatch, terminal):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import session_task_stop

    service, host, actor = _service(business_host)
    root, _, _ = chain(service, actor)
    store = host.conversation_store
    store.session_tasks.advance(root, SessionTaskUpdate(status="accepted", conversation_request_id="original-turn"))
    store.session_tasks.advance(root, SessionTaskUpdate(status=terminal, result_refs=("original-result",)))
    edit_task(store, root, lambda task: replace(task, metadata={**task.metadata,
        "cancellation_control": {"conversation_request_id": "original-turn", "outstanding_refs": ["original-turn"]}}))
    ready_index(store)
    confirmed, stopped = [False], []
    def stop(host, *, target_thread, turn_id, canonical_task=None):
        stopped.append(turn_id)
        return SimpleNamespace(confirmed=confirmed[0])
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result == {"operation_id": root, "state": "pending", "outstanding_refs": ["original-turn"]}
    current = store.session_tasks.require(root)
    assert current.status == terminal and current.result_refs == ("original-result",)
    confirmed[0] = True
    assert service.cancel_task(root, actor=actor, include_descendants=True) == {
        "operation_id": root, "state": "completed", "outstanding_refs": []}
    assert stopped == ["original-turn", "original-turn"]


def test_ancestor_cancel_preserves_descendants_previous_root_responsibility(business_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import session_task_stop

    service, host, actor = _service(business_host)
    root, child, leaf = chain(service, actor)
    store = host.conversation_store
    for identifier, turn in ((child, "child-turn"), (leaf, "leaf-turn")):
        store.session_tasks.advance(identifier, SessionTaskUpdate(status="accepted", conversation_request_id=turn))
    confirmed, stopped = set(), []
    def stop(host, *, target_thread, turn_id, canonical_task=None):
        stopped.append(turn_id)
        return session_task_stop.SessionTaskStopOutcome(turn_id in confirmed, "observed stop", turn_id)
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    ready_index(store)
    pending = service.cancel_task(child, actor=actor, include_descendants=True)
    assert pending == {"operation_id": child, "state": "pending", "outstanding_refs": ["child-turn", "leaf-turn"]}
    confirmed.add("child-turn")
    assert service.cancel_task(root, actor=actor, include_descendants=True) == {
        "operation_id": root, "state": "pending", "outstanding_refs": ["leaf-turn"]}
    control = store.session_tasks.require(child).metadata["cancellation_control"]
    assert control["outstanding_refs"] == ["leaf-turn"]
    assert control["descendant_refs"] == {leaf: ["leaf-turn"]}
    before_retry = list(stopped)
    assert service.cancel_task(child, actor=actor, include_descendants=False) == {
        "operation_id": child, "state": "pending", "outstanding_refs": ["leaf-turn"]}
    assert stopped == before_retry
    assert store.session_tasks.require(leaf).metadata["cancellation_control"]["outstanding_refs"] == ["leaf-turn"]
    confirmed.add("leaf-turn")
    assert service.cancel_task(child, actor=actor, include_descendants=True) == {
        "operation_id": child, "state": "completed", "outstanding_refs": []}
    assert service.cancel_task(root, actor=actor, include_descendants=True) == {
        "operation_id": root, "state": "completed", "outstanding_refs": []}
    assert [store.session_tasks.require(identifier).status for identifier in (root, child, leaf)] == ["cancelled"] * 3


@pytest.mark.parametrize("first_descendants", [False, True])
def test_overlapping_cancel_preserves_new_descendant_scope(business_host, monkeypatch, first_descendants):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident

    from agent_py_agent.agent.conversation import session_messaging
    from agent_py_agent.agent.gateway_parts import session_task_stop

    service, host, actor = _service(business_host)
    root, _, leaf = chain(service, actor)
    store = host.conversation_store
    store.session_tasks.advance(root, SessionTaskUpdate(status="accepted", conversation_request_id="root-turn"))
    confirmed = {"root-turn"}
    def stop(host, *, target_thread, turn_id, canonical_task=None):
        return session_task_stop.SessionTaskStopOutcome(turn_id in confirmed, "observed stop", turn_id)
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    ready_index(store)
    original = session_messaging._save_cancellation_control
    saving, resume, single_thread = Event(), Event(), []
    def save(host, binding, context, refs, **options):
        if single_thread and get_ident() == single_thread[0] and not refs and binding[0] == root:
            saving.set()
            assert resume.wait(timeout=10), "overlapping cancellation did not release final save"
        return original(host, binding, context, refs, **options)
    monkeypatch.setattr(session_messaging, "_save_cancellation_control", save)
    def single_cancel():
        single_thread.append(get_ident())
        return service.cancel_task(root, actor=actor, include_descendants=first_descendants)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(single_cancel)
        try:
            assert saving.wait(timeout=10), "single-task cancel did not reach its final save"
            new_leaf = service.follow_up({"task_id": leaf, "instruction": "new cancellation scope"},
                actor=actor, idempotency_key="overlapping-scope")["new_task_id"]
            store.session_tasks.advance(new_leaf, SessionTaskUpdate(status="accepted", conversation_request_id="leaf-turn"))
            assert service.cancel_task(root, actor=actor, include_descendants=True) == {
                "operation_id": root, "state": "pending", "outstanding_refs": ["leaf-turn"]}
        finally:
            resume.set()
        result = future.result(timeout=10)
    assert result == {"operation_id": root, "state": "pending", "outstanding_refs": ["leaf-turn"]}
    control = store.session_tasks.require(root).metadata["cancellation_control"]
    assert control["outstanding_refs"] == ["leaf-turn"]
    assert control["descendant_refs"] == {new_leaf: ["leaf-turn"]}
    confirmed.add("leaf-turn")
    assert service.cancel_task(root, actor=actor, include_descendants=True) == {
        "operation_id": root, "state": "completed", "outstanding_refs": []}
