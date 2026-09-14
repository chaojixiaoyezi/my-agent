"""飞书扫码自助建应用(设备码 OAuth)流程单测。mock HTTP,不打真网络。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_py_agent.agent.adapter import feishu_app_registration as far

_MOD = "agent_py_agent.agent.adapter.feishu_app_registration"


def _begin(**over):
    b = far.BeginResult(device_code="dc", qr_url="u", user_code="UC", interval=0, expire_in=600)
    for k, v in over.items():
        setattr(b, k, v)
    return b


def test_init_rejects_when_client_secret_unsupported():
    with patch(f"{_MOD}._post_registration", return_value={"supported_auth_methods": ["private_key_jwt"]}):
        with pytest.raises(far.FeishuRegistrationError):
            far.init_app_registration("feishu")


def test_init_ok_when_client_secret_supported():
    with patch(f"{_MOD}._post_registration", return_value={"supported_auth_methods": ["client_secret"]}):
        far.init_app_registration("feishu")  # 不抛即通过


def test_begin_uses_raw_clean_url_without_sample_c_tag():
    raw = {
        "device_code": "DEV123",
        "user_code": "U-9",
        "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=U-9",
        "expires_in": 480,  # 实测字段名(非 通道运行时 写的 expire_in)
        "interval": 5,
    }
    with patch(f"{_MOD}._post_registration", return_value=raw):
        b = far.begin_app_registration("feishu")
    assert b.device_code == "DEV123"
    assert b.expire_in == 480
    assert b.interval == 5
    # 直接用飞书原始 URL,不加 通道运行时 的 tp=ob_cli_app(否则授权页挂 通道运行时 牌子)
    assert b.qr_url == "https://open.feishu.cn/page/launcher?user_code=U-9"
    assert "ob_cli_app" not in b.qr_url


def test_begin_falls_back_when_expires_missing():
    raw = {"device_code": "D", "verification_uri_complete": "https://x/?q=1"}
    with patch(f"{_MOD}._post_registration", return_value=raw):
        b = far.begin_app_registration("feishu")
    assert b.expire_in == far._DEFAULT_EXPIRE_S
    assert b.interval == far._DEFAULT_POLL_INTERVAL_S


def test_poll_success():
    resp = {"client_id": "cli_app", "client_secret": "sek", "user_info": {"open_id": "ou_1"}}
    with patch(f"{_MOD}._post_registration", return_value=resp):
        r = far.poll_app_registration(_begin())
    assert r.app_id == "cli_app"
    assert r.app_secret == "sek"
    assert r.open_id == "ou_1"
    assert r.domain == "feishu"


def test_poll_pending_then_success():
    seq = [
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"client_id": "A", "client_secret": "B"},
    ]
    with patch(f"{_MOD}._post_registration", side_effect=seq), patch(f"{_MOD}.time.sleep"):
        r = far.poll_app_registration(_begin())
    assert (r.app_id, r.app_secret) == ("A", "B")


def test_poll_auto_switches_to_lark():
    seq = [
        {"user_info": {"tenant_brand": "lark"}},   # 触发切域名
        {"client_id": "A", "client_secret": "B", "user_info": {"tenant_brand": "lark"}},
    ]
    with patch(f"{_MOD}._post_registration", side_effect=seq), patch(f"{_MOD}.time.sleep"):
        r = far.poll_app_registration(_begin(), initial_domain="feishu")
    assert r.domain == "lark"


def test_poll_access_denied_raises():
    with patch(f"{_MOD}._post_registration", return_value={"error": "access_denied"}):
        with pytest.raises(far.FeishuRegistrationError, match="access_denied|拒绝"):
            far.poll_app_registration(_begin())


def test_poll_expired_raises():
    with patch(f"{_MOD}._post_registration", return_value={"error": "expired_token"}), patch(f"{_MOD}.time.sleep"):
        with pytest.raises(far.FeishuRegistrationError, match="过期|expired"):
            far.poll_app_registration(_begin())


def test_full_scan_flow_calls_on_qr_and_returns_creds():
    captured = {}

    def on_qr(begin, qr_ascii):
        captured["begin"] = begin

    seq = [
        {"supported_auth_methods": ["client_secret"]},  # init
        {"device_code": "D", "verification_uri_complete": "https://x/?q=1", "expires_in": 600, "interval": 0},  # begin
        {"client_id": "cli_X", "client_secret": "secY", "user_info": {"open_id": "ou_z"}},  # poll
    ]
    with patch(f"{_MOD}._post_registration", side_effect=seq), patch(f"{_MOD}.time.sleep"):
        r = far.register_feishu_app_by_scan(domain="feishu", on_qr=on_qr)
    assert r.app_id == "cli_X"
    assert r.app_secret == "secY"
    assert "begin" in captured  # on_qr 被回调
