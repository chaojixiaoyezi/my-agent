"""卸载 CAS 与包回收的文件系统合同，不代替实际 TUI 装卸验收。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from agent_py_agent.agent import plugin_install_store
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallation, PluginInstallationError
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_plugin_install_store import _request


def test_remove_keeps_other_installations_user_files_and_package_until_consumed(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    entry = store.install(request).installation
    other = store.install(_request(tmp_path, plugin_id="other", operation_id="other")).installation
    artifact = tmp_path / "user-result.txt"
    artifact.write_text("keep")
    removed = store.remove("remove", entry.manifest.plugin_id, entry)
    assert removed.outcome == "removed" and removed.commit_state == "committed"
    assert store.snapshot() == (other,)
    assert store.package_bytes(entry) == request.package.archive_bytes
    assert store.consume_removed_package(removed.receipt) == "removed"
    assert store.consume_removed_package(removed.receipt) == "removed"
    assert store.package_bytes(other) and artifact.read_text() == "keep"
    with pytest.raises(PluginInstallationError):
        store.package_bytes(entry)
    with pytest.raises(ValueError):
        PluginInstallation(entry.manifest, entry.package_sha256, removed.receipt.after_revision, removed.receipt)


def test_old_removal_and_absence_snapshot_cannot_delete_reinstallation(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    old = store.install(request).installation
    removed = store.remove("remove", old.manifest.plugin_id, old)
    with pytest.raises(PluginInstallationError):
        store.remove("remove", old.manifest.plugin_id, old)
    new = store.install(replace(request, operation_id="reinstall")).installation
    assert old.revision == new.revision and old.installation_ref != new.installation_ref
    for expected in (old, None):
        with pytest.raises(PluginInstallationError) as caught:
            store.remove("stale", old.manifest.plugin_id, expected)
        assert caught.value.reason == "revision_conflict"
    assert store.consume_removed_package(removed.receipt) == "retained_in_use"
    assert store.package_bytes(new) == request.package.archive_bytes
    assert store.snapshot() == (new,)


def test_active_or_revoked_installation_cannot_be_deleted_without_release(tmp_path):
    from agent_py_agent.tests.test_plugin_activation import activation_fixture, revocation

    store, _, request = activation_fixture(tmp_path)
    preparing = store.change_activation(request).installation
    with pytest.raises(PluginInstallationError) as caught:
        store.remove("remove", preparing.manifest.plugin_id, preparing)
    assert caught.value.reason == "activation_in_use"
    revoked = store.change_activation(revocation(preparing, "revoke")).installation
    with pytest.raises(PluginInstallationError):
        store.remove("remove", revoked.manifest.plugin_id, revoked)
    assert store.snapshot() == (revoked,)


@pytest.mark.parametrize("phase,expected_state", [("before", "not_committed"), ("after", "committed"),
                                                 ("unreadable", "unknown")])
def test_installation_delete_commit_errors_keep_package_and_exact_commit_state(tmp_path, monkeypatch, phase, expected_state):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    entry = store.install(_request(tmp_path)).installation
    original = plugin_install_store.write_text_atomic_beneath
    def failed_write(root, parts, text):
        if phase != "before":
            original(root, parts, "broken" if phase == "unreadable" else text)
        raise OSError("fixture write failure")
    monkeypatch.setattr(plugin_install_store, "write_text_atomic_beneath", failed_write)
    with pytest.raises(PluginInstallationError) as caught:
        store.remove("remove", entry.manifest.plugin_id, entry)
    assert caught.value.commit_state == expected_state
    assert caught.value.receipt.action == "remove"
    assert store.package_bytes(entry)
    if phase != "unreadable":
        assert store.snapshot() == ((entry,) if phase == "before" else ())


def test_package_cleanup_refuses_symlink_and_damaged_installation_table(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    entry = store.install(_request(tmp_path)).installation
    removed = store.remove("remove", entry.manifest.plugin_id, entry)
    package = store.root / "packages" / f"{entry.package_sha256}.zip"
    package.unlink()
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"keep")
    package.symlink_to(outside)
    with pytest.raises(OSError):
        store.consume_removed_package(removed.receipt)
    assert package.is_symlink() and outside.read_bytes() == b"keep"
    (store.root / "installations.json").write_text("broken")
    with pytest.raises(PluginInstallationError):
        store.consume_removed_package(removed.receipt)
    assert package.is_symlink()


def test_missing_confirmation_and_removal_do_not_require_quota(tmp_path):
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    entry = store.install(_request(tmp_path)).installation
    owner.quota_json.write_text("broken")
    removed = store.remove("remove", entry.manifest.plugin_id, entry)
    assert removed.commit_state == "committed"
    state = (store.root / "installations.json").stat().st_mtime_ns
    absent = store.remove("already-absent", entry.manifest.plugin_id, None)
    assert absent.outcome == "absent" and absent.receipt is None
    assert (store.root / "installations.json").stat().st_mtime_ns == state


def test_reinstallation_and_package_cleanup_share_the_original_lock(tmp_path, monkeypatch):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    old = store.install(request).installation
    removed = store.remove("remove", old.manifest.plugin_id, old)
    entered, release, consuming, consumed = Event(), Event(), Event(), Event()
    original = PluginInstallStore._save_blob
    def hold_install(instance, package):
        entered.set()
        assert release.wait(5)
        return original(instance, package)
    def consume():
        consuming.set()
        result = store.consume_removed_package(removed.receipt)
        consumed.set()
        return result
    monkeypatch.setattr(PluginInstallStore, "_save_blob", hold_install)
    with ThreadPoolExecutor(max_workers=2) as pool:
        installing = pool.submit(store.install, replace(request, operation_id="reinstall"))
        try:
            assert entered.wait(5)
            cleanup = pool.submit(consume)
            assert consuming.wait(5) and not consumed.wait(.05)
        finally:
            release.set()
        new = installing.result(timeout=5).installation
        assert cleanup.result(timeout=5) == "retained_in_use"
    assert store.snapshot() == (new,) and store.package_bytes(new) == request.package.archive_bytes
