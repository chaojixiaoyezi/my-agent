"""用固定 17i 源码真实解码 v4/导出 v3；临时 owner，无 Gateway。"""
import hashlib
import json
import subprocess
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.plugin_permissions.rollback import export_installations_v3
from agent_py_agent.tests.test_plugin_activation import activation_fixture, publication
from agent_py_agent.tests.test_plugin_install_store import _request
from agent_py_agent.tests.test_plugin_legacy_records import granted
from agent_py_agent.tests.test_plugin_legacy_state import upgrade_source

REVISION = "22e02c668"
# 17i（22e02c668）原文件逐字节存为测试夹具：Linux 车道和浅克隆没有 Git 历史，不能在用例里 git show（3a 10-05）。
#   哈希钉住夹具与 22e02c668 原文一致；更新夹具必须同步这里，不能拿当前解码器冒充旧代码。
_LEGACY_17I = Path(__file__).resolve().parent / "fixtures" / "legacy_17i"
_LEGACY_17I_SHA256 = {
    "plugin_activation_record": "67949fd238bb2cbe18f01423dada4d8d1391ebaee78c631fde33e6dd51730518",
    "plugin_installation": "3aae2373898af0271439da3a566b32086b9971291d31899a5ddd009aaf22e582",
    "plugin_installation_state": "c1f024c59e6cfc5d32476a95cd6a6013c228fab0745eb48664a9b4f5841129a0",
    "plugin_install_store": "ee92b4595443589b578de826f9ccd1ff57e95099efc4f1578a93529912d949c2",
}


def old_readers(monkeypatch):
    """固定 17i 原文件（夹具，哈希核对）读原函数及三个真实依赖，不用当前宽化后的解码器冒充旧代码。"""
    modules = []
    with monkeypatch.context() as patch:
        for name in ("plugin_activation_record", "plugin_installation", "plugin_installation_state", "plugin_install_store"):
            source = (_LEGACY_17I / f"{name}.py.txt").read_bytes()
            assert hashlib.sha256(source).hexdigest() == _LEGACY_17I_SHA256[name], name
            alias = f"agent_py_agent.agent.{name}"
            module = types.ModuleType(alias)
            module.__package__ = "agent_py_agent.agent"
            patch.setitem(sys.modules, alias, module)
            exec(compile(source, f"{REVISION}:{name}", "exec"), module.__dict__)
            modules.append(module)
    return modules[-2].decode_installation_state, modules[-1].PluginInstallStore


def test_17i_rejects_v4_without_overwriting_and_exported_v3_keeps_every_plugin(tmp_path, monkeypatch):
    store, _, request = activation_fixture(tmp_path)
    store.change_activation(publication(store.change_activation(request)))
    old_path = upgrade_source(store)
    old = store.snapshot()[0]
    store.install(_request(tmp_path, operation_id="other-install", plugin_id="other-peek"))
    before = old_path.read_bytes()
    decode, old_store_type = old_readers(monkeypatch)
    with pytest.raises(ValueError, match="安装表协议无效"):
        decode(before, store._owner)
    with pytest.raises(ValueError) as error:
        old_store_type(store_owner(store)).snapshot()
    assert error.value.reason == "invalid_state"
    assert old_path.read_bytes() == before
    exported = export_installations_v3(before, store._owner)
    assert exported is not None, "必须提供显式回滚导出，不能只让17i拒读"
    content, report = exported
    rows = decode(content, store._owner).entries
    assert {entry.manifest.plugin_id for entry in rows} == {entry.manifest.plugin_id for entry in store.snapshot()}
    active = next(entry for entry in rows if entry.manifest.plugin_id == old.manifest.plugin_id)
    assert active.enabled and active.activation_id == old.activation_id
    assert report["compatibility_authorizations_lost"] == [old.manifest.plugin_id]
    assert report["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert old_path.read_bytes() == before


def store_owner(store):
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    return resolve_owner_home(store._anchor)


@pytest.mark.parametrize("mode", ["restricted", "wide"])
def test_new_grant_downgrade_keeps_active_and_repairs_old_receipt_digest(tmp_path, monkeypatch, mode):
    store, active, _ = granted(tmp_path, mode)
    path = store.root / "installations.json"
    before = path.read_bytes()
    result = export_installations_v3(before, store._owner)
    assert result is not None, "v4新授权不能静默丢安装或丢enabled"
    content, report = result
    decode, _ = old_readers(monkeypatch)
    restored = decode(content, store._owner).entries[0]
    assert restored.enabled and restored.activation_id == active.activation_id
    assert restored.package_sha256 == active.package_sha256
    assert report["permissions_lost"] == [{"plugin_id": active.manifest.plugin_id, "mode": mode}]
    assert restored.last_commit.activation_sha256 == restored.activation.content_sha256
    assert path.read_bytes() == before


def run_export(source, output, confirmation=""):
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(root / "scripts/export_plugin_installations_v3.py"),
               "--source", str(source), "--output", str(output)]
    if confirmation:
        command += ["--confirm", confirmation]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    assert result.stdout, result.stderr
    return result.returncode, json.loads(result.stdout)


