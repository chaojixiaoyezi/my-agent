# LLM: 只使用合成模型和临时目录；v6 允许未知窗口及真实小窗口，不把默认能力从其它模型带入。
# 模块用途: 核对配置迁入唯一模型目录后的容量、能力与采样边界。
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from agent_py_agent.agent.settings.model_provider_schema import (
    SCHEMA,
    ModelProfileError,
    resolved_model,
    validate_model,
    validate_model_profile,
    validate_provider,
)


# LLM: runtime 载体保留内部投影，但旧连接字段不得再从 YAML/owner/task 层进入公开配置。
# 函数用途: 验证模型十六字段退出平铺持久入口，推理强度仍是独立用户偏好。
def test_config_profile_fields_are_host_projections_not_public_config():
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.config_sources import public_config_keys

    keys = public_config_keys(AgentConfig)
    assert not set(_legacy_model()) & keys
    assert "model_reasoning_effort" in keys


# LLM: 未选目录模型就未配置，不把旧部署连接变成第二套默认；显式 echo 仍是离线调试。
# 函数用途: 验证正式选择入口不再回落旧配置，并保持显式离线模式。
def test_config_profile_empty_selection_does_not_fall_back_to_deployment(tmp_path):
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.model_profiles import selected_model_config_for_resources

    home = _migration_home(tmp_path)
    base = AgentConfig(model_backend="openai_compatible", model_name="retired-model",
                       api_base="https://example.invalid/v1", api_key="retired-static-fixture")
    resolved = selected_model_config_for_resources(base, home)
    assert resolved.model_backend == "" and resolved.model_name == "" and resolved.api_key == ""
    assert base.api_key == "retired-static-fixture"
    echo = AgentConfig(model_backend="echo")
    assert selected_model_config_for_resources(echo, home).model_backend == "echo"


# LLM: 固定假地址与假密钥，只生成纯数据，不建后端、不联网、不读取真实配置。
# 函数用途: 为每个边界用例生成独立的模型输入。
def _model(**changes):
    return {"provider_id": "example", "model_name": "synthetic-model", "model_backend": "openai_compatible",
            "model_context_window_tokens": 128000, **changes}


# LLM: 新目录写入必须声明新版本，不能在 v5 内偷偷放宽容量合同。
# 函数用途: 固定本批模型目录版本。
def test_config_profile_current_schema_is_v6():
    assert SCHEMA == "owner_model_profiles.v6"


# LLM: null 是未声明；1..4095 是真实容量，禁止夹成 4096 或持久化临时构造默认值。
# 函数用途: 验证新目录保留窗口原意。
@pytest.mark.parametrize("window", [None, 1, 4095, 4096, 128000, 2**31 - 1])
def test_config_profile_v6_window_is_nullable_or_positive(window):
    assert validate_model(_model(model_context_window_tokens=window))["model_context_window_tokens"] == window


# LLM: 0 只由离线旧输入迁移转 null，当前 schema 拒绝它；bool、非整数和溢出都不能伪装容量。
# 函数用途: 确保新写入的窗口严格有效。
@pytest.mark.parametrize("window", [0, -1, True, False, 1.5, float("nan"), 2**31, [], {}])
def test_config_profile_v6_invalid_window_is_rejected(window):
    with pytest.raises(ModelProfileError):
        validate_model(_model(model_context_window_tokens=window))


# LLM: 开关跟随当前档案；缺省 auto 也必须在解析快照中明示，不能继承另一个模型的 on/off。
# 函数用途: 验证模型新增、规范化和解析均保留能力开关。
@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_config_profile_reasoning_update_items_round_trip(mode):
    row = validate_model_profile({**_model(reasoning_update_items=mode),
                                  "api_base": "https://example.invalid/v1", "api_key": "synthetic-test-key"})
    provider = validate_provider({"display_name": "example", "api_base": row["api_base"], "api_key": row["api_key"]})
    model = validate_model({**row, "provider_id": "example"})
    result = resolved_model({"profiles": {"chosen": model}, "providers": {"example": provider}}, "chosen")
    assert result["model_reasoning_update_items"] == mode


# LLM: schema 不静默丢掉非法能力声明；决策协议也不能接受生成侧开关。
# 函数用途: 拒绝误拼、布尔或不适用的能力设置。
@pytest.mark.parametrize("value", [True, False, "invalid", 1, [], {}])
def test_config_profile_reasoning_update_items_invalid(value):
    with pytest.raises(ModelProfileError):
        validate_model(_model(reasoning_update_items=value))


