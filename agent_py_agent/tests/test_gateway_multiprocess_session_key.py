"""1502：合成顺序键投影，不物化 owner、不读取真实会话。"""
import copy
import hashlib
import json

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_records as records
from agent_py_agent.agent.gateway_parts.multiprocess_session_key import project_admission_session
from agent_py_agent.agent.gateway_parts.request_worker import request_conversation_key
from agent_py_agent.tests.test_gateway_multiprocess_records import (
    admission,
    samples,
    terminal_envelope,
)
from agent_py_agent.tests.test_gateway_multiprocess_records import (
    isolated_home as isolated_home,
)


def expected_scope(user="alice", channel="synthetic", conversation="chat/a"):
    canonical = json.dumps({"schema_version": "multiprocess_session_scope.v1", "user_key": user,
                            "channel": channel, "channel_conversation_id": conversation},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"schema_version": "multiprocess_session_scope.v1",
            "key_sha256": hashlib.sha256(canonical).hexdigest() if conversation else None}


@pytest.mark.parametrize("conversation", ["chat/a", "主题一", "x" * 10000, "has\x1fseparator"])
def test_external_session_domain_has_fixed_canonical_digest(conversation):
    assert project_admission_session("alice", "synthetic", conversation) == expected_scope(conversation=conversation)


def test_projection_keeps_tuple_boundaries_not_joined_separator_aliases():
    assert project_admission_session("alice\x1fb", "c", "d") != project_admission_session("alice", "b\x1fc", "d")
    assert project_admission_session("bob", "synthetic", "chat/a") != expected_scope()
    assert project_admission_session("alice", "http", "chat/a") != expected_scope()


def test_no_conversation_is_explicit_nonserial_scope_not_fabricated_id():
    assert project_admission_session("alice", "synthetic", "") == expected_scope(conversation="")


@pytest.mark.parametrize("values", [(False, "a", "b"), ("a", None, "b"), ("a", "b", []),
                                    ("a", "b", "\ud800")])
def test_invalid_input_is_structured_rejection(values):
    with pytest.raises(records.MultiprocessRecordError):
        project_admission_session(*values)


@pytest.mark.parametrize("conversation", ["chat/a", "主题一", "x" * 129, ""])
def test_admission_v2_roundtrip_and_canonical_terminal_release(conversation):
    value = admission()
    value.update(schema_version="multiprocess_admission_reservation.v2",
                 session_scope=expected_scope(conversation=conversation))
    assert records.deserialize_record(records.serialize_record(value)) == value
    assert records.release_admission(value, terminal_envelope())["state"] == "released"


def test_v2_refuses_legacy_scope_and_unversioned_future_defaults():
    value = admission()
    value["schema_version"] = "multiprocess_admission_reservation.v2"
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record(value)
    value["session_scope"] = {**expected_scope(), "thread_id": "t-1"}
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record(value)


def test_v1_fields_and_golden_remain_unchanged_after_v2_projection():
    snapshots = copy.deepcopy(samples())
    for value in snapshots:
        assert records.deserialize_record(records.serialize_record(value)) == value
    scope = project_admission_session("alice", "synthetic", "chat/a")
    assert scope == expected_scope()
    assert samples() == snapshots


def test_projection_matches_current_sequence_key_without_adding_runtime_interface():
    payload = {"metadata": {"user_id": "fallback"}, "conversation": {
        "channel": "synthetic", "canonical_user_id": "alice", "channel_conversation_id": "chat/a"}}
    before = copy.deepcopy(payload)
    parts = ('alice', 'synthetic', 'chat/a')
    assert request_conversation_key(payload) == "\x1f".join(parts)
    assert project_admission_session(*parts) == expected_scope()
    assert request_conversation_key({}) == ""
    assert payload == before


def test_projection_input_matches_existing_sequence_key_without_thread_materialization(monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_worker

    function = getattr(request_worker, "request_conversation_parts", None)
    assert callable(function)
    payload = {"metadata": {"user_id": "fallback"}, "conversation": {
        "channel": "synthetic", "canonical_user_id": "alice", "channel_conversation_id": "chat/a"}}
    assert function(payload) == ("alice", "synthetic", "chat/a")
    assert request_conversation_key(payload) == "\x1f".join(function(payload))
    assert function({}) == ()
    assert request_conversation_key({}) == ""
