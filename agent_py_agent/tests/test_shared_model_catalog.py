"""共享只授予服务端模型调用，不复制凭证或共享私有模型/会话。"""

import json

import pytest

from agent_py_agent.agent.settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
)
from agent_py_agent.agent.settings.shared_model_catalog import (
    public_shared_profiles,
    resolve_shared_model,
    set_shared_profile,
    shared_catalog_path,
    shared_profile_key,
)
from agent_py_agent.tests.test_model_profiles import Host, add


def admin_host(path):
    host = Host(path, owner="local/main")
    host.home_paths.owner_kind = "main"
    return host


def test_shared_catalog_opt_in_no_secret_copy_and_revocation(tmp_path):
    admin, alice = admin_host(tmp_path), Host(tmp_path)
    private, _ = add(admin, model_name="private")
    shared, _ = add(admin, model_name="shared")
    assert public_shared_profiles(alice.home_paths) == []
    with pytest.raises(ModelProfileError):
        set_shared_profile(alice, shared, True)
    set_shared_profile(admin, shared, True)
    rows = public_shared_profiles(alice.home_paths)
    assert len(rows) == 1 and rows[0]["id"] == "shared:" + shared
    assert private not in json.dumps(rows)
    assert "secret" not in json.dumps(rows)
    assert "secret" not in shared_catalog_path(admin.home_paths).read_text()
    assert resolve_shared_model(alice.home_paths, rows[0]["id"])["api_key"] == "only-private-secret"
    with pytest.raises(ModelProfileError):
        resolve_shared_model(alice.home_paths, "shared:" + private)
    set_shared_profile(admin, shared, False)
    assert public_shared_profiles(alice.home_paths) == []
    with pytest.raises(ModelProfileError):
        resolve_shared_model(alice.home_paths, "shared:" + shared)


def test_deleted_shared_source_is_not_silently_replaced(tmp_path):
    admin = admin_host(tmp_path)
    key, _ = add(admin)
    set_shared_profile(admin, key, True)
    execute_model_profile_operation(admin, "delete_model", {"profile_id": key})
    assert public_shared_profiles(admin.home_paths) == []
    with pytest.raises(ModelProfileError, match="已删除"):
        resolve_shared_model(admin.home_paths, "shared:" + key)


@pytest.mark.parametrize("value", ["shared:../../private", "shared:default", "shared:", "shared:x"])
def test_shared_profile_reference_never_accepts_paths(value):
    with pytest.raises(ModelProfileError):
        shared_profile_key(value)


def test_shared_selection_defaults_child_and_private_isolation(tmp_path):
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
        resolve_child_model_profile,
    )
    from agent_py_agent.agent.settings.thread_model_selection import (
        execute_local_model_operation,
        thread_model_config,
    )
    from agent_py_agent.tests.test_thread_model_selection import host_with_store

    admin = admin_host(tmp_path / "config")
    alice, bob = host_with_store(tmp_path), host_with_store(tmp_path, "bob")
    key, _ = add(admin, model_name="shared-A")
    execute_model_profile_operation(admin, "set_shared", {"profile_id": key, "enabled": True})
    shared = "shared:" + key
    one = execute_local_model_operation(alice, "one", "select", {"profile_id": shared})
    two = execute_local_model_operation(alice, "two", "list", {})
    assert one["selected"] == shared and two["selected"] == "default"
    assert thread_model_config(alice, one["thread_id"]).model_name == "shared-A"
    assert resolve_child_model_profile(bob, "shared-A") == shared
    assert resolve_child_model_profile(admin, "shared-A") == key  # 同一模型的共享别名不造成重名
    execute_local_model_operation(bob, "existing", "set_default", {"profile_id": shared})
    assert execute_local_model_operation(bob, "fresh", "list", {})["selected"] == shared
    data = read_model_profiles(model_profiles_path(bob.home_paths))
    assert data["selected"] == shared and data["providers"] == {} and data["profiles"] == {}
    assert "secret" not in model_profiles_path(bob.home_paths).read_text()
    set_shared_profile(admin, key, False)
    with pytest.raises(ModelProfileError, match="撤销"):
        thread_model_config(alice, one["thread_id"])
    repaired = execute_local_model_operation(alice, "one", "select", {"profile_id": "default"})
    assert repaired["selected"] == "default"


