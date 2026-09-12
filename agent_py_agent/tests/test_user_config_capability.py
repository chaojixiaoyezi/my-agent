"""LLM: 用户级配置能力的合同单测（R249）。

真机问题：用户问"compact 阈值能不能改"，模型没有任何入口、也不知道用户配置在哪，于是凭空答
"我没有修改 compact 阈值的工具或权限"。本文件守住三条：白名单外一律拒、安全边界结构性拒、
读取必须区分"用户配置"与"随包默认"。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.settings.user_config_capability import (
    BOUNDARY_KEYS,
    TUNABLE_KEYS,
    read_config_fact,
    set_tunable_value,
)


def _files(tmp_path: Path) -> tuple[Path, Path]:
    user = tmp_path / "desktop.yaml"
    packaged = tmp_path / "agent_config.yaml"
    user.write_text("my_agent_home: /tmp/home\n", encoding="utf-8")
    packaged.write_text(
        "my_agent_home: /tmp/home\nmemory_compact_auto_trigger_percent: 90\n",
        encoding="utf-8",
    )
    return user, packaged


# LLM: 安全边界必须结构性拒绝，且给出原因——模型不能靠"试试看"绕过。
# 函数用途: 验证权限/凭据类键永远不可写。
def test_boundary_keys_are_never_writable(tmp_path):
    user, _packaged = _files(tmp_path)
    for key in ("access_mode", "api_key", "my_agent_home"):
        assert key in BOUNDARY_KEYS
        report = set_tunable_value(key, "full-access", user_path=user)
        assert report["ok"] is False
        assert "安全边界" in report["error"]


# LLM: 白名单内的键要校验取值区间，越界/非数字都必须拒绝并说明合法范围。
# 函数用途: 验证取值校验。
def test_tunable_value_is_validated(tmp_path):
    user, _packaged = _files(tmp_path)
    assert set_tunable_value("memory_compact_auto_trigger_percent", "150", user_path=user)["ok"] is False
    assert set_tunable_value("memory_compact_auto_trigger_percent", "abc", user_path=user)["ok"] is False
    report = set_tunable_value("memory_compact_auto_trigger_percent", "80", user_path=user)
    assert report["ok"] is True
    assert report["saved"] == "80"
    assert report["written_value_matches"] is True
    assert "生效" in report["effect_text"]
    assert "80" in user.read_text(encoding="utf-8")


# LLM: 生效值来源必须如实区分：用户配置写了就是 user_config，没写才回落 packaged_default。
# 模型把随包默认当用户配置答出来，正是真机里那次错误回答的形态。
# 函数用途: 验证来源标注。
def test_source_distinguishes_user_and_packaged(tmp_path):
    user, packaged = _files(tmp_path)
    fact = read_config_fact(
        "memory_compact_auto_trigger_percent", user_path=user, default_path=packaged
    )
    assert fact["source"] == "packaged_default"
    assert fact["user_value"] is None

    set_tunable_value("memory_compact_auto_trigger_percent", "70", user_path=user)
    fact = read_config_fact(
        "memory_compact_auto_trigger_percent", user_path=user, default_path=packaged
    )
    assert fact["source"] == "user_config"
    assert fact["effective"] == "70"


# LLM: 没有用户配置文件位置时不能退化成"写随包默认"，必须拒绝并告诉人工编辑哪一份。
# 函数用途: 验证缺用户配置路径时拒绝写入。
def test_missing_user_path_refuses_write(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    report = set_tunable_value("memory_compact_auto_trigger_percent", "80", user_path=None)
    assert report["ok"] is False
    assert "MY_AGENT_CONFIG" in report["error"]
    assert "memory_compact_auto_trigger_percent" in TUNABLE_KEYS
