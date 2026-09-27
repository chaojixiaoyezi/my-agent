# LLM: 已删键只能告警不能拒绝加载；类型错误这类真校验不允许被这条放宽带走。
# 模块用途: 钉住 capability 配置对未知键的处理与主配置一致（告警 + 忽略），已知键与真校验不受影响。
from __future__ import annotations

import pytest
from agent.capability.config import load_capability_config


def _write(tmp_path, body: str):
    path = tmp_path / "capability_config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_removed_decision_keys_load_with_warning(tmp_path) -> None:
    """owner 配置里残留 13u 删掉的决策点位键时，仍能加载，并且留下告警。"""
    path = _write(
        tmp_path,
        "\n".join(
            [
                "decision_model_selection_timeout_seconds: 5",
                "decision_model_selection_profile_id: ''",
                "decision_planning_timeout_seconds: 3",
                "decision_planning_profile_id: ''",
            ]
        )
        + "\n",
    )

    config = load_capability_config(path)

    warnings = list(getattr(config, "config_warnings", []) or [])
    assert any("decision_model_selection_timeout_seconds" in w for w in warnings)
    assert any("decision_planning_profile_id" in w for w in warnings)
    assert len(warnings) == 4


def test_known_keys_still_loaded(tmp_path) -> None:
    """已知键照常生效，别被这条放宽弄坏。"""
    path = _write(tmp_path, "capability_candidate_limit: 9\nenable_capability_routing: true\n")

    config = load_capability_config(path)

    assert config.capability_candidate_limit == 9
    assert config.enable_capability_routing is True
    assert not list(getattr(config, "config_warnings", []) or [])


def test_known_and_removed_keys_together(tmp_path) -> None:
    """混写时：已知键生效，已删键只告警。"""
    path = _write(
        tmp_path,
        "capability_candidate_limit: 7\ndecision_recall_timeout_seconds: 4\n",
    )

    config = load_capability_config(path)

    assert config.capability_candidate_limit == 7
    assert any("decision_recall_timeout_seconds" in w for w in config.config_warnings)


def test_unknown_key_with_value_still_not_fatal(tmp_path) -> None:
    """完全没见过的键也不该拦住加载（主配置同样是告警忽略）。"""
    path = _write(tmp_path, "totally_unknown_key: 1\n")

    config = load_capability_config(path)

    assert config is not None
    assert any("totally_unknown_key" in w for w in config.config_warnings)


def test_decision_settings_validation_still_rejects_bad_values(tmp_path) -> None:
    """真校验不许放宽：仍是当前登记点位的字段，值非法时照旧报错。"""
    path = _write(tmp_path, "decision_subagent_model_mode: not_a_real_mode\n")

    with pytest.raises(Exception) as excinfo:
        load_capability_config(path)

    assert "决策模式只能是" in str(excinfo.value)


def test_removed_key_is_not_validated_but_only_warned(tmp_path) -> None:
    """已删键不再走点位校验，而是落进"未知键告警"这一条；两条路径互不越界。"""
    path = _write(tmp_path, "decision_model_selection_timeout_seconds: -5\n")

    config = load_capability_config(path)

    assert any(
        "decision_model_selection_timeout_seconds" in w for w in config.config_warnings
    )


def test_missing_file_still_raises(tmp_path) -> None:
    """缺文件仍旧是硬错误，没被误放宽。"""
    with pytest.raises(FileNotFoundError):
        load_capability_config(tmp_path / "nope.yaml")