def test_shared_probe_error_redacts_admin_key(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )
    from agent_py_agent.agent.settings.model_provider_network import execute_provider_network

    admin, alice = admin_host(tmp_path), Host(tmp_path)
    key, _ = add(admin)
    set_shared_profile(admin, key, True)

    class Failure(RuntimeError):
        details = {"provider_error": {"error": {"message": "only-private-secret is rejected"}}}

    def fail(*args):
        raise Failure()

    monkeypatch.setattr("agent_py_agent.agent.settings.model_provider_network.get_backend",
                        lambda *args: SimpleNamespace(generate=fail))
    result = execute_provider_network(alice, read_model_profiles(model_profiles_path(alice.home_paths)),
                                      "probe", {"profile_id": "shared:" + key})
    assert result["ok"] is False and "secret" not in json.dumps(result)
    assert "已隐藏" in result["provider_message"]


def test_shared_probe_success_redacts_custom_header(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )
    from agent_py_agent.agent.settings.model_provider_network import execute_provider_network

    admin, alice = admin_host(tmp_path), Host(tmp_path)
    key, _ = add(admin, model_custom_headers={"X-Private": "header-secret-value"})
    set_shared_profile(admin, key, True)
    reply = SimpleNamespace(text="only-private-secret header-secret-value hello", truncated=False, tool_use_blocks=[], usage={})
    monkeypatch.setattr("agent_py_agent.agent.settings.model_provider_network.get_backend",
                        lambda *args: SimpleNamespace(generate=lambda *args: reply))
    result = execute_provider_network(alice, read_model_profiles(model_profiles_path(alice.home_paths)),
                                      "probe", {"profile_id": "shared:" + key})
    assert result["ok"] is True and "secret" not in json.dumps(result)


def test_corrupt_catalog_does_not_block_private_menu(tmp_path):
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
    from agent_py_agent.tests.test_thread_model_selection import host_with_store

    alice = host_with_store(tmp_path)
    key, _ = add(alice)
    shared_catalog_path(alice.home_paths).write_text('{"schema": "bad"}')
    result = execute_local_model_operation(alice, "one", "select", {"profile_id": key})
    assert result["ok"] and result["selected"] == key and result["selection_available"]
    assert "共享模型目录暂不可用" in result["warning"]


def test_revoked_legacy_default_can_be_replaced_and_is_not_shown_as_default(tmp_path):
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
    from agent_py_agent.tests.test_thread_model_selection import host_with_store

    admin, alice = admin_host(tmp_path / "config"), host_with_store(tmp_path)
    key, _ = add(admin)
    set_shared_profile(admin, key, True)
    execute_model_profile_operation(alice, "set_default", {"profile_id": "shared:" + key})
    one = execute_local_model_operation(alice, "one", "list", {})
    thread = alice.conversation_store.threads.load(one["thread_id"])
    path = alice.conversation_store.threads.storage.thread_path(thread.thread_id)
    legacy = json.loads(path.read_text())
    legacy["model_profile_id"] = ""
    for field in ("model_selection_revision", "model_selection_source", "model_selection_last_explicit_revision"):
        legacy.pop(field)
    path.write_text(json.dumps(legacy))
    set_shared_profile(admin, key, False)
    unavailable = execute_local_model_operation(alice, "one", "list", {})
    assert unavailable["ok"] and not unavailable["selection_available"]
    assert unavailable["selected"] == "shared:" + key
    result = execute_local_model_operation(alice, "one", "select", {"profile_id": "default"})
    assert result["selection_available"] and result["selected"] == "default"
