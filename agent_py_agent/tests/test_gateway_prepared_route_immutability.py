"""真实C租约的首次输入冻结：无worker、模型、网络或日常数据写入。"""
from __future__ import annotations

import copy
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
from agent_py_agent.agent.gateway_parts import multiprocess_route as route
from agent_py_agent.agent.gateway_parts.control_plane import (
    GatewayControlPlane,
    prepare_coordinator_input,
)
from agent_py_agent.agent.gateway_parts.io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    gateway_request_fingerprint,
    write_gateway_request_once,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_owner_binding import (
    REQUEST_OWNER_KEY,
    frozen_request_owner,
)
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space import admin_channel_identity as identities
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture
def context(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    assert home != (Path.home() / ".my-agent").resolve()
    paths = gateway_paths_from_root(tmp_path / "gateway")
    config = AgentConfig(my_agent_home=str(home), gateway_per_user_owner_scoping=True,
                         admin_channel_identity_enabled=True)
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "fixture-c")
        assert controller.active()
        control = GatewayControlPlane(config, home, paths, 2, "fixture-c",
                                      agent_root=tmp_path, workspace_roots=(tmp_path,))
        yield control


# LLM: 夹具只模拟认证入口已采集的结构事实；不把metadata里的owner或版本当授权，指纹在所有事实就绪后生成。
# 函数用途: 构造带原文、媒体引用和宿主actor事实的完整合成请求。
def _request():
    request = {"id": "fixture-input", "request_id": "fixture-input", "kind": "ask",
               "goal": "合成正文", "user_id": "fixture-alice",
               "conversation": {"enabled": True, "id": "fixture-thread"},
               "input_media": [{"kind": "image", "ref": "fixture-media-ref"}],
               "metadata": {"channel": "feishu", "user_id": "fixture-alice",
                            "channel_chat_type": "p2p", "client_input_digest": "fixture-digest",
                            "channel_message_id": "fixture-message", "sender_type": "user"}}
    _fingerprint(request)
    return request


def _fingerprint(request):
    request["request_fingerprint"] = gateway_request_fingerprint(request, request["id"])
    request["request_fingerprint_schema"] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA


def _prepare(paths, request=None, *, prepare_request=None):
    with delivery.gateway_input_transition(paths, "fixture-input"):
        return delivery.load_or_prepare_gateway_input_locked(
            paths, request_id="fixture-input", client_input_digest="fixture-digest",
            client_message_id="fixture-message", guidance_dedupe_key="fixture-guidance",
            prepared_request=request or _request(),
            prepare_request=prepare_request,
        )


def test_first_prepare_freezes_owner_and_version_before_complete_digest(context):
    original = _request()
    receipt, created = _prepare(context.paths, original)
    prepared = receipt.prepared_request
    assert created is True and original == _request()
    assert frozen_request_owner(prepared) == OwnerIdentity.provider_user("feishu", "fixture-alice")
    assert prepared["metadata"]["gateway_admin_binding_version"] == "none"
    before = copy.deepcopy(prepared)
    stamped = route.freeze_request_route(prepared, frozen_request_owner(prepared),
                                        route.RouteStamp(2, "fixture-c", 1.0, "none"))
    assert prepared == before
    assert stamped["metadata"] == before["metadata"]
    assert stamped["request_fingerprint"] == before["request_fingerprint"]
    assert delivery._prepared_payload_digest(stamped) == receipt.prepared_payload_digest
    assert stamped["input_media"] == original["input_media"]
    write_json_file_atomic(context.paths.inbox / "fixture-input.json", stamped)
    assert delivery._materialize_prepared_request_locked(context.paths, receipt) is False


@pytest.mark.parametrize("version", [None, "new-binding"])
def test_route_cannot_add_or_replace_version_after_prepared_digest(version):
    request = _request()
    request[REQUEST_OWNER_KEY] = asdict(OwnerIdentity.provider_user("feishu", "fixture-alice"))
    if version is not None:
        request["metadata"]["gateway_admin_binding_version"] = version
    _fingerprint(request)
    before = copy.deepcopy(request)
    with pytest.raises(route.OwnerRouteMismatchError, match="admin_binding_version"):
        route.freeze_request_route(request, frozen_request_owner(request),
                                   route.RouteStamp(2, "fixture-c", 1.0, "none"))
    assert request == before


def test_route_cannot_create_missing_prepared_fingerprint():
    request = _request()
    request[REQUEST_OWNER_KEY] = asdict(OwnerIdentity.provider_user("feishu", "fixture-alice"))
    request["metadata"]["gateway_admin_binding_version"] = "none"
    request.pop("request_fingerprint")
    before = copy.deepcopy(request)
    with pytest.raises(route.OwnerRouteMismatchError, match="request_identity"):
        route.freeze_request_route(request, frozen_request_owner(request),
                                   route.RouteStamp(2, "fixture-c", 1.0, "none"))
    assert request == before


