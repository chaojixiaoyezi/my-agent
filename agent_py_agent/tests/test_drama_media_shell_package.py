"""drama-media-shell 真实标准包、MCP 工具与读写上下文组件验收；不代替真实 TUI 或模型验收。"""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.tooling.workspace_write_scope import build_workspace_write_context
from agent_py_agent.agent.workspace_read_context import WORKSPACE_READ_EXTENSION
from agent_py_agent.agent.workspace_write_context import WORKSPACE_WRITE_EXTENSION
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_workspace_read_context import read_context
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package

PROJECT = ROOT / "plugins/drama-media-shell"
EXPECTED_TOOLS = {
    "image_prompt_check", "container_check", "motion_timing_check", "music_spec_check",
    "production_prepare", "production_confirm", "production_run", "production_status",
    "production_audit", "production_collect",
}
NOTICE = "夹具，不是真实生成"

IMAGE_JSONL = """{"record_type":"sources","schema_version":"1.0.0","sources":{"characters":{"owner":"short-drama-assets","artifact":"characters.jsonl"},"looks":{"owner":"short-drama-assets","artifact":"looks.jsonl"},"composition-reference":{"owner":"creator","artifact":"composition-reference.png"}}}
{"spec_id":"IMG-LIN-RAIN","status":"candidate","purpose":"character_sheet","asset_binding":{"identity_ref":{"src":"characters","record_id":"CHAR-LIN"},"variant_ref":{"src":"looks","record_id":"LOOK-LIN-RAIN"}},"source_refs":[{"src":"characters","record_id":"CHAR-LIN","field":"/identity_anchors","role":"identity_anchor"}],"reference_bindings":[{"slot_id":"REF-LIN-COMPOSITION","order":1,"artifact_ref":{"src":"composition-reference","record_id":"REF-COMP-01"},"role":"composition","may_control":["layout"],"must_not_control":["identity"],"admission_status":"creator_described","unresolved_risks":[]}],"identity_or_form_anchors":["black hair"],"negative_constraints":["no readable text"],"generic_prompt":"Character reference sheet, black hair, plain background, no readable text."}
"""
MUSIC_JSONL = """{"record_type":"sources","schema_version":"1.0.0","sources":{"screenplay":{"owner":"short-drama-write","artifact":"episode.md"}}}
{"music_id":"MUS-EP001-01","scope":{"episode_id":"EP001","start_seconds":0,"end_seconds":12},"source_refs":[{"src":"screenplay","record_id":"EP001-SC001"}],"narrative_function":"restrained opening","prompt":"Instrumental restrained urban drama score.","mode":"instrumental","lyrics":null,"mix_intent":{"entry":"fade in","exit":"fade out","duck_under_dialogue":true,"loop":false},"status":"candidate"}
"""