# LLM: 缺省/null 不发采样，显式 0 必须保留；所有解析字段都独立于旧部署模型。
# 函数用途: 验证解析快照会主动清除未声明的采样和模态。
@pytest.mark.parametrize("sampling", [{}, {"temperature": None, "top_p": None}, {"temperature": 0, "top_p": 0}])
def test_config_profile_snapshot_contains_explicit_sampling_absence(sampling):
    provider = validate_provider({"display_name": "example", "api_base": "https://example.invalid/v1", "api_key": "synthetic-test-key"})
    row = validate_model(_model(**sampling))
    result = resolved_model({"profiles": {"chosen": row}, "providers": {"example": provider}}, "chosen")
    assert "temperature" in result and "top_p" in result and "model_input_modalities" in result
    assert result["model_input_modalities"] == []
    assert result["temperature"] == ("0.0" if sampling.get("temperature") == 0 else None)
    assert result["top_p"] == sampling.get("top_p")


# LLM: 只读旧目录只在内存升级，保 UUID、选择与随机代次；不落盘、不生成第二份目录。
# 函数用途: 确保 v5 读取迁到 v6 不改原件或覆盖已选模型。
def test_config_profile_v5_read_migrates_without_rewriting(tmp_path):
    from agent_py_agent.agent.settings.decision_settings_schema import empty_decision_settings
    from agent_py_agent.agent.settings.model_profiles import read_model_profiles

    key, generation = str(uuid4()), uuid4().hex
    provider = validate_provider({"display_name": "example", "api_base": "https://example.invalid/v1", "api_key": "synthetic-test-key"})
    original = {"schema": "owner_model_profiles.v5", "selected": key, "catalog_generation": generation,
                "profiles": {key: validate_model(_model())}, "providers": {"example": provider},
                "decision_settings": empty_decision_settings()}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    before = path.read_bytes()
    actual = read_model_profiles(path)
    assert actual["schema"] == "owner_model_profiles.v6"
    assert actual["selected"] == key and actual["catalog_generation"] == generation
    assert path.read_bytes() == before


# LLM: 只有临时 Home 的完整管理员身份；不复用真实部署根或系统环境里的凭据。
# 函数用途: 构造离线导入测试的准确宿主路径。
def _migration_home(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.user_space.home_layout import home_paths

    return replace(home_paths(tmp_path), owner_provider="local", owner_kind="main", owner_id="main")


# LLM: 保留全部十六个原字段的结构值；凭据值均为合成输入，测试不联网。
# 函数用途: 提供可完整迁入 provider/model 的旧部署样本。
def _legacy_model():
    return {"model_backend": "openai_compatible", "api_base": "https://example.invalid/v1",
            "api_key": "unused-static-fixture", "api_key_env": "E905_IMPORT_KEY", "model_name": "synthetic-model",
            "model_context_window_tokens": 4, "model_queue_wait_seconds": 0,
            "model_custom_headers": {"x-fixture": "header-fixture"}, "model_session_header": "x-session-id",
            "model_input_modalities": ["text"], "temperature": 0, "top_p": 0,
            "model_reasoning_control": "effort", "model_reasoning_levels": ["low", "high"],
            "model_reasoning_update_items": "off", "model_structured_output": "json_object"}


# LLM: 只有显式调用才写原目录；普通读不导入，环境只存引用，复用原 selected-model 解析链而不是另造运行配置。
# 函数用途: 验证十六字段和决策覆盖一次导入后能由正式模型选择入口读回。
def test_config_profile_offline_import_reuses_catalog_and_real_selected_reader(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.model_credentials import import_legacy_owner_configuration
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
        selected_model_config_for_resources,
    )

    home = _migration_home(tmp_path)
    monkeypatch.setenv("E905_IMPORT_KEY", "environment-fixture")
    result = import_legacy_owner_configuration(
        home, agent_values={**_legacy_model(), "decision_enabled": True},
        capability_values={"decision_skill_tool_mode": "observe"},
        environ={"E905_IMPORT_KEY": "environment-fixture"}, expected_generation=None,
    )
    path = model_profiles_path(home)
    saved = path.read_text()
    assert "env:E905_IMPORT_KEY" in saved
    assert "environment-fixture" not in saved and "unused-static-fixture" not in saved
    assert not (home.root / "system" / "model_credentials").exists()
    data = read_model_profiles(path)
    assert data["selected"] == result["profile_id"]
    assert data["decision_settings"]["overrides"] == {"enabled": True, "points.skill_tool.mode": "observe"}
    config = selected_model_config_for_resources(AgentConfig(), home)
    assert config.api_key == "environment-fixture" and config.api_key_env == "E905_IMPORT_KEY"
    assert config.model_context_window_tokens == 4 and config.model_queue_wait_seconds == 0
    assert config.temperature == "0.0" and config.top_p == 0
    assert config.model_custom_headers == {"x-fixture": "header-fixture"}
    assert config.model_session_header == "x-session-id" and config.model_input_modalities == ["text"]
    assert config.model_reasoning_update_items == "off" and config.model_structured_output == "json_object"
    before = path.read_bytes()
    again = import_legacy_owner_configuration(
        home, agent_values={**_legacy_model(), "decision_enabled": True},
        capability_values={"decision_skill_tool_mode": "observe"},
        environ={"E905_IMPORT_KEY": "environment-fixture"}, expected_generation=data["catalog_generation"],
    )
    assert again["changed"] is False and path.read_bytes() == before


# LLM: 旧窗口0表示未声明，只允许离线转null；环境为空时静态值保留，写事务仍受准确版本和身份门限制。
# 函数用途: 验证迁移边界不会丢静态密钥或覆盖并发版本。
def test_config_profile_import_static_unknown_window_and_stale_cas(tmp_path):
    from agent_py_agent.agent.settings.model_credentials import import_legacy_owner_configuration
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )

    home = _migration_home(tmp_path)
    legacy = {**_legacy_model(), "model_context_window_tokens": 0}
    result = import_legacy_owner_configuration(home, agent_values=legacy, capability_values={}, environ={}, expected_generation=None)
    path = model_profiles_path(home)
    data = read_model_profiles(path)
    model = data["profiles"][result["profile_id"]]
    assert model["model_context_window_tokens"] is None
    assert data["providers"][model["provider_id"]]["api_key"] == "unused-static-fixture"
    before = path.read_bytes()
    with pytest.raises(ModelProfileError):
        import_legacy_owner_configuration(home, agent_values=legacy, capability_values={}, environ={}, expected_generation=None)
    assert path.read_bytes() == before


