"""M-B1：真实 Node MCP、固定上游自检、读取一致性与可复现包；不启动 Gateway。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile

import pytest

from agent_py_agent.agent.plugin_runtime import plugin_tool_name
from agent_py_agent.agent.plugin_sandbox import (
    plugin_sandbox_problem,
    plugin_sandbox_spec,
    sandboxed_plugin_argv,
)
from agent_py_agent.tests.plugin_activation_fixtures import invoke_registered_tool, plugin_registry
from agent_py_agent.tests.test_plugin_any_language_samples import (
    VECTORS,
    _expected,
    _install_and_confirm,
    _materialize,
)
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_files_package import build_files_package
from scripts.check_clean_package import check_zip_findings

PROJECT = ROOT / "plugins" / "shuohao-novel-gates"
FIXTURES = ROOT / "agent_py_agent" / "tests" / "fixtures" / "shuohao_skills"
NODE = shutil.which("node")
EXTENSION = "my-agent/workspace-read-context"
STAGES = ("outline", "art", "script", "storyboard", "characters")
TOOLS = ("outline_check", "art_check", "script_check", "storyboard_check", "cast_check")


def _declaration():
    assert (PROJECT / "declaration.json").is_file(), "M-B1 插件声明尚未实现"
    return json.loads((PROJECT / "declaration.json").read_text())


def _context(root):
    policy = {"mode": "normal", "dangerous_roots": [], "owner_scope_root": None, "agent_home_root": None}
    return {"version": "1", "cwd": str(root.resolve()), "read_roots": [str(root.resolve())],
            "path_policy": policy, "granted_external_roots": [], "external_policy": policy}


def _workspace(root):
    for stage in STAGES:
        examples = FIXTURES / "skills" / f"novel-{stage}" / "examples"
        for source in examples.iterdir():
            shutil.copyfile(source, root / source.name)
    shutil.copytree(FIXTURES / "skills/novel-storyboard/references/test-fixtures/shot-recipes", root / "shots")


def _args(stage, operation="checkup"):
    stem = "cast" if stage == "characters" else stage
    result = {"path": f"渡口-{stem}.json", "operation": operation}
    references = {"art": ("cast",), "script": ("outline", "art"),
                  "storyboard": ("script", "outline", "cast", "art")}.get(stage, ())
    result.update({key: f"渡口-{key}.json" for key in references})
    if stage == "characters":
        result["operation"], result["book"] = "validate", "渡口.txt"
    return result


def _rpc(root, calls):
    _declaration()
    frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    frames.extend({"jsonrpc": "2.0", "id": index + 3, "method": "tools/call", "params": params}
                  for index, params in enumerate(calls))
    process = subprocess.run([NODE, str(PROJECT / "src/server.js")], cwd=root,
                             input="".join(json.dumps(frame) + "\n" for frame in frames),
                             text=True, capture_output=True, timeout=60, check=True)
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(responses) == len(frames), process.stderr
    return responses


def _call(tool, args, root):
    return {"name": tool, "arguments": args, "_meta": {EXTENSION: _context(root)}}


def _value(response):
    return json.loads(response["result"]["content"][0]["text"])


def test_package_is_reproducible_with_verbatim_licenses_and_nul(tmp_path):
    declaration = _declaration()
    assert [tool["name"] for tool in declaration["tools"]] == list(TOOLS)
    assert all(tool["requested_effect"] == "read_only" for tool in declaration["tools"])
    first = build_files_package(declaration, PROJECT, tmp_path / "first.zip")
    second = build_files_package(declaration, PROJECT, tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    assert not check_zip_findings(first)
    with zipfile.ZipFile(first) as archive:
        manifest = json.loads(archive.read("plugin.json"))
        assert manifest["schema_version"] == "plugin_package.v6"
        assert manifest["entry"]["interpreter"] == "node"
        for name in ("LICENSE", "NOTICE"):
            assert archive.read(name) == (FIXTURES / name).read_bytes()
        sources = json.loads(archive.read("UPSTREAM.json"))
        assert sources["commit"] == "7ebef4f2f53159ee1eaaec2793271a114a8be8cc"
        for entry in sources["files"]:
            actual = ROOT / entry["destination"]
            assert hashlib.sha256(actual.read_bytes()).hexdigest() == entry["sha256"]
        characters = "upstream/skills/novel-characters/scripts/novel-characters.mjs"
        assert archive.read(characters).count(b"\0") == 1
        assert all("selftest" not in name and "/examples/" not in name and "/report.mjs" not in name
                   for name in archive.namelist())
        assert "skills/storycast/examples/渡口.txt" in archive.read("PROVENANCE.md").decode()


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_node_read_port_matches_all_74_plus_20_vectors(tmp_path):
    _declaration()
    root, vectors = _materialize(tmp_path)
    runner = PROJECT / "conformance.js"
    output = subprocess.run([NODE, str(runner), str(VECTORS), root], text=True, capture_output=True,
                            timeout=60, check=True)
    result = json.loads(output.stdout)
    assert len(result["valid"]) == 74 and result["valid"] == _expected(vectors)
    assert len(result["invalid"]) == 20 and result["invalid"] == [True] * 20


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
@pytest.mark.parametrize("stage,tool,count", list(zip(STAGES, TOOLS, (14, 11, 10, 17, 0))))
def test_live_mcp_checks_match_upstream_and_leave_workspace_unchanged(tmp_path, stage, tool, count):
    _declaration()
    _workspace(tmp_path)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    operations = ["validate"] if stage == "characters" else ["validate", "checkup"]
    responses = _rpc(tmp_path, [_call(tool, _args(stage, op), tmp_path) for op in operations])
    listing = responses[1]["result"]["tools"]
    expected = [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                for t in _declaration()["tools"]]
    assert listing == expected
    assert responses[0]["result"]["capabilities"]["experimental"][EXTENSION] == {"versions": ["1"]}
    for operation, response in zip(operations, responses[2:]):
        value = _value(response)
        assert response["result"]["isError"] is False, value
        assert value["operation"] == operation and value["stage"] == stage
        assert value["passed"] is True, value
        assert value["counts"]["total"] == count and value["counts"]["failed"] == 0
        assert len(value["gates"]) == count
        argv = [NODE, str(PROJECT / f"upstream/skills/novel-{stage}/scripts/novel-{stage}.mjs"), operation,
                _args(stage)["path"]]
        argv.extend(item for key, path in _args(stage).items() if key in ("outline", "art", "cast", "script")
                    for item in (f"--{key}", path))
        if stage == "characters":
            argv.append("渡口.txt")
        if stage == "storyboard":
            argv.append("--no-log")
        oracle = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert oracle.returncode == 0, oracle.stderr
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before and not (tmp_path / ".gates.jsonl").exists()


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
@pytest.mark.parametrize("case,code", [("missing", "MISSING_CONTEXT"), ("invalid", "INVALID_CONTEXT"),
                                      ("outside", "PATH_READ_SCOPE_BLOCKED"), ("link", "PATH_READ_SCOPE_BLOCKED"),
                                      ("reference", "PATH_READ_SCOPE_BLOCKED"), ("credential", "PATH_CREDENTIAL_FILE_BLOCKED"),
                                      ("command", "INVALID_ARGUMENTS"), ("extra", "INVALID_ARGUMENTS")])
def test_mcp_rejects_unauthorized_reads_and_non_read_commands(tmp_path, case, code):
    _declaration()
    root = tmp_path / "workspace"
    root.mkdir()
    _workspace(root)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    request = _call("art_check", _args("art"), root)
    if case == "missing":
        request.pop("_meta")
        request["arguments"]["_meta"] = {EXTENSION: _context(root)}
    if case == "invalid":
        request["_meta"][EXTENSION]["version"] = "99"
    if case in ("outside", "link", "reference"):
        target = str(outside)
        if case == "link":
            (root / "link.json").symlink_to(outside)
            target = "link.json"
        request["arguments"]["cast" if case == "reference" else "path"] = target
    if case == "credential":
        (root / ".env").write_text("{}")
        request["arguments"]["path"] = ".env"
    if case == "command":
        request["arguments"]["operation"] = "seed"
    if case == "extra":
        request["arguments"]["out"] = "unwanted.json"
    value = _value(_rpc(root, [request])[-1])
    assert value["code"] == code, value
    assert not (root / "unwanted.json").exists()


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_storyboard_stats_reads_authorized_log_without_appending(tmp_path):
    _declaration()
    log = tmp_path / ".gates.jsonl"
    original = (json.dumps({"kind": "run", "failed": 1}) + "\n" +
                json.dumps({"kind": "fail", "gate": "coverage", "label": "覆盖", "detail": "缺镜头"}) + "\nBAD\n")
    log.write_text(original)
    value = _value(_rpc(tmp_path, [_call("storyboard_check", {"operation": "stats", "path": ".gates.jsonl"}, tmp_path)])[-1])
    assert value["stats"]["runs"] == 1 and value["stats"]["fails"] == 1
    assert value["stats"]["ranked"][0]["gate"] == "coverage"
    assert value["ignored_lines"] == 1 and log.read_text() == original


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_missing_optional_references_are_skipped_not_counted_as_passes(tmp_path):
    _declaration()
    _workspace(tmp_path)
    art = {"operation": "checkup", "path": "渡口-art.json"}
    board = {"operation": "checkup", "path": "渡口-storyboard.json", "script": "渡口-script.json"}
    responses = _rpc(tmp_path, [_call("art_check", art, tmp_path), _call("storyboard_check", board, tmp_path)])
    for response, gate_id in zip(responses[2:], ("no-names", "shot-recipe")):
        value = _value(response)
        gate = next(item for item in value["gates"] if item["id"] == gate_id)
        assert gate["status"] == "skipped" and gate["passed"] is None
        assert value["counts"]["skipped"] >= 1


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_cast_without_names_skips_no_names_gate(tmp_path):
    _declaration()
    _workspace(tmp_path)
    (tmp_path / "cast-noname.json").write_text(
        json.dumps({"characters": [{"id": "c1", "tier": "lead"}]}), encoding="utf-8")
    art = {"operation": "checkup", "path": "渡口-art.json", "cast": "cast-noname.json"}
    value = _value(_rpc(tmp_path, [_call("art_check", art, tmp_path)])[-1])
    gate = next(item for item in value["gates"] if item["id"] == "no-names")
    assert gate["status"] == "skipped" and gate["passed"] is None
    assert value["counts"]["skipped"] >= 1


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_outline_without_props_skips_prop_cap_gate(tmp_path):
    _declaration()
    _workspace(tmp_path)
    (tmp_path / "outline-noprops.json").write_text(json.dumps(
        {"params": {"episodes": 1}, "characters": [], "scenes": [], "beats": [], "episodes": []}),
        encoding="utf-8")
    outline = {"operation": "checkup", "path": "outline-noprops.json"}
    value = _value(_rpc(tmp_path, [_call("outline_check", outline, tmp_path)])[-1])
    gate = next(item for item in value["gates"] if item["id"] == "prop-cap")
    assert gate["status"] == "skipped" and gate["passed"] is None
    assert value["counts"]["skipped"] >= 1


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_empty_shots_array_skips_shot_recipe_and_schema_requires_min_items(tmp_path):
    declaration = _declaration()
    shots_schema = next(tool for tool in declaration["tools"]
                        if tool["name"] == "storyboard_check")["input_schema"]["properties"]["shots"]
    assert shots_schema.get("minItems") == 1
    _workspace(tmp_path)
    board = {"operation": "checkup", "path": "渡口-storyboard.json",
             "script": "渡口-script.json", "shots": []}
    value = _value(_rpc(tmp_path, [_call("storyboard_check", board, tmp_path)])[-1])
    gate = next(item for item in value["gates"] if item["id"] == "shot-recipe")
    assert gate["status"] == "skipped" and gate["passed"] is None
    assert value["counts"]["skipped"] >= 1


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_script_character_refs_gate_is_skipped_without_outline_even_with_cast(tmp_path):
    _declaration()
    _workspace(tmp_path)
    script = {"operation": "checkup", "path": "渡口-script.json", "cast": "渡口-cast.json"}
    value = _value(_rpc(tmp_path, [_call("script_check", script, tmp_path)])[-1])
    gates = {item["id"]: item for item in value["gates"]}
    assert {gate_id: gates[gate_id]["status"] for gate_id in ("refs-characters", "beats-claimed", "refs-scenes")} == \
        dict.fromkeys(("refs-characters", "beats-claimed", "refs-scenes"), "skipped")
    assert gates["refs-characters"]["passed"] is None and value["complete"] is False
    assert value["counts"]["skipped"] == 3 and value["counts"]["passed"] == value["counts"]["total"] - 3


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
@pytest.mark.parametrize("stage", STAGES + ("report",))
def test_pinned_upstream_selftests(tmp_path, stage):
    _declaration()
    tree = tmp_path / "upstream"
    shutil.copytree(FIXTURES, tree)
    shutil.copytree(PROJECT / "upstream", tree, dirs_exist_ok=True)
    relative = "scripts/report-selftest.mjs" if stage == "report" else f"skills/novel-{stage}/scripts/selftest.mjs"
    result = subprocess.run([NODE, str(tree / relative)], cwd=tree, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_report_sibling_cli_subprocess_runs_in_real_plugin_sandbox(tmp_path):
    _declaration()
    if plugin_sandbox_problem(True, tmp_path):
        pytest.skip("本机平台插件沙箱不可用；不把普通子进程结果当作沙箱证据")
    tree, data = tmp_path / "files", tmp_path / "private-data"
    shutil.copytree(FIXTURES, tree)
    shutil.copytree(PROJECT / "upstream", tree, dirs_exist_ok=True)
    data.mkdir()
    output = data / "report.html"
    probe = sandboxed_plugin_argv([NODE, "-e", "process.exit(0)"],
                                  plugin_sandbox_spec(cwd=tree, data_dir=data, owner_home=tmp_path))
    observed = subprocess.run(probe, cwd=tree, text=True, capture_output=True, timeout=60)
    if observed.returncode == 71 and "sandbox_apply: Operation not permitted" in observed.stderr:
        pytest.skip("系统明确拒绝应用 Seatbelt（exit 71）；report 子进程尚未执行，保留未验证")
    assert observed.returncode == 0, observed.stdout + observed.stderr
    argv = sandboxed_plugin_argv([NODE, str(tree / "scripts/report.mjs"), "--art",
                                 str(tree / "skills/novel-art/examples/渡口-art.json"), "--out", str(output)],
                                plugin_sandbox_spec(cwd=tree, data_dir=data, owner_home=tmp_path))
    environment = {**os.environ, "TMPDIR": str(data / ".tmp"), "MY_AGENT_PLUGIN_DATA_DIR": str(data)}
    result = subprocess.run(argv, cwd=tree, env=environment, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "<!doctype html" in output.read_text().lower()


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_real_host_install_confirmation_and_all_five_registered_tools(tmp_path):
    package = build_files_package(_declaration(), PROJECT, tmp_path / "plugin.zip")
    _workspace(tmp_path)
    service, plugin_id, confirmation = _install_and_confirm(tmp_path, package)
    assert plugin_id == "shuohao-novel-gates"
    assert confirmation["mode"] == "wide" and confirmation["runtime"]["interpreter"]["path"] == os.path.realpath(NODE)
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        for stage, tool, count in zip(STAGES, TOOLS, (14, 11, 10, 17, 0)):
            result = invoke_registered_tool(service, registry, plugin_tool_name(plugin_id, tool), _args(stage),
                                            request_id=stage)
            assert result["state"] == "succeeded", result
            assert result["ok"] is True, result
            assert result["stored_output"] == result["output"]
            # 宿主 output 是 JSON 信封，result 是插件的 JSON 文本；两层都解码，不能在转义文本中猜布尔值。
            value = json.loads(json.loads(result["output"])["result"])
            assert value["passed"] is True, value
            assert value["stage"] == stage
            assert value["operation"] == _args(stage)["operation"]
            assert value["counts"]["total"] == count and len(value["gates"]) == count
            assert value["counts"]["failed"] == 0 and value["problems"] == []
            assert value["counts"]["passed"] + value["counts"]["skipped"] == count
            assert value["complete"] is (value["counts"]["skipped"] == 0)
    finally:
        registry.close_mcp_clients()


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
@pytest.mark.parametrize("stage,tool", list(zip(STAGES, TOOLS)))
def test_structurally_invalid_documents_are_never_reported_as_passed(tmp_path, stage, tool):
    _declaration()
    _workspace(tmp_path)
    (tmp_path / "broken.json").write_text("{}")
    args = {**_args(stage, "validate"), "path": "broken.json"}
    response = _rpc(tmp_path, [_call(tool, args, tmp_path)])[-1]
    assert _value(response).get("passed") is not True


@pytest.mark.skipif(NODE is None, reason="本机没有 node")
def test_quotes_and_shot_cards_are_read_from_current_authorized_files(tmp_path):
    _declaration()
    _workspace(tmp_path)
    (tmp_path / "wrong-book.txt").write_text("原文完全不包含角色的引文。")
    bad_cast = {**_args("characters"), "book": "wrong-book.txt"}
    shots = [str(p.relative_to(tmp_path)) for p in sorted((tmp_path / "shots").glob("*.md"))]
    board = {**_args("storyboard"), "shots": shots}
    responses = _rpc(tmp_path, [_call("cast_check", bad_cast, tmp_path), _call("storyboard_check", board, tmp_path)])
    cast, storyboard = map(_value, responses[2:])
    assert cast["passed"] is False and cast["problems"]
    gate = next(item for item in storyboard["gates"] if item["id"] == "shot-recipe")
    assert gate["status"] != "skipped"
