import json
from dataclasses import FrozenInstanceError, asdict, make_dataclass, replace

import pytest

from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.capability.agents_source_contract import (
    AgentsSourceSnapshot,
    validate_source_snapshot,
)


@pytest.fixture
def snapshot():
    return AgentsSourceSnapshot(
        schema_version=1, owner="local/alice", kind="owner_default",
        locator="/fixture/alice/AGENTS.md", config_version=1, revision=0,
        content_sha256="a" * 64, fetched_at=1000.0, last_good_ref="cache-1",
        status="ready", failure_code="", used_cache=False, stale=False,
    )


@pytest.mark.parametrize("kind,locator", [
    ("owner_default", "/fixture/alice/AGENTS.md"),
    ("local_path", "/fixture/alice/rules.md"),
    ("local_path", r"C:\fixture\alice\rules.md"),
    ("url", "https://example.invalid/rules.md"),
])
def test_legal_source_is_validated_and_roundtrips(snapshot, kind, locator):
    value = replace(snapshot, kind=kind, locator=locator)
    assert validate_source_snapshot(value, "local/alice") == value
    assert AgentsSourceSnapshot.from_dict(json.loads(json.dumps(value.to_dict())), "local/alice") == value


def test_snapshot_is_readonly_and_serialization_does_not_alias(snapshot):
    with pytest.raises(FrozenInstanceError):
        snapshot.owner = "local/bob"
    data = snapshot.to_dict()
    assert data.get("owner") == "local/alice"
    data["owner"] = "local/bob"
    assert snapshot.owner == "local/alice"


@pytest.mark.parametrize("field", [
    "schema_version", "owner", "kind", "locator", "config_version", "revision",
    "content_sha256", "fetched_at", "last_good_ref", "status", "failure_code", "used_cache", "stale",
])
def test_missing_fields_fail_closed(snapshot, field):
    data = snapshot.to_dict()
    data.pop(field, None)
    with pytest.raises(AgentsContractError) as error:
        AgentsSourceSnapshot.from_dict(data, "local/alice")
    assert error.value.code == "AGENTS_CONTRACT_FIELD_MISSING"
    assert error.value.field == field


def test_foreign_snapshot_is_rejected_before_use(snapshot):
    with pytest.raises(AgentsContractError) as error:
        validate_source_snapshot(snapshot, "local/bob")
    assert error.value.code == "AGENTS_SOURCE_OWNER_MISMATCH"


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("revision", -1), ("revision", True),
    ("config_version", 0), ("config_version", "1"), ("owner", ""),
    ("content_sha256", "short"), ("content_sha256", "A" * 64),
    ("fetched_at", float("nan")), ("fetched_at", float("inf")),
    ("fetched_at", -1.0), ("used_cache", "false"), ("stale", 1),
    ("status", "unknown"), ("kind", "unknown"),
    ("locator", "relative/AGENTS.md"), ("locator", "/fixture/../other.md"),
])
def test_invalid_scalar_or_source_fails_closed(snapshot, field, value):
    data = snapshot.to_dict()
    data[field] = value
    with pytest.raises(AgentsContractError) as error:
        AgentsSourceSnapshot.from_dict(data, "local/alice")
    assert error.value.code.startswith("AGENTS_")
    assert error.value.field == field


@pytest.mark.parametrize("locator", [
    "http://example.invalid/rules.md", "https://user@example.invalid/rules.md",
    "https://example.invalid/rules.md#fragment", "https:///rules.md",
])
def test_noncanonical_or_nonhttps_url_is_rejected(snapshot, locator):
    with pytest.raises(AgentsContractError) as error:
        validate_source_snapshot(replace(snapshot, kind="url", locator=locator), "local/alice")
    assert error.value.field == "locator"


def test_cached_failure_retains_digest_and_cas(snapshot):
    cached = replace(snapshot, status="cached", failure_code="FETCH_TIMEOUT", used_cache=True, stale=True)
    assert validate_source_snapshot(cached, "local/alice") == cached
    assert cached.prefix_digest == snapshot.content_sha256
    assert cached.configuration_cas == snapshot.configuration_cas
    assert replace(snapshot, fetched_at=2000.0, revision=1).prefix_digest == snapshot.prefix_digest
    assert snapshot.configuration_cas == ("local/alice", "owner_default", snapshot.locator, 1, 0)


