"""飞书人设确认卡片单测:卡片 JSON 构造、lark 回调对象归一化、以及核心回调逻辑 apply_card_action
(confirm→真的 append 进 owner 的 SOUL/AGENTS.md;decline/找不到→不写;重复 confirm→只写一次)。
真机没法测真人点按钮(护栏禁止给真实用户发消息),所以回调逻辑靠模拟 card.action payload 的单测覆盖。"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.adapter.feishu_card import (
    apply_card_action,
    build_persona_confirm_card,
    extract_card_action,
    extract_webhook_card_action,
)
from agent_py_agent.agent.capability import persona_pending
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

# --------------------------------------------------------------------------- 卡片 JSON 构造


def test_build_card_has_two_buttons_carrying_token_and_choice():
    card = build_persona_confirm_card("tok123", "soul", "语气偏活泼")
    assert card["header"]["title"]["content"] == "确认写入长期设定"
    # 一句话说明里带内容与目标标签
    div_text = card["elements"][0]["text"]["content"]
    assert "语气偏活泼" in div_text and "SOUL" in div_text
    actions = card["elements"][1]["actions"]
    assert len(actions) == 2
    values = {a["value"]["choice"]: a["value"]["token"] for a in actions}
    assert values == {"confirm": "tok123", "decline": "tok123"}  # 两按钮 action.value 都带同一 token


def test_build_card_agents_label():
    card = build_persona_confirm_card("t", "agents", "产物用 HTML")
    assert "AGENTS" in card["elements"][0]["text"]["content"]


# --------------------------------------------------------------------------- lark 回调归一化


def _fake_action(value, open_id="ou_clicker", form_value=None):
    action = SimpleNamespace(tag="button", value=value)
    if form_value is not None:
        action.form_value = form_value
    return SimpleNamespace(
        event=SimpleNamespace(
            action=action,
            operator=SimpleNamespace(open_id=open_id),
        )
    )


def test_extract_card_action_from_dict_value():
    norm = extract_card_action(_fake_action({"token": "t1", "choice": "confirm"}, open_id="ou_9"))
    assert norm == {"value": {"token": "t1", "choice": "confirm"}, "operator_open_id": "ou_9", "form_value": {}}


def test_extract_card_action_form_value():
    # 密码卡是表单提交:form_value 里带输入框值(密码走这,不进聊天)。
    norm = extract_card_action(_fake_action({"session_lock_action": "pwd_unlock"}, form_value={"pwd": "Abcd1234"}))
    assert norm is not None and norm["form_value"] == {"pwd": "Abcd1234"}


def test_extract_card_action_from_json_string_value():
    # 某些 SDK 版本 action.value 是 JSON 串:也要能解析
    norm = extract_card_action(_fake_action('{"token": "t2", "choice": "decline"}'))
    assert norm is not None and norm["value"] == {"token": "t2", "choice": "decline"}


def test_extract_card_action_invalid_returns_none():
    assert extract_card_action(SimpleNamespace(event=None)) is None
    assert extract_card_action(SimpleNamespace()) is None
    assert extract_card_action(_fake_action("not-json")) is None
    assert extract_card_action(_fake_action(123)) is None  # value 非 dict/串


def test_extract_webhook_card_action_supports_v2_payload():
    norm = extract_webhook_card_action(
        {
            "event": {
                "operator": {"operator_id": {"open_id": "ou_webhook"}},
                "action": {
                    "value": {"session_lock_action": "pwd_set", "user_id": "ou_webhook"},
                    "form_value": {"pwd": "Abcd1234"},
                },
            }
        }
    )
    assert norm == {
        "value": {"session_lock_action": "pwd_set", "user_id": "ou_webhook"},
        "operator_open_id": "ou_webhook",
        "form_value": {"pwd": "Abcd1234"},
    }


# --------------------------------------------------------------------------- 核心回调逻辑


def _pending(root, target="soul", content="语气偏活泼", owner_id="ou_owner"):
    return persona_pending.add(root, ("feishu", "user", owner_id), target, content)


def _owner_soul(root, owner_id="ou_owner"):
    return resolve_owner_home(root, OwnerIdentity.provider_user("feishu", owner_id)).soul_md


def _owner_agents(root, owner_id="ou_owner"):
    return resolve_owner_home(root, OwnerIdentity.provider_user("feishu", owner_id)).agents_md


def test_confirm_appends_to_owner_soul(tmp_path):
    token = _pending(tmp_path, target="soul", content="语气偏活泼")
    result = apply_card_action({"token": token, "choice": "confirm"}, tmp_path, "ou_owner")
    assert result["wrote"] is True and result["owner_id"] == "ou_owner"
    soul = _owner_soul(tmp_path)
    assert soul.exists() and "语气偏活泼" in soul.read_text(encoding="utf-8")
    # 记录已消费,pop 不再存在
    assert persona_pending.load(tmp_path, token) is None


def test_confirm_appends_to_owner_agents(tmp_path):
    token = _pending(tmp_path, target="agents", content="产物用 HTML")
    result = apply_card_action({"token": token, "choice": "confirm"}, tmp_path, "ou_owner")
    assert result["wrote"] is True
    assert "产物用 HTML" in _owner_agents(tmp_path).read_text(encoding="utf-8")


def test_decline_does_not_write(tmp_path):
    token = _pending(tmp_path, target="soul", content="别写我")
    result = apply_card_action({"token": token, "choice": "decline"}, tmp_path, "ou_owner")
    assert result["wrote"] is False and "取消" in result["reply_text"]
    # SOUL 文件没被创建/没这句
    soul = _owner_soul(tmp_path)
    assert (not soul.exists()) or "别写我" not in soul.read_text(encoding="utf-8")
    assert persona_pending.load(tmp_path, token) is None  # decline 也丢弃了待确认记录


def test_token_not_found_no_write(tmp_path):
    result = apply_card_action(
        {"token": "deadbeefdeadbeef", "choice": "confirm"}, tmp_path, "ou_owner"
    )
    assert result["wrote"] is False and "失效" in result["reply_text"]


def test_repeated_confirm_writes_only_once(tmp_path):
    # 卡片重复回调:同一 token 连 confirm 两次,只写一次(pop 原子领取兜底幂等)
    token = _pending(tmp_path, target="soul", content="只写一次")
    first = apply_card_action({"token": token, "choice": "confirm"}, tmp_path, "ou_owner")
    second = apply_card_action({"token": token, "choice": "confirm"}, tmp_path, "ou_owner")
    assert first["wrote"] is True and second["wrote"] is False
    assert _owner_soul(tmp_path).read_text(encoding="utf-8").count("只写一次") == 1


def test_missing_token_in_value(tmp_path):
    assert apply_card_action({}, tmp_path)["wrote"] is False
    assert apply_card_action({"choice": "confirm"}, tmp_path)["wrote"] is False


def test_non_owner_cannot_consume_or_confirm_pending_persona(tmp_path):
    token = _pending(tmp_path, target="soul", content="只允许本人确认")
    result = apply_card_action({"token": token, "choice": "confirm"}, tmp_path, "ou_other")
    assert result["wrote"] is False and "发起人" in result["reply_text"]
    assert persona_pending.load(tmp_path, token) is not None
    assert not _owner_soul(tmp_path).exists()
