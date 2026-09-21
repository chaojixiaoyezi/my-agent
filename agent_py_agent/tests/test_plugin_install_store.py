"""安装事实合同；使用临时私有目录，不安装 wheel、不运行插件或真实模型。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package, read_plugin_package
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_plugin_package import _bundle


# LLM: 来源仅由测试者创建候选包；安装服务必须保存同一快照，不能再次读取来源或执行其中内容。
# 函数用途: 构造一个可并发重放的安装请求，期望版本零表示未安装。
def _request(
    tmp_path,
    *,
    operation_id="install-a",
    plugin_id="sample-peek",
    version="1.0",
    expected_revision=0,
):
    source = tmp_path / f"{operation_id}.zip"
    source.write_bytes(_bundle(change=lambda row: row.update(plugin_id=plugin_id, version=version)))
    return PluginInstallRequest(read_plugin_package(source), operation_id, expected_revision)


def test_canonical_owner_path_is_same_in_both_projections_and_query_stays_cold(tmp_path):
    owner = resolve_owner_home(tmp_path, OwnerIdentity.provider_user("test", "user-a"))
    paths = home_paths_with_owner(home_paths(tmp_path), owner)
    assert paths.owner_plugins_dir == owner.plugins_dir == owner.data_dir / "plugins"
    assert PluginInstallStore(owner).snapshot() == ()
    assert not owner.home_dir.exists()
    assert not paths.owner_plugins_dir.exists()


def test_install_publishes_one_disabled_record_and_exact_package_without_import(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("安装事实不启动插件"))
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    request = _request(tmp_path)
    result = store.install(request)
    assert result.outcome == "installed"
    assert result.commit_state == "committed"
    assert result.installation.revision == 1
    assert result.installation.enabled is False
    assert result.installation.activation_id == ""
    assert result.receipt.operation_id == request.operation_id
    assert result.receipt.before_revision == 0 and result.receipt.after_revision == 1
    assert store.snapshot() == (result.installation,)
    assert store.package_bytes(result.installation) == request.package.archive_bytes
    assert not owner.agents_md.exists()
    assert (owner.plugins_dir / "installations.json").stat().st_mode & 0o777 == 0o600


def test_exact_retry_replays_and_new_request_is_noop_without_rewriting(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    first = store.install(request)
    path = store.root / "installations.json"
    before = path.read_bytes(), path.stat().st_mtime_ns
    replay = store.install(request)
    assert replay.outcome == "replayed" and replay.receipt == first.receipt
    unchanged = store.install(replace(request, operation_id="new-request", expected_revision=1))
    assert unchanged.outcome == "unchanged" and unchanged.receipt is None
    assert unchanged.installation == first.installation
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_other_plugin_install_does_not_erase_first_receipt(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    first = _request(tmp_path)
    original = store.install(first)
    store.install(_request(tmp_path, plugin_id="another", operation_id="install-b"))
    assert len(store.snapshot()) == 2
    assert store.install(first).receipt == original.receipt


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"operation_id": "another-operation", "expected_revision": 3}, "revision_conflict"),
        ({"operation_id": "another-operation"}, "revision_conflict"),
    ],
)
def test_stale_request_cannot_overwrite_existing_record(tmp_path, change, reason):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    store.install(request)
    before = (store.root / "installations.json").read_bytes()
    with pytest.raises(PluginInstallationError) as caught:
        store.install(replace(request, **change))
    assert caught.value.reason == reason
    assert caught.value.commit_state == "not_committed"
    assert (store.root / "installations.json").read_bytes() == before


def test_reused_operation_id_with_changed_input_conflicts(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    store.install(request)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(replace(request, expected_revision=1))
    assert caught.value.reason == "operation_conflict"


def test_version_or_content_replacement_is_explicit_conflict(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    store.install(_request(tmp_path))
    with pytest.raises(PluginInstallationError) as caught:
        store.install(
            _request(tmp_path, operation_id="different", version="2.0", expected_revision=1)
        )
    assert caught.value.reason == "package_conflict"
    assert store.snapshot()[0].manifest.version == "1.0"


def test_same_version_with_different_bytes_cannot_replace_installed_package(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    original = store.install(_request(tmp_path))
    changed = inspect_plugin_package(_bundle(change=lambda row: row.update(summary="不同内容")))
    assert changed.manifest.version == original.installation.manifest.version
    request = PluginInstallRequest(changed, "another", 1)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(request)
    assert caught.value.reason == "package_conflict"
    assert store.snapshot() == (original.installation,)


def test_copied_owner_state_is_rejected_and_not_overwritten(tmp_path):
    first = PluginInstallStore(
        resolve_owner_home(tmp_path, OwnerIdentity.provider_user("test", "one"))
    )
    second = PluginInstallStore(
        resolve_owner_home(tmp_path, OwnerIdentity.provider_user("test", "two"))
    )
    request = _request(tmp_path)
    first.install(request)
    shutil.copytree(first.root, second.root)
    before = (second.root / "installations.json").read_bytes()
    with pytest.raises(PluginInstallationError) as caught:
        second.install(request)
    assert caught.value.reason == "owner_mismatch"
    assert (second.root / "installations.json").read_bytes() == before


@pytest.mark.parametrize(
    "text", ["", "{}", "{", "[]", '{"schema_version":"one","schema_version":"two"}']
)
def test_damaged_state_is_never_replaced_by_empty_installation_table(tmp_path, text):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    store.root.mkdir(parents=True)
    path = store.root / "installations.json"
    path.write_text(text)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(_request(tmp_path))
    assert caught.value.reason == "invalid_state"
    assert path.read_text() == text


@pytest.mark.parametrize("after_replace", [False, True])
def test_commit_error_distinguishes_before_and_after_visible_replace(
    tmp_path, monkeypatch, after_replace
):
    from agent_py_agent.agent import plugin_install_store as module

    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    original = module.write_text_atomic_beneath

    def fail_write(root, parts, text):
        if after_replace:
            original(root, parts, text)
        raise OSError("injected commit failure")

    monkeypatch.setattr(module, "write_text_atomic_beneath", fail_write)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(request)
    assert caught.value.commit_state == ("committed" if after_replace else "not_committed")
    assert caught.value.receipt is not None
    if after_replace:
        assert store.install(request).outcome == "replayed"
    else:
        assert store.snapshot() == ()
        monkeypatch.setattr(module, "write_text_atomic_beneath", original)
        assert store.install(request).outcome == "installed"


def test_failed_commit_with_unreadable_outcome_reports_unknown(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_install_store as module

    store = PluginInstallStore(resolve_owner_home(tmp_path))

    def damage_then_fail(root, parts, text):
        (store.root / "installations.json").write_text("{")
        raise OSError("outcome unavailable")

    monkeypatch.setattr(module, "write_text_atomic_beneath", damage_then_fail)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(_request(tmp_path))
    assert caught.value.commit_state == "unknown"
    assert (store.root / "installations.json").read_text() == "{"


def test_unreferenced_blob_is_not_an_installation_and_bad_blob_is_not_reused(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    blobs = store.root / "packages"
    blobs.mkdir(parents=True)
    blob = blobs / f"{request.package.sha256}.zip"
    blob.write_bytes(b"damaged blob")
    assert store.snapshot() == ()
    with pytest.raises(PluginInstallationError) as caught:
        store.install(request)
    assert caught.value.reason == "package_integrity"
    assert blob.read_bytes() == b"damaged blob"
    assert not (store.root / "installations.json").exists()


def test_record_with_missing_package_cannot_be_reported_as_usable_installation(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    record = store.install(request).installation
    (store.root / "packages" / f"{request.package.sha256}.zip").unlink()
    with pytest.raises(PluginInstallationError) as caught:
        store.package_bytes(record)
    assert caught.value.reason == "package_integrity"
    assert store.snapshot() == (record,)


@pytest.mark.parametrize("damage", ["missing", "changed"])
@pytest.mark.parametrize("replay", [True, False])
def test_package_damage_keeps_original_commit_fact_on_retry(tmp_path, damage, replay):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    result = store.install(request)
    blob = store.root / "packages" / f"{request.package.sha256}.zip"
    if damage == "missing":
        blob.unlink()
    else:
        blob.write_bytes(b"broken")
    state = (store.root / "installations.json").read_bytes()
    retry = request if replay else replace(request, operation_id="new", expected_revision=1)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(retry)
    assert caught.value.reason == "package_integrity"
    assert caught.value.commit_state == ("committed" if replay else "not_committed")
    assert caught.value.receipt == (result.receipt if replay else None)
    assert (store.root / "installations.json").read_bytes() == state


def test_existing_state_record_cannot_forge_a_new_activation(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    store.install(_request(tmp_path))
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    payload["installations"][0]["enabled"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(PluginInstallationError):
        store.snapshot()


def test_lock_cleanup_error_does_not_report_committed_install_as_absent(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_install_store as module

    store = PluginInstallStore(resolve_owner_home(tmp_path))
    original = module.locked_private_directory

    @contextmanager
    def fail_after_release(root, **kwargs):
        with original(root, **kwargs):
            yield
        raise OSError("lock cleanup failure")

    monkeypatch.setattr(module, "locked_private_directory", fail_after_release)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(_request(tmp_path))
    assert caught.value.commit_state == "committed"
    assert caught.value.receipt == store.snapshot()[0].last_commit


@pytest.mark.parametrize("commit_state", ["committed", "unknown", "not_committed"])
def test_commit_error_and_lock_cleanup_error_preserve_first_receipt(
    tmp_path, monkeypatch, commit_state
):
    from agent_py_agent.agent import plugin_install_store as module

    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    original_write = module.write_text_atomic_beneath
    original_lock = module.locked_private_directory

    def fail_write(root, parts, text):
        if commit_state == "committed":
            original_write(root, parts, text)
        elif commit_state == "unknown":
            (store.root / "installations.json").write_text("{")
        raise OSError("commit acknowledgement lost")

    @contextmanager
    def fail_cleanup(root, **kwargs):
        try:
            with original_lock(root, **kwargs):
                yield
        finally:
            raise OSError("lock cleanup failed too")

    monkeypatch.setattr(module, "write_text_atomic_beneath", fail_write)
    monkeypatch.setattr(module, "locked_private_directory", fail_cleanup)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(request)
    assert caught.value.reason == "commit_unconfirmed"
    assert caught.value.commit_state == commit_state
    assert caught.value.receipt.operation_id == request.operation_id
    assert caught.value.receipt.input_digest == request.input_digest


def test_failed_blob_publication_leaves_no_installed_record(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_install_store as module

    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    original = module.write_bytes_atomic_beneath

    def fail_after_blob(root, parts, content):
        original(root, parts, content)
        raise OSError("blob publication acknowledgement lost")

    monkeypatch.setattr(module, "write_bytes_atomic_beneath", fail_after_blob)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(request)
    assert caught.value.commit_state == "not_committed"
    assert store.snapshot() == ()
    monkeypatch.setattr(module, "write_bytes_atomic_beneath", original)
    assert store.install(request).outcome == "installed"


def test_forged_package_snapshot_does_not_write_owner_data(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    request = _request(tmp_path)
    forged = replace(request.package, manifest=replace(request.package.manifest, version="forged"))
    with pytest.raises(PluginInstallationError) as caught:
        store.install(replace(request, package=forged))
    assert caught.value.reason == "package_integrity"
    assert not store.root.exists()


def test_owner_data_symlink_cannot_redirect_installation(tmp_path):
    owner = resolve_owner_home(tmp_path)
    owner.home_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    owner.data_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PluginInstallationError):
        PluginInstallStore(owner).install(_request(tmp_path))
    assert not list(outside.iterdir())


def test_lock_leaf_symlink_cannot_create_file_outside_owner(tmp_path):
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    store.root.mkdir(parents=True)
    outside = tmp_path / "outside-lock"
    (store.root / ".plugins.lock").symlink_to(outside)
    with pytest.raises(PluginInstallationError) as caught:
        store.install(_request(tmp_path))
    assert caught.value.commit_state == "not_committed"
    assert not outside.exists()
    assert not (store.root / "installations.json").exists()


def test_directory_replacement_after_open_cannot_redirect_lock_creation(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs as fs

    if not fs._supports_dir_fd():
        pytest.skip("requires descriptor-relative directory traversal")
    store = PluginInstallStore(resolve_owner_home(tmp_path))
    store.root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = fs.open_directory_beneath
    moved = store.root.with_name("moved-plugins")

    def swap_after_open(root, parts, **kwargs):
        descriptor = original(root, parts, **kwargs)
        if not moved.exists():
            store.root.rename(moved)
            store.root.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(fs, "open_directory_beneath", swap_after_open)
    with pytest.raises(PluginInstallationError):
        store.install(_request(tmp_path))
    assert not list(outside.iterdir())
    assert (moved / ".plugins.lock").is_file()
    assert not (moved / "installations.json").exists()


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize(
    "same_plugin,same_operation,expected",
    [
        (False, False, ["installed", "installed"]),
        (True, False, ["installed", "revision_conflict"]),
        (True, True, ["installed", "replayed"]),
    ],
)
def test_independent_processes_serialize_installation_and_keep_receipts(
    tmp_path, same_plugin, same_operation, expected, portable
):
    first = _request(tmp_path)
    second_id = first.operation_id if same_operation else "install-b"
    if not same_operation:
        _request(
            tmp_path, operation_id=second_id, plugin_id="sample-peek" if same_plugin else "another"
        )
    script = """