# LLM: 构建与安装都使用真实标准后端；独立解释器预先只装 SDK，插件 wheel 不借宿主源码运行。
# 函数用途: 构建 drama-media-shell 包、安装入口 wheel并返回解释器、manifest 和包路径。
@pytest.fixture(scope="module")
def installed_drama(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("drama-media-shell")
    bundle = build_plugin_package(PROJECT, "drama_media_shell/declaration.json", (sdk,), root / "drama.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    with ZipFile(bundle) as archive:
        wheel = root / package.manifest.entry_wheel.rsplit("/", 1)[1]
        wheel.write_bytes(archive.read(package.manifest.entry_wheel))
    result = subprocess.run(
        [sys.executable, "-m", "pip", "--isolated", "--python", str(python), "install",
         "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return python, package.manifest, bundle, sdk


# LLM: 测试进程只得到临时插件数据目录，不共享仓库或用户数据；由原 MCP 客户端负责启动和回收。
# 函数用途: 启动已安装的 drama-media-shell MCP 服务。
def start(installed, data: Path) -> MCPStdioClient:
    python, _, _, _ = installed
    client = MCPStdioClient(_config("", name="drama-media-shell", command=str(python),
                                   args=["-I", "-m", "drama_media_shell"],
                                   env={"MY_AGENT_PLUGIN_DATA_DIR": str(data)}))
    client.start()
    return client


# LLM: 默认不给写权限（模拟宿主不给只读工具写权限）；需要写时传 _write=True，它不会作为工具参数发出。
#   权限值全部由核心同源构造器产生，测试不手写协议。
# 函数用途: 调用一个真实 MCP 工具并解析结构化 JSON 正文。
def invoke(client, workspace: Path, tool: str, **arguments):
    write = bool(arguments.pop("_write", False))
    metadata = {WORKSPACE_READ_EXTENSION: read_context(workspace).to_payload()}
    if write:
        policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[])
        context = build_workspace_write_context(cwd=workspace, write_boundary=None, path_policy=policy)
        metadata[WORKSPACE_WRITE_EXTENSION] = context.to_payload()
    result = client.call_tool(tool, arguments, request_meta=metadata)
    return result["isError"], json.loads(result["content"])


@pytest.fixture
def dirs(tmp_path):
    workspace, data = tmp_path / "workspace", tmp_path / "plugin-data"
    workspace.mkdir()
    data.mkdir()
    return workspace, data


@pytest.fixture
def drama(installed_drama, dirs):
    client = start(installed_drama, dirs[1])
    try:
        yield client
    finally:
        client.stop()


def test_manifest_tools_resources_and_reproducible_build(installed_drama, tmp_path):
    _, manifest, first, sdk = installed_drama
    assert {tool.name for tool in manifest.tools} == EXPECTED_TOOLS
    assert all("夹具产物不算生成成功" in tool.description
               for tool in manifest.tools if tool.name.startswith("production_"))
    assert {tool.name: tool.requested_effect for tool in manifest.tools} == {
        **dict.fromkeys(EXPECTED_TOOLS - {"production_run", "production_collect"}, "read_only"),
        "production_run": "mutating", "production_collect": "mutating",
    }
    with ZipFile(first) as outer:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in outer.infolist())
        wheel_name = manifest.entry_wheel
        with ZipFile(io.BytesIO(outer.read(wheel_name))) as wheel:
            names = wheel.namelist()
            assert not any("provider_adapters.py" in name or "remotion" in name.casefold() for name in names)
            assert not any("creator-first" in name or "evaluations/" in name or "让你管账号" in name for name in names)
            license_name, = [name for name in names if name.endswith("drama_media_shell/LICENSE.drama-skills")]
            provenance_name, = [name for name in names if name.endswith("drama_media_shell/PROVENANCE.md")]
            assert b"Copyright (c) 2026 drama-skills contributors" in wheel.read(license_name)
            provenance = wheel.read(provenance_name).decode("utf-8")
            assert "0e8929881bb59248618c4f402707c64723adc017" in provenance
            assert "provider_adapters.py" in provenance and "未迁移" in provenance
    second = build_plugin_package(PROJECT, "drama_media_shell/declaration.json", (sdk,), tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()


def test_tools_list_matches_manifest(drama, installed_drama):
    tools = drama.list_tools()
    _, manifest, _, _ = installed_drama
    assert {tool.name: (tool.description, tool.input_schema) for tool in tools} == {
        tool.name: (tool.description, tool.input_schema) for tool in manifest.tools
    }


def test_four_checkers_run_through_read_context(drama, dirs):
    workspace, _ = dirs
    (workspace / "image.jsonl").write_text(IMAGE_JSONL, encoding="utf-8")
    (workspace / "music.jsonl").write_text(MUSIC_JSONL, encoding="utf-8")
    shots = [{"shot_id": "SHOT-1", "duration_seconds": 2.0}]
    containers = [{"container_id": "CONT-1", "members": [{"order": 1, "shot_ref": {"record_id": "SHOT-1"},
                   "accepted_duration": 2.0}], "membership_basis": {"source_order_contiguous": "yes",
                   "binding_chain_equal": "yes", "scene_boundary_not_crossed": "yes"}, "container_duration": 2.0}]
    motion = [{"motion_id": "MOTION-1", "shot_ref": {"record_id": "SHOT-1"},
               "boundary_refs": {"duration": {"record_id": "SHOT-1", "value_seconds": 2.0}},
               "timing_plan": {"mode": "explicit", "declared_total_or_endpoint_seconds": 2.0},
               "ordered_subject_motion": [{"timing": {"mode": "explicit", "value": "0-2s"}}]}]
    for name, value in (("shots.jsonl", shots), ("containers.jsonl", containers), ("motion.jsonl", motion)):
        (workspace / name).write_text("\n".join(json.dumps(item) for item in value) + "\n", encoding="utf-8")
    calls = [
        ("image_prompt_check", {"path": "image.jsonl"}, "valid"),
        ("music_spec_check", {"path": "music.jsonl"}, "valid"),
        ("container_check", {"containers_path": "containers.jsonl", "shots_path": "shots.jsonl"}, "pass"),
        ("motion_timing_check", {"motion_specs_path": "motion.jsonl", "shots_path": "shots.jsonl"}, "pass"),
    ]
    for tool, arguments, status in calls:
        error, result = invoke(drama, workspace, tool, **arguments)
        assert not error and result["status"] == status, (tool, result)


def test_fixture_job_flow_private_records_and_collect(drama, dirs):
    workspace, data = dirs
    job = {"schema_version": "1.0", "job_id": "JOB-MUSIC-1", "modality": "music", "adapter": "fixture",
           "prompt": "restrained instrumental tension", "references": [], "outputs": ["production/cue.wav"],
           "parameters": {"is_instrumental": True}, "overwrite": False}
    (workspace / "job.json").write_text(json.dumps(job), encoding="utf-8")
    before = sorted(path.relative_to(workspace) for path in workspace.rglob("*"))
    error, preview = invoke(drama, workspace, "production_prepare", job_path="job.json")
    assert not error and preview["state"] == "needs_confirmation" and preview["generation_success"] is False
    assert sorted(path.relative_to(workspace) for path in workspace.rglob("*")) == before
    error, wrong = invoke(drama, workspace, "production_confirm", job_id=job["job_id"], confirmation="wrong")
    assert error and wrong["code"] == "CONFIRMATION_REQUIRED"
    error, confirmed = invoke(drama, workspace, "production_confirm", job_id=job["job_id"],
                              confirmation=preview["confirmation"])
    assert not error and confirmed["state"] == "confirmed"
    error, missing = invoke(drama, workspace, "production_run", job_id=job["job_id"])
    assert error and missing["code"] == "MISSING_WRITE_CONTEXT"
    error, run = invoke(drama, workspace, "production_run", _write=True, job_id=job["job_id"])
    assert not error and run["state"] == "fixture_succeeded" and run["fixture"] is True
    assert run["generation_success"] is False and NOTICE in run["notice"]
    target = workspace / job["outputs"][0]
    assert target.read_bytes().startswith(b"RIFF")
    error, status = invoke(drama, workspace, "production_status", job_id=job["job_id"])
    assert not error and status["state"] == "fixture_succeeded" and status["fixture"] is True
    target.unlink()
    error, collected = invoke(drama, workspace, "production_collect", _write=True, job_id=job["job_id"])
    assert not error and collected["collected"] is True and collected["generation_success"] is False
    assert target.read_bytes().startswith(b"RIFF")
    error, audit = invoke(drama, workspace, "production_audit")
    assert not error and audit["jobs"]["total"] == 1 and audit["quality_verdict"] == "not_assessed"
    assert any(path.name.endswith(".json") for path in data.rglob("*"))
    assert not (workspace / ".short-drama").exists()


def test_real_provider_request_is_structurally_refused(drama, dirs):
    workspace, _ = dirs
    job = {"schema_version": "1.0", "job_id": "JOB-PAID-1", "modality": "video", "adapter": "seedance",
           "prompt": "slow push in", "references": [], "outputs": ["production/shot.mp4"],
           "parameters": {"duration": 5}, "overwrite": False}
    (workspace / "paid.json").write_text(json.dumps(job), encoding="utf-8")
    _, preview = invoke(drama, workspace, "production_prepare", job_path="paid.json")
    _, confirmed = invoke(drama, workspace, "production_confirm", job_id=job["job_id"],
                          confirmation=preview["confirmation"])
    assert confirmed["state"] == "confirmed"
    error, refused = invoke(drama, workspace, "production_run", _write=True, job_id=job["job_id"])
    assert error and refused == {
        "code": "PROVIDER_NOT_CONFIGURED", "provider": "seedance",
        "message": "未配置供应商，本插件不调用付费生成。", "paid_generation_called": False,
    }
    assert not (workspace / job["outputs"][0]).exists()


# LLM: 这组辅助函数把固定上游 selftest 的内存变异原样送过真实 MCP 入口，不直接导入源码绕过包和读取上下文。
# 函数用途: 将 JSONL 文本拆成独立对象，供上游变异用例深拷贝。
def _jsonl_records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# LLM: 每个变异覆盖同一临时文件即可，调用后立即断言；测试不依赖文件名或样例专项产品分支。
# 函数用途: 写入一组 JSONL 记录并调用单文件检查器。
def _call_records(client, workspace: Path, tool: str, records: list[dict]):
    path = workspace / "upstream-selftest.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    return invoke(client, workspace, tool, path=path.name)


# LLM: 上游 selftest 用错误标记保护具体合同；迁移测试继续检查标记，而不是只检查笼统失败。
# 函数用途: 断言一组 MCP 检查输入按预期结构化失败并含上游错误标记。
def _expect_checker_failure(client, workspace: Path, case: tuple[str, list[dict], str]):
    tool, records, marker = case
    error, result = _call_records(client, workspace, tool, records)
    assert error and result["code"] == "CHECK_FAILED"
    assert marker in result["message"], result


# LLM: 紧凑引用展开规则逐字来自固定上游 image selftest，用于证明无 sources 首行的旧格式仍兼容。
# 函数用途: 将一条 src 引用展开为内联 owner/artifact 快照。
def _expanded_reference(reference: dict, sources: dict[str, dict]) -> dict:
    entry = sources[reference["src"]]
    return {"owner": entry["owner"], "artifact": entry["artifact"],
            **{key: value for key, value in reference.items() if key != "src"}}


def test_upstream_image_prompt_selftest_cases(drama, dirs):
    """迁入固定提交 image selftest 的 10 组成功/失败合同。"""
    workspace, _ = dirs
    parsed = _jsonl_records(IMAGE_JSONL)
    header, records = parsed[0], parsed[1:]
    sources = header["sources"]
    error, valid = _call_records(drama, workspace, "image_prompt_check", parsed)
    assert not error and valid["specs"] == 1 and len(sources) == 3

    duplicate = [header, records[0], copy.deepcopy(records[0])]
    bad_order = copy.deepcopy(parsed)
    bad_order[1]["reference_bindings"].append(copy.deepcopy(bad_order[1]["reference_bindings"][0]))
    leaked = copy.deepcopy(parsed)
    leaked[1]["provider"] = "example"
    nested_provider = copy.deepcopy(parsed)
    nested_provider[1]["reference_bindings"][0]["provider"] = "example"
    nested_secret = copy.deepcopy(parsed)
    nested_secret[1]["asset_binding"]["credentials"] = {"token": "not-safe"}
    undeclared = copy.deepcopy(parsed)
    undeclared[1]["asset_binding"]["identity_ref"]["src"] = "no-such-source"
    unbound = copy.deepcopy(parsed)
    del unbound[1]["source_refs"][0]["src"]
    cases = [
        (duplicate, "duplicate spec_id"), (bad_order, "duplicate slot_id"),
        (leaked, "provider execution fields"),
        (nested_provider, "reference_bindings[0].provider"),
        (nested_secret, "asset_binding.credentials"),
        (undeclared, "REF_SRC_IS_NOT_DECLARED"), (unbound, "REF_HAS_NO_UPSTREAM_BINDING"),
    ]
    for document, marker in cases:
        _expect_checker_failure(drama, workspace, ("image_prompt_check", document, marker))
    for syntax in ("--ar 9:16", "(red coat:1.2)", "cat::2"):
        engine_syntax = copy.deepcopy(parsed)
        engine_syntax[1]["generic_prompt"] += f" {syntax}"
        _expect_checker_failure(
            drama, workspace, ("image_prompt_check", engine_syntax, "engine-specific syntax")
        )

    inline = copy.deepcopy(records)
    binding = inline[0]["asset_binding"]
    binding["identity_ref"] = _expanded_reference(binding["identity_ref"], sources)
    binding["variant_ref"] = _expanded_reference(binding["variant_ref"], sources)
    inline[0]["source_refs"] = [_expanded_reference(ref, sources) for ref in inline[0]["source_refs"]]
    for slot in inline[0]["reference_bindings"]:
        slot["artifact_ref"] = _expanded_reference(slot["artifact_ref"], sources)
    error, compatible = _call_records(drama, workspace, "image_prompt_check", inline)
    assert not error and compatible["specs"] == 1


def test_upstream_music_spec_selftest_cases(drama, dirs):
    """迁入固定提交 video-prompts selftest 的 10 组音乐规范合同。"""
    workspace, _ = dirs
    parsed = _jsonl_records(MUSIC_JSONL)
    header, spec = parsed
    error, valid = _call_records(drama, workspace, "music_spec_check", parsed)
    assert not error and valid["music_specs"] == 1 and spec["source_refs"][0]["src"] == "screenplay"

    expanded = copy.deepcopy(spec)
    expanded["source_refs"] = [{**header["sources"]["screenplay"], "record_id": "EP001-SC001"}]
    error, compatible = _call_records(drama, workspace, "music_spec_check", [expanded])
    assert not error and compatible["music_specs"] == 1

    undeclared = copy.deepcopy(spec)
    undeclared["source_refs"] = [{"src": "screenplai", "record_id": "EP001-SC001"}]
    unbound = copy.deepcopy(spec)
    unbound["source_refs"] = [{"record_id": "EP001-SC001"}]
    leaked = copy.deepcopy(spec)
    leaked["model"] = "example"
    song_without_lyrics = copy.deepcopy(spec)
    song_without_lyrics["mode"] = "song"
    invalid_scope = copy.deepcopy(spec)
    invalid_scope["scope"]["end_seconds"] = 0
    unsupported = copy.deepcopy(spec)
    unsupported["token"] = "not provider-neutral"
    stale_header = copy.deepcopy(header)
    stale_header["sources"]["screenplay"]["hash"] = "not-a-sha256"
    cases = [
        ([header, undeclared], "REF_SRC_IS_NOT_DECLARED"),
        ([header, unbound], "REF_HAS_NO_UPSTREAM_BINDING"),
        ([header, spec, copy.deepcopy(spec)], "duplicate music_id"),
        ([header, leaked], "provider execution fields"),
        ([header, song_without_lyrics], "lyrics"),
        ([header, invalid_scope], "0 <= start < end"),
        ([header, unsupported], "unsupported fields"),
        ([stale_header, spec], "unsupported fields"),
    ]
    for document, marker in cases:
        _expect_checker_failure(drama, workspace, ("music_spec_check", document, marker))


def test_read_and_write_symlinks_are_rejected(drama, dirs):
    workspace, _ = dirs
    (workspace / "real-image.jsonl").write_text(IMAGE_JSONL, encoding="utf-8")
    (workspace / "linked-image.jsonl").symlink_to("real-image.jsonl")
    error, refused_read = invoke(drama, workspace, "image_prompt_check", path="linked-image.jsonl")
    assert error and refused_read["code"] == "UNSAFE_PATH"

    job = {"schema_version": "1.0", "job_id": "JOB-LINK-1", "modality": "music",
           "adapter": "fixture", "prompt": "offline fixture", "references": [],
           "outputs": ["production/link.wav"], "parameters": {}, "overwrite": True}
    (workspace / "link-job.json").write_text(json.dumps(job), encoding="utf-8")
    _, preview = invoke(drama, workspace, "production_prepare", job_path="link-job.json")
    invoke(drama, workspace, "production_confirm", job_id=job["job_id"],
           confirmation=preview["confirmation"])
    (workspace / "production").mkdir()
    real = workspace / "real.wav"
    real.write_bytes(b"do-not-overwrite")
    (workspace / job["outputs"][0]).symlink_to("../real.wav")
    error, refused_write = invoke(drama, workspace, "production_run", _write=True, job_id=job["job_id"])
    assert error and refused_write["code"] == "UNSAFE_OUTPUT_PATH"
    assert real.read_bytes() == b"do-not-overwrite"


def test_confirmed_input_change_requires_reconfirmation(drama, dirs):
    workspace, _ = dirs
    source = workspace / "prompt-source.txt"
    source.write_text("version one", encoding="utf-8")
    job = {"schema_version": "1.0", "job_id": "JOB-INPUT-1", "modality": "image",
           "adapter": "fixture", "prompt": "offline fixture", "source": source.name,
           "references": [], "outputs": ["production/frame.png"],
           "parameters": {}, "overwrite": False}
    (workspace / "input-job.json").write_text(json.dumps(job), encoding="utf-8")
    _, preview = invoke(drama, workspace, "production_prepare", job_path="input-job.json")
    invoke(drama, workspace, "production_confirm", job_id=job["job_id"],
           confirmation=preview["confirmation"])
    source.write_text("version two", encoding="utf-8")
    error, status = invoke(drama, workspace, "production_status", job_id=job["job_id"])
    assert not error and status["state"] == "needs_reconfirmation"
    error, refused = invoke(drama, workspace, "production_run", _write=True, job_id=job["job_id"])
    assert error and refused["code"] == "NEEDS_RECONFIRMATION"
    assert not (workspace / job["outputs"][0]).exists()
