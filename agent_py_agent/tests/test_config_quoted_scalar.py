"""真 bug 回归:自建 YAML 的 parse_scalar 必须尊重引号语义——带引号 = 字符串,不做 int 推断。

原问题(真机 QQ 接入暴露):qq_app_id: "1903943966" 在 parse_scalar 里被先无条件剥引号、再 int() 成
整数 1903943966,随后 string 字段归一 _string_config_value(int) 把它丢成空字符串 → QQ app_id 加载为
空 → 机器人鉴权失败"不在线"。qq_app_secret 因含字母 int() 失败才侥幸存活。这会静默打击一切纯数字字符串
配置(QQ 号/手机号/账号/邮编)。修复:① parse_scalar 见到成对外层引号即判定字符串,原样返回不推断;
② _string_config_value 对非 bool 的 int/float 兜底转字符串而非丢空。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.config_io import load_simple_yaml, parse_scalar
from agent_py_agent.agent.settings.services._normalize import _string_config_value


def test_parse_scalar_quoted_digits_stay_string() -> None:
    """带引号的纯数字 = 用户显式要字符串,不能被 int 化。"""
    assert parse_scalar('"1903943966"') == "1903943966"
    assert parse_scalar("'1903943966'") == "1903943966"
    assert isinstance(parse_scalar('"1903943966"'), str)


def test_parse_scalar_bare_digits_still_int() -> None:
    """裸数字(无引号)仍是 int——端口/间隔/计数等 int 字段不回归。"""
    assert parse_scalar("8421") == 8421
    assert isinstance(parse_scalar("8421"), int)
    assert parse_scalar("-5") == -5


def test_parse_scalar_other_types_unaffected() -> None:
    assert parse_scalar('""') == ""  # 显式空字符串
    assert parse_scalar('"hello"') == "hello"
    assert parse_scalar("true") is True
    assert parse_scalar("false") is False
    assert parse_scalar("[1, 2]") == [1, 2]


def test_string_config_value_coerces_number_not_drop() -> None:
    """防御层:string 字段收到数字(没加引号被推断成 int)按字符串还原,而不是静默丢空。"""
    assert _string_config_value("abc") == "abc"
    assert _string_config_value(1903943966) == "1903943966"  # int -> str,不丢空
    assert _string_config_value(3.14) == ""  # 仅 int 兜底;float 不是合法 ID,保持丢空
    assert _string_config_value(True) == ""  # bool 不该变 "True"
    assert _string_config_value(None) == ""


def test_load_config_quoted_numeric_id_survives() -> None:
    """全链路:引号数字 ID 经 load_config 后仍是字符串、值不丢(真机 testbox 配置形态)。"""
    y = 'qq_app_id: "1903943966"\nqq_app_secret: "Sk2LfzKg2PnBa0QrJlEiChDjGnLuT3eF"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(y)
        p = f.name
    try:
        c = load_config(p)
        assert c.qq_app_id == "1903943966"  # 原 bug:加载成空字符串
        assert c.qq_app_secret == "Sk2LfzKg2PnBa0QrJlEiChDjGnLuT3eF"
    finally:
        Path(p).unlink()


def test_load_config_bare_numeric_id_coerced_by_defense() -> None:
    """全链路:即便用户漏写引号(裸数字 ID),防御层兜底也还原成字符串而非丢空。"""
    y = "qq_app_id: 1903943966\n"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(y)
        p = f.name
    try:
        assert load_config(p).qq_app_id == "1903943966"
    finally:
        Path(p).unlink()


def test_load_simple_yaml_quoted_digit_type() -> None:
    """load_simple_yaml 层面:引号数字读成 str,裸数字读成 int。"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write('quoted: "12345"\nbare: 12345\n')
        p = f.name
    try:
        d = load_simple_yaml(Path(p))
        assert d["quoted"] == "12345" and isinstance(d["quoted"], str)
        assert d["bare"] == 12345 and isinstance(d["bare"], int)
    finally:
        Path(p).unlink()
