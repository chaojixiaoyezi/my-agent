"""SecretRef 解析 + 脱敏单测(#4 借鉴 通道运行时 secret-contract 核心)。"""

from __future__ import annotations

import os
from unittest.mock import patch

from agent_py_agent.agent.settings.secret_ref import is_secret_ref, redact, resolve_secret_ref


def test_plain_value_returned_asis():
    assert resolve_secret_ref("cli_aab3c9e11178dbc1") == "cli_aab3c9e11178dbc1"
    assert resolve_secret_ref("") == ""


def test_env_ref_resolves():
    with patch.dict(os.environ, {"MY_FEISHU_SECRET_X": "s3cr3t"}):
        assert resolve_secret_ref("env:MY_FEISHU_SECRET_X") == "s3cr3t"
        assert resolve_secret_ref("  env:MY_FEISHU_SECRET_X  ") == "s3cr3t"  # 去首尾空白


def test_env_ref_missing_returns_empty():
    os.environ.pop("NOPE_SECRET_XYZ", None)
    assert resolve_secret_ref("env:NOPE_SECRET_XYZ") == ""


def test_file_ref_resolves(tmp_path):
    p = tmp_path / "sec"
    p.write_text("filesecret\n", encoding="utf-8")  # 含尾换行
    assert resolve_secret_ref(f"file:{p}") == "filesecret"  # 去尾空白


def test_file_ref_missing_returns_empty():
    assert resolve_secret_ref("file:/no/such/path/never_xyz") == ""


def test_is_secret_ref():
    assert is_secret_ref("env:X")
    assert is_secret_ref("file:/p")
    assert not is_secret_ref("plain_secret")
    assert not is_secret_ref("")


def test_redact():
    assert redact("abcdefgh") == "abc***"
    assert redact("ab") == "***"
    assert redact("") == ""
    assert redact(None) == ""
