import json
from dataclasses import FrozenInstanceError, asdict, make_dataclass, replace
from hashlib import sha256

import pytest

from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.capability.agents_write_contract import (
    AgentsWriteAuthorization,
    AgentsWriteContext,
    validate_write_authorization,
    write_intent_digest,
)


@pytest.fixture
def authorization():
    return AgentsWriteAuthorization(
        1, "nonce-1", "local/alice", "local/alice", "thread-1", "turn-1", "request-1", "write",
        ("local/alice", "owner_default", "/private/owner/AGENTS.md", 1, 0), "a" * 64,
        100.0, 200.0, "host-user-command-1", "", "b" * 64,
    )


@pytest.fixture
def context(authorization):
    return AgentsWriteContext(
        1, authorization.owner, authorization.actor, authorization.thread_id, authorization.turn_id,
        authorization.request_id, authorization.action, authorization.target_cas,
        authorization.content_sha256, 150.0, "user_dialogue", authorization, (), False, False, "",
        authorization.intent_sha256,
    )


def test_only_matching_host_record_grants_own_write(authorization, context):
    assert validate_write_authorization(authorization, context) is authorization


def test_authorization_json_round_trip_is_metadata_not_authority(authorization, context):
    encoded = authorization.to_dict()
    assert type(encoded) is dict
    assert encoded["target_cas"] == list(authorization.target_cas)
    parsed = AgentsWriteAuthorization.from_dict(json.loads(json.dumps(encoded)))
    assert parsed == authorization
    assert validate_write_authorization(parsed, context) is parsed
    with pytest.raises(AgentsContractError):
        validate_write_authorization(parsed, replace(context, host_authorization=None))


@pytest.mark.parametrize("claim", [{"role": "user"}, {"user_explicit": True},
                                  {"model_says_authorized": True}, None, "用户允许了"])
def test_role_or_model_claim_is_not_authorization(context, claim):
    with pytest.raises(AgentsContractError):
        validate_write_authorization(claim, context)


@pytest.mark.parametrize("kind", ["subagent", "dispatch", "schedule", "background", "unknown"])
def test_non_user_trigger_cannot_inherit_authorization(authorization, context, kind):
    with pytest.raises(AgentsContractError) as error:
        validate_write_authorization(authorization, replace(context, origin_kind=kind))
    assert error.value.code == "AGENTS_WRITE_NOT_USER_REQUEST"


def test_complete_forged_record_does_not_match_host(authorization, context):
    with pytest.raises(AgentsContractError) as error:
        validate_write_authorization(replace(authorization, authorization_id="fake"), context)
    assert error.value.code == "AGENTS_WRITE_NOT_HOST_ISSUED"


def test_spent_nonce_replay_is_rejected(authorization, context):
    with pytest.raises(AgentsContractError) as error:
        validate_write_authorization(authorization, replace(context, consumed_authorization_ids=("nonce-1",)))
    assert error.value.code == "AGENTS_WRITE_REPLAYED"


@pytest.mark.parametrize("field,value", [
    ("owner", "local/bob"), ("actor", "local/bob"), ("thread_id", "thread-2"),
    ("turn_id", "turn-2"), ("request_id", "old-request"), ("action", "set_source"),
    ("target_cas", ("local/alice", "owner_default", "/private/owner/AGENTS.md", 1, 1)),
    ("content_sha256", "b" * 64),
])
def test_current_binding_cannot_drift(authorization, context, field, value):
    with pytest.raises(AgentsContractError):
        validate_write_authorization(authorization, replace(context, **{field: value}))


@pytest.mark.parametrize("now", [99.0, 200.0, 201.0])
def test_time_window_is_issued_inclusive_expires_exclusive(authorization, context, now):
    with pytest.raises(AgentsContractError) as error:
        validate_write_authorization(authorization, replace(context, now=now))
    assert error.value.code == "AGENTS_WRITE_EXPIRED"


def test_issued_boundary_is_accepted(authorization, context):
    assert validate_write_authorization(authorization, replace(context, now=100.0)) is authorization