@pytest.mark.parametrize("reserved", ["none", "binding-from-client", None])
def test_first_prepare_rejects_client_reserved_version_even_if_equal(context, reserved):
    request = _request()
    request["metadata"]["gateway_admin_binding_version"] = reserved
    _fingerprint(request)
    with pytest.raises(route.OwnerRouteMismatchError, match="reserved_route_input"):
        _prepare(context.paths, request)
    assert not delivery.gateway_input_receipt_path(context.paths, request["id"]).exists()


def test_same_digest_replay_uses_old_owner_and_version_without_fresh_binding_read(context, monkeypatch):
    identities.bind_admin_channel_identity(context.home_root, "feishu", "fixture-alice", now=1.0)
    first, created = _prepare(context.paths)
    assert created is True
    assert frozen_request_owner(first.prepared_request) == OwnerIdentity.local_main()
    identities.remove_admin_channel_identity(context.home_root, "feishu", "fixture-alice")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("重放不得重新读取管理员绑定")

    monkeypatch.setattr(identities, "read_private_config_json", unexpected)
    second, created = _prepare(context.paths)
    assert created is False and second == first
    with delivery.gateway_input_transition(context.paths, "fixture-input"):
        queued, created = delivery.queue_gateway_input_locked(context.paths, second)
        assert created is True and queued.state == "queued"
    assert delivery.read_gateway_input_receipt(context.paths, "fixture-input").prepared_request == first.prepared_request


def test_first_prepare_owner_and_version_use_the_same_safe_read(context, monkeypatch):
    identities.bind_admin_channel_identity(context.home_root, "feishu", "fixture-alice", now=1.0)
    before = identities.read_admin_channel_identity_snapshot(context.home_root)
    reader = identities.read_private_config_json
    calls = []

    def changed_after_read(*args):
        payload = reader(*args)
        calls.append(payload)
        identities.write_private_config_json(context.home_root, "admin-channel-identities.json",
                                            {"schema": identities.ADMIN_CHANNEL_IDENTITIES_SCHEMA, "identities": []})
        return payload

    monkeypatch.setattr(identities, "read_private_config_json", changed_after_read)
    prepared = context.prepare_input(_request())
    assert len(calls) == 1
    assert frozen_request_owner(prepared) == OwnerIdentity.local_main()
    assert prepared["metadata"]["gateway_admin_binding_version"] == before.version


def test_first_prepare_retains_existing_ingress_owner_conflict_rejection(context):
    request = _request()
    request["metadata"][REQUEST_OWNER_KEY] = asdict(OwnerIdentity.local_main())
    _fingerprint(request)
    with pytest.raises(DataCorruptionError, match="owner"):
        context.prepare_input(request)


def test_same_id_different_digest_conflicts_before_snapshot_read(context, monkeypatch):
    first, _ = _prepare(context.paths)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("冲突输入不得读取绑定或更改旧回执")

    monkeypatch.setattr(identities, "read_private_config_json", unexpected)
    with delivery.gateway_input_transition(context.paths, "fixture-input"):
        with pytest.raises(ValueError, match="different input"):
            delivery.load_or_prepare_gateway_input_locked(
                context.paths, request_id="fixture-input", client_input_digest="other-digest",
                client_message_id="fixture-message", guidance_dedupe_key="fixture-guidance",
                prepared_request=_request(),
            )
    assert delivery.read_gateway_input_receipt(context.paths, "fixture-input") == first


def test_host_readonly_prepare_runs_once_and_never_during_same_digest_replay(context):
    calls = []

    def prepare(payload):
        calls.append(payload[REQUEST_OWNER_KEY])
        return payload

    first, created = _prepare(context.paths, prepare_request=prepare)
    assert created and calls == [first.prepared_request[REQUEST_OWNER_KEY]]

    def unexpected(_payload):
        raise AssertionError("重放不得再次解析配置/偏好/role")

    assert _prepare(context.paths, prepare_request=unexpected) == (first, False)


@pytest.mark.parametrize("field", ["owner", "metadata", "media", "fingerprint"])
def test_host_prepare_cannot_change_protected_input_facts(context, field):
    def changed(payload):
        if field == "owner":
            payload[REQUEST_OWNER_KEY] = asdict(OwnerIdentity.local_main())
        elif field == "metadata":
            payload["metadata"]["sender_type"] = "bot"
        elif field == "media":
            payload["input_media"][0]["ref"] = "other-media-ref"
        else:
            payload.pop("request_fingerprint")
        return payload

    with pytest.raises(route.OwnerRouteMismatchError, match="prepared_input_changed"):
        _prepare(context.paths, prepare_request=changed)
    assert not delivery.gateway_input_receipt_path(context.paths, "fixture-input").exists()


