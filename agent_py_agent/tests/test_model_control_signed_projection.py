"""普通合法模型卡签名投影：不重写私有卡行、不运行受限损坏探针。"""

import pytest

from agent_py_agent.agent.gateway_parts.model_control_cards import (
    _model_control_payload,
    _require_model_control_record,
)


@pytest.mark.parametrize(
    "optional",
    [
        {},
        {"parent_control_id": ""},
        {"parent_control_id": "8c048fba-f053-44d0-b034-62c9570ebaff"},
        {"extension": {"version": 2, "facts": ["declared", None]}},
    ],
)
def test_legal_signed_projection_preserves_presence_and_original_extensions(optional):
    payload = {
        "kind": "model_configuration",
        "owner": {"provider": "feishu", "owner_kind": "user", "owner_id": "ou_actor"},
        "channel": "feishu",
        "actor_id": "ou_actor",
        "thread_id": "thread-original",
        "control_id": "593b38ac-d44a-43f9-9f3a-9119f83bd5a5",
        "expires_at": 2000000000.5,
        "mode": "form",
        "profile_id": "",
        "catalog_revisions": [None, "admin-generation", "shared-generation"],
        "selection_revision": 2,
        **optional,
    }
    body = {
        "reference": "original-verified-reference",
        "message_id": "om_original",
        "chat_id": "oc_original",
    }
    # 已由原生产writer保存的合法行形状；只有实际status/result等运行字段不属于签名载荷。
    row = {
        **payload,
        **body,
        "status": "completed",
        "decision": "save",
        "submission_digest": "same-original-digest",
        "result": {"ok": True},
    }
    _require_model_control_record(row, payload, body)
    assert _model_control_payload(row) == payload
    assert ("parent_control_id" in _model_control_payload(row)) == ("parent_control_id" in optional)
