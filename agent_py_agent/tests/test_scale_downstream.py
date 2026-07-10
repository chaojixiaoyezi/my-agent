from __future__ import annotations

import pytest

from agent_py_agent.agent.scale_downstream import _extract_message


def test_extracts_private_feishu_message_and_owner() -> None:
    message = _extract_message(
        {
            "tenant": "acme",
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "content": '{"text":"你好"}',
                },
            },
        }
    )
    assert message.owner_kind == "user"
    assert message.owner_id == "ou_user"
    assert message.prompt == "你好"


def test_rejects_message_without_tenant_or_reply_target() -> None:
    with pytest.raises(ValueError):
        _extract_message({"event": {"message": {"content": '{"text":"x"}'}}})
