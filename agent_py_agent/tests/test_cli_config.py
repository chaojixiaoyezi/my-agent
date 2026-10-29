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
    set_simple_yaml_value(p, "api_key", "sk-fake-9876543210")
    assert C.cmd_config_get(Namespace(key="api_key", config=str(p))) == 0
    out = capsys.readouterr().out
    assert "sk-fake-9876543210" not in out and "sk-***" in out  # 与参数中心同一凭据判定，原来只认 3 个飞书键


def test_cmd_config_set_file_missing(tmp_path, capsys) -> None:
    rc = C.cmd_config_set(Namespace(key="feishu_app_id", value="x", config=str(tmp_path / "nope.yaml")))
    assert rc == 1
    assert "不存在" in capsys.readouterr().out


# LLM: 临时配置真实经过参数写服务，旧值去除 YAML 外层引号后才脱敏；不读取生产配置。
# 函数用途: 回归普通键和密钥旧值的展示与实际落盘。
@pytest.mark.parametrize("key,old,expected", [
    ("feishu_app_id", "cli_old_id", "cli_old_id"),
    ("feishu_app_secret", "oldsecret", "old***"),
])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_set_previous_value_without_yaml_quotes(tmp_path, capsys, key, old, expected, quote):
    p = tmp_path / "config.yaml"
    p.write_text(f"{key}: {quote}{old}{quote}\n", encoding="utf-8")
    assert C.cmd_config_set(Namespace(key=key, value="newvalue", config=str(p))) == 0
    out = capsys.readouterr().out
    assert f"{key}: {expected} →" in out
    if key.endswith("secret"):
        assert old not in out and "newvalue" not in out
    assert load_simple_yaml(p)[key] == "newvalue"


# LLM: 坏端口必须被真实 typed 校验拒绝；输出具体服务原因而非中文兜底，且不改变原文件。
# 函数用途: 验证参数写失败保留错误码和人可读说明。
def test_set_invalid_port_prints_reason(tmp_path, capsys):
    p = _write_config(tmp_path)
    before = p.read_bytes()
    assert C.cmd_config_set(Namespace(key="feishu_callback_port", value="not-a-port", config=str(p))) == 1
    out = capsys.readouterr().out
    assert "PARAMETER_INVALID" in out
    assert "'feishu_callback_port' 取值不合法：必须是正整数" in out
    assert p.read_bytes() == before


# LLM: 使用独立随包默认替身区分空串、非空默认值及数值零，不调用真实用户配置。
# 函数用途: 保留 config get 空串未设置展示并防止零值误判。
@pytest.mark.parametrize("user_line,default_line,expected", [
    ("", 'feishu_encrypt_key: ""\n', "(未设置) feishu_encrypt_key"),
    ('feishu_encrypt_key: ""\n', 'feishu_encrypt_key: "default-key"\n', "(未设置) feishu_encrypt_key"),
    ("", 'feishu_encrypt_key: "default-key"\n', "feishu_encrypt_key: def***"),
    ("feishu_callback_port: 0\n", "", "feishu_callback_port: 0"),
])
def test_get_empty_string_compat(tmp_path, monkeypatch, capsys, user_line, default_line, expected):
    p = tmp_path / "user.yaml"
    p.write_text(user_line, encoding="utf-8")
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text(default_line, encoding="utf-8")
    monkeypatch.setattr(C, "packaged_config_path", lambda: defaults)
    key = "feishu_callback_port" if "callback_port" in user_line else "feishu_encrypt_key"
    assert C.cmd_config_get(Namespace(key=key, config=str(p))) == 0
    assert capsys.readouterr().out.strip() == expected


# LLM: 注入正式写门的受限事实且自动恢复；拒绝前后配置与参数账本都不得变化。
# 函数用途: 验证 worker/executor 权限拒绝返回结构化非零结果。
@pytest.mark.parametrize("restricted", ["worker", "executor"])
def test_set_global_writer_denial(tmp_path, monkeypatch, capsys, restricted):
    import json

    from agent_py_agent.agent.common import global_write_guard as guard
    p = _write_config(tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr(guard, "_restricted_writer", lambda: object() if restricted == "worker" else None)
    monkeypatch.setattr(guard, "_restricted_executor", restricted == "executor")
    assert C.cmd_config_set(Namespace(key="feishu_app_id", value="new", config=str(p))) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["executed"] is False
    assert report["code"] == "GLOBAL_WRITE_IN_" + restricted.upper()
    assert "权限" in report["error"]
    assert p.read_bytes() == before
    assert not (p.parent / "settings-changes.jsonl").exists()


# LLM: 未知异常可能发生于写入后；不得用权限兜底将它谎报为未执行。
# 函数用途: 确保未知服务故障仍传给上层而非被吞掉。
def test_set_unknown_service_error_is_not_denial(tmp_path, monkeypatch):
    from unittest.mock import Mock
    p = _write_config(tmp_path)
    monkeypatch.setattr(C, "set_parameter", Mock(side_effect=RuntimeError("result unknown")))
    with pytest.raises(RuntimeError, match="result unknown"):
        C.cmd_config_set(Namespace(key="feishu_app_id", value="new", config=str(p)))
