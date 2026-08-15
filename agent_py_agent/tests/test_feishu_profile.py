"""飞书首聊自动称呼单测:查名(mock HTTP,fail-open)+ 种"称呼"幂等。"""

from __future__ import annotations

import json
from unittest.mock import patch

from agent_py_agent.agent.adapter import feishu_profile as fp

_TEMPLATE = "# USER\n\n## 画像\n- 称呼:\n- 角色/背景:\n- 语言:\n"


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_name_success():
    seq = [_FakeResp({"tenant_access_token": "t"}), _FakeResp({"code": 0, "data": {"user": {"name": "大叶子"}}})]
    with patch.object(fp.urllib.request, "urlopen", side_effect=seq):
        assert fp.fetch_feishu_display_name("a", "b", "ou_x") == "大叶子"


def test_fetch_name_no_permission_returns_none():
    seq = [_FakeResp({"tenant_access_token": "t"}), _FakeResp({"code": 99991672, "msg": "no permission"})]
    with patch.object(fp.urllib.request, "urlopen", side_effect=seq):
        assert fp.fetch_feishu_display_name("a", "b", "ou_x") is None


def test_fetch_name_network_error_fail_open():
    with patch.object(fp.urllib.request, "urlopen", side_effect=OSError("boom")):
        assert fp.fetch_feishu_display_name("a", "b", "ou_x") is None


def test_fetch_name_empty_creds_returns_none():
    assert fp.fetch_feishu_display_name("", "b", "ou_x") is None


def test_call_name_empty_then_filled(tmp_path):
    p = tmp_path / "USER.md"
    p.write_text(_TEMPLATE, encoding="utf-8")
    assert fp.call_name_is_empty(p) is True
    assert fp.seed_call_name(p, "大叶子") is True
    assert "- 称呼: 大叶子" in p.read_text(encoding="utf-8")
    assert fp.call_name_is_empty(p) is False  # 已填→不再空


def test_seed_call_name_idempotent(tmp_path):
    p = tmp_path / "USER.md"
    p.write_text(_TEMPLATE.replace("- 称呼:", "- 称呼: 老王"), encoding="utf-8")
    assert fp.seed_call_name(p, "大叶子") is False  # 已填不覆盖
    assert "老王" in p.read_text(encoding="utf-8")


def test_seed_missing_file_fail_open(tmp_path):
    assert fp.seed_call_name(tmp_path / "nope.md", "x") is False
    assert fp.call_name_is_empty(tmp_path / "nope.md") is False
