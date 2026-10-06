"""learnpack 第 2 步：产品里唯一的打包实现（capability/package_build）。

锁定：仓库样例能力包/样例插件经产品函数打出的包，sha256 与搬家前的旧脚本产物逐字节一致（固定摘要）；两个脚本只是薄壳；
同一输入两次打包字节相同；声明外形、链接、预算、文件清单不一致、产物复验失败都给登记过的错误码。
"""
from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.package_build import (
    KIND_CAPABILITY_PACK,
    KIND_PLUGIN,
    PackageBuildError,
    build_capability_pack,
    build_files_plugin,
    capability_pack_paths,
    files_plugin_paths,
    read_declared_files,
)
from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from scripts.build_capability_package import build_capability_package
from scripts.build_plugin_files_package import build_files_package

ROOT = Path(__file__).resolve().parents[2]
# 2026-10-06 用搬家前的旧脚本（基线 e5d7f3c90）对同一批样例打出的包摘要；改打包规则会改这些值，需要先说明原因。
_GOLDEN = {
    "pack:drama-text-a": "51ca17734b865464ad18ecf1de5c254f97cb3d16afc66f1595a776f081619a36",
    "pack:drama-workflow-b": "0cfa7cc079186c4b9c59ce37e4c9b2e8785146a5275a18b243ccd6fea63a3530",
    "plugin:event-watch": "46aa75798cfc61452f531c0d20f479606e37cd40d96bb0d85d85227b11354a27",
    "plugin:hello-node": "16b6dbe5c4c58c17afbecf67be016c8a4b8ce150a4bbf5176b8a20d118a55e7a",
    "plugin:rm-guard": "d7d1cba0c5ed3dc41f4ff8bbb64b17f97cc3b032d00c203b24ecb08d6c115adf",
}


