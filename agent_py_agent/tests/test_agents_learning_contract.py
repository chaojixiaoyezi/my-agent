import json
from dataclasses import FrozenInstanceError, asdict, make_dataclass, replace

import pytest

from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.capability.agents_learning_contract import (
    AgentsLearningSourceFacts,
    validate_learning_source,
)


@pytest.fixture
def user_facts():
    return AgentsLearningSourceFacts(
        1, "local/alice", "user_dialogue", "user_dialogue", True, "", (), (),
        "thread-1", "turn-1", "request-1", 1,
    )


@pytest.fixture
def child_facts(user_facts):
    return replace(user_facts, source_kind="subagent", subagent_tag="subagent",
                   source_run_ids=("run-1",), source_task_ids=("task-1",))


def test_user_source_is_eligible(user_facts):
    assert validate_learning_source(user_facts, user_facts.owner) is user_facts
    assert user_facts.can_learn is True


def test_tagged_child_keeps_host_provenance_not_user_explicit(child_facts):
    assert validate_learning_source(child_facts, child_facts.owner) is child_facts
    assert child_facts.source_kind == "subagent"
    assert child_facts.subagent_tag == "subagent"
    assert child_facts.source_run_ids == ("run-1",)
    assert child_facts.source_task_ids == ("task-1",)


def test_explicit_disabled_user_source_stays_disabled(user_facts):
    disabled = replace(user_facts, can_learn=False)
    assert validate_learning_source(disabled, disabled.owner) is disabled
    assert validate_learning_source(disabled, disabled.owner).can_learn is False


@pytest.mark.parametrize("kind", ["dispatch", "schedule", "background", "future_kind", "model"])
def test_automation_and_unknown_remain_unlearnable(user_facts, kind):
    facts = replace(user_facts, source_kind=kind, trigger_kind=kind, can_learn=False)
    assert validate_learning_source(facts, facts.owner) is facts
    assert validate_learning_source(facts, facts.owner).can_learn is False
    with pytest.raises(AgentsContractError) as error:
        validate_learning_source(replace(facts, can_learn=True), facts.owner)
    assert error.value.code == "AGENTS_LEARNING_SOURCE_FORBIDDEN"


@pytest.mark.parametrize("trigger", ["dispatch", "schedule", "background", "future_trigger"])
def test_child_of_automation_cannot_launder_origin(child_facts, trigger):
    with pytest.raises(AgentsContractError):
        validate_learning_source(replace(child_facts, trigger_kind=trigger), child_facts.owner)
    disabled = replace(child_facts, trigger_kind=trigger, can_learn=False)
    assert validate_learning_source(disabled, disabled.owner).can_learn is False


@pytest.mark.parametrize("field,value", [("subagent_tag", ""), ("subagent_tag", "user_explicit"),
                                       ("source_run_ids", ())])
def test_child_without_trustworthy_tag_and_run_is_rejected(child_facts, field, value):
    with pytest.raises(AgentsContractError):
        validate_learning_source(replace(child_facts, **{field: value}), child_facts.owner)


def test_unknown_policy_cannot_enable_learning(user_facts):
    with pytest.raises(AgentsContractError):
        validate_learning_source(replace(user_facts, policy_version=2), user_facts.owner)
    disabled = replace(user_facts, policy_version=2, can_learn=False)
    assert validate_learning_source(disabled, disabled.owner).can_learn is False


def test_fact_and_nested_lineage_are_immutable(child_facts):
    with pytest.raises(FrozenInstanceError):
        child_facts.can_learn = False
    assert type(child_facts.source_run_ids) is tuple
    assert validate_learning_source(child_facts, child_facts.owner) is child_facts


def test_full_json_round_trip_is_independent(child_facts):
    payload = child_facts.to_dict()
    assert type(payload) is dict
    assert payload["source_run_ids"] == ["run-1"]
    assert payload["source_task_ids"] == ["task-1"]
    parsed = AgentsLearningSourceFacts.from_dict(json.loads(json.dumps(payload)), child_facts.owner)
    assert parsed == child_facts
    payload["source_run_ids"].append("other-run")
    assert parsed.source_run_ids == ("run-1",)


def test_cross_owner_is_rejected_even_for_unlearnable_source(user_facts):
    with pytest.raises(AgentsContractError) as error:
        validate_learning_source(replace(user_facts, can_learn=False), "local/bob")
    assert error.value.code == "AGENTS_LEARNING_OWNER_MISMATCH"


@pytest.mark.parametrize("field", list(AgentsLearningSourceFacts.__dataclass_fields__))
def test_missing_json_field_never_defaults_to_user(user_facts, field):
    payload = asdict(user_facts)
    payload["source_run_ids"], payload["source_task_ids"] = [], []
    del payload[field]
    with pytest.raises(AgentsContractError) as error:
        AgentsLearningSourceFacts.from_dict(payload, user_facts.owner)
    assert error.value.code == "AGENTS_CONTRACT_FIELD_MISSING"


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("owner", ""), ("can_learn", 1), ("request_id", ""),
    ("source_kind", ""), ("trigger_kind", ""), ("policy_version", True), ("policy_version", 0),
    ("source_run_ids", ["run-1"]), ("source_run_ids", ("",)),
    ("source_task_ids", ("task-1", "task-1")), ("subagent_tag", "subagent"),
])
def test_invalid_native_facts_fail_closed(user_facts, field, value):
    with pytest.raises(AgentsContractError):
        validate_learning_source(replace(user_facts, **{field: value}), user_facts.owner)


@pytest.mark.parametrize("payload", [None, [], {"role": "user"}])
def test_role_only_json_is_not_learning_fact(payload):
    with pytest.raises(AgentsContractError):
        AgentsLearningSourceFacts.from_dict(payload, "local/alice")


def test_extra_json_role_is_rejected(user_facts):
    payload = asdict(user_facts)
    payload["source_run_ids"], payload["source_task_ids"] = [], []
    payload["role"] = "user"
    with pytest.raises(AgentsContractError) as error:
        AgentsLearningSourceFacts.from_dict(payload, user_facts.owner)
    assert error.value.code == "AGENTS_CONTRACT_FIELD_UNKNOWN"


def test_unknown_source_remains_forbidden_even_under_known_user_trigger(user_facts):
    unknown = replace(user_facts, source_kind="future_source", can_learn=False)
    assert validate_learning_source(unknown, unknown.owner) is unknown
    with pytest.raises(AgentsContractError) as error:
        validate_learning_source(replace(unknown, can_learn=True), unknown.owner)
    assert error.value.code == "AGENTS_LEARNING_SOURCE_FORBIDDEN"


@pytest.mark.parametrize("extra", [False, True])
def test_learning_subclass_cannot_emit_noncontract_json(user_facts, extra):
    fields = [("extra", str)] if extra else []
    cls = make_dataclass("ExtendedLearning", fields, bases=(type(user_facts),), frozen=True, slots=True)
    values = asdict(user_facts) | ({"extra": "value"} if extra else {})
    with pytest.raises(AgentsContractError) as error:
        cls(**values).to_dict()
    assert error.value.code == "AGENTS_CONTRACT_OBJECT_REQUIRED"
