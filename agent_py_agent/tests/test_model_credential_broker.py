# LLM: 仅使用合成凭据和 pytest 环境；验证凭据仍归 provider，环境引用不复制实际值、不建立第二仓库。
# 模块用途: 锁定旧模型导入与 provider 环境引用的安全合同。
import pytest

from agent_py_agent.agent.settings.model_provider_operations import _save_provider
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError, validate_provider


# LLM: 测试数据不是实际服务地址或秘密，不发请求；provider 只有原私有目录的一份记录。
# 函数用途: 创建可验证的纯内存服务商输入。
def _provider(**changes):
    return {"display_name": "fixture", "api_base": "https://fixture.invalid/v1", "api_key": "", **changes}


# LLM: 非空环境优先时只能保存 env 引用，不把环境实值或未采用的旧静态密钥写进 provider。
# 函数用途: 验证离线导入的凭据选择不会制造第二份明文。
def test_provider_import_uses_env_reference_without_copying_value():
    from agent_py_agent.agent.settings.model_credentials import legacy_provider_credential

    result = legacy_provider_credential("original-static-fixture", "E905_FIXTURE_KEY", environ={"E905_FIXTURE_KEY": "environment-fixture"})
    assert result == {"api_key": "", "api_key_ref": "env:E905_FIXTURE_KEY"}
    assert "environment-fixture" not in repr(result) and "original-static-fixture" not in repr(result)


# LLM: 环境为空不能覆盖原静态配置；两者都没有则保留未配置，不能替用户选择凭据。
# 函数用途: 验证空环境与未配置状态的导入语义。
def test_empty_environment_preserves_static_credential():
    from agent_py_agent.agent.settings.model_credentials import legacy_provider_credential

    assert legacy_provider_credential("static-fixture", "E905_FIXTURE_KEY", environ={}) == {"api_key": "static-fixture"}
    assert legacy_provider_credential("", "", environ={}) == {"api_key": ""}


# LLM: 目录 schema 保留引用，实际解析只读取已记录的准确 env 名，不支持 file/URL 或拼接表达式。
# 函数用途: 验证引用经过 schema 后仍能准确解析，失效时不回退其他秘密。
def test_stored_provider_reference_roundtrips_and_resolves(monkeypatch):
    from agent_py_agent.agent.settings.model_credentials import provider_api_key

    monkeypatch.setenv("E905_FIXTURE_KEY", "environment-fixture")
    stored = validate_provider(_provider(api_key_ref="env:E905_FIXTURE_KEY"))
    assert stored["api_key_ref"] == "env:E905_FIXTURE_KEY"
    assert provider_api_key(stored) == "environment-fixture"
    monkeypatch.delenv("E905_FIXTURE_KEY")
    assert provider_api_key(stored) == ""
    with pytest.raises(ModelProfileError):
        validate_provider(_provider(api_key_ref="file:/tmp/not-a-credential-source"))


# LLM: 只有显式离线迁移能建立宿主环境引用；普通新增/编辑表单不能把任意宿主变量名变成凭据。
# 函数用途: 验证管理表单拒绝引用注入，而不是只检查引用格式。
def test_save_provider_rejects_environment_reference_in_public_payload():
    data = {"providers": {}, "profiles": {}}
    with pytest.raises(ModelProfileError):
        _save_provider(data, {"provider_id": "fixture", "provider": _provider(api_key_ref="env:E905_FIXTURE_KEY")})
    assert data["providers"] == {}


# LLM: 凭据快照只解析本次选中的连接；未选且环境已失效的草稿不能阻止另一个合法模型请求。
# 函数用途: 验证网络前快照准确限定当前连接，不污染原目录数据。
def test_network_snapshot_only_resolves_selected_providers(monkeypatch):
    from agent_py_agent.agent.settings.model_provider_network import _network_credential_snapshot

    monkeypatch.delenv("E905_UNUSED_KEY", raising=False)
    data = {"providers": {"active": validate_provider(_provider(api_key="static-fixture")),
                          "unused": validate_provider(_provider(api_key_ref="env:E905_UNUSED_KEY"))}}
    snapshot = _network_credential_snapshot(data, provider_ids={"active"})
    assert snapshot["providers"]["active"]["api_key"] == "static-fixture"
    assert snapshot["providers"]["unused"] == data["providers"]["unused"]


# LLM: 环境可能在请求后变化，回显清洗必须仍记得实际发送过的值；只做本地模拟，不声称远端调用通过。
# 函数用途: 验证一次请求的凭据固定后仍能清洗旧值且不把展开值写回 catalog 输入。
def test_network_snapshot_redacts_sent_value_after_env_rotation(monkeypatch):
    from agent_py_agent.agent.settings.model_provider_network import (
        _network_credential_snapshot,
        _redact_network_text,
    )

    monkeypatch.setenv("E905_FIXTURE_KEY", "before-rotation-fixture")
    original = validate_provider(_provider(api_key_ref="env:E905_FIXTURE_KEY"))
    data = {"providers": {"active": original}}
    snapshot = _network_credential_snapshot(data, provider_ids={"active"})
    monkeypatch.setenv("E905_FIXTURE_KEY", "after-rotation-fixture")
    assert "before-rotation-fixture" not in _redact_network_text("echo before-rotation-fixture", snapshot)
    assert original["api_key"] == "" and original["api_key_ref"] == "env:E905_FIXTURE_KEY"
