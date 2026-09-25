"""非 Python 插件样例与跨语言一致性用例。

- 工作区读取检查用例（plugins/sdk/conformance/workspace_read_check.json）先由宿主 Python 参考实现裁决，
  再交给 Node 移植（plugins/hello-node）逐条比对；
- hello-node / hello-go 样例经真实安装、确认回执、带确认码启用和工具调用走一遍宿主链路。
本机没有 node / go 时对应用例跳过；全部写入都在 tmp_path。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_py_agent.agent.plugin_entry import host_platform_tag
from agent_py_agent.agent.plugin_runtime import plugin_tool_name
from agent_py_agent.agent.workspace_read_context import WorkspaceReadContext
from agent_py_agent.tests.plugin_activation_fixtures import invoke_registered_tool, plugin_registry
from agent_py_agent.tests.test_plugin_management import manager
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_files_package import build_files_package

VECTORS = ROOT / "plugins" / "sdk" / "conformance" / "workspace_read_check.json"
NODE = shutil.which("node")
GO = shutil.which("go")


# 函数用途: 把用例里所有字符串中的 {root} 换成目录树根。
def _substitute(value, root: str):
    if isinstance(value, str):
        return value.replace("{root}", root)
    if isinstance(value, list):
        return [_substitute(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, root) for key, item in value.items()}
    return value


# LLM: 目录树只建在 tmp_path；根取真实路径，与宿主总是下发规范真实路径一致（macOS 的 /var 是链接）。
# 函数用途: 按用例建文件与符号链接，返回 (根, 已替换 {root} 的用例)。
def _materialize(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    root = os.path.realpath(root)
    raw = json.loads(VECTORS.read_text(encoding="utf-8"))
    for relative in raw["tree"]["files"]:
        target = Path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    for relative, link in raw["tree"]["symlinks"].items():
        target = Path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link.replace("{root}", root), target)
    return root, _substitute(raw, root)


# 函数用途: 由有效用例得到期望裁决列表。
def _expected(vectors):
    return [{"allowed": item["allowed"], "code": item["code"]} for item in vectors["valid_cases"]]


# 函数用途: 按 base/set/drop 生成一份无效上下文。
def _invalid_payload(vectors, item):
    payload = copy.deepcopy(vectors["contexts"][item["base"]])
    payload.update(item.get("set", {}))
    payload.pop(item.get("drop", ""), None)
    return payload


def test_python_reference_decides_every_conformance_case(tmp_path):
    _, vectors = _materialize(tmp_path)
    contexts = {name: WorkspaceReadContext.from_payload(payload) for name, payload in vectors["contexts"].items()}
    actual = []
    for item in vectors["valid_cases"]:
        decision = contexts[item["context"]].check(Path(item["path"]))
        actual.append({"allowed": decision.allowed, "code": decision.code})
    mismatches = [(case, want, got) for case, want, got in zip(vectors["valid_cases"], _expected(vectors), actual) if want != got]
    assert not mismatches
    for item in vectors["invalid_contexts"]:
        with pytest.raises(ValueError):
            WorkspaceReadContext.from_payload(_invalid_payload(vectors, item))


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_node_port_matches_every_conformance_case(tmp_path):
    root, vectors = _materialize(tmp_path)
    runner = ROOT / "plugins" / "hello-node" / "conformance.js"
    output = subprocess.run([NODE, str(runner), str(VECTORS), root], capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(output.stdout)
    mismatches = [(case, want, got) for case, want, got in zip(vectors["valid_cases"], _expected(vectors), result["valid"])
                  if want != got]
    assert len(result["valid"]) == len(vectors["valid_cases"]) and not mismatches
    assert result["invalid"] == [True] * len(vectors["invalid_contexts"])


# 函数用途: 安装一个样例包并完成"看回执 → 带确认码启用"，返回管理服务与第一次回执。
def _install_and_confirm(tmp_path, package):
    service, _ = manager(tmp_path)
    result = service.command(f'/plugins install "{package}"', revision=service.catalog().revision, request_id="install")
    assert result["state"] == "succeeded", result
    plugin_id = service.installations.snapshot()[0].manifest.plugin_id
    first = service.command(f"/plugins enable {plugin_id}", revision=service.catalog().revision, request_id="enable")
    assert first["details"]["reason"] == "confirmation_required", first
    code = first["details"]["confirmation"]["confirm_code"]
    enabled = service.command(f"/plugins enable {plugin_id} --confirm {code}", revision=service.catalog().revision,
                              request_id="confirmed")
    assert enabled["state"] == "succeeded", enabled
    return service, plugin_id, first["details"]["confirmation"]


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_node_sample_runs_through_host_and_honors_read_context(tmp_path):
    project = ROOT / "plugins" / "hello-node"
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    package = build_files_package(declaration, project, tmp_path / "hello-node.zip")
    service, plugin_id, confirmation = _install_and_confirm(tmp_path, package)
    assert confirmation["interpreter"]["path"] == os.path.realpath(NODE)
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        greeting = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, "hello"), {"name": "测试"},
                                          request_id="hello")
        assert greeting["state"] == "succeeded" and "来自 Node.js" in json.dumps(greeting, ensure_ascii=False), greeting
        (tmp_path / "notes.txt").write_text("工作区里的笔记", encoding="utf-8")
        read = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, "read_text"), {"path": "notes.txt"},
                                      request_id="read")
        assert read["state"] == "succeeded" and "工作区里的笔记" in json.dumps(read, ensure_ascii=False), read
        outside = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, "read_text"),
                                         {"path": str(ROOT / "README.md")}, request_id="outside")
        assert "PATH_READ_SCOPE_BLOCKED" in json.dumps(outside, ensure_ascii=False), outside
    finally:
        registry.close_mcp_clients()


@pytest.mark.skipif(GO is None, reason="本机没有 go")
def test_go_sample_builds_and_runs_through_host(tmp_path):
    project = ROOT / "plugins" / "hello-go"
    staging = tmp_path / "build"
    environment = {**os.environ, "CGO_ENABLED": "0", "GOFLAGS": "-mod=mod"}
    subprocess.run([GO, "build", "-trimpath", "-o", str(staging / "bin" / "hello-go"), "."], cwd=project, env=environment,
                   capture_output=True, text=True, timeout=300, check=True)
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    package = build_files_package(declaration, staging, tmp_path / "hello-go.zip", (host_platform_tag(),))
    service, plugin_id, confirmation = _install_and_confirm(tmp_path, package)
    assert confirmation["platform"] == host_platform_tag() and confirmation["files"][0]["executable"] is True
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        greeting = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, "hello"), {"name": "测试"},
                                          request_id="hello")
        assert greeting["state"] == "succeeded" and "来自 Go 程序" in json.dumps(greeting, ensure_ascii=False), greeting
    finally:
        registry.close_mcp_clients()
