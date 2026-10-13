"""群/私聊交互的引用防篡改与宿主密钥保护，全部使用隔离目录及合成身份。"""
from __future__ import annotations

import base64
import json
import time

import pytest

from agent_py_agent.agent.path_access_policy import (
    PRIVATE_INTERACTIONS_OWNER_PARTS,
    PathAccessPolicy,
)
from agent_py_agent.agent.user_space.interaction_refs import (
    interaction_key,
    sign_interaction_reference,
    verify_interaction_reference,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home


# LLM: 签名数据只含合成身份/执行 refs，不包含任何密码；真实授权仍由原运行记录检查。
# 函数用途: 创建可被原发起人验证的群来源交互样本。
def _case(tmp_path):
    owner = ensure_owner_home(tmp_path, OwnerIdentity.provider_group("feishu", "oc_test"))
    payload = {"kind": "tool_approval", "owner": {"provider": "feishu", "owner_kind": "group", "owner_id": "oc_test"},
               "channel": "feishu", "actor_id": "ou_creator", "expires_at": time.time() + 60,
               "run_id": "child-run", "request_hash": "sha256:synthetic"}
    return owner, payload, sign_interaction_reference(owner.home_dir, payload)


def test_original_actor_can_verify_and_other_actor_cannot(tmp_path):
    owner, payload, ref = _case(tmp_path)
    assert verify_interaction_reference(tmp_path, ref, channel="feishu", actor_id="ou_creator") == payload
    assert len(interaction_key(owner.home_dir)) == 32
    for channel, actor in (("qq", "ou_creator"), ("feishu", "ou_other")):
        with pytest.raises(ValueError):
            verify_interaction_reference(tmp_path, ref, channel=channel, actor_id=actor)


def test_edited_execution_or_owner_is_rejected_without_creating_owner(tmp_path):
    _owner, payload, ref = _case(tmp_path)
    prefix, _encoded, signature = ref.split(".")
    for change in ({"run_id": "another-run"}, {"owner": {"provider": "feishu", "owner_kind": "group", "owner_id": "absent"}}):
        modified = {**payload, **change}
        encoded = base64.urlsafe_b64encode(json.dumps(modified).encode()).decode().rstrip("=")
        with pytest.raises(ValueError):
            verify_interaction_reference(tmp_path, f"{prefix}.{encoded}.{signature}", channel="feishu", actor_id="ou_creator")
    assert not (tmp_path / "owners" / "feishu" / "groups" / "absent").exists()


def test_expired_reference_and_bad_existing_key_are_not_repaired(tmp_path):
    owner, payload, _ref = _case(tmp_path)
    expired = sign_interaction_reference(owner.home_dir, {**payload, "expires_at": time.time() - 1})
    with pytest.raises(ValueError):
        verify_interaction_reference(tmp_path, expired, channel="feishu", actor_id="ou_creator")
    key_path = owner.home_dir.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS, "key.json")
    key_path.write_text("{}")
    with pytest.raises(ValueError):
        interaction_key(owner.home_dir, create=True)
    assert key_path.read_text() == "{}"


@pytest.mark.parametrize("mode", ["normal", "full"])
def test_key_storage_is_hidden_from_model_even_in_full_mode(tmp_path, mode):
    owner, _payload, _ref = _case(tmp_path)
    key_path = owner.home_dir.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS, "key.json")
    policy = PathAccessPolicy.from_values(mode=mode, agent_home_root=tmp_path)
    decision = policy.check(key_path)
    assert not decision.allowed and decision.code == "PATH_HOST_MANAGED_STORE_BLOCKED"
