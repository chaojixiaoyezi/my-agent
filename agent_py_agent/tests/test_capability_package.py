"""无进程能力包的静态合同；所有文件在临时目录，不运行模型或包内脚本。"""

import hashlib
import io
import json
import zipfile
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability_package_manifest import CapabilityFile
from agent_py_agent.agent.plugin_manifest import PluginManifest, PluginPackageError
from agent_py_agent.agent.plugin_package import (
    PackageReadLimits,
    inspect_plugin_package,
    read_plugin_member,
)
from scripts.build_capability_package import build_capability_package


# LLM: 只生成临时静态包，文件名和内容由测试明确提供，不能当作学习或真实验收证据。
# 函数用途: 提供内容声明、归档和修改入口，供格式与生命周期测试共用。
def content_bundle(*, files=None, version="1.0", change=None):
    files = files if files is not None else {"CAPABILITY.md": b"# Content\n", "methods/SKILL.md": b"private method"}
    manifest = {"schema_version": "plugin_package.v7", "package_kind": "capability", "plugin_id": "story-content",
                "version": version, "summary": "内容能力示例", "capability": {
                    "description": "把长篇材料整理成分镜", "keywords": ["分镜", "故事"], "entry_document": "CAPABILITY.md"},
                "files": [{"path": path, "sha256": hashlib.sha256(content).hexdigest(), "executable": False}
                          for path, content in files.items()],
                "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False}}
    if change:
        change(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False))
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def test_content_manifest_has_no_execution_or_global_skill_contributions():
    snapshot = inspect_plugin_package(content_bundle(files={"CAPABILITY.md": b"entry", "方法/分镜 示例.md": b"method"}))
    manifest = snapshot.manifest
    assert manifest.is_content_only
    assert manifest.entry is None and manifest.entry_module == "" and manifest.wheels == ()
    assert manifest.tools == manifest.actions == manifest.skills == manifest.panels == manifest.host_api == ()
    assert isinstance(manifest.files[1], CapabilityFile)
    assert PluginManifest.from_payload(manifest.to_payload()) == manifest
    assert manifest.to_payload()["package_kind"] == "capability"
    assert manifest.command_spec.actions == ()
    assert read_plugin_member(snapshot, "方法/分镜 示例.md", max_bytes=6) == b"method"


@pytest.mark.parametrize("change", [
    lambda p: p.update(package_kind="plugin"),
    lambda p: p.update(entry=None),
    lambda p: p.update(skills=[]),
    lambda p: p.update(tools=[]),
    lambda p: p.update(files=[]),
    lambda p: p["capability"].update(entry_document="missing.md"),
    lambda p: p["capability"].update(keywords=["same", "same"]),
    lambda p: p["capability"].update(keywords="story"),
    lambda p: p["capability"].update(description="bad\ncontrol"),
    lambda p: p["capability"].update(permissions=["all"]),
    lambda p: p["files"][0].update(executable=True),
    lambda p: p["files"][0].update(path="../escape"),
    lambda p: p["files"][0].update(path="/absolute"),
    lambda p: p["files"][0].update(path="a\\b"),
    lambda p: p["files"][0].update(path="a//b"),
    lambda p: p["files"][0].update(path="plugin.json"),
    lambda p: p["files"][0].update(sha256="bad"),
])
def test_content_manifest_rejects_execution_fields_and_invalid_content(change):
    with pytest.raises(PluginPackageError):
        inspect_plugin_package(content_bundle(change=change))


def test_content_reader_rejects_unlisted_tampered_or_large_members():
    with pytest.raises(PluginPackageError, match="摘要"):
        inspect_plugin_package(content_bundle(change=lambda p: p["files"][0].update(sha256="0" * 64)))
    with pytest.raises(PluginPackageError, match="描述不一致"):
        inspect_plugin_package(content_bundle(change=lambda p: p["files"].pop()))
    snapshot = inspect_plugin_package(content_bundle())
    with pytest.raises(PluginPackageError) as error:
        read_plugin_member(snapshot, "missing", max_bytes=100)
    assert error.value.reason == "member_missing"
    with pytest.raises(PluginPackageError) as error:
        read_plugin_member(snapshot, "CAPABILITY.md", max_bytes=1)
    assert error.value.reason == "package_limit"


@pytest.mark.parametrize("other", ["capability.MD", "me\u0301thod.md"])
def test_content_paths_reject_portable_collisions(other):
    files = {"CAPABILITY.md": b"entry", "méthod.md": b"first", other: b"collision"}
    with pytest.raises(PluginPackageError):
        inspect_plugin_package(content_bundle(files=files))


def test_content_member_budget_supports_large_private_libraries_and_remains_bounded():
    files = {"CAPABILITY.md": b"entry", **{f"methods/{index}.md": b"private" for index in range(529)}}
    assert len(inspect_plugin_package(content_bundle(files=files)).manifest.files) == 530
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_package(content_bundle(files=files), limits=replace(PackageReadLimits(), members=100))
    assert error.value.reason == "package_limit"
    for field in ("archive_bytes", "expanded_bytes", "member_bytes", "manifest_bytes", "directory_bytes"):
        with pytest.raises(PluginPackageError):
            inspect_plugin_package(content_bundle(), limits=replace(PackageReadLimits(), **{field: 1}))


def test_legacy_outer_package_member_limit_is_not_raised_for_content_libraries():
    from agent_py_agent.tests.test_plugin_package import _bundle

    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_package(_bundle(extra=tuple((f"extra-{index}", b"") for index in range(127))))
    assert error.value.reason == "package_limit"


