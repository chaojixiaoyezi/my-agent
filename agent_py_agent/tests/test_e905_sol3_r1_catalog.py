# LLM: 回归只用临时规范目录与合成凭据；表单替身仅替换交互，不替换目录保存和解析主链。
# 模块用途: 验证模型编辑的字段存在性、离线空值解码、环境来源及离线目录一致性。
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import model_control_cards as cards
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.config_io import parse_simple_yaml_text
from agent_py_agent.agent.settings.model_credentials import (
    import_legacy_owner_configuration,
    provider_api_key,
)
from agent_py_agent.agent.settings.model_profiles import (
    _save_profiles,
    execute_model_profile_operation,
    model_profiles_path,
    public_model_profiles,
    read_model_profiles,
    selected_model_config_for_resources,
)
from agent_py_agent.agent.settings.model_provider_schema import (
    ModelProfileError,
    resolved_model,
    validate_model,
    validate_provider,
)
from agent_py_agent.agent.settings.thread_model_selection import (
    execute_local_model_operation,
    thread_model_config,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.cli.chat_parts import tui_provider_menu as tui


# LLM: 不初始化真实宿主或读取用户环境，只构造本次临时目录的完整管理员身份。
# 函数用途: 为正式保存、导入与会话选择提供规范资源。
def _host(tmp_path):
    home = replace(home_paths(tmp_path / "home"), owner_provider="local", owner_kind="main", owner_id="main")
    return SimpleNamespace(home_paths=home, config=AgentConfig(),
                           conversation_store=ConversationStore(home.owner_home_dir))


# LLM: 直接建立合法的测试目录输入，不经被测编辑入口；所有后续操作仍读取原目录。
# 函数用途: 保存一个保留关闭开关和未知窗口的模型及可信服务商。
def _seed(host, *, env=False):
    path = model_profiles_path(host.home_paths)
    data = read_model_profiles(path)
    key = str(uuid4())
    provider = validate_provider({"display_name": "原连接", "api_base": "https://example.invalid/v1",
                                  "api_key": "" if env else "inline-fixture",
                                  **({"api_key_ref": "env:E905_R1_KEY"} if env else {})})
    data["providers"]["p"] = provider
    data["profiles"][key] = validate_model({"provider_id": "p", "model_backend": "openai_compatible",
        "model_name": "fixture", "model_context_window_tokens": None, "reasoning_update_items": "off"})
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_profiles(path, data)
    return key


# LLM: 只返回公开表单字段，不把环境引用或内联秘密回送客户端。
# 函数用途: 构造旧五字段飞书表单的一次普通改名提交。
def _form():
    return {"model_backend": "openai_compatible", "api_base": "https://example.invalid/v1",
            "model_name": "renamed", "model_context_window_tokens": "", "api_key": ""}


# LLM: 空串表示用户未声明容量；列表仍属非法正式输入，不能为迁移放宽在线校验。
# 函数用途: 验证正式模型窗口空值往返与非法结构拒绝。
def test_nullable_window_form_value():
    row = {"provider_id": "p", "model_backend": "openai_compatible", "model_name": "m"}
    assert validate_model({**row, "model_context_window_tokens": ""})["model_context_window_tokens"] is None
    with pytest.raises(ModelProfileError):
        validate_model({**row, "model_context_window_tokens": []})


# LLM: 未出现的字段保留，明确提交自动则清除关闭；不能把缺键当作重置请求。
# 函数用途: 验证服务端模型编辑的存在性感知合并。
def test_save_model_preserves_missing_v6_fields(tmp_path):
    host = _host(tmp_path)
    key = _seed(host)
    execute_model_profile_operation(host, "save_model", {"profile_id": key, "editing": True,
                                                       "profile": {"model_name": "new-name"}})
    model = read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]
    assert model["reasoning_update_items"] == "off" and model["model_context_window_tokens"] is None
    execute_model_profile_operation(host, "save_model", {"profile_id": key, "editing": True,
                                                       "profile": {"reasoning_update_items": "auto"}})
    assert "reasoning_update_items" not in read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]


