from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_skills
from agent_py_agent.agent.capability import SkillsService
from agent_py_agent.agent.plugin_manifest import (
    PLUGIN_PACKAGE_SCHEMA_V3,
    PluginManifest,
    PluginPackageError,
)


def _write_skill(root: Path, name: str, description: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n按步骤执行。\n", encoding="utf-8")


def _service(tmp_path, roots):
    home = SimpleNamespace(owner_home_dir=tmp_path / "owner", shared_skills_dir=tmp_path / "shared",
                           shared_builtin_dir=tmp_path / "builtin")
    for directory in (home.owner_home_dir / "skills", home.shared_skills_dir, home.shared_builtin_dir):
        directory.mkdir(parents=True, exist_ok=True)
    policy = SimpleNamespace(owner_id="o", skills_enabled=True, enabled_skill_sources=("workspace", "owner", "shared", "builtin"),
                             enabled_shared_skills=(), disabled_skills=())
    service = SkillsService(home_paths=home, workspace_root=tmp_path / "ws", policy_provider=lambda: policy,
                            plugin_roots=lambda: roots)
    return service, home, policy


def test_plugin_skill_follows_enablement_and_never_shadows_owner(tmp_path):
    plugin_root = tmp_path / "env" / "genui_lite" / "skills"
    _write_skill(plugin_root, "table-render", "plugin skill")
    _write_skill(plugin_root, "shared-name", "plugin version")
    roots = [(plugin_root, "plugin:genui-lite")]
    service, home, policy = _service(tmp_path, roots)
    _write_skill(home.owner_home_dir / "skills", "shared-name", "owner version")

    snapshot = service.snapshot_for(tmp_path / "ws", force_reload=True)
    entry = snapshot.resolve("table-render")
    assert entry is not None and entry.source == "plugin:genui-lite"
    assert snapshot.resolve("shared-name").source == "owner"

    # 停用插件：提供方不再返回该根，下一次快照即消失（不需要手动清缓存）
    roots.clear()
    assert service.snapshot_for(tmp_path / "ws").resolve("table-render") is None

    # 总闸关闭时插件 Skill 同样不出现
    roots.append((plugin_root, "plugin:genui-lite"))
    policy.skills_enabled = False
    assert service.snapshot_for(tmp_path / "ws").resolve("table-render") is None


def test_enabled_plugin_skill_roots_only_active_declared(tmp_path, monkeypatch):
    def entry(plugin_id, enabled, skills):
        manifest = SimpleNamespace(plugin_id=plugin_id, skills=skills, entry_module=plugin_id.replace("-", "_"))
        activation = SimpleNamespace(plan=SimpleNamespace(environment_ref="env-" + plugin_id))
        return SimpleNamespace(enabled=enabled, manifest=manifest, activation=activation)

    rows = (entry("genui-lite", True, ("table-render",)), entry("off", False, ("x",)), entry("plain", True, ()))
    monkeypatch.setattr(plugin_skills, "PluginInstallStore", lambda owner: SimpleNamespace(snapshot=lambda: rows))
    owner = SimpleNamespace(plugins_dir=tmp_path / "plugins")
    roots = plugin_skills.enabled_plugin_skill_roots(owner)
    assert [source for _path, source in roots] == ["plugin:genui-lite"]
    path = roots[0][0]
    assert path.parts[-2:] == ("genui_lite", "skills") and "env-genui-lite" in path.parts
    assert plugin_skills.enabled_plugin_skill_roots(None) == ()

    def broken(owner):
        raise OSError("unreadable")

    monkeypatch.setattr(plugin_skills, "PluginInstallStore", broken)
    assert plugin_skills.enabled_plugin_skill_roots(owner) == ()


def _manifest_payload(**extra):
    payload = {
        "schema_version": PLUGIN_PACKAGE_SCHEMA_V3, "plugin_id": "genui-lite", "version": "0.1.0",
        "summary": "渲染", "entry_module": "genui_lite", "entry_wheel": "wheels/a.whl",
        "wheels": [{"path": "wheels/a.whl", "sha256": "0" * 64}],
        "actions": [], "default_action": "", "settings_schema": {"type": "object", "properties": {}},
        "tools": [{"name": "render", "description": "渲染", "input_schema": {"type": "object", "properties": {}},
                   "requested_effect": "mutating"}],
        "panels": [], "skills": ["table-render"],
    }
    payload.update(extra)
    return payload


def test_manifest_v3_round_trip_and_validation():
    manifest = PluginManifest.from_payload(_manifest_payload())
    assert manifest.skills == ("table-render",)
    again = manifest.to_payload()
    assert again["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V3 and again["skills"] == ["table-render"]
    assert PluginManifest.from_payload(json.loads(json.dumps(again))) == manifest
    for bad in ({"skills": []}, {"skills": ["Bad Name"]}, {"skills": ["a", "a"]}):
        with pytest.raises(PluginPackageError):
            PluginManifest.from_payload(_manifest_payload(**bad))
    legacy = _manifest_payload(schema_version="plugin_package.v1")
    del legacy["panels"], legacy["skills"]
    assert "skills" not in PluginManifest.from_payload(legacy).to_payload()
