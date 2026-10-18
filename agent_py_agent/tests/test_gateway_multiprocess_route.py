"""F3-a 纯路由合同；只用合成请求，不启动 C/W、服务、模型或真实 Home。"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.gateway_parts import multiprocess_route as routing
from agent_py_agent.agent.gateway_parts.io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    gateway_request_fingerprint,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import (
    REQUEST_OWNER_KEY,
    frozen_request_owner,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    target = (tmp_path / "explicit-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    assert home_paths(str(target)).root == target
    assert target != (Path.home() / ".my-agent").resolve()
    yield target
    assert not target.exists()


def _request(owner=None, *, actor_id="synthetic-a"):
    owner = owner or OwnerIdentity.provider_user("feishu", "synthetic-a")
    result = {"id": "synthetic-request", "kind": "ask", "prompt": "合成请求", REQUEST_OWNER_KEY: asdict(owner),
              "metadata": {"provider": "feishu", "channel": "feishu", "user_id": actor_id, "chat_id": "chat-a",
                           "gateway_admin_binding_version": "binding-v1"}}
    if owner.owner_kind == "group":
        result["metadata"].update(chat_id=owner.owner_id, channel_chat_id=owner.owner_id, channel_chat_type="group")
    result["request_fingerprint"] = gateway_request_fingerprint(result, result["id"])
    result["request_fingerprint_schema"] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    return result


def _stamp(owner=None, *, actor_id="synthetic-a"):
    request = _request(owner, actor_id=actor_id)
    return routing.freeze_request_route(request, frozen_request_owner(request),
                                        routing.RouteStamp(2, "c-generation", 100.0, "binding-v1"))


def _partition(request):
    return routing.WorkerPartition(request["route"]["worker"], 2, "c-generation")


def _rejected(result):
    assert result["ok"] is False
    assert result["error_code"] == "OWNER_ROUTE_MISMATCH"
    assert set(result) == {"ok", "error_code", "reason"}


def test_canonical_owner_encoding_and_stable_mapping():
    owner = OwnerIdentity.provider_user("feishu", "用户甲")
    encoded = json.dumps(["feishu", "user", "用户甲"], ensure_ascii=False, separators=(",", ":"))
    expected = hashlib.sha256(encoded.encode()).hexdigest()
    assert routing.owner_digest(owner) == expected
    assert routing.owner_worker(owner, 2) == int(expected, 16) % 2
    assert routing.owner_worker(OwnerIdentity.local_main(), 2) == "C"


def test_stamp_binds_original_host_owner_and_preserves_input():
    original = _request()
    before = copy.deepcopy(original)
    result = routing.freeze_request_route(original, frozen_request_owner(original),
                                          routing.RouteStamp(2, "c-generation", 100.0, "binding-v1"))
    assert original == before
    assert result is not original and result["metadata"] is not original["metadata"]
    assert "gateway_route_owner" not in result["metadata"]
    assert result[REQUEST_OWNER_KEY] == before[REQUEST_OWNER_KEY]
    assert result["route"]["owner"] == ["feishu", "user", "synthetic-a"]
    assert result["request_fingerprint"] == gateway_request_fingerprint(result, result["id"])
    expected = {"request_id": result["id"], "request_fingerprint": result["request_fingerprint"],
                "ingress_identity": {"provider": "feishu", "channel": "feishu", "user_id": "synthetic-a",
                                     "chat_id": "chat-a", "admin_binding_version": "binding-v1"},
                "owner": ["feishu", "user", "synthetic-a"]}
    canonical = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert result["route"]["binding"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert routing.validate_worker_route(result, _partition(result)) == {
        "ok": True, "owner": ["feishu", "user", "synthetic-a"]}


@pytest.mark.parametrize("owner", [OwnerIdentity.provider_user("local", "fixture-alice"),
    OwnerIdentity.provider_group("local", "fixture-room"), OwnerIdentity.provider_user("feishu", "fixture-alice"),
    OwnerIdentity.provider_group("feishu", "fixture-room"), OwnerIdentity.local_main()])
def test_canonical_host_owner_roundtrip_and_c_worker_separation(owner):
    request = _stamp(owner)
    if owner == OwnerIdentity.local_main():
        assert routing.validate_coordinator_route(request, 2, "c-generation")["ok"] is True
        _rejected(routing.validate_worker_route(request, routing.WorkerPartition(0, 2, "c-generation")))
    else:
        assert routing.validate_worker_route(request, _partition(request))["ok"] is True
        _rejected(routing.validate_coordinator_route(request, 2, "c-generation"))
    assert routing.validate_published_route(request, 2, "c-generation")["ok"] is True


@pytest.mark.parametrize("raw", [None, [], "untrusted", {},
    {"provider": "local", "owner_kind": "main", "owner_id": "other"},
    {"provider": "feishu", "owner_kind": "main", "owner_id": "main"},
    {"provider": "local", "owner_kind": "user", "owner_id": "../main"},
    {"provider": "feishu", "owner_kind": "user", "owner_id": ""}])
def test_missing_or_corrupt_host_owner_rejected_by_freeze_and_validate(raw):
    stamped = _stamp()
    stamped[REQUEST_OWNER_KEY] = raw
    _rejected(routing.validate_worker_route(stamped, _partition(stamped)))
    original = _request()
    original[REQUEST_OWNER_KEY] = raw
    before = copy.deepcopy(original)
    with pytest.raises(routing.OwnerRouteMismatchError) as exc:
        routing.freeze_request_route(original, OwnerIdentity.provider_user("feishu", "synthetic-a"),
                                     routing.RouteStamp(2, "c-generation", 100.0, "binding-v1"))
    _rejected(exc.value.to_payload())
    assert original == before


def test_valid_other_owner_rejected_by_freeze_and_validate():
    other = OwnerIdentity.provider_user("feishu", "other-owner")
    original = _request()
    with pytest.raises(routing.OwnerRouteMismatchError, match="request_owner"):
        routing.freeze_request_route(original, other, routing.RouteStamp(2, "c-generation", 100.0, "binding-v1"))
    stamped = _stamp()
    stamped[REQUEST_OWNER_KEY] = asdict(other)
    _rejected(routing.validate_worker_route(stamped, _partition(stamped)))


def test_published_request_cannot_be_restamped_to_bless_changed_host_owner():
    request = _stamp()
    other = OwnerIdentity.provider_user("feishu", "other-owner")
    request[REQUEST_OWNER_KEY] = asdict(other)
    before = copy.deepcopy(request)
    with pytest.raises(routing.OwnerRouteMismatchError, match="already_published_route"):
        routing.freeze_request_route(request, other, routing.RouteStamp(2, "new-generation", 200.0, "binding-v2"))
    assert request == before
    _rejected(routing.validate_worker_route(request, _partition(request)))


@pytest.mark.parametrize("fault", ["digest", "worker", "old-run", "missing", "binding", "prompt", "identity",
    "owner", "fingerprint-schema", "fingerprint", "input-media", "client-capability", "legacy-metadata-owner"])
def test_invalid_route_or_original_request_fact_is_rejected(fault):
    request = _stamp()
    partition = _partition(request)
    route = request["route"]
    mutations = {"digest": lambda: route.update(owner_sha="0" * 64),
                 "worker": lambda: route.update(worker=1 - partition.index),
                 "old-run": lambda: route.update(c_run_id="old"), "missing": lambda: route.pop("stamped_at"),
                 "binding": lambda: route.update(binding="0" * 64), "prompt": lambda: request.update(prompt="另一请求"),
                 "identity": lambda: request["metadata"].update(chat_id="another-chat"),
                 "owner": lambda: route.update(owner=["local", "main", "main"]),
                 "fingerprint-schema": lambda: request.update(request_fingerprint_schema="unsupported.v2"),
                 "fingerprint": lambda: request.update(request_fingerprint="0" * 64),
                 "input-media": lambda: request.update(input_media=[{"ref_id": "media-b"}]),
                 "client-capability": lambda: request.update(client_capabilities={"input_media": True}),
                 "legacy-metadata-owner": lambda: request["metadata"].update(gateway_route_owner=["local", "main", "main"])}
    mutations[fault]()
    _rejected(routing.validate_worker_route(request, partition))
    _rejected(routing.validate_published_route(request, 2, "c-generation"))


@pytest.mark.parametrize("fault", ["metadata", "prompt", "media", "schema"])
def test_freeze_does_not_resign_a_tampered_original_fingerprint(fault):
    request = _request()
    if fault == "metadata":
        request["metadata"]["user_id"] = "other-actor"
    elif fault == "prompt":
        request["prompt"] = "changed"
    elif fault == "media":
        request["input_media"] = [{"ref_id": "media-b"}]
    else:
        request["request_fingerprint_schema"] = "unsupported.v2"
    with pytest.raises(routing.OwnerRouteMismatchError) as exc:
        routing.freeze_request_route(request, frozen_request_owner(request),
                                     routing.RouteStamp(2, "c-generation", 100.0, "binding-v1"))
    _rejected(exc.value.to_payload())


def test_self_consistent_other_owner_in_same_partition_cannot_replace_original_route():
    request = _stamp()
    partition = _partition(request)
    candidates = [OwnerIdentity.provider_user("feishu", f"other-{i}") for i in range(20)]
    other = next(owner for owner in candidates if routing.owner_worker(owner, 2) == partition.index)
    wrong = _stamp(other)
    request["route"] = wrong["route"]
    assert routing.owner_worker(other, 2) == partition.index
    result = routing.validate_worker_route(request, partition)
    _rejected(result)
    assert result["reason"] == "request_owner"


def test_legacy_metadata_owner_cannot_override_original_host_owner():
    request = _request()
    request["metadata"]["gateway_route_owner"] = ["local", "main", "main"]
    request["request_fingerprint"] = gateway_request_fingerprint(request, request["id"])
    result = routing.freeze_request_route(request, frozen_request_owner(request),
                                          routing.RouteStamp(2, "c-generation", 100.0, "binding-v1"))
    assert result["route"]["owner"] == ["feishu", "user", "synthetic-a"]
    assert routing.validate_worker_route(result, _partition(result))["ok"] is True


def test_same_group_owner_new_typed_actor_uses_original_main_fingerprint():
    owner = OwnerIdentity.provider_group("feishu", "synthetic-group")
    actor = HostChannelActor("feishu:group:synthetic-group", "thread-a", "synthetic-request",
                             "feishu", "synthetic-group", "actor-a")
    first = _stamp(owner, actor_id=actor.actor_id)
    other = HostChannelActor.from_record({**actor.to_record(), "actor_id": "actor-b"})
    changed = copy.deepcopy(first)
    changed["metadata"]["user_id"] = other.actor_id
    assert frozen_request_owner(first) == frozen_request_owner(changed) == owner
    assert gateway_request_fingerprint(changed, changed["id"]) != first["request_fingerprint"]
    result = routing.validate_worker_route(changed, _partition(first))
    assert result == {"ok": False, "error_code": "OWNER_ROUTE_MISMATCH", "reason": "request_fingerprint"}
    newly_authenticated = _stamp(owner, actor_id=other.actor_id)
    assert routing.validate_worker_route(newly_authenticated, _partition(newly_authenticated))["ok"] is True
    assert newly_authenticated["route"]["binding"] != first["route"]["binding"]
    assert newly_authenticated["request_fingerprint"] != first["request_fingerprint"]


@pytest.mark.parametrize("bad", [None, [], "text", {}, {"owner": []}])
def test_nonrecord_routes_are_rejected(bad):
    request = _stamp()
    partition = _partition(request)
    request["route"] = bad
    _rejected(routing.validate_worker_route(request, partition))
    _rejected(routing.validate_published_route(request, 2, "c-generation"))


@pytest.mark.parametrize("count", [0, -1, True, 2.0, "2"])
def test_invalid_count_cannot_stamp_or_validate(count):
    request = _request()
    with pytest.raises(routing.OwnerRouteMismatchError):
        routing.freeze_request_route(request, frozen_request_owner(request),
                                     routing.RouteStamp(count, "c-generation", 100.0, "binding-v1"))
    _rejected(routing.validate_worker_route(_stamp(), routing.WorkerPartition(0, count, "c-generation")))
    with pytest.raises(ValueError):
        routing.owner_worker(frozen_request_owner(request), count)


@pytest.mark.parametrize("stamp", [True, -1, float("nan"), float("inf"), 10 ** 1000])
def test_bad_timestamp_rejected_in_both_directions(stamp):
    request = _request()
    with pytest.raises(routing.OwnerRouteMismatchError):
        routing.freeze_request_route(request, frozen_request_owner(request),
                                     routing.RouteStamp(2, "c-generation", stamp, "binding-v1"))
    stamped = _stamp()
    stamped["route"]["stamped_at"] = stamp
    _rejected(routing.validate_worker_route(stamped, _partition(stamped)))


@pytest.mark.parametrize("partition", [routing.WorkerPartition(True, 2, "c-generation"),
    routing.WorkerPartition(-1, 2, "c-generation"), routing.WorkerPartition(2, 2, "c-generation"),
    routing.WorkerPartition("C", 2, "c-generation"), routing.WorkerPartition(0, 2, ""),
    routing.WorkerPartition(0, 2, 42)])
def test_worker_requires_original_typed_partition(partition):
    _rejected(routing.validate_worker_route(_stamp(), partition))


def test_mutable_processing_facts_do_not_change_immutable_route():
    request = _stamp()
    request.update(status="processing", lease_epoch=99, cancel_requested=True, worker_id="mutable-worker")
    before = copy.deepcopy(request)
    assert routing.validate_worker_route(request, _partition(request))["ok"] is True
    assert request == before


def test_old_generation_also_rejected_for_local_coordinator():
    request = _stamp(OwnerIdentity.local_main())
    assert routing.validate_coordinator_route(request, 2, "new-generation") == {
        "ok": False, "error_code": "OWNER_ROUTE_MISMATCH", "reason": "old_generation"}
