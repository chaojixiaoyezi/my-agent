"""默认、管理员边界和模型拒绝不依赖真实配置。"""
import pytest

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.parameter_changes import (
    ParameterChangeError,
    _writable_spec,
    user_settings_write_scope,
)
from agent_py_agent.agent.settings.parameter_registry import _BOUNDARY_NAMES, parameter_registry
from agent_py_agent.agent.settings.user_config_capability import USER_SETTINGS_BOUNDARY_KEYS

KEY = "plugin_legacy_sandbox_default"


def test_new_default_is_true_and_explicitly_registered():
    assert getattr(AgentConfig(), KEY, None) is True
    assert KEY in _BOUNDARY_NAMES and KEY in USER_SETTINGS_BOUNDARY_KEYS
    spec = parameter_registry().get(KEY)
    assert spec is not None and spec.default is True and not spec.writable


def test_model_write_is_parameter_boundary_not_unknown():
    with pytest.raises(ParameterChangeError) as error:
        _writable_spec(KEY)
    assert error.value.code == "PARAMETER_BOUNDARY"


def test_only_authenticated_settings_scope_may_write_and_does_not_leak():
    with user_settings_write_scope():
        assert _writable_spec(KEY).key == KEY
    with pytest.raises(ParameterChangeError) as error:
        _writable_spec(KEY)
    assert error.value.code == "PARAMETER_BOUNDARY"
