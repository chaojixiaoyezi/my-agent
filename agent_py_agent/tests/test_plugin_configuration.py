"""私有配置合同：使用原安装表、文件锁和原子写入；不启动插件或真实模型。"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from agent_py_agent.agent.plugin_configuration import PluginConfigureRequest
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_manifest import canonical_plugin_settings
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_plugin_package import _bundle

SETTINGS_SCHEMA = {
    "type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20},
                                    "credential": {"type": "string"}},
    "required": ["limit"], "additionalProperties": False,
}


# LLM: 合成包仅验证安装和配置，未装 wheel；私有值只写临时 owner，不可用作真实产品验收。
# 函数用途: 为每个用例创建有必填设置的停用安装和原始安装请求。
def installed(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path / "private"))
    package = inspect_plugin_package(_bundle(change=lambda row: row.update(settings_schema=SETTINGS_SCHEMA)))
    request = PluginInstallRequest(package, "install-a", 0)
    entry = store.install(request).installation
    return store, entry, request


# LLM: 值走与产品同一原 schema 验证入口；故障用例通过 replace 显式构造坏输入。
# 函数用途: 构造绑定固定包和安装版本的一次配置替换。
def configure_request(entry, *, operation="configure-a", limit=3):
    return PluginConfigureRequest(entry.manifest.plugin_id, entry.package_sha256, operation,
                                  entry.revision, canonical_plugin_settings({"limit": limit}, entry.manifest.settings_schema))


def test_configure_atomic_private_state_and_replay(tmp_path):
    store, entry, install_request = installed(tmp_path)
    request = configure_request(entry)
    first = store.configure(request)
    assert first.installation.revision == first.installation.settings_revision == 2
    assert first.installation.settings_json == '{"limit":3}'
    assert first.installation.enabled is False and first.receipt.action == "configure"
    assert store.snapshot() == (first.installation,)
    path = store.root / "installations.json"
    saved = path.read_bytes(), path.stat().st_mtime_ns
    assert json.loads(saved[0])["migration"] is None
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.configure(request).outcome == "replayed"
    same = replace(request, operation_id="new-request", expected_revision=2)
    assert store.configure(same).outcome == "unchanged"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == saved
    with pytest.raises(PluginInstallationError, match="版本已变化"):
        store.install(install_request)
    assert store.install(replace(install_request, operation_id="install-b", expected_revision=2)).installation.settings_json == '{"limit":3}'


def test_v1_is_read_only_until_atomic_migration_with_configuration(tmp_path):
    store, entry, _ = installed(tmp_path)
    path = store.root / "installations.json"
    old = json.loads(path.read_text())
    old["schema_version"] = "plugin_installations.v1"
    del old["migration"]
    for row in old["installations"]:
        del row["settings_json"], row["settings_revision"]
        del row["last_commit"]["action"], row["last_commit"]["settings_sha256"]
    path.write_text(json.dumps(old))
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert store.snapshot() == (entry,)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    store.configure(configure_request(entry))
    current = json.loads(path.read_text())
    assert current["schema_version"] == "plugin_installations.v2"
    assert current["migration"] == {"from_schema": "plugin_installations.v1", "source_sha256": hashlib.sha256(before[0]).hexdigest()}
    assert store.snapshot()[0].settings_json == '{"limit":3}'


def test_install_receipt_cannot_claim_configured_values(tmp_path):
    store, _entry, _ = installed(tmp_path)
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    payload["installations"][0].update(settings_json='{"limit":3}', settings_revision=1)
    path.write_text(json.dumps(payload))
    with pytest.raises(PluginInstallationError):
        store.snapshot()


def test_configuration_checks_original_owner_quota_before_commit(tmp_path):
    store, entry, _ = installed(tmp_path)
    owner = resolve_owner_home(tmp_path / "private")
    owner.quota_json.write_text('{"max_disk_mb":1}')
    (owner.home_dir / "occupied.bin").write_bytes(b"x" * (1024 * 1024))
    before = (store.root / "installations.json").read_bytes()
    with pytest.raises(PluginInstallationError) as caught:
        store.configure(configure_request(entry))
    assert caught.value.reason == "quota_unavailable"
    assert (store.root / "installations.json").read_bytes() == before


@pytest.mark.parametrize("settings", ['{}', '{"limit":"3"}', '{"limit":0}', '{"limit":3,"extra":"secret"}',
                                     '{"limit":NaN}', '{"limit":3,"limit":4}', '[]', '{'])
def test_invalid_settings_do_not_change_original_record(tmp_path, settings):
    store, entry, _ = installed(tmp_path)
    path = store.root / "installations.json"
    before = path.read_bytes()
    with pytest.raises(PluginInstallationError) as caught:
        store.configure(replace(configure_request(entry), settings_json=settings))
    assert caught.value.reason == "invalid_settings"
    assert path.read_bytes() == before


@pytest.mark.parametrize("change,reason", [({"package_sha256": "0" * 64}, "package_conflict"),
                                         ({"plugin_id": "missing"}, "plugin_missing"),
                                         ({"expected_revision": 0}, "revision_conflict"),
                                         ({"operation_id": "install-a"}, "operation_conflict")])
def test_wrong_binding_cannot_configure(tmp_path, change, reason):
    store, entry, _ = installed(tmp_path)
    with pytest.raises(PluginInstallationError) as caught:
        store.configure(replace(configure_request(entry), **change))
    assert caught.value.reason == reason
    assert store.snapshot() == (entry,)


def test_reused_configure_id_with_changed_input_and_concurrent_cas(tmp_path):
    store, entry, _ = installed(tmp_path)
    barrier = Barrier(2)

    def configure(limit):
        barrier.wait(timeout=5)
        try:
            return store.configure(configure_request(entry, limit=limit, operation=f"configure-{limit}")).outcome
        except PluginInstallationError as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(configure, (3, 4)))
    assert sorted(results) == ["configured", "revision_conflict"]
    saved = store.snapshot()[0]
    wrong = configure_request(entry, limit=5, operation=saved.last_commit.operation_id)
    with pytest.raises(PluginInstallationError) as caught:
        store.configure(wrong)
    assert caught.value.reason == "operation_conflict"
    assert store.snapshot() == (saved,)


@pytest.mark.parametrize("outcome", ["before", "after", "unreadable"])
def test_configure_commit_failure_retains_actual_classification(tmp_path, monkeypatch, outcome):
    from agent_py_agent.agent import plugin_install_store

    store, entry, _ = installed(tmp_path)
    write = plugin_install_store.write_text_atomic_beneath
    request = configure_request(entry)

    def fail(root, parts, text):
        if outcome == "after":
            write(root, parts, text)
        elif outcome == "unreadable":
            (store.root / "installations.json").write_text("{")
        raise OSError("injected acknowledgement failure")

    monkeypatch.setattr(plugin_install_store, "write_text_atomic_beneath", fail)
    with pytest.raises(PluginInstallationError) as caught:
        store.configure(request)
    assert caught.value.commit_state == {"before": "not_committed", "after": "committed", "unreadable": "unknown"}[outcome]
    assert caught.value.receipt.action == "configure"
    if outcome == "after":
        assert store.configure(request).outcome == "replayed"
    elif outcome == "before":
        assert store.snapshot() == (entry,)


@pytest.mark.parametrize("change", [
    lambda row: row.pop("migration"),
    lambda row: row.update(migration={"from_schema": "unknown", "source_sha256": "0" * 64}),
    lambda row: row["installations"][0].update(settings_json='{"limit":7}'),
    lambda row: row["installations"][0].update(settings_revision=True),
    lambda row: row["installations"][0]["last_commit"].pop("action"),
    lambda row: row["installations"][0].update(enabled=True),
])
def test_bad_v2_cannot_fall_back_to_v1_or_repair_missing_private_values(tmp_path, change):
    store, entry, _ = installed(tmp_path)
    store.configure(configure_request(entry))
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    change(payload)
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(PluginInstallationError):
        store.snapshot()
    with pytest.raises(PluginInstallationError):
        store.configure(configure_request(entry, operation="another"))
    assert path.read_bytes() == before
