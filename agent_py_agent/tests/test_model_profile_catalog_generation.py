"""原目录代次的只读迁移、跨重启复核、凭据变化与采用锁测试；全部使用临时假配置。"""

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.settings import model_oauth, model_profiles
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation as op,
)
from agent_py_agent.agent.settings.model_profiles import (
    model_profile_generation,
    model_profile_generation_guard,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import (
    ModelProfileError,
    ModelProfileGeneration,
)
from agent_py_agent.agent.settings.shared_model_catalog import (
    _admin_profiles_path,
    public_shared_profiles,
    set_shared_profile,
    shared_catalog_path,
)
from agent_py_agent.tests.test_model_oauth import host as oauth_host
from agent_py_agent.tests.test_model_oauth import login, setup
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_shared_model_catalog import admin_host


# LLM: 只把临时测试目录降成真实旧格式，不经生产写入伪造过去代次；不得用于实际用户配置。
# 函数用途: 构造没有持久版本的旧 owner/shared 文件，验证只读与显式准备边界。
def _legacy(path, schema):
    data = json.loads(path.read_text())
    data["schema"] = schema
    data.pop("catalog_generation")
    path.write_text(json.dumps(data))
    return path.read_bytes()


# LLM: 沿原 guard 检查，不以公开列表或同名模型推断当前性；不修改文件。
# 函数用途: 让各 mutation 测试验证同一个最终采用入口。
def _current(host, expected):
    with model_profile_generation_guard(host, expected) as current:
        return current


def test_reads_do_not_create_or_upgrade_and_enabled_prepare_is_idempotent(tmp_path):
    host = Host(tmp_path / "config")
    path = model_profiles_path(host.home_paths)
    assert read_model_profiles(path)["catalog_generation"] is None
    assert model_profile_generation(host, "default", initialize=True) is None
    op(host, "list", {})
    assert not host.home_paths.config_dir.exists()
    key, _ = add(host)
    before = _legacy(path, "owner_model_profiles.v4")
    assert model_profile_generation(host, key) is None
    op(host, "list", {})
    selected_model_config(host, profile_id=key)
    assert path.read_bytes() == before
    expected = model_profile_generation(host, key, initialize=True)
    assert isinstance(expected, ModelProfileGeneration)
    saved = path.read_bytes()
    assert model_profile_generation(host, key, initialize=True) == expected
    assert path.read_bytes() == saved
    data, legacy = json.loads(saved), json.loads(before)
    assert data["schema"] == "owner_model_profiles.v5"
    assert {k: v for k, v in data.items() if k not in {"schema", "catalog_generation"}} == {
        k: v for k, v in legacy.items() if k != "schema"
    }


def test_generation_is_serializable_and_stable_in_new_process(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host, model_custom_headers={"X-Private": "fake-private-header"})
    expected = model_profile_generation(host, key)
    encoded = json.dumps(expected.to_dict())
    assert ModelProfileGeneration.from_dict(json.loads(encoded)) == expected
    assert all(value not in encoded + repr(expected) for value in (
        "only-private-secret", "fake-private-header", "example.test", str(tmp_path), "MiniMax",
    ))
    source = """
import json, sys
from pathlib import Path
from types import SimpleNamespace
from agent_py_agent.agent.settings.model_profiles import model_profile_generation
home = SimpleNamespace(config_dir=Path(sys.argv[1]), owner_provider='local', owner_kind='user', owner_id='alice')
print(json.dumps(model_profile_generation(SimpleNamespace(home_paths=home), sys.argv[2]).to_dict()))
"""
    result = subprocess.run([sys.executable, "-c", source, str(tmp_path), key], check=True,
                            capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2], timeout=10)
    assert ModelProfileGeneration.from_dict(json.loads(result.stdout)) == expected


@pytest.mark.parametrize("field,value", [
    ("api_key", "rotated-fake-key"), ("api_base", "https://new.example.test/v1"),
    ("custom_headers", {"X-Private": "rotated-header"}), ("session_header", "X-Session"),
    ("enabled", False), ("capabilities", ["embedding"]), ("display_name", "新名称"),
])
def test_every_provider_change_invalidates_pending(tmp_path, field, value):
    host = Host(tmp_path)
    key, _ = add(host)
    expected = model_profile_generation(host, key)
    op(host, "save_provider", {"provider_id": "provider-" + key, "editing": True, "provider": {field: value}})
    assert not _current(host, expected)


