"""B 路学习来源：子代理接线（spawn 冻结 → 线程/运行期/候选 合成；自动化触发不可学）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.conversation.agent_thread import ensure_subagent_thread
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.subagents.context_bundle_refs import runtime_task_attributes
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentTask


def _run_agent(run_attributes: dict, *, owner_home: str = ""):
    return SimpleNamespace(
        _current_run_params=SimpleNamespace(task_id="", task_attributes=dict(run_attributes)),
        home_paths=SimpleNamespace(owner_home_dir=owner_home, owner_id="local/test"),
    )


# ---------- spawn 判定：自动化痕迹优先、显式其次、都无则 fail-closed ----------


def test_spawn_source_uses_explicit_user_dialogue_projection():
    source = ls.spawn_source_projection({"learning_source": ls.user_dialogue_projection()})
    assert source == ls.user_dialogue_projection()
    assert ls.projection_is_learnable(source) is True


def test_spawn_source_maps_automation_traces_to_unlearnable_projections():
    cases = {
        "scheduler_run_id": "schedule",
        "scheduler_job_id": "schedule",
        "conversation_session_task_id": "dispatch",
        "session_task_id": "dispatch",
        "background_wake_signal_id": "background",
        "wake_signal_id": "background",
        "background_delivery_reason": "background",
    }
    for key, expected_kind in cases.items():
        source = ls.spawn_source_projection({key: "trace-1"})
        assert source is not None, key
        assert source["source_kind"] == expected_kind, key
        assert source["can_learn"] is False, key


def test_spawn_source_trace_wins_over_explicit_projection():
    source = ls.spawn_source_projection(
        {
            "learning_source": ls.user_dialogue_projection(),
            "scheduler_run_id": "sr-1",
        }
    )
    assert source["source_kind"] == "schedule"
    assert source["can_learn"] is False


def test_spawn_source_without_evidence_or_with_bad_explicit_is_none():
    assert ls.spawn_source_projection({}) is None
    assert ls.spawn_source_projection(None) is None
    assert ls.spawn_source_projection("not-a-mapping") is None
    assert ls.spawn_source_projection({"learning_source": {"source_kind": "bogus"}}) is None
    assert ls.spawn_source_projection({"learning_source": {"can_learn": True}}) is None


# ---------- 子代理投影合成：标签、run/task 关联、禁止来源沿链继承 ----------


def test_child_projection_from_user_dialogue_is_learnable_with_tag_and_ids():
    child = ls.child_projection_from_parent(
        ls.user_dialogue_projection(), run_id="run-a", task_id="task-a"
    )
    assert child["source_kind"] == "subagent"
    assert child["subagent_tag"] == "subagent"
    assert child["trigger_kind"] == "user_dialogue"
    assert child["can_learn"] is True
    assert child["source_run_ids"] == ["run-a"]
    assert child["source_task_ids"] == ["task-a"]


def test_child_projection_accumulates_lineage_across_generations():
    first = ls.child_projection_from_parent(
        ls.user_dialogue_projection(), run_id="run-a", task_id="task-a"
    )
    second = ls.child_projection_from_parent(first, run_id="run-b", task_id="task-b")
    assert second["can_learn"] is True
    assert second["source_run_ids"] == ["run-a", "run-b"]
    assert second["source_task_ids"] == ["task-a", "task-b"]


def test_child_projection_from_automated_parent_stays_unlearnable():
    for parent in (ls.schedule_projection(), ls.dispatch_projection(), ls.background_projection()):
        child = ls.child_projection_from_parent(parent, run_id="run-x")
        assert child["can_learn"] is False
        assert child["trigger_kind"] == parent["trigger_kind"]
        assert child["source_kind"] == "subagent"
    chained = ls.child_projection_from_parent(
        ls.subagent_projection(trigger_kind="schedule", run_ids=["run-s"], task_ids=["task-s"]),
        run_id="run-t",
    )
    assert chained["can_learn"] is False
    assert chained["source_run_ids"] == ["run-s", "run-t"]


def test_child_projection_requires_parent_and_run_identity():
    assert ls.child_projection_from_parent(None, run_id="run-a") is None
    assert ls.child_projection_from_parent(ls.user_dialogue_projection(), run_id="") is None
    assert ls.child_projection_from_parent({"source_kind": "bogus"}, run_id="run-a") is None
    unknown_child = ls.child_projection_from_parent(ls.unknown_projection(), run_id="run-a")
    assert unknown_child["can_learn"] is False
    assert unknown_child["trigger_kind"] == "unknown"


# ---------- spawn 边界：宿主冻结来源、剔除模型自带 ----------


def test_create_task_attributes_freezes_host_source_and_drops_model_value():
    agent = _run_agent({"learning_source": ls.user_dialogue_projection()})
    attrs = create_task_attributes(
        {"goal": "g", "attributes": {"learning_source": {"source_kind": "bogus", "can_learn": True}}},
        agent,
    )
    assert attrs["learning_source"] == ls.user_dialogue_projection()


def test_create_task_attributes_marks_scheduled_and_dispatched_turns_unlearnable():
    scheduled = create_task_attributes({"goal": "g"}, _run_agent({"scheduler_run_id": "sr-1"}))
    assert scheduled["learning_source"]["source_kind"] == "schedule"
    assert scheduled["learning_source"]["can_learn"] is False
    dispatched = create_task_attributes(
        {"goal": "g"}, _run_agent({"conversation_session_task_id": "task-9"})
    )
    assert dispatched["learning_source"]["source_kind"] == "dispatch"
    assert dispatched["learning_source"]["can_learn"] is False


def test_create_task_attributes_without_evidence_writes_no_source():
    attrs = create_task_attributes({"goal": "g"}, _run_agent({}))
    assert "learning_source" not in attrs
    model_only = create_task_attributes(
        {"goal": "g", "attributes": {"learning_source": ls.user_dialogue_projection()}},
        None,
    )
    assert "learning_source" not in model_only


# ---------- 子代理线程：来源从冻结事实合成并落盘 ----------


def _agent_request(store: ConversationStore, thread_id: str = "thread-1", run_id: str = "run-1", **extra):
    return store.threads.ensure_agent(
        {
            "thread_id": thread_id,
            "agent_run_id": run_id,
            "canonical_user_id": "local/user",
            "owner_id": "local/user",
            "now": 10.0,
            **extra,
        }
    )


def test_agent_thread_writes_host_frozen_source_to_disk(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    child = ls.child_projection_from_parent(
        ls.user_dialogue_projection(), run_id="run-1", task_id="task-1"
    )
    thread = _agent_request(store, learning_source=child)
    loaded = store.threads.load(thread.thread_id)
    assert loaded.learning_source == child
    payload = json.loads(store.storage.thread_path(thread.thread_id).read_text(encoding="utf-8"))
    assert payload["learning_source"]["subagent_tag"] == "subagent"
    assert payload["learning_source"]["can_learn"] is True


def test_agent_thread_bad_source_is_normalized_empty(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _agent_request(store, learning_source={"can_learn": True})
    assert thread.learning_source == {}


def test_agent_thread_existing_source_is_not_overwritten(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    scheduled = ls.child_projection_from_parent(ls.schedule_projection(), run_id="run-1")
    first = _agent_request(store, learning_source=scheduled)
    again = _agent_request(
        store,
        learning_source=ls.child_projection_from_parent(
            ls.user_dialogue_projection(), run_id="run-1"
        ),
    )
    assert again.thread_id == first.thread_id
    assert again.learning_source == scheduled


def test_agent_thread_without_source_stays_empty(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _agent_request(store)
    assert thread.learning_source == {}


# ---------- 子代理线程创建（ensure_subagent_thread）从 task 冻结事实合成 ----------


def _manager(store: ConversationStore):
    return SimpleNamespace(
        conversation_store=store,
        owner_home_dir="",
        workspace_root="",
        load=lambda run_id: None,
    )


def _task(**overrides):
    values = {
        "agent_thread_id": "child-thread",
        "id": "run-child",
        "root_id": "root-child",
        "depth": 1,
        "parent_id": "",
        "description": "child",
        "goal": "child goal",
        "attributes": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_ensure_subagent_thread_uses_frozen_parent_facts(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    task = _task(attributes={"learning_source": ls.user_dialogue_projection()})
    thread = ensure_subagent_thread(_manager(store), task)
    assert thread.learning_source["source_kind"] == "subagent"
    assert thread.learning_source["can_learn"] is True
    assert thread.learning_source["source_run_ids"] == ["run-child"]
    assert thread.learning_source["source_task_ids"] == ["root-child"]


def test_ensure_subagent_thread_keeps_automated_ancestor_unlearnable(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    task = _task(attributes={"learning_source": ls.schedule_projection()})
    thread = ensure_subagent_thread(_manager(store), task)
    assert thread.learning_source["can_learn"] is False
    assert thread.learning_source["trigger_kind"] == "schedule"


def test_ensure_subagent_thread_without_facts_stays_unknown(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = ensure_subagent_thread(_manager(store), _task())
    assert thread.learning_source == {}


# ---------- 子代理运行期：合成自己的投影供孙代理继承 ----------


def _runtime_task(**overrides):
    values = {
        "id": "run-r",
        "root_id": "root-r",
        "attributes": {},
        "task_workspace_dir": "",
        "task_dir": "",
        "agent_run_workspace_dir": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runtime_attributes_synthesize_own_projection():
    task = _runtime_task(attributes={"learning_source": ls.user_dialogue_projection()})
    out = runtime_task_attributes(task)
    assert out["learning_source"]["source_kind"] == "subagent"
    assert out["learning_source"]["can_learn"] is True
    assert out["learning_source"]["source_run_ids"] == ["run-r"]


def test_runtime_attributes_accumulate_chain_and_keep_forbidden_source():
    parent_chain = ls.child_projection_from_parent(
        ls.schedule_projection(), run_id="run-parent", task_id="task-parent"
    )
    task = _runtime_task(attributes={"learning_source": parent_chain})
    out = runtime_task_attributes(task)
    assert out["learning_source"]["can_learn"] is False
    assert out["learning_source"]["trigger_kind"] == "schedule"
    assert out["learning_source"]["source_run_ids"] == ["run-parent", "run-r"]


def test_runtime_attributes_without_facts_do_not_invent_source():
    out = runtime_task_attributes(_runtime_task())
    assert "learning_source" not in out


# ---------- 候选：标签与 run/task 关联持久化 ----------


def _candidate_service(tmp_path):
    candidates = CandidateService(tmp_path / "owner" / "memory" / "candidates.jsonl")
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=candidates)
    return manager, candidates


def test_subagent_candidate_carries_tag_and_lineage(tmp_path):
    manager, candidates = _candidate_service(tmp_path)
    task = SubAgentTask(
        id="run-1",
        root_id="project-root",
        goal="g",
        thought="",
        plan=[],
        attributes={"learning_source": ls.user_dialogue_projection()},
        updated_at=100.0,
    )
    manager.memory_candidates.record_result_candidates(task, lessons=["lesson a"], findings=[])
    item = candidates.list()[0]
    assert item.learning_source["source_kind"] == "subagent"
    assert item.learning_source["subagent_tag"] == "subagent"
    assert item.learning_source["can_learn"] is True
    assert item.learning_source["source_run_ids"] == ["run-1"]
    assert item.learning_source["source_task_ids"] == ["project-root"]
    raw = json.loads(
        (tmp_path / "owner" / "memory" / "candidates.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert raw["learning_source"]["can_learn"] is True


def test_subagent_candidate_from_automated_parent_is_unlearnable(tmp_path):
    manager, candidates = _candidate_service(tmp_path)
    task = SubAgentTask(
        id="run-2",
        root_id="project-root",
        goal="g",
        thought="",
        plan=[],
        attributes={"learning_source": ls.background_projection()},
        updated_at=100.0,
    )
    manager.memory_candidates.record_result_candidates(task, lessons=["lesson b"], findings=[])
    item = candidates.list()[0]
    assert item.learning_source["can_learn"] is False
    assert item.learning_source["trigger_kind"] == "background"


def test_subagent_candidate_without_facts_is_unknown(tmp_path):
    manager, candidates = _candidate_service(tmp_path)
    task = SubAgentTask(
        id="run-3", root_id="project-root", goal="g", thought="", plan=[], updated_at=100.0
    )
    manager.memory_candidates.record_result_candidates(task, lessons=["lesson c"], findings=[])
    item = candidates.list()[0]
    assert item.learning_source["can_learn"] is False
    assert item.learning_source["source_kind"] == "unknown"


def test_subagent_introspection_lesson_keeps_source(tmp_path):
    manager, candidates = _candidate_service(tmp_path)
    task = SubAgentTask(
        id="run-4",
        root_id="project-root",
        goal="g",
        thought="",
        plan=[],
        attributes={"learning_source": ls.dispatch_projection()},
        updated_at=100.0,
    )
    manager.memory_candidates.record_introspection_lesson(
        task,
        suggested_params={"temperature": 0.2},
        failure_type="timeout",
        attempts=2,
        confidence=0.4,
    )
    item = candidates.list()[0]
    assert item.learning_source["can_learn"] is False
    assert item.learning_source["trigger_kind"] == "dispatch"