def test_failed_host_prepare_leaves_no_receipt_and_can_retry_original_input(context):
    def cancelled(_payload):
        raise InterruptedError("fixture cancellation")

    with pytest.raises(InterruptedError, match="fixture cancellation"):
        _prepare(context.paths, prepare_request=cancelled)
    assert not delivery.gateway_input_receipt_path(context.paths, "fixture-input").exists()
    receipt, created = _prepare(context.paths)
    assert created and receipt.prepared_request["goal"] == _request()["goal"]


def test_revocation_during_snapshot_cannot_publish_prepared_receipt(context, monkeypatch):
    reader = identities.read_private_config_json
    lease = context._coordinator_role_lease

    def revoked(*args):
        value = reader(*args)
        lease._closing = True
        return value

    monkeypatch.setattr(identities, "read_private_config_json", revoked)
    try:
        with pytest.raises(route.OwnerRouteMismatchError, match="source_changed"):
            _prepare(context.paths)
        assert not delivery.gateway_input_receipt_path(context.paths, "fixture-input").exists()
    finally:
        lease._closing = False


def test_c_replay_without_original_guidance_owner_is_unknown_not_new_owner(context):
    request = _request()
    request["kind"] = "steer_closeout_replay"
    _fingerprint(request)
    with pytest.raises(route.OwnerRouteMismatchError, match="missing_replay_owner"):
        _prepare(context.paths, request)
    assert not delivery.gateway_input_receipt_path(context.paths, "fixture-input").exists()