@pytest.mark.parametrize("field,value", [
    ("model_name", "new-model"), ("model_backend", "openai_compatible"),
    ("model_context_window_tokens", 1000000), ("enabled", False), ("temperature", "0.5"),
    ("top_p", 0.6), ("model_queue_wait_seconds", 3), ("capability", "embedding"),
])
def test_every_model_change_invalidates_pending(tmp_path, field, value):
    host = Host(tmp_path)
    key, _ = add(host)
    expected = model_profile_generation(host, key)
    row = read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]
    op(host, "save_model", {"profile_id": key, "editing": True, "profile": {**row, field: value}})
    assert not _current(host, expected)


def test_clear_delete_recreate_and_other_owner_cannot_reuse_version(tmp_path):
    host, other = Host(tmp_path), Host(tmp_path, "bob")
    key, _ = add(host)
    expected = model_profile_generation(host, key)
    assert not _current(other, expected)
    op(host, "save_provider", {"provider_id": "provider-" + key, "editing": True,
                               "clear_key": True, "provider": {}})
    assert not _current(host, expected)
    op(host, "delete_model", {"profile_id": key})
    op(host, "delete_provider", {"provider_id": "provider-" + key})
    assert not _current(host, expected)
    from agent_py_agent.tests.test_model_profiles import profile

    op(host, "add", {"profile_id": key, "profile": profile()})
    assert model_profile_generation(host, key) != expected


@pytest.mark.parametrize("value", [None, True, 1, "", "secret-input", "0" * 32, str(uuid4())])
def test_current_schema_rejects_bad_generation_without_rewriting(tmp_path, value):
    host = Host(tmp_path)
    add(host)
    path = model_profiles_path(host.home_paths)
    data = json.loads(path.read_text())
    data["catalog_generation"] = value
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ModelProfileError) as caught:
        read_model_profiles(path)
    assert "secret-input" not in str(caught.value) and path.read_bytes() == before


def test_old_schema_cannot_smuggle_generation_and_missing_current_is_invalid(tmp_path):
    host = Host(tmp_path)
    add(host)
    path = model_profiles_path(host.home_paths)
    data = json.loads(path.read_text())
    data["schema"] = "owner_model_profiles.v4"
    path.write_text(json.dumps(data))
    with pytest.raises(ModelProfileError):
        read_model_profiles(path)
    data["schema"] = "owner_model_profiles.v5"
    data.pop("catalog_generation")
    path.write_text(json.dumps(data))
    with pytest.raises(ModelProfileError):
        read_model_profiles(path)


