from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests.test_mega_business_task_run_lineage import context


def test_shared_turn_bind_restores_child_context_from_exact_attempt(tmp_path, monkeypatch):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    assert hasattr(repo, "parent_execution_ref")
    root = repo.record_run_creation(owner_id="owner", run_id="root", task_run_metadata={
        "business_context": context().to_dict(), "session_task_ref": "stask-exact"})
    child = repo.record_run_creation(owner_id="owner", run_id="child", host_parent_execution=repo.parent_execution_ref(
        owner_id="owner", run_id="root", attempt_id=root["attempt_id"]))
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), home_paths=SimpleNamespace(owner_id="owner"))
    monkeypatch.setattr(runtime_mixin, "_bind_main_agent_authority", lambda a, p: p)
    monkeypatch.setattr(runtime_mixin, "attach_run_task_workspace_context", lambda a, p, text: p)
    params = RunParams(run_id="child", attempt_id=child["attempt_id"], task_id="different-runtime-task")
    bound = runtime_mixin._bind_main_agent_turn_params(agent, "hello", params)
    assert bound.business_context == context()
    assert bound.business_context.task_id != bound.task_id
    with pytest.raises(ValueError):
        runtime_mixin._bind_main_agent_turn_params(agent, "hello", replace(params, business_context=replace(context(), space_id="late-space")))


def test_root_binding_persists_original_session_context_and_reverse_locator(tmp_path):
    conversation = ConversationStore(tmp_path / "conversation")
    source = conversation.threads.create(owner_id="owner")
    target = conversation.threads.create(owner_id="owner")
    from agent_py_agent.agent.conversation.business_contracts import BusinessContextIntent
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft

    task = conversation.session_tasks.create(SessionTaskDraft(source.thread_id, target.thread_id, "business"),
        business_context_intent=BusinessContextIntent("owner", "space-a", "project", None),
        business_context_factory=lambda task_id: context(task_id))
    repo = RuntimeRepository(tmp_path / "runtime.db")
    agent = SimpleNamespace(conversation_store=conversation, subagents=SimpleNamespace(runtime_db=repo),
                            home_paths=SimpleNamespace(owner_id="owner"))
    params = RunParams(run_id="main", task_id="runtime-task", business_context=context(task.task_id),
                       task_attributes={"conversation_thread_id": target.thread_id})
    bound = runtime_mixin._bind_main_agent_authority(agent, params)
    restored = repo.business_context_for_execution(owner_id="owner", run_id=bound.run_id, attempt_id=bound.attempt_id)
    assert restored == context(task.task_id)
    assert conversation.session_tasks.load(task.task_id).task_run_id == repo.agent_run_for_run_id("main")["task_run_id"]


def test_sequential_business_tasks_on_same_thread_get_distinct_exact_roots(tmp_path):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.business_contracts import BusinessContextIntent
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft

    store = ConversationStore(tmp_path / "conversations")
    source, target = store.threads.create(owner_id="owner"), store.threads.create(owner_id="owner")
    repo = RuntimeRepository(tmp_path / "runtime.db")
    host = SimpleNamespace(conversation_store=store, subagents=SimpleNamespace(runtime_db=repo),
                           home_paths=SimpleNamespace(owner_id="owner"))
    bound = []
    for number in range(2):
        task = store.session_tasks.create(SessionTaskDraft(source.thread_id, target.thread_id, f"work {number}"),
            business_context_intent=BusinessContextIntent("owner", "space-a", "project", None),
            business_context_factory=lambda identifier: context(identifier))
        params = RunParams(run_id="bg-main-thread", business_context=context(task.task_id),
                           task_attributes={"conversation_thread_id": target.thread_id})
        result = runtime_mixin._bind_main_agent_authority(host, params)
        assert repo.business_context_for_execution(owner_id="owner", run_id=result.run_id, attempt_id=result.attempt_id) == context(task.task_id)
        bound.append(result)
    assert bound[0].run_id != bound[1].run_id
    assert repo.agent_run_for_run_id(bound[0].run_id)["task_run_id"] != repo.agent_run_for_run_id(bound[1].run_id)["task_run_id"]


def test_failed_reverse_locator_does_not_publish_or_enter_model_and_can_repair(tmp_path, monkeypatch):
    import pytest

    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.business_contracts import BusinessContextIntent
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft

    store = ConversationStore(tmp_path / "conversations")
    source, target = store.threads.create(owner_id="owner"), store.threads.create(owner_id="owner")
    task = store.session_tasks.create(SessionTaskDraft(source.thread_id, target.thread_id, "work"),
        business_context_intent=BusinessContextIntent("owner", "space-a", "project", None),
        business_context_factory=lambda identifier: context(identifier))
    repo = RuntimeRepository(tmp_path / "runtime.db")
    host = SimpleNamespace(conversation_store=store, subagents=SimpleNamespace(runtime_db=repo), home_paths=SimpleNamespace(owner_id="owner"))
    published = []
    params = RunParams(run_id="main", task_id="runtime-task", business_context=context(task.task_id),
        conversation_task_binding_callback=SimpleNamespace(bind_runtime_authority=lambda value: published.append(value) or True),
        task_attributes={"conversation_thread_id": target.thread_id})
    original = store.session_tasks.bind_execution
    def fail(*args, **kwargs):
        raise OSError("locator write failed")
    monkeypatch.setattr(store.session_tasks, "bind_execution", fail)
    with pytest.raises(OSError):
        runtime_mixin._bind_main_agent_authority(host, params)
    assert published == []
    first = repo.agent_run_for_run_id("main")
    assert repo.get_attempt(first["current_attempt_id"])["ended_at"] > 0
    monkeypatch.setattr(store.session_tasks, "bind_execution", original)
    repaired = runtime_mixin._bind_main_agent_authority(host, params)
    assert store.session_tasks.require(task.task_id).task_run_id == first["task_run_id"]
    assert published[-1]["attempt_id"] == repaired.attempt_id
