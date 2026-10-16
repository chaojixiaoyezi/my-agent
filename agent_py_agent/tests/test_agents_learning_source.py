"""B 路学习来源投影的合同单测：构造、判定与 fail-closed 回退。"""

import pytest

from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.conversation import learning_source as ls

OWNER = "local/alice"
_PROJECTION_KEYS = {
    "schema_version",
    "source_kind",
    "trigger_kind",
    "can_learn",
    "subagent_tag",
    "source_run_ids",
    "source_task_ids",
    "policy_version",
}


def test_user_dialogue_projection_is_learnable_and_round_trips():
    projection = ls.user_dialogue_projection()
    assert set(projection) == _PROJECTION_KEYS
    facts = ls.projection_to_facts(
        projection,
        owner=OWNER,
        thread_id="thread-1",
        refs=ls.LearningSourceRefs(turn_id="turn-1", request_id="request-1"),
    )
    assert facts.can_learn is True
    assert facts.source_kind == "user_dialogue"
    assert facts.trigger_kind == "user_dialogue"
    assert facts.owner == OWNER
    assert facts.policy_version == ls.LEARNING_POLICY_VERSION


def test_subagent_projection_keeps_tag_and_lineage():
    projection = ls.subagent_projection(
        trigger_kind="user_dialogue", run_ids=["run-1"], task_ids=["task-1"]
    )
    assert projection["subagent_tag"] == "subagent"
    assert projection["source_run_ids"] == ["run-1"]
    facts = ls.projection_to_facts(projection, owner=OWNER, thread_id="thread-1")
    assert facts.can_learn is True
    assert facts.subagent_tag == "subagent"
    assert facts.source_run_ids == ("run-1",)
    assert facts.source_task_ids == ("task-1",)


@pytest.mark.parametrize("trigger", ["dispatch", "schedule", "background"])
def test_subagent_of_automation_cannot_learn(trigger):
    projection = ls.subagent_projection(trigger_kind=trigger, run_ids=["run-1"])
    assert projection["can_learn"] is False
    assert ls.projection_is_learnable(projection) is False


def test_subagent_projection_requires_nonempty_run_ids():
    with pytest.raises(AgentsContractError):
        ls.subagent_projection(trigger_kind="user_dialogue", run_ids=[])


@pytest.mark.parametrize(
    "factory",
    [ls.dispatch_projection, ls.schedule_projection, ls.background_projection, ls.unknown_projection],
)
def test_automation_projections_are_unlearnable(factory):
    projection = factory()
    assert projection["can_learn"] is False
    assert ls.projection_is_learnable(projection) is False
    assert ls.projection_to_facts(projection, owner=OWNER, thread_id="thread-1").can_learn is False


def test_projection_is_learnable_fails_closed_on_bad_shapes():
    assert ls.projection_is_learnable(None) is False
    assert ls.projection_is_learnable({}) is False
    missing_schema = dict(ls.user_dialogue_projection())
    del missing_schema["schema_version"]
    assert ls.projection_is_learnable(missing_schema) is False
    bad_flag = dict(ls.user_dialogue_projection())
    bad_flag["can_learn"] = "true"
    assert ls.projection_is_learnable(bad_flag) is False
    forged = dict(ls.unknown_projection())
    forged["can_learn"] = True
    assert ls.projection_is_learnable(forged) is False


def test_facts_for_message_unknown_default_is_unlearnable():
    facts = ls.facts_for_message({}, owner=OWNER, thread_id="thread-1")
    assert facts.can_learn is False
    assert facts.source_kind == "unknown"


def test_facts_for_message_uses_thread_source_default():
    facts = ls.facts_for_message(
        {"conversation_request_id": "req-1"},
        owner=OWNER,
        thread_id="thread-1",
        thread_source=ls.user_dialogue_projection(),
    )
    assert facts.can_learn is True
    assert facts.request_id == "req-1"
    assert facts.turn_id == "req-1"


def test_facts_for_message_explicit_source_overrides_thread():
    metadata = {
        ls.LEARNING_SOURCE_KEY: ls.background_projection(),
        "conversation_request_id": "req-1",
    }
    facts = ls.facts_for_message(
        metadata,
        owner=OWNER,
        thread_id="thread-1",
        thread_source=ls.user_dialogue_projection(),
    )
    assert facts.can_learn is False
    assert facts.source_kind == "background"


def test_facts_for_message_broken_explicit_source_stays_unlearnable():
    metadata = {ls.LEARNING_SOURCE_KEY: {"can_learn": True}, "conversation_request_id": "req-1"}
    facts = ls.facts_for_message(
        metadata,
        owner=OWNER,
        thread_id="thread-1",
        thread_source=ls.user_dialogue_projection(),
    )
    assert facts.can_learn is False
    assert facts.source_kind == "unknown"


@pytest.mark.parametrize(
    "trace,expected_kind",
    [
        ({"background_delivery_reason": "owner_delivery"}, "background"),
        ({"scheduler_run_id": "run-1"}, "schedule"),
        ({"scheduler_job_id": "job-1"}, "schedule"),
        ({"session_task_id": "task-1"}, "dispatch"),
        ({"session_task_run_id": "run-2"}, "dispatch"),
        ({"session_message_run_id": "run-3"}, "dispatch"),
        ({"wake_signal_id": "wake-1"}, "background"),
        ({"background_wake_signal_id": "wake-2"}, "background"),
    ],
)
def test_facts_for_message_derives_automation_from_host_traces(trace, expected_kind):
    metadata = {**trace, "conversation_request_id": "req-1"}
    facts = ls.facts_for_message(
        metadata,
        owner=OWNER,
        thread_id="thread-1",
        thread_source=ls.user_dialogue_projection(),
    )
    assert facts.can_learn is False
    assert facts.source_kind == expected_kind


def test_projection_to_facts_requires_owner():
    with pytest.raises(AgentsContractError):
        ls.projection_to_facts(ls.user_dialogue_projection(), owner="", thread_id="thread-1")


def test_all_sources_learnable_requires_nonempty_and_all_learnable():
    learnable = ls.projection_to_facts(ls.user_dialogue_projection(), owner=OWNER, thread_id="t")
    unlearnable = ls.projection_to_facts(ls.unknown_projection(), owner=OWNER, thread_id="t")
    assert ls.all_sources_learnable([]) is False
    assert ls.all_sources_learnable([learnable]) is True
    assert ls.all_sources_learnable([learnable, unlearnable]) is False


def test_thread_source_projection_rejects_bad_records():
    assert ls.thread_source_projection(None) is None
    assert ls.thread_source_projection("user_dialogue") is None
    assert ls.thread_source_projection({"source_kind": "user_dialogue"}) is None
    assert ls.thread_source_projection(ls.user_dialogue_projection()) == ls.user_dialogue_projection()


def test_projection_from_wake_metadata_reads_host_written_projection():
    wake = {ls.LEARNING_SOURCE_KEY: ls.schedule_projection()}
    assert ls.projection_from_wake_metadata(wake) == ls.schedule_projection()
    assert ls.projection_from_wake_metadata({}) is None
