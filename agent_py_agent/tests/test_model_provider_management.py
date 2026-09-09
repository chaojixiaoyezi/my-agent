"""服务商/模型、私有迁移和传输身份的定向验证，不代替真实 TUI 验收。"""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import BackendOptions, get_backend
from agent_py_agent.agent.backends.base import HttpBackend
from agent_py_agent.agent.backends.provider_headers import (
    provider_runtime_scope,
    request_headers,
    validate_headers,
)
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation as op,
)
from agent_py_agent.agent.settings.model_profiles import (
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from agent_py_agent.tests.test_model_profiles import Host, profile


def provider(host):
    return op(host, "save_provider", {"provider_id": "service", "provider": {
        "display_name": "示例", "api_base": "https://example.test/v1", "api_key": "private-key",
        "custom_headers": {"User-Agent": "my-agent/1.0", "X-Private-Setting": "private-header"},
        "session_header": "x-opencode-session"}})


def model(host, name="model-a", **fields):
    key = str(uuid4())
    op(host, "save_model", {"profile_id": key, "profile": {
        "provider_id": "service", "model_name": name, "model_backend": "openai_responses",
        "model_context_window_tokens": 200000, **fields}})
    return key


def test_shared_provider_edit_key_retention_and_disable(tmp_path):
    host = Host(tmp_path)
    provider(host)
    first, second = model(host), model(host, "model-b")
    op(host, "select", {"profile_id": first})
    before = selected_model_config(host)
    result = op(host, "save_provider", {"provider_id": "service", "editing": True,
        "provider": {"display_name": "新版", "api_key": "", "custom_headers": None}})
    assert "private-key" not in json.dumps(result) and "private-header" not in json.dumps(result)
    assert selected_model_config(host).api_key == before.api_key
    assert selected_model_config(host).model_custom_headers == before.model_custom_headers
    op(host, "save_provider", {"provider_id": "service", "editing": True, "provider": {"api_key": "new-key"}})
    assert selected_model_config(host, profile_id=second).api_key == "new-key"
    assert before.api_key == "private-key"
    op(host, "save_provider", {"provider_id": "service", "editing": True, "clear_key": True, "provider": {}})
    with pytest.raises(ValueError, match="缺少密钥"):
        selected_model_config(host)
    assert op(host, "list", {})["selected"] == first


def test_delete_and_embedding_guard(tmp_path):
    host = Host(tmp_path)
    provider(host)
    first = model(host)
    op(host, "select", {"profile_id": first})
    for action, payload in [("delete_model", {"profile_id": first}), ("delete_provider", {"provider_id": "service"})]:
        with pytest.raises(ValueError):
            op(host, action, payload)
    second = model(host, "embed", capability="embedding")
    with pytest.raises(ValueError):
        op(host, "select", {"profile_id": second})
    op(host, "select", {"profile_id": "default"})
    op(host, "delete_model", {"profile_id": first})
    op(host, "delete_model", {"profile_id": second})
    op(host, "delete_provider", {"provider_id": "service"})
    assert op(host, "list", {})["providers"] == []


def test_v1_read_only_migration_preserves_profile_uuid(tmp_path):
    host = Host(tmp_path)
    path = model_profiles_path(host.home_paths)
    path.parent.mkdir(parents=True)
    key = str(uuid4())
    path.write_text(json.dumps({"schema": "owner_model_profiles.v1", "selected": key, "profiles": {key: profile()}}))
    before = path.read_bytes()
    assert selected_model_config(host).model_name == "MiniMax-M2.7"
    assert path.read_bytes() == before
    op(host, "select", {"profile_id": key})
    assert read_model_profiles(path)["schema"] == "owner_model_profiles.v2"
    assert op(host, "list", {})["selected"] == key


@pytest.mark.parametrize("value", [{"Authorization": "secret"}, {"Cookie": "secret"}, {"Host": "elsewhere"},
                                  {"X-Header": "bad\r\ninjected"}, {"X-Header": "a", "x-header": "b"}, {"é": "a"}])
def test_header_injection_rejected(value):
    with pytest.raises(ValueError):
        validate_headers(value)


def test_real_transport_envelope_session_isolation_and_v1(tmp_path):
    host = Host(tmp_path)
    backend = HttpBackend(BackendOptions("https://example.test/v1", "key", "m", session_header="x-opencode-session"))
    def run(thread):
        with provider_runtime_scope(host, SimpleNamespace(task_attributes={"agent_thread_id": thread})):
            first = backend._gateway_request("/v1/messages", {}, {"Authorization": "Bearer key"})
            second = backend._gateway_request("/v1/messages", {}, {"Authorization": "Bearer key"})
            assert first.headers["x-opencode-session"] == second.headers["x-opencode-session"]
            assert first.url == "https://example.test/v1/messages"
            return first.headers["x-opencode-session"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(run, ["thread-a", "thread-b"]))
    assert a != b and run("thread-a") == a
    with pytest.raises(ValueError, match="没有绑定"):
        request_headers({}, {}, "x-opencode-session")


def test_responses_backend_registered(tmp_path):
    host = Host(tmp_path)
    provider(host)
    key = model(host)
    config = selected_model_config(host, profile_id=key)
    assert get_backend(config.model_backend, config).name == "openai_responses"


def test_per_model_temperature_and_invalid_values(tmp_path):
    host = Host(tmp_path)
    provider(host)
    key = model(host, temperature="1")
    assert selected_model_config(host, profile_id=key).temperature == "1.0"
    for value in [True, "nan", "inf", "-1", "2.1", "oops"]:
        with pytest.raises(ValueError, match="温度"):
            model(host, temperature=value)


def test_probe_failure_diagnostics_redact_key_headers_and_preserve_identity(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
    from agent_py_agent.agent.settings import model_provider_network

    host = Host(tmp_path)
    provider(host)
    key = model(host)
    def rejected(*args):
        raise ProviderRequestRejectedError("rejected", status_code=400, details={"provider_error": {
            "error": {"message": "invalid temperature; private-key private-header\r\n"}}})
    monkeypatch.setattr(model_provider_network, "_probe", rejected)
    result = op(host, "probe", {"profile_id": key, "session_id": "tui-test"})
    assert result["model_name"] == "model-a" and result["status_code"] == 400
    assert "invalid temperature" in result["provider_message"]
    assert "private-key" not in json.dumps(result) and "private-header" not in json.dumps(result)
    assert "\r" not in result["provider_message"] and "\n" not in result["provider_message"]