@pytest.mark.parametrize("field", list(AgentsWriteAuthorization.__dataclass_fields__))
def test_json_missing_any_field_fails(authorization, field):
    payload = asdict(authorization)
    payload["target_cas"] = list(authorization.target_cas)
    del payload[field]
    with pytest.raises(AgentsContractError) as error:
        AgentsWriteAuthorization.from_dict(payload)
    assert error.value.code == "AGENTS_CONTRACT_FIELD_MISSING"


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True), ("owner", ""), ("actor", []),
    ("request_id", "\nprivate"), ("issued_at", True), ("expires_at", float("inf")),
    ("expires_at", 100.0), ("host_request_ref", ""), ("authorization_id", ""),
    ("target_cas", ("local/alice", "owner_default", "/private/owner/AGENTS.md", True, False)),
    ("target_cas", ["local/alice", "owner_default", "/private/owner/AGENTS.md", 1, 0]),
    ("content_sha256", "A" * 64), ("action", "unknown"),
])
def test_bad_native_authorization_fails_closed(authorization, context, field, value):
    with pytest.raises(AgentsContractError):
        validate_write_authorization(replace(authorization, **{field: value}), context)


@pytest.mark.parametrize("payload", [None, [], {"role": "user"}])
def test_bad_json_is_structured(payload):
    with pytest.raises(AgentsContractError):
        AgentsWriteAuthorization.from_dict(payload)


def test_extra_json_authority_flag_is_rejected(authorization):
    payload = asdict(authorization)
    payload["target_cas"] = list(authorization.target_cas)
    payload["role"] = "user"
    with pytest.raises(AgentsContractError) as error:
        AgentsWriteAuthorization.from_dict(payload)
    assert error.value.code == "AGENTS_CONTRACT_FIELD_UNKNOWN"


def test_both_records_are_immutable(authorization, context):
    with pytest.raises(FrozenInstanceError):
        authorization.owner = "local/bob"
    with pytest.raises(FrozenInstanceError):
        context.owner = "local/bob"
    assert validate_write_authorization(authorization, context) is authorization


@pytest.mark.parametrize("grant", ["", "local/bob", "local/alice"])
def test_full_admin_without_exact_explicit_target_is_denied(authorization, context, grant):
    foreign = replace(authorization, actor="local/admin", admin_target_owner=grant)
    host = replace(context, actor=foreign.actor, actor_is_admin=True, full_access=True,
                   admin_target_owner="", host_authorization=foreign)
    with pytest.raises(AgentsContractError) as error:
        validate_write_authorization(foreign, host)
    assert error.value.code == "AGENTS_WRITE_ADMIN_TARGET_REQUIRED"


def test_admin_cross_owner_needs_both_host_and_record_explicit_target(authorization, context):
    foreign = replace(authorization, actor="local/admin", admin_target_owner=authorization.owner)
    host = replace(context, actor=foreign.actor, actor_is_admin=True, full_access=True,
                   admin_target_owner=authorization.owner, host_authorization=foreign)
    assert validate_write_authorization(foreign, host) is foreign
    with pytest.raises(AgentsContractError):
        validate_write_authorization(foreign, replace(host, full_access=False))
    with pytest.raises(AgentsContractError):
        validate_write_authorization(foreign, replace(host, actor_is_admin=False))


def test_json_cannot_supply_runtime_context(authorization, context):
    with pytest.raises(AgentsContractError):
        validate_write_authorization(authorization, asdict(context))


@pytest.mark.parametrize("field,value", [
    ("now", float("nan")), ("actor_is_admin", 1), ("full_access", "true"),
    ("consumed_authorization_ids", ["nonce-1"]), ("host_authorization", {"role": "user"}),
])
def test_invalid_runtime_context_fails_closed(authorization, context, field, value):
    with pytest.raises(AgentsContractError):
        validate_write_authorization(authorization, replace(context, **{field: value}))


def test_set_source_can_recover_unavailable_source_without_content(authorization, context):
    recovery = replace(authorization, action="set_source", content_sha256="")
    host = replace(context, action="set_source", content_sha256="", host_authorization=recovery)
    assert validate_write_authorization(recovery, host) is recovery


