"""无模型也能启动配置界面；没有显式选择时不得伪造模型或发送请求。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.errors import (
    ModelNotConfiguredError,
    provider_configuration_report,
)
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.runtime_errors import runtime_error_report
from agent_py_agent.agent.settings import AgentConfig, load_config
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    public_model_profiles,
    read_model_profiles,
)
from agent_py_agent.cli.chat_parts.tui_model_menu import _publish_selection
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


def test_dataclass_and_release_config_have_no_model_defaults():
    from pathlib import Path

    config_path = Path(__file__).parents[1] / "config" / "agent_config.yaml"
    for config in (AgentConfig(), load_config(config_path)):
        assert (config.model_backend, config.model_name, config.api_base) == ("", "", "")


@pytest.mark.parametrize("backend", ["", "openai_compatible", "openai_responses", "anthropic_compatible"])
def test_missing_model_configuration_never_probes_or_generates(backend):
    adapter = get_backend(backend, AgentConfig(model_backend=backend))
    assert adapter.name == "unconfigured"
    for call in (adapter.probe_tool_capability, lambda: adapter.generate("普通任务")):
        with pytest.raises(ModelNotConfiguredError) as caught:
            call()
        assert caught.value.error_code == "MODEL_NOT_CONFIGURED"


def test_missing_model_has_actionable_error_on_all_surfaces():
    error = ModelNotConfiguredError()
    assert "/model" in provider_configuration_report(error)
    assert "/model" in gateway_client_error_message(error.error_code)
    report = runtime_error_report(error)
    assert report["recoverable"] is False
    assert "/model" in str(report)


def test_empty_catalog_has_no_placeholder_and_tui_can_open_settings(tmp_path):
    host = SimpleNamespace(
        config=AgentConfig(),
        home_paths=SimpleNamespace(config_dir=tmp_path, owner_provider="test", owner_kind="user", owner_id="alice"),
    )
    result = execute_model_profile_operation(host, "list", {})
    assert result["profiles"] == [] and result["selection_available"] is False
    assert "/model" in result["warning"]
    runtime = TuiRuntime("unconfigured-model")
    _publish_selection(runtime, result)
    assert runtime.store.snapshot().selected_model_name == "未配置模型（/model 配置）"


def test_explicit_connection_is_preserved_and_key_presence_is_not_faked(tmp_path):
    config = AgentConfig(model_backend="openai_compatible", model_name="chosen-model", api_base="https://example.test/v1")
    result = public_model_profiles(read_model_profiles(tmp_path / "absent.json"), config)
    row = result["profiles"][0]
    assert row["model_name"] == "chosen-model" and row["has_key"] is False
    assert get_backend(config.model_backend, config).model_name == "chosen-model"


def test_echo_requires_explicit_selection():
    assert get_backend("", AgentConfig()).name != "echo"
    assert get_backend("echo", AgentConfig(model_backend="echo")).name == "echo"


@pytest.mark.parametrize("backend", ["", "echo", "openai_compatible", "openai_responses", "anthropic_compatible"])
@pytest.mark.parametrize("model,url", [("", ""), ("chosen", ""), ("", "https://example.test/v1"),
                                       ("chosen", "https://example.test/v1")])
def test_background_readiness_matches_backend_factory(backend, model, url):
    from agent_py_agent.agent.backends.factory import model_configuration_missing

    config = AgentConfig(model_backend=backend, model_name=model, api_base=url)
    assert model_configuration_missing(backend, config) == (get_backend(backend, config).name == "unconfigured")


def test_curator_invalid_selection_does_not_fall_back_to_deployment(monkeypatch):
    from agent_py_agent.agent.core import _curator_profile_config
    from agent_py_agent.agent.settings import model_profiles

    def invalid_selection(agent):
        raise model_profiles.ModelProfileError("选定模型不存在")

    monkeypatch.setattr(model_profiles, "selected_model_config", invalid_selection)
    deployment = AgentConfig(model_backend="openai_compatible", model_name="different-model",
                             api_base="https://example.test/v1", api_key="test-secret")
    config = _curator_profile_config(SimpleNamespace(), deployment)
    assert (config.model_backend, config.model_name, config.api_base, config.api_key) == ("", "", "", "")
    assert get_backend(config.model_backend, config).name == "unconfigured"
