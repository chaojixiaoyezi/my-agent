"""W0 合同测试：自学习与教训归档默认策略、V2-1 参数可调登记、派生效果规则及 LiveSwitches 零重启热刷新。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.cache_freshness import CACHE_TRUST_AGE_SECONDS
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import settings_control_service
from agent_py_agent.agent.settings import live_switches as live
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.live_switches import (
    LIVE_SWITCH_KEYS,
    SELF_LEARNING_LIVE_POLICY_KEYS,
    LiveSwitches,
    refresh_live_switches,
    register_live_config,
)
from agent_py_agent.agent.settings.memory import MemorySettings, normalize_memory_settings
from agent_py_agent.agent.settings.normalize import normalize_agent_config
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin, WritePaths, set_parameter
from agent_py_agent.agent.settings.parameter_registry import (
    SAFETY_FREE,
    applied_value,
    applied_value_with,
    parameter_registry,
)
from agent_py_agent.agent.settings.user_config_capability import (
    BOUNDARY_KEYS,
    EFFECT_IMMEDIATE,
    TUNABLE_KEYS,
    USER_SETTINGS_BOUNDARY_KEYS,
    packaged_config_path,
)
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

_EXPECTED_DEFAULTS: dict[str, bool | int | str] = {
    "enable_self_learning": True,
    "self_learning_maintenance_mode": "autonomous",
    "self_learning_max_skills": 50,
    "self_learning_recommended_limit": 50,
    "self_learning_recommended_explore_percent": 20,
    "self_learning_merge_similarity_percent": 70,
    "self_learning_keep_versions": 5,
    "self_learning_cool_seconds": 604800,
    "self_learning_archive_seconds": 1296000,
    "self_learning_evict_seconds": 1296000,
    "memory_lesson_archive_days": 15,
}

_ADMIN_HOME = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
_NON_ADMIN_HOME = SimpleNamespace(owner_provider="feishu", owner_kind="users", owner_id="ou_non_admin")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    return home


def _write_aged_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    aged = time.time() - CACHE_TRUST_AGE_SECONDS - 1.0
    os.utime(path, (aged, aged))


def test_shipped_defaults_match_agent_config_memory_settings_and_packaged_yaml():
    packaged = load_config(packaged_config_path())
    agent_defaults = AgentConfig()
    memory_defaults = MemorySettings()

    assert set(_EXPECTED_DEFAULTS) == set(SELF_LEARNING_LIVE_POLICY_KEYS)
    for key, expected in _EXPECTED_DEFAULTS.items():
        assert getattr(packaged, key) == expected, key
        assert type(getattr(packaged, key)) is type(expected), key
        assert getattr(agent_defaults, key) == expected, key
        assert type(getattr(agent_defaults, key)) is type(expected), key
    assert memory_defaults.memory_lesson_archive_days == 15


def test_explicit_false_observe_and_zero_disable_settings_are_preserved(tmp_path: Path):
    path = tmp_path / "explicit_zero.yaml"
    _write_aged_yaml(
        path,
        "\n".join([
            "enable_self_learning: false",
            "self_learning_maintenance_mode: observe",
            "self_learning_max_skills: 0",
            "self_learning_recommended_limit: 0",
            "self_learning_recommended_explore_percent: 0",
            "self_learning_merge_similarity_percent: 0",
            "self_learning_keep_versions: 0",
            "self_learning_cool_seconds: 0",
            "self_learning_archive_seconds: 0",
            "self_learning_evict_seconds: 0",
            "memory_lesson_archive_days: 0",
        ]) + "\n",
    )
    loaded = load_config(path)
    assert loaded.config_warnings == []
    assert loaded.memory_config_warnings == []
    assert loaded.enable_self_learning is False
    assert loaded.self_learning_maintenance_mode == "observe"
    for zero_key in (
        "self_learning_max_skills",
        "self_learning_recommended_limit",
        "self_learning_recommended_explore_percent",
        "self_learning_merge_similarity_percent",
        "self_learning_keep_versions",
        "self_learning_cool_seconds",
        "self_learning_archive_seconds",
        "self_learning_evict_seconds",
        "memory_lesson_archive_days",
    ):
        assert getattr(loaded, zero_key) == 0, zero_key


def test_invalid_yaml_and_bool_as_int_inputs_fall_back_safely_with_warnings():
    raw = {
        "enable_self_learning": "maybe",
        "self_learning_maintenance_mode": "invalid_mode",
        "self_learning_max_skills": True,
        "self_learning_recommended_limit": False,
        "self_learning_recommended_explore_percent": 101,
        "self_learning_merge_similarity_percent": -1,
        "self_learning_keep_versions": "2.5",
        "self_learning_cool_seconds": -5,
        "self_learning_archive_seconds": True,
        "self_learning_evict_seconds": -10,
    }
    normalized, warnings = normalize_agent_config(raw)
    assert normalized["enable_self_learning"] is True
    assert normalized["self_learning_maintenance_mode"] == "autonomous"
    for key in (
        "self_learning_max_skills",
        "self_learning_recommended_limit",
        "self_learning_recommended_explore_percent",
        "self_learning_merge_similarity_percent",
        "self_learning_keep_versions",
        "self_learning_cool_seconds",
        "self_learning_archive_seconds",
        "self_learning_evict_seconds",
    ):
        assert normalized[key] == _EXPECTED_DEFAULTS[key], key
    assert len(warnings) == len(raw)

    for bad_days in (True, False, -1, 3651, "1.5", "not-an-int"):
        mem, mem_warnings = normalize_memory_settings({"memory_lesson_archive_days": bad_days})
        assert mem.memory_lesson_archive_days == 15
        assert len(mem_warnings) == 1

    boundary_min, w_min = normalize_memory_settings({"memory_lesson_archive_days": 0})
    boundary_max, w_max = normalize_memory_settings({"memory_lesson_archive_days": 3650})
    assert (boundary_min.memory_lesson_archive_days, w_min) == (0, [])
    assert (boundary_max.memory_lesson_archive_days, w_max) == (3650, [])


def test_v2_1_tunable_contracts_parameter_registry_and_applied_rules():
    assert "enable_self_learning" not in BOUNDARY_KEYS
    assert "self_learning_maintenance_mode" not in BOUNDARY_KEYS
    assert "enable_self_learning" not in USER_SETTINGS_BOUNDARY_KEYS
    assert "self_learning_maintenance_mode" not in USER_SETTINGS_BOUNDARY_KEYS

    registry = parameter_registry()
    for key, default_value in _EXPECTED_DEFAULTS.items():
        assert key in TUNABLE_KEYS, key
        assert TUNABLE_KEYS[key].effect == EFFECT_IMMEDIATE, key
        assert (key in LIVE_SWITCH_KEYS) == isinstance(default_value, bool), key
        assert key in SELF_LEARNING_LIVE_POLICY_KEYS, key

        spec = registry[key]
        assert spec.default == default_value, key
        assert spec.safety == SAFETY_FREE, key
        assert spec.writable is True, key
        assert spec.user_editable is True, key
        assert spec.effect == EFFECT_IMMEDIATE, key
        assert spec.description, key
        if spec.value_type != "bool":
            assert spec.range, key

    # 校验 TUNABLE_KEYS 对边界、枚举与非整数的严格拒绝
    assert TUNABLE_KEYS["self_learning_maintenance_mode"].validate("OBSERVE") == (True, "", "observe")
    assert TUNABLE_KEYS["self_learning_maintenance_mode"].validate("autonomous") == (True, "", "autonomous")
    assert TUNABLE_KEYS["self_learning_maintenance_mode"].validate("invalid")[0] is False
    assert TUNABLE_KEYS["self_learning_maintenance_mode"].validate(True)[0] is False
    for pct_key in ("self_learning_recommended_explore_percent", "self_learning_merge_similarity_percent"):
        assert TUNABLE_KEYS[pct_key].validate("0") == (True, "", 0)
        assert TUNABLE_KEYS[pct_key].validate("100") == (True, "", 100)
        assert TUNABLE_KEYS[pct_key].validate("-1")[0] is False
        assert TUNABLE_KEYS[pct_key].validate("101")[0] is False
        assert TUNABLE_KEYS[pct_key].validate(True)[0] is False
        assert TUNABLE_KEYS[pct_key].validate("1_0")[0] is False
    assert TUNABLE_KEYS["memory_lesson_archive_days"].validate("0") == (True, "", 0)
    assert TUNABLE_KEYS["memory_lesson_archive_days"].validate("3650") == (True, "", 3650)
    assert TUNABLE_KEYS["memory_lesson_archive_days"].validate("3651")[0] is False
    assert TUNABLE_KEYS["self_learning_evict_seconds"].validate("-1")[0] is False
    assert TUNABLE_KEYS["self_learning_cool_seconds"].validate("1.5")[0] is False

    # 校验 _APPLIED_RULES 派生效果计算（正常运行 vs observe vs paused vs recommended_limit=0）
    active_cfg = AgentConfig()
    for key, expected in _EXPECTED_DEFAULTS.items():
        derived = applied_value(key, active_cfg)
        assert derived is not None, key
        assert derived[0] == expected and derived[1], key

    observe_cfg = AgentConfig(self_learning_maintenance_mode="observe")
    assert applied_value("self_learning_maintenance_mode", observe_cfg)[0] == "observe"
    for auto_key in (
        "self_learning_cool_seconds",
        "self_learning_archive_seconds",
        "self_learning_evict_seconds",
        "self_learning_merge_similarity_percent",
        "self_learning_keep_versions",
    ):
        assert applied_value(auto_key, observe_cfg)[0] == 0, auto_key

    paused_cfg = AgentConfig(enable_self_learning=False)
    assert applied_value("enable_self_learning", paused_cfg)[0] is False
    assert applied_value("self_learning_maintenance_mode", paused_cfg)[0] == "paused"
    assert applied_value("self_learning_evict_seconds", paused_cfg)[0] == 0

    uncapped_catalog = AgentConfig(self_learning_recommended_limit=0, self_learning_recommended_explore_percent=20)
    assert applied_value("self_learning_recommended_explore_percent", uncapped_catalog)[0] == 0
    assert applied_value_with("self_learning_cool_seconds", "0", active_cfg)[0] == 0
    assert applied_value_with("self_learning_maintenance_mode", "observe", active_cfg)[0] == "observe"


def test_live_switches_reloads_all_policy_fields_with_three_way_merge_and_stat_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "agent.yaml"
    _write_aged_yaml(path, "agent_name: live-policy\n")
    base = load_config(path)
    overridden = replace(base, self_learning_max_skills=12, memory_lesson_archive_days=3)
    unregistered = replace(base, config_path="", self_learning_cool_seconds=99)

    LiveSwitches.register(base)
    LiveSwitches.register(overridden)
    LiveSwitches.register(unregistered)

    # 未改写老文件时只 stat 不重读配置
    load_calls = 0
    orig_load = live.load_config

    def _counting_load(target):
        nonlocal load_calls
        load_calls += 1
        return orig_load(target)

    monkeypatch.setattr(live, "load_config", _counting_load)
    assert LiveSwitches.refresh(base) == frozenset()
    assert load_calls == 0

    updated = {
        "enable_self_learning": False,
        "self_learning_maintenance_mode": "observe",
        "self_learning_max_skills": 80,
        "self_learning_recommended_limit": 25,
        "self_learning_recommended_explore_percent": 0,
        "self_learning_merge_similarity_percent": 85,
        "self_learning_keep_versions": 3,
        "self_learning_cool_seconds": 120,
        "self_learning_archive_seconds": 240,
        "self_learning_evict_seconds": 0,
        "memory_lesson_archive_days": 30,
    }
    _write_aged_yaml(
        path,
        "\n".join(
            f"{key}: {str(val).lower() if isinstance(val, bool) else val}"
            for key, val in updated.items()
        ) + "\n",
    )
    changed = refresh_live_switches(base)
    assert load_calls == 1
    assert set(updated).issubset(changed)
    for key, val in updated.items():
        assert getattr(base, key) == val and type(getattr(base, key)) is type(val), key

    # 运行时显式 replace 覆盖保留，非覆盖字段跟随文件热更新
    assert overridden.self_learning_max_skills == 12
    assert overridden.memory_lesson_archive_days == 3
    assert overridden.enable_self_learning is False
    assert overridden.self_learning_maintenance_mode == "observe"
    assert overridden.self_learning_evict_seconds == 0
    assert unregistered.self_learning_cool_seconds == 99

    # 切回开启与非零宽限 + 越界 memory_lesson_archive_days 自动规范化
    _write_aged_yaml(
        path,
        "\n".join([
            "enable_self_learning: true",
            "self_learning_maintenance_mode: autonomous",
            "self_learning_evict_seconds: 86400",
            "memory_lesson_archive_days: 9999",
        ]) + "\n",
    )
    changed_back = refresh_live_switches(base)
    assert {"enable_self_learning", "self_learning_maintenance_mode", "self_learning_evict_seconds", "memory_lesson_archive_days"} <= changed_back
    assert base.enable_self_learning is True
    assert base.self_learning_maintenance_mode == "autonomous"
    assert base.self_learning_evict_seconds == 86400
    assert base.memory_lesson_archive_days == 15
    assert any(item["field_name"] == "memory_lesson_archive_days" for item in base.memory_config_warnings)


def test_main_owner_user_config_tool_and_settings_take_effect_immediately_while_non_main_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "desktop.yaml"
    _write_aged_yaml(path, 'agent_name: "w0"\n')
    config = load_config(path)
    register_live_config(config)

    main_agent = SimpleNamespace(home_paths=_ADMIN_HOME, config=config)
    tool = UserConfigTool(main_agent)

    # 主会话 UserConfigTool 依次修改 bool / enum / int 策略项并马上热刷新生效
    for key, raw_value, expected in (
        ("enable_self_learning", "false", False),
        ("self_learning_maintenance_mode", "observe", "observe"),
        ("self_learning_max_skills", "0", 0),
        ("self_learning_recommended_explore_percent", "35", 35),
        ("self_learning_merge_similarity_percent", "80", 80),
        ("self_learning_evict_seconds", "0", 0),
        ("memory_lesson_archive_days", "30", 30),
    ):
        outcome = tool.execute({"action": "set", "key": key, "value": raw_value, "reason": "w0 test"})
        assert outcome.ok, (key, outcome.output)
        receipt = json.loads(outcome.output)
        assert receipt["effect_when"] == EFFECT_IMMEDIATE
        assert "applied_value" in receipt
        changed = refresh_live_switches(main_agent.config)
        assert key in changed
        assert getattr(main_agent.config, key) == expected

    # /settings 控制指令写入并立即热刷新生效
    monkeypatch.setattr(settings_control_service, "_scoped_home", lambda _agent, _scope: _ADMIN_HOME)
    cmd = parse_conversation_control("/settings set self_learning_maintenance_mode autonomous", reject_unknown_slash=True)
    assert cmd is not None
    res = settings_control_service.execute_settings_control(main_agent, cmd, None)
    assert res.ok, res.message
    assert "self_learning_maintenance_mode" in refresh_live_switches(main_agent.config)
    assert main_agent.config.self_learning_maintenance_mode == "autonomous"

    # 回滚/恢复也即刻生效且参数事务拒收非法值无副作用
    invalid = set_parameter("self_learning_recommended_explore_percent", "105", paths=WritePaths(user_path=path), origin=ChangeOrigin("model"))
    assert invalid["ok"] is False and invalid["code"] == "PARAMETER_INVALID"

    # 非主拥有者 / 子上下文不得经 UserConfigTool 或 /settings 修改本机全局策略
    before_bytes = path.read_bytes()
    denied_tool = UserConfigTool(SimpleNamespace(home_paths=_NON_ADMIN_HOME, config=config))
    for key, val in (("enable_self_learning", "true"), ("self_learning_maintenance_mode", "observe")):
        refusal = denied_tool.execute({"action": "set", "key": key, "value": val})
        assert refusal.ok is False
        assert refusal.error_code == "TOOL_PERMISSION_DENIED"
        assert refusal.effect_outcome == "not_started"
    monkeypatch.setattr(settings_control_service, "_scoped_home", lambda _agent, _scope: _NON_ADMIN_HOME)
    denied_cmd = settings_control_service.execute_settings_control(
        SimpleNamespace(config=config),
        parse_conversation_control("/settings set enable_self_learning true", reject_unknown_slash=True),
        None,
    )
    assert denied_cmd.ok is False
    assert path.read_bytes() == before_bytes