# LLM: 明确 echo 不需要虚构网络地址或密钥；仍落原目录并经同一选择解析，空配置绝不能自动选 echo。
# 函数用途: 验证 headless 旧离线模式有可导入的新目录表示。
def test_config_profile_explicit_echo_import_is_offline_catalog(tmp_path):
    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.model_credentials import import_legacy_owner_configuration
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
        selected_model_config_for_resources,
    )

    home = _migration_home(tmp_path)
    report = import_legacy_owner_configuration(home, agent_values={"model_backend": "echo"},
                                              capability_values={}, environ={}, expected_generation=None)
    catalog = read_model_profiles(model_profiles_path(home))
    assert catalog["selected"] == report["profile_id"]
    config = selected_model_config_for_resources(AgentConfig(), home)
    assert config.model_backend == "echo" and config.api_base == "" and config.api_key == ""
    assert get_backend(config.model_backend, config).generate("offline").text


# LLM: 原 mini-YAML 的留空窗口读作空列表，只在离线导入转 null；当前 v6 写入仍严格拒绝列表。
# 函数用途: 验证旧空窗口迁移不制造显式默认容量。
def test_config_profile_legacy_empty_window_is_null(tmp_path):
    from agent_py_agent.agent.settings.model_credentials import import_legacy_owner_configuration
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )

    home = _migration_home(tmp_path)
    result = import_legacy_owner_configuration(home, agent_values={**_legacy_model(), "model_context_window_tokens": []},
                                              capability_values={}, environ={}, expected_generation=None)
    data = read_model_profiles(model_profiles_path(home))
    assert data["profiles"][result["profile_id"]]["model_context_window_tokens"] is None


# LLM: Echo 是明确的离线分支，不能夹带任何网络/认证输入并静默丢弃，拒绝时不写目录。
# 函数用途: 验证离线导入不会吞掉不兼容的旧连接信息。
@pytest.mark.parametrize("extra", [{"api_base": "https://fixture.invalid"}, {"api_key": "fake-key"},
                                   {"api_key_env": "E905_FAKE_KEY"}, {"model_custom_headers": {"X-Test": "fixture"}},
                                   {"model_session_header": "X-Session"}])
def test_config_profile_echo_rejects_network_or_credential_fields(tmp_path, extra):
    from agent_py_agent.agent.settings.model_credentials import import_legacy_owner_configuration
    from agent_py_agent.agent.settings.model_profiles import model_profiles_path

    home = _migration_home(tmp_path)
    with pytest.raises(ModelProfileError):
        import_legacy_owner_configuration(home, agent_values={"model_backend": "echo", **extra},
                                          capability_values={}, environ={}, expected_generation=None)
    assert not model_profiles_path(home).exists()


# LLM: 离线不等于忽略模型选项，显式窗口和零采样值仍按同一目录 schema 保存。
# 函数用途: 验证 Echo 导入保留合法的模型参数。
def test_config_profile_echo_preserves_explicit_model_options(tmp_path):
    from agent_py_agent.agent.settings.model_credentials import import_legacy_owner_configuration
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )

    home = _migration_home(tmp_path)
    report = import_legacy_owner_configuration(home, agent_values={"model_backend": "echo", "model_context_window_tokens": 4096,
                                                "top_p": 0, "model_reasoning_update_items": "off"},
                                              capability_values={}, environ={}, expected_generation=None)
    model = read_model_profiles(model_profiles_path(home))["profiles"][report["profile_id"]]
    assert model["model_context_window_tokens"] == 4096 and model["top_p"] == 0 and model["reasoning_update_items"] == "off"