@pytest.mark.parametrize("changes", [
    {"failure_code": "TIMEOUT"}, {"used_cache": True}, {"stale": True},
    {"status": "cached"},
    {"status": "cached", "failure_code": "TIMEOUT", "used_cache": True, "stale": True, "last_good_ref": ""},
    {"status": "unavailable", "failure_code": "TIMEOUT"},
])
def test_inconsistent_failure_metadata_is_rejected(snapshot, changes):
    with pytest.raises(AgentsContractError):
        validate_source_snapshot(replace(snapshot, **changes), "local/alice")


def test_first_failure_without_cache_has_no_prefix(snapshot):
    failed = replace(snapshot, status="unavailable", content_sha256="", fetched_at=None,
                     last_good_ref="", failure_code="FETCH_TIMEOUT")
    assert validate_source_snapshot(failed, "local/alice") == failed
    with pytest.raises(AgentsContractError) as error:
        _ = failed.prefix_digest
    assert error.value.code == "AGENTS_SOURCE_UNAVAILABLE"


def test_changed_config_cas_rejects_old_snapshot(snapshot):
    expected = (snapshot.owner, snapshot.kind, snapshot.locator, snapshot.config_version, 2)
    with pytest.raises(AgentsContractError) as error:
        validate_source_snapshot(snapshot, "local/alice", expected)
    assert error.value.code == "AGENTS_SOURCE_CONFIG_CONFLICT"


@pytest.mark.parametrize("payload", [None, [], "text"])
def test_nonobject_payload_is_not_coerced(payload):
    with pytest.raises(AgentsContractError) as error:
        AgentsSourceSnapshot.from_dict(payload, "local/alice")
    assert error.value.code == "AGENTS_CONTRACT_OBJECT_REQUIRED"


def test_unknown_payload_fields_are_not_authoritative(snapshot):
    data = snapshot.to_dict() | {"permission": "full"}
    with pytest.raises(AgentsContractError) as error:
        AgentsSourceSnapshot.from_dict(data, "local/alice")
    assert error.value.code == "AGENTS_CONTRACT_FIELD_UNKNOWN"


def test_unrepresentable_timestamp_fails_with_structured_error(snapshot):
    with pytest.raises(AgentsContractError) as error:
        validate_source_snapshot(replace(snapshot, fetched_at=10 ** 1000), "local/alice")
    assert error.value.field == "fetched_at"


@pytest.mark.parametrize("locator", [
    r"https://exa\mple.invalid/AGENTS.md", "https://example.invalid/AGENTS%GG.md",
    "https://example.invalid/rules%", "https://example.invalid/rules%0",
])
def test_malformed_uri_cannot_enter_snapshot_or_cas(snapshot, locator):
    with pytest.raises(AgentsContractError) as error:
        validate_source_snapshot(replace(snapshot, kind="url", locator=locator), "local/alice")
    assert error.value.code == "AGENTS_SOURCE_LOCATOR_INVALID"


@pytest.mark.parametrize("versions", [(True, False), (1.0, 0.0)])
def test_expected_cas_rejects_numerically_equal_nonintegers(snapshot, versions):
    expected = (*snapshot.configuration_cas[:3], *versions)
    with pytest.raises(AgentsContractError) as error:
        validate_source_snapshot(snapshot, "local/alice", expected)
    assert error.value.code == "AGENTS_SOURCE_CAS_INVALID"


@pytest.mark.parametrize("extra", [False, True])
def test_source_subclass_cannot_emit_noncontract_json(snapshot, extra):
    fields = [("extra", str)] if extra else []
    cls = make_dataclass("ExtendedSource", fields, bases=(type(snapshot),), frozen=True, slots=True)
    values = asdict(snapshot) | ({"extra": "value"} if extra else {})
    with pytest.raises(AgentsContractError) as error:
        cls(**values).to_dict()
    assert error.value.code == "AGENTS_CONTRACT_OBJECT_REQUIRED"
