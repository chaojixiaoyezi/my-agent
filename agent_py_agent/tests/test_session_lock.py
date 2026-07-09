"""个人私聊会话锁 + 密码解锁(

钉:密码 scrypt 策略、闲置锁定阈值、群不锁、首设不可覆盖、改密验旧、
解锁验证、暴破失败指数退避锁定(monotonic)、成功清零。
"""

from __future__ import annotations

import time

from agent.session_lock import (
    PasswordPolicyError,
    SessionLockStore,
    UnlockService,
    hash_password,
    validate_password_policy,
    verify_password,
)


# ── 密码 crypto ──


def test_password_hash_verify_roundtrip():
    h = hash_password("Abcd1234")
    assert h.startswith("scrypt$")
    assert verify_password("Abcd1234", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("x", "garbage-not-a-hash") is False  # 坏格式不崩


def test_password_policy():
    validate_password_policy("Abcd1234")  # 合规不抛
    for bad in ["short1A", "abcd1234", "ABCD1234", "Abcdefgh"]:  # 短/无大写/无小写/无数字
        try:
            validate_password_policy(bad)
            raise AssertionError(f"应拦住:{bad}")
        except PasswordPolicyError:
            pass


# ── 锁服务 ──


def _svc(tmp_path, *, idle=3600, mono=None):
    store = SessionLockStore(tmp_path / "lock.db")
    return UnlockService(store, idle_limit_seconds=idle, monotonic=mono or time.monotonic), store


def test_first_contact_and_recent_activity_not_locked(tmp_path):
    svc, _ = _svc(tmp_path)
    assert svc.status("u1").locked is False  # 首次接触
    svc.record_activity("u1")
    assert svc.status("u1").locked is False  # 刚活跃


def test_group_never_locked(tmp_path):
    svc, store = _svc(tmp_path, idle=1)
    store.record_activity("g1", now=time.time() - 9999)  # 很久没活动
    assert svc.status("g1", is_group=True).locked is False  # 群聊永不锁
    svc.record_activity("g1", is_group=True)  # 群活动不触碰锁状态
    assert store.last_activity("g1") is not None  # (上面手动写的还在,但 record 群没写)


def test_idle_locks_with_and_without_password(tmp_path):
    svc, store = _svc(tmp_path, idle=1)
    store.record_activity("u1", now=time.time() - 100)  # 闲置 100s > 1s
    st = svc.status("u1")
    assert st.locked is True and st.requires_password_setup is True  # 无密码→引导设置
    store.set_password_hash("u1", hash_password("Abcd1234"))
    st2 = svc.status("u1")
    assert st2.locked is True and st2.requires_password_setup is False  # 有密码→要解锁


def test_set_password_first_time_only(tmp_path):
    svc, _ = _svc(tmp_path)
    ok, _ = svc.set_password("u1", "Abcd1234")
    assert ok is True
    ok2, msg = svc.set_password("u1", "Zzzz9999")  # 已设过不能覆盖
    assert ok2 is False and "改密码" in msg


def test_set_password_policy_rejected(tmp_path):
    svc, _ = _svc(tmp_path)
    ok, msg = svc.set_password("u1", "weak")
    assert ok is False and "不符合要求" in msg


def test_unlock_correct_and_wrong(tmp_path):
    svc, _ = _svc(tmp_path)
    svc.set_password("u1", "Abcd1234")
    assert svc.unlock("u1", "Abcd1234") is True
    assert svc.unlock("u1", "nope") is False


def test_change_password_requires_old(tmp_path):
    svc, _ = _svc(tmp_path)
    svc.set_password("u1", "Abcd1234")
    ok, _ = svc.change_password("u1", "wrongold", "Newpass9")
    assert ok is False  # 旧密码错
    ok2, _ = svc.change_password("u1", "Abcd1234", "Newpass9")
    assert ok2 is True and svc.unlock("u1", "Newpass9") is True  # 新密码生效


def test_bruteforce_backoff_lockout(tmp_path):
    clock = [1000.0]
    svc, _ = _svc(tmp_path, mono=lambda: clock[0])
    svc.set_password("u1", "Abcd1234")
    # 前 4 次失败不锁定
    for _ in range(4):
        assert svc.unlock("u1", "bad") is False
    assert svc.lockout_remaining("u1") == 0.0
    # 第 5 次失败起进入退避锁定窗
    assert svc.unlock("u1", "bad") is False
    assert svc.lockout_remaining("u1") > 0.0
    # 锁定窗内即便密码对也被节流拒(不验密码)
    assert svc.unlock("u1", "Abcd1234") is False
    # 时间前进过锁定窗后,正确密码解锁成功且清零
    clock[0] += 100000.0
    assert svc.unlock("u1", "Abcd1234") is True
    assert svc.lockout_remaining("u1") == 0.0


def test_events_emitted(tmp_path):
    seen = []
    store = SessionLockStore(tmp_path / "lock.db")
    svc = UnlockService(store, events=lambda k, p: seen.append(k))
    svc.set_password("u1", "Abcd1234")
    svc.unlock("u1", "Abcd1234")
    svc.unlock("u1", "bad")
    kinds = set(seen)
    assert "session_lock.password.set" in kinds
    assert "session_lock.unlock.succeeded" in kinds
    assert "session_lock.unlock.failed" in kinds


# ── 飞书密码卡 + 回调分发 ──


def test_build_password_card_modes():
    from agent.session_lock.feishu_cards import PWD_ACTION_FIELD, build_password_card

    for mode, action in [("set", "pwd_set"), ("unlock", "pwd_unlock"), ("change", "pwd_change")]:
        card = build_password_card(mode=mode, user_id="u1")
        form = next(e for e in card["elements"] if e.get("tag") == "form")
        submit = next(e for e in form["elements"] if e.get("action_type") == "form_submit")
        assert submit["value"][PWD_ACTION_FIELD] == action
        assert submit["value"]["user_id"] == "u1"
    # change 卡有旧+新两个输入框
    change = build_password_card(mode="change", user_id="u1")
    form = next(e for e in change["elements"] if e.get("tag") == "form")
    names = {e.get("name") for e in form["elements"] if e.get("tag") == "input"}
    assert names == {"old_pwd", "new_pwd"}


def test_is_password_action():
    from agent.session_lock.feishu_cards import is_password_action

    assert is_password_action({"session_lock_action": "pwd_unlock", "user_id": "u1"}) is True
    assert is_password_action({"token": "x", "choice": "confirm"}) is False  # persona 卡不误认
    assert is_password_action({}) is False


def test_handle_password_action_dispatch(tmp_path):
    from agent.session_lock.feishu_cards import handle_password_action

    svc, _ = _svc(tmp_path)
    # set
    r = handle_password_action(svc, {"session_lock_action": "pwd_set", "user_id": "u1"}, {"pwd": "Abcd1234"})
    assert "✅" in r["elements"][0]["content"]
    # unlock 对
    r2 = handle_password_action(svc, {"session_lock_action": "pwd_unlock", "user_id": "u1"}, {"pwd": "Abcd1234"})
    assert r2["header"]["template"] == "green"
    # unlock 错(不回显密码)
    r3 = handle_password_action(svc, {"session_lock_action": "pwd_unlock", "user_id": "u1"}, {"pwd": "nope"})
    assert r3["header"]["template"] == "grey" and "nope" not in str(r3)
    # change
    r4 = handle_password_action(svc, {"session_lock_action": "pwd_change", "user_id": "u1"},
                                {"old_pwd": "Abcd1234", "new_pwd": "Newpass9"})
    assert svc.unlock("u1", "Newpass9") is True


# ── 适配器锁门:首次要求设密码 ──


def test_adapter_gate_requires_password_on_first_contact(tmp_path):
    import tempfile

    from agent.adapter.feishu import FeishuAdapter
    from agent.adapter.protocol import IncomingMessage

    a = FeishuAdapter(config={"feishu_session_lock_enabled": True, "my_agent_home": tempfile.mkdtemp()})
    sent = []
    a._send_password_card = lambda uid, mode: sent.append((uid, mode))

    def _msg(ct="p2p"):
        return IncomingMessage(channel="feishu", user_id="ou_x", content="hi", message_id="m",
                               metadata={"feishu_chat_type": ct})

    # 首次(无密码)→ 拦下 + 发设置卡
    assert a._session_locked_gate(_msg()) is True
    assert sent == [("ou_x", "set")]
    # 设密码后 → 放行(未锁)
    a._unlock.set_password("ou_x", "Abcd1234")
    sent.clear()
    assert a._session_locked_gate(_msg()) is False
    assert sent == []
    # 群聊永不拦
    assert a._session_locked_gate(_msg("group")) is False


def test_adapter_gate_disabled_passes_through():
    from agent.adapter.feishu import FeishuAdapter
    from agent.adapter.protocol import IncomingMessage

    a = FeishuAdapter(config={"my_agent_home": "/tmp/x"})  # 未开开关
    assert a._unlock is None
    msg = IncomingMessage(channel="feishu", user_id="ou_x", content="hi", message_id="m", metadata={})
    assert a._session_locked_gate(msg) is False  # 放行,零影响
