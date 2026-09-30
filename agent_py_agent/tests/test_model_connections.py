"""「新增模型」按连接一次加多个模型：连接去重、重复跳过、整批落盘、未保存连接的目录读取与报错脱敏。"""

import json
from uuid import uuid4

import pytest

from agent_py_agent.agent.settings import model_provider_network as network
from agent_py_agent.agent.settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.tests.test_model_profiles import Host

SECRET = "only-private-secret"


def connection(**values):
    return {"model_backend": "openai_compatible", "api_base": "https://api.example.test/v1", "api_key": SECRET,
            "custom_headers": {}, "session_header": "", **values}


def models(*names, window=128000):
    return [{"profile_id": str(uuid4()), "model_name": name, "model_context_window_tokens": window} for name in names]


def add_models(host, payload):
    return execute_model_profile_operation(host, "add_models", payload)


def test_one_connection_adds_several_models_and_reuses_the_same_connection(tmp_path):
    host = Host(tmp_path)
    first = add_models(host, {"connection": connection(), "models": models("m-a", "m-b")})
    assert first["added_models"] == ["m-a", "m-b"]
    again = add_models(host, {"connection": connection(), "models": models("m-b", "m-c", window="200000")})
    assert again["added_models"] == ["m-c"]  # m-b 已在同一连接下，不重复添加
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert len(data["providers"]) == 1
    provider_id, provider = next(iter(data["providers"].items()))
    assert provider_id.startswith("provider-") and provider["api_key"] == SECRET
    assert provider["display_name"] == "api.example.test" and provider["capabilities"] == ["agentic"]
    rows = {row["model_name"]: row for row in data["profiles"].values()}
    assert set(rows) == {"m-a", "m-b", "m-c"} and rows["m-c"]["model_context_window_tokens"] == 200000
    assert all(row["provider_id"] == provider_id and row["capability"] == "agentic" for row in rows.values())
    assert SECRET not in json.dumps(first) + json.dumps(again)


def test_a_different_key_or_header_is_a_different_connection(tmp_path):
    host = Host(tmp_path)
    add_models(host, {"connection": connection(), "models": models("m-a")})
    add_models(host, {"connection": connection(api_key="other-secret"), "models": models("m-a")})
    add_models(host, {"connection": connection(custom_headers={"X-Title": "my-agent"}), "models": models("m-a")})
    add_models(host, {"connection": connection(session_header="x-opencode-session", display_name="OpenCode Go"),
                      "models": models("m-a")})
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert len(data["providers"]) == 4 and len(data["profiles"]) == 4
    assert "OpenCode Go" in {row["display_name"] for row in data["providers"].values()}


def test_decision_connection_gets_decision_capability(tmp_path):
    host = Host(tmp_path)
    result = add_models(host, {"connection": connection(model_backend="typesafe_decision"), "models": models("jev-a")})
    row = next(row for row in result["profiles"] if row["model_name"] == "jev-a")
    assert row["capability"] == "decision" and row["available"] is False and row["available_for"] == ["decision"]


def test_invalid_batches_write_nothing(tmp_path):
    host = Host(tmp_path)
    path = model_profiles_path(host.home_paths)
    for payload, message in (
        ({"connection": connection(api_key=""), "models": models("m-a")}, "密钥"),
        ({"connection": connection(), "models": []}, "至少选择一个模型"),
        ({"connection": connection(), "models": models(*[f"m-{index}" for index in range(201)])}, "最多 200"),
        ({"connection": connection(model_backend="bogus"), "models": models("m-a")}, "接口类型"),
        ({"connection": connection(), "models": [*models("ok"), *models("bad", window=10)]}, "上下文窗口"),
        ({"provider_id": "missing", "model_backend": "openai_responses", "models": models("m-a")}, "服务商不存在"),
    ):
        with pytest.raises(ModelProfileError, match=message):
            add_models(host, payload)
    assert not path.exists()  # 整批校验失败时一个也不写


def test_existing_provider_gets_models_with_the_requested_interface(tmp_path):
    host = Host(tmp_path)
    add_models(host, {"connection": connection(), "models": models("m-a")})
    provider_id = next(iter(read_model_profiles(model_profiles_path(host.home_paths))["providers"]))
    result = add_models(host, {"provider_id": provider_id, "model_backend": "openai_responses", "models": models("m-r")})
    assert result["added_models"] == ["m-r"]
    row = next(row for row in result["profiles"] if row["model_name"] == "m-r")
    assert row["model_backend"] == "openai_responses" and row["provider_id"] == provider_id


def test_reused_profile_id_with_other_content_is_rejected(tmp_path):
    host = Host(tmp_path)
    batch = models("m-a")
    add_models(host, {"connection": connection(), "models": batch})
    changed = [{**batch[0], "model_name": "m-z"}]
    with pytest.raises(ModelProfileError, match="编号已被使用"):
        add_models(host, {"connection": connection(), "models": changed})


def test_unsaved_connection_discovery_reads_the_catalog_without_saving(tmp_path, monkeypatch):
    host = Host(tmp_path)
    seen = []

    def fake_get_json(request):
        seen.append((request.api_base, request.path, request.headers.get("Authorization")))
        return {"data": [{"id": "m-a", "context_length": 64000}, {"id": "m-b"}]}

    monkeypatch.setattr(network, "get_json", fake_get_json)
    result = execute_model_profile_operation(host, "discover", {"connection": connection(), "conversation_id": "menu"})
    assert result["ok"] is True
    assert result["models"] == [{"model_name": "m-a", "model_context_window_tokens": 64000},
                                {"model_name": "m-b", "model_context_window_tokens": 0}]
    assert seen == [("https://api.example.test/v1", "/models", "Bearer " + SECRET)]
    assert not model_profiles_path(host.home_paths).exists()


def test_unsaved_connection_discovery_error_hides_the_key(tmp_path, monkeypatch):
    host = Host(tmp_path)

    class Failure(RuntimeError):
        details = {"provider_error": {"error": {"message": f"invalid api key {SECRET}"}}}

    def failing(request):
        raise Failure()

    monkeypatch.setattr(network, "get_json", failing)
    result = execute_model_profile_operation(host, "discover", {"connection": connection(), "conversation_id": "menu"})
    assert result["ok"] is False and "invalid api key" in result["provider_message"]
    assert SECRET not in json.dumps(result, ensure_ascii=False)