def test_snapshot_is_strict_and_not_a_capability(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    expected = model_profile_generation(host, key)
    for field in expected.to_dict():
        value = expected.to_dict()
        del value[field]
        with pytest.raises(ModelProfileError):
            ModelProfileGeneration.from_dict(value)
    for patch in ({"path": "private-secret"}, {"catalog_generation": "private-secret"},
                  {"authority_id": "private-secret"}, {"profile_id": "default"},
                  {"shared_generation": uuid4().hex}):
        with pytest.raises(ModelProfileError) as caught:
            ModelProfileGeneration.from_dict({**expected.to_dict(), **patch})
        assert "private-secret" not in str(caught.value)
    assert not _current(host, replace(expected, authority_id="f" * 64))
    assert not _current(host, None)


def test_failed_private_save_does_not_publish_generation(tmp_path, monkeypatch):
    host = Host(tmp_path)
    key, _ = add(host)
    path = model_profiles_path(host.home_paths)
    before = path.read_bytes()
    data = read_model_profiles(path)
    generation = data["catalog_generation"]

    def fail(*args):
        raise OSError("injected save failure")

    monkeypatch.setattr(model_profiles.os, "replace", fail)
    with locked_json_path(path), pytest.raises(OSError):
        model_profiles._save_profiles(path, data)
    assert data["catalog_generation"] == generation and path.read_bytes() == before
    assert model_profile_generation(host, key).catalog_generation == generation
    assert not list(path.parent.glob(".models-*"))


def test_guard_holds_original_lock_until_cas_and_releases_after_error(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    expected = model_profile_generation(host, key)
    started, finished = threading.Event(), threading.Event()

    def mutate():
        started.set()
        op(host, "save_provider", {"provider_id": "provider-" + key, "editing": True,
                                   "provider": {"api_key": "late-key"}})
        finished.set()

    with ThreadPoolExecutor(1) as pool:
        with model_profile_generation_guard(host, expected) as current:
            assert current
            future = pool.submit(mutate)
            assert started.wait(2)
            with pytest.raises(BlockingIOError):
                model_profile_generation(host, key, initialize=True)
            assert not finished.is_set()
        future.result(timeout=3)
    assert not _current(host, expected)
    current = model_profile_generation(host, key)
    with pytest.raises(RuntimeError), model_profile_generation_guard(host, current):
        raise RuntimeError("caller CAS failed")
    assert _current(host, current)


def test_guard_uses_original_cross_process_lock(tmp_path):
    from agent_py_agent.agent.common import json_io

    if json_io.fcntl is None:
        pytest.skip("原平台没有 OS 文件锁，不能声称跨进程互斥")
    host = Host(tmp_path)
    key, _ = add(host)
    expected = model_profile_generation(host, key)
    source = """
import json, sys
from pathlib import Path
from types import SimpleNamespace
from agent_py_agent.agent.settings.model_profiles import model_profile_generation_guard
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileGeneration
home = SimpleNamespace(config_dir=Path(sys.argv[1]), owner_provider='local', owner_kind='user', owner_id='alice')
try:
    with model_profile_generation_guard(SimpleNamespace(home_paths=home), ModelProfileGeneration.from_dict(json.loads(sys.argv[2]))) as current:
        print('current' if current else 'stale')
except BlockingIOError:
    print('busy')
"""
    with model_profile_generation_guard(host, expected) as current:
        assert current
        result = subprocess.run([sys.executable, "-c", source, str(tmp_path), json.dumps(expected.to_dict())],
                                check=True, capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[2], timeout=10)
        assert result.stdout.strip() == "busy"


def test_shared_generation_covers_source_grant_and_local_owner_lock(tmp_path):
    admin, alice = admin_host(tmp_path), Host(tmp_path)
    key, _ = add(admin)
    set_shared_profile(admin, key, True)
    shared = "shared:" + key
    expected = model_profile_generation(alice, shared)
    assert expected.shared_generation and _current(alice, expected)
    with model_profile_generation_guard(alice, expected) as current:
        assert current
        for path in (model_profiles_path(alice.home_paths), _admin_profiles_path(alice.home_paths),
                     shared_catalog_path(alice.home_paths)):
            with pytest.raises(BlockingIOError), locked_json_path(path, blocking=False):
                pytest.fail("guard did not hold original lock")
    assert not model_profiles_path(alice.home_paths).exists()
    op(admin, "save_provider", {"provider_id": "provider-" + key, "editing": True,
                                "provider": {"api_key": "new-admin-key"}})
    assert not _current(alice, expected)
    current = model_profile_generation(alice, shared)
    set_shared_profile(admin, key, False)
    assert not _current(alice, current)
    set_shared_profile(admin, key, True)
    assert not _current(alice, current)
    assert model_profile_generation(alice, shared).shared_generation != current.shared_generation


@pytest.mark.parametrize("old_source,old_grant", [(True, False), (False, True), (True, True)])
def test_legacy_shared_is_unknown_for_user_and_admin_prepare_migrates(tmp_path, old_source, old_grant):
    admin, alice = admin_host(tmp_path), Host(tmp_path)
    key, _ = add(admin)
    set_shared_profile(admin, key, True)
    source, grant = model_profiles_path(admin.home_paths), shared_catalog_path(admin.home_paths)
    if old_source:
        _legacy(source, "owner_model_profiles.v4")
    if old_grant:
        _legacy(grant, "shared_model_catalog.v1")
    before = source.read_bytes(), grant.read_bytes()
    assert public_shared_profiles(alice.home_paths)
    assert model_profile_generation(alice, "shared:" + key, initialize=True) is None
    assert (source.read_bytes(), grant.read_bytes()) == before
    expected = model_profile_generation(admin, "shared:" + key, initialize=True)
    assert expected and model_profile_generation(alice, "shared:" + key) == expected
    assert json.loads(source.read_text())["schema"] == "owner_model_profiles.v5"
    assert json.loads(grant.read_text())["schema"] == "shared_model_catalog.v2"


def test_admin_publish_migrates_old_source_without_changing_models(tmp_path):
    admin = admin_host(tmp_path)
    key, _ = add(admin)
    source = model_profiles_path(admin.home_paths)
    before = json.loads(_legacy(source, "owner_model_profiles.v4"))
    set_shared_profile(admin, key, True)
    assert model_profile_generation(admin, "shared:" + key) is not None
    saved = json.loads(source.read_text())
    assert saved["profiles"] == before["profiles"] and saved["providers"] == before["providers"]


@pytest.mark.parametrize("patch", [{"catalog_generation": None}, {"catalog_generation": "fake-secret"},
                                    {"schema": "shared_model_catalog.v1"}])
def test_bad_shared_generation_is_not_repaired_or_adopted(tmp_path, patch):
    admin, alice = admin_host(tmp_path), Host(tmp_path)
    key, _ = add(admin)
    set_shared_profile(admin, key, True)
    expected = model_profile_generation(alice, "shared:" + key)
    path = shared_catalog_path(admin.home_paths)
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, **patch}))
    before = path.read_bytes()
    assert not _current(alice, expected)
    with pytest.raises(ModelProfileError):
        model_profile_generation(admin, "shared:" + key, initialize=True)
    assert path.read_bytes() == before