# LLM: 对话替身驱动真实编辑函数，保存仍走原服务端操作；分别核对预填和提交，防止只修一端。
# 函数用途: 验证终端编辑未知窗口和关闭开关后原目录不漂移。
def test_tui_edit_model_nullable_and_off(tmp_path, monkeypatch):
    host = _host(tmp_path)
    key = _seed(host)
    row = {"id": key, **read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]}
    fields = []
    original_field = tui._field

    # LLM: 保留真实控件的构造与渲染，只登记控件供交互替身读取。
    # 函数用途: 收集真实编辑框以检查窗口预填值。
    def field(*args, **kwargs):
        control = original_field(*args, **kwargs)
        fields.append(control)
        return control

    # LLM: 只替换用户按保存的交互，读取真实控件值，不替换模型字段生成。
    # 函数用途: 检查窗口预填为空并提交保存动作。
    async def dialog(app, title, body, actions, **kwargs):
        assert fields[1].text == ""
        return True

    # LLM: 复用原目录事务，不把被测载荷伪造成成功回执。
    # 函数用途: 核对关闭开关已提交并保存真实表单请求。
    async def request(app, agent, session, operation, payload):
        assert payload["profile"]["reasoning_update_items"] == "off"
        return execute_model_profile_operation(agent, operation, payload)

    monkeypatch.setattr(tui, "_field", field)
    monkeypatch.setattr(tui, "_dialog", dialog)
    monkeypatch.setattr(tui, "_request", request)
    assert asyncio.run(tui._edit_model(None, host, "s", "p", row))
    model = read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]
    assert model["reasoning_update_items"] == "off" and model["model_context_window_tokens"] is None


# LLM: 密钥不预填，未知窗口不能格式化为字符串空值标记。
# 函数用途: 检查飞书表单窗口的可见默认值。
def test_feishu_nullable_prefill():
    card = cards.model_configuration_card("fixture-ref", "测试范围", {"model_context_window_tokens": None})
    form = next(item for item in card["elements"] if item["tag"] == "form")
    window = next(item for item in form["elements"] if item.get("name") == "model_context_window_tokens")
    assert window["default_value"] == ""


# LLM: 父卡生命周期不是本回归目标，替换该前置检查；目录代次、向量保护、保存及凭据解析不替换。
# 函数用途: 验证飞书旧字段表单改名保留可信环境来源和关闭开关。
def test_feishu_edit_env_preserves_credential_and_v6(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import model_control_dispatch

    host = _host(tmp_path)
    key = _seed(host, env=True)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    row = {"profile_id": key, "thread_id": "", "catalog_revisions": [data["catalog_generation"]]}
    monkeypatch.setattr(model_control_dispatch, "model_form_parent_error", lambda *args, **kwargs: None)
    result = cards._save_model_form(host, row, _form())
    assert result["ok"] is True
    saved = read_model_profiles(model_profiles_path(host.home_paths))
    assert saved["profiles"][key]["reasoning_update_items"] == "off"
    assert saved["profiles"][key]["model_context_window_tokens"] is None
    assert saved["providers"]["p"]["api_key_ref"] == "env:E905_R1_KEY"
    monkeypatch.setenv("E905_R1_KEY", "late-fixture")
    assert resolved_model(saved, key)["api_key"] == "late-fixture"


# LLM: 任何普通表单均不能自行创建环境引用；只有服务端锁内既有记录可参与保留。
# 函数用途: 检查快捷模型新增和编辑拒绝客户端注入环境名。
@pytest.mark.parametrize("operation", ["add", "save_profile", "save_provider"])
def test_forms_reject_new_env_reference(tmp_path, operation):
    host = _host(tmp_path)
    key = _seed(host)
    value = {**_form(), "api_key": "", "api_key_ref": "env:UNTRUSTED_FIXTURE"}
    payload = {"profile_id": key, "provider_id": "p", "editing": True,
               "profile": value, "provider": value}
    before = model_profiles_path(host.home_paths).read_bytes()
    with pytest.raises(ModelProfileError):
        execute_model_profile_operation(host, operation, payload)
    assert model_profiles_path(host.home_paths).read_bytes() == before


# LLM: 端点包含地址和传输，变更后旧引用不得跟随；失败必须保留整个原目录。
# 函数用途: 验证服务商改地址或传输时不能留空继承凭据。
@pytest.mark.parametrize("change", [{"api_base": "https://other.invalid/v1"}, {"transport": "offline"}])
def test_provider_endpoint_change_does_not_retain_env(tmp_path, change):
    host = _host(tmp_path)
    _seed(host, env=True)
    before = model_profiles_path(host.home_paths).read_bytes()
    with pytest.raises(ModelProfileError):
        execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
                                                               "provider": {"api_key": "", **change}})
    assert model_profiles_path(host.home_paths).read_bytes() == before


