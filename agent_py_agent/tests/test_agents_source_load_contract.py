import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.capability.agents_source_contract import AgentsSourceSnapshot
from agent_py_agent.agent.capability.agents_source_load_contract import (
    AgentsSourceLoadResult,
    validate_source_load,
)


@pytest.fixture
def loaded():
    content = "先核对证据。\r\n不越权。\r"
    snapshot = AgentsSourceSnapshot(1, "local/alice", "local_path", "/fixture/alice/rules.md", 1, 2,
        hashlib.sha256(content.encode("utf-8")).hexdigest(), 1000.0, "cache-1", "ready", "", False, False)
    return AgentsSourceLoadResult(snapshot, content)


def test_loaded_text_roundtrips_without_newline_normalization(loaded):
    assert validate_source_load(loaded, "local/alice") is loaded
    payload = json.loads(json.dumps(loaded.to_dict()))
    assert AgentsSourceLoadResult.from_dict(payload, "local/alice") == loaded
    assert payload["content"] == loaded.content


def test_empty_text_is_content_not_unavailable(loaded):
    snapshot = replace(loaded.snapshot, content_sha256=hashlib.sha256(b"").hexdigest())
    value = replace(loaded, snapshot=snapshot, content="")
    assert validate_source_load(value, "local/alice") is value


def test_unavailable_result_has_no_content(loaded):
    snapshot = replace(loaded.snapshot, status="unavailable", content_sha256="", fetched_at=None,
        last_good_ref="", failure_code="FETCH_TIMEOUT")
    value = replace(loaded, snapshot=snapshot, content=None)
    assert validate_source_load(value, "local/alice") is value
    with pytest.raises(AgentsContractError):
        validate_source_load(replace(value, content=""), "local/alice")


@pytest.mark.parametrize("content", [None, b"binary", [], "tampered", "\ud800"])
def test_missing_invalid_or_tampered_content_is_rejected(loaded, content):
    with pytest.raises(AgentsContractError) as error:
        validate_source_load(replace(loaded, content=content), "local/alice")
    assert error.value.code.startswith("AGENTS_")


def test_foreign_result_and_changed_cas_are_rejected(loaded):
    with pytest.raises(AgentsContractError) as error:
        validate_source_load(loaded, "local/bob")
    assert error.value.code == "AGENTS_SOURCE_OWNER_MISMATCH"
    with pytest.raises(AgentsContractError) as error:
        validate_source_load(loaded, "local/alice", (*loaded.snapshot.configuration_cas[:-1], 9))
    assert error.value.code == "AGENTS_SOURCE_CONFIG_CONFLICT"


def test_result_is_readonly_and_payload_is_independent(loaded):
    with pytest.raises(FrozenInstanceError):
        loaded.content = "changed"
    data = loaded.to_dict()
    assert isinstance(data, dict)
    data["snapshot"]["owner"] = "local/bob"
    assert loaded.snapshot.owner == "local/alice"


@pytest.mark.parametrize("payload", [None, {}, {"snapshot": {}, "content": ""},
    {"snapshot": {}, "content": "", "role": "user"}])
def test_bad_serialized_result_fails_closed(payload):
    with pytest.raises(AgentsContractError):
        AgentsSourceLoadResult.from_dict(payload, "local/alice")


@pytest.mark.parametrize("snapshot", [None, {}, "foreign"])
def test_invalid_snapshot_serialization_is_a_contract_error(snapshot):
    with pytest.raises(AgentsContractError):
        AgentsSourceLoadResult(snapshot, "").to_dict()