def test_export_cli_requires_complete_confirmation_and_17i_reads_output(tmp_path, monkeypatch):
    store, active, _ = granted(tmp_path, "restricted")
    source, output = store.root / "installations.json", tmp_path / "rollback-v3.json"
    before = source.read_bytes()
    code, preview = run_export(source, output)
    assert code == 0 and preview["state"] == "confirmation_required" and not output.exists()
    assert preview["permissions_lost"] == [{"plugin_id": active.manifest.plugin_id, "mode": "restricted"}]
    assert preview["enabled_preserved"] == [active.manifest.plugin_id] and preview["warning"]
    code, exported = run_export(source, output, preview["confirm_code"])
    assert code == 0 and exported["state"] == "exported", exported
    decode, _ = old_readers(monkeypatch)
    restored = decode(output.read_bytes(), store._owner).entries[0]
    assert restored.enabled and restored.activation_id == active.activation_id
    assert source.read_bytes() == before and output.stat().st_mode & 0o777 == 0o600
    code, refused = run_export(source, output, preview["confirm_code"])
    assert code == 1 and refused["state"] == "rejected"


def test_export_cli_source_drift_invalidates_confirmation_and_never_overwrites_source(tmp_path):
    store, _, _ = granted(tmp_path, "wide")
    source, output = store.root / "installations.json", tmp_path / "rollback-v3.json"
    _, preview = run_export(source, output)
    source.write_bytes(source.read_bytes() + b"\n")
    before = source.read_bytes()
    code, drifted = run_export(source, output, preview["confirm_code"])
    assert code == 0 and drifted["state"] == "confirmation_required"
    assert drifted["confirm_code"] != preview["confirm_code"] and not output.exists()
    code, rejected = run_export(source, source, preview["confirm_code"])
    assert code == 1 and rejected["state"] == "rejected" and source.read_bytes() == before


@pytest.mark.parametrize("damage", ["invalid", "symlink", "future", "missing_grant"])
def test_export_cli_bad_source_never_creates_output(tmp_path, damage):
    store, _, _ = granted(tmp_path, "wide")
    source, output = store.root / "installations.json", tmp_path / "rollback-v3.json"
    if damage == "symlink":
        link = tmp_path / "source-link.json"
        link.symlink_to(source)
        source = link
    if damage == "invalid":
        source.write_bytes(b"not-json")
    if damage in {"future", "missing_grant"}:
        payload = json.loads(source.read_bytes())
        if damage == "future":
            payload["schema_version"] = "plugin_installations.v5"
        else:
            payload["installations"][0].pop("legacy_permission_grant")
        source.write_text(json.dumps(payload))
    before = source.read_bytes()
    code, rejected = run_export(source, output)
    assert code == 1 and rejected["state"] == "rejected" and not output.exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_every_original_schema_persists_v4_then_exports_old_readable_all_installs(tmp_path, monkeypatch, version):
    store, _, _ = activation_fixture(tmp_path)
    path = store.root / "installations.json"
    payload = json.loads(path.read_bytes())
    payload["schema_version"] = "plugin_installations." + version
    row = payload["installations"][0]
    row.pop("legacy_permission_grant")
    if version != "v3":
        row.pop("activation")
        row.update(enabled=False, activation_id="")
        row["last_commit"].pop("activation_sha256")
    if version == "v1":
        payload.pop("migration")
        row.pop("settings_json")
        row.pop("settings_revision")
        row["last_commit"].pop("settings_sha256")
        row["last_commit"].pop("action")
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    assert legacy_free(store.snapshot()) and path.read_bytes() == original
    store.install(_request(tmp_path, operation_id="second-install", plugin_id="second-peek"))
    saved = json.loads(path.read_bytes())
    assert saved["schema_version"] == "plugin_installations.v4"
    assert saved["migration"]["from_schema"] == "plugin_installations." + version
    assert saved["migration"]["source_sha256"] == hashlib.sha256(original).hexdigest()
    exported, report = export_installations_v3(path.read_bytes(), store._owner)
    decode, _ = old_readers(monkeypatch)
    restored = decode(exported, store._owner).entries
    assert {row.manifest.plugin_id for row in restored} == {row.manifest.plugin_id for row in store.snapshot()}
    assert report["permissions_lost"] == [] and report["compatibility_authorizations_lost"] == []


def legacy_free(entries):
    from agent_py_agent.agent.plugin_permissions.state import legacy_permissions

    return all(legacy_permissions(entry) is None for entry in entries)
