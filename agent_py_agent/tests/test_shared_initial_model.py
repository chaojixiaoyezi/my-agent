"""管理员给其他用户指定初始模型：只影响没选过模型的普通用户；撤销共享同次清除；删除/损坏明确报错，不悄悄换模型。"""

import json
from uuid import uuid4

import pytest

from agent_py_agent.agent.settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.agent.settings.shared_model_catalog import (
    initial_profile_key,
    set_initial_profile,
    set_shared_profile,
    shared_catalog_path,
)
from agent_py_agent.agent.settings.thread_model_selection import (
    execute_local_model_operation,
    thread_model_config,
)
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_shared_model_catalog import admin_host
from agent_py_agent.tests.test_thread_model_selection import host_with_store


def test_only_the_admin_sets_it_and_setting_it_also_shares_the_model(tmp_path):
    admin, alice = admin_host(tmp_path), Host(tmp_path)
    key, _ = add(admin, model_name="team-model")
    with pytest.raises(ModelProfileError, match="只有管理员"):
        set_initial_profile(alice, key)
    result = execute_model_profile_operation(admin, "set_initial", {"profile_id": key})
    assert result["initial_profile"] == "shared:" + key
    row = next(row for row in result["profiles"] if row["id"] == key)
    assert row["shared_enabled"] is True and row["initial_for_others"] is True
    assert "secret" not in shared_catalog_path(admin.home_paths).read_text()
    with pytest.raises(ModelProfileError, match="只有管理员可以指定"):
        set_initial_profile(alice, "")  # 普通用户也不能清除
    assert initial_profile_key(admin.home_paths) == "shared:" + key
    execute_model_profile_operation(admin, "set_initial", {"profile_id": ""})
    assert initial_profile_key(admin.home_paths) == ""
    assert execute_model_profile_operation(alice, "list", {})["profiles"][0]["id"] == "default"


def test_oauth_models_cannot_become_the_initial_model(tmp_path):
    admin = admin_host(tmp_path)
    execute_model_profile_operation(admin, "save_provider", {"provider_id": "account", "provider": {
        "display_name": "ChatGPT 订阅", "api_base": "https://chatgpt.com/backend-api/codex", "auth": {"mode": "chatgpt"}}})
    key = "7f4f1d5e-2c3b-4a59-9e8f-1a2b3c4d5e6f"
    execute_model_profile_operation(admin, "save_model", {"profile_id": key, "profile": {
        "provider_id": "account", "model_name": "gpt-x", "model_backend": "openai_responses",
        "model_context_window_tokens": 272000}})
    with pytest.raises(ModelProfileError, match="不能跨用户共享"):
        set_initial_profile(admin, key)
    assert initial_profile_key(admin.home_paths) == ""


def test_default_follows_the_initial_model_only_for_other_users(tmp_path):
    admin = admin_host(tmp_path / "config")
    alice = host_with_store(tmp_path)
    key, _ = add(admin, model_name="team-model")
    assert selected_model_config(alice) is alice.config
    set_initial_profile(admin, key)
    config = selected_model_config(alice)
    assert config.model_name == "team-model" and config.api_key == "only-private-secret"
    assert config.config_sources["model_name"]["profile_id"] == "shared:" + key
    assert selected_model_config(admin) is admin.config  # 管理员自己的默认仍是部署配置
    listing = execute_local_model_operation(alice, "im-chat", "list", {})
    default = listing["profiles"][0]
    assert listing["selected"] == "default" and listing["selection_available"] is True
    assert default["id"] == "default" and default["default_source"] == "admin_initial"
    assert default["model_name"] == "team-model" and "shared" not in default
    assert "secret" not in json.dumps(listing)
    assert thread_model_config(alice, listing["thread_id"]).model_name == "team-model"


def test_an_explicit_choice_is_never_overridden(tmp_path):
    admin = admin_host(tmp_path / "config")
    alice = host_with_store(tmp_path)
    initial, _ = add(admin, model_name="team-model")
    own, _ = add(alice, model_name="alice-own")
    chosen = execute_local_model_operation(alice, "tui", "select", {"profile_id": own})
    set_initial_profile(admin, initial)
    assert thread_model_config(alice, chosen["thread_id"]).model_name == "alice-own"
    execute_model_profile_operation(alice, "set_default", {"profile_id": own})
    assert selected_model_config(alice).model_name == "alice-own"


def test_revoking_the_share_clears_the_initial_model(tmp_path):
    admin = admin_host(tmp_path / "config")
    alice = host_with_store(tmp_path)
    key, _ = add(admin, model_name="team-model")
    set_initial_profile(admin, key)
    set_shared_profile(admin, key, False)
    assert initial_profile_key(admin.home_paths) == ""
    assert selected_model_config(alice) is alice.config


def test_deleted_source_fails_loudly_instead_of_switching(tmp_path):
    admin = admin_host(tmp_path / "config")
    alice = host_with_store(tmp_path)
    key, _ = add(admin, model_name="team-model")
    set_initial_profile(admin, key)
    execute_model_profile_operation(admin, "delete_model", {"profile_id": key})
    with pytest.raises(ModelProfileError, match="已删除"):
        selected_model_config(alice)
    listing = execute_local_model_operation(alice, "im-chat", "list", {})
    assert listing["profiles"][0]["available"] is False and listing["selection_available"] is False
    assert "管理员指定的初始模型暂不可用" in listing["warning"]


def test_corrupt_catalog_is_an_error_not_a_silent_default(tmp_path):
    admin = admin_host(tmp_path / "config")
    alice = host_with_store(tmp_path)
    key, _ = add(admin, model_name="team-model")
    set_initial_profile(admin, key)
    path = shared_catalog_path(admin.home_paths)
    data = json.loads(path.read_text())
    # 编号格式错，或指向一个没发布的模型（手改文件），都按目录损坏处理。
    for broken in ("shared:not-a-uuid", "shared:" + str(uuid4())):
        path.write_text(json.dumps({**data, "initial_profile": broken}))
        with pytest.raises(ModelProfileError, match="损坏"):
            selected_model_config(alice)
        listing = execute_local_model_operation(alice, "im-chat", "list", {})
        assert "共享模型目录暂不可用" in listing["warning"]