def test_real_c_admission_projects_each_original_prepared_version_without_hot_read(context, monkeypatch):
    receipt, _ = _prepare(context.paths)
    with delivery.gateway_input_transition(context.paths, "fixture-input"):
        delivery.queue_gateway_input_locked(context.paths, receipt)
    identities.bind_admin_channel_identity(context.home_root, "feishu", "fixture-alice", now=1.0)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("admission不得重读或重签已prepared身份")

    monkeypatch.setattr(identities, "read_private_config_json", unexpected)
    gate = admission.coordinator_admission_gate(
        None, context.paths, SimpleNamespace(coordinator_worker_count=2),
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime

    GatewayDrainRuntime(context).retain_admission(gate)
    owner = frozen_request_owner(receipt.prepared_request)
    ownership = owner, admission.session_scope_for_request(receipt.prepared_request)
    result = gate.admit(context.paths.inbox / "fixture-input.json", ownership,
                        AdmissionLimits(user_inflight=8, global_inflight=8))
    assert result["status"] == "admitted"
    stamped = result["request"]
    assert stamped["metadata"] == receipt.prepared_request["metadata"]
    assert stamped["request_fingerprint"] == receipt.prepared_request["request_fingerprint"]
    assert delivery._prepared_payload_digest(stamped) == receipt.prepared_payload_digest
    assert gate.occupied_snapshot()["total"] == 1
    result = gate.admit(context.paths.inbox / "fixture-input.json", ownership,
                        AdmissionLimits(user_inflight=8, global_inflight=8))
    assert result["status"] == "admitted" and gate.occupied_snapshot()["total"] == 1


def test_cancelled_canonical_request_replay_does_not_enqueue_or_rebind(context, monkeypatch):
    receipt, _ = _prepare(context.paths)
    # canonical终态由原writer创建；恢复只对账原输入，不把done投影当作另一份执行许可。
    from agent_py_agent.agent.gateway_parts.recovery import terminalize_gateway_request_file

    path = context.paths.processing / "fixture-input.json"
    write_json_file_atomic(path, receipt.prepared_request)
    terminalize_gateway_request_file(
        context.paths, path, context.paths.failed, "fixture-input",
        terminal_response={"id": "fixture-input", "ok": False, "status": "cancelled",
                           "error_code": "CANCELLED", "response": ""},
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("取消后的重放不能读取新绑定")

    monkeypatch.setattr(identities, "read_private_config_json", unexpected)
    replay, created = _prepare(context.paths)
    assert created is False and replay == receipt
    with delivery.gateway_input_transition(context.paths, "fixture-input"):
        _queued, created = delivery.queue_gateway_input_locked(context.paths, replay)
    assert created is False
    assert not (context.paths.inbox / "fixture-input.json").exists()


@pytest.mark.parametrize("field", ["metadata", "input_media", "goal"])
def test_route_and_materializer_do_not_bless_changed_prepared_facts(context, field):
    receipt, _ = _prepare(context.paths)
    request = copy.deepcopy(receipt.prepared_request)
    if field == "metadata":
        request[field]["sender_type"] = "bot"
    elif field == "input_media":
        request[field][0]["ref"] = "other-media-ref"
    else:
        request[field] = "替换正文"
    with pytest.raises(route.OwnerRouteMismatchError):
        route.freeze_request_route(request, frozen_request_owner(request),
                                   route.RouteStamp(2, "fixture-c", 1.0, "none"))
    _fingerprint(request)
    write_json_file_atomic(context.paths.inbox / "fixture-input.json", request)
    with pytest.raises(ValueError, match="different input"):
        delivery._materialize_prepared_request_locked(context.paths, receipt)


def test_count_zero_prepare_keeps_original_input_and_does_not_read_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Count0不能新增绑定读取")

    monkeypatch.setattr(identities, "read_private_config_json", unexpected)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    request = _request()
    assert prepare_coordinator_input(paths, request) is request
    receipt, created = _prepare(paths, request)
    assert created and receipt.prepared_request == request


def test_copied_or_revoked_control_plane_cannot_freeze_new_input(context):
    with pytest.raises(route.OwnerRouteMismatchError, match="source_changed"):
        copy.copy(context).prepare_input(_request())
    lease = context._coordinator_role_lease
    with lease._lifecycle_gate:
        lease._closing = True
        try:
            with pytest.raises(route.OwnerRouteMismatchError, match="source_changed"):
                prepare_coordinator_input(context.paths, _request())
        finally:
            lease._closing = False


def test_closed_coordinator_context_never_falls_back_to_count_zero(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    paths = gateway_paths_from_root(tmp_path / "gateway")
    with coordinator_guard(paths.root, 2):
        prepare_coordinator_takeover(paths, 2, "fixture-c")
        control = GatewayControlPlane(AgentConfig(my_agent_home=str(root)), root, paths, 2, "fixture-c")
        assert control.controller().active()
    with pytest.raises(route.OwnerRouteMismatchError, match="source_changed"):
        prepare_coordinator_input(paths, _request())


def test_replacement_coordinator_cannot_overwrite_original_paths_source(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    paths = gateway_paths_from_root(tmp_path / "gateway")
    config = AgentConfig(my_agent_home=str(home))
    with coordinator_guard(paths.root, 2):
        prepare_coordinator_takeover(paths, 2, "fixture-c")
        old = GatewayControlPlane(config, home, paths, 2, "fixture-c")
    old_lease = paths._coordinator_role_lease
    with coordinator_guard(paths.root, 2):
        replacement_paths = gateway_paths_from_root(paths.root)
        prepare_coordinator_takeover(replacement_paths, 2, "fixture-c")
        with pytest.raises(route.OwnerRouteMismatchError, match="source_changed"):
            GatewayControlPlane(config, home, paths, 2, "fixture-c")
        assert paths._coordinator_role_lease is old_lease
        current = GatewayControlPlane(config, home, replacement_paths, 2, "fixture-c")
        assert current.controller().active() and old.controller() is None
        with pytest.raises(route.OwnerRouteMismatchError, match="source_changed"):
            prepare_coordinator_input(paths, _request())


def test_once_commit_is_in_original_transaction_and_cannot_replay_after_exit(context):
    captured = []

    def transition(commit):
        captured.append(commit)
        result = commit()
        with pytest.raises(ValueError, match="commit_window_required"):
            commit()
        return result

    path, created = write_gateway_request_once(context.paths, _request(), commit_transition=transition)
    assert created and path == context.paths.inbox / "fixture-input.json"
    receipt = delivery.read_gateway_input_receipt(context.paths, "fixture-input")
    assert frozen_request_owner(receipt.prepared_request) == OwnerIdentity.provider_user("feishu", "fixture-alice")
    with pytest.raises(ValueError, match="commit_window_required"):
        captured[0]()
    assert write_gateway_request_once(context.paths, _request())[1] is False


def test_once_commit_cannot_cross_thread_while_original_window_is_open(context):
    errors = []

    def transition(commit):
        def copied():
            try:
                commit()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=copied)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert str(errors[0]) == "gateway_request_commit_window_required"
        assert not delivery.gateway_input_receipt_path(context.paths, "fixture-input").exists()
        return commit()

    assert write_gateway_request_once(context.paths, _request(), commit_transition=transition)[1] is True


def test_once_failed_transition_does_not_leave_an_active_commit_or_prepare(context):
    captured = []

    def refused(commit):
        captured.append(commit)
        raise InterruptedError("fixture cancellation")

    with pytest.raises(InterruptedError, match="fixture cancellation"):
        write_gateway_request_once(context.paths, _request(), commit_transition=refused)
    with pytest.raises(ValueError, match="commit_window_required"):
        captured[0]()
    assert not delivery.gateway_input_receipt_path(context.paths, "fixture-input").exists()
    assert not (context.paths.inbox / "fixture-input.json").exists()
