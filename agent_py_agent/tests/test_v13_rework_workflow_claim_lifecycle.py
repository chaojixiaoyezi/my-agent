"""R1 原生工作流准入与清理生命周期回归。"""
from __future__ import annotations

import errno
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.business_api import (
    BusinessServices,
    _BusinessBackgroundService,
    bind_business_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.workflows import concurrency_policy as policy
from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps
from agent_py_agent.agent.workflows.engine import WorkflowEngine


def _definition(*, delegated=True):
    steps = [{"step_id": "prefix", "name": "原前缀", "mode": "llm_skill_analysis"}]
    if delegated:
        steps.append({"step_id": "agent", "name": "原任务", "mode": "agent_task",
                      "config": {"instruction": "核对原工作流输入"}})
    return {"descriptor": {"id": "lifecycle-flow", "kind": "workflow", "name": "准入生命周期",
                           "access_targets": [], "data_sources": [], "credential_refs": [], "io_contract": {}},
            "steps": steps}


@pytest.fixture
def native(tmp_path):
    """采用正式后台提交服务、规范任务/引导 Store 和真实 Engine，不调用模型。"""
    home = replace(home_paths(tmp_path / "home"), owner_id="alice", owner_provider="local", owner_kind="main")
    store = ConversationStore(home.owner_home_dir / "conversations")
    source = store.threads.create(owner_id="alice", canonical_user_id="source-user")
    actor = HostChannelActor("alice", source.thread_id, "original-request", "local", "local", "user")
    host = SimpleNamespace(home_paths=home, conversation_store=store,
                           config=AgentConfig(business_spaces_enabled=True, model_backend="echo"))
    services = BusinessServices(host)
    authority = bind_business_context(host, actor=actor, authorize=lambda *args: True)
    background = _BusinessBackgroundService(services, authority)
    space = services.spaces.create({"name": "原工作流空间"}, actor=services.actor(authority), idempotency_key="space")
    context = BusinessExecutionContext("alice", space["space_id"], None, "original-workflow-task",
                                       space["revision"], None, (), (), "business-context.v1")
    calls = []
    agents = WorkflowAgentSteps(background, actor_provider=lambda run: actor,
                               completion_reader=lambda *args, **kwargs: None,
                               cancel_handler=lambda task_id, **kwargs: background.cancel_task(
                                   task_id, actor=kwargs["actor"], include_descendants=True))
    engine = WorkflowEngine(home.owner_home_dir, agent_steps=agents, run_authorizer=lambda *args: None,
                            analysis_handler=lambda spec, *args: calls.append(spec["step_id"]) or {"value": 7})
    assert Path(policy.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    return SimpleNamespace(home=home.owner_home_dir, store=store, engine=engine, agents=agents, background=background,
                           actor=actor, context=context, calls=calls)


def _start(case, run_id, *, delegated=True, strategy="skip_if_running"):
    return case.engine.run_workflow(_definition(delegated=delegated), owner_id="alice",
                                    business_context=case.context, workflow_run_id=run_id,
                                    concurrency_policy=strategy)


def test_restricted_run_blocks_prefix_while_prior_agent_task_is_accepted(native):
    first = _start(native, "original")
    task = native.store.session_tasks.require(first.steps[-1].escalation_result["task_id"])
    assert first.status == "waiting_for_agent" and task.status in ("queued", "accepted")
    assert native.store.guidance.receipt(task.body_dedupe_key) is not None
    second = _start(native, "different")
    assert isinstance(second, dict) and second["status"] == "skipped"
    assert second["running_run_id"] == first.workflow_run_id
    assert native.calls == ["prefix"]
    rows, errors = native.store.session_tasks.list_report(limit=0)
    assert not errors and [row.task_id for row in rows] == [task.task_id]


def test_single_release_error_must_not_leave_live_process_claim(native, monkeypatch):
    original = policy._unlink_same
    errors = []

    def remove(parent, name, expected):
        if name == "claim.json" and not errors:
            errors.append(name)
            raise OSError(errno.EIO, "one claim removal error")
        return original(parent, name, expected)

    monkeypatch.setattr(policy, "_unlink_same", remove)
    returned = None
    try:
        returned = _start(native, "finished", delegated=False)
    except OSError:
        pass
    assert native.engine.store.load_run("finished").status == "succeeded"
    following = _start(native, "following", delegated=False)
    assert not isinstance(following, dict), "settled claim remained held after normal I/O resumed"
    assert following.status == "succeeded"
    assert returned is not None and returned.workflow_run_id == "finished"
    assert errors == ["claim.json"] and native.calls == ["prefix", "prefix"]


def test_accepted_queued_run_not_abandoned_after_one_poll_error(native, monkeypatch):
    entered, release, read_error, queued_finished = (threading.Event() for _ in range(4))
    original_queue = policy._queue
    order = []

    def queue(scope, parent):
        if threading.current_thread().name.startswith("workflow-queue-") and not read_error.is_set():
            read_error.set()
            raise OSError(errno.EIO, "one queue read error")
        return original_queue(scope, parent)

    def analyze(spec, context, skills):
        order.append(context.run_inputs["label"])
        if context.run_inputs["label"] == "first":
            entered.set()
            assert release.wait(10)
        else:
            queued_finished.set()
        return {"value": 7}

    native.engine.analysis_handler = analyze
    monkeypatch.setattr(policy, "_queue", queue)
    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(native.engine.run_workflow, _definition(delegated=False), {"label": "first"},
                             workflow_run_id="holder", concurrency_policy="queue")
        try:
            assert entered.wait(5)
            receipt = native.engine.run_workflow(_definition(delegated=False), {"label": "queued"},
                                                 workflow_run_id="accepted", concurrency_policy="queue")
            assert receipt["status"] == "queued" and read_error.wait(5)
            release.set()
            assert holder.result(5).status == "succeeded"
            assert queued_finished.wait(3), "accepted original workflow lost its queue worker"
            with native.engine.store._lock("accepted"):
                assert native.engine.store.load_run("accepted").status == "succeeded"
            assert order == ["first", "queued"]
        finally:
            release.set()