def test_content_build_is_reproducible_private_and_never_executes(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "CAPABILITY.md").write_text("参考 scripts/check.py")
    (root / "scripts").mkdir()
    (root / "scripts/check.py").write_text("raise RuntimeError('never execute')")
    declaration = {"plugin_id": "test-content", "version": "1", "summary": "仅内容", "capability": {
        "description": "只读方法", "keywords": [], "entry_document": "CAPABILITY.md"},
        "files": [{"path": "CAPABILITY.md"}, {"path": "scripts/check.py"}],
        "settings_schema": {"type": "object", "properties": {}}}
    a = build_capability_package(declaration, root, tmp_path / "a.zip")
    b = build_capability_package(declaration, root, tmp_path / "b.zip")
    assert a.read_bytes() == b.read_bytes()
    assert inspect_plugin_package(a.read_bytes()).manifest.is_content_only
    with pytest.raises(FileExistsError):
        build_capability_package(declaration, root, a)
    with pytest.raises(ValueError):
        build_capability_package({**declaration, "schema_version": "plugin_package.v7"}, root, tmp_path / "bad.zip")
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (root / "linked.md").symlink_to(outside)
    with pytest.raises(ValueError):
        build_capability_package({**declaration, "files": [{"path": "linked.md"}]}, root, tmp_path / "bad.zip")


@pytest.mark.parametrize("kind", ["inside_file", "outside_file", "inside_directory", "outside_directory"])
def test_content_builder_rejects_all_member_symlinks(tmp_path, kind):
    root = tmp_path / "source"
    root.mkdir()
    target_root = root / "actual" if kind.startswith("inside") else tmp_path / "outside"
    target_root.mkdir()
    (target_root / "CAPABILITY.md").write_text("contents")
    if kind.endswith("directory"):
        (root / "linked").symlink_to(target_root, target_is_directory=True)
        member = "linked/CAPABILITY.md"
    else:
        (root / "CAPABILITY.md").symlink_to(target_root / "CAPABILITY.md")
        member = "CAPABILITY.md"
    declaration = {"plugin_id": "content", "version": "1", "summary": "内容", "capability": {
        "description": "内容", "keywords": [], "entry_document": member}, "files": [{"path": member}],
        "settings_schema": {"type": "object", "properties": {}}}
    output = tmp_path / "invalid.zip"
    with pytest.raises(ValueError, match="无链接"):
        build_capability_package(declaration, root, output)
    assert not output.exists()


_SAMPLE_PACKAGE_IDS = ("pkg-alpha", "pkg-beta", "pkg-gamma")


def _write_sample_package_dir(root, package_id: str) -> tuple[dict, object]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "methods").mkdir(parents=True, exist_ok=True)
    (root / "licenses").mkdir(parents=True, exist_ok=True)
    files = {
        "CAPABILITY.md": f"# {package_id}\n\n入口说明。\n",
        "PROVENANCE.md": f"来源说明: {package_id}\n",
        "LICENSE": "MIT\n",
        "licenses/NOTICE.txt": f"Notice for {package_id}\n",
        "methods/workflow.md": f"# 工作流 ({package_id})\n",
        "methods/review.md": f"# 复核清单 ({package_id})\n",
    }
    for rel_path, text in files.items():
        (root / rel_path).write_text(text, encoding="utf-8")
    declaration = {
        "plugin_id": package_id,
        "version": "1.0.0",
        "summary": f"{package_id} 能力包",
        "capability": {"description": f"{package_id} 描述", "keywords": ["workflow"], "entry_document": "CAPABILITY.md"},
        "files": [{"path": rel_path} for rel_path in sorted(files)],
        "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    (root / "declaration.json").write_text(json.dumps(declaration, ensure_ascii=False), encoding="utf-8")
    return declaration, root


@pytest.mark.parametrize("package", _SAMPLE_PACKAGE_IDS)
def test_samples_build_reproducibly_and_expose_only_one_package(package, tmp_path):
    declaration, root = _write_sample_package_dir(tmp_path / package, package)
    first = build_capability_package(declaration, root, tmp_path / "first.zip")
    second = build_capability_package(declaration, root, tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    inspected = inspect_plugin_package(first.read_bytes())
    manifest = inspected.manifest
    assert manifest.is_content_only and manifest.plugin_id == package
    assert manifest.skills == () and manifest.tools == () and manifest.actions == ()
    assert manifest.to_payload()["schema_version"] == "plugin_package.v7"
    assert manifest.to_payload()["package_kind"] == "capability"
    assert manifest.capability.entry_document == "CAPABILITY.md"
    members = {item.path: item for item in manifest.files}
    assert {"CAPABILITY.md", "PROVENANCE.md", "LICENSE", "methods/workflow.md", "methods/review.md"} <= set(members)
    assert any(path.startswith("licenses/") for path in members)
    assert "declaration.json" not in members
    assert all(item.executable is False for item in members.values())
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {"plugin.json", *members}
        for name, member in members.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == member.sha256
            assert archive.read(name) == (root / name).read_bytes()


def test_packages_keep_same_relative_method_independent(tmp_path):
    texts, digests = set(), set()
    for package in _SAMPLE_PACKAGE_IDS:
        declaration, root = _write_sample_package_dir(tmp_path / package, package)
        bundle = build_capability_package(declaration, root, tmp_path / f"{package}.zip")
        manifest = inspect_plugin_package(bundle.read_bytes()).manifest
        method = next(item for item in manifest.files if item.path == "methods/review.md")
        digests.add(method.sha256)
        with zipfile.ZipFile(bundle) as archive:
            texts.add(archive.read(method.path))
    assert len(texts) == len(digests) == 3
