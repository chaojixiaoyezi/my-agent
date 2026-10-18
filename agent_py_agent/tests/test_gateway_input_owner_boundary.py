"""首次C受理的观察版本不重选原宿主owner；HTTP客户端字段不成为宿主身份。"""
from dataclasses import asdict

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _append_control_guidance,
    active_turn_guidance_dedupe_key,
)
from agent_py_agent.agent.gateway_parts.input_delivery_service import (
    _replay_request_payload,
    read_gateway_input_receipt,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import (
    REQUEST_OWNER_KEY,
    frozen_request_owner,
)
from agent_py_agent.agent.user_space import admin_channel_identity as identities
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_http_prepare_boundary import (
    _body,
    _submit,
)
from agent_py_agent.tests.test_gateway_http_prepare_boundary import server as server
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


@pytest.fixture(params=[False, True], ids=["count0", "C"])
def ingress(server, request):
    if not request.param:
        yield server
        return
    config = server.agent.config
    config.enable_subagents = False
    constructor_root = server.agent.root
    home = server.agent.home_paths.root
    with coordinator_guard(server.paths.root, 2):
        controller = prepare_coordinator_takeover(server.paths, 2, "owner-boundary-c")
        server.control_plane = GatewayControlPlane(config, home, server.paths, 2, controller.c_run_id,
                                                  agent_root=constructor_root)
        server.agent = None
        yield server


def test_http_discards_client_top_level_host_owner_and_other_route_identity(ingress):
    body = _body()
    body[REQUEST_OWNER_KEY] = asdict(OwnerIdentity.local_main())
    body.update(owner_id="local/main", route={"worker": "C"}, user_id="other-client",
                gateway_admin_binding_version="client-owned")
    (status, _), request_id = _submit(
        ingress, body, user_id="fixture-user", channel="feishu", authenticated=True
    )
    assert status == 202
    prepared = read_gateway_input_receipt(ingress.paths, request_id).prepared_request
    assert frozen_request_owner(prepared) == OwnerIdentity.provider_user("feishu", "fixture-user")
    assert prepared["metadata"]["user_id"] == "fixture-user"
    assert "route" not in prepared
    assert prepared["owner_id"] != "local/main"


def test_http_metadata_host_owner_claim_is_only_a_conflict_assertion_not_authority(ingress):
    body = _body()
    body["metadata"][REQUEST_OWNER_KEY] = asdict(OwnerIdentity.local_main())
    (status, _), request_id = _submit(ingress, body, user_id="fixture-user", channel="feishu")
    assert status == 503
    assert read_gateway_input_receipt(ingress.paths, request_id) is None
    assert not list(ingress.paths.inbox.glob("*.json"))


def test_first_c_steer_preserves_original_host_owner_and_records_current_observation_version(context, tmp_path):
    original_owner = OwnerIdentity.local_main()
    identities.bind_admin_channel_identity(context.home_root, "feishu", "fixture-user", now=1.0)
    old_version = identities.read_admin_channel_identity_snapshot(context.home_root).version
    store = ConversationStore(tmp_path / "guidance")
    thread = store.threads.get_or_create({"canonical_user_id": "fixture-user"})
    scope = GatewayControlScope(user_id="fixture-user", channel="feishu", conversation_id="fixture-room",
        metadata={"message_id": "guidance-owner"}, resolved_owner=original_owner)
    _append_control_guidance(store, "request", "old-turn", ConversationControlCommand("steer", "合成补充"),
        scope, thread.thread_id, "old-turn")
    receipt = store.guidance.receipt(active_turn_guidance_dedupe_key(scope))
    identities.remove_admin_channel_identity(context.home_root, "feishu", "fixture-user")
    current_version = identities.read_admin_channel_identity_snapshot(context.home_root).version
    assert current_version != old_version
    payload = _replay_request_payload(receipt, "first-steer-replay", "first-steer-digest")
    prepared = context.prepare_input(payload)
    assert frozen_request_owner(prepared) == original_owner
    assert prepared["metadata"][REQUEST_OWNER_KEY] == asdict(original_owner)
    assert prepared["metadata"]["gateway_admin_binding_version"] == current_version