def test_network_source_is_read_only_even_with_matching_user_request(authorization, context):
    cas = (authorization.owner, "url", "https://example.invalid/AGENTS.md", 1, 0)
    remote = replace(authorization, target_cas=cas)
    host = replace(context, target_cas=cas, host_authorization=remote)
    with pytest.raises(AgentsContractError) as error:
        validate_write_authorization(remote, host)
    assert error.value.code == "AGENTS_WRITE_SOURCE_READ_ONLY"


@pytest.mark.parametrize("action", ["write", "set_source"])
def test_proposed_operation_digest_is_bound_not_only_old_cas(authorization, context, action):
    issued = replace(authorization, action=action)
    host = replace(context, action=action, host_authorization=issued, intent_sha256="c" * 64)
    with pytest.raises(AgentsContractError) as error:
        validate_write_authorization(issued, host)
    assert error.value.code == "AGENTS_WRITE_BINDING_MISMATCH"


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64])
def test_bad_intent_digest_is_invalid_even_in_matching_host_record(authorization, context, digest):
    issued = replace(authorization, intent_sha256=digest)
    host = replace(context, host_authorization=issued, intent_sha256=digest)
    with pytest.raises(AgentsContractError) as error:
        validate_write_authorization(issued, host)
    assert error.value.code == "AGENTS_CONTRACT_FIELD_INVALID"
    assert error.value.field == "intent_sha256"


@pytest.mark.parametrize("content", ["", "规则\r\n第二行\r", "新规则\n"])
def test_write_intent_hashes_exact_utf8_content(content):
    assert write_intent_digest("write", content) == sha256(content.encode("utf-8")).hexdigest()


def test_set_source_intent_hash_has_canonical_json_recipe():
    value = {"locator": "/private/owner/规则.md", "kind": "local_path"}
    encoded = '{"kind":"local_path","locator":"/private/owner/规则.md"}'.encode()
    assert write_intent_digest("set_source", value) == sha256(encoded).hexdigest()
    assert write_intent_digest("set_source", dict(reversed(list(value.items())))) == sha256(encoded).hexdigest()


@pytest.mark.parametrize("action,value", [
    ("unknown", "rules"), ("write", None), ("write", "\ud800"), ("set_source", {}),
    ("set_source", {"kind": "url", "locator": "https://example.invalid/AGENTS.md", "role": "user"}),
    ("set_source", {"kind": "url", "locator": "https://example.invalid/%GG"}),
    ("set_source", {"kind": "unknown", "locator": "/private/owner/AGENTS.md"}),
])
def test_malformed_operation_intent_is_structured(action, value):
    with pytest.raises(AgentsContractError):
        write_intent_digest(action, value)


@pytest.mark.parametrize("extra", [False, True])
def test_authorization_subclass_cannot_emit_noncontract_json(authorization, extra):
    fields = [("extra", str)] if extra else []
    cls = make_dataclass("ExtendedAuthorization", fields, bases=(type(authorization),), frozen=True, slots=True)
    values = asdict(authorization) | ({"extra": "value"} if extra else {})
    with pytest.raises(AgentsContractError) as error:
        cls(**values).to_dict()
    assert error.value.code == "AGENTS_CONTRACT_OBJECT_REQUIRED"


def test_source_intent_requires_native_string_keys():
    key = type("AliasKey", (str,), {})("kind")
    with pytest.raises(AgentsContractError) as error:
        write_intent_digest("set_source", {key: "local_path", "locator": "/fixture/AGENTS.md"})
    assert error.value.code == "AGENTS_CONTRACT_FIELD_INVALID"


def test_source_intent_hashes_captured_value_not_changed_caller_dict(monkeypatch):
    from agent_py_agent.agent.capability import agents_write_contract as contract

    value = {"kind": "local_path", "locator": "/fixture/AGENTS.md"}
    expected = sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def change_caller(kind, locator):
        value["locator"] = "/other/AGENTS.md"

    monkeypatch.setattr(contract, "_validate_locator", change_caller)
    assert write_intent_digest("set_source", value) == expected
    assert value["locator"] == "/other/AGENTS.md"