def test_failed_shared_save_keeps_old_generation_and_partial_migration_stays_unknown(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import shared_model_catalog

    admin, alice = admin_host(tmp_path), Host(tmp_path)
    key, _ = add(admin)
    set_shared_profile(admin, key, True)
    expected = model_profile_generation(alice, "shared:" + key)
    source, grant = model_profiles_path(admin.home_paths), shared_catalog_path(admin.home_paths)
    before = grant.read_bytes()

    def fail(*args):
        raise OSError("injected publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(shared_model_catalog, "write_json_file_atomic_unlocked", fail)
        with pytest.raises(OSError):
            set_shared_profile(admin, key, False)
        assert grant.read_bytes() == before and _current(alice, expected)
        _legacy(source, "owner_model_profiles.v4")
        _legacy(grant, "shared_model_catalog.v1")
        with pytest.raises(OSError):
            model_profile_generation(admin, "shared:" + key, initialize=True)
        assert json.loads(source.read_text())["schema"] == "owner_model_profiles.v5"
        assert model_profile_generation(alice, "shared:" + key) is None
    assert model_profile_generation(admin, "shared:" + key, initialize=True) is not None


def test_oauth_read_is_stable_refresh_rotates_catalog_but_not_auth_session(tmp_path, monkeypatch):
    host = oauth_host(tmp_path)
    key = setup(host)
    login(host, monkeypatch)
    config = selected_model_config(host, profile_id=key)
    expected = model_profile_generation(host, key)
    op(host, "auth_status", {"provider_id": "account"})
    assert model_oauth.request_credentials(config.model_auth_ref, config.api_base)[0] == "private-access"
    assert model_profile_generation(host, key) == expected
    path = model_profiles_path(host.home_paths)
    with locked_json_path(path):
        data = read_model_profiles(path)
        data["providers"]["account"]["auth"]["expires_at"] = 1
        model_profiles._save_profiles(path, data)
    before = model_profile_generation(host, key)
    monkeypatch.setattr(model_oauth, "refresh_tokens", lambda auth: {
        "access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_at": time.time() + 600,
    })
    assert model_oauth.request_credentials(config.model_auth_ref, config.api_base)[0] == "rotated-access"
    assert not _current(host, before)
    after = model_profile_generation(host, key)
    assert "private-" not in repr(after) and "rotated-" not in repr(after)
    assert selected_model_config(host, profile_id=key).model_auth_ref["generation"] == config.model_auth_ref["generation"]
    attempt = op(host, "auth_start", {"provider_id": "account"})
    assert not _current(host, after)
    before_cancel = model_profile_generation(host, key)
    op(host, "auth_cancel", {"provider_id": "account", "attempt_id": attempt["attempt_id"]})
    assert not _current(host, before_cancel)
    before_login = model_profile_generation(host, key)
    login(host, monkeypatch, token="new-login")
    assert not _current(host, before_login)
    before_logout = model_profile_generation(host, key)
    op(host, "auth_logout", {"provider_id": "account"})
    assert not _current(host, before_logout)
