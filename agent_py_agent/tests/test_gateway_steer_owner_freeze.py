"""插话恢复沿原guidance保存的宿主owner，不从当前绑定或会话名称重推归属。"""
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _append_control_guidance,
)
from agent_py_agent.agent.gateway_parts.input_delivery_service import _replay_request_payload
from agent_py_agent.agent.gateway_parts.request_owner_binding import (
    REQUEST_OWNER_KEY,
    frozen_request_owner,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    root = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    assert root != (Path.home() / ".my-agent").resolve()


@pytest.mark.parametrize("owner", [OwnerIdentity.provider_user("feishu", "fixture-user"),
    OwnerIdentity.provider_group("feishu", "fixture-room"), OwnerIdentity.local_main()])
def test_original_guidance_owner_survives_canonical_replay(tmp_path, owner):
    store = ConversationStore(tmp_path / "owner-conversation")
    thread = store.threads.get_or_create({"canonical_user_id": "fixture-user"})
    scope = GatewayControlScope(user_id="fixture-user", channel="feishu", conversation_id="fixture-room",
                                metadata={"message_id": "fixture-steer"}, resolved_owner=owner)
    _append_control_guidance(store, "request", "old-turn", ConversationControlCommand("steer", "合成补充"),
                             scope, thread.thread_id, "old-turn")
    from agent_py_agent.agent.gateway_parts.control_service import active_turn_guidance_dedupe_key

    receipt = store.guidance.receipt(active_turn_guidance_dedupe_key(scope))
    assert receipt.entry.metadata[REQUEST_OWNER_KEY] == asdict(owner)
    payload = _replay_request_payload(receipt, "replay-input", "same-client-digest")
    assert frozen_request_owner(payload) == owner
    assert payload["metadata"][REQUEST_OWNER_KEY] == asdict(owner)
    assert payload["metadata"]["thread_id"] == thread.thread_id
    assert payload["goal"] == "合成补充"
    assert "expected_turn_id" not in payload["metadata"]
