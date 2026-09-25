"""v6 非 Python 插件合同：包描述、读包、用户确认、解包准备、解释器固定与真实 MCP 启停。

插件进程用 python3 脚本扮演"任意语言"程序（随包可执行文件 / 系统解释器加随包脚本），不依赖 Node 或 Go；
全部写入都在 tmp_path 的临时 owner 里，不启动产品 TUI 或模型。
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from dataclasses import asdict, replace

import pytest

from agent_py_agent.agent import plugin_runtime_facts
from agent_py_agent.agent.command_arguments import ArgumentSpec, CommandActionSpec
from agent_py_agent.agent.plugin_entry import PluginEntry, PluginFile, host_platform_tag
from agent_py_agent.agent.plugin_environment import prepare_plugin_environment
from agent_py_agent.agent.plugin_environment_process import EnvironmentPreparationError
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_manifest import PluginManifest, PluginPackageError, PluginWheel
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_runtime import PluginMCPClient, plugin_tool_name
from agent_py_agent.agent.plugin_runtime_facts import (
    RUNTIME_PIN_FILE,
    PluginRuntimeError,
    confirmation_message,
    resolve_plugin_runtime,
)
from agent_py_agent.agent.plugin_skills import enabled_plugin_skill_roots
from agent_py_agent.tests.plugin_activation_fixtures import invoke_registered_tool, plugin_registry
from agent_py_agent.tests.plugin_environment_fixtures import (
    environment_operation,
    prepare_environment,
)
from agent_py_agent.tests.test_plugin_management import manager
from scripts.build_plugin_files_package import build_files_package

PLUGIN_ID = "sample-any"
_TOOL = {"name": "read", "description": "读取文本", "requested_effect": "read_only",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
_SERVER = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
data = os.environ.get("MY_AGENT_PLUGIN_DATA_DIR")
if data:
    Path(data, "started").write_text("1")
tools = __TOOLS__
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "any-language-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        text = Path(request["params"]["arguments"]["path"]).read_text()
        facts = f"entry={os.path.basename(sys.argv[0])};args={' '.join(sys.argv[1:])};cwd={os.path.basename(os.getcwd())}"
        result = {"content": [{"type": "text", "text": text + "|" + facts}]}
    else:
        raise RuntimeError("unsupported fixture method")
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''.replace("__TOOLS__", repr([{"name": _TOOL["name"], "description": _TOOL["description"],
                                 "inputSchema": _TOOL["input_schema"]}])).encode()


# 函数用途: 生成不含摘要与协议版本的 v6 声明；executable 用随包脚本本身，interpreter 用 PATH 上的解释器名。
#   extra 里的 files 追加到入口文件之后，其余键（skills、platforms 等）直接覆盖。
def _declaration(kind: str = "executable", interpreter: str = "", **extra) -> dict:
    command = "bin/server.py" if kind == "executable" else "server.py"
    action = CommandActionSpec("read", "读取文件", (ArgumentSpec("path", "路径", required=True, path=True),),
                               kind="tool", target="read")
    files = [{"path": command, "executable": kind == "executable"}, *extra.pop("files", [])]
    return {
        "plugin_id": PLUGIN_ID, "version": "1.0", "summary": "任意语言插件示例",
        "entry": {"kind": kind, "command": command, "args": ["--stdio"], **({"interpreter": interpreter} if interpreter else {})},
        "files": files, "platforms": ["any"], "actions": [json.loads(json.dumps(asdict(action)))], "default_action": "read",
        "tools": [_TOOL], "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False}, "skills": [],
        **extra,
    }


# 函数用途: 把声明和文件内容写进临时源目录，经打包脚本生成 v6 安装包。
def _package(tmp_path, declaration: dict, contents: dict[str, bytes], name: str = "any.zip"):
    root = tmp_path / ("files-" + name)
    for path, content in contents.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return build_files_package(declaration, root, tmp_path / name)


# 函数用途: 在临时 owner 里安装一个 v6 包，返回管理服务。
def _installed(tmp_path, declaration: dict, contents: dict[str, bytes]):
    service, _ = manager(tmp_path)
    source = _package(tmp_path, declaration, contents)
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")
    assert result["state"] == "succeeded", result
    return service


# 函数用途: 在临时 PATH 目录放一个转交给当前 Python 的 shell 解释器，内容可被测试改写以模拟解释器被替换。
def _fake_interpreter(tmp_path, monkeypatch, name: str = "fixture-python"):
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    interpreter = directory / name
    interpreter.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    interpreter.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    return interpreter


# 函数用途: 发一次启用命令并返回结果。
def _enable(service, request_id: str, code: str = ""):
    suffix = f" --confirm {code}" if code else ""
    return service.command(f"/plugins enable {PLUGIN_ID}{suffix}", revision=service.catalog().revision, request_id=request_id)


# 函数用途: 把成员表重新打成 ZIP 字节（测试篡改用，不经打包脚本）。
def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


# 函数用途: 按内容补齐摘要与协议版本，得到可直接交给 PluginManifest.from_payload 的 v6 描述。
def _payload(declaration: dict, contents: dict[str, bytes]) -> dict:
    files = [{**item, "sha256": hashlib.sha256(contents[item["path"]]).hexdigest()} for item in declaration["files"]]
    return {"panels": [], "host_api": [], **declaration, "schema_version": "plugin_package.v6", "files": files}


def test_v6_manifest_round_trip_is_canonical_and_keeps_python_fields_empty():
    payload = _payload(_declaration("interpreter", interpreter="node"), {"server.py": _SERVER})
    manifest = PluginManifest.from_payload(payload)
    assert manifest.entry == PluginEntry("interpreter", "server.py", ("--stdio",), "node")
    assert manifest.files == (PluginFile("server.py", hashlib.sha256(_SERVER).hexdigest(), False),)
    assert (manifest.entry_module, manifest.entry_wheel, manifest.wheels) == ("", "", ())
    assert PluginManifest.from_payload(manifest.to_payload()) == manifest
    assert manifest.to_payload()["schema_version"] == "plugin_package.v6"
    assert "entry_module" not in manifest.to_payload()


@pytest.mark.parametrize("change", [
    lambda p: p["entry"].update(kind="shell"),
    lambda p: p["entry"].update(kind="interpreter", interpreter="/usr/bin/node"),
    lambda p: p["entry"].update(interpreter="node"),  # executable 类型不能带解释器
    lambda p: p["entry"].update(command="missing.py"),
    lambda p: p["entry"].update(args=["ok", "bad\nline"]),
    lambda p: p["entry"].update(args=[str(index) for index in range(17)]),
    lambda p: p["entry"].update(extra=True),
    lambda p: p["files"][0].update(executable=False),  # 可执行入口必须有执行位
    lambda p: p["files"][0].update(executable="true"),
    lambda p: p["files"].append({"path": "../escape", "sha256": "0" * 64, "executable": False}),
    lambda p: p["files"].append({"path": "/abs", "sha256": "0" * 64, "executable": False}),
    lambda p: p["files"].append({"path": "a//b", "sha256": "0" * 64, "executable": False}),
    lambda p: p["files"].append({"path": "plugin.json", "sha256": "0" * 64, "executable": False}),
    lambda p: p["files"].append({"path": "BIN/server.py", "sha256": "0" * 64, "executable": False}),
    lambda p: p["files"].append({"path": "skills/extra/SKILL.md", "sha256": "0" * 64, "executable": False}),
    lambda p: p["files"].append({"path": "Skills/extra/skill.md", "sha256": "0" * 64, "executable": False}),
    lambda p: p.update(skills=["declared"]),
    lambda p: p.update(platforms=[]),
    lambda p: p.update(platforms=["Linux-x86_64"]),
    lambda p: p.update(entry_module="peek"),
])
def test_v6_manifest_rejects_unsafe_or_inconsistent_declarations(change):
    payload = _payload(_declaration(), {"bin/server.py": _SERVER})
    change(payload)
    with pytest.raises(PluginPackageError):
        PluginManifest.from_payload(payload)


def test_v6_skill_files_must_match_declared_skills_exactly():
    contents = {"bin/server.py": _SERVER, "skills/hello/SKILL.md": b"---\nname: hello\n---\n",
                "skills/hello/references/a.md": b"ref"}
    declaration = _declaration(files=[{"path": "skills/hello/SKILL.md", "executable": False},
                                      {"path": "skills/hello/references/a.md", "executable": False}], skills=["hello"])
    assert PluginManifest.from_payload(_payload(declaration, contents)).skills == ("hello",)


def test_python_manifest_cannot_carry_v6_fields():
    payload = _payload(_declaration(), {"bin/server.py": _SERVER})
    manifest = PluginManifest.from_payload(payload)
    python = {"entry": None, "entry_module": "peek", "entry_wheel": "wheels/a.whl",
              "wheels": (PluginWheel("wheels/a.whl", "0" * 64),)}
    with pytest.raises(ValueError, match="随包文件或平台"):
        replace(manifest, **python)
    assert replace(manifest, **python, files=(), platforms=()).entry is None


def test_build_is_reproducible_and_reader_rejects_extra_or_tampered_members(tmp_path):
    declaration = _declaration()
    first = _package(tmp_path, declaration, {"bin/server.py": _SERVER}, "one.zip")
    second = _package(tmp_path, declaration, {"bin/server.py": _SERVER}, "two.zip")
    assert first.read_bytes() == second.read_bytes()
    package = inspect_plugin_package(first.read_bytes())
    assert package.manifest.entry.kind == "executable"
    with zipfile.ZipFile(first) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
        assert stat.S_IMODE(archive.getinfo("bin/server.py").external_attr >> 16) == 0o755
    for change, reason in (({"extra.txt": b"x"}, "invalid_archive"), ({"bin/server.py": b"tampered"}, "digest_mismatch")):
        with pytest.raises(PluginPackageError) as error:
            inspect_plugin_package(_zip({**members, **change}))
        assert error.value.reason == reason


def test_build_rejects_generated_fields_and_files_outside_root(tmp_path):
    declaration = _declaration()
    with pytest.raises(ValueError, match="协议版本"):
        build_files_package({**declaration, "schema_version": "plugin_package.v6"}, tmp_path, tmp_path / "a.zip")
    with pytest.raises(ValueError, match="摘要由脚本生成"):
        build_files_package({**declaration, "files": [{"path": "bin/server.py", "executable": True, "sha256": "0" * 64}]},
                            tmp_path, tmp_path / "b.zip")
    (tmp_path / "outside.py").write_bytes(_SERVER)
    root = tmp_path / "root"
    root.mkdir()
    escaping = copy.deepcopy(declaration)
    escaping["files"][0]["path"] = "../outside.py"
    with pytest.raises(ValueError, match="越出"):
        build_files_package(escaping, root, tmp_path / "c.zip")
    assert not any((tmp_path / name).exists() for name in ("a.zip", "b.zip", "c.zip"))


def test_runtime_facts_pin_interpreter_realpath_and_content(tmp_path, monkeypatch):
    interpreter = _fake_interpreter(tmp_path, monkeypatch)
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / "fixture-python").symlink_to(interpreter)
    manifest = PluginManifest.from_payload(_payload(_declaration("interpreter", interpreter="fixture-python"),
                                                    {"server.py": _SERVER}))
    facts = resolve_plugin_runtime(manifest, search_path=str(alias))
    assert facts.interpreter_path == os.path.realpath(interpreter)
    assert facts.interpreter_sha256 == hashlib.sha256(interpreter.read_bytes()).hexdigest()
    assert facts.platform == host_platform_tag()
    interpreter.write_text(interpreter.read_text() + "# changed\n")
    assert resolve_plugin_runtime(manifest, search_path=str(alias)).fingerprint != facts.fingerprint
    with pytest.raises(PluginRuntimeError) as missing:
        resolve_plugin_runtime(manifest, search_path=str(tmp_path / "empty"))
    assert missing.value.reason == "interpreter_not_found"
    interpreter.chmod(0o644)
    with pytest.raises(PluginRuntimeError) as not_executable:
        resolve_plugin_runtime(manifest, search_path=str(alias))
    assert not_executable.value.reason == "interpreter_not_found"
    foreign = PluginManifest.from_payload(_payload(_declaration(platforms=["plan9-mips"]), {"bin/server.py": _SERVER}))
    with pytest.raises(PluginRuntimeError) as unsupported:
        resolve_plugin_runtime(foreign)
    assert unsupported.value.reason == "platform_unsupported"


def test_executable_plugin_needs_user_confirmation_then_runs_and_releases(tmp_path):
    service = _installed(tmp_path, _declaration(), {"bin/server.py": _SERVER})
    owner = service.context.owner
    first = _enable(service, "enable")
    assert first["state"] == "failed", first
    confirmation = first["details"]["confirmation"]
    assert first["details"]["reason"] == "confirmation_required"
    code = confirmation["confirm_code"]
    assert re.fullmatch(r"[0-9a-f]{12}", code)
    assert confirmation["platform"] == host_platform_tag() and "interpreter" not in confirmation
    assert confirmation["files"] == [{"path": "bin/server.py", "sha256": hashlib.sha256(_SERVER).hexdigest(),
                                      "size": len(_SERVER), "executable": True, "shebang": "#!/usr/bin/env python3"}]
    assert f"/plugins enable {PLUGIN_ID} --confirm {code}" in first["message"]
    # 未确认前不准备环境、不启动任何随包程序
    assert service.installations.snapshot()[0].activation is None
    assert not (owner.plugins_dir / "environments").exists() or not any((owner.plugins_dir / "environments").iterdir())
    assert not (owner.plugins_dir / "data" / PLUGIN_ID / "started").exists()
    wrong = _enable(service, "wrong", "0" * 12)
    assert wrong["state"] == "failed" and wrong["details"]["reason"] == "confirmation_required", wrong
    enabled = _enable(service, "confirmed", code)
    assert enabled["state"] == "succeeded", enabled
    entry = service.installations.snapshot()[0]
    environment = owner.plugins_dir / "environments" / entry.activation.plan.environment_ref
    assert stat.S_IMODE((environment / "files" / "bin" / "server.py").stat().st_mode) == 0o500
    assert not (environment / RUNTIME_PIN_FILE).exists()
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        name = plugin_tool_name(PLUGIN_ID, "read")
        source = tmp_path / "input.txt"
        source.write_text("任意语言插件读取")
        result = invoke_registered_tool(service, registry, name, {"path": str(source)})
        assert result["state"] == "succeeded", result
        assert "任意语言插件读取|entry=server.py;args=--stdio;cwd=files" in json.dumps(result, ensure_ascii=False), result
        disabled = service.command(f"/plugins disable {PLUGIN_ID}", revision=service.catalog().revision, request_id="disable")
        assert disabled["state"] == "succeeded" and disabled["details"]["released"], disabled
        assert not environment.exists()
        removed = service.command(f"/plugins remove {PLUGIN_ID}", revision=service.catalog().revision, request_id="remove")
        assert removed["state"] == "succeeded", removed
    finally:
        registry.close_mcp_clients()


# 函数用途: 用临时 PATH 上的解释器安装并带码启用 interpreter 类型插件，返回 (服务, 解释器文件, 第一次回执)。
def _enabled_interpreter_plugin(tmp_path, monkeypatch):
    interpreter = _fake_interpreter(tmp_path, monkeypatch)
    service = _installed(tmp_path, _declaration("interpreter", interpreter="fixture-python"), {"server.py": _SERVER})
    first = _enable(service, "enable")
    enabled = _enable(service, "confirmed", first["details"]["confirmation"]["confirm_code"])
    assert enabled["state"] == "succeeded", enabled
    return service, interpreter, first


def test_interpreter_plugin_pins_interpreter_and_runs_script(tmp_path, monkeypatch):
    service, interpreter, first = _enabled_interpreter_plugin(tmp_path, monkeypatch)
    confirmation = first["details"]["confirmation"]
    assert confirmation["interpreter"] == {"name": "fixture-python", "path": os.path.realpath(interpreter),
                                           "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest()}
    assert confirmation["files"][0]["executable"] is False
    assert "用系统解释器 fixture-python" in first["message"]
    entry = service.installations.snapshot()[0]
    environment = service.context.owner.plugins_dir / "environments" / entry.activation.plan.environment_ref
    assert stat.S_IMODE((environment / "files" / "server.py").stat().st_mode) == 0o400
    assert stat.S_IMODE((environment / RUNTIME_PIN_FILE).stat().st_mode) == 0o600
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        source = tmp_path / "input.txt"
        source.write_text("解释器插件读取")
        result = invoke_registered_tool(service, registry, plugin_tool_name(PLUGIN_ID, "read"), {"path": str(source)})
        assert result["state"] == "succeeded", result
        assert "解释器插件读取|entry=server.py;args=--stdio;cwd=files" in json.dumps(result, ensure_ascii=False), result
    finally:
        registry.close_mcp_clients()


def test_changed_interpreter_refuses_launch_until_confirmed_again(tmp_path, monkeypatch):
    service, interpreter, first = _enabled_interpreter_plugin(tmp_path, monkeypatch)
    old_code = first["details"]["confirmation"]["confirm_code"]
    entry = service.installations.snapshot()[0]
    interpreter.write_text(interpreter.read_text() + "# replaced\n")
    with pytest.raises(PluginRuntimeError) as changed:
        PluginMCPClient(service.context.owner, entry)
    assert changed.value.reason == "interpreter_changed"
    source = tmp_path / "input.txt"
    source.write_text("x")
    explicit = service.command(f'/plugins@{PLUGIN_ID} read "{source}"', revision=service.catalog().revision,
                               request_id="explicit-after-change")
    assert explicit["state"] == "rejected" and explicit["error_code"] == "PLUGIN_RUNTIME_UNAVAILABLE", explicit
    assert explicit["details"] == {"reason": "interpreter_changed"} and "重新确认" in explicit["message"]
    assert service.command("/plugins status explicit-after-change", revision="", request_id="query")["state"] == "not_found"
    fresh = plugin_registry(service)
    try:
        fresh.prepare_for_run()
        assert plugin_tool_name(PLUGIN_ID, "read") not in fresh.tools
    finally:
        fresh.close_mcp_clients()
    disabled = service.command(f"/plugins disable {PLUGIN_ID}", revision=service.catalog().revision, request_id="disable")
    assert disabled["state"] == "succeeded", disabled
    again = _enable(service, "again")
    assert again["details"]["reason"] == "confirmation_required"
    assert again["details"]["confirmation"]["confirm_code"] != old_code
    stale = _enable(service, "stale", old_code)
    assert stale["details"]["reason"] == "confirmation_required", stale


def test_launch_rehashes_only_when_interpreter_stat_changes(tmp_path, monkeypatch):
    interpreter = _fake_interpreter(tmp_path, monkeypatch)
    service = _installed(tmp_path, _declaration("interpreter", interpreter="fixture-python"), {"server.py": _SERVER})
    code = _enable(service, "enable")["details"]["confirmation"]["confirm_code"]
    assert _enable(service, "confirmed", code)["state"] == "succeeded"
    owner, entry = service.context.owner, service.installations.snapshot()[0]
    environment = owner.plugins_dir / "environments" / entry.activation.plan.environment_ref
    fingerprint = entry.activation.plan.interpreter_fingerprint
    original = plugin_runtime_facts._file_sha256
    calls = []
    monkeypatch.setattr(plugin_runtime_facts, "_file_sha256", lambda *args: calls.append(args) or original(*args))
    assert plugin_runtime_facts.verified_runtime_command(owner.root, environment, "interpreter", fingerprint) == \
        os.path.realpath(interpreter)
    assert calls == []
    info = interpreter.stat()
    os.utime(interpreter, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    assert plugin_runtime_facts.verified_runtime_command(owner.root, environment, "interpreter", fingerprint)
    assert len(calls) == 1
    with pytest.raises(PluginRuntimeError) as mismatch:
        plugin_runtime_facts.verified_runtime_command(owner.root, environment, "interpreter", "f" * 64)
    assert mismatch.value.reason == "interpreter_pin_invalid"
    pin = environment / RUNTIME_PIN_FILE
    payload = json.loads(pin.read_text())
    pin.chmod(0o600)
    pin.write_text(json.dumps({**payload, "interpreter_path": "/bin/sh"}))
    with pytest.raises(PluginRuntimeError) as forged:
        plugin_runtime_facts.verified_runtime_command(owner.root, environment, "interpreter", fingerprint)
    assert forged.value.reason == "interpreter_pin_invalid"
    interpreter.unlink()
    pin.write_text(json.dumps(payload))
    with pytest.raises(PluginRuntimeError) as vanished:
        plugin_runtime_facts.verified_runtime_command(owner.root, environment, "interpreter", fingerprint)
    assert vanished.value.reason == "interpreter_changed"


def test_executable_environment_refuses_other_platform(tmp_path, monkeypatch):
    service = _installed(tmp_path, _declaration(), {"bin/server.py": _SERVER})
    code = _enable(service, "enable")["details"]["confirmation"]["confirm_code"]
    assert _enable(service, "confirmed", code)["state"] == "succeeded"
    monkeypatch.setattr(plugin_runtime_facts, "host_platform_tag", lambda: "plan9-mips")
    with pytest.raises(PluginRuntimeError) as changed:
        PluginMCPClient(service.context.owner, service.installations.snapshot()[0])
    assert changed.value.reason == "platform_changed"


def test_unsupported_platform_or_missing_interpreter_fails_before_confirmation(tmp_path, monkeypatch):
    service = _installed(tmp_path, _declaration(platforms=["plan9-mips"]), {"bin/server.py": _SERVER})
    result = _enable(service, "enable")
    assert result["state"] == "failed" and result["details"]["reason"] == "platform_unsupported", result
    assert "没有为本机平台构建" in result["message"]
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    service = _installed(other, _declaration("interpreter", interpreter="fixture-python"), {"server.py": _SERVER})
    result = _enable(service, "enable")
    assert result["state"] == "failed" and result["details"]["reason"] == "interpreter_not_found", result
    assert "PATH 里找到插件需要的解释器" in result["message"]
    assert service.installations.snapshot()[0].activation is None


def test_preparation_refuses_interpreter_changed_after_plan(tmp_path, monkeypatch):
    interpreter = _fake_interpreter(tmp_path, monkeypatch)
    service = _installed(tmp_path, _declaration("interpreter", interpreter="fixture-python"), {"server.py": _SERVER})
    owner = service.context.owner
    package = inspect_plugin_package(PluginInstallStore(owner).package_bytes(service.installations.snapshot()[0]))
    operation = environment_operation(owner, package, "planned")
    interpreter.write_text(interpreter.read_text() + "# swapped\n")
    with pytest.raises(EnvironmentPreparationError) as error:
        prepare_plugin_environment(owner, package, operation)
    assert error.value.reason == "environment_interpreter_changed"
    assert not (owner.plugins_dir / "environments").exists() or not any((owner.plugins_dir / "environments").iterdir())


def test_prepared_files_match_declaration_and_skills_are_exposed(tmp_path):
    contents = {"bin/server.py": _SERVER, "skills/hello/SKILL.md": b"---\nname: hello\ndescription: d\n---\nbody\n",
                "data/table.txt": b"rows"}
    declaration = _declaration(files=[{"path": "skills/hello/SKILL.md", "executable": False},
                                      {"path": "data/table.txt", "executable": False}], skills=["hello"])
    service = _installed(tmp_path, declaration, contents)
    owner = service.context.owner
    package = inspect_plugin_package(PluginInstallStore(owner).package_bytes(service.installations.snapshot()[0]))
    prepared = prepare_environment(owner, package, "prep")
    environment = owner.plugins_dir / "environments" / prepared.environment_ref
    for path, content in contents.items():
        target = environment / "files" / path
        assert target.read_bytes() == content
        assert stat.S_IMODE(target.stat().st_mode) == (0o500 if path == "bin/server.py" else 0o400)
    code = _enable(service, "enable")["details"]["confirmation"]["confirm_code"]
    assert _enable(service, "confirmed", code)["state"] == "succeeded"
    entry = service.installations.snapshot()[0]
    roots = enabled_plugin_skill_roots(owner)
    expected = owner.plugins_dir / "environments" / entry.activation.plan.environment_ref / "files" / "skills"
    assert roots == ((expected, "plugin:" + PLUGIN_ID),)
    assert (expected / "hello" / "SKILL.md").is_file()


def test_confirmation_message_lists_facts_and_command():
    message = confirmation_message({
        "plugin_id": "demo", "version": "2.0", "package_sha256": "a" * 64,
        "entry": {"kind": "interpreter", "command": "main.js", "args": ["--stdio"], "interpreter": "node"},
        "platform": "linux-x86_64", "declared_platforms": ["any"],
        "files": [{"path": f"lib/{index}.js", "sha256": "b" * 64, "size": index, "executable": False} for index in range(22)],
        "interpreter": {"name": "node", "path": "/usr/bin/node", "sha256": "c" * 64}, "confirm_code": "0123456789ab",
    })
    assert "用系统解释器 node（/usr/bin/node" in message and "固定参数：--stdio" in message
    assert "另有 2 个文件" in message and "lib/19.js" in message and "lib/20.js" not in message
    assert message.endswith("/plugins enable demo --confirm 0123456789ab")