# LLM: 同端点留空只保留服务端原来源，环境暂未就绪不阻止名称编辑。
# 函数用途: 验证服务商改名保留环境引用及新密钥明确替换旧来源。
def test_provider_edit_env_empty_key_then_replace(tmp_path):
    host = _host(tmp_path)
    _seed(host, env=True)
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "provider": {"display_name": "新名称", "api_key": ""}})
    provider = read_model_profiles(model_profiles_path(host.home_paths))["providers"]["p"]
    assert provider["api_key_ref"] == "env:E905_R1_KEY" and provider["display_name"] == "新名称"
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "provider": {"api_key": "replacement-fixture"}})
    provider = read_model_profiles(model_profiles_path(host.home_paths))["providers"]["p"]
    assert provider["api_key"] == "replacement-fixture" and "api_key_ref" not in provider


# LLM: 真实解析器的裸字段会生成空列表，只允许唯一离线解码器归一；数值零不得误判为空。
# 函数用途: 验证旧简化配置经正式导入后空值与零温度语义正确。
@pytest.mark.parametrize("temperature", [None, [], "", 0])
@pytest.mark.parametrize("key", [None, [], ""])
def test_legacy_empty_fields_import(tmp_path, monkeypatch, temperature, key):
    host = _host(tmp_path)
    raw = parse_simple_yaml_text("model_backend: openai_compatible\napi_base: https://example.invalid/v1\n"
        "model_name: imported\napi_key:\napi_key_env: E905_R1_KEY\ntemperature:\nmodel_context_window_tokens:\n")
    assert raw["api_key"] == [] and raw["temperature"] == []
    raw.update(api_key=key, temperature=temperature)
    monkeypatch.setenv("E905_R1_KEY", "import-fixture")
    report = import_legacy_owner_configuration(host.home_paths, agent_values=raw, capability_values={},
        environ={"E905_R1_KEY": "import-fixture"}, expected_generation=None)
    config = selected_model_config_for_resources(host.config, host.home_paths, profile_id=report["profile_id"])
    assert config.api_key == "import-fixture" and config.model_context_window_tokens is None
    assert config.temperature == ("0.0" if temperature == 0 else None)


# LLM: 暂空环境不应擦掉合法来源；注值后不用重迁移即可解析，历史静态值仅为后备。
# 函数用途: 验证环境迟到及历史内联备份的优先级。
@pytest.mark.parametrize("inline", ["", "backup-fixture"])
def test_empty_env_migration_later_resolves(tmp_path, monkeypatch, inline):
    host = _host(tmp_path)
    monkeypatch.delenv("E905_R1_KEY", raising=False)
    report = import_legacy_owner_configuration(host.home_paths, agent_values={"model_backend": "openai_compatible",
        "api_base": "https://example.invalid/v1", "model_name": "imported", "api_key": inline,
        "api_key_env": "E905_R1_KEY"}, capability_values={}, environ={}, expected_generation=None)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    provider = next(iter(data["providers"].values()))
    assert provider["api_key_ref"] == "env:E905_R1_KEY"
    assert provider_api_key(provider, environ={}) == inline
    if not inline:
        with pytest.raises(ModelProfileError) as error:
            resolved_model(data, report["profile_id"])
        assert error.value.reason == "credential_missing"
    before = model_profiles_path(host.home_paths).read_bytes()
    monkeypatch.setenv("E905_R1_KEY", "late-fixture")
    assert resolved_model(data, report["profile_id"])["api_key"] == "late-fixture"
    assert model_profiles_path(host.home_paths).read_bytes() == before


# LLM: 离线协议不要求凭据，但停用与协议错配仍须关闭；不以模型名或环境猜测。
# 函数用途: 覆盖导入、公开列表、真实会话选择、解析和后端生成的完整离线链。
def test_echo_import_list_select_resolve(tmp_path):
    host = _host(tmp_path)
    report = import_legacy_owner_configuration(host.home_paths, agent_values={"model_backend": "echo"},
        capability_values={}, environ={}, expected_generation=None)
    key = report["profile_id"]
    catalog = read_model_profiles(model_profiles_path(host.home_paths))
    assert next(row for row in public_model_profiles(catalog, host.config)["profiles"] if row["id"] == key)["available"]
    listing = execute_local_model_operation(host, "echo-session", "list", {})
    assert listing["selection_available"] is True
    selected = execute_local_model_operation(host, "echo-session", "select", {"profile_id": key})
    assert selected["selected"] == key and selected["selection_available"] is True
    config = thread_model_config(host, selected["thread_id"])
    assert config.model_backend == "echo"
    assert get_backend(config.model_backend, config).generate("offline-fixture").text
    provider_id = catalog["profiles"][key]["provider_id"]
    catalog["providers"][provider_id]["enabled"] = False
    assert not public_model_profiles(catalog, host.config)["profiles"][0]["available"]
    with pytest.raises(ModelProfileError):
        resolved_model(catalog, key)