def _declaration(root: Path) -> dict:
    return json.loads((root / "declaration.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["drama-text-a", "drama-workflow-b"])
def test_product_pack_build_matches_pre_move_script_bytes(name, tmp_path):
    root = ROOT / "examples" / "capability-packages" / name
    declaration = _declaration(root)
    built = build_capability_pack(declaration, read_declared_files(root, capability_pack_paths(declaration)))
    assert built.kind == KIND_CAPABILITY_PACK and built.sha256 == _GOLDEN[f"pack:{name}"]
    script = build_capability_package(declaration, root, tmp_path / "script.zip")
    assert script.read_bytes() == built.payload
    assert inspect_plugin_package(built.payload).manifest.plugin_id == built.package_id == name


@pytest.mark.parametrize("name", ["event-watch", "hello-node", "rm-guard"])
def test_product_plugin_build_matches_pre_move_script_bytes(name, tmp_path):
    root = ROOT / "plugins" / name
    declaration = _declaration(root)
    contents = read_declared_files(root, files_plugin_paths(declaration))
    built = build_files_plugin(declaration, contents)
    assert built.kind == KIND_PLUGIN and built.sha256 == _GOLDEN[f"plugin:{name}"]
    assert build_files_package(declaration, root, tmp_path / "script.zip").read_bytes() == built.payload


def _pack_source(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "source"
    (root / "methods").mkdir(parents=True)
    (root / "CAPABILITY.md").write_text("# 短剧分场方法\n先列人物，再分场。\n", encoding="utf-8")
    (root / "methods" / "review.md").write_text("逐场核对冲突与钩子。\n", encoding="utf-8")
    declaration = {"plugin_id": "drama-scenes", "version": "0.1.0", "summary": "短剧分场方法",
                   "capability": {"description": "把短剧故事拆成场次", "keywords": ["短剧", "分场"],
                                  "entry_document": "CAPABILITY.md"},
                   "files": [{"path": "CAPABILITY.md"}, {"path": "methods/review.md"}],
                   "settings_schema": {"type": "object", "properties": {}}}
    return root, declaration


def test_pack_build_is_reproducible_and_content_only(tmp_path):
    root, declaration = _pack_source(tmp_path)
    first = build_capability_pack(declaration, read_declared_files(root, capability_pack_paths(declaration)))
    second = build_capability_pack(declaration, read_declared_files(root, capability_pack_paths(declaration)))
    assert first.payload == second.payload and first.files == ("CAPABILITY.md", "methods/review.md")
    assert inspect_plugin_package(first.payload).manifest.is_content_only


@pytest.mark.parametrize("change,code", [
    (lambda d: d.pop("settings_schema"), "PACKAGE_BUILD_DECLARATION_INVALID"),
    (lambda d: d.update(schema_version="plugin_package.v7"), "PACKAGE_BUILD_DECLARATION_INVALID"),
    (lambda d: d.update(files=[{"path": "CAPABILITY.md", "sha256": "0" * 64}]), "PACKAGE_BUILD_DECLARATION_INVALID"),
    (lambda d: d.update(files=[]), "PACKAGE_BUILD_DECLARATION_INVALID"),
])
def test_pack_declaration_shape_errors_are_coded(tmp_path, change, code):
    _root, declaration = _pack_source(tmp_path)
    change(declaration)
    with pytest.raises(PackageBuildError) as error:
        capability_pack_paths(declaration)
    assert error.value.code == code and code in ERROR_CONTRACTS


def test_reader_rejects_links_missing_files_duplicates_and_bad_paths(tmp_path):
    root, _declaration_unused = _pack_source(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (root / "linked.md").symlink_to(outside)
    for paths in (["linked.md"], ["missing.md"], ["CAPABILITY.md", "CAPABILITY.md"]):
        with pytest.raises(PackageBuildError) as error:
            read_declared_files(root, paths)
        assert error.value.code == "PACKAGE_BUILD_FILE_INVALID"
    with pytest.raises(ValueError):
        read_declared_files(root, ["../outside.md"])


def test_contents_must_match_the_declared_files(tmp_path):
    root, declaration = _pack_source(tmp_path)
    contents = read_declared_files(root, ["CAPABILITY.md"])
    with pytest.raises(PackageBuildError) as error:
        build_capability_pack(declaration, contents)
    assert error.value.code == "PACKAGE_BUILD_DECLARATION_INVALID"


def test_plugin_declaration_shape_errors_are_coded():
    for declaration in ({"schema_version": "x", "files": []}, {"files": [{"path": "a"}]}, "not-a-dict"):
        with pytest.raises(PackageBuildError) as error:
            files_plugin_paths(declaration)
        assert error.value.code == "PACKAGE_BUILD_DECLARATION_INVALID"
    with pytest.raises(PackageBuildError):
        files_plugin_paths({"platforms": ["x"], "files": []}, ("darwin-arm64",))


def test_plugin_executable_flag_sets_the_zip_member_mode(tmp_path):
    root = tmp_path / "hello-node"
    shutil.copytree(ROOT / "plugins" / "hello-node", root)
    (root / "bin").mkdir()
    (root / "bin" / "helper.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    declaration = _declaration(root)
    declaration["files"].append({"path": "bin/helper.sh", "executable": True})
    built = build_files_plugin(declaration, read_declared_files(root, files_plugin_paths(declaration)))
    with zipfile.ZipFile(io.BytesIO(built.payload)) as archive:
        modes = {info.filename: (info.external_attr >> 16) & 0o777 for info in archive.infolist()}
    assert modes["bin/helper.sh"] == 0o755 and modes["src/server.js"] == 0o644 and modes["plugin.json"] == 0o644


def test_member_paths_must_be_portable_even_when_the_file_exists(tmp_path):
    root, _declaration_unused = _pack_source(tmp_path)
    (root / "a:b.md").write_text("冒号文件名在别的系统上不可用", encoding="utf-8")
    with pytest.raises(ValueError, match="路径无效"):
        read_declared_files(root, ["a:b.md"])