import json, sys, traceback
from pathlib import Path
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_package import read_plugin_package
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.agent.common import nofollow_fs
if sys.argv[4] == 'portable':
    nofollow_fs._supports_dir_fd = lambda: False
store = PluginInstallStore(resolve_owner_home(Path(sys.argv[1])))
request = PluginInstallRequest(read_plugin_package(Path(sys.argv[2])), sys.argv[3], 0)
print('ready', flush=True)
sys.stdin.readline()
try:
    result = store.install(request)
    print(json.dumps({'outcome': result.outcome}), flush=True)
except PluginInstallationError as exc:
    print(json.dumps({'outcome': exc.reason, 'cause': traceback.format_exc()}), flush=True)
"""
    children = []
    try:
        for operation in (first.operation_id, second_id):
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(tmp_path),
                        str(tmp_path / f"{operation}.zip"),
                        operation,
                        "portable" if portable else "native",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            out, err = child.communicate(timeout=10)
            assert child.returncode == 0, err
            results.append(json.loads(out))
        assert sorted(row["outcome"] for row in results) == expected, "\n".join(
            row.get("cause", "") for row in results
        )
        records = PluginInstallStore(resolve_owner_home(tmp_path)).snapshot()
        assert len(records) == (1 if same_plugin else 2)
        assert all(record.revision == 1 and not record.enabled for record in records)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