# LLM: 有规范目录的正式入口不得把内存 echo 当未选模型的旁路；无目录显式测试桩单列。
# 函数用途: 检查纯内存兼容只存在于明确无目录的测试调用。
def test_echo_memory_stub_is_not_formal_catalog_bypass(tmp_path):
    host = _host(tmp_path)
    config = AgentConfig(model_backend="echo")
    assert selected_model_config_for_resources(config, None) is config
    formal = selected_model_config_for_resources(config, host.home_paths)
    assert formal.model_backend != "echo"
    assert not any(row.get("available") for row in execute_model_profile_operation(
        SimpleNamespace(home_paths=host.home_paths, config=config), "list", {})["profiles"])


# LLM: 快捷编辑必须从锁内原记录补未出现的字段，而不能依赖客户端回传整个旧模型。
# 函数用途: 检查快捷改名保留关闭开关、未知窗口、环境来源和原请求头。
def test_flat_edit_presence_and_endpoint_replacement(tmp_path):
    host = _host(tmp_path)
    key = _seed(host, env=True)
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "provider": {"custom_headers": {"X-Fixture": "private-header"}, "session_header": "x-session"}})
    execute_model_profile_operation(host, "save_profile", {"profile_id": key, "editing": True,
        "profile": {"model_name": "new-name", "api_key": ""}})
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert data["profiles"][key]["reasoning_update_items"] == "off"
    assert data["profiles"][key]["model_context_window_tokens"] is None
    assert data["providers"]["p"]["api_key_ref"] == "env:E905_R1_KEY"
    assert data["providers"]["p"]["custom_headers"] == {"X-Fixture": "private-header"}
    before = model_profiles_path(host.home_paths).read_bytes()
    with pytest.raises(ModelProfileError):
        execute_model_profile_operation(host, "save_profile", {"profile_id": key, "editing": True,
            "profile": {"api_base": "https://other.invalid/v1", "api_key": ""}})
    assert model_profiles_path(host.home_paths).read_bytes() == before
    execute_model_profile_operation(host, "save_profile", {"profile_id": key, "editing": True,
        "profile": {"api_base": "https://other.invalid/v1", "api_key": "new-fixture"}})
    provider = read_model_profiles(model_profiles_path(host.home_paths))["providers"]["p"]
    assert "api_key_ref" not in provider and provider["api_key"] == "new-fixture"
    assert provider["custom_headers"] == {} and provider["session_header"] == ""


# LLM: 明确清除凭据与留空保留是两种不同动作；历史后备不应让清除失效。
# 函数用途: 检查引用和静态后备同时存在时，普通编辑保留而明确清除全部移除。
def test_env_inline_backup_roundtrip_and_clear(tmp_path):
    host = _host(tmp_path)
    key = _seed(host, env=True)
    path = model_profiles_path(host.home_paths)
    data = read_model_profiles(path)
    data["providers"]["p"]["api_key"] = "backup-fixture"
    _save_profiles(path, data)
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "provider": {"api_key": "", "display_name": "renamed"}})
    data = read_model_profiles(path)
    assert data["providers"]["p"]["api_key_ref"] == "env:E905_R1_KEY"
    assert provider_api_key(data["providers"]["p"], environ={}) == "backup-fixture"
    assert provider_api_key(data["providers"]["p"], environ={"E905_R1_KEY": "env-fixture"}) == "env-fixture"
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "clear_key": True, "provider": {}})
    data = read_model_profiles(path)
    assert "api_key_ref" not in data["providers"]["p"] and data["providers"]["p"]["api_key"] == ""
    with pytest.raises(ModelProfileError) as error:
        resolved_model(data, key)
    assert error.value.reason == "credential_missing"


# LLM: 正式协议合法性必须与解析器一致，离线服务商不能伪装成网络模型的可用来源。
# 函数用途: 检查公开目录和实际解析同时拒绝传输与模型协议错配。
def test_echo_protocol_mismatch_unavailable(tmp_path):
    host = _host(tmp_path)
    key = _seed(host)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    data["profiles"][key]["model_backend"] = "echo"
    assert not public_model_profiles(data, host.config)["profiles"][0]["available"]
    with pytest.raises(ModelProfileError) as error:
        resolved_model(data, key)
    assert error.value.reason == "capability_mismatch"
