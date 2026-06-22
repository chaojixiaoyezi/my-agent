"""config-set / config-get 测试:行级安全写回(保留注释/原子)+ 白名单 + secret 脱敏。

钉死:① 顶层 key 原地替换、注释和其余行不动 ② 整数字段不加引号 ③ 缺失 key 追加 ④ 含引号的值拒绝
(不写坏这套简化 yaml)⑤ 白名单外字段(安全项)拒绝 ⑥ 飞书 secret 写入成功但回显脱敏 ⑦ get 脱敏/缺失。
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agent_py_agent.agent.settings.config_io import load_simple_yaml, set_simple_yaml_value
from agent_py_agent.cli import config_cmd as C

_SAMPLE = '''# 顶部注释,不能被动
agent_name: "myagent"

# 飞书通道配置(注释要保留)
feishu_app_id: ""
feishu_app_secret: ""
feishu_callback_port: 8421
'''


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "agent_config.yaml"
    p.write_text(_SAMPLE, encoding="utf-8")
    return p


def test_set_value_replaces_in_place_and_keeps_comments(tmp_path: Path) -> None:
    p = _write_config(tmp_path)
    old, new_line = set_simple_yaml_value(p, "feishu_app_id", "cli_abc123")
    text = p.read_text(encoding="utf-8")
    assert old == '""' and new_line == 'feishu_app_id: "cli_abc123"'
    assert "# 飞书通道配置(注释要保留)" in text  # 注释没被动
    assert "# 顶部注释,不能被动" in text
    assert 'agent_name: "myagent"' in text  # 其余行没被动
    assert load_simple_yaml(p)["feishu_app_id"] == "cli_abc123"


def test_set_integer_value_no_quotes(tmp_path: Path) -> None:
    p = _write_config(tmp_path)
    _old, new_line = set_simple_yaml_value(p, "feishu_callback_port", "9001")
    assert new_line == "feishu_callback_port: 9001"  # 整数不加引号
    assert load_simple_yaml(p)["feishu_callback_port"] == 9001


def test_set_missing_key_appends(tmp_path: Path) -> None:
    p = _write_config(tmp_path)
    old, _new = set_simple_yaml_value(p, "feishu_encrypt_key", "ek123")
    assert old is None  # 原文件无此行 → 追加
    assert load_simple_yaml(p)["feishu_encrypt_key"] == "ek123"


def test_set_value_with_quote_rejected(tmp_path: Path) -> None:
    p = _write_config(tmp_path)
    with pytest.raises(ValueError):
        set_simple_yaml_value(p, "feishu_app_id", 'has"quote')
    assert load_simple_yaml(p)["feishu_app_id"] == ""  # 拒绝后原值没被破坏


def test_cmd_config_set_rejects_non_whitelisted(tmp_path, capsys) -> None:
    p = _write_config(tmp_path)
    rc = C.cmd_config_set(Namespace(key="path_access_mode", value="dangerous", config=str(p)))
    assert rc == 1
    assert "白名单" in capsys.readouterr().out  # 挡掉安全相关字段误改


def test_cmd_config_set_feishu_ok_and_masks_secret(tmp_path, capsys) -> None:
    p = _write_config(tmp_path)
    rc = C.cmd_config_set(Namespace(key="feishu_app_secret", value="supersecretvalue", config=str(p)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "supersecretvalue" not in out and "sup***" in out  # secret 回显脱敏
    assert load_simple_yaml(p)["feishu_app_secret"] == "supersecretvalue"  # 但真值确实写进去了


def test_cmd_config_get_masks_and_handles_missing(tmp_path, capsys) -> None:
    p = _write_config(tmp_path)
    set_simple_yaml_value(p, "feishu_app_secret", "abcdef123456")
    assert C.cmd_config_get(Namespace(key="feishu_app_secret", config=str(p))) == 0
    out = capsys.readouterr().out
    assert "abcdef123456" not in out and "abc***" in out  # get 也脱敏


def test_cmd_config_set_file_missing(tmp_path, capsys) -> None:
    rc = C.cmd_config_set(Namespace(key="feishu_app_id", value="x", config=str(tmp_path / "nope.yaml")))
    assert rc == 1
    assert "不存在" in capsys.readouterr().out
